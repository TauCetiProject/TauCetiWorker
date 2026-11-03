"""Select token-free reads only for a locally verified client and reader policy.

The verification runner writes this private local record after capturing actual
client requests at a loopback test endpoint. This is compatibility evidence, not
source approval. Missing or stale evidence leaves new inspections on protocol 3.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
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


def selected_protocol(provider: str, state: Path) -> int:
    try:
        record = state / RECORD
        if record.is_symlink() or record.stat().st_uid != os.getuid() or record.stat().st_mode & 0o022:
            return 3
        value = json.loads(record.read_text())
        expected = fingerprint(provider)
        if value.get(provider) == {"protocol_version": 4, "fingerprint": expected, "intact": True}:
            return 4
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass
    return 3
