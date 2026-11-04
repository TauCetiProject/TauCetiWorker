#!/usr/bin/env python3
"""The cutover rehearsal's dry run: the whole scenario script over the fakes, through the rehearsal code path.

`tauceti_queue.rehearsal.run_local` builds the fake GitHub, the real controller, server and loop in
this process, installs the scripted `claude` and the `lake` stub on PATH, and drives every scenario
of the rehearsal the way the live run drives the scratch repository: approvals as signed webhook
deliveries, the real repair task runner claiming and submitting through the HTTP task API, a service
restart mid-head, feed validation and chain validation after every landing. No network, no inference.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ["TAUCETI_CACHE_SERVICE"] = ""
os.environ.setdefault("TAUCETI_WORKER_ID", "queue-rehearsal-test")
for key in ("TAUCETI_RUNTIME_STATUS", "TAUCETI_FLEET_ADMISSION_FILE", "CLAUDE_CONFIG_DIR", "TAUCETI_STREAM"):
    os.environ.pop(key, None)

from tauceti_queue import rehearsal  # noqa: E402

EXPECTED = (
    "clean",
    "eligibility",
    "source-failure",
    "shrink",
    "conflict",
    "urgent",
    "stale-status",
    "cancelled-run",
    "closed-member",
    "inference-hold",
    "restart",
    "chain",
    "feeds",
    "status",
)


class DryRun(unittest.TestCase):
    def test_every_scenario_passes_over_the_fakes(self):
        with tempfile.TemporaryDirectory(prefix="queue-rehearsal-") as tmp:
            report = rehearsal.run_local(tmp, report_dir=os.path.join(tmp, "report"))
            document = json.loads((Path(tmp) / "report" / "rehearsal-local.json").read_text())
            markdown = (Path(tmp) / "report" / "rehearsal-local.md").read_text()
        statuses = {s["id"]: (s["status"], s["detail"]) for s in document["scenarios"]}
        self.assertEqual(tuple(statuses), EXPECTED)
        failures = {k: v for k, v in statuses.items() if v[0] != "pass"}
        self.assertEqual(failures, {}, json.dumps(document, indent=1)[:20000])
        self.assertTrue(report.ok)
        self.assertEqual(document["schema"], rehearsal.SCHEMA)
        self.assertIn("| clean:", markdown)
        evidence = {s["id"]: s["evidence"] for s in document["scenarios"]}
        self.assertEqual(evidence["clean"]["agent_invocations"], 0)
        self.assertEqual(evidence["source-failure"]["adoption"]["status"], "adopted")
        self.assertTrue(evidence["shrink"]["empty_commit"]["empty"])
        self.assertEqual(evidence["conflict"]["task"]["kind"], "rebase_conflict")
        self.assertGreaterEqual(evidence["conflict"]["xray_first_probe"]["probes"], 1)
        self.assertEqual(evidence["conflict"]["xray_inserted_event"]["kind"], "inserted")
        self.assertEqual(evidence["urgent"]["promotion_event"]["details"]["how"], "promoted")
        self.assertIn("missing-status", evidence["stale-status"]["refusal"]["outcome_detail"])
        self.assertEqual(len(evidence["inference-hold"]["releases"]), 3)
        self.assertEqual(evidence["restart"]["before"]["run_id"], evidence["restart"]["after"]["run_id"])
        self.assertGreaterEqual(len(evidence["chain"]["heads"]), 10)
        self.assertTrue(all(not c["errors"] for c in evidence["feeds"]["checks"]))


class Scaffold(unittest.TestCase):
    def test_scaffold_writes_the_scratch_content(self):
        with tempfile.TemporaryDirectory(prefix="queue-scaffold-") as tmp:
            written = rehearsal.scaffold(tmp)
            self.assertIn("lakefile.toml", written)
            self.assertIn(".github/workflows/pr-build.yml", written)
            toolchain = (Path(tmp) / "lean-toolchain").read_text().strip()
            self.assertEqual(toolchain, rehearsal.LEAN_TOOLCHAIN)
            workflow = (Path(tmp) / ".github/workflows/pr-build.yml").read_text()
            # What the service relies on: the dispatch event, the run title, the job names, the bound status.
            self.assertIn("types: [tauceti-bors-staging]", workflow)
            self.assertIn("format('bors branch={0} batch={1} head={2} base={3}'", workflow)
            self.assertIn("  sandboxed-build:", workflow)
            self.assertIn("  finalize-merge-group-build:", workflow)
            self.assertIn('-f target_url="$RUN_URL"', workflow)
            self.assertIn("platformIndependent = true", (Path(tmp) / "lakefile.toml").read_text())


if __name__ == "__main__":
    unittest.main()
