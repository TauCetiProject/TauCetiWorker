#!/usr/bin/env python3
"""Real-Git main mirror leases, divergence refusal and lost-response recovery."""

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_stage1 as fixtures
from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable

from tauceti_queue import github_app
from tauceti_queue.stage1 import Operator
from tauceti_queue.stage1_mirror import PERMISSIONS, MainMirror, MirrorEvidence


class Mirror(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixtures.Stage1()
        f.setUp()
        self.addCleanup(f.doCleanups)
        with f.operator().session() as operator:
            prepared = operator.prepare(1, f.head)
            operator.source.retain_candidate(operator.ledger.load(prepared["attempt_id"]))
        self.tip = prepared["request"]["candidate"]["head"]
        fixtures.git(f.remote, "update-ref", "refs/heads/main", self.tip, f.base)
        self.destination = f.root / "github.git"
        fixtures.git(f.root, "init", "-q", "--bare", str(self.destination))
        fixtures.git(f.author, "push", "-q", str(self.destination), f.base + ":refs/heads/main")
        credentials = github_app.AppCredentials(1, "fixture", 2, b"not-used", b"not-used")
        self.tokens = github_app.InstallationTokenSource(
            credentials,
            lambda *_: self.fail("unexpected mint"),
            repository_id=f.policy.execution.native_id,
            permissions=PERMISSIONS,
        )
        self.tokens._current = github_app.InstallationToken("fixture-mirror-only", 10**12, permissions=PERMISSIONS)
        self.metadata = {"id": f.policy.execution.native_id, "full_name": "team/Execution", "default_branch": "main"}
        self.before_read = None

        def get(path):
            self.assertEqual(path, "/git/ref/heads/main")
            if self.before_read is not None:
                callback, self.before_read = self.before_read, None
                callback()
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.remote_head()}}

        self.reader = SimpleNamespace(repository=lambda: self.metadata, get=get)
        self.mirror = MainMirror(f.policy, f.clone, f.root, tokens=self.tokens, reader_factory=lambda _: self.reader)
        self.commands = []
        self.lose_push_response = False
        self.before_push = None
        run = subprocess.run

        def transport(arguments, **kwargs):
            if arguments[:2] != ["git", "--no-replace-objects"]:
                return run(arguments, **kwargs)
            self.commands.append(arguments)
            self.assertIn(arguments[4], {"fetch", "push"})
            env = kwargs["env"]
            values = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
            self.assertNotIn("fixture-source-token", " ".join(values))
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraheader")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
            self.assertIn(
                "http." + f.policy.execution.repository.clone_url + ".extraheader",
                [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))],
            )
            if arguments[4] == "push" and self.before_push is not None:
                callback, self.before_push = self.before_push, None
                callback()
            mapped = [
                str(self.destination) if arg == f.policy.execution.repository.clone_url else arg for arg in arguments
            ]
            mapped[4:4] = ["-c", "protocol.file.allow=always"]
            # Only the test transport maps the verified HTTPS repository onto
            # a local bare repository. The actual Git lease still runs.
            result = run(mapped, capture_output=True, **kwargs)
            if arguments[4] == "push" and self.lose_push_response:
                self.assertEqual(result.returncode, 0)
                self.lose_push_response = False
                raise OSError("lost acknowledgement after successful Git update")
            return result

        self.transport = transport

    def remote_head(self):
        return fixtures.git(self.destination, "rev-parse", "refs/heads/main")

    def other_main(self):
        f = self.fixture
        fixtures.git(f.author, "checkout", "-q", "--detach", f.base)
        fixtures.git(f.author, "commit", "-qm", "independent GitHub contribution", "--allow-empty")
        sha = fixtures.git(f.author, "rev-parse", "HEAD")
        fixtures.git(f.author, "push", "-q", str(self.destination), "HEAD:main")
        return sha

    def publish(self):
        with patch.object(self.mirror, "_run_git", side_effect=self.transport):
            return self.mirror.publish(self.fixture.source, self.tip, self.fixture.base)

    def test_only_main_moves_and_equal_main_is_a_read_only_success(self):
        first = self.publish()
        self.assertTrue(first["changed"])
        self.assertEqual(self.remote_head(), self.tip)
        pushes = [args for args in self.commands if args[4] == "push"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][-1], self.tip + ":refs/heads/main")
        self.assertIn("--force-with-lease=refs/heads/main:" + self.fixture.base, pushes[0])
        self.assertFalse(self.publish()["changed"])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(fixtures.git(self.destination, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_lost_push_acknowledgement_recovers_without_another_push(self):
        self.lose_push_response = True
        with self.assertRaises(EvidenceUnavailable):
            self.publish()
        self.assertEqual(self.remote_head(), self.tip)
        self.assertFalse(self.publish()["changed"])
        self.assertEqual(len([a for a in self.commands if a[4] == "push"]), 1)

    def test_divergence_preserves_both_histories(self):
        other = self.other_main()
        with self.assertRaisesRegex(ValueError, "diverged or is ahead"):
            self.publish()
        self.assertEqual(self.remote_head(), other)
        self.assertFalse(any(a[4] == "push" for a in self.commands))
        self.assertEqual(
            self.fixture.source.ref_sha(self.fixture.policy.source.repository.key, "refs/heads/main"), self.tip
        )

    def test_concurrent_github_change_is_refused_by_real_git_lease(self):
        changed = []
        self.before_push = lambda: changed.append(self.other_main())
        with self.assertRaisesRegex(ValueError, "GitHub refused"):
            self.publish()
        self.assertEqual(self.remote_head(), changed[0])
        with self.assertRaisesRegex(ValueError, "diverged or is ahead"):
            self.publish()

    def test_source_change_or_wrong_repository_never_writes(self):
        f = self.fixture
        self.before_read = lambda: fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        with self.assertRaisesRegex(EvidenceUnavailable, "source main advanced"):
            self.publish()
        self.assertEqual(self.commands, [])
        fixtures.git(f.remote, "update-ref", "refs/heads/main", self.tip, f.base)
        self.metadata["id"] = 999
        with self.assertRaisesRegex(ValueError, "repository identity"):
            self.publish()
        self.assertEqual(self.commands, [])

    def test_unscoped_or_wrong_permission_credentials_are_refused(self):
        f = self.fixture
        self.tokens._repository_id = 999
        with self.assertRaisesRegex(ValueError, "repository-ID-scoped"):
            MainMirror(f.policy, f.clone, f.root, tokens=self.tokens)
        with self.assertRaises(ValueError):
            MainMirror(f.policy, f.clone, f.root, tokens=SimpleNamespace())

    def test_ref_reads_are_bound_to_native_repository_id(self):
        evidence = MirrorEvidence(self.fixture.policy, token="fixture-only", token_origin=cc.GITHUB)
        self.assertEqual(
            evidence._url("/git/ref/heads/main"), "https://api.github.com/repositories/12/git/ref/heads/main"
        )

    def test_unknown_source_main_and_changed_controls_cannot_be_automatically_mirrored(self):
        f = self.fixture
        f.profile.update(mirror_main=True, workflow_revision=f.base)
        with Operator(f.profile, source=f.source, client=f.api, execution=f.execution).session() as operator:
            with self.assertRaisesRegex(ValueError, "no matching completed"):
                operator.mirror()
        fixtures.git(f.author, "checkout", "-q", "--detach", f.base)
        (f.author / "tauceti.toml").write_text("unapproved control change\n")
        fixtures.git(f.author, "add", "tauceti.toml")
        fixtures.git(f.author, "commit", "-qm", "change execution control")
        changed = fixtures.git(f.author, "rev-parse", "HEAD")
        fixtures.git(f.clone, "fetch", "-q", str(f.author), changed)
        with self.assertRaisesRegex(ValueError, "control files changed"):
            MainMirror.check_controls(f.clone, f.base, changed)

    def test_preflight_refusal_keeps_source_unpromoted(self):
        f = self.fixture
        fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        f.profile.update(mirror_main=True, workflow_revision=f.base)

        def preflight(*_):
            raise ValueError("App installation or mirror needs repair")

        publisher = SimpleNamespace(preflight=preflight, publish=lambda *_: self.fail("must not publish"))
        f.execution.classification = "passed"
        with Operator(
            f.profile, source=f.source, client=f.api, execution=f.execution, mirror=publisher
        ).session() as operator:
            row = operator.prepare(1, f.head)
            operator.dispatch(row["attempt_id"])
            with self.assertRaisesRegex(ValueError, "needs repair"):
                operator.promote(row["attempt_id"])
        self.assertEqual(f.source.ref_sha(f.policy.source.repository.key, "refs/heads/main"), f.base)
        self.assertEqual(f.source.promotions, [])

    def test_next_handoff_uses_unchanged_approved_caller_after_first_mirror(self):
        f = self.fixture
        fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        f.profile.update(mirror_main=True, workflow_revision=f.base)

        publisher = self.mirror
        f.execution.classification = "passed"
        with (
            patch.object(self.mirror, "_run_git", side_effect=self.transport),
            Operator(
                f.profile, source=f.source, client=f.api, execution=f.execution, mirror=publisher
            ).session() as operator,
        ):
            first = operator.prepare(1, f.head)
            operator.dispatch(first["attempt_id"])
            landed = operator.promote(first["attempt_id"])["candidate"]
        fixtures.git(f.author, "fetch", "-q", str(f.remote), "main")
        fixtures.git(f.author, "checkout", "-q", "--detach", "FETCH_HEAD")
        (f.author / "Math/Second.lean").write_text("theorem second : True := trivial\n")
        fixtures.git(f.author, "add", "Math/Second.lean")
        fixtures.git(f.author, "commit", "-qm", "second contribution")
        head = fixtures.git(f.author, "rev-parse", "HEAD")
        fixtures.git(f.author, "push", "-q", str(f.remote), "HEAD:refs/pull/2/head")
        f.api.head = head
        f.profile["execution_mode"] = "workflow"
        from queue_ci_execution import Reader

        with (
            patch.object(self.mirror, "_run_git", side_effect=self.transport),
            Operator(
                f.profile, source=f.source, client=f.api, execution=f.execution, mirror=publisher
            ).session() as operator,
        ):
            second = operator.prepare(2, head)
            self.assertEqual(second["request"]["workflow_revision"], landed)
            reader = Reader(operator.ledger.load(second["attempt_id"]))
            reader.metadata["id"] = f.policy.execution.native_id
            read = reader.get
            reader.get = lambda path: (
                {"commit": {"sha": self.remote_head()}} if path.startswith("/branches/") else read(path)
            )
            f.execution._reader = lambda _: reader
            payload = operator.handoff(second["attempt_id"])
            self.assertIn(landed, payload["request"])
            f.execution.discover = lambda frozen, **_: ([f.execution.observe(frozen, "100")], False)
            result = operator.promote(second["attempt_id"])
            self.assertEqual(self.remote_head(), result["candidate"])
            self.assertEqual(len(f.source.promotions), 2)
            self.assertEqual(len([args for args in self.commands if args[4] == "push"]), 2)
        self.assertEqual(f.profile["workflow_revision"], f.base)

    def test_bootstrap_confirmation_is_read_only_and_cannot_catch_up_a_different_main(self):
        f = self.fixture
        fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        f.profile.update(mirror_main=True, workflow_revision=f.base)
        f.api.writable = False
        with Operator(
            f.profile, source=f.source, client=f.api, execution=f.execution, mirror=self.mirror
        ).session() as operator:
            result = operator.mirror()
            self.assertFalse(result["changed"])
            self.assertEqual(result["state"], "confirmed")
            self.assertEqual(self.commands, [])
        fixtures.git(f.remote, "update-ref", "refs/heads/main", self.tip, f.base)
        f.profile["workflow_revision"] = self.tip
        with Operator(
            f.profile, source=f.source, client=f.api, execution=f.execution, mirror=self.mirror
        ).session() as operator:
            with self.assertRaisesRegex(ValueError, "must already match"):
                operator.mirror()
            self.assertEqual(operator.store.get_setting("stage1:mirror-result")["state"], "refused")
        self.assertEqual(self.commands, [])

    def test_rebuilt_clone_fetches_the_pin_before_comparing_controls(self):
        f = self.fixture
        clone = f.root / "rebuilt-clone"
        fixtures.git(f.root, "init", "-q", str(clone))
        f.source.clone = clone
        self.mirror = MainMirror(f.policy, clone, f.root, tokens=self.tokens, reader_factory=lambda _: self.reader)
        self.assertTrue(self.publish()["changed"])
        self.assertEqual(self.remote_head(), self.tip)

    def test_source_promotion_survives_mirror_failure_and_retry_does_not_merge_twice(self):
        f = self.fixture
        # Return to a source main that has not yet landed the candidate.
        fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        f.profile["mirror_main"] = True
        f.profile["workflow_revision"] = f.base
        calls = []

        def publish(source, head, approved):
            calls.append(head)
            self.assertEqual(source.ref_sha(f.policy.source.repository.key, "refs/heads/main"), head)
            if len(calls) == 1:
                raise OSError("mirror temporarily unavailable")
            return {"main": head, "changed": False}

        publisher = SimpleNamespace(publish=publish, preflight=lambda *args: None)
        f.execution.classification = "passed"
        with Operator(
            f.profile, source=f.source, client=f.api, execution=f.execution, mirror=publisher
        ).session() as operator:
            row = operator.prepare(1, f.head)
            operator.dispatch(row["attempt_id"])
            with self.assertRaisesRegex(EvidenceUnavailable, "source promotion is confirmed"):
                operator.promote(row["attempt_id"])
            self.assertEqual(len(f.source.promotions), 1)
            result = operator.promote(row["attempt_id"])
            self.assertTrue(result["reconciled"])
            self.assertEqual(result["mirror"]["main"], row["request"]["candidate"]["head"])
            self.assertEqual(len(f.source.promotions), 1)
            self.assertEqual(f.execution.sends, 1)

    def test_read_only_mirror_recovers_a_lost_source_push_acknowledgement(self):
        f = self.fixture
        fixtures.git(f.remote, "update-ref", "refs/heads/main", f.base, self.tip)
        f.profile.update(mirror_main=True, workflow_revision=f.base)
        f.execution.classification = "passed"
        original = f.source.fast_forward

        def lose(*args):
            original(*args)
            raise OSError("source push completed but acknowledgement was lost")

        with (
            patch.object(self.mirror, "_run_git", side_effect=self.transport),
            Operator(
                f.profile, source=f.source, client=f.api, execution=f.execution, mirror=self.mirror
            ).session() as operator,
        ):
            row = operator.prepare(1, f.head)
            operator.dispatch(row["attempt_id"])
            with patch.object(f.source, "fast_forward", side_effect=lose), self.assertRaises(OSError):
                operator.promote(row["attempt_id"])
            self.assertEqual(operator.store.row("ci_attempts", {"id": row["attempt_id"]})["outcome"], "pending")
        f.api.writable = False
        with (
            patch.object(self.mirror, "_run_git", side_effect=self.transport),
            Operator(
                f.profile, source=f.source, client=f.api, execution=f.execution, mirror=self.mirror
            ).session() as operator,
        ):
            result = operator.mirror()
            self.assertEqual(result["main"], row["request"]["candidate"]["head"])
            self.assertEqual(operator.store.row("ci_attempts", {"id": row["attempt_id"]})["outcome"], "pass")
        self.assertEqual(len(f.source.promotions), 1)


if __name__ == "__main__":
    unittest.main()
