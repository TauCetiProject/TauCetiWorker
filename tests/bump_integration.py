#!/usr/bin/env python3
"""Only stopped exact-head singleton failures justify an integration repair editor."""

import copy
import importlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.constants import TAUCETI
from tauceti_worker.github import GitHub
from tauceti_worker.survey import Candidate, PRInfo
from tauceti_worker.work_units import _still_actionable

survey_mod = importlib.import_module("tauceti_worker.survey")
HEAD = "a" * 40


def test_exact_handoff():
    gh = GitHub()
    live = {
        "state": "open",
        "draft": False,
        "head": {"sha": HEAD, "repo": {"full_name": TAUCETI}},
        "base": {"ref": "main"},
        "user": {"login": "tauceti-review-bot[bot]"},
        "labels": [],
    }
    observation = {
        "schema": "tauceti-bors.observation/v1",
        "repo": TAUCETI,
        "base": "main",
        "bump_repairs": [{"pr": 1, "head_sha": HEAD, "batch_id": 417}],
    }
    for changes, expected in [
        ({}, 417),
        ({"draft": True}, None),
        ({"state": "closed"}, None),
        ({"head": {"sha": "b" * 40}}, None),
        ({"labels": [{"name": "hold"}]}, None),
    ]:
        with (
            patch.object(gh, "_gh", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(live | changes))),
            patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(observation).encode())),
        ):
            assert gh.bump_repair_requested(1, HEAD) == expected
    for changes in [
        {"bump_repairs": []},
        {"repo": "other/repo"},
        {"schema": "wrong"},
        {"bump_repairs": [{"pr": 1, "head_sha": "b" * 40, "batch_id": 417}]},
    ]:
        with (
            patch.object(gh, "_gh", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(live))),
            patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(observation | changes).encode())),
        ):
            assert gh.bump_repair_requested(1, HEAD) is None
    with (
        patch.object(gh, "_gh", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(live))),
        patch("urllib.request.urlopen", side_effect=OSError("offline")),
    ):
        assert gh.bump_repair_requested(1, HEAD) is None


def raw_pr(number, *, branch="bump-mathlib/test", build="SUCCESS", integration="FAILURE", labels=()):
    return {
        "number": number,
        "headRefOid": HEAD,
        "headRefName": branch,
        "headRepositoryOwner": {"login": "TauCetiProject"},
        "headRepository": {"name": "TauCeti"},
        "author": {"login": "tauceti-review-bot[bot]", "is_bot": True},
        "mergeable": "MERGEABLE",
        "labels": [{"name": label} for label in labels],
        "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "build", "state": build},
            {"__typename": "StatusContext", "context": "bump-integration", "state": integration},
        ],
    }


def test_selection_and_cheap_revalidation():
    raw = [
        raw_pr(1),
        raw_pr(2, integration="PENDING"),
        raw_pr(3, branch="ordinary"),
        raw_pr(4, labels=("needs-rebase",)),
        raw_pr(5, branch="hopscotch/lkg-bump"),
        raw_pr(6, build="FAILURE", integration="PENDING"),
        raw_pr(7, labels=("hold",)),
    ]
    gh = GitHub()
    counters = SimpleNamespace(read=lambda key: 0)
    with (
        patch.object(survey_mod, "me", return_value="me"),
        patch.object(survey_mod, "can_push", return_value=True),
        patch.object(gh, "open_prs", return_value=raw),
        patch.object(gh, "bump_repair_requested", side_effect=AssertionError("no extra survey requests")),
    ):
        sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
    assert [c.pr for c in sv.bump.actionable] == [1, 5, 6]
    assert [c.bump_integration_failure for c in sv.bump.actionable] == [True, True, False]
    fake_check = copy.deepcopy(raw[0])
    fake_check["statusCheckRollup"][1] = {"__typename": "CheckRun", "name": "bump-integration", "conclusion": "FAILURE"}
    assert not PRInfo.from_json(fake_check).bump_integration_failed
    c = Candidate(1, HEAD, "", bump_integration_failure=True)
    for answer in [417, None]:
        with patch.object(gh, "bump_repair_requested", return_value=answer) as check:
            assert _still_actionable("bump", SimpleNamespace(gh=gh), sv, c) is bool(answer)
            check.assert_called_once_with(1, HEAD)


if __name__ == "__main__":
    test_exact_handoff()
    test_selection_and_cheap_revalidation()
    print("bump integration: PASS")
