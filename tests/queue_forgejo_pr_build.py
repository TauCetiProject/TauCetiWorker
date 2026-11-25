#!/usr/bin/env python3
"""PR builds of Forgejo PR heads (decision 215): dispatch, result copy-back, skips and spoofing."""

import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_mixed_sources import MixedFixture, NativeFake  # noqa: E402
from tauceti_tools.forge import Forge, RepositoryRef  # noqa: E402

from tauceti_queue import forgejo_pr_build  # noqa: E402
from tauceti_queue.mixed_native import MixedNativeSource  # noqa: E402
from tauceti_queue.mixed_sources import AdmissionInputs, NativePoller  # noqa: E402
from tauceti_queue.store import loads  # noqa: E402


class Unapproved:
    """Reviews never admit here: these tests are about the PR build, not the queue."""

    revision = "fixture-policy"

    def evaluate(self, snapshot):
        from tauceti_queue.store import dumps

        result = {"merge": False, "review_safe": True, "reason": "unreviewed", "recovery_heads": []}
        return result, dumps({}), {"kind": "admission_policy"}, {}, {}


class StatusNative(NativeFake):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.posted = []
        self.refuse = set()  # states whose posts fail

    def list_open_pull_requests(self, repo, **kwargs):
        # Like the Forgejo adapter's listing, each item's details carry the PR's labels.
        return [
            dataclasses.replace(
                pull, admission={**(pull.admission or {}), "labels": sorted(self.labels.get(pull.number, ()))}
            )
            for pull in super().list_open_pull_requests(repo, **kwargs)
        ]

    def post_source_status(self, repo, sha, *, context, state, description, target_url):
        self._call("post_source_status", sha, state)
        if state in self.refuse:
            raise RuntimeError("Forgejo is unavailable")
        self.posted.append((sha, context, state, description, target_url))


