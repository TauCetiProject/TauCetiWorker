#!/usr/bin/env python3
"""Default inertness and qualified mixed admission over real local Git history."""

import copy
import dataclasses
import hashlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_queue.config import Config, load, validate
from tauceti_queue.fakes import FakeGitHub, make_controller
from tauceti_queue.mixed_identity import SourceNumber, from_source_pr
from tauceti_queue.mixed_profile import MixedProfile
from tauceti_queue.mixed_sources import MixedSources, NativePoller
from tauceti_queue.store import dumps, loads


class Decision:
    revision = "fixture-policy"

    def evaluate(self, snapshot):
        ok = snapshot.pull.state == "open" and not snapshot.pull.draft and "hold" not in snapshot.pull.labels
        result = {"merge": ok, "review_safe": ok, "reason": "fixture policy", "recovery_heads": []}
        return result, dumps({"kind": "fixed approval"}), {"kind": "admission_policy"}, {}, {}


class NativeFake(FakeGitHub):
    def list_open_pull_requests(self, repo, *, review_app_id=0, **kwargs):
        # Like the Forgejo adapter, the listing carries each PR's listed-facts version.
        return [
            dataclasses.replace(
                pull, admission={"discovery_version": self._discovery_version(self.fetch_pr(repo, pull.number))}
            )
            for pull in super().list_open_pull_requests(repo, review_app_id=review_app_id, **kwargs)
        ]

    def _discovery_version(self, pull):
        return pull.head_sha + ":" + pull.state

    def fetch_pr_or_none(self, repo, number):
        return self.fetch_pr(repo, number) if number in self.prs else None


class MixedFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root, publication=False)
        self.addCleanup(self.controller.store.close)
        self.native_id = cc.RepositoryIdentity(
            RepositoryRef(Forge("forgejo", "https://forge.example"), "team/Math"), 41
        )
        self.github_id = cc.RepositoryIdentity(RepositoryRef(GITHUB, self.controller.repository), 42)
        self.github.repository_identity = self.github_id
        self.profile = MixedProfile(self.github_id, self.native_id, 4, self.root / "unused-token", {})
        self.native = NativeFake(self.repo, self.clock, self.native_id.repository.name)
        self.native.repository_identity = self.native_id
        self.native.admission_policy = Decision()

    def registry(self, mode="admit"):
        config = Config(repository=self.controller.repository, canonical_branch="main", forgejo_mode=mode)
        registry = MixedSources(
            self.profile,
            config,
            self.controller.store,
            self.github,
            self.repo.clone,
            clock=self.clock,
            native=self.native,
        )
        self.controller.mixed = registry
        self.controller.source = registry.port if mode == "admit" else self.github
        registry.controller = self.controller
        return registry


