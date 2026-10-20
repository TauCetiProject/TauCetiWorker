"""Shared GitHub reads and allow-listed HTTP accounting, including gh subprocesses.

No tokens, query strings, request/response bodies or raw debug traces enter accounting.
Cache entries are credential-scoped; fresh checks bypass them, and writes invalidate them.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

_fresh = contextvars.ContextVar("github_fresh", default=False)
_direct = contextvars.ContextVar("github_direct", default=False)
_revision = contextvars.ContextVar("github_revision", default="")
_cache_hit = contextvars.ContextVar("github_cache_hit", default=False)
_request_context = contextvars.ContextVar("github_request_context", default=None)


def root() -> Path:
    return Path(
        os.environ.get("TAUCETI_GITHUB_SHARED_DIR", Path(__file__).resolve().parent.parent / "state/github-shared")
    )


def scope(env=None) -> str | None:
    env = os.environ if env is None else env
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    # A login is not a permission boundary: two tokens of one account can see different repos.
    if not token:
        return None
    return hashlib.sha256((env.get("GH_HOST", "github.com") + "\0" + token).encode()).hexdigest()


def quota_scope(identity):
    if not identity:
        return None
    try:
        return json.loads((root() / "identities" / (identity + ".json")).read_text())["quota_scope"]
    except (OSError, ValueError, KeyError):
        return identity


def register_principal(user_id: int, login: str, env=None):
    """User primary quotas are shared by tokens; cache permissions remain credential-scoped."""
    identity = scope(env)
    if not identity:
        return
    primary = hashlib.sha256(("github.com:user:" + str(user_id)).encode()).hexdigest()
    try:
        atomic(root() / "identities" / (identity + ".json"), {"quota_scope": primary, "principal": login})
    except OSError:
        return  # Verified authentication does not depend on the optional local cache.
    for path in (root() / "quota" / identity).glob("*.json"):
        try:
            record = json.loads(path.read_text())
            if path.stem != "secondary":
                remember_quota({**record, "resource": path.stem}, primary)
        except (OSError, ValueError):
            continue


@contextlib.contextmanager
def read_context(*, fresh: bool = False, revision: str = "", direct: bool = False):
    a = _fresh.set(_fresh.get() or fresh)
    b = _revision.set(revision or _revision.get())
    c = _cache_hit.set(False)
    d = _direct.set(_direct.get() or direct)
    try:
        yield
    finally:
        _fresh.reset(a)
        _revision.reset(b)
        _cache_hit.reset(c)
        _direct.reset(d)


def direct_reads():
    return _direct.get()


def cache_hit():
    return _cache_hit.get()


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 30):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("shared GitHub lock wait expired") from None
                time.sleep(0.05)
        yield


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def generation() -> str:
    try:
        return (root() / "generation").read_text()
    except OSError:
        return ""


def invalidate():
    """Invalidate every credential's view, including peers with different permissions."""
    try:
        atomic(root() / "generation", uuid.uuid4().hex)
    except OSError:
        pass


