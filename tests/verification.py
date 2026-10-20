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
if mode == 'sleep': time.sleep(1.5)
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
            TAUCETI_PROCESS_CONTAINMENT="legacy",
            TAUCETI_VERIFY_STATE=str(self.state),
            TAUCETI_VERIFY_HOST_QUEUE=str(self.home / "queue"),
            TAUCETI_VERIFY_HEAVY_SLOTS="1",
            TAUCETI_VERIFY_BASE=self.base,
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
        p = self.spawn(FIXTURE_MODE="sleep")
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.wait_calls(p)
        # Another worker has its own durable state, but shares the actual host queue.
        q = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker2"), TAUCETI_ROUND_DEADLINE=str(time.time() + 0.4))
        out, _ = q.communicate(timeout=10)
        result = json.loads(out)
        self.assertNotEqual(q.returncode, 0)
        self.assertTrue(result["waiting_for_capacity"])
        self.assertIn("deadline", result["error"])
        self.assertEqual(len(self.calls()), 1)
        p.kill()
        p.communicate(timeout=10)
        receipt = json.loads(next(self.state.glob("verification/*/receipt.json")).read_text())
        self.assertEqual(receipt["status"], "incomplete")
        # An owner killed without Python cleanup releases its flock immediately.
        q = self.spawn(TAUCETI_VERIFY_STATE=str(self.home / "worker3"), TAUCETI_ROUND_DEADLINE=str(time.time() + 3))
        out, _ = q.communicate(timeout=10)
        self.assertEqual(q.returncode, 0, out)

    def test_deadline_interrupts_child_and_sigterm(self):
        p = self.spawn(FIXTURE_MODE="sleep", TAUCETI_ROUND_DEADLINE=str(time.time() + 0.4))
        out, _ = p.communicate(timeout=10)
        result = json.loads(out)
        self.assertEqual(result["status"], "incomplete")
        self.assertLess(result["commands"][-1]["exit"], 0)
        (self.root / ".lake/calls").unlink()
        p = self.spawn(FIXTURE_MODE="sleep")
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
            self.state / "verification-capacity.json", dict(attempt=os.environ["TAUCETI_VERIFY_ATTEMPT"], waiting=True)
        )
        self.assertTrue(callback())
        self.assertEqual(values["fix-1"], 2)
        self.assertTrue(refund_capacity_wait(self.state, counters))
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

    def test_slot_configuration(self):
        self.assertEqual(heavy_slots(), 1)
        self.assertIn("queue", str(queue_root()))
        for bad in ("0", "-1", "two", "1.5"):
            os.environ["TAUCETI_VERIFY_HEAVY_SLOTS"] = bad
            with self.assertRaises(ValueError):
                heavy_slots()


if __name__ == "__main__":
    unittest.main()