class Mixed(MixedFixture, unittest.TestCase):
    def test_default_configuration_is_inert_and_does_not_validate_unused_profile(self):
        cfg = load(environ={"TAUCETI_QUEUE_FORGEJO_PROFILE": "/missing/profile"})
        self.assertEqual(cfg.forgejo_mode, "disabled")
        with patch(
            "tauceti_queue.mixed_profile.read_control_policy", side_effect=AssertionError("private file access")
        ):
            self.assertFalse(any("forgejo" in reason.lower() for reason in validate(cfg, check_credentials=False)))
        cfg.forgejo_mode = "typo"
        self.assertTrue(any("forgejo_mode" in reason for reason in validate(cfg, check_credentials=False)))

    def test_disabled_service_startup_never_opens_native_profile_or_client(self):
        from types import SimpleNamespace

        from tauceti_queue import __main__ as cli

        cfg = Config(
            repository=self.controller.repository,
            canonical_branch="main",
            forgejo_profile="/missing/private/profile",
            content_clone=self.repo.clone,
            admission_source="policy",
        )
        service = cli.Service(cfg, SimpleNamespace(revision=SimpleNamespace(sha="fixture")))
        service.store, service.github = self.controller.store, self.github
        with (
            patch("tauceti_queue.mixed_profile.MixedProfile.load", side_effect=AssertionError("private file read")),
            patch("tauceti_queue.mixed_sources.MixedSources", side_effect=AssertionError("native client constructed")),
        ):
            service.build_controller()
        self.assertIsNone(service.forgejo_poller)
        self.assertIsNone(service.controller.mixed)

    def test_number_identity_survives_database_json_copy_and_recreation(self):
        number = SourceNumber(1, self.native_id)
        self.assertNotEqual(number, 1)
        self.assertNotEqual(1, number)
        self.assertEqual(len({number, 1}), 2)
        self.assertEqual(copy.deepcopy(number), number)
        self.assertEqual(from_source_pr(number.source_pr), number)
        self.assertEqual(loads(dumps({"number": number}))["number"], number)
        first = self.controller.store.insert(
            "inputs",
            repository=self.controller.repository,
            number=number,
            head_sha="a" * 40,
            state="waiting",
            approved_at=1,
        )
        row = self.controller.store.row("inputs", {"id": first})
        self.assertEqual(row["number"], number)
        recreated = SourceNumber(1, dataclasses.replace(self.native_id, native_id=45))
        self.assertNotEqual(recreated, number)
        self.assertNotEqual(recreated.ref_key, number.ref_key)
        from tauceti_queue.admission_planning import fingerprint

        self.assertNotEqual(fingerprint([{"pr": 1}]), fingerprint([{"pr": number}]))
        self.assertNotEqual(fingerprint([{"pr": recreated}]), fingerprint([{"pr": number}]))

    def test_observation_evaluates_without_admission_or_source_publication(self):
        registry = self.registry("observe")
        self.native.create_pr(1, {"Native.lean": "native\n"})
        self.native.calls.clear()
        result = NativePoller(registry, self.controller).run_once()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["would_admit"], 1)
        self.assertEqual(self.controller.store.count("inputs"), 0)
        self.assertEqual(self.controller.store.count("outbox"), 0)
        self.assertFalse(
            any(
                name in {"post_comment", "set_labels", "close_pr", "update_ref", "dispatch_candidate"}
                for name, _ in self.native.calls
            )
        )

    def test_paused_admission_observes_new_work_and_resumes_with_fresh_heads(self):
        registry = self.registry()
        poller = registry.poller = NativePoller(registry, self.controller)
        self.controller.set_setting("forgejo_admission_enabled", False, "operator")
        old = self.native.create_pr(1, {"Native.lean": "old\n"})
        self.assertEqual(poller.run_once()["approved"], 0)
        self.assertEqual(self.controller.store.count("inputs"), 0)
        self.assertEqual(poller.state.get(1)["disposition"], "admission-paused")
        self.assertEqual(
            self.controller.approve(self.native_id.repository.key, 1, old, "manual")["reason"], "admission-paused"
        )
        fresh = self.native.create_pr(1, {"Native.lean": "fresh\n"})
        self.controller.set_setting("forgejo_admission_enabled", True, "operator")
        self.assertEqual(poller.run_once()["approved"], 1)
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["head_sha"], fresh)

    def test_pause_resume_invalidates_an_inflight_new_admission_snapshot(self):
        import threading

        registry = self.registry()
        poller = registry.poller = NativePoller(registry, self.controller)
        self.native.create_pr(1, {"Native.lean": "old\n"})
        entered, release = threading.Event(), threading.Event()
        snapshot = self.native.admission_snapshot

        def slow(*args):
            value = snapshot(*args)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release not delivered")
            return value

        with patch.object(self.native, "admission_snapshot", side_effect=slow):
            thread = threading.Thread(target=poller.run_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.controller.set_setting("forgejo_admission_enabled", False, "operator")
                self.controller.set_setting("forgejo_admission_enabled", True, "operator")
                fresh = self.native.create_pr(1, {"Native.lean": "new head\n"})
                poller.stopping.set()
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.controller.store.count("inputs"), 0)
        poller.stopping.clear()
        self.assertEqual(poller.run_once()["approved"], 1)
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["head_sha"], fresh)

    def test_resume_same_head_does_not_requeue_closed_history_or_duplicate_discovery(self):
        registry = self.registry()
        poller = registry.poller = NativePoller(registry, self.controller)
        self.native.create_pr(1, {"Closed.lean": "closed\n"})
        self.native.prs[1]["state"] = "closed"
        poller.state.observe(1, self.native.prs[1]["head_sha"], "closed")
        work = poller.state.get(1)
        poller.state.complete(work, "closed", "", "closed")
        closed_generation = poller.state.get(1)["generation"]
        self.controller.set_setting("forgejo_admission_enabled", False, "operator")
        head = self.native.create_pr(2, {"Native.lean": "same head\n"})
        self.assertEqual(poller.run_once()["approved"], 0)
        self.controller.set_setting("forgejo_admission_enabled", True, "operator")
        self.assertEqual(poller.state.get(1)["generation"], closed_generation)
        self.assertEqual(poller.state.get(2)["urgent"], 0)
        self.assertEqual(poller.run_once()["approved"], 1)
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 2)["head_sha"], head)
        self.clock.advance(registry.poll_seconds)
        self.assertEqual(poller.run_once()["evaluated"], 0)

    def test_observation_keeps_the_existing_github_main_writer(self):
        from tauceti_queue.controller import Controller

        registry = self.registry("observe")
        self.controller = Controller(
            self.controller.store,
            self.github,
            str(self.repo.clone),
            self.controller.repository,
            revision="observe-restart",
            canonical_branch="main",
            mixed=registry,
            clock=self.clock,
        )
        self.assertIs(self.controller.source, self.github)
        self.controller.set_setting("publication_enabled", True, "fixture")
        head = self.github.create_pr(1, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 1, head, "fixture")
        with patch.object(self.github, "fast_forward_leased", side_effect=AssertionError("changed GitHub writer")):
            self.controller.tick()
            self.github.start_runs()
            self.github.complete_runs()
            self.clock.advance(60)
            self.controller.tick()
        self.assertEqual(self.controller.store.rows("batches")[0]["state"], "landed")

    def test_mixed_attribution_requires_explicit_source_and_never_reinterprets_a_suffix(self):
        registry = self.registry()
        native = registry.number(7)
        github = self.controller.repository + "#12"
        text = f"Interacting-PR: {github}\nFinding: interacts with {native.source_pr} and #12."
        self.assertEqual(self.controller._attribution(text, native, qualified=True)["interacting"], 12)
        self.assertEqual(self.controller._mentioned_prs(native.source_pr, 12, qualified=True), [native])
        self.assertEqual(self.controller._attribution("Interacting-PR: #12", native)["interacting"], None)
        self.assertEqual(self.controller._mentioned_prs("#7 and #12", 12, qualified=True), [])
        self.assertEqual(self.controller._mentioned_prs(native.source_pr, 12), [native])
        self.assertEqual(self.controller._mentioned_prs("other/repo#7", 12, qualified=True), [])
        self.assertEqual(self.controller._mentioned_prs(f"#3 before {github}", 99), [3, 12])

    def test_native_followup_recognizes_exact_qualified_main_provenance(self):
        from tauceti_queue.fakes import run_git

        registry = self.registry()
        head = self.native.create_pr(70, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 70, head, "fixture")
        self.controller.tick()
        batch = self.controller._head()
        run_git(self.repo.clone, "push", "-q", "origin", batch["tip"] + ":refs/heads/main")
        self.controller._canonical = batch["tip"]
        self.assertTrue(self.controller._on_canonical(registry.number(70)))
        self.assertFalse(self.controller._on_canonical(registry.number(7)))

    def test_same_number_from_both_sources_forms_distinct_attributed_members(self):
        registry = self.registry()
        github = self.github.create_pr(1, {"Github.lean": "github\n"})
        native = self.native.create_pr(1, {"Native.lean": "native\n"})
        self.assertTrue(self.controller.approve(self.controller.repository, 1, github, "fixture")["accepted"])
        self.assertTrue(self.controller.approve(self.native_id.repository.key, 1, native, "fixture")["accepted"])
        self.controller.tick()
        members = self.controller.store.rows("members", order="position")
        self.assertEqual(len(members), 2)
        self.assertEqual(len({row["number"] for row in members}), 2)
        self.assertEqual(len({row["source_pr"] for row in members}), 2)
        self.assertEqual(self.controller.input_for(self.controller.repository, 1)["head_sha"], github)
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["head_sha"], native)
        self.assertEqual(self.controller._chain_member(members[1]).source_pr, members[1]["source_pr"])
        self.assertNotEqual(self.controller.input_ref(1, native), self.controller.input_ref(registry.number(1), native))

    def test_publication_is_routed_to_the_original_source_after_recovery(self):
        registry = self.registry()
        self.controller.set_setting("publication_enabled", True, "fixture")
        self.github.create_pr(1, {"Github.lean": "github\n"})
        self.native.create_pr(1, {"Native.lean": "native\n"})
        number = registry.number(1)
        payload = loads(dumps({"number": number, "body": "A native result", "marker": "native-result"}))
        self.controller._perform("comment", payload, self.clock())
        self.assertEqual(len(self.native.comments), 1)
        self.assertEqual(len(self.github.comments), 0)
        self.assertEqual(int(self.native.comments[0]["number"]), 1)

    def test_inert_source_history_is_preserved_and_never_becomes_a_github_input(self):
        self.registry()
        head = self.native.create_pr(1, {"Native.lean": "native\n"})
        self.assertTrue(self.controller.approve(self.native_id.repository.key, 1, head, "fixture")["accepted"])
        self.controller.mixed = None
        self.controller.source = self.github
        self.assertEqual(self.controller._waiting(), [])
        self.assertIsNone(self.controller.input_for(self.controller.repository, 1))
        self.assertEqual(self.controller.store.count("inputs"), 1)

    def test_native_waiting_approval_can_be_withdrawn_without_affecting_github_same_number(self):
        self.registry()
        github = self.github.create_pr(1, {"Github.lean": "github\n"})
        native = self.native.create_pr(1, {"Native.lean": "native\n"})
        self.controller.approve(self.controller.repository, 1, github, "fixture")
        self.controller.approve(self.native_id.repository.key, 1, native, "fixture")
        self.controller.cancel(self.native_id.repository.key, 1)
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["state"], "cancelled")
        self.assertEqual(self.controller.input_for(self.controller.repository, 1)["state"], "waiting")

    def test_github_landings_work_after_primary_reversal_and_native_disable(self):
        self.controller.store.set_setting("mixed:primary_generation", 2)
        self.controller.set_setting("publication_enabled", True, "fixture")
        head = self.github.create_pr(1, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 1, head, "fixture")
        self.controller.tick()
        self.github.start_runs()
        self.github.complete_runs()
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.controller.store.rows("batches")[0]["state"], "landed", self.controller._reasons)
        promotion = self.controller.store.rows("outbox", {"kind": "promote"})[0]
        self.assertEqual(loads(promotion["payload"])["primary_generation"], 2)

    def test_native_discovery_skips_unchanged_snapshots_and_withdraws_unsafe_reviews(self):
        registry = self.registry()
        head = self.native.create_pr(1, {"Native.lean": "native\n"})
        poller = NativePoller(registry, self.controller)
        self.controller.admission = __import__(
            "tauceti_queue.mixed_sources", fromlist=["AdmissionInputs"]
        ).AdmissionInputs(None, poller)
        self.assertEqual(poller.run_once()["approved"], 1)
        count = sum(name == "admission_snapshot" for name, _ in self.native.calls)
        self.clock.advance(registry.poll_seconds)
        self.assertEqual(poller.run_once()["evaluated"], 0)
        self.assertEqual(sum(name == "admission_snapshot" for name, _ in self.native.calls), count)
        original = self.native.admission_policy.evaluate

        def revoked(snapshot):
            result, *rest = original(snapshot)
            return ({**result, "merge": False, "review_safe": False}, *rest)

        self.native.admission_policy.evaluate = revoked
        poller.invalidate(1, head=head, urgent=True)
        poller.run_once()
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["state"], "cancelled")
        self.assertFalse(poller.is_current(1, head))
        poller.withdraw(99, None)
        self.assertEqual(poller.state.get(99)["cancelled_head_sha"], "*")

    def test_native_listing_runs_once_per_approval_poll_interval_and_evaluations_continue(self):
        registry = self.registry()
        poller = NativePoller(registry, self.controller)
        self.native.create_pr(1, {"One.lean": "one\n"})

        def listings():
            return sum(name == "list_open_pull_requests" for name, _ in self.native.calls)

        poller.run_once()
        self.assertEqual(listings(), 1)
        interval = self.controller.setting("approval_poll_interval_seconds")
        self.assertGreater(interval, registry.poll_seconds)
        # Work dirtied between listings is evaluated without listing again.
        self.native.create_pr(2, {"Two.lean": "two\n"})
        poller.invalidate(2, head=self.native.prs[2]["head_sha"])
        self.clock.advance(registry.poll_seconds)
        self.assertEqual(poller.run_once()["evaluated"], 1)
        self.assertEqual(listings(), 1)
        self.assertLessEqual(poller.interval(), interval)
        self.clock.advance(interval - registry.poll_seconds)
        poller.run_once()
        self.assertEqual(listings(), 2)
        # A requested discovery (a canonical push, a setting change) still lists at once.
        poller.request_discovery()
        poller.run_once()
        self.assertEqual(listings(), 3)

    def native_approved(self):
        from tauceti_queue.mixed_sources import AdmissionInputs

        registry = self.registry()
        poller = registry.poller = NativePoller(registry, self.controller)
        self.controller.admission = AdmissionInputs(None, poller)
        head = self.native.create_pr(1, {"Native.lean": "native\n"})
        self.assertEqual(poller.run_once()["approved"], 1)
        return poller, head

    def native_input(self):
        return self.controller.input_for(self.native_id.repository.key, 1)

    def test_native_insertion_reads_the_pr_once_more_and_inserts_it_unchanged(self):
        poller, head = self.native_approved()
        reads = sum(name == "fetch_pr" for name, _ in self.native.calls)
        self.controller.tick()
        self.assertEqual(self.native_input()["state"], "inserted")
        self.assertGreater(sum(name == "fetch_pr" for name, _ in self.native.calls), reads)

    def test_native_insertion_is_held_for_a_change_no_listing_has_seen(self):
        poller, head = self.native_approved()
        self.native.prs[1]["state"] = "closed"  # closed after evaluation, before the next listing
        self.controller.tick()
        row = self.native_input()
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "admission-recheck"))
        self.assertFalse(poller.is_current(1, head))
        poller.run_once()
        self.assertEqual(self.native_input()["state"], "cancelled")

    def test_native_insertion_is_held_while_a_blocking_label_is_on_the_pr(self):
        poller, head = self.native_approved()
        self.native.labels[1] = {"hold"}  # added after approval; the fixture's listed facts omit labels
        self.controller.tick()
        self.assertEqual(self.native_input()["state"], "waiting")
        self.native.labels[1] = set()
        self.controller.tick()
        self.assertEqual(self.native_input()["state"], "inserted")

    def test_native_insertion_is_held_while_the_source_cannot_be_read(self):
        poller, head = self.native_approved()

        def unavailable(repo, number):
            raise OSError("forge unreachable")

        self.native.fetch_pr_or_none = unavailable
        self.controller.tick()
        self.assertEqual(self.native_input()["state"], "waiting")
        del self.native.fetch_pr_or_none
        self.controller.tick()
        self.assertEqual(self.native_input()["state"], "inserted")

    def recent_fixture(self):
        """A native fake that reports update times and serves a recent listing."""
        registry = self.registry()
        poller = NativePoller(registry, self.controller)
        self.updated = {}
        recent_calls = []
        plain = self.native.list_open_pull_requests

        def stamped(pulls):
            return [
                dataclasses.replace(p, admission={**(p.admission or {}), "updated_at": self.updated.get(p.number, "")})
                for p in pulls
            ]

        def full(repo, **kwargs):
            return stamped(plain(repo, **kwargs))

        def recent(repo, *, since):
            recent_calls.append(since)
            from tauceti_queue.forgejo_source import _forge_time

            listed = [p for p in full(repo) if _forge_time(p.admission["updated_at"]) >= since]
            newest = max([_forge_time(t) for t in self.updated.values()] or [0.0])
            return listed, newest, True

        self.native.list_open_pull_requests = full
        self.native.list_recent_pull_requests = recent
        return registry, poller, recent_calls

    def test_recent_listing_finds_a_change_between_full_listings(self):
        registry, poller, recent_calls = self.recent_fixture()

        def listings():
            return sum(name == "list_open_pull_requests" for name, _ in self.native.calls)

        self.native.create_pr(1, {"One.lean": "one\n"})
        self.updated[1] = "2026-11-19T04:00:00Z"
        poller.run_once()  # full listing; evaluates #1
        full = listings()
        self.assertEqual(recent_calls, [])
        # A new PR opens after the full listing: the next pass's recent listing finds it.
        self.native.create_pr(2, {"Two.lean": "two\n"})
        self.updated[2] = "2026-11-19T04:05:00Z"
        self.clock.advance(registry.poll_seconds)
        summary = poller.run_once()
        self.assertEqual(len(recent_calls), 1)
        self.assertEqual(summary.get("recent"), 2)  # #1 too: it is within the margin of the newest update
        self.assertEqual(poller.state.get(2)["evaluated_generation"], poller.state.get(2)["generation"])
        self.assertLessEqual(listings() - full, 1)  # only the fixture's own use, no full re-listing
        # Turned off, passes between full listings list nothing.
        self.controller.set_setting("forgejo_recent_listing", False, "operator")
        self.clock.advance(registry.poll_seconds)
        poller.run_once()
        self.assertEqual(len(recent_calls), 1)

    def test_a_scoreboard_edit_from_the_comment_feed_dirties_its_settled_pr_once(self):
        registry, poller, recent_calls = self.recent_fixture()
        feed = []
        self.native.list_recent_comments = lambda repo, *, since, **kw: (list(feed), 0.0, True)
        head = self.native.create_pr(1, {"One.lean": "one\n"})
        self.updated[1] = "2026-11-19T04:00:00Z"
        poller.run_once()
        work = poller.state.get(1)
        self.assertEqual(work["generation"], work["evaluated_generation"])
        board = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {}-->"
        feed[:] = [
            (1, {"id": 50, "body": board, "updated_at": "2026-11-19T04:30:00Z"}),
            (1, {"id": 51, "body": "thanks!", "updated_at": "2026-11-19T04:30:00Z"}),
        ]
        self.clock.advance(registry.poll_seconds)
        poller.stopping.set()  # read the feed, evaluate nothing yet
        summary = poller.run_once()
        poller.stopping.clear()
        self.assertEqual(summary["comment_feed"], {"read": 2, "dirtied": 1})
        self.assertNotEqual(poller.state.get(1)["generation"], poller.state.get(1)["evaluated_generation"])
        poller.run_once()  # evaluated; the same edit, listed again, is not dirtied again
        self.clock.advance(registry.poll_seconds)
        self.assertEqual(poller.run_once()["comment_feed"]["dirtied"], 0)
        self.assertTrue(poller.is_current(1, head))

    def test_feed_edits_that_drop_a_marker_count_and_an_incomplete_read_keeps_its_place(self):
        registry, poller, recent_calls = self.recent_fixture()
        feed, complete = [], [True]
        newest = datetime(2026, 11, 19, 5, tzinfo=UTC).timestamp()
        self.native.list_recent_comments = lambda repo, *, since, **kw: (list(feed), newest, complete[0])
        self.native.create_pr(1, {"One.lean": "one\n"})
        self.updated[1] = "2026-11-19T04:00:00Z"
        poller.run_once()
        listed = poller.state.get(1)["observed_version"]
        position = poller._comments_since
        feed[:] = [
            (
                1,
                {
                    "id": 50,
                    "body": "no longer a scoreboard",
                    "created_at": "2026-11-19T03:00:00Z",
                    "updated_at": "2026-11-19T04:30:00Z",
                },
            )
        ]
        complete[0] = False
        self.clock.advance(registry.poll_seconds)
        poller.stopping.set()
        summary = poller.run_once()
        poller.stopping.clear()
        self.assertEqual(summary["comment_feed"]["dirtied"], 1)  # an edit, whatever it says now
        self.assertEqual(poller.state.get(1)["observed_version"], listed)  # listed facts kept
        self.assertEqual(poller._comments_since, position)  # unread pages may hold older edits
        complete[0] = True
        self.clock.advance(registry.poll_seconds)
        poller.run_once()
        self.assertEqual(poller._comments_since, newest - 120)

    def test_native_insertion_rejudges_a_review_no_listing_or_feed_reported(self):
        poller, head = self.native_approved()
        original = self.native.admission_policy.evaluate

        def revoked(snapshot):  # the scoreboard was edited to red; nothing else changed
            result, *rest = original(snapshot)
            return ({**result, "merge": False, "review_safe": False}, *rest)

        self.native.admission_policy.evaluate = revoked
        self.controller.tick()
        row = self.native_input()
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "admission-recheck"))
        self.assertFalse(poller.is_current(1, head))
        poller.run_once()
        self.assertEqual(self.native_input()["state"], "cancelled")

    def test_native_insertion_is_held_when_the_merge_base_moved(self):
        poller, head = self.native_approved()
        work = poller.state.get(1)
        decision = loads(work["decision"], {})
        decision.setdefault("evidence", {})["merge_base_sha"] = "e" * 40
        self.controller.store.update("admission_work", {"id": work["id"]}, decision=dumps(decision))
        self.native._merge_base = lambda repo, pull: "f" * 40
        self.controller.tick()
        row = self.native_input()
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "admission-recheck"))
        self.assertFalse(poller.is_current(1, head))

    def test_the_comment_feed_position_survives_a_restart(self):
        registry, poller, recent_calls = self.recent_fixture()
        positions = []

        def feed(repo, *, since, **kw):
            positions.append(since)
            return [], datetime(2026, 11, 19, 5, tzinfo=UTC).timestamp(), True

        self.native.list_recent_comments = feed
        self.native.create_pr(1, {"One.lean": "one\n"})
        self.updated[1] = "2026-11-19T04:00:00Z"
        poller.run_once()
        # The first read starts a recheck interval before the newest listed update.
        listed = datetime(2026, 11, 19, 4, tzinfo=UTC).timestamp()
        self.assertLess(positions[0], listed - self.controller.setting("forgejo_recheck_seconds") + 1)
        restarted = NativePoller(registry, self.controller)
        self.assertEqual(restarted._comments_since, datetime(2026, 11, 19, 5, tzinfo=UTC).timestamp() - 120)

    def test_rechecks_follow_their_own_interval(self):
        registry, poller, recent_calls = self.recent_fixture()
        self.native.list_open_pull_requests = lambda repo, **kw: [
            dataclasses.replace(p, admission={**(p.admission or {}), "recheck_seconds": 900, "updated_at": ""})
            for p in NativeFake.list_open_pull_requests(self.native, repo, **kw)
        ]
        self.native.create_pr(1, {"One.lean": "one\n"})
        poller.run_once()
        self.controller.set_setting("forgejo_recheck_seconds", 3600, "operator")
        self.clock.advance(1800)
        poller.request_discovery()
        poller.stopping.set()
        poller.run_once()
        poller.stopping.clear()
        work = poller.state.get(1)
        self.assertEqual(work["generation"], work["evaluated_generation"])  # 30 min: no recheck yet
        self.clock.advance(1801)
        poller.request_discovery()
        poller.stopping.set()
        poller.run_once()
        poller.stopping.clear()
        self.assertEqual(poller.state.get(1)["recheck"], 1)

    def test_an_incomplete_recent_listing_asks_for_a_full_one(self):
        registry, poller, recent_calls = self.recent_fixture()
        self.native.create_pr(1, {"One.lean": "one\n"})
        self.updated[1] = "2026-11-19T04:00:00Z"
        poller.run_once()
        self.native.list_recent_pull_requests = lambda repo, *, since: ([], 0.0, False)
        self.clock.advance(registry.poll_seconds)
        poller.run_once()
        self.assertEqual(poller.interval(), 0)  # the full listing is due at once

    def test_native_readmit_rereads_admission_and_refuses_another_head_or_a_closed_pr(self):
        registry = self.registry()
        head = self.native.create_pr(1, {"Native.lean": "native\n"})
        poller = NativePoller(registry, self.controller)
        self.controller.admission = __import__(
            "tauceti_queue.mixed_sources", fromlist=["AdmissionInputs"]
        ).AdmissionInputs(None, poller)
        self.assertEqual(poller.run_once()["approved"], 1)
        number = registry.number(1)
        self.assertTrue(poller.is_current(1, head))
        self.assertFalse(self.controller.admission.readmit(number, "f" * 40))
        self.assertTrue(self.controller.admission.readmit(number, head))
        self.assertFalse(poller.is_current(1, head), "re-read before the input can be restored")
        work = poller.state.get(1)
        self.controller.store.update("admission_work", {"id": work["id"]}, disposition="closed")
        self.assertFalse(self.controller.admission.readmit(number, head))

    def test_native_landed_head_is_not_readmitted_and_missing_head_is_retried(self):
        registry = self.registry()
        self.native.create_pr(1, {"Native.lean": "native\n"})
        poller = NativePoller(registry, self.controller)
        with patch.object(self.controller, "_fetch_head", return_value=False):
            self.assertTrue(poller.run_once()["errors"])
        self.assertEqual(self.controller.store.count("inputs"), 0)
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        self.assertEqual(poller.run_once()["approved"], 1)
        row = self.controller.input_for(self.native_id.repository.key, 1)
        self.controller.store.update("inputs", {"id": row["id"]}, state="landed")
        poller.invalidate(1, urgent=True)
        self.assertEqual(poller.run_once()["approved"], 0)
        self.assertEqual(self.controller.store.count("inputs"), 1)

    def test_disabled_native_successor_does_not_stop_an_earlier_github_head(self):
        registry = self.registry()
        registry.native_main = self.native
        self.controller.set_setting("publication_enabled", True, "fixture")
        github = self.github.create_pr(1, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 1, github, "fixture")
        self.controller.tick()
        github_batch = self.controller._head()["id"]
        native = self.native.create_pr(2, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 2, native, "fixture")
        self.controller.tick()
        self.assertTrue(any(self.controller._members(batch["id"]) for batch in self.controller._open_batches()))
        self.controller.mixed = None
        self.controller.source = self.github
        self.native.calls.clear()
        self.github.start_runs()
        self.github.complete_runs()
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": github_batch})["state"], "landed")
        self.assertEqual(self.native.calls, [])
        self.assertTrue(any(row["source_identity"] for row in self.controller.store.rows("members")))

    def test_profiles_load_with_or_without_retired_review_keys(self):
        import json
        import os

        from tauceti_queue.config import ConfigError

        raw = {
            "schema": "tauceti-queue.mixed-sources/v1",
            "github": self.github_id.record(),
            "forgejo": self.native_id.record(),
            "publisher_id": 4,
            "source_token_file": str(self.root / "unused-token"),
        }
        cfg = Config(repository=self.controller.repository, canonical_branch="main", forgejo_mode="observe")
        path = self.root / "profile.json"

        def load(value):
            path.unlink(missing_ok=True)
            path.write_text(json.dumps(value))
            os.chmod(path, 0o600)
            return MixedProfile.load(path, cfg)

        # Production profiles still name the reviewer IDs and authority generation scoreboards no longer need.
        for value in (raw, {**raw, "reviewer_ids": [3], "authority_generation": "1" * 32}):
            profile = load(value)
            self.assertEqual(profile.publisher, 4)
            self.assertEqual(profile.publishers().record(), {"source": self.native_id.record(), "recoveries": [4]})
        for value in ({**raw, "unknown": 1}, {k: v for k, v in raw.items() if k != "publisher_id"}):
            with self.assertRaises(ConfigError):
                load(value)


