#!/usr/bin/env python3
"""`services/queue/safepoint.py` calls a controller restart unsafe exactly while the controller has
something in flight that a restart would cut off (docs/queue/operations.md, "Install and update").

The busy states are observed from inside the real controller's own operations, over the fixture's
fake GitHub: the check runs while the CI dispatch is being sent, while the promotion pushes the
canonical branch, while the landing is being recorded and while an insertion replays its chain.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain  # noqa: E402
from tauceti_queue.fakes import make_controller  # noqa: E402
from tauceti_queue.store import Store  # noqa: E402

SCRIPT = REPO / "services/queue/safepoint.py"
_spec = importlib.util.spec_from_file_location("safepoint", SCRIPT)
safepoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(safepoint)

PROJECT = "TauCetiProject/TauCeti"


class Controller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")

    def approve(self, number, files):
        head = self.github.create_pr(number, files)
        self.assertTrue(self.controller.approve(PROJECT, number, head, "reviewer")["accepted"])

    def check(self):
        """(busy, info) for the controller's database as another process reads it, read-only."""
        db = sqlite3.connect(Path(self.controller.store.path).resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            return safepoint.check(db, self.clock())
        finally:
            db.close()

    def head(self):
        return self.controller.store.row("batches", {"state": "head"})

    def run_ci(self):
        self.github.start_runs()
        self.clock.advance(300)
        for run in self.github.complete_runs():
            self.controller.handle_event(
                "workflow_run", {"action": "completed", "workflow_run": {"head_sha": run.head_sha}}
            )
            self.controller.handle_event("status", {"sha": run.head_sha, "context": "build", "state": "success"})

    def test_an_idle_queue_is_a_safe_point(self):
        self.controller.tick()
        busy, info = self.check()
        self.assertEqual(busy, [])
        self.assertIn("no head", info)

    def test_a_head_building_on_github_is_a_safe_point(self):
        """The run is recorded and the controller re-reads it after a restart."""
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.github.start_runs()
        self.clock.advance(120)
        self.controller.tick()
        busy, info = self.check()
        self.assertEqual(busy, [])
        head = self.head()
        self.assertIn(f"head {head['id']} version {head['version']} tip {head['tip'][:12]}", info)
        attempt = next(line for line in info if "CI attempt" in line)
        self.assertIn("outcome pending, dispatch accepted", attempt)

    def test_a_dispatch_is_busy_until_its_send_is_acknowledged(self):
        seen = []
        dispatch = self.github.dispatch_candidate

        def observed(*args, **kwargs):
            seen.append(self.check()[0])
            return dispatch(*args, **kwargs)

        self.approve(101, {"A.lean": "a\n"})
        with patch.object(self.github, "dispatch_candidate", observed):
            self.controller.tick()
        [busy] = seen
        self.assertEqual(len(busy), 1, busy)
        self.assertRegex(busy[0], r"^CI dispatch of attempt \d+ for batch \d+ sent 0s ago, not yet acknowledged$")
        self.assertEqual(self.check()[0], [])

    def test_a_dispatch_left_sending_by_an_earlier_interruption_is_stale_not_busy(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        attempt = self.controller.store.row("ci_attempts", {"batch_id": self.head()["id"]})
        self.controller.store.update(
            "ci_attempts", {"id": attempt["id"]}, dispatch_state="sending", sent_at=self.clock() - 301
        )
        busy, info = self.check()
        self.assertEqual(busy, [])
        self.assertTrue(any(line.startswith("stale: CI dispatch of attempt") for line in info), info)
        self.controller.store.update("ci_attempts", {"id": attempt["id"]}, sent_at=self.clock() - 60)
        self.assertEqual(len(self.check()[0]), 1)

    def test_promotion_and_landing_are_busy_until_the_batch_lands(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.run_ci()
        seen = {}
        fast_forward = self.github.fast_forward
        landing_facts = self.controller._landing_facts

        def pushing(*args, **kwargs):
            seen["promotion"] = self.check()[0]
            return fast_forward(*args, **kwargs)

        def recording(*args, **kwargs):
            seen.setdefault("landing", self.check()[0])
            return landing_facts(*args, **kwargs)

        with (
            patch.object(self.github, "fast_forward", pushing),
            patch.object(self.controller, "_landing_facts", recording),
        ):
            for _ in range(3):
                self.controller.tick()
        self.assertEqual(len(seen["promotion"]), 1, seen)
        self.assertRegex(seen["promotion"][0], r"^promotion due or in progress: outbox \d+ promote:1:")
        # the landing is recorded inside the promotion, which stays pending until both are done
        self.assertEqual(seen["landing"], seen["promotion"])
        self.assertEqual(self.controller.store.row("batches", {"id": 1})["state"], "landed")
        self.assertEqual(self.check()[0], [])

    def test_a_promotion_backing_off_is_not_busy(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.run_ci()
        with patch.object(self.github, "fast_forward", side_effect=RuntimeError("push refused")):
            self.controller.tick()
        [entry] = self.controller.store.rows("outbox", {"kind": "promote"})
        self.assertEqual(entry["state"], "pending")
        self.assertGreater(entry["next_attempt_at"], self.clock())
        busy, info = self.check()
        self.assertEqual(busy, [])
        self.assertTrue(any(line.startswith("promotion backing off:") for line in info), info)
        self.clock.advance(entry["next_attempt_at"] - self.clock())
        self.assertEqual(len(self.check()[0]), 1)

    def test_a_promotion_held_while_publication_is_disabled_is_not_busy(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.run_ci()
        with patch.object(self.github, "fast_forward", side_effect=RuntimeError("push refused")):
            self.controller.tick()
        [entry] = self.controller.store.rows("outbox", {"kind": "promote"})
        self.clock.advance(entry["next_attempt_at"] - self.clock())
        self.controller.set_setting("publication_enabled", False, "operator")
        busy, info = self.check()
        self.assertEqual(busy, [])
        self.assertTrue(any(line.startswith("promotion held while publication is disabled:") for line in info), info)

    def test_a_controller_replay_is_busy_and_worker_claims_are_not(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        head = self.head()
        self.controller.store.insert(
            "claims",
            batch_id=head["id"],
            batch_version=head["version"],
            operation="repair",
            worker="repair-2@host",
            token="t",
            starting_tip=head["tip"],
            task_id=7,
            created_at=self.clock(),
            expires_at=self.clock() + 900,
        )
        self.assertEqual(self.check()[0], [])
        seen = []
        build = chain.build

        def replaying(*args, **kwargs):
            seen.append(self.check()[0])
            return build(*args, **kwargs)

        self.approve(102, {"B.lean": "b\n"})
        with patch("tauceti_queue.controller.chain.build", replaying):
            self.controller.tick()
        self.assertTrue(seen, "the insertion replayed a chain")
        self.assertEqual(len(seen[0]), 1, seen)
        self.assertRegex(seen[0][0], r"^controller replay: claim \d+ \(insert\) on batch \d+, taken 0s ago$")
        busy, info = self.check()
        self.assertEqual(busy, [])
        claims = next(line for line in info if "worker claims" in line)
        self.assertIn(
            "worker claims: 1 (kept across a restart); claim 2 repair-2@host task 7 repair, expires in 900s", claims
        )

    def test_an_expired_controller_claim_is_not_busy(self):
        self.controller.store.insert(
            "claims",
            batch_id=1,
            batch_version=1,
            operation="rebase",
            worker="controller",
            token="t",
            starting_tip="t" * 40,
            created_at=0,
            expires_at=self.clock() - 1,
        )
        self.assertEqual(self.check()[0], [])


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_script(self, *args, script=("python3", "-I", str(SCRIPT))):
        return subprocess.run([*script, *args], capture_output=True, text=True, timeout=60)

    def test_exit_status_and_output(self):
        path = self.root / "queue.sqlite3"
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe: no queue database", result.stdout)
        path.write_text("")
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("holds no queue records", result.stdout)
        path.unlink()
        store = Store(str(path))
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe point: nothing in flight", result.stdout)
        self.assertIn("info: no head", result.stdout)
        store.insert(
            "claims",
            batch_id=1,
            batch_version=1,
            operation="insert",
            worker="controller",
            token="t",
            starting_tip="t" * 40,
            created_at=0,
            expires_at=4e9,
        )
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("busy: controller replay: claim 1 (insert) on batch 1", result.stdout)
        self.assertIn("not a safe point: 1 operation(s)", result.stdout)
        # the release.sh subcommand runs the same check
        result = self.run_script(
            "safepoint", "--database", str(path), script=("bash", str(REPO / "services/queue/release.sh"))
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("busy: controller replay", result.stdout)
        store.close()
        path.write_bytes(b"not a database" * 100)
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown: cannot read", result.stdout)

    def test_the_check_never_writes(self):
        path = self.root / "queue.sqlite3"
        Store(str(path)).close()
        before = {p.name: p.stat().st_mtime_ns for p in self.root.iterdir()}
        path.chmod(0o444)
        self.addCleanup(path.chmod, 0o644)
        result = self.run_script(str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path.stat().st_mtime_ns, before[path.name])


if __name__ == "__main__":
    unittest.main()
