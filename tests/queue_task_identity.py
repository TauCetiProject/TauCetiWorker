#!/usr/bin/env python3
"""Source task identities survive restart and never become GitHub authority."""

import contextlib
import copy
import json
import sys
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_source_authority as native_fixtures

from tauceti_queue import monitoring, repair_parallel, repair_task, task_progress
from tauceti_queue.controller import TaskError
from tauceti_queue.source_authority import NativeQueueAuthority
from tauceti_queue.store import dumps, loads
from tauceti_queue.task_identity import TASK_SCHEMA, NativeTaskExecutionUnavailable, TaskBinding, TaskBindingMismatch


class Identity(unittest.TestCase):
    def setUp(self):
        self.native = native_fixtures.NativeClaims()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.fixture = self.native.fixture
        self.binding = TaskBinding.from_policy(self.fixture.ledger.policy, queue_epoch=self.fixture.ledger.epoch)

    def raw(self):
        return self.native.claim().to_dict()

    def config(self, **extra):
        return repair_task.RunnerConfig(
            service="https://queue.example",
            clone=self.fixture.repo.clone,
            root=self.fixture.root / "runner",
            repository=self.binding.source.repository.name,
            **extra,
        )

    def test_controller_emits_v2_without_the_legacy_repository_fallback(self):
        raw = self.raw()
        self.assertEqual(raw["schema"], TASK_SCHEMA)
        self.assertNotIn("repository", raw)
        self.assertEqual(TaskBinding.parse(raw["binding"]), self.binding)
        row = self.fixture.store.row("tasks", {"id": raw["task_id"]})
        self.assertEqual(loads(row["task_binding"]), self.binding.record())
        for member in raw["members"]:
            self.assertEqual(member["source_identity"], self.binding.pull_key(member["number"]))

    def test_checkpoint_round_trip_keeps_native_ids_and_never_restores_repository(self):
        task = repair_task.Task.from_json(self.raw())
        saved = json.loads(json.dumps(task.to_json()))
        self.assertNotIn("repository", saved)
        restored = repair_task.Task.from_json(saved)
        self.assertEqual(restored.binding, self.binding)
        self.assertEqual(restored.repository, "")
        self.assertEqual(restored.to_json(), task.to_json())

    def test_adding_legacy_repository_or_losing_schema_is_refused(self):
        raw = self.raw()
        for altered in ({**raw, "repository": "other/repo"}, {k: v for k, v in raw.items() if k != "schema"}):
            with self.assertRaises(ValueError):
                repair_task.Task.from_json(altered)

    def test_wrong_origin_id_execution_policy_and_queue_epoch_are_not_client_authority(self):
        raw = self.raw()
        variants = []
        for target, field, value in (
            ("source", "id", self.binding.source.native_id + 1),
            ("execution", "id", self.binding.execution.native_id + 1),
        ):
            altered = copy.deepcopy(raw)
            altered["binding"][target][field] = value
            variants.append(altered)
        for key, value in (("policy_digest", "f" * 64), ("queue_epoch", "f" * 32)):
            altered = copy.deepcopy(raw)
            altered["binding"][key] = value
            variants.append(altered)
        altered = copy.deepcopy(raw)
        altered["binding"]["source"]["forge"]["url"] = "https://other.example"
        altered["members"][0]["source_identity"] = TaskBinding.parse(altered["binding"]).pull_key(
            altered["members"][0]["number"]
        )
        variants.append(altered)
        client = repair_task.TaskClient("https://queue.example", "native-token", "worker", binding=self.binding)
        for altered in variants:
            with self.subTest(binding=altered["binding"]):
                changed_binding = TaskBinding.parse(altered["binding"])
                for member in altered["members"]:
                    member["source_identity"] = changed_binding.pull_key(member["number"])
                task = repair_task.Task.from_json(altered)
                with patch.object(client, "_post", side_effect=AssertionError("cross-binding request")):
                    for action in (
                        lambda task=task: client.renew(task),
                        lambda task=task: client.release(task, "worker-stopped"),
                        lambda task=task: client.submit(task, task.tip, task.submission_ref, {}),
                    ):
                        with self.assertRaises(TaskBindingMismatch):
                            action()

    def test_member_identity_and_duplicate_number_refusal(self):
        raw = self.raw()
        for field, value in (
            ("source_identity", "https://github.com/team/Math/pull/1"),
            ("source_pr", "other/repo#1"),
            ("number", True),
        ):
            altered = copy.deepcopy(raw)
            altered["members"][0][field] = value
            with self.assertRaises(ValueError):
                repair_task.Task.from_json(altered)
        altered = copy.deepcopy(raw)
        altered["members"].append(copy.deepcopy(altered["members"][0]))
        with self.assertRaises(TaskBindingMismatch):
            repair_task.Task.from_json(altered)

    def test_invalid_native_references_do_not_reach_git(self):
        raw = self.raw()
        for field, value in (
            ("task_id", "../1"),
            ("task_id", True),
            ("task_id", str(2**63)),
            ("tip", "--help"),
            ("claim_token", "\n"),
            ("expires_at", float("nan")),
            ("expires_at", 10**1000),
            ("submission_ref", "refs/heads/main"),
            ("retained_refs", ["refs/heads/main"]),
        ):
            with self.subTest(field=field, value=str(value)[:30]):
                with self.assertRaises(ValueError):
                    repair_task.Task.from_json({**raw, field: value})

    def test_unqualified_or_malformed_persisted_native_tasks_are_not_claimed(self):
        for value in ("{}", "[]", "not-json"):
            self.fixture.store.update("tasks", {"id": self.native.task_id}, task_binding=value)
            self.assertIsNone(self.native.claim())
            self.assertEqual(self.fixture.store.rows("claims", {"task_id": self.native.task_id}), [])

    def test_binding_is_preserved_over_controller_restart(self):
        raw = self.raw()
        self.fixture.restart()
        self.fixture.controller.source_authority = NativeQueueAuthority(
            self.native.identity,
            client_factory=self.native.authority._client_factory,
            clock=self.fixture.clock,
        )
        controller = self.fixture.controller
        task = self.fixture.store.row("tasks", {"id": raw["task_id"]})
        self.assertEqual(controller._task_binding(task), self.binding)
        controller.renew_task(
            raw["task_id"], raw["claim_token"], source_access=controller.source_authority.authenticate("token")
        )

    def test_policy_change_cannot_reinterpret_an_existing_claim(self):
        raw = self.raw()
        self.fixture.ledger.policy = replace(self.fixture.ledger.policy, harness_revision="e" * 40)
        with self.assertRaises(TaskError) as error:
            self.fixture.controller.renew_task(
                raw["task_id"], raw["claim_token"], source_access=self.native.authority.authenticate("token")
            )
        self.assertEqual(error.exception.code, "not-authorized")

    def test_policy_upgrade_releases_old_claim_and_replaces_current_work(self):
        raw = self.raw()
        self.fixture.ledger.policy = replace(self.fixture.ledger.policy, harness_revision="e" * 40)
        controller = self.fixture.controller
        controller.tick()
        old = self.fixture.store.row("tasks", {"id": raw["task_id"]})
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["resolution"], "policy-changed")
        self.assertEqual(TaskBinding.parse(loads(old["task_binding"])), self.binding)
        claim = self.fixture.store.row("claims", {"task_id": raw["task_id"], "token": raw["claim_token"]})
        self.assertIsNotNone(claim["released_at"])
        fresh = self.native.claim()
        self.assertNotEqual(fresh.task_id, raw["task_id"])
        self.assertEqual(fresh.binding.policy_digest, self.fixture.ledger.policy.digest)
        self.assertTrue(self.fixture.store.rows("journal", {"kind": "task-policy-changed"}))

    def test_late_result_after_policy_upgrade_is_retained_and_audited_without_adoption(self):
        from tauceti_queue import chain

        raw = self.raw()
        old_batch = self.fixture.store.row("batches", {"id": raw["batch_id"]})
        self.fixture.ledger.policy = replace(self.fixture.ledger.policy, harness_revision="e" * 40)
        result = self.fixture.controller.submit_task(
            raw["task_id"], raw["claim_token"], raw["tip"], source_access=self.native.authority.authenticate("token")
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "policy-changed")
        self.assertEqual(chain.git(self.fixture.repo.clone, "rev-parse", result.retained_ref).strip(), raw["tip"])
        self.assertEqual(self.fixture.store.row("batches", {"id": raw["batch_id"]})["version"], old_batch["version"])
        self.assertEqual(
            self.fixture.store.rows("submissions", {"task_id": raw["task_id"]})[0]["reason"], "policy-changed"
        )
        self.assertEqual(self.fixture.store.row("tasks", {"id": raw["task_id"]})["state"], "superseded")

    def test_owner_can_release_stale_policy_work_before_the_next_tick(self):
        raw = self.raw()
        self.fixture.ledger.policy = replace(self.fixture.ledger.policy, harness_revision="e" * 40)
        self.fixture.controller.release_task(
            raw["task_id"],
            raw["claim_token"],
            "worker-stopped",
            source_access=self.native.authority.authenticate("token"),
        )
        claim = self.fixture.store.row("claims", {"task_id": raw["task_id"], "token": raw["claim_token"]})
        self.assertEqual(claim["release_reason"], "worker-stopped")
        self.assertIsNotNone(claim["released_at"])
        self.assertNotEqual(self.native.claim().task_id, raw["task_id"])

    def test_policy_change_during_submit_refresh_keeps_the_result_but_refuses_adoption(self):
        raw = self.raw()

        def refresh():
            self.fixture.ledger.policy = replace(self.fixture.ledger.policy, harness_revision="e" * 40)
            return self.native.authority.authenticate("token")

        result = self.fixture.controller.submit_task(
            raw["task_id"],
            raw["claim_token"],
            raw["tip"],
            source_access=self.native.authority.authenticate("token"),
            source_refresh=refresh,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "policy-changed")
        self.assertTrue(result.retained_ref)

    def test_native_execution_refusal_precedes_legacy_admin_git_leases_and_agent_work(self):
        task = repair_task.Task.from_json(self.raw())
        cfg = self.config(binding=self.binding, token="native-token")
        client = Mock()
        with (
            patch.object(repair_task, "git", side_effect=AssertionError("legacy Git")),
            patch.object(repair_task, "verify_admin", side_effect=AssertionError("legacy ACL")),
            patch.object(repair_task.ClaimLease, "start", side_effect=AssertionError("lease")),
        ):
            for action in (
                lambda: repair_task.run_task(cfg, task, client),
                lambda: repair_task.fetch_retained(cfg.clone, task),
                lambda: repair_task.push_result(cfg, task, cfg.clone, task.tip, cfg.root, None),
                lambda: repair_task.claim_and_run(cfg, client),
                lambda: repair_task.AdminCheck(cfg).ensure(),
                lambda: repair_task.serve(cfg, client, repair_task.Stop()),
                lambda: repair_task.run_repair_agent(cfg, task, cfg.clone, "", cfg.root, threading.Event(), Mock(), {}),
                lambda: repair_task.run_parallel_groups(
                    cfg, task, cfg.clone, [], {}, None, cfg.root, threading.Event(), Mock(), {}
                ),
            ):
                with self.assertRaises(NativeTaskExecutionUnavailable):
                    action()
        self.assertFalse(cfg.root.exists())
        self.assertFalse(client.mock_calls)

    def test_native_make_client_never_falls_back_to_the_github_token(self):
        cfg = self.config(binding=self.binding)
        with patch.object(repair_task, "worker_token", side_effect=AssertionError("GitHub token lookup")):
            with self.assertRaises(TaskBindingMismatch):
                repair_task.make_client(cfg)
            client = repair_task.make_client(replace(cfg, token="native-token"))
            self.assertEqual(client.binding, self.binding)
            self.assertFalse(repair_task.preflight(cfg)[0]["ok"])

    def test_parallel_job_configuration_round_trip_retains_trusted_binding_without_token(self):
        cfg = self.config(binding=self.binding, token="private-token")
        saved = repair_parallel.config_to_json(cfg)
        self.assertNotIn("private-token", json.dumps(saved))
        restored = repair_parallel.config_from_json(json.loads(json.dumps(saved)), repair_task.RunnerConfig)
        self.assertEqual(restored.binding, self.binding)
        self.assertIsNone(restored.token)

    def test_parallel_subprocess_entry_refuses_native_work_before_credentials_or_agent_state(self):
        import os

        task = repair_task.Task.from_json(self.raw())
        cfg = self.config(binding=self.binding)
        path = self.fixture.root / "native-group-job.json"
        path.write_text(json.dumps({"cfg": repair_parallel.config_to_json(cfg), "task": task.to_json()}))
        with patch.dict(os.environ, {repair_parallel.API_KEY_ENV: "private-agent-key"}):
            with self.assertRaises(NativeTaskExecutionUnavailable):
                repair_parallel.run_group_job(path)
            self.assertEqual(os.environ[repair_parallel.API_KEY_ENV], "private-agent-key")

    def test_resuming_or_releasing_native_checkpoint_never_uses_a_legacy_client(self):
        task = repair_task.Task.from_json(self.raw())
        cfg = self.config()
        state = repair_task.TaskState(cfg.tasks_dir / task.task_id, task)
        state.write(
            "agent_running", worker=cfg.worker, pending_release={"reason": "worker-stopped", "error": "", "next_at": 0}
        )
        before = state.path.read_bytes()
        client = Mock()
        for configured in (cfg, replace(cfg, binding=self.binding)):
            self.assertEqual(repair_task.resume_pending(configured, client), [])
            self.assertEqual(repair_task.release_stale_claims(configured, client), 0)
            self.assertEqual(repair_task.retry_releases(configured, client), [])
        self.assertFalse(client.mock_calls)
        self.assertEqual(state.path.read_bytes(), before)

    def test_legacy_controller_never_adopts_bound_or_falsy_nonobject_task_rows(self):
        from tauceti_queue.fakes import make_controller

        legacy, github, _repo, clock = make_controller(self.fixture.root / "legacy")
        self.addCleanup(legacy.store.close)
        head = github.create_pr(27, {"B.lean": "example : True := trivial\n"})
        legacy.approve(legacy.repository, 27, head, "reviewer")
        legacy.tick()
        batch = legacy._head()
        task_id = legacy._create_task(batch, "repair", batch["base"], [], clock())
        token = "a" * 32
        claim_id = legacy.store.insert(
            "claims",
            batch_id=batch["id"],
            batch_version=batch["version"],
            operation="task:repair",
            worker="worker",
            github_login="reviewer",
            token=token,
            starting_tip=batch["tip"],
            task_id=task_id,
            created_at=clock(),
            expires_at=clock() + 900,
        )
        with (
            patch.object(legacy, "_repository_admin", return_value=True),
            patch.object(legacy, "_retain_submission", side_effect=AssertionError("foreign task Git I/O")),
        ):
            for value in (self.binding.record(), [], None, False, 0, ""):
                legacy.store.update("tasks", {"id": task_id}, task_binding=dumps(value))
                self.assertIsNone(legacy.claim_task("other-worker", "reviewer"))
                for action in (
                    lambda: legacy.renew_task(task_id, token),
                    lambda: legacy.release_task(task_id, token, "worker-stopped"),
                    lambda: legacy.submit_task(task_id, token, batch["tip"]),
                ):
                    with self.assertRaises(TaskError):
                        action()
            claim = legacy.store.row("claims", {"id": claim_id})
            self.assertIsNone(claim["released_at"])
            self.assertIsNone(claim["renewed_at"])

    def test_controller_display_repository_cannot_differ_from_its_native_binding(self):
        from tauceti_queue.controller import Controller

        with self.assertRaisesRegex(ValueError, "qualified source"):
            Controller(
                self.fixture.store,
                self.fixture.source,
                str(self.fixture.repo.clone),
                "other/repo",
                revision="identity-mismatch-test",
                candidate_ci=self.fixture.ci,
            )

    def test_all_native_revision_and_ref_hints_are_validated(self):
        raw = self.raw()
        variants = []
        for key, value in (
            ("batch_id", "../1"),
            ("batch_version", True),
            ("warm_revs", ["--upload-pack=evil"]),
            ("renew_after", -1),
        ):
            variants.append({**raw, key: value})
        for key, value in (("canonical_branch", "--help"), ("renew_after", -1), ("pre_rebased", {"source": "--help"})):
            variants.append({**raw, "extra": {**raw["extra"], key: value}})
        for key, value in (("commit", "--help"), ("retained_ref", "refs/heads/main")):
            altered = copy.deepcopy(raw)
            altered["members"][0][key] = value
            variants.append(altered)
        for altered in variants:
            with self.assertRaises(ValueError):
                repair_task.Task.from_json(altered)

    def test_progress_is_bound_to_native_repository_policy_and_queue_epoch(self):
        task = repair_task.Task.from_json(self.raw())
        root = self.fixture.root / "progress"
        state = repair_task.TaskState(root / "tasks" / task.task_id, task)
        state.write("agent_running")
        record = json.loads((state.directory / task_progress.FILENAME).read_text())
        public = task_progress.public(record)
        self.assertEqual(public["task_binding"], self.binding.record())
        self.assertNotIn(task.claim_token, json.dumps(public))
        observer = monitoring.TaskObservations()
        row = self.fixture.store.row("tasks", {"id": int(task.task_id)})
        self.assertIn(int(task.task_id), observer.observe([root], [row]))
        for binding in ({}, replace(self.binding, queue_epoch="f" * 32).record()):
            foreign = {**row, "task_binding": dumps(binding)}
            self.assertNotIn(int(task.task_id), observer.observe([root], [foreign]))
            with patch.object(monitoring, "json_read", side_effect=AssertionError("mismatched checkpoint read again")):
                self.assertNotIn(int(task.task_id), observer.observe([root], [foreign]))

    def test_sql_task_identity_is_reported_without_a_worker_checkpoint(self):
        self.raw()
        row = self.fixture.store.row("tasks", {"id": self.native.task_id})
        self.assertEqual(monitoring.task_binding_fields(row), {"task_binding": self.binding.record()})
        self.assertEqual(monitoring.task_binding_fields({}), {})
        self.assertEqual(monitoring.task_binding_fields({"task_binding": "broken"})["identity_coverage"], "invalid")
        with self.assertRaisesRegex(ValueError, "qualified public monitoring policy"):
            monitoring.project(self.fixture.store.path, public_repositories=[self.binding.source.repository.name])

    def test_native_renewal_requires_a_finite_numeric_expiry(self):
        task = repair_task.Task.from_json(self.raw())
        client = repair_task.TaskClient("https://queue.example", "token", "worker", binding=self.binding)
        for value in (True, float("nan"), float("inf"), -1, "9000000000"):
            with patch.object(client, "_post", return_value=(200, {"expires_at": value})):
                with self.assertRaises(repair_task.ServiceError):
                    client.renew(task)

    def test_legacy_task_clients_accept_a_task_from_another_module_instance(self):
        from types import SimpleNamespace

        task = SimpleNamespace(binding=None, task_id="1", claim_token="a" * 32)
        client = repair_task.TaskClient("https://queue.example", "token", "worker")
        with patch.object(client, "_post", return_value=(200, {"expires_at": 900})):
            self.assertEqual(client.renew(task), 900)


