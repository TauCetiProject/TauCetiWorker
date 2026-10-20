#!/usr/bin/env python3
"""Exact receipts, failure propagation and host queue with local Git/fake Lean fixtures only."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.verification import (
    Verification,
    atomic,
    begin_attempt,
    heavy_slots,
    inventory,
    queue_root,
    refund_capacity_wait,
)
from tauceti_worker.verification_scope import local_main, scope

REPO = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def write(root, path, value):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value)
    return p


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "source"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.org")
        for name in ("TauCeti/A.lean", "TauCeti/B.lean", "lean-toolchain", "lake-manifest.json", "lakefile.toml"):
            write(self.root, name, "fixture\n")
        write(self.root, ".gitignore", ".lake/\nTauCeti/Ignored.lean\n")
        write(self.root, "scripts/lint-env.sh", 'echo "LINT-ENV: PASS"\n')
        write(self.root, "scripts/lint-style.sh", 'echo "LINT-STYLE: PASS"\n')
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        git(self.root, "remote", "add", "origin", "https://github.com/TauCetiProject/TauCeti.git")
        git(self.root, "update-ref", "refs/remotes/origin/main", self.base)
        self.prefix = self.home / "toolchain"
        write(self.prefix, "bin/lean", "toolchain bytes")
        self.bin = self.home / "bin"
        fake = write(
            self.bin,
            "lake",
            f"""#!{sys.executable}
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ['--version'] or '--version' in args:
    print('fixture Lean/Lake version 1'); sys.exit(0)
if '--print-prefix' in args:
    print({str(self.prefix)!r}); sys.exit(0)
p = Path('.lake'); p.mkdir(exist_ok=True)
with (p/'calls').open('a') as f: f.write(' '.join(args)+' AXIOMS='+os.environ.get('AXIOMS_ONLY_MODULES','')+'\\n')
mode = os.environ.get('FIXTURE_MODE', '')
if mode == 'fail': sys.exit(23)
if mode == 'mutation':
    source = Path('TauCeti/A.lean'); before = source.read_text(); source.write_text('changed'); source.write_text(before)
if mode == 'sleep':
    (p/'tool.pid').write_text(str(os.getpid()))
    time.sleep(float(os.environ.get('FIXTURE_SLEEP', '1.5')))
if os.environ.get('AUDIT_PHASES_FILE'):
    Path(os.environ['AUDIT_PHASES_FILE']).write_text('audit-import\\t1\\naudit-duplicates\\t2\\naudit-axioms\\t3\\n')
