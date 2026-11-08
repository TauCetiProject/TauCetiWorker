#!/usr/bin/env python3
"""Trusted workflow credentials: resource scope, live launch and single sends."""

import copy
import io
import json
import sys
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_execution import Reader
from queue_ci_requests import policy
from tauceti_tools import ci_candidate as cc

from tauceti_queue.ci_execution import ExecutionEvidence, GitHubSourceExecution
from tauceti_queue.github_app import CredentialsError
from tauceti_queue.ports import DispatchNotAccepted
from tauceti_queue.workflow_token import (
    MAX_LIFETIME_SECONDS,
    MAX_RESPONSE_BYTES,
    TrustedWorkflowJob,
    WorkflowTokenSource,
)


class WorkflowCredentials(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.job = TrustedWorkflowJob(
            policy().execution,
            ".github/workflows/controller.yml",
            "b" * 40,
            "main",
            "controller",
            101,
            2,
            "refs/heads/main",
        )
        self.repository = {"id": 42, "full_name": "team/Execution"}
        self.scope = {"total_count": 1, "repositories": [self.repository]}
        self.run = {
            "id": 101,
            "run_attempt": 2,
            "repository": self.repository,
            "head_repository": self.repository,
            "head_sha": self.job.revision,
            "head_branch": "main",
            "path": self.job.workflow,
            "status": "in_progress",
            "event": "workflow_dispatch",
        }
        self.jobs = {"total_count": 1, "jobs": [{"name": "controller", "status": "in_progress"}]}
        self.calls = []
        self.status = 200
        self.headers = {}

        def transport(method, url, headers, body):
            self.calls.append((method, url, headers, body))
            value = self.scope if "/installation/" in url else self.jobs if "/jobs?" in url else self.run
            return self.status, self.headers, json.dumps(value).encode()

        self.source = WorkflowTokenSource(
            self.job, "fixture-native-job-token", transport=transport, clock=lambda: self.now
        )

    def fresh_source(self):
        return WorkflowTokenSource(
            self.job, "fixture-native-job-token", transport=self.source._transport, clock=lambda: self.now
        )

    def test_launch_is_validated_once_without_recurring_identity_polling(self):
        self.assertEqual(self.source.token(), "fixture-native-job-token")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.source.token(), "fixture-native-job-token")
        self.assertEqual(len(self.calls), 3)
        for method, url, headers, body in self.calls:
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith("https://api.github.com/"))
            self.assertEqual(headers["Authorization"], "Bearer fixture-native-job-token")
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertIsNone(body)
        self.now += 3600
        self.source.token()
        self.assertEqual(len(self.calls), 3)
        permissions = self.source.permissions
        permissions["actions"] = "read"
        self.assertEqual(self.source.permissions, {"contents": "write", "actions": "write"})

    def test_scope_refuses_broader_tokens_name_reuse_and_incomplete_observations(self):
        for scope in (
            {"total_count": True, "repositories": [self.repository]},
            {"total_count": 2, "repositories": [self.repository, {"id": 43, "full_name": "team/Other"}]},
            {"total_count": 1, "repositories": []},
            {"total_count": 1, "repositories": [{"id": 99, "full_name": "team/Execution"}]},
            {"total_count": 1, "repositories": [{"id": 42, "full_name": "team/Reused"}]},
        ):
            with self.subTest(scope=scope), self.assertRaises(CredentialsError):
                self.scope = scope
                self.fresh_source().token()
        self.scope = {"total_count": 1, "repositories": [self.repository]}
        self.headers = {"Link": '<https://api.github.com/installation/repositories?page=2>; rel="next"'}
        with self.assertRaises(CredentialsError):
            self.fresh_source().token()

    def test_run_identity_revision_attempt_path_fork_and_event_are_bound(self):
        original = copy.deepcopy(self.run)
        for field, bad in (
            ("id", True),
            ("id", 102),
            ("run_attempt", True),
            ("run_attempt", 1),
            ("repository", {"id": 99, "full_name": "team/Execution"}),
            ("head_repository", {"id": 43, "full_name": "attacker/Fork"}),
            ("head_sha", "c" * 40),
            ("head_branch", "unapproved"),
            ("path", ".github/workflows/other.yml"),
            ("status", "completed"),
            ("event", "pull_request_target"),
            ("event", "repository_dispatch"),
        ):
            with self.subTest(field=field, bad=bad), self.assertRaises(CredentialsError):
                self.run = {**original, field: bad}
                self.fresh_source().token()
        self.run = original
        self.assertEqual(self.source.token(), "fixture-native-job-token")

    def test_approved_push_launch_and_rate_limited_reads(self):
        self.run["event"] = "push"
        self.assertEqual(self.source.token(), "fixture-native-job-token")
        for headers in ({"X-RateLimit-Remaining": "0"}, {"Retry-After": "30"}):
            self.status, self.headers = 403, headers
            source = WorkflowTokenSource(self.job, "fixture", transport=self.source._transport, clock=lambda: self.now)
            with self.assertRaises(CredentialsError) as caught:
                source.token()
            self.assertTrue(caught.exception.retryable)

    def test_job_must_be_uniquely_running_and_listing_complete(self):
        for jobs in (
            {"total_count": True, "jobs": []},
            {"total_count": 2, "jobs": self.jobs["jobs"]},
            {"total_count": 1, "jobs": [None]},
            {"total_count": 1, "jobs": [{"name": "controller", "status": "completed"}]},
            {"total_count": 1, "jobs": [{"name": "attacker", "status": "in_progress"}]},
            {"total_count": 2, "jobs": self.jobs["jobs"] * 2},
            {"total_count": 101, "jobs": self.jobs["jobs"] * 101},
        ):
            with self.subTest(jobs=jobs), self.assertRaises(CredentialsError):
                self.jobs = jobs
                self.fresh_source().token()

    def test_invalidated_expired_or_backwards_clock_credentials_cannot_be_refreshed(self):
        for now in (99, 100 + MAX_LIFETIME_SECONDS):
            source = WorkflowTokenSource(
                self.job, "fixture", transport=lambda *_: self.fail("must not read"), clock=lambda: self.now
            )
            self.now = now
            with self.assertRaises(CredentialsError):
                source.token()
            self.now = 100
            with self.assertRaises(CredentialsError):
                source.token()
        self.source.invalidate()
        with self.assertRaises(CredentialsError):
            self.source.token()
        self.assertIsNone(self.source._value)
        self.assertEqual(self.calls, [])

    def test_http_refusals_are_not_retried_and_invalid_credentials_stay_disabled(self):
        for status, retryable in ((302, False), (401, False), (403, False), (429, True), (503, True)):
            with self.subTest(status=status):
                self.status = status
                source = WorkflowTokenSource(
                    self.job, "fixture", transport=self.source._transport, clock=lambda: self.now
                )
                before = len(self.calls)
                with self.assertRaises(CredentialsError) as caught:
                    source.token()
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(len(self.calls), before + 1)
                if status == 401:
                    with self.assertRaises(CredentialsError):
                        source.token()
                    self.assertEqual(len(self.calls), before + 1)

    def test_default_http_reader_keeps_redirects_and_response_bytes_bounded(self):
        failure = urllib.error.HTTPError(
            "https://api.github.com/installation/repositories", 302, "redirect", {}, io.BytesIO(b"private-body")
        )
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
        with patch("tauceti_queue.workflow_token.urllib.request.build_opener", return_value=opener) as build:
            source = WorkflowTokenSource(self.job, "fixture", clock=lambda: self.now)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[-1].redirect_request(None, None, 302, "moved", {}, "https://foreign.invalid"))
        with self.assertRaisesRegex(CredentialsError, "HTTP 302"):
            source.token()

        read_sizes = []

        class Response(io.BytesIO):
            code = 200
            headers = {}

            def read(self, size=-1):
                read_sizes.append(size)
                return super().read(size)

        opener.open = lambda *_args, **_kwargs: Response(b"x" * (MAX_RESPONSE_BYTES + 50))
        with patch("tauceti_queue.workflow_token.urllib.request.build_opener", return_value=opener):
            source = WorkflowTokenSource(self.job, "fixture", clock=lambda: self.now)
        with self.assertRaisesRegex(CredentialsError, "malformed"):
            source.token()
        self.assertEqual(read_sizes, [MAX_RESPONSE_BYTES + 1])

    def test_slow_first_observation_cannot_extend_instance_lifetime(self):
        original = self.source._transport

        def slow(*args):
            value = original(*args)
            self.now += 10
            return value

        self.source._transport = slow
        self.now = 100 + MAX_LIFETIME_SECONDS - 1
        with self.assertRaisesRegex(CredentialsError, "expired during observation"):
            self.source.token()
        with self.assertRaises(CredentialsError):
            self.source.token()

    def test_json_bytes_nesting_and_duplicate_fields_are_bounded(self):
        for body in (b'{"total_count":1,"total_count":2}', b"x" * (MAX_RESPONSE_BYTES + 1), b"[" * 2000, b"NaN"):
            source = WorkflowTokenSource(
                self.job, "fixture", transport=lambda *_, raw=body: (200, {}, raw), clock=lambda: self.now
            )
            with self.subTest(size=len(body)), self.assertRaises(CredentialsError):
                source.token()

    def test_binding_requires_positive_exact_ids_and_approved_configuration(self):
        for field, value in (
            ("run_id", True),
            ("attempt", 0),
            ("workflow", "../other.yml"),
            ("revision", "0" * 40),
            ("branch", "../unapproved"),
            ("ref", "refs/tags/main"),
            ("job_name", "controller (untrusted matrix)"),
        ):
            with self.subTest(field=field), self.assertRaises(CredentialsError):
                replace(self.job, **{field: value})
        for token in ("", "secret\nheader", None):
            with self.subTest(token=token), self.assertRaises(CredentialsError):
                WorkflowTokenSource(self.job, token)

    def frozen(self):
        config = policy()
        req = cc.CandidateRequest(
            "1" * 32,
            "2" * 32,
            config.digest,
            config.source,
            config.execution,
            "b" * 40,
            config.harness_revision,
            config.config_digest,
            5,
            7,
            "c" * 40,
            "d" * 40,
            "e" * 40,
            (cc.Member(17, "f" * 40),),
        )
        return SimpleNamespace(policy=config, request=req)

    def test_existing_adapter_single_send_uses_verified_native_job_credential(self):
        frozen = self.frozen()
        reader = Reader(frozen)
        posts = []

        def send(*args):
            posts.append(args)
            raise OSError("accepted dispatch response lost")

        adapter = GitHubSourceExecution(self.source, transport=send, reader_factory=lambda *_: reader)
        with self.assertRaises(OSError):
            adapter.dispatch(frozen)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["Authorization"], "Bearer fixture-native-job-token")
        self.assertEqual(json.loads(posts[0][3]), frozen.request.dispatch())
        self.source.invalidate()
        with self.assertRaises(DispatchNotAccepted) as caught:
            adapter.dispatch(frozen)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(posts), 1)

    def test_frozen_caller_and_branch_are_independent_preflight_anchors(self):
        frozen = self.frozen()
        for job in (
            replace(self.job, revision="c" * 40),
            replace(self.job, branch="unapproved", ref="refs/heads/unapproved"),
        ):
            source = WorkflowTokenSource(
                job, "fixture", transport=lambda *_: self.fail("no reads before launch check"), clock=lambda: self.now
            )
            adapter = GitHubSourceExecution(
                source, transport=lambda *_: self.fail("no dispatch"), reader_factory=lambda *_: self.fail("no reader")
            )
            with self.assertRaises(DispatchNotAccepted) as caught:
                adapter.dispatch(frozen)
            self.assertFalse(caught.exception.retryable)

    def test_dispatch_401_is_permanent_for_fixed_job_credential(self):
        frozen = self.frozen()
        posts = []

        def refused(*args):
            posts.append(args)
            return 401, {}, b""

        adapter = GitHubSourceExecution(self.source, transport=refused, reader_factory=lambda *_: Reader(frozen))
        for _ in range(2):
            with self.assertRaises(DispatchNotAccepted) as caught:
                adapter.dispatch(frozen)
            self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(posts), 1)
        self.assertIsNone(self.source._value)

    def test_real_execution_metadata_reader_accepts_the_verified_job_source(self):
        frozen = self.frozen()
        reads = []

        def read(_evidence, request, _limit):
            reads.append(request)
            value = (
                {"commit": {"sha": self.job.revision}}
                if "/branches/" in request.full_url
                else {**self.repository, "default_branch": "main"}
            )
            return json.dumps(value).encode()

        adapter = GitHubSourceExecution(self.source, transport=lambda *_: (204, {}, b""))
        with patch.object(ExecutionEvidence, "_read", read):
            self.assertEqual(adapter.dispatch(frozen), frozen.request.request_id)
        self.assertEqual(len(reads), 2)
        self.assertTrue(
            all(request.get_header("Authorization") == "Bearer fixture-native-job-token" for request in reads)
        )

    def test_approved_new_controller_can_observe_and_cancel_an_archived_older_request(self):
        frozen = self.frozen()
        job = replace(self.job, revision="c" * 40)
        self.run["head_sha"] = job.revision
        source = WorkflowTokenSource(job, "fixture", transport=self.source._transport, clock=lambda: self.now)
        reader = Reader(frozen)
        proof = reader.add("202")
        posts = []

        def cancel(*args):
            posts.append(args)
            return 202, {}, b""

        adapter = GitHubSourceExecution(source, transport=cancel, reader_factory=lambda *_: reader)
        self.assertEqual(adapter.status(frozen, "202").run_id, "202")
        self.assertEqual(posts, [])
        self.assertEqual(adapter.cancel(frozen, "202", proof=proof), "cancelled")
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith("/actions/runs/202/cancel"))
        with self.assertRaises(DispatchNotAccepted) as caught:
            adapter.preflight(frozen)
        self.assertFalse(caught.exception.retryable)

    def test_equivalent_workflow_path_forms_are_bound_to_the_approved_branch(self):
        for path in (self.job.workflow, self.job.workflow + "@main", self.job.workflow + "@refs/heads/main"):
            self.run["path"] = path
            self.assertEqual(self.fresh_source().token(), "fixture-native-job-token")
        self.run["path"] = self.job.workflow + "@unapproved"
        source = self.fresh_source()
        for _ in range(2):
            with self.assertRaises(CredentialsError):
                source.token()
        self.assertEqual(len(self.calls), 11)  # 3 successful triples + one scope/run refusal.

    def test_jobs_pagination_is_refused_before_returning_any_credential(self):
        original = self.source._transport

        def paginated(*args):
            status, headers, body = original(*args)
            if "/jobs?" in args[1]:
                headers = {"link": '<https://api.github.com/jobs?page=2>; rel="next"'}
            return status, headers, body

        source = WorkflowTokenSource(self.job, "fixture", transport=paginated, clock=lambda: self.now)
        for _ in range(2):
            with self.assertRaises(CredentialsError):
                source.token()
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
