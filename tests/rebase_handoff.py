#!/usr/bin/env python3
"""A trusted head-bound request reaches only the tending worker, with bounded retries."""

import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.github import GitHub

survey_mod = importlib.import_module("tauceti_worker.survey")
HEAD = "a" * 40


def request(head=HEAD, author="tauceti-review-bot[bot]"):
    return {"author": author, "body": f"Merge-queue recovery for head `{head[:7]}`.\n\n<!--tauceti-rebase:v1 {head}-->"}


def test_trusted_paginated_comments():
    gh = GitHub()
    with patch.object(gh, "issue_comments") as call:
        for rows, expected in [
            ([request(author="peer"), request()], True),
            ([request(author="peer")], False),
            ([request("b" * 40)], False),
            ([{**request(), "body": request()["body"].replace("tauceti-rebase", "tauceti-merge-stalled")}], False),
            ([{**request(), "body": "Quoted text\n" + request()["body"]}], False),
            ([{"author": "tauceti-review-bot[bot]", "body": None}], False),
            ([{"author": "tauceti-review-bot[bot]", "body": []}], False),
            ([None, [], request()], True),
        ]:
            call.return_value = [
                {**row, "user": {"login": row["author"]}} if isinstance(row, dict) else row for row in rows
            ]
            assert gh.rebase_requested(1, HEAD) is expected, rows
        call.return_value = None
        assert not gh.rebase_requested(1, HEAD)
        call.reset_mock()
        assert not gh.rebase_requested(1, "invalid")
        call.assert_not_called()


def pr(number, *, author="me", labels=("needs-rebase",), head=HEAD, conflicting=False):
    return {
        "number": number,
        "headRefOid": head,
        "headRefName": "feature",
        "headRepositoryOwner": {"login": author},
        "headRepository": {"name": "TauCeti"},
        "author": {"login": author},
        "statusCheckRollup": [],
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
        "labels": [{"name": label} for label in labels],
    }


def test_survey_ownership_head_pause_and_budget():
    raw = [
        pr(1),
        pr(2, author="peer"),
        pr(3, labels=()),
        pr(4, labels=("needs-rebase", "keep")),
        pr(5, head="b" * 40),
        pr(6),
        pr(7, labels=(), conflicting=True),
        pr(8, labels=("hold",), conflicting=True),
    ]
    gh = GitHub()
    counters = SimpleNamespace(read=lambda name: survey_mod.MAX_REBASE_ATTEMPTS if name == "rebase-pr-6" else 0)
    with (
        patch.object(survey_mod, "me", return_value="me"),
        patch.object(survey_mod, "can_push", side_effect=AssertionError("no canonical bot PRs")),
        patch.object(gh, "open_prs", return_value=raw),
        patch.object(
            gh, "issue_comments", return_value=[{**request(), "user": {"login": request()["author"]}}]
        ) as calls,
    ):
        sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
    assert [c.pr for c in sv.rebaseable.actionable] == [1, 7]
    assert [c.pr for c in sv.rebaseable.suppressed] == [6]
    assert all(c.budget == survey_mod.MAX_REBASE_ATTEMPTS for c in sv.rebaseable.actionable)
    assert calls.call_count == 3  # only our labelled, unpaused PRs need comment reads


