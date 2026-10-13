"""Durable GitHub publication intents, replayed only when rejection or reconciliation permits it.

Opaque commands with partial effects are retained for inspection, never blindly repeated.
Private bodies/files live here, separately from the allow-listed accounting ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import github_transport as tr


def directory(env=None):
    env = os.environ if env is None else env
    return (
        tr.root() / "outbox" / (tr.quota_scope(tr.scope(env)) or "unselected") / env.get("TAUCETI_WORKER_ID", "unknown")
    )


def _flag(args, *flags):
    for i, arg in enumerate(args):
        if arg in flags and i + 1 < len(args):
            return args[i + 1]
        for flag in flags:
            if arg.startswith(flag + "="):
                return arg[len(flag) + 1 :]
    return None


def local_operation(args):
    """Commands that modify a local checkout but publish nothing to GitHub."""
    return args[:2] in (["pr", "checkout"], ["repo", "clone"])


def prepare(argv, env, cwd=None, input=None):
    if env.get("TAUCETI_OUTBOX_REPLAY") == "1" or tr.proxy_readonly(argv[1:]) or local_operation(argv[1:]):
        return None
    args = list(argv[1:])
    assets = {}
    # Snapshot transient body/input files before the paid round cleans its working directory.
    for i, arg in enumerate(args[:-1]):
        if arg in ("--body-file", "--input") or (arg == "-F" and args[:2] == ["pr", "create"]):
            source = Path(args[i + 1])
            if not source.is_absolute():
                source = Path(cwd or os.getcwd()) / source
            if source.is_file():
                assets[str(i + 1)] = source.read_text()
    context = {}
    if args[:2] == ["pr", "create"]:
        context["repository"] = _flag(args, "--repo", "-R")
        context["head"] = _flag(args, "--head", "-H")
        if not context["head"]:
            git_dir = Path(cwd or os.getcwd()) / ".git"
            try:
                if git_dir.is_file():
                    git_dir = (git_dir.parent / git_dir.read_text().strip().removeprefix("gitdir: ")).resolve()
                head = git_dir.joinpath("HEAD").read_text().strip()
                branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else ""
            except OSError:
                branch = ""
            owner = env.get("TAUCETI_GITHUB_ACCOUNT")
            if branch and branch != "HEAD" and owner:
                context["head"] = owner + ":" + branch
                args += ["--head", context["head"]]
    payload = {
        "version": 1,
        "argv": args,
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "assets": assets,
        "input": input,
        "context": context,
    }
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    path = directory(env) / (key + ".json")
    with tr.locked(path.with_suffix(".lock")):
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("status") not in ("complete",):
                return path
        tr.atomic(path, {**payload, "created": time.time(), "status": "running"})
    return path


def finish(path, result, records=None, *, before_request=False):
    if path is None:
        return
    with tr.locked(path.with_suffix(".lock")):
        value = json.loads(path.read_text())
        _finish_locked(path, value, result, records, before_request=before_request)


def _finish_locked(path, value, result, records=None, *, before_request=False):
    """Classify an initial attempt or replay while its intent lock is held."""
    if result.returncode == 0:
        path.unlink(missing_ok=True)
        return
    records = records or []
    text = (result.stderr or "") + (result.stdout or "")
    quota = any(r.get("remaining") == 0 or r.get("status") == 429 for r in records) or any(
        s in text.lower() for s in ("rate limit", "retry-after", "quota", "creation cap")
    )
    # GraphQL permission/validation failures use HTTP 200. Treat a wholly
    # rejected mutation as terminal; do not park the worker as if its
    # acknowledgement were lost. Partial data remains ambiguous.
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        payload = {}
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    data = payload.get("data") if isinstance(payload, dict) else None
    rejected_logically = bool(errors) and (
        data is None or isinstance(data, dict) and all(v is None for v in data.values())
    )
    terminal_types = {"FORBIDDEN", "UNAUTHORIZED", "INSUFFICIENT_SCOPES", "NOT_FOUND", "GRAPHQL_VALIDATION_FAILED"}
    terminal = rejected_logically and all(
        isinstance(e, dict) and (e.get("type") or e.get("extensions", {}).get("code")) in terminal_types for e in errors
    )
    if terminal and not quota:
        archive(
            path,
            value,
            "GraphQL rejected the mutation",
            error_types=[e.get("type") or e.get("extensions", {}).get("code") for e in errors],
        )
        return
    if records and all(400 <= r.get("status", 0) < 500 for r in records) and not quota:
        archive(path, value, "GitHub rejected the publication", http_statuses=[r["status"] for r in records])
        return
    writes = [r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS")]
    definitive = before_request or (writes and all(r.get("status") in (403, 429) for r in writes))
    value["status"] = "rejected" if definitive else "ambiguous"
    value["updated"] = time.time()
    tr.atomic(path, value)
    tr.event({"kind": "publication_deferred", "status": value["status"], "intent": path.stem})
    result.github_deferred = True
    result.stderr = (
        result.stderr or ""
    ) + "\nTauCeti: publication retained for retry/reconciliation; no paid work needs to be repeated.\n"


def archive(path, value, reason, **evidence):
    """Preserve failed intent evidence privately without blocking unrelated paid work."""
    value = {**value, "status": "permanent_failure", "updated": time.time(), "reason": reason, **evidence}
    tr.atomic(path.with_suffix(".failed"), value)
    path.unlink(missing_ok=True)
    tr.event({"kind": "publication_failed", "intent": path.stem, "reason": reason})


def resolution(args):
    if args[:2] != ["api", "graphql"]:
        return None
    query = next((a[6:] for a in args if a.startswith("query=")), "")
    # Match only the reviewed single-operation protocol, not arbitrary
    # mutations with additional effects or another requested state.
    canonical = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    if re.sub(r"\s+", "", query) != canonical:
        return None
    return next((a[3:] for a in args if a.startswith("id=")), None)


def reconcile(value, env):
    args = value["argv"]
    replay = {**env, "TAUCETI_OUTBOX_REPLAY": "1"}
    thread_id = resolution(args)
    if thread_id:
        query = "query($id:ID!){node(id:$id){... on PullRequestReviewThread{isResolved viewerCanResolve}}}"
        result = tr.execute(["gh", "api", "graphql", "-f", "query=" + query, "-F", "id=" + thread_id], env=replay)
        if result.returncode:
            return None
        try:
            node = json.loads(result.stdout)["data"]["node"]
            if node is None:
                return "permanent_failure"  # deleted target
            if node.get("isResolved") is True:
                return True
            if node.get("viewerCanResolve") is False:
                return "permanent_failure"
            if node.get("isResolved") is False and node.get("viewerCanResolve") is True:
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        return None
    if args[:2] == ["pr", "create"]:
        context = value.get("context", {})
        target = context.get("repository")
        head = context.get("head")
        if not target or not head or ":" not in head:
            return None
        owner, name = target.split("/", 1)
        who, branch = head.split(":", 1)
        q = "query($owner:String!,$name:String!,$head:String!){repository(owner:$owner,name:$name){pullRequests(headRefName:$head,first:100,states:[OPEN,CLOSED,MERGED]){nodes{url headRepositoryOwner{login}} pageInfo{hasNextPage}}}}"
        result = tr.execute(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=" + q,
                "-F",
                "owner=" + owner,
                "-F",
                "name=" + name,
                "-F",
                "head=" + branch,
            ],
            env=replay,
        )
        if result.returncode:
            return None
        try:
            connection = json.loads(result.stdout)["data"]["repository"]["pullRequests"]
            matches = [
                n
                for n in connection["nodes"]
                if (n.get("headRepositoryOwner") or {}).get("login", "").lower() == who.lower()
            ]
            if matches:
                return True
            if connection["pageInfo"]["hasNextPage"]:
                return None
            return False
        except (ValueError, KeyError, TypeError):
            return None
    if args[:1] == ["api"]:
        endpoint = next((a for a in args[1:] if re.match(r"^/?repos/[^/]+/[^/]+/", a)), None)
        method = (
            _flag(args, "-X", "--method") or ("POST" if any(a in ("-f", "-F", "--input") for a in args) else "GET")
        ).upper()
        if method in ("PATCH", "PUT", "DELETE"):
            return False  # HTTP-idempotent updates, no duplicate creation
        if endpoint and endpoint.endswith("/reactions"):
            return False  # GitHub uniqueness is (actor, content)
        if endpoint and re.fullmatch(r"/?repos/[^/]+/[^/]+/(issues|pulls)/\d+/comments", endpoint):
            body = next((a[5:] for a in args if a.startswith("body=")), None)
            if body is None and value.get("input"):
                try:
                    body = json.loads(value["input"]).get("body")
                except (ValueError, TypeError):
                    pass
            if not isinstance(body, str):
                return None
            r = tr.execute(["gh", "api", endpoint + "?per_page=100", "--paginate", "--slurp"], env=replay)
            if r.returncode:
                return None
            try:
                pages = json.loads(r.stdout)
                actor = env.get("TAUCETI_GITHUB_ACCOUNT", "").lower()
                if not actor:
                    return None
                return any(
                    c.get("body") == body and (c.get("user") or {}).get("login", "").lower() == actor
                    for page in pages
                    for c in page
                )
            except (ValueError, TypeError):
                return None
    return None


def drain(env=None):
    env = dict(os.environ if env is None else env)
    remaining = []
    legacy = tr.root() / "outbox" / (tr.scope(env) or "unselected") / env.get("TAUCETI_WORKER_ID", "unknown")
    paths = set(directory(env).glob("*.json")) | set(legacy.glob("*.json"))
    for path in sorted(paths):
        with tr.locked(path.with_suffix(".lock")):
            try:
                value = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                remaining.append(path.stem)
                continue
            if local_operation(value["argv"]):
                # Recover entries created by older proxies. Local checkout
                # failure belongs to the normal round retry path; it cannot
                # have an ambiguous remote publication to reconcile.
                archive(path, value, "Local checkout operation is not a GitHub publication")
                continue
            allowed = value.get("status") == "rejected"
            optional_resolution = bool(resolution(value["argv"]))
            if not allowed or optional_resolution:
                applied = reconcile(value, env)
                if applied is True:
                    path.unlink()
                    continue
                if applied == "permanent_failure":
                    archive(path, value, "Review-thread resolution unavailable for this account or target")
                    continue
                allowed = applied is False
            if not allowed:
                # Thread resolution is a best-effort follow-up after the paid
                # review was posted. Preserve it for later reconciliation,
                # while allowing this worker to select unrelated useful work.
                if not optional_resolution:
                    remaining.append(path.stem)
                else:
                    tr.event({"kind": "publication_optional_pending", "intent": path.stem})
                continue
            args = list(value["argv"])
            for index, body in value.get("assets", {}).items():
                asset = path.with_name(path.stem + "-" + index + ".body")
                fd = os.open(asset, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(body)
                args[int(index)] = str(asset)
            r = tr.execute(
                ["gh", *args], env={**env, "TAUCETI_OUTBOX_REPLAY": "1"}, cwd=value["cwd"], input=value.get("input")
            )
            # A retry can itself be denied or lose its acknowledgement. Use
            # the same classification as the original attempt, so an uncertain
            # creation is reconciled before another retry and permanent denials
            # release the worker instead of becoming an endless retry loop.
            _finish_locked(
                path,
                value,
                r,
                getattr(r, "github_records", []),
                before_request=getattr(r, "github_before_request", False),
            )
            if path.exists() and not optional_resolution:
                remaining.append(path.stem)
    return remaining
