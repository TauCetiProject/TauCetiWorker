"""Upload a frozen native Lake output set, skipping artifacts the bucket already holds.

Stock ``lake cache put-staged`` PUTs every artifact its map names, about 42,000 per repair
publication, although nearly all of them are already in the bucket from earlier publications (R2
bills each PUT as a Class A operation). This module performs the same upload with ``curl``, as Lake
does, to the same URLs with the same content types and signing, and the same order: every artifact
first, then the revision map, and no map unless every artifact is confirmed in the bucket. It
uploads only the artifacts the bucket does not hold:

* a per-host record (``UploadRecord``, SQLite, shared by every repair worker and publisher of the
  user) lists the artifact URLs this host saw uploaded or present in the last ``RECORD_TTL``
  seconds; those are skipped without any request;
* every other artifact gets a signed HEAD (a Class B operation, a twelfth of the price); a 200 is
  recorded and skipped, anything else is uploaded.

Artifact URLs end in the artifact's content hash, so a record entry can only ever stand for the
same bytes. A wrong entry (an object deleted from the bucket within ``RECORD_TTL``) leaves a map
naming a missing artifact, which a fetcher treats as a miss and builds cold. The record never
decides what bytes are published, and a record that cannot be opened only costs HEAD requests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

ARTIFACT_TYPE = "application/vnd.reservoir.artifact"  # Lake's artifactContentType
MAP_TYPE = "application/vnd.reservoir.outputs+json-lines"  # Lake's mapContentType
ATTEMPTS = 3  # tries per transfer, as Lake's transferAttempts
PARALLEL = 32
RECORD_TTL = 7 * 86400.0  # seconds an entry is trusted before the artifact is checked again
RECORD_LIMIT = 1_000_000  # entries kept; the oldest go first
BUSY_TIMEOUT = 60.0  # seconds to wait for another process's write lock on the record


def default_record_path(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".cache")
    return Path(base) / "tauceti-queue" / "repair-uploaded.sqlite3"


def _encode(component: str) -> str:
    """Lake's ``uriEncode``: everything but ASCII letters, digits and ``-_.~`` escaped."""
    return quote(component, safe="")


def scope_path(repository: str) -> str:
    """Lake's ``appendScope`` for ``--repo``: each ``/``-separated component encoded."""
    return "/".join(_encode(part) for part in repository.split("/"))


def toolchain_dir(toolchain: str) -> str:
    """Lake's ``toolchain2Dir``: ``/`` becomes ``--`` and ``:`` becomes ``---``."""
    return "".join("--" if c == "/" else "---" if c == ":" else c for c in toolchain)


def artifact_url(base: str, repository: str, content_hash: str) -> str:
    """Lake's ``s3ArtifactUrl`` under ``--repo``: no platform or toolchain component."""
    return f"{base}/artifacts/{scope_path(repository)}/{content_hash}.art"


def revision_url(base: str, repository: str, toolchain: str, revision: str) -> str:
    """Lake's ``s3RevisionUrl`` under ``--repo`` with ``--toolchain`` and no platform."""
    return f"{base}/revisions/{scope_path(repository)}/tc/{_encode(toolchain_dir(toolchain))}/{revision}.jsonl"


def staged_artifacts(stage: Path) -> list[str]:
    """The distinct artifact content hashes the stage's ``outputs.jsonl`` names, in map order
    (``repair_publish.validate_native_stage`` has checked the map's form)."""
    hashes: dict[str, None] = {}
    for line in (stage / "outputs.jsonl").read_text().splitlines()[1:]:
        row = json.loads(line)
        hashes[row[1].removesuffix(".ltar")] = None
    return list(hashes)


