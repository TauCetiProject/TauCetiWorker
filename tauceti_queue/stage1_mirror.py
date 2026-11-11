"""Mirror authoritative Forgejo main with a repository-scoped GitHub App token."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import urllib.request
from pathlib import Path

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable, NoRedirect

from . import github_app
from .ci_execution import ExecutionEvidence
from .github_adapter import UrllibTransport

PERMISSIONS = {"contents": "write", "workflows": "write"}
# Validated Lake/Mathlib/toolchain bumps remain governed by the existing source
# candidate attestation and bump verdict. These paths control execution itself.
CONTROL_PATHS = (".github", ".tauceti", "tauceti.toml", "scripts")


class MirrorEvidence(ExecutionEvidence):
    def _url(self, path):
        super()._url(path)  # Keep the existing relative-path validation.
        return cc.GITHUB.api_url + f"/repositories/{self.policy.execution.native_id}" + path


class MainMirror:
    """Publish only main; refuse divergence and reconcile acknowledgements by reading refs."""

    def __init__(self, policy, clone, credentials_dir, *, tokens=None, reader_factory=None):
        if not isinstance(policy, cc.ExecutionPolicy) or (
            policy.source.repository.forge.kind != "forgejo"
            or policy.execution.repository.forge != cc.GITHUB
            or policy.base_branch != "main"
            or policy.execution_branch != "main"
        ):
            raise ValueError("Stage 1 main mirroring requires explicit Forgejo main and GitHub main identities")
        self.policy, self.clone = policy, str(Path(clone).resolve(strict=True))
        if tokens is None:
            credentials = github_app.load_execution_credentials(Path(credentials_dir))
            tokens = github_app.InstallationTokenSource(
                credentials, UrllibTransport(), repository_id=policy.execution.native_id, permissions=PERMISSIONS
            )
        if (
            not isinstance(tokens, github_app.InstallationTokenSource)
            or tokens.repository_id != policy.execution.native_id
            or tokens.permissions != PERMISSIONS
            or tokens._api != github_app.API
        ):
            raise ValueError("main mirror requires its exact GitHub repository-ID-scoped App role")
        self.tokens = tokens
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=github_app.ssl_context()), NoRedirect()
        )
        self.reader_factory = reader_factory or (
            lambda token: MirrorEvidence(policy, token=token, token_origin=cc.GITHUB, opener=opener)
        )

    @classmethod
    def local_git(cls, clone, *arguments):
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_GRAFT_FILE="/dev/null",
        )
        return cls._run_git(
            ["git", "--no-replace-objects", "-C", str(clone), "-c", "core.fsmonitor=false", *arguments], env=env
        )

    @classmethod
    def check_controls(cls, clone, approved, candidate):
        if any(cls.local_git(clone, "cat-file", "-e", sha + "^{commit}").returncode for sha in (approved, candidate)):
            raise ValueError("mirror control comparison needs both immutable commits")
        result = cls.local_git(
            clone, "diff", "--no-ext-diff", "--no-textconv", "--name-only", approved, candidate, "--", *CONTROL_PATHS
        )
        if result.returncode:
            raise EvidenceUnavailable("mirror control comparison is unavailable")
        if result.stdout:
            raise ValueError("execution control files changed; deploy and approve their new caller explicitly")

    def _ensure_objects(self, source, *commits):
        if (
            getattr(source, "repository_identity", None) != self.policy.source
            or str(Path(source.clone).resolve()) != self.clone
        ):
            raise ValueError("mirror source differs from its approved identity or trusted clone")
        missing = [
            sha for sha in set(commits) if self.local_git(self.clone, "cat-file", "-e", sha + "^{commit}").returncode
        ]
        if missing:
            source.fetch_refs(self.policy.source.repository.key, missing)

    def _reader(self):
        reader = self.reader_factory(self._token())
        metadata = reader.repository()
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("id")) is not int
            or metadata["id"] != self.policy.execution.native_id
            or metadata.get("full_name") != self.policy.execution.repository.name
            or metadata.get("default_branch") != "main"
        ):
            raise ValueError("GitHub mirror repository identity or default branch differs from policy")
        return reader

    def _token(self):
        try:
            return self.tokens.token()
        except github_app.CredentialsError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError):
            raise EvidenceUnavailable("mirror App token response is incomplete") from None
        except ValueError:
            raise ValueError("mirror App token response is invalid; inspect credential configuration") from None

    def preflight(self, source, base, candidate, approved_caller):
        """Refuse predictable mirror failures before changing authoritative source main."""
        self._ensure_objects(source, base, candidate, approved_caller)
        self.check_controls(self.clone, approved_caller, candidate)
        if getattr(source, "repository_identity", None) != self.policy.source:
            raise ValueError("mirror preflight belongs to another source")
        if source.ref_sha(self.policy.source.repository.key, "refs/heads/main") != base:
            raise ValueError("source main changed before mirror preflight")
        if self._head(self._reader()) != base:
            raise ValueError("repair the main mirror before another source promotion")

    def confirm_bootstrap(self, source, head):
        if getattr(source, "repository_identity", None) != self.policy.source:
            raise ValueError("mirror bootstrap belongs to another source")
        if source.ref_sha(self.policy.source.repository.key, "refs/heads/main") != head:
            raise EvidenceUnavailable("source main changed during bootstrap confirmation")
        if self._head(self._reader()) != head:
            raise ValueError("initial/repinned main must already match; finish the reviewed deployment explicitly")
        if source.ref_sha(self.policy.source.repository.key, "refs/heads/main") != head:
            raise EvidenceUnavailable("source main changed during bootstrap confirmation")
        return {
            "source": self.policy.source.record(),
            "execution": self.policy.execution.record(),
            "main": head,
            "changed": False,
            "bootstrap": True,
        }

    def _git(self, token, *arguments):
        # Use the existing Git transport's explicit child-environment pattern.
        # Source credentials and ambient helpers never enter this child, and a
        # local generic header is reset before the exact GitHub repository role.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "SSL_CERT_FILE", "GIT_SSL_CAINFO") if key in os.environ}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_NO_LAZY_FETCH="1",
            GIT_ATTR_NOSYSTEM="1",
        )
        encoded = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
        settings = [
            ("http.extraheader", ""),
            (f"http.{self.policy.execution.repository.clone_url}.extraheader", "AUTHORIZATION: basic " + encoded),
            ("credential.helper", ""),
            ("core.hooksPath", "/dev/null"),
            ("core.attributesFile", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("http.followRedirects", "false"),
            ("http.sslVerify", "true"),
            ("fetch.fsckObjects", "true"),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
        ]
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        try:
            result = self._run_git(
                ["git", "--no-replace-objects", "-C", self.clone, *arguments],
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise EvidenceUnavailable(
                "main mirror Git transport unavailable; reconcile remote main before retry"
            ) from None
        if result.returncode:
            refused = [line for line in result.stdout.splitlines() if line.startswith(b"!\t")]
            if arguments[0] == "push" and refused:
                reason = refused[0].split(b"\t", 2)[-1].decode("utf-8", errors="replace")[:200]
                raise ValueError("GitHub refused the main update: " + reason)
            # No credential-bearing subprocess environment/stderr enters logs.
            raise EvidenceUnavailable("main mirror Git operation failed; reconcile exact remote refs before retry")

    @staticmethod
    def _run_git(command, *, env, timeout=120):
        process = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise

    @staticmethod
    def _head(reader):
        value = reader.get("/git/ref/heads/main")
        if not isinstance(value, dict) or not isinstance(value.get("object"), dict):
            raise EvidenceUnavailable("GitHub main identity is missing or malformed")
        obj = value["object"]
        sha = obj.get("sha")
        if (
            value.get("ref") != "refs/heads/main"
            or obj.get("type") != "commit"
            or (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha == "0" * 40)
        ):
            raise EvidenceUnavailable("GitHub main identity is missing or malformed")
        return sha

    def publish(self, source, expected_head, approved_caller):
        if getattr(source, "repository_identity", None) != self.policy.source or (
            str(Path(source.clone).resolve()) != self.clone
        ):
            raise ValueError("mirror source differs from its approved identity or trusted clone")
        if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise ValueError("mirror requires a full immutable source main revision")
        repository = self.policy.source.repository.key

        def current():
            if source.ref_sha(repository, "refs/heads/main") != expected_head:
                raise EvidenceUnavailable("source main advanced; mirror its newly observed tip instead")

        current()
        self._ensure_objects(source, expected_head, approved_caller)
        self.check_controls(self.clone, approved_caller, expected_head)
        token = self._token()
        reader = self._reader()
        old = self._head(reader)
        current()
        if old != expected_head:
            if self.local_git(self.clone, "cat-file", "-e", old + "^{commit}").returncode:
                self._git(
                    token,
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    self.policy.execution.repository.clone_url,
                    old,
                )
            ancestry = self.local_git(self.clone, "merge-base", "--is-ancestor", old, expected_head)
            if ancestry.returncode not in (0, 1):
                raise EvidenceUnavailable("mirror ancestry is unavailable")
            if ancestry.returncode:
                raise ValueError("GitHub main diverged or is ahead; preserve both histories and reconcile authority")
            current()
            self._git(
                token,
                "push",
                "--porcelain",
                "--no-follow-tags",
                "--no-recurse-submodules",
                "--no-verify",
                f"--force-with-lease=refs/heads/main:{old}",
                self.policy.execution.repository.clone_url,
                f"{expected_head}:refs/heads/main",
            )
        if self._head(reader) != expected_head:
            raise EvidenceUnavailable("GitHub main does not yet confirm the source tip; mirror needs recovery")
        current()
        return {
            "source": self.policy.source.record(),
            "execution": self.policy.execution.record(),
            "main": expected_head,
            "changed": old != expected_head,
        }
