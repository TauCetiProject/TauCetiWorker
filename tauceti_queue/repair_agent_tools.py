"""The repair agent's Lean toolchain commands, routed through the worker's build sandbox.

The agent's own shell is not sandboxed (the agent is trusted), but the batch it repairs is untrusted:
any Lean the batch contains can run code at elaboration time, and Lake elaborates package
configurations too. So every toolchain command the agent runs goes through the offline ``bwrap``
sandbox of the worker's own builds (``repair_sandbox``): read-only root, no network, only the
worktree's ``.lake`` and the artifact pool writable, no credential readable.

:func:`write_tools` writes, into one directory placed first on the agent's ``PATH``:

- one shim per toolchain command (``elan``, ``lake``, ``lean``, ``leanc``, ``leanchecker``,
  ``leanmake``, ``leanpkg`` and every ``lean*`` or ``lake*`` executable of an installed toolchain),
  each running the command under the sandbox from the caller's directory. That covers ``lake build``,
  ``lake env``, ``lake exe``, ``lake lean``, ``lean --run``, ``elan run`` and any program that
  looks the commands up on ``PATH``. The worktree is the nearest directory above the caller's
  holding ``lakefile.toml``, ``lakefile.lean`` or ``lean-toolchain`` (a caller inside ``.lake``
  counts as its worktree); outside one the shim refuses. Inside the sandbox only the worktree, the
  toolchains and the artifact pool are visible, so a path elsewhere fails to resolve;
- ``shell-guard.sh``, sourced by every bash the agent starts (``BASH_ENV``, and Claude Code's
  ``CLAUDE_ENV_FILE`` through ``repair.prepare_claude_shell_env``). Before each command it puts the
  shim directory back first on ``PATH`` (a login profile may reorder it) and refuses a command
  that names a toolchain executable by path (``~/.elan/toolchains/.../bin/lean``, the elan proxy's
  resolved file), which would bypass the shims.

What this cannot cover while the agent's shell itself runs unsandboxed: a program other than bash
(Python, Node, a compiled tool) that executes a toolchain binary by absolute path, a command line the
guard cannot read literally (a path assembled at run time, ``eval``) and deliberate evasion. Those
need the agent itself in a sandbox, with builds requested from outside it.

``TAUCETI_QUEUE_SANDBOX=0`` makes the shims run the real commands unsandboxed, as the worker's own
builds do, with the same logged warning.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

TOOLS_DIR = "agent-tools"
SHIM = "sandboxed-tool"
GUARD = "shell-guard.sh"
# Always shimmed, whether or not a toolchain is installed yet.
COMMANDS = ("elan", "lake", "lean", "leanc", "leanchecker", "leanmake", "leanpkg")
PROJECT_MARKERS = ("lakefile.toml", "lakefile.lean", "lean-toolchain")
TOOLS_VARIABLE = "TAUCETI_AGENT_TOOLS"


def elan_home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("ELAN_HOME"):
        return Path(env["ELAN_HOME"])
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".elan"


def commands(env: dict | None = None) -> list[str]:
    """``COMMANDS`` plus every ``lean*``/``lake*`` executable in an installed toolchain's ``bin``."""
    names = set(COMMANDS)
    toolchains = elan_home(env) / "toolchains"
    for binary in toolchains.glob("*/bin/*") if toolchains.is_dir() else ():
        if binary.name.startswith(("lean", "lake")) and os.access(binary, os.X_OK) and binary.is_file():
            names.add(binary.name)
    return sorted(names)


def without(path: str, directory: Path) -> str:
    """``path`` with every entry naming ``directory`` (any alias) removed."""
    kept = []
    for entry in path.split(os.pathsep):
        try:
            same = bool(entry) and Path(entry).resolve() == directory.resolve()
        except OSError:
            same = False
        if not same:
            kept.append(entry)
    return os.pathsep.join(kept)


def guarded_paths(tools: Path, env: dict | None = None) -> list[str]:
    """Real paths the guard refuses to execute directly: the elan home, each toolchain's real
    directory and the resolved file of every shimmed command found on ``PATH`` without the shims."""
    env = os.environ if env is None else env
    elan = elan_home(env)
    found = [elan.resolve()]
    toolchains = elan / "toolchains"
    found += [t.resolve() for t in toolchains.iterdir()] if toolchains.is_dir() else []
    search = without(env.get("PATH", ""), tools)
    for name in commands(env):
        if where := shutil.which(name, path=search):
            found.append(Path(where).resolve())
    unique: list[str] = []
    for path in found:
        if str(path) not in unique and str(path) != "/":
            unique.append(str(path))
    return unique


