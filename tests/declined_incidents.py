#!/usr/bin/env python3
"""A repair round that leaves nothing on GitHub is recorded, and can hold its head.

With TAUCETI_INCIDENTS_DIR unset nothing is written and selection is what it was. Set, a rebase, fix
or fix-ci round that ends with no push, closure or reply leaves a `declined` record carrying the
agent's final message, and a spent attempt budget leaves a `budget-spent` record once per head.
TAUCETI_HOLD_DECLINED=1 then keeps the stage off a declined head, in the survey, at dispatch and
again once the branch claim is held, until the head moves or the record is deleted; an acknowledged
record holds as well.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import contextlib
import hashlib
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_tools.forge import Forge, RepositoryRef

import tauceti_worker as tc
from tauceti_worker import agents, incidents, work_units
from tauceti_worker.review_state import Meta

survey_mod = importlib.import_module("tauceti_worker.survey")

fails = 0


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}" + ("" if ok else f": got={got!r} want={want!r}"))


def settings(root=None, hold=False):
    env = {k: v for k, v in os.environ.items() if k not in (incidents.ENV, incidents.HOLD_ENV)}
    if root is not None:
        env[incidents.ENV] = str(root)
    if hold:
        env[incidents.HOLD_ENV] = "1"
    return patch.dict(os.environ, env, clear=True)


def load(path):
    return json.loads(Path(path).read_text())


def pr(number, *, author="me", build="", conflicting=False, head_ref="feature"):
    return {
        "number": number,
        "headRefOid": f"head-{number}",
        "headRefName": head_ref,
        "headRepositoryOwner": {"login": author},
        "headRepository": {"name": "TauCeti"},
        "statusCheckRollup": [{"context": "build", "state": build}] if build else [],
        "author": {"login": author},
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
    }


# 1 conflicts, 2 is red, 3 and 4 carry a blocking review at their heads; 4 has spent its fix attempts.
RAW = [pr(1, conflicting=True), pr(2, build="FAILURE"), pr(3, build="SUCCESS"), pr(4, build="SUCCESS")]
SPENT = {"fix-4-head-4": tc.MAX_FIX_ATTEMPTS}


def classify():
    reviews = SimpleNamespace(
        observe=lambda _prs: None,
        gh_meta=lambda number: Meta({"head_sha": f"head-{number}" if number > 2 else ""}, "fresh"),
        ledger_blocking=lambda number, _head: number > 2,
        newest_contest_reply=lambda _number: None,
    )
    with patch.object(survey_mod, "me", lambda: "me"):
        sv = survey_mod.survey(
            SimpleNamespace(wid="test"),
            SimpleNamespace(open_prs=lambda: RAW),
            reviews,
            SimpleNamespace(read=lambda name: SPENT.get(name, 0)),
            stages=["rebase", "fix", "fix-ci"],
        )
    return {
        stage: (sorted(c.pr for c in sv.kind(stage).actionable), sorted(c.pr for c in sv.kind(stage).suppressed))
        for stage in incidents.STAGES
    }, sv


def test_records(root):
    with settings():
        check("unset: no record is written", incidents.record_declined("fix", 7, "h1", message="no"), None)
        check("unset: nothing is held", incidents.declined("fix", 7, "h1"), False)
        check("unset: nothing is listed", incidents.records(), [])
    check("unset: the directory is not created", root.exists(), False)

    message = (
        "Nothing to repair: #7 restates what #41 merged, see also owner/repo#9 and #52.\n"
        "Subsumed-by: #99\n\nlater revised\n**Subsumed-by:** #41 #7\n"
    )
    with settings(root):
        path = incidents.record_declined(
            "fix", 7, "h1", reason="blocking review at head", message=message, transcript="/logs/agent.log"
        )
        check("a declined round is one file per stage and PR", path, root / "declined-fix-7.json")
        value = load(path)
        check(
            "it carries the stage, PR, head and the agent's words",
            (value["schema"], value["kind"], value["stage"], value["pr"], value["head"], value["summary"]),
            (incidents.SCHEMA, "declined", "fix", 7, "h1", message.strip()),
        )
        check("the last Subsumed-by line names the replacements", value["subsumed_by"], [41])
        check("other PRs it mentions are kept apart", value["mentions"], [52, 99])
        check("the transcript is named", value["transcript"], "/logs/agent.log")
        check("no hold without TAUCETI_HOLD_DECLINED", incidents.declined("fix", 7, "h1"), False)

        long = incidents.record_declined("fix", 7, "h2", message="x" * 5000 + "\nSubsumed-by: #41")
        value = load(long)
        check(
            "a refresh counts, takes the new head, and bounds the summary",
            (value["count"], value["head"], len(value["summary"]) <= incidents.MAX_SUMMARY, value["subsumed_by"]),
            (2, "h2", True, [41]),
        )

    with settings(root, hold=True):
        check("held at the recorded head", incidents.declined("fix", 7, "h2"), True)
        check("not at another head", incidents.declined("fix", 7, "h3"), False)
        check("not for another stage", incidents.declined("rebase", 7, "h2"), False)
        (root / incidents.ACKED).mkdir()
        (root / "declined-fix-7.json").rename(root / incidents.ACKED / "declined-fix-7.json")
        check("an acknowledged record still holds", incidents.declined("fix", 7, "h2"), True)
        incidents.record_declined("fix", 7, "h4", message="again")
        check(
            "live and acknowledged records at different heads both hold",
            (incidents.declined("fix", 7, "h2"), incidents.declined("fix", 7, "h4")),
            (True, True),
        )

        incidents.note_budget_spent("rebase", 8, "h1", "conflicting; 3/3 attempts used")
        incidents.note_budget_spent("rebase", 8, "h1", "conflicting; 3/3 attempts used")
        spent = root / "budget-spent-rebase-8.json"
        check("a spent budget is written once per head", load(spent)["count"], 1)
        spent.rename(root / incidents.ACKED / spent.name)
        incidents.note_budget_spent("rebase", 8, "h1", "conflicting; 3/3 attempts used")
        check("an acknowledged one stays acknowledged", spent.exists(), False)
        incidents.note_budget_spent("rebase", 8, "h2", "conflicting; 3/3 attempts used")
        check("a new head is a new live record", load(spent)["head"], "h2")

        (root / "torn.json").write_text("{")
        check(
            "records lists live before acknowledged and skips unreadable files",
            [(p.parent.name, p.name) for p, _ in incidents.records()],
            [
                (root.name, "budget-spent-rebase-8.json"),
                (root.name, "declined-fix-7.json"),
                (incidents.ACKED, "budget-spent-rebase-8.json"),
                (incidents.ACKED, "declined-fix-7.json"),
            ],
        )
        check("…or one kind, live only", len(incidents.records(incidents.DECLINED, acked=False)), 1)

    with settings(root):
        # A second writer of the same record runs to completion inside the first one's rename.
        rename, inside = os.replace, []

        def interleaved(src, dst):
            if not inside:
                inside.append(None)
                inside[0] = incidents.record("declined", "fix-30", pr=30, by="B")
            rename(src, dst)

        with patch.object(incidents.os, "replace", interleaved):
            first = incidents.record("declined", "fix-30", pr=30, by="A")
        check(
            "concurrent writers of one record each publish a whole file",
            (inside[0], first, load(root / "declined-fix-30.json")["by"]),
            (root / "declined-fix-30.json", root / "declined-fix-30.json", "A"),
        )
        check("…and leave no temporary file", sorted(p.name for p in root.glob(".*.tmp")), [])

    blocked = root / "torn.json" / "below"
    with settings(blocked), patch.object(incidents, "log") as said:
        check("a write that fails returns None", incidents.record("declined", "fix-1", pr=1), None)
        check("…and says so", said.call_count, 1)


def test_survey(root):
    everything = {"rebase": ([1], []), "fix": ([3, 4], []), "fix-ci": ([2], [])}
    with settings():
        check("unset: every repair is offered", classify()[0], everything)
    with settings(root):
        for stage, number in (("rebase", 1), ("fix-ci", 2), ("fix", 3), ("fix", 4)):
            incidents.record_declined(stage, number, f"head-{number}", message="declined")
        check("records alone change no selection", classify()[0], everything)
    with settings(root, hold=True):
        got, sv = classify()
        check(
            "held: declined heads are suppressed", got, {"rebase": ([], [1]), "fix": ([], [3, 4]), "fix-ci": ([], [2])}
        )
        check(
            "…with the reason, also for a fix-focused worker",
            ({c.reason for stage in incidents.STAGES for c in sv.kind(stage).suppressed}, sv.fix_waiting),
            ({incidents.HELD}, [(3, incidents.HELD), (4, incidents.HELD)]),
        )
        for stage, number in (("rebase", 1), ("fix-ci", 2), ("fix", 3), ("fix", 4)):
            incidents.record_declined(stage, number, "an-earlier-head", message="declined")
        check("a new head is offered again", classify()[0], everything)


def test_round(root, tmp):
    cfg = SimpleNamespace(checkout=tmp / "checkout", wid="test", logdir=tmp / "logs", state=tmp / "state")
    counts, launched = {}, []
    counters = SimpleNamespace(read=lambda key: counts.get(key, 0), write=counts.__setitem__)
    worker = SimpleNamespace(cfg=cfg, counters=counters, gh=None, rs=None)

    def fake_fix(_w, _sv, c, _opts, _bubble):
        launched.append(c.pr)
        agents._LAST_AGENT_RUN = {"last_message": f"#{c.pr} is covered.\nSubsumed-by: #41", "transcript": "/t.log"}
        return 0

    def round_over(prs, *, dry_run=False, bump=False):
        sv = tc.Survey(worker_id="test")
        sv.open_prs = [SimpleNamespace(number=n, automatic_bump=bump, labels=[]) for n in prs]
        sv.needs_fix.actionable = [tc.Candidate(n, f"head-{n}", "blocking review at head") for n in prs]
        sv.red_ci.suppressed = [tc.Candidate(9, "head-9", "build failed at head", attempts=1, budget=3)]
        counts["ci-pr-9"] = 5
        launched.clear()
        with (
            patch.object(work_units, "mirror_creds"),
            patch.object(work_units, "survey", return_value=sv),
            patch.object(work_units, "spread_candidates", side_effect=lambda cs: cs),
            patch.object(work_units, "_still_actionable", return_value=True),
            patch.object(work_units, "_host_agent_binary", return_value="python3"),
            patch.object(work_units, "_progress_snapshot", return_value={}),
            patch.object(work_units, "_progressed", return_value=False),
            patch.object(work_units, "_candidate_checkout_unchanged", return_value=True),
            patch.object(work_units, "do_fix", side_effect=fake_fix),
            patch("tauceti_worker.github_transport.observed_budget", return_value={}),
            patch("tauceti_worker.claude_api.api_mode", return_value=False),
            patch("tauceti_worker.claude_api.api_context", side_effect=lambda *_a, **_k: contextlib.nullcontext()),
            contextlib.suppress(tc.NoProgress),
        ):
            work_units.run_round(
                worker,
                tc.RoundOpts(
                    only=["fix", "fix-ci"], agent="claude", work_model="claude", sandbox_host=True, dry_run=dry_run
                ),
            )
        return list(launched)

    with settings(), patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(tmp / "shared")}):
        check("unset: the no-change round runs as before", round_over([11, 12]), [11, 12])
        check("unset: and leaves no directory", root.exists(), False)

    with settings(root), patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(tmp / "shared")}):
        check("a dry run writes nothing", (round_over([11], dry_run=True), root.exists()), ([], False))
        check(
            "a no-change round on an automatic bump is not recorded",
            (round_over([13], bump=True), (root / "declined-fix-13.json").exists()),
            ([13], False),
        )
        check("set: both no-change rounds still run", round_over([11, 12]), [11, 12])
        value = load(root / "declined-fix-11.json")
        check(
            "each leaves the agent's final message",
            (value["head"], value["reason"], value["summary"], value["subsumed_by"], value["transcript"]),
            ("head-11", "blocking review at head", "#11 is covered.\nSubsumed-by: #41", [41], "/t.log"),
        )
        check("without the hold the head is taken again", round_over([11]), [11])
        check("…and the record counts it", load(root / "declined-fix-11.json")["count"], 2)
        spent = load(root / "budget-spent-fix-ci-9.json")
        check(
            "a suppressed candidate leaves one budget-spent record naming both budgets",
            (spent["head"], spent["count"], spent["detail"]),
            ("head-9", 1, "build failed at head; 1/3 attempts used at this head, 5/5 on the PR"),
        )

    with settings(root, hold=True), patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(tmp / "shared")}):
        # The survey here is canned, so this is the dispatch-time check a stale survey relies on.
        check("held: a declined head is not launched; a fresh one is", round_over([11, 14]), [14])
        check("…and that one is held from then on", round_over([14]), [])


def test_prompt(root):
    from tauceti_worker.github import GitHub

    gh = GitHub()
    raw = {**pr(20, conflicting=True), "labels": []}
    w = SimpleNamespace(
        gh=gh,
        claims=SimpleNamespace(begin_branch_work=lambda *args: True),
        rs=SimpleNamespace(bust=lambda *args: None),
    )

    def prompt():
        with (
            patch.object(survey_mod, "me", return_value="me"),
            patch.object(work_units, "me", return_value="me"),
            patch.object(gh, "open_prs", return_value=[raw]),
            patch.object(gh, "issue_comments", return_value=[]),
            patch.object(gh, "pr_progress_state", return_value=None),
            patch.object(gh, "pr_view", return_value={**raw, "state": "OPEN", "isDraft": False}),
            patch.object(work_units, "run_in_bubble", return_value=0) as agent,
        ):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, SimpleNamespace(read=lambda _: 0), deep=False)
            work_units._do_fixlike(
                w,
                sv,
                sv.rebaseable.actionable[0],
                SimpleNamespace(agent_name="Codex"),
                True,
                prompt_file="rebase.md",
                label="rebase",
            )
        return agent.call_args.args[2]

    with settings():
        check("unset: the repair prompt is unchanged", "Subsumed-by" in prompt(), False)
    with settings(root):
        check("set: the agent is told its final message is recorded", incidents.PROMPT in prompt(), True)


def test_claim_race(root):
    """A peer's decline written while this worker acquires the branch claim holds the head too."""
    p = SimpleNamespace(
        number=30, head_ref="feature", head_owner="me", head_repo="TauCeti", automatic_bump=False, labels=[]
    )
    sv = SimpleNamespace(open_prs=[p], owns=lambda _p: True)
    counts, released = {"fix-30-head-30": tc.MAX_FIX_ATTEMPTS}, []

    def claim(*_args):
        incidents.record_declined("fix", 30, "head-30", message="peer declined")
        claims.held = ("branch/30", "token")
        return True

    claims = SimpleNamespace(
        held=None,
        begin_branch_work=claim,
        stop_heartbeat=lambda: None,
        release=lambda: released.append(30),
    )
    w = SimpleNamespace(
        claims=claims, counters=SimpleNamespace(read=lambda k: counts.get(k, 0), write=counts.__setitem__)
    )
    c = tc.Candidate(30, "head-30", "blocking review at head")
    with (
        settings(root, hold=True),
        patch.object(work_units, "_progress_snapshot") as snapshot,
        patch.object(work_units, "run_in_bubble") as agent,
    ):
        got = work_units._do_fixlike(
            w,
            sv,
            c,
            SimpleNamespace(agent_name="Codex"),
            True,
            prompt_file="fix.md",
            label="fix",
            charged=("fix-30-head-30",),
        )
        check(
            "a repair whose head was declined during the claim launches nothing and refunds its attempt",
            (got, snapshot.called, agent.called, released, counts["fix-30-head-30"]),
            (None, False, False, [30], tc.MAX_FIX_ATTEMPTS - 1),
        )

    (root / "declined-fix-30.json").unlink()
    counts["fix-30-head-30"], released[:] = tc.MAX_FIX_ATTEMPTS, []
    with (
        settings(root, hold=True),
        patch.object(work_units, "_still_actionable", return_value=True),
        patch.object(work_units, "gh_run") as close,
    ):
        got = work_units._close_spent_pr(w, sv, c)
        check(
            "a spent head declined during the claim is left open",
            (got, close.called, released),
            (None, False, [30]),
        )


