#!/usr/bin/env python3
"""The queue controller's unit behaviours: settings, approval intake, events, the draw, tasks, holds, comments, status.

The end-to-end flows are in tests/queue_fixture.py; this file checks the controller's individual
decisions against the fake GitHub without driving whole head cycles.
"""

import dataclasses
import json
import sqlite3
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
    LOST_IN_PROGRESS,
    MERGED_TITLE_PREFIX,
    STATUS_SCHEMA,
    VERDICT_CONTEXTS,
    ControllerError,
    ControllerLock,
    TaskError,
)
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller, reopen_controller  # noqa: E402
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


class DeclarationNames(ControllerCase):
    def test_declaration_fetch_is_background_head_bound_retry_bounded_and_in_task(self):
        from tauceti_queue import declaration_names as dn

        head, _ = self.approve(101, {"TauCeti/A.lean": "theorem one : True := trivial\n"})
        record = dn.validate(
            dict(version=1, head=head, base=head, tested=head, complete=True, added={"TauCeti.A": ["one"]}), head
        )
        entered, release = threading.Event(), threading.Event()
        original = self.github.declaration_names

        def slow(repo, accepted):
            entered.set()
            release.wait(5)
            return record

        self.github.declaration_names = slow
        with self.controller._lock:
            self.assertFalse(self.controller._fetch_declaration_names(self.clock()))
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.controller.store.row("inputs", {"number": 101})["declaration_names"], "{}")
        # Admission and sealing proceed while the read is blocked.
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"state": "head"})["version"], 1)
        future = self.controller._declaration_fetches[head]
        release.set()
        future.result(timeout=2)
        with self.controller._lock:
            self.assertTrue(self.controller._fetch_declaration_names(self.clock()))
        self.assertEqual(
            loads(self.controller.store.row("inputs", {"number": 101})["declaration_names"], {})["head"], head
        )
        # New tasks include evidence fetched after insertion.
        batch = self.controller.store.row("batches", {"state": "head"})
        taskid = self.controller._create_task(
            batch, "repair", batch["base"], [{"kind": "compiler", "message": "boom"}], self.clock()
        )
        taskrow = self.controller.store.row("tasks", {"id": taskid})
        # Task serialization uses the input, independently of the task creation time.
        task = self.controller._task_record(taskrow, batch, 1, "token", self.clock() + 60)
        self.assertEqual(task.members[0].declaration_names, record)
        self.assertEqual(task.to_dict()["members"][0]["declaration_names"], record)
        self.github.declaration_names = original

    def test_declaration_fetch_bounds_inflight_and_records_unknown_once(self):
        heads = [self.approve(n, {f"A{n}.lean": "one\n"})[0] for n in (101, 102, 103)]
        release = threading.Event()

        def slow(_repo, _head):
            release.wait(5)
            return {"state": "unknown", "reason": "record_missing"}

        self.github.declaration_names = slow
        try:
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            self.assertEqual(set(self.controller._declaration_fetches), set(heads[:2]))
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            self.assertEqual(len(self.controller._declaration_fetches), 2)
            release.set()
            for f in list(self.controller._declaration_fetches.values()):
                f.result(timeout=2)
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            self.assertEqual(set(self.controller._declaration_fetches), {heads[2]})
            self.controller._declaration_fetches[heads[2]].result(timeout=2)
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            self.assertFalse(self.controller._declaration_fetches)
            self.assertTrue(all(r["declaration_fetch_attempts"] == 1 for r in self.controller.store.rows("inputs")))
        finally:
            release.set()

    def test_declaration_late_result_does_not_update_a_changed_revision(self):
        head, _ = self.approve(101, {"A.lean": "one\n"})
        with self.controller._lock:
            self.controller._fetch_declaration_names(self.clock())
        future = self.controller._declaration_fetches[head]
        future.result(timeout=2)
        self.controller.store.update("inputs", {"number": 101}, state="cancelled", head_sha="f" * 40)
        with self.controller._lock:
            self.assertFalse(self.controller._fetch_declaration_names(self.clock()))
        self.assertEqual(self.controller.store.row("inputs", {"number": 101})["declaration_names"], "{}")

    def test_declaration_transport_errors_retry_three_times_and_missing_once(self):
        head, _ = self.approve(101, {"A.lean": "one\n"})
        self.github.fail_next["declaration_names"] = 3
        for _attempt in range(3):
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            future = self.controller._declaration_fetches[head]
            with self.assertRaises(RuntimeError):
                future.result(timeout=2)
            with self.controller._lock:
                self.controller._fetch_declaration_names(self.clock())
            self.clock.advance(300)
        row = self.controller.store.row("inputs", {"number": 101})
        self.assertEqual(row["declaration_fetch_attempts"], 3)
        self.assertEqual(loads(row["declaration_names"], {})["reason"], "fetch_failed")
        with self.controller._lock:
            self.controller._fetch_declaration_names(self.clock())
        self.assertFalse(self.controller._declaration_fetches)


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

    def setting_rows(self, key):
        return [e for e in self.controller.store.journal_entries("setting") if e["detail"]["key"] == key]

    def test_configuration_seeds_an_empty_store(self):
        self.assertIsNone(self.controller.store.setting_record("repair_concurrency"))
        self.assertTrue(self.controller.seed_setting("repair_concurrency", 3))
        record = self.controller.store.setting_record("repair_concurrency")
        self.assertEqual((record["value"], record["actor"]), (3, "configuration"))
        self.assertEqual(self.controller.setting("repair_concurrency"), 3)
        # A seed equal to the default records its provenance without a journal row.
        self.assertTrue(self.controller.seed_setting("stall_interval_seconds", 900))
        self.assertEqual(self.controller.store.setting_record("stall_interval_seconds")["actor"], "configuration")
        self.assertEqual(self.setting_rows("stall_interval_seconds"), [])
        with self.assertRaises(ControllerError):
            self.controller.seed_setting("nope", 1)

    def test_configuration_never_overwrites_an_operator_value(self):
        self.controller.seed_setting("repair_concurrency", 1)
        self.controller.set_setting("repair_concurrency", 2, "kim-em")
        rows = len(self.setting_rows("repair_concurrency"))
        for _ in range(3):
            self.assertFalse(self.controller.seed_setting("repair_concurrency", 1))
            self.assertFalse(self.controller.seed_setting("repair_concurrency", 4))
        record = self.controller.store.setting_record("repair_concurrency")
        self.assertEqual((record["value"], record["actor"]), (2, "kim-em"))
        self.assertEqual(len(self.setting_rows("repair_concurrency")), rows)
        # An operator choosing the value a seed already holds also takes ownership of it.
        self.controller.seed_setting("insertion_age_window", 16)
        self.controller.set_setting("insertion_age_window", 16, "kim-em")
        self.assertEqual(self.controller.store.setting_record("insertion_age_window")["actor"], "kim-em")
        self.assertFalse(self.controller.seed_setting("insertion_age_window", 8))
        self.assertEqual(self.controller.setting("insertion_age_window"), 16)

    def test_configuration_overwrites_its_own_stale_seed(self):
        self.controller.seed_setting("repair_concurrency", 1)
        self.assertFalse(self.controller.seed_setting("repair_concurrency", 1))
        self.assertTrue(self.controller.seed_setting("repair_concurrency", 2))
        record = self.controller.store.setting_record("repair_concurrency")
        self.assertEqual((record["value"], record["actor"]), (2, "configuration"))
        [entry] = self.setting_rows("repair_concurrency")
        self.assertEqual(entry["detail"], {"key": "repair_concurrency", "value": 2, "actor": "configuration"})

    def test_unchanged_value_is_not_journaled_and_does_not_wake(self):
        self.controller.set_setting("repair_concurrency", 2, "kim-em")
        rows = len(self.controller.store.journal_entries("setting"))
        wake = self.controller.wake_event()
        wake.clear()
        for actor in ("kim-em", "configuration", "someone-else"):
            self.controller.set_setting("repair_concurrency", 2, actor)
        self.controller.set_setting("publication_enabled", True, "operator")
        self.assertFalse(wake.is_set())
        self.assertEqual(len(self.controller.store.journal_entries("setting")), rows)
        self.assertEqual(self.controller.store.setting_record("repair_concurrency")["actor"], "kim-em")
        self.controller.set_setting("repair_concurrency", 3, "kim-em")
        self.assertTrue(wake.is_set())
        self.assertEqual(len(self.controller.store.journal_entries("setting")), rows + 1)

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


