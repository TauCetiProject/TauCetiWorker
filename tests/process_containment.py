#!/usr/bin/env python3
"""Every host round's agent and tools die with their containment boundary.

The fixture agent leaves behind exactly the processes that escaped the old process-group sweep: a
plain background child, a setsid child, a nested setsid pair, and a double-forked grandchild. Each
carries a unique argv token, so the test finds (and, on failure, kills) only its own processes,
never anything else on a shared host. Strong cases need rootless bwrap PID namespaces and are
skipped without them; the legacy and unsupported-capability cases run everywhere.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity, agents, containment, shared_mathlib
from tauceti_worker import round as round_
from tauceti_worker.config import Die

# argv: token ready-file mode(exit|hang)
FIXTURE = r"""
import os, subprocess, sys, time
token, ready, mode = sys.argv[1:4]
py = sys.executable
sleep = "import time; time.sleep(60)"
subprocess.Popen([py, "-c", sleep, token])
subprocess.Popen([py, "-c", "import os,time; os.setsid(); time.sleep(60)", token])
subprocess.Popen([py, "-c",
    "import os,subprocess,sys,time; os.setsid(); "
    "subprocess.Popen([sys.executable,'-c','import os,time; os.setsid(); time.sleep(60)',sys.argv[1]]); "
    "time.sleep(60)", token])
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        os.execv(py, [py, "-c", sleep, token])  # inherits every descriptor, including any lease
    os._exit(0)
os.wait()
open(ready, "w").write("up")
if mode == "hang":
    time.sleep(60)
