"""One explicitly scoped foreign repair; no live reads, pushes, or paid agents."""

import copy
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import repair_route as route
from tauceti_worker import work_units
from tauceti_worker.constants import TAUCETI
from tauceti_worker.github import GitHub, GitHubError

survey_mod = importlib.import_module("tauceti_worker.survey")
round_mod = importlib.import_module("tauceti_worker.round")
HEAD, BASE = "a" * 40, "b" * 40
SCOPE = f"13940:{HEAD}:{BASE}:{BASE}"


def fixture():
    live = {
        "state": "open",
        "draft": False,
        "maintainer_can_modify": True,
        "user": {"login": route.AUTHOR, "type": "User"},
        "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": route.FORK, "fork": True}},
        "base": {"ref": "main", "repo": {"full_name": TAUCETI}},
        "labels": [{"name": "needs-rebase"}],
    }
    comment = {
        "user": {"login": "tau-ceti-bors[bot]"},
        "body": (
            f"Merge-queue recovery for head `{HEAD[:7]}`: exact individual source compatibility failed.\n\n"
            f"Tested main: `{BASE}`. Checker policy: `local-source-v3`.\n"
            "Missing source: TauCeti/Consumer.lean imports TauCeti.Removed.\n\n"
            f"<!--tauceti-rebase:v1 {HEAD}-->"
        ),
    }
    gh = GitHub()
    gh.issue_comments = Mock(return_value=[comment])

    def api(path, jq):
        if path.endswith("/pulls/13940"):
            return json.dumps(live)
        if path.endswith("/git/ref/heads/main"):
            return BASE
        if path == "repos/" + TAUCETI:
            return "true"
        raise AssertionError((path, jq))

    gh.api_jq = Mock(side_effect=api)
    return live, comment, gh


def raw_pr():
    return {
        "number": 13940,
        "headRefOid": HEAD,
        "headRefName": "feature",
        "headRepositoryOwner": {"login": route.AUTHOR},
        "headRepository": {"name": "TauCeti"},
        "author": {"login": route.AUTHOR},
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
        "labels": [{"name": "needs-rebase"}],
    }


def test_default_off_and_exact_operator_scope():
    _, _, gh = fixture()
    for value in [
        "",
        "13940",
        f"13940:{HEAD}:{BASE}",
        SCOPE + "," + SCOPE,
        SCOPE.upper(),
        SCOPE.replace("13940:", "0:"),
    ]:
        with patch.dict(os.environ, {route.ENV: value}):
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)
    gh.api_jq.assert_not_called()
    with patch.dict(os.environ, {route.ENV: SCOPE}):
        assert not route.authorize(gh, 13940, HEAD, "feature", "other/TauCeti")
        assert not route.authorize(gh, 13941, HEAD, "feature", route.FORK)
        assert not route.authorize(gh, 13940, "c" * 40, "feature", route.FORK)
        assert route.authorize(gh, 13940, HEAD, "feature", route.FORK) == BASE


def test_live_permission_pause_identity_and_movement_guards():
    original, _, _ = fixture()
    changes = [
        {"state": "closed"},
        {"draft": True},
        {"maintainer_can_modify": False},
        {"maintainer_can_modify": "true"},
        {"user": {"login": "another-author", "type": "User"}},
        {"user": {"login": route.AUTHOR, "type": "Bot"}},
        {"head": {**original["head"], "sha": "c" * 40}},
        {"head": {**original["head"], "ref": "renamed"}},
        {"head": {**original["head"], "repo": {"full_name": "other/TauCeti", "fork": True}}},
        {"head": {**original["head"], "repo": None}},
        {"head": {**original["head"], "repo": {"full_name": route.FORK, "fork": False}}},
        {"base": {"ref": "other", "repo": {"full_name": TAUCETI}}},
        {"base": {"ref": "main", "repo": {"full_name": "other/TauCeti"}}},
        {"labels": []},
        *({"labels": [{"name": "needs-rebase"}, {"name": name.upper()}]} for name in route.PAUSE),
    ]
    with patch.dict(os.environ, {route.ENV: SCOPE}):
        for change in changes:
            live, _, gh = fixture()
            live.update(copy.deepcopy(change))
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK), change
        for path, result in [
            (f"repos/{TAUCETI}", "false"),
            (f"repos/{TAUCETI}", None),
            (f"repos/{TAUCETI}/git/ref/heads/main", "c" * 40),
            (f"repos/{TAUCETI}/pulls/13940", "not-json"),
        ]:
            _, _, gh = fixture()
            read = gh.api_jq.side_effect
            gh.api_jq.side_effect = lambda p, q, path=path, result=result, read=read: (
                result if p == path else read(p, q)
            )
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)
        _, _, gh = fixture()
        gh.api_jq.side_effect = GitHubError("API unavailable")
        assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)


