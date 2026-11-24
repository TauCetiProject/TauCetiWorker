"""The offline ``bwrap`` sandbox for the repair worker's ``lake build`` runs.

CI's required build (TauCeti ``.github/workflows/pr-build.yml``, step "Build exact candidate under
bwrap with workflow-pinned audits, offline", running TauCetiTools ``sandbox-build.sh``) compiles
candidate Lean under ``bwrap`` with no network, a read-only synthetic root, the elan toolchains
read-only, the candidate's sources read-only and only its ``.lake`` writable, from a cleared
environment. Every ``lake build`` the repair worker runs, its own verification pass and the agent's
scoped builds through ``.tauceti-repair/build``, goes through :func:`sandboxed_lake` so it runs in
the same confinement on the worker host.

Host-specific differences from CI: the system roots are whichever of ``/usr``, ``/bin``, ``/lib``,
``/etc``, ``/nix``... exist (a NixOS host keeps its tools under ``/nix`` and ``/run/current-system``);
the worker's private artifact pool (``LAKE_CACHE_DIR``) is a writable bind, since Lake stores every
built artifact there; ``LAKE_CONFIG`` and a shared ``.lake/packages`` (``--seed-packages``) are
read-only binds; the content clone's Git directory is read-only so Lake's Git queries resolve.

Credentials stay outside: the sandbox binds nothing of ``$HOME`` beyond the toolchain, and every
credential path :func:`credential_paths` knows (the queue App's directory, the worker token file,
the repair-cache write key, the provider and ``gh`` config directories, ``~/.ssh`` and the like) that
lies inside a bound tree is covered by an empty ``tmpfs`` (a directory) or ``/dev/null`` (a file). A
bound tree inside a credential directory is refused. :func:`check` confirms from inside the sandbox
that none of them can be read.

The repair agent's own ``lake``, ``lean`` and ``elan`` commands reach the same sandbox through the
shims of ``repair_agent_tools``, which pass the caller's directory as ``cwd``.

The worker's cache fetches go through :func:`sandboxed_fetch`, the same sandbox online.

``TAUCETI_QUEUE_SANDBOX=0`` turns the sandbox off for hosts without ``bwrap``; every build then logs
a warning that it ran unsandboxed. ``python -m tauceti_queue.repair_sandbox [--worktree DIR] -- lake
build ...`` runs one command under the sandbox (the helper scripts' entry point); ``--check`` runs
the credential probe inside it.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DISABLE_VARIABLE = "TAUCETI_QUEUE_SANDBOX"
# Set to 1 by the worker while it runs a background task (``repair_task.background_task``), and so
# inherited by the agent and its build script: every sandboxed build then runs at niceness 19.
BACKGROUND_VARIABLE = "TAUCETI_REPAIR_BACKGROUND"
LOGGER = logging.getLogger("tauceti_queue.repair_sandbox")
PRESSURE_WRAPPER = "/opt/tauceti/tauceti-lean"

# Read-only system roots, bound when present. A symlinked root (``/bin -> usr/bin``) is recreated as
# the same symlink, the way CI's ``--symlink usr/bin /bin`` does.
SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/nix", "/run/current-system")
# Lake's artifact-cache settings, passed through from the build's environment as CI passes them.
PASSED_ENV = ("LAKE_ARTIFACT_CACHE", "LAKE_RESTORE_ARTIFACTS", "LAKE_CACHE_DIR", "LAKE_CONFIG", "ELAN_HOME")
# Passed through to a networked sandbox (``network=True``): TLS trust and proxies for the fetch.
NETWORK_ENV = (
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
# Mathlib's cache tool (``lake exe cache get``) reads and writes this directory; ``repair.lake_env``
# points it at the workers' own one, never the host's ``~/.cache/mathlib``, which unsandboxed Lean
# elsewhere on the host imports from.
MATHLIB_CACHE_VARIABLE = "MATHLIB_CACHE_DIR"
# Retain the caller's cgroup namespace: the pressure wrapper needs every capped ancestor,
# with /proc/self/cgroup paths matching the read-only host cgroup mount.
NAMESPACE_FLAGS = (
    "--unshare-user",
    "--unshare-ipc",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-uts",
    "--disable-userns",
    "--die-with-parent",
    "--new-session",
)


# Credential locations, by variable (the worker's environment) and under $HOME. The worker passes
# the resolved list to its agents in HIDDEN_VARIABLE, since their environment drops the variables.
CREDENTIAL_VARIABLES = (
    "TAUCETI_QUEUE_GITHUB_APP",
    "TAUCETI_QUEUE_TOKEN_FILE",
    "TAUCETI_REPAIR_CACHE_KEY_FILE",
    "TAUCETI_QUEUE_CONFIG",
    "TAUCETI_CLAUDE_USAGE_CREDENTIALS",
    "TAUCETI_ANTHROPIC_KEY_FILE",
    "TAUCETI_FORGEJO_TOKEN_FILE",
    "TAUCETI_GITHUB_STATE_TOKEN_FILE",
    "TAUCETI_SECURITY_AUDIT_ANTHROPIC_API_KEY_FILE",
    "CREDENTIALS_DIRECTORY",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GH_CONFIG_DIR",
)
HOME_CREDENTIALS = (
    ".config/tauceti-queue",
    ".config/gh",
    ".claude",
    ".claude.json",
    ".codex",
    ".ssh",
    ".gnupg",
    ".netrc",
    ".git-credentials",
    ".aws",
    ".config/rclone",
)
HIDDEN_VARIABLE = "TAUCETI_SANDBOX_HIDDEN"
# Trees a sandbox may bind only read-only (the shared ``--seed-packages`` Mathlib), as a path list;
# ``TAUCETI_QUEUE_SEED_PACKAGES`` counts too. A worktree inside one is refused, since its ``.lake``
# would be writable.
READ_ONLY_VARIABLE = "TAUCETI_SANDBOX_READONLY"


class SandboxUnavailable(RuntimeError):
    """``bwrap`` is required (the sandbox is not disabled) but is not on PATH."""


def disabled(env: dict | None = None) -> bool:
    return (os.environ if env is None else env).get(DISABLE_VARIABLE, "").strip().lower() in ("0", "false", "no", "off")


def warn_disabled(argv: list[str]) -> None:
    LOGGER.warning(
        "%s=0: running `%s` WITHOUT the bwrap sandbox (network, home and the whole filesystem are reachable)",
        DISABLE_VARIABLE,
        " ".join(argv),
    )


def _elan_home(env: dict) -> Path:
    if env.get("ELAN_HOME"):
        return Path(env["ELAN_HOME"])
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".elan"


def _git_dir(worktree: Path) -> Path | None:
    """The Git directory a worktree's ``.git`` file points into (the content clone's common dir)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    if path is None or not path.is_dir() or path.is_relative_to(worktree):
        return None  # a worktree's own .git directory is already under the read-only worktree bind
    return path