# The `ci_attempts` table as created before `repair_outputs_revs` was declared.
PRE_REPAIR_OUTPUTS_CI_ATTEMPTS = """
CREATE TABLE ci_attempts (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    batch_version INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    dispatch_id TEXT NOT NULL DEFAULT '',
    dispatched_at REAL NOT NULL,
    run_id TEXT,
    run_attempt INTEGER,
    run_status TEXT NOT NULL DEFAULT '',
    run_conclusion TEXT,
    run_created_at REAL,
    run_started_at REAL,
    run_completed_at REAL,
    run_updated_at REAL,
    runner_name TEXT,
    runner_labels TEXT NOT NULL DEFAULT '[]',
    host TEXT,
    phases TEXT NOT NULL DEFAULT '[]',
    html_url TEXT NOT NULL DEFAULT '',
    last_progress_at REAL,
    last_observed_at REAL,
    outcome TEXT NOT NULL DEFAULT 'pending',
    outcome_detail TEXT NOT NULL DEFAULT '',
    diagnostics TEXT NOT NULL DEFAULT '[]',
    infrastructure_retry INTEGER NOT NULL DEFAULT 0,
    needs_observation INTEGER NOT NULL DEFAULT 1,
    verdicts TEXT NOT NULL DEFAULT '{}'
);
"""

# Columns declared on `ci_attempts` for runs the controller cancels.
CANCEL_COLUMNS = ("cancelled_at", "cancelled_by", "cancel_reason", "cancel_result")

# Columns declared on `batches` and `tasks` after those tables existed.
PRE_REBASED_CHAIN_COLUMNS = (("batches", "pre_rebase_prepared"), ("tasks", "pre_rebase_source"))


