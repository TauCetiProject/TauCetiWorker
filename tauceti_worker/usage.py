"""Read-only credit telemetry for explicit, unpaced providers.

Kiro exposes subscription credit usage through an ACP extension. OpenRouter
publishes separate endpoints for the active inference key and (when supplied) a
management key's account-wide credit balance. None of these readings selects or
paces a provider; they are observability only.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path


class UsageError(RuntimeError):
    """A provider's read-only usage query could not be completed."""


def kiro_data_dir(home: Path, env: dict[str, str] | None = None) -> Path:
    """Directory containing Kiro's browser-auth SQLite store."""
    env = os.environ if env is None else env
    exact = env.get("TAUCETI_KIRO_DATA_DIR")
    if exact:
        return Path(exact)
    if sys.platform == "darwin":
        # Kiro follows macOS's native application-data location; XDG_DATA_HOME
        # is not a reliable redirect for the proprietary CLI on this platform.
        base = home / "Library" / "Application Support"
    elif xdg := env.get("TAUCETI_KIRO_XDG_DATA_HOME") or env.get("XDG_DATA_HOME"):
        base = Path(xdg)
    else:
        base = home / ".local" / "share"
    return base / "kiro-cli"


def snapshot_kiro_auth_db(src: Path, dst: Path) -> None:
    """Take a mode-0600, transactionally consistent snapshot of Kiro auth."""
    import sqlite3

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        dst.touch(mode=0o600)
        with closing(
            sqlite3.connect("file:" + urllib.parse.quote(str(src.resolve())) + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(dst)) as target:
                source.backup(target)
        os.chmod(dst, 0o600)
    except (OSError, sqlite3.Error) as e:
        dst.unlink(missing_ok=True)
        raise UsageError(f"could not snapshot Kiro credentials from {src}: {e}") from e


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _burn_estimate(remaining: float | None, burn_rate: float | None) -> float | None:
    if remaining is None or burn_rate is None or burn_rate <= 0:
        return None
    return remaining / burn_rate


def _normalized_balance(
    *,
    used,
    limit,
    unit: str,
    burn_rate: float | None = None,
    provider_percentage=None,
) -> dict:
    """Normalize a balance without rounding away provider precision.

    Kiro's `percentage` is an integer display value (for example 1 for
    0.9/50, whose actual percentage is 1.8). Preserve it for diagnosis but
    compute the percentage used by automation from the fractional values.
    """
    used_n, limit_n = _number(used), _number(limit)
    remaining = max(0.0, limit_n - used_n) if used_n is not None and limit_n is not None else None
    percentage = (used_n * 100.0 / limit_n) if used_n is not None and limit_n and limit_n > 0 else None
    return {
        "used": used_n,
        "limit": limit_n,
        "remaining": remaining,
        "percentage": percentage,
        "provider_percentage": _number(provider_percentage),
        "unit": unit,
        "burn_rate": burn_rate,
        "estimated_rounds_remaining": _burn_estimate(remaining, burn_rate),
    }


def kiro_process_env(
    base: dict[str, str] | None = None,
    *,
    private_root: Path | None = None,
    force_private: bool = False,
) -> dict[str, str]:
    """Environment for Kiro that makes API-key precedence deterministic.

    Kiro normally prefers a persisted browser login over KIRO_API_KEY. Its
    browser credential lives outside KIRO_HOME (XDG data on Linux and native
    application data on macOS), so API-key runs must isolate both locations.
    """
    env = dict(os.environ if base is None else base)
    kiro_key = (env.get("KIRO_API_KEY") or "").strip()
    if kiro_key:
        env["KIRO_API_KEY"] = kiro_key
    else:
        env.pop("KIRO_API_KEY", None)
    for key in ("TAUCETI_CLAUDE_USAGE_CREDENTIALS", "TAUCETI_CLAUDE_USAGE_OPERATOR_HOME"):
        env.pop(key, None)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY"):
        env.pop(key, None)
    if env.get("TAUCETI_KIRO_HOME"):
        env["KIRO_HOME"] = env["TAUCETI_KIRO_HOME"]
    if env.get("TAUCETI_KIRO_PROCESS_HOME"):
        env["HOME"] = env["TAUCETI_KIRO_PROCESS_HOME"]
    if sys.platform != "darwin" and env.get("TAUCETI_KIRO_DATA_DIR"):
        env["XDG_DATA_HOME"] = str(Path(env["TAUCETI_KIRO_DATA_DIR"]).parent)
    elif sys.platform != "darwin" and env.get("TAUCETI_KIRO_XDG_DATA_HOME"):
        env["XDG_DATA_HOME"] = env["TAUCETI_KIRO_XDG_DATA_HOME"]
    if force_private or kiro_key:
        root = private_root
        if root is None:
            state = env.get("TAUCETI_DATA_HOME")
            root = Path(state) / "kiro-api-profile" if state else Path(env.get("HOME", ".")) / ".tauceti-kiro-api"
        env["KIRO_HOME"] = str(root / "home")
        if sys.platform == "darwin":
            env["HOME"] = str(root / "profile")
            env.pop("XDG_DATA_HOME", None)
        else:
            env["XDG_DATA_HOME"] = str(root / "data")
        env["TAUCETI_KIRO_DATA_DIR"] = str(
            kiro_data_dir(Path(env["HOME"]), {k: v for k, v in env.items() if k != "TAUCETI_KIRO_DATA_DIR"})
        )
    return env


def _kiro_acp_usage(*, timeout: float = 30.0, env: dict[str, str] | None = None) -> dict:
    """Execute Kiro's undocumented `usage` slash command over documented ACP.

    The command value is a tagged object, not the string ``"/usage"``. This
    protocol exchange creates an empty session but sends no model prompt.
    """
    with tempfile.TemporaryDirectory(prefix="tauceti-kiro-usage-") as tmp:
        tmp_path = Path(tmp)
        child_env = dict(os.environ if env is None else env)
        source_home = Path(child_env.get("HOME", os.path.expanduser("~")))
        auth_source = kiro_data_dir(source_home, child_env) / "data.sqlite3"
        child_env = kiro_process_env(child_env, private_root=tmp_path / "private-profile", force_private=True)
        child_env["KIRO_HOME"] = str(tmp_path / "kiro-home")
        try:
            auth_source_exists = auth_source.is_file()
        except OSError:
            auth_source_exists = False
        if not (child_env.get("KIRO_API_KEY") or "").strip() and auth_source_exists:
            snapshot_kiro_auth_db(
                auth_source,
                kiro_data_dir(Path(child_env["HOME"]), child_env) / "data.sqlite3",
            )
        with tempfile.TemporaryFile(mode="w+b") as stderr:
            try:
                proc = subprocess.Popen(
                    ["kiro-cli", "acp"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    cwd=tmp,
                    env=child_env,
                    close_fds=True,
                )
            except FileNotFoundError as e:
                raise UsageError("kiro-cli is not on PATH") from e
            except OSError as e:
                raise UsageError(f"could not start kiro-cli acp: {e}") from e

            assert proc.stdin is not None and proc.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            buffered = bytearray()
            deadline = time.monotonic() + timeout

            def send(payload: dict) -> None:
                proc.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
                proc.stdin.flush()

            def receive(request_id: int) -> dict:
                while True:
                    while b"\n" in buffered:
                        raw, _, rest = buffered.partition(b"\n")
                        buffered[:] = rest
                        try:
                            message = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(message, dict):
                            continue
                        if message.get("id") == request_id and "method" not in message:
                            return message
                        # Usage is deliberately read-only and prompt-free. Refuse
                        # any reverse request instead of letting the agent wait for
                        # an implicit permission or filesystem callback.
                        if "id" in message and isinstance(message.get("method"), str):
                            send(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "TauCeti usage does not service ACP agent requests",
                                    },
                                }
                            )
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        raise UsageError(f"kiro-cli ACP usage query timed out after {timeout:g}s")
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise UsageError("kiro-cli ACP closed stdout before answering the usage query")
                    buffered.extend(chunk)

            try:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": 1,
                            "clientCapabilities": {},
                            "clientInfo": {"name": "tauceti", "version": "0.1.0"},
                        },
                    }
                )
                initialized = receive(0)
                if "error" in initialized:
                    raise UsageError(f"kiro-cli ACP initialize failed: {initialized['error']}")
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "session/new",
                        "params": {"cwd": tmp, "mcpServers": []},
                    }
                )
                session_message = receive(1)
                session_result = session_message.get("result") or {}
                session_id = session_result.get("sessionId") or session_result.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise UsageError(f"kiro-cli ACP session/new returned no session id: {session_message}")
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "_kiro.dev/commands/execute",
                        "params": {
                            "sessionId": session_id,
                            "command": {"command": "usage", "args": {}},
                        },
                    }
                )
                usage_message = receive(2)
                if "error" in usage_message:
                    raise UsageError(f"kiro-cli ACP usage command failed: {usage_message['error']}")
                result = usage_message.get("result")
                if not isinstance(result, dict):
                    raise UsageError(f"kiro-cli ACP usage returned an invalid result: {usage_message}")
                if result.get("success") is not True:
                    raise UsageError(str(result.get("message") or "Kiro usage command was unsuccessful"))
                return result
            except (BrokenPipeError, OSError) as e:
                raise UsageError(f"kiro-cli ACP transport failed: {e}") from e
            finally:
                selector.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)