def credential_paths(env: dict | None = None) -> list[Path]:
    """Every credential location this environment names or implies, absolute and deduplicated,
    whether or not it exists: the variables of ``CREDENTIAL_VARIABLES`` (a configuration file's
    directory for ``TAUCETI_QUEUE_CONFIG``), the entries of ``HIDDEN_VARIABLE`` and
    ``HOME_CREDENTIALS`` under ``$HOME``."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    found: list[Path] = []
    for key in CREDENTIAL_VARIABLES:
        value = (env.get(key) or "").strip()
        # A path, absolute or relative to this process's directory. A relative value names nothing
        # unless it exists, so a misplaced secret value is never copied anywhere.
        if value.startswith(("/", "~")) or (value and "\n" not in value and os.path.lexists(value)):
            found.append(Path(value).parent if key == "TAUCETI_QUEUE_CONFIG" else Path(value))
    found += [Path(p) for p in (env.get(HIDDEN_VARIABLE) or "").split(os.pathsep) if p.strip()]
    found += [home / name for name in HOME_CREDENTIALS]
    result: list[Path] = []
    for path in found:
        path = Path(os.path.abspath(os.path.expanduser(str(path))))
        for candidate in (path, path.resolve()):
            if candidate.is_absolute() and candidate not in result:
                result.append(candidate)
    return result


def _binds(args: list[str]) -> list[tuple[str, str]]:
    """The ``(source, destination)`` of every bind in sandbox arguments, ``/dev/null`` masks excluded."""
    return [
        (args[i + 1], args[i + 2])
        for i, a in enumerate(args)
        if a in ("--ro-bind", "--bind") and args[i + 1] != "/dev/null"
    ]


def linked_credentials(paths: list[Path]) -> list[Path]:
    """The targets of the symlinks directly inside each credential directory (a key file kept
    elsewhere and linked in), treated as credential paths themselves."""
    found: list[Path] = []
    for directory in paths:
        try:
            entries = list(os.scandir(directory)) if directory.is_dir() else []
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                target = Path(entry.path).resolve()
                if target not in paths and target not in found:
                    found.append(target)
    return found


def exposures(binds: list[tuple[str, str]], env: dict) -> list[tuple[Path, Path]]:
    """Where each credential path would appear inside the sandbox: ``(inside, host)`` for every bind
    whose resolved source contains the credential's resolved path, the inside path being that bind's
    destination joined with the remainder (a symlinked source shows its target's files under its own
    name). Raises :class:`SandboxUnavailable` when a bound source lies inside a credential directory
    (or is one), which no mask could repair."""
    found: list[tuple[Path, Path]] = []
    named = credential_paths(env)
    for credential in [*named, *linked_credentials(named)]:
        host = credential.resolve()
        for source, destination in binds:
            resolved = Path(source).resolve()
            if resolved == host or resolved.is_relative_to(host):
                raise SandboxUnavailable(f"the sandbox would bind {source}, inside the credential path {credential}")
            if host.is_relative_to(resolved):
                pair = (Path(destination) / host.relative_to(resolved), host)
                if pair not in found:
                    found.append(pair)
    return found


def _credential_masks(binds: list[tuple[str, str]], env: dict) -> list[str]:
    """Mount arguments covering each existing credential path wherever a bind exposes it: an empty
    tmpfs on a directory, ``/dev/null`` on a file."""
    masks: list[str] = []
    for inside, host in exposures(binds, env):
        if host.is_dir():
            masks += ["--tmpfs", str(inside)]
        elif host.is_file():
            masks += ["--ro-bind", "/dev/null", str(inside)]
    return masks


# ``repair.link_shared_packages`` records the target it linked a worktree's ``.lake/packages`` to in
# this directory of the worker's root (the worktrees directory's parent), outside the checkout (which
# a candidate commit could fill) and outside every bind (so no sandbox can write it).
PACKAGES_RECORDS = "packages-links"


def packages_record(worktree: Path) -> Path:
    worktree = Path(os.path.abspath(worktree))
    return worktree.parent.parent / PACKAGES_RECORDS / worktree.parent.name / worktree.name


def _read_only_trees(env: dict) -> list[Path]:
    trees = [*(env.get(READ_ONLY_VARIABLE) or "").split(os.pathsep), env.get("TAUCETI_QUEUE_SEED_PACKAGES") or ""]
    return [Path(t).resolve() for t in trees if t.strip()]


def shareable_packages(target: Path, env: dict | None = None) -> bool:
    """Whether ``target`` may be bound read-only as a worktree's ``.lake/packages``: a real directory
    with no symlink on its path, never under ``/proc``, ``/dev``, ``/sys`` or ``/run``, and either
    inside a configured read-only tree or another worktree's own ``.lake/packages``."""
    env = os.environ if env is None else env
    target = Path(os.path.abspath(target))
    if target != target.resolve() or not target.is_dir():
        return False
    if any(target.is_relative_to(root) for root in ("/proc", "/dev", "/sys", "/run")):
        return False
    if any(target.is_relative_to(tree) for tree in _read_only_trees(env)):
        return True
    return target.name == "packages" and target.parent.name == ".lake"


