#!/usr/bin/env python3
"""The one evidence classification: FINDING > UNREADABLE > PASS > NOT_JUDGED, for API responses."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_evidence as ev
from tauceti_worker import security_audit as sa

PASS = {"verdict": "pass", "findings": []}
FINDING = {"verdict": "finding", "findings": ["commit 1 TauCeti/A.lean:3: #eval reads the environment"]}


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

    def respond(self, *blocks, stop="end_turn", protocol=sa.PROTOCOL, attempt=None, stage=ev.ESCALATION):
        """A retained response; by default the escalation reviewer's, which binds."""
        attempt = attempt or self.attempt
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / "execution.json").write_text(sa.serialize({"protocol_version": protocol}))
        content = [b if "type" in b else {"type": "text", "text": sa.serialize(b)} for b in blocks]
        if stage == ev.ESCALATION:
            (attempt / "escalation-request.json").write_text("{}")
        (attempt / f"{stage}response.json").write_text(sa.serialize({"content": content, "stop_reason": stop}))

    def kind(self, failure=None):
        return ev.classify(self.state, self.attempt, self.batch, failure)

    def ledger(self, row):
        with (self.state / "ledger.jsonl").open("a") as f:
            f.write(sa.serialize(row) + "\n")

    def test_a_clean_complete_pass_passes(self):
        self.respond({"type": "thinking", "thinking": ""}, PASS)
        outcome = self.kind()
        self.assertEqual(outcome.kind, ev.PASS, outcome)
        self.assertEqual(outcome.verdict["input_digest"], self.batch["input_digest"])

    def test_a_finding_binds_even_when_the_request_failed(self):
        self.respond(FINDING, stop="max_tokens")
        self.assertEqual(self.kind("security-audit: reviewer stopped with max_tokens").kind, ev.FINDING)

    def test_a_pass_that_did_not_end_normally_is_not_judged(self):
        for stop in ("max_tokens", "refusal"):
            self.respond(PASS, stop=stop)
            self.assertEqual(self.kind().kind, ev.NOT_JUDGED, stop)

    def test_a_pass_from_an_earlier_protocol_cannot_pass(self):
        self.respond(PASS, protocol=5)
        self.assertEqual(self.kind().kind, ev.NOT_JUDGED)

    def test_unexpected_response_content_prevents_a_pass_but_not_a_finding(self):
        self.respond({"type": "tool_use", "name": "x", "input": {}}, PASS)
        self.assertEqual(self.kind().kind, ev.NOT_JUDGED)
        self.respond({"type": "tool_use", "name": "x", "input": {}}, FINDING)
        self.assertEqual(self.kind().kind, ev.FINDING)

    def test_two_answers_are_not_a_pass(self):
        self.respond(PASS, PASS)
        self.assertEqual(self.kind().kind, ev.NOT_JUDGED)

    def test_an_answer_that_is_not_a_judgment_is_unreadable(self):
        self.respond({"type": "text", "text": "all fine"})
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_a_malformed_judgment_beside_a_readable_pass_is_unreadable(self):
        self.respond({"type": "text", "text": '{"verdict": "pass", "findings": [] } {"findings": ['})
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_duplicate_keys_cannot_hide_a_concern(self):
        self.respond({"type": "text", "text": '{"verdict": "finding", "verdict": "pass", "findings": []}'})
        self.assertNotEqual(self.kind().kind, ev.PASS)

    def test_an_unreadable_response_file_is_unreadable(self):
        (self.attempt / "execution.json").write_text(sa.serialize({"protocol_version": sa.PROTOCOL}))
        (self.attempt / "escalation-request.json").write_text("{}")
        (self.attempt / "escalation-response.json").write_text('{"content": [')
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_a_current_protocol_ledger_concern_binds_every_range_with_its_commits(self):
        self.respond(PASS)
        self.ledger({"protocol_version": sa.PROTOCOL, "verdict": {**FINDING, "commits": ["b" * 40]}})
        self.assertEqual(self.kind().kind, ev.FINDING)

    def test_an_earlier_protocol_ledger_concern_or_hold_does_not_bind(self):
        self.respond(PASS)
        self.ledger({"protocol_version": 5, "verdict": {**FINDING, "commits": ["b" * 40]}})
        self.ledger({"outcome": "unreadable", "commits": ["b" * 40]})
        self.assertEqual(self.kind().kind, ev.PASS)

    def test_a_current_protocol_unreadable_hold_binds(self):
        self.respond(PASS)
        self.ledger({"protocol_version": sa.PROTOCOL, "outcome": "unreadable", "commits": ["b" * 40]})
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_a_damaged_ledger_row_naming_this_input_is_unreadable(self):
        self.respond(PASS)
        with (self.state / "ledger.jsonl").open("a") as f:
            f.write('{"verdict": {"commits": ["' + "b" * 40 + '"\n')
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_a_finding_in_the_continued_attempt_binds(self):
        parent = self.state / "attempt-0"
        self.respond(FINDING, attempt=parent)
        self.respond(PASS)
        (self.attempt / "previous-block.json").write_text(json.dumps({"attempt": str(parent)}))
        self.assertEqual(self.kind().kind, ev.FINDING)

    def test_a_cyclic_or_foreign_lineage_is_unreadable(self):
        self.respond(PASS)
        (self.attempt / "previous-block.json").write_text(json.dumps({"attempt": "/elsewhere/attempt-0"}))
        self.assertEqual(self.kind().kind, ev.UNREADABLE)

    def test_no_response_is_not_judged(self):
        (self.attempt / "execution.json").write_text(sa.serialize({"protocol_version": sa.PROTOCOL}))
        self.assertEqual(self.kind("security-audit: reviewer request failed").kind, ev.NOT_JUDGED)
        self.assertFalse(ev.engaged(self.attempt))
        (self.attempt / "reviewer-error.json").write_text(json.dumps({"too_large": True}))
        self.assertTrue(ev.engaged(self.attempt))

    def test_holds_reports_binding_evidence_only(self):
        self.respond(PASS)
        self.assertFalse(ev.holds(self.state, self.attempt))
        self.respond(FINDING)
        self.assertTrue(ev.holds(self.state, self.attempt))

    def test_a_triage_pass_passes(self):
        self.respond(PASS, stage=ev.TRIAGE)
        self.assertEqual(self.kind().kind, ev.PASS)

    def test_a_triage_concern_or_unreadable_answer_escalates_and_never_binds(self):
        for answer in (FINDING, {"type": "text", "text": "all fine"}):
            with self.subTest(answer=answer):
                self.respond(answer, stage=ev.TRIAGE)
                self.assertEqual(self.kind().kind, ev.ESCALATE)
                self.assertFalse(ev.holds(self.state, self.attempt))
                with self.assertRaises(ev.NeedsEscalation):
                    sa.judged(self.attempt, self.batch)

    def test_once_escalated_only_the_escalation_reviewer_decides(self):
        self.respond(FINDING, stage=ev.TRIAGE)
        self.respond(PASS)
        self.assertEqual(self.kind().kind, ev.PASS)
        self.assertEqual(ev.triage_judgment(self.attempt)["judgments"], [FINDING])
        self.respond(FINDING)
        self.assertEqual(self.kind().kind, ev.FINDING)

    def test_an_escalation_that_never_answered_still_needs_escalation(self):
        self.respond(FINDING, stage=ev.TRIAGE)
        (self.attempt / "escalation-request.json").write_text("{}")
        with self.assertRaises(ev.NeedsEscalation):
            sa.judged(self.attempt, self.batch, "security-audit: claude-sonnet-5-5 request failed")

    def test_judged_raises_typed_outcomes(self):
        self.respond({"type": "text", "text": "all fine"})
        with self.assertRaises(ev.Unreadable):
            sa.judged(self.attempt, self.batch)
        self.respond(PASS, stop="refusal")
        with self.assertRaises(ev.NotJudged):
            sa.judged(self.attempt, self.batch, "security-audit: reviewer stopped with refusal")
        self.respond(FINDING)
        self.assertEqual(sa.judged(self.attempt, self.batch)["verdict"], "finding")


if __name__ == "__main__":
    unittest.main()
