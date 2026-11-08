"""Repair progress is independent of private task size and never controls the repair."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_queue import monitoring, repair_task, task_progress
from tauceti_worker import runtime_status


def attempt(cost=1.25, tokens=7, model="gpt-test"):
    return {
        "provider": "openai",
        "model": model,
        "estimated_cost_usd": cost,
        "telemetry": {"usage": {"input_tokens": tokens, "total_tokens": tokens}},
        "session": "PRIVATE_SESSION",
    }


class TaskProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "tasks/21"
        self.task = SimpleNamespace(to_json=lambda: {"task_id": "21", "claim_token": "PRIVATE_CURRENT"})
        self.state = repair_task.TaskState(self.directory, self.task)

    def observe(self, **kwargs):
        return monitoring.TaskObservations().observe([self.root], [{"id": 21, "state": "open"}], **kwargs)[21]

    def test_large_checkpoint_and_outcome_never_enter_the_compact_reader(self):
        private = "PRIVATE_CONTEXT" * 1_300_000
        self.state.write("verifying", prompt=private, diagnostics={"private": private})
        self.assertGreater(self.state.path.stat().st_size, 16_000_000)
        outcome = {"seconds": 93, "agent": [{"attempts": [attempt()]}], "private": private * 2}
        runtime_status.atomic_json(self.directory / "outcome.json", outcome)
        self.state.write_progress(outcome)
        self.assertGreater((self.directory / "outcome.json").stat().st_size, 16_000_000)
        read = monitoring.json_read
        paths = []

        def compact_only(path, *args, **kwargs):
            paths.append(Path(path).name)
            self.assertEqual(Path(path).name, task_progress.FILENAME)
            return read(path, *args, **kwargs)

        with (
            patch.object(monitoring, "json_read", side_effect=compact_only),
            patch.object(monitoring.json, "load", side_effect=AssertionError("must not open private payload")),
        ):
            value = self.observe()
        self.assertEqual(paths, [task_progress.FILENAME])
        self.assertEqual(value["state"], "verifying")
        self.assertEqual(value["seconds"], 93)
        self.assertEqual(value["cost"]["recorded_usd"], 1.25)
        self.assertEqual(value["usage"]["input_tokens"]["recorded_tokens"], 7)
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertLess((self.directory / task_progress.FILENAME).stat().st_size, 4096)

    def test_legacy_checkpoint_and_outcome_have_no_valid_task_size_ceiling(self):
        private = "PRIVATE_CONTEXT" * 1_300_000
        raw = {"task": self.task.to_json(), "state": "adopted", "updated_at": 100, "prompt": private}
        runtime_status.atomic_json(self.state.path, raw)
        runtime_status.atomic_json(
            self.directory / "outcome.json",
            {
                "seconds": 20,
                "agent": [{"attempts": [attempt()]}],
                "private": private,
            },
        )
        self.assertGreater(self.state.path.stat().st_size, 16_000_000)
        self.assertGreater((self.directory / "outcome.json").stat().st_size, 16_000_000)
        value = self.observe()
        self.assertEqual(value["state"], "adopted")
        self.assertEqual(value["seconds"], 20)
        self.assertEqual(value["cost"]["recorded_usd"], 1.25)
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_accounting_includes_every_attempt_once_and_bounds_only_model_display(self):
        runs = [attempt(model=f"model-{index}") for index in range(1200)]
        unknown = {"telemetry": {"usage": {}}, "provider": "openai"}
        final = {
            "agent": [{"parallel": True, "attempts": [*runs, unknown]}],
            "parallel": {"attempts": runs, "groups": [{"agent": {"attempts": runs}}]},
        }
        self.state.write("adopted", outcome=final)
        value = self.observe()
        self.assertEqual(value["cost"]["recorded_usd"], 1500)
        self.assertEqual(value["cost"]["recorded_attempts"], 1200)
        self.assertEqual(value["cost"]["unknown_attempts"], 1)
        self.assertEqual(
            value["usage"]["input_tokens"], {"recorded_tokens": 8400, "recorded_attempts": 1200, "unknown_attempts": 1}
        )
        self.assertEqual(value["usage"]["output_tokens"]["unknown_attempts"], 1201)
        self.assertEqual(len(value["models"]), task_progress.DISPLAY_MODELS)
        self.assertTrue(value["models_truncated"])

    def test_failed_parallel_attempts_and_later_sequential_attempt_both_count(self):
        parallel = attempt(cost=2, tokens=10)
        final = {
            "agent": [{"attempts": [attempt(cost=3, tokens=20)]}],
            "parallel": {"attempts": [parallel], "groups": [{"agent": {"attempts": [parallel]}}]},
        }
        self.state.write("adopted", outcome=final)
        value = self.observe()
        self.assertEqual(value["cost"]["recorded_usd"], 5)
        self.assertEqual(value["cost"]["recorded_attempts"], 2)
        self.assertEqual(value["usage"]["input_tokens"]["recorded_tokens"], 30)

    def test_current_lease_wins_over_newer_released_projection_and_legacy_worker_still_works(self):
        self.state.write("verifying")
        other = self.root / "other"
        directory = other / "tasks/21"
        runtime_status.atomic_json(
            directory / "task.json",
            {
                "task": {"task_id": "21", "claim_token": "PRIVATE_OLD"},
                "state": "released",
                "updated_at": 10**10,
            },
        )
        observer = monitoring.TaskObservations()
        tasks = [{"id": 21, "state": "open"}]
        claims = [{"task_id": 21, "token": "PRIVATE_CURRENT", "expires_at": 200}]
        value = observer.observe([self.root, other], tasks, claims=claims, observed_at=100)[21]
        self.assertEqual(value["state"], "verifying")
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(
            observer.observe([self.root, other], tasks, claims=claims, observed_at=201)[21]["state"], "released"
        )
        self.assertEqual(observer.observe([other], tasks)[21]["state"], "released")

    def test_failed_projection_is_optional_and_atomic_old_progress_remains_readable(self):
        self.state.write("sources_ready")
        original = (self.directory / task_progress.FILENAME).read_bytes()
        replace = runtime_status.os.replace

        def fail_progress(source, destination):
            if Path(destination).name == task_progress.FILENAME:
                self.assertEqual((self.directory / task_progress.FILENAME).read_bytes(), original)
                raise OSError("projection storage unavailable")
            return replace(source, destination)

        with patch.object(runtime_status.os, "replace", side_effect=fail_progress):
            self.state.write("verifying")
        self.assertEqual(json.loads(self.state.path.read_text())["state"], "verifying")
        self.assertEqual((self.directory / task_progress.FILENAME).read_bytes(), original)
        self.assertEqual(self.observe()["state"], "sources_ready")
        self.assertEqual(list(self.directory.glob(".progress.json.*")), [])
        with patch.object(task_progress, "summarize", side_effect=ValueError("broken optional telemetry")):
            self.state.write("adopted")
        self.assertEqual(json.loads(self.state.path.read_text())["state"], "adopted")
        self.state.write_progress({"seconds": 5, "agent": [{"attempts": [attempt()]}]})
        self.assertEqual(self.observe()["state"], "adopted")
        self.assertEqual(self.observe()["seconds"], 5)

    def test_compact_record_identity_and_allowlist_are_rechecked_without_private_fallback(self):
        self.state.write("verifying")
        path = self.directory / task_progress.FILENAME
        record = json.loads(path.read_text())
        record.update(private="PRIVATE", claim_token="PRIVATE", state={"PRIVATE": "not display text"})
        record["cost"]["private"] = "PRIVATE"
        runtime_status.atomic_json(path, record)
        self.assertNotIn("PRIVATE", json.dumps(self.observe()))
        record["task_id"] = 22
        runtime_status.atomic_json(path, record)
        self.assertEqual(monitoring.TaskObservations().observe([self.root], [{"id": 21, "state": "open"}]), {})
        path.write_text("broken JSON")
        self.assertEqual(monitoring.TaskObservations().observe([self.root], [{"id": 21, "state": "open"}]), {})


if __name__ == "__main__":
    unittest.main()