def linked_packages(worktree: Path) -> list[Path]:
    """The ``.lake/packages`` targets the worker itself linked in ``worktree`` (:func:`packages_record`),
    as recorded: canonical when written and never resolved again, since a target inside another
    worktree's ``.lake`` is build state that could since have been replaced by a link elsewhere."""
    try:
        text = packages_record(worktree).read_text()
    except OSError:
        return []
    return [Path(line) for line in text.splitlines() if line.startswith("/")]


def _packages_binds(packages: Path, worktree: Path, read_only: list[Path], env: dict) -> list[str]:
    """The bind that makes a worktree's ``.lake/packages`` link resolve inside the sandbox, if any.

    The link is build state the batch's code can rewrite, so it is never followed on the host to pick
    a bind source. A link the worker recorded (:func:`linked_packages`) to another worktree's
    ``.lake/packages`` must still name that path literally; that worktree's ``.lake`` is bound, a
    directory no sandbox can rename or replace (its worktree is read-only inside every sandbox), so a
    later swap of its ``packages`` entry for a link resolves inside the sandbox, never on the host. A
    link into a configured read-only tree is bound at its resolved path, which no sandbox can change.
    Any other link dangles."""
    try:
        literal = Path(os.readlink(packages))
    except OSError:
        return []
    if literal.is_absolute() and literal in linked_packages(worktree) and literal.name == "packages":
        lake = literal.parent
        if lake.name == ".lake" and not lake.is_symlink() and lake.is_dir() and lake == lake.resolve():
            return ["--ro-bind", str(lake), str(lake)]
    shared = packages.resolve()
    if any(shared.is_relative_to(tree) for tree in read_only) and shareable_packages(shared, env):
        return ["--ro-bind", str(shared), str(shared)]
    return []


