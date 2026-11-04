#!/usr/bin/env python3
"""The worker-side queue repair task (tauceti_queue.repair_task) against a fake service.

No inference and no network: the queue service is an ``http.server`` in a thread, the content
repository is a local bare repository with a three-member attributed chain whose second member breaks
the build, ``lake`` is a stub on PATH that fails modules containing ``BROKEN``, ``gh`` is a stub that
grants admin permission, and ``claude`` is a scripted stub that rewrites the constituent commits the
way the agent is told to. Covered: the claim/renew/push/submit happy path with adoption; claim expiry
stopping the agent without a submission; a chain the service rejects, retried once with its report;
a locally invalid chain retried with the local report; a content change without an explanation,
completed by a follow-up turn in the same session; artifacts unavailable (released, or left to expire
when the service has no release endpoint); inference unavailable after bounded retries; an ordinary
agent failure released as such; a second claimant refused; and a checkpointed task resumed in its
recorded session.
"""

from __future__ import annotations

import json
import os
import re
import secrets
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
"""lake stub: `cache get` succeeds unless STUB_LAKE_CACHE_FAIL=1; `build` fails modules containing BROKEN."""
import os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["STUB_DIR"]) / "lake-calls.log"
log.open("a").write(" ".join(args) + "\n")
if args[:2] == ["cache", "get"]:
    if os.environ.get("STUB_LAKE_CACHE_FAIL") == "1":
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
# gh stub: the admin predicate reads repos/<owner>/<repo>; everything else is unexpected here.
case "$*" in
  *repos/*) echo '{"permissions": {"admin": '"${STUB_GH_ADMIN:-true}"'}}' ;;
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
            {"source_pr": f"{PROJECT}#{n}", "pr": n, "accepted_head": heads[n], "commit": c.sha, "provenance": "clean"}
            for n, c in zip((1, 2, 3), built.commits, strict=True)
        ]
        sh(seed, "push", "--quiet", "origin", f"{self.base}:refs/heads/queue/batch-1/base")
        sh(seed, "push", "--quiet", "origin", f"{self.tip}:refs/heads/queue/batch-1/tip")
        self.clone = root / "clone"
        subprocess.run(["git", "clone", "--quiet", str(self.bare), str(self.clone)], check=True)
        sh(self.clone, "config", "user.name", "Worker")
        sh(self.clone, "config", "user.email", "worker@example.invalid")

    def task(self, task_id: str = "t1", kind: str = "repair") -> dict:
        return {
            "task_id": task_id,
            "kind": kind,
            "repository": PROJECT,
            "batch_id": "b1",
            "batch_version": 3,
            "base": self.base,
            "tip": self.tip,
            "members": self.members,
            "diagnostics": {
                "compiler_errors": [
                    {"path": "TauCeti/Two.lean", "line": 1, "column": 20, "message": "unknown identifier BROKEN"}
                ],
                "failed_modules": ["TauCeti.Two"],
            },
            "retained_refs": ["refs/heads/queue/batch-1/base", "refs/heads/queue/batch-1/tip"],
        }


# ---- the fake service ----------------------------------------------------------------------------


class FakeService:
    """Claims with expiry, renewals, results validated with the chain tool against the bare repository,
    and an optional release endpoint. Scripted result responses override the validation."""

    def __init__(self, bare: Path, tasks: list[dict], *, ttl: float = 5.0, renew_after: float = 0.2):
        self.bare = bare
        self.tasks = {t["task_id"]: dict(t) for t in tasks}
        self.ttl = ttl
        self.renew_after = renew_after
        self.claims: dict[str, dict] = {}
        self.done: set[str] = set()
        self.results: list[dict] = []
        self.releases: list[dict] = []
        self.renewals = 0
        self.claim_attempts: list[dict] = []
        self.result_script: list[dict] = []
        self.renew_mode = "ok"  # ok | refuse | error
        self.release_supported = True
        self.lock = threading.Lock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                status, payload = service.handle(self.path, body, self.headers.get("Authorization"))
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def preclaim(self, task_id: str, token: str, worker: str) -> dict:
        with self.lock:
            self.claims[task_id] = {"token": token, "worker": worker, "expires_at": time.time() + self.ttl}
            return self.claims[task_id]

    def _claim_ok(self, task_id: str, token: str) -> bool:
        claim = self.claims.get(task_id)
        return bool(claim and claim["token"] == token and claim["expires_at"] > time.time())

    def handle(self, path: str, body: dict, auth: str | None):
        if auth != f"Bearer {TOKEN}":
            return 401, {"reason": "unauthorized"}
        with self.lock:
            if path == "/tasks/claim":
                self.claim_attempts.append(body)
                now = time.time()
                for task_id, task in self.tasks.items():
                    claim = self.claims.get(task_id)
                    if task_id in self.done or (claim and claim["expires_at"] > now):
                        continue
                    token = secrets.token_hex(8)
                    self.claims[task_id] = {"token": token, "worker": body.get("worker"), "expires_at": now + self.ttl}
                    return 200, {
                        **task,
                        "claim_token": token,
                        "expires_at": now + self.ttl,
                        "renew_after": self.renew_after,
                    }
                return 204, None
            match = re.fullmatch(r"/tasks/([^/]+)/(renew|result|release)", path)
            if not match:
                return 404, {"reason": "no such endpoint"}
            task_id, verb = match.groups()
            if verb == "release" and not self.release_supported:
                return 404, {"reason": "no such endpoint"}
            if task_id not in self.tasks:
                return 404, {"reason": "no such task", "task_id": task_id}
            if not self._claim_ok(task_id, body.get("claim_token", "")):
                return 409, {"reason": "claim_invalid", "adopted": False}
            if verb == "renew":
                if self.renew_mode == "refuse":
                    del self.claims[task_id]
                    return 410, {"reason": "claim_expired"}
                if self.renew_mode == "error":
                    return 503, {"reason": "unavailable"}
                self.renewals += 1
                self.claims[task_id]["expires_at"] = time.time() + self.ttl
                return 200, {"expires_at": self.claims[task_id]["expires_at"]}
            if verb == "release":
                self.releases.append({"task_id": task_id, **body})
                del self.claims[task_id]
                return 200, {"task_id": task_id, "released": True}
            # result
            record = {"task_id": task_id, **body}
            if self.result_script:
                response = self.result_script.pop(0)
            else:
                ref = body.get("pushed_ref", "")
                task = self.tasks[task_id]
                try:
                    pushed = sh(self.bare, "rev-parse", "--verify", ref)
                except subprocess.CalledProcessError:
                    response = {"adopted": False, "reason": "pushed_ref_missing", "validation": None}
                else:
                    report = chain_tool.validate(
                        self.bare, task["base"], pushed, [m["source_pr"] for m in task["members"]]
                    )
                    response = {
                        "adopted": report.ok and pushed == body.get("tip"),
                        "reason": "adopted" if report.ok else "validation",
                        "validation": report.to_dict(),
                    }
            record["response"] = response
            self.results.append(record)
            if response.get("adopted"):
                self.done.add(task_id)
                del self.claims[task_id]
            return 200, response


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

    def state(self, task_id="t1") -> dict:
        return json.loads((self.root / "state" / "tasks" / task_id / "task.json").read_text())


class HappyPath(Fixture):
    def test_claim_renew_push_submit_adopt(self):
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "adopted", outcome)
        # The service saw one claim, renewals while the agent ran, and one result with the pushed ref.
        self.assertEqual(len(self.service.results), 1)
        result = self.service.results[0]
        self.assertEqual(result["pushed_ref"], "refs/heads/queue/task-t1")
        self.assertEqual(result["tip"], outcome["tip"])
        self.assertTrue(result["response"]["adopted"])
        self.assertGreaterEqual(outcome["lease"]["renewals"], 1)
        self.assertFalse(outcome["lease"]["lost"])
        # The pushed chain is on the bare repository, validates, and member #2 is edited with an explanation.
        pushed = sh(self.content.bare, "rev-parse", "refs/heads/queue/task-t1")
        self.assertEqual(pushed, outcome["tip"])
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
        self.assertIn("TauCeti/Two.lean:1:20", prompt)
        self.assertIn("changed by PR #2", prompt)
        self.assertIn(f"Source-PR: {PROJECT}#3", prompt)
        self.assertIn("do not push", prompt)
        self.assertIn("repair task t1", prompt)
        self.assertIsNone(calls[0]["env"]["GH_TOKEN"])
        self.assertTrue(calls[0]["env"]["GIT_CONFIG_VALUE_0"].endswith("no-push"))
        self.assertTrue(calls[0]["env"]["CLAUDE_CONFIG_DIR"].startswith(str(self.root / "state")))
        self.assertEqual(calls[0]["cwd"], str(self.root / "state" / "worktrees" / "task-t1"))
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
        self.assertEqual(sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/t1/attempt-1"), outcome["tip"])
        self.assertEqual(sh(self.content.clone, "rev-parse", "refs/tauceti-queue/task/t1/input"), self.content.tip)
        self.assertIsNotNone(outcome["agent"][0]["attempts"][0]["estimated_cost_usd"])
        self.assertFalse((self.root / "state" / "worktrees" / "task-t1").exists())
        self.assertIsNone(repair_task.claim_and_run(cfg, self.client(cfg)), "the adopted task is not claimable again")

    def test_second_claimant_and_stale_token_are_refused(self):
        cfg = self.config()
        first = self.client(cfg, "worker-a@test").claim()
        self.assertIsNotNone(first)
        second = self.client(cfg, "worker-b@test")
        self.assertIsNone(second.claim(), "a claimed task is not offered to a second worker")
        stale = repair_task.Task.from_json({**first.to_json(), "claim_token": "not-the-token"})
        with self.assertRaises(repair_task.ClaimRefused):
            second.submit(stale, first.tip, "refs/heads/queue/task-t1", {})
        with self.assertRaises(repair_task.ClaimRefused):
            second.renew(stale)
        self.assertEqual(self.service.results, [])
        # Once the first claim expires the task is claimable again, by anyone.
        self.service.claims["t1"]["expires_at"] = time.time() - 1
        self.assertIsNotNone(second.claim())


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
        self.assertFalse(sh(self.content.bare, "rev-parse", "--verify", "-q", "refs/heads/queue/task-t1", check=False))

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
                "adopted": False,
                "reason": "validation",
                "validation": {
                    "ok": False,
                    "violations": [{"code": "accepted-head-mismatch", "detail": "#2 head differs"}],
                },
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
        # The second push updated the task branch in place (CAS against the first push).
        self.assertEqual(sh(self.content.bare, "rev-parse", "refs/heads/queue/task-t1"), outcome["tip"])

    def test_persistent_rejection_is_not_retried_again(self):
        cfg = self.config()
        self.service.result_script = [
            {"adopted": False, "reason": "validation", "validation": "wrong base"},
            {"adopted": False, "reason": "validation", "validation": "still wrong"},
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
        pushed = sh(self.content.bare, "rev-parse", "refs/heads/queue/task-t1")
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
        self.assertEqual(self.service.results, [])
        self.assertEqual(self.state()["state"], "released")
        self.assertNotIn("t1", self.service.claims, "the service freed the claim")

    def test_without_a_release_endpoint_the_claim_is_left_to_expire(self):
        os.environ["STUB_LAKE_CACHE_FAIL"] = "1"
        self.service.release_supported = False
        cfg = self.config()
        outcome = repair_task.claim_and_run(cfg, self.client(cfg))
        self.assertEqual(outcome["status"], "release_unsupported", outcome)
        self.assertEqual(outcome["reason"], "artifacts_unavailable")
        self.assertEqual(self.service.releases, [])
        self.assertEqual(self.service.results, [])
        self.assertIn("t1", self.service.claims, "the claim is held until it expires on its own")
        self.assertFalse(outcome["lease"]["lost"])


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


class Resume(Fixture):
    def test_checkpointed_task_resumes_its_session(self):
        os.environ["STUB_AGENT_MODE"] = "resume-only"
        cfg = self.config()
        client = self.client(cfg)
        # A previous runner claimed the task, prepared the worktree and was running the agent in sess-old.
        claim = self.service.preclaim("t1", "old-token", cfg.worker)
        task = repair_task.Task.from_json(
            {**self.content.task(), "claim_token": "old-token", "expires_at": claim["expires_at"], "renew_after": 0.2}
        )
        repair_task.fetch_retained(cfg.clone, task)
        worktree = cfg.root / "worktrees" / "task-t1"
        repair.checkout_worktree(cfg.clone, task.tip, worktree)
        state = repair_task.TaskState(cfg.tasks_dir / "t1", task)
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
        state = repair_task.TaskState(cfg.tasks_dir / "t1", task)
        state.write("agent_running")
        self.assertEqual(repair_task.resume_pending(cfg, self.client(cfg)), [])
        self.assertEqual(self.state()["state"], "claim_lost")
        self.assertEqual(self.agent_calls(), [])


class Records(unittest.TestCase):
    def test_task_record_and_prompt_for_a_rebase_conflict(self):
        root = Path(tempfile.mkdtemp(prefix="records-", dir=TMP))
        content = Content(root)
        raw = content.task("t9", kind="rebase_conflict")
        onto = commit(root / "seed", "main moved", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
        raw["diagnostics"] = {"conflict": {"member": f"{PROJECT}#2", "paths": ["TauCeti/Two.lean"], "onto": onto}}
        task = repair_task.Task.from_json({**raw, "claim_token": "x", "expires_at": time.time() + 60})
        self.assertEqual(task.validation_base, onto)
        self.assertEqual(task.member_keys, [f"{PROJECT}#1", f"{PROJECT}#2", f"{PROJECT}#3"])
        self.assertEqual(task.push_branch, "queue/task-t9")
        self.assertEqual(repair_task.Task.from_json(task.to_json()), task)
        chain = repair.read_chain(root / "seed", content.base, content.tip)
        prompt = repair.compose_prompt(
            repair_task.prompt_context(task, None), repair_task.task_findings(task), chain, root / "seed"
        )
        self.assertTrue(prompt.startswith(repair.AGENT_INSTRUCTION))
        self.assertIn("scripted rebase conflicted", prompt)
        self.assertIn(f"--onto {onto[:12]} {content.base[:12]}", prompt)
        self.assertIn("`TauCeti/Two.lean` is changed by PR #2", prompt)
        self.assertIn(f"Source-PR: {PROJECT}#2`, `Accepted-Head: {content.members[1]['accepted_head']}", prompt)
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
