"""Configuration for the queue service process.

Values come from three layers, later ones winning: the defaults below, a TOML file
(`~/.config/tauceti-queue/config.toml` unless `TAUCETI_QUEUE_CONFIG` or `--config` names another),
and `TAUCETI_QUEUE_*` environment variables. `load()` returns a `Config` that remembers where each
value came from, so `tauceti-queue check-config` can print the effective configuration with its
sources, and `validate()` lists the problems that would stop `serve`.

The file is flat apart from one `[settings]` table holding the runtime controls of the plan
(`docs/redesign/specs/ai-merge-queue.md`, "Runtime controls") and the approval poll interval. Those values are only the
*initial* settings: once the store exists, the controller's persisted settings win and are changed
through the admin endpoint (`tauceti-queue settings set`), never by editing this file.

    repository = "TauCetiProject/TauCeti"
    canonical_branch = ""          # empty: discovered from the repository at start
    host = "chungus2"              # where this service runs; reported in status
    publication_enabled = false    # the cutover flag; false is shadow mode
    content_clone = "~/.local/share/tauceti-queue/content"
    database = "~/.local/state/tauceti-queue/queue.sqlite3"
    credentials_dir = "~/.config/tauceti-queue"
    log_dir = "~/.local/state/tauceti-queue/logs"
    listen_host = "127.0.0.1"
    listen_port = 8780
    public_url = "https://queue.taucetiproject.org"
    reconcile_interval_seconds = 30
    review_app_id = 3947238        # the App whose `merge eligibility` checks approve (TauCetiReview)

    [settings]
    insertion_age_window = 16
    repair_concurrency = 1
    stall_interval_seconds = 900
    approval_poll_interval_seconds = 900

Environment overrides use the upper-cased key with the `TAUCETI_QUEUE_` prefix
(`TAUCETI_QUEUE_LISTEN_PORT=8781`, `TAUCETI_QUEUE_PUBLICATION_ENABLED=true`,
`TAUCETI_QUEUE_INSERTION_AGE_WINDOW=8`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX = "TAUCETI_QUEUE_"
CONFIG_ENV = ENV_PREFIX + "CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/tauceti-queue/config.toml")
STATUS_SCHEMA = "tauceti-queue.status/v1"

# Credential files the service needs at start, relative to `credentials_dir`
# (docs/queue/operations.md, "GitHub App").
CREDENTIAL_FILES = ("github-app.json", "github-private-key.pem", "github-webhook-secret")

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[^\s~^:?*\[\\]+$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """The configuration cannot be read: a bad file, an unknown key, or a value of the wrong type."""


@dataclass
class Settings:
    """The runtime controls of the plan, with their initial values."""

    insertion_age_window: int = 16
    repair_concurrency: int = 1
    stall_interval_seconds: int = 900
    approval_poll_interval_seconds: int = 900

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclass
class Config:
    repository: str = "TauCetiProject/TauCeti"
    canonical_branch: str = ""  # empty until discovered from the repository
    host: str = ""  # empty: this machine's host name
    publication_enabled: bool = False
    content_clone: Path = Path("~/.local/share/tauceti-queue/content")
    database: Path = Path("~/.local/state/tauceti-queue/queue.sqlite3")
    credentials_dir: Path = Path("~/.config/tauceti-queue")
    log_dir: Path = Path("~/.local/state/tauceti-queue/logs")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8780
    public_url: str = "https://queue.taucetiproject.org"
    reconcile_interval_seconds: int = 30
    review_app_id: int = 3947238  # the App whose `merge eligibility` checks and exact-head comments approve
    admission_source: str = "policy"  # eligibility is only for an explicit pre-cutover deployment
    admission_rubrics: str = (
        "correctness,reuse,scope,attribution,api-design,generality,placement,naming,documentation,proof-quality"
    )
    admission_lean_root: str = "TauCeti"
    admission_extra_files: str = ""
    settings: Settings = field(default_factory=Settings)
    # Bookkeeping, not configuration: where the file came from and the source of every value.
    path: Path | None = None
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def listen_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    @property
    def webhook_url(self) -> str:
        return self.public_url.rstrip("/") + "/webhook/github"

    @property
    def mode(self) -> str:
        return "production" if self.publication_enabled else "shadow"

    def credential_paths(self) -> dict[str, Path]:
        return {name: self.credentials_dir / name for name in CREDENTIAL_FILES}

    def as_dict(self) -> dict[str, Any]:
        """Effective values, JSON-ready (paths as strings), with the source of each top-level key."""
        values: dict[str, Any] = {}
        for name in _FIELDS:
            value = getattr(self, name)
            values[name] = str(value) if isinstance(value, Path) else value
        values["settings"] = self.settings.as_dict()
        values["listen_url"] = self.listen_url
        values["webhook_url"] = self.webhook_url
        values["mode"] = self.mode
        values["config_file"] = str(self.path) if self.path else None
        values["sources"] = dict(self.sources)
        return values


_FIELDS = tuple(f.name for f in dataclasses.fields(Config) if f.name not in ("settings", "path", "sources"))
SETTING_FIELDS = tuple(f.name for f in dataclasses.fields(Settings))
_PATH_FIELDS = tuple(f.name for f in dataclasses.fields(Config) if f.type == "Path")


def _coerce(name: str, value: Any, kind: type) -> Any:
    """Convert a file or environment value to the field's type, or raise ConfigError."""
    if kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(f"{name}: expected true or false, got {value!r}")
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ConfigError(f"{name}: expected an integer, got {value!r}")
        try:
            return int(value)
        except ValueError:
            raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if kind is Path:
        if not isinstance(value, str | os.PathLike):
            raise ConfigError(f"{name}: expected a path, got {value!r}")
        return Path(os.path.expanduser(str(value)))
    if not isinstance(value, str):
        raise ConfigError(f"{name}: expected a string, got {value!r}")
    return value


