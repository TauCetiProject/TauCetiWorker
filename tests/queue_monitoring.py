#!/usr/bin/env python3
"""Public export invariants using actual queue records and attributed Git history."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_queue import monitoring
from tauceti_queue.fakes import make_controller, run_git

PROJECT = "TauCetiProject/TauCeti"


class Monitoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)
        for number in (101, 102):
            head = self.github.create_pr(number, {f"TauCeti/P{number}.lean": f"def p{number} := 1\n"})
            self.controller.approve(PROJECT, number, head, "reviewer")
        self.controller.tick()
        self.database = Path(self.controller.store.path)

    def export(self, **kwargs):
        return monitoring.project(self.database, clock=self.clock, **kwargs)

    def test_attributed_members_and_exact_attempt_identity_are_retained(self):
        value = self.export(facts=monitoring.CommitFacts(self.repo.clone))
        self.assertEqual(value["schema"], monitoring.SCHEMA)
        self.assertEqual({m["pr"] for m in value["members"]}, {"101", "102"})
        for member in value["members"]:
            self.assertFalse(member["empty"])
            self.assertEqual(member["disposition"], "clean_replay")
            self.assertTrue(member["commit_url"].endswith(member["commit"]))
        batch = next(b for b in value["batches"] if b["state"] == "head")
        [attempt] = value["attempts"]
        self.assertEqual(attempt["candidate"], batch["tip"])
        self.assertEqual(attempt["version"], batch["version"])
        self.assertEqual(attempt["base"], batch["base"])
        self.assertEqual(batch["size"], 2)
        self.assertEqual(attempt["dispatch_state"], "accepted")
        self.assertRegex(attempt["request_id"], r"^[0-9a-f]{32}$")

    def test_export_distinguishes_a_sealed_successor_from_unsealed_batches(self):
        self.controller.set_setting("sealed_successor_min_members", 1, "test")
        head = self.github.create_pr(201, {"TauCeti/P201.lean": "def p201 := 1\n"})
        self.controller.approve(PROJECT, 201, head, "reviewer")
        self.controller.tick()
        successor = self.controller.status()["sealed_successor"]
        value = self.export(status=self.controller.status())
        record = next(b for b in value["batches"] if b["id"] == str(successor["id"]))
        self.assertTrue(record["sealed"])
        self.assertEqual(record["state"], "open")
        self.assertIsNotNone(record["sealed_at"])
        self.assertEqual(value["settings"]["sealed_successor_min_members"], 1)

    def test_export_reads_an_unmigrated_dispatch_ledger_without_writing_it(self):
        missing = (
            "sent_at",
            "dispatch_retry_at",
            "request_id",
            "dispatch_state",
            "dispatch_error",
            "dispatch_retryable",
        )
        for name in missing:
            self.controller.store.execute(f"ALTER TABLE ci_attempts DROP COLUMN {name}")
        value = self.export()
        self.assertEqual(value["attempts"][0]["dispatch_state"], "legacy")
        self.assertEqual(value["attempts"][0]["request_id"], "")
        present = {r["name"] for r in self.controller.store.query("PRAGMA table_info(ci_attempts)")}
        self.assertFalse(set(missing) & present)

    def test_legacy_task_table_is_read_without_adding_a_binding_column(self):
        self.controller.store.execute("ALTER TABLE tasks DROP COLUMN task_binding")
        self.export()
        present = {r["name"] for r in self.controller.store.query("PRAGMA table_info(tasks)")}
        self.assertNotIn("task_binding", present)

    def test_dispatch_policy_is_allowlisted_cached_and_bound_to_the_canonical_commit(self):
        revision = self.repo.commit_branch(
            {
                "tauceti.toml": """[services.fleet]
