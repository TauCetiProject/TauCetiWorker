#!/usr/bin/env python3
"""Native self-tested confirmations use the existing source-bound publisher policy."""

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_forgejo_source as fixture
from tauceti_tools.forge import AmbiguousWrite, ForgeError

from tauceti_worker.forgejo_worker import ForgejoWorkerSource
from tauceti_worker.self_tested_label import _live_reason


class NativeSelfTested(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NativeSource()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.policy = f.clone / "policy.json"
        self.record = dict(
            schema="tauceti-native-review-publisher/v1",
            publishers=f.publishers.record(),
            ledger=str(f.clone / "outbox/publications.sqlite3"),
        )
        self.record["publishers"]["statuses"] = [7]
        self.fixture.policy.publishers = type(f.publishers)(
            f.identity,
            f.publishers.reviews,
            frozenset({f.actor.principal}),
            f.publishers.recoveries,
            generation=f.publishers.generation,
        )
        self.save_policy()
        f.api._token = "token"
        f.api.max_bytes = 1024 * 1024
        f.api._repo = lambda repository: "repos/" + repository.name
        f.api.repository.return_value = dict(id=41, full_name=f.repository.name, archived=False)
        self.source = ForgejoWorkerSource(f.identity, f.api, token_file=f.clone / "token")
        self.source.review_publication_policy = self.policy
        self.source.authenticated_actor = Mock(return_value=f.actor)
        self.live = dict(
            state="open",
            draft=False,
            base=dict(ref="main"),
            head=dict(sha="a" * 40, ref="feature"),
            user=dict(id=7, login="reviewer"),
            labels=[],
        )
        self.source.live_pull = Mock(side_effect=lambda n: copy.deepcopy(self.live))

        def label(number, name):
            self.live["labels"] = [dict(name=name)]
            return True

        self.source.add_label = Mock(side_effect=label)

    def save_policy(self):
        self.policy.write_text(json.dumps(self.record))
        self.policy.chmod(0o600)

    def confirm(self):
        return self.source.confirm_self_tested(1, "a" * 40, lambda row: _live_reason(row, "a" * 40))

    def test_confirmed_status_is_pr_head_actor_and_generation_bound(self):
        self.assertEqual(self.confirm(), "accepted")
        self.assertEqual(self.source.commit_status("a" * 40, "self-tested/1"), "success")
        self.assertEqual(self.source.commit_status("a" * 40, "self-tested/2"), "")
        self.assertEqual(self.fixture.source._statuses("a" * 40)["self-tested/1"], "success")
        self.assertEqual(len(self.fixture.status_rows), 1)
        self.assertEqual(self.confirm(), "accepted")
        self.assertEqual(len(self.fixture.status_rows), 1)  # Observe, do not duplicate assertions.

    def test_unrelated_statuses_never_gain_authority(self):
        self.assertEqual(self.source.commit_status("a" * 40, "build"), "")
        self.fixture.api.request.assert_not_called()

    def test_label_alone_is_not_an_assertion(self):
        self.live["labels"] = [dict(name="self-tested")]
        self.assertEqual(self.source.commit_status("a" * 40, "self-tested/1"), "")

    def test_policy_source_generation_host_and_permissions_fail_closed(self):
        original = copy.deepcopy(self.record)
        for change in ("source", "generation", "host", "mode"):
            self.record = copy.deepcopy(original)
            if change == "source":
                self.record["publishers"]["source"]["id"] = 99
            elif change == "generation":
                self.record["publishers"]["generation"] = "0" * 32
            elif change == "host":
                self.record["publisher_host_id"] = "another-host"
            self.save_policy()
            if change == "mode":
                self.policy.chmod(0o644)
            self.assertEqual(self.confirm(), "unobserved", change)
            self.source.add_label.assert_not_called()

    def test_unenrolled_or_readonly_publisher_cannot_label_or_assert(self):
        self.source.authenticated_actor.return_value = self.fixture.queue
        self.assertEqual(self.confirm(), "refused")
        self.source.authenticated_actor.return_value = self.fixture.actor
        with patch("tauceti_queue.forgejo_source.ForgejoSourceAdapter._permission", return_value="read"):
            self.assertEqual(self.confirm(), "refused")
        self.source.add_label.assert_not_called()
        self.assertFalse(self.fixture.status_rows)

    def test_stale_generation_and_unenrolled_latest_status_do_not_confirm(self):
        self.assertEqual(self.confirm(), "accepted")
        self.record["publishers"]["generation"] = "2" * 32
        self.save_policy()
        self.assertEqual(self.source.commit_status("a" * 40, "self-tested/1"), "error")
        self.record["publishers"]["generation"] = self.fixture.publishers.generation
        self.save_policy()
        newest = copy.deepcopy(self.fixture.status_rows[-1])
        newest.update(id=999, creator=dict(id=999, login="outsider"))
        self.fixture.status_rows.append(newest)
        self.assertEqual(self.source.commit_status("a" * 40, "self-tested/1"), "error")

    def test_moved_closed_draft_held_scope_never_publishes(self):
        for key, value in (
            ("head", dict(sha="b" * 40, ref="feature")),
            ("state", "closed"),
            ("draft", True),
            ("base", dict(ref="other")),
            ("labels", [dict(name="full-lint")]),
            ("head", dict(sha="a" * 40, ref="bump-mathlib/automatic")),
            ("user", dict(id=999, login="other-author")),
        ):
            original = copy.deepcopy(self.live)
            self.live[key] = value
            self.assertTrue(self.confirm().startswith("stopped:"))
            self.live = original
        self.source.add_label.assert_not_called()
        self.assertFalse(self.fixture.status_rows)

    def test_push_racing_with_label_does_not_assert_new_head(self):
        def label(*args):
            self.live["head"]["sha"] = "b" * 40
            return True

        self.source.add_label.side_effect = label
        self.assertIn("head moved", self.confirm())
        self.assertFalse(self.fixture.status_rows)

    def test_ambiguous_status_write_is_read_back_without_replay(self):
        request = self.fixture.api.request.side_effect

        def lose_response(method, path, data=None):
            result = request(method, path, data)
            if method == "POST":
                raise AmbiguousWrite("response lost")
            return result

        self.fixture.api.request.side_effect = lose_response
        self.assertEqual(self.confirm(), "accepted")
        self.assertEqual(len(self.fixture.status_rows), 1)

    def test_failed_status_write_is_not_replayed_or_claimed(self):
        request = self.fixture.api.request.side_effect

        def fail(method, path, data=None):
            if method == "POST":
                raise ForgeError("outage", status=503)
            return request(method, path, data)

        self.fixture.api.request.side_effect = fail
        self.assertEqual(self.confirm(), "unobserved")
        self.assertFalse(self.fixture.status_rows)
        self.assertEqual(sum(call.args[0] == "POST" for call in self.fixture.api.request.call_args_list), 1)

    def test_existing_refusal_is_never_overwritten(self):
        self.assertEqual(self.confirm(), "accepted")
        self.fixture.status_rows[-1]["status"] = "failure"
        self.source.add_label.reset_mock()
        self.assertEqual(self.confirm(), "refused")
        self.assertEqual(len(self.fixture.status_rows), 1)
        self.source.add_label.assert_not_called()

    def test_ambiguous_label_is_observed_with_case_normalization(self):
        def lose_response(*args):
            self.live["labels"] = [dict(name="Self-Tested")]
            raise AmbiguousWrite("response lost")

        self.source.add_label.side_effect = lose_response
        self.assertEqual(self.confirm(), "accepted")
        self.assertEqual(self.source.add_label.call_count, 1)

    def test_failed_label_and_removed_publisher_fail_closed(self):
        self.source.add_label.side_effect = ForgeError("outage", status=503)
        self.assertEqual(self.confirm(), "label failed")
        self.assertFalse(self.fixture.status_rows)
        with patch(
            "tauceti_queue.forgejo_source.ForgejoSourceAdapter._permission", side_effect=ForgeError("gone", status=404)
        ):
            self.assertEqual(self.confirm(), "refused")

    def test_permission_is_reused_without_holding_the_review_lock(self):
        with patch("tauceti_queue.forgejo_source.ForgejoSourceAdapter._permission", return_value="write") as permission:
            self.assertEqual(self.confirm(), "accepted")
            self.assertEqual(permission.call_count, 1)

    def test_roadmap_branch_lookup_is_fresh_owner_bound_and_unambiguous(self):
        row = dict(number=1, headRefName="roadmap/x", headRepositoryOwner=dict(login="team"))
        self.source.reader.pulls = Mock(return_value=[row])
        self.assertEqual(self.source.pull_for_head("team", "roadmap/x"), 1)
        self.assertIsNone(self.source.pull_for_head("other", "roadmap/x"))
        self.source.reader.pulls.return_value = [row, {**row, "number": 2}]
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.source.reader.pulls.side_effect = ForgeError("outage", status=503)
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.assertEqual(self.source.reader.pulls.call_count, 4)


class CanonicalSource(unittest.TestCase):
    def test_only_enrolled_native_content_repository_qualifies(self):
        from types import SimpleNamespace

        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        from tauceti_worker.agents import self_tested_guidance
        from tauceti_worker.self_tested_label import enabled

        with patch.dict(os.environ, TAUCETI_SELF_TESTED="1"):
            for origin, name, native_id, eligible in (
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 1, True),
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 2, False),
                ("https://elsewhere.example", "TauCetiProject/TauCeti", 1, False),
                ("https://git.taucetiproject.org", "Another/TauCeti", 1, False),
            ):
                repository = RepositoryRef(Forge("forgejo", origin), name)
                source = SimpleNamespace(repository=repository, identity=RepositoryIdentity(repository, native_id))
                with patch("tauceti_worker.worker_sources.current", return_value=source):
                    self.assertEqual(enabled() == "", eligible)
                    self.assertEqual(bool(self_tested_guidance("ordinary")), eligible)
                    self.assertFalse(self_tested_guidance("full-lint"))


if __name__ == "__main__":
    unittest.main()
