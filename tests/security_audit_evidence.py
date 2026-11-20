#!/usr/bin/env python3
"""The one evidence classification: FINDING > UNREADABLE > PASS > NOT_JUDGED, for both reviewers."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_evidence as ev
from tauceti_worker import security_audit as sa

PASS = {"verdict": "pass", "findings": []}
UNFINISHED = {"verdict": "unfinished", "findings": []}
FINDING = {"verdict": "finding", "findings": ["commit 1 TauCeti/A.lean:3: #eval reads the environment"]}
THREAD = "11111111-2222-4333-8444-555555555555"


def jsonl(path: Path, rows, tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(sa.serialize(r) + "\n" for r in rows) + tail)


def native_message(value, phase="final_answer") -> dict:
    text = value if isinstance(value, str) else sa.serialize(value)
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def native_call(name) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call", "name": name, "arguments": "{}"}}


def exec_answer(value) -> list[dict]:
    text = value if isinstance(value, str) else sa.serialize(value)
    return [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}, {"type": "turn.completed"}]


def claude_answer(value) -> list[dict]:
    use = {"type": "tool_use", "name": "StructuredOutput", "input": value}
    return [
        {"type": "assistant", "message": {"content": [use]}},
        {"type": "result", "subtype": "success", "structured_output": value},
    ]


class Classification(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "security-audit"
        self.attempt = self.state / "attempt-1"
        self.attempt.mkdir(parents=True)
        self.batch = {
            "from_sha": "a" * 40,
            "main_sha": "b" * 40,
            "commits": [{"sha": "b" * 40, "parent": "a" * 40, "files": []}],
        }
        self.batch["input_digest"] = hashlib.sha256(sa.serialize(self.batch).encode()).hexdigest()
        (self.attempt / "input.json").write_text(sa.serialize(self.batch))

    # Fixtures ----------------------------------------------------------------
    def codex(self, native, stream):
        (self.attempt / "execution.json").write_text(
            sa.serialize({"argv": ["codex", "exec"], "paged": False, "protocol_version": 5})
        )
        jsonl(
            self.attempt / "native-sessions/2026/rollout.jsonl",
            [{"type": "session_meta", "payload": {"id": THREAD}}, *native],
        )
        jsonl(self.attempt / "model-transcript.jsonl", [{"type": "thread.started", "thread_id": THREAD}, *stream])

    def claude(self, session, stream, tail=""):
        (self.attempt / "execution.json").write_text(
            sa.serialize({"argv": ["claude", "-p"], "paged": False, "protocol_version": 5})
        )
        jsonl(self.attempt / sa.CLAUDE_SESSIONS / "project" / f"{THREAD}.jsonl", session)
        jsonl(self.attempt / "model-transcript.jsonl", stream, tail)

    def outcome(self, failure=None):
        return ev.classify(self.state, self.attempt, self.batch, failure)

    def assertFinding(self, outcome, findings=FINDING["findings"]):
        self.assertEqual(outcome.kind, ev.FINDING, outcome)
        self.assertEqual(outcome.verdict["findings"], findings)
        self.assertEqual(sa.validate_verdict(outcome.verdict, self.batch), outcome.verdict)

    # PASS ----------------------------------------------------------------------
    def test_a_clean_complete_pass_passes_for_both_reviewers(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.PASS)
        self.assertEqual(self.outcome().verdict["verdict"], "pass")
        self.setUp()
        self.claude(claude_answer(PASS)[:1], claude_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.PASS)

    # FINDING beats everything ----------------------------------------------------
    def test_a_finding_followed_by_unfinished_or_a_later_pass_binds(self):
        for later in (UNFINISHED, PASS):
            with self.subTest(later=later["verdict"]):
                self.setUp()
                self.codex([native_message(FINDING), native_message(later)], exec_answer(later))
                self.assertFinding(self.outcome())
                self.setUp()
                self.claude([*claude_answer(FINDING)[:1], *claude_answer(later)[:1]], claude_answer(later))
                self.assertFinding(self.outcome())

    def test_a_finding_before_a_timeout_or_failed_exit_binds(self):
        for failure in (subprocess.TimeoutExpired(["codex"], 1800), "security-audit: codex failed"):
            self.setUp()
            self.codex([native_message(FINDING)], [])
            self.assertFinding(self.outcome(failure))
            self.setUp()
            self.claude(claude_answer(FINDING)[:1], [])
            self.assertFinding(self.outcome(failure))

    def test_unfinished_or_incomplete_with_findings_is_a_finding(self):
        coverage = {"verdict": "unfinished", "findings": ["Pages 21-45 remain unread; no source concern so far"]}
        self.codex([native_message(coverage)], exec_answer(coverage))
        self.assertFinding(self.outcome(), coverage["findings"])
        self.assertEqual(self.outcome().verdict["verdict"], "finding")
        self.setUp()
        explained = {"verdict": "incomplete", "findings": ["origin of `stacks` is uncertain"]}
        self.claude(claude_answer(explained)[:1], claude_answer(explained))
        self.assertFinding(self.outcome(), explained["findings"])
        self.assertEqual(self.outcome().verdict["verdict"], "incomplete")  # A legacy explained incomplete.

    def test_malformed_findings_still_bind(self):
        for value in (
            {**FINDING, "extra": True},
            {"verdict": "finding", "findings": []},
            {"verdict": "pass", "findings": ["IO in A.lean"]},
            {"verdict": "finding", "findings": "IO in A.lean"},
            {"findings": [{"path": "A.lean", "line": 3}]},
        ):
            with self.subTest(value=value):
                self.setUp()
                self.codex([native_message(value)], exec_answer(value))
                self.assertEqual(self.outcome().kind, ev.FINDING)

    def test_an_unauthorized_tool_does_not_hide_a_later_finding(self):
        self.codex([native_call("shell"), native_message(FINDING)], exec_answer(FINDING))
        self.assertFinding(self.outcome())

    def test_a_judgment_in_commentary_or_a_code_fence_binds(self):
        note = "Interim: ```json\n" + sa.serialize(FINDING) + "\n``` continuing."
        self.codex([native_message(note, phase="commentary"), native_message(PASS)], exec_answer(PASS))
        self.assertFinding(self.outcome())
        self.setUp()
        text = {"type": "assistant", "message": {"content": [{"type": "text", "text": note}]}}
        self.claude([text], claude_answer(PASS))
        self.assertFinding(self.outcome())

    def test_a_finding_only_in_an_archived_launch_or_the_continued_attempt_binds(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        jsonl(self.attempt / "service-error-1/model-transcript.jsonl", exec_answer(FINDING))
        self.assertFinding(self.outcome())
        self.setUp()
        parent = self.state / "attempt-0"
        jsonl(parent / "model-transcript.jsonl", claude_answer(FINDING))
        self.claude(claude_answer(PASS)[:1], claude_answer(PASS))
        (self.attempt / "previous-block.json").write_text(sa.serialize({"attempt": str(parent)}))
        self.assertFinding(self.outcome())

    def test_a_ledger_concern_for_the_same_input_binds(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        sa.record(self.state / "ledger.jsonl", {"batch": "other", "verdict": FINDING})
        self.assertEqual(self.outcome().kind, ev.PASS)
        sa.record(self.state / "ledger.jsonl", {"batch": self.batch["input_digest"], "verdict": FINDING})
        self.assertFinding(self.outcome())
        self.assertTrue(ev.recorded(self.state, self.batch).concerns())

    def test_a_ledger_concern_binds_every_range_containing_its_commits(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        other = {
            "input_digest": "other-plan",
            "commits": ["c" * 40, "b" * 40],
            "verdict": "finding",
            "findings": ["IO"],
        }
        sa.record(self.state / "ledger.jsonl", {"batch": "other-plan", "verdict": other})
        self.assertFinding(self.outcome(), ["IO"])
        self.setUp()
        self.codex([native_message(PASS)], exec_answer(PASS))
        (self.state / "ledger.jsonl").write_text('{"batch": "x", "verdict": {"commits": ["' + "b" * 40 + '"], "verd\n')
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    def test_an_empty_incomplete_names_no_concern(self):
        self.assertEqual(ev.concern({"verdict": "incomplete", "findings": []}), [])
        self.assertEqual(ev.concern({"verdict": "unfinished", "findings": [" "]}), [])

    def test_escaped_keys_and_json_carried_in_strings_still_bind(self):
        escaped = '{"\\u0076erdict":"finding","\\u0066indings":["unsafe IO"]}'
        self.assertEqual(json.loads(escaped), {"verdict": "finding", "findings": ["unsafe IO"]})
        self.codex([native_message(escaped), native_message(PASS)], exec_answer(PASS))
        self.assertFinding(self.outcome(), ["unsafe IO"])
        self.setUp()
        encoded = json.dumps(sa.serialize(FINDING))  # A JSON string whose content is the judgment.
        self.codex([native_message(encoded), native_message(PASS)], exec_answer(PASS))
        self.assertFinding(self.outcome())
        self.setUp()
        self.claude([*claude_answer(sa.serialize(FINDING))[:1], *claude_answer(PASS)[:1]], claude_answer(PASS))
        self.assertFinding(self.outcome())
        self.setUp()
        nested = {"answer": FINDING}
        self.claude(claude_answer(nested)[:1], claude_answer(PASS))
        self.assertFinding(self.outcome())

    def test_claude_result_text_and_codex_native_events_are_read(self):
        result = {"type": "result", "subtype": "success", "result": sa.serialize(FINDING)}
        self.claude(claude_answer(PASS)[:1], [*claude_answer(PASS), result])
        self.assertFinding(self.outcome())
        self.setUp()
        event = {"type": "event_msg", "payload": {"type": "agent_message", "message": sa.serialize(FINDING)}}
        self.codex([event, native_message(PASS)], exec_answer(PASS))
        self.assertFinding(self.outcome())

    def test_an_interrupted_exec_message_is_read_from_its_latest_update(self):
        updated = {"type": "item.updated", "item": {"id": "m1", "type": "agent_message", "text": sa.serialize(FINDING)}}
        self.codex([native_message(UNFINISHED)], [updated])
        self.assertFinding(self.outcome("security-audit: reviewer timed out"))
        self.setUp()
        # A completed message replaces its partial updates, which are never read as text.
        partial = {"type": "item.updated", "item": {"id": "m1", "type": "agent_message", "text": '{"verdict":"pa'}}
        done = {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": sa.serialize(PASS)}}
        self.codex([native_message(PASS)], [partial, done, {"type": "turn.completed"}])
        self.assertEqual(self.outcome().kind, ev.PASS)

    # UNREADABLE --------------------------------------------------------------------
    def test_a_malformed_judgment_beside_a_readable_one_is_unreadable(self):
        note = sa.serialize(PASS) + ' then {"verdict":"finding","findings":["unsafe IO"],}'
        self.codex([native_message(note, phase="commentary"), native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    def test_duplicate_keys_and_deep_encodings_cannot_hide_a_concern(self):
        duplicate = '{"verdict":"finding","findings":["unsafe IO"],"verdict":"pass","findings":[]}'
        self.codex([native_message(duplicate, phase="commentary"), native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)
        self.setUp()
        deep = sa.serialize(FINDING)
        for _ in range(8):
            deep = json.dumps(deep)
        self.codex([native_message(deep, phase="commentary"), native_message(PASS)], exec_answer(PASS))
        self.assertFinding(self.outcome())

    def test_an_answer_that_is_not_a_judgment_is_unreadable(self):
        self.codex([native_message("I found unsafe IO in A.lean."), native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)
        self.setUp()
        self.claude([*claude_answer("unsafe IO in A.lean")[:1], *claude_answer(PASS)[:1]], claude_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)
        self.setUp()
        # Commentary prose is not an answer, so it never holds by itself.
        self.codex(
            [native_message("Reading page 6 next.", phase="commentary"), native_message(PASS)], exec_answer(PASS)
        )
        self.assertEqual(self.outcome().kind, ev.PASS)

    # UNREADABLE --------------------------------------------------------------------
    def test_a_partial_row_that_could_hold_an_answer_is_unreadable(self):
        self.claude(claude_answer(UNFINISHED)[:1], [], tail='{"type":"assistant","message":{"con')
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)
        self.setUp()
        self.codex([native_message(UNFINISHED)], [])
        with (self.attempt / "model-transcript.jsonl").open("a") as f:
            f.write('{"type":"item.completed","item":{"type":"agent_message","text":"{\\"verdict\\":\\"fin')
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    def test_a_partial_tool_output_row_only_prevents_a_pass(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        with (self.attempt / "model-transcript.jsonl").open("a") as f:
            f.write('{"type":"item.completed","item":{"type":"mcp_tool_call","server":"audit","result":"Verdier')
        outcome = self.outcome()
        self.assertEqual(outcome.kind, ev.NOT_JUDGED)
        self.assertIn("unreadable rows", outcome.reason)

    def test_text_mentioning_a_judgment_that_cannot_be_read_is_unreadable(self):
        note = 'I will answer with "verdict": finding about the IO later'
        self.codex([native_message(note, phase="commentary"), native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    def test_a_damaged_ledger_row_naming_this_input_is_unreadable(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        ledger = self.state / "ledger.jsonl"
        ledger.write_text('{"batch":"other","verdict":{"verd\n')
        self.assertEqual(self.outcome().kind, ev.PASS)
        ledger.write_text('{"batch":"' + self.batch["input_digest"] + '","verdict":{"verd\n')
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    def test_a_cyclic_or_foreign_lineage_is_unreadable(self):
        self.codex([native_message(PASS)], exec_answer(PASS))
        (self.attempt / "previous-block.json").write_text(sa.serialize({"attempt": str(self.attempt)}))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)
        (self.attempt / "previous-block.json").write_text(sa.serialize({"attempt": "/elsewhere/attempt-9"}))
        self.assertEqual(self.outcome().kind, ev.UNREADABLE)

    # NOT_JUDGED ------------------------------------------------------------------------
    def test_no_concern_and_no_valid_pass_is_not_judged(self):
        cases = {
            "unfinished": ([native_message(UNFINISHED)], exec_answer(UNFINISHED), None),
            "unauthorized tool": ([native_call("shell"), native_message(PASS)], exec_answer(PASS), None),
            "provider failure": ([native_message(PASS)], exec_answer(PASS), "security-audit: codex failed"),
            "no answer": ([], [{"type": "turn.completed"}], None),
            "extra field on a pass": (
                [native_message({**PASS, "extra": 1})],
                exec_answer({**PASS, "extra": 1}),
                None,
            ),
        }
        for name, (native, stream, failure) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.codex(native, stream)
                self.assertEqual(self.outcome(failure).kind, ev.NOT_JUDGED)
        self.setUp()
        self.assertEqual(self.outcome().kind, ev.NOT_JUDGED)  # Never launched.

    def test_codex_mcp_resource_helpers_are_inert(self):
        calls = [native_call(name) for name in sorted(ev.INERT_TOOLS)]
        self.codex([*calls, native_message(PASS)], exec_answer(PASS))
        self.assertEqual(self.outcome().kind, ev.PASS)
        self.assertEqual(ev.INERT_TOOLS, frozenset(sa.INERT_TOOLS))

    def test_holds_reports_binding_evidence_only(self):
        self.assertFalse(ev.holds(self.state, self.state / "attempt-missing"))
        self.codex([native_message(UNFINISHED)], exec_answer(UNFINISHED))
        self.assertFalse(ev.holds(self.state, self.attempt))
        self.codex([native_message(FINDING)], exec_answer(UNFINISHED))
        self.assertTrue(ev.holds(self.state, self.attempt))

    def test_judged_raises_typed_outcomes(self):
        self.codex([native_message(UNFINISHED)], exec_answer(UNFINISHED))
        with self.assertRaises(ev.NotJudged):
            sa.judged(self.attempt, self.batch)
        note = 'I will answer with "verdict": finding about the IO later'
        self.codex([native_message(note, phase="commentary")], [])
        with self.assertRaises(ev.Unreadable):
            sa.judged(self.attempt, self.batch)
        self.codex([native_message(FINDING)], [])
        self.assertEqual(sa.judged(self.attempt, self.batch, "timed out")["verdict"], "finding")
        self.assertEqual(json.loads((self.attempt / "input.json").read_text()), self.batch)


if __name__ == "__main__":
    unittest.main()
