#!/usr/bin/env python3
"""Statistics use positive landing receipts and exact rolling window boundaries."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from queue_controller import PROJECT, ControllerCase

from tauceti_queue import server
from tauceti_queue.statistics import snapshot


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


if __name__ == "__main__":
    unittest.main()