def test_only_authenticated_current_v3_source_request():
    with patch.dict(os.environ, {route.ENV: SCOPE}):
        for change in ["actor", "policy", "base", "head", "quoted", "missing", "oversized"]:
            _, comment, gh = fixture()
            if change == "actor":
                comment["user"]["login"] = "tauceti-review-bot[bot]"
            elif change == "quoted":
                comment["body"] = "Quoted text\n" + comment["body"]
            elif change == "missing":
                gh.issue_comments.return_value = None
            elif change == "oversized":
                comment["body"] += "x" * 32768
            else:
                old = {"policy": "local-source-v3", "base": BASE, "head": HEAD}[change]
                comment["body"] = comment["body"].replace(old, "old")
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK), change


def test_frozen_genuine_request_fixture_without_live_execution():
    observed = json.loads((Path(__file__).parent / "fixtures" / "scoped-repair-route.json").read_text())
    live, _, gh = fixture()
    live["head"]["sha"] = observed["head"]
    gh.issue_comments.return_value = [observed["request"]]
    read = gh.api_jq.side_effect
    gh.api_jq.side_effect = lambda p, q: observed["base"] if p.endswith("/git/ref/heads/main") else read(p, q)
    value = f"{observed['pr']}:{observed['head']}:{observed['base']}:{observed['base']}"
    with patch.dict(os.environ, {route.ENV: value}):
        assert route.authorize(gh, observed["pr"], observed["head"], "feature", route.FORK) == observed["base"]
    assert not observed["production_worker_repair_verified"]


def test_original_request_survives_main_advance_without_reissuing_or_retargeting():
    integration = "c" * 40
    _, comment, gh = fixture()
    original_read = gh.api_jq.side_effect
    relation = "ahead"
    live_main = integration

    def read(path, jq):
        if path.endswith("/git/ref/heads/main"):
            return live_main
        if path == f"repos/{TAUCETI}/compare/{BASE}...{integration}":
            return relation
        return original_read(path, jq)

    gh.api_jq.side_effect = read
    original_comment = copy.deepcopy(comment)
    scope = f"13940:{HEAD}:{BASE}:{integration}"
    with patch.dict(os.environ, {route.ENV: scope}):
        # The exact same original Bors request authorizes source investigation
        # only against the separately named integration main; no comment write.
        assert route.authorize(gh, 13940, HEAD, "feature", route.FORK) == integration
        assert comment == original_comment
        for status in [None, "behind", "diverged", "unknown"]:
            relation = status
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)
        relation = "ahead"
        live_main = "d" * 40
        assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)
        live_main = integration
        assert route.authorize(gh, 13940, HEAD, "feature", route.FORK) == integration
        comment["body"] = comment["body"].replace(BASE, integration)
        assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)
        comment["body"] = original_comment["body"].replace("Merge-queue recovery", "Resolved recovery")
        assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK)


def test_selection_is_rebase_only_and_one_attempt_without_budget_reset():
    _, _, gh = fixture()
    gh.open_prs = Mock(return_value=[raw_pr()])
    counters = SimpleNamespace(read=Mock(return_value=0))
    with patch.object(survey_mod, "me", return_value="maintainer"):
        with patch.dict(os.environ, {route.ENV: ""}):
            assert not survey_mod.survey(
                SimpleNamespace(wid="test"), gh, None, counters, deep=False
            ).rebaseable.actionable
        with patch.dict(os.environ, {route.ENV: SCOPE}):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False, stages=["rebase"])
            c = sv.rebaseable.actionable[0]
            assert (c.pr, c.head, c.foreign_repair_base, c.budget, c.rebase_budget_key) == (
                13940,
                HEAD,
                BASE,
                1,
                "rebase-pr-13940",
            )
            counters.read.return_value = 1
            capped = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False, stages=["rebase"])
            assert not capped.rebaseable.actionable and capped.rebaseable.suppressed[0].attempts == 1
            gh.api_jq.reset_mock()
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False, stages=["fix-ci"])
            assert not sv.rebaseable.actionable
            gh.api_jq.assert_not_called()


