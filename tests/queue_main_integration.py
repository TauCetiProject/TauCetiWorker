#!/usr/bin/env python3
"""The main-integration backfill (decision 175 B): dispatch, results, confirmation, take-out before seal."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain, main_integration, stale_check  # noqa: E402
from tauceti_queue.fakes import make_controller  # noqa: E402
from tauceti_queue.store import loads  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


class Backfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller, self.github, self.repo, self.clock = make_controller(Path(self.tmp.name))
        self.addCleanup(self.controller.store.close)
        # The head (101) waits on CI; 102 to 104 join the open batch behind it.
        self.approve(101, {"TauCeti/A.lean": "theorem a : True := trivial\n"})
        self.controller.tick()
        self.heads = {
            number: self.approve(number, {f"TauCeti/F{number}.lean": f"theorem f{number} : True := trivial\n"})
            for number in (102, 103, 104)
        }
        self.controller.set_setting("main_integration_interval_seconds", 0, "test")
        self.controller.set_setting("main_integration_enabled", True, "test")

    def approve(self, number, files):
        head = self.github.create_pr(number, files)
        self.controller.approve(PROJECT, number, head, "reviewer")
        return head

    def batch(self):
        return next(b for b in self.controller.store.rows("batches", {"state": "open"}) if not b["sealed"])

    def record(self, number):
        return main_integration.record_of(self.controller.store.row("inputs", {"number": number}))

    def post(self, number, state, description="passed"):
        main = self.record(number)["main"]
        self.github.post_status(
            self.heads[number], state, "77", f"B={main[:12]} T=0123456789ab {description}", context="main-integration"
        )

    def test_dispatches_unsealed_members_onto_main_and_records_green(self):
        self.controller.tick()
        canonical = self.controller._canonical
        requests = self.github.main_integration_requests
        self.assertEqual(sorted(r[0] for r in requests), [102, 103, 104])
        self.assertTrue(all(r[2] == canonical and r[1] == self.heads[r[0]] for r in requests))
        # The head is never a candidate.
        self.assertNotIn(101, [r[0] for r in requests])
        self.assertIsNotNone(self.record(102)["checked"])
        self.post(102, "success")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.record(102)["state"], "success")
        # Fresh evidence is not asked for again.
        self.assertEqual([r[0] for r in requests].count(102), 1)

    def test_max_in_flight_and_deferred_retry(self):
        self.controller.set_setting("main_integration_max_in_flight", 1, "test")
        self.controller.tick()
        self.assertEqual(len(self.github.main_integration_requests), 1)
        number = self.github.main_integration_requests[0][0]
        self.post(number, "error", "deferred: Fleet PR pool busy (idle 2)")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.record(number)["state"], "deferred")
        # Another member takes the free slot; the deferred one waits for its retry time.
        self.assertEqual(len(self.github.main_integration_requests), 2)
        self.assertNotEqual(self.github.main_integration_requests[1][0], number)

    def test_a_status_for_another_main_or_from_before_the_dispatch_is_not_an_answer(self):
        self.controller.tick()
        self.github.post_status(self.heads[102], "failure", "1", "B=000000000000 T=x failed", context="main-integration")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.record(102)["state"], "pending")
        self.assertFalse(
            main_integration._answers(
                self.github.statuses[(self.heads[102], "main-integration")], dict(main="0" * 40, dispatched_at=1e12)
            )
        )

    def test_confirmed_red_takes_the_member_out_before_seal(self):
        self.controller.tick()
        before = self.batch()
        self.post(103, "failure", "failed: sandboxed build, audit, or simp-NF lint failed")
        self.clock.advance(60)
        self.controller.tick()
        # One red result is confirmed by a second run before anything changes.
        self.assertEqual(self.record(103)["failures"], 1)
        self.assertEqual([r[0] for r in self.github.main_integration_requests].count(103), 2)
        self.assertEqual(self.batch()["version"], before["version"])
        self.clock.advance(60)
        self.post(103, "failure", "failed again")
        self.clock.advance(60)
        self.controller.tick()
        batch = self.batch()
        self.assertEqual(batch["version"], before["version"] + 1)
        members = self.controller._members(batch["id"])
        self.assertEqual([m["number"] for m in members], [102, 104])
        removed = self.controller.store.row("members", {"number": 103})
        self.assertEqual((removed["removed"], removed["removed_reason"]), (1, "main-integration-failed"))
        row = self.controller.store.row("inputs", {"number": 103})
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "main-integration-failed"))
        self.assertEqual(self.record(103)["acted"], "removed")
        # The chain is exactly the remaining members, each commit where the member row says.
        commits = chain.chain_commits(self.controller.clone, batch["base"], batch["tip"])
        self.assertEqual([c.sha for c in commits], [m["commit_sha"] for m in members])
        self.assertNotIn("TauCeti/F103.lean", chain.git(self.controller.clone, "ls-tree", "-r", "--name-only", batch["tip"]))
        recovery = [o for o in self.controller.store.rows("outbox", {"kind": "recovery"}) if "103" in o["key"]]
        self.assertEqual(len(recovery), 1)
        self.assertIn("main-integration", loads(recovery[0]["payload"])["marker"])
        journal = [e for e in self.controller.store.rows("journal", {"kind": "main-integration-removed"})]
        self.assertEqual(len(journal), 1)

    def test_sealed_batches_and_take_out_off_are_only_recorded(self):
        self.controller.tick()
        for _ in range(2):
            self.clock.advance(60)
            self.post(103, "failure", "failed")
            self.clock.advance(60)
            self.controller.set_setting("main_integration_take_out", False, "test")
            self.controller.tick()
        self.assertEqual(self.record(103)["failures"], 2)
        self.assertFalse(self.record(103).get("acted"))
        batch = self.batch()
        self.controller.store.update("batches", {"id": batch["id"]}, sealed=1)
        self.controller.set_setting("main_integration_take_out", True, "test")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.controller.store.row("members", {"number": 103})["removed"], 0)
        self.assertEqual(self.controller.store.row("inputs", {"number": 103})["state"], "inserted")

    def test_off_by_default(self):
        self.controller.set_setting("main_integration_enabled", False, "test")
        self.controller.tick()
        self.assertEqual(self.github.main_integration_requests, [])


class StaticCheck(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files, message):
        for path, text in files.items():
            target = Path(self.root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                target.unlink()
            else:
                target.write_text(text)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.base = self.commit(
            {"TauCeti/Lp.lean": "import Mathlib.Tactic.Linarith\ntheorem MemLp.integrableOn_of_subset : True := trivial\n"},
            "base",
        )

    def test_flags_moved_declarations_duplicates_and_dropped_imports(self):
        main = self.commit(
            {
                "TauCeti/Lp.lean": "theorem MemLp.integrableOn_of_subset : True := trivial\n",
                "TauCeti/Pde.lean": "theorem uses : True := MemLp.integrableOn_of_subset\ntheorem IsConj.orderOf_eq : True := trivial\n",
            },
            "main",
        )
        self.git("checkout", "-q", "-b", "pr", self.base)
        head = self.commit(
            {
                "TauCeti/Lp.lean": "import Mathlib.Tactic.Linarith\ntheorem other_name_here : True := by nlinarith\n",
                "TauCeti/Conj.lean": "theorem IsConj.orderOf_eq : True := trivial\n",
            },
            "pr",
        )
        result = stale_check.StaleCheck(self.root).check(head, main)
        self.assertTrue(result["flagged"])
        self.assertEqual(result["base"], self.base)
        self.assertIn("MemLp.integrableOn_of_subset", result["pr_changes_main_uses"])
        self.assertEqual(result["duplicates"], ["IsConj.orderOf_eq"])
        self.assertEqual(result["dropped_imports"], ["TauCeti/Lp.lean"])
        self.assertEqual(result["overlap"], ["TauCeti/Lp.lean"])

    def test_disjoint_change_is_clean_and_a_missing_commit_is_unknown(self):
        main = self.commit({"TauCeti/Other.lean": "theorem elsewhere_lemma : True := trivial\n"}, "main")
        self.git("checkout", "-q", "-b", "pr", self.base)
        head = self.commit({"TauCeti/New.lean": "theorem fresh_lemma_name : True := trivial\n"}, "pr")
        result = stale_check.StaleCheck(self.root).check(head, main)
        self.assertFalse(result["flagged"], result)
        missing = stale_check.StaleCheck(self.root).check("f" * 40, main)
        self.assertTrue(missing["flagged"])
        self.assertIn("unknown", missing)


if __name__ == "__main__":
    unittest.main()
