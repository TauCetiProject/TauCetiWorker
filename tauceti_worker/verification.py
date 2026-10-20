"""Synchronous, exact-input local verification and a rootless host heavy-work queue.

Receipts are evidence of commands run against particular bytes, never a proof of correctness.
A receipt is durable and incomplete before any child starts; SIGKILL cannot turn it into success.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .config import Die
from .containment import Boundary
from .paths import HERE
from .runtime_status import read_json, report_runtime
from .verification_scope import git, local_main, scope, untracked

SCHEMA = "tauceti-verification/v1"
POLICY_VERSION = 1
STAGES = ("shim-expiry", "build", "axioms", "module-system", "lint")


class Incomplete(Exception):
    pass


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


def queue_root() -> Path:
    # Bubble explicitly mounts this same host directory; a private container /tmp is not shared.
    root = Path(
        os.environ.get("TAUCETI_VERIFY_HOST_QUEUE") or f"/tmp/tauceti-verification-{os.getuid()}-{machine_key()}"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(f"unsafe verification queue directory: {root}")
    return root


def heavy_slots() -> int:
    raw = os.environ.get("TAUCETI_VERIFY_HEAVY_SLOTS", "1")
    if not raw.isascii() or not raw.isdigit() or int(raw) < 1:
        raise ValueError("TAUCETI_VERIFY_HEAVY_SLOTS must be a positive integer")
    return int(raw)


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
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


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
        self, root: Path, state: Path, *, base=None, stage="all", reuse=True, policy="ordinary", deadline=None
    ):
        self.root, self.state = root.resolve(), state.resolve()
        self.base_arg, self.policy = base, policy
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
            queue_wait_seconds=0.0,
        )
        self.base = ""
        self.watched = {}
        self.watch_names = set()
        self.dependency_names = None
        self.mutated = False
        self.last_watch = 0.0

    def save(self):
        atomic(self.path, self.receipt)

    def progress(self, message):
        print(message, file=sys.stderr, flush=True)

    def check_deadline(self):
        if self.deadline is not None and time.time() >= self.deadline:
            raise Incomplete("round deadline interrupted verification")

    def watch(self):
        self.check_deadline()
        if self.watched and time.monotonic() - self.last_watch >= 0.25:
            self.last_watch = time.monotonic()
            if (
                stamps(self.watched) != self.watched
                or source_names(self.root, self.base) != self.watch_names
                or self.dependency_names is not None
                and {str(p) for p in files_under(self.root / ".lake/packages")} != self.dependency_names
            ):
                self.mutated = True
                raise Incomplete("verification inputs mutated during execution")

    @contextlib.contextmanager
    def slot(self, phase):
        count = heavy_slots()
        root = queue_root()
        started = time.monotonic()
        waiting = False
        handle = None
        try:
            while handle is None:
                self.watch()
                for index in range(count):
                    fd = os.open(root / f"slot-{index}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        handle = fd
                        break
                    except BlockingIOError:
                        os.close(fd)
                    except BaseException:
                        os.close(fd)
                        raise
                if handle is None:
                    if not waiting:
                        self.progress(f"{phase}: waiting-verification-capacity")
                        self.capacity(True)
                        self.receipt["waiting_for_capacity"] = True
                        self.save()
                        waiting = True
                    # Keep the supervisor's existing candidate/claim heartbeat running; no new GitHub reads.
                    report_runtime("waiting-verification-capacity", verification_receipt=str(self.path))
                    time.sleep(0.1)
            self.receipt["queue_wait_seconds"] += time.monotonic() - started
            self.capacity(False)
            self.receipt["waiting_for_capacity"] = False
            self.save()
            report_runtime("verifying", verification_phase=phase, verification_receipt=str(self.path))
            yield
        finally:
            if handle is not None:
                os.close(handle)  # close-on-exec: neither tools nor orphaned children keep the slot

    def capacity(self, waiting):
        atomic(
            self.state / "verification-capacity.json",
            dict(
                attempt=os.environ.get("TAUCETI_VERIFY_ATTEMPT"),
                waiting=waiting,
                receipt=str(self.path),
                at=time.time(),
            ),
        )

    def command(self, phase, argv, *, env=None, capture=False):
        self.watch()
        log = self.directory / f"{len(self.receipt['commands']):03d}-{phase}.log"
        entry = dict(phase=phase, argv=argv, started_at=time.time(), ended_at=None, exit=None, log=str(log))
        self.receipt["commands"].append(entry)
        self.save()
        self.progress(f"{phase}: start {json.dumps(argv)} (log {log})")
        boundary = Boundary(f"verify-{phase}")
        entry["containment"] = dict(mode=boundary.mode, strong=boundary.strong, reason=boundary.reason)
        process = None
        offset = 0
        try:
            with log.open("wb") as output:
                process = subprocess.Popen(
                    boundary.argv(argv),
                    cwd=self.root,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=boundary.pass_fds,
                )
                boundary.launched()
                while process.poll() is None:
                    if not capture:
                        with log.open("rb") as progress:
                            progress.seek(offset)
                            data = progress.read()
                            offset += len(data)
                            if data:
                                print(data.decode(errors="replace"), file=sys.stderr, end="", flush=True)
                    self.watch()
                    if capture and time.time() - entry["started_at"] > 30:
                        raise Incomplete(f"{phase}: toolchain probe timed out")
                    time.sleep(0.05)
                entry["exit"] = process.returncode
        finally:
            try:
                if process is not None and process.poll() is None:
                    boundary.abort()
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    entry["exit"] = process.returncode
                entry["cleanup"] = boundary.finish(
                    process.returncode if process is not None else None,
                    pgid=process.pid if process is not None else None,
                )
            finally:
                entry["ended_at"] = time.time()
                self.save()
        text = log.read_text(errors="replace")
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
        return text.strip()

    def inputs(self):
        self.check_deadline()
        toolchain = {}
        for name in ("lake", "lean", "bash", "python3", "git", "tar"):
            executable = shutil.which(name)
            if executable is None:
                raise ValueError(f"verification tool missing: {name}")
            toolchain[name] = dict(path=executable, file=file_record(Path(executable)))
        for command in (
            ["lake", "--version"],
            ["lake", "env", "lean", "--version"],
            ["lake", "env", "lean", "--print-prefix"],
        ):
            toolchain[" ".join(command)] = self.command("toolchain", command, capture=True)
        prefix = Path(toolchain["lake env lean --print-prefix"])
        if not prefix.is_absolute() or not prefix.is_dir():
            raise ValueError("Lean did not report an absolute installed toolchain prefix")
        toolchain["distribution"] = {str(p.relative_to(prefix)): file_record(p) for p in files_under(prefix)}
        deps = self.root / ".lake" / "packages"
        dependencies = {str(p.relative_to(deps)): file_record(p) for p in files_under(deps)}
        config = os.environ.get("LAKE_CONFIG")
        # Hash environment values to avoid recording credentials. Volatile orchestration values do not build inputs.
        volatile = {"TAUCETI_ROUND_DEADLINE", "TAUCETI_RUNTIME_STATUS", "_", "SHLVL"}
        environment = {
            k: digest(v) for k, v in os.environ.items() if not k.startswith("TAUCETI_VERIFY_") and k not in volatile
        }
        verifier = {str(p.name): hash_file(p) for p in Path(__file__).parent.glob("*.py")}
        verifier["wrapper"] = hash_file(HERE / "scripts" / "tauceti-verify")
        sources = inventory(self.root, self.base)
        return dict(
            sources=sources,
            dependencies=dependencies,
            base=self.base,
            scope=scope(self.root, self.base, self.policy),
            configuration=file_record(Path(config)) if config else None,
            environment=environment,
            toolchain=toolchain,
            verifier=verifier,
            policy_version=POLICY_VERSION,
            python=sys.version,
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
            env["AXIOMS_ONLY_MODULES"] = str(modules) if selected["mode"] == "scoped" else ""
            env["AUDIT_PHASES_FILE"] = str(self.directory / "audit-phases.tsv")
            return [(["lake", "exe", "axioms"], env)]
        if phase == "module-system":
            return [(["lake", "exe", "module-system"], env)]
        if phase == "lint":
            if selected["mode"] == "unknown":
                raise Incomplete("lint: changed modules unknown; CI owns the full environment lint")
            commands = []
            if selected["modules"]:
                env["LINT_ONLY_MODULES"] = str(modules)
                commands.append((["bash", "-o", "pipefail", "scripts/lint-env.sh"], env))
            commands.append((["bash", "-o", "pipefail", "scripts/lint-style.sh"], env))
            return commands
        if phase == "shim-expiry":
            script = self.root / "scripts/check-expired-mathlib-shims.py"
            if not script.exists():
                return []  # Supported older TauCeti revisions have no shim registry/checker.
            merge = selected["merge_base"]
            if not merge:
                raise Incomplete("shim-expiry: cannot determine merge-base registry")
            args = ["python3", str(script), "--fail-on-available"]
            baseline = self.directory / "base"
            baseline.mkdir(exist_ok=True)
            try:
                manifest = git(self.root, "show", f"{merge}:TauCeti/mathlib-shims.json")
            except subprocess.CalledProcessError:
                manifest = None
            if manifest is not None:
                (baseline / "mathlib-shims.json").write_bytes(manifest)
                # No shell pipeline: both git and tar exits are checked independently.
                archive = self.directory / "base.tar"
                archive.write_bytes(git(self.root, "archive", merge, "TauCeti"))
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
        try:

            def interrupted(signum, _):
                raise Incomplete(f"verification interrupted by signal {signum}")

            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, interrupted)
            heavy_slots()
            self.check_deadline()
            raw = self.base_arg or os.environ.get("TAUCETI_VERIFY_BASE") or local_main(self.root)
            self.base = git(self.root, "rev-parse", "--verify", f"{raw}^{{commit}}").decode().strip()
            self.receipt["scope"] = scope(self.root, self.base, self.policy)
            self.progress(f"scope: {self.receipt['scope']['mode']}: {self.receipt['scope']['reason']}")
            self.watch_names = source_names(self.root, self.base)
            dep_paths = list(files_under(self.root / ".lake/packages"))
            self.watched = stamps([self.root / p for p in self.watch_names] + dep_paths)
            inputs = self.inputs()
            self.watch()
            extra = list(Path(__file__).parent.glob("*.py")) + [HERE / "scripts/tauceti-verify"]
            prefix = Path(inputs["toolchain"]["lake env lean --print-prefix"])
            extra += list(files_under(prefix))
            self.dependency_names = {str(p) for p in files_under(self.root / ".lake/packages")}
            if os.environ.get("LAKE_CONFIG"):
                extra.append(Path(os.environ["LAKE_CONFIG"]))
            self.watched.update(stamps(extra))
            fingerprint = digest(inputs)
            self.receipt.update(inputs=inputs, fingerprint=fingerprint)

            modules = self.directory / "modules.txt"
            modules.write_text("".join(f"{m}\n" for m in self.receipt["scope"]["modules"]))
            self.save()
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
                record = dict(status="incomplete", started_at=time.time(), ended_at=None)
                self.receipt["phases"][phase] = record
                self.save()
                with self.slot(phase):
                    for argv, env in self.phase_commands(phase, modules):
                        self.command(phase, argv, env=env)
                record.update(status="success", ended_at=time.time())
                self.save()
            # Recompute bytes, pins, dependencies, toolchain, environment and verifier after all tools exit.
            self.last_watch = 0
            self.watch()
            if self.mutated or digest(self.inputs()) != fingerprint:
                raise Incomplete("verification inputs changed during execution")
            self.last_watch = 0
            self.watch()
            self.receipt["status"] = "success"
        except subprocess.CalledProcessError as error:
            self.receipt.update(status="failed", error=f"command exited {error.returncode}: {error.cmd}")
        except (Incomplete, Die, OSError, ValueError, RuntimeError) as error:
            self.receipt.update(status="incomplete", error=str(error))
        finally:
            self.receipt["ended_at"] = time.time()
            timings = []
            timing_file = self.directory / "audit-phases.tsv"
            if timing_file.exists():
                for line in timing_file.read_text(errors="replace").splitlines():
                    try:
                        name, timestamp = line.split("\t")
                        timings.append(dict(phase=name, started_at=float(timestamp)))
                    except (ValueError, TypeError):
                        continue
                for entry, following in zip(timings, timings[1:], strict=False):
                    entry["seconds"] = following["started_at"] - entry["started_at"]
            self.receipt["audit_phase_timings"] = timings
            for phase in self.receipt["phases"].values():
                if phase.get("ended_at") is None:
                    phase.update(ended_at=self.receipt["ended_at"], status=self.receipt["status"])
            self.save()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return self.receipt

    def find_reuse(self, fingerprint, phase):
        for path in sorted((self.state / "verification").glob("*/receipt.json")):
            if path == self.path:
                continue
            try:
                old = json.loads(path.read_text())
                evidence = old.get("phases", {}).get(phase, {})
                # Only a fully input-stable run can contribute evidence, including stage-only runs.
                if (
                    old.get("schema") == SCHEMA
                    and old.get("status") == "success"
                    and old.get("fingerprint") == fingerprint
                    and evidence.get("status") == "success"
                ):
                    return path
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return None


def add_parser(sub):
    parser = sub.add_parser("verify", help="synchronously verify exact local inputs; never query GitHub")
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--base", help="resolved canonical-main commit; defaults to the round's pinned SHA")
    parser.add_argument(
        "--json", action="store_true", help="emit exactly one JSON result on stdout; progress on stderr"
    )
    parser.add_argument("--no-reuse", action="store_true", help="run requested phases even when exact evidence exists")


def command(args):
    state = Path(
        os.environ.get("TAUCETI_VERIFY_STATE") or HERE / "state" / os.environ.get("TAUCETI_WORKER_ID", "local")
    )
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        deadline = float(os.environ["TAUCETI_ROUND_DEADLINE"]) if os.environ.get("TAUCETI_ROUND_DEADLINE") else None
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("round deadline must be finite")
        verification = Verification(
            root,
            state,
            base=args.base,
            stage=args.stage,
            reuse=not args.no_reuse,
            policy=os.environ.get("TAUCETI_VERIFY_POLICY", "ordinary"),
            deadline=deadline,
        )
        result = verification.run()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result = dict(schema=SCHEMA, status="incomplete", error=str(error))
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        print(f"verification: {result['status']} ({result.get('receipt', 'no receipt')})")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return 0 if result["status"] == "success" else 1


def refund_capacity_wait(state: Path, counters) -> bool:
    """Idempotent refund of an attempt interrupted while queued, also after supervisor SIGKILL.

    Runs under the existing per-worker round lock. It restores captured counter values rather
    than decrementing twice if a crash occurs between a counter write and journal completion.
    """
    attempt = read_json(state / "verification-attempt.json")
    wait = read_json(state / "verification-capacity.json")
    if not attempt or wait.get("attempt") != attempt.get("id") or not wait.get("waiting"):
        return False
    if not attempt.get("refunded"):
        for key, value in attempt.get("restore", {}).items():
            counters.write(key, min(counters.read(key), value))
        attempt["refunded"] = True
        atomic(state / "verification-attempt.json", attempt)
    return True


def begin_attempt(w, charged, *, bubble=False):
    if not charged:
        return lambda: False
    state = w.cfg.state / "verification-state" if bubble else w.cfg.state
    refund_capacity_wait(state, w.counters)
    identifier = uuid.uuid4().hex
    atomic(
        state / "verification-attempt.json",
        dict(id=identifier, restore={key: max(0, w.counters.read(key) - 1) for key in charged}),
    )
    os.environ["TAUCETI_VERIFY_ATTEMPT"] = identifier

    def callback():
        return refund_capacity_wait(state, w.counters)

    if getattr(w, "rc", None) is not None:
        w.rc.add_cleanup(callback)
    return callback


def waiting_for_capacity(cfg) -> bool:
    state = getattr(cfg, "state", None)
    if state is None:
        return False
    attempt = os.environ.get("TAUCETI_VERIFY_ATTEMPT")
    for directory in (state, state / "verification-state"):
        wait = read_json(directory / "verification-capacity.json")
        if attempt and wait.get("attempt") == attempt and wait.get("waiting"):
            return True
    return False
