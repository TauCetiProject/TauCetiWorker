#!/usr/bin/env python3
"""Bubble rounds must use the stable release containing SSH and artifact-cache fixes."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


for version, accepted in (
    ("0.7.28", False),
    ("0.7.29", False),  # has the macOS $HOME fix but not bubble's $CODEX_HOME support
    ("0.7.30", True),
    ("0.7.30+local", True),
    ("0.7.31", True),
    ("0.7.31+local", True),
    ("0.8.0", True),
    ("", False),
    ("not-a-version", False),
):
    check(
        f"version comparison: {version or '<empty>'}",
        tc.bubble_version_meets_minimum(version) is accepted,
    )


def opts(*, dry_run=False, model="claude"):
    return tc.RoundOpts(
        only=["review"],
        agent="claude",
        work_model=model,
        sandbox_host=False,
        dry_run=dry_run,
    )


saved_have = tc.cli._have
saved_disposable = tc.cli.bubble_cmd_is_disposable
saved_version = tc.cli.installed_bubble_version
try:
    tc.cli._have = lambda _tool: True
    tc.cli.bubble_cmd_is_disposable = lambda: False

    tc.cli.installed_bubble_version = lambda: "0.7.29"
    try:
        tc.cli.preflight(SimpleNamespace(), opts())
        blocked = False
    except tc.Die as exc:
        blocked = "0.7.30" in str(exc) and "--force" in str(exc)
    check("real bubble round rejects 0.7.29 with update guidance", blocked)

    tc.cli.installed_bubble_version = lambda: "0.7.30"
    try:
        tc.cli.preflight(SimpleNamespace(), opts(model="kiro"))
        kiro_blocked = False
    except tc.Die as exc:
        kiro_blocked = "0.7.31" in str(exc)
    check("Kiro Bubble round requires the Kiro-capable release", kiro_blocked)

    try:
        tc.cli.preflight(SimpleNamespace(), opts(model="claude"))
        existing_agent_accepted = True
    except tc.Die:
        existing_agent_accepted = False
    check("existing agents remain compatible with Bubble 0.7.30", existing_agent_accepted)

    tc.cli.installed_bubble_version = lambda: "0.7.31"
    try:
        tc.cli.preflight(SimpleNamespace(), opts())
        accepted = True
    except tc.Die:
        accepted = False
    check("real bubble review accepts 0.7.31", accepted)

    tc.cli.installed_bubble_version = lambda: "0.7.27"
    try:
        tc.cli.preflight(SimpleNamespace(), opts(dry_run=True))
        dry_run_accepted = True
    except tc.Die:
        dry_run_accepted = False
    check("dry-run remains side-effect-free and skips the installed-version gate", dry_run_accepted)
finally:
    tc.cli._have = saved_have
    tc.cli.bubble_cmd_is_disposable = saved_disposable
    tc.cli.installed_bubble_version = saved_version

sys.exit(1 if fails else 0)
