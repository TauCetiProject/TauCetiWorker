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
            ([request(author="tau-ceti-bors[bot]")], True),
            ([request(author="tau-ceti-bors")], False),
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
            assert bool(gh.rebase_request_body(1, HEAD)) is expected, rows
        call.return_value = [
            {**request(), "user": {"login": request()["author"]}, "body": request()["body"] + "\n" + "x" * 32768}
        ]
        assert gh.rebase_requested(1, HEAD)
        assert not gh.rebase_request_body(1, HEAD)
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


def test_launch_rechecks_head_hold_and_active_marker():
    from tauceti_worker.work_units import _still_actionable

    gh = GitHub()
    live = {
        "headRefOid": HEAD,
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "labels": [{"name": "needs-rebase"}],
    }
    candidate = SimpleNamespace(pr=1, head=HEAD)
    with (
        patch.object(gh, "pr_view", return_value=live),
        patch.object(gh, "rebase_requested", return_value=True) as marker,
    ):
        assert _still_actionable("rebase", SimpleNamespace(gh=gh), None, candidate)
        live["labels"] = [{"name": "hold"}]
        assert not _still_actionable("rebase", SimpleNamespace(gh=gh), None, candidate)
        live["labels"] = [{"name": "needs-rebase"}]
        live["headRefOid"] = "b" * 40
        assert not _still_actionable("rebase", SimpleNamespace(gh=gh), None, candidate)
        live["headRefOid"] = HEAD
        marker.return_value = False
        assert not _still_actionable("rebase", SimpleNamespace(gh=gh), None, candidate)
        live["mergeable"] = "CONFLICTING"
        assert _still_actionable("rebase", SimpleNamespace(gh=gh), None, candidate)


def test_semantic_evidence_reaches_claimed_repair(provisional=False):
    from tauceti_worker import work_units

    gh = GitHub()
    body = request(author="tau-ceti-bors[bot]")["body"] + (
        "\nTested main: `" + "b" * 40 + "`.\n`TauCeti/Consumer.lean` imports missing `TauCeti.Removed`."
    )
    if provisional:
        body = request(author="tau-ceti-bors[bot]")["body"] + (
            "\nChecker policy: `provisional-diagnostic-v1`. PROVISIONAL SUSPECT, not a proven individual failure."
            "\nCandidate 42, failed commit " + "c" * 40 + ", run attempt 2. Evidence main `" + "b" * 40 + "`."
            "\nDiagnostic path: TauCeti/Consumer.lean. An interaction may be responsible."
        )
    raw = pr(20)
    raw["statusCheckRollup"] = [{"context": "build", "state": "SUCCESS"}]
    w = SimpleNamespace(
        gh=gh,
        claims=SimpleNamespace(begin_branch_work=lambda *args: True),
        rs=SimpleNamespace(bust=lambda *args: None),
    )
    with (
        patch.object(survey_mod, "me", return_value="me"),
        patch.object(gh, "open_prs", return_value=[raw]),
        patch.object(gh, "issue_comments", return_value=[{"user": {"login": "tau-ceti-bors[bot]"}, "body": body}]),
    ):
        sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, SimpleNamespace(read=lambda name: 0), deep=False)
        candidate = sv.rebaseable.actionable[0]
        assert candidate.pr == 20
        with (
            patch.object(work_units, "run_in_bubble", return_value=0) as agent,
            patch.object(work_units, "me", return_value="me"),
        ):
            assert (
                work_units._do_fixlike(
                    w, sv, candidate, SimpleNamespace(agent_name="Codex"), True, prompt_file="rebase.md", label="rebase"
                )
                == 0
            )
        prompt = agent.call_args.args[2]
        assert body in prompt and HEAD in prompt
        assert "Nobody reads your final transcript" in prompt
        assert "You may close this PR" in prompt
        assert "gh pr close 20 --repo TauCetiProject/TauCeti" in prompt
        assert "--add-label on-hold" in prompt
        if provisional:
            assert "attribution **unconfirmed**" in prompt
            assert "still complete all verification below" in prompt
            assert "Never integrate another PR's branch" in prompt
        assert "semantic repair may still be needed" in prompt
        assert '"__BIN__' not in prompt
        assert agent.call_args.kwargs["allow_push"] == "me/TauCeti"
        with (
            patch.object(work_units, "run_in_bubble", return_value=0) as other,
            patch.object(work_units, "me", return_value="different-account"),
        ):
            work_units._do_fixlike(
                w,
                sv,
                candidate,
                SimpleNamespace(agent_name="Codex"),
                True,
                prompt_file="rebase.md",
                label="rebase",
            )
        assert "You may NOT close this PR" in other.call_args.args[2]
    # A lost branch claim starts no authoring task.
    w.claims.begin_branch_work = lambda *args: False
    with patch.object(work_units, "run_in_bubble") as agent:
        assert (
            work_units._do_fixlike(
                w, sv, candidate, SimpleNamespace(agent_name="Codex"), True, prompt_file="rebase.md", label="rebase"
            )
            is None
        )
        agent.assert_not_called()


if __name__ == "__main__":
    test_launch_rechecks_head_hold_and_active_marker()
    test_semantic_evidence_reaches_claimed_repair()
    test_semantic_evidence_reaches_claimed_repair(provisional=True)
    test_trusted_paginated_comments()
    test_survey_ownership_head_pause_and_budget()
    test_verified_progress_restores_budget_without_unbounded_retries()
    test_green_push_handoff_cycle_keeps_its_per_pr_bound()
    test_broken_retry_evidence_suppresses_rebase()
    print("PASS: trusted fork handoff, ownership, stale heads, pause labels and retry cap")