class OlderStore(ControllerCase):
    def test_older_tables_gain_their_declared_columns_and_status_reads_the_existing_attempt(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"]})
        self.controller.store.close()
        # Rebuild `ci_attempts` at the older schema, keeping the dispatched attempt.
        connection = sqlite3.connect(str(self.root / "queue.sqlite"))
        columns = [row[1] for row in connection.execute("PRAGMA table_info(ci_attempts)")]
        for column in ("repair_outputs_revs", *CANCEL_COLUMNS):
            columns.remove(column)
        listed = ", ".join(columns)
        connection.executescript(
            "ALTER TABLE ci_attempts RENAME TO ci_attempts_current;"
            + PRE_REPAIR_OUTPUTS_CI_ATTEMPTS
            + f"INSERT INTO ci_attempts ({listed}) SELECT {listed} FROM ci_attempts_current;"
            + "DROP TABLE ci_attempts_current;"
        )
        # And `batches` and `tasks` as created before their pre-rebased chain columns were declared.
        for table, column in PRE_REBASED_CHAIN_COLUMNS:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.close()
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        present = {row["name"] for row in restarted.store.execute("PRAGMA table_info(ci_attempts)")}
        for column in ("repair_outputs_revs", *CANCEL_COLUMNS):
            self.assertIn(column, present)
        for table, column in PRE_REBASED_CHAIN_COLUMNS:
            present = {row["name"] for row in restarted.store.execute(f"PRAGMA table_info({table})")}
            self.assertIn(column, present, table)
        ci = restarted.status()["head"]["ci"]
        self.assertEqual(ci["attempt_id"], attempt["id"])
        self.assertEqual(ci["repair_outputs_revs"], [])
        self.assertEqual(restarted.status()["head"]["cancelled_runs"], [])


class Dispatch(ControllerCase):
    def test_a_head_without_history_sends_only_its_base_as_output_candidate(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        payload = self.github.dispatches[-1]["client_payload"]
        self.assertEqual(payload["repair_outputs_revs"], [head["base"]])
        ci = self.controller.status()["head"]["ci"]
        self.assertEqual(ci["repair_outputs_revs"], [head["base"]])

    def test_a_rebased_head_sends_its_previous_tip_first_then_the_base(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        before = self.controller.store.row("batches", {"state": "head"})
        canonical = self.repo.advance_main({"Z.lean": "z\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual((head["version"], head["base"]), (before["version"] + 1, canonical))
        self.assertNotEqual(head["tip"], before["tip"])
        self.assertEqual(self.github.dispatches[-1]["client_payload"]["head_sha"], head["tip"])
        revs = self.github.dispatches[-1]["client_payload"]["repair_outputs_revs"]
        self.assertEqual(revs, [before["tip"], canonical])
        self.assertEqual(self.controller.status()["head"]["ci"]["repair_outputs_revs"], revs)
        self.assertEqual(self.controller.store.journal_entries("dispatched")[-1]["detail"]["repair_outputs_revs"], revs)

    def test_output_candidates_are_bounded_and_never_repeat_or_name_the_head(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.controller.store.row("batches", {"state": "head"})
        base, tip = head["base"], head["tip"]
        # Earlier versions, one of them back at the current tip and one at the base, and later
        # versions that are not previous tips at all.
        recorded = ["1" * 40, "2" * 40, tip, "2" * 40, base, "3" * 40, "4" * 40, "5" * 40]
        version = head["version"] + 1
        for offset, sha in enumerate(recorded):
            self.controller._record_tip(head["id"], version + offset, sha, self.clock())
        current = len(recorded) - 1
        head = dict(head, version=version + current, tip="6" * 40)
        self.controller._canonical = "7" * 40
        revs = self.controller._repair_outputs_revs(head)
        self.assertEqual(revs, ["4" * 40, "3" * 40, base, "2" * 40, "7" * 40])
        self.assertEqual(len(revs), len(set(revs)))
        self.assertNotIn(head["tip"], revs)
        # With the head back at its base and main there too, the base is listed once, as a previous tip.
        self.controller._canonical = base
        revs = self.controller._repair_outputs_revs(dict(head, tip="6" * 40))
        self.assertEqual(revs, ["4" * 40, "3" * 40, base, "2" * 40])

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


class LostRuns(ControllerCase):
    """A run GitHub still reports queued or in progress is never lost, and is re-adopted rather than duplicated."""

    def setUp(self):
        super().setUp()
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()  # dispatch
        self.head = self.controller.store.row("batches", {"state": "head"})

    def attempt(self):
        return self.controller.store.row("ci_attempts", {"batch_id": self.head["id"]}, order="id DESC")

    def observe(self, seconds=0):
        self.clock.advance(seconds or self.controller.setting("polling_interval_seconds") + 1)
        return self.controller.tick()

    def test_default_lost_run_seconds_is_four_hours(self):
        self.assertEqual(DEFAULT_SETTINGS["lost_run_seconds"], 14400)

    def test_in_progress_run_building_for_an_hour_without_job_updates_is_not_lost(self):
        # The incident: one long `lake build` step, no phase change for an hour, GitHub says in_progress.
        self.github.start_runs()
        self.observe()
        reasons = self.observe(3601)
        attempt = self.attempt()
        self.assertEqual(attempt["outcome"], "pending", attempt["outcome_detail"])
        self.assertEqual(len(self.github.dispatches), 1, "no duplicate run is dispatched")
        self.assertFalse(any(r.startswith("stall:") for r in reasons), "under lost_run_seconds: not even a stall line")

    def test_in_progress_run_without_job_updates_is_a_stall_while_github_still_updates_the_run(self):
        self.github.start_runs()
        self.observe()
        threshold = self.controller.setting("lost_run_seconds")
        listed = self.github.read_run

        def fresh(repo, run_id, **kwargs):
            run = listed(repo, run_id, **kwargs)
            return dataclasses.replace(run, updated_at=self.clock() - 60)

        self.github.read_run = fresh
        reasons = self.observe(threshold + 1)
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertTrue(any(r.startswith("stall:") and "not treated as lost" in r for r in reasons), reasons)
        self.assertEqual(len(self.github.dispatches), 1)

    def test_updated_at_moving_advances_last_progress(self):
        self.github.start_runs()
        self.observe()
        threshold = self.controller.setting("lost_run_seconds")
        before = self.attempt()["last_progress_at"]
        for _ in range(3):
            self.clock.advance(threshold - 100)
            self.github.runs[0].updated_at = self.clock()
            self.observe()
        attempt = self.attempt()
        self.assertEqual(attempt["outcome"], "pending", attempt["outcome_detail"])
        self.assertGreater(attempt["last_progress_at"], before + 2 * threshold)
        self.assertEqual(self.controller.status()["head"]["ci"]["updated_at"], self.github.runs[0].updated_at)

    def test_queued_run_is_never_lost(self):
        self.observe()
        reasons = self.observe(3 * self.controller.setting("lost_run_seconds"))
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertTrue(any(r.startswith("stall:") and "queued" in r for r in reasons), reasons)
        self.assertEqual(len(self.github.dispatches), 1)

    def test_in_progress_run_stale_in_a_direct_read_is_lost_but_readopted_while_github_reports_it_live(self):
        self.github.start_runs()
        self.observe()
        self.observe(self.controller.setting("lost_run_seconds") + 1)
        attempt = self.attempt()
        self.assertEqual(attempt["outcome"], "infrastructure")
        self.assertTrue(attempt["outcome_detail"].startswith(LOST_IN_PROGRESS), attempt["outcome_detail"])
        self.assertIn(("read_run", (attempt["run_id"],)), self.github.calls, "the run was read directly first")
        self.observe(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
        self.assertEqual(len(self.github.dispatches), 1, "a dispatch would cancel the live run")
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertEqual(
            self.controller.store.journal_entries("run-readopted")[-1]["detail"]["run_id"], attempt["run_id"]
        )

    def test_no_second_dispatch_for_a_tip_while_its_run_is_live(self):
        # pr-build.yml's concurrency group cancels the older staging run when a second dispatch for the
        # same candidate starts (the fake models it): the live run must survive every re-dispatch path.
        self.github.start_runs()
        self.observe()
        first = self.attempt()
        for outcome, detail in (
            ("infrastructure", "lost-run: no progress for 3600s"),
            ("infrastructure", "no-run: the dispatch produced no run"),
            ("superseded", ""),
        ):
            with self.subTest(outcome=outcome, detail=detail):
                self.controller.store.update("ci_attempts", {"id": first["id"]}, outcome=outcome, outcome_detail=detail)
                self.observe(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
                self.assertEqual(len(self.github.dispatches), 1)
                self.assertEqual(self.github.runs[0].status, "in_progress", "the live run was not cancelled")
                self.assertEqual(self.attempt()["id"], first["id"])
                self.assertEqual(self.attempt()["outcome"], "pending")
        # Once the run has really finished without a verdict, the re-dispatch goes ahead.
        self.github.runs[0].status, self.github.runs[0].conclusion = "completed", "cancelled"
        self.observe()
        self.assertEqual(self.attempt()["outcome_detail"], "run-cancelled")
        self.observe(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
        self.assertEqual(len(self.github.dispatches), 2)

    def test_retry_ci_refuses_while_a_run_for_the_tip_is_live(self):
        self.github.start_runs()
        self.observe()
        run_id = self.attempt()["run_id"]
        self.controller.store.update(
            "ci_attempts", {"id": self.attempt()["id"]}, outcome="infrastructure", outcome_detail="lost-run: old"
        )
        result = self.controller.retry_ci(PROJECT, "operator")
        self.assertEqual((result["retried"], result["reason"], result["run_id"]), (False, "live-run", run_id))
        self.assertIn("would cancel it", result["detail"])
        self.assertEqual(self.attempt()["outcome"], "infrastructure", "nothing was forgotten")
        self.github.complete_runs()
        self.assertTrue(self.controller.retry_ci(PROJECT, "operator")["retried"])

    def test_vanished_run_is_lost(self):
        self.observe()
        self.github.runs[0].vanished = True
        self.observe()
        attempt = self.attempt()
        self.assertEqual(attempt["outcome"], "infrastructure")
        self.assertEqual(attempt["outcome_detail"], f"lost-run: run {attempt['run_id']} is no longer on GitHub")

    def test_live_run_is_readopted_instead_of_redispatched(self):
        # An attempt declared lost by the old rule while its run was still building: the next
        # re-dispatch finds the run in progress and binds the attempt to it again.
        self.github.start_runs()
        self.observe()
        attempt = self.attempt()
        self.controller.store.update(
            "ci_attempts",
            {"id": attempt["id"]},
            outcome="infrastructure",
            outcome_detail="lost-run: no progress for 3600s",
        )
        reasons = self.observe(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
        self.assertEqual(len(self.github.dispatches), 1, "no duplicate run")
        readopted = self.attempt()
        self.assertEqual(readopted["id"], attempt["id"], "the same attempt, so the staging branch is unchanged")
        self.assertEqual((readopted["outcome"], readopted["run_id"]), ("pending", attempt["run_id"]))
        entry = self.controller.store.journal_entries("run-readopted")[-1]
        self.assertEqual(entry["detail"]["run_id"], attempt["run_id"])
        self.assertTrue(any("re-adopted live CI run" in r for r in reasons), reasons)
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.head["tip"], "context": context})
        self.controller.tick()
        self.assertEqual(self.attempt()["outcome"], "pass")
        self.assertEqual(self.repo.main_sha(), self.head["tip"])

    def test_run_found_after_the_dispatch_timeout_is_readopted(self):
        self.github.runs[0].vanished = True  # GitHub lists nothing yet
        self.observe(self.controller.setting("dispatch_timeout_seconds") + 1)
        self.assertTrue(self.attempt()["outcome_detail"].startswith("no-run"))
        self.github.runs[0].vanished = False
        self.observe(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertEqual(self.attempt()["run_id"], self.github.runs[0].run_id)


class SupersededRuns(ControllerCase):
    """A run left verifying a candidate the queue no longer uses is cancelled by the controller."""

    def setUp(self):
        super().setUp()
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()  # dispatch
        self.head = self.controller.store.row("batches", {"state": "head"})

    def attempts(self):
        return self.controller.store.rows("ci_attempts", {"batch_id": self.head["id"]}, order="id")

    def observe(self, seconds=0):
        self.clock.advance(seconds or self.controller.setting("polling_interval_seconds") + 1)
        return self.controller.tick()

    def replay(self):
        """Bind the first attempt to its running run, then move main so the head is replayed and dispatched again."""
        self.github.start_runs()
        self.observe()
        self.assertEqual(self.attempts()[0]["run_status"], "in_progress")
        self.repo.advance_main({"Z.lean": "z\n"})
        self.observe()
        first, second = self.attempts()
        self.assertEqual(second["batch_version"], first["batch_version"] + 1)
        return first, second

    def cancel_calls(self):
        return [args for name, args in self.github.calls if name == "cancel_run"]

    def test_dispatch_after_replay_cancels_the_previous_live_run(self):
        first, second = self.replay()
        run = self.github.runs[0]
        self.assertEqual((run.status, run.conclusion), ("completed", "cancelled"))
        self.assertEqual(self.github.runs[1].status, "queued", "the new run is left alone")
        self.assertEqual((first["outcome"], first["cancelled_by"]), ("superseded", second["id"]))
        self.assertEqual(first["cancel_result"], "cancelled")
        self.assertEqual(second["outcome"], "pending")
        entry = self.controller.store.journal_entries("ci-cancelled")[-1]["detail"]
        self.assertEqual(
            (entry["attempt_id"], entry["run_id"], entry["superseded_by"]), (first["id"], run.run_id, second["id"])
        )
        listed = self.controller.status()["head"]["cancelled_runs"]
        self.assertEqual(
            [(c["attempt_id"], c["run_id"], c["superseded_by"]) for c in listed],
            [(first["id"], run.run_id, second["id"])],
        )
        self.assertEqual(self.controller.status()["head"]["ci"]["attempt_id"], second["id"])

    def test_no_cancel_when_the_previous_run_has_completed(self):
        self.github.start_runs()
        self.observe()
        run = self.github.runs[0]
        run.status, run.conclusion, run.completed_at = "completed", "cancelled", self.clock()
        self.observe()
        self.assertEqual(self.attempts()[0]["run_status"], "completed")
        self.repo.advance_main({"Z.lean": "z\n"})
        self.observe()
        self.assertEqual(len(self.attempts()), 2)
        self.assertEqual(self.cancel_calls(), [])
        self.assertEqual(self.controller.store.journal_entries("ci-cancelled"), [])
        self.assertIsNone(self.attempts()[0]["cancelled_at"])
        self.assertEqual(self.controller.status()["head"]["cancelled_runs"], [])

    def test_a_failed_cancel_never_blocks_the_dispatch_and_is_retried_once(self):
        self.github.fail_next["cancel_run"] = 5
        first, second = self.replay()
        self.assertEqual(len(self.github.dispatches), 2, "the new attempt was dispatched")
        self.assertEqual(second["dispatch_id"], "dispatch-2")
        self.assertEqual(first["outcome"], "superseded")
        self.assertTrue(first["cancel_result"].startswith("failed:"), first["cancel_result"])
        for _ in range(4):
            self.observe(self.controller.setting("outbox_retry_max_seconds") + 1)
        self.assertEqual(len(self.cancel_calls()), 2, "one inline call and one retry from the outbox")
        entry = self.controller.store.row("outbox", {"kind": "cancel_run"})
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["result"].startswith("gave-up:"), entry["result"])
        self.assertEqual(self.attempts()[1]["outcome"], "pending")

    def test_a_failed_cancel_succeeds_on_its_outbox_retry(self):
        self.github.fail_next["cancel_run"] = 1
        self.replay()
        self.observe()
        self.assertEqual(self.github.runs[0].conclusion, "cancelled")
        self.assertEqual(self.controller.store.row("outbox", {"kind": "cancel_run"})["result"], "cancelled")

    def test_a_landed_batch_cancels_a_straggler(self):
        # A controller unable to cancel at dispatch (as before this was added) leaves the old run going.
        self.github.cancel_run = None
        first, second = self.replay()
        del self.github.cancel_run
        self.assertEqual(self.github.runs[0].status, "in_progress")
        self.assertIsNone(first["cancelled_at"])
        head = self.controller.store.row("batches", {"id": self.head["id"]})
        self.github.runs[0].verdict = "lost"  # `complete_runs` leaves it running
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": head["tip"], "context": context})
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        straggler = self.attempts()[0]
        self.assertEqual(self.github.runs[0].conclusion, "cancelled")
        self.assertEqual((straggler["cancel_reason"], straggler["cancelled_by"]), ("batch landed", None))
        self.assertEqual(self.attempts()[1]["outcome"], "pass")

    def test_rebuilding_the_head_cancels_its_live_run(self):
        self.github.start_runs()
        self.observe()
        result = self.controller.rebuild_head(PROJECT, "kim-em")
        self.assertTrue(result["rebuilt"], result)
        self.assertEqual(self.github.runs[0].conclusion, "cancelled")
        self.assertEqual(self.attempts()[0]["cancel_reason"], f"head rebuilt as version {result['version']}")

    def test_the_lost_run_detector_ignores_a_superseded_attempt(self):
        # GitHub accepts the cancel but the run lingers in progress, silent.
        lingering = []
        self.github.cancel_run = lambda repo, run_id: lingering.append(run_id) or "cancelled"
        first, second = self.replay()
        self.assertEqual(lingering, [first["run_id"]])
        self.assertEqual(self.github.runs[0].status, "in_progress")
        self.github.start_runs()
        self.observe()
        self.observe(self.controller.setting("lost_run_seconds") + 1)
        first, second = self.attempts()
        self.assertEqual(first["outcome"], "superseded")
        verdicts = [e["detail"]["attempt_id"] for e in self.controller.store.journal_entries("ci-infrastructure")]
        self.assertNotIn(first["id"], verdicts)
        self.assertEqual(self.controller.status()["head"]["cancelled_runs"][0]["attempt_id"], first["id"])
        # Nor is it re-adopted as the live run for the tip it built.
        head = self.controller.store.row("batches", {"id": self.head["id"]})
        self.assertIsNone(self.controller._live_run(dict(head, tip=first["head_sha"], base=first["base_sha"]), [first]))


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
        self.assertEqual(
            background["active"], {"kind": "prepare", "task_id": prepare["id"], "claimed": False, "chain": "own_base"}
        )
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

    def test_a_pre_rebase_task_names_the_heads_tips_then_its_base_to_warm_from(self):
        self.conflicting_batch()
        batch = self.open_batch()
        head = self.controller.store.row("batches", {"state": "head"})
        # Earlier tips of the head, oldest first; one repeats and the current tip is never listed twice.
        earlier = ["1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40, "5" * 40]
        self.controller.store.execute("DELETE FROM batch_tips WHERE batch_id = ?", (head["id"],))
        for version, sha in enumerate(earlier):
            self.controller._record_tip(head["id"], version, sha, self.clock())
        self.controller._record_tip(head["id"], len(earlier), head["tip"], self.clock())
        self.controller.store.update("batches", {"id": head["id"]}, version=len(earlier))
        task = self.controller.claim_task("w", "admin-worker")
        self.assertEqual((task.kind, task.onto, task.batch_id), ("pre_rebase", head["tip"], batch["id"]))
        expected = [head["tip"], "5" * 40, "4" * 40, "2" * 40, "3" * 40, head["base"]]
        self.assertEqual(task.warm_revs, expected)
        self.assertEqual(task.to_dict()["warm_revs"], expected)

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
    def test_the_heads_repair_task_warms_from_its_tips_then_its_base_and_main(self):
        head = self.failing_head()
        # Earlier tips of the head, oldest first; one repeats and the current tip is never listed twice.
        earlier = ["1" * 40, "2" * 40, "3" * 40, "2" * 40, "4" * 40, "5" * 40]
        self.controller.store.execute("DELETE FROM batch_tips WHERE batch_id = ?", (head["id"],))
        for version, sha in enumerate(earlier):
            self.controller._record_tip(head["id"], version, sha, self.clock())
        self.controller._record_tip(head["id"], len(earlier), head["tip"], self.clock())
        self.controller.store.update("batches", {"id": head["id"]}, version=len(earlier))
        self.controller.store.update("tasks", {"batch_id": head["id"], "state": "open"}, batch_version=len(earlier))
        self.controller._canonical = "c" * 40
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((task.kind, task.tip), ("repair", head["tip"]))
        expected = [head["tip"], "5" * 40, "4" * 40, "2" * 40, "3" * 40, head["base"], "c" * 40]
        self.assertEqual(task.warm_revs, expected)
        self.assertEqual(task.to_dict()["warm_revs"], expected)

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

    def test_admin_answers_survive_a_restart_during_an_outage(self):
        self.failing_head()
        self.assertIsNotNone(self.controller.claim_task("worker-1", "admin-worker"))
        self.assertIsNone(self.controller.claim_task("worker-3", "stranger"))
        rows = {row["key"]: row["value"] for row in self.controller.store.cached_identities("admin")}
        self.assertEqual(rows, {"admin-worker": True, "stranger": False})
        # Restarted seven hours on (the claim and every fresh answer expired) with GitHub rate limited.
        self.clock.advance(7 * 3600)
        self.github.unavailable = "rate limited for 2400s"
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        self.assertIsNotNone(restarted.claim_task("worker-2", "admin-worker"), "the stored admin answer stands")
        self.assertIsNone(restarted.claim_task("worker-3", "stranger"))
        self.assertEqual(restarted.store.journal_entries("claim-refused")[-1]["detail"]["reason"], "not-admin")
        # An answer GitHub has not confirmed for the maximum age is not served.
        self.clock.advance(8 * 86400)
        again = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(again.store.close)
        self.assertIsNone(again._repository_admin("admin-worker"))
        self.assertEqual(again.store.cached_identities("admin"), [])

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

    def test_idle_capacity_never_duplicates_a_held_head_task(self):
        self.controller.set_setting("repair_concurrency", 8, "t")
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
        self.assertIsNotNone(task_one)
        self.assertIsNone(two.claim(self.controller))
        self.assertIsNone(one.claim(self.controller), "a repeated claim request cannot duplicate its own work")
        self.assertEqual(len(self.controller._active_task_claims()), 1)
        tip_one = one.produce(task_one)
        self.assertTrue(self.controller.submit_task(task_one.task_id, task_one.claim_token, tip_one).accepted)
        self.assertEqual(
            self.repo.file_at(self.controller.store.row("batches", {"state": "head"})["tip"], "A.lean"), "one\n"
        )

    def test_simultaneous_claim_requests_assign_one_owner_with_spare_capacity(self):
        self.controller.set_setting("repair_concurrency", 8, "t")
        self.failing_head()
        barrier = threading.Barrier(2)
        admin = self.controller._repository_admin

        def together(login):
            allowed = admin(login)
            barrier.wait(timeout=5)
            return allowed

        self.controller._repository_admin = together
        results, errors = [], []

        def claim(worker):
            try:
                results.append(self.controller.claim_task(worker, "admin-worker"))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=claim, args=(worker,)) for worker in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(task is not None for task in results), 1)
        self.assertEqual(len(self.controller._active_task_claims()), 1)
        [task] = self.controller.store.rows("tasks", {"state": "open"})
        self.assertEqual(task["claims"], 1)
        self.assertEqual(len(self.controller.store.journal_entries("claimed")), 1)

    def test_restart_preserves_owner_and_renewal_then_release_or_expiry_allows_replacement(self):
        self.controller.set_setting("repair_concurrency", 8, "t")
        self.failing_head()
        first = self.controller.claim_task("one", "admin-worker")
        self.controller.store.close()
        self.controller = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.assertIsNone(self.controller.claim_task("two", "admin-worker"))
        self.clock.advance(self.controller.setting("claim_ttl_seconds") - 1)
        renewed = self.controller.renew_task(first.task_id, first.claim_token)
        self.clock.advance(2)
        self.assertIsNone(self.controller.claim_task("two", "admin-worker"), "renewed owner still holds the task")
        self.assertGreater(renewed, self.clock())
        self.controller.release_task(first.task_id, first.claim_token, "worker-stopped")
        second = self.controller.claim_task("two", "admin-worker")
        self.assertEqual(second.task_id, first.task_id)
        self.assertNotEqual(second.claim_token, first.claim_token)
        with self.assertRaisesRegex(TaskError, "claim-superseded"):
            self.controller.renew_task(first.task_id, first.claim_token)
        self.clock.advance(self.controller.setting("claim_ttl_seconds"))
        third = self.controller.claim_task("three", "admin-worker")
        self.assertEqual(third.task_id, first.task_id)
        self.assertNotEqual(third.claim_token, second.claim_token)
        self.assertEqual(len(self.controller._active_task_claims()), 1)
        with self.assertRaisesRegex(TaskError, "claim-expired"):
            self.controller.renew_task(second.task_id, second.claim_token)

    def test_replacing_a_task_cannot_assign_another_worker_to_its_held_batch(self):
        self.controller.set_setting("repair_concurrency", 8, "t")
        head = self.failing_head()
        first = self.controller.claim_task("one", "admin-worker")
        replacement = self.controller._create_task(head, "repair", head["base"], [], self.clock())
        self.assertNotEqual(replacement, first.task_id)
        self.assertIsNone(self.controller.claim_task("two", "admin-worker"), "ownership is per batch, not task id")
        self.controller.release_task(first.task_id, first.claim_token, "worker-stopped")
        self.assertEqual(self.controller.claim_task("two", "admin-worker").task_id, replacement)


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
        """The own-base preparations (`PreRebasedPreparation` covers those of the pre-rebased chain)."""
        return self.controller.store.rows(
            "tasks", {"kind": "prepare", "state": state, "pre_rebase_source": None}, order="id"
        )

    def quiet(self):
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.controller.tick()

    def land_head(self):
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[-1].head_sha, "context": context})
        self.controller.tick()  # observes the verdict and promotes
        self.controller.tick()  # seals the next head

    def test_a_prepare_task_names_the_batchs_previous_tips_then_its_base_to_warm_from(self):
        batch = self.open_batch()
        tips = [
            row["tip"]
            for row in self.controller.store.rows("batch_tips", {"batch_id": batch["id"]}, order="version DESC")
        ]
        self.assertEqual(tips[0], batch["tip"])
        self.assertGreaterEqual(len(tips), 3, "one tip per member inserted")
        self.quiet()
        [row] = self.prepare_tasks()
        task = self.controller.claim_task("w", "admin-worker")
        self.assertEqual((task.task_id, task.kind), (row["id"], "prepare"))
        self.assertEqual(task.warm_revs, [*tips[1:5], batch["base"]])
        self.assertNotIn(batch["tip"], task.warm_revs)

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
        self.assertEqual((second.kind, second.batch_id), ("prepare", batch["id"]))
        third = self.controller.claim_task("worker-3", "admin-worker")
        self.assertIsNone(third, "unused capacity cannot duplicate either batch's work")
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

    def test_a_landed_heads_tips_are_candidates_for_the_next_head_and_its_preparations(self):
        """Production batch 3: head 2 landed as a new `main` commit the public cache did not yet
        hold, and its published tip was the nearest tree for the next head and the batches behind it."""
        first = self.open_batch()
        landed = self.controller.store.row("batches", {"state": "head"})
        self.land_head()
        landed = self.controller.store.row("batches", {"id": landed["id"]})
        self.assertEqual(landed["state"], "landed")
        # The fixture lands by fast-forward; in production the landed tip and the `main` commit differ.
        published, earlier = "a" * 40, "b" * 40
        self.controller._record_tip(landed["id"], landed["version"] + 2, published, self.clock())
        self.controller._record_tip(landed["id"], landed["version"] + 1, earlier, self.clock())
        landed_tips = [
            row["tip"]
            for row in self.controller.store.rows("batch_tips", {"batch_id": landed["id"]}, order="version DESC")
        ]
        self.assertEqual(landed_tips[:2], [published, earlier])
        head = self.controller.store.row("batches", {"state": "head"})
        self.assertEqual(head["id"], first["id"])
        revs = self.controller._repair_outputs_revs(head)
        own = self.controller._previous_tips(head)
        expected: list[str] = []
        for sha in [*own, *landed_tips, head["base"], self.controller._canonical]:
            if sha and sha != head["tip"] and sha not in expected:
                expected.append(sha)
        self.assertEqual(revs, expected, "the head's tips, then the landed head's, then the base and main")
        self.assertLess(revs.index(earlier), revs.index(head["base"]))
        batch = self.open_batch_behind((401, 402, 403))
        tips = [
            row["tip"]
            for row in self.controller.store.rows("batch_tips", {"batch_id": batch["id"]}, order="version DESC")
        ]
        self.quiet()
        [row] = self.prepare_tasks()
        task = self.controller.claim_task("w", "admin-worker")
        self.assertEqual((task.task_id, task.kind), (row["id"], "prepare"))
        expected = []
        for sha in [*tips[1:5], *landed_tips, batch["base"]]:
            if sha not in expected:
                expected.append(sha)
        self.assertEqual(task.warm_revs, expected, "the batch's tips, then the landed head's, then the base")
        self.assertEqual(task.warm_revs[-3:], [published, earlier, batch["base"]])

    def open_batch_behind(self, numbers):
        """An open batch of `numbers` behind the current head."""
        for number in numbers:
            self.approve(number, {f"P{number}.lean": f"{number}\n"})
        self.controller.tick()
        return self.controller.store.row("batches", {"state": "open"})

    def test_sealing_waits_for_prepared_edits_and_the_owner_can_renew_after_main_advances(self):
        batch = self.open_batch()
        self.quiet()
        agent = ScriptedAgent(
            "preparer",
            "admin-worker",
            self.repo,
            self.root / "preparer",
            lambda _: {f"{PROJECT}#202": Resolution({"P202.lean": "fixed\n"}, explanation="Fixed P202.")},
        )
        preparation = agent.claim(self.controller)
        prepared_tip = agent.produce(preparation)
        self.land_head()
        self.assertNotEqual(self.repo.main_sha(), preparation.base)
        self.assertIsNone(self.controller._head())
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "open")
        self.assertIn(f"open batch {batch['id']} is claimed; sealing waits for the release", self.controller.tick())
        self.controller.renew_task(preparation.task_id, preparation.claim_token)
        self.assertIsNone(self.controller.claim_task("second-worker", "admin-worker"))
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertNotEqual(self.controller.input_for(PROJECT, 301)["batch_id"], batch["id"])
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["version"], batch["version"])
        result = self.controller.submit_task(preparation.task_id, preparation.claim_token, prepared_tip)
        self.assertTrue(result.accepted, result)
        self.assertEqual(self.controller._open_batch()["prepared_version"], result.version)
        self.controller.tick()
        sealed = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual((sealed["state"], sealed["base"]), ("head", self.repo.main_sha()))
        self.assertEqual(self.repo.file_at(sealed["tip"], "P202.lean"), "fixed\n")
        member = next(m for m in self.controller.members(batch["id"]) if m["number"] == 202)
        self.assertEqual((member["provenance"], member["explanation"]), ("edited", "Fixed P202."))
        self.assertEqual(self.controller.store.row("tasks", {"id": preparation.task_id})["resolution"], "adopted")
        self.assertEqual(len(self.github.runs), 2, "the prepared result still needs the new head's CI")
        self.assertEqual(self.github.runs[-1].head_sha, sealed["tip"])

    def test_a_clean_preparation_release_unblocks_sealing(self):
        batch = self.open_batch()
        self.quiet()
        task = self.controller.claim_task("preparer", "admin-worker")
        self.land_head()
        self.assertIsNone(self.controller._head())
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], batch["id"])

    def test_a_worker_release_leaves_an_unclaimed_offer_that_can_seal(self):
        batch = self.open_batch()
        self.quiet()
        task = self.controller.claim_task("preparer", "admin-worker")
        self.land_head()
        self.controller.release_task(task.task_id, task.claim_token, "worker-stopped")
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], batch["id"])
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["resolution"], "sealed")

    def test_an_expired_preparation_claim_unblocks_sealing_and_rejects_late_results(self):
        batch = self.open_batch()
        self.quiet()
        task = self.controller.claim_task("preparer", "admin-worker")
        self.land_head()
        self.assertIsNone(self.controller._head())
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], batch["id"])
        result = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertEqual((result.accepted, result.reason), (False, "claim-expired"))

    def test_an_unclaimed_preparation_offer_does_not_delay_sealing(self):
        batch = self.open_batch()
        self.quiet()
        [task] = self.prepare_tasks()
        self.land_head()
        self.assertEqual(self.controller._head()["id"], batch["id"])
        self.assertEqual(self.controller.store.row("tasks", {"id": task["id"]})["resolution"], "sealed")

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


