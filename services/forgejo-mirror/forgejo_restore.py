#!/usr/bin/env python3
"""Restore the refs of a Forgejo repository that lost them (docs/forgejo-restore.md).

  check     list the target's refs, report empty or broken ones, and print the root commands
            that clear them on the server
  restore   plan and (with --execute) push: main from GitHub as the main sync account, then every
            other ref from the off-host mirror (services/forgejo-mirror), falling back to the
            fleet's shared repositories for open PR branches the mirror lacks. A ref that exists
            on the target is never changed. Without --execute nothing is pushed.
  verify    compare the target's refs and open PR heads with a saved plan
  pull-refs report open PRs whose head the base repository does not serve (no matching
            refs/pull/N/head); with --publish, push each fork PR's head to
            refs/tauceti-fork-heads/N so the base repository serves it again

Uses only the standard library and git. Run it where the mirror is (tauchonki).
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ZERO = "0" * 40
DEFAULT_FLEET_GLOB = "~/.local/share/tauceti-fleet/worker-runtimes/*/lib/python3*/site-packages/tauceti_worker/host-repositories/TauCeti-*.git"
# Forgejo writes a fork PR's refs/pull/N/head only when the PR is opened or its fork branch is
# pushed, and refuses that ref from anyone else. After a restore the base repository therefore
# lacks these heads' commits, so fetching them, inline review comments and the queue's merge base
# fail. A plain ref here makes the exact head commit reachable again (pull-refs --publish).
FORK_HEADS = "refs/tauceti-fork-heads/"
# Never pushed: Forgejo derives refs/pull/* itself, pull-refs derives the fork heads from the
# forks' current branches, and the rest are the mirror's own namespaces.
NEVER_PUSH = ("refs/pull/", FORK_HEADS, "refs/forks/", "refs/tauceti-mirror/")
TRANSIENT = (
    " 50",
    "HTTP 5",
    "error: 5",
    "RPC failed",
    "timed out",
    "Connection reset",
    "early EOF",
    "unexpected disconnect",
    "Could not resolve host",
    "Failed to connect",
)


class RestoreError(Exception):
    pass


def say(message: str = "") -> None:
    print(message, flush=True)


def git(repo, *args, env=None, timeout=600, check=True):
    cmd = ["git"] + (["-C", str(repo)] if repo is not None else []) + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise RestoreError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip()[-1500:]}")
    return proc


def git_env(url_prefix: str | None = None, user: str | None = None, token_file: Path | None = None) -> dict:
    """No prompts or credential helpers; a token, if any, travels as an HTTP header in the environment."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    for name in ("GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(name, None)
    pairs = [("credential.helper", "")]
    if token_file is not None:
        basic = base64.b64encode(f"{user}:{token_file.read_text().strip()}".encode()).decode()
        pairs.append((f"http.{url_prefix.rstrip('/')}/.extraHeader", f"Authorization: Basic {basic}"))
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def ls_remote(url: str, env: dict, *patterns: str, attempts: int = 4) -> dict[str, str]:
    for attempt in range(attempts):
        proc = git(None, "ls-remote", url, *patterns, env=env, timeout=300, check=False)
        if proc.returncode == 0:
            refs = {}
            for line in proc.stdout.splitlines():
                sha, _, name = line.partition("\t")
                if name and not name.endswith("^{}"):
                    refs[name] = sha
            return refs
        if attempt + 1 < attempts:
            time.sleep(10 * (attempt + 1))
    raise RestoreError(f"git ls-remote {url} failed: {proc.stderr.strip()[-800:]}")


