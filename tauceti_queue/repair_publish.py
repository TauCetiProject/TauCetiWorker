"""Publish a repair's build outputs to the workers' own Lake cache namespace.

After a repair's final green ``lake build`` the worktree holds every module's outputs for the chain
tip. Publishing them under the tip's revision lets the next repair round on the same head, and the
next head's agent, start warm instead of rebuilding what this repair already built. CI fetches from
this namespace only the exact staging head (TauCetiTools ``repair-cache-get.sh``) and Lake checks every
trace, so nothing published here changes a CI verdict.

The namespace is two Lake cache services written into the private pool's ``lake.toml``:

- ``tauceti-repair`` (``TAUCETI_REPAIR_CACHE_URL``), the read side, which ``prepare_artifacts``
  consults for the task's ``tip``, ``onto`` and ``base`` before the public service; Lake fetches
  without credentials, so this is a publicly readable endpoint;
- ``tauceti-repair-upload`` (``TAUCETI_REPAIR_CACHE_UPLOAD_URL``, default the read URL), the S3
  write side of the same bucket, which ``lake cache put`` signs with the key.

Each base URL holds ``artifacts/`` and ``revisions/``, laid out as the public cache is
(``revisions/<owner>/<repo>/tc/<toolchain>/<rev>.jsonl``, through ``--repo``). The write key (the
``access-key-id:secret`` pair ``lake cache put`` takes as ``LAKE_CACHE_KEY``) is read from the file
named by ``TAUCETI_REPAIR_CACHE_KEY_FILE`` and given only to the ``lake cache put`` process: it is
never in the worker's environment, so the agent, which inherits that environment, never holds it.
Use a bucket of its own with a token scoped to it, so the key cannot write the public cache.

``TAUCETI_QUEUE_PUBLISH_CACHE`` turns publication on or off; unset, it is on when the key file and an
upload URL are configured. Publication never fails a task: every failure is logged and recorded.

``TAUCETI_REPAIR_PUBLISH`` says when a repair publishes: ``after`` (the default) submits the chain
first and then publishes the submitted tip in a process of its own (``start_background``), so the
rehash and upload of a whole library's outputs never delay CI's dispatch; ``before`` publishes in the
task before the push, as the worker first did; ``off`` publishes nothing. The background publisher
outlives the worker: under systemd it runs in a transient unit of its own (``systemd-run --user``,
since the worker unit's ``KillMode=mixed`` kills every process left in its cgroup on a stop),
elsewhere in a new session. It writes ``logs/publish.log`` and the record ``logs/publish.json`` under
the task, removes the task's worktree when done, and ``background_status`` reports what it did in
``worker.json``. Both commands run under ``nice`` so a concurrent repair or build on the host keeps
the CPU. ``lake cache put`` uploads every artifact before the revision mapping and exits without
writing the mapping when an artifact upload fails, so the fetcher (TauCetiTools
``repair-cache-get.sh``, for which a missing mapping is the normal case) never sees a mapping for a
partially uploaded set; a publisher killed mid-upload leaves no mapping either.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from tauceti_queue import repair

SERVICE = "tauceti-repair"
UPLOAD_SERVICE = "tauceti-repair-upload"
PUBLISH_TIMEOUT = 1800.0  # seconds for the upload itself
CHECK_TIMEOUT = 900.0  # seconds for the `--no-build` pass that writes the output map
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


def _artifact_bytes(mapping: Path, artifacts: Path) -> tuple[int, int]:
    """Count and total size of the artifacts the output map names (rows ``[input, artifact]``)."""
    count = size = 0
    for line in mapping.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, list) and len(row) >= 2 and isinstance(row[1], str):
            count += 1
            try:
                size += (artifacts / row[1]).stat().st_size
            except OSError:
                pass
    return count, size


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
    """Publish the worktree's outputs for ``revision`` (its checked-out HEAD) to the repair namespace.

    ``lake build --no-build --rehash -o`` writes the output map and exits nonzero unless every
    module is built and current, so only a green final build at this revision is published. Then
    ``lake cache put`` uploads the artifacts and then the map under ``revision`` with a bounded
    timeout. Both run under ``nice``. Returns the record (``published``, ``skipped`` or ``error``, sizes, seconds); never raises.
    """
    record: dict = {"revision": revision, "service": settings.upload_service, "published": False}
    reason = settings.disabled_reason()
    if reason:
        record["skipped"] = reason
        return record
    started = time.monotonic()
    try:
        head = repair.git(worktree, "rev-parse", "HEAD").strip()
        if head != revision:
            record["skipped"] = f"worktree HEAD {head[:12]} is not {revision[:12]}"
            return record
        if repair.git(worktree, "status", "--porcelain", "--untracked-files=no").strip():
            record["skipped"] = "worktree has uncommitted changes"
            return record
        mapping = logdir / "repair-outputs.jsonl"
        mapping.unlink(missing_ok=True)
        check = repair.run_logged(
            niced([*lake, "build", "--no-build", "--rehash", "-o", str(mapping)], nice, env),
            worktree,
            logdir / "publish-outputs.log",
            env=env,
            timeout=CHECK_TIMEOUT,
        )
        record["outputs_check"] = {k: check[k] for k in ("rc", "seconds", "timed_out")}
        if check["rc"] != 0 or not mapping.is_file():
            record["skipped"] = f"outputs not complete at {revision[:12]} (lake build --no-build exited {check['rc']})"
            return record
        artifacts = Path(env.get("LAKE_CACHE_DIR") or worktree / ".lake" / "cache") / "artifacts"
        record["artifacts"], record["bytes"] = _artifact_bytes(mapping, artifacts)
        put_env = dict(env)
        put_env["LAKE_CACHE_KEY"] = settings.key() or ""
        put = repair.run_logged(
            niced(
                [*lake, "cache", "put", str(mapping), "--service", settings.upload_service, "--repo", repository],
                nice,
                env,
            ),
            worktree,
            logdir / "cache-put.log",
            env=put_env,
            timeout=settings.timeout,
        )
        record["put"] = {k: put[k] for k in ("rc", "seconds", "timed_out", "log")}
        if put["rc"] != 0:
            record["error"] = (
                f"lake cache put did not finish within {settings.timeout:.0f}s"
                if put["timed_out"]
                else f"lake cache put exited {put['rc']}"
            )
        else:
            record["published"] = True
    except Exception as error:  # publication never fails the task
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        if record["published"]:
            log(
                f"published {record['artifacts']} artifacts ({record['bytes'] / 2**20:.1f} MiB) for "
                f"{revision[:12]} to {settings.upload_service} in {record['seconds']}s"
            )
        elif "error" in record or "skipped" in record:
            log(f"not published for {revision[:12]}: {record.get('error') or record.get('skipped')}")
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
    child_env = dict(env)
    child_env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p)
    argv = [sys.executable, "-m", "tauceti_queue.repair_publish", str(logdir / SPEC)]
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
        return record
    record.update(state="running", launched=True)
    BACKGROUND.add(record, proc)
    log(
        f"publishing {revision[:12]} for task {task_id} in the background "
        f"({'unit ' + record['unit'] if 'unit' in record else 'pid ' + str(record.get('pid'))}; log {record['log']})"
    )
    return record


def run_spec(spec_path: Path) -> int:
    """The background publisher: publish as the spec says, write the record, then remove the worktree."""
    spec = json.loads(spec_path.read_text())
    path = Path(spec["record"]["record"])
    worktree = Path(spec["worktree"])
    record = dict(spec["record"])
    record["pid"] = os.getpid()
    try:
        result = publish_outputs(
            worktree,
            spec["revision"],
            settings=Settings.from_json(spec["settings"]),
            env=dict(os.environ),
            logdir=Path(spec["logdir"]),
            repository=spec["repository"],
            lake=tuple(spec["lake"]),
            nice=int(spec.get("nice", NICE)),
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
