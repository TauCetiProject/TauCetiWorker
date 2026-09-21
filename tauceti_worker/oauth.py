"""Rotating a provider's OAuth credential, shared by the pacer and the standalone refresher.

Claude and Codex both issue SINGLE-USE refresh tokens: exchanging one invalidates it and returns a
replacement. Two processes holding the same token can therefore race, and the loser is left holding a
credential the server has already retired. Everything here exists to make that safe:

  * an exclusive flock on the credential file serializes every rotation on one host, whoever starts it
    (`tauceti work --loop`, several isolated workers sharing one source file, or the Docker refresher);
  * a rotated refresh token is persisted even when the rest of the response is unusable, so a retry
    never replays a token the server has already consumed;
  * a success marker enforces a minimum interval between rotations, and an attempt marker bounds how
    often a FAILING credential may be retried — a TUI that polls every few seconds must not turn into a
    request flood against the token endpoint.

This module is stdlib-only (the `tauceti` package depends on rich/textual and nothing else), so it is
importable from the pacer without adding a dependency.
"""

from __future__ import annotations

import base64
import fcntl
import http.client
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "tauceti-worker (+https://github.com/kim-em/TauCetiWorker)"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPE = "user:profile user:inference user:sessions:claude_code user:mcp_servers"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_PLACEHOLDER = "rt.0.tauceti-worker-placeholder-never-a-real-refresh-token"


@dataclass(frozen=True)
class Provider:
    name: str
    credentials: Path
    token_url: str
    client_id: str
    mirror: Path | None = None


