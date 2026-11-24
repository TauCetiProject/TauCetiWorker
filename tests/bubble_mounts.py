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
relative_link = link.parent / "relative"
relative_link.symlink_to(os.path.relpath(snapshot, relative_link.parent), target_is_directory=True)

with patch.object(tc.agents.sys, "platform", "linux"):
    check(
        "a symlinked source becomes its target",
        tc.agents.resolved_mount(f"{link}:/opt/r:ro") == f"{snapshot}:/opt/r:ro",
    )
    check(
        "a path below a symlink is resolved too", tc.agents.resolved_mount(f"{link}/a:/opt/a") == f"{snapshot}/a:/opt/a"
    )
    check("a plain source is unchanged", tc.agents.resolved_mount(f"{snapshot}:/opt/s:rw") == f"{snapshot}:/opt/s:rw")
    check(
        "a relative symlink target is resolved",
        tc.agents.resolved_mount(f"{relative_link}:/opt/r") == f"{snapshot}:/opt/r",
    )
    check(
        "missing source components are not created",
        tc.agents.resolved_mount(f"{link}/missing:/opt/m:rw") == f"{snapshot}/missing:/opt/m:rw"
        and not (snapshot / "missing").exists(),
    )
    check(
        "spaces and destination spelling are preserved",
        tc.agents.resolved_mount(f"{link}/with spaces:/opt/../mount target:ro")
        == f"{snapshot}/with spaces:/opt/../mount target:ro",
    )
    for spec in (
        "~/x:/opt/x",
        "relative:/opt/x",
        f"{link}:relative:ro",
        f"{link}:/opt/x:other",
        f"{link}:/opt/x:ro:extra",
    ):
        check(f"unsupported mount syntax is left alone: {spec}", tc.agents.resolved_mount(spec) == spec)
    # Python 3.11/3.12 raise RuntimeError for a loop even with strict=False;
    # newer pathlib versions may instead leave it for the mount runtime.
    with patch.object(tc.agents.Path, "resolve", side_effect=RuntimeError("Symlink loop")):
        try:
            tc.agents.resolved_mount(f"{link}:/opt/cycle:ro")
        except tc.Die as error:
            check(
                "a resolution failure has an explicit mount-source diagnostic",
                "cannot resolve Bubble mount source" in str(error),
            )
        else:
            check("a failed resolution is rejected", False)

with (
    patch.object(tc.agents.sys, "platform", "darwin"),
    patch.object(tc.agents.Path, "resolve", side_effect=AssertionError),
):
    for spec in (f"{link}:/opt/r:ro", "/tmp/session:/opt/session:rw", "/var/folders/session:/opt/session"):
        check("Darwin mount sources retain their original spelling", tc.agents.resolved_mount(spec) == spec)

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
    with (
        patch.dict(os.environ, TAUCETI_AGENT_ECHO="1", TAUCETI_BUILD_RESOURCE_WAIT=str(link / "pending.json")),
        patch.object(tc.agents.sys, "platform", "linux"),
    ):
        rc, specs = mounts(mounts=[f"{link}:/opt/roadmap:ro"], inner_cmd="python3 -m runner.cli", cred_model="claude")
        check("review echo succeeds", rc == 0)
        check("the attached roadmap reaches Bubble as its snapshot", f"{snapshot}:/opt/roadmap:ro" in specs)
        check("no mount source keeps the symlink", not any(spec.startswith(str(link)) for spec in specs))
        check("the round directory is still mounted", f"{cfg.state / 'bubble-round'}:/opt/round:ro" in specs)
        check("the late resource-wait mount is resolved too", f"{snapshot}:/opt/resource-waits:rw" in specs)
    with (
        patch.dict(os.environ, TAUCETI_AGENT_ECHO="1", TAUCETI_BUILD_RESOURCE_WAIT=str(link / "pending.json")),
        patch.object(tc.agents.sys, "platform", "darwin"),
    ):
        rc, specs = mounts(mounts=[f"{link}:/opt/roadmap:ro"], inner_cmd="true", cred_model="claude")
        check("Darwin review echo succeeds", rc == 0)
        check("Darwin roadmap mount is unchanged", f"{link}:/opt/roadmap:ro" in specs)
        check("Darwin resource-wait mount is unchanged", f"{link}:/opt/resource-waits:rw" in specs)
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
    shutil.rmtree(tmp, ignore_errors=True)

print("PASS" if not fails else f"FAIL: {fails} failure(s)")
sys.exit(1 if fails else 0)
