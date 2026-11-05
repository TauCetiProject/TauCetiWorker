#!/usr/bin/env python3
"""The queue's GitHub App authentication and adapter (tauceti_queue.github_app,
tauceti_queue.github_adapter) over a fake transport with recorded responses.

Covered: the RS256 App JWT (claims and a signature the public key verifies), installation token
caching and refresh ahead of expiry, the client's rate-limit bookkeeping and secondary-limit retry,
token invalidation on 401, pagination, and every `GitHubPort` method: pull request reads, the
staging dispatch payload, candidate run lookup by run-name with phases from the run's jobs and the
ambiguity rule, the `build` status with its run binding, fast-forward promotion with a moved
parent, retained-ref updates, idempotent marked comments, labels, permission checks, bearer-token
identity, the rate limit, the token-free `git fetch` argv, and the polling fallback's bounds.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import github_adapter as ga  # noqa: E402
from tauceti_queue import github_app as app  # noqa: E402
from tauceti_queue import ports  # noqa: E402

TAUCETI = "TauCetiProject/TauCeti"
HEAD = "a" * 40
BASE = "b" * 40
TIP = "c" * 40


def key_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return pem, private.public_key()


class FakeTransport:
    """Recorded responses keyed by `METHOD url` (query included); a list is consumed in order."""

    def __init__(self):
        self.responses = {}
        self.calls = []

    def add(self, method, url, status, body=None, headers=None):
        self.responses.setdefault(f"{method} {url}", []).append((status, headers or {}, body))

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        key = f"{method} {url}"
        queue = self.responses.get(key)
        if not queue:
            raise AssertionError(f"unexpected request {key}\nrecorded: {sorted(self.responses)}")
        status, response_headers, response_body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response_body, bytes | type(None)):
            raw = response_body or b""
        else:
            raw = json.dumps(response_body).encode()
        return status, dict(response_headers), raw


def credentials(pem=b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"):
    return app.AppCredentials(
        app_id=5607007, slug="tauceti-queue", installation_id=178451644, private_key_pem=pem, webhook_secret=b"s"
    )


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class AppAuth(unittest.TestCase):
    def test_jwt_claims_and_signature(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        pem, public = key_pair()
        token = app.app_jwt(5607007, pem, now=1_800_000_000)
        header, payload, signature = token.split(".")
        claims = app.decode_jwt_claims(token)
        self.assertEqual(claims, {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "5607007"})
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        import base64

        public.verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            f"{header}.{payload}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_load_credentials(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(app.CredentialsError):
                app.AppCredentials.load(root)
            (root / "github-app.json").write_text(
                json.dumps({"id": 5607007, "slug": "tauceti-queue", "installation_id": 178451644})
            )
            (root / "github-private-key.pem").write_bytes(
                b"-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n"
            )
            (root / "github-webhook-secret").write_text("hunter2\n")
            loaded = app.AppCredentials.load(root)
            self.assertEqual(
                (loaded.app_id, loaded.slug, loaded.installation_id, loaded.webhook_secret),
                (5607007, "tauceti-queue", 178451644, b"hunter2"),
            )
            self.assertNotIn("PRIVATE", repr(loaded), "the key and secret stay out of reprs")
            os.environ["TAUCETI_QUEUE_CONFIG"] = tmp
            try:
                self.assertEqual(app.config_dir(), root)
            finally:
                del os.environ["TAUCETI_QUEUE_CONFIG"]

    def test_installation_token_refreshes_before_expiry(self):
        pem, _ = key_pair()
        clock = Clock()
        transport = FakeTransport()
        url = "https://api.github.com/app/installations/178451644/access_tokens"
        transport.add(
            "POST",
            url,
            201,
            {"token": "ghs_one", "expires_at": "2027-01-15T10:00:00Z", "permissions": {"contents": "write"}},
        )
        transport.add("POST", url, 201, {"token": "ghs_two", "expires_at": "2027-01-15T11:00:00Z"})
        source = app.InstallationTokenSource(credentials(pem), transport, clock=clock)
        clock.now = app.parse_github_time("2027-01-15T09:00:00Z")
        self.assertEqual(source.token(), "ghs_one")
        self.assertEqual(source.token(), "ghs_one", "cached while fresh")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(
            transport.calls[0][2]["Authorization"].startswith("Bearer ey"), "the JWT authenticates the request"
        )
        clock.now = app.parse_github_time("2027-01-15T09:56:00Z")
        self.assertEqual(source.token(), "ghs_two", "refreshed inside the five-minute margin")
        self.assertEqual(source.refreshes, 2)
        source.invalidate()
        self.assertEqual(source.token(), "ghs_two")
        self.assertEqual(source.refreshes, 3)


class StaticTokens:
    def __init__(self, value="ghs_static"):
        self.value = value
        self.invalidated = 0

    def token(self):
        return self.value

    def jwt(self):
        return "ey.jwt"

    def invalidate(self):
        self.invalidated += 1


def client(transport, clock=None):
    clock = clock or Clock()
    return ga.GitHubClient(StaticTokens(), transport, clock=clock, sleep=clock.sleep), clock


class Client(unittest.TestCase):
    def test_records_rate_limit_and_counts(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/x",
            200,
            {"ok": True},
            {"X-RateLimit-Remaining": "4321", "X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "1800000600"},
        )
        c, _ = client(transport)
        status, data = c.get_json("x")
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(c.rate_limit(), (4321, 1800000600.0))
        stats = c.stats()
        self.assertEqual((stats["requests"], stats["retries"], stats["rate_limit_remaining"]), (1, 0, 4321))
        headers = transport.calls[0][2]
        self.assertEqual(headers["Authorization"], "Bearer ghs_static")
        self.assertEqual(headers["X-GitHub-Api-Version"], "2022-11-28")

    def test_secondary_limit_waits_and_retries(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/x", 403, {"message": "secondary"}, {"Retry-After": "7"})
        transport.add("GET", "https://api.github.com/x", 200, {"ok": True})
        c, clock = client(transport)
        self.assertEqual(c.get_json("x")[1], {"ok": True})
        self.assertEqual(clock.slept, [7.0])
        self.assertEqual((c.stats()["retries"], c.stats()["limit_waits"]), (1, 1))

    def test_primary_limit_exhausted_waits_until_reset(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/x",
            403,
            {},
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000030"},
        )
        transport.add("GET", "https://api.github.com/x", 200, {"ok": True})
        c, clock = client(transport)
        c.get_json("x")
        self.assertEqual(clock.slept, [31.0])

    def test_long_limit_raises_instead_of_blocking(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/x", 403, {}, {"Retry-After": "3600"})
        c, clock = client(transport)
        with self.assertRaises(ga.RateLimited):
            c.get_json("x")
        self.assertEqual(clock.slept, [])
        self.assertIn("rate limited", c.stats()["last_error"])

    def test_401_invalidates_the_installation_token_once(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/x", 401, {"message": "Bad credentials"})
        transport.add("GET", "https://api.github.com/x", 200, {"ok": True})
        c, _ = client(transport)
        self.assertEqual(c.get_json("x")[1], {"ok": True})
        self.assertEqual(c._tokens.invalidated, 1)

    def test_server_errors_retry_then_raise(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/x", 502, b"bad gateway")
        c, clock = client(transport)
        with self.assertRaises(ga.GitHubError) as caught:
            c.get_json("x")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(c.stats()["failures"], 1)

    def test_pagination(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/items?per_page=2&page=1", 200, [1, 2])
        transport.add("GET", "https://api.github.com/items?per_page=2&page=2", 200, [3])
        c, _ = client(transport)
        self.assertEqual(c.paginate("items", per_page=2), [1, 2, 3])


def pr_json(**overrides):
    data = {
        "number": 42,
        "title": "feat: add",
        "body": "body",
        "state": "open",
        "draft": False,
        "merged": False,
        "user": {"login": "alice", "id": 7},
        "labels": [{"name": "ready-to-merge"}],
        "head": {"sha": HEAD, "repo": {"full_name": TAUCETI}},
        "base": {"ref": "main", "sha": BASE},
    }
    data.update(overrides)
    return data


def adapter(transport, git=None):
    c, _ = client(transport)
    tokens = c._tokens
    return ga.GitHubAdapter(
        c,
        repo=TAUCETI,
        clone_dir="/tmp/clone",
        tokens=tokens,
        git=git or (lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")),
    )


class Adapter(unittest.TestCase):
    def test_fetch_pr(self):
        transport = FakeTransport()
        transport.add("GET", f"https://api.github.com/repos/{TAUCETI}/pulls/42", 200, pr_json())
        transport.add("GET", f"https://api.github.com/repos/{TAUCETI}/pulls/43", 404, {"message": "Not Found"})
        a = adapter(transport)
        pr = a.fetch_pr(TAUCETI, 42)
        self.assertEqual(
            (pr.number, pr.head_sha, pr.base_ref, pr.author_login, pr.state, pr.draft, pr.labels),
            (42, HEAD, "main", "alice", "open", False, ("ready-to-merge",)),
        )
        self.assertEqual((pr.base_sha, pr.author_id, pr.head_repo), (BASE, 7, TAUCETI))
        with self.assertRaises(ga.PullRequestNotFound):
            a.fetch_pr(TAUCETI, 43)
        self.assertIsNone(a.fetch_pr_or_none(TAUCETI, 43))

    def test_pr_merge_base(self):
        transport = FakeTransport()
        transport.add("GET", f"https://api.github.com/repos/{TAUCETI}/pulls/42", 200, pr_json())
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/compare/{BASE}...{HEAD}?per_page=1",
            200,
            {"merge_base_commit": {"sha": "d" * 40}},
        )
        a = adapter(transport)
        self.assertEqual(a.pr_merge_base(TAUCETI, 42, HEAD), "d" * 40)
        self.assertIsNone(a.pr_merge_base(TAUCETI, 42, "e" * 40), "a moved head gets no merge base")

    def test_dispatch_candidate_sends_the_bors_payload(self):
        transport = FakeTransport()
        transport.add("POST", f"https://api.github.com/repos/{TAUCETI}/dispatches", 204)
        a = adapter(transport)
        members = [{"pr": 1, "head_sha": "1" * 40}, {"pr": 2, "head_sha": "2" * 40}]
        dispatch_id = a.dispatch_candidate(
            TAUCETI, head_sha=HEAD, base_sha=BASE, base_ref="main", batch_id=2650, members=members
        )
        self.assertEqual(dispatch_id, f"bors branch=main batch=2650 head={HEAD} base={BASE}")
        body = json.loads(transport.calls[0][3])
        self.assertEqual(
            body,
            {
                "event_type": "tauceti-bors-staging",
                "client_payload": {
                    "head_sha": HEAD,
                    "base_sha": BASE,
                    "batch_id": 2650,
                    "members": members,
                    "base_ref": "main",
                },
            },
        )

    def test_find_candidate_run_with_phases(self):
        transport = FakeTransport()
        title = ga.dispatch_title("main", 2650, HEAD, BASE)
        runs_url = f"https://api.github.com/repos/{TAUCETI}/actions/workflows/pr-build.yml/runs"
        run = {
            "id": 41775740735,
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-04T04:07:11Z",
            "run_started_at": "2026-11-04T04:07:20Z",
            "updated_at": "2026-11-04T04:40:00Z",
            "display_title": title,
            "event": "repository_dispatch",
            "html_url": f"https://github.com/{TAUCETI}/actions/runs/41775740735",
        }
        other = dict(run, id=1, display_title="pr-build pr=1 head=x")
        transport.add(
            "GET",
            f"{runs_url}?event=repository_dispatch&created=2026-11-04T04%3A06%3A00Z..2026-11-04T04%3A08%3A00Z&per_page=100&page=1",
            200,
            {"total_count": 2, "workflow_runs": [other, run]},
        )
        jobs = {
            "jobs": [
                {
                    "name": "pick-runner",
                    "runner_name": "chungus-1",
                    "labels": ["self-hosted"],
                    "started_at": "s0",
                    "completed_at": "c0",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [],
                },
                {
                    "name": "sandboxed-build",
                    "runner_name": "fleet-host-7",
                    "labels": ["self-hosted", "staging"],
                    "started_at": "s1",
                    "completed_at": "c1",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [
                        {
                            "name": "Restore cache",
                            "number": 1,
                            "started_at": "s2",
                            "completed_at": "c2",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Build",
                            "number": 2,
                            "started_at": "s3",
                            "completed_at": None,
                            "status": "in_progress",
                            "conclusion": None,
                        },
                    ],
                },
            ]
        }
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/actions/runs/41775740735/attempts/2/jobs?per_page=100&page=1",
            200,
            jobs,
        )
        a = adapter(transport)
        import time

        real_time = time.time
        time.time = lambda: app.parse_github_time("2026-11-04T04:07:00Z")
        try:
            found = a.find_candidate_run(
                TAUCETI,
                batch_id=2650,
                head_sha=HEAD,
                base_sha=BASE,
                since=app.parse_github_time("2026-11-04T04:07:00Z"),
            )
        finally:
            time.time = real_time
        self.assertEqual(
            (found.run_id, found.attempt, found.status, found.conclusion), ("41775740735", 2, "completed", "success")
        )
        self.assertEqual(
            (found.runner_name, found.host, found.runner_labels),
            ("fleet-host-7", "fleet-host-7", ("self-hosted", "staging")),
        )
        self.assertEqual(
            [p.name for p in found.phases],
            ["pick-runner", "sandboxed-build", "sandboxed-build: Restore cache", "sandboxed-build: Build"],
        )
        self.assertEqual(found.phases[-1].status, "in_progress")
        self.assertEqual(found.completed_at, app.parse_github_time("2026-11-04T04:40:00Z"))
        self.assertEqual(found.diagnostics, (), "a successful run carries no diagnostics")
        self.assertEqual(
            ga.dispatch_members([7, {"pr": 8, "head_sha": HEAD}]), [{"pr": 7}, {"pr": 8, "head_sha": HEAD}]
        )
        self.assertEqual(found.phases[0].started_at, None, "fixture timestamps that are not ISO read as unknown")
        self.assertEqual(found.diagnostics, (), "a successful run carries no diagnostics")

    def test_find_candidate_run_absent_and_ambiguous(self):
        transport = FakeTransport()
        title = ga.dispatch_title("main", 2650, HEAD, BASE)
        run = {"id": 5, "display_title": title, "event": "repository_dispatch", "status": "queued", "created_at": "x"}
        import time

        real_time = time.time
        time.time = lambda: 1_800_000_000.0
        try:
            # The recorded URL carries the adapter's own creation window: a minute either side.
            a = adapter(transport)
            since = app.parse_github_time("2026-11-04T04:08:00Z")
            expected_url = a.client.url(
                f"repos/{TAUCETI}/actions/workflows/pr-build.yml/runs",
                {
                    "event": "repository_dispatch",
                    "created": f"{ga._iso(since - 60)}..{ga._iso(1_800_000_000.0 + 60)}",
                    "per_page": 100,
                    "page": 1,
                },
            )
            transport.add("GET", expected_url, 200, {"total_count": 0, "workflow_runs": []})
            self.assertIsNone(
                a.find_candidate_run(TAUCETI, batch_id=2650, head_sha=HEAD, base_sha=BASE, since=since, with_jobs=False)
            )
            transport.responses.clear()
            transport.add("GET", expected_url, 200, {"total_count": 2, "workflow_runs": [run, dict(run, id=6)]})
            with self.assertRaises(ga.AmbiguousCandidateRun):
                a.find_candidate_run(TAUCETI, batch_id=2650, head_sha=HEAD, base_sha=BASE, since=since, with_jobs=False)
        finally:
            time.time = real_time

    def test_commit_status_binds_the_run(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/commits/{HEAD}/status",
            200,
            {
                "state": "failure",
                "statuses": [
                    {
                        "context": "scope",
                        "state": "success",
                        "description": "ok",
                        "target_url": None,
                        "updated_at": "t1",
                    },
                    {
                        "context": "build",
                        "state": "failure",
                        "description": "3 modules failed",
                        "target_url": f"https://github.com/{TAUCETI}/actions/runs/41771236355",
                        "updated_at": "t2",
                        "creator": {"login": "tauceti-ci-status[bot]"},
                    },
                ],
            },
        )
        a = adapter(transport)
        status = a.commit_status(TAUCETI, HEAD, "build")
        self.assertEqual(
            (status.state, status.description, status.run_id, status.updated_at),
            ("failure", "3 modules failed", "41771236355", None),
        )
        self.assertIsNone(a.commit_status(TAUCETI, HEAD, "bump-guard"))
        self.assertIsNone(a.commit_status(TAUCETI, HEAD, "scope").run_id)

    def test_fast_forward(self):
        transport = FakeTransport()
        ref_url = f"https://api.github.com/repos/{TAUCETI}/git/ref/heads/main"
        patch_url = f"https://api.github.com/repos/{TAUCETI}/git/refs/heads/main"
        transport.add("GET", ref_url, 200, {"object": {"sha": BASE}})
        transport.add("PATCH", patch_url, 200, {"object": {"sha": TIP}})
        a = adapter(transport)
        self.assertEqual(a.fast_forward(TAUCETI, "main", BASE, TIP), ga.FastForwardResult(True, BASE))
        self.assertEqual(json.loads(transport.calls[-1][3]), {"sha": TIP, "force": False})
        # The parent moved before the push: refused without a write.
        transport.responses.clear()
        transport.add("GET", ref_url, 200, {"object": {"sha": "e" * 40}})
        refused = a.fast_forward(TAUCETI, "main", BASE, TIP)
        self.assertEqual((refused.ok, refused.actual_parent), (False, "e" * 40))
        self.assertEqual(transport.calls[-1][0], "GET")
        # The parent moved between the read and the push: GitHub refuses the non-fast-forward.
        transport.responses.clear()
        transport.add("GET", ref_url, 200, {"object": {"sha": BASE}})
        transport.add("GET", ref_url, 200, {"object": {"sha": "f" * 40}})
        transport.add("PATCH", patch_url, 422, {"message": "Update is not a fast forward"})
        refused = a.fast_forward(TAUCETI, "main", BASE, TIP)
        self.assertEqual(
            (refused.ok, refused.actual_parent, refused.error), (False, "f" * 40, "Update is not a fast forward")
        )

    def test_close_pr_as_bors_does(self):
        transport = FakeTransport()
        transport.add("PATCH", f"https://api.github.com/repos/{TAUCETI}/pulls/42", 200, {})
        adapter(transport).close_pr(TAUCETI, 42, "[Merged by Bors] - feat: add")
        self.assertEqual(
            json.loads(transport.calls[-1][3]), {"state": "closed", "title": "[Merged by Bors] - feat: add"}
        )

    def test_failed_run_diagnostics_from_the_job_log(self):
        transport = FakeTransport()
        run = {
            "id": 7,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-11-04T04:07:11Z",
            "updated_at": "2026-11-04T04:40:00Z",
        }
        jobs = {
            "jobs": [
                {
                    "id": 70,
                    "name": "sandboxed-build",
                    "runner_name": "h7",
                    "labels": [],
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [],
                },
                {
                    "id": 71,
                    "name": "finalize-merge-group-build",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [],
                },
            ]
        }
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/actions/runs/7/attempts/1/jobs?per_page=100&page=1",
            200,
            jobs,
        )
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/actions/jobs/70/logs",
            302,
            b"",
            {"Location": "https://signed.example/log"},
        )
        log = (
            "2026-11-04T04:20:00.1Z error: TauCeti/Algebra/Group/Basic.lean:12:3: error: unknown identifier 'foo'\n"
            "2026-11-04T04:20:01.1Z audit-driver: import TauCeti.A.B failed, environment already contains 'x' from TauCeti.C.D\n"
            '2026-11-04T04:20:02.1Z LINT-ENV-LOCATIONS {"version": 1, "complete": true, "findings": [{"linter": "docBlame", "declaration": "foo", "path": "TauCeti/Lint/Target.lean", "line": 3, "column": 1}]}\n'
            "2026-11-04T04:21:00.1Z error: build failed\n"
        )
        transport.add("GET", "https://signed.example/log", 200, log.encode())
        found = adapter(transport).candidate_run(TAUCETI, run)
        self.assertEqual(
            transport.calls[-1][2], {"User-Agent": ga.USER_AGENT}, "the signed URL is fetched without credentials"
        )
        kinds = [(d["kind"], d["module"]) for d in found.diagnostics]
        self.assertEqual(
            kinds,
            [
                ("compiler", "TauCeti.Algebra.Group.Basic"),
                ("audit", "TauCeti.A.B"),
                ("audit", "TauCeti.C.D"),
                ("lint", "TauCeti.Lint.Target"),
            ],
        )
        self.assertTrue(all("message" in d for d in found.diagnostics))
        # A cancelled run is an infrastructure finding, not a source one.
        transport.responses.clear()
        cancelled = dict(run, conclusion="cancelled")
        transport.add(
            "GET",
            f"https://api.github.com/repos/{TAUCETI}/actions/runs/7/attempts/1/jobs?per_page=100&page=1",
            200,
            {"jobs": [dict(jobs["jobs"][0], conclusion="cancelled")]},
        )
        found = adapter(transport).candidate_run(TAUCETI, cancelled)
        self.assertEqual({d["kind"] for d in found.diagnostics}, {"infrastructure"})

    def test_update_ref_pushes_the_object_with_the_ref_from_the_clone(self):
        # Retained commits exist only in the service's clone: the ref is pushed from it, objects included.
        captured = {}

        def git(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, "", "")

        transport = FakeTransport()
        a = adapter(transport, git=git)
        a.update_ref(TAUCETI, "refs/tauceti-queue/2650/tip", TIP)
        self.assertEqual(captured["command"][:4], ["git", "-C", "/tmp/clone", "push"])
        self.assertIn(f"https://github.com/{TAUCETI}.git", captured["command"])
        self.assertEqual(captured["command"][-1], f"+{TIP}:refs/tauceti-queue/2650/tip")
        self.assertEqual(captured["env"]["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(transport.calls, [], "no API call when the clone can push")

    def test_update_ref_without_a_clone_creates_through_the_api_when_missing(self):
        transport = FakeTransport()
        transport.add(
            "PATCH",
            f"https://api.github.com/repos/{TAUCETI}/git/refs/tauceti-queue/2650/tip",
            422,
            {"message": "Reference does not exist"},
        )
        transport.add("POST", f"https://api.github.com/repos/{TAUCETI}/git/refs", 201, {})
        c, _ = client(transport)
        a = ga.GitHubAdapter(c, repo=TAUCETI, tokens=c._tokens)
        a.update_ref(TAUCETI, "refs/tauceti-queue/2650/tip", TIP)
        self.assertEqual(json.loads(transport.calls[-1][3]), {"ref": "refs/tauceti-queue/2650/tip", "sha": TIP})

    def test_post_comment_is_idempotent_by_marker(self):
        transport = FakeTransport()
        comments_url = f"https://api.github.com/repos/{TAUCETI}/issues/42/comments"
        transport.add("GET", f"{comments_url}?per_page=100&page=1", 200, [{"id": 1, "body": "unrelated"}])
        transport.add("POST", comments_url, 201, {"id": 900})
        a = adapter(transport)
        self.assertEqual(a.post_comment(TAUCETI, 42, "Merged.", "landed 2650"), "900")
        posted = json.loads(transport.calls[-1][3])["body"]
        self.assertEqual(posted, "Merged.\n\n<!-- tauceti-queue landed 2650 -->\n")
        transport.responses.clear()
        transport.add(
            "GET",
            f"{comments_url}?per_page=100&page=1",
            200,
            [{"id": 1, "body": "unrelated"}, {"id": 900, "body": posted}],
        )
        transport.add("PATCH", f"https://api.github.com/repos/{TAUCETI}/issues/comments/900", 200, {"id": 900})
        self.assertEqual(a.post_comment(TAUCETI, 42, "Merged.", "landed 2650"), "900")
        self.assertEqual(transport.calls[-1][0], "GET", "an identical body is not rewritten")
        self.assertEqual(a.post_comment(TAUCETI, 42, "Merged, then commented.", "landed 2650"), "900")
        self.assertEqual(transport.calls[-1][0], "PATCH", "a changed body edits the marked comment")

    def test_set_labels(self):
        transport = FakeTransport()
        transport.add("POST", f"https://api.github.com/repos/{TAUCETI}/issues/42/labels", 200, [])
        transport.add("DELETE", f"https://api.github.com/repos/{TAUCETI}/issues/42/labels/awaiting-requeue", 404, {})
        transport.add("DELETE", f"https://api.github.com/repos/{TAUCETI}/issues/42/labels/roadmap%2FX", 200, [])
        a = adapter(transport)
        a.set_labels(TAUCETI, 42, ["on-queue"], ["awaiting-requeue", "roadmap/X"])
        self.assertEqual(json.loads(transport.calls[0][3]), {"labels": ["on-queue"]})
        self.assertEqual(len(transport.calls), 3)

    def test_permissions(self):
        transport = FakeTransport()
        base = f"https://api.github.com/repos/{TAUCETI}/collaborators"
        transport.add(
            "GET",
            f"{base}/kim-em/permission",
            200,
            {"permission": "admin", "role_name": "admin", "user": {"login": "kim-em"}},
        )
        transport.add("GET", f"{base}/alice/permission", 200, {"permission": "write", "role_name": "write"})
        transport.add("GET", f"{base}/bob/permission", 200, {"permission": "read", "role_name": "read"})
        transport.add("GET", f"{base}/nobody/permission", 404, {})
        a = adapter(transport)
        self.assertTrue(a.repository_admin("kim-em"))
        self.assertTrue(a.reviewer("kim-em"))
        self.assertFalse(a.repository_admin("alice"))
        self.assertTrue(a.reviewer("alice"))
        self.assertFalse(a.reviewer("bob"))
        self.assertIsNone(a.collaborator_permission(TAUCETI, "nobody"))

    def test_user_for_token(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/user", 200, {"login": "worker-login", "id": 99})
        a = adapter(transport)
        self.assertEqual(a.user_for_token("ghp_x"), ("worker-login", 99))
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer ghp_x", "the worker's token, not the App's")
        transport.responses.clear()
        transport.add("GET", "https://api.github.com/user", 401, {"message": "Bad credentials"})
        self.assertIsNone(a.user_for_token("bad"))
        self.assertEqual(a.client._tokens.invalidated, 0, "a worker's bad token does not invalidate the App's")
        # An exhausted user quota is not a verdict on the token: the server keeps its cached identity.
        transport.responses.clear()
        transport.add(
            "GET", "https://api.github.com/user", 403, {"message": "API rate limit exceeded"}, {"Retry-After": "3600"}
        )
        with self.assertRaises(ports.GitHubUnavailable):
            a.user_for_token("ghp_x")
        transport.responses.clear()
        transport.add("GET", "https://api.github.com/user", 502, b"bad gateway")
        with self.assertRaises(ports.GitHubUnavailable):
            a.user_for_token("ghp_x")

    def test_repository_admin_reports_an_outage(self):
        transport = FakeTransport()
        transport.add(
            "GET", f"https://api.github.com/repos/{TAUCETI}/collaborators/kim-em/permission", 502, b"bad gateway"
        )
        with self.assertRaises(ports.GitHubUnavailable):
            adapter(transport).repository_admin("kim-em")

    def test_list_open_pull_requests_over_graphql(self):
        transport = FakeTransport()
        rest_headers = {"x-ratelimit-remaining": "4321", "x-ratelimit-limit": "5000", "x-ratelimit-reset": "1800003600"}
        transport.add("GET", f"https://api.github.com/repos/{TAUCETI}/pulls/42", 200, pr_json(), rest_headers)
        external = json.dumps(
            {
                "schema": "tauceti-merge.eligibility/v1",
                "repo": TAUCETI,
                "pr": 7,
                "merge_base_sha": BASE,
                "eligible": True,
                "review_safe": True,
                "single": False,
            }
        )
        run = {
            "databaseId": 9001,
            "name": "merge eligibility",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "externalId": external,
            "completedAt": "2026-11-04T07:00:00Z",
            "permalink": "https://github.com/TauCetiProject/TauCeti/runs/9001",
        }

        def page(nodes, cursor):
            return {
                "data": {
                    "rateLimit": {"cost": 8, "remaining": 4990, "resetAt": "2026-11-04T08:00:00Z"},
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                            "nodes": nodes,
                        }
                    },
                }
            }

        with_check = {
            "number": 7,
            "isDraft": False,
            "baseRefName": "main",
            "headRefOid": HEAD,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": HEAD,
                            "checkSuites": {
                                "nodes": [
                                    {
                                        "app": {"databaseId": 3947238},
                                        "checkRuns": {"nodes": [run, dict(run, name="other", databaseId=9002)]},
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
        }
        without = {
            "number": 8,
            "isDraft": True,
            "baseRefName": "main",
            "headRefOid": TIP,
            "commits": {"nodes": [{"commit": {"oid": TIP, "checkSuites": {"nodes": []}}}]},
        }
        graphql_headers = {"x-ratelimit-remaining": "4990", "x-ratelimit-limit": "5000"}
        transport.add("POST", "https://api.github.com/graphql", 200, page([with_check], "c1"), graphql_headers)
        transport.add("POST", "https://api.github.com/graphql", 200, page([without], None), graphql_headers)
        a = adapter(transport)
        a.fetch_pr(TAUCETI, 42)
        listed = a.list_open_pull_requests(TAUCETI, review_app_id=3947238)
        self.assertEqual(
            [(p.number, p.head_sha, p.draft, p.base_ref) for p in listed],
            [(7, HEAD, False, "main"), (8, TIP, True, "main")],
        )
        (check,) = listed[0].eligibility_checks
        self.assertEqual(
            (check["id"], check["name"], check["app"]["id"], check["head_sha"], check["status"], check["conclusion"]),
            (9001, "merge eligibility", 3947238, HEAD, "completed", "success"),
        )
        self.assertEqual((check["external_id"], check["html_url"]), (external, run["permalink"]))
        self.assertEqual(listed[1].eligibility_checks, ())
        bodies = [json.loads(body) for method, url, _h, body in transport.calls if url.endswith("/graphql")]
        self.assertEqual([b["variables"]["after"] for b in bodies], [None, "c1"])
        self.assertEqual(
            bodies[0]["variables"], {"owner": "TauCetiProject", "name": "TauCeti", "after": None, "appId": 3947238}
        )
        self.assertEqual(a.rate_limit()[0], 4321, "the GraphQL quota headers do not overwrite the REST counter")
        transport.add("POST", "https://api.github.com/graphql", 200, {"errors": [{"message": "bad query"}]})
        transport.responses["POST https://api.github.com/graphql"] = transport.responses[
            "POST https://api.github.com/graphql"
        ][-1:]
        with self.assertRaises(ga.GitHubError):
            a.list_open_pull_requests(TAUCETI, review_app_id=3947238)

    def test_list_open_pull_requests_trusts_the_filtered_suite_when_app_is_null(self):
        """Production shape (2026-11-04): through the installation token the review App's suite has `app: null`."""
        fixture = json.loads((REPO / "tests/fixtures/merge-eligibility-check-runs.json").read_text())
        transport = FakeTransport()
        transport.add("POST", "https://api.github.com/graphql", 200, fixture["graphql"])
        a = adapter(transport)
        listed = a.list_open_pull_requests(fixture["repo"], review_app_id=fixture["review_app_id"])
        self.assertEqual([p.number for p in listed], [24557, 9268])
        from tauceti_queue import eligibility

        eligible, withdrawn = listed
        self.assertEqual([c["app"]["id"] for c in eligible.eligibility_checks], [3947238, 3947238])
        self.assertTrue(all(eligibility.trusted(c) for c in eligible.eligibility_checks))
        decision = eligibility.latest(
            list(eligible.eligibility_checks),
            fixture["repo"],
            eligible.number,
            eligible.head_sha,
            fixture["review_app_id"],
        )
        self.assertEqual(
            (decision.check_id, decision.eligible, decision.review_safe, decision.merge_base_sha),
            (125149712005, True, True, "93af5b549b7d9ed44291d6ceb093a8013dda00f8"),
            "the highest check id wins over the earlier withdrawal",
        )
        decision = eligibility.latest(
            list(withdrawn.eligibility_checks),
            fixture["repo"],
            withdrawn.number,
            withdrawn.head_sha,
            fixture["review_app_id"],
        )
        self.assertEqual((decision.eligible, decision.review_safe), (False, False))
        # The mapped records equal the REST records in every field the reader uses.
        rest = {c["id"]: c for c in fixture["rest"]}
        for check in eligible.eligibility_checks:
            for key in ("id", "name", "head_sha", "status", "conclusion", "external_id", "completed_at", "html_url"):
                self.assertEqual(check[key], rest[check["id"]][key], key)
            self.assertEqual(check["app"]["id"], rest[check["id"]]["app"]["id"])

    def test_rate_limit_falls_back_to_the_endpoint(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/rate_limit",
            200,
            {"resources": {"core": {"remaining": 4999, "reset": 1800000600}}},
        )
        a = adapter(transport)
        self.assertEqual(a.rate_limit(), (4999, 1800000600.0))

    def test_fetch_refs_keeps_the_token_out_of_argv(self):
        captured = {}

        def git(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, "", "")

        a = adapter(FakeTransport(), git=git)
        a.fetch_refs(
            TAUCETI, ["+refs/pull/42/head:refs/tauceti-queue/pr/42", "+refs/heads/main:refs/tauceti-queue/main"]
        )
        command = captured["command"]
        self.assertEqual(command[:5], ["git", "-C", "/tmp/clone", "fetch", "--no-tags"])
        self.assertIn(f"https://github.com/{TAUCETI}.git", command)
        self.assertFalse(any("ghs_static" in part for part in command))
        env = captured["env"]
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        import base64

        self.assertEqual(
            env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:ghs_static").decode()
        )
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 128, "", "fatal: could not read")

        with self.assertRaises(ga.GitHubError):
            adapter(FakeTransport(), git=failing).fetch_refs(TAUCETI, ["refs/heads/main"])


class Polling(unittest.TestCase):
    def test_one_lookup_per_candidate_per_interval_and_a_quota_reserve(self):
        calls = []

        class StubAdapter:
            remaining = 1000

            def rate_limit(self):
                return self.remaining, None

            def find_candidate_run(self, repo, **kw):
                calls.append(("run", kw["batch_id"]))
                return None

            def commit_status(self, repo, sha, context):
                calls.append(("status", sha))
                return None

        clock = Clock()
        stub = StubAdapter()
        poller = ga.CandidatePoller(stub, interval=60, reserve=200, clock=clock)
        refs = [ga.CandidateRef(TAUCETI, 1, HEAD, BASE), ga.CandidateRef(TAUCETI, 2, TIP, BASE)]
        self.assertEqual(len(poller.poll(refs)), 2)
        self.assertEqual(poller.poll(refs), [], "nothing is due again within the interval")
        clock.now += 61
        self.assertEqual(len(poller.poll(refs)), 2)
        self.assertEqual(calls.count(("run", 1)), 2)
        clock.now += 61
        stub.remaining = 150
        self.assertEqual(poller.poll(refs), [], "below the reserve nothing is polled")
        self.assertEqual(poller.skipped, 2)


if __name__ == "__main__":
    unittest.main()
