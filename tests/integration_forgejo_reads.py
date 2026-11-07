#!/usr/bin/env python3
"""Disposable real Forgejo -> Worker ReviewState, with shared-service access blocked.

Set FORGEJO_BINARY to run. Every repository, credential, cookie and server process
belongs to a temporary loopback-only instance; no installed worker is executed.
"""

import base64
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import Forge, ForgeError, Forgejo, RepositoryRef

from tauceti_worker.forgejo_reads import BoundedReadCache
from tauceti_worker.github import GitHub
from tauceti_worker.review_state import ReviewState


class Integration(unittest.TestCase):
    def test_real_source_with_service_and_github_fallback_blocked(self):
        binary = os.environ.get("FORGEJO_BINARY")
        if not binary:
            self.skipTest("set FORGEJO_BINARY to run a disposable loopback server")
        with tempfile.TemporaryDirectory(prefix="worker-forgejo-") as temporary:
            root = Path(temporary)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            config = root / "app.ini"
            config.write_text(f"""RUN_USER = {getpass.getuser()}
WORK_PATH = {root}
[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = {origin}/
DISABLE_SSH = true
APP_DATA_PATH = {root}/data
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
[api]
MAX_RESPONSE_ITEMS = 1
DEFAULT_PAGING_NUM = 1
[log]
MODE = file
ROOT_PATH = {root}/log
""")
            base = [binary, "--work-path", temporary, "--config", str(config)]

            def admin(*args):
                result = subprocess.run([*base, *args], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, "local Forgejo setup failed; credential output withheld")
                return result.stdout

            admin("migrate")
            admin(
                "admin",
                "user",
                "create",
                "--username",
                "operator",
                "--email",
                "operator@example.invalid",
                "--admin",
                "--random-password",
                "--must-change-password=false",
            )

            def token(name, scopes):
                output = admin(
                    "admin",
                    "user",
                    "generate-access-token",
                    "--username",
                    "operator",
                    "--token-name",
                    name,
                    "--scopes",
                    scopes,
                    "--raw",
                )
                matches = re.findall(r"(?m)^[0-9a-f]{40}$", output)
                self.assertEqual(len(matches), 1, "local token unavailable; output withheld")
                return matches[0]

            api = Forgejo(Forge("forgejo", origin), token=token("source", "all"), timeout=3)
            restricted = Forgejo(api.forge, token=token("identity-only", "read:user"), timeout=3)
            with (root / "server.log").open("w") as log:
                process = subprocess.Popen([*base, "web"], stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            api.request("GET", "version")
                            break
                        except ForgeError:
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail("local Forgejo did not start; private log retained only within fixture")
                            time.sleep(0.05)
                    api.request(
                        "POST",
                        "user/repos",
                        {"name": "source", "private": True, "auto_init": True, "default_branch": "main"},
                    )
                    api.request(
                        "POST",
                        "repos/operator/source/branches",
                        {"new_branch_name": "proof", "old_branch_name": "main"},
                    )
                    api.request(
                        "POST",
                        "repos/operator/source/contents/Proof.lean",
                        {
                            "branch": "proof",
                            "message": "Add proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    pull = api.request(
                        "POST", "repos/operator/source/pulls", {"head": "proof", "base": "main", "title": "Proof"}
                    )
                    number = pull["number"]
                    repo = RepositoryRef(api.forge, "operator/source")

                    def board(head):
                        return "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + json.dumps({"head_sha": head}) + "-->"

                    comment = api.post_comment(repo, number, board("a" * 40))
                    for i in range(32):
                        api.post_comment(repo, number, f"Discussion {i}")
                    cfg = SimpleNamespace(sbcache=root / "review-state")
                    shared_cache = BoundedReadCache(max_entries=4, max_bytes=64 * 1024)
                    source = GitHub.for_repository(repo, api=api, cache=shared_cache)
                    other = GitHub.for_repository(repo, api=restricted, cache=shared_cache)
                    with (
                        patch(
                            "tauceti_worker.github_state.read", side_effect=AssertionError("shared service consulted")
                        ),
                        patch(
                            "tauceti_worker.review_state.github_state_enabled",
                            side_effect=AssertionError("shared service selected"),
                        ),
                        patch("tauceti_worker.github.gh_run", side_effect=AssertionError("native GitHub CLI fallback")),
                    ):
                        state = ReviewState(cfg, source)
                        self.assertEqual(state.gh_meta(number).provenance, "fresh")
                        self.assertEqual(ReviewState(cfg, source).gh_meta(number).provenance, "assumed")
                        history = source.issue_comments(number)
                        self.assertEqual(len(history), 33)
                        self.assertEqual(len({row["id"] for row in history}), 33)
                        self.assertTrue(all(row["node_id"].startswith(api.forge.key + "/comments/") for row in history))
                        review = api.request(
                            "POST",
                            f"repos/operator/source/pulls/{number}/reviews",
                            {
                                "event": "COMMENT",
                                "commit_id": pull["head"]["sha"],
                                "body": "Checked source",
                            },
                        )
                        self.assertEqual([row["state"] for row in source.reviews(number)], ["COMMENTED"])
                        draft = api.request(
                            "POST",
                            f"repos/operator/source/pulls/{number}/reviews",
                            {"event": "PENDING", "body": "Own draft"},
                        )
                        self.assertEqual(
                            {row["id"] for row in source.reviews(number, fresh=True)}, {review["id"], draft["id"]}
                        )
                        denied = ReviewState(cfg, other)
                        self.assertNotEqual(denied.sbcache, state.sbcache)
                        self.assertEqual(denied.gh_meta(number, force=True).provenance, "fetch_failed")
                        self.assertIn(other.last_failure.status, {401, 403, 404})
                        api.edit_comment(repo, comment["id"], board("b" * 40))
                        fresh = state.gh_meta(number, force=True)
                        self.assertEqual(fresh.provenance, "fresh")
                        self.assertEqual(fresh.data["head_sha"], "b" * 40)
                        state.bust(number)
                        self.assertEqual(state.gh_meta(number).data["head_sha"], "b" * 40)
                        api.request("DELETE", f"repos/operator/source/issues/comments/{comment['id']}")
                        self.assertEqual(state.gh_meta(number, force=True).provenance, "missing")
                        self.assertEqual(
                            source.pr_view(number, ["number", "title"], fresh=True),
                            {"number": number, "title": "Proof"},
                        )
                        self.assertLessEqual(shared_cache.stats()["entries"], 4)
                        self.assertLessEqual(shared_cache.stats()["bytes"], 64 * 1024)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