def _system_binds() -> list[str]:
    args: list[str] = []
    for root in SYSTEM_ROOTS:
        path = Path(root)
        if path.is_symlink():
            args += ["--symlink", os.readlink(path), root]
        elif path.exists():
            args += ["--ro-bind", root, root]
    return args


def _inside_path(env: dict, elan: Path) -> str:
    """CI's ``$HOME/.elan/bin:/usr/bin:/bin``: the toolchain proxies, then the host's PATH entries
    that lie under the read-only system roots. Nothing the sandbox can write is on it."""
    entries = [str(elan / "bin")]
    for entry in (env.get("PATH") or os.environ.get("PATH", "")).split(os.pathsep):
        if entry and entry not in entries and any(entry == r or entry.startswith(r + "/") for r in SYSTEM_ROOTS):
            entries.append(entry)
    for fallback in ("/usr/bin", "/bin"):
        if fallback not in entries:
            entries.append(fallback)
    return ":".join(entries)


def _pressure_env(worktree: Path, wrapper: str) -> dict[str, str]:
    """Reuse the worker's compiler gate, with its disposable overlay in writable build state."""
    return {
        "LAKE_OVERRIDE_LEAN": "true",
        "LEAN": wrapper,
        "XDG_CACHE_HOME": str(worktree / ".lake" / "pressure-cache"),
    }


def _pressure_source() -> Path:
    # Keep this helper stdlib-only: the agent's build script may use system Python.
    # Wheels bundle assets inside tauceti_worker; source checkouts keep them at the root.
    package = Path(__file__).resolve().parent.parent / "tauceti_worker"
    root = package if (package / "prompts").is_dir() else package.parent
    source = root / "scripts" / "tauceti-lean"
    if not source.is_file():
        raise SandboxUnavailable(f"the worker's bundled compiler pressure wrapper is missing: {source}")
    return source


