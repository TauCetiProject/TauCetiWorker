#!/usr/bin/env python3
"""Independent workflow-clock decisions preserve active work and require authoritative reads."""

import contextlib
import fcntl
import io
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import workflow_backstop as wb

NOW = 1_800_000_000
ONE = (wb.Workflow("update.yml", 3600),)


def run(age=0, status="completed", branch="main"):
    return {
        "status": status,
        "head_branch": branch,
        "head_repository": {"id": 12},
        "event": "workflow_dispatch",
        "created_at": datetime.fromtimestamp(NOW - age, UTC).isoformat(),
    }


class FakeGitHub:
    def __init__(self):
        self.info = {"id": 12, "default_branch": "main", "archived": False, "disabled": False}
        self.enabled = {"state": "active"}
        self.latest = []
        self.active = {}
        self.sent = []
        self.fail_dispatch = False
        self.fail_workflow = None

    def repository(self):
        return self.info

    def workflow(self, name):
        if name == self.fail_workflow:
            raise wb.ObservationError("read unavailable")
        return self.enabled

    def runs(self, _name, _branch, status=None, *, repo_id):
        assert repo_id == self.info["id"]
        return self.latest if status is None else self.active.get(status, [])

    def dispatch(self, name, branch):
        self.sent.append((name, branch))
        if self.fail_dispatch:
            raise wb.ObservationError("request result unknown")
        return "https://github.com/org/repo/actions/runs/123"


