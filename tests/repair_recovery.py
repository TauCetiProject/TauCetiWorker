#!/usr/bin/env python3
"""Recovery protocol faults with real private Git clones/CAS pushes and fake paid/verifier boundaries."""

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
from tauceti_worker.agents import run_agent_host as run_real_agent_host
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
        self.values = {"historical-paid": 17}
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
        legacy = self.root / "activity/containment-unconfirmed"
        legacy.mkdir(parents=True)
        (legacy / "malformed.json").write_text("{broken")
        (legacy / "stale.json").write_text('{"boundary": "old", "pid_namespace": 42}')
        (self.cfg.state / "quiescence.json").write_text("not json")
        self.patches = [
            patch.dict(os.environ, TAUCETI_ACTIVITY_DIR=str(legacy.parent), TAUCETI_PROCESS_CONTAINMENT="required"),
            patch.dict(os.environ, {"TAUCETI_CLAIM_KEY": "", "TAUCETI_AUTO_RECOVERY": "on"}),
            patch.object(rr, "repository_url", lambda repo: str(self.remote)),
            patch("tauceti_worker.github.me", return_value="alice"),
            patch("tauceti_worker.github.can_push", return_value=True),
            patch("tauceti_worker.github_state.read", side_effect=lambda *a, **kw: (True, self.raw_facts())),
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
            body="",
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
        self.assertEqual(self.values["historical-paid"], 17)
        co = self.record / "checkout"
        if co.exists():
            self.assertEqual((co / "A.lean").read_text(), "retained repair\n")
            self.assertEqual((co / "binary").read_bytes(), b"\x00\xff\x01")
            self.assertEqual(os.readlink(co / "link"), "A.lean")

    def model_repair(self, checkout, *args, **kwargs):
        (checkout / "A.lean").write_text("retained repair\n-- repaired\n")
        return 0

    def rewritten_checkpoint(self, *, conflicting=False):
        git(self.co, "reset", "--hard", self.base)
        (self.co / "Dropped.lean").write_text("intentionally removed\n")
        git(self.co, "add", "Dropped.lean")
        git(self.co, "commit", "-qm", "later dropped")
        old_head = git(self.co, "rev-parse", "HEAD")
        self.owner["expected_head"] = old_head
        (self.co / "A.lean").write_text("retained delta\n")
        older = self.record
        self.save_updated_checkpoint()
        rr.write(
            older,
            {},
            "obsolete",
            reason=rr.reason(
                rr.Obsolete("source-replaced", "A newer checkpoint from the same round retains the current source")
            ),
        )
        self.advance_writer(conflicting=conflicting)
        return old_head

    def test_rewritten_head_does_not_resurrect_removed_history(self):
        old_head = self.rewritten_checkpoint()
        self.assertTrue(self.run_recovery())
        self.assertEqual(FakeVerification.calls, 1)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")
        self.assertNotIn("Dropped.lean", git(self.remote, "ls-tree", "--name-only", "main"))
        self.assertNotIn(old_head, git(self.remote, "rev-list", "main").split())
        self.assertEqual(saved._complete(self.record), self.manifest)
        transaction = self.journal()["delta_integration"]
        self.assertEqual(transaction["stage"], "applied")
        self.assertEqual(git(Path(transaction["archived"]), "show", "HEAD:A.lean"), "retained delta")

    def test_rewritten_conflict_resolves_under_original_admission(self):
        old_head = self.rewritten_checkpoint(conflicting=True)

        def resolve(checkout, prompt, *args, **kwargs):
            self.assertIn("never merge the old expected/source ancestry", prompt)
            (checkout / "A.lean").write_text("retained delta plus other writer\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertEqual(self.journal()["content_attempts"], 1)
        self.assertEqual(self.journal()["attempts"], 1)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta plus other writer")
        self.assertNotIn(old_head, git(self.remote, "rev-list", "main").split())

    def test_delta_handoff_crashes_resume_without_model_or_source_loss(self):
        for stage in ("prepared", "ready", "applied"):
            with self.subTest(stage=stage):
                case = Fixture()
                case.setUp()
                try:
                    case.rewritten_checkpoint()
                    original = rr.write

                    def die(record, journal, state, original=original, stage=stage, **fields):
                        original(record, journal, state, **fields)
                        if (fields.get("delta_integration") or {}).get("stage") == stage:
                            raise Death()

                    with patch.object(rr, "write", side_effect=die):
                        with self.assertRaises(Death):
                            case.run_recovery()
                    case.assertEqual(saved._complete(case.record), case.manifest)
                    case.assertTrue(case.run_recovery())
                    case.assertEqual(git(case.remote, "show", "main:A.lean"), "retained delta")
                    case.assertNotIn("Dropped.lean", git(case.remote, "ls-tree", "--name-only", "main"))
                    case.assertEqual(git(case.remote, "rev-list", "--count", "main"), "3")
                finally:
                    case.doCleanups()

    def test_delta_handoff_death_between_renames_resumes(self):
        self.rewritten_checkpoint()
        original = Path.rename

        def die(path, target):
            result = original(path, target)
            if Path(target).name.startswith("restore-delta-source-"):
                raise Death()
            return result

        with patch.object(Path, "rename", die):
            with self.assertRaises(Death):
                self.run_recovery()
        self.assertFalse((self.record / "checkout").exists())
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_interrupted_resolved_delta_verifies_without_another_model(self):
        self.rewritten_checkpoint(conflicting=True)

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
        self.assertEqual(self.journal()["content_attempts"], 1)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "both writers")

    def test_successful_round_leftovers_are_archived(self):
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "published repair")
        self.assertEqual(self.normal_push().returncode, 0)
        published = git(self.co, "rev-parse", "HEAD")
        self.current["head"] = published
        self.owner["round_outcome"] = dict(published_head=published, exit_code=0)
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["round_outcome"] = self.owner["round_outcome"]
        self.assertFalse(recovery.run())
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(self.journal()["reason"]["code"], "post-publication-leftovers")
        self.assertEqual(FakeVerification.calls, 0)
        self.assertEqual(git(self.remote, "rev-parse", "main"), published)

    def test_push_marker_survives_death_before_round_finally(self):
        self.owner["publication_marker"] = dict(ref="refs/tauceti/published/push/main", before=None)
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "published repair")
        self.assertEqual(self.normal_push().returncode, 0)
        published = git(self.co, "rev-parse", "HEAD")
        self.current["head"] = published
        (self.co / "New.lean").write_text("later idea\n")
        self.save_updated_checkpoint()
        self.assertEqual(self.manifest["round_outcome"]["published_head"], published)
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
        rr.write(self.record, {}, "blocked", reason=rr.reason(rr.Block("original-authorization-missing", "legacy")))
        self.current["state"] = "CLOSED"
        self.assertFalse(rr.due(self.cfg))
        with patch.object(saved, "_complete", side_effect=AssertionError("metadata probe rehashed source")):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_old_markers_and_wait_resume_without_losing_source_or_paid_counts(self):
        markers = self.root / "activity/containment-unconfirmed"
        markers.mkdir(parents=True, exist_ok=True)
        (markers / "malformed.json").write_text("{broken")
        (markers / "stale.json").write_text('{"boundary": "old", "pid_namespace": 42}')
        (self.cfg.state / "quiescence.json").write_text("not json")
        self.values["historical-paid"] = 17
        rr.write(
            self.record,
            {},
            "eligible",
            attempts=0,
            retry_at=0,
            reason=rr.reason(rr.Wait("containment-unavailable", "old global wait")),
            selection=rr.selection([], (), True, "claude"),
        )
        with patch.dict(
            os.environ, TAUCETI_ACTIVITY_DIR=str(markers.parent), TAUCETI_PROCESS_CONTAINMENT="invalid-old-policy"
        ):
            self.assertTrue(rr.due(self.cfg, only=[], agent="claude"))
            self.assertTrue(self.run_recovery())
        self.assertEqual(self.values["historical-paid"], 17)
        self.assertEqual(self.journal()["state"], "published")
        self.assertEqual((markers / "malformed.json").read_text(), "{broken")
        self.assert_retained()

    def test_live_recovery_checkout_is_skipped_without_touching_source(self):
        journal = {}
        checkout = self.backend.restore(self.record, self.manifest, journal)
        lock = checkout / ".git/index.lock"
        lock.write_text("still owned")
        journal["content_started"] = "interrupted"
        with patch.object(saved, "live_users", return_value=["123 (fixture)"]):
            with self.assertRaises(rr.Wait) as error:
                self.backend.restore(self.record, self.manifest, journal)
        self.assertEqual(error.exception.code, "continuation-cleanup")
        self.assertEqual(lock.read_text(), "still owned")
        self.assert_retained()

    def test_original_content_allowance_supports_multiple_autonomous_repairs(self):
        # One paid launch was already charged by the original round; two remain.
        self.values["fix-10-head"] = 1
        attempts = []

        def repair(checkout, *args, **kwargs):
            attempts.append(1)
            (checkout / "A.lean").write_text(f"repair version {len(attempts)}\n")
            return 0

        FakeVerification.failures = 2
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=repair),
        ):
            self.assertFalse(self.run_recovery())
            self.assertEqual(self.journal()["reason"]["code"], "source-verification-failed")
            self.assertEqual(self.journal()["attempts"], 0)
            self.retry_now()
            self.assertTrue(self.run_recovery())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.values["fix-10-head"], 3)
        self.assertEqual(self.journal()["content_attempts"], 2)
        self.assertFalse((self.record / "operator.json").exists())

    def test_exhausted_content_does_not_require_a_human_or_grant_more_budget(self):
        self.values["fix-10-head"] = 3
        FakeVerification.failures = 1
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertEqual(self.journal()["reason"]["next_action"], "continue-normal-selection")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        self.assertEqual(self.values["fix-10-head"], 3)
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_blocked_and_waiting_records_never_reserve_the_pr(self):
        for state, code in (
            ("blocked", "original-authorization-missing"),
            ("blocked", "incomplete-git-operation"),
            ("blocked", "recovery-exhausted"),
            ("blocked", "content-allowance"),
            ("eligible", "budget-refused"),
            ("eligible", "work-policy"),
        ):
            rr.write(self.record, {}, state, reason=rr.reason(rr.Block(code, "unavailable")))
            self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
            self.assertIn(str(self.record), rr.retained_context(self.cfg, "upstream/repo", 10))
        self.assertEqual(saved._complete(self.record), self.manifest)
        self.assertFalse((self.record / "operator.json").exists())

    def test_rollback_does_not_leave_a_recovery_reservation(self):
        self.assertTrue(rr.pending_for(self.cfg, "upstream/repo", 10))
        with patch.dict(os.environ, TAUCETI_AUTO_RECOVERY="off"):
            self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_old_operator_dependent_block_is_migrated_automatically(self):
        self.rewritten_checkpoint()
        for older in (self.cfg.state / "recovery").iterdir():
            if older.is_dir() and older != self.record:
                rr.write(older, {}, "obsolete")
        rr.atomic_json(
            self.record / "recovery.json",
            dict(state="blocked", attempts=0, reason=dict(code="head-rewritten"), retry_at=10**12),
        )
        self.assertTrue(rr.due(self.cfg))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["protocol"], rr.PROTOCOL)
        self.assertFalse((self.record / "operator.json").exists())

    def test_budget_reopens_and_recovery_finishes_without_operator_action(self):
        FakeVerification.failures = 1
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("no funding")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "budget-refused")
        self.assertTrue(rr.pending_for(self.cfg, "upstream/repo", 10))
        with patch.object(rr.time, "time", return_value=self.journal()["reservation_until"] + 1):
            self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        self.retry_now()
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.model_repair) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertEqual(FakeVerification.calls, 2)
        self.assertFalse((self.record / "operator.json").exists())

    def test_migration_preserves_exhausted_infrastructure_budget(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            for _ in range(3):
                self.run_recovery()
                self.retry_now()
        journal = self.journal()
        journal.pop("protocol")
        rr.atomic_json(self.record / "recovery.json", journal)
        with patch.object(self.backend, "verify", side_effect=AssertionError("Migration reset failures")) as verify:
            self.assertFalse(self.run_recovery())
        verify.assert_not_called()
        self.assertEqual(self.journal()["attempts"], 3)
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_old_one_continuation_block_uses_remaining_original_budget(self):
        self.values["fix-10-head"] = 1
        FakeVerification.failures = 2
        calls = []

        def repair(checkout, *args, **kwargs):
            calls.append(1)
            (checkout / "A.lean").write_text(f"repair {len(calls)}\n")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=repair),
        ):
            self.assertFalse(self.run_recovery())
            journal = self.journal()
            journal.pop("protocol")
            journal.update(state="blocked", reason=dict(code="content-allowance"), retry_at=10**12)
            rr.atomic_json(self.record / "recovery.json", journal)
            self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.values["fix-10-head"], 3)
        self.assertEqual(self.journal()["state"], "published")
        self.assertFalse((self.record / "operator.json").exists())

    def test_blocked_record_allows_fresh_dispatch_under_normal_quota_checks(self):
        from tauceti_worker import work_units

        class QuotaReached(Exception):
            pass

        rr.write(
            self.record, {}, "blocked", reason=rr.reason(rr.Block("incomplete-git-operation", "unsupported metadata"))
        )
        candidate = SimpleNamespace(pr=10)
        options = SimpleNamespace(dry_run=False, sandbox_host=True)
        with patch.object(work_units, "_check_stage_read_quota", side_effect=QuotaReached()) as quota:
            with self.assertRaises(QuotaReached):
                work_units.dispatch("fix", self.w, None, candidate, options)
        quota.assert_called_once_with("fix")
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_continuation_cannot_import_removed_lineage(self):
        old_head = self.rewritten_checkpoint(conflicting=True)
        writer_head = self.current["head"]

        def import_history(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("resolved delta\n")
            git(checkout, "add", "A.lean")
            identity = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.com")
            git(checkout, *identity, "commit", "-qm", "resolve")
            git(checkout, *identity, "merge", "-s", "ours", "--no-edit", old_head)
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=import_history),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "removed-history-reintroduced")
        self.assertEqual(self.journal()["reason"]["next_action"], "retry-with-original-content-allowance")
        self.assertTrue(self.journal()["rebuild_delta"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), writer_head)

    def test_unreadable_recovery_metadata_does_not_disable_normal_work(self):
        original = Path.iterdir

        def unavailable(path):
            if path == self.cfg.state / "recovery":
                raise PermissionError("fixture metadata unavailable")
            return original(path)

        with patch.object(Path, "iterdir", unavailable):
            self.assertFalse(rr.due(self.cfg))
            self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
            self.assertEqual(rr.retained_context(self.cfg, "upstream/repo", 10), "")
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(saved._complete(self.record), self.manifest)

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

    def test_confirmed_publication_removed_by_writer_is_not_replayed(self):
        actual = self.backend.publish

        def push_then_die(*args):
            actual(*args)
            raise Death()

        with patch.object(self.backend, "publish", side_effect=push_then_die):
            with self.assertRaises(Death):
                self.run_recovery()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.current["head"] = self.base
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-superseded")
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_uncertain_push_with_new_writer_is_archived_without_replay(self):
        actual = rr.write

        def die_after_intent(record, journal, state, **fields):
            actual(record, journal, state, **fields)
            if (fields.get("publication") or {}).get("push_started"):
                raise Death()

        with patch.object(rr, "write", side_effect=die_after_intent):
            with self.assertRaises(Death):
                self.run_recovery()
        self.advance_writer()
        writer = self.current["head"]
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-head-moved")
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        self.assertIn("publication-head-moved", rr.retained_context(self.cfg, "upstream/repo", 10))
        self.assertEqual(git(self.remote, "rev-parse", "main"), writer)
        self.assertEqual(FakeVerification.calls, 1)

    def test_three_failures_release_pr_and_changed_head_rearms(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("disk full")):
            for n in range(1, 4):
                self.assertFalse(self.run_recovery())
                self.assertEqual(self.journal()["attempts"], n)
                self.retry_now()
            self.assertEqual(self.journal()["state"], "blocked")
            self.assertFalse(self.run_recovery())
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        self.advance_writer()
        self.retry_now()  # simulate the bounded external-fact probe becoming due
        self.assertTrue(self.run_recovery())
        self.assert_retained()

    def test_claim_wait_does_not_consume_attempt(self):
        with patch.object(self.backend, "acquire", side_effect=rr.Wait("claim", "busy")):
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
        FakeVerification.failures = 1
        recovery = rr.Recovery(self.record, self.backend)
        recovery.manifest["conversation"] = dict(actor="other", identity="old-account", profile={"model": "other"})
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.model_repair) as model,
        ):
            self.assertTrue(recovery.run())
        self.assertNotIn("resume", model.call_args.kwargs)
        self.assertEqual(self.journal()["conversation_mode"], "narrow-new")

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
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        self.assertFalse(rr.due(self.cfg))

    def test_one_invocation_even_when_cleanup_wait_refunds_attempt(self):
        other = self.record.parent / "second"
        other.mkdir()
        for path in self.record.iterdir():
            if path.is_file():
                (other / path.name).write_bytes(path.read_bytes())
        with patch.object(rr.WorkerBackend, "restore", side_effect=rr.Wait("continuation-cleanup", "busy")) as restore:
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(restore.call_count, 1)

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

    def test_terminal_recovery_defers_cache_eviction(self):
        actual = self.backend.verify

        def cache(checkout, *args):
            (checkout / ".lake").mkdir(exist_ok=True)
            (checkout / ".lake/large-build").write_bytes(b"cache")
            return actual(checkout, *args)

        with patch.object(self.backend, "verify", cache):
            self.assertTrue(self.run_recovery())
        self.assertTrue((self.record / "checkout/.lake").exists())
        self.assertTrue(list((self.cfg.state / "recovery-cleanup" / "cache-roots").glob("*.json")))
        self.assert_retained()

    def test_consumer_status_is_distinct_from_moved_aside_checkout(self):
        rr.write(self.record, {}, "retained")
        saved.housekeep(self.cfg.state.parent, seconds=5, entries=10000)
        summary = saved.summary(self.cfg.state.parent, workers={"w"})
        self.assertEqual(summary["recovery_retained"], 1)
        self.assertEqual(summary["retained"], 0)
        rr.write(
            self.record, {}, "blocked", reason=rr.reason(rr.Block("repair", "precise failure", "restore dependency"))
        )
        saved.housekeep(self.cfg.state.parent, seconds=5, entries=10000)
        summary = saved.summary(self.cfg.state.parent, workers={"w"})
        self.assertIn("next action: continue-normal-selection", summary["reasons"][0])

    def test_valid_original_conversation_uses_fresh_checkout_without_rebinding(self):
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
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.model_repair) as model,
        ):
            self.assertTrue(recovery.run())
        self.assertNotIn("resume", model.call_args.kwargs)
        self.assertNotIn("original_cwd", model.call_args.kwargs)
        self.assertEqual(model.call_args.args[0], self.record / "checkout")
        self.assertEqual(self.journal()["conversation_mode"], "narrow-new")
        self.assertEqual((self.co / "A.lean").read_text(), "retained repair\n")

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

    def test_legacy_containment_wait_migrates_without_namespaces_or_counter_reset(self):
        for name, payload in (("uncertain.json", "malformed"), ("proof.json", '{"strong":false}')):
            path = self.cfg.state / "containment" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload)
        self.values["fix-10-head"] = 2
        rr.atomic_json(
            self.record / "recovery.json",
            dict(
                protocol=2,
                state="eligible",
                attempts=1,
                content_attempts=2,
                reason=dict(code="containment-unavailable"),
                retry_at=10**12,
            ),
        )
        self.assertTrue(rr.due(self.cfg, only=["fix"]))
        with patch.dict(sys.modules, {"tauceti_worker.containment": None}):
            self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.values["fix-10-head"], 2)
        self.assertEqual(self.journal()["content_attempts"], 2)
        self.assertEqual(self.journal()["attempts"], 1)
        self.assert_retained()

    def test_terminal_legacy_selection_changes_never_schedule_recovery(self):
        for state in ("published", "obsolete"):
            rr.atomic_json(
                self.record / "recovery.json",
                dict(state=state, selection=dict(only=[], prs=[], sandbox_host=True, agent="auto")),
            )
            self.assertFalse(rr.due(self.cfg, only=["fix"], prs=(10,), agent="claude"))

    def test_selection_change_failure_persists_new_policy_and_backoff(self):
        rr.write(self.record, {}, "eligible", selection=rr.selection(["review"], (), True, "claude"))
        self.opts.only = ["fix"]
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["selection"]["only"], ["fix"])
        self.assertFalse(rr.due(self.cfg, only=["fix"], agent="claude"))
        with patch.object(self.backend, "restore", side_effect=AssertionError("bypassed backoff")):
            self.assertFalse(self.run_recovery())

    def test_known_unsent_intent_is_reintegrated_after_head_move(self):
        with patch.object(self.backend, "publish", side_effect=rr.Wait("claim-or-head-changed", "pre-push")):
            self.assertFalse(self.run_recovery())
        self.assertNotIn("publication", self.journal())
        self.assertEqual(self.journal()["publication_history"][-1]["disposition"], "not-sent")
        self.advance_writer()
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:Other.lean"), "other writer")
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")

    def test_crash_before_push_started_does_not_archive_known_unsent_delta(self):
        actual = rr.write

        def die(record, journal, state, **fields):
            actual(record, journal, state, **fields)
            if state == "publication-pending":
                raise Death()

        with patch.object(rr, "write", side_effect=die), self.assertRaises(Death):
            self.run_recovery()
        self.advance_writer()
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["state"], "published")

    def move_writer_again(self, *, rewrite=False):
        other = self.root / "other"
        if rewrite:
            git(other, "reset", "--hard", self.base)
        (other / "Next.lean").write_text("next writer\n")
        git(other, "add", "-A")
        git(other, "-c", "user.name=other", "-c", "user.email=other@example.com", "commit", "-qm", "next")
        git(other, "push", "-q", "--force", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")

    def test_unresolved_rewritten_delta_retargets_after_forward_head_move(self):
        old = self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.move_writer_again()
        target = self.current["head"]
        self.retry_now()

        def resolve(checkout, *args, **kwargs):
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), target)
            (checkout / "A.lean").write_text("both writers\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertNotIn(old, git(self.remote, "rev-list", "main").split())
        self.assertEqual(git(self.remote, "show", "main:Next.lean"), "next writer")
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_second_rewrite_reuses_frozen_delta_without_paid_work(self):
        self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        patch_bytes = Path(self.journal()["delta_integration"]["patch"]).read_bytes()
        self.move_writer_again(rewrite=True)
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(Path(self.journal()["delta_integration"]["patch"]).read_bytes(), patch_bytes)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")
        self.assertNotIn("Dropped.lean", git(self.remote, "ls-tree", "--name-only", "main"))

    def test_pending_owned_merge_retargets_after_head_move(self):
        self.advance_writer(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.move_writer_again()
        self.retry_now()

        def resolve(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("both writers\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve),
        ):
            self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:Next.lean"), "next writer")

    def test_missing_derived_checkout_restores_immutable_source(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            self.assertFalse(self.run_recovery())
        (self.record / "checkout").rename(self.record / "lost-checkout")
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assert_retained()

    def handoff(self):
        from tauceti_worker.activity import current_round_id

        if (self.journal().get("handoff") or {}).get("state") == "offered":
            saved.guard(self.cfg, self.co, repository="upstream/repo")
        round_id = current_round_id() or "receiving-round"
        with patch("tauceti_worker.activity.current_round_id", return_value=round_id):
            saved.note_owner(
                self.cfg,
                **{
                    **self.owner,
                    "round_id": round_id,
                    "publication_marker": dict(ref="refs/tauceti/published/push/main", before=None, scope="unscoped"),
                },
            )
            return rr.handoff_context(
                self.cfg,
                "upstream/repo",
                10,
                head=self.current["head"],
                actor="alice",
                checkout=self.co,
                push_marker="refs/tauceti/published/push/main",
                published_before=None,
            )

    def test_normal_handoff_retires_recovery_before_exposing_immutable_source(self):
        text, handoffs = self.handoff()
        self.assertIn(str(self.record / "manifest.json"), text)
        self.assertNotIn(str(self.record / "checkout"), text)
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertTrue(rr.due(self.cfg, only=["fix"]))
        self.assertFalse(self.run_recovery())
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal publication")
        self.assertEqual(self.normal_push().returncode, 0)
        published = git(self.co, "rev-parse", "HEAD")
        rr.complete_handoffs(self.cfg, handoffs, published)
        self.assertEqual(rr.retained_context(self.cfg, "upstream/repo", 10), "")
        self.assertEqual(self.journal()["handoff"]["published_head"], published)
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_unlaunched_handoff_remains_available_to_normal_work(self):
        self.handoff()
        context, handoffs = self.handoff()
        self.assertIn(str(self.record), context)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(self.journal()["handoff"]["state"], "offered")
        self.assertFalse(self.run_recovery())

    def test_normal_push_survives_death_before_handoff_acknowledgement(self):
        self.handoff()
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal publication")
        self.assertEqual(self.normal_push().returncode, 0)
        context, handoffs = self.handoff()
        self.assertEqual((context, handoffs), ("", []))
        self.assertEqual(self.journal()["reason"]["code"], "source-handoff-published")
        self.assertFalse(self.run_recovery())

    def test_handoff_preserves_latest_partial_source(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            self.assertFalse(self.run_recovery())
        (self.record / "checkout/A.lean").write_text("latest partial\n")
        context, _ = self.handoff()
        snapshot = Path(self.journal()["source_checkpoint"])
        self.assertIn(str(snapshot / "manifest.json"), context)
        clone = self.root / "reconstruction"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(clone)], check=True)
        saved.restore(snapshot, clone)
        self.assertEqual((clone / "A.lean").read_text(), "latest partial\n")
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_content_limit_schedules_mechanical_recheck_without_more_paid_work(self):
        self.values["fix-10-head"] = 3
        FakeVerification.failures = 1
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "content-allowance")
        self.assertFalse(rr.due(self.cfg, only=["fix"]))
        with patch.object(rr.time, "time", return_value=self.journal()["retry_at"] + 1):
            self.assertTrue(rr.due(self.cfg, only=["fix"]))
            self.test_loop_recovery_precedes_model_budget()
            self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(FakeVerification.calls, 2)
        self.assertEqual(self.values["fix-10-head"], 3)

    def test_infrastructure_backoff_eventually_rechecks_automatically(self):
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            for _ in range(3):
                self.assertFalse(self.run_recovery())
                self.retry_now()
        due = self.journal()["mechanical_recheck_at"]
        with patch.object(rr.time, "time", return_value=due + 1):
            self.assertTrue(rr.due(self.cfg, only=["fix"]))
            self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["infrastructure_failures"], 3)
        self.assertEqual(self.journal()["state"], "published")

    def test_handoff_excludes_foreign_and_automatic_bump_source(self):
        for restriction in ("foreign", "automatic_bump"):
            self.owner["authorization"][restriction] = True
            (self.co / "A.lean").write_text(f"{restriction} source\n")
            self.save_updated_checkpoint()
            self.assertNotIn(self.record, [p for p, _, _ in rr.evidence_records(self.cfg, "upstream/repo", 10)])
            self.owner["authorization"].pop(restriction)

    def test_reintegration_patch_ignores_external_diff_configuration(self):
        self.rewritten_checkpoint()
        actual = self.backend.reintegrate_delta

        def configure(checkout, *args, **kwargs):
            git(checkout, "config", "diff.noprefix", "true")
            git(checkout, "config", "color.diff", "always")
            git(checkout, "config", "diff.external", "false")
            return actual(checkout, *args, **kwargs)

        with patch.object(self.backend, "reintegrate_delta", side_effect=configure):
            self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")

    def test_rejected_removed_lineage_is_automatically_replanned(self):
        old = self.rewritten_checkpoint(conflicting=True)

        def import_history(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("resolved delta\n")
            git(checkout, "add", "A.lean")
            identity = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.com")
            git(checkout, *identity, "commit", "-qm", "resolve")
            git(checkout, *identity, "merge", "-s", "ours", "--no-edit", old)
            return 0

        def repair(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("safe retained repair\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=import_history),
        ):
            self.assertFalse(self.run_recovery())
        self.retry_now()
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=repair),
        ):
            self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 2)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "safe retained repair")
        self.assertNotIn(old, git(self.remote, "rev-list", "main").split())

    def test_blocked_metadata_recheck_never_falls_back_to_direct_github(self):
        rr.write(self.record, {}, "blocked", reason=rr.reason(rr.Block("original-authorization-missing", "legacy")))
        with (
            patch("tauceti_worker.github_state.read", return_value=(False, None)),
            patch.object(self.w.gh, "pr_view", side_effect=AssertionError("New GitHub polling")),
        ):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertIn("no direct GitHub probe", self.journal()["probe_error"])
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_later_rewrite_cannot_reintroduce_an_earlier_target_lineage(self):
        self.rewritten_checkpoint(conflicting=True)
        earlier_target = self.current["head"]
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.move_writer_again(rewrite=True)
        other = self.root / "other"
        (other / "A.lean").write_text("second rewritten writer\n")
        git(other, "add", "A.lean")
        identity = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.com")
        git(other, *identity, "commit", "-qm", "second writer")
        git(other, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")
        target = self.current["head"]
        self.retry_now()

        def import_old_target(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("resolved delta\n")
            git(checkout, "add", "A.lean")
            git(checkout, *identity, "commit", "-qm", "resolve")
            git(checkout, *identity, "merge", "-s", "ours", "--no-edit", earlier_target)
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=import_old_target),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "removed-history-reintroduced")
        self.assertEqual(git(self.remote, "rev-parse", "main"), target)

    def test_missing_rewritten_checkout_restores_retained_history(self):
        self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.move_writer_again(rewrite=True)
        self.retry_now()
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            self.assertFalse(self.run_recovery())
        (self.record / "checkout").rename(self.record / "lost-checkout")
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_push_helper_exec_failure_is_known_unsent(self):
        actual = subprocess.run

        def unavailable(argv, *args, **kwargs):
            if argv[0].endswith("git-safe-push"):
                raise FileNotFoundError("helper unavailable")
            return actual(argv, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=unavailable):
            self.assertFalse(self.run_recovery())
        self.assertNotIn("publication", self.journal())
        self.assertEqual(self.journal()["publication_history"][-1]["disposition"], "not-sent")
        self.advance_writer()
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")

    def test_real_remote_lease_rejection_retains_unsent_repair(self):
        actual = subprocess.run
        raced = False

        def race(argv, *args, **kwargs):
            nonlocal raced
            if argv[0].endswith("git-safe-push") and not raced:
                raced = True
                self.advance_writer()
            return actual(argv, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=race):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "push-rejected")
        self.assertNotIn("publication", self.journal())
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")
        self.assertEqual(git(self.remote, "show", "main:Other.lean"), "other writer")

    def test_rewrite_during_owned_merge_uses_original_delta(self):
        old = self.rewritten_checkpoint(conflicting=True)
        rr.write(
            self.record,
            {},
            "eligible",
            content_started="earlier-content-attempt",
            source_checkpoint_for="earlier-content-attempt",
        )
        other = self.root / "other"
        git(other, "fetch", str(self.co), old)
        git(other, "reset", "--hard", old)
        (other / "A.lean").write_text("conflicting forward writer\n")
        (other / "RemovedWriter.lean").write_text("drop this on rewrite\n")
        git(other, "add", "-A")
        git(other, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "forward")
        git(other, "push", "-q", "--force", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.assertTrue(self.journal()["pending_merge_head"])
        self.move_writer_again(rewrite=True)
        self.retry_now()
        self.assertTrue(self.run_recovery())
        paths = git(self.remote, "ls-tree", "--name-only", "main")
        self.assertNotIn("Dropped.lean", paths)
        self.assertNotIn("RemovedWriter.lean", paths)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")

    def test_zero_exit_with_conflicts_can_retarget_and_resolve(self):
        self.rewritten_checkpoint(conflicting=True)
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", return_value=0),
        ):
            self.assertFalse(self.run_recovery())
        self.assertTrue(self.journal()["delta_integration"]["requires_reasoning"])
        self.assertEqual(self.journal()["reason"]["code"], "recovery-source-unresolved")
        self.move_writer_again()
        self.retry_now()

        def resolve(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("both writers\n")
            git(checkout, "add", "A.lean")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=resolve),
        ):
            self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["content_attempts"], 2)

    def test_prepared_delta_crash_then_head_move_keeps_frozen_source(self):
        self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        self.move_writer_again(rewrite=True)
        self.retry_now()
        actual = rr.write

        def die(record, journal, state, **fields):
            actual(record, journal, state, **fields)
            if (fields.get("delta_integration") or {}).get("stage") == "prepared":
                raise Death()

        with patch.object(rr, "write", side_effect=die), self.assertRaises(Death):
            self.run_recovery()
        other = self.root / "other"
        (other / "Last.lean").write_text("last writer\n")
        git(other, "add", "Last.lean")
        git(other, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "last")
        git(other, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained delta")

    def test_unused_normal_handoff_returns_to_zero_paid_mechanical_recovery(self):
        _, handoffs = self.handoff()
        self.assertTrue(rr.due(self.cfg))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["handoff"]["state"], "returned")
        self.assertEqual(self.journal()["state"], "published")
        self.assertEqual(self.values.get("fix-10-head", 0), 0)
        # A stale acknowledgement cannot consume a newer handoff.
        rr.complete_handoffs(self.cfg, [(handoffs[0][0], "stale")], self.base)
        self.assertEqual(self.journal()["state"], "published")

    def test_known_refused_normal_launch_restores_recovery_even_after_head_moves(self):
        _, handoffs = self.handoff()
        self.advance_writer()
        rr.return_handoffs(handoffs)
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "published")

    def test_normal_partial_checkpoint_takes_recovery_ownership(self):
        with patch("tauceti_worker.activity.current_round_id", return_value="receiving-round"):
            self.handoff()
        (self.co / "A.lean").write_text("normal partial source\n")
        owner = {**self.owner, "round_id": "receiving-round"}
        latest = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertFalse(rr.recover(self.w, self.opts))  # Reconcile the older lease on the next bounded phase.
        self.assertEqual(self.journal()["reason"]["code"], "source-handoff-checkpoint")
        self.assertIn(str(self.record), rr.retained_context(self.cfg, "upstream/repo", 10))
        self.assertEqual(read_json(latest / "recovery.json")["state"], "published")
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "normal partial source")
        context, handoffs = self.handoff()
        self.assertIn(str(self.record), context)
        self.assertEqual(handoffs, [])
        rr.return_handoffs(handoffs)
        self.assertEqual(self.journal()["state"], "obsolete")

    def test_handoff_finds_nonsequential_receiving_checkpoint_by_durable_source_receipt(self):
        with patch("tauceti_worker.activity.current_round_id", return_value="receiving-round"):
            self.handoff()
        owner = {**read_json(saved.owner_path(self.cfg)), **self.owner, "round_id": "receiving-round"}
        (self.co / "A.lean").write_text("first partial source\n")
        first = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        (self.co / "A.lean").write_text("latest partial source\n")
        latest = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        self.assertNotEqual(latest, first)
        self.assertNotIn(latest.name, [f"receiving-round.{n}" for n in range(2, 9)])
        rr.reconcile_handoff(self.cfg, self.record, self.journal())
        self.assertEqual(self.journal()["handoff"]["checkpoint"], str(latest))
        self.assertEqual(self.journal()["handoff"]["state"], "transferred")

    def test_reintegration_cache_cleanup_uses_bounded_housekeeping(self):
        self.rewritten_checkpoint()
        checkout = self.backend.restore(self.record, self.manifest, {})
        cache = checkout / ".lake/build/cache"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"rebuildable")
        with patch(
            "tauceti_worker.recovery_maintenance._evict_caches", side_effect=AssertionError("foreground cache walk")
        ):
            self.assertTrue(self.run_recovery())
        archived = Path(self.journal()["delta_integration"]["archived"])
        self.assertEqual(archived.parent, self.record)
        self.assertTrue((archived / ".lake").exists())
        with (
            patch.object(saved, "owner_stopped", return_value=True),
            patch.object(saved, "live_users", return_value=[]),
        ):
            saved.housekeep(self.cfg.state.parent, seconds=5, entries=10000)
        self.assertFalse((archived / ".lake").exists())
        self.assertTrue((archived / "A.lean").is_file())
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_reintegration_wait_keeps_working_dependency_cache(self):
        self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding closed")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["state"], "eligible")
        cache = self.record / "checkout/.lake/packages/dependency"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"warm dependency")
        with (
            patch.object(saved, "owner_stopped", return_value=True),
            patch.object(saved, "live_users", return_value=[]),
        ):
            saved.housekeep(self.cfg.state.parent, seconds=5, entries=10000)
        self.assertEqual(cache.read_bytes(), b"warm dependency")

    def test_publication_sidecar_survives_a_concurrent_consumer_journal_write(self):
        original = saved.atomic_json
        raced = []

        def concurrent(path, value):
            if path.name == "normal-publication.json":
                rr.write(path.parent, read_json(path.parent / "recovery.json"), "verified", content_attempts=2)
                raced.append(path.parent)
            return original(path, value)

        with patch.object(saved, "atomic_json", side_effect=concurrent):
            latest, _ = self.normal_lost_push()
        self.assertEqual(raced, [latest])
        self.assertEqual(read_json(latest / "recovery.json")["state"], "verified")
        self.assertEqual(read_json(latest / "recovery.json")["content_attempts"], 2)
        self.assertNotIn("normal_publication", read_json(latest / "recovery.json"))
        self.assertTrue(rr.Recovery(latest, self.backend).run())
        self.assertEqual(read_json(latest / "recovery.json")["content_attempts"], 2)

    def test_capture_crash_before_disposable_index_is_repaired_without_replaying_older_source(self):
        self.handoff()
        (self.co / "A.lean").write_text("receiving round partial progress\n")
        owner = read_json(saved.owner_path(self.cfg))
        with patch.object(saved, "_handoff_source_receipts", side_effect=Death()), self.assertRaises(Death):
            saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "receiving round partial progress")
        self.assertEqual(self.values.get("fix-10-head", 0), 0)
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_unconfirmed_capture_does_not_block_independent_normal_work(self):
        self.handoff()
        context, handoffs = rr.handoff_context(
            self.cfg,
            "upstream/repo",
            10,
            head=self.current["head"],
            actor="alice",
            checkout=self.co,
            push_marker="refs/tauceti/published/push/main",
            published_before=None,
            push_scope="next-round",
        )
        self.assertIn(str(self.record), context)
        self.assertEqual(handoffs, [])
        self.assertEqual(self.journal()["handoff"]["state"], "offered")
        self.assertEqual(self.journal()["reason"]["code"], "source-handoff-source-pending")
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))

    def test_unchanged_source_capture_wait_backs_off_without_consuming_allowances(self):
        self.handoff()
        now = rr.time.time()
        for _ in range(7):
            with patch.object(rr.time, "time", return_value=now):
                rr.reconcile_handoff(self.cfg, self.record, self.journal())
            delay = self.journal()["retry_at"] - now
            now = self.journal()["retry_at"] + 1
        self.assertEqual(delay, 21600)
        self.assertEqual(self.journal().get("attempts", 0), 0)
        self.assertEqual(self.journal().get("content_attempts", 0), 0)
        self.assertFalse(rr.pending_for(self.cfg, "upstream/repo", 10))
        saved.guard(self.cfg, self.co, repository="upstream/repo")
        self.assertTrue(rr.due(self.cfg))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_noop_normal_push_does_not_consume_retained_repair(self):
        self.handoff()
        self.assertEqual(self.normal_push().returncode, 0)  # HEAD is still the starting remote commit.
        self.assertIsNone(saved._rev(self.co, self.journal()["handoff"]["push_marker"]))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_uncertain_noop_normal_push_does_not_strand_valid_source(self):
        self.handoff()
        env = {
            **os.environ,
            "TAUCETI_PUSH_REF": "main",
            "TAUCETI_PUSH_EXPECT": self.base,
            "TAUCETI_PUSH_REMOTE": str(self.root / "missing-remote.git"),
        }
        result = subprocess.run(
            [str(Path(__file__).resolve().parent.parent / "scripts/git-safe-push")],
            cwd=self.co,
            env=env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_clean_guard_and_missing_index_cannot_erase_a_captured_checkpoint(self):
        self.handoff()
        saved._record(
            self.cfg,
            self.co,
            saved.inspect(self.co),
            "progress-roadmap",
            "upstream/roadmap",
            owner=dict(round_id="receiving-round", work="progress"),
        )
        (self.co / "A.lean").write_text("receiving round committed progress\n")
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "partial repair")
        latest = saved._record(
            self.cfg,
            self.co,
            saved.inspect(self.co),
            "TauCeti",
            "upstream/repo",
            owner=read_json(saved.owner_path(self.cfg)),
        )
        self.assertNotEqual(latest.name, "receiving-round")
        git(self.co, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(saved.guard(self.cfg, self.co, repository="upstream/repo").state, "clean")
        self.assertEqual(self.journal()["handoff"]["source_receipt"]["checkpoint"], str(latest))
        for entry in (self.cfg.state / "recovery-index").glob("*.json"):
            entry.unlink()  # Fault injection: this cache is explicitly disposable.
        rr.reconcile_handoff(self.cfg, self.record, self.journal())
        self.assertEqual(self.journal()["handoff"]["checkpoint"], str(latest))
        self.assertEqual(self.journal()["handoff"]["state"], "transferred")

    def test_gc_cannot_prune_reintegration_lineage_inputs(self):
        self.rewritten_checkpoint(conflicting=True)
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding")):
            self.assertFalse(self.run_recovery())
        tx = self.journal()["delta_integration"]
        for path in (self.record / "checkout", Path(tx["archived"])):
            git(path, "reflog", "expire", "--expire=now", "--all")
            git(path, "gc", "--prune=now")
            for name in ("expected", "source", "target"):
                self.assertEqual(git(path, "rev-parse", f"refs/tauceti/recovery/{tx['id']}/{name}"), tx[name])
        self.move_writer_again(rewrite=True)
        self.retry_now()
        self.assertTrue(self.run_recovery())

    def test_claim_refusal_before_push_is_known_unsent(self):
        claim = self.root / "claim-refused.sh"
        claim.write_text("#!/bin/sh\nexit 1\n")
        claim.chmod(0o700)
        with patch.dict(os.environ, TAUCETI_CLAIM_KEY="branch/10", TAUCETI_CLAIM_SH=str(claim)):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "push-rejected")
        self.assertNotIn("publication", self.journal())
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        self.retry_now()
        self.assertTrue(self.run_recovery())

    def test_cached_failure_is_mechanically_rechecked_without_funding(self):
        FakeVerification.failures = 1
        with patch("tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("funding closed")):
            self.assertFalse(self.run_recovery())
            with patch.object(rr.time, "time", return_value=self.journal()["failed_recheck_at"] + 1):
                self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(FakeVerification.calls, 2)
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_repeated_unchanged_infrastructure_failure_backs_off_to_hours(self):
        now = rr.time.time()
        with patch.object(self.backend, "verify", side_effect=RuntimeError("offline")):
            for _ in range(9):
                with patch.object(rr.time, "time", return_value=now):
                    self.assertFalse(self.run_recovery())
                delay = self.journal()["retry_at"] - now
                now = self.journal()["retry_at"] + 1
        self.assertGreaterEqual(delay, 7200)
        self.assertEqual(self.journal()["infrastructure_streak"], 9)
        self.assertEqual(self.journal()["infrastructure_failures"], 9)
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_forensic_handoff_cannot_return_to_automatic_publication(self):
        rr.write(self.record, {}, "obsolete", reason=rr.reason(rr.Obsolete("publication-head-moved", "uncertain")))
        _, handoffs = self.handoff()
        rr.return_handoffs(handoffs)
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertEqual(handoffs, [])
        self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_fresh_continuation_runner_needs_no_pid_namespaces(self):
        import contextlib

        FakeVerification.failures = 1
        script = "from pathlib import Path; Path('A.lean').write_text('fresh fixture repair\\n')"
        with (
            patch.dict(sys.modules, {"tauceti_worker.containment": None}),
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch.object(rr, "conversation_identity", return_value="fixture-account"),
            patch(
                "tauceti_worker.agents.host_agent_argv",
                side_effect=lambda *a: ([sys.executable, "-c", script], os.environ.copy()),
            ),
            patch("tauceti_worker.agents.host_build_cache_context", side_effect=lambda *a: contextlib.nullcontext([])),
            patch("tauceti_worker.fleet_capacity.check_launch_admission") as admission,
            patch("tauceti_worker.agents.run_agent_host", side_effect=run_real_agent_host),
        ):
            self.assertTrue(self.run_recovery())
        admission.assert_called_once()
        self.assertEqual(self.journal()["conversation_mode"], "narrow-new")
        self.assertEqual(self.values["fix-10-head"], 1)
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "fresh fixture repair")
        self.assertEqual((self.co / "A.lean").read_text(), "retained repair\n")

    def lost_push_response(self):
        hook = self.remote / "hooks/reference-transaction"
        hook.write_text("""#!/bin/sh
if [ "$1" = committed ] && [ ! -f ./lost-once ]; then
    : > ./lost-once
    kill -KILL "$PPID"
fi
""")
        hook.chmod(0o700)
        self.assertFalse(self.run_recovery())
        intent = self.journal()["publication"]
        self.assertTrue(intent["push_started"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), intent["oid"])
        return intent

    def test_remote_failure_after_ref_commit_reconciles_without_duplicate(self):
        intent = self.lost_push_response()
        self.retry_now()
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.journal()["evidence"]["commit"], intent["oid"])
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")

    def normal_lost_push(self):
        with patch("tauceti_worker.activity.current_round_id", return_value="receiving-round"):
            self.handoff()
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal retained repair")
        owner = {
            **self.owner,
            "round_id": "receiving-round",
            "publication_marker": dict(ref="refs/tauceti/published/push/main", before=None),
        }
        prior = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        hook = self.remote / "hooks/reference-transaction"
        hook.write_text('#!/bin/sh\nif [ "$1" = committed ]; then kill -KILL "$PPID"; fi\n')
        hook.chmod(0o700)
        result = self.normal_push()
        self.assertEqual(result.returncode, 1)
        hook.unlink()
        pending = git(self.co, "rev-parse", "refs/tauceti/pending-push/unscoped/main")
        latest = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        self.assertEqual(latest, prior)  # Same immutable source; uncertainty updates its consumer journal.
        self.assertEqual(read_json(latest / "normal-publication.json")["oid"], git(self.co, "rev-parse", "HEAD"))
        return latest, pending

    def normal_push(self):
        env = {
            **os.environ,
            "TAUCETI_PUSH_REF": "main",
            "TAUCETI_PUSH_EXPECT": self.base,
            "TAUCETI_PUSH_REMOTE": str(self.remote),
        }
        return subprocess.run(
            [str(Path(__file__).resolve().parent.parent / "scripts/git-safe-push")],
            cwd=self.co,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_normal_uncertain_push_survives_source_checkpoint_and_process_death(self):
        latest, _ = self.normal_lost_push()
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(read_json(latest / "recovery.json")["state"], "published")
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "2")
        self.assertEqual(FakeVerification.calls, 0)

    def test_normal_uncertain_push_then_reset_is_not_replayed_via_partial_checkpoint(self):
        latest, _ = self.normal_lost_push()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.assertFalse(rr.recover(self.w, self.opts))
        j = read_json(latest / "recovery.json")
        self.assertEqual(j["state"], "obsolete")
        self.assertEqual(j["reason"]["code"], "publication-uncertain")
        self.assertTrue(j["forensic_only"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        self.assertIn(str(latest), rr.retained_context(self.cfg, "upstream/repo", 10))

    def test_later_normal_rejection_preserves_earlier_uncertain_attempt(self):
        latest, pending = self.normal_lost_push()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.advance_writer()
        current = self.current["head"]
        self.assertEqual(self.normal_push().returncode, 78)
        self.assertEqual(git(self.co, "rev-parse", "refs/tauceti/pending-push/unscoped/main"), pending)
        self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(read_json(latest / "recovery.json")["state"], "obsolete")
        self.assertEqual(git(self.remote, "rev-parse", "main"), current)

    def test_definitive_normal_rejection_returns_handoff_without_pending_intent(self):
        self.handoff()
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal retained repair")
        self.advance_writer()
        self.assertEqual(self.normal_push().returncode, 78)
        self.assertIsNone(saved.ref_object(self.co, "refs/tauceti/pending-push/unscoped/main"))
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["handoff"]["state"], "transferred")

    def test_lost_response_then_removed_commit_never_becomes_known_unsent(self):
        self.lost_push_response()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.advance_writer()
        latest = self.current["head"]
        self.retry_now()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-head-moved")
        self.assertEqual(git(self.remote, "rev-parse", "main"), latest)
        self.assertTrue(self.journal()["publication"]["push_started"])
        self.assertEqual(saved._complete(self.record), self.manifest)

    def test_server_policy_rejection_consumes_bounded_infrastructure_retries(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        for count in range(1, 4):
            self.assertFalse(self.run_recovery())
            self.assertEqual(self.journal()["attempts"], count)
            self.assertNotIn("publication", self.journal())
            self.retry_now()
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertIn("server policy rejected", self.journal()["reason"]["detail"])
        self.assertEqual(self.values.get("fix-10-head", 0), 0)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_unused_normal_handoff_reintegrates_after_another_writer_moves_head(self):
        self.handoff()
        self.advance_writer()
        self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["handoff"]["state"], "returned")
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "retained repair")
        self.assertEqual(git(self.remote, "show", "main:Other.lean"), "other writer")

    def test_handoff_exception_returns_every_already_offered_record(self):
        with patch.object(rr, "immutable_evidence", side_effect=RuntimeError("fixture failure")):
            with self.assertRaises(RuntimeError):
                self.handoff()
        self.assertEqual(self.journal()["handoff"]["state"], "returned")
        self.assertTrue(rr.recover(self.w, self.opts))

    def test_changed_normal_attempt_without_acknowledgement_stays_uncertain(self):
        self.handoff()
        # The safe-push intent is a unique commit whose parent is the attempted OID.
        attempt = git(
            self.co,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit-tree",
            self.base + "^{tree}",
            "-p",
            self.base,
            "-m",
            "attempt",
        )
        git(self.co, "update-ref", self.journal()["handoff"]["attempt_marker"], attempt)
        self.advance_writer()
        self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["reason"]["code"], "publication-uncertain")
        self.assertTrue(self.journal()["forensic_only"])

    def test_repeated_unchanged_content_failure_backs_off_without_paid_replay(self):
        self.values["fix-10-head"] = 3
        FakeVerification.failures = 6
        now = rr.time.time()
        for _ in range(6):
            with patch.object(rr.time, "time", return_value=now):
                self.assertFalse(self.run_recovery())
            delay = self.journal()["retry_at"] - now
            now = self.journal()["retry_at"] + 1
        self.assertEqual(FakeVerification.calls, 6)
        self.assertEqual(self.journal()["verification_failures"], 6)
        self.assertEqual(delay, 21600)
        self.assertEqual(self.values["fix-10-head"], 3)

    def test_non_owned_operation_on_rewritten_source_gets_narrow_reasoning(self):
        old = self.rewritten_checkpoint(conflicting=True)
        journal = {}
        checkout = self.backend.restore(self.record, self.manifest, journal)
        (checkout / ".git/CHERRY_PICK_HEAD").write_text(old + "\n")

        def finish(checkout, *args, **kwargs):
            (checkout / ".git/CHERRY_PICK_HEAD").unlink()
            git(checkout, "reset", "--hard", self.current["head"])
            (checkout / "A.lean").write_text("retained delta resolved\n")
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=finish) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertEqual(self.journal()["continuation_reason"], "resolve-interrupted-source-operation")
        self.assertFalse((self.record / "reintegrations").exists())

    def test_delta_with_operation_and_no_conflicts_cannot_skip_reasoning(self):
        self.rewritten_checkpoint(conflicting=True)

        def unfinished(checkout, *args, **kwargs):
            (checkout / "A.lean").write_text("resolved source\n")
            git(checkout, "add", "A.lean")
            (checkout / ".git/CHERRY_PICK_HEAD").write_text(self.current["head"] + "\n")
            raise Death()

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=unfinished),
            self.assertRaises(Death),
        ):
            self.run_recovery()

        def finish(checkout, *args, **kwargs):
            (checkout / ".git/CHERRY_PICK_HEAD").unlink()
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=finish) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertEqual(self.journal()["content_attempts"], 2)

    def test_lifted_hold_can_publish_without_model_funding(self):
        self.current["labels"] = ["hold"]
        self.assertFalse(self.run_recovery())
        due = self.journal()["retry_at"] + 1
        with patch.object(rr.time, "time", return_value=due):
            self.assertTrue(rr.due(self.cfg))
            self.test_loop_recovery_precedes_model_budget()
            with patch.object(saved, "_complete", side_effect=AssertionError("held metadata rehashed source")):
                self.assertFalse(rr.recover(self.w, self.opts))
        self.current["labels"] = []
        with patch.object(rr.time, "time", return_value=due + 1801):
            self.assertTrue(rr.due(self.cfg))
            self.assertTrue(rr.recover(self.w, self.opts))
        self.assertEqual(self.values.get("fix-10-head", 0), 0)

    def test_held_backlog_does_not_continuously_replace_normal_rounds(self):
        self.current["labels"] = ["hold"]
        self.assertFalse(self.run_recovery())
        for n in range(40):
            record = self.record.parent / f"held-{n}"
            rr.atomic_json(record / "manifest.json", self.manifest)
            rr.write(record, {}, "blocked", reason=rr.reason(rr.Block("pr-held", "hold")))
        self.assertTrue(rr.due(self.cfg))
        with patch.object(saved, "_complete", side_effect=AssertionError("held metadata rehashed source")):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertFalse(rr.due(self.cfg))

    def test_old_pending_normal_push_does_not_poison_a_new_round(self):
        self.normal_lost_push()
        self.current["head"] = git(self.remote, "rev-parse", "main")
        (self.co / "A.lean").write_text("independent later repair\n")
        self.owner = {
            **self.owner,
            "round_id": "next-round",
            "expected_head": self.current["head"],
            "publication_marker": dict(ref="refs/tauceti/published/push/main", before=None, scope="next-round"),
        }
        self.save_updated_checkpoint()
        self.assertNotIn("pending_push", self.manifest.get("round_outcome") or {})
        self.assertTrue(self.run_recovery())
        self.assertEqual(git(self.remote, "show", "main:A.lean"), "independent later repair")

    def test_new_round_rejection_preserves_only_the_older_rounds_uncertainty(self):
        _, pending = self.normal_lost_push()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.advance_writer()
        with patch.dict(os.environ, TAUCETI_PUSH_SCOPE="next-round"):
            self.assertEqual(self.normal_push().returncode, 78)
        self.assertIsNone(saved.ref_object(self.co, "refs/tauceti/pending-push/next-round/main"))
        self.assertEqual(saved.ref_object(self.co, "refs/tauceti/pending-push/unscoped/main"), pending)

    def test_push_attempt_tags_do_not_obstruct_retired_checkout_cleanup(self):
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal source")
        self.assertEqual(self.normal_push().returncode, 0)
        self.assertEqual(git(self.co, "rev-list", "--count", "--all"), "2")
        decision = saved.save_for_cleanup(self.cfg, self.co, owner=self.owner)
        self.assertTrue(decision.ok, decision.reason)

    def test_round_publication_receipts_remain_cleanup_evidence_after_rewrite(self):
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "first published source")
        first = git(self.co, "rev-parse", "HEAD")
        with patch.dict(os.environ, TAUCETI_PUSH_SCOPE="first-round"):
            self.assertEqual(self.normal_push().returncode, 0)
        git(self.co, "reset", "--hard", self.base)
        (self.co / "B.lean").write_text("replacement source\n")
        git(self.co, "add", "B.lean")
        git(self.co, "commit", "-qm", "replacement")
        env = {
            **os.environ,
            "TAUCETI_PUSH_SCOPE": "second-round",
            "TAUCETI_PUSH_REF": "main",
            "TAUCETI_PUSH_EXPECT": first,
            "TAUCETI_PUSH_REMOTE": str(self.remote),
        }
        result = subprocess.run(
            [str(Path(__file__).resolve().parent.parent / "scripts/git-safe-push")],
            cwd=self.co,
            env=env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = saved.save_for_cleanup(self.cfg, self.co, owner=self.owner)
        self.assertTrue(decision.ok, decision.reason)

    def test_post_publication_leftovers_stay_available_to_fresh_normal_work(self):
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "published repair")
        published = git(self.co, "rev-parse", "HEAD")
        git(self.co, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = published
        self.owner["round_outcome"] = dict(published_head=published)
        (self.co / "A.lean").write_text("useful unpublished follow-up\n")
        self.save_updated_checkpoint()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "post-publication-leftovers")
        context, handoffs = self.handoff()
        self.assertIn(str(self.record), context)
        self.assertNotIn(str(self.record), [record for record, _ in handoffs])
        rr.return_handoffs(handoffs)
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertFalse(self.journal().get("forensic_only", False))

    def test_confirmed_normal_push_preserves_later_unpublished_edits_as_evidence(self):
        self.normal_lost_push()
        (self.co / "A.lean").write_text("useful unpublished follow-up\n")
        owner = {
            **self.owner,
            "round_id": "receiving-round",
            "publication_marker": dict(ref="refs/tauceti/published/push/main", before=None),
        }
        self.record = saved._record(self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=owner)
        self.manifest = saved._complete(self.record)
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "post-publication-leftovers")
        self.assertIn(str(self.record), rr.retained_context(self.cfg, "upstream/repo", 10))
        self.assertFalse(self.journal().get("forensic_only", False))
        self.assertEqual(FakeVerification.calls, 0)

    def test_reoffered_handoff_cannot_reset_an_uncertain_attempt(self):
        self.handoff()
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal source")
        self.remote.joinpath("hooks/pre-receive").write_text("#!/bin/sh\nexit 1\n")
        # An auth/transport failure has no definitive destination rejection.
        missing = self.root / "missing-remote.git"
        with patch.dict(os.environ, TAUCETI_PUSH_REMOTE=str(missing)):
            env = {**os.environ, "TAUCETI_PUSH_REF": "main", "TAUCETI_PUSH_EXPECT": self.base}
            result = subprocess.run(
                [str(Path(__file__).resolve().parent.parent / "scripts/git-safe-push")],
                cwd=self.co,
                env=env,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 1)
        context, handoffs = self.handoff()
        self.assertIn(str(self.record), context)
        self.assertEqual(handoffs, [])
        self.assertTrue(self.journal()["forensic_only"])
        self.assertEqual(self.journal()["state"], "obsolete")
        self.assertFalse(self.run_recovery())

    def test_non_owned_operation_cannot_import_removed_ancestry(self):
        old = self.rewritten_checkpoint(conflicting=True)
        checkout = self.backend.restore(self.record, self.manifest, {})
        (checkout / ".git/CHERRY_PICK_HEAD").write_text(old + "\n")

        def merge_old_lineage(checkout, *args, **kwargs):
            (checkout / ".git/CHERRY_PICK_HEAD").unlink()
            identity = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.com")
            git(checkout, "add", "-A")
            git(checkout, *identity, "commit", "-qm", "saved source")
            git(checkout, *identity, "merge", "-s", "ours", "--no-edit", self.current["head"])
            return 0

        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", side_effect=merge_old_lineage),
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "removed-history-reintroduced")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.current["head"])

    def test_local_push_ref_failure_is_unsent_infrastructure_with_backoff(self):
        prepare = self.backend.prepare

        def lock_ref(checkout, *args):
            result = prepare(checkout, *args)
            lock = checkout / ".git/refs/tauceti/push-attempt-scope/unscoped/main.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("stale local lock\n")
            return result

        with patch.object(self.backend, "prepare", side_effect=lock_ref):
            for _ in range(3):
                self.assertFalse(self.run_recovery())
                self.retry_now()
        self.assertEqual(self.journal()["infrastructure_failures"], 3)
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertNotIn("publication", self.journal())
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_uncertain_publication_cannot_change_commit_or_launch_content_repair(self):
        intent = self.lost_push_response()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.retry_now()
        with patch.object(self.backend, "verify", return_value=dict(status="failed")):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-revalidation-failed")
        self.assertEqual(self.values.get("fix-10-head", 0), 0)
        (self.record / "checkout/A.lean").write_text("different source\n")
        self.retry_now()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-uncertain")
        self.assertEqual(self.journal()["publication"]["oid"], intent["oid"])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_unavailable_shared_metadata_preserves_hold_without_direct_fallback(self):
        self.current["labels"] = ["hold"]
        self.assertFalse(self.run_recovery())
        due = self.journal()["retry_at"] + 1
        with (
            patch.object(rr.time, "time", return_value=due),
            patch("tauceti_worker.github_state.read", return_value=(False, None)),
            patch.object(self.w.gh, "pr_view", side_effect=AssertionError("direct GitHub fallback")),
        ):
            self.assertFalse(rr.recover(self.w, self.opts))
        self.assertEqual(self.journal()["state"], "blocked")
        self.assertEqual(self.journal()["reason"]["code"], "pr-held")

    def test_rejected_replay_cannot_erase_an_earlier_uncertain_recovery_push(self):
        intent = self.lost_push_response()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        self.retry_now()
        self.assertFalse(self.run_recovery())
        self.assertTrue(self.journal()["publication"]["push_started"])
        self.assertEqual(self.journal()["publication"]["oid"], intent["oid"])
        hook.unlink()
        self.advance_writer()
        current = self.current["head"]
        self.retry_now()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-head-moved")
        self.assertEqual(git(self.remote, "rev-parse", "main"), current)

    def test_unreconciled_supervisor_publication_is_forensic_evidence_without_handoff(self):
        intent = self.lost_push_response()
        git(self.remote, "update-ref", "refs/heads/main", self.base)
        self.advance_writer()
        with patch.object(rr.time, "time", return_value=self.journal()["retry_at"] + 301):
            context, handoffs = self.handoff()
        self.assertEqual(handoffs, [])
        self.assertIn('"possibly_published_then_removed": true', context)
        self.assertEqual(self.journal()["state"], "eligible")
        self.assertEqual(self.journal()["publication"]["oid"], intent["oid"])
        self.retry_now()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "publication-head-moved")

    def test_another_round_push_cannot_consume_an_unused_handoff(self):
        self.handoff()
        saved.guard(self.cfg, self.co, repository="upstream/repo")
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "unrelated round source")
        with patch.dict(os.environ, TAUCETI_PUSH_SCOPE="another-round"):
            self.assertEqual(self.normal_push().returncode, 0)
        rr.reconcile_handoff(self.cfg, self.record, self.journal())
        self.assertEqual(self.journal()["handoff"]["state"], "returned")
        self.assertEqual(self.journal()["state"], "eligible")
        self.assertIn(str(self.record), rr.retained_context(self.cfg, "upstream/repo", 10))

    def test_normal_local_ref_failure_retains_retryable_source_without_pending_intent(self):
        self.handoff()
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "normal source")
        lock = self.co / ".git/refs/tauceti/push-attempt-scope/unscoped/main.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("stale local lock\n")
        self.assertEqual(self.normal_push().returncode, 80)
        self.assertIsNone(saved.ref_object(self.co, "refs/tauceti/pending-push/unscoped/main"))
        saved.guard(self.cfg, self.co, repository="upstream/repo")
        rr.reconcile_handoff(self.cfg, self.record, self.journal())
        self.assertEqual(self.journal()["handoff"]["state"], "transferred")
        self.assertTrue(rr.recover(self.w, self.opts))


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
            for name, phase in second["phases"].items():
                # Artifact checks must run again: unchanged source alone cannot certify
                # the build outputs inspected by a previous stage-only invocation.
                self.assertEqual("reused_from" in phase, name not in {"axioms", "module-system"})
            warm.assert_called()
            publication = backend.prepare(case.root, manifest, facts, second)
        self.assertEqual(publication["oid"], git(case.root, "rev-parse", "HEAD"))
        self.assertTrue(Path(publication["receipt"]).is_file())


if __name__ == "__main__":
    unittest.main()