def _field_type(name: str) -> type:
    if name in SETTING_FIELDS:
        return int
    if name in _PATH_FIELDS:
        return Path
    for f in dataclasses.fields(Config):
        if f.name == name:
            return {"bool": bool, "int": int, "str": str}[f.type]
    raise KeyError(name)


def _apply(config: Config, name: str, value: Any, source: str) -> None:
    if name in SETTING_FIELDS:
        setattr(config.settings, name, _coerce(name, value, int))
    else:
        setattr(config, name, _coerce(name, value, _field_type(name)))
    config.sources[name] = source


def read_file(path: Path) -> dict[str, Any]:
    """Parse the TOML file; unknown keys are errors so a typo cannot silently keep a default."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key == "settings":
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: [settings] must be a table")
            for setting, inner in value.items():
                if setting not in SETTING_FIELDS:
                    raise ConfigError(f"{path}: unknown setting {setting!r} (known: {', '.join(SETTING_FIELDS)})")
                flat[setting] = inner
        elif key in _FIELDS:
            flat[key] = value
        else:
            raise ConfigError(f"{path}: unknown key {key!r} (known: {', '.join(_FIELDS)})")
    return flat


def load(path: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Defaults, then the file (if it exists), then `TAUCETI_QUEUE_*` environment overrides."""
    environ = os.environ if environ is None else environ
    config = Config()
    for name in (*_FIELDS, *SETTING_FIELDS):
        config.sources[name] = "default"
    for name in _PATH_FIELDS:
        setattr(config, name, Path(os.path.expanduser(str(getattr(config, name)))))

    chosen = path or environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    file_path = Path(os.path.expanduser(str(chosen)))
    explicit = bool(path or environ.get(CONFIG_ENV))
    if file_path.exists():
        config.path = file_path
        for name, value in read_file(file_path).items():
            _apply(config, name, value, "file")
    elif explicit:
        raise ConfigError(f"configuration file not found: {file_path}")

    for name in (*_FIELDS, *SETTING_FIELDS):
        variable = ENV_PREFIX + name.upper()
        if variable in environ:
            _apply(config, name, environ[variable], "env:" + variable)

    if not config.host:
        config.host = socket.gethostname()
        config.sources["host"] = "hostname"
    return config


