#!/usr/bin/env python3
"""Qualified direct review-state reads never consult the shared service or gh."""

import concurrent.futures
import json
import os
import sys
import tempfile
import threading
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
            second_started.set()
            return cache.read(["key"], slow, ttl=15)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
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
            with self.assertRaises(ReadInvalidated):
                old.result(timeout=5)
        self.assertEqual(cache.read(["key"], lambda: self.fail("new observation lost"), ttl=15), (["fresh"], True))

    def test_invalid_bounds_and_ttls_are_rejected(self):
        for kwargs in [{"max_entries": True}, {"max_bytes": 0}, {"max_entries": 1.5}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BoundedReadCache(**kwargs)
        for ttl in [-1, float("inf"), float("nan"), True]:
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                BoundedReadCache().read(["key"], lambda: [], ttl=ttl)


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
        state._cache_path(12).unlink()
        self.assertEqual(state.gh_meta(12).provenance, "assumed")

    def test_review_state_does_not_retain_histories_beyond_source_cache_bounds(self):
        state = self.state()
        self.api.comments.return_value = []
        for number in range(1, 201):
            self.assertEqual(state._issue_comments(number), [])
        self.assertEqual(state._comments, {})
        self.assertLessEqual(len(state._shared_issue_hit), 128)
        self.assertLessEqual(self.source.cache.stats()["entries"], 128)

    def test_normal_read_recovers_without_memoizing_failure(self):
        state = self.state()
        self.api.comments.side_effect = ForgeError("private failure", status=503)
        self.assertEqual(state.gh_meta(12).provenance, "fetch_failed")
        self.api.comments.side_effect = None
        self.api.comments.return_value = []
        self.assertEqual(state.gh_meta(12).provenance, "missing")
        self.assertEqual(self.api.comments.call_count, 2)

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
        self.assertIsNone(self.source.review_comments(12))
        self.assertEqual(self.source.last_failure.kind, "Unsupported")

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
        }
        self.assertEqual(self.source.pr_view(12, ["number", "title"]), {"number": 12, "title": "Proof"})
        self.api.pull.side_effect = Incomplete("unavailable")
        self.assertIsNone(self.source.pr_view(12, ["number", "title"], fresh=True))

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
