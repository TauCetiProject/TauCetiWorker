"""tauceti_worker.config — per-worker Config resolution, the roadmap/claims env dials, worker-slot
locking, and logging."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .constants import MAX_OPEN_PRS, ROADMAP, ROADMAP_DIR, TAUCETI
from .paths import HERE

_SCP_GIT_URL_RE = re.compile(r"^[^/@\s]+@[^:\s]+:.+$")


def positive_integer(value) -> int:
    """Strict positive policy integer, shared by CLI, environment and TOML."""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("must be a positive integer")
    result = int(value)
    if result < 1:
        raise ValueError("must be a positive integer")
    return result


def security_audit_problem(only, *, agent="auto", prs=(), author_override=False, api=False) -> str | None:
    """Static constraints on a worker that includes security-audit, shared by CLI and TOML.
    Other phases may accompany it; admin permission is checked when the worker runs."""
    if "security-audit" not in only:
        return None
    if prs:
        return "security-audit cannot be combined with --pr"
    if agent not in ("auto", "codex", "claude"):
        return "security-audit supports Codex Luna or Claude Haiku only"
    if author_override:
        return "security-audit uses its dedicated Luna/Haiku profile; omit author-model/effort overrides"
    if api:
        return "security-audit currently uses paced subscription CLI sessions"
    return None


def max_open_prs() -> int:
    return positive_integer(os.environ.get("TAUCETI_MAX_OPEN_PRS", MAX_OPEN_PRS))


def is_git_url(value: str) -> bool:
    """Whether value has a URL shape accepted by `git clone` (including scp-style SSH URLs)."""
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return False
    return scheme in {"http", "https", "ssh", "git", "file"} or bool(_SCP_GIT_URL_RE.fullmatch(value))


def roadmap_only() -> str | None:
    """The single roadmap area the worker steers toward, as the operator set it — read live from the
    env each call so the TUI's [o] key can change it and have both the survey display and any launched
    round pick it up (children inherit TAUCETI_ROADMAP_ONLY). Tri-state: None = unset, so a fresh
    random area is picked before each work survey; "" = all areas; else the area name. There is
    deliberately no baked-in default, and nothing here consults the dashboard prefs — a bare CLI run
    resolves only from the env + the live area list, never from a saved dashboard preference."""
    return os.environ.get("TAUCETI_ROADMAP_ONLY")


def roadmap_skip() -> list[str]:
    """Roadmap areas to exclude from selection (so concurrent workers can divide the roadmap), read
    live from the env each call — the TUI's [x] key and any launched round both see changes (children
    inherit TAUCETI_ROADMAP_SKIP). Comma-separated; whitespace and empty entries are dropped; the
    result is deduped and sorted. Returns [] when unset/blank. Shapes the auto-random pick and the
    "all areas" (any) case; an explicit --roadmap-only area takes precedence over it (see do_roadmap)."""
    raw = os.environ.get("TAUCETI_ROADMAP_SKIP", "")
    return sorted({tok for tok in (t.strip() for t in raw.split(",")) if tok})


EXTRA_ROADMAP_PROMPT_MAX_CHARS = 4096


def validate_extra_roadmap_prompt(value: str) -> str:
    """Normalise an operator's extra roadmap prompt, or raise ValueError. Shared by the `--extra-roadmap-prompt` flag
    and the `extra_roadmap_prompt` workers.toml field so a bad value fails at the configuration boundary
    rather than inside a round. Blank text is rejected (unset is the way to have none); the cap keeps
    a pasted file from crowding out the task prompt itself."""
    text = value.strip()
    if not text:
        raise ValueError("extra roadmap prompt must not be empty")
    if "\0" in text:
        raise ValueError("extra roadmap prompt must not contain a NUL byte")
    if len(text) > EXTRA_ROADMAP_PROMPT_MAX_CHARS:
        raise ValueError(
            f"extra roadmap prompt is {len(text)} characters; the limit is {EXTRA_ROADMAP_PROMPT_MAX_CHARS}"
        )
    return text


def extra_roadmap_prompt() -> str | None:
    """Operator-supplied guidance for roadmap rounds, read live from TAUCETI_EXTRA_ROADMAP_PROMPT so loop
    children and dashboard-launched rounds inherit it. Rendered into the roadmap prompt as a block of
    steering text: it refines the choice of target within the designated roadmap but never overrides
    the skip list, other contributors' claims, or the repository's hard rules. None when unset or
    blank."""
    raw = os.environ.get("TAUCETI_EXTRA_ROADMAP_PROMPT", "")
    return raw.strip() or None


def roadmap_extra_identities() -> list[str]:
    """Additional GitHub logins, beyond the worker's own `gh auth` identity, whose registered
    intentions this worker should treat as its own (so it won't avoid targets they've claimed).
    Comma-separated, read live from TAUCETI_ROADMAP_EXTRA_IDENTITIES; deduped and lowercased.
    Returns [] when unset/blank. Never hardcodes an account: who the worker is is whoever ran
    `gh auth`; this only widens that set when the operator opts in."""
    raw = os.environ.get("TAUCETI_ROADMAP_EXTRA_IDENTITIES", "")
    return sorted({tok.lower() for tok in (t.strip() for t in raw.split(",")) if tok})


def respect_claims() -> bool:
    """Whether roadmap workers avoid targets claimed by other contributors on the intentions
    board. On by default; read live from TAUCETI_RESPECT_CLAIMS so --ignore-claims (which sets it
    false) is inherited by loop children."""
    return os.environ.get("TAUCETI_RESPECT_CLAIMS", "true").strip().lower() not in ("0", "false", "no", "off")


def _only_label(sv=None) -> str:
    """Friendly render of the roadmap-only area for the status bar. Prefers the survey's sanitized
    value ("auto"/"any"/area); falls back to the raw env tri-state before the first survey lands."""
    v = sv.roadmap_only if (sv is not None and sv.roadmap_only) else roadmap_only()
    if v is None or v == "auto":
        return "auto (random each round)"
    return "all areas" if v in ("", "any") else v


def _skip_label(sv=None) -> str:
    """Friendly render of the skipped roadmap areas for the status bar."""
    areas = sv.roadmap_skip if (sv is not None and sv.roadmap_skip) else roadmap_skip()
    return ", ".join(areas) if areas else "none"


def roadmap_areas(gh) -> list[str]:
    """The roadmap areas a user can steer toward: the subdirectories of the roadmap repo (each
    holds a README.md + Suggested.lean). Listed over the API so the TUI can offer a picker. Returns []
    if it can't be fetched (the picker then falls back to free-text entry)."""
    from .shared_repositories import snapshot

    try:
        tree = snapshot(ROADMAP, ttl=3600)
        if tree:
            return sorted(path.name for path in (tree / ROADMAP_DIR).iterdir() if path.is_dir())
    except (OSError, TimeoutError, subprocess.SubprocessError):
        pass
    out = gh.api_jq(f"repos/{ROADMAP}/contents/{ROADMAP_DIR}", '.[] | select(.type=="dir") | .name')
    return sorted(out.splitlines()) if out else []