def test_paid_launch_rechecks_scope_and_requires_a_real_claim():
    _, comment, gh = fixture()
    p = survey_mod.PRInfo.from_json(raw_pr())
    sv = SimpleNamespace(open_prs=[p])
    c = survey_mod.Candidate(13940, HEAD, foreign_repair_base=BASE, rebase_budget_key="rebase-pr-13940")
    count = {c.rebase_budget_key: 0}
    counters = SimpleNamespace(
        read=lambda k: count[k],
        write=lambda k, v: count.update({k: v}),
        incr=lambda k: count.update({k: count[k] + 1}),
        foreign_repair_launched=lambda pr: False,
        record_foreign_repair_launch=Mock(),
    )
    w = SimpleNamespace(
        gh=gh,
        counters=counters,
        rs=SimpleNamespace(bust=Mock()),
        claims=SimpleNamespace(
            begin_branch_work=Mock(return_value=True),
            held=("branch/13940", "claims"),
            stop_heartbeat=Mock(),
            release=Mock(),
        ),
    )
    with patch.dict(os.environ, {route.ENV: SCOPE}), patch.object(work_units, "run_in_bubble", return_value=0) as agent:
        assert work_units._still_actionable("rebase", w, sv, c)
        # A pause added after selection prevents both claim and paid launch, even
        # though a textual conflict would normally select an owned PR.
        live = json.loads(gh.api_jq(f"repos/{TAUCETI}/pulls/13940", "."))
        original_read = gh.api_jq.side_effect
        live["labels"].append({"name": "human"})
        gh.api_jq.side_effect = lambda p, q: json.dumps(live) if "/pulls/" in p else original_read(p, q)
        assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) is None
        assert count[c.rebase_budget_key] == 0
        w.claims.begin_branch_work.assert_not_called()
        agent.assert_not_called()
        gh.api_jq.side_effect = original_read
        for acquired, held in [(False, None), (True, None), (True, ("branch/other", "claims"))]:
            w.claims.begin_branch_work.return_value = acquired
            w.claims.held = held
            assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) is None
            assert count[c.rebase_budget_key] == 0
            agent.assert_not_called()
            w.claims.stop_heartbeat.assert_not_called()
            w.claims.release.assert_not_called()
        w.claims.begin_branch_work.return_value = True
        w.claims.held = ("branch/13940", "claims")
        assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) == 0
        assert count[c.rebase_budget_key] == 1
        assert agent.call_args.kwargs["allow_push"] == route.FORK
        prompt = agent.call_args.args[2]
        assert comment["body"] in prompt and HEAD in prompt and BASE in prompt
        assert "You may NOT close this PR" in prompt
        assert "stop without editing if origin/main differs" in prompt
        agent.reset_mock()
        # A provider failure after launch also consumes the one canary attempt.
        count[c.rebase_budget_key] = 0
        agent.return_value = 1
        with patch.object(work_units, "_refund_infra_failure") as refund:
            assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) == 1
            assert count[c.rebase_budget_key] == 1
            refund.assert_not_called()
        agent.reset_mock()
        with patch.dict(os.environ, {route.ENV: ""}):
            assert not work_units._still_actionable("rebase", w, sv, c)
            assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) is None
        agent.assert_not_called()


