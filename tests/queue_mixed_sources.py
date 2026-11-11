#!/usr/bin/env python3
"""Default inertness and qualified mixed admission over real local Git history."""

import copy
import dataclasses
import sys
import tempfile
import unittest
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
        return super().list_open_pull_requests(repo, review_app_id=review_app_id, **kwargs)

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
        self.profile = MixedProfile(
            self.github_id,
            self.native_id,
            frozenset({3}),
            4,
            self.root / "unused-token",
            {"authority_generation": "1" * 32},
        )
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
        self.controller.source = registry.port
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

    def test_native_discovery_skips_unchanged_snapshots_and_withdraws_recovery_holds(self):
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
            return ({**result, "merge": False, "recovery_authority_hold": True}, *rest)

        self.native.admission_policy.evaluate = revoked
        poller.invalidate(1, head=head, urgent=True)
        poller.run_once()
        self.assertEqual(self.controller.input_for(self.native_id.repository.key, 1)["state"], "cancelled")
        self.assertFalse(poller.is_current(1, head))
        poller.withdraw(99, None)
        self.assertEqual(poller.state.get(99)["cancelled_head_sha"], "*")

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

    def test_operational_profile_revert_does_not_reuse_retired_review_authority(self):
        from types import SimpleNamespace

        from tauceti_queue.mixed_native import MixedNativeSource

        cfg = Config(repository=self.controller.repository, canonical_branch="main", forgejo_mode="observe")

        def configured(profile):
            source = MixedNativeSource(
                profile,
                cfg,
                self.repo.clone,
                client=SimpleNamespace(forge=self.native_id.repository.forge),
                token="fixture",
                clock=self.clock,
            )
            source.configure_publisher_policy(self.controller.store)

        configured(self.profile)
        old = dict(self.profile.raw)
        self.profile.raw["execution"] = {"workflow_revision": "a" * 40}
        configured(self.profile)
        self.profile.raw.clear()
        self.profile.raw.update(old)
        configured(self.profile)
        self.profile.raw["authority_generation"] = "2" * 32
        configured(self.profile)
        self.profile.raw.clear()
        self.profile.raw.update(old)
        with self.assertRaisesRegex(ValueError, "generation"):
            configured(self.profile)

    def test_operational_profile_edits_preserve_review_generation(self):
        original = self.profile.publishers().generation
        self.profile.raw.update(
            execution={"workflow_revision": "a" * 40},
            mirror={"approved_caller": "b" * 40},
            source_token_file="/another/credential",
        )
        self.assertEqual(self.profile.publishers().generation, original)
        self.profile.raw.pop("execution")
        self.profile.raw.pop("mirror")
        self.profile.raw.pop("source_token_file")
        self.assertEqual(self.profile.publishers().generation, original)


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
        self.registry_value.approved_caller = base
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
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.controller.tick()  # Landing enqueues publication and follower sync for the next drain.
        self.assertEqual(self.repo.main_sha(), frozen.request.head, self.controller._reasons)
        self.assertEqual(self.native_repo.main_sha(), frozen.request.head)
        self.assertEqual(self.controller.store.rows("batches")[0]["state"], "landed")
        self.assertTrue(any(comment["number"] == 17 for comment in self.github.comments))
        self.assertTrue(any(comment["number"] == 17 for comment in self.native.comments))

    def test_changed_original_native_head_cannot_land_cached_pass(self):
        original = self.repo.main_sha()
        frozen = self.start_mixed()
        self.native.create_pr(17, {"Native.lean": "changed\n"})
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), original)
        attempt = self.controller.store.row("ci_attempts", {"id": frozen.attempt_id})
        self.assertEqual(attempt["outcome"], "pending")
        self.assertIn("reconciliation-required", attempt["outcome_detail"])

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
        self.ledger.policy = dataclasses.replace(self.ledger.policy, harness_revision="e" * 40)
        self.assertGreater(self.controller.renew_task(task_id, task.claim_token), self.clock())
        self.profile.raw["authority_generation"] = "2" * 32
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

    def test_moving_execution_caller_pins_content_changes_but_refuses_control_changes(self):
        from tauceti_queue.source_ci import SourceCandidateCI

        approved = self.repo.main_sha()
        self.profile.raw["execution"] = {"workflow_revision": approved}
        self.ledger.policy = dataclasses.replace(self.ledger.policy, execution=self.github_id)
        moved = self.github.create_pr(99, {"Content.lean": "content\n"})
        self.github.fast_forward(self.controller.repository, "main", approved, moved)
        with patch.object(SourceCandidateCI, "prepare", return_value="prepared"):
            self.assertEqual(self.ci.prepare({}, [], "1" * 32), "prepared")
            self.assertEqual(self.ledger.workflow_revision, moved)
        changed = self.github.create_pr(100, {"scripts/unsafe.py": "changed execution\n"})
        self.github.fast_forward(self.controller.repository, "main", moved, changed)
        with self.assertRaisesRegex(ValueError, "execution control files changed"):
            self.ci.prepare({}, [], "2" * 32)

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
