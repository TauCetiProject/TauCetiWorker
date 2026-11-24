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

For a routed review the engine still runs as the account: its reads, claims and archive work are
unchanged. It is told `TAUCETI_REVIEW_APP_PUBLISH=1`, and the worker's `gh` proxy then answers the
engine's identity query with the App's bot and sends each publication request (comment creates,
edits, deletes and thread resolution) with a fresh installation token. The token is minted with
the permissions publication needs, shared by the host in one private file next to the shared
GitHub cache, and never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import github_transport as transport
from .config import NoProgress
from .constants import TAUCETI

DIR_ENV = "TAUCETI_REVIEW_APP_DIR"
SHARE_ENV = "TAUCETI_REVIEW_APP_SHARE"
PUBLISH_ENV = "TAUCETI_REVIEW_APP_PUBLISH"
LOGIN_ENV = "TAUCETI_REVIEW_APP_LOGIN"
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
# A request is sent with a token that has at least this long left; GitHub issues them for an hour.
TOKEN_MARGIN = 10 * 60
# Identity mappings for expired tokens are removed after this long.
IDENTITY_TTL = 24 * 3600

_source = None
_source_key = None


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


def login(credentials) -> str:
    return credentials.slug + "[bot]"


def app_login() -> str | None:
    """The App's bot login when configured and readable, else None. Mints no token."""
    if directory() is None:
        return None
    try:
        return login(_credentials())
    except Exception:
        return None


def _identity(credentials) -> str:
    """Names one installation and key, so a reinstall or new key never reuses an old token."""
    key = hashlib.sha256(credentials.private_key_pem).hexdigest()[:16]
    return f"{credentials.app_id}-{credentials.installation_id}-{key}"


def _cache_path(credentials) -> Path:
    return transport.root() / "review-app" / (_identity(credentials) + ".json")


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
    global _source, _source_key
    from tauceti_queue import github_app
    from tauceti_queue.github_adapter import UrllibTransport

    if _source is None or _source_key != _identity(credentials):
        _source = github_app.InstallationTokenSource(
            credentials, UrllibTransport(timeout=45), permissions=dict(PERMISSIONS)
        )
        _source_key = _identity(credentials)
    else:
        _source.invalidate()
    token = _source.token()
    if not isinstance(token, str) or not token or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("invalid installation token")
    return {"token": token, "expires_at": _source.expires_at()}


def token(credentials) -> str:
    """A host-shared installation token with at least `TOKEN_MARGIN` left."""
    path = _cache_path(credentials)
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


def invalidate(rejected: str) -> None:
    """Forget the host's token after GitHub answered 401 to it, unless a peer already replaced it."""
    try:
        path = _cache_path(_credentials())
        with transport.locked(path.with_suffix(".lock"), timeout=60):
            if (_cached(path) or {}).get("token") == rejected:
                path.unlink(missing_ok=True)
    except Exception:
        return


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
        path = folder / (identity + ".json")
        if not path.is_file():
            transport.atomic(path, {"quota_scope": shared, "principal": login(credentials)})
        cutoff = time.time() - IDENTITY_TTL
        for other in folder.glob("*.json"):
            if other == path:
                continue
            try:
                if other.stat().st_mtime < cutoff and json.loads(other.read_text()).get("quota_scope") == shared:
                    other.unlink()
            except (OSError, ValueError, AttributeError):
                continue
    except OSError:
        return  # Pacing then falls back to this token alone; publication still works.


def _unavailable(error) -> NoProgress:
    # Credentials or GitHub; never include the message, which may quote input.
    return NoProgress(f"review posting App unavailable ({type(error).__name__}); not charged to the PR")


def environment(base: dict | None = None) -> dict:
    """`base` (default: this process's environment) acting as the App, with a fresh token."""
    try:
        credentials = _credentials()
        value = token(credentials)
    except Exception as error:
        raise _unavailable(error) from None
    env = dict(os.environ if base is None else base)
    env.pop("GITHUB_TOKEN", None)
    env.pop(PUBLISH_ENV, None)
    bot = login(credentials)
    env.update(GH_TOKEN=value, GH_HOST="github.com", TAUCETI_GITHUB_ACCOUNT=bot)
    env["_TAUCETI_PINNED_GITHUB_ACCOUNT"] = bot
    env["_TAUCETI_PINNED_GITHUB_SCOPE"] = transport.scope(env)
    env[LOGIN_ENV] = bot
    env["_TAUCETI_REVIEW_APP_TOKEN"] = "1"
    register(env, credentials)
    return env


def refreshed(env: dict) -> dict:
    """`env` with the host's current App token, when the one it carries is near expiry."""
    try:
        if token(_credentials()) == env.get("GH_TOKEN"):
            return env
        return environment(env)
    except Exception:
        return env  # GitHub then rejects the old token, and the write is retried later.


def engine_overrides() -> dict:
    """Variables that make a review engine publish through the App; checks the App first.

    Minting here means an unusable App defers the review before any paid inference.
    """
    try:
        credentials = _credentials()
        token(credentials)
    except Exception as error:
        raise _unavailable(error) from None
    return {PUBLISH_ENV: "1", LOGIN_ENV: login(credentials)}


def publishing(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get(PUBLISH_ENV) == "1" and bool(env.get(DIR_ENV, "").strip())


def publishing_login(env=None) -> str | None:
    value = (os.environ if env is None else env).get(LOGIN_ENV, "")
    return value if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,33}\[bot\]", value) else None


_COMMENT = re.compile(r"/?repos/[^/]+/[^/]+/(?:issues|pulls)/comments/\d+")
_RESOLVE = re.compile(r"\bresolveReviewThread\s*\(")


def publication_request(args) -> bool:
    """A `gh` request that publishes or maintains review comments, which the App must send."""
    if transport.content_write(args):
        return True
    endpoint = (transport.api_endpoint(args) or "").split("?")[0]
    method = next((args[i + 1].upper() for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), "")
    if method == "DELETE" and _COMMENT.fullmatch(endpoint):
        return True
    if endpoint == "graphql":
        return any(a.startswith("query=") and _RESOLVE.search(a) for a in args)
    return False


def request_environment(args):
    """The App's environment for a review's publication request, else None (send as the account).

    Raises GitHubUnavailable when the App cannot supply a token, so the write is retried later
    rather than sent as another identity.
    """
    if not (publishing() and publication_request(args)):
        return None
    try:
        return environment()
    except NoProgress as error:
        from .config import GitHubUnavailable

        raise GitHubUnavailable(str(error), retry_at=time.time() + 60) from None


def blocked_since(start: float) -> bool:
    """Whether GitHub blocked one of this worker's App requests since `start`."""
    try:
        credentials = _credentials()
    except Exception:
        return False
    worker = hashlib.sha256(os.environ.get("TAUCETI_WORKER_ID", "unknown").encode()).hexdigest()
    try:
        record = json.loads((transport.root() / "blocked" / shared_scope(credentials) / (worker + ".json")).read_text())
        return record["time"] >= start
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
