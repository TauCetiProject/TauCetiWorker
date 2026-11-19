#!/usr/bin/env python3
"""Source-bound queue landing proofs, entirely over recorded API responses."""

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import target_landings as proof

REPO = "team/Math"
SHA, TIP, PARENT = "a" * 40, "b" * 40, "c" * 40
SUBJECT = f"{REPO}#7"
NATIVE = f"forgejo:https://forge.example/prefix/{REPO}@91#7"


def comment(subject=SUBJECT, *, sha=SHA, branch="release/next", login="tauceti-queue[bot]"):
    marker = f"<!-- tauceti-queue landed:{subject}:41:{sha} -->"
    return {
        "user": {"login": login},
        "body": f"Pull request successfully merged into {branch}.\n\nBuild succeeded.\n\n{marker}\n\n"
        f"<!-- tauceti-queue {marker} -->\n",
    }


class Proof(unittest.TestCase):
    def github(self, comments=None, *, status="behind", branch="release/next"):
        self.calls = []
        self.responses = {
            "repos/team/Math/branches/release%2Fnext": {"name": branch, "commit": {"sha": TIP}},
            f"repos/team/Math/compare/{TIP}...{SHA}": {"status": status},
        }
        self.comments = [comment()] if comments is None else comments
        return self.check

    def native(self, *, sha=SHA, subject=NATIVE):
        self.calls = []
        self.comments = [comment(subject, sha=sha, login="any-contributor")]
        self.responses = {
            "repos/team/Math/branches/release%2Fnext": {"name": "release/next", "commit": {"id": TIP}},
            f"repos/team/Math/compare/{TIP}...{sha}?files=false&verification=false": {
                "total_commits": 0,
                "commits": [],
            },
            f"repos/team/Math/compare/{sha}...{TIP}?files=false&verification=false": {
                "total_commits": 2,
                "commits": [{"sha": TIP}],
            },
            f"repos/team/Math/git/commits/{sha}": {
                "sha": sha,
                "message": f"A real queue squash (#7)\n\nSource-PR: {subject}\nAccepted-Head: {'d' * 40}\n\nExplanation.",
                "tree": {"sha": "e" * 40},
                "parents": [{"sha": PARENT}],
            },
            f"repos/team/Math/git/commits/{PARENT}": {"sha": PARENT, "tree": {"sha": "f" * 40}},
        }
        return lambda **kwargs: self.check(native=True, subject=subject, **kwargs)

    def read(self, path):
        self.calls.append(path)
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    def check(self, **kwargs):
        return proof.queue_landed(
            REPO,
            7,
            "release/next",
            read_comments=lambda: self.comments,
            read_json=self.read,
            **kwargs,
        )

    def test_github_queue_comment_and_configured_branch_ancestry(self):
        check = self.github()
        self.assertTrue(check())
        self.assertCountEqual(self.calls, list(self.responses))

    def test_github_tip_is_itself_a_landing_without_compare(self):
        check = self.github()
        self.responses[next(iter(self.responses))]["commit"]["sha"] = SHA
        self.assertTrue(check())
        self.assertEqual(len(self.calls), 1)

    def test_title_alone_has_no_proof(self):
        check = self.github([{"title": "[Merged by Bors] - anything", "state": "CLOSED"}])
        self.assertFalse(check())
        self.assertEqual(self.calls, [])

    def test_copied_github_record_by_author_is_not_trusted(self):
        self.assertFalse(self.github([comment(login="author")])())
        self.assertEqual(self.calls, [])

    def test_graphql_bot_identity(self):
        row = comment()
        row["author"] = {"login": "tauceti-queue", "__typename": "Bot"}
        del row["user"]
        self.assertTrue(self.github([row])())
        row["author"]["__typename"] = "User"
        self.assertFalse(self.github([row])())

    def test_record_must_bind_repository_and_number(self):
        for subject in ["other/Math#7", "team/Math#8", "forgejo:https://other/team/Math@91#7"]:
            with self.subTest(subject=subject):
                self.assertFalse(self.github([comment(subject)])())

    def test_only_complete_record_with_its_exact_wrapper_counts(self):
        original = comment()
        for body in [
            original["body"].split("\n\n<!-- tauceti-queue <!--")[0],
            original["body"].replace(SHA + " -->\n", "f" * 40 + " -->\n", 1),
            original["body"] + "\n<!-- tauceti-queue reply:7 -->",
            original["body"].replace(":41:", ":not-a-batch:"),
            original["body"].replace(":41:", ":0:"),
        ]:
            with self.subTest(body=body):
                self.assertFalse(self.github([{**original, "body": body}])())

    def test_quoted_marker_in_queue_reply_does_not_count(self):
        original = comment()
        for prefix in ["You asked:\n", "> ", "Pull request was rejected.\n"]:
            self.assertFalse(self.github([{**original, "body": prefix + original["body"]}])())

    def test_success_narrative_must_name_this_base(self):
        self.assertFalse(self.github([comment(branch="main")])())

    def test_crlf_record_is_valid(self):
        row = comment()
        row["body"] = row["body"].replace("\n", "\r\n")
        self.assertTrue(self.github([row])())

    def test_unlanded_or_diverged_commit_does_not_count(self):
        for status in ["ahead", "diverged", "", None, True]:
            with self.subTest(status=status):
                self.assertFalse(self.github(status=status)())

    def test_unreachable_or_different_branch_fails_closed(self):
        for value in [OSError("offline"), None, {}, {"name": "main", "commit": {"sha": TIP}}]:
            check = self.github()
            self.responses[next(iter(self.responses))] = value
            self.assertFalse(check())

    def test_unreadable_comments_fail_closed(self):
        self.github()
        for value in [None, {}, "partial", [None, 7, {}]]:
            self.comments = value
            self.assertFalse(self.check())
        self.assertFalse(proof.queue_landed(REPO, 7, "release/next", read_comments=lambda: 1 / 0, read_json=self.read))

    def test_native_canonical_commit_proves_untrusted_comment_candidate(self):
        check = self.native()
        self.assertTrue(check())
        self.assertCountEqual(self.calls, list(self.responses))

    def test_subjects_match_both_real_queue_modes(self):
        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        from tauceti_queue.controller import Controller
        from tauceti_queue.mixed_identity import SourceNumber

        repository = RepositoryRef(Forge("forgejo", "https://forge.example"), REPO)
        queue = SimpleNamespace(provenance_repository=repository.key)
        standalone = Controller._source_pr(queue, 7)
        mixed = Controller._source_pr(queue, SourceNumber(7, RepositoryIdentity(repository, 91)))
        self.assertEqual(standalone, f"{repository.key}#7")
        self.assertEqual(mixed.casefold(), f"{repository.key}@91#7".casefold())
        for subject in (standalone, mixed):
            self.native(subject=subject)
            self.assertTrue(self.check(native=True, subject=mixed, alternate_subjects=(standalone,)))

    def test_unbound_native_candidate_is_rejected_before_any_compare(self):
        check = self.native()
        self.responses[f"repos/team/Math/git/commits/{SHA}"]["message"] = "Initial commit"
        self.assertFalse(check())
        self.assertFalse(any("/compare/" in path or "/branches/" in path for path in self.calls))

    def test_native_subject_includes_forge_and_repository_id(self):
        check = self.native()
        for wrong in [NATIVE.replace("@91", "@92"), NATIVE.replace("forge.example", "other.example"), SUBJECT]:
            self.comments = [comment(wrong)]
            self.assertFalse(check())
        self.comments = [comment(NATIVE)]
        self.assertFalse(self.check(native=True))  # no native identity supplied

    def test_native_zero_forward_without_common_history_is_not_ancestry(self):
        check = self.native()
        key = f"repos/team/Math/compare/{SHA}...{TIP}?files=false&verification=false"
        for value in [{"total_commits": 0, "commits": []}, None, OSError("offline")]:
            self.responses[key] = value
            self.assertFalse(check())

    def test_native_forward_commits_or_malformed_comparison_fail(self):
        check = self.native()
        key = f"repos/team/Math/compare/{TIP}...{SHA}?files=false&verification=false"
        for value in [{"total_commits": 1, "commits": [{}]}, {"total_commits": False, "commits": []}, {}]:
            self.responses[key] = value
            self.assertFalse(check())

    def test_native_commit_must_bind_source_with_unique_valid_identity(self):
        check = self.native()
        row = self.responses[f"repos/team/Math/git/commits/{SHA}"]
        original = row["message"]
        for message in [
            original.replace(NATIVE, NATIVE.replace("@91", "@92")),
            original + "\nSource-PR: other/repo#7",
            original + "\nSource-PR: malformed subject with spaces",
            original + "\nAccepted-Head: invalid",
            original + "\nAccepted-Head:",
            original.replace("Accepted-Head:", "Not-Accepted-Head:"),
            original.replace("Source-PR:", "> Source-PR:"),
        ]:
            row["message"] = message
            self.assertFalse(check())

    def test_native_empty_withdrawn_queue_member_does_not_unlock(self):
        check = self.native()
        self.responses[f"repos/team/Math/git/commits/{PARENT}"]["tree"]["sha"] = "e" * 40
        self.assertFalse(check())

    def test_native_missing_malformed_or_wrong_commit_fails(self):
        check = self.native()
        key = f"repos/team/Math/git/commits/{SHA}"
        original = copy.deepcopy(self.responses[key])
        for value in [None, OSError("unavailable"), {**original, "sha": TIP}, {**original, "parents": []}]:
            self.responses[key] = value
            self.assertFalse(check())

    def test_number_and_subject_must_agree(self):
        self.github()
        for number in [True, 0, -1, "7"]:
            self.assertFalse(
                proof.queue_landed(
                    REPO, number, "release/next", read_comments=lambda: self.comments, read_json=self.read
                )
            )
        self.assertFalse(self.check(subject="team/Math#8"))


