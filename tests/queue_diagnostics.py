#!/usr/bin/env python3
"""Candidate run diagnostics (tauceti_queue.diagnostics): the source findings read from a failed
build job's log with bors-ng's `priv/failure_diagnostics.py` patterns, mapped to the controller's
`kind`/`module`/`message` records, and the infrastructure findings taken from the run and jobs.

Covered: compiler errors in both the Lean and Lake forms with timestamps and ANSI stripped,
duplicate declarations from the audit driver and the axioms script, environment linter locations
(ranged, module-only, unknown), the failed-targets fallback used only without located findings,
deduplication, a lost runner, and cancellation, timeouts and a failed finalizer as infrastructure.
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import diagnostics as d  # noqa: E402


def kinds(findings):
    return [(f["kind"], f["module"]) for f in findings]


class ParseLog(unittest.TestCase):
    def test_compiler_errors(self):
        lines = [
            "2026-11-04T04:20:00.1234567Z \x1b[31merror: TauCeti/Algebra/Group/Basic.lean:12:3: error: unknown identifier\x1b[0m",
            "2026-11-04T04:20:00.2Z TauCeti/Algebra/Group/Basic.lean:12:3: error: unknown identifier",
            "2026-11-04T04:20:01.1Z error: TauCeti/Topology/Open.lean:7:1:",
            "2026-11-04T04:20:02.1Z TauCeti/Topology/Open.lean:9:1: warning: unused variable",
            "2026-11-04T04:21:00.1Z error: build failed",
        ]
        findings = d.parse_log(lines)
        self.assertEqual(
            kinds(findings), [("compiler", "TauCeti.Algebra.Group.Basic"), ("compiler", "TauCeti.Topology.Open")]
        )
        self.assertEqual(
            (findings[0]["path"], findings[0]["line_number"], findings[0]["column"]),
            ("TauCeti/Algebra/Group/Basic.lean", 12, 3),
        )
        self.assertNotIn("\x1b", findings[0]["message"])
        self.assertFalse(findings[0]["message"].startswith("2026-"))

    def test_audits(self):
        lines = [
            "audit-driver: import TauCeti.A.B failed, environment already contains 'Foo.bar' from TauCeti.C.D",
            "duplicate-declarations: Foo.baz is declared in both Mathlib.X and TauCeti.E.F",
            "duplicate-declarations: Foo.qux is declared in both Mathlib.X and Mathlib.Y",
        ]
        findings = d.parse_log(lines)
        self.assertEqual(
            kinds(findings), [("audit", "TauCeti.A.B"), ("audit", "TauCeti.C.D"), ("audit", "TauCeti.E.F")]
        )
        self.assertEqual(findings[0]["declaration"], "Foo.bar")
        self.assertEqual(findings[2]["path"], "TauCeti/E/F.lean")

    def test_environment_linter(self):
        payload = {
            "version": 1,
            "complete": True,
            "findings": [
                {
                    "linter": "docBlame",
                    "declaration": "foo",
                    "path": "TauCeti/Lint/Target.lean",
                    "line": 3,
                    "column": 1,
                },
                {"linter": "docBlame", "declaration": "bar", "path": "TauCeti/Lint/Other.lean"},
                {"linter": "docBlame", "declaration": "baz", "unknown": "no_location"},
                {"linter": "docBlame", "declaration": "bad", "path": "../etc/passwd"},
            ],
        }
        findings = d.parse_log([d.ENV_LOCATIONS + json.dumps(payload)])
        self.assertEqual(
            kinds(findings), [("lint", "TauCeti.Lint.Target"), ("lint", "TauCeti.Lint.Other"), ("lint", "")]
        )
        self.assertEqual(findings[0]["line_number"], 3)
        self.assertEqual(findings[2]["unknown"], "no_location")
        self.assertEqual(d.parse_log([d.ENV_LOCATIONS + "not json"]), [])
        self.assertEqual(d.parse_log([d.ENV_LOCATIONS + json.dumps({"version": 2, "findings": []})]), [])

    def test_failed_targets_fallback_only_without_located_findings(self):
        targets = ["Some required targets logged failures:", "- TauCeti.A.B", "- TauCeti.C.D", "error: build failed"]
        findings = d.parse_log(targets)
        self.assertEqual(kinds(findings), [("compiler", "TauCeti.A.B"), ("compiler", "TauCeti.C.D")])
        self.assertFalse(findings[0]["located"])
        located = d.parse_log(["TauCeti/A/B.lean:1:1: error: x", *targets])
        self.assertEqual(kinds(located), [("compiler", "TauCeti.A.B")])
        self.assertNotIn("located", located[0])

    def test_dedup_and_runner_loss(self):
        lines = ["TauCeti/A/B.lean:1:1: error: x"] * 3 + [
            "The runner has received a shutdown signal. This can happen when the runner service is stopped."
        ]
        findings = d.parse_log(lines)
        self.assertEqual(kinds(findings), [("compiler", "TauCeti.A.B"), ("infrastructure", "")])
        self.assertEqual(findings[1]["reason"], "runner_lost")
        self.assertEqual(d.parse_log([]), [])
        self.assertEqual(d.parse_log(["info: all good", "error: something else entirely"]), [])

    def test_message_bound(self):
        finding = d.parse_log(["TauCeti/A/B.lean:1:1: error: " + "x" * 5000])[0]
        self.assertLessEqual(len(finding["message"]), d.MAX_MESSAGE)


class Infrastructure(unittest.TestCase):
    def test_run_and_job_conclusions(self):
        run = {"id": 7, "conclusion": "cancelled"}
        jobs = [
            {"name": "sandboxed-build", "conclusion": "cancelled"},
            {"name": "finalize-merge-group-build", "conclusion": "failure"},
        ]
        findings = d.infrastructure_findings(run, jobs)
        self.assertEqual([f["reason"] for f in findings], ["cancelled", "cancelled", "finalizer_failed"])
        self.assertTrue(all(f["kind"] == "infrastructure" for f in findings))
        self.assertEqual(
            d.infrastructure_findings(
                {"id": 7, "conclusion": "failure"}, [{"name": "sandboxed-build", "conclusion": "failure"}]
            ),
            [],
        )
        missing = d.infrastructure_findings({"id": 7, "conclusion": "failure"}, [], build_status_posted=False)
        self.assertEqual([f["reason"] for f in missing], ["missing_status"])
        self.assertEqual(d.infrastructure_findings({"id": 7, "conclusion": "success"}, []), [])


if __name__ == "__main__":
    unittest.main()
