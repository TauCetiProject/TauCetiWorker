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

    def test_head_moved_requires_continuation(self):
        # A new writer's commit changes another file, so this checkpoint needs integration.
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        (other / "Other.lean").write_text("other writer\n")
        git(other, "add", "-A")
        git(other, "-c", "user.name=other", "-c", "user.email=other@example.com", "commit", "-qm", "other writer")
        git(other, "push", "-q", "origin", "HEAD:main")
        self.current["head"] = git(self.remote, "rev-parse", "main")
        with patch.object(self.backend, "continue_source", side_effect=rr.Wait("budget-refused", "no funds")) as model:
            self.assertFalse(self.run_recovery())
        self.assertEqual(model.call_args.args[4], "remote-head-moved-reintegration-required")
        self.assertEqual(self.journal()["reason"]["code"], "budget-refused")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.current["head"])
        self.assert_retained()

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

    def test_outbox_uncertainty_reconciles_without_duplicate_push(self):
        with patch("tauceti_worker.github_outbox.pending_for", return_value=True):
            self.assertFalse(self.run_recovery())
            self.assertFalse(self.run_recovery())
        self.retry_now()
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

    def test_justified_continuation_fresh_admission_and_reverify(self):
        FakeVerification.failures = 1
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)) as admission,
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch("tauceti_worker.agents.run_agent_host", return_value=0) as model,
        ):
            self.assertTrue(self.run_recovery())
        admission.assert_called_once()
        self.assertTrue(admission.call_args.kwargs["fresh"])
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

    def test_damaged_checkpoint_never_runs(self):
        (self.record / "staged.patch").write_text("corrupt")
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "corrupt-checkpoint")
        self.assertFalse((self.record / "checkout").exists())


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
