#!/usr/bin/env python3
"""Protocol 4 numbered reads, conservative served coverage and retained judgments."""

import concurrent.futures
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import security_audit as historical

from tauceti_worker import audit_client_check as check
from tauceti_worker import audit_clients as clients
from tauceti_worker import audit_progress as progress
from tauceti_worker import audit_reader as ar
from tauceti_worker import audit_recovery as recovery
from tauceti_worker import security_audit as sa
from tauceti_worker.config import Die, NoProgress


def read_events(provider, pages, *, failed=False):
    events = []
    for page in pages:
        if provider == "codex":
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": str(page),
                        "type": "mcp_tool_call",
                        "server": "audit",
                        "tool": "read_page",
                        "arguments": {"page": page},
                        "status": "failed" if failed else "completed",
                        "error": {"message": "client timeout"} if failed else None,
                        "result": None if failed else {"content": []},
                    },
                }
            )
        else:
            events.extend(
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": str(page),
                                    "name": "mcp__audit__read_page",
                                    "input": {"page": page},
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": str(page), "is_error": failed, "content": []}
                            ]
                        },
                    },
                ]
            )
    return events


def native_read_rows(pages, batch):
    expected = ar.pages(batch)
    rows = []
    for page in pages:
        rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_page",
                        "arguments": sa.serialize({"page": page}),
                        "call_id": f"read-{page}",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"read-{page}",
                        "output": [
                            {"type": "input_text", "text": sa.serialize({"page": page, "sections": expected[page]})}
                        ],
                    },
                },
            ]
        )
    return rows


