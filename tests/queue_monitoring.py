#!/usr/bin/env python3
"""Public export invariants using actual queue records and attributed Git history."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
