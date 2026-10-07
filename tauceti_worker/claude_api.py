"""Host accounting bridge for Claude subprocesses, including external engines."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import sys
import threading
import uuid
from decimal import Decimal
from pathlib import Path

from .budget import Budget, BudgetError, budget_dir, money
from .claude_api_client import atomic
from .runtime_status import report_runtime


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
    os.environ["ANTHROPIC_API_KEY"] = key
    if getattr(args, "loop", False):
        os.environ["TAUCETI_API_WAIT"] = "1"


def api_mode():
    return os.environ.get("TAUCETI_CLAUDE_BILLING") == "api"


class ClaudeAPIContext:
    def __init__(self, worker, phase):
        self.worker = worker
        self.phase = phase
        self.budget = Budget()
        self.enabled = os.environ.get("TAUCETI_USE_BUDGET") == "1"
        self.directory = self.budget.directory / "bridges" / uuid.uuid4().hex
        self.bin = self.directory / "bin"
        self.stop = threading.Event()
        self.errors = []
        self.invocations = set()

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            (self.directory / "key").unlink(missing_ok=True)
            raise

    def start(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.bin.mkdir(mode=0o700)
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        keyfile = self.directory / "key"
        fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(key)
        real = shlex.split(os.environ.get("TAUCETI_CLAUDE_CMD", "claude"))
        binary = shutil.which(real[0])
        if not binary:
            raise BudgetError("Claude executable not found")
        real[0] = str(Path(binary).resolve())
        shim = self.bin / "claude"
        shutil.copy(Path(__file__).with_name("claude_api_client.py"), shim)
        # Use the actual host interpreter even on systems whose python3 is older than 3.11.
        text = shim.read_text().replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
        shim.write_text(text)
        shim.chmod(0o700)
        self.config = dict(
            command=real, key=str(keyfile), bridge=str(self.directory), wait=os.environ.get("TAUCETI_API_WAIT") == "1"
        )
        atomic(self.bin / "config.json", self.config)
        self.previous_path = os.environ.get("PATH", "")
        self.previous_context = os.environ.get("TAUCETI_API_CONTEXT")
        self.previous_cmd = os.environ.get("TAUCETI_CLAUDE_CMD")
        os.environ["PATH"] = f"{self.bin}:{self.previous_path}"
        os.environ["TAUCETI_API_CONTEXT"] = str(self.directory)
        os.environ["TAUCETI_CLAUDE_CMD"] = str(shim)
        (self.directory / "heartbeat").touch()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def handle(self, request):
        action = request["action"]
        if action == "admit":
            if self.enabled:
                answer = self.budget.admit(
                    request["id"],
                    self.worker,
                    request["model"],
                    self.phase,
                    bridge=str(self.directory),
                    resume=request.get("resume"),
                )
            else:
                answer = dict(allowed=True)
            if answer["allowed"]:
                self.invocations.add(request["id"])
            else:
                report_runtime("waiting-budget", detail=answer["reason"], budget=self.budget.snapshot())
            return answer
        if action == "settle":
            self.record(request)
            return dict(ok=True)
        if action == "cooldown":
            self.budget.cooldown(request["model"], min(3600, float(request["seconds"])))
            return dict(ok=True)
        raise BudgetError("unknown bridge request")

    def record(self, receipt):
        if receipt["id"] not in self.invocations:
            raise BudgetError("receipt has no admission in this bridge")
        if not self.enabled:
            return
        totals = receipt["totals"]
        cost = None
        if totals is not None:
            with self.budget.locked():
                state = self.budget.replay()
            baseline = {}
            for session in state["sessions"].values():
                if session["id"] == receipt["id"]:
                    continue
                for provider_id, value in session.get("totals", {}).items():
                    baseline[provider_id] = max(baseline.get(provider_id, Decimal(0)), money(value))
            differences = [money(v) - baseline.get(k, Decimal(0)) for k, v in totals.items()]
            if all(v >= 0 for v in differences):
                cost = sum(differences, Decimal(0))
        self.budget.settle(receipt["id"], cost, receipt["outcome"], totals=totals)

    def serve(self):
        while not self.stop.is_set():
            (self.directory / "heartbeat").touch()
            for path in self.directory.glob("*.request"):
                reply = path.with_suffix(".reply")
                if reply.exists():
                    continue
                try:
                    response = self.handle(json.loads(path.read_text()))
                except (BudgetError, OSError, KeyError, ValueError, TypeError, OverflowError) as error:
                    response = dict(error=str(error))
                    self.errors.append(str(error))
                try:
                    atomic(reply, response)
                except OSError as error:
                    self.errors.append(str(error))
            self.stop.wait(0.1)

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        # Drain durable receipts even if a child died before receiving its settlement acknowledgement.
        for path in self.directory.glob("*.receipt"):
            try:
                self.record(json.loads(path.read_text()))
            except (BudgetError, OSError, ValueError) as error:
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
        os.environ["PATH"] = self.previous_path
        for variable, old in (
            ("TAUCETI_API_CONTEXT", self.previous_context),
            ("TAUCETI_CLAUDE_CMD", self.previous_cmd),
        ):
            if old is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = old
        (self.directory / "key").unlink(missing_ok=True)
        if self.errors:
            print("tauceti API accounting: " + "; ".join(self.errors), file=sys.stderr)


def api_context(worker, phase):
    return ClaudeAPIContext(worker, phase) if api_mode() else contextlib.nullcontext()