def _pressure_executable(worktree: Path) -> Path:
    source = _pressure_source()
    if os.access(source, os.X_OK):
        return source
    # Wheel assets may be read-only and lack execute permission. Never modify the installation.
    content = source.read_bytes()
    name = "tauceti-lean-" + hashlib.sha256(content).hexdigest()
    cache = worktree / ".lake" / "pressure-cache"
    # Candidate build state is writable: do not follow symlinks when creating the trusted copy.
    lake_fd = os.open(worktree / ".lake", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir("pressure-cache", 0o700, dir_fd=lake_fd)
        except FileExistsError:
            pass
        cache_fd = os.open("pressure-cache", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=lake_fd)
    finally:
        os.close(lake_fd)
    temporary = ".wrapper-" + secrets.token_hex(12)
    try:
        try:
            existing_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cache_fd)
            with os.fdopen(existing_fd, "rb") as existing:
                mode = os.fstat(existing.fileno()).st_mode
                if stat.S_ISREG(mode) and mode & stat.S_IXUSR and existing.read(len(content) + 1) == content:
                    return cache / name
        except OSError:
            pass
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=cache_fd)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            os.fchmod(output.fileno(), 0o755)
        os.replace(temporary, name, src_dir_fd=cache_fd, dst_dir_fd=cache_fd)
        return cache / name
    finally:
        try:
            os.unlink(temporary, dir_fd=cache_fd)
        except FileNotFoundError:
            pass
        os.close(cache_fd)


def sandbox_argv(
    argv: list[str],
    worktree: Path,
    env: dict | None = None,
    *,
    bwrap: str | None = None,
    extra_env: dict | None = None,
    cwd: Path | None = None,
    network: bool = False,
) -> list[str]:
    """``argv`` wrapped in the offline sandbox (with ``network``, the same sandbox online, for the
    worker's cache fetches: no ``--unshare-net``, no ``LAKE_NO_CACHE``, ``NETWORK_ENV`` passed and
    Mathlib's cache directory ``MATHLIB_CACHE_DIR`` writable), run from ``worktree`` (or ``cwd``, a directory inside
    it), with ``extra_env`` set inside it (the audit and lint scripts' inputs, ``repair_audit``). Raises
    :class:`SandboxUnavailable` when ``bwrap`` is not on PATH, when ``cwd`` lies outside ``worktree``
    or when a bound tree lies inside a credential directory."""
    env = dict(os.environ if env is None else env)
    bwrap = bwrap or shutil.which("bwrap")
    if not bwrap:
        raise SandboxUnavailable(f"bwrap is not on PATH; install bubblewrap or set {DISABLE_VARIABLE}=0")
    worktree = Path(worktree).resolve()
    directory = worktree if cwd is None else Path(os.path.abspath(cwd))
    if not (directory.is_relative_to(worktree) or directory.resolve().is_relative_to(worktree)):
        raise SandboxUnavailable(f"{directory} is outside the sandboxed worktree {worktree}")
    read_only = _read_only_trees(env)
    for tree in read_only:
        if worktree.is_relative_to(tree):
            raise SandboxUnavailable(f"{worktree} is inside {tree}, which builds may only read")
    lake_dir = worktree / ".lake"
    lake_dir.mkdir(exist_ok=True)
    (lake_dir / "tmp").mkdir(exist_ok=True)
    elan = _elan_home(env)
    home = env.get("HOME") or os.path.expanduser("~")
    args = [shutil.which("env") or "/usr/bin/env", "-i", bwrap, "--tmpfs", "/", *_system_binds()]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    args += ["--ro-bind", str(_pressure_executable(worktree)), PRESSURE_WRAPPER]
    if Path("/sys/fs/cgroup").is_dir():
        args += ["--ro-bind", "/sys/fs/cgroup", "/sys/fs/cgroup"]
    if elan.is_dir():
        args += ["--ro-bind", str(elan), str(elan)]
    git_dir = _git_dir(worktree)
    if git_dir is not None:
        args += ["--ro-bind", str(git_dir), str(git_dir)]
    if env.get("LAKE_CONFIG") and Path(env["LAKE_CONFIG"]).is_file():
        args += ["--ro-bind", env["LAKE_CONFIG"], env["LAKE_CONFIG"]]
    if env.get("LAKE_CACHE_DIR"):
        cache = Path(env["LAKE_CACHE_DIR"])
        cache.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(cache), str(cache)]
    if network and env.get(MATHLIB_CACHE_VARIABLE):
        mathlib = Path(env[MATHLIB_CACHE_VARIABLE])
        mathlib.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(mathlib), str(mathlib)]
    if network:
        args += _network_binds(env)
    args += ["--ro-bind", str(worktree), str(worktree), "--bind", str(lake_dir), str(lake_dir)]
    packages = lake_dir / "packages"
    if packages.is_symlink():  # the shared extracted Mathlib (--seed-packages), read-only for every build
        args += _packages_binds(packages, worktree, read_only, env)
    args += _credential_masks(_binds(args), env)
    args += ["--remount-ro", "/"]
    args += ["--setenv", "PATH", _inside_path(env, elan), "--setenv", "HOME", home]
    # Offline, Lake restores only from the local pool, never a remote cache.
    if not network:
        args += ["--setenv", "LAKE_NO_CACHE", "true"]
    args += ["--setenv", "TMPDIR", str(lake_dir / "tmp")]
    for key in PASSED_ENV + ((*NETWORK_ENV, MATHLIB_CACHE_VARIABLE) if network else ()):
        if env.get(key):
            args += ["--setenv", key, env[key]]
    for key, value in (extra_env or {}).items():
        args += ["--setenv", key, str(value)]
    for key, value in _pressure_env(worktree, PRESSURE_WRAPPER).items():
        args += ["--setenv", key, value]
    flags = [f for f in NAMESPACE_FLAGS if not (network and f == "--unshare-net")]
    args += ["--chdir", str(directory), *flags, "--", *argv]
    return args


