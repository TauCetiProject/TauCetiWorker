"""Strict shared Git leases and a compare-and-swap Mathlib cursor.

Unlike ordinary cooperative claims, errors here never authorize work. Credentials
come from the worker's account-aware process environment, not a global git setting.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import Die, log
from .constants import CLAIM_HEARTBEAT_S, CLAIM_TTL_S, CLAIMS
from .overlap_support import locked

CURSOR_REF = "refs/tauceti-state/mathlib-overlap"
SHA = re.compile(r"[0-9a-f]{40}")


def git(*args: str, cwd: Path | None = None, data: str | None = None, check: bool = True, env=None):
    result = subprocess.run(
        ["git", *map(str, args)], cwd=cwd, input=data, capture_output=True, text=True, timeout=180, env=env
    )
    if check and result.returncode:
        raise Die(f"overlap git failed: {(result.stderr or result.stdout).strip()[-600:]}")
    return result


class State:
    """A private object store; remote state always lives in the shared repository."""

    def __init__(self, directory: Path, *, url: str | None = None):
        self.directory = directory
        self.url = url or f"https://github.com/{CLAIMS}"
        directory.parent.mkdir(parents=True, exist_ok=True)
        with locked(directory.with_suffix(".init.lock")):
            if not directory.exists():
                git("init", "--bare", directory)

    def run(self, *args, data=None, check=True, env=None):
        return git("--git-dir", self.directory, *args, data=data, check=check, env=env)

    def read(self, ref: str) -> tuple[str, dict | None]:
        output = self.run("ls-remote", "--refs", self.url, ref).stdout.strip()
        if not output:
            return "", None
        rows = output.splitlines()
        if len(rows) != 1:
            raise Die("overlap: ambiguous remote ref")
        oid, name = rows[0].split()
        if name != ref or not SHA.fullmatch(oid):
            raise Die("overlap: malformed remote ref")
        # Fetch the immutable observed object, not a potentially newer ref.
        if self.run("cat-file", "-e", oid, check=False).returncode:
            self.run("fetch", "--no-tags", self.url, oid)
        try:
            payload = json.loads(self.run("show", "-s", "--format=%B", oid).stdout)
        except (ValueError, TypeError) as error:
            raise Die(f"overlap: malformed state at {ref}") from error
        if not isinstance(payload, dict):
            raise Die(f"overlap: malformed state at {ref}")
        return oid, payload

    def cas(self, ref: str, previous: str, payload: dict | None) -> bool:
        if payload is None:
            new = ""
        else:
            tree = self.run("hash-object", "-t", "tree", "-w", "--stdin", data="").stdout.strip()
            # Tiny checkpoint history: do not transfer Mathlib commits to the claim repo.
            parent = ["-p", previous] if previous else []
            env_names = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
            defaults = ("tauceti-overlap", "overlap@tauceti.invalid") * 2
            commit_env = {
                **os.environ,
                **{key: os.environ.get(key, default) for key, default in zip(env_names, defaults, strict=True)},
            }
            new = self.run("commit-tree", tree, *parent, data=json.dumps(payload) + "\n", env=commit_env).stdout.strip()
        result = self.run("push", f"--force-with-lease={ref}:{previous}", self.url, f"{new}:{ref}", check=False)
        if result.returncode:
            # Reconcile a lost response, without blindly retrying a mutation.
            observed, _ = self.read(ref)
            if observed == new:
                return True
            if observed != previous:
                return False
            raise Die(f"overlap: state push failed: {result.stderr.strip()[-500:]}")
        if ref == CURSOR_REF:
            from .overlap_support import invalidate

            invalidate()
        return True

    def cursor(self) -> tuple[str, str]:
        oid, payload = self.read(CURSOR_REF)
        if payload is None:
            return "", ""
        if payload.get("schema") != "tauceti-mathlib-overlap-cursor/v1" or not SHA.fullmatch(
            str(payload.get("mathlib_commit", ""))
        ):
            raise Die("overlap: malformed cursor; refusing to skip history")
        return oid, payload["mathlib_commit"]

    def advance(self, previous: str, mathlib_commit: str) -> bool:
        if not SHA.fullmatch(mathlib_commit):
            raise Die("overlap: invalid Mathlib commit")
        return self.cas(
            CURSOR_REF, previous, {"schema": "tauceti-mathlib-overlap-cursor/v1", "mathlib_commit": mathlib_commit}
        )


@contextlib.contextmanager
def environment(values: dict):
    before = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Lease:
    def __init__(self, state: State, key: str, account: str, worker: str, *, ttl: int = CLAIM_TTL_S):
        self.state, self.key, self.ttl = state, key, ttl
        self.ref = "refs/tauceti-claims/" + key
        self.owner = f"{account}/{worker}/{uuid.uuid4().hex}"
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.mutex = threading.Lock()
        self.thread = None
        self.acquired = False

    def _payload(self):
        return {
            "schema": "tauceti-claim/v1",
            "owner": self.owner,
            "resource": self.key,
            "expires_at": int(time.time()) + self.ttl,
        }

    def _read(self):
        oid, payload = self.state.read(self.ref)
        if payload is not None and (
            payload.get("schema") != "tauceti-claim/v1"
            or not isinstance(payload.get("owner"), str)
            or type(payload.get("expires_at")) is not int
            or payload.get("resource") != self.key
        ):
            raise Die("overlap: malformed shared lease")
        return oid, payload

    def acquire(self) -> bool:
        oid, payload = self._read()
        if payload and payload["expires_at"] > time.time():
            return False
        if not self.state.cas(self.ref, oid, self._payload()):
            return False
        self.acquired = True
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return True

    def require(self):
        with self.mutex:
            if self.lost.is_set():
                raise Die("overlap: shared lease lost; not publishing")
            _, payload = self._read()
            if not payload or payload["owner"] != self.owner or payload["expires_at"] <= time.time():
                self.lost.set()
                raise Die("overlap: shared lease lost; not publishing")

    def _heartbeat(self):
        while not self.stop.wait(min(CLAIM_HEARTBEAT_S, max(1, self.ttl // 3))):
            try:
                with self.mutex:
                    oid, payload = self._read()
                    if not payload or payload["owner"] != self.owner or payload["expires_at"] <= time.time():
                        self.lost.set()
                        return
                    if not self.state.cas(self.ref, oid, self._payload()):
                        self.lost.set()
                        return
            except Exception:
                self.lost.set()
                return

    def release(self):
        if not self.acquired:
            return
        self.acquired = False
        self.stop.set()
        if self.thread:
            self.thread.join(185)
        try:
            with self.mutex:
                oid, payload = self._read()
                if payload and payload["owner"] == self.owner:
                    self.state.cas(self.ref, oid, None)
        except Exception:
            # Cleanup cannot turn a completed cursor/PR publication into failure.
            # A disconnected lease still expires under its existing TTL.
            log(f"overlap: could not release shared lease {self.key}; retained until expiry")

    def __enter__(self):
        if not self.acquire():
            return None
        return self

    def __exit__(self, *_):
        self.release()
