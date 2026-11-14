#!/usr/bin/env python3
"""Activity counts are local, replay-safe, and never fall back to upstream reads."""

import contextlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity as a
from tauceti_worker import fleet_events


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_ACTIVITY_DIR": self.tmp.name,
                "TAUCETI_FLEET_EVENTS": self.tmp.name + "/worker-events.jsonl",
                "TAUCETI_BUDGET_DIR": self.tmp.name + "/budget",
                "TAUCETI_ACTIVITY_CI_DB": self.tmp.name + "/ci-events.sqlite",
                "TAUCETI_FLEET_MACHINE_ID": "machine",
                "TAUCETI_WORKER_ID": "worker",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, a, "_round", None)
        from tauceti_worker import work_recovery

        recovery_home = patch.object(work_recovery, "HERE", Path(self.tmp.name) / "worker")
        recovery_home.start()
        self.addCleanup(recovery_home.stop)
        self.now = time.time()

    def test_collector_keeps_minute_cadence_without_overlapping_or_replaying_missed_ticks(self):
        from tauceti_worker import processes, worker_scopes

        clock = [0.0]
        starts, waits = [], []
        durations = iter([40, 82, 3, 1, 2])
        exits = iter([0, 0, 1, -signal.SIGTERM, -signal.SIGKILL])

        class Stop:
            def is_set(self):
                return len(waits) == 5

            def wait(self, delay):
                waits.append(delay)
                clock[0] += delay

        def refresh(argv, **kwargs):
            self.assertEqual(
                argv[-5:], ["activity", "refresh", "--json", "--config", str(Path(self.tmp.name) / "workers.toml")]
            )
            self.assertEqual(kwargs["env"]["TAUCETI_ACTIVITY_DIR"], self.tmp.name)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertTrue(kwargs["check"])
            starts.append(clock[0])
            clock[0] += next(durations)
            returncode = next(exits)
            if returncode:
                raise subprocess.CalledProcessError(returncode, argv)

        # Run the actual collector body synchronously with a controlled clock.
        # Thread creation is the only scheduling boundary replaced here.
        def start_thread(**kwargs):
            return SimpleNamespace(start=kwargs["target"])

        with (
            patch.object(a.threading, "Event", return_value=Stop()),
            patch.object(a.threading, "Thread", side_effect=start_thread),
            patch.object(a.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(processes, "run", side_effect=refresh),
            patch.object(a, "atomic_json", wraps=a.atomic_json) as publish,
            patch.object(worker_scopes, "collect_oom_events", return_value=False),
        ):
            a.start_collector(Path(self.tmp.name) / "workers.toml")
        self.assertEqual(starts, [0, 60, 142, 202, 262])
        self.assertEqual(waits, [20, 0, 57, 59, 58])
        self.assertEqual(publish.call_count, 2, "failed/OOM children report errors; intentional SIGTERM does not")
        self.assertEqual(a.read_json(a.root() / "preparation-error.json")["error"], "CalledProcessError")

    def test_live_sampling_continues_while_historical_refresh_is_blocked(self):
        from tauceti_worker import activity_live, processes, worker_scopes

        entered, release, sampled = threading.Event(), threading.Event(), threading.Event()

        def refresh(*args, **kwargs):
            entered.set()
            release.wait(5)

        with (
            patch.object(processes, "run", side_effect=refresh),
            patch.object(worker_scopes, "collect_oom_events", return_value=False),
            patch.object(activity_live, "sample", side_effect=sampled.set),
        ):
            stop = a.start_collector(None)
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(sampled.wait(2), "live sampling must not wait for the historical subprocess")
            finally:
                stop.set()
                release.set()
                for thread in threading.enumerate():
                    if thread.name in {"activity-preparation", "live-spending"}:
                        thread.join(2)

    def test_collector_owner_exit_cleans_refresh_descendants_and_releases_locks(self):
        import fcntl

        from tauceti_worker.paths import self_env

        marker = a.root() / "child-started"
        locks = [a.root() / "prepare.lock", a.root() / "helper.lock"]
        helper = (
            "import fcntl,sys,time;from pathlib import Path;"
            "lock=open(sys.argv[1],'a');fcntl.flock(lock,fcntl.LOCK_EX);"
            "Path(sys.argv[2]).touch();time.sleep(60)"
        )
        refresh = (
            "import fcntl,subprocess,sys;"
            "lock=open(sys.argv[1],'a');fcntl.flock(lock,fcntl.LOCK_EX);"
            f"subprocess.run([sys.executable,'-c',{helper!r},sys.argv[2],sys.argv[3]],check=True)"
        )
        argv = [sys.executable, "-c", refresh, *map(str, locks), str(marker)]
        wait_for_child = f"while not Path({str(marker)!r}).exists(): time.sleep(.01)"
        owner_code = (
            "import os,time;from pathlib import Path;from tauceti_worker import activity,paths,worker_scopes;"
            f"paths.self_argv=lambda *args:{argv!r};"
            "worker_scopes.collect_oom_events=lambda *args:False;activity.start_collector(None);"
            f"exec({wait_for_child!r});os._exit(0)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", owner_code],
            env=self_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        ) as owner:
            try:
                owner.wait(timeout=15)
            except BaseException:
                owner.kill()
                raise
            self.assertEqual(owner.returncode, 0)
        self.assertTrue(marker.exists(), "refresh helper never acquired its lock")
        for path in locks:
            with path.open("a") as lock:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail(f"orphaned refresh process still holds {path.name}")
                        time.sleep(0.01)

    def test_refresh_child_inherits_activity_root_and_respects_existing_preparation_lock(self):
        import fcntl

        from tauceti_worker.paths import self_argv, self_env

        published = dict(version=1, prepared_at=self.now, all={"marker": "previous publication"}, errors=[])
        a.atomic_json(a.root() / "summary.json", published)
        with (a.root() / "prepare.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.run(
                self_argv("activity", "refresh", "--json", "--config", str(a.root() / "workers.toml")),
                env=self_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        self.assertEqual(json.loads(child.stdout)["summary"], published["all"])
        self.assertEqual(a.read_json(a.root() / "summary.json"), published)
        self.assertFalse((a.root() / "activity.sqlite").exists())

    def finish(self, age, kind="review", outcome="completed", rid=None):
        rid = rid or str(age)
        with patch.object(a.time, "time", return_value=self.now - age):
            a.emit("round_finished", key=rid, round_id=rid, round_type=kind, outcome=outcome)

    def test_receipts_stay_durable_and_missing_derived_views_rebuild_from_ingested_facts(self):
        from tauceti_worker import runtime_status

        with patch.object(runtime_status.os, "fsync", wraps=os.fsync) as sync:
            self.assertTrue(
                a.emit("round_finished", round_id="durable-round", round_type="review", outcome="completed")
            )
            self.assertEqual(sync.call_count, 1)
        with patch("tauceti_worker.activity_live.atomic_json", wraps=a.atomic_json) as publish:
            a.prepare(now=self.now + 1)
        views = [call for call in publish.call_args_list if call.args[0].name != "preparation-error.json"]
        self.assertTrue(views)
        self.assertTrue(all(call.kwargs.get("durable") is False for call in views), views)
        self.assertFalse(list((a.root() / "pending").glob("*.json")))
        for path in (a.root() / "summary.json", a.scope_path("workers", "worker"), a.scope_path("groups", "machine")):
            path.write_text("")  # Simulate a disposable cache lost during a power failure.
        self.assertFalse(a.cached()["available"])
        a.prepare(now=self.now + 2)
        for selection in ({}, {"worker": "worker"}, {"group": "machine"}):
            restored = a.cached(**selection)
            self.assertTrue(restored["available"], restored)
            self.assertEqual(restored["summary"]["rounds"]["total"]["review"]["finished"], 1)

    def test_nondurable_view_still_replaces_atomically_and_cleans_failed_temp_file(self):
        from tauceti_worker import runtime_status

        path = a.root() / "view.json"
        a.atomic_json(path, {"old": True})
        with (
            patch.object(runtime_status.os, "fsync", side_effect=AssertionError("derived view forced to disk")),
            patch.object(runtime_status.os, "replace", side_effect=OSError("rename failed")),
            self.assertRaises(OSError),
        ):
            a.atomic_json(path, {"new": True}, durable=False)
        self.assertEqual(a.read_json(path), {"old": True})
        self.assertFalse(list(a.root().glob(".view.json.*")))

    def test_upgrade_preserves_usage_history_and_ignores_old_containment_projection(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("CREATE TABLE containment(boundary TEXT PRIMARY KEY, cleanup TEXT)")
            db.execute("INSERT INTO containment VALUES ('historical', 'namespace-unconfirmed')")
            db.execute("INSERT INTO conversation_costs VALUES ('paid', 'worker', 'machine', 1, 2, '17.25')")
        a.emit("containment_result", boundary="old", cleanup="namespace-unconfirmed")
        with contextlib.closing(a._connect()) as db, db:
            a._consume(db, [])
            self.assertEqual(
                tuple(db.execute("SELECT * FROM containment").fetchone()), ("historical", "namespace-unconfirmed")
            )
            self.assertEqual(db.execute("SELECT cost FROM conversation_costs WHERE id='paid'").fetchone()[0], "17.25")

    def test_host_cache_metrics_are_in_prepared_group_status(self):
        from tauceti_worker import expanded_cache

        a.atomic_json(a.root() / "cleanup.json", dict(at=1, managed_bytes=1024, protected_bytes=512))
        a.emit("round_started", worker="one", group="host-one", round_id="round-one")
        with patch.object(expanded_cache, "summary", return_value=dict(workers=1, hits=10, expanded=2, seconds=3)):
            value = a.prepare()
        self.assertEqual(value["groups"]["host-one"]["storage_cleanup"]["managed_bytes"], 1024)
        self.assertEqual(a.cached(group="host-one")["summary"]["expanded_cache"]["hits"], 10)

    def test_table_aligns_all_rows_and_lists_zero_count_work_types(self):
        self.finish(10, kind="fix")
        a.prepare(now=self.now)
        value = a.cached()
        self.assertEqual(value["summary"]["round_types"], list(a.ROUND_TYPES))
        value["summary"]["rounds"]["total"]["fix"]["finished"] = 12345678
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(value)
        lines = output.getvalue().splitlines()
        header = next(line for line in lines if line.startswith("Finished rounds"))
        expected_ends = [header.index(h) + len(h) for h in ("1h", "3h", "24h", "7d", "All time")]
        data_rows = [line for line in lines if re.search(r"\d+\s+\d+\s+\d+\s+\d+$", line)]
        for line in data_rows:
            self.assertEqual([m.end() for m in re.finditer(r"\d+", line)], expected_ends, line)
        for kind in ("roadmap", "review", "fix-ci", "progress", "security-audit", "no-work"):
            line = next(line for line in data_rows if line.strip().startswith(kind + " "))
            self.assertEqual(re.findall(r"\d+", line), ["0"] * 5)
        self.assertNotIn("unrecorded", output.getvalue())
        self.assertNotIn("Older resource logs", output.getvalue())
        self.assertNotIn("totals cover", output.getvalue())
        self.assertNotIn("Outcome rows are subsets", output.getvalue())
        pr_start = next(i for i, line in enumerate(lines) if line.startswith("PRs authored"))
        self.assertEqual(lines[pr_start - 1], "")

    def test_mathlib_worker_message_states_survive_fleet_aggregation(self):
        a.prepare(now=self.now)
        one, two, older = a.cached(), a.cached(), a.cached()
        private_reason = "Mathlib dependency is not available for sharing"
        clear_reason = "sharing was skipped because the Mathlib checkout was absent."
        idle_reason = "shared view released; awaiting next natural round"
        one["summary"]["shared_mathlib"] = dict(
            private=2,
            idle=14,
            reasons=[private_reason, idle_reason],
            reasons_by_mode={"private": [private_reason], "idle": [idle_reason]},
        )
        two["summary"]["shared_mathlib"] = dict(
            private=1, reasons=[clear_reason], reasons_by_mode={"private": [clear_reason]}
        )
        older["summary"]["shared_mathlib"] = dict(private=1, reasons=["legacy reason"])
        for value in (one, a.activity_summary.combine([one, two, older])):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertEqual(output.getvalue().count(f"  Private builds: {clear_reason}\n"), 1)
            self.assertNotIn(idle_reason, output.getvalue())
            self.assertNotIn(f"  {private_reason}\n", output.getvalue())
        self.assertIn("  Worker message (state unavailable): legacy reason\n", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(older)
        self.assertIn("  Worker message (state unavailable): legacy reason\n", output.getvalue())
        older["summary"]["shared_mathlib"] = dict(idle=1, reasons=[idle_reason])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(a.activity_summary.combine([older]))
        self.assertNotIn(idle_reason, output.getvalue())
        older["summary"]["shared_mathlib"] = dict(private=1, reasons=[private_reason, idle_reason])
        for value in (older, a.activity_summary.combine([older])):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertIn(f"  Private builds: {clear_reason}\n", output.getvalue())
            self.assertNotIn(idle_reason, output.getvalue())
        older["summary"]["shared_mathlib"] = dict(blocked=1, reasons=[private_reason])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(older)
        self.assertIn(
            "  Blocked builds: the Mathlib checkout was absent, but sharing is required.\n", output.getvalue()
        )
        older["summary"]["shared_mathlib"]["private"] = 1
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(older)
        self.assertIn(f"  Worker message (state unavailable): {private_reason}\n", output.getvalue())

    def test_housekeeping_failure_does_not_abort_activity_refresh(self):
        from tauceti_worker import work_recovery

        with patch.object(work_recovery, "housekeep", side_effect=OSError("fixture housekeeping failure")):
            value = a.prepare(now=self.now)
        self.assertTrue(a.cached()["available"])
        self.assertTrue(any("Recovery housekeeping deferred" in error for error in value["errors"]))

    def test_installed_collector_does_not_stop_refresh_from_deleting(self):
        from tauceti_worker import retention, work_recovery

        # A stalled timer run once stopped all deletion for a day; refresh keeps deleting regardless.
        a.atomic_json(a.root() / "cleanup-service.json", dict(installed=True))
        with (
            patch.object(work_recovery, "housekeep", return_value=dict(cleanup_pending=True)) as cleanup,
            patch.object(retention, "request_cleanup") as request,
        ):
            a.prepare(now=self.now)
        cleanup.assert_called_once_with(cleanup=True, **retention.COLLECTOR_SLICE)
        request.assert_called_once()
        self.assertTrue(a.cached()["available"])

    def test_without_collector_refresh_runs_a_collector_slice(self):
        from tauceti_worker import retention, work_recovery

        with (
            patch.object(work_recovery, "housekeep", return_value=dict(cleanup_pending=True)) as cleanup,
            patch.object(retention, "request_cleanup") as request,
        ):
            a.prepare(now=self.now)
        cleanup.assert_called_once_with(cleanup=True, **retention.COLLECTOR_SLICE)
        request.assert_not_called()
        self.assertTrue(a.cached()["available"])

    def discarded_tree(self, files):
        from tauceti_worker import work_recovery

        tree = work_recovery._state_root() / "worker1" / "recovery-cleanup" / "discarded" / "abandoned-fixture"
        (tree / ".lake" / "build").mkdir(parents=True)
        for index in range(files):
            (tree / ".lake" / "build" / f"{index}.olean").write_bytes(b"x" * 65536)
        return tree

    def test_without_collector_refresh_drains_discarded_trees_past_the_tick_budget(self):
        # 6000 entries and ~390 MiB exceed one default tick (4096 entries, 256 MiB).
        tree = self.discarded_tree(6000)
        a.prepare(now=self.now)
        self.assertFalse(tree.exists())
        self.assertTrue(a.cached()["available"])

    def test_without_collector_refresh_honors_the_recovery_rollback_switch(self):
        tree = self.discarded_tree(3)
        with patch.dict(os.environ, {"TAUCETI_WORK_RECOVERY": "off"}):
            a.prepare(now=self.now)
        self.assertTrue(tree.exists())

    def test_installed_collector_still_lets_refresh_drain_discarded_trees(self):
        from tauceti_worker import retention

        tree = self.discarded_tree(3)
        a.atomic_json(a.root() / "cleanup-service.json", dict(installed=True))
        with patch.object(retention, "request_cleanup"):
            a.prepare(now=self.now)
        self.assertFalse(tree.exists())

    def test_discarded_trees_drain_while_another_process_holds_housekeeping(self):
        import fcntl

        from tauceti_worker import work_recovery

        tree = self.discarded_tree(3)
        base = work_recovery._state_root()
        with open(base / ".recovery-housekeeping.lock", "a+") as held:  # e.g. a collector stuck in I/O
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            a.prepare(now=self.now)
        self.assertFalse(tree.exists())

    def test_a_locked_discarded_entry_blocks_only_itself(self):
        import fcntl

        from tauceti_worker import work_recovery

        stuck = self.discarded_tree(3)
        other = stuck.with_name("abandoned-other")
        (other / ".lake").mkdir(parents=True)
        (other / ".lake/A.olean").write_bytes(b"x")
        with open(stuck.with_name(stuck.name + ".lock"), "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            work_recovery.housekeep(cleanup=True, seconds=5, entries=100000)
        self.assertTrue(stuck.exists())
        self.assertFalse(other.exists())

    def test_locked_discarded_entries_stay_reported_as_backlog(self):
        import fcntl

        from tauceti_worker import work_recovery

        stuck = self.discarded_tree(3)
        with open(stuck.with_name(stuck.name + ".lock"), "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = work_recovery.housekeep(cleanup=True, seconds=5, entries=100000)
        self.assertTrue(report["cleanup_pending"])

    def test_orphan_sidecars_are_dropped_and_a_bad_queue_blocks_no_other(self):
        from tauceti_worker import work_recovery

        tree = self.discarded_tree(3)
        queue = tree.parent
        (queue / "gone-entry.json").write_text("{}")  # its tree was removed, the cursor survived
        (queue / "gone-entry.lock").write_text("")
        broken = work_recovery._state_root() / "worker0" / "recovery-cleanup" / "discarded"
        broken.mkdir(parents=True)
        broken.chmod(0)  # an unreadable queue, sorted before worker1
        try:
            work_recovery.housekeep(cleanup=True, seconds=5, entries=100000)
        finally:
            broken.chmod(0o700)
        self.assertFalse(tree.exists())
        self.assertFalse((queue / "gone-entry.json").exists())
        self.assertFalse((queue / "gone-entry.lock").exists())

    def test_current_work_counts_distinct_workers_not_unfinished_receipts(self):
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            for key, worker in (("one", "w1"), ("duplicate", "w1"), ("two", "w2")):
                db.execute(
                    "INSERT INTO rounds(id,worker,scope,kind,started) VALUES (?,?,?,'fix',?)",
                    (key, worker, "machine", self.now - 5),
                )
            summary = a._summary(db, self.now, "scope", "machine")
        self.assertEqual(summary["running"], 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=summary))
        self.assertIn("Currently: 2 workers working;", output.getvalue())

    def test_windows_total_failures_and_idempotent_replays(self):
        for age in (10, 3600, 3601, 10800, 10801, 86400, 86401, 604800, 604801):
            self.finish(age)
        self.finish(10)  # Same receipt, not an additional round.
        self.finish(20, kind="fix", outcome="timeout")
        a.prepare(now=self.now)
        s = a.cached(group="machine")["summary"]
        self.assertEqual([s["rounds"][w]["review"]["finished"] for w in a.WINDOWS], [2, 4, 6, 8, 9])
        self.assertEqual(s["rounds"]["3h"]["fix"]["failed"], 1)
        a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["rounds"], s["rounds"])

    def test_round_lifecycle_selection_no_work_waits_and_interruption(self):
        r = a.Round("worker")
        r.select("roadmap")
        r.finish()
        r.finish()
        r = a.Round("worker")
        r.rc = 76
        r.finish()
        for code in (80, 81):
            r = a.Round("worker")
            r.rc = code
            r.finish()
        from tauceti_worker.config import ClaimCollision

        r = a.Round("worker")
        r.finish(ClaimCollision("peer owns this PR"))
        r = a.Round("worker")
        r.finish()
        a.emit("round_started", round_id="killed", pid=999999999, hostname=__import__("socket").gethostname())
        a.prepare(now=time.time())
        s = a.cached(worker="worker")["summary"]
        self.assertEqual(s["rounds"]["total"]["roadmap"]["finished"], 1)
        self.assertEqual(s["rounds"]["total"]["no-work"]["finished"], 1)
        self.assertEqual(s["interrupted_unfinished"], 1)
        self.assertEqual(s["running"], 0)

    def test_deferred_creation_keeps_origin_and_reconciles_without_moving_finish(self):
        from tauceti_worker import github_outbox as outbox

        r = a.Round("worker")
        r.select("roadmap")
        original = r.id
        with patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": self.tmp.name + "/shared", "GH_TOKEN": "fixture"}):
            env = dict(os.environ)
            argv = ["gh", "pr", "create", "--repo", "org/repo", "--head", "author:branch", "--body", "proof"]
            path = outbox.prepare(argv, env, self.tmp.name)
            outbox.finish(path, subprocess.CompletedProcess(argv, 1, "", "rate limit exceeded"), before_request=True)
            a.observation("unconfirmed")
            r.finish()
            a.prepare(now=self.now + 1)
            with contextlib.closing(a._connect()) as db:
                before = dict(db.execute("SELECT * FROM rounds WHERE id=?", (original,)).fetchone())
                self.assertEqual(before["outcome"], "unconfirmed")
            # The retry runs in a different round, with a different current host
            # identity. The retained intent owns attribution, not the drainer.
            with (
                patch.dict(
                    os.environ, {"TAUCETI_ACTIVITY_ROUND_ID": "later-round", "TAUCETI_FLEET_MACHINE_ID": "later-host"}
                ),
                patch.object(
                    outbox.tr,
                    "execute",
                    return_value=subprocess.CompletedProcess(argv, 0, "https://github.com/org/repo/pull/7", ""),
                ),
            ):
                self.assertEqual(outbox.drain(env), [])
        a.prepare(now=self.now + 2)
        with contextlib.closing(a._connect()) as db:
            after = dict(db.execute("SELECT * FROM rounds WHERE id=?", (original,)).fetchone())
            self.assertEqual(after, {**before, "outcome": "completed"})
            self.assertIsNone(db.execute("SELECT * FROM rounds WHERE id='later-round'").fetchone())
            self.assertEqual(
                tuple(db.execute("SELECT worker,scope FROM prs WHERE number=7").fetchone()), ("worker", "machine")
            )
        self.assertEqual(a.cached()["summary"]["rounds"]["total"]["roadmap"], dict(finished=1, failed=0, unknown=0))

    def test_publication_evidence_is_order_independent_and_conservative(self):
        import itertools

        base = dict(version=1, worker="worker", group="machine")
        for n, order in enumerate(itertools.permutations(("start", "finish", "publication"))):
            rid = f"ordering-{n}"
            receipts = dict(
                start=dict(kind="round_started", at=self.now - 20),
                finish=dict(kind="round_finished", round_type="roadmap", outcome="unconfirmed", at=self.now - 10),
                publication=dict(kind="pr_created", repository="org/repo", pr=n + 10, at=self.now),
            )
            with contextlib.closing(a._connect()) as db, db:
                for key in (*order, *order):  # Both delivery order and duplicate replay.
                    a._ingest(db, {**base, **receipts[key], "id": rid + key, "round_id": rid})
                row = db.execute("SELECT outcome,finished FROM rounds WHERE id=?", (rid,)).fetchone()
                self.assertEqual(tuple(row), ("completed", self.now - 10))
        cases = (
            ("unconfirmed", "roadmap", "worker", "machine", "pr_created", {}, "completed"),
            ("unknown", "fix", "worker", "machine", "pr_activity", dict(activity="branch_push"), "completed"),
            ("unconfirmed", "fix", "worker", "machine", "pr_activity", dict(publication="closure"), "completed"),
            ("unconfirmed", "fix", "worker", "machine", "pr_activity", dict(publication="reply"), "unconfirmed"),
            ("unconfirmed", "fix", "worker", "machine", "pr_created", {}, "unconfirmed"),
            ("unconfirmed", "roadmap", "peer", "machine", "pr_created", {}, "unconfirmed"),
            ("unconfirmed", "roadmap", "worker", "other", "pr_created", {}, "unconfirmed"),
            ("failed", "roadmap", "worker", "machine", "pr_created", {}, "failed"),
            ("timeout", "roadmap", "worker", "machine", "pr_created", {}, "timeout"),
            ("no-progress", "roadmap", "worker", "machine", "pr_created", {}, "no-progress"),
        )
        with contextlib.closing(a._connect()) as db, db:
            for n, (outcome, phase, worker, scope, kind, fields, expected) in enumerate(cases):
                rid = f"conservative-{n}"
                a._ingest(
                    db,
                    {
                        **base,
                        "id": rid,
                        "round_id": rid,
                        "kind": "round_finished",
                        "round_type": phase,
                        "outcome": outcome,
                        "at": self.now - 10,
                    },
                )
                a._ingest(
                    db,
                    {
                        **base,
                        "id": rid + "pub",
                        "round_id": rid,
                        "kind": kind,
                        "repository": "org/repo",
                        "pr": n + 100,
                        "worker": worker,
                        "group": scope,
                        "at": self.now,
                        **fields,
                    },
                )
                self.assertEqual(db.execute("SELECT outcome FROM rounds WHERE id=?", (rid,)).fetchone()[0], expected)
            # A process that never emitted a finish still has no trusted finish time.
            a._ingest(
                db, {**base, "id": "unfinished", "round_id": "unfinished", "kind": "round_started", "at": self.now - 10}
            )
            a._ingest(
                db,
                {
                    **base,
                    "id": "unfinished-pub",
                    "round_id": "unfinished",
                    "kind": "pr_created",
                    "repository": "org/repo",
                    "pr": 200,
                    "at": self.now,
                },
            )
            self.assertEqual(
                tuple(db.execute("SELECT finished,outcome FROM rounds WHERE id='unfinished'").fetchone()), (None, None)
            )

    def test_lost_creation_acknowledgement_reconciles_original_round(self):
        from tauceti_worker import github_outbox as outbox

        with patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": self.tmp.name + "/shared", "GH_TOKEN": "fixture"}):
            r = a.Round("worker")
            r.select("roadmap")
            env = dict(os.environ)
            argv = ["gh", "pr", "create", "--repo", "org/repo", "--head", "author:branch", "--body", "proof"]
            path = outbox.prepare(argv, env, self.tmp.name)
            outbox.finish(path, subprocess.CompletedProcess(argv, 1, "", "lost response"))
            a.observation("unconfirmed")
            r.finish()
            data = {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "url": "https://github.com/org/repo/pull/7",
                                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "headRepositoryOwner": {"login": "author"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
            with patch.object(
                outbox.tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")
            ) as read:
                self.assertEqual(outbox.drain(env), [])
                self.assertEqual(read.call_count, 1)
                self.assertEqual(read.call_args.args[0][:3], ["gh", "api", "graphql"])
        a.prepare(now=self.now + 1)
        self.assertEqual(a.cached()["summary"]["rounds"]["total"]["roadmap"], dict(finished=1, failed=0, unknown=0))

    def test_legacy_publication_does_not_borrow_the_draining_round(self):
        with patch.dict(os.environ, {"TAUCETI_ACTIVITY_ROUND_ID": "unrelated"}):
            fleet_events.mutation(
                {"argv": ["pr", "create"]}, SimpleNamespace(stdout="https://github.com/org/repo/pull/7")
            )
        receipts = [json.loads(p.read_text()) for p in (a.root() / "pending").glob("*.json")]
        self.assertEqual(len(receipts), 1)
        self.assertNotIn("round_id", receipts[0])

    def test_pr_creation_is_recorded_without_fleet_journal(self):
        with patch.dict(os.environ, {"TAUCETI_FLEET_EVENTS": ""}):
            fleet_events.mutation(
                {"argv": ["pr", "create"]}, SimpleNamespace(stdout="https://github.com/org/repo/pull/7")
            )
        with patch.object(a, "observe", side_effect=AssertionError("No configured endpoint")):
            a.prepare(now=self.now + 1)
        s = a.cached()["summary"]
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        self.assertEqual(s["unknown_prs"], 1)

    def test_missing_finishes_show_start_windows_latest_round_and_scope(self):
        # These were all discovered together. Recent counts must use their
        # recorded starts, never pretend discovery is the interruption time.
        with contextlib.closing(a._connect()) as db, db:
            for rid, age, scope in (
                ("recent", 60, "machine"),
                ("hour-boundary", 3600, "machine"),
                ("day-boundary", 86400, "machine"),
                ("old", 86401, "machine"),
                ("unknown-start", None, "machine"),
                ("other-host", 1, "other"),
            ):
                db.execute(
                    "INSERT INTO rounds(id,worker,scope,kind,started,outcome) "
                    "VALUES (?,'worker',?,'fix',?,'interrupted-unfinished')",
                    (rid, scope, self.now - age if age is not None else None),
                )
        a.prepare(now=self.now)
        value = a.cached(group="machine")
        s = value["summary"]
        self.assertEqual(s["interrupted_unfinished"], 5)
        self.assertEqual(s["rounds"]["total"], {})
        self.assertEqual(
            s["interrupted_unfinished_details"],
            dict(started_1h=2, started_24h=3, latest=dict(round_id="recent", worker="worker", started=self.now - 60)),
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(value)
        self.assertIn("Round reporting gaps: 2 in 1h; 3 in 24h (by start time)", output.getvalue())
        self.assertIn("latest start 1m ago", output.getvalue())
        self.assertIn("worker, round recent", output.getvalue())
        self.assertIn("excluded from finished totals", output.getvalue())
        self.assertNotIn("all time", output.getvalue())

        # A delayed finish reconciles the round rather than leaving a permanent
        # interruption warning or counting it in both categories.
        self.finish(0, kind="fix", rid="recent")
        a.prepare(now=self.now)
        s = a.cached(group="machine")["summary"]
        self.assertEqual(s["interrupted_unfinished"], 4)
        self.assertEqual(s["interrupted_unfinished_details"]["started_1h"], 1)
        self.assertEqual(s["interrupted_unfinished_details"]["latest"]["round_id"], "hour-boundary")
        self.assertEqual(s["rounds"]["total"]["fix"]["finished"], 1)

    def test_missing_finish_fleet_totals_and_older_summaries(self):
        from tauceti_worker import activity_summary

        a.prepare(now=self.now)
        one, two = a.cached(), a.cached()
        for value, count, age, rid in ((one, 3, 20, "one"), (two, 5, 10, "two")):
            value["summary"]["interrupted_unfinished"] = count
            value["summary"]["interrupted_unfinished_details"] = dict(
                started_1h=1, started_24h=2, latest=dict(started=self.now - age, worker=rid, round_id=rid)
            )
        combined = activity_summary.combine([one, two])
        self.assertEqual(combined["summary"]["interrupted_unfinished"], 8)
        detail = combined["summary"]["interrupted_unfinished_details"]
        self.assertEqual((detail["started_1h"], detail["started_24h"]), (2, 4))
        self.assertEqual(detail["latest"]["round_id"], "two")

        del two["summary"]["interrupted_unfinished_details"]
        for values in ([two], [one, two], [one, dict(available=False)]):
            value = activity_summary.combine(values)
            self.assertNotIn("interrupted_unfinished_details", value["summary"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertIn("historical; recent counts unavailable", output.getvalue())
            self.assertNotIn("latest start", output.getvalue())

    def test_old_reporting_gaps_are_quiet_and_recent_history_is_compact(self):
        self.finish(0)
        a.prepare(now=self.now)
        value = a.cached()
        s = value["summary"]
        s["interrupted_unfinished"] = 176
        detail = dict(
            started_1h=0,
            started_24h=14,
            latest=dict(started=self.now - 13 * 3600, worker="worker", round_id="long-round-identity"),
        )
        s["interrupted_unfinished_details"] = detail
        with patch("tauceti_worker.activity_summary.time.time", return_value=self.now):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertIn(
                "Round reporting gaps: 0 in 1h; 14 in 24h (by start time); latest start 13h ago", output.getvalue()
            )
            self.assertNotIn("long-round-identity", output.getvalue())
            detail["started_24h"] = 0
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertNotIn("Round reporting gaps", output.getvalue())
        self.assertEqual(s["interrupted_unfinished"], 176)

    def test_transport_wait_is_not_counted_as_a_failed_round(self):
        from tauceti_worker.config import Die

        r = a.Round("worker")
        with patch("tauceti_worker.github_transport.blocking_event_since", return_value={"reset": self.now + 60}):
            r.finish(Die("GitHub quota wait"))
        a.prepare(now=time.time())
        self.assertEqual(a.cached()["summary"]["rounds"]["total"], {})

    def test_missing_stale_and_failed_server_do_not_fallback_or_retry_early(self):
        a.emit("pr_created", repository="org/repo", pr=7)
        a.emit("pr_created", repository="org/repo", pr=8)
        env = {
            "TAUCETI_GITHUB_STATE_URL": "https://state.example",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": "/not-read-by-mock",
        }
        response = dict(prs=[dict(number=7, state="OPEN", observed_at=(self.now - 10000) * 1000)], missing=[8])
        with patch.dict(os.environ, env), patch.object(a, "observe", return_value=response) as observation:
            a.prepare(now=self.now + 1)
            a.prepare(now=self.now + 61)
            self.assertEqual(observation.call_count, 1)
        s = a.cached()["summary"]
        self.assertEqual(s["open_prs"], 1)
        self.assertEqual(s["unknown_prs"], 1)
        self.assertLess(s["pr_observed_at"], self.now - 9000)
        with patch.dict(os.environ, env), patch.object(a, "observe", side_effect=OSError("offline")) as observation:
            a.prepare(now=self.now + 302)
            a.prepare(now=self.now + 362)
            self.assertEqual(observation.call_count, 1)
        self.assertEqual(a.cached()["summary"]["open_prs"], 1)

    def test_missing_loc_uses_exact_git_comparison_in_normal_refresh(self):
        a.emit("pr_created", repository="org/repo", pr=7)
        head, base = "a" * 40, "b" * 40
        response = dict(
            prs=[
                dict(number=7, state="OPEN", observed_at=self.now * 1000, head_sha=head, base_sha=base, lean_loc=None)
            ],
            missing=[],
        )
        env = {
            "TAUCETI_GITHUB_STATE_URL": "https://state.example",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": "/not-read-by-mock",
        }
        with (
            patch.dict(os.environ, env),
            patch.object(a, "observe", return_value=response),
            patch.object(
                a.activity_git, "measure", return_value={7: dict(added=17, deleted=3, head_sha=head)}
            ) as measure,
        ):
            a.prepare(now=self.now + 1)
            a.prepare(now=self.now + 61)
        self.assertEqual(measure.call_count, 1)
        with contextlib.closing(a._connect()) as db:
            row = db.execute("SELECT head,added,deleted FROM pr_loc WHERE repo='org/repo' AND number=7").fetchone()
            self.assertEqual(tuple(row), (head, 17, 3))

    def test_read_and_display_do_not_open_database_or_network(self):
        self.finish(10)
        a.prepare(now=self.now)
        with (
            patch.object(a, "_connect", side_effect=AssertionError("SQL on read")),
            patch.object(a, "observe", side_effect=AssertionError("network on read")),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(a.cached())
        self.assertIn("All time", output.getvalue())

    def test_repository_access_failure_is_explicit_without_github_fallback(self):
        a.emit("pr_created", repository="org/roadmap", pr=7)
        env = {
            "TAUCETI_GITHUB_STATE_URL": "https://state.example",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": "/not-read-by-mock",
        }
        error = a.urllib.error.HTTPError("https://state.example/v1/activity", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch.dict(os.environ, env), patch.object(a, "observe", side_effect=error) as observation:
            a.prepare(now=self.now + 1)
            a.prepare(now=self.now + 61)
        self.assertEqual(observation.call_count, 1)
        self.assertIn(
            "Shared PR observations unavailable for org/roadmap (HTTP 403; repository unavailable or access denied); retaining last-known data",
            a.cached()["errors"],
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(a.cached())
        self.assertIn(
            "PR reporting: org/roadmap access denied (HTTP 403); open/landed counts may be stale", output.getvalue()
        )
        self.assertIn("Retrying in background", output.getvalue())

    def test_observation_errors_expire_without_refresh_work_and_survive_retry_delay(self):
        a.emit("pr_created", repository="org/roadmap", pr=7)
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('observation_errors',?)",
                (json.dumps({"org/repo": "orphan warning", "org/roadmap": "real warning"}),),
            )
        with patch.object(a, "observe", side_effect=AssertionError("No retry before due")):
            a.prepare(now=self.now + 60)
        self.assertEqual(a.cached()["errors"], ["real warning"])
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE report_pr_refresh SET next_at=NULL WHERE repo='org/roadmap'")
        a.prepare(now=self.now + 61)
        self.assertEqual(a.cached()["errors"], [])

    def test_repository_warning_only_reaches_affected_workers_and_groups(self):
        self.finish(0)
        a.emit("pr_created", repository="TauCetiProject/TauCeti", pr=1)
        a.emit(
            "pr_activity", repository="org/repo", pr=7, worker="other-worker", group="other-machine", activity="review"
        )
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('observation_errors',?)",
                (json.dumps({"org/repo": "unrelated repository access failure"}),),
            )
        a.prepare(now=self.now + 60)
        self.assertEqual(a.cached(worker="worker")["errors"], [])
        self.assertEqual(a.cached(group="machine")["errors"], [])
        self.assertEqual(a.cached(worker="other-worker")["errors"], ["unrelated repository access failure"])
        self.assertEqual(a.cached(group="other-machine")["errors"], ["unrelated repository access failure"])
        self.assertEqual(a.cached()["errors"], ["unrelated repository access failure"])

    def test_pr_freshness_excludes_settled_history_and_respects_scope(self):
        old = self.now - 3 * 86400
        with contextlib.closing(a._connect()) as db, db:
            for number, state, landed, observed, created, worker, scope in (
                (1, "CLOSED", None, old, old, "worker", "machine"),
                (2, "OPEN", old, old, old, "worker", "machine"),
                (3, "MERGED", old, old, old, "worker", "machine"),
                (4, "OPEN", None, self.now - 2700, old, "worker", "machine"),
                (5, None, None, None, self.now - 300, "worker", "machine"),
                (6, "OPEN", None, old, old, "other", "other-machine"),
            ):
                db.execute(
                    "INSERT INTO prs(repo,number,state,landed,observed,created,worker,scope) VALUES ('org/repo',?,?,?,?,?,?,?)",
                    (number, state, landed, observed, created, worker, scope),
                )
            for column, identity in (("scope", "machine"), ("worker", "worker")):
                summary = a._summary(db, self.now, column, identity)
                self.assertEqual(summary["pr_observed_at"], old)  # Raw historical evidence is preserved.
                self.assertEqual(
                    summary["pr_freshness"], dict(observed_at=self.now - 2700, missing_since=self.now - 300)
                )
                with (
                    patch.object(a.time, "time", return_value=self.now),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    a.display(dict(available=True, summary=summary))
                self.assertNotIn("PR evidence:", out.getvalue())
            self.assertEqual(a._summary(db, self.now)["pr_freshness"]["observed_at"], old)

    def test_pr_evidence_warns_only_after_two_hours_without_refreshing(self):
        with contextlib.closing(a._connect()) as db, db:
            summary = a._summary(db, self.now)
        summary["pr_observed_at"] = self.now - 3 * 86400
        summary["pr_checked_at"] = self.now  # A recent read attempt cannot renew stored evidence.
        for freshness, expected in (
            (None, None),  # Legacy summaries cannot distinguish current data from history.
            (dict(observed_at=None, missing_since=None), None),
            (dict(observed_at=self.now - 7200, missing_since=self.now - 7200), None),
            (dict(observed_at=self.now - 7201, missing_since=None), "oldest unresolved PR observation 2h 0m"),
            (dict(observed_at=None, missing_since=self.now - 47 * 3600), "without an observation; created 47h 0m"),
        ):
            with self.subTest(freshness=freshness):
                if freshness is None:
                    summary.pop("pr_freshness", None)
                else:
                    summary["pr_freshness"] = freshness
                with (
                    patch.object(a.time, "time", return_value=self.now),
                    patch.object(a, "_connect", side_effect=AssertionError("SQL on display")),
                    patch.object(a, "observe", side_effect=AssertionError("network on display")),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    a.display(dict(available=True, summary=summary))
                if expected:
                    self.assertIn(expected, out.getvalue())
                    self.assertIn("stale; expected within 2h", out.getvalue())
                else:
                    self.assertNotIn("PR evidence:", out.getvalue())

    def test_pr_freshness_combines_hosts_without_using_legacy_history(self):
        with contextlib.closing(a._connect()) as db, db:
            summary = a._summary(db, self.now)
        legacy = dict(summary)
        legacy.pop("pr_freshness")
        legacy["pr_observed_at"] = self.now - 3 * 86400
        recent = dict(summary, pr_freshness=dict(observed_at=self.now - 2700, missing_since=None))
        stale = dict(summary, pr_freshness=dict(observed_at=self.now - 10800, missing_since=self.now - 86400))
        for hosts, expected in (
            ([legacy, recent], recent["pr_freshness"]),
            ([recent, stale, legacy], stale["pr_freshness"]),
            ([legacy], dict(observed_at=None, missing_since=None)),
        ):
            combined = a.activity_summary.combine([dict(available=True, summary=s) for s in hosts])
            self.assertEqual(combined["summary"]["pr_freshness"], expected)
        # Age is evaluated when reading the cache, even if it crosses the limit
        # without another preparation pass.
        with (
            patch.object(a.time, "time", return_value=self.now + 7201),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            a.display(a.activity_summary.combine([dict(available=True, summary=recent)]))
        self.assertIn("oldest unresolved PR observation", out.getvalue())

    def test_bors_requires_exact_head_and_verified_main_landing(self):
        a.emit("pr_created", repository="org/repo", pr=7)
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET state='CLOSED',head='right'")
        path = Path(self.tmp.name) / "ci.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE merge_builds(repo TEXT,members_json TEXT,head_sha TEXT,landed INTEGER);CREATE TABLE main_landings(repo TEXT,head_sha TEXT,landed_at TEXT);"
            )
            db.execute(
                "INSERT INTO merge_builds VALUES (?,?,?,1)",
                ("org/repo", json.dumps([dict(pr=7, head_sha="wrong")]), "batch"),
            )
            db.execute("INSERT INTO main_landings VALUES ('org/repo','batch','2026-10-19T00:00:00Z')")
        with patch.dict(os.environ, TAUCETI_ACTIVITY_BORS_DB=str(path)):
            a.prepare(now=self.now)
            self.assertEqual(a.cached()["summary"]["unknown_landings"], 0)
            self.assertEqual(a.cached()["summary"]["pr_windows"]["total"]["landed"], 0)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE merge_builds SET members_json=?", (json.dumps([dict(pr=7, head_sha="right")]),))
            a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["pr_windows"]["total"]["landed"], 1)

    def test_normal_refresh_imports_coordination_after_cursor_migration(self):
        journal = Path(os.environ["TAUCETI_FLEET_EVENTS"])
        event = dict(
            version=1,
            kind="review_coordination",
            id="native-start",
            at=self.now,
            worker="worker",
            machine_id="machine",
            git_held=True,
        )
        journal.write_text(json.dumps(event) + "\n")
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO cursors VALUES (?,?,?)",
                (str(journal), journal.stat().st_ino, journal.stat().st_size),
            )
            db.execute("DELETE FROM meta WHERE key='coordination_journal_schema'")
        a.prepare(now=self.now)
        a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["local_coordination"]["24h"], dict(started=1, git_held=1))

    def test_backfill_ignores_resource_receipts(self):
        logs = Path(self.tmp.name) / "logs" / "worker"
        logs.mkdir(parents=True)
        path = logs / "resources.jsonl"
        rows = [
            dict(worker_id="worker", sampled_at=self.now, round=dict(id="old")),
            dict(worker_id="worker", sampled_at=self.now, round=dict(id="new", activity_id="native")),
        ]
        path.write_text("".join(json.dumps(v) + "\n" for v in rows))
        self.finish(1, rid="native")
        with patch("tauceti_worker.paths.HERE", Path(self.tmp.name)):
            a.prepare(now=self.now, import_history=True)
            a.prepare(now=self.now, import_history=True)
        s = a.cached()["summary"]
        self.assertEqual(sum(r["finished"] for r in s["rounds"]["total"].values()), 1)

    def test_history_cutoff_keeps_native_rounds_and_preserves_older_pr_identities(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT INTO rounds(id,worker,scope,kind,finished,outcome) VALUES ('resource:old','worker','machine','unknown',?,'unknown')",
                (self.now - 80,),
            )
            db.execute("INSERT INTO cursors VALUES ('/old/logs/worker/resources.jsonl',1,100)")
        with patch.object(a.time, "time", return_value=self.now - 60):
            a.emit("round_started", key="native:start", round_id="native")
        self.finish(30, rid="native")
        with patch.object(a.time, "time", return_value=self.now - 100):
            a.emit("pr_created", repository="org/repo", pr=7)
        with patch.object(a.time, "time", return_value=self.now - 10):
            a.emit("pr_created", repository="org/repo", pr=8)
        a.prepare(now=self.now)
        s = a.cached()["summary"]
        self.assertEqual(s["history_since"], self.now - 60)
        self.assertEqual(s["rounds"]["total"], {"review": dict(finished=1, failed=0, unknown=0)})
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        # Imported resource rounds remain excluded; older PR identities survive.
        with contextlib.closing(a._connect()) as db, db:
            db.execute("INSERT INTO rounds(id,kind) VALUES ('resource:replayed','unknown')")
            db.execute("INSERT INTO prs(repo,number,created) VALUES ('org/repo',9,?)", (self.now - 100,))
            db.execute("INSERT INTO prs(repo,number,created) VALUES ('org/repo',11,?)", (self.now - 1,))
            db.execute("UPDATE prs SET created=? WHERE number=11", (self.now - 100,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
            cutoff = db.execute("SELECT value FROM meta WHERE key='history_cutoff'").fetchone()[0]
        a.emit("round_finished", round_id="resource:pending", round_type="unknown", outcome="unknown")
        with patch.object(a.time, "time", return_value=self.now - 100):
            a.emit("pr_created", repository="org/repo", pr=10)
        a.prepare(now=self.now + 1, import_history=True)
        s = a.cached()["summary"]
        self.assertEqual(s["history_since"], self.now - 60)
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        with contextlib.closing(a._connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prs").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='history_cutoff'").fetchone()[0], cutoff)

    def test_landing_window_migration_replays_discarded_prs_without_double_counting(self):
        receipt = dict(
            version=1,
            id="old-pr",
            kind="pr_created",
            repository="org/repo",
            pr=7,
            at=self.now - 100,
            worker="worker",
            group="machine",
        )
        journal = Path(os.environ["TAUCETI_FLEET_EVENTS"])
        journal.write_text(json.dumps(receipt) + "\n")
        self.finish(60)
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            # Simulate a collector that consumed this receipt, then discarded
            # its PR because it was opened before local round recording began.
            db.execute("DELETE FROM prs")
            db.execute("DELETE FROM meta WHERE key='landing_window_schema'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE id='old-pr'").fetchone()[0], 1)
        for _ in range(2):
            a.prepare(now=self.now + 1)
        with contextlib.closing(a._connect()) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prs").fetchone()[0], 1)
            db.execute("UPDATE prs SET landed=?,head='h',state='CLOSED'", (self.now,))
            db.execute("INSERT INTO pr_loc VALUES ('org/repo',7,'h',77,0,?)", (self.now,))
            s = a._summary(db, self.now + 1)
        self.assertEqual(s["pr_windows"]["total"]["authored"], 0)
        self.assertEqual(s["costs"]["windows"]["24h"]["landed_loc_added"], 77)

    def test_observation_client_has_no_redirect_or_direct_api_fallback(self):
        import threading
        import urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer

        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header("Location", "https://api.github.com/repos/org/repo")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = Path(self.tmp.name) / "token"
        token.write_text("client")
        try:
            with self.assertRaises(urllib.error.HTTPError):
                a.observe(f"http://127.0.0.1:{server.server_port}", token, "org/repo", [7])
            self.assertEqual(calls, ["/v1/activity"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