def roadmap_subareas(gh, area: str) -> list[str]:
    """The sub-roadmaps nested in `area`: its subdirectories holding a Suggested.lean (a references
    folder holds none). An intention may name only the sub-roadmap. Returns [] if it can't be fetched."""
    from .shared_repositories import snapshot

    try:
        tree = snapshot(ROADMAP, ttl=3600)
        if tree:
            root = tree / ROADMAP_DIR / area
            return sorted(p.name for p in root.iterdir() if (p / "Suggested.lean").is_file()) if root.is_dir() else []
    except (OSError, TimeoutError, subprocess.SubprocessError):
        pass
    out = gh.api_jq(
        f"repos/{ROADMAP}/git/trees/HEAD:{ROADMAP_DIR}/{area}?recursive=1",
        r'.tree[] | select(.type=="blob") | .path | select(test("^[^/]+/Suggested\\.lean$")) | split("/")[0]',
    )
    return sorted(out.splitlines()) if out else []


def sanitize_wid(raw: str) -> str:
    s = raw.lower()
    return re.sub(r"[^a-z0-9-]", "-", s)


# Slots held by the current process for its whole life. The fds must NOT be closed (closing releases the
# flock and frees the slot); keeping the ints here also documents the intentional hold and prevents any
# tooling from flagging them as leaks.
_HELD_SLOTS: list[int] = []


