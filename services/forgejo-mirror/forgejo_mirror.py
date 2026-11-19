#!/usr/bin/env python3
"""Keep an off-host bare mirror of a Forgejo repository and its forks (services/forgejo-mirror/README.md).

Each run fetches every ref of each source into a private staging namespace of the mirror,
compares the result with the mirror's last accepted copy, and applies it in one ref transaction
only when it looks sane: the source returned refs, the base repository still has its main
branch, and no more than the allowed share of the accepted refs disappeared. A fetch that fails
any check leaves the accepted copy untouched and makes the run fail, so the systemd unit shows
as failed and the journal records why.

Layout of the mirror repository:

  refs/...                          the base repository's refs, exactly as Forgejo has them
  refs/forks/<owner>/...            every ref of <owner>'s fork (Forgejo allows one per owner)
  refs/tauceti-mirror/staging/...   raw fetch results, not yet accepted (never restore these)

Uses only the standard library and git.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import gzip
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

STAGING = "refs/tauceti-mirror/staging/"
FORKS = "refs/forks/"
# Namespaces in the mirror that never belong to the base repository's own refs.
PRIVATE = (STAGING, FORKS, "refs/tauceti-mirror/")


def reserved(name: str) -> bool:
    """A ref in, or at the root of, one of the mirror's own namespaces."""
    return any(name == prefix.rstrip("/") or name.startswith(prefix) for prefix in PRIVATE)


EXIT_OK, EXIT_REFUSED, EXIT_SETUP, EXIT_BUSY = 0, 1, 2, 75


def log(level: str, event: str, **fields) -> None:
    """One JSON line per event. The <N> prefix sets the journal priority under systemd."""
    priority = {"error": 3, "warning": 4, "info": 6}[level]
    stream = sys.stderr if priority <= 4 else sys.stdout
    stream.write(f"<{priority}>" + json.dumps({"level": level, "event": event, **fields}, sort_keys=True) + "\n")
    stream.flush()


class MirrorError(Exception):
    pass


@dataclass
class Source:
    name: str  # "base" or "fork-<owner>"
    url: str
    live_prefix: str  # "refs/" for the base, "refs/forks/<owner>/" for a fork

    @property
    def staging_prefix(self) -> str:
        return f"{STAGING}{self.name}/"


def git(repo: Path, *args: str, env: dict | None = None, timeout: float = 600, input: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=input,
    )
    if proc.returncode != 0:
        raise MirrorError(f"git {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[-1500:]}")
    return proc.stdout


