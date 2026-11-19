import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import monitoring, task_activity


class Activity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / "stat").write_text("btime 1000\n")
        self.directory = self.root / "worker/tasks/7"
        self.directory.mkdir(parents=True)
        self.tree = self.root / "worker/worktrees/task-7"
        self.tree.mkdir(parents=True)
        self.progress = {
            "schema": "tauceti.queue.task-progress/v1",
            "task_id": "7",
            "claim_token": "PRIVATE_TOKEN",
            "state": "artifacts_ready",
            "updated_at": 1001,
        }
        (self.directory / "progress.json").write_text(json.dumps(self.progress))
        self.ticks = patch.object(task_activity.os, "sysconf", return_value=100)
        self.ticks.start()
        self.addCleanup(self.ticks.stop)

    def process(self, name, args, started=1010):
        p = self.proc / "123"
        p.mkdir(exist_ok=True)
        (p / "cwd").symlink_to(self.tree)
        (p / "comm").write_text(name + "\n")
        (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        fields = ["0"] * 22
        fields[0] = "S"
        fields[19] = str((started - 1000) * 100)
        (p / "stat").write_text("123 (" + name + ") " + " ".join(fields))

    def sample(self):
        return task_activity.sample([self.root / "worker"], proc=self.proc, now=1020)

    def test_observes_live_build_without_waiting_for_worker_checkpoint(self):
        self.process("lake", ["/nix/bin/lake", "build", "--iofail", "PRIVATE_ARGUMENT"])
        log = self.tree / ".tauceti-repair/build.log"
        log.parent.mkdir()
        log.write_text("[10/100] Built Private.Module\n")
        os.utime(log, (1015, 1015))
        [record] = self.sample()
        self.assertEqual(record["state"], "building")
        self.assertEqual(record["build"]["completed_tasks"], 10)
        observation = task_activity.public(self.directory, "PRIVATE_TOKEN", 1021)
        self.assertEqual(observation["observed_at"], monitoring.iso(1010))
        self.assertNotIn("PRIVATE", json.dumps(observation))
        self.assertNotIn(str(self.root), json.dumps(observation))

    def test_discriminates_cache_freeze_from_compilation(self):
        self.process("lake", ["lake", "build", "--no-build", "--rehash", "-o", "PRIVATE_PATH"])
        [record] = self.sample()
        self.assertEqual(record["state"], "freezing_cache_outputs")
        self.assertNotIn("build", record)

    def test_audit_phase_comes_from_a_live_driver_not_old_agent_state(self):
        self.process("audit-driver", ["/private/audit-driver"])
        [record] = self.sample()
        self.assertEqual(record["state"], "auditing")
        observation = task_activity.public(self.directory, "PRIVATE_TOKEN", 1021)
        self.assertEqual(observation["state"], "auditing")
        self.assertIsNone(task_activity.public(self.directory, "OTHER_CLAIM", 1021))
        self.assertIsNone(task_activity.public(self.directory, "PRIVATE_TOKEN", 1066))

    def test_current_audit_marker_refines_phase_without_publishing_paths(self):
        self.process("audit-driver", ["audit-driver"])
        marker = self.tree / ".lake/tauceti-repair-audit/audit-phases.tsv"
        marker.parent.mkdir(parents=True)
        marker.write_text("audit-import\t1011\naudit-duplicates\t1015\naudit-axioms\t1017\n")
        [record] = self.sample()
        self.assertEqual((record["state"], record["phase_started_at"]), ("audit_axioms", 1017))
        marker.write_text("audit-import\t1000\n")
        self.assertEqual(self.sample()[0]["state"], "auditing")

    def test_does_not_retag_process_from_a_previous_attempt(self):
        self.process("lake", ["lake", "build"], started=1000)
        self.progress["updated_at"] = 1009
        (self.directory / "progress.json").write_text(json.dumps(self.progress))
        self.assertEqual(self.sample(), [])

    def test_public_projection_rejects_wrong_task_and_unsafe_build_values(self):
        self.process("lake", ["lake", "build"])
        [record] = self.sample()
        record["task_id"] = "8"
        (self.directory / "activity.json").write_text(json.dumps(record))
        self.assertIsNone(task_activity.public(self.directory, "PRIVATE_TOKEN", 1021))
        record.update(task_id="7", build={"completed_tasks": True, "total_tasks": 5, "stage": "PRIVATE"})
        (self.directory / "activity.json").write_text(json.dumps(record))
        self.assertNotIn("build", task_activity.public(self.directory, "PRIVATE_TOKEN", 1021))

    def test_exporter_uses_only_current_claims_fresh_live_phase(self):
        self.process("audit-driver", ["audit-driver"])
        self.sample()
        observer = monitoring.TaskObservations()
        tasks = [{"id": 7, "state": "open"}]
        claims = [{"task_id": 7, "token": "PRIVATE_TOKEN", "expires_at": 1100}]
        value = observer.observe([self.root / "worker"], tasks, claims=claims, observed_at=1021)[7]
        self.assertEqual(value["state"], "auditing")
        self.assertNotIn("PRIVATE", json.dumps(value))
        expired = observer.observe([self.root / "worker"], tasks, claims=claims, observed_at=1066)[7]
        self.assertEqual(expired["state"], "artifacts_ready")


if __name__ == "__main__":
    unittest.main()
