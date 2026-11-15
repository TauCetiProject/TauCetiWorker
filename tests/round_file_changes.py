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
        cfg.wid, cfg.logdir, cfg.state = "test", root / "logs", root / "state"
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
            with patch.object(tc.work_units, "_round_mutated", return_value="closure" if expected else None):
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
            if changed_head != head:
                pre["published"] = changed_head
            with patch.object(
                tc.work_units, "_round_mutated", return_value="closure" if state == "CLOSED" else "reply"
            ):
                check(
                    "final resolution requires push or closure: " + state,
                    tc.work_units._progressed_direct(types.SimpleNamespace(gh=gh), candidate, pre) == expected,
                )

        # A finding that only the PR description can answer (a quality-pass report) leaves no source
        # change to push. Replacing the description and replying on its thread completes even the
        # final attempt; a reply alone, or nothing, still does not.
        gh = types.SimpleNamespace(pr_progress_state=lambda _: {"head": head, "ncomments": 1, "state": "OPEN"})
        pre = {"head": head, "ncomments": 0, "state": "OPEN", "require_push": True}
        for mutated, expected in (("description-reply", True), ("reply", False), (None, False)):
            with patch.object(tc.work_units, "_round_mutated", return_value=mutated):
                check(
                    "final resolution accepts a description repair with its reply: " + str(mutated),
                    tc.work_units._progressed_direct(types.SimpleNamespace(gh=gh), candidate, pre) == expected,
                )
        final = tc.work_units.FINAL_ATTEMPT_INSTRUCTION
        check(
            "the final-attempt instruction keeps its rule and names the exception",
            "A comment alone does not complete this attempt" in final
            and "update the description and reply on the finding's thread" in final,
        )

        from tauceti_worker import github_transport as transport

        gh = types.SimpleNamespace(
            pr_progress_state=lambda _: {
                "head": "b" * 40 if transport.direct_reads() else head,
                "ncomments": 0,
                "state": "OPEN",
            }
        )
        baseline = tc.work_units._progress_snapshot(types.SimpleNamespace(gh=gh), candidate)
        check("baseline bypasses the stale mirror", baseline["head"] == "b" * 40)
        check(
            "an earlier push is not this round's progress",
            tc.work_units._progressed(types.SimpleNamespace(gh=gh), candidate, baseline) is False,
        )
        check(
            "an unattributed head change stays unknown",
            tc.work_units._progressed(
                types.SimpleNamespace(gh=gh), candidate, {"head": head, "ncomments": 0, "state": "OPEN"}
            )
            is None,
        )
        journal = root / "mutations.jsonl"
        journal.write_text("")
        with patch.dict(os.environ, TAUCETI_FLEET_EVENTS=str(journal), TAUCETI_FLEET_MACHINE_ID="test"):
            baseline = {
                "head": "b" * 40,
                "ncomments": 0,
                "state": "OPEN",
                "round_id": "this-round",
                "mutation_cursor": tc.work_units._mutation_cursor(),
            }
            from tauceti_worker import fleet_events

            command = {"argv": ["pr", "comment", "1", "--repo", "TauCetiProject/TauCeti"], "created": 1}
            with patch.dict(os.environ, TAUCETI_ACTIVITY_ROUND_ID="other-round"):
                fleet_events.mutation(
                    command | {"context": {"activity": {"round_id": "other-round"}}},
                    types.SimpleNamespace(stdout=""),
                )
            check("a peer's reply is not attributed", tc.work_units._round_mutated(candidate, baseline) is None)
            with patch.dict(os.environ, TAUCETI_ACTIVITY_ROUND_ID="this-round"):
                fleet_events.mutation(
                    command
                    | {
                        "created": 2,
                        "context": {"activity": {"round_id": "this-round"}},
                        "argv": [
                            "api",
                            "-X",
                            "POST",
                            "repos/TauCetiProject/TauCeti/pulls/1/comments/42/replies",
                            "--repo",
                            "TauCetiProject/TauCeti",
                        ],
                    },
                    types.SimpleNamespace(stdout=""),
                )
            check("this round's reply is attributed", tc.work_units._round_mutated(candidate, baseline) == "reply")
            check(
                "a confirmed reply remains useful",
                tc.work_units._progressed(types.SimpleNamespace(gh=gh), candidate, baseline) is True,
            )
            baseline["mutation_cursor"] = tc.work_units._mutation_cursor()
            for method in ("DELETE", "PATCH"):
                with patch.dict(os.environ, TAUCETI_ACTIVITY_ROUND_ID="this-round"):
                    fleet_events.mutation(
                        command
                        | {
                            "created": method,
                            "context": {"activity": {"round_id": "this-round"}},
                            "argv": [
                                "api",
                                "-X",
                                method,
                                "repos/TauCetiProject/TauCeti/pulls/1/comments/42",
                                "--repo",
                                "TauCetiProject/TauCeti",
                            ],
                        },
                        types.SimpleNamespace(stdout=""),
                    )
                check(method + " is not a new reply", tc.work_units._round_mutated(candidate, baseline) is None)

            def record(created, argv, round_id="this-round"):
                # The journal takes an action's round from the command's own context.
                context = {"activity": {"round_id": round_id}}
                with patch.dict(os.environ, TAUCETI_ACTIVITY_ROUND_ID=round_id):
                    fleet_events.mutation(
                        command | {"created": created, "argv": argv, "context": context},
                        types.SimpleNamespace(stdout=""),
                    )

            edit = ["pr", "edit", "1", "--repo", "TauCetiProject/TauCeti", "--body-file", "/tmp/body.md"]
            reply = ["api", "-X", "POST", "repos/TauCetiProject/TauCeti/pulls/1/comments/42/replies"]
            final = baseline | {"require_push": True}
            baseline["mutation_cursor"] = final["mutation_cursor"] = tc.work_units._mutation_cursor()
            record("label", ["pr", "edit", "1", "--repo", "TauCetiProject/TauCeti", "--add-label", "x"])
            record("peer-edit", edit, round_id="other-round")
            record("reply-1", reply)
            check(
                "a reply without this round's description edit stays a reply",
                tc.work_units._round_mutated(candidate, final) == "reply",
            )
            check(
                "a reply alone does not complete a final attempt",
                tc.work_units._progressed(types.SimpleNamespace(gh=gh), candidate, final) is False,
            )
            baseline["mutation_cursor"] = final["mutation_cursor"] = tc.work_units._mutation_cursor()
            record("edit", edit)
            check(
                "a description edit alone is not a publication",
                tc.work_units._round_mutated(candidate, final) is None,
            )
            record("reply-2", reply)
            check(
                "a description edit with its reply is attributed",
                tc.work_units._round_mutated(candidate, final) == "description-reply",
            )
            check(
                "a description repair with its reply completes a final attempt",
                tc.work_units._progressed(types.SimpleNamespace(gh=gh), candidate, final) is True,
            )
            check(
                "and an ordinary one",
                tc.work_units._progressed(types.SimpleNamespace(gh=gh), candidate, baseline) is True,
            )
            baseline["mutation_cursor"] = final["mutation_cursor"] = tc.work_units._mutation_cursor()
            record("rest-edit", ["api", "-X", "PATCH", "repos/TauCetiProject/TauCeti/pulls/1", "-f", "body=new text"])
            record("reply-3", reply)
            check(
                "the REST form of a description edit counts the same",
                tc.work_units._round_mutated(candidate, final) == "description-reply",
            )
            baseline["mutation_cursor"] = tc.work_units._mutation_cursor()
            journal.unlink()
            check(
                "missing journal leaves observation unknown", tc.work_units._round_mutated(candidate, baseline) is None
            )
            journal.write_text("[]\n")
            baseline["mutation_cursor"] = (str(journal), journal.stat().st_ino, 0)
            check("malformed journal stays nonblocking", tc.work_units._round_mutated(candidate, baseline) is None)
        check(
            "claimed bubble uses its current direct baseline",
            tc.work_units._progressed(
                types.SimpleNamespace(gh=gh),
                candidate,
                {"head": head, "ncomments": 0, "state": "OPEN", "claimed_bubble": True},
            )
            is True,
        )

        # The journal is optional and best effort, so it cannot be what completes a final attempt.
        # Without its answer the same repair is read from the PR: the description's edit time moved,
        # a comment was added, and the source says both are this account's.
        class Source:
            def __init__(self, reports_edits=True):
                self.edited, self.ncomments, self.repair, self.asked = None, 0, True, []
                self.reports_edits = reports_edits

            def pr_progress_state(self, _pr):
                state = {"head": head, "ncomments": self.ncomments, "state": "OPEN"}
                return state | {"edited": self.edited} if self.reports_edits else state

            def pr_description_repair(self, pr, edited, ncomments):
                self.asked.append((pr, edited, ncomments))
                return self.repair

        def final_attempt(edit=True, reply=True, repair=True, final=True, source=None):
            source = source or Source()
            w = types.SimpleNamespace(gh=source)
            pre = tc.work_units._progress_snapshot(w, candidate)
            pre["require_push"] = final
            if edit:
                source.edited = "2026-11-13T12:00:00Z"
            source.ncomments += reply
            source.repair = repair
            return tc.work_units._progressed(w, candidate, pre), pre["mutation_cursor"], source.asked

        with patch.dict(os.environ):
            os.environ.pop("TAUCETI_FLEET_EVENTS", None)
            check("with the journal unset, a description repair completes a final attempt", final_attempt()[0] is True)
            check("...read from the PR, from where the round began", final_attempt()[1:] == (None, [(1, None, 0)]))
            check("a reply alone still does not", final_attempt(edit=False) == (False, None, []))
            check("nor a description edit alone", final_attempt(reply=False) == (False, None, []))
            check("a peer's edit or reply is not this round's", final_attempt(repair=False)[0] is False)
            check("a repair that cannot be read stays unknown", final_attempt(repair=None)[0] is None)
            check(
                "a source that reports no edit time answers as before",
                final_attempt(source=Source(reports_edits=False)) == (False, None, []),
            )
            check("an ordinary round is judged as before", final_attempt(final=False) == (None, None, []))
        with patch.dict(os.environ, TAUCETI_FLEET_EVENTS=str(root / "absent.jsonl"), TAUCETI_FLEET_MACHINE_ID="test"):
            check("with the journal missing when the round began, likewise", final_attempt()[:2] == (True, None))
            check("...and a reply alone still does not", final_attempt(edit=False)[0] is False)
        journal.write_text("")
        with patch.dict(os.environ, TAUCETI_FLEET_EVENTS=str(journal), TAUCETI_FLEET_MACHINE_ID="test"):
            # The journal recorded this round's reply and lost its description edit.
            source = Source()
            lost = {"head": head, "ncomments": 0, "state": "OPEN", "edited": None, "require_push": True}
            lost |= {"round_id": "this-round", "mutation_cursor": tc.work_units._mutation_cursor()}
            record("reply-4", reply)
            source.edited, source.ncomments = "2026-11-13T12:00:00Z", 1
            check("the journal has the reply alone", tc.work_units._round_mutated(candidate, lost) == "reply")
            check(
                "a journal that lost the edit does not hide the repair",
                tc.work_units._progressed(types.SimpleNamespace(gh=source), candidate, lost) is True,
            )

        # The source's own answer, from one request for the PR's edit time, editor and newest comments.
        from tauceti_worker import github

        def written(*comments, total=None):
            nodes = [{"createdAt": at, "author": {"login": who} if who else None} for who, at in comments]
            return {"totalCount": len(nodes) if total is None else total, "nodes": nodes}

        def pull(edited, editor, issue=(), threads=(), nthreads=None):
            return {
                "lastEditedAt": edited,
                "editor": {"login": editor} if editor else None,
                "comments": written(*issue),
                "reviewThreads": {
                    "totalCount": len(threads) if nthreads is None else nthreads,
                    "nodes": [{"comments": written(*thread)} for thread in threads],
                },
            }

        def repaired(pull_request, edited=None, ncomments=1, **answer):
            payload = {"data": {"viewer": {"login": "worker"}, "repository": {"pullRequest": pull_request}}} | answer
            client = types.SimpleNamespace(
                repo="TauCetiProject/TauCeti",
                _gh=lambda _args: types.SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
            )
            return github.GitHub.pr_description_repair(client, 1, edited, ncomments)

        finding, later = ("reviewer", "2026-11-13T10:00:00Z"), "2026-11-13T12:05:00Z"
        check(
            "this account's edit and its reply on the thread are a repair",
            repaired(pull("2026-11-13T12:00:00Z", "Worker", threads=[[finding, ("worker", later)]])) is True,
        )
        check(
            "an earlier edit is not this round's",
            repaired(
                pull("2026-11-13T09:00:00Z", "worker", threads=[[finding, ("worker", later)]]), "2026-11-13T09:00:00Z"
            )
            is False,
        )
        check(
            "nor a description never edited",
            repaired(pull(None, None, threads=[[finding, ("worker", later)]])) is False,
        )
        check(
            "a peer's edit is not attributed",
            repaired(pull("2026-11-13T12:00:00Z", "peer", threads=[[finding, ("worker", later)]])) is False,
        )
        check(
            "a peer's reply is not attributed",
            repaired(pull("2026-11-13T12:00:00Z", "worker", threads=[[finding, ("peer", later)]])) is False,
        )
        check(
            "nor a deleted account's",
            repaired(pull("2026-11-13T12:00:00Z", "worker", threads=[[finding, (None, later)]])) is False,
        )
        check(
            "this account's comment from before the round is not its reply",
            repaired(pull("2026-11-13T12:00:00Z", "worker", [("worker", "2026-11-13T09:00:00Z")], [[("peer", later)]]))
            is False,
        )
        check(
            "its reply counts among several new comments",
            repaired(
                pull(
                    "2026-11-13T12:00:00Z", "worker", [finding, ("worker", "2026-11-13T12:01:00Z")], [[("peer", later)]]
                )
            )
            is True,
        )
        crowded = pull("2026-11-13T12:00:00Z", "worker", threads=[[("worker", later)] * 10])
        crowded["reviewThreads"]["nodes"][0]["comments"]["totalCount"] = 12
        check("more new comments than the request shows stay unknown", repaired(crowded) is None)
        check(
            "and so do more threads than it shows",
            repaired(pull("2026-11-13T12:00:00Z", "worker", threads=[[finding, ("worker", later)]], nthreads=101))
            is None,
        )
        check(
            "a rejected query is unknown",
            repaired(pull("2026-11-13T12:00:00Z", "worker", threads=[[finding, ("worker", later)]]), errors=[{}])
            is None,
        )
        check("a missing PR is unknown", repaired(None) is None)
        failed = types.SimpleNamespace(repo="o/r", _gh=lambda _args: types.SimpleNamespace(returncode=1, stdout=""))
        check("a failed read is unknown", github.GitHub.pr_description_repair(failed, 1, None, 0) is None)

        def progress_state(pull_request):
            payload = {"data": {"repository": {"pullRequest": pull_request}}}
            client = types.SimpleNamespace(
                repo="TauCetiProject/TauCeti",
                _gh=lambda _args: types.SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
            )
            with transport.read_context(direct=True):
                return github.GitHub.pr_progress_state(client, 1)

        counted = {"headRefOid": head, "state": "OPEN", "comments": {"totalCount": 2}}
        counted["reviewThreads"] = {"totalCount": 1, "nodes": [{"comments": {"totalCount": 3}}]}
        check(
            "the progress read carries the description's edit time",
            progress_state(counted | {"lastEditedAt": "2026-11-13T12:00:00Z"})
            == {"head": head, "ncomments": 5, "state": "OPEN", "edited": "2026-11-13T12:00:00Z"},
        )
        check(
            "...None for a description never edited", progress_state(counted | {"lastEditedAt": None})["edited"] is None
        )
        check("...and nothing when the answer has none", "edited" not in progress_state(counted))

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
