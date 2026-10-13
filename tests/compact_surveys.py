"""Selection parity, roadmap-first filtering, outage fallback and compact coverage."""

import dataclasses
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.github_state import SurveyRows, read_survey
from tauceti_worker.review_state import ReviewState
from tauceti_worker.survey_facts import CONTRACT, summarize

mod = importlib.import_module("tauceti_worker.survey")
head = "a" * 40


def pr(number, area="PDE"):
    return {
        "number": number,
        "author": {"login": "author"},
        "headRefOid": head,
        "headRefName": "roadmap/work",
        "headRepositoryOwner": {"login": "author"},
        "labels": [{"name": "roadmap/" + area}],
        "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
    }


with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(wid="test", sbcache=Path(tmp) / "sb", store_dir=Path(tmp) / "review", state=Path(tmp))
    raw = [pr(n) for n in range(1, 5)] + [pr(n, "Topology") for n in range(5, 5005)]
    issues = {
        n: [
            {
                "id": str(n),
                "body": "<!--tauceti-scoreboard--> <!--tauceti-meta:v1 "
                + json.dumps(
                    {
                        "head_sha": head,
                        "runs": [{"provider": "claude", "verdict": "request_changes" if n == 3 else "approve"}],
                        "states": {"reuse": "blocking_request" if n == 3 else "green"},
                    }
                )
                + "-->",
                "updated_at": "now",
            }
        ]
        for n in range(1, 5)
    }
    issues[2] = []
    issues[4].append(
        {
            "id": "99",
            "body": "<!--tauceti-review-in-progress "
            + json.dumps({"head": head, "expires_at": 9999999999, "providers": ["claude"]})
            + "-->",
        }
    )
    calls = []

    def issue(n):
        assert n < 5, "unrelated roadmap history fetched"
        calls.append(("issue", n))
        return issues[n]

    def review(n):
        assert n < 5, "unrelated roadmap history fetched"
        calls.append(("review", n))
        return []

    gh = SimpleNamespace(
        repo="org/repo",
        open_prs=lambda: raw,
        issue_comments=issue,
        review_comments=review,
        rebase_requested=lambda *_: False,
    )
    counters = SimpleNamespace(read=lambda _: 0)
    with (
        patch.dict(os.environ, {"TAUCETI_ROADMAP_ONLY": "PDE", "TAUCETI_GITHUB_STATE_URL": "https://state.test"}),
        patch.object(mod, "me", return_value="author"),
        patch.object(mod, "can_push", return_value=False),
    ):
        with patch("tauceti_worker.github_state.read_survey", return_value=(False, None)):
            direct = mod.survey(cfg, gh, ReviewState(cfg, gh), counters, stages=["review", "fix", "rebase"])
        assert len(direct.open_prs) == 4 and calls
        compact = [
            {
                "contract": CONTRACT,
                "pr": p,
                "review": {**summarize(issues[p["number"]], []), "issue_complete": True, "review_complete": True},
            }
            for p in raw[:4]
        ]
        calls.clear()
        rs = ReviewState(cfg, gh)
        with patch("tauceti_worker.github_state.read_survey", return_value=(True, compact)) as gateway:
            optimized = mod.survey(cfg, gh, rs, counters, stages=["review", "fix", "rebase"])
        assert dataclasses.asdict(optimized) == dataclasses.asdict(direct), "optimized path changed task selection"
        assert not calls and gateway.call_count == 1, "compact survey must eliminate per-PR history requests"
        with patch(
            "tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows(compact, {"author": 5004}))
        ):
            automatic = mod.survey(cfg, gh, rs, counters, stages=[], selected_roadmap="PDE", count_all_roadmaps=True)
        assert len(automatic.open_prs) == 4 and automatic.n_mine_open == 5004, (
            "early scope must preserve the total automatic authoring cap"
        )
        with (
            patch("tauceti_worker.github_state.read_survey", return_value=(False, None)),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": "Topology"}),
        ):
            targeted = mod.survey(cfg, gh, rs, counters, stages=[], numbers=(5,))
        assert [p.number for p in targeted.open_prs] == [5], "explicit targets override roadmap skips"
        rs.seed_survey(compact)
        with patch("tauceti_worker.review_state.time.time", return_value=9999999999):
            assert not rs.inflight_review(4, head), "expiry is checked on the worker clock, not frozen at projection"
        assert not rs.inflight_review(4, "b" * 40), "claims must match the exact head"
        rs.gh_meta(3, force=True)
        assert calls == [("issue", 3)] and 3 not in rs._survey_facts, (
            "selected-target revalidation cannot reuse an older snapshot"
        )
        calls.clear()
        with patch("tauceti_worker.github_state.read_survey", return_value=(False, None)):
            mod.survey(cfg, gh, ReviewState(cfg, gh), counters, stages=[])
        assert not calls, "authoring-only work must not inspect review histories"
        compact[0]["review"]["review_complete"] = False
        rs.seed_survey(compact)
        calls.clear()
        rs.newest_contest_reply(1)
        assert calls == [("review", 1)], "incomplete coverage is not a confirmed absence"

with patch("tauceti_worker.github_state.read", return_value=(True, {"contract": "future", "data": []})):
    assert read_survey("org/repo") == (False, None), "incompatible empty snapshots must fall back too"
for bad in ({"contest": "invalid"}, {"markers": [None]}, {"pr": {"number": 1, "labels": [None]}}):
    row = {
        "contract": CONTRACT,
        "pr": pr(1),
        "review": {**summarize([], []), "issue_complete": True, "review_complete": True},
    }
    if "pr" in bad:
        row["pr"] = bad["pr"]
    else:
        row["review"].update(bad)
    with patch("tauceti_worker.github_state.read", return_value=(True, {"contract": CONTRACT, "data": [row]})):
        assert read_survey("org/repo") == (False, None), "malformed snapshots must fall back"


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = {}


with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "TAUCETI_GITHUB_SHARED_DIR": tmp,
            "TAUCETI_GITHUB_STATE_URL": "https://state.test",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": tmp + "/token",
        },
    ),
):
    Path(tmp + "/token").write_text("client")
    for error in (
        HTTPError("https://state.test", 400, "old service", {}, None),
        HTTPError("https://state.test", 403, "auth", {}, None),
        TimeoutError("offline"),
    ):
        with patch("tauceti_worker.github_state.urllib.request.urlopen", side_effect=error):
            assert read_survey("org/repo") == (False, None)
    payload = {
        "version": 1,
        "repository": "org/repo",
        "operation": "survey",
        "status": "ok",
        "complete": True,
        "revision": 1,
        "contract": CONTRACT,
        "data": [],
    }
    with patch("tauceti_worker.github_state.urllib.request.urlopen", return_value=Response(payload)):
        assert read_survey("org/repo") == (True, []), "a confirmed empty roadmap is distinct from an outage"
print(
    "PASS: 5,000 unrelated histories skipped; compact/direct selectors agree; outage and partial coverage retain fallback"
)
