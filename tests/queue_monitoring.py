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


if __name__ == "__main__":
    unittest.main()
