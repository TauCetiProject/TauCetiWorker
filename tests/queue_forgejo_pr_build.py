#!/usr/bin/env python3
"""PR builds of Forgejo PR heads (decision 215): dispatch, result copy-back, skips and spoofing."""

import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_mixed_sources import MixedFixture, NativeFake  # noqa: E402
from tauceti_tools.forge import Forge, RepositoryRef  # noqa: E402

from tauceti_queue import forgejo_pr_build  # noqa: E402
from tauceti_queue.mixed_native import MixedNativeSource  # noqa: E402
from tauceti_queue.mixed_sources import AdmissionInputs, NativePoller  # noqa: E402
from tauceti_queue.ports import Status  # noqa: E402
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

    def test_a_head_listed_again_after_a_push_is_built(self):
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 0, "test")
        first = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle()
        self.native.push_pr(1, {"TauCeti/One.lean": "one, changed\n"})
        self.cycle()
        self.assertEqual(self.row(first)["state"], "superseded")
        self.native.prs[1]["head_sha"] = first  # the push is undone (a force-push back)
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 4, "test")
        self.cycle()
        self.cycle()
        self.assertEqual([r[:2] for r in self.github.main_integration_requests], [(1, first)])

    def test_a_label_added_before_dispatch_skips_the_head(self):
        head = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        self.cycle(passes=1)
        self.assertEqual(self.row(head)["state"], "retaining")
        self.native.labels[1] = {"self-tested"}
        self.cycle()
        self.assertEqual((self.row(head)["state"], self.row(head)["reason"]), ("skipped", "self-tested"))
        self.assertEqual(self.github.main_integration_requests, [])

    def test_an_unfetchable_head_waits_and_the_others_go_on(self):
        first = self.native.create_pr(1, {"TauCeti/One.lean": "one\n"})
        second = self.native.create_pr(2, {"TauCeti/Two.lean": "two\n"})
        missing = {first}
        exists = forgejo_pr_build.chain.object_exists

        def fetch(number, sha):
            if sha in missing:
                raise RuntimeError("Forgejo Git is unavailable")
            return True

        with (
            patch.object(
                forgejo_pr_build.chain, "object_exists", lambda clone, sha: sha not in missing and exists(clone, sha)
            ),
            patch.object(self.controller, "_fetch_head", fetch),
        ):
            self.cycle()
        self.assertEqual(self.row(first)["state"], "wanted")
        self.assertGreater(self.row(first)["retry_at"], self.clock())
        self.assertEqual([r[:2] for r in self.github.main_integration_requests], [(2, second)])

    def test_in_flight_builds_never_exceed_one_status_query(self):
        self.controller.set_setting("forgejo_pr_build_max_in_flight", 500, "test")
        self.assertEqual(forgejo_pr_build.MAX_IN_FLIGHT, 50)
        for number in range(1, 53):
            self.native.create_pr(number, {f"TauCeti/F{number}.lean": f"f{number}\n"})
        self.cycle(passes=3)
        self.assertEqual(len(self.github.main_integration_requests), 50)

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


class ForgejoMembersOnMain(MixedFixture, unittest.TestCase):
    """The main-integration backfill (decision 208 E) builds queued Forgejo members too."""

    def setUp(self):
        super().setUp()
        self.registry_ = self.registry()
        self.poller = self.registry_.poller = NativePoller(self.registry_, self.controller)
        self.controller.admission = AdmissionInputs(None, self.poller)
        self.controller.set_setting("publication_enabled", True, "test")
        # Forgejo PR 1 heads the queue, so PR 2 waits in the open batch behind it.
        self.native.create_pr(1, {"TauCeti/A.lean": "theorem a : True := trivial\n"})
        self.poller.run_once()
        self.controller.tick()
        self.native_head = self.native.create_pr(2, {"TauCeti/F.lean": "theorem f : True := trivial\n"})
        self.clock.advance(self.registry_.poll_seconds + 1)
        self.poller._discovery_at = 0
        self.poller.run_once()
        self.controller.tick()
        member = self.controller.input_for(self.native_id.repository.key, 2)
        self.assertEqual(member["state"], "inserted")
        self.assertFalse(self.controller.store.row("batches", {"id": member["batch_id"]})["sealed"])
        self.controller.set_setting("main_integration_interval_seconds", 0, "test")
        self.controller.set_setting("main_integration_enabled", True, "test")

    def test_a_forgejo_member_is_dispatched_with_its_source(self):
        self.controller.tick()
        requests = list(zip(self.github.main_integration_requests, self.github.main_integration_options, strict=True))
        self.assertEqual(
            requests, [((2, self.native_head, self.controller._canonical), {"source": "forgejo", "priority": "idle"})]
        )

    def test_a_member_waits_until_its_head_is_on_github(self):
        row = self.controller.input_for(self.native_id.repository.key, 2)
        key = f"retain:{row['retained_ref']}:{row['head_sha']}"
        self.controller.store.update("outbox", {"key": key}, state="pending")
        from tauceti_queue import main_integration

        self.assertFalse(main_integration._on_github(self.controller, row))
        self.controller.store.update("outbox", {"key": key}, state="done")
        self.assertTrue(main_integration._on_github(self.controller, row))

    def test_a_forgejo_members_verdict_counts_only_from_the_ci_app(self):
        from tauceti_queue import main_integration

        self.controller.tick()
        record = main_integration.record_of(self.controller.input_for(self.native_id.repository.key, 2))
        main = record["main"]
        trusted = {"tauceti-live-ci"}
        for creator, state, description, counts in (
            ("kim-em", "failure", "failed", False),
            ("github-actions", "failure", "failed", False),
            ("github-actions", "error", "deferred: Fleet PR pool busy", True),
            ("tauceti-live-ci", "failure", "failed", True),
        ):
            with self.subTest(creator=creator, state=state):
                status = Status(state, f"B={main[:12]} {description}", "", record["dispatched_at"], None, creator)
                self.assertIs(main_integration._answers(status, record, trusted), counts)

    def test_a_head_with_a_pending_pr_build_waits_for_it(self):
        self.controller.store.insert(
            "pr_builds",
            source=self.native_id.repository.key,
            number=2,
            head_sha=self.native_head,
            state="pending",
            main_sha=self.controller._canonical,
        )
        self.controller.tick()
        self.assertEqual(self.github.main_integration_requests, [])


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