class ForgejoPRBuild(MixedFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.native = StatusNative(self.repo, self.clock, self.native_id.repository.name)
        self.native.repository_identity = self.native_id
        self.native.admission_policy = Unapproved()
        self.registry_ = self.registry()
        self.poller = self.registry_.poller = NativePoller(self.registry_, self.controller)
        self.controller.admission = AdmissionInputs(None, self.poller)
        self.controller.set_setting("publication_enabled", True, "test")
        self.controller.set_setting("forgejo_pr_build_interval_seconds", 0, "test")
        self.controller.set_setting("forgejo_pr_build_enabled", True, "test")
        self.runs = 0

    def rows(self, number=1):
        return self.controller.store.rows("pr_builds", {"number": number}, order="id")

    def row(self, head, number=1):
        return next(r for r in self.rows(number) if r["head_sha"] == head)

    def cycle(self, passes=2):
        """List the Forgejo PRs, then run the controller (its outbox pushes and posts) a few times."""
        self.clock.advance(self.registry_.poll_seconds + 1)
        self.poller._discovery_at = 0
        self.poller.run_once()
        for _ in range(passes):
            self.controller.tick()
            self.clock.advance(1)

    def post(self, head, state, description="passed", *, creator="tauceti-live-ci", main=None):
        main = main or self.row(head)["main_sha"]
        self.runs += 1
        self.github.post_status(
            head, state, str(self.runs), f"B={main[:12]} T=0123456789ab {description}", "main-integration", creator
        )
        key = (head, "main-integration")
        self.github.statuses[key] = dataclasses.replace(
            self.github.statuses[key], target_url=f"https://github.com/run/{self.runs}"
        )

    def builds(self, head):
        return [p for p in self.native.posted if p[0] == head and p[1] == "build"]

    def test_an_unlabelled_head_is_built_on_github_and_green_is_copied_back(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "theorem one : True := trivial\n"})
        self.cycle()
        main = self.controller._canonical
        self.assertEqual(self.github.main_integration_requests, [(1, head, main)])
        self.assertEqual(self.github.main_integration_options, [{"source": "forgejo", "priority": "pr"}])
        # The head reached GitHub under the queue's input ref before the build was asked for.
        ref = self.controller.input_ref(self.registry_.number(1), head)
        self.assertIn(("update_ref", (ref, head)), [c for c in self.github.calls if c[0] == "update_ref"])
        self.assertEqual([p[2] for p in self.builds(head)], ["pending"])
        self.post(head, "success")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "success")
        self.assertEqual(
            self.builds(head)[-1][2:],
            ("success", f"B={main[:12]} T=0123456789ab passed", "https://github.com/run/1"),
        )
        # Built once: later passes and listings ask nothing more for this head.
        self.cycle()
        self.assertEqual(len(self.github.main_integration_requests), 1)

    def test_red_is_copied_back_as_failure(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "broken\n"})
        self.cycle()
        self.post(head, "failure", "failed: sandboxed build, audit, or simp-NF lint failed")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "failure")
        self.assertEqual(self.builds(head)[-1][2], "failure")
        self.assertEqual(len(self.github.main_integration_requests), 1)

    def test_self_tested_draft_and_other_base_heads_are_not_built(self):
        tested = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"}, labels=["self-tested"])
        draft = self.native.create_pr(2, {"TauCeti/Two.lean": "two\n"})
        self.native.prs[2]["draft"] = True
        other = self.native.create_pr(3, {"TauCeti/Three.lean": "three\n"})
        self.native.prs[3]["base_ref"] = "release"
        lint = self.native.create_pr(4, {"TauCeti/Four.lean": "four\n"}, labels=["self-tested", "full-lint"])
        self.cycle()
        self.assertEqual(
            {n: (self.rows(n)[0]["state"], self.rows(n)[0]["reason"]) for n in (1, 2, 3, 4)},
            {1: ("skipped", "self-tested"), 2: ("skipped", "draft"), 3: ("skipped", "base"), 4: ("pending", "")},
        )
        self.assertEqual([r[:2] for r in self.github.main_integration_requests], [(4, lint)])
        # Withdrawing the label before a build starts brings the head back, as on GitHub.
        self.native.labels[1] = set()
        self.cycle()
        self.assertIn((1, tested), [r[:2] for r in self.github.main_integration_requests])
        self.assertEqual(self.builds(draft) + self.builds(other), [])

    def test_a_push_before_dispatch_supersedes_the_old_head(self):
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 0, "test")
        old = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.assertEqual(self.row(old)["state"], "wanted")
        new = self.native.push_pr(1, {"TauCeti/One.lean": "one, fixed\n"})
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 4, "test")
        self.cycle()
        self.assertEqual(self.row(old)["state"], "superseded")
        self.assertEqual([r[:2] for r in self.github.main_integration_requests], [(1, new)])

    def test_untrusted_stale_or_other_main_statuses_are_not_answers(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        # A repository writer posting the context by hand does not make it a verdict.
        self.post(head, "success", creator="kim-em")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "pending")
        # Nor does the backfill's deferral of the same head on another main.
        self.post(head, "error", "deferred: Fleet PR pool busy (idle 2)", main="0" * 40, creator="github-actions")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "pending")
        row = self.row(head)
        status = self.github.statuses[(head, "main-integration")]
        early = dataclasses.replace(
            status,
            description=f"B={row['main_sha'][:12]} passed",
            updated_at=row["dispatched_at"] - 600,
            creator="tauceti-live-ci",
        )
        self.assertFalse(forgejo_pr_build.answers(early, row, {"tauceti-live-ci"}))
        self.assertEqual([p[2] for p in self.builds(head)], ["pending"])

    def test_a_trusted_verdict_on_another_main_answers(self):
        # The backfill built the same head on a newer main and its status is now the latest.
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.post(head, "success", main="1" * 40)
        self.cycle()
        self.assertEqual(self.row(head)["state"], "success")
        self.assertEqual(self.builds(head)[-1][3][:15], "B=111111111111 ")

    def test_an_error_is_asked_once_more_and_then_posted(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.post(head, "error", "error: the head conflicts with main")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "wanted")
        self.clock.advance(self.controller.setting("forgejo_pr_build_retry_seconds"))
        self.cycle()
        self.assertEqual(len(self.github.main_integration_requests), 2)
        # The first run's status, read again, is not the second run's answer.
        self.cycle()
        self.assertEqual(self.row(head)["state"], "pending")
        self.post(head, "error", "error: the head conflicts with main")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "error")
        self.assertEqual(self.builds(head)[-1][2], "error")

    def test_a_deferral_from_the_workflow_token_is_retried(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.post(head, "error", "deferred: Fleet PR pool busy (idle unknown)", creator="github-actions")
        self.cycle()
        self.assertEqual(self.row(head)["state"], "wanted")
        # A verdict from that token is not trusted.
        row = self.row(head)
        verdict = dataclasses.replace(self.github.statuses[(head, "main-integration")], state="success", target_url="")
        self.assertFalse(forgejo_pr_build.answers(verdict, {**row, "runs": "[]"}, {"tauceti-live-ci"}))

    def test_an_unposted_pending_status_never_lands_after_the_result(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.native.refuse = {"pending"}
        self.cycle(passes=4)
        self.assertEqual(self.row(head)["state"], "pending")
        self.assertEqual(self.builds(head), [])  # the pending post failed and waits for its retry
        self.post(head, "success")
        self.cycle(passes=3)
        self.assertEqual([p[2] for p in self.builds(head)], ["success"])
        # When the failed pending post is retried, the newer result has superseded it.
        self.native.refuse = set()
        self.clock.advance(3600)
        self.cycle()
        self.assertEqual([p[2] for p in self.builds(head)], ["success"])
        results = sorted(e["result"] for e in self.controller.store.rows("outbox", {"kind": "source_status"}))
        self.assertEqual(results, ["build success", "superseded"])

    def test_inert_when_disabled_or_without_publication(self):
        self.controller.set_setting("forgejo_pr_build_enabled", False, "test")
        self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.assertEqual(self.rows(), [])
        self.controller.set_setting("forgejo_pr_build_enabled", True, "test")
        self.controller.set_setting("publication_enabled", False, "test")
        self.cycle()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.github.main_integration_requests, [])

    def test_a_closed_pr_is_not_built(self):
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 0, "test")
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.native.prs[1]["state"] = "closed"
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 4, "test")
        self.cycle()
        self.cycle()
        self.assertEqual(self.row(head)["state"], "superseded")
        self.assertEqual(self.github.main_integration_requests, [])

    def test_the_status_record_names_the_runs_counted(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.post(head, "success")
        self.cycle()
        self.assertEqual(loads(self.row(head)["runs"]), ["https://github.com/run/1"])


class PublisherStatus(unittest.TestCase):
    """The Forgejo write itself: only `build`, only as the publisher, only linking to GitHub."""

    def source(self, actor=4):
        native = MixedNativeSource.__new__(MixedNativeSource)
        identity = SimpleNamespace(native_id=41)
        repository = RepositoryRef(Forge("forgejo", "https://forge.example"), "team/Math")
        calls = []
        native.client = SimpleNamespace(
            current_user=lambda: {"id": actor},
            repository=lambda ref: {"id": 41, "full_name": "team/Math"},
            request=lambda method, path, body=None: calls.append((method, path, body)) or {},
        )
        native.profile = SimpleNamespace(publisher=4)
        native.mode = "admit"
        native.repository, native.repository_identity = repository, identity
        return native, calls

    def test_posts_build_as_the_publisher(self):
        native, calls = self.source()
        sha = "a" * 40
        native.post_source_status(
            "team/Math",
            sha,
            context="build",
            state="success",
            description="x" * 300,
            target_url="https://github.com/r/1",
        )
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", f"repos/team/Math/statuses/{sha}"))
        self.assertEqual((body["context"], body["state"], len(body["description"])), ("build", "success", 140))

    def test_refuses_other_contexts_links_and_actors(self):
        native, calls = self.source()
        sha = "a" * 40
        for kwargs in (
            dict(context="scope", state="success", description="", target_url=""),
            dict(context="build", state="warning", description="", target_url=""),
            dict(context="build", state="success", description="", target_url="https://evil.example/"),
        ):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                native.post_source_status("team/Math", sha, **kwargs)
        other, other_calls = self.source(actor=5)
        from tauceti_tools.forge import Incomplete

        with self.assertRaises(Incomplete):
            other.post_source_status("team/Math", sha, context="build", state="success", description="", target_url="")
        self.assertEqual(calls + other_calls, [])


if __name__ == "__main__":
    unittest.main()
