"""Complete source pagination, resource waits and safe git diff reads for primary intake."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from queue_github_adapter import BASE, HEAD, TAUCETI, Clock, FakeTransport, StaticTokens, client, pr_json

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


def fence_document(raw, *, build="SUCCESS"):
    head, base = raw["head"]["sha"], raw["base"]["sha"]
    fresh = node(raw["number"], head=head, base=base)
    fresh.update(
        state=raw["state"].upper(),
        isDraft=raw["draft"],
        title=raw["title"],
        body=raw["body"],
        updatedAt=raw.get("updated_at", ""),
        comments={"totalCount": raw.get("comments", 0)},
    )
    return {
        "data": {
            "repository": {
                "pullRequest": fresh,
                "object": {
                    "oid": head,
                    "status": {
                        "contexts": [{"context": "build", "state": build}, {"context": "scope", "state": "SUCCESS"}]
                    },
                },
            }
        }
    }


class SourceTests(unittest.TestCase):
    def test_restored_server_deadlines_protect_new_reads_and_both_api_resources(self):
        transport = FakeTransport()
        c, clock = client(transport)
        c.restore_deadlines({"core": clock.now + 3600, "secondary": clock.now + 60})
        with c.deferred_requests(urgent=True):
            with self.assertRaises(ga.RequestDeferred) as blocked:
                c.get_json("new-head")
            self.assertEqual(blocked.exception.retry_at, clock.now + 3600)
            with self.assertRaises(ga.RequestDeferred) as blocked:
                c.graphql("query { ready }")
            self.assertEqual(blocked.exception.retry_at, clock.now + 60)
        self.assertEqual(transport.calls, [])

    def test_restored_background_deadline_preserves_essential_priority(self):
        transport = FakeTransport()
        c, clock = client(transport)
        c.restore_deadlines({"core:background": clock.now + 10})
        with c.deferred_requests(), self.assertRaises(ga.RequestDeferred):
            c.get_json("background")
        transport.add("GET", "https://api.github.com/essential", 200, {})
        with c.deferred_requests(urgent=True):
            c.get_json("essential")
        self.assertEqual(len(transport.calls), 1)

    def test_graphql_bot_identity_matches_rest_for_recovery_holds_and_source_versions(self):
        body = f"Merge-queue recovery for head `{HEAD}`: repair needed\n<!--tauceti-rebase:v1 {HEAD}-->"
        transport = FakeTransport()
        c, _ = client(transport)
        adapter = ga.GitHubAdapter(c, repo=TAUCETI)
        comment = {
            "databaseId": 6007920631,
            "body": body,
            "createdAt": "2026-11-06T04:00:00Z",
            "updatedAt": "2026-11-06T04:00:00Z",
            "author": {"login": "tau-ceti-bors", "__typename": "Bot"},
        }
        observed = adapter._admission_comments(TAUCETI, 42, connection([comment]))
        rest = [
            {
                "id": comment["databaseId"],
                "body": body,
                "created_at": comment["createdAt"],
                "updated_at": comment["updatedAt"],
                "user": {"login": "tau-ceti-bors[bot]"},
            }
        ]
        source = {"head": HEAD, "state": "open"}
        self.assertEqual(
            source_version(source | {"comments": observed}, "policy"),
            source_version(source | {"comments": rest}, "policy"),
        )
        human = dict(comment, author={"login": "tau-ceti-bors", "__typename": "User"})
        human_comments = adapter._admission_comments(TAUCETI, 42, connection([human]))
        self.assertEqual(
            source_version(source | {"comments": human_comments}, "policy"),
            source_version(source | {"comments": []}, "policy"),
        )

    def test_a_callers_exhausted_quota_cannot_poison_installation_intake(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/user",
            200,
            {"login": "operator"},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800007200"},
        )
        transport.add("GET", "https://api.github.com/source", 200, {"ready": True})
        c, _ = client(transport)
        c.get_json("user", token="test-personal-bearer")
        with c.deferred_requests():
            self.assertEqual(c.get_json("source")[1], {"ready": True})
        self.assertNotIn("core", c.stats()["blocked_until"])

    def test_secondary_limit_without_retry_header_blocks_all_installation_calls(self):
        transport = FakeTransport()
        transport.add(
            "GET", "https://api.github.com/source", 403, {"message": "You have exceeded a secondary rate limit"}
        )
        c, clock = client(transport)
        with c.deferred_requests(), self.assertRaises(ga.RateLimited) as paused:
            c.get_json("source")
        self.assertEqual(paused.exception.retry_at, clock.now + 60)
        # An essential request waits for the same known deadline, without extending the
        # secondary penalty by sending another rejected request before it.
        transport.add("GET", "https://api.github.com/essential", 200, {})
        c.get_json("essential")
        self.assertEqual(clock.slept, [60])
        self.assertEqual(len(transport.calls), 2)

    def test_background_reads_run_at_full_speed_above_the_floor(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/background",
            200,
            {},
            {"x-ratelimit-remaining": "1001", "x-ratelimit-reset": "1800003600"},
        )
        c, clock = client(transport)
        with c.deferred_requests():
            for _ in range(5):
                c.get_json("background")
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual(clock.slept, [])

    def test_background_waits_for_reset_at_the_floor_and_essential_requests_continue(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/background",
            200,
            {},
            {"x-ratelimit-remaining": "1000", "x-ratelimit-reset": "1800003600"},
        )
        transport.add(
            "GET",
            "https://api.github.com/essential",
            200,
            {},
            {"x-ratelimit-remaining": "999", "x-ratelimit-reset": "1800003600"},
        )
        c, clock = client(transport)
        with c.deferred_requests():
            c.get_json("background")
            with self.assertRaises(ga.RequestDeferred) as paused:
                c.get_json("background")
        self.assertEqual(paused.exception.retry_at, 1800003601)
        self.assertEqual(paused.exception.resource, "core:background")
        c.get_json("essential")
        with c.deferred_requests(urgent=True):
            c.get_json("essential")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(clock.slept, [])
        # After the reset background reads resume at full speed.
        clock.now = paused.exception.retry_at
        transport.add(
            "GET",
            "https://api.github.com/after",
            200,
            {},
            {"x-ratelimit-remaining": "7149", "x-ratelimit-reset": "1800007200"},
        )
        with c.deferred_requests():
            c.get_json("after")
            c.get_json("after")
        self.assertEqual(len(transport.calls), 5)

    def test_the_floor_is_a_setting(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/background",
            200,
            {},
            {"x-ratelimit-remaining": "500", "x-ratelimit-reset": "1800003600"},
        )
        c, _ = client(transport)
        c.background_floor = 100
        with c.deferred_requests():
            c.get_json("background")
            c.get_json("background")
        self.assertEqual(len(transport.calls), 2)

    def test_a_changed_floor_applies_to_the_next_read(self):
        transport = FakeTransport()
        transport.add(
            "GET",
            "https://api.github.com/background",
            200,
            {},
            {"x-ratelimit-remaining": "500", "x-ratelimit-reset": "1800003600"},
        )
        c, _ = client(transport)
        with c.deferred_requests():
            c.get_json("background")
            with self.assertRaises(ga.RequestDeferred):
                c.get_json("background")
            c.background_floor = 100
            c.get_json("background")
            c.background_floor = 1000
            with self.assertRaises(ga.RequestDeferred):
                c.get_json("background")
        self.assertEqual(len(transport.calls), 2)

    def test_a_background_read_waiting_on_essential_work_sees_its_response(self):
        essential_started, release = threading.Event(), threading.Event()

        def transport(method, url, headers, body):
            if url.endswith("/essential"):
                essential_started.set()
                release.wait(5)
                return 200, {"x-ratelimit-remaining": "1000", "x-ratelimit-reset": "1800003600"}, b"{}"
            return 200, {"x-ratelimit-remaining": "999", "x-ratelimit-reset": "1800003600"}, b"{}"

        c, _ = client(transport)
        thread = threading.Thread(target=lambda: c.get_json("essential"))
        thread.start()
        essential_started.wait(5)
        outcome = {}

        def background():
            try:
                with c.deferred_requests():
                    c.get_json("background")
                outcome["sent"] = True
            except ga.RequestDeferred as error:
                outcome["deferred"] = error.retry_at

        waiting = threading.Thread(target=background)
        waiting.start()
        release.set()
        thread.join(5)
        waiting.join(5)
        self.assertEqual(outcome, {"deferred": 1800003601})

    def test_graphql_floor_uses_reported_points(self):
        transport = FakeTransport()
        transport.add(
            "POST",
            "https://api.github.com/graphql",
            200,
            {"data": {"rateLimit": {"cost": 20, "remaining": 990, "resetAt": "2027-01-15T08:01:40Z"}}},
            {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1800000100"},
        )
        c, _ = client(transport)
        with c.deferred_requests():
            c.graphql("query { rateLimit { cost remaining resetAt } }")
            with self.assertRaises(ga.RequestDeferred) as paused:
                c.graphql("query { rateLimit { cost remaining resetAt } }")
        self.assertEqual(paused.exception.resource, "graphql:background")
        self.assertEqual(paused.exception.retry_at, ga._timestamp("2027-01-15T08:01:40Z") + 1)
        self.assertEqual(len(transport.calls), 1)

    def test_snapshot_reads_without_pauses_while_quota_is_plentiful(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = FakeRepo(directory)
            base = repo.main_sha()
            head = repo.commit_branch({"TauCeti/A.lean": "-- source\n"}, "head", push_to="refs/pull/42/head")
            run_git(repo.clone, "fetch", "-q", str(repo.origin), head)
            transport = FakeTransport()
            raw = pr_json(head={"sha": head}, base={"ref": "main", "sha": base})

            def headers(remaining, reset=1800000020):
                return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset)}

            url = f"https://api.github.com/repos/{TAUCETI}"
            transport.add("GET", url + "/pulls/42", 200, raw, headers(4))
            transport.add(
                "GET",
                url + f"/compare/{base}...{head}?per_page=1",
                200,
                {"merge_base_commit": {"sha": base}},
                headers(3),
            )
            transport.add("GET", url + "/issues/42/comments?per_page=100&page=1", 200, [], headers(2))
            transport.add("POST", "https://api.github.com/graphql", 200, fence_document(raw, build="FAILURE"))
            c, clock = client(transport)
            c.background_floor = 0
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=repo.clone)
            with c.deferred_requests():
                snapshot = adapter.admission_snapshot(TAUCETI, 42, generation=1)
            self.assertEqual(snapshot.pull.head_sha, head)
            self.assertEqual(snapshot.statuses["build"], "failure")
            self.assertEqual(len(transport.calls), 4)
            self.assertEqual(clock.slept, [])

    def test_snapshot_deferred_at_the_floor_resumes_after_reset_without_repeating_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = FakeRepo(directory)
            base = repo.main_sha()
            head = repo.commit_branch({"TauCeti/A.lean": "-- source\n"}, "head", push_to="refs/pull/42/head")
            run_git(repo.clone, "fetch", "-q", str(repo.origin), head)
            transport = FakeTransport()
            raw = pr_json(head={"sha": head}, base={"ref": "main", "sha": base})

            def headers(remaining, reset=1800000020):
                return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset)}

            url = f"https://api.github.com/repos/{TAUCETI}"
            transport.add("GET", url + "/pulls/42", 200, raw, headers(4))
            transport.add(
                "GET",
                url + f"/compare/{base}...{head}?per_page=1",
                200,
                {"merge_base_commit": {"sha": base}},
                headers(3),
            )
            transport.add("GET", url + "/issues/42/comments?per_page=100&page=1", 200, [], headers(2))
            transport.add("POST", "https://api.github.com/graphql", 200, fence_document(raw, build="FAILURE"))
            c, clock = client(transport)
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=repo.clone)
            with c.deferred_requests():
                while True:
                    try:
                        snapshot = adapter.admission_snapshot(TAUCETI, 42, generation=1)
                        break
                    except ga.RequestDeferred as pending:
                        self.assertLess(pending.retry_at, 1800000040, "the wait ends at the reset")
                        clock.now = pending.retry_at
            self.assertEqual(snapshot.pull.head_sha, head)
            self.assertEqual(snapshot.statuses["build"], "failure", "CI must be read fresh after the wait")
            self.assertEqual(len(transport.calls), 4)
            self.assertEqual(clock.slept, [])

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

    def test_incomplete_node_stays_unknown_without_hiding_later_prs(self):
        transport = FakeTransport()
        bad = connection([])
        bad["pageInfo"]["hasNextPage"] = True
        transport.add("POST", "https://api.github.com/graphql", 200, graph_page([node(1, comments=bad), node(2)]))
        c, _ = client(transport)
        adapter = ga.GitHubAdapter(c, repo=TAUCETI)
        observed = []
        listed = adapter.list_open_pull_requests(TAUCETI, review_app_id=0, admission=True, on_page=observed.extend)
        self.assertEqual([p.number for p in listed], [1, 2])
        self.assertIn("incomplete", observed[0].admission)
        self.assertNotIn("incomplete", observed[1].admission)

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

    def test_label_pages_resume_under_pacing_in_discovery_and_final_fence(self):
        for final_fence in (False, True):
            with self.subTest(final_fence=final_fence):
                transport = FakeTransport()
                raw = pr_json()
                if final_fence:
                    document = fence_document(raw)
                    document["data"]["repository"]["pullRequest"]["labels"]["pageInfo"]["hasNextPage"] = True
                else:
                    pull = node(42)
                    pull["labels"]["pageInfo"]["hasNextPage"] = True
                    document = graph_page([pull])
                transport.add("POST", "https://api.github.com/graphql", 200, document)
                if final_fence:
                    transport.add("POST", "https://api.github.com/graphql", 200, document)
                url = f"https://api.github.com/repos/{TAUCETI}/issues/42/labels?per_page=100&page="
                transport.add(
                    "GET",
                    url + "1",
                    200,
                    [{"name": f"label-{n}"} for n in range(100)],
                    {"x-ratelimit-remaining": "10", "x-ratelimit-reset": "1800000010"},
                )
                transport.add("GET", url + "2", 200, [{"name": "keep"}])
                c, clock = client(transport)
                adapter = ga.GitHubAdapter(c, repo=TAUCETI)
                progress = {}

                def read_labels(final_fence=final_fence, adapter=adapter, raw=raw, progress=progress):
                    if final_fence:
                        return adapter._admission_fence(
                            TAUCETI, 42, ga.PullRequest.from_json(raw), labels_progress=progress
                        )[0].labels
                    return adapter.list_open_pull_requests(TAUCETI, review_app_id=0, admission=True)[0].admission[
                        "labels"
                    ]

                with c.deferred_requests():
                    with self.assertRaises(ga.RequestDeferred) as paused:
                        read_labels()
                    clock.now = paused.exception.retry_at
                    self.assertIn("keep", read_labels())
                self.assertEqual([call[1] for call in transport.calls if call[0] == "GET"], [url + "1", url + "2"])

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
            transport.add("POST", "https://api.github.com/graphql", 200, fence_document(raw))
            transport.add("POST", "https://api.github.com/graphql", 200, graph_page([node(42, head=head, base=base)]))
            c, _ = client(transport)
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=repo.clone)
            snapshot = adapter.admission_snapshot(TAUCETI, 42)
            self.assertEqual(set(snapshot.paths), {"scripts/untrusted.py", "TauCeti/untrusted.py"})
            self.assertFalse(marker.exists(), "diff drivers supplied through repository config must not execute")
            pulls = adapter.list_open_pull_requests(TAUCETI, review_app_id=0, admission=True)
            self.assertEqual(source_version(snapshot.source(), "policy"), source_version(pulls[0].admission, "policy"))


class BumpGuardFloorTests(unittest.TestCase):
    """PR #43422: a self-tested Mathlib bump's guard needs 19 reads, and restarts from its first."""

    REPO = "TauCetiProject/TauCeti"

    def setUp(self):
        from unittest import mock

        from tauceti_tools import self_tested

        self.self_tested = self_tested
        environment = mock.patch.dict("os.environ")
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SELF_TESTED_TEAM_TOKEN", "TAUCETI_TEAM_TOKEN", "TAUCETI_SELF_TESTED_TEAM", "TAUCETI_BASE_BRANCH"):
            os.environ.pop(name, None)
        self_tested._MEMBERS.clear()
        guard = self_tested._bump_policy()

        def run_guard(api, command, directory, env):
            for upstream in ("a", "b", "c"):
                api.request("GET", f"repos/leanprover-community/mathlib4/commits/{upstream}")
            return subprocess.CompletedProcess(command, 0, "pins accepted", "")

        patcher = mock.patch.object(guard, "run_mathlib_guard", run_guard)
        patcher.start()
        self.addCleanup(patcher.stop)

    def quota(self, clock, remaining):
        state = {"remaining": remaining, "reset": clock.now + 600, "calls": []}

        def transport(method, url, headers, body):
            if clock.now >= state["reset"]:
                state.update(remaining=7150, reset=clock.now + 3600)
            state["remaining"] -= 1
            path = url.removeprefix("https://api.github.com/")
            state["calls"].append(path)
            if "/issues/7/events" in path:
                data = [{"id": 1, "event": "labeled", "label": {"name": "self-tested"}, "actor": {"login": "kim-em"}}]
            elif "/teams/self-tested/memberships/" in path:
                data = {"state": "active"}
            elif path.endswith("/branches/main"):
                data = {"commit": {"sha": "b" * 40}}
            elif "/compare/" in path:
                data = {"merge_base_commit": {"sha": "c" * 40}}
            elif "/contents/" in path:
                name = path.split("/contents/", 1)[1].split("?", 1)[0]
                if name.startswith("lakefile"):
                    return 404, self.headers(state), b"{}"
                data = {"type": "file", "encoding": "base64", "content": base64.b64encode(b"{}").decode()}
            else:
                data = {"sha": path.rsplit("/", 1)[-1]}
            return 200, self.headers(state), json.dumps(data).encode()

        return transport, state

    @staticmethod
    def headers(state):
        return {"x-ratelimit-remaining": str(state["remaining"]), "x-ratelimit-reset": str(int(state["reset"]))}

    def pull(self):
        return {
            "number": 7,
            "state": "open",
            "labels": [{"name": "self-tested"}],
            "base": {"ref": "main"},
            "head": {"sha": "d" * 40, "ref": "bump-mathlib/2026-11-18", "repo": {"full_name": self.REPO}},
            "user": {"login": "tauceti-review-bot[bot]"},
        }

    def evaluate(self, c, clock):
        """Admission's loop: a deferred evaluation is retried from scratch at its retry time."""
        attempts = 0
        while True:
            attempts += 1
            try:
                with c.deferred_requests():
                    return attempts, self.self_tested.eligible(
                        self.pull(),
                        self.REPO,
                        paths=["lake-manifest.json", "lean-toolchain"],
                        read=lambda path: c.get_json(path)[1],
                    )
            except ga.RequestDeferred as pending:
                self.assertLessEqual(attempts, 3, "a deferred bump guard must finish after the reset")
                clock.now = pending.retry_at

    def test_bump_guard_completes_in_one_pass_while_quota_is_plentiful(self):
        clock = Clock()
        transport, state = self.quota(clock, 7000)
        c = ga.GitHubClient(StaticTokens(), transport, clock=clock, sleep=clock.sleep)
        attempts, verdict = self.evaluate(c, clock)
        self.assertEqual((attempts, verdict), (1, True))
        self.assertEqual(len(state["calls"]), 19)
        self.assertEqual(clock.slept, [])

    def test_bump_guard_near_the_floor_defers_cleanly_and_completes_after_reset(self):
        clock = Clock()
        transport, state = self.quota(clock, 1010)
        c = ga.GitHubClient(StaticTokens(), transport, clock=clock, sleep=clock.sleep)
        reset = state["reset"]
        attempts, verdict = self.evaluate(c, clock)
        self.assertEqual((attempts, verdict), (2, True))
        # The first pass stopped at the floor, spending nothing below it. The restart after the
        # reset ran the whole guard again; only the team membership came from the one-minute cache.
        self.assertEqual(len(state["calls"]), 10 + 18)
        self.assertEqual(clock.now, reset + 1)
        self.assertEqual(clock.slept, [])


