"""Publish native Lake outputs without another whole-library graph scan.

The final build writes .lake/outputs.jsonl. Native `lake cache stage` freezes
its complete artifacts outside the disposable worktree; stock `put-staged`
uploads them with explicit repository, revision and toolchain and never loads
workspace configuration. The write key reaches only the native upload process.
Lake uploads artifacts before the revision map. Failures remain visible in
existing publication records and never change the compilation/audit verdict.

Early publication can overlap checks. An independent transient unit owns its
immutable staging directory and removes it on completion. The late fallback
uses the same native path and removes its source worktree when finished.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from tauceti_queue import repair

SERVICE = "tauceti-repair"
UPLOAD_SERVICE = "tauceti-repair-upload"
PUBLISH_TIMEOUT = 1800.0  # seconds for the upload itself
CHECK_TIMEOUT = 900.0  # seconds to stage the final build map
TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")
WHEN = ("after", "before", "off")  # TAUCETI_REPAIR_PUBLISH: after the submission, before the push, never
NICE = 10  # the niceness of the rehash and the upload
RECORD = "publish.json"  # the background publication's record, in the task's logs/
BACKGROUND_LOG = "publish.log"
SPEC = "publish-spec.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The environment a publisher in a transient systemd unit gets (the user manager's own is not the
# worker's); everything else, the worker's credentials included, stays behind.
UNIT_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "ELAN_HOME", "PYTHONPATH")


def log(message: str) -> None:
    try:
        from tauceti_worker.config import log as worker_log

        worker_log(f"queue-repair: cache publication: {message}")
    except Exception:
        print(f"queue-repair: cache publication: {message}", file=sys.stderr, flush=True)


@dataclasses.dataclass(frozen=True)
class Settings:
    url: str | None = None  # read base: <url>/artifacts, <url>/revisions
    upload_url: str | None = None  # S3 write base; default ``url``
    key_file: Path | None = None
    publish: bool | None = None  # None: on when the key file and an upload URL are configured
    timeout: float = PUBLISH_TIMEOUT
    service: str = SERVICE
    upload_service: str = UPLOAD_SERVICE
    when: str = "after"  # TAUCETI_REPAIR_PUBLISH: after | before | off

    @classmethod
    def from_env(cls, environ: dict | None = None) -> Settings:
        env = os.environ if environ is None else environ
        flag = (env.get("TAUCETI_QUEUE_PUBLISH_CACHE") or "").strip().lower()
        key_file = (env.get("TAUCETI_REPAIR_CACHE_KEY_FILE") or "").strip()
        timeout = (env.get("TAUCETI_REPAIR_CACHE_PUBLISH_TIMEOUT") or "").strip()
        when = (env.get("TAUCETI_REPAIR_PUBLISH") or "").strip().lower()
        if when and when not in WHEN:
            log(f"TAUCETI_REPAIR_PUBLISH={when!r} is not one of {', '.join(WHEN)}; publishing after submission")
        return cls(
            url=(env.get("TAUCETI_REPAIR_CACHE_URL") or "").strip().rstrip("/") or None,
            upload_url=(env.get("TAUCETI_REPAIR_CACHE_UPLOAD_URL") or "").strip().rstrip("/") or None,
            key_file=Path(key_file) if key_file else None,
            publish=True if flag in TRUE else False if flag in FALSE else None,
            timeout=float(timeout) if timeout else PUBLISH_TIMEOUT,
            when=when if when in WHEN else "after",
        )

    def to_json(self) -> dict:
        return {
            f.name: str(getattr(self, f.name)) if isinstance(getattr(self, f.name), Path) else getattr(self, f.name)
            for f in dataclasses.fields(self)
        }

    @classmethod
    def from_json(cls, record: dict) -> Settings:
        values = {f.name: record[f.name] for f in dataclasses.fields(cls) if f.name in record}
        if values.get("key_file"):
            values["key_file"] = Path(values["key_file"])
        return cls(**values)

    @property
    def upload_base(self) -> str | None:
        return self.upload_url or self.url

    def key(self) -> str | None:
        if self.key_file is None:
            return None
        try:
            return self.key_file.read_text().strip() or None
        except OSError:
            return None

    def disabled_reason(self) -> str | None:
        """Why publication is off, or None when it is on."""
        if self.when == "off":
            return "TAUCETI_REPAIR_PUBLISH is off"
        if self.publish is False:
            return "TAUCETI_QUEUE_PUBLISH_CACHE is off"
        if not self.upload_base:
            return "no repair cache URL (TAUCETI_REPAIR_CACHE_URL or TAUCETI_REPAIR_CACHE_UPLOAD_URL)"
        if self.key() is None:
            return "no write key (TAUCETI_REPAIR_CACHE_KEY_FILE)"
        return None


def _service_block(name: str, base: str) -> str:
    return (
        f'[[cache.service]]\nname = "{name}"\nkind = "s3"\n'
        f'artifactEndpoint = "{base}/artifacts"\nrevisionEndpoint = "{base}/revisions"\n'
    )


def configure(env: dict, settings: Settings) -> None:
    """Add the repair namespace's services to the pool's ``lake.toml`` (replacing earlier entries of
    the same names) and drop any ``LAKE_CACHE_KEY`` from ``env``, which the agent inherits."""
    env.pop("LAKE_CACHE_KEY", None)
    config = Path(env["LAKE_CONFIG"])
    text = config.read_text() if config.exists() else ""
    marker = "[[cache.service]]"
    head, *sections = text.split(marker)
    ours = (f'name = "{settings.service}"', f'name = "{settings.upload_service}"')
    kept = head + "".join(marker + section for section in sections if not any(name in section for name in ours))
    blocks = [
        _service_block(name, base)
        for name, base in ((settings.service, settings.url), (settings.upload_service, settings.upload_base))
        if base
    ]
    new = kept + "".join(blocks)
    if new != text:
        config.write_text(new)


def preferred(settings: Settings, task) -> tuple[tuple[str, str], ...]:
    """The (service, revision) fetches to try before the public cache: the repair namespace at the
    task's ``tip``, ``onto`` and ``base``, when the namespace is configured."""
    if not settings.url:
        return ()
    revisions: list[str] = []
    for revision in (task.tip, task.onto, task.base):
        if revision and revision not in revisions:
            revisions.append(revision)
    return tuple((settings.service, revision) for revision in revisions)


