#!/usr/bin/env python3
"""Inventory tracked forge integrations at immutable Git revisions, without reading credentials.

Usage: python scripts/forge-inventory.py --output REPORT.json NAME=/checkout ...
Refresh each checkout's origin/main before running; untracked files and local edits
are never included. Findings identify review locations, not proven API compatibility.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "github_origin": r"(?:api\.|raw\.githubusercontent\.)?github\.com|raw\.githubusercontent\.com",
    "graphql": r"graphql|GraphQL|resolveReviewThread|reviewThreads",
    "gh_cli": r"\bgh (?:api|pr|issue|repo|run|auth|release|workflow)\b|[\"']gh[\"']\s*,",
    "credentials": r"GH_TOKEN|GITHUB_TOKEN|github_account|github_token_file|InstallationToken|github-app",
    "source_identity": r"TAUCETI_REPO|TAUCETI_FORK|Source-PR|repository_id|review_app_id",
    "workflow_event": r"pull_request_target|repository_dispatch|merge_group|issue_comment|workflow_dispatch",
    "shared_state": r"github.state|github_state|GITHUB_STATE",
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def inspect(name, root, ref):
    commit = git(root, "rev-parse", ref + "^{commit}").decode().strip()
    files = git(root, "ls-tree", "-rz", "--name-only", commit).decode().split("\0")
    findings = []
    for file in files:
        parts = Path(file).parts
        if not parts or any(part in {"tests", "test", "fixtures", "build", "vendor", "node_modules"} for part in parts):
            continue
        if file.startswith("docs/") or file.endswith("README.md"):
            continue
        if Path(file).name.startswith("test_") or file.endswith((".test.ts", ".spec.ts")):
            continue
        if Path(file).suffix == ".md" and not (
            "prompts" in parts or Path(file).name == "AGENTS.md" or file.startswith("tauceti_tools/assets/scripts/")
        ):
            continue
        if Path(file).suffix not in {".py", ".sh", ".yml", ".yaml", ".ts", ".toml", ".md"}:
            continue
        data = git(root, "show", commit + ":" + file)
        if len(data) > 2 * 1024 * 1024:
            continue
        try:
            lines = data.decode().splitlines()
        except UnicodeError:
            continue
        matches = {
            category: [i for i, line in enumerate(lines, 1) if pattern.search(line)]
            for category, pattern in COMPILED.items()
        }
        matches = {category: numbers for category, numbers in matches.items() if numbers}
        if matches:
            findings.append({"path": file, "categories": matches})
    return {
        "repository": name,
        "source_revision": commit,
        "canonical_shared_assets": name == "TauCetiTools",
        "counts": {category: sum(category in row["categories"] for row in findings) for category in PATTERNS},
        "files": findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("repositories", nargs="+", help="NAME=/absolute/checkout")
    args = parser.parse_args()
    repositories = []
    for spec in args.repositories:
        name, separator, path = spec.partition("=")
        if not separator or not name or not path:
            parser.error("repository must be NAME=/checkout")
        repositories.append(inspect(name, Path(path), args.ref))
    report = {
        "schema": "tauceti.forge-inventory/v1",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "scope": "Tracked executable/configuration/template/prompt sources; excludes tests, docs, fixtures, and vendored dependencies.",
        "limitations": "Lexical findings guide manual contract review; absence is not proof of provider independence. Dynamic commands require inspection.",
        "repositories": repositories,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for row in repositories:
        print(f"{row['repository']}@{row['source_revision'][:12]}: {len(row['files'])} integration files")


if __name__ == "__main__":
    main()
