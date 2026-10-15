#!/usr/bin/env python3
"""Fleet admission fails closed and provenance never affects a mutation."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.claude_api import ClaudeAPIContext
from tauceti_worker.fleet_capacity import admission_reason
from tauceti_worker.fleet_events import emit, mutation


class FleetTests(unittest.TestCase):
    def test_unmanaged_and_expired(self):
        self.assertIsNone(admission_reason("a", env={}))
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "policy.json"
            env = {"TAUCETI_FLEET_ADMISSION_FILE": str(p)}
            self.assertIn("unavailable", admission_reason("a", env=env))
            p.write_text(json.dumps(dict(version=1, expires_at=20, workers=["a"])))
            self.assertIsNone(admission_reason("a", env=env, now=10))
            self.assertIn("reserved", admission_reason("b", env=env, now=10))
            self.assertIn("expired", admission_reason("a", env=env, now=21))
            for value in [
                dict(version=1, expires_at=True, workers=["a"]),
                dict(version=1, expires_at=100, workers="a"),
            ]:
                p.write_text(json.dumps(value))
                self.assertIn("unavailable", admission_reason("a", env=env, now=10))

    def test_bridge_checks_capacity_even_without_budget_and_never_launches(self):
        context = ClaudeAPIContext("worker", "roadmap")
        with (
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI has capacity priority"),
            patch.object(context.budget, "admit") as admit,
        ):
            answer = context.handle(dict(action="admit", id="a" * 32, model="test-model"))
            self.assertFalse(answer["allowed"])
            admit.assert_not_called()
            self.assertFalse(context.invocations)

    def test_events_are_structured_private_and_deduplicable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"TAUCETI_FLEET_EVENTS": str(path), "TAUCETI_FLEET_MACHINE_ID": "host-a"}):
                request = dict(argv=["pr", "create"], context={}, created=1)
                result = SimpleNamespace(stdout="https://github.com/TauCetiProject/TauCeti/pull/123\n")
                mutation(request, result)
                mutation(request, result)
                emit("pr_activity", secret="do-not-record", repository="a/b", pr=1)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["id"], records[1]["id"])
            self.assertEqual(records[0]["pr"], 123)
            self.assertNotIn("do-not-record", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
