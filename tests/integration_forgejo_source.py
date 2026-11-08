#!/usr/bin/env python3
"""Actual HTTPS Forgejo PRs -> native admission -> queue -> source promotion.

CI observations are deterministic fixtures; the full hosted execution rehearsal
is separate. Every source read/write and protection hook uses the real server.
"""

import base64
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from forgejo_git_source import source_fixture
from queue_ci_requests import CONFIG, policy
from queue_source_ci import VerifiedExecution
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Forgejo, Incomplete

from tauceti_queue.admission import AdmissionPoller
from tauceti_queue.ci_requests import RequestLedger
from tauceti_queue.controller import Controller
from tauceti_queue.fakes import FakeClock
from tauceti_queue.forgejo_source import ForgejoSourceAdapter, NativeAdmissionPolicy, NativePublishers
from tauceti_queue.source_ci import SourceCandidateCI
from tauceti_queue.store import Store
from tauceti_worker.source_identity import SourceActor


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable HTTPS source")
class Integration(unittest.TestCase):
    def test_native_source_admission_and_queue_promotion(self):
        with (
            source_fixture() as (root, admin, repository, row, tokens),
            patch("tauceti_worker.github_state.read", side_effect=AssertionError("shared state consulted")),
            patch(
                "tauceti_queue.github_adapter.GitHubClient.request",
                side_effect=AssertionError("legacy GitHub source consulted"),
            ),
        ):
            # Only repository admin, never site admin: required by Forgejo's
            # API for querying another native user's repository permission.
            admin.request("PUT", "repos/operator/source/collaborators/queue", {"permission": "admin"})
            api = Forgejo(repository.forge, token=tokens["queue"])
            reviewer = Forgejo(repository.forge, token=tokens["reviewer"])
            contributor = Forgejo(repository.forge, token=tokens["contributor"])
            actor = SourceActor.from_native(repository.forge, reviewer.current_user())
            queue_actor = SourceActor.from_native(repository.forge, api.current_user())
            identity = RepositoryIdentity(repository, row["id"])
            publishers = NativePublishers(
                identity,
                frozenset({actor.principal}),
                frozenset({queue_actor.principal}),
                frozenset({queue_actor.principal}),
                generation="1" * 32,
            )
            admission = NativeAdmissionPolicy(publishers, rubrics=("proof",), prefix="Math/", allow_files=())
            clone = root / "controller"
            subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
            source = ForgejoSourceAdapter(identity, api, clone, token=tokens["queue"], admission_policy=admission)
            publisher = ForgejoSourceAdapter(
                identity, reviewer, clone, token=tokens["reviewer"], admission_policy=admission
            )
            name = repository.name
            base = source.ref_sha(name, "heads/main")
            contributor.request(
                "POST", f"repos/{name}/branches", {"new_branch_name": "proof", "old_branch_name": "main"}
            )
            contributor.request(
                "POST",
                f"repos/{name}/contents/Math/A.lean",
                {
                    "branch": "proof",
                    "message": "Add proof",
                    "content": base64.b64encode(b"example : True := trivial\n").decode(),
                },
            )
            pull = contributor.request(
                "POST", f"repos/{name}/pulls", {"head": "proof", "base": "main", "title": "Prove True"}
            )
            number = pull["number"]
            head = pull["head"]["sha"]
            meta = {
                "head_sha": head,
                "merge_base_sha": base,
                "states": {"proof": "green"},
                "round": "one",
                "mode": "review",
            }
            body = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->\nProof checked."
            # Old/imported comments are not native authority, even when posted by
            # an otherwise configured publisher and tied to the same Git SHA.
            reviewer.post_comment(repository, number, body)
            for context in ("build", "scope"):
                api.request(
                    "POST",
                    f"repos/{name}/statuses/{head}",
                    {"context": context, "state": "success", "description": source.ci_status_description},
                )
            snapshot = source.admission_snapshot(name, number)
            self.assertFalse(admission.evaluate(snapshot)[0]["merge"])
            intent = publisher.prepare_publication(name, number, head, body, "review-native", sequence=1)
            comment_id = publisher.publish(intent)
            # A write-only publisher reconciles its own accepted receipt without
            # the repo-admin permission needed to inspect another user's ACL.
            self.assertEqual(publisher.publish(intent), comment_id)
            snapshot = source.admission_snapshot(name, number)
            self.assertTrue(admission.evaluate(snapshot)[0]["merge"])
            self.assertEqual(snapshot.paths, ("Math/A.lean",))
            with patch.object(source, "fetch_refs", side_effect=AssertionError("refetched immutable Git objects")):
                self.assertTrue(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            # A copied native body cannot borrow the configured actor's identity.
            contributor.post_comment(repository, number, admission.bind_comment(body, actor))
            self.assertEqual(len(source.admission_snapshot(name, number).comments), 1)
            # Forgejo writers can edit another author's comments. The immutable
            # native status receipt, not the surviving author field, protects it.
            contributor.edit_comment(
                repository, int(comment_id), intent["body"].replace('"green"', '"blocking_request"')
            )
            with self.assertRaises(Incomplete):
                source.admission_snapshot(name, number)
            repaired = publisher.prepare_publication(
                name, number, head, body + "\nRestored intended publication.", "review-native-restored", sequence=2
            )
            publisher.publish(repaired)
            self.assertTrue(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            red = body.replace('"green"', '"blocking_request"')
            withdrawn = publisher.prepare_publication(name, number, head, red, "review-native-red", sequence=3)
            red_id = publisher.publish(withdrawn)
            self.assertFalse(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            contributor.request("DELETE", f"repos/{name}/issues/comments/{red_id}")
            with self.assertRaises(Incomplete):
                source.admission_snapshot(name, number)
            restored = publisher.prepare_publication(
                name, number, head, body + "\nNew confirmed verdict.", "review-native-current", sequence=4
            )
            publisher.publish(restored)
            self.assertTrue(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            # Latest trusted status wins; an ordinary contributor's same-name
            # green status cannot hide a trusted failure.
            api.request(
                "POST",
                f"repos/{name}/statuses/{head}",
                {"context": "build", "state": "failure", "description": source.ci_status_description},
            )
            contributor.request(
                "POST",
                f"repos/{name}/statuses/{head}",
                {"context": "build", "state": "success", "description": source.ci_status_description},
            )
            self.assertFalse(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            api.request(
                "POST",
                f"repos/{name}/statuses/{head}",
                {"context": "build", "state": "success", "description": source.ci_status_description},
            )
            # Revocation is a current native ACL read, never a cached login grant.
            admin.request("PUT", f"repos/{name}/collaborators/reviewer", {"permission": "read"})
            self.assertFalse(admission.evaluate(source.admission_snapshot(name, number))[0]["merge"])
            admin.request("PUT", f"repos/{name}/collaborators/reviewer", {"permission": "write"})
            original = api.request

            def outage(method, path, *args, **kwargs):
                if "/permission" in path:
                    raise ForgeError("fixture source outage", status=503)
                return original(method, path, *args, **kwargs)

            with patch.object(api, "request", side_effect=outage):
                with self.assertRaises(ForgeError):
                    source.admission_snapshot(name, number)
            source.ensure_label(name, "ready", "aabbcc", "ready for queue")
            source.set_labels(name, number, ["ready"], [])
            self.assertIn("ready", source.fetch_pr(name, number).labels)
            source.set_labels(name, number, [], ["ready"])
            posted = source.post_comment(name, number, "Queue observation", "native-source-test")
            self.assertEqual(source.post_comment(name, number, "Queue observation", "native-source-test"), posted)
            self.assertTrue(
                source.repository_admin(queue_actor.login, name, actor_id=int(queue_actor.principal.native_id))
            )
            self.assertFalse(source.repository_admin(actor.login, name, actor_id=int(actor.principal.native_id)))
            # Real protected candidate refs and main, driven by the unchanged
            # controller and ledger. Only execution receipts are a fixture here.
            for branch in ("main", "tauceti-ci/**"):
                admin.request(
                    "POST",
                    f"repos/{name}/branch_protections",
                    {
                        "branch_name": branch,
                        "enable_push": True,
                        "enable_push_whitelist": True,
                        "push_whitelist_usernames": ["queue"],
                    },
                )
            configuration = CONFIG.replace(b"https://source.example", repository.forge.url.encode()).replace(
                b"team/Math", name.encode()
            )
            configured = replace(policy(configuration), source=identity, credential_origin=repository.forge)
            store = Store(str(root / "queue.sqlite"))
            try:
                clock = FakeClock()
                ledger = RequestLedger(store, clone, configured, configuration, workflow_revision="b" * 40)
                execution = VerifiedExecution()
                ci = SourceCandidateCI(ledger, execution, clock=clock)
                controller = Controller(
                    store, source, str(clone), name, revision="native-source-fixture", clock=clock, candidate_ci=ci
                )
                controller.set_setting("publication_enabled", True, "disposable fixture")
                recovery_body = f"Merge-queue recovery for head `{head}`\n<!--tauceti-rebase:v1 {head}-->"
                controller._enqueue(
                    "comment",
                    "native-recovery-retry",
                    {
                        "number": number,
                        "body": recovery_body,
                        "marker": "native-recovery-retry",
                    },
                )
                recovery_entry = store.row("outbox", {"key": "native-recovery-retry"})
                original_publish = source.publish

                def lose_accepted_publication(intent):
                    original_publish(intent)
                    raise OSError("lost accepted recovery response")

                with patch.object(source, "publish", side_effect=lose_accepted_publication):
                    controller._drain_outbox(clock())
                pending_recovery = store.row("outbox", {"id": recovery_entry["id"]})
                self.assertEqual(pending_recovery["state"], "pending")
                frozen_recovery = json.loads(pending_recovery["payload"])["native_intent"]
                comments_after_accept = len(api.comments(repository, number))
                clock.advance(60)
                with patch.object(
                    source, "prepare_outbox_comment", side_effect=AssertionError("intent was re-prepared")
                ):
                    controller._drain_outbox(clock())
                settled = store.row("outbox", {"id": recovery_entry["id"]})
                self.assertEqual(settled["state"], "done")
                self.assertEqual(json.loads(settled["payload"])["native_intent"], frozen_recovery)
                self.assertEqual(len(api.comments(repository, number)), comments_after_accept)
                clear = source.prepare_publication(
                    name,
                    number,
                    head,
                    "",
                    "native-recovery-clear",
                    sequence=recovery_entry["id"] + 1,
                    kind="recovery",
                    clear=True,
                )
                source.publish(clear)
                poller = AdmissionPoller(controller, source, name, admission, clock=clock)
                summary = poller.run_once()
                self.assertEqual(summary["errors"], [])
                self.assertTrue(store.rows("inputs"), summary)
                poller.request_discovery()
                repeated = poller.run_once()
                self.assertEqual(repeated["evaluated"], 0, repeated)
                controller.tick()
                attempts = store.rows("ci_attempts")
                self.assertEqual(len(attempts), 1, controller.status())
                frozen = ledger.load(attempts[0]["id"])
                self.assertEqual(source.ref_sha(name, frozen.request.retained_ref), frozen.request.head)
                provenance = source._git("show", "-s", "--format=%B", frozen.request.head).stdout.decode()
                self.assertIn(f"Source-PR: {repository.key}#{number}", provenance)
                accepted_author = source._git("show", "-s", "--format=%an <%ae>", head).stdout
                self.assertEqual(
                    source._git("show", "-s", "--format=%an <%ae>", frozen.request.head).stdout, accepted_author
                )
                execution.add(frozen, classification="passed")
                reviewing = publisher.prepare_publication(
                    name, number, head, body + "\nRefreshed verdict.", "review-native-current", sequence=5
                )
                with patch.object(reviewer, "post_comment", side_effect=ForgeError("publication outage", status=503)):
                    with self.assertRaises(ForgeError):
                        publisher.publish(reviewing)
                clock.advance(60)
                controller.tick()
                self.assertNotEqual(source.ref_sha(name, "heads/main"), frozen.request.head)
                self.assertIsNone(store.get_setting(f"source-ci:promotion-hold:{attempts[0]['id']}"))
                publisher.publish(reviewing)
                clock.advance(60)
                controller.tick()
                self.assertEqual(source.ref_sha(name, "heads/main"), frozen.request.head)
                self.assertEqual(store.rows("batches")[0]["state"], "landed")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