class UploadRecord:
    """Artifact URLs this host has seen in the bucket, with when.

    One SQLite file per host (WAL, so publishers of the three workers read while another writes).
    ``known`` trusts an entry for ``ttl`` seconds; ``add`` stamps entries with the current time;
    ``prune`` drops expired entries and the oldest beyond ``limit``. Every method swallows database
    errors: without the record each artifact is HEAD-checked instead.
    """

    def __init__(self, path: Path, *, ttl: float = RECORD_TTL, limit: int = RECORD_LIMIT) -> None:
        self.path = path
        self.ttl = ttl
        self.limit = limit
        self.error: str | None = None
        self._db: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, timeout=BUSY_TIMEOUT, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS uploaded (url TEXT PRIMARY KEY, at REAL NOT NULL) WITHOUT ROWID")
            db.execute("CREATE INDEX IF NOT EXISTS uploaded_at ON uploaded (at)")
            self._db = db
        except (sqlite3.Error, OSError) as error:
            self.error = f"{type(error).__name__}: {error}"

    def known(self, urls: list[str], now: float | None = None) -> set[str]:
        if self._db is None or not urls:
            return set()
        cutoff = (time.time() if now is None else now) - self.ttl
        found: set[str] = set()
        try:
            for start in range(0, len(urls), 500):
                chunk = urls[start : start + 500]
                marks = ",".join("?" * len(chunk))
                found.update(
                    url
                    for (url,) in self._db.execute(
                        f"SELECT url FROM uploaded WHERE at >= ? AND url IN ({marks})", (cutoff, *chunk)
                    )
                )
        except sqlite3.Error as error:
            self.error = f"{type(error).__name__}: {error}"
            return set()
        return found

    def add(self, urls: list[str], now: float | None = None) -> None:
        if self._db is None or not urls:
            return
        at = time.time() if now is None else now
        try:
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.executemany(
                    "INSERT INTO uploaded (url, at) VALUES (?, ?) ON CONFLICT (url) DO UPDATE SET at = excluded.at",
                    ((url, at) for url in urls),
                )
        except sqlite3.Error as error:
            self.error = f"{type(error).__name__}: {error}"

    def prune(self, now: float | None = None) -> None:
        if self._db is None:
            return
        cutoff = (time.time() if now is None else now) - self.ttl
        try:
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("DELETE FROM uploaded WHERE at < ?", (cutoff,))
                self._db.execute(
                    "DELETE FROM uploaded WHERE url IN (SELECT url FROM uploaded ORDER BY at DESC LIMIT -1 OFFSET ?)",
                    (self.limit,),
                )
        except sqlite3.Error as error:
            self.error = f"{type(error).__name__}: {error}"

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Curl:
    """Signed parallel transfers through ``curl`` (``$CURL``, as Lake reads it, else ``curl``).

    The key reaches curl on its standard input, inside the config, never on its command line.
    """

    def __init__(self, key: str, env: dict, log, *, deadline: float, prefix: tuple[str, ...] = ()) -> None:
        self.key = key
        self.env = env
        self.log = log
        self.deadline = deadline
        self.argv = [*prefix, env.get("CURL") or "curl", "--config", "-"]

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the upload ran out of time")
        return left

    def _once(self, kind: str, items: list[tuple[str, Path | None]], content_type: str | None) -> dict[int, dict]:
        lines = [
            f"user = {_quoted(self.key)}",
            'aws-sigv4 = "aws:amz:auto:s3"',
            "silent",
            'write-out = "%{json}\\n"',
        ]
        if len(items) > 1:
            lines += ["parallel", f"parallel-max = {PARALLEL}"]
        if kind == "head":
            lines.append("head")
        else:
            lines.append(f"header = {_quoted('Content-Type: ' + str(content_type))}")
        for url, path in items:
            if path is not None:
                lines.append(f"upload-file = {_quoted(str(path))}")
            lines += [f"url = {_quoted(url)}", 'output = "/dev/null"']
        config = "\n".join(lines) + "\n"
        proc = subprocess.run(
            self.argv,
            input=config,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=self._left(),
            check=False,
        )
        results: dict[int, dict] = {}
        for line in proc.stdout.splitlines():
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(out, dict) and isinstance(out.get("urlnum"), int):
                results[out["urlnum"]] = out
        if proc.stderr.strip():
            self.log(f"curl {kind}: {proc.stderr.strip()[:2000]}")
        if proc.returncode and not results:
            self.log(f"curl {kind} exited {proc.returncode} reporting no transfer")
        return results

    def transfer(
        self, kind: str, items: list[tuple[str, Path | None]], content_type: str | None = None
    ) -> dict[str, int | None]:
        """HTTP status by URL (None where curl failed the transfer itself), retrying failed transfers,
        429s and 5xx answers up to ``ATTEMPTS`` times."""
        final: dict[str, int | None] = {url: None for url, _ in items}
        pending = list(items)
        for attempt in range(1, ATTEMPTS + 1):
            if not pending:
                break
            if attempt > 1:
                time.sleep(min(float(attempt - 1), max(0.0, self._left() - 1)))
            results = self._once(kind, pending, content_type)
            retry = []
            for index, item in enumerate(pending):
                out = results.get(index) or {}
                code = out.get("http_code") if out.get("exitcode", 0) == 0 else None
                final[item[0]] = code if isinstance(code, int) and code else None
                if final[item[0]] is None or final[item[0]] == 429 or final[item[0]] >= 500:
                    retry.append(item)
            pending = retry
        return final


