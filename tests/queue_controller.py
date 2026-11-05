#!/usr/bin/env python3
"""The queue controller's unit behaviours: settings, approval intake, events, the draw, tasks, holds, comments, status.

The end-to-end flows are in tests/queue_fixture.py; this file checks the controller's individual
decisions against the fake GitHub without driving whole head cycles.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain, feeds  # noqa: E402
from tauceti_queue.controller import (  # noqa: E402
    DEFAULT_SETTINGS,
    LOG_GRACE_SECONDS,
    MERGED_TITLE_PREFIX,
    STATUS_SCHEMA,
    VERDICT_CONTEXTS,
    ControllerError,
    TaskError,
)
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller  # noqa: E402
from tauceti_queue.ports import AGENT_INSTRUCTION  # noqa: E402
from tauceti_queue.store import loads  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")

    def approve(self, number, files, **kwargs):
        head = self.github.create_pr(number, files, **kwargs)
        return head, self.controller.approve(PROJECT, number, head, "reviewer")

    def failing_head(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "M.lean", "message": "boom"}])
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.github.complete_runs()
        self.controller.handle_event("status", {"sha": self.github.runs[0].head_sha, "context": "build"})
        self.controller.tick()
        return self.controller.store.row("batches", {"state": "head"})


class Verdicts(ControllerCase):
    """The statuses Bors required on the staging commit, `build`, `bump-guard` and `scope`, decide an attempt together."""

    def observe(self, verdict):
        """Approve one PR, dispatch it, complete its run under `verdict` and let the controller observe the statuses."""
        self.github.ci_policy = lambda *_: (verdict, [])
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[0].head_sha, "context": context})
        self.controller.tick()
        return self.controller.store.row("batches", {"id": head["id"]}), self.attempt(head)

    def attempt(self, head):
        return self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")

    def verdict_states(self):
        verdicts = self.controller.status()["head"]["ci"]["verdicts"]
        return {context: verdicts[context]["state"] for context in VERDICT_CONTEXTS}

    def test_all_three_verdicts_green_land_the_head(self):
        head, attempt = self.observe("pass")
        self.assertEqual(attempt["outcome"], "pass")
        verdicts = json.loads(attempt["verdicts"])
        self.assertEqual({c: v["state"] for c, v in verdicts.items()}, dict.fromkeys(VERDICT_CONTEXTS, "success"))
        self.assertTrue(all(v["bound"] for v in verdicts.values()))
        self.assertEqual(self.repo.main_sha(), head["tip"], "the promotion is drained in the same tick")
        self.assertEqual(head["state"], "landed")

    def test_a_red_scope_beside_a_green_build_is_a_source_failure_that_does_not_land(self):
        base = self.repo.main_sha()
        head, attempt = self.observe("scope-fail")
        self.assertEqual(attempt["outcome"], "source-failure")
        description = "restricted paths changed with no valid current bors scope approval"
        self.assertEqual(attempt["outcome_detail"], f"scope: {description}", "the status says why")
        self.assertEqual(
            json.loads(attempt["diagnostics"]),
            [
                {
                    "kind": "status",
                    "context": "scope",
                    "module": "",
                    "message": description,
                    "target_url": self.github.runs[0].to_candidate_run().html_url,
                }
            ],
            "the diagnostic carries the verdict and attributes it to no member",
        )
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), base, "a red scope is never fast-forwarded")
        self.assertEqual(self.controller.store.count("outbox", {"kind": "promote"}), 0)
        task = self.controller.store.row("tasks", {"batch_id": head["id"], "state": "open"})
        self.assertEqual(task["kind"], "repair")
        status = self.controller.status()
        json.dumps(status)
        self.assertEqual(status["head"]["stage"], "repair")
        self.assertEqual(self.verdict_states(), {"build": "success", "bump-guard": "success", "scope": "failure"})
        self.assertEqual(status["head"]["ci"]["verdicts"]["scope"]["description"], description)
        self.assertEqual(status["counters"].get("ci_source_failures"), 1)

    def test_a_red_bump_guard_beside_a_green_build_is_a_source_failure(self):
        head, attempt = self.observe("bump-guard-fail")
        self.assertEqual(attempt["outcome"], "source-failure")
        self.assertEqual(attempt["outcome_detail"], "bump-guard: Lake pin change failed the trusted bump validator")
        self.assertEqual(self.verdict_states(), {"build": "success", "bump-guard": "failure", "scope": "success"})
        self.assertEqual(
            self.controller.store.row("tasks", {"batch_id": head["id"], "state": "open"})["kind"], "repair"
        )

    def test_build_alone_waits_for_the_other_verdicts_then_counts_as_a_missing_status(self):
        head, attempt = self.observe("build-only")
        self.assertEqual(attempt["outcome"], "pending", "a bound build with the others absent is not yet a verdict")
        self.assertEqual(attempt["needs_observation"], 1)
        self.assertEqual(self.verdict_states(), {"build": "success", "bump-guard": "absent", "scope": "absent"})
        self.assertIn("waiting for bump-guard, scope", " ".join(self.controller.status()["reasons"]))
        self.clock.advance(LOG_GRACE_SECONDS)
        self.controller.tick()
        attempt = self.attempt(head)
        self.assertEqual(attempt["outcome"], "infrastructure")
        self.assertEqual(
            attempt["outcome_detail"], "missing-status: the completed run posted no bound bump-guard, scope status"
        )
        self.assertNotEqual(self.repo.main_sha(), head["tip"])


class Settings(ControllerCase):
    def test_defaults_and_validation(self):
        self.assertEqual(self.controller.setting("insertion_age_window"), 16)
        self.assertEqual(self.controller.setting("repair_concurrency"), 1)
        self.assertEqual(self.controller.setting("stall_interval_seconds"), 900)
        self.assertTrue(self.controller.setting("publication_enabled"), "the fixture enables publication")
        self.assertFalse(DEFAULT_SETTINGS["publication_enabled"])
        self.controller.set_setting("repair_concurrency", 2, "kim")
        self.assertEqual(self.controller.status()["settings"]["repair_concurrency"], 2)
        for key, value in (
            ("repair_concurrency", -1),
            ("repair_concurrency", True),
            ("publication_enabled", 1),
            ("membership_label", 3),
        ):
            with self.assertRaises(ControllerError):
                self.controller.set_setting(key, value, "kim")
        with self.assertRaises(ControllerError):
            self.controller.setting("nope")
        entry = self.controller.store.journal_entries("setting")[-1]
        self.assertEqual((entry["detail"]["key"], entry["detail"]["actor"]), ("repair_concurrency", "kim"))

    def test_canonical_branch_is_discovered_from_the_clone(self):
        self.assertEqual(self.controller.canonical_branch, "main")


class Approvals(ControllerCase):
    def test_approval_records_the_input_and_retains_the_head(self):
        head, result = self.approve(101, {"A.lean": "a\n"}, title="Add A", body="Adds A.", author="bob")
        self.assertEqual(result["reason"], "approved")
        row = result["input"]
        self.assertEqual(
            (row["title"], row["description"], row["author"], row["reviewer"]), ("Add A", "Adds A.", "bob", "reviewer")
        )
        self.assertEqual(row["retained_ref"], f"refs/tauceti-queue/inputs/101/{head}")
        self.assertTrue(chain.object_exists(self.repo.clone, head))
        self.controller.tick()
        self.assertEqual(self.repo.rev_parse(row["retained_ref"]), head, "retained on GitHub through the outbox")

    def test_approval_refusals(self):
        head = self.github.create_pr(101, {"A.lean": "a\n"}, labels=["stacked-pr"])
        self.assertEqual(
            self.controller.approve(PROJECT, 101, head, "r"),
            {"accepted": False, "reason": "blocked-label: stacked-pr", "input": None},
            "a PR carrying one of Bors's block labels is refused, as Bors refused it",
        )
        self.github.labels[101] = {"Hold", "stacked-pr"}
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "blocked-label: hold, stacked-pr")
        self.github.labels[101] = set()
        self.assertEqual(self.controller.approve(PROJECT, 101, "0" * 40, "r")["reason"], "head-mismatch")
        self.github.prs[101]["draft"] = True
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "draft")
        self.github.prs[101]["draft"] = False
        self.github.prs[101]["state"] = "closed"
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "closed")
        self.github.prs[101]["state"] = "open"
        self.github.prs[101]["base_ref"] = "release"
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "base-branch")
        with self.assertRaises(ControllerError):
            self.controller.approve("other/repo", 101, head, "r")
        self.assertEqual(self.controller.store.count("inputs"), 0)

    def test_new_head_supersedes_a_waiting_approval_and_import_keeps_original_age(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        new_head = self.github.push_pr(101, {"A.lean": "a2\n"})
        result = self.controller.approve(PROJECT, 101, new_head, "reviewer", priority=5, approved_at=1_700_000_000.0)
        self.assertEqual(result["reason"], "approved")
        rows = self.controller.inputs()
        self.assertEqual([(r["head_sha"], r["state"]) for r in rows], [(head, "cancelled"), (new_head, "waiting")])
        self.assertEqual(rows[0]["reason"], "superseded-by-new-approval")
        self.assertEqual((rows[1]["priority"], rows[1]["approved_at"]), (5, 1_700_000_000.0))
        self.assertEqual(self.controller.set_priority(PROJECT, 101, 9)["priority"], 9)
        self.assertEqual(self.controller.set_priority(PROJECT, 404, 9)["changed"], False)

    def test_waiting_inputs_react_to_events_and_members_do_not(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.handle_event(
            "pull_request",
            {"action": "synchronize", "number": 101, "pull_request": {"number": 101, "head": {"sha": "f" * 40}}},
        )
        self.assertEqual(self.controller.inputs()[0]["reason"], "head-changed")
        self.approve(102, {"B.lean": "b\n"})
        self.controller.handle_event(
            "pull_request", {"action": "converted_to_draft", "number": 102, "pull_request": {"number": 102}}
        )
        self.assertEqual(self.controller.inputs()[1]["reason"], "draft")
        self.approve(103, {"C.lean": "c\n"})
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 103, "pull_request": {"number": 103, "merged": True}}
        )
        self.assertEqual(self.controller.inputs()[2]["reason"], "merged-externally")
        self.approve(104, {"D.lean": "d\n"})
        self.controller.tick()
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 104, "pull_request": {"number": 104}}
        )
        self.assertEqual(self.controller.inputs()[3]["state"], "inserted")
        self.controller.handle_event("issue_comment", {"action": "created"})
        self.assertEqual(self.controller.store.count("journal", {"kind": "event"}), 5)

    def test_externally_landed_waiting_input_is_withdrawn_on_the_canonical_advance(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()  # inserted, sealed, dispatched: a member now
        head2, _ = self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()  # 102 is in the open batch
        head3, _ = self.approve(103, {"C.lean": "c\n"})
        # An admin lands 103's content with its Source-PR trailer directly on main.
        member = chain.Member(PROJECT, 103, head3, title="Change 103")
        self.controller.github.fetch_refs(PROJECT, [head3])
        built = chain.build(self.repo.clone, self.repo.main_sha(), [member], chain_id="admin")
        self.github.update_ref(PROJECT, "refs/heads/main", built.tip)
        self.controller.tick()
        states = {r["number"]: (r["state"], r["reason"]) for r in self.controller.inputs()}
        self.assertEqual(states[103], ("cancelled", "landed-externally"))
        self.assertEqual(states[102][0], "inserted", "members are kept")


class Draw(ControllerCase):
    def test_draw_is_random_inside_the_window_and_bounded(self):
        rows = [
            {"id": n, "number": n, "urgent": 0, "priority": 0, "approved_at": n, "passes_waited": 0}
            for n in range(1, 30)
        ]
        self.controller.set_setting("insertion_age_window", 4, "t")
        seen = set()
        for _ in range(50):
            seen.add(self.controller._draw(rows)["id"])
        self.assertEqual(seen, {1, 2, 3, 4})
        rows[2]["passes_waited"] = 4
        self.assertEqual(self.controller._draw(rows)["id"], 3, "an input that waited the window's passes is drawn")
        rows[1]["passes_waited"] = 4
        self.assertEqual(self.controller._draw(rows)["id"], 2, "the oldest overdue first")
        rows[10]["urgent"] = 1
        self.assertEqual(
            self.controller._draw(sorted(rows, key=lambda r: (-r["urgent"], r["approved_at"])))["id"],
            11,
            "urgent first",
        )


class Republication(ControllerCase):
    """Refs retained while publication was disabled reach GitHub once it is enabled, each pushed once."""

    def retained_pushes(self):
        pushes = []
        for name, args in self.github.calls:
            if name == "update_ref" and args[0].startswith("refs/tauceti-queue/"):
                pushes.append(args)
            elif name == "update_refs":
                pushes.extend(update for update in args[0] if update[0].startswith("refs/tauceti-queue/"))
        return pushes

    def retain_entries(self):
        return self.controller.store.rows("outbox", {"kind": "retain_ref"}, order="id")

    def test_refs_retained_in_shadow_mode_are_published_once_at_cutover(self):
        self.controller.set_setting("publication_enabled", False, "operator")
        heads = {}
        for number in (101, 102):
            heads[number], _ = self.approve(number, {f"Pr{number}.lean": "x\n"})
        self.controller.tick()  # 101 and 102 inserted and sealed as the head; nothing dispatched
        head = self.controller.store.row("batches", {"state": "head"})
        heads[103], _ = self.approve(103, {"Pr103.lean": "x\n"})
        self.controller.tick()  # 103 goes into the open batch behind the head
        open_batch = self.controller.store.row("batches", {"state": "open"})
        self.assertEqual(sorted(m["number"] for m in self.controller.members(head["id"])), [101, 102])
        self.assertEqual([m["number"] for m in self.controller.members(open_batch["id"])], [103])
        self.assertEqual(self.retained_pushes(), [], "shadow mode pushes nothing")
        self.assertEqual(self.retain_entries(), [], "shadow mode enqueues nothing")
        expected = {
            (self.controller.batch_ref(head["id"], head["version"]), head["tip"]),
            (self.controller.batch_ref(open_batch["id"], open_batch["version"]), open_batch["tip"]),
            *((self.controller.input_ref(n, sha), sha) for n, sha in heads.items()),
        }
        for ref, sha in expected:
            self.assertEqual(chain.resolve_commit(self.controller.clone, ref), sha, "retained locally")

        self.controller.set_setting("publication_enabled", True, "operator")
        self.assertEqual(
            {(json.loads(e["payload"])["ref"], json.loads(e["payload"])["sha"]) for e in self.retain_entries()},
            expected,
            "switching publication on enqueues every missing ref",
        )
        self.controller.tick()
        pushes = self.retained_pushes()
        self.assertEqual(sorted(pushes), sorted(expected), "every ref is pushed exactly once")
        for ref, sha in expected:
            self.assertEqual(self.repo.rev_parse(ref), sha, "the ref is on GitHub")
        self.assertEqual({e["state"] for e in self.retain_entries()}, {"done"})
        self.assertEqual(
            self.controller.store.count("ci_attempts", {"batch_id": head["id"]}), 1, "the head was dispatched"
        )
        self.assertEqual(self.controller.store.count("journal", {"kind": "republish"}), 1)

        entries = len(self.controller.store.rows("outbox"))
        self.controller.tick()
        self.assertEqual(len(self.controller.store.rows("outbox")), entries, "a second pass enqueues nothing")
        self.assertEqual(self.retained_pushes(), pushes, "and pushes nothing")

    def test_restart_republishes_and_dispatch_pushes_a_published_batch_ref_once(self):
        self.controller.set_setting("publication_enabled", False, "operator")
        head_sha, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.controller.store.set_setting("publication_enabled", True, "configuration", self.clock())
        recovered = self.controller.recover()
        self.assertEqual(recovered["republished_refs"], 2)
        self.assertEqual(recovered["outbox_pending"], 2)
        self.controller.tick()
        batch_ref = self.controller.batch_ref(head["id"], head["version"])
        self.assertEqual(
            sorted(self.retained_pushes()),
            sorted([(batch_ref, head["tip"]), (self.controller.input_ref(101, head_sha), head_sha)]),
        )
        self.assertEqual(self.controller.store.count("ci_attempts", {"batch_id": head["id"]}), 1)
        self.assertEqual(self.controller.recover()["republished_refs"], 0)


class Dispatch(ControllerCase):
    def test_dispatch_pushes_the_staging_candidate_branch_the_feed_names(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")
        branch = f"staging-candidate-{attempt['id']}-{head['version']}"
        self.assertTrue(attempt["dispatch_id"], "the dispatch id is recorded on the reserved attempt")
        names = [name for name, _ in self.github.calls]
        pushes = [args for name, args in self.github.calls if name == "update_ref"]
        self.assertIn((f"refs/heads/{branch}", head["tip"]), pushes, "the branch carries the head")
        self.assertIn((f"refs/tauceti-queue/batches/{head['id']}/v{head['version']}", head["tip"]), pushes)
        branch_push = self.github.calls.index(("update_ref", (f"refs/heads/{branch}", head["tip"])))
        self.assertLess(branch_push, names.index("dispatch_candidate"), "the branch is pushed before the dispatch")
        self.assertEqual(self.repo.rev_parse(f"refs/heads/{branch}"), head["tip"])
        requested = feeds.observation_feed(feeds.feed_source(self.controller), PROJECT, "main", head["id"])[
            "requested_batch"
        ]
        self.assertEqual(requested["state"], "running")
        self.assertEqual(
            requested["train"],
            {
                "candidate_id": attempt["id"],
                "generation": head["version"],
                "predecessor_id": None,
                "staging_ref": branch,
            },
        )

    def test_failed_dispatch_leaves_no_attempt(self):
        self.approve(101, {"A.lean": "a\n"})

        def refuse(*_args, **_kwargs):
            raise RuntimeError("dispatch refused")

        self.github.dispatch_candidate = refuse
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertIsNone(self.controller.store.row("ci_attempts", {"batch_id": head["id"]}))
        self.assertTrue(self.controller.store.journal_entries("dispatch-failed"))
        self.assertIn(
            "dispatch refused", self.controller.store.journal_entries("dispatch-failed")[-1]["detail"]["error"]
        )


class Rebuild(ControllerCase):
    def test_rebuild_head_regenerates_the_chain_supersedes_the_task_and_redispatches(self):
        head = self.failing_head()
        task = self.controller.store.row("tasks", {"batch_id": head["id"], "state": "open"})
        self.assertIsNotNone(task)
        result = self.controller.rebuild_head(PROJECT, "kim-em")
        self.assertTrue(result["rebuilt"], result)
        self.assertEqual(result["version"], head["version"] + 1)
        self.assertEqual(result["tasks_superseded"], 1)
        rebuilt = self.controller.store.row("batches", {"id": head["id"]})
        self.assertEqual((rebuilt["version"], rebuilt["tip"]), (head["version"] + 1, result["tip"]))
        self.assertEqual(self.controller.store.row("tasks", {"id": task["id"]})["state"], "superseded")
        dispatches = len(self.github.dispatches)
        self.controller.tick()
        self.assertEqual(len(self.github.dispatches), dispatches + 1, "the rebuilt head is dispatched again")
        self.assertEqual(
            self.repo.rev_parse(f"refs/tauceti-queue/batches/{head['id']}/v{rebuilt['version']}"), result["tip"]
        )
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")
        self.assertEqual((attempt["batch_version"], attempt["head_sha"]), (rebuilt["version"], rebuilt["tip"]))

    def test_rebuild_head_is_refused_without_a_head(self):
        self.assertEqual(self.controller.rebuild_head(PROJECT, "kim-em"), {"rebuilt": False, "reason": "no-head"})


class PreRebase(ControllerCase):
    """The next open batch is replayed onto the head only once the head's chain is submitted."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 2, "kim")  # room for one background claim

    def open_batch(self):
        return self.controller.store.row("batches", {"state": "open"})

    def pre_rebased(self):
        return self.controller.store.journal_entries("pre-rebased")

    def test_never_before_the_heads_chain_is_submitted(self):
        blocked = self.controller._pre_rebase_blocked
        self.assertEqual(blocked(None), "no head")
        # Unpublished: the head is sealed but never dispatched, so it is not waiting for a verdict.
        self.controller.set_setting("publication_enabled", False, "kim")
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual(blocked(head), f"head {head['id']} v{head['version']} is not dispatched")
        self.assertEqual((self.open_batch()["pre_rebase_state"], self.pre_rebased()), ("", []))
        self.assertIsNone(self.controller.status()["open_batches"][0]["pre_rebase"])
        # Dispatched and waiting for CI: now it is replayed.
        self.controller.set_setting("publication_enabled", True, "kim")
        self.controller.tick()
        self.assertIsNone(blocked(head))
        self.assertEqual([e["detail"]["onto"] for e in self.pre_rebased()], [head["tip"]])
        # Main moves past the head's base: the head must be rebased (and its CI rerun) first.
        self.repo.advance_main({"Z.lean": "z\n"})
        self.controller._observe_canonical(self.controller._fetch_canonical(), self.clock())
        self.assertEqual(blocked(head), f"head {head['id']} is not yet based on main")

    def test_never_while_the_head_has_an_open_task(self):
        head = self.failing_head()
        self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()
        self.assertEqual(self.controller._pre_rebase_blocked(head), f"head {head['id']} has an open task")
        self.assertEqual(self.open_batch()["pre_rebase_state"], "")
        self.assertEqual(self.pre_rebased(), [])
        self.assertEqual(self.controller.claim_task("w", "admin-worker").kind, "repair")

    def test_rebuilding_the_head_discards_the_pre_rebase_and_it_is_redone(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()
        self.assertEqual(self.open_batch()["pre_rebase_state"], "clean")
        self.assertTrue(self.controller.rebuild_head(PROJECT, "kim")["rebuilt"])
        batch = self.open_batch()
        self.assertEqual((batch["pre_rebase_state"], batch["pre_rebase_tip"]), ("", None))
        self.assertEqual(
            self.controller.store.journal_entries("pre-rebase-discarded")[0]["detail"]["reason"], "head-rebuilt"
        )
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual(self.open_batch()["pre_rebase_onto"], head["tip"])
        self.assertEqual(len(self.pre_rebased()), 2)

    def conflicting_batch(self):
        """A head waiting on CI and an open batch whose member 102 conflicts with it on `Shared.lean`."""
        self.approve(101, {"Shared.lean": "line one\nline two by 101\nline three\n"})
        self.controller.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        return self.controller.tick()

    def open_tasks(self, batch):
        return self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"}, order="id")

    def test_no_pre_rebase_task_until_prepared_and_then_one_from_the_prepared_chain(self):
        self.controller.set_setting("prepare_min_members", 1, "kim")
        self.controller.set_setting("prepare_quiet_seconds", 0, "kim")
        reasons = self.conflicting_batch()
        batch = self.open_batch()
        self.assertEqual([t["kind"] for t in self.open_tasks(batch)], ["prepare"], "one background kind per batch")
        self.assertEqual(batch["pre_rebase_state"], "conflict", "the conflict is recorded, its task waits")
        prepare = self.open_tasks(batch)[0]
        waits = f"open batch {batch['id']}: pre_rebase task waits for its preparation (prepare task {prepare['id']} is open)"
        reasons = self.controller.tick()
        self.assertIn(waits, reasons)
        background = self.controller.status()["open_batches"][0]["background"]
        self.assertEqual(background["active"], {"kind": "prepare", "task_id": prepare["id"], "claimed": False})
        self.assertEqual(background["waiting"]["kind"], "pre_rebase")
        task = self.controller.claim_task("w", "admin-worker")
        self.assertEqual(task.kind, "prepare")
        self.assertEqual([t["kind"] for t in self.open_tasks(batch)], ["prepare"])
        # The preparation edits 102 (so the chain is rewritten) and reports an error it left.
        agent = ScriptedAgent(
            "preparer",
            "admin-worker",
            self.repo,
            self.root / "preparer",
            lambda _task: {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two by 102\nline three\n", "Extra.lean": "extra\n"},
                    explanation="Added Extra.",
                )
            },
        )
        tip = agent.produce(task)
        left = [{"path": "Extra.lean", "line": 1, "column": 1, "message": "unknown identifier `extra`"}]
        notes = json.dumps({"worker": "w", "remaining_errors": left})
        adoption = self.controller.submit_task(task.task_id, task.claim_token, tip, worker_notes=notes)
        self.assertTrue(adoption.accepted, adoption)
        prepared = self.open_batch()
        self.assertEqual(prepared["prepared_version"], prepared["version"])
        self.controller.tick()
        [pre_rebase] = self.open_tasks(prepared)
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual(
            (pre_rebase["kind"], pre_rebase["base"], pre_rebase["tip"], pre_rebase["onto"]),
            ("pre_rebase", prepared["base"], tip, head["tip"]),
            "replayed from the prepared chain",
        )
        diagnostics = loads(pre_rebase["diagnostics"])
        self.assertEqual([d["kind"] for d in diagnostics], ["conflict", "cross_batch", "prepare_errors"])
        self.assertEqual(
            (diagnostics[2]["task_id"], diagnostics[2]["resolution"], diagnostics[2]["errors"]),
            (task.task_id, "adopted", left),
        )
        background = self.controller.status()["open_batches"][0]["background"]
        self.assertEqual((background["active"]["kind"], background["waiting"]), ("pre_rebase", None))
        self.assertEqual(self.controller.claim_task("w", "admin-worker").kind, "pre_rebase")

    def test_a_clean_preparation_hands_the_recorded_conflict_to_a_pre_rebase_task(self):
        self.controller.set_setting("prepare_min_members", 1, "kim")
        self.controller.set_setting("prepare_quiet_seconds", 0, "kim")
        self.conflicting_batch()
        batch = self.open_batch()
        task = self.controller.claim_task("w", "admin-worker")
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.controller.tick()
        [pre_rebase] = self.open_tasks(batch)
        self.assertEqual((pre_rebase["kind"], pre_rebase["tip"]), ("pre_rebase", batch["tip"]))
        self.assertEqual([d["kind"] for d in loads(pre_rebase["diagnostics"])], ["conflict", "cross_batch"])

    def test_a_preparation_waits_while_a_pre_rebase_task_is_claimed(self):
        self.controller.set_setting("prepare_min_members", 2, "kim")
        self.controller.set_setting("prepare_quiet_seconds", 0, "kim")
        self.conflicting_batch()
        batch = self.open_batch()
        [pre_rebase] = self.open_tasks(batch)
        self.assertEqual(pre_rebase["kind"], "pre_rebase", "below prepare_min_members: no preparation to wait for")
        task = self.controller.claim_task("w", "admin-worker")
        self.assertEqual(task.kind, "pre_rebase")
        # The batch becomes eligible for preparation while the agent works on its chain.
        self.controller.set_setting("prepare_min_members", 1, "kim")
        reasons = self.controller.tick()
        self.assertEqual([t["kind"] for t in self.open_tasks(batch)], ["pre_rebase"])
        self.assertIn(
            f"open batch {batch['id']}: preparation waits for the claimed pre_rebase task {pre_rebase['id']}", reasons
        )
        background = self.controller.status()["open_batches"][0]["background"]
        self.assertEqual(background["active"], {"kind": "pre_rebase", "task_id": pre_rebase["id"], "claimed": True})
        self.assertEqual(background["waiting"]["kind"], "prepare")
        # Given back unresolved, the pre_rebase task gives way to the preparation.
        self.controller.release_task(task.task_id, task.claim_token, "artifacts_unavailable")
        self.controller.tick()
        self.assertEqual([t["kind"] for t in self.open_tasks(batch)], ["prepare"])
        superseded = self.controller.store.row("tasks", {"id": pre_rebase["id"]})
        self.assertEqual((superseded["state"], superseded["resolution"]), ("superseded", "prepare"))

    def test_the_setting_turns_it_off(self):
        self.controller.set_setting("pre_rebase_enabled", False, "kim")
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()
        self.assertEqual(self.pre_rebased(), [])


