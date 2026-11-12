#!/usr/bin/env python3
"""Qualified direct review-state reads never consult the shared service or gh."""

import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import GITHUB, Forge, ForgeError, Incomplete, RepositoryRef

from tauceti_worker import github_transport as transport
from tauceti_worker.forgejo_reads import BoundedReadCache, ForgejoReadSource, ReadInvalidated, prune_state_cache
from tauceti_worker.github import GitHub
from tauceti_worker.review_state import ReviewState


def notify_wait(joined):
    class Waitable(concurrent.futures.Future):
        def result(self, *args, **kwargs):
            joined.set()
            return super().result(*args, **kwargs)

    return patch("tauceti_worker.forgejo_reads.Future", Waitable)


class CacheTests(unittest.TestCase):
    def test_disk_budget_covers_multiple_credentials_without_touching_other_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outbox.json"
            outside.write_text("durable intent")
            for actor in ["a" * 64, "b" * 64]:
                directory = root / actor
                directory.mkdir()
                for number in range(1, 5):
                    path = directory / f"{number}.key.json"
                    path.write_text("x" * 100)
                    os.utime(path, (number, number))
            (root / ("b" * 64) / "5.json").symlink_to(outside)
            durable = root / "durable"
            durable.mkdir()
            (durable / "1.json").write_text("durable state")
            (root / ("c" * 64)).symlink_to(durable, target_is_directory=True)
            abandoned = root / ("a" * 64) / ("1.key." + "a" * 32 + ".tmp")
            abandoned.write_text("x" * 100)
            os.utime(abandoned, (0, 0))
            untouched = root / ("a" * 64) / "request.json"
            untouched.write_text("must not delete")
            prune_state_cache(root, max_files=3, max_bytes=250)
            eligible = [path for path in root.glob("*/*.key.json") if not path.is_symlink()]
            self.assertLessEqual(len(eligible), 2)
            self.assertLessEqual(sum(path.stat().st_size for path in eligible), 250)
            self.assertTrue(all(path.name == "4.key.json" for path in eligible))
            self.assertEqual(outside.read_text(), "durable intent")
            self.assertEqual(untouched.read_text(), "must not delete")
            self.assertTrue((root / ("b" * 64) / "5.json").is_symlink())
            self.assertEqual((durable / "1.json").read_text(), "durable state")
            self.assertFalse(abandoned.exists())

    def test_entry_and_serialized_byte_bounds_include_keys(self):
        cache = BoundedReadCache(max_entries=2, max_bytes=100)
        for n in range(20):
            cache.read([n], lambda: {"body": "x" * 20}, ttl=15)
            self.assertLessEqual(cache.stats()["entries"], 2)
            self.assertLessEqual(cache.stats()["bytes"], 100)
        cache.read(["x" * 1000], lambda: [], ttl=15)
        self.assertLessEqual(cache.stats()["bytes"], 100)
        self.assertLessEqual(cache.stats()["entries"], 2)

    def test_mutating_returned_json_does_not_change_the_cache(self):
        cache = BoundedReadCache()
        value, hit = cache.read(["key"], lambda: {"rows": [1]}, ttl=15)
        self.assertFalse(hit)
        value["rows"].append(2)
        again, hit = cache.read(["key"], lambda: self.fail("cache miss"), ttl=15)
        self.assertTrue(hit)
        self.assertEqual(again, {"rows": [1]})

    def test_ttl_expiry_and_shorter_callers_cannot_extend_an_entry(self):
        now = [10.0]
        cache = BoundedReadCache(clock=lambda: now[0])
        cache.read(["key"], lambda: [1], ttl=100)
        now[0] += 5
        self.assertEqual(cache.read(["key"], lambda: [2], ttl=1), ([2], False))
        now[0] += 1
        self.assertEqual(cache.read(["key"], lambda: [3], ttl=1), ([3], False))
        now[0] = 1
        self.assertEqual(cache.read(["key"], lambda: [4], ttl=15), ([4], False))

    def test_failures_and_oversized_values_are_not_cached(self):
        cache = BoundedReadCache(max_bytes=100)
        for fetch in [lambda: None, lambda: "x" * 1000]:
            value, hit = cache.read(["key"], fetch, ttl=15)
            self.assertFalse(hit)
            self.assertIn(value, [None, "x" * 1000])
            self.assertEqual(cache.stats()["entries"], 0)
        with self.assertRaises(RuntimeError):
            cache.read(["key"], lambda: (_ for _ in ()).throw(RuntimeError("offline")), ttl=15)
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertEqual(cache.stats()["active"], 0)

    def test_invalidation_during_any_read_refuses_the_observation(self):
        for fresh in [False, True]:
            cache = BoundedReadCache()

            def fetch(cache=cache):
                cache.invalidate()
                return ["before-write"]

            with self.subTest(fresh=fresh), self.assertRaises(ReadInvalidated):
                cache.read(["key"], fetch, ttl=15, fresh=fresh)
            self.assertEqual(cache.stats()["entries"], 0)

    def test_identical_triage_reads_coalesce(self):
        cache = BoundedReadCache()
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def slow():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return ["triage"]

        def second():
            return cache.read(["key"], slow, ttl=15)

        with notify_wait(second_started), concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(cache.read, ["key"], slow, ttl=15)
            self.assertTrue(entered.wait(5))
            other = executor.submit(second)
            self.assertTrue(second_started.wait(5))
            release.set()
            self.assertEqual(first.result(timeout=5), (["triage"], False))
            self.assertEqual(other.result(timeout=5), (["triage"], True))
        self.assertEqual(len(calls), 1)

    def test_fresh_checks_do_not_wait_or_get_overwritten_by_older_triage(self):
        cache = BoundedReadCache()
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(release.wait(5))
            return ["old"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            old = executor.submit(cache.read, ["key"], slow, ttl=15)
            self.assertTrue(entered.wait(5))
            try:
                fresh = executor.submit(cache.read, ["key"], lambda: ["fresh"], ttl=15, fresh=True)
                self.assertEqual(fresh.result(timeout=5), (["fresh"], False))
            finally:
                release.set()
            self.assertEqual(old.result(timeout=5), (["old"], False))
        self.assertEqual(cache.read(["key"], lambda: self.fail("new observation lost"), ttl=15), (["fresh"], True))

    def test_invalid_bounds_and_ttls_are_rejected(self):
        for kwargs in [{"max_entries": True}, {"max_bytes": 0}, {"max_entries": 1.5}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BoundedReadCache(**kwargs)
        for ttl in [-1, float("inf"), float("nan"), True]:
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                BoundedReadCache().read(["key"], lambda: [], ttl=ttl)

    def test_concurrent_fresh_reads_both_return_and_later_start_wins(self):
        cache = BoundedReadCache()
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(release.wait(5))
            return ["earlier"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.read, ["key"], slow, ttl=15, fresh=True)
            self.assertTrue(entered.wait(5))
            try:
                later = executor.submit(cache.read, ["key"], lambda: ["later"], ttl=15, fresh=True)
                self.assertEqual(later.result(timeout=5), (["later"], False))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), (["earlier"], False))
        self.assertEqual(cache.read(["key"], lambda: self.fail("lost later result"), ttl=15), (["later"], True))
        self.assertEqual(cache.stats()["active"], 0)

    def test_later_triage_wins_over_slow_fresh_without_canceling_it(self):
        cache = BoundedReadCache()
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(release.wait(5))
            return ["earlier"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.read, ["key"], slow, ttl=15, fresh=True)
            self.assertTrue(entered.wait(5))
            try:
                later = executor.submit(cache.read, ["key"], lambda: ["later"], ttl=15)
                self.assertEqual(later.result(timeout=5), (["later"], False))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), (["earlier"], False))
        self.assertEqual(cache.read(["key"], lambda: self.fail("lost later result"), ttl=15), (["later"], True))

    def test_distinct_keys_fetch_independently(self):
        cache = BoundedReadCache()
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(release.wait(5))
            return ["slow"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.read, ["one"], slow, ttl=15)
            self.assertTrue(entered.wait(5))
            try:
                second = executor.submit(cache.read, ["two"], lambda: ["other"], ttl=15)
                self.assertEqual(second.result(timeout=5), (["other"], False))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), (["slow"], False))

    def test_scoped_write_does_not_invalidate_other_inflight_or_cached_reads(self):
        cache = BoundedReadCache()
        cache.read(["cached"], lambda: [1], ttl=15, scope="other")

        def fetch():
            cache.invalidate(scope="written")
            return [2]

        self.assertEqual(cache.read(["active"], fetch, ttl=15, scope="other"), ([2], False))
        self.assertEqual(
            cache.read(["cached"], lambda: self.fail("other namespace cleared"), ttl=15, scope="other"), ([1], True)
        )
        self.assertEqual(cache.stats()["active"], 0)

    def test_slow_fetch_does_not_extend_ttl_past_its_start(self):
        now = [10.0]
        cache = BoundedReadCache(clock=lambda: now[0])

        def slow():
            now[0] += 20
            return [1]

        self.assertEqual(cache.read(["key"], slow, ttl=15), ([1], False))
        self.assertEqual(cache.stats()["entries"], 0)

    def test_invalidation_releases_followers_and_allows_a_new_leader(self):
        cache = BoundedReadCache()
        entered, joined, release = threading.Event(), threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(release.wait(5))
            return ["old"]

        with notify_wait(joined), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(cache.read, ["key"], slow, ttl=15, scope=("repo", 1))
            self.assertTrue(entered.wait(5))
            follower = executor.submit(cache.read, ["key"], slow, ttl=15, scope=("repo", 1))
            self.assertTrue(joined.wait(5))
            try:
                cache.invalidate(scope=("repo", 1))
                with self.assertRaises(ReadInvalidated):
                    follower.result(timeout=2)
                self.assertFalse(leader.done())
                self.assertEqual(cache.read(["key"], lambda: ["new"], ttl=15, scope=("repo", 1)), (["new"], False))
            finally:
                release.set()
            with self.assertRaises(ReadInvalidated):
                leader.result(timeout=5)
        self.assertEqual(cache.stats()["active"], 0)
        self.assertEqual(
            cache.read(["key"], lambda: self.fail("new result lost"), ttl=15, scope=("repo", 1)), (["new"], True)
        )

    def test_failed_and_interrupted_leaders_release_followers_without_cached_failure(self):
        for error, expected in [(RuntimeError("offline"), RuntimeError), (KeyboardInterrupt(), ReadInvalidated)]:
            with self.subTest(kind=type(error).__name__):
                cache = BoundedReadCache()
                entered, joined, release = threading.Event(), threading.Event(), threading.Event()

                def failing(error=error, entered=entered, release=release):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    raise error

                with notify_wait(joined), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    leader = executor.submit(cache.read, ["key"], failing, ttl=15)
                    self.assertTrue(entered.wait(5))
                    follower = executor.submit(cache.read, ["key"], failing, ttl=15)
                    self.assertTrue(joined.wait(5))
                    release.set()
                    with self.assertRaises(type(error)):
                        leader.result(timeout=5)
                    with self.assertRaises(expected):
                        follower.result(timeout=5)
                self.assertEqual(cache.stats()["entries"], 0)
                self.assertEqual(cache.stats()["active"], 0)
                self.assertEqual(cache.read(["key"], lambda: ["recovered"], ttl=15), (["recovered"], False))


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.api = self.client(self.forge, "fixture-token")
        self.api.comments.return_value = [self.comment()]
        self.source = GitHub.for_repository(self.repo, api=self.api)
        # Any accidental fallback fails, even when an ambient GitHub credential
        # and service destination exist. No real credential/network is used.
        for target in [
            "tauceti_worker.github_state.read",
            "tauceti_worker.github_state.refresh_pr",
            "tauceti_worker.github_state.enabled",
            "tauceti_worker.review_state.github_state_enabled",
            "tauceti_worker.github.gh_run",
        ]:
            blocker = patch(target, side_effect=AssertionError("unexpected GitHub/service fallback"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def client(self, forge, token):
        return Mock(forge=forge, _token=token, max_bytes=1024 * 1024, max_pages=100)

    def comment(self, head="a" * 40):
        return {
            "id": 7,
            "body": "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + json.dumps({"head_sha": head}) + "-->",
            "created_at": "2026-11-07T00:00:00Z",
            "updated_at": "2026-11-07T00:00:00Z",
            "html_url": self.repo.pull_url(12) + "#issuecomment-7",
            "user": {"id": 2, "login": "operator"},
        }

    def state(self, source=None):
        cfg = SimpleNamespace(sbcache=Path(self.root.name) / "scoreboard")
        return ReviewState(cfg, source or self.source)

    def test_selected_head_refresh_bypasses_cached_native_header_and_never_uses_github(self):
        from tauceti_worker.state_repair import repair_selected, retry_key
        from tauceti_worker.survey import Candidate, Counters

        self.api.pull.return_value = {
            "number": 12,
            "html_url": self.repo.pull_url(12),
            "flow": 0,
            "state": "open",
            "merged": False,
            "head": {"sha": "a" * 40},
        }
        self.assertEqual(self.source.pr_view(12, ["headRefOid"]), {"headRefOid": "a" * 40})
        self.api.pull.return_value["head"]["sha"] = "b" * 40
        cfg = SimpleNamespace(state=Path(self.root.name))
        worker = SimpleNamespace(gh=self.source, rs=self.state(), counters=Counters(cfg))
        repair_selected(worker, Candidate(12, "a" * 40), "b" * 40)
        self.assertTrue(worker.selection_changed)
        self.assertEqual(self.api.pull.call_count, 2)
        # A successful native refresh needs no outage backoff from the GitHub service.
        self.assertLess(worker.counters.read(retry_key(12, "a" * 40)), time.time() + 5)

    def test_native_refresh_failure_does_not_reuse_the_cached_head(self):
        self.api.pull.return_value = {
            "number": 12,
            "html_url": self.repo.pull_url(12),
            "flow": 0,
            "state": "open",
            "merged": False,
            "head": {"sha": "a" * 40},
        }
        self.source.pr_view(12, ["headRefOid"])
        self.api.pull.side_effect = ForgeError("offline", status=503)
        result = self.source.refresh_pr_state(12, "a" * 40, "b" * 40)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("data", result)

    def test_review_state_consumes_native_comments_and_cache_provenance(self):
        state = self.state()
        first = state.gh_meta(12)
        self.assertEqual(first.provenance, "fresh")
        self.assertEqual(first.data["head_sha"], "a" * 40)
        self.assertEqual(state.sbcache.stat().st_mode & 0o777, 0o700)
        self.assertEqual(state._cache_path(12).stat().st_mode & 0o777, 0o600)
        self.assertEqual(state._key_path(12).stat().st_mode & 0o777, 0o600)
        self.api.comments.assert_called_once_with(self.repo, 12)
        again = self.state().gh_meta(12)
        self.assertEqual(again.provenance, "assumed")
        self.assertEqual(self.api.comments.call_count, 1)
        self.api.comments.return_value = [self.comment("b" * 40)]
        forced = state.gh_meta(12, force=True)
        self.assertEqual(forced.provenance, "fresh")
        self.assertEqual(forced.data["head_sha"], "b" * 40)
        self.assertEqual(self.api.comments.call_count, 2)

    def test_inherited_fresh_context_bypasses_triage_cache(self):
        self.source.issue_comments(12)
        with transport.read_context(fresh=True):
            self.source.issue_comments(12)
            self.assertFalse(transport.cache_hit())
        self.assertEqual(self.api.comments.call_count, 2)

    def test_memory_memo_does_not_relabel_cached_metadata_as_fresh(self):
        state = self.state()
        self.assertEqual(state.gh_meta(12).provenance, "fresh")
        fetched = json.loads(state._key_path(12).read_text())["fetched_at"]
        state._cache_path(12).unlink()
        self.assertEqual(state.gh_meta(12).provenance, "assumed")
        self.assertFalse(state._cache_path(12).exists())
        self.assertEqual(json.loads(state._key_path(12).read_text())["fetched_at"], fetched)

    def test_review_state_does_not_retain_histories_beyond_source_cache_bounds(self):
        state = self.state()
        self.api.comments.return_value = []
        for number in range(1, 201):
            self.assertEqual(state._issue_comments(number), ([], False))
        self.assertEqual(state._comments, {})
        self.assertLessEqual(self.source.cache.stats()["entries"], 128)

    def test_normal_read_recovers_without_memoizing_failure(self):
        state = self.state()
        self.api.comments.side_effect = ForgeError("private failure", status=503)
        self.assertEqual(state.gh_meta(12).provenance, "fetch_failed")
        self.api.comments.side_effect = None
        self.api.comments.return_value = []
        self.assertEqual(state.gh_meta(12).provenance, "missing")
        self.assertEqual(self.api.comments.call_count, 2)

    def test_source_sidecar_expires_at_triage_ttl_even_if_pr_clock_stays_put(self):
        now = [10.0]
        self.source.cache.clock = lambda: now[0]
        state = self.state()
        state.observe([SimpleNamespace(number=12, updated_at="unchanged")])
        wall = time.time()
        with patch("tauceti_worker.review_state.time.time", return_value=wall):
            self.assertEqual(state.gh_meta(12).data["head_sha"], "a" * 40)
        self.api.comments.return_value = [self.comment("b" * 40)]
        now[0] += 16
        with patch("tauceti_worker.review_state.time.time", return_value=wall + 16):
            result = state.gh_meta(12)
        self.assertEqual((result.data["head_sha"], result.provenance), ("b" * 40, "fresh"))

    def test_source_disk_age_starts_before_fetch_and_older_writer_cannot_replace_newer(self):
        state = self.state()
        state.observe([SimpleNamespace(number=12, updated_at="unchanged")])
        started = time.time()
        wall = [started]

        def slow(*args):
            wall[0] += 20
            return [self.comment()]

        self.api.comments.side_effect = slow
        with patch("tauceti_worker.review_state.time.time", side_effect=lambda: wall[0]):
            self.assertEqual(state.gh_meta(12, force=True).provenance, "fresh")
        self.assertEqual(json.loads(state._key_path(12).read_text())["fetched_at"], started)
        self.assertAlmostEqual(state._cache_path(12).stat().st_mtime, started, places=4)
        state._write_source_cache(state._cache_path(12), {"head_sha": "newer"}, observed_at=started + 10)
        state._write_source_cache(state._cache_path(12), {"head_sha": "older"}, observed_at=started)
        self.assertEqual(json.loads(state._cache_path(12).read_text())["head_sha"], "newer")

    def test_nonfinite_or_surrogate_meta_cannot_turn_a_cache_write_into_a_read_failure(self):
        for value in ['{"x":1e400}', '{"x":"\\ud800"}']:
            with self.subTest(value=value):
                comment = {**self.comment(), "body": "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + value + "-->"}
                self.api.comments.return_value = [comment]
                result = self.state().gh_meta(12, force=True)
                self.assertEqual(result.provenance, "fresh")
                self.assertIn("x", result.data)
                self.assertFalse(self.state()._cache_path(12).exists())

    def test_busy_directory_lock_skips_cache_write_without_blocking_a_fresh_read(self):
        state = self.state()
        state.sbcache.mkdir(mode=0o700, parents=True)
        directory = os.open(state.sbcache, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(state.gh_meta, 12, force=True)
                try:
                    self.assertEqual(future.result(timeout=2).provenance, "fresh")
                finally:
                    fcntl.flock(directory, fcntl.LOCK_UN)
            self.assertFalse(state._cache_path(12).exists())
        finally:
            os.close(directory)

    def test_concurrent_forced_states_return_their_reads_but_keep_the_later_disk_record(self):
        state = self.state()
        entered, release = threading.Event(), threading.Event()
        calls = iter(["slow", "fast"])
        lock = threading.Lock()

        def fetch(*args):
            with lock:
                kind = next(calls)
            if kind == "slow":
                entered.set()
                self.assertTrue(release.wait(5))
            return [self.comment("a" * 40 if kind == "slow" else "b" * 40)]

        self.api.comments.side_effect = fetch
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(state.gh_meta, 12, force=True)
            self.assertTrue(entered.wait(5))
            try:
                fast = executor.submit(state.gh_meta, 12, force=True).result(timeout=5)
                self.assertEqual((fast.data["head_sha"], fast.provenance), ("b" * 40, "fresh"))
            finally:
                release.set()
            slow = slow.result(timeout=5)
            self.assertEqual((slow.data["head_sha"], slow.provenance), ("a" * 40, "fresh"))
        self.assertEqual(json.loads(state._cache_path(12).read_text())["head_sha"], "b" * 40)
        self.assertEqual(json.loads(state._key_path(12).read_text())["meta"]["head_sha"], "b" * 40)

    def test_bust_targets_one_pr_across_credentials_without_touching_other_prs(self):
        other_api = self.client(self.forge, "other-token")
        other_api.comments.side_effect = lambda repo, number: [{**self.comment(), "html_url": repo.pull_url(number)}]
        self.api.comments.side_effect = lambda repo, number: [{**self.comment(), "html_url": repo.pull_url(number)}]
        other = ForgejoReadSource(self.repo, other_api, cache=self.source.cache)
        for source in [self.source, other]:
            source.issue_comments(12)
            source.issue_comments(13)
        self.state().bust(12)
        for source in [self.source, other]:
            source.issue_comments(12)
            source.issue_comments(13)
        self.assertEqual(self.api.comments.call_count, 3)
        self.assertEqual(other_api.comments.call_count, 3)

    def test_service_facts_cannot_entitle_a_different_source(self):
        state = self.state()
        state.seed_survey([{"pr": {"number": 12}, "review": {"issue_complete": True, "meta": {"head_sha": "forged"}}}])
        self.assertEqual(state.gh_meta(12, force=True).data["head_sha"], "a" * 40)
        self.api.comments.assert_called_once()

    def test_origins_credentials_and_repository_names_isolate_all_caches(self):
        first = self.state()
        first.gh_meta(12)
        variants = [
            (RepositoryRef(self.forge, "other/project"), self.client(self.forge, "fixture-token")),
            (self.repo, self.client(self.forge, "other-token")),
            (
                RepositoryRef(Forge("forgejo", "https://other.example.org"), self.repo.name),
                self.client(Forge("forgejo", "https://other.example.org"), "fixture-token"),
            ),
        ]
        for repo, api in variants:
            api.comments.return_value = []
            source = ForgejoReadSource(repo, api, cache=self.source.cache)
            with self.subTest(repo=repo.key):
                self.assertNotEqual(source.cache_namespace, self.source.cache_namespace)
                self.assertNotEqual(self.state(source).sbcache, first.sbcache)
                self.assertEqual(self.state(source).gh_meta(12).provenance, "missing")
                api.comments.assert_called_once()

    def test_reused_logins_never_replace_origin_qualified_native_ids(self):
        value = self.source.issue_comments(12)[0]
        self.assertEqual(value["id"], 7)
        self.assertEqual(value["node_id"], self.forge.key + "/comments/7")
        self.assertEqual(value["user"]["node_id"], self.forge.key + "/users/2")
        self.assertNotIn("author_association", value)

    def test_credential_or_origin_rotation_refuses_old_reader_and_sidecars(self):
        state = self.state()
        self.assertEqual(state.gh_meta(12).data["head_sha"], "a" * 40)
        for changed in ["changed-token", "fixture-token"]:
            with self.subTest(changed=changed):
                self.api._token = changed
                if changed == "fixture-token":
                    self.api.forge = Forge("forgejo", "https://other.example.org")
                self.assertIsNone(self.source.issue_comments(12))
                self.assertEqual(self.source.last_failure.kind, "SourceBindingChanged")
                self.assertNotIn(changed, repr(self.source.last_failure))
                for force in [False, True]:
                    result = state.gh_meta(12, force=force)
                    self.assertEqual((result.data, result.provenance), ({}, "fetch_failed"))
                self.assertEqual(self.state().gh_meta(12).data, {})
        self.assertEqual(self.api.comments.call_count, 1)

    def test_failed_forced_read_is_stale_then_recovers_without_cached_failure(self):
        state = self.state()
        state.gh_meta(12)
        self.api.comments.side_effect = ForgeError("secret diagnostic must not be retained", status=503)
        failed = state.gh_meta(12, force=True)
        self.assertEqual(failed.provenance, "stale")
        self.assertEqual(self.source.last_failure.status, 503)
        self.assertNotIn("secret", repr(self.source.last_failure))
        self.api.comments.side_effect = None
        self.api.comments.return_value = []
        recovered = state.gh_meta(12, force=True)
        self.assertEqual(recovered.provenance, "missing")
        self.assertIsNone(self.source.last_failure)

    def test_wrong_discussion_or_incomplete_body_is_unavailable_not_empty(self):
        self.api.comments.return_value = [{**self.comment(), "html_url": "https://evil.example/one/project/pulls/12"}]
        self.assertIsNone(self.source.issue_comments(12))
        self.assertEqual(self.source.last_failure.kind, "Incomplete")
        self.assertEqual(self.state().gh_meta(12).provenance, "fetch_failed")

    def test_bust_invalidates_only_this_sources_read_path(self):
        state = self.state()
        state.gh_meta(12)
        with patch("tauceti_worker.review_state.invalidate", side_effect=AssertionError("GitHub invalidation")):
            state.bust(12)
        self.assertEqual(self.source.cache.stats()["entries"], 0)
        self.api.comments.return_value = [self.comment("b" * 40)]
        self.assertEqual(state.gh_meta(12).data["head_sha"], "b" * 40)

    def test_partial_thread_support_is_explicitly_unavailable(self):
        self.source.issue_comments(12)
        before = self.source.cache.stats()
        self.assertIsNone(self.source.review_comments(12))
        self.assertEqual(self.source.last_failure.kind, "Unsupported")
        self.assertEqual(self.source.cache.stats(), before)

    def test_credential_change_during_fetch_cannot_publish_a_cache_entry(self):
        def changed(*args):
            self.api._token = "new-token"
            return [self.comment()]

        self.api.comments.side_effect = changed
        self.assertIsNone(self.source.issue_comments(12))
        self.assertEqual(self.source.cache.stats()["entries"], 0)
        self.assertEqual(self.source.last_failure.kind, "SourceBindingChanged")

    def test_pr_metadata_projection_uses_direct_typed_reader(self):
        self.api.pull.return_value = {
            "id": 3,
            "number": 12,
            "title": "Proof",
            "body": "",
            "state": "open",
            "user": {"id": 2, "login": "operator"},
            "html_url": self.repo.pull_url(12),
            "flow": 0,
            "merged": False,
        }
        self.assertEqual(self.source.pr_view(12, ["number", "title"]), {"number": 12, "title": "Proof"})
        self.api.pull.side_effect = Incomplete("unavailable")
        self.assertIsNone(self.source.pr_view(12, ["number", "title"], fresh=True))

    def test_scoreboard_order_handles_timezone_offsets_and_same_second_ids(self):
        older = {**self.comment("a" * 40), "updated_at": "2026-04-05T01:50:00+11:00"}
        later = {**self.comment("b" * 40), "id": 8, "updated_at": "2026-04-05T01:10:00+10:00"}
        self.api.comments.return_value = [older, later]
        state = self.state()
        self.assertEqual(state.gh_meta(12, force=True).data["head_sha"], "b" * 40)
        tied = {**later, "id": 9, "body": self.comment("c" * 40)["body"]}
        self.api.comments.return_value = [older, later, tied]
        self.assertEqual(state.gh_meta(12, force=True).data["head_sha"], "c" * 40)

    def test_invalid_timestamp_and_malformed_projection_are_unavailable(self):
        self.api.comments.return_value = [{**self.comment(), "updated_at": "no offset or valid time"}]
        self.assertIsNone(self.source.issue_comments(12))
        self.assertEqual(self.source.last_failure.kind, "Incomplete")
        self.api.pull.return_value = {"number": 12, "html_url": 42}
        self.assertIsNone(self.source.pr_view(12, ["number"], fresh=True))
        self.assertEqual(self.source.last_failure.kind, "Incomplete")
        with self.assertRaises(ValueError):
            self.source.pr_view(12, None)
        self.api.pull.return_value = {
            "number": 12,
            "title": "Wrong source",
            "html_url": "https://evil.example/pulls/12",
        }
        self.assertIsNone(self.source.pr_view(12, ["number", "title"], fresh=True))
        self.assertEqual(self.source.last_failure.kind, "Incomplete")

    def test_factory_and_constructor_reject_ambiguous_bindings(self):
        with self.assertRaises(ValueError):
            GitHub(self.repo)
        with self.assertRaises(ValueError):
            GitHub.for_repository(self.repo)
        with self.assertRaises(ValueError):
            GitHub.for_repository(self.repo, api=self.client(GITHUB, "token"))
        with self.assertRaises(ValueError):
            GitHub.for_repository("one/project", api=self.api)
        self.assertIsInstance(GitHub.for_repository(RepositoryRef(GITHUB, self.repo.name)), GitHub)

    def test_real_worker_bootstrap_accepts_native_content_and_refuses_unported_shared_sources(self):
        root = Path(self.root.name)
        config = root / "project.toml"
        config.write_text("""schema_version=2
[forges.source]
kind="forgejo"
url="https://git.example.org"
[repositories]
content={forge="source",name="one/Project"}
roadmap="one/Roadmap"
[ci]
backend="github-actions"
repository="one/Project"
""")
        env = os.environ.copy()
        for key in ("TAUCETI_CONF", "TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT"):
            env.pop(key, None)
        env["XDG_STATE_HOME"] = str(root / "state")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "tauceti"), "--conf", str(config), "--help"],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config.write_text(
            config.read_text().replace('roadmap="one/Roadmap"', 'roadmap={forge="source",name="one/Roadmap"}')
        )
        script = "import tauceti_worker"
        result = subprocess.run(
            [sys.executable, "-c", script, "--conf", str(config)],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Shared roadmap, tool and archive repositories must currently use GitHub bindings", result.stderr)

    def test_generic_test_doubles_do_not_select_direct_source_by_truthiness(self):
        cfg = SimpleNamespace(sbcache=Path(self.root.name) / "legacy")
        state = ReviewState(cfg, Mock())
        self.assertFalse(state._direct_source)
        self.assertEqual(state.sbcache, cfg.sbcache)

    def test_revision_context_and_invalidation_races_do_not_entitle_old_reads(self):
        for revision in ["before", "after"]:
            with transport.read_context(revision=revision):
                self.source.issue_comments(12)
        self.assertEqual(self.api.comments.call_count, 2)
        self.source.invalidate()

        def fetch(*args):
            self.source.invalidate()
            return [self.comment()]

        self.api.comments.side_effect = fetch
        self.assertIsNone(self.source.issue_comments(12))
        self.assertEqual(self.source.last_failure.kind, "ReadInvalidated")


if __name__ == "__main__":
    # All real installations and credential files stay outside this harness.
    with patch.dict(
        os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://must-not-contact.example", "GH_TOKEN": "github-sentinel"}
    ):
        unittest.main()
