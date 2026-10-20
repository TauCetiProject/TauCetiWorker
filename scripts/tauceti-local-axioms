#!/usr/bin/env python3
"""Cheap changed-file diagnostics, with one native checker build per toolchain/source version.

Standalone stdlib wrapper so V0 Bubble rounds need no installed worker package. Neither cold
compilation nor warm execution loads candidate code or initializes Lake. No interpreter fallback.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def identity(path):
    info = path.stat()
    return [str(path.resolve()), info.st_size, info.st_mtime_ns]


def cache_root():
    # The login user's home is stable across host workers' isolated HOME environments.
    configured = os.environ.get("TAUCETI_LOCAL_AXIOMS_CACHE")
    if configured:
        root = Path(configured)
    else:
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            home = Path.home()
        root = home / ".cache/tauceti/local-axioms"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"local-axioms: unsafe checker cache (need an owned private directory): {root}")
    return root


def checker(driver, prefix, cache):
    # Cheap metadata identity; do not hash/walk the installed Lean distribution on every check.
    # The worker separately fingerprints its complete installation for verification receipts.
    inputs = [prefix / "bin/lean", prefix / "bin/leanc"]
    inputs += sorted((prefix / "lib/lean").glob("libleanshared*"))
    for mod in ("Environment", "Util/FoldConsts", "Util/Path"):
        inputs += sorted((prefix / "lib/lean/Lean" / mod).parent.glob(Path(mod).name + ".olean*"))
    key = hashlib.sha256(json.dumps([driver.read_text(), [identity(p) for p in inputs]]).encode()).hexdigest()
    destination = cache / key
    # Every key uses the same lock: twenty cold workers must not compile twenty versions at once.
    fd = os.open(cache / "build.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A killed wrapper may bypass TemporaryDirectory cleanup. With the build lease held,
        # every earlier scratch directory is abandoned; never require manual marker removal.
        for abandoned in cache.glob("build-*"):
            if abandoned.is_symlink():
                abandoned.unlink()
            else:
                shutil.rmtree(abandoned)
        if destination.exists() and (not destination.is_file() or not os.access(destination, os.X_OK)):
            destination.unlink()
        if not destination.exists():
            print(
                "local-axioms: compiling the small native checker once for this toolchain", file=sys.stderr, flush=True
            )
            with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as name:
                work = Path(name)
                (work / "Main.lean").write_text(driver.read_text())
                env = dict(os.environ)
                # Compile our checker using ONLY the installed sysroot, never candidate imports.
                env.pop("LEAN_SYSROOT", None)
                env["LEAN_PATH"] = ""
                subprocess.run([str(prefix / "bin/lean"), "-c", "Main.c", "Main.lean"], cwd=work, env=env, check=True)
                subprocess.run(
                    [str(prefix / "bin/leanc"), "-O1", "-o", "checker", "Main.c"], cwd=work, env=env, check=True
                )
                os.replace(work / "checker", destination)
        # Keep four recent versions; interrupted builds are removed by TemporaryDirectory.
        os.utime(destination, None)
        versions = [p for p in cache.iterdir() if len(p.name) == 64 and all(c in "0123456789abcdef" for c in p.name)]
        for old in sorted(versions, key=lambda p: p.stat().st_mtime_ns, reverse=True)[4:]:
            old.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", required=True, type=Path)
    parser.add_argument("--root", default=os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti"))
    parser.add_argument("--artifacts", type=Path, default=Path(".lake/build/lib/lean"))
    args = parser.parse_args()
    # Empty documentation/deletion-only scopes need neither a toolchain nor a native build.
    if not args.modules.read_text().strip():
        print("local-axioms: checked 0 changed module(s); full checks run in CI.")
        return 0
    prefix = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip()).resolve()
    native = checker(Path(__file__).resolve().with_name("LocalAxioms.lean"), prefix, cache_root())
    argv = [str(native), str(args.modules.resolve()), args.root, str(args.artifacts.resolve())]
    try:
        os.execv(native, argv)
    except FileNotFoundError:
        # A concurrent version eviction between lock release and exec is recoverable.
        native = checker(Path(__file__).resolve().with_name("LocalAxioms.lean"), prefix, cache_root())
        os.execv(native, argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"local-axioms: {error}", file=sys.stderr)
        raise SystemExit(75) from None
