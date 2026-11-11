#!/usr/bin/env python3
"""An authoring agent in Bubble can renew and release the target claim it acquired.

Bubble runs its command over SSH, so the container sees only the variables the generated command sets.
Each claim.sh call below runs in its own process with nothing but that command's environment, against a
local stand-in for the claim repository.
"""

import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

fails = 0
PROBE = "__CLAIM_PROBE__"
KEY = "author/Fixture/target"


def check(name, ok):
    global fails
    print(f"[{'OK ' if ok else 'XX '}] {name}")
    if not ok:
        fails += 1


tmp = Path(tempfile.mkdtemp())
saved_home, saved_pop = tc.agents.ensure_bubble_home, tc.agents._bubble_pop
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
cfg = SimpleNamespace(state=tmp / "state", home=tmp / "home", wid="worker3", logdir=tmp / "logs")
try:
    out = io.StringIO()
    os.environ.update(TAUCETI_AGENT_ECHO="1", CLAIM_REPO="fixture/claims", TAUCETI_WORKER_ID="worker3")
    with contextlib.redirect_stdout(out):
        tc.run_in_bubble(
            SimpleNamespace(cfg=cfg), tc.TAUCETI, "", SimpleNamespace(work_model="claude"), inner_cmd=PROBE
        )
    argv = shlex.split(out.getvalue().removeprefix("BUBBLE "))
    command = argv[argv.index("--command") + 1]
    check("generated command ends with the inner command", command.endswith(" " + PROBE))

    remote = tmp / "claims.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    container_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp / "container-home"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"url.{remote}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/fixture/claims",
        "GIT_CONFIG_KEY_1": "protocol.file.allow",
        "GIT_CONFIG_VALUE_1": "always",
    }

    def lifecycle(cmd):
        rcs = {}
        for op in ("acquire", "holds", "renew", "release"):
            inner = cmd.replace(PROBE, shlex.join([str(REPO / "scripts/claim.sh"), op, KEY]))
            rcs[op] = subprocess.run(
                ["bash", "-c", inner], env=container_env, capture_output=True, timeout=30
            ).returncode
        refs = subprocess.run(
            ["git", "-C", remote, "for-each-ref", "refs/tauceti-claims"], capture_output=True, text=True
        )
        return rcs, refs.stdout.strip()

    rcs, left = lifecycle(command)
    check(f"acquire/holds/renew/release all succeed: {rcs}", set(rcs.values()) == {0})
    check("release deletes the lease", left == "")

    # Without the worker id each call is a fresh owner: the same agent cannot keep its own claim.
    rcs, left = lifecycle(re.sub(r" TAUCETI_WORKER_ID=\S+", "", command))
    check(f"control: without the worker id the claim is orphaned: {rcs}", rcs["holds"] == 1 and left != "")
finally:
    for var in ("TAUCETI_AGENT_ECHO", "CLAIM_REPO", "TAUCETI_WORKER_ID"):
        os.environ.pop(var, None)
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved_home, saved_pop
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