def auth_env(base_url: str, token_file: Path | None, user: str) -> dict:
    """Environment for git: no prompts, no user credential helpers, optional token as an HTTP header.

    The token travels in GIT_CONFIG_* variables, never in argv, so it does not appear in process
    listings or the journal.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    pairs = [("credential.helper", "")]
    if token_file is not None:
        token = token_file.read_text().strip()
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        pairs.append((f"http.{base_url.rstrip('/')}/.extraHeader", f"Authorization: Basic {basic}"))
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def list_refs(repo: Path, prefix: str) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", prefix)
    refs = {}
    for line in out.splitlines():
        sha, name = line.split(" ", 1)
        refs[name] = sha
    return refs


def live_refs(repo: Path, source: Source) -> dict[str, str]:
    refs = list_refs(repo, source.live_prefix)
    if source.live_prefix == "refs/":
        refs = {name: sha for name, sha in refs.items() if not reserved(name)}
    return refs


def staged_refs(repo: Path, source: Source) -> dict[str, str]:
    """The staging copy, renamed to where it would live once accepted."""
    raw = list_refs(repo, source.staging_prefix)
    staged = {source.live_prefix + name[len(source.staging_prefix) :]: sha for name, sha in raw.items()}
    if source.live_prefix == "refs/":
        # The base repository may not write into the mirror's own namespaces.
        staged = {name: sha for name, sha in staged.items() if not reserved(name)}
    return staged


def ensure_repo(repo: Path) -> None:
    if not (repo / "HEAD").exists():
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(repo)], check=True)
    # Keep every ref update in a reflog, and never let automatic maintenance drop objects or
    # reflog entries: a deleted ref's commits stay available to the daily snapshots.
    for key, value in (
        ("core.logAllRefUpdates", "always"),
        ("gc.auto", "0"),
        ("gc.pruneExpire", "never"),
        ("gc.reflogExpire", "never"),
        ("gc.reflogExpireUnreachable", "never"),
        ("fetch.writeCommitGraph", "false"),
    ):
        git(repo, "config", key, value)


def fetch(repo: Path, source: Source, env: dict, timeout: float) -> None:
    git(
        repo,
        "fetch",
        "--quiet",
        "--prune",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-auto-gc",
        source.url,
        f"+refs/*:{source.staging_prefix}*",
        env=env,
        timeout=timeout,
    )


def assess(source: Source, live: dict[str, str], staged: dict[str, str], max_loss_percent: float, loss_allowance: int):
    """Return (ok, details). Refuses empty results, a missing base main, and large losses."""
    lost = sorted(set(live) - set(staged))
    details = {
        "source": source.name,
        "live": len(live),
        "staged": len(staged),
        "lost": len(lost),
        "added": len(set(staged) - set(live)),
        "changed": sum(1 for name in set(live) & set(staged) if live[name] != staged[name]),
    }
    if not staged:
        return False, {**details, "reason": "the fetch returned no refs"}
    if source.live_prefix == "refs/" and "refs/heads/main" not in staged:
        return False, {**details, "reason": "the base repository has no refs/heads/main"}
    allowed = max(math.floor(len(live) * max_loss_percent / 100), loss_allowance)
    details["loss_allowed"] = allowed
    if len(lost) > allowed:
        return False, {
            **details,
            "reason": f"{len(lost)} accepted refs would disappear (allowed {allowed})",
            "lost_sample": lost[:20],
        }
    return True, details


def apply(repo: Path, live: dict[str, str], staged: dict[str, str], backup: Path) -> None:
    """Move the accepted copy to the staged one in a single all-or-nothing ref transaction.

    If a second transaction is unavoidable, the refs the first one deletes are written to
    `backup` beforehand, and the file is removed once both succeed or the first is undone. A file
    left by an interrupted run is replayed by recover() before the next fetch is assessed.
    """
    deletes = [f"delete {name} {sha}" for name, sha in sorted(live.items()) if name not in staged]
    updates = [
        f"update {name} {sha} {live.get(name, '0' * len(sha))}"
        for name, sha in sorted(staged.items())
        if live.get(name) != sha
    ]
    if not deletes and not updates:
        return
    try:
        transaction(repo, deletes + updates)
        return
    except MirrorError:
        # The files backend cannot delete refs/heads/a and create refs/heads/a/b (or the reverse)
        # in one transaction. Only then: delete the conflicting refs first, apply the rest in a
        # second transaction, and put the conflicting refs back if that fails.
        conflicts = {
            name: sha
            for name, sha in live.items()
            if name not in staged
            and any(other.startswith(name + "/") or name.startswith(other + "/") for other in staged)
        }
        if not conflicts:
            raise
    backup.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(backup, "".join(f"{sha} {name}\n" for name, sha in sorted(conflicts.items())))
    transaction(repo, [f"delete {name} {sha}" for name, sha in sorted(conflicts.items())])
    try:
        transaction(repo, [line for line in deletes if line.split(" ")[1] not in conflicts] + updates)
    except BaseException:
        recover(repo, backup)
        raise
    backup.unlink()


def recover(repo: Path, backup: Path) -> int:
    """Recreate the refs an interrupted two-step apply deleted, so the loss baseline stays intact.

    A ref is skipped when it, or a ref it conflicts with, exists: then the second transaction had
    committed and the deletion was accepted. Returns the number of refs recreated.
    """
    if not backup.exists():
        return 0
    existing = set(list_refs(repo, "refs/"))
    restored = 0
    for line in backup.read_text().splitlines():
        sha, name = line.split(" ", 1)
        if any(other == name or other.startswith(name + "/") or name.startswith(other + "/") for other in existing):
            continue
        transaction(repo, [f"create {name} {sha}"])
        restored += 1
    backup.unlink()
    return restored


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with open(partial, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def transaction(repo: Path, lines: list[str]) -> None:
    git(repo, "update-ref", "--stdin", input="\n".join(["start", *lines, "prepare", "commit"]) + "\n")


def api_get(url: str, token_file: Path | None, timeout: float = 30):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token_file is not None:
        request.add_header("Authorization", f"token {token_file.read_text().strip()}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def discover_forks(base_url: str, repository: str, token_file: Path | None) -> list[str]:
    """Full names (owner/name) of the repository's forks; a fork may have a different name."""
    owners = []
    page = 1
    while True:
        url = f"{base_url.rstrip('/')}/api/v1/repos/{repository}/forks?limit=50&page={page}"
        batch = api_get(url, token_file)
        owners += [item["full_name"] for item in batch]
        if len(batch) < 50:
            return owners
        page += 1