def provider(name: str, *, credentials: Path | None = None, mirror: Path | None = None) -> Provider:
    """The refreshable credential for `name`. With no explicit path this resolves the operator's own
    store from the environment (the standalone refresher's case); the pacer passes the credential file
    it has already resolved through its own isolation rules."""
    home = Path(os.environ.get("HOME", "/root"))
    if name == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        path = credentials or config / ".credentials.json"
        url = os.environ.get("TAUCETI_CLAUDE_TOKEN_URL", CLAUDE_TOKEN_URL)
        client = CLAUDE_CLIENT_ID
    else:
        path = credentials or home / ".codex" / "auth.json"
        url = os.environ.get("TAUCETI_CODEX_TOKEN_URL", CODEX_TOKEN_URL)
        client = CODEX_CLIENT_ID
    if mirror is None:
        env_mirror = os.environ.get("TAUCETI_REFRESH_MIRROR")
        mirror = Path(env_mirror) if env_mirror else None
    return Provider(name, path, url, client, mirror)


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _block(data: Any, key: str) -> dict[str, Any]:
    """A named credential block, or {} for anything that is not a JSON object. Nothing here writes these
    files: a partially-written one, a hand edit, or a schema change all arrive as valid JSON of the wrong
    SHAPE, and `.get()` on a list raises AttributeError — which the daemon does not catch, so a mangled
    credential would kill the process whose entire job is to keep retrying. Wrong shape must read as
    "nothing usable here"."""
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _seconds(value: Any) -> float | None:
    """A finite, non-boolean number of seconds, else None. JSON admits Infinity and NaN, and both would
    propagate into an expiry that then either never lapses or fails every comparison."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _expires_at(prov: Provider, data: Any) -> float | None:
    if prov.name == "claude":
        value = _seconds(_block(data, "claudeAiOauth").get("expiresAt"))
        if value is None:
            return None
        return value / 1000 if value >= 100_000_000_000 else value
    token = _block(data, "tokens").get("access_token")
    return _jwt_exp(token) if isinstance(token, str) else None


def _stored_refresh_token(prov: Provider, data: Any) -> str | None:
    block = "claudeAiOauth" if prov.name == "claude" else "tokens"
    key = "refreshToken" if prov.name == "claude" else "refresh_token"
    value = _block(data, block).get(key)
    if not isinstance(value, str) or not value or value == CODEX_REFRESH_PLACEHOLDER:
        return None
    return value


def renewable(prov: Provider) -> bool:
    """Whether this credential file carries a refresh token we could actually spend. False for a
    worker MIRROR, whose whole point is that the real token was stripped out of it — asking the token
    endpoint about a placeholder would be a guaranteed failure reported every poll."""
    try:
        data = json.loads(prov.credentials.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return _stored_refresh_token(prov, data) is not None


def expires_at(prov: Provider) -> float | None:
    """Unix expiry of the stored ACCESS token, or None when the file is missing or says nothing."""
    try:
        data = json.loads(prov.credentials.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _expires_at(prov, data)


def _refresh_request(prov: Provider, data: dict[str, Any]) -> tuple[dict[str, str], str]:
    refresh_token = _stored_refresh_token(prov, data)
    if not refresh_token:
        raise ValueError(f"no renewable {prov.name} credential; log in again")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": prov.client_id,
    }
    if prov.name == "claude":
        payload["scope"] = CLAUDE_SCOPE
    return payload, refresh_token


def _merge_response(
    prov: Provider,
    credentials: dict[str, Any],
    response: dict[str, Any],
    old_refresh_token: str,
) -> dict[str, Any]:
    """Merge every usable rotated token before validating the rest of the response.

    Refresh tokens are single-use. If the server returned a new one, preserving it is more
    important than rejecting an incomplete response and leaving the already-consumed token on disk.
    """
    refresh_token = response.get("refresh_token") or old_refresh_token
    access_token = response.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("refresh response contained no refresh token")
    prior_expiry = _expires_at(prov, credentials)
    if prov.name == "claude":
        expires_in = _seconds(response.get("expires_in"))
        candidate_expiry = time.time() + expires_in if expires_in is not None else None
        block = dict(_block(credentials, "claudeAiOauth"))
        block["refreshToken"] = refresh_token
        regressed = prior_expiry is not None and candidate_expiry is not None and candidate_expiry <= prior_expiry
        if isinstance(access_token, str) and access_token and not regressed:
            block["accessToken"] = access_token
        if (
            isinstance(access_token, str)
            and access_token
            and candidate_expiry is not None
            and candidate_expiry > 0
            and not regressed
        ):
            block["expiresAt"] = int(candidate_expiry * 1000)
        credentials["claudeAiOauth"] = block
    else:
        candidate_expiry = _jwt_exp(access_token) if isinstance(access_token, str) else None
        regressed = prior_expiry is not None and candidate_expiry is not None and candidate_expiry <= prior_expiry
        tokens = dict(_block(credentials, "tokens"))
        tokens["refresh_token"] = refresh_token
        if isinstance(access_token, str) and access_token and not regressed:
            tokens["access_token"] = access_token
        id_token = response.get("id_token")
        if isinstance(id_token, str) and id_token and not regressed:
            tokens["id_token"] = id_token
        credentials["tokens"] = tokens
        credentials["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if not isinstance(access_token, str) or not access_token:
        # The caller still persists a newly rotated refresh token before surfacing this error.
        raise ValueError("refresh response contained no access token")
    if regressed:
        raise ValueError("refresh response would regress the access-token expiry")
    return credentials


def _write_atomic(destination: Path, data: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".refresh.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_worker_mirror(prov: Provider, credentials: dict[str, Any]) -> None:
    """Publish an access-only copy; the agent container never mounts the real refresh token."""
    if prov.mirror is None:
        return
    output = dict(credentials)
    if prov.name == "claude":
        block = dict(_block(output, "claudeAiOauth"))
        block.pop("refreshToken", None)
        if not block.get("accessToken"):
            return
        output["claudeAiOauth"] = block
    else:
        tokens = dict(_block(output, "tokens"))
        if not tokens.get("access_token"):
            return
        tokens["refresh_token"] = CODEX_REFRESH_PLACEHOLDER
        output["tokens"] = tokens
    _write_atomic(prov.mirror, output)


def _marker_time(marker: Path) -> float | None:
    try:
        value = json.loads(marker.read_text()).get("at")
        return float(value) if isinstance(value, (int, float)) else None
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None


def _effective_skew(skew_seconds: int, expiry: float, last_success: float | None) -> float:
    """Never spend more than half a freshly issued token's lifetime refreshing early."""
    if last_success is None or expiry <= last_success:
        return float(skew_seconds)
    return min(float(skew_seconds), max(1.0, (expiry - last_success) / 2))


