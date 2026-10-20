#!/usr/bin/env python3
"""Private-state scaling, concurrent verification and mechanical recovery fixtures; no agents, Lean or network."""

import contextlib
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
    # Reuse the private Git/fake-verifier fixture; load_tests collects only these audit cases.
    def test_repeated_deadlines_resume_without_model_or_remote_reads(self):
        with patch.dict(os.environ, FIXTURE_MODE="sleep", FIXTURE_SLEEP="60"):
            result = self.verify(stage="build", deadline=time.time() + 1)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["result_type"], "pending")
            self.assertIn("without a model", result["next_action"])
            job_path = Path(result["continuation_job"])
            priority = v.read_json(job_path)["priority"]
            for _ in range(2):
                job = v.read_json(job_path)
                job["next_at"] = 0
                v.atomic(job_path, job)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=0.5))
                job = v.read_json(job_path)
                self.assertEqual(job["priority"], priority)
                self.assertEqual(job["status"], "pending")
                self.assertTrue(job["isolated"])
        job["next_at"] = 0
        v.atomic(job_path, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertFalse(job_path.exists())
        complete = v.read_json(self.state / "verification-jobs" / job_path.name)
        receipt = v.read_json(Path(complete["receipt"]))
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(complete["attempts"], 3)
        self.assertTrue(any(c["argv"] == ["lake", "build", "--iofail"] and c["exit"] == 0 for c in receipt["commands"]))
        self.assertEqual(len(self.calls()), 1)  # all further work ran in the isolated candidate

    def test_sigkill_build_owner_retains_mechanical_next_action(self):
        process = self.spawn(FIXTURE_MODE="sleep", FIXTURE_SLEEP="60")
        tool = None
        try:
            limit = time.monotonic() + 10
            while not (self.root / ".lake/tool.pid").exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), limit)
                time.sleep(0.02)
            tool = int((self.root / ".lake/tool.pid").read_text())
            [path] = list((self.state / "verification-pending").glob("*.json"))
            process.kill()
            process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
            if tool is not None:
                import signal

                with contextlib.suppress(ProcessLookupError):
                    os.kill(tool, signal.SIGKILL)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        complete = v.read_json(self.state / "verification-jobs" / path.name)
        self.assertEqual(complete["status"], "success")
        commands = v.read_json(Path(complete["receipt"]))["commands"]
        self.assertTrue(any(c["argv"] == ["lake", "build", "--iofail"] and c["exit"] == 0 for c in commands))

    def test_pending_cooldown_retains_owner_and_invalid_job_cannot_delete_source(self):
        result = self.verify(stage="build", deadline=time.time() - 1)
        cfg = SimpleNamespace(state=self.state)
        with patch("tauceti_worker.verification.Verification", side_effect=AssertionError("not eligible yet")):
            self.assertFalse(resume_pending(cfg))
        path = Path(result["continuation_job"])
        job = v.read_json(path)
        job.update(id=str(self.root), next_at=0, isolated=False)
        v.atomic(path, job)
        with patch("tauceti_worker.verification_jobs.snapshot", side_effect=AssertionError("invalid job copied")):
            self.assertFalse(resume_pending(cfg))
        self.assertTrue((self.root / "TauCeti/A.lean").is_file())

    def test_supervisor_stop_preserves_job_and_kills_owned_tool(self):
        result = self.verify(stage="build", deadline=time.time() - 1)
        path = Path(result["continuation_job"])
        job = v.read_json(path)
        job["next_at"] = 0
        v.atomic(path, job)
        script = """
import signal, sys
from pathlib import Path
from types import SimpleNamespace
from tauceti_worker.verification_jobs import resume_pending
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
resume_pending(SimpleNamespace(state=Path(sys.argv[1])))
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.state)],
            cwd=REPO,
            env={**self.env, "FIXTURE_MODE": "sleep", "FIXTURE_SLEEP": "60"},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            pid_file = Path(job["root"]) / ".lake/tool.pid"
            limit = time.monotonic() + 10
            while not pid_file.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), limit)
                time.sleep(0.02)
            tool = int(pid_file.read_text())
            process.terminate()
            process.communicate(timeout=20)
            self.assertEqual(process.returncode, 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(tool, 0)
            job = v.read_json(path)
            self.assertEqual(job["status"], "pending")
            job["next_at"] = 0
            v.atomic(path, job)
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
            self.assertEqual(v.read_json(self.state / "verification-jobs" / path.name)["status"], "success")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    def test_host_snapshot_retains_external_trusted_immutable_packages(self):
        packages = self.home / "immutable-packages"
        write(packages, "mathlib/Mathlib/A.lean", "dependency")
        (self.root / ".lake").mkdir()
        (self.root / ".lake/packages").symlink_to(packages, target_is_directory=True)
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(packages): "publisher-1"})
        with patch.dict(os.environ, FIXTURE_MODE="sleep", FIXTURE_SLEEP="60"):
            with (
                patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
                patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only"]),
            ):
                result = self.verify(stage="build", deadline=time.time() - 1)
        job = v.read_json(Path(result["continuation_job"]))
        preserved = Path(job["root"]) / ".lake/packages"
        self.assertTrue(preserved.is_symlink())
        self.assertEqual(preserved.resolve(), packages)

    def test_malformed_compact_identity_is_revalidated(self):
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only"]),
        ):
            self.assertEqual(self.verify(stage="build")["status"], "success")
            [path] = list((v.cache_root() / "installations").glob("*.json"))
            old = v.read_json(path)
            old.update(sha256="invalid")
            old.pop("files")
            v.atomic(path, old)
            self.assertEqual(self.verify(stage="build")["status"], "success")
            self.assertEqual(len(v.read_json(path)["sha256"]), 64)
            self.assertEqual(v.read_json(path)["files"], 1)

    def test_permanent_incomplete_does_not_suppress_inference(self):
        from tauceti_worker import loop

        write(self.root, ".git/shallow", self.base + "\n")
        result = self.verify(stage="lint")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["result_type"], "incomplete")
        self.assertNotIn("continuation_job", result)
        self.assertFalse(list((self.state / "verification-pending").glob("*.json")))
        with (
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "choose_model", side_effect=KeyboardInterrupt) as model,
        ):
            loop.cmd_loop(
                SimpleNamespace(), SimpleNamespace(state=self.state, wid="fixture"), only=["fix"], agent="claude"
            )
        model.assert_called_once()

    def test_background_verification_leaves_inference_admission_available_and_drains(self):
        from tauceti_worker import loop
        from tauceti_worker.verification_jobs import stop_continuations

        result = self.verify(stage="build", deadline=time.time() - 1)
        path = Path(result["continuation_job"])
        job = v.read_json(path)
        job["next_at"] = 0
        v.atomic(path, job)
        cfg = SimpleNamespace(state=self.state, wid="fixture")
        with patch.dict(os.environ, FIXTURE_MODE="sleep", FIXTURE_SLEEP="60"):
            with (
                patch.object(loop, "fleet_admission_reason", return_value="reserved for CI"),
                patch("tauceti_worker.verification_jobs.resume_pending") as resume,
                patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt),
            ):
                loop.cmd_loop(SimpleNamespace(), cfg, only=["fix"], agent="claude")
            resume.assert_not_called()
            started = time.monotonic()
            self.assertTrue(resume_pending(cfg, background=True))
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(resume_pending(cfg, background=True))  # inherited lease prevents duplicate owners
            with (
                patch.object(loop, "fleet_admission_reason", return_value=None),
                patch.object(loop, "choose_model", side_effect=KeyboardInterrupt) as model,
            ):
                loop.cmd_loop(SimpleNamespace(), cfg, only=["fix"], agent="claude")
            model.assert_called_once()
            stop_continuations(cfg)
            self.assertTrue(path.is_file())
        job = v.read_json(path)
        job["next_at"] = 0
        v.atomic(path, job)
        self.assertTrue(resume_pending(cfg, seconds=10))
        self.assertEqual(v.read_json(self.state / "verification-jobs" / path.name)["status"], "success")

    def test_default_all_runs_style_before_build(self):
        write(self.root, "scripts/lint-style.sh", "mkdir -p .lake; echo checked > .lake/style-ran\n")
        result = self.verify(stage="all")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertTrue((self.root / ".lake/style-ran").is_file())
        commands = result["commands"]
        style = next(i for i, c in enumerate(commands) if "scripts/lint-style.sh" in c["argv"])
        build = next(i for i, c in enumerate(commands) if c["argv"] == ["lake", "build", "--iofail"])
        self.assertLess(style, build)

    def test_completed_phase_indexes_even_when_deadline_arrives_at_its_boundary(self):
        verification = v.Verification(self.root, self.state, stage="all")
        command = verification.command

        def completes(phase, *args, **kwargs):
            output = command(phase, *args, **kwargs)
            if phase == "build":
                verification.deadline = time.time() - 1
            return output

        with patch.object(verification, "command", side_effect=completes), contextlib.redirect_stderr(io.StringIO()):
            result = verification.run()
        self.assertEqual(result["result_type"], "pending")
        self.assertEqual(result["phases"]["build"]["status"], "success")
        self.assertIn("reused_from", self.verify(stage="build")["phases"]["build"])

    def test_snapshot_preserves_build_caches_and_live_index(self):
        cache = write(self.root, ".lake/build/lib/Candidate.olean", "candidate build output")
        dependency = write(self.root, ".lake/packages/mathlib/.lake/build/lib/Mathlib.olean", "dependency build output")
        index = self.root / ".git/index"
        before = index.read_bytes()
        result = self.verify(stage="build", deadline=time.time() - 1)
        job = v.read_json(Path(result["continuation_job"]))
        target = Path(job["root"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((target / cache.relative_to(self.root)).read_bytes(), cache.read_bytes())
        self.assertEqual((target / dependency.relative_to(self.root)).read_bytes(), dependency.read_bytes())
        duplicate = self.verify(stage="build")
        self.assertEqual(duplicate["result_type"], "pending")
        self.assertEqual(duplicate["continuation_job"], result["continuation_job"])
        self.assertEqual(len(list((self.state / "verification-pending").glob("*.json"))), 1)

    def test_missing_tools_wait_for_real_availability_change_then_resume(self):
        import shutil

        which = shutil.which
        with patch("shutil.which", side_effect=lambda name: None if name == "lean" else which(name)):
            result = self.verify(stage="build")
            self.assertEqual(result["result_type"], "pending")
            path = Path(result["continuation_job"])
            job = v.read_json(path)
            self.assertEqual(job["status"], "blocked-tools")
            job["next_at"] = 0
            v.atomic(path, job)
            self.assertFalse(resume_pending(SimpleNamespace(state=self.state)))
        self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        complete = v.read_json(self.state / "verification-jobs" / path.name)
        self.assertEqual(complete["status"], "success")
        with contextlib.redirect_stderr(io.StringIO()):
            reused = v.Verification(Path(complete["root"]), self.state, base=complete["base"], stage="build").run()
        self.assertIn("reused_from", reused["phases"]["build"])

    def test_external_source_symlink_preservation_fails_incomplete_without_source_loss(self):
        outside = write(self.home, "external.lean", "external source")
        (self.root / "TauCeti/Linked.lean").symlink_to(outside)
        result = self.verify(stage="build", deadline=time.time() - 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "preservation")
        self.assertIn("ownership", result["error"])
        self.assertEqual(outside.read_text(), "external source")
        self.assertTrue((self.root / "TauCeti/Linked.lean").is_symlink())

    def test_partial_phase_survives_next_deadline(self):
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

    def test_supervisor_continues_after_fleet_admission_before_inference_or_github(self):
        from tauceti_worker import loop

        with (
            patch("tauceti_worker.verification_jobs.resume_pending", side_effect=KeyboardInterrupt) as resume,
            patch.object(loop, "fleet_admission_reason", return_value=None) as admission,
            patch.object(loop, "github_budget") as github,
            patch.object(loop, "choose_model") as model,
        ):
            loop.cmd_loop(
                SimpleNamespace(), SimpleNamespace(state=self.state, wid="fixture"), only=["fix"], agent="claude"
            )
        resume.assert_called_once()
        admission.assert_called_once()
        github.assert_not_called()
        model.assert_not_called()

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
        metrics = dict(files=0, bytes=0, walks=0, stats=0, statvfs=0, mountinfo_reads=0, mountinfo_bytes=0)

        def files(root):
            metrics["walks"] += 1
            yield from v.files_under(root)

        def record(path):
            metrics["files"] += 1
            metrics["bytes"] += path.stat().st_size
            return v.file_record(path)

        real_stat, real_lstat, real_read = os.stat, os.lstat, Path.read_text

        def readonly(path):
            metrics["statvfs"] += 1
            return SimpleNamespace(f_flag=os.ST_RDONLY)

        def read_text(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if str(path) == "/proc/self/mountinfo":
                metrics["mountinfo_reads"] += 1
                metrics["mountinfo_bytes"] += len(value.encode())
            return value

        def counted(function):
            def call(path, *args, **kwargs):
                if str(path).startswith(str(large)):
                    metrics["stats"] += 1
                return function(path, *args, **kwargs)

            return call

        def installations(state=None):
            return Installations(
                state or self.state,
                digest=v.digest,
                atomic=v.atomic,
                read_json=v.read_json,
                files_under=files,
                file_record=record,
                stamps=v.stamps,
                cache_directory=lambda: v.cache_root() / "installations",
            )

        # Fixture-only read-only mount identity. Real mutable directories never take this path.
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("os.stat", side_effect=counted(real_stat)),
            patch("os.lstat", side_effect=counted(real_lstat)),
            patch("os.statvfs", side_effect=readonly),
            patch.object(Path, "read_text", read_text),
        ):
            cold = installations().inventory(large)
            self.assertEqual(metrics["bytes"], 122880000)
            cold_metrics = dict(metrics)
            metrics.update({key: 0 for key in metrics})
            # Unrelated historical receipts must have no effect on preparation.
            for index in range(2000):
                write(self.state, f"verification/history-{index}/receipt.json", "{}")

            def contender(index):
                installation = installations(self.home / f"worker-{index}-state")
                identity = installation.inventory(large)
                installation.validate()
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
            self.assertEqual(metrics["statvfs"], 40)
            self.assertEqual(metrics["mountinfo_reads"], 40)
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
            with (
                patch("tauceti_worker.verification.files_under", side_effect=AssertionError("warm immutable walk")),
                patch("tauceti_worker.verification_jobs.snapshot", side_effect=AssertionError("warm candidate copy")),
                patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"),
            ):
                second = self.verify(stage="build")
            self.assertIn("reused_from", second["phases"]["build"])
            self.assertEqual(self.verify(stage="all")["status"], "success")
            write(self.prefix, "bin/lean", "publisher replaced toolchain")
            v.atomic(manifest, {str(self.prefix): "lean-2", str(deps): "mathlib-1"})
            third = self.verify(stage="build")
            self.assertNotIn("reused_from", third["phases"]["build"])

    def test_v0_dispatch_default_and_explicit_opt_in(self):
        from tauceti_worker.agents import local_verification

        with patch.dict(os.environ):
            os.environ.pop("TAUCETI_VERIFY_HELPER", None)
            self.assertNotIn("tauceti-verify", local_verification("ordinary"))
            for value in ("", "false", "no", "0"):
                os.environ["TAUCETI_VERIFY_HELPER"] = value
                self.assertNotIn("tauceti-verify", local_verification("ordinary"))
            os.environ["TAUCETI_VERIFY_HELPER"] = "1"
            self.assertIn("tauceti-verify", local_verification("ordinary"))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(Recovery(name) for name in Recovery.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
