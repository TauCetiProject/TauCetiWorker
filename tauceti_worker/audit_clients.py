"""Select token-free reads only for a locally verified client and reader policy.

The verification runner writes this private local record after capturing actual
client requests at a loopback test endpoint. This is compatibility evidence, not
source approval. Missing or stale evidence leaves new inspections on protocol 3.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

RECORD = "client-coverage.json"


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


def fingerprint(provider: str) -> dict:
    from . import security_audit as sa

    path = client_path(provider)
    # Hash native clients directly, or the managed Codex launcher AND its native
    # binary. Other wrappers may hide code changes behind an unchanged version;
    # keep those on the token protocol rather than claim verified identity.
    files = {str(path): file_digest(path)}
    managed = path.parent.parent / "dist/current/bin/codex"
    if provider == "codex" and managed.is_file():
        binary = managed.resolve()
        files[str(binary)] = file_digest(binary)
    else:
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
            raise ValueError("protocol 4 requires a native client or the managed Codex launcher")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE")}
    version = subprocess.run([str(path), "--version"], env=env, capture_output=True, text=True, timeout=20, check=True)
    return {
        "files": files,
        "version": version.stdout.strip(),
        "provider": provider,
        "reader_digest": sa.inspection_profile(4)["reader_digest"],
        "catalog_digest": sa.inspection_profile(4)["catalog_digest"],
        "verification_digest": file_digest(Path(__file__)),
        "check_digest": file_digest(Path(__file__).with_name("audit_client_check.py")),
        "launcher_digest": file_digest(Path(sa.__file__)),
    }


def selected_protocol(provider: str, state: Path, *, verify: bool = True) -> int:
    """Refresh compatibility locally when needed, with one cached failure per client."""
    from .audit_alerts import save
    from .config import Die, log

    def verified(expected):
        record = state / RECORD
        try:
            if record.is_symlink() or record.stat().st_uid != os.getuid() or record.stat().st_mode & 0o022:
                return False
            value = json.loads(record.read_text())
            return value.get(provider) == {"protocol_version": 4, "fingerprint": expected, "intact": True}
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    try:
        expected = fingerprint(provider)
        if verified(expected):
            return 4
        if not verify:
            return 3
        state.mkdir(parents=True, exist_ok=True)
        # Separate from verify()'s short record-update lock: one probe at a time,
        # without holding the lock the probe itself needs to save its result.
        with (state / "client-coverage-check.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = fingerprint(provider)
            if verified(expected):
                return 4
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
                return 3
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
                    f"security-audit: {provider} delivery check failed; protocol 4 unavailable, retrying after 5 minutes: {exc}"
                )
                return 3
            failures.pop(provider, None)
            save(failures_path, failures)
            return 4
    except (Die, OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        log(f"security-audit: {provider} delivery verification unavailable: {exc}")
    return 3