def test_verified_progress_restores_budget_without_unbounded_retries():
    gh = GitHub()
    raw = pr(17, labels=())
    raw["statusCheckRollup"] = [{"context": "build", "state": "SUCCESS", "startedAt": "2026-10-12T00:00:00Z"}]
    with tempfile.TemporaryDirectory() as directory:
        counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))
        counters.write("rebase-pr-17", 3)
        with (
            patch.object(survey_mod, "me", return_value="me"),
            patch.object(gh, "open_prs", return_value=[raw]),
            patch.object(gh, "rebase_requested", return_value=True) as requests,
        ):
            cfg = SimpleNamespace(wid="test")
            # Observe the repaired head while healthy, before later main movement.
            survey_mod.survey(cfg, gh, None, counters, deep=False)
            epoch = counters.rebase_budget_key(17)
            assert epoch != "rebase-pr-17"
            raw["mergeable"] = "CONFLICTING"
            selected = survey_mod.survey(cfg, gh, None, counters, deep=False).rebaseable.actionable[0]
            assert selected.attempts == 0 and selected.rebase_budget_key == epoch
            for _ in range(3):
                counters.incr(epoch)
            assert survey_mod.survey(cfg, gh, None, counters, deep=False).rebaseable.suppressed[0].attempts == 3
            # A new commit alone and a hold both leave that cap in place.
            raw["headRefOid"] = "b" * 40
            raw["statusCheckRollup"][0]["state"] = "FAILURE"
            assert survey_mod.survey(cfg, gh, None, counters, deep=False).rebaseable.suppressed[0].attempts == 3
            raw["mergeable"] = "MERGEABLE"
            raw["statusCheckRollup"][0]["state"] = "SUCCESS"
            raw["labels"] = [{"name": "hold"}]
            survey_mod.survey(cfg, gh, None, counters, deep=False)
            assert counters.rebase_budget_key(17) == epoch
            # A live reconciliation request also blocks renewal on green CI.
            raw["labels"] = [{"name": "needs-rebase"}]
            assert survey_mod.survey(cfg, gh, None, counters, deep=False).rebaseable.suppressed[0].attempts == 3
            raw["labels"] = []
            survey_mod.survey(cfg, gh, None, counters, deep=False)
            raw["mergeable"] = "CONFLICTING"
            selected = survey_mod.survey(cfg, gh, None, counters, deep=False).rebaseable.actionable[0]
            assert selected.attempts == 0 and selected.rebase_budget_key != epoch
            assert requests.call_count == 1  # no extra GitHub reads for checkpoints


def test_green_push_handoff_cycle_keeps_its_per_pr_bound():
    gh = GitHub()
    raw = pr(18)
    raw["statusCheckRollup"] = [{"context": "build", "state": "SUCCESS", "startedAt": "2026-10-12T00:00:00Z"}]
    with tempfile.TemporaryDirectory() as directory:
        counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))
        with (
            patch.object(survey_mod, "me", return_value="me"),
            patch.object(gh, "open_prs", return_value=[raw]),
            patch.object(gh, "rebase_requested", return_value=True),
        ):
            cfg = SimpleNamespace(wid="test")
            for i, character in enumerate("abcd"):
                raw["headRefOid"] = character * 40
                raw["labels"] = []
                survey_mod.survey(cfg, gh, None, counters, deep=False)
                raw["labels"] = [{"name": "needs-rebase"}]
                sv = survey_mod.survey(cfg, gh, None, counters, deep=False)
                if i < 3:
                    selected = sv.rebaseable.actionable[0]
                    assert selected.rebase_budget_key == "rebase-pr-18" and selected.attempts == i
                    counters.incr(selected.rebase_budget_key)
                else:
                    assert not sv.rebaseable.actionable
                    assert sv.rebaseable.suppressed[0].attempts == 3
            # A comment read failure does not permit checkpoint renewal while
            # the label remains, even on another successful head.
            before = counters.rebase_budget_key(18)
            raw["headRefOid"] = "e" * 40
            with patch.object(gh, "rebase_requested", return_value=False):
                sv = survey_mod.survey(cfg, gh, None, counters, deep=False)
            assert not sv.rebaseable.actionable and counters.rebase_budget_key(18) == before


def test_broken_retry_evidence_suppresses_rebase():
    gh = GitHub()
    raw = pr(19, labels=(), conflicting=True)
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory)
        counters = survey_mod.Counters(SimpleNamespace(state=state))
        (state / "rebase-progress-19.json").write_text("{")
        with patch.object(survey_mod, "me", return_value="me"), patch.object(gh, "open_prs", return_value=[raw]):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
        assert not sv.rebaseable.actionable
        assert "checkpoint is unreadable" in sv.rebaseable.suppressed[0].reason
        (state / "rebase-progress-19.json").unlink()
        (state / "rebase-pr-19").write_text("")
        with patch.object(survey_mod, "me", return_value="me"), patch.object(gh, "open_prs", return_value=[raw]):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False)
        assert not sv.rebaseable.actionable
        assert "counter is malformed" in sv.rebaseable.suppressed[0].reason


if __name__ == "__main__":
    test_trusted_paginated_comments()
    test_survey_ownership_head_pause_and_budget()
    test_verified_progress_restores_budget_without_unbounded_retries()
    test_green_push_handoff_cycle_keeps_its_per_pr_bound()
    test_broken_retry_evidence_suppresses_rebase()
    print("PASS: trusted fork handoff, ownership, stale heads, pause labels and retry cap")
