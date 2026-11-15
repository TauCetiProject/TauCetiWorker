#!/usr/bin/env python3
"""Actual native Service startup over disposable Forgejo HTTPS and fresh ACLs."""

import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.forgejo_git_source import source_fixture
from queue_ci_requests import CONFIG, policy
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forgejo

from tauceti_queue.__main__ import Service
from tauceti_queue.config import Config
from tauceti_queue.forgejo_source import NativePublishers
from tauceti_queue.github_app import AppCredentials, InstallationTokenSource
from tauceti_queue.native_bootstrap import NativeBootstrap
from tauceti_queue.runtime import Revision, ServiceRuntime
from tauceti_queue.source_authority import QueueControlPolicy, SourceAccessDenied, control_token_fingerprint
from tauceti_queue.task_identity import TASK_SCHEMA, TaskBinding
from tauceti_worker.source_identity import SourceActor


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable HTTPS bootstrap")
class Integration(unittest.TestCase):
    def test_service_uses_source_scope_and_native_repository_identity(self):
        with (
            source_fixture() as (root, admin, repository, row, tokens),
            patch("tauceti_worker.github_state.read", side_effect=AssertionError("shared-state source read")),
            patch(
                "tauceti_queue.github_adapter.GitHubClient.request", side_effect=AssertionError("GitHub source read")
            ),
        ):
            admin.request("PUT", f"repos/{repository.name}/collaborators/queue", {"permission": "admin"})
            admin.request("PUT", f"repos/{repository.name}/collaborators/worker", {"permission": "admin"})
            api = Forgejo(repository.forge, token=tokens["queue"])
            reviewer = Forgejo(repository.forge, token=tokens["reviewer"])
            queue_actor = SourceActor.from_native(repository.forge, api.current_user())
            review_actor = SourceActor.from_native(repository.forge, reviewer.current_user())
            identity = RepositoryIdentity(repository, row["id"])
            configuration = CONFIG.replace(b"https://source.example", repository.forge.url.encode()).replace(
                b"team/Math", repository.name.encode()
            )
            approved = replace(policy(configuration), source=identity, credential_origin=repository.forge)
            publishers = NativePublishers(
                identity,
                frozenset({review_actor.principal}),
                frozenset({queue_actor.principal}),
                frozenset({queue_actor.principal}),
                generation=secrets.token_hex(16),
            )
            clone = root / "controller"
            subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
            config = Config(
                repository=repository.name,
                canonical_branch="main",
                host="owned-fixture",
                content_clone=clone,
                database=root / "queue.sqlite",
                credentials_dir=root / "no-github-app",
                log_dir=root / "logs",
                listen_host="127.0.0.1",
                listen_port=0,
                admission_rubrics="proof",
                admission_lean_root="Math",
            )

            def no_execution_requests(*_args, **_kwargs):
                raise AssertionError("native startup made an execution API request")

            execution_tokens = InstallationTokenSource(
                AppCredentials(1, "fixture", 2, b"fixture", b"fixture"),
                no_execution_requests,
                repository_id=approved.execution.native_id,
                permissions={"contents": "write", "actions": "write"},
            )
            controls = QueueControlPolicy(identity, frozenset({control_token_fingerprint(identity, tokens["worker"])}))
            bootstrap = NativeBootstrap(
                configuration,
                approved,
                "b" * 40,
                publishers,
                controls,
                api,
                tokens["queue"],
                execution_tokens,
                secrets.token_hex(32).encode("ascii"),
            )
            service = Service(
                config, ServiceRuntime.start(config, Revision("a" * 40, False, "fixture")), native=bootstrap
            )
            try:
                service.open_store()
                service.open_github()
                if not os.environ.get("FORGEJO_EFFECTIVE_ACCESS"):
                    with self.assertRaises(SourceAccessDenied):
                        service.build_controller()
                    self.assertIsNone(service.store.get_setting("source-ci:binding"))
                    return
                service.build_controller()
                service.seed_settings()
                service.start_server()
                host, port = service.server.server_address
                url = f"http://{host}:{port}"
                with urllib.request.urlopen(url + "/healthz", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                # Create a real hook and PR in the owned Forgejo. Its background
                # delivery must pass the actual HTTP signature/repository checks.
                delivered = threading.Event()
                outcomes = []
                receiver = service.server.native_receiver
                original_handle = receiver.handle

                def observed_delivery(*args, **kwargs):
                    result = original_handle(*args, **kwargs)
                    outcomes.append(result)
                    if result == "invalidated":
                        delivered.set()
                    return result

                api.request(
                    "POST",
                    f"repos/{repository.name}/hooks",
                    {
                        "type": "forgejo",
                        "config": {
                            "url": url + "/webhook/forgejo",
                            "content_type": "json",
                            "secret": bootstrap.source_webhook_secret.decode("ascii"),
                        },
                        "events": ["pull_request"],
                        "active": True,
                    },
                )
                contributor = Forgejo(repository.forge, token=tokens["contributor"])
                contributor.request(
                    "POST",
                    f"repos/{repository.name}/branches",
                    {"new_branch_name": "webhook-proof", "old_branch_name": "main"},
                )
                contributor.request(
                    "POST",
                    f"repos/{repository.name}/contents/Math/Hook.lean",
                    {
                        "branch": "webhook-proof",
                        "message": "Owned hook fixture",
                        "content": base64.b64encode(b"example : True := trivial\n").decode(),
                    },
                )
                with patch.object(receiver, "handle", side_effect=observed_delivery):
                    pull = contributor.request(
                        "POST",
                        f"repos/{repository.name}/pulls",
                        {"head": "webhook-proof", "base": "main", "title": "Owned webhook proof"},
                    )
                    self.assertTrue(delivered.wait(20), outcomes)
                work = service.poller.state.get(pull["number"])
                self.assertEqual(work["head_sha"], pull["head"]["sha"])
                self.assertNotEqual(work["generation"], work["evaluated_generation"])
                self.assertEqual(service.store.rows("inputs"), [])
                binding = TaskBinding.from_policy(approved, queue_epoch=service.controller.candidate_ci.ledger.epoch)
                body = json.dumps(
                    {"worker": "owned-bootstrap", "schema": TASK_SCHEMA, "task_binding": binding.record()}
                ).encode()

                def claim():
                    request = urllib.request.Request(
                        url + "/tasks/claim",
                        data=body,
                        headers={"Authorization": "Bearer " + tokens["worker"], "Content-Type": "application/json"},
                    )
                    return urllib.request.urlopen(request, timeout=10)

                with claim() as response:
                    self.assertIn(response.status, {200, 204})
                self.assertEqual(service.store.rows("claims"), [])
                admin.request("PUT", f"repos/{repository.name}/collaborators/worker", {"permission": "write"})
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    claim()
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