def legacy_project() -> bool:
    return TAUCETI.lower() == "taucetiproject/tauceti"


def worker_state_root() -> Path:
    if legacy_project():
        return HERE / "state"
    return Path(os.environ["TAUCETI_PROJECT_STATE"]) / "workers"


def acquire_slot(wid: str) -> bool:
    """Hold an instance lock on this worker slot for the life of the process, returning False if another
    process already holds it. The lock auto-releases on exit/crash (the kernel drops the flock), so a
    restart reclaims the slot — reusing its seeded $HOME and recognising its own leases. Mirrors
    RoundContext's flock-NB pattern, one level up (per process, not per round)."""
    state = worker_state_root() / wid
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "instance.lock", os.O_CREAT | os.O_WRONLY, 0o644)
    os.set_inheritable(fd, False)  # spawned rounds take their own round.lock; don't inherit this one
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _HELD_SLOTS.append(fd)
    return True


def auto_assign_wid(limit: int = 64) -> str:
    """Pick the lowest-numbered free worker slot (worker1, worker2, ...) and hold it, so several
    `work --loop` terminals on one host get distinct ids without the operator hand-numbering them."""
    for n in range(1, limit + 1):
        wid = f"worker{n}"
        if acquire_slot(wid):
            return wid
    raise Die(f"all {limit} worker slots are busy — pass an explicit --worker-id")


