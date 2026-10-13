#!/usr/bin/env python3
"""Independent coverage comparisons, private continuation, and quota model assumptions."""

import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


shadow = module("shadow", "services/github-state/scripts/shadow.py")
capacity = module("capacity", "scripts/github-state-capacity.py")
observe = module("observe", "services/github-state/scripts/observe_reconciliation.py")

reference = shadow.fingerprints(
    "open_prs", [{"number": 1, "body": "private"}, {"number": 99, "body": "missing from both"}]
)
mirror = shadow.fingerprints("open_prs", [{"number": 1, "body": "private"}])
assert shadow.compare(reference, mirror) == {"missing": 1, "extra": 0, "changed": 0}
assert "private" not in json.dumps(reference)
rest_job = {
    "id": 1,
    "run_id": 77,
    "name": "build",
    "status": "completed",
    "conclusion": "success",
    "head_sha": "a" * 40,
}
webhook_job = {**rest_job, "run_attempt": 2, "head_branch": "staging"}
assert shadow.fingerprints("workflow_jobs", [rest_job]) == shadow.fingerprints("workflow_jobs", [webhook_job])
assert shadow.fingerprints("workflow_jobs", [{**rest_job, "conclusion": "failure"}]) != shadow.fingerprints(
    "workflow_jobs", [webhook_job]
)
summary = {}
item = {"environment": "test", "operation": "issue_comments", "args": {"number": 7}, "missing": 1}
key = json.dumps(["test", "issue_comments", {"number": 7}], sort_keys=True)
shadow.update_convergence(summary, [item], {key}, 100)
shadow.update_convergence(summary, [], set(), 200)
assert key in summary["outstanding"], "an unsampled query cannot appear fixed"
shadow.update_convergence(summary, [], {key}, 250)
assert summary["converged"][0]["seconds"] == 150 and not summary["outstanding"]


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    (directory / "admin-token").write_text("test-private-admin")
    seen = []

    def fetch(request, **kwargs):
        assert request.full_url.endswith("/admin/reference")
        body = json.loads(request.data)
        seen.append(body)
        page = body["page"]
        data = {"data": [{"id": str(page), "body": "secret comment"}], "api": "core", "points": 1}
        if page < 3:
            data["cursor"] = "page:" + str(page + 1)
        return Response(data)

    with patch.object(shadow.urllib.request, "urlopen", side_effect=fetch):
        first = shadow.Reference(directory, directory, limit=2)
        assert first.read("org/repo", "issue_comments", number=7) is None and first.requests == 2
        checkpoints = list((directory / "reference-checkpoints").glob("*.json"))
        assert len(checkpoints) == 1 and "secret comment" not in checkpoints[0].read_text()
        second = shadow.Reference(directory, directory, limit=2)
        result = second.read("org/repo", "issue_comments", number=7)
        assert (
            set(result) == {"1", "2", "3"}
            and second.requests == 1
            and not list((directory / "reference-checkpoints").glob("*.json"))
        )
    assert [r["page"] for r in seen] == [1, 2, 3]
    deferred = shadow.Reference(directory, directory)
    with patch.object(
        shadow.urllib.request, "urlopen", return_value=Response({"deferred": True, "category": "primary:core"})
    ):
        assert deferred.read("org/repo", "files", number=7) is None and deferred.deferred == "primary:core"
    transient = shadow.Reference(directory, directory, limit=1)
    with (
        patch.object(
            shadow.urllib.request,
            "urlopen",
            side_effect=[
                Response({"deferred": True, "category": "capacity"}),
                Response({"data": [], "api": "core", "points": 1}),
            ],
        ),
        patch.object(shadow.time, "sleep"),
    ):
        assert transient.read("org/repo", "files", number=8) == {}
    assert transient.requests == 1 and transient.local_deferrals == 1 and transient.points == 1

old = capacity.scenario(1000, 200, 100, mandatory_ci=True)
new = capacity.scenario(1000, 200, 100)
assert old["rest_requests_per_hour"] - new["rest_requests_per_hour"] == 139
assert old["graphql_points_per_hour"] == new["graphql_points_per_hour"]
stress = capacity.scenario(
    1000, 200, 100, important_active_runs=10, important_completions=20, requested_workflow_runs=40
)
assert stress["rest_components"]["workflow_job_reconciliation"] == 660
healthy = {
    "state": {"reconciliation_version": 2, "revision": 1, "last_repair": 1},
    "quota": {"reconciliation_version": 2},
}
samples = [{"at": 300 * i, "health": json.loads(json.dumps(healthy))} for i in range(13)]
samples[-1]["health"]["state"].update(revision=2, last_repair=2)
assert observe.evaluate(samples, interval=300) == []
assert "observation_window_incomplete" in observe.evaluate(samples[:-1], interval=300)
interrupted = json.loads(json.dumps(samples))
for sample in interrupted[7:]:
    sample["at"] += 3600
assert observe.observed_seconds(interrupted, 300) == 3300
assert "observation_window_incomplete" in observe.evaluate(interrupted, interval=300)
assert (
    observe.observed_seconds(
        [{"at": 0}, {"at": 60}, {"at": 600, "interval": 300}, {"at": 900, "interval": 300}, {"at": 960}]
    )
    == 960
)
samples[-1]["health"]["state"].update(
    lanes=[{"lane": "requested", "eligible": 1, "oldest_requested": 1000}],
    waits=[{"lane": "archive", "wait_reason": "primary:core"}],
)
assert "requested_gap_not_progressing" in observe.evaluate(samples, interval=300)
samples[-1]["health"]["state"]["waits"] = [{"lane": "requested", "wait_reason": "primary:core"}]
assert "requested_gap_not_progressing" not in observe.evaluate(samples, interval=300)
print(
    "PASS: independent inventory gaps, comparison convergence, private reference pagination, and explicit CI model inputs"
)
