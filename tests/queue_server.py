#!/usr/bin/env python3
"""The queue service's HTTP surface (tauceti_queue.server, tauceti_queue.webhook) on a loopback
port with a fake controller and a fake GitHub port.

Covered: health and status (revision, start time, GitHub and webhook counters), the two feeds with
their repository-id mapping and `batch_id` query, webhook signature verification and delivery
deduplication, routing of comments to command execution and of every event to the controller,
bearer authentication of the worker and admin endpoints with its login cache (persisted across
restarts, revalidated in the background, revocable), the controller
error mapping (403, 404, 400, 500) and the urgent-lane request validation.
"""

import http.client
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

sys.path.insert(0, str(REPO / "tests"))
from queue_feeds import FakeSource  # noqa: E402

from tauceti_queue import feeds, server, webhook  # noqa: E402
from tauceti_queue.github_adapter import PullRequest  # noqa: E402
from tauceti_queue.ports import GitHubUnavailable  # noqa: E402

TAUCETI = "TauCetiProject/TauCeti"
SECRET = b"webhook-secret"
HEAD = "a" * 40


class CodedError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code


class Adoption:
    def __init__(self, **fields):
        self.fields = fields

    def to_dict(self):
        return dict(self.fields)


class FakeController(FakeSource):
    def __init__(self):
        super().__init__()
        self.events = []
        self.calls = []
        self.status_waits = []
        self.fail_with = None

    def status(self, *, wait=None):
        self.status_waits.append(wait)
        return {"queue": {"approved_waiting": 3}, "settings": {"insertion_age_window": 16}}

    def handle_event(self, kind, payload):
        self.events.append((kind, payload.get("action")))

    def approve(self, repo, pr, head_sha, reviewer, priority, evidence):
        self.calls.append(("approve", repo, pr, head_sha, reviewer, priority, evidence["kind"]))

    def unapprove(self, repo, pr, actor=""):
        self.calls.append(("unapprove", repo, pr))

    def cancel(self, repo, pr, actor=""):
        self.calls.append(("cancel", repo, pr))

    def set_priority(self, repo, pr, priority):
        self.calls.append(("set_priority", repo, pr, priority))

    def claim_task(self, worker_identity, github_login):
        self.calls.append(("claim_task", worker_identity, github_login))
        if self.fail_with:
            raise self.fail_with
        return {"task": "repair-2650", "claim_token": "t1"} if worker_identity != "idle" else None

    def renew_task(self, task_id, claim_token):
        self.calls.append(("renew_task", task_id, claim_token))
        if claim_token == "stale":
            raise CodedError("claim-expired", "the claim expired")
        return 1_793_776_000.0

    def submit_task(self, task_id, claim_token, tip_sha, worker_notes=""):
        self.calls.append(("submit_task", task_id, claim_token, tip_sha, worker_notes))
        return Adoption(
            accepted=True,
            reason="adopted",
            task_id=task_id,
            batch_id=2650,
            version=3,
            tip=tip_sha,
            retained_ref="refs/x",
        )

    def release_task(self, task_id, claim_token, reason, error=""):
        self.calls.append(("release_task", task_id, claim_token, reason, error))

    def retry_ci(self, repo, actor):
        self.calls.append(("retry_ci", repo, actor))
        return {"retried": True}

    def rebuild_head(self, repo, actor):
        self.calls.append(("rebuild_head", repo, actor))
        return {"rebuilt": True}

    def report_inference(self, worker_identity, ok, error=""):
        self.calls.append(("report_inference", worker_identity, ok, error))
        return None

    def update_settings(self, github_login, settings):
        self.calls.append(("update_settings", github_login, settings))
        if github_login != "kim-em":
            raise PermissionError("repository admin permission is required")
        return {"effective": settings}

    def mark_urgent(self, repo, pr, actor):
        self.calls.append(("mark_urgent", repo, pr, actor))
        if pr == 404:
            raise LookupError("no such approved PR")
        return {"urgent": pr}

    def import_bors_state(self, observation, candidates=None, *, dry_run=False, release_head=False):
        self.calls.append(("import_bors_state", observation.get("schema"), candidates, dry_run, release_head))
        return {"imported": 3, "present": 0, "refused": 0, "dry_run": dry_run}