def test_current_at_launch_freezes_after_claim_and_survives_restart():
    with tempfile.TemporaryDirectory() as directory:
        live, comment, gh = fixture()
        gh.open_prs = Mock(return_value=[raw_pr()])
        original = gh.api_jq.side_effect
        current = BASE

        def read(path, jq):
            if path.endswith("/git/ref/heads/main"):
                return current
            if "/compare/" in path:
                return "ahead"
            return original(path, jq)

        gh.api_jq.side_effect = read
        counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))
        scope = f"13940:{HEAD}:{BASE}:{route.CURRENT}"
        with patch.dict(os.environ, {route.ENV: scope}), patch.object(survey_mod, "me", return_value="maintainer"):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, counters, deep=False, stages=["rebase"])
            c = sv.rebaseable.actionable[0]
            assert c.foreign_repair_base == BASE
            current = "c" * 40
            w = SimpleNamespace(gh=gh, counters=counters, rs=SimpleNamespace(bust=Mock()))
            assert work_units._still_actionable("rebase", w, sv, c)

            def claim(*args):
                nonlocal current
                current = "d" * 40
                return True

            w.claims = SimpleNamespace(begin_branch_work=claim, held=("branch/13940", "lease"))

            def agent(*args, **kwargs):
                nonlocal current
                receipt = json.loads((Path(directory) / "rebase-foreign-launch-13940.json").read_text())
                assert receipt == {"pr": 13940, "head": HEAD, "evidence_main": BASE, "integration_main": "d" * 40}
                assert counters.read("rebase-pr-13940") == 1
                assert "current-at-launch snapshot, now frozen" in args[2]
                assert "Fetch exactly `" + "d" * 40 + "`" in args[2]
                assert comment["body"] in args[2]
                current = "e" * 40  # later promotion never retargets this prompt
                return 1

            with patch.object(work_units, "run_in_bubble", side_effect=agent) as launch:
                assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) == 1
                assert launch.call_count == 1
                # Even an unstarted-preparation refund cannot erase the durable launch fence.
                counters.write("rebase-pr-13940", 0)
                w.counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))
                assert work_units.do_rebase(w, sv, c, SimpleNamespace(agent_name="Codex"), True) is None
                assert launch.call_count == 1
                sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, w.counters, deep=False, stages=["rebase"])
                assert not sv.rebaseable.actionable and sv.rebaseable.suppressed[0].attempts == 1


def test_current_at_launch_keeps_all_authentication_and_post_claim_pause_guards():
    scope = f"13940:{HEAD}:{BASE}:{route.CURRENT}"
    with patch.dict(os.environ, {route.ENV: scope}):
        for failure in ["ancestry", "permission", "request", "head", "unknown_main"]:
            live, comment, gh = fixture()
            original = gh.api_jq.side_effect
            main = "c" * 40
            if failure == "head":
                live["head"]["sha"] = "f" * 40
            if failure == "request":
                gh.issue_comments.return_value = []

            def read(path, jq, failure=failure, main=main, original=original):
                if path.endswith("/git/ref/heads/main"):
                    return "unknown" if failure == "unknown_main" else main
                if "/compare/" in path:
                    return "diverged" if failure == "ancestry" else "ahead"
                if path == "repos/" + TAUCETI and failure == "permission":
                    return "false"
                return original(path, jq)

            gh.api_jq.side_effect = read
            assert not route.authorize(gh, 13940, HEAD, "feature", route.FORK), failure
        with tempfile.TemporaryDirectory() as directory:
            live, _, gh = fixture()
            p = survey_mod.PRInfo.from_json(raw_pr())
            c = survey_mod.Candidate(13940, HEAD, foreign_repair_base=BASE, rebase_budget_key="rebase-pr-13940")
            counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))

            def claim(*args):
                live["labels"].append({"name": "keep"})
                return True

            w = SimpleNamespace(
                gh=gh,
                counters=counters,
                claims=SimpleNamespace(
                    begin_branch_work=claim, held=("branch/13940", "lease"), stop_heartbeat=Mock(), release=Mock()
                ),
            )
            with patch.object(work_units, "run_in_bubble") as launch:
                assert (
                    work_units.do_rebase(w, SimpleNamespace(open_prs=[p]), c, SimpleNamespace(agent_name="Codex"), True)
                    is None
                )
                launch.assert_not_called()
                assert counters.read(c.rebase_budget_key) == 0
                assert not counters.foreign_repair_launched(13940)
                w.claims.stop_heartbeat.assert_called_once()
                w.claims.release.assert_called_once()