def append_native_reads(home, pages, thread, batch):
    directory = home / ".codex/sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = next(directory.glob("*.jsonl"), directory / "rollout.jsonl")
    if not path.exists():
        path.write_text(sa.serialize({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    with path.open("a") as stream:
        stream.write("\n".join(map(sa.serialize, native_read_rows(pages, batch))) + "\n")


class NumberedReader(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.batch = check.test_batch()
        self.receipt = self.root / "served.json"
        self.reader = ar.Reader(self.batch, self.receipt)

    def read(self, page, reader=None, **kwargs):
        return (reader or self.reader).request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}}, **kwargs)

    def test_exact_source_reconstruction_and_encoded_size_with_unicode_and_escaping(self):
        self.assertGreater(len(self.reader.pages), 3)
        collected = {}
        for page in range(len(self.reader.pages)):
            result = self.read(page)
            self.assertLessEqual(len(ar.encode(result).encode()), ar.RESPONSE_BYTES)
            text = result["content"][0]["text"]
            self.assertNotIn('"next_token"', text)
            for section in json.loads(text)["sections"]:
                key = section["path"], section["field"]
                self.assertEqual(section["start"], len(collected.get(key, "")))
                collected[key] = collected.get(key, "") + (section["text"] or "")
        for file in self.batch["commits"][0]["files"]:
            for field in ("before", "after", "diff"):
                self.assertEqual(collected[file["path"], field], file[field])
        self.assertEqual(self.reader.coverage()["served"], list(range(len(self.reader.pages))))

    def test_skip_rejected_reread_is_identical_and_restart_retains_progress(self):
        with self.assertRaisesRegex(ValueError, "cannot be skipped"):
            self.read(1)
        self.assertFalse(self.receipt.exists())
        first = self.read(0)
        original = self.receipt.read_bytes()
        again = ar.Reader(self.batch, self.receipt)
        self.assertEqual(self.read(0, again), first)
        self.assertEqual(original, self.receipt.read_bytes())
        self.read(1, again)
        self.assertEqual(again.coverage()["served"], [0, 1])

    def test_invalid_arguments_and_receipts_never_gain_coverage(self):
        for args in ({"page": True}, {"page": -1}, {"page": 0, "token": "anything"}, {}, {"page": "0"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.reader.request("tools/call", {"name": ar.TOOL, "arguments": args})
        self.read(0)
        valid = self.reader.coverage()
        for update in (
            {"input_digest": "other"},
            {"pages": True},
            {"served": [1]},
            {"served": [0, 0]},
            {"served": [False]},
            {"protocol_version": 3},
            {"extra": 1},
        ):
            self.receipt.write_text(ar.encode({**valid, **update}))
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.read(1)

    def test_oversized_response_and_failed_output_flush_do_not_credit_page(self):
        with patch.object(ar, "RESPONSE_BYTES", 1), self.assertRaisesRegex(ValueError, "allowance"):
            self.read(0)
        self.assertFalse(self.receipt.exists())

        def broken_pipe(result):
            self.assertFalse(self.receipt.exists())
            raise BrokenPipeError("client exited")

        with self.assertRaises(BrokenPipeError):
            self.read(0, emit=broken_pipe)
        self.assertFalse(self.receipt.exists())
        delivered = []
        self.read(0, emit=delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.reader.coverage()["served"], [0])

    def test_concurrent_readers_cannot_drop_progress(self):
        self.read(0)

        def read_again(_):
            return self.read(0, ar.Reader(self.batch, self.receipt))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(read_again, range(24)))
        self.assertEqual(self.reader.coverage()["served"], [0])
        self.read(1)
        self.assertEqual(self.reader.coverage()["served"], [0, 1])

    def test_stdio_server_flushes_exact_response_then_records_coverage(self):
        source = self.root / "input.json"
        source.write_text(ar.encode(self.batch))
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": ar.TOOL, "arguments": {"page": 0}},
        }
        expected = self.read(0)
        self.receipt.unlink()
        process = subprocess.run(
            [sys.executable, "-I", str(sa.reader_path(4)), str(source), str(self.receipt)],
            input=ar.encode(request) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(process.stdout)["result"], expected)
        self.assertEqual(self.reader.coverage()["served"], [0])


class ServedJudgments(unittest.TestCase):
    def setUp(self):
        historical.WorkerBoundJudgments.setUp(self)
        verified = patch.object(clients, "selected_protocol", return_value=4)
        verified.start()
        self.addCleanup(verified.stop)

    def replace_answer(self, value):
        historical.WorkerBoundJudgments.replace_answer(self, value)
        receipt = ar.Reader(self.batch, self.original / "read-receipt.json").coverage()
        (self.original / "coverage-launch.json").write_text(sa.serialize({"before": receipt, "provider": "codex"}))

    def evidence(self, *, verdict="unfinished", findings=None, original=None):
        a = original or self.original
        a.mkdir(exist_ok=True)
        (a / "input.json").write_text(sa.serialize(self.batch))
        r = ar.Reader(self.batch, a / "read-receipt.json")
        r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": 0}})
        home = a / "home"
        prompt = sa.inspection_prompt(self.batch, True, protocol=4)
        execution = {
            **sa.inspection_profile(4),
            "argv": sa.codex_launch(home, True, receipt=a / "read-receipt.json", protocol=4),
            "environment": {"HOME": str(home), "CODEX_SQLITE_HOME": str(sa.goal_state(a, self.batch))},
            "paged": True,
            "protocol_version": 4,
            "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
            "batch_identity": {
                "input_digest": self.batch["input_digest"],
                "commits": [c["sha"] for c in self.batch["commits"]],
            },
        }
        (a / "execution.json").write_text(sa.serialize(execution))
        (a / "prompt.txt").write_text(prompt)
        # replace_answer expects an existing native final.
        (a / "model-rollout-0.jsonl").write_text(
            sa.serialize({"type": "session_meta", "payload": {"id": self.thread}}) + "\n"
        )
        historical.append_native(a / "home", {"verdict": verdict, "findings": findings or []}, self.thread)
        (a / "model-rollout-0.jsonl").write_bytes(next((home / ".codex/sessions").glob("*.jsonl")).read_bytes())
        self.replace_answer({"verdict": verdict, "findings": findings or []})

    def all_served(self, attempt=None):
        attempt = attempt or self.original
        r = ar.Reader(self.batch, attempt / "read-receipt.json")
        for page in range(len(r.pages)):
            r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})

        native = attempt / "model-rollout-0.jsonl"
        rows = native.read_text().splitlines()
        rows[-1:-1] = list(map(sa.serialize, native_read_rows(range(len(r.pages)), self.batch)))
        native.write_text("\n".join(rows) + "\n")
        transcript = attempt / "model-transcript.jsonl"
        transcript.write_text(
            "\n".join(map(sa.serialize, read_events("codex", range(len(r.pages))))) + "\n" + transcript.read_text()
        )

    def test_prompt_schema_and_launcher_contain_no_copied_identifiers(self):
        prompt = sa.inspection_prompt(self.batch, True, protocol=4)
        for forbidden in ("read_token", "next_token", "reader_token", "latest reader token", "final token"):
            self.assertNotIn(forbidden, prompt)
            self.assertNotIn(forbidden, sa.serialize(sa.inspection_catalog(4)))
        args = json.loads(
            next(
                v.split("=", 1)[1]
                for v in sa.codex_launch(self.original, True, protocol=4)
                if v.startswith("mcp_servers.audit.args=")
            )
        )
        self.assertEqual(len(args), 4)
        tool = ar.Reader(self.batch, self.original / "new.json").request("tools/list", {})["tools"][0]
        self.assertEqual(tool["inputSchema"]["required"], ["page"])

    def test_premature_pass_and_missing_receipt_cannot_approve(self):
        verdict = sa.bind_verdict({"verdict": "pass", "findings": []}, self.batch)
        with self.assertRaisesRegex(Die, "not served"):
            sa.validate_reads(verdict, self.batch, self.original / "read-receipt.json", True, protocol=4)
        with self.assertRaisesRegex(Die, "not served"):
            sa.validate_reads(verdict, self.batch, self.original / "absent.json", True, protocol=4)
        self.replace_answer({"verdict": "pass", "findings": []})
        a, batch, execution, _ = recovery.retained(self.work, self.block)
        self.assertIsNone(recovery.completed_pass(a, batch, execution))
        self.assertTrue(
            progress.continuable(
                (a / "model-transcript.jsonl").read_text(), batch, a / "read-receipt.json", a, protocol=4
            )
        )
        self.all_served()
        self.assertEqual(recovery.completed_pass(a, batch, execution), verdict)

    def test_findings_and_uncertainty_before_compaction_still_block_later_pass(self):
        for kind in ("finding", "incomplete"):
            self.evidence(verdict=kind, findings=["Possible IO"])
            path = self.original / "model-rollout-0.jsonl"
            old = path.read_bytes()
            self.replace_answer({"verdict": "pass", "findings": []})
            rows = path.read_text().splitlines()
            path.write_text(old.decode() + sa.serialize({"type": "compacted", "payload": {}}) + "\n" + rows[-1] + "\n")
            with patch.object(sa, "inspect") as model:
                self.assertFalse(recovery.recover(self.cfg, self.opts, self.block))
            model.assert_not_called()

    def test_retained_protocol_and_reader_cannot_be_changed(self):
        p = self.original / "execution.json"
        original = json.loads(p.read_text())
        for change in ({"protocol_version": 3}, {"reader_digest": "wrong"}, {"batch_identity": {}}):
            p.write_text(sa.serialize({**original, **change}))
            with self.subTest(change=change), self.assertRaises(Die):
                recovery.retained(self.work, self.block)
        p.write_text(sa.serialize(original))
        with self.assertRaisesRegex(Die, "cannot change"):
            sa.inspect(self.batch, "codex", self.work / "attempt-next", resume_from=self.original, protocol=3)

    def test_unverified_client_cannot_continue_protocol_four_or_repair_tokens(self):
        with self.assertRaisesRegex(Die, "unavailable in protocol 4"):
            sa.inspect(self.batch, "codex", self.work, protocol=4, repair_output=True)
        with patch.object(clients, "selected_protocol", return_value=3), patch.object(sa, "run_process") as model:
            with self.assertRaisesRegex(NoProgress, "delivery verification"):
                sa.inspect(self.batch, "codex", self.work, resume_from=self.original)
        model.assert_not_called()

    def test_missing_verification_does_not_reserve_quota_or_a_recovery_attempt(self):
        ledger = (self.work / "ledger.jsonl").read_bytes()
        with (
            patch.object(clients, "selected_protocol", return_value=3),
            patch("tauceti_worker.loop.resolve_work_model") as quota,
            patch.object(sa, "inspect") as model,
            self.assertRaisesRegex(NoProgress, "awaiting client"),
        ):
            recovery.recover(self.cfg, self.opts, self.block)
        quota.assert_not_called()
        model.assert_not_called()
        self.assertFalse((self.work / "operational-recovery.json").exists())
        self.assertEqual((self.work / "ledger.jsonl").read_bytes(), ledger)

    def test_crash_after_pipe_write_rolls_back_new_coverage_without_losing_findings(self):
        before = ar.Reader(self.batch, self.original / "read-receipt.json").coverage()
        self.all_served()
        self.replace_answer({"verdict": "pass", "findings": []})
        # The client died before its new tool result reached a completed turn.
        (self.original / "coverage-launch.json").write_text(
            sa.serialize(
                {
                    "before": before,
                    "provider": "codex",
                }
            )
        )
        self.assertIsNone(recovery.completed_pass(self.original, self.batch, {"protocol_version": 4, "paged": True}))
        self.assertEqual(ar.Reader(self.batch, self.original / "read-receipt.json").coverage(), before)
        prompt = progress.snapshot(self.batch, self.original / "read-receipt.json", self.original, protocol=4)
        self.assertIn("next_unserved_page=1", prompt)
        self.assertNotIn("All chunks were served", prompt)
        # Source judgments are retained even though coverage rolls back.
        self.replace_answer({"verdict": "finding", "findings": ["Possible IO before interruption"]})
        with patch.object(sa, "inspect") as model:
            self.assertFalse(recovery.recover(self.cfg, self.opts, self.block))
        model.assert_not_called()

    def test_interrupted_or_failed_client_reads_cannot_credit_fresh_chunks(self):
        for kind in ("timeout", "nonzero", "failed-tool"):
            attempt = self.work / ("attempt-" + kind)
            attempt.mkdir()
            home = attempt / "isolated"
            home.mkdir()
            receipt = attempt / "read-receipt.json"

            def model(argv, *, kind=kind, receipt=receipt, home=home, **kwargs):
                r = ar.Reader(self.batch, receipt)
                for page in range(len(r.pages)):
                    r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
                value = {"verdict": "pass", "findings": []}
                historical.append_native(home, value, self.thread)
                if kind == "timeout":
                    raise subprocess.TimeoutExpired("codex", 1800, output=b"")
                events = [
                    {"type": "thread.started", "thread_id": self.thread},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(value)}},
                    {"type": "turn.completed"},
                ]
                return SimpleNamespace(
                    returncode=0 if kind == "failed-tool" else 1,
                    stdout="\n".join(
                        map(sa.serialize, read_events("codex", range(len(r.pages)), failed=True) + events)
                    ),
                    stderr="client failed",
                )

            with patch.object(sa, "run_process", side_effect=model), patch.object(sa.time, "sleep"):
                kwargs = dict(
                    home=home,
                    env={"CODEX_SQLITE_HOME": str(home)},
                    work=attempt,
                    receipt=receipt,
                    provider="codex",
                    paged=True,
                    batch=self.batch,
                    protocol=4,
                )
                if kind == "timeout":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        sa.run_client(["codex", "exec", "-"], "", **kwargs)
                else:
                    self.assertEqual(
                        sa.run_client(["codex", "exec", "-"], "", **kwargs).returncode,
                        0 if kind == "failed-tool" else 1,
                    )
            self.assertEqual(sa.reconcile_coverage(attempt, self.batch)["served"], [])
            self.assertEqual(ar.Reader(self.batch, receipt).coverage()["served"], [])
            self.assertIsNone(recovery.completed_pass(attempt, self.batch, {"protocol_version": 4, "paged": True}))

    def test_timeout_does_not_credit_saved_error_for_unsaved_success(self):
        attempt = self.work / "attempt-last-read"
        attempt.mkdir()
        home = attempt / "isolated"
        home.mkdir()
        receipt = attempt / "read-receipt.json"

        def model(argv, **kwargs):
            r = ar.Reader(self.batch, receipt)
            for page in (0, 1):
                r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            append_native_reads(home, [0], self.thread, self.batch)
            # The earlier skip-ahead error is saved, but the later successful
            # page-1 result exists only in stdout when the client times out.
            failed_rows = native_read_rows([1], self.batch)
            failed_rows[-1]["payload"]["output"] = [{"type": "input_text", "text": "Error: skipped page"}]
            with next((home / ".codex/sessions").glob("*.jsonl")).open("a") as native:
                native.write("\n".join(map(sa.serialize, failed_rows)) + "\n")
            events = read_events("codex", [0]) + read_events("codex", [1])
            raise subprocess.TimeoutExpired("codex", 1800, output="\n".join(map(sa.serialize, events)).encode())

        with patch.object(sa, "run_process", side_effect=model), self.assertRaises(subprocess.TimeoutExpired):
            sa.run_client(
                ["codex", "exec", "-"],
                "",
                home=home,
                env={},
                work=attempt,
                receipt=receipt,
                provider="codex",
                paged=True,
                batch=self.batch,
                protocol=4,
            )
        self.assertEqual(ar.Reader(self.batch, receipt).coverage()["served"], [0])
        prompt = progress.snapshot(self.batch, receipt, attempt, protocol=4)
        self.assertIn("next_unserved_page=1", prompt)

    def test_two_timeouts_preserve_cumulative_successful_reading(self):
        attempt = self.work / "attempt-timeouts"
        attempt.mkdir()
        home = attempt / "isolated"
        home.mkdir()
        receipt = attempt / "read-receipt.json"
        rounds = 0

        def model(argv, **kwargs):
            nonlocal rounds
            r = ar.Reader(self.batch, receipt)
            start = len(r.coverage()["served"])
            self.assertEqual(start, rounds * 2)
            pages = range(start, start + 2)
            for page in pages:
                r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            rounds += 1
            append_native_reads(home, pages, self.thread, self.batch)
            historical.append_native(home, {"verdict": "unfinished", "findings": []}, self.thread)
            # Preserve complete tool success reports even if the final event is
            # cut off when the subprocess times out.
            transcript = "\n".join(map(sa.serialize, read_events("codex", pages))) + '\n{"type":'
            raise subprocess.TimeoutExpired("codex", 1800, output=transcript.encode())

        with patch.object(sa, "run_process", side_effect=model):
            for expected in (2, 4):
                with self.assertRaises(subprocess.TimeoutExpired):
                    sa.run_client(
                        ["codex", "exec", "-"],
                        "",
                        home=home,
                        env={},
                        work=attempt,
                        receipt=receipt,
                        provider="codex",
                        paged=True,
                        batch=self.batch,
                        protocol=4,
                    )
                self.assertEqual(ar.Reader(self.batch, receipt).coverage()["served"], list(range(expected)))
                self.assertIsNone(recovery.completed_pass(attempt, self.batch, {"protocol_version": 4, "paged": True}))

    def test_automatic_recovery_accumulates_progress_across_timeouts(self):
        (self.work / "auth.json").write_text('{"tokens":{"access_token":"fixture","refresh_token":"fixture"}}')
        calls = 0

        def model(argv, **kwargs):
            nonlocal calls
            self.assertEqual(argv[-3:], ["resume", self.thread, "-"])
            args = json.loads(next(v.split("=", 1)[1] for v in argv if v.startswith("mcp_servers.audit.args=")))
            receipt = Path(args[3])
            r = ar.Reader(self.batch, receipt)
            start = len(r.coverage()["served"])
            self.assertEqual(start, 1 + calls)
            for page in range(start, start + 1):
                r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            calls += 1
            append_native_reads(kwargs["cwd"], range(start, start + 1), self.thread, self.batch)
            historical.append_native(kwargs["cwd"], {"verdict": "unfinished", "findings": []}, self.thread)
            events = [{"type": "thread.started", "thread_id": self.thread}] + read_events(
                "codex", range(start, start + 1)
            )
            raise subprocess.TimeoutExpired(
                "codex", 1800, output=("\n".join(map(sa.serialize, events)) + '\n{"type":').encode()
            )

        previous = self.block
        with (
            patch.object(sa, "codex_dir", return_value=self.work),
            patch.object(sa, "run_process", side_effect=model),
            patch.object(sa, "advance") as advance,
        ):
            for now, count in ((1000, 2), (10000, 3)):
                with patch.object(recovery.time, "time", return_value=now), self.assertRaises(NoProgress) as stopped:
                    recovery.recover(self.cfg, self.opts, previous)
                previous = json.loads((self.work / "blocked.json").read_text())
                receipt = Path(previous["attempt"]) / "read-receipt.json"
                self.assertEqual(
                    ar.Reader(self.batch, receipt).coverage()["served"], list(range(count)), str(stopped.exception)
                )
            advance.assert_not_called()
        self.assertEqual(calls, 2)

    def test_partial_tool_failure_keeps_only_the_successful_contiguous_prefix(self):
        before = ar.Reader(self.batch, self.original / "read-receipt.json").coverage()
        self.all_served()
        (self.original / "coverage-launch.json").write_text(sa.serialize({"before": before, "provider": "codex"}))
        events = read_events("codex", [1, 3]) + read_events("codex", [2], failed=True)
        (self.original / "model-transcript.jsonl").write_text("\n".join(map(sa.serialize, events)))
        self.assertEqual(sa.reconcile_coverage(self.original, self.batch)["served"], [0, 1])
        self.assertEqual(sa.reconcile_coverage(self.original, self.batch)["served"], [0, 1])

    def test_saved_read_requires_the_matching_native_tool_output(self):
        path = self.work / "native-read.jsonl"
        rows = native_read_rows([0], self.batch)
        path.write_text(sa.serialize(rows[0]) + "\n")
        self.assertEqual(sa.saved_read_pages([path], ar.pages(self.batch)), set())
        path.write_text("\n".join(map(sa.serialize, rows)) + '\ninvalid\n42\n{"type":')
        self.assertEqual(sa.saved_read_pages([path], ar.pages(self.batch)), {0})
        original_output = rows[-1]["payload"]["output"]
        rows[-1]["payload"]["output"] = [{"type": "input_text", "text": sa.serialize({"page": 0, "sections": []})}]
        path.write_text("\n".join(map(sa.serialize, rows)))
        self.assertEqual(sa.saved_read_pages([path], ar.pages(self.batch)), set())
        rows[-1]["payload"]["output"] = original_output
        # A saved error for an earlier request of the same page is not source.
        successful_output = rows[-1]["payload"]["output"]
        rows[-1]["payload"]["output"] = [{"type": "input_text", "text": "Error: skipped page"}]
        path.write_text("\n".join(map(sa.serialize, rows)))
        self.assertEqual(sa.saved_read_pages([path], ar.pages(self.batch)), set())
        rows[-1]["payload"]["output"] = successful_output
        # A function output belonging to a different call cannot vouch for this read.
        rows[-1]["payload"]["call_id"] = "other-call"
        path.write_text("\n".join(map(sa.serialize, rows)))
        self.assertEqual(sa.saved_read_pages([path], ar.pages(self.batch)), set())

    def test_client_reports_must_confirm_read_success(self):
        for provider in ("codex", "claude"):
            for failed in (False, True):
                transcript = "\n".join(map(sa.serialize, read_events(provider, [0, 1], failed=failed)))
                self.assertEqual(sa.successful_read_pages(transcript, provider), set() if failed else {0, 1})
        # Both completion paths accept blank lines and an omitted legacy subtype.
        transcript = '\n{"type":"result","structured_output":{"verdict":"pass","findings":[]}}\n\n'
        self.assertEqual(sa.claude_result(transcript)["structured_output"]["verdict"], "pass")

    def test_wrong_native_conversation_and_extra_final_fields_block_approval(self):
        self.all_served()
        a, batch, execution, _ = recovery.retained(self.work, self.block)
        self.replace_answer({"verdict": "pass", "findings": [], "read_token": "obsolete"})
        with self.assertRaises(Die):
            recovery.retained(self.work, self.block)
        self.replace_answer({"verdict": "pass", "findings": []})
        p = a / "model-transcript.jsonl"
        p.write_text(p.read_text().replace(self.thread, "22222222-2222-4333-8444-555555555555"))
        self.assertIsNone(recovery.completed_pass(a, batch, execution))

    def test_ordinary_recovery_continues_same_protocol_with_no_output_repair(self):
        with (
            patch.object(sa, "inspect", side_effect=OSError("service unavailable")) as model,
            self.assertRaises(NoProgress),
        ):
            recovery.recover(self.cfg, self.opts, self.block)
        self.assertEqual(model.call_args.kwargs["protocol"], 4)
        self.assertEqual(model.call_args.kwargs["resume_from"], self.original)
        self.assertTrue(model.call_args.kwargs["continue_reading"])
        self.assertFalse(model.call_args.kwargs["repair_output"])

    def test_both_inspectors_accept_only_verdict_and_findings_with_complete_served_coverage(self):
        (self.work / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
        (self.work / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"fixture"}}')
        value = {"verdict": "pass", "findings": []}
        for provider in ("codex", "claude"):
            attempt = self.work / ("attempt-" + provider)
            attempt.mkdir()

            def model(argv, *, provider=provider, **kwargs):
                if provider == "codex":
                    schema = json.loads((kwargs["cwd"] / "verdict-schema.json").read_text())
                    args = json.loads(next(v.split("=", 1)[1] for v in argv if v.startswith("mcp_servers.audit.args=")))
                else:
                    schema = json.loads(argv[argv.index("--json-schema") + 1])
                    config = json.loads(argv[argv.index("--mcp-config") + 1])
                    args = config["mcpServers"]["audit"]["args"]
                self.assertEqual(set(schema["properties"]), {"verdict", "findings"})
                self.assertEqual(set(schema["required"]), {"verdict", "findings"})
                self.assertEqual(len(args), 4)
                r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
                for page in range(len(r.pages)):
                    r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
                if provider == "codex":
                    append_native_reads(kwargs["cwd"], range(len(r.pages)), self.thread, self.batch)
                    historical.append_native(kwargs["cwd"], value, self.thread)
                    events = [
                        {"type": "thread.started", "thread_id": self.thread},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(value)}},
                        {"type": "turn.completed"},
                    ]
                else:
                    events = [{"type": "result", "subtype": "success", "structured_output": value}]
                events = read_events(provider, range(len(r.pages))) + events
                return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

            with (
                patch.object(sa, "codex_dir", return_value=self.work),
                patch.object(sa, "claude_dir", return_value=self.work),
                patch.object(sa, "run_process", side_effect=model),
            ):
                self.assertEqual(sa.inspect(self.batch, provider, attempt), sa.bind_verdict(value, self.batch))
            self.assertEqual(sa.attempt_protocol(attempt), 4)

    def test_three_turns_then_restart_preserve_original_inspection_and_judgment(self):
        (self.work / "auth.json").write_text('{"tokens":{"access_token":"fixture","refresh_token":"fixture"}}')
        calls = 0

        def model(argv, **kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(argv[-3:], ["resume", self.thread, "-"])
            self.assertNotIn("read_token", kwargs["input"])
            args = json.loads(next(v.split("=", 1)[1] for v in argv if v.startswith("mcp_servers.audit.args=")))
            self.assertEqual(len(args), 4)
            receipt = Path(args[3])
            r = ar.Reader(self.batch, receipt)
            r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": 0}})
            if calls == 4:
                for page in range(len(r.pages)):
                    r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            value = {"verdict": "pass" if calls == 4 else "unfinished", "findings": []}
            append_native_reads(kwargs["cwd"], range(len(r.pages)) if calls == 4 else [0], self.thread, self.batch)
            historical.append_native(kwargs["cwd"], value, self.thread)
            events = [
                {"type": "thread.started", "thread_id": self.thread},
                {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(value)}},
                {"type": "turn.completed"},
            ]
            events = read_events("codex", range(len(r.pages)) if calls == 4 else [0]) + events
            return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

        with (
            patch.object(sa, "codex_dir", return_value=self.work),
            patch.object(sa, "run_process", side_effect=model),
            patch.object(sa.time, "sleep"),
            patch.object(sa, "advance", return_value="b" * 40) as advance,
        ):
            with patch.object(recovery.time, "time", return_value=1000), self.assertRaises(NoProgress):
                recovery.recover(self.cfg, self.opts, self.block)
            self.assertEqual(calls, 3)
            advance.assert_not_called()
            previous = json.loads((self.work / "blocked.json").read_text())
            with patch.object(recovery.time, "time", return_value=1301):
                self.assertTrue(recovery.recover(self.cfg, self.opts, previous))
            self.assertEqual(calls, 4)
            advance.assert_called_once()
        self.assertFalse((self.work / "blocked.json").exists())


