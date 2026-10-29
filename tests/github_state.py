#!/usr/bin/env python3
"""Service age independence, conditional results, outage fallback, and cutoff configuration."""

import http.client
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_state as state
from tauceti_worker import github_transport as transport
from tauceti_worker.cli import build_parser
from tauceti_worker.config import max_open_prs, positive_integer
from tauceti_worker.survey import Survey
from tauceti_worker.worker_manager import WorkersError, WorkerSpec

for value in (True, False, 0, -1, 1.5, "1.5", "invalid", "0"):
    try:
        positive_integer(value)
    except ValueError:
        pass
    else:
        raise AssertionError(value)
assert positive_integer("128") == 128
with patch.dict(os.environ, {"TAUCETI_MAX_OPEN_PRS": "128"}):
    assert max_open_prs() == 128 and Survey("test").max_open_prs == 128
    sv = Survey("test", max_open_prs=128)
    with patch("tauceti_worker.survey.roadmap_open_count", return_value=127):
        sv.rescope_roadmap()
        assert not sv.roadmap_backpressure
    with patch("tauceti_worker.survey.roadmap_open_count", return_value=128):
        sv.rescope_roadmap()
        assert sv.roadmap_backpressure
spec = WorkerSpec.from_dict(
    {
        "id": "science",
        "max_open_prs": 128,
        "github_state_url": "https://state.test",
        "github_state_token_file": "/private/token",
    },
    0,
)
assert spec.as_dict()["max_open_prs"] == 128 and "--max-open-prs" in spec.work_argv()
assert WorkerSpec.from_dict(spec.as_dict(), 0).fingerprint() == spec.fingerprint()
for value in (True, 0, -1, 1.2, "128"):
    try:
        WorkerSpec.from_dict({"id": "science", "max_open_prs": value}, 0)
    except WorkersError:
        pass
    else:
        raise AssertionError(value)
assert build_parser().parse_args(["work", "--max-open-prs", "128"]).max_open_prs == 128

# Old runtimes persisted a hash without the applied definition. This must still protect paid work.
from tauceti_worker.worker_manager import _defer_read_policy_reload

old = WorkerSpec(id="busy")
new = WorkerSpec(id="busy", max_open_prs=128)
assert _defer_read_policy_reload(new, {"spec_hash": old.fingerprint(), "state": "reviewing"})
assert _defer_read_policy_reload(new, {"applied_spec": old.as_dict(), "state": "reviewing"})
assert _defer_read_policy_reload(new, {"spec_hash": "unknown", "state": "authoring"})


class Response(io.BytesIO):
    def __init__(self, payload, etag='"1"'):
        super().__init__(json.dumps(payload).encode())
        self.headers = {"ETag": etag}


# Unconfigured Tau Ceti workers read the public mirror without a per-machine secret.
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp}):
    for key in ("TAUCETI_GITHUB_STATE_URL", "TAUCETI_GITHUB_STATE_TOKEN_FILE", "TAUCETI_REPO"):
        os.environ.pop(key, None)
    public = {
        "version": 1,
        "repository": state.PUBLIC_REPOSITORY,
        "operation": "issue_comments",
        "status": "ok",
        "complete": True,
        "revision": 1,
        "observed_at": 1,
        "verified_at": 1,
        "data": [{"id": "123", "body": "mirrored"}],
    }
    with patch.object(state.urllib.request, "urlopen", return_value=Response(public)) as fetch:
        assert state.read(state.PUBLIC_REPOSITORY, "issue_comments", number=7) == (
            True,
            [{"id": 123, "body": "mirrored"}],
        )
        request = fetch.call_args.args[0]
        assert request.full_url.startswith(state.DEFAULT_URL + "/v1/query?")
        assert not request.has_header("Authorization")
    with (
        patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": ""}),
        patch.object(state.urllib.request, "urlopen") as fetch,
    ):
        assert state.read(state.PUBLIC_REPOSITORY, "issue_comments", number=7) == (False, None)
        fetch.assert_not_called()
    with (
        patch.dict(os.environ, {"TAUCETI_REPO": "other/private"}),
        patch.object(state.urllib.request, "urlopen") as fetch,
    ):
        assert state.read("other/private", "issue_comments", number=7) == (False, None)
        fetch.assert_not_called()
    with (
        patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://custom.test"}),
        patch.object(state.urllib.request, "urlopen") as fetch,
    ):
        assert state.read(state.PUBLIC_REPOSITORY, "issue_comments", number=7) == (False, None)
        assert state.last_failure()["reason"] == "client_auth_configuration"
        fetch.assert_not_called()
    with (
        patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_TOKEN_FILE": tmp + "/missing"}),
        patch.object(state.urllib.request, "urlopen") as fetch,
    ):
        assert state.read(state.PUBLIC_REPOSITORY, "issue_comments", number=7) == (False, None)
        assert state.last_failure()["reason"] == "client_auth_configuration"
        fetch.assert_not_called()