def _post_json(url: str, payload: dict[str, Any], timeout: int = 15) -> tuple[int, Any]:
    """POST a JSON body; return (status, parsed body or None when it did not parse). Never returns the
    raw body text: an authentication service is allowed to echo request details, and a refresh token
    must not reach a log through an exception message."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        # Cloudflare fronts both token endpoints and answers 403 (error 1010) to Python's default
        # `Python-urllib/x.y` agent; identify the worker so a refresh is not mistaken for a bot.
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    # http.client raises its own exception tree for a truncated or malformed response (IncompleteRead,
    # BadStatusLine), and it is not under OSError. `requests` folded those into RequestException, which
    # the refresher daemon caught; left uncaught here they would kill a daemon that is supposed to back
    # off, and crash a status read. Only the exception TYPE is reported — a body is never quoted.
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        try:
            error.read()
        except (OSError, http.client.HTTPException):
            pass
        return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, UnicodeDecodeError) as error:
        raise OSError(f"token endpoint unreachable: {type(error).__name__}") from error
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, None


def refresh_if_due(
    prov: Provider,
    skew_seconds: int,
    *,
    force: bool = False,
    minimum_interval_seconds: int = 600,
    attempt_interval_seconds: int = 0,
) -> str:
    """Rotate the credential if it is at or near expiry. Returns one of "refreshed", "current",
    "cooldown" (a rotation was due but is rate-limited) or "waiting" (no credential file yet).

    `attempt_interval_seconds` bounds how often the token endpoint may be CONTACTED, successfully or
    not. `minimum_interval_seconds` bounds how often a rotation may SUCCEED. A caller that polls on its
    own schedule (the standalone refresher) leaves the attempt bound at 0; a caller invoked at the whim
    of a UI refresh must set it, or a permanently broken credential becomes a request flood."""
    prov.credentials.parent.mkdir(parents=True, exist_ok=True)
    credential_name = prov.credentials.name.lstrip(".")
    lock_path = prov.credentials.with_name(f".{credential_name}.refresh.lock")
    success_path = prov.credentials.with_name(f".{credential_name}.refresh.last-success")
    attempt_path = prov.credentials.with_name(f".{credential_name}.refresh.last-attempt")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            credentials = json.loads(prov.credentials.read_text())
        except FileNotFoundError:
            return "waiting"
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read {prov.credentials}: {error}") from error
        if not isinstance(credentials, dict):
            # Valid JSON of the wrong shape. Rewriting it would discard whatever the operator has, and
            # every field we need is absent, so say so and let the caller's back-off take over.
            raise ValueError(f"{prov.credentials} is not a credential object; log in again")

        # Publish immediately after login/startup as well as after a rotation. This lets the
        # read-only worker volume become usable without waiting for the token to approach expiry.
        _write_worker_mirror(prov, credentials)

        now = time.time()
        expiry = _expires_at(prov, credentials)
        last_success = _marker_time(success_path)
        effective_skew = _effective_skew(skew_seconds, expiry, last_success) if expiry is not None else skew_seconds
        if not force and expiry is not None and expiry > now + effective_skew:
            return "current"
        if not force and expiry is None:
            raise ValueError(f"cannot determine {prov.name} access-token expiry; log in again")
        # The success cooldown binds even a FORCED rotation. `force` means "do not believe the stored
        # expiry", which is not a reason to spend a refresh token that was issued moments ago: a caller
        # reacting to a 401 that arrives right after another process rotated would otherwise retire the
        # brand-new credential, and this marker is the only bound shared with that process.
        if last_success is not None and now < last_success + minimum_interval_seconds:
            return "cooldown"
        if attempt_interval_seconds:
            last_attempt = _marker_time(attempt_path)
            if last_attempt is not None and now < last_attempt + attempt_interval_seconds:
                return "cooldown"
            # Stamped BEFORE the exchange, so a failure (or a crash mid-request) still throttles the
            # next attempt. A success additionally stamps the success marker below.
            _write_atomic(attempt_path, {"at": now})

        payload, old_refresh_token = _refresh_request(prov, credentials)
        status, body = _post_json(prov.token_url, payload)
        if status != 200:
            raise RuntimeError(f"OAuth endpoint returned HTTP {status}")
        if body is None:
            raise RuntimeError("OAuth endpoint returned invalid JSON")
        if not isinstance(body, dict):
            raise RuntimeError("OAuth endpoint returned a non-object response")

        try:
            updated = _merge_response(prov, credentials, body, old_refresh_token)
        except ValueError:
            # If the exchange rotated the refresh token but omitted another required field, preserve
            # that token so the next attempt does not replay the consumed credential.
            if body.get("refresh_token"):
                _write_atomic(prov.credentials, credentials)
                _write_worker_mirror(prov, credentials)
            raise
        _write_atomic(prov.credentials, updated)
        _write_worker_mirror(prov, updated)
        _write_atomic(success_path, {"at": time.time()})
        return "refreshed"
