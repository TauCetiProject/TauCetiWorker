"""Review rounds in Bubble: the git and gh wrappers that give the review engine what Bubble's proxy does not.

Bubble writes `url.<proxy>.insteadOf = https://github.com/` and the proxy's token header into the
container user's ~/.gitconfig, which TauCetiTools blanks for source git, and its proxy refuses `/user`.
A local dumb-HTTP server stands in for the proxy, so these tests never reach GitHub.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tauceti_tools.forge import GITHUB
from tauceti_tools.git_credentials import scoped_git_environment

from tauceti_worker import agents

WRAPPER = ROOT / "scripts" / "review-git"
GH_WRAPPER = ROOT / "scripts" / "review-gh"
GIT = shutil.which("git")
REPOSITORY = "https://github.com/TauCetiProject/TauCeti"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Proxy(http.server.SimpleHTTPRequestHandler):
    tokens: list

    def do_GET(self):
        self.tokens.append(self.headers.get("X-Bubble-Token"))
        super().do_GET()

    def log_message(self, *args):
        pass


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        served = self.root / "served" / "git" / "TauCetiProject" / "TauCeti"
        work = self.root / "work"
        git("init", "-q", "-b", "main", str(work))
        (work / "README").write_text("x\n")
        git("add", "README", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=work)
        self.head = git("rev-parse", "HEAD", cwd=work)
        served.parent.mkdir(parents=True)
        git("clone", "-q", "--bare", str(work), str(served))
        git("update-server-info", cwd=served)

        self.tokens = []
        handler = type("Handler", (Proxy,), {"tokens": self.tokens})
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(handler, directory=str(self.root / "served"))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.proxy = f"http://127.0.0.1:{server.server_address[1]}"

        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            f'[url "{self.proxy}/git/"]\n\tinsteadOf = https://github.com/\n'
            f'[http "{self.proxy}/"]\n\textraHeader = X-Bubble-Token: proxy-token\n'
            '[url "file:///elsewhere/"]\n\tinsteadOf = https://example.com/\n'
        )
        self.bin = self.root / "review-git"
        self.bin.mkdir()
        (self.bin / "git").symlink_to(WRAPPER)
        self.parent = {"HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}

    def run_git(self, env, *args):
        return subprocess.run([str(self.bin / "git"), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_scoped_source_git_reaches_the_proxy(self):
        env = scoped_git_environment(GITHUB, "github-token", parent=self.parent)
        unrouted = subprocess.run(
            [GIT, "-c", f"url.{self.proxy}/git/.insteadOf=https://github.com/", "ls-remote", REPOSITORY],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(unrouted.returncode, 0)
        self.assertIn("transport 'http' not allowed", unrouted.stderr)
        result = self.run_git(env, "ls-remote", REPOSITORY, "refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [self.head, "refs/heads/main"])
        self.assertIn("proxy-token", self.tokens)

    def test_blanked_config_without_credentials_reaches_the_proxy(self):
        env = dict(self.parent, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        result = self.run_git(env, "ls-remote", REPOSITORY, "refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[0], self.head)

    def test_only_http_proxy_settings_are_reapplied(self):
        env = dict(self.parent, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        listed = self.run_git(env, "config", "--show-scope", "--get-regexp", r"^(url|http|protocol)\.").stdout
        self.assertEqual(
            sorted(listed.splitlines()),
            sorted(
                [
                    f"command\turl.{self.proxy}/git/.insteadof https://github.com/",
                    f"command\thttp.{self.proxy}/.extraheader X-Bubble-Token: proxy-token",
                    "command\tprotocol.http.allow always",
                ]
            ),
        )

    def test_user_config_is_untouched_when_not_blanked(self):
        listed = self.run_git(self.parent, "config", "--show-scope", "--get-regexp", r"^(url|protocol)\.").stdout
        self.assertNotIn("command", listed)
        self.assertIn(f"global\turl.{self.proxy}/git/.insteadof https://github.com/", listed)


class GhWrapper(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        real, self.bin = root / "real", root / "review-bin"
        real.mkdir()
        self.bin.mkdir()
        (real / "gh").write_text('#!/bin/sh\necho "real gh $*"\n')
        (real / "gh").chmod(0o755)
        (self.bin / "gh").symlink_to(GH_WRAPPER)
        self.env = {"PATH": f"{self.bin}{os.pathsep}{real}{os.pathsep}/usr/bin:/bin", "TAUCETI_BUBBLE_LOGIN": "someone"}

    def gh(self, *args, env=None):
        return subprocess.run([str(self.bin / "gh"), *args], env=env or self.env, capture_output=True, text=True).stdout

    def test_only_the_login_query_is_answered(self):
        self.assertEqual(self.gh("api", "user", "--jq", ".login"), "someone\n")
        self.assertEqual(self.gh("api", "user"), "real gh api user\n")
        self.assertEqual(self.gh("pr", "view", "1"), "real gh pr view 1\n")
        unset = {k: v for k, v in self.env.items() if k != "TAUCETI_BUBBLE_LOGIN"}
        self.assertEqual(self.gh("api", "user", "--jq", ".login", env=unset), "real gh api user --jq .login\n")


class Staging(unittest.TestCase):
    def test_review_commands_find_the_wrappers_first(self):
        root = Path(tempfile.mkdtemp())
        record = root / "argv"
        bubble = root / "fake-bubble.py"
        bubble.write_text(
            f"import json, pathlib, sys\npathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        )
        state = root / "state"
        state.mkdir()
        worker = SimpleNamespace(
            cfg=SimpleNamespace(state=state, wid="w", home=root, logdir=state),
            rc=SimpleNamespace(add_cleanup=lambda _: None),
        )
        opts = SimpleNamespace(work_model="claude", account=None)
        with contextlib.ExitStack() as stack:
            for name, replacement in (
                ("ensure_bubble_home", lambda _: dict(os.environ)),
                ("_bubble_pop", lambda *_: None),
                ("mirror_creds", lambda _: None),
                ("bubble_cmd", lambda: [sys.executable, str(bubble)]),
                ("_stage_claude_creds_for_bubble", lambda *a, **k: None),
                ("me", lambda: "someone"),
            ):
                stack.enter_context(patch.object(agents, name, replacement, create=True))
            stack.enter_context(patch.dict(os.environ, {"TAUCETI_API_CONTEXT": ""}))
            agents.run_in_bubble(worker, "repo/pull/1", "", opts, inner_cmd="true")
        argv = json.loads(record.read_text())
        command = argv[argv.index("--command") + 1]
        self.assertIn("PATH=/opt/round/review-bin:/opt/round:$PATH ", command)
        self.assertIn(" TAUCETI_BUBBLE_LOGIN=someone ", command)
        for name, source in (("git", WRAPPER), ("gh", GH_WRAPPER)):
            staged = state / "bubble-round" / "review-bin" / name
            self.assertEqual(staged.read_bytes(), source.read_bytes())
            self.assertTrue(os.access(staged, os.X_OK))


if __name__ == "__main__":
    unittest.main()
