#!/usr/bin/env python3
"""Native source transport exercises the common persistent self-tested worker flow."""

import contextlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_forgejo_source as fixture
from tauceti_tools.forge import ForgeError

from tauceti_worker.forgejo_worker import ForgejoWorkerSource


class NativeSelfTested(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NativeSource()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.api._token = "token"
        f.api.max_bytes = 1024 * 1024
        f.api.repository.return_value = dict(id=41, full_name=f.repository.name, archived=False)
        self.source = ForgejoWorkerSource(f.identity, f.api, token_file=f.clone / "token")
        self.live = dict(
            state="open",
            draft=False,
            base=dict(ref="main"),
            head=dict(sha="a" * 40, ref="feature"),
            user=dict(id=7, login="reviewer"),
            labels=[],
        )
        self.source.live_pull = Mock(side_effect=lambda n: self.live)

        def label(number, name):
            self.live["labels"] = [dict(name=name)]
            return True

        self.source.add_label = Mock(side_effect=label)

    def test_roadmap_branch_lookup_is_fresh_owner_bound_and_unambiguous(self):
        row = dict(number=1, headRefName="roadmap/x", headRepositoryOwner=dict(login="team"))
        self.source.reader.pulls = Mock(return_value=[row])
        self.assertEqual(self.source.pull_for_head("team", "roadmap/x"), 1)
        self.assertEqual(self.source.reader.pulls.call_args.kwargs, dict(state="open", head="roadmap/x"))
        self.assertIsNone(self.source.pull_for_head("other", "roadmap/x"))
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/other"))
        self.source.reader.pulls.return_value = [row, {**row, "number": 2}]
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.source.reader.pulls.side_effect = ForgeError("outage", status=503)
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.assertEqual(self.source.reader.pulls.call_count, 5)

    def test_selected_state_refresh_expires_shared_inventory(self):
        from tauceti_worker.source_survey import discovery_inventory

        self.source.shared_inventory = True
        self.source.open_prs = Mock(side_effect=[[dict(number=1)], [dict(number=2)]])
        self.source.pr_view = Mock(return_value=dict(headRefOid="b" * 40))
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=str(self.fixture.clone / "shared-reads")):
            self.assertEqual(discovery_inventory(self.source), [dict(number=1)])
            self.assertEqual(discovery_inventory(self.source), [dict(number=1)])
            result = self.source.refresh_pr_state(1, "a" * 40)
            self.assertEqual(result["status"], "ok")
            self.source.pr_view.assert_called_once_with(1, ["headRefOid"], fresh=True)
            self.assertEqual(discovery_inventory(self.source), [dict(number=2)])

    def test_shared_refill_bypasses_adapter_local_inventory(self):
        from tauceti_worker.github_transport import invalidate_shared
        from tauceti_worker.source_identity import discovery_scope
        from tauceti_worker.source_survey import discovery_inventory

        self.source.shared_inventory = True
        first, second = [dict(number=1)], [dict(number=2)]
        self.source.reader.pulls = Mock(side_effect=[first, second])
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=str(self.fixture.clone / "shared-reads")):
            self.assertEqual(discovery_inventory(self.source), first)
            invalidate_shared(discovery_scope(self.source))
            self.assertEqual(discovery_inventory(self.source), second)
        self.assertEqual(self.source.reader.pulls.call_count, 2)

    def test_native_label_write_invalidates_shared_discovery(self):
        from tauceti_worker.github_transport import shared_generation
        from tauceti_worker.source_identity import discovery_scope

        self.source.shared_inventory = True
        scope = discovery_scope(self.source)
        with (
            patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=str(self.fixture.clone / "shared-reads")),
            patch.object(self.source, "validate_binding"),
            patch("tauceti_tools.forge_write.ForgejoWriter.edit") as edit,
        ):
            before = shared_generation(scope)
            ForgejoWorkerSource.add_label(self.source, 1, "self-tested")
            self.assertNotEqual(shared_generation(scope), before)
            edit.assert_called_once_with(self.source.repository, 1, pull=True, add_labels=["self-tested"])

    def test_native_roadmap_receipt_through_persistent_label(self):
        from types import SimpleNamespace

        from tauceti_worker import self_tested_label, verification

        root, state = self.fixture.clone / "content", self.fixture.clone / "verification-state"
        (root / "TauCeti").mkdir(parents=True)

        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.com")
        (root / "TauCeti/A.lean").write_text("-- base\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("remote", "add", "origin", self.source.repository.clone_url)
        git("update-ref", "refs/remotes/origin/main", base)
        (root / "TauCeti/A.lean").write_text("-- verified change\n")
        git("commit", "-q", "-am", "change")
        head = git("rev-parse", "HEAD")
        self.live["head"].update(sha=head, ref="roadmap/x")
        self.source.reader.pulls = Mock(
            return_value=[dict(number=1, headRefName="roadmap/x", headRepositoryOwner=dict(login="team"))]
        )
        project = self.fixture.clone / "project.toml"
        self.source.write_cli_configuration(project)
        inputs = dict(base=base, sources=verification.inventory(root, base))
        fingerprint = verification.digest(inputs)
        inputs_path = state / "inputs.json"
        inputs_path.parent.mkdir()
        inputs_path.write_text(json.dumps(inputs))
        receipt = state / "verification" / "run" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                dict(
                    schema=verification.SCHEMA,
                    status="success",
                    requested_phases=list(verification.STAGES),
                    phases={name: dict(status="success") for name in verification.STAGES},
                    scope=dict(policy="ordinary"),
                    inputs=dict(path=str(inputs_path), sha256=fingerprint),
                    fingerprint=fingerprint,
                )
            )
        )
        worker = SimpleNamespace(gh=self.source, cfg=SimpleNamespace(checkout=root, state=state))
        candidate = SimpleNamespace(
            pr=None, progress=dict(published_branches={"roadmap/x": head}, published_owner="team")
        )
        with (
            patch.dict(os.environ, TAUCETI_SELF_TESTED="1", TAUCETI_SOURCE_GH_CONF=str(project)),
            patch("tauceti_worker.worker_sources.canonical_mathematics_source", return_value=True),
        ):
            os.environ.pop("TAUCETI_CONF", None)
            result = self_tested_label.label_round_pushes(worker, candidate, "roadmap", time.time() - 5)
            self.assertNotIn("TAUCETI_CONF", os.environ)
        self.assertEqual(result, [(1, f"labelled {head[:12]}: persistent self-tested label applied")])
        self.source.add_label.assert_called_once_with(1, "self-tested")

    def test_native_checkout_separates_local_branch_from_remote_pr_branch(self):
        from tauceti_worker.source_cli import checkout

        row = dict(head=dict(sha="a" * 40, ref="remote/pr-branch", repo=dict(full_name="team/Math")))
        self.fixture.api.pull = Mock(return_value=row)
        with (
            patch("tauceti_worker.source_cli.subprocess.run") as run,
            patch("tauceti_worker.source_cli.subprocess.check_output", return_value="a" * 40 + "\n"),
        ):
            self.assertEqual(checkout(["1", "--force", "--branch", "tauceti/worker/pr-1"], self.source), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "checkout", "--force", "-B", "tauceti/worker/pr-1", "a" * 40], commands)
        self.assertIn(["git", "config", "branch.tauceti/worker/pr-1.merge", "refs/heads/remote/pr-branch"], commands)
        self.assertTrue(
            any(
                command[:2] == ["git", "fetch"] and command[-1] == "refs/heads/remote/pr-branch" for command in commands
            )
        )


