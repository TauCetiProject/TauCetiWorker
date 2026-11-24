"""Long diagnostics keep both ends, so the failure's name survives truncation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.excerpt import ends


class Ends(unittest.TestCase):
    def test_short_text_is_only_stripped(self):
        self.assertEqual(ends("  exit 1\n"), "exit 1")
        self.assertEqual(ends(None), "")
        self.assertEqual(ends("x" * 200), "x" * 200)

    def test_long_text_keeps_the_start_and_the_end(self):
        message = (
            "Configuration needs attention: tools.tools.ref must be a full immutable 40-character commit SHA.\n"
            + "setup instructions\n" * 40
            + "Manual: https://example.invalid/project-setup.md"
        )
        excerpt = ends(message, 200)
        self.assertLessEqual(len(excerpt), 200)
        self.assertTrue(excerpt.startswith("Configuration needs attention: tools.tools.ref"))
        self.assertTrue(excerpt.endswith("project-setup.md"))
        self.assertIn(" … ", excerpt)

    def test_bytes_and_small_limits(self):
        # A timed-out subprocess can return bytes even in text mode.
        self.assertEqual(ends(b"start " + b"x" * 400 + b" end\xff", 20), "start xx … xxxx end\ufffd")
        for limit in (-1, 0, 1, 4):
            self.assertLessEqual(len(ends("abcdefghij", limit)), max(limit, 0))
        self.assertEqual(ends("abcdefghij", 5), "a … j")


if __name__ == "__main__":
    unittest.main()