def prioritized(argv: list[str]) -> list[str]:
    """``argv`` at niceness 19 during a background task (``BACKGROUND_VARIABLE``), else unchanged.

    The repair units set no CPU weight of their own, so ``tauceti-repair.slice`` enables no CPU
    controller for them and every repair process on the host shares the slice's CPU by niceness:
    the head batch's builds, at the units' ``Nice=10``, get about seven times the CPU of a
    background build at 19 when both are busy. ``nice -n 19`` saturates at 19 from any start."""
    nice = shutil.which("nice") if os.environ.get(BACKGROUND_VARIABLE) == "1" else None
    return [nice, "-n", "19", *argv] if nice else argv


def sandboxed_lake(
    argv: list[str],
    worktree: Path,
    env: dict | None = None,
    *,
    extra_env: dict | None = None,
    cwd: Path | None = None,
    network: bool = False,
) -> list[str]:
    """The command to run for ``argv`` (a ``lake build ...``) from ``worktree``: wrapped in the
    sandbox, or an environment wrapper with a logged warning when ``TAUCETI_QUEUE_SANDBOX=0``
    (the caller then passes ``extra_env`` in the process environment). Both paths gate compilation
    and run at the task's priority (:func:`prioritized`)."""
    if disabled():
        warn_disabled(argv)
        worktree = Path(worktree).resolve()
        (worktree / ".lake").mkdir(exist_ok=True)
        return prioritized(
            [
                shutil.which("env") or "/usr/bin/env",
                "-u",
                "LEAN_SYSROOT",
                *(
                    f"{key}={value}"
                    for key, value in _pressure_env(worktree, str(_pressure_executable(worktree))).items()
                ),
                *argv,
            ]
        )
    return prioritized(sandbox_argv(argv, worktree, env, extra_env=extra_env, cwd=cwd, network=network))


# Files name resolution and TLS read, bound read-only into an online sandbox wherever they resolve
# (``/etc/resolv.conf`` often links into ``/run``, which is not bound); the CA variables' files too.
NETWORK_FILES = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")
CA_VARIABLES = ("SSL_CERT_FILE", "NIX_SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")


