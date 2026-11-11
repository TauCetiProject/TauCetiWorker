#!/usr/bin/env python3
"""Leaked-background-process sweep (the orphaned build-waiter).

A tool-using `claude` agent backgrounds a long `lake build` and Claude Code's Bash tool waits on it with
a synthesized `until ... do sleep; done` poll-loop (one job-control variant busy-spins a whole core). When
the agent exits 0, that loop has no live parent and reparents to init — surviving forever. The round runs
in its OWN session, and the loop driver's timeout teardown (kill_round_group) only fires on the abnormal
paths, so a round that simply *finishes* never swept its group. reap_round_group closes that gap on every
exit path. This harness reproduces the exact leak (a round that exits 0 leaving a backgrounded grandchild)
and pins that the sweep clears it, plus a direct check that it reaches a whole multi-process group.

Exit 0 = swept as expected; 1 = a straggler survived.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

sys.path.insert(0, str(REPO))
import tauceti_worker as tc

pass_ = 0
fail = 0


def ok(msg: str) -> None:
    global pass_
    print(f"  [PASS] {msg}")
    pass_ += 1


def no(msg: str) -> None:
    global fail
    print(f"  [FAIL] {msg}")
    fail += 1


def group_alive(pgid: int) -> bool:
    """Is this group still holding live processes?

    Three outcomes, not two, and the third is why this is not a bare bool internally. ESRCH is an
    empty group. EPERM means the group exists but nothing in it could be signalled: on Darwin that is
    what a group of unreaped zombies reports, but it is also what a live member we may not signal
    reports, and killpg cannot distinguish them. Collapsing EPERM to "dead" would let this test's
    oracle certify a sweep that actually leaked something, so say which happened instead of hiding it.
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        print(f"  [note] group {pgid} exists but is unsignalable (EPERM); treating as swept")
        return False


