"""Ordinary Forgejo Git transport for the source side of the queue.

The clone is a trusted controller object store, never a candidate checkout. API
identity checks detect changed repository bindings; repository rename/deletion is
an operator operation and must not overlap publication. Promotion uses Git's
explicit expected-old lease after local ancestry validation. This component does
not decide review authority or CI success.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, ForgeError, Incomplete

from .ci_requests import FrozenRequest
from .ports import FastForwardResult

SHA = re.compile(r"[0-9a-f]{40}")


class ForgejoGitSource:
    """Git operations composed into the native source adapter, without CI credentials."""

    def __init__(self, identity, client, clone, *, token, timeout=120):
        if not isinstance(identity, RepositoryIdentity) or identity.repository.forge.kind != "forgejo":
            raise ValueError("Forgejo Git requires a qualified native repository")
        if client.forge != identity.repository.forge:
            raise ValueError("Git/API credential origins differ")
        if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
            raise ValueError("an explicit source Git bearer is required")
        if type(timeout) not in (int, float) or not 0 < timeout <= 600:
            raise ValueError("Git timeout must be bounded")
        self.repository_identity, self.client = identity, client
        self.repository = identity.repository
        self.clone = str(Path(clone).resolve(strict=True))
        self.timeout, self._token = timeout, token
        self._lock = threading.RLock()
        executable = shutil.which("git")
        if not executable:
            raise OSError("Git is unavailable")
        self.command = [
            executable,
            "--no-replace-objects",
            "-C",
            self.clone,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "protocol.allow=never",
            "-c",
            f"protocol.{self.repository.forge.url.split(':', 1)[0]}.allow=always",
        ]

    def _environment(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "SSL_CERT_FILE", "GIT_SSL_CAINFO"}
        }
        auth = base64.b64encode(f"x-access-token:{self._token}".encode("ascii")).decode("ascii")
        env.update(
            LC_ALL="C",
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_NO_LAZY_FETCH="1",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1=f"http.{self.repository.clone_url}.extraHeader",
            GIT_CONFIG_VALUE_1=f"Authorization: Basic {auth}",
        )
        return env

    def _git(self, *args, write=False, check=True):
        process = None

        def stop():
            if process is not None:
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

        try:
            process = subprocess.Popen(
                [*self.command, *args],
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=self.timeout)
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired):
            stop()
            error = AmbiguousWrite if write else ForgeError
            raise error(
                "source Git transport failed; reconcile before retrying publication"
                if write
                else "source Git operation failed"
            ) from None
        except BaseException:
            stop()
            raise
        if check and result.returncode:
            error = AmbiguousWrite if write else ForgeError
            raise error(
                "source Git publication was refused or its outcome is uncertain"
                if write
                else "source Git operation failed"
            )
        return result

    def _bound(self, repo):
        if repo not in (self.repository.name, self.repository.key):
            raise ValueError("source Git repository differs from its trusted binding")
        row = self.client.repository(self.repository)
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] != self.repository_identity.native_id
            or not isinstance(row.get("full_name"), str)
            or row["full_name"].casefold() != self.repository.name.casefold()
        ):
            raise Incomplete("native Git repository identity changed")

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or SHA.fullmatch(value) is None or value == "0" * 40:
            raise ValueError("source Git requires a nonzero immutable commit")
        return value

    def _ref(self, value):
        if (
            not isinstance(value, str)
            or not value.startswith("refs/")
            or self._git("check-ref-format", value, check=False).returncode
        ):
            raise ValueError("source Git requires a complete ref name")
        return value

    def _commit(self, value):
        self._sha(value)
        if self._git("cat-file", "-t", value).stdout != b"commit\n":
            raise ValueError("source Git object is not a commit")

    @staticmethod
    def _remote_ref(ref):
        # The queue keeps its existing local refs. On Forgejo, expose retention
        # as ordinary protected branches; custom refs have no protection rules.
        if ref.startswith("refs/tauceti-queue/"):
            return "refs/heads/tauceti-ci/queue/" + ref.removeprefix("refs/tauceti-queue/")
        return ref

    def ref_sha(self, repo, ref):
        ref = self._ref(ref if ref.startswith("refs/") else "refs/" + ref)
        return self._remote_refs(repo, [ref])[ref]

    def _remote_refs(self, repo, refs):
        refs = tuple(self._ref(ref if ref.startswith("refs/") else "refs/" + ref) for ref in refs)
        remote = {self._remote_ref(ref): ref for ref in refs}
        if not refs or len(refs) > 4096 or len(remote) != len(refs):
            raise ValueError("source ref lookup needs bounded distinct refs")
        groups = {}
        for ref in remote:
            # Only a single immutable CI request has a bounded shared namespace.
            # Queue outbox batches can span years of input/batch refs; read those
            # by their exact names instead of listing a growing queue prefix.
            prefix = ref
            if re.fullmatch(
                r"refs/heads/tauceti-ci/[0-9a-f]{32}/[0-9a-f]{32}/(?:candidate|members|member-[1-9][0-9]*)", ref
            ):
                prefix = ref.rsplit("/", 1)[0]
            groups.setdefault(prefix, []).append(ref)
        with self._lock:
            self._bound(repo)
            found = dict.fromkeys(refs)
            for prefix, targets in groups.items():
                try:
                    rows = self.client.request(
                        "GET", f"repos/{self.repository.name}/git/refs/" + quote(prefix.removeprefix("refs/"), safe="/")
                    )
                except ForgeError as error:
                    if error.status != 404:
                        raise
                    rows = []
                if isinstance(rows, dict):
                    if rows.get("ref") not in targets:
                        raise Incomplete("source ref response identifies a different ref")
                    rows = [rows]
                if not isinstance(rows, list):
                    raise Incomplete("source ref listing is unavailable")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
                        raise Incomplete("source ref listing is malformed")
                    if row["ref"] not in targets:
                        continue
                    logical = remote[row["ref"]]
                    if found[logical] is not None or not isinstance(row.get("object"), dict):
                        raise Incomplete("source ref listing is ambiguous")
                    found[logical] = self._sha(row["object"].get("sha"))
            self._bound(repo)
            return found

    def fetch_refs(self, repo, refspecs):
        refspecs = tuple(refspecs)
        if not refspecs or len(refspecs) > 4096:
            raise ValueError("source fetch needs bounded explicit refspecs")
        for spec in refspecs:
            if not isinstance(spec, str):
                raise ValueError("invalid source refspec")
            if SHA.fullmatch(spec):
                self._sha(spec)
            else:
                parts = spec.removeprefix("+").split(":")
                if len(parts) != 2:
                    raise ValueError("source fetch requires explicit refs or commit IDs")
                for ref in parts:
                    self._ref(ref)
        remote_specs = []
        for spec in refspecs:
            if SHA.fullmatch(spec):
                remote_specs.append(spec)
            else:
                source, destination = spec.removeprefix("+").split(":")
                remote_specs.append(
                    ("+" if spec.startswith("+") else "") + self._remote_ref(source) + ":" + destination
                )
        with self._lock:
            self._bound(repo)
            self._git("fetch", "--no-tags", "--no-recurse-submodules", self.repository.clone_url, *remote_specs)
            self._bound(repo)

    def update_ref(self, repo, ref, sha):
        self.update_refs(repo, [(ref, sha)])

    def update_refs(self, repo, updates):
        """Create immutable retained refs atomically; an existing different tip is refused."""
        updates = tuple(updates)
        if not updates or len(updates) > 4096:
            raise ValueError("source retention needs bounded updates")
        normalized = []
        for ref, sha in updates:
            ref = self._ref(ref if ref.startswith("refs/") else "refs/" + ref)
            if not ref.startswith(("refs/tauceti-queue/", "refs/heads/tauceti-ci/")):
                raise ValueError("source retention cannot update ordinary branches")
            self._commit(sha)
            normalized.append((self._remote_ref(ref), sha))
        if len({ref for ref, _ in normalized}) != len(normalized):
            raise ValueError("duplicate retained ref")
        with self._lock:
            pending = []
            observed = self._remote_refs(repo, [ref for ref, _ in normalized])
            for ref, sha in normalized:
                current = observed[ref]
                if current not in (None, sha):
                    raise ForgeError("retained source ref already names another commit")
                if current is None:
                    pending.append((ref, sha))
            try:
                if pending:
                    self._git(
                        "push",
                        "--atomic",
                        "--porcelain",
                        *(f"--force-with-lease={ref}:" for ref, _ in pending),
                        self.repository.clone_url,
                        *(f"{sha}:{ref}" for ref, sha in pending),
                        write=True,
                    )
            except AmbiguousWrite:
                # A lost acknowledgement or another controller creating identical
                # refs is settled only by observing the exact complete target set.
                try:
                    if self._remote_refs(repo, [ref for ref, _ in normalized]) == dict(normalized):
                        return
                except (ForgeError, ValueError):
                    pass
                raise
            try:
                if self._remote_refs(repo, [ref for ref, _ in normalized]) == dict(normalized):
                    return
            except (ForgeError, ValueError):
                pass
            raise AmbiguousWrite("retained source refs could not be reconciled to their expected commits")

    def retain_candidate(self, frozen):
        if (
            not isinstance(frozen, FrozenRequest)
            or frozen.policy.source != self.repository_identity
            or frozen.request.source != self.repository_identity
        ):
            raise ValueError("retained candidate belongs to another source")
        request = frozen.request
        with self._lock:
            for sha in (request.head, request.base, *request.accepted_heads):
                self._commit(sha)
            tree = self._git("rev-parse", request.head + "^{tree}").stdout.decode("ascii").strip()
            if (
                tree != request.tree
                or self._git("merge-base", "--is-ancestor", request.base, request.head, check=False).returncode
            ):
                raise ValueError("candidate tree or base differs from frozen request")
            self.update_refs(self.repository.name, frozen.retained_refs())

    def fast_forward(self, repo, branch, expected_parent, tip):
        """Publish one descendant using an explicit lease, preserving server protection hooks."""
        ref = self._ref("refs/heads/" + branch)
        if branch.startswith("tauceti-ci/"):
            raise ValueError("candidate refs are not promotion branches")
        with self._lock:
            self._commit(expected_parent)
            self._commit(tip)
            ancestry = self._git("merge-base", "--is-ancestor", expected_parent, tip, check=False)
            if ancestry.returncode == 1:
                return FastForwardResult(False, None, "candidate is not a descendant of the expected parent")
            if ancestry.returncode:
                raise ForgeError("source ancestry could not be verified")
            current = self.ref_sha(repo, ref)
            if current == tip:
                return FastForwardResult(True, expected_parent)
            if current != expected_parent:
                return FastForwardResult(False, current, "parent changed before the push")
            result = self._git(
                "push",
                "--porcelain",
                f"--force-with-lease={ref}:{expected_parent}",
                self.repository.clone_url,
                f"{tip}:{ref}",
                write=True,
                check=False,
            )
            explicit_refusal = False
            for line in result.stdout.splitlines():
                fields = line.split(b"\t")
                if len(fields) != 3 or fields[1].split(b":", 1)[-1] != ref.encode("utf-8"):
                    continue
                if fields[0] in (b"*", b" ", b"=") and result.returncode == 0:
                    # The server acknowledged the exact leased update. Another
                    # writer may advance the branch before a later observation.
                    return FastForwardResult(True, expected_parent)
                explicit_refusal |= fields[0] == b"!" and fields[2].startswith((b"[rejected]", b"[remote rejected]"))
            try:
                actual = self.ref_sha(repo, ref)
            except (ForgeError, ValueError):
                raise AmbiguousWrite("source promotion outcome could not be observed") from None
            if actual == tip:
                return FastForwardResult(True, expected_parent)
            if explicit_refusal:
                return FastForwardResult(False, actual, "source lease or branch protection refused publication")
            raise AmbiguousWrite("source promotion has no definitive acknowledgement; reconcile before retry")