class Tasks(ControllerCase):
    def test_task_record_carries_members_diagnostics_refs_and_instruction(self):
        head = self.failing_head()
        self.assertIsNone(self.controller.claim_task("w", "nobody"))
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual(task.kind, "repair")
        self.assertEqual(
            (task.batch_id, task.batch_version, task.base, task.tip, task.onto),
            (head["id"], head["version"], head["base"], head["tip"], head["base"]),
        )
        self.assertEqual(task.diagnostics, [{"kind": "compiler", "module": "M.lean", "message": "boom"}])
        self.assertEqual(task.members[0].number, 101)
        self.assertEqual(task.members[0].retained_ref, f"refs/tauceti-queue/inputs/101/{task.members[0].accepted_head}")
        self.assertIn(f"refs/tauceti-queue/batches/{head['id']}/v{head['version']}", task.retained_refs)
        self.assertEqual(task.instruction, AGENT_INSTRUCTION)
        self.assertEqual(task.expires_at, self.clock() + self.controller.setting("claim_ttl_seconds"))
        json.dumps(task.to_dict())
        status = self.controller.status()["head"]
        self.assertEqual(status["claim"]["worker"], "worker-1")
        self.assertEqual(status["task"]["active_claims"][0]["worker"], "worker-1")

    def test_task_api_errors(self):
        head = self.failing_head()
        with self.assertRaises(TaskError) as caught:
            self.controller.renew_task(99, "x")
        self.assertEqual(caught.exception.code, "unknown-claim")
        task = self.controller.claim_task("worker-1", "admin-worker")
        with self.assertRaises(TaskError):
            self.controller.renew_task(task.task_id, "wrong-token")
        with self.assertRaises(TaskError):
            self.controller.submit_task(99, task.claim_token, head["tip"])
        with self.assertRaises(TaskError):
            self.controller.submit_task(task.task_id, "wrong-token", head["tip"])
        # Admin permission revoked between claim and submit (the cached answer forgotten).
        self.github.admins.discard("admin-worker")
        self.controller.admins.invalidate("admin-worker")
        result = self.controller.submit_task(task.task_id, task.claim_token, head["tip"])
        self.assertEqual(result.reason, "not-authorized")
        self.github.admins.add("admin-worker")
        self.controller.admins.invalidate("admin-worker")
        # An unchanged chain resubmitted is a valid (if pointless) adoption: nothing edited.
        result = self.controller.submit_task(task.task_id, task.claim_token, head["tip"])
        self.assertTrue(result.accepted)
        self.assertEqual(result.provenance, {f"{PROJECT}#101": "clean"})

    def test_admin_answers_are_cached_and_served_stale_during_an_outage(self):
        head = self.failing_head()

        def checks():
            return [c for c in self.github.calls if c[0] == "repository_admin"]

        self.assertIsNotNone(self.controller.claim_task("worker-1", "admin-worker"))
        self.assertIsNone(self.controller.claim_task("worker-2", "admin-worker"), "refused by concurrency")
        self.assertEqual(len(checks()), 1, "one GitHub call per login, not per claim")
        self.assertIsNone(self.controller.claim_task("worker-3", "stranger"))
        self.assertIsNone(self.controller.claim_task("worker-3", "stranger"))
        self.assertEqual(len(checks()), 2, "a refusal is cached too")
        # Seven hours on every answer has expired and GitHub is rate limited: the stale answers
        # stand, and a login never checked before is neither granted nor refused as a non-admin.
        self.clock.advance(7 * 3600)
        self.github.unavailable = "rate limited for 2400s"
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertIsNotNone(task, "the stale admin answer keeps the worker claiming")
        self.assertEqual(len(checks()), 3, "the refresh was attempted")
        self.assertIsNone(self.controller.claim_task("worker-3", "stranger"))
        self.assertIsNone(self.controller.claim_task("worker-4", "newcomer"))
        refusals = self.controller.store.journal_entries("claim-refused")
        self.assertEqual(refusals[-2]["detail"]["reason"], "not-admin")
        self.assertEqual(refusals[-1]["detail"]["reason"], "github-unavailable")
        result = self.controller.submit_task(task.task_id, task.claim_token, head["tip"])
        self.assertTrue(result.accepted, "the submission's admin check serves the same stale answer")

    def test_invalid_chains_are_rejected_with_the_tool_violations(self):
        head = self.failing_head()
        task = self.controller.claim_task("worker-1", "admin-worker")
        # A chain that drops the member (tip == base) fails validation: count mismatch.
        result = self.controller.submit_task(task.task_id, task.claim_token, head["base"])
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("count-mismatch", {v["code"] for v in result.violations})
        # A chain with an extra commit for an unknown PR.
        stranger = chain.Member(PROJECT, 999, head["base"], title="not a member")
        built = chain.build(self.repo.clone, head["tip"], [stranger], chain_id="stranger")
        result = self.controller.submit_task(task.task_id, task.claim_token, built.tip)
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("unknown-member", {v["code"] for v in result.violations})
        self.assertEqual(self.controller.status()["counters"]["chain_validation_failures"], 2)
        self.assertTrue(any("failed validation 2 times" in a for a in self.controller.status()["alerts"]))
        # A commit whose message lost the prior text.
        commits = chain.chain_commits(self.repo.clone, head["base"], head["tip"])
        rewritten = chain.commit_tree(
            self.repo.clone,
            commits[0].tree,
            head["base"],
            f"rewritten\n\nSource-PR: {PROJECT}#101\nAccepted-Head: {commits[0].accepted_head}\n",
            ("a", "a@x"),
        )
        result = self.controller.submit_task(task.task_id, task.claim_token, rewritten)
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("message-not-preserved", {v["code"] for v in result.violations})
        self.controller.tick()  # the retention push is an outbox side effect
        self.assertEqual(
            self.repo.rev_parse(f"refs/tauceti-queue/batches/{head['id']}/submissions/{rewritten}"),
            rewritten,
            "rejected chains stay retained",
        )
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["version"], head["version"])

    def test_repair_concurrency_allows_parallel_claims_and_first_adoption_wins(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.github.admins.add("second-admin")
        self.failing_head()
        one = ScriptedAgent(
            "one",
            "admin-worker",
            self.repo,
            self.root / "one",
            lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "one\n"}, explanation="One.")},
        )
        two = ScriptedAgent(
            "two",
            "second-admin",
            self.repo,
            self.root / "two",
            lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "two\n"}, explanation="Two.")},
        )
        task_one = one.claim(self.controller)
        task_two = two.claim(self.controller)
        self.assertIsNotNone(task_two)
        self.assertEqual(task_one.task_id, task_two.task_id)
        tip_two = two.produce(task_two)
        tip_one = one.produce(task_one)
        self.assertTrue(self.controller.submit_task(task_two.task_id, task_two.claim_token, tip_two).accepted)
        late = self.controller.submit_task(task_one.task_id, task_one.claim_token, tip_one)
        self.assertEqual((late.accepted, late.reason), (False, "claim-superseded"))
        self.assertEqual(
            self.repo.file_at(self.controller.store.row("batches", {"state": "head"})["tip"], "A.lean"), "two\n"
        )