def valid_owner(owner: str) -> bool:
    return (
        "/" not in owner
        and subprocess.run(["git", "check-ref-format", f"{FORKS}{owner}/x"], capture_output=True).returncode == 0
    )


def read_health(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, prefix=f".{path.name}.") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, path)


def snapshot(repo: Path, snapshots: Path, keep_days: int, now: dt.datetime) -> Path | None:
    """Write today's list of accepted refs once a day, compact the repository, drop old snapshots."""
    snapshots.mkdir(parents=True, exist_ok=True)
    target = snapshots / f"refs-{now:%Y-%m-%d}.txt.gz"
    written = None
    if not target.exists():
        refs = {name: sha for name, sha in list_refs(repo, "refs/").items() if not name.startswith(STAGING)}
        body = "".join(f"{sha} {name}\n" for name, sha in sorted(refs.items()))
        partial = target.with_name(target.name + ".partial")
        with gzip.open(partial, "wt") as handle:
            handle.write(body)
        os.replace(partial, target)
        written = target
        # Once a day: pack loose objects. gc.pruneExpire=never keeps unreachable objects, so the
        # commits a snapshot names stay in the repository.
        git(repo, "gc", "--quiet", timeout=7200)
    cutoff = (now - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for old in snapshots.glob("refs-*.txt.gz"):
        if old.name[len("refs-") : len("refs-") + 10] < cutoff:
            old.unlink()
    return written


def run(args) -> int:
    repo = Path(args.repo).expanduser()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else repo.parent
    health_path = state_dir / "health.json"
    ensure_repo(repo)
    lock = open(repo / "tauceti-mirror.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("warning", "busy", repo=str(repo))
        return EXIT_BUSY

    base_url = args.base_url.rstrip("/")
    token_file = Path(args.token_file).expanduser() if args.token_file else None
    env = auth_env(base_url, token_file, args.user)
    health = read_health(health_path)
    now = dt.datetime.now(dt.UTC)

    name = args.repository.split("/", 1)[1]
    # A bare owner means a fork with the base repository's name.
    # Precedence for an owner: --fork, then the API's current answer, then what earlier runs found
    # (a renamed fork is followed; one the API no longer lists stays mirrored).
    discovered = []
    if args.discover_forks:
        try:
            discovered = discover_forks(base_url, args.repository, token_file)
        except Exception as error:  # the API being down must not stop the git mirror
            log("warning", "fork-discovery-failed", error=str(error)[:500], known=health.get("forks", []))
    owners = list(
        dict.fromkeys(f if "/" in f else f"{f}/{name}" for f in args.fork + discovered + health.get("forks", []))
    )
    by_owner: dict[str, str] = {}
    for full_name in owners:
        by_owner.setdefault(full_name.split("/", 1)[0], full_name)
    owners = list(by_owner.values())
    sources = [Source("base", f"{base_url}/{args.repository}.git", "refs/")]
    for full_name in owners:
        owner = full_name.split("/", 1)[0]
        if not valid_owner(owner):
            log("warning", "fork-skipped", fork=full_name, reason="owner is not a valid ref component")
            continue
        sources.append(Source(f"fork-{owner}", f"{base_url}/{full_name}.git", f"{FORKS}{owner}/"))

    status = EXIT_OK
    results = dict(health.get("sources", {}))
    for source in sources:
        started = time.monotonic()
        record = dict(results.get(source.name, {}))
        record["last_attempt"] = now.isoformat()
        try:
            backup = state_dir / f"apply-backup-{source.name}.txt"
            recovered = recover(repo, backup)
            if recovered:
                log("warning", "recovered-interrupted-apply", source=source.name, refs=recovered)
            fetch(repo, source, env, args.fetch_timeout)
            live = live_refs(repo, source)
            staged = staged_refs(repo, source)
            ok, details = assess(source, live, staged, args.max_loss_percent, args.loss_allowance)
            if not ok and args.accept_loss and details["staged"]:
                log("warning", "loss-accepted-by-operator", **details)
                ok = True
            details["seconds"] = round(time.monotonic() - started, 1)
            if ok:
                apply(repo, live, staged, backup)
                record.update(last_success=now.isoformat(), refs=details["staged"], last_error=None)
                log("info", "mirrored", **details)
            else:
                status = EXIT_REFUSED
                record["last_error"] = details["reason"]
                log("error", "refused", **details)
        except (MirrorError, subprocess.TimeoutExpired, OSError) as error:
            status = EXIT_REFUSED
            record["last_error"] = str(error)[:1500]
            log("error", "fetch-failed", source=source.name, error=str(error)[:1500])
        results[source.name] = record

    try:
        written = snapshot(repo, state_dir / "snapshots", args.snapshot_days, now)
        if written:
            log("info", "snapshot", path=str(written))
    except (MirrorError, subprocess.TimeoutExpired, OSError) as error:
        status = EXIT_REFUSED
        log("error", "snapshot-failed", error=str(error)[:1500])

    write_json_atomic(
        health_path,
        {
            "repository": args.repository,
            "forks": owners,
            "sources": results,
            "last_run": now.isoformat(),
            "exit": status,
        },
    )
    return status


def check(args) -> int:
    """Exit nonzero when any known source has not been mirrored within --max-age-minutes."""
    repo = Path(args.repo).expanduser()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else repo.parent
    health = read_health(state_dir / "health.json")
    now = dt.datetime.now(dt.UTC)
    stale = {}
    for name, record in health.get("sources", {}).items():
        success = record.get("last_success")
        age = None if success is None else (now - dt.datetime.fromisoformat(success)).total_seconds() / 60
        if age is None or age > args.max_age_minutes:
            stale[name] = {"minutes": None if age is None else round(age), "last_error": record.get("last_error")}
    if not health.get("sources") or "base" not in health["sources"]:
        stale["base"] = {"minutes": None, "last_error": "no recorded run"}
    for backup in state_dir.glob("apply-backup-*.txt"):
        stale[backup.name] = {"minutes": None, "last_error": "an interrupted apply left this list of accepted refs"}
    print(json.dumps({"stale": stale, "health": health}, indent=1, sort_keys=True))
    return EXIT_REFUSED if stale else EXIT_OK


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--repo", default="~/.local/share/tauceti-forgejo-mirror/TauCeti.git", help="mirror bare repository"
    )
    parser.add_argument("--state-dir", help="health.json and snapshots/ (default: the mirror's parent directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="fetch every source once and accept sane results")
    fetch_parser.add_argument("--base-url", default="https://git.taucetiproject.org")
    fetch_parser.add_argument("--repository", default="TauCetiProject/TauCeti")
    fetch_parser.add_argument(
        "--fork", action="append", default=[], metavar="OWNER[/NAME]", help="always mirror this fork"
    )
    fetch_parser.add_argument("--no-fork-discovery", dest="discover_forks", action="store_false")
    fetch_parser.add_argument("--token-file", help="read-only Forgejo token (default: anonymous)")
    fetch_parser.add_argument("--user", default="tauceti-mirror-reader", help="user name sent with --token-file")
    fetch_parser.add_argument("--max-loss-percent", type=float, default=10.0)
    fetch_parser.add_argument(
        "--loss-allowance", type=int, default=10, help="refs any source may lose regardless of size"
    )
    fetch_parser.add_argument("--accept-loss", action="store_true", help="operator override: accept a large loss once")
    fetch_parser.add_argument("--fetch-timeout", type=float, default=3600)
    fetch_parser.add_argument("--snapshot-days", type=int, default=60)
    check_parser = sub.add_parser("check", help="report health; fail when a source is stale")
    check_parser.add_argument("--max-age-minutes", type=float, default=30)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    try:
        return run(args) if args.command == "fetch" else check(args)
    except (MirrorError, OSError, subprocess.SubprocessError) as error:
        log("error", "setup-failed", error=str(error)[:1500])
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