GUARD_BODY = r"""
__tauceti_guard() {
  # extdebug makes a non-zero return skip the command. Set in a startup file, it would also start
  # bash's debugger, so it is set on the first command instead.
  shopt -q extdebug || shopt -s extdebug
  case ":$PATH:" in
    ":$__TAUCETI_TOOLS:"*) ;;
    *) PATH="$__TAUCETI_TOOLS:$PATH" ;;
  esac
  local c=$BASH_COMMAND
  case $c in */*) ;; *) return 0 ;; esac
  case $c in *lean*|*lake*|*elan*) ;; *) return 0 ;; esac
  local -a w
  local x r p
  read -r -a w <<<"$c" || true
  for x in "${w[@]}"; do
    x=${x//\"/}
    x=${x//\'/}
    case ${x##*/} in lean*|lake*|elan*) ;; *) continue ;; esac
    case $x in "~/"*) x=$HOME/${x#\~/} ;; esac
    x=${x//\$\{HOME\}/$HOME}
    x=${x//\$HOME/$HOME}
    x=${x//\$\{ELAN_HOME\}/$__TAUCETI_ELAN}
    x=${x//\$ELAN_HOME/$__TAUCETI_ELAN}
    [[ -f $x && -x $x ]] || continue
    r=$(readlink -f -- "$x" 2>/dev/null) || continue
    for p in "${__TAUCETI_GUARDED[@]}"; do
      if [[ $r == "$p" || $r == "$p"/* ]]; then
        printf 'tauceti repair: refused `%s`: a toolchain executable named by path bypasses the build sandbox. Run `%s` by name instead; it runs offline in the sandbox, from this directory.\n' "$x" "${x##*/}" >&2
        return 1
      fi
    done
  done
  return 0
}
trap __tauceti_guard DEBUG
"""


def guard_script(tools: Path, env: dict | None = None) -> str:
    guarded = " ".join(shlex.quote(p) for p in guarded_paths(tools, env))
    return (
        "# Written by the TauCeti repair worker (tauceti_queue/repair_agent_tools.py); sourced by every bash\n"
        "# the repair agent starts. Keeps the sandboxed toolchain shims first on PATH and refuses a toolchain\n"
        "# executable named by path, which would run the batch's Lean outside the build sandbox.\n"
        f"__TAUCETI_TOOLS={shlex.quote(str(tools))}\n"
        f"__TAUCETI_ELAN={shlex.quote(str(elan_home(env)))}\n"
        f"__TAUCETI_GUARDED=({guarded})\n" + GUARD_BODY
    )


def shim_script(python: str | None = None) -> str:
    package = Path(__file__).resolve().parent.parent
    return (
        f"#!{python or sys.executable} -I\n"
        '"""A toolchain command of the repair agent, run in the build sandbox. Written by the queue\'s repair\n'
        'worker (tauceti_queue/repair_agent_tools.py)."""\n'
        "import sys\n\n"
        f"sys.path.insert(0, {str(package)!r})\n"
        "from tauceti_queue.repair_agent_tools import shim_main\n\n"
        "sys.exit(shim_main(sys.argv))\n"
    )


def _replace(path: Path, text: str, mode: int) -> None:
    """Write ``path`` atomically: parallel agents of one worker share the directory."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_tools(root: Path, env: dict | None = None) -> Path:
    """Write the shims and the shell guard into ``root/agent-tools``; returns the directory."""
    tools = (root / TOOLS_DIR).resolve()
    tools.mkdir(parents=True, exist_ok=True)
    _replace(tools / SHIM, shim_script(), 0o755)
    for name in commands(env):
        link = tools / name
        if not (link.is_symlink() and os.readlink(link) == SHIM):
            temporary = tools / f".{name}-{os.getpid()}"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(SHIM)
            os.replace(temporary, link)
    _replace(tools / GUARD, guard_script(tools, env), 0o644)
    return tools


def agent_environment(root: Path, env: dict | None = None) -> dict:
    """Environment changes for the repair agent: ``TAUCETI_AGENT_TOOLS`` (placed first on its
    ``PATH`` by ``agents.host_agent_argv``) and ``BASH_ENV``, the shell guard."""
    tools = write_tools(root, env)
    return {TOOLS_VARIABLE: str(tools), "BASH_ENV": str(tools / GUARD)}


def project_root(directory: Path) -> Path | None:
    """The worktree a command run from ``directory`` belongs to: the nearest directory holding a
    ``PROJECT_MARKERS`` file, searched from above any ``.lake`` component."""
    directory = directory.resolve()
    parts = directory.parts
    if ".lake" in parts:
        directory = Path(*parts[: parts.index(".lake")])
    for candidate in (directory, *directory.parents):
        if any((candidate / marker).is_file() for marker in PROJECT_MARKERS):
            return candidate
    return None


def shim_main(argv: list[str]) -> int:
    from tauceti_queue import repair_sandbox

    name = Path(argv[0]).name
    tools = Path(argv[0]).resolve().parent
    env = dict(os.environ)
    env["PATH"] = without(env.get("PATH", ""), tools)  # never back into a shim
    env.pop("BASH_ENV", None)
    try:
        cwd = Path.cwd()
    except OSError as error:
        print(f"{name}: no current directory ({error})", file=sys.stderr)
        return 126
    root = project_root(cwd)
    if root is None:
        print(
            f"{name}: runs only inside the repair worktree, in the build sandbox; "
            f"{cwd} is in no Lake project. Change to the worktree and run it again.",
            file=sys.stderr,
        )
        return 126
    import logging

    logging.basicConfig(format=f"{name}: %(levelname)s: %(message)s")
    try:
        wrapped = repair_sandbox.sandboxed_lake([name, *argv[1:]], root, env, cwd=cwd)
    except repair_sandbox.SandboxUnavailable as error:
        print(f"{name}: the build sandbox is unavailable: {error}", file=sys.stderr)
        return 127
    os.execvpe(wrapped[0], wrapped, env)
