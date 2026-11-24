#!/usr/bin/env python3
"""Register, record and check the review posting GitHub App (tauceti_worker/review_app.py).

The App is a second GitHub identity for review comments, beside the worker account. It needs an
organization owner once; everything else runs on a posting host and prints nothing secret.

  manifest      write the App manifest page and print the link that opens it; the owner clicks
                "Create" on GitHub, which then shows a URL carrying a one-hour `code`
  convert CODE  exchange that code (or the whole URL) for the App's id, slug and private key, and
                save them under ~/.config/tauceti-review-poster (directory 0700, files 0600)
  installation  after the owner installs the App on TauCetiProject/TauCeti, record its installation
  verify        mint a token with the permissions review publication requests, and print what
                GitHub granted and whether TauCeti is reachable
  copy HOST...  copy the directory to other posting hosts over ssh, keeping it private

Then set `TAUCETI_REVIEW_APP_DIR` for the workers (docs/review-app.md).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

OWNER = "TauCetiProject"
REPOSITORY = "TauCeti"
NAME = "tauceti-review-poster"
CONFIG = pathlib.Path.home() / ".config" / NAME
HOMEPAGE = "https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/review-app.md"
# GitHub sends the owner here with `?code=...&state=...`; any page works, since the owner copies
# the address. The organization page exists and shows nothing that needs the code.
LANDING = f"https://github.com/{OWNER}"
# The App's permissions are exactly those its tokens request (tauceti_worker/review_app.py).
from tauceti_worker.review_app import PERMISSIONS  # noqa: E402


def manifest(state: str) -> dict:
    return {
        "name": NAME,
        "url": HOMEPAGE,
        "description": "Posts a share of the Tau Ceti fleet's AI review comments, beside tauceti-ai-for-science.",
        "hook_attributes": {"url": LANDING, "active": False},
        "redirect_url": LANDING + "?" + urllib.parse.urlencode({"review-app-state": state}),
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def page(state: str) -> str:
    action = f"https://github.com/organizations/{OWNER}/settings/apps/new?state={state}"
    value = html.escape(json.dumps(manifest(state)), quote=True)
    return (
        "<!doctype html><meta charset=utf-8><title>Create the review posting App</title>"
        f"<form action='{html.escape(action, quote=True)}' method=post>"
        f"<input type=hidden name=manifest value='{value}'>"
        "<p>Create the <b>tauceti-review-poster</b> GitHub App in TauCetiProject.</p>"
        "<button type=submit>Continue to GitHub</button></form>"
    )


def save_private(path: pathlib.Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text if text.endswith("\n") else text + "\n")
    os.chmod(path, 0o600)


def request(method: str, url: str, *, headers: dict | None = None, body: dict | None = None):
    from tauceti_queue import github_app

    data = None if body is None else json.dumps(body).encode()
    call = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": NAME + "-setup",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(call, context=github_app.ssl_context(), timeout=60) as response:
        return json.loads(response.read() or b"null")


def write_manifest(config: pathlib.Path) -> None:
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config, 0o700)
    state = secrets.token_urlsafe(24)
    save_private(config / "manifest-state", state)
    target = pathlib.Path("/tmp") / f"{NAME}-manifest.html"
    save_private(target, page(state))
    link = "data:text/html;charset=utf-8," + urllib.parse.quote(page(state))
    print(f"Manifest page: {target}")
    print("Or paste this into a browser's address bar, then click Continue and Create GitHub App:")
    print(link)
    print(f"Afterwards copy the address GitHub shows (it contains ?code=...) and run: {sys.argv[0]} convert 'ADDRESS'")


def convert(value: str, config: pathlib.Path) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query) if "://" in value else {}
    code = (query.get("code") or [value])[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", code):
        raise SystemExit("expected the code GitHub returned, or the whole address containing ?code=")
    state_file = config / "manifest-state"
    returned = (query.get("state") or query.get("review-app-state") or [""])[0]
    if returned and state_file.is_file() and returned != state_file.read_text().strip():
        raise SystemExit("the address's state does not match this host's manifest; rerun `manifest`")
    if (config / "github-private-key.pem").exists():
        raise SystemExit(f"{config / 'github-private-key.pem'} exists; refusing to overwrite it")
    app = request("POST", f"https://api.github.com/app-manifests/{code}/conversions")
    if not isinstance(app, dict) or type(app.get("id")) is not int or not isinstance(app.get("pem"), str):
        raise SystemExit("GitHub's conversion response had no App id or key")
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_private(config / "github-private-key.pem", app["pem"])
    save_private(
        config / "github-app.json",
        json.dumps({"id": app["id"], "slug": app["slug"], "owner": OWNER, "permissions": PERMISSIONS}, indent=2),
    )
    state_file.unlink(missing_ok=True)
    print(f"Saved GitHub App {app['slug']} ({app['id']}) to {config}; nothing secret was printed.")
    print(
        f"Install it on {OWNER} with only {REPOSITORY} selected: https://github.com/apps/{app['slug']}/installations/new"
    )
    print(f"Then run: {sys.argv[0]} installation")


def jwt_headers(metadata: dict, config: pathlib.Path) -> dict:
    from tauceti_queue import github_app

    key = (config / "github-private-key.pem").read_bytes()
    return {"Authorization": "Bearer " + github_app.app_jwt(metadata["id"], key)}


def installation(config: pathlib.Path, wait: float) -> None:
    metadata = json.loads((config / "github-app.json").read_text())
    deadline = time.time() + wait
    while True:
        rows = request("GET", "https://api.github.com/app/installations", headers=jwt_headers(metadata, config))
        found = [r for r in rows or [] if (r.get("account") or {}).get("login", "").lower() == OWNER.lower()]
        if found:
            break
        if time.time() >= deadline:
            raise SystemExit(f"the App is not installed on {OWNER} yet")
        time.sleep(10)
    metadata["installation_id"] = found[0]["id"]
    save_private(config / "github-app.json", json.dumps(metadata, indent=2))
    print(f"Recorded installation {found[0]['id']} ({found[0].get('repository_selection')} repositories).")
    print(f"Check it with: {sys.argv[0]} verify")


def verify(config: pathlib.Path) -> None:
    from tauceti_queue import github_app
    from tauceti_queue.github_adapter import UrllibTransport

    os.environ["TAUCETI_REVIEW_APP_DIR"] = str(config)
    from tauceti_worker import review_app

    credentials = review_app._credentials()
    source = github_app.InstallationTokenSource(
        credentials, UrllibTransport(timeout=45), permissions=dict(review_app.PERMISSIONS)
    )
    granted = source.granted_permissions()
    headers = {"Authorization": "Bearer " + source.token()}
    repositories = request("GET", "https://api.github.com/installation/repositories?per_page=100", headers=headers)
    names = sorted(r["full_name"] for r in (repositories or {}).get("repositories", []))
    print(f"App {review_app.login(credentials)} (id {credentials.app_id}, installation {credentials.installation_id})")
    print("granted:", json.dumps(granted, sort_keys=True))
    print("repositories:", ", ".join(names) or "(none)")
    missing = {k: v for k, v in review_app.PERMISSIONS.items() if granted.get(k) not in (v, "write")}
    if missing or f"{OWNER}/{REPOSITORY}" not in names:
        raise SystemExit(
            f"not ready: missing {missing or 'nothing'}; TauCeti selected: {f'{OWNER}/{REPOSITORY}' in names}"
        )
    print("ready: set TAUCETI_REVIEW_APP_DIR=" + str(config) + " for the workers")


def copy(hosts: list[str], config: pathlib.Path) -> None:
    remote = shlex.quote(".config/" + NAME)
    for host in hosts:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "--", host, f"umask 077 && mkdir -p {remote} && chmod 700 {remote}"],
            check=True,
        )
        for name in ("github-app.json", "github-private-key.pem"):
            with open(config / name, "rb") as source:
                subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "--",
                        host,
                        f"umask 077 && cat > {remote}/{name}.tmp && "
                        f"chmod 600 {remote}/{name}.tmp && mv {remote}/{name}.tmp {remote}/{name}",
                    ],
                    stdin=source,
                    check=True,
                )
        print(f"Copied to {host}:~/.config/{NAME}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=pathlib.Path, default=CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifest", help="write the manifest page and print its link")
    converting = commands.add_parser("convert", help="exchange GitHub's code for the App's key")
    converting.add_argument("code")
    installed = commands.add_parser("installation", help="record the App's installation on TauCetiProject")
    installed.add_argument("--wait", type=float, default=900, help="seconds to wait for the installation")
    commands.add_parser("verify", help="mint a token and check its permissions and repository")
    copying = commands.add_parser("copy", help="copy the credentials to other posting hosts")
    copying.add_argument("hosts", nargs="+")
    args = parser.parse_args()
    if args.command == "manifest":
        write_manifest(args.config)
    elif args.command == "convert":
        convert(args.code, args.config)
    elif args.command == "installation":
        installation(args.config, args.wait)
    elif args.command == "verify":
        verify(args.config)
    else:
        copy(args.hosts, args.config)


if __name__ == "__main__":
    main()
