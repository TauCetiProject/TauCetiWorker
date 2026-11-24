"""Post a share of GitHub reviews as a dedicated GitHub App, beside the worker's account.

GitHub accepts about 500 new or edited comments an hour from one identity. Every worker posts as the
same account, so review publication queues behind that limit. A GitHub App's bot is a separate
identity with its own limit, so routing part of the reviews through an App installation token adds
another such budget.

The setting is inert until an operator configures it:

    TAUCETI_REVIEW_APP_DIR     a private directory holding the App's `github-app.json`
                               ({"id", "slug", "installation_id"}) and `github-private-key.pem`
    TAUCETI_REVIEW_APP_SHARE   the fraction of PRs the App posts for (default 0.5)

A PR's identity is a fixed function of its number, so every worker posts its review threads and
scoreboard as the same identity. The review engine edits only comments its own identity wrote and
answers an identity change by posting its own current-head scoreboard and threads, which the
readers order by update time. A fixed assignment avoids that churn, and lets a retained publication
retry under the identity that rendered it.

Only the review engine invocation runs as the App. The worker's own reads and writes, fixes, labels,
reactions and archive pushes stay on the account. The installation token is minted with the
permissions publication needs, kept in memory or in one private host file next to the shared GitHub
cache, and never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import github_transport as transport
from .config import NoProgress
from .constants import TAUCETI

DIR_ENV = "TAUCETI_REVIEW_APP_DIR"
SHARE_ENV = "TAUCETI_REVIEW_APP_SHARE"
# What publication needs: comment on pull requests (GitHub accepts a PR's issue comments under
# either issues or pull_requests write), and read the source and its CI results.
PERMISSIONS = {
    "pull_requests": "write",
    "issues": "write",
    "contents": "read",
    "checks": "read",
    "statuses": "read",
    "metadata": "read",
}
# Reuse a cached token while it has this long left; GitHub issues them for an hour.
TOKEN_MARGIN = 15 * 60
# Identity mappings for expired tokens are removed after this long.
IDENTITY_TTL = 24 * 3600

_source = None


def directory() -> Path | None:
    value = os.environ.get(DIR_ENV, "").strip()
    return Path(value).expanduser() if value else None


def share() -> float:
    try:
        value = float(os.environ.get(SHARE_ENV, "0.5"))
    except ValueError:
        return 0.0
    return min(1.0, max(0.0, value)) if value == value else 0.0


def routes(repo: str, pr: int) -> bool:
    """Whether the App posts this PR's review. Only for the TauCeti repository on GitHub."""
    if directory() is None or str(repo).lower() != TAUCETI.lower():
        return False
    portion = share()
    if portion <= 0:
        return False
    digest = hashlib.sha256(f"{str(repo).lower()}#{int(pr)}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < portion


def _credentials():
    from tauceti_queue import github_app

    return github_app.load_execution_credentials(directory())


def app_login() -> str | None:
    """The App's bot login when configured and readable, else None. Mints no token."""
    if directory() is None:
        return None
    try:
        return login(_credentials())
    except Exception:
        return None


def _cache_path(app_id: int) -> Path:
    return transport.root() / "review-app" / f"{app_id}.json"


def _cached(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        isinstance(value, dict)
        and isinstance(value.get("token"), str)
        and value["token"]
        and isinstance(value.get("expires_at"), (int, float))
        and value["expires_at"] - time.time() > TOKEN_MARGIN
    ):
        return value
    return None


def _mint(credentials) -> dict:
    global _source
    from tauceti_queue import github_app
    from tauceti_queue.github_adapter import UrllibTransport

    if _source is None or _source.credentials.app_id != credentials.app_id:
        _source = github_app.InstallationTokenSource(
            credentials, UrllibTransport(timeout=45), permissions=dict(PERMISSIONS)
        )
    else:
        _source.invalidate()
    token = _source.token()
    if not isinstance(token, str) or not token or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("invalid installation token")
    return {"token": token, "expires_at": _source.expires_at()}


def token(credentials) -> str:
    """A host-shared installation token with at least `TOKEN_MARGIN` left."""
    path = _cache_path(credentials.app_id)
    cached = _cached(path)
    if cached:
        return cached["token"]
    with transport.locked(path.with_suffix(".lock"), timeout=60):
        cached = _cached(path)
        if cached:
            return cached["token"]
        value = _mint(credentials)
        transport.atomic(path, value)
        return value["token"]


def login(credentials) -> str:
    return credentials.slug + "[bot]"


def shared_scope(credentials) -> str:
    """The quota and outbox scope every token of this App shares."""
    return hashlib.sha256(f"github.com:app:{credentials.app_id}".encode()).hexdigest()


def register(env: dict, credentials) -> None:
    """Pace every token of this App as one GitHub identity, and drop mappings of expired tokens."""
    identity = transport.scope(env)
    if not identity:
        return
    shared = shared_scope(credentials)
    folder = transport.root() / "identities"
    try:
        transport.atomic(folder / (identity + ".json"), {"quota_scope": shared, "principal": login(credentials)})
        cutoff = time.time() - IDENTITY_TTL
        for path in folder.glob("*.json"):
            if path.name == identity + ".json":
                continue
            try:
                if path.stat().st_mtime < cutoff and json.loads(path.read_text()).get("quota_scope") == shared:
                    path.unlink()
            except (OSError, ValueError, AttributeError):
                continue
    except OSError:
        return  # Pacing then falls back to this token alone; publication still works.


IDENTITY_KEYS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_HOST",
    "TAUCETI_GITHUB_ACCOUNT",
    "_TAUCETI_PINNED_GITHUB_ACCOUNT",
    "_TAUCETI_PINNED_GITHUB_SCOPE",
    "TAUCETI_REVIEW_APP_LOGIN",
)


def overrides() -> dict:
    """The GitHub identity variables that make the review engine post as the App.

    The `gh` proxy answers the engine's identity query from the pinned login, and paces and
    retains the App's writes under the App's own identity. None removes a variable.
    """
    try:
        credentials = _credentials()
        value = token(credentials)
    except Exception as error:  # Credentials or GitHub; never include the message, which may quote input.
        raise NoProgress(f"review posting App unavailable ({type(error).__name__}); not charged to the PR") from None
    bot = login(credentials)
    result = dict.fromkeys(IDENTITY_KEYS)
    result.update(GH_TOKEN=value, GH_HOST="github.com", TAUCETI_GITHUB_ACCOUNT=bot)
    result["_TAUCETI_PINNED_GITHUB_ACCOUNT"] = bot
    result["_TAUCETI_PINNED_GITHUB_SCOPE"] = transport.scope(result)
    # The review engine's own answer when it cannot ask GitHub who an installation token is.
    result["TAUCETI_REVIEW_APP_LOGIN"] = bot
    register(result, credentials)
    return result


def environment(base: dict | None = None) -> dict:
    """`base` (default: this process's environment) acting as the App."""
    env = dict(os.environ if base is None else base)
    for name, value in overrides().items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def drain() -> list:
    """Retry or reconcile the App's retained publications, as the round start does for the account."""
    if directory() is None:
        return []
    try:
        credentials = _credentials()
    except Exception:
        return []
    worker = os.environ.get("TAUCETI_WORKER_ID", "unknown")
    if not any((transport.root() / "outbox" / shared_scope(credentials) / worker).glob("*.json")):
        return []  # Nothing retained: no token needed.
    from .github_outbox import drain as drain_outbox

    try:
        env = environment()
    except NoProgress:
        return []
    return drain_outbox(env)
