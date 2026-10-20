#!/usr/bin/env python3
"""Shared-state outages are typed dependency waits, never no-work rounds or GitHub fallback scans."""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity, cli, work_units
from tauceti_worker import github_state as state
from tauceti_worker import github_transport as transport
from tauceti_worker.config import Die, NoProgress, SharedStateUnavailable
from tauceti_worker.constants import EX_GITHUB_WAIT
from tauceti_worker.github_state import SurveyRows
from tauceti_worker.review_state import ReviewState
from tauceti_worker.work_units import RoundOpts, run_round
from tauceti_worker.worker_manager import _worker_status_lines

SECRET = "client-secret-value"


class Response(io.BytesIO):
    def __init__(self, payload, etag='"1"'):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def page(data, *, revision=1, cursor=None, **extra):
    value = {
        "version": 1,
        "repository": "org/repo",
        "operation": "issue_comments",
        "status": "ok",
        "complete": True,
        "revision": revision,
        "observed_at": 10,
        "verified_at": 11,
        "data": data,
        **extra,
    }
    if cursor:
        value["cursor"] = cursor
    return value


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://state.test/v1/query", code, "error", headers or {}, io.BytesIO(body))


def fails(code, body=b"", headers=None):
    """A request side effect raising a fresh HTTP error (its body is readable once per attempt)."""

    def request(*_a, **_k):
        raise http_error(code, body, headers)

    return request


class StateClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "token").write_text(SECRET)
        env = patch.dict(
            os.environ,
            {
                "TAUCETI_GITHUB_SHARED_DIR": str(self.root),
                "TAUCETI_GITHUB_STATE_URL": "https://state.test",
                "TAUCETI_GITHUB_STATE_TOKEN_FILE": str(self.root / "token"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def fail_with(self, side_effect, number=7):
        with patch.object(state.urllib.request, "urlopen", side_effect=side_effect):
            self.assertEqual(state.read("org/repo", "issue_comments", number=number), (False, None))
        failure = state.last_failure()
        self.assertIsNotNone(failure)
        self.assertEqual(failure["dependency"], "github-state")
        return failure

    def circuit(self):
        (path,) = (self.root / "gateway").glob("*/circuits/*.json")
        return json.loads(path.read_text())

    def test_injected_failures_have_structured_sanitized_reasons(self):
        cases = [
            (fails(503, b'{"error":"Exceeded the maximum database size."}'), "http_503"),
            (fails(429, b"slow down", {"Retry-After": "120"}), "http_429"),
            (fails(401, b"Bearer " + SECRET.encode()), "client_auth_configuration"),
            (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
            (TimeoutError("header read timed out"), "timeout"),
            (http.client.RemoteDisconnected("closed"), "connection"),
            (ConnectionResetError("reset"), "connection"),
            (lambda *_a, **_k: Response(b"{not json"), "invalid_response"),
            (lambda *_a, **_k: Response({**page([]), "version": 2}), "incompatible_protocol"),
        ]
        for i, (error, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                failure = self.fail_with(error, number=100 + i)
                self.assertEqual(failure["reason"], reason)
                self.assertNotIn(SECRET, json.dumps(failure))
                self.assertLessEqual(len(failure["detail"]), 200)
                self.assertEqual(failure["actionable"], reason == "client_auth_configuration")
                for circuit in (self.root / "gateway").glob("*/circuits/*.json"):
                    circuit.unlink()  # Each case starts with a closed circuit.
        self.assertNotIn(SECRET, (self.root / "requests.jsonl").read_text())
        failure = self.fail_with(fails(503, b'{"error":"Exceeded the maximum database size."}'))
        self.assertEqual((failure["status"], failure["detail"]), (503, "Exceeded the maximum database size."))
        failure = self.fail_with(fails(429, b"", {"Retry-After": "120"}), number=2)
        self.assertGreaterEqual(failure["retry_at"], time.time() + 100)

    def test_missing_client_token_is_actionable_configuration(self):
        (self.root / "token").write_text("")
        failure = self.fail_with(AssertionError("no request may be sent"))
        self.assertEqual((failure["reason"], failure["actionable"]), ("client_auth_configuration", True))

    def test_coverage_partial_pagination_and_mixed_revisions_never_cache_or_empty(self):
        failure = self.fail_with(lambda *_a, **_k: Response({**page([]), "status": "unavailable", "complete": False}))
        self.assertEqual(failure["reason"], "coverage_unavailable")
        self.assertGreater(failure["retry_at"], time.time())

        def partial(request, **_):
            if "cursor=" in request.full_url:
                return Response({**page([]), "status": "unavailable", "complete": False, "reason": "backfill"})
            return Response(page([{"id": "1"}], cursor="p1"))

        self.assertEqual(self.fail_with(partial, number=8)["reason"], "coverage_unavailable")

        def mixed(request, **_):
            if "cursor=" in request.full_url:
                return Response(page([{"id": "2"}], revision=2))
            return Response(page([{"id": "1"}], cursor="p1"))

        self.assertEqual(self.fail_with(mixed, number=9)["reason"], "mixed_revision")
        self.assertEqual(list((self.root / "gateway").glob("*/responses/*.json")), [])

    def test_complete_cache_keeps_provenance_and_binding(self):
        with patch.object(state.urllib.request, "urlopen", return_value=Response(page([{"id": "5"}]))):
            self.assertEqual(state.read("org/repo", "issue_comments", number=7), (True, [{"id": 5}]))
        self.assertIsNone(state.last_failure())
        (path,) = (self.root / "gateway").glob("*/responses/*.json")
        cached = json.loads(path.read_text())
        self.assertEqual(cached["query"]["args"], {"number": 7})
        self.assertEqual(
            {k: cached["payload"][k] for k in ("revision", "complete", "observed_at", "verified_at")},
            {"revision": 1, "complete": True, "observed_at": 10, "verified_at": 11},
        )
        failure = self.fail_with(fails(503))
        self.assertEqual(failure["cached"]["revision"], 1)
        self.assertEqual(failure["cached"]["verified_at"], 11)
        # A cached response bound to another query never answers a conditional read.
        path.write_text(json.dumps({**cached, "query": {**cached["query"], "args": {"number": 8}}}))
        with patch.object(state.urllib.request, "urlopen", return_value=Response(page([{"id": "6"}]))) as fetch:
            self.assertTrue(state.read("org/repo", "issue_comments", number=7)[0])
        self.assertIsNone(fetch.call_args.args[0].get_header("If-none-match"))

    def test_local_cache_io_failure_does_not_hide_a_healthy_service(self):
        with (
            patch.object(state.urllib.request, "urlopen", return_value=Response(page([{"id": "5"}]))),
            patch.object(transport, "atomic", side_effect=OSError("disk full")),
            patch.object(transport, "locked", side_effect=OSError("lock unavailable")),
        ):
            self.assertEqual(state.read("org/repo", "issue_comments", number=7), (True, [{"id": 5}]))

    def test_open_circuit_preserves_cause_and_one_process_probes_recovery(self):
        for _ in range(3):
            self.fail_with(fails(503, b"Exceeded the maximum database size."))
        circuit = self.circuit()
        self.assertEqual((circuit["failures"], circuit["reason"]), (3, "http_503"))
        self.assertTrue(time.time() + 20 <= circuit["until"] <= time.time() + 40)
        failure = self.fail_with(AssertionError("an open circuit sends no request"))
        self.assertEqual((failure["circuit"], failure["reason"]), ("open", "http_503"))
        self.assertEqual(failure["retry_at"], circuit["until"])

        # Half-open: concurrent local readers share one probe lease.
        (path,) = (self.root / "gateway").glob("*/circuits/*.json")
        path.write_text(json.dumps({**circuit, "until": 0}))
        callers, release = set(), threading.Event()

        def slow_outage(*_a, **_k):
            callers.add(threading.get_ident())
            release.wait(2)
            raise http_error(503)

        results = []
        with patch.object(state.urllib.request, "urlopen", side_effect=slow_outage):
            threads = [
                threading.Thread(target=lambda: results.append(state.read("org/repo", "issue_comments", number=7)))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            time.sleep(0.3)
            release.set()
            for thread in threads:
                thread.join()
        self.assertEqual(len(callers), 1)
        self.assertEqual(results, [(False, None)] * 6)
        self.assertEqual(self.circuit()["failures"], 4)

        # Recovery after the open interval resumes without any operator action.
        path.write_text(json.dumps({**self.circuit(), "until": 0, "probe_until": 0}))
        with patch.object(state.urllib.request, "urlopen", return_value=Response(page([]))):
            self.assertEqual(state.read("org/repo", "issue_comments", number=7), (True, []))
        self.assertEqual(self.circuit(), {"failures": 0, "until": 0})

    def test_sanitizer_removes_credentials_other_than_the_client_token(self):
        body = (
            b'{"error":"Authorization: Bearer ghp_FAKEfake0123456789abcdefABCDEF '
            b'Basic dXNlcjpwYXNzd29yZA== token=abc123 password: hunter2 Exceeded the maximum database size."}'
        )
        failure = self.fail_with(fails(503, body))
        for secret in ("ghp_FAKE", "dXNlcjpw", "abc123", "hunter2"):
            self.assertNotIn(secret, failure["detail"])
            self.assertNotIn(secret, (self.root / "requests.jsonl").read_text())
            self.assertNotIn(secret, json.dumps(self.circuit()))
        self.assertIn("Exceeded the maximum database size.", failure["detail"])

    def test_retry_after_is_finite_and_capped(self):
        for value in ("inf", "1e12", "-5", "nan"):
            with self.subTest(value=value):
                for _ in range(3):
                    failure = self.fail_with(fails(429, b"", {"Retry-After": value}))
                until = self.circuit()["until"]
                self.assertTrue(time.time() < until <= time.time() + 360, until)
                self.assertTrue(time.time() < failure["retry_at"] <= time.time() + 360)
                for circuit in (self.root / "gateway").glob("*/circuits/*.json"):
                    circuit.unlink()

    def test_unavailable_inventory_shares_a_cooldown(self):
        unavailable = {
            **page([]),
            "operation": "survey",
            "status": "unavailable",
            "complete": False,
            "reason": "backfill",
        }
        with patch.object(state.urllib.request, "urlopen", return_value=Response(unavailable)) as fetch:
            for _ in range(4):
                self.assertEqual(state.read("org/repo", "survey", roadmap="any"), (False, None))
                self.assertEqual(state.last_failure()["reason"], "coverage_unavailable")
        self.assertEqual(fetch.call_count, 1)
        self.assertGreater(state.last_failure()["retry_at"], time.time() + 20)

    def test_repository_scope_is_operator_actionable(self):
        unavailable = {**page([]), "status": "unavailable", "complete": False, "reason": "repository_unavailable"}
        failure = self.fail_with(lambda *_a, **_k: Response(unavailable))
        self.assertEqual((failure["reason"], failure["actionable"]), ("client_auth_configuration", True))

    def test_failed_refresh_preserves_the_last_complete_response(self):
        with patch.object(state.urllib.request, "urlopen", return_value=Response(page([{"id": "5"}]))):
            self.assertTrue(state.read("org/repo", "issue_comments", number=7)[0])
        (path,) = (self.root / "gateway").glob("*/responses/*.json")
        before = path.read_text()

        def changed_mid_read(request, **_):
            if "cursor=" in request.full_url:
                return Response(page([{"id": "7"}], revision=3), etag='"3"')
            return Response(page([{"id": "6"}], revision=2, cursor="p1"), etag='"2"')

        self.assertEqual(self.fail_with(changed_mid_read)["reason"], "mixed_revision")
        self.assertEqual(path.read_text(), before)

    def test_probe_lease_is_renewed_across_a_long_paginated_read(self):
        for _ in range(3):
            self.fail_with(fails(503))
        (path,) = (self.root / "gateway").glob("*/circuits/*.json")
        path.write_text(json.dumps({**self.circuit(), "until": 0}))
        peer = []

        def slow_pages(request, **_):
            if peer:
                self.fail("a peer probed while the lease holder was still reading")
            time.sleep(0.3)
            if "cursor=" not in request.full_url:
                return Response(page([{"id": "1"}], cursor="p1"))
            # Past the initial lease, but within the renewed one.
            peer.append(True)
            outcome = state.read("org/repo", "issue_comments", number=8)
            peer.append(state.last_failure())
            self.assertEqual(outcome, (False, None))
            return Response(page([{"id": "2"}]))

        with (
            patch.object(state, "PROBE_LEASE", 0.5),
            patch.object(state.urllib.request, "urlopen", side_effect=slow_pages),
        ):
            self.assertEqual(state.read("org/repo", "issue_comments", number=7), (True, [{"id": 1}, {"id": 2}]))
        self.assertEqual((peer[1]["circuit"], peer[1]["reason"]), ("open", "http_503"))
        self.assertEqual(self.circuit(), {"failures": 0, "until": 0})

    def test_stale_success_does_not_erase_a_newer_outage(self):
        def outage_during_request(*_a, **_k):
            for _ in range(3):
                with patch.object(state.urllib.request, "urlopen", side_effect=fails(503)):
                    state.read("org/repo", "issue_comments", number=8)
            return Response(page([]))

        with patch.object(state.urllib.request, "urlopen", side_effect=outage_during_request):
            self.assertTrue(state.read("org/repo", "issue_comments", number=7)[0])
        self.assertEqual(self.circuit()["failures"], 3)
        self.assertGreater(self.circuit()["until"], time.time())

    def test_backoff_is_bounded_and_jittered(self):
        values = {round(state._backoff(n), 3) for n in range(3, 30) for _ in range(3)}
        self.assertTrue(all(24 <= value <= 360 for value in values))
        self.assertGreater(len(values), 10)


HEAD = "a" * 40


def row(number):
    return {
        "pr": {
            "number": number,
            "author": {"login": "science"},
            "headRefOid": HEAD,
            "headRefName": "roadmap/work",
            "headRepositoryOwner": {"login": "science"},
            "headRepository": {"name": "TauCeti"},
            "labels": [{"name": "roadmap/Other"}],
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [],
        },
        "review": {
            "meta": None,
            "markers": [],
            "rebase_heads": [],
            "contest": None,
            "issue_complete": True,
            "review_complete": True,
        },
    }


class RoundBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = SimpleNamespace(wid="test", sbcache=root / "sb", store_dir=root / "review", state=root)

        def forbidden(*_a, **_k):
            self.fail("an outage started a direct GitHub read")

        gh = SimpleNamespace(
            repo="org/repo",
            open_prs=forbidden,
            issue_comments=forbidden,
            review_comments=forbidden,
            fresh_claim_age=forbidden,
            rebase_requested=forbidden,
        )
        self.w = SimpleNamespace(
            cfg=cfg,
            gh=gh,
            claims=SimpleNamespace(),
            rs=ReviewState(cfg, gh),
            counters=SimpleNamespace(read=lambda _key: 0, write=lambda *_: self.fail("allowance charged")),
        )
        self.opts = RoundOpts(
            only=["rebase", "fix-ci", "fix", "review", "roadmap"],
            agent="claude",
            work_model="claude-opus-5-5",
            sandbox_host=True,
            dry_run=False,
        )
        for item in (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://state.test", "TAUCETI_ROADMAP_SKIP": ""}),
            patch.object(work_units, "roadmap_only", return_value=None),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch.object(work_units, "mirror_creds"),
            patch.object(work_units, "_branch_claimed_elsewhere", return_value=False),
        ):
            item.start()
            self.addCleanup(item.stop)

    def outage(self, failure):
        def unavailable(*_a, **_k):
            state._last_failure.set(failure)
            return False, None

        return unavailable

    def test_outage_is_a_typed_wait_without_authoring_or_fallback(self):
        failure = {
            "dependency": "github-state",
            "reason": "http_503",
            "detail": "Exceeded the maximum database size.",
            "retry_at": 2000000000,
            "circuit": "open",
            "actionable": False,
        }
        with (
            patch("tauceti_worker.github_state.read_survey", side_effect=self.outage(failure)),
            patch.object(work_units, "roadmap_areas") as areas,
            patch.object(work_units, "dispatch") as dispatch,
            self.assertRaises(SharedStateUnavailable) as raised,
        ):
            run_round(self.w, self.opts)
        areas.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual((raised.exception.reason, raised.exception.retry_at), ("http_503", 2000000000))
        self.assertIn("Exceeded the maximum database size", str(raised.exception))

    def test_authentication_failure_is_operator_actionable(self):
        failure = {"dependency": "github-state", "reason": "client_auth_configuration", "actionable": True}
        with (
            patch("tauceti_worker.github_state.read_survey", side_effect=self.outage(failure)),
            self.assertRaisesRegex(Die, "operator action required"),
        ):
            run_round(self.w, self.opts)

    def test_malformed_service_url_is_operator_actionable(self):
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "http://state.test"}),
            self.assertRaisesRegex(Die, "operator action required"),
        ):
            run_round(self.w, self.opts)

    def test_genuine_work_failures_remain_failures(self):
        with (
            patch("tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows([row(10)], {}))),
            patch.object(work_units, "dispatch", side_effect=NoProgress("model failed")),
            self.assertRaises(NoProgress) as raised,
        ):
            run_round(self.w, self.opts)
        self.assertNotIsInstance(raised.exception, SharedStateUnavailable)


class PreparedStatus(unittest.TestCase):
    def test_cli_reports_dependency_wait_and_activity_excludes_it(self):
        error = SharedStateUnavailable(
            "global maintenance cache unavailable",
            {"dependency": "github-state", "reason": "http_503", "detail": "db full", "retry_at": 2000000000},
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TAUCETI_RUNTIME_STATUS": tmp + "/status.json"}),
            patch.object(cli, "main", side_effect=error),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(cli, "report_failure") as failure,
        ):
            self.assertEqual(cli.cli_main(), EX_GITHUB_WAIT)
            failure.assert_not_called()
            status = json.loads(Path(tmp + "/status.json").read_text())
        self.assertEqual(status["state"], "waiting-github")
        self.assertEqual(
            (status["wait_dependency"], status["wait_reason"], status["wait_detail"], status["next_action_at"]),
            ("github-state", "http_503", "db full", 2000000000),
        )
        events = []
        with patch.object(activity, "emit", side_effect=lambda kind, **f: events.append((kind, f))):
            activity.Round("w").finish(error)
        self.assertEqual(events[-1][0], "round_waited")
        self.assertEqual((events[-1][1]["dependency"], events[-1][1]["reason"]), ("github-state", "http_503"))

    def test_worker_status_names_the_dependency_from_prepared_data(self):
        item = {
            "id": "w",
            "actual": "waiting-github",
            "desired": "running",
            "detail": "global maintenance cache unavailable",
            "wait_dependency": "github-state",
            "wait_reason": "http_503",
            "wait_detail": "db full",
            "next_action_at": time.time() + 30,
            "spec": None,
        }
        with patch.object(state.urllib.request, "urlopen", side_effect=AssertionError("status made a request")):
            text = "\n".join(_worker_status_lines(Path("/nonexistent"), [item], True, width=120))
        self.assertIn("github-state: http_503 (db full)", text)


if __name__ == "__main__":
    unittest.main()
