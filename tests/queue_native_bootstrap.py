#!/usr/bin/env python3
"""The actual Service wires native source and separately scoped execution."""

import sys
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_source_authority as authority_fixtures
from queue_ci_requests import CONFIG, policy
from tauceti_tools.forge import ForgeError, PrincipalRef

from tauceti_queue.__main__ import Service
from tauceti_queue.config import Config
from tauceti_queue.fakes import FakeRepo
from tauceti_queue.forgejo_source import NativePublishers
from tauceti_queue.github_app import AppCredentials, GitHubApp, InstallationTokenSource
from tauceti_queue.native_bootstrap import NativeBootstrap
from tauceti_queue.runtime import Revision, ServiceRuntime
from tauceti_queue.source_authority import SourceAccessDenied, SourceAuthorityUnavailable


class Bootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = FakeRepo(root / "git")
        self.config = Config(
            repository="team/Math",
            canonical_branch="main",
            host="fixture",
            content_clone=repo.clone,
            database=root / "queue.sqlite",
            listen_host="127.0.0.1",
            listen_port=0,
            credentials_dir=root / "no-app-credentials",
            log_dir=root / "logs",
            admission_rubrics="proof",
            admission_lean_root="Math",
        )
        self.policy = policy()
        self.native = authority_fixtures.SourceAuthority()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.native.identity = self.policy.source
        self.native.repository = self.policy.source.repository
        self.native.repo.update(
            id=self.policy.source.native_id, full_name="team/Math", html_url=self.policy.source.repository.url
        )
        api = self.native.authority._client_factory(
            self.policy.source.repository.forge, token="controller-source-token"
        )
        api.forge = self.policy.source.repository.forge
        actor = PrincipalRef(api.forge, "7")
        publishers = NativePublishers(
            self.policy.source,
            frozenset({PrincipalRef(api.forge, "8")}),
            frozenset({actor}),
            frozenset({actor}),
            generation="1" * 32,
        )
        credentials = AppCredentials(1, "fixture", 2, b"test-only-private-key", b"test-only-app-secret")
        self.tokens = InstallationTokenSource(
            credentials,
            self.forbidden_execution_request,
            repository_id=self.policy.execution.native_id,
            permissions={"contents": "write", "actions": "write"},
        )
        self.bootstrap = NativeBootstrap(
            CONFIG,
            self.policy,
            "b" * 40,
            publishers,
            self.native.control_policy(self.policy.source),
            api,
            "source-git-token",
            self.tokens,
            b"x" * 32,
        )
        self.services = []
        self.addCleanup(self.close_services)

    def forbidden_execution_request(self, *_args, **_kwargs):
        self.fail("service startup attempted an execution API request")

    def close_services(self):
        for service in self.services:
            service.shutdown()

    def service(self, bootstrap=None, config=None):
        config = config or self.config
        service = Service(
            config,
            ServiceRuntime.start(config, Revision("a" * 40, False, "fixture")),
            native=bootstrap or self.bootstrap,
        )
        self.services.append(service)
        service.open_store()
        return service

    def test_actual_service_startup_and_http_status_do_not_discover_github_source_credentials(self):
        service = self.service()
        with patch.object(GitHubApp, "load", side_effect=AssertionError("implicit GitHub credentials")):
            service.open_github()
            service.build_controller()
            service.seed_settings()
            service.start_server()
        self.assertIs(service.controller.source, service.github)
        self.assertIs(service.controller.source_authority.source, self.policy.source)
        self.assertEqual(service.controller.candidate_ci.ledger.policy, self.policy)
        self.assertEqual(service.native_http.repositories, {self.policy.source.native_id: "team/Math"})
        host, port = service.server.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(
            self.native.calls,
            [("GET", f"repositories/{self.policy.source.native_id}/effective_access", "controller-source-token")],
        )

    def test_source_branch_and_execution_repository_mismatches_fail_before_source_reads(self):
        for configured in [
            replace(self.config, repository="other/source"),
            replace(self.config, canonical_branch="other"),
            replace(self.config, admission_source="eligibility"),
        ]:
            with self.subTest(config=configured.repository + configured.canonical_branch):
                with self.assertRaises(ValueError):
                    self.service(config=configured).build_controller()
        wrong_tokens = InstallationTokenSource(
            self.tokens.credentials,
            self.forbidden_execution_request,
            repository_id=self.policy.execution.native_id + 1,
            permissions={"contents": "write", "actions": "write"},
        )
        with self.assertRaises(ValueError):
            self.service(bootstrap=replace(self.bootstrap, execution_tokens=wrong_tokens)).build_controller()
        self.assertEqual(self.native.calls, [])

    def test_readonly_or_unavailable_source_cannot_start_a_native_controller(self):
        self.native.effective.update(code_write=False, repository_admin=False)
        denied = self.service()
        with self.assertRaises(SourceAccessDenied):
            denied.build_controller()
        self.assertIsNone(denied.store.get_setting("source-ci:binding"))
        self.native.effective.clear()
        self.native.error = ForgeError("private upstream response", status=503)
        with self.assertRaises(SourceAuthorityUnavailable):
            self.service().build_controller()

    def test_controller_actor_must_be_explicitly_enrolled_for_publication(self):
        publishers = replace(self.bootstrap.publishers, recoveries=frozenset())
        with self.assertRaises(SourceAccessDenied):
            self.service(bootstrap=replace(self.bootstrap, publishers=publishers)).build_controller()

    def test_source_control_binding_and_secret_material_are_not_inferred_or_printed(self):
        value = repr(self.bootstrap)
        for secret in ["controller-source-token", "source-git-token", "test-only-private-key", "x" * 32]:
            self.assertNotIn(secret, value)
        with self.assertRaises(ValueError):
            self.service(bootstrap=replace(self.bootstrap, control_policy=None)).build_controller()
        with self.assertRaises(ValueError):
            Service(self.config, ServiceRuntime.start(self.config), native=object())


if __name__ == "__main__":
    unittest.main()
