#!/usr/bin/env python3
"""Shared-state races, coverage validation, retry publication and phase integration."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import overlaps, work_units
from tauceti_worker.cli import build_parser, raise_on_untargetable_tasks
from tauceti_worker.config import Die, NoProgress
from tauceti_worker.constants import KIND_KEYS
from tauceti_worker.overlap_state import CURSOR_REF, Lease, State, environment, git
from tauceti_worker.survey import Candidate, Survey
from tauceti_worker.transcript import AgentTranscriptRenderer


class CacheTests(unittest.TestCase):
    def test_fresh_read_and_credential_boundary(self):
        from tauceti_worker import overlap_support as support

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "alice"}),
        ):
            values = iter([1, 2, 3, 4])

            def fetch():
                return next(values)

            self.assertEqual(support.shared_read(["cursor"], fetch), 1)
            self.assertEqual(support.shared_read(["cursor"], fetch), 1)
            with support.read_context(fresh=True):
                self.assertEqual(support.shared_read(["cursor"], fetch), 2)
            with patch.dict(os.environ, {"GH_TOKEN": "bob"}):
                self.assertEqual(support.shared_read(["cursor"], fetch), 3)
            support.invalidate()
            self.assertEqual(support.shared_read(["cursor"], fetch), 4)

    def test_failed_and_invalidated_reads_are_not_cached(self):
        from tauceti_worker import overlap_support as support

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "alice"}),
        ):

            def fail():
                raise Die("incomplete inventory")

            with self.assertRaises(Die):
                support.shared_read(["inventory"], fail)

            def concurrent_write():
                support.invalidate()
                return [1]

            self.assertEqual(support.shared_read(["inventory"], concurrent_write), [1])
            self.assertEqual(support.shared_read(["inventory"], lambda: [2]), [2])

    def test_failed_snapshot_clone_retries_and_repairs_legacy_partial(self):
        from tauceti_worker import overlap_support as support

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp}):
            fixture = Path(tmp) / "source"
            fixture.mkdir()
            git("init", fixture)
            (fixture / "lake-manifest.json").write_text('{"packages": []}')
            git("add", ".", cwd=fixture)
            with environment(
                {
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@invalid",
                    "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "test@invalid",
                }
            ):
                git("commit", "-m", "fixture", cwd=fixture)
            sha = git("rev-parse", "HEAD", cwd=fixture).stdout.strip()
            runner = support._git
            attempts = []

            def clone_failure(*args):
                args = list(args)
                if "https://github.com/TauCetiProject/TauCeti" in args:
                    args[args.index("https://github.com/TauCetiProject/TauCeti")] = fixture
                if args[:1] == ["clone"] and "--branch=snapshot" in args:
                    attempts.append(Path(args[-1]))
                    if len(attempts) == 1:
                        Path(args[-1]).mkdir()
                        (Path(args[-1]) / "partial").write_text("interrupted clone")
                        return subprocess.CompletedProcess(args, 1, "", "clone failed")
                return runner(*args)

            with patch.object(support, "_git", side_effect=clone_failure):
                self.assertIsNone(support.snapshot("TauCetiProject/TauCeti", ttl=3600))
                self.assertFalse(attempts[0].exists())
                target = support.snapshot("TauCetiProject/TauCeti", ttl=3600)
                self.assertEqual(git("rev-parse", "HEAD", cwd=target).stdout.strip(), sha)
                # Existing partial legacy caches must not pass the target-exists test.
                support._remove_snapshot(target)
                target.mkdir()
                (target / "partial").write_text("legacy failed clone")
                self.assertEqual(support.snapshot("TauCetiProject/TauCeti", ttl=3600), target)
                self.assertTrue((target / "lake-manifest.json").is_file())
                self.assertEqual(len(attempts), 3)

    def test_failed_refresh_keeps_latest_successful_snapshot(self):
        from tauceti_worker import overlap_support as support

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp}):
            fixture = Path(tmp) / "source"
            fixture.mkdir()
            git("init", fixture)
            (fixture / "lake-manifest.json").write_text('{"packages": []}')
            git("add", ".", cwd=fixture)
            identity = {
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@invalid",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@invalid",
            }
            with environment(identity):
                git("commit", "-m", "A", cwd=fixture)
            runner = support._git
            fail_refresh = False

            def scripted(*args):
                args = list(args)
                if "https://github.com/TauCetiProject/TauCeti" in args:
                    args[args.index("https://github.com/TauCetiProject/TauCeti")] = fixture
                if "fetch" in args and fail_refresh:
                    return subprocess.CompletedProcess(args, 1, "", "network unavailable")
                return runner(*args)

            with patch.object(support, "_git", side_effect=scripted):
                first = support.snapshot("TauCetiProject/TauCeti", ttl=0)
                (fixture / "additional.lean").write_text("-- commit B")
                git("add", ".", cwd=fixture)
                with environment(identity):
                    git("commit", "-m", "B", cwd=fixture)
                latest = support.snapshot("TauCetiProject/TauCeti", ttl=0)
                self.assertNotEqual(first, latest)
                state_path = next((Path(tmp) / "overlap" / "repositories").glob("*/state.json"))
                last_success = state_path.read_text()
                fail_refresh = True
                self.assertEqual(support.snapshot("TauCetiProject/TauCeti", ttl=0), latest)
                self.assertEqual(state_path.read_text(), last_success)
                state_path.unlink()
                self.assertIsNone(support.snapshot("TauCetiProject/TauCeti", ttl=0))

    def test_ambiguous_secondary_write_is_not_replayed(self):
        from tauceti_worker import github

        result = subprocess.CompletedProcess([], 1, "", "You have exceeded a secondary rate limit")
        with patch.object(github, "run", return_value=result) as run, patch.object(github.time, "sleep") as sleep:
            self.assertEqual(github.gh_run(["gh", "api", "--method", "POST", "repos/a/b/issues"]).returncode, 1)
            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        remote = self.root / "remote.git"
        git("init", "--bare", remote)
        self.a = State(self.root / "a.git", url=str(remote))
        self.b = State(self.root / "b.git", url=str(remote))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_only_and_stale_update(self):
        self.assertTrue(self.a.advance("", "a" * 40))
        self.assertFalse(self.b.advance("", "b" * 40))
        old, _ = self.a.cursor()
        self.assertEqual(self.b.cursor(), self.a.cursor())
        self.assertTrue(self.b.advance(old, "b" * 40))
        self.assertFalse(self.a.advance(old, "c" * 40))
        self.assertEqual(self.a.cursor()[1], "b" * 40)

    def test_account_and_run_distinct_leases(self):
        a = Lease(self.a, "mathlib-overlap-scan", "alice", "worker1")
        b = Lease(self.b, "mathlib-overlap-scan", "bob", "worker1")
        again = Lease(self.a, "mathlib-overlap-scan", "alice", "worker1")
        self.assertNotEqual(a.owner, again.owner)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertFalse(again.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.require()
        b.release()

    def test_takeover_cannot_publish_or_release_peer(self):
        a = Lease(self.a, "scan", "alice", "worker1")
        b = Lease(self.b, "scan", "bob", "worker1")
        self.assertTrue(a.acquire())
        oid, payload = self.a.read(a.ref)
        payload["expires_at"] = 0
        self.a.cas(a.ref, oid, payload)
        self.assertTrue(b.acquire())
        with self.assertRaises(Die):
            a.require()
        a.release()
        b.require()
        b.release()

    def test_unacquired_lease_has_no_remote_cleanup(self):
        held = Lease(self.a, "scan", "alice", "test")
        self.assertTrue(held.acquire())
        other = Lease(self.b, "scan", "bob", "test")
        with patch.object(self.b, "read", wraps=self.b.read) as read:
            with other as lease:
                self.assertIsNone(lease)
            self.assertEqual(read.call_count, 1)
        held.release()

    def test_release_failure_does_not_mask_successful_body(self):
        lease = Lease(self.a, "scan", "alice", "test")
        self.assertTrue(lease.acquire())
        with (
            patch.object(lease, "acquire", return_value=True),
            patch.object(self.a, "read", side_effect=Die("network unavailable")),
        ):
            with lease:
                completed = True
        self.assertTrue(completed)
        self.assertFalse(lease.acquired)

    def test_commit_identity_is_subprocess_local(self):
        names = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
        run = self.a.run

        def inspect(*args, **kwargs):
            if "commit-tree" in args:
                self.assertTrue(all(name not in os.environ for name in names))
                self.assertTrue(all(name in kwargs["env"] for name in names))
            return run(*args, **kwargs)

        with environment({name: None for name in names}), patch.object(self.a, "run", side_effect=inspect):
            self.assertTrue(self.a.advance("", "a" * 40))
            self.assertTrue(all(name not in os.environ for name in names))

    def test_malformed_and_remote_errors_fail_closed(self):
        self.a.cas(CURSOR_REF, "", {"mathlib_commit": "a" * 40})
        with self.assertRaises(Die):
            self.a.cursor()
        lease = Lease(self.b, "scan", "bob", "worker1")
        self.b.cas(lease.ref, "", {"owner": "alice", "expires_at": 0})
        with self.assertRaises(Die):
            lease.acquire()
        with patch.object(self.b, "run", side_effect=Die("remote failed")), self.assertRaises(Die):
            self.b.cursor()


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.batch = {"from": "a" * 40, "through": "c" * 40, "commits": [{"sha": "b" * 40}, {"sha": "c" * 40}]}
        self.finding = {
            "change_commit": "b" * 40,
            "mathlib_declarations": ["Mathlib.foo"],
            "tauceti_declarations": ["TauCeti.foo"],
            "classification": "equivalent",
            "title": "Adopt Mathlib foo",
            "body": "Evidence: https://github.com/example\n\nPreserve: extra API.",
        }
        self.result = {
            "from": "a" * 40,
            "through": "c" * 40,
            "complete": True,
            "examined_commits": ["b" * 40, "c" * 40],
            "findings": [self.finding],
        }

    def test_all_commits_and_completion_required(self):
        self.assertEqual(overlaps.validate_scan(self.result, self.batch), [self.finding])
        for updates in (
            {"complete": False},
            {"complete": "true"},
            {"through": "d" * 40},
            {"examined_commits": ["c" * 40]},
            {"findings": None},
        ):
            bad = {**self.result, **updates}
            with self.assertRaises(Die):
                overlaps.validate_scan(bad, self.batch)
        self.assertEqual(overlaps.validate_scan({**self.result, "findings": []}, self.batch), [])

    def test_mapping_and_preservation_required(self):
        for updates in (
            {"classification": "maybe"},
            {"change_commit": "d" * 40},
            {"body": "delete everything"},
            {"mathlib_declarations": []},
        ):
            with self.assertRaises(Die):
                overlaps.validate_scan({**self.result, "findings": [{**self.finding, **updates}]}, self.batch)

    def test_partial_publication_retry_including_closed_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            published = []
            attempts = []
            second = {**self.finding, "mathlib_declarations": ["Mathlib.bar"]}

            def invoke(argv):
                attempts.append(argv)
                if len(attempts) == 2:
                    return SimpleNamespace(returncode=1, stdout="", stderr="interrupted")
                body = json.loads(Path(argv[argv.index("--input") + 1]).read_text())["body"]
                number = 100 + len(published)
                published.append(
                    {"number": number, "body": body, "state": "closed", "labels": [{"name": overlaps.LABEL}]}
                )
                return SimpleNamespace(returncode=0, stdout=json.dumps(published[-1]), stderr="")

            gh = SimpleNamespace(_gh=invoke)
            lease = SimpleNamespace(state=SimpleNamespace(directory=Path(tmp) / "state.git"), require=lambda: None)
            with patch.object(overlaps, "issues", side_effect=lambda *_a, **_k: list(published)):
                with self.assertRaises(Die):
                    overlaps.publish(gh, [self.finding, second], lease)
                self.assertEqual(overlaps.publish(gh, [self.finding, second], lease), [100, 101])
            self.assertEqual(len(published), 2)
            self.assertEqual(len(attempts), 3)

    def test_missing_publication_label_retains_receipt_without_recreating(self):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = overlaps.issue_id(self.finding)
            row = {"number": 123, "body": overlaps.marker(fingerprint), "labels": []}
            calls = []

            def invoke(argv):
                calls.append(argv)
                return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="")

            gh = SimpleNamespace(_gh=invoke)
            lease = SimpleNamespace(state=SimpleNamespace(directory=Path(tmp) / "state.git"), require=lambda: None)
            with patch.object(overlaps, "issues", return_value=[]):
                for _ in range(2):
                    with self.assertRaisesRegex(Die, "issue #123 lacks"):
                        overlaps.publish(gh, [self.finding], lease)
                self.assertEqual(sum("POST" in argv for argv in calls), 1)
                row["labels"] = [{"name": overlaps.LABEL}]
                self.assertEqual(overlaps.publish(gh, [self.finding], lease), [123])
                self.assertEqual(sum("POST" in argv for argv in calls), 1)
                row["body"] = "missing finding marker"
                with self.assertRaisesRegex(Die, "issue #123 lacks"):
                    overlaps.publish(gh, [self.finding], lease)

    def test_unlabelled_issue_does_not_advance_retained_scan_cursor(self):
        from contextlib import nullcontext

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "overlap-runs" / "prior"
            work.mkdir(parents=True)
            packet = {**self.batch, "pin": "p" * 40}
            (work / "batch.json").write_text(json.dumps(packet))
            (work / "result.json").write_text(json.dumps(self.result))
            state = SimpleNamespace(
                cursor=lambda: ("checkpoint", self.batch["from"]), advance=Mock(), directory=Path(tmp) / "state.git"
            )
            lease = SimpleNamespace(require=lambda: None, state=state)
            row = {"number": 123, "body": overlaps.marker(overlaps.issue_id(self.finding)), "labels": []}
            gh = SimpleNamespace(_gh=lambda *args: SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr=""))
            worker = SimpleNamespace(cfg=SimpleNamespace(state=Path(tmp), wid="test"), gh=gh)
            with (
                patch.object(overlaps, "require_shared"),
                patch.object(overlaps, "require_publication_rights"),
                patch.object(overlaps, "me", return_value="alice"),
                patch.object(overlaps, "state_for", return_value=state),
                patch.object(overlaps, "Lease", return_value=nullcontext(lease)),
                patch.object(overlaps, "sources", return_value=(Path(tmp), "t" * 40, "p" * 40)),
                patch.object(overlaps, "mathlib_history", return_value=Path(tmp)),
                patch.object(overlaps, "select_batch", return_value=["b" * 40, "c" * 40]),
                patch.object(overlaps, "issues", return_value=[]),
                self.assertRaisesRegex(Die, "issue #123 lacks"),
            ):
                overlaps.do_scan(worker, None, None, SimpleNamespace(only=["overlap-scan"]), False)
            state.advance.assert_not_called()

    def test_paginated_inventory_and_peer_pr(self):
        rows = [{"number": i, "body": "", "state": "open"} for i in range(250)]
        gh = SimpleNamespace(
            _gh=lambda *_: SimpleNamespace(returncode=0, stdout=json.dumps([rows[:100], rows[100:200], rows[200:]]))
        )
        self.assertEqual(len(overlaps.issues(gh, fresh=True)), 250)
        peer = {"number": 1, "body": overlaps.marker("a" * 64), "draft": False, "user": {"login": "bob"}}
        gh._gh = lambda *_: SimpleNamespace(returncode=0, stdout=json.dumps([[peer]]))
        self.assertEqual(overlaps.overlap_prs(gh, fresh=True)[0]["author"]["login"], "bob")


class BatchTests(unittest.TestCase):
    def test_chronology_bounds_and_no_gap(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            environment(
                {
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_COMMITTER_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@test",
                    "GIT_COMMITTER_EMAIL": "test@test",
                }
            ),
        ):
            repo = Path(tmp)
            git("init", "-b", "master", repo)
            shas = []
            for i in range(5):
                (repo / "content").write_text(str(i))
                git("add", "content", cwd=repo)
                git("commit", "-m", str(i), cwd=repo)
                shas.append(git("rev-parse", "HEAD", cwd=repo).stdout.strip())
            bare = repo / ".git"
            self.assertEqual(overlaps.select_batch(bare, shas[0], shas[4], 2), shas[1:3])
            self.assertEqual(overlaps.select_batch(bare, shas[3], shas[4], 20), shas[4:])
            self.assertEqual(overlaps.select_batch(bare, shas[4], shas[4], 20), [])
            with self.assertRaises(Die):
                overlaps.select_batch(bare, shas[4], shas[0], 2)

    def test_first_parent_merge_includes_side_branch_change(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            environment(
                {
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_COMMITTER_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@test",
                    "GIT_COMMITTER_EMAIL": "test@test",
                }
            ),
        ):
            repo = Path(tmp)
            git("init", "-b", "master", repo)
            git("commit", "--allow-empty", "-m", "base", cwd=repo)
            base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
            git("checkout", "-b", "side", cwd=repo)
            (repo / "Mathlib").mkdir()
            (repo / "Mathlib/Side.lean").write_text("theorem side : True := trivial\n")
            git("add", ".", cwd=repo)
            git("commit", "-m", "side", cwd=repo)
            git("checkout", "master", cwd=repo)
            git("merge", "--no-ff", "side", "-m", "merge", cwd=repo)
            merged = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
            self.assertEqual(overlaps.select_batch(repo / ".git", base, merged, 20), [merged])
            changes = overlaps.history(repo / ".git", "diff", merged + "^1", merged).stdout
            self.assertIn("theorem side", changes)


class DispatchTests(unittest.TestCase):
    def test_missing_shared_access_never_uses_private_state_or_launches(self):
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None)
        sv = Survey(worker_id="test")
        with (
            patch.object(overlaps, "can_push", return_value=False),
            patch.object(overlaps, "shared_claims_granted", return_value=False),
            patch.object(overlaps, "state_for", side_effect=AssertionError("private coordination")),
        ):
            overlaps.survey_overlaps(worker.cfg, worker.gh, sv)
            self.assertFalse(sv.overlap_scan.actionable)
            self.assertFalse(sv.overlap_fix.actionable)
            self.assertIn("no private-fork fallback", sv.overlap_scan.suppressed[0].reason)
            for action in (overlaps.do_scan, overlaps.do_fix):
                with self.assertRaises(NoProgress):
                    action(worker, sv, Candidate(0, "123"), None, False)

    def test_scan_requires_label_rights_without_blocking_fork_repairs(self):
        from tauceti_worker import agents

        state = SimpleNamespace(cursor=lambda: ("checkpoint", "a" * 40))
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None)
        row = {"number": 123, "title": "overlap", "body": overlaps.marker("f" * 64), "state": "open", "labels": []}
        sv = Survey(worker_id="test")
        with (
            patch.object(overlaps, "can_push", side_effect=lambda repo: repo == overlaps.CLAIMS),
            patch.object(overlaps, "shared_claims_granted", return_value=True),
            patch.object(overlaps, "state_for", return_value=state),
            patch.object(overlaps, "sources", return_value=(Path("/tmp"), "t" * 40, "b" * 40)),
            patch.object(overlaps, "issues", return_value=[row]),
            patch.object(overlaps, "overlap_prs", return_value=[]),
            patch.object(overlaps, "me", return_value="alice"),
            patch.object(agents, "run_agent_proc", side_effect=AssertionError("paid scan without label rights")),
        ):
            overlaps.survey_overlaps(worker.cfg, worker.gh, sv)
            self.assertFalse(sv.overlap_scan.actionable)
            self.assertIn("required to label findings", sv.overlap_scan.suppressed[0].reason)
            self.assertEqual(len(sv.overlap_fix.actionable), 1)
            with self.assertRaises(NoProgress):
                overlaps.do_scan(worker, sv, Candidate(0, "cursor"), SimpleNamespace(only=["overlap-scan"]), False)

    def test_invalid_range_declines_automatic_scan_but_focused_scan_is_loud(self):
        from contextlib import nullcontext

        from tauceti_worker import agents

        state = SimpleNamespace(cursor=lambda: ("checkpoint", "a" * 40))
        lease = SimpleNamespace(require=lambda: None)
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None)
        with (
            patch.object(overlaps, "require_shared"),
            patch.object(overlaps, "require_publication_rights"),
            patch.object(overlaps, "state_for", return_value=state),
            patch.object(overlaps, "Lease", side_effect=lambda *args: nullcontext(lease)),
            patch.object(overlaps, "me", return_value="alice"),
            patch.object(overlaps, "sources", return_value=(Path("/tmp"), "t" * 40, "b" * 40)),
            patch.object(overlaps, "mathlib_history", return_value=Path("/tmp")),
            patch.object(overlaps, "select_batch", side_effect=overlaps.HistoryRangeError("pin reverted")),
            patch.object(agents, "run_agent_proc", side_effect=AssertionError("paid scan for invalid history")),
        ):
            self.assertIsNone(overlaps.do_scan(worker, None, None, SimpleNamespace(only=[]), False))
            sv = Survey(worker_id="test")
            sv.overlap_scan.actionable.append(Candidate(0, "cursor", "pin changed"))
            worker.rs = worker.counters = None
            stages = []

            def dispatch(stage, current, survey, candidate, opts):
                stages.append(stage)
                return overlaps.do_scan(current, survey, candidate, opts, False) if stage == "overlap-scan" else 0

            opts = SimpleNamespace(only=[], dry_run=True, prs=(), work_model="codex", sandbox_host=True)
            with (
                patch.object(work_units, "survey", return_value=sv),
                patch.object(work_units, "dispatch", side_effect=dispatch),
                patch.object(work_units, "throttle_review"),
            ):
                self.assertEqual(work_units.run_round(worker, opts), 0)
            self.assertEqual(stages, ["overlap-scan", "roadmap"])
            with self.assertRaisesRegex(overlaps.HistoryRangeError, "pin reverted"):
                overlaps.do_scan(worker, None, None, SimpleNamespace(only=["overlap-scan"]), False)

    def test_repair_recognizes_peer_pr_without_launching(self):
        from contextlib import nullcontext

        from tauceti_worker import agents

        fingerprint = "f" * 64
        row = {"number": 123, "body": overlaps.marker(fingerprint), "state": "open", "labels": []}
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None)
        lease = SimpleNamespace(require=lambda: None)
        with (
            patch.object(overlaps, "require_shared"),
            patch.object(overlaps, "me", return_value="alice"),
            patch.object(overlaps, "state_for", return_value=None),
            patch.object(overlaps, "Lease", return_value=nullcontext(lease)),
            patch.object(overlaps, "issues", return_value=[row]),
            patch.object(overlaps, "overlap_prs", return_value=[{"body": row["body"], "author": {"login": "bob"}}]),
            patch.object(agents, "run_agent_host", side_effect=AssertionError("duplicate repair")),
        ):
            self.assertIsNone(overlaps.do_fix(worker, None, Candidate(0, "123"), None, False))

    def test_repair_host_enforces_claim_and_marker(self):
        from contextlib import nullcontext

        from tauceti_worker import agents

        row = {"number": 123, "body": overlaps.marker("f" * 64), "state": "open", "labels": []}
        worker = SimpleNamespace(
            cfg=SimpleNamespace(wid="test", checkout=Path("/tmp/co"), logdir=Path("/tmp/log")), gh=None
        )
        lease = SimpleNamespace(require=lambda: None, owner="alice/test/run", key="overlap-fix/123")
        import os

        def agent(_cwd, prompt, *_args, **_kw):
            self.assertEqual(os.environ["TAUCETI_AUTHOR_CLAIM_KEY"], lease.key)
            self.assertEqual(os.environ["TAUCETI_TARGET_MARKER"], row["body"])
            self.assertEqual(os.environ["TAUCETI_WORKER_ID"], lease.owner)
            self.assertIn("--head alice:<branch>", prompt)
            self.assertNotIn("--head alice/TauCeti:", prompt)
            return 0

        with (
            patch.object(overlaps, "require_shared"),
            patch.object(overlaps, "me", return_value="alice"),
            patch.object(overlaps, "state_for", return_value=None),
            patch.object(overlaps, "Lease", return_value=nullcontext(lease)),
            patch.object(overlaps, "issues", return_value=[row]),
            patch.object(overlaps, "ensure_fork", return_value="alice/TauCeti"),
            patch.object(overlaps, "overlap_prs", side_effect=[[], [{"body": row["body"]}]]),
            patch.object(agents, "prepare_checkout", return_value=True),
            patch.object(agents, "run_agent_host", side_effect=agent),
            patch.object(work_units, "_effective_authoring_profile", return_value="codex"),
        ):
            self.assertEqual(overlaps.do_fix(worker, None, Candidate(0, "123"), None, False), 0)

    def test_completed_local_result_retries_publication_without_another_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "overlap-runs" / "old"
            work.mkdir(parents=True)
            packet = {"from": "a" * 40, "through": "b" * 40, "pin": "p" * 40, "commits": [{"sha": "b" * 40}]}
            (work / "batch.json").write_text(json.dumps(packet))
            (work / "result.json").write_text(
                json.dumps(
                    {
                        "from": "a" * 40,
                        "through": "b" * 40,
                        "examined_commits": ["b" * 40],
                        "complete": True,
                        "findings": [],
                    }
                )
            )
            state = SimpleNamespace(cursor=lambda: ("old-checkpoint", "a" * 40), advance=lambda *_: True)
            lease = SimpleNamespace(require=lambda: None)
            worker = SimpleNamespace(cfg=SimpleNamespace(state=Path(tmp), wid="test"), gh=None)
            from contextlib import nullcontext

            from tauceti_worker import agents

            with (
                patch.object(overlaps, "require_shared"),
                patch.object(overlaps, "require_publication_rights"),
                patch.object(overlaps, "me", return_value="alice"),
                patch.object(overlaps, "state_for", return_value=state),
                patch.object(overlaps, "Lease", return_value=nullcontext(lease)),
                patch.object(overlaps, "sources", return_value=(Path(tmp), "t" * 40, "p" * 40)),
                patch.object(overlaps, "mathlib_history", return_value=Path(tmp)),
                patch.object(overlaps, "select_batch", return_value=["b" * 40]),
                patch.object(agents, "run_agent_proc", side_effect=AssertionError("repeated model call")),
            ):
                self.assertEqual(overlaps.do_scan(worker, None, None, None, False), 0)
            self.assertTrue(json.loads((work / "metrics.json").read_text())["publication_retried"])


class InterfaceTests(unittest.TestCase):
    def test_guarded_agent_preserves_exit_status_and_closed_stdin(self):
        from tauceti_worker import agents

        with tempfile.TemporaryDirectory() as tmp:
            for status in (0, 7):
                rc = agents.run_agent_proc(
                    [sys.executable, "-c", f"import sys; assert sys.stdin.read()==''; sys.exit({status})"],
                    env=dict(os.environ),
                    logdir=Path(tmp),
                    label=f"exit-{status}",
                    provider="test",
                    cancel_event=threading.Event(),
                )
                self.assertEqual(rc, status)

    def test_killed_worker_cannot_leave_detached_agent_or_tools_running(self):
        # Inside a PID namespace the agent's own pids are not host pids, so find the processes by a
        # unique argv token instead, and only ever signal what carries it.
        for policy in ("legacy", "auto"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmp:
                token = f"tauceti-overlap-test-{os.getpid()}-{policy}"
                ready = Path(tmp) / "ready"
                child = (
                    "import os,subprocess,sys,time; "
                    "tool=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',sys.argv[1]]); "
                    f"open({str(ready)!r},'w').write('up'); "
                    "time.sleep(30)"
                )
                worker = (
                    "import os,threading; from pathlib import Path; "
                    "from tauceti_worker.agents import run_agent_proc; "
                    f"run_agent_proc({[sys.executable, '-c', child, token]!r}, env=dict(os.environ), "
                    f"logdir=Path({tmp!r}), label='bound', provider='test', cancel_event=threading.Event())"
                )
                proc = subprocess.Popen(
                    [sys.executable, "-c", worker],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "TAUCETI_PROCESS_CONTAINMENT": policy},
                )

                def tagged(token=token):
                    found = []
                    for entry in Path("/proc").iterdir():
                        if entry.name.isdigit():
                            try:
                                args = (entry / "cmdline").read_bytes().split(b"\0")
                                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                            except OSError:
                                continue
                            if token.encode() in args and state != "Z":
                                found.append(int(entry.name))
                    return found

                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "agent never started")
                    self.assertGreaterEqual(len(tagged()), 2)
                    proc.kill()  # SIGKILL bypasses worker finally/atexit handlers.
                    proc.wait(timeout=3)
                    deadline = time.monotonic() + 4
                    while tagged() and time.monotonic() < deadline:
                        time.sleep(0.03)
                    self.assertFalse(tagged(), "detached agent/tool survived worker death")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)
                    for pid in tagged():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)

    def test_scan_loop_runs_with_rest_budget_when_graphql_is_exhausted(self):
        from tauceti_worker import loop

        args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
        for only, expected_launch in ((["overlap-scan"], True), (["overlap-fix"], False), ([], False)):
            with (
                patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": ""}),
                patch.object(loop, "api_mode", return_value=False),
                patch.object(loop, "choose_model", return_value=("codex", {})),
                patch.object(loop, "github_budget", return_value={"core": (5000, 0), "graphql": (0, 9999999999)}),
                patch.object(loop, "run_round_subprocess", side_effect=KeyboardInterrupt) as launch,
                patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt),
            ):
                loop.cmd_loop(args, SimpleNamespace(wid="test"), only=only, agent="codex")
                self.assertEqual(launch.called, expected_launch)

    def test_lost_lease_stops_agent_and_records_telemetry(self):
        from tauceti_worker import agents

        with tempfile.TemporaryDirectory() as tmp:
            event = threading.Event()
            timer = threading.Timer(0.15, event.set)
            timer.start()
            try:
                rc = agents.run_agent_proc(
                    [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                    env=dict(os.environ),
                    logdir=Path(tmp),
                    label="cancel",
                    provider="test",
                    cancel_event=event,
                )
            finally:
                timer.cancel()
            self.assertLess(rc, 0)
            telemetry = json.loads((Path(tmp) / "cancel-telemetry.json").read_text())
            self.assertEqual(telemetry["compactions"], 0)
            retained = list(Path(tmp).glob("cancel-20*-telemetry.json"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(json.loads(retained[0].read_text()), telemetry)

    def test_successful_bulk_read_does_not_run_rate_limit_regex(self):
        from tauceti_worker import github_transport as transport

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "test"}),
            patch.object(
                transport.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "pull request " * 50000, ""),
            ),
        ):
            started = time.monotonic()
            result = transport.execute(["gh", "api", "repos/a/b/pulls"])
            self.assertEqual(result.returncode, 0)
            self.assertLess(time.monotonic() - started, 2)

    def test_flags_keys_and_targeting(self):
        args = build_parser().parse_args(["work", "--only", "overlap-scan", "--overlap-batch-commits", "40"])
        self.assertEqual(args.overlap_batch_commits, 40)
        self.assertEqual(KIND_KEYS["8"], "lint-repair")
        self.assertEqual(KIND_KEYS["9"], "overlap-scan")
        self.assertEqual(KIND_KEYS["0"], "overlap-fix")
        for name in ("overlap-scan", "overlap-fix"):
            with self.assertRaises(SystemExit):
                raise_on_untargetable_tasks((123,), [name])

    def test_compaction_and_usage_allowlist(self):
        for provider, event in (
            ("claude", {"type": "system", "subtype": "compact_boundary", "thinking": "secret"}),
            ("codex", {"type": "context.compacted", "encrypted_content": "secret"}),
        ):
            renderer = AgentTranscriptRenderer(provider)
            self.assertNotIn("secret", renderer.render_line(json.dumps(event)))
            self.assertEqual(renderer.compactions, 1)
            renderer.render_line(
                json.dumps(
                    {
                        "type": "result" if provider == "claude" else "turn.completed",
                        "usage": {"input_tokens": 42, "output_tokens": 7, "secret": 99},
                    }
                )
            )
            self.assertEqual(renderer.usage, {"input_tokens": 42, "output_tokens": 7})

    def test_focused_scan_skips_pr_review_survey_and_dry_run_has_no_writes(self):
        sv = Survey(worker_id="test")
        sv.overlap_scan.actionable.append(Candidate(0, "a" * 40, "behind"))
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None, rs=None, counters=None)
        opts = SimpleNamespace(only=["overlap-scan"], dry_run=True, work_model="codex", sandbox_host=True, prs=())

        def fill(_cfg, _gh, actual, **_kw):
            actual.overlap_scan = sv.overlap_scan

        with (
            patch.object(overlaps, "survey_overlaps", side_effect=fill),
            patch.object(work_units, "survey", side_effect=AssertionError("unrelated survey")),
            patch.object(overlaps, "do_scan", side_effect=AssertionError("write")),
        ):
            self.assertEqual(work_units.run_round(worker, opts), 0)


if __name__ == "__main__":
    unittest.main()