def warm(settings: Settings, task) -> tuple[tuple[str, str], ...]:
    """The (service, revision) fetches that all run before the public cache, every one with a mapping
    restoring its outputs: the controller's ``warm_revs`` (the task's nearby revisions: a head
    task's tips, or a ``pre_rebase`` or ``prepare`` task's), then ``preferred``'s, without repeats; empty when the task names none or the
    namespace is not configured."""
    revisions = list(getattr(task, "warm_revs", None) or ())
    if not settings.url or not revisions:
        return ()
    ordered = dict.fromkeys([*revisions, *(revision for _, revision in preferred(settings, task))])
    return tuple((settings.service, revision) for revision in ordered if revision)


def validate_native_stage(stage: Path) -> None:
    """Keep only the native flat file boundary before credentials are available.

    The build and native staging processes have exited under the build lock;
    Lake owns artifact validity. Do not follow any supplied path or special file.
    """
    fd = os.open(stage / "outputs.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("native staging accepts only ordinary files")
        raw = handle.read(16 * 1024**2 + 1)
    if len(raw) > 16 * 1024**2:
        raise ValueError("oversized native output map")
    header, *lines = raw.decode().splitlines()
    if json.loads(header) != "2026-03-17" or not lines:
        raise ValueError("unsupported or empty native output map")
    names = {"outputs.jsonl"}
    inputs = set()
    for line in lines:
        row = json.loads(line)
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not isinstance(row[0], str)
            or not re.fullmatch(r"[0-9a-f]{16}", row[0])
            or row[0] in inputs
            or not isinstance(row[1], str)
            or not re.fullmatch(r"[0-9a-f]{16}\.ltar", row[1])
        ):
            raise ValueError("unsafe native output map entry")
        inputs.add(row[0])
        names.add(row[1])
    if {p.name for p in stage.iterdir()} != names:
        raise ValueError("staged files do not match native output map")
    for name in names:
        if not stat.S_ISREG((stage / name).lstat().st_mode):
            raise ValueError("native staging accepts only ordinary files")


