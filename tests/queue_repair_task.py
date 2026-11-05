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
agent failure released as such; a second claimant refused; and a checkpointed task resumed in its
recorded session. The long-running worker (``serve``): the claim/run/submit cycle with the provider
outcome reported to ``/tasks/inference`` and idle polling until stopped; a failing service and a
failing task that do not end the loop; SIGTERM stopping the agent while keeping the claim and
checkpoint, which the next start resumes; and the host preflight checks. The streaming repair: the
worker's background ``lake build --iofail`` (a scripted stub emitting two errors with a pause between
them) launches the agent after the first error with the "Build in progress" section, the error list and
the lock behave as the prompt says, and ``--no-stream-repair`` restores the sequential flow.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
os.environ["HOME"] = str(HOME)
os.environ["TAUCETI_CACHE_SERVICE"] = ""  # the worker's own cache warming is not exercised here
os.environ["TAUCETI_WORKER_ID"] = "queue-repair-test"
os.environ["GH_TOKEN"] = "worker-token"
for key in ("TAUCETI_RUNTIME_STATUS", "TAUCETI_FLEET_ADMISSION_FILE", "CLAUDE_CONFIG_DIR", "TAUCETI_STREAM"):
    os.environ.pop(key, None)

from tauceti_queue import chain as chain_tool  # noqa: E402
from tauceti_queue import (  # noqa: E402
    repair,
    repair_context,
    repair_parallel,
    repair_publish,
    repair_sandbox,
    repair_stream,
    repair_task,
    repair_tools,
)

PROJECT = "TauCetiProject/TauCeti"
TOKEN = "worker-token"

# ---- stubs on PATH -------------------------------------------------------------------------------