FORGEJO = RepositoryRef(Forge("forgejo", "https://forge.example"), "TauCetiProject/TauCeti")


def test_sources(root):
    """Records of one PR number on two sources are separate files; GitHub names are unchanged."""
    digest = hashlib.sha256(FORGEJO.key.encode()).hexdigest()[:12]
    with settings(root, hold=True):
        github = incidents.record_declined("fix", 7, "h-github", message="github")
        native = incidents.record_declined("fix", 7, "h-native", message="forgejo", repository=FORGEJO, repository_id=1)
        check(
            "the GitHub record keeps its name and a native one is qualified by its repository",
            (github.name, native.name),
            ("declined-fix-7.json", f"declined-fix-7.{digest}.json"),
        )
        check(
            "each names its forge and repository",
            (load(github)["forge"], load(native)["forge"], load(native)["repository"], load(native)["repository_id"]),
            (
                {"kind": "github", "url": "https://github.com"},
                {"kind": "forgejo", "url": "https://forge.example"},
                "TauCetiProject/TauCeti",
                1,
            ),
        )
        check(
            "the holds are independent",
            (
                incidents.declined("fix", 7, "h-github"),
                incidents.declined("fix", 7, "h-github", tc.TAUCETI),
                incidents.declined("fix", 7, "h-github", FORGEJO),
                incidents.declined("fix", 7, "h-native", FORGEJO),
                incidents.declined("fix", 7, "h-native"),
            ),
            (True, True, False, True, False),
        )
        (root / incidents.ACKED).mkdir()
        native.rename(root / incidents.ACKED / native.name)
        check("an acknowledged native record still holds", incidents.declined("fix", 7, "h-native", FORGEJO), True)
        check(
            "another GitHub repository is qualified too",
            incidents.subject("fix", 7, "Other/Repo"),
            f"fix-7.{hashlib.sha256(b'github:https://github.com/other/repo').hexdigest()[:12]}",
        )
        check(
            "the source of a native client is its repository, of a GitHub client its name",
            (incidents.source_of(SimpleNamespace(repository=FORGEJO, repo=tc.TAUCETI)), incidents.source_of(None)),
            (FORGEJO, None),
        )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test_records(tmp / "records")
        test_survey(tmp / "survey")
        test_round(tmp / "round", tmp)
        test_prompt(tmp / "prompt")
        test_claim_race(tmp / "race")
        test_sources(tmp / "sources")
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
