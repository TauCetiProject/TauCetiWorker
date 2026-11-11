#!/usr/bin/env python3
"""TAUCETI_MATHLIB_STORE: a Bubble work round mounts the store and links the entry its checkout pins.

Three layers, none of which starts a container: the linker script against a store and a workspace on
disk; the bootstrap shell run under bash with a fake `lake`; and run_in_bubble's echoed argv.
Exit 0 = all agree; 1 = a mismatch.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

LINKER = REPO / "scripts" / "tauceti-mathlib-store"
REV, REV2, REV3 = "a" * 40, "b" * 40, "c" * 40
BATTERIES, AESOP = "d" * 40, "e" * 40
TOOLCHAIN = "leanprover/lean4:v4.36.0"
FETCH = "lake exe cache get || lake exe cache get; "

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


tmp = Path(tempfile.mkdtemp()).resolve()
store = tmp / "store"


def manifest(mathlib, aesop):
    """A TauCeti checkout's manifest, or with `mathlib=None` the one in a Mathlib checkout."""
    packages = [
        dict(name="mathlib", type="git", rev=mathlib),
        dict(name="batteries", type="git", rev=BATTERIES),
        dict(name="aesop", type="git", rev=aesop),
        dict(name="local", type="path", dir="../local"),
    ]
    return json.dumps(dict(version="1.1.0", packages=packages[1:] if mathlib is None else packages))


def publish(rev, *, toolchain=TOOLCHAIN, ready=True, dependencies=("batteries", "aesop")):
    """A Mathlib checkout with its builds, as the store's builder leaves it."""
    entry = store / rev
    shutil.rmtree(entry, ignore_errors=True)
    (entry / ".lake" / "build" / "lib" / "lean").mkdir(parents=True)
    (entry / ".lake" / "build" / "lib" / "lean" / "Mathlib.olean").write_text("olean")
    for name in dependencies:
        (entry / ".lake" / "packages" / name / ".lake" / "build").mkdir(parents=True)
    (entry / "lake-manifest.json").write_text(manifest(None, "f" * 40))
    (entry / "lean-toolchain").write_text(toolchain + "\n")
    if ready:
        (entry / ".tauceti-store-ready").write_text(rev + "\n")
    return entry / ".lake"


def workspace(rev=REV, *, toolchain=TOOLCHAIN):
    ws = tmp / "ws"
    shutil.rmtree(ws, ignore_errors=True)
    for name in ("mathlib", "batteries", "aesop"):
        (ws / ".lake" / "packages" / name).mkdir(parents=True)
    pin(ws, rev)
    (ws / "lean-toolchain").write_text(toolchain + "\n")
    return ws


def pin(ws, rev):
    (ws / "lake-manifest.json").write_text(manifest(rev, AESOP))


def build(ws, name="mathlib"):
    return ws / ".lake" / "packages" / name / ".lake" / "build"


def link(ws, *args, artifact_cache=None):
    env = {k: v for k, v in os.environ.items() if k != "LAKE_ARTIFACT_CACHE"}
    if artifact_cache is not None:
        env["LAKE_ARTIFACT_CACHE"] = artifact_cache
    return subprocess.run(
        [sys.executable, str(LINKER), *args, str(store)], cwd=ws, env=env, capture_output=True, text=True
    )


def links(ws):
    return {name for name in ("mathlib", "batteries", "aesop") if build(ws, name).is_symlink()}


# --- the linker -------------------------------------------------------------------------------------
entry = publish(REV)
ws = workspace()
done = link(ws)
check("a matching entry links Mathlib", done.returncode == 0 and build(ws).resolve() == entry / "build")
check("the linked build is the entry's files", (build(ws) / "lib/lean/Mathlib.olean").read_text() == "olean")
check(
    "a dependency pinned alike is linked", build(ws, "batteries").resolve() == entry / "packages/batteries/.lake/build"
)
check("a dependency pinned differently is left to the unpack", not build(ws, "aesop").exists())
check("the log line names what was linked", "linked mathlib, batteries" in done.stdout)
check("a second run changes nothing", link(ws).returncode == 0 and links(ws) == {"mathlib", "batteries"})