class PreRebasedPreparation(ControllerCase):
    """A `prepare` task of the next batch's pre-rebased chain on the head's tip, so the head it seals as
    reaches CI with published outputs for its exact tip."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 2, "t")  # room for one background claim
        self.controller.set_setting("prepare_min_members", 2, "t")
        self.controller.set_setting("prepare_quiet_seconds", 0, "t")

    def open_batch(self):
        """A head waiting on CI and an open batch of 201 and 202, prepared on its own base and pre-rebased
        cleanly onto the head's tip."""
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.approve(201, {"P201.lean": "201\n"})
        self.approve(202, {"P202.lean": "202\n"})
        self.controller.tick()
        batch = self.controller.store.row("batches", {"state": "open"})
        self.assertEqual(batch["pre_rebase_state"], "clean")
        own = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((own.kind, own.tip, own.base), ("prepare", batch["tip"], batch["base"]))
        self.assertNotIn("pre_rebased", own.extra, "the own-base preparation comes first")
        self.controller.release_task(own.task_id, own.claim_token, "prepared_clean")
        self.controller.tick()
        return self.controller.store.row("batches", {"id": batch["id"]})

    def head(self):
        return self.controller.store.row("batches", {"state": "head"})

    def pre_rebased_tasks(self, state="open"):
        return [
            t
            for t in self.controller.store.rows("tasks", {"kind": "prepare", "state": state}, order="id")
            if t["pre_rebase_source"]
        ]

    def preparation(self):
        return self.controller.status()["open_batches"][0]["pre_rebase"]

    def land_head(self):
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[-1].head_sha, "context": context})
        self.controller.tick()  # observes the verdict and promotes
        self.controller.tick()  # seals the next head

    def test_the_pre_rebased_preparation_warms_from_the_heads_tips_then_the_batchs_own(self):
        batch = self.open_batch()
        head = self.head()
        self.controller._record_tip(head["id"], head["version"] + 1, "1" * 40, self.clock())
        self.controller.store.update("batches", {"id": head["id"]}, version=head["version"] + 1, tip="1" * 40)
        self.controller._record_tip(head["id"], head["version"] + 2, head["tip"], self.clock())
        self.controller.store.update("batches", {"id": head["id"]}, version=head["version"] + 2, tip=head["tip"])
        own_tips = [
            row["tip"]
            for row in self.controller.store.rows("batch_tips", {"batch_id": batch["id"]}, order="version DESC")
        ]
        claimed = self.controller.claim_task("worker-1", "admin-worker")
        self.assertIn("pre_rebased", claimed.extra)
        self.assertEqual(claimed.warm_revs[:2], [head["tip"], "1" * 40], "the head's tip, then its previous tip")
        self.assertEqual(claimed.warm_revs[2], batch["tip"], "then the batch tip the pre-rebase covers")
        self.assertEqual(claimed.warm_revs[3:], [t for t in own_tips if t != batch["tip"]][:4])
        self.assertEqual(len(claimed.warm_revs), len(set(claimed.warm_revs)))

    def test_adopting_the_pre_rebase_creates_a_prepare_task_for_the_pre_rebased_chain(self):
        self.assertTrue(DEFAULT_SETTINGS["prepare_pre_rebased"])
        batch = self.open_batch()
        head = self.head()
        [task] = self.pre_rebased_tasks()
        self.assertEqual(
            (task["batch_id"], task["base"], task["tip"], task["onto"], task["pre_rebase_source"]),
            (batch["id"], head["tip"], batch["pre_rebase_tip"], head["tip"], batch["tip"]),
            "the replayed commits on the head's tip, not the batch's own chain",
        )
        self.assertEqual(self.controller.status()["counters"]["tasks_prepare_pre_rebased"], 1)
        ref = f"refs/tauceti-queue/batches/{batch['id']}/pre-rebased/{batch['pre_rebase_tip']}"
        self.assertEqual(self.repo.rev_parse(ref), batch["pre_rebase_tip"], "published for the worker to fetch")
        status = self.preparation()
        self.assertEqual(
            (status["preparation"], status["prepare_task_id"], status["prepare_claimed"]),
            ("preparing", task["id"], False),
        )
        background = self.controller.status()["open_batches"][0]["background"]
        self.assertEqual(
            background["active"], {"kind": "prepare", "task_id": task["id"], "claimed": False, "chain": "pre_rebased"}
        )
        claimed = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual((claimed.kind, claimed.task_id), ("prepare", task["id"]))
        self.assertEqual((claimed.base, claimed.tip, claimed.onto), (head["tip"], batch["pre_rebase_tip"], head["tip"]))
        self.assertEqual(
            claimed.extra["pre_rebased"],
            {
                "source": batch["tip"],
                "batch_base": batch["base"],
                "head_batch_id": head["id"],
                "head_base": head["base"],
            },
        )
        self.assertIn(ref, claimed.retained_refs)
        self.assertEqual(claimed.extra["renew_after"], 60)
        self.assertEqual([m.number for m in claimed.members], [201, 202])
        self.assertIsNone(self.controller.claim_task("worker-2", "admin-worker"), "the last slot is the head's")
        self.assertTrue(self.preparation()["prepare_claimed"])
        self.controller.tick()
        self.assertEqual(len(self.pre_rebased_tasks()), 1, "one background task per batch")

    def test_clean_pre_rebase_preparation_gets_compiled_cross_batch_collisions(self):
        from unittest.mock import patch

        fetching = patch.object(self.controller, "_fetch_declaration_names", return_value=False)
        fetching.start()
        self.addCleanup(fetching.stop)
        self.approve(101, {"A.lean": "def a := 1\n"})
        self.controller.tick()
        self.approve(201, {"P201.lean": "def b := 1\n"})
        self.approve(202, {"P202.lean": "def c := 1\n"})
        for row in self.controller.store.rows("inputs"):
            record = {
                "state": "known",
                "version": 1,
                "head": row["head_sha"],
                "base": self.repo.main_sha(),
                "tested": row["head_sha"],
                "complete": True,
                "added": {f"TauCeti.M{row['number']}": ["Generated.shared" if row["number"] != 202 else "other"]},
            }
            self.controller.store.update("inputs", {"id": row["id"]}, declaration_names=json.dumps(record))
        self.controller.tick()
        own = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(own.task_id, own.claim_token, "prepared_clean")
        self.controller.tick()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertIn("pre_rebased", task.extra)
        cross = next(d for d in task.diagnostics if d["kind"] == "cross_batch")
        [collision] = cross["added_by_both"]
        self.assertEqual(collision["name"], "Generated.shared")
        self.assertEqual([m["pr"] for m in collision["head"]], [101])
        self.assertEqual([m["pr"] for m in collision["next"]], [201])

    def test_no_task_before_the_own_base_preparation_or_below_the_size(self):
        self.controller.set_setting("prepare_min_members", 3, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.approve(201, {"P201.lean": "201\n"})
        self.approve(202, {"P202.lean": "202\n"})
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [])
        self.assertEqual(
            (self.preparation()["preparation"], self.preparation()["prepare_blocked"]),
            ("unprepared", "below prepare_min_members"),
        )
        self.controller.set_setting("prepare_min_members", 2, "t")
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [], "the own-base preparation is open")
        self.assertTrue(self.preparation()["prepare_blocked"].startswith("prepare task"))
        self.controller.set_setting("prepare_pre_rebased", False, "t")
        own = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(own.task_id, own.claim_token, "prepared_clean")
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [])
        self.controller.set_setting("prepare_pre_rebased", True, "t")
        self.controller.tick()
        self.assertEqual(len(self.pre_rebased_tasks()), 1)

    def test_the_adopted_result_becomes_the_pre_rebase_and_seals_with_its_exact_tip(self):
        batch = self.open_batch()
        agent = ScriptedAgent(
            "preparer",
            "admin-worker",
            self.repo,
            self.root / "preparer",
            lambda task: {
                f"{PROJECT}#202": Resolution({"P202.lean": "fixed on the head\n"}, explanation="Fixed P202.")
            },
        )
        task = agent.claim(self.controller)
        self.assertIn("pre_rebased", task.extra)
        row = self.controller.store.row("tasks", {"id": task.task_id})
        diagnostics = loads(row["diagnostics"], [])
        diagnostics.append(
            {
                "kind": "admission_predictions",
                "records": [{"kind": "declaration_collision", "name": "Generated.shared", "sources": [{"pr": 202}]}],
            }
        )
        self.controller.store.update("tasks", {"id": task.task_id}, diagnostics=json.dumps(diagnostics))
        tip = agent.produce(task)
        result = self.controller.submit_task(task.task_id, task.claim_token, tip, worker_notes="{}")
        self.assertTrue(result.accepted, result)
        event = self.controller.store.row("journal", {"kind": "admission-repair-result"}, order="id DESC")
        detail = loads(event["detail"])
        self.assertEqual(detail["task_id"], task.task_id)
        self.assertIsNone(detail["attempt_id"], "a background result must not bind to the preceding head's CI")
        self.assertEqual(detail["edited"], [f"{PROJECT}#202"])
        self.assertEqual(result.provenance, {f"{PROJECT}#201": "clean", f"{PROJECT}#202": "edited"})
        after = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(
            (after["version"], after["base"], after["tip"]),
            (batch["version"], batch["base"], batch["tip"]),
            "the batch's own chain is unchanged",
        )
        self.assertEqual(
            (after["pre_rebase_state"], after["pre_rebase_tip"], after["pre_rebase_prepared"]),
            ("adopted", tip, tip),
        )
        self.assertEqual(after["pre_rebase_onto"], self.head()["tip"])
        self.assertEqual(loads(after["pre_rebase_edits"])[f"{PROJECT}#202"]["explanation"], "Fixed P202.")
        status = self.preparation()
        self.assertEqual((status["preparation"], status["prepared_tip"]), ("prepared", tip))
        self.assertEqual(self.controller.status()["counters"]["pre_rebased_prepares_adopted"], 1)
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [], "prepared for this head tip: nothing more to do")
        self.land_head()
        sealed = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(sealed["state"], "head")
        self.assertEqual(sealed["tip"], tip, "the head CI runs on is exactly the prepared tip")
        members = {m["number"]: m for m in self.controller.members(batch["id"])}
        self.assertEqual((members[202]["provenance"], members[201]["provenance"]), ("edited", "clean"))
        self.assertIn("Fixed P202.", members[202]["explanation"])
        self.assertEqual(self.github.runs[-1].head_sha, tip)

    def test_a_clean_release_marks_the_chain_prepared_and_a_failure_is_not_retried(self):
        batch = self.open_batch()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertIn("pre_rebased", task.extra)
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        after = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(after["pre_rebase_prepared"], batch["pre_rebase_tip"])
        self.assertEqual(self.pre_rebased_tasks("submitted")[0]["resolution"], "prepared")
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [])
        self.assertEqual(self.preparation()["preparation"], "prepared")
        self.land_head()
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["tip"], batch["pre_rebase_tip"])

    def test_a_failed_preparation_is_not_retried_for_the_same_chain(self):
        self.open_batch()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.release_task(task.task_id, task.claim_token, "agent_failed", "no progress")
        self.assertEqual(self.pre_rebased_tasks("superseded")[0]["resolution"], "failed")
        self.controller.tick()
        self.assertEqual(self.pre_rebased_tasks(), [])
        status = self.preparation()
        self.assertEqual(status["preparation"], "unprepared")
        self.assertEqual(status["prepare_blocked"], f"prepare task {task.task_id} was failed")

    def test_superseded_when_the_heads_tip_changes(self):
        self.open_batch()
        head = self.head()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertIn("pre_rebased", task.extra)
        # Main moves: the head is replayed onto it, so its tip (the chain's base) changes.
        self.repo.advance_main({"Z.lean": "z\n"})
        self.controller.tick()
        self.assertNotEqual(self.head()["tip"], head["tip"])
        [superseded] = self.pre_rebased_tasks("superseded")
        self.assertEqual((superseded["id"], superseded["resolution"]), (task.task_id, "head-moved"))
        with self.assertRaises(TaskError):
            self.controller.renew_task(task.task_id, task.claim_token)
        result = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertEqual((result.accepted, result.reason), (False, "claim-superseded"))

    def test_sealing_waits_for_and_adopts_a_running_pre_rebased_preparation(self):
        batch = self.open_batch()
        agent = ScriptedAgent(
            "preparer",
            "admin-worker",
            self.repo,
            self.root / "preparer",
            lambda _: {f"{PROJECT}#202": Resolution({"P202.lean": "fixed on the head\n"}, explanation="Fixed P202.")},
        )
        task = agent.claim(self.controller)
        self.assertIn("pre_rebased", task.extra)
        tip = agent.produce(task)
        self.land_head()
        self.assertIsNone(self.head())
        self.controller.renew_task(task.task_id, task.claim_token)
        self.assertIsNone(self.controller.claim_task("second-worker", "admin-worker"))
        result = self.controller.submit_task(task.task_id, task.claim_token, tip)
        self.assertTrue(result.accepted, result)
        self.controller.tick()
        sealed = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual((sealed["state"], sealed["tip"]), ("head", tip))
        self.assertEqual(self.repo.file_at(tip, "P202.lean"), "fixed on the head\n")
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["resolution"], "adopted")
        self.assertEqual(self.github.runs[-1].head_sha, tip)


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
    """The `dont-repair-in-queue` opt-out: read at approval, refreshed by label events, sent with tasks."""

    def label_event(self, number, action, name, labels=None):
        pull = {"number": number}
        if labels is not None:
            pull["labels"] = [{"name": label} for label in labels]
        self.controller.handle_event(
            "pull_request", {"action": action, "number": number, "label": {"name": name}, "pull_request": pull}
        )

    def test_label_at_approval_and_label_events_set_the_opt_out(self):
        self.approve(101, {"A.lean": "a\n"}, labels=["Dont-Repair-In-Queue"])
        self.approve(102, {"B.lean": "b\n"})
        rows = {row["number"]: row for row in self.controller.inputs()}
        self.assertEqual((rows[101]["no_repair"], rows[102]["no_repair"]), (1, 0))
        self.label_event(102, "labeled", "dont-repair-in-queue", ["dont-repair-in-queue"])
        self.assertEqual(self.controller.input_for(PROJECT, 102)["no_repair"], 1)
        self.controller.tick()  # both inserted and sealed
        head = self.controller.store.row("batches", {"state": "head"})
        members = {m["number"]: m for m in self.controller.members(head["id"])}
        self.assertEqual((members[101]["no_repair"], members[102]["no_repair"]), (1, 1))
        self.label_event(101, "unlabeled", "dont-repair-in-queue", [])
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
        self.assertEqual(DEFAULT_SETTINGS["no_repair_labels"], ["dont-repair-in-queue"])
        with self.assertRaises(ControllerError):
            self.controller.set_setting("no_repair_labels", "dont-repair-in-queue", "operator")
        with self.assertRaises(ControllerError):
            self.controller.set_setting("no_repair_labels", [1], "operator")
        self.controller.set_setting("no_repair_labels", ["dont-repair-in-queue", "no-agents"], "operator")
        self.approve(101, {"A.lean": "a\n"}, labels=["no-agents"])
        self.assertEqual(self.controller.input_for(PROJECT, 101)["no_repair"], 1)

    def test_tasks_carry_the_opt_out_and_an_unattributed_withdrawal_says_so(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "B.lean", "message": "boom"}])
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"}, labels=["dont-repair-in-queue"])
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
        reductions = self.controller.store.rows("journal", {"kind": "member-reduced-to-empty"})
        self.assertEqual(len(reductions), 1)
        reduced = loads(reductions[0]["detail"])
        self.assertTrue(self.controller._commit_empty(reduced["commit"]))
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
    def test_unclaimed_head_task_stalls_without_being_masked_by_other_admissions(self):
        self.failing_head()
        task = self.controller.status()["head"]["task"]
        interval = self.controller.setting("stall_interval_seconds")
        self.clock.advance(interval - 1)
        self.assertIsNone(self.controller.status()["head"]["stall"])
        self.clock.advance(1)
        stalled = self.controller.status()
        self.assertEqual(
            stalled["head"]["stall"],
            {
                "kind": "task_unclaimed",
                "since": task["created_at"] + interval,
                "silence_seconds": interval,
                "phase": "repair",
            },
        )
        self.assertEqual(stalled["alerts"], [])
        self.approve(102, {"Unrelated.lean": "more work\n"})
        self.controller.tick()
        self.clock.advance(interval)
        stalled = self.controller.status()
        self.assertEqual(stalled["head"]["task"]["task_id"], task["task_id"])
        self.assertEqual(stalled["head"]["stall"]["silence_seconds"], 2 * interval)
        self.assertTrue(any(" unclaimed for " in alert for alert in stalled["alerts"]))

    def test_claim_clears_unclaimed_stall_and_expiry_uses_ownership_end_before_cleanup(self):
        self.failing_head()
        interval = self.controller.setting("stall_interval_seconds")
        self.clock.advance(2 * interval)
        self.assertIsNotNone(self.controller.status()["head"]["stall"])
        task = self.controller.claim_task("repair-worker", "admin-worker")
        self.assertIsNotNone(task)
        self.assertIsNone(self.controller.status()["head"]["stall"])
        self.assertEqual(self.controller.status()["alerts"], [])
        self.clock.advance(task.expires_at - self.clock() + interval - 1)
        self.assertIsNone(self.controller.status()["head"]["stall"])
        self.clock.advance(1)
        stalled = self.controller.status()["head"]["stall"]
        self.assertEqual(stalled["since"], task.expires_at + interval)
        self.assertEqual(stalled["silence_seconds"], interval)
        self.clock.advance(interval)
        self.controller._expire_claims(self.clock())
        # Cleanup is late; it must not restart the timer at released_at.
        stalled = self.controller.status()["head"]["stall"]
        self.assertEqual(stalled["since"], task.expires_at + interval)
        self.assertEqual(stalled["silence_seconds"], 2 * interval)

    def test_released_task_wait_starts_at_release_not_the_future_lease_expiry(self):
        self.failing_head()
        task = self.controller.claim_task("repair-worker", "admin-worker")
        self.clock.advance(10)
        self.controller.release_task(task.task_id, task.claim_token, "worker-stopped")
        released_at = self.clock()
        interval = self.controller.setting("stall_interval_seconds")
        self.clock.advance(interval)
        stalled = self.controller.status()["head"]["stall"]
        self.assertEqual(stalled["since"], released_at + interval)
        self.assertEqual(stalled["silence_seconds"], interval)

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
                "admission",
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