class FreshRun(unittest.TestCase):
    setUp = historical.Run.setUp

    def test_verified_client_selects_protocol_four_and_records_matching_policy(self):
        with (
            patch.object(clients, "selected_protocol", return_value=4),
            patch.object(sa, "inspect", return_value={"verdict": "pass", "findings": []}) as model,
            patch.object(sa, "advance", return_value=self.target),
        ):
            self.assertEqual(sa.run(SimpleNamespace(state=self.state), self.opts), 0)
        self.assertEqual(model.call_args.kwargs, {"protocol": 4})
        rows = list(map(json.loads, (self.state / sa.CURSOR / "ledger.jsonl").read_text().splitlines()))
        row = next(r for r in rows if "verdict" in r)
        self.assertEqual(row["protocol_version"], 4)
        self.assertEqual(row["reader_digest"], sa.inspection_profile(4)["reader_digest"])
        self.assertEqual(row["prompt_digest"], hashlib.sha256(sa.SERVED_MODEL_PROMPT.encode()).hexdigest())

    def test_fresh_claude_ledger_identifies_the_executed_prompt(self):
        self.opts.work_model = "claude"
        with (
            patch.object(clients, "selected_protocol", return_value=4),
            patch.object(sa, "inspect", return_value={"verdict": "pass", "findings": []}),
            patch.object(sa, "advance", return_value=self.target),
        ):
            self.assertEqual(sa.run(SimpleNamespace(state=self.state), self.opts), 0)
        rows = list(map(json.loads, (self.state / sa.CURSOR / "ledger.jsonl").read_text().splitlines()))
        row = next(r for r in rows if "verdict" in r)
        self.assertEqual(row["prompt_digest"], hashlib.sha256(sa.SERVED_CLAUDE_PROMPT.encode()).hexdigest())

    def test_protocol_selection_is_passed_once_even_if_verification_changes(self):
        with (
            patch.object(clients, "selected_protocol", side_effect=[3, 4]) as selected,
            patch.object(sa, "inspect", return_value={"verdict": "pass", "findings": []}) as model,
            patch.object(sa, "advance", return_value=self.target),
        ):
            self.assertEqual(sa.run(SimpleNamespace(state=self.state), self.opts), 0)
        selected.assert_called_once()
        self.assertEqual(model.call_args.kwargs, {"protocol": 3})


