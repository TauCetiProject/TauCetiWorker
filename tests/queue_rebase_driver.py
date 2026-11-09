#!/usr/bin/env python3
"""Experimental driver and historical oracle, using isolated real Git repositories."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import queue_chain

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_queue import chain  # noqa: E402
from tauceti_queue.rebase_driver import Driver, DriverError  # noqa: E402
from tauceti_queue.rebase_replay import replay  # noqa: E402


class DriverTests(queue_chain.Fixture):
    def prepare(self, **kwargs):
        members = [
            self.pr(101, {"A.lean": "a\n"}),
            self.pr(102, {"Shared.lean": "source\n"}),
            self.pr(103, {"C.lean": "c\n"}),
        ]
        built = chain.build(self.repo, self.base, members, chain_id="input")
        self.git("checkout", "-q", "main")
        self.write({"Shared.lean": "target\n"})
        self.target = self.commit("target")
        self.input_tip = built.tip
        self.state_dir = Path(self.tmp.name) / "driver"
        return Driver.create(self.repo, self.state_dir, self.base, built.tip, self.target, **kwargs)

    def resolve(self, driver):
        self.write({"Shared.lean": "resolved\n"})
        return driver.resolve(driver.status()["packet_id"], "Keep the target and source changes.")

    def test_conflict_packet_preservation_empty_and_final_check(self):
        driver = self.prepare()
        status = driver.advance()
        self.assertEqual((status["phase"], status["completed"]), ("conflict", 1))
        packet = json.loads(Path(status["packet"]).read_text())
        self.assertEqual(packet["source_pr"], "TauCetiProject/TauCeti#102")
        stages = packet["stages"]["Shared.lean"]
        self.assertEqual(Path(stages["1"]["file"]).read_text(), "line one\nline two\nline three\n")
        self.assertEqual(Path(stages["2"]["file"]).read_text(), "target\n")
        self.assertEqual(Path(stages["3"]["file"]).read_text(), "source\n")
        self.resolve(driver)
        driver = Driver(self.state_dir)
        self.assertEqual(driver.advance()["phase"], "complete")
        self.assertTrue(Driver(self.state_dir).check()["ok"])
        source = chain.chain_commits(self.repo, self.base, self.input_tip)
        result = chain.chain_commits(self.repo, self.target, driver.state["tip"])
        self.assertEqual([c.source_pr for c in source], [c.source_pr for c in result])
        self.assertIn("Repair: Keep the target and source changes.", result[1].message)
        for before, after in zip(source, result, strict=True):
            self.assertEqual(
                (before.author, before.author_date, before.accepted_head),
                (after.author, after.author_date, after.accepted_head),
            )
            self.assertTrue(after.message.startswith(before.message.rstrip()))

    def test_stale_duplicate_explanations_markers_and_scope(self):
        driver = self.prepare()
        packet = driver.advance()["packet_id"]
        with self.assertRaisesRegex(DriverError, "stale"):
            driver.resolve("unknown", "note")
        with self.assertRaisesRegex(DriverError, "explanation"):
            driver.resolve(packet, "")
        with self.assertRaisesRegex(DriverError, "markers"):
            driver.resolve(packet, "note")
        self.write({"Shared.lean": "resolved\n", "Other.lean": "unrelated\n"})
        with self.assertRaisesRegex(DriverError, "attribution"):
            driver.resolve(packet, "note")
        with self.assertRaisesRegex(DriverError, "TauCeti Lean"):
            driver.resolve(packet, "note", allow_paths=["Other.lean"])
        (self.repo / "Other.lean").unlink()
        driver.resolve(packet, "note")
        driver.advance()
        tip = driver.state["tip"]
        self.assertIn("already_resolved", driver.resolve(packet, "second submission"))
        self.assertEqual(driver.state["tip"], tip)

    def test_index_is_not_trusted_and_extra_source_paths_are_attributed(self):
        driver = self.prepare()
        driver.advance()
        (self.repo / "TauCeti").mkdir()
        self.write({"Shared.lean": "resolved\n", "TauCeti/Extra.lean": "extra\n"})
        self.git("add", "-A")
        self.git("read-tree", self.target)  # Agent staging cannot hide source edits.
        with self.assertRaisesRegex(DriverError, "attribution"):
            self.resolve(driver)
        driver.resolve(
            driver.status()["packet_id"], "Also adjust the dependent theorem.", allow_paths=["TauCeti/Extra.lean"]
        )
        driver.advance()
        self.assertTrue(driver.check()["ok"])
        self.assertIn("TauCeti/Extra.lean", driver.state["records"][1]["changed_paths"])

    def test_protected_member_rejected(self):
        driver = self.prepare(protected_prs=[102])
        driver.advance()
        with self.assertRaisesRegex(DriverError, "opted out"):
            driver.resolve(driver.status()["packet_id"], "A conflict requires a source repair.")
        with self.assertRaisesRegex(DriverError, "opted out"):
            self.resolve(driver)

    def test_symlink_rejected(self):
        driver = self.prepare()
        driver.advance()
        (self.repo / "Shared.lean").unlink()
        (self.repo / "Shared.lean").symlink_to(self.repo / "README.md")
        with self.assertRaisesRegex(DriverError, "symlink"):
            driver.resolve(driver.status()["packet_id"], "note")

    def test_head_ref_and_binding_drift_rejected(self):
        driver = self.prepare()
        driver.advance()
        self.git("update-ref", "HEAD", self.base)
        with self.assertRaisesRegex(DriverError, "HEAD changed"):
            driver.advance()
        self.git("update-ref", "HEAD", driver.state["checkout_head"])
        self.git("update-ref", driver.state["ref"], self.base)
        with self.assertRaisesRegex(DriverError, "ref changed"):
            driver.advance()
        state = json.loads(driver.state_path.read_text())
        state["plan"]["target"] = self.base
        driver.state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(DriverError, "binding"):
            Driver(self.state_dir)

    def test_restart_after_publication_before_materialization(self):
        driver = self.prepare()
        driver.advance()
        with patch.object(driver, "_finish_materialization", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.resolve(driver)
        fresh = Driver(self.state_dir)
        self.assertEqual(fresh.advance()["phase"], "complete")
        self.assertTrue(fresh.check()["ok"])
        self.assertEqual(len(fresh.state["records"]), 3)

    def test_materialization_recovery_rejects_external_head_change(self):
        driver = self.prepare()
        driver.advance()
        with patch.object(driver, "_finish_materialization", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.resolve(driver)
        self.git("update-ref", "HEAD", self.base)
        with self.assertRaisesRegex(DriverError, "HEAD changed"):
            Driver(self.state_dir).advance()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_restart_around_compare_and_swap(self):
        driver = self.prepare()
        driver.advance()
        with patch.object(driver, "_finish_transition", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.resolve(driver)
        # Simulate Git publishing the ref but a crash before checkpoint publication.
        transition = json.loads(driver.state_path.read_text())["transition"]
        self.git("update-ref", driver.state["ref"], transition["after"], transition["before"])
        fresh = Driver(self.state_dir)
        fresh.advance()
        self.assertTrue(fresh.check()["ok"])
        self.assertEqual(len(fresh.state["records"]), 3)

    def test_restart_before_compare_and_swap(self):
        driver = self.prepare()
        driver.advance()
        with patch.object(driver, "_finish_transition", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.resolve(driver)
        fresh = Driver(self.state_dir)
        fresh.advance()
        self.assertTrue(fresh.check()["ok"])
        self.assertEqual(len(fresh.state["records"]), 3)

    def test_restart_reexports_packet_after_interrupted_pause(self):
        driver = self.prepare()
        with patch.object(driver, "_export_packet", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                driver.advance()
        fresh = Driver(self.state_dir)
        status = fresh.advance()
        self.assertTrue(Path(status["packet"]).is_file())
        self.resolve(fresh)
        fresh.advance()
        self.assertTrue(fresh.check()["ok"])

    def test_empty_member_preserved(self):
        member = self.pr(101, {"A.lean": "a\n"})
        built = chain.build(self.repo, self.base, [member], chain_id="input")
        self.write({"A.lean": "a\n"})
        target = self.commit("already present")
        driver = Driver.create(self.repo, Path(self.tmp.name) / "state", self.base, built.tip, target)
        driver.advance()
        self.assertTrue(driver.check()["ok"])
        self.assertEqual(chain.tree_of(self.repo, driver.state["tip"]), chain.tree_of(self.repo, target))
        self.assertEqual(len(driver.state["records"]), 1)

    def test_modify_delete_resolution(self):
        self.git("checkout", "-q", "--detach", self.base)
        (self.repo / "Shared.lean").unlink()
        head = self.commit("remove source")
        self.git("checkout", "-q", "main")
        member = chain.Member(queue_chain.PROJECT, 101, head, title="Remove source")
        built = chain.build(self.repo, self.base, [member], chain_id="delete")
        self.write({"Shared.lean": "target modified\n"})
        target = self.commit("target")
        driver = Driver.create(self.repo, Path(self.tmp.name) / "state", self.base, built.tip, target)
        status = driver.advance()
        self.assertEqual(status["phase"], "conflict")
        (self.repo / "Shared.lean").unlink()
        driver.resolve(status["packet_id"], "Keep the intended deletion.")
        driver.advance()
        self.assertTrue(driver.check()["ok"])
        self.assertFalse((self.repo / "Shared.lean").exists())

    def test_generated_resolve_helper_and_cli_check(self):
        driver = self.prepare()
        status = driver.advance()
        self.write({"Shared.lean": "resolved\n"})
        note = Path(self.tmp.name) / "explanation.txt"
        note.write_text("Keep the target and source changes.")
        helper = self.repo / ".tauceti-repair"
        resolved = subprocess.run(
            [str(helper / "resolve"), "--conflict-id", status["packet_id"], "--explanation-file", str(note)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(resolved.stdout)["phase"], "complete")
        checked = subprocess.run([str(helper / "check")], capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(checked.stdout)["ok"])

    def test_oracle_attributes_late_build_correction_to_clean_member(self):
        driver = self.prepare()
        driver.advance()
        self.resolve(driver)
        driver.advance()
        gold = chain.chain_commits(self.repo, self.target, driver.state["tip"])
        # Rewrite the first member with a later build correction, and propagate its tree forward.
        parent = self.target
        for accepted in gold:
            tree = self.edit_tree(accepted.tree, "A.lean", "a corrected after the build\n")
            parent = self.raw_commit(tree, parent, accepted.message + "\nBuild correction.\n")
        checkpoint = Path(self.tmp.name) / "task.json"
        checkpoint.write_text(
            json.dumps(
                {
                    "claim_token": "must-not-copy",
                    "task": {
                        "base": self.base,
                        "tip": self.input_tip,
                        "onto": self.target,
                        "members": [{"number": n} for n in (101, 102, 103)],
                    },
                }
            )
        )
        dest = Path(self.tmp.name) / "replay"
        report = replay(self.repo, checkpoint, parent, dest)
        self.assertTrue(report["every_member_tree_matches"])
        self.assertEqual(report["recorded_corrections"], 1)
        self.assertEqual(report["git_conflicts"], 1)
        self.assertFalse((dest / "repo").exists())
        self.assertNotIn("must-not-copy", (dest / "report.json").read_text())
        self.assertEqual(report["corrections"][0]["source_pr"], "TauCetiProject/TauCeti#101")


if __name__ == "__main__":
    unittest.main()
