#!/usr/bin/env python3
"""Bubble receives every mount by its resolved host path.

A restricted Incus project refuses a disk source that crosses an absolute symlink, which is how the
shared roadmap and review-engine checkouts are attached. Checked through run_in_bubble's echo path,
as tests/bubble_cache.py does. Exit 0 = the argv agrees; 1 = a mismatch.
"""

import contextlib
import io
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


tmp = Path(tempfile.mkdtemp()).resolve()
snapshot = tmp / "repositories" / "snapshots-v3" / "abc"
snapshot.mkdir(parents=True)
link = tmp / "state" / "refs" / "roadmap"
link.parent.mkdir(parents=True)
link.symlink_to(snapshot, target_is_directory=True)

check("a symlinked source becomes its target", tc.agents.resolved_mount(f"{link}:/opt/r:ro") == f"{snapshot}:/opt/r:ro")
check("a path below a symlink is resolved too", tc.agents.resolved_mount(f"{link}/a:/opt/a") == f"{snapshot}/a:/opt/a")
check("a plain source is unchanged", tc.agents.resolved_mount(f"{snapshot}:/opt/s:rw") == f"{snapshot}:/opt/s:rw")
check("a source that is not an absolute path is left alone", tc.agents.resolved_mount("~/x:/opt/x") == "~/x:/opt/x")

cfg = SimpleNamespace(state=link.parent.parent, home=tmp / "home", wid="mount-test", logdir=tmp / "logs")
w = SimpleNamespace(cfg=cfg)
opts = SimpleNamespace(work_model="claude")


def mounts(**kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "PROMPT", opts, **kwargs)
    argv = shlex.split(out.getvalue())
    return rc, [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]


saved = (tc.agents.ensure_bubble_home, tc.agents._bubble_pop)
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
try:
    with patch.dict(os.environ, TAUCETI_AGENT_ECHO="1"):
        review = dict(mounts=[f"{link}:/opt/roadmap:ro"], inner_cmd="python3 -m runner.cli", cred_model="claude")
        rc, specs = mounts(**review)
        check("review echo succeeds", rc == 0)
        check("the attached roadmap reaches Bubble as its snapshot", f"{snapshot}:/opt/roadmap:ro" in specs)
        check("no mount source keeps the symlink", not any(spec.startswith(str(link)) for spec in specs))
        check("the round directory is still mounted", f"{cfg.state / 'bubble-round'}:/opt/round:ro" in specs)
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
    shutil.rmtree(tmp, ignore_errors=True)

print("PASS" if not fails else f"FAIL: {fails} failure(s)")
sys.exit(1 if fails else 0)
