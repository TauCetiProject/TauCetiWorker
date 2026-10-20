#!/usr/bin/env python3
"""Recovery protocol faults with real private Git clones/CAS pushes and fake paid/verifier boundaries."""

import copy
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import repair_recovery as rr
from tauceti_worker import work_recovery as saved
from tauceti_worker.agents import AuthoringProfile
from tauceti_worker.runtime_status import read_json
from tauceti_worker.verification import digest, inventory


def git(repo, *args):
    return saved._git(repo, *args, text=True).strip()


class Death(BaseException):
    pass


class FakeVerification:
    calls = 0
    failures = 0

    def __init__(self, root, state, *, base=None, **kwargs):
        self.root, self.base = root, base
        self.path = state / "fake-receipt.json"

    def inputs(self):
        return dict(sources=inventory(self.root, self.base))

    def run(self):
        FakeVerification.calls += 1
        if FakeVerification.failures:
            FakeVerification.failures -= 1
            return dict(status="failed", error="source failure")
        return dict(status="success", fingerprint=digest(self.inputs()), receipt=str(self.path))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.co = self.root / "source"
        self.co.mkdir()
        git(self.co, "init", "-q", "-b", "main")
        git(self.co, "config", "user.name", "fixture")
        git(self.co, "config", "user.email", "fixture@example.com")
        git(self.co, "config", "commit.gpgsign", "false")
        (self.co / ".gitignore").write_text(".lake/\n")
        (self.co / "A.lean").write_text("original\n")
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "base")
        self.base = git(self.co, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.co), str(self.remote)], check=True)
        git(self.remote, "update-ref", "refs/pull/10/head", self.base)
        git(self.co, "remote", "add", "origin", str(self.remote))
        git(self.co, "fetch", "-q", "origin")
        (self.co / "A.lean").write_text("staged\n")
        git(self.co, "add", "A.lean")
        (self.co / "A.lean").write_text("retained repair\n")
        (self.co / "binary").write_bytes(b"\x00\xff\x01")
        (self.co / "link").symlink_to("A.lean")
        self.profile = AuthoringProfile("claude", "test-opus", "high", "test", "test")
        self.cfg = SimpleNamespace(
            state=self.root / "state" / "w", checkout=self.co, wid="w", logdir=self.root / "logs"
        )
        self.owner = dict(
            round_id="round-10",
            pr=10,
            work="fix",
            expected_head=self.base,
            authorization=dict(
                actor="alice",
                author="alice",
                repository="alice/repo",
                ref="main",
                sandbox_host=True,
                profile=asdict(self.profile),
                charged=["fix-10-head"],
                budget=3,
            ),
        )
        self.record = saved._record(
            self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=self.owner
        )
        self.manifest = saved._complete(self.record)
        self.current = dict(
            head=self.base,
            state="OPEN",
            ref="main",
            repository="alice/repo",
            author="alice",
            labels=[],
            draft=False,
            base="main",
            targets=[],
        )
        claims = SimpleNamespace(held=None, lost=threading.Event())

        def acquire(*args):
            claims.held = ("branch/10", "claims")
            os.environ.update(
                TAUCETI_PUSH_REF="main", TAUCETI_PUSH_EXPECT=self.current["head"], TAUCETI_PUSH_REMOTE=str(self.remote)
            )
            return True

        claims.begin_branch_work = acquire
        claims.stop_heartbeat = lambda: None
        claims.release = lambda: setattr(claims, "held", None)
        self.values = {}
        counters = SimpleNamespace(read=lambda k: self.values.get(k, 0), write=lambda k, v: self.values.update({k: v}))
        self.w = SimpleNamespace(
            cfg=self.cfg,
            gh=SimpleNamespace(repo="upstream/repo", pr_view=lambda *args: self.raw_facts()),
            claims=claims,
            rs=SimpleNamespace(maintenance_blocked=lambda *a, **kw: False),
            counters=counters,
            rc=SimpleNamespace(deadline=__import__("time").time() + 3600),
        )
        self.opts = SimpleNamespace(
            only=[], prs=(), sandbox_host=True, agent="claude", account=None, ignore_quota=False, dry_run=False
        )
        self.patches = [
            patch.dict(os.environ, {"TAUCETI_CLAIM_KEY": "", "TAUCETI_AUTO_RECOVERY": "on"}),
            patch.object(rr, "repository_url", lambda repo: str(self.remote)),
            patch("tauceti_worker.github.me", return_value="alice"),
            patch("tauceti_worker.github.can_push", return_value=True),
            patch("tauceti_worker.containment.strong", return_value=True),
            patch("tauceti_worker.containment.unconfirmed", return_value=[]),
            patch("tauceti_worker.config.roadmap_only", return_value="any"),
            patch("tauceti_worker.config.roadmap_skip", return_value=[]),
            patch("tauceti_worker.github_outbox.drain", return_value=[]),
            patch("tauceti_worker.github_outbox.pending_for", return_value=False),
            patch("tauceti_worker.verification.Verification", FakeVerification),
            patch("tauceti_worker.agents.run_agent_host", side_effect=AssertionError("Unexpected paid invocation")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        FakeVerification.calls = FakeVerification.failures = 0
        self.backend = rr.WorkerBackend(self.w, self.opts)
        real_publish = self.backend.publish

        def publish(*args):
            real_publish(*args)
            self.current["head"] = git(self.remote, "rev-parse", "main")

        self.backend.publish = publish

    def raw_facts(self):
        f = self.current
        return dict(
            number=10,
            state=f["state"],
            headRefOid=f["head"],
            headRefName=f["ref"],
            headRepositoryOwner=dict(login=f["repository"].split("/")[0]),
            headRepository=dict(name=f["repository"].split("/")[1]),
            author=dict(login=f["author"]),
            labels=[dict(name=label) for label in f["labels"]],
            isDraft=f["draft"],
            baseRefName=f["base"],
        )

    def run_recovery(self):
        return rr.Recovery(self.record, self.backend).run()

    def journal(self):
        return read_json(self.record / "recovery.json")

    def retry_now(self):
        j = self.journal()
        j["retry_at"] = 0
        rr.write(self.record, j, j["state"])

    def assert_retained(self):
        self.assertEqual(saved._complete(self.record), self.manifest)
        co = self.record / "checkout"
        if co.exists():
            self.assertEqual((co / "A.lean").read_text(), "retained repair\n")
            self.assertEqual((co / "binary").read_bytes(), b"\x00\xff\x01")
            self.assertEqual(os.readlink(co / "link"), "A.lean")

    def model_repair(self, checkout, *args, **kwargs):
        (checkout / "A.lean").write_text("retained repair\n-- repaired\n")
        return 0

    def test_rewritten_head_does_not_resurrect_removed_history(self):
        git(self.co, "reset", "--hard", self.base)
        (self.co / "Dropped.lean").write_text("intentionally removed\n")
        git(self.co, "add", "Dropped.lean")
        git(self.co, "commit", "-qm", "later dropped")
        old_head = git(self.co, "rev-parse", "HEAD")
        self.owner["expected_head"] = old_head
        (self.co / "A.lean").write_text("retained delta\n")
        self.save_updated_checkpoint()
        self.advance_writer()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "head-rewritten")
        self.assertEqual(FakeVerification.calls, 0)
        self.assertNotIn("Dropped.lean", git(self.remote, "ls-tree", "--name-only", "main"))
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_successful_round_leftovers_are_archived(self):
        self.owner["round_outcome"] = dict(published_head=self.base, exit_code=0)
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["round_outcome"] = self.owner["round_outcome"]
        self.assertFalse(recovery.run())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(self.journal()["reason"]["code"], "post-publication-leftovers")
        self.assertEqual(FakeVerification.calls, 0)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_push_marker_survives_death_before_round_finally(self):
        self.owner["publication_marker"] = dict(ref="refs/tauceti/published/push/main", before=None)
        git(self.co, "update-ref", "refs/tauceti/published/push/main", self.base)
        (self.co / "New.lean").write_text("later idea\n")
        self.save_updated_checkpoint()
        self.assertEqual(self.manifest["round_outcome"]["published_head"], self.base)
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "post-publication-leftovers")

    def test_interrupted_continuation_preserves_new_source_without_another_model(self):
        FakeVerification.failures = 1

        def interrupted(checkout, *args, **kwargs):
            (checkout / "New.lean").write_bytes(b"new retained source\n")
            raise Death()

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=interrupted),
        ):
            with self.assertRaises(Death):
                self.run_recovery()
        self.assertTrue(self.journal()["content_started"])
        self.assertNotIn("source_checkpoint_for", self.journal())
        self.assertTrue(self.run_recovery())
        journal = self.journal()
        self.assertEqual(journal["source_checkpoint_for"], journal["content_started"])
        self.assertIn("New.lean", saved._complete(Path(journal["source_checkpoint"]))["untracked"])
        self.assertEqual(git(self.remote, "show", "main:New.lean"), "new retained source")
        self.assertEqual(journal["content_attempts"], 1)

    def test_owned_conflict_merge_can_be_resolved_and_committed_by_supervisor(self):
        self.advance_writer(conflicting=True)
        writer_head = self.current["head"]

        def resolve(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("retained repair plus other writer\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve),
        ):
            self.assertTrue(self.run_recovery())
        parents = git(self.remote, "show", "-s", "--format=%P", "main").split()
        self.assertIn(writer_head, parents)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair plus other writer")

    def test_restart_after_owned_merge_is_resolved_needs_no_second_model(self):
        self.advance_writer(conflicting=True)

        def resolve_and_die(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("both writers\n")
            git(checkout, "add", "A.lean")
            raise Death()

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve_and_die),
        ):
            with self.assertRaises(Death):
                self.run_recovery()
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "both writers")

    def test_budget_wait_does_not_repeat_known_failed_build(self):
        FakeVerification.failures = 1
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("budget closed")):
            self.assertFalse(self.run_recovery())
            self.retry_now()
            with patch.object(self.backend, "restore", side_effect=AssertionError("restored during budget wait")):
                self.assertFalse(self.run_recovery())
        self.assertEqual(FakeVerification.calls, 1)
        self.assertEqual(self.journal()["attempts"], 0)

    def test_verified_provider_outage_refunds_content_but_counts_infrastructure(self):
        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", return_value=1),
            patch("tauceti_worker.agents.take_last_agent_infra_failure", return_value="529 before work"),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 0)
        self.assertEqual(self.journal()["attempts"], 1)
        self.assertEqual(self.journal()["provider_refunds"], 1)
        self.assertEqual(self.values["fix-10-head"], 0)

    def test_post_launch_file_error_does_not_refund_content(self):
        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=FileNotFoundError("after launch")),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 1)
        self.assertEqual(self.values["fix-10-head"], 1)

    def test_typed_process_creation_failure_refunds_content(self):
        from tauceti_worker.agents import AgentLaunchUnavailable

        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=AgentLaunchUnavailable("missing binary")),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 0)
        self.assertEqual(self.journal()["attempts"], 1)
        self.assertEqual(self.values["fix-10-head"], 0)

    def test_automatic_loop_preserves_original_recovery_provider_policy(self):
        from tauceti_worker import loop

        tails = []

        def run(tail):
            tails.append(tail)
            raise KeyboardInterrupt()

        with (
            patch.object(rr, "due", return_value=False),
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "api_mode", return_value=False),
            patch.object(loop, "choose_model", return_value=("codex", {})),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", run),
        ):
            args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
            self.assertEqual(loop.cmd_loop(args, self.cfg, only=["fix"], agent="auto"), 130)
        tail = tails[0]
        self.assertEqual(tail[tail.index("--agent") + 1], "codex")
        self.assertEqual(tail[tail.index("--recovery-agent") + 1], "auto")
        self.assertIn("--recovery-unpinned-profile", tail)

    def test_blocked_head_change_reenables_mechanical_recovery(self):
        rr.write(
            self.record,
            {},
            "blocked",
            observed_head=self.base,
            reason=rr.reason(rr.Block("content-allowance", "spent")),
        )
        self.advance_writer()
        self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "eligible")
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "published")

    def test_archived_backlog_does_not_delay_new_checkpoint(self):
        for n in range(30):
            record = self.cfg.state / "recovery" / f"archived-{n}"
            rr.write(record, {}, "obsolete")
            blocked = self.cfg.state / "recovery" / f"blocked-{n}"
            rr.write(blocked, {}, "blocked", reason=rr.reason(rr.Block("missing-publication-scope", "legacy")))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "published")

    def test_blocked_checkpoint_observes_closed_pr_without_hashing_payload(self):
        rr.write(self.record, {}, "blocked", reason=rr.reason(rr.Block("content-allowance", "spent")))
        self.current["state"] = "CLOSED"
        self.assertTrue(rr.due(self.cfg))
        with patch.object(saved, "_complete", side_effect=AssertionError("metadata probe rehashed source")):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_mechanical_zero_paid_and_duplicate(self):
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["state"], "published")
        self.assertFalse(self.run_recovery())
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")
        self.assertEqual(FakeVerification.calls, 1)
        self.assert_retained()

    def test_state_boundary_crashes(self):
        # Reinitialize the fixture independently at each persisted state boundary.
        for state in ("retained", "eligible", "resuming", "verified", "publication-pending", "published"):
            with self.subTest(state=state):
                case = Fixture()
                case.setUp()
                try:
                    actual = rr.write
                    crashed = False

                    def die(record, journal, next_state, actual=actual, state=state, **fields):
                        nonlocal crashed
                        actual(record, journal, next_state, **fields)
                        if next_state == state and not crashed:
                            crashed = True
                            raise Death()

                    with patch.object(rr, "write", die), self.assertRaises(Death):
                        case.run_recovery()
                    case.run_recovery()
                    self.assertEqual(case.journal()["state"], "published")
                    self.assertEqual(git(case.remote, "rev-list", "--count", "main"), "2")
                    case.assert_retained()
                finally:
                    case.doCleanups()

    def test_death_after_successful_push(self):
        actual = self.backend.publish

        def die(*args):
            actual(*args)
            raise Death()

        with patch.object(self.backend, "publish", die), self.assertRaises(Death):
            self.run_recovery()
        self.assertEqual(self.journal()["state"], "publication-pending")
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["state"], "published")
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")

    def test_three_failures_block_and_operator_rearms(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("disk full")):
            for n in range(1, 4):
                self.assertFalse(self.run_recovery())
                self.assertEqual(self.journal()["attempts"], n)
                self.retry_now()
            self.assertEqual(self.journal()["state"], "blocked")
            self.assertFalse(self.run_recovery())
        rr.atomic_json(self.record / "operator.json", dict(retry_epoch=1))
        self.assertTrue(self.run_recovery())
        self.assert_retained()

    def test_claim_and_capacity_wait_free(self):
        with patch.object(self.backend, "acquire", side_effect=rr.Wait("claim", "busy")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["attempts"], 0)
        self.retry_now()
        with patch.object(self.backend, "verify", side_effect=rr.Wait("verification-capacity", "busy")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["attempts"], 0)
        self.assert_retained()

    def test_closed_and_held_precise(self):
        self.current["labels"] = ["hold"]
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "pr-held")
        self.assertEqual(self.journal()["attempts"], 0)
        self.retry_now()
        self.current["state"] = "CLOSED"
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(self.journal()["reason"]["code"], "pr-closed")

    def test_source_already_superseded(self):
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        saved.restore(self.record, other)
        git(other, "add", "-A")
        git(
            other,
            "-c",
            "user.name=other",
            "-c",
            "user.email=other@example.com",
            "commit",
            "-qm",
            "independently repaired",
        )
        git(other, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(self.journal()["reason"]["code"], "source-superseded")
        self.assertEqual(FakeVerification.calls, 0)

    def advance_writer(self, *, conflicting=False, already_integrated=False):
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        if already_integrated:
            saved.restore(self.record, other)
        (other / ("A.lean" if conflicting else "Other.lean")).write_text("other writer\n")
        git(other, "add", "-A")
        git(other, "-c", "user.name=other", "-c", "user.email=other@example.com", "commit", "-qm", "other writer")
        git(other, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")

    def test_head_moved_clean_merge_uses_zero_paid_invocations(self):
        self.advance_writer()
        self.assertTrue(self.run_recovery())
        self.assertTrue(self.journal()["mechanical_integration"])
        self.assertEqual(git(self.remote, "show", "main:Other.lean"), "other writer")
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")

    def test_head_moved_conflict_requires_admitted_continuation(self):
        self.advance_writer(conflicting=True)
        with patch.object(self.backend, "continue_source", side_effect=rr.Wait("budget-refused", "no funds")) as model:
            self.assertFalse(self.run_recovery())
        self.assertEqual(model.call_args.args[4], "remote-head-moved-reintegration-required")
        self.assertEqual(self.journal()["reason"]["code"], "budget-refused")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.current["head"])
        self.assertEqual(saved._complete(self.record), self.manifest)
        self.assertIn("retained repair", (self.record / "checkout/A.lean").read_text())
        self.assertEqual(git(self.record / "checkout", "rev-parse", "MERGE_HEAD"), self.current["head"])

    def test_other_writer_superseded_source_plus_more(self):
        self.advance_writer(already_integrated=True)
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(FakeVerification.calls, 0)

    def test_claim_loss_and_changed_head_before_push(self):
        actual = self.backend.verify

        def verify(*args):
            result = actual(*args)
            self.w.claims.lost.set()
            return result

        with patch.object(self.backend, "verify", verify):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "claim-or-head-changed")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_unrelated_outbox_does_not_hide_confirmed_git_publication(self):
        with (
            patch("tauceti_worker.github_outbox.pending_for", return_value=True),
            patch("tauceti_worker.github_outbox.drain") as drain,
        ):
            self.assertTrue(self.run_recovery())
            drain.assert_not_called()
        self.assertFalse(self.run_recovery())
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")

    def test_remote_uncertainty_is_reconciled_before_replay(self):
        with patch.object(self.backend, "reconcile", side_effect=RuntimeError("remote read unavailable")):
            for _ in range(3):
                self.assertFalse(self.run_recovery())
                self.retry_now()
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")

    def test_changed_source_after_verify_not_published(self):
        actual = self.backend.prepare

        def mutate(checkout, *args):
            (checkout / "A.lean").write_text("unverified mutation\n")
            return actual(checkout, *args)

        with patch.object(self.backend, "prepare", mutate):
            self.assertFalse(self.run_recovery())
        self.assertIn("inputs changed", self.journal()["reason"]["detail"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_recovery_commit_does_not_execute_source_mutating_hooks(self):
        actual = self.backend.prepare

        def install_hook(checkout, *args):
            hook = checkout / ".git/hooks/pre-commit"
            hook.write_text('#!/bin/sh\nprintf "unverified hook change\\n" > A.lean\ngit add A.lean\n')
            hook.chmod(0o755)
            return actual(checkout, *args)

        with patch.object(self.backend, "prepare", install_hook):
            self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")

    def test_final_push_boundary_rechecks_exact_inputs(self):
        actual = self.backend.prepare

        def change_inputs(checkout, *args):
            publication = actual(checkout, *args)
            self.backend.verification_fingerprint = lambda *args: "changed-toolchain-or-source"
            return publication

        with patch.object(self.backend, "prepare", change_inputs):
            self.assertFalse(self.run_recovery())
        self.assertIn("final push boundary", self.journal()["reason"]["detail"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_justified_continuation_fresh_admission_and_reverify(self):
        FakeVerification.failures = 1
        self.opts.ignore_quota = True
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)) as admission,
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.model_repair) as model,
        ):
            self.assertTrue(self.run_recovery())
        admission.assert_called_once()
        self.assertTrue(admission.call_args.kwargs["fresh"])
        self.assertFalse(admission.call_args.kwargs["ignore_quota"])
        model.assert_called_once()
        self.assertEqual(FakeVerification.calls, 2)
        self.assertEqual(self.journal()["continuation_reason"], "exact-source-verification-failed")
        self.assertEqual(self.journal()["content_attempts"], 1)
        self.assertEqual(self.values["fix-10-head"], 1)

    def test_verification_failure_budget_refusal_retains_source(self):
        FakeVerification.failures = 1
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("budget closed")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "budget-refused")
        self.assertEqual(self.journal()["attempts"], 0)
        self.assert_retained()

    def test_identity_profile_mismatch_new_narrow_conversation(self):
        value = dict(profile=asdict(self.profile), actor="alice", identity="credential-1", session="session-12345")
        with patch.object(rr, "conversation_identity", return_value="credential-1"):
            self.assertEqual(rr.valid_conversation(value, self.profile, "alice"), "session-12345")
            self.assertIsNone(rr.valid_conversation(value, self.profile, "bob"))
            self.assertIsNone(rr.valid_conversation(value, self.profile, "alice", scope=dict(pr=11)))
            wrong = copy.deepcopy(value)
            wrong["profile"]["model"] = "other"
            self.assertIsNone(rr.valid_conversation(wrong, self.profile, "alice"))
        with patch.object(rr, "conversation_identity", return_value="credential-2"):
            self.assertIsNone(rr.valid_conversation(value, self.profile, "alice"))
        value["identity"] = None
        with patch.object(rr, "conversation_identity", return_value=None):
            self.assertIsNone(rr.valid_conversation(value, self.profile, "alice"))

    def test_rollback_preserves_everything(self):
        with (
            patch.dict(os.environ, TAUCETI_AUTO_RECOVERY="off"),
            patch.object(rr.Recovery, "run", side_effect=AssertionError),
        ):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assert_retained()

    def test_ownership_fork_and_original_scope_restrictions(self):
        self.current["repository"] = "other/repo"
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "ownership-changed")
        self.current["repository"] = "alice/repo"
        self.retry_now()
        self.opts.prs = (11,)
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "pr-policy")

    def test_loop_recovery_precedes_model_budget(self):
        from tauceti_worker import loop

        args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
        tails = []

        def run(tail):
            tails.append(tail)
            raise KeyboardInterrupt()

        with (
            patch.object(loop, "api_mode", return_value=True),
            patch.object(loop, "Budget", side_effect=AssertionError("Mechanical recovery must precede model funding")),
            patch.object(loop, "choose_model", side_effect=AssertionError("No model selection for recovery")),
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", run),
            patch.dict(os.environ, TAUCETI_USE_BUDGET="1"),
        ):
            self.assertEqual(loop.cmd_loop(args, self.cfg, only=["fix"], agent="claude", prs=(10,)), 130)
        self.assertIn("--recovery-only", tails[0])
        self.assertIn("10", tails[0])
        self.assertNotIn("--ignore-quota", tails[0])

    def test_continuation_publication_helpers_fail_closed(self):
        from tauceti_worker import github_transport

        with patch.dict(os.environ, TAUCETI_RECOVERY_CONTINUATION="1"):
            result = subprocess.run(
                [str(Path(rr.__file__).parent.parent / "scripts/git-safe-push")], cwd=self.co, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            result = github_transport.execute(["gh", "pr", "comment", "10", "--body", "unverified"])
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_resume_identity_recorded_from_structured_events(self):
        from tauceti_worker.agents import run_agent_proc

        path = self.root / "session.json"
        rr.atomic_json(path, dict(actor="alice", profile=asdict(self.profile), identity="fixture"))
        script = 'print(\'{"type":"system","subtype":"init","session_id":"session-12345"}\')'
        rc = run_agent_proc(
            [sys.executable, "-c", script],
            env=os.environ.copy(),
            cwd=self.co,
            logdir=self.cfg.logdir,
            label="fixture",
            provider="claude",
            contain=False,
            session_path=path,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(read_json(path)["session"], "session-12345")

    def test_retry_throttling_does_not_restore_or_launch(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("disk full")):
            self.assertFalse(self.run_recovery())
        with patch.object(self.backend, "restore", side_effect=AssertionError("backoff ignored")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["attempts"], 1)

    def save_updated_checkpoint(self):
        self.record = saved._record(
            self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=self.owner
        )
        self.manifest = saved._complete(self.record)

    def test_ignored_and_scratch_files_are_archived_without_publication(self):
        (self.co / ".gitignore").write_text(".lake/\nprivate-draft.txt\n")
        (self.co / "private-draft.txt").write_text("retained draft bytes")
        (self.co / "notes.md").write_text("retained scratch bytes")
        (self.co / ".claude").mkdir()
        (self.co / ".claude/settings.local.json").write_text("private settings")
        self.save_updated_checkpoint()
        self.assertTrue(self.run_recovery())
        paths = git(self.remote, "ls-tree", "-r", "--name-only", "main").splitlines()
        for rel in ("private-draft.txt", "notes.md", ".claude/settings.local.json"):
            self.assertNotIn(rel, paths)
        self.assertTrue(list(self.record.glob("retained-extra/*/notes.md")))
        self.assert_retained()

    def test_scratch_only_record_is_obsolete_without_build_or_model(self):
        git(self.co, "reset", "--hard", self.base)
        (self.co / "binary").unlink()
        (self.co / "link").unlink()
        (self.co / "notes.md").write_text("draft diagnostics")
        self.save_updated_checkpoint()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(FakeVerification.calls, 0)

    def test_label_churn_does_not_reset_infrastructure_allowance(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("infrastructure")):
            for count in range(1, 4):
                self.current["labels"] = [f"status/{count}"]
                self.retry_now() if self.journal() else None
                self.assertFalse(self.run_recovery())
                self.assertEqual(self.journal()["attempts"], count)
        self.assertEqual(self.journal()["state"], "blocked")

    def test_no_model_cache_outage_preserves_content_allowance(self):
        from tauceti_worker.agents import BuildCacheUnavailable

        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=BuildCacheUnavailable("cache offline")),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 0)
        self.assertEqual(self.values["fix-10-head"], 0)
        self.assertEqual(self.journal()["attempts"], 1)

    def test_blocked_backlog_does_not_schedule_recovery_only_children(self):
        for n in range(40):
            record = self.cfg.state / "recovery" / f"blocked-{n}"
            rr.write(
                record, {}, "blocked", reason=rr.reason(rr.Block("missing-publication-scope", "legacy")), retry_at=0
            )
        rr.write(
            self.record,
            {},
            "blocked",
            reason=rr.reason(rr.Block("original-authorization-missing", "legacy")),
            retry_at=0,
        )
        self.assertFalse(rr.due(self.cfg))
        with patch.object(saved, "_complete", side_effect=AssertionError("Blocked records must not rehash source")):
            self.assertFalse(rr.recover(self.w, self.opts))
        from tauceti_worker import loop

        tails = []

        def run(tail):
            tails.append(tail)
            raise KeyboardInterrupt()

        with (
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "api_mode", return_value=False),
            patch.object(loop, "choose_model", return_value=("claude", {})),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", run),
        ):
            args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
            self.assertEqual(loop.cmd_loop(args, self.cfg, only=["fix"], agent="claude"), 130)
        self.assertNotIn("--recovery-only", tails[0])
        rr.atomic_json(self.record / "operator.json", dict(retry_epoch=1))
        self.assertTrue(rr.due(self.cfg))

    def test_one_invocation_even_when_capacity_refunds_attempt(self):
        other = self.record.parent / "second"
        other.mkdir()
        for path in self.record.iterdir():
            if path.is_file():
                (other / path.name).write_bytes(path.read_bytes())
        with patch.object(rr.WorkerBackend, "verify", side_effect=rr.Wait("verification-capacity", "busy")) as verify:
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(verify.call_count, 1)

    def test_unfinished_native_git_operation_is_durably_blocked(self):
        (self.co / ".git/MERGE_HEAD").write_text(self.base + "\n")
        self.save_updated_checkpoint()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "incomplete-git-operation")
        self.assertFalse((self.record / "checkout").exists())

    def test_git_clean_filter_cannot_publish_different_bytes(self):
        (self.co / ".gitattributes").write_text("A.lean filter=poison\n")
        git(self.co, "add", ".gitattributes")
        self.save_updated_checkpoint()
        actual = self.backend.verify

        def configure_filter(checkout, *args):
            git(checkout, "config", "filter.poison.clean", "sed s/retained/unverified/g")
            return actual(checkout, *args)

        with patch.object(self.backend, "verify", configure_filter):
            self.assertFalse(self.run_recovery())
        self.assertIn("Committed source differs", self.journal()["reason"]["detail"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_terminal_recovery_evicts_only_rebuildable_caches(self):
        actual = self.backend.verify

        def cache(checkout, *args):
            (checkout / ".lake").mkdir(exist_ok=True)
            (checkout / ".lake/large-build").write_bytes(b"cache")
            return actual(checkout, *args)

        with patch.object(self.backend, "verify", cache):
            self.assertTrue(self.run_recovery())
        self.assertFalse((self.record / "checkout/.lake").exists())
        self.assert_retained()

    def test_consumer_status_is_distinct_from_moved_aside_checkout(self):
        rr.write(self.record, {}, "retained")
        summary = saved.summary(self.cfg.state.parent, workers={"w"})
        self.assertEqual(summary["recovery_retained"], 1)
        self.assertEqual(summary["retained"], 0)
        rr.write(
            self.record, {}, "blocked", reason=rr.reason(rr.Block("repair", "precise failure", "restore dependency"))
        )
        summary = saved.summary(self.cfg.state.parent, workers={"w"})
        self.assertIn("next action: restore dependency", summary["reasons"][0])

    def test_valid_original_conversation_resumes_in_original_cwd_alias(self):
        FakeVerification.failures = 1
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["conversation"] = dict(
            profile=asdict(self.profile),
            actor="alice",
            identity="account-a",
            session="session-12345",
            scope=dict(repository="upstream/repo", pr=10, source_round="round-10"),
        )
        with (
            patch.object(rr, "conversation_identity", return_value="account-a"),
            patch.object(rr, "session_available", return_value=True),
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.model_repair) as model,
        ):
            self.assertTrue(recovery.run())
        self.assertEqual(model.call_args.kwargs["resume"], "session-12345")
        self.assertEqual(model.call_args.kwargs["original_cwd"], self.co)

    def test_continuation_raw_git_push_is_denied_but_supervisor_can_publish(self):
        actual = self.backend.verify

        def try_push(checkout, *args):
            git(checkout, "add", "-u")
            git(
                checkout,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.com",
                "commit",
                "-qm",
                "private source checkpoint",
            )
            with rr.environment(TAUCETI_RECOVERY_CONTINUATION="1"):
                attempt = subprocess.run(
                    ["git", "-C", str(checkout), "push", str(self.remote), "HEAD:main"], capture_output=True
                )
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn(b"supervisor", attempt.stderr)
            self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
            return actual(checkout, *args)

        with patch.object(self.backend, "verify", try_push):
            self.assertTrue(self.run_recovery())

    def test_lifted_hold_reenables_a_previously_exhausted_checkpoint(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            for _ in range(3):
                self.run_recovery()
                self.retry_now()
        self.current["labels"] = ["hold"]
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "pr-held")
        self.current["labels"] = []
        self.retry_now()
        self.assertTrue(self.run_recovery())

    def test_changed_round_scope_reenables_local_policy_wait(self):
        self.opts.only = ["review"]
        self.assertFalse(self.run_recovery())
        self.assertFalse(rr.due(self.cfg, only=["review"]))
        self.assertTrue(rr.due(self.cfg, only=["fix"]))
        self.opts.only = ["fix"]
        self.assertTrue(self.run_recovery())

    def test_api_refusal_before_admission_refunds_content(self):
        import contextlib

        FakeVerification.failures = 1
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["authorization"]["billing"] = "api"
        api = SimpleNamespace(blocked_reason="waiting for funding", launched=False)
        with (
            patch("tauceti_worker.claude_api.api_mode", return_value=True),
            patch("tauceti_worker.claude_api.api_context", return_value=contextlib.nullcontext(api)) as admission,
            patch(
                "tauceti_worker.loop.resolve_work_model",
                side_effect=AssertionError("API mode must use fresh budget admission"),
            ),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", return_value=75),
        ):
            self.assertFalse(recovery.run())
        admission.assert_called_once()
        self.assertEqual(self.journal()["content_attempts"], 0)
        self.assertEqual(self.values["fix-10-head"], 0)
        self.assertEqual(self.journal()["attempts"], 0)

    def test_late_api_refusal_preserves_spent_content_allowance(self):
        import contextlib

        FakeVerification.failures = 1
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["authorization"]["billing"] = "api"
        api = SimpleNamespace(blocked_reason=None, launched=False)

        def admitted(*args, **kwargs):
            api.launched = True
            api.blocked_reason = "waiting for funding after an admitted request"
            return 75

        with (
            patch("tauceti_worker.claude_api.api_mode", return_value=True),
            patch("tauceti_worker.claude_api.api_context", return_value=contextlib.nullcontext(api)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", admitted),
        ):
            self.assertFalse(recovery.run())
        self.assertEqual(self.journal()["content_attempts"], 1)
        self.assertEqual(self.values["fix-10-head"], 1)
        self.assertTrue(Path(self.journal()["source_checkpoint"]).is_dir())

    def test_late_capacity_refusal_before_launch_does_not_spend_content(self):
        from tauceti_worker.config import AdmissionUnavailable

        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch(
                "tauceti_worker.agents.run_agent_host",
                side_effect=AdmissionUnavailable("capacity reserved", state="waiting-capacity"),
            ),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 0)
        self.assertEqual(self.values["fix-10-head"], 0)
        self.assertEqual(self.journal()["attempts"], 0)

    def test_fresh_paid_dispatch_cannot_bypass_retained_source_on_same_pr(self):
        from tauceti_worker import work_units

        self.assertTrue(rr.pending_for(self.cfg, "upstream/repo", 10))
        candidate = SimpleNamespace(pr=10)
        options = SimpleNamespace(dry_run=False, sandbox_host=True)
        with patch.object(
            work_units, "_check_stage_read_quota", side_effect=AssertionError("Fresh repair bypassed retained source")
        ):
            self.assertIsNone(work_units.dispatch("fix", self.w, None, candidate, options))
        rr.write(self.record, {}, "published")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_damaged_checkpoint_never_runs(self):
        (self.record / "staged.patch").write_text("corrupt")
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "corrupt-checkpoint")
        self.assertFalse((self.record / "checkout").exists())