def niced(argv: list[str], nice: int, env: dict) -> list[str]:
    """``argv`` under ``nice -n <nice>`` when ``nice`` is positive and on the ``PATH``."""
    if nice > 0 and shutil.which("nice", path=env.get("PATH")):
        return ["nice", "-n", str(nice), *argv]
    return list(argv)


def publish_outputs(
    worktree: Path,
    revision: str,
    *,
    settings: Settings,
    env: dict,
    logdir: Path,
    repository: str = repair.DEFAULT_REPOSITORY,
    lake: tuple[str, ...] = ("lake",),
    nice: int = NICE,
) -> dict:
    """Publish the final build's native staged outputs, without another build scan."""
    reason = settings.disabled_reason()
    if reason:
        return {"revision": revision, "published": False, "skipped": reason}
    try:
        frozen = freeze_outputs(worktree, revision, env, logdir, lake)
        if frozen is None:
            return {"revision": revision, "published": False, "skipped": "no complete final-build map"}
        return publish_frozen(
            {
                "settings": settings.to_json(),
                "frozen": frozen,
                "revision": revision,
                "repository": repository,
                "lake": list(lake),
                "logdir": str(logdir),
                "nice": nice,
            }
        )
    except Exception as error:
        return {"revision": revision, "published": False, "error": f"{type(error).__name__}: {error}"}


EARLY_SETTINGS = "publication.json"


def write_early_settings(
    worktree: Path, *, settings: Settings, logdir: Path, task_id: str, repository: str, lake: tuple[str, ...]
) -> None:
    """Host-side helper configuration, containing a key-file path, never its contents."""
    repair.write_record(
        worktree / repair.LISTINGS_DIR / EARLY_SETTINGS,
        {
            "settings": settings.to_json(),
            "logdir": str(logdir),
            "task_id": task_id,
            "repository": repository,
            "lake": list(lake),
            "python": sys.executable,
        },
    )


def early_record(logdir: Path, revision: str, settings: Settings) -> dict | None:
    record = _read(logdir / "publications" / revision / RECORD)
    if record.get("revision") != revision:
        return None
    if record.get("published") or (
        record.get("state") == "running"
        and time.time() - record.get("started_at", 0) < settings.timeout + CHECK_TIMEOUT + 120
    ):
        return record
    return None


def build_flags(worktree: Path) -> list[str]:
    """Emit a map during the final native build when publication is configured."""
    config = _read(worktree / repair.LISTINGS_DIR / EARLY_SETTINGS)
    settings = config.get("settings") or {}
    flags = ["--iofail"]
    if (
        (settings.get("url") or settings.get("upload_url"))
        and settings.get("key_file")
        and settings.get("publish") is not False
        and settings.get("when") != "off"
    ):
        flags += ["--rehash", "-o", ".lake/outputs.jsonl"]
    return flags


