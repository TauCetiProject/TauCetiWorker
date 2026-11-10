#!/usr/bin/env python3
"""Real-Git operator acceptance, single-send recovery and exact-candidate promotion."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_queue.ports import FastForwardResult
from tauceti_queue.stage1 import Operator, ReadOnlyExecution, main

CONFIG = b"""schema_version=2
[project]
branch="main"
[forges.source]
kind="forgejo"
url="https://source.example"
[repositories]
content={forge="source",name="team/Math"}
roadmap={forge="source",name="team/Roadmap"}
[ci]
backend="github-actions"
repository="team/Execution"
"""


def git(directory, *arguments):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class Source:
    def __init__(self, identity, clone, remote):
        self.repository_identity, self.clone, self.remote = identity, clone, remote
        self.promotions = []

    def _bound(self, repository):
        assert repository == self.repository_identity.repository.key

    def ref_sha(self, repository, ref):
        self._bound(repository)
        return git(self.remote, "rev-parse", ref)

    def fetch_refs(self, repository, refspecs):
        self._bound(repository)
        git(self.clone, "fetch", "-q", str(self.remote), *refspecs)

    def retain_candidate(self, frozen):
        for ref, sha in frozen.retained_refs():
            git(self.clone, "push", "-q", str(self.remote), f"{sha}:{ref}")

    def fast_forward(self, repository, branch, expected_parent, tip):
        self._bound(repository)
        actual = self.ref_sha(repository, "refs/heads/" + branch)
        if actual != expected_parent:
            return FastForwardResult(False, actual)
        git(self.remote, "merge-base", "--is-ancestor", expected_parent, tip)
        git(self.remote, "update-ref", "refs/heads/" + branch, tip, expected_parent)
        self.promotions.append(tip)
        return FastForwardResult(True, expected_parent)


class Api:
    actor = 7
    writable = True
    state = "open"

    def __init__(self, source):
        self.source = source
        self.head = source.ref_sha(source.repository_identity.repository.key, "refs/pull/1/head")

    def current_user(self):
        return {"id": self.actor}

    def repository(self, reference):
        return {"id": self.source.repository_identity.native_id, "permissions": {"push": self.writable}}

    def pull(self, reference, number):
        return {
            "number": number,
            "state": self.state,
            "merged": False,
            "draft": False,
            "title": "A theorem",
            "body": "Reviewed by the operator",
            "head": {"sha": self.head},
            "base": {"ref": "main", "repo": {"id": self.source.repository_identity.native_id}},
        }

    def pull_ref(self, reference, number):
        return self.head


class Execution:
    def __init__(self):
        self.sends = 0
        self.lost = False
        self.classification = None

    def preflight(self, frozen):
        return SimpleNamespace(request_digest=frozen.request.digest)

    def dispatch(self, frozen, **kwargs):
        self.sends += 1
        if self.lost:
            raise OSError("lost dispatch acknowledgement")
        return frozen.request.request_id

    def observe(self, frozen, run_id, **kwargs):
        run = SimpleNamespace(run_id=str(run_id), attempt=1, status="completed")
        proof = {
            "run_id": int(run_id),
            "request_digest": frozen.request.digest,
            "kind": "result",
            "artifact_id": 101,
            "artifact_digest": "sha256:" + "c" * 64,
            "attempt": 1,
            "classification": self.classification,
        }
        return SimpleNamespace(run=run, ownership=proof, result=proof if self.classification else None)

    def discover(self, frozen, **kwargs):
        return ([self.observe(frozen, "99")] if self.classification else []), False


class Stage1(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stage1-operator-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.clone = self.root / "operator"
        git(self.root, "init", "-q", "--bare", str(self.remote))
        git(self.root, "init", "-q", "-b", "main", str(self.author))
        forge = Forge("forgejo", "https://source.example")
        self.policy = cc.ExecutionPolicy(
            cc.RepositoryIdentity(RepositoryRef(forge, "team/Math"), 11),
            cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Execution"), 12),
            forge,
            ".github/workflows/pr-build.yml",
            cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Tools"), 13),
            "d" * 40,
            "main",
            "main",
            True,
            hashlib.sha256(CONFIG).hexdigest(),
        )
        (self.author / "tauceti.toml").write_bytes(CONFIG)
        (self.author / ".tauceti").mkdir()
        (self.author / ".tauceti/ci-source.json").write_text(json.dumps(self.policy.record(with_config=False)))
        (self.author / "Math").mkdir()
        (self.author / "Math/Proof.lean").write_text("theorem first : True := trivial\n")
        git(self.author, "add", ".")
        git(self.author, "commit", "-qm", "base")
        self.base = git(self.author, "rev-parse", "HEAD")
        git(self.author, "push", "-q", str(self.remote), "HEAD:refs/heads/main")
        (self.author / "Math/Proof.lean").write_text("theorem first : True := by trivial\n")
        git(self.author, "commit", "-qam", "member")
        self.head = git(self.author, "rev-parse", "HEAD")
        git(self.author, "push", "-q", str(self.remote), "HEAD:refs/pull/1/head")
        git(self.root, "init", "-q", str(self.clone))
        for name, data in [("configuration", CONFIG), ("token", b"fixture-source-token")]:
            path = self.root / name
            path.write_bytes(data)
            path.chmod(0o600)
        self.profile = {
            "schema": "tauceti-stage1.operator/v1",
            "configuration": str(self.root / "configuration"),
            "execution_policy": self.policy.record(with_config=False),
            "workflow_revision": "e" * 40,
            "clone": str(self.clone),
            "database": str(self.root / "ledger.sqlite"),
            "source_token_file": str(self.root / "token"),
            "execution_credentials_dir": str(self.root),
            "reviewer_ids": [7],
        }
        self.source = Source(self.policy.source, self.clone, self.remote)
        self.api = Api(self.source)
        self.execution = Execution()

    def operator(self):
        return Operator(self.profile, source=self.source, client=self.api, execution=self.execution)

    def test_preparation_is_durable_and_exact_retries_keep_one_request(self):
        with self.operator().session() as operator:
            first = operator.prepare(1, self.head)
            second = operator.prepare(1, self.head)
            self.assertEqual(first, second)
            self.assertEqual(operator.store.count("ci_attempts"), 1)
            candidate = first["request"]["candidate"]["head"]
            self.assertEqual(git(self.clone, "rev-parse", candidate + "^"), self.base)
            self.assertIn(
                self.policy.source.repository.key + "#1", git(self.clone, "show", "-s", "--format=%B", candidate)
            )
        with self.operator().session() as operator:
            self.assertEqual(operator.prepare(1, self.head), first)
        self.assertEqual(self.execution.sends, 0)

    def test_wrong_actor_or_stale_acceptance_cannot_prepare(self):
        for actor, head in [(99, self.head), (7, "f" * 40)]:
            self.api.actor = actor
            with self.operator().session() as operator:
                with self.assertRaises(ValueError):
                    operator.prepare(1, head)
                self.assertEqual(operator.store.count("ci_attempts"), 0)

    def test_changed_source_base_or_head_refuses_dispatch_and_promotion(self):
        with self.operator().session() as operator:
            row = operator.prepare(1, self.head)
            self.api.head = "f" * 40
            with self.assertRaises(ValueError):
                operator.dispatch(row["attempt_id"])
            self.api.head = self.head
            git(self.remote, "update-ref", "refs/heads/main", self.head)
            with self.assertRaises(ValueError):
                operator.promote(row["attempt_id"])
        self.assertEqual(self.execution.sends, 0)
        self.assertFalse(self.source.promotions)

    def test_lost_dispatch_is_never_automatically_retransmitted_after_restart(self):
        self.execution.lost = True
        with self.operator().session() as operator:
            row = operator.prepare(1, self.head)
            with self.assertRaises(OSError):
                operator.dispatch(row["attempt_id"])
        with self.operator().session() as operator:
            with self.assertRaises(EvidenceUnavailable):
                operator.dispatch(row["attempt_id"])
            self.assertEqual(operator.store.row("ci_attempts", {"id": row["attempt_id"]})["dispatch_state"], "sending")
        self.assertEqual(self.execution.sends, 1)

    def test_failed_result_cannot_promote_and_passing_result_lands_exact_candidate_once(self):
        with self.operator().session() as operator:
            row = operator.prepare(1, self.head)
            operator.dispatch(row["attempt_id"])
            self.execution.classification = cc.classify_verdicts(
                {**dict.fromkeys(cc.VERDICTS, "success"), "build": "failure"}
            )
            with self.assertRaisesRegex(ValueError, "failed CI"):
                operator.promote(row["attempt_id"])
            self.assertEqual(self.source.ref_sha(self.policy.source.repository.key, "refs/heads/main"), self.base)
            self.execution.classification = "passed"
            result = operator.promote(row["attempt_id"])
            self.assertEqual(result["candidate"], row["request"]["candidate"]["head"])
            recovered = operator.promote(row["attempt_id"])
            self.assertTrue(recovered["reconciled"])
            self.assertEqual(len(self.source.promotions), 1)
            self.assertEqual(operator.store.row("ci_attempts", {"id": row["attempt_id"]})["outcome"], "pass")
            batch_id = operator.ledger.load(row["attempt_id"]).request.batch_id
            self.assertEqual(operator.store.row("batches", {"id": batch_id})["state"], "done")
            with self.assertRaisesRegex(ValueError, "already landed|no changes"):
                operator.prepare(1, self.head)
            # Preserve an unrebased new revision of the same still-open PR.
            (self.author / "Math/Proof.lean").write_text("theorem first : True := by exact True.intro\n")
            git(self.author, "commit", "-qam", "follow-up revision")
            self.api.head = git(self.author, "rev-parse", "HEAD")
            git(self.author, "push", "-q", str(self.remote), "HEAD:refs/pull/1/head")
            with self.assertRaisesRegex(ValueError, "earlier revision.*preserve the new work"):
                operator.prepare(1, self.api.head)
            # Later main movement does not make a known completed promotion
            # look unlanded, even though the original PR remained open.
            git(self.author, "fetch", "-q", str(self.remote), "main")
            git(self.author, "checkout", "-q", "--detach", "FETCH_HEAD")
            git(self.author, "commit", "--allow-empty", "-qm", "later main")
            later = git(self.author, "rev-parse", "HEAD")
            git(self.author, "push", "-q", str(self.remote), "HEAD:main")
            self.assertEqual(operator.promote(row["attempt_id"])["current_main"], later)
            self.assertEqual(len(self.source.promotions), 1)

    def test_workflow_handoff_is_durable_and_export_sends_nothing(self):
        self.profile["execution_mode"] = "workflow"
        with self.operator().session() as operator:
            row = operator.prepare(1, self.head)
            frozen = operator.ledger.load(row["attempt_id"])
            from queue_ci_execution import Reader

            reader = Reader(frozen)
            reader.metadata["id"] = self.policy.execution.native_id
            reader.metadata["id"] = self.policy.execution.native_id
            self.execution._reader = lambda _: reader
            payload = operator.handoff(row["attempt_id"])
            self.assertEqual(json.loads(payload["request"]), frozen.request.record())
            with self.assertRaisesRegex(ValueError, "already handed off"):
                operator.handoff(row["attempt_id"])
        with self.operator().session() as operator:
            self.assertEqual(operator.export_handoff(row["attempt_id"]), payload)
            self.assertEqual(self.execution.sends, 0)
            self.assertEqual(operator.ledger.owner_run(row["attempt_id"]), None)

    def test_handoff_output_errors_cannot_change_the_dispatch_state(self):
        with self.operator().session() as operator:
            row = operator.prepare(1, self.head)
        target = self.root / "existing.json"
        target.write_text("keep this file")
        for path in ["relative.json", str(target), str(self.root / "missing" / "file.json")]:
            with patch("tauceti_queue.stage1.Operator") as constructor:
                result = main(["--profile", "/unused", "handoff", "--attempt", str(row["attempt_id"]), "--out", path])
                self.assertEqual(result, 78)
                constructor.assert_not_called()
        self.assertEqual(target.read_text(), "keep this file")
        with self.operator().session() as operator:
            self.assertEqual(operator.store.row("ci_attempts", {"id": row["attempt_id"]})["dispatch_state"], "prepared")

    def test_workflow_read_client_observes_and_discovers_without_write_credentials(self):
        self.profile["execution_mode"] = "workflow"
        token = self.root / "read-token"
        token.write_text("fixture-only-read-credential")
        token.chmod(0o600)
        self.profile["execution_read_token_file"] = str(token)
        with Operator(self.profile, source=self.source, client=self.api).session() as operator:
            self.assertIsInstance(operator.execution, ReadOnlyExecution)
            row = operator.prepare(1, self.head)
            frozen = operator.ledger.load(row["attempt_id"])
            from queue_ci_execution import Reader

            reader = Reader(frozen)
            proof = reader.add()
            reader.runs["101"]["repository"]["id"] = self.policy.execution.native_id
            reader.pages = [{"total_count": 1, "workflow_runs": [reader.runs["101"]]}]
            with patch.object(operator.execution, "_reader", return_value=reader):
                operator.handoff(row["attempt_id"])
                observed = operator.observe(row["attempt_id"])
            self.assertEqual(observed["owner_run_id"], "101")
            self.assertEqual(observed["classification"], "unavailable")
            self.assertEqual(proof["request_digest"], frozen.request.digest)
            evidence = operator.execution._reader(frozen)
            calls = []

            def read(request, _limit):
                calls.append((request.get_method(), request.full_url))
                return json.dumps({"id": self.policy.execution.native_id, "full_name": "team/Execution"}).encode()

            with patch.object(evidence, "_read", side_effect=read):
                self.assertEqual(evidence.repository()["id"], self.policy.execution.native_id)
            self.assertEqual(calls, [("GET", "https://api.github.com/repos/team/Execution")])
            with self.assertRaisesRegex(ValueError, "HTTP writes"):
                operator.execution._post(frozen, "/dispatches")

    def test_operator_and_controller_cannot_adopt_each_others_ledger(self):
        from tauceti_queue.controller import Controller
        from tauceti_queue.store import Store

        with self.operator().session() as operator:
            with self.assertRaisesRegex(ValueError, "operator ledger"):
                Controller(operator.store, self.source, str(self.clone), "team/Math", revision="fixture")
        other = self.root / "controller.sqlite"
        store = Store(str(other))
        try:
            store.set_setting("queue:operating-mode", "controller")
        finally:
            store.close()
        self.profile["database"] = str(other)
        with self.assertRaisesRegex(ValueError, "controller-owned"):
            with self.operator().session():
                self.fail("operator adopted a controller database")

    def test_unused_profile_binding_can_be_corrected_and_reviewers_can_rotate(self):
        wrong = replace(self.policy, execution=replace(self.policy.execution, native_id=99))
        self.profile["execution_policy"] = wrong.record(with_config=False)
        self.profile["reviewer_ids"] = [99]
        with self.operator().session() as operator:
            with self.assertRaisesRegex(ValueError, "not an approved"):
                operator.prepare(1, self.head)
        self.profile["execution_policy"] = self.policy.record(with_config=False)
        self.profile["reviewer_ids"] = [7]
        with self.operator().session() as operator:
            first = operator.prepare(1, self.head)
        self.profile["reviewer_ids"] = [99]
        self.api.actor = 99
        with self.operator().session() as operator:
            with self.assertRaisesRegex(ValueError, "matching explicit"):
                operator.promote(first["attempt_id"])
            second = operator.prepare(1, self.head)
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])

    def test_abandon_fences_late_success_and_allocates_a_distinct_request(self):
        self.execution.lost = True
        with self.operator().session() as operator:
            first = operator.prepare(1, self.head)
            with self.assertRaises(OSError):
                operator.dispatch(first["attempt_id"])
            operator.abandon(first["attempt_id"], "Original dispatch outcome cannot be settled; explicitly replace it")
        self.execution.classification = "passed"
        with self.operator().session() as operator:
            with self.assertRaisesRegex(ValueError, "abandoned"):
                operator.promote(first["attempt_id"])
            second = operator.prepare(1, self.head)
            self.assertNotEqual(first["request"]["request_id"], second["request"]["request_id"])
            self.assertEqual(self.execution.sends, 1)
            operator.store.set_setting(f"stage1:promotion-intent:{second['attempt_id']}", {"uncertain": True})
            with self.assertRaisesRegex(ValueError, "promotion may already"):
                operator.abandon(second["attempt_id"], "Cannot retire uncertain source push")

    def test_changed_protected_caller_revokes_old_candidate_promotion(self):
        with self.operator().session() as operator:
            first = operator.prepare(1, self.head)
        self.profile["workflow_revision"] = "f" * 40
        with self.operator().session() as operator:
            with self.assertRaisesRegex(ValueError, "caller revision changed"):
                operator.promote(first["attempt_id"])

    def test_real_policy_source_and_infrastructure_verdicts_are_definite_refusals(self):
        with self.operator().session() as operator:
            first = operator.prepare(1, self.head)
            operator.dispatch(first["attempt_id"])
            for field, value, message in [
                ("scope", "failure", "failed CI"),
                ("build", "failure", "failed CI"),
                ("build", "infrastructure", "infrastructure failure"),
            ]:
                verdicts = {**dict.fromkeys(cc.VERDICTS, "success"), field: value}
                self.execution.classification = cc.classify_verdicts(verdicts)
                with self.subTest(verdicts=verdicts), self.assertRaisesRegex(ValueError, message):
                    operator.promote(first["attempt_id"])
            self.assertEqual(self.source.promotions, [])

    def test_read_only_execution_never_uses_its_token_for_a_write(self):
        client = ReadOnlyExecution(self.policy, "fixture-read-token")
        from tauceti_queue.ports import DispatchNotAccepted

        for operation in (client.preflight, client.dispatch):
            with self.assertRaises(DispatchNotAccepted):
                operation(None)
        with self.assertRaises(ValueError):
            client.cancel(None)

    def test_only_one_operator_can_hold_the_existing_ledger(self):
        with self.operator().session():
            with self.assertRaises(BlockingIOError):
                with self.operator().session():
                    self.fail("second operator entered the publication window")


if __name__ == "__main__":
    unittest.main()