class ClientHTTP(unittest.TestCase):
    def setUp(self):
        self.native = native_fixtures.NativeHTTP()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.binding = TaskBinding.from_policy(
            self.native.fixture.ledger.policy, queue_epoch=self.native.fixture.ledger.epoch
        )
        self.service = f"http://127.0.0.1:{self.native.http.server_address[1]}"

    def test_real_native_http_claim_renew_and_release_preserve_binding(self):
        client = repair_task.TaskClient(self.service, "token", "worker", binding=self.binding)
        task = client.claim()
        self.assertEqual(task.binding, self.binding)
        self.assertGreater(client.renew(task), 0)
        client.release(task, "worker-stopped")
        claim = self.native.store.rows("claims", {"task_id": int(task.task_id)})[0]
        self.assertIsNotNone(claim["released_at"])

    def test_legacy_client_cannot_interpret_native_task_as_a_github_task(self):
        with self.assertRaises(repair_task.ServiceError):
            repair_task.TaskClient(self.service, "token", "old-worker").claim()
        self.assertEqual(self.native.store.rows("claims", {"task_id": self.native.task_id}), [])
        self.assertEqual(self.native.clients, [])

    def test_mismatched_claim_policy_is_refused_before_native_api_or_lease_allocation(self):
        client = repair_task.TaskClient(
            self.service, "token", "worker", binding=replace(self.binding, queue_epoch="f" * 32)
        )
        with self.assertRaises(repair_task.ServiceError):
            client.claim()
        self.assertEqual(self.native.store.rows("claims", {"task_id": self.native.task_id}), [])
        self.assertEqual(self.native.clients, [])

    def test_policy_is_rechecked_under_controller_lock_after_native_reads(self):
        client = repair_task.TaskClient(self.service, "token", "worker", binding=self.binding)
        original = self.native.authority.authenticate

        def changed(token):
            evidence = original(token)
            ledger = self.native.fixture.ledger
            ledger.policy = replace(ledger.policy, harness_revision="e" * 40)
            return evidence

        with patch.object(self.native.authority, "authenticate", side_effect=changed):
            with self.assertRaises(repair_task.ClaimRefused):
                client.claim()
        self.assertEqual(self.native.store.rows("claims", {"task_id": self.native.task_id}), [])

    def test_native_service_requires_secure_origin_and_explicit_bearer(self):
        for url in ("http://outside.example", "https://user:pass@queue.example", "https://queue.example/#fragment"):
            with self.assertRaises(ValueError):
                repair_task.TaskClient(url, "token", "worker", binding=self.binding)
        for bearer in ("", "bad\nvalue", None):
            with self.assertRaises(ValueError):
                repair_task.TaskClient(self.service, bearer, "worker", binding=self.binding)

    def test_redirect_never_forwards_the_native_bearer_to_a_second_origin(self):
        sink_calls = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                sink_calls.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(BaseHTTPRequestHandler):
            status = 307

            def do_POST(self):
                self.send_response(self.status)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stolen")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (sink, redirect):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        client = repair_task.TaskClient(
            f"http://127.0.0.1:{redirect.server_port}", "native-token", "worker", binding=self.binding
        )
        for status in (301, 302, 303, 307, 308):
            Redirect.status = status
            with self.assertRaises(repair_task.ServiceError):
                client.claim()
        self.assertEqual(sink_calls, [])

    def test_native_response_bounds_and_duplicate_json_are_enforced_on_the_wire(self):
        class Response(BaseHTTPRequestHandler):
            payload = b"{}"
            status = 200

            def do_POST(self):
                self.send_response(self.status)
                self.send_header("Content-Length", str(len(self.payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(self.payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Response)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = repair_task.TaskClient(
            f"http://127.0.0.1:{server.server_port}", "token", "worker", binding=self.binding
        )
        Response.payload = b'{"binding":{},"binding":{}}'
        with self.assertRaisesRegex(repair_task.ServiceError, "not JSON"):
            client.claim()
        Response.payload = b" " * (16 * 1024 * 1024) + b"{}"
        with self.assertRaisesRegex(repair_task.ServiceError, "byte bound"):
            client.claim()
        Response.status = 500
        with self.assertRaisesRegex(repair_task.ServiceError, "byte bound"):
            client.claim()
        Response.status = 200
        Response.payload = b"{}"
        with self.assertRaises(repair_task.ServiceError):
            client.claim()


if __name__ == "__main__":
    unittest.main()
