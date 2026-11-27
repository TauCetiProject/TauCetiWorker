#!/usr/bin/env python3
"""Forgejo landings are attributed to GitHub accounts by immutable IDs, and the workflow is dispatched."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import http.client
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queue_controller import PROJECT, ControllerCase

from tauceti_queue import claims_access, server

FORGE = "https://forge.example"
SOURCE = json.dumps({"forge": {"kind": "forgejo", "url": FORGE}, "id": 1, "name": PROJECT})
OTHER = json.dumps({"forge": {"kind": "forgejo", "url": "https://elsewhere.example"}, "id": 1, "name": PROJECT})


def ledger(path, rows):
    with closing(sqlite3.connect(path)) as c, c:
        c.execute(
            "CREATE TABLE inputs (id INTEGER PRIMARY KEY, repository TEXT, number INTEGER, author TEXT, "
            "state TEXT, landed_at REAL, source_identity TEXT NOT NULL DEFAULT '')"
        )
        c.executemany(
            "INSERT INTO inputs (repository, number, author, state, landed_at, source_identity) VALUES (?,?,?,?,?,?)",
            rows,
        )


def account(id, login, github_id=None, source_id=None):
    return {
        "id": id,
        "login": login,
        "source_id": (1 if github_id else 0) if source_id is None else source_id,
        "login_name": str(github_id or ""),
    }


class Forgejo:
    def __init__(self, users, pulls):
        self.users, self.pulls, self.calls = users, pulls, []

    @staticmethod
    def assertion(ok, url):
        if not ok:
            raise AssertionError(f"wrong credential for {url}")

    def __call__(self, url, token):
        self.calls.append(url)
        path = url[len(FORGE) + len("/api/v1/") :]
        # the admin token reads the user listing and nothing else
        self.assertion(token == ("fj" if path.startswith("admin/users") else None), url)
        if path.startswith("admin/users"):
            page = int(path.rsplit("page=", 1)[1])
            return self.users[(page - 1) * 50 : page * 50]
        number = int(path.rsplit("/", 1)[1])
        if number not in self.pulls:
            raise OSError("404")
        return self.pulls[number]


def pull(poster, repo_id=1):
    return {"user": {"id": poster[0], "login": poster[1]}, "base": {"repo": {"id": repo_id}}}


def args(root, dry_run=False):
    return SimpleNamespace(
        database=root / "queue.sqlite3",
        repository=PROJECT,
        forgejo_url=FORGE + "/",
        forgejo_repository=PROJECT,
        forgejo_repository_id=1,
        forgejo_token="fj",
        github_source_id=1,
        output=None,
        state=None,
        workflow="claims-access.yml",
        ref="main",
        dispatch_interval=3600,
        config=None,
        dry_run=dry_run,
    )


class Attribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ledger(
            self.root / "queue.sqlite3",
            [
                (PROJECT, 46000, "ghuser", "landed", 50.0, ""),
                (PROJECT, 7, "alice", "landed", 100.0, SOURCE),
                (PROJECT, 9, "alice", "landed", 200.0, SOURCE),
                # renamed since: the ledger keeps the old login, the PR keeps the poster's ID
                (PROJECT, 11, "old-bob", "landed", 150.0, SOURCE),
                # not landed, or no receipt, or another forge: never evidence
                (PROJECT, 12, "carol", "cancelled", None, SOURCE),
                (PROJECT, 13, "dave", "landed", None, SOURCE),
                (PROJECT, 14, "erin", "landed", 160.0, OTHER),
                # a local account and an account from another sign-in source
                (PROJECT, 15, "mallory", "landed", 170.0, SOURCE),
                (PROJECT, 16, "oscar", "landed", 180.0, SOURCE),
                # a PR of another repository under the same number space
                (PROJECT, 17, "trent", "landed", 190.0, SOURCE),
            ],
        )
        fillers = [account(1000 + i, f"filler{i}", 5000 + i) for i in range(60)]
        self.forgejo = Forgejo(
            users=fillers
            + [
                account(8, "alice", 56166236),
                account(21, "bob", 477956),
                # a local account whose name is a GitHub ID; and an LDAP account with a numeric name
                account(30, "mallory", None),
                account(31, "oscar", 999, source_id=2),
                account(40, "trent", 31337),
            ],
            pulls={
                9: pull((8, "alice")),
                7: pull((8, "alice")),
                11: pull((21, "bob")),
                15: pull((30, "mallory")),
                16: pull((31, "oscar")),
                17: pull((40, "trent"), repo_id=2),
            },
        )
        self.forgejo.users[-3]["login_name"] = "12345"
        self.dispatched = []

    def dispatcher(self, *call):
        self.dispatched.append(call)

    def run_job(self, now=10_000.0, dry_run=False, dispatcher=None):
        return claims_access.run(
            args(self.root, dry_run), now=now, get=self.forgejo, dispatcher=dispatcher or self.dispatcher
        )

    def written(self):
        return json.loads((self.root / claims_access.OUTPUT_NAME).read_text())

    def test_only_landed_github_sign_ins_are_attributed_by_poster_id(self):
        self.assertEqual(self.run_job(), 0)
        document = self.written()
        self.assertEqual(document["schema"], claims_access.SCHEMA)
        self.assertEqual(
            document["landers"],
            [{"forgejo_pr": 11, "github_id": 477956}, {"forgejo_pr": 9, "github_id": 56166236}],
        )
        # the newest landing is read first, and one confirmed PR is enough
        pulls = [url.rsplit("/", 1)[1] for url in self.forgejo.calls if "/pulls/" in url]
        self.assertEqual(pulls, ["9", "15", "11", "16", "17"])

    def test_an_unreadable_pr_falls_back_to_an_older_landing(self):
        del self.forgejo.pulls[9]
        self.run_job()
        self.assertIn({"forgejo_pr": 7, "github_id": 56166236}, self.written()["landers"])

    def test_dispatches_for_new_authors_and_hourly_otherwise(self):
        self.run_job(now=10_000.0)
        self.assertEqual(self.dispatched, [(PROJECT, "claims-access.yml", "main", None)])
        self.run_job(now=10_600.0)
        self.assertEqual(len(self.dispatched), 1)
        with closing(sqlite3.connect(self.root / "queue.sqlite3")) as c, c:
            c.execute(
                "INSERT INTO inputs (repository, number, author, state, landed_at) VALUES (?,?,?,?,?)",
                (PROJECT, 46001, "newcomer", "landed", 300.0),
            )
        self.run_job(now=11_200.0)
        self.assertEqual(len(self.dispatched), 2)
        self.run_job(now=11_200.0 + 3600)
        self.assertEqual(len(self.dispatched), 3)

    def test_a_failed_dispatch_is_retried_on_the_next_run(self):
        def broken(*_):
            raise RuntimeError("HTTP 502")

        self.assertEqual(self.run_job(dispatcher=broken), 1)
        self.assertFalse((self.root / "claims-access-state.json").exists())
        self.run_job(now=10_060.0)
        self.assertEqual(len(self.dispatched), 1)

    def test_dry_run_writes_and_dispatches_nothing(self):
        self.run_job(dry_run=True)
        self.assertFalse((self.root / claims_access.OUTPUT_NAME).exists())
        self.assertEqual(self.dispatched, [])


class Route(ControllerCase):
    def get(self, path):
        from queue_server import FakeAdapter

        config = server.ServerConfig(
            webhook_secret=b"test",
            repositories={1: PROJECT},
            adapter=FakeAdapter(),
            host="127.0.0.1",
            port=0,
            clock=self.clock,
        )
        srv = server.create_server(self.controller, config)
        server.serve_in_thread(srv)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        finally:
            srv.shutdown()
            srv.server_close()

    def test_serves_the_attribution_written_next_to_the_ledger(self):
        path = Path(self.controller.store.path).with_name(claims_access.OUTPUT_NAME)
        self.assertEqual(self.get("/claims/forgejo-landers")[0], 503)
        path.write_text(json.dumps({"schema": "something-else", "landers": [{"github_id": 1}]}))
        self.assertEqual(self.get("/claims/forgejo-landers")[0], 503)
        document = {"schema": claims_access.SCHEMA, "landers": [{"github_id": 477956, "forgejo_pr": 3}]}
        path.write_text(json.dumps(document))
        self.assertEqual(self.get("/claims/forgejo-landers"), (200, document))


if __name__ == "__main__":
    unittest.main()
