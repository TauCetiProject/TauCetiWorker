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


# An environment shim: a POSIX shell script that only assigns or exports variables and then
# execs an absolute path with the caller's arguments. Nix's makeWrapper output and a one-line
# launcher shim have this form. Every file it can run must be hashed or immutable: its
# interpreter and target are hashed, and any other path it names must be in the Nix store.
SHIM_INTERPRETER = re.compile(r"#!\s*(/[^\s]+/(?:sh|bash|dash))(?:\s+-e)?\s*")
SHIM_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
SHIM_PREFIX = rf"{SHIM_NAME}=\S*"
SHIM_EXEC = re.compile(rf'((?:{SHIM_PREFIX}\s+)*)exec\s+(?:-a\s+"\$0"\s+)?("?)(/[^"\s$]+)\2\s+"\$@"')
SHIM_ASSIGNMENT = re.compile(rf"export\s+{SHIM_NAME}|(?:export\s+)?{SHIM_PREFIX}")
SHIM_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
SHIM_STORE_PATH = re.compile(r"/nix/store/[^:'\"\s}]*")
# Variables that make the loader or shell run code no path in the shim need name.
SHIM_REFUSED = re.compile(r"(?:export\s+)?(?:LD_PRELOAD|LD_AUDIT|BASH_ENV|ENV|DYLD_\w*)(?:=|$)")
SHIM_LIMIT = 64 * 1024
SHIM_DEPTH = 4


def shim_word(text: str) -> bool:
    """Whether an assignment, with its quoted text removed, is one word naming only store paths."""
    if SHIM_REFUSED.match(text):
        return False
    bare = SHIM_QUOTED.sub("", text)
    if "'" in bare or '"' in bare or not SHIM_ASSIGNMENT.fullmatch(bare):
        return False
    # A slash left once store paths are removed is a mutable path, unless it is the operator in
    # makeWrapper's `${PATH/':'...'':'/':'}`, followed by a quote.
    if any(".." in store for store in SHIM_STORE_PATH.findall(text)):
        return False
    rest = SHIM_STORE_PATH.sub("", text)
    return all(rest[i + 1 : i + 2] == "'" for i, c in enumerate(rest) if c == "/")


def shim_target(path: Path) -> tuple[Path, Path] | None:
    """The interpreter and absolute target of an environment shim, or None for anything else."""
    with path.open("rb") as stream:
        data = stream.read(SHIM_LIMIT + 1)
    if len(data) > SHIM_LIMIT:
        return None
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    interpreter = SHIM_INTERPRETER.fullmatch(lines[0]) if lines else None
    if interpreter is None:
        return None
    body = [line.strip() for line in lines[1:] if line.strip() and not line.strip().startswith("#")]
    if not body or any(c in "".join(body) for c in ("\\", "`", "$(", ";", "&", "|", "<", ">")):
        return None
    match = SHIM_EXEC.fullmatch(body[-1])
    if match is None:
        return None
    assignments = body[:-1] + match[1].split()
    if not all(shim_word(line) for line in assignments):
        return None
    return Path(interpreter[1]), Path(match[3])


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
        files[str(interpreter)] = file_digest(interpreter)
        files[str(path)] = file_digest(path)
    managed = path.parent.parent / "dist/current/bin/codex"
    if provider == "codex" and managed.is_file():
        binary = managed.resolve()
        files[str(binary)] = file_digest(binary)
    else:
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
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
