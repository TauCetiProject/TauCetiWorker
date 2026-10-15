"""Durable GitHub publication intents, replayed only when rejection or reconciliation permits it.

Opaque commands with partial effects are retained for inspection, never blindly repeated.
Private bodies/files live here, separately from the allow-listed accounting ledger.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
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


def semantic_key(payload):
    args = list(payload["argv"])
    for index in payload.get("assets", {}):
        i = int(index)
        prefix = args[i].split("=@", 1)[0] + "=@" if "=@" in args[i] else ""
        args[i] = prefix + "<retained-asset:" + index + ">"
    repository = _flag(args, "--repo", "-R")
    if not repository:
        repository, _ = target(payload)
    cwd = "repository:" + repository if repository else payload.get("cwd")
    key = {"argv": args, "cwd": cwd, "assets": payload.get("assets", {}), "input": payload.get("input")}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def intent_paths(env=None):
    env = os.environ if env is None else env
    legacy = tr.root() / "outbox" / (tr.scope(env) or "unselected") / env.get("TAUCETI_WORKER_ID", "unknown")
    return set(directory(env).glob("*.json")) | set(legacy.glob("*.json"))


def prepare(argv, env, cwd=None, input=None):
    if env.get("TAUCETI_OUTBOX_REPLAY") == "1" or tr.proxy_readonly(argv[1:]) or local_operation(argv[1:]):
        return None
    args = list(argv[1:])
    assets = {}
    asset_fields = {}
    # Snapshot transient body/input files before the paid round cleans its working directory.
    for i, arg in enumerate(args[:-1]):
        if arg in ("--body-file", "--input") or (arg == "-F" and args[:2] == ["pr", "create"]):
            source = Path(args[i + 1])
            if not source.is_absolute():
                source = Path(cwd or os.getcwd()) / source
            if source.is_file():
                assets[str(i + 1)] = source.read_text()
        if arg in ("-F", "--field") and "=@" in args[i + 1]:
            field, source_name = args[i + 1].split("=@", 1)
            source = Path(source_name)
            if not source.is_absolute():
                source = Path(cwd or os.getcwd()) / source
            if source.is_file():
                assets[str(i + 1)] = source.read_text()
                asset_fields[str(i + 1)] = field + "=@"
    context = {}
    context["repository"] = _flag(args, "--repo", "-R")
    context["pr"] = env.get("TAUCETI_GITHUB_PR") or None
    input_file = _flag(args, "--input")
    payload_input = (
        input
        if input_file == "-"
        else next((assets[str(i)] for i, arg in enumerate(args) if arg == input_file and str(i) in assets), None)
    )
    try:
        fields = json.loads(payload_input) if payload_input else {}
    except (ValueError, TypeError):
        fields = {}
    if isinstance(fields, dict):
        context.update({k: fields[k] for k in ("body", "title") if isinstance(fields.get(k), str)})
    body = _flag(args, "--body", "-b")
    for i, arg in enumerate(args):
        if arg.startswith("body="):
            body = assets.get(str(i), arg[5:])
    body_file = _flag(args, "--body-file", "-F") if args[:1] == ["pr"] else None
    if body_file:
        body = next((assets[str(i)] for i, arg in enumerate(args) if arg == body_file and str(i) in assets), body)
        if body_file == "-":
            body = input
    if body == "@-":
        body = input
    if isinstance(body, str):
        context["body"] = body
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
        "asset_fields": asset_fields,
        "input": input,
        "context": context,
    }
    key = semantic_key(payload)
    for previous_path in intent_paths(env):
        try:
            if semantic_key(json.loads(previous_path.read_text())) == key:
                return previous_path
        except (OSError, ValueError, KeyError, TypeError):
            continue
    path = directory(env) / (key + ".json")
    with tr.locked(path.with_suffix(".lock")):
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("status") not in ("complete",):
                return path
        tr.atomic(path, {**payload, "created": time.time(), "status": "prepared"})
    return path


def may_send(path):
    with tr.locked(path.with_suffix(".lock")):
        return json.loads(path.read_text()).get("status") in ("prepared", "rejected")


def retry_at(path):
    with tr.locked(path.with_suffix(".lock")):
        return max(time.time() + 1, json.loads(path.read_text()).get("retry_at") or time.time() + 60)


def sending(path):
    with tr.locked(path.with_suffix(".lock")):
        value = json.loads(path.read_text())
        if value.get("status") not in ("prepared", "rejected"):
            from .config import GitHubUnavailable

            raise GitHubUnavailable(
                "GitHub publication already pending; retaining its result for reconciliation",
                retry_at=value.get("retry_at") or time.time() + 60,
            )
        tr.atomic(path, {**value, "status": "sending", "updated": time.time()})


def target(value):
    args = value["argv"]
    context = value.get("context", {})
    repository = context.get("repository") or _flag(args, "--repo", "-R")
    number = context.get("pr")
    for arg in args:
        match = re.search(r"(?:^/?repos/|https://github.com/)([^/]+/[^/]+)/(?:issues|pulls?)/(\d+)", arg)
        if match:
            return match[1], int(match[2])
    if args[:1] == ["pr"] and len(args) > 2 and args[2].isdigit():
        number = args[2]
    return (repository, int(number)) if repository and str(number).isdigit() else (None, None)


def pending_for(repo, number, env=None):
    for path in intent_paths(env):
        try:
            value = json.loads(path.read_text())
            if target(value) == (repo, number):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


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
    writes = [
        r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS") and r.get("graphql_operation") != "read"
    ]
    logical_quota_rejection = rejected_logically and all(
        isinstance(e, dict) and (e.get("type") or e.get("extensions", {}).get("code")) in ("RATE_LIMIT", "RATE_LIMITED")
        for e in errors
    )
    definitive = (
        before_request or logical_quota_rejection or (writes and all(r.get("status") in (403, 429) for r in writes))
    )
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


def actor(env):
    if env.get("TAUCETI_GITHUB_ACCOUNT"):
        return env["TAUCETI_GITHUB_ACCOUNT"].lower()

    def fetch():
        result = tr.execute(
            ["gh", "api", "graphql", "-f", "query=query{viewer{login}}"], env={**env, "TAUCETI_OUTBOX_REPLAY": "1"}
        )
        if result.returncode:
            return None
        try:
            login = json.loads(result.stdout)["data"]["viewer"]["login"]
            return login.lower() if isinstance(login, str) and login else None
        except (ValueError, KeyError, TypeError):
            return None

    return tr.shared_read(["publication-actor"], fetch, ttl=3600, identity=tr.scope(env), immutable=True)


def same_body(left, right):
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.replace("\r\n", "\n").strip() == right.replace("\r\n", "\n").strip()
    )


def reconcile(value, env):
    args = value["argv"]
    replay = {**env, "TAUCETI_OUTBOX_REPLAY": "1"}
    repo, number = target(value)
    endpoint = None
    if repo and number and args[:2] in (["pr", "comment"], ["pr", "review"]):
        endpoint = f"repos/{repo}/" + (
            f"pulls/{number}/reviews" if args[1] == "review" else f"issues/{number}/comments"
        )
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
        repository_target = context.get("repository")
        head = context.get("head")
        if not repository_target or not head or ":" not in head:
            return None
        owner, name = repository_target.split("/", 1)
        who, branch = head.split(":", 1)
        q = "query($owner:String!,$name:String!,$head:String!){repository(owner:$owner,name:$name){pullRequests(headRefName:$head,first:100,states:[OPEN,CLOSED,MERGED]){nodes{url createdAt headRepositoryOwner{login}} pageInfo{hasNextPage}}}}"
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
                and (
                    not value.get("created")
                    or datetime.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).timestamp()
                    >= int(value["created"]) - 300
                )
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
    issue_creation = endpoint and re.fullmatch(r"/?repos/[^/]+/[^/]+/issues", endpoint)
    if issue_creation:
        body = value.get("context", {}).get("body")
        if body is None:
            # Older receipts retained input files without extracting their JSON fields.
            contents = value.get("input") or next(iter(value.get("assets", {}).values()), "{}")
            try:
                body = json.loads(contents).get("body")
            except (ValueError, TypeError, AttributeError):
                pass
        body = body or next((a[5:] for a in args if a.startswith("body=")), None)
        who = actor(env)
        if not isinstance(body, str) or not who:
            return None
        from urllib.parse import urlencode

        since = datetime.datetime.fromtimestamp(value.get("created", 0) - 300, datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        listing = endpoint + "?" + urlencode({"state": "all", "creator": who, "since": since, "per_page": 100})
        result = tr.execute(["gh", "api", listing, "--paginate", "--slurp"], env=replay)
        if result.returncode:
            return None
        try:
            pages = json.loads(result.stdout)
            return any(
                not c.get("pull_request")
                and same_body(c.get("body"), body)
                and (c.get("user") or {}).get("login", "").lower() == who
                and datetime.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
                >= value.get("created", 0) - 300
                for page in pages
                for c in page
            )
        except (ValueError, TypeError, AttributeError, KeyError):
            return None
    if endpoint and re.fullmatch(
        r"/?repos/[^/]+/[^/]+/(?:issues|pulls)/\d+/(?:comments(?:/\d+/replies)?|reviews)", endpoint
    ):
        body = value.get("context", {}).get("body", next((a[5:] for a in args if a.startswith("body=")), None))
        if body is None and value.get("input"):
            try:
                body = json.loads(value["input"]).get("body")
            except (ValueError, TypeError):
                pass
        if not isinstance(body, str):
            return None
        reply = re.search(r"/comments/(\d+)/replies$", endpoint)
        listing = re.sub(r"/comments/\d+/replies$", "/comments", endpoint)
        who = actor(env)
        if not who:
            return None
        r = tr.execute(["gh", "api", listing + "?per_page=100", "--paginate", "--slurp"], env=replay)
        if r.returncode:
            return None
        try:
            pages = json.loads(r.stdout)

            def recent(c):
                stamp = c.get("created_at") or c.get("submitted_at")
                if not stamp:
                    return not value.get("created")  # legacy receipts cannot assert timing
                when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                # GitHub timestamps have second precision and the local clock
                # can be ahead. Allow a small skew when acknowledging a body.
                return when >= int(value.get("created", 0)) - 300

            return any(
                same_body(c.get("body"), body)
                and (c.get("user") or {}).get("login", "").lower() == who
                and (not reply or str(c.get("in_reply_to_id")) == reply[1])
                and recent(c)
                for page in pages
                for c in page
            )
        except (ValueError, TypeError, AttributeError, KeyError):
            return None
    return None


def drain(env=None):
    env = dict(os.environ if env is None else env)
    remaining = []
    paths = intent_paths(env)

    def creation_order(path):
        try:
            return json.loads(path.read_text()).get("created", 0), str(path)
        except (OSError, ValueError, AttributeError):
            return 0, str(path)

    blocked_targets = set()
    for path in sorted(paths, key=creation_order):
        with tr.locked(path.with_suffix(".lock")):
            try:
                value = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                remaining.append(path.stem)
                continue
            destination = target(value)
            if value.get("retry_at", 0) > time.time():
                remaining.append(path.stem)
                blocked_targets.add(destination)
                continue
            if destination != (None, None) and destination in blocked_targets:
                remaining.append(path.stem)
                continue
            if local_operation(value["argv"]) or tr.proxy_readonly(value["argv"]):
                # Recover entries created by older proxies. Read-only queries
                # and local checkouts cannot have an ambiguous publication.
                archive(path, value, "Read-only or local operation is not a GitHub publication")
                continue
            allowed = value.get("status") in ("rejected", "prepared")
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
                    value["retry_at"] = time.time() + 60
                    tr.atomic(path, value)
                    remaining.append(path.stem)
                    blocked_targets.add(destination)
                else:
                    tr.event({"kind": "publication_optional_pending", "intent": path.stem})
                continue
            args = list(value["argv"])
            if not Path(value["cwd"]).is_dir():
                archive(path, value, "Publication checkout no longer exists; retained for manual recovery")
                continue
            assets = []
            for index, body in value.get("assets", {}).items():
                asset = path.with_name(path.stem + "-" + index + ".body")
                fd = os.open(asset, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(body)
                args[int(index)] = value.get("asset_fields", {}).get(index, "") + str(asset)
                assets.append(asset)
            try:
                value["status"] = "sending"
                tr.atomic(path, value)
                r = tr.execute(
                    ["gh", *args], env={**env, "TAUCETI_OUTBOX_REPLAY": "1"}, cwd=value["cwd"], input=value.get("input")
                )
            except (OSError, subprocess.SubprocessError):
                value["status"] = "ambiguous"
                tr.atomic(path, value)
                remaining.append(path.stem)
                continue
            finally:
                for asset in assets:
                    asset.unlink(missing_ok=True)
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
                blocked_targets.add(destination)
    return remaining


def main():
    """Local recovery controls; never publish merely because an operator lists intents."""
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or release retained GitHub publications")
    parser.add_argument("action", choices=("list", "abandon", "retry"))
    parser.add_argument("--intent", type=Path)
    parser.add_argument("--note")
    parser.add_argument("--acknowledge-duplicate-risk", action="store_true")
    args = parser.parse_args()
    base = (tr.root() / "outbox").resolve()
    if args.action == "list":
        entries = []
        for path in base.glob("*/*/*.json"):
            try:
                value = json.loads(path.read_text())
                repo, number = target(value)
                entries.append(
                    {
                        "path": str(path),
                        "status": value.get("status"),
                        "created": value.get("created"),
                        "repository": repo,
                        "pr": number,
                    }
                )
            except (OSError, ValueError, KeyError, TypeError):
                entries.append({"path": str(path), "status": "unreadable"})
        print(json.dumps(entries, indent=2))
        return
    if not args.intent or not args.intent.resolve().is_relative_to(base) or not args.note:
        parser.error("--intent must name a retained file beneath the local outbox, and --note is required")
    if args.action == "retry" and not args.acknowledge_duplicate_risk:
        parser.error("retry requires --acknowledge-duplicate-risk; an earlier ambiguous write may already have landed")
    with tr.locked(args.intent.with_suffix(".lock")):
        value = json.loads(args.intent.read_text())
        if args.action == "abandon":
            archive(args.intent, value, "Operator abandoned publication", note=args.note)
        else:
            tr.atomic(args.intent, {**value, "status": "rejected", "retry_at": 0, "operator_note": args.note})
            tr.event({"kind": "publication_manual_retry", "intent": args.intent.stem})


if __name__ == "__main__":
    main()
