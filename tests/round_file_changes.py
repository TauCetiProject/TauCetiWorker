#!/usr/bin/env python3
"""What a round wrote is recorded from git, not recovered from the transcript.

"What did this round actually write?" and "did it build after its last edit?" were not answerable
from a log. An agent edits through a structured tool, a `python3 - <<EOF` heredoc, or `apply_patch`,
and a long command is truncated before its target path is reached. Recovering the paths by
pattern-matching command text was tried and withdrawn: it claimed a write for `jq '.a > .b'` and
missed `2>err.log`, and no amount of regex fixes that without a shell parser. git already knows
exactly.

Pinned here:

  1. A round that committed reports its committed files, from `pre..HEAD`.
  2. A round that left the tree dirty reports that separately, including untracked files, which is
     the case a `git diff` alone would miss entirely.
  3. Both at once, for a round that committed and then kept editing.
  4. A clean round says nothing, so the log does not grow a line per round for no reason.
  5. Output is bounded, and says how many it elided.
  6. Every git failure is silent: this is a log line and must never fail a round that succeeded.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0
lines: list[str] = []


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def fresh_repo(root: Path) -> Path:
    repo = root / "checkout"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


def run(repo, pre_head):
    lines.clear()
    orig = tc.work_units.log
    tc.work_units.log = lambda m: lines.append(str(m))
    try:
        tc.work_units.log_round_file_changes(types.SimpleNamespace(checkout=repo), pre_head)
    finally:
        tc.work_units.log = orig
    return "\n".join(lines)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = fresh_repo(root)
        head = git(repo, "rev-parse", "HEAD").stdout.strip()

        candidate = tc.Candidate(1, head)
        cfg = types.SimpleNamespace(checkout=repo)
        check(
            "an unchanged candidate can yield to other work",
            tc.work_units._candidate_checkout_unchanged(cfg, candidate),
        )
        check(
            "roadmap work is never discarded as a declined PR",
            not tc.work_units._candidate_checkout_unchanged(cfg, tc.Candidate(0, head)),
        )
        (repo / "unpublished.txt").write_text("retained work\n")
        check(
            "uncommitted work prevents declining the candidate",
            not tc.work_units._candidate_checkout_unchanged(cfg, candidate),
        )
        git(repo, "add", "unpublished.txt")
        git(repo, "commit", "-qm", "retained publication")
        check(
            "unpublished commits prevent declining the candidate",
            not tc.work_units._candidate_checkout_unchanged(cfg, candidate),
        )
        git(repo, "reset", "--hard", head)

        # Exercise the actual dispatch/cascade: a paid no-change fix consumes
        # its target allowance but does not stop another actionable PR.
        cfg.wid, cfg.logdir = "test", root / "logs"
        counts, attempts = {}, []
        counters = types.SimpleNamespace(
            read=lambda key: counts.get(key, 0),
            write=lambda key, value: counts.__setitem__(key, value),
        )
        worker = types.SimpleNamespace(cfg=cfg, counters=counters, gh=None, rs=None)
        survey = tc.Survey(worker_id="test")
        survey.needs_fix.actionable = [candidate, tc.Candidate(2, head)]
        opts = tc.RoundOpts(only=["fix"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)

        def fake_fix(_w, _sv, c, _opts, _bubble):
            attempts.append(c.pr)
            key = f"fix-{c.pr}-{c.head[:12]}"
            counters.write(key, counters.read(key) + 1)
            return 0

        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(root / "shared")}),
            patch.object(tc.work_units, "mirror_creds"),
            patch.object(tc.work_units, "survey", return_value=survey),
            patch.object(tc.work_units, "spread_candidates", side_effect=lambda cs: cs),
            patch.object(tc.work_units, "_still_actionable", return_value=True),
            patch.object(tc.work_units, "_host_agent_binary", return_value="python3"),
            patch.object(tc.work_units, "_progress_snapshot", return_value={}),
            patch.object(tc.work_units, "_progressed", side_effect=lambda _w, c, _pre: c.pr == 2),
            patch.object(tc.work_units, "do_fix", side_effect=fake_fix),
            patch("tauceti_worker.github_transport.observed_budget", return_value={}),
            patch("tauceti_worker.claude_api.api_mode", return_value=False),
            patch("tauceti_worker.claude_api.api_context", side_effect=lambda *_a, **_k: contextlib.nullcontext()),
        ):
            check(
                "no-change target falls through to another actionable target",
                tc.work_units.run_round(worker, opts) == 0 and attempts == [1, 2],
            )
            check("the no-change target attempt stays charged", counters.read(f"fix-1-{head[:12]}") == 1)
            # Retain unfinished local edits instead of switching checkouts.
            (repo / "retained.txt").write_text("unfinished\n")
            try:
                tc.work_units.dispatch("fix", worker, survey, candidate, opts)
            except tc.NoProgress:
                check("unpublished edits retain the recovery path", True)
            else:
                check("unpublished edits retain the recovery path", False)
            (repo / "retained.txt").unlink()
            from tauceti_worker.github_outbox import directory

            pending = directory() / "retained.json"
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text("{}")
            check(
                "pending publications prevent declining the candidate",
                not tc.work_units._candidate_checkout_unchanged(cfg, candidate),
            )
            pending.unlink()
            # A roadmap-fork intent has no unpublished work for this PR. It
            # must not strand a clean maintenance checkout in global backoff.
            pending.write_text(json.dumps({"argv": ["repo", "fork", "TauCetiProject/TauCetiRoadmap"]}))
            check(
                "unrelated publication permits other PR work",
                tc.work_units._candidate_checkout_unchanged(cfg, candidate),
            )
            pending.write_text(json.dumps({"argv": ["pr", "comment", "1", "--repo", "TauCetiProject/TauCeti"]}))
            check(
                "same-PR publication retains its recovery path",
                not tc.work_units._candidate_checkout_unchanged(cfg, candidate),
            )
            pending.write_text(json.dumps({"argv": ["pr", "comment", "2", "--repo", "TauCetiProject/TauCeti"]}))
            check(
                "another PR publication does not stall this checkout",
                tc.work_units._candidate_checkout_unchanged(cfg, candidate),
            )
            pending.write_text(json.dumps({"argv": ["pr", "comment", "--body", "pending"], "context": {"pr": "1"}}))
            check(
                "same-PR branch-relative publication is retained",
                not tc.work_units._candidate_checkout_unchanged(cfg, candidate),
            )
            pending.unlink()

        # Closing a superseded PR is progress even without a push/comment;
        # a stale merged/closed snapshot is not a new state transition.
        for before, after, expected in (
            ("OPEN", "CLOSED", True),
            ("OPEN", "MERGED", True),
            ("OPEN", "OPEN", False),
            ("CLOSED", "CLOSED", False),
        ):
            gh = types.SimpleNamespace(
                pr_progress_state=lambda _, after=after: {"head": head, "ncomments": 0, "state": after}
            )
            pre = {"head": head, "ncomments": 0, "state": before}
            check(
                "PR transition " + before + " -> " + after,
                tc.work_units._progressed_direct(types.SimpleNamespace(gh=gh), candidate, pre) == expected,
            )

        for state, changed_head, expected in (("OPEN", head, False), ("CLOSED", head, True), ("OPEN", "b" * 40, True)):
            gh = types.SimpleNamespace(
                pr_progress_state=lambda _, state=state, changed_head=changed_head: {
                    "head": changed_head,
                    "ncomments": 1,
                    "state": state,
                }
            )
            pre = {"head": head, "ncomments": 0, "state": "OPEN", "require_push": True}
            check(
                "final resolution requires push or closure: " + state,
                tc.work_units._progressed_direct(types.SimpleNamespace(gh=gh), candidate, pre) == expected,
            )

        # 4) a round that changed nothing is silent.
        check("a clean round logs nothing", run(repo, head) == "")

        # 1) committed work, the normal success path: the agent commits and pushes, so the tree is
        # clean and the evidence is only in the range.
        (repo / "TauCeti").mkdir()
        (repo / "TauCeti" / "New.lean").write_text("theorem t : True := trivial\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "feat: add")
        out = run(repo, head)
        check("committed files are reported", "files committed this round" in out)
        check("...naming the file", "TauCeti/New.lean" in out)
        check("...and nothing is claimed uncommitted", "left uncommitted" not in out)

        # 2) an untracked new file left behind: invisible to `git diff`, which is exactly why
        # status --porcelain is the right question.
        head2 = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "TauCeti" / "Abandoned.lean").write_text("sorry\n")
        out = run(repo, head2)
        check("an untracked leftover is reported", "left uncommitted" in out and "Abandoned.lean" in out)
        check("...and no commit is claimed", "files committed" not in out)

        # 3) both halves at once.
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "feat: more")
        (repo / "TauCeti" / "Dirty.lean").write_text("x\n")
        out = run(repo, head2)
        check(
            "committed and dirty are both reported",
            "files committed" in out and "left uncommitted" in out and "Dirty.lean" in out,
        )

        # 5) bounded.
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "wip")
        head3 = git(repo, "rev-parse", "HEAD").stdout.strip()
        for i in range(60):
            (repo / "TauCeti" / f"F{i}.lean").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "many")
        out = run(repo, head3)
        body = [ln for ln in out.splitlines() if ln.strip().startswith("TauCeti/")]
        check("the file list is bounded", len(body) <= tc.work_units._MAX_CHANGED_FILES)
        check("...and says how many it elided", "more" in out)

        # 6) failures are silent, never raised.
        try:
            check("a missing checkout logs nothing", run(root / "does-not-exist", head) == "")
        except Exception as e:
            check(f"a missing checkout logs nothing (raised {e!r})", False)
        try:
            check("an unknown pre-head still reports the dirty half", "files committed" not in run(repo, "0" * 40))
        except Exception as e:
            check(f"an unknown pre-head does not raise (raised {e!r})", False)
        try:
            check("no pre-head still works", run(repo, None) == "")
        except Exception as e:
            check(f"no pre-head does not raise (raised {e!r})", False)

    # The hook is wired only where an agent edits the host checkout.
    src = (REPO / "tauceti_worker" / "work_units.py").read_text()
    check("review is not in scope", "review" not in tc.work_units.FILE_CHANGE_STAGES)
    check("progress is not in scope", "progress" not in tc.work_units.FILE_CHANGE_STAGES)
    check(
        "roadmap and the fix-likes are",
        {"roadmap", "fix", "fix-ci", "rebase", "bump", "lint-repair"} == tc.work_units.FILE_CHANGE_STAGES,
    )
    check("a bubble round is skipped", "stage in FILE_CHANGE_STAGES and not bubble" in src)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