def kiro_usage(*, burn_rate: float | None = None, timeout: float = 30.0, env=None) -> dict:
    result = _kiro_acp_usage(timeout=timeout, env=env)
    data = result.get("data")
    if not isinstance(data, dict):
        raise UsageError("Kiro usage response contains no data object")
    breakdowns = []
    for item in data.get("usageBreakdowns") or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_balance(
            used=item.get("used"),
            limit=item.get("limit") if item.get("hasLimit", True) else None,
            unit="credits",
            burn_rate=burn_rate,
            provider_percentage=item.get("percentage"),
        )
        breakdowns.append({**item, **normalized})
    return {
        "ok": True,
        "source": "kiro-acp",
        "plan_name": data.get("planName"),
        "billing_cycle_reset": data.get("billingCycleReset"),
        "overages_enabled": data.get("overagesEnabled"),
        "overage_capable": data.get("overageCapable"),
        "is_enterprise": data.get("isEnterprise"),
        "usage_breakdowns": breakdowns,
        "bonus_credits": data.get("bonusCredits") or [],
        "add_on_credits": data.get("addOnCredits") or [],
        "message": result.get("message"),
    }


def _http_json(url: str, key: str, *, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = None
        raise UsageError(f"OpenRouter usage endpoint returned HTTP {e.code}{': ' + detail if detail else ''}") from e
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise UsageError(f"OpenRouter usage fetch failed: {e}") from e
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise UsageError("OpenRouter usage endpoint returned an invalid payload")
    return value["data"]


def openrouter_usage(*, burn_rate: float | None = None, timeout: float = 15.0, env=None) -> dict:
    env = os.environ if env is None else env
    api_key = (env.get("OPENROUTER_API_KEY") or "").strip()
    management_key = (env.get("OPENROUTER_MANAGEMENT_KEY") or "").strip()
    if not api_key and not management_key:
        raise UsageError("set OPENROUTER_API_KEY or OPENROUTER_MANAGEMENT_KEY")

    out = {
        "ok": True,
        "source": "openrouter-api",
        "key": None,
        "account": None,
        "account_unavailable_reason": None,
    }
    if api_key:
        data = _http_json("https://openrouter.ai/api/v1/key", api_key, timeout=timeout)
        limit = data.get("limit")
        used = data.get("usage")
        normalized = _normalized_balance(used=used, limit=limit, unit="USD", burn_rate=burn_rate)
        # OpenRouter directly returns limit_remaining. Prefer it when present,
        # including keys whose reset semantics make subtraction non-obvious.
        direct_remaining = _number(data.get("limit_remaining"))
        if direct_remaining is not None:
            normalized["remaining"] = direct_remaining
            normalized["estimated_rounds_remaining"] = _burn_estimate(direct_remaining, burn_rate)
        out["key"] = {
            **normalized,
            "label": data.get("label"),
            "limit_reset": data.get("limit_reset"),
            "expires_at": data.get("expires_at"),
            "is_free_tier": data.get("is_free_tier"),
        }
    if management_key:
        data = _http_json("https://openrouter.ai/api/v1/credits", management_key, timeout=timeout)
        total = _number(data.get("total_credits"))
        used = _number(data.get("total_usage"))
        remaining = max(0.0, total - used) if total is not None and used is not None else None
        out["account"] = {
            "purchased": total,
            "used": used,
            "remaining": remaining,
            "percentage": (used * 100.0 / total) if used is not None and total and total > 0 else None,
            "unit": "USD",
            "burn_rate": burn_rate,
            "estimated_rounds_remaining": _burn_estimate(remaining, burn_rate),
        }
    else:
        out["account_unavailable_reason"] = (
            "OPENROUTER_MANAGEMENT_KEY is not set; the inference key exposes only its own usage and limit"
        )
    return out


def usage_snapshot(
    *,
    providers: list[str] | None = None,
    kiro_burn_rate: float | None = None,
    openrouter_burn_rate: float | None = None,
    timeout: float = 30.0,
    env=None,
) -> dict:
    providers = providers or ["kiro", "openrouter"]
    result = {}
    for provider in providers:
        try:
            if provider == "kiro":
                result[provider] = kiro_usage(burn_rate=kiro_burn_rate, timeout=timeout, env=env)
            elif provider == "openrouter":
                result[provider] = openrouter_usage(burn_rate=openrouter_burn_rate, timeout=min(timeout, 30.0), env=env)
            else:
                result[provider] = {"ok": False, "error": f"unknown provider {provider!r}"}
        except UsageError as e:
            result[provider] = {"ok": False, "error": str(e)}
    return result
