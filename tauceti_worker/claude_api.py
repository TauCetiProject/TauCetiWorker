"""Host accounting bridge for Claude subprocesses, including external engines."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shlex
import shutil
import stat
import sys
import threading
import uuid
from pathlib import Path

from .budget import Budget, BudgetError, budget_dir, money
from .claude_api_client import api_environment, atomic, authenticate
from .runtime_status import report_runtime

CURRENT_CONTEXT = None


def blocked_reason():
    return CURRENT_CONTEXT.blocked_reason if CURRENT_CONTEXT is not None else None


def admission_failure(message):
    from .config import AdmissionUnavailable, NoProgress

    waiting = message.startswith(("waiting", "unresolved", "API throughput", "provider session already"))
    return AdmissionUnavailable(message) if waiting else NoProgress(message)


def review_environment():
    if not api_mode() or CURRENT_CONTEXT is None:
        return None
    return {**os.environ, "ANTHROPIC_API_KEY": (CURRENT_CONTEXT.directory / "key").read_text()}


def safe_message(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise BudgetError("invalid accounting message file")
        return json.load(file)


def configure_api(args, agent):
    mode = getattr(args, "claude_billing", "subscription")
    enabled = getattr(args, "budget", False)
    keyfile = getattr(args, "anthropic_api_key_file", None)
    if mode != "api":
        if enabled or keyfile:
            raise BudgetError("--budget and --anthropic-api-key-file require --claude-billing api")
        os.environ.pop("TAUCETI_CLAUDE_BILLING", None)
        return
    if agent != "claude":
        raise BudgetError("--claude-billing api requires --agent claude")
    if getattr(args, "ignore_quota", False) and getattr(args, "cmd", None) != "_round":
        raise BudgetError("API mode does not use --ignore-quota")
    if getattr(args, "quota_cmd", None) or getattr(args, "pace", None) or getattr(args, "auto_refresh", False):
        raise BudgetError("API mode does not use subscription pacing or OAuth refresh")
    directory = budget_dir().resolve()
    os.environ["TAUCETI_BUDGET_DIR"] = str(directory)
    os.environ["TAUCETI_CLAUDE_BILLING"] = "api"
    os.environ["TAUCETI_USE_BUDGET"] = "1" if enabled else "0"
    if keyfile:
        path = Path(keyfile).expanduser().resolve()
        key = path.read_text().strip()
        os.environ["TAUCETI_ANTHROPIC_KEY_FILE"] = str(path)
        args.anthropic_api_key_file = str(path)
    else:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        os.environ.pop("TAUCETI_ANTHROPIC_KEY_FILE", None)
    if not key or "\n" in key or "\r" in key:
        raise BudgetError("API mode needs ANTHROPIC_API_KEY or --anthropic-api-key-file containing one key")
    if not keyfile:
        credentials = directory / "credentials"
        credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = credentials / uuid.uuid4().hex
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(key)
        atexit.register(path.unlink, missing_ok=True)
    args.anthropic_api_key_file = str(path)
    os.environ["TAUCETI_ANTHROPIC_KEY_FILE"] = str(path)
    os.environ.pop("ANTHROPIC_API_KEY", None)


def api_mode():
    return os.environ.get("TAUCETI_CLAUDE_BILLING") == "api"


class ClaudeAPIContext:
    def __init__(self, worker, phase, *, host=True, model=None):
        self.worker = worker
        self.phase = phase
        self.host = host
        self.model = model
        self.budget = Budget()
        self.enabled = os.environ.get("TAUCETI_USE_BUDGET") == "1"
        self.directory = self.budget.directory / "bridges" / uuid.uuid4().hex
        self.bin = self.directory / "bin"
        self.inbox = self.directory / "inbox"
        self.outbox = self.directory / "outbox"
        self.stop = threading.Event()
        self.errors = []
        self.invocations = set()
        self.admission_ids = {}
        self.models = {}
        self.blocked_reason = None
        self.previous_path = os.environ.get("PATH", "")
        self.previous_context = os.environ.get("TAUCETI_API_CONTEXT")
        self.previous_cmd = os.environ.get("TAUCETI_CLAUDE_CMD")
        self.parent_context = CURRENT_CONTEXT

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.stop.set()
            for name in ("thread", "heartbeat_thread"):
                thread = getattr(self, name, None)
                if thread is not None and thread.is_alive():
                    thread.join()
            self.restore()
            (self.directory / "key").unlink(missing_ok=True)
            raise

    def start(self):
        global CURRENT_CONTEXT
        if self.enabled:
            snapshot = self.budget.snapshot(worker=self.worker, model=self.model, phase=self.phase)
            if (
                snapshot["unresolved"]
                or money(snapshot["available"]) <= 0
                or money(snapshot["available"]) < money(snapshot["admission_threshold"])
            ):
                reason = "unresolved session cost" if snapshot["unresolved"] else "waiting for funding"
                if not snapshot["unresolved"] and self.model:
                    self.budget.wait_for_funding(self.worker, self.model, self.phase)
                report_runtime("waiting-budget", detail=reason, budget=snapshot)
                raise BudgetError(reason)
        self.directory.mkdir(parents=True, mode=0o700)
        self.bin.mkdir(mode=0o700)
        self.inbox.mkdir(mode=0o700)
        self.outbox.mkdir(mode=0o700)
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if path := os.environ.get("TAUCETI_ANTHROPIC_KEY_FILE"):
            key = Path(path).read_text().strip()
        keyfile = self.directory / "key"
        fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(key)
        real = shlex.split(os.environ.get("TAUCETI_CLAUDE_CMD", "claude"))
        binary = shutil.which(real[0])
        if binary:
            real[0] = str(Path(binary).resolve())
        if self.host:
            if not binary:
                raise BudgetError("Claude executable not found")
            try:
                authenticate(real, api_environment(keyfile))
            except (RuntimeError, OSError) as error:
                raise BudgetError(str(error)) from None
        shim = self.bin / "claude"
        shutil.copy(Path(__file__).with_name("claude_api_client.py"), shim)
        # Use the actual host interpreter even on systems whose python3 is older than 3.11.
        text = shim.read_text().replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
        shim.write_text(text)
        shim.chmod(0o700)
        self.config = dict(command=real, key=str(keyfile), bridge=str(self.directory), wait=False)
        atomic(self.bin / "config.json", self.config)
        os.environ["PATH"] = f"{self.bin}:{self.previous_path}"
        os.environ["TAUCETI_API_CONTEXT"] = str(self.directory)
        os.environ["TAUCETI_CLAUDE_CMD"] = str(shim)
        (self.outbox / "heartbeat").touch()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.heartbeat_thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.heartbeat_thread.start()
        CURRENT_CONTEXT = self
        return self

    def handle(self, request):
        action = request["action"]
        if action == "preflight-failed":
            self.blocked_reason = "Claude API authentication unavailable"
            return dict(ok=True)
        if action == "admit":
            ticket = request["id"]
            if not isinstance(ticket, str) or len(ticket) != 32 or not isinstance(request["model"], str):
                raise BudgetError("invalid admission request")
            invocation = self.admission_ids.setdefault(ticket, uuid.uuid4().hex)
            if self.enabled:
                answer = self.budget.admit(
                    invocation,
                    self.worker,
                    request["model"],
                    self.phase,
                    bridge=str(self.directory),
                    resume=request.get("resume"),
                )
            else:
                answer = dict(allowed=True)
            if answer["allowed"]:
                self.invocations.add(invocation)
                self.models[invocation] = request["model"]
                self.blocked_reason = None
                answer["id"] = invocation
                report_runtime(
                    "running",
                    phase=self.phase,
                    claude_billing="api",
                    detail="Claude API session running",
                    **({"budget": self.budget.snapshot()} if self.enabled else {}),
                )
            else:
                self.blocked_reason = answer["reason"]
                report_runtime("waiting-budget", detail=answer["reason"], budget=self.budget.snapshot())
            return answer
        if action == "settle":
            self.record(request)
            return dict(ok=True)
        if action == "cooldown":
            if self.models.get(request["id"]) != request["model"]:
                raise BudgetError("cooldown requires an admitted invocation and its model")
            self.budget.cooldown(request["model"], min(3600, float(request["seconds"])))
            return dict(ok=True)
        raise BudgetError("unknown bridge request")

    def record(self, receipt):
        if receipt["id"] not in self.invocations:
            raise BudgetError("receipt has no admission in this bridge")
        if receipt["outcome"] == "launch-failed":
            self.blocked_reason = "native Claude launch failed before spending"
        if not self.enabled:
            return
        self.budget.settle_receipt(receipt)

    def heartbeat(self):
        while not self.stop.is_set():
            if not self.thread.is_alive():
                return
            (self.outbox / "heartbeat").touch()
            self.stop.wait(1)

    def serve(self):
        while not self.stop.is_set():
            for path in self.inbox.glob("*.request"):
                if len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem):
                    continue
                reply = self.outbox / (path.stem + ".reply")
                if reply.exists():
                    continue
                message = {}
                try:
                    message = safe_message(path)
                    response = self.handle(message)
                except Exception as error:
                    response = dict(error=str(error))
                    message = {}
                    self.errors.append(str(error))
                try:
                    if message.get("reply", True):
                        atomic(reply, response)
                    else:
                        path.unlink(missing_ok=True)
                except OSError as error:
                    self.errors.append(str(error))
            for reply in self.outbox.glob("*.reply"):
                if not (self.inbox / (reply.stem + ".request")).exists():
                    try:
                        reply.unlink(missing_ok=True)
                    except OSError as error:
                        self.errors.append(str(error))
            self.stop.wait(0.1)

    def __exit__(self, *_):
        try:
            self.finish()
        finally:
            self.restore()
            (self.directory / "key").unlink(missing_ok=True)

    def finish(self):
        self.stop.set()
        self.thread.join()
        self.heartbeat_thread.join()
        # Drain durable receipts even if a child died before receiving its settlement acknowledgement.
        for path in self.inbox.glob("*.receipt"):
            try:
                self.record(safe_message(path))
            except Exception as error:
                self.errors.append(str(error))
        if self.enabled:
            try:
                with self.budget.locked():
                    sessions = self.budget.replay()["sessions"]
                for invocation in self.invocations:
                    if sessions[invocation]["status"] == "active":
                        self.budget.settle(invocation, None, "missing cost receipt")
                report_runtime(budget=self.budget.snapshot())
            except (BudgetError, OSError) as error:
                self.errors.append(str(error))
        if self.errors:
            print("tauceti API accounting: " + "; ".join(self.errors), file=sys.stderr)

    def restore(self):
        global CURRENT_CONTEXT
        os.environ["PATH"] = self.previous_path
        CURRENT_CONTEXT = self.parent_context
        for variable, old in (
            ("TAUCETI_API_CONTEXT", self.previous_context),
            ("TAUCETI_CLAUDE_CMD", self.previous_cmd),
        ):
            if old is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = old


def api_context(worker, phase, *, host=True, model=None):
    return ClaudeAPIContext(worker, phase, host=host, model=model) if api_mode() else contextlib.nullcontext()
