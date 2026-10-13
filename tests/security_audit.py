#!/usr/bin/env python3
"""Cursor invariants, complete history coverage, authorization, and reviewer isolation."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cli
from tauceti_worker import security_audit as sa
from tauceti_worker.config import Die, NoProgress
from tauceti_worker.work_units import want


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / ".git"
        self.command("init", "-q", "-b", "main")
        self.base = self.commit("base", "TauCeti/A.lean", "theorem a : True := trivial\n")

    def command(self, *args):
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        return subprocess.check_output(["git", "-C", str(self.root), *args], env=env).decode().strip()

    def commit(self, message, path, content):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.command("add", "--", path)
        self.command("commit", "-qm", message)
        return self.command("rev-parse", "HEAD")

    def test_introduction_and_revert_are_separate_inputs(self):
        evil = self.commit("add", "TauCeti/A.lean", 'unsafe def a : IO Unit := IO.println "x"\n')
        fixed = self.commit("remove", "TauCeti/A.lean", "theorem a : True := trivial\n")
        batch = sa.plan_batch(self.repo, self.base, fixed)
        self.assertEqual([c["sha"] for c in batch["commits"]], [evil, fixed])
        self.assertIn("IO.println", batch["commits"][0]["files"][0]["after"])
        self.assertIn("IO.println", batch["commits"][1]["files"][0]["before"])

    def test_excluded_infrastructure_is_not_model_input(self):
        head = self.commit("tooling", "scripts/evil.py", "EXCLUDED_SECRET_SENTINEL")
        batch = sa.plan_batch(self.repo, self.base, head)
        self.assertFalse(batch["commits"][0]["files"])
        self.assertNotIn("EXCLUDED_SECRET_SENTINEL", sa.serialize(batch))

    def test_first_parent_rejects_merged_side_commit(self):
        self.command("checkout", "-qb", "side")
        side = self.commit("side", "TauCeti/Side.lean", "def side := 1\n")
        self.command("checkout", "-q", "main")
        self.commit("main", "TauCeti/Main.lean", "def main := 2\n")
        self.command("merge", "-q", "--no-ff", "side", "-m", "merge")
        head = self.command("rev-parse", "HEAD")
        with self.assertRaises(Die):
            sa.validate_history(self.repo, self.base, head, side)
        batch = sa.plan_batch(self.repo, self.base, head)
        self.assertIn("TauCeti/Side.lean", [f["path"] for f in batch["commits"][-1]["files"]])

    def test_oversized_commit_is_not_truncated(self):
        head = self.commit("big", "TauCeti/A.lean", "x" * sa.MAX_BYTES)
        with self.assertRaisesRegex(Die, "budget"):
            sa.plan_batch(self.repo, self.base, head)

    def test_batch_ends_before_oversized_commit(self):
        small = self.commit("small", "TauCeti/A.lean", "def x := 1\n")
        head = self.commit("big", "TauCeti/A.lean", "x" * sa.MAX_BYTES)
        batch = sa.plan_batch(self.repo, self.base, head)
        self.assertEqual([c["sha"] for c in batch["commits"]], [small])

    def test_normal_multifile_edit_fits_with_complete_images(self):
        content = "-- A reasonably sized mathematical file with explanation\n" * 800
        self.commit("large baseline", "TauCeti/A.lean", content)
        baseline = self.commit("another file", "TauCeti/B.lean", content)
        for name in ("A", "B"):
            (self.root / f"TauCeti/{name}.lean").write_text(content + "theorem x : True := trivial\n")
        self.command("add", ".")
        self.command("commit", "-qm", "edit both")
        batch = sa.plan_batch(self.repo, baseline, self.command("rev-parse", "HEAD"))
        self.assertGreater(len(sa.serialize(batch).encode()), 128 * 1024)
        self.assertEqual(len(batch["commits"][0]["files"]), 2)
        for f in batch["commits"][0]["files"]:
            self.assertEqual(f["before"], content)
            self.assertEqual(f["after"], content + "theorem x : True := trivial\n")

    def test_symlink_and_binary_are_blocked(self):
        p = self.root / "TauCeti/Link.lean"
        p.symlink_to("../../scripts/file")
        self.command("add", ".")
        self.command("commit", "-qm", "link")
        with self.assertRaisesRegex(Die, "unexpected"):
            sa.plan_batch(self.repo, self.base, self.command("rev-parse", "HEAD"))
        p.unlink()
        p.write_bytes(b"binary\0content")
        self.command("add", ".")
        self.command("commit", "-qm", "binary")
        with self.assertRaisesRegex(Die, "binary"):
            sa.image(self.repo, self.command("rev-parse", "HEAD"), "TauCeti/Link.lean")

    def test_caught_up_and_rewind(self):
        with self.assertRaises(NoProgress):
            sa.plan_batch(self.repo, self.base, self.base)
        with self.assertRaises(Die):
            sa.validate_history(self.repo, self.base, self.base, self.base)

    def test_commits_limited(self):
        for i in range(23):
            head = self.commit(str(i), "TauCeti/A.lean", f"def x := {i}\n")
        self.assertEqual(len(sa.plan_batch(self.repo, self.base, head)["commits"]), 20)


class Authority(unittest.TestCase):
    def test_phase_is_explicit_only(self):
        self.assertFalse(want([], "security-audit"))
        self.assertNotIn("security-audit", cli.resolve_tasks([], ["review"]))
        self.assertEqual(cli.resolve_tasks(["security-audit"], []), ["security-audit"])

    def test_admin_must_be_literal_true(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value), patch.object(sa, "api", return_value={"permissions": {"admin": value}}):
                with self.assertRaises(Die):
                    sa.require_admin()

    def test_permission_failure_does_not_authorize(self):
        with patch.object(sa, "api", side_effect=NoProgress("unavailable")), self.assertRaises(NoProgress):
            sa.require_admin()

    def test_concurrent_update_never_writes(self):
        with (
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", return_value="b" * 40),
            patch.object(sa, "api") as api,
            self.assertRaises(NoProgress),
        ):
            sa.advance(Path("unused"), {"from_sha": "a" * 40})
        api.assert_not_called()

    def test_revoked_admin_never_writes(self):
        with (
            patch.object(sa, "require_admin", side_effect=Die("revoked")),
            patch.object(sa, "api") as api,
            self.assertRaises(Die),
        ):
            sa.advance(Path("unused"), {})
        api.assert_not_called()

    def test_no_force_update_and_confirmed_sha(self):
        target, old, main = "c" * 40, "a" * 40, "b" * 40
        batch = {"from_sha": old, "commits": [{"sha": target}]}
        with (
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", side_effect=[old, main, target]),
            patch.object(sa, "refresh"),
            patch.object(sa, "validate_history") as history,
            patch.object(sa, "api", return_value={"object": {"sha": target}}) as api,
        ):
            self.assertEqual(sa.advance(Path("unused"), batch), target)
        history.assert_called_once_with(Path("unused"), old, main, target)
        self.assertEqual(api.call_args.kwargs["body"], {"sha": target, "force": False})

    def test_protection_bypass_is_separate(self):
        rules = sa.protections()
        self.assertEqual(len(rules), 4)
        for r in rules:
            if r["name"].endswith("admins"):
                self.assertEqual(
                    r["bypass_actors"], [{"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}]
                )
            else:
                self.assertEqual(r["bypass_actors"], [])

    def test_invalid_verdict_is_never_approval(self):
        batch = {"input_digest": "digest", "commits": [{"sha": "a" * 40}]}
        good = {"input_digest": "digest", "commits": ["a" * 40], "verdict": "pass", "findings": []}
        self.assertEqual(sa.validate_verdict(good, batch), good)
        for changes in (
            {"input_digest": "other"},
            {"commits": []},
            {"verdict": "incomplete"},
            {"findings": ["danger"]},
            {"verdict": "approve"},
        ):
            with self.subTest(changes=changes), self.assertRaises(Die):
                sa.validate_verdict({**good, **changes}, batch)

    def test_launcher_isolated_from_github_and_config(self):
        batch = {"input_digest": "digest", "commits": [{"sha": "a" * 40}]}
        verdict = {"input_digest": "digest", "commits": ["a" * 40], "verdict": "pass", "findings": []}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            auth = work / "auth.json"
            auth.write_text('{"tokens":{"access_token":"only-the-provider","refresh_token":"real-refresh-secret"}}')

            def model(argv, **kwargs):
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
                self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
                self.assertNotEqual(kwargs["cwd"], work)
                credentials = json.loads((kwargs["cwd"] / ".codex/auth.json").read_text())
                self.assertEqual(credentials["tokens"]["refresh_token"], sa.CODEX_RT_PLACEHOLDER)
                self.assertNotIn("real-refresh-secret", json.dumps(credentials))
                self.assertIn("--output-schema", argv)
                events = [
                    {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(verdict)}},
                    {"type": "turn.completed"},
                ]
                return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")

            with (
                patch.dict(os.environ, {"GH_TOKEN": "admin-secret", "GITHUB_TOKEN": "secret"}),
                patch.object(sa, "codex_dir", return_value=work),
                patch.object(sa.subprocess, "run", side_effect=model),
            ):
                self.assertEqual(sa.inspect(batch, "codex", work), verdict)
        self.assertIn("--ignore-user-config", sa.reviewer_argv("codex"))
        self.assertIn("shell_tool", sa.reviewer_argv("codex"))
        args = sa.reviewer_argv("claude")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)

    def test_publications_do_not_enter_the_replay_outbox(self):
        with patch.object(sa, "execute", return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")) as execute:
            sa.api("repos/example/repo/git/refs/heads/security-audit", method="PATCH", body={"sha": "a" * 40})
        self.assertEqual(execute.call_args.kwargs["env"]["TAUCETI_OUTBOX_REPLAY"], "1")

    def test_setup_accepts_github_normalized_existing_rules(self):
        protections = sa.protections()
        rules = [{"name": r["name"], "id": i} for i, r in enumerate(protections)]

        def response(path, **kwargs):
            self.assertFalse(kwargs, "idempotent setup must not write")
            if "?per_page=" in path:
                return rules
            if "/rulesets/" in path:
                rule_id = int(path.rsplit("/", 1)[1])
                return {**protections[rule_id], "id": rule_id, "source_type": "Repository"}
            return [{"ref": "refs/heads/security-audit"}]

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", return_value=sa.BOOTSTRAP),
            patch.object(sa, "refresh"),
            patch.object(sa, "validate_history"),
            patch.object(sa, "api", side_effect=response),
        ):
            sa.setup(Path(tmp))


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.old, self.target = "a" * 40, "b" * 40
        self.batch = {"from_sha": self.old, "input_digest": "digest", "commits": [{"sha": self.target, "files": [{}]}]}
        self.opts = SimpleNamespace(dry_run=False, work_model="codex", claude_bootstrap=False)
        for name, kwargs in (
            ("require_admin", {}),
            ("remote_sha", {"return_value": self.old}),
            ("refresh", {}),
            ("plan_batch", {"return_value": self.batch}),
            ("api", {"return_value": {"login": "admin"}}),
        ):
            mock = patch.object(sa, name, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def test_finding_blocks_repeated_model_calls(self):
        verdict = {"verdict": "finding", "findings": ["unsafe IO"]}
        with patch.object(sa, "inspect", return_value=verdict) as inspect, patch.object(sa, "advance") as advance:
            with self.assertRaises(Die):
                sa.run(SimpleNamespace(state=self.state), self.opts)
            self.assertTrue((self.state / sa.CURSOR / "blocked.json").exists())
            with self.assertRaises(NoProgress):
                sa.run(SimpleNamespace(state=self.state), self.opts)
            inspect.assert_called_once()
            advance.assert_not_called()

    def test_concurrent_update_does_not_create_a_block(self):
        with (
            patch.object(sa, "inspect", return_value={"verdict": "pass", "findings": []}),
            patch.object(sa, "advance", side_effect=NoProgress("concurrent")),
            self.assertRaises(NoProgress),
        ):
            sa.run(SimpleNamespace(state=self.state), self.opts)
        self.assertFalse((self.state / sa.CURSOR / "blocked.json").exists())

    def test_planning_failure_retains_manual_review_record(self):
        with patch.object(sa, "plan_batch", side_effect=Die("oversized")), self.assertRaises(Die):
            sa.run(SimpleNamespace(state=self.state), self.opts)
        blocked = json.loads((self.state / sa.CURSOR / "blocked.json").read_text())
        self.assertEqual(blocked["from_sha"], self.old)
        self.assertTrue(blocked["manual_inspection_required"])

    def test_attestation_records_exact_range_before_advance(self):
        with (
            patch.object(sa, "validate_history", return_value=[self.target, "c" * 40]),
            patch.object(sa, "advance", return_value=self.target) as advance,
        ):
            sa.attest(self.state, self.target, "Reviewed each commit manually")
        self.assertEqual(advance.call_args.args[1]["commits"], [{"sha": self.target}])
        ledger = [json.loads(line) for line in (self.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[0]["commits"], [self.target])
        self.assertIn("Reviewed", ledger[0]["human_attestation"])

    def test_empty_attestation_reason_never_updates(self):
        with patch.object(sa, "advance") as advance, self.assertRaises(Die):
            sa.attest(self.state, self.target, "   ")
        advance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
