#!/usr/bin/env python3
"""Haiku reviewers continue their own retained Claude session, and earlier judgments bind."""

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

from tauceti_worker import audit_client_check as check
from tauceti_worker import audit_clients as clients
from tauceti_worker import audit_pool as pool
from tauceti_worker import audit_reader as ar
from tauceti_worker import audit_recovery as recovery
from tauceti_worker import security_audit as sa
from tauceti_worker.config import Die

UNFINISHED = {"verdict": "unfinished", "findings": []}
PASS = {"verdict": "pass", "findings": []}


class FakeHaiku:
    """The claude CLI as the Worker sees it: a session file under CLAUDE_CONFIG_DIR/projects and stream-json.

    Layout and flags follow the real client (2.1.x): `--session-id` names a new
    session stored as `projects/<cwd with non-alphanumerics as '-'>/<id>.jsonl`;
    `--resume <id>` finds that file in any project directory and appends to it.
    """

    def __init__(self, batch, launches):
        self.batch = batch
        self.launches = list(launches)
        self.calls = []

    def __call__(self, argv, *, input, cwd, env, timeout, **kwargs):
        plan = self.launches.pop(0)
        self.calls.append(SimpleNamespace(argv=list(argv), prompt=input, home=Path(cwd), env=dict(env)))
        session, resume = sa.claude_session_arg(argv)
        projects = Path(env["CLAUDE_CONFIG_DIR"]) / "projects"
        existing = list(projects.rglob(f"{session}.jsonl"))
        if resume and not existing:
            stdout = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True})
            return SimpleNamespace(returncode=1, stdout=stdout + "\n", stderr="No conversation found")
        if not resume and existing:
            raise AssertionError("--session-id reused an existing session")
        path = (
            existing[0]
            if existing
            else projects / "".join(c if c.isalnum() else "-" for c in str(cwd)) / (session + ".jsonl")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        args = config["mcpServers"]["audit"]["args"]
        reader = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
        self.calls[-1].served = reader.coverage()["served"]
        stream = [{"type": "system", "subtype": "init", "session_id": session}]
        rows = [{"type": "user", "sessionId": session, "message": {"role": "user", "content": input}}]
        for page in plan.get("pages", ()):
            result = reader.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            call = f"toolu_{len(self.calls)}_{page}"
            use = {"type": "tool_use", "id": call, "name": "mcp__audit__read_page", "input": {"page": page}}
            back = {"type": "tool_result", "tool_use_id": call, "content": result["content"]}
            stream += [
                {"type": "assistant", "session_id": session, "message": {"content": [use]}},
                {"type": "user", "session_id": session, "message": {"content": [back]}},
            ]
            rows += [
                {"type": "assistant", "sessionId": session, "message": {"role": "assistant", "content": [use]}},
                {"type": "user", "sessionId": session, "message": {"role": "user", "content": [back]}},
            ]
        for i, value in enumerate(plan.get("outputs", ())):
            use = {"type": "tool_use", "id": f"so_{len(self.calls)}_{i}", "name": "StructuredOutput", "input": value}
            stream.append({"type": "assistant", "session_id": session, "message": {"content": [use]}})
            rows.append({"type": "assistant", "sessionId": session, "message": {"role": "assistant", "content": [use]}})
        with path.open("a") as f:
            f.write("".join(sa.serialize(row) + "\n" for row in rows))
        stdout = "".join(sa.serialize(event) + "\n" for event in stream)
        if plan.get("timeout"):
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout.encode(), stderr=b"")
        outputs = plan.get("outputs", ())
        stdout += sa.serialize(
            {"type": "result", "subtype": "success", "session_id": session, "structured_output": outputs[-1]}
        )
        return SimpleNamespace(returncode=0, stdout=stdout + "\n", stderr="")