class ConversationIsolation(unittest.TestCase):
    def test_oauth_refresh_keeps_account_identity_and_switch_invalidates_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for provider, envkey, filename, initial, refreshed in (
                (
                    "codex",
                    "CODEX_HOME",
                    "auth.json",
                    {"tokens": {"account_id": "account-a", "access_token": "old"}},
                    {"tokens": {"account_id": "account-a", "access_token": "new"}},
                ),
                (
                    "claude",
                    "CLAUDE_CONFIG_DIR",
                    ".credentials.json",
                    {"claudeAiOauth": {"accountUuid": "account-a", "accessToken": "old"}},
                    {"claudeAiOauth": {"accountUuid": "account-a", "accessToken": "new"}},
                ),
            ):
                with patch.dict(os.environ, {envkey: str(root), "TAUCETI_CLAUDE_BILLING": "subscription"}):
                    rr.atomic_json(root / filename, initial)
                    before = rr.conversation_identity(provider)
                    self.assertIsNotNone(before)
                    rr.atomic_json(root / filename, refreshed)
                    self.assertEqual(before, rr.conversation_identity(provider))
                    rr.atomic_json(root / filename, {})
                    self.assertIsNone(rr.conversation_identity(provider))

    def test_cached_profile_cannot_bind_opaque_mirrored_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "isolated"
            rr.atomic_json(root / ".credentials.json", dict(claudeAiOauth=dict(accessToken="opaque-old")))
            rr.atomic_json(Path(str(root) + ".json"), dict(oauthAccount=dict(accountUuid="stale-account")))
            with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(root), TAUCETI_CLAUDE_BILLING="subscription"):
                self.assertIsNone(rr.conversation_identity("claude"))
                rr.atomic_json(root / ".credentials.json", dict(claudeAiOauth=dict(accessToken="new-account-token")))
                self.assertIsNone(rr.conversation_identity("claude"))

    def test_original_absolute_cwd_maps_to_isolated_source(self):
        from tauceti_worker import agents, containment

        if not containment.capability().strong:
            self.skipTest("rootless PID namespace unavailable")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original, isolated = root / "original", root / "isolated"
            original.mkdir()
            isolated.mkdir()
            (original / "source").write_text("live")
            (isolated / "source").write_text("retained")
            profile = agents.AuthoringProfile("claude", "fixture", "high", "fixture", "fixture")
            script = f"from pathlib import Path; Path({str(original / 'source')!r}).write_text('repaired')"
            with patch.object(
                agents, "host_agent_argv", return_value=([sys.executable, "-c", script], os.environ.copy())
            ):
                self.assertEqual(
                    agents.run_agent_host(isolated, "fixture", profile, root / "logs", original_cwd=original), 0
                )
            self.assertEqual((original / "source").read_text(), "live")
            self.assertEqual((isolated / "source").read_text(), "repaired")


