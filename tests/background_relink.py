"""Between-rounds relinking: the build-tree lock, the stop handshake and the loop's child."""

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import background_relink as background

# A stand-in pass: holds the lock under the real command line, records its pid like the child, and
# exits on SIGTERM unless told to ignore it.
HOLDER = r"""
import fcntl, os, signal, sys, time
state, ignore = sys.argv[3], sys.argv[4] == "ignore"
fd = os.open(os.path.join(state, "build-tree.lock"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\n".encode())
signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore else (lambda *_: sys.exit(0)))
print("held", flush=True)
time.sleep(60)
"""


@unittest.skipUnless(background.supported(), "the stop handshake reads /proc")
class BackgroundRelink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(os.path.realpath(self.tmp.name)) / "state" / "w1"
        self.state.mkdir(parents=True)
        (self.state / "round.lock").touch()
        held = patch.dict(background._HELD, clear=True)
        held.start()
        self.addCleanup(self.release)
        self.addCleanup(held.stop)

    def release(self):
        for fd in background._HELD.values():
            os.close(fd)

    def holder(self, *, command=background.COMMAND, ignore=False):
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, command, "--state-dir", str(self.state), "ignore" if ignore else "stop"],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def test_a_round_stops_the_pass_and_keeps_the_lock(self):
        process = self.holder()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            self.assertTrue(background.stop(self.state / "sources" / "abc"))  # A source's state.
        self.assertEqual(process.wait(5), 0)
        self.assertIn("stopped the between-rounds relink", err.getvalue())
        self.assertIn(str(self.state), background._HELD)
        fd = os.open(self.state / background.LOCK, os.O_RDWR)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # No pass can start while the round runs.
        os.close(fd)
        self.assertTrue(background.stop(self.state))  # Idempotent within the round.

    def test_a_pass_that_ignores_the_request_is_killed(self):
        process = self.holder(ignore=True)
        with (
            patch.object(background, "STOP_WAIT", 0.5),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(background.stop(self.state))
        self.assertEqual(process.wait(5), -9)

    def test_an_unknown_holder_is_never_signalled(self):
        process = self.holder(command="something-else")
        with (
            patch.object(background, "STOP_WAIT", 0.5),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(background.stop(self.state))
            with background.paused(self.state) as idle:
                self.assertFalse(idle)
        self.assertIsNone(process.poll())
        self.assertNotIn(str(self.state), background._HELD)

    def test_paused_lets_the_lock_go(self):
        with background.paused(self.state) as idle:
            self.assertTrue(idle)
        self.assertNotIn(str(self.state), background._HELD)
        fd = os.open(self.state / background.LOCK, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)

    def test_only_a_round_holding_the_lock_requests_a_pass(self):
        checkout = self.state.parent.parent / "checkouts" / "w1" / "TauCeti"
        checkout.mkdir(parents=True)
        (checkout / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")
        env = {"LAKE_CACHE_DIR": "/private", "TAUCETI_BUILD_CACHE_ROOT": "/pool", "GH_TOKEN": "secret"}
        background.request(checkout, env)
        self.assertFalse((self.state / background.REQUESTS).exists())
        self.assertTrue(background.stop(self.state))
        background.request(checkout, env)
        rows = json.loads((self.state / background.REQUESTS).read_text())
        self.assertEqual(list(rows), [str(checkout)])
        self.assertEqual(rows[str(checkout)]["toolchain"], "leanprover/lean4:v4.37.0")
        self.assertEqual(
            rows[str(checkout)]["env"], {"LAKE_CACHE_DIR": "/private", "TAUCETI_BUILD_CACHE_ROOT": "/pool"}
        )
        with patch.dict(os.environ, {"TAUCETI_BACKGROUND_RELINK": "0"}):
            self.assertIsNone(background.start(self.state))

    def test_the_child_relinks_idle_trees_and_drops_finished_requests(self):
        root = self.state.parent.parent
        worktree, ordinary, unreleased = (root / name / "TauCeti" for name in ("a", "b", "c"))
        for checkout in (worktree, ordinary, unreleased):
            (checkout / ".lake").mkdir(parents=True)
        background.parked(worktree).mkdir()
        os.rmdir(worktree / ".lake")  # Released: only the parked tree is left.
        rows = {
            str(path): dict(requested_at=1, toolchain="leanprover/lean4:v4.37.0", env={"LAKE_CACHE_DIR": "/s"})
            for path in (worktree, unreleased)
        }
        (self.state / background.REQUESTS).write_text(json.dumps(rows))
        calls = []

        def between_rounds(checkout, lake, env, *, stop):
            calls.append((checkout, lake, env["ELAN_TOOLCHAIN"], env["LAKE_CACHE_DIR"]))
            return {}

        with patch("tauceti_worker.canonical_store.between_rounds", between_rounds):
            self.assertEqual(background.main(str(self.state)), 0)
        # A worktree still in place was never released: another worker's sweep may remove it.
        self.assertEqual(calls, [(worktree, background.parked(worktree), "leanprover/lean4:v4.37.0", "/s")])
        self.assertFalse((self.state / background.REQUESTS).exists())
        # Without per-round worktrees the checkout keeps its `.lake` in place.
        (self.state / background.REQUESTS).write_text(json.dumps({str(ordinary): dict(env={})}))
        calls.clear()

        def unfinished(checkout, lake, env, *, stop):
            calls.append(lake)
            return {"incomplete": True}

        with (
            patch.dict(os.environ, {"TAUCETI_ROUND_WORKTREES": "0"}),
            patch("tauceti_worker.canonical_store.between_rounds", unfinished),
        ):
            background.main(str(self.state))
        self.assertEqual(calls, [ordinary / ".lake"])
        self.assertIn(str(ordinary), json.loads((self.state / background.REQUESTS).read_text()))  # Resumes later.

    def test_the_child_leaves_a_tree_a_round_holds(self):
        self.assertTrue(background.stop(self.state))
        (self.state / background.REQUESTS).write_text(json.dumps({"/nowhere": {}}))
        result = subprocess.run(
            [sys.executable, "-m", "tauceti_worker", background.COMMAND, "--state-dir", str(self.state)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / background.REQUESTS).exists())  # Untouched while a round holds the lock.

    def test_the_loop_starts_one_child_and_stops_it_on_exit(self):
        (self.state / background.REQUESTS).write_text("{}")
        started = []

        class Child:
            def __init__(self, argv, **kwargs):
                started.append(argv)
                self.signals = []
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def send_signal(self, sig):
                self.signals.append(sig)
                self.alive = False

            def wait(self, timeout=None):
                return 0

        with patch.object(background.subprocess, "Popen", Child):
            child = background.start(self.state)
            self.assertIs(background.start(self.state, child), child)  # Still running: no second one.
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][-3:], [background.COMMAND, "--state-dir", str(self.state)])
        background.finish(child)
        self.assertEqual(child.signals, [background.signal.SIGTERM])
        (self.state / background.REQUESTS).unlink()
        self.assertIsNone(background.start(self.state))  # Nothing requested.


if __name__ == "__main__":
    unittest.main()