def _system(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in SYSTEM_ROOTS if Path(root).is_dir())


def _network_binds(env: dict) -> list[str]:
    """The resolver's files at their resolved paths (the system path is then a link into the bind),
    and each CA variable's file at the path the variable names, which is what a TLS client opens."""
    args: list[str] = []
    for path in NETWORK_FILES:
        target = Path(path).resolve()
        if target.is_file() and not _system(target):
            args += ["--ro-bind", str(target), str(target)]
    for path in dict.fromkeys(env[key] for key in CA_VARIABLES if env.get(key)):
        named, target = Path(os.path.abspath(path)), Path(path).resolve()
        if not target.is_file():
            continue
        if not _system(named):
            args += ["--ro-bind", str(target), str(named)]
        elif not _system(target):
            args += ["--ro-bind", str(target), str(target)]
    return args


# An elan toolchain name the worker installs itself: the official releases and nightlies only.
TOOLCHAIN = re.compile(r"leanprover/lean4(?:-nightly)?:(?:v\d+\.\d+\.\d+(?:-rc\d+)?|nightly-\d{4}-\d{2}-\d{2})")


def ensure_toolchain(worktree: Path, env: dict, timeout: float = 1800) -> None:
    """Install the toolchain ``worktree`` names, from outside the sandbox (whose elan home is
    read-only), when it is an official release that is not installed yet. Any other name is left to
    fail inside the sandbox. Installing runs nothing from the worktree."""
    try:
        name = (worktree / "lean-toolchain").read_text().strip()
    except OSError:
        return
    if not TOOLCHAIN.fullmatch(name):
        return
    directory = _elan_home(env) / "toolchains" / name.replace("/", "--").replace(":", "---")
    elan = shutil.which("elan", path=env.get("PATH"))
    if directory.is_dir() or not elan:
        return
    clean = {k: env[k] for k in ("PATH", "HOME", "ELAN_HOME", "LANG", *NETWORK_ENV) if env.get(k)}
    subprocess.run([elan, "toolchain", "install", name], env=clean, cwd="/", capture_output=True, timeout=timeout)


def sandboxed_python(code: str, args: list[str], tree: Path, env: dict | None = None) -> list[str]:
    """``python -I -c code args...`` of this worker's own interpreter and package, offline, with only
    ``tree`` writable (one bind, so hard links inside it work) and nothing else of the host but the
    system roots: for host-side maintenance of build-writable state, where a planted symlink must not
    resolve to the worker's files. Raises :class:`SandboxUnavailable` like :func:`sandbox_argv`."""
    env = dict(os.environ if env is None else env)
    bwrap = shutil.which("bwrap", path=env.get("PATH"))
    if not bwrap:
        raise SandboxUnavailable(f"bwrap is not on PATH; install bubblewrap or set {DISABLE_VARIABLE}=0")
    package = Path(__file__).resolve().parent.parent
    # The real interpreter and this process's import path, all at their real paths (a venv or a
    # versioned install is often reached through links that would not resolve inside), and the
    # outermost of those trees bound read-only.
    python = Path(sys.executable).resolve()
    trees = {package, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), python.parent.parent}
    # Only import-path entries inside those trees: `python -m` puts the working directory first.
    entries = (Path(p).resolve() for p in sys.path if p and Path(p).exists())
    path = [str(package), *dict.fromkeys(str(e) for e in entries if any(e.is_relative_to(t) for t in trees))]
    found = sorted(trees)
    roots = [r for r in found if not any(r != o and r.is_relative_to(o) for o in found) and not _system(r)]
    argv = [shutil.which("env") or "/usr/bin/env", "-i", bwrap, "--tmpfs", "/", *_system_binds()]
    argv += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for root in roots:
        argv += ["--ro-bind", str(root), str(root)]
    tree = Path(tree).resolve()
    argv += ["--bind", str(tree), str(tree)]
    argv += _credential_masks(_binds(argv), env)
    argv += ["--remount-ro", "/", "--setenv", "PATH", "/usr/bin:/bin", "--chdir", str(tree), *NAMESPACE_FLAGS]
    code = f"import sys; sys.path[:] = {path!r}\n" + code
    return [*argv, "--", str(python), "-I", "-c", code, *args]


