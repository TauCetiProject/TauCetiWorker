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

``TAUCETI_QUEUE_SANDBOX=0`` turns the sandbox off for hosts without ``bwrap``; every build then logs
a warning that it ran unsandboxed. ``python -m tauceti_queue.repair_sandbox [--worktree DIR] -- lake
build ...`` runs one command under the sandbox (the helper scripts' entry point); ``--check`` runs
``true`` inside it.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DISABLE_VARIABLE = "TAUCETI_QUEUE_SANDBOX"
LOGGER = logging.getLogger("tauceti_queue.repair_sandbox")

# Read-only system roots, bound when present. A symlinked root (``/bin -> usr/bin``) is recreated as
# the same symlink, the way CI's ``--symlink usr/bin /bin`` does.
SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/nix", "/run/current-system")
# Lake's artifact-cache settings, passed through from the build's environment as CI passes them.
PASSED_ENV = ("LAKE_ARTIFACT_CACHE", "LAKE_RESTORE_ARTIFACTS", "LAKE_CACHE_DIR", "LAKE_CONFIG", "ELAN_HOME")
# CI's namespace flags, verbatim.
NAMESPACE_FLAGS = (
    "--unshare-user",
    "--unshare-ipc",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-uts",
    "--unshare-cgroup",
    "--disable-userns",
    "--die-with-parent",
    "--new-session",
)


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


def sandbox_argv(argv: list[str], worktree: Path, env: dict | None = None, *, bwrap: str | None = None) -> list[str]:
    """``argv`` wrapped in the offline sandbox, run from ``worktree``. Raises
    :class:`SandboxUnavailable` when ``bwrap`` is not on PATH."""
    env = dict(os.environ if env is None else env)
    bwrap = bwrap or shutil.which("bwrap")
    if not bwrap:
        raise SandboxUnavailable(f"bwrap is not on PATH; install bubblewrap or set {DISABLE_VARIABLE}=0")
    worktree = Path(worktree).resolve()
    lake_dir = worktree / ".lake"
    lake_dir.mkdir(exist_ok=True)
    (lake_dir / "tmp").mkdir(exist_ok=True)
    elan = _elan_home(env)
    home = env.get("HOME") or os.path.expanduser("~")
    args = [shutil.which("env") or "/usr/bin/env", "-i", bwrap, "--tmpfs", "/", *_system_binds()]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
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
    args += ["--ro-bind", str(worktree), str(worktree), "--bind", str(lake_dir), str(lake_dir)]
    packages = lake_dir / "packages"
    if packages.is_symlink():  # the shared extracted Mathlib (--seed-packages), read-only for every build
        shared = packages.resolve()
        if shared.is_dir():
            args += ["--ro-bind", str(shared), str(shared)]
    args += ["--remount-ro", "/"]
    args += ["--setenv", "PATH", _inside_path(env, elan), "--setenv", "HOME", home]
    # The sandbox is always offline: Lake restores only from the local pool, never a remote cache.
    args += ["--setenv", "LAKE_NO_CACHE", "true", "--setenv", "TMPDIR", str(lake_dir / "tmp")]
    for key in PASSED_ENV:
        if env.get(key):
            args += ["--setenv", key, env[key]]
    args += ["--chdir", str(worktree), *NAMESPACE_FLAGS, "--", *argv]
    return args


def sandboxed_lake(argv: list[str], worktree: Path, env: dict | None = None) -> list[str]:
    """The command to run for ``argv`` (a ``lake build ...``) from ``worktree``: wrapped in the
    sandbox, or ``argv`` itself with a logged warning when ``TAUCETI_QUEUE_SANDBOX=0``."""
    if disabled():
        warn_disabled(argv)
        return list(argv)
    return sandbox_argv(argv, worktree, env)


def check(scratch: Path | None = None, env: dict | None = None) -> tuple[bool, str]:
    """Run ``true`` inside the sandbox; ``(ok, detail)`` for the worker preflight."""
    if disabled():
        return True, f"DISABLED by {DISABLE_VARIABLE}=0: repair builds run unsandboxed"
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-check-", dir=scratch) as directory:
        try:
            argv = sandbox_argv(["true"], Path(directory), env)
        except SandboxUnavailable as error:
            return False, str(error)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"{argv[2]}: {error}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return False, f"{argv[2]} exited {proc.returncode}: {detail[-1] if detail else 'no output'}"
        return True, f"{argv[2]} runs `true` offline with the toolchain read-only"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_sandbox", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--worktree", type=Path, default=Path.cwd(), help="the repair worktree (default: cwd)")
    parser.add_argument("--check", action="store_true", help="run `true` inside the sandbox and report")
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