print('imports timing 1ms; duplicates timing 2ms; axioms timing 3ms', flush=True)
""",
        )
        fake.chmod(0o755)
        for name in ("lean", "gh"):
            p = write(self.bin, name, "#!/bin/sh\nexit 97\n")
            p.chmod(0o755)
        self.state = self.home / "state"
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TAUCETI_ACTIVITY_DIR=str(self.home / "activity"),
            TAUCETI_FLEET_EVENTS=str(self.home / "worker-events.jsonl"),
            TAUCETI_VERIFY_HELPER="1",
            TAUCETI_PROCESS_CONTAINMENT="legacy",
            TAUCETI_VERIFY_STATE=str(self.state),
            TAUCETI_VERIFY_HOST_QUEUE=str(self.home / "queue"),
            TAUCETI_VERIFY_HEAVY_SLOTS="1",
            TAUCETI_VERIFY_BASE=self.base,
            TAUCETI_VERIFY_CHECKPOINT_MARGIN="0",
        )
        self.environ = patch.dict(os.environ, self.env, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def verify(self, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            result = Verification(self.root, self.state, **kwargs).run()
        return result

    def calls(self):
        return (self.root / ".lake/calls").read_text().splitlines()

    def spawn(self, **updates):
        return subprocess.Popen(
            [sys.executable, str(REPO / "scripts/tauceti-verify"), "--stage", "build", "--json"],
            cwd=self.root,
            env={**self.env, **updates},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def wait_calls(self, p):
        limit = time.monotonic() + 10
        while not (self.root / ".lake/calls").exists():
            self.assertIsNone(p.poll())
            self.assertLess(time.monotonic(), limit)
            time.sleep(0.02)

    def test_success_reuse_and_phase_reuse(self):
        first = self.verify(stage="build")
        self.assertEqual(first["status"], "success", first.get("error"))
        second = self.verify(stage="all")
        self.assertEqual(second["status"], "success", second.get("error"))
        self.assertIn("reused_from", second["phases"]["build"])
        self.assertEqual(sum(c.startswith("build") for c in self.calls()), 1)
        self.assertTrue(any(e["audit_timings"] for e in second["commands"]))
        self.assertEqual(second["audit_phase_timings"][0]["phase"], "audit-import")
        self.assertEqual(second["audit_phase_timings"][0]["seconds"], 1)
        third = self.verify(stage="all")
        self.assertTrue(all("reused_from" in e for e in third["phases"].values()))
        fourth = self.verify(stage="build", reuse=False)
        self.assertNotIn("reused_from", fourth["phases"]["build"])

    def test_failure_and_pipeline(self):
        os.environ["FIXTURE_MODE"] = "fail"
        result = self.verify(stage="build")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["commands"][-1]["exit"], 23)
        os.environ.pop("FIXTURE_MODE")
        write(self.root, "scripts/lint-style.sh", "false | cat\n")
        result = self.verify(stage="lint")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["commands"][-1]["exit"], 1)

    def test_change_then_restore_during_execution(self):
        os.environ["FIXTURE_MODE"] = "mutation"
        result = self.verify(stage="build")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("mutat", result["error"])

    def test_source_pin_base_and_toolchain_invalidate(self):
        self.assertEqual(self.verify(stage="build")["status"], "success")
        for path in ("TauCeti/A.lean", "lake-manifest.json", "lean-toolchain", "lakefile.toml"):
            write(self.root, path, "changed\n")
            result = self.verify(stage="build")
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertNotIn("reused_from", result["phases"]["build"])
        write(self.prefix, "bin/lean", "new toolchain bytes")
        self.assertNotIn("reused_from", self.verify(stage="build")["phases"]["build"])
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "new base")
        new_base = git(self.root, "rev-parse", "HEAD")
        result = self.verify(stage="build", base=new_base)
        self.assertNotIn("reused_from", result["phases"]["build"])
        write(self.root, ".lake/packages/mathlib/Mathlib/A.lean", "dependency")
        self.assertNotIn("reused_from", self.verify(stage="build", base=new_base)["phases"]["build"])

    def test_deletion_rename_untracked_ignored(self):
        original = inventory(self.root, self.base)
        (self.root / "TauCeti/A.lean").unlink()
        self.assertEqual(scope(self.root, self.base, "ordinary")["mode"], "full")
        self.assertEqual(inventory(self.root, self.base)["TauCeti/A.lean"]["kind"], "missing")
        (self.root / "TauCeti/B.lean").rename(self.root / "TauCeti/New.lean")
        write(self.root, "TauCeti/Ignored.lean", "ignored source")
        current = inventory(self.root, self.base)
        self.assertNotEqual(original, current)
        self.assertIn("TauCeti/New.lean", current)
        self.assertIn("TauCeti/Ignored.lean", current)
        self.assertIn("TauCeti.New", scope(self.root, self.base, "ordinary")["modules"])
        self.assertEqual(local_main(self.root), self.base)

    def test_queue_contention_deadline_and_owner_death(self):
        p = self.spawn(FIXTURE_MODE="sleep", FIXTURE_SLEEP="10")
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.wait_calls(p)
        # Another worker has its own durable state, but shares the actual host queue.
        q = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker2"), TAUCETI_ROUND_DEADLINE=str(time.time() + 3))
        out, _ = q.communicate(timeout=10)
        result = json.loads(out)
        self.assertNotEqual(q.returncode, 0)
        self.assertTrue(result["waiting_for_capacity"])
        self.assertIn("deadline", result["error"])
        self.assertEqual(len(self.calls()), 1)
        p.kill()
        p.communicate(timeout=10)
        owned_tool = int((self.root / ".lake/tool.pid").read_text())
        os.kill(owned_tool, signal.SIGKILL)
        receipt = json.loads(next(self.state.glob("verification/*/receipt.json")).read_text())
        self.assertEqual(receipt["status"], "incomplete")
        # An owner killed without Python cleanup releases its flock immediately.
        q = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker3"), TAUCETI_ROUND_DEADLINE=str(time.time() + 3))
        out, _ = q.communicate(timeout=10)
        self.assertEqual(q.returncode, 0, out)

    def test_deadline_interrupts_child_and_sigterm(self):
        p = self.spawn(FIXTURE_MODE="sleep", FIXTURE_SLEEP="10", TAUCETI_ROUND_DEADLINE=str(time.time() + 2))
        out, _ = p.communicate(timeout=10)
        result = json.loads(out)
        self.assertEqual(result["status"], "incomplete")
        self.assertLess(result["commands"][-1]["exit"], 0)
        (self.root / ".lake/calls").unlink()
        p = self.spawn(FIXTURE_MODE="sleep", TAUCETI_VERIFY_STATE=str(self.home / "sigterm-state"))
        self.wait_calls(p)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(json.loads(out)["status"], "incomplete")

    def test_verifier_and_executable_invalidation(self):
        self.assertEqual(self.verify(stage="build")["status"], "success")
        with patch("tauceti_worker.verification.POLICY_VERSION", 999):
            result = self.verify(stage="build")
            self.assertNotIn("reused_from", result["phases"]["build"])
        lake = self.bin / "lake"
        lake.write_text(lake.read_text() + "\n# executable changed\n")
        result = self.verify(stage="build")
        self.assertNotIn("reused_from", result["phases"]["build"])

    def test_durable_capacity_refund_is_idempotent(self):
        values = {"fix-1": 3}
        counters = SimpleNamespace(read=lambda key: values[key], write=lambda key, value: values.update({key: value}))
        worker = SimpleNamespace(cfg=SimpleNamespace(state=self.state), counters=counters)
        callback = begin_attempt(worker, ("fix-1",))
        atomic(
            self.state / "verification-capacity.json",
            dict(attempt=os.environ["TAUCETI_VERIFY_ATTEMPT"], waiting=False, interrupted=True, deadline=True),
        )
        self.assertTrue(callback())
        self.assertEqual(values["fix-1"], 2)
        self.assertFalse(refund_capacity_wait(self.state, counters))
        self.assertEqual(values["fix-1"], 2)

    def test_two_workers_run_serially(self):
        first = self.spawn(FIXTURE_MODE="sleep")
        self.wait_calls(first)
        started = time.monotonic()
        second = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker2"), FIXTURE_MODE="sleep")
        out1, _ = first.communicate(timeout=10)
        out2, _ = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, out1)
        self.assertEqual(second.returncode, 0, out2)
        result = json.loads(out2)
        self.assertGreater(result["queue_wait_seconds"], 0.7)
        self.assertGreater(time.monotonic() - started, 2.5)

    def test_shim_ratchet_and_one_json_result(self):
        write(self.root, "TauCeti/mathlib-shims.json", "{}")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "registry")
        base = git(self.root, "rev-parse", "HEAD")
        write(
            self.root,
            "scripts/check-expired-mathlib-shims.py",
            "import json, sys\nfrom pathlib import Path\nPath('.lake').mkdir(exist_ok=True)\nPath('.lake/shim-args').write_text(json.dumps(sys.argv[1:]))\n",
        )
        result = self.verify(stage="shim-expiry", base=base)
        self.assertEqual(result["status"], "success", result.get("error"))
        args = json.loads((self.root / ".lake/shim-args").read_text())
        for expected in ("--fail-on-available", "--base-manifest", "--base-root", "--only-new"):
            self.assertIn(expected, args)
        write(self.root, "lean-toolchain", "pin change")
        self.verify(stage="shim-expiry", base=base)
        self.assertNotIn("--only-new", json.loads((self.root / ".lake/shim-args").read_text()))

    def test_strong_containment_when_available(self):
        from tauceti_worker.containment import capability

        with patch.dict(os.environ, TAUCETI_PROCESS_CONTAINMENT="auto"):
            if not capability().strong:
                self.skipTest("rootless namespace unavailable")
            result = self.verify(stage="build")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertTrue(all(c["containment"]["strong"] for c in result["commands"]))

    def test_refused_containment_leaves_json_incomplete(self):
        p = self.spawn(TAUCETI_PROCESS_CONTAINMENT="invalid")
        output, _ = p.communicate(timeout=10)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(output)["status"], "incomplete")

    def test_stale_wait_does_not_refund_normal_completion(self):
        values = {"fix-1": 3}
        counters = SimpleNamespace(read=lambda key: values[key], write=lambda key, value: values.update({key: value}))
        worker = SimpleNamespace(cfg=SimpleNamespace(state=self.state), counters=counters)
        callback = begin_attempt(worker, ("fix-1",))
        lease = self.state / "dead.lock"
        lease.touch()
        atomic(
            self.state / "verification-capacity.json",
            dict(attempt=os.environ["TAUCETI_VERIFY_ATTEMPT"], waiting=True, lease="dead.lock", at=time.time()),
        )
        self.assertFalse(callback())
        self.assertEqual(values["fix-1"], 3)
        self.assertFalse(refund_capacity_wait(self.state, counters, interrupted_round=True))

    def test_new_reused_invocation_clears_old_wait(self):
        first = self.verify(stage="build")
        self.assertEqual(first["status"], "success", first.get("error"))
        atomic(self.state / "verification-capacity.json", dict(waiting=True, interrupted=True, deadline=True))
        second = self.verify(stage="build")
        self.assertIn("reused_from", second["phases"]["build"])
        self.assertFalse(json.loads((self.state / "verification-capacity.json").read_text())["waiting"])

    def test_explicit_lint_modules_are_bound_inputs(self):
        modules = write(self.home, "lint-modules.txt", "TauCeti.B\n")
        write(self.root, "scripts/lint-env.sh", 'cat "$LINT_ONLY_MODULES"\n')
        result = self.verify(stage="lint", lint_modules=modules)
        self.assertEqual(result["status"], "success", result.get("error"))
        lint = next(c for c in result["commands"] if "scripts/lint-env.sh" in c["argv"])
        self.assertIn("TauCeti.B", Path(lint["log"]).read_text())
        modules.write_text("TauCeti.A\n")
        result = self.verify(stage="lint", lint_modules=modules)
        self.assertNotIn("reused_from", result["phases"]["lint"])

    def test_bubble_checkpoint_survives_incomplete_verification(self):
        write(self.root, "TauCeti/A.lean", "candidate edit\n")
        write(self.root, "TauCeti/Untracked.lean", "untracked edit\n")
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1", TAUCETI_VERIFY_CHECKPOINT_MARGIN="10"):
            result = self.verify(stage="build", deadline=time.time() + 1)
        self.assertEqual(result["status"], "incomplete")
        checkpoint = Path(result["source_checkpoint"])
        self.assertTrue(checkpoint.is_file())
        self.assertIn("candidate edit", checkpoint.with_name("source.patch").read_text())
        import tarfile

        with tarfile.open(checkpoint.with_name("untracked.tar")) as archive:
            self.assertEqual(archive.extractfile("TauCeti/Untracked.lean").read(), b"untracked edit\n")

    def test_content_cache_rechecks_changed_ctime(self):
        self.verify(stage="build")
        before = json.loads((self.state / "verification-hashes.json").read_text())
        write(self.root, "TauCeti/A.lean", "changed input\n")
        result = self.verify(stage="build")
        after = json.loads((self.state / "verification-hashes.json").read_text())
        self.assertGreater(len(after), len(before))
        self.assertNotIn("reused_from", result["phases"]["build"])
        self.assertIn("path", result["inputs"])

    def test_bubble_wrapper_layout_in_python_safe_path_mode(self):
        import shutil

        layout = self.home / "round"
        layout.mkdir()
        shutil.copy(REPO / "scripts/tauceti-verify", layout / "tauceti-verify")
        shutil.copytree(
            REPO / "tauceti_worker", layout / "tauceti_worker", ignore=shutil.ignore_patterns("__pycache__")
        )
        result = subprocess.run(
            [sys.executable, "-P", str(layout / "tauceti-verify"), "--help"],
            cwd=self.home,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--lint-modules", result.stdout)

    def test_host_wrapper_uses_worker_python_and_long_timeout(self):
        from tauceti_worker.agents import host_agent_argv, local_verification

        with patch.dict(os.environ, TAUCETI_ROUND_DEADLINE=str(time.time() + 1800)):
            _, env = host_agent_argv("fixture prompt", "claude")
        self.assertEqual(env["TAUCETI_VERIFY_PYTHON"], sys.executable)
        self.assertGreater(int(env["BASH_MAX_TIMEOUT_MS"]), 1000000)
        self.assertIn(sys.executable, local_verification("ordinary"))

    def test_queued_unmanaged_round_is_detected_from_local_journal(self):
        from tauceti_worker.verification import waiting_for_capacity

        marker = self.state / "verification-capacity.json"
        atomic(marker, dict(waiting=True, at=time.time(), lease="dead.lock"))
        self.assertTrue(waiting_for_capacity(SimpleNamespace(state=self.state), since=time.time() - 1))
        self.assertFalse(waiting_for_capacity(SimpleNamespace(state=self.state), since=time.time() + 1))

    def test_loop_capacity_wait_skips_api_budget_and_no_work_backoff(self):
        from tauceti_worker import loop
        from tauceti_worker.constants import EX_VERIFICATION_WAIT

        for code in (124, EX_VERIFICATION_WAIT):

            def round_result(_, code=code):
                atomic(
                    self.state / "verification-state/verification-capacity.json",
                    dict(waiting=True, at=time.time(), lease="dead.lock"),
                )
                return code

            with (
                patch.object(loop, "fleet_admission_reason", return_value=None),
                patch.object(loop, "api_mode", return_value=False),
                patch.object(loop, "choose_model", return_value=("claude", {})),
                patch.object(loop, "github_budget", return_value={}),
                patch.object(loop, "run_round_subprocess", side_effect=round_result),
                patch.object(loop, "Budget") as budget,
                patch.object(loop, "report_runtime") as status,
                patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt) as sleep,
            ):
                loop.cmd_loop(
                    SimpleNamespace(bubble=True),
                    SimpleNamespace(wid="fixture", state=self.state),
                    only=["fix"],
                    agent="claude",
                )
            budget.assert_not_called()
            sleep.assert_called_once_with(5)
            self.assertIn(
                "waiting-verification-capacity", [call.args[0] for call in status.call_args_list if call.args]
            )

    def test_cli_capacity_exit_and_installed_wrapper_permission(self):
        from tauceti_worker import cli
        from tauceti_worker.config import VerificationUnavailable
        from tauceti_worker.constants import EX_VERIFICATION_WAIT

        with (
            patch.object(cli, "main", side_effect=VerificationUnavailable("fixture wait")),
            patch.object(cli, "report_runtime"),
        ):
            self.assertEqual(cli.cli_main(), EX_VERIFICATION_WAIT)
        package = self.home / "installed"
        wrapper = write(package, "scripts/tauceti-verify", "fixture")
        wrapper.chmod(0o644)
        with patch.object(cli, "HERE", package):
            cli._ensure_scripts_executable()
        self.assertTrue(os.access(wrapper, os.X_OK))

    def test_deleted_renamed_and_untracked_sources_invalidate_reuse(self):
        self.assertEqual(self.verify(stage="build")["status"], "success")
        (self.root / "TauCeti/A.lean").unlink()
        for change in (
            lambda: None,
            lambda: (self.root / "TauCeti/B.lean").rename(self.root / "TauCeti/Renamed.lean"),
            lambda: write(self.root, "TauCeti/New.lean", "new"),
            lambda: write(self.root, "TauCeti/Ignored.lean", "ignored"),
        ):
            change()
            result = self.verify(stage="build")
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertNotIn("reused_from", result["phases"]["build"])

    def test_unknown_scope_and_missing_checker_fail_closed(self):
        write(self.root, ".git/shallow", self.base + "\n")
        build = self.verify(stage="build")
        self.assertEqual(build["status"], "success", build.get("error"))
        self.assertEqual(build["scope"]["mode"], "unknown")
        result = self.verify(stage="all")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("reused_from", result["phases"]["build"])
        (self.root / ".git/shallow").unlink()
        write(self.root, "TauCeti/mathlib-shims.json", "{}")
        result = self.verify(stage="shim-expiry")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("checker is missing", result["error"])

    def test_slot_configuration(self):
        self.assertEqual(heavy_slots(), 1)
        self.assertIn("queue", str(queue_root()))
        for bad in ("0", "-1", "two", "1.5"):
            os.environ["TAUCETI_VERIFY_HEAVY_SLOTS"] = bad
            with self.assertRaises(ValueError):
                heavy_slots()


if __name__ == "__main__":
    unittest.main()
