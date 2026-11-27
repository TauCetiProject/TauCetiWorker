#!/usr/bin/env python3
"""Statistics use positive landing receipts and exact rolling window boundaries."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from queue_controller import PROJECT, ControllerCase

from tauceti_queue import server
from tauceti_queue.statistics import landings, snapshot


class Statistics(ControllerCase):
    def report(self):
        return snapshot(self.controller.store.path, PROJECT, "main", now=self.clock())

    def land(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.clock.advance(10)
        self.github.complete_runs()
        from tauceti_queue.controller import VERDICT_CONTEXTS

        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[-1].head_sha, "context": context})
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": 1})["state"], "landed")

    def test_landing_and_ci_are_separate_metrics_without_provider_calls(self):
        self.land()
        self.github.fail_next["fetch_refs"] = 100
        d = self.report()
        json.dumps(d)
        for w in d["windows"]:
            self.assertEqual((w["landed_batches"], w["landed_prs"], w["ci_passed"]), (1, 1, 1))
        self.assertEqual(d["last_landing"]["batch_id"], 1)
        self.assertIn("not build-only", d["duration_definition"])

    def test_window_boundary_and_future_landing_are_excluded(self):
        self.land()
        self.clock.advance(3600)
        d = self.report()
        self.assertEqual(d["windows"][0]["landed_prs"], 0)
        self.assertEqual(d["windows"][1]["landed_prs"], 1)
        self.controller.store.update("batches", {"id": 1}, landed_at=self.clock() + 10)
        self.assertTrue(all(w["landed_batches"] == 0 for w in self.report()["windows"]))

    def test_empty_ledger_has_no_fabricated_landing_or_duration(self):
        d = self.report()
        self.assertIsNone(d["last_landing"])
        self.assertTrue(all(w["landed_prs"] == 0 and w["run_p50_seconds"] is None for w in d["windows"]))

    def test_removed_withdrawn_or_missing_receipt_members_never_count(self):
        self.land()
        self.controller.store.update("members", {"number": 101}, withdrawn=1)
        self.assertEqual(self.report()["windows"][0]["landed_prs"], 0)
        self.controller.store.update("members", {"number": 101}, withdrawn=0)
        self.controller.store.update("inputs", {"number": 101}, landed_at=None)
        self.assertEqual(self.report()["windows"][0]["landed_prs"], 0)

    def test_statistics_route_is_public_and_answers_over_http(self):
        import http.client

        from queue_server import FakeAdapter

        self.land()
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
            connection.request("GET", "/statistics")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["windows"][0]["landed_prs"], 1)
            connection.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_http_route_uses_authoritative_ledger_and_public_repository(self):
        self.land()
        from types import SimpleNamespace

        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(
            controller=self.controller,
            source_authority=None,
            config=SimpleNamespace(repositories={1: PROJECT}, clock=self.clock),
        )
        self.assertEqual(handler._statistics()["windows"][0]["landed_prs"], 1)
        handler.server.source_authority = object()
        with self.assertRaises(ValueError):
            handler._statistics()


class Landings(ControllerCase):
    """Per-PR landing and first-approval times, for the opening-to-land breakdown."""

    land = Statistics.land

    def feed(self, days=1):
        return landings(self.controller.store.path, PROJECT, days=days, now=self.clock())

    def test_a_landed_pr_reports_its_first_approval_and_landing(self):
        approved = self.clock()
        self.land()
        d = self.feed()
        json.dumps(d)
        self.assertEqual(d["fields"], ["forge", "number", "first_approved_at", "landed_at"])
        [(forge, number, first, landed)] = d["landings"]
        self.assertEqual((forge, number), ("github", 101))
        self.assertAlmostEqual(first, approved, places=2)
        row = self.controller.store.row("inputs", {"number": 101})
        self.assertAlmostEqual(landed, row["landed_at"], places=2)
        self.assertLessEqual(first, landed)

    def test_an_earlier_admission_of_another_head_is_the_first_approval(self):
        self.land()
        row = self.controller.store.row("inputs", {"number": 101})
        earlier = row["approved_at"] - 500
        self.controller.store.insert(
            "inputs",
            repository=PROJECT,
            number=101,
            head_sha="0" * 40,
            state="cancelled",
            approved_at=earlier,
            reason="superseded-by-new-approval",
        )
        [(_, _, first, _)] = self.feed()["landings"]
        self.assertAlmostEqual(first, earlier, places=2)

    def test_window_and_receipts_bound_the_feed(self):
        self.land()
        self.clock.advance(86400 + 1)
        self.assertEqual(self.feed(1)["landings"], [])
        self.assertEqual(len(self.feed(2)["landings"]), 1)
        self.controller.store.update("inputs", {"number": 101}, landed_at=None)
        self.assertEqual(self.feed(2)["landings"], [])

    def test_a_forgejo_input_is_named_by_its_forge(self):
        self.land()
        identity = json.dumps({"forge": {"kind": "forgejo", "url": "https://git.example"}, "id": 1, "name": PROJECT})
        self.controller.store.update("inputs", {"number": 101}, source_identity=identity)
        self.assertEqual(self.feed()["landings"][0][0], "forgejo")

    def test_a_second_receipt_does_not_move_a_landing_into_the_window(self):
        self.land()
        row = self.controller.store.row("inputs", {"number": 101})
        self.clock.advance(2 * 86400)
        self.controller.store.insert(
            "inputs",
            repository=PROJECT,
            number=101,
            head_sha="1" * 40,
            state="landed",
            approved_at=self.clock() - 60,
            landed_at=self.clock() - 30,
        )
        self.assertEqual(self.feed(1)["landings"], [])
        [(_, _, _, landed)] = self.feed(3)["landings"]
        self.assertAlmostEqual(landed, row["landed_at"], places=2)

    def test_the_same_number_on_two_forges_is_two_pull_requests(self):
        self.land()
        row = self.controller.store.row("inputs", {"number": 101})
        identity = json.dumps({"forge": {"kind": "forgejo", "url": "https://git.example"}, "id": 1, "name": PROJECT})
        self.controller.store.insert(
            "inputs",
            repository=PROJECT,
            number=101,
            head_sha="2" * 40,
            state="landed",
            approved_at=row["approved_at"] + 5,
            landed_at=row["landed_at"],
            source_identity=identity,
        )
        self.assertEqual(sorted(e[0] for e in self.feed()["landings"]), ["forgejo", "github"])

    def test_another_repository_s_inputs_are_not_listed(self):
        self.land()
        row = self.controller.store.row("inputs", {"number": 101})
        self.controller.store.insert(
            "inputs",
            repository="Other/Repo",
            number=7,
            head_sha="3" * 40,
            state="landed",
            approved_at=row["approved_at"],
            landed_at=row["landed_at"],
        )
        self.assertEqual([e[1] for e in self.feed()["landings"]], [101])

    def test_a_malformed_stored_identity_is_a_server_error(self):
        self.land()
        for identity in ("not json", '{"forge": [1]}'):
            self.controller.store.update("inputs", {"number": 101}, source_identity=identity)
            with self.subTest(identity=identity), self.assertRaises(RuntimeError):
                self.feed()

    def test_coverage_starts_no_earlier_than_the_ledger(self):
        self.land()
        first = self.controller.store.row("batches", {"id": 1})["created_at"]
        self.assertEqual(self.feed(30)["since"], first)
        self.clock.advance(3 * 86400)
        self.assertEqual(self.feed(1)["since"], self.clock() - 86400)

    def test_days_are_bounded(self):
        for days in (0, 36, "7"):
            with self.assertRaises(ValueError):
                self.feed(days)

    def test_landings_route_answers_over_http(self):
        import http.client

        from queue_server import FakeAdapter

        self.land()
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
            connection.request("GET", "/statistics/landings?days=2")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
            self.assertEqual((body["days"], body["landings"][0][1]), (2, 101))
            connection.request("GET", "/statistics/landings?days=x")
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
