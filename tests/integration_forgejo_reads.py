#!/usr/bin/env python3
"""Disposable real Forgejo -> Worker ReviewState, with shared-service access blocked.

Set FORGEJO_BINARY to run. Every repository, credential, cookie and server process
belongs to a temporary loopback-only instance; no installed worker is executed.
"""

import base64
import contextlib
import getpass
import importlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forge, ForgeError, Forgejo, RepositoryRef

from tauceti_queue.source_authority import NativeQueueAuthority, SourceAccessDenied
from tauceti_worker.forgejo_reads import BoundedReadCache
from tauceti_worker.github import GitHub
from tauceti_worker.review_state import ReviewState


class Integration(unittest.TestCase):
    def test_real_source_with_service_and_github_fallback_blocked(self):
        binary = os.environ.get("FORGEJO_BINARY")
        effective_access = os.environ.get("FORGEJO_EFFECTIVE_ACCESS") == "1"
        https = os.environ.get("FORGEJO_HTTPS") == "1"
        if not binary:
            self.skipTest("set FORGEJO_BINARY to run a disposable loopback server")
        with tempfile.TemporaryDirectory(prefix="worker-forgejo-") as temporary, contextlib.ExitStack() as environment:
            root = Path(temporary)
            tls_settings = ""
            if https:
                certificate, key = root / "source.crt", root / "source.key"
                result = subprocess.run(
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
                        str(certificate),
                        "-subj",
                        "/CN=source-fixture.invalid",
                        "-config",
                        "/dev/null",
                        "-addext",
                        "subjectAltName=IP:127.0.0.1",
                        "-addext",
                        "basicConstraints=critical,CA:TRUE",
                        "-addext",
                        "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
                        "-addext",
                        "extendedKeyUsage=serverAuth",
                    ],
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, "local TLS certificate generation failed")
                key.chmod(0o600)
                environment.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "SSL_CERT_FILE": str(certificate),
                            "GIT_SSL_CAINFO": str(certificate),
                        },
                    )
                )
                tls_settings = f"PROTOCOL = https\nCERT_FILE = {certificate}\nKEY_FILE = {key}\n"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = f"{'https' if https else 'http'}://127.0.0.1:{port}"
            config = root / "app.ini"
            config.write_text(f"""RUN_USER = {getpass.getuser()}
WORK_PATH = {root}
[server]
{tls_settings}
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = {origin}/
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
            admin(
                "admin",
                "user",
                "create",
                "--username",
                "collaborator",
                "--email",
                "collaborator@example.invalid",
                "--random-password",
                "--must-change-password=false",
            )

            def token(name, scopes, username="operator"):
                output = admin(
                    "admin",
                    "user",
                    "generate-access-token",
                    "--username",
                    username,
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
            readonly = Forgejo(api.forge, token=token("repo-reader", "read:user,read:repository"), timeout=3)
            collaborator = Forgejo(api.forge, token=token("member", "all", "collaborator"), timeout=3)
            collaborator_control = token("queue-control", "write:repository", "collaborator")
            probe = Forgejo(api.forge, timeout=3)
            with (root / "server.log").open("w") as log:
                process = subprocess.Popen([*base, "web"], stdout=log, stderr=subprocess.STDOUT)
                try:

                    def await_source():
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                probe.request("GET", "version")
                                return
                            except ForgeError:
                                if process.poll() is not None or time.monotonic() >= deadline:
                                    self.fail("local Forgejo did not start; private log retained only within fixture")
                                time.sleep(0.05)

                    await_source()
                    if https:
                        # Probe real TLS before sending any bearer: neither an
                        # untrusted certificate nor a hostname mismatch is accepted.
                        for context, hostname in (
                            (ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "127.0.0.1"),
                            (ssl.create_default_context(cafile=str(certificate)), "wrong-fixture.invalid"),
                        ):
                            with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
                                with self.assertRaises(ssl.SSLCertVerificationError):
                                    with context.wrap_socket(connection, server_hostname=hostname):
                                        pass
                        # The actual client must reject an untrusted CA too.
                        empty_ca = root / "empty-ca.pem"
                        empty_ca.write_text("")
                        with patch.dict(os.environ, {"SSL_CERT_FILE": str(empty_ca)}):
                            with self.assertRaises(ForgeError):
                                # A fresh opener obtains its own trust context;
                                # existing clients may retain the trusted one.
                                Forgejo(api.forge, timeout=3).request("GET", "version")
                        probe.request("GET", "version")
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
                    api.request(
                        "POST",
                        "repos/operator/source/branches",
                        {
                            "new_branch_name": "second-proof",
                            "old_branch_name": "main",
                        },
                    )
                    api.request(
                        "POST",
                        "repos/operator/source/contents/Second.lean",
                        {
                            "branch": "second-proof",
                            "message": "Second proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    second = api.request(
                        "POST",
                        "repos/operator/source/pulls",
                        {
                            "head": "second-proof",
                            "base": "main",
                            "title": "Second proof",
                        },
                    )
                    cfg = SimpleNamespace(sbcache=root / "review-state", wid="native-source-survey")
                    shared_cache = BoundedReadCache(max_entries=4, max_bytes=64 * 1024)
                    source = GitHub.for_repository(repo, api=api, cache=shared_cache)
                    other = GitHub.for_repository(repo, api=restricted, cache=shared_cache)
                    reader = GitHub.for_repository(repo, api=readonly, cache=shared_cache)
                    member = GitHub.for_repository(repo, api=collaborator, cache=shared_cache)
                    with (
                        patch(
                            "tauceti_worker.github_state.read", side_effect=AssertionError("shared service consulted")
                        ),
                        patch(
                            "tauceti_worker.review_state.github_state_enabled",
                            side_effect=AssertionError("shared service selected"),
                        ),
                        patch("tauceti_worker.github.gh_run", side_effect=AssertionError("native GitHub CLI fallback")),
                        patch(
                            "tauceti_worker.review_state.invalidate",
                            side_effect=AssertionError("GitHub invalidation fallback"),
                        ),
                        patch(
                            "tauceti_worker.github_transport.shared_read",
                            side_effect=AssertionError("GitHub read-cache fallback"),
                        ),
                    ):
                        state = ReviewState(cfg, source)
                        state.observe([SimpleNamespace(number=number, updated_at=pull["updated_at"])])
                        self.assertEqual(state.gh_meta(number).provenance, "fresh")
                        self.assertEqual(ReviewState(cfg, source).gh_meta(number).provenance, "assumed")
                        survey_module = importlib.import_module("tauceti_worker.survey")
                        with (
                            patch(
                                "tauceti_worker.github_state.read_survey", side_effect=AssertionError("survey service")
                            ),
                            patch.object(survey_module, "me", side_effect=AssertionError("GitHub login")),
                            patch.object(survey_module, "can_push", side_effect=AssertionError("GitHub permission")),
                        ):
                            inventory = survey_module.survey(
                                cfg, source, state, None, deep=False, selected_roadmap="any"
                            )
                        self.assertFalse(inventory.github_failed)
                        self.assertEqual({p.number for p in inventory.open_prs}, {number, second["number"]})
                        self.assertTrue(all(inventory.owns(p) for p in inventory.open_prs))
                        self.assertEqual(inventory.n_reviewable, 0)
                        self.assertIsNone(inventory.next_auto_stage)
                        # A one-page client cannot claim this two-PR inventory is complete.
                        capped_api = Forgejo(api.forge, token=api._token, max_pages=1)
                        capped = GitHub.for_repository(repo, api=capped_api)
                        self.assertIsNone(capped.open_prs())
                        self.assertEqual(capped.last_failure.kind, "Incomplete")
                        actor = source.authenticated_actor()
                        self.assertEqual(actor.login, "operator")
                        self.assertEqual(actor.principal.forge, api.forge)
                        self.assertEqual(actor.principal.native_id, str(pull["user"]["id"]))
                        self.assertEqual(other.authenticated_actor(), actor)
                        access = source.repository_access()
                        self.assertEqual(access.actor, actor)
                        self.assertTrue(access.user_code_can_push)
                        self.assertTrue(access.user_is_repo_admin)
                        queue_authority = NativeQueueAuthority(RepositoryIdentity(repo, access.repository_id))

                        def deny_queue_admin(bearer):
                            if effective_access:
                                # Observation must succeed on the private build;
                                # an unsupported route or scope error is a failure.
                                observation = queue_authority.observe(bearer)
                                with self.assertRaises(SourceAccessDenied):
                                    queue_authority.require_admin(observation)
                                return observation
                            with self.assertRaises(SourceAccessDenied):
                                queue_authority.observe(bearer)

                        if effective_access:
                            queue_authority.require_admin(
                                queue_authority.validate(queue_authority.authenticate(api._token))
                            )
                        else:
                            # Stock Forgejo lacks effective scope evidence. User
                            # ACLs remain available for surveys, never controls.
                            with self.assertRaises(SourceAccessDenied):
                                queue_authority.observe(api._token)
                        missing_id = NativeQueueAuthority(RepositoryIdentity(repo, access.repository_id + 1))
                        with self.assertRaises(SourceAccessDenied):
                            missing_id.observe(api._token)
                        self.assertIsNone(other.repository_access())
                        self.assertIn(other.last_failure.status, {401, 403, 404})
                        read_access = reader.repository_access()
                        self.assertEqual(read_access.actor, actor)
                        self.assertTrue(read_access.user_code_can_push)
                        readonly_control = deny_queue_admin(readonly._token)
                        if effective_access:
                            self.assertTrue(readonly_control.user_code_can_push)
                            self.assertTrue(readonly_control.user_is_repo_admin)
                            self.assertEqual(
                                (
                                    readonly_control.credential_code_can_read,
                                    readonly_control.credential_code_can_push,
                                    readonly_control.credential_is_repo_admin,
                                ),
                                (True, False, False),
                            )
                        print("Read-only token reports native push ACL:", read_access.user_code_can_push, flush=True)
                        with self.assertRaises(ForgeError) as denied_write:
                            readonly.request(
                                "POST",
                                "repos/operator/source/branches",
                                {"new_branch_name": "must-not-write", "old_branch_name": "main"},
                            )
                        self.assertEqual(denied_write.exception.status, 403)
                        member_actor = member.authenticated_actor()
                        self.assertEqual(member_actor.login, "collaborator")
                        self.assertNotEqual(member_actor, actor)
                        self.assertFalse(collaborator.request("GET", "user")["is_admin"])
                        self.assertIsNone(member.repository_access())
                        self.assertEqual(member.last_failure.status, 404)
                        with self.assertRaises(SourceAccessDenied):
                            queue_authority.observe(collaborator._token)
                        api.request("PUT", "repos/operator/source/collaborators/collaborator", {"permission": "read"})
                        member_read = member.repository_access()
                        deny_queue_admin(collaborator_control)
                        self.assertEqual(member_read.actor, member_actor)
                        self.assertEqual(
                            (
                                member_read.user_code_can_read,
                                member_read.user_code_can_push,
                                member_read.user_is_repo_admin,
                            ),
                            (True, False, False),
                        )
                        api.request("PUT", "repos/operator/source/collaborators/collaborator", {"permission": "write"})
                        member_write = member.repository_access()
                        deny_queue_admin(collaborator_control)
                        self.assertEqual(member_write.actor, member_actor)
                        self.assertEqual(
                            (
                                member_write.user_code_can_read,
                                member_write.user_code_can_push,
                                member_write.user_is_repo_admin,
                            ),
                            (True, True, False),
                        )
                        api.request("PUT", "repos/operator/source/collaborators/collaborator", {"permission": "admin"})
                        if effective_access:
                            # No site-admin privilege and no read:user scope:
                            # current repository role plus narrow write scope.
                            narrowed = queue_authority.observe(collaborator_control)
                            self.assertEqual(narrowed.actor, member_actor)
                            self.assertTrue(narrowed.credential_code_can_push)
                            self.assertTrue(narrowed.credential_is_repo_admin)
                            queue_authority.require_admin(narrowed)
                        api.request("DELETE", "repos/operator/source/collaborators/collaborator")
                        self.assertIsNone(member.repository_access())
                        self.assertEqual(member.last_failure.status, 404)
                        self.assertEqual(member.authenticated_actor(), member_actor)
                        with self.assertRaises(SourceAccessDenied):
                            queue_authority.observe(collaborator._token)
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
                        before_edit = api.pull(repo, number)["updated_at"]
                        state.observe([SimpleNamespace(number=number, updated_at=before_edit)])
                        self.assertEqual(state.gh_meta(number, force=True).data["head_sha"], "a" * 40)
                        time.sleep(1.05)  # Cross native second-resolution timestamps.
                        api.edit_comment(repo, comment["id"], board("b" * 40))
                        after_edit = api.pull(repo, number)["updated_at"]
                        print("Comment edit moved PR.updated_at:", before_edit != after_edit, flush=True)
                        state.observe([SimpleNamespace(number=number, updated_at=after_edit)])
                        triage = state.gh_meta(number)
                        if before_edit == after_edit:
                            self.assertEqual(triage.provenance, "assumed")
                            self.assertEqual(triage.data["head_sha"], "a" * 40)
                        else:
                            self.assertEqual((triage.provenance, triage.data["head_sha"]), ("fresh", "b" * 40))
                        fresh = state.gh_meta(number, force=True)
                        self.assertEqual(fresh.provenance, "fresh")
                        self.assertEqual(fresh.data["head_sha"], "b" * 40)
                        state.bust(number)
                        self.assertEqual(state.gh_meta(number).data["head_sha"], "b" * 40)
                        before_delete = api.pull(repo, number)["updated_at"]
                        time.sleep(1.05)
                        api.request("DELETE", f"repos/operator/source/issues/comments/{comment['id']}")
                        after_delete = api.pull(repo, number)["updated_at"]
                        print("Comment deletion moved PR.updated_at:", before_delete != after_delete, flush=True)
                        self.assertEqual(state.gh_meta(number, force=True).provenance, "missing")
                        self.assertEqual(
                            source.pr_view(number, ["number", "title"], fresh=True),
                            {"number": number, "title": "Proof"},
                        )
                        self.assertLessEqual(shared_cache.stats()["entries"], 4)
                        self.assertLessEqual(shared_cache.stats()["bytes"], 64 * 1024)
                        api.request("PATCH", "repos/operator/source", {"archived": True})
                        archived = source.repository_access()
                        self.assertTrue(archived.archived)
                        self.assertFalse(archived.mirror)
                        self.assertTrue(archived.user_code_can_push)
                        archive_control = deny_queue_admin(api._token)
                        if effective_access:
                            self.assertTrue(archive_control.archived)
                            self.assertFalse(archive_control.credential_code_can_push)
                        if https:
                            # Keep the source database, certificate and client
                            # objects; replace only the owned server process.
                            process.terminate()
                            process.wait(timeout=10)
                            process = subprocess.Popen([*base, "web"], stdout=log, stderr=subprocess.STDOUT)
                            await_source()
                            after_restart = source.repository_access()
                            self.assertEqual(after_restart.repository_id, access.repository_id)
                            self.assertEqual(after_restart.actor, actor)
                            self.assertTrue(after_restart.archived)
                            deny_queue_admin(api._token)
                            self.assertIsNone(member.repository_access())
                            self.assertEqual(member.last_failure.status, 404)
                            with self.assertRaises(SourceAccessDenied):
                                queue_authority.observe(collaborator_control)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
