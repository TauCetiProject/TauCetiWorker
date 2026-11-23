#!/usr/bin/env python3
"""Parallel isolated audit batches advance the cursor strictly in order."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_pool as pool
from tauceti_worker import security_audit as sa
from tauceti_worker.audit_evidence import NotJudged, Unreadable
from tauceti_worker.config import Die, NoProgress


class PoolFixture(unittest.TestCase):
    commits = 6
    per_batch = 2

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "src"
        self.root.mkdir()
        self.repo = self.root / ".git"
        self.git("init", "-q", "-b", "main")
        self.shas = [self.commit(i) for i in range(self.commits + 1)]
        self.cursor = self.shas[0]
        self.state = Path(self.tmp.name) / "worker" / "security-audit"
        self.state.mkdir(parents=True)
        self.advanced = []
        self.lock = threading.Lock()
        self.calls = []
        self.calls_from = []

        def advance(repo, batch):
            with self.lock:
                if batch["from_sha"] != self.cursor:
                    raise NoProgress("security-audit: cursor moved concurrently; replan without reusing this verdict")
                self.cursor = batch["commits"][-1]["sha"]
                self.advanced.append((batch["from_sha"], self.cursor))
                return self.cursor

        def remote_sha(branch):
            return self.cursor if branch == sa.CURSOR else self.shas[-1]

        # The configured reviewer API key; tests flip it.
        self.key = "sk-ant-fixture-secret"

        patches = [
            patch.object(sa, "MAX_COMMITS", self.per_batch),
            patch.object(sa, "refresh"),
            patch.object(sa, "require_admin"),
            patch.object(sa, "api", return_value={"login": "admin"}),
            patch.object(sa, "admin_login", return_value="admin"),
            patch.object(sa, "advance", side_effect=advance),
            patch.object(sa, "dispatch_advanced"),
            patch.object(sa, "remote_sha", side_effect=remote_sha),
            patch.object(sa, "anthropic_api_key", side_effect=lambda: self.key),
            patch.object(pool, "notify_block"),
            patch.object(pool, "RETRY_SECONDS", 0),
            patch.object(pool.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # objects.git is the bare store in production; the fixture checkout's .git serves the same role.
        (self.state / "objects.git").symlink_to(self.repo)

    def git(self, *args):
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        return subprocess.check_output(["git", "-C", str(self.root), *args], env=env).decode().strip()

    def commit(self, i):
        path = self.root / "TauCeti" / "A.lean"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"theorem a{i} : True := trivial\n")
        self.git("add", "--", "TauCeti/A.lean")
        self.git("commit", "-qm", f"c{i}")
        return self.git("rev-parse", "HEAD")

    def verdict(self, batch, value="pass", findings=()):
        return {
            "input_digest": batch["input_digest"],
            "commits": [c["sha"] for c in batch["commits"]],
            "verdict": value,
            "findings": list(findings),
        }

    def index(self, batch):
        return self.shas.index(batch["from_sha"]) // self.per_batch

    def serve(self, inspect, parallel=3, stop=None):
        self.launches = getattr(self, "launches", [])

        def fake(batch, attempt):
            with self.lock:
                self.calls.append(self.index(batch))
                self.calls_from.append(batch["from_sha"])
                self.launches.append(self.index(batch))
            return inspect(batch, attempt)

        stop = stop or self.settled
        passes = [0]

        def bounded(p):
            # A pool that never settles is a failing test, never an unbounded run filling the disk.
            passes[0] += 1
            if passes[0] > 400:
                raise AssertionError("the pool did not settle")
            return stop(p)

        with patch.object(sa, "inspect", side_effect=fake):
            p = pool.Pool(self.state, parallel=parallel)
            p.serve(stop=bounded)
        return p

    def not_judged(self, attempt, *, engaged=True):
        """An attempt whose reviewer ran and returned no judgment; `engaged` if it answered at all."""
        (attempt / "execution.json").write_text(sa.serialize({"protocol_version": sa.PROTOCOL}))
        if engaged:
            (attempt / "response.json").write_text(sa.serialize({"content": [], "stop_reason": "max_tokens"}))
        raise NotJudged("security-audit: reviewer stopped with max_tokens")

    def settled(self, p):
        """The pool no longer exits at a block: stop once nothing can run or advance."""
        if p.running:
            return False
        if self.cursor == self.shas[-1]:
            return True
        if not p.slots or any(s.status == "planned" for s in p.slots):
            return False
        return p.slots[0].status == "blocked" or all(s.status in ("pass", "blocked") for s in p.slots)

    def ledger(self):
        path = self.state / "ledger.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Ordering(PoolFixture):
    def test_later_batches_finishing_first_still_advance_in_chain_order(self):
        released = {i: threading.Event() for i in range(3)}

        def inspect(batch, attempt):
            i = self.index(batch)
            if i < 2:
                self.assertTrue(released[i].wait(10))
            released[i - 1 if i else 0].set()
            return self.verdict(batch)

        # Batch 2 finishes first and releases 1, which releases 0.
        self.serve(inspect)
        self.assertEqual(
            self.advanced, [(self.shas[0], self.shas[2]), (self.shas[2], self.shas[4]), (self.shas[4], self.shas[6])]
        )
        self.assertEqual(sorted(self.calls), [0, 1, 2])
        self.assertFalse((self.state / "blocked.json").exists())

    def test_finding_is_binding_and_never_reinspected(self):
        def inspect(batch, attempt):
            if self.index(batch) == 1:
                return self.verdict(batch, "finding", ["unsafe IO in A.lean"])
            return self.verdict(batch)

        self.serve(inspect, stop=self.settled)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])
        self.assertEqual(self.calls.count(1), 1)
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertEqual((blocked["from_sha"], blocked["to_sha"]), (self.shas[2], self.shas[4]))
        self.assertIn("unsafe IO", blocked["reason"])
        # A restarted pool keeps the block: it is not operational and cannot be superseded.
        self.serve(inspect, stop=self.settled)
        self.assertEqual(self.calls.count(1), 1)
        self.assertEqual(len(self.advanced), 1)

    def test_a_range_with_no_judgment_is_split_into_complete_halves(self):
        def inspect(batch, attempt):
            if batch["from_sha"] == self.shas[0] and len(batch["commits"]) > 1:
                self.not_judged(attempt)
            return self.verdict(batch)

        self.serve(inspect)
        splits = [r for r in self.ledger() if "split" in r]
        self.assertEqual([(r["from_sha"], r["to_sha"]) for r in splits], [(self.shas[0], self.shas[2])])
        self.assertEqual(
            self.advanced,
            [
                (self.shas[0], self.shas[1]),
                (self.shas[1], self.shas[2]),
                (self.shas[2], self.shas[4]),
                (self.shas[4], self.shas[6]),
            ],
        )
        self.assertFalse((self.state / "blocked.json").exists())

    def test_a_single_commit_with_no_judgment_is_retried_afresh(self):
        seen = []

        def inspect(batch, attempt):
            seen.append((batch["from_sha"], len(batch["commits"])))
            if batch["from_sha"] == self.shas[0] and len(seen) < 4:
                self.not_judged(attempt)
            return self.verdict(batch)

        self.serve(inspect, parallel=1)
        left = [n for frm, n in seen if frm == self.shas[0]]
        # The whole range, then its left half, then the half's fresh retry.
        self.assertEqual(left[:3], [2, 1, 1])
        self.assertEqual(len(self.advanced), 4)
        self.assertFalse((self.state / "blocked.json").exists())

    def test_a_range_retried_many_times_keeps_only_its_last_attempts(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0 and self.calls.count(0) <= 5:
                self.not_judged(attempt, engaged=False)
            return self.verdict(batch)

        self.serve(inspect)
        self.assertEqual(self.calls.count(0), 6)
        attempts = sorted(self.state.glob("attempt-*"))
        self.assertLessEqual(len(attempts), 2 + 2 + 2)  # Range 0's last two, plus the two other ranges.
        self.assertEqual(sum(1 for r in self.ledger() if r.get("outcome") == "not-judged"), 5)

    def test_a_launch_where_no_reviewer_engaged_retries_the_same_range(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0 and self.calls.count(0) == 1:
                self.not_judged(attempt, engaged=False)
            return self.verdict(batch)

        self.serve(inspect)
        self.assertEqual(self.calls.count(0), 2)
        self.assertFalse([r for r in self.ledger() if "split" in r])
        self.assertEqual(len(self.advanced), 3)

    def test_held_block_alert_is_offered_again_on_later_passes(self):
        # A failed DM is retried from its receipts; the pool must keep offering the block.
        def inspect(batch, attempt):
            if self.index(batch) == 1:
                return self.verdict(batch, "finding", ["unsafe IO in A.lean"])
            return self.verdict(batch)

        passes = {"n": 0}

        def stop(p):
            if not self.settled(p):
                return False
            passes["n"] += 1
            return passes["n"] >= 3

        self.serve(inspect, stop=stop)
        offered = [c.args[1] for c in pool.notify_block.call_args_list]
        blocked = [b for b in offered if b.get("from_sha") == self.shas[2]]
        self.assertGreaterEqual(len(blocked), 3)
        self.assertTrue(all("unsafe IO" in b["reason"] for b in blocked))

    def test_every_pass_feeds_the_cursor_watchdog_after_advancing(self):
        seen = []

        def check(work, observe, *, min_commits):
            seen.append(observe())

        with patch.object(pool.audit_watchdog, "check", side_effect=check):
            self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertTrue(seen)
        self.assertTrue(all(isinstance(c, str) and isinstance(n, int) for c, n in seen))
        # The last pass reports the cursor it advanced to, never the one it started from.
        self.assertEqual(seen[-1], (self.shas[-1], 0))

    def test_a_pass_failing_with_any_exception_still_reaches_the_watchdog(self):
        errors = []

        def check(work, observe, *, min_commits):
            try:
                observe()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def failing(branch):
            raise subprocess.TimeoutExpired(["git", "ls-remote"], 1)

        with (
            patch.object(pool.audit_watchdog, "check", side_effect=check),
            patch.object(sa, "remote_sha", side_effect=failing),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], subprocess.TimeoutExpired)

    def test_unreadable_evidence_blocks_without_retry(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                raise Unreadable("security-audit: unreadable evidence: reviewer text cannot be read")
            return self.verdict(batch)

        self.serve(inspect, stop=self.settled)
        self.assertEqual(self.calls.count(0), 1)
        self.assertEqual(self.advanced, [])

    def test_failed_attempt_with_a_recorded_finding_binds_and_is_not_retried(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                text = sa.serialize({"verdict": "finding", "findings": ["x"]})
                response = {"content": [{"type": "text", "text": text}], "stop_reason": "max_tokens"}
                (attempt / "response.json").write_text(sa.serialize(response))
                return sa.judged(attempt, batch, "security-audit: reviewer stopped with max_tokens")
            return self.verdict(batch)

        self.serve(inspect, stop=self.settled)
        self.assertEqual(self.calls.count(0), 1)
        self.assertEqual(self.advanced, [])
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertEqual(blocked["reason"], "security-audit: x")


class Restart(PoolFixture):
    def persisted(self, *, tamper=False):
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-1])
        if tamper:
            batch["commits"][0]["files"][0]["after"] += "-- edited\n"
            body = {k: v for k, v in batch.items() if k != "input_digest"}
            batch["input_digest"] = sa.hashlib.sha256(sa.serialize(body).encode()).hexdigest()
        p = pool.Pool(self.state, parallel=3)
        p.slots = [pool.Slot(batch, status="pass", verdict=self.verdict(batch), protocol=5)]
        p.persist()

    def test_identical_persisted_pass_is_reused(self):
        self.persisted()
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertNotIn(0, self.calls)
        self.assertEqual(len(self.advanced), 3)

    def test_changed_persisted_input_is_inspected_again(self):
        self.persisted(tamper=True)
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertIn(0, self.calls)
        self.assertEqual(len(self.advanced), 3)


class ExternalCursor(PoolFixture):
    def test_cursor_moved_outside_the_window_replans(self):
        started = threading.Event()
        release = threading.Event()
        moved = []

        def inspect(batch, attempt):
            if not moved:
                started.set()
                self.assertTrue(release.wait(10))
            return self.verdict(batch)

        def mover():
            self.assertTrue(started.wait(10))
            with self.lock:
                self.cursor = self.shas[3]  # Mid-batch: no planned batch starts here.
                moved.append(True)
            release.set()

        threading.Thread(target=mover, daemon=True).start()
        self.serve(inspect)
        self.assertTrue(all(frm != self.shas[0] for frm, _ in self.advanced))
        self.assertEqual(self.advanced[0][0], self.shas[3])
        self.assertEqual(self.cursor, self.shas[-1])


class Ownership(PoolFixture):
    def test_worker_rounds_defer_while_a_pool_holds_the_lock(self):
        import fcntl

        self.assertFalse(sa.pool_active(self.state))
        with (self.state / "pool.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertTrue(sa.pool_active(self.state))
            with self.assertRaises(sa.AuditNotDue):
                sa.run(type("C", (), {"state": self.state.parent})(), None)
        self.assertFalse(sa.pool_active(self.state))

    def test_a_block_file_without_recorded_evidence_does_not_hold_the_cursor(self):
        (self.state / "blocked.json").write_text(
            sa.serialize(
                {
                    "from_sha": self.shas[0],
                    "to_sha": self.shas[2],
                    "reason": "security-audit: inspection unfinished; retained for same-session continuation",
                    "attempt": str(self.state / "attempt-1"),
                }
            )
        )
        # blocked.json only mirrors a block; binding comes from the ranges and the ledger.
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual(len(self.advanced), 3)


class BindingJudgments(PoolFixture):
    def test_incomplete_with_a_concern_binds_without_another_review(self):
        def inspect(batch, attempt):
            if self.index(batch) == 1:
                return self.verdict(batch, "incomplete", ["origin of `stacks` is uncertain"])
            return self.verdict(batch)

        self.serve(inspect)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])
        self.assertEqual([i for i in self.launches if i == 1], [1])
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertIn("stacks", blocked["reason"])

    def test_recorded_binding_verdict_is_never_inspected_again_even_if_pool_state_is_lost(self):

        def first(batch, attempt):
            if self.index(batch) == 0:
                return self.verdict(batch, "finding", ["concern"])
            return self.verdict(batch)

        self.serve(first)
        for path in (self.state / "pool").glob("*"):
            path.unlink()  # Lose the pool's persisted window; the ledger still binds.
        (self.state / "blocked.json").unlink(missing_ok=True)
        self.launches.clear()
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual([i for i in self.launches if i == 0], [])
        self.assertEqual(self.advanced, [])

    def test_nothing_launches_without_an_api_key(self):
        self.key = None
        passes = []
        self.serve(lambda batch, attempt: self.verdict(batch), stop=lambda p: passes.append(1) or len(passes) > 3)
        self.assertEqual(self.calls, [])


class Supersession(PoolFixture):
    """Concerns from an earlier judgment protocol are inspected afresh, once, by the current one."""

    def test_an_earlier_protocol_block_and_its_ledger_concern_are_inspected_afresh(self):
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-1])
        verdict = self.verdict(batch, "finding", ["cannot confirm `stacks` is Mathlib's attribute"])
        sa.record(
            self.state / "ledger.jsonl", {"protocol_version": 5, "batch": batch["input_digest"], "verdict": verdict}
        )
        p = pool.Pool(self.state, parallel=3)
        p.slots = [pool.Slot(batch, status="blocked", verdict=verdict, attempts=["/gone"], protocol=5)]
        p.persist()
        self.serve(lambda b, a: self.verdict(b))
        self.assertIn(0, self.calls)
        self.assertEqual(len(self.advanced), 3)

    def test_a_current_protocol_block_stays_binding_across_a_restart(self):
        def first(batch, attempt):
            if self.index(batch) == 0:
                return self.verdict(batch, "finding", ["IO"])
            return self.verdict(batch)

        self.serve(first)
        self.calls.clear()
        self.serve(lambda b, a: self.verdict(b))
        self.assertNotIn(0, self.calls)
        self.assertEqual(self.advanced, [])
        saved = json.loads((self.state / "pool" / pool.RANGES).read_text())["ranges"]
        self.assertEqual((saved[0]["status"], saved[0]["protocol_version"]), ("blocked", sa.PROTOCOL))

    def test_an_earlier_protocol_range_left_running_is_inspected_afresh(self):
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-1])
        attempt = self.state / "attempt-legacy"
        attempt.mkdir()
        (attempt / "input.json").write_text(sa.serialize(batch))
        p = pool.Pool(self.state, parallel=3)
        p.slots = [pool.Slot(batch, status="running", attempts=[str(attempt)], protocol=5)]
        p.persist()
        self.serve(lambda b, a: self.verdict(b))
        self.assertIn(0, self.calls)
        self.assertEqual(len(self.advanced), 3)


class OwnJournal(PoolFixture):
    def test_a_running_launch_journal_at_the_cursor_does_not_idle_the_pool(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                # Launches journal their attempt in blocked.json while they run.
                (self.state / "blocked.json").write_text(
                    sa.serialize(
                        {
                            "from_sha": batch["from_sha"],
                            "to_sha": batch["commits"][-1]["sha"],
                            "attempt": str(attempt),
                            "reason": "security-audit: operational recovery interrupted",
                        }
                    )
                )
            return self.verdict(batch)

        self.serve(inspect)
        self.assertEqual(len(self.advanced), 3)


class DurableRanges(PoolFixture):
    """The ordered list of active ranges survives a restart, including ranges that were running."""

    def interrupted(self, evidence=()):
        """Persist a window whose head was running when the pool stopped, with this evidence."""
        p = pool.Pool(self.state, parallel=3)
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-1])
        attempt = self.state / "attempt-1"
        attempt.mkdir()
        (attempt / "input.json").write_text(sa.serialize(batch))
        if evidence:
            (attempt / "execution.json").write_text(sa.serialize({"protocol_version": sa.PROTOCOL}))
            content = [{"type": "text", "text": sa.serialize(value)} for value in evidence]
            (attempt / "response.json").write_text(sa.serialize({"content": content, "stop_reason": "end_turn"}))
        p.slots = [pool.Slot(batch, status="running", attempts=[str(attempt)], protocol=sa.PROTOCOL)]
        p.persist()
        return batch

    def test_a_finding_in_an_interrupted_attempt_binds_after_restart(self):
        batch = self.interrupted([{"verdict": "finding", "findings": ["IO"]}])
        self.serve(lambda b, a: self.verdict(b), stop=lambda p: not p.running and p.slots[0].status == "blocked")
        self.assertNotIn(0, self.calls)
        self.assertEqual(self.advanced, [])
        rows = [r for r in self.ledger() if r.get("batch") == batch["input_digest"] and "verdict" in r]
        self.assertEqual([r["verdict"]["findings"] for r in rows], [["IO"]])
        self.assertIn("IO", json.loads((self.state / "blocked.json").read_text())["reason"])

    def test_an_interrupted_attempt_with_an_unreadable_input_is_still_classified(self):
        self.interrupted([{"verdict": "finding", "findings": ["IO"]}])
        (self.state / "attempt-1" / "input.json").write_text("{damaged")
        self.serve(lambda b, a: self.verdict(b), stop=lambda p: not p.running and p.slots[0].status == "blocked")
        self.assertNotIn(0, self.calls)
        self.assertIn("IO", json.loads((self.state / "blocked.json").read_text())["reason"])

    def test_an_attestation_inside_a_range_keeps_later_and_remaining_binding_ranges(self):
        p = pool.Pool(self.state, parallel=3)
        head = sa.build_batch(self.repo, self.shas[0], self.shas[-1], self.shas[1:3])
        later = sa.build_batch(self.repo, self.shas[2], self.shas[-1], self.shas[3:5])
        block = {"from_sha": self.shas[2], "to_sha": self.shas[4], "reason": "security-audit: IO", "outcome": "finding"}
        p.slots = [
            pool.Slot(
                head,
                status="blocked",
                failure={**block, "from_sha": self.shas[0], "to_sha": self.shas[2]},
                protocol=sa.PROTOCOL,
            ),
            pool.Slot(later, status="blocked", failure=block, protocol=sa.PROTOCOL),
        ]
        p.persist()
        self.cursor = self.shas[1]  # A human attested the first commit only.
        self.serve(lambda b, a: self.verdict(b), stop=lambda p: not p.running and len(p.slots) >= 3)
        # Both binding ranges hold, and nothing is re-planned over them; only the commits after them are new.
        self.assertEqual([self.shas.index(c) for c in self.calls_from], [4])
        saved = json.loads((self.state / "pool" / pool.RANGES).read_text())["ranges"]
        self.assertEqual(
            [(r["from_sha"], r["commits"], r["status"]) for r in saved[:2]],
            [
                (self.shas[1], self.shas[2:3], "blocked"),
                (self.shas[2], self.shas[3:5], "blocked"),
            ],
        )

    def test_a_passed_range_the_cursor_entered_is_inspected_again_for_its_rest(self):
        p = pool.Pool(self.state, parallel=3)
        head = sa.build_batch(self.repo, self.shas[0], self.shas[-1], self.shas[1:3])
        p.slots = [pool.Slot(head, status="pass", verdict=self.verdict(head), protocol=sa.PROTOCOL)]
        p.persist()
        self.cursor = self.shas[1]
        self.serve(lambda b, a: self.verdict(b))
        self.assertEqual(self.advanced[0], (self.shas[1], self.shas[2]))
        self.assertEqual(len(self.launches), 3)  # The rest of the entered range, then the two after it.

    def test_an_interrupted_attempt_without_a_concern_is_retried(self):
        self.interrupted()
        self.serve(lambda b, a: self.verdict(b))
        self.assertEqual(self.calls.count(0), 1)
        self.assertEqual(len(self.advanced), 3)
        self.assertTrue(any("interrupted when the pool stopped" in r.get("reason", "") for r in self.ledger()))

    def test_a_recorded_concern_binds_its_commits_whatever_the_new_plan(self):
        # A legacy block from another plan of these commits (here, against an older main).
        old = sa.build_batch(self.repo, self.shas[2], self.shas[-2], self.shas[3:5])
        sa.record(
            self.state / "ledger.jsonl",
            {
                "protocol_version": sa.PROTOCOL,
                "batch": old["input_digest"],
                "verdict": self.verdict(old, "finding", ["IO"]),
            },
        )
        self.serve(lambda b, a: self.verdict(b), stop=self.settled)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])
        self.assertNotIn(self.shas[2], self.calls_from)
        self.assertIn("binding judgment", json.loads((self.state / "blocked.json").read_text())["reason"])

    def test_an_unreadable_hold_binds_its_commits_whatever_the_new_plan(self):
        old = sa.build_batch(self.repo, self.shas[2], self.shas[-2], self.shas[3:5])
        sa.record(
            self.state / "ledger.jsonl",
            {
                "protocol_version": sa.PROTOCOL,
                "outcome": "unreadable",
                "batch": old["input_digest"],
                "commits": self.shas[3:5],
                "reason": "x",
            },
        )
        self.serve(lambda b, a: self.verdict(b), stop=self.settled)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])
        self.assertNotIn(self.shas[2], self.calls_from)

    def test_retry_state_and_reviewer_survive_a_restart(self):
        def first(batch, attempt):
            if self.index(batch) == 0:
                self.not_judged(attempt, engaged=False)
            return self.verdict(batch)

        with patch.object(pool, "RETRY_SECONDS", 1000):
            self.serve(first, stop=lambda p: not p.running and p.slots[0].failures == 1)
        path = self.state / "pool" / pool.RANGES
        saved = json.loads(path.read_text())["ranges"]
        self.assertEqual([r["status"] for r in saved], ["planned", "pass", "pass"])
        self.assertEqual((saved[0]["failures"], saved[0]["protocol_version"]), (1, sa.PROTOCOL))
        self.assertGreater(saved[0]["retry_at"], 0)
        saved[0]["retry_at"] = 0  # The persisted backoff has elapsed.
        path.write_text(json.dumps({"ranges": saved}))
        self.launches.clear()
        self.calls.clear()
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual(self.calls, [0])  # Only the unfinished range is inspected again.
        self.assertEqual(len(self.advanced), 3)
        self.assertEqual(json.loads((self.state / "pool" / pool.RANGES).read_text())["ranges"], [])

    def test_unreadable_ranges_hold_rather_than_replan(self):
        (self.state / "pool").mkdir()
        (self.state / "pool" / pool.RANGES).write_text("{not json")
        p = pool.Pool(self.state, parallel=1)
        with self.assertRaisesRegex(NoProgress, "cannot be restored"):
            p.sync(self.shas[0], self.shas[-1])
        self.assertEqual(p.slots, [])

    def test_build_batch_accepts_only_the_next_consecutive_commits(self):
        whole = sa.build_batch(self.repo, self.shas[0], self.shas[-1], self.shas[1:3])
        self.assertEqual(whole, sa.plan_batch(self.repo, self.shas[0], self.shas[-1]))
        right = sa.build_batch(self.repo, self.shas[1], self.shas[-1], self.shas[2:3])
        self.assertEqual(right["commits"], whole["commits"][1:])
        for start, shas in ((self.shas[0], []), (self.shas[0], self.shas[2:3]), (self.shas[1], self.shas[1:2])):
            with self.subTest(shas=shas), self.assertRaises(Die):
                sa.build_batch(self.repo, start, self.shas[-1], shas)


class RestoredPass(PoolFixture):
    def test_a_restored_pass_does_not_advance_over_a_hold_recorded_after_it(self):
        batch = sa.build_batch(self.repo, self.shas[0], self.shas[-1], self.shas[1:3])
        p = pool.Pool(self.state, parallel=1)
        p.slots = [pool.Slot(batch, status="pass", verdict=self.verdict(batch), protocol=sa.PROTOCOL)]
        p.persist()
        sa.record(
            self.state / "ledger.jsonl",
            {"protocol_version": sa.PROTOCOL, "outcome": "unreadable", "commits": self.shas[1:2], "reason": "x"},
        )
        self.serve(lambda b, a: self.verdict(b), stop=self.settled)
        self.assertEqual(self.advanced, [])


class WorkerRounds(PoolFixture):
    """A Worker round runs one bounded quantum of the same scheduler, with its round's reviewer."""

    def round(self, inspect, **kwargs):
        def fake(batch, attempt):
            self.calls.append(self.index(batch))
            self.launches.append(self.index(batch))
            return inspect(batch, attempt)

        self.launches = getattr(self, "launches", [])
        cfg = SimpleNamespace(state=self.state.parent)
        opts = SimpleNamespace(dry_run=False, work_model="codex")
        with patch.object(sa, "inspect", side_effect=fake), patch("tauceti_worker.quota.Quota") as quota:
            try:
                return sa.run(cfg, opts, **kwargs)
            finally:
                quota.assert_not_called()

    def test_each_round_inspects_at_most_one_range_and_advances_it(self):
        for expected in (1, 2, 3):
            self.assertEqual(self.round(lambda b, a: self.verdict(b)), 0)
            self.assertEqual(len(self.advanced), expected)
            self.assertEqual(len(self.calls), expected)
        with self.assertRaises(sa.AuditNotDue):
            self.round(lambda b, a: self.verdict(b))
        self.assertEqual(self.launches, [0, 1, 2])

    def test_the_admission_threshold_applies_only_to_new_work(self):
        with self.assertRaises(sa.AuditNotDue):
            self.round(lambda b, a: self.verdict(b), min_commits=7)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.round(lambda b, a: self.verdict(b), min_commits=6), 0)
        # The window is admitted: its remaining ranges continue below the threshold.
        self.assertEqual(self.round(lambda b, a: self.verdict(b), min_commits=6), 0)
        self.assertEqual(len(self.advanced), 2)

    def test_no_judgment_splits_and_waiting_yields_to_maintenance(self):
        def inspect(batch, attempt):
            if batch["from_sha"] == self.shas[0]:
                self.not_judged(attempt)
            return self.verdict(batch)

        with patch.object(pool, "RETRY_SECONDS", 1000):
            self.assertEqual(self.round(inspect), 0)  # The whole range: not judged, split.
            self.assertEqual(self.round(inspect), 0)  # Its left half: not judged, retried later.
            for _ in range(3):  # Meanwhile the ranges ahead are inspected; advancement waits in order.
                self.assertEqual(self.round(inspect), 0)
            with self.assertRaises(NoProgress):
                self.round(inspect)  # Only the left half's backoff remains: wait.
        self.assertEqual(self.advanced, [])
        self.assertEqual(len(self.calls), 5)
        self.assertFalse((self.state / "blocked.json").exists())

    def test_a_legacy_block_without_a_concern_is_inspected_afresh(self):
        block = {
            "from_sha": self.shas[0],
            "to_sha": self.shas[2],
            "attempt": str(self.state / "attempt-gone"),
            "reason": "model failed",
        }
        (self.state / "blocked.json").write_text(sa.serialize(block))
        self.assertEqual(self.round(lambda b, a: self.verdict(b)), 0)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])

    def test_a_planning_failure_holds_and_reports(self):
        with patch.object(sa, "plan_batch", side_effect=Die("oversized")), self.assertRaises(NoProgress):
            self.round(lambda b, a: self.verdict(b))
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertEqual((blocked["from_sha"], blocked["manual_inspection_required"]), (self.shas[0], True))

    def test_rounds_record_the_current_protocol_and_reviewer(self):
        self.assertEqual(self.round(lambda b, a: self.verdict(b)), 0)
        row = next(r for r in self.ledger() if "verdict" in r)
        self.assertEqual((row["protocol_version"], row["model"]), (sa.PROTOCOL, sa.REVIEWER_MODEL))
        self.assertEqual(row["policy_digest"], sa.hashlib.sha256(sa.POLICY.encode()).hexdigest())


class Lookahead(PoolFixture):
    commits = 12

    def test_block_at_cursor_holds_advancement_while_inspection_continues_ahead(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                return self.verdict(batch, "finding", ["concern"])
            return self.verdict(batch)

        self.serve(inspect, parallel=2)
        self.assertEqual(self.advanced, [])
        inspected = set(self.launches)
        self.assertEqual(inspected, set(range(self.commits // self.per_batch)))
        # A human attestation moves the cursor past the block; persisted passes then advance.
        self.cursor = self.shas[self.per_batch]
        self.launches.clear()
        self.serve(lambda batch, attempt: self.verdict(batch), parallel=2)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.cursor, self.shas[-1])


if __name__ == "__main__":
    unittest.main()
