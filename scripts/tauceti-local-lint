#!/usr/bin/env python3
"""Scoped local environment/docstring checks; never run a checkout's full-library driver."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().with_name("LocalLint.lean")


def copy_file(source, destination):
    with open(source, "rb") as src, open(destination, "wb") as dst:
        try:
            fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
        except OSError:
            shutil.copyfileobj(src, dst)
    shutil.copystat(source, destination)
    return str(destination)


MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def checker(project, source):
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        home = Path.home()
    root = Path(os.environ.get("TAUCETI_LOCAL_LINT_CACHE") or home / ".cache/tauceti/local-lint")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"local-lint: checker cache must be an owned private directory: {root}")
    batteries = (project / ".lake/packages/batteries").resolve()
    prefix = Path(subprocess.check_output(["lean", "--print-prefix"], cwd=project, text=True).strip()).resolve()
    identities = []
    for p in [
        prefix / "bin/lean",
        prefix / "bin/leanc",
        *sorted((prefix / "lib/lean").glob("libleanshared*")),
    ]:
        s = p.stat()
        identities.append((str(p), s.st_size, s.st_mtime_ns, s.st_ctime_ns))
    # Key shared binaries by dependency content, not worker-specific checkout paths or
    # artifact timestamps. Otherwise twenty identical workers continually evict one another.
    lint_sources = [batteries / "Batteries/Tactic/Lint.lean"]
    lint_sources += sorted((batteries / "Batteries/Tactic/Lint").glob("*.lean"))
    content = {str(p.relative_to(batteries)): p.read_text() for p in lint_sources if p.is_file()}
    try:
        packages = json.loads((project / "lake-manifest.json").read_text()).get("packages", [])
        revision = next((p.get("rev") or p.get("version") for p in packages if p.get("name") == "batteries"), None)
    except (ValueError, AttributeError):
        revision = None
    key = hashlib.sha256(json.dumps([source.read_text(), revision, identities, content]).encode()).hexdigest()
    target = root / key
    # One brief build lease; no worker round owns it after the checker is published.
    with (root / "build.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for p in root.glob("build-*"):
            if p.is_symlink():
                p.unlink()
            else:
                shutil.rmtree(p)
        if not target.is_file():
            print("local-lint: compiling the small checker once for these dependencies", flush=True)
            with tempfile.TemporaryDirectory(prefix="build-", dir=root) as directory:
                work = Path(directory)
                shutil.copyfile(source, work / "LocalLint.lean")
                shutil.copyfile(project / "lean-toolchain", work / "lean-toolchain")
                private_batteries = work / "batteries"
                shutil.copytree(
                    batteries,
                    private_batteries,
                    copy_function=copy_file,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"),
                )
                # Published dependency installations may be read-only. Only this private
                # reflink/copy becomes writable; compiler outputs never touch the source.
                for directory, _, files in os.walk(private_batteries):
                    path = Path(directory)
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
                    for name in files:
                        path = Path(directory) / name
                        path.chmod(path.stat().st_mode | stat.S_IWUSR)
                (work / "lakefile.toml").write_text(
                    'name = "local-lint"\n[[require]]\nname = "batteries"\npath = '
                    + json.dumps(str(private_batteries))
                    + '\n[[lean_exe]]\nname = "local-lint"\nroot = "LocalLint"\nsupportInterpreter = true\n'
                )
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k not in {"ELAN_TOOLCHAIN", "LAKE_CONFIG", "LEAN_PATH", "LEAN_SRC_PATH", "LAKE_PKG_URL_MAP"}
                }
                env.update(
                    LAKE_CACHE_DIR="", LAKE_ARTIFACT_CACHE="false", LAKE_RESTORE_ARTIFACTS="false", LAKE_NO_CACHE="true"
                )
                subprocess.run(["lake", "build", "local-lint"], cwd=work, env=env, check=True, timeout=180)
                os.replace(work / ".lake/build/bin/local-lint", target)
        os.utime(target, None)
        versions = [p for p in root.iterdir() if re.fullmatch(r"[0-9a-f]{64}", p.name)]
        for p in sorted(versions, key=lambda p: p.stat().st_mtime, reverse=True)[8:]:
            if __import__("time").time() - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
    return target


def pairs(path):
    if not path.exists():
        return set()
    lines = [line for line in path.read_text().splitlines() if line]
    if lines != sorted(set(lines)):
        raise ValueError(f"local-lint: unsorted or duplicate entries in {path}")
    return set(lines)


def diagnose(result, baseline, allowlist):
    failures = []
    for finding in result["findings"]:
        key = finding["linter"] + " " + finding["declaration"]
        if key not in baseline:
            failures.append(f"{key}: {finding['detail']}")
    for linter, name in result["nolints"]:
        key = linter + " " + name
        if key not in allowlist:
            failures.append(f"unaccounted nolint: {key}")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", required=True, type=Path)
    args = parser.parse_args(argv)
    project = Path.cwd()
    modules = sorted({line.strip() for line in args.modules.read_text().splitlines() if line.strip()})
    for module in modules:
        if not MODULE.fullmatch(module) or not (project / (module.replace(".", "/") + ".lean")).is_file():
            raise ValueError(f"local-lint: invalid requested module: {module!r}")
    if not modules:
        print("local-lint: checked 0 changed modules; full checks run in CI")
        return 0
    native = checker(project, SOURCE)
    with tempfile.TemporaryDirectory(prefix="local-lint-") as directory:
        directory = Path(directory)
        selected = directory / "modules.txt"
        selected.write_text("\n".join(modules) + "\n")
        marker = "LOCAL-LINT-" + uuid.uuid4().hex
        nonce = directory / "marker.txt"
        nonce.write_text(marker)
        catalogs = ["Batteries.Tactic.Lint"]
        for name in ("Lint", "Style", "TacticDocumentation"):
            if (project / f".lake/packages/mathlib/Mathlib/Tactic/Linter/{name}.lean").is_file():
                catalogs.append("Mathlib.Tactic.Linter." + name)
        catalog = directory / "catalog.txt"
        catalog.write_text("\n".join(catalogs) + "\n")
        result = subprocess.run(
            ["lake", "env", str(native), str(selected), str(nonce), str(catalog)], capture_output=True, text=True
        )
        print(result.stderr, end="")
        lines = result.stdout.splitlines()
        for line in lines:
            if not line.startswith(marker + " "):
                print(line)
        reports = [json.loads(line[len(marker) + 1 :]) for line in lines if line.startswith(marker + " ")]
        if result.returncode or len(reports) != 1:
            raise ValueError(f"local-lint: checker failed (exit {result.returncode}, {len(reports)} reports)")
        report = reports[0]
        if report["modules"] != len(modules):
            raise ValueError("local-lint: requested module count does not match checker report")
        failures = diagnose(
            report, pairs(project / "scripts/lint-baseline.txt"), pairs(project / "scripts/lint-nolints-allowlist.txt")
        )
        print(
            f"local-lint: checked {len(modules)} changed module(s), {report['declarations']} declarations; "
            f"{report['doc_scanned']} documentation checks; {report['linters']} environment linters"
        )
        for message in failures:
            print(message)
        return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"local-lint: {error}")
        raise SystemExit(75) from None
