#!/usr/bin/env python3
"""Prepare/register the dedicated App and provision Cloudflare without exposing secrets.

`prepare` creates a private HTML registration form and private client/admin credentials.
`exchange` accepts GitHub's one-use manifest code on stdin; `deploy` uploads secrets and resources.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
EVENTS = [
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "issues",
    "issue_comment",
    "label",
    "milestone",
    "push",
    "create",
    "delete",
    "repository",
    "status",
    "check_run",
    "check_suite",
    "workflow_run",
    "workflow_job",
    "merge_group",
]
PERMISSIONS = {
    key: "read"
    for key in ["metadata", "contents", "pull_requests", "issues", "checks", "statuses", "actions", "merge_queues"]
}


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    path.chmod(0o600)


def run(*args, input=None, check=True, env=None):
    return subprocess.run(list(args), cwd=SERVICE, input=input, text=True, capture_output=True, check=check, env=env)


def app_jwt(directory):
    code = (
        "import {readFileSync} from 'node:fs'; import {sign} from 'node:crypto'; "
        "const directory=process.argv[1]; "
        "const app=JSON.parse(readFileSync(directory+'/github-app.json','utf8')); "
        "const now=Math.floor(Date.now()/1000); "
        "const enc=(v)=>Buffer.from(JSON.stringify(v)).toString('base64url'); "
        "const data=enc({alg:'RS256',typ:'JWT'})+'.'+enc({iat:now-60,exp:now+540,iss:String(app.id)}); "
        "const key=readFileSync(directory+'/github-private-key.pem','utf8'); "
        "process.stdout.write(data+'.'+sign('RSA-SHA256',Buffer.from(data),key).toString('base64url'));"
    )
    response = run("node", "--input-type=module", "-e", code, str(directory), check=False)
    if response.returncode:
        raise SystemExit("App authentication preparation failed; raw details withheld")
    return response.stdout.strip()


def app_api(path, token, body=None):
    # App JWTs require explicit Bearer authentication; gh's ordinary token
    # authentication rejects these. Keep credentials in HTTP headers only.
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TauCetiWorker/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"App API request failed (HTTP {error.code}); credentials withheld") from None


def inspect_installation(directory, name, repository, installation=None):
    app = json.loads((directory / "github-app.json").read_text())
    if app["slug"] != name:
        raise SystemExit("Wrong App")
    token = app_jwt(directory)
    owner = repository.split("/", 1)[0]
    matches = [
        i
        for i in app_api("app/installations?per_page=100", token)
        if i.get("account", {}).get("login", "").lower() == owner.lower()
        and (installation is None or str(i["id"]) == str(installation))
    ]
    if len(matches) != 1:
        raise SystemExit("Expected exactly one dedicated App installation on " + owner)
    match = matches[0]
    if match.get("suspended_at") or match.get("repository_selection") != "selected":
        raise SystemExit("Installation must be active with Only select repositories")
    if any(access != "read" for access in match.get("permissions", {}).values()):
        raise SystemExit("Installation has permissions beyond read-only")
    # Validate the actual selected repository using the App's own quota.
    grant = app_api(f"app/installations/{match['id']}/access_tokens", token, {})
    selected = app_api("installation/repositories?per_page=100", grant["token"])
    rows = selected.get("repositories", [])
    if selected.get("total_count") != 1 or len(rows) != 1 or rows[0]["full_name"].lower() != repository.lower():
        raise SystemExit("Dedicated App must be installed only on " + repository)
    result = {
        "app_id": app["id"],
        "installation": str(match["id"]),
        "permissions": match["permissions"],
        "repository": rows[0]["full_name"],
        "repository_id": str(rows[0]["id"]),
        "verified_at": time.time(),
    }
    private(directory / "installation.json", json.dumps(result))
    return result


def finish_exchange(directory, value, name):
    if value.get("slug") != name:
        raise SystemExit("Wrong App slug; refusing to adopt unrelated credentials")
    # Persist the one-use conversion result before any fallible key conversion.
    private(directory / "github-app-credentials.json", json.dumps(value))
    private(directory / "github-webhook-secret", value["webhook_secret"])
    # WebCrypto imports PKCS8; the manifest supplies a PKCS1 RSA PEM. Node is
    # already required for Wrangler, so setup also works without system OpenSSL.
    if shutil.which("openssl"):
        converted = run("openssl", "pkcs8", "-topk8", "-nocrypt", input=value["pem"], check=False)
    else:
        converted = run(
            "node",
            "--input-type=module",
            "-e",
            "import { createPrivateKey } from 'node:crypto'; "
            "let pem = ''; for await (const chunk of process.stdin) pem += chunk; "
            "process.stdout.write(createPrivateKey(pem).export({type:'pkcs8',format:'pem'}));",
            input=value["pem"],
            check=False,
        )
    if converted.returncode or not converted.stdout.startswith("-----BEGIN PRIVATE KEY-----"):
        raise SystemExit("Key conversion failed; exchange credentials saved privately for recovery")
    private(directory / "github-private-key.pem", converted.stdout)
    private(directory / "github-app.json", json.dumps({k: value[k] for k in ("id", "slug", "html_url")}))


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "exchange", "finish-exchange", "inspect", "deploy", "bootstrap"])
    p.add_argument("--environment", choices=["production", "test"], default="production")
    p.add_argument("--credentials-dir", type=Path)
    p.add_argument("--installation", help="dedicated installation ID; required for deploy")
    p.add_argument("--repository", default="TauCetiProject/TauCeti")
    a = p.parse_args()
    suffix = "-test" if a.environment == "test" else ""
    name = "tauceti-github-state" + suffix
    url = f"https://github-state{suffix}.taucetiproject.org"
    directory = a.credentials_dir or Path.home() / ".config" / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    wrangler = ["npx", "wrangler"]
    flags = ["--env", "test" if suffix else ""]
    if a.action == "prepare":
        manifest = {
            "name": name,
            "url": url,
            "hook_attributes": {"url": url + "/webhook/github", "active": True},
            "redirect_url": url + "/registration-complete",
            "public": False,
            "default_permissions": PERMISSIONS,
            "default_events": EVENTS,
            "description": "Exclusive read-only webhook mirror for TauCeti fleet queries.",
        }
        form = f'<!doctype html><meta name="referrer" content="no-referrer"><title>Register {name}</title><h1>Register {name}</h1><p>Creates a separate read-only App owned by TauCetiProject. Install it only on TauCeti.</p><form action="https://github.com/organizations/TauCetiProject/settings/apps/new" method="post"><input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest), quote=True)}"><button>Register dedicated read-only App</button></form>'
        private(directory / "register.html", form)
        private(directory / "manifest.json", json.dumps(manifest, indent=2))
        for field in ["client-token", "admin-token"]:
            if not (directory / field).exists():
                private(directory / field, secrets.token_urlsafe(48) + "\n")
        print("Registration form:", directory / "register.html")
        print("After GitHub redirects, pass the code from the URL to exchange on stdin. Do not reuse another App.")
    elif a.action in ("exchange", "finish-exchange"):
        if a.action == "finish-exchange":
            value = json.loads((directory / "github-app-credentials.json").read_text())
        else:
            code = input().strip()
            if not re.fullmatch("[a-fA-F0-9]{40}", code):
                raise SystemExit("Expected one-use 40-character manifest code on stdin")
            r = run("gh", "api", "-X", "POST", f"app-manifests/{code}/conversions", check=False)
            if r.returncode:
                raise SystemExit("Manifest exchange failed; raw response withheld to protect credentials")
            value = json.loads(r.stdout)
        finish_exchange(directory, value, name)
        print("Dedicated App credentials saved privately; install only on", a.repository)
        print(value["html_url"] + "/installations/new")
    elif a.action == "inspect":
        print(json.dumps(inspect_installation(directory, name, a.repository, a.installation)))
    elif a.action == "deploy":
        verified = inspect_installation(directory, name, a.repository, a.installation)
        a.installation = verified["installation"]
        app = json.loads((directory / "github-app.json").read_text())
        if app["slug"] != name:
            raise SystemExit("Wrong App")
        info = {"id": verified["repository_id"]}
        bucket = name
        for queue in [name + "-events-dlq", name + "-events"]:
            listing = run(*wrangler, "queues", "list", check=False)
            if listing.returncode or queue not in listing.stdout:
                r = run(*wrangler, "queues", "create", queue, check=False)
                if r.returncode and not any(
                    message in r.stderr.lower() for message in ("already exists", "already taken")
                ):
                    raise SystemExit("Queue provisioning failed: " + r.stderr)
        r = run(*wrangler, "r2", "bucket", "create", bucket, check=False)
        if r.returncode and "already" not in r.stderr.lower():
            raise SystemExit("R2 provisioning failed: " + r.stderr)
        settings = {
            "GITHUB_APP_ID": str(app["id"]),
            "GITHUB_INSTALLATION_ID": a.installation,
            "GITHUB_PRIVATE_KEY": (directory / "github-private-key.pem").read_text(),
            "WEBHOOK_SECRET": (directory / "github-webhook-secret").read_text().strip(),
            "CLIENT_TOKENS": json.dumps(
                [
                    {
                        "hash": hashlib.sha256((directory / "client-token").read_text().strip().encode()).hexdigest(),
                        "repositories": [a.repository],
                    }
                ]
            ),
            "ADMIN_TOKEN_HASH": hashlib.sha256((directory / "admin-token").read_text().strip().encode()).hexdigest(),
            "ALLOWED_REPOSITORIES": json.dumps(
                [{"id": str(info["id"]), "name": a.repository, "installation": a.installation}]
            ),
        }
        # Deployment bindings are supplied through a generated private config, avoiding duplicate vars/secrets.
        config = json.loads((SERVICE / "wrangler.jsonc").read_text())
        target = config["env"]["test"] if suffix else config
        target["vars"] = {
            **target.get("vars", {}),
            **{k: settings[k] for k in ["GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "ALLOWED_REPOSITORIES"]},
        }
        fd, path = tempfile.mkstemp(prefix="deploy-", suffix=".json", dir=SERVICE)
        try:
            os.close(fd)
            private(Path(path), json.dumps(config))
            r = run(*wrangler, "deploy", "--config", path, *flags, check=False)
            if r.returncode:
                raise SystemExit(r.stderr)
            r = run(
                *wrangler,
                "secret",
                "bulk",
                *flags,
                "--config",
                path,
                input=json.dumps({k: v for k, v in settings.items() if k not in target["vars"]}),
                check=False,
            )
            if r.returncode:
                raise SystemExit("Secret upload failed: " + r.stderr)
            private(
                directory / "deployment.json",
                json.dumps(
                    {
                        "name": name,
                        "url": url,
                        "repository": a.repository,
                        "repository_id": str(info["id"]),
                        "installation": a.installation,
                        "app_id": app["id"],
                    }
                ),
            )
        finally:
            Path(path).unlink(missing_ok=True)
        print("Deployed:", url, "; bootstrap next. Client token remains private at", directory / "client-token")
    else:
        import urllib.request

        request = urllib.request.Request(
            url + "/admin/bootstrap",
            data=json.dumps({"repository": a.repository}).encode(),
            headers={
                "Authorization": "Bearer " + (directory / "admin-token").read_text().strip(),
                "Content-Type": "application/json",
                "User-Agent": "TauCetiWorker/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.status, response.read().decode())


if __name__ == "__main__":
    cli()
