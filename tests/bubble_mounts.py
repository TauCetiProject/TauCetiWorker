#!/usr/bin/env python3
"""Bubble receives every mount by its resolved host path, and the host's Lean toolchains on request.

A restricted Incus project refuses a disk source that crosses an absolute symlink, which is how the
shared roadmap and review-engine checkouts are attached. Checked through run_in_bubble's echo path,
as tests/bubble_cache.py does. Exit 0 = the argv agrees; 1 = a mismatch.
"""

import contextlib
import importlib.machinery
import importlib.util
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

pool = tmp / "elan"
complete = ("leanprover--lean4---v4.34.0-rc1", "leanprover--lean4---v4.34.0-rc2")
for name in (*complete, "leanprover--lean4---v4.35.0"):
    (pool / "toolchains" / name / "bin").mkdir(parents=True)
# elan's unpack directory for v4.35.0, already holding `bin/lean` before its rename
(pool / "toolchains" / "leanprover--lean4---v4.35.tmp" / "bin").mkdir(parents=True)
for name in (*complete, "leanprover--lean4---v4.35.tmp"):
    (pool / "toolchains" / name / "bin" / "lean").write_text("#!/bin/sh\n")
toolchains = [f"{pool}/toolchains/{name}:/home/user/.elan/toolchains/{name}:ro" for name in complete]

cfg = SimpleNamespace(state=link.parent.parent, home=tmp / "home", wid="mount-test", logdir=tmp / "logs")
w = SimpleNamespace(cfg=cfg)
opts = SimpleNamespace(work_model="claude")


def mounts(platform="linux", **kwargs):
    out = io.StringIO()
    with patch.object(sys, "platform", platform), contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "PROMPT", opts, **kwargs)
    argv = shlex.split(out.getvalue())
    return rc, [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]


