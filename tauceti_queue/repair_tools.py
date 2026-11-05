"""The build lock shared by every Lake build in a repair worktree, and the agent's scoped build script.

One ``lake build`` runs in a worktree at a time, whoever starts it: the worker's own builds hold the
lock through ``build_lock`` and the agent's ``.tauceti-repair/build`` (written by
``write_build_script`` from the very same ``take_lock``) holds it the same way. The lock is the
directory ``.tauceti-repair/build.lock``, taken atomically by ``mkdir`` with the holder's pid in
``pid`` inside; a waiter polls every 2 s, says so every 30 s, and clears a lock whose holder has died.

The script takes module names (``TauCeti.Foo.Bar``) or paths (``TauCeti/Foo/Bar.lean``), with
``--dependents`` every module importing them transitively (from the library's ``import`` lines, one
pass, cached in ``.tauceti-repair/imports.json``), and runs ``lake build --iofail`` on them in the
foreground, exiting with Lake's status; no arguments builds the whole library. Lake runs in the offline
``bwrap`` sandbox of CI's required build (``repair_sandbox.sandboxed_lake``), as the worker's own builds do.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import shutil
import time
from pathlib import Path

TOOLS_DIR = ".tauceti-repair"
BUILD_FILE = "build"
LOCK_DIR = "build.lock"


def take_lock(lock: Path, log=print) -> None:
    """Wait for the lock directory to disappear (poll every 2 s, a line every 30 s; a lock whose holder
    has died is cleared), then take it by ``mkdir`` with our pid inside."""
    waited = 0
    while True:
        with contextlib.suppress(FileExistsError):
            lock.mkdir(parents=True)
            (lock / "pid").write_text(str(os.getpid()))
            return
        try:
            pid = int((lock / "pid").read_text())
            os.kill(pid, 0)  # ProcessLookupError when the holder has died
        except (FileNotFoundError, ValueError):
            pid = 0  # still being taken
        except ProcessLookupError:
            with contextlib.suppress(OSError):  # one waiter wins the rename; the others find the lock gone
                os.rename(lock, stale := f"{lock}.stale-{os.getpid()}")
                shutil.rmtree(stale, ignore_errors=True)
            continue
        if waited % 30 == 0:
            log(f"waiting for {lock.parent.name}/{lock.name} (held by pid {pid or '?'})", flush=True)
        time.sleep(2)
        waited += 2


@contextlib.contextmanager
def build_lock(worktree: Path, log=print):
    """Hold the worktree's build lock around the body, the way the agent's build script does."""
    lock = worktree / TOOLS_DIR / LOCK_DIR
    take_lock(lock, log)
    try:
        yield lock
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def imports(root: Path) -> dict[str, list[str]]:
    cache = root / ".tauceti-repair" / "imports.json"  # module to the modules it imports, one pass, cached
    if cache.exists():
        return json.loads(cache.read_text())
    table = {}
    for path in [*root.glob("*.lean"), *root.glob("[!.]*/**/*.lean")]:  # every .lean outside hidden dirs
        text = path.read_text(errors="replace")
        table[str(path.relative_to(root))[:-5].replace("/", ".")] = re.findall(r"^import +(\S+)", text, re.M)
    cache.write_text(json.dumps(table))
    return table


BUILD_HEADER = '''#!/usr/bin/env python3
"""Build only the named modules, one lake build at a time. Written by the queue's repair worker.

Usage: .tauceti-repair/build [--dependents] [TauCeti.Foo.Bar | TauCeti/Foo/Bar.lean ...]; no arguments
builds the whole library, --dependents adds the modules importing the named ones (from the library's
import lines, cached in .tauceti-repair/imports.json: delete it after changing imports). Builds wait for
and hold the lock directory .tauceti-repair/build.lock (mkdir, pid inside); exits with Lake's status.
Lake runs offline in the bwrap sandbox of CI's required build (tauceti_queue.repair_sandbox).
"""
import contextlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

'''

BUILD_MAIN = """
modules = [a[:-5].replace("/", ".") if a.endswith(".lean") else a for a in sys.argv[1:] if a != "--dependents"]
if "--dependents" in sys.argv:
    table = imports(ROOT)
    for m in modules:  # grows while iterating: the closure over the reverse import graph
        modules += [d for d, deps in table.items() if m in deps and d not in modules]
take_lock(lock := LOCK)
try:
    sys.exit(subprocess.run(sandboxed_lake(["lake", "build", "--iofail", *modules], ROOT), cwd=ROOT).returncode)
finally:
    shutil.rmtree(lock, ignore_errors=True)
"""


def build_script(lock: Path | None = None) -> str:
    """The agent's build script: ``take_lock`` and ``imports`` verbatim, the lock (the worktree's own
    ``.tauceti-repair/build.lock``, or ``lock`` when several worktrees of one repair share one) and the
    main, which runs Lake through ``repair_sandbox.sandboxed_lake`` imported from this checkout of the
    package."""
    sandbox = (
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
        "from tauceti_queue.repair_sandbox import sandboxed_lake\n\n"
    )
    where = f"LOCK = Path({str(lock)!r})\n" if lock else 'LOCK = ROOT / ".tauceti-repair" / "build.lock"\n'
    return (
        BUILD_HEADER
        + sandbox
        + "\n\n".join(inspect.getsource(f) for f in (take_lock, imports))
        + "\n"
        + where
        + BUILD_MAIN
    )


def write_build_script(directory: Path, lock: Path | None = None) -> Path:
    """Write ``directory/build``, executable, dropping any stale import cache beside it; ``lock`` names
    a lock directory shared with other worktrees (``repair_parallel``) instead of the worktree's own."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / BUILD_FILE
    script.write_text(build_script(lock))
    script.chmod(0o755)
    (directory / "imports.json").unlink(missing_ok=True)
    return script
