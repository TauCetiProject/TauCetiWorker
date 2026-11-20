#!/usr/bin/env python3
"""The Forgejo contributor-fleets team follows the GitHub claim repository's collaborators."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_queue.forgejo_team_sync import sync

BASE = "https://forge.example/api/v1"


class Fake:
    def __init__(self, collaborators, users, members):
        self.collaborators, self.users, self.members = collaborators, users, members
        self.writes = []

    def __call__(self, method, url, token, scheme="token"):
        if method != "GET":
            self.writes.append((method, url))
            return None, ""
        if url.startswith("https://api.github.com/"):
            assert scheme == "Bearer" and token == "gh"
            if "page=2" in url:
                return self.collaborators[1:], ""
            return self.collaborators[:1], '<https://api.github.com/x?page=2>; rel="next", <z>; rel="last"'
        assert token == "fj"
        page = int(url.rsplit("page=", 1)[1])
        path = url[len(BASE) + 1 :].split("?")[0]
        rows = {
            "orgs/TauCetiProject/teams": [{"id": 1, "name": "Owners"}, {"id": 2, "name": "contributor-fleets"}],
            "admin/users": self.users,
            "teams/2/members": self.members,
        }[path]
        return rows[(page - 1) * 50 : page * 50], ""


def args(dry_run=False):
    return SimpleNamespace(
        github_repository="TauCetiProject/tauceti-claims",
        github_token="gh",
        forgejo_url="https://forge.example/",
        forgejo_token="fj",
        org="TauCetiProject",
        team="contributor-fleets",
        github_source_id=1,
        dry_run=dry_run,
    )


def user(login, github_id=None):
    return {"login": login, "source_id": 1 if github_id else 0, "login_name": str(github_id or "")}


FILLERS = [user(f"filler{i}", 1000 + i) for i in range(60)]


class TeamSync(unittest.TestCase):
    def setUp(self):
        # The accounts that matter sit on the second page of every listing, so a listing that stops
        # after one page adds or removes the wrong people.
        users = FILLERS + [user("CBirkbeck", 56166236), user("gone", 7), user("joscha"), user("kim-em", 477956)]
        # a lookalike: a local account named like a collaborator is never matched by name
        users.append(user("mo271"))
        self.fake = Fake(
            collaborators=[{"id": 3491627, "login": "mo271"}, {"id": 56166236, "login": "CBirkbeck"}]
            + [{"id": 1000 + i} for i in range(60)],
            users=users,
            members=[{"login": f"filler{i}"} for i in range(55)] + [{"login": "gone"}, {"login": "joscha"}],
        )

    def test_adds_linked_collaborators_and_removes_everyone_else(self):
        sync(args(), self.fake)
        self.assertEqual(
            self.fake.writes,
            [
                ("PUT", f"{BASE}/teams/2/members/{name}")
                for name in ["CBirkbeck"] + [f"filler{i}" for i in range(55, 60)]
            ]
            + [
                ("DELETE", f"{BASE}/teams/2/members/gone"),
                ("DELETE", f"{BASE}/teams/2/members/joscha"),
            ],
        )

    def test_rerun_changes_nothing(self):
        self.fake.members = [{"login": f"filler{i}"} for i in range(60)] + [{"login": "CBirkbeck"}]
        sync(args(), self.fake)
        self.assertEqual(self.fake.writes, [])

    def test_dry_run_writes_nothing(self):
        sync(args(dry_run=True), self.fake)
        self.assertEqual(self.fake.writes, [])

    def test_empty_collaborator_list_changes_nothing(self):
        self.fake.collaborators = []
        with self.assertRaises(SystemExit):
            sync(args(), self.fake)
        self.assertEqual(self.fake.writes, [])

    def test_a_failed_later_page_changes_nothing(self):
        for broken in ("api.github.com/x?page=2", "admin/users?limit=50&page=2", "members?limit=50&page=2"):
            self.fake.writes = []

            def get(method, url, token, scheme="token", broken=broken):
                if url.endswith(broken):
                    raise OSError("unavailable")
                return self.fake(method, url, token, scheme)

            with self.assertRaises(OSError):
                sync(args(), get)
            self.assertEqual(self.fake.writes, [], broken)


if __name__ == "__main__":
    unittest.main()
