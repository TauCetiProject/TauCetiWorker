"""Select token-free reads only for a locally verified client and reader policy.

The verification runner writes this private local record after capturing actual
client requests at a loopback test endpoint. This is compatibility evidence, not
source approval. New inspections use judgment protocol 5 only: missing or stale
evidence makes them wait for verification, never fall back to an older protocol.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

RECORD = "client-coverage.json"
PROTOCOL = 5  # The judgment protocol whose client delivery this record verifies.


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def client_path(provider: str) -> Path:
    if provider not in ("codex", "claude"):
        raise ValueError("unsupported audit client")
    path = shutil.which(provider)
    if not path:
        raise ValueError("audit client is unavailable")
    return Path(path).resolve()


# An environment shim: a shell script that only sets variables, in exactly the forms Nix's
# makeWrapper emits, then execs an absolute path with the caller's arguments; or a one-line
# launcher shim of that exec. Every file it can run must be hashed or immutable: its
# interpreter and target are hashed, and any other path it names is in the Nix store. This is
# an allowlist of whole lines, never a shell parser.
_NAME = r"(?!(?:LD_PRELOAD|LD_AUDIT|BASH_ENV|ENV|DYLD_\w*)\b)[A-Za-z_][A-Za-z0-9_]*"
# A literal is one plain word, set only in a client's own namespace: no literal can name a
# file for an interpreter or loader to read.
_LITERAL = r"[A-Za-z0-9_]*"
_SETTING = r"(?:CLAUDE|CODEX|DISABLE|FORCE|USE)_(?!\w*PATH\b)[A-Za-z0-9_]*"
# Absolute paths of plain components: never `.` or `..`, so a store path stays in the store.
_COMPONENT = r"/(?!\.\.?(?![A-Za-z0-9._+-]))[A-Za-z0-9._+-]+"
_PATH = rf"(?:{_COMPONENT})+"
_STORE = rf"/nix/store(?:{_COMPONENT})+"
SHIM_INTERPRETER = re.compile(rf"#!\s*({_PATH})(?:\s+-e)?\s*")
SHIM_LINES = [
    re.compile(p)
    for p in (
        rf"export {_NAME}",
        rf"export {_SETTING}='{_LITERAL}'",
        rf"export (?P<n>{_SETTING})=\$\{{(?P=n):?-'{_LITERAL}'\}}",
        rf"(?P<n>{_NAME})=\$\{{(?P=n):\+':'\$(?P=n)':'\}}",
        rf"(?P<n>{_NAME})=\$\{{(?P=n)/':''{_STORE}'':'/':'\}}",
        rf"(?P<n>{_NAME})='{_STORE}'\$(?P=n)",
        rf"(?P<n>{_NAME})=\$(?P=n)'{_STORE}'",
        rf"(?P<n>{_NAME})=\$\{{(?P=n)[#%]':'\}}",
    )
]
SHIM_EXEC = re.compile(rf'((?:{_SETTING}={_LITERAL} )*)exec (?:-a "\$0" )?("?)({_PATH})\2\s+"\$@"')
SHIM_LIMIT = 64 * 1024
SHIM_DEPTH = 4


def shim_target(path: Path) -> tuple[Path, Path] | None:
    """The interpreter and absolute target of an environment shim, or None for anything else."""
    with path.open("rb") as stream:
        data = stream.read(SHIM_LIMIT + 1)
    if len(data) > SHIM_LIMIT:
        return None
    # Lines end only at LF, and nothing the shell would read differently from this check
    # (CR, form feed, other control characters) is accepted.
    if any(not (32 <= byte < 127 or byte == 10) for byte in data):
        return None
    lines = data.decode("ascii").split("\n")
    interpreter = SHIM_INTERPRETER.fullmatch(lines[0]) if lines else None
    if interpreter is None or Path(interpreter[1]).name not in ("sh", "bash", "dash"):
        return None
    body = [line.strip(" ") for line in lines[1:] if line.strip(" ") and not line.strip(" ").startswith("#")]
    match = SHIM_EXEC.fullmatch(body[-1]) if body else None
    if match is None or not all(any(p.fullmatch(line) for p in SHIM_LINES) for line in body[:-1]):
        return None
    return Path(interpreter[1]), Path(match[3])


def native(path: Path) -> bool:
    with path.open("rb") as stream:
        magic = stream.read(4)
    return magic in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")


def fingerprint(provider: str) -> dict:
    from . import security_audit as sa

    path = client = client_path(provider)
    # Hash native clients directly, or the managed Codex launcher AND its native
    # binary, after every environment shim leading to them. Other wrappers may hide
    # code changes behind an unchanged version; keep those unverified rather than
    # claim verified identity.
    files = {str(path): file_digest(path)}
    for _ in range(SHIM_DEPTH):
        shim = shim_target(path)
        if shim is None:
            break
        interpreter, path = (p.resolve() for p in shim)
        if not interpreter.is_file() or not path.is_file():
            raise ValueError("audit client shim names a missing file")
        if not native(interpreter):
            # A script interpreter could itself read files this fingerprint never hashes.
            raise ValueError("delivery verification requires a native client or the managed Codex launcher")
        files[str(interpreter)] = file_digest(interpreter)
        files[str(path)] = file_digest(path)
    managed = path.parent.parent / "dist/current/bin/codex"
    if provider == "codex" and managed.is_file():
        binary = managed.resolve()
        files[str(binary)] = file_digest(binary)
    elif not native(path):
        raise ValueError("delivery verification requires a native client or the managed Codex launcher")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE")}
    version = subprocess.run(
        [str(client), "--version"], env=env, capture_output=True, text=True, timeout=20, check=True
    )
    return {
        "files": files,
        "version": version.stdout.strip(),
        "provider": provider,
        "reader_digest": sa.inspection_profile()["reader_digest"],
        "catalog_digest": sa.inspection_profile()["catalog_digest"],
        "verification_digest": file_digest(Path(__file__)),
        "check_digest": file_digest(Path(__file__).with_name("audit_client_check.py")),
        "launcher_digest": file_digest(Path(sa.__file__)),
    }


def selected_protocol(provider: str, state: Path, *, verify: bool = True) -> int | None:
    """Protocol 5 once this client's delivery is verified, else None: the caller waits.

    Compatibility is refreshed locally when needed, with one cached failure per client."""
    from .audit_alerts import save
    from .config import Die, log

    def verified(expected):
        record = state / RECORD
        try:
            if record.is_symlink() or record.stat().st_uid != os.getuid() or record.stat().st_mode & 0o022:
                return False
            value = json.loads(record.read_text())
            return value.get(provider) == {"protocol_version": PROTOCOL, "fingerprint": expected, "intact": True}
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    try:
        expected = fingerprint(provider)
        if verified(expected):
            return PROTOCOL
        if not verify:
            return None
        state.mkdir(parents=True, exist_ok=True)
        # Separate from verify()'s short record-update lock: one probe at a time,
        # without holding the lock the probe itself needs to save its result.
        with (state / "client-coverage-check.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = fingerprint(provider)
            if verified(expected):
                return PROTOCOL
            failures_path = state / "client-coverage-failures.json"
            try:
                failures = json.loads(failures_path.read_text())
                if not isinstance(failures, dict):
                    failures = {}
            except (OSError, ValueError):
                failures = {}
            failed = failures.get(provider, {})
            if (
                isinstance(failed, dict)
                and failed.get("fingerprint") == expected
                and type(failed.get("retry_at")) in (int, float)
                and failed["retry_at"] > time.time()
            ):
                return None
            log(f"security-audit: checking {provider} tool delivery at a local endpoint")
            try:
                from .audit_client_check import verify as check

                check(provider, state)
                if not verified(expected):
                    raise ValueError("client changed or did not produce a matching record")
            except Exception as exc:
                # This is an isolated, bounded compatibility probe. Unexpected
                # client formats also need a cooldown, never a hot retry loop.
                failures[provider] = {"fingerprint": expected, "retry_at": time.time() + 300, "error": str(exc)}
                save(failures_path, failures)
                log(
                    f"security-audit: {provider} delivery check failed; new inspections wait, retrying after 5 minutes: {exc}"
                )
                return None
            failures.pop(provider, None)
            save(failures_path, failures)
            return PROTOCOL
    except (Die, OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        log(f"security-audit: {provider} delivery verification unavailable: {exc}")
    return None