class RealVerifierIntegration(unittest.TestCase):
    def test_real_receipt_reuse_and_final_input_check(self):
        from verification import Receipts

        case = Receipts()
        case.setUp()
        self.addCleanup(case.doCleanups)
        (case.root / "TauCeti/A.lean").write_text("retained verified repair\n")
        cfg = SimpleNamespace(state=case.state, logdir=case.home / "logs")
        w = SimpleNamespace(cfg=cfg, rc=SimpleNamespace(deadline=__import__("time").time() + 60))
        backend = rr.WorkerBackend(w, SimpleNamespace())
        facts = dict(labels=[], ref="repair", head=case.base, repository="alice/repo")
        manifest = dict(id="real-receipt", authorization={})
        with patch("tauceti_worker.agents.warm_host_build_caches") as warm:
            first = backend.verify(case.root, manifest, facts)
            self.assertEqual(first["status"], "success", first.get("error"))
            second = backend.verify(case.root, manifest, facts)
            self.assertEqual(second["status"], "success", second.get("error"))
            self.assertTrue(all("reused_from" in p for p in second["phases"].values()))
            warm.assert_called()
            publication = backend.prepare(case.root, manifest, facts, second)
        self.assertEqual(publication["oid"], git(case.root, "rev-parse", "HEAD"))
        self.assertTrue(Path(publication["receipt"]).is_file())


if __name__ == "__main__":
    unittest.main()