class Preparation(ControllerCase):
    """`prepare` tasks: an open batch is built and repaired on its own base while the head is in CI."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("prepare_min_members", 3, "t")
        # Background claims leave one slot for head work: with 1 there are none.
        self.controller.set_setting("repair_concurrency", 2, "t")

    def open_batch(self, numbers=(201, 202, 203)):
        """A head waiting on CI and an open batch of `numbers` behind it."""
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "ci")
        for number in numbers:
            self.approve(number, {f"P{number}.lean": f"{number}\n"})
        self.controller.tick()
        return self.controller.store.row("batches", {"state": "open"})

    def prepare_tasks(self, state="open"):
        return self.controller.store.rows("tasks", {"kind": "prepare", "state": state}, order="id")

    def quiet(self):
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.controller.tick()

    def land_head(self):
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[-1].head_sha, "context": context})
        self.controller.tick()  # observes the verdict and promotes
        self.controller.tick()  # seals the next head

    def test_a_quiet_open_batch_gets_one_prepare_task_on_its_own_base(self):
        self.assertTrue(DEFAULT_SETTINGS["prepare_open_batches"])
        self.assertEqual(
            (DEFAULT_SETTINGS["prepare_min_members"], DEFAULT_SETTINGS["prepare_quiet_seconds"]), (20, 600)
        )
        batch = self.open_batch()
        self.assertEqual(self.prepare_tasks(), [], "not before the quiet period")
        self.clock.advance(self.controller.setting("prepare_quiet_seconds") - 1)
        self.controller.tick()
        self.assertEqual(self.prepare_tasks(), [])
        self.quiet()
        self.controller.tick()
        tasks = self.prepare_tasks()
        self.assertEqual(len(tasks), 1, "at most one open prepare task per open batch")
        task = tasks[0]
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual(
            (task["batch_id"], task["batch_version"], task["base"], task["tip"], task["onto"]),
            (batch["id"], batch["version"], batch["base"], batch["tip"], batch["base"]),
        )
        self.assertNotEqual(task["tip"], head["tip"], "never stacked on the head")
        self.assertEqual(loads(task["diagnostics"]), [])
        status = self.controller.status()
        summary = status["open_batches"][0]
        self.assertEqual(summary["prepare_task"]["task_id"], task["id"])
        self.assertEqual((summary["prepared_version"], summary["prepared"]), (None, False))
        self.assertEqual([t["kind"] for t in status["tasks"]], ["prepare"])
        self.assertEqual(status["open_batch"]["prepare_task"]["kind"], "prepare")
        self.assertEqual(status["counters"]["tasks_prepare"], 1)
        claimed = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((claimed.kind, claimed.batch_id, claimed.tip), ("prepare", batch["id"], batch["tip"]))
        self.assertEqual(claimed.extra["renew_after"], 60, "a short renewal so a superseded preparation stops soon")
        self.assertIsNone(self.controller.claim_task("worker-2", "admin-worker"), "within repair_concurrency")

    def test_small_batches_and_the_setting_turned_off_get_no_prepare_task(self):
        self.controller.set_setting("prepare_min_members", 4, "t")
        self.open_batch()
        self.quiet()
        self.assertEqual(self.prepare_tasks(), [])
        self.controller.set_setting("prepare_min_members", 3, "t")
        self.controller.set_setting("prepare_open_batches", False, "t")
        self.controller.tick()
        self.assertEqual(self.prepare_tasks(), [])
        self.controller.set_setting("prepare_open_batches", True, "t")
        self.controller.tick()
        self.assertEqual(len(self.prepare_tasks()), 1)

    def fail_head(self):
        head = self.controller.store.row("batches", {"state": "head"})
        self.github.runs[-1].verdict = "fail"
        self.github.runs[-1].diagnostics = [{"kind": "compiler", "module": "M.lean", "message": "boom"}]
        self.github.complete_runs()
        self.controller.handle_event("status", {"sha": head["tip"], "context": "build"})
        self.controller.tick()
        return head

    def test_a_running_preparation_is_not_superseded_and_a_head_task_is_claimed_at_once(self):
        self.open_batch()
        self.quiet()
        preparation = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual(preparation.kind, "prepare")
        self.assertIsNone(self.controller.claim_task("worker-2", "admin-worker"), "the last slot is the head's")
        self.fail_head()
        self.assertEqual([t["id"] for t in self.prepare_tasks()], [preparation.task_id])
        self.controller.renew_task(preparation.task_id, preparation.claim_token)
        repair = self.controller.claim_task("worker-2", "admin-worker")
        self.assertEqual(repair.kind, "repair", "head work never waits behind background work")
        self.assertIsNone(self.controller.claim_task("worker-3", "admin-worker"))

    def test_with_concurrency_one_a_preparation_is_never_claimed(self):
        self.controller.set_setting("repair_concurrency", 1, "t")
        self.open_batch()
        self.quiet()
        self.assertEqual(len(self.prepare_tasks()), 1)
        self.assertIsNone(self.controller.claim_task("worker-1", "admin-worker"), "no background claims at 1")
        self.fail_head()
        self.quiet()
        self.assertEqual(len(self.prepare_tasks()), 1, "still open beside the head's task")
        self.assertEqual(self.controller.claim_task("worker-1", "admin-worker").kind, "repair")
        self.assertIsNone(self.controller.claim_task("worker-2", "admin-worker"))

    def test_a_preparation_is_created_and_claimed_while_the_head_has_a_task(self):
        batch = self.open_batch()
        head = self.fail_head()
        self.quiet()
        [task] = self.prepare_tasks()
        self.assertEqual(task["batch_id"], batch["id"])
        self.controller.set_setting("repair_concurrency", 2, "t")
        first = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((first.kind, first.batch_id), ("repair", head["id"]), "the head's task is claimed first")
        second = self.controller.claim_task("worker-2", "admin-worker")
        self.assertEqual((second.kind, second.batch_id), ("prepare", batch["id"]), "a second worker prepares")
        self.assertIsNone(self.controller.claim_task("worker-3", "admin-worker"), "two claims in total")
        result = self.controller.submit_task(second.task_id, second.claim_token, batch["tip"])
        self.assertTrue(result.accepted, "adopted while the head is being repaired")
        self.assertEqual(self.controller.store.row("tasks", {"id": first.task_id})["state"], "open")

    def test_claims_are_ordered_by_kind_priority_then_age(self):
        batch = self.open_batch()
        self.quiet()
        head = self.controller.store.row("batches", {"state": "head"})
        # A head task created after the preparation still comes first.
        self.clock.advance(5)
        self.controller._create_task(head, "repair", head["base"], [], self.clock())
        self.controller.set_setting("repair_concurrency", 3, "t")
        first = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((first.kind, first.batch_id), ("repair", head["id"]))
        second = self.controller.claim_task("worker-2", "admin-worker")
        self.assertEqual((second.kind, second.batch_id), ("prepare", batch["id"]), "before a second head claim")
        third = self.controller.claim_task("worker-3", "admin-worker")
        self.assertEqual(third.kind, "repair", "then the head's task takes the claims left")
        self.assertIsNone(self.controller.claim_task("worker-4", "admin-worker"))

    def test_the_submission_adopts_repaired_commits_and_keeps_the_batch_open(self):
        batch = self.open_batch()
        self.quiet()
        agent = ScriptedAgent(
            "preparer",
            "admin-worker",
            self.repo,
            self.root / "preparer",
            lambda task: {f"{PROJECT}#202": Resolution({"P202.lean": "fixed\n"}, explanation="Fixed P202.")},
        )
        result = agent.run(self.controller)
        self.assertTrue(result.accepted, result)
        after = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(after["state"], "open")
        self.assertEqual(after["version"], batch["version"] + 1)
        self.assertEqual(after["prepared_version"], after["version"])
        self.assertEqual((after["base"], after["tip"]), (batch["base"], result.tip))
        members = {m["number"]: m for m in self.controller.members(batch["id"])}
        self.assertEqual(members[202]["provenance"], "edited")
        self.assertIn("Fixed P202.", members[202]["explanation"])
        self.assertEqual(members[201]["provenance"], "clean")
        self.assertEqual(self.repo.file_at(after["tip"], "P202.lean"), "fixed\n")
        status = self.controller.status()
        self.assertTrue(status["open_batches"][0]["prepared"])
        self.assertEqual(status["counters"]["prepares_adopted"], 1)
        self.quiet()
        self.assertEqual(self.prepare_tasks(), [], "prepared at its current version")
        # A new member makes the batch eligible again once it is quiet.
        self.approve(204, {"P204.lean": "204\n"})
        self.controller.tick()
        self.assertEqual(self.prepare_tasks(), [])
        self.quiet()
        self.assertEqual(len(self.prepare_tasks()), 1)

    def test_insertion_into_a_batch_under_preparation_goes_to_the_next_batch(self):
        batch = self.open_batch()
        self.quiet()
        preparation = self.controller.claim_task("worker-1", "admin-worker")
        self.approve(301, {"P301.lean": "301\n"})
        reasons = self.controller.tick()
        self.assertIn(f"open batch {batch['id']} is being prepared; insertion uses the next one", reasons)
        row = self.controller.input_for(PROJECT, 301)
        self.assertEqual(row["state"], "inserted")
        self.assertNotEqual(row["batch_id"], batch["id"])
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["version"], batch["version"])
        self.assertEqual([b["id"] for b in self.controller.batches("open")], [batch["id"], row["batch_id"]])
        result = self.controller.submit_task(preparation.task_id, preparation.claim_token, batch["tip"])
        self.assertTrue(result.accepted, "the preparation still applies to its unchanged batch")

    def test_sealing_supersedes_a_running_preparation(self):
        batch = self.open_batch()
        self.quiet()
        preparation = self.controller.claim_task("worker-1", "admin-worker")
        self.land_head()
        sealed = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(sealed["state"], "head", "a preparation does not hold the head back")
        self.assertEqual(self.prepare_tasks("superseded")[0]["resolution"], "sealed")
        result = self.controller.submit_task(preparation.task_id, preparation.claim_token, batch["tip"])
        self.assertEqual((result.accepted, result.reason), (False, "claim-superseded"))

    def test_a_clean_release_records_the_version_and_a_failed_one_is_not_retried(self):
        batch = self.open_batch()
        self.quiet()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.assertEqual(
            self.controller.store.row("batches", {"id": batch["id"]})["prepared_version"], batch["version"]
        )
        self.assertEqual(self.prepare_tasks("submitted")[0]["resolution"], "prepared")
        self.quiet()
        self.assertEqual(self.prepare_tasks(), [])
        self.approve(204, {"P204.lean": "204\n"})
        self.controller.tick()
        self.quiet()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(task.task_id, task.claim_token, "agent_failed", "no progress")
        self.assertEqual(self.prepare_tasks("superseded")[-1]["resolution"], "failed")
        self.quiet()
        self.assertEqual(self.prepare_tasks(), [], "not retried at the same version")
        # A release for want of artifacts leaves the task open for the next claim, as for the head.
        self.approve(205, {"P205.lean": "205\n"})
        self.controller.tick()
        self.quiet()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(task.task_id, task.claim_token, "artifacts_unavailable")
        self.assertEqual(len(self.prepare_tasks()), 1)
        self.assertEqual(self.controller.claim_task("worker-1", "admin-worker").task_id, task.task_id)


class Holds(ControllerCase):
    def test_hold_starts_after_the_threshold_and_backs_off(self):
        threshold = self.controller.setting("inference_failure_threshold")
        for _ in range(threshold - 1):
            self.assertIsNone(self.controller.report_inference("w", False, "err"))
        self.assertIsNone(self.controller.inference_hold())
        hold = self.controller.report_inference("w", False, "quota")
        self.assertEqual((hold["since"], hold["last_error"], hold["attempts"]), (self.clock(), "quota", 0))
        first_retry = hold["next_retry_at"]
        self.assertEqual(first_retry, self.clock() + self.controller.setting("inference_hold_backoff_seconds"))
        hold = self.controller.report_inference("w", False, "again")
        self.assertEqual(
            hold["next_retry_at"], first_retry, "a report before the retry time does not extend the backoff"
        )
        self.clock.advance(first_retry - self.clock() + 1)
        hold = self.controller.report_inference("w", False, "still")
        self.assertEqual(hold["attempts"], 1)
        self.assertEqual(
            hold["next_retry_at"], self.clock() + 2 * self.controller.setting("inference_hold_backoff_seconds")
        )
        self.assertEqual(hold["last_error"], "still")
        self.assertEqual(self.controller.status()["inference_hold"]["reports"], threshold + 2)
        self.controller.report_inference("w", True)
        self.assertIsNone(self.controller.inference_hold())
        self.assertEqual(self.controller.status()["counters"], {"inference_holds": 1, "inference_holds_resolved": 1})
        # Failures start counting again from zero after a success.
        self.controller.report_inference("w", False, "err")
        self.assertIsNone(self.controller.inference_hold())


class Comments(ControllerCase):
    def test_member_comment_text(self):
        batch = {"id": 7, "version": 3}
        clean = {
            "number": 101,
            "accepted_head": "a" * 40,
            "provenance": "clean",
            "commit_sha": "b" * 40,
            "explanation": "",
            "edited_shas": "[]",
        }
        text = self.controller.member_comment(batch, clean)
        self.assertEqual(text.splitlines()[0], "Pull request successfully merged into main.")
        self.assertIn("Build succeeded:", text)
        self.assertIn(f"Merged revision: `{'a' * 40}`.", text)
        self.assertNotIn("edited", text)
        edited = dict(
            clean,
            provenance="edited",
            explanation="Kept #102's version of the lemma.\n\nDropped the duplicate import.",
            edited_shas=json.dumps(["c" * 40, "b" * 40]),
        )
        text = self.controller.member_comment(batch, edited)
        self.assertIn(f"https://github.com/{PROJECT}/commit/{'b' * 40}", text)
        self.assertIn("> Kept #102's version of the lemma.\n>\n> Dropped the duplicate import.", text)
        self.assertIn(f"Related PRs: [#102](https://github.com/{PROJECT}/pull/102).", text)
        self.assertIn(f"Edited revisions: [`{'c' * 12}`](https://github.com/{PROJECT}/commit/{'c' * 40}).", text)
        self.assertIn("refs/tauceti-queue/batches/7/v3", text)
        self.assertEqual(self.controller._mentioned_prs(f"see {PROJECT}#5, other/repo#6 and #7 and #5", exclude=7), [5])
        self.assertEqual(MERGED_TITLE_PREFIX, "[Merged by Bors] - ")


class NoRepair(ControllerCase):
    """The `queue-no-repair` opt-out: read at approval, refreshed by label events, sent with tasks."""

    def label_event(self, number, action, name, labels=None):
        pull = {"number": number}
        if labels is not None:
            pull["labels"] = [{"name": label} for label in labels]
        self.controller.handle_event(
            "pull_request", {"action": action, "number": number, "label": {"name": name}, "pull_request": pull}
        )

    def test_label_at_approval_and_label_events_set_the_opt_out(self):
        self.approve(101, {"A.lean": "a\n"}, labels=["Queue-No-Repair"])
        self.approve(102, {"B.lean": "b\n"})
        rows = {row["number"]: row for row in self.controller.inputs()}
        self.assertEqual((rows[101]["no_repair"], rows[102]["no_repair"]), (1, 0))
        self.label_event(102, "labeled", "queue-no-repair", ["queue-no-repair"])
        self.assertEqual(self.controller.input_for(PROJECT, 102)["no_repair"], 1)
        self.controller.tick()  # both inserted and sealed
        head = self.controller.store.row("batches", {"state": "head"})
        members = {m["number"]: m for m in self.controller.members(head["id"])}
        self.assertEqual((members[101]["no_repair"], members[102]["no_repair"]), (1, 1))
        self.label_event(101, "unlabeled", "queue-no-repair", [])
        self.label_event(101, "labeled", "awaiting-author", ["awaiting-author"])
        members = {m["number"]: m for m in self.controller.members(head["id"])}
        self.assertEqual(members[101]["no_repair"], 0)
        self.assertEqual(self.controller.store.count("journal", {"kind": "no-repair"}), 2)
        self.assertEqual(self.controller.store.count("journal", {"kind": "member-event-ignored"}), 1)
        self.assertEqual(self.controller.status()["counters"]["no_repair_opt_outs"], 2)
        self.assertEqual(
            [m["no_repair"] for m in self.controller.status()["head"]["members"] if m["number"] == 102], [True]
        )

    def test_the_label_list_is_a_setting(self):
        self.assertEqual(DEFAULT_SETTINGS["no_repair_labels"], ["queue-no-repair"])
        with self.assertRaises(ControllerError):
            self.controller.set_setting("no_repair_labels", "queue-no-repair", "operator")
        with self.assertRaises(ControllerError):
            self.controller.set_setting("no_repair_labels", [1], "operator")
        self.controller.set_setting("no_repair_labels", ["queue-no-repair", "no-agents"], "operator")
        self.approve(101, {"A.lean": "a\n"}, labels=["no-agents"])
        self.assertEqual(self.controller.input_for(PROJECT, 101)["no_repair"], 1)

    def test_tasks_carry_the_opt_out_and_an_unattributed_withdrawal_says_so(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "B.lean", "message": "boom"}])
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"}, labels=["queue-no-repair"])
        self.controller.tick()
        self.github.complete_runs()
        self.controller.handle_event("status", {"sha": self.github.runs[0].head_sha, "context": "build"})
        self.controller.tick()
        agent = ScriptedAgent(
            "agent",
            "admin-worker",
            self.repo,
            self.root / "agent",
            lambda task: {f"{PROJECT}#102": Resolution(empty=True, explanation="Emptied; it would not build.")},
        )
        self.assertTrue(agent.run(self.controller).accepted)
        self.assertEqual({m.number: m.no_repair for m in agent.tasks[0].members}, {101: False, 102: True})
        record = loads(self.controller.input_for(PROJECT, 102)["anticipated"])
        self.assertEqual((record["stage"], record["interacting"]), ("unattributed", None))
        self.controller.tick()
        body = self.github.comments_for(102)[0]["body"]
        self.assertIn("could not attribute the change to another PR, file or finding", body)
        self.assertIn("> Emptied; it would not build.", body)
        self.assertEqual(self.controller.status()["anticipated"], [], "nothing to follow")


class EditSummary(ControllerCase):
    def test_a_long_diff_is_capped_with_a_note_and_the_commit_link(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "Long.lean", "message": "boom"}])
        self.approve(101, {"Long.lean": "".join(f"line {i}\n" for i in range(500))})
        self.controller.tick()
        self.github.complete_runs()
        self.controller.handle_event("status", {"sha": self.github.runs[0].head_sha, "context": "build"})
        self.controller.tick()
        agent = ScriptedAgent(
            "agent",
            "admin-worker",
            self.repo,
            self.root / "agent",
            lambda task: {
                f"{PROJECT}#101": Resolution(
                    {"Long.lean": "".join(f"changed {i}\n" for i in range(500))}, explanation="Rewrote every line."
                )
            },
        )
        self.assertTrue(agent.run(self.controller).accepted)
        head = self.controller.store.row("batches", {"state": "head"})
        member = self.controller.members(head["id"])[0]
        text = self.controller.member_comment(head, member)
        self.assertIn("The diff is 1005 lines; the first 400 are shown.", text)
        self.assertIn(f"[the landed commit](https://github.com/{PROJECT}/commit/{member['commit_sha']})", text)
        self.assertEqual(text.count("\n-line "), 395)

    def test_an_empty_commit_names_no_landed_pr(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        base = self.repo.main_sha()
        member = chain.Member(PROJECT, 101, head, title="Change 101")
        built = chain.build(self.repo.clone, base, [member], chain_id="t")
        empty = chain.commit_tree(
            self.repo.clone,
            chain.tree_of(self.repo.clone, built.tip),
            built.tip,
            chain.read_commit(self.repo.clone, built.tip).message.replace("#101", "#102"),
            ("a", "a@example.invalid"),
        )
        self.assertEqual(chain.landed_prs(self.repo.clone, base, empty), {f"{PROJECT}#101": built.tip})


class StatusShape(ControllerCase):
    def test_status_is_json_serialisable_and_stable_when_empty(self):
        status = self.controller.status()
        json.dumps(status)
        self.assertEqual(status["schema"], "tauceti-queue.status/v1")
        self.assertEqual(status["service"]["revision"], "fixture")
        self.assertEqual(status["service"]["started_at"], self.clock())
        self.assertEqual(
            set(status),
            {
                "schema",
                "repository",
                "canonical_branch",
                "canonical_sha",
                "service",
                "settings",
                "publication_enabled",
                "inference_hold",
                "waiting",
                "open_batch",
                "open_batches",
                "head",
                "external_head",
                "approvals",
                "anticipated",
                "tasks",
                "outbox",
                "reasons",
                "alerts",
                "counters",
                "head_cycle_seconds",
                "insertion_passes",
                "max_passes_waited",
            },
        )
        self.assertIsNone(status["head"])
        self.assertIsNone(status["external_head"])
        self.assertEqual(status["approvals"], {"webhook": 0, "polled": 0, "revoked_by_poll": 0, "last_poll": None})
        self.assertEqual(status["waiting"]["count"], 0)
        self.approve(101, {"A.lean": "a\n"})
        self.clock.advance(30)
        status = self.controller.status()
        self.assertEqual((status["waiting"]["count"], status["waiting"]["oldest_age_seconds"]), (1, 30))
        self.controller.tick()
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "ci")
        self.assertEqual(status["head"]["size"], 1)
        self.assertEqual(status["open_batch"]["size"], 0)
        self.assertEqual(status["head"]["ci"]["dispatch_id"], "dispatch-1")
        json.dumps(status)

    def test_recover_releases_controller_claims_and_returns_reservations(self):
        self.approve(101, {"A.lean": "a\n"})
        row = self.controller.inputs()[0]
        self.controller.store.update("inputs", {"id": row["id"]}, state="reserved")
        batch = self.controller._create_open_batch(self.repo.main_sha(), self.clock())
        self.controller._take_claim(batch, "insert", self.clock())
        self.controller.store.update("claims", {"batch_id": batch["id"]}, worker="controller")
        report = self.controller.recover()
        self.assertEqual((report["released_claims"], report["returned_reservations"]), (1, 1))
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")
        self.assertEqual(self.controller.store.row("claims", {"batch_id": batch["id"]})["release_reason"], "restart")


class TickBudget(ControllerCase):
    """One tick's work is bounded by wall time so the controller lock is not held for minutes."""

    def inserted(self):
        return sorted(row["number"] for row in self.controller.inputs() if row["state"] == "inserted")

    def test_insertion_stops_at_the_probe_budget_and_resumes_next_tick_without_starving(self):
        self.controller.set_setting("insertion_probe_budget_seconds", 0, "t")  # one probe per tick
        numbers = [101, 102, 103, 104]
        for number in numbers:
            self.approve(number, {f"F{number}.lean": f"{number}\n"})
        wake = self.controller.wake_event()
        wake.clear()
        reasons = self.controller.tick()
        self.assertEqual(len(self.inserted()), 1, "the budget stops the pass after the first probe")
        self.assertIn("insertion paused after 1 probes (budget)", reasons)
        self.assertTrue(wake.is_set(), "a budget stop wakes the loop for the next tick")
        self.assertFalse(
            [e for e in self.controller.store.journal_entries() if "paus" in e["kind"]], "no journal per stop"
        )
        for expected in (2, 3, 4):
            reasons = self.controller.tick()
            self.assertEqual(len(self.inserted()), expected, "each tick continues where the last stopped")
        self.assertEqual(self.inserted(), numbers, "every input is inserted; the budget starves nobody")
        self.assertFalse(any("(budget)" in reason for reason in reasons))
        self.assertEqual(len([r for r in self.controller.inputs() if r["state"] == "waiting"]), 0)

    def test_outbox_drain_stops_at_its_budget_and_wakes_the_loop(self):
        self.controller.set_setting("outbox_drain_budget_seconds", 0, "t")  # one operation per drain
        for number in (101, 102):
            self.approve(number, {f"F{number}.lean": "x\n"})  # two retained input refs, pushed as one batch
        self.controller._enqueue("labels", "labels:101", {"number": 101, "add": ["queued"], "remove": []})
        self.controller.wake_event().clear()
        self.controller._reasons = []
        self.controller._drain_outbox(self.clock())
        pending = self.controller.store.rows("outbox", {"state": "pending"})
        self.assertEqual([entry["kind"] for entry in pending], ["labels"])
        self.assertIn("outbox drain paused with 1 entries pending (budget)", self.controller._reasons)
        self.assertTrue(self.controller.wake_event().is_set())
        self.controller._drain_outbox(self.clock())
        self.assertEqual(self.controller.store.count("outbox", {"state": "pending"}), 0)