class FakeAdapter:
    class client:  # noqa: N801 - mirrors the attribute the server reads
        @staticmethod
        def stats():
            return {"requests": 12, "rate_limit_remaining": 4800}

    def __init__(self):
        self.tokens = {"ghp_worker": ("worker-login", 5), "ghp_kim": ("kim-em", 477956), "ghp_guest": ("guest", 7)}
        self.lookups = 0
        self.admin_checks = 0
        self.comments = []
        # When set, `user_for_token` and `repository_admin` raise `GitHubUnavailable` with this message.
        self.unavailable = None

    def user_for_token(self, token):
        self.lookups += 1
        if self.unavailable:
            raise GitHubUnavailable(self.unavailable)
        return self.tokens.get(token)

    def fetch_pr(self, repo, number):
        return PullRequest(number, HEAD, "main", "t", "", "author", "open", False, (), base_sha="b" * 40)

    def reviewer(self, login, repo=None):
        return login in ("alice", "kim-em")

    def repository_admin(self, login, repo=None):
        self.admin_checks += 1
        if self.unavailable:
            raise GitHubUnavailable(self.unavailable)
        return login == "kim-em"

    def pr_merge_base(self, repo, number, head):
        return "d" * 40

    def post_comment(self, repo, number, body, marker):
        self.comments.append((number, body, marker))
        return 1


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = FakeController()
        cls.adapter = FakeAdapter()
        cls.clock = [1_793_775_999.0]
        config = server.ServerConfig(
            webhook_secret=SECRET,
            repositories={1: TAUCETI},
            adapter=cls.adapter,
            host="127.0.0.1",
            port=0,
            revision="abc123",
            clock=lambda: cls.clock[0],
        )
        cls.server = server.create_server(cls.controller, config)
        cls.port = cls.server.server_address[1]
        server.serve_in_thread(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.controller.events.clear()
        self.controller.calls.clear()
        self.controller.fail_with = None
        self.adapter.comments.clear()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            return response.status, json.loads(raw) if raw else None
        except ValueError:
            return response.status, raw

    def test_health_and_unknown_routes(self):
        self.assertEqual(self.request("GET", "/health/"), (200, b"ok\n"))
        status, body = self.request("GET", "/healthz")
        self.assertEqual((status, body["ok"], body["revision"]), (200, True, "abc123"))
        self.assertEqual(self.controller.status_waits[-1], server.HEALTHZ_LOCK_WAIT_SECONDS, "healthz waits briefly")
        self.request("GET", "/status")
        self.assertIsNone(self.controller.status_waits[-1], "status waits the controller's default")
        status, body = self.request("GET", "/settings")
        self.assertEqual((status, body), (200, {"settings": {"insertion_age_window": 16}, "source": "controller"}))
        self.assertEqual(self.request("GET", "/api/observation?base=main")[1]["schema"], "tauceti-bors.observation/v1")
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/nope", {})[0], 404)

    def test_status(self):
        status, body = self.request("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["queue"], {"approved_waiting": 3})
        self.assertEqual(body["revision"], "abc123")
        self.assertEqual(body["started_at"], "2026-11-04T07:06:39Z")
        self.assertEqual(body["github"], {"requests": 12, "rate_limit_remaining": 4800})
        self.assertEqual(set(body["webhooks"]), {"received", "duplicates", "ignored"})

    def test_frozen_admission_header_only_for_native_observation(self):
        def fetch(path):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", path)
            response = connection.getresponse()
            result = (response.status, response.getheader("TauCeti-Admission"), json.loads(response.read()))
            connection.close()
            return result

        path = "/repositories/1/active-batches?base=main&batch_id=2650"
        status, header, ordinary = fetch(path)
        self.assertEqual(status, 200)
        self.assertIsNone(header)  # compatibility FeedSource makes no frozen claim
        native = feeds.ControllerFeedSource(object())
        with (
            patch.object(feeds, "feed_source", return_value=native),
            patch.object(feeds, "observation_feed", return_value=ordinary),
        ):
            for route in (path, "/api/observation?base=main"):
                status, header, doc = fetch(route)
                self.assertEqual((status, header, doc), (200, "frozen-accepted-head/v1", ordinary))
            with patch.object(feeds, "candidates_feed", return_value={}):
                self.assertIsNone(fetch("/repositories/1/candidates?base=main")[1])
            with patch.object(feeds, "observation_feed", side_effect=ValueError("invalid")):
                status, header, _ = fetch(path)
                self.assertEqual(status, 400)
                self.assertIsNone(header)

    def test_feeds(self):
        schema = feeds.load_schema()
        status, doc = self.request("GET", "/repositories/1/active-batches?base=main")
        self.assertEqual(status, 200)
        self.assertEqual(feeds.validate_observation(doc, schema), [])
        self.assertEqual((doc["repo"], doc["base"], doc["batch_ids"]), (TAUCETI, "main", [2650, 2651]))
        self.assertIsNone(doc["requested_batch"])
        status, doc = self.request("GET", "/repositories/1/active-batches?base=main&batch_id=2650")
        self.assertEqual(doc["requested_batch"]["id"], 2650)
        self.assertEqual(
            self.request("GET", "/repositories/1/active-batches?base=main&batch_id=x")[1]["requested_batch"], None
        )
        self.assertEqual(self.request("GET", "/repositories/2/active-batches?base=main")[0], 404)
        status, doc = self.request("GET", "/repositories/1/candidates?base=main")
        self.assertEqual(status, 200)
        self.assertEqual(feeds.validate_candidates(doc, schema), [])
        self.assertEqual(len(doc["candidates"]), 2)
        status, doc = self.request("GET", "/repositories/1/candidates?base=main&batch_id=2650&promoted=true")
        self.assertEqual((doc["batch_id"], doc["admissions"]), (2650, []))
        status, doc = self.request("GET", "/repositories/1/active-batches")
        self.assertEqual(doc["base"], "main", "the canonical branch is the default base")

    def deliver(self, event, payload, delivery_id="d-1", secret=SECRET, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": webhook.signature_for(secret, body),
        }
        return self.request("POST", "/webhook/github", body, headers)

    def test_webhook_signature(self):
        payload = {"action": "opened", "repository": {"full_name": TAUCETI}}
        self.assertEqual(self.deliver("pull_request", payload, secret=b"wrong")[0], 401)
        self.assertEqual(self.request("POST", "/webhook/github", payload, {"X-GitHub-Event": "pull_request"})[0], 401)
        self.assertEqual(self.deliver("pull_request", None, raw=b"not json")[0], 400)
        self.assertEqual(self.controller.events, [])
        self.assertEqual(self.deliver("pull_request", payload, delivery_id="d-sig"), (202, {"result": "ok"}))
        self.assertEqual(self.controller.events, [("pull_request", "opened")])

    def test_webhook_dedupes_and_filters(self):
        payload = {"action": "completed", "repository": {"full_name": TAUCETI}}
        self.assertEqual(self.deliver("workflow_run", payload, delivery_id="d-dup")[1], {"result": "ok"})
        self.assertEqual(self.deliver("workflow_run", payload, delivery_id="d-dup")[1], {"result": "duplicate"})
        self.assertEqual(len(self.controller.events), 1)
        self.assertEqual(
            self.deliver("workflow_run", {"repository": {"full_name": "other/repo"}}, delivery_id="d-other")[1],
            {"result": "ignored"},
        )
        self.assertEqual(self.deliver("installation", payload, delivery_id="d-inst")[1], {"result": "ignored"})
        self.assertEqual(self.deliver("ping", {"zen": "x"}, delivery_id="d-ping")[1], {"result": "pong"})
        self.assertEqual(len(self.controller.events), 1)

    def test_issue_comment_runs_commands(self):
        payload = {
            "action": "created",
            "repository": {"full_name": TAUCETI},
            "issue": {"number": 42, "pull_request": {"url": "x"}},
            "comment": {
                "id": 1001,
                "body": "bors r+ p=2",
                "created_at": "2026-11-04T07:00:00Z",
                "html_url": "u",
                "user": {"login": "alice", "id": 7},
            },
        }
        self.assertEqual(self.deliver("issue_comment", payload, delivery_id="d-c1")[0], 202)
        self.assertEqual(self.controller.calls, [("approve", TAUCETI, 42, HEAD, "alice", 2, "issue_comment")])
        self.assertEqual(self.controller.events, [("issue_comment", "created")])
        review_app = dict(
            payload,
            comment=dict(
                payload["comment"],
                id=1002,
                body=f"bors r+ sha={HEAD}",
                user={"login": "tauceti-review-bot[bot]", "id": 1},
                performed_via_github_app={"id": webhook.REVIEW_APP_ID},
            ),
        )
        self.deliver("issue_comment", review_app, delivery_id="d-c2")
        self.assertEqual(
            self.controller.calls[-1], ("approve", TAUCETI, 42, HEAD, "tauceti-review-bot[bot]", 0, "review_app")
        )
        stranger = dict(
            payload, comment=dict(payload["comment"], id=1003, body="bors r+", user={"login": "stranger", "id": 8})
        )
        self.deliver("issue_comment", stranger, delivery_id="d-c3")
        self.assertEqual(len(self.controller.calls), 2)
        self.assertEqual(self.adapter.comments[-1][1].split("\n")[0], ":lock: Permission denied")
        not_a_pr = dict(payload, issue={"number": 43})
        self.deliver("issue_comment", not_a_pr, delivery_id="d-c4")
        self.assertEqual(len(self.controller.calls), 2)

    def test_review_submission_runs_commands(self):
        payload = {
            "action": "submitted",
            "repository": {"full_name": TAUCETI},
            "pull_request": {"number": 42},
            "review": {
                "id": 5,
                "body": "bors r-",
                "submitted_at": "2026-11-04T07:00:00Z",
                "html_url": "u",
                "commit_id": HEAD,
                "user": {"login": "alice", "id": 7},
            },
        }
        self.assertEqual(self.deliver("pull_request_review", payload, delivery_id="d-r1")[0], 202)
        self.assertEqual(self.controller.calls, [("unapprove", TAUCETI, 42)])
        self.assertEqual(self.controller.events, [("pull_request_review", "submitted")])

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def test_worker_endpoints_need_a_github_token(self):
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "w1"})[0], 401)
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "w1"}, self.bearer("ghp_bad"))[0], 401)
        self.assertEqual(self.controller.calls, [])
        status, body = self.request("POST", "/tasks/claim", {"worker": "w1"}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (200, {"task": "repair-2650", "claim_token": "t1"}))
        self.assertEqual(self.controller.calls, [("claim_task", "w1", "worker-login")])
        before = self.adapter.lookups
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_worker"))[0], 204)
        self.assertEqual(self.adapter.lookups, before, "the login is cached by token digest")
        status, body = self.request("POST", "/tasks/17/renew", {"claim_token": "t1"}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (200, {"task_id": 17, "expires_at": 1_793_776_000.0}))
        self.assertEqual(self.controller.calls[-1], ("renew_task", 17, "t1"))
        self.assertEqual(self.request("POST", "/tasks/17/renew", {}, self.bearer("ghp_worker"))[0], 400)
        status, body = self.request("POST", "/tasks/17/renew", {"claim_token": "stale"}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (409, {"error": "claim-expired", "detail": "the claim expired"}))
        self.assertEqual(
            self.request(
                "POST", "/tasks/17/result", {"claim_token": "t1", "tip_sha": "short"}, self.bearer("ghp_worker")
            )[0],
            400,
        )
        status, body = self.request(
            "POST",
            "/tasks/17/result",
            {"claim_token": "t1", "tip_sha": "c" * 40, "worker_notes": "n"},
            self.bearer("ghp_worker"),
        )
        self.assertEqual((status, body["accepted"], body["tip"]), (200, True, "c" * 40))
        self.assertEqual(self.controller.calls[-1], ("submit_task", 17, "t1", "c" * 40, "n"))
        self.assertEqual(
            self.request(
                "POST",
                "/tasks/17/release",
                {"claim_token": "t1", "reason": "inference_unavailable", "error": "429"},
                self.bearer("ghp_worker"),
            )[0],
            204,
        )
        self.assertEqual(self.controller.calls[-1], ("release_task", 17, "t1", "inference_unavailable", "429"))
        self.assertEqual(
            self.request(
                "POST", "/tasks/inference", {"worker": "w1", "ok": False, "error": "credits"}, self.bearer("ghp_worker")
            )[0],
            204,
        )
        self.assertEqual(self.controller.calls[-1], ("report_inference", "w1", False, "credits"))
        self.assertEqual(self.request("POST", "/tasks/claim", b"[1]", self.bearer("ghp_worker"))[0], 400)

    def test_controller_errors_map_to_statuses(self):
        self.controller.fail_with = ValueError("worker identity missing")
        self.assertEqual(
            self.request("POST", "/tasks/claim", {}, self.bearer("ghp_worker")),
            (400, {"error": "worker identity missing"}),
        )
        self.controller.fail_with = RuntimeError("boom")
        self.assertEqual(
            self.request("POST", "/tasks/claim", {}, self.bearer("ghp_worker")), (500, {"error": "internal error"})
        )

    def github_outage(self):
        """Six hours on (every cached answer expired) and GitHub rate limited; restored after the test."""
        start, tokens = self.clock[0], dict(self.adapter.tokens)

        def restore():
            self.clock[0] = start
            self.adapter.tokens = tokens
            self.adapter.unavailable = None
            for cache in self.server.identity_caches():
                cache.reset_backoff()

        self.addCleanup(restore)
        self.clock[0] += 7 * 3600
        self.adapter.unavailable = "rate limited for 2400s"

    def test_stale_identity_is_served_while_github_cannot_answer(self):
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_worker"))[0], 204)
        self.github_outage()
        before = self.adapter.lookups
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_worker"))[0], 204)
        self.assertEqual(self.adapter.lookups, before + 1, "the expired entry is refreshed, and kept when that fails")
        status, body = self.request("POST", "/tasks/17/renew", {"claim_token": "t1"}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (200, {"task_id": 17, "expires_at": 1_793_776_000.0}))
        self.assertEqual(self.adapter.lookups, before + 1, "the renewal did not wait on GitHub (backing off)")
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_never"))[0], 401)
        status, body = self.request("GET", "/status")
        self.assertTrue(body["identities"]["github_lookups_rate_limited"])
        self.assertEqual(body["identities"]["github_lookups_retry_at"], feeds._iso(self.clock[0] + 2400))
        # GitHub answers again (after the reset) and rejects the token: evicted, so the next outage
        # has nothing to serve.
        self.clock[0] += 2400
        self.adapter.unavailable = None
        del self.adapter.tokens["ghp_worker"]
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_worker"))[0], 401)
        self.adapter.unavailable = "rate limited for 2400s"
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_worker"))[0], 401)

    def test_admin_answers_are_cached_and_survive_an_outage(self):
        self.assertEqual(self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_kim"))[0], 200)
        self.assertEqual(self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_worker"))[0], 403)
        self.assertEqual(self.request("POST", "/tasks/claim", {"worker": "idle"}, self.bearer("ghp_guest"))[0], 204)
        before = self.adapter.admin_checks
        self.assertEqual(self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_kim"))[0], 200)
        self.assertEqual(self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_worker"))[0], 403)
        self.assertEqual(self.adapter.admin_checks, before, "both answers are cached per login")
        self.github_outage()
        status, body = self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_kim"))
        self.assertEqual((status, body), (200, {"retried": True}), "the stale admin answer stands")
        self.assertEqual(self.controller.calls[-1], ("retry_ci", TAUCETI, "kim-em"))
        self.assertEqual(self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_worker"))[0], 403)
        status, body = self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_guest"))
        self.assertEqual(status, 503, "a login never checked before is neither granted nor refused")
        self.assertIn("could not confirm", body["error"])

    def test_admin_settings_accepts_every_controller_setting_and_refuses_unknown_keys_first(self):
        from tauceti_queue.controller import DEFAULT_SETTINGS

        applied = []
        self.controller.effective_settings = lambda: dict(DEFAULT_SETTINGS)
        self.controller.set_setting = lambda key, value, actor: applied.append((key, value, actor))
        body = {"lost_run_seconds": 14400, "publication_enabled": True, "membership_label": "bors-staging"}
        status, _ = self.request("POST", "/admin/settings", body, self.bearer("ghp_kim"))
        self.assertEqual(status, 200)
        self.assertEqual(applied, [(key, value, "kim-em") for key, value in body.items()])
        applied.clear()
        status, reply = self.request(
            "POST", "/admin/settings", {"lost_run_seconds": 1, "batch_cap": 4}, self.bearer("ghp_kim")
        )
        self.assertEqual(status, 400)
        self.assertIn("batch_cap", reply["error"])
        self.assertEqual(applied, [], "nothing is applied when any key is unknown")

    def test_admin_endpoints(self):
        self.assertEqual(self.request("POST", "/admin/settings", {"insertion_age_window": 8})[0], 401)
        status, body = self.request("POST", "/admin/settings", {"insertion_age_window": 8}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (403, {"error": "worker-login is not a repository admin"}))
        self.assertEqual(
            [c for c in self.controller.calls if c[0] == "update_settings"],
            [],
            "the predicate runs before the controller",
        )
        status, body = self.request("POST", "/admin/settings", {"insertion_age_window": 8}, self.bearer("ghp_kim"))
        self.assertEqual((status, body), (200, {"effective": {"insertion_age_window": 8}}))
        status, body = self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_kim"))
        self.assertEqual((status, body), (200, {"retried": True}))
        self.assertEqual(self.controller.calls[-1], ("retry_ci", TAUCETI, "kim-em"))
        status, body = self.request("POST", "/admin/retry-ci", {}, self.bearer("ghp_kim"))
        self.assertEqual((status, body), (200, {"retried": True}))
        self.assertEqual(self.controller.calls[-1], ("retry_ci", TAUCETI, "kim-em"))
        self.assertEqual(
            self.request("POST", "/admin/urgent", {"repo": "other/repo", "pr": 1}, self.bearer("ghp_kim"))[0], 400
        )
        self.assertEqual(
            self.request("POST", "/admin/urgent", {"repo": TAUCETI, "pr": "1"}, self.bearer("ghp_kim"))[0], 400
        )
        self.assertEqual(
            self.request("POST", "/admin/urgent", {"repo": TAUCETI, "pr": 404}, self.bearer("ghp_kim"))[0], 404
        )
        status, body = self.request("POST", "/admin/urgent", {"repo": TAUCETI, "pr": 42}, self.bearer("ghp_kim"))
        self.assertEqual((status, body), (200, {"urgent": 42}))
        self.assertEqual(self.controller.calls[-1], ("mark_urgent", TAUCETI, 42, "kim-em"))

        status, body = self.request("POST", "/admin/rebuild-head", {}, self.bearer("ghp_kim"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.controller.calls[-1], ("rebuild_head", TAUCETI, "kim-em"))

    def test_import_bors_and_reconcile_approvals_endpoints(self):
        feed = {"schema": "tauceti-bors.observation/v1", "repo": TAUCETI, "batches": []}
        self.assertEqual(self.request("POST", "/admin/import-bors", {"observation": feed})[0], 401)
        status, body = self.request("POST", "/admin/import-bors", {"observation": feed}, self.bearer("ghp_worker"))
        self.assertEqual((status, body), (403, {"error": "worker-login is not a repository admin"}))
        self.assertEqual(
            self.request("POST", "/admin/import-bors", {"observation": []}, self.bearer("ghp_kim"))[0], 400
        )
        self.assertEqual(
            self.request(
                "POST", "/admin/import-bors", {"observation": feed, "candidates": "x"}, self.bearer("ghp_kim")
            )[0],
            400,
        )
        status, body = self.request(
            "POST",
            "/admin/import-bors",
            {"observation": feed, "candidates": [{"admissions": []}], "dry_run": True},
            self.bearer("ghp_kim"),
        )
        self.assertEqual((status, body), (200, {"imported": 3, "present": 0, "refused": 0, "dry_run": True}))
        self.assertEqual(
            self.controller.calls[-1],
            ("import_bors_state", "tauceti-bors.observation/v1", [{"admissions": []}], True, False),
        )
        self.assertEqual(self.request("POST", "/admin/reconcile-approvals", {})[0], 401)
        status, body = self.request("POST", "/admin/reconcile-approvals", {}, self.bearer("ghp_kim"))
        self.assertEqual(status, 503, "no poller is attached to this server")

        class StubPoller:
            def run_once(self, *, reason):
                return {"reason": reason, "listed": 2, "approved": 1}

        self.server.poller = StubPoller()
        try:
            status, body = self.request("POST", "/admin/reconcile-approvals", {}, self.bearer("ghp_kim"))
            self.assertEqual((status, body), (200, {"reason": "operator kim-em", "listed": 2, "approved": 1}))
        finally:
            self.server.poller = None

    def test_primary_reconciliation_wakes_intake_without_network_work_in_http_request(self):
        class PrimaryPoller:
            def __init__(self):
                self.state = self
                self.requested = False

            def summary(self):
                return {"pending": 12}

            def request_discovery(self):
                self.requested = True

            def run_once(self, **kwargs):
                raise AssertionError("HTTP request must not run policy/source reads")

        poller = self.server.poller = PrimaryPoller()
        try:
            status, body = self.request("POST", "/admin/reconcile-approvals", {}, self.bearer("ghp_kim"))
            self.assertEqual((status, body), (200, {"requested": True, "pending": 12}))
            self.assertTrue(poller.requested)
        finally:
            self.server.poller = None


class PersistentIdentities(unittest.TestCase):
    """Bearer identities and admin answers kept in the store across a restart, served while GitHub's
    `/user` lookup is rate limited, revalidated in the background, revocable by an admin."""

    def setUp(self):
        import tempfile

        from tauceti_queue.store import Store

        self.Store = Store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "queue.sqlite")
        self.adapter = FakeAdapter()
        self.clock = [1_793_775_999.0]
        self.controller = FakeController()

    def start(self):
        """A server over the database at `self.path`, as one start of the service."""
        store = self.Store(self.path)
        self.addCleanup(store.close)
        config = server.ServerConfig(
            webhook_secret=SECRET,
            repositories={1: TAUCETI},
            adapter=self.adapter,
            port=0,
            revision="abc123",
            clock=lambda: self.clock[0],
            store=store,
        )
        srv = server.create_server(self.controller, config)
        server.serve_in_thread(srv)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, store

    def request(self, srv, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def claim(self, srv, token="ghp_worker"):
        return self.request(srv, "POST", "/tasks/claim", {"worker": "idle"}, token)[0]

    def test_a_restart_while_github_is_rate_limited_keeps_workers_and_admins(self):
        first, store = self.start()
        self.assertEqual(self.claim(first), 204)
        self.assertEqual(self.request(first, "POST", "/admin/retry-ci", {}, "ghp_kim")[0], 200)
        rows = store.cached_identities("bearer")
        self.assertEqual(sorted(row["value"][0] for row in rows), ["kim-em", "worker-login"])
        self.assertNotIn("ghp_worker", json.dumps(rows), "the token itself is never stored")
        self.assertEqual(store.cached_identities("server-admin")[0]["key"], "kim-em")
        first.shutdown()
        # The service restarts seven hours on with the operator's quota exhausted.
        self.clock[0] += 7 * 3600
        self.adapter.unavailable = "GET /user: rate limited for 184s"
        second, _ = self.start()
        with self.assertLogs("tauceti_queue.server", "WARNING") as logged:
            for _ in range(3):
                self.assertEqual(self.claim(second), 204, "authenticated with the stored identity, never 401")
            status, body = self.request(second, "POST", "/tasks/9/renew", {"claim_token": "t"}, "ghp_worker")
            self.assertEqual(status, 200)
            self.assertEqual(self.request(second, "POST", "/admin/retry-ci", {}, "ghp_kim")[0], 200)
        serving = [line for line in logged.output if "serving the answer" in line and "bearer" in line]
        self.assertEqual(len(serving), 2, "logged once per token")
        self.assertEqual(self.adapter.lookups, 3, "two lookups before the restart, one after; then backing off")
        self.assertEqual(self.claim(second, "ghp_guest"), 401, "a token never seen is not admitted")
        status, document = self.request(second, "GET", "/status")
        identities = document["identities"]
        self.assertEqual((identities["cached"], identities["admin_answers_cached"]), (2, 1))
        self.assertTrue(identities["persisted"])
        self.assertTrue(identities["github_lookups_rate_limited"])
        self.assertIn("rate limited for 184s", identities["last_lookup_error"])
        # An hour on the identity is still served, and the stale answer is logged again once.
        self.clock[0] += 3600
        with self.assertLogs("tauceti_queue.server", "WARNING") as logged:
            self.assertEqual(self.claim(second), 204)
            self.assertEqual(self.claim(second), 204)
        self.assertEqual(len([line for line in logged.output if "serving the answer" in line]), 1)

    def test_stored_identities_expire_after_the_maximum_age(self):
        first, store = self.start()
        self.assertEqual(self.claim(first), 204)
        first.shutdown()
        self.clock[0] += 8 * 86400
        self.adapter.unavailable = "GET /user: rate limited for 184s"
        second, _ = self.start()
        self.assertEqual(store.cached_identities("bearer"), [], "dropped at load")
        self.assertEqual(self.claim(second), 401)

    def test_background_revalidation(self):
        srv, store = self.start()
        self.assertEqual(self.claim(srv), 204)
        self.assertEqual(srv.revalidate_identities(), 0, "nothing is due within the lifetime")
        self.clock[0] += 7 * 3600
        self.assertEqual(srv.revalidate_identities(), 1)
        self.assertEqual(store.cached_identities("bearer")[0]["verified_at"], self.clock[0])
        self.assertEqual(self.adapter.lookups, 2)
        # Rate limited: the pass stops at the first failure and the next waits for the reset.
        self.clock[0] += 7 * 3600
        self.adapter.unavailable = "GET /user: rate limited for 184s"
        self.assertEqual(srv.revalidate_identities(), 0)
        self.assertEqual(srv.revalidate_identities(), 0)
        self.assertEqual(self.adapter.lookups, 3)
        # After the reset GitHub answers that the token was revoked: the entry goes, so a later
        # outage cannot serve it.
        self.clock[0] += 200
        self.adapter.unavailable = None
        del self.adapter.tokens["ghp_worker"]
        self.assertEqual(srv.revalidate_identities(), 1)
        self.assertEqual(store.cached_identities("bearer"), [])
        self.adapter.unavailable = "GET /user: rate limited for 184s"
        self.assertEqual(self.claim(srv), 401)

    def test_an_admin_can_revoke_a_cached_identity(self):
        srv, store = self.start()
        self.assertEqual(self.claim(srv), 204)
        self.assertEqual(self.request(srv, "POST", "/admin/retry-ci", {}, "ghp_worker")[0], 403)
        status, body = self.request(srv, "POST", "/admin/revoke-identity", {"login": "worker-login"}, "ghp_worker")
        self.assertEqual(status, 403, "only an admin revokes")
        self.assertEqual(self.request(srv, "POST", "/admin/revoke-identity", {}, "ghp_kim")[0], 400)
        status, body = self.request(srv, "POST", "/admin/revoke-identity", {"login": "worker-login"}, "ghp_kim")
        self.assertEqual(
            (status, body), (200, {"login": "worker-login", "tokens_forgotten": 1, "admin_answers_forgotten": 1})
        )
        self.assertEqual([row["value"][0] for row in store.cached_identities("bearer")], ["kim-em"])
        self.adapter.unavailable = "GET /user: rate limited for 184s"
        self.assertEqual(self.claim(srv), 401, "a revoked identity is not served through an outage")


class ServiceConfigForm(unittest.TestCase):
    """`create_server(controller, Config, status=..., adapter=..., app=...)`, the entrypoint's call."""

    def test_config_form(self):
        import tempfile

        from tauceti_queue import config as cfg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "github-webhook-secret").write_bytes(SECRET + b"\n")
            config = cfg.Config(credentials_dir=root, listen_host="127.0.0.1", listen_port=0)
            controller = FakeController()
            adapter = FakeAdapter()

            def provider():
                return {"schema": "tauceti-queue.status/v1", "service": {"revision": "deadbeef"}, "settings": {}}

            srv = server.create_server(controller, config, status=provider, adapter=adapter)
            server.serve_in_thread(srv)
            try:
                port = srv.server_address[1]
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", "/status")
                document = json.loads(connection.getresponse().read())
                self.assertEqual(document["service"]["revision"], "deadbeef")
                self.assertIn("webhooks", document)
                connection.request("GET", "/healthz")
                self.assertEqual(json.loads(connection.getresponse().read())["service"]["revision"], "deadbeef")
                body = json.dumps({"action": "opened", "repository": {"full_name": TAUCETI}}).encode()
                connection.request(
                    "POST",
                    "/webhook/github",
                    body,
                    {
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "cf-1",
                        "X-Hub-Signature-256": webhook.signature_for(SECRET, body),
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(
                    connection.getresponse().status, 202, "the secret is read from the credentials directory"
                )
                connection.close()
            finally:
                srv.shutdown()
                srv.server_close()
            with self.assertRaises(ValueError):
                server.create_server(object(), config, status=provider)


class Signature(unittest.TestCase):
    def test_verify(self):
        body = b'{"zen": "x"}'
        self.assertTrue(webhook.verify_signature(SECRET, body, webhook.signature_for(SECRET, body)))
        self.assertFalse(webhook.verify_signature(SECRET, body, webhook.signature_for(b"other", body)))
        self.assertFalse(webhook.verify_signature(SECRET, body, None))
        self.assertFalse(webhook.verify_signature(SECRET, body, "sha1=abc"))
        self.assertFalse(webhook.verify_signature(SECRET, body + b" ", webhook.signature_for(SECRET, body)))


if __name__ == "__main__":
    unittest.main()