class LocalMergeBaseTests(unittest.TestCase):
    """`admission_local_merge_base`: the clone answers when it can vouch for the answer."""

    def snapshot(self, *, local, shallow=False):
        with tempfile.TemporaryDirectory() as directory:
            repo = FakeRepo(directory)
            base = repo.main_sha()
            head = repo.commit_branch({"TauCeti/A.lean": "-- source\n"}, "head", push_to="refs/pull/42/head")
            run_git(repo.clone, "fetch", "-q", str(repo.origin), head)
            if shallow:
                (Path(repo.clone) / ".git" / "shallow").write_text(base + "\n")
            transport = FakeTransport()
            raw = pr_json(head={"sha": head}, base={"ref": "main", "sha": base})
            url = f"https://api.github.com/repos/{TAUCETI}"
            transport.add("GET", url + "/pulls/42", 200, raw)
            transport.add(
                "GET", url + f"/compare/{base}...{head}?per_page=1", 200, {"merge_base_commit": {"sha": base}}
            )
            transport.add("GET", url + "/issues/42/comments?per_page=100&page=1", 200, [])
            transport.add("POST", "https://api.github.com/graphql", 200, fence_document(raw))
            c, _clock = client(transport)
            c.background_floor = 0
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=repo.clone)
            adapter.local_merge_base = local
            with c.deferred_requests():
                snapshot = adapter.admission_snapshot(TAUCETI, 42, generation=1)
            compared = [call for call in transport.calls if "/compare/" in str(call)]
            return snapshot, base, compared, adapter.merge_base_sources

    def test_off_asks_compare(self):
        snapshot, base, compared, sources = self.snapshot(local=False)
        self.assertEqual(snapshot.merge_base_sha, base)
        self.assertEqual(len(compared), 1)
        self.assertEqual(sources, {"local": 0, "compare": 1})

    def test_on_answers_from_the_clone_without_compare(self):
        snapshot, base, compared, sources = self.snapshot(local=True)
        self.assertEqual(snapshot.merge_base_sha, base)
        self.assertEqual(snapshot.paths, ("TauCeti/A.lean",))
        self.assertEqual(compared, [])
        self.assertEqual(sources, {"local": 1, "compare": 0})

    def test_a_shallow_clone_falls_back_to_compare(self):
        snapshot, base, compared, sources = self.snapshot(local=True, shallow=True)
        self.assertEqual(snapshot.merge_base_sha, base)
        self.assertEqual(len(compared), 1)
        self.assertEqual(sources, {"local": 0, "compare": 1})

    def test_ambiguous_or_missing_merge_base_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            run_git(directory, "init", "-q")
            run_git(directory, "config", "user.name", "t")
            run_git(directory, "config", "user.email", "t@example.invalid")

            def commit(message, *parents):
                tree = run_git(directory, "write-tree")
                args = [arg for parent in parents for arg in ("-p", parent)]
                return run_git(directory, "commit-tree", tree, *args, "-m", message)

            root = commit("root")
            a, b = commit("a", root), commit("b", root)
            # A criss-cross: two merges of a and b, so the merge base is not unique.
            left, right = commit("left", a, b), commit("right", b, a)
            transport = FakeTransport()
            c, _clock = client(transport)
            adapter = ga.GitHubAdapter(c, repo=TAUCETI, clone_dir=directory)
            self.assertEqual(adapter._local_merge_base(a, b), root)
            self.assertIsNone(adapter._local_merge_base(left, right))
            self.assertIsNone(adapter._local_merge_base(a, "f" * 40))
            self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
