"""tauceti_worker.agents — prompt filling, the host/bubble checkout, and the agent launch: the host
argv path and the repo-scoped bubble sandbox path (plus per-worker $HOME isolation)."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only; importing at runtime would invert the layer order
    from .work_units import RoundOpts, Worker

from . import build_caches
from .config import Config, Die, NoProgress, log
from .constants import (
    AUTHORING_DEFAULTS,
    CLAUDE_CMD,
    CODEX_AUTHORING_FALLBACK_MODEL,
    CODEX_MODEL_ACCESS_TTL,
    OPENROUTER_MODELS,
    PI_RUN,
    REVIEW,
    REVIEW_DAILY_CAP,
    ROADMAP,
    TAUCETI,
)
from .github import me
from .paths import HERE
from .quota import (
    Quota,
    _claude_keychain_creds_interactive,
    _read_json_file,
    _safe_exists,
    _write_json_atomic,
    claude_dir,
    codex_dir,
    mirror_creds,
)
from .runtime_status import report_failure
from .transcript import AgentTranscriptRenderer
from .usage import UsageError, kiro_data_dir, kiro_process_env, snapshot_kiro_auth_db

# ============================================================================


@dataclass(frozen=True)
class AuthoringProfile:
    """Exact provider configuration consumed by both execution backends."""

    provider: str
    model: str
    effort: str | None
    model_source: str
    effort_source: str
    fallback_model: str | None = None


def _validate_kiro_model_pin(model: str, source: str) -> str:
    """Reject Kiro's router anywhere an exact model id is required."""
    if model.lower().startswith("auto"):
        raise Die(f"Kiro model from {source} must be an exact model id, not the Auto router: {model!r}")
    _reject_retired_opus(model, source)
    return model


def _reject_retired_opus(model: str, source: str) -> None:
    """Keep direct Claude and Kiro dispatches off the replaced Opus generation."""
    if model.lower() in {"claude-opus-4.8", "claude-opus-4-8"}:
        raise Die(f"Claude Opus 4.8 from {source} is retired; use the exact claude-opus-5 model")


def resolve_authoring_profile(
    provider: str,
    *,
    cli_model: str | None = None,
    cli_effort: str | None = None,
    resolved_fallback_model: str | None = None,
) -> AuthoringProfile:
    """Resolve CLI > provider environment > committed default, without user CLI config.

    `TAUCETI_CODEX_MODEL` remains a deprecated authoring-only fallback. Reviews
    deliberately use `TAUCETI_REVIEW_CODEX_MODEL` instead.
    """

    cli_model = cli_model.strip() if cli_model else None
    cli_effort = cli_effort.strip() if cli_effort else None
    resolved_fallback_model = resolved_fallback_model.strip() if resolved_fallback_model else None
    key = provider.upper()
    model_env = f"TAUCETI_AUTHORING_{key}_MODEL"
    effort_env = f"TAUCETI_AUTHORING_{key}_EFFORT"

    if provider in AUTHORING_DEFAULTS:
        default_model, default_effort = AUTHORING_DEFAULTS[provider]
    elif provider in OPENROUTER_MODELS:
        default_model, default_effort = OPENROUTER_MODELS[provider], None
    else:
        raise Die(f"no authoring profile for provider {provider!r}")

    legacy = ((os.environ.get("TAUCETI_CODEX_MODEL") or "").strip() or None) if provider == "codex" else None
    env_model = (os.environ.get(model_env) or "").strip() or None
    if cli_model:
        model, model_source = cli_model, "--author-model"
    elif env_model:
        model, model_source = env_model, f"${model_env}"
    elif legacy:
        model, model_source = legacy, "$TAUCETI_CODEX_MODEL (deprecated; authoring only)"
    else:
        model, model_source = default_model, "repository default"

    env_effort = (os.environ.get(effort_env) or "").strip() or None
    if provider in OPENROUTER_MODELS and (cli_effort or env_effort):
        raise Die(f"authoring effort is not supported for OpenRouter agent {provider}")
    if cli_effort:
        effort, effort_source = cli_effort, "--author-effort"
    elif env_effort:
        effort, effort_source = env_effort, f"${effort_env}"
    else:
        effort, effort_source = default_effort, "repository default"

    if not model:
        raise Die(f"authoring model for {provider} must not be empty")
    if provider == "kiro":
        _validate_kiro_model_pin(model, model_source)
    elif provider == "claude":
        _reject_retired_opus(model, model_source)
    if effort and not re.fullmatch(r"[A-Za-z0-9._-]+", effort):
        raise Die(f"authoring effort for {provider} contains unsupported characters: {effort!r}")
    fallback_model = None
    if provider == "codex":
        if resolved_fallback_model:
            # Internal loop-child handoff: the parent already resolved whether its model was a default
            # (eligible for fallback) or an operator pin. Without this provenance, pinning the resolved
            # Sol model into the child would accidentally turn the default into an explicit override.
            fallback_model = resolved_fallback_model
        elif model_source == "repository default" and model == AUTHORING_DEFAULTS["codex"][0]:
            fallback_model = CODEX_AUTHORING_FALLBACK_MODEL
    return AuthoringProfile(provider, model, effort, model_source, effort_source, fallback_model)


def _authoring_profile(value: AuthoringProfile | str) -> AuthoringProfile:
    return value if isinstance(value, AuthoringProfile) else resolve_authoring_profile(value)


_CODEX_MODEL_ERROR_STATUSES = {400, 403, 404}
_CODEX_MODEL_ERROR_MESSAGE = re.compile(
    r"not supported when using codex"
    r"|does not exist or you do not have access"
    r"|(?:no|do not have) access to (?:this )?model"
    r"|model[_ ]not[_ ]found|model metadata for .*? not found"
    r"|unsupported model|invalid model|unknown model"
    r"|model .*?not entitled|not entitled to (?:this |use )?model",
    re.I,
)
_CODEX_MODEL_ACCESS_PROMPT = "Reply with exactly OK. Do not use tools."


def _codex_error_details(transcript: str) -> tuple[int | None, str | None]:
    """Parse Codex JSONL's terminal HTTP status/message; never classify raw transcript text."""
    failed_payload = error_payload = None
    for line in transcript.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                failed_payload = error["message"]
        elif kind and ("error" in kind or "failed" in kind):
            if error_payload is None and isinstance(event.get("message"), str):
                error_payload = event["message"]

    for payload in (failed_payload, error_payload):
        if not isinstance(payload, str):
            continue
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(decoded, dict):
            continue
        error = decoded.get("error")
        error = error if isinstance(error, dict) else {}
        status = decoded.get("status", error.get("status"))
        if isinstance(status, bool):
            status = None
        elif isinstance(status, str) and status.lstrip("-").isdigit():
            status = int(status)
        if not isinstance(status, int):
            status = None
        message = error.get("message") or decoded.get("message")
        return status, message if isinstance(message, str) else None
    return None, None


def _codex_model_unavailable(returncode: int, transcript: str) -> bool:
    """Only a structured client rejection naming model access confirms an entitlement miss."""
    if returncode == 0:
        return False
    status, message = _codex_error_details(transcript)
    return bool(
        status in _CODEX_MODEL_ERROR_STATUSES
        and isinstance(message, str)
        and _CODEX_MODEL_ERROR_MESSAGE.search(message)
    )