def event(value: dict):
    try:
        record = {
            "time": time.time(),
            "worker": os.environ.get("TAUCETI_WORKER_ID", "unknown"),
            "phase": os.environ.get("TAUCETI_GITHUB_PHASE", "survey"),
            **(_request_context.get() or {}),
            **value,
        }
        with locked(root() / "accounting.lock", timeout=0.05):
            path = root() / "requests.jsonl"
            # Keep bounded evidence on long-running hosts; reports cover retained records only.
            if path.exists() and path.stat().st_size > 64 * 1024 * 1024:
                path.replace(root() / "requests.previous.jsonl")
            with open(path, "a", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
                f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def shared_read(key, fetch, *, ttl: float = 30, identity=None, immutable: bool = False):
    """Coalesce a complete logical read under a per-key lock. Never cache failures.

    Network/parser exceptions propagate; unavailable storage merely disables caching. Generation
    checks prevent an overlapping write from publishing a pre-write result after invalidation.
    """
    identity = identity or scope()
    if not identity or _fresh.get() or os.environ.get("TAUCETI_GITHUB_CACHE_BYPASS") == "1":
        return fetch()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    path = root() / "cache" / identity / (digest + ".json")
    try:
        cm = locked(path.with_suffix(".lock"))
        cm.__enter__()
    except OSError:
        return fetch()
    try:
        epoch = generation()
        try:
            cached = json.loads(path.read_text())
            if (
                (immutable or cached["generation"] == epoch)
                and cached.get("revision", "") == _revision.get()
                and 0 <= time.time() - cached["time"] < ttl
            ):
                event({"kind": "cache_hit", "read": key[0]})
                _cache_hit.set(True)
                return cached["value"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        value = fetch()
        if value is not None and (immutable or generation() == epoch):
            try:
                atomic(path, {"time": time.time(), "generation": epoch, "revision": _revision.get(), "value": value})
            except OSError:
                pass
        return value
    finally:
        cm.__exit__(None, None, None)


def remember_quota(record, identity):
    resource = record.get("resource")
    identity = quota_scope(identity)
    if (
        not identity
        or not isinstance(resource, str)
        or not re.fullmatch(r"[a-z_-]+", resource)
        or "reset" not in record
        or "remaining" not in record
    ):
        return
    if not all(isinstance(record[k], int) and not isinstance(record[k], bool) for k in ("remaining", "reset")):
        return
    path = root() / "quota" / identity / (resource + ".json")
    try:
        with locked(path.with_suffix(".lock")):
            try:
                prior = json.loads(path.read_text())
            except (OSError, ValueError):
                prior = {}
            if not isinstance(prior, dict) or not isinstance(prior.get("reset", 0), int):
                prior = {}
            exhausted_until = prior.get("exhausted_until", 0)
            if not isinstance(exhausted_until, (int, float)):
                exhausted_until = 0
            if record["remaining"] == 0 and record["reset"] > time.time():
                exhausted_until = max(exhausted_until, record["reset"])
            if prior.get("reset", 0) > record["reset"]:
                # Different GitHub responses have reported contradictory reset windows. A
                # live zero reading still blocks admission until its own reset; a positive
                # response with a later reset cannot erase that concrete exhaustion.
                value = prior
            else:
                # Out-of-order responses from concurrent workers cannot resurrect an exhausted bucket.
                if prior.get("reset") == record["reset"]:
                    record = {
                        **record,
                        "remaining": min(prior.get("remaining", record["remaining"]), record["remaining"]),
                    }
                value = {"remaining": record["remaining"], "reset": record["reset"]}
            atomic(path, {**value, "exhausted_until": exhausted_until})
            if record.get("status") in (403, 429) and record["remaining"] == 0:
                record_block(resource, record["reset"], identity)
    except OSError:
        pass


def record_block(resource, reset, identity=None):
    identity = quota_scope(identity or scope())
    if not identity:
        return
    worker = hashlib.sha256(os.environ.get("TAUCETI_WORKER_ID", "unknown").encode()).hexdigest()
    try:
        atomic(
            root() / "blocked" / identity / (worker + ".json"),
            {"time": time.time(), "resource": resource, "reset": reset},
        )
    except OSError:
        pass


def blocking_event_since(start):
    identity = quota_scope(scope())
    if not identity:
        return None
    worker = hashlib.sha256(os.environ.get("TAUCETI_WORKER_ID", "unknown").encode()).hexdigest()
    try:
        r = json.loads((root() / "blocked" / identity / (worker + ".json")).read_text())
        # A failed stage can finish after the reset (engine cleanup, bounded waits, or
        # publishing). The request still failed due to quota; expiry must not turn that
        # infrastructure pause into a charged PR failure. Admission uses observed_budget.
        return r if r["time"] >= start else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def blocked_since(start):
    return blocking_event_since(start) is not None


def observed_budget(env=None):
    identity = quota_scope(scope(env))
    result = {}
    if identity:
        for path in (root() / "quota" / identity).glob("*.json"):
            resource = path.stem
            if resource == "secondary":
                continue
            try:
                r = json.loads((root() / "quota" / identity / (resource + ".json")).read_text())
                if time.time() < r.get("exhausted_until", 0):
                    result[resource] = (0, r["exhausted_until"])
                elif time.time() < r["reset"]:
                    result[resource] = (r["remaining"], r["reset"])
            except (OSError, ValueError, TypeError, KeyError):
                pass
    return result


def cooldown(env=None, *, until=None):
    identity = quota_scope(scope(env))
    if not identity:
        return 0
    path = root() / "quota" / identity / "secondary.json"
    try:
        with locked(path.with_suffix(".lock")):
            try:
                current = json.loads(path.read_text())["until"]
            except (OSError, ValueError, KeyError, TypeError):
                current = 0
            if until is not None:
                current = max(current, until)
                atomic(path, {"until": current})
            return max(0, current - time.time())
    except OSError:
        return 0


def parse_trace(stderr: str, *, identity=None, records=None) -> str:
    """Consume gh's private debug blocks; retain ordinary error text for existing retry logic."""
    clean, record, active = [], None, False
    body = ""

    def inspect_body(payload):
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("query"), str):
            record["graphql_operation"] = (
                "read" if proxy_readonly(["api", "graphql", "-f", "query=" + payload["query"]]) else "mutation"
            )
        data = payload.get("data") or {}
        limit = data.get("rateLimit") or {} if isinstance(data, dict) else {}
        charge = limit.get("cost") if isinstance(limit, dict) else None
        if isinstance(charge, int):
            record["graphql_cost"] = charge

    for line in stderr.splitlines(keepends=True):
        # gh pretty-prints both request and response JSON. Keep these bodies
        # private, bounded, and in memory; export only allow-listed metadata.
        if (
            active
            and record.get("endpoint") == "/graphql"
            and (body or line.lstrip().startswith("{"))
            and not line.startswith(("* ", "< ", "> "))
        ):
            body += line
            if not (line.rstrip() == "}" or line.lstrip().startswith("{") and line.rstrip().endswith("}")):
                if len(body) > 2_000_000:
                    body = ""
                continue
            try:
                inspect_body(json.loads(body))
                body = ""
            except ValueError:
                if len(body) > 2_000_000:
                    body = ""
            continue
        body = ""
        if line.startswith("* Request at "):
            active, record = True, {"kind": "http"}
        elif active and line.startswith("* Request to "):
            url = urlsplit(line[len("* Request to ") :].strip())
            path = url.path
            # Dynamic object IDs are grouped together. Query params can contain private search text.
            record["endpoint"] = re.sub(r"/[0-9]+(?=/|$)", "/{id}", path)
            record["host"] = url.hostname
        elif active and re.match(r"> [A-Z]+ /", line):
            record["method"] = line.split()[1]
        elif active and line.startswith("< HTTP/"):
            try:
                record["status"] = int(line.split()[2])
            except (ValueError, IndexError):
                pass
        elif active and line.lower().startswith("< x-ratelimit-"):
            name, _, value = line[2:].partition(":")
            field = name.lower().removeprefix("x-ratelimit-")
            if field in ("limit", "remaining", "reset", "used", "resource"):
                record[field] = int(value.strip()) if value.strip().isdigit() else value.strip()
        elif active and line.lower().startswith("< retry-after:"):
            record["retry_after"] = line.partition(":")[2].strip()
        elif active and line.startswith("* Request took "):
            duration = re.search(r"([0-9.]+)(ms|s)", line)
            if duration:
                record["latency_ms"] = float(duration[1]) * (1000 if duration[2] == "s" else 1)
            if record.get("endpoint") and record.get("method"):
                event(record)
                remember_quota(record, identity)
                if records is not None:
                    records.append(record)
            active, record = False, None
        elif not active:
            clean.append(line)
    # An interrupted request is still a request attempt, even without a response.
    if record and record.get("endpoint") and record.get("method"):
        event(record)
        if records is not None:
            records.append(record)
    return "".join(clean)


def execute(argv, *, env=None, wait: bool = False, **kwargs):
    context = _request_context.set({})
    effective = dict(os.environ if env is None else env)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tauceti_worker import github_outbox

    args = argv[1:]
    stdin_body = any(
        a in ("--input", "--body-file", "-F") and i + 1 < len(args) and args[i + 1] == "-" for i, a in enumerate(args)
    ) or any(a.endswith("=@-") for a in args)
    if stdin_body and kwargs.get("input") is None and not sys.stdin.isatty():
        kwargs["input"] = sys.stdin.read()
    intent = github_outbox.prepare(argv, effective, kwargs.get("cwd"), kwargs.get("input"))
    try:
        if intent and not github_outbox.may_send(intent):
            from tauceti_worker.config import GitHubUnavailable

            raise GitHubUnavailable(
                "GitHub publication already retained; reconciling before another attempt",
                retry_at=github_outbox.retry_at(intent),
            )
        result = _execute(argv, env=env, wait=wait, _intent=intent, **kwargs)
        if intent:
            # The HTTP ledger is deliberately separate from private intent bodies.
            github_outbox.finish(
                intent,
                result,
                getattr(result, "github_records", []),
                before_request=getattr(result, "github_before_request", False),
            )
        return result
    finally:
        _request_context.reset(context)


def _execute(argv, *, env=None, wait: bool = False, _intent=None, **kwargs):
    effective = dict(os.environ if env is None else env)
    effective["GH_DEBUG"] = "api"
    args = argv[1:]
    readonly = proxy_readonly(args)
    if not readonly:
        invalidate()
    if effective.get("TAUCETI_REAL_GH"):
        argv = [effective["TAUCETI_REAL_GH"], *argv[1:]]
    # Only direct GraphQL and quota probes can bypass a known REST block. Opaque gh commands
    # may make REST calls even when their final query is GraphQL.
    endpoint = api_endpoint(args)
    gql = endpoint == "graphql"
    query = next((a[6:] for a in argv if a.startswith("query=")), "") if gql else ""
    if gql and readonly and query and "rateLimit" not in query and "{" in query:
        measured = query.replace("{", "{rateLimit{cost remaining resetAt} ", 1)
        argv = ["query=" + measured if a == "query=" + query else a for a in argv]
    _request_context.set(
        {
            "principal": effective.get("_TAUCETI_PINNED_GITHUB_ACCOUNT")
            or "credential:" + (scope(effective) or "unknown")[:16],
            "credential_scope": (scope(effective) or "unknown")[:16],
            "operation": query_operation(query) if gql else ":".join(argv[1:3]),
            "query_fingerprint": hashlib.sha256(query.encode()).hexdigest()[:20] if query else None,
            "source": "direct",
            "readonly": readonly,
        }
    )
    quota_probe = endpoint == "rate_limit"
    resource = (
        "graphql"
        if gql
        else "code_search"
        if endpoint and endpoint.startswith("search/code")
        else "search"
        if endpoint and endpoint.startswith("search/")
        else "core"
    )
    started = time.monotonic()
    ceiling = min(900, max(0, int(effective.get("TAUCETI_GH_PROXY_WAIT", "900"))))
    sent_intent = False
    observed_resource = endpoint is not None
    while True:
        cooling = cooldown(effective)
        if cooling and not quota_probe:
            if not wait or time.monotonic() - started + cooling > ceiling:
                record_block("secondary", time.time() + cooling, scope(effective))
                blocked = subprocess.CompletedProcess(
                    argv, 1, "", f"gh: secondary rate limit; Retry-After: {int(cooling) + 1}\n"
                )
                blocked.github_before_request = True
                return blocked
            time.sleep(cooling)
        known = observed_budget(effective).get(resource)
        delay = (
            max(1, known[1] - time.time() + 2)
            if observed_resource and known and known[0] == 0 and not quota_probe
            else 0
        )
        if delay:
            if not wait or time.monotonic() - started + delay > ceiling:
                record_block(resource, known[1], scope(effective))
                blocked = subprocess.CompletedProcess(
                    argv, 1, "", f"gh: API rate limit exceeded ({resource}); reset at {known[1]}\n"
                )
                blocked.github_before_request = True
                return blocked
            print(f"gh: API rate limit exhausted ({resource}); waiting {int(delay)}s for reset", file=sys.stderr)
            time.sleep(delay)
        records = []
        try:
            if _intent and not sent_intent:
                from tauceti_worker.github_outbox import sending

                sending(_intent)
                sent_intent = True
            p = subprocess.run(argv, env=effective, capture_output=True, text=True, **kwargs)
        except subprocess.TimeoutExpired as e:
            raw = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr or ""
            e.stderr = parse_trace(raw, identity=scope(effective))
            raise
        p.stderr = parse_trace(p.stderr or "", identity=scope(effective), records=records)
        p.github_records = records
        if gql:
            traced = next((r["graphql_cost"] for r in reversed(records) if "graphql_cost" in r), None)
            try:
                payload = json.loads(p.stdout)
                cost = (
                    payload.get("data", {}).get("rateLimit", {}).get("cost", traced)
                    if isinstance(payload, dict) and isinstance(payload.get("data"), dict)
                    else traced
                )
                event(
                    {
                        "kind": "graphql_charge",
                        "points": cost,
                        "measured": isinstance(cost, int),
                        "bytes": len((p.stdout or "").encode()),
                    }
                )
                if isinstance(payload, dict) and payload.get("errors"):
                    p.returncode = 1
                    if any(
                        (e.get("type") or e.get("extensions", {}).get("code")) in ("RATE_LIMIT", "RATE_LIMITED")
                        for e in payload["errors"]
                        if isinstance(e, dict)
                    ):
                        p.stderr += "\ngh: API rate limit exceeded (graphql)\n"
            except (ValueError, AttributeError):
                event({"kind": "graphql_charge", "points": traced, "measured": isinstance(traced, int)})
        if not readonly:
            invalidate()
        # Restore a machine-readable retry hint, never the private trace or response body.
        retry = next((r.get("retry_after") for r in reversed(records) if r.get("retry_after")), None)
        if p.returncode and retry:
            p.stderr += f"\nRetry-After: {retry}\n"
        # An opaque write command may have local side effects even without a successful API
        # response. Only read commands or one explicitly rejected API request can be replayed.
        retryable = readonly or (args[:1] == ["api"] and len(records) == 1 and "--paginate" not in args)
        rejected = retryable and (
            records
            and all(r.get("status") in (403, 429) for r in records)
            or readonly
            and gql
            and p.returncode
            and "API rate limit exceeded (graphql)" in p.stderr
        )
        p.github_retry_safe = bool(rejected)
        primary = any(r.get("remaining") == 0 for r in records)
        text = (p.stderr or "") + "\n" + (p.stdout or "")
        # Successful bulk reads can contain hundreds of PR bodies. They are not
        # rate-limit errors, and applying this unbounded regex to them can take
        # minutes. Failed responses retain the same creation-cap classification.
        creation_cap = bool(
            p.returncode
            and re.search(
                r"pull.request.{0,500}(?:creation.limit|creation.cap|limit.reached)|(?:creation.limit|creation.cap).{0,500}pull.request",
                text,
                re.I,
            )
        )
        secondary = bool(
            p.returncode
            and (
                any(r.get("status") == 429 for r in records)
                or "secondary rate limit" in p.stderr.lower()
                or creation_cap
            )
        )
        if p.returncode and primary:
            # GraphQL errors can arrive with HTTP 200. The CLI still fails the logical read,
            # so record the exhaustion for stage accounting regardless of the HTTP status.
            for record in records:
                if record.get("remaining") == 0 and isinstance(record.get("reset"), int):
                    record_block(record.get("resource", resource), record["reset"], scope(effective))
        if (
            p.returncode
            and not primary
            and not secondary
            and re.search(r"\bapi rate limit(?: already)? exceeded\b", text, re.I)
        ):
            # Proxies/network failures can strip the headers. Pause briefly and recheck rather
            # than inventing an hourly reset or hammering an endpoint with an optimistic probe.
            reset = observed_budget(effective).get(resource, (0, int(time.time()) + 60))[1]
            remember_quota({"resource": resource, "remaining": 0, "reset": reset, "status": 403}, scope(effective))
            record_block(resource, reset, scope(effective))
            primary = True
        if secondary:
            delay = int(retry) if retry and retry.isdigit() else 60
            cooldown(effective, until=time.time() + max(1, delay))
            record_block("secondary", time.time() + max(1, delay), scope(effective))
        if p.returncode and (primary or secondary):
            if primary:
                actual = {r.get("resource", resource) for r in records if r.get("remaining") == 0} or {resource}
                resource = next((r.get("resource") for r in reversed(records) if r.get("remaining") == 0), resource)
                observed_resource = True
                for bucket in actual:
                    quota_type("primary:" + bucket, records, effective)
            if secondary:
                quota_type("secondary:pr-creation-cap" if creation_cap else secondary_type(text), records, effective)
        if wait and p.returncode and secondary and rejected and not primary:
            delay = int(retry) if retry and retry.isdigit() else 60
            if time.monotonic() - started + max(1, delay) > ceiling:
                return p
            print(f"gh: secondary rate limit; waiting {max(1, delay)}s", file=sys.stderr)
            time.sleep(max(1, delay))
            continue
        if not (wait and p.returncode and rejected and primary):
            return p
        # Retry a definitively rejected command only; never replay a command that partly wrote.
        known = observed_budget(effective).get(resource)
        if not known or time.monotonic() - started + max(0, known[1] - time.time() + 2) > ceiling:
            return p


def api_endpoint(args):
    if args[:1] != ["api"]:
        return None
    value_flags = {
        "--hostname",
        "--method",
        "-X",
        "--jq",
        "-q",
        "--header",
        "-H",
        "--field",
        "-F",
        "--raw-field",
        "-f",
        "--input",
        "--cache",
        "--template",
        "-t",
    }
    skip = False
    for arg in args[1:]:
        if skip:
            skip = False
        elif arg in value_flags:
            skip = True
        elif not arg.startswith("-"):
            return arg.lstrip("/")
    return None


def query_operation(query: str) -> str:
    if re.search(r"\bmutation\b", query):
        return "mutation"
    if "headRefOid" in query and "comments{totalCount}" in query:
        return "progress"
    for field, operation in (
        ("pullRequests", "open_prs"),
        ("reviewThreads", "review_threads"),
        ("issueOrPullRequest", "issue_comments"),
        ("reactions", "reactions"),
        ("viewer", "identity"),
        ("status{", "statuses"),
    ):
        if field in query:
            return operation
    return "graphql_other"


def secondary_type(text: str) -> str:
    text = text.lower()
    for category, words in (
        ("content", ("content creation", "creating content", "spam", "creation cap", "too many pull requests")),
        ("concurrency", ("concurrent", "concurrency")),
        ("compute", ("cpu time", "compute", "processing time")),
        ("points", ("points per minute", "point limit")),
    ):
        if any(word in text for word in words):
            return "secondary:" + category
    return "secondary:unspecified"


def quota_type(category: str, records: list, env=None):
    """Persist first-seen quota categories with allow-listed evidence, never response bodies."""
    try:
        path = root() / "quota-types.json"
        with locked(path.with_suffix(".lock")):
            try:
                known = json.loads(path.read_text())
            except (OSError, ValueError):
                # These are the existing fleet's known categories, not newly discovered limits.
                known = {
                    name: {"existing": True, "count": 0}
                    for name in ("primary:core", "primary:graphql", "secondary:unspecified")
                }
            new = category not in known
            evidence = [
                {
                    k: r[k]
                    for k in ("endpoint", "method", "status", "resource", "remaining", "reset", "retry_after")
                    if k in r
                }
                for r in records
            ]
            prior = known.get(category, {})
            known[category] = {
                **prior,
                "first_seen": prior.get("first_seen", time.time()),
                "last_seen": time.time(),
                "count": prior.get("count", 0) + 1,
                "evidence": evidence,
            }
            atomic(path, known)
        if new:
            event({"kind": "new_quota_type", "category": category, "evidence": evidence})
            print(f"TauCeti: NEW GitHub quota category: {category}; evidence saved in {path}", file=sys.stderr)
    except OSError:
        pass


def install_proxy():
    from .config import log
    from .runtime_status import report_runtime

    try:
        _install_proxy()
    except OSError:
        event({"kind": "instrumentation_unavailable", "component": "gh_proxy"})
        # Keep quota handling in the CLI path even when its normal bin directory
        # is unavailable. Never relocate the durable publication outbox.
        import atexit
        import tempfile

        try:
            temporary_bin = Path(tempfile.mkdtemp(prefix="tauceti-gh-"))
            atexit.register(shutil.rmtree, temporary_bin, ignore_errors=True)
            _install_proxy(bindir=temporary_bin)
        except OSError:
            log("GitHub CLI instrumentation unavailable; endpoint accounting coverage is incomplete")
        else:
            log("GitHub CLI instrumentation uses a temporary launcher; endpoint accounting may be incomplete")
        report_runtime(github_instrumentation_degraded=True)
    else:
        report_runtime(github_instrumentation_degraded=False)


def _install_proxy(*, bindir=None):
    """Make host review/authoring subprocess gh traffic visible too; preserve real gh credentials."""
    os.environ.setdefault("TAUCETI_GITHUB_SHARED_DIR", str(root().resolve()))
    if not os.environ.get("TAUCETI_REAL_GH"):
        real = shutil.which("gh")
        if not real:
            return
        os.environ["TAUCETI_REAL_GH"] = real
    # A stable directory survives HOME isolation and uvx's interpreter changes.
    bindir = bindir or root() / "bin"
    bindir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proxy = bindir / "gh"
    import shlex

    content = (
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --proxy "$@"\n'
    )
    if not proxy.exists() or proxy.read_text() != content:
        atomic_script = proxy.with_name("gh." + uuid.uuid4().hex)
        atomic_script.write_text(content)
        atomic_script.chmod(0o700)
        atomic_script.replace(proxy)
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["TAUCETI_GITHUB_TRANSPORT_REV"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def migrated_history(args):
    """Compatibility bridge for host review engines, with live reads and existing jq formatting.

    Match ONLY the complete-history forms we can preserve. Unrecognized flags use the real CLI.
    Publishing/claim ownership reads remain live even though survey histories share a cache.
    """
    if args[:1] != ["api"] or "--paginate" not in args:
        return None
    endpoint, jq = None, None
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--paginate":
            pass
        elif arg in ("--jq", "-q") and i + 1 < len(args):
            i += 1
            jq = args[i]
        elif re.fullmatch(r"/?repos/[^/]+/[^/]+/(issues|pulls)/[0-9]+/comments\?per_page=100", arg):
            if endpoint is not None:
                return None
            endpoint = arg
        else:
            return None
        i += 1
    if not endpoint or (jq and not shutil.which("jq")):
        return None
    scoreboard_filter = (
        '.[] | select((.body // "") | contains("<!--tauceti-scoreboard-->")) '
        '| {id, login: (.user.login // ""), updated_at: (.updated_at // "")} | @json'
    )
    if jq not in (".[]", ".[] | @json", scoreboard_filter):
        return None  # not a general REST emulator: unknown projections need the full REST schema
    # In a wheel this file lives directly in the importable package; in a checkout likewise.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tauceti_worker import github_transport as imported_transport
    from tauceti_worker.github import GitHub

    parts = endpoint.lstrip("/").split("/")
    client = GitHub("/".join(parts[1:3]))
    with imported_transport.read_context(fresh=True):
        rows = (client.issue_comments if parts[3] == "issues" else client.review_comments)(int(parts[4]))
    if rows is None:
        return subprocess.CompletedProcess(
            args, 1, "", "gh: GraphQL comment history unavailable; no partial result returned\n"
        )
    body = json.dumps(rows)
    if jq:
        return subprocess.run(["jq", "-c", "-r", jq], input=body, capture_output=True, text=True)
    return subprocess.CompletedProcess(args, 0, body + "\n", "")


def migrated_identity(args):
    if args not in (["api", "user", "--jq", ".login"], ["api", "/user", "--jq", ".login"]):
        return None
    login = os.environ.get("_TAUCETI_PINNED_GITHUB_ACCOUNT")
    if (
        login
        and login == os.environ.get("TAUCETI_GITHUB_ACCOUNT")
        and scope() == os.environ.get("_TAUCETI_PINNED_GITHUB_SCOPE")
    ):
        event({"kind": "cache_hit", "read": "verified_identity"})
        return subprocess.CompletedProcess(args, 0, login + "\n", "")
    return execute(
        ["gh", "api", "graphql", "-f", "query=query{viewer{login}}", "--jq", ".data.viewer.login"], wait=True
    )


def proxy_readonly(args):
    if len(args) > 1 and args[0] == "search" and args[1] in ("code", "commits", "issues", "prs", "repos"):
        return True
    if args[:1] == ["api"]:
        if "graphql" in args:
            document = next((a[6:] for a in args if a.startswith("query=")), None)
            if document is None:
                return False
            # Literal search text and variable values can contain "mutation".
            # Only operation declarations in the explicit document determine
            # whether an exhausted request is safe to retry as a read.
            cleaned = re.sub(r'"""(?:\\.|(?!""")[\s\S])*"""|"(?:\\.|[^"\\])*"|#[^\r\n]*', " ", document)
            if not re.match(r"\s*(?:query\b|\{)", cleaned):
                return False
            depth = 0
            for match in re.finditer(r"[{}]|[A-Za-z_][A-Za-z_0-9]*", cleaned):
                token = match.group()
                if token == "{":
                    depth += 1
                elif token == "}":
                    depth -= 1
                elif token == "mutation" and depth == 0:
                    return False
            return depth == 0
        if any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args):
            return False
        for i, a in enumerate(args):
            if a in ("-X", "--method") and (i + 1 >= len(args) or args[i + 1].upper() != "GET"):
                return False
            if a.startswith("--method=") and a.split("=", 1)[1].upper() != "GET":
                return False
        return True
    return len(args) > 1 and args[1] in ("list", "view", "status", "checks", "diff", "search")


def report(hours: float):
    from collections import Counter

    counts, hits, failures = Counter(), Counter(), Counter()
    start = time.time() - hours * 3600
    for path in (root() / "requests.previous.jsonl", root() / "requests.jsonl"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("time"), (int, float)) or r["time"] < start:
                continue
            if r.get("kind") == "cache_hit":
                hits[r["read"]] += 1
            elif r.get("kind") == "http":
                key = (r.get("resource", "unknown"), r.get("method", "?"), r.get("endpoint", "?"))
                counts[key] += 1
                failures[key] += r.get("status", 0) >= 400
    print("Requests (including retries/pages; not GraphQL point charges):")
    for (resource, method, endpoint), count in counts.most_common():
        print(f"{count:7}  errors={failures[(resource, method, endpoint)]:4}  {resource:8} {method:6} {endpoint}")
    print("Shared cache hits:", dict(hits))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--proxy"]:
        # No API accounting for auth token output or interactive operations.
        args = sys.argv[2:]
        if not args or args[0] in ("auth", "help", "completion", "config", "extension") or "--web" in args:
            os.execv(os.environ["TAUCETI_REAL_GH"], ["gh", *args])
        # Writes issued outside the Python client also invalidate shared survey data.
        migrated = migrated_identity(args)
        if migrated is None and os.environ.get("TAUCETI_GITHUB_HISTORY_BRIDGE") == "1":
            migrated = migrated_history(args)
        if migrated is not None:
            sys.stdout.write(migrated.stdout or "")
            sys.stderr.write(migrated.stderr or "")
            sys.exit(migrated.returncode)
        readonly = proxy_readonly(args)
        # Conservatively invalidate opaque CLI operations as well. Cache correctness wins over
        # retaining a hit after a child command whose write behavior cannot be proved.
        if not readonly:
            invalidate()
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tauceti_worker.config import GitHubUnavailable
        from tauceti_worker.constants import EX_GITHUB_WAIT

        try:
            p = execute(["gh", *args], wait=True)
        except GitHubUnavailable as error:
            # The shell caller cannot catch Python exceptions. Preserve the
            # infrastructure wait marker for its owning worker's stage guard.
            record_block("publication", error.retry_at or time.time() + 60)
            p = subprocess.CompletedProcess(
                args, EX_GITHUB_WAIT, "", f"gh: {error}; publication retained for recovery\n"
            )
        finally:
            if not readonly:
                invalidate()
        if getattr(p, "github_permanent_failure", False):
            p.returncode = 78  # Definitive rejection; the private .failed intent is retained.
        sys.stdout.write(p.stdout or "")
        sys.stderr.write(p.stderr or "")
        sys.exit(p.returncode)
    parser = argparse.ArgumentParser(description="GitHub endpoint accounting (retained records)")
    parser.add_argument("--hours", type=float, default=1)
    report(parser.parse_args().hours)