namespace_build_cap = 20
pr_order = ["github", "chris", "namespace"]
staging_order = ["chris", "namespace", "github"]
private_token = "DO_NOT_PUBLISH_SECRET"
url = "https://private.invalid"
"""
            },
            "policy",
            push_to="refs/heads/policy",
        )
        run_git(self.repo.clone, "fetch", "-q", "origin", revision)
        facts = monitoring.CommitFacts(self.repo.clone)
        value = self.export(facts=facts, status={"canonical_sha": revision})
        self.assertEqual(value["dispatch_config"]["revision"], revision)
        self.assertEqual(value["dispatch_config"]["namespace_build_cap"], 20)
        self.assertEqual(value["dispatch_config"]["pr_order"], ["github", "chris", "namespace"])
        self.assertNotIn("DO_NOT_PUBLISH_SECRET", json.dumps(value))
        self.assertNotIn("private.invalid", json.dumps(value))
        with patch.object(monitoring.subprocess, "run", side_effect=AssertionError("cached policy must not run git")):
            self.assertEqual(facts.dispatch_config(revision), value["dispatch_config"])
            self.assertIsNone(facts.dispatch_config("--help"))
        self.assertIsNone(facts.dispatch_config(self.repo.main_sha()))
        self.assertIsNone(
            self.export(facts=facts, status={"canonical_sha": revision}, public_repositories=("Other/Public",))[
                "dispatch_config"
            ]
        )

    def test_raw_claims_task_diagnostics_descriptions_and_worker_paths_never_escape(self):
        sentinel = "DO_NOT_PUBLISH_SECRET"
        row = self.controller.store.rows("inputs")[0]
        self.controller.store.update(
            "inputs", {"id": row["id"]}, description=sentinel, evidence=json.dumps({"token": sentinel})
        )
        worker = self.root / "worker"
        worker.mkdir()
        (worker / "worker.json").write_text(
            json.dumps(
                {
                    "worker": "repair-a",
                    "state": "idle",
                    "updated_at": self.clock(),
                    "root": sentinel,
                    "last_error": {"error": sentinel},
                    "environment": {"TOKEN": sentinel},
                    "last_outcome": {"raw_prompt": sentinel},
                    "errors": 0,
                }
            )
        )
        self.github.admins.add("admin-worker")
        batch = self.controller._head()
        self.controller._create_task(batch, "repair", batch["base"], [{"message": sentinel}], self.clock())
        claim = self.controller.claim_task("fixture", "admin-worker")
        self.controller.store.update("claims", {"token": claim.claim_token}, token=sentinel)
        exported = self.export(worker_roots=[worker])
        self.assertNotIn(sentinel, json.dumps(exported))
        self.assertEqual(len(exported["claims"]), 1)
        self.assertEqual(exported["workers"][0]["id"], monitoring.worker_alias("repair-a"))
        self.assertEqual(exported["workers"][0]["errors"], 0)

    def test_canonical_tip_uses_the_existing_status_fact(self):
        value = self.export(status={"canonical_sha": "a" * 40, "service": {"now": self.clock()}})
        self.assertEqual(value["canonical_sha"], "a" * 40)
        self.assertEqual(value["status_observed_at"], monitoring.iso(self.clock()))
        self.assertIsNone(self.export(status={"canonical_sha": "PRIVATE_PATH"})["canonical_sha"])

    def test_task_checkpoint_and_outcome_keep_only_recorded_public_progress(self):
        sentinel = "DO_NOT_PUBLISH_SECRET"
        head = self.controller._head()
        self.controller._create_task(head, "repair", head["base"], [], self.clock())
        task_id = self.controller.store.rows("tasks")[-1]["id"]
        root = self.root / "worker"
        directory = root / "tasks" / str(task_id)
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(
            json.dumps(
                {
                    "task": {"task_id": str(task_id), "claim_token": sentinel},
                    "state": "agent_running",
                    "updated_at": self.clock(),
                    "worktree": sentinel,
                }
            )
        )
        (directory / "outcome.json").write_text(
            json.dumps(
                {
                    "seconds": 20,
                    "agent": [
                        {
                            "attempts": [
                                {
                                    "provider": "codex",
                                    "model": "gpt-6.1",
                                    "estimated_cost_usd": 1.25,
                                    "telemetry": {
                                        "raw_prompt": sentinel,
                                        "usage": {"input_tokens": 321, "output_tokens": 123, "private": sentinel},
                                    },
                                }
                            ]
                        }
                    ],
                    "error": sentinel,
                }
            )
        )
        value = self.export(worker_roots=[root], task_observations=monitoring.TaskObservations())
        progress = next(t for t in value["tasks"] if t["id"] == str(task_id))["progress"]
        self.assertEqual(progress["models"], ["gpt-6.1"])
        self.assertEqual(
            progress["usage"]["input_tokens"], {"recorded_tokens": 321, "recorded_attempts": 1, "unknown_attempts": 0}
        )
        self.assertEqual(
            progress["usage"]["cached_input_tokens"],
            {"recorded_tokens": 0, "recorded_attempts": 0, "unknown_attempts": 1},
        )
        self.assertEqual(progress["cost"]["recorded_usd"], 1.25)
        self.assertEqual(progress["state"], "agent_running")
        self.assertNotIn(sentinel, json.dumps(value))

    def test_mixed_task_binding_is_published_and_its_checkpoint_progress_matched(self):
        github = {"forge": {"kind": "github", "url": "https://github.com"}, "id": 7, "name": PROJECT}
        forgejo = {"forge": {"kind": "forgejo", "url": "https://git.example"}, "id": 1, "name": PROJECT}
        binding = {
            "schema": "tauceti-queue.mixed-task/v1",
            "github": github,
            "forgejo": forgejo,
            "policy_digest": "8" * 64,
            "epoch": "d" * 32,
        }
        head = self.controller._head()
        self.controller._create_task(head, "repair", head["base"], [], self.clock())
        task_id = self.controller.store.rows("tasks")[-1]["id"]
        self.controller.store.execute("UPDATE tasks SET task_binding=? WHERE id=?", (json.dumps(binding), task_id))
        directory = self.root / "worker" / "tasks" / str(task_id)
        directory.mkdir(parents=True)
        task = {
            "task_id": str(task_id),
            "claim_token": "c" * 32,
            "schema": "tauceti-queue.mixed-task/v1",
            "mixed_binding": binding,
            "repository": PROJECT,
        }
        (directory / "task.json").write_text(
            json.dumps({"task": task, "state": "agent_running", "updated_at": self.clock()})
        )
        value = self.export(worker_roots=[self.root / "worker"], task_observations=monitoring.TaskObservations())
        record = next(t for t in value["tasks"] if t["id"] == str(task_id))
        self.assertEqual(record["task_binding"], binding)
        self.assertNotIn("identity_coverage", record)
        self.assertEqual(record["progress"]["coverage"], "observed")
        self.assertEqual(record["progress"]["state"], "agent_running")
        self.assertNotIn("task_binding", record["progress"])
        # A checkpoint bound to another queue generation is never attributed to this task.
        task["mixed_binding"] = dict(binding, epoch="e" * 32)
        (directory / "task.json").write_text(
            json.dumps({"task": task, "state": "agent_running", "updated_at": self.clock() + 1})
        )
        value = self.export(worker_roots=[self.root / "worker"], task_observations=monitoring.TaskObservations())
        record = next(t for t in value["tasks"] if t["id"] == str(task_id))
        self.assertEqual(record["progress"]["coverage"], "unavailable")

    def test_export_does_not_write_the_database(self):
        before = {
            table: self.controller.store.rows(table) for table in ("inputs", "members", "batches", "claims", "settings")
        }
        self.export()
        after = {table: self.controller.store.rows(table) for table in before}
        self.assertEqual(before, after)
        with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_missing_git_and_worker_observations_remain_unknown(self):
        value = self.export(worker_roots=[self.root / "missing"])
        self.assertTrue(all(m["empty"] is None and m["disposition"] == "unknown" for m in value["members"]))
        self.assertEqual(value["workers"][0]["coverage"], "unavailable")
        self.assertIsNone(value["workers"][0]["observed_at"])
        self.assertIsNone(value["service"]["publication_enabled"])

    def test_truncation_does_not_turn_total_membership_into_a_partial_count(self):
        with patch.object(monitoring, "LIMIT", 1):
            value = self.export()
        self.assertTrue(value["truncated"])
        self.assertEqual(value["coverage"], "partial")
        self.assertEqual(len(value["members"]), 1)
        self.assertEqual(value["batches"][0]["size"], 2)

    def landed_batches(self, *sizes):
        """Terminal batches, oldest first, each with members whose inputs have landed."""
        store = self.controller.store
        template = {k: v for k, v in self.controller._head().items() if k != "id"}
        member = {k: v for k, v in store.rows("members")[0].items() if k != "id"}
        source = {k: v for k, v in store.rows("inputs")[0].items() if k != "id"}
        made = []
        for index, size in enumerate(sizes):
            batch = store.insert("batches", **(template | {"state": "landed", "landed_at": self.clock()}))
            for position in range(size):
                title = f"Landed {index}.{position}"
                number = 1000 + 10 * index + position
                item = store.insert("inputs", **(source | {"number": number, "state": "landed", "title": title}))
                store.insert("members", **(member | {"batch_id": batch, "input_id": item, "number": number}))
            made.append(str(batch))
        return made

    def test_the_row_limit_shortens_terminal_history_by_whole_batches(self):
        oldest, middle, newest = self.landed_batches(3, 1, 2)
        # Two active members leave room for three terminal ones: the newest two batches fit whole.
        with patch.object(monitoring, "LIMIT", 5):
            value = self.export()
        retained = {b["id"] for b in value["batches"]}
        self.assertTrue({middle, newest} <= retained)
        self.assertNotIn(oldest, retained)
        self.assertEqual(len(value["members"]), 5)
        self.assertEqual(value["coverage"], "observed")
        self.assertFalse(value["truncated"])
        self.assertTrue(value["collection_complete"]["members"])

    def test_members_keep_their_input_detail_outside_the_published_inputs(self):
        [batch] = self.landed_batches(2)
        with patch.object(monitoring, "TERMINAL_INPUTS", 0):
            value = self.export()
        self.assertNotIn("landed", {i["state"] for i in value["inputs"]})
        landed = [m for m in value["members"] if m["batch_id"] == batch]
        self.assertEqual(sorted(m["title"] for m in landed), ["Landed 0.0", "Landed 0.1"])
        self.assertTrue(all(m["approved_at"] for m in landed))

    def test_a_member_never_carries_a_private_input_detail(self):
        [batch] = self.landed_batches(1)
        self.controller.store.execute(
            "UPDATE inputs SET repository='OtherOrg/Private', title='PRIVATE_TITLE', author='PRIVATE_AUTHOR' "
            "WHERE id=(SELECT input_id FROM members WHERE batch_id=?)",
            (int(batch),),
        )
        value = self.export()
        [member] = [m for m in value["members"] if m["batch_id"] == batch]
        self.assertEqual((member["title"], member["author"]), ("", ""))
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_live_inputs_leave_terminal_inputs_only_the_remaining_records(self):
        self.landed_batches(1, 1)
        live = sum(1 for r in self.controller.store.rows("inputs") if r["state"] in ("waiting", "reserved"))
        with patch.object(monitoring, "LIMIT", live + 1):
            value = self.export()
        self.assertEqual(len(value["inputs"]), live + 1)
        self.assertTrue(value["collection_complete"]["inputs"])

    def test_git_failures_are_retried_without_discarding_previous_evidence(self):
        facts = monitoring.CommitFacts(self.repo.clone)
        first = self.export(facts=facts)
        with patch.object(facts, "_read", side_effect=OSError("private path")):
            second = self.export(facts=facts)
        self.assertEqual(first["members"], second["members"])
        self.assertNotIn("private path", json.dumps(second))

    def test_empty_edited_member_alone_does_not_establish_reduction(self):
        member = self.controller.store.rows("members")[0]
        self.controller.store.update("members", {"id": member["id"]}, provenance="edited")
        facts = monitoring.CommitFacts(None)
        facts.cache[member["commit_sha"]] = {"empty": True, "already_landed": False}
        value = self.export(facts=facts)
        item = next(m for m in value["members"] if m["id"] == str(member["id"]))
        self.assertEqual(item["disposition"], "empty_unknown")
        self.controller.store.journal(
            self.clock(), "member-reduced-to-empty", member["batch_id"], commit=member["commit_sha"]
        )
        value = self.export(facts=facts)
        item = next(m for m in value["members"] if m["id"] == str(member["id"]))
        self.assertEqual(item["disposition"], "reduced_empty")

    def test_claim_conditions_are_structured_without_publishing_provider_errors(self):
        value = self.export(
            status={
                "service": {"revision": "a" * 40, "controller": "running", "mode": "production"},
                "inference_hold": {"since": self.clock(), "last_error": "secret provider token"},
                "alerts": ["promotion is failing; see outbox history"],
            }
        )
        self.assertEqual([c["kind"] for c in value["conditions"]], ["inference_hold", "promotion_failed"])
        self.assertNotIn("secret provider token", json.dumps(value))

    def test_stuck_heads_are_published_as_bounded_conditions_without_pr_detail(self):
        now = self.clock()
        stuck = {
            "state": "fresh",
            "observed_at": now - 60,
            "count": 31,
            "complete": True,
            "heads": [{"pr": 40000 + n, "last_push_at": now - 4 * 3600 - n} for n in range(30)],
        }
        alert = "31 PR heads stuck without CI or a final state for 7h (oldest 4h)"
        value = self.export(status={"stuck_heads": stuck, "alerts": [alert]})
        kinds = [c["kind"] for c in value["conditions"]]
        self.assertEqual(kinds, ["stuck_heads_observed"] + ["stuck_head"] * 30 + ["stuck_heads_truncated"])
        self.assertEqual(value["conditions"][1]["since"], monitoring.iso(now - 4 * 3600))
        self.assertNotIn("40000", json.dumps(value["conditions"]))
        crowded = ["promotion is failing; see outbox history"] * 98
        value = self.export(status={"stuck_heads": dict(stuck, complete=False), "alerts": crowded})
        self.assertLessEqual(len(value["conditions"]), 100)
        kinds = [c["kind"] for c in value["conditions"]]
        self.assertEqual(kinds[-3:], ["stuck_heads_observed", "stuck_heads_partial", "stuck_heads_truncated"])
        self.assertEqual(value["conditions"][-1]["kind"], "stuck_heads_truncated")
        for state in ("stale", "missing", "unconfigured"):
            value = self.export(status={"stuck_heads": dict(stuck, state=state)})
            self.assertEqual(value["conditions"], [])

    def test_unclaimed_head_task_is_distinct_from_build_silence_without_duplicate_alert(self):
        head = self.controller.status()["head"]
        value = self.export(
            status={
                "head": {
                    "id": head["id"],
                    "stall": {
                        "kind": "task_unclaimed",
                        "since": self.clock() - 900,
                        "silence_seconds": 1800,
                        "phase": "repair",
                    },
                },
                "alerts": [f"head {head['id']} task 15 (repair) unclaimed for 1800s"],
            }
        )
        self.assertEqual(
            value["conditions"],
            [
                {
                    "kind": "task_unclaimed",
                    "since": monitoring.iso(self.clock() - 900),
                    "batch_id": str(head["id"]),
                    "seconds": 1800,
                }
            ],
        )

    def test_a_public_commit_link_cannot_point_to_an_arbitrary_origin(self):
        with self.assertRaises(ValueError):
            monitoring.public_url("https://private.example", "commit", "a" * 40)
        self.assertIsNone(monitoring.public_url(PROJECT, "commit", "../../secret"))

    def test_a_malformed_display_value_cannot_stringify_a_private_object(self):
        self.assertEqual(monitoring.text({"token": "PRIVATE_TOKEN"}), "")
        self.assertEqual(monitoring.text("a\x00b\x1bc\nλ"), "abc\nλ")

    def test_an_unlisted_repository_never_enters_the_public_pr_index(self):
        original = self.controller.store.rows("inputs")[0]
        fields = {k: v for k, v in original.items() if k != "id"}
        fields.update(repository="OtherOrg/Private", title="PRIVATE_TITLE", description="PRIVATE_DESCRIPTION")
        private_input = self.controller.store.insert("inputs", **fields)
        batch = {k: v for k, v in self.controller._head().items() if k != "id"}
        batch["repository"] = "OtherOrg/Private"
        private_batch = self.controller.store.insert("batches", **batch)
        member = {k: v for k, v in self.controller.store.rows("members")[0].items() if k != "id"}
        member.update(input_id=private_input, batch_id=private_batch)
        self.controller.store.insert("members", **member)
        value = self.export()
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("OtherOrg", json.dumps(value))
        self.assertEqual(value["scope"]["repositories"], [PROJECT])


class ParallelTaskObservations(unittest.TestCase):
    def test_a_released_worker_does_not_hide_another_current_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / str(i) for i in range(2)]
            for root, state, token, updated in zip(
                roots, ["agent_running", "released"], ["PRIVATE_ACTIVE", "PRIVATE_RELEASED"], [100, 120], strict=True
            ):
                task_dir = root / "tasks/19"
                task_dir.mkdir(parents=True)
                (task_dir / "task.json").write_text(
                    json.dumps({"task": {"task_id": "19", "claim_token": token}, "state": state, "updated_at": updated})
                )
            tasks = [{"id": 19, "state": "open"}]
            claims = [{"task_id": 19, "token": "PRIVATE_ACTIVE", "expires_at": 200}]
            observer = monitoring.TaskObservations()
            value = observer.observe(roots, tasks, claims=claims, observed_at=130)
            self.assertEqual(value[19]["state"], "agent_running")
            self.assertNotIn("PRIVATE", json.dumps(value))
            # Lease expiry and terminal task state must not make stale running
            # work appear current, even when the cached files have not changed.
            self.assertEqual(observer.observe(roots, tasks, claims=claims, observed_at=201)[19]["state"], "released")
            tasks[0]["state"] = "submitted"
            self.assertEqual(observer.observe(roots, tasks, claims=claims, observed_at=130)[19]["state"], "released")


class WorkerLiveness(unittest.TestCase):
    def test_positive_process_evidence_is_bound_to_root_role_owner_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "proc/42"
            process.mkdir(parents=True)
            command = process / "cmdline"
            command.write_bytes(b"python\0-m\0tauceti_queue.repair_task\0PRIVATE_ARGUMENT\0")
            record = process / "stat"

            def stat_record(ticks):
                return "42 (python) " + " ".join(["S"] + ["0"] * 18 + [str(ticks)])

            record.write_text(stat_record(100))
            value = {"pid": 42, "started_at": 95, "root": str(root)}
            with (
                patch.object(monitoring.time, "time", return_value=100),
                patch.object(monitoring.time, "clock_gettime", return_value=10),
                patch.object(monitoring.os, "sysconf", return_value=100),
            ):
                self.assertTrue(monitoring.verified_worker_alive(value, root, proc=root / "proc"))
                record.write_text(stat_record(1500))  # Same PID, newer process: no liveness claim.
                self.assertFalse(monitoring.verified_worker_alive(value, root, proc=root / "proc"))
                record.write_text(stat_record(100))
                self.assertFalse(
                    monitoring.verified_worker_alive(value | {"root": str(root / "other")}, root, proc=root / "proc")
                )
                with patch.object(monitoring.os, "getuid", return_value=os.getuid() + 1):
                    self.assertFalse(monitoring.verified_worker_alive(value, root, proc=root / "proc"))
                command.write_bytes(b"python\0unrelated_process\0")
                self.assertFalse(monitoring.verified_worker_alive(value, root, proc=root / "proc"))
                command.write_bytes(b"python\0-m\0tauceti_queue.repair_task\0" + b"PRIVATE" * 10000)
                self.assertFalse(monitoring.verified_worker_alive(value, root, proc=root / "proc"))
                command.unlink()
                self.assertFalse(monitoring.verified_worker_alive(value, root, proc=root / "proc"))

    def test_liveness_does_not_refresh_task_progress_or_invent_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.json").write_text(
                json.dumps(
                    {
                        "pid": 42,
                        "root": "PRIVATE",
                        "worker": "repair",
                        "state": "claiming",
                        "updated_at": 10,
                        "started_at": 5,
                        "tasks": 3,
                        "errors": 0,
                    }
                )
            )
            phase = {21: {"state": "agent_running", "observed_at": monitoring.iso(100)}}
            claims = [{"worker": "repair", "task_id": 21, "expires_at": 200}]
            with patch.object(monitoring, "verified_worker_alive", return_value=True):
                [value] = monitoring.workers([root], claims=claims, task_states=phase, captured=130)
                self.assertEqual(value["observed_at"], monitoring.iso(130))
                self.assertEqual(value["state"], "alive / agent_running")
                self.assertEqual(phase[21]["observed_at"], monitoring.iso(100))
                self.assertNotIn("PRIVATE", json.dumps(value))
                self.assertNotIn("pid", value)
                [expired] = monitoring.workers([root], claims=claims, task_states=phase, captured=201)
                self.assertEqual(expired["state"], "alive; task state unknown")
            with patch.object(monitoring, "verified_worker_alive", return_value=False):
                [unknown] = monitoring.workers([root], claims=claims, task_states=phase, captured=130)
                self.assertEqual(unknown["observed_at"], monitoring.iso(10))
                self.assertEqual(unknown["state"], "claiming")


class BuildObservations(unittest.TestCase):
    def test_live_preparation_is_visible_before_the_finished_log_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "tasks/79"
            task.mkdir(parents=True)
            live = Path(tmp) / "worktrees/task-79/.tauceti-repair/build.log"
            live.parent.mkdir(parents=True)
            live.write_text("[36/100] Built private.module\n")
            observer = monitoring.BuildObservations()
            first = observer.observe(task, "artifacts_ready", "token", 100, 0)
            self.assertEqual((first["stage"], first["completed_tasks"], first["total_tasks"]), ("preparation", 36, 100))
            self.assertIsNone(first["last_progress_at"])
            live.write_text("[60/120] Built another.private.module\n")
            second = observer.observe(task, "artifacts_ready", "token", 110, 0)
            self.assertEqual(second["last_progress_at"], monitoring.iso(110))
            self.assertNotIn("private", json.dumps(second))
            live.write_text("[118/120] Built private\nBuild completed successfully (120 jobs).\n")
            final = observer.observe(task, "artifacts_ready", "token", 120, 0)
            self.assertEqual((final["completed_tasks"], final["total_tasks"]), (120, 120))
            self.assertEqual(final["last_progress_at"], monitoring.iso(120))

    def test_live_preparation_never_follows_candidate_symlinks_or_reuses_an_old_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "tasks/79"
            task.mkdir(parents=True)
            tree = Path(tmp) / "worktrees/task-79"
            tree.mkdir(parents=True)
            private = Path(tmp) / "private"
            private.mkdir()
            log = private / "build.log"
            log.write_text("[50/100] Private data\n")
            (tree / ".tauceti-repair").symlink_to(private, target_is_directory=True)
            observer = monitoring.BuildObservations()
            self.assertIsNone(observer.observe(task, "artifacts_ready", "token", 100, 0))
            (tree / ".tauceti-repair").unlink()
            (tree / ".tauceti-repair").mkdir()
            (tree / ".tauceti-repair/build.log").symlink_to(log)
            self.assertIsNone(observer.observe(task, "artifacts_ready", "token", 100, 0))
            (tree / ".tauceti-repair/build.log").unlink()
            (tree / ".tauceti-repair/build.log").write_text("[20/100] Old attempt\n")
            self.assertIsNone(observer.observe(task, "artifacts_ready", "token", 100, 10**12))

    def test_build_counters_follow_the_current_claim_not_the_newest_worker_file(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / str(i) for i in range(2)]
            for root, token, updated, count in zip(roots, ["ACTIVE", "OLD"], [100, 120], [10, 99], strict=True):
                task = root / "tasks/21"
                (task / "logs").mkdir(parents=True)
                (task / "task.json").write_text(
                    json.dumps(
                        {
                            "task": {"task_id": "21", "claim_token": token},
                            "state": "sources_ready",
                            "updated_at": updated,
                        }
                    )
                )
                (task / "logs/prepare-build.log").write_text(f"[{count}/100] Built private\n")
            value = monitoring.TaskObservations().observe(
                roots,
                [{"id": 21, "state": "open"}],
                claims=[{"task_id": 21, "token": "ACTIVE", "expires_at": 200}],
                observed_at=130,
            )[21]
            self.assertEqual(value["build"]["completed_tasks"], 10)
            self.assertEqual(value["observed_at"], monitoring.iso(100))

    def test_numeric_tail_progress_is_independent_of_checkpoint_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "tasks/21"
            (task / "logs").mkdir(parents=True)
            checkpoint = task / "task.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "task": {"task_id": "21", "claim_token": "PRIVATE_TOKEN"},
                        "state": "sources_ready",
                        "updated_at": 100,
                    }
                )
            )
            log = task / "logs/prepare-build.log"
            log.write_text("PRIVATE_LOG /private/path\n[58742/60357] Built Private.Module\n")
            os.utime(log, (105, 105))
            observer = monitoring.TaskObservations()
            tasks = [{"id": 21, "state": "open"}]
            first = observer.observe([root], tasks, observed_at=110)[21]
            self.assertEqual(first["observed_at"], monitoring.iso(100))
            self.assertEqual(first["build"]["completed_tasks"], 58742)
            self.assertIsNone(first["build"]["last_progress_at"])
            with log.open("a") as output:
                output.write("[58743/60357] Built Private.Next\n")
            os.utime(log, (115, 115))
            second = observer.observe([root], tasks, observed_at=120)[21]
            self.assertEqual(second["build"]["last_progress_at"], monitoring.iso(120))
            self.assertEqual(second["observed_at"], monitoring.iso(100))
            with log.open("a") as output:
                output.write("heartbeat PRIVATE_LOG\n")
            os.utime(log, (125, 125))
            third = observer.observe([root], tasks, observed_at=130)[21]
            self.assertEqual(third["build"]["sampled_at"], monitoring.iso(130))
            self.assertEqual(third["build"]["log_updated_at"], monitoring.iso(125))
            self.assertEqual(third["build"]["last_progress_at"], monitoring.iso(120))
            self.assertNotIn("PRIVATE", json.dumps(third))
            self.assertNotIn("Private", json.dumps(third))
            self.assertNotIn("/private", json.dumps(third))
            tasks[0]["state"] = "adopted"
            self.assertNotIn("build", observer.observe([root], tasks, observed_at=140)[21])

    def test_tail_is_bounded_and_resets_do_not_invent_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            (task / "logs").mkdir()
            log = task / "logs/verify-build.log"
            # A giant earlier count outside the bounded tail is not evidence.
            log.write_bytes(b"[999/999] old\n" + b"private\n" * 10000)
            observer = monitoring.BuildObservations()
            self.assertIsNone(observer.observe(task, "verifying", "token", 100, 0))
            with log.open("ab") as output:
                output.write(b"[1/10] Built private\n[999/1] invalid\n[1234567890/1234567890] invalid\n")
            first = observer.observe(task, "verifying", "token", 110, 0)
            self.assertEqual((first["completed_tasks"], first["total_tasks"]), (1, 10))
            log.write_text("[0/10] restarted\n")
            reset = observer.observe(task, "verifying", "token", 120, 0)
            self.assertIsNone(reset["last_progress_at"])
            with log.open("a") as output:
                output.write("[2/10] Built private\n")
            self.assertEqual(
                observer.observe(task, "verifying", "token", 130, 0)["last_progress_at"], monitoring.iso(130)
            )
            self.assertIsNone(observer.observe(task, "verifying", "new-token", 140, 0)["last_progress_at"])
            self.assertIsNone(observer.observe(task, "verifying", "new-token", 150, 1)["last_progress_at"])
            self.assertIsNone(observer.observe(task, "verifying", "new-token", 160, log.stat().st_mtime + 1))
            log.unlink()
            target = task / "secret"
            target.write_text("[5/10] PRIVATE\n")
            log.symlink_to(target)
            self.assertIsNone(observer.observe(task, "verifying", "token", 170, 0))
            log.unlink()
            os.mkfifo(log)
            self.assertIsNone(observer.observe(task, "verifying", "token", 180, 0))


class LargeTaskCheckpoint(unittest.TestCase):
    def test_large_private_context_does_not_erase_bounded_public_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "tasks/21"
            task_dir.mkdir(parents=True)
            checkpoint = task_dir / "task.json"
            private = "PRIVATE_CONTEXT_DO_NOT_PUBLISH" * 120000
            checkpoint.write_text(
                json.dumps(
                    {
                        "task": {"task_id": "21", "claim_token": "PRIVATE_CLAIM"},
                        "state": "sources_ready",
                        "updated_at": 100,
                        "prompt": private,
                    }
                )
            )
            self.assertGreater(checkpoint.stat().st_size, monitoring.MAX_BYTES)
            with self.assertRaises(ValueError):
                monitoring.json_read(checkpoint)  # Public/default readers retain their existing cap.
            observer = monitoring.TaskObservations()
            value = observer.observe([root], [{"id": 21, "state": "open"}], observed_at=110)
            self.assertEqual(value[21]["state"], "sources_ready")
            wire = json.dumps(value)
            self.assertNotIn("PRIVATE", wire)
            self.assertLess(len(wire), 4096)
            checkpoint.write_text("corrupt private checkpoint")
            self.assertEqual(monitoring.TaskObservations().observe([root], [{"id": 21, "state": "open"}]), {})


class PublisherCadence(unittest.TestCase):
    def test_upload_duration_is_included_without_execution_deadlines_or_hot_retries(self):
        for duration, failed, expected_wait in [(7, False, 3), (21, False, 1), (21, True, 20)]:
            with self.subTest(duration=duration, failed=failed), tempfile.TemporaryDirectory() as directory:
                clock = [0]
                token = Path(directory) / "token"
                token.write_text("test-telemetry-only-token")
                args = SimpleNamespace(
                    interval=10,
                    follow=True,
                    status_url="http://status",
                    database="unused",
                    worker_root=[],
                    public_repository=[],
                    output=None,
                    publish_url="https://receiver",
                    token_file=token,
                )
                stop = Mock()
                stop.is_set.return_value = False
                stop.wait.return_value = True
                publisher = Mock()

                def publish(_observation, _send, clock=clock, duration=duration, failed=failed):
                    clock[0] += duration
                    if failed:
                        raise OSError("receiver unavailable")

                publisher.publish.side_effect = publish
                with (
                    patch.object(monitoring.time, "monotonic", side_effect=lambda clock=clock: clock[0]),
                    patch.object(monitoring.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")),
                    patch.object(monitoring, "project", return_value={}),
                ):
                    self.assertEqual(monitoring.run(args, stop, None, None, publisher), 0)
                publisher.publish.assert_called_once()
                stop.wait.assert_called_once_with(expected_wait)


def trim_one_batch_at_a_time(observation, budget):
    """The previous trimming loop, one full encoding per removed batch, as the reference result."""
    encode = monitoring.encode
    while len(encode(observation)) > budget:
        terminal = [b for b in observation["batches"] if b["state"] not in ("head", "open")]
        if terminal:
            drop = min(terminal, key=lambda b: int(b["id"]))["id"]
            observation["batches"] = [b for b in observation["batches"] if b["id"] != drop]
            for name in ("members", "attempts", "tasks", "events"):
                observation[name] = [r for r in observation[name] if r["batch_id"] != drop]
        else:
            name = max(
                (k for k in ("members", "inputs", "attempts", "tasks", "events") if observation[k]),
                key=lambda k: len(encode(observation[k])),
                default=None,
            )
            if name is None:
                raise ValueError("metadata exceeds public projection budget")
            observation[name] = observation[name][: max(0, len(observation[name]) - 100)]
            observation["collection_complete"][name] = False
            observation["truncated"] = True
            observation["coverage"] = "partial"


class ProjectionBudget(unittest.TestCase):
    """The byte budget removes the same records as trimming one batch at a time, in a few encodings."""

    @staticmethod
    def observation(terminal, active, members_per_batch):
        import random

        rng = random.Random(terminal * 1000 + active)
        names = ("batches", "inputs", "members", "attempts", "tasks", "events")
        value = {
            "schema": monitoring.SCHEMA,
            "coverage": "observed",
            "truncated": False,
            "collection_complete": dict.fromkeys(names, True),
            **{name: [] for name in names},
        }
        for number in range(1, terminal + active + 1):
            batch = str(number)
            state = "landed" if number <= terminal else ("head" if number == terminal + 1 else "open")
            value["batches"].append({"id": batch, "state": state})
            for _ in range(rng.randint(0, members_per_batch)):
                ident = str(len(value["members"]) + 1)
                value["members"].append({"id": ident, "batch_id": batch, "title": "x" * rng.randint(0, 400)})
                value["inputs"].append({"id": ident, "title": "y" * rng.randint(0, 200)})
            for name in ("attempts", "tasks", "events"):
                for _ in range(rng.randint(0, 3)):
                    value[name].append(
                        {"id": str(len(value[name]) + 1), "batch_id": batch, "x": "z" * rng.randint(0, 99)}
                    )
        rng.shuffle(value["batches"])
        return value

    def assert_same_as_reference(self, value, budget):
        expected = json.loads(json.dumps(value))
        trim_one_batch_at_a_time(expected, budget)
        full = []
        real = monitoring.encode

        def counted(item):
            if isinstance(item, dict) and "schema" in item:
                full.append(item)
            return real(item)

        with patch.object(monitoring, "encode", counted):
            monitoring.fit(value, budget)
        self.assertEqual(value, expected)
        self.assertLessEqual(len(monitoring.encode(value)), budget)
        return len(full)

    def test_shortened_terminal_history_is_not_partial(self):
        value = self.observation(terminal=50, active=2, members_per_batch=10)
        active = {b["id"] for b in value["batches"] if b["state"] in ("head", "open")}
        monitoring.fit(value, len(monitoring.encode(value)) // 2)
        self.assertEqual(value["coverage"], "observed")
        self.assertFalse(value["truncated"])
        self.assertTrue(all(value["collection_complete"].values()))
        self.assertTrue(active < {b["id"] for b in value["batches"]})
        self.assertLess(len(value["batches"]), 52)

    def test_active_only_overflow_is_partial(self):
        value = self.observation(terminal=2, active=3, members_per_batch=300)
        monitoring.fit(value, len(monitoring.encode(value)) // 2)
        self.assertEqual(value["coverage"], "partial")
        self.assertTrue(value["truncated"])
        self.assertFalse(value["collection_complete"]["members"])

    def test_terminal_history_is_shortened_oldest_first(self):
        value = self.observation(terminal=200, active=3, members_per_batch=20)
        size = len(monitoring.encode(value))
        for fraction in (0.95, 0.6, 0.3, 0.1):
            with self.subTest(fraction=fraction):
                encodings = self.assert_same_as_reference(json.loads(json.dumps(value)), int(size * fraction))
                self.assertLessEqual(encodings, 3)

    def test_active_only_overflow_trims_the_largest_collection(self):
        value = self.observation(terminal=5, active=4, members_per_batch=400)
        size = len(monitoring.encode(value))
        for fraction in (0.9, 0.5, 0.2):
            with self.subTest(fraction=fraction):
                self.assert_same_as_reference(json.loads(json.dumps(value)), int(size * fraction))

    def test_a_fitting_projection_is_encoded_once_and_left_alone(self):
        value = self.observation(terminal=3, active=2, members_per_batch=5)
        expected = json.loads(json.dumps(value))
        self.assertEqual(self.assert_same_as_reference(value, 10**9), 1)
        self.assertEqual(value, expected)

    def test_flag_bytes_count_toward_an_exact_fit(self):
        # Clearing the flags saves bytes of their own; a budget met only with them still fits.
        for terminal, active in ((0, 1), (1, 1), (6, 1)):
            value = self.observation(terminal=terminal, active=active, members_per_batch=3)
            value["members"] = value["members"][:1] or [{"id": "1", "batch_id": "1"}]
            for slack in range(-3, 4):
                with self.subTest(terminal=terminal, slack=slack):
                    budget = len(monitoring.encode(value)) - len(monitoring.encode(value["members"][-1])) + slack
                    try:
                        self.assert_same_as_reference(json.loads(json.dumps(value)), budget)
                    except ValueError:
                        with self.assertRaises(ValueError):
                            trim_one_batch_at_a_time(json.loads(json.dumps(value)), budget)

    def test_metadata_alone_over_budget_is_refused(self):
        value = self.observation(terminal=2, active=1, members_per_batch=3)
        with self.assertRaises(ValueError):
            monitoring.fit(value, 50)


class StatusRead(unittest.TestCase):
    """The publisher reads the status summary, and the full document from a controller without it."""

    def serve(self, routes):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                body = routes.get(self.path)
                self.send_response(404 if body is None else 200)
                self.end_headers()
                self.wfile.write(json.dumps(body or {"error": "no such route"}).encode())

            def log_message(self, *_):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_summary_is_read_when_served(self):
        base = self.serve({"/status/summary": {"head": {"id": 1}}, "/status": {"head": {"id": 2}}})
        self.assertEqual(monitoring.read_status(base + "/status/summary"), {"head": {"id": 1}})

    def test_older_controller_falls_back_to_the_full_document(self):
        base = self.serve({"/status": {"head": {"id": 2}}})
        self.assertEqual(monitoring.read_status(base + "/status/summary"), {"head": {"id": 2}})

    def test_other_failures_leave_status_unavailable(self):
        base = self.serve({})
        self.assertEqual(monitoring.read_status(base + "/status"), {})
        self.assertEqual(monitoring.read_status("http://127.0.0.1:1/status/summary", timeout=1), {})


if __name__ == "__main__":
    unittest.main()
