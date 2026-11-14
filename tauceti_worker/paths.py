"""tauceti_worker.paths — install-location detection and self-invocation helpers.

`HERE` is the single source of truth for where the worker's bundled assets (prompts/, scripts/)
and runtime dirs (state/, checkouts/, logs/) live. It must resolve to the SAME directory the
old single-file `tauceti` used: the repo root in a source checkout, and the package dir in an
installed wheel. We detect which by asking whether `prompts/` sits beside the modules — only true
in the wheel (where pyproject force-includes it into the package); in a checkout prompts/ stays at
the repo root, one level up.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

_pkg = Path(__file__).resolve().parent  # …/tauceti_worker
HERE = _pkg if (_pkg / "prompts").is_dir() else _pkg.parent
INSTALLED = HERE == _pkg  # a wheel (uv tool, pip, Fleet runtime), not a source checkout


def login_home() -> Path:
    """The account's home from the password database. Worker isolation changes $HOME."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path(os.path.expanduser("~"))


def user_data_root() -> Path:
    """Per-user worker data for installed packages, outside the replaceable install.

    Anchored at the login home, never $HOME: an isolated worker runs with $HOME inside its own
    state, and must keep selecting the root that holds that state."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and Path(xdg).expanduser().is_absolute():  # the spec ignores relative values
        return Path(xdg).expanduser() / "tauceti"
    if sys.platform == "darwin":
        return login_home() / "Library" / "Application Support" / "tauceti" / "data"
    return login_home() / ".local" / "share" / "tauceti"


def held_lock_files() -> set[tuple[int, int]] | None:
    """(st_dev, st_ino) of files that are locked (Linux) or open (macOS) right now, or None.

    Observed without taking any lock: probing with flock could make an older worker's own
    exclusive acquisition fail, and an unmanaged loop then runs without its slot reservation."""
    import re

    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        text = None
    if text is not None:
        held = set()
        for line in text.splitlines():
            if "->" in line:
                continue  # a waiter, not a holder
            for token in line.split():
                if match := re.fullmatch(r"([0-9a-f]+):([0-9a-f]+):(\d+)", token):
                    held.add((os.makedev(int(match[1], 16), int(match[2], 16)), int(match[3])))
        return held
    if sys.platform != "darwin":
        return None
    try:
        import psutil
    except ImportError:
        return None
    held = set()
    for process in psutil.process_iter():
        try:
            if process.uids().real != os.getuid():
                continue
            files = process.open_files()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            continue  # system agents; worker processes are always inspectable
        for item in files:
            try:
                info = os.stat(item.path)
            except OSError:
                continue
            held.add((info.st_dev, info.st_ino))
    return held


def _older_generation_live(root: Path) -> bool:
    """Whether a worker or round of an earlier installation still holds a lock under ``root``.

    Such a loop keeps its package-relative root until it exits, and the rounds it starts run the
    upgraded code; they must keep using its root rather than split from it. Unknown counts as
    live: the package directory then simply stays selected, as before this change."""
    try:
        locks = [*root.glob("state/*/instance.lock"), *root.glob("state/*/round.lock")]
    except OSError:
        return False
    if not locks:
        return False
    held = held_lock_files()
    if held is None:
        return True
    # On overlayfs, stat() can report a layer's device while /proc/locks reports the overlay's,
    # so an inode number alone also counts; a false match only keeps the old root selected.
    inodes = {ino for _dev, ino in held}
    for lock in locks:
        try:
            info = lock.stat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) in held or info.st_ino in inodes:
            return True
    return False


def legacy_data_root(default: Path | None = None) -> Path:
    """Where worker state/, checkouts/ and logs/ live.

    An explicit TAUCETI_LEGACY_DATA_ROOT wins and is inherited by round, verification and
    cleanup subprocesses, including isolated worker homes. A source checkout keeps them beside
    the code (gitignored). An installed package uses user_data_root(): data inside the package
    directory was silently kept across `uv tool upgrade` yet destroyed by `--force` or
    `uninstall`, and the collector deletes what earlier installations left there. The default
    is resolved once per process and exported, so every descendant agrees on it.
    """
    raw = os.environ.get("TAUCETI_LEGACY_DATA_ROOT")
    if not raw:
        if default is not None and default != HERE:
            return default
        if not INSTALLED:
            return HERE
        root = HERE if _older_generation_live(HERE) else user_data_root()
        os.environ["TAUCETI_LEGACY_DATA_ROOT"] = str(root)
        return root
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("TAUCETI_LEGACY_DATA_ROOT must be an absolute path")
    return root.resolve()


# The branch-lease helper the agents run on PATH inside a round. Overridable for tests.
CLAIM_SH = os.environ.get("TAUCETI_CLAIM_SH") or str(HERE / "scripts" / "claim.sh")


def canonical_candidate(path: Path) -> Path:
    """Resolve only the managed candidate, using the kernel's spelling on Darwin."""
    result = Path(os.path.realpath(path))
    if sys.platform == "darwin":
        import fcntl

        command = getattr(fcntl, "F_GETPATH", None)
        if command is not None:
            descriptor = os.open(result, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                raw = fcntl.fcntl(descriptor, command, b"\0" * 1024).split(b"\0", 1)[0]
                if not raw.startswith(b"/"):
                    raise OSError("candidate kernel path unavailable")
                result = Path(os.fsdecode(raw))
            finally:
                os.close(descriptor)
    return result


# CPython builds do not agree on the default CA-bundle path. In particular, uv's standalone
# CPython uses OpenSSL's /etc/ssl/cert.pem default on NixOS, while NixOS exposes the system bundle
# at /etc/ssl/certs/ca-certificates.crt. A worker service has no login shell to supply
# SSL_CERT_FILE, so quota telemetry otherwise fails closed before the worker can select any work.
_COMMON_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, NixOS
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # macOS, Alpine
)


