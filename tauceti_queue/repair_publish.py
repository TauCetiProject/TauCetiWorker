"""Publish a repair's build outputs to the workers' own Lake cache namespace.

After a repair's final green ``lake build`` the worktree holds every module's outputs for the chain
tip. Publishing them under the tip's revision lets the next repair round on the same head, and the
next head's agent, start warm instead of rebuilding what this repair already built. The namespace is
the workers' alone: CI fetches only from the public service (``tauceti-public``), so nothing
published here changes a CI verdict.

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
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from tauceti_queue import repair

SERVICE = "tauceti-repair"
UPLOAD_SERVICE = "tauceti-repair-upload"
PUBLISH_TIMEOUT = 1800.0  # seconds for the upload itself
CHECK_TIMEOUT = 900.0  # seconds for the `--no-build` pass that writes the output map
TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")


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

    @classmethod
    def from_env(cls, environ: dict | None = None) -> Settings:
        env = os.environ if environ is None else environ
        flag = (env.get("TAUCETI_QUEUE_PUBLISH_CACHE") or "").strip().lower()
        key_file = (env.get("TAUCETI_REPAIR_CACHE_KEY_FILE") or "").strip()
        timeout = (env.get("TAUCETI_REPAIR_CACHE_PUBLISH_TIMEOUT") or "").strip()
        return cls(
            url=(env.get("TAUCETI_REPAIR_CACHE_URL") or "").strip().rstrip("/") or None,
            upload_url=(env.get("TAUCETI_REPAIR_CACHE_UPLOAD_URL") or "").strip().rstrip("/") or None,
            key_file=Path(key_file) if key_file else None,
            publish=True if flag in TRUE else False if flag in FALSE else None,
            timeout=float(timeout) if timeout else PUBLISH_TIMEOUT,
        )

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


def publish_outputs(
    worktree: Path,
    revision: str,
    *,
    settings: Settings,
    env: dict,
    logdir: Path,
    repository: str = repair.DEFAULT_REPOSITORY,
    lake: tuple[str, ...] = ("lake",),
) -> dict:
    """Publish the worktree's outputs for ``revision`` (its checked-out HEAD) to the repair namespace.

    ``lake build --no-build --rehash -o`` writes the output map and exits nonzero unless every
    module is built and current, so only a green final build at this revision is published. Then
    ``lake cache put`` uploads the map and its artifacts under ``revision`` with a bounded timeout.
    Returns the record (``published``, ``skipped`` or ``error``, sizes, seconds); never raises.
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
            [*lake, "build", "--no-build", "--rehash", "-o", str(mapping)],
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
            [*lake, "cache", "put", str(mapping), "--service", settings.upload_service, "--repo", repository],
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