def freeze_outputs(worktree: Path, revision: str, env: dict, logdir: Path, lake: tuple[str, ...]) -> dict | None:
    """Native staging with no credentials, moved outside candidate-writable paths."""
    from tauceti_queue import repair_sandbox

    mapping = worktree / ".lake/outputs.jsonl"
    if not mapping.is_file() or repair.git(worktree, "rev-parse", "HEAD").strip() != revision:
        return None
    if repair.git(worktree, "status", "--porcelain", "--untracked-files=no").strip():
        return None
    stage = worktree / ".lake/publication-stage"
    shutil.rmtree(stage, ignore_errors=True)
    argv = [*lake, "cache", "stage", ".lake/outputs.jsonl", ".lake/publication-stage"]
    wrapped = repair_sandbox.sandboxed_lake(argv, worktree, env)
    check = repair.run_logged(wrapped, worktree, logdir / "cache-stage.log", env=env, timeout=CHECK_TIMEOUT)
    if check["rc"] or not (stage / "outputs.jsonl").is_file():
        shutil.rmtree(stage, ignore_errors=True)
        return None
    if (
        repair.git(worktree, "rev-parse", "HEAD").strip() != revision
        or repair.git(worktree, "status", "--porcelain", "--untracked-files=no").strip()
    ):
        shutil.rmtree(stage, ignore_errors=True)
        return None
    destination = worktree.parent / (worktree.name + "-publish-" + revision)
    if destination.exists():
        shutil.rmtree(stage, ignore_errors=True)
        return None
    toolchain = (worktree / "lean-toolchain").read_text().strip()
    if not toolchain or any(c.isspace() for c in toolchain):
        shutil.rmtree(stage, ignore_errors=True)
        return None
    try:
        validate_native_stage(stage)
        stage.rename(destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    artifacts = list(destination.glob("*.ltar"))
    return {
        "path": str(destination),
        "toolchain": toolchain,
        "artifacts": len(artifacts),
        "bytes": sum(p.stat().st_size for p in artifacts),
        "outputs_check": {"kind": "native-stage", **{k: check[k] for k in ("rc", "seconds", "timed_out")}},
    }


def start_early(worktree: Path, env: dict) -> dict | None:
    """Upload the final native build's staged outputs during audits and lint."""
    config = _read(worktree / repair.LISTINGS_DIR / EARLY_SETTINGS)
    if not config:
        return None
    settings = Settings.from_json(config["settings"])
    if settings.when != "after" or settings.disabled_reason():
        return None
    frozen = None
    try:
        revision = repair.git(worktree, "rev-parse", "HEAD").strip()
        existing = early_record(Path(config["logdir"]), revision, settings)
        if existing:
            return existing
        layout = tomllib.loads((worktree / "lakefile.toml").read_text())
        if layout.get("platformIndependent") is not True or layout.get("fixedToolchain") or layout.get("bootstrap"):
            return None
        logdir = Path(config["logdir"]) / "publications" / revision
        logdir.mkdir(parents=True, exist_ok=True)
        frozen = freeze_outputs(worktree, revision, env, logdir, tuple(config["lake"]))
        if frozen is None:
            return {"revision": revision, "launched": False, "skipped": "no complete final-build map"}
        record = start_background(
            worktree,
            revision,
            settings=settings,
            env=env,
            logdir=logdir,
            task_id=config["task_id"],
            repository=config["repository"],
            lake=tuple(config["lake"]),
            remove_worktree=False,
            frozen=frozen,
            python=config.get("python"),
        )
        if record.get("launched"):
            frozen = None
        return record
    except Exception as error:
        log(f"early publication unavailable: {type(error).__name__}: {error}")
        return {"launched": False, "error": f"{type(error).__name__}: {error}"}
    finally:
        if frozen:
            shutil.rmtree(frozen["path"], ignore_errors=True)


def publish_frozen(spec: dict) -> dict:
    settings = Settings.from_json(spec["settings"])
    frozen = spec["frozen"]
    stage = Path(frozen["path"])
    record = {
        "revision": spec["revision"],
        "service": settings.upload_service,
        "published": False,
        "early": True,
        **{k: frozen[k] for k in ("artifacts", "bytes", "outputs_check")},
    }
    started = time.monotonic()
    try:
        reason = settings.disabled_reason()
        if reason:
            record["skipped"] = reason
            return record
        env = dict(os.environ, LAKE_CACHE_KEY=settings.key() or "")
        argv = [
            *spec["lake"],
            "cache",
            "put-staged",
            str(stage),
            f"--rev={spec['revision']}",
            "--service",
            settings.upload_service,
            "--repo",
            spec["repository"],
            f"--toolchain={frozen['toolchain']}",
        ]
        put = repair.run_logged(
            niced(argv, int(spec.get("nice", NICE)), env),
            stage,
            Path(spec["logdir"]) / "cache-put.log",
            env=env,
            timeout=settings.timeout,
        )
        record["put"] = {k: put[k] for k in ("rc", "seconds", "timed_out", "log")}
        if put["rc"]:
            record["error"] = f"lake cache put-staged exited {put['rc']}"
        else:
            record["published"] = True
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        shutil.rmtree(stage, ignore_errors=True)
    return record


# ----------------------------------------------------------------------------------------------
# Publication after submission, in a process of its own


class Background:
    """The background publications this worker process started, for ``worker.json``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running: dict[str, dict] = {}  # record path -> launch record (with the process, when ours)
        self.published = 0
        self.failed = 0
        self.last: dict | None = None
        self.last_failure: dict | None = None

    def add(self, record: dict, proc: subprocess.Popen | None) -> None:
        with self._lock:
            self.running[record["record"]] = {"record": record, "proc": proc}

    def _settle(self, path: str, entry: dict) -> dict | None:
        """The finished record at ``path``, or None while its publisher runs."""
        proc = entry["proc"]
        record = _read(Path(path))
        if record.get("state") not in (None, "running"):
            return record
        if proc is None or proc.poll() is None:
            return None
        failed = dict(entry["record"])
        failed.update(state="failed", error=f"the publisher exited {proc.returncode} without a record")
        return failed

    def status(self) -> dict:
        """``running`` (task, revision, since), counts, and the last result and failure; every result is
        logged once, a failure as such."""
        with self._lock:
            for path, entry in list(self.running.items()):
                record = self._settle(path, entry)
                if record is None:
                    continue
                del self.running[path]
                summary = {
                    k: record.get(k)
                    for k in ("task_id", "revision", "state", "published", "error", "skipped", "seconds", "artifacts")
                }
                summary["log"] = entry["record"].get("log")
                summary["at"] = record.get("finished_at") or time.time()
                self.last = summary
                if record.get("published"):
                    self.published += 1
                elif record.get("state") == "failed":
                    self.failed += 1
                    self.last_failure = summary
                    log(
                        f"background publication for task {record.get('task_id')} at "
                        f"{str(record.get('revision'))[:12]} failed: {record.get('error')} (see {summary['log']})"
                    )
            return {
                "running": [
                    {k: e["record"].get(k) for k in ("task_id", "revision", "started_at", "pid", "unit")}
                    for e in self.running.values()
                ],
                "published": self.published,
                "failed": self.failed,
                "last": self.last,
                "last_failure": self.last_failure,
            }

    def wait(self, timeout: float | None = None) -> list[dict]:
        """Wait for the publishers this process started as subprocesses; return their records."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            entries = list(self.running.items())
        for _path, entry in entries:
            if entry["proc"] is not None:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    entry["proc"].wait(left)
                except subprocess.TimeoutExpired:
                    pass
        return [_read(Path(path)) for path, _entry in entries]


BACKGROUND = Background()


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def background_status() -> dict | None:
    """What ``worker.json`` reports of this process's background publications, None before the first."""
    if not (BACKGROUND.running or BACKGROUND.last):
        return None
    return BACKGROUND.status()


def _under_systemd(env: dict) -> bool:
    return bool(env.get("INVOCATION_ID")) and shutil.which("systemd-run", path=env.get("PATH")) is not None


def _spawn(argv: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    """The publisher in a session of its own, so the worker's signals do not reach it."""
    with open(log_path, "ab") as handle:
        return subprocess.Popen(
            argv,
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def start_background(
    worktree: Path,
    revision: str,
    *,
    settings: Settings,
    env: dict,
    logdir: Path,
    task_id: str,
    repository: str = repair.DEFAULT_REPOSITORY,
    lake: tuple[str, ...] = ("lake",),
    clone: Path | None = None,
    remove_worktree: bool = False,
    nice: int = NICE,
    systemd: bool | None = None,
    frozen: dict | None = None,
    python: str | None = None,
) -> dict:
    """Publish the worktree's outputs for ``revision`` in a process of its own and return at once.

    The publisher runs ``publish_outputs``, writes its record to ``logs/publish.json`` (``state``
    ``published``, ``skipped`` or ``failed``) and its output to ``logs/publish.log``, and then removes
    the worktree when ``remove_worktree`` (the caller leaves it in place for it). Under systemd
    (``systemd``, by default when the worker runs in a unit) it is a transient unit of its own, so a
    stop or restart of the worker does not kill it. The returned record says whether it was
    ``launched``; when it was not, the worktree is still the caller's. Never raises.
    """
    logdir.mkdir(parents=True, exist_ok=True)
    path = logdir / RECORD
    record: dict = {
        "revision": revision,
        "service": settings.upload_service,
        "published": False,
        "background": True,
        "launched": False,
        "task_id": task_id,
        "record": str(path),
        "log": str(logdir / BACKGROUND_LOG),
        "started_at": time.time(),
    }
    reason = settings.disabled_reason()
    if reason:
        record.update(state="skipped", skipped=reason)
        return record
    spec = {
        "worktree": str(worktree),
        "cache_root": str(Path(env["LAKE_CACHE_DIR"]).parent)
        if env.get("LAKE_CACHE_DIR")
        else str(worktree.parent.parent),
        "revision": revision,
        "settings": settings.to_json(),
        "logdir": str(logdir),
        "task_id": task_id,
        "repository": repository,
        "lake": list(lake),
        "clone": str(clone) if clone else None,
        "remove_worktree": bool(remove_worktree and clone),
        "nice": nice,
        "record": dict(record, state="running", launched=True),
    }
    if frozen is not None:
        spec["frozen"] = frozen
    child_env = dict(env)
    child_env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p)
    argv = [python or sys.executable, "-m", "tauceti_queue.repair_publish", str(logdir / SPEC)]
    proc: subprocess.Popen | None = None
    try:
        repair.write_record(logdir / SPEC, spec)
        repair.write_record(path, spec["record"])
        use_systemd = _under_systemd(env) if systemd is None else systemd
        if use_systemd:
            unit = f"tauceti-repair-publish-{task_id}-{revision[:12]}-{int(time.time())}"
            run = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--quiet",
                    "--collect",
                    f"--unit={unit}",
                    f"--working-directory={PROJECT_ROOT}",
                    f"--property=StandardOutput=append:{record['log']}",
                    f"--property=StandardError=append:{record['log']}",
                    *(f"--setenv={k}={child_env[k]}" for k in (*UNIT_ENV, *repair.LAKE_ENV_KEYS) if k in child_env),
                    *argv,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if run.returncode == 0:
                record["unit"] = unit
            else:
                log(f"systemd-run exited {run.returncode} ({run.stderr.strip()[:300]}); publishing in a new session")
                use_systemd = False
        if not use_systemd:
            proc = _spawn(argv, child_env, Path(record["log"]))
            record["pid"] = proc.pid
    except Exception as error:  # publication never fails the task
        record.update(state="failed", error=f"{type(error).__name__}: {error}")
        log(f"not published for {revision[:12]}: could not start the publisher: {record['error']}")
        repair.write_record(path, record)
        return record
    record.update(state="running", launched=True)
    BACKGROUND.add(record, proc)
    log(
        f"publishing {revision[:12]} for task {task_id} in the background "
        f"({'unit ' + record['unit'] if 'unit' in record else 'pid ' + str(record.get('pid'))}; log {record['log']})"
    )
    return record


def run_spec(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    try:
        from tauceti_queue.repair_cache import cache_lease
        from tauceti_queue.repair_cleanup import worktree_lease

        root = Path(spec["worktree"]).parent.parent
        with cache_lease(Path(spec.get("cache_root", root))), worktree_lease(root, spec["task_id"]):
            return _run_spec(spec_path)
    except Exception as error:
        record = dict(
            spec["record"],
            state="failed",
            published=False,
            error=f"publisher startup: {type(error).__name__}: {error}",
            finished_at=time.time(),
        )
        repair.write_record(Path(record["record"]), record)
        if spec.get("frozen"):
            shutil.rmtree(spec["frozen"]["path"], ignore_errors=True)
        log(record["error"])
        return 1


def _run_spec(spec_path: Path) -> int:
    """The background publisher: publish as the spec says, write the record, then remove the worktree."""
    spec = json.loads(spec_path.read_text())
    path = Path(spec["record"]["record"])
    worktree = Path(spec["worktree"])
    record = dict(spec["record"])
    record["pid"] = os.getpid()
    try:
        result = (
            publish_frozen(spec)
            if spec.get("frozen")
            else publish_outputs(
                worktree,
                spec["revision"],
                settings=Settings.from_json(spec["settings"]),
                env=dict(os.environ),
                logdir=Path(spec["logdir"]),
                repository=spec["repository"],
                lake=tuple(spec["lake"]),
                nice=int(spec.get("nice", NICE)),
            )
        )
        record.update(result)
    except Exception as error:  # publish_outputs does not raise; the record must be written regardless
        record["error"] = f"{type(error).__name__}: {error}"
    record["state"] = "published" if record.get("published") else "failed" if record.get("error") else "skipped"
    record["finished_at"] = time.time()
    repair.write_record(path, record)
    if spec.get("remove_worktree") and spec.get("clone"):
        try:
            repair.remove_worktree(Path(spec["clone"]), worktree)
        except Exception as error:
            log(f"could not remove {worktree}: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(run_spec(Path(sys.argv[1])))
