"""Complete source pagination, resource waits and safe git diff reads for primary intake."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from queue_github_adapter import BASE, HEAD, TAUCETI, FakeTransport, client, pr_json

from tauceti_queue import github_adapter as ga
from tauceti_queue.admission import source_version
from tauceti_queue.fakes import FakeRepo, run_git


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def node(number, *, comments=None, head=HEAD, base=BASE):
    return {
        "number": number,
        "headRefOid": head,
        "baseRefOid": base,
        "baseRefName": "main",
        "body": "body",
        "isDraft": False,
        "labels": connection([{"name": "ready-to-merge"}]),
        "comments": comments or connection([]),
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "status": {
                            "contexts": [
                                {"context": "build", "state": "SUCCESS"},
                                {"context": "scope", "state": "SUCCESS"},
                            ]
                        },
                    }
                }
            ]
        },
    }


def graph_page(nodes, cursor=None):
    return {"data": {"repository": {"pullRequests": connection(nodes, cursor)}}}


class SourceTests(unittest.TestCase):
    def test_essential_requests_take_priority_without_serializing_other_buckets(self):
        essential_started, release = threading.Event(), threading.Event()
        background_started, core_entered = threading.Event(), threading.Event()

        def transport(method, url, headers, body):
            if url.endswith("/essential"):
                essential_started.set()
                release.wait(3)
            if url.endswith("/background"):
                core_entered.set()
            return 200, {}, b'{"data": {"ok": true}}'

        c, _ = client(transport)
        essential = threading.Thread(target=lambda: c.get_json("essential"))

        def background():
            with c.deferred_requests():
                background_started.set()
                c.get_json("background")

        background_thread = threading.Thread(target=background)
        essential.start()
        try:
            self.assertTrue(essential_started.wait(3))
            background_thread.start()
            self.assertTrue(background_started.wait(3))
            self.assertFalse(core_entered.wait(0.05))
            with c.deferred_requests():
                self.assertEqual(c.graphql("query { ok }"), {"ok": True})
        finally:
            release.set()
            essential.join(3)
            background_thread.join(3)
        self.assertTrue(core_entered.is_set())
        self.assertFalse(essential.is_alive() or background_thread.is_alive())

    def test_entire_repository_is_paginated_without_a_work_allowance(self):
        transport = FakeTransport()
        for start in range(1, 1201, 100):
            transport.add(
                "POST",
                "https://api.github.com/graphql",
                200,
                graph_page([node(n) for n in range(start, start + 100)], str(start + 100) if start < 1101 else None),
            )
        c, _ = client(transport)
        adapter = ga.GitHubAdapter(c, repo=TAUCETI)
        observed = []
        pulls = adapter.list_open_pull_requests(
            TAUCETI, review_app_id=0, admission=True, on_page=lambda page: observed.extend(p.number for p in page)
        )
        self.assertEqual([p.number for p in pulls], list(range(1, 1201)))
        self.assertEqual(observed, list(range(1, 1201)))
        self.assertEqual(len(transport.calls), 12)

    def test_nested_comments_resume_at_the_resource_wait_and_then_finish_repository(self):
        transport = FakeTransport()
        first = {"databaseId": 1, "body": "old comment", "createdAt": "t1", "updatedAt": "t1", "url": "url"}
        second = dict(first, databaseId=2, body="<!--tauceti-scoreboard-->", updatedAt="t2")
        transport.add(
            "POST",
            "https://api.github.com/graphql",
            200,
            graph_page([node(1, comments=connection([first], "comments-1"))], "prs-1"),
        )
        transport.add(
            "POST",
            "https://api.github.com/graphql",
            200,
            {"errors": [{"type": "RATE_LIMITED", "message": "wait"}]},
            {"x-ratelimit-reset": "1800003600"},
        )
        transport.add(
            "POST",
            "https://api.github.com/graphql",
            200,
            {"data": {"repository": {"pullRequest": {"comments": connection([second])}}}},
        )
        transport.add("POST", "https://api.github.com/graphql", 200, graph_page([node(2)]))
        c, clock = client(transport)
        adapter = ga.GitHubAdapter(c, repo=TAUCETI)
        observed = []
        with c.deferred_requests():
            with self.assertRaises(ga.RateLimited):
                adapter.list_open_pull_requests(
                    TAUCETI, review_app_id=0, admission=True, on_page=lambda page: observed.extend(page)
                )
            self.assertEqual(observed, [], "a partial nested connection is not a completed PR observation")
            clock.now += 3601
            pulls = adapter.list_open_pull_requests(
                TAUCETI, review_app_id=0, admission=True, on_page=lambda page: observed.extend(page)
            )
        self.assertEqual([p.number for p in pulls], [1, 2])
        self.assertEqual([p.number for p in observed], [1, 2])
        self.assertEqual([c["id"] for c in pulls[0].admission["comments"]], [1, 2])
        variables = [json.loads(call[3])["variables"] for call in transport.calls]
        self.assertEqual([v["after"] for v in variables], [None, "comments-1", "comments-1", "prs-1"])
        self.assertEqual(clock.slept, [])

    def test_incomplete_nested_connection_never_finishes_discovery(self):
        transport = FakeTransport()
        bad = connection([])
        bad["pageInfo"]["hasNextPage"] = True
        transport.add("POST", "https://api.github.com/graphql", 200, graph_page([node(1, comments=bad)]))
        c, _ = client(transport)
        adapter = ga.GitHubAdapter(c, repo=TAUCETI)
        observed = []
        with self.assertRaises(ga.GitHubError):
            adapter.list_open_pull_requests(TAUCETI, review_app_id=0, admission=True, on_page=observed.extend)
        self.assertEqual(observed, [])

    def test_rest_pagination_keeps_read_pages_across_quota_wait(self):
        transport = FakeTransport()
        transport.add("GET", "https://api.github.com/items?per_page=2&page=1", 200, [1, 2])
        transport.add("GET", "https://api.github.com/items?per_page=2&page=2", 429, {}, {"Retry-After": "7200"})
        transport.add("GET", "https://api.github.com/items?per_page=2&page=2", 200, [3])
        c, clock = client(transport)
        progress = {}
        with c.deferred_requests():
            with self.assertRaises(ga.RateLimited):
                c.paginate("items", per_page=2, progress=progress)
            clock.now += 7200
            self.assertEqual(c.paginate("items", per_page=2, progress=progress), [1, 2, 3])
        self.assertEqual(len(transport.calls), 3)

    def test_real_diff_includes_old_rename_path_and_source_versions_match_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = FakeRepo(directory, files={"scripts/untrusted.py": "print('unused')\n"})
            base = repo.main_sha()
            head = repo.commit_branch(
                {"scripts/untrusted.py": None, "TauCeti/untrusted.py": "print('unused')\n"},
                "rename",
                start=base,
                push_to="refs/pull/42/head",
            )
            run_git(repo.clone, "fetch", "-q", str(repo.origin), head)
            marker = Path(directory) / "executed"
            driver = Path(directory) / "diff-driver"
            driver.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
            driver.chmod(0o755)
            run_git(repo.clone, "config", "diff.external", str(driver))
            transport = FakeTransport()
            raw = pr_json(head={"sha": head}, base={"ref": "main", "sha": base})
            transport.add("GET", f"https://api.github.com/repos/{TAUCETI}/pulls/42", 200, raw)
            transport.add(
                "GET",
                f"https://api.github.com/repos/{TAUCETI}/compare/{base}...{head}?per_page=1",
                200,
                {"merge_base_commit": {"sha": base}},
            )
            transport.add(
                "GET", f"https://api.github.com/repos/{TAUCETI}/issues/42/comments?per_page=100&page=1", 200, []
            )
            transport.add(
                "GET",
                f"https://api.github.com/repos/{TAUCETI}/commits/{head}/statuses?per_page=100&page=1",
                200,
                [{"context": "build", "state": "success"}, {"context": "scope", "state": "success"}],
            )
            transport.add("POST", "https://api.github.com/graphql", 200, graph_page([node(42, head=head, base=base)]))
            c, _ = client(transport)
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=repo.clone)
            snapshot = adapter.admission_snapshot(TAUCETI, 42)
            self.assertEqual(set(snapshot.paths), {"scripts/untrusted.py", "TauCeti/untrusted.py"})
            self.assertFalse(marker.exists(), "diff drivers supplied through repository config must not execute")
            pulls = adapter.list_open_pull_requests(TAUCETI, review_app_id=0, admission=True)
            self.assertEqual(source_version(snapshot.source(), "policy"), source_version(pulls[0].admission, "policy"))


if __name__ == "__main__":
    unittest.main()
