"""The installed verification tooling, independent of disposable Lean build caches."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


class Unavailable(RuntimeError):
    def __init__(self, message, *, reason="tool-missing"):
        super().__init__(message)
        self.reason = reason


def root():
    try:
        import tauceti_tools

        directory = Path(tauceti_tools.__file__).parent
        if not (directory / "templates.py").is_file():
            raise ImportError("installed tauceti_tools.templates is absent")
        return directory
    except (ImportError, TypeError) as error:
        raise Unavailable(f"installed verification Tools unavailable: {error}") from error


def files():
    from .verification import files_under

    try:
        return list(files_under(root()))
    except Unavailable:
        return []  # Readiness changes when the installation becomes available again.


def configuration():
    value = os.environ.get("TAUCETI_CONF_SNAPSHOT")
    return Path(value) if value else None


def identity():
    """Reuse input hashing; no source, dependency or build-tree inventory is needed."""
    from .paths import HERE
    from .verification import stamps

    paths = {"python": Path(sys.executable).resolve(), "wrapper": HERE / "scripts/tauceti-verify"}
    paths.update({"verifier/" + p.name: p for p in Path(__file__).parent.glob("verification*.py")})
    try:
        directory = root()
        paths.update({"tools/" + str(p.relative_to(directory)): p for p in files()})
    except Unavailable:
        pass
    if config := configuration():
        paths["configuration"] = config
    current = stamps(paths.values())
    return _identity(tuple((name, str(path), current[str(path)]) for name, path in sorted(paths.items())))


@lru_cache(maxsize=1)
def _identity(entries):
    from .verification import digest, file_record

    # Paths/stat keys invalidate this process-local memo; only logical names and bytes
    # enter the durable identity. Identical runtime/snapshot copies are equivalent.
    return digest({name: file_record(Path(path)) for name, path, _ in entries})


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


def check(checkout, *, env=None, required=False):
    """Probe installed tooling with the inherited environment, not candidate imports."""
    from . import processes

    if not required and not needs_tools(checkout):
        return
    try:
        result = processes.run(
            [
                sys.executable,
                "-c",
                "import sys; del sys.path[0]; import tauceti_tools.templates",
            ],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable(f"verification Python tooling probe deferred: {error}", reason="verification-busy") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise Unavailable(
            f"verification Python tooling unavailable with {sys.executable}: {detail}",
            reason="verification-busy" if result.returncode < 0 else "tool-missing",
        )
