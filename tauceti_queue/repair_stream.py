"""Repair while the build still streams.

The first production repair spent its whole first ``lake build --iofail`` pass collecting errors
before touching anything. Here the worker runs that build itself, in the background, in the task's
worktree: the log goes to ``.tauceti-repair/build.log``, the worker tails it and appends each compiler
error to ``.tauceti-repair/errors.jsonl`` as it appears (module, file, line, column, the message with
its continuation lines, and the member whose squash commit last touched the file), and
``.tauceti-repair/build.lock`` exists while the build runs. The agent is launched at the first error,
or when the build ends, whichever comes first, with a "Build in progress" prompt section naming the
files and the rule that exactly one ``lake build`` runs at a time: no build of its own while the lock
exists, and ``.tauceti-repair/build <Module> ...`` (``repair_tools``) for a scoped build once the lock
is free. When the build ends the worker appends a ``build finished, exit N, M errors total`` line to
the error list and removes the lock. The agent's final whole-library ``lake build --iofail`` after its
fixes is still the only verdict. The lock is ``repair_tools``'s, the one the agent's build script takes, and
the build runs in ``repair_sandbox``'s offline sandbox like every build of the worker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from tauceti_queue import repair_sandbox, repair_tools
from tauceti_queue.diagnostics import ANSI, LAKE, SOURCE, STAMP, module_of

BUILD_LOG = "build.log"
ERRORS_FILE = "errors.jsonl"
LOCK_FILE = repair_tools.LOCK_DIR
POLL_SECONDS = 0.25
MAX_MESSAGE = 4000
# Lines that end an error's continuation: a job line, another located message, a Lake summary.
JOB_LINE = re.compile(r"^(?:[✔✖⚠ℹ✓×!]\s*)?\[\d+/\d+\] ")
LOCATED = re.compile(r"^(?:(?:error|warning|info|trace): )?\S+\.lean:\d+:\d+:")
PREFIXED = re.compile(r"^(?:error|warning|info|trace|Error|Warning):")
SUMMARY = ("Some required targets logged failures:", "Build completed", "Build failed", "$ ", "- TauCeti.")


class ErrorParser:
    """Compiler errors of a ``lake build`` log, fed line by line. ``feed`` returns the errors the line
    completes (an error is complete when the next line is not part of its message); ``finish`` closes
    the last one at the end of the log."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self._open: dict | None = None
        self._seen: set[tuple] = set()

    def feed(self, raw: str) -> list[dict]:
        line = STAMP.sub("", ANSI.sub("", raw.rstrip("\r\n").lstrip("﻿")))
        source = SOURCE.match(line) or LAKE.match(line)
        if source and len(source.group(1)) <= 512:
            closed = self._close()
            path = source.group(1)
            self._open = {
                "module": module_of(path),
                "file": path,
                "line": int(source.group(2)),
                "column": int(source.group(3)),
                "message": line[source.end() :].strip(),
            }
            return closed
        if self._open is None:
            return []
        if (
            not line.strip()
            or JOB_LINE.match(line)
            or LOCATED.match(line)
            or PREFIXED.match(line)
            or any(line.startswith(s) for s in SUMMARY)
        ):
            return self._close()
        if len(self._open["message"]) < MAX_MESSAGE:
            self._open["message"] = (self._open["message"] + "\n" + line.rstrip())[:MAX_MESSAGE]
        return []

    def finish(self) -> list[dict]:
        return self._close()

    def _close(self) -> list[dict]:
        error, self._open = self._open, None
        if error is None:
            return []
        key = (error["file"], error["line"], error["column"])
        if key in self._seen:
            return []
        self._seen.add(key)
        self.errors.append(error)
        return [error]


def parse_errors(lines: Iterable[str]) -> list[dict]:
    parser = ErrorParser()
    for line in lines:
        parser.feed(line)
    parser.finish()
    return parser.errors


