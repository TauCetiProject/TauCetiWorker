"""A private, disposable HTTPS Forgejo with its ordinary repository storage."""

import contextlib
import getpass
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import Forge, ForgeError, Forgejo, RepositoryRef


@contextlib.contextmanager
def source_fixture():
    binary = os.environ["FORGEJO_BINARY"]
    with tempfile.TemporaryDirectory(prefix="queue-forgejo-git-") as temporary, contextlib.ExitStack() as cleanup:
        root = Path(temporary)
        cert, key = root / "source.crt", root / "source.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=source-fixture.invalid",
                "-config",
                "/dev/null",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        key.chmod(0o600)
        cleanup.enter_context(patch.dict(os.environ, SSL_CERT_FILE=str(cert), GIT_SSL_CAINFO=str(cert)))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"https://127.0.0.1:{port}"
        config = root / "app.ini"
        config.write_text(f"""RUN_USER = {getpass.getuser()}
WORK_PATH = {root}
[server]
PROTOCOL = https
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = {origin}/
CERT_FILE = {cert}
KEY_FILE = {key}
DISABLE_SSH = true
APP_DATA_PATH = {root}/data
STATIC_ROOT_PATH = {os.environ.get("FORGEJO_STATIC_ROOT", "")}
[database]
DB_TYPE = sqlite3
PATH = {root}/db.sqlite
[security]
INSTALL_LOCK = true
[repository]
ROOT = {root}/repos
DEFAULT_BRANCH = main
[service]
DISABLE_REGISTRATION = true
[actions]
ENABLED = false
[log]
MODE = file
ROOT_PATH = {root}/log
""")
        base = [binary, "--work-path", str(root), "--config", str(config)]

        def admin(*args):
            result = subprocess.run([*base, *args], capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError("disposable Forgejo setup failed; credential output withheld")
            return result.stdout

        admin("migrate")
        tokens = {}
        for username in ("operator", "contributor", "queue", "reviewer"):
            args = [
                "admin",
                "user",
                "create",
                "--username",
                username,
                "--email",
                f"{username}@example.invalid",
                "--random-password",
                "--must-change-password=false",
            ]
            if username == "operator":
                args.append("--admin")
            admin(*args)
            output = admin(
                "admin",
                "user",
                "generate-access-token",
                "--username",
                username,
                "--token-name",
                "source",
                "--scopes",
                "all",
                "--raw",
            )
            matches = re.findall(r"(?m)^[0-9a-f]{40}$", output)
            if len(matches) != 1:
                raise RuntimeError("disposable token unavailable")
            tokens[username] = matches[0]
        api = Forgejo(Forge("forgejo", origin), token=tokens["operator"], timeout=5)
        log = cleanup.enter_context((root / "server.log").open("w"))
        process = subprocess.Popen([*base, "web"], stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

        cleanup.callback(stop)
        deadline = time.monotonic() + 30
        while True:
            try:
                api.request("GET", "version")
                break
            except ForgeError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("disposable Forgejo did not start") from None
                time.sleep(0.05)
        row = api.request(
            "POST", "user/repos", {"name": "source", "private": True, "auto_init": True, "default_branch": "main"}
        )
        api.request("PUT", "repos/operator/source/collaborators/contributor", {"permission": "write"})
        api.request("PUT", "repos/operator/source/collaborators/queue", {"permission": "write"})
        api.request("PUT", "repos/operator/source/collaborators/reviewer", {"permission": "write"})
        repository = RepositoryRef(api.forge, "operator/source")
        yield root, api, repository, row, tokens
