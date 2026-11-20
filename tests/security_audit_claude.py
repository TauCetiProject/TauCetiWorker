#!/usr/bin/env python3
"""Haiku reviewers continue their own retained Claude session, and earlier judgments bind."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_client_check as check
from tauceti_worker import audit_clients as clients
from tauceti_worker import audit_evidence as evidence
from tauceti_worker import audit_reader as ar
from tauceti_worker import security_audit as sa
from tauceti_worker.config import NoProgress

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
        session, resume = argv[-1], argv[-2] == "--resume"
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
            patch.object(clients, "selected_protocol", return_value=5),
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

    def test_protocol_five_policy_and_launch(self):
        self.assertIn("unfinished", sa.JUDGMENT_PROMPT)
        self.assertIn("genuine uncertainty", sa.JUDGMENT_PROMPT)
        self.assertEqual(sa.JUDGMENT_SCHEMA["properties"]["verdict"]["enum"], ["pass", "finding", "unfinished"])
        argv = sa.claude_launch(self.root, True, self.root / "r.json", session=str(uuid.uuid4()))
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), sa.JUDGMENT_SCHEMA)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], sa.JUDGMENT_PROMPT + sa.JUDGMENT_COMPACTION)
        self.assertNotIn("--no-session-persistence", argv)

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
        self.assertTrue(second.prompt.startswith("The Worker has not received a judgment"))
        self.assertIn("next_unserved_page=2", second.prompt)
        session = sa.claude_sessions(attempt)[0]
        finals = [
            item["input"]
            for row in map(json.loads, session.read_text().splitlines())
            if row.get("type") == "assistant"
            for item in row["message"]["content"]
            if item.get("name") == "StructuredOutput"
        ]
        self.assertEqual(finals, [UNFINISHED, PASS])
        # Each launch's own output is kept; nothing else is copied.
        self.assertEqual(
            sorted(p.name for p in (attempt / "launch-1").iterdir()),
            ["execution.json", "model-stderr.txt", "model-transcript.jsonl", "prompt.txt"],
        )

    def test_reads_missing_from_the_saved_session_are_served_again(self):
        fake = FakeHaiku(
            self.batch,
            [{"pages": [0, 1], "outputs": [UNFINISHED]}, {"pages": range(self.pages), "outputs": [PASS]}],
        )
        attempt = self.attempt("1")
        saved = iter([set(), set(range(self.pages))])
        with (
            patch.object(sa, "run_process", side_effect=fake),
            patch.object(sa, "claude_saved_read_pages", side_effect=lambda *a: next(saved)),
        ):
            self.assertEqual(sa.inspect(self.batch, "claude", attempt), sa.bind_verdict(PASS, self.batch))
        self.assertIn("next_unserved_page=0", fake.calls[1].prompt)

    def test_an_earlier_concern_in_the_session_binds_a_later_pass_or_unfinished(self):
        uncertain = {"verdict": "finding", "findings": ["commit 1 TauCeti/ascii.lean: unclear elaborator origin"]}
        for later in (PASS, UNFINISHED):
            with self.subTest(later=later["verdict"]):
                attempt = self.attempt(later["verdict"])
                fake = FakeHaiku(self.batch, [{"pages": range(self.pages), "outputs": [uncertain, later]}])
                with patch.object(sa, "run_process", side_effect=fake):
                    self.assertEqual(sa.inspect(self.batch, "claude", attempt), sa.bind_verdict(uncertain, self.batch))
                self.assertEqual(len(fake.calls), 1)  # A concern ends the attempt; it is never continued.

    def test_a_timeout_ends_the_attempt_without_a_judgment(self):
        attempt = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0, 1], "timeout": True}])
        with patch.object(sa, "run_process", side_effect=fake), self.assertRaises(evidence.NotJudged) as stopped:
            sa.inspect(self.batch, "claude", attempt)
        self.assertIsInstance(stopped.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(len(fake.calls), 1)

    def test_three_charged_launches_without_progress_end_the_attempt(self):
        attempt = self.attempt("1")
        fake = FakeHaiku(self.batch, [{"pages": [0], "outputs": [UNFINISHED]}] + [{"outputs": [UNFINISHED]}] * 3)
        with patch.object(sa, "run_process", side_effect=fake), self.assertRaises(evidence.NotJudged):
            sa.inspect(self.batch, "claude", attempt)
        self.assertEqual(len(fake.calls), 4)  # The first launch served a page and was not charged.

    def test_an_unverified_client_waits_rather_than_using_an_older_protocol(self):
        attempt = self.attempt("1")
        with (
            patch.object(clients, "selected_protocol", return_value=None),
            patch.object(sa, "run_process", side_effect=AssertionError("no launch")),
            self.assertRaisesRegex(NoProgress, "awaiting client delivery verification"),
        ):
            sa.inspect(self.batch, "claude", attempt)

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
        self.assertTrue(evidence.holds(self.work, attempt))
        events[1] = {"type": "result", "subtype": "success", "structured_output": UNFINISHED}
        (attempt / "model-transcript.jsonl").write_text("".join(sa.serialize(e) + "\n" for e in events))
        self.assertFalse(evidence.holds(self.work, attempt))
        # A judgment written as an assistant note counts as one.
        events[0]["message"]["content"][0]["text"] = "```json\n" + sa.serialize(uncertain) + "\n```"
        (attempt / "model-transcript.jsonl").write_text("".join(sa.serialize(e) + "\n" for e in events))
        self.assertTrue(evidence.holds(self.work, attempt))
        # An interrupted line that might hold a judgment is unreadable, never ignored.
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1])
            + '\n{"type":"assistant","message":{"content":[{"name":"StructuredOutput","input":{"verdict":"fin'
        )
        self.assertTrue(evidence.holds(self.work, attempt))
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1])
            + '\n{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"{\\"page\\":3,'
        )
        self.assertFalse(evidence.holds(self.work, attempt))
        # Truncated before its kind is visible: it could have been an answer.
        (attempt / "model-transcript.jsonl").write_text(
            sa.serialize(events[1]) + '\n{"type":"assistant","message":{"con'
        )
        self.assertTrue(evidence.holds(self.work, attempt))

    def test_a_finding_before_a_timeout_binds_and_survives_a_lost_session(self):
        first = self.attempt("1")
        finding = {"verdict": "finding", "findings": ["commit 1: run_cmd executes a process"]}
        fake = FakeHaiku(self.batch, [{"pages": [0], "outputs": [finding], "timeout": True}])
        with patch.object(sa, "run_process", side_effect=fake):
            self.assertEqual(sa.inspect(self.batch, "claude", first), sa.bind_verdict(finding, self.batch))
        # Without the retained session, the captured transcript still binds.
        for path in sa.claude_sessions(first):
            path.unlink()
        self.assertTrue(evidence.holds(self.work, first))
        self.assertEqual(sa.judged(first, self.batch, "timed out"), sa.bind_verdict(finding, self.batch))


if __name__ == "__main__":
    unittest.main()
