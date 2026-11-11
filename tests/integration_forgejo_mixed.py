#!/usr/bin/env python3
"""Real native review receipts and Git source joined to the existing GitHub queue.

GitHub execution/evidence is deterministic here. Forgejo uses the stock HTTPS
server, native actor IDs, protected retention refs, and the actual Git transport.
"""

import base64
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from forgejo_git_source import source_fixture
from queue_ci_requests import CONFIG, policy
from queue_mixed_sources import Decision
from queue_source_ci import VerifiedExecution
from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import GITHUB, Forgejo, RepositoryRef

from tauceti_queue.ci_requests import RequestLedger
from tauceti_queue.config import Config
from tauceti_queue.controller import Controller
from tauceti_queue.fakes import make_controller, run_git
from tauceti_queue.forgejo_source import ForgejoSourceAdapter
from tauceti_queue.mixed_ci import MixedCandidateCI
from tauceti_queue.mixed_landing import MainWriter
from tauceti_queue.mixed_native import MixedNativeSource
from tauceti_queue.mixed_profile import MixedProfile
from tauceti_queue.mixed_sources import AdmissionInputs, MixedSources, NativePoller


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable HTTPS mixed queue")
class MixedNativeIntegration(unittest.TestCase):
    def test_native_receipt_moderation_mixed_candidate_landing_and_sync(self):
        with source_fixture() as (root, admin, repository, row, tokens):
            initial, github, repo, clock = make_controller(root / "github")
            self.addCleanup(initial.store.close)
            cfg = Config(
                repository=initial.repository,
                canonical_branch="main",
                forgejo_mode="admit",
                admission_rubrics="proof",
                admission_lean_root="Math",
            )
            native_id = cc.RepositoryIdentity(repository, row["id"])
            github_id = cc.RepositoryIdentity(RepositoryRef(GITHUB, initial.repository), 42)
            github.repository_identity = github_id
            reviewer = Forgejo(repository.forge, token=tokens["reviewer"])
            contributor = Forgejo(repository.forge, token=tokens["contributor"])
            queue = Forgejo(repository.forge, token=tokens["queue"])
            admin.request("PUT", f"repos/{repository.name}/collaborators/queue", {"permission": "admin"})
            reviewer_id, queue_id = reviewer.current_user()["id"], queue.current_user()["id"]
            token_file = root / "queue-token"
            token_file.write_text(tokens["queue"])
            token_file.chmod(0o600)
            profile = MixedProfile(
                github_id,
                native_id,
                frozenset({reviewer_id}),
                queue_id,
                token_file,
                {"fixture": "trusted native publishers", "authority_generation": "1" * 32},
            )
            native = MixedNativeSource(profile, cfg, repo.clone, client=queue, token=tokens["queue"], clock=clock)
            native.configure_publisher_policy(initial.store)
            base = native.ref_sha(repository.name, "refs/heads/main")
            native.fetch_refs(repository.name, [base])
            run_git(repo.origin, "fetch", "-q", str(repo.clone), base)
            run_git(repo.origin, "update-ref", "refs/heads/main", base)
            main_token_file = root / "main-token"
            main_token_file.write_text(tokens["contributor"])
            main_token_file.chmod(0o600)
            writer = MainWriter(native_id, repo.clone, main_token_file, contributor.current_user()["id"])
            registry = MixedSources(profile, cfg, initial.store, github, repo.clone, native=native, native_main=writer)
            controller = Controller(
                initial.store,
                github,
                str(repo.clone),
                initial.repository,
                revision="native-mixed-fixture",
                mixed=registry,
                clock=clock,
            )
            registry.controller = controller
            native_poll = NativePoller(registry, controller)
            controller.admission = AdmissionInputs(
                SimpleNamespace(
                    policy=Decision(),
                    is_current=lambda *args: True,
                    invalidate=lambda *args, **kw: None,
                    withdraw=lambda *args: None,
                    accept_manual=lambda *args: None,
                    state=SimpleNamespace(summary=lambda: {}),
                ),
                native_poll,
            )
            configuration = CONFIG.replace(b"https://source.example", repository.forge.url.encode()).replace(
                b'name="team/Math"', ('name="' + repository.name + '"').encode()
            )
            trusted = dataclasses.replace(
                policy(configuration),
                source=native_id,
                credential_origin=repository.forge,
                member_sources=(github_id, native_id),
            )
            ledger = RequestLedger(
                initial.store, repo.clone, trusted, configuration, workflow_revision="b" * 40, mixed_sources=True
            )
            execution = VerifiedExecution()
            registry.ci = controller.mixed_ci = MixedCandidateCI(ledger, execution, registry, controller, clock=clock)
            controller.set_setting("publication_enabled", True, "fixture")
            name = repository.name
            for branch, actor in (("main", "contributor"), ("tauceti-ci/**", "queue")):
                admin.request(
                    "POST",
                    f"repos/{name}/branch_protections",
                    {
                        "branch_name": branch,
                        "enable_push": True,
                        "enable_push_whitelist": True,
                        "push_whitelist_usernames": [actor],
                    },
                )
            contributor.request(
                "POST", f"repos/{name}/branches", {"new_branch_name": "proof", "old_branch_name": "main"}
            )
            contributor.request(
                "POST",
                f"repos/{name}/contents/Math/Native.lean",
                {
                    "branch": "proof",
                    "message": "Native proof",
                    "content": base64.b64encode(b"example : True := trivial\n").decode(),
                },
            )
            pull = contributor.request(
                "POST", f"repos/{name}/pulls", {"head": "proof", "base": "main", "title": "Native proof"}
            )
            number, head = pull["number"], pull["head"]["sha"]
            github_head = github.create_pr(number, {"Math/Github.lean": "example : True := trivial\n"})
            meta = {
                "head_sha": head,
                "merge_base_sha": base,
                "states": {"proof": "green"},
                "round": "one",
                "mode": "review",
            }
            body = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->\nProof checked."
            reviewer.post_comment(repository, number, body)
            self.assertFalse(native.admission_policy.evaluate(native.admission_snapshot(name, number))[0]["merge"])
            publisher = ForgejoSourceAdapter(
                native_id, reviewer, repo.clone, token=tokens["reviewer"], admission_policy=native.admission_policy
            )
            publisher.configure_publisher_policy(initial.store)
            intent = publisher.prepare_publication(name, number, head, body, "mixed-native-review", sequence=1)
            comment_id = publisher.publish(intent)
            self.assertTrue(native.admission_policy.evaluate(native.admission_snapshot(name, number))[0]["merge"])
            contributor.edit_comment(
                repository, int(comment_id), intent["body"].replace('"green"', '"blocking_request"')
            )
            self.assertFalse(
                native.admission_policy.evaluate(native.admission_snapshot(name, number))[0]["review_safe"]
            )
            restored = publisher.prepare_publication(
                name, number, head, body, "mixed-native-review-restored", sequence=2
            )
            publisher.publish(restored)
            with patch("tauceti_worker.github_state.read", side_effect=AssertionError("shared state consulted")):
                result = native_poll.run_once()
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["approved"], 1)
                controller.approve(controller.repository, number, github_head, "fixture")
                controller.tick()
                self.assertEqual(len(execution.dispatches), 1, controller._reasons)
                frozen = execution.dispatches[0]
                self.assertEqual({member.source for member in frozen.request.accepted_members}, {native_id, github_id})
                execution.add(frozen, classification="passed")
                clock.advance(60)
                controller.tick()
                controller.tick()
            # Native writes and the GitHub main change are real local/HTTPS Git.
            self.assertEqual(repo.main_sha(), frozen.request.head, controller._reasons)
            self.assertEqual(native.ref_sha(name, "refs/heads/main"), frozen.request.head)
            closed = queue.pull(repository, number)
            self.assertEqual(closed["state"], "closed")


if __name__ == "__main__":
    unittest.main()
