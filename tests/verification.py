#!/usr/bin/env python3
"""Exact receipts, failure propagation and concurrent execution with local Git/fake Lean fixtures only."""

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_cleanup import running

from tauceti_worker.verification import (
    Verification,
    atomic,
    inventory,
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
        lean = write(
            self.prefix,
            "bin/lean",
            f"""#!{sys.executable}
import sys
from pathlib import Path
if sys.argv[1:] == ['--print-prefix']: print({str(self.prefix)!r})
else: Path('Main.c').write_text('fixture compiler output')
""",
        )
        lean.chmod(0o755)
        leanc = write(
            self.prefix,
            "bin/leanc",
            f"""#!{sys.executable}
import sys
from pathlib import Path
Path(sys.argv[sys.argv.index('-o') + 1]).write_text({f"#!{sys.executable}"!r} + '\\nimport sys\\nfrom pathlib import Path\\nprint("local-axioms: fixture")\\nsys.exit(1 if Path(".lake/axiom-invalid").exists() else 0)\\n')
Path(sys.argv[sys.argv.index('-o') + 1]).chmod(0o755)
""",
        )
        leanc.chmod(0o755)
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
if mode == 'bad-axiom-after-build' and args[0] == 'build': (p/'axiom-invalid').touch()
if mode == 'mutation':
    source = Path('TauCeti/A.lean'); before = source.read_text(); source.write_text('changed'); source.write_text(before)
if mode == 'sleep':
    (p/'tool.pid').write_text(str(os.getpid()))
    time.sleep(float(os.environ.get('FIXTURE_SLEEP', '1.5')))
if mode == 'barrier':
    barrier = Path(os.environ['FIXTURE_BARRIER'])
    (barrier / ('ready-' + Path(os.environ['TAUCETI_VERIFY_STATE']).name)).touch()
    deadline = time.monotonic() + 40
    while not (barrier / 'release').exists():
        if time.monotonic() > deadline: sys.exit(98)
        time.sleep(0.02)
if os.environ.get('AUDIT_PHASES_FILE'):
    Path(os.environ['AUDIT_PHASES_FILE']).write_text('audit-import\\t1\\naudit-duplicates\\t2\\naudit-axioms\\t3\\n')
print('imports timing 1ms; duplicates timing 2ms; axioms timing 3ms', flush=True)
""",
        )
        fake.chmod(0o755)
        for name in ("gh",):
            p = write(self.bin, name, "#!/bin/sh\nexit 97\n")
            p.chmod(0o755)
        (self.bin / "lean").symlink_to(lean)
        self.state = self.home / "state"
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TAUCETI_ACTIVITY_DIR=str(self.home / "activity"),
            TAUCETI_FLEET_EVENTS=str(self.home / "worker-events.jsonl"),
            TAUCETI_VERIFY_HELPER="1",
            TAUCETI_PROCESS_CONTAINMENT="invalid-old-policy",
            TAUCETI_VERIFY_STATE=str(self.state),
            TAUCETI_VERIFY_CACHE_DIR=str(self.home / "cache"),
            TAUCETI_VERIFY_BASE=self.base,
            TAUCETI_VERIFY_CHECKPOINT_MARGIN="0",
            TAUCETI_LOCAL_AXIOMS_CACHE=str(self.home / "local-axioms-cache"),
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

    def spawn(self, *, root=None, **updates):
        return subprocess.Popen(
            [sys.executable, str(REPO / "scripts/tauceti-verify"), "--stage", "build", "--json"],
            cwd=root or self.root,
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
        self.assertEqual(second["audit_phase_timings"], [])  # No imported environment in local checks.
        audit = next(c for c in second["commands"] if c["phase"] == "axioms")
        self.assertTrue(audit["argv"][1].endswith("scripts/tauceti-local-axioms"))
        third = self.verify(stage="all")
        for name, phase in third["phases"].items():
            self.assertEqual("reused_from" in phase, name not in {"axioms", "module-system"})
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

    def test_local_axioms_never_initialize_lake_or_fall_back_to_full(self):
        write(self.root, "TauCeti/A.lean", "changed")
        write(self.root, "lean-toolchain", "changed pin")
        result = self.verify(stage="axioms", policy="bump")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(result["scope"]["mode"], "full")  # Full CI requirement, never full local import.
        audit = next(c for c in result["commands"] if c["phase"] == "axioms")
        self.assertIn("tauceti-local-axioms", audit["argv"][1])
        self.assertFalse((self.root / ".lake/calls").exists())  # Not even Lake startup on this path.
        self.assertNotIn("queue_wait_seconds", result)
        write(self.root, ".git/shallow", self.base + "\n")
        result = self.verify(stage="axioms")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("changed modules unknown", result["error"])
        self.assertFalse(any(c["phase"] == "axioms" for c in result["commands"]))

    def test_stage_only_axiom_success_cannot_hide_new_build_artifacts(self):
        write(self.root, "TauCeti/A.lean", "custom axiom added after the previous build")
        os.environ["FIXTURE_MODE"] = "bad-axiom-after-build"
        initial = self.verify(stage="axioms")
        self.assertEqual(initial["status"], "success", initial.get("error"))
        final = self.verify(stage="all")
        self.assertEqual(final["status"], "failed", final.get("error"))
        self.assertEqual(final["phases"]["build"]["status"], "success")
        self.assertNotIn("reused_from", final["phases"]["axioms"])
        self.assertEqual(final["commands"][-1]["exit"], 1)

    def test_local_checker_compile_failure_is_incomplete_not_a_source_violation(self):
        write(self.root, "TauCeti/A.lean", "changed")
        compiler = self.prefix / "bin/leanc"
        compiler.write_text(compiler.read_text().replace("import sys", "import sys; sys.exit(23)"))
        result = self.verify(stage="axioms")
        self.assertEqual(result["status"], "incomplete", result.get("error"))
        self.assertEqual(result["incomplete_reason"], "tool-missing")
        self.assertEqual(result["commands"][-1]["exit"], 75)

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

    def test_owner_death_leaves_incomplete_receipt_and_recoverable_job(self):
        p = self.spawn(FIXTURE_MODE="sleep", FIXTURE_SLEEP="60")
        try:
            self.wait_calls(p)
            limit = time.monotonic() + 10
            while not (self.root / ".lake/tool.pid").exists():
                self.assertIsNone(p.poll())
                self.assertLess(time.monotonic(), limit)
                time.sleep(0.02)
            owned_tool = int((self.root / ".lake/tool.pid").read_text())
            p.kill()
            p.communicate(timeout=10)
            limit = time.monotonic() + 5
            while running(owned_tool) and time.monotonic() < limit:
                time.sleep(0.02)
            self.assertFalse(running(owned_tool), "verification tool survived its owner")
            receipt = json.loads(next(self.state.glob("verification/*/receipt.json")).read_text())
            self.assertEqual(receipt["status"], "incomplete")
            self.assertEqual(len(list((self.state / "verification-pending").glob("*.json"))), 1)
            q = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker2"))
            out, _ = q.communicate(timeout=10)
            self.assertEqual(q.returncode, 0, out)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=10)

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

    def test_twenty_independent_verifiers_enter_build_together_despite_old_slot(self):
        import fcntl
        import shutil

        barrier = self.home / "barrier"
        barrier.mkdir()
        old_queue = self.home / "obsolete-queue"
        old_queue.mkdir()
        processes = []
        with (old_queue / "slot-0.lock").open("a+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            try:
                for index in range(20):
                    root = self.home / f"source-{index}"
                    shutil.copytree(self.root, root)
                    state = self.home / f"worker-{index}"
                    # Old deployment variables and stale waits cannot throttle new verification.
                    atomic(state / "verification-capacity.json", dict(waiting=True, interrupted=True))
                    processes.append(
                        self.spawn(
                            root=root,
                            TAUCETI_VERIFY_STATE=str(state),
                            TAUCETI_VERIFY_HOST_QUEUE=str(old_queue),
                            TAUCETI_VERIFY_HEAVY_SLOTS="1",
                            FIXTURE_MODE="barrier",
                            FIXTURE_BARRIER=str(barrier),
                        )
                    )
                limit = time.monotonic() + 30
                while len(list(barrier.glob("ready-*"))) != len(processes):
                    self.assertTrue(all(p.poll() is None for p in processes))
                    self.assertLess(time.monotonic(), limit, "independent builds were serialized")
                    time.sleep(0.02)
                (barrier / "release").touch()
                for p in processes:
                    out, _ = p.communicate(timeout=20)
                    self.assertEqual(p.returncode, 0, out)
                    result = json.loads(out)
                    self.assertEqual(result["status"], "success")
                    self.assertNotIn("queue_wait_seconds", result)
                    self.assertNotIn("waiting_for_capacity", result)
                self.assertFalse(list(old_queue.glob("wait-*")))
            finally:
                (barrier / "release").touch()
                for p in processes:
                    if p.poll() is None:
                        p.kill()
                    p.communicate(timeout=10)

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

    def test_old_cleanup_markers_do_not_block_verification(self):
        markers = self.home / "activity/containment-unconfirmed"
        markers.mkdir(parents=True)
        (markers / "malformed.json").write_text("{broken")
        (markers / "stale.json").write_text('{"pid_namespace": 42, "boundary": "old"}')
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "quiescence.json").write_text("not json")
        result = self.verify(stage="build")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertTrue(all("containment" not in command for command in result["commands"]))
        self.assertEqual((markers / "malformed.json").read_text(), "{broken")
        self.assertEqual((self.state / "quiescence.json").read_text(), "not json")

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
        before = json.loads((self.state / "verification-hashes-v2.json").read_text())["entries"]
        write(self.root, "TauCeti/A.lean", "changed input\n")
        result = self.verify(stage="build")
        after = json.loads((self.state / "verification-hashes-v2.json").read_text())["entries"]
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

    def test_installed_wrapper_permission(self):
        from tauceti_worker import cli

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

    def test_obsolete_slot_settings_do_not_restrict_or_reject_verification(self):
        for value in ("1", "0", "invalid"):
            with patch.dict(os.environ, TAUCETI_VERIFY_HEAVY_SLOTS=value):
                result = self.verify(stage="build", reuse=False)
            self.assertEqual(result["status"], "success", result.get("error"))
        self.assertFalse((self.state / "verification-capacity.json").exists())
        self.assertFalse((self.state / "verification-attempt.json").exists())


if __name__ == "__main__":
    unittest.main()