def _codex_model_probe(cfg: Config, model: str) -> subprocess.CompletedProcess[str]:
    """Make one read-only, ephemeral request that cannot mutate the authoring checkout."""
    cfg.state.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)  # authoring is subscription-paced; probe that same account
    env.pop("ANTHROPIC_API_KEY", None)
    command = [
        "codex",
        "exec",
        "--json",
        "--model",
        model,
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        _CODEX_MODEL_ACCESS_PROMPT,
    ]
    try:
        return subprocess.run(
            command,
            cwd=cfg.state,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
    except OSError as error:
        raise NoProgress(f"codex model-access probe could not launch: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise NoProgress("codex model-access probe timed out after 120s; not launching authoring") from error


def _codex_probe_failure(model: str, result: subprocess.CompletedProcess[str]) -> NoProgress:
    status, message = _codex_error_details(result.stdout or "")
    detail = message or (result.stderr or "").strip()[-1000:] or f"exit {result.returncode}"
    status_text = f"HTTP {status}: " if status is not None else ""
    return NoProgress(
        f"codex model-access probe for {model} failed without confirming subscription unavailability "
        f"({status_text}{detail}); not launching authoring"
    )


def resolve_codex_model_access(cfg: Config, profile: AuthoringProfile) -> AuthoringProfile:
    """Resolve a default Sol profile to Sol or Terra before the real task runs.

    Explicit model pins have no fallback and bypass this probe. A confirmed result is cached per worker
    and account; failures that might be transient are never cached and never cause a downgrade.
    """
    fallback = profile.fallback_model
    if profile.provider != "codex" or not fallback:
        return profile

    fp = Quota(cfg).codex_account_fingerprint()
    cache_path = cfg.quota_cache / "codex-model-access.json"
    cached = _read_json_file(cache_path) or {}
    fetched_at = cached.get("fetched_at")
    age_ok = (
        isinstance(fetched_at, (int, float))
        and not isinstance(fetched_at, bool)
        and time.time() - fetched_at <= CODEX_MODEL_ACCESS_TTL
    )
    if (
        fp is not None
        and cached.get("fp") == fp
        and cached.get("primary_model") == profile.model
        and cached.get("fallback_model") == fallback
        and age_ok
        and isinstance(cached.get("available"), bool)
    ):
        available = cached["available"]
    else:
        first = _codex_model_probe(cfg, profile.model)
        if first.returncode == 0:
            available = True
        elif _codex_model_unavailable(first.returncode, first.stdout or ""):
            # Reconfirm entitlement before persisting a downgrade. Both probes are trivial, read-only,
            # and checkout-independent; the real authoring prompt is still executed exactly once.
            second = _codex_model_probe(cfg, profile.model)
            if second.returncode == 0:
                available = True
            elif _codex_model_unavailable(second.returncode, second.stdout or ""):
                available = False
            else:
                raise _codex_probe_failure(profile.model, second)
        else:
            raise _codex_probe_failure(profile.model, first)

        if fp is not None:
            cfg.quota_cache.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                cache_path,
                {
                    "fetched_at": int(time.time()),
                    "fp": fp,
                    "primary_model": profile.model,
                    "fallback_model": fallback,
                    "available": available,
                },
            )

    if available:
        return replace(profile, fallback_model=None)
    log(f"codex: {profile.model} is unavailable to this subscription; using {fallback}")
    return replace(profile, model=fallback, model_source="subscription fallback", fallback_model=None)


def _kiro_model_ids(payload) -> set[str]:
    """Extract exact ids from the documented `--list-models --format json` shapes."""
    found: set[str] = set()
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                found.add(item)
            else:
                found.update(_kiro_model_ids(item))
    elif isinstance(payload, dict):
        for key in ("id", "modelId", "model_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                found.add(value)
        for key in ("models", "availableModels", "available_models", "data"):
            if key in payload:
                found.update(_kiro_model_ids(payload[key]))
    return found


def validate_kiro_model_access(cfg: Config, profile: AuthoringProfile) -> AuthoringProfile:
    """Confirm that Kiro advertises the exact pinned model without sending a prompt."""
    if profile.provider != "kiro":
        return profile
    _validate_kiro_model_pin(profile.model, profile.model_source)
    cfg.state.mkdir(parents=True, exist_ok=True)
    env = kiro_process_env(dict(os.environ))
    try:
        result = subprocess.run(
            ["kiro-cli", "chat", "--list-models", "--format", "json"],
            cwd=cfg.state,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except OSError as e:
        raise NoProgress(f"Kiro model-access check could not launch: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise NoProgress("Kiro model-access check timed out after 60s; not launching authoring") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[-1000:]
        raise NoProgress(f"Kiro could not list this account's models: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise NoProgress("Kiro returned malformed JSON from --list-models; not risking its Auto router") from e
    models = _kiro_model_ids(payload)
    if not models:
        raise NoProgress("Kiro returned no model ids from --list-models; not risking its Auto router")
    if profile.model not in models:
        available = ", ".join(sorted(models))
        raise NoProgress(
            f"Kiro model {profile.model!r} is not available to this account (available: {available}); "
            "the worker will not fall back to Auto"
        )
    return profile


# Agents — prompt filling, the host checkout, and the agent launch. The host argv lists are a frozen
# contract — keep them byte-for-byte stable (the historical `( cd … ) 9>&-` and `env -u` shell
# mechanics map to cwd=, close_fds=True, and a pruned env). claim.sh / git-safe-push /
# gh-safe-pr-create are put on the agent's PATH, AND named by absolute path in the prompts — see
# wrapper_bin.
# ============================================================================


def wrapper_bin(bubble: bool = False) -> str:
    """The directory holding the write wrappers, as the prompts must spell it.

    PATH alone is not enough. Codex runs its shell as `bash -lc`, and a login shell re-runs the
    system profile: on NixOS /etc/profile REPLACES $PATH outright unless $__ETC_PROFILE_SOURCED
    came in with the environment, so the `HERE/scripts` prefix host_agent_argv exports is silently
    dropped. Observed on 131 of 1525 codex transcripts and none of 1161 claude ones (claude uses a
    non-login shell): `git-safe-push: command not found`, at the one moment that matters — after a
    green build, before anything is pushed. 120 of those rounds spent two to four turns hunting for
    the wrapper with `rg --files`; 11 never pushed at all and their work was lost.

    The agents were right to refuse a raw `git push` — the prompts forbid it and it would bypass the
    branch CAS. So the fix belongs here: name the wrapper by a path that no shell startup file can
    take away. The PATH prefix stays as well, so a hand-written `git-safe-push` keeps working."""
    return "/opt/round" if bubble else str(HERE / "scripts")


_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def fill_prompt(path: Path, **subs) -> str:
    """Render a prompt template, substituting `__KEY__` for each keyword argument.

    One pass over the TEMPLATE, never over what was substituted into it. Several values are
    untrusted: `__CLAIMED__` is text copied from other contributors' intention issues, and
    `__SOURCE_GUIDANCE__` names an operator-supplied repository. Substituting sequentially let a
    claim that happened to contain `__BIN__` be rewritten by a later pass, and validating the
    RENDERED text would let a claim containing any `__WORD__` abort the round — turning a mechanism
    documented as cooperative and fail-open into a way to stop a roadmap worker."""
    path = Path(path)
    text = path.read_text()
    # An unfilled placeholder ships literal `__BIN__/git-safe-push` to the agent, which is the
    # command-not-found failure wrapper_bin exists to end. Fail here instead, but only for the
    # prompts we ship: the progress prompt is served by the pinned TauCetiProgress build (written
    # to a scratch file), and its placeholder set is that repo's business, not ours.
    if path.parent == HERE / "prompts":
        missing = sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(text)} - {f"__{k}__" for k in subs})
        if missing:
            raise Die(f"{path.name}: unfilled prompt placeholder(s) {', '.join(missing)}")
    # A function replacement is taken literally, so a value containing a backslash escape or a
    # `\g<name>` group reference cannot be reinterpreted either.
    return _PLACEHOLDER_RE.sub(lambda m: str(subs.get(m.group(0)[2:-2], m.group(0))), text)


def sync_mathlib_pool(cfg: Config) -> None:
    """Exchange finished `.ltar`s with the machine pool before the agent runs.

    This is how the Mathlib cache is shared without pointing two unlocked downloaders at one
    directory: the worker promotes what it fetched last round, then takes a link to everything the
    pool has that it lacks, so `lake exe cache get` downloads only what nobody here has and still
    writes nowhere another process can see. Best effort — a worker that cannot reach the pool
    downloads more, which is slow rather than wrong. It must run before the agent starts, since the
    worker's own directory is only quiescent until then."""
    private = Path(os.environ.get("MATHLIB_CACHE_DIR") or (cfg.data_home / ".cache" / "mathlib"))
    pool = build_caches.mathlib_pool(_host_home(), {k: v for k, v in os.environ.items() if k != "MATHLIB_CACHE_DIR"})
    if private.resolve() == pool.resolve():
        return  # operator pointed this worker straight at the pool; nothing to exchange
    try:
        promoted, hydrated = build_caches.sync_pool(private, pool)
    except OSError as e:
        log(f"mathlib cache pool: skipped ({e})")
        return
    if promoted or hydrated:
        log(f"mathlib cache pool: promoted {promoted}, hydrated {hydrated} ({pool})")


def clean_lake_cache_after_toolchain_bump(cfg: Config) -> None:
    """Drop this worker's owned Lake artifact store when canonical main changes toolchains.

    The marker follows ``origin/main``, not the branch left by the previous round and not a PR the
    worker is about to check out.  Otherwise alternating between an old-toolchain PR and current
    main would erase the cache every round.  The first observation only seeds the marker, so enabling
    this policy does not unexpectedly discard an established store.

    Only the default per-worker path is ours to delete.  An explicit ``LAKE_CACHE_DIR`` can name an
    operator-managed or shared store, and is therefore reported but left untouched.
    """
    import hashlib

    toolchain_file = cfg.checkout / "lean-toolchain"
    try:
        contents = toolchain_file.read_bytes()
    except OSError:
        return
    digest = hashlib.sha256(contents).hexdigest()
    label = next((line.strip() for line in contents.decode(errors="replace").splitlines() if line.strip()), "empty")
    label = label[:160]

    marker = cfg.state / "cache" / "lake-cache-toolchain.json"
    previous = _read_json_file(marker)
    if not isinstance(previous, dict) or not isinstance(previous.get("sha256"), str):
        # Missing is the expected migration path.  A corrupt marker is also treated conservatively:
        # replace it, but do not turn unreadable bookkeeping into permission for a destructive clean.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(marker, {"sha256": digest, "toolchain": label})
        except OSError as e:
            log(f"Lake artifact cache: could not record toolchain marker ({e})")
        return
    if previous["sha256"] == digest:
        return

    old_label = str(previous.get("toolchain") or previous["sha256"][:12])
    owned_cache = cfg.data_home / ".cache" / "lake"
    configured_cache = Path(os.environ.get("LAKE_CACHE_DIR") or owned_cache)
    same_path = os.path.abspath(configured_cache) == os.path.abspath(owned_cache)
    if not same_path or configured_cache.is_symlink():
        log(
            f"Lake artifact cache: main toolchain changed ({old_label} → {label}); "
            f"leaving operator-managed LAKE_CACHE_DIR untouched ({configured_cache})"
        )
    else:
        try:
            had_cache = configured_cache.exists()
            if had_cache:
                shutil.rmtree(configured_cache)
        except OSError as e:
            # Do not advance the marker: retry at the next quiescent checkout preparation.
            log(f"Lake artifact cache: main toolchain changed ({old_label} → {label}), but cleanup failed ({e})")
            return
        result = f"cleared {configured_cache}" if had_cache else "cache already empty"
        log(f"Lake artifact cache: main toolchain changed ({old_label} → {label}); {result}")

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(marker, {"sha256": digest, "toolchain": label})
    except OSError as e:
        log(f"Lake artifact cache: cleanup completed but could not update toolchain marker ({e})")


def prepare_checkout(cfg: Config) -> bool:
    """Clean checkout of TauCeti main; keep .lake for fast rebuilds, drop every other leftover."""
    sync_mathlib_pool(cfg)
    co = cfg.checkout
    if not (co / ".git").is_dir():
        co.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning {TAUCETI} → {co} (first run)")
        if subprocess.run(["git", "clone", "-q", f"https://github.com/{TAUCETI}", str(co)]).returncode:
            return False

    def g(*a) -> int:
        return subprocess.run(["git", "-C", str(co), *a]).returncode

    if g("fetch", "-q", "origin"):
        return False
    # -f discards a prior round's leftover edits and lands us on main in one step; a plain
    # switch/checkout would refuse on a dirty tree (two noisy errors) and could leave HEAD on
    # the old branch with only main's content. Bail if even the forced checkout fails.
    if g("checkout", "-q", "-f", "-B", "main", "origin/main"):
        return False
    # The checkout is quiescent here: the previous agent has exited and the next one has not started.
    # Compare canonical main, rather than an arbitrary PR branch, and retire old-toolchain artifacts
    # before anything can begin writing this worker's private Lake store again.
    clean_lake_cache_after_toolchain_bump(cfg)
    g("clean", "-fdxq", "-e", ".lake")
    return True


def _fetch_shallow(url: str, dir: Path) -> bool:
    """Clone or refresh a worker-owned shallow checkout and make its origin fetch-only."""
    if (dir / ".git").is_dir():
        ok = (
            subprocess.run(["git", "-C", str(dir), "fetch", "-q", "--depth", "1", "origin", "HEAD"]).returncode == 0
            and subprocess.run(["git", "-C", str(dir), "reset", "-q", "--hard", "FETCH_HEAD"]).returncode == 0
        )
        clean = subprocess.run(["git", "-C", str(dir), "clean", "-fdxq"]).returncode == 0
        no_push = subprocess.run(["git", "-C", str(dir), "config", "remote.origin.pushurl", "no_push"]).returncode == 0
        return ok and clean and no_push
    import shutil

    shutil.rmtree(dir, ignore_errors=True)
    dir.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(["git", "clone", "-q", "--depth", "1", "--", url, str(dir)]).returncode == 0
    return (
        cloned and subprocess.run(["git", "-C", str(dir), "config", "remote.origin.pushurl", "no_push"]).returncode == 0
    )


def fetch_ref(repo: str, dir: Path) -> bool:
    """Worker-owned throwaway shallow mirror of repo's default branch (reset hard, clean)."""
    return _fetch_shallow(f"https://github.com/{repo}", dir)


def fetch_git_source(url: str, dir: Path) -> bool:
    """Clone or refresh a worker-owned shallow snapshot of a source Git repository."""
    return _fetch_shallow(url, dir)


def host_agent_argv(prompt: str, profile: AuthoringProfile | str) -> tuple[list[str], dict]:
    """The exact argv + env for the host work agent. HERE is on PATH so the agent
    resolves git-safe-push / gh-safe-pr-create / claim.sh; close_fds=True replaces `9>&-`."""
    env = {**os.environ, "PATH": f"{HERE / 'scripts'}:{os.environ.get('PATH', '')}"}
    profile = _authoring_profile(profile)
    if profile.provider == "codex":
        # Explicit model/effort flags are authoritative while preserving unrelated operator config
        # such as enterprise model providers, MCP servers, and notification hooks.
        env.pop("OPENAI_API_KEY", None)  # Codex rounds are paced against ChatGPT subscription usage
        argv = ["codex", "exec", "--json", "--model", profile.model]
        if profile.effort:
            argv += ["-c", f'model_reasoning_effort="{profile.effort}"']
        argv += ["-c", 'model_reasoning_summary="detailed"', "-c", "show_raw_agent_reasoning=false"]
        argv += ["--sandbox", "danger-full-access", "--skip-git-repo-check", prompt]
    elif profile.provider == "kiro":
        # --model is mandatory: Kiro's Auto router is never allowed to choose on
        # the worker's behalf. Isolate its platform credential store for API-key
        # runs so a persisted browser login cannot shadow the key.
        env = kiro_process_env(env)
        argv = [
            "kiro-cli",
            "chat",
            "--no-interactive",
            "--trust-all-tools",
            "--model",
            profile.model,
        ]
        if profile.effort:
            argv += ["--effort", profile.effort]
        argv += [prompt]
    elif profile.provider in OPENROUTER_MODELS:
        argv = [PI_RUN, "openrouter", profile.model, "--prompt", prompt]
    else:  # claude (Opus); ANTHROPIC_API_KEY unset so it bills the Max plan
        env.pop("ANTHROPIC_API_KEY", None)
        base = shlex_split(CLAUDE_CMD) or ["claude"]  # empty / whitespace-only falls back, not a broken argv
        argv = [*base, "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", profile.model]
        if profile.effort:
            argv += ["--effort", profile.effort]
        argv += ["--dangerously-skip-permissions"]
    return argv, env


def run_agent_host(cwd: Path, prompt: str, profile: AuthoringProfile | str, logdir: Path) -> int:
    profile = _authoring_profile(profile)
    argv, env = host_agent_argv(prompt, profile)
    if os.environ.get("TAUCETI_AGENT_ECHO"):
        print(f"HOST cwd={cwd}\n  " + " ".join(_shq(a) for a in argv))
        return 0
    return run_agent_proc(
        argv,
        env=env,
        cwd=cwd,
        logdir=logdir,
        label=f"agent-{profile.provider}",
        provider=profile.provider,
    )


# Provider statuses that mean "the service could not serve this request right now", as opposed to
# "this request was wrong". An explicit allowlist, not a 5xx range: an unrecognised status stays
# charged, which is the behaviour that existed before this classifier, so the cost of omitting one is
# a single attempt rather than a retry loop. 401/403/404 are
# deliberately absent: a real auth or entitlement failure must stay charged and stay loud, or a
# misconfigured worker would retry for ever having reviewed nothing (TauCetiReview#105 is exactly
# that failure, and it burned two PRs' worth of scoreboards before a human noticed).
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})

# Claude Code in -p mode reports a provider failure as `API Error: <status> <text>` and, when the
# call never produced a turn, that single line is the WHOLE log — observed verbatim as
# "Failed to authenticate. API Error: 401 Invalid authentication credentials". Jeremy Kahn's report
# (kim-em/TauCetiWorker, the #1434 casualty) shows the same shape carrying 529 Overloaded. The colon
# is required: both observed samples have it, and making it optional would match prose like
# "API Error529" or an agent quoting a status number.
_API_ERROR_RE = re.compile(r"API Error:\s*(\d{3})\b", re.I)

# A refund asserts "the agent never ran", so only a log with nothing in it BUT the failure qualifies.
# The log is a combined stdout+stderr transcript: agent prose, tool output, test output, and under
# bubble the pre-agent build. Scanning that for a status number refunds a genuine task failure whose
# transcript merely quotes one — a flaky `lake exe cache get` that the agent recovered from, a test
# asserting on an error string, the agent reading a log. Requiring the whole transcript to be this
# short is what distinguishes "the call never happened" from "the call happened and something later
# went wrong", and it is the claim the refund actually makes.
_INFRA_MAX_LINES = 6

# Transport failures that never reach a status line at all. Kept deliberately short and literal:
# over-matching here refunds a real task failure, and the whole point of the budget is that a PR the
# agent genuinely cannot fix stops consuming rounds. Anything not listed simply stays charged, which
# is the behaviour that existed before this classifier.
_TRANSPORT_RE = re.compile(
    r"\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH|EAI_AGAIN)\b"
    r"|\bsocket hang up\b"
    r"|\bConnection (?:error|reset by peer)\b"
    r"|\bTLS handshake timeout\b",
    re.I,
)


def classify_agent_failure(text: str) -> str | None:
    """Name the INFRASTRUCTURE reason an agent run failed, or None when the failure is the agent's own.

    "Infrastructure" means: this round would have failed identically no matter which PR it was pointed
    at, and no work was lost that retrying cannot redo. Such a failure must not be charged to a PR's
    attempt budget — the same principle the host-agent-binary preflight already applies in
    work_units.py, where a machine-wide fault raises NoProgress instead of marching PRs one by one to
    the escalation cap.

    Returns a short human-readable reason ("provider returned 529") for logging, or None.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines or len(lines) > _INFRA_MAX_LINES:
        return None  # the agent produced a transcript, so it ran; whatever failed is its own
    hay = "\n".join(lines)
    # The LAST status is the terminal one. Taking the first would refund a run that saw a 529, retried
    # past it, and then died of a 401 — and would charge the reverse. Only the outcome counts.
    status = None
    for m in _API_ERROR_RE.finditer(hay):
        status = int(m.group(1))
    if status is not None:
        # A status we DID parse and that is not transient is a definite task-side answer (a bad
        # request, a dead credential). Stop here rather than falling through to the transport
        # patterns, which could otherwise rescue a 401 that happens to mention a socket.
        return f"provider returned {status}" if status in _TRANSIENT_STATUSES else None
    if _TRANSPORT_RE.search(hay):
        return "could not reach the provider"
    return None


# The classification of the most recent agent subprocess this round, or None. A round runs exactly one
# work agent (run_round dispatches a single work unit), so a module-level record is unambiguous here,
# and it keeps run_agent_proc's `-> int` contract intact for its several callers. Written on every
# non-zero exit, including the bubble path, which funnels through the same function.
_LAST_AGENT_FAILURE: str | None = None


def take_last_agent_infra_failure() -> str | None:
    """The infrastructure reason the last agent subprocess failed, or None (agent's own failure or no
    agent run yet). Reads AND CLEARS: a refund is granted against one observed failure, so a second
    caller must not be able to spend the same one twice."""
    global _LAST_AGENT_FAILURE
    reason, _LAST_AGENT_FAILURE = _LAST_AGENT_FAILURE, None
    return reason


def run_agent_proc(
    argv: list[str],
    *,
    env: dict,
    logdir: Path,
    label: str,
    provider: str,
    cwd: Path | None = None,
) -> int:
    """Run an agent subprocess through its readable transcript renderer.

    By default the normalized transcript goes to a timestamped file under logdir. ``--stream`` sends
    the identical content to the terminal instead. On a non-zero exit we always tail the logfile so
    failures aren't silent.
    """
    global _LAST_AGENT_FAILURE
    _LAST_AGENT_FAILURE = None
    cwds = str(cwd) if cwd is not None else None
    # Every supported agent receives its prompt in argv. Close stdin explicitly: Bubble reaches the
    # agent through a non-PTY SSH channel, so an inherited terminal becomes a non-TTY stream there;
    # Codex then treats it as additional prompt input and waits forever for EOF.
    renderer = AgentTranscriptRenderer(provider)
    tail: deque[str] = deque(maxlen=20)

    def write_rendered(destination, rendered: str) -> None:
        # TextIOWrapper defaults to strict encoding errors. Preserve live streaming even under a
        # non-UTF-8 locale by replacing characters that the terminal encoding cannot represent.
        encoding = getattr(destination, "encoding", None)
        if encoding:
            rendered = rendered.encode(encoding, errors="replace").decode(encoding)
        destination.write(rendered)
        destination.flush()
        tail.extend(rendered.splitlines())

    def run_rendered(destination) -> int:
        proc = subprocess.Popen(
            argv,
            cwd=cwds,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                rendered = renderer.render_line(line)
                if rendered:
                    write_rendered(destination, rendered)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        rc = proc.wait()
        if provider in {"codex", "claude"} and not renderer.active:
            write_rendered(destination, f"[warning] no structured {provider} events were recognized\n")
        return rc

    def classify_rendered_failure() -> str | None:
        # A refund asserts that the agent never ran. Structured work events are stronger evidence
        # than transcript length, while a final structured provider diagnostic is stronger evidence
        # than Bubble prelude lines. A retry event alone is deliberately not terminal evidence.
        if provider in {"codex", "claude"} and renderer.saw_work:
            return None
        classification_text = "\n".join(tail)
        if renderer.terminal_failure_final and renderer.terminal_failure_text:
            classification_text = renderer.terminal_failure_text
        return classify_agent_failure(classification_text)

    if os.environ.get("TAUCETI_STREAM"):
        rc = run_rendered(sys.stdout)
        if rc != 0:
            _LAST_AGENT_FAILURE = classify_rendered_failure()
            report_failure(f"{label.removeprefix('agent-')} agent exited with status {rc}", code=rc)
        return rc
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log(f"{label}: output → {logf}  (run with --stream to watch live)")
    with open(logf, "a", encoding="utf-8", errors="replace") as f:
        rc = run_rendered(f)
    if rc != 0:
        log(f"{label}: exited {rc}; last lines of {logf.name}:")
        for line in tail:
            print("    " + line)
        summary = next((line.strip() for line in reversed(tail) if line.strip()), "")
        reason = f"{label.removeprefix('agent-')} agent exited with status {rc}"
        if summary:
            reason += f": {summary}"
        _LAST_AGENT_FAILURE = classify_rendered_failure()
        report_failure(reason, code=rc, log_file=logf)
    return rc


def run_to_logfile(argv: list[str], logf: Path, label: str) -> int:
    """Run a subprocess with stdout+stderr redirected to logf, keeping the worker's MAIN log clean. Used
    for the review engine, which prints a lot (git clones, the full scoreboard dump, per-rubric lines) —
    detail that belongs in a subsidiary per-review log, not the orchestration stream. TAUCETI_STREAM=1
    streams to the terminal instead. Tails logf to the main log on a non-zero exit so failures aren't
    silent. The caller logs a one-line pointer to logf so the detail is discoverable."""
    if os.environ.get("TAUCETI_STREAM"):
        return subprocess.run(argv).returncode
    logf.parent.mkdir(parents=True, exist_ok=True)
    log(f"  {label}: engine output → {logf}  (run with --stream to watch live)")
    with open(logf, "ab") as f:
        rc = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        log(f"{label}: exited {rc}; last lines of {logf.name}:")
        tail: list[str] = []
        try:
            tail = logf.read_text(errors="replace").splitlines()[-20:]
            for line in tail:
                log("    " + line)
        except OSError:
            pass
        summary = next((line.strip() for line in reversed(tail) if line.strip()), "")
        reason = f"{label} exited with status {rc}"
        if summary:
            reason += f": {summary}"
        report_failure(reason, code=rc, log_file=logf)
    return rc


def _shq(s: str) -> str:
    import shlex

    return shlex.quote(s)


# ===== Bubble authoring path (opt in with --bubble; the host is the default) =======
# The checkout, lake build, and every git/gh call happen IN a repo-scoped bubble
# container. GitHub goes through bubble's auth proxy (the host gh token never
# enters); only the one credential the work model needs is seeded; no host config
# crosses the boundary. The in-container agent invocation is a frozen contract (see agent_inner_cmd).

BUBBLE_REPO = "git+https://github.com/kim-em/bubble.git"
BUBBLE_MIN_VERSION = "0.7.30"
KIRO_BUBBLE_MIN_VERSION = "0.7.31"

# TauCeti's public, anonymous Lake artifact cache. Mathlib's separate cache is fetched by
# `lake exe cache get`; this one contains TauCeti's own main-built outputs.
#
# The custom domain, NOT the bucket's `pub-<id>.r2.dev` development URL. That development URL is
# disabled on this bucket and answers 401 for every path, root included, so every round's
# `lake cache get` failed and fell through to a from-scratch `lake build` -- silently, because a
# cache miss is non-fatal here. The custom domain is also what TauCeti's own CI publishes and reads
# through (the LAKE_CACHE_*_PUBLIC repo variables). Keep the two in step.
TAUCETI_CACHE_DOMAIN = "cache.taucetiproject.org"
TAUCETI_CACHE_SERVICE = "tauceti-public"
TAUCETI_CACHE_ARTIFACT_URL = f"https://{TAUCETI_CACHE_DOMAIN}/artifacts"
TAUCETI_CACHE_REVISION_URL = f"https://{TAUCETI_CACHE_DOMAIN}/revisions"


def bubble_cmd() -> list[str]:
    """Resolve the Bubble CLI. The uvx fallback is suitable for dry-run capability probes only;
    real sandbox rounds require an installed executable because Bubble owns host-global services."""
    import shutil

    override = os.environ.get("TAUCETI_BUBBLE")
    if override:
        return shlex_split(override)
    if shutil.which("bubble"):
        return ["bubble"]
    return ["uvx", "--from", BUBBLE_REPO, "bubble"]


@functools.lru_cache(maxsize=1)
def _bubble_open_help() -> str:
    """The resolved Bubble's `open --help`, fetched once for all capability probes."""
    try:
        p = subprocess.run([*bubble_cmd(), "open", "--help"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "") + (p.stderr or "")


def bubble_supports_allow_push() -> bool:
    """Does the resolved Bubble support repo-scoped fork pushes?"""
    return "--allow-push" in _bubble_open_help()


def bubble_supports_lake_cache_service() -> bool:
    """Does the resolved Bubble support its host-global, download-only Lake cache proxy?"""
    import re

    return re.search(r"(?<![\w-])--lake-cache-service(?=[\s=,]|$)", _bubble_open_help()) is not None


@functools.lru_cache(maxsize=1)
def tauceti_cache_unreachable_reason() -> str | None:
    """``None`` if TauCeti's public artifact cache serves anonymous reads, else why it does not.

    Probes the revision endpoint rather than trusting the URL. A healthy bucket answers 404 for a
    path that holds no object -- including its own root -- so ANY 404 here means the host is serving
    us. What we are looking for is 401/403, which is what R2 returns for a bucket whose public access
    is switched off: the host then rejects every path identically and no revision can ever be found.

    That is not hypothetical. The worker pointed at the bucket's `pub-<id>.r2.dev` development URL
    after it had been disabled, so `lake cache get` failed on every round and the non-fatal fallback
    quietly rebuilt TauCeti from source each time. A dead endpoint must stop a round in preflight,
    where it is one loud line, not 30 minutes into a build the cache existed to avoid.
    """
    import urllib.error
    import urllib.request

    url = f"{TAUCETI_CACHE_REVISION_URL}/"
    try:
        with urllib.request.urlopen(url, timeout=30):
            return None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"{url} answered {e.code} {e.reason}; the cache is not publicly readable"
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Unreachable for a reason that is not an auth wall (DNS, TLS, offline). Do not block the
        # round on it: the round's own `lake cache get` retries, and a transient network fault must
        # not be indistinguishable from a misconfigured bucket.
        log(f"could not probe TauCeti's artifact cache ({e}); continuing")
        return None


def _host_home() -> Path:
    """The login user's real home, unaffected by per-worker ``$HOME`` isolation."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path(os.path.expanduser("~"))


def bubble_cmd_is_disposable(cmd: list[str] | None = None) -> bool:
    """Whether Bubble would run from uv's disposable one-shot tool cache."""
    cmd = cmd or bubble_cmd()
    names = [Path(arg).name for arg in cmd]
    return bool(names) and (names[0] == "uvx" or names[:3] == ["uv", "tool", "run"])


def _bubble_version(cmd: list[str]) -> str:
    """Return the installed Bubble package version, or ``""`` when it cannot be read."""
    import re

    try:
        result = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\bversion\s+([^\s,]+)", (result.stdout or result.stderr or ""))
    return match.group(1) if result.returncode == 0 and match else ""


def installed_bubble_version() -> str:
    """Return the version of the exact stable Bubble command this worker would execute."""
    return _bubble_version(bubble_cmd())


def bubble_version_meets_minimum(version: str, minimum: str = BUBBLE_MIN_VERSION) -> bool:
    """Compare stable three-component Bubble versions without adding a packaging dependency."""
    import re

    pattern = r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?"
    found = re.fullmatch(pattern, version)
    required = re.fullmatch(pattern, minimum)
    if found is None or required is None:
        return False
    return tuple(map(int, found.groups()[:3])) >= tuple(map(int, required.groups()[:3]))


def _bubble_proxy_endpoint_healthy(*, newer_than: int | None = None, expected_version: str | None = None) -> bool:
    """Whether Bubble's host-global endpoint describes a live compatible daemon.

    Validate pid liveness, the fork-push capability, and (when known) the installed
    Bubble version. The version check restarts the daemon once after upgrades so
    fixes inside the proxy process take effect even when its protocol is unchanged.
    """
    import json

    endpoint_file = _host_home() / ".bubble" / "auth-proxy.endpoint"
    try:
        if newer_than is not None and endpoint_file.stat().st_mtime_ns <= newer_than:
            return False
        endpoint = json.loads(endpoint_file.read_text())
        tcp = endpoint["tcp"]
        host, port = tcp["host"], tcp["port"]
        capabilities = endpoint.get("capabilities")
        if not isinstance(capabilities, list) or "allow-push" not in capabilities:
            return False
        if expected_version is not None and endpoint.get("bubble_version") != expected_version:
            return False
        pid = endpoint.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        if not isinstance(host, str) or not host or not isinstance(port, int) or isinstance(port, bool):
            return False
        os.kill(pid, 0)
        return True
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False


def _wait_bubble_proxy_endpoint_healthy(
    *, newer_than: int | None = None, expected_version: str | None = None, timeout: float = 30
) -> bool:
    """Wait for a freshly started daemon to publish and bind its endpoint."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        if _bubble_proxy_endpoint_healthy(newer_than=newer_than, expected_version=expected_version):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _bubble_proxy_endpoint_mtime() -> int | None:
    try:
        return (_host_home() / ".bubble" / "auth-proxy.endpoint").stat().st_mtime_ns
    except OSError:
        return None


def _auth_proxy_lock_path() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / f"tauceti-worker-auth-proxy-{os.getuid()}.lock"


def ensure_fork_proxy_current() -> None:
    """Keep bubble's git auth-proxy daemon in step with the installed `bubble` CLI; Die if it can't be.

    The proxy that enforces `--allow-push` runs as a long-lived launchd/systemd daemon. Upgrading the
    `bubble` CLI does NOT restart it, so a daemon started before kim-em/bubble#320 keeps rejecting fork
    pushes with `403 Repository mismatch` even though `bubble open --help` (and so bubble_supports_allow_push)
    advertises the flag — the fork round then silently falls back to canonical (a wrong-target PR for an
    account with canonical write) or fails outright (a read-only contributor). The CLI capability probe
    can't catch this: it inspects the binary, not the running daemon.

    A healthy endpoint explicitly advertises fork-push support, so unrelated Bubble upgrades do not churn
    the shared daemon. Missing, dead, or pre-capability endpoints are refreshed through Bubble's own
    `gh proxy start`. The refresh is serialized under a host-global file lock so concurrent TauCeti workers
    do not race; Bubble separately serializes all service installers. Fail-CLOSED throughout: if the refresh
    cannot publish a fresh endpoint we Die rather than burn a long round that cannot authenticate. Call this
    ONLY for rounds that push to a fork — a review-only worker must not be blocked by it."""
    import fcntl

    # The lock lives in the always-writable temp dir, per OS user, so acquiring it effectively never fails.
    lockpath = _auth_proxy_lock_path()
    lockf = None
    try:
        try:
            lockf = open(lockpath, "w")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        except OSError:
            lockf = None  # extraordinary (temp dir unwritable) — fall through and refresh anyway
        cmd = bubble_cmd()
        if bubble_cmd_is_disposable(cmd):
            if _bubble_proxy_endpoint_healthy():
                return
            raise Die(
                "preflight: fork authoring needs Bubble installed at a stable path; the uvx fallback "
                "cannot safely own a host-global launchd/systemd daemon. Install dev-bubble or set "
                "$TAUCETI_BUBBLE to a stable Bubble executable, then re-run."
            )
        version = _bubble_version(cmd)
        if _bubble_proxy_endpoint_healthy(expected_version=version or None):
            return
        endpoint_mtime = _bubble_proxy_endpoint_mtime()
        try:
            subprocess.run([*cmd, "gh", "proxy", "start"], capture_output=True, text=True, timeout=120, check=True)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip()[-500:]
            suffix = f"\n  Bubble said: {detail}" if detail else ""
            raise Die(
                "preflight: bubble's git auth-proxy daemon lacks fork-push support and could not be "
                f"refreshed: {e}{suffix}"
            ) from e
        except (OSError, subprocess.SubprocessError) as e:
            raise Die(
                "preflight: bubble's git auth-proxy daemon lacks fork-push support and could not be "
                f"refreshed: {e}\n"
                "  Restart it yourself with `bubble gh proxy start`, then re-run."
            ) from e
        if not _wait_bubble_proxy_endpoint_healthy(newer_than=endpoint_mtime, expected_version=version or None):
            raise Die(
                "preflight: `bubble gh proxy start` returned success but did not publish a reachable "
                "fresh auth-proxy endpoint with fork-push support. Check ~/.bubble/auth-proxy.log and "
                "/tmp/bubble-auth-proxy.log, then re-run."
            )
        log("bubble auth-proxy: restarted daemon with fork --allow-push support")
    finally:
        if lockf is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
            finally:
                lockf.close()


def shlex_split(s: str) -> list[str]:
    import shlex

    return shlex.split(s)


def bubble_name(cfg: Config) -> str:
    return f"tauceti-worker-{cfg.wid}"


def bubble_home(cfg: Config) -> Path:
    env = os.environ.get("TAUCETI_BUBBLE_HOME")
    return Path(env) if env else (cfg.data_home / ".cache" / "tauceti-worker" / cfg.wid / "bubble")


def ensure_bubble_home(cfg: Config) -> dict:
    """Fail-closed hardening of the private Bubble home.

    Both shared Lean caches must be writable only through a per-round overlay. A persistent writable
    cache would let one untrusted agent poison artifacts restored by later rounds, so verify the actual
    config rather than trusting the legacy `.worker-init` sentinel.
    """
    import tomllib

    home = bubble_home(cfg)
    env = {**os.environ, "BUBBLE_HOME": str(home)}
    # Host cache paths mean nothing inside the container, and Bubble supplies the in-container Lake and
    # Mathlib cache configuration itself (through its own download-only proxy and a per-round overlay,
    # below). Drop the host redirects so they cannot reach the container if Bubble ever forwards them.
    for var in ("ELAN_HOME", "MATHLIB_CACHE_DIR", "LAKE_CACHE_DIR"):
        env.pop(var, None)
    home.mkdir(parents=True, exist_ok=True)

    def overlay_configured() -> bool:
        try:
            with open(home / "config.toml", "rb") as f:
                return tomllib.load(f).get("security", {}).get("shared_cache") == "overlay"
        except (OSError, tomllib.TOMLDecodeError):
            return False

    if not overlay_configured():
        try:
            p = subprocess.run(
                [*bubble_cmd(), "security", "set", "shared-cache", "overlay"],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise Die(f"could not configure Bubble's shared-cache overlay; refusing unsafe cache mounts: {e}") from e
        if p.returncode != 0 or not overlay_configured():
            detail = (p.stderr or p.stdout or "Bubble did not persist security.shared-cache=overlay").strip()[-300:]
            raise Die(
                "could not configure Bubble's shared-cache overlay; refusing to expose persistent "
                f"writable Lean caches to the agent. Bubble said: {detail}"
            )
    return env


def _uses_claude_credentials(work_model: str) -> bool:
    """Keep Bubble's Claude credential flag and macOS private-seed decision on one predicate."""
    models = {m.strip() for m in work_model.split(",")}
    known_non_claude = {"codex", "kiro", *OPENROUTER_MODELS}
    return bool(models & {"claude", "sonnet"}) or not models <= known_non_claude


def _copy_kiro_auth_db(src: Path, dst: Path) -> None:
    """Take a consistent snapshot of Kiro's live SQLite credential store."""
    try:
        snapshot_kiro_auth_db(src, dst)
    except UsageError as e:
        raise Die(str(e)) from e


def _uses_kiro_credentials(models: str) -> bool:
    return "kiro" in {model.strip() for model in models.split(",")}


def _stage_kiro_credentials(cfg: Config, rounddir: Path) -> None:
    """Stage exactly one Kiro auth mechanism for a read-only Bubble mount."""
    key = (os.environ.get("KIRO_API_KEY") or "").strip()
    keyf = rounddir / "kiro.key"
    keyf.write_text(key)
    os.chmod(keyf, 0o600)
    if key:
        return
    src = kiro_data_dir(cfg.home) / "data.sqlite3"
    if _safe_exists(src):
        _copy_kiro_auth_db(src, rounddir / "kiro-auth.sqlite3")


def agent_cred_flags(work_model: str) -> list[str]:
    """Bubble flags seeding ONLY the work model's credential; all config and other models' creds stay out."""
    if work_model == "codex":
        return ["--codex-credentials", "--no-codex-config", "--no-claude-credentials", "--no-claude-config"]
    if _uses_claude_credentials(work_model):
        return ["--claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"]
    return ["--no-claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"]


def _codex_model() -> str:
    """Compatibility helper for callers that only need the resolved Codex model."""
    return resolve_authoring_profile("codex").model


def agent_inner_cmd(profile: AuthoringProfile | str) -> str:
    """The command bubble runs INSIDE the container (bash -lc); a frozen contract, kept byte-for-byte
    stable: the prompt is read from the read-only /opt/round mount; *_API_KEY emptied to force
    subscription auth."""
    import shlex

    profile = _authoring_profile(profile)
    if profile.provider == "codex":
        effort_config = f'model_reasoning_effort="{profile.effort}"'
        effort = f" -c {shlex.quote(effort_config)}" if profile.effort else ""
        summary = shlex.quote('model_reasoning_summary="detailed"')
        raw_reasoning = shlex.quote("show_raw_agent_reasoning=false")
        return (
            f"env OPENAI_API_KEY= ANTHROPIC_API_KEY= codex exec --json --model "
            f"{shlex.quote(profile.model)}{effort} -c {summary} -c {raw_reasoning} "
            '--sandbox danger-full-access --skip-git-repo-check "$(cat /opt/round/prompt.txt)"'
        )
    if profile.provider in OPENROUTER_MODELS:
        return (
            'env ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENROUTER_API_KEY="$(cat /opt/round/openrouter.key)" '
            f"pi --provider openrouter --model {shlex.quote(profile.model)} --print "
            '"$(cat /opt/round/prompt.txt)"'
        )
    if profile.provider == "kiro":
        effort = f" --effort {shlex.quote(profile.effort)}" if profile.effort else ""
        setup = (
            "set -eu; "
            "export KIRO_HOME=/tmp/tauceti-kiro-home XDG_DATA_HOME=/tmp/tauceti-kiro-data; "
            'mkdir -p "$KIRO_HOME" "$XDG_DATA_HOME/kiro-cli"; '
            "if [ -s /opt/round/kiro.key ]; then "
            'export KIRO_API_KEY="$(cat /opt/round/kiro.key)"; '
            "else unset KIRO_API_KEY; "
            "test -f /opt/round/kiro-auth.sqlite3 || "
            "{ echo 'Kiro is not logged in and KIRO_API_KEY is unset' >&2; exit 2; }; "
            'cp /opt/round/kiro-auth.sqlite3 "$XDG_DATA_HOME/kiro-cli/data.sqlite3"; fi; '
            f"exec kiro-cli chat --no-interactive --trust-all-tools --model {shlex.quote(profile.model)}"
            f'{effort} "$(cat /opt/round/prompt.txt)"'
        )
        return (
            "env ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENROUTER_API_KEY= OPENROUTER_MANAGEMENT_KEY= sh -c "
            + shlex.quote(setup)
        )
    effort = f" --effort {shlex.quote(profile.effort)}" if profile.effort else ""
    return (
        'env ANTHROPIC_API_KEY= OPENAI_API_KEY= CLAUDECODE= claude -p "$(cat /opt/round/prompt.txt)" '
        f"--output-format stream-json --verbose --model {shlex.quote(profile.model)}{effort} "
        "--dangerously-skip-permissions"
    )


def bubble_work_cmd(inner: str) -> str:
    """Trusted bootstrap run before the work agent inside Bubble.

    Bubble's noninteractive `--command` mode deliberately does not run a hook-generated build, so do
    both cache fetches explicitly: Mathlib's `lake exe cache`, then Lake's built-in cache for TauCeti's
    own outputs. Bubble's login shell supplies `MATHLIB_CACHE_GET_URL` and the generated user-level Lake
    cache config for the host-global proxy. Keep a Lake-cache miss and the preliminary build non-fatal:
    fix/fix-ci/bump/rebase rounds often start from a red tree, and repairing it is the agent's job. A
    Mathlib-cache failure is fatal because compiling Mathlib would consume the round.

    A Lake-cache miss stays non-fatal but no longer stays quiet, and the two reasons for one are now
    told apart. "No outputs for this revision" is ordinary: it is every round on a commit main has not
    built yet, and there is nothing to do but build them. Any OTHER failure means the cache did not
    answer, which is infrastructure being broken rather than cold, and it used to look identical in the
    log to the ordinary case. It printed one `warning: TauCeti Lake cache miss` line and rebuilt the
    library from source, every round, for as long as the endpoint stayed down.
    """
    fetch = f"lake cache get --service {TAUCETI_CACHE_SERVICE} --repo {TAUCETI}"
    return (
        "set -e; "
        "lake exe cache get || lake exe cache get; "
        'tc_log="$(mktemp)"; tc_hit=0; tc_cold=0; '
        # Two attempts, because a dropped connection is worth one retry, but stop immediately when
        # Lake reports the revision simply is not cached: retrying cannot change that answer.
        "for _ in 1 2; do "
        f'if {fetch} >"$tc_log" 2>&1; then tc_hit=1; break; fi; '
        "if grep -q 'no outputs found' \"$tc_log\"; then tc_cold=1; break; fi; "
        "sleep 2; "
        "done; "
        'if [ "$tc_hit" != 1 ]; then '
        'if [ "$tc_cold" = 1 ]; then '
        "echo 'warning: TauCeti Lake cache holds no outputs for this revision; building them' >&2; "
        "else "
        "echo 'error: TauCeti Lake cache did not answer (not a missing revision); building TauCeti "
        "from scratch. The cache endpoint is probably broken -- this should not happen.' >&2; "
        "sed 's/^/  cache: /' \"$tc_log\" >&2; "
        "fi; "
        "fi; "
        'rm -f "$tc_log"; '
        "if ! timeout 1800 lake build; then "
        "echo 'warning: pre-agent lake build failed or timed out; the agent starts from a red tree' >&2; "
        "fi; "
        f"exec {inner}"
    )


def _bubble_pop(cfg: Config, env: dict) -> None:
    subprocess.run([*bubble_cmd(), "pop", bubble_name(cfg), "-f"], env=env, capture_output=True)


def _stage_claude_creds_for_bubble(cfg: Config) -> Path | None:
    """Return a private Claude config dir for one macOS Bubble launch, or ``None`` off macOS.

    Bubble seeds the in-container Claude from ``<CLAUDE_CONFIG_DIR>/.credentials.json`` but cannot read
    the macOS Keychain. The Keychain is authoritative and may hold tokens rotated since the prior round,
    so read it interactively and write the current blob into a mode-0700 temporary directory owned by the
    worker. ``run_in_bubble`` exposes that directory only through the Bubble subprocess environment and
    removes it after the container exits. In particular, never create or overwrite the operator's
    configured credential file: a persistent Keychain snapshot can later shadow the live Keychain in a
    host review process. If the Keychain is unavailable, copy an existing configured credential file into
    the private directory as the same fallback the old in-place handoff provided.

    The caller owns and must remove the returned directory."""
    if sys.platform != "darwin":
        return None  # Linux/Windows: the configured file is the store; Bubble reads it directly
    cfg.state.mkdir(parents=True, exist_ok=True)
    # A SIGKILL bypasses both finally and RoundContext's signal/atexit cleanup. Bound the lifetime and
    # number of those orphaned snapshots by removing them before the next staging attempt.
    for stale in cfg.state.glob("bubble-claude-seed-*"):
        shutil.rmtree(stale, ignore_errors=True)

    source = claude_dir(cfg.home) / ".credentials.json"
    blob = _claude_keychain_creds_interactive()
    if not (blob or {}).get("claudeAiOauth"):
        blob = _read_json_file(source)
        if (blob or {}).get("claudeAiOauth"):
            expires = blob["claudeAiOauth"].get("expiresAt")
            expired = (
                isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= time.time() * 1000
            )
            suffix = " (access token is already expired)" if expired else ""
            log(f"claude creds: Keychain unavailable; seeding Bubble from file snapshot {source}{suffix}")
    if not (blob or {}).get("claudeAiOauth"):
        raise Die(
            "no Claude credentials to seed the bubble: none in "
            f'{source} and could not read the "Claude Code-credentials" login Keychain item. Unlock '
            "the Keychain and retry, or drop --bubble (the host claude reads the Keychain itself)."
        )

    seed_dir = Path(tempfile.mkdtemp(prefix="bubble-claude-seed-", dir=cfg.state))
    try:
        _write_json_atomic(seed_dir / ".credentials.json", blob)
    except BaseException:
        shutil.rmtree(seed_dir, ignore_errors=True)
        raise
    return seed_dir


def run_in_bubble(
    w: Worker,
    target: str,
    prompt: str,
    opts: RoundOpts,
    mounts: list[str] | None = None,
    *,
    inner_cmd: str | None = None,
    cred_model: str | None = None,
    allow_push: str | None = None,
) -> int:
    """Open a fresh repo-scoped bubble for target, run a command inside it, pop it. By default runs the
    work agent (agent_inner_cmd) seeding the work model's credential; pass inner_cmd / cred_model to run
    something else in the same sandbox (e.g. the review engine — see review_in_bubble)."""
    import shlex

    cfg, wm = w.cfg, opts.work_model
    # Review/probe commands bring their own model policy. Do not let an unrelated authoring override
    # (including a malformed effort value) prevent those isolated commands from running.
    profile = getattr(opts, "authoring_profile", None) or resolve_authoring_profile(wm) if inner_cmd is None else None
    cred_model = cred_model or wm
    # OpenRouter agents run in the bubble: the image ships `pi` and allows openrouter.ai egress
    # (kim-em/bubble#299), and the key is staged 0600 at /opt/round/openrouter.key below.
    # Bubble honors $CLAUDE_CONFIG_DIR for its own credential seeding (kim-em/bubble#317). On macOS we
    # replace it in this subprocess env with a private, transient Keychain handoff below; the process-wide
    # value and any operator-owned credential file remain untouched.
    env = ensure_bubble_home(cfg)
    rounddir = cfg.state / "bubble-round"

    shutil.rmtree(rounddir, ignore_errors=True)
    rounddir.mkdir(parents=True, exist_ok=True)
    (rounddir / "prompt.txt").write_text(prompt)
    # Stage the write wrappers (contract §1/§4): mounted read-only at /opt/round and put on PATH inside
    # the container, so the agent's ONLY push path is the branch-CAS git-safe-push.
    for f in ("git-safe-push", "gh-safe-pr-create", "claim.sh"):
        shutil.copy(HERE / "scripts" / f, rounddir / f)
        os.chmod(rounddir / f, 0o755)
    if wm in OPENROUTER_MODELS:  # OpenRouter key has no proxy — stage it 0600, mounted read-only
        keyf = rounddir / "openrouter.key"
        keyf.write_text(os.environ.get("OPENROUTER_API_KEY", ""))
        os.chmod(keyf, 0o600)
    if _uses_kiro_credentials(cred_model):
        # Prefer API-key authentication. Otherwise snapshot the browser login;
        # never expose the live SQLite store to the untrusted container.
        _stage_kiro_credentials(cfg, rounddir)

    _bubble_pop(cfg, env)  # clear any container a SIGKILLed prior round left behind

    mount_flags = ["--mount", f"{rounddir}:/opt/round:ro"]
    for m in mounts or []:
        mount_flags += ["--mount", m]

    # Fork-PR write support (kim-em/bubble#320): grant the in-container agent git fetch/push to the
    # contributor's own fork on top of the base-scoped GitHub access, so it can push an authored branch
    # (roadmap) or a fix to a fork-headed PR. The base repo keeps its allowlist-write-graphql scope; the
    # fork gets git only. (For a PR target, bubble also auto-derives the head fork, so this is belt-and-
    # suspenders for maintenance and the sole grant for authoring, which has no PR to derive from.)
    push_flags = ["--allow-push", allow_push] if allow_push else []

    # Push-arbiter env crossing into the container: /opt/round on PATH + the branch-CAS inputs the
    # agent's git-safe-push / gh-safe-pr-create need. \$PATH stays literal so it expands to the
    # CONTAINER PATH inside bubble's bash -lc. We do NOT forward TAUCETI_CLAIM_* (the claim+heartbeat
    # are host-side; the branch CAS is the [HARD] guarantee and needs no in-container claim).
    tcenv = "env PATH=/opt/round:$PATH"
    for var in (
        "TAUCETI_PUSH_REF",
        "TAUCETI_PUSH_EXPECT",
        "TAUCETI_PUSH_REMOTE",
        "TAUCETI_TARGET_MARKER",
        "TAUCETI_REQUIRE_TARGET_MARKER",
    ):
        val = os.environ.get(var)
        if val:
            tcenv += f" {var}={shlex.quote(val)}"
    command_inner = inner_cmd if inner_cmd is not None else agent_inner_cmd(profile)
    if inner_cmd is None:
        command_inner = f"bash -c {shlex.quote(bubble_work_cmd(command_inner))}"
    command = f"{tcenv} {command_inner}"

    # Only work-agent rounds compile TauCeti. Bubble turns the two immutable public endpoints into
    # capability-scoped routes through its host-global download cache; the upstream host is not exposed
    # to the container. Review/probe commands neither compile nor need an artifact-cache capability.
    cache_flags = (
        [
            "--lake-cache-service",
            TAUCETI_CACHE_SERVICE,
            TAUCETI_CACHE_ARTIFACT_URL,
            TAUCETI_CACHE_REVISION_URL,
        ]
        if inner_cmd is None
        else []
    )

    argv = [
        *bubble_cmd(),
        "open",
        target,
        "--shell",
        "--local",
        "--name",
        bubble_name(cfg),
        "--ephemeral",
        "--github-security",
        "allowlist-write-graphql",
        *push_flags,
        *cache_flags,
        *mount_flags,
        *agent_cred_flags(cred_model),
        "--command",
        command,
    ]

    if os.environ.get("TAUCETI_AGENT_ECHO"):
        print("BUBBLE " + " ".join(_shq(a) for a in argv))
        return 0

    if allow_push and not _wait_bubble_proxy_endpoint_healthy(timeout=5):
        raise Die(
            "bubble auth-proxy became unavailable before the fork-writing round started; re-run to refresh it safely"
        )

    # Re-mirror the operator's fresh creds into the isolated home at the last moment before bubble seeds
    # the container (provider-neutral: covers codex too, and the unpaced review / probe paths that never
    # call the pacer). No-op when not isolated or on macOS.
    mirror_creds(cfg)
    # That re-mirror is the LAST thing to touch the credential before bubble seeds the container, so it
    # can hand the container an account rotated in since the round's earlier --account checks. Re-check
    # against what bubble is about to receive; this is the final gate before the container spends.
    account = getattr(opts, "account", None)
    if account and cred_model == "codex":
        problem = Quota(cfg).codex_account_problem(account)
        if problem:
            raise Die(f"bubble: {problem}")
    # On macOS, Claude Code keeps creds in the Keychain, not a file. Stage a current snapshot privately
    # and override only Bubble's subprocess env (done after the echo path so a dry-run never prompts the
    # Keychain). Register cleanup before launch for signals; the normal finally removes it promptly too.
    claude_seed: Path | None = None
    if _uses_claude_credentials(cred_model):
        claude_seed = _stage_claude_creds_for_bubble(cfg)
        if claude_seed is not None:
            env = {**env, "CLAUDE_CONFIG_DIR": str(claude_seed)}
            # Register the seed first: RoundContext cleans up LIFO, so the pop registered next runs
            # before the container's credential source disappears.
            w.rc.add_cleanup(lambda p=claude_seed: shutil.rmtree(p, ignore_errors=True))
    w.rc.add_cleanup(lambda: _bubble_pop(cfg, env))  # pop if we're killed mid-run
    try:
        if inner_cmd is None:  # the work agent — quiet/log it like the host path
            rc = run_agent_proc(
                argv,
                env=env,
                logdir=cfg.logdir,
                label=f"agent-{wm}",
                provider=profile.provider,
            )
        else:  # review engine / probe — leave its output inline
            rc = subprocess.run(argv, env=env).returncode
    finally:
        try:
            _bubble_pop(cfg, env)  # don't rely on --ephemeral alone, and pop even on an exception
        finally:
            if claude_seed is not None:
                shutil.rmtree(claude_seed, ignore_errors=True)
    return rc


def _codex_review_model_override(reviewers: str) -> str | None:
    """Independent review-model override, or None for the engine's own policy."""
    m = os.environ.get("TAUCETI_REVIEW_CODEX_MODEL")
    return m if (m and "codex" in [r.strip() for r in reviewers.split(",")]) else None


def _kiro_review_model(reviewers: str) -> str | None:
    """Exact Kiro review model, independent of authoring model policy."""
    if "kiro" not in [r.strip() for r in reviewers.split(",")]:
        return None
    configured = (os.environ.get("TAUCETI_REVIEW_KIRO_MODEL") or "").strip()
    model = configured or AUTHORING_DEFAULTS["kiro"][0]
    return _validate_kiro_model_pin(model, "$TAUCETI_REVIEW_KIRO_MODEL" if configured else "repository default")


def review_in_bubble(w: Worker, pr: int, head: str, reviewers: str, opts: RoundOpts) -> int:
    """Run the tauceti-review engine INSIDE bubble — a hard container boundary around an engine that
    reads an untrusted PR diff and runs a model on it (and, once review gains tool use, runs that
    model's tools). The repo-scoped proxy can't reach a second repo, so we pre-stage everything the
    engine would otherwise fetch and run it OFFLINE: the engine itself, the roadmap, and the review
    store are host→container mounts; `--no-sync` makes the engine archive review records to the mounted
    outbox but NOT push (the bubble can't reach TauCetiData) — do_review drains that outbox to
    TauCetiData host-side afterwards. The only traffic that
    crosses the boundary is the engine's TauCeti code clone + PR API + scoreboard post (all scoped to
    TauCeti, already allowed by the proxy) and the reviewer model's provider egress. The engine has no
    Python deps, so we run the mounted source with the image's python3 — no uvx/uv/PyPI.

    The store is mounted READ-WRITE from the worker's persistent store_dir (not /tmp): it holds the
    scoreboard/thread comment ids the next round edits in place — an ephemeral store would post a
    duplicate scoreboard. The engine mount keeps its `.git` so the engine never falls back to a
    cross-repo `gh api` for its own rev. TAUCETI_REVIEW_ENGINE_DIR pins a local engine checkout
    (operator override / pre-merge testing); otherwise a shallow REVIEW clone is staged."""
    cfg = w.cfg
    eng = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")
    engine_dir = Path(eng) if eng else (cfg.state / "refs" / "review-engine")
    if not eng and not fetch_ref(REVIEW, engine_dir):  # keeps .git (no cross-repo rev fallback)
        raise Die(f"fetch {REVIEW} failed")
    roadmap_dir = cfg.state / "refs" / "roadmap"
    if not fetch_ref(ROADMAP, roadmap_dir):
        raise Die(f"fetch {ROADMAP} failed")
    store = cfg.store_dir
    store.mkdir(parents=True, exist_ok=True)
    mounts = [f"{engine_dir}:/opt/engine:ro", f"{roadmap_dir}:/opt/roadmap:ro", f"{store}:/opt/review-store:rw"]
    # No --rubrics-sha/--shadow (they'd re-fetch TauCetiReview). --no-mathlib for now; wiring
    # --mathlib-dir at the bubble's vendored .lake/packages/mathlib is the reuse-rubric refinement.
    # run_in_bubble prefixes `env PATH=… `, so the inner command must start with an executable, not the
    # `cd` shell builtin — carry the engine on PYTHONPATH instead of cwd (cwd is irrelevant: the engine
    # uses --repo-dir for its files and an absolute temp workdir).
    import shlex

    cm = _codex_review_model_override(reviewers)
    codex_flag = f" --codex-model {shlex.quote(cm)}" if cm else ""  # operator override; else engine default
    km = _kiro_review_model(reviewers)
    kiro_flag = f" --kiro-model {shlex.quote(km)}" if km else ""
    inner = (
        "env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/engine python3 -m runner.cli "
        f"{pr} --repo {TAUCETI} --repo-dir /opt/engine --roadmap-dir /opt/roadmap "
        f"--no-mathlib --no-sync --store /opt/review-store --post "
        f"--max-rounds-per-day {REVIEW_DAILY_CAP} "  # one value drives the survey prefilter + engine
        f"--reviewer {reviewers} --expect-head {head} --submitted-by {me()}{codex_flag}{kiro_flag}"
    )
    if km:
        # The review engine creates another clean reviewer HOME. Seed this
        # container's provider-only credential first so that clean-room copy has
        # a source, while keeping the operator's live database outside Bubble.
        setup = (
            "set -eu; export KIRO_HOME=/tmp/tauceti-kiro-home "
            "XDG_DATA_HOME=/home/user/.local/share; "
            'mkdir -p "$KIRO_HOME" "$XDG_DATA_HOME/kiro-cli"; '
            "if [ -s /opt/round/kiro.key ]; then "
            'export KIRO_API_KEY="$(cat /opt/round/kiro.key)"; '
            "else unset KIRO_API_KEY; "
            "test -f /opt/round/kiro-auth.sqlite3 || "
            "{ echo 'Kiro is not logged in and KIRO_API_KEY is unset' >&2; exit 2; }; "
            'cp /opt/round/kiro-auth.sqlite3 "$XDG_DATA_HOME/kiro-cli/data.sqlite3"; fi; '
            f"exec {inner}"
        )
        inner = "sh -c " + shlex.quote(setup)
    # target is the PR so bubble checks it out; prompt unused by the engine.
    return run_in_bubble(w, f"{TAUCETI}/pull/{pr}", "", opts, mounts=mounts, inner_cmd=inner, cred_model=reviewers)


def _worker_iso_home(wid: str, _base: Path | None = None) -> Path:
    """The per-worker isolated home: on macOS the parent of the isolated $CLAUDE_CONFIG_DIR and
    $CODEX_HOME, elsewhere the isolated $HOME itself.

    The macOS location is kept SHORT and anchored at the real login user's home (via `pwd`, NOT $HOME).
    That began as a hard requirement — bubble ran the sandbox in a colima VM whose lima/incus unix
    sockets nested under the isolated $HOME, and the location beneath the installed package
    (site-packages) pushed those socket paths past UNIX_PATH_MAX (104), so colima refused to start
    ("instance name … too long"). isolate_home no longer moves $HOME on macOS, so those sockets nest
    under the operator's real home and the bound no longer binds anything. The path is unchanged anyway:
    workers already hold their codex credential copy and their creds-source markers here, and moving it
    would strand them. The per-worker component stays bounded and deterministic for the same reason it
    always was — a long --worker-id or login name must still produce a stable, recomputable path.

    Linux keeps the in-tree location beside the worker's other state. Either way the path must be a pure
    function of wid (no $HOME), so a loop child recomputes the same one its parent isolated to."""
    if sys.platform != "darwin":
        return HERE / "state" / wid / "home"
    base = _base
    if base is None:
        try:
            import pwd

            base = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            base = Path(os.path.expanduser("~"))
    root = base / ".tauceti"
    # colima binds <home>/.colima/_lima/<profile>/ssh.sock.<16-digit id>; keep that whole path strictly
    # under UNIX_PATH_MAX (104) by bounding the per-worker component.
    sock_suffix = len("/.colima/_lima/colima-bubble-colima/ssh.sock.") + 16
    budget = (104 - 1 - sock_suffix) - len(str(root)) - 1  # max home length, minus root and its trailing "/"
    if len(wid) <= budget:
        return root / wid
    import hashlib

    digest = hashlib.sha1(wid.encode()).hexdigest()[:8]
    keep = max(1, budget - 9)  # leave room for "-" + 8 hex chars
    return root / f"{wid[:keep]}-{digest}"


def share_build_caches(wid: str, data_home: Path) -> dict[str, str]:
    """Settle where this worker's Lean build caches live: what it shares, and what stays its own.

    Toolchains are shared. Every worker installs the same ones, an install takes a per-toolchain lock
    and lands by rename, and 22 installs covering 6 distinct toolchains is 46 GB of duplicate disk and
    a redundant download per worker per bump. So `ELAN_HOME` points at the login user's `~/.elan`,
    where the operator's own toolchains already are.

    Lake's cache is NOT shared, even though it normally sits under the toolchain directory and would
    otherwise follow `ELAN_HOME` there. It is written throughout a build rather than once at install,
    and that its concurrent writers are safe is not established; `LAKE_CACHE_DIR` therefore stays in
    the worker's own data root. That also keeps a per-worker experiment with `LAKE_ARTIFACT_CACHE`
    honest, since the store it fills is then that worker's alone.

    Mathlib's `.ltar` cache also stays per-worker — as the download target. `lake exe cache get` takes
    no lock and, in any checkout older than mathlib4#42752, writes fixed-name temporaries, so pointing
    two workers at one directory risks a corrupt `.ltar` under a name every later run trusts. It is
    pooled instead by hardlink, before the agent starts; see `tauceti_worker.build_caches`.

    Called on both isolate_home() paths, so a round child of a loop that predates this still gets it,
    and resolved through `_host_home()` (pwd, not $HOME) so it computes the same answer either way. An
    operator-supplied value always wins. Returns the resolved mapping, for logging."""
    host = _host_home()
    os.environ.setdefault("ELAN_HOME", str(build_caches.elan_pool(host)))
    os.environ.setdefault("MATHLIB_CACHE_DIR", str(data_home / ".cache" / "mathlib"))
    os.environ.setdefault("LAKE_CACHE_DIR", str(data_home / ".cache" / "lake"))
    return {var: os.environ[var] for var in ("ELAN_HOME", "MATHLIB_CACHE_DIR", "LAKE_CACHE_DIR")}


# Operator config that is tooling the WORKER runs, not instructions the AGENT reads. Symlinked into
# the isolated config dir as before: none of it reaches a prompt.
_MIRRORED_TOOLING = ("swap-account", "bin", "config.json")

# The one skill the worker itself dispatches through: $PI_RUN defaults to
# ~/.claude/skills/pi/scripts/run.sh, and in a loop child `~` is already the isolated home, so the
# OpenRouter agents need this path to resolve. Symlinked individually rather than by exposing the
# whole skills dir.
_WORKER_SKILL = "pi"

# What the isolated config dir gets instead of the operator's settings.json. Only the settings that
# change how an UNATTENDED round behaves; --model / --effort / --dangerously-skip-permissions come
# from host_agent_argv, so they are deliberately absent rather than a second source of truth.
_WORKER_CLAUDE_SETTINGS = {
    # The isolated .claude/projects/*.jsonl is the only per-turn record of what a claude round did;
    # the worker's own logs/agent-*.log is a rendered summary. Keep both.
    "cleanupPeriodDays": 36500,
    "permissions": {"defaultMode": "bypassPermissions"},
    # An unattended round has no one to show an artifact to, and publishing one is outward-facing.
    "enableArtifact": False,
    "alwaysThinkingEnabled": True,
    # host_agent_argv already passes --dangerously-skip-permissions, and a clean home was verified to
    # run a tool-using headless round without this. Set anyway: it is the documented setting for
    # accepting that mode without a human, it costs nothing, and the alternative failure — every
    # claude round stopping at an acceptance prompt nobody can answer — is not one to discover in
    # production.
    "skipDangerousModePermissionPrompt": True,
}


def _config_sources(real_claude: Path | None, iso_claude: Path) -> tuple[Path, ...]:
    """Every config dir a symlink in `iso_claude` could have been created from, newest first.

    The current one, plus the one recorded in `.tauceti-creds-source` when this home was first
    seeded. They differ when a worker id is relaunched against a different `$CLAUDE_CONFIG_DIR`, and
    without the recorded one a stale symlink into the ORIGINAL directory would survive the migration
    and keep feeding that operator's instructions to the round. `real_claude` is None on the
    early-return path, where `$CLAUDE_CONFIG_DIR` has already been repointed at `iso_claude` and the
    marker is the only surviving record of where the credential came from."""
    out = [] if real_claude is None else [real_claude]
    try:
        recorded = (iso_claude / ".tauceti-creds-source").read_text().strip()
    except OSError:
        recorded = ""
    if recorded and Path(recorded) not in out:
        out.append(Path(recorded))
    return tuple(out)


def _ours(dst: Path, item: str, sources: tuple[Path, ...]) -> bool:
    """True when `dst` is a symlink WE created into one of the operator's config dirs. A real file,
    or a symlink the operator pointed somewhere else, is theirs and is never touched."""
    if not dst.is_symlink():
        return False
    try:
        target = Path(os.readlink(dst))
    except OSError:
        return False
    return any(target == src / item for src in sources)


def seed_worker_claude_config(real_claude: Path | None, iso_claude: Path) -> None:
    """Give the worker's Claude config dir its own instruction surface, not the operator's.

    isolate_home used to symlink `CLAUDE.md`, `settings.json` and `skills` from the operator's real
    config dir, so every authoring round loaded whatever that operator happens to keep there. On this
    fleet that is a 7 KB personal CLAUDE.md and 63 personal skills — roughly 5,500 tokens per round of
    Kindle, WhatsApp and mathlib-fork instructions — and several of its rules contradict the task
    prompt outright (a different PR-body sign-off, "always clone to /tmp for a repository that isn't
    the cwd", "'draft a reply' means show it to me first" against a fix round that must post its
    contest). It also made a round's behaviour depend on who ran it.

    This is the same clean-room the review engine already builds for a reviewer, and for the same
    stated reason: seed the credential, not the personality. Tooling the worker itself shells out to
    is still symlinked (none of it reaches a prompt), and so is the single `pi` skill $PI_RUN
    resolves through.

    Idempotent, and safe to call on both isolate_home paths — which it must be, because a round child
    of a loop parent started before this code exists reaches only the early-return one, and would
    otherwise keep the operator's config until the whole fleet restarts.

    What we created, we may replace: a symlink into any config dir this home was seeded from
    (_config_sources), and a `settings.json` still byte-identical to the one we generated. Anything
    the operator put there by hand — a real file, a symlink somewhere else, an edited settings.json —
    is left exactly as found, in both directions. `$TAUCETI_INHERIT_CLAUDE_CONFIG=1` goes back to
    mirroring the operator's config, including from a home already seeded the clean way."""
    sources = _config_sources(real_claude, iso_claude)
    src_dir = sources[0] if sources else None

    for item in _MIRRORED_TOOLING:
        dst = iso_claude / item
        if src_dir is not None and _safe_exists(src_dir / item) and not dst.exists():
            try:
                dst.symlink_to(src_dir / item)
            except OSError:
                pass

    if os.environ.get("TAUCETI_INHERIT_CLAUDE_CONFIG") == "1":
        # Undo our own generated surface first, or the escape hatch silently does nothing on a home
        # that has already run once the clean way: the entries exist, so a plain "link if absent"
        # never replaces them.
        _discard_generated(iso_claude)
        for item in ("skills", "settings.json", "CLAUDE.md"):
            dst = iso_claude / item
            if src_dir is not None and _safe_exists(src_dir / item) and not dst.exists():
                try:
                    dst.symlink_to(src_dir / item)
                except OSError:
                    pass
        return

    # Drop the inherited instruction surface. Only ever a symlink we created.
    for item in ("CLAUDE.md", "settings.json", "skills"):
        dst = iso_claude / item
        if _ours(dst, item, sources):
            try:
                dst.unlink()
            except OSError:
                pass

    # One skill, not the operator's shelf. A pre-existing real skills dir is left alone.
    skills = iso_claude / "skills"
    if not skills.exists() and not skills.is_symlink():
        try:
            skills.mkdir(parents=True, exist_ok=True)
            for src in sources:
                if _safe_exists(src / "skills" / _WORKER_SKILL):
                    (skills / _WORKER_SKILL).symlink_to(src / "skills" / _WORKER_SKILL)
                    break
        except OSError:
            pass

    # Written once, so an operator can tune a worker by editing it in place.
    settings = iso_claude / "settings.json"
    if not settings.exists() and not settings.is_symlink():
        try:
            _write_json_atomic(settings, _WORKER_CLAUDE_SETTINGS)
        except OSError:
            pass


def _discard_generated(iso_claude: Path) -> None:
    """Remove the entries this module generates, and only while they are still exactly as generated.

    An operator who edited the settings.json we wrote has made it theirs; it is preserved and the
    inherit symlink is simply not created over it, which is visible rather than silent. The skills
    directory counts as generated only while it holds nothing but our own single symlink."""
    settings = iso_claude / "settings.json"
    try:
        if settings.is_file() and not settings.is_symlink():
            if json.loads(settings.read_text()) == _WORKER_CLAUDE_SETTINGS:
                settings.unlink()
    except (OSError, ValueError):
        pass
    skills = iso_claude / "skills"
    try:
        if skills.is_dir() and not skills.is_symlink():
            entries = list(skills.iterdir())
            if all(e.name == _WORKER_SKILL and e.is_symlink() for e in entries):
                for e in entries:
                    e.unlink()
                skills.rmdir()
    except OSError:
        pass


def isolate_home(wid: str) -> Path:
    """Give this worker its OWN $HOME so its credentials can't race other workers or the operator (Codex
    review / the --isolate-home flag). Gives the config dir its own agent-facing surface rather than
    the operator's (seed_worker_claude_config) and symlinks the worker's own tooling from the real
    config dir; copies the mutable Claude/Codex auth files in ONCE, then records the source dirs in
    .tauceti-creds-source markers so mirror_creds() can re-mirror a fresher access token whenever the
    operator's external refresher rotates it. The worker itself never refreshes (never touches the
    single-use refresh token). The copy always lives at <home>/.claude and $CLAUDE_CONFIG_DIR is repointed
    there, so both the pacer and the spawned claude read the isolated creds even when the operator's real
    config dir is elsewhere. Returns the worker home and sets $HOME. Children inherit.

    macOS does NOT move $HOME. Both Claude Code and gh keep their credentials in the login Keychain,
    which `security` resolves through $HOME, so repointing it made both unreachable: the pacer found no
    Claude creds and parked every non-default worker in a 300s sleep for ever (#135, Jeremy Kahn), and
    gh lost its token (Bryan's report) until a $GH_TOKEN seed was bolted on to compensate. Nothing was
    bought in exchange. The Keychain is ONE per-login-user store, so the credential copy never isolated
    Claude on macOS in the first place — which is why mirror_creds() returns early there and the pacer
    reads the Keychain ahead of any file. So on macOS isolate the two things that genuinely are
    per-worker and are addressed by environment variable, $CLAUDE_CONFIG_DIR and $CODEX_HOME, and leave
    $HOME alone. Workers share the one Claude account there, which is what was already happening.

    Returns the worker home. Children inherit the exported variables (and, off macOS, $HOME)."""
    import shutil

    home = _worker_iso_home(wid)
    iso_claude, iso_codex = home / ".claude", home / ".codex"
    iso_kiro_home, iso_kiro_data = home / ".kiro", kiro_data_dir(home, {})
    # Idempotence: a loop child inherits its parent's isolation and must not re-copy or re-warn. The
    # signal is one sentinel naming the isolation root, not any individual redirect: keying on
    # $CLAUDE_CONFIG_DIR alone would return early for an operator who happens to export that path,
    # leaving codex unisolated and the directories uncreated, and keying on $HOME alone cannot work
    # on macOS where $HOME deliberately does not move. The sentinel is written last, so it means
    # "isolation completed", and a partially built environment re-runs the whole setup.
    if os.environ.get("TAUCETI_DATA_HOME") == str(home):
        # Reassert the redirects rather than trusting them: they are what every credential read
        # resolves through, and a child that lost one would silently use the operator's account.
        # The build caches are asserted here too, so a round child running this code under a loop
        # parent that predates it still pools its downloads.
        share_build_caches(wid, home)
        # Same reason, same shape: a round child of a loop parent started before this code exists
        # would otherwise keep the operator's CLAUDE.md/settings/skills until the whole fleet is
        # restarted, and this is the one place such a child runs. Idempotent, a few stat calls.
        seed_worker_claude_config(
            None, iso_claude
        )  # $CLAUDE_CONFIG_DIR already repoints here; the marker knows the source
        os.environ["CLAUDE_CONFIG_DIR"] = str(iso_claude)
        os.environ["CODEX_HOME"] = str(iso_codex)
        os.environ["TAUCETI_KIRO_HOME"] = str(iso_kiro_home)
        os.environ["TAUCETI_KIRO_DATA_DIR"] = str(iso_kiro_data)
        if sys.platform == "darwin":
            os.environ["TAUCETI_KIRO_PROCESS_HOME"] = str(home)
        else:
            os.environ["TAUCETI_KIRO_XDG_DATA_HOME"] = str(iso_kiro_data.parent)
        return home
    real = Path(os.environ.get("HOME", os.path.expanduser("~")))
    real_claude = claude_dir(real)  # honors the operator's $CLAUDE_CONFIG_DIR before we repoint it
    real_kiro_data = kiro_data_dir(real)
    iso_claude.mkdir(parents=True, exist_ok=True)
    iso_codex.mkdir(parents=True, exist_ok=True)
    iso_kiro_home.mkdir(parents=True, exist_ok=True)
    iso_kiro_data.mkdir(parents=True, exist_ok=True)
    seed_worker_claude_config(real_claude, iso_claude)
    for f in (".credentials.json", ".gist-id", ".gist-encryption-key"):
        src, dst = real_claude / f, iso_claude / f
        if _safe_exists(src) and not dst.exists():
            shutil.copy2(src, dst)
    # The initial copy is once-only; thereafter mirror_creds() RE-MIRRORS a fresher access token from the
    # source whenever the operator's external refresher rotates it (the worker never refreshes its own
    # tokens). So a reused --worker-id stays pinned to whatever account it was first seeded from. Record the
    # source and warn if it changes, rather than silently pacing/running the stale account.
    marker = iso_claude / ".tauceti-creds-source"
    if marker.exists():
        if marker.read_text().strip() != str(real_claude):
            log(
                f"WARNING: worker '{wid}' keeps Claude creds first copied from {marker.read_text().strip()} "
                f"(not {real_claude}); use a fresh --worker-id to switch accounts."
            )
    else:
        marker.write_text(str(real_claude))
    # Honour an operator-supplied $CODEX_HOME, exactly as real_claude honours $CLAUDE_CONFIG_DIR
    # above. Reading the literal <home>/.codex would seed the isolated dir from a directory the
    # operator may not be using, and since we then repoint $CODEX_HOME at our own copy, the worker
    # would end up authenticated nowhere. Must be captured BEFORE the repoint below.
    real_codex = codex_dir(real)
    src, dst = real_codex / "auth.json", iso_codex / "auth.json"
    if _safe_exists(src) and not dst.exists():
        shutil.copy2(src, dst)
    # Record the real ~/.codex so mirror_creds() can re-mirror the codex token too (the Claude marker only
    # names the Claude source). Written unconditionally so homes seeded before this marker existed get it
    # backfilled on their next isolate_home() run.
    codex_marker = iso_codex / ".tauceti-creds-source"
    if not codex_marker.exists():
        try:
            codex_marker.write_text(str(real_codex))
        except OSError:
            pass
    # Kiro's browser login is a SQLite database outside KIRO_HOME. Give each
    # worker a consistent snapshot, but do not let an absent or malformed Kiro
    # login break a worker that may only use Codex or Claude.
    kiro_src, kiro_dst = real_kiro_data / "data.sqlite3", iso_kiro_data / "data.sqlite3"
    if _safe_exists(kiro_src) and not kiro_dst.exists():
        try:
            _copy_kiro_auth_db(kiro_src, kiro_dst)
        except Die as e:
            log(f"WARNING: {e}; Kiro browser authentication was not isolated")
    kiro_marker = iso_kiro_data / ".tauceti-creds-source"
    if kiro_marker.exists():
        if kiro_marker.read_text().strip() != str(real_kiro_data):
            log(
                f"WARNING: worker '{wid}' keeps Kiro creds first copied from "
                f"{kiro_marker.read_text().strip()} (not {real_kiro_data}); use a fresh "
                "--worker-id to switch accounts."
            )
    else:
        kiro_marker.write_text(str(real_kiro_data))
    # Both credential dirs are addressed by environment variable, and both CLIs honour the same ones the
    # worker's own claude_dir()/codex_dir() read, so the pacer and the spawned agent always agree. These
    # are the WHOLE isolation on macOS, and they ride alongside the $HOME move elsewhere.
    os.environ["CLAUDE_CONFIG_DIR"] = str(iso_claude)
    os.environ["CODEX_HOME"] = str(iso_codex)
    os.environ["TAUCETI_KIRO_HOME"] = str(iso_kiro_home)
    os.environ["TAUCETI_KIRO_DATA_DIR"] = str(iso_kiro_data)
    if sys.platform == "darwin":
        # Change HOME only in Kiro subprocesses so that its native macOS data
        # path resolves to the private profile without disrupting Keychain/gh.
        os.environ["TAUCETI_KIRO_PROCESS_HOME"] = str(home)
    else:
        os.environ["TAUCETI_KIRO_XDG_DATA_HOME"] = str(iso_kiro_data.parent)
    # Which build caches this worker shares with the machine and which stay its own; see
    # share_build_caches(). Set on both platforms, and before the sentinel below.
    caches = share_build_caches(wid, home)
    log("build caches: " + ", ".join(f"{var}={path}" for var, path in caches.items()))
    # The worker's data root, wherever $HOME ends up pointing. Config.resolve hangs the review store,
    # the bubble home and the claim scratch off this, so those stay per-worker and stay put on macOS
    # even though $HOME no longer moves. Written last: it doubles as the completion sentinel above.
    os.environ["TAUCETI_DATA_HOME"] = str(home)
    if sys.platform == "darwin":
        # Leave $HOME at the operator's, so `security` keeps resolving the login Keychain for the pacer,
        # for the spawned claude, and for gh. See the docstring for why moving it cost three separate
        # workarounds and isolated nothing.
        log(f"isolated config for worker '{wid}': CLAUDE_CONFIG_DIR={iso_claude}, CODEX_HOME={iso_codex}")
        return home
    # Keep host-side gh and git working under the isolated $HOME: the survey's `gh pr list` and host
    # pushes run in this $HOME, but their config (unlike Claude/Codex tokens) doesn't refresh-race, so
    # point them back at the operator's real config rather than an empty isolated one. Respect a value
    # the operator already exported. Children inherit these, so the early-return path above is covered.
    gh_cfg = real / ".config" / "gh"
    if gh_cfg.is_dir():
        os.environ.setdefault("GH_CONFIG_DIR", str(gh_cfg))
    git_cfg = real / ".gitconfig"
    if git_cfg.exists():
        os.environ.setdefault("GIT_CONFIG_GLOBAL", str(git_cfg))
    os.environ["HOME"] = str(home)
    log(f"isolated HOME={home} (worker '{wid}')")
    return home