class CanonicalSource(unittest.TestCase):
    def test_only_enrolled_native_content_repository_qualifies(self):
        from types import SimpleNamespace

        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        from tauceti_worker.agents import self_tested_guidance
        from tauceti_worker.self_tested_label import enabled

        with patch.dict(os.environ, TAUCETI_SELF_TESTED="1"):
            for origin, name, native_id, eligible in (
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 1, True),
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 2, False),
                ("https://elsewhere.example", "TauCetiProject/TauCeti", 1, False),
                ("https://git.taucetiproject.org", "Another/TauCeti", 1, False),
            ):
                repository = RepositoryRef(Forge("forgejo", origin), name)
                source = SimpleNamespace(repository=repository, identity=RepositoryIdentity(repository, native_id))
                with patch("tauceti_worker.worker_sources.current", return_value=source):
                    self.assertEqual(enabled() == "", eligible)
                    self.assertEqual(bool(self_tested_guidance("ordinary")), eligible)
                    self.assertFalse(self_tested_guidance("full-lint"))


def shared_discovery_child(directory, start=None, outcome="success", fallback=None):
    import fcntl

    from tauceti_tools.ci_candidate import RepositoryIdentity
    from tauceti_tools.forge import Forge, RepositoryRef

    from tauceti_worker import github_transport as transport
    from tauceti_worker.source_identity import discovery_scope
    from tauceti_worker.source_survey import discovery_inventory

    repository = RepositoryRef(Forge("forgejo", "https://example.test"), "team/project")

    def fetch(**kwargs):
        with (Path(directory) / "fetches").open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            ordinal = len(f.read().splitlines()) + 1
            f.write("network\n")
        if outcome == "legacy-invalidate" and ordinal == 1:
            transport.invalidate()
        if outcome == "invalidate" and ordinal == 1:
            transport.invalidate_shared(discovery_scope(source))
        if outcome == "exception":
            if ordinal == 1:
                time.sleep(0.15)
                raise OSError("network unavailable")
            else:
                fallback.wait(10)
        if outcome == "storage":
            fallback.wait(10)
        time.sleep(0.15)
        return None if outcome in {"unavailable", "legacy-unavailable"} else [dict(number=1)]

    source = SimpleNamespace(
        repository=repository,
        identity=RepositoryIdentity(repository, 1),
        cache_namespace="a" * 64,
        shared_inventory=True,
        inventory_fields=("number",),
        open_prs=fetch,
    )
    original = transport.locked
    first = True

    @contextlib.contextmanager
    def synchronised(*args, **kwargs):
        nonlocal first
        if start is not None and first:
            first = False
            start.wait(10)
        with original(*args, **kwargs):
            yield

    storage = (
        patch.object(transport, "atomic", side_effect=OSError("cache unavailable"))
        if outcome == "storage"
        else contextlib.nullcontext()
    )
    with patch.object(transport, "locked", side_effect=synchronised), storage:
        try:
            if outcome.startswith("legacy-"):
                attempts = []

                def fetch_with_side_effect():
                    attempts.append("attempted")
                    return fetch()

                value = transport.shared_read(
                    ["legacy-identity"],
                    fetch_with_side_effect,
                    identity=source.cache_namespace,
                    ttl=0 if outcome == "legacy-unavailable" else 60,
                )
                if value is None:
                    assert attempts == ["attempted"], "identity failure must retain caller's attempt evidence"
            else:
                value = discovery_inventory(source)
        except OSError:
            assert outcome == "exception"
        else:
            assert value == (None if outcome in {"unavailable", "legacy-unavailable"} else [dict(number=1)])


