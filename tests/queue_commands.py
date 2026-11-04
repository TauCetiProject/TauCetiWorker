#!/usr/bin/env python3
"""Bors-compatible command parsing (tauceti_queue.commands) and its execution against a fake
controller and GitHub port.

Covered: the command corpus from bors-ng's COMMAND_PARSING.md for the commands the queue runs
(well formed, with leftover text, miscased, run into a longer word, prose), the review App's
exact-head forms, unsupported Bors commands, permission levels, and `run_comment`: permission
denied, drafts, approval evidence (head, reviewer, priority, admin merge base), stale exact-head
approvals, unapprove and cancel, and reply idempotency markers.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import commands as c  # noqa: E402
from tauceti_queue.github_adapter import PullRequest  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def kinds(body, **kw):
    return [cmd.kind for cmd in c.parse(body, **kw)]


class Parsing(unittest.TestCase):
    def test_well_formed_commands(self):
        for body, kind in [
            ("bors r+", "approve"),
            ("bors merge", "approve"),
            ("bors: r+", "approve"),
            ("BORS r+", "approve"),
            ("bors r-", "unapprove"),
            ("bors merge-", "unapprove"),
            ("bors cancel", "cancel"),
            ("bors p=5", "priority"),
            ("bors single on", "single"),
            ("bors ping", "ping"),
        ]:
            with self.subTest(body=body):
                parsed = c.parse(body)
                self.assertEqual([p.kind for p in parsed], [kind], body)
                self.assertTrue(parsed[0].runs)

    def test_approval_modifiers(self):
        (cmd,) = c.parse("bors r+ p=10")
        self.assertEqual((cmd.kind, cmd.priority), ("approve", 10))
        (cmd,) = c.parse("bors merge p=-3")
        self.assertEqual((cmd.kind, cmd.priority), ("approve", -3))
        (cmd,) = c.parse("bors r+ single on")
        self.assertEqual((cmd.kind, cmd.single), ("approve", True))
        (cmd,) = c.parse("bors r+   p=7")
        self.assertEqual(cmd.priority, 7, "runs of whitespace read as one space")

    def test_activation_by(self):
        (cmd,) = c.parse("bors r=alice")
        self.assertEqual((cmd.kind, cmd.reviewer), ("approve", "alice"))
        (cmd,) = c.parse("bors r=@alice,bob p=2")
        self.assertEqual((cmd.reviewer, cmd.priority), ("alice,bob", 2))
        (cmd,) = c.parse("bors merge=alice, bob")
        self.assertEqual(cmd.reviewer, "alice,bob")
        (cmd,) = c.parse("bors r=alice bob")
        self.assertEqual(cmd.kind, "refusal", "a word after the names can never become a login")
        self.assertIn("bors did not run `bors r=alice bob`", cmd.reply)
        (cmd,) = c.parse("bors r=")
        self.assertEqual(cmd.kind, "refusal")
        (cmd,) = c.parse("bors r=a.b")
        self.assertEqual(cmd.kind, "refusal")

    def test_leftover_text_refuses_the_line(self):
        for body in [
            "bors r+ now",
            "bors r- now",
            "bors cancel please",
            "bors merge!",
            "bors p=5 please",
            "bors ping!",
        ]:
            with self.subTest(body=body):
                parsed = c.parse(body)
                self.assertEqual([p.kind for p in parsed], ["refusal"], body)
                self.assertTrue(parsed[0].reply.startswith(":-1: bors did not run `" + body + "`"), parsed[0].reply)
                for line in parsed[0].reply.splitlines():
                    self.assertFalse(line.lower().startswith("bors"), "no reply line may begin with the trigger")

    def test_priorities(self):
        self.assertEqual(c.parse("bors p=abc")[0].kind, "refusal")
        self.assertEqual(c.parse("bors p=5abc")[0].kind, "refusal")
        self.assertEqual(
            c.parse("bors r+ p=abc")[0].kind, "refusal", "a modifier that cannot be read swallows its command"
        )
        self.assertEqual(c.parse("bors p=99999999999")[0].kind, "refusal")
        self.assertEqual(c.parse("bors p=-2147483648")[0].priority, -2147483648)

    def test_prose_stays_quiet(self):
        for body in [
            "bors merged this",
            "bors is slow today",
            "bors cancelled it",
            "bors single-handedly",
            "bors does this work",
            "bors merge-conflicts",
            "bors pinged",
            "thanks bors",
            "",
            None,
        ]:
            with self.subTest(body=body):
                self.assertEqual(c.parse(body), [], body)

    def test_corrections_are_suggestions(self):
        (cmd,) = c.parse("bors R+")
        self.assertEqual((cmd.kind, cmd.reply), ("autocorrect", "Did you mean `bors r+`?"))
        (cmd,) = c.parse("bors Merge")
        self.assertEqual(cmd.reply, "Did you mean `bors merge`?")
        (cmd,) = c.parse("bors r +")
        self.assertEqual(cmd.reply, "Did you mean `bors r+`?")
        (cmd,) = c.parse("bors p = 5")
        self.assertEqual(cmd.reply, "Did you mean `bors p=5`?")
        (cmd,) = c.parse("bors +r")
        self.assertEqual(cmd.reply, "Did you mean `bors r+`?")
        (cmd,) = c.parse("bors:r+")
        self.assertEqual(cmd.reply, "Did you mean `bors r+`?")
        (cmd,) = c.parse("bors R+ now")
        self.assertEqual(cmd.kind, "refusal", "a miscased line with leftover gets the leftover refusal")
        self.assertEqual(c.parse("bors +rebase"), [])

    def test_several_lines(self):
        body = "Looks good.\n\nbors r+\nbors p=3\nbors merge now"
        self.assertEqual(kinds(body), ["approve", "priority", "refusal"])

    def test_bros_never_runs(self):
        self.assertEqual(kinds("bros r+"), ["bros"])
        self.assertEqual(kinds("Bros r+"), ["bros"])
        self.assertEqual(kinds("bros is here"), [])
        self.assertFalse(c.parse("bros r+")[0].runs)

    def test_unsupported_bors_commands_are_refused_not_ignored(self):
        for body in [
            "bors try",
            "bors try --layout",
            "bors try-",
            "bors retry",
            "bors delegate+",
            "bors d=alice",
            "bors link #1",
            "bors stack",
            "bors unlink",
        ]:
            with self.subTest(body=body):
                (cmd,) = c.parse(body)
                self.assertEqual(cmd.kind, "unsupported", body)
                self.assertIn("the merge queue does not run", cmd.reply)
                self.assertEqual(cmd.level, "reviewer")

    def test_review_app_exact_head_forms(self):
        (cmd,) = c.parse(f"bors r+ sha={HEAD}", review_app=True)
        self.assertEqual((cmd.kind, cmd.sha, cmd.single), ("approve", HEAD, None))
        (cmd,) = c.parse(f"bors r+ single sha={HEAD}", review_app=True)
        self.assertEqual((cmd.kind, cmd.sha, cmd.single), ("approve", HEAD, True))
        (cmd,) = c.parse(f"bors r- sha={HEAD}", review_app=True)
        self.assertEqual((cmd.kind, cmd.sha), ("unapprove", HEAD))
        (cmd,) = c.parse(f"bors r+ sha={HEAD}", review_app=False)
        self.assertEqual(cmd.kind, "refusal", "a human typing sha= is refused as leftover, as Bors does")
        (cmd,) = c.parse("bors r+ sha=abc", review_app=True)
        self.assertEqual(cmd.kind, "refusal")

    def test_permission_levels(self):
        self.assertEqual(c.required_level(c.parse("bors ping")), "none")
        self.assertEqual(c.required_level(c.parse("bors r-")), "member")
        self.assertEqual(c.required_level(c.parse("bors r- now")), "member", "a refusal needs its command's level")
        self.assertEqual(c.required_level(c.parse("bors r+")), "reviewer")
        self.assertEqual(c.required_level(c.parse("bors r-\nbors p=1")), "reviewer")
        self.assertEqual(c.required_level(c.parse("bors R+")), "reviewer")
        self.assertEqual(c.required_level(c.parse("bors try")), "reviewer", "unknown refusals fail closed")


class FakeController:
    def __init__(self):
        self.calls = []

    def approve(self, repo, pr, head_sha, reviewer, priority, evidence):
        self.calls.append(("approve", repo, pr, head_sha, reviewer, priority, evidence))

    def unapprove(self, repo, pr, actor=""):
        self.calls.append(("unapprove", repo, pr))

    def cancel(self, repo, pr, actor=""):
        self.calls.append(("cancel", repo, pr))

    def set_priority(self, repo, pr, priority):
        self.calls.append(("set_priority", repo, pr, priority))


class FakeAdapter:
    def __init__(self, pr=None, permissions=None, merge_base="c" * 40):
        self.pr = pr
        self.permissions = permissions or {}
        self.merge_base = merge_base
        self.comments = []
        self.permission_lookups = 0

    def fetch_pr(self, repo, number):
        return self.pr

    def collaborator_permission(self, repo, login):
        self.permission_lookups += 1
        return self.permissions.get(login)

    def reviewer(self, login, repo=None):
        return self.collaborator_permission(repo, login) in ("admin", "maintain", "write")

    def repository_admin(self, login, repo=None):
        return self.collaborator_permission(repo, login) == "admin"

    def pr_merge_base(self, repo, number, head):
        return self.merge_base

    def post_comment(self, repo, number, body, marker):
        self.comments.append((number, body, marker))
        return len(self.comments)


def pr(**overrides):
    fields = dict(
        number=42,
        head_sha=HEAD,
        base_ref="main",
        title="t",
        body="",
        author_login="author",
        state="open",
        draft=False,
        labels=(),
        base_sha="d" * 40,
    )
    fields.update(overrides)
    return PullRequest(**fields)


def context(body, *, login="alice", review_app=False, kind="issue_comment", review_head=None):
    return c.CommentContext(
        repo="TauCetiProject/TauCeti",
        pr_number=42,
        actor_login=login,
        actor_id=7,
        body=body,
        created_at="2026-11-04T07:00:00Z",
        comment_id=1001,
        comment_url="https://github.com/TauCetiProject/TauCeti/pull/42#issuecomment-1001",
        kind=kind,
        review_app=review_app,
        review_head=review_head,
    )


class Running(unittest.TestCase):
    def test_prose_touches_nothing(self):
        controller, adapter = FakeController(), FakeAdapter(pr())
        self.assertEqual(c.run_comment(controller, adapter, context("bors is slow")), [])
        self.assertEqual(controller.calls, [])
        self.assertEqual(adapter.permission_lookups, 0)

    def test_permission_denied_once(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "read"})
        replies = c.run_comment(controller, adapter, context("bors r+\nbors p=1 now"))
        self.assertEqual(replies, [c.PERMISSION_DENIED])
        self.assertEqual(controller.calls, [])
        self.assertEqual(adapter.comments[0][2], "reply issue_comment:1001")

    def test_member_level_for_unapprove(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors r-"))
        self.assertEqual(controller.calls, [("unapprove", "TauCetiProject/TauCeti", 42)])

    def test_approval_records_evidence(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors r+ p=4"))
        (call,) = controller.calls
        self.assertEqual(call[:6], ("approve", "TauCetiProject/TauCeti", 42, HEAD, "alice", 4))
        evidence = call[6]
        self.assertEqual(evidence["kind"], "issue_comment")
        self.assertEqual((evidence["actor_login"], evidence["actor_id"], evidence["comment_id"]), ("alice", 7, 1001))
        self.assertIsInstance(evidence, dict, "the controller receives evidence as a plain dict")
        self.assertEqual(evidence["head_sha"], HEAD)
        self.assertFalse(evidence["admin"])
        self.assertIsNone(evidence["merge_base_sha"], "no merge base is read for a non-admin")
        self.assertEqual(evidence["command"], "bors r+ p=4")

    def test_admin_approval_carries_the_merge_base_for_scope_approvals(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"kim": "admin"})
        c.run_comment(controller, adapter, context("bors r+", login="kim"))
        evidence = controller.calls[0][6]
        self.assertTrue(evidence["admin"])
        self.assertEqual(evidence["merge_base_sha"], "c" * 40)

    def test_activation_by_names_the_reviewer(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors r=bob"))
        self.assertEqual(controller.calls[0][4], "bob")
        self.assertEqual(controller.calls[0][6]["actor_login"], "alice", "evidence names the actual commenter")

    def test_review_submission_approves_at_the_reviewed_head(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors r+", kind="pull_request_review", review_head=OTHER))
        self.assertEqual(controller.calls[0][3], OTHER)
        self.assertEqual(controller.calls[0][6]["kind"], "pull_request_review")

    def test_review_app_exact_head(self):
        controller, adapter = FakeController(), FakeAdapter(pr())
        c.run_comment(
            controller, adapter, context(f"bors r+ sha={HEAD}", login="tauceti-review-bot[bot]", review_app=True)
        )
        self.assertEqual(controller.calls[0][3], HEAD)
        self.assertEqual(controller.calls[0][6]["kind"], "review_app")
        self.assertEqual(adapter.permission_lookups, 0, "the review App is the reviewer, not a collaborator lookup")

    def test_review_app_stale_head_is_ignored(self):
        controller, adapter = FakeController(), FakeAdapter(pr(head_sha=OTHER))
        replies = c.run_comment(
            controller, adapter, context(f"bors r+ sha={HEAD}", login="tauceti-review-bot[bot]", review_app=True)
        )
        self.assertEqual((replies, controller.calls), ([], []))

    def test_draft_refuses_merge_bound_commands_and_runs_the_rest(self):
        controller, adapter = FakeController(), FakeAdapter(pr(draft=True), permissions={"alice": "admin"})
        replies = c.run_comment(controller, adapter, context("bors r+\nbors r-"))
        self.assertEqual(len(replies), 1)
        self.assertIn("draft", replies[0])
        self.assertEqual(controller.calls, [("unapprove", "TauCetiProject/TauCeti", 42)])

    def test_cancel_and_priority(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors cancel\nbors p=9"))
        self.assertEqual(
            controller.calls,
            [("cancel", "TauCetiProject/TauCeti", 42), ("set_priority", "TauCetiProject/TauCeti", 42, 9)],
        )

    def test_replies_carry_distinct_idempotency_markers(self):
        controller, adapter = FakeController(), FakeAdapter(pr(), permissions={"alice": "write"})
        c.run_comment(controller, adapter, context("bors r+ now\nbors Merge\nbors try"))
        markers = [m for _, _, m in adapter.comments]
        self.assertEqual(len(set(markers)), 3)
        self.assertTrue(all(m.startswith("reply issue_comment:1001:") for m in markers))
        self.assertEqual(controller.calls, [])

    def test_missing_pull_request_runs_nothing(self):
        controller, adapter = FakeController(), FakeAdapter(None, permissions={"alice": "write"})
        self.assertEqual(c.run_comment(controller, adapter, context("bors r+")), [])
        self.assertEqual(controller.calls, [])


if __name__ == "__main__":
    unittest.main()
