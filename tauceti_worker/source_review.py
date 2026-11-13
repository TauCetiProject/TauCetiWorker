"""Legacy native-review entrypoint; delegate to the common review CLI."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import replace
from pathlib import Path

from .config import Config
from .review_runtime import resolve
from .source_cli import load_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    source = load_source()
    cfg = replace(Config.resolve(), state=args.work.parent.parent)
    runtime = resolve(cfg)
    return subprocess.run(
        runtime.command(
            args.pr,
            "--repo",
            source.repository.key,
            "--expect-head",
            args.head,
            "--workdir",
            args.work,
            "--store",
            args.store,
            "--reviewer",
            args.reviewer,
            "--post",
            "--no-sync",
        )
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
