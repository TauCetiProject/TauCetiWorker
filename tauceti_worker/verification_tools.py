"""The installed verification tooling, independent of disposable Lean build caches."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


class Unavailable(RuntimeError):
    pass


def root():
    import tauceti_tools

    return Path(tauceti_tools.__file__).parent


def files():
    from .verification import files_under

    return list(files_under(root()))


def configuration():
    value = os.environ.get("TAUCETI_CONF_SNAPSHOT")
    return Path(value) if value else None


def identity():
    """Reuse input hashing; no source, dependency or build-tree inventory is needed."""
    from .paths import HERE
    from .verification import stamps

    paths = [Path(sys.executable), *Path(__file__).parent.glob("verification*.py"), *files()]
    paths += [Path(__file__).with_name("repair_recovery.py"), HERE / "scripts/tauceti-verify"]
    if config := configuration():
        paths.append(config)
    return _identity(tuple(sorted(stamps(paths).items())))


@lru_cache(maxsize=1)
def _identity(entries):
    from .verification import digest, file_record

    return digest({path: file_record(Path(path)) for path, _ in entries})


def needs_tools(checkout):
    script = Path(checkout) / "scripts/check-expired-mathlib-shims.py"
    try:
        tree = ast.parse(script.read_text())
    except (FileNotFoundError, SyntaxError, UnicodeError):
        return False  # Missing/invalid project scripts retain normal verifier diagnostics.
    return any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] == "tauceti_tools"
        or isinstance(node, ast.Import)
        and any(n.name.split(".")[0] == "tauceti_tools" for n in node.names)
        for node in ast.walk(tree)
    )


def check(checkout, *, env=None):
    """Probe the wrapper's real child environment, including Bubble's mounted Tools."""
    from . import processes

    if not needs_tools(checkout):
        return
    try:
        result = processes.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path[0] = sys.argv[1]; import tauceti_tools.templates",
                str((Path(checkout) / "scripts").absolute()),
            ],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable(f"verification Python tooling unavailable: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise Unavailable(f"verification Python tooling unavailable with {sys.executable}: {detail}")
