#!/usr/bin/env python3
"""Inventory tracked forge integrations at immutable Git revisions, without reading credentials.

Usage: python scripts/forge-inventory.py --output REPORT.json NAME=/checkout ...
Refresh each checkout's origin/main before running; untracked files and local edits
are never included. Findings identify review locations, not proven API compatibility.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "github_origin": r"github\.com|githubusercontent\.com|ghcr\.io|github\.io",
    "graphql": r"graphql|GraphQL|resolveReviewThread|reviewThreads",
    "gh_cli": r"\bgh\s+(?:--?[A-Za-z]|[a-z][\w-]*\b)|[\"']gh[\"']\s*,|\(\s*\[\s*[\"'](?:api|pr|issue|repo|label|run)[\"']\s*,",
    "credentials": r"GH_[A-Z][A-Z0-9_]*|GITHUB_(?:TOKEN|ENTERPRISE_TOKEN)|github_account|github_token_file|InstallationToken|github-app|secrets\.\w+|APP_PRIVATE_KEY|APP_ID|CLAIMS_APP_",
    "source_identity": r"TAUCETI_REPO|TAUCETI_FORK|Source-PR|repository_id|review_app_id",
    "org_slug": r"TauCetiProject/[\w.-]+",
    "forge_paths": r"\.(?:github|forgejo|gitea)/",
    "actions_context": r"github\.(?!com\b|io\b)[a-z_]+|GITHUB_[A-Z][A-Z0-9_]*|vars\.\w+",
    "workflow_event": r"pull_request|repository_dispatch|merge_group|issue_comment|workflow_(?:dispatch|call|run)|pull_request_review|check_(?:run|suite)|\b(?:push|schedule)\b",
    "workflow_uses": r"\buses\s*:",
    "shared_state": r"github[.-]state|github_state|GITHUB_STATE",
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}
EXCLUDED_PARTS = {"tests", "test", "fixtures", "build", "vendor", "node_modules", ".lake", "records", "logs", "db"}
EXTENSIONS = {
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".ts",
    ".js",
    ".mjs",
    ".cjs",
    ".toml",
    ".md",
    ".json",
    ".jsonc",
    ".service",
    ".timer",
    ".socket",
    ".example",
    ".conf",
    ".ini",
    ".nix",
}
MAX_BYTES = 2 * 1024 * 1024


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git in {root}: {result.stderr.decode(errors='replace')[:1000]}")
    return result.stdout


def inspect(name, root, ref, canonical_names=("TauCetiTools",)):
    commit = git(root, "rev-parse", ref + "^{commit}").decode().strip()
    files = git(root, "ls-tree", "-rlz", commit).split(b"\0")
    findings, skipped = [], []
    for entry in files:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        if kind != b"blob":
            continue
        file = raw_name.decode(errors="surrogateescape")
        parts = Path(file).parts
        ownership = Path(file).name == "CODEOWNERS"
        forge_config = any(part in {".github", ".forgejo", ".gitea"} for part in parts[:-1])
        if not parts or (
            not forge_config
            and (any(part in EXCLUDED_PARTS for part in parts[:-1]) or ("docs" in parts[:-1] and not ownership))
        ):
            continue
        if not forge_config and not ownership and (file.startswith("docs/") or file.endswith("README.md")):
            continue
        if not forge_config and (Path(file).name.startswith("test_") or file.endswith((".test.ts", ".spec.ts"))):
            continue
        if (
            not forge_config
            and Path(file).suffix == ".md"
            and not (
                "prompts" in parts
                or Path(file).name in {"AGENTS.md", "CLAUDE.md", "SKILL.md"}
                or file.startswith("tauceti_tools/assets/scripts/")
            )
        ):
            continue
        known = (
            forge_config
            or Path(file).name in {"CODEOWNERS", "lakefile.lean"}
            or Path(file).suffix in EXTENSIONS
            or Path(file).name.startswith("Dockerfile")
            or mode == b"100755"
        )
        if not known and Path(file).suffix:
            continue
        if int(size) > MAX_BYTES:
            skipped.append({"path": file, "reason": "exceeds_byte_limit", "size": int(size)})
            continue
        data = git(root, "cat-file", "blob", oid.decode("ascii"))
        if not known and not data.startswith(b"#!"):
            continue
        try:
            lines = data.decode().split("\n")
        except UnicodeError:
            skipped.append({"path": file, "reason": "non_utf8", "size": len(data)})
            continue
        matches = {
            category: [i for i, line in enumerate(lines, 1) if pattern.search(line)]
            for category, pattern in COMPILED.items()
        }
        matches = {category: numbers for category, numbers in matches.items() if numbers}
        workflow = any(parts[i] == ".github" and parts[i + 1] == "workflows" for i in range(len(parts) - 1))
        if matches or workflow or forge_config or Path(file).name == "CODEOWNERS":
            findings.append({"path": file, "categories": matches})
    return {
        "repository": name,
        "source_revision": commit,
        "canonical_shared_assets": name in canonical_names,
        "counts": {category: sum(category in row["categories"] for row in findings) for category in PATTERNS},
        "files": findings,
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--replay", type=Path, help="Use per-repository immutable revisions from a prior report")
    parser.add_argument("--canonical-shared-assets", action="append", default=None, metavar="NAME")
    parser.add_argument("repositories", nargs="+", help="NAME=/absolute/checkout")
    args = parser.parse_args()
    repositories = []
    revisions = (
        {row["repository"]: row["source_revision"] for row in json.loads(args.replay.read_text())["repositories"]}
        if args.replay
        else {}
    )
    canonical_names = args.canonical_shared_assets or ["TauCetiTools"]
    for spec in args.repositories:
        name, separator, path = spec.partition("=")
        if not separator or not name or not path:
            parser.error("repository must be NAME=/checkout")
        if args.replay and name not in revisions:
            parser.error(f"repository {name} is absent from the replay report")
        if any(row["repository"] == name for row in repositories):
            parser.error(f"duplicate repository name {name}")
        repositories.append(inspect(name, Path(path), revisions.get(name, args.ref), canonical_names))
    report = {
        "schema": "tauceti.forge-inventory/v1",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "scanner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy": {
            "patterns": PATTERNS,
            "extensions": sorted(EXTENSIONS),
            "excluded_parts": sorted(EXCLUDED_PARTS | {"docs"}),
            "exclusions_apply_to": "Directory components outside forge configuration; docs/CODEOWNERS is retained",
            "max_bytes": MAX_BYTES,
            "canonical_names": canonical_names,
            "extra_inclusions": [
                "executable Git blobs",
                "extensionless shebang scripts",
                "Dockerfile*",
                "all files in .github/, .forgejo/ and .gitea/ configuration directories",
                "CODEOWNERS, including docs/CODEOWNERS",
                "lakefile.lean",
            ],
            "extra_exclusions": [
                "Outside forge configuration: *README.md, basenames beginning test_, *.test.ts, *.spec.ts",
                "Outside forge configuration: Markdown unless under prompts/, named AGENTS.md/CLAUDE.md/SKILL.md, or under tauceti_tools/assets/scripts/",
            ],
        },
        "scope": "Selected tracked executable/configuration/template/prompt text sources under the recorded candidate policy, including extensionless scripts, Dockerfiles, service units, JSON/JSONC, CODEOWNERS and examples. Outside forge configuration, archived records, tests, docs (except CODEOWNERS), fixtures, build artifacts and vendored dependencies are excluded. Forge configuration directories are enumerated unconditionally, subject to recorded size/encoding skips.",
        "limitations": "Lexical findings guide manual contract review; absence is not proof of provider independence. Dynamic commands require inspection.",
        "repositories": repositories,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for row in repositories:
        print(f"{row['repository']}@{row['source_revision'][:12]}: {len(row['files'])} integration files")


if __name__ == "__main__":
    main()
