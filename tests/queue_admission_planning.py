#!/usr/bin/env python3
"""Real Git evidence and speculative repair races, with no inference or network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_queue import admission_planning, repair
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller, reopen_controller

PROJECT = "TauCetiProject/TauCeti"


def collision():
    return {
        "kind": "declaration_collision",
        "name": "TauCeti.shared",
        "approximate": False,
        "prediction": True,
        "sources": [{"pr": 101, "module": "TauCeti.A"}, {"pr": 102, "module": "TauCeti.B"}],
    }


def knowledge():
    return {"kind": "admission_predictions", "records": [collision()], "complete": True}


class Planning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller, self.github, self.repo, self.clock = make_controller(Path(self.tmp.name))
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")

    def approve(self, pr, files):
        head = self.github.create_pr(pr, files)
        self.controller.approve(PROJECT, pr, head, "reviewer")
        return head

    def context(self):
        batch = self.controller._head()
        members = self.controller._members(batch["id"])
        inputs = {m["input_id"]: {} for m in members}
        for m in members:
            inputs[m["input_id"]]["declaration_names"] = {
                "state": "known",
                "version": 1,
                "base": self.repo.main_sha(),
                "tested": m["accepted_head"],
                "complete": True,
                "head": m["accepted_head"],
                "added": {f"TauCeti.M{m['number']}": ["TauCeti.shared"]},
            }
        return batch, members, inputs

    def test_compiled_collisions_and_transitive_import_components(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        self.approve(102, {"TauCeti/B.lean": "public import TauCeti.A\ndef b := 1\n"})
        self.approve(103, {"TauCeti/C.lean": "import TauCeti.B\ndef c := 1\n"})
        self.controller.tick()
        batch, members, inputs = self.context()
        planner = admission_planning.Planner(self.repo.clone)
        context = planner.context(members, inputs, tip=batch["tip"])
        self.assertEqual(context["errors"], [])
        collisions = [r for r in context["records"] if r["kind"] == "declaration_collision"]
        self.assertEqual(len(collisions), 1)
        self.assertEqual({r["pr"] for r in collisions[0]["sources"]}, {101, 102, 103})
        component = next(r for r in context["records"] if r["kind"] == "import_component")
        self.assertEqual(component["members"], [101, 102, 103])
        self.assertEqual(len(component["edges"]), 2)
        with patch.object(planner, "git", side_effect=AssertionError("immutable evidence should be cached")):
            self.assertEqual(planner.context(members, inputs, tip=batch["tip"]), context)

    def test_stale_incomplete_or_edited_accepted_names_cannot_trigger_repair(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        self.approve(102, {"TauCeti/B.lean": "def b := 1\n"})
        self.controller.tick()
        batch, members, inputs = self.context()
        planner = admission_planning.Planner(self.repo.clone)
        for mutation in ("head", "complete", "edited"):
            with self.subTest(mutation=mutation):
                first = members[0]
                names = inputs[first["input_id"]]["declaration_names"]
                saved = names.copy()
                if mutation == "head":
                    names["head"] = "0" * 40
                elif mutation == "complete":
                    names["complete"] = False
                else:
                    first["provenance"] = "edited"
                result = planner.context(members, inputs, tip=batch["tip"])
                self.assertFalse(any(r["kind"] == "declaration_collision" for r in result["records"]))
                inputs[first["input_id"]]["declaration_names"] = saved
                first["provenance"] = "clean"

    def test_comments_and_source_strings_are_not_import_edges(self):
        self.assertEqual(
            admission_planning.imports(
                "/- outer /- import TauCeti.Bad -/ -/\nmodule\npublic import TauCeti.Good\n"
                'def s := "import TauCeti.Wrong"\nimport TauCeti.Late\n'
            ),
            ["TauCeti.Good"],
        )

    def test_budget_failure_keeps_known_paths_and_marks_unknown(self):
        self.approve(101, {"TauCeti/A.lean": "x" * (admission_planning.MAX_FILE_BYTES + 1)})
        self.approve(102, {"TauCeti/B.lean": "import TauCeti.C\ndef b := 1\n"})
        self.approve(103, {"TauCeti/C.lean": "def c := 1\n"})
        self.controller.tick()
        batch, members, inputs = self.context()
        context = admission_planning.Planner(self.repo.clone).context(members, inputs, tip=batch["tip"])
        self.assertFalse(context["complete"])
        self.assertIn("budget", context["errors"][0])
        component = next(r for r in context["records"] if r["kind"] == "import_component")
        self.assertEqual(component["members"], [102, 103], "one oversized blob must not discard other headers")

    def start_prediction(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        self.patch = patch.object(self.controller, "_admission_context", return_value=knowledge())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.controller.tick()
        return self.controller._head(), self.controller.store.row("tasks", {"state": "open"})

    def test_first_ci_is_dispatched_while_prediction_task_is_available(self):
        head, task = self.start_prediction()
        self.assertIsNotNone(task)
        self.assertEqual(len(self.github.runs), 1)

        self.assertEqual(task["tip"], head["tip"])
        self.assertTrue(self.controller._prediction_task(task))
        self.controller.tick()
        self.assertEqual(self.controller.store.count("tasks"), 1)
        self.assertEqual(len(self.github.runs), 1)

    def test_stored_compiled_records_trigger_repair_and_reach_the_claimed_task(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        self.approve(102, {"TauCeti/B.lean": "def b := 1\n"})
        for row in self.controller.store.rows("inputs"):
            record = {
                "state": "known",
                "version": 1,
                "head": row["head_sha"],
                "base": self.repo.main_sha(),
                "tested": row["head_sha"],
                "complete": True,
                "added": {f"TauCeti.M{row['number']}": ["TauCeti.generated.projection"]},
            }
            self.controller.store.update("inputs", {"id": row["id"]}, declaration_names=json.dumps(record))
        self.controller.tick()
        task = self.controller.claim_task("fixture", "admin-worker")
        self.assertIsNotNone(task)
        self.assertEqual(len(self.github.runs), 1)
        self.assertEqual({m.declaration_names["state"] for m in task.members}, {"known"})
        knowledge = next(d for d in task.diagnostics if d["kind"] == "admission_predictions")
        self.assertTrue(knowledge["proactive"])
        self.assertEqual(knowledge["records"][0]["name"], "TauCeti.generated.projection")

    def test_noop_prediction_preserves_version_candidate_and_comments(self):
        head, _ = self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        adopted = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertTrue(adopted.accepted)
        self.assertEqual(adopted.version, head["version"])
        self.controller.tick()
        self.assertEqual(self.controller.store.count("tasks"), 1)
        self.assertEqual(len(self.github.runs), 1)
        self.assertEqual(self.controller.store.count("outbox", {"kind": "comment"}), 0)

    def test_unclaimed_prediction_is_superseded_by_own_ci_pass(self):
        self.start_prediction()
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        self.assertIsNone(self.controller._head())
        task = self.controller.store.row("tasks", {"id": 1})
        self.assertEqual(task["resolution"], "prediction-ci-passed")

    def test_own_ci_pass_retires_a_claimed_prediction_and_lands_immediately(self):
        head, _ = self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        self.assertEqual(self.controller._current_attempt(head)["outcome"], "pass")
        self.assertIsNone(self.controller._head())
        result = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "claim-superseded")

    def test_source_failure_is_added_to_existing_prediction_task(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "TauCeti.A", "message": "boom"}])
        _, task = self.start_prediction()
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        diagnostics = json.loads(self.controller.store.row("tasks", {"id": task["id"]})["diagnostics"])
        self.assertEqual({r["kind"] for r in diagnostics}, {"compiler", "admission_predictions"})
        self.assertEqual(self.controller.store.count("tasks"), 1)

    def test_structured_context_survives_worker_findings_conversion(self):
        record = knowledge()
        self.assertEqual(repair.findings_from_diagnostics([record])["admission_predictions"], record)
        self.assertEqual(
            repair.findings_from_diagnostics({"admission_predictions": record})["admission_predictions"], record
        )

    def test_failed_selection_evidence_travels_to_later_batch(self):
        self.approve(101, {"TauCeti/Head.lean": "def h := 1\n"})
        self.controller.tick()
        self.approve(102, {"Shared.lean": "line one\nfirst alternative\nline three\n"})
        self.approve(103, {"Shared.lean": "line one\nsecond alternative\nline three\n"})
        self.controller.tick()
        inputs = self.controller.store.rows("inputs")
        observed = [r for r in inputs if json.loads(r["evidence"]).get("merge_selection")]
        self.assertEqual(len(observed), 1)
        inserted = observed[0]
        self.assertEqual(inserted["state"], "inserted")
        evidence = json.loads(inserted["evidence"])["merge_selection"][0]
        self.assertEqual(evidence["paths"], ["Shared.lean"])
        self.assertEqual(set(evidence["members"]), {102, 103} - {inserted["number"]})
        batch = self.controller.store.row("batches", {"id": inserted["batch_id"]})
        with self.controller._lock:
            context = self.controller._admission_context(batch, self.clock())
        self.assertIn(evidence, context["records"])

    def test_conflicting_insertion_after_restart_uses_persisted_ownership(self):
        self.approve(101, {"TauCeti/Head.lean": "def h := 1\n"})
        self.controller.tick()
        self.approve(102, {"Shared.lean": "line one\nfirst alternative\nline three\n"})
        self.controller.tick()
        batch = self.controller._open_batch()
        self.assertTrue(self.controller._state(f"admission_context:{batch['id']}")["ownership"])
        self.controller.store.close()
        self.controller = reopen_controller(self.tmp.name, self.github, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.controller.recover()
        self.assertFalse(hasattr(self.controller, "_admission_planner"))
        self.approve(103, {"Shared.lean": "line one\nsecond alternative\nline three\n"})
        self.controller.tick()
        row = self.controller._input_for(103)
        self.assertEqual(row["state"], "inserted")
        self.assertEqual(json.loads(row["evidence"])["merge_selection"][0]["members"], [102])
        self.assertFalse(self.controller.store.rows("inputs", {"state": "reserved"}))

    def test_infrastructure_retry_continues_without_a_prediction_worker(self):
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        head, _ = self.start_prediction()
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        self.assertEqual(self.controller._current_attempt(head)["outcome"], "infrastructure")
        self.clock.advance(61)
        self.controller.tick()
        self.assertEqual(len(self.github.runs), 2)
        self.assertEqual(self.controller._current_attempt(head)["outcome"], "pending")

    def test_prediction_keeps_pre_rebase_available_until_actual_source_failure(self):
        head, _ = self.start_prediction()
        self.assertIsNone(self.controller._pre_rebase_blocked(head))
        self.controller.claim_task("fixture", "admin-worker")
        self.assertIsNone(self.controller._pre_rebase_blocked(head))

    def test_real_source_failure_cannot_be_resolved_by_an_earlier_noop_prediction(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "TauCeti.A", "message": "boom"}])
        head, task_row = self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        row = self.controller.store.row("tasks", {"id": task_row["id"]})
        self.assertFalse(self.controller._prediction_task(row))
        self.assertEqual(json.loads(row["diagnostics"])[0]["message"], "boom")
        result = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "claim-superseded")
        self.assertEqual(self.controller._head()["version"], head["version"])
        refreshed = self.controller.claim_task("refreshed-worker", "admin-worker")
        self.assertIsNotNone(refreshed, "no expired-claim delay after a stale no-op")
        self.assertEqual(refreshed.diagnostics[0]["message"], "boom")

    def test_landing_discards_operational_admission_caches(self):
        head, _ = self.start_prediction()
        self.patch.stop()
        with self.controller._lock:
            self.controller._admission_context(head, self.clock())
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        prefix = f"state:admission_repair:{head['id']}:"
        self.assertFalse(any(r["key"].startswith(prefix) for r in self.controller.store.rows("settings")))
        self.assertIsNone(self.controller._state(f"admission_context:{head['id']}"))

    def test_optional_planner_exception_does_not_strand_admission(self):
        with patch.object(admission_planning.Planner, "context", side_effect=OSError("unavailable")):
            self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
            self.controller.tick()
        self.assertEqual(len(self.github.runs), 1)
        self.assertFalse(self.controller.store.rows("inputs", {"state": "reserved"}))
        context = self.controller._state(f"admission_context:{self.controller._head()['id']}")
        self.assertFalse(context["complete"])

    def test_prompt_never_claims_a_prediction_is_a_failed_ci_verdict(self):
        self.assertFalse(any("admission.json" in line for line in admission_planning.prompt_lines(knowledge())))

    def test_preparation_navigation_is_not_a_preparation_error(self):
        self.approve(101, {"TauCeti/Head.lean": "def h := 1\n"})
        self.controller.tick()
        self.approve(102, {"TauCeti/A.lean": "def a := 1\n"})
        self.controller.tick()
        batch = self.controller._open_batch()
        with self.controller._lock:
            task_id = self.controller._create_task(batch, "prepare", batch["base"], [knowledge()], self.clock())
            self.controller.store.update("tasks", {"id": task_id}, state="submitted", resolution="adopted")
            self.controller.store.update("batches", {"id": batch["id"]}, prepared_version=batch["version"])
            batch = self.controller.store.row("batches", {"id": batch["id"]})
            self.assertIsNone(self.controller._prepare_outcome(batch))

    def test_new_insertions_do_not_repeat_a_collision_preparation_bypass(self):
        self.start_prediction()
        self.approve(102, {"TauCeti/A.lean": "def a := 1\n"})
        self.controller.tick()
        batch = self.controller._open_batch()
        prepared = self.controller.store.rows("tasks", {"kind": "prepare", "batch_id": batch["id"]})
        self.assertEqual(len(prepared), 1)
        self.approve(103, {"TauCeti/B.lean": "def b := 1\n"})
        self.controller.tick()
        self.assertEqual(len(self.controller.store.rows("tasks", {"kind": "prepare", "batch_id": batch["id"]})), 1)

    def test_payload_is_bounded_and_compiled_collisions_are_prioritized(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        self.approve(102, {"TauCeti/B.lean": "def b := 1\n"})
        self.controller.tick()
        batch, members, inputs = self.context()
        for row in inputs.values():
            row["evidence"] = {
                "merge_selection": [
                    {"kind": "selection_conflict", "pr": 101, "batch_id": 1, "paths": ["x" * 8192], "members": [102]}
                    for _ in range(50)
                ]
            }
        with patch.object(admission_planning, "MAX_CONTEXT_BYTES", 8192):
            context = admission_planning.Planner(self.repo.clone).context(members, inputs, tip=batch["tip"])
        self.assertLessEqual(len(json.dumps(context).encode()), 8192)
        self.assertFalse(context["complete"])
        self.assertEqual(context["records"][0]["kind"], "declaration_collision")

    def test_transient_planning_failure_retries_without_changing_the_tip(self):
        self.approve(101, {"TauCeti/A.lean": "def a := 1\n"})
        with patch.object(admission_planning.Planner, "context", side_effect=OSError("temporary")):
            self.controller.tick()
        head = self.controller._head()
        self.assertTrue(self.controller._state(f"admission_context:{head['id']}")["retryable"])
        self.clock.advance(61)
        with self.controller._lock:
            context = self.controller._admission_context(head, self.clock())
        self.assertTrue(context["complete"])
        self.assertEqual(context["tip"], head["tip"])
        self.assertTrue(context["ownership_complete"])

    def test_total_blob_budget_progresses_over_multiple_passes(self):
        self.approve(101, {"TauCeti/A.lean": "import TauCeti.B\ndef a := 1\n"})
        self.approve(102, {"TauCeti/B.lean": "def b := 1\n"})
        self.controller.tick()
        batch, members, inputs = self.context()
        planner = admission_planning.Planner(self.repo.clone)
        with patch.object(admission_planning, "MAX_BLOB_BYTES", 35):
            first = planner.context(members, inputs, tip=batch["tip"])
            self.assertTrue(first["retryable"])
            second = planner.context(members, inputs, tip=batch["tip"])
        self.assertTrue(second["complete"])
        self.assertFalse(planner.omitted, "a per-call budget is not a permanent per-file failure")
        self.assertTrue(any(r["kind"] == "import_component" for r in second["records"]))

    def test_task_payload_omits_ownership_and_controller_bookkeeping(self):
        context = dict(
            knowledge(),
            ownership=[{"pr": 101, "paths": ["A.lean"]}],
            signature="internal",
            at=1,
            prediction_digest="internal",
            retry_after=2,
        )
        payload = admission_planning.task_context(context)
        self.assertEqual(payload, knowledge())

    def test_prediction_result_uses_the_retried_ci_attempt(self):
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        head, original = self.start_prediction()
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        self.clock.advance(61)
        self.controller.tick()
        current = self.controller._current_attempt(head)
        self.assertNotEqual(current["id"], original["ci_attempt_id"])
        task = self.controller.claim_task("fixture", "admin-worker")
        self.assertTrue(self.controller.submit_task(task.task_id, task.claim_token, task.tip).accepted)
        row = self.controller.store.row("journal", {"kind": "admission-repair-result"}, order="id DESC")
        self.assertEqual(json.loads(row["detail"])["attempt_id"], current["id"])

    def test_verified_fixed_requires_observed_duplicate_final_checks_and_an_edited_owner(self):
        self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        agent = ScriptedAgent(
            "fixture",
            "admin-worker",
            self.repo,
            Path(self.tmp.name) / "agent",
            lambda _: {f"{PROJECT}#101": Resolution({"TauCeti/A.lean": "def a := 2\n"})},
        )
        tip = agent.produce(task)
        notes = {
            "admission_verification": {
                "tip": tip,
                "checks_passed": True,
                "observed_duplicate_names": ["TauCeti.shared", "Other.name"],
            }
        }
        self.assertTrue(self.controller.submit_task(task.task_id, task.claim_token, tip, json.dumps(notes)).accepted)
        row = self.controller.store.row("journal", {"kind": "admission-repair-result"}, order="id DESC")
        self.assertEqual(json.loads(row["detail"])["verified_fixed"], ["TauCeti.shared"])

    def test_noop_cannot_count_as_a_fixed_prediction(self):
        self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        notes = {
            "admission_verification": {
                "tip": task.tip,
                "checks_passed": True,
                "observed_duplicate_names": ["TauCeti.shared"],
            }
        }
        self.assertTrue(
            self.controller.submit_task(task.task_id, task.claim_token, task.tip, json.dumps(notes)).accepted
        )
        row = self.controller.store.row("journal", {"kind": "admission-repair-result"}, order="id DESC")
        self.assertEqual(json.loads(row["detail"])["verified_fixed"], [])

    def test_clean_background_preparation_records_zero_source_fixes(self):
        self.start_prediction()
        self.controller.set_setting("repair_concurrency", 2, "operator")
        self.approve(102, {"TauCeti/B.lean": "def b := 1\n"})
        self.controller.tick()
        self.controller.claim_task("head-worker", "admin-worker")
        task = self.controller.claim_task("background-worker", "admin-worker")
        self.assertEqual(task.kind, "prepare")
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        row = self.controller.store.row("journal", {"kind": "admission-repair-result"}, order="id DESC")
        detail = json.loads(row["detail"])
        self.assertEqual(detail["verified_fixed"], [])
        self.assertIsNone(detail["attempt_id"])
        self.assertEqual(detail["task_kind"], "prepare")


if __name__ == "__main__":
    unittest.main()