"""
# the agent itself, a background child, a setsid child, a nested setsid pair, a double fork
EXPECTED = 6


def tagged(token: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if token.encode() in args and state != "Z":
            found.append(int(entry.name))
    return found


def kill_tagged(token: str) -> None:
    for pid in tagged(token):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def wait_for(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


STRONG, WHY = containment.probe()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tauceti-containment-"))
        self.token = f"tauceti-containment-{uuid.uuid4().hex}"
        self.ready = self.tmp / "ready"
        self.env = patch.dict(os.environ, {"TAUCETI_ACTIVITY_DIR": str(self.tmp / "activity")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(kill_tagged, self.token)
        self.saved_probe = containment._probed
        self.addCleanup(setattr, containment, "_probed", self.saved_probe)
        # A process group that is not ours to clean up: it must survive every teardown.
        self.bystander_token = self.token + "-bystander"
        self.bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", self.bystander_token], start_new_session=True
        )
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.kill)
        self.round = activity.Round("containment-test")
        self.addCleanup(self.round.finish)

    def agent(self, mode="exit"):
        return [sys.executable, "-c", FIXTURE, self.token, str(self.ready), mode]

    def events(self, kind="containment_result"):
        rows = [json.loads(p.read_text()) for p in (self.tmp / "activity" / "pending").glob("*.json")]
        return [r for r in rows if r["kind"] == kind]

    def assert_bystander_alive(self):
        self.assertIsNone(self.bystander.poll(), "an unrelated process group was signalled")
        self.assertEqual(len(tagged(self.bystander_token)), 1)

    def run_agent(self, **kwargs):
        return agents.run_agent_proc(
            self.agent(kwargs.pop("mode", "exit")),
            env=kwargs.pop("env", dict(os.environ)),
            logdir=self.tmp / "logs",
            label=kwargs.pop("label", "agent-test"),
            provider="test",
            **kwargs,
        )


@unittest.skipUnless(STRONG, f"rootless bwrap PID namespaces unavailable: {WHY}")
class Namespace(Base):
    def setUp(self):
        super().setUp()
        containment._probed = None
        os.environ.pop(containment.POLICY_ENV, None)

    def test_successful_exit_kills_setsid_and_double_forked_descendants(self):
        # One-shot `tauceti work` runs this in-process; nothing sweeps afterwards.
        self.assertEqual(self.run_agent(), 0)
        self.assertTrue(self.ready.exists())
        # Checked immediately: descendants are gone before the caller may clean up the checkout.
        self.assertEqual(tagged(self.token), [])
        self.assert_bystander_alive()
        [event] = self.events()
        self.assertEqual(event["round_id"], self.round.id)
        self.assertEqual((event["mode"], event["cleanup"], event["exit_code"]), ("pid-namespace", "namespace-empty", 0))
        self.assertIsInstance(event["child_pid"], int)
        self.assertIsInstance(event["pid_namespace"], int)
        self.assertNotIn(self.token, json.dumps(event), "argv must not be recorded")
        # The identity was durable from launch, before the outcome was known.
        [started] = self.events("containment_started")
        self.assertEqual(started["boundary"], event["boundary"])
        self.assertEqual(started["pid_namespace"], event["pid_namespace"])
        self.assertLess(started["at"], event["at"])
        db = activity._connect()
        activity._consume(db, [])
        row = db.execute("SELECT mode, cleanup, round_id, exit_code, pid_namespace FROM containment").fetchone()
        self.assertEqual(tuple(row), ("pid-namespace", "namespace-empty", self.round.id, 0, event["pid_namespace"]))

    def test_cancellation_and_claim_loss_kill_every_descendant(self):
        lost = threading.Event()
        watcher = threading.Thread(
            target=lambda: wait_for(lambda: len(tagged(self.token)) >= EXPECTED) and lost.set(), daemon=True
        )
        watcher.start()
        rc = self.run_agent(mode="hang", cancel_event=lost)
        self.assertTrue(lost.is_set(), "fixture never reached its full process tree")
        self.assertLess(rc, 0)
        self.assertEqual(tagged(self.token), [])
        self.assert_bystander_alive()
        (self.tmp / "usage.json").write_text("{}")  # supervising state is still ours to write

    def test_timeout_kills_every_descendant(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            containment.run(self.agent("hang"), label="security-audit", timeout=4, capture_output=True)
        self.assertTrue(self.ready.exists())
        self.assertEqual(tagged(self.token), [])
        self.assert_bystander_alive()
        [event] = self.events()
        self.assertEqual(event["label"], "security-audit")
        self.assertIn(event["cleanup"], {"namespace-empty", "namespace-killed"})

    def test_parent_sigkill_kills_every_descendant(self):
        worker = (
            "import os,sys; from pathlib import Path; from tauceti_worker.agents import run_agent_proc; "
            f"run_agent_proc({self.agent('hang')!r}, env=dict(os.environ), logdir=Path({str(self.tmp)!r}), "
            "label='agent-test', provider='test')"
        )
        proc = subprocess.Popen([sys.executable, "-c", worker], cwd=Path(__file__).resolve().parent.parent)
        try:
            self.assertTrue(wait_for(lambda: len(tagged(self.token)) >= EXPECTED))
            proc.kill()  # no finally, atexit or signal handler runs
            proc.wait(5)
            self.assertTrue(wait_for(lambda: not tagged(self.token), 5), tagged(self.token))
            self.assert_bystander_alive()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_loop_round_timeout_tears_down_before_round_cleanup(self):
        # The loop's round child: a RoundContext-style SIGTERM handler, then cleanup that records
        # how many fixture processes were still alive when it ran.
        state = self.tmp / "round-state.json"
        script = (
            "import json,os,signal,sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from tauceti_worker.agents import run_agent_proc; "
            "sys.path.insert(0, sys.argv[2]); from process_containment import tagged; "
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
            "try:\n"
            f"    run_agent_proc({self.agent('hang')!r}, env=dict(os.environ), logdir=Path({str(self.tmp)!r}), "
            "label='agent-test', provider='test')\n"
            "finally:\n"
            f"    Path({str(state)!r}).write_text(json.dumps(len(tagged({self.token!r}))))\n"
        )
        here = Path(__file__).resolve()

        def spawn(_tail, *, timeout):
            return subprocess.Popen(
                [sys.executable, "-c", script, str(here.parent.parent), str(here.parent)], start_new_session=True
            )

        with patch.object(round_, "spawn_round", spawn):
            started = threading.Thread(target=lambda: wait_for(lambda: len(tagged(self.token)) >= EXPECTED, 20))
            started.start()
            rc = round_.run_round_subprocess(["--worker-id", "test"], timeout=6)
            started.join()
        self.assertEqual(rc, 124)
        self.assertEqual(json.loads(state.read_text()), 0, "descendants outlived the round's own cleanup")
        self.assertEqual(tagged(self.token), [])
        self.assert_bystander_alive()

    def test_cache_preparation_with_sharing_off_is_contained(self):
        with (self.tmp / "cache.log").open("wb") as output:
            rc = agents._cache_fetch(self.agent(), self.tmp, dict(os.environ), output, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(tagged(self.token), [])
        self.assertEqual(self.events()[0]["label"], "cache-fetch")

    def test_shared_mathlib_view_is_the_single_boundary_and_releases_its_lease(self):
        ok, reason = shared_mathlib.capability(self.tmp)
        if not ok:
            self.skipTest(reason)
        lower, upper, work, pool = (self.tmp / n for n in ("lower", "upper", "work", "pool"))
        for path in (lower, upper, work, pool):
            path.mkdir()
        lock = pool / "lease.lock"
        with lock.open("a+") as lease:
            fcntl.flock(lease, fcntl.LOCK_SH)
            prefix = shared_mathlib.overlay_command(lower, upper, work, upper, pool, lease_fd=lease.fileno())
            command = containment.Boundary("agent", prefix=prefix).argv(["agent"])
            self.assertEqual(command.count("bwrap"), 1, "no nested owner")
            self.assertEqual(command.count("--unshare-pid"), 1)
            self.assertIn("--die-with-parent", command)
            env = {**os.environ, "TAUCETI_SHARED_MATHLIB_LEASE_FD": str(lease.fileno())}
            rc = self.run_agent(env=env, containment_prefix=prefix, cancel_event=threading.Event())
            self.assertEqual(rc, 0)
            self.assertEqual(tagged(self.token), [])
        # The double-forked grandchild inherited the lease; it is gone, so the lease is free.
        with lock.open("a+") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        [event] = self.events()
        self.assertEqual((event["mode"], event["cleanup"]), ("pid-namespace", "namespace-empty"))

    def round_context(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        cfg = SimpleNamespace(state=self.tmp / "state", wid="containment-test", logdir=self.tmp / "logs")
        return cfg, round_.RoundContext(cfg)

    def test_round_cleanup_proves_quiescence_for_checkout_recovery(self):
        from tauceti_worker import work_recovery

        cfg, context = self.round_context()
        with context as ctx:
            self.assertEqual(self.run_agent(), 0)
            round_id = ctx.activity.id
        proof = json.loads((cfg.state / "quiescence.json").read_text())
        self.assertEqual((proof["round_id"], proof["empty"], proof["boundaries"]), (round_id, True, 1))
        # The checkout-recovery consumer accepts it for the round that owns the checkout, only.
        work_recovery.owner_path(cfg).parent.mkdir(parents=True)
        work_recovery.owner_path(cfg).write_text(json.dumps({"round_id": round_id}))
        snap = SimpleNamespace(fingerprint="unused")
        self.assertEqual(work_recovery.quiescence(cfg, self.tmp / "checkout", None, snap, "TauCeti")[0], "quiescent")
        work_recovery.owner_path(cfg).write_text(json.dumps({"round_id": "another-round"}))
        self.assertEqual(work_recovery.quiescence(cfg, self.tmp / "checkout", None, snap, "TauCeti")[0], "unknown")

    def test_best_effort_or_unconfirmed_rounds_withhold_the_proof(self):
        cfg, context = self.round_context()
        with patch.dict(os.environ, {containment.POLICY_ENV: "legacy"}), context:
            rc = agents.run_agent_proc(
                [sys.executable, "-c", "pass"], env=dict(os.environ), logdir=self.tmp, label="agent", provider="test"
            )
            self.assertEqual(rc, 0)
        self.assertFalse(json.loads((cfg.state / "quiescence.json").read_text())["empty"])
        cfg, context = self.round_context()
        with patch.object(containment, "unconfirmed", return_value=[{"boundary": "b"}]), context:
            pass
        self.assertFalse(json.loads((cfg.state / "quiescence.json").read_text())["empty"])
        # A boundary that started but never reached its confirmed outcome also withholds it.
        cfg, context = self.round_context()
        with context:
            containment.Boundary("abandoned")
        self.assertFalse(json.loads((cfg.state / "quiescence.json").read_text())["empty"])

    def test_branch_claim_loss_cancels_the_agent(self):
        # The real heartbeat watcher: its child reports the lease lost while the agent is running.
        claims = round_.Claims(SimpleNamespace(), SimpleNamespace(add_cleanup=lambda fn: None))
        lose = [sys.executable, "-c", f"import sys,time; time.sleep(2); sys.exit({round_.HEARTBEAT_LOST})"]
        with patch.object(round_, "self_argv", lambda *a: lose), patch.object(round_, "self_env", lambda e: e):
            claims.start_heartbeat("branch/1", "owner/claims")
        rc = self.run_agent(mode="hang", cancel_event=claims.lost)
        self.assertTrue(claims.lost.is_set())
        self.assertLess(rc, 0)
        self.assertEqual(tagged(self.token), [])
        claims.stop_heartbeat()
        # The next claim starts with a fresh cancellation event.
        keep = [sys.executable, "-c", "import time; time.sleep(30)"]
        with patch.object(round_, "self_argv", lambda *a: keep), patch.object(round_, "self_env", lambda e: e):
            claims.start_heartbeat("branch/2", "owner/claims")
        self.assertFalse(claims.lost.is_set())
        claims.stop_heartbeat()

    def test_stopped_heartbeat_is_not_a_lost_claim(self):
        claims = round_.Claims(SimpleNamespace(), SimpleNamespace(add_cleanup=lambda fn: None))
        lose = [sys.executable, "-c", f"import sys,time; time.sleep(0.5); sys.exit({round_.HEARTBEAT_LOST})"]
        with patch.object(round_, "self_argv", lambda *a: lose), patch.object(round_, "self_env", lambda e: e):
            claims.start_heartbeat("branch/1", "owner/claims")
        claims._stopped.set()  # stop_heartbeat would also terminate it; the race is the point
        time.sleep(1)
        self.assertFalse(claims.lost.is_set())
        claims.stop_heartbeat()

    def test_unconfirmed_namespace_is_reported_and_blocks_reuse(self):
        boundary = containment.Boundary("stuck")
        argv = boundary.argv([sys.executable, "-c", "import time; time.sleep(60)", self.token])
        proc = subprocess.Popen(argv, pass_fds=boundary.pass_fds)
        boundary.launched()
        if hasattr(os, "pidfd_open"):
            self.assertIsNotNone(boundary._pidfd, "init is addressed by pidfd, not a reusable pid")
        try:
            with (
                patch.object(containment, "_namespace_state", return_value=None),
                self.assertRaises(containment.ContainmentUnconfirmed),
            ):
                boundary.finish(None, grace=0.2)
            [event] = self.events()
            self.assertEqual(event["cleanup"], "namespace-unconfirmed")
            with patch.object(containment, "_namespace_state", return_value=None):
                self.assertEqual([m["label"] for m in containment.unconfirmed()], ["stuck"])
        finally:
            proc.kill()
            proc.wait()
        # Once its own namespace is observed gone, the marker retires.
        self.assertTrue(wait_for(lambda: not containment.unconfirmed(), 5))

    def test_namespace_init_is_killed_if_it_outlives_its_command(self):
        boundary = containment.Boundary("stuck")
        argv = boundary.argv([sys.executable, "-c", "import time; time.sleep(60)", self.token])
        proc = subprocess.Popen(argv, pass_fds=boundary.pass_fds)
        boundary.launched()
        self.assertTrue(wait_for(lambda: tagged(self.token)))
        proc.kill()  # the outer bwrap only; the namespace dies through its own init
        proc.wait()
        self.assertIn(boundary.finish(proc.returncode), {"namespace-empty", "namespace-killed"})
        self.assertEqual(tagged(self.token), [])


class Legacy(Base):
    def test_explicit_rollback_reports_best_effort_and_runs_uncontained(self):
        with patch.dict(os.environ, {containment.POLICY_ENV: "legacy"}):
            cap = containment.capability()
            self.assertFalse(cap.strong)
            self.assertEqual(cap.mode, "process-group")
            self.assertIn("best-effort", cap.reason)
            self.assertIn("automatic work recovery is paused", cap.reason)
            self.assertFalse(containment.strong())
            self.assertEqual(containment.Boundary("x").argv(["agent", "-p"]), ["agent", "-p"])
            ok, note = containment.doctor_row()
            self.assertFalse(ok)
            self.assertIn("process-group", note)
            # Escaped legacy descendants would hold the transcript pipe open, so run a plain agent.
            rc = agents.run_agent_proc(
                [sys.executable, "-c", "pass"], env=dict(os.environ), logdir=self.tmp, label="agent", provider="test"
            )
            self.assertEqual(rc, 0)
        [event] = self.events()
        self.assertEqual((event["mode"], event["cleanup"]), ("process-group", "round-group-best-effort"))
        self.assertIsNone(event["pid_namespace"])
        self.assert_bystander_alive()

    def test_unsupported_linux_names_the_prerequisite_and_never_claims_containment(self):
        with (
            patch.dict(os.environ, {containment.POLICY_ENV: "auto"}),
            patch.object(containment, "probe", return_value=(False, "bubblewrap (bwrap) is not on PATH")),
            patch.object(containment.platform, "system", return_value="Linux"),
        ):
            containment._probed = None
            cap = containment.capability()
            self.assertFalse(cap.strong)
            self.assertIn("install bubblewrap", cap.reason)
            self.assertIn("unprivileged user namespaces", cap.reason)
            self.assertIn("best-effort process-group cleanup", cap.reason)
            self.assertEqual(containment.Boundary("x").argv(["agent"]), ["agent"])
            with patch.dict(os.environ, {containment.POLICY_ENV: "required"}), self.assertRaises(Die):
                containment.Boundary("x")
            with patch.dict(os.environ, {containment.POLICY_ENV: "required"}):
                self.assertFalse(containment.strong())
                self.assertFalse(containment.doctor_row()[0])

    def test_non_linux_runs_without_bwrap_and_reports_accurately(self):
        with (
            patch.dict(os.environ, {containment.POLICY_ENV: "auto"}),
            patch.object(containment.platform, "system", return_value="Darwin"),
            patch.object(containment.shutil, "which", side_effect=AssertionError("must not look for bwrap")),
        ):
            self.assertEqual(containment.probe()[0], False)
            containment._probed = None
            cap = containment.capability()
            self.assertFalse(cap.strong)
            self.assertIn("Linux-only", cap.reason)
            self.assertNotIn("install bubblewrap", cap.reason)
            self.assertEqual(containment.Boundary("x").argv(["agent"]), ["agent"])

    def test_legacy_rollback_also_disables_the_namespace_based_shared_view(self):
        checkout = self.tmp / "checkout"
        (checkout / ".lake/packages/mathlib").mkdir(parents=True)
        env = {
            **os.environ,
            containment.POLICY_ENV: "legacy",
            "TAUCETI_SHARED_MATHLIB_ROOT": str(self.tmp / "pool"),
            "TAUCETI_SHARED_MATHLIB_BUILDS": "auto",
        }
        with patch.dict(os.environ, {containment.POLICY_ENV: "legacy"}):
            with shared_mathlib.view(checkout, env, self.tmp / "logs", None) as prefix:
                self.assertEqual(prefix, [])
            env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
            with self.assertRaisesRegex(shared_mathlib.Unavailable, "legacy"):
                with shared_mathlib.view(checkout, env, self.tmp / "logs", None):
                    pass

    def test_invalid_policy_is_refused(self):
        with patch.dict(os.environ, {containment.POLICY_ENV: "sometimes"}), self.assertRaises(Die):
            containment.capability()

    def test_bubble_agents_are_left_to_their_container(self):
        boundary = containment.Boundary("agent-bubble", contain=False)
        self.assertEqual(boundary.argv(["bubble", "run"]), ["bubble", "run"])
        self.assertEqual(boundary.finish(0), "external")
        self.assertEqual(self.events()[0]["mode"], "uncontained")


if __name__ == "__main__":
    unittest.main(verbosity=2)