def validate(config: Config) -> list[str]:
    """Problems that would stop `serve`; an empty list means the configuration is usable."""
    problems: list[str] = []
    if not _REPOSITORY_RE.match(config.repository):
        problems.append(f"repository must be owner/name, got {config.repository!r}")
    if config.canonical_branch and not _BRANCH_RE.match(config.canonical_branch):
        problems.append(f"canonical_branch is not a branch name: {config.canonical_branch!r}")
    if not config.host.strip():
        problems.append("host is empty")
    if not (1 <= config.listen_port <= 65535):
        problems.append(f"listen_port must be between 1 and 65535, got {config.listen_port}")
    if not config.listen_host.strip():
        problems.append("listen_host is empty")
    if not re.match(r"^https?://[^/\s]+", config.public_url):
        problems.append(f"public_url must be an http(s) URL, got {config.public_url!r}")
    if config.reconcile_interval_seconds < 1:
        problems.append("reconcile_interval_seconds must be at least 1")
    if config.review_app_id < 1:
        problems.append("review_app_id must be a positive GitHub App id")
    if config.admission_source not in ("eligibility", "policy"):
        problems.append("admission_source must be eligibility or policy")
    if not config.admission_rubrics or any(
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s) for s in config.admission_rubrics.split(",")
    ):
        problems.append("admission_rubrics must be a comma-separated list of rubric names")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", config.admission_lean_root):
        problems.append("admission_lean_root must be a Lean module name")
    if any(s.startswith(("/", "-")) or ".." in Path(s).parts for s in config.admission_extra_files.split(",") if s):
        problems.append("admission_extra_files must contain relative repository paths")
    for name in SETTING_FIELDS:
        if getattr(config.settings, name) < 1:
            problems.append(f"{name} must be at least 1")
    for name in ("content_clone", "database", "log_dir", "credentials_dir"):
        if not getattr(config, name).is_absolute():
            problems.append(f"{name} must be an absolute path, got {getattr(config, name)}")
    if not config.credentials_dir.is_dir():
        problems.append(f"credentials_dir does not exist: {config.credentials_dir}")
    else:
        for file_path in config.credential_paths().values():
            if not file_path.is_file():
                problems.append(f"missing credential file {file_path}")
            elif os.name == "posix" and file_path.stat().st_mode & 0o077:
                problems.append(f"{file_path} is readable by others; expected mode 0600")
        app = config.credentials_dir / "github-app.json"
        if app.is_file():
            try:
                record = json.loads(app.read_text())
                if not isinstance(record, dict) or not isinstance(record.get("id"), int) or not record.get("slug"):
                    problems.append(f"{app} lacks the App id and slug")
            except (OSError, ValueError) as exc:
                problems.append(f"{app} is not readable JSON: {exc}")
    return problems


def discover_canonical_branch(config: Config, timeout: float = 30) -> str:
    """The repository's default branch, read from the remote rather than assumed.

    `git ls-remote --symref` needs no credentials for a public repository and no clone; the
    GitHub adapter can replace this with the App-authenticated repository read once it exists.
    """
    if config.canonical_branch:
        return config.canonical_branch
    url = f"https://github.com/{config.repository}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ConfigError(f"cannot discover the canonical branch of {config.repository}: {detail.strip()}") from None
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
            return line.split()[1][len("refs/heads/") :]
    raise ConfigError(f"{config.repository} reports no symbolic HEAD; set canonical_branch explicitly")


def runtime_settings(config: Config) -> dict[str, Any]:
    """The runtime controls the file seeds into the store: `[settings]` and `publication_enabled`."""
    return {**config.settings.as_dict(), "publication_enabled": config.publication_enabled}


def effective_runtime_settings(config: Config, stored: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Each runtime control's effective value and where it comes from: the store's value (with the
    actor that wrote it) once the store holds one, otherwise the file's (or default) value."""
    effective: dict[str, dict[str, Any]] = {}
    for name, value in runtime_settings(config).items():
        record = (stored or {}).get(name)
        if record is None:
            effective[name] = {"value": value, "source": config.sources.get(name, "default")}
        else:
            effective[name] = {
                "value": record["value"],
                "source": "store",
                "actor": record.get("actor") or "",
                "file_value": value,
            }
    return effective


def format_effective(config: Config, problems: list[str], stored: dict[str, dict[str, Any]] | None = None) -> str:
    """The one-screen rendering of `check-config`; `stored` is the store's settings rows, if any."""
    lines = [f"configuration file: {config.path or '(none; defaults and environment only)'}"]
    width = max(len(name) for name in (*_FIELDS, *SETTING_FIELDS)) + 2
    for name in _FIELDS:
        lines.append(f"  {name:<{width}}{getattr(config, name)!s:<48} [{config.sources[name]}]")
    if stored is None:
        lines.append("  runtime settings (no store yet; `serve` seeds it with these values):")
    else:
        lines.append("  runtime settings (the store's value wins; the file only seeds keys the store lacks):")
    for name, entry in effective_runtime_settings(config, stored).items():
        if entry["source"] == "store":
            origin = f"store, set by {entry['actor'] or 'unknown'}; file {entry['file_value']!s}"
        else:
            origin = entry["source"]
        lines.append(f"    {name:<{width - 2}}{entry['value']!s:<48} [{origin}]")
    lines.append(f"  {'listen_url':<{width}}{config.listen_url}")
    lines.append(f"  {'webhook_url':<{width}}{config.webhook_url}")
    lines.append(f"  {'mode':<{width}}{config.mode}")
    for name, file_path in config.credential_paths().items():
        state = "present" if file_path.is_file() else "missing"
        lines.append(f"  {'credential':<{width}}{name:<48} {state}")
    if problems:
        lines.append("problems:")
        lines.extend(f"  - {problem}" for problem in problems)
    else:
        lines.append("ok: no problems found")
    return "\n".join(lines)