class StreamingBuild:
    """One background ``lake build --iofail`` in ``worktree`` with its log, error list and lock in
    ``directory`` (the worktree's ``.tauceti-repair``). ``chain`` (``repair.ChainCommit`` records,
    oldest first) and ``repository`` attribute each error to the member whose squash commit last
    touched the file. ``ready`` is set at the first error or at the end of the build; ``finished``
    when the build has ended and the lock is gone. ``copy_log_to`` receives the whole log afterwards
    (the worktree is removed when the task ends)."""

    def __init__(
        self,
        worktree: Path,
        directory: Path,
        env: dict,
        *,
        chain: list,
        repository: str,
        lake: tuple[str, ...] = ("lake",),
        timeout: float | None = None,
        copy_log_to: Path | None = None,
    ) -> None:
        self.worktree = worktree
        self.directory = directory
        self.env = env
        self.chain = chain
        self.repository = repository
        self.lake = tuple(lake)
        self.timeout = timeout
        self.copy_log_to = copy_log_to
        self.log_path = directory / BUILD_LOG
        self.errors_path = directory / ERRORS_FILE
        self.lock_path = directory / LOCK_FILE
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.errors: list[dict] = []
        self.rc: int | None = None
        self.timed_out = False
        self.started_at: float | None = None
        self.seconds: float | None = None
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._owners: dict[str, dict] = {}

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self) -> StreamingBuild:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.errors_path.write_text("")
        repair_tools.take_lock(self.lock_path, log=lambda *a, **k: None)
        argv = [*self.lake, "build", "--iofail"]
        self.started_at = time.monotonic()
        with self.log_path.open("wb") as handle:
            handle.write(f"$ {' '.join(argv)}\n".encode())
            try:
                # CI's offline bwrap sandbox, as every build the worker runs (``repair_sandbox``).
                self._process = subprocess.Popen(
                    repair_sandbox.sandboxed_lake(argv, self.worktree, self.env),
                    cwd=self.worktree,
                    env=self.env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
            except OSError as error:
                handle.write(f"{error}\n".encode())
                self._end(127)
                return self
        self._thread = threading.Thread(target=self._tail, name="repair-stream-build", daemon=True)
        self._thread.start()
        return self

    def wait_for_agent(self, cancel: threading.Event, poll: float = 0.5) -> bool:
        """Block until the first error or the end of the build; False when ``cancel`` was set first."""
        while not self.ready.wait(poll):
            if cancel.is_set():
                return False
        return True

    def stop(self, timeout: float = 30.0) -> None:
        """End the build if it is still running (the task is over or interrupted) and release the lock."""
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._thread is not None:
            self._thread.join(timeout)
        if not self.finished.is_set():
            self._end(process.returncode if process is not None and process.returncode is not None else -1)

    # -- the record -------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.started_at is not None and not self.finished.is_set()

    @property
    def failed_modules(self) -> list[str]:
        return list(dict.fromkeys(e["module"] for e in self.errors))

    def summary(self) -> dict:
        return {
            "log": str(self.log_path),
            "errors_file": str(self.errors_path),
            "errors": len(self.errors),
            "failed_modules": self.failed_modules,
            "rc": self.rc,
            "timed_out": self.timed_out,
            "finished": self.finished.is_set(),
            "seconds": self.seconds,
        }

    def prompt_section(self) -> list[str]:
        directory = self.directory.name
        count = len(self.errors)
        errors = f"{count} error{'s' if count != 1 else ''}"
        if self.finished.is_set():
            status = (
                f"The whole-library `lake build --iofail` the worker started in this worktree has finished: exit "
                f"{self.rc}, {errors}" + (" (it hit the worker's time limit)" if self.timed_out else "") + "."
            )
        else:
            status = (
                "The whole-library `lake build --iofail` is running in the background in this worktree, started by "
                f"the worker; it has reported {errors} so far and keeps going on the rest of the library."
            )
        return [
            "",
            "## Build in progress",
            "",
            status
            + f" Its log is `{directory}/{BUILD_LOG}` and its error list is `{directory}/{ERRORS_FILE}`, one JSON "
            "object per line with `module`, `file`, `line`, `column`, `message` (continuation lines included), "
            "`member` (the PR whose squash commit last touched the file) and `commit`; both grow while the build "
            "runs, so read the error list again before deciding what is left. When the build ends, the worker "
            f"appends a `build finished, exit N, M errors total` line to the error list and removes `{directory}/{LOCK_FILE}`.",
            "",
            "Start on the modules that have already failed while the build goes on; they are in the error list, and "
            "the running build will not revisit them. Edit only modules that have already failed: a module the build "
            "has not reached yet would be compiled with a half-made edit. Exactly one `lake build` runs at a time: do "
            f"not start your own `lake build` while `{directory}/{LOCK_FILE}` exists. `{directory}/{repair_tools.BUILD_FILE} "
            "<Module> ...` waits for the lock to be free, builds only the named modules (`--dependents` adds the modules "
            "importing them) and returns Lake's exit status, so a fix is checked in seconds rather than by rerunning the "
            "whole library; the same command with no modules runs the whole library with `--iofail`. The verdict is "
            "still your own whole-library `lake build --iofail` after your fixes, once the lock is free.",
        ]

    def annotate(self, prompt: str) -> str:
        """The prompt with the Build in progress section before the local verification section."""
        section = "\n".join(self.prompt_section())
        marker = "\n## Verifying locally"
        at = prompt.find(marker)
        if at < 0:
            return prompt.rstrip("\n") + "\n" + section + "\n"
        return prompt[:at] + section + "\n" + prompt[at:]

    # -- the tail ---------------------------------------------------------------------------------

    def _tail(self) -> None:
        parser = ErrorParser()
        process = self._process
        assert process is not None
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        fd = os.open(self.log_path, os.O_RDONLY)
        buffer = b""
        try:
            while True:
                chunk = os.read(fd, 1 << 16)
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        self._record(parser.feed(line.decode("utf-8", errors="replace")))
                    continue
                if process.poll() is not None or self._stop.is_set():
                    break
                if deadline is not None and time.monotonic() > deadline:
                    self.timed_out = True
                    process.kill()
                    process.wait()
                    break
                time.sleep(POLL_SECONDS)
            # The process has ended; take whatever it wrote after the last read.
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                buffer += chunk
            if buffer:
                *lines, buffer = buffer.split(b"\n")
                for line in [*lines, buffer]:
                    if line:
                        self._record(parser.feed(line.decode("utf-8", errors="replace")))
            self._record(parser.finish())
        finally:
            os.close(fd)
            if process.poll() is None:
                process.wait()
            self._end(124 if self.timed_out else process.returncode)

    def _record(self, errors: list[dict]) -> None:
        if not errors:
            return
        with self._lock:
            with self.errors_path.open("a") as handle:
                for error in errors:
                    error.update(self._owner(error["file"]))
                    self.errors.append(error)
                    handle.write(json.dumps(error) + "\n")
        self.ready.set()

    def _owner(self, path: str) -> dict:
        """The member whose squash commit last touched ``path``: ``member`` (``owner/repo#N``) and ``commit``."""
        if path in self._owners:
            return self._owners[path]
        record: dict = {"member": None, "commit": None}
        if self.chain:
            try:
                sha = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.worktree),
                        "log",
                        "-1",
                        "--format=%H",
                        f"{self.chain[0].parent}..{self.chain[-1].sha}",
                        "--",
                        path,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout.strip()
            except OSError:
                sha = ""
            for commit in self.chain:
                if sha and commit.sha == sha:
                    record = {
                        "member": f"{self.repository}#{commit.pr}" if commit.pr is not None else None,
                        "commit": sha,
                    }
                    break
        self._owners[path] = record
        return record

    def _end(self, rc: int | None) -> None:
        with self._lock:
            if self.finished.is_set():
                return
            self.rc = rc
            self.seconds = round(time.monotonic() - self.started_at, 1) if self.started_at is not None else None
            count = len(self.errors)
            try:
                with self.errors_path.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "event": "build_finished",
                                "exit": rc,
                                "errors": count,
                                "timed_out": self.timed_out,
                                "message": f"build finished, exit {rc}, {count} error{'s' if count != 1 else ''} total",
                            }
                        )
                        + "\n"
                    )
            except OSError:
                pass
            shutil.rmtree(self.lock_path, ignore_errors=True)
            if self.copy_log_to is not None:
                try:
                    self.copy_log_to.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.log_path, self.copy_log_to)
                except OSError:
                    pass
            self.finished.set()
            self.ready.set()


def start_build(
    worktree: Path,
    directory: Path,
    env: dict,
    *,
    chain: list,
    repository: str,
    lake: tuple[str, ...] = ("lake",),
    timeout: float | None = None,
    copy_log_to: Path | None = None,
) -> StreamingBuild:
    """Start the streaming build; the caller waits with ``wait_for_agent`` and ends it with ``stop``."""
    return StreamingBuild(
        worktree,
        directory,
        env,
        chain=chain,
        repository=repository,
        lake=lake,
        timeout=timeout,
        copy_log_to=copy_log_to,
    ).start()