class Decisions(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.state = {"version": 1, "repo_id": None, "dispatches": {}}

    def cycle(self, *, now=NOW, execute=True, policies=ONE, **kwargs):
        with patch.object(wb, "WORKFLOWS", policies):
            return wb.reconcile(self.api, self.state, clock=lambda: now, execute=execute, **kwargs)

    def test_overdue_dispatch_is_recorded_and_not_repeated_before_visibility(self):
        self.assertEqual(self.cycle()[0]["decision"], "dispatched")
        self.assertEqual(self.state["dispatches"]["update.yml"]["accepted_at"], NOW)
        self.assertEqual(self.cycle(now=NOW + 30)[0]["decision"], "recent")
        self.assertEqual(len(self.api.sent), 1)

    def test_preview_has_no_dispatch_or_identity_mutation(self):
        self.assertEqual(self.cycle(execute=False)[0]["decision"], "would-dispatch")
        self.assertIsNone(self.state["repo_id"])
        self.assertEqual(self.api.sent, [])

    def test_active_work_is_preserved_even_if_very_old(self):
        for status in wb.ACTIVE:
            with self.subTest(status=status):
                self.api.latest = [run(100_000, status)]
                self.assertEqual(self.cycle()[0]["decision"], "active")
        self.assertEqual(self.api.sent, [])

    def test_older_active_run_outside_latest_history_is_preserved(self):
        self.api.latest = [run(4000)]
        self.api.active["waiting"] = [run(100_000, "waiting")]
        self.assertEqual(self.cycle()[0]["decision"], "active")
        self.assertEqual(self.api.sent, [])

    def test_recent_failed_or_cancelled_attempt_does_not_create_retry_storm(self):
        self.api.latest = [run(30)]
        self.assertEqual(self.cycle()[0]["decision"], "recent")
        self.assertEqual(self.api.sent, [])

    def test_disabled_workflow_is_not_reenabled_or_dispatched(self):
        self.api.enabled = {"state": "disabled_manually"}
        self.assertEqual(self.cycle()[0]["decision"], "disabled")
        self.assertEqual(self.api.sent, [])

    def test_missing_enabled_state_defers(self):
        self.api.enabled = {}
        self.assertEqual(self.cycle()[0]["decision"], "deferred")
        self.assertEqual(self.api.sent, [])

    def test_one_failed_observation_does_not_block_other_workflows(self):
        self.api.fail_workflow = "update.yml"
        rows = self.cycle(policies=(*ONE, wb.Workflow("merge-sweep.yml", 3600)))
        self.assertEqual([r["decision"] for r in rows], ["deferred", "dispatched"])
        self.assertEqual(self.api.sent, [("merge-sweep.yml", "main")])

    def test_unknown_post_result_has_cooldown_and_is_reobserved(self):
        self.api.fail_dispatch = True
        self.assertEqual(self.cycle()[0]["decision"], "deferred")
        self.assertEqual(self.cycle(now=NOW + 10)[0]["decision"], "uncertain-cooldown")
        self.api.latest = [run(0, "queued")]
        self.assertEqual(self.cycle(now=NOW + 301)[0]["decision"], "active")
        self.assertEqual(len(self.api.sent), 1)

    def test_default_branch_is_authoritative(self):
        self.api.info["default_branch"] = "release"
        self.cycle()
        self.assertEqual(self.api.sent, [("update.yml", "release")])

    def test_repository_identity_change_fails_closed(self):
        self.state["repo_id"] = 99
        with self.assertRaises(wb.ObservationError):
            self.cycle()
        self.assertEqual(self.api.sent, [])

    def test_future_or_invalid_creation_time_defers(self):
        for value in ("invalid", "2026-01-01", run(-1000)["created_at"]):
            with self.subTest(value=value):
                self.api.latest = [{**run(), "created_at": value}]
                self.assertEqual(self.cycle()[0]["decision"], "deferred")
        self.assertEqual(self.api.sent, [])

    def test_alert_posting_requires_explicit_opt_in(self):
        rows = wb.reconcile(self.api, self.state, clock=lambda: NOW)
        self.assertNotIn("stuck-alerts.yml", [r["workflow"] for r in rows])
        rows = wb.reconcile(self.api, self.state, clock=lambda: NOW, include_alerts=True)
        self.assertIn("stuck-alerts.yml", [r["workflow"] for r in rows])
        self.assertEqual(self.api.sent, [])

    def test_persistence_failure_stops_further_dispatches(self):
        def persist(state):
            if state["dispatches"]:
                raise OSError("disk full")

        with self.assertRaises(OSError):
            self.cycle(policies=(*ONE, wb.Workflow("merge-sweep.yml", 3600)), persist=persist)
        self.assertEqual(self.api.sent, [("update.yml", "main")])


class StorageAndAuthority(unittest.TestCase):
    def test_incomplete_api_page_is_not_an_empty_history(self):
        api = wb.GitHub("org/repo")
        for page in (
            {},
            {"workflow_runs": [], "total_count": 1},
            {"workflow_runs": [], "total_count": None},
            {"workflow_runs": [run()], "total_count": 0},
        ):
            with self.subTest(page=page), patch.object(api, "read", return_value=page):
                with self.assertRaises(wb.ObservationError):
                    api.runs("update.yml", "main", repo_id=12)

    def test_wrong_branch_or_status_filter_is_rejected(self):
        api = wb.GitHub("org/repo")
        for row, status in ((run(branch="other"), None), (run(status="completed"), "queued")):
            with patch.object(api, "read", return_value={"total_count": 1, "workflow_runs": [row]}):
                with self.assertRaises(wb.ObservationError):
                    api.runs("update.yml", "main", status, repo_id=12)

    def test_same_named_fork_branch_and_pr_events_cannot_count_as_maintenance(self):
        api = wb.GitHub("org/repo")
        for row in (
            {**run(), "head_repository": {"id": 99}},
            {**run(), "head_repository": None},
            {**run(), "event": "pull_request"},
            {**run(), "event": "pull_request_target"},
        ):
            with (
                self.subTest(row=row),
                patch.object(api, "read", return_value={"total_count": 1, "workflow_runs": [row]}),
            ):
                with self.assertRaises(wb.ObservationError):
                    api.runs("update.yml", "main", repo_id=12)

    def test_pr_run_cannot_obscure_older_maintenance(self):
        api = wb.GitHub("org/repo")
        pr = {**run(), "head_repository": {"id": 99}, "event": "pull_request"}

        def response(_path, **params):
            rows = (
                [pr]
                if "event" not in params
                else ([{**run(4000), "event": "schedule"}] if params["event"] == "schedule" else [])
            )
            return {"total_count": len(rows), "workflow_runs": rows}

        with patch.object(api, "read", side_effect=response) as read:
            rows = api.runs("update.yml", "main", repo_id=12)
        self.assertEqual(rows, [{**run(4000), "event": "schedule"}])
        self.assertEqual([call.kwargs.get("event") for call in read.call_args_list], [None, *wb.EVENTS])

    def test_common_maintenance_read_uses_one_request(self):
        api = wb.GitHub("org/repo")
        with patch.object(api, "read", return_value={"total_count": 1, "workflow_runs": [run()]}) as read:
            self.assertEqual(api.runs("update.yml", "main", repo_id=12), [run()])
        read.assert_called_once_with("repos/org/repo/actions/workflows/update.yml/runs", branch="main", per_page="1")

    def test_state_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = wb.load_state(path)
            state["repo_id"] = 12
            wb.atomic(path, state)
            self.assertEqual(wb.load_state(path), state)
            path.write_text("{}")
            with self.assertRaises(wb.ObservationError):
                wb.load_state(path)

    def test_symlink_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("{}")
            path = Path(directory) / "state.json"
            path.symlink_to(target)
            with self.assertRaises(wb.ObservationError):
                wb.load_state(path)

    def test_preview_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new/state.json"
            with patch.object(wb, "GitHub", return_value=FakeGitHub()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wb.main(["--repo", "org/repo", "--state", str(path)]), 0)
            self.assertFalse(path.parent.exists())

    def test_lock_prevents_two_local_dispatchers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with open(str(path) + ".lock", "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(wb, "GitHub") as api, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(wb.main(["--repo", "org/repo", "--state", str(path), "--execute"]), 0)
                api.assert_not_called()

    def test_corrupt_state_never_contacts_github(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json")
            with patch.object(wb, "GitHub") as api, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wb.main(["--repo", "org/repo", "--state", str(path), "--execute"]), 1)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