class OutboxBatching(ControllerCase):
    def retains(self, state):
        return self.controller.store.rows("outbox", {"kind": "retain_ref", "state": state}, order="id")

    def test_retained_refs_are_pushed_in_one_batch(self):
        heads = {number: self.approve(number, {f"F{number}.lean": "x\n"})[0] for number in (101, 102, 103)}
        self.github.calls.clear()
        self.controller._drain_outbox(self.clock())
        batched = [args for name, args in self.github.calls if name == "update_refs"]
        expected = tuple(
            (self.controller.input_ref(number, heads[number]), heads[number]) for number in (101, 102, 103)
        )
        self.assertEqual(batched, [(expected,)], "the three retained refs go to GitHub in one push")
        self.assertEqual(self.retains("pending"), [])
        self.assertEqual([entry["result"] for entry in self.retains("done")], ["retained"] * 3)
        for number, head in heads.items():
            self.assertEqual(self.repo.rev_parse(self.controller.input_ref(number, head)), head)

    def test_a_failed_batch_falls_back_to_single_pushes(self):
        heads = {number: self.approve(number, {f"F{number}.lean": "x\n"})[0] for number in (101, 102)}
        self.github.calls.clear()
        self.github.fail_next["update_refs"] = 1
        self.github.fail_next["update_ref"] = 1  # the first single push fails too; the second must still go
        self.controller._drain_outbox(self.clock())
        names = [name for name, _ in self.github.calls]
        self.assertEqual(names, ["update_refs", "update_ref", "update_ref"])
        pending = self.retains("pending")
        self.assertEqual(
            [(entry["attempts"], loads(entry["payload"], {})["sha"]) for entry in pending], [(1, heads[101])]
        )
        self.assertEqual([loads(entry["payload"], {})["sha"] for entry in self.retains("done")], [heads[102]])
        self.assertEqual(self.repo.rev_parse(self.controller.input_ref(102, heads[102])), heads[102])

    def test_a_single_retained_ref_is_pushed_alone(self):
        self.approve(101, {"F101.lean": "x\n"})
        self.github.calls.clear()
        self.controller._drain_outbox(self.clock())
        self.assertEqual([name for name, _ in self.github.calls], ["update_ref"])


