#!/usr/bin/env python3
"""TAUCETI_SHARED_ATTEMPTS_DIR gives every worker naming one directory one attempt budget per PR."""

import importlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
survey_mod = importlib.import_module("tauceti_worker.survey")
from tauceti_worker import agents, work_units
from tauceti_worker import github_transport as transport
from tauceti_worker.config import GitHubUnavailable, NoProgress

HEAD = "a" * 40
FIX = "fix-7-" + HEAD[:12]
SHARED_NAMES = (
    FIX,
    "ci-7-" + HEAD[:12],
    "ci-pr-7",
    "bump-7-" + HEAD[:12],
    "bump-pr-7",
    "lint-repair-7-" + HEAD[:12],
    "lint-repair-pr-7",
    "rebase-pr-7",
    "rebase-pr-7-0123456789abcdef0123",
    "infra-fix-ci-7",
)
WORKER_NAMES = (
    "review-err-7",
    "review-contest-7",
    "review-contest-7-style",
    "round-base-7",
    "state-refresh-7-aaaaaaaaaaaa-retry-at",
    "progress-attempt-ts",
)


def healthy(number=7):
    return survey_mod.PRInfo(
        number=number,
        head_oid=HEAD,
        head_ref="feature",
        head_owner="me",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="MERGEABLE",
        author="me",
        build_success=True,
        build_failed=False,
    )


class SharedAttemptsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shared = self.root / "attempts"

    def counters(self, worker, *, shared=True):
        environment = {"TAUCETI_SHARED_ATTEMPTS_DIR": str(self.shared) if shared else ""}
        with patch.dict(os.environ, environment):
            return survey_mod.Counters(SimpleNamespace(state=self.root / worker))

    def test_unset_keeps_every_counter_in_the_worker_state(self):
        first, second = self.counters("first", shared=False), self.counters("second", shared=False)
        for name in SHARED_NAMES + WORKER_NAMES:
            self.assertEqual(first.incr(name), 1)
            self.assertEqual(second.read(name), 0)
            self.assertTrue((self.root / "first" / name).is_file())
        first.write(FIX, 0)
        self.assertEqual(first.read(FIX), 0)
        first.observe_rebase_progress(healthy(), handoff=False)
        self.assertEqual(second.rebase_budget_key(7), "rebase-pr-7")
        self.assertFalse(self.shared.exists())

    def test_workers_charge_one_budget(self):
        first, second = self.counters("first"), self.counters("second")
        for name in SHARED_NAMES:
            self.assertEqual(first.incr(name), 1)
            self.assertEqual(second.incr(name), 2)
            self.assertEqual(first.read(name), 2)
            self.assertEqual((self.shared / name).read_text(), "2")
        for name in WORKER_NAMES:
            first.incr(name)
            self.assertEqual(second.read(name), 0)
            self.assertFalse((self.shared / name).exists())
        first.record_foreign_repair_launch(7, HEAD, "evidence", "main")
        self.assertFalse(second.foreign_repair_launched(7))
        self.assertEqual(sorted(path.name for path in (self.root / "first").iterdir() if path.name in SHARED_NAMES), [])

    def test_a_refund_returns_the_callers_own_charge(self):
        first, second = self.counters("first"), self.counters("second")
        second.incr(FIX)

        # The decrement every refund path writes, with a peer charging between its read and write.
        first.incr(FIX)
        seen = first.read(FIX)
        second.incr(FIX)
        first.write(FIX, max(0, seen - 1))
        self.assertEqual(second.read(FIX), 2)

        # The restore a paused stage writes, likewise.
        before = first.read(FIX)
        first.incr(FIX)
        second.incr(FIX)
        first.write(FIX, min(before, first.read(FIX)))
        self.assertEqual(second.read(FIX), 3)
        first.write(FIX, min(before, first.read(FIX)))
        self.assertEqual(second.read(FIX), 3)

        # A worker that charged nothing lowers nothing; one that charged can hand all of it back.
        third = self.counters("third")
        third.write(FIX, max(0, third.read(FIX) - 1))
        third.write(FIX, 0)
        self.assertEqual(second.read(FIX), 3)
        for _ in range(5):
            second.write(FIX, max(0, second.read(FIX) - 1))
        self.assertEqual(first.read(FIX), 0)

    def test_a_rebase_checkpoint_renews_the_budget_for_every_worker(self):
        first, second = self.counters("first"), self.counters("second")
        for _ in range(3):
            first.incr("rebase-pr-7")
        self.assertEqual(second.read(second.rebase_budget_key(7)), 3)
        first.observe_rebase_progress(healthy(), handoff=False)
        renewed = second.rebase_budget_key(7)
        self.assertNotEqual(renewed, "rebase-pr-7")
        self.assertEqual(renewed, first.rebase_budget_key(7))
        self.assertTrue((self.shared / "rebase-progress-7.json").is_file())
        self.assertFalse((self.root / "first" / "rebase-progress-7.json").exists())
        second.incr(renewed)
        self.assertEqual(first.read(renewed), 1)

    def test_files_take_the_directory_read_and_write_bits(self):
        for mode, expected in ((0o2770, 0o660), (0o700, 0o600), (0o775, 0o664)):
            self.shared.mkdir(exist_ok=True)
            self.shared.chmod(mode)
            counters = self.counters("first")
            counters.incr(FIX)
            counters.observe_rebase_progress(healthy(), handoff=False)
            for name in (FIX, "rebase-progress-7.json"):
                self.assertEqual(stat.S_IMODE((self.shared / name).stat().st_mode), expected)
                (self.shared / name).unlink()

    def test_concurrent_processes_lose_no_charge(self):
        script = (
            "import sys; from pathlib import Path; from types import SimpleNamespace\n"
            "from tauceti_worker.survey import Counters\n"
            "counters = Counters(SimpleNamespace(state=Path(sys.argv[1])))\n"
            "for _ in range(25):\n"
            "    counters.incr(sys.argv[2])\n"
            "    counters.incr(sys.argv[2])\n"
            "    counters.write(sys.argv[2], max(0, counters.read(sys.argv[2]) - 1))\n"
        )
        environment = {**os.environ, "TAUCETI_SHARED_ATTEMPTS_DIR": str(self.shared), "PYTHONPATH": str(REPO)}
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(self.root / f"w{index}"), FIX], env=environment)
            for index in range(4)
        ]
        self.assertEqual([worker.wait(timeout=120) for worker in workers], [0] * 4)
        self.assertEqual(self.counters("reader").read(FIX), 100)
        self.assertEqual([path.name for path in self.shared.iterdir()], [FIX])

    def test_a_github_pause_leaves_a_peers_charge(self):
        first, second = self.counters("first"), self.counters("second")
        second.incr(FIX)
        candidate = SimpleNamespace(pr=7, head=HEAD)

        def interrupted(*_args):
            first.incr(FIX)
            second.incr(FIX)
            transport.record_block("core", 2000000000)
            return 1

        with (
            patch.dict(
                os.environ,
                {"TAUCETI_GITHUB_SHARED_DIR": str(self.root / "github"), "GH_TOKEN": "fixture"},
            ),
            self.assertRaises(GitHubUnavailable),
        ):
            work_units._run_github_stage(
                "fix", interrupted, SimpleNamespace(counters=first, cfg=None), None, candidate, None, False
            )
        self.assertEqual(second.read(FIX), 2)

    def test_a_github_pause_refunds_its_charge_when_a_peer_refunds_meanwhile(self):
        first, second = self.counters("first"), self.counters("second")
        second.incr(FIX)
        candidate = SimpleNamespace(pr=7, head=HEAD)

        def interrupted(*_args):
            first.incr(FIX)
            second.write(FIX, max(0, second.read(FIX) - 1))
            raise GitHubUnavailable("paused")

        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(self.root / "github")}),
            self.assertRaises(GitHubUnavailable),
        ):
            work_units._run_github_stage(
                "fix", interrupted, SimpleNamespace(counters=first, cfg=None), None, candidate, None, False
            )
        self.assertEqual(second.read(FIX), 0)

    def test_restore_keeps_the_private_arithmetic(self):
        counters = self.counters("first", shared=False)
        counters.incr(FIX)
        saved = survey_mod.checkpoint_counters(counters, (FIX,))
        counters.incr(FIX)
        counters.incr(FIX)
        survey_mod.restore_counters(counters, saved)
        self.assertEqual(counters.read(FIX), 1)
        counters.write(FIX, 0)
        survey_mod.restore_counters(counters, saved)
        self.assertEqual(counters.read(FIX), 0)

    def test_the_outage_refund_allowance_is_shared(self):
        first, second = self.counters("first"), self.counters("second")
        candidate = SimpleNamespace(pr=7, head=HEAD)
        for counters in (first, second):
            counters.incr(FIX)
            agents._LAST_AGENT_FAILURE = "provider returned 529"
            with self.assertRaises(NoProgress):
                work_units._refund_infra_failure(SimpleNamespace(counters=counters), candidate, "fix", (FIX,))
        self.assertEqual(first.read(FIX), 0)
        self.assertEqual((self.shared / "infra-fix-7").read_text(), "2")


if __name__ == "__main__":
    unittest.main()