ws = workspace()
done = link(ws, artifact_cache="true")
check("no link is made under a set LAKE_ARTIFACT_CACHE", done.returncode == 1 and not links(ws))

for label, kwargs in (
    ("another toolchain", dict(toolchain="leanprover/lean4:v4.35.0")),
    ("no ready marker", dict(ready=False)),
):
    publish(REV, **kwargs)
    ws = workspace()
    check(f"an entry with {label} is not used", link(ws).returncode == 1 and not links(ws))
ws = workspace(REV2)
check("a revision outside the store links nothing", link(ws).returncode == 1 and not links(ws))

publish(REV)
ws = workspace()
build(ws).mkdir(parents=True)
(build(ws) / "kept").write_text("private")
done = link(ws)
check(
    "an unpacked Mathlib is left alone, and no dependency is linked beside it",
    done.returncode == 1 and not links(ws) and (build(ws) / "kept").read_text() == "private",
)

entry2 = publish(REV2, dependencies=())
ws = workspace()
link(ws)
pin(ws, REV2)
done = link(ws)
check(
    "after the pin moves, a rerun links the new entry and drops the dependency it does not hold",
    done.returncode == 0 and build(ws).resolve() == entry2 / "build" and links(ws) == {"mathlib"},
)
pin(ws, REV3)
check("a pin with no entry leaves no stale link", link(ws).returncode == 1 and not links(ws))

ws = workspace()
link(ws)
elsewhere = tmp / "elsewhere"
elsewhere.mkdir()
build(ws, "aesop").parent.mkdir()
build(ws, "aesop").symlink_to(elsewhere)
link(ws, "--unlink")
check("--unlink removes the store's links and no other", links(ws) == {"aesop"})


# --- the bootstrap, under bash with a fake lake -----------------------------------------------------
shim = tmp / "bin"
shim.mkdir()
calls = tmp / "lake-calls"
(shim / "lake").write_text(
    "#!/bin/sh\n"
    'printf "%s|%s\\n" "$*" "${LAKE_ARTIFACT_CACHE-unset}" >> "$LAKE_CALLS"\n'
    'case "$*" in\n'
    '  "env true") exit 0 ;;\n'
    '  "exe cache get")\n'
    '    if [ -L .lake/packages/mathlib/.lake/build ]; then exit "${LINKED_RC:-0}"; fi\n'
    '    exit "${PRIVATE_RC:-0}" ;;\n'
    "esac\n"
    "exit 99\n"
)
(shim / "lake").chmod(0o755)
seen = tmp / "agent-saw"
AGENT = 'printf %s "${LAKE_ARTIFACT_CACHE-unset}" > "$SEEN"'


def bootstrap(ws, command, *, linked_rc=0, private_rc=0):
    home = tmp / "home"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir()
    seen.unlink(missing_ok=True)
    calls.unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "LAKE_ARTIFACT_CACHE": "true",
        "LAKE_CALLS": str(calls),
        "LINKED_RC": str(linked_rc),
        "PRIVATE_RC": str(private_rc),
        "SEEN": str(seen),
    }
    result = subprocess.run(["bash", "-c", command], cwd=ws, env=env, capture_output=True, text=True)
    rc_files = [home / ".profile", home / ".bashrc"]
    return result, calls.read_text().splitlines(), [f.read_text() if f.exists() else "" for f in rc_files]


def staged():
    return "set -e; " + tc.mathlib_store_fetch(FETCH, str(store), str(LINKER)) + AGENT


publish(REV)
ws = workspace()
result, lake_calls, rc_files = bootstrap(ws, staged())
check("a linked round resolves packages, then fetches once", lake_calls == ["env true|true", "exe cache get|unset"])
check("a linked round's agent sees LAKE_ARTIFACT_CACHE unset", result.returncode == 0 and seen.read_text() == "unset")
check("a linked round's login shells unset it too", rc_files == ["unset LAKE_ARTIFACT_CACHE\n"] * 2)