print("== 1. reap_round_group sweeps a whole multi-process session group ==")
# A leader in its own session that spawns a grandchild; both outlive the test window. start_new_session
# makes the leader its own group leader, so its pgid == its pid — exactly spawn_round's guarantee. Use a
# python leader (not `bash -c`, whose non-interactive SIGTERM deferral muddies the timing) so the group
# mirrors a real round: a process that forks a long-lived child and then exits/sleeps.
leader = subprocess.Popen(
    [sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','300']); time.sleep(300)"],
    start_new_session=True,
)
time.sleep(0.5)
pgid = leader.pid
if group_alive(pgid):
    tc.reap_round_group(pgid)
    # reap_round_group kills the leader but, as its parent, we must wait() it so it isn't left a zombie —
    # a zombie still answers kill(pid, 0), which would mask whether the grandchild actually died.
    try:
        leader.wait(2)
    except subprocess.TimeoutExpired:
        pass
    if not group_alive(pgid):
        ok("multi-process group fully swept")
    else:
        no("group survived reap_round_group")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
else:
    no("could not stand up the test group")
    try:
        leader.wait(2)
    except subprocess.TimeoutExpired:
        pass

print("== 2. reap_round_group is a no-op on an already-empty group ==")
try:
    tc.reap_round_group(leader.pid)  # leader and its group are gone now
    ok("no error sweeping an empty group")
except Exception as e:
    no(f"raised on empty group: {e}")

print("== 3. a round that exits 0 leaves a backgrounded grandchild; the sweep clears it ==")
# Drive the real _round through the test HOLD hook: it spawns `sleep <hold>` and returns 0 immediately —
# the leak. Spawn it the way spawn_round does (own session) so its pid is the group id to sweep.
WID = "sweep-test"
env = dict(os.environ, TAUCETI_WORKER_ID=WID, TAUCETI_TEST_HOLD="300")
round_proc = subprocess.Popen(
    [sys.executable, str(REPO / "tauceti"), "_round", "--worker-id", WID],
    start_new_session=True,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
rc = round_proc.wait(60)
group = round_proc.pid  # == pgid (own session)
time.sleep(0.3)
if rc != 0:
    no(f"round exited rc={rc} (expected 0)")
elif not group_alive(group):
    no("round left no grandchild — test can't prove the sweep (HOLD hook may have changed)")
else:
    ok("round exited 0 with a live orphaned grandchild (the leak reproduced)")
    tc.reap_round_group(group)
    time.sleep(0.2)
    if not group_alive(group):
        ok("sweep cleared the leaked grandchild")
    else:
        no("grandchild survived the sweep")
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

print("== 4. run_round_subprocess sweeps the group on a normal (rc 0) return ==")
# The integration point: not just that reap_round_group works, but that run_round_subprocess's `finally`
# actually invokes it on the happy path. Spy on spawn_round to capture the round's pgid, drive a real
# round that leaks a grandchild (TAUCETI_TEST_HOLD), and assert the group is gone once the call returns.
captured = {}
orig_spawn = tc.round.spawn_round  # patch where run_round_subprocess looks it up


def spy_spawn(argv_tail, **kwargs):
    p = orig_spawn(argv_tail, **kwargs)
    captured["pgid"] = p.pid  # == pgid (spawn_round uses start_new_session)
    return p


tc.round.spawn_round = spy_spawn
os.environ["TAUCETI_TEST_HOLD"] = "300"
try:
    rc = tc.run_round_subprocess(["--worker-id", WID])
finally:
    tc.round.spawn_round = orig_spawn
    os.environ.pop("TAUCETI_TEST_HOLD", None)

pgid4 = captured.get("pgid")
if rc != 0:
    no(f"run_round_subprocess returned rc={rc} (expected 0)")
elif pgid4 is None:
    no("spawn_round was never called")
elif group_alive(pgid4):
    no("run_round_subprocess returned with the round group still alive — finally sweep not wired")
    try:
        os.killpg(pgid4, signal.SIGKILL)
    except ProcessLookupError:
        pass
else:
    ok("run_round_subprocess swept the leaked group on normal return")

print("== 5. an unsignalable group is reported, not raised, at every call site ==")
# Darwin returns EPERM from killpg when a group exists but no member could be signalled -- a group of
# unreaped zombies, which is what a just-killed leader is until its parent wait()s it, but equally a
# live member we may not signal. Neither may crash a teardown path: reap_round_group runs in the loop
# parent's finally, so an escaping PermissionError takes the whole --loop down. Simulate the platform's
# answer rather than the platform, so this runs anywhere.
real_killpg = os.killpg


def denying_killpg(_pgid, _sig):
    raise PermissionError(1, "Operation not permitted")


# signal_group is the single place that classifies; check it first, then that no caller re-raises.
os.killpg = denying_killpg
try:
    if tc.round.signal_group(999999, 0) == "denied":
        ok("signal_group classifies EPERM as denied")
    else:
        no("signal_group mis-classified EPERM")
finally:
    os.killpg = real_killpg


# Every site, driven so the EPERM lands on each signal in turn: reap's SIGTERM, reap's probe, reap's
# final SIGKILL, and both of kill_round_group's. "Did not raise" is necessary but not sufficient, so
# also record the calls to show the sweep stops at the denial instead of spinning to its deadline.
class FakeProc:
    """Minimal Popen stand-in: alive until waited, and never a real process."""

    pid = os.getpid()  # so getpgid() resolves; killpg is patched out, so nothing is ever signalled
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fake", timeout or 0)


for label, fn, deny_at in (
    ("reap: SIGTERM", lambda: tc.reap_round_group(999999, term_grace=0.2), 0),
    ("reap: liveness probe", lambda: tc.reap_round_group(999999, term_grace=0.2), 1),
    ("reap: final SIGKILL", lambda: tc.reap_round_group(999999, term_grace=0.0), 1),
    ("kill_round_group: SIGTERM", lambda: tc.round.kill_round_group(FakeProc(), term_grace=0), 0),
    ("kill_round_group: SIGKILL", lambda: tc.round.kill_round_group(FakeProc(), term_grace=0), 1),
):
    seen = []

    def counting_killpg(pgid, sig, _seen=seen, _deny_at=deny_at):
        _seen.append(sig)
        if len(_seen) - 1 == _deny_at:
            raise PermissionError(1, "Operation not permitted")
        return None  # pretend it landed; never touch a real process group

    os.killpg = counting_killpg
    try:
        fn()
        if len(seen) == deny_at + 1:
            ok(f"{label}: EPERM handled and no further signals sent")
        else:
            no(f"{label}: kept signalling after EPERM ({len(seen)} calls)")
    except PermissionError:
        no(f"{label}: EPERM escaped")
    except Exception as exc:  # noqa: BLE001 - any escape is the failure being tested for
        no(f"{label}: raised {type(exc).__name__}: {exc}")
    finally:
        os.killpg = real_killpg

# ESRCH must stay distinguishable from EPERM, or the classification is decorative.
os.killpg = lambda _pgid, _sig: (_ for _ in ()).throw(ProcessLookupError())
try:
    if tc.round.signal_group(999999, 0) == "gone":
        ok("signal_group still classifies ESRCH as gone")
    else:
        no("signal_group conflated ESRCH with denied")
finally:
    os.killpg = real_killpg

# Clean the per-worker state this test seeded.
shutil.rmtree(REPO / "state" / WID, ignore_errors=True)

print(f"\n{pass_} passed, {fail} failed")
sys.exit(1 if fail else 0)
