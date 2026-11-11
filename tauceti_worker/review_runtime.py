"""Resolve one review engine for preparation, inference, publication and archive.

The source shim already supplies Tools. The review engine is Python source, so
the existing on-demand checkout works in both a shim and an installed Worker;
neither needs another distribution/pin in the launcher's inline dependencies.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import NoProgress


@dataclass(frozen=True)
class ReviewRuntime:
    directory: Path
    revision: str

    def command(self, *arguments):
        return [
            sys.executable,
            str(self.directory / "runner/cli.py"),
            "--repo-dir",
            str(self.directory),
            *map(str, arguments),
        ]


def resolve(cfg):
    from .agents import fetch_ref
    from .constants import REVIEW
    from .worker_sources import shared_github_environment

    override = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")
    revision = os.environ.get("TAUCETI_REVIEW_REF")
    if revision and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise NoProgress("review engine revision must be an exact configured commit")
    directory = (
        Path(override).resolve()
        if override
        else cfg.state / "refs" / ("review-engine" + ("-" + revision if revision else ""))
    )
    if not override:
        with shared_github_environment(required=False):
            if revision:
                directory.mkdir(parents=True, exist_ok=True)
                commands = (
                    ["git", "init", "-q", str(directory)],
                    [
                        "git",
                        "-C",
                        str(directory),
                        "fetch",
                        "--no-tags",
                        "--depth=1",
                        f"https://github.com/{REVIEW}",
                        revision,
                    ],
                    ["git", "-C", str(directory), "checkout", "--detach", revision],
                )
                current = subprocess.run(
                    ["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True
                )
                if current.returncode or current.stdout.strip() != revision:
                    for command in commands:
                        try:
                            result = subprocess.run(command, capture_output=True, timeout=300)
                        except (OSError, subprocess.TimeoutExpired) as error:
                            raise NoProgress(
                                f"review engine preparation unavailable ({type(error).__name__})"
                            ) from error
                        if result.returncode:
                            raise NoProgress("configured review engine could not be prepared")
            elif not fetch_ref(REVIEW, directory):
                raise NoProgress("review engine could not be prepared")
    if not (directory / "runner/cli.py").is_file() or not (directory / "rubrics").is_dir():
        raise NoProgress("review engine checkout is incomplete")
    current = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True)
    return ReviewRuntime(directory, current.stdout.strip() if current.returncode == 0 else "")


def pending_reviews(cfg):
    """Withhold a selected head with paid output until common recovery reconciles it."""
    from .runtime_status import read_json

    pending = set()
    root = cfg.state / "review-work"
    if not root.is_dir():
        return pending
    for work in root.iterdir():
        if work.is_symlink() or not work.is_dir():
            continue
        receipt = read_json(work / "publication.json")
        context = read_json(work / "review-context.json")
        if receipt.get("state") == "prepared":
            pending.add((receipt.get("pr"), receipt.get("head")))
        elif not receipt and context and not context.get("complete"):
            for name in ("post_plan.json", "plan.json"):
                plan = work / name
                if plan.is_file() and read_json(plan).get("review_changed") is not False:
                    pending.add((context.get("pr"), context.get("head")))
    return pending


def recover(worker, *, numbers=()):
    """Reconcile paid output before provider admission, independently of the source."""
    import shutil

    from tauceti_tools.forge import GITHUB

    from . import work_recovery
    from .retention import locked, read_json

    root = worker.cfg.state / "review-work"
    if not root.is_dir():
        return False
    identity = getattr(worker.gh, "identity", None)
    expected = identity.record() if identity else dict(forge=dict(kind="github", url=GITHUB.url), name=worker.gh.repo)
    eligible, unreadable = [], False
    for work in sorted(root.iterdir()):
        if work.is_symlink() or not work.is_dir():
            continue
        try:
            with locked(work / "live.lock"), locked(work / "runner.lock"):
                context = read_json(work / "review-context.json")
                receipt = read_json(work / "publication.json")
                if (work / "publication.json").exists() and not receipt:
                    unreadable = True
                    continue
                record = receipt or context
                if not record:
                    continue
                if (
                    record.get("source") != expected
                    or Path(record.get("ledger", record.get("store", ""))).resolve() != worker.cfg.store.resolve()
                ):
                    from .config import log

                    log(f"review recovery: preserving {work.name}, which belongs to another source/store")
                    continue
                if numbers and record.get("pr") not in numbers:
                    continue
                owner = read_json(work / "owner.json")
                if not (
                    owner.get("finished")
                    or context.get("lifecycle") == 2
                    or work_recovery.owner_stopped(owner.get("process", context.get("process")))
                ):
                    continue
                if work_recovery.live_users(work):
                    continue
                plan = next((work / name for name in ("post_plan.json", "plan.json") if (work / name).is_file()), None)
                if not receipt and not plan:
                    shutil.rmtree(work)
                    continue
                if receipt.get("state") == "prepared" or not receipt and plan:
                    eligible.append(work.name)
        except (BlockingIOError, FileNotFoundError):
            continue
    if not eligible:
        if unreadable:
            raise NoProgress("retained review publication record is unreadable; preserving workspace")
        return False
    runtime = worker.review_runtime = resolve(worker.cfg)
    command = runtime.command(
        "--recover-only",
        "--repo",
        getattr(worker.gh, "review_repository", worker.gh.repo),
        "--workdir",
        root,
        "--store",
        worker.cfg.store_dir,
        *[arg for name in eligible for arg in ("--recover-work", name)],
        *([str(numbers[0])] if len(numbers) == 1 else []),
    )
    result = subprocess.run(command)
    if result.returncode == 0:
        return True
    if result.returncode not in (82, 84, 86):
        raise NoProgress("rendered review output remains retained for publication retry")
    return False
