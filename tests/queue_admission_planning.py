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
from tauceti_queue.fakes import make_controller

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
        self.controller.tick()
        batch, members, inputs = self.context()
        context = admission_planning.Planner(self.repo.clone).context(members, inputs, tip=batch["tip"])
        self.assertFalse(context["complete"])
        self.assertIn("budget", context["errors"][0])

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

    def test_ci_observation_continues_while_prediction_claim_blocks_promotion(self):
        head, _ = self.start_prediction()
        task = self.controller.claim_task("fixture", "admin-worker")
        self.github.complete_runs()
        self.clock.advance(61)
        self.controller.tick()
        self.assertEqual(self.controller._current_attempt(head)["outcome"], "pass")
        self.assertEqual(self.controller._head()["tip"], task.tip)
        self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.controller.tick()
        self.assertIsNone(self.controller._head())

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


if __name__ == "__main__":
    unittest.main()