class HaikuContinuation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / "security-audit"
        self.work.mkdir()
        (self.root / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"fixture"}}')
        self.batch = check.test_batch()
        self.pages = len(ar.pages(self.batch))
        self.assertGreater(self.pages, 4)
        for p in (
            patch.object(clients, "selected_protocol", return_value=4),
            patch.object(sa, "claude_dir", return_value=self.root),
            patch.object(sa.time, "sleep"),
            patch.dict(os.environ, {sa.API_KEY_FILE_ENV: ""}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def attempt(self, name):
        attempt = self.work / f"attempt-{name}"
        attempt.mkdir()
        (attempt / "input.json").write_text(sa.serialize(self.batch))
        return attempt

    def block(self, attempt, **extra):
        return {
            "from_sha": self.batch["from_sha"],
            "to_sha": self.batch["commits"][-1]["sha"],
            "reason": "security-audit: inspection unfinished; retained for same-session continuation",
            "attempt": str(attempt),
            **extra,
        }

    def assert_isolated(self, argv):
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertNotIn("--no-session-persistence", argv)
        self.assertNotIn("--safe-mode", argv)  # Removed only so the explicit reader loads.
        for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__audit__read_page")
        self.assertEqual(set(json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]), {"audit"})
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertIn("unfinished", schema["properties"]["verdict"]["enum"])

    def test_separate_usage_login_missing_agent_file_never_uses_keychain(self):
        (self.root / ".credentials.json").unlink()
        from tauceti_worker import quota

        with (
            patch.dict(os.environ, TAUCETI_CLAUDE_USAGE_CREDENTIALS=str(self.root / "usage.json")),
            patch.object(
                quota, "_claude_keychain_creds", side_effect=AssertionError("wrong account fallback")
            ) as keychain,
            patch.object(sa.subprocess, "run") as launch,
        ):
            with self.assertRaisesRegex(Die, "refusing Keychain fallback"):
                sa.inspect(self.batch, "claude", self.attempt("missing-credential"), claude_credential="subscription")
        keychain.assert_not_called()
        launch.assert_not_called()

    def test_separate_usage_login_expired_agent_is_refused_before_launch(self):
        (self.root / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "expired", "expiresAt": 1, "refreshToken": "source-owned"}})
        )
        with (
            patch.dict(os.environ, TAUCETI_CLAUDE_USAGE_CREDENTIALS=str(self.root / "usage.json")),
            patch.object(sa.subprocess, "run") as launch,
        ):
            with self.assertRaisesRegex(Die, "expired"):
                sa.inspect(self.batch, "claude", self.attempt("expired-credential"), claude_credential="subscription")
        launch.assert_not_called()

    def test_policy_has_luna_continuation_semantics(self):
        self.assertEqual(sa.SERVED_CLAUDE_PROMPT, sa.V2_MODEL_PROMPT + sa.SERVED_READING_POLICY)
        self.assertEqual(sa.SERVED_CLAUDE_PROMPT, sa.SERVED_MODEL_PROMPT)
        self.assertIn('return verdict "unfinished"', sa.SERVED_CLAUDE_PROMPT)
        self.assertIn("--no-session-persistence", sa.reviewer_argv("claude", protocol=3))
        self.assertNotIn("--no-session-persistence", sa.reviewer_argv("claude", protocol=4))

    def test_unfinished_turn_resumes_the_same_session_to_a_pass(self):
        fake = FakeHaiku(
            self.batch,
            [{"pages": [0, 1], "outputs": [UNFINISHED]}, {"pages": range(2, self.pages), "outputs": [PASS]}],
        )
        attempt = self.attempt("1")
        with patch.object(sa, "run_process", side_effect=fake):
            verdict = sa.inspect(self.batch, "claude", attempt)
        self.assertEqual(verdict, sa.bind_verdict(PASS, self.batch))
        first, second = fake.calls
        session = sa.claude_thread(attempt)
        self.assertEqual(first.argv[-2:], ["--session-id", session])
        self.assertEqual(second.argv[-2:], ["--resume", session])
        self.assertEqual(first.argv[:-2], second.argv[:-2])
        self.assert_isolated(first.argv)
        self.assertEqual(first.home, second.home)
        self.assertTrue(
            second.prompt.startswith(sa.continuation_prompt(self.batch, attempt / "coverage-before.json", protocol=4))
        )
        self.assertIn(sa.SERVED_CONTINUE_PROMPT, second.prompt)
        # The live session was written straight into the attempt, and holds both turns.
        finals = recovery.claude_finals(attempt)
        self.assertEqual(finals, [UNFINISHED, PASS])
        self.assertTrue((attempt / "coverage-turn-1" / "model-transcript.jsonl").exists())

    def test_retained_session_continues_in_a_fresh_home_to_a_pass(self):
        first = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0, 1], "outputs": [UNFINISHED]}])
        with patch.object(sa, "run_process", side_effect=fake), patch.object(sa, "MAX_INVOCATION_LAUNCHES", 1):
            with self.assertRaisesRegex(Die, "inspection unfinished"):
                sa.inspect(self.batch, "claude", first)
        original, batch, execution = recovery.retained_claude(self.work, self.block(first))
        self.assertEqual((original, batch), (first, self.batch))
        second = self.attempt("2")
        resumed = FakeHaiku(self.batch, [{"pages": range(2, self.pages), "outputs": [PASS]}])
        with patch.object(sa, "run_process", side_effect=resumed):
            verdict = sa.inspect(self.batch, "claude", second, resume_from=first, continue_reading=True)
        self.assertEqual(verdict, sa.bind_verdict(PASS, self.batch))
        (call,) = resumed.calls
        self.assertEqual(call.argv[-2:], ["--resume", sa.claude_thread(first)])
        self.assertNotEqual(call.home, fake.calls[0].home)
        self.assertEqual(sa.claude_thread(second), sa.claude_thread(first))
        self.assertEqual(recovery.claude_finals(second), [UNFINISHED, PASS])

    def test_earlier_uncertainty_in_the_session_binds_a_later_pass(self):
        uncertain = {"verdict": "incomplete", "findings": ["commit 1 TauCeti/ascii.lean: unclear elaborator origin"]}
        attempt = self.attempt("1")
        # The client re-asks for structured output; the earlier uncertainty is still recorded.
        fake = FakeHaiku(self.batch, [{"pages": range(self.pages), "outputs": [uncertain, PASS]}])
        with patch.object(sa, "run_process", side_effect=fake):
            verdict = sa.inspect(self.batch, "claude", attempt)
        self.assertEqual(verdict, sa.bind_verdict(uncertain, self.batch))
        self.assertFalse(pool.clean_history(attempt))

    def test_earlier_finding_in_a_continued_attempt_binds_its_pass(self):
        finding = {"verdict": "finding", "findings": ["commit 1 TauCeti/ascii.lean: #eval reads the environment"]}
        first = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0, 1], "outputs": [finding, UNFINISHED]}])
        with patch.object(sa, "run_process", side_effect=fake), patch.object(sa, "MAX_INVOCATION_LAUNCHES", 1):
            with self.assertRaisesRegex(Die, "inspection unfinished"):
                sa.inspect(self.batch, "claude", first)
        with self.assertRaisesRegex(Die, "forbid automatic recovery"):
            recovery.retained_claude(self.work, self.block(first))
        self.assertFalse(pool.clean_history(first))
        # Even forced, the continued session cannot discard the earlier finding.
        second = self.attempt("2")
        resumed = FakeHaiku(self.batch, [{"pages": range(2, self.pages), "outputs": [PASS]}])
        with patch.object(sa, "run_process", side_effect=resumed):
            verdict = sa.inspect(self.batch, "claude", second, resume_from=first)
        self.assertEqual(verdict, sa.bind_verdict(finding, self.batch))

    def test_judgment_only_in_the_captured_transcript_still_binds(self):
        attempt = self.attempt("1")
        (attempt / "execution.json").write_text(sa.serialize({"argv": ["claude", "-p"]}))
        uncertain = {"verdict": "incomplete", "findings": ["commit 1: unresolved indirection"]}
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the input."}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "StructuredOutput", "input": uncertain}]},
            },
        ]
        (attempt / "model-transcript.jsonl").write_text("".join(sa.serialize(e) + "\n" for e in events))
        self.assertFalse(pool.clean_history(attempt))
        events[1] = {"type": "result", "subtype": "success", "structured_output": UNFINISHED}
        (attempt / "model-transcript.jsonl").write_text("".join(sa.serialize(e) + "\n" for e in events))
        self.assertTrue(pool.clean_history(attempt))
        # A judgment written as an assistant note counts as one.
        events[0]["message"]["content"][0]["text"] = "```json\n" + sa.serialize(uncertain) + "\n```"
        (attempt / "model-transcript.jsonl").write_text("".join(sa.serialize(e) + "\n" for e in events))
        self.assertFalse(pool.clean_history(attempt))
        # An interrupted line that might hold a judgment is unreadable, never ignored.
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1])
            + '\n{"type":"assistant","message":{"content":[{"name":"StructuredOutput","input":{"verdict":"fin'
        )
        self.assertFalse(pool.clean_history(attempt))
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1])
            + '\n{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"{\\"page\\":3,'
        )
        self.assertTrue(pool.clean_history(attempt))
        # Truncated before its kind is visible: it could have been an answer.
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1]) + '\n{"type":"assistant","message":{"con'
        )
        self.assertFalse(pool.clean_history(attempt))

    def test_timeout_mid_session_continues_in_the_pool_rather_than_restarting(self):
        first = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0, 1, 2], "timeout": True}])
        with patch.object(sa, "run_process", side_effect=fake), self.assertRaises(subprocess.TimeoutExpired):
            sa.inspect(self.batch, "claude", first)
        self.assertEqual(ar.Reader(self.batch, first / "read-receipt.json").coverage()["served"], [0, 1, 2])
        # The client reported page 2, but its result never reached the stored session:
        # the resumed session must be served that page again.
        session = sa.claude_sessions(first)[0]
        session.write_text("".join(session.read_text().splitlines(keepends=True)[:-1]))
        block = self.block(first, reason="Command timed out", failure_kind="provider-timeout")
        self.assertTrue(pool.operational(subprocess.TimeoutExpired(["claude"], 1800), first, "claude"))
        p = pool.Pool(self.work, parallel=1, chooser=None)
        second = self.attempt("2")
        resumed = FakeHaiku(self.batch, [{"pages": range(2, self.pages), "outputs": [PASS]}])
        with patch.object(sa, "run_process", side_effect=resumed):
            verdict = p.inspect(self.batch, second, "claude", "subscription", 4, block)
        self.assertEqual(verdict, sa.bind_verdict(PASS, self.batch))
        (call,) = resumed.calls
        self.assertEqual(call.argv[-2:], ["--resume", sa.claude_thread(first)])
        self.assertEqual(call.served, [0, 1])
        self.assertTrue(call.prompt.startswith(sa.SERVED_CONTINUE_PROMPT) or "next_unserved_page=2" in call.prompt)
        self.assertEqual(json.loads((second / "previous-block.json").read_text()), block)

    def test_pool_refuses_a_fresh_attempt_when_the_stopped_transcript_holds_a_finding(self):
        first = self.attempt("1")
        finding = {"verdict": "finding", "findings": ["commit 1: run_cmd executes a process"]}
        fake = FakeHaiku(self.batch, [{"pages": [0], "outputs": [finding], "timeout": True}])
        with patch.object(sa, "run_process", side_effect=fake), self.assertRaises(subprocess.TimeoutExpired):
            sa.inspect(self.batch, "claude", first)
        # Without the retained session, continuation is impossible; the transcript still binds.
        for path in sa.claude_sessions(first):
            path.unlink()
        self.assertFalse(pool.operational(subprocess.TimeoutExpired(["claude"], 1800), first, "claude"))
        p = pool.Pool(self.work, parallel=1, chooser=None)
        second = self.attempt("2")
        with patch.object(sa, "run_process", side_effect=AssertionError("no fresh launch")):
            with self.assertRaisesRegex(Die, "fresh attempt would discard"):
                p.inspect(self.batch, second, "claude", "subscription", 4, self.block(first))

    def test_pool_starts_fresh_only_when_nothing_valid_is_retained_and_the_output_is_clean(self):
        first = self.attempt("1")
        (first / "execution.json").write_text(sa.serialize({"argv": ["claude", "-p"], "protocol_version": 4}))
        (first / "model-transcript.jsonl").write_text(
            sa.serialize({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading page 0."}]}})
            + "\n"
        )
        p = pool.Pool(self.work, parallel=1, chooser=None)
        second = self.attempt("2")
        fresh = FakeHaiku(self.batch, [{"pages": range(self.pages), "outputs": [PASS]}])
        with patch.object(sa, "run_process", side_effect=fresh):
            verdict = p.inspect(self.batch, second, "claude", "subscription", 4, self.block(first))
        self.assertEqual(verdict, sa.bind_verdict(PASS, self.batch))
        self.assertEqual(fresh.calls[0].argv[-2], "--session-id")
        self.assertNotEqual(fresh.calls[0].argv[-1], None)

    def test_retained_launch_must_match_the_isolated_profile(self):
        first = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0, 1], "outputs": [UNFINISHED]}])
        with patch.object(sa, "run_process", side_effect=fake), patch.object(sa, "MAX_INVOCATION_LAUNCHES", 1):
            with self.assertRaises(Die):
                sa.inspect(self.batch, "claude", first)
        execution = json.loads((first / "execution.json").read_text())
        for tamper in (
            lambda argv: argv.remove("--restricted"),
            lambda argv: argv.insert(2, "--dangerously-skip-permissions"),
            lambda argv: argv.__setitem__(argv.index("--tools") + 1, "Bash"),
            lambda argv: argv.__setitem__(-1, "22222222-2222-4333-8444-555555555555"),
        ):
            changed = json.loads(sa.serialize(execution))
            tamper(changed["argv"])
            (first / "execution.json").write_text(sa.serialize(changed))
            with self.assertRaises(Die):
                recovery.retained_claude(self.work, self.block(first))
        (first / "execution.json").write_text(sa.serialize(execution))
        recovery.retained_claude(self.work, self.block(first))


if __name__ == "__main__":
    unittest.main()