LAKE = r'''#!/usr/bin/env python3
"""lake stub: `cache get` succeeds unless STUB_LAKE_CACHE_FAIL=1 or the `--rev` is listed in
STUB_LAKE_CACHE_FAIL_REVS (comma separated); from the repair namespace (`--service tauceti-repair`)
only the revisions in STUB_LAKE_REPAIR_REVS are published. `build` fails modules containing BROKEN
and with `-o FILE` writes an output map naming one artifact in the pool. `cache put` records the
key it was given in `lake-put-key` and fails when STUB_LAKE_PUT_FAIL=1. `build --iofail` replays the
file STUB_LAKE_BUILD_SCRIPT when set (literal lines, `sleep S`, `exit N`)."""
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
stub = Path(os.environ["STUB_DIR"])
log = stub / "lake-calls.log"
log.open("a").write(" ".join(args) + "\n")
if args == ["build", "--iofail"] and os.environ.get("STUB_LAKE_BUILD_SCRIPT"):
    for line in Path(os.environ["STUB_LAKE_BUILD_SCRIPT"]).read_text().splitlines():
        if line.startswith("sleep "):
            time.sleep(float(line.split()[1]))
        elif line.startswith("exit "):
            sys.exit(int(line.split()[1]))
        else:
            print(line, flush=True)
    sys.exit(0)
if args[:2] == ["cache", "put"]:
    (stub / "lake-put-key").write_text(os.environ.get("LAKE_CACHE_KEY", ""))
    sys.exit(1 if os.environ.get("STUB_LAKE_PUT_FAIL") == "1" else 0)
if args[:2] == ["cache", "get"] and "tauceti-repair" in args:
    rev = next((a[len("--rev="):] for a in args if a.startswith("--rev=")), "")
    if rev not in os.environ.get("STUB_LAKE_REPAIR_REVS", "").split(","):
        print("error: outputs not found for revision")
        sys.exit(1)
    print("downloaded artifact 0022")
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
    for index, path in enumerate(modules, 1):
        name = "TauCeti." + path.stem
        if "BROKEN" in path.read_text():
            print(f"✖ [{index}/{len(modules)}] Building {name}")
            print(f"error: {path}:1:1: unknown identifier BROKEN")
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
# else is unexpected here.
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
import json, os, re, subprocess, sys, time
from pathlib import Path

agent = Path(sys.argv[0]).name  # claude | codex
args = sys.argv[1:]
stub_dir = Path(os.environ["STUB_DIR"])
calls = stub_dir / "agent-calls.jsonl"
previous = sum(1 for _ in calls.open()) if calls.exists() else 0
call = previous + 1
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
    handle.write(json.dumps({"call": call, "agent": agent, "mode": mode, "resume": resume, "started": started, "argv": args[:1] + [a for a in args[1:] if a != prompt], "prompt": prompt, "prompt_via": prompt_via, "prompt_chars": len(prompt), "cwd": os.getcwd(), "launch": launch, "env": {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_VALUE_0", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TAUCETI_CLAUDE_BILLING", "BASH_MAX_TIMEOUT_MS", "TAUCETI_SHELL_LIMIT_SECONDS", "LAKE_CACHE_KEY")}, "claude_credentials": bool(config_dir) and os.path.exists(os.path.join(config_dir, ".credentials.json")), "codex_credentials": bool(codex_home) and os.path.exists(os.path.join(codex_home, "auth.json"))}) + "\n")
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
    """Replay HEAD's chain onto its base, fixing the broken member in place."""
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

for name, text in (("lake", LAKE), ("gh", GH), ("claude", CLAUDE), ("codex", CLAUDE), ("bwrap", BWRAP)):
    path = BIN / name
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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


class Content:
    """A bare content repository, a seed clone that built the chain, and the worker's clone."""

    def __init__(self, root: Path, broken: tuple[int, ...] = (2,)):
        self.broken = broken
        self.bare = root / "content.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(self.bare)], check=True)
        seed = root / "seed"
        subprocess.run(["git", "clone", "--quiet", str(self.bare), str(seed)], check=True, capture_output=True)
        sh(seed, "config", "user.name", "Fixture")
        sh(seed, "config", "user.email", "fixture@example.invalid")
        sh(seed, "checkout", "--quiet", "-b", "main")
        self.base = commit(seed, "base", {"TauCeti/A.lean": "theorem a : True := trivial\n"})
        sh(seed, "push", "--quiet", "origin", "main")
        heads = {}
        for number, name in ((1, "One"), (2, "Two"), (3, "Three")):
            body = "BROKEN" if number in broken else "trivial"
            files = {f"TauCeti/{name}.lean": f"theorem {name.lower()} : True := {body}\n"}
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

    def rebase_task(self, task_id: int = 1) -> dict:
        """A rebase-conflict task: main moved past the batch base, so ``onto`` differs from ``base``."""
        seed = self.bare.parent / "seed"
        sh(seed, "checkout", "--quiet", "--detach", self.base)
        onto = commit(seed, "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        sh(seed, "push", "--quiet", "origin", f"{onto}:refs/heads/queue/batch-1/onto")
        task = self.task(task_id, kind="rebase_conflict")
        task["onto"] = onto
        task["retained_refs"] = [*task["retained_refs"], "refs/heads/queue/batch-1/onto"]
        task["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#2",
                "paths": ["TauCeti/Two.lean"],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#2",
                "pending": [f"{PROJECT}#3"],
                "clean_prefix_tip": self.members[0]["commit"],
            }
        ]
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

    def submit_task(self, task_id: int, token: str, tip_sha: str, worker_notes: str) -> dict:
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
                match = re.fullmatch(r"/tasks/([^/]+)/(renew|result|release)", path)
                if not match:
                    return 404, {"error": "no such route"}
                task_id, verb = int(match.group(1)), match.group(2)
                if verb == "release" and not self.release_supported:
                    return 404, {"error": "no such route"}
                if task_id not in self.tasks:
                    raise LookupError("unknown-task")
                token = str(body.get("claim_token") or "")
                if verb == "renew":
                    return 200, self.renew_task(task_id, token)
                if verb == "release":
                    if self.release_mode == "error":
                        raise RuntimeError("database unavailable")
                    return 200, self.release_task(task_id, token, str(body.get("reason")), str(body.get("error", "")))
                return 200, self.submit_task(
                    task_id, token, str(body.get("tip_sha") or ""), str(body.get("worker_notes", ""))
                )
            except LookupError as error:  # TaskError: a stable code in the body
                return 409, {"error": str(error), "code": str(error)}
            except RuntimeError as error:
                return 500, {"error": str(error)}


# ---- tests ---------------------------------------------------------------------------------------


class Fixture(unittest.TestCase):
    BROKEN = (2,)  # the members whose module fails to build

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=TMP))
        self.content = Content(self.root, broken=self.BROKEN)
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
        os.environ.pop("STUB_LAKE_CACHE_FAIL_REVS", None)
        os.environ.pop("STUB_LAKE_REPAIR_REVS", None)
        os.environ.pop("STUB_LAKE_PUT_FAIL", None)
        os.environ.pop("STUB_LAKE_BUILD_SCRIPT", None)
        os.environ.pop("STUB_SLEEP", None)
        os.environ.pop(repair_sandbox.DISABLE_VARIABLE, None)
        (BIN / "sandbox-env.json").write_text(json.dumps({"STUB_DIR": str(self.stub_dir)}))
        for key in ("STUB_FAIL_GROUP_WITH", "STUB_GROUP_STRAY", "STUB_GROUP_BUILD", "STUB_LAKE_BUILD_SLEEP"):
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
        self.assertEqual([c["argv"][c["argv"].index("--") + 1 :] for c in sandboxed], [["lake", "build", "--iofail"]])
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
        self.assertFalse((self.root / "state" / "worktrees" / "task-1").exists())
        self.assertIsNone(repair_task.claim_and_run(cfg, self.client(cfg)), "the adopted task is not claimable again")

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
        sh(seed, "push", "--quiet", "--force", "origin", f"{moved}:refs/heads/queue/batch-1/onto")
        self.raw["onto"] = moved
        self.raw["diagnostics"][0]["onto"] = moved
        self.service.tasks = {1: self.raw}
        return moved

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
        prompt = self.agent_calls()[0]["prompt"]
        self.assertIn("Artifacts are warm", prompt)
        self.assertNotIn("The build is cold", prompt)
        env = self.agent_calls()[0]["env"]
        self.assertEqual(env["TAUCETI_SHELL_LIMIT_SECONDS"], "7200", "the repair task's own shell limit")
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], str(min(7_200_000, int(env["BASH_MAX_TIMEOUT_MS"]))))
        self.assertIn("shell commands have a two-hour limit", prompt)

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
        self.assertIn("Artifacts are warm", self.agent_calls()[0]["prompt"])

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

    def test_a_resumed_session_keeps_its_provider_even_over_pace(self):
        # An interrupted task resumes the recorded codex thread, ahead of a now-eligible claude and
        # without re-checking codex's pace: the work was admitted when it started.
        os.environ["STUB_AGENT_MODE"] = "resume-only"
        cfg = self.config(providers=("claude", "codex"))
        claim = self.service.preclaim(1, "old-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        repair_task.fetch_retained(cfg.clone, task)
        worktree = cfg.root / "worktrees" / "task-1"
        repair.checkout_worktree(cfg.clone, task.tip, worktree)
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("agent_running", agent_attempt=1, provider="codex")
        from tauceti_worker.runtime_status import atomic_json

        atomic_json(state.session_path, {"session": "thread-old", "cwd": str(worktree)})
        self.statuses["codex"] = (False, "weekly ahead of pace")
        outcomes = repair_task.resume_pending(cfg, self.client(cfg))
        self.assertEqual([o["status"] for o in outcomes], ["adopted"], outcomes)
        calls = self.agent_calls()
        self.assertEqual([(c["agent"], c["resume"]) for c in calls], [("codex", "thread-old")])
        self.assertEqual(self.provider_attempts(outcomes[0]), [("codex", "ran")])

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
            seen["renew"] = renew
            window = quota.Window("session", 61.0, 70.0, time.time() + 3600, quota.STATUS_OVER_PACE, 55.0)
            return quota.Provider("claude", False, None, [window])

        def codex(self_, *, refresh=False):
            return quota.Provider(
                "codex", True, "gpt-5", [quota.Window("weekly", 10.0, 20.0, None, "under-pace", 30.0)]
            )

        cfg = self.config(rate="0:10,50:70")
        with patch.object(repair_task, "subscription_status", self._real_subscription_status):
            with patch.object(quota.Quota, "claude", claude), patch.object(quota.Quota, "codex", codex):
                self.assertEqual(
                    repair_task.subscription_status(cfg, "claude"),
                    (False, "session ahead of pace (70% elapsed: used 61% > 55% pace budget), 39% left"),
                )
                self.assertEqual(
                    repair_task.subscription_status(cfg, "codex"), (True, "under pace (weekly 10% used of 30% budget)")
                )
            self.assertEqual(
                seen, {"pace": "0:10,50:70", "renew": False}, "the task's rate is the pacer's curve; no token rotation"
            )
            self.assertIsNone(os.environ.get("TAUCETI_PACE"))
            # No credential at all: unmeasurable, not eligible, and the reason names the file.
            ok, reason = repair_task.subscription_status(cfg, "claude")
            self.assertFalse(ok)
            self.assertIn(".credentials.json", reason)

    def test_the_chain_spec(self):
        self.assertEqual(repair_task.parse_providers(None), ("claude", "codex", "api"))
        self.assertEqual(repair_task.parse_providers(" Codex, api "), ("codex", "api"))
        for bad in ("claude,kiro", "claude,claude", ","):
            with self.assertRaises(ValueError):
                repair_task.parse_providers(bad)
        self.assertEqual(repair.codex_profile().model, "gpt-6.1-sol")
        self.assertEqual(repair.claude_profile().model, "claude-opus-5-5")


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


class Resume(Fixture):
    def test_checkpointed_task_resumes_its_session(self):
        os.environ["STUB_AGENT_MODE"] = "resume-only"
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
        outcomes = repair_task.resume_pending(cfg, client)
        self.assertEqual([o["status"] for o in outcomes], ["adopted"], outcomes)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resume"], "sess-old")
        self.assertIn("has resumed your session", calls[0]["prompt"])
        self.assertEqual(self.lake_calls(), [], "artifacts are not prepared again on resume")
        self.assertTrue(self.state()["resumed"])

    def test_a_checkpoint_whose_claim_is_gone_is_not_resumed(self):
        cfg = self.config()
        task = repair_task.Task.from_json(
            {**self.content.task(), "claim_token": "gone", "expires_at": time.time() + 5, "renew_after": 0.2}
        )
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("agent_running")
        self.assertEqual(repair_task.resume_pending(cfg, self.client(cfg)), [])
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
        stop = repair_task.Stop()
        thread, result = self.serve_in_thread(cfg, stop)
        self.wait_for(
            lambda: len(self.service.results) == 1 and len(self.service.claim_attempts) >= 3,
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

    def test_sigterm_interrupts_the_agent_keeps_the_claim_and_the_next_start_resumes(self):
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
        self.assertEqual(summary["outcomes"], {"interrupted": 1}, summary)
        state = self.state()
        self.assertEqual(state["state"], "interrupted")
        self.assertEqual(state["phase"], "agent_running")
        # Nothing was submitted or released, the claim is still ours and the worktree is kept.
        self.assertEqual(self.service.results, [])
        self.assertEqual(self.service.releases, [])
        self.assertEqual(self.service.inference_reports, [])
        self.assertIsNotNone(self.service.live_claim(1), "the claim is kept for the next start")
        self.assertTrue((self.root / "state" / "worktrees" / "task-1").is_dir())
        self.assertEqual(self.heartbeat(cfg)["state"], "stopped")
        # The next start renews the claim and resumes the recorded session in the kept worktree.
        os.environ["STUB_AGENT_MODE"] = "resume-only"
        outcomes = repair_task.resume_pending(cfg, self.client(cfg))
        self.assertEqual([o["status"] for o in outcomes], ["adopted"], outcomes)
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["resume"], "sess-1")
        self.assertIn("has resumed your session", calls[1]["prompt"])
        self.assertEqual(len(self.service.results), 1)
        self.assertEqual(self.state()["state"], "adopted")

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


class StopAndResume(Fixture):
    """A claim never outlives the worker that holds it: a stop before the agent runs releases it, a
    restart releases or resumes whatever a stopped run left, and a claim refused for our own stale
    claim is given back and retried at once."""

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

    def test_sigterm_with_the_agent_running_renews_the_claim_before_checkpointing(self):
        os.environ["STUB_AGENT_MODE"] = "sleep"
        os.environ["STUB_SLEEP"] = "30"
        cfg = self.config(poll_seconds=0.2)
        stop = self.install_handlers()
        self.terminate_when(lambda: self.checkpoint_state() == "agent_running", delay=1.0)
        renewals_before = self.service.renewals
        summary = repair_task.serve(cfg, self.client(cfg), stop)
        self.assertEqual(summary["outcomes"], {"interrupted": 1}, summary)
        claim = self.service.live_claim(1)
        self.assertIsNotNone(claim, "the claim is kept for the resume")
        self.assertGreater(self.service.renewals, renewals_before)
        self.assertAlmostEqual(claim["expires_at"], self.state()["task"]["expires_at"], delta=1.0)
        self.assertEqual(self.service.releases, [])

    def test_on_start_a_checkpoint_stopped_before_the_agent_is_released_and_claimed_fresh(self):
        cfg = self.config()
        client = self.client(cfg)
        # A stopped run left the task claimed (its token in the checkpoint) and never got further.
        claim = self.service.preclaim(1, "stopped-run-token", cfg.worker)
        task = repair_task.Task.from_json(self.service.task_record(1, claim))
        state = repair_task.TaskState(cfg.tasks_dir / "1", task)
        state.write("claimed", worker=cfg.worker)
        self.assertIsNone(client.claim(), "the stale claim blocks a fresh one (concurrency)")
        outcomes = repair_task.resume_pending(cfg, client)
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

    KEY = "access-id:secret-value"

    def settings(self, **overrides) -> repair_publish.Settings:
        key_file = self.root / "repair-cache-key"
        key_file.write_text(self.KEY + "\n")
        values = dict(url="https://repair-cache.example", key_file=key_file)
        values.update(overrides)
        return repair_publish.Settings(**values)

    def puts(self) -> list[str]:
        return [c for c in self.lake_calls() if c.startswith("cache put")]

    def test_a_green_repair_publishes_the_tip_before_submission(self):
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        record = outcome["publish"]
        self.assertTrue(record["published"], record)
        self.assertEqual(record["revision"], outcome["tip"])
        self.assertEqual((record["artifacts"], record["bytes"]), (1, 2048))
        calls = self.lake_calls()
        check = next(i for i, c in enumerate(calls) if c.startswith("build --no-build --rehash -o "))
        put = next(i for i, c in enumerate(calls) if c.startswith("cache put "))
        self.assertLess(check, put, "the output map is written by the --no-build pass first")
        self.assertIn(f"--service tauceti-repair-upload --repo {PROJECT}", calls[put])
        self.assertEqual((self.stub_dir / "lake-put-key").read_text(), self.KEY, "the key reaches lake cache put")
        self.assertIsNone(self.agent_calls()[0]["env"]["LAKE_CACHE_KEY"], "and never the agent")
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

    def test_a_published_tip_is_restored_from_the_repair_namespace(self):
        os.environ["STUB_LAKE_REPAIR_REVS"] = self.content.tip
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        fetches = [c for c in self.lake_calls() if c.startswith("cache get")]
        self.assertEqual(len(fetches), 1, fetches)
        self.assertIn(f"--service tauceti-repair --repo {PROJECT} --rev={self.content.tip}", fetches[0])
        self.assertEqual(outcome["artifacts"]["cache_service"], "tauceti-repair")
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.content.tip)

    def test_a_failed_upload_is_logged_and_the_task_still_submits(self):
        os.environ["STUB_LAKE_PUT_FAIL"] = "1"
        cfg = self.config(publish=self.settings())
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertFalse(outcome["publish"]["published"])
        self.assertEqual(outcome["publish"]["error"], "lake cache put exited 1")
        self.assertEqual(len(self.service.results), 1)

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
        self.assertIn("lake build --no-build exited 1", record["skipped"])
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
        cfg = self.config(parallel_agents=3)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assert_two_groups_folded(outcome)
        # Two group agents ran at the same time, each in its own worktree of the tip with a scoped prompt.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(c["cwd"] for c in calls), [str(self.group_worktree(1)), str(self.group_worktree(2))])
        self.assertEqual({c["agent"] for c in calls}, {"claude"})
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
        # The groups came from the streaming build's complete error list, after that build had finished;
        # the verification build ran in the sandbox like the rest.
        self.assertEqual(outcome["parallel"]["source"], "errors.jsonl")
        self.assertEqual(outcome["parallel"]["stream_errors"], 2)
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

    def test_groups_come_from_the_streaming_build_when_the_diagnostics_under_report(self):
        # CI's --fail-fast reported only the first broken module; the worker's own build finds both.
        task = self.service.tasks[1]
        task["diagnostics"] = [d for d in task["diagnostics"] if d["module"] == "TauCeti/Two.lean"]
        cfg = self.config(parallel_agents=2)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assert_two_groups_folded(outcome)
        self.assertEqual(outcome["parallel"]["source"], "errors.jsonl")
        three = next(c["prompt"] for c in self.agent_calls() if c["cwd"] == str(self.group_worktree(2)))
        self.assertIn("`TauCeti/Three.lean:1:1` (PR #3's commit `", three, "the streamed error, with its owner")
        self.assertIn("unknown identifier BROKEN", three)

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
        self.assertEqual(by_name["github token"]["detail"], "from $GH_TOKEN")
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
        self.assertEqual(sorted(writable), sorted([(str(worktree / ".lake"),) * 2, (env["LAKE_CACHE_DIR"],) * 2]))
        self.assertNotIn(str(HOME), [source for source, _ in read_only + writable])
        self.assertIn(("/tmp",), [(argv[i + 1],) for i, a in enumerate(argv) if a == "--tmpfs"])
        self.assertLess(argv.index("--ro-bind"), argv.index("--remount-ro"))
        setenv = dict(pairs(argv, "--setenv"))
        self.assertEqual(setenv["LAKE_NO_CACHE"], "true")
        self.assertEqual(setenv["LAKE_CACHE_DIR"], env["LAKE_CACHE_DIR"])
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

    def test_disabled_falls_back_with_a_logged_warning(self):
        worktree, _, env = self.worktree()
        os.environ[repair_sandbox.DISABLE_VARIABLE] = "0"
        with self.assertLogs(repair_sandbox.LOGGER, "WARNING") as logs:
            argv = repair_sandbox.sandboxed_lake(["lake", "build", "--iofail"], worktree, env)
        self.assertEqual(argv, ["lake", "build", "--iofail"])
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
        by_name = {c["check"]: c for c in repair_task.preflight(self.config(), min_free_gib=0)}
        self.assertTrue(by_name["build sandbox"]["ok"], by_name["build sandbox"])
        self.assertIn(str(BIN / "bwrap"), by_name["build sandbox"]["detail"])
        self.assertEqual(self.bwrap_calls()[-1]["argv"][-2:], ["--", "true"])
        (BIN / "sandbox-env.json").write_text("{}")  # the stub now fails, as a broken bwrap would
        by_name = {c["check"]: c for c in repair_task.preflight(self.config(), min_free_gib=0)}
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
        self.script()
        os.environ["STUB_AGENT_MODE"] = "stream"
        cfg = self.config(keep_worktree=True)
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        # No prepare build: the streaming build is the one whole-library build before the agent.
        lake = self.lake_calls()
        self.assertEqual(lake[:3], ["exe cache get", lake[1], "build --iofail"])
        self.assertIn("cache get", lake[1])
        self.assertEqual(lake.count("build --iofail"), 1)
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
        self.assertIn("verdict is still your own whole-library `lake build --iofail`", prompt)
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
        self.assertIn("Artifacts are warm:", calls[0]["prompt"])
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


class Records(unittest.TestCase):
    def test_task_record_and_prompt_for_a_rebase_conflict(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(9, kind="rebase_conflict")
        seed = root / "seed"
        onto = commit(seed, "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        raw["onto"] = onto
        raw["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#2",
                "paths": ["TauCeti/Two.lean"],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#2",
                "pending": [f"{PROJECT}#3"],
                "clean_prefix_tip": content.members[0]["commit"],
            }
        ]
        task = repair_task.Task.from_json(
            {**raw, "claim_token": "x", "expires_at": time.time() + 60, "extra": {"claim_id": 5}}
        )
        self.assertEqual(task.validation_base, onto)
        self.assertEqual(task.member_keys, [f"{PROJECT}#1", f"{PROJECT}#2", f"{PROJECT}#3"])
        self.assertEqual(task.submission_ref, "refs/tauceti-queue/submissions/9/5")
        self.assertEqual(repair_task.Task.from_json(task.to_json()), task)
        chain = repair.read_chain(seed, content.base, content.tip)
        listings = repair.listings_path(seed)
        context = repair_task.prompt_context(task, None, listings=listings)
        prompt = repair.compose_prompt(context, repair_task.task_findings(task), chain, seed)
        self.assertTrue(prompt.startswith(repair.AGENT_INSTRUCTION))
        self.assertLess(len(prompt), 10_000, f"prompt is {len(prompt)} bytes")
        # What happened, in plain words and before anything else, with the git refs named once.
        self.assertIn("## What happened", prompt)
        self.assertLess(prompt.index("## What happened"), prompt.index("## Conflict evidence"))
        self.assertIn(
            f"The queue replayed this batch of 3 squash commits, one per pull request, from its base "
            f"`{content.base[:12]}` onto the current `main` commit `{onto[:12]}` with git. The replay stopped at "
            f"PR #2's commit `{chain[1].sha[:12]}` with conflicts in `TauCeti/Two.lean`. The 1 commit before it "
            f"replayed cleanly and is at commit `{chain[0].sha[:12]}`. Your job is to finish that replay so the "
            f"whole batch sits on `{onto[:12]}`.",
            prompt,
        )
        self.assertIn(f"- conflicting commit: `{chain[1].sha[:12]}` (PR #2, position 2 of 3: feat: add 2)", prompt)
        self.assertIn(f"- clean prefix: `{chain[0].sha[:12]}` (position 1 replayed onto the target)", prompt)
        self.assertIn(f"- base, the batch's previous base: `{content.base[:12]}`", prompt)
        self.assertIn(f"- target, the current `main`: `{onto[:12]}`", prompt)
        self.assertIn(
            f"- `TauCeti/Two.lean`: PR #2's commit `{chain[1].sha[:12]}` modifies this file, and `main` has changed it "
            "since the batch's base",
            prompt,
        )
        self.assertIn("- not yet replayed: 1 commit after it (position 3)", prompt)
        self.assertIn(".tauceti-repair/members.jsonl", prompt)
        for phrase in (
            "scripted rebase conflicted",
            "exposed it as",
            "Nothing you do here is published",
            "do not push",
            "Do not edit `lakefile.toml`",
            "retaining their source PR identities",
            "is changed by PR",
            "not yet replayed after it",
            "One workable recipe",
            "Any method is fine",
            "scripts/lint-env.sh",
            "duplicate declarations there is no light local audit",
            "Do not weaken anything",
            "maxHeartbeats",
            "ten-minute",
            "## Constituent commits",
            "## Recorded members",
            "members.md",
            "what failed",
        ):
            self.assertNotIn(phrase, prompt, phrase)
        # The result, the rebase instruction, the check and the build.
        self.assertIn(
            f"a linear chain on `{onto[:12]}` with one commit per PR in the same order as now, each squash commit "
            "keeping its `Source-PR:` and `Accepted-Head:` trailers and its subject ending in ` (#N)`",
            prompt,
        )
        self.assertIn("nothing to push, publish or report anywhere else, and `gh` is not needed", prompt)
        self.assertIn(
            "Do not introduce changes to `lakefile.toml`, `lake-manifest.json`, `lean-toolchain`, `scripts/`, "
            "`.github/` or anything under `.lake/`; a constituent commit that itself modifies such a file keeps its "
            "change.",
            prompt,
        )
        self.assertIn(
            f"Run `git rebase --keep-empty --empty=keep --reapply-cherry-picks --onto {onto[:12]} {content.base[:12]}`.",
            prompt,
        )
        self.assertIn("`bash .tauceti-repair/step` does this stepping for you", prompt)
        self.assertNotIn("## Already on main", prompt, "nothing landed outside the queue")
        self.assertIn(
            "`git add` them, `git commit --allow-empty -C REBASE_HEAD` (the commit's own message and author, kept even "
            "when the resolution leaves it empty), then `GIT_EDITOR=true git rebase --continue`",
            prompt,
        )
        self.assertIn('`git commit --allow-empty --squash=<sha> -m "<explanation>"`', prompt)
        self.assertIn(
            f"`GIT_EDITOR=true GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash --keep-empty --empty=keep {onto[:12]}`",
            prompt,
        )
        self.assertIn("`python3 .tauceti-repair/check` verifies the chain", prompt)
        self.assertIn("It prints the first problem, or `ok`.", prompt)
        self.assertIn("Artifacts are warm", prompt)
        self.assertIn("Long builds are fine to run in the foreground: shell commands have a two-hour limit", prompt)
        self.assertLess(prompt.index("as its author intended, compiled and checked"), prompt.index("no `sorry`"))
        self.assertIn("no `set_option` (banned throughout Tau Ceti), no silenced linters", prompt)
        self.assertTrue(
            prompt.rstrip().endswith(
                "Finish with a short summary of what you changed in which PR's commit and the exit status of the "
                "final `lake build --iofail`."
            ),
            prompt[-300:],
        )
        # The listings file: one JSON object per member in chain order, with the replay state.
        records = [json.loads(line) for line in listings.read_text().splitlines()]
        self.assertEqual(
            records,
            [
                {
                    "position": i + 1,
                    "commit": c.sha,
                    "pr": i + 1,
                    "title": f"feat: add {i + 1}",
                    "accepted_head": content.members[i]["accepted_head"],
                    "provenance": "clean",
                    "landed_on_main": None,
                    "state": ("replayed", "conflicting", "pending")[i],
                }
                for i, c in enumerate(chain)
            ],
        )
        self.assertEqual(sh(seed, "status", "--porcelain"), "", "the listings are excluded from Git")
        self.assertEqual(sh(seed, "check-ignore", str(listings)), str(listings))
        custom = repair_task.Task.from_json({**task.to_json(), "instruction": "Custom instruction."})
        custom_prompt = repair.compose_prompt(
            repair_task.prompt_context(custom, None), repair_task.task_findings(custom), chain, seed
        )
        self.assertTrue(custom_prompt.startswith("Custom instruction."), "the service's instruction is used verbatim")
        self.assertIn("## Constituent commits", custom_prompt, "without a listings path the listings are inline")
        with self.assertRaises(ValueError):
            repair_task.Task.from_json({**raw, "kind": "mystery", "claim_token": "x", "expires_at": 1})

    def landed_fixture(self, prefix: str) -> tuple[Content, Path, str, list, repair_task.Task]:
        """A rebase conflict where PR 1 landed on ``main`` with a different patch (its file conflicts),
        ``main`` also added PR 2's file (the real conflict) and PR 3 landed with an identical patch."""
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=TMP))
        content = Content(root)
        seed = root / "seed"
        chain = repair.read_chain(seed, content.base, content.tip)
        sh(seed, "checkout", "--quiet", "--detach", content.base)
        landed_one = commit(seed, "feat: add 1 (#1)", {"TauCeti/One.lean": "theorem one : True := by trivial\n"})
        commit(seed, "main moved", {"TauCeti/Two.lean": "theorem two : True := by trivial\n"})
        sh(seed, "cherry-pick", chain[2].sha)
        sh(seed, "commit", "--quiet", "--amend", "-m", "feat: add 3 (#3)")
        onto = sh(seed, "rev-parse", "HEAD")
        sh(seed, "checkout", "--quiet", "--detach", content.tip)
        raw = content.task(9, kind="rebase_conflict")
        raw["onto"] = onto
        raw["diagnostics"] = [
            {
                "kind": "conflict",
                "source_pr": f"{PROJECT}#1",
                "paths": ["TauCeti/One.lean"],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {PROJECT}#1",
                "pending": [f"{PROJECT}#2", f"{PROJECT}#3"],
                "clean_prefix_tip": None,
            }
        ]
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        return content, seed, onto, [landed_one, onto], task

    def test_members_already_on_main_are_listed_and_the_step_script_keeps_them_empty(self):
        content, seed, onto, (landed_one, landed_three), task = self.landed_fixture("landed-")
        chain = repair.read_chain(seed, content.base, content.tip)
        listings = repair.listings_path(seed)
        prompt = repair.compose_prompt(
            repair_task.prompt_context(task, None, listings=listings), repair_task.task_findings(task), chain, seed
        )
        self.assertLess(len(prompt), 10_000, f"prompt is {len(prompt)} bytes")
        self.assertIn("## Already on main", prompt)
        self.assertLess(prompt.index("## Conflict evidence"), prompt.index("## Already on main"))
        self.assertLess(prompt.index("## Already on main"), prompt.index("## The result"))
        self.assertIn("2 of the 3 members' PRs have landed on `main` outside the queue", prompt)
        self.assertIn(f"- PR #1: feat: add 1, landed as `{landed_one[:12]}`", prompt)
        self.assertIn(f"- PR #3: feat: add 3, landed as `{landed_three[:12]}`", prompt)
        self.assertNotIn("- PR #2:", prompt)
        self.assertIn("a conflict in one of them is resolved by taking `main`'s version", prompt)
        self.assertIn("It is the first commit of the batch, so nothing replayed before it.", prompt)
        records = [json.loads(line) for line in listings.read_text().splitlines()]
        self.assertEqual([r["landed_on_main"] for r in records], [landed_one, None, landed_three])
        self.assertEqual([r["state"] for r in records], ["conflicting", "pending", "pending"])
        # The service's own flag, once it exists, is used as given and the history is not consulted.
        flagged = repair_task.prompt_context(task, None, listings=listings)
        flagged.members[1]["landed_externally"] = True
        flagged_prompt = repair.compose_prompt(flagged, repair_task.task_findings(task), chain, seed)
        self.assertIn("1 of the 3 members' PRs have landed", flagged_prompt)
        self.assertIn("- PR #2: feat: add 2\n", flagged_prompt)
        # The step script: PR 1's conflict is taken from main, PR 2's is real, PR 3 is kept empty.
        repair.compose_prompt(
            repair_task.prompt_context(task, None, listings=listings), repair_task.task_findings(task), chain, seed
        )
        step, check = listings.parent / "step", listings.parent / "check"
        self.assertTrue(os.access(step, os.X_OK))
        self.assertLess(len(step.read_text().splitlines()), 40)

        def run(script: Path) -> tuple[int, str]:
            result = subprocess.run(
                ["bash" if script is step else sys.executable, str(script)], cwd=seed, text=True, capture_output=True
            )
            return result.returncode, (result.stdout + result.stderr).strip()

        rc, out = run(step)
        self.assertEqual(rc, 1, out)
        self.assertEqual(
            out, f"stopped at PR #2, commit {chain[1].sha[:12]}: resolve the conflict in\nTauCeti/Two.lean"
        )
        self.assertEqual(run(check)[1], "a rebase is still in progress: finish it with `git rebase --continue`")
        (seed / "TauCeti" / "Two.lean").write_text("theorem two : True := trivial\n")
        sh(seed, "add", "TauCeti/Two.lean")
        rc, out = run(step)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith(f"rebase finished: 3 commits on {onto[:12]}"), out)
        self.assertEqual(run(check), (0, "ok"))
        result = repair.read_chain(seed, onto, sh(seed, "rev-parse", "HEAD"))
        self.assertEqual([c.pr for c in result], [1, 2, 3])
        self.assertEqual([c.empty for c in result], [True, False, True], "landed members are empty commits")
        self.assertEqual(
            sh(seed, "show", "HEAD~1:TauCeti/One.lean"), "theorem one : True := by trivial", "main's version"
        )
        self.assertEqual(
            sh(seed, "log", "-1", "--format=%an", result[0].sha), sh(seed, "log", "-1", "--format=%an", chain[0].sha)
        )
        # Run again when nothing is in progress: HEAD is already on the target, so nothing is replayed.
        rc, out = run(step)
        self.assertEqual((rc, out), (0, f"HEAD is already on {onto[:12]}; nothing to replay (run check)"))
        self.assertEqual(run(check), (0, "ok"))

    def test_the_check_script_judges_the_chain(self):
        content, seed, onto, _, task = self.landed_fixture("check-")
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

        self.assertEqual(run(), (1, f"HEAD is not on top of the target commit {onto[:12]}"))
        env = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": ":"}
        subprocess.run(["bash", str(listings.parent / "step")], cwd=seed, capture_output=True)
        (seed / "TauCeti" / "Two.lean").write_text("theorem two : True := trivial\n")
        sh(seed, "add", "TauCeti/Two.lean")
        subprocess.run(["bash", str(listings.parent / "step")], cwd=seed, capture_output=True, check=True)
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
        chain = repair.read_chain(seed, base, tip)
        self.assertEqual(len(chain), count)
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
            f"commit `{members[conflicting - 2]['commit'][:12]}`.",
            prompt,
        )
        self.assertIn(
            f"- conflicting commit: `{conflict_sha}` (PR #{conflicting}, position {conflicting} of {count}: "
            f"feat: add {conflicting})",
            prompt,
        )
        self.assertIn(
            f"- not yet replayed: {count - conflicting} commits after it (positions {conflicting + 1} to {count})",
            prompt,
        )
        self.assertNotIn("#10: feat: add 10", prompt, "members are left to the listings file")
        self.assertNotIn(f"{PROJECT}#{conflicting + 1}", prompt, "the pending list is in the file, not the prompt")
        self.assertIn("The build is cold", prompt)
        self.assertNotIn("Artifacts are warm", prompt)
        self.assertIn(f"`lake build TauCeti.M{conflicting}`", prompt)
        self.assertIn("Long builds are fine to run in the foreground: shell commands have a two-hour limit", prompt)
        # The file holds everything in chain order and Git ignores it.
        records = [json.loads(line) for line in listings.read_text().splitlines()]
        self.assertEqual(len(records), count)
        self.assertEqual([r["position"] for r in records], list(range(1, count + 1)))
        self.assertEqual([r["pr"] for r in records], list(range(1, count + 1)))
        self.assertEqual(records[conflicting - 1]["state"], "conflicting")
        self.assertEqual({r["state"] for r in records[: conflicting - 1]}, {"replayed"})
        self.assertEqual({r["state"] for r in records[conflicting:]}, {"pending"})
        self.assertEqual(records[9]["commit"], members[9]["commit"])
        self.assertEqual(records[9]["accepted_head"], f"{10:040x}")
        self.assertTrue(os.access(listings.parent / "check", os.X_OK))
        self.assertIn(f"TARGET = {onto!r}", (listings.parent / "check").read_text())
        self.assertTrue(os.access(listings.parent / "build", os.X_OK), "the scoped build script is written too")
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
        self.assertNotIn(".tauceti-repair", inline)

    def test_the_shell_limit_is_the_repair_task_s_own(self):
        from tauceti_worker import agents

        deadline = time.time() + 3 * 3600
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": str(repair.SHELL_LIMIT_SECONDS)}):
            env = agents._foreground_shell_env(deadline)
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], "7200000")
        self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], "7200000")
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": ""}):
            self.assertEqual(agents._foreground_shell_env(deadline)["BASH_MAX_TIMEOUT_MS"], "600000")
        with patch.dict(os.environ, {"TAUCETI_SHELL_LIMIT_SECONDS": "7200"}):
            self.assertEqual(agents._foreground_shell_env(time.time() + 30)["BASH_MAX_TIMEOUT_MS"][:2], "29")

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
        self.assertLess(len(self.script.read_text().splitlines()), 70)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
