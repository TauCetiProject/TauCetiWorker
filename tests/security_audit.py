#!/usr/bin/env python3
"""Cursor invariants, complete history coverage, authorization, and reviewer isolation."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import audit_alerts as alerts
from tauceti_worker import audit_clients as clients
from tauceti_worker import cli
from tauceti_worker import security_audit as sa
from tauceti_worker import work_units as wu
from tauceti_worker.config import Die, NoProgress
from tauceti_worker.work_units import want


def append_native(home, answer, thread="11111111-2222-4333-8444-555555555555"):
    sessions = home / ".codex/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    files = list(sessions.rglob("*.jsonl"))
    if not files:
        path = sessions / "rollout.jsonl"
        path.write_text(sa.serialize({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    else:
        path = files[0]
    with path.open("a") as native:
        native.write(
            sa.serialize(
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": sa.serialize(answer)}],
                    },
                }
            )
            + "\n"
        )


CAPACITY_RETRY = {
    "type": "error",
    "message": "Retrying model request in 300 seconds... (Selected model is at capacity. Please try a different model.)",
}


class RetryEvents(unittest.TestCase):
    def final(self, events):
        return sa.codex_final("\n".join(map(sa.serialize, events)), True)

    def test_server_retry_notices_followed_by_a_completed_answer_are_progress(self):
        answer = {"verdict": "pass", "findings": [], "read_token": "token"}
        for message in (
            CAPACITY_RETRY["message"],
            "Retrying model request in 600 seconds... (An error occurred while processing your request. Request ID abc)",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    self.final(
                        [
                            {"type": "turn.started"},
                            {"type": "error", "message": message},
                            CAPACITY_RETRY,
                            {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(answer)}},
                            {"type": "turn.completed"},
                        ]
                    ),
                    answer,
                )

    def test_terminal_unknown_unfinished_and_out_of_order_errors_never_pass(self):
        answer = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"verdict":"pass"}'}}
        completed = {"type": "turn.completed"}
        for events in (
            [CAPACITY_RETRY, answer],
            [answer, completed, CAPACITY_RETRY],
            [answer, CAPACITY_RETRY, completed],
            [CAPACITY_RETRY, completed, answer],
            [answer, completed, {"type": "turn.started"}, completed],
            [{"type": "error", "message": "unknown error"}, answer, completed],
            [{"type": "turn.failed", "error": {"message": CAPACITY_RETRY["message"]}}, answer, completed],
            [{**CAPACITY_RETRY, "unknown": True}, answer, completed],
            [
                {"type": "error", "message": "Retrying model request in 300 seconds... (unknown failure)"},
                answer,
                completed,
            ],
            [CAPACITY_RETRY, {"type": "item.completed", "item": {"type": "command_execution"}}, answer, completed],
        ):
            with self.subTest(events=events), self.assertRaises(Die):
                self.final(events)


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

    def test_large_commit_is_complete_and_isolated_from_neighbors(self):
        content = "-- mathematical source\n" * 50000
        for i in range(5):
            self.commit(str(i), f"TauCeti/Large{i}.lean", content)
        baseline = self.command("rev-parse", "HEAD")
        for i in range(5):
            (self.root / f"TauCeti/Large{i}.lean").write_text(content + "def x := 1\n")
        self.command("add", ".")
        self.command("commit", "-qm", "wide ordinary change")
        wide = self.command("rev-parse", "HEAD")
        head = self.commit("next", "TauCeti/A.lean", "def a := 1\n")
        batch = sa.plan_batch(self.repo, baseline, head)
        self.assertGreater(len(sa.serialize(batch).encode()), 8 * 1024 * 1024)
        self.assertEqual([c["sha"] for c in batch["commits"]], [wide])
        self.assertEqual(len(batch["commits"][0]["files"]), 5)
        for f in batch["commits"][0]["files"]:
            self.assertEqual(f["before"], content)
            self.assertEqual(f["after"], content + "def x := 1\n")

    def test_batch_ends_before_oversized_commit(self):
        small = self.commit("small", "TauCeti/A.lean", "def x := 1\n")
        head = self.commit("big", "TauCeti/A.lean", "x" * sa.MAX_BYTES)
        batch = sa.plan_batch(self.repo, self.base, head)
        self.assertEqual([c["sha"] for c in batch["commits"]], [small])

    def test_single_large_commit_gets_one_complete_compacting_session(self):
        content = "-- Ordinary mathematical source\n" * 45000
        head = self.commit("large", "TauCeti/A.lean", content)
        batch = sa.plan_batch(self.repo, self.base, head)
        self.assertGreater(len(sa.serialize(batch).encode()), sa.MAX_BYTES)
        self.assertEqual([c["sha"] for c in batch["commits"]], [head])
        self.assertEqual(batch["commits"][0]["files"][0]["after"], content)

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


class MaintenanceScheduling(unittest.TestCase):
    def setUp(self):
        self.opts = wu.RoundOpts(
            only=["bump", "progress", "security-audit"],
            agent="claude",
            work_model="claude",
            sandbox_host=True,
            dry_run=False,
        )
        values = {}
        self.worker = SimpleNamespace(
            cfg=SimpleNamespace(),
            counters=SimpleNamespace(read=lambda name: values.get(name, 0), write=values.__setitem__),
        )

    def test_below_threshold_reaches_maintenance_without_changing_options(self):
        with (
            patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_MIN_COMMITS": "20"}),
            patch.object(sa, "run", side_effect=sa.AuditNotDue("waiting")) as audit,
            patch.object(wu, "mirror_creds", side_effect=RuntimeError("maintenance reached")),
            self.assertRaisesRegex(RuntimeError, "maintenance reached"),
        ):
            wu.run_round(self.worker, self.opts)
        self.assertEqual(audit.call_args.kwargs, {"min_commits": 20})
        self.assertEqual(audit.call_args.args[1].only, ["security-audit"])
        self.assertEqual(self.opts.only, ["bump", "progress", "security-audit"])

    def test_successful_audit_does_not_run_maintenance_in_same_round(self):
        with patch.object(sa, "run", return_value=0), patch.object(wu, "mirror_creds") as maintenance:
            self.assertEqual(wu.run_round(self.worker, self.opts), 0)
        maintenance.assert_not_called()

    def test_watchdog_runs_on_audit_and_maintenance_turns_and_failures(self):
        with (
            patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_MIN_COMMITS": "20"}),
            patch.object(sa, "watch") as watch,
            patch.object(sa, "run", side_effect=[0, Die("finding")]),
            patch.object(wu, "mirror_creds", side_effect=RuntimeError("maintenance reached")),
        ):
            self.assertEqual(wu.run_round(self.worker, self.opts), 0)
            for _ in range(2):  # The maintenance turn, then a failed audit falling through.
                with self.assertRaisesRegex(RuntimeError, "maintenance reached"):
                    wu.run_round(self.worker, self.opts)
        self.assertEqual(watch.call_count, 3)
        self.assertEqual(watch.call_args.kwargs, {"min_commits": 20})

    def test_audit_and_maintenance_take_turns(self):
        with (
            patch.object(sa, "run", return_value=0) as audit,
            patch.object(wu, "mirror_creds", side_effect=RuntimeError("maintenance reached")),
        ):
            self.assertEqual(wu.run_round(self.worker, self.opts), 0)
            with self.assertRaisesRegex(RuntimeError, "maintenance reached"):
                wu.run_round(self.worker, self.opts)
            self.assertEqual(wu.run_round(self.worker, self.opts), 0)
        self.assertEqual(audit.call_count, 2)

    def test_failed_audit_keeps_its_next_turn(self):
        with (
            patch.object(sa, "run", side_effect=FileExistsError("audit state")) as audit,
            patch.object(wu, "mirror_creds", side_effect=RuntimeError("maintenance reached")),
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "maintenance reached"):
                    wu.run_round(self.worker, self.opts)
        self.assertEqual(audit.call_count, 2)

    def test_audit_blocks_and_service_failures_allow_maintenance(self):
        for result in (sa.NoProgress("service unavailable"), sa.Die("finding"), FileExistsError("audit state")):
            with (
                self.subTest(error=str(result)),
                patch.object(sa, "run", side_effect=result),
                patch.object(wu, "mirror_creds", side_effect=RuntimeError("maintenance reached")),
                self.assertRaisesRegex(RuntimeError, "maintenance reached"),
            ):
                wu.run_round(self.worker, self.opts)

    def test_audit_only_failures_still_stop(self):
        self.opts.only = ["security-audit"]
        with patch.object(sa, "run", side_effect=Die("finding")), patch.object(wu, "mirror_creds") as maintenance:
            with self.assertRaisesRegex(Die, "finding"):
                wu.run_round(self.worker, self.opts)
        maintenance.assert_not_called()

    def test_mixed_round_still_preflights_maintenance_tools(self):
        with patch.object(cli, "_have", side_effect=lambda name: name != "lake"):
            with self.assertRaisesRegex(Die, "toolchain"):
                cli.preflight(self.worker.cfg, self.opts)


class PrivateAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.config = self.work / "bot.json"
        self.config.write_text(
            json.dumps({"site": "https://chat.example", "recipient_id": 123, "email": "bot", "api_key": "secret"})
        )
        env = patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.block = {"from_sha": "a" * 40, "to_sha": "b" * 40, "reason": "network unavailable"}
        timer = patch.object(alerts.time, "time", return_value=10000)
        self.clock = timer.start()
        self.addCleanup(timer.stop)
        # Delivery tests exercise a recorded source concern; operational pauses belong to the watchdog.
        sa.record(
            self.work / "ledger.jsonl",
            {
                "from_sha": self.block["from_sha"],
                "to_sha": self.block["to_sha"],
                "verdict": {"verdict": "finding", "findings": ["TauCeti/A.lean:3 reads a file"]},
            },
        )
        self.real_request = alerts.request
        self.request = patch.object(alerts, "request", return_value={"result": "success", "id": 456})
        self.http = self.request.start()
        self.addCleanup(self.request.stop)

    def test_operational_block_is_left_to_the_cursor_watchdog(self):
        (self.work / "ledger.jsonl").unlink()
        self.config.unlink()  # Nothing is loaded or sent, however long the block is held.
        for now in (10000, 10**9):
            self.clock.return_value = now
            alerts.notify_block(self.work, self.block)
        self.http.assert_not_called()
        self.assertFalse((self.work / "zulip-alerts.json").exists())

    def test_alerts_use_the_outcome_the_block_records(self):
        (self.work / "ledger.jsonl").write_text('{"from_sha": "' + "a" * 40 + '", "verdict": {"verd\n')
        unreadable = {**self.block, "reason": "security-audit: unreadable evidence: x", "outcome": "unreadable"}
        alerts.notify_block(self.work, unreadable)
        self.assertEqual(self.http.call_count, 1)  # Immediately, despite a damaged ledger row.
        sent = self.http.call_args.args[2]["content"]
        self.assertIn("Unreadable evidence", sent)
        self.assertIn("unreadable evidence: x", sent)
        finding = {**self.block, "outcome": "finding", "verdict": {"verdict": "finding", "findings": ["run_cmd"]}}
        self.assertEqual(alerts.report(self.work, finding)["details"], ["run_cmd"])
        self.assertEqual(alerts.report(self.work, {**self.block, "outcome": "not-judged"})["kind"], "Operational stall")

    def test_every_block_writer_records_its_outcome(self):
        from tauceti_worker.audit_evidence import NotJudged, Unreadable, outcome_of

        finding = {"verdict": "finding", "findings": ["IO"]}
        self.assertEqual(outcome_of(finding, Die("x")), {"outcome": "finding", "verdict": finding})
        self.assertEqual(outcome_of(None, Unreadable("x")), {"outcome": "unreadable"})
        self.assertEqual(outcome_of(None, NotJudged("x")), {"outcome": "not-judged"})
        self.assertEqual(outcome_of({"verdict": "pass", "findings": []}, Die("ref")), {"outcome": "not-judged"})

    def test_code_uncertainty_notifies_immediately_during_operational_delay(self):
        (self.work / "ledger.jsonl").unlink()
        alerts.notify_block(self.work, self.block)
        self.http.assert_not_called()
        sa.record(
            self.work / "ledger.jsonl",
            {
                "from_sha": self.block["from_sha"],
                "to_sha": self.block["to_sha"],
                "verdict": {"verdict": "incomplete", "findings": ["Cannot establish whether macro performs IO"]},
            },
        )
        alerts.notify_block(self.work, self.block)
        self.http.assert_called_once()
        message = self.http.call_args.args[2]["content"]
        self.assertIn("Inspection incomplete", message)
        self.assertIn("Human review required", message)
        self.assertNotIn("operational stall", message)

    def test_request_trusts_the_host_ca_bundle_without_ssl_cert_file(self):
        self.request.stop()
        self.addCleanup(self.request.start)
        seen = {}

        class Opener:
            def open(self, req, timeout):
                raise OSError("stop before network")

        def build_opener(*handlers):
            seen["handlers"] = handlers
            return Opener()

        context = unittest.mock.MagicMock()
        with (
            patch.dict(os.environ, {"SSL_CERT_FILE": ""}),
            patch.object(alerts, "ensure_ssl_cert_file", return_value="/host/ca-bundle.crt") as bundle,
            patch.object(alerts.ssl, "create_default_context", return_value=context) as default,
            patch.object(alerts.urllib.request, "HTTPSHandler") as https,
            patch.object(alerts.urllib.request, "build_opener", side_effect=build_opener),
        ):
            with self.assertRaisesRegex(OSError, "stop before network"):
                alerts.request(json.loads(self.config.read_text()), "POST", {"type": "direct"})
        bundle.assert_called_once()
        # The default stores (including SSL_CERT_DIR) stay loaded; the host bundle is added to them.
        default.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="/host/ca-bundle.crt")
        https.assert_called_once_with(context=context)
        self.assertIn(https.return_value, seen["handlers"])

    def test_alert_filter_never_clears_a_block_or_modifies_recovery_budget(self):
        blocked = self.work / "blocked.json"
        blocked.write_text(json.dumps(self.block))
        recovery = self.work / "operational-recovery.json"
        recovery.write_text('{"budget": "unchanged"}')
        before = blocked.read_bytes(), recovery.read_bytes()
        alerts.notify_block(self.work, self.block)
        self.assertEqual((blocked.read_bytes(), recovery.read_bytes()), before)
        self.http.assert_called_once()

    def test_private_recipient_and_durable_deduplication(self):
        alerts.notify_block(self.work, self.block)
        alerts.notify_block(self.work, self.block)
        alerts.notify_block(self.work, {**self.block, "reason": "another request failed"})
        self.http.assert_called_once()
        _, method, params = self.http.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(params["type"], "direct")
        self.assertEqual(json.loads(params["to"]), [123])
        self.assertIn("Source-safety finding", params["content"])
        self.assertIn("Human review required", params["content"])
        self.assertNotIn("secret", params["content"])

    def test_source_finding_is_reported_as_data_with_evidence(self):
        # A finding bypasses both a fresh delay and damaged operational bookkeeping.
        (self.work / "operational-alert.json").write_text("invalid operational timer")
        (self.work / "ledger.jsonl").write_text(
            json.dumps(
                {
                    "from_sha": self.block["from_sha"],
                    "to_sha": self.block["to_sha"],
                    "verdict": {"verdict": "finding", "findings": ["TauCeti/A.lean:12 IO access ``` @**everyone**"]},
                }
            )
            + "\n"
        )
        alerts.notify_block(self.work, self.block)
        message = self.http.call_args.args[2]["content"]
        self.assertIn("Source-safety finding", message)
        self.assertIn("Human review required", message)
        self.assertIn("TauCeti/A.lean:12", message)
        self.assertIn("blocked.json", message)
        self.assertNotIn("@**everyone**", message)
        self.assertEqual(message.count("```"), 2)

    def test_failed_delivery_backs_off_and_reconciles_ambiguous_post(self):
        self.http.side_effect = OSError("response lost")
        alerts.notify_block(self.work, self.block)
        marker = "tauceti-audit-" + next(iter(json.loads((self.work / "zulip-alerts.json").read_text())))
        alerts.notify_block(self.work, self.block)
        self.assertEqual(self.http.call_count, 1)
        self.http.side_effect = None
        self.http.return_value = {"result": "success", "messages": [{"id": 789, "content": marker}]}
        with patch.object(alerts.time, "time", return_value=10**12):
            alerts.notify_block(self.work, self.block)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.http.call_args.args[1], "GET")
        self.assertEqual(json.loads(self.http.call_args.args[2]["narrow"]), [{"operator": "dm", "operand": [123]}])
        self.assertEqual(
            next(iter(json.loads((self.work / "zulip-alerts.json").read_text()).values()))["message_id"], 789
        )

    def test_failed_reconciliation_does_not_resend_and_backs_off(self):
        self.http.side_effect = OSError("unavailable")
        alerts.notify_block(self.work, self.block)
        with patch.object(alerts.time, "time", return_value=10**12):
            alerts.notify_block(self.work, self.block)
            alerts.notify_block(self.work, self.block)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.http.call_args.args[1], "GET")

    def test_unconfigured_worker_sends_nothing(self):
        with patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG": ""}):
            alerts.notify_block(self.work, self.block)
        self.http.assert_not_called()

    def test_malformed_notification_state_preserves_audit_block(self):
        blocked = self.work / "blocked.json"
        blocked.write_text(json.dumps(self.block))
        for malformed in ([], "invalid-state"):
            (self.work / "zulip-alerts.json").write_text(json.dumps(malformed))
            alerts.notify_block(self.work, self.block)
            self.assertEqual(json.loads(blocked.read_text()), self.block)
        self.config.write_text("[]")
        alerts.notify_block(self.work, self.block)
        self.assertEqual(json.loads(blocked.read_text()), self.block)
        self.http.assert_not_called()

    def test_invalid_destination_is_rejected_before_network_access(self):
        config = json.loads(self.config.read_text())
        for changes in (
            {"site": "http://chat.example"},
            {"site": "https://name:password@chat.example"},
            {"site": "https://chat.example/other"},
            {"site": "https://chat.example?token=secret"},
            {"recipient_id": True},
            {"recipient_id": "123"},
            {"recipient_id": 0},
        ):
            with self.subTest(changes=changes), patch.object(alerts.urllib.request, "build_opener") as network:
                with self.assertRaises(ValueError):
                    self.real_request({**config, **changes}, "POST", {})
                network.assert_not_called()


class Authority(unittest.TestCase):
    def test_cli_carries_pacing_override_into_audit_round(self):
        for ignored in (False, True):
            with (
                self.subTest(ignore_quota=ignored),
                patch.object(sa, "require_admin"),
                patch.dict(
                    os.environ, {"TAUCETI_TEST_SLEEP": "", "TAUCETI_TEST_HOLD": "", "TAUCETI_TEST_HEARTBEAT": ""}
                ),
                patch.multiple(
                    cli,
                    Config=DEFAULT,
                    RoundContext=DEFAULT,
                    Worker=DEFAULT,
                    GitHub=DEFAULT,
                    ReviewState=DEFAULT,
                    Counters=DEFAULT,
                    Claims=DEFAULT,
                    set_log_file=DEFAULT,
                    configure_api=DEFAULT,
                    resolve_work_model=DEFAULT,
                    run_round=DEFAULT,
                ) as mocks,
            ):
                mocks["resolve_work_model"].return_value = "codex"
                mocks["run_round"].return_value = 0
                cli.cmd_work(
                    SimpleNamespace(worker_id="default", dry_run=True, ignore_quota=ignored),
                    only=["security-audit", "bump", "progress"],
                    agent="codex",
                    one_round=True,
                )
                opts = mocks["run_round"].call_args.args[1]
                self.assertIs(opts.ignore_quota, ignored)
                self.assertIs(mocks["resolve_work_model"].call_args.kwargs["ignore_quota"], ignored)

    def test_phase_is_explicit_only(self):
        self.assertFalse(want([], "security-audit"))
        self.assertNotIn("security-audit", cli.resolve_tasks([], ["review"]))
        self.assertEqual(cli.resolve_tasks(["security-audit"], []), ["security-audit"])

    def test_any_combination_still_requires_admin(self):
        for only in (
            ["security-audit"],
            ["bump", "progress", "security-audit"],
            ["review", "overlap-scan", "overlap-fix", "security-audit"],
        ):
            with patch.object(sa, "require_admin", side_effect=Die("not admin")) as admin:
                with self.assertRaisesRegex(Die, "not admin"):
                    cli.cmd_work(SimpleNamespace(), only=only, agent="claude", one_round=True)
            admin.assert_called_once()
        with patch.object(sa, "require_admin") as admin, self.assertRaisesRegex(Die, "--pr"):
            cli.cmd_work(SimpleNamespace(), only=["review", "security-audit"], agent="claude", one_round=True, prs=(1,))
        admin.assert_not_called()

    def test_concurrent_saves_of_one_state_file_never_collide(self):
        import concurrent.futures

        from tauceti_worker.audit_alerts import save

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blocked.json"

            def write(i):
                for _ in range(50):
                    save(path, {"writer": i})

            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(write, range(8)))
            self.assertIn(json.loads(path.read_text())["writer"], range(8))
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["blocked.json"])

    def test_admin_must_be_literal_admin_permission(self):
        def graphql(permission, login="admin"):
            data = {"data": {"viewer": {"login": login}, "repository": {"viewerPermission": permission}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        for value in ("WRITE", "MAINTAIN", None, "admin", True):
            with self.subTest(value=value), patch.object(sa, "execute", return_value=graphql(value)):
                with self.assertRaises(Die):
                    sa.require_admin()
        with patch.object(sa, "execute", return_value=graphql("ADMIN", "")), self.assertRaises(Die):
            sa.admin_login()
        with patch.object(sa, "execute", return_value=graphql("ADMIN")) as call:
            self.assertEqual(sa.admin_login(), "admin")
        self.assertIn("graphql", call.call_args.args[0])

    def test_permission_failure_does_not_authorize(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="unavailable")
        with patch.object(sa, "execute", return_value=failed), self.assertRaises(NoProgress):
            sa.require_admin()
        with patch.object(sa, "execute", return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")):
            with self.assertRaises(NoProgress):
                sa.require_admin()

    def test_concurrent_update_never_writes(self):
        with (
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", return_value="b" * 40),
            patch.object(sa, "push_cursor") as push,
            self.assertRaises(NoProgress),
        ):
            sa.advance(Path("unused"), {"from_sha": "a" * 40})
        push.assert_not_called()

    def test_revoked_admin_never_writes(self):
        with (
            patch.object(sa, "require_admin", side_effect=Die("revoked")),
            patch.object(sa, "push_cursor") as push,
            self.assertRaises(Die),
        ):
            sa.advance(Path("unused"), {})
        push.assert_not_called()

    def test_no_force_update_and_confirmed_sha(self):
        target, old, main = "c" * 40, "a" * 40, "b" * 40
        batch = {"from_sha": old, "commits": [{"sha": target}]}
        with (
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", side_effect=[old, main, target]),
            patch.object(sa, "refresh"),
            patch.object(sa, "validate_history") as history,
            patch.object(sa, "push_cursor") as push,
        ):
            self.assertEqual(sa.advance(Path("unused"), batch), target)
        history.assert_called_once_with(Path("unused"), old, main, target)
        push.assert_called_once_with(Path("unused"), old, target)

    def test_unconfirmed_push_does_not_report_advancement(self):
        target, old, main = "c" * 40, "a" * 40, "b" * 40
        batch = {"from_sha": old, "commits": [{"sha": target}]}
        with (
            patch.object(sa, "require_admin"),
            patch.object(sa, "remote_sha", side_effect=[old, main, old]),
            patch.object(sa, "refresh"),
            patch.object(sa, "validate_history"),
            patch.object(sa, "push_cursor"),
            self.assertRaises(NoProgress),
        ):
            sa.advance(Path("unused"), batch)

    def test_cursor_push_is_a_leased_fast_forward_with_env_token(self):
        old, target = "a" * 40, "c" * 40
        with (
            patch.dict(sa.os.environ, {"GH_TOKEN": "ghp-fixture-secret"}),
            patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run,
        ):
            sa.push_cursor(Path("repo"), old, target)
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertIn(f"--force-with-lease=refs/heads/{sa.CURSOR}:{old}", argv)
        self.assertIn(f"{target}:refs/heads/{sa.CURSOR}", argv)
        self.assertFalse(any(a.startswith("+") or a in ("--force", "-f") for a in argv))
        self.assertNotIn("ghp-fixture-secret", " ".join(argv))
        self.assertEqual(env["TAUCETI_AUDIT_PUSH_TOKEN"], "ghp-fixture-secret")
        with (
            patch.dict(sa.os.environ, {"GH_TOKEN": "x"}),
            patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="stale")),
            self.assertRaises(NoProgress),
        ):
            sa.push_cursor(Path("repo"), old, target)

    def test_remote_sha_reads_exactly_one_ref_over_git(self):
        sha = "d" * 40
        ok = SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/heads/main\n", stderr="")
        with patch.object(sa.subprocess, "run", return_value=ok) as run:
            self.assertEqual(sa.remote_sha("main"), sha)
        self.assertEqual(run.call_args.args[0][:2], ["git", "ls-remote"])
        for out in (
            "",
            f"{sha}\trefs/heads/mainx\n",
            f"{sha}\trefs/heads/main\n{sha}\trefs/heads/main\n",
            "zz\trefs/heads/main\n",
        ):
            with (
                self.subTest(out=out),
                patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=out, stderr="")),
            ):
                with self.assertRaises(NoProgress):
                    sa.remote_sha("main")

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

    def app_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        (directory / "github-app.json").write_text(json.dumps({"id": 42, "slug": "tauceti-security-audit"}))
        self.addCleanup(setattr, sa, "_app_source", None)
        return directory

    def test_audit_app_pushes_cursor_with_its_own_token(self):
        directory = self.app_fixture()
        old, target = "a" * 40, "c" * 40
        with (
            patch.dict(sa.os.environ, {sa.APP_ENV: str(directory), "GH_TOKEN": "ghp-personal"}),
            patch.object(sa, "app_token", return_value="ghs-app-token"),
            patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run,
        ):
            sa.push_cursor(Path("repo"), old, target)
        self.assertEqual(run.call_args.kwargs["env"]["TAUCETI_AUDIT_PUSH_TOKEN"], "ghs-app-token")
        self.assertNotIn("ghs-app-token", " ".join(run.call_args.args[0]))
        with (
            patch.dict(sa.os.environ, {sa.APP_ENV: str(directory), "GH_TOKEN": "ghp-personal"}),
            patch.object(sa, "app_token", side_effect=NoProgress("no App token")),
            patch.object(sa.subprocess, "run") as run,
            self.assertRaises(NoProgress),
        ):
            sa.push_cursor(Path("repo"), old, target)
        run.assert_not_called()  # never falls back to the personal token

    def test_audit_app_authority_and_dispatch(self):
        directory = self.app_fixture()
        installed = {"full_name": "TauCetiProject/TauCeti"}
        source = SimpleNamespace(granted_permissions=lambda: {"contents": "write", "metadata": "read"})
        ok = SimpleNamespace(returncode=0, stdout=json.dumps(installed), stderr="")
        with (
            patch.dict(sa.os.environ, {sa.APP_ENV: str(directory), "GH_TOKEN": "ghp-personal"}),
            patch.object(sa, "app_token", return_value="ghs-app-token"),
            patch.object(sa, "_app_source", source),
            patch.object(sa, "execute", return_value=ok) as call,
        ):
            self.assertEqual(sa.admin_login(), "tauceti-security-audit[bot]")
            self.assertEqual(call.call_args.kwargs["env"]["GH_TOKEN"], "ghs-app-token")
            self.assertNotIn("graphql", call.call_args.args[0])
            self.assertIn(f"repos/{sa.TAUCETI}", call.call_args.args[0])
            call.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            sa.dispatch_advanced(Path("unused"), "d" * 40)
            self.assertIn(f"repos/{sa.TAUCETI}/dispatches", call.call_args.args[0])
            self.assertEqual(call.call_args.kwargs["env"]["GH_TOKEN"], "ghs-app-token")
        for repository, granted in (
            ({"full_name": "other/repo"}, {"contents": "write"}),
            (installed, {"contents": "read"}),
        ):
            with (
                self.subTest(repository=repository, granted=granted),
                patch.dict(sa.os.environ, {sa.APP_ENV: str(directory)}),
                patch.object(sa, "app_token", return_value="ghs-app-token"),
                patch.object(sa, "_app_source", SimpleNamespace(granted_permissions=lambda g=granted: g)),
                patch.object(
                    sa, "execute", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(repository), stderr="")
                ),
                self.assertRaises(Die),
            ):
                sa.admin_login()

    def test_attestation_needs_a_human_admin_even_with_the_audit_app(self):
        directory = self.app_fixture()
        target = "b" * 40
        with (
            patch.dict(sa.os.environ, {sa.APP_ENV: str(directory)}),
            patch.object(sa, "app_login", return_value="tauceti-security-audit[bot]") as app,
            patch.object(sa, "human_admin_login", side_effect=Die("not an administrator")) as human,
            patch.object(sa, "advance") as advance,
            self.assertRaises(Die),
        ):
            sa.attest(Path("unused"), target, "reviewed by hand")
        human.assert_called_once()
        app.assert_not_called()
        advance.assert_not_called()

    def test_audit_app_bypasses_only_the_cursor_ruleset(self):
        directory = self.app_fixture()
        with patch.dict(sa.os.environ, {sa.APP_ENV: str(directory)}):
            rules = {r["name"]: r["bypass_actors"] for r in sa.protections()}
        admin = {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}
        self.assertEqual(
            rules["security-audit-admins"],
            [admin, {"actor_type": "Integration", "actor_id": 42, "bypass_mode": "always"}],
        )
        self.assertEqual(rules["toolchain-tags-admins"], [admin])

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
                self.assertNotIn("TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG", kwargs["env"])
                self.assertNotEqual(kwargs["cwd"], work)
                credentials = json.loads((kwargs["cwd"] / ".codex/auth.json").read_text())
                self.assertEqual(credentials["tokens"]["refresh_token"], sa.CODEX_RT_PLACEHOLDER)
                self.assertNotIn("real-refresh-secret", json.dumps(credentials))
                self.assertIn("--output-schema", argv)
                catalog = json.loads((kwargs["cwd"] / "model-catalog.json").read_text())["models"][0]
                self.assertIsNone(catalog["apply_patch_tool_type"])
                self.assertEqual(catalog["shell_type"], "disabled")
                self.assertEqual(catalog["experimental_supported_tools"], [])
                self.assertTrue(any(a.startswith("model_catalog_json=") for a in argv))
                append_native(kwargs["cwd"], {k: verdict[k] for k in ("verdict", "findings")})
                events = [
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps({k: verdict[k] for k in ("verdict", "findings")}),
                        },
                    },
                    {"type": "turn.completed"},
                ]
                return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")

            with (
                patch.dict(
                    os.environ,
                    {
                        "GH_TOKEN": "admin-secret",
                        "GITHUB_TOKEN": "secret",
                        "TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG": "/private/bot.json",
                    },
                ),
                patch.object(sa, "codex_dir", return_value=work),
                patch.object(sa, "run_process", side_effect=model),
                patch.object(clients, "selected_protocol", return_value=sa.PROTOCOL),
            ):
                attempt = work / "attempt-1"
                attempt.mkdir()
                self.assertEqual(sa.inspect(batch, "codex", attempt), verdict)
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
        self.opts = SimpleNamespace(dry_run=False, work_model="codex")
        protocol = patch.object(clients, "selected_protocol", return_value=3)
        protocol.start()
        self.addCleanup(protocol.stop)
        for name, kwargs in (
            ("require_admin", {}),
            ("remote_sha", {"return_value": self.old}),
            ("refresh", {}),
            ("validate_history", {"return_value": [self.target]}),
            ("plan_batch", {"return_value": self.batch}),
            ("api", {"return_value": {"login": "admin"}}),
            ("admin_login", {"return_value": "admin"}),
            ("human_admin_login", {"return_value": "admin"}),
        ):
            mock = patch.object(sa, name, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

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

    def retained_incomplete(self, *, finding=False):

        work = self.state / sa.CURSOR
        original = work / "attempt-1"
        original.mkdir(parents=True)
        batch = {
            "from_sha": self.old,
            "main_sha": self.target,
            "commits": [{"sha": self.target, "parent": self.old, "files": [{}]}],
        }
        batch["input_digest"] = hashlib.sha256(sa.serialize(batch).encode()).hexdigest()
        (original / "input.json").write_text(sa.serialize(batch))
        (work / "blocked.json").write_text(
            sa.serialize(
                {
                    "from_sha": self.old,
                    "to_sha": self.target,
                    "attempt": str(original),
                    "reason": "security-audit: Unable to inspect pages 19–20: the available reader token could not be validated, so the final two pages and their source changes remain unreviewed.",
                }
            )
        )
        verdict = {
            "input_digest": batch["input_digest"],
            "commits": [self.target],
            "verdict": "finding" if finding else "incomplete",
            "findings": [
                "IO"
                if finding
                else "Unable to inspect pages 19–20: the available reader token could not be validated, so the final two pages and their source changes remain unreviewed."
            ],
        }
        sa.record(
            work / "ledger.jsonl",
            {
                "batch": batch["input_digest"],
                "provider": "codex",
                "model": "gpt-6-luna",
                "verdict": verdict,
                "execution_record": str(original / "execution.json"),
                "reader_digest": sa.LEGACY_READER_DIGEST,
            },
        )
        return work, original, batch

    def service_evidence(self, work, original):

        batch = json.loads((original / "input.json").read_text())
        prompt = sa.inspection_prompt(batch, False, policy=sa.PROMPT)
        (original / "prompt.txt").write_text(prompt)
        (original / "execution.json").write_text(
            sa.serialize(
                {
                    **sa.inspection_profile(),
                    "argv": sa.codex_launch(original / "home", False),
                    "environment": {"HOME": str(original / "home")},
                    "paged": False,
                    "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                }
            )
        )
        blocked = work / "blocked.json"
        value = json.loads(blocked.read_text())
        value["reason"] = f"security-audit: codex failed; see {original}/model-stderr.txt"
        blocked.write_text(sa.serialize(value))


if __name__ == "__main__":
    unittest.main()