def test_declined_foreign_claim_lifecycle_before_next_candidate():
    # Real claim/heartbeat lifecycle, stubbing only external claim commands and
    # child creation. Both post-claim refusals must clean up before dispatch can
    # acquire the next candidate's lease, and stale cleanup cannot release it.
    for refusal in ["pause_after_claim", "scope_withdrawn_during_prompt", "claim_replaced_during_prompt"]:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {route.ENV: SCOPE}):
            live, _, gh = fixture()
            counters = survey_mod.Counters(SimpleNamespace(state=Path(directory)))
            cleanups, events, children = [], [], []
            claims = round_mod.Claims(SimpleNamespace(), SimpleNamespace(add_cleanup=cleanups.append))

            def command(cmd, *, claims=claims, refusal=refusal, live=live, events=events, **kwargs):
                operation, key = cmd[1:3]
                assert kwargs["env"]["CLAIM_REPO"] == "test/claims"
                if operation == "acquire":
                    # A previous candidate must no longer own a lease/heartbeat.
                    assert claims.held is None and claims._hb is None and claims._hb_wfd is None
                    if key == "branch/13940" and refusal == "pause_after_claim":
                        live["labels"].append({"name": "keep"})
                else:
                    assert operation == "release" and claims._hb is None and claims._hb_wfd is None
                events.append((operation, key))
                return SimpleNamespace(returncode=0)

            def child(*args, events=events, children=children, **kwargs):
                process = Mock()
                process.terminate.side_effect = lambda: events.append(("stop", kwargs["env"]["TAUCETI_CLAIM_KEY"]))
                children.append(process)
                return process

            def evidence(*args, refusal=refusal, claims=claims):
                if refusal == "claim_replaced_during_prompt":
                    claims.stop_heartbeat()
                    claims.release()
                    assert claims.begin_branch_work(13941, HEAD, "other", "other", "TauCeti")
                if refusal != "pause_after_claim":
                    os.environ.pop(route.ENV)
                return ""

            gh.rebase_request_body = Mock(side_effect=evidence)
            w = SimpleNamespace(gh=gh, counters=counters, claims=claims)
            p = survey_mod.PRInfo.from_json(raw_pr())
            c = survey_mod.Candidate(13940, HEAD, foreign_repair_base=BASE, rebase_budget_key="rebase-pr-13940")
            with (
                patch.object(round_mod, "claims_repo", return_value="test/claims"),
                patch.object(round_mod.subprocess, "run", side_effect=command),
                patch.object(round_mod.subprocess, "Popen", side_effect=child),
                patch.object(work_units, "run_in_bubble") as launch,
            ):
                assert (
                    work_units.do_rebase(w, SimpleNamespace(open_prs=[p]), c, SimpleNamespace(agent_name="Codex"), True)
                    is None
                )
                expected = [("acquire", "branch/13940"), ("stop", "branch/13940"), ("release", "branch/13940")]
                if refusal == "claim_replaced_during_prompt":
                    expected.append(("acquire", "branch/13941"))
                else:
                    assert claims.held is None and claims._hb is None and claims._hb_wfd is None
                    assert "TAUCETI_CLAIM_KEY" not in os.environ and "TAUCETI_CLAIM_REPO" not in os.environ
                assert events == expected
                children[0].wait.assert_called_once_with(5)
                launch.assert_not_called()
                assert counters.read(c.rebase_budget_key) == 0 and not counters.foreign_repair_launched(13940)
                if refusal != "claim_replaced_during_prompt":
                    assert claims.begin_branch_work(13941, HEAD, "other", "other", "TauCeti")
                assert claims.held == ("branch/13941", "test/claims")
                assert claims._hb is children[1] and os.environ["TAUCETI_CLAIM_KEY"] == "branch/13941"
                assert events[-1] == ("acquire", "branch/13941")
                claims.stop_heartbeat()
                claims.release()
                # Round-end cleanup remains harmless after explicit cleanup.
                for cleanup in reversed(cleanups):
                    cleanup()
                assert [event for event in events if event[0] == "release"] == [
                    ("release", "branch/13940"),
                    ("release", "branch/13941"),
                ]


if __name__ == "__main__":
    test_current_at_launch_freezes_after_claim_and_survives_restart()
    test_current_at_launch_keeps_all_authentication_and_post_claim_pause_guards()
    test_declined_foreign_claim_lifecycle_before_next_candidate()
    test_default_off_and_exact_operator_scope()
    test_live_permission_pause_identity_and_movement_guards()
    test_only_authenticated_current_v3_source_request()
    test_frozen_genuine_request_fixture_without_live_execution()
    test_original_request_survives_main_advance_without_reissuing_or_retargeting()
    test_selection_is_rebase_only_and_one_attempt_without_budget_reset()
    test_paid_launch_rechecks_scope_and_requires_a_real_claim()
    print("PASS: default-off exact fork repair scope, live guards, one attempt, real claim and paid-launch fences")