@dataclass(frozen=True)
class Config:
    """Resolved per-worker configuration (paths derived from the worker id)."""

    wid: str
    home: Path  # the LOGIN home: where per-user credential stores live (macOS Keychain, gh, git)
    data_home: Path  # the worker's own data root: per-worker, and independent of where $HOME points
    state: Path  # HERE/state/<wid>
    checkout: Path  # host authoring checkout
    store_dir: Path  # tauceti-review persistent store
    sbcache: Path  # scoreboard meta cache dir
    logdir: Path  # HERE/logs/<wid>
    quota_cache: Path  # raw provider usage responses

    @property
    def store(self) -> Path:
        return self.store_dir / "ledger.json"

    @staticmethod
    def resolve(worker_id: str | None = None, home: Path | None = None) -> Config:
        wid = sanitize_wid(worker_id or os.environ.get("TAUCETI_WORKER_ID", "default") or "default")
        # Export the resolved id so claim.sh (acquire / heartbeat-renew / git-safe-push's lease check)
        # all share ONE stable owner identity. Without this it falls back to `hostname-$$`, a different
        # owner per claim.sh invocation, so in host mode a worker can't renew or recognise its own
        # branch/<pr> lease and git-safe-push fails closed with "lease lost (another agent took over)".
        os.environ["TAUCETI_WORKER_ID"] = wid
        if os.environ.get("TAUCETI_FLEET_EVENTS"):
            os.environ["TAUCETI_FLEET_PYTHON"] = sys.executable
            os.environ["TAUCETI_FLEET_EMITTER"] = str(Path(__file__).with_name("fleet_events.py").resolve())
        # Lake may otherwise leave a cache hit only in its local store, where TauCeti's later
        # `lake exe axioms` and `lake exe module-system` audits cannot resolve it. Enable the store
        # and restore its artifacts into the build directory as one default policy. A manager's
        # per-worker `env` table is already present by this point, so setdefault preserves explicit
        # experiments and opt-outs.
        os.environ.setdefault("LAKE_ARTIFACT_CACHE", "1")
        os.environ.setdefault("LAKE_RESTORE_ARTIFACTS", "1")
        h = home or Path(os.environ.get("HOME", os.path.expanduser("~")))
        # Export before worker HOME isolation so every local worker shares receipts.
        os.environ.setdefault(
            "TAUCETI_ACTIVITY_DIR",
            str(h / ".local/state/tauceti/activity")
            if legacy_project()
            else str(worker_state_root().parent / "activity"),
        )
        # Two different homes, and conflating them is what made the macOS isolation change risky.
        # `home` answers "whose credentials?" and must follow the login user, because the macOS
        # Keychain, gh and git are per-login-user stores. `data_home` answers "whose working data?"
        # and must follow the WORKER, because the review store holds an outbox of unpublished
        # records and the scoreboard/thread ids: point it somewhere new and a worker silently
        # abandons reviews it never sent and re-posts scoreboards it already posted.
        #
        # isolate_home() exports $TAUCETI_DATA_HOME (children inherit it); an unisolated worker has
        # none and its data lives under the login home exactly as before. Every isolated worker's
        # data path is therefore the same one it had when isolation moved $HOME, on both platforms,
        # so nothing migrates.
        dh = Path(os.environ.get("TAUCETI_DATA_HOME") or h)
        state = worker_state_root() / wid
        # Per-worker, per-repository claim scratch. claim.sh defaults this under $HOME, which was
        # per-worker only while $HOME moved; use the worker data root while letting a dynamic
        # CLAIM_REPO select its own child store. An explicit CLAIM_GITDIR still overrides the base.
        os.environ.setdefault("CLAIM_GITDIR_BASE", str(dh / ".cache" / "tauceti-claims"))
        return Config(
            wid=wid,
            home=h,
            data_home=dh,
            state=state,
            checkout=(HERE / "checkouts" / wid / "TauCeti")
            if legacy_project()
            else worker_state_root() / wid / "checkout",
            store_dir=dh / ".cache" / "tauceti-review" / wid / "store" / TAUCETI.replace("/", "__"),
            sbcache=state / "cache" / "scoreboard",
            logdir=(HERE / "logs" / wid) if legacy_project() else state / "logs",
            quota_cache=state / "cache",
        )


_LOG_FH = None  # set by set_log_file(): tee log() to a per-worker file
_LOG_DIR = None
_LOG_DAY = None
_LOG_LOCK = threading.RLock()

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")  # strip color codes from the on-disk copy


def set_log_file(logdir: Path) -> None:
    with _LOG_LOCK:
        _set_log_file(logdir)


def _set_log_file(logdir: Path) -> None:
    """Tee log() output to a per-worker file so a worker's activity is readable ON DISK, not only on the
    terminal it was launched from (a loop run directly in a shell otherwise leaves no log to inspect).
    The loop driver picks ONE timestamped file and exports its path so every round child appends to the
    SAME file — one continuous session log per worker; a standalone round with no parent makes its own.
    Idempotent and best-effort: a failure to open the file just leaves logging at stderr-only."""
    global _LOG_FH, _LOG_DIR, _LOG_DAY
    if _LOG_FH is not None:
        return
    path = os.environ.get("TAUCETI_LOG_FILE")
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y%m%d")
        if not path or (Path(path).parent == logdir and Path(path).name.startswith("work-")):
            _LOG_DIR, _LOG_DAY = logdir, day
            path = str(logdir / f"work-{day}.log")
            os.environ["TAUCETI_LOG_FILE"] = path  # children inherit and append to the same file
        _LOG_FH = open(path, "a", buffering=1)  # line-buffered: each log line flushes on its newline
    except OSError:
        _LOG_FH = None


