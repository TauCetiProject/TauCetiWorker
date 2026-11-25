#!/usr/bin/env python3
"""Direct GitHub reads during a state-service fallback share one paced host budget.

On 26 Nov 2026 the state service was down for four hours. Every worker fell back to direct
reads under one account, spent an hour's GraphQL quota about two minutes after each reset and
kept sending pull request views at zero remaining. These cases pin the host-wide token bucket,
the zero-remaining check for opaque commands and the spread of wakeups after a reset.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures.review_runtime_stub import install_runtime_stub

install_runtime_stub()

from tauceti_worker import github_state
from tauceti_worker import github_transport as tr


class Clock:
    def __init__(self, now=1_800_000_000.0, *, advance=True):
        self.now, self.advance, self.sleeps = now, advance, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.advance:
            self.now += seconds


class FallbackPacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.log = root / "sent"
        fake = root / "fake-gh"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        fake.chmod(0o700)
        env = {
            "GH_TOKEN": "fake-private-token",
            "TAUCETI_GITHUB_SHARED_DIR": str(root / "shared"),
            "TAUCETI_WORKER_ID": "test",
            "TAUCETI_REAL_GH": str(fake),
            "TAUCETI_GH_RESET_JITTER": "0",
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("TAUCETI_GH_FALLBACK_SHARE", "TAUCETI_GH_PROXY_WAIT", "TAUCETI_GITHUB_EPHEMERAL"):
            os.environ.pop(name, None)

    def run_gh(self, clock, *args, wait=False):
        with patch.object(tr.time, "time", clock.time), patch.object(tr.time, "sleep", clock.sleep):
            return tr._execute(["gh", *args], wait=wait)

    def sent(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def exhaust(self, clock, resource, remaining=0, reset_in=600):
        with patch.object(tr.time, "time", clock.time):
            tr.remember_quota(
                {"resource": resource, "remaining": remaining, "reset": int(clock.now + reset_in)}, tr.scope()
            )

    def test_opaque_pull_request_view_is_not_sent_at_zero_graphql(self):
        clock = Clock()
        self.exhaust(clock, "graphql")
        p = self.run_gh(clock, "pr", "view", "1", "--json", "title")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.github_before_request)
        self.assertIn("rate limit exceeded (graphql)", p.stderr)
        self.assertEqual(self.sent(), [])
        # An exhausted REST bucket does not stop a GraphQL-only command, nor GraphQL a REST command.
        self.assertEqual(self.run_gh(clock, "run", "list").returncode, 0)
        self.exhaust(clock, "core")
        self.exhaust(clock, "graphql", remaining=4000, reset_in=1200)
        self.assertEqual(self.run_gh(clock, "pr", "diff", "1").returncode, 1)
        self.assertEqual(self.sent(), ["run list"])

    def test_waiting_read_wakes_after_reset_with_jitter(self):
        clock = Clock()
        self.exhaust(clock, "graphql", reset_in=600)
        with patch.object(tr, "reset_jitter", return_value=45.0):
            p = self.run_gh(clock, "pr", "view", "1", wait=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(clock.sleeps, [600 + 2 + 45.0])
        self.assertEqual(self.sent(), ["pr view 1"])

    def test_jitter_is_bounded_and_configurable(self):
        os.environ["TAUCETI_GH_RESET_JITTER"] = "120"
        values = [tr.reset_jitter() for _ in range(200)]
        self.assertTrue(all(0 <= v <= 120 for v in values))
        self.assertGreater(max(values) - min(values), 30)
        os.environ["TAUCETI_GH_RESET_JITTER"] = "0"
        self.assertEqual(tr.reset_jitter(), 0)

    def test_reads_are_unpaced_without_a_service_fallback(self):
        clock = Clock(advance=False)
        for _ in range(60):
            self.assertEqual(self.run_gh(clock, "api", "repos/o/r/pulls").returncode, 0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(len(self.sent()), 60)

    def test_service_failure_marks_the_host_for_pacing(self):
        clock = Clock()
        with patch.object(tr.time, "time", clock.time):
            github_state._unavailable("o/r", "issue_comments", "coverage_unavailable")
            self.assertFalse(tr.fallback_active())
            github_state._unavailable("o/r", "issue_comments", "http_503")
            self.assertTrue(tr.fallback_active())
            clock.now += tr.FALLBACK_WINDOW + 1
            self.assertFalse(tr.fallback_active())

    def test_fallback_reads_queue_behind_a_shared_bucket(self):
        clock = Clock(advance=False)
        with patch.object(tr.time, "time", clock.time):
            tr.note_fallback()
        for _ in range(tr.FALLBACK_BURST + 10):
            self.assertEqual(self.run_gh(clock, "api", "repos/o/r/pulls", wait=True).returncode, 0)
        rate = 0.5 * (tr.DEFAULT_LIMIT - tr.FALLBACK_RESERVE) / 3600
        # The burst goes out at once; later callers each reserve the next refill rather than
        # all waking for the same token.
        self.assertEqual(len(clock.sleeps), 10)
        for i, delay in enumerate(clock.sleeps, start=1):
            self.assertAlmostEqual(delay, i / rate, places=3)
        self.assertEqual(len(self.sent()), tr.FALLBACK_BURST + 10)

    def test_fast_failing_reader_is_refused_past_its_wait(self):
        clock = Clock(advance=False)
        with patch.object(tr.time, "time", clock.time):
            tr.note_fallback()
        rate = 0.5 * (tr.DEFAULT_LIMIT - tr.FALLBACK_RESERVE) / 3600
        allowed = tr.FALLBACK_BURST + int(tr.FALLBACK_NOWAIT * rate)
        results = [self.run_gh(clock, "pr", "view", "1") for _ in range(allowed + 3)]
        self.assertTrue(all(p.returncode == 0 for p in results[:allowed]))
        for p in results[allowed:]:
            self.assertEqual(p.returncode, 1)
            self.assertTrue(p.github_before_request)
            self.assertIn("rate limit exceeded (graphql)", p.stderr)
        self.assertEqual(len(self.sent()), allowed)
        # The refusal is recorded so the stage pauses instead of charging the PR.
        with patch.object(tr.time, "time", clock.time):
            self.assertEqual(tr.blocking_event_since(clock.now - 1)["resource"], "graphql")

    def test_rate_follows_the_accounts_remaining_quota(self):
        clock = Clock(advance=False)
        with patch.object(tr.time, "time", clock.time):
            tr.note_fallback()
        self.exhaust(clock, "core", remaining=tr.FALLBACK_RESERVE + 360, reset_in=1800)
        for _ in range(tr.FALLBACK_BURST + 1):
            self.run_gh(clock, "api", "repos/o/r/pulls", wait=True)
        # 360 spare requests over 30 minutes at a half share: one request per 10 seconds.
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 10.0, places=3)

    def test_reserved_quota_waits_for_the_reset(self):
        clock = Clock(advance=False)
        with patch.object(tr.time, "time", clock.time):
            tr.note_fallback()
            path = tr.root() / "fallback-pacing" / tr.quota_scope(tr.scope()) / "core.json"
            tr.atomic(path, {"tokens": 0, "updated": clock.now})
        self.exhaust(clock, "core", remaining=tr.FALLBACK_RESERVE, reset_in=300)
        p = self.run_gh(clock, "api", "repos/o/r/pulls")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.sent(), [])
        self.run_gh(clock, "api", "repos/o/r/pulls", wait=True)
        self.assertEqual(clock.sleeps, [302])

    def test_writes_and_quota_probes_are_not_paced(self):
        clock = Clock(advance=False)
        with patch.object(tr.time, "time", clock.time):
            tr.note_fallback()
            path = tr.root() / "fallback-pacing" / tr.quota_scope(tr.scope()) / "core.json"
            tr.atomic(path, {"tokens": -1000, "updated": clock.now})
        self.assertEqual(self.run_gh(clock, "api", "-X", "PATCH", "repos/o/r/issues/1").returncode, 0)
        self.assertEqual(self.run_gh(clock, "api", "rate_limit").returncode, 0)
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