def _ssl_cert_candidates(env: Mapping[str, str]) -> tuple[str | None, ...]:
    # get_default_verify_paths().cafile is shadowed by the process's SSL_CERT_FILE, which may
    # differ from *env*. openssl_cafile is the interpreter's environment-independent default.
    import ssl

    openssl_default = ssl.get_default_verify_paths().openssl_cafile
    return (env.get("NIX_SSL_CERT_FILE"), openssl_default, *_COMMON_CA_BUNDLES)


def ensure_ssl_cert_file(env: MutableMapping[str, str] | None = None) -> str | None:
    """Put a usable system CA bundle in *env* when its Python has no working default.

    Preserve an explicit ``SSL_CERT_FILE`` even when it is unusual: operator configuration wins.
    Otherwise prefer Nix's advertised bundle, the interpreter's existing default, then common
    system locations. Return the selected path, or ``None`` when no usable bundle can be found.
    """
    base = os.environ if env is None else env
    configured = base.get("SSL_CERT_FILE")
    if configured:
        return configured
    for raw in _ssl_cert_candidates(base):
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_file():
                base["SSL_CERT_FILE"] = str(candidate)
                return str(candidate)
        except (OSError, RuntimeError):
            continue
    return None


def self_argv(*tail) -> list[str]:
    """argv to re-invoke this worker as a child (a `_round` or `_heartbeat` subprocess). `-m`
    works from a source checkout (paired with self_env's PYTHONPATH) and from an installed wheel
    alike — unlike the old `[sys.executable, __file__]`, which pointed at a now-unrunnable module."""
    return [sys.executable, "-m", "tauceti_worker", *(str(a) for a in tail)]


def self_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a self_argv child: ensure the package is importable. In a source checkout
    HERE is the repo root (which contains tauceti_worker/), so putting it on PYTHONPATH lets the
    child `python -m tauceti_worker`; in a wheel the package is already importable and this is a
    harmless no-op."""
    from .build_resources import defaults

    base = defaults(dict(os.environ if env is None else env))
    existing = base.get("PYTHONPATH")
    base["PYTHONPATH"] = os.pathsep.join([str(HERE)] + ([existing] if existing else []))
    ensure_ssl_cert_file(base)
    return base


def entry_cmd() -> list[str]:
    """The user-facing command that runs this worker, for the TUI's copy/launch helpers. A source
    checkout has the executable `./tauceti` shim beside HERE; an installed wheel puts `tauceti` on
    PATH as a console script."""
    shim = HERE / "tauceti"
    return [str(shim)] if shim.exists() else ["tauceti"]
