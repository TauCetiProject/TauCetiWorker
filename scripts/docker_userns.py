#!/usr/bin/env python3
"""Run a command as root of a new user namespace that maps every id to itself.

The Docker image runs the worker as root, and bubblewrap run by root creates no user namespace of
its own: its mount namespace would need CAP_SYS_ADMIN over the host. Inside this namespace
the worker holds every capability over the namespace and none over the host, so bubblewrap can
build shared Mathlib views while the container keeps Docker's default capability set. Ids map to
themselves, so file ownership and permissions are exactly what they were.

Creating the namespace needs the seccomp profile of compose.shared-mathlib.yaml. Where the
namespace cannot be created the command still runs, without it, after a warning: rounds that
need Mathlib then stop with the reason, and `tauceti doctor` reports it.
"""

import ctypes
import os
import sys

CLONE_NEWUSER = 0x10000000
MARKER = "TAUCETI_IN_USER_NAMESPACE"
# Every id but the last, which the kernel reserves as "no id".
IDENTITY = "0 0 4294967295\n"


def enter():
    """Move this process into the new namespace; return why not, or None."""
    target = os.getpid()
    unshared_read, unshared_write = os.pipe()
    mapped_read, mapped_write = os.pipe()
    # A namespace's root may map only its own id. Mapping all of them takes CAP_SETUID and
    # CAP_SETGID in the parent namespace, which a helper that stays behind still holds.
    helper = os.fork()
    if helper == 0:
        os.close(unshared_write)
        os.close(mapped_read)
        code = 1
        try:
            if os.read(unshared_read, 1) == b"x":
                for name in ("uid_map", "gid_map"):
                    with open(f"/proc/{target}/{name}", "w") as handle:
                        handle.write(IDENTITY)
                os.write(mapped_write, b"x")
                code = 0
        except OSError as error:
            os.write(mapped_write, f"cannot write the id maps: {error}".encode())
        finally:
            os._exit(code)
    os.close(unshared_read)
    os.close(mapped_write)
    reason = None
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(CLONE_NEWUSER) == 0:
        os.write(unshared_write, b"x")
    else:
        reason = f"unshare(CLONE_NEWUSER): {os.strerror(ctypes.get_errno())}"
    os.close(unshared_write)
    answer = os.read(mapped_read, 400)
    os.close(mapped_read)
    os.waitpid(helper, 0)
    if reason is None and answer != b"x":
        # The namespace exists but maps no ids, and it cannot be left: nothing can run in it.
        sys.exit("tauceti-userns: " + (answer.decode(errors="replace") or "the id maps were not written"))
    return reason


def main():
    command = sys.argv[1:]
    if not command:
        sys.exit("usage: tauceti-userns COMMAND [ARGUMENT...]")
    if os.geteuid() != 0:
        # An unprivileged user's bubblewrap creates its own user namespace.
        os.environ[MARKER] = "0"
        os.execvp(command[0], command)
    reason = enter()
    if reason is None:
        os.environ[MARKER] = "1"
    else:
        os.environ[MARKER] = "0"
        sys.stderr.write(
            f"tauceti-userns: WARNING: no user namespace ({reason}); rounds that need Mathlib will not run."
            " Start the container with compose.shared-mathlib.yaml (see docs/docker.md)\n"
        )
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