def upload(
    stage: Path,
    *,
    base: str,
    repository: str,
    toolchain: str,
    revision: str,
    key: str,
    env: dict,
    log_path: Path,
    timeout: float,
    record_path: Path | None,
    prefix: tuple[str, ...] = (),
) -> dict:
    """Upload ``stage`` (a validated native stage) for ``revision``: the artifacts the bucket lacks,
    then the map. Returns counts and ``rc`` (0 when the map was uploaded); never raises."""
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a")

    def log(message: str) -> None:
        handle.write(message.rstrip() + "\n")
        handle.flush()

    result: dict = {"rc": 1, "timed_out": False, "log": str(log_path)}
    record: UploadRecord | None = None
    try:
        log(f"$ upload {stage} --rev={revision} --repo {repository} --toolchain={toolchain} to {base}")
        hashes = staged_artifacts(stage)
        urls = {h: artifact_url(base, repository, h) for h in hashes}
        record = UploadRecord(record_path) if record_path is not None else None
        if record is not None and record.error:
            log(f"upload record {record_path} unavailable ({record.error}); checking every artifact")
        recorded = record.known(list(urls.values())) if record is not None else set()
        curl = Curl(key, env, log, deadline=started + timeout, prefix=prefix)
        unknown = [(h, urls[h]) for h in hashes if urls[h] not in recorded]
        heads = curl.transfer("head", [(url, None) for _, url in unknown]) if unknown else {}
        present = [url for _, url in unknown if heads.get(url) == 200]
        if record is not None:
            record.add(present)
        missing = [(h, url) for h, url in unknown if heads.get(url) != 200]
        result.update(artifacts=len(hashes), recorded=len(recorded), present=len(present), uploaded=0)
        log(
            f"{len(hashes)} artifacts: {len(recorded)} in the host record, {len(present)} found by HEAD, "
            f"{len(missing)} to upload"
        )
        if missing:
            puts = curl.transfer("put", [(url, stage / f"{h}.ltar") for h, url in missing], ARTIFACT_TYPE)
            done = [url for _, url in missing if puts.get(url) in (200, 201)]
            if record is not None:
                record.add(done)
            result["uploaded"] = len(done)
            failed = [url for _, url in missing if puts.get(url) not in (200, 201)]
            if failed:
                for url in failed[:20]:
                    log(f"failed to upload {url}: HTTP {puts.get(url)}")
                log(f"{len(failed)} artifact upload(s) failed; the revision map is not uploaded")
                return result
        map_url = revision_url(base, repository, toolchain, revision)
        put = curl.transfer("put", [(map_url, stage / "outputs.jsonl")], MAP_TYPE)
        if put.get(map_url) not in (200, 201):
            log(f"failed to upload the revision map {map_url}: HTTP {put.get(map_url)}")
            return result
        log(f"uploaded the revision map {map_url}")
        result["rc"] = 0
        if record is not None:
            record.prune()
            if record.error:
                log(f"upload record {record_path}: {record.error}")
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        result.update(rc=124, timed_out=True)
        log(f"timed out: {error}")
    except Exception as error:  # the caller records the failure; publication never raises
        result["rc"] = 1
        log(f"{type(error).__name__}: {error}")
    finally:
        if record is not None:
            record.close()
        result["seconds"] = round(time.monotonic() - started, 1)
        handle.close()
    return result
