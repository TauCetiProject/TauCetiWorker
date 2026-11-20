#!/usr/bin/env python3
"""The worker-side queue repair task (tauceti_queue.repair_task) against a fake service.

No inference and no network: the queue service is an ``http.server`` in a thread speaking the
controller's task API (stream E's ``claim_task``/``renew_task``/``submit_task``/``release_task`` shapes
and reason codes, served the way stream F's server encodes them), the content
repository is a local bare repository with a three-member attributed chain whose second member breaks
the build, ``lake`` is a stub on PATH that fails modules containing ``BROKEN``, ``gh`` is a stub that
grants admin permission, and ``claude`` is a scripted stub that rewrites the constituent commits the
way the agent is told to. Covered: the claim/renew/push/submit happy path with adoption; claim expiry
stopping the agent without a submission; a chain the service rejects, retried once with its report;
a locally invalid chain retried with the local report; a content change without an explanation,
completed by a follow-up turn in the same session; artifacts unavailable (released, or left to expire
when the service has no release endpoint); inference unavailable after bounded retries; an ordinary
agent failure released as such; a second claimant refused; and a task left mid-attempt abandoned and
its claim released. The long-running worker (``serve``): the claim/run/submit cycle with the provider
outcome reported to ``/tasks/inference`` and idle polling until stopped; a failing service and a
failing task that do not end the loop; SIGTERM stopping the agent and releasing the claim; and the host
preflight checks. The streaming repair: the
worker's background ``lake build --iofail`` (a scripted stub emitting two errors with a pause between
them) launches the agent after the first error with the "Build in progress" section, the error list and
the lock behave as the prompt says, and ``--no-stream-repair`` restores the sequential flow.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="queue-repair-task-"))
BIN = TMP / "bin"
BIN.mkdir()
HOME = TMP / "home"
HOME.mkdir()
os.environ["PATH"] = f"{BIN}:{os.environ.get('PATH', '')}"
# Bind inference to this fixture even when another launch context supplies its own PATH.
os.environ["TAUCETI_CLAUDE_CMD"] = str(BIN / "claude")
os.environ["HOME"] = str(HOME)
os.environ["TAUCETI_CACHE_SERVICE"] = ""  # the worker's own cache warming is not exercised here
os.environ["TAUCETI_WORKER_ID"] = "queue-repair-test"
os.environ["GH_TOKEN"] = "worker-token"
os.environ["TAUCETI_QUEUE_TOKEN"] = "worker-token"
for key in ("TAUCETI_RUNTIME_STATUS", "TAUCETI_FLEET_ADMISSION_FILE", "CLAUDE_CONFIG_DIR", "TAUCETI_STREAM"):
    os.environ.pop(key, None)

from tauceti_queue import chain as chain_tool  # noqa: E402
from tauceti_queue import diagnostics as queue_diagnostics  # noqa: E402
from tauceti_queue import (  # noqa: E402
    diff_guard,
    repair,
    repair_audit,
    repair_context,
    repair_numproj,
    repair_parallel,
    repair_publish,
    repair_sandbox,
    repair_stream,
    repair_targeted,
    repair_task,
    repair_tools,
)

PROJECT = "TauCetiProject/TauCeti"
TOKEN = "worker-token"

# ---- stubs on PATH -------------------------------------------------------------------------------

LAKE = r'''#!/usr/bin/env python3
"""lake stub: `cache get` succeeds unless STUB_LAKE_CACHE_FAIL=1 or the `--rev` is listed in
STUB_LAKE_CACHE_FAIL_REVS (comma separated); from the repair namespace (`--service tauceti-repair`)
only the revisions in STUB_LAKE_REPAIR_REVS are published. `build` fails modules containing BROKEN,
and those named in STUB_LAKE_NUMPROJ (comma separated stems) with a numeric projection, and with `-o FILE` writes an output map naming one artifact in the pool. `cache put` records the
key it was given in `lake-put-key` and fails when STUB_LAKE_PUT_FAIL=1; otherwise it uploads, as Lake
does, each artifact the map names and then the map to `remote/` under STUB_DIR, logging each upload
in `remote/uploads.log`, and with STUB_LAKE_PUT_FAIL=partial fails after the artifacts. `build --iofail` replays the
file STUB_LAKE_BUILD_SCRIPT when set (literal lines, `sleep S`, `exit N`), the first time only. `env CMD ...`
runs CMD, as `lake env` does."""
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
stub = Path(os.environ["STUB_DIR"])
log = stub / "lake-calls.log"
log.open("a").write(" ".join(args) + "\n")
def write_outputs():
    import json
    if "-o" not in args:
        return
    artifacts = Path(os.environ["LAKE_CACHE_DIR"]) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "00aa00aa00aa00aa.ltar").write_bytes(b"x" * 2048)
    mapping = Path(args[args.index("-o") + 1])
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text('"2026-03-17"\n["11bb11bb11bb11bb","00aa00aa00aa00aa.ltar"]\n')
replayed = stub / "build-script-replayed"  # the script is the first whole-library build only
if args[:2] == ["build", "--iofail"] and os.environ.get("STUB_LAKE_BUILD_SCRIPT") and not replayed.exists():
    replayed.touch()
    for line in Path(os.environ["STUB_LAKE_BUILD_SCRIPT"]).read_text().splitlines():
        if line.startswith("sleep "):
            time.sleep(float(line.split()[1]))
        elif line.startswith("wait_file "):
            gate = stub / line.split(maxsplit=1)[1]
            deadline = time.monotonic() + 30
            while not gate.exists():
                if time.monotonic() >= deadline: sys.exit(90)
                time.sleep(0.02)
        elif line.startswith("exit "):
            if int(line.split()[1]) == 0: write_outputs()
            sys.exit(int(line.split()[1]))
        else:
            print(line, flush=True)
    write_outputs()
    sys.exit(0)
if args[:1] == ["env"]:
    os.execvp(args[1], args[1:])
if args[:2] in (["cache", "put"], ["cache", "put-staged"]):
    import json, shutil, subprocess
    (stub / "lake-put-key").write_text(os.environ.get("LAKE_CACHE_KEY", ""))
    (stub / "lake-put-toolchain").write_text(os.environ.get("ELAN_TOOLCHAIN", ""))
    if os.environ.get("STUB_LAKE_PUT_FAIL") == "1":
        sys.exit(1)
    remote = stub / "remote"
    (remote / "artifacts").mkdir(parents=True, exist_ok=True)
    uploads = (remote / "uploads.log").open("a")
    staged = args[1] == "put-staged"
    mapping = Path(args[2]) / "outputs.jsonl" if staged else Path(args[2])
    artifact_root = Path(args[2]) if staged else Path(os.environ["LAKE_CACHE_DIR"]) / "artifacts"
    if staged:
        (stub / "staged-upload-started").touch()
        gate = stub / "staged-upload-gate"
        while gate.exists():
            time.sleep(0.05)
    for line in mapping.read_text().splitlines():
        row = json.loads(line)
        if isinstance(row, list):
            shutil.copy(artifact_root / row[1], remote / "artifacts" / row[1])
            uploads.write(f"artifact {row[1]}\n")
            uploads.flush()
    if os.environ.get("STUB_LAKE_PUT_FAIL") == "partial":
        sys.exit(1)
    rev = (next(a.split("=", 1)[1] for a in args if a.startswith("--rev=")) if staged else
           subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    (remote / "revisions").mkdir(exist_ok=True)
    shutil.copy(mapping, remote / "revisions" / f"{rev}.jsonl")
    uploads.write(f"mapping {rev}\n")
    sys.exit(0)
if args[:2] == ["cache", "get"] and "tauceti-repair" in args:
    rev = next((a[len("--rev="):] for a in args if a.startswith("--rev=")), "")
    if rev not in os.environ.get("STUB_LAKE_REPAIR_REVS", "").split(","):
        print("error: outputs not found for revision")
        sys.exit(1)
    print("downloaded artifact 0022")
    sys.exit(0)
if args[:2] == ["cache", "stage"]:
    import json, shutil
    stage = Path(args[3]); stage.mkdir(parents=True, exist_ok=True)
    mapping = Path(args[2]); shutil.copyfile(mapping, stage / "outputs.jsonl")
    for row in mapping.read_text().splitlines()[1:]:
        name = json.loads(row)[1]
        shutil.copyfile(Path(os.environ["LAKE_CACHE_DIR"]) / "artifacts" / name, stage / name)
    sys.exit(0)
if args[:2] == ["cache", "get"]:
    rev = next((a[len("--rev="):] for a in args if a.startswith("--rev=")), "")
    unpublished = [r for r in os.environ.get("STUB_LAKE_CACHE_FAIL_REVS", "").split(",") if r]
    if os.environ.get("STUB_LAKE_CACHE_FAIL") == "1" or rev in unpublished:
        print("error: outputs not found for revision")
        sys.exit(1)
    print("downloaded artifact 0011")
    sys.exit(0)
if args[:3] == ["exe", "cache", "get"]:
    sys.exit(0)
if args[:1] == ["build"]:
    import json
    skip = {args[args.index("-o") + 1]} if "-o" in args else set()
    named = [a for a in args[1:] if not a.startswith("-") and a not in skip]
    modules = sorted(Path("TauCeti").glob("*.lean"))
    if named:
        modules = [p for p in modules if "TauCeti." + p.stem in named]
    started = time.time()
    time.sleep(float(os.environ.get("STUB_LAKE_BUILD_SLEEP", "0")))
    with (stub / "lake-builds.jsonl").open("a") as handle:
        handle.write(json.dumps({"args": args, "cwd": os.getcwd(), "started": started, "finished": time.time()}) + "\n")
    rc = 0
    linted = [n for n in os.environ.get("STUB_LAKE_NUMPROJ", "").split(",") if n]
    for index, path in enumerate(modules, 1):
        name = "TauCeti." + path.stem
        import re
        projections = [
            (row, m.start())
            for row, text in enumerate(path.read_text().splitlines(), 1)
            for m in re.finditer(r"(?<=[\w)])\.\d+\b", text)
        ] if path.stem in linted else []
        if "BROKEN" in path.read_text():
            print(f"✖ [{index}/{len(modules)}] Building {name}")
            print(f"error: {path}:1:1: unknown identifier BROKEN")
            rc = 1
        elif projections:
            print(f"✖ [{index}/{len(modules)}] Building {name}")
            for row, col in projections:
                print(f"error: {path}:{row}:{col}: Numeric projections are disallowed.")
                print("")
                print("  [apply] Instead, use `.fst`.")
            rc = 1
        else:
            print(f"✔ [{index}/{len(modules)}] Built {name} (0.1s)")
    if "-o" in args and rc == 0:
        artifacts = Path(os.environ["LAKE_CACHE_DIR"]) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "00aa00aa00aa00aa.ltar").write_bytes(b"x" * 2048)
        Path(args[args.index("-o") + 1]).write_text('"2026-03-17"\n["11bb11bb11bb11bb","00aa00aa00aa00aa.ltar"]\n')
    sys.exit(rc)
sys.exit(2)
'''

GH = """#!/usr/bin/env bash
# gh stub: the admin predicate reads repos/<owner>/<repo> and the preflight reads user; everything
# else is unexpected here. Every call is logged; STUB_GH_FAIL makes it fail with that message.
[ -n "$STUB_DIR" ] && echo "$*" >> "$STUB_DIR/gh-calls.log"
if [ -n "$STUB_GH_FAIL" ]; then echo "$STUB_GH_FAIL" >&2; exit 1; fi
case "$*" in
  *repos/*) echo '{"permissions": {"admin": '"${STUB_GH_ADMIN:-true}"'}}' ;;
  *" user") echo '{"login": "stub-worker"}' ;;
  *) echo "gh stub: unexpected call: $*" >&2; exit 1 ;;
esac
"""

CLAUDE = r'''#!/usr/bin/env python3
"""claude / codex stub (installed under both names; the name selects the CLI it imitates): a scripted
repair agent. STUB_AGENT_MODE selects the behaviour (STUB_AGENT_MODES, a comma list, selects it per
call); every call is logged."""
import fcntl, json, os, re, subprocess, sys, time
from pathlib import Path

agent = Path(sys.argv[0]).name  # claude | codex
args = sys.argv[1:]
stub_dir = Path(os.environ["STUB_DIR"])
calls = stub_dir / "agent-calls.jsonl"
prompt, prompt_via, resume = "", None, None
if agent == "claude":
    # Like Claude Code: `-p <prompt>` takes the prompt from argv; a bare `-p` (the next argument is an
    # option, or there is none) reads the whole of stdin as the prompt.
    if "-p" in args:
        after = args[args.index("-p") + 1 :]
        if after and not after[0].startswith("--"):
            prompt, prompt_via = after[0], "argv"
        else:
            prompt, prompt_via = sys.stdin.read(), "stdin"
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
else:
    # Like `codex exec`: the prompt is the last argument, `-` reads it from stdin; `exec resume <thread>`
    # continues a thread.
    if args[-1] == "-":
        prompt, prompt_via = sys.stdin.read(), "stdin"
    else:
        prompt, prompt_via = args[-1], "argv"
    resume = args[-2] if "resume" in args[:3] else None
# Concurrent calls (the parallel path's group agents) number themselves under a lock held until their
# call record is appended, so no two of them count the same lines and take the same number.
call_lock = (stub_dir / "agent-calls.lock").open("a")
fcntl.flock(call_lock, fcntl.LOCK_EX)
previous = sum(1 for _ in calls.open()) if calls.exists() else 0
call = previous + 1
modes = [m for m in os.environ.get("STUB_AGENT_MODES", "").split(",") if m]
mode = modes[min(call, len(modes)) - 1] if modes else os.environ.get("STUB_AGENT_MODE", "repair")
config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
codex_home = os.environ.get("CODEX_HOME")
repair_dir = Path(".tauceti-repair")  # what the worker had written when the agent was launched
errors_file = repair_dir / "errors.jsonl"
launch = {
    "errors": [json.loads(l) for l in errors_file.read_text().splitlines() if l.strip()] if errors_file.exists() else None,
    "lock": (repair_dir / "build.lock").exists(),
    "build_helper": os.access(repair_dir / "build", os.X_OK),
}
started = time.time()
with calls.open("a") as handle:
    handle.write(json.dumps({"call": call, "agent": agent, "mode": mode, "resume": resume, "started": started, "argv": args[:1] + [a for a in args[1:] if a != prompt], "prompt": prompt, "prompt_via": prompt_via, "prompt_chars": len(prompt), "cwd": os.getcwd(), "launch": launch, "env": {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "TAUCETI_QUEUE_TOKEN", "GIT_CONFIG_VALUE_0", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TAUCETI_CLAUDE_BILLING", "BASH_MAX_TIMEOUT_MS", "TAUCETI_SHELL_LIMIT_SECONDS", "LAKE_CACHE_KEY", "TAUCETI_PR_CACHE_MISS")}, "claude_credentials": bool(config_dir) and os.path.exists(os.path.join(config_dir, ".credentials.json")), "codex_credentials": bool(codex_home) and os.path.exists(os.path.join(codex_home, "auth.json"))}) + "\n")
fcntl.flock(call_lock, fcntl.LOCK_UN)
call_lock.close()
session = ("sess-" if agent == "claude" else "thread-") + str(call)
if agent == "claude":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
else:
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)


def git(*a, **kw):
    return subprocess.run(["git", *a], text=True, capture_output=True, check=True, **kw).stdout.strip()


def finish(text="done", rc=0):
    with (stub_dir / "agent-finished.jsonl").open("a") as handle:
        handle.write(json.dumps({"call": call, "agent": agent, "started": started, "finished": time.time(), "rc": rc}) + "\n")
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2}
    if agent == "codex":
        print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": text}}), flush=True)
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5}}), flush=True)
        sys.exit(rc)
    print(json.dumps({"type": "assistant", "session_id": session, "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "text", "text": text}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.01, "result": text, "session_id": session, "usage": usage}), flush=True)
    sys.exit(rc)


def refuse(message):
    """The provider refused the request before any work: Codex's error event, Claude's error result."""
    if agent == "codex":
        print(json.dumps({"type": "error", "message": message}), flush=True)
    else:
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0, "result": message, "session_id": session}), flush=True)
    sys.exit(1)


def rebuild(explain=True, drop=None):
    """Replay HEAD's chain onto its base, fixing the broken member in place. For a conflict task, fix the
    working tree only and write the explanation file: the worker makes the commit."""
    if "## Resolving the commit" in prompt or "is missing or empty" in prompt:
        for path in Path("TauCeti").glob("*.lean"):
            text = path.read_text()
            if "BROKEN" in text or "<<<<<<<" in text:
                lines = [l for l in text.replace("BROKEN", "trivial").splitlines() if not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
                path.write_text("\n".join(lines) + "\n")
        if explain:
            (repair_dir / "explanation.md").write_text("Repair: replaced the unknown identifier with `trivial`; the batch-mate #1 renamed it.\n")
        return
    match = re.search(r"linear chain on `([0-9a-f]{12})`", prompt)
    base = git("rev-parse", match.group(1))
    retained = re.search(r"retained at `(refs/[^`]+)`", prompt)
    start = retained.group(1) if retained else "HEAD"
    shas = git("rev-list", "--reverse", "--first-parent", f"{base}..{start}").split()
    git("checkout", "--quiet", "--detach", base)
    for sha in shas:
        message = git("log", "-1", "--format=%B", sha)
        if drop and f"Source-PR: {drop}" in message:
            continue
        git("cherry-pick", "--allow-empty", "--keep-redundant-commits", sha)
        broken = [p for p in Path("TauCeti").glob("*.lean") if "BROKEN" in p.read_text()]
        for path in broken:
            path.write_text(path.read_text().replace("BROKEN", "trivial"))
        edited = broken or "Source-PR: TauCetiProject/TauCeti#2" in message  # the member this fixture breaks
        if edited and explain and "Repair:" not in message:
            message = message.rstrip("\n") + "\n\nRepair: replaced the unknown identifier with `trivial`; the batch-mate #1 renamed it.\n"
        if broken or edited:
            git("add", "-A")
            git("commit", "--quiet", "--amend", "--allow-empty", "-F", "-", input=message)


def fixup():
    """A group agent: fix BROKEN in the files the prompt allows, as fixup! + squash! commits on top of HEAD."""
    allowed = re.findall(r"`([^`]+\.lean)`", prompt.split("Files you may edit: ", 1)[1].split("\n", 1)[0])
    failing = os.environ.get("STUB_FAIL_GROUP_WITH")
    if failing and failing in allowed:
        finish("this group's agent gave up", rc=1)
    barrier = int(os.environ.get("STUB_GROUP_BARRIER", "0"))
    if barrier:
        # Wait until that many group agents have arrived, so they provably run at the same time.
        arrived = stub_dir / "group-barrier"
        arrived.mkdir(exist_ok=True)
        (arrived / str(call)).touch()
        deadline = time.time() + 60
        while len(list(arrived.iterdir())) < barrier:
            if time.time() > deadline:
                finish("the other group agents never arrived", rc=4)
            time.sleep(0.05)
    time.sleep(float(os.environ.get("STUB_GROUP_SLEEP", "1.0")))
    for path in allowed:
        file = Path(path)
        if not file.exists() or "BROKEN" not in file.read_text():
            continue
        owner = re.search(r"`" + re.escape(path) + r":\d+:\d+` \(PR #(\d+)'s commit `([0-9a-f]{12})`", prompt)
        sha = owner.group(2)
        file.write_text(file.read_text().replace("BROKEN", "trivial"))
        git("add", path)
        git("commit", "--quiet", f"--fixup={sha}")
        git("commit", "--quiet", "--allow-empty", f"--squash={sha}", "-m", f"Repair: replaced the unknown identifier in {path} with `trivial`.")
    if os.environ.get("STUB_GROUP_STRAY") in allowed:
        Path("TauCeti/A.lean").write_text("theorem a : True := trivial -- stray\n")
        git("add", "TauCeti/A.lean")
        git("commit", "--quiet", f"--fixup={sha}")
    if os.environ.get("STUB_GROUP_BUILD") == "1":
        build = subprocess.run(["python3", ".tauceti-repair/build", *re.findall(r"\.tauceti-repair/build ([^`]+)`", prompt)[0].split()], text=True, capture_output=True)
        finish(f"done; build exit {build.returncode}")


if "## Your part of the repair" in prompt:
    fixup()
    finish()
elif mode == "repair":
    rebuild()
    if agent == "codex" and Path(".tauceti-repair/check").exists():
        # The helper the prompt names is plain Python and runs the same under either CLI.
        check = subprocess.run([".tauceti-repair/check"], text=True, capture_output=True)
        finish(f"done; check: {check.stdout.strip() or check.stderr.strip()} (exit {check.returncode})")
    finish()
elif mode == "no-explanation":
    rebuild(explain=resume is not None)
    finish()
elif mode == "explain-only":
    # Resolve the conflict markers by keeping both sides, and change nothing else.
    for path in Path("TauCeti").glob("*.lean"):
        text = path.read_text()
        if "<<<<<<<" in text:
            path.write_text("\n".join(l for l in text.splitlines() if not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))) + "\n")
    (repair_dir / "explanation.md").write_text("Kept both sides of the conflict.\n")
    finish()
elif mode == "absorb":
    rebuild()
    Path("TauCeti/Three.lean").write_text("theorem three : True := trivial\n")
    finish()
elif mode == "move-history":
    # Resolve, then try to finish the replay by hand: commit, cherry-pick what follows, move HEAD.
    rebuild()
    git("add", "-A")
    git("commit", "--quiet", "-m", "agent's own commit")
    own = git("rev-parse", "HEAD")
    for sha in os.environ.get("STUB_LATER_COMMITS", "").split():
        git("cherry-pick", "--allow-empty", "--keep-redundant-commits", sha)
    git("update-ref", "refs/heads/agent-replay", "HEAD")
    git("reset", "--quiet", "--hard", own)
    git("reset", "--quiet", "--soft", os.environ["STUB_MOVE_HEAD_TO"])
    finish()
elif mode == "drop-member":
    rebuild(drop=None if call > 1 else "TauCetiProject/TauCeti#3")
    finish()
elif mode == "sleep":
    time.sleep(float(os.environ.get("STUB_SLEEP", "30")))
    finish()
elif mode == "529":
    print("API Error: 529 Overloaded", flush=True)
    sys.exit(1)
elif mode == "usage-limit":
    if agent == "codex":
        refuse("usage_limit_reached: You've hit your usage limit. Try again later.")
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 0}}), flush=True)
    refuse("You've hit your usage limit.")
elif mode == "auth-error":
    refuse("401 Unauthorized: Not logged in" if agent == "codex" else "Failed to authenticate. API Error: 401 Invalid authentication credentials")
elif mode == "work-then-fail":
    print(json.dumps({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "ls", "exit_code": 0}} if agent == "codex" else {"type": "assistant", "session_id": session, "message": {"id": "m0", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}), flush=True)
    refuse("the provider dropped the connection after the first tool call")
elif mode == "noop":
    finish("nothing to do")
elif mode == "resume-only":
    if resume is None:
        finish("expected a resumed session", rc=3)
    rebuild()
    finish()
elif mode == "stream":
    # As the prompt asks: fix what has failed, check it with the scoped build helper (which waits for the
    # worker's build to release the lock), and record what the helper returned and what the list said.
    rebuild()
    if (stub_dir / "stream-handshake").exists():
        process = subprocess.Popen([str(repair_dir / "build"), "TauCeti.Two"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = []
        for line in process.stdout:
            first.append(line)
            if "waiting for .tauceti-repair/build.lock" in line:
                break
        (stub_dir / "stream-helper-waiting").touch()
        output, error = process.communicate(timeout=30)
        helper = subprocess.CompletedProcess(process.args, process.returncode, "".join(first) + output, error)
    else:
        helper = subprocess.run([str(repair_dir / "build"), "TauCeti.Two"], text=True, capture_output=True)
    deadline = time.time() + 30
    while (repair_dir / "build.lock").exists() and time.time() < deadline:
        time.sleep(0.1)
    record = {"helper_rc": helper.returncode, "helper_out": helper.stdout, "lock_after": (repair_dir / "build.lock").exists(),
              "errors_after": [json.loads(l) for l in errors_file.read_text().splitlines() if l.strip()]}
    (stub_dir / "stream-agent.json").write_text(json.dumps(record))
    finish()
else:
    finish(f"unknown mode {mode}", rc=2)
'''

BWRAP = r'''#!PYTHON
"""bwrap stub: records its arguments and environment, then runs the command after `--` from
`--chdir` with the `--setenv` values. The `env -i` before it cleared STUB_DIR, so that comes from
sandbox-env.json beside this script, and PATH is led by this directory so the lake stub is found."""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
here = Path(__file__).resolve().parent
extra = json.loads((here / "sandbox-env.json").read_text())
with (Path(extra["STUB_DIR"]) / "bwrap-calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": args, "environ": dict(os.environ)}) + "\n")
split = args.index("--")
env, cwd, i = {}, None, 0
while i < split:
    if args[i] == "--setenv":
        env[args[i + 1]] = args[i + 2]
        i += 3
    elif args[i] == "--chdir":
        cwd = args[i + 1]
        i += 2
    else:
        i += 1
env.update(extra)
env["PATH"] = f"{here}:{env.get('PATH', '')}"
if cwd:
    os.chdir(cwd)
os.execvpe(args[split + 1], args[split + 1 :], env)
'''.replace("PYTHON", sys.executable)

LEAN = r"""#!/bin/sh
# lean stub: `lean --run ModuleSystem.lean`, the module-system check, reports $STUB_DIR/module-system-findings.
echo "$PWD" >> "$STUB_DIR/module-system-runs.log"
if [ -f "$STUB_DIR/module-system-findings" ]; then cat "$STUB_DIR/module-system-findings" >&2; exit 1; fi
echo "module-system: all TauCeti module(s) opt into the module system."
"""

for name, text in (
    ("lake", LAKE),
    ("gh", GH),
    ("claude", CLAUDE),
    ("codex", CLAUDE),
    ("bwrap", BWRAP),
    ("lean", LEAN),
):
    path = BIN / name
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---- a fake TauCetiTools at a pinned commit -------------------------------------------------------
#
# ``python -m tauceti_tools materialize DEST --conf C --policy-dir P`` copies the shared scripts and the
# policy files, as the real setup step does. The driver builders write stand-in executables; the
# ``audit-driver`` prints the lines of ``$STUB_DIR/audit-findings`` once (the file is then renamed, as if the
# agent's fix removed the duplicate) or of ``$STUB_DIR/audit-findings-always`` on every run, exiting 1 when
# it printed any; ``lint-env.sh`` records its scope and does the same with ``lint-findings``. ``lint-style.sh``
# and ``lint-dot-notation.py`` print ``$STUB_DIR/style-findings`` and ``$STUB_DIR/dot-notation-findings``.

TOOLS_MAIN = r"""
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "materialize", args
dest = Path(args[1])
policy = Path(args[args.index("--policy-dir") + 1])
assert Path(args[args.index("--conf") + 1]).is_file()
shutil.copytree(Path(__file__).parent / "assets" / "scripts", dest)
for source in policy.glob("*.txt") if policy.is_dir() else ():
    shutil.copyfile(source, dest / source.name)
"""

AUDIT_DRIVER = r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
stub = Path(os.environ["STUB_DIR"])
with (stub / "audit-runs.log").open("a") as log:
    log.write(os.getcwd() + "\n")
print("audit-driver: phase audit-import")
phase_file = os.environ.get("AUDIT_PHASES_FILE")
if phase_file:
    with Path(phase_file).open("a") as phases:
        phases.write(f"audit-import\t{time.time()}\n")
lines = []
for name in ("audit-findings", "audit-findings-always"):
    path = stub / name
    if path.exists():
        lines += path.read_text().splitlines()
        if name == "audit-findings":
            path.rename(stub / "audit-findings.reported")
print("audit-driver: phase audit-duplicates")
if phase_file:
    with Path(phase_file).open("a") as phases:
        phases.write(f"audit-duplicates\t{time.time()}\n")
for line in lines:
    print(line, file=sys.stderr)
if not lines:
    print("duplicate-declarations: checked 4 TauCeti modules and 0 dependency modules; no duplicate declarations.")
sys.exit(1 if lines else 0)
"""

LINT_DRIVER = r"""#!/usr/bin/env python3
import os, sys, json
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "--only-listed", args
_, tag, marker, scope, *linters = args
stub = Path(os.environ["STUB_DIR"])
with (stub / "targeted-lint-runs.jsonl").open("a") as log:
    log.write(json.dumps({"modules": Path(scope).read_text().splitlines(), "linters": linters}) + "\n")
if (stub / "targeted-lint-fail").exists():
    print("simpNF: finding persists")
    sys.exit(1)
if not (stub / "targeted-no-marker").exists():
    print(Path(marker).read_text(), end="")
"""

TOOLS_SCRIPTS = {
    "build-lint-driver.sh": 'cp "$(dirname "$2")/lint-driver.py" "$3/lint-env-driver"; chmod +x "$3/lint-env-driver"\n',
    "lint-driver.py": LINT_DRIVER,
    "build-audit-driver.sh": 'cp "$2/audit-driver.py" "$3/audit-driver"; chmod +x "$3/audit-driver"\n',
    "audit-driver.py": AUDIT_DRIVER,
    "LintEnvDriver.lean": "-- stand-in\n",
    "Axioms.lean": "-- stand-in\n",
    "lint-env.sh": (
        'if [ -n "$LINT_ONLY_MODULES" ]; then cp "$LINT_ONLY_MODULES" "$STUB_DIR/lint-scope.txt"; '
        'else find TauCeti -name "*.lean" | sort | sed "s|/|.|g;s|.lean$||" > "$STUB_DIR/lint-scope.txt"; fi\n'
        'test -x "$LINT_DRIVER_EXE" || { echo "LINT-ENV: FAIL no driver"; exit 1; }\n'
        'if [ -f "$STUB_DIR/lint-findings" ]; then cat "$STUB_DIR/lint-findings"; mv "$STUB_DIR/lint-findings" '
        '"$STUB_DIR/lint-findings.reported"; echo "LINT-ENV: FAIL"; exit 1; fi\n'
        'echo "LINT-ENV: PASS"\n'
    ),
    "ModuleSystem.lean": "-- stand-in\n",
    "lint-style.sh": (
        'echo "$PWD" >> "$STUB_DIR/style-runs.log"\n'
        'if [ -f "$STUB_DIR/style-findings" ]; then cat "$STUB_DIR/style-findings"; exit 1; fi\n'
    ),
    "lint-dot-notation.py": (
        "import os, sys\n"
        "from pathlib import Path\n"
        "assert Path(sys.argv[sys.argv.index('--baseline') + 1]).is_file()\n"
        "findings = Path(os.environ['STUB_DIR']) / 'dot-notation-findings'\n"
        "print(findings.read_text() if findings.exists() else 'lint-dot-notation: 0 new', end='')\n"
        "sys.exit(1 if findings.exists() else 0)\n"
    ),
    "lint-dot-notation-baseline.txt": "",
}


def make_tools_repository(root: Path) -> tuple[Path, str]:
    repo = root / "TauCetiTools"
    package = repo / "tauceti_tools"
    (package / "assets" / "scripts").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(TOOLS_MAIN)
    for name, text in TOOLS_SCRIPTS.items():
        (package / "assets" / "scripts" / name).write_text(text)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    sh(repo, "add", "-A")
    sh(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "tools")
    return repo, sh(repo, "rev-parse", "HEAD")


# ---- the content repository ----------------------------------------------------------------------


def sh(repo: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
        env={**os.environ, **(env or {})},
    ).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        sh(repo, "add", name)
    sh(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


def commit_on(repo: Path, parent: str, files: dict[str, str]) -> str:
    """A commit on ``parent`` with ``files`` written, made through a private index."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        sh(repo, "read-tree", parent, env=env)
        for name, content in files.items():
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content, capture_output=True, text=True
            ).stdout.strip()
            sh(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=env)
        tree = sh(repo, "write-tree", env=env)
    return sh(repo, "commit-tree", tree, "-p", parent, "-m", "fixture")


class Content:
    """A bare content repository, a seed clone that built the chain, and the worker's clone."""

    def __init__(
        self,
        root: Path,
        broken: tuple[int, ...] = (2,),
        base_files: dict[str, str] | None = None,
        member_files: dict[int, dict[str, str]] | None = None,
    ):
        self.broken = broken
        self.bare = root / "content.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(self.bare)], check=True)
        seed = root / "seed"
        subprocess.run(["git", "clone", "--quiet", str(self.bare), str(seed)], check=True, capture_output=True)
        sh(seed, "config", "user.name", "Fixture")
        sh(seed, "config", "user.email", "fixture@example.invalid")
        sh(seed, "checkout", "--quiet", "-b", "main")
        self.base = commit(seed, "base", {"TauCeti/A.lean": "theorem a : True := trivial\n", **(base_files or {})})
        sh(seed, "push", "--quiet", "origin", "main")
        heads = {}
        for number, name in ((1, "One"), (2, "Two"), (3, "Three")):
            body = "BROKEN" if number in broken else "trivial"
            files = {f"TauCeti/{name}.lean": f"theorem {name.lower()} : True := {body}\n"}
            files.update((member_files or {}).get(number, {}))
            sh(seed, "checkout", "--quiet", "--detach", self.base)
            heads[number] = commit(seed, f"pr {number}", files)
        members = [
            chain_tool.Member(PROJECT, n, heads[n], title=f"feat: add {n}", description=f"Body of {n}.", coauthors=())
            for n in (1, 2, 3)
        ]
        built = chain_tool.build(seed, self.base, members, chain_id="fixture")
        assert built.status == chain_tool.CLEAN, built
        self.tip = built.tip
        self.members = [
            {
                "source_pr": f"{PROJECT}#{n}",
                "number": n,
                "accepted_head": heads[n],
                "commit": c.sha,
                "provenance": "clean",
                "title": f"feat: add {n}",
                "description": f"Body of {n}.",
                "author": "alice",
                "explanation": "",
                "retained_ref": f"refs/tauceti-queue/inputs/{n}/{heads[n]}",
            }
            for n, c in zip((1, 2, 3), built.commits, strict=True)
        ]
        sh(seed, "push", "--quiet", "origin", f"{self.base}:refs/heads/queue/batch-1/base")
        sh(seed, "push", "--quiet", "origin", f"{self.tip}:refs/heads/queue/batch-1/tip")
        self.clone = root / "clone"
        subprocess.run(["git", "clone", "--quiet", str(self.bare), str(self.clone)], check=True)
        sh(self.clone, "config", "user.name", "Worker")
        sh(self.clone, "config", "user.email", "worker@example.invalid")

    def task(self, task_id: int = 1, kind: str = "repair") -> dict:
        """The controller's Task record (``ports.Task.to_dict``) before the claim fields are added."""
        return {
            "task_id": task_id,
            "kind": kind,
            "repository": PROJECT,
            "batch_id": 7,
            "batch_version": 3,
            "base": self.base,
            "tip": self.tip,
            "onto": self.base,
            "members": self.members,
            "diagnostics": [
                entry
                for name in ("One", "Two", "Three")
                if ("One", "Two", "Three").index(name) + 1 in self.broken
                for entry in (
                    {
                        "kind": "compiler",
                        "module": f"TauCeti/{name}.lean",
                        "message": f"TauCeti/{name}.lean:1:20: unknown identifier BROKEN",
                    },
                    {"kind": "build", "module": f"TauCeti/{name}.lean", "message": "module failed to build"},
                )
            ],
            "retained_refs": ["refs/heads/queue/batch-1/base", "refs/heads/queue/batch-1/tip"],
            "instruction": repair.AGENT_INSTRUCTION,
            "created_at": time.time(),
            "extra": {"canonical_branch": "main"},
        }

    def rebase_task(self, task_id: int = 1, kind: str = "rebase_conflict") -> dict:
        """A rebase-conflict task: main moved past the batch base, so ``onto`` differs from ``base``, and
        added its own ``TauCeti/Two.lean``, so replaying the chain puts member 1 on ``onto`` (the clean
        prefix) and conflicts at member 2's commit. The worker replays the chain itself."""
        seed = self.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.base)
        onto = commit(
            seed,
            "main moved",
            {"TauCeti/A.lean": "theorem a : True := by trivial\n", "TauCeti/Two.lean": "-- two, on main\n"},
        )
        prefix = chain_tool.replay(seed, self.base, self.members[0]["commit"], onto, chain_id="prefix").tip
        sh(seed, "push", "--quiet", "origin", f"{onto}:refs/heads/queue/batch-1/onto")
        task = self.task(task_id, kind=kind)
        task["onto"] = onto
        task["retained_refs"] = [*task["retained_refs"], "refs/heads/queue/batch-1/onto"]
        task["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#2",
                "commit": self.members[1]["commit"],
                "paths": ["TauCeti/Two.lean"],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#2",
                "pending": [f"{PROJECT}#3"],
                "clean_prefix_tip": prefix,
            }
        ]
        return task

    def pre_rebased_task(self, task_id: int = 1) -> dict:
        """A ``prepare`` task of the batch's pre-rebased chain: the head's tip ``H`` (one head member on
        the base) is ``base`` and ``onto``, and ``tip`` is the batch's members replayed onto it."""
        seed = self.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.base)
        head = commit(seed, "head member", {"TauCeti/Head.lean": "theorem head : True := trivial\n"})
        members = [
            chain_tool.Member(PROJECT, m["number"], m["accepted_head"], title=m["title"], description=m["description"])
            for m in self.members
        ]
        built = chain_tool.build(seed, head, members, chain_id="pre-rebased")
        assert built.status == chain_tool.CLEAN, built
        sh(seed, "push", "--quiet", "origin", f"{head}:refs/heads/queue/head/tip")
        sh(seed, "push", "--quiet", "origin", f"{built.tip}:refs/heads/queue/batch-1/pre-rebased")
        task = self.task(task_id, kind="prepare")
        task.update(base=head, tip=built.tip, onto=head, diagnostics=[])
        task["members"] = [dict(m, commit=c.sha) for m, c in zip(self.members, built.commits, strict=True)]
        task["retained_refs"] = [
            *task["retained_refs"],
            "refs/heads/queue/head/tip",
            "refs/heads/queue/batch-1/pre-rebased",
        ]
        task["extra"] = {
            **task["extra"],
            "renew_after": 0.2,
            "pre_rebased": {"source": self.tip, "batch_base": self.base, "head_batch_id": 6, "head_base": self.base},
        }
        return task


# ---- the fake service ----------------------------------------------------------------------------


class FakeService:
    """The task API with the controller's semantics: claims with expiry within a concurrency of one,
    renewals, submissions validated with the chain tool (``validate`` on ``onto``, ``compare`` against
    the prior chain, ``explanation-required``), the ``release`` call, and the controller's reason codes
    and ``Adoption`` shape, encoded the way stream F's server does (bare JSON results, ``{"error"}``
    bodies, a ``TaskError`` as 409 with its code). Scripted ``Adoption`` responses override the
    validation."""

    def __init__(self, bare: Path, tasks: list[dict], *, ttl: float = 5.0, renew_after: float = 0.2):
        self.bare = bare
        self.tasks = {int(t["task_id"]): dict(t) for t in tasks}
        self.ttl = ttl
        self.renew_after = renew_after
        self.claims: dict[int, dict] = {}  # claim id -> record
        self.done: set[int] = set()
        self.results: list[dict] = []
        self.releases: list[dict] = []
        self.renewals = 0
        self.claim_attempts: list[dict] = []
        self.result_script: list[dict] = []
        self.inference_reports: list[dict] = []
        self.renew_mode = "ok"  # ok | refuse | error
        # Renewals answered 401 before the next is served, as while the service cannot resolve the
        # worker's GitHub identity.
        self.unauthenticated_renewals = 0
        self.claim_mode = "ok"  # ok | error
        self.release_mode = "ok"  # ok | error
        self.release_supported = True
        self.inference_supported = True
        self.lock = threading.Lock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, payload):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_GET(self):
                if self.path == "/healthz":
                    self.respond(200, {"ok": True, "revision": "fakefakefake"})
                else:
                    self.respond(404, {"error": "no such route"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self.respond(*service.handle(self.path, body, self.headers.get("Authorization")))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- controller semantics

    def live_claim(self, task_id: int) -> dict | None:
        now = time.time()
        for claim in self.claims.values():
            if claim["task_id"] == task_id and claim["released_at"] is None and claim["expires_at"] > now:
                return claim
        return None

    def _claim_for(self, task_id: int, token: str) -> dict:
        """E's ``_claim_for``: TaskError unknown-claim / claim-superseded / claim-expired."""
        claim = next((c for c in self.claims.values() if c["task_id"] == task_id and c["token"] == token), None)
        if claim is None:
            raise LookupError("unknown-claim")
        if claim["released_at"] is not None:
            raise LookupError("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
        if claim["expires_at"] <= time.time():
            claim["released_at"] = time.time()
            claim["release_reason"] = "expired"
            raise LookupError("claim-expired")
        return claim

    def task_record(self, task_id: int, claim: dict) -> dict:
        task = self.tasks[task_id]
        return {
            **task,
            "submission_ref": f"refs/tauceti-queue/submissions/{task_id}/{claim['id']}",
            "claim_token": claim["token"],
            "expires_at": claim["expires_at"],
            "extra": {**task.get("extra", {}), "claim_id": claim["id"], "renew_after": self.renew_after},
        }

    def preclaim(self, task_id: int, token: str, worker: str) -> dict:
        claim = {
            "id": len(self.claims) + 1,
            "task_id": task_id,
            "token": token,
            "worker": worker,
            "claimed_at": time.time(),
            "expires_at": time.time() + self.ttl,
            "released_at": None,
            "release_reason": None,
        }
        self.claims[claim["id"]] = claim
        return claim

    def claim_task(self, worker: str) -> dict | None:
        self.claim_attempts.append({"worker": worker})
        for task_id in self.tasks:
            if task_id in self.done or self.live_claim(task_id) is not None:  # repair_concurrency = 1
                continue
            return self.task_record(task_id, self.preclaim(task_id, secrets.token_hex(16), worker))
        return None

    def renew_task(self, task_id: int, token: str) -> float:
        claim = self._claim_for(task_id, token)
        if self.renew_mode == "refuse":
            claim["released_at"] = time.time()
            claim["release_reason"] = "superseded"
            raise LookupError("claim-superseded")
        if self.renew_mode == "error":
            raise RuntimeError("database unavailable")
        self.renewals += 1
        claim["expires_at"] = time.time() + self.ttl
        return claim["expires_at"]

    def release_task(self, task_id: int, token: str, reason: str, error: str) -> dict:
        claim = self._claim_for(task_id, token)
        claim["released_at"] = time.time()
        claim["release_reason"] = reason
        self.releases.append({"task_id": task_id, "reason": reason, "error": error, "worker": claim["worker"]})
        return {"task_id": task_id, "released": True}

    def submit_task(self, task_id: int, token: str, tip_sha: str, worker_notes: str, *, checkpoint=False) -> dict:
        task = self.tasks[task_id]
        claim = next((c for c in self.claims.values() if c["task_id"] == task_id and c["token"] == token), None)
        if claim is None:
            raise LookupError("unknown-claim")
        record = {"task_id": task_id, "tip_sha": tip_sha, "worker_notes": worker_notes, "claim_id": claim["id"]}

        def adoption(accepted: bool, reason: str, violations=(), version=None) -> dict:
            response = {
                "accepted": accepted,
                "reason": reason,
                "task_id": task_id,
                "batch_id": task["batch_id"],
                "version": version,
                "tip": tip_sha,
                "retained_ref": f"refs/tauceti-queue/batches/{task['batch_id']}/submissions/{tip_sha}",
                "violations": [v.to_dict() if hasattr(v, "to_dict") else v for v in violations],
                "provenance": {},
            }
            record["response"] = response
            self.results.append(record)
            return response

        for previous in self.results:
            if (
                previous["claim_id"] == claim["id"]
                and previous["tip_sha"] == tip_sha
                and previous["response"].get("accepted")
            ):
                return dict(previous["response"], reason="already-adopted", version=None)
            if (
                checkpoint
                and previous["claim_id"] == claim["id"]
                and previous["tip_sha"] == tip_sha
                and previous["response"]["reason"] == "repair-checkpoint"
            ):
                return previous["response"]
        if self.result_script:
            scripted = self.result_script.pop(0)
            return adoption(scripted.get("accepted", False), scripted.get("reason", ""), scripted.get("violations", ()))
        if claim["released_at"] is not None:
            return adoption(False, "claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
        if claim["expires_at"] <= time.time():
            return adoption(False, "claim-expired")
        ref = f"refs/tauceti-queue/submissions/{task_id}/{claim['id']}"
        try:
            pushed = sh(self.bare, "rev-parse", "--verify", ref)
        except subprocess.CalledProcessError:
            return adoption(False, "tip-unavailable")
        if pushed != tip_sha:
            return adoption(False, "tip-unavailable")
        members = [m["source_pr"] for m in task["members"]]
        report = chain_tool.validate(self.bare, task["onto"], tip_sha, members)
        if not report.ok:
            return adoption(False, "invalid-chain", report.violations)
        comparison = chain_tool.compare(self.bare, task["base"], task["tip"], task["onto"], tip_sha)
        if not comparison.ok:
            return adoption(False, "invalid-chain", comparison.violations)
        unexplained = [m for m in comparison.members if m.provenance == chain_tool.EDITED and not m.explanation.strip()]
        if unexplained:
            return adoption(
                False,
                "explanation-required",
                [
                    {
                        "code": "explanation-required",
                        "detail": f"{m.source_pr}: no explanation",
                        "source_pr": m.source_pr,
                    }
                    for m in unexplained
                ],
            )
        if checkpoint:
            task.setdefault("extra", {})["repair_checkpoint"] = {
                "tip": tip_sha,
                "diagnostics": json.loads(worker_notes)["repair_checkpoint"],
            }
            task["retained_refs"].append(ref)
            claim["released_at"] = time.time()
            claim["release_reason"] = "repair-checkpoint"
            return adoption(False, "repair-checkpoint")
        self.done.add(task_id)
        claim["released_at"] = time.time()
        claim["release_reason"] = "adopted"
        return adoption(True, "adopted", version=task["batch_version"] + 1)

    # -- F's encoding

    def handle(self, path: str, body: dict, auth: str | None):
        if auth != f"Bearer {TOKEN}":
            return 401, {"error": "a GitHub token in `Authorization: Bearer` is required"}
        with self.lock:
            try:
                if path == "/tasks/inference":
                    if not self.inference_supported:
                        return 404, {"error": "no such route"}
                    self.inference_reports.append(
                        {"worker": body.get("worker"), "ok": body.get("ok"), "error": body.get("error")}
                    )
                    return 204, None
                if path == "/tasks/claim":
                    if self.claim_mode == "error":
                        self.claim_attempts.append({"worker": str(body.get("worker") or ""), "error": True})
                        raise RuntimeError("database unavailable")
                    result = self.claim_task(str(body.get("worker") or ""))
                    return (204, None) if result is None else (200, result)
                match = re.fullmatch(r"/tasks/([^/]+)/(renew|result|release|checkpoint)", path)
                if not match:
                    return 404, {"error": "no such route"}
                task_id, verb = int(match.group(1)), match.group(2)
                if verb == "release" and not self.release_supported:
                    return 404, {"error": "no such route"}
                if task_id not in self.tasks:
                    raise LookupError("unknown-task")
                token = str(body.get("claim_token") or "")
                if verb == "renew":
                    if self.unauthenticated_renewals > 0:
                        self.unauthenticated_renewals -= 1
                        return 401, {"error": "a GitHub token in `Authorization: Bearer` is required"}
                    return 200, self.renew_task(task_id, token)
                if verb == "release":
                    if self.release_mode == "error":
                        raise RuntimeError("database unavailable")
                    return 200, self.release_task(task_id, token, str(body.get("reason")), str(body.get("error", "")))
                return 200, self.submit_task(
                    task_id,
                    token,
                    str(body.get("tip_sha") or ""),
                    str(body.get("worker_notes", "")),
                    checkpoint=verb == "checkpoint",
                )
            except LookupError as error:  # TaskError: a stable code in the body
                return 409, {"error": str(error), "code": str(error)}
            except RuntimeError as error:
                return 500, {"error": str(error)}


# ---- tests ---------------------------------------------------------------------------------------


class Fixture(unittest.TestCase):
    BROKEN = (2,)  # the members whose module fails to build
    BASE_FILES: dict[str, str] | None = None  # more files in the base commit (``tauceti.toml``)
    MEMBER_FILES: dict[int, dict[str, str]] | None = None  # more files in a member's commit, by number

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=TMP))
        self.content = Content(
            self.root, broken=self.BROKEN, base_files=self.BASE_FILES, member_files=self.MEMBER_FILES
        )
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        os.environ["STUB_DIR"] = str(self.stub_dir)
        os.environ["STUB_AGENT_MODE"] = "repair"
        os.environ.pop("STUB_AGENT_MODES", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CODEX_HOME", None)
        # Every subscription is under pace unless a scenario says otherwise; the pacer's real readers
        # (``Quota.claude`` / ``Quota.codex``) are exercised by ``Providers.test_the_status_is_the_pacer_s``.
        self.statuses: dict[str, tuple[bool, str]] = {}
        self._real_subscription_status = repair_task.subscription_status
        status = patch.object(
            repair_task,
            "subscription_status",
            lambda cfg, provider: self.statuses.get(provider, (True, "under pace (test)")),
        )
        status.start()
        self.addCleanup(status.stop)
        os.environ.pop("STUB_LAKE_CACHE_FAIL", None)
        os.environ.pop(repair_sandbox.BACKGROUND_VARIABLE, None)
        os.environ.pop("STUB_LAKE_CACHE_FAIL_REVS", None)
        os.environ.pop("STUB_LAKE_REPAIR_REVS", None)
        os.environ.pop("STUB_LAKE_PUT_FAIL", None)
        os.environ.pop("STUB_LAKE_BUILD_SCRIPT", None)
        os.environ.pop("STUB_LAKE_NUMPROJ", None)
        os.environ.pop("STUB_SLEEP", None)
        os.environ.pop("STUB_GH_FAIL", None)
        os.environ.pop("STUB_GH_ADMIN", None)
        os.environ.pop(repair_sandbox.DISABLE_VARIABLE, None)
        os.environ.pop("INVOCATION_ID", None)  # background publishers are subprocesses here, not units
        background = patch.object(repair_publish, "BACKGROUND", repair_publish.Background())
        background.start()
        self.addCleanup(background.stop)
        self.addCleanup(lambda: repair_publish.BACKGROUND.wait(60))
        (BIN / "sandbox-env.json").write_text(json.dumps({"STUB_DIR": str(self.stub_dir)}))
        for key in (
            "STUB_FAIL_GROUP_WITH",
            "STUB_GROUP_STRAY",
            "STUB_GROUP_BUILD",
            "STUB_GROUP_BARRIER",
            "STUB_LAKE_BUILD_SLEEP",
        ):
            os.environ.pop(key, None)
        os.environ["STUB_GROUP_SLEEP"] = "1.0"
        self.service = FakeService(self.content.bare, [self.content.task()])
        self.addCleanup(self.service.close)

    def config(self, **overrides) -> repair_task.RunnerConfig:
        values = dict(
            service=self.service.url,
            clone=self.content.clone,
            root=self.root / "state",
            worker="worker-a@test",
            repository=PROJECT,
            token=TOKEN,
            agent_timeout=60,
            build_timeout=60,
            inference_delays=(0.0,),
            service_delays=(0.0,),
            service_attempts=2,
            lease_retry_interval=0.1,
            push_remote=str(self.content.bare),
            mathlib_cache=True,
            fetch_retry_seconds=0.0,  # the scenarios that wait for late refs set their own window
            fetch_retry_delay=0.05,
            release_backoff=(0.0,),
            artifacts_backoff=(0.0, 0.0),
            providers=("claude",),
            publish=repair_publish.Settings(),
            parallel_agents=1,  # the parallel path has its own scenarios
            lookahead_helpers=0,  # the lookahead has its own scenarios
        )
        values.update(overrides)
        return repair_task.RunnerConfig(**values)

    def break_launch(self, name: str) -> None:
        """Make the ``name`` stub impossible to start (an interpreter that does not exist), restored after
        the test: ``Popen`` fails with ENOENT, which is ``AgentLaunchUnavailable``."""
        path = BIN / name
        original = path.read_text()
        path.write_text("#!/nonexistent/interpreter\n" + original)
        self.addCleanup(path.write_text, original)

    def client(self, cfg: repair_task.RunnerConfig, worker: str | None = None) -> repair_task.TaskClient:
        return repair_task.TaskClient(cfg.service, TOKEN, worker or cfg.worker)

    def pre_rebase_task(self) -> dict:
        """Task 1 as a ``pre_rebase`` task whose replay conflicts at member 2's commit (``rebase_task``)."""
        task = self.content.rebase_task(1, kind="pre_rebase")
        self.service.tasks[1] = task
        return task

    def agent_calls(self) -> list[dict]:
        path = self.stub_dir / "agent-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def lake_calls(self) -> list[str]:
        path = self.stub_dir / "lake-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def bwrap_calls(self) -> list[dict]:
        path = self.stub_dir / "bwrap-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def lake_builds(self) -> list[dict]:
        path = self.stub_dir / "lake-builds.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def agent_finishes(self) -> list[dict]:
        path = self.stub_dir / "agent-finished.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self, task_id="1") -> dict:
        return json.loads((self.root / "state" / "tasks" / task_id / "task.json").read_text())

    def submission_ref(self, claim_id: int = 1, task_id: int = 1) -> str:
        return f"refs/tauceti-queue/submissions/{task_id}/{claim_id}"

    @staticmethod
    def padded_prompts(chars: int) -> contextlib.AbstractContextManager:
        """Every composed prompt grows by ``chars`` of trailing context the scripted agent ignores."""
        compose = repair.compose_prompt
        padding = "\n\nContext the agent may ignore:\n" + ("x" * 99 + "\n") * (chars // 100 + 1)
        return patch.object(repair, "compose_prompt", lambda *a, **k: compose(*a, **k) + padding)

    @staticmethod
    def prompt_too_long_for_argv() -> contextlib.AbstractContextManager:
        """The production failure: with the stdin path disabled, a prompt above Linux's 128 KiB limit on one
        argv string (MAX_ARG_STRLEN) makes the kernel reject the exec with E2BIG, "Argument list too long"."""
        stack = contextlib.ExitStack()
        stack.enter_context(patch("tauceti_worker.agents.PROMPT_ARGV_LIMIT", 1 << 40))
        stack.enter_context(Fixture.padded_prompts(129 * 1024))
        return stack


class HappyPath(Fixture):
    def test_claim_renew_push_submit_adopt(self):
        cfg = self.config()
        progress_phases = {}
        write_progress = repair_task.TaskState.write_progress

        def capture_progress(state, final=None):
            write_progress(state, final)
            progress_phases[state.state] = json.loads((state.directory / "progress.json").read_text())

        with patch.object(repair_task.TaskState, "write_progress", capture_progress):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        # The service saw one claim, renewals while the agent ran, and one result with the pushed ref.
        self.assertEqual(len(self.service.results), 1)
        result = self.service.results[0]
        self.assertEqual(result["tip_sha"], outcome["tip"])
        self.assertTrue(result["response"]["accepted"])
        self.assertEqual(outcome["version"], 4)
        self.assertEqual(json.loads(result["worker_notes"])["edited"], [f"{PROJECT}#2"])
        self.assertGreaterEqual(outcome["lease"]["renewals"], 1)
        self.assertFalse(outcome["lease"]["lost"])
        # The pushed chain is on the bare repository, validates, and member #2 is edited with an explanation.
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertEqual(pushed, outcome["tip"])
        self.assertEqual(outcome["pushed_ref"], self.submission_ref())
        comparison = chain_tool.compare(
            self.content.bare, self.content.base, self.content.tip, self.content.base, pushed
        )
        by_pr = {m.source_pr: m for m in comparison.members}
        self.assertEqual(by_pr[f"{PROJECT}#2"].provenance, chain_tool.EDITED)
        self.assertTrue(by_pr[f"{PROJECT}#2"].message_preserved)
        self.assertIn("Repair:", by_pr[f"{PROJECT}#2"].explanation)
        self.assertEqual(by_pr[f"{PROJECT}#1"].provenance, chain_tool.CLEAN)
        self.assertEqual(by_pr[f"{PROJECT}#3"].provenance, chain_tool.CLEAN)
        # Artifacts were prepared before the agent: Mathlib cache, base cache at the base; the incremental
        # build is the worker's streaming build, which the agent was launched into.
        lake = self.lake_calls()
        self.assertEqual(lake[0], "exe cache get")
        self.assertIn(f"--rev={self.content.base}", lake[1])
        self.assertTrue(lake[2].startswith("build --iofail"))
        # Only the build ran in the sandbox, from a cleared environment; the cache fetches need the network.
        sandboxed = self.bwrap_calls()
        # The streaming build, then the worker's verification build of the agent's chain.
        self.assertEqual(
            [c["argv"][c["argv"].index("--") + 1 :] for c in sandboxed], [["lake", "build", "--iofail"]] * 2
        )
        self.assertEqual(len(outcome["verification"]), 1)
        self.assertTrue(outcome["verification"][0]["ok"])
        self.assertEqual(outcome["verification"][0]["checks"]["status"], "unavailable", "this fixture has no pin")
        self.assertFalse({"GH_TOKEN", "HOME", "PATH", "STUB_DIR"} & set(sandboxed[0]["environ"]))
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertFalse(outcome["artifacts"]["incremental_build"], "the streaming build replaces the prepare build")
        self.assertEqual(outcome["stream_build"]["failed_modules"], ["TauCeti.Two"])
        self.assertTrue(outcome["stream_build"]["finished"])
        # One agent call with the plan's instruction, the diagnostics, the members and no credentials.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        prompt = calls[0]["prompt"]
        self.assertTrue(prompt.startswith(repair.AGENT_INSTRUCTION))
        self.assertIn("`TauCeti/Two.lean:1:20`", prompt)
        self.assertIn("unknown identifier BROKEN", prompt)
        self.assertIn("PR #2's commit `", prompt)
        self.assertIn("- build in `TauCeti/Two.lean` (PR #2's commit `", prompt)
        self.assertIn("## What happened", prompt)
        self.assertIn("nothing to push, publish or report anywhere else, and `gh` is not needed", prompt)
        self.assertIn(".tauceti-repair/members.jsonl", prompt)
        self.assertIn("## Likely collisions and renames", prompt)
        self.assertNotIn("repair task 1", prompt, "task identifiers are not in the prompt")
        self.assertNotIn(f"Source-PR: {PROJECT}#3", prompt, "the member listings are in the file")
        self.assertIsNone(calls[0]["env"]["GH_TOKEN"])
        self.assertIsNone(calls[0]["env"]["TAUCETI_QUEUE_TOKEN"], "the worker's own token stays out of agents")
        self.assertTrue(calls[0]["env"]["GIT_CONFIG_VALUE_0"].endswith("no-push"))
        self.assertTrue(calls[0]["env"]["CLAUDE_CONFIG_DIR"].startswith(str(self.root / "state")))
        self.assertEqual(calls[0]["cwd"], str(self.root / "state" / "worktrees" / "task-1"))
        # Checkpoints, retained refs, cost accounting and cleanup.
        state = self.state()
        self.assertEqual(state["state"], "adopted")
        states = [h["state"] for h in state["history"]]
        for expected in (
            "claimed",
            "sources_ready",
            "artifacts_ready",
            "agent_running",
            "validated",
            "pushed",
            "submitted",
        ):
            self.assertIn(expected, states)
        self.assertEqual(sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/attempt-1"), outcome["tip"])
        self.assertEqual(sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/input"), self.content.tip)
        self.assertIsNotNone(outcome["agent"][0]["attempts"][0]["estimated_cost_usd"])
        written = json.loads((self.root / "state" / "tasks" / "1" / "outcome.json").read_text())
        self.assertEqual(written["status"], "adopted")
        progress = json.loads((self.root / "state" / "tasks" / "1" / "progress.json").read_text())
        self.assertEqual(progress["state"], "adopted")
        self.assertEqual(progress["seconds"], written["seconds"])
        self.assertEqual(progress["cost"]["recorded_usd"], outcome["agent"][0]["attempts"][0]["estimated_cost_usd"])
        for phase in ("validated", "verifying"):
            self.assertEqual(progress_phases[phase]["cost"]["recorded_attempts"], 1)
            self.assertEqual(progress_phases[phase]["cost"]["recorded_usd"], progress["cost"]["recorded_usd"])
        self.assertNotIn("progress_outcome", state)
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())
        self.assertIsNone(repair_task.claim_and_run(cfg, self.client(cfg)), "the adopted task is not claimable again")

    def test_the_batch_tree_is_parked_and_the_next_task_starts_warm_without_fetching(self):
        """One tree per batch: the first task restores from the caches and parks its tree; the batch's
        next task adopts it, fetches nothing and builds only incrementally."""
        cfg = self.config()
        self.service.tasks = {1: self.content.task()}
        first = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(first["status"], "adopted", first)
        self.assertEqual(first["sources"]["batch_tree"], "new")
        self.assertEqual(first["worktree"], "parked")
        parked = cfg.root / "worktrees" / "batch-7"
        self.assertTrue(parked.is_dir())
        self.assertFalse((cfg.root / "worktrees" / "task-1").exists())
        fetched = len(self.lake_calls())
        self.assertTrue(any("cache get" in call for call in self.lake_calls()))
        self.service.tasks = {2: self.content.task(task_id=2)}
        second = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(second["status"], "adopted", second)
        self.assertEqual(second["sources"]["batch_tree"], "warm")
        self.assertTrue(second["artifacts"]["warm_tree"])
        self.assertTrue(second["artifacts"]["available"])
        later = self.lake_calls()[fetched:]
        self.assertFalse([call for call in later if "cache get" in call], later)
        self.assertTrue(any(call.startswith("build --iofail") for call in later))
        self.assertEqual(self.agent_calls()[-1]["cwd"], str(cfg.root / "worktrees" / "task-2"))
        self.assertTrue(parked.is_dir(), "parked again for the batch's next task")

    def test_second_claimant_and_stale_token_are_refused(self):
        cfg = self.config()
        first = self.client(cfg, "worker-a@test").claim()
        self.assertIsNotNone(first)
        second = self.client(cfg, "worker-b@test")
        self.assertIsNone(second.claim(), "a claimed task is not offered to a second worker")
        self.assertEqual(first.submission_ref, self.submission_ref(1))
        self.assertEqual(first.onto, self.content.base)
        self.assertEqual(first.members[1].number, 2)
        self.assertAlmostEqual(first.renew_after, 0.2)
        stale = repair_task.Task.from_json({**first.to_json(), "claim_token": "not-the-token"})
        with self.assertRaises(repair_task.ClaimRefused) as refused:
            second.submit(stale, first.tip, first.submission_ref, {})
        self.assertIn("unknown-claim", str(refused.exception))
        with self.assertRaises(repair_task.ClaimRefused):
            second.renew(stale)
        self.assertEqual(self.service.results, [])
        # Once the first claim expires the task is claimable again, by anyone.
        self.service.claims[1]["expires_at"] = time.time() - 1
        third = second.claim()
        self.assertIsNotNone(third)
        self.assertEqual(third.submission_ref, self.submission_ref(2), "a new claim gets its own submission ref")


class ClaimExpiry(Fixture):
    def test_lost_claim_stops_the_agent_and_submits_nothing(self):
        os.environ["STUB_AGENT_MODE"] = "sleep"
        os.environ["STUB_SLEEP"] = "30"
        cfg = self.config()
        self.service.renew_mode = "refuse"
        started = time.monotonic()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "claim_lost", outcome)
        self.assertLess(time.monotonic() - started, 15, "the sleeping agent was killed when the claim was lost")
        self.assertEqual(self.service.results, [])
        self.assertEqual(self.service.releases, [])
        self.assertTrue(outcome["lease"]["lost"])
        self.assertEqual(self.state()["state"], "claim_lost")
        self.assertFalse(sh(self.content.bare, "rev-parse", "--verify", "-q", self.submission_ref(), check=False))

    def test_unauthenticated_renewals_keep_the_claim_until_one_succeeds(self):
        cfg = self.config()
        client = self.client(cfg)
        task = client.claim()
        claimed_until = task.expires_at
        self.service.unauthenticated_renewals = 2
        with self.assertRaises(repair_task.ServiceError, msg="a 401 is not a refused claim"):
            client.renew(task)
        lease = repair_task.ClaimLease(client, task, retry_interval=0.1)
        lease.start()
        self.addCleanup(lease.stop)
        deadline = time.monotonic() + 4
        while lease.renewals < 2 and not lease.lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(lease.lost.is_set(), lease.lost_reason)
        self.assertFalse(lease.cancel.is_set(), "the agent is not stopped")
        self.assertEqual(lease.errors, 1)
        self.assertGreaterEqual(lease.renewals, 2, "the renewal after the 401 continues the claim")
        self.assertGreater(task.expires_at, claimed_until)

    def test_a_401_renewal_followed_by_a_success_keeps_the_task_running(self):
        cfg = self.config()
        self.service.unauthenticated_renewals = 1
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertFalse(outcome["lease"]["lost"])
        self.assertEqual(self.service.unauthenticated_renewals, 0, "the 401 was served")
        self.assertEqual(len(self.service.results), 1)

    def test_renew_errors_keep_the_agent_running(self):
        cfg = self.config()
        self.service.renew_mode = "error"  # the coordinator is unreachable but the claim has not expired
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertGreaterEqual(outcome["lease"]["errors"], 1)


class ValidationRetry(Fixture):
    def test_service_rejection_is_retried_once_with_its_report(self):
        cfg = self.config()
        self.service.result_script = [
            {
                "accepted": False,
                "reason": "invalid-chain",
                "violations": [{"code": "accepted-head-mismatch", "detail": "#2 head differs"}],
            }
        ]
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 2)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("A previous attempt was rejected", calls[1]["prompt"])
        self.assertIn("accepted-head-mismatch: #2 head differs", calls[1]["prompt"])
        self.assertIsNone(calls[1]["resume"], "a rejected attempt starts a fresh session")
        # The second push updated the submission ref in place (CAS against the first push).
        self.assertEqual(sh(self.content.bare, "rev-parse", self.submission_ref()), outcome["tip"])

    def test_explanation_required_resubmits_in_the_same_session(self):
        cfg = self.config()
        self.service.result_script = [
            {
                "accepted": False,
                "reason": "explanation-required",
                "violations": [
                    {"code": "explanation-required", "detail": "#2: no explanation", "source_pr": f"{PROJECT}#2"}
                ],
            }
        ]
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 2)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["resume"], "sess-1", "the same claim adds the explanation in its session")
        self.assertIn(f"{PROJECT}#2", calls[1]["prompt"])
        self.assertNotIn(repair.AGENT_INSTRUCTION, calls[1]["prompt"])

    def test_a_terminal_rejection_ends_the_task(self):
        cfg = self.config()
        self.service.result_script = [{"accepted": False, "reason": "version-mismatch"}]
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "not_adopted", outcome)
        self.assertEqual(outcome["reason"], "version-mismatch")
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(len(self.agent_calls()), 1)

    def test_a_superseded_claim_at_submission_is_a_lost_claim(self):
        cfg = self.config()
        self.service.result_script = [{"accepted": False, "reason": "claim-superseded"}]
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "claim_lost", outcome)
        self.assertIn("claim-superseded", outcome["detail"])

    def test_persistent_rejection_is_not_retried_again(self):
        cfg = self.config()
        self.service.result_script = [
            {"accepted": False, "reason": "invalid-chain", "violations": [{"code": "wrong-base", "detail": "x"}]},
            {"accepted": False, "reason": "invalid-chain", "violations": [{"code": "wrong-base", "detail": "x"}]},
        ]
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "not_adopted", outcome)
        self.assertEqual(len(self.service.results), 2)
        self.assertEqual(self.state()["state"], "not_adopted")

    def test_locally_invalid_chain_is_retried_with_the_local_report(self):
        os.environ["STUB_AGENT_MODE"] = "drop-member"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 1, "the invalid chain was never submitted")
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("Local validation of the previous attempt", calls[1]["prompt"])
        self.assertIn("missing-member", calls[1]["prompt"])


class MissingExplanation(Fixture):
    def test_follow_up_turn_adds_the_explanation_in_the_same_session(self):
        os.environ["STUB_AGENT_MODE"] = "no-explanation"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["resume"], "sess-1", "the explanation turn continues the same session")
        self.assertIn("without appending an explanation", calls[1]["prompt"])
        self.assertIn(f"{PROJECT}#2", calls[1]["prompt"])
        self.assertNotIn(repair.AGENT_INSTRUCTION, calls[1]["prompt"])
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        comparison = chain_tool.compare(
            self.content.bare, self.content.base, self.content.tip, self.content.base, pushed
        )
        two = next(m for m in comparison.members if m.source_pr == f"{PROJECT}#2")
        self.assertTrue(two.content_changed and two.explanation.startswith("Repair:"))
        self.assertEqual(len(self.service.results), 1)


class ArtifactsUnavailable(Fixture):
    def test_release_with_reason_before_any_inference(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertIn(f"no published cache mapping for {self.content.base[:12]}", outcome["detail"])
        self.assertEqual(self.agent_calls(), [], "the agent never ran cold")
        self.assertEqual(len(self.service.releases), 1)
        self.assertEqual(self.service.releases[0]["reason"], "artifacts_unavailable")
        self.assertIn("no published cache mapping", self.service.releases[0]["error"])
        self.assertEqual(self.service.results, [])
        self.assertEqual(self.state()["state"], "released")
        self.assertIsNone(self.service.live_claim(1), "the service freed the claim")

    def test_without_a_release_endpoint_the_claim_is_left_to_expire(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        self.service.release_supported = False
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "release_unsupported", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertEqual(self.service.releases, [])
        self.assertEqual(self.service.results, [])
        self.assertIsNotNone(self.service.live_claim(1), "the claim is held until it expires on its own")
        self.assertFalse(outcome["lease"]["lost"])

    def test_a_repair_on_a_published_base_fetches_the_base_only(self):
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        fetches = [c.split("--rev=")[1] for c in self.lake_calls() if c.startswith("cache get")]
        self.assertEqual(fetches, [self.content.base])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.content.base)

    def test_revision_order_by_task_kind(self):
        repair_raw = self.content.task()
        rebase_raw = self.content.rebase_task()
        claim = {"claim_token": "x", "expires_at": time.time() + 60, "extra": {"claim_id": 1}}
        repair_same = repair_task.Task.from_json({**repair_raw, **claim})
        self.assertEqual(repair_task.artifact_revisions(repair_same), [self.content.base])
        repair_moved = repair_task.Task.from_json({**repair_raw, **claim, "onto": rebase_raw["onto"]})
        self.assertEqual(repair_task.artifact_revisions(repair_moved), [self.content.base, rebase_raw["onto"]])
        rebase = repair_task.Task.from_json({**rebase_raw, **claim})
        self.assertEqual(repair_task.artifact_revisions(rebase), [rebase_raw["onto"], self.content.base])


class RebaseArtifacts(Fixture):
    """A rebase conflict builds on ``onto``: its published cache is fetched first, the stale base is
    the fallback, and with neither the rebase runs without warm artifacts instead of being released."""

    def setUp(self):
        super().setUp()
        self.raw = self.content.rebase_task()
        self.onto = self.raw["onto"]
        self.service.tasks = {1: self.raw}

    def fetches(self) -> list[str]:
        return [c.split("--rev=")[1] for c in self.lake_calls() if c.startswith("cache get")]

    def submitted_chain(self) -> list:
        """The chain the worker pushed, checked as the queue checks it: every member on ``onto``."""
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        members = [m["source_pr"] for m in self.content.members]
        report = chain_tool.validate(self.content.bare, self.onto, pushed, members)
        self.assertTrue(report.ok, report)
        comparison = chain_tool.compare(self.content.bare, self.content.base, self.content.tip, self.onto, pushed)
        self.assertTrue(comparison.ok, comparison)
        return chain_tool.chain_commits(self.content.bare, self.onto, pushed)

    def test_explanation_follow_up_preserves_the_new_rebase_target(self):
        self.raw["kind"] = "pre_rebase"
        os.environ["STUB_AGENT_MODE"] = "no-explanation"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["resume"], "sess-1")
        self.assertIn(f"Your resolution of {PROJECT}#2's commit is in the working tree", calls[1]["prompt"])
        self.assertIn("explanation.md` is missing or empty", calls[1]["prompt"])
        one, two, three = self.submitted_chain()
        self.assertNotIn("BROKEN", sh(self.content.bare, "show", f"{two.sha}:TauCeti/Two.lean"))
        self.assertIn("Repair: replaced the unknown identifier", two.message)
        self.assertTrue(
            chain_tool.same_change(
                self.content.bare, chain_tool.read_commit(self.content.bare, self.content.members[2]["commit"]), three
            )
        )
        self.assertEqual([c["source_pr"] for c in outcome["conflicts"]], [f"{PROJECT}#2"])

    def test_the_agent_cannot_replay_or_move_history(self):
        """The agent commits its resolution, cherry-picks the next member and moves HEAD: the worker
        commits only the working tree on the clean prefix with the member's message, and replays
        member 3 itself."""
        os.environ["STUB_AGENT_MODE"] = "move-history"
        os.environ["STUB_LATER_COMMITS"] = self.content.members[2]["commit"]
        os.environ["STUB_MOVE_HEAD_TO"] = self.content.tip
        self.addCleanup(os.environ.pop, "STUB_LATER_COMMITS", None)
        self.addCleanup(os.environ.pop, "STUB_MOVE_HEAD_TO", None)
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        one, two, three = self.submitted_chain()
        self.assertEqual(two.parents, (one.sha,))
        self.assertIn(f"Source-PR: {PROJECT}#2", two.message)
        self.assertNotIn("agent's own commit", two.message)
        self.assertNotEqual(three.tree, two.tree, "member 3 keeps its own content")

    def test_a_service_rejection_of_a_rebased_chain_is_not_sent_back_to_the_agent(self):
        """The agent only ever sees one conflicting commit: a rejection of the whole chain ends the task."""
        self.service.result_script = [
            {
                "accepted": False,
                "reason": "explanation-required",
                "violations": [{"code": "explanation-required", "source_pr": f"{PROJECT}#2"}],
            }
        ]
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual((outcome["status"], outcome["reason"]), ("not_adopted", "explanation-required"))
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(len(self.agent_calls()), 1)

    def test_every_conflict_is_resolved_on_one_claim(self):
        """Main also changed member 3's file: the worker resolves member 2, replays on, stops at member 3
        and resolves it in the same task, then submits the whole chain once."""
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        moved = commit(seed, "main moved again", {"TauCeti/Three.lean": "-- three, on main\n"})
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        self.raw["onto"] = self.onto = moved
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual([c["source_pr"] for c in outcome["conflicts"]], [f"{PROJECT}#2", f"{PROJECT}#3"])
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn(f"{PROJECT}#3", calls[0]["prompt"], "the first round does not see the later commit")
        self.assertIn(f"PR #3's commit `{self.content.members[2]['commit'][:12]}`", calls[1]["prompt"])
        self.assertEqual(len(self.service.results), 1, "one submission: the whole chain")
        one, two, three = self.submitted_chain()
        self.assertEqual(three.parents, (two.sha,), "the second round starts from the first resolution")
        for path in ("TauCeti/Two.lean", "TauCeti/Three.lean"):
            self.assertNotIn("<<<<<<<", sh(self.content.bare, "show", f"{three.sha}:{path}"))

    def test_a_later_conflict_is_resolved_ahead_on_the_target(self):
        """With a lookahead helper, member 3's conflict with main is resolved on ``onto`` alone while member 2's
        is resolved on the clean prefix; at member 3 the replay merges that resolution onto its real
        prefix and checks it like any other, and the agent is not run a third time."""
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        moved = commit(seed, "main moved again", {"TauCeti/Three.lean": "-- three, on main\n"})
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        self.raw["onto"] = self.onto = moved
        cfg = self.config(lookahead_helpers=1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual([c["source_pr"] for c in outcome["conflicts"]], [f"{PROJECT}#2", f"{PROJECT}#3"])
        self.assertEqual([c.get("lookahead", False) for c in outcome["conflicts"]], [False, True])
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2, "one agent per conflict")
        ahead = [c for c in calls if f"PR #3's commit `{self.content.members[2]['commit'][:12]}`" in c["prompt"]]
        self.assertEqual(len(ahead), 1)
        self.assertNotIn(f"PR #2's commit `{self.content.members[1]['commit'][:12]}`", ahead[0]["prompt"])
        self.assertEqual(len(outcome["agent"]), 2, "the lookahead's agent run is recorded with the task's")
        one, two, three = self.submitted_chain()
        self.assertEqual(three.parents, (two.sha,))
        for path in ("TauCeti/Two.lean", "TauCeti/Three.lean"):
            self.assertNotIn("<<<<<<<", sh(self.content.bare, "show", f"{three.sha}:{path}"))
        self.assertEqual(sh(self.content.clone, "for-each-ref", repair_task.RESOLUTION_PREFIX), "")
        self.assertEqual(list((cfg.root / "worktrees").glob("task-1-lookahead-*")), [])
        self.assertIn("build Mathlib", self.lake_calls(), "the packages are replayed once for the helpers")

    def test_the_head_task_gets_all_the_helpers_and_background_work_half(self):
        from tauceti_queue import repair_lookahead

        self.assertEqual(repair_lookahead.DEFAULT_HELPERS, 16)
        cfg = self.config(lookahead_helpers=16)
        head = types.SimpleNamespace(kind="rebase_conflict", extra={})
        background = types.SimpleNamespace(kind="rebase_conflict", extra={"background": True})
        self.assertEqual(repair_lookahead.helper_count(cfg, head), 16)
        self.assertEqual(repair_lookahead.helper_count(cfg, background), 8)
        self.assertEqual(repair_lookahead.helper_count(self.config(lookahead_helpers=1), background), 1)

    def test_a_chain_that_changes_the_dependencies_gets_no_lookahead(self):
        """Helpers share packages prepared at ``onto``: a member changing ``lake-manifest.json`` turns them off."""
        from tauceti_queue import repair_lookahead

        clone = self.content.clone
        sh(clone, "fetch", "--quiet", str(self.content.bare), self.onto, self.content.tip)
        task = repair_task.Task.from_json(
            {**self.raw, "claim_token": "x", "expires_at": time.time() + 60, "extra": {"claim_id": 1}}
        )
        self.assertFalse(repair_lookahead.changes_dependencies(clone, task))
        bumped = commit_on(clone, self.content.tip, {"lake-manifest.json": "{}\n"})
        self.assertTrue(repair_lookahead.changes_dependencies(clone, dataclasses.replace(task, tip=bumped)))

    def test_a_lookahead_resolution_that_does_not_merge_onto_the_prefix_is_not_used(self):
        """``recommit_resolution`` with ``made_on`` merges the resolution's change onto the prefix and
        gives None when that merge conflicts."""
        clone = self.content.clone
        sh(clone, "fetch", "--quiet", str(self.content.bare), self.onto, self.content.tip)
        conflicting = self.content.members[1]["commit"]
        original = chain_tool.read_commit(clone, conflicting)

        def resolution(parent: str, text: str) -> str:
            tree = chain_tool.tree_of(clone, commit_on(clone, parent, {"TauCeti/Two.lean": text}))
            return chain_tool.commit_tree(
                clone,
                tree,
                parent,
                original.message.rstrip() + "\n\nResolved.\n",
                (original.author_name, original.author_email),
                author_date=original.author_date,
                committer_date=original.author_date,
            )

        ahead = resolution(self.onto, "-- two, resolved on the target\n")
        prefix = commit_on(clone, self.onto, {"TauCeti/Two.lean": "-- two, changed by an earlier member\n"})
        self.assertIsNone(
            repair.recommit_resolution(clone, ahead, prefix, conflicting, ["TauCeti/Two.lean"], made_on=self.onto)
        )
        other = commit_on(clone, self.onto, {"TauCeti/Other.lean": "-- elsewhere\n"})
        sha, report = repair.recommit_resolution(
            clone, ahead, other, conflicting, ["TauCeti/Two.lean"], made_on=self.onto
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(sh(clone, "show", f"{sha}:TauCeti/Two.lean"), "-- two, resolved on the target")
        self.assertEqual(sh(clone, "show", f"{sha}:TauCeti/Other.lean"), "-- elsewhere")

    def test_the_lookahead_skips_a_member_an_earlier_run_resolved(self):
        """After a restart a member resolved on its prefix by the earlier run is the replay's to reuse:
        no helper resolves it again on ``onto``."""
        from tauceti_queue import repair_lookahead

        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        moved = commit(seed, "main moved again", {"TauCeti/Three.lean": "-- three, on main\n"})
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        self.raw["onto"] = self.onto = moved
        clone = self.content.clone
        sh(clone, "fetch", "--quiet", str(self.content.bare), self.onto, self.content.tip)
        task = repair_task.Task.from_json(
            {**self.raw, "claim_token": "x", "expires_at": time.time() + 60, "extra": {"claim_id": 1}}
        )
        lookahead = types.SimpleNamespace(cfg=types.SimpleNamespace(clone=clone), task=task, stopped=lambda: False)
        three = self.content.members[2]["commit"]

        def candidates() -> list[str]:
            return [c.sha for c in repair_lookahead.Lookahead.candidates(lookahead, self.content.members[1]["commit"])]

        self.assertEqual(candidates(), [three])
        sh(
            clone,
            "update-ref",
            repair_task.resolution_ref("1", three, chain_tool.tree_of(clone, self.onto) + "x"),
            three,
        )
        self.assertEqual(candidates(), [])

    def test_a_stopped_task_resumes_after_its_last_resolution(self):
        """The worker stops while the agent resolves member 3, after member 2's resolution was accepted.
        Another worker sharing the clone re-claims the task later, so its replay makes the same prefix
        under another SHA: member 2's conflict is not given to the agent again, and the submitted chain
        is the one an uninterrupted run builds."""
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        moved = commit(seed, "main moved again", {"TauCeti/Three.lean": "-- three, on main\n"})
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        self.raw["onto"] = self.onto = moved
        # Fixed committer dates make the replays reproducible, so the chains can be compared by SHA.
        dates = patch.dict(os.environ, {"GIT_COMMITTER_DATE": "1700000000 +0000"})
        dates.start()
        self.addCleanup(dates.stop)

        def contents(tip: str) -> list[tuple]:
            return [
                (c.tree, c.message, c.author_name, c.author_date)
                for c in chain_tool.chain_commits(clone, self.onto, tip)
            ]

        clone = self.content.clone
        stored = lambda: sh(clone, "for-each-ref", "--format=%(refname)", repair_task.RESOLUTION_PREFIX).split()  # noqa: E731

        # The reference: task 2, the same chain and target, run without a stop.
        self.service.tasks = {2: {**self.raw, "task_id": 2}}
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        reference = chain_tool.chain_commits(clone, self.onto, outcome["tip"])
        self.assertEqual(len(reference), 3)
        self.assertEqual(stored(), [], "an answered task leaves no stored resolution")

        self.service.tasks = {1: self.raw}
        os.environ["STUB_AGENT_MODES"] = "repair,repair,repair,sleep"  # task 2 made calls 1 and 2
        os.environ["STUB_SLEEP"] = "60"
        stop = repair_task.Stop()
        self.addCleanup(stop.set, "cleanup")

        def stop_in_the_second_conflict():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and len(self.agent_calls()) < 4:
                time.sleep(0.05)
            time.sleep(0.5)
            stop.set("test")

        threading.Thread(target=stop_in_the_second_conflict, daemon=True).start()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg), stop=stop)
        self.assertEqual((outcome["status"], outcome["reason"]), ("released", "worker-stopped"), outcome)
        self.assertIn("PR #3's commit", self.agent_calls()[3]["prompt"], "stopped in the second conflict")
        self.assertEqual(len(stored()), 1, "member 2's accepted resolution is recorded")
        recorded = sh(clone, "rev-parse", stored()[0])
        self.assertEqual(recorded, reference[1].sha, "the run before the stop made the reference's commit")

        os.environ.pop("STUB_AGENT_MODES")
        os.environ["GIT_COMMITTER_DATE"] = "1700003600 +0000"  # the restart replays an hour later
        other = self.config(root=self.root / "state-b", worker="worker-b@test")
        outcome = repair_task.claim_and_run(other, self.client(other))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 5, "one more agent run: member 3's conflict")
        self.assertIn(f"PR #3's commit `{self.content.members[2]['commit'][:12]}`", calls[4]["prompt"])
        self.assertNotIn(f"PR #2's commit `{self.content.members[1]['commit'][:12]}`", calls[4]["prompt"])
        self.assertEqual([c.get("reused", False) for c in outcome["conflicts"]], [True, False])
        resumed = chain_tool.chain_commits(clone, self.onto, outcome["tip"])
        self.assertNotEqual(resumed[0].sha, reference[0].sha, "the prefix was replayed under another SHA")
        self.assertEqual(contents(outcome["tip"]), contents(reference[-1].sha), "the same chain")
        self.assertEqual(stored(), [])

    def test_a_stored_resolution_that_does_not_match_is_not_used(self):
        """An entry naming another commit's resolution under this commit's key is not reused: the agent
        resolves the conflict and the entry is dropped."""
        cfg = self.config()
        clone = self.content.clone
        sh(clone, "fetch", "--quiet", str(self.content.bare), self.onto, self.content.tip)
        prefix = chain_tool.replay(clone, self.content.base, self.content.members[0]["commit"], self.onto).tip
        conflicting = self.content.members[1]["commit"]
        ref = repair_task.resolution_ref("1", conflicting, chain_tool.tree_of(clone, prefix))
        sh(clone, "update-ref", ref, self.content.members[2]["commit"])
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertEqual([c.get("reused", False) for c in outcome["conflicts"]], [False])
        self.assertEqual(sh(clone, "for-each-ref", repair_task.RESOLUTION_PREFIX), "")

    def test_a_resolution_that_takes_in_a_later_member_is_refused(self):
        """Replaying member 3 by hand inside member 2's resolution is refused before the replay continues."""
        os.environ["STUB_AGENT_MODE"] = "absorb"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(self.service.releases[-1]["reason"], "agent_failed")
        self.assertIn("absorbed-later-member", self.service.releases[-1]["error"])
        self.assertIn("absorbed-later-member", self.agent_calls()[1]["prompt"], "the retry is told why")
        self.assertEqual(self.service.results, [])

    def observe_log(self) -> list[str]:
        lines: list[str] = []
        original = repair_task.log
        repair_task.log = lines.append
        self.addCleanup(setattr, repair_task, "log", original)
        return lines

    def advance_main(self) -> str:
        """``main`` moves once more past ``onto``: the new rebase target's first parent is the old one,
        not the batch base."""
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        moved = commit(seed, "main moved again", {"TauCeti/B.lean": "theorem b : True := trivial\n"})
        prefix = chain_tool.replay(seed, self.content.base, self.content.members[0]["commit"], moved).tip
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        sh(seed, "push", "--quiet", "--force", "origin", f"{prefix}:refs/heads/queue/batch-1/conflict")
        self.raw["onto"] = moved
        self.raw["diagnostics"][0].update(onto=moved, clean_prefix_tip=prefix)
        self.service.tasks = {1: self.raw}
        return moved

    def test_changed_rebase_dependencies_are_prepared_on_target_and_input_is_restored(self):
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.onto)
        target = commit(seed, "bump dependency", {"lake-manifest.json": '{"packages": []}\n'})
        sh(seed, "fetch", "--quiet", str(self.content.bare), self.content.tip)
        task = repair_task.Task.from_json(
            {**self.raw, "onto": target, "claim_token": "x", "expires_at": time.time() + 60}
        )
        sh(seed, "checkout", "--quiet", "--detach", self.content.tip)
        package = seed / ".lake" / "packages" / "hydrated"
        for fail in (False, True):
            try:
                with repair_task.artifact_checkout(seed, task, self.content.tip):
                    self.assertEqual(sh(seed, "rev-parse", "HEAD"), target)
                    self.assertTrue((seed / "lake-manifest.json").is_file())
                    package.parent.mkdir(parents=True, exist_ok=True)
                    package.write_text("prepared dependency objects")
                    if fail:
                        raise RuntimeError("cache transport failed")
            except RuntimeError:
                if not fail:
                    raise
            self.assertEqual(sh(seed, "rev-parse", "HEAD"), self.content.tip)
            self.assertFalse((seed / "lake-manifest.json").exists())
            self.assertEqual(package.read_text(), "prepared dependency objects")

    def test_the_rebase_target_is_fetched_first(self):
        os.environ["STUB_LAKE_CACHE_FAIL_REVS"] = self.content.base  # the stale base has no outputs
        lines = self.observe_log()
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto], "onto's cache was found; the base was never tried")
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.onto)
        self.assertEqual(outcome["artifacts"]["cache_distance"], 0)
        self.assertEqual(outcome["artifacts"]["cache_skipped"], [])
        self.assertNotIn("cold", outcome["artifacts"])
        warm = [line for line in lines if "warm artifacts from" in line]
        self.assertEqual(warm, [f"task 1: warm artifacts from {self.onto[:12]} (the rebase target)"])
        self.assertEqual(self.service.releases, [])
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual([call for call in self.lake_calls() if call.startswith("build")], ["build --iofail"])
        self.assertFalse(outcome["artifacts"]["incremental_build"], "the old-base chain is not compiled before replay")
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("The worker owns final verification", prompt)
        self.assertIn("The worker owns final verification", prompt)
        self.assertIn("The worker checks the downstream closure of your edits", prompt)
        self.assertNotIn("every module of this batch that compiled are already built", prompt)
        self.assertNotIn("The build is cold", prompt)
        env = self.agent_calls()[0]["env"]
        self.assertEqual(env["TAUCETI_SHELL_LIMIT_SECONDS"], "7200", "the repair task's own shell limit")
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], str(min(7_200_000, int(env["BASH_MAX_TIMEOUT_MS"]))))
        self.assertIn("shell commands have a two-hour limit", prompt)

    def test_a_warm_pre_rebase_fetches_artifacts_without_building_the_old_chain(self):
        self.raw["kind"] = "pre_rebase"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.onto)
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertFalse(outcome["artifacts"]["incremental_build"])
        self.assertEqual([call for call in self.lake_calls() if call.startswith("build")], ["build --iofail"])
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertEqual(self.service.releases, [])
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("The worker owns final verification", prompt)
        self.assertIn("The worker checks the downstream closure of your edits", prompt)
        self.assertTrue(self.state()["report"]["ok"], "the resulting chain still goes through ordinary validation")

    def test_a_pre_rebase_cache_miss_still_runs_the_agent_cold(self):
        self.raw["kind"] = "pre_rebase"
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto, self.content.base])
        self.assertFalse(outcome["artifacts"]["available"])
        self.assertTrue(outcome["artifacts"]["cold"])
        self.assertEqual([call for call in self.lake_calls() if call.startswith("build")], [])
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertIn("The build is cold", self.agent_calls()[0]["prompt"])
        self.assertEqual(self.service.releases, [])

    def test_the_base_is_the_fallback_when_onto_is_not_published_yet(self):
        os.environ["STUB_LAKE_CACHE_FAIL_REVS"] = self.onto
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto, self.content.base])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.content.base)
        attempts = outcome["artifacts"]["cache_attempts"]
        self.assertEqual([a["mapping_found"] for a in attempts], [False, True])
        self.assertEqual(self.service.releases, [])

    def test_the_parent_of_an_unpublished_target_is_used_before_the_base(self):
        """Production repair 2 (2026-11-05): the rebase target's mapping had not been published yet, so the
        newest cached revision is its first parent, found before the stale base."""
        moved = self.advance_main()
        os.environ["STUB_LAKE_CACHE_FAIL_REVS"] = moved
        lines = self.observe_log()
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [moved, self.onto], "the parent answered; the base was never tried")
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.onto)
        self.assertEqual(outcome["artifacts"]["cache_distance"], 1)
        attempts = outcome["artifacts"]["cache_attempts"]
        self.assertEqual(
            [(a["revision"], a["distance"], a["mapping_found"]) for a in attempts],
            [(moved, 0, False), (self.onto, 1, True)],
        )
        self.assertEqual(outcome["artifacts"]["cache_skipped"], [f"no published cache mapping for {moved[:12]}"])
        warm = [line for line in lines if "warm artifacts from" in line]
        self.assertEqual(
            warm,
            [
                f"task 1: warm artifacts from {self.onto[:12]} (1 commit behind the rebase target {moved[:12]}); "
                f"skipped: no published cache mapping for {moved[:12]}"
            ],
        )
        self.assertNotIn("cold", outcome["artifacts"])
        self.assertIn("The worker owns final verification", self.agent_calls()[0]["prompt"])

    def test_the_walk_runs_newest_first_and_ends_at_a_fallback_off_it(self):
        moved = self.advance_main()
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [moved, self.onto, self.content.base], "newest first, down to the base")
        self.assertIsNone(outcome["artifacts"]["cache_revision"])
        self.assertEqual(
            outcome["artifacts"]["cache_skipped"],
            [
                f"no published cache mapping for {moved[:12]}",
                f"no published cache mapping for the 2 first-parent ancestors of {moved[:12]} "
                f"from {self.onto[:12]} back to {self.content.base[:12]}",
            ],
        )
        seed = self.content.bare.parent / "seed"
        self.assertEqual(
            repair._artifact_candidates(seed, moved, (self.content.base,), 1),
            [(moved, 0), (self.onto, 1), (self.content.base, None)],
            "a fallback older than the walk is tried last",
        )

    def test_an_earlier_miss_in_the_fetch_log_does_not_hide_a_published_mapping(self):
        """The production failure: ``run_logged`` appends, every claim of the task fetches into the same
        log, and the verdict read the whole file, so one "outputs not found" from an earlier claim made
        every later fetch of that revision look unpublished although it exited 0 and downloaded."""
        seed = self.content.bare.parent / "seed"
        logdir = self.root / "artifact-logs"
        env = dict(os.environ)
        first = repair.prepare_artifacts(seed, self.onto, env | {"STUB_LAKE_CACHE_FAIL_REVS": self.onto}, logdir, 60)
        self.assertIsNone(first["cache_revision"])
        second = repair.prepare_artifacts(seed, self.onto, env, logdir, 60)
        self.assertEqual(second["cache_revision"], self.onto, second["cache_attempts"])
        self.assertTrue(second["cache_attempts"][0]["mapping_found"])
        self.assertEqual(second["cache_attempts"][0]["artifacts_downloaded"], 1, "this run's output only")
        log = (logdir / f"cache-get-{self.onto[:12]}.log").read_text()
        self.assertEqual(log.count("outputs not found"), 1, "the earlier miss is still in the shared log")

    def test_without_any_artifacts_the_rebase_runs_cold(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto, self.content.base], "every candidate was tried")
        self.assertFalse(outcome["artifacts"]["available"])
        self.assertIsNone(outcome["artifacts"]["cache_revision"])
        self.assertTrue(outcome["artifacts"]["cold"])
        self.assertIn(self.onto[:12], outcome["artifacts"]["unavailable_reason"])
        self.assertIn(self.content.base[:12], outcome["artifacts"]["unavailable_reason"])
        self.assertEqual([c for c in self.lake_calls() if c.startswith("build")], [], "no incremental build")
        self.assertEqual(len(self.agent_calls()), 1, "the agent ran without warm artifacts")
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("The build is cold", prompt)
        self.assertNotIn("Artifacts are warm", prompt)
        self.assertIn(self.onto[:12], prompt, "the prompt says why the build is cold")
        self.assertIn("`lake build TauCeti.Two`", prompt, "the conflicting module is the suggested first build")
        self.assertEqual(self.service.releases, [], "a rebase without artifacts is not released")
        history = [h["state"] for h in self.state()["history"]]
        self.assertIn("artifacts_unavailable", history)
        self.assertGreater(history.index("agent_running"), history.index("artifacts_unavailable"))
        self.assertEqual(self.state()["state"], "adopted")

    def test_a_repair_without_artifacts_is_still_released(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        self.service.tasks = {1: self.content.task()}
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertEqual(self.agent_calls(), [])


class ToolchainBumpWithoutArtifacts(Fixture):
    """A chain that changes `lean-toolchain` (a Lean or Mathlib bump): Lake scopes the published
    mappings by the checkout's toolchain, so main's revisions never answer for it. Production repair
    task 1273 (2026-11-19) was released as ``artifacts_unavailable`` on every claim until escalation."""

    BASE_FILES = {"lean-toolchain": "leanprover/lean4:v4.36.0-rc2\n"}
    MEMBER_FILES = {1: {"lean-toolchain": "leanprover/lean4:v4.36.0\n"}}

    def test_the_repair_runs_cold_instead_of_being_released(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.service.releases, [], "a toolchain bump without artifacts is not released")
        self.assertFalse(outcome["artifacts"]["available"])
        self.assertTrue(outcome["artifacts"]["cold"])
        self.assertTrue(self.state()["artifacts"]["cold"], "a resumed claim still knows the build is cold")
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertIn("The build is cold", self.agent_calls()[0]["prompt"])
        self.assertNotIn("stream_build", outcome, "nothing to stream from without artifacts")

    def test_parallel_agents_are_not_dispatched_cold(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config(parallel_agents=3)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertNotIn("parallel", outcome)

    def test_a_preparation_is_still_released(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        raw = self.content.task(kind="prepare")
        raw["diagnostics"] = []
        self.service.tasks = {1: raw}
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual(os.environ.get(repair_sandbox.BACKGROUND_VARIABLE), "1", "its builds run in the background")

    def test_background_tasks(self):
        def task(kind, diagnostics=()):
            raw = {**self.content.task(kind=kind), "claim_token": "t", "expires_at": time.time() + 60}
            raw["diagnostics"] = list(diagnostics)
            return repair_task.Task.from_json(raw)

        prediction = {"kind": "admission_predictions", "proactive": True}
        converted = {"kind": "admission_predictions", "proactive": False, "was_proactive": True}
        self.assertTrue(repair_task.background_task(task("prepare")))
        self.assertTrue(repair_task.background_task(task("pre_rebase")))
        self.assertFalse(repair_task.background_task(task("repair", [prediction])))
        self.assertFalse(repair_task.background_task(task("repair", [converted])))
        self.assertFalse(repair_task.background_task(task("repair")))
        self.assertFalse(repair_task.background_task(task("rebase_conflict")))

    def test_the_toolchain_change_is_read_from_the_chain(self):
        clone = self.content.bare.parent / "seed"
        self.assertTrue(repair_task.toolchain_changed(clone, self.content.base, self.content.tip))
        self.assertFalse(repair_task.toolchain_changed(clone, self.content.base, self.content.base))
        self.assertFalse(repair_task.toolchain_changed(clone, self.content.members[1]["commit"], self.content.tip))


class InferenceUnavailable(Fixture):
    def test_bounded_retries_then_release(self):
        os.environ["STUB_AGENT_MODE"] = "529"
        cfg = self.config(inference_attempts=3)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "inference_unavailable")
        self.assertIn("529", outcome["detail"])
        self.assertEqual(len(self.agent_calls()), 3)
        self.assertEqual([r["reason"] for r in self.service.releases], ["inference_unavailable"])
        self.assertEqual(self.service.results, [])
        attempts = outcome["agent"][0]["attempts"]
        self.assertTrue(all(a["infrastructure_failure"] == "provider returned 529" for a in attempts))

    def test_an_ordinary_agent_failure_is_not_inference_unavailability(self):
        os.environ["STUB_AGENT_MODE"] = "noop"  # exits 0 without repairing anything
        cfg = self.config(agent_attempts=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "agent_failed")
        self.assertIn("nothing was repaired", outcome["detail"])
        self.assertEqual(len(self.agent_calls()), 2, "one bounded retry with the local report")
        self.assertEqual(self.service.results, [])


class ScopeOnlyRepair(Fixture):
    BROKEN = ()

    def scope_failure(self):
        self.service.tasks[1]["diagnostics"] = [
            {"kind": "status", "context": "scope", "message": "scope approval unavailable"},
            {"kind": "admission_predictions", "complete": True, "records": []},
        ]
        os.environ["STUB_AGENT_MODE"] = "noop"

    def test_unchanged_scope_only_chain_is_verified_and_submitted_for_fresh_ci(self):
        self.scope_failure()
        cfg = self.config(agent_attempts=1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(outcome["tip"], self.content.tip)
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(json.loads(self.service.results[0]["worker_notes"])["edited"], [])
        self.assertEqual(len(outcome["verification"]), 1)
        self.assertTrue(outcome["verification"][0]["ok"])
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("This is not a failed Lean build", prompt)
        self.assertIn("fresh normal CI; no check is waived", prompt)

    def test_local_verification_failure_still_prevents_submission(self):
        self.scope_failure()
        cfg = self.config(agent_attempts=1, verify_retries=0)
        result = {
            "ok": False,
            "diagnostics": [{"kind": "compiler", "message": "broken"}],
            "checks": {"status": "failed"},
        }
        with patch.object(repair_audit, "verify", return_value=result):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["reason"], "agent_failed", outcome)
        self.assertEqual(self.service.results, [])

    def test_other_findings_and_converted_predictions_keep_the_changed_result_rule(self):
        self.scope_failure()
        record = self.service.task_record(1, self.service.preclaim(1, "token", "worker"))
        task = repair_task.Task.from_json(record)
        self.assertTrue(repair_task.scope_only_repair(task))
        for finding in [
            {"kind": "compiler"},
            {"kind": "status", "context": "build"},
            {"kind": "status", "context": "bump-guard"},
            None,
            {"kind": "admission_predictions", "was_proactive": True},
        ]:
            original = list(task.diagnostics)
            task.diagnostics.append(finding)
            self.assertFalse(repair_task.scope_only_repair(task))
            task.diagnostics[:] = original
        task.diagnostics[:] = [{"kind": "admission_predictions"}]
        self.assertFalse(repair_task.scope_only_repair(task))


class PredictedRepair(Fixture):
    BROKEN = ()

    def prediction(self):
        knowledge = {
            "kind": "admission_predictions",
            "proactive": True,
            "complete": True,
            "records": [{"kind": "path_overlap", "path": "TauCeti/One.lean", "members": [1, 2]}],
        }
        self.service.tasks[1]["diagnostics"] = [knowledge]
        return knowledge

    def test_noop_proactive_repair_is_verified_and_submitted_without_a_forced_edit(self):
        self.prediction()
        os.environ["STUB_AGENT_MODE"] = "noop"
        cfg = self.config(agent_attempts=1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(outcome["tip"], self.content.tip)
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(json.loads(self.service.results[0]["worker_notes"])["edited"], [])
        self.assertEqual(len(outcome["verification"]), 1)
        self.assertTrue(outcome["verification"][0]["ok"])
        self.assertIn("Admission-time knowledge", self.agent_calls()[0]["prompt"])

    def test_admission_navigation_survives_replacement_of_build_errors(self):
        knowledge = self.prediction()
        task = repair_task.Task.from_json(self.service.task_record(1, self.service.preclaim(1, "token", "worker")))
        findings = repair.empty_findings()
        context = repair_task.prompt_context(task, "next verification round", findings)
        self.assertEqual(findings["admission_predictions"], knowledge)
        self.assertEqual(context.kind, "repair")

    def test_prediction_verification_requires_the_exact_tip_and_actual_audits(self):
        self.prediction()
        task = repair_task.Task.from_json(self.service.task_record(1, self.service.preclaim(1, "token", "worker")))
        outcome = {
            "observed_duplicate_names": ["Foo.bar"],
            "verification": [{"tip": task.tip, "ok": True, "checks": {"status": "passed"}}],
        }
        note = repair_task.admission_verification(task, task.tip, outcome, [])
        self.assertTrue(note["checks_passed"])
        self.assertEqual(note["observed_duplicate_names"], ["Foo.bar"])
        self.assertFalse(repair_task.admission_verification(task, "0" * 40, outcome, [])["checks_passed"])
        outcome["verification"][0]["checks"]["status"] = "unavailable"
        self.assertFalse(repair_task.admission_verification(task, task.tip, outcome, [])["checks_passed"])


class LongPrompt(Fixture):
    def test_a_prompt_too_long_for_argv_is_delivered_on_stdin(self):
        # Linux rejects a single argv string above 128 KiB (MAX_ARG_STRLEN); a 1,197-member head built a
        # prompt that size in production and Popen failed with "Argument list too long". Past the
        # worker's 64 KiB limit the prompt leaves argv and is the whole of the agent's stdin.
        from tauceti_worker.agents import PROMPT_ARGV_LIMIT

        with self.padded_prompts(PROMPT_ARGV_LIMIT + 1024):
            cfg = self.config()
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["prompt_via"], "stdin")
        self.assertGreater(calls[0]["prompt_chars"], PROMPT_ARGV_LIMIT)
        self.assertTrue(calls[0]["prompt"].endswith("x" * 99 + "\n"), "the whole prompt arrived, in order")
        self.assertIn(repair.AGENT_INSTRUCTION, calls[0]["prompt"])
        self.assertEqual(calls[0]["argv"][:3], ["-p", "--output-format", "stream-json"])
        self.assertEqual(len(self.service.results), 1)

    def test_a_short_prompt_stays_in_argv(self):
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual([c["prompt_via"] for c in calls], ["argv"])
        self.assertEqual(calls[0]["argv"][:2], ["-p", "--output-format"])


class AgentLaunchFailed(Fixture):
    """The agent process could not be created: not the cache's fault, so not ``artifacts_unavailable``."""

    def test_a_process_that_cannot_start_is_released_under_its_own_reason(self):
        with self.prompt_too_long_for_argv():
            cfg = self.config()
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "agent_launch_failed")
        self.assertIn("could not start", outcome["detail"])
        self.assertIn("Argument list too long", outcome["detail"], "the record names the real cause")
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual([r["reason"] for r in self.service.releases], ["agent_launch_failed"])
        self.assertIn("could not start", self.service.releases[0]["error"])
        self.assertEqual(self.service.results, [])
        self.assertEqual(self.state()["state"], "released")
        history = [h["state"] for h in self.state()["history"]]
        self.assertIn("artifacts_ready", history, "the artifacts were prepared; the launch is what failed")
        self.assertIsNone(self.service.live_claim(1), "the service freed the claim")


class Providers(Fixture):
    """The provider chain: order, the pacer's eligibility check, fall-through on a failure before any
    work, the api provider's environment, and the attempts recorded in the outcome and the heartbeat."""

    def provider_attempts(self, outcome: dict) -> list[tuple[str, str]]:
        return [(a["provider"], a["status"]) for a in repair_task.provider_attempts_of(outcome)]

    def test_the_chain_order_is_honoured(self):
        cfg = self.config(providers=("codex", "claude", "api"), api_key="sk-test-key")
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual([c["agent"] for c in calls], ["codex"])
        self.assertEqual(calls[0]["argv"][:4], ["exec", "--json", "--model", repair.codex_profile().model])
        self.assertEqual(calls[0]["prompt_via"], "argv")
        self.assertIn(repair.AGENT_INSTRUCTION, calls[0]["prompt"])
        env = calls[0]["env"]
        self.assertTrue(env["CODEX_HOME"].startswith(str(self.root / "state")), "an isolated CODEX_HOME")
        self.assertIsNone(env["ANTHROPIC_API_KEY"], "a subscription run never sees the api key")
        self.assertIsNone(env["OPENAI_API_KEY"])
        self.assertIsNone(env["TAUCETI_CLAUDE_BILLING"])
        self.assertEqual(env["TAUCETI_SHELL_LIMIT_SECONDS"], str(repair.SHELL_LIMIT_SECONDS), "the same two-hour limit")
        self.assertEqual(self.provider_attempts(outcome), [("codex", "ran")])
        self.assertEqual(outcome["agent"][0]["provider"], "codex")
        self.assertEqual(outcome["agent"][0]["attempts"][0]["provider"], "codex")
        self.assertEqual(outcome["agent"][0]["attempts"][0]["telemetry"]["provider"], "codex")
        self.assertEqual(self.state()["provider"], "codex")

    def test_codex_runs_a_repair_end_to_end(self):
        # The scripted agent through the Codex argv: the same prompt (no CLI-specific tool names), the
        # task's own check script run from it, the transcript rendered to the same agent log shape, and
        # the usage priced from the telemetry the renderer writes.
        cfg = self.config(providers=("codex",))
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 1)
        call = self.agent_calls()[0]
        self.assertEqual(call["agent"], "codex")
        profile = repair.codex_profile()
        self.assertEqual(
            call["argv"],
            [
                "exec",
                "--json",
                "--model",
                profile.model,
                "-c",
                f'model_reasoning_effort="{profile.effort}"',
                "-c",
                'model_reasoning_summary="detailed"',
                "-c",
                "show_raw_agent_reasoning=false",
                "--sandbox",
                "danger-full-access",
                "--skip-git-repo-check",
            ],
        )
        for word in ("Bash tool", "Read tool", "Edit tool", "Claude", "claude"):
            self.assertNotIn(word, call["prompt"], f"the prompt is provider-neutral; found {word!r}")
        self.assertIn(".tauceti-repair/check", call["prompt"])
        self.assertIn("two-hour limit", call["prompt"], "the shell limit reaches Codex through the prompt")
        logs = cfg.tasks_dir / "1" / "logs"
        rendered = sorted(logs.glob("agent-codex-*.log"))
        self.assertEqual(len(rendered), 1, [p.name for p in logs.iterdir()])
        text = rendered[0].read_text()
        self.assertIn("[session] codex thread-1", text)
        self.assertIn("[turn] started", text)
        self.assertIn("[assistant]\ndone; check: ok (exit 0)", text)
        self.assertIn("[turn] completed; input=10, cached_input=2, output=5", text)
        self.assertNotIn("[warning] no structured codex events", text)
        telemetry = json.loads((logs / "agent-codex-telemetry.json").read_text())
        self.assertEqual(telemetry["provider"], "codex")
        self.assertEqual(telemetry["usage"], {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5})
        attempt = outcome["agent"][0]["attempts"][0]
        self.assertEqual(attempt["telemetry"]["usage"], telemetry["usage"])
        self.assertEqual(attempt["session"], "thread-1")
        self.assertIsInstance(attempt["estimated_cost_usd"], float, "Sol usage is priced from the repository table")
        self.assertGreater(attempt["estimated_cost_usd"], 0)
        notes = json.loads(self.service.results[0]["worker_notes"])
        self.assertEqual(notes["estimated_cost_usd"], attempt["estimated_cost_usd"])
        self.assertEqual(json.loads((cfg.tasks_dir / "1" / "session.json").read_text())["profile"]["provider"], "codex")

    def test_a_long_prompt_reaches_codex_on_stdin(self):
        from tauceti_worker.agents import PROMPT_ARGV_LIMIT

        cfg = self.config(providers=("codex",))
        with self.padded_prompts(PROMPT_ARGV_LIMIT + 1024):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual([c["agent"] for c in calls], ["codex"])
        self.assertEqual(calls[0]["prompt_via"], "stdin")
        self.assertEqual(calls[0]["argv"][-2:], ["--skip-git-repo-check", "-"])
        self.assertTrue(calls[0]["prompt"].endswith("x" * 99 + "\n"), "the whole prompt arrived, in order")

    def test_a_rate_limited_subscription_is_skipped(self):
        self.statuses["claude"] = (False, "session ahead of pace (70% elapsed: used 61% > 55% pace budget), 39% left")
        cfg = self.config(providers=("claude", "codex"))
        with patch.object(repair_task, "log") as logged:
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual([c["agent"] for c in self.agent_calls()], ["codex"])
        attempts = repair_task.provider_attempts_of(outcome)
        self.assertEqual([(a["provider"], a["status"]) for a in attempts], [("claude", "skipped"), ("codex", "ran")])
        self.assertIn("ahead of pace", attempts[0]["reason"])
        self.assertEqual(attempts[0]["seconds"], 0.0)
        self.assertTrue(
            any("provider claude skipped: session ahead of pace" in str(c.args[0]) for c in logged.call_args_list),
            [c.args[0] for c in logged.call_args_list],
        )

    def test_an_unmeasurable_subscription_is_skipped_and_an_empty_chain_releases(self):
        self.statuses["claude"] = (False, "no /home/x/.claude/.credentials.json")
        self.statuses["codex"] = (False, "codex usage HTTP 503")
        cfg = self.config(providers=("claude", "codex"))
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "inference_unavailable")
        self.assertIn("no eligible provider", outcome["detail"])
        self.assertIn("codex usage HTTP 503", outcome["detail"])
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual(self.provider_attempts(outcome), [("claude", "skipped"), ("codex", "skipped")])

    def test_a_launch_failure_falls_through_to_the_next_provider(self):
        # Popen searches every PATH entry and skips one whose exec fails with ENOENT, so the broken stub
        # must be the only `codex` on PATH or the host's real one would run.
        self.break_launch("codex")
        path = [d for d in os.environ["PATH"].split(":") if d == str(BIN) or not (Path(d) / "codex").exists()]
        cfg = self.config(providers=("codex", "claude"))
        with patch.dict(os.environ, {"PATH": ":".join(path)}):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual([c["agent"] for c in self.agent_calls()], ["claude"])
        attempts = repair_task.provider_attempts_of(outcome)
        self.assertEqual([(a["provider"], a["status"]) for a in attempts], [("codex", "failed"), ("claude", "ran")])
        self.assertIn("could not start", attempts[0]["reason"])
        self.assertEqual(outcome["agent"][0]["provider"], "claude")
        self.assertEqual(len(self.service.releases), 0)

    def test_a_refusal_before_any_work_falls_through_at_once(self):
        # Codex refuses with its usage-limit error; Claude's stream reports a rejected rate limit; the api
        # provider does the work. No inference delay is spent between providers.
        os.environ["STUB_AGENT_MODES"] = "usage-limit,usage-limit,repair"
        cfg = self.config(providers=("codex", "claude", "api"), api_key="sk-test-key", inference_delays=(30.0,))
        started = time.monotonic()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual([c["agent"] for c in calls], ["codex", "claude", "claude"])
        attempts = repair_task.provider_attempts_of(outcome)
        self.assertEqual(
            [(a["provider"], a["status"]) for a in attempts],
            [("codex", "failed"), ("claude", "failed"), ("api", "ran")],
        )
        self.assertIn("usage_limit_reached", attempts[0]["reason"])
        self.assertEqual(attempts[1]["reason"], "rate limit status rejected")
        self.assertTrue(all(a["seconds"] >= 0 for a in attempts))
        self.assertEqual(outcome["agent"][0]["provider"], "api")
        # The refused runs are in the attempt records too, with no session to resume.
        self.assertEqual([a["provider"] for a in outcome["agent"][0]["attempts"]], ["codex", "claude", "api"])
        self.assertIsNone(outcome["agent"][0]["attempts"][0]["session"])

    def test_an_authentication_error_falls_through(self):
        os.environ["STUB_AGENT_MODES"] = "auth-error,repair"
        cfg = self.config(providers=("api", "claude"), api_key="sk-bad-key")
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        attempts = repair_task.provider_attempts_of(outcome)
        self.assertEqual([(a["provider"], a["status"]) for a in attempts], [("api", "failed"), ("claude", "ran")])
        self.assertIn("API Error: 401", attempts[0]["reason"])
        self.assertNotIn("sk-bad-key", json.dumps(outcome))
        self.assertNotIn("sk-bad-key", json.dumps(self.state()))

    def test_a_provider_that_failed_after_working_is_not_replaced(self):
        os.environ["STUB_AGENT_MODE"] = "work-then-fail"
        cfg = self.config(providers=("codex", "claude"), agent_attempts=1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "agent_failed")
        self.assertEqual([c["agent"] for c in self.agent_calls()], ["codex"])
        self.assertEqual(self.provider_attempts(outcome), [("codex", "ran")])

    def test_a_whole_chain_refused_is_retried_then_released(self):
        os.environ["STUB_AGENT_MODE"] = "usage-limit"
        cfg = self.config(providers=("codex", "claude"), inference_attempts=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "inference_unavailable")
        self.assertEqual([c["agent"] for c in self.agent_calls()], ["codex", "claude", "codex", "claude"])
        self.assertEqual(self.provider_attempts(outcome), [("codex", "failed"), ("claude", "failed")] * 2)
        self.assertEqual([r["reason"] for r in self.service.releases], ["inference_unavailable"])

    def test_the_api_provider_sets_the_key_and_drops_the_subscription_credentials(self):
        claude_dir = HOME / ".claude"
        claude_dir.mkdir(exist_ok=True)
        credentials = claude_dir / ".credentials.json"
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sub-token"}}))
        self.addCleanup(credentials.unlink)
        cfg = self.config(providers=("api",), api_key="sk-test-key")
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual([c["agent"] for c in calls], ["claude"])
        env = calls[0]["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-test-key")
        self.assertEqual(env["TAUCETI_CLAUDE_BILLING"], "api")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].endswith("claude-config-api"))
        self.assertFalse(calls[0]["claude_credentials"], "no subscription credential beside the key")
        self.assertNotIn("sk-test-key", json.dumps(outcome))
        self.assertNotIn("sk-test-key", json.dumps(self.state()))
        # The subscription provider, on the same host, gets the credential and no key.
        self.service = FakeService(self.content.bare, [self.content.task(2)])
        self.addCleanup(self.service.close)
        cfg = self.config(providers=("claude",), api_key="sk-test-key")
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        env = self.agent_calls()[1]["env"]
        self.assertIsNone(env["ANTHROPIC_API_KEY"])
        self.assertIsNone(env["TAUCETI_CLAUDE_BILLING"])
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].endswith("claude-config"))
        self.assertTrue(self.agent_calls()[1]["claude_credentials"])

    def test_the_codex_credential_is_copied_into_the_isolated_home(self):
        codex_dir = HOME / ".codex"
        codex_dir.mkdir(exist_ok=True)
        auth = codex_dir / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "codex-token", "account_id": "acct"}}))
        self.addCleanup(auth.unlink)
        cfg = self.config(providers=("codex",))
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        call = self.agent_calls()[0]
        self.assertTrue(call["codex_credentials"])
        self.assertEqual(call["env"]["CODEX_HOME"], str(self.root / "state" / "codex-home"))

    def test_the_heartbeat_counts_the_providers(self):
        self.statuses["claude"] = (False, "weekly exhausted")
        self.break_launch("codex")
        path = [d for d in os.environ["PATH"].split(":") if d == str(BIN) or not (Path(d) / "codex").exists()]
        cfg = self.config(providers=("claude", "codex", "api"), api_key="sk-test-key", poll_seconds=0.2)
        stop = repair_task.Stop()
        result: dict = {}

        def run():
            result["summary"] = repair_task.serve(cfg, self.client(cfg), stop)

        with patch.dict(os.environ, {"PATH": ":".join(path)}):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.addCleanup(stop.set, "cleanup")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and len(self.service.results) < 1:
                time.sleep(0.05)
            stop.set("test")
            thread.join(30)
        self.assertFalse(thread.is_alive())
        heartbeat = json.loads(repair_task.heartbeat_path(cfg).read_text())
        self.assertEqual(heartbeat["outcomes"], {"adopted": 1})
        self.assertEqual(
            heartbeat["providers"],
            {
                "claude": {"ran": 0, "skipped": 1, "failed": 0},
                "codex": {"ran": 0, "skipped": 0, "failed": 1},
                "api": {"ran": 1, "skipped": 0, "failed": 0},
            },
        )
        self.assertEqual(heartbeat["last_outcome"]["provider"], "api")
        attempts = heartbeat["last_outcome"]["provider_attempts"]
        self.assertEqual(
            [(a["provider"], a["status"]) for a in attempts],
            [("claude", "skipped"), ("codex", "failed"), ("api", "ran")],
        )
        self.assertEqual(attempts[0]["reason"], "weekly exhausted")
        self.assertIn("could not start", attempts[1]["reason"])
        self.assertNotIn("sk-test-key", repair_task.heartbeat_path(cfg).read_text())

    def test_the_status_is_the_pacer_s(self):
        from tauceti_worker import quota

        seen: dict = {}

        def claude(self_, *, refresh=False, renew=False):
            seen["pace"] = os.environ.get("TAUCETI_PACE")
            seen["own"] = os.environ.get("TAUCETI_PACE_CLAUDE")
            seen["other"] = os.environ.get("TAUCETI_PACE_CODEX")
            seen["renew"] = renew
            window = quota.Window("session", 61.0, 70.0, time.time() + 3600, quota.STATUS_OVER_PACE, 55.0)
            return quota.Provider("claude", False, None, [window])

        def codex(self_, *, refresh=False):
            return quota.Provider(
                "codex", True, "gpt-5", [quota.Window("weekly", 10.0, 20.0, None, "under-pace", 30.0)]
            )

        cfg = self.config(rate="0:10,50:70")
        with patch.object(repair_task, "subscription_status", self._real_subscription_status):
            with (
                patch.object(quota.Quota, "claude", claude),
                patch.object(quota.Quota, "codex", codex),
                patch.dict(os.environ, {"TAUCETI_PACE_CLAUDE": "0:100", "TAUCETI_PACE_CODEX": "0:90"}),
            ):
                self.assertEqual(
                    repair_task.subscription_status(cfg, "claude"),
                    (False, "session ahead of pace (70% elapsed: used 61% > 55% pace budget), 39% left"),
                )
                self.assertEqual(os.environ.get("TAUCETI_PACE_CLAUDE"), "0:100")
                self.assertEqual(os.environ.get("TAUCETI_PACE_CODEX"), "0:90")
                self.assertEqual(
                    repair_task.subscription_status(cfg, "codex"), (True, "under pace (weekly 10% used of 30% budget)")
                )
            self.assertEqual(
                seen,
                {"pace": "0:10,50:70", "own": None, "other": None, "renew": False},
                "the task's rate is the pacer's only curve; no token rotation",
            )
            self.assertIsNone(os.environ.get("TAUCETI_PACE"))
            # No credential at all: unmeasurable, not eligible, and the reason names the file.
            ok, reason = repair_task.subscription_status(cfg, "claude")
            self.assertFalse(ok)
            self.assertIn(".credentials.json", reason)

    def test_pacing_environment_is_restored_when_usage_read_fails(self):
        from tauceti_worker import quota

        original = {
            "TAUCETI_PACE": "0:80",
            "TAUCETI_PACE_CLAUDE": "0:90",
            "TAUCETI_PACE_CODEX": "0:100",
            "CLAUDE_CONFIG_DIR": "/credentials/claude",
            "CODEX_HOME": "/credentials/codex",
        }

        def fail(self_, **kwargs):
            self.assertEqual(os.environ.get("TAUCETI_PACE"), "60:40")
            for key in ("TAUCETI_PACE_CLAUDE", "TAUCETI_PACE_CODEX"):
                self.assertNotIn(key, os.environ)
            for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
                self.assertEqual(os.environ[key], original[key])
            self.assertFalse(kwargs.get("renew", False))
            raise RuntimeError("usage unavailable")

        with (
            patch.dict(os.environ, original),
            patch.object(repair_task, "subscription_status", self._real_subscription_status),
            patch.object(quota.Quota, "claude", fail),
            patch.object(quota.Quota, "codex", fail),
        ):
            for provider in ("claude", "codex"):
                ok, reason = repair_task.subscription_status(self.config(rate="60:40"), provider)
                self.assertFalse(ok)
                self.assertIn("usage unavailable", reason)
                self.assertEqual({key: os.environ.get(key) for key in original}, original)

    def test_the_chain_spec(self):
        self.assertEqual(repair_task.parse_providers(None), ("claude", "codex", "api"))
        self.assertEqual(repair_task.parse_providers(" Codex, api "), ("codex", "api"))
        for bad in ("claude,kiro", "claude,claude", ","):
            with self.assertRaises(ValueError):
                repair_task.parse_providers(bad)
        self.assertEqual(repair.codex_profile().model, "gpt-6.1-sol")
        self.assertEqual(repair.claude_profile().model, "claude-opus-5-5")


class QueueCacheNote(Fixture):
    """The queue warms the tree itself: the fleet harness's cache-fetch appendix never reaches its agents."""

    BASE_FILES = {"lakefile.toml": 'name = "TauCeti"\n'}  # the appendix is added only in a Lake checkout

    def test_a_codex_pre_rebase_gets_no_cache_fetch_appendix_and_no_cache_miss_flag(self):
        """Batch 3's pre-rebase (2026-11-05): the fleet harness appended its cache-fetch paragraph, the
        host preparation set ``TAUCETI_PR_CACHE_MISS=1`` because a queue head's tip is never in the public
        cache, and Codex followed the appendix's stop rule and aborted the replay."""
        from tauceti_worker import agents

        def host_preparation(*args, **kwargs):
            raise AssertionError("the queue prepares its own tree: no host cache preparation or relinking")

        raw = self.content.rebase_task()
        raw["kind"] = "pre_rebase"
        self.service.tasks = {1: raw}
        cfg = self.config(providers=("codex",))
        with (
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(agents, "host_build_cache_context", host_preparation),
            patch.object(agents, "relink_build_outputs", host_preparation),
            patch.dict(os.environ, {"TAUCETI_PR_CACHE_MISS": "1"}),
        ):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        call = self.agent_calls()[0]
        self.assertEqual(call["agent"], "codex")
        self.assertNotIn("Build cache preparation", call["prompt"])
        self.assertNotIn("TAUCETI_PR_CACHE_MISS", call["prompt"])
        self.assertNotIn("report the cache miss and stop", call["prompt"])
        self.assertNotIn("(the worker already tried)", call["prompt"])
        self.assertIsNone(call["env"]["TAUCETI_PR_CACHE_MISS"])
        self.assertIn(repair.CACHE_SENTENCE, call["prompt"])
        self.assertIn("that miss is expected and is not a reason to stop", call["prompt"])
        # The fleet's own runs keep the appendix.
        captured: list[str] = []

        def launch(prompt, profile):
            captured.append(prompt)
            return ["true"], {}, None

        with (
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(agents, "host_agent_launch", launch),
            patch.dict(os.environ, {"TAUCETI_AGENT_ECHO": "1"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            agents.run_agent_host(self.content.clone, "fleet", repair.codex_profile(), self.root / "fleet-logs")
            agents.run_agent_host(
                self.content.clone, "queue", repair.codex_profile(), self.root / "fleet-logs", cache_note=False
            )
        self.assertIn("Build cache preparation", captured[0])
        self.assertEqual(captured[1], "queue")


class AgentTimeout(Fixture):
    def test_the_worker_cap_is_an_agent_failure_not_a_lost_claim(self):
        os.environ["STUB_AGENT_MODE"] = "sleep"
        os.environ["STUB_SLEEP"] = "30"
        cfg = self.config(agent_timeout=2, agent_attempts=1)
        started = time.monotonic()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "agent_failed")
        self.assertIn("cap", outcome["detail"])
        self.assertFalse(outcome["lease"]["lost"], "the claim itself was never lost")
        self.assertEqual([r["reason"] for r in self.service.releases], ["agent_failed"])


class Abandon(Fixture):
    def test_a_task_left_mid_agent_is_abandoned_and_its_claim_released(self):
        cfg = self.config()
        client = self.client(cfg)
        # A previous runner claimed the task, prepared the worktree and was running the agent in sess-old.
        claim = self.service.preclaim(1, "old-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        repair_task.fetch_retained(cfg.clone, task)
        worktree = cfg.root / "worktrees" / "task-1"
        repair.checkout_worktree(cfg.clone, task.tip, worktree)
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("claimed")
        state.write("artifacts_ready")
        state.write("agent_running", agent_attempt=1)
        from tauceti_worker.runtime_status import atomic_json

        atomic_json(state.session_path, {"session": "sess-old", "cwd": str(worktree)})
        outcomes = repair_task.retry_deliveries(cfg, client)
        self.assertEqual([(o["status"], o["reason"]) for o in outcomes], [("released", "worker-stopped")])
        self.assertEqual(self.agent_calls(), [], "nothing of the attempt is continued")
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])
        self.assertIsNone(self.service.live_claim(1))
        self.assertFalse(worktree.exists())
        outcome = repair_task.claim_and_run(cfg, client)
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.agent_calls()[0]["resume"], None, "a fresh run, not a resume")

    def test_a_record_whose_claim_is_gone_is_not_continued(self):
        cfg = self.config()
        task = repair_task.Task.from_json(
            {**self.content.task(), "claim_token": "gone", "expires_at": time.time() + 5, "renew_after": 0.2}
        )
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("agent_running")
        self.assertEqual(repair_task.retry_deliveries(cfg, self.client(cfg)), [])
        self.assertEqual(self.state()["state"], "claim_lost")
        self.assertEqual(self.agent_calls(), [])


class Serve(Fixture):
    """The long-running worker: ``serve`` in a thread (or, for the signal test, in the main thread)."""

    def serve_in_thread(self, cfg: repair_task.RunnerConfig, stop: repair_task.Stop) -> tuple[threading.Thread, dict]:
        result: dict = {}

        def run():
            result["summary"] = repair_task.serve(cfg, self.client(cfg), stop)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(stop.set, "cleanup")
        return thread, result

    def wait_for(self, predicate, message: str, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    def heartbeat(self, cfg: repair_task.RunnerConfig) -> dict:
        return json.loads(repair_task.heartbeat_path(cfg).read_text())

    def test_claim_run_submit_then_idle_until_stopped(self):
        cfg = self.config(poll_seconds=0.2)
        idle = threading.Event()
        service = self.service

        class IdleStop(repair_task.Stop):
            def wait(self, timeout=None):
                if timeout == cfg.poll_seconds and len(service.results) == 1 and len(service.claim_attempts) >= 3:
                    # Hold the observed idle interval until the test stops the worker. A claim
                    # being recorded alone does not mean its request has returned to idle yet.
                    idle.set()
                    return super().wait()
                return super().wait(timeout)

        stop = IdleStop()
        thread, result = self.serve_in_thread(cfg, stop)
        self.wait_for(
            idle.is_set,
            "the task was adopted and the loop went on polling for the next one",
        )
        self.assertEqual(self.heartbeat(cfg)["state"], "idle")
        stop.set("test")
        thread.join(30)
        self.assertFalse(thread.is_alive(), "stop ends the loop")
        summary = result["summary"]
        self.assertEqual(summary["tasks"], 1)
        self.assertEqual(summary["outcomes"], {"adopted": 1})
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(self.state()["state"], "adopted")
        # The provider answered, so the service hears a success (which lifts any inference hold).
        self.assertEqual(self.service.inference_reports, [{"worker": "worker-a@test", "ok": True, "error": ""}])
        heartbeat = self.heartbeat(cfg)
        self.assertEqual(heartbeat["state"], "stopped")
        self.assertEqual(heartbeat["pid"], os.getpid())
        self.assertEqual(heartbeat["stop_reason"], "test")
        self.assertEqual(heartbeat["last_outcome"]["status"], "adopted")

    def test_a_failing_service_and_a_failing_task_do_not_end_the_loop(self):
        # Task 1's base is unknown to every repository, so its run raises before any inference;
        # task 2 is the ordinary fixture.
        broken = {**self.content.task(task_id=1), "base": "0" * 40, "onto": "0" * 40, "retained_refs": []}
        self.service.tasks = {1: broken, 2: self.content.task(task_id=2)}
        self.service.claim_mode = "error"
        cfg = self.config(poll_seconds=0.1)
        stop = repair_task.Stop()
        thread, result = self.serve_in_thread(cfg, stop)
        self.wait_for(
            lambda: sum(1 for a in self.service.claim_attempts if a.get("error")) >= 4,
            "claims keep being retried while the service answers 500",
        )
        self.assertTrue(thread.is_alive())
        self.service.claim_mode = "ok"
        # Task 1's error gives its claim back at once (so the service re-exposes it); the fake offers
        # it first again, so once released it is withdrawn here to let task 2 be reached.
        self.wait_for(lambda: len(self.service.releases) >= 1, "task 1's error released its claim")
        with self.service.lock:
            del self.service.tasks[1]
        self.wait_for(lambda: len(self.service.results) == 1, "task 2 was adopted after task 1 failed")
        stop.set("test")
        thread.join(30)
        self.assertFalse(thread.is_alive())
        summary = result["summary"]
        self.assertGreaterEqual(summary["errors"], 2, summary)
        self.assertEqual(summary["outcomes"], {"adopted": 1})
        self.assertEqual(self.service.results[0]["task_id"], 2)
        self.assertEqual(self.service.releases[0]["reason"], "artifacts_unavailable")
        self.assertEqual(self.state("1")["state"], "released")
        self.assertIn("SourcesUnavailable", self.state("1")["error"])
        self.assertEqual(self.state("2")["state"], "adopted")
        self.assertEqual(self.heartbeat(cfg)["last_error"]["what"], "task")

    def test_a_repair_released_for_artifacts_waits_with_backoff_before_the_next_claim(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config(poll_seconds=0.05, artifacts_backoff=(0.4, 0.6))
        stop = repair_task.Stop()
        thread, result = self.serve_in_thread(cfg, stop)
        self.wait_for(lambda: len(self.service.releases) >= 4, "the task keeps being released and re-claimed")
        stop.set("test")
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(r["reason"] == "artifacts_unavailable" for r in self.service.releases))
        claims = sorted(self.service.claims.values(), key=lambda c: c["id"])[:4]
        gaps = [
            later["claimed_at"] - earlier["released_at"] for earlier, later in zip(claims, claims[1:], strict=False)
        ]
        # A minute doubling to ten in production; here 0.4 s then the 0.6 s cap, never within poll_seconds.
        self.assertGreaterEqual(gaps[0], 0.4, gaps)
        self.assertGreaterEqual(gaps[1], 0.6, gaps)
        self.assertGreaterEqual(gaps[2], 0.6, gaps)
        self.assertEqual(result["summary"]["artifacts_backoff"], 0.6)
        self.assertEqual(self.agent_calls(), [])

    def test_a_launch_failure_does_not_back_off_as_if_the_cache_were_missing(self):
        cfg = self.config(poll_seconds=0.05, artifacts_backoff=(5.0, 5.0))
        stop = repair_task.Stop()
        with self.prompt_too_long_for_argv():
            thread, result = self.serve_in_thread(cfg, stop)
            self.wait_for(lambda: len(self.service.releases) >= 2, "the task is released and re-claimed")
            stop.set("test")
            thread.join(30)
        self.assertFalse(thread.is_alive())
        reasons = [r["reason"] for r in self.service.releases]
        # The stop may catch a re-claimed task before its agent, which releases it as worker-stopped.
        self.assertGreaterEqual(reasons.count("agent_launch_failed"), 2, reasons)
        self.assertTrue(all(r in ("agent_launch_failed", "worker-stopped") for r in reasons), reasons)
        self.assertNotIn("artifacts_backoff", result["summary"])
        self.assertEqual(self.agent_calls(), [])

    def test_sigterm_abandons_the_attempt_and_releases_the_claim(self):
        os.environ["STUB_AGENT_MODE"] = "sleep"
        os.environ["STUB_SLEEP"] = "30"
        cfg = self.config(poll_seconds=0.2)
        stop = repair_task.Stop()
        previous = repair_task.install_signal_handlers(stop)
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
        task_json = self.root / "state" / "tasks" / "1" / "task.json"

        def terminate():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if task_json.exists() and json.loads(task_json.read_text()).get("state") == "agent_running":
                    time.sleep(1.0)  # the agent process is up and sleeping
                    os.kill(os.getpid(), signal.SIGTERM)  # what systemd sends on stop
                    return
                time.sleep(0.05)

        threading.Thread(target=terminate, daemon=True).start()
        started = time.monotonic()
        summary = repair_task.serve(cfg, self.client(cfg), stop)  # the main thread: where the handler runs
        self.assertLess(time.monotonic() - started, 25, "the sleeping agent was stopped promptly")
        self.assertEqual(stop.reason, "SIGTERM")
        self.assertEqual(summary["outcomes"], {"released": 1}, summary)
        state = self.state()
        self.assertEqual(state["state"], "released")
        self.assertEqual(state["reason"], "worker-stopped")
        # Nothing was submitted, the claim was released at once and the worktree removed.
        self.assertEqual(self.service.results, [])
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])
        self.assertIsNone(self.service.live_claim(1), "the task is claimable again at once")
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())
        self.assertEqual(self.heartbeat(cfg)["state"], "stopped")
        # The next start has nothing to continue.
        self.assertEqual(repair_task.retry_deliveries(cfg, self.client(cfg)), [])
        self.assertEqual(len(self.agent_calls()), 1)

    def test_stop_before_a_claim_does_not_claim(self):
        cfg = self.config(poll_seconds=0.1)
        stop = repair_task.Stop()
        stop.set("early")
        summary = repair_task.serve(cfg, self.client(cfg), stop)
        self.assertEqual(summary["tasks"], 0)
        self.assertEqual(self.service.claim_attempts, [])
        self.assertEqual(self.heartbeat(cfg)["state"], "stopped")


class ReleaseRetry(Fixture):
    """Claims given back through outages: an errored task releases its claim, a release the service
    could not confirm is retried from the checkpoint before the next claim, and retained refs the
    service publishes a moment after the task are waited for."""

    RETAINED_TIP = "refs/heads/queue/batch-1/tip"

    def drop_retained_ref(self):
        sh(self.content.bare, "update-ref", "-d", self.RETAINED_TIP)

    def restore_retained_ref(self):
        sh(self.content.bare, "update-ref", self.RETAINED_TIP, self.content.tip)

    def retain_many(self, count: int) -> list[str]:
        """``count`` retained refs on the bare repository, created in one ``update-ref --stdin``."""
        refs = [f"refs/heads/queue/inputs/{n}" for n in range(count)]
        subprocess.run(
            ["git", "-C", str(self.content.bare), "update-ref", "--stdin"],
            input="".join(f"update {r} {self.content.tip}\n" for r in refs),
            text=True,
            check=True,
            capture_output=True,
        )
        return refs

    def fetch_calls(self):
        """Run with ``repair_task.git`` observed; returns (calls, restore) for the fetch invocations."""
        calls: list[tuple[str, ...]] = []
        original = repair_task.git

        def observed(clone, *args, **kwargs):
            if args[:1] == ("fetch",):
                calls.append(args)
            return original(clone, *args, **kwargs)

        repair_task.git = observed
        self.addCleanup(setattr, repair_task, "git", original)
        return calls

    def observe_log(self) -> list[str]:
        lines: list[str] = []
        original = repair_task.log
        repair_task.log = lines.append
        self.addCleanup(setattr, repair_task, "log", original)
        return lines

    def test_many_retained_refs_are_fetched_a_chunk_at_a_time(self):
        refs = self.retain_many(450)
        task = repair_task.Task.from_json(
            {**self.content.task(), "retained_refs": refs, "claim_token": "t", "expires_at": time.time() + 60}
        )
        calls = self.fetch_calls()
        fetched = repair_task.fetch_retained(self.content.clone, task, chunk=200)
        self.assertEqual(len(fetched["refs"]), 450)
        self.assertEqual([len(c) - 3 for c in calls], [200, 200, 50], "one fetch per chunk of refspecs, not per ref")
        self.assertEqual(
            sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/retained/queue/inputs/449"), self.content.tip
        )

    def test_a_chunk_with_missing_refs_is_trimmed_by_one_listing_not_fetched_ref_by_ref(self):
        # Production: the service publishes a task's refs through its outbox over minutes, so a chunk
        # fails on the first ref the remote lacks (git names only that one) and the old fallback
        # fetched the chunk's other 199 refs one at a time, 1.3 s each, on every retry.
        refs = self.retain_many(450)
        absent = [
            "refs/heads/queue/inputs/absent-a",
            "refs/heads/queue/inputs/absent-b",
            "refs/heads/queue/inputs/absent-c",
        ]
        listed = refs[:10] + absent[:1] + refs[10:250] + absent[1:] + refs[250:]
        task = repair_task.Task.from_json(
            {**self.content.task(), "retained_refs": listed, "claim_token": "t", "expires_at": time.time() + 60}
        )
        calls = self.fetch_calls()
        warnings = self.observe_log()
        with self.assertRaises(repair_task.SourcesUnavailable) as failed:
            repair_task.fetch_retained(self.content.clone, task, chunk=200)
        message = str(failed.exception)
        self.assertIn("3 retained ref(s) not fetchable", message)
        self.assertIn("queue/inputs/absent-a", message)
        # Chunk 1 (one missing): fail, list, fetch 199. Chunk 2 (two missing): fail, list, fetch 198. Chunk 3: fine.
        self.assertEqual([len(c) - 3 for c in calls], [200, 199, 200, 198, 53])
        self.assertTrue(any("warning: fetch of 200 retained refs failed" in line for line in warnings), warnings)
        self.assertTrue(any("couldn't find remote ref" in line for line in warnings), "git's stderr is logged")
        self.assertTrue(any("2 of 200 retained refs are not on the remote yet" in line for line in warnings), warnings)
        # Every present ref was fetched despite the failure.
        self.assertEqual(
            sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/retained/queue/inputs/449"), self.content.tip
        )

    def test_a_retry_fetches_only_the_refs_the_earlier_attempt_lacked(self):
        refs = self.retain_many(450)
        late = "refs/heads/queue/inputs/late"
        task = repair_task.Task.from_json(
            {
                **self.content.task(),
                "retained_refs": refs[:100] + [late] + refs[100:],
                "claim_token": "t",
                "expires_at": time.time() + 60,
            }
        )
        calls = self.fetch_calls()
        timer = threading.Timer(0.5, lambda: sh(self.content.bare, "update-ref", late, self.content.tip))
        timer.start()
        self.addCleanup(timer.cancel)
        fetched = repair_task.fetch_retained(self.content.clone, task, chunk=200, retry_seconds=20, retry_delay=0.3)
        self.assertGreaterEqual(fetched["attempts"], 2)
        # Attempt 1: chunk 1 fails and is trimmed (2 fetches), chunks 2 and 3 succeed; later attempts
        # fetch only the one ref still missing, never the 450 again.
        self.assertEqual([len(c) - 3 for c in calls[:4]], [200, 199, 200, 51])
        self.assertTrue(all(len(c) - 3 == 1 for c in calls[4:]), [len(c) - 3 for c in calls])
        self.assertEqual(
            sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/retained/queue/inputs/late"),
            self.content.tip,
        )

    def test_without_a_listing_a_failed_chunk_is_halved_not_walked(self):
        refs = self.retain_many(200)
        listed = refs[:77] + ["refs/heads/queue/inputs/absent"] + refs[77:]
        task = repair_task.Task.from_json(
            {**self.content.task(), "retained_refs": listed, "claim_token": "t", "expires_at": time.time() + 60}
        )
        calls = self.fetch_calls()
        original = repair_task.git

        def no_listing(clone, *args, **kwargs):
            if args[:1] == ("ls-remote",):
                raise RuntimeError("git ls-remote failed (128): fatal: unable to access origin")
            return original(clone, *args, **kwargs)

        repair_task.git = no_listing
        self.addCleanup(setattr, repair_task, "git", original)
        with self.assertRaises(repair_task.SourcesUnavailable) as failed:
            repair_task.fetch_retained(self.content.clone, task, chunk=200)
        self.assertIn("queue/inputs/absent", str(failed.exception))
        self.assertIn("couldn't find remote ref", str(failed.exception))
        self.assertLess(len(calls), 24, f"one bad ref in 201 costs about 2 log2(n) fetches, not n: {len(calls)}")
        self.assertGreater(len(calls), 8)
        for n in (0, 76, 77, 199):
            self.assertEqual(
                sh(self.content.clone, "rev-parse", f"refs/tauceti-queue/task/1/retained/queue/inputs/{n}"),
                self.content.tip,
            )

    def test_a_retained_ref_published_late_is_waited_for(self):
        self.drop_retained_ref()
        timer = threading.Timer(1.0, self.restore_retained_ref)
        timer.start()
        self.addCleanup(timer.cancel)
        cfg = self.config(fetch_retry_seconds=20, fetch_retry_delay=0.3)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertGreaterEqual(outcome["sources"]["attempts"], 2, "the first fetch found no ref")
        self.assertEqual(self.service.releases, [])

    def test_sources_still_missing_after_the_window_release_the_claim(self):
        self.drop_retained_ref()
        cfg = self.config(fetch_retry_seconds=0.5, fetch_retry_delay=0.1)
        with self.assertRaises(repair_task.SourcesUnavailable):
            repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual([r["reason"] for r in self.service.releases], ["artifacts_unavailable"])
        self.assertIn("queue/batch-1/tip", self.service.releases[0]["error"])
        state = self.state()
        self.assertEqual(state["state"], "released")
        self.assertEqual([h["state"] for h in state["history"]][-2:], ["error", "released"])
        self.assertIsNone(state["pending_release"])
        self.assertIsNone(self.service.live_claim(1), "the task is claimable again at once")
        self.assertEqual(self.agent_calls(), [])

    def test_an_unconfirmed_release_is_retried_from_the_checkpoint_before_the_next_claim(self):
        self.drop_retained_ref()
        self.service.ttl = 600  # production's claim_ttl_seconds: an unreleased claim blocks the task for long
        self.service.release_mode = "error"
        cfg = self.config(fetch_retry_seconds=0, release_backoff=(0.0,))
        client = self.client(cfg)
        with self.assertRaises(repair_task.SourcesUnavailable):
            repair_task.claim_and_run(cfg, client)
        state = self.state()
        self.assertEqual(state["state"], "release_failed")
        self.assertEqual(state["pending_release"]["reason"], "artifacts_unavailable")
        self.assertEqual(state["pending_release"]["attempts"], 1)
        self.assertEqual(state["task"]["claim_token"], self.service.claims[1]["token"], "the claim token is kept")
        self.assertEqual(self.service.releases, [])
        self.assertIsNotNone(self.service.live_claim(1))
        # While the service is still down a pass keeps the pending release and counts the attempt.
        self.assertEqual(repair_task.retry_releases(cfg, client), [])
        self.assertEqual(self.state()["pending_release"]["attempts"], 2)
        self.assertIsNone(client.claim(), "our own stale claim makes the task unclaimable (concurrency)")
        # The service is back and the ref exists: the loop settles the release first, then claims
        # the task again and runs it to adoption, instead of polling for the claim's whole TTL.
        self.service.release_mode = "ok"
        self.restore_retained_ref()
        stop = repair_task.Stop()
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(summary=repair_task.serve(cfg, client, stop)), daemon=True
        )
        thread.start()
        self.addCleanup(stop.set, "cleanup")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not self.service.results:
            time.sleep(0.05)
        stop.set("test")
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual([r["reason"] for r in self.service.releases], ["artifacts_unavailable"])
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(result["summary"]["releases_settled"], 1)
        self.assertEqual(result["summary"]["outcomes"], {"adopted": 1})
        states = [h["state"] for h in self.state()["history"]]
        self.assertLess(states.index("released"), states.index("adopted"))
        self.assertEqual(self.state()["state"], "adopted")
        self.assertIsNone(self.state()["pending_release"])

    def test_an_error_checkpoint_written_before_pending_releases_is_released_once(self):
        cfg = self.config()
        client = self.client(cfg)
        claim = self.service.preclaim(1, "old-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("claimed", worker=cfg.worker)
        state.write("error", error="RuntimeError: git fetch failed", phase="claimed")
        # Another worker's checkpoint in the same root is not ours to release.
        other = repair_task.TaskState(cfg.tasks_dir / "9", task)
        other.write("claimed", worker="worker-b@test")
        other.write("error", error="RuntimeError: git fetch failed", phase="claimed")
        settled = repair_task.retry_releases(cfg, client)
        self.assertEqual(settled, [{"task_id": "1", "state": "released", "reason": "artifacts_unavailable"}])
        self.assertEqual([r["reason"] for r in self.service.releases], ["artifacts_unavailable"])
        self.assertIsNone(self.service.live_claim(1))
        self.assertEqual(repair_task.retry_releases(cfg, client), [], "a settled release is not sent again")
        self.assertEqual(len(self.service.releases), 1)
        # A pending release whose claim the service no longer knows is settled as lost.
        gone = repair_task.Task.from_json({**task.to_json(), "claim_token": "gone"})
        lost = repair_task.TaskState(cfg.tasks_dir / "2", gone)
        lost.write("claimed", worker=cfg.worker)
        lost.write("error", error="RuntimeError: boom", phase="agent_running")
        self.assertEqual(
            repair_task.retry_releases(cfg, client), [{"task_id": "1", "state": "claim_lost", "reason": "agent_failed"}]
        )
        self.assertEqual(len(self.service.releases), 1)


class StopAndRelease(Fixture):
    """A claim never outlives the worker that holds it: a stop releases it, a restart releases whatever
    a stopped run left, and a claim refused for our own stale claim is given back and retried at once."""

    RETAINED_TIP = "refs/heads/queue/batch-1/tip"

    def install_handlers(self) -> repair_task.Stop:
        stop = repair_task.Stop()
        previous = repair_task.install_signal_handlers(stop)
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
        return stop

    def terminate_when(self, predicate, delay: float = 0.5) -> None:
        def terminate():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if predicate():
                    time.sleep(delay)
                    os.kill(os.getpid(), signal.SIGTERM)  # what systemd sends on stop
                    return
                time.sleep(0.05)

        threading.Thread(target=terminate, daemon=True).start()

    def checkpoint_state(self, task_id: str = "1") -> str | None:
        path = self.root / "state" / "tasks" / task_id / "task.json"
        return json.loads(path.read_text()).get("state") if path.exists() else None

    def test_sigterm_before_the_agent_releases_the_claim_as_worker_stopped(self):
        # The retained tip is not published yet, so the claimed task waits in the fetch retry window
        # (production: the stop at 03:06 landed two minutes into the `claimed` phase).
        sh(self.content.bare, "update-ref", "-d", self.RETAINED_TIP)
        cfg = self.config(poll_seconds=0.2, fetch_retry_seconds=60, fetch_retry_delay=0.2)
        stop = self.install_handlers()
        self.terminate_when(lambda: self.checkpoint_state() == "claimed")
        started = time.monotonic()
        summary = repair_task.serve(cfg, self.client(cfg), stop)
        self.assertLess(time.monotonic() - started, 20, "the fetch retry window was left at once")
        self.assertEqual(stop.reason, "SIGTERM")
        self.assertEqual(summary["outcomes"], {"released": 1}, summary)
        self.assertEqual(summary["errors"], 0)
        state = self.state()
        self.assertEqual(state["state"], "released")
        self.assertEqual(state["reason"], "worker-stopped")
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])
        self.assertIn("claimed", self.service.releases[0]["error"])
        self.assertIsNone(self.service.live_claim(1), "the task is claimable again at once, not at the TTL")
        self.assertEqual(self.agent_calls(), [])
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())
        # Once the ref is published a fresh claim runs the task to adoption.
        sh(self.content.bare, "update-ref", self.RETAINED_TIP, self.content.tip)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)

    def test_sigterm_with_the_agent_running_releases_the_claim(self):
        os.environ["STUB_AGENT_MODE"] = "sleep"
        os.environ["STUB_SLEEP"] = "30"
        cfg = self.config(poll_seconds=0.2)
        stop = self.install_handlers()
        self.terminate_when(lambda: self.checkpoint_state() == "agent_running", delay=1.0)
        summary = repair_task.serve(cfg, self.client(cfg), stop)
        self.assertEqual(summary["outcomes"], {"released": 1}, summary)
        self.assertIsNone(self.service.live_claim(1))
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])

    def test_on_start_a_checkpoint_stopped_before_the_agent_is_released_and_claimed_fresh(self):
        cfg = self.config()
        client = self.client(cfg)
        # A stopped run left the task claimed (its token in the checkpoint) and never got further.
        claim = self.service.preclaim(1, "stopped-run-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("claimed", worker=cfg.worker)
        self.assertIsNone(client.claim(), "the stale claim blocks a fresh one (concurrency)")
        outcomes = repair_task.retry_deliveries(cfg, client)
        self.assertEqual([(o["status"], o["reason"]) for o in outcomes], [("released", "worker-stopped")])
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])
        self.assertEqual(self.state()["state"], "released")
        self.assertIsNone(self.service.live_claim(1))
        outcome = repair_task.claim_and_run(cfg, client)
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.agent_calls()[0]["resume"], None, "a fresh run, not a resume")

    def test_a_claim_refused_for_our_own_stale_claim_is_released_and_retried_at_once(self):
        cfg = self.config()
        client = self.client(cfg)
        claim = self.service.preclaim(1, "stopped-run-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("claimed", worker=cfg.worker)
        state.write("sources_ready", worker=cfg.worker)
        # Another worker's in-flight checkpoint in the same root is not ours to release.
        other = repair_task.TaskState(cfg.tasks_dir / "9", task)
        other.write("claimed", worker="worker-b@test")
        outcome = repair_task.claim_and_run(cfg, client)
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.claim_attempts), 2, "refused once, released the stale claim, claimed again")
        self.assertEqual([r["reason"] for r in self.service.releases], ["worker-stopped"])
        self.assertEqual(self.service.releases[0]["worker"], cfg.worker)
        self.assertEqual(json.loads((cfg.tasks_dir / "9" / "task.json").read_text())["state"], "claimed")
        # Nothing stale left: a later empty claim stays empty with no extra release.
        self.assertIsNone(repair_task.claim_and_run(cfg, client))
        self.assertEqual(len(self.service.releases), 1)


class PublishOutputs(Fixture):
    """After a green repair the worker publishes the worktree's outputs under the tip to the repair
    namespace, and the next preparation consults that namespace for tip, onto and base first."""

    BASE_FILES = {
        "lakefile.toml": 'name = "TauCeti"\nplatformIndependent = true\n',
        "lean-toolchain": "leanprover/lean4:v4.36.0-rc2\n",
        ".gitignore": ".lake/\n",
    }

    def setUp(self):
        super().setUp()
        early = patch.object(repair_publish, "start_early", return_value=None)
        early.start()
        self.addCleanup(early.stop)

    KEY = "access-id:secret-value"

    def settings(self, **overrides) -> repair_publish.Settings:
        key_file = self.root / "repair-cache-key"
        key_file.write_text(self.KEY + "\n")
        values = dict(url="https://repair-cache.example", key_file=key_file)
        values.update(overrides)
        return repair_publish.Settings(**values)

    def puts(self) -> list[str]:
        return [c for c in self.lake_calls() if c.startswith("cache put")]

    def published(self) -> dict:
        """The background publication's record, once its publisher has finished."""
        records = repair_publish.BACKGROUND.wait(60)
        self.assertEqual(len(records), 1, records)
        return records[0]

    def test_a_green_repair_submits_and_then_publishes_the_tip(self):
        submitted: list[float] = []
        submit = self.service.submit_task

        def timed(*args, **kwargs):
            submitted.append(time.time())
            return submit(*args, **kwargs)

        cfg = self.config(publish=self.settings())
        with patch.object(self.service, "submit_task", timed):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        launch = outcome["publish"]
        self.assertTrue(launch["launched"] and launch["background"], launch)
        self.assertEqual(launch["revision"], outcome["tip"])
        record = self.published()
        self.assertEqual(record["state"], "published", record)
        self.assertTrue(record["published"], record)
        self.assertEqual(record["revision"], outcome["tip"])
        self.assertEqual((record["artifacts"], record["bytes"]), (1, 2048))
        self.assertEqual(len(submitted), 1)
        builds = [b for b in self.lake_builds() if "-o" in b["args"]]
        self.assertTrue(builds, "the final build produces the map")
        self.assertLess(builds[-1]["finished"], submitted[0])
        self.assertFalse(any("--no-build" in b["args"] for b in self.lake_builds()))
        calls = self.lake_calls()
        put = next(c for c in calls if c.startswith("cache put-staged "))
        self.assertIn(f"--service tauceti-repair-upload --repo {PROJECT}", put)
        self.assertEqual((self.stub_dir / "lake-put-key").read_text(), self.KEY)
        self.assertEqual((self.stub_dir / "lake-put-toolchain").read_text(), "leanprover/lean4:v4.36.0-rc2")
        self.assertIsNone(self.agent_calls()[0]["env"]["LAKE_CACHE_KEY"], "and never the agent")
        logs = self.root / "state" / "tasks" / "1" / "logs"
        self.assertTrue((logs / repair_publish.BACKGROUND_LOG).is_file())
        self.assertIn("cache stage .lake/outputs.jsonl", (logs / "cache-stage.log").read_text())
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists(), "the publisher removes it")
        config = (self.root / "state" / "lake.toml").read_text()
        self.assertIn('name = "tauceti-repair"', config)
        self.assertIn('artifactEndpoint = "https://repair-cache.example/artifacts"', config)
        self.assertIn('name = "tauceti-repair-upload"', config)
        self.assertNotIn(self.KEY, config)
        # Preparation asked the repair namespace for tip, onto and base before the public cache.
        fetches = [c for c in calls if c.startswith("cache get")]
        repair_fetches = [c.split("--rev=")[1] for c in fetches if "--service tauceti-repair " in c]
        self.assertEqual(repair_fetches, [self.content.tip, self.content.base])
        self.assertNotIn("tauceti-repair", fetches[-1], "the public cache came last")
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.content.base)
        # worker.json reports the publication.
        repair_task.write_heartbeat(cfg, {}, "idle")
        heartbeat = json.loads(repair_task.heartbeat_path(cfg).read_text())["publish"]
        self.assertEqual((heartbeat["published"], heartbeat["failed"], heartbeat["running"]), (1, 0, []))
        self.assertEqual(heartbeat["last"]["revision"], outcome["tip"])

    def test_a_pre_rebase_warms_from_every_published_warm_rev_and_skips_the_missing(self):
        """Production batch 3's pre-rebase onto head 2's tip: the target is not on main, so the public
        cache cannot have it, but the repair namespace has the head's previous tip and its base."""
        raw = self.content.rebase_task()
        raw["kind"] = "pre_rebase"
        onto = raw["onto"]
        previous, unpublished = "8" * 40, "9" * 40
        raw["warm_revs"] = [onto, unpublished, previous, self.content.base]
        self.service.tasks = {1: raw}
        os.environ["STUB_LAKE_REPAIR_REVS"] = f"{previous},{self.content.base}"
        lines: list[str] = []
        original = repair_task.log
        repair_task.log = lines.append
        self.addCleanup(setattr, repair_task, "log", original)
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        fetches = [c for c in self.lake_calls() if c.startswith("cache get")]
        repair_fetches = [c.split("--rev=")[1] for c in fetches if "--service tauceti-repair " in c]
        self.assertEqual(
            repair_fetches,
            [onto, unpublished, previous, self.content.base, self.content.tip],
            "every warm revision was tried in order, then the batch's own tip, each once",
        )
        public = [c.split("--rev=")[1] for c in fetches if "--service tauceti-repair " not in c]
        self.assertEqual(public, [self.content.base], "the public walk starts at the head's base, not its tip")
        artifacts = outcome["artifacts"]
        self.assertEqual(artifacts["warmed_from"], [previous, self.content.base])
        self.assertEqual(artifacts["cache_revision"], self.content.base)
        self.assertTrue(artifacts["available"])
        self.assertIn(
            f"task 1: repair outputs warmed the tree from {previous[:12]}, {self.content.base[:12]}; "
            f"no published mapping for {onto[:12]}, {unpublished[:12]}, {self.content.tip[:12]}",
            lines,
        )
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn(repair.CACHE_SENTENCE, prompt)
        self.assertIn("worker owns final verification", prompt)

    def test_warm_revs_alone_make_the_artifacts_available(self):
        raw = self.content.rebase_task()
        raw["kind"] = "pre_rebase"
        previous = "8" * 40
        raw["warm_revs"] = [raw["onto"], previous, self.content.base]
        self.service.tasks = {1: raw}
        os.environ["STUB_LAKE_REPAIR_REVS"] = previous
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"  # nothing public at all
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        artifacts = outcome["artifacts"]
        self.assertEqual((artifacts["warmed_from"], artifacts["cache_revision"]), ([previous], previous))
        self.assertEqual(artifacts["cache_service"], "tauceti-repair")
        self.assertTrue(artifacts["available"])
        self.assertNotIn("cold", artifacts)

    def test_a_head_repair_warms_from_the_first_published_tip_of_the_head(self):
        """Production head 2's repair task 10: its current tip was never published, but the repair
        namespace has its previous tip's outputs."""
        raw = self.content.task()
        previous, older, canonical = "8" * 40, "7" * 40, "6" * 40
        raw["warm_revs"] = [self.content.tip, previous, older, self.content.base, canonical]
        self.service.tasks = {1: raw}
        os.environ["STUB_LAKE_REPAIR_REVS"] = f"{previous},{older}"
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"  # nothing public at all
        lines: list[str] = []
        original = repair_task.log
        repair_task.log = lines.append
        self.addCleanup(setattr, repair_task, "log", original)
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        fetches = [c for c in self.lake_calls() if c.startswith("cache get")]
        repair_fetches = [c.split("--rev=")[1] for c in fetches if "--service tauceti-repair " in c]
        self.assertEqual(repair_fetches, raw["warm_revs"], "every warm revision was tried in order, each once")
        artifacts = outcome["artifacts"]
        self.assertEqual(artifacts["warmed_from"], [previous, older])
        self.assertEqual((artifacts["cache_revision"], artifacts["cache_service"]), (previous, "tauceti-repair"))
        self.assertTrue(artifacts["available"])
        self.assertIn(
            f"task 1: repair outputs warmed the tree from {previous[:12]}, {older[:12]}; "
            f"no published mapping for {self.content.tip[:12]}, {self.content.base[:12]}, {canonical[:12]}",
            lines,
        )
        self.assertTrue(
            any(
                line.startswith(f"task 1: warm artifacts from {previous[:12]} (an earlier tip of the head)")
                for line in lines
            ),
            lines,
        )

    def test_before_publishes_ahead_of_the_push(self):
        cfg = self.config(publish=self.settings(when="before"))
        pushed_at: list[float] = []
        push = repair_task.push_result

        def timed(*args, **kwargs):
            pushed_at.append(time.time())
            return push(*args, **kwargs)

        with patch.object(repair_task, "push_result", timed):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertTrue(outcome["publish"]["published"], outcome["publish"])
        self.assertNotIn("background", outcome["publish"])
        check = next(b for b in self.lake_builds() if "-o" in b["args"])
        self.assertLess(check["finished"], pushed_at[0], "published before the push")
        self.assertEqual(repair_publish.BACKGROUND.running, {})
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())

    def test_off_publishes_nothing(self):
        cfg = self.config(publish=self.settings(when="off"))
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertFalse(outcome["publish"]["launched"])
        self.assertEqual(outcome["publish"]["skipped"], "TAUCETI_REPAIR_PUBLISH is off")
        self.assertEqual(self.puts(), [])
        self.assertEqual([b for b in self.lake_builds() if "--no-build" in b["args"]], [])
        self.assertEqual(repair_publish.BACKGROUND.running, {})
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists(), "the task removes it")
        # The read side still prefers the repair namespace.
        self.assertTrue(any("--service tauceti-repair " in c for c in self.lake_calls() if c.startswith("cache get")))

    def test_the_mapping_is_uploaded_after_the_artifacts(self):
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(self.published()["state"], "published")
        uploads = (self.stub_dir / "remote" / "uploads.log").read_text().splitlines()
        self.assertEqual(uploads, ["artifact 00aa00aa00aa00aa.ltar", f"mapping {outcome['tip']}"])
        self.assertTrue((self.stub_dir / "remote" / "revisions" / f"{outcome['tip']}.jsonl").is_file())

    def test_a_partial_upload_leaves_no_mapping(self):
        os.environ["STUB_LAKE_PUT_FAIL"] = "partial"
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        record = self.published()
        self.assertEqual((record["state"], record["error"]), ("failed", "lake cache put-staged exited 1"))
        self.assertTrue((self.stub_dir / "remote" / "artifacts" / "00aa00aa00aa00aa.ltar").is_file())
        self.assertFalse((self.stub_dir / "remote" / "revisions").exists(), "no mapping for a partial set")

    def test_a_failed_upload_is_logged_and_the_task_still_submits(self):
        os.environ["STUB_LAKE_PUT_FAIL"] = "1"
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 1)
        self.assertTrue(self.service.results[0]["response"]["accepted"])
        record = self.published()
        self.assertFalse(record["published"])
        self.assertEqual((record["state"], record["error"]), ("failed", "lake cache put-staged exited 1"))
        repair_task.write_heartbeat(cfg, {}, "idle")
        heartbeat = json.loads(repair_task.heartbeat_path(cfg).read_text())["publish"]
        self.assertEqual((heartbeat["published"], heartbeat["failed"]), (0, 1))
        self.assertEqual(heartbeat["last_failure"]["error"], "lake cache put-staged exited 1")
        self.assertEqual(self.state()["state"], "adopted", "the checkpoint keeps the submission's outcome")

    def test_a_publisher_that_cannot_start_does_not_fail_the_task(self):
        cfg = self.config(publish=self.settings())
        with patch.object(repair_publish, "_spawn", side_effect=OSError("no processes left")):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(outcome["publish"]["state"], "failed")
        self.assertIn("no processes left", outcome["publish"]["error"])
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists(), "the task removes it")

    def test_under_systemd_the_publisher_is_a_unit_of_its_own(self):
        """A worker in a unit starts the publisher with ``systemd-run --user``, passing the lake and path
        variables and none of the worker's credentials."""
        bindir = self.root / "systemd-bin"
        bindir.mkdir()
        tool = bindir / "systemd-run"
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_DIR/systemd-run-args"\n')
        tool.chmod(0o755)
        env = repair.lake_env(self.root / "state")
        env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", INVOCATION_ID="abc", GH_TOKEN="gh-secret")
        logdir = self.root / "logs"
        record = repair_publish.start_background(
            self.content.clone, self.content.tip, settings=self.settings(), env=env, logdir=logdir, task_id="7"
        )
        self.assertTrue(record["launched"], record)
        self.assertTrue(record["unit"].startswith(f"tauceti-repair-publish-7-{self.content.tip[:12]}-"))
        args = (self.stub_dir / "systemd-run-args").read_text().splitlines()
        self.assertEqual(args[:3], ["--user", "--quiet", "--collect"])
        # the transient unit starts from the user manager, so it is given the worker's priority
        self.assertIn("--slice=tauceti-repair.slice", args)
        self.assertIn("--property=Nice=10", args)
        self.assertIn("--property=IOSchedulingClass=best-effort", args)
        self.assertIn("--property=IOSchedulingPriority=7", args)
        self.assertIn("--property=OOMScoreAdjust=1000", args)
        self.assertIn(f"--setenv=LAKE_CACHE_DIR={env['LAKE_CACHE_DIR']}", args)
        self.assertFalse([a for a in args if "gh-secret" in a or "INVOCATION_ID" in a], args)
        self.assertEqual(args[-2:], ["tauceti_queue.repair_publish", str(logdir / repair_publish.SPEC)])
        self.assertNotIn(self.KEY, "\n".join(args))
        self.assertEqual(json.loads((logdir / repair_publish.RECORD).read_text())["state"], "running")

    def test_the_publisher_takes_the_repair_units_priority(self):
        """The background publisher's transient unit matches what the repair units set, so publication
        never runs above the worker that started it."""
        for name in ("tauceti-queue-repair.service", "tauceti-queue-repair@.service"):
            text = (Path(__file__).resolve().parents[1] / "services/queue" / name).read_text()
            with self.subTest(unit=name):
                self.assertEqual(re.findall(r"^Slice=(.*)$", text, re.M), [repair_publish.SLICE])
                for prop in repair_publish.UNIT_PRIORITY:
                    self.assertEqual(re.findall(rf"^{prop.split('=')[0]}=(.*)$", text, re.M), [prop.split("=", 1)[1]])

    def test_niced_reaches_a_target_niceness_without_compounding(self):
        env = {"PATH": os.environ.get("PATH", "")}
        with patch.object(repair_publish.os, "getpriority", return_value=0):
            self.assertEqual(repair_publish.niced(["x"], 10, env), ["nice", "-n", "10", "x"])
        with patch.object(repair_publish.os, "getpriority", return_value=4):
            self.assertEqual(repair_publish.niced(["x"], 10, env), ["nice", "-n", "6", "x"])
        for current in (10, 15):
            with patch.object(repair_publish.os, "getpriority", return_value=current):
                self.assertEqual(repair_publish.niced(["x"], 10, env), ["x"], "already at least that nice")

    def test_without_a_key_or_when_switched_off_nothing_is_put(self):
        for settings, reason in (
            (self.settings(key_file=self.root / "missing"), "no write key"),
            (self.settings(publish=False), "TAUCETI_QUEUE_PUBLISH_CACHE is off"),
        ):
            with self.subTest(reason=reason):
                record = repair_publish.publish_outputs(
                    self.content.clone,
                    "0" * 40,
                    settings=settings,
                    env=repair.lake_env(self.root / "state"),
                    logdir=self.root / "logs",
                )
                self.assertFalse(record["published"])
                self.assertIn(reason, record["skipped"])
        self.assertEqual(self.puts(), [])
        self.assertEqual([c for c in self.lake_calls() if c.startswith("build")], [])

    def test_outputs_that_are_not_green_are_not_published(self):
        worktree = self.root / "broken"
        sh(self.content.clone, "worktree", "add", "--detach", str(worktree), self.content.tip)
        record = repair_publish.publish_outputs(
            worktree,
            self.content.tip,
            settings=self.settings(),
            env=repair.lake_env(self.root / "state"),
            logdir=self.root / "logs",
        )
        self.assertFalse(record["published"])
        self.assertIn("no complete final-build map", record["skipped"])
        self.assertEqual(self.puts(), [])

    def test_settings_from_the_environment(self):
        key_file = self.root / "key"
        key_file.write_text("k\n")
        base = {"TAUCETI_REPAIR_CACHE_URL": "https://r.example/", "TAUCETI_REPAIR_CACHE_KEY_FILE": str(key_file)}
        on = repair_publish.Settings.from_env(base)
        self.assertEqual((on.url, on.upload_base, on.key()), ("https://r.example", "https://r.example", "k"))
        self.assertIsNone(on.disabled_reason(), "on by default when the key is present")
        self.assertIsNotNone(
            repair_publish.Settings.from_env({**base, "TAUCETI_QUEUE_PUBLISH_CACHE": "0"}).disabled_reason()
        )
        self.assertIsNotNone(repair_publish.Settings.from_env({}).disabled_reason())
        self.assertEqual(repair_publish.Settings.from_env({}).url, None)
        upload = repair_publish.Settings.from_env({**base, "TAUCETI_REPAIR_CACHE_UPLOAD_URL": "https://s3.example/b"})
        self.assertEqual(upload.upload_base, "https://s3.example/b")
        self.assertEqual(on.when, "after", "after the submission by default")
        for value, when in (("before", "before"), ("OFF", "off"), ("after", "after"), ("later", "after")):
            self.assertEqual(repair_publish.Settings.from_env({**base, "TAUCETI_REPAIR_PUBLISH": value}).when, when)
        off = repair_publish.Settings.from_env({**base, "TAUCETI_REPAIR_PUBLISH": "off"})
        self.assertEqual(off.disabled_reason(), "TAUCETI_REPAIR_PUBLISH is off")
        self.assertEqual(repair_publish.Settings.from_json(json.loads(json.dumps(on.to_json()))), on)

    def test_configure_replaces_its_own_services_and_keeps_the_public_one(self):
        env = repair.lake_env(self.root / "state")
        env["LAKE_CACHE_KEY"] = "inherited"
        repair_publish.configure(env, self.settings(url="https://one.example"))
        repair_publish.configure(env, self.settings(url="https://two.example"))
        text = Path(env["LAKE_CONFIG"]).read_text()
        self.assertNotIn("LAKE_CACHE_KEY", env)
        self.assertNotIn("one.example", text)
        self.assertEqual(text.count('name = "tauceti-repair"'), 1)
        self.assertEqual(text.count('name = "tauceti-repair-upload"'), 1)
        self.assertEqual(text.count("[[cache.service]]"), 3, "the public service is kept")


class ParallelRepair(Fixture):
    """Independent failing groups (members #2 and #3, disjoint files) repaired by concurrent agents, each
    in its own worktree of the tip and its own child process, whose fixup commits the worker folds."""

    BROKEN = (2, 3)
    PROVIDER_WORDS = ("claude", "codex", "anthropic", "openai", "subagent", "agent tool", "task tool", "--resume")

    def group_worktree(self, number: int) -> Path:
        return self.root / "state" / "worktrees" / f"task-1-group-{number}"

    def assert_two_groups_folded(self, outcome: dict) -> None:
        parallel = outcome["parallel"]
        self.assertEqual(parallel["status"], "ok", parallel)
        self.assertEqual([g["status"] for g in parallel["groups"]], ["folded", "folded"])
        self.assertEqual([g["members"] for g in parallel["groups"]], [[2], [3]])
        self.assertEqual([g["paths"] for g in parallel["groups"]], [["TauCeti/Two.lean"], ["TauCeti/Three.lean"]])
        self.assertEqual(parallel["fold"]["cherry_picked"], 4, "a fixup and a squash per group")
        self.assertEqual(parallel["build"]["rc"], 0)
        self.assertTrue(all(g["seconds"] is not None for g in parallel["groups"]))
        self.assertEqual(parallel["tip"], outcome["tip"])
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertEqual(pushed, outcome["tip"])
        comparison = chain_tool.compare(
            self.content.bare, self.content.base, self.content.tip, self.content.base, pushed
        )
        by_pr = {m.source_pr: m for m in comparison.members}
        for number in (2, 3):
            member = by_pr[f"{PROJECT}#{number}"]
            self.assertEqual(member.provenance, chain_tool.EDITED, member)
            self.assertTrue(member.message_preserved)
            self.assertIn("Repair:", member.explanation)
        self.assertEqual(by_pr[f"{PROJECT}#1"].provenance, chain_tool.CLEAN)
        self.assertEqual(len(sh(self.content.bare, "rev-list", f"{self.content.base}..{pushed}").split()), 3)

    def test_two_independent_groups_are_repaired_in_parallel_and_folded(self):
        os.environ["STUB_GROUP_BUILD"] = "1"  # each group agent checks its fix through the scoped build script
        os.environ["STUB_GROUP_BARRIER"] = "2"  # neither agent finishes until both have started
        os.environ["STUB_GROUP_SLEEP"] = "0"
        cfg = self.config(parallel_agents=3)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assert_two_groups_folded(outcome)
        # Two group agents ran at the same time, each in its own worktree of the tip with a scoped prompt.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(c["cwd"] for c in calls), [str(self.group_worktree(1)), str(self.group_worktree(2))])
        self.assertEqual({c["agent"] for c in calls}, {"claude"})
        self.assertEqual(sorted(c["call"] for c in calls), [1, 2], "concurrent agents take distinct call numbers")
        finishes = {f["call"]: f for f in self.agent_finishes()}
        first, second = finishes[1], finishes[2]
        self.assertLess(first["started"], second["finished"])
        self.assertLess(second["started"], first["finished"])
        two = next(c["prompt"] for c in calls if c["cwd"] == str(self.group_worktree(1)))
        self.assertTrue(two.startswith(repair.AGENT_INSTRUCTION))
        self.assertIn(repair_parallel.GROUP_HEADING, two)
        self.assertIn("`TauCeti/Two.lean:1:20` (PR #2's commit `", two)
        self.assertIn("Files you may edit: `TauCeti/Two.lean`.", two)
        self.assertNotIn("TauCeti/Three.lean", two, "the other group's module is not this agent's business")
        self.assertIn("python3 .tauceti-repair/build TauCeti.Two`", two)
        self.assertIn("git commit --fixup=<sha>", two)
        self.assertIn("## Likely collisions and renames", two)
        self.assertIn("Do not rebase, reorder, squash or amend the chain", two)
        self.assertNotIn("linear chain on", two, "the group prompt does not ask for a rewritten chain")
        self.assertEqual(
            sorted(json.loads(self.service.results[0]["worker_notes"])["edited"]), [f"{PROJECT}#2", f"{PROJECT}#3"]
        )
        # The group agents' scoped builds and the worker's one whole-library verification build, all under
        # the task worktree's lock, so none of them overlapped; the group worktrees are gone afterwards.
        task_worktree = str(self.root / "state" / "worktrees" / "task-1")
        builds = sorted(self.lake_builds(), key=lambda b: b["started"])
        scoped = [b for b in builds if b["cwd"] != task_worktree]
        self.assertEqual(
            sorted(b["args"] for b in scoped),
            [["build", "--iofail", "TauCeti.Three"], ["build", "--iofail", "TauCeti.Two"]],
        )
        whole = [b for b in builds if b["cwd"] == task_worktree and b["args"] == ["build", "--iofail"]]
        self.assertEqual(len(whole), 2, "the artifact preparation's build, then the verification build")
        self.assertGreater(whole[-1]["started"], max(first["finished"], second["finished"]))
        for earlier, later in zip(builds, builds[1:], strict=False):
            self.assertGreaterEqual(later["started"], earlier["finished"], "builds are serialized by the lock")
        self.assertEqual(outcome["parallel"]["lock"], f"{task_worktree}/.tauceti-repair/build.lock")
        self.assertFalse(self.group_worktree(1).exists())
        self.assertFalse(self.group_worktree(2).exists())
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())
        self.assertEqual(self.state()["parallel"]["status"], "ok")
        # The groups came from the task's own diagnostics, without waiting for the streaming build;
        # the verification build ran in the sandbox like the rest.
        self.assertEqual(outcome["parallel"]["source"], "diagnostics")
        self.assertNotIn("stream_wait_seconds", outcome["parallel"])
        self.assertTrue(outcome["stream_build"]["finished"])
        self.assertEqual(outcome["stream_build"]["failed_modules"], ["TauCeti.Three", "TauCeti.Two"])
        sandboxed = [c["argv"][c["argv"].index("--") + 1 :] for c in self.bwrap_calls()]
        self.assertEqual(sandboxed.count(["lake", "build", "--iofail"]), 2, "the streaming and verification builds")
        self.assertIn(["lake", "build", "--iofail", "TauCeti.Two"], sandboxed)
        # Each group ran in its own child process with its own checkpoint, prompt and result.
        for number in (1, 2):
            group_dir = self.root / "state" / "tasks" / "1" / "groups" / f"group-{number}"
            self.assertEqual(json.loads((group_dir / "result.json").read_text())["status"], "ok")
            self.assertEqual(json.loads((group_dir / "group.json").read_text())["state"], "agent_running")
            self.assertTrue((group_dir / "prompt.md").exists())

    def test_under_reported_diagnostics_go_sequential_at_once_without_waiting_for_the_stream(self):
        # CI's --fail-fast reported only the first broken module. Planning never waits for the worker's
        # streaming build to find the rest: one owner means the sequential agent starts at once, with
        # the build's growing error list.
        task = self.service.tasks[1]
        task["diagnostics"] = [d for d in task["diagnostics"] if d["module"] == "TauCeti/Two.lean"]
        cfg = self.config(parallel_agents=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["parallel"]["status"], "sequential", outcome["parallel"])
        self.assertEqual(outcome["parallel"]["source"], "diagnostics")
        self.assertNotIn("stream_wait_seconds", outcome["parallel"])
        calls = self.agent_calls()
        self.assertEqual(calls[0]["cwd"], str(self.root / "state" / "worktrees" / "task-1"))
        self.assertIn(".tauceti-repair/errors.jsonl", calls[0]["prompt"])

    def test_group_agents_run_under_the_codex_argv_shape_too(self):
        cfg = self.config(parallel_agents=2, providers=("codex",))
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assert_two_groups_folded(outcome)
        calls = self.agent_calls()
        self.assertEqual([c["agent"] for c in calls], ["codex", "codex"])
        self.assertEqual({c["prompt_via"] for c in calls}, {"argv"})
        self.assertEqual({a["provider"] for a in outcome["parallel"]["attempts"]}, {"codex"})

    def test_a_group_whose_agent_fails_falls_back_to_the_sequential_path(self):
        os.environ["STUB_FAIL_GROUP_WITH"] = "TauCeti/Three.lean"
        cfg = self.config(parallel_agents=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        parallel = outcome["parallel"]
        self.assertEqual(parallel["status"], "partial", parallel)
        statuses = {g["name"]: g["status"] for g in parallel["groups"]}
        self.assertEqual(statuses, {"group-1": "folded", "group-2": "failed"})
        self.assertEqual(parallel["fold"]["applied"], ["group-1"])
        self.assertEqual(parallel["build"]["failed_modules"], ["TauCeti.Three"])
        # The sequential agent ran afterwards on the task's worktree with the usual prompt, for what was left.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 3)
        last = calls[-1]
        self.assertEqual(last["cwd"], str(self.root / "state" / "worktrees" / "task-1"))
        self.assertNotIn(repair_parallel.GROUP_HEADING, last["prompt"])
        self.assertIn("linear chain on", last["prompt"])
        self.assertIn("has finished: exit 1, 2 errors", last["prompt"], "the streaming build's section says it ended")
        self.assertIn("`TauCeti/Three.lean:1:20`", last["prompt"])
        self.assertNotIn("TauCeti/Two.lean:1:20", last["prompt"], "the folded group's error is not reported again")
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        comparison = chain_tool.compare(
            self.content.bare, self.content.base, self.content.tip, self.content.base, pushed
        )
        by_pr = {m.source_pr: m for m in comparison.members}
        self.assertEqual(by_pr[f"{PROJECT}#2"].provenance, chain_tool.EDITED)
        self.assertEqual(by_pr[f"{PROJECT}#3"].provenance, chain_tool.EDITED)
        self.assertEqual(by_pr[f"{PROJECT}#1"].provenance, chain_tool.CLEAN)
        self.assertEqual(outcome["agent"][0]["parallel"], True)
        self.assertEqual(outcome["agent"][1]["status"], "ok")

    def test_fixups_outside_the_group_are_not_folded(self):
        os.environ["STUB_GROUP_STRAY"] = "TauCeti/Two.lean"  # group 1's agent also changes TauCeti/A.lean
        cfg = self.config(parallel_agents=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        parallel = outcome["parallel"]
        self.assertEqual(parallel["status"], "partial", parallel)
        statuses = {g["name"]: (g["status"], g["detail"]) for g in parallel["groups"]}
        self.assertEqual(statuses["group-2"], ("folded", None))
        self.assertEqual(statuses["group-1"][0], "unusable")
        self.assertIn("outside the group: TauCeti/A.lean", statuses["group-1"][1])
        self.assertEqual(len(self.agent_calls()), 3)
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertEqual(sh(self.content.bare, "show", f"{pushed}:TauCeti/A.lean"), "theorem a : True := trivial")

    def test_a_lost_claim_cancels_the_group_agents(self):
        os.environ["STUB_GROUP_SLEEP"] = "30"
        cfg = self.config(parallel_agents=2)

        def lose_the_claim_once_both_agents_run():
            deadline = time.monotonic() + 60
            while len(self.agent_calls()) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)
            self.service.renew_mode = "refuse"

        threading.Thread(target=lose_the_claim_once_both_agents_run, daemon=True).start()
        started = time.monotonic()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "claim_lost", outcome)
        self.assertLess(time.monotonic() - started, 25, "the sleeping group agents were stopped")
        self.assertEqual(outcome["parallel"]["status"], "cancelled")
        self.assertEqual([g["status"] for g in outcome["parallel"]["groups"]], ["cancelled", "cancelled"])
        self.assertEqual(len(self.agent_calls()), 2)
        self.assertEqual(self.service.results, [])
        self.assertFalse(self.group_worktree(1).exists())

    def test_the_build_lock_serializes_two_concurrent_scoped_builds(self):
        # The sandbox clears the environment, so the stub's sleep travels with the bwrap stub's extras.
        (BIN / "sandbox-env.json").write_text(
            json.dumps({"STUB_DIR": str(self.stub_dir), "STUB_LAKE_BUILD_SLEEP": "0.6"})
        )
        lock = self.root / "shared" / "build.lock"
        trees = []
        for number in (1, 2):
            tree = self.root / f"tree-{number}"
            sh(self.content.clone, "worktree", "add", "--quiet", "--detach", str(tree), self.content.tip)
            script = repair_tools.write_build_script(tree / repair_tools.TOOLS_DIR, lock=lock)
            self.assertEqual(script, tree / ".tauceti-repair" / "build")
            self.assertIn(f"LOCK = Path({str(lock)!r})", script.read_text())
            trees.append(tree)
        # A lock left by a builder that is gone is stale and taken over.
        lock.mkdir(parents=True)
        (lock / "pid").write_text("2147483000")
        procs = [
            subprocess.Popen([sys.executable, str(tree / ".tauceti-repair" / "build"), "TauCeti.One"], cwd=tree)
            for tree in trees
        ]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0, 0])
        builds = sorted(self.lake_builds(), key=lambda b: b["started"])
        self.assertEqual([b["args"] for b in builds], [["build", "--iofail", "TauCeti.One"]] * 2)
        self.assertEqual(sorted(b["cwd"] for b in builds), sorted(str(t) for t in trees))
        self.assertGreaterEqual(builds[1]["started"], builds[0]["finished"], "the second build waited for the first")
        self.assertFalse(lock.exists(), "the lock is removed when the build ends")
        # The worker's verification build holds the same directory through ``repair_tools.build_lock``.
        self.assertEqual(repair_parallel.lock_path(trees[0]), trees[0] / ".tauceti-repair" / "build.lock")
        with repair_tools.build_lock(trees[0]) as held:
            self.assertEqual(held, repair_parallel.lock_path(trees[0]))
            self.assertEqual(int((held / "pid").read_text()), os.getpid())
        self.assertFalse(repair_parallel.lock_path(trees[0]).exists())
        # The script a sequential agent gets keeps the worktree's own lock.
        own = repair_tools.write_build_script(self.root / "own")
        self.assertIn('LOCK = ROOT / ".tauceti-repair" / "build.lock"', own.read_text())

    def test_the_group_prompt_is_provider_neutral_and_dependent_failures_stay_sequential(self):
        clone = self.content.clone
        chain = repair.read_chain(clone, self.content.base, self.content.tip)
        task = repair_task.Task.from_json(
            {**self.content.task(), "claim_token": "token", "expires_at": time.time() + 60}
        )
        findings = repair_task.task_findings(task)
        tree = self.root / "tree"
        sh(clone, "worktree", "add", "--quiet", "--detach", str(tree), self.content.tip)
        groups, reason = repair_parallel.plan_groups(clone, chain, findings, tree)
        self.assertIsNone(reason)
        self.assertEqual(
            [(g.members, g.files, g.paths) for g in groups],
            [([2], ["TauCeti/Two.lean"], ["TauCeti/Two.lean"]), ([3], ["TauCeti/Three.lean"], ["TauCeti/Three.lean"])],
        )
        self.assertEqual([e["path"] for e in groups[1].findings["compiler_errors"]], ["TauCeti/Three.lean"])
        self.assertEqual([e["module"] for e in groups[1].findings["other"]], ["TauCeti/Three.lean"])
        context = repair_task.prompt_context(task, None, findings)
        for group in groups:
            prompt = repair_parallel.group_prompt(context, group, chain, clone, self.content.tip)
            lowered = prompt.lower()
            for word in self.PROVIDER_WORDS:
                self.assertNotIn(word, lowered, f"{word!r} in the group prompt")
            self.assertIn("`git commit --fixup=<sha>`", prompt)
            self.assertIn('`git commit --allow-empty --squash=<sha> -m "<explanation>"`', prompt)
            self.assertIn(f"python3 .tauceti-repair/build {repair.path_to_module(group.paths[0])}`", prompt)
            self.assertIn("Do not run `lake build` directly", prompt)
            self.assertIn(repair.HARD_RULES, prompt)
            self.assertNotIn("Already on main", prompt)
        # Three importing Two is one group: the import check reads the files at the tip.
        (tree / "TauCeti" / "Three.lean").write_text("import TauCeti.Two\ntheorem three : True := BROKEN\n")
        groups, reason = repair_parallel.plan_groups(clone, chain, findings, tree)
        self.assertEqual(groups, [])
        self.assertEqual(reason, "the failing modules depend on each other")
        # A failure in a module no member changes (a ripple into main) cannot be assigned.
        ripple = repair.findings_from_diagnostics(
            [
                {"kind": "compiler", "module": "TauCeti/A.lean", "message": "TauCeti/A.lean:1:1: unknown"},
                *task.diagnostics,
            ]
        )
        groups, reason = repair_parallel.plan_groups(clone, chain, ripple, tree)
        self.assertEqual((groups, reason), ([], "no member's commit changes TauCeti/A.lean"))
        # One member owning every failing module is sequential too.
        one = repair.findings_from_diagnostics([d for d in task.diagnostics if d["module"] == "TauCeti/Two.lean"])
        self.assertEqual(
            repair_parallel.plan_groups(clone, chain, one, tree), ([], "one member owns every failing module")
        )
        # The job file carries the configuration without its secrets and comes back typed.
        cfg = self.config(
            api_key="sk-secret",
            token="tok",
            seed_packages=self.root / "pkgs",
            publish=repair_publish.Settings(url="https://r.example", key_file=self.root / "key"),
        )
        record = json.loads(json.dumps(repair_parallel.config_to_json(cfg)))
        self.assertNotIn("sk-secret", json.dumps(record))
        self.assertNotIn("tok", record.values())
        revived = repair_parallel.config_from_json(record, repair_task.RunnerConfig)
        self.assertEqual(revived.seed_packages, self.root / "pkgs")
        self.assertEqual(revived.providers, ("claude",))
        self.assertEqual(revived.inference_delays, (0.0,))
        self.assertEqual(revived.publish, cfg.publish)
        self.assertIsNone(revived.api_key)

    def test_parallel_agents_disabled_keeps_the_sequential_path(self):
        cfg = self.config(parallel_agents=1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertNotIn("parallel", outcome)
        self.assertEqual(len(self.agent_calls()), 1)


class AdminCache(Fixture):
    """The worker's admin check: cached on disk, tolerant of a check that cannot be made."""

    def gh_calls(self) -> list[str]:
        path = self.stub_dir / "gh-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def admin_calls(self) -> list[str]:
        return [call for call in self.gh_calls() if f"repos/{PROJECT}" in call]

    def test_cache_hit_avoids_the_call(self):
        cfg = self.config()
        repair_task.AdminCheck(cfg).ensure()
        self.assertEqual(len(self.admin_calls()), 1)
        record = json.loads(repair_task.admin_check_path(cfg).read_text())
        self.assertEqual((record["login"], record["repository"]), ("stub-worker", PROJECT))
        self.assertLess(time.time() - record["verified_at"], 60)
        # A restarted worker within the window reads the record instead of asking GitHub.
        restarted = repair_task.AdminCheck(cfg)
        restarted.ensure()
        restarted.ensure()
        self.assertEqual(len(self.admin_calls()), 1)
        # A record for another repository is not this one's verification.
        self.assertIsNone(repair_task.AdminCheck(self.config(repository="Other/Repo")).verified_at)
        checks = {c["check"]: c for c in repair_task.preflight(cfg, min_free_gib=0)}
        self.assertTrue(checks["admin verification cache"]["ok"])
        self.assertRegex(checks["admin verification cache"]["detail"], r"stub-worker on .* verified \d+m ago \(current")

    def test_cache_expiry_rechecks(self):
        cfg = self.config()
        old = time.time() - cfg.admin_recheck_seconds - 60
        repair_task.admin_check_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        repair_task.admin_check_path(cfg).write_text(
            json.dumps({"login": "stub-worker", "repository": PROJECT, "verified_at": old})
        )
        detail = {c["check"]: c for c in repair_task.preflight(cfg, min_free_gib=0)}["admin verification cache"]
        self.assertIn("expired", detail["detail"])
        admin = repair_task.AdminCheck(cfg)
        self.assertEqual(admin.verified_at, old)
        before = len(self.admin_calls())  # the preflight's own live check
        admin.ensure()
        self.assertEqual(len(self.admin_calls()), before + 1)
        self.assertGreater(json.loads(repair_task.admin_check_path(cfg).read_text())["verified_at"], old)

    def test_transient_failure_proceeds(self):
        # A token of its own, so the transport's record of the exhausted quota stays in this test.
        exhausted = patch.dict(
            os.environ,
            {"GH_TOKEN": "exhausted-token", "STUB_GH_FAIL": "gh: API rate limit exceeded for user ID 477956"},
        )
        exhausted.start()
        self.addCleanup(exhausted.stop)
        cfg = self.config()
        admin = repair_task.AdminCheck(cfg)
        with patch.object(repair_task, "log") as logged:
            admin.ensure()  # no cached answer: the claim goes ahead and the service decides
        self.assertIn("local admin check skipped", logged.call_args.args[0])
        self.assertIn("claiming anyway", logged.call_args.args[0])
        self.assertFalse(repair_task.admin_check_path(cfg).exists())
        # The next poll does not spend the quota again before the retry interval.
        admin.ensure()
        self.assertEqual(len(self.admin_calls()), 1)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg), admin)
        self.assertEqual(outcome["status"], "adopted", outcome)
        # With an expired cached answer the worker keeps it.
        stale = repair_task.AdminCheck(cfg)
        stale.verified_at = time.time() - cfg.admin_recheck_seconds - 60
        with patch.object(repair_task, "log") as logged:
            stale.ensure()
        self.assertIn("keeping the verification from", logged.call_args.args[0])

    def test_negative_answer_stops(self):
        from tauceti_worker.config import Die

        cfg = self.config()
        repair_task.AdminCheck(cfg).ensure()
        self.assertTrue(repair_task.admin_check_path(cfg).exists())
        os.environ["STUB_GH_ADMIN"] = "false"
        admin = repair_task.AdminCheck(cfg)
        admin.verified_at = 0.0  # expired, so the next pass asks again
        with self.assertRaises(Die):
            repair_task.claim_and_run(cfg, self.client(cfg), admin)
        self.assertEqual(self.service.claim_attempts, [])
        self.assertFalse(repair_task.admin_check_path(cfg).exists(), "a negative answer drops the cache")
        # A repository the account cannot see is a negative answer too, not a transient failure.
        os.environ.pop("STUB_GH_ADMIN")
        os.environ["STUB_GH_FAIL"] = "gh: Not Found (HTTP 404)"
        with self.assertRaises(Die):
            repair_task.AdminCheck(cfg).ensure()


class Preflight(Fixture):
    def test_checks_name_their_sources_and_never_print_the_token(self):
        claude_home = HOME / ".claude"
        claude_home.mkdir(exist_ok=True)
        (claude_home / ".credentials.json").write_text("{}")
        self.addCleanup(lambda: (claude_home / ".credentials.json").unlink(missing_ok=True))
        cfg = self.config()
        checks = repair_task.preflight(cfg, min_free_gib=0)
        by_name = {c["check"]: c for c in checks}
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertEqual(by_name["github token"]["detail"], "from $TAUCETI_QUEUE_TOKEN")
        self.assertEqual(by_name["repository admin"]["detail"], f"stub-worker on {PROJECT}")
        self.assertIn("revision fakefakefak", by_name["service"]["detail"])
        self.assertIn(str(self.content.bare), by_name["content clone"]["detail"])
        self.assertNotIn(TOKEN, json.dumps(checks))
        # An unreachable service and a non-admin account are reported as failed checks, not raised.
        os.environ["STUB_GH_ADMIN"] = "false"
        self.addCleanup(os.environ.pop, "STUB_GH_ADMIN", None)
        checks = repair_task.preflight(self.config(service="http://127.0.0.1:9/"), min_free_gib=0)
        by_name = {c["check"]: c for c in checks}
        self.assertFalse(by_name["service"]["ok"])
        self.assertFalse(by_name["repository admin"]["ok"])
        self.assertTrue(by_name["tool lake"]["ok"])

    def test_token_file_is_read_and_named(self):
        token_file = self.root / "token"
        token_file.write_text("file-token\n")
        os.environ["TAUCETI_QUEUE_TOKEN_FILE"] = str(token_file)
        self.addCleanup(os.environ.pop, "TAUCETI_QUEUE_TOKEN_FILE", None)
        self.assertEqual(repair_task.worker_token(), "file-token")
        self.assertEqual(repair_task.token_source(), f"file {token_file}")
        token_file.write_text("")
        from tauceti_worker.config import Die

        with self.assertRaises(Die):
            repair_task.worker_token()

    def test_no_silent_fallback_to_a_personal_token(self):
        from tauceti_worker.config import Die

        personal = patch.dict(os.environ, {"GH_TOKEN": "personal", "GITHUB_TOKEN": "personal"})
        personal.start()
        self.addCleanup(personal.stop)
        for key in ("TAUCETI_QUEUE_TOKEN_FILE", "TAUCETI_QUEUE_TOKEN"):
            os.environ.pop(key, None)
        with patch("subprocess.run", side_effect=AssertionError("gh auth token")):
            with self.assertRaises(Die) as refused:
                repair_task.worker_token()
        self.assertIn("TAUCETI_QUEUE_TOKEN_FILE", str(refused.exception))
        self.assertEqual(repair_task.token_source(), "none configured")
        checks = {c["check"]: c for c in repair_task.preflight(self.config(token=None), min_free_gib=0)}
        self.assertFalse(checks["github token"]["ok"])
        self.assertFalse(checks["repository admin"]["ok"], "the operator's gh login is not checked instead")

    def test_the_worker_token_is_the_one_its_own_gh_calls_use(self):
        token_file = self.root / "token"
        token_file.write_text("dedicated\n")
        environment = patch.dict(os.environ, {"TAUCETI_QUEUE_TOKEN_FILE": str(token_file), "GITHUB_TOKEN": "personal"})
        environment.start()
        self.addCleanup(environment.stop)
        repair_task.make_client(self.config(token=None))
        self.assertEqual(os.environ["GH_TOKEN"], "dedicated")
        self.assertNotIn("GITHUB_TOKEN", os.environ)


class WorkerAsTheQueueApp(Fixture):
    """``TAUCETI_QUEUE_GITHUB_APP``: the worker mints App installation tokens and a local-worker bearer."""

    PEM = None

    def app(self, permissions=None):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from tauceti_queue import github_app

        if WorkerAsTheQueueApp.PEM is None:
            WorkerAsTheQueueApp.PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        credentials = github_app.AppCredentials(
            app_id=5607007,
            slug="tauceti-queue",
            installation_id=178451644,
            private_key_pem=self.PEM,
            webhook_secret=b"",
        )
        clock = [1_800_000_000.0]
        minted = []

        def transport(method, url, headers, body):
            minted.append(json.loads(body))
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock[0] + 3600))
            granted = permissions or {"contents": "write", "workflows": "write", "metadata": "read"}
            payload = {"token": f"ghs_installation{len(minted)}", "expires_at": expires, "permissions": granted}
            return 201, {}, json.dumps(payload).encode()

        app = repair_task.WorkerApp(credentials, transport, clock=lambda: clock[0])
        environment = patch.dict(
            os.environ, {"TAUCETI_QUEUE_GITHUB_APP": str(self.root / "app"), "GITHUB_TOKEN": "personal"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("TAUCETI_QUEUE_TOKEN", "TAUCETI_QUEUE_TOKEN_FILE"):
            os.environ.pop(key, None)
        installed = patch.object(repair_task, "_worker_app", app)
        installed.start()
        self.addCleanup(installed.stop)
        return app, credentials, clock, minted

    def test_the_service_bearer_is_a_fresh_local_worker_bearer(self):
        from tauceti_queue import github_app

        app, credentials, clock, minted = self.app()
        client = repair_task.make_client(self.config(token=None))
        first = client.token()
        self.assertTrue(github_app.verify_local_worker_bearer(credentials, first, clock[0]))
        clock[0] += 600
        self.assertTrue(github_app.verify_local_worker_bearer(credentials, client.token(), clock[0]))
        self.assertFalse(github_app.verify_local_worker_bearer(credentials, first, clock[0]), "an old one expires")
        self.assertEqual(minted, [], "the service bearer spends no GitHub request")
        self.assertEqual(repair_task.admin_login(), "tauceti-queue[bot]")

    def test_gh_and_git_get_a_current_installation_token_with_narrowed_permissions(self):
        app, _, clock, minted = self.app()
        self.assertIsNone(repair_task.refresh_github_token())
        self.assertEqual(os.environ["GH_TOKEN"], "ghs_installation1")
        self.assertNotIn("GITHUB_TOKEN", os.environ)
        self.assertEqual(minted, [{"permissions": repair_task.WORKER_APP_PERMISSIONS}])
        repair_task.verify_admin(PROJECT)
        self.assertEqual(len(minted), 1, "the token is reused while it is fresh")
        clock[0] += 3600 - 60
        repair_task.verify_admin(PROJECT)
        self.assertEqual(os.environ["GH_TOKEN"], "ghs_installation2", "renewed before it expires")
        checks = {c["check"]: c for c in repair_task.preflight(self.config(token=None), min_free_gib=0)}
        self.assertTrue(checks["github token"]["ok"], checks["github token"])
        self.assertEqual(checks["repository access"]["detail"], f"tauceti-queue[bot] with contents write on {PROJECT}")
        self.assertNotIn("ghs_installation", json.dumps(checks))

    def test_an_installation_without_contents_write_stops_the_worker(self):
        from tauceti_worker.config import Die

        self.app(permissions={"contents": "read", "metadata": "read"})
        with self.assertRaises(Die):
            repair_task.verify_admin(PROJECT)

    def test_an_unmintable_token_defers_rather_than_using_another(self):
        from tauceti_queue import github_app
        from tauceti_worker.config import NoProgress

        app, *_ = self.app()
        os.environ["GH_TOKEN"] = "personal"
        with patch.object(app.tokens, "token", side_effect=github_app.CredentialsError("HTTP 502", retryable=True)):
            with self.assertRaises(NoProgress):
                repair_task.verify_admin(PROJECT)
        self.assertEqual(os.environ["GH_TOKEN"], "personal", "unchanged; the push path then refuses to push")

    def test_loading_the_app_hides_the_hosts_gh_login(self):
        from tauceti_queue import github_app

        self.app()
        directory = self.root / "app"
        directory.mkdir()
        (directory / "github-app.json").write_text(json.dumps({"id": 1, "installation_id": 2, "slug": "tauceti-queue"}))
        (directory / "github-private-key.pem").write_bytes(self.PEM)
        os.environ.update(GH_TOKEN="personal", GH_CONFIG_DIR=str(HOME / ".config" / "gh"))
        with patch.object(repair_task, "_worker_app", None):
            app = repair_task.worker_app()
            self.assertIsInstance(app.credentials, github_app.AppCredentials)
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertEqual(list(Path(os.environ["GH_CONFIG_DIR"]).iterdir()), [])

    def test_git_asks_only_gh_for_github_credentials(self):
        personal = self.root / "personal-gitconfig"
        personal.write_text(
            '[credential "https://github.com"]\n\thelper = !f() { echo username=me; echo password=personal; }; f\n'
            "[http]\n\textraHeader = Authorization: Basic personal\n[user]\n\tname = Kept\n"
        )
        environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(personal)})
        environment.start()
        self.addCleanup(environment.stop)
        repair_task.pin_app_credentials()

        # Outside any repository: a CI checkout's own config carries actions/checkout's extraHeader.
        def config(*args):
            result = subprocess.run(
                ["git", "config", *args], cwd=self.root, capture_output=True, text=True, check=False
            )
            return result.stdout.splitlines()

        self.assertEqual(config("--get-all", "user.name"), ["Kept"], "the user's own config still applies")
        self.assertEqual(config("--get-urlmatch", "http.extraHeader", "https://github.com/"), [""])
        helpers = config("--get-urlmatch", "credential.helper", "https://github.com")
        self.assertTrue(helpers[-1].endswith(" auth git-credential"), helpers)
        filled = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=self.root,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"},
            check=False,
        )
        self.assertNotIn("personal", filled.stdout)

    def test_a_repositorys_own_github_headers_and_helpers_do_not_apply(self):
        personal = self.root / "personal-gitconfig"
        personal.write_text("[http]\n\textraHeader = Authorization: Basic personal\n")
        remote = f"https://github.com/{PROJECT}"
        clone = self.root / "credential-clone"
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        for key, value in (
            ("http.https://github.com/.extraheader", "AUTHORIZATION: basic local"),  # as actions/checkout leaves it
            (f"http.{remote}.extraheader", "AUTHORIZATION: basic repository"),
            ("credential.https://github.com.helper", "store"),
        ):
            subprocess.run(["git", "-C", str(clone), "config", key, value], check=True)
        environment = patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(personal), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "0"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        pins = patch.object(repair_task, "_app_git_config", ())
        pins.start()
        self.addCleanup(pins.stop)
        repair_task.pin_app_credentials()

        def config(*args, env=None):
            result = subprocess.run(
                ["git", "-C", str(clone), "config", *args], capture_output=True, text=True, env=env, check=False
            )
            return result.stdout.splitlines()

        # push_result drops the inherited command-scope config and passes only the App's pins.
        pushing = {k: v for k, v in os.environ.items() if k == "GIT_CONFIG_GLOBAL" or not k.startswith("GIT_CONFIG_")}
        pushing.update(repair_task.app_git_environment(remote))
        for env in (None, pushing):
            self.assertEqual(config("--get-urlmatch", "http.extraHeader", "https://github.com/", env=env), [""])
            helpers = config("--get-urlmatch", "credential.helper", "https://github.com", env=env)
            self.assertTrue(helpers[-1].endswith(" auth git-credential"), helpers)
        self.assertEqual(config("--get-urlmatch", "http.extraHeader", remote, env=pushing), [""])
        # Agents' isolation replaces the command-scope pins; the global config still resets the host's own.
        agent = {**os.environ, **{k: v for k, v in repair.isolation_env(self.root / "isolation", {}).items() if v}}
        self.assertEqual(
            config("--global", "--get-urlmatch", "http.extraHeader", "https://github.com/", env=agent), [""]
        )
        helpers = config("--global", "--get-urlmatch", "credential.helper", "https://github.com", env=agent)
        self.assertTrue(helpers[-1].endswith(" auth git-credential"), helpers)

    def assert_worktree_hooks_do_not_run(self, cfg_token):
        """Hooks an agent leaves in its worktree, in the shared hooks directory or through the worktree's
        own ``core.hooksPath``, run neither in the worker's ordinary git calls nor in its pushes."""
        sh(self.content.clone, "config", "extensions.worktreeConfig", "true")
        common = Path(sh(self.content.clone, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        unpinned = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
        for index, location in enumerate(("shared", "worktree config")):
            with self.subTest(location=location):
                worktree = self.root / f"agent-worktree-{index}"
                sh(self.content.clone, "worktree", "add", "-q", "--detach", str(worktree), self.content.base)
                hooks = common / "hooks"
                if location == "worktree config":
                    hooks = worktree / "agent-hooks"
                    sh(worktree, "config", "--worktree", "core.hooksPath", str(hooks))
                hooks.mkdir(exist_ok=True)
                leaked = self.root / f"leaked-token-{index}"
                for name in ("pre-push", "reference-transaction", "post-checkout", "fsmonitor"):
                    hook = hooks / name
                    hook.write_text(f'#!/bin/sh\necho "{name}: $GH_TOKEN" >> {shlex.quote(str(leaked))}\n')
                    hook.chmod(0o755)
                probe = ["git", "-C", str(worktree), "update-ref", f"refs/probe/{index}", self.content.tip]
                subprocess.run(probe, env=unpinned, check=True)
                sh(worktree, "config", "--worktree", "core.fsmonitor", str(hooks / "fsmonitor"))
                self.assertTrue(leaked.exists(), "the hooks are live without the worker's pins")
                leaked.unlink()
                # The worker's ordinary calls inherit its environment.
                sh(worktree, "checkout", "-q", "--detach", self.content.tip)
                sh(worktree, "update-ref", f"refs/worker/{index}", self.content.tip)
                sh(worktree, "status", "--porcelain")
                task = types.SimpleNamespace(
                    task_id=f"7{index}",
                    repository=PROJECT,
                    submission_ref=f"refs/tauceti-queue/submissions/7{index}/1",
                    tip=self.content.tip,
                )
                cfg = self.config(token=cfg_token, clone=worktree)
                with patch.object(repair_task, "require_task_execution"):
                    self.assertEqual(repair_task.check_submission_access(cfg, task, self.root)["rc"], 0)
                    record = repair_task.push_result(cfg, task, worktree, self.content.tip, self.root, None)
                self.assertTrue(record["ok"], record)
                self.assertEqual(sh(self.content.bare, "rev-parse", task.submission_ref), self.content.tip)
                self.assertFalse(leaked.exists(), leaked.read_text() if leaked.exists() else "")
                for name in ("pre-push", "reference-transaction", "post-checkout", "fsmonitor"):
                    (hooks / name).unlink()

    def test_hooks_in_the_agents_worktree_do_not_run_with_the_app_token(self):
        self.app()
        pins = patch.object(repair_task, "_app_git_config", ())
        pins.start()
        self.addCleanup(pins.stop)
        environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull})
        environment.start()
        self.addCleanup(environment.stop)
        repair_task.pin_app_credentials()
        self.assertIsNone(repair_task.refresh_github_token())
        self.assert_worktree_hooks_do_not_run(None)

    def test_hooks_in_the_agents_worktree_do_not_run_with_the_worker_token(self):
        environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TAUCETI_QUEUE_GITHUB_APP", None)
        repair_task.use_worker_token("worker-token")
        repair_task.use_worker_token("worker-token")
        self.assertEqual(os.environ["GIT_CONFIG_COUNT"], str(len(repair_task._NO_GIT_HOOKS)), "added once")
        self.assert_worktree_hooks_do_not_run(TOKEN)

    def test_a_push_without_a_mintable_token_does_not_run(self):
        from tauceti_queue import github_app

        app, *_ = self.app()
        task = types.SimpleNamespace(
            task_id="7", repository=PROJECT, submission_ref="refs/tauceti-queue/submissions/7/1"
        )
        with (
            patch.object(app.tokens, "token", side_effect=github_app.CredentialsError("HTTP 502", retryable=True)),
            patch.object(repair_task, "require_task_execution"),
            patch.object(repair, "run_logged", side_effect=AssertionError("pushed")),
        ):
            record = repair_task.push_result(self.config(token=None), task, self.root, "a" * 40, self.root, None)
            self.assertEqual((record["ok"], record["rc"]), (False, 75))
            self.assertEqual(repair_task.check_submission_access(self.config(token=None), task, self.root)["rc"], 75)

    def test_the_app_and_a_worker_token_together_are_refused(self):
        from tauceti_worker.config import Die

        self.app()
        os.environ["TAUCETI_QUEUE_TOKEN"] = "worker-token"
        with patch.object(repair_task, "_worker_app", None), self.assertRaises(Die):
            repair_task.worker_app()

    def test_agents_inherit_neither_the_app_directory_nor_a_token(self):
        self.app()
        changes = repair.isolation_env(self.root / "isolation", {})
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "TAUCETI_QUEUE_GITHUB_APP", "TAUCETI_QUEUE_CONFIG"):
            self.assertIsNone(changes[key], key)


def pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


class SandboxedBuilds(Fixture):
    def worktree(self) -> tuple[Path, Path, dict]:
        worktree = self.root / "worktree"
        seed = self.root / "seed-packages"
        (seed / "mathlib").mkdir(parents=True)
        (worktree / ".lake").mkdir(parents=True)
        (worktree / ".lake" / "packages").symlink_to(seed)
        (HOME / ".elan" / "bin").mkdir(parents=True, exist_ok=True)
        env = repair.lake_env(self.root / "state")
        return worktree, seed, env

    def test_the_wrapper_composes_ci_sandbox(self):
        worktree, seed, env = self.worktree()
        argv = repair_sandbox.sandboxed_lake(["lake", "build", "--iofail"], worktree, env)
        self.assertEqual(argv[1:3], ["-i", str(BIN / "bwrap")], "bwrap starts from an empty environment")
        self.assertEqual(argv[argv.index("--") :], ["--", "lake", "build", "--iofail"])
        for flag in ("--unshare-net", "--unshare-user", "--unshare-pid", "--disable-userns", "--die-with-parent"):
            self.assertIn(flag, argv)
        read_only, writable = pairs(argv, "--ro-bind"), pairs(argv, "--bind")
        self.assertIn((str(worktree), str(worktree)), read_only)
        self.assertIn((str(HOME / ".elan"), str(HOME / ".elan")), read_only)
        self.assertIn((str(seed), str(seed)), read_only, "the shared Mathlib packages are read-only")
        self.assertIn((env["LAKE_CONFIG"], env["LAKE_CONFIG"]), read_only)
        self.assertIn((str(REPO / "scripts/tauceti-lean"), repair_sandbox.PRESSURE_WRAPPER), read_only)
        self.assertIn(("/sys/fs/cgroup", "/sys/fs/cgroup"), read_only)
        self.assertNotIn("--unshare-cgroup", argv, "the guard must see the caller's capped ancestors")
        self.assertEqual(sorted(writable), sorted([(str(worktree / ".lake"),) * 2, (env["LAKE_CACHE_DIR"],) * 2]))
        self.assertNotIn(str(HOME), [source for source, _ in read_only + writable])
        self.assertIn(("/tmp",), [(argv[i + 1],) for i, a in enumerate(argv) if a == "--tmpfs"])
        self.assertLess(argv.index("--ro-bind"), argv.index("--remount-ro"))
        setenv = dict(pairs(argv, "--setenv"))
        self.assertEqual(setenv["LAKE_NO_CACHE"], "true")
        self.assertEqual(setenv["LAKE_CACHE_DIR"], env["LAKE_CACHE_DIR"])
        self.assertEqual(setenv["LAKE_OVERRIDE_LEAN"], "true")
        self.assertEqual(setenv["LEAN"], repair_sandbox.PRESSURE_WRAPPER)
        self.assertEqual(setenv["XDG_CACHE_HOME"], str(worktree / ".lake/pressure-cache"))
        self.assertTrue(setenv["PATH"].startswith(f"{HOME / '.elan' / 'bin'}:"))
        self.assertNotIn(str(BIN), setenv["PATH"], "nothing writable or test-local is on the sandbox PATH")
        self.assertNotIn("GH_TOKEN", setenv)
        self.assertEqual(argv[argv.index("--chdir") + 1], str(worktree))
        # Run through the recording bwrap: the inner command sees only what was set.
        run = subprocess.run(
            repair_sandbox.sandboxed_lake(["sh", "-c", "pwd; echo ${GH_TOKEN:-none}"], worktree, env),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.stdout.split(), [str(worktree), "none"], run.stderr)
        self.assertFalse({"GH_TOKEN", "HOME", "PATH", "STUB_DIR"} & set(self.bwrap_calls()[-1]["environ"]))

    def test_the_agents_build_script_runs_lake_in_the_sandbox(self):
        worktree, _, env = self.worktree()
        (worktree / "TauCeti").mkdir()
        (worktree / "TauCeti" / "A.lean").write_text("theorem a : True := trivial\n")
        script = repair_tools.write_build_script(worktree / ".tauceti-repair")
        run = subprocess.run([str(script), "TauCeti/A.lean"], cwd=worktree, env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        call = self.bwrap_calls()[-1]["argv"]
        self.assertEqual(call[call.index("--") :], ["--", "lake", "build", "--iofail", "TauCeti.A"])
        self.assertIn("--unshare-net", call)
        self.assertIn((str(worktree), str(worktree)), pairs(call, "--ro-bind"))
        self.assertEqual(self.lake_calls(), ["build --iofail TauCeti.A"])

    def test_background_builds_run_at_the_lowest_cpu_priority(self):
        worktree, _, env = self.worktree()
        command = ["sh", "-c", "nice"]
        self.assertEqual(repair_sandbox.sandboxed_lake(command, worktree, env)[1:3], ["-i", str(BIN / "bwrap")])
        os.environ[repair_sandbox.BACKGROUND_VARIABLE] = "1"
        argv = repair_sandbox.sandboxed_lake(command, worktree, env)
        self.assertEqual(argv[:3], [shutil.which("nice"), "-n", "19"])
        run = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(run.stdout.split(), ["19"], run.stderr)
        os.environ[repair_sandbox.DISABLE_VARIABLE] = "0"
        with self.assertLogs(repair_sandbox.LOGGER, "WARNING"):
            self.assertEqual(repair_sandbox.sandboxed_lake(command, worktree, env)[:3], argv[:3])

    def test_disabled_falls_back_with_a_logged_warning(self):
        worktree, _, env = self.worktree()
        os.environ[repair_sandbox.DISABLE_VARIABLE] = "0"
        with self.assertLogs(repair_sandbox.LOGGER, "WARNING") as logs:
            argv = repair_sandbox.sandboxed_lake(["lake", "build", "--iofail"], worktree, env)
        self.assertEqual(argv[-3:], ["lake", "build", "--iofail"])
        self.assertIn("LEAN_SYSROOT", argv)
        self.assertIn(f"LEAN={REPO / 'scripts/tauceti-lean'}", argv)
        self.assertIn("LAKE_OVERRIDE_LEAN=true", argv)
        run = subprocess.run(
            repair_sandbox.sandboxed_lake(
                ["sh", "-c", 'test -z "${LEAN_SYSROOT:-}" && test "$LAKE_OVERRIDE_LEAN" = true && test -f "$LEAN"'],
                worktree,
                env,
            ),
            cwd=worktree,
            env={**env, "LEAN_SYSROOT": "/incorrect/inherited/sysroot"},
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("WITHOUT the bwrap sandbox", logs.output[0])
        ok, detail = repair_sandbox.check(self.root)
        self.assertTrue(ok)
        self.assertIn("DISABLED", detail)

    def test_missing_bwrap_fails_closed(self):
        worktree, _, env = self.worktree()
        which = repair_sandbox.shutil.which
        with patch.object(repair_sandbox.shutil, "which", lambda name: None if name == "bwrap" else which(name)):
            with self.assertRaises(repair_sandbox.SandboxUnavailable):
                repair_sandbox.sandboxed_lake(["lake", "build"], worktree, env)
            ok, detail = repair_sandbox.check(self.root)
        self.assertFalse(ok)
        self.assertIn(repair_sandbox.DISABLE_VARIABLE, detail)

    def test_preflight_runs_true_in_the_sandbox(self):
        by_name = {c["check"]: c for c in repair_task.preflight(self.config(token=None), min_free_gib=0)}
        self.assertTrue(by_name["build sandbox"]["ok"], by_name["build sandbox"])
        self.assertIn(str(BIN / "bwrap"), by_name["build sandbox"]["detail"])
        self.assertEqual(self.bwrap_calls()[-1]["argv"][-2:], ["--", "true"])
        (BIN / "sandbox-env.json").write_text("{}")  # the stub now fails, as a broken bwrap would
        by_name = {c["check"]: c for c in repair_task.preflight(self.config(token=None), min_free_gib=0)}
        self.assertFalse(by_name["build sandbox"]["ok"])


class StreamRepair(Fixture):
    """The worker's background build streams its errors to the agent (``repair_stream``)."""

    SCRIPT = """\
✖ [2/3] Building TauCeti.Two
error: TauCeti/Two.lean:1:20: unknown identifier 'BROKEN'
  the batch-mate renamed it
sleep 1.5
✖ [3/3] Building TauCeti.Three
error: TauCeti/Three.lean:1:1: unsolved goals
⊢ True
sleep 0.8
Some required targets logged failures:
- TauCeti.Two
- TauCeti.Three
exit 1
"""

    def script(self, text: str = SCRIPT) -> None:
        path = self.root / "build-script.txt"
        path.write_text(text)
        os.environ["STUB_LAKE_BUILD_SCRIPT"] = str(path)
        # The sandbox starts from an empty environment; the bwrap stub passes these to the lake stub.
        (BIN / "sandbox-env.json").write_text(
            json.dumps({"STUB_DIR": str(self.stub_dir), "STUB_LAKE_BUILD_SCRIPT": str(path)})
        )

    def repair_dir(self, task_id: int = 1) -> Path:
        return self.root / "state" / "worktrees" / f"task-{task_id}" / ".tauceti-repair"

    def test_the_agent_is_launched_at_the_first_error_and_the_lock_is_released_at_the_end(self):
        # Hold the producer until the helper has observed its live lock, independent of startup speed.
        (self.stub_dir / "stream-handshake").touch()
        self.script(self.SCRIPT.replace("sleep 0.8", "wait_file stream-helper-waiting"))
        os.environ["STUB_AGENT_MODE"] = "stream"
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        # No prepare build: the streaming build is the one whole-library build before the agent.
        lake = self.lake_calls()
        self.assertEqual(lake[:3], ["exe cache get", lake[1], "build --iofail"])
        self.assertIn("cache get", lake[1])
        self.assertEqual(lake.count("build --iofail"), 2, "the streaming build and the worker's verification")
        self.assertFalse(outcome["artifacts"]["incremental_build"])
        # The agent was launched after the first error, while the build still ran.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        launch = calls[0]["launch"]
        self.assertTrue(launch["lock"], "the lock exists while the build runs")
        self.assertTrue(launch["build_helper"], ".tauceti-repair/build is executable")
        self.assertEqual(len(launch["errors"]), 1, launch)
        first = launch["errors"][0]
        self.assertEqual(
            {k: first[k] for k in ("module", "file", "line", "column", "member")},
            {"module": "TauCeti.Two", "file": "TauCeti/Two.lean", "line": 1, "column": 20, "member": f"{PROJECT}#2"},
        )
        self.assertEqual(first["message"], "unknown identifier 'BROKEN'\n  the batch-mate renamed it")
        self.assertEqual(first["commit"], self.content.members[1]["commit"])
        prompt = calls[0]["prompt"]
        self.assertIn("## Build in progress", prompt)
        self.assertLess(prompt.index("## Build in progress"), prompt.index("## Verifying locally"))
        self.assertIn("running in the background", prompt)
        self.assertIn("reported 1 error so far", prompt)
        self.assertIn(".tauceti-repair/build.log", prompt)
        self.assertIn(".tauceti-repair/errors.jsonl", prompt)
        self.assertIn("do not start your own `lake build` while `.tauceti-repair/build.lock` exists", prompt)
        self.assertIn("`.tauceti-repair/build <Module> ...` waits for the lock", prompt)
        self.assertIn("`--dependents` adds the modules importing them", prompt)
        self.assertIn("build finished, exit N, M errors total", prompt)
        self.assertIn("the worker completes its assigned verification before submission", prompt)
        # The scoped build helper waited for the lock, ran lake for the named module and returned its status.
        agent = json.loads((self.stub_dir / "stream-agent.json").read_text())
        self.assertEqual(agent["helper_rc"], 0, agent)
        self.assertIn("waiting for .tauceti-repair/build.lock", agent["helper_out"])
        self.assertIn("build --iofail TauCeti.Two", lake)
        self.assertGreater(lake.index("build --iofail TauCeti.Two"), lake.index("build --iofail"))
        self.assertFalse(agent["lock_after"])
        # The error list grew to both errors and ends with the finished line; the lock is gone.
        self.assertEqual([e.get("module") for e in agent["errors_after"]], ["TauCeti.Two", "TauCeti.Three", None])
        self.assertEqual(agent["errors_after"][1]["message"], "unsolved goals\n⊢ True")
        self.assertEqual(agent["errors_after"][1]["member"], f"{PROJECT}#3")
        finished = agent["errors_after"][2]
        self.assertEqual(finished["event"], "build_finished")
        self.assertEqual((finished["exit"], finished["errors"]), (1, 2))
        self.assertEqual(finished["message"], "build finished, exit 1, 2 errors total")
        self.assertFalse((self.repair_dir() / "build.lock").exists())
        self.assertEqual(
            (self.repair_dir() / "errors.jsonl").read_text().splitlines()[-1], json.dumps(finished), "nothing after"
        )
        log = (self.repair_dir() / "build.log").read_text()
        self.assertTrue(log.startswith("$ lake build --iofail\n"))
        self.assertIn("- TauCeti.Three", log)
        self.assertEqual((self.root / "state" / "tasks" / "1" / "logs" / "stream-build.log").read_text(), log)
        self.assertEqual(outcome["stream_build"]["rc"], 1)
        self.assertEqual(outcome["stream_build"]["errors"], 2)
        self.assertEqual(outcome["stream_build"]["failed_modules"], ["TauCeti.Two", "TauCeti.Three"])

    def test_a_build_without_errors_launches_the_agent_when_it_ends(self):
        self.script("✔ [1/1] Built TauCeti.Two (0.1s)\nexit 0\n")
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(
            calls[0]["launch"]["errors"],
            [
                {
                    "event": "build_finished",
                    "exit": 0,
                    "errors": 0,
                    "timed_out": False,
                    "message": "build finished, exit 0, 0 errors total",
                }
            ],
        )
        self.assertFalse(calls[0]["launch"]["lock"])
        self.assertIn("has finished: exit 0, 0 errors.", calls[0]["prompt"])

    def test_no_stream_repair_restores_the_sequential_flow(self):
        cfg = self.config(stream_repair=False, keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        lake = self.lake_calls()
        self.assertEqual(lake[2], "build --iofail")
        self.assertEqual(outcome["artifacts"]["incremental_build"]["failed_modules"], ["TauCeti.Two"])
        self.assertNotIn("stream_build", outcome)
        calls = self.agent_calls()
        self.assertNotIn("## Build in progress", calls[0]["prompt"])
        self.assertIn("The worker owns final verification", calls[0]["prompt"])
        self.assertIsNone(calls[0]["launch"]["errors"])
        self.assertFalse(calls[0]["launch"]["lock"])
        self.assertFalse((self.repair_dir() / "build.log").exists())

    def test_the_flag_and_the_environment_select_the_flow(self):
        self.assertTrue(repair_task.stream_repair_enabled(environ={}))
        self.assertTrue(repair_task.stream_repair_enabled(environ={"TAUCETI_QUEUE_STREAM_REPAIR": "1"}))
        self.assertFalse(repair_task.stream_repair_enabled(environ={"TAUCETI_QUEUE_STREAM_REPAIR": "0"}))
        self.assertFalse(repair_task.stream_repair_enabled(environ={"TAUCETI_QUEUE_STREAM_REPAIR": "false"}))
        self.assertFalse(repair_task.stream_repair_enabled(True, environ={}))
        seen: list[repair_task.RunnerConfig] = []
        with patch.object(repair_task, "preflight", lambda cfg, **kw: seen.append(cfg) or []):
            common = [
                "--check",
                "--service",
                self.service.url,
                "--clone",
                str(self.content.clone),
                "--root",
                str(self.root / "s"),
            ]
            repair_task.main(common)
            repair_task.main(["--no-stream-repair", *common])
        self.assertEqual([cfg.stream_repair for cfg in seen], [True, False])

    def prepare_task(self) -> dict:
        """A ``prepare`` task: an open batch on its own base, not yet tried, so no diagnostics."""
        raw = self.content.task(kind="prepare")
        raw["diagnostics"] = []
        raw["extra"] = {**raw["extra"], "renew_after": 0.2}
        self.service.tasks = {1: raw}
        return raw

    def test_a_prepare_task_whose_batch_builds_is_released_as_prepared_without_an_agent(self):
        self.prepare_task()
        self.script("✔ [1/3] Built TauCeti.One (0.1s)\n✔ [2/3] Built TauCeti.Two (0.1s)\nexit 0\n")
        cfg = self.config(stream_repair=False)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual((outcome["status"], outcome["reason"]), ("released", "prepared_clean"), outcome)
        self.assertEqual(self.agent_calls(), [], "a batch that builds needs no agent")
        self.assertEqual([r["reason"] for r in self.service.releases], ["prepared_clean"])
        self.assertEqual(
            self.lake_calls().count("build --iofail"), 1, "a prepare task streams even when repair does not"
        )

    def test_a_prepare_task_repairs_what_fails_with_the_not_yet_tried_prompt(self):
        self.prepare_task()
        self.script()
        os.environ["STUB_AGENT_MODE"] = "stream"
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn(
            "This batch has not yet been tried; fix what fails to build so it is ready when it becomes the head; "
            "nothing you do is a verdict.",
            prompt,
        )
        self.assertNotIn("with CI (`lake build --iofail`, as the staging build runs it) and the build failed", prompt)
        self.assertIn("## Build in progress", prompt)
        self.assertEqual(outcome["stream_build"]["failed_modules"], ["TauCeti.Two", "TauCeti.Three"])

    def test_a_cancelled_wait_stops_the_build_and_releases_the_lock(self):
        self.script("✔ [1/2] Built TauCeti.One (0.1s)\nsleep 30\nexit 0\n")
        worktree = self.root / "wt"
        repair.create_worktree(self.content.clone, self.content.tip, worktree)
        chain = repair.read_chain(self.content.clone, self.content.base, self.content.tip)
        stream = repair_stream.start_build(
            worktree, worktree / ".tauceti-repair", dict(os.environ), chain=chain, repository=PROJECT, timeout=60
        )
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        self.assertFalse(stream.wait_for_agent(cancel, poll=0.1))
        self.assertTrue((worktree / ".tauceti-repair" / "build.lock").exists())
        stream.stop()
        self.assertTrue(stream.finished.is_set())
        self.assertFalse((worktree / ".tauceti-repair" / "build.lock").exists())
        self.assertEqual(stream.errors, [])
        last = json.loads((worktree / ".tauceti-repair" / "errors.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["event"], "build_finished")
        self.assertEqual(last["errors"], 0)

    def test_a_module_edited_during_the_build_is_a_rerun_not_an_error(self):
        worktree = self.root / "wt"
        repair.create_worktree(self.content.clone, self.content.tip, worktree)
        chain = repair.read_chain(self.content.clone, self.content.base, self.content.tip)
        # Lake v4.37.0-rc1's output when the agent edits TauCeti/Two.lean while Lean compiles it.
        self.script(
            "✖ [2/3] Building TauCeti.Two (6.1s)\n"
            "error: module source file:\n"
            f"   {worktree.resolve()}/TauCeti/Two.lean\n"
            "changed since build began; rerun the build\n"
            "✖ [3/3] Building TauCeti.Three\n"
            "error: TauCeti/Three.lean:1:1: unsolved goals\n"
            "Some required targets logged failures:\n"
            "- TauCeti.Two\n"
            "- TauCeti.Three\n"
            "exit 1\n"
        )
        directory = worktree / ".tauceti-repair"
        stream = repair_stream.start_build(
            worktree, directory, dict(os.environ), chain=chain, repository=PROJECT, timeout=60
        )
        self.assertTrue(stream.finished.wait(30))
        self.assertEqual(stream.failed_modules, ["TauCeti.Three"])
        self.assertEqual(stream.summary()["reruns"], ["TauCeti.Two"])
        entries = [json.loads(line) for line in (directory / "errors.jsonl").read_text().splitlines()]
        self.assertEqual(
            entries[0],
            {
                "kind": "rerun",
                "module": "TauCeti.Two",
                "file": "TauCeti/Two.lean",
                "message": "module source file TauCeti/Two.lean changed since build began; rerun the build",
            },
        )
        self.assertEqual(entries[-1]["reruns"], 1)
        self.assertIn("1 error total, 1 module to rerun", entries[-1]["message"])
        self.assertEqual(
            [e["file"] for e in repair_parallel.read_errors(directory)], ["TauCeti/Three.lean"], "not a compiler error"
        )
        self.assertIn("`kind` `rerun` is not a compile error", "\n".join(stream.prompt_section()))


class PreRebasedPrepare(Fixture):
    """A ``prepare`` task of the next batch's pre-rebased chain is run like an own-base preparation, on
    the head's tip: warm from the outputs published for that tip, built with the streaming build,
    repaired in place, submitted on the head's tip and published for the resulting tip."""

    script = StreamRepair.script

    BASE_FILES = {
        "lakefile.toml": 'name = "TauCeti"\nplatformIndependent = true\n',
        "lean-toolchain": "leanprover/lean4:v4.36.0-rc2\n",
        ".gitignore": ".lake/\n",
    }

    def setUp(self):
        super().setUp()
        # This class exercises the late publication fallback; early upload has its own tests.
        early = patch.object(repair_publish, "start_early", return_value=None)
        early.start()
        self.addCleanup(early.stop)
        self.raw = self.content.pre_rebased_task()
        self.head = self.raw["onto"]
        self.service.tasks = {1: self.raw}

    def settings(self) -> repair_publish.Settings:
        key_file = self.root / "repair-cache-key"
        key_file.write_text("access-id:secret\n")
        return repair_publish.Settings(url="https://repair-cache.example", key_file=key_file)

    def observe_log(self) -> list[str]:
        lines: list[str] = []
        original = repair_task.log
        repair_task.log = lines.append
        self.addCleanup(setattr, repair_task, "log", original)
        return lines

    def test_the_revisions_tried_are_the_heads_tip_then_its_base_then_the_batch_base(self):
        task = repair_task.Task.from_json({**self.raw, "claim_token": "t", "expires_at": time.time() + 60})
        self.assertEqual(repair_task.pre_rebased(task)["source"], self.content.tip)
        self.assertEqual(repair_task.artifact_revisions(task), [self.head, self.content.base])
        task.extra["pre_rebased"]["head_base"] = "f" * 40
        self.assertEqual(repair_task.artifact_revisions(task), [self.head, "f" * 40, self.content.base])
        self.assertEqual(
            [rev for _, rev in repair_publish.preferred(self.settings(), task)],
            [task.tip, self.head],
            "the repair namespace at the pre-rebased tip, then at the head's tip",
        )
        own = repair_task.Task.from_json(
            {**self.content.task(kind="prepare"), "claim_token": "t", "expires_at": time.time() + 60}
        )
        self.assertIsNone(repair_task.pre_rebased(own))
        self.assertEqual(repair_task.artifact_revisions(own), [self.content.base])

    def test_the_worker_repairs_the_pre_rebased_chain_on_the_heads_tip_and_publishes_its_tip(self):
        os.environ["STUB_LAKE_CACHE_FAIL_REVS"] = self.head  # the public cache never has the head's tip
        os.environ["STUB_LAKE_REPAIR_REVS"] = self.head  # the head's repair published its outputs
        self.script()
        os.environ["STUB_AGENT_MODE"] = "stream"
        lines = self.observe_log()
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(
            (outcome["artifacts"]["cache_revision"], outcome["artifacts"]["cache_service"]),
            (self.head, "tauceti-repair"),
        )
        warm = [line for line in lines if "warm artifacts from" in line]
        self.assertEqual(
            warm,
            [
                f"task 1: warm artifacts from {self.head[:12]} (the head's tip); skipped: tauceti-repair: "
                f"no published cache mapping for {self.raw['tip'][:12]}"
            ],
        )
        self.assertEqual(outcome["stream_build"]["failed_modules"], ["TauCeti.Two", "TauCeti.Three"])
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn(f"has been replayed onto the head's tip `{self.head[:12]}`", prompt)
        self.assertNotIn("on the `main` commit", prompt)
        # The result sits on the head's tip, one commit per member, and is published under its tip.
        tip = outcome["tip"]
        self.assertEqual(
            [c.source_pr for c in chain_tool.chain_commits(self.content.clone, self.head, tip)],
            [m["source_pr"] for m in self.content.members],
        )
        self.assertEqual(self.service.results[0]["tip_sha"], tip)
        record = repair_publish.BACKGROUND.wait(60)
        self.assertEqual([(r["revision"], r["state"]) for r in record], [(tip, "published")])

    def test_a_pre_rebased_chain_that_builds_is_released_as_prepared_and_published(self):
        self.script("✔ [1/3] Built TauCeti.One (0.1s)\n✔ [2/3] Built TauCeti.Two (0.1s)\nexit 0\n")
        lines = self.observe_log()
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual((outcome["status"], outcome["reason"]), ("released", "prepared_clean"), outcome)
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual(
            [(r["reason"], r["error"]) for r in self.service.releases],
            [("prepared_clean", "the batch built on the head's tip without errors")],
        )
        self.assertIn("task 1: the open batch builds on the head's tip; nothing to prepare", lines)
        # The scripted build says green while the stub's own build still fails member 2, so the
        # publisher's rehash pass skips; what matters here is which tip it was started for.
        self.assertTrue(outcome["publish"]["launched"], outcome["publish"])
        self.assertEqual(outcome["publish"]["revision"], self.raw["tip"])


class ErrorParsing(unittest.TestCase):
    def test_continuation_lines_and_terminators(self):
        lines = [
            "✖ [1/4] Building TauCeti.A",
            "error: TauCeti/A.lean:3:4: unknown identifier 'x'",
            "  with a hint",
            "",
            "✖ [2/4] Building TauCeti.B",
            "TauCeti/B.lean:7:1: error: unsolved goals",
            "⊢ False",
            "TauCeti/B.lean:9:1: warning: declaration uses sorry",
            "error: TauCeti/B.lean:11:2: type mismatch",
            "  expected Nat",
            "Some required targets logged failures:",
            "- TauCeti.A",
            "error: TauCeti/A.lean:3:4: unknown identifier 'x'",
        ]
        errors = repair_stream.parse_errors(lines)
        self.assertEqual(
            [(e["module"], e["line"], e["column"], e["message"]) for e in errors],
            [
                ("TauCeti.A", 3, 4, "unknown identifier 'x'\n  with a hint"),
                ("TauCeti.B", 7, 1, "unsolved goals\n⊢ False"),
                ("TauCeti.B", 11, 2, "type mismatch\n  expected Nat"),
            ],
        )

    def test_lake_changed_since_build_began(self):
        # Lake v4.37.0-rc1 (lean4 #16457) for a source edited while Lean compiled it, as captured
        # from a real run; the message has no location and must not become a compiler error.
        lines = [
            "✖ [3/7] Building TauCeti.Slow (6.1s)",
            "trace: .> LEAN_PATH=/w/.lake/build/lib/lean lean /w/TauCeti/Slow.lean -o /w/x.olean --json",
            "error: TauCeti/A.lean:3:4: unknown identifier 'x'",
            "error: module source file:",
            "   /w/TauCeti/Slow.lean",
            "changed since build began; rerun the build",
            "error: module source file:",
            "   /elsewhere/Other.lean",
            "changed since build began; rerun the build",
            "Some required targets logged failures:",
            "- TauCeti.Slow",
            "error: build failed",
        ]
        parser = repair_stream.ErrorParser(root=Path("/w"))
        found = [entry for line in lines for entry in parser.feed(line)] + parser.finish()
        self.assertEqual(
            [(e["module"], e["message"]) for e in parser.errors], [("TauCeti.A", "unknown identifier 'x'")]
        )
        self.assertEqual(
            [(e["kind"], e["module"], e["file"]) for e in parser.reruns],
            [("rerun", "TauCeti.Slow", "TauCeti/Slow.lean"), ("rerun", None, "/elsewhere/Other.lean")],
        )
        self.assertEqual(len(found), 3)
        self.assertEqual(repair_stream.parse_errors(lines), parser.errors)


class Records(unittest.TestCase):
    def test_a_pathless_verification_finding_reaches_the_next_repair_prompt(self):
        root = Path(tempfile.mkdtemp(prefix="pathless-", dir=TMP))
        content = Content(root)
        task = repair_task.Task.from_json({**content.task(12), "claim_token": "x", "expires_at": time.time() + 60})
        seed = root / "seed"
        chain = repair.read_chain(seed, content.base, content.tip)
        context = repair_task.prompt_context(task, None, listings=repair.listings_path(seed))
        findings = repair_task.task_findings(task)
        findings["compiler_errors"] = [{"path": "", "message": "the repaired chain failed verification"}]
        with patch.object(repair, "git", side_effect=AssertionError("pathless findings have no file ownership")):
            self.assertEqual(repair.owners_of(seed, chain, ""), [])
        prompt = repair.compose_prompt(context, findings, chain, seed)
        self.assertIn("Build/verification diagnostic (no file path)", prompt)
        self.assertIn("the repaired chain failed verification", prompt)

    def test_a_pre_rebase_prompt_lists_the_errors_its_preparation_left(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(12, kind="pre_rebase")
        seed = root / "seed"
        raw["onto"] = commit(seed, "head tip", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        left = [{"path": "TauCeti/Two.lean", "line": 3, "column": 5, "message": "unknown identifier `x`\n  more"}]
        raw["diagnostics"] = [{"kind": "prepare_errors", "task_id": 7, "resolution": "adopted", "errors": left}]
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        findings = repair_task.task_findings(task)
        self.assertEqual(findings["prepare_errors"]["errors"], left)
        self.assertEqual(findings["compiler_errors"], [], "not errors of this task's own build")
        chain = repair.read_chain(seed, content.base, content.tip)
        context = repair_task.prompt_context(task, None, listings=repair.listings_path(seed))
        prompt = repair.compose_prompt(context, findings, chain, seed)
        self.assertIn("## Errors the batch's preparation left", prompt)
        self.assertIn("(prepare task 7)", prompt)
        self.assertIn("- `TauCeti/Two.lean:3:5`: unknown identifier `x`", prompt)

    def test_a_preparation_reports_the_errors_no_worker_build_showed_fixed(self):
        worktree = Path(tempfile.mkdtemp(prefix="remaining-", dir=TMP))
        listings = repair.listings_path(worktree).parent
        listings.mkdir(parents=True)
        (listings / repair_stream.ERRORS_FILE).write_text(
            json.dumps({"module": "TauCeti.B", "file": "TauCeti/B.lean", "line": 2, "column": 1, "message": "boom"})
            + "\n"
            + json.dumps({"event": "finished", "rc": 1})
            + "\n"
        )
        findings = repair.findings_from_diagnostics([])
        self.assertEqual(repair_task.prepare_remaining_errors({"status": "ok"}, findings, worktree), [])
        self.assertEqual(
            repair_task.prepare_remaining_errors(None, findings, worktree),
            [{"path": "TauCeti/B.lean", "line": 2, "column": 1, "message": "boom"}],
        )
        narrowed = {**findings, "compiler_errors": [{"path": "TauCeti/C.lean", "line": 1, "column": 1, "message": "x"}]}
        self.assertEqual(
            [e["path"] for e in repair_task.prepare_remaining_errors({"status": "partial"}, narrowed, worktree)],
            ["TauCeti/C.lean"],
        )

    def test_task_record_and_prompt_for_a_pre_rebase(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(11, kind="pre_rebase")
        seed = root / "seed"
        onto = commit(seed, "head tip", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        raw["onto"] = onto
        raw["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#2",
                "paths": ["TauCeti/Two.lean"],
                "onto": onto,
                "message": "replaying the open batch onto the head's tip conflicts",
                "commit": content.members[1]["commit"],
                "pending": [f"{PROJECT}#3"],
                "clean_prefix_tip": content.members[0]["commit"],
            },
            {
                "kind": "cross_batch",
                "approximate": True,
                "removed_used": [
                    {
                        "name": "foo_bar",
                        "removed_by": [{"position": 4, "pr": 40, "file": "TauCeti/X.lean", "renamed_to": "foo_baz"}],
                        "references": [{"file": "TauCeti/Two.lean", "position": 2, "pr": 2, "lines": [3]}],
                    }
                ],
                "added_by_both": [
                    {
                        "name": "both_add",
                        "head": [{"position": 1, "pr": 41, "file": "TauCeti/Y.lean"}],
                        "next": [{"position": 3, "pr": 3, "file": "TauCeti/Three.lean"}],
                    }
                ],
            },
        ]
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        self.assertEqual((task.kind, task.validation_base), ("pre_rebase", onto))
        self.assertEqual(repair_task.artifact_revisions(task), [onto, content.base])
        chain = repair.read_chain(seed, content.base, content.tip)
        context = repair_task.prompt_context(task, None, listings=repair.listings_path(seed))
        prompt = repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        self.assertIn(f"onto the tip `{onto[:12]}` of the head batch ahead of it with git", prompt)
        self.assertIn(f"The target is not `main`: `{onto[:12]}` is what `main` becomes when the head lands", prompt)
        self.assertIn(f"- target, the head's tip: `{onto[:12]}`", prompt)
        self.assertIn(f"- clean prefix, HEAD: `{content.members[0]['commit'][:12]}`", prompt)
        self.assertIn("## Predicted ripples from the head", prompt)
        self.assertIn(
            "- `foo_bar` is removed by the head's position 4 (PR #40, `TauCeti/X.lean`, renamed to `foo_baz`) and used "
            "in this batch's `TauCeti/Two.lean` (position 2, PR #2)",
            prompt,
        )
        self.assertIn(
            "- `both_add` is added by the head's position 1 (PR #41, `TauCeti/Y.lean`) and by this batch's position 3 "
            "(PR #3, `TauCeti/Three.lean`)",
            prompt,
        )
        rebase = repair_task.Task.from_json({**raw, "kind": "rebase_conflict", "claim_token": "x", "expires_at": 1e10})
        context = repair_task.prompt_context(rebase, None, listings=repair.listings_path(seed))
        self.assertNotIn(
            "Predicted ripples", repair.compose_prompt(context, repair_task.task_findings(rebase), chain, seed)
        )

    def test_a_no_repair_member_is_listed_under_do_not_edit(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(12)
        raw["members"] = [dict(m, no_repair=m["number"] == 2) for m in raw["members"]]
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        self.assertEqual([m.no_repair for m in task.members], [False, True, False])
        seed = root / "seed"
        chain = repair.read_chain(seed, content.base, content.tip)
        context = repair_task.prompt_context(task, None, listings=repair.listings_path(seed))
        prompt = repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        self.assertIn("## Do not edit", prompt)
        self.assertIn("- #2 ", prompt.split("## Do not edit", 1)[1])
        self.assertIn("    Interacting-PR: #<the other PR>", prompt)
        listings = [json.loads(line) for line in repair.listings_path(seed).read_text().splitlines()]
        self.assertEqual([r.get("no_repair", False) for r in listings], [False, True, False])
        plain = repair_task.Task.from_json({**content.task(13), "claim_token": "x", "expires_at": time.time() + 60})
        context = repair_task.prompt_context(plain, None, listings=repair.listings_path(seed))
        self.assertNotIn("Do not edit", repair.compose_prompt(context, repair_task.task_findings(plain), chain, seed))

    def test_a_conflict_prompt_shows_only_findings_about_its_own_commits(self):
        """Each conflict's prompt keeps the head's ripples into the prefix and the conflicting commit, and
        drops those into later members and the batch-wide predictions and preparation errors."""
        ripple = {"file": "TauCeti/X.lean", "position": 3, "pr": 3, "lines": [1]}
        raw = {
            "task_id": 1,
            "kind": "pre_rebase",
            "repository": PROJECT,
            "batch_id": 7,
            "batch_version": 1,
            "base": "a" * 40,
            "tip": "b" * 40,
            "onto": "c" * 40,
            "members": [],
            "diagnostics": [
                {"kind": "admission_predictions", "proactive": True},
                {"kind": "prepare_errors", "task_id": 4, "errors": [{"module": "TauCeti/X.lean", "message": "boom"}]},
                {
                    "kind": "cross_batch",
                    "removed_used": [
                        {"name": "foo", "removed_by": [], "references": [dict(ripple, pr=2), ripple]},
                        {"name": "bar", "removed_by": [], "references": [ripple]},
                    ],
                    "added_by_both": [
                        {"name": "baz", "head": [], "next": [{"pr": 3}]},
                        {"name": "qux", "head": [], "next": [{"pr": 1}, {"pr": 3}]},
                    ],
                },
            ],
            "claim_token": "x",
            "expires_at": time.time() + 60,
        }
        task = repair_task.Task.from_json(raw)
        entry = {"kind": "conflict", "source_pr": f"{PROJECT}#2", "commit": "d" * 40, "paths": []}
        findings = repair_task.conflict_findings(task, entry, {1, 2})
        self.assertEqual(findings["conflict"]["commit"], "d" * 40)
        self.assertIsNone(findings["admission_predictions"])
        self.assertIsNone(findings.get("prepare_errors"))
        self.assertEqual(
            [(i["name"], [r["pr"] for r in i["references"]]) for i in findings["cross_batch"]["removed_used"]],
            [("foo", [2])],
        )
        self.assertEqual(
            [(i["name"], i["next"]) for i in findings["cross_batch"]["added_by_both"]], [("qux", [{"pr": 1}])]
        )
        repair_task.prompt_context(task, None, findings)
        self.assertIsNone(findings["admission_predictions"], "the task's predictions are not put back")

    def test_ownership_spans_the_replayed_prefix_and_the_input_commit(self):
        """A conflict's chain is the replayed prefix followed by the input chain's conflicting commit, which
        are not one history: each commit is judged against its own parent."""
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        shared = "".join(f"line {i}\n" for i in range(10))
        content = Content(
            root,
            base_files={"TauCeti/S.lean": shared},
            member_files={
                1: {"TauCeti/S.lean": shared.replace("line 0", "line zero")},
                2: {"TauCeti/S.lean": shared.replace("line 9", "line nine")},
            },
        )
        seed = root / "seed"
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        onto = commit(seed, "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        prefix = chain_tool.replay(seed, content.base, content.members[0]["commit"], onto).tip
        conflicting = content.members[1]["commit"]
        chain = [*repair.read_chain(seed, onto, prefix), *repair.read_chain(seed, f"{conflicting}^", conflicting)]
        self.assertEqual(repair.owners_of(seed, chain, "TauCeti/S.lean"), [1, 2])
        self.assertEqual(repair.owners_of(seed, chain, "TauCeti/One.lean"), [1])
        self.assertEqual(repair.owners_of(seed, chain, "TauCeti/A.lean"), [])

    def test_task_record_and_prompt_for_a_rebase_conflict(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(9, kind="rebase_conflict")
        seed = root / "seed"
        onto = commit(seed, "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        prefix = chain_tool.replay(seed, content.base, content.members[0]["commit"], onto).tip
        raw["onto"] = onto
        raw["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#2",
                "commit": content.members[1]["commit"],
                "paths": ["TauCeti/Two.lean"],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#2",
                "pending": [f"{PROJECT}#3"],
                "clean_prefix_tip": prefix,
            }
        ]
        task = repair_task.Task.from_json(
            {**raw, "claim_token": "x", "expires_at": time.time() + 60, "extra": {"claim_id": 5}}
        )
        self.assertEqual(task.validation_base, onto)
        self.assertEqual(task.member_keys, [f"{PROJECT}#1", f"{PROJECT}#2", f"{PROJECT}#3"])
        self.assertEqual(task.submission_ref, "refs/tauceti-queue/submissions/9/5")
        self.assertEqual(repair_task.Task.from_json(task.to_json()), task)
        # The worker's chain: the clean prefix, then the one conflicting commit; never the commits after it.
        chain = [
            *repair.read_chain(seed, onto, prefix),
            *repair.read_chain(seed, f"{content.members[1]['commit']}^", content.members[1]["commit"]),
        ]
        listings = repair.listings_path(seed)
        context = repair_task.prompt_context(task, None, listings=listings)
        prompt = repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        self.assertTrue(prompt.startswith(repair.RESOLUTION_INSTRUCTION))
        self.assertLess(len(prompt), 10_000, f"prompt is {len(prompt)} bytes")
        self.assertIn("## What happened", prompt)
        self.assertLess(prompt.index("## What happened"), prompt.index("## Conflict evidence"))
        conflicting = content.members[1]["commit"][:12]
        self.assertIn(
            f"from its base `{content.base[:12]}` onto the current `main` commit `{onto[:12]}` with git. The replay "
            f"stopped at PR #2's commit `{conflicting}` with conflicts in `TauCeti/Two.lean`. The 1 commit before it "
            f"replayed cleanly and is at `{prefix[:12]}`. Your job is to resolve that one commit",
            prompt,
        )
        self.assertIn(f"HEAD is `{prefix[:12]}`, and the working tree holds PR #2's commit", prompt)
        self.assertIn(f"- conflicting commit: `{conflicting}` (PR #2, position 2: feat: add 2)", prompt)
        self.assertIn(f"- clean prefix, HEAD: `{prefix[:12]}` (position 1 replayed onto the target)", prompt)
        self.assertIn(f"- target, the current `main`: `{onto[:12]}`", prompt)
        self.assertIn(f"`TauCeti/Two.lean`: PR #2's commit `{conflicting}` modifies this file", prompt)
        self.assertIn("Do not commit, rebase, reset, cherry-pick or create branches", prompt)
        self.assertIn("`.tauceti-repair/explanation.md`", prompt)
        # Nothing hands the agent the replay: no rebase recipe, no step script, no later commit.
        third = content.members[2]["commit"][:12]
        for phrase in (
            "git rebase",
            "REBASE_HEAD",
            ".tauceti-repair/step",
            "--autosquash",
            "not yet replayed",
            third,
            "#3",
        ):
            self.assertNotIn(phrase, prompt, phrase)
        self.assertNotIn("python3 .tauceti-repair/check", prompt, "the chain check is not the agent's")
        self.assertFalse((listings.parent / "step").exists())
        self.assertFalse((listings.parent / "check").exists())
        records = [json.loads(line) for line in listings.read_text().splitlines()]
        self.assertEqual([(r["pr"], r["state"]) for r in records], [(1, "replayed"), (2, "conflicting")])
        self.assertEqual(sh(seed, "status", "--porcelain"), "", "the listings are excluded from Git")
        with self.assertRaises(ValueError):
            repair_task.Task.from_json({**raw, "kind": "mystery", "claim_token": "x", "expires_at": 1})

    def test_the_worker_commits_the_working_tree_and_discards_what_the_agent_did_to_history(self):
        """The agent resolves one commit; whatever it does to HEAD, the index, refs or history, the result is
        the working tree committed by the worker on the clean prefix, with the member's own message."""
        root = Path(tempfile.mkdtemp(prefix="resolve-", dir=TMP))
        content = Content(root)
        seed = root / "seed"
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        onto = commit(seed, "main adds Two", {"TauCeti/Two.lean": "theorem two : True := by trivial\n"})
        prefix = chain_tool.replay(seed, content.base, content.members[0]["commit"], onto).tip
        conflicting = content.members[1]["commit"]
        worktree = root / "worktree"
        repair.create_worktree(seed, prefix, worktree)
        repair.listings_path(worktree).parent.mkdir(parents=True, exist_ok=True)
        stale = worktree / repair.LISTINGS_DIR / repair.EXPLANATION_FILE
        stale.write_text("an earlier task's explanation\n")
        paths = repair.apply_conflict(worktree, prefix, conflicting)
        self.assertEqual(paths, ["TauCeti/Two.lean"])
        self.assertFalse(stale.exists(), "no explanation is inherited")
        self.assertEqual(sh(worktree, "rev-parse", "HEAD"), prefix)
        self.assertIn("<<<<<<<", (worktree / "TauCeti" / "Two.lean").read_text())
        self.assertFalse(Path(sh(worktree, "rev-parse", "--git-path", "rebase-merge")).exists())
        # Conflict markers left in place are reported.
        tip, report = repair.commit_resolution(seed, worktree, prefix, conflicting, paths)
        self.assertFalse(report["ok"])
        self.assertIn("conflict-markers: `TauCeti/Two.lean` still contains conflict markers", report["problems"])
        self.assertEqual(report["unexplained"], [f"{PROJECT}#2"])
        # The agent resolves the file, then tries to replay and rewrite history itself.
        (worktree / "TauCeti" / "Two.lean").write_text(
            "theorem two : True := by trivial\ntheorem two' : True := trivial\n"
        )
        (worktree / "TauCeti" / "Extra.lean").write_text("theorem extra : True := trivial\n")
        stale.write_text("Kept main's proof of `two` and added the PR's statement as `two'`.\n")
        sh(worktree, "add", "-A")
        sh(worktree, "commit", "--quiet", "-m", "agent commit without trailers")
        sh(worktree, "update-ref", "refs/heads/agent", "HEAD")
        sh(worktree, "reset", "--quiet", "--soft", content.tip)  # HEAD on the old chain's tip, files kept
        (worktree / ".lake").mkdir(exist_ok=True)
        (worktree / ".lake" / "junk").write_text("build output\n")
        tip, report = repair.commit_resolution(seed, worktree, prefix, conflicting, paths)
        self.assertTrue(report["ok"], report)
        # A project whose `.gitignore` ignores `.lake`, as TauCeti's does, commits the same tree.
        (worktree / ".gitignore").write_text(".lake\n")
        ignored, _ = repair.commit_resolution(seed, worktree, prefix, conflicting, paths)
        self.assertEqual(
            sorted(sh(seed, "diff", "--name-only", tip, ignored).split()), [".gitignore"], "only the new .gitignore"
        )
        (worktree / ".gitignore").unlink()
        tip, report = repair.commit_resolution(seed, worktree, prefix, conflicting, paths)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unexplained"], [])
        resolved = chain_tool.read_commit(seed, tip)
        original = chain_tool.read_commit(seed, conflicting)
        self.assertEqual(resolved.parents, (prefix,), "the parent is the clean prefix, not the agent's HEAD")
        self.assertEqual(sh(seed, "rev-list", "--count", f"{onto}..{tip}"), "2", "the prefix and one commit")
        self.assertFalse(chain_tool.is_ancestor(seed, content.tip, tip), "the agent's HEAD is not an ancestor")
        self.assertEqual(sh(worktree, "rev-parse", "HEAD"), tip)
        self.assertEqual(
            sorted(sh(seed, "diff", "--name-only", prefix, tip).split()),
            ["TauCeti/Extra.lean", "TauCeti/Two.lean"],
            "the working tree is the resolution, without .lake or the listings",
        )
        self.assertTrue(resolved.message.startswith(original.message.rstrip()))
        self.assertEqual(
            chain_tool.appended_text(original.message, resolved.message),
            "Kept main's proof of `two` and added the PR's statement as `two'`.",
        )
        self.assertEqual((resolved.author_name, resolved.author_date), (original.author_name, original.author_date))
        self.assertEqual(resolved.source_pr, f"{PROJECT}#2")
        again, _ = repair.commit_resolution(seed, worktree, prefix, conflicting, paths)
        self.assertEqual(again, tip, "the same files and explanation make the same commit")

    def test_the_check_script_judges_the_chain(self):
        root = Path(tempfile.mkdtemp(prefix="check-", dir=TMP))
        content = Content(root)
        seed = root / "seed"
        onto = content.base
        task = repair_task.Task.from_json({**content.task(9), "claim_token": "x", "expires_at": time.time() + 60})
        chain = repair.read_chain(seed, content.base, content.tip)
        listings = repair.listings_path(seed)
        repair.compose_prompt(
            repair_task.prompt_context(task, None, listings=listings), repair_task.task_findings(task), chain, seed
        )
        check = listings.parent / "check"
        self.assertTrue(os.access(check, os.X_OK))

        def run() -> tuple[int, str]:
            result = subprocess.run([sys.executable, str(check)], cwd=seed, text=True, capture_output=True)
            return result.returncode, result.stdout.strip()

        env = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": ":"}
        sh(seed, "checkout", "--quiet", "--detach", content.tip)
        self.assertEqual(run(), (0, "ok"))
        # The prompt's explanation recipe keeps the message as a prefix and passes.
        second = sh(seed, "rev-list", "--reverse", f"{onto}..HEAD").split()[1]
        sh(seed, "commit", "--allow-empty", f"--squash={second}", "-m", "Repair: used trivial.")
        sh(seed, "rebase", "-i", "--autosquash", "--keep-empty", "--empty=keep", onto, env=env)
        self.assertEqual(run(), (0, "ok"))
        second = sh(seed, "rev-list", "--reverse", f"{onto}..HEAD").split()[1]
        self.assertTrue(sh(seed, "log", "-1", "--format=%B", second).rstrip().endswith("Repair: used trivial."))
        self.assertEqual(
            sh(seed, "log", "-1", "--format=%an", second), sh(seed, "log", "-1", "--format=%an", chain[1].sha)
        )
        # A dirty tree, a dropped trailer and a reordered chain are each reported as the first problem.
        (seed / "TauCeti" / "Two.lean").write_text("theorem two : True := by trivial\n")
        self.assertEqual(run(), (1, "the working tree has uncommitted changes"))
        sh(seed, "checkout", "--", "TauCeti/Two.lean")
        tip = sh(seed, "rev-parse", "HEAD")
        sh(seed, "commit", "--amend", "--allow-empty", "-m", f"feat: add 3\n\nSource-PR: {PROJECT}#3\n")
        rc, problem = run()
        self.assertEqual(rc, 1)
        self.assertIn("position 3", problem)
        self.assertIn("Accepted-Head", problem)
        sh(seed, "checkout", "--quiet", "--detach", tip)
        shas = sh(seed, "rev-list", "--reverse", f"{onto}..HEAD").split()
        sh(seed, "checkout", "--quiet", "--detach", onto)
        for sha in (shas[0], shas[2], shas[1]):
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", sha, env=env)
        rc, problem = run()
        self.assertEqual(rc, 1)
        self.assertIn("position 2, commit", problem)
        self.assertIn(f"expected one `Source-PR: {PROJECT}#2` trailer (recorded order)", problem)
        # A dropped member is a count mismatch.
        sh(seed, "checkout", "--quiet", "--detach", onto)
        for sha in (shas[0], shas[1]):
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", sha, env=env)
        self.assertEqual(run(), (1, f"2 commits on {onto[:12]} for 3 recorded members"))

    def test_a_large_batch_prompt_stays_short(self):
        root = Path(tempfile.mkdtemp(prefix="large-", dir=TMP))
        seed = root / "seed"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
        sh(seed, "config", "user.name", "Fixture")
        sh(seed, "config", "user.email", "fixture@example.invalid")
        base = commit(seed, "base", {"TauCeti/A.lean": "theorem a : True := trivial\n"})
        count, conflicting = 1197, 437
        members = []
        for number in range(1, count + 1):
            head = f"{number:040x}"
            sha = commit(
                seed,
                f"feat: add {number} (#{number})\n\nBody of {number}.\n\nSource-PR: {PROJECT}#{number}\n"
                f"Accepted-Head: {head}\n",
                {f"TauCeti/M{number}.lean": f"theorem m{number} : True := trivial\n"},
            )
            members.append(
                {
                    "source_pr": f"{PROJECT}#{number}",
                    "number": number,
                    "accepted_head": head,
                    "commit": sha,
                    "provenance": "clean",
                    "title": f"feat: add {number}",
                    "description": f"Body of {number}.",
                    "author": "alice",
                    "explanation": "",
                    "retained_ref": f"refs/tauceti-queue/inputs/{number}/{head}",
                }
            )
        tip = members[-1]["commit"]
        sh(seed, "checkout", "--quiet", "--detach", base)
        onto = commit(seed, "main moved", {f"TauCeti/M{conflicting}.lean": "theorem clash : True := trivial\n"})
        sh(seed, "checkout", "--quiet", "--detach", tip)
        raw = {
            "task_id": 11,
            "kind": "rebase_conflict",
            "repository": PROJECT,
            "batch_id": 8,
            "batch_version": 1,
            "base": base,
            "tip": tip,
            "onto": onto,
            "members": members,
            "diagnostics": [
                {
                    "kind": "conflict",
                    "source_pr": f"{PROJECT}#{conflicting}",
                    "paths": [f"TauCeti/M{conflicting}.lean"],
                    "onto": onto,
                    "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#{conflicting}",
                    "commit": members[conflicting - 1]["commit"],
                    "pending": [f"{PROJECT}#{n}" for n in range(conflicting + 1, count + 1)],
                    "clean_prefix_tip": members[conflicting - 2]["commit"],
                }
            ],
            "retained_refs": [],
            "instruction": repair.AGENT_INSTRUCTION,
            "created_at": time.time(),
            "extra": {},
            "claim_token": "x",
            "expires_at": time.time() + 60,
        }
        task = repair_task.Task.from_json(raw)
        # What the worker hands the prompt: the clean prefix and the conflicting commit.
        chain = repair.read_chain(seed, base, members[conflicting - 1]["commit"])
        self.assertEqual(len(chain), conflicting)
        listings = repair.listings_path(seed)
        artifacts = {
            "available": False,
            "cold": True,
            "unavailable_reason": "no published cache mapping for " + onto[:12],
        }
        context = repair_task.prompt_context(task, None, artifacts=artifacts, listings=listings)
        prompt = repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        self.assertLess(len(prompt), 10_000, f"prompt is {len(prompt)} bytes")
        conflict_sha = members[conflicting - 1]["commit"][:12]
        self.assertIn(
            f"The replay stopped at PR #{conflicting}'s commit `{conflict_sha}` with conflicts in "
            f"`TauCeti/M{conflicting}.lean`. The {conflicting - 1} commits before it replayed cleanly and are at "
            f"`{members[conflicting - 2]['commit'][:12]}`.",
            prompt,
        )
        self.assertIn(
            f"- conflicting commit: `{conflict_sha}` (PR #{conflicting}, position {conflicting}: "
            f"feat: add {conflicting})",
            prompt,
        )
        self.assertNotIn("not yet replayed", prompt)
        self.assertNotIn("#10: feat: add 10", prompt, "members are left to the listings file")
        self.assertNotIn(f"{PROJECT}#{conflicting + 1}", prompt, "the pending list is in the file, not the prompt")
        self.assertIn("The build is cold", prompt)
        self.assertNotIn("Artifacts are warm", prompt)
        self.assertIn(f"`lake build TauCeti.M{conflicting}`", prompt)
        self.assertIn("Long builds are fine to run in the foreground: shell commands have a two-hour limit", prompt)
        # The file holds everything in chain order and Git ignores it.
        records = [json.loads(line) for line in listings.read_text().splitlines()]
        self.assertEqual(len(records), conflicting, "the commits after the conflicting one are not listed")
        self.assertEqual([r["position"] for r in records], list(range(1, conflicting + 1)))
        self.assertEqual([r["pr"] for r in records], list(range(1, conflicting + 1)))
        self.assertEqual(records[conflicting - 1]["state"], "conflicting")
        self.assertEqual({r["state"] for r in records[: conflicting - 1]}, {"replayed"})
        self.assertEqual(records[9]["commit"], members[9]["commit"])
        self.assertEqual(records[9]["accepted_head"], f"{10:040x}")
        self.assertTrue(os.access(listings.parent / "build", os.X_OK), "the scoped build script is written")
        self.assertIn("Check a fix with `.tauceti-repair/build <Module.Name>` first", prompt)
        self.assertIn("Run the whole-library `lake build --iofail` only as the final verdict.", prompt)
        self.assertEqual(sh(seed, "status", "--porcelain"), "", "the listings are excluded from Git")
        self.assertEqual(sh(seed, "check-ignore", str(listings)), str(listings))
        # Written again for the next prompt, and the exclude entry is not repeated.
        repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        repair.listings_path(seed)
        exclude = Path(sh(seed, "rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else seed / exclude
        self.assertEqual(exclude.read_text().count("/.tauceti-repair/"), 1)
        # Without a listings path the listings stay inline, as the experiment's prompt preview expects.
        inline = repair.compose_prompt(
            repair_task.prompt_context(task, None), repair_task.task_findings(task), chain, seed
        )
        self.assertIn("#10: feat: add 10", inline)
        self.assertNotIn("members.jsonl", inline)

    def test_the_shell_limit_is_the_repair_task_s_own(self):
        from tauceti_worker import agents

        self.addCleanup(patch.stopall)
        patch("tauceti_worker.agents.time.time", return_value=1000).start()
        deadline = 1000 + 3 * 3600
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": str(repair.SHELL_LIMIT_SECONDS)}):
            env = agents._foreground_shell_env(deadline)
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], "7200000")
        self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], "7200000")
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": ""}):  # the default: an hour
            self.assertEqual(agents._foreground_shell_env(deadline)["BASH_MAX_TIMEOUT_MS"], "3600000")
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": "not a number"}):
            self.assertEqual(agents._foreground_shell_env(deadline)["BASH_MAX_TIMEOUT_MS"], "3600000")
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": "7200"}):
            self.assertEqual(agents._foreground_shell_env(time.time() + 30)["BASH_MAX_TIMEOUT_MS"], "30000")

    def test_admin_predicate(self):
        repair_task.verify_admin(PROJECT)
        os.environ["STUB_GH_ADMIN"] = "false"
        try:
            from tauceti_worker.config import Die

            with self.assertRaises(Die):
                repair_task.verify_admin(PROJECT)
        finally:
            os.environ.pop("STUB_GH_ADMIN", None)

    def test_chain_report_flags_unexplained_edits(self):
        root = Path(tempfile.mkdtemp(prefix="report-", dir=TMP))
        content = Content(root)
        seed = root / "seed"
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        chain = repair.read_chain(seed, content.base, content.tip)
        for commit_ in chain:
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", commit_.sha)
            if commit_.pr == 2:
                (seed / "TauCeti" / "Two.lean").write_text("theorem two : True := trivial\n")
                sh(seed, "add", "-A")
                sh(seed, "commit", "--quiet", "--amend", "--no-edit")
        tip = sh(seed, "rev-parse", "HEAD")
        report = repair.chain_report(seed, content.base, content.tip, tip, [m["source_pr"] for m in content.members])
        self.assertTrue(report["ok"])
        self.assertEqual(report["edited"], [f"{PROJECT}#2"])
        self.assertEqual(report["unexplained"], [f"{PROJECT}#2"])
        self.assertIn(f"{PROJECT}#2", repair.explanation_prompt(report["unexplained"], chain))
        short = repair.chain_report(seed, content.base, content.tip, tip, [m["source_pr"] for m in content.members[:2]])
        self.assertFalse(short["ok"])
        self.assertTrue(any("unknown-member" in p for p in short["problems"]))

    def test_an_admin_labelled_large_refactor_member_is_measured_without_the_size_caps(self):
        root = Path(tempfile.mkdtemp(prefix="large-refactor-", dir=TMP))
        content = Content(root)
        seed = root / "seed"
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        chain = repair.read_chain(seed, content.base, content.tip)
        for commit_ in chain:
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", commit_.sha)
            if commit_.pr == 2:
                # Exceed the shared added-line cap with new files each within the new-file cap, so only
                # the size caps fail; TauCetiTools owns both numbers and has raised them before.
                from tauceti_tools.assets.scripts import ci_git

                per_file = ci_git.NEW_FILE_LINE_CAP
                for index in range(ci_git.REVIEW_ADDED_LINE_CAP // per_file + 1):
                    lines = "".join(f"theorem r{index}_{i} : True := trivial\n" for i in range(per_file))
                    (seed / "TauCeti" / f"Refactor{index}.lean").write_text(lines)
                sh(seed, "add", "-A")
                sh(seed, "commit", "--quiet", "--amend", "--no-edit")
        tip = sh(seed, "rev-parse", "HEAD")
        members = [m["source_pr"] for m in content.members]
        capped = repair.chain_report(seed, content.base, content.tip, tip, members)
        [entry] = capped["review_size"]
        self.assertIn("too large to review", entry["violations"][0])
        head = content.members[1]["accepted_head"]
        exempt = repair.chain_report(seed, content.base, content.tip, tip, members, large_refactors={(2, head)})
        self.assertEqual(exempt["review_size"], [])
        other = repair.chain_report(seed, content.base, content.tip, tip, members, large_refactors={(3, head)})
        self.assertEqual(len(other["review_size"]), 1)

        # The task member's flag reaches the agent's check script through the listings.
        raw = content.task(1)
        raw["members"][1]["large_refactor"] = True
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        self.assertEqual(task.large_refactor_heads, frozenset({(2, head)}))
        listings = repair.listings_path(seed)
        repair.compose_prompt(
            repair_task.prompt_context(task, None, listings=listings),
            repair_task.task_findings(task),
            repair.read_chain(seed, content.base, tip),
            seed,
        )
        check = listings.parent / "check"

        def run(*args: str) -> tuple[int, str]:
            result = subprocess.run([sys.executable, str(check), *args], cwd=seed, text=True, capture_output=True)
            return result.returncode, result.stdout.strip()

        self.assertEqual(run(), (0, "ok"))
        second = sh(seed, "rev-list", "--reverse", f"{content.base}..HEAD").split()[1]
        rc, measured = run("--size", second)
        self.assertEqual((rc, json.loads(measured)["large_refactor"]), (0, True))

    def test_the_review_size_policy_measures_each_commit_against_its_parent_and_accepts_a_split(self):
        root = Path(tempfile.mkdtemp(prefix="review-size-", dir=TMP))
        content = Content(root)
        seed = root / "seed"
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        chain = repair.read_chain(seed, content.base, content.tip)
        long_file = "".join(f"theorem t{i} : True := trivial\n" for i in range(1251))
        for commit_ in chain:
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", commit_.sha)
            if commit_.pr == 2:
                (seed / "TauCeti" / "Long.lean").write_text(long_file)
                sh(seed, "add", "-A")
                sh(seed, "commit", "--quiet", "--amend", "--no-edit")
        tip = sh(seed, "rev-parse", "HEAD")
        members = [m["source_pr"] for m in content.members]
        report = repair.chain_report(seed, content.base, content.tip, tip, members)
        self.assertFalse(report["ok"])
        [entry] = report["review_size"]
        self.assertEqual(entry["pr"], 2)
        self.assertIn(
            f"TauCeti/Long.lean: new Lean files must be at most {diff_guard.ci_git.NEW_FILE_LINE_CAP} lines (found 1251)",
            entry["violations"][0],
        )
        self.assertTrue(any(p.startswith("review-size: #2 (commit ") for p in report["problems"]))

        # The agent's check script reports the same rejection, and `--size` measures one commit.
        record = {
            "source_pr": 2,
            "head": content.members[1]["accepted_head"],
            "reasons": ["long"],
            "measured": "batch-commit",
            "batch_commit": tip,
            "parent": content.base,
        }
        raw = {**content.task(1), "diagnostics": [queue_diagnostics.member_policy_finding(json.dumps(record))]}
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        listings = repair.listings_path(seed)
        prompt = repair.compose_prompt(
            repair_task.prompt_context(task, None, listings=listings),
            repair_task.task_findings(task),
            repair.read_chain(seed, content.base, tip),
            seed,
        )
        self.assertIn("## The review-size policy", prompt)
        self.assertIn("Splitting the change inside the batch is a valid repair", prompt)
        self.assertIn("python3 .tauceti-repair/check --size <commit>", prompt)
        check = listings.parent / "check"

        def run(*args: str) -> tuple[int, str]:
            result = subprocess.run([sys.executable, str(check), *args], cwd=seed, text=True, capture_output=True)
            return result.returncode, result.stdout.strip()

        rc, problem = run()
        self.assertEqual(rc, 1)
        self.assertIn("(#2): the staging review-size policy measures this commit against its parent", problem)
        second = sh(seed, "rev-list", "--reverse", f"{content.base}..HEAD").split()[1]
        rc, measured = run("--size", second)
        self.assertEqual((rc, json.loads(measured)["new_files_too_long"]), (1, [["TauCeti/Long.lean", 1251]]))

        # Splitting the long file inside member 2's own commit satisfies the policy.
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        for commit_ in chain:
            sh(seed, "cherry-pick", "--allow-empty", "--keep-redundant-commits", commit_.sha)
            if commit_.pr == 2:
                lines = long_file.splitlines(keepends=True)
                (seed / "TauCeti" / "Long.lean").write_text("".join(lines[:650]))
                (seed / "TauCeti" / "Long2.lean").write_text("".join(lines[650:]))
                sh(seed, "add", "-A")
                sh(seed, "commit", "--quiet", "--amend", "--no-edit")
        split = sh(seed, "rev-parse", "HEAD")
        report = repair.chain_report(seed, content.base, content.tip, split, members)
        self.assertEqual(report["review_size"], [])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(run(), (0, "ok"))


TOOLS_REPOSITORY, TOOLS_REF = make_tools_repository(TMP / "tools")
DUPLICATE_LINE = "duplicate-declarations: Foo.bar is declared in both TauCeti.Two and TauCeti.Three"


class WorkerAudits(Fixture):
    """CI's audits and changed-module lint on the worker (``repair_audit``), with the fake TauCetiTools pin."""

    BASE_FILES = {
        ".gitignore": ".lake/\n",
        "tauceti.toml": f'[tools.tools]\nrepo = "TauCetiProject/TauCetiTools"\nref = "{TOOLS_REF}"\n',
        "scripts/lint-baseline.txt": "",
    }

    def setUp(self):
        super().setUp()
        os.environ[repair_audit.URL_VARIABLE] = str(TOOLS_REPOSITORY)
        self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)

    def findings(self, name: str, text: str) -> None:
        (self.stub_dir / name).write_text(text + "\n")

    def errors_file(self) -> list[dict]:
        path = self.root / "state" / "worktrees" / "task-1" / ".tauceti-repair" / "errors.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_an_old_staging_owner_record_still_runs_every_check(self):
        """A task record from a controller that still names a check owner gets the complete check set."""
        self.service.tasks[1]["extra"]["checks_owner"] = "staging"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        checks = outcome["verification"][-1]["checks"]
        self.assertEqual(checks["status"], "passed")
        for name in ("audit", "lint", "module_system", "style_lint", "dot_notation"):
            self.assertEqual(checks[name]["rc"], 0, name)
        self.assertTrue((self.stub_dir / "audit-runs.log").exists())
        self.assertTrue((self.stub_dir / "module-system-runs.log").exists())
        self.assertTrue((self.stub_dir / "style-runs.log").exists())

    def test_every_checks_findings_return_together(self):
        self.findings(
            "audit-findings", "duplicate-declarations: TauCeti.Two.x is declared in both TauCeti.Two and TauCeti.Three"
        )
        self.findings(
            "lint-findings",
            'LINT-ENV-LOCATIONS {"version": 1, "findings": [{"linter": "simpNF", "declaration": "TauCeti.Two.y", '
            '"path": "TauCeti/Two.lean", "line": 3}]}',
        )
        self.findings(
            "style-findings", "TauCeti/Three.lean: Copyright too short!\nerror: TauCeti/Two.lean:1: Line too long"
        )
        self.findings(
            "dot-notation-findings",
            "TauCeti/Three.lean:2:0: error: dot-notation namespace lint: TauCeti.Nat.z puts a Mathlib type's namespace",
        )
        cfg = self.config()
        checks = repair_audit.check(
            self.content.clone,
            repair.lake_env(cfg.root),
            base=self.content.base,
            tools_root=cfg.root / "tools",
            logdir=cfg.root / "logs",
        )
        self.assertEqual(checks["status"], "failed")
        self.assertEqual(
            {
                name: checks[name]["findings"]
                for name in ("audit", "lint", "module_system", "style_lint", "dot_notation")
            },
            {"audit": 2, "lint": 1, "module_system": 0, "style_lint": 2, "dot_notation": 1},
        )
        self.assertEqual(
            {(d["kind"], d.get("linter")) for d in checks["diagnostics"]},
            {("audit", None), ("lint", "simpNF"), ("lint", "style"), ("lint", "dot-notation")},
        )
        settings = json.loads(
            repair_targeted.write(
                self.root / "targeted",
                candidate=self.content.tip,
                findings=repair.findings_from_diagnostics(checks["diagnostics"]),
            )
            .with_name(repair_targeted.SETTINGS)
            .read_text()
        )
        self.assertEqual((settings["linters"], settings["unchecked"]), (["simpNF"], True))

    def test_a_failed_final_build_is_still_rejected(self):
        cfg = self.config(verify_retries=0)
        run = repair_audit._sandboxed

        def fail_build(argv, *args, **kwargs):
            if "build" in argv:
                return {"rc": 1, "output": "TauCeti/Two.lean:1:1: error: final build failed"}
            return run(argv, *args, **kwargs)

        with patch.object(repair_audit, "_sandboxed", side_effect=fail_build):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["reason"], "agent_failed", outcome)
        self.assertEqual(self.service.results, [])

    def test_pre_rebase_retains_full_check_receipt(self):
        onto = self.pre_rebase_task()["onto"]
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        notes = json.loads(self.service.results[0]["worker_notes"])
        receipt = notes["pre_rebase_verification"]
        self.assertEqual(
            receipt,
            {
                "tip": outcome["tip"],
                "base": onto,
                "tools": f"TauCetiProject/TauCetiTools@{TOOLS_REF}",
                "scope": "full",
            },
        )

    def test_lint_checkpoint_survives_lost_receipt_and_continues_on_a_new_claim(self):
        self.service.tasks[1].setdefault("extra", {})["can_checkpoint_repair"] = True
        cfg = self.config(poll_seconds=0, verify_retries=0)
        client = self.client(cfg)
        finding = {"kind": "lint", "path": "TauCeti/Two.lean", "message": "unused argument"}
        failed = {"ok": False, "build": {"rc": 0}, "checks": {"status": "failed"}, "diagnostics": [finding]}
        submit = client.submit

        def lost(*args, **kwargs):
            self.assertTrue(kwargs.get("checkpoint"))
            self.assertEqual(submit(*args, **kwargs)["reason"], "repair-checkpoint")
            raise repair_task.ServiceError("lost checkpoint reply")

        with (
            patch.object(repair_audit, "verify", return_value=failed),
            patch.object(client, "submit", side_effect=lost),
        ):
            pending = repair_task.claim_and_run(cfg, client)
        self.assertEqual(pending["status"], "delivery_pending", pending)
        tip = pending["repair_checkpoint"]["tip"]
        self.assertEqual(self.service.done, set())
        self.assertEqual(len(self.service.results), 1)
        before = (len(self.agent_calls()), len(self.lake_calls()))
        [saved] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(saved["reason"], "repair-checkpoint", saved)
        self.assertEqual((len(self.agent_calls()), len(self.lake_calls())), before)
        self.assertFalse((cfg.root / "worktrees/task-1").exists())

        os.environ["STUB_AGENT_MODE"] = "noop"
        passed = {"ok": True, "build": {"rc": 0}, "checks": {"status": "passed"}, "diagnostics": []}
        with patch.object(repair_audit, "verify", return_value=passed) as verify:
            resumed = repair_task.claim_and_run(cfg, client)
        self.assertEqual((resumed["status"], resumed["tip"]), ("adopted", tip), resumed)
        verify.assert_called_once()
        prompt = self.agent_calls()[-1]["prompt"]
        self.assertIn(tip[:12], prompt)
        self.assertIn("unused argument", prompt)

    def test_submission_preflight_failure_defers_before_inference(self):
        cfg = self.config()
        with patch.object(repair_task, "check_submission_access", return_value={"rc": 1}):
            failed = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(failed["reason"], "submission_unavailable", failed)
        self.assertEqual(self.agent_calls(), [])
        self.assertFalse(self.lake_builds(), "authentication is checked before compilation")
        retried = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(retried["status"], "adopted", retried)

    def test_lost_accepted_response_recovers_receipt_after_restart(self):
        cfg = self.config(poll_seconds=0)
        client = self.client(cfg)
        submit = client.submit
        first = True

        def lost(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                self.assertTrue(submit(*args, **kwargs)["accepted"])
            raise repair_task.ServiceError("response lost")

        with patch.object(client, "submit", side_effect=lost):
            pending = repair_task.claim_and_run(cfg, client)
        self.assertEqual(pending["status"], "delivery_pending", pending)
        before = (len(self.agent_calls()), len(self.lake_calls()))
        [recovered] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(recovered["status"], "adopted", recovered)
        self.assertEqual(recovered["response"]["reason"], "already-adopted")
        self.assertEqual((len(self.agent_calls()), len(self.lake_calls())), before)
        self.assertFalse((cfg.root / "worktrees/task-1").exists())

    def test_concurrent_checks_join_and_collect_both_failures(self):
        cfg = self.config()
        worktree = self.content.clone
        barrier = threading.Barrier(2)
        original = repair_audit._sandboxed

        def run(argv, *args, **kwargs):
            if any(str(a).endswith(("/audit-driver", "/lint-env.sh")) for a in argv):
                barrier.wait(timeout=5)
            return original(argv, *args, **kwargs)

        self.findings("audit-findings", "duplicate-declarations: Foo is declared in both TauCeti.One and TauCeti.Two")
        self.findings("lint-findings", "LINT-ENV: NEW finding TauCeti.Two unusedArguments foo")
        with patch.object(repair_audit, "_sandboxed", run):
            record = repair_audit.check(
                worktree,
                repair.lake_env(cfg.root),
                base=self.content.base,
                tools_root=cfg.root / "tools",
                logdir=cfg.root / "logs",
            )
        self.assertNotEqual(record["audit"]["rc"], 0)
        self.assertNotEqual(record["lint"]["rc"], 0)
        self.assertEqual({d["kind"] for d in record["diagnostics"]}, {"audit", "lint"})
        self.assertTrue(all(record[name]["finished_at"] for name in ("audit", "lint")))

    @unittest.skipUnless(Path("/proc/self/task").is_dir(), "requires Linux process accounting")
    def test_rss_counts_a_child_spawned_by_another_thread(self):
        child = 'import time; data=bytearray(48*1024**2); print("ready", flush=True); time.sleep(4)'
        script = 'import sys,subprocess,threading; t=threading.Thread(target=lambda:subprocess.run([sys.executable,"-c",sys.argv[1]]));t.start();t.join()'
        with subprocess.Popen(
            [sys.executable, "-c", script, child], stdout=subprocess.PIPE, text=True, start_new_session=True
        ) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                self.assertGreaterEqual(repair_audit.process_tree_rss(process.pid), 48 * 1024**2)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

    def test_lost_push_response_reconciles_the_remote_without_rebuilding(self):
        cfg = self.config(poll_seconds=0)
        client = self.client(cfg)
        push = repair_task.push_result

        def lost(*args, **kwargs):
            result = push(*args, **kwargs)
            self.assertTrue(result["ok"], result)
            return dict(result, ok=False, rc=1)

        with patch.object(repair_task, "push_result", side_effect=lost):
            pending = repair_task.claim_and_run(cfg, client)
        self.assertEqual(pending["status"], "delivery_pending", pending)
        before = (len(self.agent_calls()), len(self.lake_calls()))
        [recovered] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(recovered["status"], "adopted", recovered)
        self.assertEqual((len(self.agent_calls()), len(self.lake_calls())), before)

    def test_failed_push_resumes_verified_delivery_without_agent_or_build(self):
        cfg = self.config(poll_seconds=0)
        client = self.client(cfg)
        with patch.object(repair_task, "push_result", return_value={"ok": False, "rc": 1}):
            failed = repair_task.claim_and_run(cfg, client)
        self.assertEqual(failed["status"], "delivery_pending", failed)
        before = (len(self.agent_calls()), len(self.lake_calls()))
        self.assertEqual(self.service.releases, [])
        self.assertTrue((cfg.root / "worktrees/task-1").exists())
        [outcome] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual((len(self.agent_calls()), len(self.lake_calls())), before)
        self.assertFalse((cfg.root / "worktrees/task-1").exists())

    def test_changed_checking_policy_reverifies_delivery_without_repair(self):
        cfg = self.config(poll_seconds=0, worker_audits=False)
        client = self.client(cfg)
        with patch.object(repair_task, "push_result", return_value={"ok": False, "rc": 1}):
            self.assertEqual(repair_task.claim_and_run(cfg, client)["status"], "delivery_pending")
        before = (len(self.agent_calls()), len(self.lake_calls()))
        cfg.worker_audits = True
        [outcome] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.agent_calls()), before[0])
        self.assertGreater(len(self.lake_calls()), before[1])
        self.assertEqual(outcome["verification"][-1]["checks"]["lint"]["scope"], "full")

    def test_prepare_runs_final_audits_and_reports_observed_duplicates(self):
        self.service.tasks[1]["kind"] = "prepare"
        self.service.tasks[1]["diagnostics"] = [
            {
                "kind": "admission_predictions",
                "complete": True,
                "records": [
                    {
                        "kind": "declaration_collision",
                        "name": "Foo.bar",
                        "sources": [{"pr": 2, "module": "TauCeti.Two"}, {"pr": 3, "module": "TauCeti.Three"}],
                    }
                ],
            }
        ]
        self.findings("audit-findings", DUPLICATE_LINE)
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        notes = json.loads(self.service.results[0]["worker_notes"])["admission_verification"]
        self.assertTrue(notes["checks_passed"])
        self.assertEqual(notes["tip"], outcome["tip"])
        self.assertEqual(notes["observed_duplicate_names"], ["Foo.bar"])
        self.assertFalse(outcome["verification"][0]["ok"])
        self.assertTrue(outcome["verification"][-1]["ok"])

    def test_a_duplicate_fails_the_verification_and_the_agent_is_run_on_it(self):
        self.findings("audit-findings", DUPLICATE_LINE)
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.service.results), 1, "the chain with the duplicate was never submitted")
        first, second = outcome["verification"]
        self.assertFalse(first["ok"])
        self.assertEqual(first["checks"]["status"], "failed")
        self.assertEqual(first["checks"]["tools"], f"TauCetiProject/TauCetiTools@{TOOLS_REF}")
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["tip"], outcome["tip"])
        # The audit driver ran in the sandbox, over the worktree's build, with CI's timeout.
        audits = [c["argv"] for c in self.bwrap_calls() if any(a.endswith("/audit-driver") for a in c["argv"])]
        self.assertEqual(len(audits), 2)
        self.assertIn("--unshare-net", audits[0])
        self.assertEqual(
            audits[0][audits[0].index("--") + 1 :][:6],
            ["lake", "env", "timeout", "--signal=TERM", "--kill-after=30", "3000s"],
        )
        # Full lint includes unchanged source when no frozen scope policy is available.
        self.assertEqual(
            (self.stub_dir / "lint-scope.txt").read_text().split(),
            ["TauCeti.A", "TauCeti.One", "TauCeti.Three", "TauCeti.Two"],
        )
        # The finding reached the agent as a diagnostic naming both modules, with their owners.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("The worker owns final verification", calls[0]["prompt"])
        prompt = calls[1]["prompt"]
        self.assertIn("The worker's verification of the chain failed", prompt)
        self.assertIn(DUPLICATE_LINE, prompt)
        self.assertIn("- `Foo.bar` is declared in both `TauCeti/Two.lean` (PR #2's commit", prompt)
        self.assertIn("and `TauCeti/Three.lean` (PR #3's commit", prompt)
        audit_errors = [e for e in self.errors_file() if e.get("kind") == "audit"]
        self.assertEqual(
            [(e["module"], e["file"], e["message"]) for e in audit_errors],
            [
                ("TauCeti.Two", "TauCeti/Two.lean", DUPLICATE_LINE),
                ("TauCeti.Three", "TauCeti/Three.lean", DUPLICATE_LINE),
            ],
        )

    def test_a_lint_finding_fails_the_verification(self):
        location = {"linter": "simpNF", "declaration": "two", "path": "TauCeti/Two.lean", "line": 1, "column": 0}
        self.findings("lint-findings", "LINT-ENV-LOCATIONS " + json.dumps({"version": 1, "findings": [location]}))
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertFalse(outcome["verification"][0]["ok"])
        self.assertEqual(outcome["verification"][0]["checks"]["lint"]["findings"], 1)
        prompt = self.agent_calls()[1]["prompt"]
        self.assertIn("- linter `simpNF`: `two` in `TauCeti/Two.lean` (PR #2's commit", prompt)

    def test_a_finding_that_persists_is_never_submitted(self):
        self.findings("audit-findings-always", DUPLICATE_LINE)
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "agent_failed")
        self.assertIn(DUPLICATE_LINE, outcome["detail"])
        self.assertEqual(self.service.results, [])
        self.assertEqual(len(self.agent_calls()), 1 + cfg.verify_retries)
        self.assertEqual(len(outcome["verification"]), 1 + cfg.verify_retries)

    def test_without_the_setting_nothing_is_checked(self):
        self.findings("audit-findings-always", DUPLICATE_LINE)
        cfg = self.config(worker_audits=False)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertNotIn("verification", outcome)
        self.assertFalse((self.stub_dir / "audit-runs.log").exists())

    def test_the_build_helper_runs_the_audits_after_a_green_whole_library_build(self):
        worktree = self.root / "helper"
        subprocess.run(["git", "clone", "--quiet", str(self.content.clone), str(worktree)], check=True)
        sh(worktree, "checkout", "--quiet", "--detach", self.content.tip)
        (worktree / "TauCeti" / "Two.lean").write_text("theorem two : True := trivial\n")
        script = repair_tools.write_build_script(worktree / ".tauceti-repair")
        repair_audit.write_settings(worktree, base=self.content.base, tools_root=self.root / "state" / "tauceti-tools")
        env = {**repair.lake_env(self.root / "state"), "STUB_DIR": str(self.stub_dir)}
        self.findings("audit-findings-always", DUPLICATE_LINE)

        def build(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([str(script), *args], cwd=worktree, env=env, capture_output=True, text=True)

        full = build()
        self.assertEqual(full.returncode, 1, full.stdout + full.stderr)
        self.assertIn(DUPLICATE_LINE, full.stdout)
        errors = [json.loads(line) for line in (worktree / ".tauceti-repair" / "errors.jsonl").read_text().splitlines()]
        self.assertEqual({e["module"] for e in errors}, {"TauCeti.Two", "TauCeti.Three"})
        self.assertEqual({e["kind"] for e in errors}, {"audit"})
        runs = len((self.stub_dir / "audit-runs.log").read_text().splitlines())
        skipped = build("--no-audit")
        self.assertEqual(skipped.returncode, 0, skipped.stdout + skipped.stderr)
        scoped = build("--dependents", "TauCeti.Two")
        self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
        self.assertIn("audits: CI's audits and lint run after a whole-library build", scoped.stdout)
        self.assertEqual(len((self.stub_dir / "audit-runs.log").read_text().splitlines()), runs)


class PreRebaseChecks(Fixture):
    BASE_FILES = WorkerAudits.BASE_FILES

    def setUp(self):
        super().setUp()
        os.environ[repair_audit.URL_VARIABLE] = str(TOOLS_REPOSITORY)
        self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)

    def test_waiting_pre_rebase_runs_full_checks(self):
        self.pre_rebase_task()
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        checks = outcome["verification"][-1]["checks"]
        self.assertEqual(checks["status"], "passed")
        self.assertEqual(checks["lint"]["scope"], "full")
        notes = json.loads(self.service.results[0]["worker_notes"])
        self.assertEqual(notes["pre_rebase_verification"]["scope"], "full")

    def test_a_resolution_that_fails_verification_is_repaired_then_submitted(self):
        """The rebased chain fails verification: the agent gets a repair round on it, and when that leaves
        the chain as it was, the chain is submitted and the whole chain's CI judges."""
        self.pre_rebase_task()
        os.environ["STUB_AGENT_MODE"] = "explain-only"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.agent_calls()), 2)
        self.assertNotIn("## Resolving the commit", self.agent_calls()[1]["prompt"])
        self.assertFalse(outcome["verification"][-1]["ok"])
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertIn("BROKEN", sh(self.content.bare, "show", f"{pushed}:TauCeti/Two.lean"))


class TargetedRepairChecks(Fixture):
    BASE_FILES = WorkerAudits.BASE_FILES

    def setUp(self):
        super().setUp()
        from tauceti_queue import repair_targeted

        os.environ[repair_audit.URL_VARIABLE] = str(TOOLS_REPOSITORY)
        self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)
        self.targeted = repair_targeted
        self.worktree = self.root / "targeted-worktree"
        subprocess.run(["git", "clone", "--quiet", str(self.content.clone), str(self.worktree)], check=True)
        sh(self.worktree, "checkout", "--quiet", "--detach", self.content.tip)
        (self.worktree / "TauCeti/Two.lean").write_text("theorem two : True := trivial\n")
        self.telemetry = self.root / "targeted-logs"
        repair_audit.write_settings(
            self.worktree, base=self.content.base, tools_root=self.root / "tools", logdir=self.telemetry
        )
        self.finding = {
            "kind": "lint",
            "module": "TauCeti.One",
            "path": "TauCeti/One.lean",
            "linter": "simpNF",
            "declaration": "one",
            "message": "simpNF: one",
        }

    def command(self, diagnostics=None):
        script = self.targeted.write(
            self.worktree / ".tauceti-repair",
            candidate=self.content.tip,
            findings=repair.findings_from_diagnostics(diagnostics or [self.finding]),
        )
        env = {**repair.lake_env(self.root / "targeted-cache"), "STUB_DIR": str(self.stub_dir)}
        return subprocess.run([str(script)], cwd=self.worktree, env=env, capture_output=True, text=True)

    def test_targeted_helper_builds_finding_and_repair_delta_then_only_reported_linter(self):
        repair_audit.write_settings(
            self.worktree,
            base=self.content.base,
            tools_root=self.root / "tools",
            logdir=self.telemetry,
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("return for the worker's full build, audits and lint", result.stdout)
        self.assertIn("build --iofail TauCeti.One TauCeti.Two", self.lake_calls())
        calls = [json.loads(line) for line in (self.stub_dir / "targeted-lint-runs.jsonl").read_text().splitlines()]
        self.assertEqual(calls, [{"modules": ["TauCeti.One", "TauCeti.Two"], "linters": ["simpNF"]}])
        self.assertFalse((self.stub_dir / "audit-runs.log").exists())
        self.assertNotIn("build --iofail", self.lake_calls(), "agent helper must not build the whole library")
        record = json.loads(next(self.telemetry.glob("targeted-*/result.json")).read_text())
        self.assertTrue(record["advisory"])
        self.assertEqual(record["status"], "passed")

    def test_focused_scope_includes_staged_and_untracked_repairs(self):
        path = self.worktree / "TauCeti/NewRepair.lean"
        path.write_text("theorem new_repair : True := trivial\n")
        sh(self.worktree, "add", "TauCeti/Two.lean")
        self.assertEqual(
            self.targeted.edited_modules(self.worktree, self.content.tip), {"TauCeti.Two", "TauCeti.NewRepair"}
        )

    def test_targeted_lint_failure_reaches_the_agent(self):
        (self.stub_dir / "targeted-lint-fail").touch()
        result = self.command()
        self.assertEqual(result.returncode, 1)
        self.assertIn("finding persists", result.stdout)

    def test_targeted_driver_cannot_silently_skip_the_check(self):
        (self.stub_dir / "targeted-no-marker").touch()
        self.assertEqual(self.command().returncode, 1)

    def test_failed_scoped_compilation_does_not_run_lint(self):
        (self.worktree / "TauCeti/Two.lean").write_text("BROKEN\n")
        self.assertEqual(self.command().returncode, 1)
        self.assertFalse((self.stub_dir / "targeted-lint-runs.jsonl").exists())

    def test_unknown_checks_report_partial_coverage(self):
        result = self.command([{"kind": "audit", "module": "TauCeti.One", "message": "disallowed axiom"}])
        self.assertEqual(result.returncode, 2)
        self.assertIn("some findings need worker verification", result.stdout)

    def test_duplicate_findings_use_only_the_ownership_driver(self):
        result = self.command([{"kind": "audit", "message": DUPLICATE_LINE}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [call["argv"] for call in self.bwrap_calls() if any(a.endswith("/audit-driver") for a in call["argv"])]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "--duplicates-only")

    def test_worker_owned_helper_rejects_a_redundant_full_suite(self):
        script = repair_tools.write_build_script(self.worktree / ".tauceti-repair", worker_verifies=True)
        result = subprocess.run([str(script)], cwd=self.worktree, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("full verification is owned by the worker", result.stdout)
        self.assertEqual(self.lake_calls(), [])

    def test_prompt_contract_requires_worker_verification_and_warm_repair_kind(self):
        task = repair_task.Task.from_json(self.service.task_record(1, self.service.preclaim(1, "token", "worker")))
        self.assertTrue(repair_task.prompt_context(task, None, worker_verifies=True).worker_verifies)
        self.assertFalse(repair_task.prompt_context(task, None, worker_verifies=False).worker_verifies)
        self.assertFalse(
            repair_task.prompt_context(task, None, worker_verifies=True, artifacts={"cold": True}).worker_verifies
        )
        for kind in ("pre_rebase", "rebase_conflict"):
            task.kind = kind
            self.assertTrue(repair_task.prompt_context(task, None, worker_verifies=True).worker_verifies)

    def test_interrupted_audit_keeps_its_phase_trace_outside_the_worktree(self):
        sandboxed = repair_audit._sandboxed

        def interrupt(argv, worktree, env, extra, log, timeout, **kwargs):
            if str(argv[-1]).endswith("/audit-driver"):
                Path(extra["AUDIT_PHASES_FILE"]).write_text(f"audit-import\t{time.time()}\n")
                raise KeyboardInterrupt
            return sandboxed(argv, worktree, env, extra, log, timeout, **kwargs)

        with patch.object(repair_audit, "_sandboxed", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            repair_audit.check(
                self.worktree,
                repair.lake_env(self.root / "cache"),
                base=self.content.base,
                tools_root=self.root / "tools",
                logdir=self.telemetry,
            )
        trace = next(self.telemetry.glob("audit-phases-*.tsv"))
        self.assertIn("audit-import", trace.read_text())
        (self.worktree / repair_audit.WORK_DIR / "audit-phases.tsv").unlink()
        self.assertIn("audit-import", trace.read_text(), "worktree removal must not remove the record")

    def test_phase_timing_is_best_effort_and_distinguishes_interruption(self):
        path = self.root / "phases.tsv"
        path.write_text("audit-import\t101\naudit-duplicates\t104\naudit-axioms\t109\n")
        run = {"started_at": 100, "finished_at": 115, "rc": 0}
        rows = repair_audit.audit_phases(path, run)
        self.assertEqual([r["seconds"] for r in rows], [3, 5, 6])
        self.assertTrue(all(r["complete"] for r in rows))
        self.assertFalse(repair_audit.audit_phases(path, {**run, "rc": 137})[-1]["complete"])
        path.write_text("invalid telemetry")
        self.assertEqual(repair_audit.audit_phases(path, run), [])
        path.unlink()
        self.assertEqual(repair_audit.audit_phases(path, run), [])


class KnownCheckRepairs(Fixture):
    """Located CI check findings start inference before any build or audit pass."""

    BASE_FILES = WorkerAudits.BASE_FILES

    def setUp(self):
        super().setUp()
        os.environ[repair_audit.URL_VARIABLE] = str(TOOLS_REPOSITORY)
        self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)

    def findings(self, name: str, text: str) -> None:
        (self.stub_dir / name).write_text(text + "\n")

    def known_checks_repair(self, diagnostics, *, stream_repair=True):
        self.service.tasks[1]["diagnostics"] = diagnostics
        cfg = self.config(stream_repair=stream_repair)
        run_agent = repair_task.run_repair_agent
        launches = []

        def agent_after_fetch(*args, **kwargs):
            # In particular, no cached preparation build or audit may block the
            # first inference call. The cache fetches still precede it.
            launches.append(True)
            if len(launches) == 1:
                self.assertTrue(self.lake_calls())
                self.assertFalse(any(c.startswith("build") for c in self.lake_calls()))
                self.assertFalse((self.stub_dir / "audit-runs.log").exists())
            return run_agent(*args, **kwargs)

        with (
            patch.object(repair_task, "run_repair_agent", agent_after_fetch),
            patch.object(repair_stream, "start_build", side_effect=AssertionError("unexpected discovery build")),
        ):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertIn("pre_repair_discovery", outcome)
        self.assertFalse(outcome["artifacts"]["incremental_build"])
        self.assertNotIn("stream_build", outcome)
        self.assertEqual(len(outcome["verification"]), 1)
        self.assertTrue(outcome["verification"][0]["ok"])
        self.assertTrue((self.stub_dir / "audit-runs.log").exists(), "final audits must run")
        self.assertEqual(self.lake_calls().count("build --iofail"), 1, "only the final verification build")
        checks = outcome["verification"][0]["checks"]
        self.assertEqual([p["name"] for p in checks["audit_phases"]], ["audit-import", "audit-duplicates"])
        self.assertTrue(all(p["seconds"] >= 0 for p in checks["audit_phases"]))
        self.assertTrue((self.root / "state/tasks/1/logs/audit-phases.tsv").exists())
        self.assertTrue(Path(checks["audit_phases_file"]).exists())
        self.assertTrue((self.root / "state/tasks/1/logs/checks-timing.json").exists())
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("Repair the reported findings first", prompt)
        self.assertIn("check-findings", prompt)
        self.assertIn("The worker owns final verification", prompt)
        self.assertNotIn("then run the whole-library", prompt)
        self.assertNotIn("exit status of the final `lake build --iofail`", prompt)
        self.assertIn("Cached outputs may still need unpacking", prompt)
        self.assertNotIn("every module of this batch that compiled are already built", prompt)
        return outcome

    def test_known_lint_starts_agent_before_build_and_audits(self):
        self.known_checks_repair(
            [
                {
                    "kind": "lint",
                    "module": "TauCeti.Two",
                    "path": "TauCeti/Two.lean",
                    "linter": "simpNF",
                    "declaration": "two",
                    "message": "simpNF: two",
                },
            ]
        )
        self.assertIn("linter `simpNF`: `two`", self.agent_calls()[0]["prompt"])

    def test_known_lint_skips_sequential_preparation_too(self):
        self.known_checks_repair(
            [
                {"kind": "environment_linter", "module": "TauCeti.Two", "message": "simpNF: two"},
            ],
            stream_repair=False,
        )

    def test_known_duplicate_starts_agent_before_build_and_audits(self):
        self.known_checks_repair([{"kind": "audit", "message": DUPLICATE_LINE}])
        self.assertIn("`Foo.bar` is declared in both", self.agent_calls()[0]["prompt"])

    def test_known_findings_do_not_allow_failed_final_audits_to_submit(self):
        self.service.tasks[1]["diagnostics"] = [
            {"kind": "lint", "module": "TauCeti.Two", "message": "simpNF: two"},
        ]
        self.findings("audit-findings-always", DUPLICATE_LINE)
        cfg = self.config(verify_retries=0)
        with patch.object(repair_stream, "start_build", side_effect=AssertionError("unexpected discovery")):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertFalse(outcome["verification"][0]["ok"])
        self.assertEqual(self.service.results, [])

    def test_known_findings_do_not_bypass_artifact_availability(self):
        self.service.tasks[1]["diagnostics"] = [
            {"kind": "lint", "module": "TauCeti.Two", "message": "simpNF: two"},
        ]
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual(self.service.results, [])

    def test_only_actionable_check_findings_bypass_discovery(self):
        task = repair_task.Task.from_json(self.service.task_record(1, self.service.preclaim(1, "token", "worker")))
        for diagnostics in (
            [],
            [{"kind": "admission_predictions", "proactive": True}],
            [{"kind": "lint", "message": "lint failed"}],
            [{"kind": "lint", "module": "TauCeti.Two"}],
            [{"kind": "audit", "module": "TauCeti.Two"}],
            [{"kind": "build", "module": "TauCeti.Two", "message": "build failed"}],
            [{"kind": "compiler", "module": "TauCeti.Two", "message": "unknown identifier"}],
        ):
            with self.subTest(diagnostics=diagnostics):
                task.diagnostics = diagnostics
                self.assertFalse(repair_task.known_check_failures(task))
        lint = {"kind": "lint", "module": "TauCeti.Two", "message": "simpNF: two"}
        task.diagnostics = [lint]
        self.assertTrue(repair_task.known_check_failures(task))
        task.diagnostics.append({"kind": "compiler", "module": "TauCeti.Three", "message": "broken"})
        self.assertFalse(repair_task.known_check_failures(task), "compiler discovery still collects all failures")
        task.diagnostics = [{"kind": "audit", "module": "TauCeti.Two", "message": "disallowed axiom in two"}]
        self.assertTrue(repair_task.known_check_failures(task))
        for kind in ("prepare", "pre_rebase", "rebase_conflict"):
            task.kind = kind
            task.diagnostics = [lint]
            self.assertFalse(repair_task.known_check_failures(task))


class StreamAudits(WorkerAudits):
    """A green streaming build runs the checks before the agent starts and lists their findings."""

    BROKEN = ()

    def test_findings_are_appended_to_the_error_list_before_the_agent_starts(self):
        self.findings("audit-findings", DUPLICATE_LINE)
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(outcome["stream_build"]["rc"], 0)
        self.assertEqual(outcome["stream_build"]["findings"], 2)
        launch = self.agent_calls()[0]["launch"]["errors"]
        audit = [e for e in launch if e.get("kind") == "audit"]
        self.assertEqual(
            [(e["module"], e["member"]) for e in audit],
            [("TauCeti.Two", f"{PROJECT}#2"), ("TauCeti.Three", f"{PROJECT}#3")],
        )
        self.assertEqual(launch[-1]["message"], "build finished, exit 0, 0 errors total, 2 audit or lint findings")
        self.assertTrue(outcome["verification"][-1]["ok"])

    # The scenarios of the parent class assume a failing streaming build.
    test_prepare_runs_final_audits_and_reports_observed_duplicates = None
    test_a_duplicate_fails_the_verification_and_the_agent_is_run_on_it = None
    test_a_lint_finding_fails_the_verification = None
    test_a_finding_that_persists_is_never_submitted = None
    test_without_the_setting_nothing_is_checked = None
    test_the_build_helper_runs_the_audits_after_a_green_whole_library_build = None


class BuildScript(unittest.TestCase):
    """The agent's scoped build, ``.tauceti-repair/build``, on a small fixture library with a fake ``lake``
    on PATH that records its arguments and exits with ``FAKE_LAKE_STATUS``."""

    LIBRARY = {
        "TauCeti.lean": "import TauCeti.A\nimport TauCeti.B\nimport TauCeti.C\nimport TauCeti.D\n",
        "TauCeti/A.lean": "theorem a : True := trivial\n",
        "TauCeti/B.lean": "import TauCeti.A\n\ntheorem b : True := trivial\n",
        "TauCeti/C.lean": "import TauCeti.B\nimport Mathlib.Logic.Basic\n\ntheorem c : True := trivial\n",
        "TauCeti/D.lean": "import TauCeti.A\n",
        ".lake/packages/mathlib/Mathlib/Other.lean": "import TauCeti.A\n",  # outside the library: not scanned
    }

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="scoped-build-", dir=TMP))
        for name, text in self.LIBRARY.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)
        self.script = repair_tools.write_build_script(self.root / ".tauceti-repair")
        # The standalone helper now includes the stale-holder recheck/guard.
        self.assertLess(len(self.script.read_text().splitlines()), 110)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "lake.log"
        lake = self.bin / "lake"
        lake.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\n"
            "open(os.environ['FAKE_LAKE_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('building', *sys.argv[3:]); sys.exit(int(os.environ.get('FAKE_LAKE_STATUS', '0')))\n"
        )
        lake.chmod(0o755)

    def build(self, *args: str, status: int = 0) -> tuple[int, str]:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LAKE_LOG": str(self.log)}
        env["FAKE_LAKE_STATUS"] = str(status)
        env[repair_sandbox.DISABLE_VARIABLE] = "0"  # SandboxedBuilds runs the script through bwrap
        result = subprocess.run([str(self.script), *args], cwd=self.root, env=env, text=True, capture_output=True)
        return result.returncode, result.stdout

    def lake_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_modules_and_paths_name_the_modules_to_build(self):
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertEqual(self.build("TauCeti/A.lean", "TauCeti.B")[0], 0)
        self.assertEqual(self.lake_calls(), [["build", "--iofail", "TauCeti.A", "TauCeti.B"]])
        self.assertEqual(self.build()[0], 0)
        self.assertEqual(self.lake_calls()[-1], ["build", "--iofail"], "no arguments builds the whole library")

    def test_dependents_come_from_the_import_lines_once(self):
        rc, out = self.build("TauCeti/B.lean", "--dependents")
        self.assertEqual(rc, 0)
        self.assertEqual(
            sorted(self.lake_calls()[-1]), sorted(["build", "--iofail", "TauCeti.B", "TauCeti", "TauCeti.C"])
        )
        cache = json.loads((self.root / ".tauceti-repair" / "imports.json").read_text())
        self.assertEqual(cache["TauCeti.C"], ["TauCeti.B", "Mathlib.Logic.Basic"])
        self.assertNotIn("Mathlib.Other", "".join(cache), "files under .lake are not the library")
        self.assertEqual(sorted(cache), ["TauCeti", "TauCeti.A", "TauCeti.B", "TauCeti.C", "TauCeti.D"])
        # The cache is read, not recomputed: an edit to a module's imports is not seen until it is dropped.
        (self.root / "TauCeti" / "D.lean").write_text("import TauCeti.B\n")
        self.build("TauCeti.B", "--dependents")
        self.assertEqual(
            sorted(self.lake_calls()[-1]), sorted(["build", "--iofail", "TauCeti.B", "TauCeti", "TauCeti.C"])
        )
        repair_tools.write_build_script(self.root / ".tauceti-repair")
        self.assertFalse((self.root / ".tauceti-repair" / "imports.json").exists(), "rewriting drops the cache")
        self.build("TauCeti.A", "--dependents")
        self.assertEqual(self.lake_calls()[-1][:3], ["build", "--iofail", "TauCeti.A"], "the named modules come first")
        self.assertEqual(set(self.lake_calls()[-1][3:]), {"TauCeti", "TauCeti.B", "TauCeti.C", "TauCeti.D"})

    def test_the_build_waits_for_the_lock_and_takes_it(self):
        lock = self.root / ".tauceti-repair" / "build.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))  # held by a live process: us
        threading.Timer(1.0, lambda: (lock / "pid").unlink()).start()
        threading.Timer(1.1, lock.rmdir).start()
        started = time.monotonic()
        rc, out = self.build("TauCeti.A")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertIn(f"waiting for .tauceti-repair/build.lock (held by pid {os.getpid()})", out)
        self.assertIn("building TauCeti.A", out)
        self.assertEqual(self.lake_calls(), [["build", "--iofail", "TauCeti.A"]])
        self.assertFalse(lock.exists(), "released after the build")
        # The worker's own builds share the lock: while build_lock holds it, the script waits.
        with repair_tools.build_lock(self.root) as held:
            self.assertEqual(held, lock)
            self.assertEqual((lock / "pid").read_text(), str(os.getpid()))
            proc = subprocess.Popen(
                [str(self.script), "TauCeti.B"],
                cwd=self.root,
                env={
                    **os.environ,
                    "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "FAKE_LAKE_LOG": str(self.log),
                    repair_sandbox.DISABLE_VARIABLE: "0",
                },
                text=True,
                stdout=subprocess.PIPE,
            )
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "the script waits while the worker builds")
            self.assertEqual(len(self.lake_calls()), 1)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.lake_calls()[-1], ["build", "--iofail", "TauCeti.B"])

    def test_a_lock_whose_holder_died_is_cleared(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        lock = self.root / ".tauceti-repair" / "build.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(dead.pid))
        started = time.monotonic()
        self.assertEqual(self.build("TauCeti.A")[0], 0)
        self.assertLess(time.monotonic() - started, 2.0, "no polling interval is spent on a dead holder")
        self.assertEqual(self.lake_calls(), [["build", "--iofail", "TauCeti.A"]])
        self.assertFalse(lock.exists())
        self.assertEqual([p.name for p in lock.parent.iterdir() if "stale" in p.name], [])

    def test_the_exit_status_is_lake_s(self):
        rc, out = self.build("TauCeti.C", status=3)
        self.assertEqual(rc, 3)
        self.assertIn("building TauCeti.C", out, "Lake's output streams through")
        self.assertFalse((self.root / ".tauceti-repair" / "build.lock").exists(), "released on failure too")


class RepairContextTests(unittest.TestCase):
    """``repair_context.compute``: the files and declarations of each member, the collisions and rename
    ripples between them, and the prompt section, on small repositories built here with
    ``(#N)``-suffixed squash commits (what ``read_chain`` reads)."""

    def chain_repo(self, prefix: str, base_files: dict[str, str], members: dict[int, dict[str, str]]):
        """A repository whose ``main`` is ``base_files`` and then one squash commit per member, in
        number order, checked out detached at the tip; ``(repo, base, chain)``."""
        repo = Path(tempfile.mkdtemp(prefix=prefix, dir=TMP)) / "repo"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
        sh(repo, "config", "user.name", "Fixture")
        sh(repo, "config", "user.email", "fixture@example.invalid")
        base = commit(repo, "base", base_files)
        for number, files in members.items():
            commit(repo, f"feat: member {number} (#{number})\n\nSource-PR: {PROJECT}#{number}", files)
        tip = sh(repo, "rev-parse", "HEAD")
        sh(repo, "checkout", "--quiet", "--detach", tip)
        return repo, base, repair.read_chain(repo, base, tip)

    def test_compiled_added_names_override_only_clean_accepted_source(self):
        import dataclasses

        from tauceti_queue import declaration_names as dn

        repo, base, chain = self.chain_repo(
            "compiled-context-",
            {},
            {
                1: {"TauCeti/One.lean": "namespace Foo\nclass One where\n  x : Nat\nend Foo\n"},
                2: {"TauCeti/Two.lean": "theorem repaired : True := trivial\n"},
            },
        )
        first_head, second_head = "a" * 40, "b" * 40
        chain = [
            dataclasses.replace(chain[0], accepted_head=first_head),
            dataclasses.replace(chain[1], accepted_head=second_head),
        ]

        def record(head, module):
            return dn.validate(
                dict(
                    version=1, head=head, base=base, tested=chain[-1].sha, complete=True, added={module: ["Foo.One.x"]}
                ),
                head,
            )

        records = {1: record(first_head, "TauCeti.One"), 2: record(second_head, "TauCeti.Two")}
        context = repair_context.compute(repo, chain, base, repo, declaration_names=records)
        self.assertIsNone(context.error)
        self.assertEqual(context.members[0].added, {"Foo.One.x": "TauCeti/One.lean"})
        self.assertEqual(context.collisions[0]["name"], "Foo.One.x")
        self.assertFalse(context.members[0].decls_record()["added_approximate"])
        self.assertTrue(context.members[0].decls_record()["removed_approximate"])
        self.assertIn("compiled `DECLARATION-NAMES`", "\n".join(context.prompt_lines()))
        # Existing agent changes supersede the accepted additions, preserving them only as evidence.
        context = repair_context.compute(repo, chain, base, repo, declaration_names=records, edited_prs={2})
        self.assertEqual(context.members[1].added, {"repaired": "TauCeti/Two.lean"})
        self.assertTrue(context.members[1].decls_record()["added_approximate"])
        self.assertEqual(context.members[1].decls_record()["compiled_added"], {"TauCeti.Two": ["Foo.One.x"]})
        self.assertEqual(context.collisions, [])
        for missing in [
            {},
            {1: dict(records[1], head=second_head)},
            {1: {"state": "incomplete", "reason": "producer_incomplete"}},
        ]:
            context = repair_context.compute(repo, chain, base, repo, declaration_names=missing)
            self.assertTrue(context.members[0].added_approximate)
            self.assertIn("Foo.One", context.members[0].added)
        cross = repair_context.cross_batch(
            repo,
            chain[:1],
            chain[1:],
            head_declaration_names={1: records[1]},
            following_declaration_names={2: records[2]},
        )
        self.assertEqual(cross["added_by_both"][0]["name"], "Foo.One.x")

    def test_declarations_follow_namespaces_and_skip_comments(self):
        text = (
            "import TauCeti.A\n/-! module doc with theorem fake_one : True -/\nnamespace Foo.Bar\nsection\n"
            "/-- doc -/\n@[simp] protected theorem one (n : Nat) : n = n := rfl\n-- theorem fake_two : True\n"
            "noncomputable def two.{u} : Type u := PUnit\ninstance : Inhabited Nat := ⟨0⟩\n"
            "instance (priority := 100) three : Inhabited Nat := ⟨0⟩\nprivate def hidden := 1\n"
            "theorem _root_.four : True := trivial\nend\nclass inductive Five | a\nend Foo.Bar\n"
            "structure Six where\n  x : Nat\nmutual\ndef seven : Nat := 0\nend\n/- a /- nested -/ theorem fake_three -/\n"
        )
        self.assertEqual(
            repair_context.declarations(text),
            {
                "Foo.Bar.one": "one",
                "Foo.Bar.two": "two",
                "Foo.Bar.three": "three",
                "four": "_root_.four",
                "Foo.Bar.Five": "Five",
                "Six": "Six",
                "seven": "seven",
            },
        )
        self.assertEqual(
            repair_context.rename_pairs({"Foo.b_old", "unrelated_zzz"}, {"Foo.b_new", "Bar.b_old", "fresh"}),
            [("Foo.b_old", "Bar.b_old")],  # the shared suffix outscores the shared prefix; one use per name
        )

    def test_collision_and_rename_ripple_are_reported_with_their_members(self):
        repo, base, chain = self.chain_repo(
            "context-",
            {
                "TauCeti/A.lean": "theorem a : True := trivial\n",
                "TauCeti/B.lean": "namespace Foo\n/-- doc -/\ntheorem b_old : True := trivial\nend Foo\n",
                "TauCeti/C.lean": "import TauCeti.B\ntheorem c : True := Foo.b_old\n",
                "TauCeti/D.lean": "-- Foo.b_old only in a comment\ntheorem d : True := trivial\n",
            },
            {
                1: {
                    "TauCeti/One.lean": "namespace Foo\ntheorem shared : True := trivial\nend Foo\n",
                    "README.md": "1\n",
                },
                2: {"TauCeti/B.lean": "namespace Foo\n/-- doc -/\ntheorem b_new : True := trivial\nend Foo\n"},
                3: {
                    "TauCeti/Three.lean": "import TauCeti.B\nnamespace Foo\ntheorem shared : True := trivial\n"
                    "theorem three : True := b_old\nend Foo\n"
                },
                4: {"TauCeti/A.lean": "theorem a : True := by trivial\n"},  # a changed signature is not an addition
            },
        )
        context = repair_context.compute(repo, chain, base, repo)
        self.assertIsNone(context.error)
        self.assertEqual(
            [m.files for m in context.members],
            [["README.md", "TauCeti/One.lean"], ["TauCeti/B.lean"], ["TauCeti/Three.lean"], ["TauCeti/A.lean"]],
        )
        self.assertEqual(
            context.collisions,
            [
                {
                    "kind": "collision",
                    "name": "Foo.shared",
                    "added_by": [
                        {"position": 1, "pr": 1, "file": "TauCeti/One.lean"},
                        {"position": 3, "pr": 3, "file": "TauCeti/Three.lean"},
                    ],
                }
            ],
        )
        self.assertEqual(
            context.ripples,
            [
                {
                    "kind": "ripple",
                    "name": "Foo.b_old",
                    "removed_by": [{"position": 2, "pr": 2, "file": "TauCeti/B.lean", "renamed_to": "Foo.b_new"}],
                    "references": [
                        {"file": "TauCeti/Three.lean", "position": 3, "pr": 3, "lines": [4]},
                        {"file": "TauCeti/C.lean", "main": True, "lines": [2]},
                    ],
                }
            ],
        )
        self.assertEqual(context.summary()["added"], 4)  # Foo.shared twice, Foo.b_new, Foo.three; not `a`
        # The files, one object per member in chain order, ignored by Git once the listings path is set up.
        where = repo / ".tauceti-repair"
        files = [json.loads(line) for line in (where / "files.jsonl").read_text().splitlines()]
        self.assertEqual([f["position"] for f in files], [1, 2, 3, 4])
        self.assertEqual(
            files[0], {"position": 1, "pr": 1, "commit": chain[0].sha, "files": ["README.md", "TauCeti/One.lean"]}
        )
        decls = [json.loads(line) for line in (where / "decls.jsonl").read_text().splitlines()]
        self.assertEqual(
            decls[1],
            {
                "position": 2,
                "pr": 2,
                "commit": chain[1].sha,
                "approximate": True,
                "added": ["Foo.b_new"],
                "removed": ["Foo.b_old"],
                "renamed": [["Foo.b_old", "Foo.b_new"]],
            },
        )
        self.assertEqual(decls[3]["added"], [])
        records = [json.loads(line) for line in (where / "collisions.jsonl").read_text().splitlines()]
        self.assertEqual([r["kind"] for r in records], ["collision", "ripple"])
        # The prompt section, next to the landed members, naming positions, PRs and files.
        lines = context.prompt_lines()
        self.assertEqual(lines[1], "## Likely collisions and renames")
        self.assertIn(
            "- `Foo.shared` is added by position 1 (PR #1, `TauCeti/One.lean`) and position 3 (PR #3, `TauCeti/Three.lean`)",
            lines,
        )
        self.assertIn(
            "- `Foo.b_old` is removed by position 2 (PR #2, `TauCeti/B.lean`, renamed to `Foo.b_new`) and still "
            "referenced in `TauCeti/Three.lean` (position 3, PR #3) and `main`'s `TauCeti/C.lean` (changed by no member)",
            lines,
        )
        task = repair_task.Task.from_json(
            {**Content(repo.parent).task(), "claim_token": "x", "expires_at": time.time() + 60}
        )
        prompt_context = repair_task.prompt_context(task, None, listings=repair.listings_path(repo))
        prompt_context.repair_context = context
        prompt = repair.compose_prompt(prompt_context, repair_task.task_findings(task), chain, repo)
        self.assertIn("\n".join(lines), prompt)
        self.assertLess(prompt.index("## Likely collisions and renames"), prompt.index("## The result"))
        self.assertIn("!! .tauceti-repair/", sh(repo, "status", "--ignored", "--porcelain"))
        # Without the context (a prompt composed before the worktree exists) the section is absent.
        prompt_context.repair_context = None
        self.assertNotIn(
            "Likely collisions", repair.compose_prompt(prompt_context, repair_task.task_findings(task), chain, repo)
        )

    def test_a_clean_batch_says_so_and_a_large_one_stays_under_the_cap(self):
        repo, base, chain = self.chain_repo(
            "clean-",
            {"TauCeti/A.lean": "theorem a : True := trivial\n"},
            {
                1: {"TauCeti/One.lean": "theorem one : True := trivial\n"},
                2: {"TauCeti/Two.lean": "theorem two : True := a\n"},
            },
        )
        context = repair_context.compute(repo, chain, base, repo)
        self.assertEqual((context.collisions, context.ripples), ([], []))
        self.assertIn("No declaration name is added by two members", "\n".join(context.prompt_lines()))
        # 120 members, each adding a module; pairs declare the same name (60 collisions) and every
        # sixth member drops a base theorem another member and `main` still use (20 ripples).
        base_files = {f"TauCeti/Old{r}.lean": f"theorem old_{r} : True := trivial\n" for r in range(20)}
        base_files["TauCeti/Uses.lean"] = "".join(f"theorem uses_{r} : True := old_{r}\n" for r in range(20))
        members: dict[int, dict[str, str]] = {}
        for i in range(1, 121):
            text = f"namespace M{i}\ntheorem own_{i} : True := trivial\nend M{i}\ntheorem dup_{(i - 1) // 2} : True := trivial\n"
            if i % 3 == 0:
                text += f"theorem ref_{i} : True := old_{i % 20}\n"
            files = {f"TauCeti/M{i}.lean": text}
            if i % 6 == 0:
                files[f"TauCeti/Old{(i // 6) - 1}.lean"] = f"theorem renamed_{(i // 6) - 1} : True := trivial\n"
            members[i] = files
        repo, base, chain = self.chain_repo("large-", base_files, members)
        started = time.monotonic()
        context = repair_context.compute(repo, chain, base, repo)
        elapsed = time.monotonic() - started
        self.assertIsNone(context.error)
        self.assertEqual((len(context.collisions), len(context.ripples)), (60, 20))
        self.assertLess(elapsed, 20.0, f"compute took {elapsed:.1f}s for 120 members")
        lines = context.prompt_lines()
        bullets = [line for line in lines if line.startswith("- ")]
        self.assertEqual(len(bullets), repair_context.SECTION_LINES + 1)
        self.assertEqual(bullets[-1], "- and 40 more in `.tauceti-repair/collisions.jsonl`")
        self.assertLess(len("\n".join(lines)), 8_000)
        records = (repo / ".tauceti-repair" / "collisions.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 80)
        ripple = json.loads(records[-1])
        self.assertEqual(ripple["name"], "old_19")
        self.assertEqual(
            ripple["removed_by"],
            [{"position": 120, "pr": 120, "file": "TauCeti/Old19.lean", "renamed_to": "renamed_19"}],
        )
        self.assertIn({"file": "TauCeti/Uses.lean", "main": True, "lines": [20]}, ripple["references"])
        self.assertTrue(all(r.get("pr") in (39, 99) for r in ripple["references"] if not r.get("main")))


class EarlyPublication(Fixture):
    def test_lost_accepted_response_publishes_before_removing_surviving_outputs(self):
        cfg = self.config(poll_seconds=0, publish=self.settings)
        client = self.client(cfg)
        submit = client.submit
        first = True

        def lost(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                self.assertTrue(submit(*args, **kwargs)["accepted"])
            raise repair_task.ServiceError("response lost")

        with (
            patch.object(client, "submit", side_effect=lost),
            patch.object(repair_publish, "start_early", return_value=None),
        ):
            pending = repair_task.claim_and_run(cfg, client)
        self.assertEqual(pending["status"], "delivery_pending", pending)
        before = (len(self.agent_calls()), len(self.lake_builds()))
        [recovered] = repair_task.retry_deliveries(cfg, client)
        self.assertEqual(recovered["status"], "adopted", recovered)
        self.assertEqual((len(self.agent_calls()), len(self.lake_builds())), before)
        records = repair_publish.BACKGROUND.wait(60)
        self.assertEqual([r["state"] for r in records], ["published"], records)
        self.assertFalse((cfg.root / "worktrees/task-1").exists())

    def test_native_staging_rejects_paths_symlinks_and_special_files(self):
        stage = self.root / "untrusted-stage"
        stage.mkdir()
        mapping = stage / "outputs.jsonl"
        artifact = stage / "00aa00aa00aa00aa.ltar"
        artifact.write_bytes(b"compiled")
        good = '"2026-03-17"\n["11bb11bb11bb11bb","00aa00aa00aa00aa.ltar"]\n'
        mapping.write_text(good)
        repair_publish.validate_native_stage(stage)
        artifact.unlink()
        artifact.symlink_to(self.root / "key")
        with self.assertRaises(ValueError):
            repair_publish.validate_native_stage(stage)
        artifact.unlink()
        os.mkfifo(artifact)
        with self.assertRaises(ValueError):
            repair_publish.validate_native_stage(stage)
        mapping.write_text(good.replace("00aa00aa00aa00aa.ltar", "../key"))
        with self.assertRaises(ValueError):
            repair_publish.validate_native_stage(stage)
        mapping.unlink()
        os.mkfifo(mapping)
        with self.assertRaises(ValueError):
            repair_publish.validate_native_stage(stage)

    BASE_FILES = {
        "lakefile.toml": 'name = "TauCeti"\nplatformIndependent = true\n',
        "lean-toolchain": "leanprover/lean4:v4.36.0-rc2\n",
        "tauceti.toml": f'[tools.tools]\nrepo = "TauCetiProject/TauCetiTools"\nref = "{TOOLS_REF}"\n',
        "scripts/lint-baseline.txt": "",
        ".gitignore": ".lake/\n",
    }

    def setUp(self):
        super().setUp()
        os.environ[repair_audit.URL_VARIABLE] = str(TOOLS_REPOSITORY)
        self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)
        key = self.root / "cache-key"
        key.write_text("test-id:test-secret")
        self.settings = repair_publish.Settings(url="https://repair-cache.example", key_file=key)

    def test_upload_overlaps_audits_and_is_reused_without_waiting_for_completion(self):
        gate = self.stub_dir / "staged-upload-gate"
        gate.touch()
        self.addCleanup(lambda: gate.unlink(missing_ok=True))
        audit = repair_audit._sandboxed
        seen = []

        def checking(argv, *args, **kwargs):
            if any(str(a).endswith("/audit-driver") for a in argv):
                deadline = time.monotonic() + 10
                while not (self.stub_dir / "staged-upload-started").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((self.stub_dir / "staged-upload-started").exists())
                self.assertEqual(self.service.results, [], "checks still block submission")
                seen.append(True)
            return audit(argv, *args, **kwargs)

        cfg = self.config(publish=self.settings)
        try:
            with patch.object(repair_audit, "_sandboxed", side_effect=checking):
                outcome = repair_task.claim_and_run(cfg, self.client(cfg))
            self.assertEqual(outcome["status"], "adopted", outcome)
            self.assertTrue(seen)
            self.assertTrue(outcome["publish"]["early"])
            self.assertFalse((self.stub_dir / "remote/revisions").exists(), "submission does not wait for upload")
            self.assertEqual(len(repair_publish.BACKGROUND.running), 1, "no duplicate late publisher")
            self.assertFalse((cfg.root / "worktrees/task-1").exists(), "frozen uploader does not retain source")
        finally:
            gate.unlink(missing_ok=True)
        records = repair_publish.BACKGROUND.wait(20)
        self.assertTrue(records[0]["published"], records)
        self.assertTrue((self.stub_dir / "remote/revisions" / (outcome["tip"] + ".jsonl")).is_file())
        self.assertEqual(list((cfg.root / "worktrees").glob("task-1-publish-*")), [])
        self.assertNotIn("cache put ", "\n".join(self.lake_calls()))
        self.assertIsNone(self.agent_calls()[0]["env"]["LAKE_CACHE_KEY"])

    def test_full_check_failures_still_block_submission_despite_early_upload(self):
        (self.stub_dir / "audit-findings-always").write_text(DUPLICATE_LINE + "\n")
        cfg = self.config(publish=self.settings, verify_retries=0)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "released")
        self.assertEqual(self.service.results, [])
        records = repair_publish.BACKGROUND.wait(20)
        self.assertTrue(records and records[0]["published"], records)
        self.assertFalse(outcome["verification"][0]["ok"])

    def test_frozen_upload_uses_captured_revision_after_source_worktree_is_removed(self):
        worktree = self.root / "state/worktrees/task-7"
        worktree.mkdir(parents=True)
        (worktree / "lakefile.toml").write_text('name = "TauCeti"\nplatformIndependent = true\n')
        (worktree / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc2\n")
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        sh(worktree, "add", "-A")
        sh(worktree, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "base")
        revision = sh(worktree, "rev-parse", "HEAD")
        logdir = self.root / "state/tasks/7/logs"
        repair_publish.write_early_settings(
            worktree, settings=self.settings, logdir=logdir, task_id="7", repository=PROJECT, lake=("lake",)
        )
        (worktree / ".lake").mkdir(exist_ok=True)
        subprocess.run(
            ["lake", "build", "--iofail", "--rehash", "-o", ".lake/outputs.jsonl"],
            cwd=worktree,
            env=repair.lake_env(self.root / "state"),
            check=True,
            capture_output=True,
        )
        with patch.object(repair_publish, "_spawn") as spawn:
            record = repair_publish.start_early(worktree, repair.lake_env(self.root / "state"))
            spec_path = Path(spawn.call_args.args[0][-1])
        self.assertTrue(record["launched"], record)
        shutil.rmtree(worktree)
        spec = json.loads(spec_path.read_text())
        spec["settings"]["key_file"] = str(self.settings.key_file)
        # The child receives Lake config/cache settings from systemd or the subprocess env.
        with patch.dict(os.environ, repair.lake_env(self.root / "state")):
            self.assertEqual(repair_publish.run_spec(spec_path), 0)
        result = json.loads(Path(record["record"]).read_text())
        self.assertTrue(result["published"], result)
        self.assertEqual(result["revision"], revision)
        self.assertFalse(Path(spec["frozen"]["path"]).exists())

    def test_later_commit_revision_does_not_reuse_an_older_upload(self):
        logdir = self.root / "publication-lookup"
        old_revision = "a" * 40
        path = logdir / "publications" / old_revision / "publish.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"revision": old_revision, "published": True, "state": "published"}))
        self.assertIsNotNone(repair_publish.early_record(logdir, old_revision, self.settings))
        self.assertIsNone(repair_publish.early_record(logdir, "b" * 40, self.settings))
        path.write_text(json.dumps({"revision": old_revision, "published": False, "state": "failed"}))
        self.assertIsNone(repair_publish.early_record(logdir, old_revision, self.settings))

    def test_failed_early_start_falls_back_to_final_publication(self):
        spawn = repair_publish._spawn
        calls = []

        def launching(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("temporary spawn failure")
            return spawn(*args, **kwargs)

        cfg = self.config(publish=self.settings)
        with patch.object(repair_publish, "_spawn", side_effect=launching):
            outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(calls), 2)
        self.assertTrue(repair_publish.BACKGROUND.wait(20)[0]["published"])
        self.assertEqual(list((cfg.root / "worktrees").glob("task-1-publish-*")), [])


class ToolsMainPin(unittest.TestCase):
    """``ref = "main"`` follows TauCetiTools main; each audit checks out its current commit."""

    def test_main_resolves_to_the_branch_tip_and_moves_with_it(self):
        self.assertEqual(
            repair_audit.parse_tools_pin('[tools.tools]\nrepo = "TauCetiProject/TauCetiTools"\nref = "main"\n'),
            ("TauCetiProject/TauCetiTools", "main"),
        )
        with self.assertRaises(repair_audit.Unavailable):
            repair_audit.parse_tools_pin('[tools.tools]\nrepo = "TauCetiProject/TauCetiTools"\nref = "develop"\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, first = make_tools_repository(root / "tools")
            sh(repo, "branch", "-M", "main")
            os.environ[repair_audit.URL_VARIABLE] = str(repo)
            self.addCleanup(os.environ.pop, repair_audit.URL_VARIABLE, None)
            checkout = repair_audit.tools_checkout(root / "checkouts", "TauCetiProject/TauCetiTools", "main")
            self.assertEqual(checkout.name, first)
            (repo / "tauceti_tools" / "moved.py").write_text("")
            sh(repo, "add", "-A")
            sh(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "move")
            moved = repair_audit.tools_checkout(root / "checkouts", "TauCetiProject/TauCetiTools", "main")
            self.assertEqual(moved.name, sh(repo, "rev-parse", "HEAD"))
            self.assertNotEqual(moved.name, first)
            with self.assertRaises(repair_audit.Unavailable):
                repair_audit.tools_checkout(root / "checkouts", "TauCetiProject/TauCetiTools", "absent")


# A stand-in for TauCetiTools' `numproj_fix.py`. Run, it renames `.1`/`.2` after an identifier or
# `)` to `.fst`/`.snd` in the files it is given, logs its call and exits with RC. As a module it
# offers what the worker checks a rewrite with: `apply`, and a lexer for `--` comments and strings.
FAKE_NUMPROJ = r"""import os, re, sys
from pathlib import Path
CODE = "c"


def line_lexes(lines):
    out = []
    for line in lines:
        kinds, quoted = [], False
        for i, ch in enumerate(line):
            quoted = quoted != (ch == '"')
            kinds.append("m" if "--" in line[: i + 1] else "s" if quoted or ch == '"' else CODE)
        out.append((kinds, set(), True))
    return out


def apply(findings):
    for path, edits in findings.items():
        lines = Path(path).read_text().split("\n")
        for line, col, field, span in sorted(edits, key=lambda e: (e[0], -e[1])):
            text = lines[line - 1]
            digits = re.match(r"\d+", text[col:]).group(0)
            text = text[:col] + field + text[col + len(digits) :]
            if span:
                text = text[: span[0]] + "(" + text[span[0] : span[1]] + ")" + text[span[1] :]
            lines[line - 1] = text
        Path(path).write_text("\n".join(lines))
    return 0, []


if __name__ == "__main__":
    files = [a for a in sys.argv[1:] if a.endswith(".lean")]
    with open(Path(os.environ["STUB_DIR"]) / "numproj-calls.log", "a") as log:
        log.write(" ".join(sys.argv[1:]) + " cache=" + os.environ.get("TAUCETI_NUMPROJ_CACHE", "") + "\n")
    for name in files:
        path = Path(name)
        text = re.sub(r"(?<=[\w)])\.([12])\b", lambda m: ".fst" if m.group(1) == "1" else ".snd", path.read_text())
        path.write_text(EDIT(text))
    for name in files:
        print(f"numproj: {name}:2:3: not rewritten, as a test; rewrite it by hand") if RC == 1 else None
    sys.exit(RC)
"""

OLD_P = "theorem p (h : A ∧ B) : A := h.1\ndef q (x : Nat × Nat) : Nat := x.2\ndef r : Nat := Nat.zero\n"
NEW_P = OLD_P.replace("h.1", "h.fst").replace("x.2", "x.snd")

# The installed TauCetiTools' numproj, when it has one (TauCetiProject/TauCetiTools#314), for the checks
# against the real tool's rewrites; TAUCETI_TEST_NUMPROJ_DIR names another copy of its scripts.
REAL_NUMPROJ = (
    Path(os.environ["TAUCETI_TEST_NUMPROJ_DIR"])
    if os.environ.get("TAUCETI_TEST_NUMPROJ_DIR")
    else repair_numproj.tool_dir()
)


class NumprojBase(Fixture):
    """Main renamed the numeric projections of ``TauCeti/P.lean``; member 2 edits a line next to them
    with the old syntax, so replaying it conflicts. The pre-pass (``repair_numproj``) rewrites both
    sides with the tool (here a stand-in) and merges them before any agent runs."""

    BROKEN = ()
    BASE_FILES = {"TauCeti/P.lean": OLD_P}
    MEMBER_FILES = {2: {"TauCeti/P.lean": OLD_P.replace("x.2", "x.2 + 1")}}

    def setUp(self):
        super().setUp()
        os.environ["STUB_AGENT_MODE"] = "explain-only"
        self.use_tool()

    def use_tool(self, rc: int = 0, edit: str = "lambda text: text", files=repair_numproj.TOOL_FILES) -> None:
        tool = self.root / f"tool-{secrets.token_hex(3)}"
        tool.mkdir()
        for name in files:
            (tool / name).write_text("")
        if "numproj_fix.py" in files:
            (tool / "numproj_fix.py").write_text(FAKE_NUMPROJ.replace("RC", str(rc)).replace("EDIT", f"({edit})"))
        patcher = patch.object(repair_numproj, "tool_dir", lambda: tool if "numproj_fix.py" in files else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def task_onto(self, main_p: str) -> dict:
        seed = self.content.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.content.base)
        onto = commit(seed, "main renames projections", {"TauCeti/P.lean": main_p})
        sh(seed, "push", "--quiet", "origin", f"{onto}:refs/heads/queue/batch-1/onto")
        raw = self.content.task(1, kind="rebase_conflict")
        raw.update(onto=onto, diagnostics=[])
        raw["retained_refs"] = [*raw["retained_refs"], "refs/heads/queue/batch-1/onto"]
        self.service.tasks = {1: raw}
        self.onto = onto
        return raw

    def run_task(self, main_p: str = NEW_P) -> tuple[dict, list]:
        self.task_onto(main_p)
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        return outcome, chain_tool.chain_commits(self.content.bare, self.onto, pushed)

    def numproj_calls(self) -> list[str]:
        path = self.stub_dir / "numproj-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def resolved_p(self, chain) -> str:
        return sh(self.content.bare, "show", f"{chain[1].sha}:TauCeti/P.lean") + "\n"


class NumprojImports(NumprojBase):
    """A rebase task's tree is not built when it is prepared: the project modules the conflicting file
    imports are built before the tool runs, so it can elaborate the file; Mathlib's modules are not."""

    IMPORTING = "public import Mathlib.Logic.Basic\nimport TauCeti.A\n"
    BASE_FILES = {"TauCeti/P.lean": IMPORTING + OLD_P}
    MEMBER_FILES = {2: {"TauCeti/P.lean": IMPORTING + OLD_P.replace("x.2", "x.2 + 1")}}

    def test_the_imported_project_modules_are_built_before_the_tool_runs(self):
        outcome, chain = self.run_task(self.IMPORTING + NEW_P)
        self.assertEqual(outcome["conflicts"][0]["numproj"], True)
        self.assertEqual(self.agent_calls(), [])
        builds = [c for c in self.lake_calls() if c.startswith("build")]
        self.assertIn("build TauCeti.A", builds)
        self.assertFalse([b for b in builds if "Mathlib" in b], builds)

    def test_the_header_is_parsed_with_comments_and_modifiers(self):
        text = (
            "/-\nCopyright\nimport Not.This\n-/\nmodule\n\n  public import Mathlib.Logic.Basic\n"
            "import /- inline -/ TauCeti.A -- a comment\nmeta import all TauCeti.B\n\n/-! doc -/\n"
            "theorem t : True := trivial\nimport Not.Header\n"
        )
        nested = "/- outer /- inner -/ outer -/\nimport TauCeti.A\n-- /- not a block\nimport TauCeti.B\n"
        self.assertEqual(repair_numproj.header_imports(nested), ["TauCeti.A", "TauCeti.B"])
        self.assertEqual(repair_numproj.header_imports(text), ["Mathlib.Logic.Basic", "TauCeti.A", "TauCeti.B"])

    def test_a_conflicting_file_is_built_at_the_clean_prefix_and_restored(self):
        """The build of the imports sees the clean prefix's version of a conflicting file (a clean
        import may import it); git's markers are back in the worktree afterwards."""
        seen = {}
        original = repair.run_logged

        def run_logged(argv, cwd, log, env=None, timeout=None):
            if "numproj" in str(log) and str(log).endswith("-imports.log"):
                seen["P"] = (Path(cwd) / "TauCeti/P.lean").read_text()
            return original(argv, cwd, log, env=env, timeout=timeout)

        with patch.object(repair, "run_logged", run_logged):
            self.use_tool(rc=1)  # not mechanical: the agent sees the worktree the pre-pass leaves
            self.run_task(self.IMPORTING + NEW_P.replace("x.snd", "x.snd * 2"))
        self.assertNotIn("<<<<<<<", seen["P"])
        self.assertIn("x.snd * 2", seen["P"], "the clean prefix's version")

    def test_every_conflicting_file_is_built_at_the_clean_prefix(self):
        """A conflicting file the tool does not rewrite (no numeric projection) is swapped too: a module
        the build needs may import it."""
        repo = self.root / "swap"
        repo.mkdir()
        sh(repo, "init", "--quiet")
        sh(repo, "config", "user.name", "T")
        sh(repo, "config", "user.email", "t@example.invalid")
        prefix = commit(
            repo, "prefix", {"TauCeti/A.lean": "import TauCeti.Q\n", "TauCeti/Q.lean": "-- clean\n", "P.lean": "-- p\n"}
        )
        (repo / "TauCeti/Q.lean").write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")
        seen = {}

        def run_logged(argv, cwd, log, env=None, timeout=None):
            seen["Q"] = (Path(cwd) / "TauCeti/Q.lean").read_text()
            return {"rc": 0}

        with (
            patch.object(repair, "run_logged", run_logged),
            patch.object(repair, "apply_conflict", lambda *a: None),
            patch.object(repair_numproj.repair_sandbox, "sandboxed_lake", lambda argv, *a, **k: argv),
        ):
            built = repair_numproj._build_imports(
                repo,
                prefix,
                prefix,
                {"P.lean": (b"", b"import TauCeti.A\n", b"import TauCeti.A\n")},
                ["P.lean", "TauCeti/Q.lean"],
                {},
                self.root / "imports.log",
                60,
                ("lake",),
            )
        self.assertTrue(built)
        self.assertEqual(seen["Q"], "-- clean\n")


class NumprojPrepass(NumprojBase):
    def test_a_projection_only_conflict_is_resolved_without_the_agent(self):
        outcome, chain = self.run_task()
        self.assertEqual(self.agent_calls(), [], "no agent ran")
        self.assertEqual(outcome["conflicts"][0]["numproj"], True)
        self.assertEqual(self.resolved_p(chain), NEW_P.replace("x.snd", "x.snd + 1"))
        self.assertEqual(chain[1].parents, (chain[0].sha,))
        self.assertIn(f"Source-PR: {PROJECT}#2", chain[1].message)
        self.assertIn("Resolved mechanically by the repair worker", chain[1].message)
        calls = self.numproj_calls()
        self.assertEqual(len(calls), 2, "the parent's and the commit's versions, one run each")
        self.assertTrue(all("/.lake/numproj/run-" in c and "/.lake/numproj/cache" in c for c in calls), calls)
        sandboxed = [c["argv"][c["argv"].index("--") + 1 :] for c in self.bwrap_calls()]
        self.assertEqual(len([a for a in sandboxed if a[:2] == ["python3", "-I"]]), 2, "both runs are sandboxed")
        worktrees = list((self.root / "state" / "worktrees").glob("*/.lake/numproj"))
        self.assertTrue(worktrees and all(not list(w.glob("run-*")) for w in worktrees), "scratch is removed")

    def test_a_real_conflict_still_goes_to_the_agent_from_the_rewritten_merge(self):
        outcome, chain = self.run_task(NEW_P.replace("x.snd", "x.snd * 2"))
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        prompt = calls[0]["prompt"]
        self.assertIn("the worker rewrote numeric projections", prompt)
        self.assertIn(
            "These still have conflict markers, now between the clean prefix and the rewritten commit: "
            "`TauCeti/P.lean`",
            prompt,
        )
        self.assertNotIn("numproj", outcome["conflicts"][0])
        # The stand-in agent keeps both sides: the commit's side is the rewritten one.
        resolved = self.resolved_p(chain)
        self.assertIn("x.snd + 1", resolved)
        self.assertIn("x.snd * 2", resolved)
        self.assertNotIn("x.2", resolved)

    def test_a_tool_failure_leaves_the_conflict_to_the_agent_as_before(self):
        self.use_tool(rc=75)
        outcome, chain = self.run_task()
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("numeric projections", calls[0]["prompt"])
        self.assertEqual(len(self.numproj_calls()), 2)
        resolved = self.resolved_p(chain)
        self.assertIn("x.2 + 1", resolved, "the agent started from git's own markers")
        self.assertNotIn("Resolved mechanically", chain[1].message)

    def test_a_missing_tool_leaves_the_conflict_to_the_agent(self):
        self.use_tool(files=("NumProjScan.lean",))
        outcome, chain = self.run_task()
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertEqual(self.numproj_calls(), [])
        self.assertIn("x.2 + 1", self.resolved_p(chain))

    def test_a_rewrite_that_changes_more_than_projections_is_not_used(self):
        self.use_tool(edit="lambda text: text.replace('def r : Nat := Nat.zero', 'def r : Nat := 1')")
        outcome, chain = self.run_task()
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertNotIn("numeric projections", self.agent_calls()[0]["prompt"])
        self.assertIn("x.2 + 1", self.resolved_p(chain))
        self.assertIn("def r : Nat := Nat.zero", self.resolved_p(chain))

    def test_sites_the_tool_left_send_a_clean_merge_to_the_agent(self):
        self.use_tool(rc=1)
        outcome, chain = self.run_task()
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1, "a clean merge is not used alone when the commit was not fully rewritten")
        prompt = calls[0]["prompt"]
        self.assertIn("These merged without conflicts and are in the working tree: `TauCeti/P.lean`", prompt)
        self.assertIn("numproj: TauCeti/P.lean:2:3: not rewritten", prompt)
        self.assertEqual(self.resolved_p(chain), NEW_P.replace("x.snd", "x.snd + 1"))

    def test_an_unacceptable_mechanical_resolution_goes_to_the_agent_from_git_s_markers(self):
        """A no-repair member may only be emptied: the clean merge fails `accept`, and the agent then
        starts from the conflict exactly as without the pre-pass."""
        raw = self.task_onto(NEW_P)
        raw["members"][1]["no_repair"] = True
        os.environ["STUB_AGENT_MODES"] = "explain-only,explain-only,explain-only"
        cfg = self.config(agent_attempts=1)
        repair_task.claim_and_run(cfg, self.client(cfg))
        calls = self.agent_calls()
        self.assertGreaterEqual(len(calls), 1)
        self.assertNotIn("numeric projections", calls[0]["prompt"])

    def test_without_warm_artifacts_the_pre_pass_does_not_run(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        outcome, chain = self.run_task()
        self.assertEqual(self.numproj_calls(), [])
        self.assertEqual(len(self.agent_calls()), 1)

    def test_a_tampered_named_field_is_not_accepted(self):
        """Elaborated member code can write the tool's inputs: a rewrite that also changes a field that
        was already named is refused, and the agent gets git's own conflict."""
        self.use_tool(edit="lambda text: text.replace('Nat.zero', 'Nat.one')")
        outcome, chain = self.run_task()
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertNotIn("numeric projections", self.agent_calls()[0]["prompt"])
        self.assertIn("x.2 + 1", self.resolved_p(chain))

    def test_a_symlinked_scratch_directory_is_not_followed(self):
        """A symlink planted under `.lake` (where member code can write) is refused, not followed."""
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        original = repair_numproj._private_dir

        def planted(lake, *names):
            target = lake / repair_numproj.SCRATCH
            if not target.exists() and not target.is_symlink():
                lake.mkdir(exist_ok=True)
                target.symlink_to(outside)
            return original(lake, *names)

        patcher = patch.object(repair_numproj, "_private_dir", planted)
        patcher.start()
        self.addCleanup(patcher.stop)
        outcome, chain = self.run_task()
        self.assertEqual(self.numproj_calls(), [])
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertEqual(oct(outside.stat().st_mode & 0o777), oct(0o755), "nothing outside was changed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_failed_write_restores_git_s_conflict(self):
        real = repair_numproj._write

        def failing(path, data):
            real(path, b"partial")
            raise OSError(28, "No space left on device")

        patcher = patch.object(repair_numproj, "_write", failing)
        patcher.start()
        self.addCleanup(patcher.stop)
        outcome, chain = self.run_task()
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertNotIn("numeric projections", self.agent_calls()[0]["prompt"])
        resolved = self.resolved_p(chain)
        self.assertIn("x.2 + 1", resolved)
        self.assertNotIn("partial", resolved)

    def test_a_tracked_symlink_is_not_rewritten(self):
        """Git records a symlink's target as its content; the pre-pass leaves such a path to the agent
        and never writes through it."""
        clone = self.content.clone
        sh(clone, "fetch", "--quiet", str(self.content.bare), "+refs/heads/*:refs/remotes/origin/*")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            tree = Path(directory)
            sh(clone, "worktree", "add", "--quiet", "--detach", str(tree), self.content.base)
            self.addCleanup(sh, clone, "worktree", "remove", "--force", str(tree), check=False)
            os.symlink("x.1", tree / "TauCeti" / "Link.lean")
            (tree / "TauCeti" / "Big.lean").write_text("x.1\n" * (repair_numproj.MAX_BYTES // 4 + 1))
            sh(tree, "add", "-A")
            sh(tree, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "links")
            self.assertIsNone(repair_numproj._blob(tree, "HEAD", "TauCeti/Link.lean"))
            self.assertIsNone(repair_numproj._blob(tree, "HEAD", "TauCeti/Big.lean"), "too large to check")
            self.assertEqual(repair_numproj._blob(tree, "HEAD", "TauCeti/P.lean"), OLD_P.encode())
            self.assertIsNone(repair_numproj._blob(tree, "HEAD", "TauCeti/Absent.lean"))
            os.symlink(self.root / "outside", tree / "Out.lean")
            with self.assertRaises(OSError):
                repair_numproj._write(tree / "Out.lean", b"x")
            self.assertFalse((self.root / "outside").exists())

    def test_the_rewrite_check(self):
        """The findings a rewrite implies, or None for an edit `numproj_fix.py` never makes."""
        edits = repair_numproj.projection_edits
        self.assertEqual(
            edits("f x.1 y.2.1", "f x.fst y.snd.fst"), {(1, 4, "fst", None), (1, 8, "snd", None), (1, 10, "fst", None)}
        )
        self.assertEqual(edits("a\n$h.1", "a\n($h).val"), {(2, 3, "val", (0, 2))})
        self.assertEqual(edits("x.1.2", "x.1.snd"), {(1, 4, "snd", None)})
        self.assertEqual(edits("(·.1)", "(·.val)"), {(1, 3, "val", None)})
        self.assertEqual(edits("h1.1 a b", "h1.fst a\n    b"), {(1, 3, "fst", None)})
        for before, after in [
            ("(x.1 + 2) * 3", "x.fst + 2 * 3"),
            ("a + b.1", "(a + b).fst"),
            ("1.5", "1.foo"),
            ("x.fst + y.1", "x.snd + y.fst"),
            ("s.val + y.1", "(s).val + y.fst"),
            ("Nat.succ", "Nat.pred"),
            ("x.1", "x"),
            ("f x.1", "g x.fst"),
            ("ab", "a\nb"),
            ("x.1", "(x.fst"),
            ("1.5", "(1).toFloat"),
            ("do returnh.1", "do return(h).fst"),
            ("℀h.1", "℀(h).fst"),
        ]:
            with self.subTest(before=before, after=after):
                self.assertIsNone(edits(before, after))
        self.assertTrue(repair_numproj.CANDIDATE.search("h.1"))
        self.assertTrue(repair_numproj.CANDIDATE.search("(f x).2"))
        self.assertTrue(repair_numproj.CANDIDATE.search("(·.1)"))
        self.assertEqual(
            edits("x!.1 x?.2 ℘.1", "x!.fst x?.snd ℘.val"),
            {(1, 3, "fst", None), (1, 8, "snd", None), (1, 12, "val", None)},
        )
        started = time.monotonic()
        edits("x.1\ny.2\n", "x." + "a" * 200_000 + "\ny.val\n")
        self.assertLess(time.monotonic() - started, 5, "linear in the rewritten copy, whatever is in it")
        self.assertFalse(repair_numproj.CANDIDATE.search("theorem a : True := trivial\n"))

    def test_a_rewrite_must_be_what_the_tool_writes(self):
        """`rewrites_only` reapplies the implied findings with the tool's own code: comments and
        string literals are not code, and only the tool's own layout passes."""
        fix = repair_numproj.load_tool(repair_numproj.tool_dir())
        same = repair_numproj.rewrites_only
        self.assertTrue(same(OLD_P, NEW_P, fix))
        self.assertTrue(same("x", "x", fix))
        self.assertFalse(same("-- y.1 here\nx", "-- y.fst here\nx", fix), "a comment is not code")
        self.assertFalse(same('def s := "y.1"', 'def s := "y.fst"', fix), "nor is a string")
        self.assertFalse(same("a b x.1", "a\nb x.fst", fix), "a line break the tool did not make")
        self.assertFalse(same("def v : Float := 1.5", "def v : Float := (1).toFloat", fix), "a literal")

    @unittest.skipUnless(REAL_NUMPROJ, "the installed TauCetiTools has no numproj")
    def test_the_real_tool_s_rewrites_pass_and_injections_do_not(self):
        fix = repair_numproj.load_tool(REAL_NUMPROJ)
        same = repair_numproj.rewrites_only
        line = "  exact ⟨fun x => (f x).1, by simpa using h.2, hs.1.2, $h.1, s.1.foo⟩"
        self.assertTrue(
            same(line, "  exact ⟨fun x => (f x).fst, by simpa using h.snd, hs.fst.snd, ($h).val, (s).val.foo⟩", fix)
        )
        long = "theorem foo (a : Nat × Nat) (b : Nat × Nat) (h : a.1 = b.2) (hh : a.2 = b.1) (hhh : a.1 + a.2 = b.1 + b.2) :\n    True := trivial\n"
        wrapped = (
            "theorem foo (a : Nat × Nat) (b : Nat × Nat) (h : a.fst = b.snd) (hh : a.snd = b.fst)\n"
            "    (hhh : a.fst + a.snd = b.fst + b.snd) :\n    True := trivial\n"
        )
        self.assertTrue(same(long, wrapped, fix))
        injected = "-- ignored def injected : Nat := 42\ndef q (x : Nat × Nat) : Nat := x.2\n"
        self.assertFalse(
            same(injected, "-- ignored\ndef injected : Nat := 42\ndef q (x : Nat × Nat) : Nat := x.snd\n", fix)
        )
        multiline = 'def s := "first\nx.1\nlast"\ndef q (x : Nat × Nat) : Nat := x.2\n'
        self.assertFalse(same(multiline, multiline.replace("x.1", "x.fst").replace("x.2", "x.snd"), fix))

    def test_scratch_files_are_read_without_following_links_or_blocking(self):
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            directory = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, directory)
            (Path(scratch) / "file").write_text("x.fst\n")
            os.symlink(Path(scratch) / "file", Path(scratch) / "link")
            os.mkfifo(Path(scratch) / "fifo")
            self.assertEqual(repair_numproj._read(directory, "file"), b"x.fst\n")
            for name in ("link", "fifo"):
                with self.subTest(name=name), self.assertRaises(OSError):
                    repair_numproj._read(directory, name)


class NumprojRepairBase(NumprojBase):
    """A repair task on a chain whose members 2 and 3 wrote numeric projections that the rc1 linter
    rejects (``TauCeti/P.lean`` and ``TauCeti/Q.lean``); member 1 has one in a file no finding names. The
    streaming build reports them, and the worker rewrites each in the member that wrote it before any
    agent runs (``repair_task.rewrite_projections``)."""

    P_BASE = "def r : Nat := Nat.zero\n"
    P_MEMBER = P_BASE + "def q (x : Nat × Nat) : Nat := x.2\n"
    Q_MEMBER = "def s (x : Nat × Nat) : Nat := x.1\n"
    KEEP = "def k (x : Nat × Nat) : Nat := x.1\n"
    BASE_FILES = {"TauCeti/P.lean": P_BASE}
    MEMBER_FILES = {
        1: {"TauCeti/Keep.lean": KEEP},
        2: {"TauCeti/P.lean": P_MEMBER},
        3: {"TauCeti/Q.lean": Q_MEMBER},
    }

    def setUp(self):
        super().setUp()
        # The sandbox clears the environment: the lint's files travel with the bwrap stub's extras.
        (BIN / "sandbox-env.json").write_text(json.dumps({"STUB_DIR": str(self.stub_dir), "STUB_LAKE_NUMPROJ": "P,Q"}))
        raw = self.content.task(1)
        raw["diagnostics"] = []
        self.service.tasks = {1: raw}
        self.seen = []
        original = repair_task.run_repair_agent

        def run_repair_agent(cfg, task, worktree, prompt, *args, **kwargs):
            errors = worktree / repair.LISTINGS_DIR / repair_stream.ERRORS_FILE
            self.seen.append({"prompt": prompt, "errors": errors.read_text() if errors.exists() else ""})
            return original(cfg, task, worktree, prompt, *args, **kwargs)

        patcher = patch.object(repair_task, "run_repair_agent", run_repair_agent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claim(self, **overrides) -> dict:
        cfg = self.config(**overrides)
        return repair_task.claim_and_run(cfg, self.client(cfg))

    def pushed_chain(self) -> list:
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        return chain_tool.chain_commits(self.content.bare, self.content.base, pushed)

    def show(self, sha: str, path: str) -> str:
        return sh(self.content.bare, "show", f"{sha}:{path}") + "\n"


class NumprojRepair(NumprojRepairBase):
    def test_each_projection_is_rewritten_in_the_member_that_wrote_it(self):
        outcome = self.claim()
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.seen, [], "nothing was left for an agent")
        chain = self.pushed_chain()
        original = chain_tool.chain_commits(self.content.bare, self.content.base, self.content.tip)
        self.assertEqual(chain[0].sha, original[0].sha, "member 1 changes no rewritten file and is kept as is")
        self.assertEqual(self.show(chain[0].sha, "TauCeti/P.lean"), self.P_BASE)
        self.assertEqual(self.show(chain[1].sha, "TauCeti/P.lean"), self.P_MEMBER.replace("x.2", "x.snd"))
        self.assertEqual(self.show(chain[2].sha, "TauCeti/Q.lean"), self.Q_MEMBER.replace("x.1", "x.fst"))
        self.assertEqual(self.show(chain[2].sha, "TauCeti/Keep.lean"), self.KEEP, "no finding names it")
        for member in chain[1:]:
            self.assertIn(repair_numproj.EXPLANATION, member.message)
            self.assertTrue(member.message.startswith(original[chain.index(member)].message.rstrip()))
        for new, old in zip(chain, original, strict=True):
            self.assertEqual((new.author_name, new.author_date), (old.author_name, old.author_date))
        diff = sh(self.content.bare, "diff", "--name-only", chain[1].parents[0], chain[1].sha).split()
        self.assertEqual(diff, ["TauCeti/P.lean", "TauCeti/Two.lean"], "member 2's own files only")
        self.assertEqual(outcome["numproj"][0]["paths"], ["TauCeti/P.lean", "TauCeti/Q.lean"])
        self.assertEqual(outcome["numproj"][0]["errors"], 0)

    def test_the_task_s_own_findings_are_replaced_too(self):
        self.service.tasks[1]["diagnostics"] = [
            {
                "kind": "compiler",
                "module": "TauCeti.P",
                "message": "TauCeti/P.lean:2:33: Numeric projections are disallowed.",
            },
            {"kind": "compiler", "module": "TauCeti.One", "message": "TauCeti/One.lean:1:1: unknown identifier X"},
        ]
        self.claim(agent_attempts=1, verify_retries=0, stream_repair=False)
        self.assertEqual(len(self.seen), 1)
        prompt = self.seen[0]["prompt"]
        self.assertIn("unknown identifier X", prompt)
        self.assertNotIn("Numeric projections", prompt)

    def test_a_rewrite_that_is_not_projections_only_is_refused(self):
        self.use_tool(edit="lambda text: text.replace('Nat.zero', 'Nat.one')")
        outcome = self.claim(agent_attempts=1, verify_retries=0)
        self.assertNotIn("numproj", outcome)
        self.assertEqual(len(self.seen), 1, "the agent gets the findings as they were")
        self.assertIn("Numeric projections are disallowed", self.seen[0]["errors"])
        self.assertNotIn('"event": "numproj"', self.seen[0]["errors"])


class NumprojRepairLeftover(NumprojRepairBase):
    """Member 3 also wrote a projection the tool cannot rewrite (the stand-in renames `.1` and `.2` only)."""

    MEMBER_FILES = {
        **NumprojRepairBase.MEMBER_FILES,
        3: {"TauCeti/Q.lean": NumprojRepairBase.Q_MEMBER + "def t (x : Nat × Nat) : Nat := x.3\n"},
    }

    def test_what_the_rewrite_leaves_reaches_the_agent(self):
        """The scoped build's error replaces the rewritten files' earlier ones in the error list the
        agent reads."""
        outcome = self.claim(agent_attempts=1, verify_retries=0)
        self.assertEqual(len(self.seen), 1)
        errors = [json.loads(line) for line in self.seen[0]["errors"].splitlines()]
        located = [(e["file"], e["line"]) for e in errors if "file" in e]
        self.assertEqual(located, [("TauCeti/Q.lean", 2)], "P is clean, and so is Q's first line")
        self.assertEqual(errors[-1]["event"], "numproj")
        self.assertEqual(outcome["numproj"][0]["errors"], 1)


class NumprojRepairSharedFile(NumprojRepairBase):
    """Members 2 and 3 both change ``TauCeti/P.lean``: member 2 adds a projection at its end, member 3
    changes its first line without one."""

    P_BASE = "def r : Nat := Nat.zero\n\n\n\n\ndef z : Nat := 0\n"
    BASE_FILES = {"TauCeti/P.lean": P_BASE}
    MEMBER_FILES = {
        2: {"TauCeti/P.lean": P_BASE + "def q (x : Nat × Nat) : Nat := x.2\n"},
        3: {"TauCeti/P.lean": P_BASE.replace("Nat.zero", "Nat.succ 0")},
    }

    def test_only_the_member_whose_patch_is_rewritten_is_edited(self):
        outcome = self.claim()
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.seen, [])
        chain = self.pushed_chain()
        original = chain_tool.chain_commits(self.content.bare, self.content.base, self.content.tip)
        expected = self.P_BASE.replace("Nat.zero", "Nat.succ 0") + "def q (x : Nat × Nat) : Nat := x.snd\n"
        self.assertEqual(self.show(chain[2].sha, "TauCeti/P.lean"), expected)
        self.assertIn(repair_numproj.EXPLANATION, chain[1].message)
        self.assertEqual(chain[2].message, original[2].message, "member 3's own patch is unchanged")
        patch_of = lambda c: sh(self.content.bare, "diff", c.parents[0], c.sha)  # noqa: E731
        self.assertEqual(
            [line for line in patch_of(chain[2]).splitlines() if line[:1] in ("+", "-")],
            [line for line in patch_of(original[2]).splitlines() if line[:1] in ("+", "-")],
        )
        self.assertEqual(outcome["numproj"][0]["edited"], [original[1].sha])


class RebasedChainRepair(Fixture):
    """Member 3 is broken too, without conflicting: the rebased chain fails the worker's verification,
    and the task then repairs it on ``onto`` as a repair task would."""

    BROKEN = (2, 3)

    def setUp(self):
        super().setUp()
        self.raw = self.content.rebase_task()
        self.service.tasks = {1: self.raw}

    def test_a_failed_verification_repairs_the_rebased_chain(self):
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("## Resolving the commit", calls[0]["prompt"])
        self.assertNotIn("## Resolving the commit", calls[1]["prompt"])
        self.assertIn("unknown identifier BROKEN", calls[1]["prompt"])
        self.assertEqual([v["ok"] for v in outcome["verification"]], [False, True])
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertNotIn("BROKEN", sh(self.content.bare, "show", f"{pushed}:TauCeti/Three.lean"))

    def test_a_chain_the_repair_does_not_change_is_submitted_for_ci(self):
        """The agent leaves the rebased chain as it is: it is submitted for CI as before, without
        verifying it again."""
        os.environ["STUB_AGENT_MODES"] = "repair,noop"
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(len(self.agent_calls()), 2)
        self.assertEqual([v["ok"] for v in outcome["verification"]], [False])
        pushed = sh(self.content.bare, "rev-parse", self.submission_ref())
        self.assertIn("BROKEN", sh(self.content.bare, "show", f"{pushed}:TauCeti/Three.lean"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
