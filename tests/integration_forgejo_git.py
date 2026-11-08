#!/usr/bin/env python3
"""Real HTTPS Git fetch, immutable retention, exact-parent promotion and server protection.

FORGEJO_BINARY=/path/to/stock/forgejo python tests/integration_forgejo_git.py
"""

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from forgejo_git_source import source_fixture
from queue_ci_requests import policy
from tauceti_tools.ci_candidate import CandidateRequest, Member, RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, ForgeError, Forgejo, Incomplete

from tauceti_queue.ci_requests import FrozenRequest
from tauceti_queue.forgejo_git import ForgejoGitSource


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable HTTPS source")
class Integration(unittest.TestCase):
    def test_source_git_with_real_protection_and_competing_publication(self):
        with source_fixture() as (root, api, repository, row, tokens):
            clone = root / "controller"
            subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
            identity = RepositoryIdentity(repository, row["id"])
            queue_api = Forgejo(repository.forge, token=tokens["queue"])
            source = ForgejoGitSource(identity, queue_api, clone, token=tokens["queue"])
            name = repository.name
            base = source.ref_sha(name, "heads/main")
            source.fetch_refs(name, ["+refs/heads/main:refs/remotes/origin/main"])
            self.assertEqual(source._git("rev-parse", "refs/remotes/origin/main").stdout.decode().strip(), base)
            tree = source._git("rev-parse", base + "^{tree}").stdout.decode().strip()

            def commit(parent, title):
                with patch.dict(
                    os.environ,
                    GIT_AUTHOR_NAME="Fixture",
                    GIT_AUTHOR_EMAIL="fixture@example.invalid",
                    GIT_COMMITTER_NAME="Fixture",
                    GIT_COMMITTER_EMAIL="fixture@example.invalid",
                ):
                    env = source._environment()
                    env.update(
                        {
                            key: os.environ[key]
                            for key in (
                                "GIT_AUTHOR_NAME",
                                "GIT_AUTHOR_EMAIL",
                                "GIT_COMMITTER_NAME",
                                "GIT_COMMITTER_EMAIL",
                            )
                        }
                    )
                    result = subprocess.run(
                        [*source.command, "commit-tree", tree, "-p", parent, "-m", title],
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                return result.stdout.decode().strip()

            competing = commit(base, "competing update")
            tip = commit(competing, "tested candidate")
            configured = replace(policy(), source=identity, credential_origin=repository.forge)
            request = CandidateRequest(
                "a" * 32,
                "e" * 32,
                configured.digest,
                identity,
                configured.execution,
                "b" * 40,
                configured.harness_revision,
                configured.config_digest,
                1,
                1,
                tip,
                base,
                tree,
                (Member(1, tip),),
            )
            frozen = FrozenRequest(1, request, configured, b"", None, None)
            api.request(
                "POST",
                f"repos/{name}/branch_protections",
                {
                    "branch_name": "tauceti-ci/**",
                    "enable_push": True,
                    "enable_push_whitelist": True,
                    "push_whitelist_usernames": ["queue"],
                },
            )
            source.retain_candidate(frozen)
            self.assertEqual(source.ref_sha(name, request.retained_ref), tip)
            untrusted = ForgejoGitSource(
                identity,
                Forgejo(repository.forge, token=tokens["contributor"]),
                clone,
                token=tokens["contributor"],
            )
            with self.assertRaises(AmbiguousWrite):
                untrusted._git("push", repository.clone_url, f"+{base}:{request.retained_ref}", write=True)
            self.assertEqual(source.ref_sha(name, request.retained_ref), tip)
            source.retain_candidate(frozen)  # Reconcile an already accepted immutable publication.
            fresh_clone = root / "fresh-reader"
            subprocess.run(["git", "init", "-q", str(fresh_clone)], check=True, capture_output=True)
            reader = ForgejoGitSource(identity, queue_api, fresh_clone, token=tokens["queue"])
            reader.fetch_refs(name, [tip])
            reader._commit(tip)
            for refspec in (f"{tip}:refs/heads/tauceti-ci/unauthorized/new", f":{request.retained_ref}"):
                with self.assertRaises(AmbiguousWrite):
                    untrusted._git("push", repository.clone_url, refspec, write=True)
            with self.assertRaises(ForgeError):
                source.update_ref(name, request.retained_ref, base)
            with self.assertRaises(ValueError):
                source.update_ref(name, "heads/main", tip)
            with self.assertRaises(ValueError):
                source.retain_candidate(replace(frozen, request=replace(request, tree="f" * 40)))
            # A non-fast-forward must be refused without touching the server.
            self.assertFalse(source.fast_forward(name, "main", tip, base).ok)
            # Race after the read: tip includes the competing commit, so an ordinary
            # fast-forward push could incorrectly accept it. The explicit lease must refuse.
            other = ForgejoGitSource(identity, queue_api, clone, token=tokens["queue"])
            original = source._git
            raced = False

            def race(*args, **kwargs):
                nonlocal raced
                if args[0] == "push" and not raced:
                    raced = True
                    self.assertTrue(other.fast_forward(name, "main", base, competing).ok)
                return original(*args, **kwargs)

            with patch.object(source, "_git", side_effect=race):
                outcome = source.fast_forward(name, "main", base, tip)
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.actual_parent, competing)
            self.assertEqual(source.ref_sha(name, "heads/main"), competing)
            self.assertTrue(source.fast_forward(name, "main", competing, tip).ok)
            self.assertEqual(source.ref_sha(name, "heads/main"), tip)
            # Ordinary contributor writes still go through Forgejo's protection hooks.
            api.request(
                "POST",
                f"repos/{name}/branch_protections",
                {
                    "branch_name": "main",
                    "enable_push": True,
                    "enable_push_whitelist": True,
                    "push_whitelist_usernames": ["queue"],
                },
            )
            member_api = Forgejo(repository.forge, token=tokens["contributor"])
            member = ForgejoGitSource(identity, member_api, clone, token=tokens["contributor"])
            blocked = commit(tip, "must be blocked")
            member._git("push", repository.clone_url, f"{blocked}:refs/heads/contributor-control", write=True)
            self.assertFalse(member.fast_forward(name, "main", tip, blocked).ok)
            self.assertEqual(source.ref_sha(name, "heads/main"), tip)
            self.assertTrue(source.fast_forward(name, "main", tip, blocked).ok)
            # Timeout after acceptance is uncertain; reconciliation finds the exact
            # immutable ref and safely avoids any second push.
            retained = "refs/tauceti-queue/rehearsal/accepted"
            original = source._git

            accepted = False
            original_observation = source._remote_refs

            def lose(*args, **kwargs):
                nonlocal accepted
                result = original(*args, **kwargs)
                if args[0] == "push":
                    accepted = True
                    raise AmbiguousWrite("fixture lost accepted response")
                return result

            def unavailable(*args, **kwargs):
                if accepted:
                    raise ForgeError("fixture observation unavailable")
                return original_observation(*args, **kwargs)

            with (
                patch.object(source, "_git", side_effect=lose),
                patch.object(source, "_remote_refs", side_effect=unavailable),
            ):
                with self.assertRaises(AmbiguousWrite):
                    source.update_ref(name, retained, tip)
            with patch.object(source, "_git", wraps=source._git) as observed:
                source.update_ref(name, retained, tip)
                self.assertFalse(any(call.args[0] == "push" for call in observed.call_args_list))
            reader.fetch_refs(name, [f"+{retained}:refs/tauceti-queue/restored"])
            self.assertEqual(reader._git("rev-parse", "refs/tauceti-queue/restored").stdout.decode().strip(), tip)
            promoted = commit(blocked, "lost promotion acknowledgement")
            original = source._git

            def lost_promotion(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[0] == "push":
                    return subprocess.CompletedProcess(result.args, 1, b"", b"fixture disconnected")
                return result

            with patch.object(source, "_git", side_effect=lost_promotion):
                self.assertTrue(source.fast_forward(name, "main", blocked, promoted).ok)
            self.assertEqual(source.ref_sha(name, "heads/main"), promoted)
            # A changed native identity refuses before another Git network call.
            wrong = ForgejoGitSource(
                replace(identity, native_id=identity.native_id + 1), api, clone, token=tokens["operator"]
            )
            with patch.object(wrong, "_git", wraps=wrong._git) as observed:
                with self.assertRaises(Incomplete):
                    wrong.fetch_refs(name, [base])
                self.assertFalse(observed.called)
            raw = (clone / ".git/config").read_text()
            self.assertFalse(any(token in raw for token in tokens.values()))


if __name__ == "__main__":
    unittest.main()