class MixedExecution(MixedFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from types import SimpleNamespace

        from queue_ci_requests import CONFIG, policy
        from queue_source_ci import VerifiedExecution

        from tauceti_queue.ci_requests import RequestLedger
        from tauceti_queue.fakes import FakeRepo, run_git
        from tauceti_queue.mixed_ci import MixedCandidateCI
        from tauceti_queue.mixed_sources import AdmissionInputs

        self.native_repo = FakeRepo(self.root / "native-git")
        base = self.repo.main_sha()
        run_git(self.native_repo.clone, "fetch", "-q", str(self.repo.origin), "refs/heads/main")
        run_git(self.native_repo.clone, "push", "-q", "--force", "origin", base + ":refs/heads/main")
        self.native = NativeFake(self.native_repo, self.clock, self.native_id.repository.name)
        self.native.repository_identity = self.native_id
        self.native.admission_policy = Decision()

        def native_fetch(repo, specs):
            run_git(self.repo.clone, "fetch", "-q", str(self.native_repo.origin), *specs)
            run_git(self.native_repo.clone, "fetch", "-q", str(self.native_repo.origin), *specs)

        self.native.fetch_refs = native_fetch
        self.registry_value = self.registry()
        self.registry_value.native_main = self.native
        self.registry_value.mirror = SimpleNamespace(
            preflight=lambda *args: None,
            publish=lambda source, tip, approved: self.github.fast_forward(
                self.controller.repository, "main", self.repo.main_sha(), tip
            ),
        )
        configuration = CONFIG.replace(b"https://source.example", b"https://forge.example")
        trusted = dataclasses.replace(
            policy(configuration),
            source=self.native_id,
            credential_origin=self.native_id.repository.forge,
            member_sources=(self.github_id, self.native_id),
        )
        self.ledger = RequestLedger(
            self.controller.store,
            self.repo.clone,
            trusted,
            configuration,
            workflow_revision="b" * 40,
            mixed_sources=True,
        )
        self.execution = VerifiedExecution()
        self.execution.caller_snapshot = ("b" * 40, trusted, configuration)
        self.ci = MixedCandidateCI(self.ledger, self.execution, self.registry_value, self.controller, clock=self.clock)
        self.registry_value.ci = self.controller.mixed_ci = self.ci
        native_policy = NativePoller(self.registry_value, self.controller)
        native_policy.is_current = lambda *args: True
        self.controller.admission = AdmissionInputs(
            SimpleNamespace(
                policy=Decision(),
                is_current=lambda *args: True,
                invalidate=lambda *args, **kw: None,
                withdraw=lambda *args: None,
                accept_manual=lambda *args: None,
                state=SimpleNamespace(summary=lambda: {}),
                last_summary=None,
            ),
            native_policy,
        )
        self.native.retain_candidate = lambda frozen: [
            run_git(self.repo.clone, "push", "-q", str(self.native_repo.origin), sha + ":" + ref)
            for ref, sha in frozen.retained_refs()
        ]
        self.controller.set_setting("publication_enabled", True, "fixture")

    def test_build_uses_enrolled_bootstrap_without_reading_execution_main(self):
        from tauceti_queue.mixed_ci import MixedCandidateCI

        frozen = self.start_mixed()
        policy_file, config_file = self.root / "ci-policy.json", self.root / "ci-config.toml"
        policy_file.write_text(dumps(self.ledger.policy.record(with_config=False)))
        config_file.write_bytes(self.ledger.configuration)
        policy_file.chmod(0o600)
        config_file.chmod(0o600)
        profile = dict(
            policy_file=str(policy_file), configuration_file=str(config_file), credentials_dir=str(self.root)
        )
        self.profile.raw["execution"] = profile
        with patch.object(self.execution, "caller", side_effect=AssertionError("startup execution network read")):
            built = MixedCandidateCI.build(self.registry_value, self.controller, execution=self.execution)
            self.assertEqual(built.ledger.load(frozen.attempt_id), frozen)
            self.execution.add(frozen, classification="passed")
            self.assertEqual(built.observe(frozen.attempt_id).result["classification"], "passed")
        self.assertIsNone(built.ledger.workflow_revision)
        with self.assertRaisesRegex(ValueError, "select a protected"):
            built.ledger.prepare({}, ())
        profile["workflow_revision"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "remove execution.workflow_revision"):
            MixedCandidateCI.build(self.registry_value, self.controller, execution=self.execution)

    def test_mirror_configuration_has_no_private_caller_pin(self):
        from tauceti_queue import mixed_landing

        self.profile.raw["mirror"] = dict(
            forgejo_token_file=str(self.root / "main-token"),
            forgejo_writer_id=6,
            github_credentials_dir=str(self.root / "github-mirror"),
        )
        with (
            patch.object(mixed_landing, "MainWriter", return_value=self.native) as writer,
            patch.object(mixed_landing, "MainMirror", return_value=self.registry_value.mirror) as mirror,
        ):
            mixed_landing.configure(self.registry_value)
        writer.assert_called_once()
        self.assertEqual(mirror.call_args.args[0].execution, self.profile.github)
        self.profile.raw["mirror"]["approved_caller"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "remove mirror.approved_caller"):
            mixed_landing.configure(self.registry_value)

    def start_mixed(self):
        github = self.github.create_pr(17, {"Github.lean": "github\n"})
        native = self.native.create_pr(17, {"Native.lean": "native\n"})
        self.controller.approve(self.controller.repository, 17, github, "fixture")
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        self.controller.tick()
        attempts = self.controller.store.rows("ci_attempts")
        self.assertEqual(len(attempts), 1, self.controller._reasons)
        frozen = self.ledger.load(attempts[0]["id"])
        self.assertEqual(len(frozen.request.accepted_members), 2)
        self.assertEqual(len({member.source for member in frozen.request.accepted_members}), 2)
        self.assertEqual(self.execution.dispatches, [frozen])
        return frozen

    def test_exact_mixed_candidate_lands_and_syncs_both_sources(self):
        frozen = self.start_mixed()
        self.controller.set_setting("forgejo_admission_enabled", False, "operator")
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()  # Landing enqueues publication and follower sync for the next drain.
        self.assertEqual(self.repo.main_sha(), frozen.request.head, self.controller._reasons)
        self.assertEqual(self.native_repo.main_sha(), frozen.request.head)
        self.assertEqual(self.controller.store.rows("batches")[0]["state"], "landed")
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertTrue(any(comment["number"] == 17 for comment in self.github.comments))
        self.assertTrue(any(comment["number"] == 17 for comment in self.native.comments))

    def test_evict_native_returns_members_and_github_only_batch_dispatches_without_forgejo(self):
        frozen = self.start_mixed()
        head = self.controller._head()
        other = self.github.create_pr(18, {"Other.lean": "other\n"})
        self.controller.approve(self.controller.repository, 18, other, "fixture")
        self.controller.tick()
        self.assertEqual(
            self.controller.evict_native(self.controller.repository, "operator")["reason"], "placement-enabled"
        )
        self.controller.set_setting("forgejo_placement_enabled", False, "operator")
        result = self.controller.evict_native(self.controller.repository, "operator", withdraw=[18])
        self.assertTrue(result["evicted"])
        self.assertIn(head["id"], result["batches"])
        self.assertEqual(result["withdrawn"], {18: True})
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "removed")
        native = self.controller.input_for(self.native_id.repository.key, 17)
        self.assertEqual(native["state"], "waiting")
        self.assertEqual(self.controller.input_for(self.controller.repository, 18)["state"], "cancelled")
        dispatches = len(self.execution.dispatches)
        self.native.calls.clear()
        self.controller.tick()
        head = self.controller._head()
        self.assertIsNotNone(head, self.controller._reasons)
        self.assertEqual([m["number"] for m in self.controller._members(head["id"])], [17])
        self.assertFalse(isinstance(self.controller._members(head["id"])[0]["number"], SourceNumber))
        attempt = self.controller._current_attempt(head)
        self.assertIsNotNone(attempt, self.controller._reasons)
        self.assertIsNone(self.controller.store.row("ci_requests", {"attempt_id": attempt["id"]}))
        self.assertEqual(attempt["dispatch_state"], "accepted")
        self.assertEqual(len(self.execution.dispatches), dispatches)
        self.assertFalse(any(name == "update_ref" for name, _ in self.native.calls))
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 17)["state"], "waiting")
        self.assertNotEqual(frozen.attempt_id, attempt["id"])
        self.controller.set_setting("forgejo_placement_enabled", True, "operator")
        self.controller.tick()
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 17)["state"], "inserted")

    def test_placement_pause_during_a_native_probe_keeps_the_input_waiting(self):
        import threading

        from tauceti_queue import chain

        native = self.native.create_pr(17, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        entered, release = threading.Event(), threading.Event()
        build = chain.build
        errors = []

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release missing")
            return build(*args, **kwargs)

        def tick():
            try:
                self.controller.tick()
            except Exception as error:
                errors.append(error)

        with patch.object(chain, "build", side_effect=slow):
            thread = threading.Thread(target=tick)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.controller.input_for(self.native_id.repository.key, 17)["state"], "reserved")
                self.controller.set_setting("forgejo_placement_enabled", False, "operator")
                self.assertTrue(self.controller.evict_native(self.controller.repository, "operator")["evicted"])
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        row = self.controller.input_for(self.native_id.repository.key, 17)
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "placement-paused"))
        self.assertFalse(
            any(isinstance(m["number"], SourceNumber) for m in self.controller.store.rows("members", {"removed": 0}))
        )

    def test_named_withdrawal_is_not_undone_by_insertion_while_runs_are_cancelled(self):
        self.start_mixed()
        self.controller.set_setting("forgejo_placement_enabled", False, "operator")
        cancel = self.controller._cancel_superseded_runs

        def cancel_then_insert(*args, **kwargs):
            # Run cancellation may let the lock go; the loop's insertion can run meanwhile.
            cancel(*args, **kwargs)
            self.controller.tick()

        with patch.object(self.controller, "_cancel_superseded_runs", side_effect=cancel_then_insert):
            result = self.controller.evict_native(self.controller.repository, "operator", withdraw=[17])
        self.assertEqual(result["withdrawn"], {17: True})
        self.assertEqual(self.controller.input_for(self.controller.repository, 17)["state"], "cancelled")
        self.controller.tick()
        self.assertEqual(self.controller.input_for(self.controller.repository, 17)["state"], "cancelled")

    def test_placement_pause_holds_a_native_emergency_request(self):
        self.controller.set_setting("emergency_enabled", True, "operator")
        native = self.native.create_pr(17, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        self.controller.set_setting("forgejo_placement_enabled", False, "operator")
        request = self.controller.request_emergency(
            self.native_id.repository.key, 17, "admin", surface="test", live_head=native
        )
        self.assertTrue(request["accepted"], request)
        self.controller.tick()
        self.assertIsNone(self.controller._emergency_batch())
        self.assertIsNone(self.controller._head())
        row = self.controller.input_for(self.native_id.repository.key, 17)
        self.assertEqual(row["state"], "waiting")
        self.assertTrue(self.controller._emergency_requested(row))

    def test_admission_pause_keeps_accepted_waiting_inputs_and_lands_running_mixed_work(self):
        # Accept an input before pausing, including its pre-batch waiting state.
        native = self.native.create_pr(17, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        self.controller.set_setting("forgejo_admission_enabled", False, "operator")
        github = self.github.create_pr(17, {"Github.lean": "github\n"})
        self.assertTrue(self.controller.approve(self.controller.repository, 17, github, "fixture")["accepted"])
        rejected = self.native.create_pr(18, {"Later.lean": "later\n"})
        self.assertEqual(
            self.controller.approve(self.native_id.repository.key, 18, rejected, "fixture")["reason"],
            "admission-paused",
        )
        self.controller.tick()
        batch = self.controller._head()
        self.assertEqual(len(self.controller._members(batch["id"])), 2)
        frozen = self.ledger.load(self.controller._current_attempt(batch)["id"])
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.native_repo.main_sha(), frozen.request.head)
        self.assertTrue(self.native.comments)

    def test_pause_during_reserved_input_probe_keeps_that_accepted_input(self):
        import threading

        from tauceti_queue import chain

        native = self.native.create_pr(17, {"Native.lean": "native\n"})
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        entered, release = threading.Event(), threading.Event()
        build = chain.build
        errors = []

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release missing")
            return build(*args, **kwargs)

        def tick():
            try:
                self.controller.tick()
            except Exception as error:
                errors.append(error)

        with patch.object(chain, "build", side_effect=slow):
            thread = threading.Thread(target=tick)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.controller.input_for(self.native_id.repository.key, 17)["state"], "reserved")
                self.controller.set_setting("forgejo_admission_enabled", False, "operator")
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 17)["state"], "inserted")

    def test_mirror_uses_current_trusted_github_base_after_reviewed_control_update(self):
        from tauceti_queue.fakes import run_git
        from tauceti_queue.mixed_landing import preflight
        from tauceti_queue.stage1_mirror import MainMirror

        reviewed = self.github.create_pr(90, {".github/workflows/maintenance.yml": "# reviewed update\n"})
        self.github.fast_forward(self.controller.repository, "main", self.repo.main_sha(), reviewed)
        run_git(self.native_repo.origin, "fetch", "-q", str(self.repo.origin), reviewed)
        run_git(self.native_repo.origin, "update-ref", "refs/heads/main", reviewed)
        self.execution.caller_snapshot = (reviewed, self.ledger.policy, self.ledger.configuration)
        checked = []

        def check(source, base, candidate, trusted_base):
            self.assertEqual(trusted_base, self.repo.main_sha())
            MainMirror.check_controls(self.repo.clone, trusted_base, candidate)
            checked.append(("preflight", trusted_base))

        def publish(source, tip, trusted_base):
            self.assertEqual(trusted_base, self.repo.main_sha())
            MainMirror.check_controls(self.repo.clone, trusted_base, tip)
            checked.append(("publish", trusted_base))
            return self.github.fast_forward(self.controller.repository, "main", trusted_base, tip)

        self.registry_value.mirror.preflight = check
        self.registry_value.mirror.publish = publish
        self.controller.change_primary("forgejo", "operator")
        frozen = self.start_mixed()
        self.assertEqual(frozen.request.workflow_revision, reviewed)
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertIn(("publish", reviewed), checked)
        malicious = self.github.create_pr(91, {"scripts/unreviewed.py": "# candidate control change\n"})
        run_git(self.repo.clone, "fetch", "-q", "origin", malicious)
        with self.assertRaisesRegex(ValueError, "execution control files changed"):
            preflight(self.registry_value, self.repo.main_sha(), malicious)

    def test_paused_setting_survives_restart_and_preserves_paid_repair_claim(self):
        from tauceti_queue.controller import Controller

        self.start_mixed()
        batch = self.controller._head()
        task_id = self.controller._create_task(batch, "repair", batch["base"], [], self.clock())
        self.github.admins.add("reviewer")
        task = self.controller.claim_task("paid-worker", "reviewer", mixed_capable=True)
        self.assertEqual(task.task_id, task_id)
        self.controller.set_setting("forgejo_admission_enabled", False, "operator")
        restarted = Controller(
            self.controller.store,
            self.github,
            str(self.repo.clone),
            self.controller.repository,
            revision="restarted",
            canonical_branch="main",
            mixed=self.registry_value,
            clock=self.clock,
        )
        restarted.mixed_ci = self.ci
        restarted.admission = self.controller.admission
        self.registry_value.controller = self.ci.controller = restarted
        new_poller = self.registry_value.poller = NativePoller(self.registry_value, restarted)
        restarted.admission.native = new_poller
        new_head = self.native.create_pr(18, {"Later.lean": "waiting for resume\n"})
        self.assertEqual(new_poller.run_once()["approved"], 0)
        self.assertIsNone(restarted.input_for(self.native_id.repository.key, 18))
        self.assertEqual(
            restarted.approve(self.native_id.repository.key, 18, new_head, "operator")["reason"], "admission-paused"
        )
        self.assertFalse(restarted.setting("forgejo_admission_enabled"))
        self.assertGreater(restarted.renew_task(task_id, task.claim_token), self.clock())
        self.assertEqual(restarted.store.row("claims", {"token": task.claim_token})["released_at"], None)
        from tauceti_queue.fakes import Resolution, ScriptedAgent

        agent = ScriptedAgent(
            "paid-worker",
            "reviewer",
            self.repo,
            self.root / "paid-repair",
            lambda task: {self.controller.repository + "#17": Resolution({"Github.lean": "repaired\n"})},
        )
        tip = agent.produce(task)
        result = restarted.submit_task(task_id, task.claim_token, tip)
        self.assertTrue(result.accepted, result)
        restarted.tick()
        frozen = self.execution.dispatches[-1]
        self.assertEqual(frozen.request.head, tip)
        self.execution.add(frozen, run_id="102", classification="passed")
        self.clock.advance(60)
        restarted.tick()
        restarted.tick()
        self.assertEqual(self.repo.main_sha(), tip)
        self.assertEqual(self.native_repo.main_sha(), tip)
        self.assertFalse(restarted.setting("forgejo_admission_enabled"))

    def test_historical_recovery_does_not_block_new_accepted_heads(self):
        frozen = self.start_mixed()
        self.execution.add(frozen, classification="passed")
        for decision in (self.native.admission_policy, self.controller.admission.github.policy):
            evaluate = decision.evaluate

            def historical(snapshot, evaluate=evaluate):
                result, *rest = evaluate(snapshot)
                return ({**result, "recovery_heads": ["a" * 40]}, *rest)

            decision.evaluate = historical
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head, self.controller._reasons)

    def test_github_only_repair_keeps_legacy_claim_when_native_admission_is_disabled(self):
        head = self.github.create_pr(17, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 17, head, "fixture")
        self.controller.tick()
        batch = self.controller._head()
        task_id = self.controller._create_task(batch, "repair", batch["base"], [], self.clock())
        self.assertEqual(loads(self.controller.store.row("tasks", {"id": task_id})["task_binding"]), {})
        self.github.admins.add("reviewer")
        task = self.controller.claim_task("old-worker", "reviewer")
        self.assertEqual(task.task_id, task_id)
        self.controller.mixed = self.controller.mixed_ci = None
        self.controller.source = self.github
        self.assertGreater(self.controller.renew_task(task_id, task.claim_token), self.clock())
        self.assertEqual(self.controller.store.row("claims", {"token": task.claim_token})["released_at"], None)
        from tauceti_queue.fakes import Resolution, ScriptedAgent

        agent = ScriptedAgent(
            "old-worker",
            "reviewer",
            self.repo,
            self.root / "repair-worker",
            lambda task: {task.members[0].source_pr: Resolution({"Github.lean": "repaired\n"})},
        )
        repaired = agent.produce(task)
        result = self.controller.submit_task(task_id, task.claim_token, repaired)
        self.assertTrue(result.accepted, result)
        self.controller.tick()
        self.github.start_runs()
        self.github.complete_runs()
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(
            self.controller.store.row("batches", {"id": batch["id"]})["state"], "landed", self.controller._reasons
        )

    def test_human_control_changes_split_from_native_batches_before_ci(self):
        self.native.admission_policy.prefix = "Math/"
        self.native.admission_policy.allow_files = ()
        human = self.github.create_pr(17, {"scripts/control.py": "control\n"})
        native = self.native.create_pr(17, {"Math/Native.lean": "native\n"})
        self.controller.approve(self.controller.repository, 17, human, "fixture")
        self.controller.approve(self.native_id.repository.key, 17, native, "fixture")
        self.controller.tick()
        batches = self.controller.store.rows("batches", {"state": ["open", "head"]})
        for batch in batches:
            self.assertEqual(len(self.controller._members(batch["id"])), 1)
        self.assertEqual(len(batches), 2)
        for request in self.execution.dispatches:
            self.assertTrue(self.registry_value.supports_candidate(request.request.base, request.request.head))
            self.assertTrue(all(member.source == self.native_id for member in request.request.accepted_members))
        from tauceti_queue.controller import ControllerError

        with self.assertRaisesRegex(ControllerError, "human control-file"):
            self.controller.change_primary("forgejo", "fixture")

    def test_human_control_changes_wait_under_native_primary_without_dispatch(self):
        self.registry_value.primary = "forgejo"
        self.native.admission_policy.prefix = "Math/"
        self.native.admission_policy.allow_files = ()
        human = self.github.create_pr(17, {"scripts/control.py": "control\n"})
        self.controller.approve(self.controller.repository, 17, human, "fixture")
        self.controller.tick()
        row = self.controller.input_for(self.controller.repository, 17)
        self.assertEqual(row["state"], "waiting")
        self.assertIn("require GitHub primary", row["last_probe"])
        self.assertEqual(self.execution.dispatches, [])
        self.assertEqual(self.controller.store.count("ci_attempts"), 0)
        self.assertTrue(self.controller.change_primary("github", "fixture")["changed"])
        self.controller.tick()
        self.assertEqual(self.controller.input_for(self.controller.repository, 17)["state"], "inserted")

    # --- the control-file lane (`control_lane_enabled`) ---------------------------------------------

    def enable_lane(self):
        self.native.admission_policy.prefix = "Math/"
        self.native.admission_policy.allow_files = ("Shared.lean",)
        self.controller.set_setting("control_lane_enabled", True, "fixture")

    def lane(self):
        return self.controller.store.row("batches", {"lane": "control", "state": ["open", "head"]}, order="id DESC")

    def sources(self, batch):
        return [
            ("forgejo" if isinstance(m["number"], SourceNumber) else "github", int(m["number"]))
            for m in self.controller._members(batch["id"])
        ]

    def pass_native_head(self):
        head = self.controller._head()
        frozen = self.ledger.load(self.controller._current_attempt(head)["id"])
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        return head

    def pass_github_head(self):
        from tauceti_queue.fakes import run_git

        head = self.controller._head()
        # The fixture's Forgejo mirror has only the objects pushed to it: give it the landing tip.
        run_git(self.repo.clone, "push", "-q", str(self.native_repo.origin), f"{head['tip']}:refs/fixture/{head['id']}")
        self.github.start_runs()
        self.github.complete_runs()
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(
            self.controller.store.row("batches", {"id": head["id"]})["state"], "landed", self.controller._reasons
        )
        return head

    def native_head(self, number=17, path="Math/Head.lean"):
        native = self.native.create_pr(number, {path: "head\n"})
        self.controller.approve(self.native_id.repository.key, number, native, "fixture")
        self.controller.tick()
        head = self.controller._head()
        self.assertEqual(self.sources(head), [("forgejo", number)])
        return head

    def approve_github(self, number, files):
        head = self.github.create_pr(number, files)
        self.assertTrue(self.controller.approve(self.controller.repository, number, head, "fixture")["accepted"])
        return head

    def approve_native(self, number, files):
        head = self.native.create_pr(number, files)
        self.assertTrue(self.controller.approve(self.native_id.repository.key, number, head, "fixture")["accepted"])
        return head

    def test_control_lane_takes_the_control_pr_alone_and_native_prs_share_an_ordinary_batch(self):
        self.enable_lane()
        self.approve_github(17, {"scripts/control.py": "control\n"})
        self.approve_native(17, {"Math/Native.lean": "native\n"})
        self.approve_native(18, {"Math/Other.lean": "other\n"})
        self.controller.tick()
        lane = self.lane()
        self.assertEqual((lane["state"], self.sources(lane)), ("head", [("github", 17)]), self.controller._reasons)
        [ordinary] = self.controller.store.rows("batches", {"state": "open"})
        self.assertEqual(ordinary["lane"], "")
        self.assertEqual(sorted(self.sources(ordinary)), [("forgejo", 17), ("forgejo", 18)])
        row = self.controller.input_for(self.controller.repository, 17)
        self.assertEqual((row["no_repair"], loads(row["evidence"])["control_files"]), (1, True))
        self.assertEqual([m["no_repair"] for m in self.controller._members(lane["id"])], [1])
        # The lane head runs GitHub-native CI; nothing went to source-candidate CI.
        self.assertIsNone(self.controller._candidate_coordinator(batch_id=lane["id"]))
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(self.execution.dispatches, [])
        from tauceti_queue.controller import ControllerError

        with self.assertRaisesRegex(ControllerError, "human control-file"):
            self.controller.change_primary("forgejo", "fixture")
        status = self.controller.status()
        self.assertEqual(status["head"]["id"], lane["id"])
        self.assertEqual([b["lane"] for b in status["open_batches"]], [""])
        self.pass_github_head()
        head = self.controller._head()
        self.assertEqual(head["id"], ordinary["id"])
        self.assertTrue(self.execution.dispatches)
        for request in self.execution.dispatches:
            self.assertTrue(self.registry_value.supports_candidate(request.request.base, request.request.head))
            self.assertTrue(all(member.source == self.native_id for member in request.request.accepted_members))

    def test_control_lane_goes_ahead_of_an_older_open_batch_but_never_preempts_the_head(self):
        self.enable_lane()
        first = self.native_head()
        self.approve_native(18, {"Math/Next.lean": "next\n"})
        self.controller.tick()
        [older] = self.controller.store.rows("batches", {"state": "open"})
        self.approve_github(19, {".github/workflows/x.yml": "on: push\n"})
        reasons = self.controller.tick()
        lane = self.lane()
        self.assertGreater(lane["id"], older["id"])
        self.assertEqual((lane["state"], self.sources(lane)), ("open", [("github", 19)]))
        self.assertEqual(self.controller._head()["id"], first["id"], "the lane never evicts the running head")
        self.assertIn(f"control-file lane batch {lane['id']} waits for head {first['id']} to land", reasons)
        status = self.controller.status()
        self.assertEqual(sorted(b["lane"] for b in status["open_batches"]), ["", "control"])
        self.assertEqual(self.controller.status()["waiting"]["control"], 0)
        self.pass_native_head()
        head = self.controller._head()
        self.assertEqual(head["id"], lane["id"], "the lane batch seals ahead of the older open batch")
        self.assertEqual(self.controller.store.row("batches", {"id": older["id"]})["state"], "open")
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(len(self.controller.store.journal_entries("control-lane-head")), 1)

    def test_control_lane_opens_outside_the_open_batch_limit(self):
        self.enable_lane()
        self.native_head()
        self.approve_native(18, {"Math/Same.lean": "eighteen\n"})
        self.approve_native(19, {"Math/Same.lean": "nineteen\n"})
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(len(self.controller._unsealed_batches()), 2)
        self.assertEqual(int(self.controller.setting("open_batch_limit")), 2)
        self.approve_github(20, {"scripts/control.sh": "echo\n"})
        self.controller.tick()
        lane = self.lane()
        self.assertEqual(self.sources(lane), [("github", 20)])
        self.assertEqual(len(self.controller._unsealed_batches()), 2)
        self.assertNotIn(lane["id"], [b["id"] for b in self.controller._staging_batches()])

    def test_a_control_pr_left_waiting_is_never_probed_into_an_ordinary_batch(self):
        self.enable_lane()
        first = self.native_head()
        self.approve_github(18, {"scripts/control.py": "eighteen\n"})
        self.controller.tick()
        # #19 conflicts with #18 in the lane: it waits for the lane's base to move, never for an ordinary batch.
        self.approve_github(19, {"scripts/control.py": "nineteen\n"})
        before = self.controller.store.count("batches")
        for _ in range(3):
            self.clock.advance(60)
            self.controller.tick()
        self.assertEqual(self.controller.store.count("batches"), before)
        row = self.controller.input_for(self.controller.repository, 19)
        self.assertEqual((row["state"], row["probes"]), ("waiting", 1))
        self.assertEqual(self.sources(self.lane()), [("github", 18)])
        self.assertEqual(self.controller.store.journal_entries("probe-source-ci-scope"), [])
        self.assertEqual(self.controller._head()["id"], first["id"])
        self.assertEqual(self.controller.status()["waiting"]["control"], 1)

    def test_an_unclassified_github_pr_waits_out_of_ordinary_batches(self):
        self.enable_lane()
        self.native_head()
        head = self.approve_github(18, {"Math/Github.lean": "github\n"})
        fetch = self.controller._fetch_head
        with patch.object(self.controller, "_fetch_head", side_effect=lambda n, sha: sha != head and fetch(n, sha)):
            self.controller.tick()
        row = self.controller.input_for(self.controller.repository, 18)
        self.assertEqual((row["state"], row["probes"]), ("waiting", 0))
        self.assertEqual(row["last_probe"], "control-lane: waits for its head to be classified")
        self.controller.tick()
        self.assertEqual(self.controller.input_for(self.controller.repository, 18)["state"], "inserted")
        self.assertIsNone(self.lane())

    def test_a_pr_with_native_and_control_changes_goes_to_the_lane_whole(self):
        self.enable_lane()
        self.native_head()
        self.approve_github(18, {"Math/Both.lean": "both\n", "scripts/both.py": "both\n"})
        self.approve_native(19, {"Math/Native.lean": "native\n"})
        self.controller.tick()
        self.assertEqual(self.sources(self.lane()), [("github", 18)])
        ordinary = [b for b in self.controller.store.rows("batches", {"state": "open"}) if not b["lane"]]
        self.assertEqual([self.sources(b) for b in ordinary], [[("forgejo", 19)]])

    def test_an_ordinary_github_pr_and_allow_listed_files_stay_out_of_the_lane(self):
        self.enable_lane()
        self.native_head()
        self.approve_github(18, {"Math/Github.lean": "github\n", "Shared.lean": "allowed\n"})
        self.controller.tick()
        self.assertIsNone(self.lane())
        row = self.controller.input_for(self.controller.repository, 18)
        self.assertEqual(
            (row["state"], row["no_repair"], loads(row["evidence"])["control_files"]), ("inserted", 0, False)
        )

    def test_native_control_changes_are_refused_and_never_enter_the_lane(self):
        self.enable_lane()
        self.native_head()
        self.approve_native(18, {"scripts/native.py": "native\n"})
        self.controller.tick()
        row = self.controller.input_for(self.native_id.repository.key, 18)
        self.assertEqual(row["state"], "waiting")
        self.assertIn("native candidate needs a compatible batch", row["last_probe"])
        self.assertNotIn("control_files", loads(row["evidence"]))
        self.assertIsNone(self.lane())
        self.assertEqual(self.controller.store.count("batches", {"lane": "control"}), 0)

    def test_control_lane_is_off_under_native_primary(self):
        self.enable_lane()
        self.registry_value.primary = "forgejo"
        self.approve_github(17, {"scripts/control.py": "control\n"})
        self.controller.tick()
        row = self.controller.input_for(self.controller.repository, 17)
        self.assertEqual(row["state"], "waiting")
        self.assertIn("require GitHub primary", row["last_probe"])
        self.assertNotIn("control_files", loads(row["evidence"]))
        self.assertEqual(self.controller.store.count("batches", {"lane": "control"}), 0)
        self.assertEqual(self.execution.dispatches, [])
        self.assertTrue(self.controller.change_primary("github", "fixture")["changed"])
        self.controller.tick()
        self.assertEqual(self.sources(self.lane()), [("github", 17)])

    def test_a_control_pr_refused_on_the_canonical_tip_is_still_probed_into_a_lane_on_that_tip(self):
        self.native.admission_policy.prefix = "Math/"
        self.native.admission_policy.allow_files = ()
        self.approve_github(17, {"scripts/control.py": "control\n"})
        canonical = self.repo.main_sha()
        # A source-candidate scope refusal recorded against an empty batch on the canonical commit.
        row = self.controller.input_for(self.controller.repository, 17)
        self.controller.store.update(
            "inputs",
            {"id": row["id"]},
            last_probe="source-ci-scope: human control-file changes require GitHub primary and a GitHub-only batch",
            last_probe_tip=canonical,
            last_probe_base=canonical,
        )
        self.controller.set_setting("control_lane_enabled", True, "fixture")
        self.controller.tick()
        lane = self.lane()
        self.assertEqual((lane["base"], self.sources(lane)), (canonical, [("github", 17)]), self.controller._reasons)

    def test_an_emergency_request_for_a_control_pr_takes_it_out_of_the_lane(self):
        self.enable_lane()
        self.controller.set_setting("emergency_enabled", True, "fixture")
        first = self.native_head()
        self.approve_github(18, {"scripts/control.py": "control\n"})
        self.controller.tick()
        lane = self.lane()
        self.assertEqual(self.sources(lane), [("github", 18)])
        request = self.controller.request_emergency(self.controller.repository, 18, "admin", surface="test")
        self.assertTrue(request["accepted"], request)
        self.controller.tick()
        emergency = self.controller._emergency_batch()
        self.assertEqual(self.sources(emergency), [("github", 18)])
        self.assertEqual([m["no_repair"] for m in self.controller._members(emergency["id"])], [1])
        self.assertEqual(self.controller.store.journal_entries("emergency-refused"), [])
        self.assertEqual(self.controller._head()["id"], first["id"])
        self.pass_native_head()
        self.assertEqual(self.controller._head()["id"], emergency["id"], "the emergency batch goes before the lane")
        self.pass_github_head()
        self.controller.tick()
        # The lane batch keeps only the displaced slot, which never makes it a head of its own.
        self.assertIsNone(self.controller._head())
        self.assertIsNone(self.controller._lane_ready())

    def test_a_label_added_and_removed_never_clears_the_control_opt_out(self):
        self.enable_lane()
        self.native_head()
        self.approve_github(18, {"scripts/control.py": "control\n"})
        self.controller.tick()
        for action, labels in (("labeled", ["dont-repair-in-queue"]), ("unlabeled", [])):
            self.controller.handle_event(
                "pull_request",
                {
                    "action": action,
                    "number": 18,
                    "label": {"name": "dont-repair-in-queue"},
                    "pull_request": {"number": 18, "labels": [{"name": label} for label in labels]},
                },
            )
        row = self.controller.input_for(self.controller.repository, 18)
        self.assertEqual((row["state"], row["no_repair"]), ("inserted", 1))
        self.assertEqual([m["no_repair"] for m in self.controller._members(self.lane()["id"])], [1])

    def test_a_failing_lane_head_withdraws_the_culprit_without_editing_it(self):
        from tauceti_queue.fakes import Resolution, ScriptedAgent

        self.enable_lane()
        self.github.ci_policy = lambda head, base, members: (
            ("fail", [{"kind": "compiler", "module": "scripts/bad.py", "message": "boom"}])
            if self.repo.file_at(head, "scripts/bad.py") is not None
            else ("pass", [])
        )
        self.approve_github(17, {"scripts/good.py": "good\n"})
        self.approve_github(18, {"scripts/bad.py": "bad\n"})
        self.controller.tick()
        lane = self.lane()
        self.assertEqual((lane["state"], sorted(self.sources(lane))), ("head", [("github", 17), ("github", 18)]))
        self.github.start_runs()
        self.github.complete_runs()
        self.clock.advance(60)
        self.controller.tick()
        self.github.admins.add("reviewer")
        [bad] = [m for m in self.controller._members(lane["id"]) if int(m["number"]) == 18]
        edit = ScriptedAgent(
            "editor",
            "reviewer",
            self.repo,
            self.root / "editor",
            lambda task: {bad["source_pr"]: Resolution({"scripts/bad.py": "edited\n"}, explanation="Edited.")},
        )
        result = edit.run(self.controller)
        self.assertIsNotNone(result, self.controller._reasons)
        self.assertFalse(result.accepted, "a control member is never edited")
        self.assertEqual(result.reason, "no-repair-edited")
        self.assertTrue(all(m.no_repair for m in edit.tasks[0].members))
        # The same worker empties the member instead, which is all a repair may do to it.
        edit.script = lambda task: {bad["source_pr"]: Resolution(empty=True, explanation="It does not build.")}
        task = edit.tasks[0]
        result = self.controller.submit_task(task.task_id, task.claim_token, edit.produce(task))
        self.assertTrue(result.accepted, result)
        row = self.controller.input_for(self.controller.repository, 18)
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "problem-anticipated"))
        self.controller.tick()
        body = self.github.comments_for(18)[0]["body"]
        self.assertIn("changes control files outside the native path policy", body)
        self.assertNotIn("dont-repair-in-queue", body)
        self.pass_github_head()
        self.assertEqual(self.repo.file_at("main", "scripts/good.py"), "good\n")
        self.assertIsNone(self.repo.file_at("main", "scripts/bad.py"))
        self.assertEqual(
            {m["number"]: m["provenance"] for m in self.controller._members(lane["id"])}, {17: "clean", 18: "edited"}
        )

    def test_turning_the_lane_off_stages_a_live_lane_batch_as_an_ordinary_one(self):
        self.enable_lane()
        self.native_head()
        self.approve_native(18, {"Math/Next.lean": "next\n"})
        self.controller.tick()
        [older] = self.controller.store.rows("batches", {"state": "open"})
        self.approve_github(19, {"scripts/control.py": "control\n"})
        self.controller.tick()
        lane = self.lane()
        self.controller.set_setting("control_lane_enabled", False, "operator")
        reasons = self.controller.tick()
        self.assertFalse(any("control-file lane" in reason for reason in reasons), reasons)
        self.assertEqual([b["id"] for b in self.controller._staging_batches()], [older["id"], lane["id"]])
        self.pass_native_head()
        self.assertEqual(self.controller._head()["id"], older["id"], "with the lane off, oldest first")
        self.assertEqual(self.controller.store.journal_entries("control-lane-head"), [])
        self.assertEqual(self.controller.store.row("batches", {"id": lane["id"]})["state"], "open")

    def lane_with_claimed_successor_pre_rebase(self):
        """Head H (#101) runs, ordinary open batch B (#102) conflicts with it on `Shared.lean` and a
        worker holds B's `pre_rebase` task onto H's tip; then a control PR (#103) opens the lane."""
        from tauceti_queue.fakes import Resolution, ScriptedAgent

        self.enable_lane()
        self.controller.set_setting("repair_concurrency", 2, "fixture")
        self.github.admins.add("reviewer")
        self.approve_github(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.controller.tick()
        head = self.controller._head()
        self.approve_github(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.controller.tick()
        successor = next(b for b in self.controller.store.rows("batches", {"state": "open"}) if not b["lane"])
        source_pr = self.controller._members(successor["id"])[0]["source_pr"]
        agent = ScriptedAgent(
            "agent",
            "reviewer",
            self.repo,
            self.root / "agent",
            lambda task: {
                source_pr: Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"}, explanation="Merged."
                )
            },
        )
        task = agent.claim(self.controller)
        self.assertIsNotNone(task, self.controller._reasons)
        self.assertEqual((task.kind, task.onto), ("pre_rebase", head["tip"]))
        self.approve_github(103, {"scripts/control.py": "control\n"})
        self.controller.tick()
        self.assertEqual(self.lane()["state"], "open")
        self.pass_github_head()
        lane = self.controller._head()
        self.assertEqual(lane["lane"], "control")
        self.assertEqual(
            [e["detail"] for e in self.controller.store.journal_entries("emergency-preserved")],
            [{"tasks": [task.task_id]}],
        )
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "open")
        return successor, agent, task, source_pr

    def test_claimed_successor_pre_rebase_survives_the_lane_head_and_is_carried_onto_it(self):
        successor, agent, task, source_pr = self.lane_with_claimed_successor_pre_rebase()
        adoption = self.controller.submit_task(task.task_id, task.claim_token, agent.produce(task))
        self.assertTrue(adoption.accepted, adoption)
        self.controller.tick()
        carried = self.controller.store.row("batches", {"id": successor["id"]})
        self.assertEqual(
            (carried["pre_rebase_state"], carried["pre_rebase_onto"]), ("adopted", self.controller._head()["tip"])
        )
        self.assertIn(source_pr, loads(carried["pre_rebase_edits"]))
        self.pass_github_head()
        sealed = self.controller._head()
        self.assertEqual(sealed["id"], successor["id"])
        self.assertTrue(self.controller.store.journal_entries("rebased")[-1]["detail"]["pre_rebased"])
        self.assertEqual(self.controller.store.count("tasks", {"kind": "rebase_conflict"}), 0)
        self.assertEqual([m["provenance"] for m in self.controller._members(sealed["id"])], ["edited"])

    def test_a_preserved_pre_rebase_submitted_after_the_lane_lands_is_carried_onto_main(self):
        successor, agent, task, source_pr = self.lane_with_claimed_successor_pre_rebase()
        self.pass_github_head()  # the lane lands while the worker still holds the task
        self.assertIsNone(self.controller._head(), "the claimed successor waits for its worker")
        adoption = self.controller.submit_task(task.task_id, task.claim_token, agent.produce(task))
        self.assertTrue(adoption.accepted, adoption)
        self.controller.tick()
        sealed = self.controller._head()
        self.assertEqual(sealed["id"], successor["id"])
        self.assertEqual(sealed["base"], self.repo.main_sha())
        self.assertTrue(self.controller.store.journal_entries("rebased")[-1]["detail"]["pre_rebased"])
        self.assertEqual(self.controller.store.count("tasks", {"kind": "rebase_conflict"}), 0)
        self.assertEqual([m["provenance"] for m in self.controller._members(sealed["id"])], ["edited"])
        self.pass_github_head()
        self.assertEqual(
            self.repo.file_at("main", "Shared.lean"), "line one\nline two changed by 101 and 102\nline three\n"
        )
        self.assertEqual(self.repo.file_at("main", "scripts/control.py"), "control\n")

    def test_old_uncertain_lease_is_settled_when_main_has_moved_off_its_expected_parent(self):
        from tauceti_queue.controller import ControllerError
        from tauceti_queue.fakes import run_git

        frozen = self.start_mixed()
        # A candidate ancestor still permits a legacy API fast-forward, but an
        # atomic expected-old Git lease can no longer apply.
        moved = run_git(self.repo.clone, "rev-parse", frozen.request.head + "^")
        run_git(self.repo.origin, "update-ref", "refs/heads/main", moved)
        run_git(self.native_repo.origin, "update-ref", "refs/heads/main", moved)
        self.controller._enqueue(
            "promote",
            "obsolete-sent-lease",
            {
                "batch_id": 1,
                "version": -1,
                "tip": frozen.request.head,
                "expected_parent": frozen.request.base,
                "primary": "github",
                "promotion_send": {"state": "sending"},
            },
        )
        entry = self.controller.store.row("outbox", {"key": "obsolete-sent-lease"})
        self.controller.store.update("outbox", {"id": entry["id"]}, state="done", attempts=1)
        with self.assertRaisesRegex(ControllerError, "reconcile a sent landing"):
            self.controller.change_primary("forgejo", "fixture")
        intent = loads(entry["payload"])
        intent["promotion_send"]["lease"] = True
        self.controller.store.update("outbox", {"id": entry["id"]}, payload=dumps(intent))
        self.assertTrue(self.controller.change_primary("forgejo", "fixture")["changed"])
        settled = loads(self.controller.store.row("outbox", {"id": entry["id"]})["payload"])
        self.assertEqual(settled["promotion_send"]["state"], "settled")

    def test_github_prepare_before_ci_survives_primary_reversal_and_disablement(self):
        self.registry_value.primary = "forgejo"
        self.controller.set_setting("repair_concurrency", 2, "fixture")
        head = self.github.create_pr(17, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 17, head, "fixture")
        base = self.repo.main_sha()
        with self.controller._lock:
            self.controller._canonical = base
            batch = self.controller._create_open_batch(base, self.clock())
            row = self.controller.input_for(self.controller.repository, 17)
            self.assertTrue(self.controller._probe(row, batch, self.clock()))
            batch = self.controller.store.row("batches", {"id": batch["id"]})
            task_id = self.controller._create_task(batch, "prepare", base, [], self.clock())
        self.assertEqual(self.controller.store.count("ci_requests"), 0)
        self.assertEqual(loads(self.controller.store.row("tasks", {"id": task_id})["task_binding"]), {})
        self.github.admins.add("reviewer")
        task = self.controller.claim_task("old-worker", "reviewer")
        self.assertEqual(task.task_id, task_id)
        self.assertTrue(self.controller.change_primary("github", "fixture")["changed"])
        self.controller.mixed = self.controller.mixed_ci = None
        self.controller.source = self.github
        self.assertGreater(self.controller.renew_task(task_id, task.claim_token), self.clock())

    def test_unsupported_pr_waits_without_new_empty_batch_probes_each_tick(self):
        self.registry_value.primary = "forgejo"
        self.native.admission_policy.prefix = "Math/"
        self.native.admission_policy.allow_files = ()
        head = self.native.create_pr(17, {"Math/Head.lean": "head\n"})
        self.controller.approve(self.native_id.repository.key, 17, head, "fixture")
        self.controller.tick()
        later = self.native.create_pr(18, {"Math/Later.lean": "later\n"})
        self.controller.approve(self.native_id.repository.key, 18, later, "fixture")
        self.controller.tick()
        human = self.github.create_pr(19, {"scripts/control.py": "control\n"})
        self.controller.approve(self.controller.repository, 19, human, "fixture")
        self.controller.tick()
        batches = self.controller.store.count("batches")
        probes = self.controller.input_for(self.controller.repository, 19)["probes"]
        for _ in range(3):
            self.clock.advance(60)
            self.controller.tick()
        self.assertEqual(self.controller.store.count("batches"), batches)
        self.assertEqual(self.controller.input_for(self.controller.repository, 19)["probes"], probes)

    def test_definite_landing_rejection_is_settled_and_does_not_block_primary_change(self):
        from tauceti_queue.ports import FastForwardResult

        frozen = self.start_mixed()
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        with patch.object(
            self.github, "fast_forward_leased", return_value=FastForwardResult(False, frozen.request.base, "protected")
        ):
            self.controller.tick()
        entry = self.controller.store.rows("outbox", {"kind": "promote"})[0]
        self.assertEqual(loads(entry["payload"])["promotion_send"]["state"], "settled")
        self.assertTrue(self.controller.change_primary("forgejo", "fixture")["changed"])

    def test_unknown_unleased_send_is_not_reinterpreted_as_leased_on_retry(self):
        self.controller._enqueue("promote", "old-unleased", {"promotion_send": {"state": "sending"}})
        entry = self.controller.store.row("outbox", {"key": "old-unleased"})
        self.controller._promotion_send_state({"_outbox_id": entry["id"]}, "sending", lease=True)
        self.assertNotIn(
            "lease", loads(self.controller.store.row("outbox", {"id": entry["id"]})["payload"])["promotion_send"]
        )
        self.controller._promotion_send_state({"_outbox_id": entry["id"]}, "settled", refused=True)
        intent = loads(self.controller.store.row("outbox", {"id": entry["id"]})["payload"])
        self.assertEqual(intent["promotion_send"]["state"], "sending")
        self.controller._promotion_send_state({"_outbox_id": entry["id"]}, "settled")
        intent = loads(self.controller.store.row("outbox", {"id": entry["id"]})["payload"])
        self.assertEqual(intent["promotion_send"]["state"], "settled")

    def test_mutable_original_prs_do_not_withdraw_frozen_staged_contributions(self):
        frozen = self.start_mixed()
        for port, filename in ((self.native, "Native.lean"), (self.github, "Github.lean")):
            port.push_pr(17, {filename: "changed after staging\n"})
            port.prs[17].update(state="closed", draft=True, base_ref="other")
            port.labels[17] = {"awaiting-author", "hold"}
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head, self.controller._reasons)
        self.assertEqual(len(self.execution.dispatches), 1)

    def test_changed_frozen_membership_still_refuses_promotion(self):
        from tauceti_queue.ports import PromotionRefused

        frozen = self.start_mixed()
        self.execution.add(frozen, classification="passed")
        members = self.controller._members(1)
        members[0] = {**members[0], "accepted_head": "a" * 40}
        with self.assertRaisesRegex(PromotionRefused, "membership changed"):
            self.ci.verify_promotion(frozen.attempt_id, members, self.controller.source)

    def test_primary_changes_reuse_running_exact_ci_and_keep_source_attribution(self):
        frozen = self.start_mixed()
        self.assertTrue(self.controller.change_primary("forgejo", "fixture")["changed"])
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.native_repo.main_sha(), frozen.request.head, self.controller._reasons)
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertTrue(self.controller.change_primary("github", "fixture")["changed"])
        self.assertEqual(self.registry_value.generation, 2)

    def test_disabled_restart_keeps_mixed_work_without_source_reads(self):
        self.start_mixed()
        self.controller.mixed = self.controller.mixed_ci = None
        self.controller.source = self.github
        self.native.calls.clear()
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(self.controller.store.rows("batches")[0]["state"], "head")

    def test_primary_change_refuses_lag_and_fences_old_pending_promotion(self):
        from tauceti_queue.controller import ControllerError

        frozen = self.start_mixed()
        moved = self.github.create_pr(18, {"Moved.lean": "moved\n"})
        self.github.fast_forward(self.controller.repository, "main", self.repo.main_sha(), moved)
        with self.assertRaises(ControllerError):
            self.controller.change_primary("forgejo", "fixture")
        self.assertEqual(self.registry_value.primary, "github")
        self.controller.store.set_setting("mixed:primary_generation", 1)
        payload = {"batch_id": 1, "version": 2, "tip": frozen.request.head, "attempt_id": frozen.attempt_id}
        self.assertEqual(self.controller._perform_promotion(payload, self.clock()), "stale-primary-generation")

    def test_mixed_repair_task_round_trip_and_policy_fence(self):
        import json

        from tauceti_queue.controller import TaskError
        from tauceti_queue.repair_task import Task as WorkerTask

        self.start_mixed()
        batch = self.controller._head()
        task_id = self.controller._create_task(batch, "repair", batch["base"], [], self.clock())
        self.github.admins.add("reviewer")
        self.assertIsNone(self.controller.claim_task("old-worker", "reviewer"))
        task = self.controller.claim_task("repair-worker", "reviewer", mixed_capable=True)
        self.assertEqual(task.task_id, task_id)
        row = task.to_dict()
        restored = WorkerTask.from_json(json.loads(json.dumps(row)))
        self.assertEqual(len({member.number for member in restored.members}), 2)
        import subprocess

        from tauceti_queue import repair, repair_task
        from tauceti_queue.fakes import run_git

        linear = repair.read_chain(self.repo.clone, restored.base, restored.tip)
        self.assertEqual(len({commit.pr for commit in linear}), 2)
        context = repair_task.prompt_context(restored, None)
        for member in context.members:
            member["no_repair"] = True
        guarded = "\n".join(repair._no_repair_section(context, linear))
        self.assertIn("other member's exact Source-PR", guarded)
        self.assertIn(self.registry_value.number(17).source_pr, guarded)
        self.assertNotIn("#forgejo:", guarded)
        run_git(self.repo.clone, "checkout", "-q", "--detach", restored.tip)
        listings = repair.listings_path(self.repo.clone)
        repair.compose_prompt(
            repair_task.prompt_context(restored, None, listings=listings),
            repair_task.task_findings(restored),
            linear,
            self.repo.clone,
        )
        checked = subprocess.run([sys.executable, str(listings.parent / "check")], capture_output=True, text=True)
        self.assertEqual((checked.returncode, checked.stdout.strip()), (0, "ok"))
        self.assertEqual(restored.to_json()["mixed_binding"], row["mixed_binding"])
        self.assertEqual(WorkerTask.from_json(restored.to_json()).members, restored.members)
        for key in ("schema", "mixed_binding"):
            damaged = {k: v for k, v in row.items() if k != key}
            with self.assertRaises(ValueError):
                WorkerTask.from_json(damaged)
        original_binding = loads(self.controller.store.row("tasks", {"id": task_id})["task_binding"])
        self.ledger.policy = dataclasses.replace(self.ledger.policy, require_publication=False)
        self.assertGreater(self.controller.renew_task(task_id, task.claim_token), self.clock())
        self.profile.raw["authority_generation"] = "2" * 32
        self.assertGreater(self.controller.renew_task(task_id, task.claim_token), self.clock())
        self.ledger.policy = dataclasses.replace(self.ledger.policy, base_branch="other")
        with self.assertRaises(TaskError):
            self.controller.renew_task(task_id, task.claim_token)
        self.assertEqual(loads(self.controller.store.row("tasks", {"id": task_id})["task_binding"]), original_binding)

    def test_legacy_feed_never_assigns_native_members_to_github_numbers(self):
        from tauceti_queue.feeds import ControllerFeedSource

        self.start_mixed()
        source = ControllerFeedSource(self.controller)
        batches = source.active_batches(self.controller.repository, "main")
        members = [member for batch in batches for member in batch.members]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].pr, 17)
        status = self.controller.status()
        identities = [member["source_identity"] for member in status["head"]["members"]]
        self.assertCountEqual(identities, [self.github_id.record(), self.native_id.record()])

    def test_new_caller_freezes_updated_policy_without_changing_existing_requests(self):
        from tauceti_queue.ci_requests import RequestLedger

        frozen = self.start_mixed()
        configuration = self.ledger.configuration + b"\n# reviewed control update\n"
        policy = dataclasses.replace(
            self.ledger.policy, require_publication=False, config_digest=hashlib.sha256(configuration).hexdigest()
        )
        self.execution.caller_snapshot = ("f" * 40, policy, configuration)
        head = self.controller._head()
        prepared = self.ci.prepare(head, self.controller._members(head["id"]), "3" * 32)
        self.assertEqual(prepared.request.workflow_revision, "f" * 40)
        self.assertEqual(prepared.policy, policy)
        self.assertEqual(prepared.configuration, configuration)
        self.assertEqual(self.ledger.load(frozen.attempt_id), frozen)
        recovered = RequestLedger(
            self.controller.store,
            self.repo.clone,
            policy,
            configuration,
            workflow_revision="f" * 40,
            mixed_sources=True,
        )
        self.assertEqual(recovered.load(frozen.attempt_id), frozen)
        self.execution.add(frozen, classification="passed")
        self.assertEqual(self.ci.observe(frozen.attempt_id).result["classification"], "passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(len(self.execution.dispatches), 1)

    def test_publication_disable_preserves_pending_landing_and_mirror_intents(self):
        self.start_mixed()
        self.controller._enqueue("mixed-sync", "paused-sync", {})
        self.controller.set_setting("publication_enabled", False, "fixture")
        self.native.calls.clear()
        self.controller._drain_outbox(self.clock())
        entry = self.controller.store.row("outbox", {"key": "paused-sync"})
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(self.native.calls, [])

    def test_ambiguous_landing_blocks_primary_change_until_readback_reconciles(self):
        from tauceti_queue.controller import ControllerError

        frozen = self.start_mixed()
        self.execution.add(frozen, classification="passed")
        self.github.lose_fast_forward_response = True
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        # The follower is still behind and a pending uncertain promotion is retained.
        with self.assertRaises(ControllerError):
            self.controller.change_primary("forgejo", "fixture")
        self.assertEqual(self.registry_value.primary, "github")
        self.clock.advance(120)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.native_repo.main_sha(), frozen.request.head)
        self.assertTrue(self.controller.change_primary("forgejo", "fixture")["changed"])
        self.assertEqual(len(self.execution.dispatches), 1)

    def test_disabled_mixed_ci_ownership_is_held_even_for_github_only_members(self):
        self.registry_value.primary = "forgejo"
        head = self.github.create_pr(17, {"Github.lean": "github\n"})
        self.controller.approve(self.controller.repository, 17, head, "fixture")
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 1)
        self.controller.mixed = self.controller.mixed_ci = None
        self.controller.source = self.github
        self.github.calls.clear()
        self.controller.tick()
        self.assertTrue(self.controller.status()["head"]["source_disabled"])
        self.assertFalse(
            any(
                name in {"find_candidate_run", "read_run", "dispatch_candidate", "fast_forward"}
                for name, _ in self.github.calls
            )
        )

    def test_failed_preflight_without_a_sent_write_does_not_block_primary_change(self):
        frozen = self.start_mixed()
        self.controller._enqueue(
            "promote",
            "failed-before-send",
            {
                "batch_id": 1,
                "version": 2,
                "tip": frozen.request.head,
                "attempt_id": frozen.attempt_id,
                "primary": "github",
                "primary_generation": 0,
                "promotion_send": {"state": "not-sent"},
            },
        )
        entry = self.controller.store.row("outbox", {"key": "failed-before-send"})
        self.controller.store.update("outbox", {"id": entry["id"]}, attempts=1, next_attempt_at=self.clock() + 3600)
        self.assertTrue(self.controller.change_primary("forgejo", "fixture")["changed"])

    def test_mixed_scope_rechecks_the_full_repaired_candidate(self):
        from tauceti_queue.ports import PromotionRefused

        frozen = self.start_mixed()
        self.execution.add(frozen, classification="passed")
        self.native.admission_policy.prefix = "OnlyAllowed/"
        self.native.admission_policy.allow_files = ()
        with self.assertRaisesRegex(PromotionRefused, "native path policy"):
            self.ci.verify_promotion(frozen.attempt_id, self.controller._members(1), self.controller.source)

    def test_sync_network_wait_does_not_block_paid_claim_renewal(self):
        import threading

        self.start_mixed()
        batch = self.controller._head()
        self.controller._create_task(batch, "repair", batch["base"], [], self.clock())
        self.github.admins.add("reviewer")
        task = self.controller.claim_task("repair-worker", "reviewer", mixed_capable=True)
        completed = threading.Event()

        def remote_wait(registry):
            worker = threading.Thread(
                target=lambda: (self.controller.renew_task(task.task_id, task.claim_token), completed.set())
            )
            worker.start()
            self.assertTrue(completed.wait(2), "mirror I/O held the worker request lock")
            worker.join()
            return {}

        self.controller._enqueue("mixed-sync", "slow-sync", {})
        entry = self.controller.store.row("outbox", {"key": "slow-sync"})
        with patch("tauceti_queue.mixed_landing.synchronize", side_effect=remote_wait):
            self.controller._drain_entry(entry, self.clock())
        self.assertEqual(self.controller.store.row("outbox", {"id": entry["id"]})["state"], "done")


if __name__ == "__main__":
    unittest.main()
