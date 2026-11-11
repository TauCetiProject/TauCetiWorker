#!/usr/bin/env python3
"""GitHub execution adapter boundaries, single sends and ownership evidence."""

import json
import socket
import sys
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_requests import policy
from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable

from tauceti_queue.ci_execution import ExecutionEvidence, GitHubSourceExecution
from tauceti_queue.github_app import AppCredentials, CredentialsError, InstallationToken, InstallationTokenSource
from tauceti_queue.ports import DispatchNotAccepted


class Reader:
    def __init__(self, frozen):
        self.frozen = frozen
        self.metadata = {"id": 42, "full_name": "team/Execution", "default_branch": "main"}
        self.runs = {}
        self.receipts = {}
        self.results = {}
        self.pages = [{"total_count": 0, "workflow_runs": []}]

    def repository(self):
        return self.metadata

    def get(self, path):
        if path.startswith("/branches/"):
            return {"commit": {"sha": self.frozen.request.workflow_revision}}
        if "/actions/workflows/" in path:
            return self.pages[int(path.rsplit("page=", 1)[1]) - 1]
        return self.runs[path.rsplit("/", 1)[1]]

    def receipt(self, request, *, run_id, observed_attempt):
        value = self.receipts.get(str(run_id))
        if value is None:
            raise EvidenceUnavailable("missing receipt")
        return value

    def result(self, request, *, run_id, attempt):
        value = self.results.get(str(run_id))
        if value is None:
            raise EvidenceUnavailable("missing current result")
        return value

    def add(self, run_id="101", *, attempt=1, status="in_progress"):
        req, config = self.frozen.request, self.frozen.policy
        self.runs[run_id] = dict(
            id=int(run_id),
            run_attempt=attempt,
            status=status,
            conclusion="success" if status == "completed" else None,
            head_sha=req.workflow_revision,
            event="repository_dispatch",
            path=config.workflow,
            repository={"id": 42, "full_name": "team/Execution"},
            referenced_workflows=[
                {
                    "path": "team/Tools/.github/workflows/pr-build.yml@" + config.harness_revision,
                    "sha": config.harness_revision,
                }
            ],
            display_title="source request=" + req.request_id,
        )
        proof = dict(
            kind="request",
            run_id=int(run_id),
            request_digest=req.digest,
            receipt_attempt=1,
            observed_attempt=attempt,
            artifact_id=201,
            artifact_digest="sha256:" + "a" * 64,
        )
        self.receipts[run_id] = proof
        return proof


