"""GitHub App authentication for the AI merge queue service.

The service acts as the `tauceti-queue` GitHub App (docs/queue/operations.md). Its credentials live
in one directory, `~/.config/tauceti-queue/` unless `TAUCETI_QUEUE_CONFIG` or an explicit path says
otherwise:

    github-app.json           {"id": <App ID>, "slug": "...", "installation_id": <id>, ...}
    github-private-key.pem    the App's RSA private key
    github-webhook-secret     the webhook secret entered in the App's settings

Two credentials are derived from these: an App JWT (RS256, signed with the private key, valid for
at most ten minutes) that identifies the App itself, and installation tokens (`POST
/app/installations/<id>/access_tokens`, valid for one hour) that act on the repositories the App
is installed on. `InstallationTokenSource` caches the installation token and refreshes it ahead of
expiry, so a long-running request cannot straddle the expiry.

The JWT is assembled here with `cryptography` alone rather than through a JWT library: the token
is three base64url segments and one PKCS#1 v1.5 SHA-256 signature, which is less code than another
dependency. Nothing here writes a token to disk or to a log; the private key stays in memory.

The uv-managed interpreters on the fleet hosts carry no system CA bundle, so `ssl_context()` loads
`/etc/ssl/certs/ca-certificates.crt` when it exists, exactly as `tauceti_worker/github.py` does.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pathlib
import re
import ssl
import threading
import time
from collections.abc import Callable

DEFAULT_CONFIG = pathlib.Path.home() / ".config" / "tauceti-queue"
SYSTEM_CA_BUNDLE = pathlib.Path("/etc/ssl/certs/ca-certificates.crt")
API = "https://api.github.com"

# GitHub accepts a JWT whose `exp` is at most ten minutes after `iat`; `iat` is backdated a minute to
# tolerate clock skew between this host and GitHub, and the usable lifetime stays under ten minutes.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60
# An installation token is refreshed this long before GitHub expires it.
TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60


class CredentialsError(RuntimeError):
    """The configuration directory is incomplete or malformed."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def config_dir(explicit: pathlib.Path | str | None = None) -> pathlib.Path:
    """The credential directory: the explicit path, else `$TAUCETI_QUEUE_CONFIG`, else the default."""
    if explicit is not None:
        return pathlib.Path(explicit)
    env = os.environ.get("TAUCETI_QUEUE_CONFIG")
    return pathlib.Path(env) if env else DEFAULT_CONFIG


@dataclasses.dataclass(frozen=True)
class AppCredentials:
    app_id: int
    slug: str
    installation_id: int
    private_key_pem: bytes = dataclasses.field(repr=False)
    webhook_secret: bytes = dataclasses.field(repr=False)

    @classmethod
    def load(cls, directory: pathlib.Path | str | None = None) -> AppCredentials:
        root = config_dir(directory)
        meta_path = root / "github-app.json"
        key_path = root / "github-private-key.pem"
        secret_path = root / "github-webhook-secret"
        for path in (meta_path, key_path, secret_path):
            if not path.is_file():
                raise CredentialsError(f"missing credential file {path}")
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError as error:
            raise CredentialsError(f"{meta_path} is not JSON: {error}") from None
        if not isinstance(meta, dict):
            raise CredentialsError(f"{meta_path} must hold a JSON object")
        try:
            app_id = int(meta["id"])
            installation_id = int(meta["installation_id"])
            slug = str(meta["slug"])
        except (KeyError, TypeError, ValueError) as error:
            raise CredentialsError(
                f"{meta_path} needs integer `id`, integer `installation_id` and `slug`: {error}"
            ) from None
        pem = key_path.read_bytes()
        if b"PRIVATE KEY-----" not in pem:
            raise CredentialsError(f"{key_path} is not a PEM private key")
        secret = secret_path.read_bytes().strip()
        if not secret:
            raise CredentialsError(f"{secret_path} is empty")
        return cls(
            app_id=app_id, slug=slug, installation_id=installation_id, private_key_pem=pem, webhook_secret=secret
        )