with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "GH_TOKEN": "fake",
            "TAUCETI_GITHUB_SHARED_DIR": tmp,
            "TAUCETI_GITHUB_STATE_URL": "https://state.test",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": tmp + "/token",
        },
    ),
):
    Path(tmp + "/token").write_text("client-secret")
    observed = {
        "version": 1,
        "repository": "org/repo",
        "operation": "workflow_jobs",
        "status": "ok",
        "complete": False,
        "coverage": "webhook_observed",
        "revision": 1,
        "data": [{"id": "9"}],
    }
    with patch.object(state.urllib.request, "urlopen", return_value=Response(observed)):
        assert state.read("org/repo", "workflow_jobs", id="77") == (False, None)
    with patch.object(state.urllib.request, "urlopen", return_value=Response(observed)) as fetch:
        available, result = state.read_workflow_jobs("org/repo", 77, allow_observed=True)
        assert available and result["complete"] is False and result["coverage"] == "webhook_observed"
        assert "allow_observed=true" in fetch.call_args.args[0].full_url
    payload = {
        "version": 1,
        "repository": "org/repo",
        "operation": "issue_comments",
        "status": "ok",
        "complete": True,
        "revision": 1,
        "observed_at": 1,
        "verified_at": 1,
        "data": [{"id": "123", "body": "old-but-newest-available"}],
    }
    # The real mirror returned a complete 5KB history in 3.58s. The old
    # 2.4s per-attempt timeout abandoned it twice and opened the shared circuit.
    clock = [0.0]

    def healthy_slow_read(request, timeout):
        assert 4.9 <= timeout <= 5.0
        clock[0] += 3.6
        return Response(payload)

    with (
        patch.object(state.time, "monotonic", side_effect=lambda: clock[0]),
        patch.object(state.urllib.request, "urlopen", side_effect=healthy_slow_read) as fetch,
    ):
        assert state.read("org/repo", "issue_comments", number=7)[0]
        assert fetch.call_count == 1

    def stalled_read(request, timeout):
        clock[0] += timeout
        raise TimeoutError("timed out")

    with (
        patch.object(state.time, "monotonic", side_effect=lambda: clock[0]),
        patch.object(state.urllib.request, "urlopen", side_effect=stalled_read) as fetch,
    ):
        assert state.read("org/repo", "issue_comments", number=7) == (False, None)
        assert fetch.call_count == 1, "a stalled read must exhaust the same five-second budget"
        assert clock[0] == 8.6
    with patch.object(state.urllib.request, "urlopen", return_value=Response(payload)) as fetch:
        available, rows = state.read("org/repo", "issue_comments", number=7)
        assert available and rows[0]["id"] == 123 and fetch.call_count == 1
    with patch.object(
        state.urllib.request,
        "urlopen",
        side_effect=urllib.error.HTTPError("https://state.test", 401, "outside repository scope", {}, None),
    ):
        for _ in range(3):
            assert state.read("org/private", "issue_comments", number=7) == (False, None)
    with patch.object(state.urllib.request, "urlopen", return_value=Response(payload)) as fetch:
        assert state.read("org/repo", "issue_comments", number=7)[0] and fetch.call_count == 1
    with patch.object(
        state.urllib.request,
        "urlopen",
        side_effect=urllib.error.HTTPError("https://state.test", 304, "unchanged", {}, None),
    ) as fetch:
        assert state.read("org/repo", "issue_comments", number=7)[0]
        assert fetch.call_args.args[0].get_header("If-none-match") == '"1"'
    # A force context still contacts the server and accepts its newest, arbitrarily aged result.
    with (
        transport.read_context(fresh=True),
        patch.object(state.urllib.request, "urlopen", return_value=Response(payload)) as fetch,
    ):
        assert state.read("org/repo", "issue_comments", number=7)[0] and fetch.call_count == 1
    with patch.object(
        state.urllib.request, "urlopen", return_value=Response({**payload, "status": "unavailable", "complete": False})
    ):
        assert state.read("org/repo", "issue_comments", number=7) == (False, None)
    with patch.object(state.urllib.request, "urlopen", side_effect=OSError("offline")) as fetch:
        for _ in range(3):
            assert state.read("org/repo", "issue_comments", number=7) == (False, None)
        calls = fetch.call_count
        assert state.read("org/repo", "issue_comments", number=7) == (False, None) and fetch.call_count == calls
    assert "client-secret" not in (Path(tmp) / "requests.jsonl").read_text()
    # Healthy pagination can take longer than the initial outage deadline.
    clock = [0.0]

    def paginated(request, **kwargs):
        clock[0] += 2
        params = state.urllib.parse.parse_qs(state.urllib.parse.urlsplit(request.full_url).query)
        offset = int(params.get("cursor", ["page:0"])[0].split(":")[1])
        page = {**payload, "data": [{"id": str(offset + 1)}]}
        if offset < 2:
            page["cursor"] = f"page:{offset + 1}"
        return Response(page)

    # A different query avoids the earlier deliberate outage circuit.
    Path(tmp + "/token").write_text("pagination-client")
    with (
        patch.object(state.time, "monotonic", side_effect=lambda: clock[0]),
        patch.object(state.urllib.request, "urlopen", side_effect=paginated),
    ):
        available, rows = state.read("org/repo", "issue_comments", number=7)
        assert available and [r["id"] for r in rows] == [1, 2, 3] and clock[0] > 5
    for i, error in enumerate((http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("invalid"))):
        Path(tmp + "/token").write_text("broken-http-" + str(i))
        with patch.object(state.urllib.request, "urlopen", side_effect=error):
            assert state.read("org/repo", "issue_comments", number=7) == (False, None)
    Path(tmp + "/token").write_text("malformed-json-client")
    with patch.object(state.urllib.request, "urlopen", side_effect=lambda *_a, **_kw: Response([])):
        assert state.read("org/repo", "issue_comments", number=7) == (False, None)
print("PASS: shared-state freshness, fallback, conditional reads and configurable cutoff")
