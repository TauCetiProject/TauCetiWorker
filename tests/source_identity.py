#!/usr/bin/env python3
"""Source users and ACLs never borrow a GitHub login or cached authority."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import Forge, ForgeError, Incomplete, PrincipalRef, RepositoryRef

from tauceti_worker.forgejo_reads import ForgejoReadSource
from tauceti_worker.source_identity import SourceActor, SourceRepositoryAccess


class IdentityFixtures:
    def setUp(self):
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.actor = SourceActor.from_native(self.forge, {"id": 2, "login": "operator"})

    def record(self):
        return {
            "id": 7,
            "full_name": self.repo.name,
            "html_url": self.repo.url,
            "permissions": {"pull": True, "push": True, "admin": False},
            "archived": False,
            "mirror": False,
        }


class Identities(IdentityFixtures, unittest.TestCase):
    def test_identity_survives_rename_but_never_crosses_origins_or_native_ids(self):
        renamed = SourceActor.from_native(self.forge, {"id": 2, "login": "renamed"})
        other = Forge("forgejo", "https://other.example.org")
        reused = SourceActor.from_native(other, {"id": 2, "login": "operator"})
        successor = SourceActor.from_native(self.forge, {"id": 3, "login": "operator"})
        self.assertEqual(self.actor, renamed)
        self.assertEqual(hash(self.actor), hash(renamed))
        self.assertNotEqual(self.actor, reused)
        self.assertNotEqual(self.actor, successor)
        self.assertEqual(renamed.login, "renamed")

    def test_native_and_projected_identity_encodings_are_explicit(self):
        projected = {"id": self.forge.key + "/users/2", "login": "operator"}
        self.assertEqual(SourceActor.from_projection(self.forge, projected), self.actor)
        with self.assertRaises(Incomplete):
            SourceActor.from_native(self.forge, projected)
        for identity in [2, "2", self.forge.key + "/users/02", "forgejo:https://other.example.org/users/2", None]:
            with self.subTest(identity=identity), self.assertRaises(Incomplete):
                SourceActor.from_projection(self.forge, {**projected, "id": identity})

    def test_unknown_deleted_imported_and_actions_actors_do_not_gain_native_user_authority(self):
        for row in [
            None,
            {},
            {"id": 0, "login": "operator"},
            {"id": -1, "login": "ghost"},
            {"id": -2, "login": "forgejo-actions"},
            {"id": True, "login": "operator"},
            {"id": 2, "login": ""},
            {"id": 2, "login": "bad\nlogin"},
        ]:
            with self.subTest(row=row), self.assertRaises(Incomplete):
                SourceActor.from_native(self.forge, row)

    def test_access_preserves_native_user_acl_without_inventing_token_scope(self):
        result = SourceRepositoryAccess.from_native(self.repo, self.actor, self.record())
        self.assertEqual(result.repository_id, 7)
        self.assertEqual(result.actor, self.actor)
        self.assertTrue(result.user_code_can_push)
        self.assertFalse(result.user_is_repo_admin)
        self.assertFalse(hasattr(result, "token_can_write"))

    def test_admin_role_does_not_require_code_access_and_archive_flags_are_retained(self):
        row = {
            **self.record(),
            "permissions": {"pull": False, "push": False, "admin": True},
            "archived": True,
            "mirror": True,
        }
        result = SourceRepositoryAccess.from_native(self.repo, self.actor, row)
        self.assertTrue(result.user_is_repo_admin)
        self.assertFalse(result.user_code_can_read)
        self.assertFalse(result.user_code_can_push)
        self.assertTrue(result.archived)
        self.assertTrue(result.mirror)

    def test_direct_construction_cannot_bypass_identity_and_acl_validation(self):
        for identity in ["-2", "02", "2\n"]:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                SourceActor(PrincipalRef(self.forge, identity), "operator")
        with self.assertRaises(ValueError):
            SourceActor(self.actor.principal, "bad\nlogin")
        with self.assertRaises(ValueError):
            SourceRepositoryAccess(self.repo, 7, self.actor, True, 1, False, False, False)
        other = SourceActor.from_native(Forge("forgejo", "https://other.example.org"), {"id": 2, "login": "operator"})
        with self.assertRaises(ValueError):
            SourceRepositoryAccess(self.repo, 7, other, True, True, False, False, False)

    def test_incomplete_or_foreign_access_is_unavailable_rather_than_a_false_acl(self):
        record = self.record()
        variants = [
            None,
            {},
            {**record, "id": True},
            {**record, "full_name": "other/project"},
            {**record, "html_url": "https://evil.example/one/project"},
            {**record, "html_url": self.repo.url + "?other=1"},
            {**record, "permissions": None},
            {**record, "permissions": {"push": False}},
            {**record, "permissions": {"pull": True, "push": 1, "admin": False}},
            {**record, "permissions": {"pull": False, "push": True, "admin": False}},
        ]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(Incomplete):
                SourceRepositoryAccess.from_native(self.repo, self.actor, value)


class DirectReads(IdentityFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.api = Mock(forge=self.forge, _token="fixture-token", max_bytes=1024 * 1024, max_pages=100)
        self.source = ForgejoReadSource(self.repo, self.api)
        self.requests = []
        self.user = {"id": 2, "login": "operator", "email": "not-needed@example.invalid"}
        self.permissions = self.record()

        def request(method, path):
            self.assertEqual(method, "GET")
            self.requests.append(path)
            return self.user if path == "user" else self.permissions

        self.api.request.side_effect = request
        for target in [
            "tauceti_worker.github.me",
            "tauceti_worker.github.can_push",
            "tauceti_worker.github.gh_run",
            "tauceti_worker.github_state.read",
        ]:
            blocker = patch(target, side_effect=AssertionError("GitHub fallback"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_actor_is_fresh_and_filters_unneeded_profile_data(self):
        self.assertEqual(self.source.authenticated_actor(), self.actor)
        self.user = {"id": 2, "login": "renamed"}
        result = self.source.authenticated_actor()
        self.assertEqual(result, self.actor)
        self.assertEqual(result.login, "renamed")
        self.assertEqual(self.requests, ["user", "user"])

    def test_identity_reads_neither_use_cache_slots_nor_fail_on_unrelated_pr_writes(self):
        before = self.source.cache.stats()
        original = self.api.request.side_effect

        def write_during_read(method, path):
            self.source.invalidate(37)
            return original(method, path)

        self.api.request.side_effect = write_during_read
        self.assertEqual(self.source.authenticated_actor(), self.actor)
        self.assertTrue(self.source.repository_access().user_code_can_push)
        after = self.source.cache.stats()
        for key in ["entries", "bytes", "active"]:
            self.assertEqual(after[key], before[key])

    def test_permission_checks_bypass_prior_success_and_return_current_denial(self):
        first = self.source.repository_access()
        self.assertTrue(first.user_code_can_push)
        self.permissions = {**self.permissions, "permissions": {"pull": True, "push": False, "admin": False}}
        self.assertFalse(self.source.repository_access().user_code_can_push)
        self.assertEqual(self.requests, ["user", "repos/one/project", "user", "repos/one/project"])

    def test_failed_actor_or_permission_read_never_reuses_prior_authority(self):
        self.assertTrue(self.source.repository_access().user_code_can_push)
        self.api.request.side_effect = ForgeError("private detail", status=403)
        self.assertIsNone(self.source.repository_access())
        self.assertEqual(self.source.last_failure.status, 403)
        self.assertNotIn("private detail", repr(self.source.last_failure))
        self.assertIsNone(self.source.authenticated_actor())

    def test_rotation_between_actor_and_repo_read_stops_before_second_request(self):
        def changed(method, path):
            self.assertEqual(path, "user")
            self.api._token = "changed-token"
            return self.user

        self.api.request.side_effect = changed
        self.assertIsNone(self.source.repository_access())
        self.assertEqual(self.api.request.call_count, 1)
        self.assertEqual(self.source.last_failure.kind, "SourceBindingChanged")


if __name__ == "__main__":
    with patch.dict(
        os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://must-not-contact.example", "GH_TOKEN": "github-sentinel"}
    ):
        unittest.main()
