"""A deliberate PR-CI skip is reviewable, without claiming a build succeeded."""

import unittest

from tauceti_worker.survey import PRInfo


class SelfTested(unittest.TestCase):
    def test_scheduling_keeps_local_attestation_distinct_from_build(self):
        pr = PRInfo(
            7, "a" * 40, "math", "author", "TauCeti", False, "MERGEABLE", "author", False, True, self_tested=True
        )
        self.assertTrue(pr.review_ready)
        self.assertFalse(pr.needs_ci_repair)
        self.assertFalse(pr.build_success)
        ordinary = PRInfo.from_json({"number": 7, "labels": [{"name": "self-tested"}]})
        self.assertFalse(ordinary.review_ready)


if __name__ == "__main__":
    unittest.main()
