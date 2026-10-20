"""Content-free launch protocol, copied with every API shim.

The native exec gate cannot infer until a host supervisor durably records its
identity and publishes a permit. Linux identities include boot, start ticks and
PID namespace, so a copied shim's namespace-local PID is never a host PID guess.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from functools import lru_cache
from pathlib import Path


def durable(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def darwin_boot():
    import subprocess

    return subprocess.check_output(["sysctl", "-n", "kern.bootsessionuuid"], text=True).strip()


def darwin_process(pid):
    import ctypes

    class BSDInfo(ctypes.Structure):
        _fields_ = (
            [
                (name, ctypes.c_uint32)
                for name in (
                    "flags",
                    "status",
                    "xstatus",
                    "pid",
                    "ppid",
                    "uid",
                    "gid",
                    "ruid",
                    "rgid",
                    "svuid",
                    "svgid",
                    "reserved",
                )
            ]
            + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
            + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid", "nice")]
            + [("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]
        )

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    info = BSDInfo()
    if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        import errno

        error = ctypes.get_errno()
        if error == errno.ESRCH:
            raise ProcessLookupError(pid)
        raise OSError(error or errno.EIO, "process birth identity unavailable")
    return dict(pid=pid, boot=darwin_boot(), start=f"{info.start_seconds}:{info.start_microseconds}"), info.status


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
        return darwin_process(pid)[0]
    if sys.platform != "linux":
        # Unsupported birth identities cannot authorize automatic stopped-process
        # recovery. Normal native receipts still work on these hosts.
        return dict(pid=pid, unsupported=True, token=uuid.uuid4().hex)
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return dict(
        pid=pid,
        boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        start=fields[19],
        namespace=os.readlink(f"/proc/{pid}/ns/pid"),
    )


def resolve(owner):
    """Translate a namespaced identity once, while its exec gate is still alive."""
    if not isinstance(owner, dict) or not owner:
        raise ValueError("missing process identity")
    if sys.platform == "darwin":
        try:
            return owner if identity(int(owner["pid"])) == owner else None
        except (OSError, ValueError, KeyError):
            return None
    if owner.get("unsupported") and sys.platform != "linux":
        return owner
    namespace = owner.get("namespace")
    if (
        namespace == os.readlink("/proc/self/ns/pid")
        and owner.get("boot") != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ):
        return None
    if namespace == os.readlink("/proc/self/ns/pid"):
        candidates = [Path("/proc") / str(int(owner["pid"]))]
    else:
        candidates = Path("/proc").glob("[0-9]*")
    for path in candidates:
        try:
            current = identity(int(path.name))
            if current["start"] != owner["start"] or current["namespace"] != namespace:
                continue
            # Start ticks alone are not unique for siblings. Match the PID in
            # the sender's namespace too (last NSpid in the host's status).
            pids = next(line for line in (path / "status").read_text().splitlines() if line.startswith("NSpid:"))
            if int(pids.split()[-1]) != int(owner["pid"]) and namespace != os.readlink("/proc/self/ns/pid"):
                continue
            return current
        except (OSError, ValueError, KeyError, StopIteration):
            continue
    return None


def alive(owner):
    if not owner or owner.get("unsupported") or owner.get("remote"):
        return True  # Unknown is never proof of termination.
    try:
        if sys.platform == "darwin":
            current, status = darwin_process(int(owner["pid"]))
            return current == owner and status != 5
        current = identity(int(owner["pid"]))
        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return current == owner and fields[0] not in {"Z", "X"}
    except (ProcessLookupError, FileNotFoundError):
        return False
    except (OSError, ValueError, KeyError, IndexError):
        return True  # Unreadable birth evidence is not proof of termination.


def exec_gate(bridge, invocation, command):
    """Runs inside the paid process boundary; exec keeps the registered identity."""
    bridge = Path(bridge)
    birth = identity()
    durable(bridge / "inbox" / (invocation + ".launch"), dict(id=invocation, native=birth))
    permit = bridge / "outbox" / (invocation + ".permit")

    def no_exec(reason):
        durable(bridge / "inbox" / (invocation + ".no-exec"), dict(id=invocation, native=birth, reason=reason))
        return 75

    deadline = time.monotonic() + 30
    while not permit.exists():
        if time.monotonic() >= deadline:
            return no_exec("permit-timeout")
        time.sleep(0.05)
    try:
        permission = json.loads(permit.read_text())
        if permission.get("id") != invocation or permission.get("gate") != birth:
            return no_exec("invalid-permit")
    except (OSError, ValueError):
        return no_exec("unreadable-permit")
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError:
        durable(bridge / "inbox" / (invocation + ".exec-failed"), dict(id=invocation, native=birth))
        return no_exec("exec-failed")


if __name__ == "__main__":
    sys.exit(exec_gate(sys.argv[1], sys.argv[2], sys.argv[3:]))