def sandboxed_fetch(argv: list[str], worktree: Path, env: dict | None = None) -> list[str]:
    """A cache fetch (``lake cache get``, ``lake exe cache get``) from ``worktree``: the build sandbox
    online. A fetch loads the workspace's and its packages' Lake configurations and may run Mathlib's
    cache executable, all from ``.lake``, which the batch's code could write during an earlier build of
    the same tree; outside the sandbox that would run with the worker's credentials in reach. The
    toolchain is installed first (:func:`ensure_toolchain`)."""
    env = dict(os.environ if env is None else env)
    if not disabled(env):
        try:
            ensure_toolchain(Path(worktree), env)
        except (OSError, subprocess.TimeoutExpired) as error:
            LOGGER.warning("installing the toolchain for %s failed: %s", worktree, error)
    return sandboxed_lake(argv, worktree, env, network=True)


# Inside the sandbox: exit 1 naming the first argument that is a non-empty file or directory.
_CREDENTIAL_PROBE = (
    'for p; do if { [ -f "$p" ] && [ -s "$p" ]; } || { [ -d "$p" ] && [ -n "$(ls -A "$p" 2>/dev/null)" ]; }; then '
    'echo "readable: $p"; exit 1; fi; done'
)


def check(scratch: Path | None = None, env: dict | None = None) -> tuple[bool, str]:
    """Run the credential probe inside the sandbox; ``(ok, detail)`` for the worker preflight. It
    fails when any credential path of :func:`credential_paths` that holds something on the host can
    be read inside, as well as when the sandbox does not start."""
    if disabled(env):
        return True, f"DISABLED by {DISABLE_VARIABLE}=0: repair builds run unsandboxed"
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ if env is None else env)
    named = credential_paths(env)
    present = [p for p in [*named, *linked_credentials(named)] if p.is_file() or (p.is_dir() and any(p.iterdir()))]
    with tempfile.TemporaryDirectory(prefix="sandbox-check-", dir=scratch) as directory:
        try:
            argv = sandbox_argv(["true"], Path(directory), env)
            # Each credential's own path and every path a bind exposes it under.
            held = {p.resolve() for p in present}
            probed = [str(p) for p in present]
            probed += [str(inside) for inside, host in exposures(_binds(argv), env) if host in held]
            argv = sandbox_argv(["sh", "-c", _CREDENTIAL_PROBE, "sh", *dict.fromkeys(probed)], Path(directory), env)
        except SandboxUnavailable as error:
            return False, str(error)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"{argv[2]}: {error}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return False, f"{argv[2]} exited {proc.returncode}: {detail[-1] if detail else 'no output'}"
        return True, (
            f"{argv[2]} runs offline with the toolchain read-only and none of {len(present)} credential paths readable"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_sandbox", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--worktree", type=Path, default=Path.cwd(), help="the repair worktree (default: cwd)")
    parser.add_argument("--check", action="store_true", help="probe the credential paths inside the sandbox and report")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- lake build ...")
    args = parser.parse_args(argv)
    if args.check:
        ok, detail = check()
        print(f"{'ok' if ok else 'FAILED'}: {detail}")
        return 0 if ok else 1
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")
    logging.basicConfig(format="repair-sandbox: %(levelname)s: %(message)s")
    try:
        wrapped = sandboxed_lake(command, args.worktree)
    except SandboxUnavailable as error:
        print(f"repair-sandbox: {error}", file=sys.stderr)
        return 127
    os.chdir(args.worktree)
    os.execvp(wrapped[0], wrapped)


if __name__ == "__main__":
    sys.exit(main())
