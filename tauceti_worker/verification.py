"""Synchronous, concurrent local verification with exact-input receipts.

Receipts are evidence of commands run against particular bytes, never a proof of correctness.
A receipt is durable and incomplete before any child starts; SIGKILL cannot turn it into success.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import hashlib
import json
import math
import os
import platform
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from .config import Die
from .constants import LEAN_ROOT
from .paths import HERE
from .processes import owned_process
from .runtime_status import read_json, report_runtime
from .verification_installations import Installations
from .verification_scope import git, local_main, scope, untracked

SCHEMA = "tauceti-verification/v1"
POLICY_VERSION = 4
_HASH_CACHE = contextvars.ContextVar("verification_hash_cache", default=None)
LEAN_DIR = LEAN_ROOT.replace(".", "/")
STAGES = ("shim-expiry", "build", "axioms", "module-system", "lint")


class Incomplete(Exception):
    def __init__(self, message, *, reason="invalid-input"):
        super().__init__(message)
        self.reason = reason


class _HashCache:
    """Keep mutable input hashes; import opaque V1 entries lazily without payload rereads."""

    def __init__(self, state):
        self.path = state / "verification-hashes-v2.json"
        self.original = read_json(self.path)
        entries = self.original.get("entries", {})
        self.entries = dict(entries) if isinstance(entries, dict) else {}
        self.imported = (
            self.original.get("schema") == "tauceti-verification-hash-cache/v2"
            and self.original.get("legacy_imported") is True
        )
        self.legacy_path = state / "verification-hashes.json"
        self.legacy = {} if self.imported else None

    def get(self, key, path):
        entry = self.entries.get(key)
        if isinstance(entry, dict) and entry.get("path") == path:
            value = entry.get("sha256")
            if isinstance(value, str) and len(value) == 64:
                return value
        if self.legacy is None:
            self.legacy = read_json(self.legacy_path)
        value = self.legacy.get(key)
        if isinstance(value, str) and len(value) == 64:
            self.put(key, path, value)
            return value
        return None

    def put(self, key, path, value):
        self.entries[key] = dict(path=path, sha256=value)

    def finish_import(self):
        # Once complete preparation has visited the mutable inputs, future invocations
        # need no historical opaque cache, including when a candidate source changes.
        self.imported = True
        if self.legacy is None:
            self.legacy = {}

    def save(self, immutable_roots):
        # A previously unregistered read-only tree can become trusted later. Remove its old
        # mutable-cache records once; warm cache JSON must not scale with sealed inventory.
        entries = {
            key: value
            for key, value in self.entries.items()
            if isinstance(value, dict)
            and isinstance(value.get("path"), str)
            and not any(Path(value["path"]).is_relative_to(root) for root in immutable_roots)
        }
        entries = dict(list(entries.items())[-200000:])
        payload = dict(schema="tauceti-verification-hash-cache/v2", entries=entries, legacy_imported=self.imported)
        if payload != self.original:
            atomic(self.path, payload)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def atomic(path: Path, value: dict) -> None:
    # fsync both the file and its directory: a power loss must not publish partial success.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".verification-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, sort_keys=True, ensure_ascii=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def machine_key() -> str:
    # Deliberately ignore fleet IDs, worker IDs, checkout paths and HOME.
    for name in ("/proc/sys/kernel/random/boot_id", "/etc/machine-id"):
        try:
            return digest(Path(name).read_text().strip())[:24]
        except OSError:
            pass
    return digest(platform.node())[:24]


def cache_root() -> Path:
    # Share immutable installation identities, never admission to verification work.
    root = Path(
        os.environ.get("TAUCETI_VERIFY_CACHE_DIR") or f"/tmp/tauceti-verification-{os.getuid()}-{machine_key()}"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(f"unsafe verification cache directory: {root}")
    return root


def files_under(root: Path):
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".lake", "__pycache__"})
        for name in sorted(files + [d for d in dirs if (Path(directory) / d).is_symlink()]):
            yield Path(directory) / name


def file_record(path: Path) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(info.st_mode)
    if path.is_symlink():
        # Follow symlinked inputs too: target changes must invalidate evidence.
        target = os.readlink(path)
        if path.is_file():
            return dict(kind="symlink", mode=mode, target=target, sha256=hash_file(path))
        if path.is_dir():
            return dict(
                kind="symlink-directory",
                mode=mode,
                target=target,
                contents={str(p.relative_to(path)): file_record(p) for p in files_under(path)},
            )
        raise ValueError(f"unreadable source symlink {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"unsupported verification input {path}")
    return dict(kind="file", mode=mode, sha256=hash_file(path))


def hash_file(path: Path) -> str:
    before = path.stat()
    identity = (
        str(path.absolute()),
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    key = digest(identity)
    cache = _HASH_CACHE.get()
    cached = cache.get(key, identity[0]) if isinstance(cache, _HashCache) else cache.get(key) if cache else None
    if isinstance(cached, str) and len(cached) == 64:
        return cached
    with path.open("rb") as source:
        result = hashlib.file_digest(source, "sha256").hexdigest()
    after = path.stat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity[1:]:
        raise Incomplete(f"input changed while fingerprinting: {path}")
    if isinstance(cache, _HashCache):
        cache.put(key, identity[0], result)
    elif cache is not None:
        cache[key] = result
    return result


def source_names(root: Path, base: str) -> set[str]:
    raw = git(root, "ls-files", "--cached", "-z") + git(root, "ls-tree", "-r", "--name-only", "-z", base)
    return {p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p} | untracked(root)


def inventory(root: Path, base: str) -> dict:
    return {name: file_record(root / name) for name in sorted(source_names(root, base))}


def stamps(paths) -> dict:
    result = {}
    for path in paths:
        try:
            s = Path(path).stat()
            link = Path(path).lstat()
            result[str(path)] = (
                s.st_dev,
                s.st_ino,
                s.st_size,
                s.st_mtime_ns,
                s.st_ctime_ns,
                s.st_mode,
                link.st_ino,
                link.st_ctime_ns,
            )
        except FileNotFoundError:
            result[str(path)] = None
    return result


class Verification:
    def __init__(
        self,
        root: Path,
        state: Path,
        *,
        base=None,
        stage="all",
        reuse=True,
        policy="ordinary",
        deadline=None,
        lint_modules=None,
        job=None,
    ):
        self.root, self.state = root.resolve(), state.resolve()
        self.base_arg, self.policy = base, policy
        self.lint_modules = lint_modules
        self.requested = list(STAGES) if stage == "all" else [stage]
        self.reuse, self.deadline = reuse, deadline
        self.id = uuid.uuid4().hex
        self.directory = self.state / "verification" / self.id
        self.path = self.directory / "receipt.json"
        self.receipt = dict(
            schema=SCHEMA,
            id=self.id,
            status="incomplete",
            started_at=time.time(),
            ended_at=None,
            root=str(self.root),
            requested_phases=self.requested,
            phases={},
            commands=[],
            receipt=str(self.path),
        )
        self.base = ""
        self.watched = {}
        self.static_stamps = {}
        self.static_names = {}
        self.at_boundary = False
        self.watch_names = set()
        self.dependency_names = None
        self.mutated = False
        self.last_watch = 0.0
        self.live_lease = None
        self.job = job
        self.installations = Installations(
            self.state,
            digest=digest,
            atomic=atomic,
            read_json=read_json,
            files_under=files_under,
            file_record=file_record,
            stamps=stamps,
            cache_directory=lambda: cache_root() / "installations",
        )

    def save(self):
        atomic(self.path, self.receipt)

    def progress(self, message):
        print(message, file=sys.stderr, flush=True)

    def check_deadline(self):
        if self.at_boundary:
            return  # Finish validation/index publication for an already completed phase.
        margin = float(os.environ.get("TAUCETI_VERIFY_CHECKPOINT_MARGIN", "600"))
        if not math.isfinite(margin) or margin < 0:
            raise ValueError("TAUCETI_VERIFY_CHECKPOINT_MARGIN must be a nonnegative number of seconds")
        if self.deadline is not None and time.time() >= self.deadline - margin:
            raise Incomplete("round deadline checkpoint margin reached; checkpoint now", reason="deadline")

    def watch(self):
        self.check_deadline()
        if self.watched and time.monotonic() - self.last_watch >= 2:
            self.last_watch = time.monotonic()
            if stamps(self.watched) != self.watched or source_names(self.root, self.base) != self.watch_names:
                self.mutated = True
                raise Incomplete("verification inputs mutated during execution")

    def final_watch(self):
        self.last_watch = 0
        self.watch()
        self.installations.validate()
        if stamps(self.static_stamps) != self.static_stamps or any(
            {str(p) for p in files_under(root)} != names for root, names in self.static_names.items()
        ):
            raise Incomplete("dependency, toolchain or verifier mutated during verification")

    def command(self, phase, argv, *, env=None, capture=False):
        self.watch()
        report_runtime("verifying", verification_phase=phase, verification_receipt=str(self.path))
        log = self.directory / f"{len(self.receipt['commands']):03d}-{phase}.log"
        entry = dict(phase=phase, argv=argv, started_at=time.time(), ended_at=None, exit=None, log=str(log))
        self.receipt["commands"].append(entry)
        self.save()
        self.progress(f"{phase}: start {json.dumps(argv)} (log {log})")
        process = None
        command_stack = contextlib.ExitStack()
        stdout_path = log.with_suffix(".stdout")
        offset = 0
        try:
            with log.open("wb") as output, stdout_path.open("wb") as captured:
                process = command_stack.enter_context(
                    owned_process(
                        argv,
                        cwd=self.root,
                        env=env,
                        stdout=captured if capture else output,
                        stderr=subprocess.STDOUT,
                    )
                )
                while process.poll() is None:
                    if not capture:
                        with log.open("rb") as progress:
                            progress.seek(offset)
                            data = progress.read()
                            offset += len(data)
                            if data:
                                print(data.decode(errors="replace"), file=sys.stderr, end="", flush=True)
                    self.watch()
                    if capture and time.time() - entry["started_at"] > 300:
                        raise Incomplete(f"{phase}: toolchain probe timed out")
                    time.sleep(0.05)
                entry["exit"] = process.returncode
        finally:
            try:
                command_stack.close()
                if process is not None:
                    entry["cleanup"] = "process-group-best-effort"
                    entry["exit"] = process.returncode
            finally:
                entry["ended_at"] = time.time()
                self.save()
        text = log.read_text(errors="replace")
        captured_text = stdout_path.read_text(errors="replace")
        if capture:
            entry["stdout_log"] = str(stdout_path)
        # Keep the original audit progress/timing lines verbatim in the receipt and full log.
        entry["audit_timings"] = [
            line
            for line in text.splitlines()
            if any(word in line.lower() for word in ("import", "duplicate", "axiom"))
            and any(word in line.lower() for word in ("ms", "seconds", "timing", "⏱"))
        ]
        if not capture:
            with log.open("rb") as progress:
                progress.seek(offset)
                tail = progress.read().decode(errors="replace")
                if tail:
                    print(tail, file=sys.stderr, end="", flush=True)
        self.progress(f"{phase}: exit {entry['exit']}")
        if entry["exit"]:
            raise subprocess.CalledProcessError(entry["exit"], argv, output=text)
        return (captured_text if capture else text).strip()

    def environment(self):
        return {
            k: digest(v)
            for k, v in os.environ.items()
            if k.startswith(("LEAN", "LAKE", "ELAN", "MATHLIB", "NIX_"))
            or k
            in {
                "PATH",
                "HOME",
                "CC",
                "CXX",
                "AR",
                "LD",
                "CFLAGS",
                "CXXFLAGS",
                "LDFLAGS",
                "LD_LIBRARY_PATH",
                "PYTHONPATH",
                "PYTHONSAFEPATH",
            }
        }

    def validate_inputs(self, inputs, *, full=False):
        self.at_boundary = True
        try:
            self.final_watch()
            if full:
                unchanged = digest(self.inputs()) == digest(inputs)
            else:
                config = os.environ.get("LAKE_CONFIG")
                unchanged = (
                    inventory(self.root, self.base) == inputs["sources"]
                    and scope(self.root, self.base, self.policy) == inputs["scope"]
                    and self.environment() == inputs["environment"]
                    and (file_record(Path(config)) if config else None) == inputs["configuration"]
                    and (file_record(Path(self.lint_modules)) if self.lint_modules else None) == inputs["lint_modules"]
                    and POLICY_VERSION == inputs["policy_version"]
                    and {p.name for p in Path(__file__).parent.glob("*.py")}
                    == set(inputs["verifier"]) - {"wrapper", "tauceti-local-axioms", "LocalAxioms.lean"}
                )
            if self.mutated or not unchanged:
                raise Incomplete("verification inputs changed during execution")
            self.final_watch()
        finally:
            self.at_boundary = False

    def inputs(self):
        self.check_deadline()
        toolchain = {}
        for name in ("lake", "lean", "bash", "python3", "git", "tar"):
            executable = shutil.which(name)
            if executable is None:
                raise Incomplete(f"verification tool missing: {name}", reason="tool-missing")
            toolchain[name] = dict(path=executable, file=file_record(Path(executable)))
        for name in ("cc", "c++", "clang", "ar", "ld"):
            selected = shlex.split(os.environ.get(name.upper(), name))
            if selected and (executable := shutil.which(selected[0])):
                toolchain[name] = dict(path=executable, file=file_record(Path(executable)))
        for command in (
            ["lake", "--version"],
            ["lake", "env", "lean", "--version"],
            ["lake", "env", "lean", "--print-prefix"],
        ):
            toolchain[" ".join(command)] = self.command("toolchain", command, capture=True)
        prefix = Path(toolchain["lake env lean --print-prefix"])
        if not prefix.is_absolute() or not prefix.is_dir():
            if prefix.is_absolute() and self.job:
                self.job["toolchain_wait_path"] = str(prefix)
            raise Incomplete("Lean did not report an absolute installed toolchain prefix", reason="tool-missing")
        toolchain["distribution"] = self.installations.inventory(prefix)
        deps = self.root / ".lake" / "packages"
        dependencies = self.installations.inventory(deps)
        config = os.environ.get("LAKE_CONFIG")
        # Hash environment values to avoid recording credentials. Volatile orchestration values do not build inputs.
        environment = self.environment()
        verifier = {str(p.name): hash_file(p) for p in Path(__file__).parent.glob("*.py")}
        verifier["wrapper"] = hash_file(HERE / "scripts" / "tauceti-verify")
        for name in ("tauceti-local-axioms", "LocalAxioms.lean"):
            verifier[name] = hash_file(HERE / "scripts" / name)
        sources = inventory(self.root, self.base)
        return dict(
            sources=sources,
            root=str(self.root),
            dependencies=dependencies,
            base=self.base,
            scope=scope(self.root, self.base, self.policy),
            configuration=file_record(Path(config)) if config else None,
            environment=environment,
            toolchain=toolchain,
            verifier=verifier,
            policy_version=POLICY_VERSION,
            python=dict(version=sys.version, executable=file_record(Path(sys.executable))),
            lint_modules=file_record(Path(self.lint_modules)) if self.lint_modules else None,
        )

    def phase_commands(self, phase, modules):
        env = dict(os.environ)
        # Never let an inherited filter silently narrow the recorded scope.
        env.pop("AXIOMS_ONLY_MODULES", None)
        env.pop("LINT_ONLY_MODULES", None)
        selected = self.receipt["scope"]
        if phase == "build":
            return [(["lake", "build", "--iofail"], env)]
        if phase == "axioms":
            if selected["mode"] == "unknown":
                raise Incomplete("axioms: changed modules unknown; refusing a whole-library local fallback")
            # This is local feedback, not CI certification. Never import the full library,
            # including for pin bumps/deletions/broad policies (CI owns their global checks).
            return [
                (
                    [
                        sys.executable,
                        str(HERE / "scripts/tauceti-local-axioms"),
                        "--modules",
                        str(modules),
                        "--root",
                        LEAN_ROOT,
                    ],
                    env,
                )
            ]
        if phase == "module-system":
            return [(["lake", "exe", "module-system"], env)]
        if phase == "lint":
            if selected["mode"] == "unknown":
                raise Incomplete("lint: changed modules unknown; CI owns the full environment lint")
            commands = [(["bash", "-o", "pipefail", "scripts/lint-style.sh"], env)]
            lint_list = self.directory / "lint-modules.txt"
            lint_modules = set(selected["modules"])
            if self.lint_modules:
                for module in Path(self.lint_modules).read_text().splitlines():
                    module = module.strip()
                    if not module:
                        continue
                    path = module.replace(".", "/") + ".lean"
                    from .verification_scope import MODULE

                    if not MODULE.fullmatch(path) or not (self.root / path).is_file():
                        raise Incomplete(f"invalid explicit lint module: {module}")
                    lint_modules.add(module)
            lint_list.write_text("".join(f"{m}\n" for m in sorted(lint_modules)))
            if lint_modules:
                env["LINT_ONLY_MODULES"] = str(lint_list)
                commands.append((["bash", "-o", "pipefail", "scripts/lint-env.sh"], env))
            return commands
        if phase == "shim-expiry":
            script = self.root / "scripts/check-expired-mathlib-shims.py"
            if not script.exists():
                checker_in_base = git(
                    self.root, "ls-tree", "--name-only", self.base, "--", "scripts/check-expired-mathlib-shims.py"
                )
                if (self.root / LEAN_DIR / "mathlib-shims.json").exists() or checker_in_base:
                    raise Incomplete("shim-expiry: required checker is missing")
                return []  # Supported older TauCeti revisions have no shim registry/checker.
            merge = selected["merge_base"]
            if not merge:
                raise Incomplete("shim-expiry: cannot determine merge-base registry")
            args = ["python3", str(script), "--fail-on-available"]
            baseline = self.directory / "base"
            baseline.mkdir(exist_ok=True)
            try:
                manifest = git(self.root, "show", f"{merge}:{LEAN_DIR}/mathlib-shims.json")
            except subprocess.CalledProcessError:
                manifest = None
            if manifest is not None:
                (baseline / "mathlib-shims.json").write_bytes(manifest)
                # No shell pipeline: both git and tar exits are checked independently.
                archive = self.directory / "base.tar"
                archive.write_bytes(git(self.root, "archive", merge, LEAN_DIR))
                self.command("shim-baseline", ["tar", "-xf", str(archive), "-C", str(baseline)])
                args += ["--base-manifest", str(baseline / "mathlib-shims.json"), "--base-root", str(baseline)]
                pins = git(
                    self.root,
                    "diff",
                    "--no-ext-diff",
                    "--name-only",
                    merge,
                    "--",
                    "lake-manifest.json",
                    "lean-toolchain",
                )
                if not pins and self.policy != "bump":
                    args += ["--only-new"]
            return [(args, env)]
        raise ValueError(f"unknown verification phase {phase}")

    def run(self):
        self.save()
        previous = {}
        self.live_lease = (self.directory / "live.lock").open("a+")
        fcntl.flock(self.live_lease, fcntl.LOCK_EX)
        cache = _HashCache(self.state)
        cache_token = _HASH_CACHE.set(cache)
        try:

            def interrupted(signum, _):
                raise Incomplete(f"verification interrupted by signal {signum}", reason="signal")

            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, interrupted)
            raw = self.base_arg or os.environ.get("TAUCETI_VERIFY_BASE") or local_main(self.root)
            self.base = git(self.root, "rev-parse", "--verify", f"{raw}^{{commit}}").decode().strip()
            self.receipt["scope"] = scope(self.root, self.base, self.policy)
            self.progress(f"scope: {self.receipt['scope']['mode']}: {self.receipt['scope']['reason']}")
            candidate_sources = inventory(self.root, self.base)
            if os.environ.get("TAUCETI_VERIFY_BUBBLE") == "1":
                self.checkpoint_sources(dict(sources=candidate_sources))
            from .verification_jobs import prepare_job

            self.job = prepare_job(self, dict(sources=candidate_sources))
            self.receipt["job"] = self.job["id"]
            if self.deadline is not None and time.time() >= self.deadline - float(
                os.environ.get("TAUCETI_VERIFY_CHECKPOINT_MARGIN", "600")
            ):
                from .verification_jobs import preserve_candidate

                preserve_candidate(self)
            self.check_deadline()
            self.watch_names = source_names(self.root, self.base)
            self.watched = stamps([self.root / p for p in self.watch_names])
            inputs = self.inputs()
            cache.finish_import()
            self.watch()
            extra = list(Path(__file__).parent.glob("*.py")) + [
                HERE / "scripts" / name for name in ("tauceti-verify", "tauceti-local-axioms", "LocalAxioms.lean")
            ]
            extra += [p for root in self.installations.walked for p in files_under(root)]
            extra += [
                Path(entry["path"])
                for entry in inputs["toolchain"].values()
                if isinstance(entry, dict) and "path" in entry
            ]
            extra.append(Path(sys.executable))
            self.static_names = {root: {str(p) for p in files_under(root)} for root in self.installations.walked}
            if os.environ.get("LAKE_CONFIG"):
                extra.append(Path(os.environ["LAKE_CONFIG"]))
            if self.lint_modules:
                extra.append(Path(self.lint_modules))
            self.static_stamps.update(stamps(extra))
            fingerprint = digest(inputs)
            input_file = self.state / "verification-inputs" / f"{fingerprint}.json"
            if not input_file.exists():
                atomic(input_file, inputs)
            self.receipt.update(inputs=dict(path=str(input_file), sha256=fingerprint), fingerprint=fingerprint)
            if os.environ.get("TAUCETI_VERIFY_BUBBLE") == "1":
                self.checkpoint_sources(inputs)

            if digest(inputs["sources"]) != self.job["source_digest"]:
                raise Incomplete("candidate changed while preparing verification")
            modules = self.directory / "modules.txt"
            modules.write_text("".join(f"{m}\n" for m in self.receipt["scope"]["modules"]))
            self.save()
            early_style = (
                len(self.requested) > 1
                and "lint" in self.requested
                and not (self.reuse and self.find_reuse(fingerprint, "lint"))
            )
            if early_style:
                from .verification_jobs import preserve_candidate

                preserve_candidate(self, disposable_only=True)
                self.receipt["phases"]["lint"] = dict(status="incomplete", started_at=time.time(), ended_at=None)
                self.command("lint", ["bash", "-o", "pipefail", "scripts/lint-style.sh"], env=dict(os.environ))
            for phase in self.requested:
                self.watch()
                reused = self.find_reuse(fingerprint, phase) if self.reuse else None
                if reused:
                    self.receipt["phases"][phase] = dict(
                        status="success", reused_from=str(reused), started_at=time.time(), ended_at=time.time()
                    )
                    self.progress(f"{phase}: reused exact-input success ({reused})")
                    self.save()
                    continue
                from .verification_jobs import preserve_candidate

                preserve_candidate(self, disposable_only=True)
                record = self.receipt["phases"].get(phase) or dict(
                    status="incomplete", started_at=time.time(), ended_at=None
                )
                self.receipt["phases"][phase] = record
                self.save()
                for argv, env in self.phase_commands(phase, modules):
                    if early_style and phase == "lint" and "scripts/lint-style.sh" in argv:
                        continue
                    try:
                        self.command(phase, argv, env=env)
                    except subprocess.CalledProcessError as error:
                        if phase == "axioms" and error.returncode in {2, 75}:
                            raise Incomplete(
                                "local axiom checker unavailable; see the phase log",
                                reason="tool-missing" if error.returncode == 75 else "invalid-input",
                            ) from error
                        raise
                self.validate_inputs(inputs)
                record.update(status="success", ended_at=time.time(), validated_fingerprint=fingerprint)
                self.index_success()
            # Recompute bytes, pins, dependencies, toolchain, environment and verifier after all tools exit.
            self.validate_inputs(inputs, full=True)
            self.receipt.update(status="success", ended_at=time.time())
            self.index_success()
        except subprocess.CalledProcessError as error:
            probing = bool(self.receipt["commands"] and self.receipt["commands"][-1]["phase"] == "toolchain")
            self.receipt.update(
                status="incomplete" if probing else "failed",
                incomplete_reason="toolchain" if probing else None,
                error=f"command exited {error.returncode}: {error.cmd}",
            )
        except Incomplete as error:
            self.receipt.update(status="incomplete", error=str(error), incomplete_reason=error.reason)
        except (Die, OSError, ValueError, RuntimeError) as error:
            self.receipt.update(status="incomplete", error=str(error), incomplete_reason="preparation")
        finally:
            self.receipt["ended_at"] = time.time()
            # Retain the legacy field for readers; local checks no longer import an environment.
            self.receipt["audit_phase_timings"] = []
            for phase in self.receipt["phases"].values():
                if phase.get("ended_at") is None:
                    phase.update(ended_at=self.receipt["ended_at"], status=self.receipt["status"])
            if self.job:
                from .verification_jobs import finish_job

                finish_job(self)
            self.save()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.live_lease.close()
            _HASH_CACHE.reset(cache_token)
            # Bound the metadata cache; immutable input blobs and receipts retain durable evidence.
            cache.save(self.installations.validated)
            shutil.rmtree(self.directory / "base", ignore_errors=True)
            (self.directory / "base.tar").unlink(missing_ok=True)
            if self.job and self.receipt["status"] == "incomplete":
                report_runtime(
                    "verification-pending",
                    verification_phase=None,
                    verification_job=self.job["id"],
                    detail="durable verification job retained for mechanical continuation",
                )
            else:
                report_runtime("working", verification_phase=None)
        return self.receipt

    def index_success(self):
        index = self.state / "verification-index" / f"{self.receipt['fingerprint']}.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = read_json(index)
            for phase, result in self.receipt["phases"].items():
                if "reused_from" not in result and result.get("validated_fingerprint") == self.receipt["fingerprint"]:
                    entries[phase] = str(self.path)
            # Publish the receipt before its index; interrupted publication never grants evidence.
            self.save()
            atomic(index, entries)

    def find_reuse(self, fingerprint, phase):
        # These cheap checks inspect build outputs. A source-only fingerprint cannot certify
        # that a stage-only check saw artifacts produced by a subsequent build.
        if phase in {"axioms", "module-system"}:
            return None
        entry = read_json(self.state / "verification-index" / f"{fingerprint}.json").get(phase)
        if not isinstance(entry, str):
            return None
        path = Path(entry)
        old = read_json(path)
        phases = old.get("phases")
        inputs = old.get("inputs")
        if not isinstance(phases, dict) or not isinstance(inputs, dict) or not isinstance(inputs.get("path"), str):
            return None
        evidence = phases.get(phase)
        if not isinstance(evidence, dict):
            return None
        if (
            old.get("schema") == SCHEMA
            and (old.get("status") == "success" or evidence.get("validated_fingerprint") == fingerprint)
            and old.get("fingerprint") == fingerprint
            and evidence.get("status") == "success"
            and Path(old.get("inputs", {}).get("path", "")).is_file()
        ):
            return path
        return None

    def checkpoint_sources(self, inputs):
        """Preserve pre-verification Bubble edits before its disposable container can disappear."""
        key = digest(dict(base=self.base, sources=inputs["sources"]))
        target = self.state / "verification-checkpoints" / key
        if not (target / "checkpoint.json").exists():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            patch = git(self.root, "diff", "--binary", "--no-ext-diff", self.base, "--")
            with (target / "source.patch").open("wb") as out:
                out.write(patch)
                out.flush()
                os.fsync(out.fileno())
            with tarfile.open(target / "untracked.tar", "w") as archive:
                for name in sorted(untracked(self.root)):
                    archive.add(self.root / name, arcname=name, recursive=False)
            with (target / "untracked.tar").open("rb") as source:
                os.fsync(source.fileno())
            atomic(
                target / "checkpoint.json",
                dict(
                    base=self.base,
                    root=str(self.root),
                    patch="source.patch",
                    untracked="untracked.tar",
                    created_at=time.time(),
                ),
            )
        self.receipt["source_checkpoint"] = str(target / "checkpoint.json")
        self.save()


def add_parser(sub):
    parser = sub.add_parser("verify", help="synchronously verify exact local inputs; never query GitHub")
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument(
        "--lint-modules", type=Path, help="additional existing TauCeti modules to lint; list outside the checkout"
    )
    parser.add_argument("--base", help="resolved canonical-main commit; defaults to the round's pinned SHA")
    parser.add_argument(
        "--json", action="store_true", help="emit exactly one JSON result on stdout; progress on stderr"
    )
    parser.add_argument("--no-reuse", action="store_true", help="run requested phases even when exact evidence exists")
    parser.add_argument("--resume-job", type=Path, help=argparse.SUPPRESS)


def command(args):
    state = Path(
        os.environ.get("TAUCETI_VERIFY_STATE") or HERE / "state" / os.environ.get("TAUCETI_WORKER_ID", "local")
    )
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip()).resolve()
        deadline = float(os.environ["TAUCETI_ROUND_DEADLINE"]) if os.environ.get("TAUCETI_ROUND_DEADLINE") else None
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("round deadline must be finite")
        job = None
        if getattr(args, "resume_job", None):
            job_path = args.resume_job.resolve()
            job = read_json(job_path)
            if (
                job.get("schema") != "tauceti-verification-job/v1"
                or job_path != state.resolve() / "verification-pending" / f"{job.get('id')}.json"
                or not isinstance(job.get("root"), str)
                or not Path(job["root"]).is_absolute()
                or Path(job["root"]).resolve() != root
            ):
                raise ValueError("invalid mechanical verification job")
            descriptor = int(os.environ.get("TAUCETI_VERIFY_JOB_FD", "-1"))
            owned = os.fstat(descriptor)
            lease = (state / "verification-jobs" / f"{job['id']}.lock").stat()
            if (owned.st_dev, owned.st_ino) != (lease.st_dev, lease.st_ino):
                raise ValueError("mechanical helper requires its supervisor's job lease")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A runtime's state symlink and Git's physical cwd name the same candidate.
            # Persist the actual execution path; different workspaces remain distinct inputs.
            job["root"] = str(root)
        verification = Verification(
            root,
            state,
            base=args.base,
            stage=args.stage,
            reuse=not args.no_reuse,
            policy=os.environ.get("TAUCETI_VERIFY_POLICY", "ordinary"),
            deadline=deadline,
            lint_modules=getattr(args, "lint_modules", None),
            job=job,
        )
        result = verification.run()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        result = dict(schema=SCHEMA, status="incomplete", error=str(error))
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        print(f"verification: {result['status']} ({result.get('receipt', 'no receipt')})")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return 0 if result["status"] == "success" else 1