def one_line(text: str, limit: int = 200) -> str:
    """`text` collapsed to a single trimmed line, capped at `limit`. A subprocess's stderr is the
    reason an operator needs, but a status field and a log line are one line each, and a raw multi-line
    tail turns either into a wall."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


def rotate_console_log():
    """Move a managed loop's own stdout/stderr to today's file without restarting its child work."""
    current = os.environ.get("TAUCETI_CONSOLE_LOG_FILE")
    if not current:
        return
    day = time.strftime("%Y%m%d")
    if os.environ.get("TAUCETI_CONSOLE_LOG_DAY") == day:
        return
    with _LOG_LOCK:
        if os.environ.get("TAUCETI_CONSOLE_LOG_DAY") == day:
            return
        target = Path(current).parent / f"work-{day}.log"
        try:
            original = os.stat(current)
            descriptors = []
            for descriptor in (1, 2):
                with contextlib.suppress(OSError):
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino):
                        descriptors.append(descriptor)
            if not descriptors:
                return  # A helper's pipe/result file is unrelated to its inherited console metadata.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                for stream in (sys.stdout, sys.stderr):
                    with contextlib.suppress(OSError, ValueError):
                        stream.flush()
                for descriptor in descriptors:
                    os.dup2(fd, descriptor)
            finally:
                os.close(fd)
            os.environ.update(TAUCETI_CONSOLE_LOG_FILE=str(target), TAUCETI_CONSOLE_LOG_DAY=day)
            from .runtime_status import report_runtime

            report_runtime(log_file=str(target))
        except OSError:
            pass  # Keep the current console usable; retry at the next ordinary loop tick.


def log(msg: str) -> None:
    global _LOG_FH

    rotate_console_log()
    line = f"{time.strftime('%F %T')} tauceti: {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
    with _LOG_LOCK:
        if _LOG_DIR is not None and _LOG_DAY != time.strftime("%Y%m%d"):
            if _LOG_FH is not None:
                with contextlib.suppress(OSError, ValueError):
                    _LOG_FH.close()
            _LOG_FH = None
            set_log_file(_LOG_DIR)
        if _LOG_FH is not None:
            try:
                _LOG_FH.write(_ANSI_RE.sub("", line) + "\n")
            except (OSError, ValueError):
                pass


_RED, _RESET = "\033[1;31m", "\033[0m"


def warn_red(msg: str) -> None:
    """A bright-red, attention-demanding log line (a PR the automation can't make progress on)."""
    log(f"{_RED}⚠ {msg}{_RESET}")


class Die(Exception):
    """Fatal error → exit 1."""


class NoProgress(Exception):
    """Round did no productive work → exit EX_NOPROGRESS (75)."""


class ClaimCollision(NoProgress):
    """Available candidates were claimed by peers; reselect without failure backoff."""


class ProgressPreparationUnavailable(NoProgress):
    """Mechanical progress preparation needs time or infrastructure, without a paid attempt."""

    state = "waiting-progress-preparation"

    def __init__(self, message, *, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


class DocumentationUnavailable(ProgressPreparationUnavailable):
    """Documentation deployment churn; replan without spending a report attempt."""

    state = "waiting-documentation"


class AdmissionUnavailable(NoProgress):
    """Inference admission needs a bounded recheck, without no-progress backoff."""

    def __init__(self, message: str, *, state: str = "waiting-budget"):
        super().__init__(message)
        self.state = state


class GitHubUnavailable(NoProgress):
    """GitHub infrastructure pause; completed work and PR attempt budgets survive."""

    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class SharedStateUnavailable(GitHubUnavailable):
    """A shared-state dependency cannot establish current evidence; wait for it, never treat it as no work.

    `failure` is github_state.last_failure(): dependency, reason, sanitized detail, retry time and the
    provenance of any retained complete response. It never contains credentials.
    """

    def __init__(self, message: str, failure: dict):
        super().__init__(message, retry_at=failure.get("retry_at"))
        self.failure = failure
        self.dependency = failure.get("dependency", "github-state")
        self.reason = failure.get("reason", "transport")