class Execution(unittest.TestCase):
    def setUp(self):
        config = policy()
        request = cc.CandidateRequest(
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
        self.frozen = SimpleNamespace(policy=config, request=request)
        self.reader = Reader(self.frozen)
        credentials = AppCredentials(1, "test", 2, b"unused", b"unused")
        self.tokens = InstallationTokenSource(
            credentials,
            lambda *_: self.fail("unexpected mint"),
            repository_id=42,
            permissions={"contents": "write", "actions": "write"},
        )
        self.tokens._current = InstallationToken("fixture-only", 10**12)
        self.posts = []
        self.status = 204

        def transport(*args):
            self.posts.append(args)
            if isinstance(self.status, Exception):
                raise self.status
            return self.status, {}, b""

        self.adapter = GitHubSourceExecution(
            self.tokens, transport=transport, reader_factory=lambda *_: self.reader, max_pages=2
        )

    def test_canonical_dispatch_and_no_mutation_retry_after_lost_response(self):
        self.assertEqual(self.adapter.dispatch(self.frozen), self.frozen.request.request_id)
        method, url, headers, body = self.posts[0]
        self.assertEqual((method, url), ("POST", "https://api.github.com/repos/team/Execution/dispatches"))
        self.assertEqual(json.loads(body), self.frozen.request.dispatch())
        self.assertEqual(headers["Authorization"], "Bearer fixture-only")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.status = OSError("lost response")
        with self.assertRaises(OSError):
            self.adapter.dispatch(self.frozen)
        self.assertEqual(len(self.posts), 2)  # exactly one POST per explicit call

    def test_preflight_rejects_default_branch_caller_and_wrong_resource_credential(self):
        self.reader.metadata["default_branch"] = "other"
        with self.assertRaises(DispatchNotAccepted) as caught:
            self.adapter.dispatch(self.frozen)
        self.assertFalse(caught.exception.retryable)
        self.reader.metadata["default_branch"] = "main"
        with patch.object(self.reader, "get", return_value={"commit": {"sha": "a" * 40}}):
            with self.assertRaises(DispatchNotAccepted):
                self.adapter.dispatch(self.frozen)
        self.tokens._repository_id = 99
        with self.assertRaises(DispatchNotAccepted):
            self.adapter.dispatch(self.frozen)
        self.assertEqual(self.posts, [])
        self.tokens._repository_id = None
        with self.assertRaises(ValueError):
            GitHubSourceExecution(self.tokens)

    def test_http_rejections_are_nonsends_but_5xx_is_uncertain(self):
        for status in (400, 401, 403, 404, 410, 422, 429):
            self.tokens._current = InstallationToken("fixture-only", 10**12)
            self.status = status
            with self.subTest(status=status), self.assertRaises(DispatchNotAccepted):
                self.adapter.dispatch(self.frozen)
        self.status = 500
        with self.assertRaises(OSError):
            self.adapter.dispatch(self.frozen)
        self.assertEqual(len(self.posts), 8)

    def test_discovery_requires_evidence_not_matching_title_and_rejects_truncated_pages(self):
        self.reader.add()
        self.reader.receipts.clear()
        self.reader.pages = [{"total_count": 1, "workflow_runs": [self.reader.runs["101"]]}]
        observations, unavailable = self.adapter.discover(self.frozen, since=100)
        self.assertEqual(observations, [])
        self.assertTrue(unavailable)
        self.reader.add()
        observations, unavailable = self.adapter.discover(self.frozen, since=100)
        self.assertEqual([obs.run.run_id for obs in observations], ["101"])
        self.assertFalse(unavailable)
        self.reader.runs["101"]["repository"]["id"] = 99
        observations, _ = self.adapter.discover(self.frozen, since=100)
        self.assertEqual(observations, [])
        self.reader.pages = [{"total_count": 2, "workflow_runs": []}]
        with self.assertRaises(EvidenceUnavailable):
            self.adapter.discover(self.frozen, since=100)

    def test_owned_rerun_needs_current_result_and_cancel_rechecks_live_metadata(self):
        proof = self.reader.add(attempt=2, status="completed")
        self.reader.receipts.clear()  # recorded ownership survives receipt expiry
        observation = self.adapter.observe(self.frozen, "101", owned=True)
        self.assertIsNone(observation.result)
        self.assertEqual(observation.run.attempt, 2)
        self.assertEqual(self.adapter.cancel(self.frozen, "101", proof=proof), "already-completed")
        self.assertEqual(self.posts, [])
        self.reader.runs["101"]["status"] = "in_progress"
        self.reader.runs["101"]["conclusion"] = None
        self.status = 202
        self.assertEqual(self.adapter.cancel(self.frozen, "101", proof=proof), "cancelled")
        self.reader.runs["101"]["head_sha"] = "c" * 40
        with self.assertRaises(ValueError):
            self.adapter.cancel(self.frozen, "101", proof=proof)
        with self.assertRaises(ValueError):
            self.adapter.cancel(self.frozen, "101", proof=dict(proof, request_digest="0" * 64))
        self.assertEqual(len(self.posts), 1)

    def test_repository_reader_rejects_name_reuse_and_malformed_api_metadata(self):
        # Exercise the real bounded metadata validator, independent of our reader fake.
        evidence = ExecutionEvidence(self.frozen.policy, token="fixture-only", token_origin=cc.GITHUB)
        for raw, exception in (
            (b"[]", ValueError),
            (b"{", EvidenceUnavailable),
            (b'{"id":99,"full_name":"team/Execution"}', ValueError),
            (b'{"id":42,"full_name":"team/Renamed"}', ValueError),
        ):
            with self.subTest(raw=raw), patch.object(evidence, "_read", return_value=raw):
                with self.assertRaises(exception):
                    evidence.repository()
        with patch.object(evidence, "_read", return_value=json.dumps(self.reader.metadata).encode()):
            self.assertEqual(evidence.repository()["id"], 42)

    def test_rate_limit_deadlines_are_finite_and_bad_credentials_are_invalidated(self):
        with patch.object(self.adapter, "_post", return_value=(401, {}, b"")):
            with self.assertRaises(DispatchNotAccepted) as caught:
                self.adapter.dispatch(self.frozen)
        self.assertTrue(caught.exception.retryable)  # Minted App credentials can refresh.
        self.assertIsNone(self.tokens._current)
        for value, expected in (("NaN", None), ("inf", None), ("-1", None), ("2000", 2000)):
            self.tokens._current = InstallationToken("fixture-only", 10**12)
            with patch.object(self.adapter, "_post", return_value=(429, {"x-ratelimit-reset": value}, b"")):
                with self.assertRaises(DispatchNotAccepted) as caught:
                    self.adapter.dispatch(self.frozen)
                self.assertEqual(caught.exception.retry_at, expected)

    def test_token_is_prepared_once_and_definite_connect_failure_is_a_nonsend(self):
        with patch.object(self.tokens, "token", side_effect=["fixture-only", RuntimeError("late refresh")]) as token:
            self.adapter.dispatch(self.frozen)
            self.assertEqual(token.call_count, 1)
        failure = urllib.error.URLError(socket.gaierror("DNS failed"))
        with patch.object(self.adapter, "_post", side_effect=failure):
            with self.assertRaises(DispatchNotAccepted) as caught:
                self.adapter.dispatch(self.frozen)
            self.assertTrue(caught.exception.retryable)
        with patch.object(self.adapter, "_post", return_value=(403, {"x-ratelimit-reset": "2000"}, b"")):
            with self.assertRaises(DispatchNotAccepted) as caught:
                self.adapter.dispatch(self.frozen)
            self.assertFalse(caught.exception.retryable)
            self.assertIsNone(caught.exception.retry_at)

    def test_expired_known_run_checks_status_without_downloading_artifacts(self):
        self.reader.add(status="completed")
        self.reader.pages = [{"total_count": 1, "workflow_runs": [self.reader.runs["101"]]}]
        with (
            patch.object(self.reader, "receipt", side_effect=AssertionError("expired receipt")),
            patch.object(self.reader, "result", side_effect=AssertionError("expired result")),
        ):
            observations, unavailable = self.adapter.discover(self.frozen, since=100, known_run_ids={"101"})
        self.assertFalse(unavailable)
        self.assertEqual(observations[0].run.status, "completed")
        self.assertIsNone(observations[0].ownership)
        self.assertIsNone(observations[0].result)

    def test_bad_result_does_not_discard_valid_ownership_receipt(self):
        receipt = self.reader.add(status="completed")
        self.reader.pages = [{"total_count": 1, "workflow_runs": [self.reader.runs["101"]]}]
        with patch.object(self.reader, "result", side_effect=ValueError("tested another candidate")):
            observations, unavailable = self.adapter.discover(self.frozen, since=100)
        self.assertFalse(unavailable)
        self.assertEqual(observations[0].ownership, receipt)
        self.assertIsNone(observations[0].result)

    def test_permanent_scope_or_configuration_error_is_not_retried(self):
        with patch.object(self.tokens, "token", side_effect=CredentialsError("wrong repository scope")):
            with self.assertRaises(DispatchNotAccepted) as caught:
                self.adapter.preflight(self.frozen)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.posts, [])

    def test_prepared_dispatch_performs_only_one_post_without_preflight_io(self):
        prepared = self.adapter.preflight(self.frozen)
        self.assertNotIn("fixture-only", repr(prepared))
        with (
            patch.object(self.tokens, "token", side_effect=AssertionError("token read after claim")),
            patch.object(self.reader, "repository", side_effect=AssertionError("repository read after claim")),
        ):
            self.assertEqual(self.adapter.dispatch(self.frozen, prepared=prepared), self.frozen.request.request_id)
        self.assertEqual(len(self.posts), 1)

    def test_discovery_uses_closed_creation_window(self):
        with patch.object(self.reader, "get", wraps=self.reader.get) as get:
            self.adapter.discover(self.frozen, since=100)
        path = get.call_args[0][0]
        self.assertIn("created=1970-01-01T00%3A00%3A00Z..", path)
        self.assertNotIn("%3E%3D", path)

    def test_large_filtered_listing_splits_windows_and_deduplicates_boundary_runs(self):
        self.adapter.max_pages = 10
        self.adapter.clock = lambda: 201
        rows = [{"id": number, "second": 50 if number <= 750 else 150} for number in range(1, 1501)]
        rows[0]["second"] = 100  # Included by both inclusive child windows.
        calls = []

        def listing(path):
            query = parse_qs(urlsplit(path).query)
            start, end = query["created"][0].split("..")
            lo, hi = [datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() for value in (start, end)]
            selected = [row for row in rows if lo <= row["second"] <= hi]
            page = int(query["page"][0])
            calls.append((lo, hi, page))
            return {"total_count": len(selected), "workflow_runs": selected[(page - 1) * 100 : page * 100]}

        with patch.object(self.reader, "get", side_effect=listing):
            found = self.adapter._list_runs(self.reader, self.frozen, since=100)
        self.assertEqual({row["id"] for row in found}, set(range(1, 1501)))
        self.assertEqual(len(found), 1500)
        self.assertGreater(len(calls), 10)
        self.assertLessEqual(len(calls), 64)
        self.assertTrue(all(end <= 200 for _start, end, _page in calls))

    def test_overfull_single_second_listing_is_unavailable_without_unbounded_pagination(self):
        self.adapter.max_pages = 10
        self.adapter.clock = lambda: 201
        with patch.object(self.reader, "get", return_value={"total_count": 1500, "workflow_runs": []}) as get:
            with self.assertRaisesRegex(EvidenceUnavailable, "time precision bound"):
                self.adapter._list_runs(self.reader, self.frozen, since=100)
        self.assertLessEqual(get.call_count, 10)

    def test_only_complete_confirmed_absence_can_start_missing_result_recovery(self):
        self.reader.add(status="completed")
        raw = self.reader.runs["101"]
        raw["conclusion"] = "failure"
        run = self.adapter._run(raw)
        original = self.reader.get
        expected = cc.artifact_name(self.frozen.request, 101, 1)

        def get(path):
            if "/artifacts?" in path:
                return {"total_count": 0, "artifacts": []}
            return original(path)

        with patch.object(self.reader, "get", side_effect=get):
            self.assertTrue(self.adapter._result_absent(self.reader, self.frozen, run))
        with patch.object(self.reader, "get", side_effect=EvidenceUnavailable("artifact API outage")):
            self.assertFalse(self.adapter._result_absent(self.reader, self.frozen, run))
        for value in (
            {"total_count": 1, "artifacts": [{"id": 201, "name": expected, "expired": True}]},
            {"total_count": 2, "artifacts": []},
            {"total_count": False, "artifacts": []},
        ):
            with patch.object(self.reader, "get", return_value=value):
                self.assertFalse(self.adapter._result_absent(self.reader, self.frozen, run))

    def test_absence_is_refused_if_attempt_or_conclusion_changes_during_confirmation(self):
        self.reader.add(status="completed")
        original = self.reader.runs["101"]
        run = self.adapter._run(original)
        for change in ({"run_attempt": 2}, {"conclusion": "failure"}):

            def get(path, change=change):
                if "/artifacts?" in path:
                    return {"total_count": 0, "artifacts": []}
                return dict(original, **change)

            with patch.object(self.reader, "get", side_effect=get):
                self.assertFalse(self.adapter._result_absent(self.reader, self.frozen, run))

    def test_download_outage_with_present_result_does_not_prove_absence(self):
        self.reader.add(status="completed")
        self.reader.runs["101"]["conclusion"] = "failure"
        original = self.reader.get
        expected = cc.artifact_name(self.frozen.request, 101, 1)

        def get(path):
            if "/artifacts?" in path:
                return {"total_count": 1, "artifacts": [{"id": 201, "name": expected, "expired": False}]}
            return original(path)

        with (
            patch.object(self.reader, "get", side_effect=get),
            patch.object(self.reader, "result", side_effect=EvidenceUnavailable("signed download unavailable")),
        ):
            observation = self.adapter.observe(self.frozen, "101", owned=True)
        self.assertIsNone(observation.result)
        self.assertFalse(observation.result_absent)


if __name__ == "__main__":
    unittest.main()
