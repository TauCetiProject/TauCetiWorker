#!/usr/bin/env python3
"""Account isolation through real child processes, Git helpers, and worker configuration.

All GitHub calls hit a temporary fake gh executable; no real credentials or network are used.
"""

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker import agents, cli, github, loop, tui
from tauceti_worker import worker_manager as wm
from tauceti_worker.config import Die

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
with open(os.environ["TEST_GH_LOG"], "a") as out:
    out.write(json.dumps({"args": args, "token": token, "home": os.environ["HOME"]}) + "\\n")
if args[:2] == ["auth", "token"]:
    user = args[args.index("--user") + 1]
    if user.lower() not in ("alice", "bob"):
        sys.exit(1)
    print("test-" + user.lower())
elif args[:2] == ["auth", "git-credential"]:
    print("username=x-access-token\\npassword=" + token)
elif args[0] == "api":
    user = token.removeprefix("test-")
    if user not in ("alice", "bob"):
        sys.exit(1)
    if "--jq" in args:
        print(user)
    else:
        print(json.dumps({"login": user, "id": 11 if user == "alice" else 22}))
else:
    sys.exit(1)
"""


def rejected(call, contains):
    try:
        call()
    except (Die, wm.WorkersError) as exc:
        assert contains in str(exc), str(exc)
    else:
        raise AssertionError("expected an account error")


with tempfile.TemporaryDirectory(prefix="tauceti-github-accounts-") as tmp:
    root = Path(tmp)
    fake = root / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(0o700)
    clean = {
        "HOME": tmp,
        "PATH": f"{tmp}:{os.environ['PATH']}",
        "PYTHONPATH": str(REPO),
        "TEST_GH_LOG": str(root / "gh.jsonl"),
        "TAUCETI_CONFIG_HOME": str(root / "config"),
    }
    with patch.dict(os.environ, clean, clear=True):
        # An inherited token for Bob must not override a requested stored Alice credential.
        os.environ["GH_TOKEN"] = "test-bob"
        os.environ["GITHUB_TOKEN"] = "test-bob"
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "test.operator"
        os.environ["GIT_CONFIG_VALUE_0"] = "preserved"
        assert github.pin_github_account("ALICE") == "alice"
        assert os.environ["GH_TOKEN"] == "test-alice"
        assert "GITHUB_TOKEN" not in os.environ
        assert os.environ["GIT_CONFIG_KEY_0"] == "test.operator"
        assert github.me() == "alice"
        assert github.me.cache_info().currsize == 1
        count = os.environ["GIT_CONFIG_COUNT"]

        # The child uses the pinned token after HOME is moved; no named credential re-lookup.
        child_env = {**os.environ, "HOME": str(root / "isolated")}
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tauceti_worker.github import pin_github_account; print(pin_github_account('alice'))",
            ],
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert child.stdout.strip() == "alice"
        events = [json.loads(line) for line in (root / "gh.jsonl").read_text().splitlines()]
        lookups = [e for e in events if e["args"][:2] == ["auth", "token"]]
        assert len(lookups) == 1 and lookups[0]["token"] == ""

        # Git must ignore an existing helper for another account, while preserving unrelated config.
        gitdir = root / "checkout"
        subprocess.run(["git", "init", "-q", str(gitdir)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(gitdir),
                "config",
                "credential.helper",
                "!f() { printf 'username=bob\npassword=test-bob\n'; }; f",
            ],
            check=True,
        )
        credential = subprocess.run(
            ["git", "-C", str(gitdir), "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            check=True,
        )
        assert "password=test-alice" in credential.stdout
        subprocess.run(["git", "-C", str(gitdir), "commit", "-q", "--allow-empty", "-m", "account test"], check=True)
        author = subprocess.check_output(["git", "-C", str(gitdir), "log", "-1", "--format=%an:%ae:%ce"], text=True)
        assert author.strip() == "alice:11+alice@users.noreply.github.com:11+alice@users.noreply.github.com"

        # Operator HTTPS-to-SSH rewrites must fail closed for fetch and push.
        for setting, operation in (("insteadOf", "ls-remote"), ("pushInsteadOf", "push")):
            command = ["git", "-C", str(gitdir), "-c", f"url.git@github.com:.{setting}=https://github.com/", operation]
            if operation == "push":
                command += ["--dry-run", "https://github.com/alice/repo", "HEAD"]
            else:
                command += ["https://github.com/alice/repo"]
            attempt = subprocess.run(command, capture_output=True, text=True, timeout=10)
            assert attempt.returncode != 0 and "transport 'ssh' not allowed" in attempt.stderr

        # Managers and unselected workers undo selection, including Git config and attribution.
        restored = github.unselected_github_env()
        assert restored["GH_TOKEN"] == "test-bob" and restored["GITHUB_TOKEN"] == "test-bob"
        assert restored["GIT_CONFIG_COUNT"] == "1"
        assert restored["GIT_CONFIG_VALUE_0"] == "preserved"
        assert "GIT_CONFIG_KEY_1" not in restored and "GIT_AUTHOR_EMAIL" not in restored
        assert "TAUCETI_GITHUB_ACCOUNT" not in restored
        assert "_TAUCETI_PINNED_GITHUB_ACCOUNT" not in restored
        assert "_TAUCETI_GITHUB_BASE_ENV" not in restored
        with patch.object(wm.subprocess, "Popen") as spawn:
            wm._launch_runner(wm.WorkerSpec(id="unset"), root / "state", root / "runtime")
            assert spawn.call_args.kwargs["env"]["GH_TOKEN"] == "test-bob"
            assert "TAUCETI_GITHUB_ACCOUNT" not in spawn.call_args.kwargs["env"]

        # A real managed-run child must not reselect the dashboard's account.
        captured_env = root / "managed-env.json"
        command = [
            sys.executable,
            "-c",
            f"import os,json; open({str(captured_env)!r}, 'w').write(json.dumps(dict(os.environ)))",
        ]
        runner_env = {**os.environ, "TAUCETI_MANAGER_TEST_COMMAND": shlex.join(command)}
        subprocess.run(
            wm.self_argv(
                "_managed-run",
                "--spec",
                wm._encode_spec(wm.WorkerSpec(id="unset")),
                "--state-dir",
                root / "managed-state",
                "--runtime-dir",
                root / "managed-runtime",
            ),
            env=runner_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        managed = json.loads(captured_env.read_text())
        assert managed["GH_TOKEN"] == "test-bob"
        assert managed["GIT_CONFIG_COUNT"] == "1"
        assert "TAUCETI_GITHUB_ACCOUNT" not in managed and "GIT_AUTHOR_NAME" not in managed

        # A fresh named selection must invalidate cached me/fork/claims decisions and attribution.
        assert github.pin_github_account("bob") == "bob"
        assert github.me() == "bob"
        assert os.environ["GIT_AUTHOR_EMAIL"] == "22+bob@users.noreply.github.com"
        assert os.environ["GIT_CONFIG_COUNT"] != count
        rejected(lambda: github.pin_github_account("charlie"), "belongs to bob")
        rejected(lambda: github.pin_github_account("--alice"), "GitHub account")
        rejected(lambda: github.pin_github_account(""), "GitHub account")
        os.environ.pop("GH_TOKEN")
        rejected(lambda: github.pin_github_account("charlie"), "no GitHub credential")

    with patch.dict(os.environ, clean, clear=True):
        responses = [
            SimpleNamespace(returncode=0, stdout="test-alice", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="HTTP 503 secret-material"),
            SimpleNamespace(returncode=0, stdout='{"login":"alice","id":11}', stderr=""),
        ]
        with (
            patch.object(github.subprocess, "run", side_effect=responses),
            patch.object(github.time, "sleep") as pause,
            patch.object(github, "log") as logger,
        ):
            assert github.pin_github_account("alice") == "alice"
            pause.assert_called_once_with(github.GH_TRANSIENT_BASE)
            assert "secret-material" not in str(logger.call_args_list)

    # Concurrent workers select independent accounts; neither changes a global gh active account.
    driver = (
        "import os, subprocess; from tauceti_worker.github import pin_github_account; "
        "pin_github_account(os.environ['TEST_ACCOUNT']); "
        "print(subprocess.check_output(['git', 'credential', 'fill'], "
        "input='protocol=https\\nhost=github.com\\n\\n', text=True).strip())"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", driver], env={**clean, "TEST_ACCOUNT": name}, stdout=subprocess.PIPE, text=True
        )
        for name in ("alice", "bob")
    ]
    for name, process in zip(("alice", "bob"), processes, strict=True):
        output, _ = process.communicate(timeout=30)
        assert process.returncode == 0 and f"password=test-{name}" in output

    with patch.dict(os.environ, clean, clear=True):
        # All entry points accept selection in either documented flag position.
        for argv in (
            ["--github-account", "alice"],
            ["work", "--github-account", "alice"],
            ["--github-account", "alice", "work"],
            ["status", "--github-account", "alice"],
            ["doctor", "--github-account", "alice"],
            ["_round", "--github-account", "alice"],
            ["--github-account", "alice", "workers", "add", "foo"],
            ["workers", "add", "foo", "--github-account", "alice"],
        ):
            assert cli.build_parser().parse_args(argv).github_account == "alice"
        with patch.object(cli, "cmd_status", return_value=0):
            assert cli.main(["status", "--github-account", "alice"]) == 0
            assert os.environ["GH_TOKEN"] == "test-alice"
        assert "--github-account" in tui.launch_cmd(None, "auto", False, False)

        alice = wm.WorkerSpec.from_dict({"id": "review-alice", "github_account": "alice"}, 0)
        bob = dataclasses.replace(alice, github_account="bob")
        config = root / "workers.toml"
        wm.save_worker_specs(config, [alice])
        assert wm.load_worker_specs(config) == [alice]
        assert alice.fingerprint() != bob.fingerprint()
        args = cli.build_parser().parse_args(alice.work_argv()[len(wm.self_argv()) :])
        assert args.github_account == "alice"
        rejected(lambda: wm.WorkerSpec.from_dict({"id": "bad", "github_account": ""}, 0), "github_account")
        legacy = root / "workers.conf"
        legacy.write_text("tauceti work --loop --worker-id review-alice --github-account alice\n")
        assert wm.parse_legacy_config(legacy) == [alice]
        with patch.object(wm, "ensure_manager"):
            dashboard = wm.add_dashboard_worker(
                root / "dashboard-workers.toml",
                only="review",
                agent="auto",
                bubble=False,
                roadmap_only=None,
                roadmap_skip=None,
                github_account="alice",
            )
            assert wm.load_worker_specs(root / "dashboard-workers.toml") == [dashboard]
            assert dashboard.github_account == "alice"

        # Loop handoff carries the login alongside the inherited pinned token.
        captured = []

        def capture(tail):
            captured.extend(tail)
            raise KeyboardInterrupt

        with (
            patch.object(loop, "choose_model", return_value=("claude", {})),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", side_effect=capture),
        ):
            loop.cmd_loop(
                SimpleNamespace(github_account="alice", ignore_quota=False, bubble=False, quota_cmd=None),
                SimpleNamespace(wid="test"),
                only=["review"],
                agent="claude",
            )
        assert captured[captured.index("--github-account") + 1] == "alice"

        # A shared proxy must advertise account support even for review-only rounds.
        opts = SimpleNamespace(work_model="claude", only=["review"], sandbox_host=False, dry_run=False)
        with (
            patch.object(cli, "_have", return_value=True),
            patch.object(cli, "bubble_cmd_is_disposable", return_value=False),
            patch.object(cli, "bubble_supports_github_account", return_value=False),
        ):
            rejected(lambda: cli.preflight(SimpleNamespace(), opts), "account-aware Bubble")
        with patch.object(agents, "_bubble_open_help", return_value="--github-account LOGIN"):
            assert agents.bubble_supports_github_account()
        with patch.object(agents, "_bubble_open_help", return_value="--no-github-account"):
            assert not agents.bubble_supports_github_account()

        # Account-selected review must check the daemon without probing build artifacts.
        with (
            patch.object(cli, "_have", return_value=True),
            patch.object(cli, "bubble_cmd_is_disposable", return_value=False),
            patch.object(cli, "bubble_supports_github_account", return_value=True),
            patch.object(cli, "installed_bubble_version", return_value="0.7.31"),
            patch.object(cli, "ensure_fork_proxy_current") as proxy_check,
            patch.object(cli, "tauceti_cache_unreachable_reason", side_effect=AssertionError("review cannot build")),
        ):
            cli.preflight(SimpleNamespace(), opts)
            proxy_check.assert_called_once()

        # Check the daemon's live capability, including a stale endpoint after a CLI upgrade.
        endpoint = root / ".bubble" / "auth-proxy.endpoint"
        endpoint.parent.mkdir()
        metadata = {"tcp": {"host": "127.0.0.1", "port": 7654}, "pid": os.getpid(), "capabilities": ["allow-push"]}
        endpoint.write_text(json.dumps(metadata))
        with patch.object(agents, "_host_home", return_value=root):
            assert not agents._bubble_proxy_endpoint_healthy()
            metadata["capabilities"].append("github-account")
            endpoint.write_text(json.dumps(metadata))
            assert agents._bubble_proxy_endpoint_healthy()

print(
    "PASS: GitHub account credentials, concurrent workers, Git identity, CLI and loop handoff, persistence, Bubble gate"
)
