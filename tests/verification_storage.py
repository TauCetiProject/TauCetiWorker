#!/usr/bin/env python3
"""Private Git/fake-tool storage pressure, cache expiry and automatic continuation fixtures."""

import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verification import Receipts, write

from tauceti_worker import retention
from tauceti_worker import verification as v
from tauceti_worker import verification_storage as storage


class Storage(Receipts):
    def test_large_filesystem_reserve_uses_bytes_instead_of_hundreds_of_gib(self):
        gib = 1024**3
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_MIN_FREE_PERCENT="10", TAUCETI_VERIFY_MIN_FREE_BYTES=str(20 * gib)),
            patch.object(
                storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(3500 * gib, 3220 * gib, 280 * gib)
            ),
        ):
            storage.free_space(self.root)
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_MIN_FREE_PERCENT="10", TAUCETI_VERIFY_MIN_FREE_BYTES=str(20 * gib)),
            patch.object(
                storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(3500 * gib, 3485 * gib, 15 * gib)
            ),
        ):
            with self.assertRaises(storage.Pressure):
                storage.free_space(self.root)

    def test_source_checkpoints_share_host_budget_and_never_expire(self):
        os.environ["TAUCETI_VERIFY_CHECKPOINT_BYTES"] = str(128 * 1024**2)
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            first = self.verify(stage="build")
            self.assertEqual(first["status"], "success")
            checkpoint = Path(first["source_checkpoint"])
            self.assertTrue(checkpoint.is_file())
            write(self.root, "TauCeti/A.lean", "next exact candidate\n")
            second = self.verify(stage="build")
            self.assertEqual(second["status"], "success", second)
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        checkpoints = [row for row in rows.values() if row.get("kind") == "checkpoint"]
        self.assertEqual(len(checkpoints), 2)
        self.assertTrue(all(row["reserved"] < 100000 for row in checkpoints))
        self.collect()
        self.assertTrue(checkpoint.exists())

    def test_bubble_discovery_never_enumerates_root_and_uses_host_identity(self):
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_HOST_STATE=str(self.state)),
            patch.object(storage, "discover") as discover,
        ):
            with patch.object(Path, "iterdir", side_effect=AssertionError("bubble walked outside mounted state")):
                storage.discover_family(Path("/opt/verification-state"))
            discover.assert_called_once_with(Path("/opt/verification-state"))
            self.assertEqual(storage.key(Path("/opt/verification-state"), "a" * 32), storage.key(self.state, "a" * 32))

    def test_checkpoint_preservation_ignores_legacy_byte_and_pool_caps(self):
        os.environ.update(
            TAUCETI_VERIFY_CHECKPOINT_BYTES="1",
            TAUCETI_VERIFY_CHECKPOINT_POOL_BYTES="1",
            TAUCETI_VERIFY_STORAGE_BYTES="1",
        )
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            receipt = self.verify(stage="build")
        self.assertEqual(receipt["status"], "success", receipt)
        self.assertTrue(Path(receipt["source_checkpoint"]).is_file())

    def test_checkpoints_book_actual_bytes_across_many_source_states(self):
        for index in range(30):
            write(self.root, "TauCeti/A.lean", f"unique preserved source {index}\n")
            verifier = v.Verification(self.root, self.state)
            verifier.base = self.base
            verifier.checkpoint_sources(dict(sources=v.inventory(self.root, self.base)))
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertEqual(len(rows), 30)
        self.assertLess(sum(row["reserved"] for row in rows.values()), 1024**2)

    def setUp(self):
        super().setUp()
        os.environ.update(TAUCETI_VERIFY_STORAGE_BYTES=str(256 * 1024**2))

    def collect(self, **kwargs):
        return storage.housekeep((self.state,), seconds=10, entries=100000, now=time.time() + 90000, **kwargs)

    def checkpoint(self, text="checkpointed source\n"):
        write(self.root, "TauCeti/A.lean", text)
        verifier = v.Verification(self.root, self.state)
        verifier.base = self.base
        verifier.checkpoint_sources(dict(sources=v.inventory(self.root, self.base)))
        return Path(verifier.receipt["source_checkpoint"])

    def test_corrupt_ledger_rebuilds_mechanically_without_deleting_checkpoints(self):
        checkpoint = self.checkpoint()
        path = storage.root() / "candidates.json"
        path.write_text("interrupted/corrupt")
        self.collect()
        self.assertEqual(len(list(storage.root().glob("candidates.corrupt-*.json"))), 1)
        self.collect()
        self.assertTrue(checkpoint.is_file())
        rows = v.read_json(path)["candidates"]
        self.assertEqual([row["kind"] for row in rows.values()], ["checkpoint"])

    def test_full_disk_housekeeping_error_does_not_escape_supervisor_hook(self):
        self.checkpoint()
        with patch.object(storage, "_scheduled", side_effect=OSError(28, "disk full")):
            result = storage.housekeep((self.state,))
        self.assertIn("disk full", result["errors"][0])
        self.assertEqual(result["next_action"], "retry-verification-storage-housekeeping")

    def test_legacy_candidate_rows_are_dropped_and_never_measured(self):
        legacy = write(self.state, "verification-candidates/" + "a" * 32 + "/unique.lean", "old candidate")
        checkpoint = self.checkpoint()
        with storage.ledger() as data:
            identity = storage.key(self.state, "a" * 32)
            data["candidates"][identity] = dict(
                state=str(self.state.resolve()), id="a" * 32, kind="candidate", reserved=16384, last_used=1.0
            )
        report = self.collect()
        self.assertEqual(report["candidates"][identity], "legacy-candidate-dropped")
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertEqual({row["kind"] for row in rows.values()}, {"checkpoint"})
        # Housekeeping only forgets the row; retention deletes the tree.
        self.assertTrue(legacy.exists())
        self.assertTrue(checkpoint.is_file())

    def test_settled_checkpoints_do_not_starve_later_legacy_rows(self):
        for index in range(3):
            self.checkpoint(f"settled source {index}\n")
        self.collect()
        with storage.ledger() as data:
            identity = storage.key(self.state, "e" * 32)
            data["candidates"][identity] = dict(
                state=str(self.state.resolve()), id="e" * 32, kind="candidate", reserved=16384, last_used=1.0
            )
        report = storage.housekeep((self.state,), seconds=10, entries=2, now=time.time() + 90000)
        self.assertEqual(report["candidates"].get(identity), "legacy-candidate-dropped", report)

    def test_verification_runs_in_the_round_checkout_and_leaves_no_continuation_state(self):
        with patch.dict(os.environ, TAUCETI_ROUND_DEADLINE=str(time.time() - 60), TAUCETI_VERIFY_BUBBLE="1"):
            result = self.verify(stage="all")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(result["root"], str(self.root.resolve()))
        self.assertTrue(Path(result["receipt"]).is_file())
        self.assertNotIn("job", result)
        self.assertNotIn("continuation_job", result)
        for name in retention.LEGACY_VERIFICATION:
            self.assertFalse((self.state / name).exists(), name)
        rows = v.read_json(storage.root() / "candidates.json").get("candidates", {})
        self.assertEqual({row["kind"] for row in rows.values()}, {"checkpoint"})
        self.assertIn("reused_from", self.verify(stage="build")["phases"]["build"])


def load_tests(loader, suite, pattern):
    return unittest.TestSuite(
        loader.loadTestsFromName("Storage." + name, sys.modules[__name__])
        for name in Storage.__dict__
        if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()
