#!/usr/bin/env python3
"""Frozen source request recovery, configuration upgrades and real Git GC."""

import concurrent.futures
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_queue.ci_requests import RequestLedger
from tauceti_queue.fakes import FakeRepo, run_git
from tauceti_queue.store import Store

CONFIG = b"""schema_version=2
[project]
branch="main"
[forges.native]
kind="forgejo"
url="https://source.example"
[repositories]
content={forge="native",name="team/Math"}
roadmap={forge="native",name="team/Roadmap"}
[lean]
roots=["Math"]
[ci]
backend="github-actions"
repository="team/Execution"
"""


def policy(configuration=CONFIG):
    native = Forge("forgejo", "https://source.example")
    return cc.ExecutionPolicy(
        cc.RepositoryIdentity(RepositoryRef(native, "team/Math"), 41),
        cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Execution"), 42),
        native,
        ".github/workflows/pr-build.yml",
        cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Tools"), 43),
        "a" * 40,
        "main",
        "main",
        True,
        hashlib.sha256(configuration).hexdigest(),
    )


class Requests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = FakeRepo(self.root / "git")
        self.base = self.repo.main_sha()
        self.head = self.repo.commit_branch({"A.lean": "a\n"}, "member", push_to="refs/heads/proof")
        run_git(self.repo.clone, "fetch", "-q", "origin", "refs/heads/proof:refs/remotes/origin/proof")
        self.path = str(self.root / "queue.sqlite")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.ledger = RequestLedger(self.store, self.repo.clone, policy(), CONFIG, workflow_revision="b" * 40)

    def attempt(self, nonce="1" * 32):
        return self.store.insert(
            "ci_attempts",
            batch_id=1,
            batch_version=2,
            head_sha=self.head,
            base_sha=self.base,
            base_ref="main",
            request_id=nonce,
            dispatch_state="prepared",
            dispatched_at=100,
        )

    def test_request_recovery_keeps_original_members_and_wire_payload(self):
        attempt = self.attempt()
        original = self.ledger.freeze(attempt, (cc.Member(17, self.head),), now=100)
        stored = self.store.row("ci_requests", {"attempt_id": attempt})
        self.assertEqual(stored["request_digest"], original.request.digest)
        self.assertEqual(stored["configuration"], CONFIG)
        self.assertEqual(json.loads(stored["request_json"]), original.request.record())
        other = Store(self.path)
        self.addCleanup(other.close)
        resumed = RequestLedger(other, self.repo.clone, policy(), CONFIG, workflow_revision="c" * 40)
        recovered = resumed.freeze(attempt, (cc.Member(99, self.base),), now=500)
        self.assertEqual(recovered, original)
        self.assertEqual(len(recovered.retained_refs()), 2)
        self.assertEqual(self.store.count("ci_requests"), 1)

    def test_approved_pipeline_upgrade_preserves_pending_policy_and_configuration(self):
        attempt = self.attempt()
        original = self.ledger.freeze(attempt, (cc.Member(17, self.head),), now=100)
        configuration = CONFIG + b"\n# approved configuration upgrade\n"
        upgraded_policy = replace(policy(configuration), harness_revision="d" * 40)
        upgraded = RequestLedger(
            self.store, self.repo.clone, upgraded_policy, configuration, workflow_revision="c" * 40
        )
        self.assertEqual(upgraded.load(attempt), original)
        newer = upgraded.freeze(self.attempt("2" * 32), (cc.Member(17, self.head),), now=200)
        self.assertEqual(newer.policy, upgraded_policy)
        self.assertNotEqual(newer.request.policy_digest, original.request.policy_digest)
        self.assertEqual(newer.request.epoch, original.request.epoch)
        with self.assertRaisesRegex(ValueError, "roles changed"):
            RequestLedger(
                self.store,
                self.repo.clone,
                replace(upgraded_policy, execution=cc.RepositoryIdentity(upgraded_policy.execution.repository, 99)),
                configuration,
                workflow_revision="e" * 40,
            )
        self.assertEqual(upgraded.load(attempt), original)

    def test_retained_membership_restores_identical_objects_after_real_gc(self):
        attempt = self.attempt()
        frozen = self.ledger.freeze(attempt, tuple(cc.Member(number, self.head) for number in range(1, 130)), now=100)
        manifest = frozen.request.member_manifest
        self.assertIsNotNone(manifest)
        self.assertEqual(len(frozen.retained_refs()), 2)
        self.ledger.restore_membership(frozen)
        self.assertEqual(run_git(self.repo.clone, "cat-file", "-t", manifest.commit), "commit")
        run_git(self.repo.clone, "gc", "--prune=now", "--quiet")
        with self.assertRaises(RuntimeError):
            run_git(self.repo.clone, "cat-file", "-e", manifest.commit)
        recovered = self.ledger.load(attempt)
        self.ledger.restore_membership(recovered)
        self.assertEqual(recovered, frozen)
        header = run_git(self.repo.clone, "cat-file", "commit", manifest.commit).split("\n\n", 1)[0]
        self.assertEqual([line for line in header.splitlines() if line.startswith("parent ")], ["parent " + self.head])
        self.assertEqual(len(run_git(self.repo.clone, "ls-tree", manifest.commit).splitlines()), 1)

    def test_legacy_records_and_corrupt_snapshots_are_never_rebound(self):
        other = Store(str(self.root / "legacy.sqlite"))
        self.addCleanup(other.close)
        other.insert(
            "inputs",
            repository="team/Math",
            number=17,
            head_sha=self.head,
            state="accepted",
            approved_at=1,
        )
        with self.assertRaisesRegex(ValueError, "unqualified"):
            RequestLedger(other, self.repo.clone, policy(), CONFIG, workflow_revision="b" * 40)
        self.assertEqual(other.count("inputs"), 1)
        self.assertIsNone(other.get_setting("source-ci:binding"))
        attempt = self.attempt()
        self.ledger.freeze(attempt, (cc.Member(17, self.head),), now=100)
        self.store.update("ci_requests", {"attempt_id": attempt}, request_digest="0" * 64)
        with self.assertRaisesRegex(ValueError, "differs from its attempt"):
            self.ledger.load(attempt)

    def test_concurrent_discovery_selects_one_owner_and_retains_duplicates(self):
        attempt = self.attempt()
        request = self.ledger.freeze(attempt, (cc.Member(17, self.head),), now=100).request
        other = Store(self.path)
        self.addCleanup(other.close)
        ledger = RequestLedger(other, self.repo.clone, policy(), CONFIG, workflow_revision="b" * 40)

        def record(pair):
            target, run_id = pair
            return target.record_owned_run(
                attempt,
                SimpleNamespace(run_id=str(run_id), attempt=2),
                {
                    "artifact_id": 201,
                    "artifact_digest": "sha256:" + "a" * 64,
                    "kind": "request",
                    "run_id": run_id,
                    "request_digest": request.digest,
                    "receipt_attempt": 1,
                    "observed_attempt": 2,
                },
                now=200,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            owners = list(pool.map(record, [(self.ledger, 101), (ledger, 102)]))
        self.assertEqual(len(set(owners)), 1)
        self.assertEqual({row["run_id"] for row in self.ledger.owned_runs(attempt)}, {"101", "102"})
        self.assertEqual(self.ledger.owner_run(attempt), owners[0])

    def test_invalid_proof_cannot_own_or_steal_a_run(self):
        attempt = self.attempt()
        request = self.ledger.freeze(attempt, (cc.Member(17, self.head),), now=100).request
        run = SimpleNamespace(run_id="101", attempt=2)
        proof = {
            "artifact_id": 201,
            "artifact_digest": "sha256:" + "a" * 64,
            "kind": "request",
            "run_id": 101,
            "request_digest": request.digest,
            "receipt_attempt": 1,
            "observed_attempt": 2,
        }
        for changes in ({"request_digest": "0" * 64}, {"kind": "status"}, {"run_id": 102}, {"observed_attempt": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ledger.record_owned_run(attempt, run, dict(proof, **changes), now=200)
        self.assertIsNone(self.ledger.owner_run(attempt))
        self.ledger.record_owned_run(attempt, run, proof, now=200)
        second = self.attempt("2" * 32)
        newer = self.ledger.freeze(second, (cc.Member(17, self.head),), now=300).request
        with self.assertRaisesRegex(ValueError, "another request"):
            self.ledger.record_owned_run(second, run, dict(proof, request_digest=newer.digest), now=400)
        self.assertIsNone(self.ledger.owner_run(second))


if __name__ == "__main__":
    unittest.main()