class NativeAdapter(unittest.TestCase):
    def source(self, comments, *, branch_error=None, compare_error=None):
        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, ForgeError, RepositoryRef

        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        repository = RepositoryRef(Forge("forgejo", "https://forge.example"), REPO)
        api = Mock(forge=repository.forge, _token="fixture", max_bytes=1024 * 1024, max_pages=100)
        source = ForgejoWorkerSource(RepositoryIdentity(repository, 91), api, token_file=None)
        source.issue_comments = Mock(return_value=comments)

        def read(method, path):
            self.assertEqual(method, "GET")
            if "/branches/" in path:
                if branch_error is not None:
                    raise branch_error
                return {"name": "release/next", "commit": {"id": TIP}}
            if "/compare/" in path and "f" * 40 in path:
                raise ForgeError("nominated object does not exist", status=404)
            if "/compare/" in path and compare_error is not None:
                raise compare_error
            if f"{TIP}...{SHA}" in path:
                return {"total_commits": 0, "commits": []}
            if f"{SHA}...{TIP}" in path:
                return {"total_commits": 2, "commits": [{"sha": TIP}]}
            raise AssertionError(path)

        api.request.side_effect = read
        return source

    def test_forged_missing_object_does_not_stall_or_hide_a_valid_landing(self):
        subject = f"forgejo:https://forge.example/{REPO}@91#7"
        invalid = comment(subject, sha="f" * 40, login="contributor")
        valid = comment(subject, login="contributor")
        with patch.object(proof, "native_commit_binds", return_value=True):
            self.assertFalse(self.source([invalid]).target_queue_landed(7, "release/next"))
            self.assertTrue(self.source([invalid, valid]).target_queue_landed(7, "release/next"))

    def test_oversized_compare_fails_candidate_without_stalling_source(self):
        from tauceti_tools.forge import Incomplete

        subject = f"forgejo:https://forge.example/{REPO}@91#7"
        source = self.source(
            [comment(subject)], compare_error=Incomplete("Forgejo response exceeded the configured byte limit")
        )
        with patch.object(proof, "native_commit_binds", return_value=True):
            self.assertFalse(source.target_queue_landed(7, "release/next"))

    def test_missing_branch_or_comments_report_source_unavailable(self):
        from tauceti_tools.forge import ForgeError

        from tauceti_worker.config import SourceUnavailable

        subject = f"forgejo:https://forge.example/{REPO}@91#7"
        source = self.source([comment(subject)], branch_error=ForgeError("base unavailable", status=404))
        with patch.object(proof, "native_commit_binds", return_value=True), self.assertRaises(SourceUnavailable):
            source.target_queue_landed(7, "release/next")
        with self.assertRaises(SourceUnavailable):
            self.source(None).target_queue_landed(7, "release/next")


if __name__ == "__main__":
    unittest.main()
