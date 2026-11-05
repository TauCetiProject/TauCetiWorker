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
checkpoint, which the next start resumes; and the host preflight checks.
"""

from __future__ import annotations

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
from tauceti_queue import repair, repair_task  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"
TOKEN = "worker-token"

# ---- stubs on PATH -------------------------------------------------------------------------------

LAKE = r'''#!/usr/bin/env python3
"""lake stub: `cache get` succeeds unless STUB_LAKE_CACHE_FAIL=1 or the `--rev` is listed in
STUB_LAKE_CACHE_FAIL_REVS (comma separated); `build` fails modules containing BROKEN."""
import os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["STUB_DIR"]) / "lake-calls.log"
log.open("a").write(" ".join(args) + "\n")
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
    modules = sorted(Path("TauCeti").glob("*.lean"))
    rc = 0
    for index, path in enumerate(modules, 1):
        name = "TauCeti." + path.stem
        if "BROKEN" in path.read_text():
            print(f"✖ [{index}/{len(modules)}] Building {name}")
            print(f"error: {path}:1:1: unknown identifier BROKEN")
            rc = 1
        else:
            print(f"✔ [{index}/{len(modules)}] Built {name} (0.1s)")
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
"""claude stub: a scripted repair agent. STUB_AGENT_MODE selects the behaviour; every call is logged."""
import json, os, re, subprocess, sys, time
from pathlib import Path

args = sys.argv[1:]
stub_dir = Path(os.environ["STUB_DIR"])
calls = stub_dir / "agent-calls.jsonl"
previous = sum(1 for _ in calls.open()) if calls.exists() else 0
call = previous + 1
prompt = args[args.index("-p") + 1] if "-p" in args else ""
resume = args[args.index("--resume") + 1] if "--resume" in args else None
mode = os.environ.get("STUB_AGENT_MODE", "repair")
with calls.open("a") as handle:
    handle.write(json.dumps({"call": call, "mode": mode, "resume": resume, "argv": args[:1] + [a for a in args[1:] if a != prompt], "prompt": prompt, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_VALUE_0", "CLAUDE_CONFIG_DIR")}}) + "\n")
session = "sess-" + str(call)
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)


def git(*a, **kw):
    return subprocess.run(["git", *a], text=True, capture_output=True, check=True, **kw).stdout.strip()


def finish(text="done", rc=0):
    print(json.dumps({"type": "assistant", "session_id": session, "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2}, "content": [{"type": "text", "text": text}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.01, "result": text, "session_id": session, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2}}), flush=True)
    sys.exit(rc)


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


if mode == "repair":
    rebuild()
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
elif mode == "noop":
    finish("nothing to do")
elif mode == "resume-only":
    if resume is None:
        finish("expected a resumed session", rc=3)
    rebuild()
    finish()
else:
    finish(f"unknown mode {mode}", rc=2)
'''

for name, text in (("lake", LAKE), ("gh", GH), ("claude", CLAUDE)):
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

    def __init__(self, root: Path):
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
        for number, files in (
            (1, {"TauCeti/One.lean": "theorem one : True := trivial\n"}),
            (2, {"TauCeti/Two.lean": "theorem two : True := BROKEN\n"}),
            (3, {"TauCeti/Three.lean": "theorem three : True := trivial\n"}),
        ):
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
                {
                    "kind": "compiler",
                    "module": "TauCeti/Two.lean",
                    "message": "TauCeti/Two.lean:1:20: unknown identifier BROKEN",
                },
                {"kind": "build", "module": "TauCeti/Two.lean", "message": "module failed to build"},
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
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=TMP))
        self.content = Content(self.root)
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        os.environ["STUB_DIR"] = str(self.stub_dir)
        os.environ["STUB_AGENT_MODE"] = "repair"
        os.environ.pop("STUB_LAKE_CACHE_FAIL", None)
        os.environ.pop("STUB_LAKE_CACHE_FAIL_REVS", None)
        os.environ.pop("STUB_SLEEP", None)
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
        )
        values.update(overrides)
        return repair_task.RunnerConfig(**values)

    def client(self, cfg: repair_task.RunnerConfig, worker: str | None = None) -> repair_task.TaskClient:
        return repair_task.TaskClient(cfg.service, TOKEN, worker or cfg.worker)

    def agent_calls(self) -> list[dict]:
        path = self.stub_dir / "agent-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def lake_calls(self) -> list[str]:
        path = self.stub_dir / "lake-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def state(self, task_id="1") -> dict:
        return json.loads((self.root / "state" / "tasks" / task_id / "task.json").read_text())

    def submission_ref(self, claim_id: int = 1, task_id: int = 1) -> str:
        return f"refs/tauceti-queue/submissions/{task_id}/{claim_id}"


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
        # Artifacts were prepared before the agent: Mathlib cache, base cache at the base, incremental build.
        lake = self.lake_calls()
        self.assertEqual(lake[0], "exe cache get")
        self.assertIn(f"--rev={self.content.base}", lake[1])
        self.assertTrue(lake[2].startswith("build --iofail"))
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertEqual(outcome["artifacts"]["incremental_build"]["failed_modules"], ["TauCeti.Two"])
        # One agent call with the plan's instruction, the diagnostics, the members and no credentials.
        calls = self.agent_calls()
        self.assertEqual(len(calls), 1)
        prompt = calls[0]["prompt"]
        self.assertTrue(prompt.startswith(repair.AGENT_INSTRUCTION))
        self.assertIn("`TauCeti/Two.lean:1:20`", prompt)
        self.assertIn("unknown identifier BROKEN", prompt)
        self.assertIn("changed by PR #2", prompt)
        self.assertIn("- build in `TauCeti/Two.lean` (PR #2): module failed to build", prompt)
        self.assertIn(f"Source-PR: {PROJECT}#3", prompt)
        self.assertIn("do not push", prompt)
        self.assertIn("repair task 1", prompt)
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
        self.assertIn("lake cache get", outcome["detail"])
        self.assertEqual(self.agent_calls(), [], "the agent never ran cold")
        self.assertEqual(len(self.service.releases), 1)
        self.assertEqual(self.service.releases[0]["reason"], "artifacts_unavailable")
        self.assertIn("lake cache get", self.service.releases[0]["error"])
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

    def test_the_rebase_target_is_fetched_first(self):
        os.environ["STUB_LAKE_CACHE_FAIL_REVS"] = self.content.base  # the stale base has no outputs
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        self.assertEqual(self.fetches(), [self.onto], "onto's cache was found; the base was never tried")
        self.assertTrue(outcome["artifacts"]["available"])
        self.assertEqual(outcome["artifacts"]["cache_revision"], self.onto)
        self.assertNotIn("cold", outcome["artifacts"])
        self.assertEqual(self.service.releases, [])
        self.assertEqual(len(self.service.results), 1)

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
            sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/1/retained/queue/inputs/449"),
            self.content.tip,
        )
        # A chunk the remote refuses is fetched ref by ref, so the error names the missing ref; the
        # other chunks are still one call each.
        calls.clear()
        missing = refs[:250] + ["refs/heads/queue/inputs/absent"] + refs[250:]
        task = repair_task.Task.from_json({**task.to_json(), "retained_refs": missing})
        with self.assertRaises(repair_task.SourcesUnavailable) as failed:
            repair_task.fetch_retained(self.content.clone, task, chunk=200)
        self.assertIn("queue/inputs/absent", str(failed.exception))
        self.assertEqual(len(calls[0]) - 3, 200)
        self.assertEqual(len(calls[1]) - 3, 200, "the second chunk holds the missing ref")
        self.assertTrue(all(len(c) - 3 == 1 for c in calls[2:]), "then its refs one at a time")

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


class Records(unittest.TestCase):
    def test_task_record_and_prompt_for_a_rebase_conflict(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task(9, kind="rebase_conflict")
        onto = commit(root / "seed", "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
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
        chain = repair.read_chain(root / "seed", content.base, content.tip)
        prompt = repair.compose_prompt(
            repair_task.prompt_context(task, None), repair_task.task_findings(task), chain, root / "seed"
        )
        self.assertTrue(prompt.startswith(repair.AGENT_INSTRUCTION))
        self.assertIn("scripted rebase conflicted", prompt)
        self.assertIn(f"--onto {onto[:12]} {content.base[:12]}", prompt)
        self.assertIn("`TauCeti/Two.lean` is changed by PR #2", prompt)
        self.assertIn(f"not yet replayed after it: {PROJECT}#3", prompt)
        self.assertIn(f"Source-PR: {PROJECT}#2`, `Accepted-Head: {content.members[1]['accepted_head']}", prompt)
        custom = repair_task.Task.from_json({**task.to_json(), "instruction": "Custom instruction."})
        custom_prompt = repair.compose_prompt(
            repair_task.prompt_context(custom, None), repair_task.task_findings(custom), chain, root / "seed"
        )
        self.assertTrue(custom_prompt.startswith("Custom instruction."), "the service's instruction is used verbatim")
        with self.assertRaises(ValueError):
            repair_task.Task.from_json({**raw, "kind": "mystery", "claim_token": "x", "expires_at": 1})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
