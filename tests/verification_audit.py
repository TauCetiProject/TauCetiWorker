#!/usr/bin/env python3
"""Private-state scaling, FIFO and mechanical recovery fixtures; no agents, Lean or network."""

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verification import REPO, Receipts, write

from tauceti_worker import verification as v
from tauceti_worker.verification_jobs import resume_pending


class Recovery(Receipts):
    # Inherit the receipt/invalidations suite as well, exercising it with the new recovery fixtures.
    def test_repeated_deadlines_resume_without_model_or_remote_reads(self):
        with (v.queue_root() / "slot-0.lock").open("a+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            result = self.verify(stage="build", deadline=time.time() + 1)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["result_type"], "pending")
            self.assertIn("without a model", result["next_action"])
            job_path = Path(result["continuation_job"])
            priority = json.loads(job_path.read_text())["priority"]
            for _ in range(2):
                job = json.loads(job_path.read_text())
                job["next_at"] = 0
                v.atomic(job_path, job)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=0.5))
                job = json.loads(job_path.read_text())
                self.assertEqual(job["priority"], priority)
                self.assertEqual(job["status"], "pending")
                self.assertTrue(job["isolated"])
        job["next_at"] = 0
        v.atomic(job_path, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertFalse(job_path.exists())
        complete = json.loads((self.state / "verification-jobs" / job_path.name).read_text())
        receipt = json.loads(Path(complete["receipt"]).read_text())
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(complete["attempts"], 3)
        self.assertTrue(any(c["argv"] == ["lake", "build", "--iofail"] and c["exit"] == 0 for c in receipt["commands"]))
        self.assertFalse((self.root / ".lake/calls").exists())  # only the isolated candidate was built

    def test_partial_phase_survives_next_queue_deadline(self):
        # A phase can establish durable evidence even when a later requested phase is interrupted.
        verification = v.Verification(self.root, self.state, stage="all")
        original = verification.phase_commands

        def commands(phase, modules):
            if phase == "axioms":
                raise v.Incomplete("fixture: deadline before next phase")
            return original(phase, modules)

        with (
            patch.object(verification, "phase_commands", side_effect=commands),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = verification.run()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["phases"]["build"]["status"], "success")
        self.assertIn("reused_from", self.verify(stage="build")["phases"]["build"])

    def test_recovery_does_not_reset_changed_original_candidate(self):
        result = self.verify(stage="build", deadline=time.time() - 1)
        job_path = Path(result["continuation_job"])
        job = json.loads(job_path.read_text())
        job["next_at"] = 0
        v.atomic(job_path, job)
        write(self.root, "TauCeti/A.lean", "later edit")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        complete = json.loads((self.state / "verification-jobs" / job_path.name).read_text())
        self.assertEqual(complete["status"], "success")
        self.assertEqual((self.root / "TauCeti/A.lean").read_text(), "later edit")

    def test_supervisor_continues_before_inference_or_github_admission(self):
        from tauceti_worker import loop

        with (
            patch("tauceti_worker.verification_jobs.resume_pending", side_effect=[True, KeyboardInterrupt]) as resume,
            patch.object(loop, "fleet_admission_reason") as admission,
            patch.object(loop, "github_budget") as github,
            patch.object(loop, "choose_model") as model,
        ):
            loop.cmd_loop(
                SimpleNamespace(), SimpleNamespace(state=self.state, wid="fixture"), only=["fix"], agent="claude"
            )
        self.assertEqual(resume.call_count, 2)
        admission.assert_not_called()
        github.assert_not_called()
        model.assert_not_called()

    def test_cheap_checks_bypass_occupied_slot(self):
        with (v.queue_root() / "slot-0.lock").open("a+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            for stage in ("shim-expiry", "lint"):
                result = self.verify(stage=stage, deadline=time.time() + 5)
                self.assertEqual(result["status"], "success", result.get("error"))
                self.assertEqual(result["queue_wait_seconds"], 0)
            write(self.root, "TauCeti/A.lean", "changed")
            result = self.verify(stage="lint", deadline=time.time() + 1)
            self.assertEqual(result["result_type"], "pending")  # environment lint is expensive

    def test_bubble_candidate_survives_namespace_and_original_deletion(self):
        import shutil

        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            result = self.verify(stage="build", deadline=time.time() - 1)
        job_path = Path(result["continuation_job"])
        job = json.loads(job_path.read_text())
        shutil.rmtree(self.root)
        # Simulate the mounted namespace used by the disposable container.
        for key in ("root", "lease"):
            job[key] = str(Path("/opt/verification-state") / Path(job[key]).relative_to(self.state))
        job["next_at"] = 0
        v.atomic(job_path, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertFalse(job_path.exists())
        complete = json.loads((self.state / "verification-jobs" / job_path.name).read_text())
        self.assertEqual(complete["status"], "success")

    def test_full_size_immutable_inventory_warm_cost_and_invalidation(self):
        from tauceti_worker.verification_installations import Installations

        # Thirty thousand files / 120 MiB: metadata and payload independent warm preparation.
        large = self.home / "immutable"
        payload = b"fixture source\n".ljust(4096, b" ")
        for directory in range(100):
            folder = large / str(directory)
            folder.mkdir(parents=True)
            for index in range(300):
                (folder / f"{index}.lean").write_bytes(payload)
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(large): "publisher-revision-1"})
        metrics = dict(files=0, bytes=0, walks=0, stats=0)

        def files(root):
            metrics["walks"] += 1
            yield from v.files_under(root)

        def record(path):
            metrics["files"] += 1
            metrics["bytes"] += path.stat().st_size
            return v.file_record(path)

        real_stat, real_lstat = os.stat, os.lstat

        def counted(function):
            def call(path, *args, **kwargs):
                if str(path).startswith(str(large)):
                    metrics["stats"] += 1
                return function(path, *args, **kwargs)

            return call

        def installations():
            return Installations(
                self.state,
                digest=v.digest,
                atomic=v.atomic,
                read_json=v.read_json,
                files_under=files,
                file_record=record,
                stamps=v.stamps,
            )

        # Fixture-only read-only mount identity. Real mutable directories never take this path.
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("os.stat", side_effect=counted(real_stat)),
            patch("os.lstat", side_effect=counted(real_lstat)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only-mount"]),
        ):
            cold = installations().inventory(large)
            self.assertEqual(metrics["bytes"], 122880000)
            cold_metrics = dict(metrics)
            metrics.update(files=0, bytes=0, walks=0, stats=0)
            # Unrelated historical receipts must have no effect on preparation.
            for index in range(2000):
                write(self.state, f"verification/history-{index}/receipt.json", "{}")
            from tauceti_worker.verification_queue import admitted

            queue = v.queue_root()

            def contender(_):
                installation = installations()
                identity = installation.inventory(large)
                installation.validate()
                with admitted(queue, 1, watch=lambda: None, waiting=lambda: None):
                    time.sleep(0.005)  # local fake heavy work after immutable preparation
                return identity, installation

            with ThreadPoolExecutor(max_workers=20) as contenders:
                results = list(contenders.map(contender, range(20)))
            for identity, _installation in results:
                self.assertEqual(identity, cold)
            installation = results[-1][1]
            self.assertEqual(
                {key: metrics[key] for key in ("files", "bytes", "walks")}, dict(files=0, bytes=0, walks=0)
            )
            self.assertLessEqual(metrics["stats"], 120)
            print("IMMUTABLE_COST", json.dumps(dict(cold=cold_metrics, warm_twenty=dict(metrics)), sort_keys=True))
            v.atomic(manifest, {str(large): "publisher-revision-2"})
            changed = installations().inventory(large)
            self.assertNotEqual(cold, changed)
            self.assertEqual(metrics["files"], 30000)
            v.atomic(manifest, {str(large): "publisher-revision-3"})
            with self.assertRaisesRegex(ValueError, "provenance changed"):
                installation.validate()
        # A registration alone never trusts a writable installation.
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=None),
        ):
            self.assertNotIn("immutable_installation", installations().inventory(large))

    def test_readonly_mount_validation_includes_nested_mounts(self):
        from tauceti_worker.verification_installations import mount_identity

        nested = self.prefix / "nested directory"
        nested.mkdir()
        escaped = str(nested).replace(" ", r"\040")
        topology = f"1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:2 / {self.prefix} ro - ext4 /dev/tool ro\n3 2 8:3 / {escaped} rw - ext4 /dev/nested rw\n"
        with patch.object(Path, "read_text", return_value=topology), patch("os.statvfs") as filesystem:
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=0 if Path(path) == nested else os.ST_RDONLY)
            self.assertIsNone(mount_identity(self.prefix))
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=os.ST_RDONLY)
            self.assertEqual(len(mount_identity(self.prefix)), 3)
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=0)
            self.assertIsNone(mount_identity(self.prefix))

    def test_immutable_receipts_skip_walks_and_invalidate_provenance(self):
        deps = self.root / ".lake/packages"
        write(deps, "mathlib/Mathlib/A.lean", "dependency")
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(self.prefix): "lean-1", str(deps): "mathlib-1"})
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only-mount"]),
        ):
            first = self.verify(stage="build")
            self.assertEqual(first["status"], "success", first.get("error"))
            with patch("tauceti_worker.verification.files_under", side_effect=AssertionError("warm immutable walk")):
                second = self.verify(stage="build")
            self.assertIn("reused_from", second["phases"]["build"])
            self.assertEqual(self.verify(stage="all")["status"], "success")
            write(self.prefix, "bin/lean", "publisher replaced toolchain")
            v.atomic(manifest, {str(self.prefix): "lean-2", str(deps): "mathlib-1"})
            third = self.verify(stage="build")
            self.assertNotIn("reused_from", third["phases"]["build"])

    def test_twenty_fifo_contenders_and_dead_waiter(self):
        queue = v.queue_root()
        output = self.home / "order"
        script = """
import sys, time
from pathlib import Path
from tauceti_worker.verification_queue import admitted
with admitted(Path(sys.argv[1]), 1, watch=lambda: None, waiting=lambda: None):
    with Path(sys.argv[2]).open('a') as out: out.write(sys.argv[3]+'\\n')
    time.sleep(0.02)
"""
        processes = []
        try:
            with (queue / "slot-0.lock").open("a+") as owner:
                fcntl.flock(owner, fcntl.LOCK_EX)
                for index in range(20):
                    process = subprocess.Popen(
                        [sys.executable, "-c", script, str(queue), str(output), str(index)], cwd=REPO
                    )
                    processes.append(process)
                    limit = time.monotonic() + 10
                    while len(list(queue.glob("wait-*"))) < index + 1:
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), limit)
                        time.sleep(0.01)
                processes[5].kill()
                processes[5].wait()
            for process in processes:
                process.wait(timeout=20)
            self.assertEqual(output.read_text().splitlines(), [str(i) for i in range(20) if i != 5])
            self.assertFalse(list(queue.glob("wait-*")))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_v0_dispatch_default_and_explicit_opt_in(self):
        from tauceti_worker.agents import local_verification

        with patch.dict(os.environ):
            os.environ.pop("TAUCETI_VERIFY_HELPER", None)
            self.assertNotIn("tauceti-verify", local_verification("ordinary"))
            os.environ["TAUCETI_VERIFY_HELPER"] = "1"
            self.assertIn("tauceti-verify", local_verification("ordinary"))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(Recovery(name) for name in Recovery.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