class ClientCompatibility(unittest.TestCase):
    def test_historical_reader_bytes_are_preserved(self):
        self.assertEqual(
            sa.inspection_profile(3)["reader_digest"],
            "e11662a02ec797222b9626f34fcccd70fe98f454240b1fd1435dcc6ef3d65c36",
        )

    def test_unrecognized_wrappers_do_not_claim_verified_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapper"
            path.write_text("#!/bin/sh\nexec hidden-client --version\n")
            with (
                patch.object(clients, "client_path", return_value=path),
                self.assertRaisesRegex(ValueError, "native client"),
            ):
                clients.fingerprint("claude")

    def test_missing_stale_and_changed_clients_use_token_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            expected = {"client": "one"}
            with patch.object(clients, "fingerprint", return_value=expected):
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), 3)
                p = state / clients.RECORD
                p.write_text(sa.serialize({"codex": {"protocol_version": 4, "fingerprint": expected, "intact": True}}))
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), 4)
                with patch.object(clients, "fingerprint", return_value={"client": "two"}):
                    self.assertEqual(clients.selected_protocol("codex", state, verify=False), 3)
                self.assertEqual(clients.selected_protocol("claude", state, verify=False), 3)
                p.chmod(0o666)
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), 3)

    def test_missing_and_changed_verification_refresh_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            current = {"client": "one"}

            def verify(provider, state):
                (state / clients.RECORD).write_text(
                    sa.serialize({provider: {"protocol_version": 4, "fingerprint": dict(current), "intact": True}})
                )

            with (
                patch.object(clients, "fingerprint", side_effect=lambda _: dict(current)),
                patch.object(check, "verify", side_effect=verify) as probe,
            ):
                self.assertEqual(clients.selected_protocol("codex", state), 4)
                self.assertEqual(clients.selected_protocol("codex", state), 4)
                self.assertEqual(probe.call_count, 1)
                current["client"] = "two"
                self.assertEqual(clients.selected_protocol("codex", state), 4)
                self.assertEqual(probe.call_count, 2)

    def test_concurrent_selection_runs_only_one_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            expected = {"client": "one"}

            def verify(provider, state):
                from tauceti_worker.audit_alerts import save

                save(
                    state / clients.RECORD, {provider: {"protocol_version": 4, "fingerprint": expected, "intact": True}}
                )

            with (
                patch.object(clients, "fingerprint", return_value=expected),
                patch.object(check, "verify", side_effect=verify) as probe,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    self.assertEqual(
                        list(pool.map(lambda _: clients.selected_protocol("codex", state), range(8))), [4] * 8
                    )
                probe.assert_called_once()

    def test_failed_probe_is_cached_retried_and_invalidated_by_client_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with (
                patch.object(clients, "fingerprint", return_value={"client": "one"}) as fingerprint,
                patch.object(check, "verify", side_effect=RuntimeError("unexpected client format")) as probe,
                patch.object(clients.time, "time", return_value=1000) as now,
            ):
                self.assertEqual(clients.selected_protocol("codex", state), 3)
                self.assertEqual(clients.selected_protocol("codex", state), 3)
                self.assertEqual(probe.call_count, 1)
                now.return_value = 1301
                self.assertEqual(clients.selected_protocol("codex", state), 3)
                self.assertEqual(probe.call_count, 2)
                fingerprint.return_value = {"client": "two"}
                self.assertEqual(clients.selected_protocol("codex", state), 3)
                self.assertEqual(probe.call_count, 3)

    def test_capture_rejects_missing_or_truncated_middle_even_if_tail_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = check.Capture(check.test_batch(), Path(tmp))
            text = capture.expected[0]
            for output in (text[: len(text) // 2], text[:100] + text[-100:], "not the source"):
                with self.subTest(output=output[:20]), self.assertRaises(ValueError):
                    capture.consume(
                        {
                            "input": [{"type": "function_call_output", "call_id": "audit_page_0", "output": output}],
                        },
                        "codex",
                    )
            self.assertEqual(capture.received, set())
            capture.consume(
                {
                    "input": [{"type": "function_call_output", "call_id": "audit_page_0", "output": text}],
                },
                "codex",
            )
            self.assertEqual(capture.received, {0})


if __name__ == "__main__":
    unittest.main()