ws = workspace(REV3)
result, lake_calls, rc_files = bootstrap(ws, staged())
check("a round with no entry fetches as today", lake_calls == ["env true|true", "exe cache get|true"])
check("a round with no entry keeps Bubble's setting", seen.read_text() == "true" and rc_files == ["", ""])

ws = workspace()
result, lake_calls, rc_files = bootstrap(ws, staged(), linked_rc=1)
check(
    "an entry the fetch rejects is unlinked and the round fetches privately",
    lake_calls == ["env true|true", "exe cache get|unset", "exe cache get|true"] and not links(ws),
)
check("that round keeps Bubble's setting", seen.read_text() == "true" and rc_files == ["", ""])

ws = workspace(REV3)
result, lake_calls, _ = bootstrap(ws, staged(), private_rc=1)
check(
    "a failing Mathlib fetch still stops the round after one retry",
    result.returncode != 0 and not seen.exists() and lake_calls[1:] == ["exe cache get|true"] * 2,
)

inner = "sh -c " + tc.agents._shq(AGENT)
default = tc.bubble_work_cmd("agent --do-work")
check("the default bootstrap has no store step", "mathlib-store" not in default and "lake env" not in default)
check("the default bootstrap leaves the variable to Bubble", "LAKE_ARTIFACT_CACHE" not in default)
# Outside a container neither /opt path exists: the whole bootstrap must still reach the agent.
with patch.object(tc.agents, "TAUCETI_CACHE_SERVICE", ""):
    result, lake_calls, _ = bootstrap(workspace(), tc.bubble_work_cmd(inner, mathlib_store=True))
check(
    "a store that cannot be read behaves as no store",
    result.returncode == 0 and seen.read_text() == "true" and lake_calls[:2] == ["env true|true", "exe cache get|true"],
)


# --- run_in_bubble's argv and the prompt ------------------------------------------------------------
cfg = SimpleNamespace(state=tmp / "state", home=tmp / "home", wid="store-test", logdir=tmp / "logs")
w = SimpleNamespace(cfg=cfg)
opts = SimpleNamespace(work_model="claude")
MOUNT = f"{store}:/opt/mathlib-store:ro"


def echoed(store, **kwargs):
    environ = {"TAUCETI_AGENT_ECHO": "1", "TAUCETI_VERIFY_HELPER": "0"}
    if store is not None:
        environ["TAUCETI_MATHLIB_STORE"] = str(store)
    out = io.StringIO()
    with (
        patch.dict(os.environ, environ),
        patch.object(tc.agents, "ensure_bubble_home", lambda _cfg: dict(os.environ)),
        patch.object(tc.agents, "_bubble_pop", lambda _cfg, _env: None),
        patch.object(tc.agents, "log", lambda *_: None),
        contextlib.redirect_stdout(out),
    ):
        tc.run_in_bubble(w, tc.TAUCETI, "PROMPT", opts, **kwargs)
    return out.getvalue()


os.environ.pop("TAUCETI_MATHLIB_STORE", None)
plain = echoed(None)
work = echoed(store)
check("a work round mounts the store read-only", f"--mount {MOUNT}" in work)
check("a work round stages the linker", os.access(cfg.state / "bubble-round" / "tauceti-mathlib-store", os.X_OK))
check("a work round's bootstrap links before it fetches", "/opt/round/tauceti-mathlib-store" in work)
review = echoed(store, inner_cmd="true", cred_model="claude")
check("a review round mounts nothing", "/opt/mathlib-store" not in review and "/opt/round/tauceti-m" not in review)
check("unset, the argv has no trace of the store", "/opt/mathlib-store" not in plain and "lake env" not in plain)
check("a path that is not a directory is not mounted", echoed(tmp / "absent") == plain)
check("a relative path is not mounted", echoed("store") == plain)

with patch.dict(os.environ, TAUCETI_MATHLIB_STORE=str(store)):
    note = "/opt/round/tauceti-mathlib-store"
    check("a Bubble prompt says how to relink after a pin change", note in tc.local_verification("ordinary", True))
    check("a host prompt does not", note not in tc.local_verification("ordinary", False))
check("unset, the Bubble prompt is unchanged", "/opt/mathlib-store" not in tc.local_verification("ordinary", True))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