class WorkerEndpointsDuringTick(ControllerCase):
    """A tick's GitHub I/O runs without the controller lock, and worker requests go ahead of the tick."""

    def slow_comments(self, delay):
        """Make each `post_comment` take `delay` seconds; returns an event set once the first call starts."""
        started = threading.Event()
        original = self.github.post_comment

        def slow(*args, **kwargs):
            started.set()
            time.sleep(delay)
            return original(*args, **kwargs)

        self.github.post_comment = slow
        return started

    def test_a_renewal_answers_while_a_tick_drains_many_entries_through_a_slow_github(self):
        self.failing_head()
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.controller.set_setting("outbox_drain_budget_seconds", 600, "t")
        entries = 30
        for index in range(entries):
            self.controller._enqueue(
                "comment", f"comment:{index}", {"number": 101, "body": f"landed {index}", "marker": f"m{index}"}
            )
        self.github.fail_next["post_comment"] = 1  # the first entry fails and is retried as before
        started = self.slow_comments(0.1)
        tick = threading.Thread(target=self.controller.tick)
        tick.start()
        self.addCleanup(tick.join)
        self.assertTrue(started.wait(5))
        latencies = []
        for _ in range(3):
            began = time.monotonic()
            expires = self.controller.renew_task(task.task_id, task.claim_token)
            latencies.append(time.monotonic() - began)
            time.sleep(0.2)
        self.assertTrue(tick.is_alive(), "the renewals were answered while the drain was still running")
        self.assertLess(max(latencies), 1.0, "a renewal waits for one entry at most, not the whole drain")
        self.assertEqual(expires, self.clock() + self.controller.setting("claim_ttl_seconds"))
        tick.join(30)
        self.assertFalse(tick.is_alive())
        keys = {f"comment:{index}" for index in range(entries)}
        rows = [row for row in self.controller.store.rows("outbox", {"kind": "comment"}) if row["key"] in keys]
        done = [row for row in rows if row["state"] == "done"]
        pending = [row for row in rows if row["state"] == "pending"]
        self.assertEqual(len(done), entries - 1, "the drain completes")
        self.assertEqual([(row["key"], row["attempts"]) for row in pending], [("comment:0", 1)])
        self.assertGreater(pending[0]["next_attempt_at"], self.clock(), "the failed entry backs off")
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        self.controller._drain_outbox(self.clock())
        self.assertEqual(self.controller.store.row("outbox", {"key": "comment:0"})["state"], "done")

    def test_a_renewal_kept_waiting_past_the_expiry_still_extends_the_claim(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.failing_head()
        task = self.controller.claim_task("worker-1", "admin-worker")
        held = threading.Event()
        proceed = threading.Event()
        answer = {}

        def busy_tick():
            with self.controller._lock:
                held.set()
                proceed.wait(10)
                self.controller._expire_claims(self.clock())  # the tick sees the claim past its expiry
                answer["active_task_claims"] = len(self.controller._active_task_claims())
                answer["active_batch_claims"] = len(self.controller._active_claims(task.batch_id))
                # Another endpoint may get the lock before the already-waiting renewal.
                answer["replacement"] = self.controller.claim_task("worker-2", "admin-worker")

        holder = threading.Thread(target=busy_tick)
        holder.start()
        self.addCleanup(holder.join)
        self.assertTrue(held.wait(5))
        renewal = threading.Thread(
            target=lambda: answer.update(expires=self.controller.renew_task(task.task_id, task.claim_token))
        )
        renewal.start()
        deadline = time.monotonic() + 5
        while not self.controller._lock.waiting() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.controller._lock.waiting(), 1, "the renewal arrived before the expiry and waits")
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 60)
        proceed.set()
        renewal.join(5)
        holder.join(5)
        self.assertFalse(renewal.is_alive())
        self.assertFalse(holder.is_alive())
        self.assertEqual((answer.get("active_task_claims"), answer.get("active_batch_claims")), (1, 1))
        self.assertIsNone(answer.get("replacement"), "a timely renewal retains its batch even before extension")
        self.assertEqual(answer.get("expires"), self.clock() + self.controller.setting("claim_ttl_seconds"))
        claim = self.controller.store.row("claims", {"token": task.claim_token})
        self.assertIsNone(claim["released_at"], "the waiting renewal kept the claim")
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        with self.assertRaises(TaskError) as caught:
            self.controller.renew_task(task.task_id, task.claim_token)
        self.assertEqual(caught.exception.code, "claim-expired", "a renewal arriving after the expiry is refused")

    def test_priority_acquirers_go_ahead_of_ordinary_waiters(self):
        lock = ControllerLock()
        order = []
        lock.acquire()
        ordinary = threading.Thread(target=lambda: (lock.acquire(), order.append("tick"), lock.release()))
        ordinary.start()
        time.sleep(0.1)

        def worker():
            with lock.priority():
                order.append("worker")

        priority = threading.Thread(target=worker)
        priority.start()
        deadline = time.monotonic() + 5
        while not lock.waiting() and time.monotonic() < deadline:
            time.sleep(0.01)
        lock.release()
        ordinary.join(5)
        priority.join(5)
        self.assertEqual(order, ["worker", "tick"])
        lock.acquire()
        lock.acquire()  # reentrant
        with lock.unlocked():
            other = threading.Thread(target=lambda: (lock.acquire(), order.append("other"), lock.release()))
            other.start()
            other.join(5)
        self.assertEqual(order[-1], "other", "`unlocked` lets go of every level")
        lock.release()
        lock.release()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


if __name__ == "__main__":
    unittest.main()
