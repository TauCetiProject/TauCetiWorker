#!/usr/bin/env python3
"""Register and record the AI merge queue's GitHub App.

`url` prints a prefilled organization App registration URL carrying the permissions and
webhook events the queue service needs (docs/redesign/specs/ai-merge-queue.md, work plan item 1).
GitHub does not accept a webhook secret through URL parameters, so that field is set in the form.

`save` records the App's identity and private key under ~/.config/tauceti-queue, the location
docs/queue/operations.md names. It reads the App ID and slug from arguments and the PEM private key
from stdin; nothing secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.parse

OWNER = "TauCetiProject"
CONFIG = pathlib.Path.home() / ".config/tauceti-queue"
NAME = "TauCeti Queue"
DESCRIPTION = "Scripted merge queue with AI conflict and CI repair for Tau Ceti"
HOMEPAGE = "https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/operations.md"
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "statuses": "write",
    "actions": "write",
    "workflows": "write",
    "metadata": "read",
}
EVENTS = [
    "pull_request",
    "pull_request_review",
    "issue_comment",
    "status",
    "check_run",
    "check_suite",
    "push",
    "workflow_run",
]


def registration_url(webhook_url: str) -> str:
    query: list[tuple[str, str]] = [
        ("name", NAME),
        ("description", DESCRIPTION),
        ("url", HOMEPAGE),
        ("public", "false"),
        ("webhook_active", "true" if webhook_url else "false"),
    ]
    if webhook_url:
        query.append(("webhook_url", webhook_url))
    query += list(PERMISSIONS.items())
    query += [("events[]", event) for event in EVENTS]
    return f"https://github.com/organizations/{OWNER}/settings/apps/new?" + urllib.parse.urlencode(query)


def save_private(path: pathlib.Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text if text.endswith("\n") else text + "\n")


def save(app_id: int, slug: str, config: pathlib.Path) -> None:
    if (config / "github-app.json").exists():
        raise SystemExit(f"{config / 'github-app.json'} exists; refusing to overwrite it")
    pem = sys.stdin.read()
    if not pem.startswith("-----BEGIN ") or "PRIVATE KEY-----" not in pem:
        raise SystemExit("Expected a PEM private key on stdin")
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise SystemExit("The App slug is the lowercase name from the App's settings URL")
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_private(config / "github-private-key.pem", pem)
    save_private(
        config / "github-app.json",
        json.dumps(
            {"id": app_id, "slug": slug, "owner": OWNER, "permissions": PERMISSIONS, "events": EVENTS}, indent=2
        ),
    )
    print(f"Saved {OWNER} GitHub App {slug} ({app_id}) to {config}")
    print(
        f"Install it on TauCetiProject/TauCeti and the scratch repository: https://github.com/apps/{slug}/installations/new"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    url = commands.add_parser("url", help="print the prefilled registration URL")
    url.add_argument("--webhook-url", default="", help="public HTTPS webhook endpoint; omit until the host exists")
    record = commands.add_parser("save", help="record the App ID, slug and private key (PEM on stdin)")
    record.add_argument("--app-id", type=int, required=True)
    record.add_argument("--slug", required=True)
    record.add_argument("--config", type=pathlib.Path, default=CONFIG)
    args = parser.parse_args()
    if args.command == "url":
        print(registration_url(args.webhook_url))
    else:
        save(args.app_id, args.slug, args.config)


if __name__ == "__main__":
    main()