def api_get(url: str, token_file: Path | None = None):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token_file is not None:
        request.add_header("Authorization", f"token {token_file.read_text().strip()}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def open_pulls(base_url: str, repository: str, token_file: Path | None = None) -> list[dict]:
    pulls, page = [], 1
    while True:
        batch = api_get(f"{base_url}/api/v1/repos/{repository}/pulls?state=open&limit=50&page={page}", token_file)
        pulls += batch
        if len(batch) < 50:
            return pulls
        page += 1


def token_login(base_url: str, token_file: Path) -> str | None:
    """The account a token belongs to, or None when the token may not read its own user."""
    try:
        return api_get(f"{base_url}/api/v1/user", token_file).get("login")
    except urllib.error.HTTPError as error:
        if error.code == 403:
            return None
        raise


@dataclass
class Target:
    base_url: str
    repository: str
    fork: str | None  # OWNER or OWNER/NAME when restoring a fork from refs/forks/<owner>/

    @property
    def owner(self) -> str | None:
        return self.fork.split("/", 1)[0] if self.fork else None

    @property
    def full_name(self) -> str:
        if not self.fork:
            return self.repository
        return self.fork if "/" in self.fork else f"{self.fork}/{self.repository.split('/', 1)[1]}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.full_name}.git"

    @property
    def mirror_prefix(self) -> str:
        return f"refs/forks/{self.owner}/" if self.fork else "refs/"


def target_from(args) -> Target:
    return Target(args.base_url.rstrip("/"), args.repository, getattr(args, "fork", None))


# --- check -----------------------------------------------------------------------------------


def broken_refs(remote: dict[str, str]) -> list[str]:
    return sorted(name for name, sha in remote.items() if sha == ZERO or len(sha) not in (40, 64))


def root_commands(server_path: str, broken: list[str]) -> list[str]:
    """One subshell that stops at the first failure, so nothing runs outside the repository."""
    lines = [
        "( set -e",
        f"  cd -- {shlex.quote(server_path)}",
        "  test -f HEAD",
        "  test -d refs",
        "  ls -l packed-refs || true",
        "  find refs -type f -empty -print",
    ]
    if broken:
        lines.append("  rm -f -- " + " ".join(shlex.quote(name) for name in broken))
    lines += [
        "  find refs -type f -empty -delete",
        "  # An empty packed-refs holds nothing; move it aside so git does not read it.",
        '  if test -e packed-refs && ! test -s packed-refs; then mv packed-refs "packed-refs.empty-$(date +%Y%m%d%H%M)"; fi',
        "  git for-each-ref --count=5",
        "  git fsck --connectivity-only 2>&1 | tail -5",
        ")",
    ]
    return lines


def cmd_check(args) -> int:
    target = target_from(args)
    remote = ls_remote(target.url, git_env())
    broken = broken_refs(remote)
    heads = sum(1 for name in remote if name.startswith("refs/heads/"))
    pulls = sum(1 for name in remote if name.startswith("refs/pull/"))
    has_main = remote.get("refs/heads/main") not in (None, ZERO)
    say(
        f"{target.url}: {len(remote)} refs, {heads} branches, {pulls} refs/pull, main {'present' if has_main else 'MISSING'}"
    )
    for name in broken[:50]:
        say(f"  broken: {remote[name]} {name}")
    if len(broken) > 50:
        say(f"  ... and {len(broken) - 50} more")
    healthy = has_main and not broken
    if not healthy:
        say()
        say("Forgejo refuses pushes while broken refs exist. As root (or the Forgejo service user) on the")
        say("server, paste this block; it stops at the first failure:")
        say()
        server_path = args.server_path or f"/var/lib/forgejo/repositories/{target.full_name.lower()}.git"
        for line in root_commands(server_path, broken):
            say(f"  {line}")
        say()
        say("Then rerun check; once it reports no broken refs, run restore.")
    return 0 if healthy else 1


# --- plan ------------------------------------------------------------------------------------


@dataclass
class Item:
    ref: str
    sha: str
    source: str  # "github", "mirror", "fleet"
    pr: int | None = None
    note: str = ""
    status: str = "planned"  # planned, present, exists-different, pushed, failed, missing-object
    detail: dict = field(default_factory=dict)


def fleet_repositories(pattern: str, target: Target) -> list[Path]:
    wanted = target.url.removesuffix(".git").rstrip("/")
    found = []
    for path in sorted(glob.glob(os.path.expanduser(pattern))):
        url = git(path, "config", "--get", "remote.origin.url", check=False).stdout.strip()
        if url.removesuffix(".git").rstrip("/") == wanted:
            found.append(Path(path))
    return found


def make_staging(mirror: Path, fleet: list[Path]) -> Path:
    """A private repository that can read the mirror's and the fleet's objects without writing to them."""
    stage = Path(tempfile.mkdtemp(prefix="forgejo-restore.")) / "stage.git"
    git(None, "init", "-q", "--bare", str(stage))
    # Alternates are resolved relative to the staging object directory, so write absolute paths.
    alternates = [
        str((Path(repo) / "objects").resolve()) for repo in [mirror, *fleet] if (Path(repo) / "objects").is_dir()
    ]
    (stage / "objects" / "info" / "alternates").write_text("".join(f"{path}\n" for path in alternates))
    return stage


def present_objects(stage: Path, shas: set[str]) -> set[str]:
    if not shas:
        return set()
    proc = subprocess.run(
        ["git", "-C", str(stage), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input="".join(f"{sha}\n" for sha in sorted(shas)),
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.split()[0] for line in proc.stdout.splitlines() if not line.endswith("missing")}


def is_ancestor(stage: Path, older: str, newer: str) -> bool:
    return git(stage, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def committer_time(stage: Path, sha: str) -> int:
    return int(git(stage, "show", "-s", "--format=%ct", sha).stdout.strip() or 0)


def choose(stage: Path, candidates: dict[str, str]) -> tuple[str, str]:
    """Pick one SHA from {label: sha}: the descendant of all others, else the newest commit."""
    distinct = sorted(set(candidates.values()))
    if len(distinct) == 1:
        return distinct[0], "+".join(sorted(candidates))
    for sha in distinct:
        if all(other == sha or is_ancestor(stage, other, sha) for other in distinct):
            label = [name for name, value in candidates.items() if value == sha]
            return sha, f"descendant ({'+'.join(sorted(label))})"
    newest = max(distinct, key=lambda sha: committer_time(stage, sha))
    return newest, "DIVERGED, newest commit chosen: " + ", ".join(
        f"{k}={v[:12]}" for k, v in sorted(candidates.items())
    )


def plan_refs(args, target: Target, stage: Path, fleet: list[Path]) -> tuple[list[Item], list[str]]:
    mirror = Path(args.mirror).expanduser()
    warnings: list[str] = []
    items: dict[str, Item] = {}
    excluded = tuple(args.exclude)

    if not target.owner and not args.skip_main and not "refs/heads/main".startswith(excluded):
        github = ls_remote(args.github_url, git_env(), "refs/heads/main")
        sha = github.get("refs/heads/main")
        if not sha:
            raise RestoreError(f"{args.github_url} has no refs/heads/main")
        git(
            stage,
            "fetch",
            "--quiet",
            "--no-tags",
            args.github_url,
            "+refs/heads/main:refs/restore/github-main",
            env=git_env(),
        )
        fetched = git(stage, "rev-parse", "refs/restore/github-main").stdout.strip()
        if fetched != sha:
            raise RestoreError(f"GitHub main moved while fetching ({sha} -> {fetched}); rerun")
        items["refs/heads/main"] = Item("refs/heads/main", sha, "github")

    if mirror.is_dir():
        out = git(mirror, "for-each-ref", "--format=%(objectname) %(refname)", target.mirror_prefix).stdout
        for line in out.splitlines():
            sha, name = line.split(" ", 1)
            ref = "refs/" + name[len(target.mirror_prefix) :]
            if not target.owner and name.startswith(NEVER_PUSH):
                continue
            if ref.startswith(("refs/pull/",)) or ref.startswith(excluded) or ref in items:
                continue
            if ref == "refs/heads/main" and (not target.owner or args.skip_main):
                continue  # base main comes from GitHub, pushed by the main sync account
            items[ref] = Item(ref, sha, "mirror")
    else:
        warnings.append(f"no mirror at {mirror}; using only the fleet's repositories")

    if not target.owner and not args.no_fallback:
        try:
            pulls = open_pulls(target.base_url, target.repository, None)
        except (OSError, ValueError) as error:
            pulls = []
            warnings.append(f"could not list open PRs, so no fallback: {error}")
        base_owner = target.repository.split("/", 1)[0].lower()
        for pull in pulls:
            head = pull.get("head") or {}
            branch = head.get("ref") or ""
            repo = head.get("repo") or {}
            owner = ((repo.get("owner") or {}).get("login") or "").lower()
            ref = f"refs/heads/{branch}"
            if not branch or ref in items or ref.startswith(excluded) or ref == "refs/heads/main":
                continue  # main only ever comes from GitHub
            if owner and owner != base_owner:
                continue  # the head lives in a fork; the fork's own refs are untouched by a base-repo loss
            candidates = {}
            for index, repo_path in enumerate(fleet):
                for label, name in (
                    ("published", f"refs/tauceti/published/push/{branch}"),
                    ("prscan", f"refs/remotes/prscan/{pull['number']}"),
                    ("origin", f"refs/remotes/origin/{branch}"),
                ):
                    proc = git(repo_path, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", check=False)
                    if proc.returncode == 0:
                        candidates[f"{label}{index or ''}"] = proc.stdout.strip()
            if not candidates:
                warnings.append(f"PR #{pull['number']} ({branch}): no copy in the mirror or the fleet")
                continue
            sha, note = choose(stage, candidates)
            items[ref] = Item(ref, sha, "fleet", pr=pull["number"], note=note)

    available = present_objects(stage, {item.sha for item in items.values()})
    for item in items.values():
        if item.sha not in available:
            item.status = "missing-object"
    return sorted(items.values(), key=lambda item: (item.ref != "refs/heads/main", item.ref)), warnings


def classify(items: list[Item], remote: dict[str, str]) -> None:
    for item in items:
        if item.status in ("missing-object", "pushed"):
            continue
        current = remote.get(item.ref)
        if current is None:
            item.status = "planned"
        elif current == item.sha:
            item.status = "present"
        else:
            item.status = "exists-different"
            item.detail = {"remote": current}


def summary(items: list[Item]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        key = f"{item.source}:{item.status}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# --- push ------------------------------------------------------------------------------------


def push(
    stage: Path, url: str, env: dict, batch: list[Item], timeout: float
) -> tuple[subprocess.CompletedProcess, dict]:
    """Push refs that must not exist yet: --force-with-lease=<ref>: makes git refuse an existing ref."""
    args = ["push", "--porcelain", "--no-verify"]
    args += [f"--force-with-lease={item.ref}:" for item in batch]
    args += [url] + [f"{item.sha}:{item.ref}" for item in batch]
    try:
        proc = git(stage, *args, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        # The server may have accepted some refs; the retry lists the target again first.
        return subprocess.CompletedProcess(args, 124, "", f"push timed out after {timeout:.0f}s"), {}
    flags = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and ":" in parts[1]:
            flags[parts[1].rsplit(":", 1)[1]] = (parts[0], parts[2] if len(parts) > 2 else "")
    return proc, flags


def push_all(args, stage: Path, url: str, env: dict, selected: list[Item], items: list[Item], results: Path) -> None:
    """Push the planned refs of `selected`; every checkpoint writes the whole plan, `items`."""
    pending = [item for item in selected if item.status == "planned"]
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        for attempt in range(args.attempts):
            classify(batch, ls_remote(url, env))
            send = [item for item in batch if item.status == "planned"]
            if not send:
                break
            proc, flags = push(stage, url, env, send, args.push_timeout)
            for item in send:
                flag, text = flags.get(item.ref, ("?", ""))
                if flag in ("*", " ", "+", "="):
                    item.status = "pushed"
                else:
                    item.status = "failed"
                    item.detail = {"flag": flag, "summary": text, "stderr": proc.stderr[-600:]}
            write_results(results, items)
            pushed = sum(1 for item in send if item.status == "pushed")
            say(
                f"batch {start // args.batch_size + 1}: attempt {attempt + 1}, {pushed}/{len(send)} pushed (exit {proc.returncode})"
            )
            transient = proc.returncode != 0 and any(marker in proc.stderr for marker in TRANSIENT)
            if proc.returncode == 0 or not transient:
                break
            for item in send:
                if item.status == "failed":
                    item.status = "planned"  # re-observed before the retry
            time.sleep(min(30 * 2**attempt, 600))
        for item in batch:
            if item.status == "planned":
                item.status = "failed"
        write_results(results, items)


def write_results(path: Path, items: list[Item]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps([asdict(item) for item in items], indent=1) + "\n")
    os.replace(tmp, path)


def verify_identity(target: Target, token_file: Path, expected: str) -> None:
    login = token_login(target.base_url, token_file)
    if login is None:
        say(f"note: {token_file} may not read its own user; trusting that it belongs to {expected}")
    elif login != expected:
        raise RestoreError(f"{token_file} belongs to {login}, not {expected}")


def cmd_restore(args) -> int:
    target = target_from(args)
    anonymous = git_env()
    remote = ls_remote(target.url, anonymous)
    broken = broken_refs(remote)
    if broken:
        say(f"{len(broken)} broken refs on {target.url}; Forgejo refuses pushes until they are removed. Run check.")
        return 1
    fleet = (
        []
        if target.owner or args.no_fallback
        else [Path(p) for p in args.fleet_repo] or fleet_repositories(args.fleet_glob, target)
    )
    mirror = Path(args.mirror).expanduser()
    stage = make_staging(mirror, fleet)
    try:
        return restore_from(args, target, remote, stage, fleet)
    finally:
        shutil.rmtree(stage.parent, ignore_errors=True)


def restore_from(args, target: Target, remote: dict[str, str], stage: Path, fleet: list[Path]) -> int:
    say(f"staging {stage} (reads objects from the mirror and {len(fleet)} fleet repositories)")
    items, warnings = plan_refs(args, target, stage, fleet)
    classify(items, remote)
    for warning in warnings:
        say(f"warning: {warning}")
    for item in items:
        if item.status in ("exists-different", "missing-object") or item.note.startswith("DIVERGED"):
            say(f"  {item.status}: {item.ref} {item.sha[:12]} {item.note} {item.detail or ''}".rstrip())
    results = Path(args.results).expanduser()
    write_results(results, items)
    say(f"plan: {summary(items)}; written to {results}")
    if not args.execute:
        say("dry run: nothing pushed. Rerun with --execute to push the planned refs.")
        return 0

    # Base main belongs to the main sync account; a fork's main is an ordinary ref of the fork.
    is_main = (lambda item: item.ref == "refs/heads/main") if not target.owner else (lambda item: False)
    main = [item for item in items if is_main(item) and item.status == "planned"]
    rest = [item for item in items if not is_main(item)]
    if main:
        verify_identity(target, Path(args.main_token_file).expanduser(), args.main_user)
    if any(item.status == "planned" for item in rest):
        verify_identity(target, Path(args.token_file).expanduser(), args.user)
    if main:
        env = git_env(target.base_url, args.main_user, Path(args.main_token_file).expanduser())
        push_all(args, stage, target.url, env, main, items, results)
        write_results(results, items)
        if main[0].status not in ("pushed", "present"):
            say(f"main was not pushed: {main[0].detail}")
            return 1
        say(f"main pushed as {args.main_user}: {main[0].sha}")
    env = git_env(target.base_url, args.user, Path(args.token_file).expanduser())
    push_all(args, stage, target.url, env, rest, items, results)
    write_results(results, items)
    say(f"pushed: {summary(items)}")
    return verify(args, target, items)


# --- verify ----------------------------------------------------------------------------------


def verify(args, target: Target, items: list[Item]) -> int:
    remote = ls_remote(target.url, git_env())
    wanted = [item for item in items if item.status in ("pushed", "present", "planned", "failed")]
    missing = [item.ref for item in wanted if remote.get(item.ref) != item.sha]
    say(f"ls-remote: {len(wanted) - len(missing)}/{len(wanted)} planned refs match; {len(remote)} refs in total")
    for ref in missing[:30]:
        say(f"  mismatch: {ref} remote={remote.get(ref)}")
    unrecoverable = [item for item in items if item.status == "missing-object"]
    for item in unrecoverable[:30]:
        say(f"  not restored, object missing: {item.ref} {item.sha}")
    kept = [item for item in items if item.status == "exists-different"]
    moved = [item for item in kept if remote.get(item.ref) != item.detail.get("remote")]
    if kept:
        say(f"left as found (the target had another SHA): {len(kept)} refs, {len(moved)} of them changed since")
    for item in moved[:30]:
        say(f"  left-as-found ref changed: {item.ref} was {item.detail.get('remote')}, now {remote.get(item.ref)}")
    problems = len(missing) + len(unrecoverable) + len(moved)
    if not target.owner:
        by_ref = {item.ref: item for item in items}
        try:
            pulls = open_pulls(target.base_url, target.repository, None)
        except (OSError, ValueError) as error:
            say(f"error: could not list open PRs, so their heads are unverified: {error}")
            return 1
        base_owner = target.repository.split("/", 1)[0].lower()
        checked = wrong = lagging = unplanned = 0
        for pull in pulls:
            head = pull.get("head") or {}
            owner = (((head.get("repo") or {}).get("owner") or {}).get("login") or "").lower()
            if owner and owner != base_owner:
                continue
            item = by_ref.get(f"refs/heads/{head.get('ref')}")
            if item is None:
                unplanned += 1
                if unplanned <= 30:
                    say(
                        f"  PR #{pull['number']}: branch {head.get('ref')} was not restored (no copy found or excluded)"
                    )
                continue
            if item.status == "missing-object":
                continue  # already counted
            # A ref left as found must show the SHA it was found at.
            expected = item.detail.get("remote") if item.status == "exists-different" else item.sha
            checked += 1
            pull_head = remote.get(f"refs/pull/{pull['number']}/head")
            if head.get("sha") != expected:
                wrong += 1
                if wrong <= 30:
                    say(f"  PR #{pull['number']}: api head {head.get('sha')}, expected {expected}")
            elif pull_head != expected:
                lagging += 1
        say(f"open PRs: {checked - wrong}/{checked} base-repository PRs show the expected head in the API")
        if lagging:
            say(f"  {lagging} of them have no matching refs/pull/N/head yet; Forgejo rewrites it after a push")
        if unplanned:
            say(f"open PRs without a restored branch: {unplanned}")
        forks = pull_ref_report(target, pulls, remote)["missing_fork"]
        if forks:
            say(
                f"open fork PRs whose head the base repository does not serve: {len(forks)} "
                f"(#{', #'.join(str(pull['number']) for pull in forks[:30])})"
            )
            say("  Forgejo rewrites their refs/pull/N/head only on a push to the fork branch; run pull-refs --publish")
        problems += wrong + unplanned + len(forks)
    return 0 if problems == 0 else 1


def pull_ref_report(target: Target, pulls: list[dict], remote: dict[str, str]) -> dict[str, list]:
    """Sort open PRs by how the base repository serves their head commit.

    `served`: refs/pull/N/head is the head. `fork_heads`: only refs/tauceti-fork-heads/N is.
    `missing`: neither, split by whether the head is in a fork (publishable) or in the base
    repository (Forgejo rewrites the pull ref on the next push of its branch). `stale`: fork-head
    refs no longer needed, because their PR closed or its pull ref is back.
    """
    base_owner = target.repository.split("/", 1)[0].lower()
    report = {"served": [], "fork_heads": [], "missing_fork": [], "missing_base": [], "stale": []}
    numbers = set()
    for pull in pulls:
        number, head = pull["number"], pull.get("head") or {}
        numbers.add(number)
        sha = head.get("sha")
        extra = remote.get(f"{FORK_HEADS}{number}")
        if remote.get(f"refs/pull/{number}/head") == sha:
            report["served"].append(pull)
            if extra is not None:
                report["stale"].append(f"{FORK_HEADS}{number}")
            continue
        if extra == sha:
            report["fork_heads"].append(pull)
            continue
        owner = (((head.get("repo") or {}).get("owner") or {}).get("login") or "").lower()
        report["missing_fork" if owner and owner != base_owner else "missing_base"].append(pull)
    for name in sorted(remote):
        suffix = name.removeprefix(FORK_HEADS)
        if name.startswith(FORK_HEADS) and not (suffix.isdigit() and int(suffix) in numbers):
            report["stale"].append(name)
    return report


def publish_fork_heads(args, target: Target, pulls: list[dict], remote: dict[str, str]) -> list[dict]:
    """Push each fork PR's exact head to refs/tauceti-fork-heads/N; returns the PRs left unserved."""
    token_file = Path(args.token_file).expanduser()
    verify_identity(target, token_file, args.user)
    env = git_env(target.base_url, args.user, token_file)
    stage = make_staging(Path(args.mirror).expanduser(), [])
    failed = []
    try:
        for pull in pulls:
            number, head = pull["number"], pull["head"]
            ref, sha = f"{FORK_HEADS}{number}", head["sha"]
            source = f"{target.base_url}/{head['repo']['full_name']}.git"
            fetched = git(stage, "fetch", "--no-tags", source, f"refs/heads/{head['ref']}", env=git_env(), check=False)
            tip = git(stage, "rev-parse", "FETCH_HEAD", check=False).stdout.strip() if fetched.returncode == 0 else ""
            if tip != sha:
                say(f"  PR #{number}: {head['repo']['full_name']} {head['ref']} is at {tip or 'unreadable'}, not {sha}")
                failed.append(pull)
                continue
            proc = git(
                stage,
                "push",
                "--porcelain",
                "--no-verify",
                f"--force-with-lease={ref}:{remote.get(ref, '')}",
                target.url,
                f"{sha}:{ref}",
                env=env,
                check=False,
            )
            if proc.returncode:
                say(f"  PR #{number}: push of {ref} failed: {proc.stderr.strip()[-300:]}")
                failed.append(pull)
            else:
                say(f"  PR #{number}: published {sha} as {ref}")
    finally:
        shutil.rmtree(stage.parent, ignore_errors=True)
    return failed


def prune_fork_heads(args, target: Target, stale: list[str], remote: dict[str, str]) -> int:
    token_file = Path(args.token_file).expanduser()
    verify_identity(target, token_file, args.user)
    leases = [f"--force-with-lease={ref}:{remote[ref]}" for ref in stale]
    # A deletion needs no objects, but git push still needs a repository to run in.
    stage = make_staging(Path(args.mirror).expanduser(), [])
    try:
        proc = git(
            stage,
            "push",
            "--porcelain",
            "--no-verify",
            *leases,
            target.url,
            *(f":{ref}" for ref in stale),
            env=git_env(target.base_url, args.user, token_file),
            check=False,
        )
    finally:
        shutil.rmtree(stage.parent, ignore_errors=True)
    say(
        f"pruned {len(stale)} fork-head refs" if proc.returncode == 0 else f"prune failed: {proc.stderr.strip()[-300:]}"
    )
    return proc.returncode


def cmd_pull_refs(args) -> int:
    target = target_from(args)
    if target.owner:
        raise RestoreError("pull-refs checks the base repository's open PRs; drop --fork")
    pulls = open_pulls(target.base_url, target.repository, None)
    remote = ls_remote(target.url, git_env(), "refs/pull/*", FORK_HEADS + "*")
    report = pull_ref_report(target, pulls, remote)
    say(
        f"open PRs: {len(pulls)}; head served by refs/pull/N/head {len(report['served'])}, "
        f"by {FORK_HEADS}N {len(report['fork_heads'])}, by neither {len(report['missing_fork']) + len(report['missing_base'])}"
    )
    for kind, pull in [("fork", p) for p in report["missing_fork"]] + [("base", p) for p in report["missing_base"]]:
        number, head = pull["number"], pull.get("head") or {}
        actual = remote.get(f"refs/pull/{number}/head", "missing")
        say(
            f"  PR #{number} ({kind} {(head.get('repo') or {}).get('full_name')}): head {head.get('sha')}, refs/pull/{number}/head {actual}"
        )
    unserved = report["missing_fork"] + report["missing_base"]
    if report["missing_base"]:
        say("  base-repository PRs get their pull ref back when their branch is next pushed")
    if args.publish and report["missing_fork"]:
        unserved = publish_fork_heads(args, target, report["missing_fork"], remote) + report["missing_base"]
    elif report["missing_fork"]:
        say(f"  rerun with --publish to push the fork heads to {FORK_HEADS}N")
    if report["stale"]:
        say(f"no longer needed: {len(report['stale'])} fork-head refs ({', '.join(report['stale'][:10])})")
        if args.prune and prune_fork_heads(args, target, report["stale"], remote):
            return 1
    return 1 if unserved else 0


def cmd_verify(args) -> int:
    target = target_from(args)
    data = json.loads(Path(args.results).expanduser().read_text())
    items = [Item(**entry) for entry in data]
    return verify(args, target, items)


# --- arguments -------------------------------------------------------------------------------


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--base-url", default="https://git.taucetiproject.org")
    parser.add_argument("--repository", default="TauCetiProject/TauCeti")
    parser.add_argument(
        "--fork", metavar="OWNER[/NAME]", help="restore this fork (default name: the base's) from refs/forks/OWNER/"
    )
    parser.add_argument("--results", default="~/.local/share/tauceti-forgejo-mirror/restore-results.json")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="report broken refs and print the root commands that clear them")
    check.add_argument(
        "--server-path",
        help="repository directory on the server (default: /var/lib/forgejo/repositories/<owner>/<name>.git)",
    )
    restore = sub.add_parser("restore", help="plan, and with --execute push, the missing refs")
    restore.add_argument("--execute", action="store_true", help="push (default: dry run)")
    restore.add_argument("--mirror", default="~/.local/share/tauceti-forgejo-mirror/TauCeti.git")
    restore.add_argument("--fleet-glob", default=DEFAULT_FLEET_GLOB)
    restore.add_argument(
        "--fleet-repo", action="append", default=[], help="use this repository instead of --fleet-glob"
    )
    restore.add_argument("--no-fallback", action="store_true", help="use the mirror only")
    restore.add_argument("--github-url", default="https://github.com/TauCetiProject/TauCeti")
    restore.add_argument("--skip-main", action="store_true", help="leave refs/heads/main alone")
    restore.add_argument(
        "--exclude", action="append", default=[], metavar="PREFIX", help="skip refs starting with PREFIX"
    )
    restore.add_argument("--main-user", default="tauceti-main-sync")
    restore.add_argument("--main-token-file", default="~/.config/tauceti-forgejo/main-sync-token")
    restore.add_argument("--user", default="kim-em")
    restore.add_argument("--token-file", default="~/.config/tauceti-forgejo/worker-token")
    restore.add_argument("--batch-size", type=int, default=50)
    restore.add_argument("--attempts", type=int, default=5)
    restore.add_argument("--push-timeout", type=float, default=1800)
    sub.add_parser("verify", help="compare the target with the saved plan (--results)")
    pull_refs = sub.add_parser("pull-refs", help="report open PRs whose head the base repository does not serve")
    pull_refs.add_argument("--publish", action="store_true", help=f"push missing fork PR heads to {FORK_HEADS}N")
    pull_refs.add_argument("--prune", action="store_true", help="delete fork-head refs no open PR needs any more")
    pull_refs.add_argument("--mirror", default="~/.local/share/tauceti-forgejo-mirror/TauCeti.git")
    pull_refs.add_argument("--user", default="tauceti-ai-for-science")
    pull_refs.add_argument("--token-file", default="~/.config/tauceti-forgejo/bot-token")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    handler = {"check": cmd_check, "restore": cmd_restore, "verify": cmd_verify, "pull-refs": cmd_pull_refs}[
        args.command
    ]
    try:
        return handler(args)
    except (RestoreError, OSError, subprocess.SubprocessError, ValueError) as error:
        say(f"error: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
