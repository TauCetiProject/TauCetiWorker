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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_clients as clients
from tauceti_worker import audit_pool as pool
from tauceti_worker import security_audit as sa
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

        def advance(repo, batch):
            with self.lock:
                if batch["from_sha"] != self.cursor:
                    raise NoProgress("security-audit: cursor moved concurrently; replan without reusing this verdict")
                self.cursor = batch["commits"][-1]["sha"]
                self.advanced.append((batch["from_sha"], self.cursor))
                return self.cursor

        def remote_sha(branch):
            return self.cursor if branch == sa.CURSOR else self.shas[-1]

        # Subscription pace by provider and the configured API key; tests flip these.
        self.pace = {"codex": True, "claude": True}
        self.key = None

        def resolve(cfg, provider, **kwargs):
            if not self.pace[provider]:
                raise NoProgress(f"no model under pace right now (agent={provider})")
            return provider

        patches = [
            patch.object(sa, "MAX_COMMITS", self.per_batch),
            patch.object(sa, "refresh"),
            patch.object(sa, "require_admin"),
            patch.object(sa, "api", return_value={"login": "admin"}),
            patch.object(sa, "advance", side_effect=advance),
            patch.object(sa, "dispatch_advanced"),
            patch.object(sa, "remote_sha", side_effect=remote_sha),
            patch("tauceti_worker.loop.resolve_work_model", side_effect=resolve),
            patch.object(sa, "anthropic_api_key", side_effect=lambda: self.key),
            patch.object(pool.Chooser, "TTL", 0),
            patch.object(pool, "notify_block"),
            patch.object(pool, "RETRY_SECONDS", 0),
            patch.object(pool.time, "sleep"),
            patch.object(clients, "selected_protocol", return_value=4),
            patch.object(sa, "reader_path", return_value=Path(__file__)),
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

    def serve(self, inspect, parallel=3, stop=None, pin=None):
        self.launches = getattr(self, "launches", [])

        def fake(batch, provider, attempt, *, protocol=None, claude_credential=None, **kwargs):
            with self.lock:
                self.calls.append(self.index(batch))
                self.launches.append((self.index(batch), provider, claude_credential, "resume_from" in kwargs))
            return inspect(batch, attempt)

        with patch.object(sa, "inspect", side_effect=fake):
            p = pool.Pool(self.state, parallel=parallel, chooser=pool.Chooser(None, pin=pin))
            p.serve(stop=stop or (lambda p: self.cursor == self.shas[-1] and not p.running))
        return p

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

        self.serve(inspect, stop=lambda p: False)
        self.assertEqual(self.advanced, [(self.shas[0], self.shas[2])])
        self.assertEqual(self.calls.count(1), 1)
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertEqual((blocked["from_sha"], blocked["to_sha"]), (self.shas[2], self.shas[4]))
        self.assertIn("unsafe IO", blocked["reason"])
        # A restarted pool keeps the block: it is not operational and cannot be superseded.
        self.serve(inspect, stop=lambda p: False)
        self.assertEqual(self.calls.count(1), 1)
        self.assertEqual(len(self.advanced), 1)

    def test_operational_failures_retry_fresh_twice_then_block(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                raise Die("security-audit: codex failed; see model-stderr.txt")
            return self.verdict(batch)

        self.serve(inspect, stop=lambda p: False)
        self.assertEqual(self.calls.count(0), 1 + pool.MAX_RETRIES)
        self.assertEqual(self.advanced, [])
        blocked = json.loads((self.state / "blocked.json").read_text())
        self.assertTrue(blocked["pool_retries_exhausted"])
        self.assertEqual(blocked["from_sha"], self.shas[0])

    def test_non_operational_failure_blocks_without_retry(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                raise Die("security-audit: reviewer attempted an unauthorized tool call")
            return self.verdict(batch)

        self.serve(inspect, stop=lambda p: False)
        self.assertEqual(self.calls.count(0), 1)
        self.assertEqual(self.advanced, [])

    def test_operational_block_with_native_judgment_is_not_retried(self):
        def inspect(batch, attempt):
            if self.index(batch) == 0:
                (attempt / "model-rollout-0.jsonl").write_text("{}\n")
                raise Die("security-audit: inspection unfinished; retained for same-session continuation")
            return self.verdict(batch)

        with patch(
            "tauceti_worker.audit_recovery.native_finals", return_value=[{"verdict": "finding", "findings": ["x"]}]
        ):
            self.serve(inspect, stop=lambda p: False)
        self.assertEqual(self.calls.count(0), 1)


class Providers(PoolFixture):
    def test_codex_under_pace_reviews_every_batch_and_ledger_names_the_reviewer(self):
        self.key = "sk-ant-fixture-secret"
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual({(prov, cred) for _, prov, cred, _ in self.launches}, {("codex", None)})
        reviews = [row for row in self.ledger() if "verdict" in row]
        self.assertEqual(len(reviews), 3)
        for row in reviews:
            self.assertEqual(
                (row["provider"], row["model"], row["credential"]), ("codex", "gpt-6-luna", "subscription")
            )
        self.assertNotIn("sk-ant-fixture-secret", (self.state / "ledger.jsonl").read_text())

    def test_over_pace_codex_falls_back_to_subscription_then_api_key_haiku(self):
        self.pace["codex"] = False
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual({(p, c) for _, p, c, _ in self.launches}, {("claude", "subscription")})
        self.launches.clear()
        self.cursor = self.shas[0]
        for path in (self.state / "pool").glob("*"):
            path.unlink()
        self.pace["claude"] = False
        self.key = "sk-ant-fixture-secret"
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual({(p, c) for _, p, c, _ in self.launches}, {("claude", "api-key")})
        rows = [row for row in self.ledger() if row.get("credential") == "api-key"]
        self.assertEqual({row["model"] for row in rows}, {"claude-haiku-5-5"})

    def test_nothing_launches_when_all_subscriptions_are_over_pace_and_no_key(self):
        self.pace = {"codex": False, "claude": False}
        passes = []
        self.serve(lambda batch, attempt: self.verdict(batch), stop=lambda p: passes.append(1) or len(passes) > 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.advanced, [])

    def test_a_batch_keeps_its_provider_and_claude_may_move_to_the_api_key(self):
        self.pace["codex"] = False
        failures = []

        def inspect(batch, attempt):
            if self.index(batch) == 0 and not failures:
                failures.append(1)
                # Codex comes back under pace and Claude's subscription goes over pace.
                self.pace.update(codex=True, claude=False)
                self.key = "sk-ant-fixture-secret"
                raise Die("security-audit: reviewer failed")
            return self.verdict(batch)

        self.serve(inspect, parallel=1)
        first = [(p, c) for i, p, c, _ in self.launches if i == 0]
        self.assertEqual(first, [("claude", "subscription"), ("claude", "api-key")])
        self.assertIn(("codex", None), {(p, c) for i, p, c, _ in self.launches if i > 0})

    def test_codex_retry_continues_the_retained_session_when_one_exists(self):
        stopped = []

        def inspect(batch, attempt):
            if self.index(batch) == 0 and not stopped:
                stopped.append(attempt)
                raise Die("security-audit: inspection unfinished; retained for same-session continuation")
            return self.verdict(batch)

        retained = []

        def fake_retained(work, previous):
            retained.append(previous["attempt"])
            batch = json.loads(Path(previous["attempt"], "input.json").read_text())
            return Path(previous["attempt"]), batch, {"paged": True}, False

        with (
            patch("tauceti_worker.audit_recovery.retained", side_effect=fake_retained),
            patch("tauceti_worker.audit_progress.continuable", return_value=True),
        ):
            self.serve(inspect)
        self.assertEqual(retained, [str(stopped[0])])
        first = [(p, resumed) for i, p, _, resumed in self.launches if i == 0]
        self.assertEqual(first, [("codex", False), ("codex", True)])

    def test_haiku_retry_is_refused_when_the_stopped_transcript_holds_a_finding(self):
        self.pace["codex"] = False

        def inspect(batch, attempt):
            if self.index(batch) == 0:
                (attempt / "execution.json").write_text(sa.serialize({"argv": ["claude", "-p"]}))
                use = {
                    "type": "tool_use",
                    "name": "StructuredOutput",
                    "input": {"verdict": "finding", "findings": ["x"]},
                }
                (attempt / "model-transcript.jsonl").write_text(
                    sa.serialize({"type": "assistant", "message": {"content": [use]}}) + "\n"
                )
                raise Die("security-audit: reviewer failed")
            return self.verdict(batch)

        self.serve(inspect, stop=lambda p: not p.running and any(s.status == "blocked" for s in p.slots))
        self.assertEqual([p for i, p, _, _ in self.launches if i == 0], ["claude"])
        self.assertEqual(self.advanced, [])
        self.assertTrue((self.state / "blocked.json").exists())

    def test_pinned_codex_waits_rather_than_using_haiku(self):
        self.pace["codex"] = False
        self.key = "sk-ant-fixture-secret"
        passes = []
        self.serve(
            lambda batch, attempt: self.verdict(batch), pin="codex", stop=lambda p: passes.append(1) or len(passes) > 3
        )
        self.assertEqual(self.calls, [])


class Restart(PoolFixture):
    def persisted(self, *, tamper=False):
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-1])
        if tamper:
            batch["commits"][0]["files"][0]["after"] += "-- edited\n"
            body = {k: v for k, v in batch.items() if k != "input_digest"}
            batch["input_digest"] = sa.hashlib.sha256(sa.serialize(body).encode()).hexdigest()
        p = pool.Pool(self.state, parallel=3, chooser=pool.Chooser(None))
        slot = pool.Slot(batch, status="pass", verdict=self.verdict(batch), provider="codex", credential="subscription")
        p.persist(slot)

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

    def test_operational_worker_block_at_cursor_is_superseded(self):
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
        self.serve(lambda batch, attempt: self.verdict(batch))
        self.assertEqual(len(self.advanced), 3)
        self.assertTrue(list(self.state.glob("superseded-by-pool-block-*.json")))

    def test_worker_codex_block_continues_in_its_session_even_over_pace(self):
        # The Worker's batch was planned against an older main, so its input differs
        # from what the pool would plan now; it must still be continued, on Codex.
        batch = sa.plan_batch(self.repo, self.shas[0], self.shas[-2])
        attempt = self.state / "attempt-1"
        attempt.mkdir()
        (attempt / "input.json").write_text(sa.serialize(batch))
        block = {
            "from_sha": self.shas[0],
            "to_sha": batch["commits"][-1]["sha"],
            "reason": "security-audit: inspection unfinished; retained for same-session continuation",
            "attempt": str(attempt),
        }
        (self.state / "blocked.json").write_text(sa.serialize(block))
        self.pace["codex"] = False
        self.key = "sk-ant-fixture-secret"
        retained = []

        def fake_retained(work, previous):
            retained.append(previous["attempt"])
            return Path(previous["attempt"]), batch, {"argv": ["codex", "exec"], "paged": True}, False

        with (
            patch("tauceti_worker.audit_recovery.retained", side_effect=fake_retained),
            patch("tauceti_worker.audit_progress.continuable", return_value=True),
        ):
            self.serve(lambda b, a: self.verdict(b))
        self.assertIn(str(attempt), retained)
        head = [(p, resumed) for i, p, _, resumed in self.launches if i == 0]
        self.assertEqual(head, [("codex", True)])
        self.assertTrue(all(p == "claude" for i, p, _, _ in self.launches if i != 0))
        self.assertEqual(len(self.advanced), 3)


if __name__ == "__main__":
    unittest.main()
