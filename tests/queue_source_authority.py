#!/usr/bin/env python3
"""Fresh source authority refuses revocation, outages and reused identities."""

import copy
import http.client
import json
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_source_ci
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forge, ForgeError, RepositoryRef

from tauceti_queue import chain, server
from tauceti_queue.controller import Controller
from tauceti_queue.source_authority import (
    NativeQueueAuthority,
    SourceAccessDenied,
    SourceAuthorityUnavailable,
    principal_key,
)
from tauceti_queue.store import loads


class SourceAuthority(unittest.TestCase):
    def setUp(self):
        self.repository = RepositoryRef(Forge("forgejo", "https://source.example"), "team/source")
        self.identity = RepositoryIdentity(self.repository, 41)
        self.actor = {"id": 7, "login": "operator"}
        self.repo = {
            "id": 41,
            "full_name": "team/source",
            "html_url": "https://source.example/team/source",
            "permissions": {"pull": True, "push": True, "admin": True},
            "archived": False,
            "mirror": False,
        }
        self.error = None
        self.calls = []
        self.clients = []
        outer = self

        class Client:
            def __init__(self, forge, *, token, **_bounds):
                outer.assertEqual(forge, outer.repository.forge)
                outer.clients.append(self)
                self.token = token

            def request(self, method, path):
                outer.calls.append((method, path, self.token))
                if outer.error:
                    raise outer.error
                return copy.deepcopy(outer.actor if path == "user" else outer.repo)

        self.authority = NativeQueueAuthority(self.identity, client_factory=Client)

    def test_repeated_bearer_observation_cannot_reuse_a_revoked_identity(self):
        self.authority.require_admin(self.authority.observe("token"))
        self.error = ForgeError("private upstream error", status=401)
        with self.assertRaises(SourceAccessDenied):
            self.authority.observe("token")
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len(self.calls), 3)

    def test_changed_acl_and_archival_are_seen_after_prior_success(self):
        self.authority.require_admin(self.authority.observe("token"))
        self.repo["permissions"]["admin"] = False
        with self.assertRaises(SourceAccessDenied):
            self.authority.require_admin(self.authority.observe("token"))
        self.repo["permissions"]["admin"] = True
        self.repo["archived"] = True
        with self.assertRaises(SourceAccessDenied):
            self.authority.require_admin(self.authority.observe("token"))

    def test_admin_role_does_not_supply_missing_code_write_access(self):
        self.repo["permissions"]["push"] = False
        with self.assertRaises(SourceAccessDenied):
            self.authority.require_admin(self.authority.observe("token"))

    def test_read_only_mirror_is_not_a_writable_queue_source(self):
        self.repo["mirror"] = True
        with self.assertRaises(SourceAccessDenied):
            self.authority.require_admin(self.authority.observe("token"))

    def test_username_rename_preserves_identity_and_reuse_does_not(self):
        first = self.authority.observe("token").actor
        self.actor["login"] = "renamed"
        renamed = self.authority.observe("token").actor
        self.assertEqual(principal_key(first), principal_key(renamed))
        self.assertNotEqual(first.login, renamed.login)
        self.actor.update(id=8, login="operator")
        replacement = self.authority.observe("replacement-token").actor
        self.assertNotEqual(principal_key(first), principal_key(replacement))

    def test_same_name_new_repository_id_never_inherits_authority(self):
        self.authority.require_admin(self.authority.observe("token"))
        self.repo["id"] = 42
        with self.assertRaisesRegex(SourceAccessDenied, "identity changed"):
            self.authority.observe("token")

    def test_outage_never_serves_a_stale_admin_answer_or_leaks_upstream_text(self):
        self.authority.require_admin(self.authority.observe("private-token"))
        for status in (0, 429, 500, 502):
            with self.subTest(status=status):
                self.error = ForgeError("private-token and response body", status=status)
                with self.assertRaises(SourceAuthorityUnavailable) as caught:
                    self.authority.observe("private-token")
                self.assertNotIn("private-token", str(caught.exception))

    def test_scope_or_visibility_denial_does_not_become_an_outage_grant(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.error = ForgeError("private upstream error", status=status)
                with self.assertRaises(SourceAccessDenied):
                    self.authority.observe("token")

    def test_wrong_origin_and_malformed_native_records_are_unavailable(self):
        self.repo["html_url"] = "https://other.example/team/source"
        with self.assertRaises(SourceAuthorityUnavailable):
            self.authority.observe("token")
        self.repo["html_url"] = self.repository.url
        self.repo["permissions"]["admin"] = "true"
        with self.assertRaises(SourceAuthorityUnavailable):
            self.authority.observe("token")

    def test_invalid_bearers_never_reach_a_client(self):
        for token in (None, "", " token", "token\n", "token\x00", "\u2603"):
            with self.subTest(token=token):
                with self.assertRaises(SourceAccessDenied):
                    self.authority.observe(token)
        self.assertEqual(self.clients, [])

    def test_observation_age_includes_the_native_reads(self):
        now = [10]
        self.authority.clock = lambda: now[0]
        observe = self.authority.observe

        def slow_read(token):
            access = observe(token)
            now[0] += 16
            return access

        with patch.object(self.authority, "observe", side_effect=slow_read):
            evidence = self.authority.authenticate("token")
        with self.assertRaises(SourceAuthorityUnavailable):
            self.authority.validate(evidence)


class NativeClaims(unittest.TestCase):
    # Keep the provider-only tests above in their own case; this case exercises
    # the actual controller against a real temporary Git repository and ledger.
    def setUp(self):
        SourceAuthority.setUp(self)
        fixture = queue_source_ci.SourceController()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.identity = fixture.source.repository_identity
        self.repository = self.identity.repository
        self.repo.update(id=self.identity.native_id, full_name=self.repository.name, html_url=self.repository.url)
        self.authority = NativeQueueAuthority(
            self.identity, client_factory=self.authority._client_factory, clock=fixture.clock
        )
        fixture.controller = Controller(
            fixture.store,
            fixture.source,
            str(fixture.repo.clone),
            queue_source_ci.PROJECT,
            revision="native-claims",
            candidate_ci=fixture.ci,
            source_authority=self.authority,
            clock=fixture.clock,
        )
        fixture.start()
        self.controller = fixture.controller
        self.store = fixture.store
        self.batch = self.store.rows("batches")[0]
        self.task_id = self.controller._create_task(self.batch, "repair", self.batch["base"], [], fixture.clock())

    def claim(self):
        return self.controller.claim_task("worker", "", source_access=self.authority.authenticate("token"))

    def test_claim_records_native_identity_without_legacy_login_or_bearer(self):
        with patch.object(self.controller.admins, "get", side_effect=AssertionError("legacy ACL cache used")):
            task = self.claim()
        self.assertIsNotNone(task)
        claim = self.store.rows("claims", {"task_id": self.task_id})[0]
        self.assertEqual(claim["github_login"], "")
        self.assertEqual(
            loads(claim["source_actor"]),
            {"origin": self.repository.forge.key, "id": "7", "login": "operator"},
        )
        self.assertNotEqual(claim["token"], "token")

    def test_native_claim_refuses_an_unqualified_cached_github_login(self):
        with patch.object(self.controller.admins, "get", return_value=True) as cache:
            with self.assertRaises(SourceAuthorityUnavailable):
                self.controller.claim_task("worker", "operator")
        cache.assert_not_called()
        self.assertEqual(self.store.rows("claims", {"task_id": self.task_id}), [])

    def test_rename_keeps_claim_and_reused_login_cannot_renew_release_or_submit(self):
        task = self.claim()
        self.actor["login"] = "renamed"
        self.fixture.clock.advance(1)
        self.controller.renew_task(task.task_id, task.claim_token, source_access=self.authority.authenticate("token"))
        self.actor.update(id=8, login="operator")
        replacement = self.authority.authenticate("other-token")
        with patch.object(self.controller, "_retain_submission", side_effect=AssertionError("unauthorized Git I/O")):
            for action in (
                lambda: self.controller.renew_task(task.task_id, task.claim_token, source_access=replacement),
                lambda: self.controller.release_task(
                    task.task_id, task.claim_token, "released", source_access=replacement
                ),
                lambda: self.controller.submit_task(
                    task.task_id, task.claim_token, self.batch["tip"], source_access=replacement
                ),
            ):
                with self.assertRaises(PermissionError):
                    action()
        self.assertIsNone(self.store.rows("claims", {"task_id": self.task_id})[0]["released_at"])
        self.assertEqual(self.store.rows("submissions"), [])

    def test_stale_observation_after_lock_wait_requires_fresh_source_reads(self):
        evidence = self.authority.authenticate("token")
        self.fixture.clock.advance(16)
        with self.assertRaises(SourceAuthorityUnavailable):
            self.controller.claim_task("worker", "", source_access=evidence)
        self.assertEqual(self.store.rows("claims", {"task_id": self.task_id}), [])
        self.assertIsNotNone(self.claim())

    def test_acl_revocation_prevents_claim_renewal_even_with_the_claim_token(self):
        task = self.claim()
        self.repo["permissions"]["admin"] = False
        denied = self.authority.authenticate("token")
        before = self.store.rows("claims", {"task_id": self.task_id})[0]
        with self.assertRaises(SourceAccessDenied):
            self.controller.renew_task(task.task_id, task.claim_token, source_access=denied)
        self.assertEqual(self.store.rows("claims", {"task_id": self.task_id})[0], before)

    def test_submission_cannot_adopt_after_retention_outlives_its_observation(self):
        task = self.claim()
        evidence = self.authority.authenticate("token")
        retain = self.controller._retain_submission

        def slow_retention(*args):
            retained = retain(*args)
            self.fixture.clock.advance(16)
            return retained

        with patch.object(self.controller, "_retain_submission", side_effect=slow_retention):
            with self.assertRaises(SourceAuthorityUnavailable):
                self.controller.submit_task(task.task_id, task.claim_token, self.batch["tip"], source_access=evidence)
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"])
        self.assertEqual(self.store.rows("submissions"), [])

    def test_operator_retry_rechecks_authority_after_slow_execution_discovery(self):
        evidence = self.authority.authenticate("token")
        attempt = self.store.rows("ci_attempts")[0]

        def slow_discovery(*_args):
            self.fixture.clock.advance(16)
            return None

        with patch.object(self.controller, "_live_run", side_effect=slow_discovery):
            with self.assertRaises(SourceAuthorityUnavailable):
                self.controller.retry_ci(
                    self.repository.name, "display-only", request_id=attempt["request_id"], source_access=evidence
                )
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], attempt["outcome"])

    def test_rebuild_cannot_adopt_after_slow_source_fetch(self):
        evidence = self.authority.authenticate("token")

        def slow_fetch(*_args):
            self.fixture.clock.advance(16)
            return True

        with patch.object(self.controller, "_fetch_head", side_effect=slow_fetch):
            with self.assertRaises(SourceAuthorityUnavailable):
                self.controller.rebuild_head(self.repository.name, "display-only", source_access=evidence)
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"])

    def test_restart_keeps_native_owner_but_requires_new_process_local_evidence(self):
        task = self.claim()
        old_evidence = self.authority.authenticate("token")
        self.fixture.restart()
        self.store = self.fixture.store
        self.controller = self.fixture.controller
        self.authority = NativeQueueAuthority(
            self.identity, client_factory=self.authority._client_factory, clock=self.fixture.clock
        )
        self.controller.source_authority = self.authority
        with self.assertRaises(SourceAuthorityUnavailable):
            self.controller.renew_task(task.task_id, task.claim_token, source_access=old_evidence)
        self.actor["login"] = "renamed"
        self.controller.renew_task(task.task_id, task.claim_token, source_access=self.authority.authenticate("token"))
        owner = loads(self.store.rows("claims", {"task_id": self.task_id})[0]["source_actor"])
        self.assertEqual(owner["id"], "7")

    def test_legacy_claim_login_cannot_acquire_a_native_owner_implicitly(self):
        task = self.claim()
        self.store.update("claims", {"task_id": task.task_id}, source_actor="{}", github_login="operator")
        with self.assertRaises(PermissionError):
            self.controller.renew_task(
                task.task_id, task.claim_token, source_access=self.authority.authenticate("token")
            )

    def test_long_chain_validation_can_refresh_authority_and_adopt(self):
        task = self.claim()
        validate = chain.validate

        def slow_validation(*args, **kwargs):
            report = validate(*args, **kwargs)
            self.fixture.clock.advance(16)
            return report

        with patch.object(chain, "validate", side_effect=slow_validation):
            result = self.controller.submit_task(
                task.task_id,
                task.claim_token,
                self.batch["tip"],
                source_access=self.authority.authenticate("token"),
                source_refresh=lambda: self.authority.authenticate("token"),
            )
        self.assertTrue(result.accepted)
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"] + 1)

    def test_revoked_acl_during_validation_is_not_replaced_by_prior_authority(self):
        task = self.claim()
        validate = chain.validate

        def revoked(*args, **kwargs):
            report = validate(*args, **kwargs)
            self.repo["permissions"]["admin"] = False
            return report

        with patch.object(chain, "validate", side_effect=revoked):
            with self.assertRaises(SourceAccessDenied):
                self.controller.submit_task(
                    task.task_id,
                    task.claim_token,
                    self.batch["tip"],
                    source_access=self.authority.authenticate("token"),
                    source_refresh=lambda: self.authority.authenticate("token"),
                )
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"])

    def test_task_change_during_unlocked_authority_refresh_refuses_adoption(self):
        task = self.claim()

        def replaced():
            with self.controller._lock:
                self.controller._create_task(self.batch, "repair", self.batch["base"], [], self.fixture.clock())
            return self.authority.authenticate("token")

        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            self.batch["tip"],
            source_access=self.authority.authenticate("token"),
            source_refresh=replaced,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "version-mismatch")
        self.assertEqual(self.store.rows("submissions")[0]["retained_ref"], result.retained_ref)
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"])

    def test_late_native_result_is_retained_and_recorded_as_a_rejection(self):
        task = self.claim()
        self.fixture.clock.advance(901)
        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            self.batch["tip"],
            source_access=self.authority.authenticate("token"),
            source_refresh=lambda: self.authority.authenticate("token"),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "claim-expired")
        submission = self.store.rows("submissions")[0]
        self.assertEqual(submission["retained_ref"], result.retained_ref)
        self.assertTrue(submission["retained_ref"])
        self.assertEqual(submission["created_at"], self.fixture.clock())
        self.assertEqual(self.store.row("tasks", {"id": task.task_id})["submissions"], 1)

    def test_claim_released_during_refresh_still_records_the_late_result(self):
        task = self.claim()

        def released():
            with self.controller._lock:
                self.store.update(
                    "claims", {"task_id": task.task_id}, released_at=self.fixture.clock(), release_reason="released"
                )
            return self.authority.authenticate("token")

        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            self.batch["tip"],
            source_access=self.authority.authenticate("token"),
            source_refresh=released,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "claim-superseded")
        self.assertEqual(self.store.rows("submissions")[0]["retained_ref"], result.retained_ref)

    def test_unrelated_batch_metadata_during_refresh_does_not_discard_a_repair(self):
        task = self.claim()

        def urgent():
            with self.controller._lock:
                self.store.update("batches", {"id": self.batch["id"]}, urgent=1)
            self.fixture.clock.advance(5)
            return self.authority.authenticate("token")

        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            self.batch["tip"],
            source_access=self.authority.authenticate("token"),
            source_refresh=urgent,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["urgent"], 1)
        self.assertEqual(self.store.rows("submissions")[0]["created_at"], self.fixture.clock())

    def background_pre_rebase(self):
        self.store.update("tasks", {"id": self.task_id}, state="superseded")
        self.controller.set_setting("repair_concurrency", 2, "fixture")
        accepted = self.fixture.source.create_pr(18, {"B.lean": "b\n"})
        self.assertTrue(self.controller.approve(self.repository.name, 18, accepted, "reviewer")["accepted"])
        self.controller.tick()
        batch = self.store.row("batches", {"state": "open"})
        self.assertIsNotNone(batch)
        task_id = self.controller._create_task(batch, "pre_rebase", self.batch["tip"], [], self.fixture.clock())
        task = self.claim()
        self.assertEqual((task.task_id, task.kind), (task_id, "pre_rebase"))
        replay = chain.replay(
            self.fixture.repo.clone, batch["base"], batch["tip"], self.batch["tip"], chain_id="native-authority-prebase"
        )
        self.assertEqual(replay.status, chain.CLEAN)
        return task, batch, replay.tip

    def test_pre_rebase_refuses_a_head_move_during_authority_refresh(self):
        task, batch, tip = self.background_pre_rebase()

        def moved():
            with self.controller._lock:
                self.store.update("batches", {"id": self.batch["id"]}, tip="d" * 40, version=self.batch["version"] + 1)
            return self.authority.authenticate("token")

        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            tip,
            source_access=self.authority.authenticate("token"),
            source_refresh=moved,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "version-mismatch")
        self.assertEqual(self.store.row("batches", {"id": batch["id"]})["pre_rebase_tip"], batch["pre_rebase_tip"])

    def test_pre_rebase_keeps_valid_work_when_insertion_appends_during_refresh(self):
        task, batch, tip = self.background_pre_rebase()

        def appended():
            with self.controller._lock:
                accepted = self.fixture.source.create_pr(19, {"C.lean": "c\n"})
                self.assertTrue(self.controller.approve(self.repository.name, 19, accepted, "reviewer")["accepted"])
                self.controller.tick()
            return self.authority.authenticate("token")

        result = self.controller.submit_task(
            task.task_id,
            task.claim_token,
            tip,
            source_access=self.authority.authenticate("token"),
            source_refresh=appended,
        )
        self.assertTrue(result.accepted, result)
        current = self.store.row("batches", {"id": batch["id"]})
        self.assertGreater(current["version"], batch["version"])
        self.assertEqual({m["number"] for m in self.controller._members(batch["id"])}, {18, 19})

    def test_authority_reacquisition_timeout_refreshes_while_another_thread_holds_the_lock(self):
        self.authority.clock = time.monotonic
        initial = self.authority.authenticate("token")
        held, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def holder():
            try:
                with self.controller._lock:
                    held.set()
                    if not release.wait(3):
                        raise AssertionError("refresh did not retry timed lock acquisition")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=holder, daemon=True)

        def refresh():
            fresh = self.authority.authenticate("token")
            calls.append(fresh)
            if len(calls) == 1:
                thread.start()
                self.assertTrue(held.wait(2))
                return replace(fresh, checked_at=fresh.checked_at - 14.99)
            release.set()
            return fresh

        try:
            with self.controller._lock.priority():
                fresh = self.controller._refresh_source_access(initial, refresh)
                self.authority.validate(fresh)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())

    def test_overlapping_renewals_keep_the_second_registration_after_the_first_finishes(self):
        task = self.claim()
        self.fixture.clock.advance(899)
        first_registered, both_registered = threading.Event(), threading.Event()
        second_refreshing, release_second = threading.Event(), threading.Event()
        results = {}
        assignments = []

        class Renewals(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                assignments.append(value)
                (first_registered if len(assignments) == 1 else both_registered).set()

        self.controller._renewing = Renewals()

        def renew(name, evidence):
            def refresh():
                if name == "second":
                    second_refreshing.set()
                    if not release_second.wait(5):
                        raise AssertionError("second renewal was not released")
                return self.authority.authenticate("token")

            try:
                results[name] = self.controller.renew_task(
                    task.task_id, task.claim_token, source_access=evidence, source_refresh=refresh
                )
            except Exception as error:
                results[name] = error

        first = threading.Thread(target=renew, args=("first", self.authority.authenticate("token")), daemon=True)
        second = None
        try:
            with self.controller._lock:
                first.start()
                self.assertTrue(first_registered.wait(2))
                self.fixture.clock.advance(0.5)
                second = threading.Thread(
                    target=renew, args=("second", self.authority.authenticate("token")), daemon=True
                )
                second.start()
                self.assertTrue(both_registered.wait(2))
                self.fixture.clock.advance(16)
            self.assertTrue(second_refreshing.wait(2))
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertIsInstance(results["first"], float)
            with self.controller._lock:
                self.fixture.clock.advance(901)
                self.controller._expire_claims(self.fixture.clock())
                self.assertIsNone(self.store.rows("claims", {"task_id": self.task_id})[0]["released_at"])
                self.assertIn(task.claim_token, self.controller._renewing)
        finally:
            release_second.set()
            first.join(5)
            if second is not None:
                second.join(5)
        self.assertIsInstance(results["second"], float)
        self.assertNotIn(task.claim_token, self.controller._renewing)
        self.assertNotIn(task.claim_token, self.controller._renewal_waiters)


class NativeHTTP(unittest.TestCase):
    def setUp(self):
        NativeClaims.setUp(self)
        self.config = server.ServerConfig(
            webhook_secret=b"unused-native-secret",
            repositories={41: self.repository.name},
            adapter=self.fixture.source,
            port=0,
            identity_revalidate_seconds=0,
        )
        self.http = server.create_server(self.controller, self.config)
        self.thread = server.serve_in_thread(self.http)

        def stop():
            self.http.shutdown()
            self.http.server_close()
            self.thread.join(2)

        self.addCleanup(stop)

    def request(self, path, body, *, token="token"):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=10)
        try:
            headers = {"Authorization": "Bearer " + token} if token is not None else {}
            connection.request("POST", path, json.dumps(body).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def test_native_http_claim_and_renewal_never_use_legacy_identity_caches(self):
        with patch.object(self.controller.admins, "get", side_effect=AssertionError("legacy cache")):
            status, task = self.request("/tasks/claim", {"worker": "worker"})
            self.assertEqual(status, 200)
            status, _ = self.request(f"/tasks/{task['task_id']}/renew", {"claim_token": task["claim_token"]})
            self.assertEqual(status, 200)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.http.identity_caches(), [])
        self.assertEqual(self.http.identity_status()["cached"], 0)
        self.assertEqual(self.store.rows("identity_cache"), [])

    def test_source_revocation_and_outage_refuse_the_next_http_operation(self):
        status, task = self.request("/tasks/claim", {"worker": "worker"})
        self.assertEqual(status, 200)
        self.repo["permissions"]["admin"] = False
        self.assertEqual(self.request(f"/tasks/{task['task_id']}/renew", {"claim_token": task["claim_token"]})[0], 403)
        self.error = ForgeError("private response", status=503)
        status, body = self.request("/admin/settings", {"repair_concurrency": 2})
        self.assertEqual(status, 503)
        self.assertNotIn("private response", json.dumps(body))
        self.assertEqual(self.controller.setting("repair_concurrency"), 1)

    def test_revoked_bearer_is_unauthorized_and_scope_denial_is_forbidden(self):
        for upstream, expected in ((401, 401), (403, 403), (404, 403)):
            with self.subTest(upstream=upstream):
                self.error = ForgeError("private response", status=upstream)
                self.assertEqual(self.request("/tasks/claim", {"worker": "worker"})[0], expected)

    def test_slow_body_is_read_before_native_authority_is_observed(self):
        entered = threading.Event()
        read_body = server.Handler._read_body

        def reading(handler):
            entered.set()
            return read_body(handler)

        connection = http.client.HTTPConnection(*self.http.server_address, timeout=10)
        body = json.dumps({"worker": "worker"}).encode()
        try:
            with patch.object(server.Handler, "_read_body", reading):
                connection.putrequest("POST", "/tasks/claim")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("Authorization", "Bearer token")
                connection.endheaders()
                connection.send(body[:1])
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.calls, [], "no authority was observed ahead of the body")
                self.repo["permissions"]["admin"] = False
                connection.send(body[1:])
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
        finally:
            connection.close()
        self.assertEqual(self.store.rows("claims", {"task_id": self.task_id}), [])

    def test_long_controller_lock_wait_cannot_preserve_an_old_http_grant(self):
        observed = threading.Event()
        authenticate = self.authority.authenticate
        result = []

        def checked(token):
            access = authenticate(token)
            observed.set()
            return access

        def request():
            try:
                result.append(self.request("/tasks/claim", {"worker": "worker"}))
            except Exception as error:
                result.append(error)

        with patch.object(self.authority, "authenticate", side_effect=checked):
            with self.controller._lock:
                thread = threading.Thread(target=request, daemon=True)
                thread.start()
                self.assertTrue(observed.wait(2))
                self.fixture.clock.advance(16)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 503)
        self.assertEqual(self.store.rows("claims", {"task_id": self.task_id}), [])

    def test_native_routes_refuse_another_repository_and_legacy_authority_inputs(self):
        with patch.object(self.controller, "retry_ci", side_effect=AssertionError("wrong repository")):
            self.assertEqual(self.request("/admin/retry-ci", {"repo": "other/source"})[0], 400)
        self.assertEqual(self.request("/webhook/github", {})[0], 404)
        self.assertEqual(self.request("/admin/import-bors", {"observation": {}})[0], 400)
        self.assertEqual(self.request("/admin/reconcile-approvals", {})[0], 503)
        self.assertEqual(self.request("/admin/revoke-identity", {"login": "operator"})[0], 400)

    def test_source_bound_http_refuses_missing_native_authority(self):
        self.controller.source_authority = None
        with self.assertRaisesRegex(ValueError, "fresh native authority"):
            server.create_server(self.controller, self.config)

    def test_native_admin_settings_and_urgent_lane_keep_qualified_journal_actor(self):
        self.assertEqual(self.request("/admin/settings", {"repair_concurrency": 2})[0], 200)
        self.assertEqual(self.controller.setting("repair_concurrency"), 2)
        status, result = self.request("/admin/urgent", {"pr": 17})
        self.assertEqual(status, 200)
        self.assertTrue(result["urgent"])
        records = self.store.rows("journal", {"kind": "urgent"})
        self.assertEqual(loads(records[-1]["detail"])["actor"], f"{self.repository.forge.key}/users/7")

    def test_native_admin_rebuild_refreshes_and_adopts_a_clean_chain(self):
        status, result = self.request("/admin/rebuild-head", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["rebuilt"])
        self.assertEqual(self.store.row("batches", {"id": self.batch["id"]})["version"], self.batch["version"] + 1)
        self.assertEqual(len(self.clients), 2)

    def test_missing_bearer_and_always_refused_routes_do_no_body_or_source_io(self):
        with patch.object(server.Handler, "_read_body", side_effect=AssertionError("unauthenticated body read")):
            self.assertEqual(self.request("/tasks/claim", {}, token=None)[0], 401)
            for path in ("/admin/import-bors", "/admin/revoke-identity", "/webhook/github"):
                self.request(path, {}, token=None)
        self.assertEqual(self.clients, [])

    def test_native_renewal_arriving_before_expiry_survives_the_controller_lock_wait(self):
        self.check_renewal_wait(2)

    def test_renewal_wait_beyond_evidence_lifetime_refreshes_without_losing_the_claim(self):
        self.check_renewal_wait(16)

    def check_renewal_wait(self, wait):
        status, task = self.request("/tasks/claim", {"worker": "worker"})
        self.assertEqual(status, 200)
        self.fixture.clock.advance(899)
        queued, results = threading.Event(), []

        class Renewals(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                queued.set()

        self.controller._renewing = Renewals()

        def renew():
            try:
                results.append(self.request(f"/tasks/{task['task_id']}/renew", {"claim_token": task["claim_token"]}))
            except Exception as error:
                results.append(error)

        thread = threading.Thread(target=renew, daemon=True)
        try:
            with self.controller._lock:
                thread.start()
                self.assertTrue(queued.wait(2))
                self.fixture.clock.advance(wait)
                self.controller._expire_claims(self.fixture.clock())
                claim = self.store.rows("claims", {"task_id": self.task_id})[0]
                self.assertIsNone(claim["released_at"])
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][0], 200)
        self.assertEqual(len(self.clients), 3 if wait > 15 else 2)

    def test_incomplete_body_reaches_its_deadline_without_source_authority_io(self):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=10)
        try:
            with patch.object(server, "SOURCE_BODY_DEADLINE_SECONDS", 0.02):
                connection.request(
                    "POST", "/tasks/claim", b"{", {"Content-Length": "100", "Authorization": "Bearer token"}
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 408)
        finally:
            connection.close()
        self.assertEqual(self.clients, [])


if __name__ == "__main__":
    unittest.main()