class SharedDiscovery(unittest.TestCase):
    def setUp(self):
        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=self.directory.name, TAUCETI_GITHUB_CACHE_BYPASS="0")
        env.start()
        self.addCleanup(env.stop)
        repository = RepositoryRef(Forge("forgejo", "https://example.test"), "team/project")
        self.source = SimpleNamespace(
            repository=repository,
            identity=RepositoryIdentity(repository, 1),
            cache_namespace="a" * 64,
            shared_inventory=True,
            inventory_fields=("number",),
            open_prs=Mock(side_effect=lambda **kw: [dict(number=self.source.open_prs.call_count)]),
        )

    def read(self, **kwargs):
        from tauceti_worker.source_survey import discovery_inventory

        return discovery_inventory(self.source, **kwargs)

    def test_shared_read_and_caller_mutation(self):
        value = self.read()
        value[0]["number"] = 999
        self.assertEqual(self.read(), [dict(number=1)])
        self.assertEqual(self.source.open_prs.call_count, 1)
        path = next((Path(self.directory.name) / "cache" / self.source.cache_namespace).glob("*.json"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text("invalid JSON")
        self.assertEqual(self.read(), [dict(number=2)])

    def test_explicit_target_and_fresh_context_bypass_shared_discovery(self):
        from tauceti_worker import github_transport as transport

        self.assertEqual(self.read(), [dict(number=1)])
        self.assertEqual(self.read(numbers={1}), [dict(number=2)])
        with transport.read_context(fresh=True):
            self.assertEqual(self.read(), [dict(number=3)])
        self.assertEqual(self.read(), [dict(number=1)])
        self.source.shared_inventory = False
        self.assertEqual(self.read(), [dict(number=4)])

    def test_credential_repository_native_id_and_revision_isolation(self):
        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import RepositoryRef

        from tauceti_worker import github_transport as transport

        self.assertEqual(self.read(), [dict(number=1)])
        self.source.cache_namespace = "b" * 64
        self.assertEqual(self.read(), [dict(number=2)])
        self.source.repository = RepositoryRef(self.source.repository.forge, "other/project")
        self.assertEqual(self.read(), [dict(number=3)])
        self.source.identity = RepositoryIdentity(self.source.repository, 2)
        self.assertEqual(self.read(), [dict(number=4)])
        with transport.read_context(revision="new-observation"):
            self.assertEqual(self.read(), [dict(number=5)])
        self.source.inventory_fields = ("number", "title")
        self.assertEqual(self.read(), [dict(number=6)])

    def test_only_qualified_source_invalidation_expires_discovery(self):
        from tauceti_worker import github_transport as transport
        from tauceti_worker.source_identity import discovery_scope

        self.assertEqual(self.read(), [dict(number=1)])
        transport.invalidate()
        self.assertEqual(self.read(), [dict(number=1)])
        transport.invalidate_shared(discovery_scope(self.source))
        self.assertEqual(self.read(), [dict(number=2)])

    def test_expiry_and_unavailable_reads_are_not_reused(self):
        now = time.time()
        self.assertEqual(self.read(), [dict(number=1)])
        with patch("tauceti_worker.github_transport.time.time", return_value=now + 61):
            self.assertEqual(self.read(), [dict(number=2)])
        self.source.cache_namespace = "c" * 64
        self.source.open_prs.side_effect = [None, [dict(number=3)]]
        self.assertIsNone(self.read())
        self.assertEqual(self.read(), [dict(number=3)])

    def concurrent(self, outcome="success"):
        context = multiprocessing.get_context("fork")
        start, fallback = context.Barrier(4), context.Barrier(4 if outcome == "storage" else 3)
        children = [
            context.Process(target=shared_discovery_child, args=(self.directory.name, start, outcome, fallback))
            for _ in range(4)
        ]
        for child in children:
            child.start()

        def reap():
            for child in children:
                if child.is_alive():
                    child.terminate()
            for child in children:
                child.join(10)

        self.addCleanup(reap)
        for child in children:
            child.join(15)
            self.assertEqual(child.exitcode, 0)
        return (Path(self.directory.name) / "fetches").read_text().splitlines()

    def test_default_failed_identity_reads_preserve_each_callers_fetch_side_effects(self):
        self.assertEqual(self.concurrent("legacy-unavailable"), ["network"] * 4)

    def test_default_generation_change_refetches_once_then_shares(self):
        self.assertEqual(self.concurrent("legacy-invalidate"), ["network"] * 2)

    def test_concurrent_processes_fetch_once(self):
        self.assertEqual(self.concurrent(), ["network"])

    def test_concurrent_unavailable_read_is_coalesced_without_caching_failure(self):
        self.assertEqual(self.concurrent("unavailable"), ["network"])
        self.assertEqual(self.read(), [dict(number=1)])
        self.assertEqual(self.source.open_prs.call_count, 1)

    def test_generation_change_refetches_once_then_shares_under_lock(self):
        self.assertEqual(self.concurrent("invalidate"), ["network"] * 2)

    def test_fetch_exception_retries_waiters_outside_the_lock(self):
        self.assertEqual(self.concurrent("exception"), ["network"] * 4)

    def test_unavailable_storage_does_not_serialize_network_fetches(self):
        self.assertEqual(self.concurrent("storage"), ["network"] * 4)


if __name__ == "__main__":
    unittest.main()