class StatusSnapshot(ControllerCase):
    def hold_lock(self):
        """Hold the controller lock from another thread until the returned event is set."""
        held = threading.Event()
        release = threading.Event()

        def hold():
            with self.controller._lock:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        return release

    def test_status_serves_the_last_snapshot_while_a_tick_holds_the_lock(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        release = self.hold_lock()
        started = time.monotonic()
        status = self.controller.status(wait=0.2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual((status["snapshot"], status["head"]["size"]), (True, 1), "the snapshot the tick took")
        self.assertGreaterEqual(status["stale_seconds"], 0)
        json.dumps(status)
        self.controller._status_snapshot = None
        status = self.controller.status(wait=0.2)
        self.assertEqual((status["snapshot"], status["stale_seconds"], status["schema"]), (True, None, STATUS_SCHEMA))
        release.set()
        live = self.controller.status()
        self.assertNotIn("snapshot", live, "with the lock free, status is read live")
        self.assertEqual(live["head"]["size"], 1)

    def test_operator_actions_refresh_the_snapshot(self):
        self.controller.tick()
        self.approve(101, {"A.lean": "a\n"})
        self.controller.set_setting("insertion_age_window", 4, "t")
        self.hold_lock()
        status = self.controller.status(wait=0.2)
        self.assertEqual((status["waiting"]["count"], status["settings"]["insertion_age_window"]), (1, 4))


if __name__ == "__main__":
    unittest.main()
