"""Public history uses pilot definitions without including another repository."""

import copy
import json
import runpy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_queue.monitoring_history import load_reporter, prepare, read_baseline

PROJECT = "TauCetiProject/TauCeti"


def snapshot():
    value = runpy.run_path(str(Path(__file__).with_name("staging_journal_report.py")))["pilot_snapshot"]()
    for row in value["tables"]["batches"]:
        row["repository"] = PROJECT
    for row in value["tables"]["inputs"]:
        row["repository"] = PROJECT
    return value


class History(unittest.TestCase):
    def test_original_frozen_bors_feed_is_accepted_without_a_separate_conversion(self):
        reporter = load_reporter()
        baseline = read_baseline(Path(__file__).with_name("fixtures") / "staging-journal-fixture.json", reporter)
        self.assertGreater(baseline["landed_prs_per_hour"], 0)
        self.assertEqual(baseline["candidate_range"], [1833, 1932])
        self.assertTrue(prepare(snapshot(), baseline, reporter)["windows"])

    def test_mixed_repository_measurements_never_appear_under_public_scope(self):
        value = snapshot()
        before = prepare(value, {"landed_prs_per_hour": 1}, load_reporter())
        private = copy.deepcopy(value["tables"]["batches"][0])
        private["id"] = 99
        private["repository"] = "Private/SECRET"
        value["tables"]["batches"].append(private)
        for name in ("members", "ci_attempts", "tasks", "claims", "journal"):
            for old in list(value["tables"][name]):
                row = copy.deepcopy(old)
                row["batch_id"] = 99
                row["id"] += 1000
                value["tables"][name].append(row)
        after = prepare(value, {"landed_prs_per_hour": 1}, load_reporter())
        self.assertEqual(before, after)
        self.assertNotIn("SECRET", json.dumps(after))
        self.assertEqual(after["windows"][0]["summary"]["cost"]["recorded_usd"], 2.5)
        self.assertIn(
            after["windows"][0]["criteria"]["week"]["status"],
            ["not_yet_observable", "requires_attribution_and_comment_audit"],
        )

    def test_unattributed_legacy_snapshot_fails_closed(self):
        value = snapshot()
        del value["tables"]["batches"][0]["repository"]
        with self.assertRaises(ValueError):
            prepare(value, {"landed_prs_per_hour": 1}, load_reporter())


if __name__ == "__main__":
    unittest.main()
