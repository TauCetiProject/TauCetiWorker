#!/usr/bin/env python3
"""Run and restart the actual native queue CLI on disposable Forgejo HTTPS."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_native_launch as launch_fixtures
from fixtures.forgejo_git_source import source_fixture
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forgejo

from tauceti_queue import config
from tauceti_queue.source_authority import QueueControlPolicy, control_token_fingerprint


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable native CLI startup")
class NativeCLI(unittest.TestCase):
    def test_private_profile_startup_restart_status_and_no_legacy_network(self):
        with source_fixture() as (_root, admin, repository, row, tokens):
            fixture = launch_fixtures.LaunchFiles()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            admin.request("PUT", f"repos/{repository.name}/collaborators/queue", {"permission": "admin"})
            api = Forgejo(repository.forge, token=tokens["queue"])
            reviewer = Forgejo(repository.forge, token=tokens["reviewer"])
            operator_id = Forgejo(repository.forge, token=tokens["operator"]).current_user()["id"]
            source = RepositoryIdentity(repository, row["id"])
            project = (
                launch_fixtures.CONFIG.replace(b"https://source.example", repository.forge.url.encode()).replace(
                    b"team/Math", repository.name.encode()
                )
                + b'\n[lean]\nroots=["Math"]\n'
            )
            fixture.write(fixture.project, project)
            approved = replace(
                fixture.policy,
                source=source,
                credential_origin=repository.forge,
                config_digest=hashlib.sha256(project).hexdigest(),
            )
            values = config.read_file(fixture.runtime, snapshot=fixture.runtime_data)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            values.update(
                repository=repository.name,
                listen_port=port,
                public_url=f"http://127.0.0.1:{port}",
                reconcile_interval_seconds=1,
            )
            clone = Path(values["content_clone"])
            subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
            runtime = "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()).encode()
            fixture.write(fixture.runtime, runtime)
            fixture.write(fixture.root / "api-token", tokens["queue"])
            fixture.write(fixture.root / "git-token", tokens["queue"])
            control = QueueControlPolicy(source, frozenset({control_token_fingerprint(source, tokens["operator"])}))
            fixture.write(fixture.control, json.dumps(control.record()))
            operator_file = fixture.root / "operator-control-token"
            fixture.write(operator_file, tokens["operator"])
            fixture.row.update(
                queue_configuration_sha256=hashlib.sha256(runtime).hexdigest(),
                project_configuration_sha256=hashlib.sha256(project).hexdigest(),
                execution_policy=approved.record(with_config=False),
                publishers=dict(
                    generation="2" * 32,
                    reviews=[reviewer.current_user()["id"]],
                    statuses=[api.current_user()["id"]],
                    recoveries=[api.current_user()["id"]],
                ),
            )
            fixture.row["source_credentials"]["origin"]["url"] = repository.forge.url
            fixture.save()
            blocked = fixture.root / "unexpected-network"
            bin_dir = fixture.root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text('#!/bin/sh\ntouch "' + str(blocked) + '"\nexit 99\n')
            gh.chmod(0o700)
            code = """import pathlib,sys,urllib.request
origin,blocked=sys.argv[1:3]
original=urllib.request.OpenerDirector.open
def guarded(self, request, *args, **kwargs):
 url=request.full_url if isinstance(request,urllib.request.Request) else str(request)
 if not url.startswith(origin+'/'):
  pathlib.Path(blocked).touch()
  raise AssertionError('unexpected non-source network access')
 return original(self,request,*args,**kwargs)
urllib.request.OpenerDirector.open=guarded
from tauceti_queue.__main__ import main
raise SystemExit(main(['serve','--native-launch',sys.argv[3]]))
"""
            env = dict(
                os.environ,
                PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                TAUCETI_QUEUE_REPOSITORY="wrong/repository",
                TAUCETI_QUEUE_PUBLICATION_ENABLED="true",
                TAUCETI_QUEUE_CONFIG="/missing/ambient-credentials",
                TAUCETI_GITHUB_STATE_URL="https://github-state.taucetiproject.org",
            )
            for _restart in range(2):
                process = subprocess.Popen(
                    [sys.executable, "-c", code, repository.forge.url, str(blocked), str(fixture.path)],
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 25
                    status = None
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as response:
                                status = json.load(response)
                            break
                        except (OSError, ValueError):
                            time.sleep(0.05)
                    if os.environ.get("FORGEJO_EFFECTIVE_ACCESS"):
                        self.assertIsNotNone(status, "native CLI did not become healthy")
                        self.assertEqual(status["service"]["source_repository"], source.record())
                        self.assertEqual(status["service"]["execution_repository"], approved.execution.record())
                        self.assertFalse(status["service"]["publication_enabled"])
                        self.assertFalse(status["settings"]["publication_enabled"])
                        desired = int(status["settings"]["insertion_age_window"]) + 1
                        operator = subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "tauceti_queue",
                                "settings",
                                "--url",
                                f"http://127.0.0.1:{port}",
                                "set",
                                "insertion_age_window",
                                str(desired),
                                "--token-file",
                                str(operator_file),
                            ],
                            cwd=Path(__file__).resolve().parents[1],
                            env=dict(env, GITHUB_TOKEN="wrong-github-token"),
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        self.assertEqual(operator.returncode, 0, operator.stderr[-1200:])
                        result = json.loads(operator.stdout)
                        self.assertEqual(result["actor"], f"{repository.forge.key}/users/{operator_id}")
                        self.assertEqual(result["settings"]["insertion_age_window"], desired)
                        self.assertFalse(result["settings"]["publication_enabled"])
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as response:
                            observed = json.load(response)
                        self.assertEqual(observed["settings"]["insertion_age_window"], desired)
                        fixture.write(operator_file, tokens["queue"])
                        refused_operator = subprocess.run(
                            operator.args,
                            cwd=Path(__file__).resolve().parents[1],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        self.assertEqual(refused_operator.returncode, 1)
                        fixture.write(operator_file, tokens["operator"])
                        for token in tokens.values():
                            self.assertNotIn(token, operator.stdout + operator.stderr)
                            self.assertNotIn(token, refused_operator.stdout + refused_operator.stderr)
                        for token in tokens.values():
                            self.assertNotIn(token, json.dumps(status))
                        self.assertIsNone(status["feeds"])
                        with self.assertRaises(urllib.error.HTTPError) as refused:
                            urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/repositories/{source.native_id}/active-batches", timeout=5
                            )
                        self.assertEqual(refused.exception.code, 503)
                        refused.exception.close()
                        process.terminate()
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 0, stderr[-1200:])
                    else:
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 1)
                        self.assertIsNone(status, "stock Forgejo must not activate the native controller")
                    self.assertFalse(blocked.exists(), "native startup used legacy network or gh credential discovery")
                    for token in tokens.values():
                        self.assertNotIn(token, stdout + stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