def load_execution_credentials(directory):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from tauceti_tools.ci_candidate import read_json

    from .control_policy_file import read_control_policy

    directory = pathlib.Path(directory)
    metadata = read_json(read_control_policy(directory / "github-app.json"), max_bytes=128 * 1024)
    if (
        not isinstance(metadata, dict)
        or any(type(metadata.get(key)) is not int or metadata[key] <= 0 for key in ("id", "installation_id"))
        or not isinstance(metadata.get("slug"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", metadata["slug"]) is None
    ):
        raise ValueError("invalid execution App metadata")
    key = read_control_policy(directory / "github-private-key.pem")
    if not isinstance(serialization.load_pem_private_key(key, password=None), rsa.RSAPrivateKey):
        raise ValueError("invalid execution App key")
    # The execution App only dispatches CI. Native ingress uses its own secret.
    return AppCredentials(metadata["id"], metadata["slug"], metadata["installation_id"], key, b"")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: int, private_key_pem: bytes, now: float | None = None) -> str:
    """An RS256 JWT identifying the App, per GitHub's App authentication rules."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued - JWT_BACKDATE_SECONDS, "exp": issued + JWT_LIFETIME_SECONDS, "iss": str(app_id)}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode()) for part in (header, payload)
    )
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialsError("the App private key must be an RSA key")
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def decode_jwt_claims(token: str) -> dict:
    """The unverified claims of a JWT (for tests and diagnostics; GitHub verifies the signature)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that also trusts the system bundle the uv interpreter lacks."""
    context = ssl.create_default_context()
    if SYSTEM_CA_BUNDLE.is_file():
        context.load_verify_locations(cafile=str(SYSTEM_CA_BUNDLE))
    return context


def parse_github_time(text: str) -> float:
    """Seconds since the epoch for a GitHub `YYYY-MM-DDTHH:MM:SSZ` timestamp."""
    import datetime

    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@dataclasses.dataclass
class InstallationToken:
    value: str = dataclasses.field(repr=False)
    expires_at: float
    permissions: dict = dataclasses.field(default_factory=dict)


# A transport takes (method, url, headers, body) and returns (status, headers, body). The HTTP
# client in github_adapter provides the real one; tests substitute a recorder.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class InstallationTokenSource:
    """Installation tokens for one installation, refreshed before expiry and shared across threads."""

    def __init__(
        self,
        credentials: AppCredentials,
        transport: Transport,
        *,
        api: str = API,
        clock: Callable[[], float] = time.time,
        repository_id: int | None = None,
        permissions: dict[str, str] | None = None,
    ) -> None:
        if repository_id is not None and (type(repository_id) is not int or repository_id < 1):
            raise CredentialsError("installation token repository ID must be positive")
        if permissions is not None and (
            not isinstance(permissions, dict)
            or not permissions
            or any(
                not isinstance(key, str) or not isinstance(value, str) or value not in {"read", "write"}
                for key, value in permissions.items()
            )
        ):
            raise CredentialsError("invalid installation token permissions")
        self.credentials = credentials
        self._transport = transport
        self._api = api
        self._clock = clock
        self._repository_id = repository_id
        self._permissions = None if permissions is None else dict(permissions)
        self._lock = threading.Lock()
        self._current: InstallationToken | None = None
        self.refreshes = 0

    @property
    def repository_id(self) -> int | None:
        """The immutable repository resource selected when each token is minted."""
        return self._repository_id

    @property
    def permissions(self) -> dict[str, str] | None:
        return None if self._permissions is None else dict(self._permissions)

    def jwt(self) -> str:
        return app_jwt(self.credentials.app_id, self.credentials.private_key_pem, self._clock())

    def token(self) -> str:
        """A token valid for at least `TOKEN_REFRESH_MARGIN_SECONDS` more."""
        with self._lock:
            current = self._current
            if current is None or current.expires_at - self._clock() <= TOKEN_REFRESH_MARGIN_SECONDS:
                current = self._fetch()
                self._current = current
            return current.value

    def invalidate(self) -> None:
        """Drop the cached token, for example after GitHub answers 401 with it."""
        with self._lock:
            self._current = None

    def expires_at(self) -> float | None:
        with self._lock:
            return self._current.expires_at if self._current else None

    def _fetch(self) -> InstallationToken:
        url = f"{self._api}/app/installations/{self.credentials.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"tauceti-queue/{self.credentials.slug}",
        }
        selection = {}
        if self._repository_id is not None:
            selection["repository_ids"] = [self._repository_id]
        if self._permissions is not None:
            selection["permissions"] = self._permissions
        status, response_headers, body = self._transport(
            "POST", url, headers, json.dumps(selection, separators=(",", ":")).encode()
        )
        if status != 201:
            limited = status == 403 and (
                response_headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response_headers
            )
            raise CredentialsError(
                f"installation token request failed with HTTP {status}",
                retryable=limited or status == 429 or 500 <= status <= 599,
            )
        data = json.loads(body)
        if self._repository_id is not None:
            token = data.get("token")
            if not isinstance(token, str) or not token or not all(33 <= ord(char) <= 126 for char in token):
                raise CredentialsError("scoped installation token response is invalid")
            if "repositories" in data and (
                not isinstance(data["repositories"], list)
                or len(data["repositories"]) != 1
                or not isinstance(data["repositories"][0], dict)
                or type(data["repositories"][0].get("id")) is not int
                or data["repositories"][0]["id"] != self._repository_id
            ):
                raise CredentialsError("installation token response selected another repository")
        self.refreshes += 1
        return InstallationToken(
            value=str(data["token"]),
            expires_at=parse_github_time(str(data["expires_at"])),
            permissions=dict(data.get("permissions") or {}),
        )


class GitHubApp:
    """The App as one object: credentials, transport and installation tokens.

    `GitHubApp.load(credentials_dir)` is what the service entrypoint (`tauceti_queue/__main__.py`)
    calls; `github_adapter.GitHubAdapter(app, repository)` builds the client from it.
    """

    def __init__(self, credentials: AppCredentials, transport: Transport | None = None, *, api: str = API) -> None:
        if transport is None:
            from .github_adapter import UrllibTransport

            transport = UrllibTransport()
        self.credentials = credentials
        self.transport = transport
        self.api = api
        self.tokens = InstallationTokenSource(credentials, transport, api=api)

    @classmethod
    def load(cls, credentials_dir: pathlib.Path | str | None = None, transport: Transport | None = None) -> GitHubApp:
        return cls(AppCredentials.load(credentials_dir), transport)

    @property
    def webhook_secret(self) -> bytes:
        return self.credentials.webhook_secret

    @property
    def slug(self) -> str:
        return self.credentials.slug