saved = (tc.agents.ensure_bubble_home, tc.agents._bubble_pop)
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
try:
    with patch.dict(os.environ, TAUCETI_AGENT_ECHO="1", ELAN_HOME=str(pool)):
        os.environ.pop("TAUCETI_BUBBLE_TOOLCHAINS", None)
        review = dict(mounts=[f"{link}:/opt/roadmap:ro"], inner_cmd="python3 -m runner.cli", cred_model="claude")
        rc, specs = mounts(**review)
        check("review echo succeeds", rc == 0)
        check("the attached roadmap reaches Bubble as its snapshot", f"{snapshot}:/opt/roadmap:ro" in specs)
        check("no mount source keeps the symlink", not any(spec.startswith(str(link)) for spec in specs))
        check("the round directory is still mounted", f"{cfg.state / 'bubble-round'}:/opt/round:ro" in specs)

        rc, specs = mounts()
        check("work echo succeeds", rc == 0)
        check("toolchains stay out by default", not any("/.elan/" in spec for spec in specs))

        os.environ["TAUCETI_BUBBLE_TOOLCHAINS"] = "1"
        _, specs = mounts()
        check(
            "a work round mounts each complete toolchain and no install in progress",
            [s for s in specs if "/.elan/" in s] == toolchains,
        )
        _, specs = mounts(**review)
        check("a review round mounts none", not any("/.elan/" in spec for spec in specs))
        _, specs = mounts(platform="darwin")
        check("a macOS host mounts none", not any("/.elan/" in spec for spec in specs))

        (pool / "toolchains" / "local").symlink_to(pool / "toolchains" / complete[0], target_is_directory=True)
        _, specs = mounts()
        check("a linked toolchain is not mounted", not any("/.elan/toolchains/local" in spec for spec in specs))
        check("the other toolchains still are", [s for s in specs if "/.elan/" in s] == toolchains)
        (pool / "toolchains" / "local").unlink()

        # A worker's private toolchain folded into the pool by `scripts/share-build-caches --link`,
        # with linking failing part-way (a full disk, say): the pool must not show it at all.
        loader = importlib.machinery.SourceFileLoader("share_build_caches", str(REPO / "scripts/share-build-caches"))
        share = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(share)
        fleet, pool_home = tmp / "fleet", tmp / "pool-home"
        private = fleet / "w1" / "home" / ".elan" / "toolchains"
        pool_home.mkdir()
        (pool_home / ".elan").symlink_to(pool, target_is_directory=True)

        def migrate():
            argv = ["share-build-caches", "--link", "--state", str(fleet), "--home", str(pool_home)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                return share.main()

        migrating = private / "leanprover--lean4---v4.36.0"
        (migrating / "lib" / "lean").mkdir(parents=True)
        (migrating / "bin").mkdir()
        (migrating / "bin" / "lean").write_text("#!/bin/sh\n")
        (migrating / "lib" / "lean" / "libleanshared.so").write_text("")
        real_link = os.link

        def no_space(src, dst, *a, **kw):
            if str(src).endswith(".so"):
                raise OSError(28, "No space left on device")
            return real_link(src, dst, *a, **kw)

        with patch.object(os, "link", no_space), contextlib.redirect_stderr(io.StringIO()):
            migrate()
        check(
            "a failed migration leaves no toolchain in the pool",
            sorted(p.name for p in (pool / "toolchains").iterdir())
            == sorted([*complete, "leanprover--lean4---v4.35.0", "leanprover--lean4---v4.35.tmp"]),
        )
        check("so it is not mounted", [s for s in mounts()[1] if "/.elan/" in s] == toolchains)
        hidden = pool / "toolchains" / ".leanprover--lean4---v4.36.0.1"
        (hidden / "bin").mkdir(parents=True)
        (hidden / "bin" / "lean").write_text("#!/bin/sh\n")
        check("a migration still staging is not mounted", [s for s in mounts()[1] if "/.elan/" in s] == toolchains)
        shutil.rmtree(hidden)
        migrate()
        check(
            "a complete migration lands under the release name",
            (pool / "toolchains" / migrating.name / "lib" / "lean" / "libleanshared.so").is_file(),
        )
        check(
            "and is mounted",
            f"{pool}/toolchains/{migrating.name}:/home/user/.elan/toolchains/{migrating.name}:ro" in mounts()[1],
        )
        check(
            "no staging directory is left behind",
            not any(p.name.startswith(".") for p in (pool / "toolchains").iterdir()),
        )

        # `elan toolchain link custom DIR` in a worker's own ELAN_HOME, and a name the pool already links.
        external = tmp / "lean-build"
        (external / "bin").mkdir(parents=True)
        (external / "bin" / "lean").write_text("#!/bin/sh\n")
        (private / "custom").symlink_to(external, target_is_directory=True)
        (private / "taken").symlink_to(external, target_is_directory=True)
        (pool / "toolchains" / "taken").symlink_to(pool / "toolchains" / complete[0], target_is_directory=True)
        migrate()
        check(
            "a linked toolchain is pooled as the same link",
            (pool / "toolchains" / "custom").is_symlink()
            and os.readlink(pool / "toolchains" / "custom") == str(external),
        )
        check(
            "a name the pool already links is kept",
            os.readlink(pool / "toolchains" / "taken") == str(pool / "toolchains" / complete[0]),
        )
        check("neither link is mounted", not any("/toolchains/custom" in s or "/taken" in s for s in mounts()[1]))
        (pool / "toolchains" / "custom").unlink()
        (pool / "toolchains" / "taken").unlink()

        os.environ["ELAN_HOME"] = str(tmp / "missing")
        check("a missing pool mounts none", not any("/.elan/" in spec for spec in mounts()[1]))
finally:
    tc.agents.ensure_bubble_home, tc.agents._bubble_pop = saved
    shutil.rmtree(tmp, ignore_errors=True)

print("PASS" if not fails else f"FAIL: {fails} failure(s)")
sys.exit(1 if fails else 0)
