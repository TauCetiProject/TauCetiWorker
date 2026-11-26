#!/usr/bin/env python3
"""Isolated-home path constraints (macOS path length; stability across re-isolations).

bubble runs the sandbox in a colima VM whose lima/incus unix sockets nest under $HOME. While the worker
moved $HOME on macOS, its isolated home therefore had to be SHORT: the old location beneath the
installed package (site-packages) pushed those socket paths past UNIX_PATH_MAX (104) and colima refused
to start ("instance name … too long"), wasting the round. isolate_home no longer moves $HOME on macOS,
so those sockets nest under the operator's real home and that bound no longer binds. The path is kept
exactly as it was regardless: existing workers hold their codex credential copy and their creds-source
markers there, and it must stay a pure function of wid so a loop child recomputes its parent's path.
These assertions pin that shape (short, bounded, deterministic, under the real login home) and the
Linux path (unchanged, in-tree beside the worker's other state).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0

UNIX_PATH_MAX = 104
# The colima/lima/incus sockets that nest under $HOME and overflowed in the field report.
INCUS_SOCK = ".colima/bubble-colima/incus.sock"  # the client socket bubble connects to (VM reused)
LIMA_SOCK = ".colima/_lima/colima-bubble-colima/ssh.sock.1234567890123456"  # 16-digit lima disambiguator


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def iso_home(wid):
    return tc.agents._worker_iso_home(wid)


def iso_home_base(wid, base):
    return tc.agents._worker_iso_home(wid, Path(base))


def main():
    orig = tc.agents.sys.platform
    try:
        # --- macOS: short, under the real login home, sockets fit under UNIX_PATH_MAX ---
        tc.agents.sys.platform = "darwin"
        for wid in ("worker1", "e2e-fork", "reviewer-bob"):
            home = iso_home(wid)
            check(f"darwin home absolute & names the wid ({wid})", home.is_absolute() and wid in home.parts)
            check(f"darwin home not under site-packages ({wid})", "site-packages" not in str(home))
            incus = len(str(home / INCUS_SOCK))
            lima = len(str(home / LIMA_SOCK))
            check(f"darwin incus.sock < UNIX_PATH_MAX ({wid}: {incus})", incus < UNIX_PATH_MAX)
            check(f"darwin lima ssh.sock < UNIX_PATH_MAX ({wid}: {lima})", lima < UNIX_PATH_MAX)
        # Pure function of wid (no $HOME): the loop-child early-return in isolate_home relies on this.
        check("darwin home stable across calls", iso_home("worker1") == iso_home("worker1"))

        # A pathologically long --worker-id must still fit (bounded/hashed), and stay deterministic.
        long_wid = "an-absurdly-long-worker-id-" + "x" * 80
        home = iso_home(long_wid)
        check(
            f"darwin long wid lima ssh.sock < UNIX_PATH_MAX ({len(str(home / LIMA_SOCK))})",
            len(str(home / LIMA_SOCK)) < UNIX_PATH_MAX,
        )
        check("darwin long wid still deterministic", iso_home(long_wid) == iso_home(long_wid))
        check("darwin distinct long wids don't collide", iso_home(long_wid) != iso_home(long_wid + "-2"))

        # A long login-home root shrinks the budget, so even an ordinary wid is hashed — the result must
        # still fit and stay deterministic (the fallback branch, exercised via an injected base).
        long_base = "/Users/" + "u" * 13  # 20-char home → ~12 chars of budget for the per-worker component
        home = iso_home_base("a-sixteen-char-id", long_base)
        socklen = len(str(home / LIMA_SOCK))
        check(f"darwin long-root home fits ({socklen})", socklen < UNIX_PATH_MAX)
        check("darwin long-root anchored under .tauceti", home.parent.name == ".tauceti")
        check(
            "darwin long-root deterministic",
            iso_home_base("a-sixteen-char-id", long_base) == iso_home_base("a-sixteen-char-id", long_base),
        )

        # --- Linux: unchanged in-tree location (native incus, no $HOME-nested sockets) ---
        tc.agents.sys.platform = "linux"
        check(
            "linux home is HERE/state/<wid>/home",
            iso_home("worker1") == tc.agents.HERE / "state" / "worker1" / "home",
        )
    finally:
        tc.agents.sys.platform = orig

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
