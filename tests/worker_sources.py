#!/usr/bin/env python3
"""Cross-forge routing, independent outages, identities and budgets."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_worker.config import Config, Die
from tauceti_worker.multi_source import collect, deduplicate
from tauceti_worker.review_state import Meta
from tauceti_worker.source_cli import api_command, checkout
from tauceti_worker.source_execution import still_actionable
from tauceti_worker.source_identity import SourceActor
from tauceti_worker.source_survey import worker_survey
from tauceti_worker.survey import Candidate, Counters, PRInfo, Survey
from tauceti_worker.worker_sources import configure, source_config


class Sources(unittest.TestCase):
    def test_native_cli_token_loading_does_not_require_host_root_uid_mapping(self):
        from tauceti_queue.control_policy_file import ControlPolicyError
        from tauceti_worker.source_cli import load_source
        from tauceti_worker.worker_sources import native_source

        credential = self.root / "worker-token"
        credential.write_text("private-test-token\n")
        credential.chmod(0o600)
        environment = dict(
            TAUCETI_SOURCE_BINDING=json.dumps(self.identity.record()),
            TAUCETI_FORGEJO_ORIGIN=self.repo.forge.url,
            TAUCETI_FORGEJO_REPO=self.repo.name,
            TAUCETI_FORGEJO_REPOSITORY_ID=str(self.identity.native_id),
            TAUCETI_FORGEJO_TOKEN_FILE=str(credential),
            TAUCETI_FORGEJO_USER_ID="3",
        )
        with (
            patch.dict(os.environ, environment),
            patch("tauceti_queue.control_policy_file._directory", side_effect=ControlPolicyError("unmapped root UID")),
            patch("tauceti_worker.forgejo_worker.ForgejoWorkerSource", return_value=self.source),
            patch("tauceti_tools.forge.Forgejo") as client,
        ):
            self.assertIs(native_source(self.cfg), self.source)
            self.assertIs(self.source.shared_inventory, True)
            client.reset_mock()
            self.assertIs(load_source(), self.source)
            client.assert_called_once_with(self.repo.forge, token="private-test-token")
            self.source.validate_binding.assert_called_once()
            credential.chmod(0o644)
            with self.assertRaises(ValueError):
                load_source()
            credential.chmod(0o600)
            link = self.root / "symlink-token"
            link.symlink_to(credential)
            os.environ["TAUCETI_FORGEJO_TOKEN_FILE"] = str(link)
            with self.assertRaises(OSError):
                load_source()

    def test_native_author_claim_uses_shared_github_namespace_resolution(self):
        from tauceti_worker.source_claims import main

        with (
            patch.object(sys, "argv", ["claim-source", "acquire", "author/Algebra/test"]),
            patch.dict(os.environ, {"TAUCETI_WORKER_ID": "native-worker"}),
            patch("tauceti_worker.worker_sources.shared_github_environment", return_value=contextlib.nullcontext()),
            patch("tauceti_worker.github.claims_repo", return_value="team/shared-claims"),
            patch("tauceti_tools.claims.GitClaim") as claim,
        ):
            claim.return_value.command.return_value = 0
            self.assertEqual(main(), 0)
            self.assertEqual(claim.call_args.args[:3], ("team/shared-claims", "author/Algebra/test", "native-worker"))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/project")
        self.identity = RepositoryIdentity(self.repo, 7)
        self.actor = SourceActor.from_native(self.repo.forge, {"id": 3, "login": "worker"})
        self.source = Mock(
            repository=self.repo, identity=self.identity, repo=self.repo.name, review_repository=self.repo.key
        )
        self.source.authenticated_actor.return_value = self.actor
        self.source.binding_current.return_value = True
        self.source.fresh_claim_age.return_value = None
        self.cfg = Config(
            "worker",
            self.root,
            self.root,
            self.root / "state",
            self.root / "checkout",
            self.root / "store",
            self.root / "scoreboard",
            self.root / "logs",
            self.root / "quota",
        )

    def checkout_fixture(self):
        def git(*args, cwd):
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        remote = self.root / "remote"
        remote.mkdir()
        git("init", "-q", "-b", "main", cwd=remote)
        git("commit", "-q", "--allow-empty", "-m", "base", cwd=remote)
        git("checkout", "-q", "-b", "patch-1", cwd=remote)
        git("commit", "-q", "--allow-empty", "-m", "proposal", cwd=remote)
        sha = git("rev-parse", "HEAD", cwd=remote)
        local = self.root / "local"
        git("clone", "-q", str(remote), str(local), cwd=self.root)
        self.source.api.pull.return_value = {
            "head": {"sha": sha, "ref": "patch-1", "repo": {"full_name": "one/project"}}
        }
        return remote, local, sha, git

    def test_pr_checkout_accepts_the_worker_branch_that_round_worktrees_pass(self):
        from tauceti_worker.round_worktrees import local_branch

        remote, local, sha, git = self.checkout_fixture()
        branch = local_branch(self.cfg, 9)
        cwd = os.getcwd()
        os.chdir(local)
        self.addCleanup(os.chdir, cwd)
        with patch("tauceti_tools.forge.RepositoryRef", return_value=Mock(clone_url=str(remote))):
            # The same argument shape as the fix/rebase checkout in work_units.
            self.assertEqual(checkout(["9", "--force", "--branch", branch], self.source), 0)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=local), branch)
        self.assertEqual(git("rev-parse", "HEAD", cwd=local), sha)
        self.assertEqual(git("config", f"branch.{branch}.merge", cwd=local), "refs/heads/patch-1")
        self.assertEqual(git("config", f"branch.{branch}.remote", cwd=local), str(remote))
        # Without --branch the local branch keeps the PR's own name, as gh does.
        with patch("tauceti_tools.forge.RepositoryRef", return_value=Mock(clone_url=str(remote))):
            self.assertEqual(checkout(["9", "--force"], self.source), 0)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=local), "patch-1")

    def row(self, number=1, *, author=3, login="worker", head="a" * 40):
        return {
            "number": number,
            "title": "Proposal",
            "body": "",
            "headRefOid": head,
            "headRefName": "proposal",
            "headRepository": {"nameWithOwner": self.repo.name, "name": "project"},
            "headRepositoryOwner": {"login": "one"},
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "author": {"id": self.repo.forge.key + "/users/" + str(author), "login": login},
            "labels": [],
            "updatedAt": "2026-11-11T00:00:00Z",
        }

    def native_survey(self, rows, *, blocking=False, metadata=None, numbers=()):
        self.source.open_prs.return_value = rows
        rs = Mock()
        if metadata is None and blocking:
            metadata = Meta({"head_sha": "a" * 40, "states": {"style": "blocking_request"}}, "fresh")
        rs.gh_meta.return_value = metadata or Meta({}, "missing")
        rs.newest_contest_reply.return_value = None
        rs.ledger_clean_head.return_value = ""
        rs.ledger_blocking.return_value = blocking
        cfg = source_config(self.cfg, self.source)
        return worker_survey(
            cfg,
            self.source,
            rs,
            Counters(cfg),
            roadmap="any",
            skip=[],
            deep=True,
            stages=["review", "fix", "rebase"],
            numbers=numbers,
            count_all_roadmaps=True,
        )

    def test_shared_inventory_surveys_preserve_explicit_targets_and_binding_checks(self):
        self.source.shared_inventory = True
        self.source.cache_namespace = "d" * 64
        self.source.inventory_fields = tuple(self.row())
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=str(self.root / "shared-discovery")):
            first = self.native_survey([self.row()])
            cached = self.native_survey([self.row(2)])
            self.assertEqual([p.number for p in first.open_prs], [1])
            self.assertEqual([p.number for p in cached.open_prs], [1])
            self.assertEqual(self.source.open_prs.call_count, 1)
            explicit = self.native_survey([self.row(2)], numbers=(2,))
            self.assertEqual([p.number for p in explicit.open_prs], [2])
            self.assertEqual(self.source.open_prs.call_count, 2)
            self.source.binding_current.return_value = False
            rejected = self.native_survey([self.row(2)])
            self.assertTrue(rejected.github_failed)
            self.assertEqual(rejected.dependency_wait["operation"], "binding")

    def test_bound_round_gh_runs_this_runtimes_shim_in_the_round_checkout(self):
        from tauceti_worker import github, worker_sources

        checkout = self.root / "checkout"
        # A PR checkout is untrusted: it must not shadow the runtime's own modules.
        (checkout / "tauceti_worker").mkdir(parents=True)
        (checkout / "tauceti_worker" / "__init__.py").write_text("raise SystemExit('shadowed')\n")
        self.source.token_file = self.root / "token"
        self.source.identity = self.identity
        marker = worker_sources._selected.set(self.source)
        self.addCleanup(worker_sources._selected.reset, marker)
        with (
            patch.dict(os.environ, {"PATH": str(self.root / "no-gh-here")}),
            patch("tauceti_worker.source_cli.subprocess.run") as run,
        ):
            github.gh_run(["gh", "pr", "checkout", "9", "--force"], cwd=checkout)
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:4], ["-P", "-m", "tauceti_worker.source_cli"])
        self.assertEqual(argv[4:], ["pr", "checkout", "9", "--force"])
        self.assertEqual(kwargs["cwd"], checkout)
        probe = subprocess.run(
            [sys.executable, "-P", "-c", "import tauceti_worker; print(tauceti_worker.__file__)"],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertNotIn(str(checkout), probe.stdout)

    def test_default_sources_and_new_pr_destination(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace())
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "github")

    def test_api_reads_use_the_shared_contract_instead_of_raw_native_pagination(self):
        from tauceti_tools.forge_command import parse_command

        for endpoint in [
            "user",
            "repos/one/project",
            "repos/one/project/pulls/1",
            "repos/one/project/issues/1/comments",
        ]:
            with self.subTest(endpoint=endpoint):
                args = ["api", endpoint]
                if endpoint.endswith("/comments"):
                    args.append("--paginate")
                self.assertIsNone(api_command(parse_command(args), self.source))
        self.source.api.request.assert_not_called()
        self.source.api.collection.assert_not_called()

    def test_worker_inline_thread_read_keeps_normalized_parent_links(self):
        from tauceti_tools.forge_command import parse_command

        self.source.review_comments.return_value = [{"id": 9, "in_reply_to_id": 8}]
        with patch("sys.stdout"):
            self.assertEqual(
                api_command(parse_command(["api", "repos/one/project/pulls/1/comments", "--paginate"]), self.source), 0
            )
        self.source.review_comments.assert_called_once_with(1)
        self.source.api.collection.assert_not_called()

    def run_cli(self, args):
        from tauceti_worker.source_cli import main

        with (
            patch("tauceti_worker.source_cli.load_source", return_value=self.source),
            patch.dict(os.environ, TAUCETI_SOURCE_GH_CONF="/fixture/project.toml"),
            patch("tauceti_tools.forge_cli.run", return_value=0) as run,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(main(args), 0)
        return output.getvalue(), run

    def test_agent_open_listing_is_the_shared_survey_inventory(self):
        self.source.shared_inventory = False
        self.source.cache_namespace = "e" * 64
        self.source.inventory_fields = tuple(self.row())
        self.source.open_prs.return_value = [self.row(3), self.row(2)]
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=str(self.root / "shared")):
            listing = ["pr", "list", "--repo", "one/project", "--limit", "1000", "--json", "number,title"]
            output, run = self.run_cli(listing)
            self.assertEqual(json.loads(output), [dict(number=3, title="Proposal"), dict(number=2, title="Proposal")])
            with patch.dict(os.environ, GH_REPO="git.example.org/one/project"):
                output, _ = self.run_cli(["pr", "list", "-L", "1", "--json", "number", "--jq", ".[].number"])
            self.assertEqual(output, "3\n")
        run.assert_not_called()
        self.assertEqual(self.source.open_prs.call_count, 1, "agents share the host's listing")

    def test_agent_closed_listings_read_the_issue_index_without_pull_rendering(self):
        def issue(number, merged):
            return dict(
                number=number,
                title=f"PR {number}",
                body="",
                state="closed",
                user=dict(id=3, login="worker"),
                labels=[],
                created_at="2026-11-01T00:00:00Z",
                updated_at="2026-11-02T00:00:00Z",
                closed_at="2026-11-02T00:00:00Z",
                pull_request=dict(merged=merged, merged_at="2026-11-02T00:00:00Z" if merged else None),
            )

        self.source.api.pages.return_value = iter(
            [[issue(5, True), issue(4, False)], [issue(4, False), issue(3, True)]]
        )
        output, run = self.run_cli(
            ["pr", "list", "-R", "one/project", "--state", "merged", "--limit", "5000", "--json", "number,state,url"]
        )
        self.assertEqual(
            json.loads(output),
            [dict(number=n, state="MERGED", url=self.repo.pull_url(n)) for n in (5, 3)],
        )
        self.assertEqual(self.source.api.pages.call_args.args, ("repos/one/project/issues?type=pulls&state=closed",))
        landed = issue(6, False) | dict(title="[Merged by Bors] - feat: landed")
        self.source.api.pages.return_value = iter([[landed, issue(5, True), issue(4, False), issue(3, True)]])
        output, _ = self.run_cli(
            ["pr", "list", "-R", "one/project", "--state", "merged", "--limit", "2", "--json", "number,mergedAt"]
        )
        self.assertEqual(
            json.loads(output),
            [dict(number=6, mergedAt="2026-11-02T00:00:00Z"), dict(number=5, mergedAt="2026-11-02T00:00:00Z")],
            "a PR the queue landed counts as merged",
        )
        self.source.api.pages.return_value = iter([[landed, issue(5, True), issue(4, False), issue(3, True)]])
        output, _ = self.run_cli(["pr", "list", "-R", "one/project", "--state", "closed", "--json", "number,state"])
        self.assertEqual(json.loads(output), [dict(number=4, state="CLOSED")], "landed PRs are not closed")
        self.source.api.pulls.assert_not_called()
        run.assert_not_called()

    def test_agent_listings_the_inventory_and_index_cannot_answer_go_to_tools(self):
        self.source.inventory_fields = ("number",)
        for args in (
            ["pr", "list", "-R", "one/project"],
            ["pr", "list", "-R", "one/project", "--json", "number,headRefOid", "--state", "merged"],
            ["pr", "list", "-R", "one/project", "--json", "number", "--author", "worker"],
            ["pr", "list", "--json", "number", "--repo", "other/project"],
            ["pr", "list", "--json", "number"],  # the checkout's repository, which Tools resolves
        ):
            with self.subTest(args=args):
                _, run = self.run_cli(args)
                run.assert_called_once()
        self.source.api.pages.assert_not_called()
        self.source.open_prs.assert_not_called()

    def test_cli_qualifies_exact_native_repository_endpoints(self):
        from tauceti_worker.source_cli import main

        for endpoint in ["repos/one/project", "repos/one/project?unused=value", "repos/one/project/issues/1/comments"]:
            with (
                self.subTest(endpoint=endpoint),
                patch("tauceti_worker.source_cli.load_source", return_value=self.source),
                patch.dict(os.environ, TAUCETI_SOURCE_GH_CONF="/fixture/project.toml"),
                patch("tauceti_tools.forge_cli.run", return_value=0) as run,
            ):
                self.assertEqual(main(["api", endpoint]), 0)
                self.assertEqual(run.call_args.args[0][-2:], ["--hostname", "git.example.org"])
        self.source.api.request.assert_not_called()

    def test_cli_uses_shared_synthetic_comment_pages_once_each(self):
        from tauceti_worker.source_cli import main

        configuration = self.root / "native.toml"
        configuration.write_text("""schema_version=2
[forges.native]
kind="forgejo"
url="https://git.example.org"
[repositories]
content={forge="native", name="one/project"}
roadmap="one/roadmap"
ci="one/project"
""")
        api = Mock(forge=self.repo.forge, max_bytes=1024 * 1024, max_pages=10)
        api.comments.return_value = [
            dict(
                id=i,
                body="comment",
                created_at="now",
                updated_at="now",
                user=None,
                html_url=self.repo.pull_url(1) + f"#issuecomment-{i}",
            )
            for i in range(1, 36)
        ]
        with (
            patch("tauceti_worker.source_cli.load_source", return_value=self.source),
            patch.dict(os.environ, {"TAUCETI_SOURCE_GH_CONF": str(configuration)}, clear=True),
            patch("tauceti_tools.forge_cli.Forgejo", return_value=api),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(main(["api", "repos/one/project/issues/1/comments", "--paginate", "--slurp"]), 0)
        pages = json.loads(output.getvalue())
        self.assertEqual([len(page) for page in pages], [30, 5])
        self.assertEqual([row["id"] for page in pages for row in page], list(range(1, 36)))
        api.comments.assert_called_once_with(self.repo, 1)
        api.collection.assert_not_called()

    def test_cli_explains_unsupported_operations_without_leaking_transport_errors(self):
        from tauceti_tools.forge import ForgeError, Unsupported

        from tauceti_worker.source_cli import main

        for error, expected in [
            (Unsupported("unsupported JSON fields: commits"), "unsupported JSON fields: commits"),
            (ForgeError("secret transport context"), "source operation unavailable (ForgeError)"),
        ]:
            with (
                self.subTest(error=type(error).__name__),
                patch("tauceti_worker.source_cli.load_source", side_effect=error),
                patch("sys.stderr", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(main(["api", "user"]), 1)
                self.assertEqual(output.getvalue(), "gh: " + expected + "\n")

    def test_explicit_numeric_target_preserves_github_default(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace(pr=["12"]))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")
            with self.assertRaises(Die):
                configure(SimpleNamespace(pr=["12"], forgejo=True))

    def test_native_destination_is_independent(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace(forgejo=True, new_pr_forge="forgejo"))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "forgejo")

    def test_native_budgets_preserve_github_files_and_separate_server_ids(self):
        self.cfg.state.mkdir()
        (self.cfg.state / "fix-1-aaaaaaaaaaaa").write_text("2")
        cfg = source_config(self.cfg, self.source)
        self.assertEqual(Counters(cfg).read("fix-1-aaaaaaaaaaaa"), 0)
        Counters(cfg).write("fix-1-aaaaaaaaaaaa", 1)
        self.assertEqual(Counters(self.cfg).read("fix-1-aaaaaaaaaaaa"), 2)
        other = Mock(repository=self.repo, identity=RepositoryIdentity(self.repo, 8))
        self.assertNotEqual(source_config(self.cfg, other).state, cfg.state)

    def test_native_review_selection_keeps_qualified_identity_and_unknown_ci(self):
        result = self.native_survey([self.row()])
        self.assertIsNone(result.dependency_wait)
        self.assertEqual(len(result.reviewable.actionable), 1)
        candidate = result.reviewable.actionable[0]
        self.assertEqual(candidate.source_repository, self.repo)
        self.assertEqual(candidate.source_repository_id, 7)
        self.assertFalse(result.open_prs[0].build_success)

    def test_same_login_does_not_authorize_repair_for_another_native_actor(self):
        result = self.native_survey([self.row(1), self.row(2, author=4)], blocking=True)
        self.assertEqual([candidate.pr for candidate in result.needs_fix.actionable], [1])

    def test_adopted_native_author_is_repaired_without_counting_as_authored(self):
        fork = {**self.row(3, author=4), "headRepository": {"nameWithOwner": "kim/project", "name": "project"}}
        with patch.dict(os.environ, {"TAUCETI_FORGEJO_ADOPTED_AUTHOR_IDS": "4"}):
            result = self.native_survey([self.row(1), self.row(2, author=4), fork], blocking=True)
        self.assertEqual([candidate.pr for candidate in result.needs_fix.actionable], [1, 2])
        self.assertEqual([p.number for p in result._mine_open_prs], [1])

    def test_adopted_native_author_authorizes_only_same_repository_repairs(self):
        w = self.native_worker()
        w.gh.authenticated_actor = Mock(return_value=self.actor)
        w.gh.validate_binding = Mock()
        candidate = Candidate(1, "a" * 40, source_repository=self.repo, source_repository_id=7)
        same = {"user": {"id": 4}, "head": {"repo": {"id": 7}}}
        fork = {"user": {"id": 4}, "head": {"repo": {"id": 8}}}
        with patch.dict(os.environ, {"TAUCETI_FORGEJO_ADOPTED_AUTHOR_IDS": "4"}):
            w.gh.live_pull = Mock(return_value=same)
            self.assertTrue(w.gh.authorize_candidate(candidate, require_writer=True))
            w.gh.live_pull = Mock(return_value=fork)
            self.assertFalse(w.gh.authorize_candidate(candidate, require_writer=True))
        w.gh.live_pull = Mock(return_value=same)
        self.assertFalse(w.gh.authorize_candidate(candidate, require_writer=True))

    def test_unavailable_history_never_becomes_unreviewed_work(self):
        result = self.native_survey([self.row()], metadata=Meta({}, "fetch_failed"))
        self.assertFalse(result.reviewable.actionable)
        self.assertEqual(result.discovery_gap_prs, [1])

    def test_unavailable_native_inventory_stays_a_failure(self):
        result = self.native_survey(None)
        self.assertTrue(result.github_failed)
        self.assertFalse(result.reviewable.actionable)

    def test_fresh_dispatch_rejects_a_candidate_from_the_other_source(self):
        candidate = Candidate(
            1, "a" * 40, source_repository=RepositoryRef(GITHUB, self.repo.name), source_repository_id=7
        )
        self.assertFalse(still_actionable("review", SimpleNamespace(gh=self.source), None, candidate))
        self.source.live_pull.assert_not_called()

    def native_worker(self):
        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        api = Mock(forge=self.repo.forge, _token="token")
        source = ForgejoWorkerSource(self.identity, api, token_file=self.root / "token")
        return SimpleNamespace(gh=source, cfg=self.cfg, counters=Mock(), rs=Mock())

    def test_native_prompt_rewrites_only_complete_content_repository_references(self):
        from tauceti_worker.agents import TAUCETI, fill_prompt

        template = self.root / "prompt.txt"
        canonical = "https://github.com/" + TAUCETI
        shared = [TAUCETI + suffix for suffix in ("Roadmap", "Data", "Review", "-claims", ".archive", ".git-other")]
        lines = [
            canonical,
            canonical + "/pull/7",
            canonical + ".git",
            "--repo " + TAUCETI,
            canonical + ".",
            canonical + ",",
            "--repo " + TAUCETI + ")",
        ]
        for name in shared:
            lines.extend(["https://github.com/" + name, "--repo " + name])
        template.write_text("\n".join(lines) + "\n__CLAIMED__")
        untrusted = canonical + " --repo " + TAUCETI
        with patch("tauceti_worker.worker_sources.current", return_value=self.source):
            rendered = fill_prompt(template, CLAIMED=untrusted).splitlines()
        native = self.repo.forge.url.removeprefix("https://") + "/" + self.repo.name
        self.assertEqual(
            rendered[:7],
            [
                self.repo.url,
                self.repo.url + "/pull/7",
                self.repo.url + ".git",
                "--repo " + native,
                self.repo.url + ".",
                self.repo.url + ",",
                "--repo " + native + ")",
            ],
        )
        self.assertEqual(rendered[7:-1], lines[7:])
        self.assertEqual(rendered[-1], untrusted)

    def test_git_mergeability_is_only_requested_for_owned_unpaused_prs(self):
        source = self.native_worker().gh
        source.authenticated_actor = Mock(return_value=self.actor)
        source.binding_current = Mock(return_value=True)
        source.pr_progress_state = Mock(return_value=None)
        rows = [self.row(number=n, author=4 if n == 2 else 3) for n in range(1, 5)]
        for row in rows:
            row["mergeable"] = "UNKNOWN"
        rows[2]["labels"] = [{"name": "hold"}]
        rows[3]["isDraft"] = True
        source.open_prs = Mock(return_value=rows)
        cfg = source_config(self.cfg, source)
        with patch.object(type(source), "resolve_mergeability", return_value="CONFLICTING") as proof:
            worker_survey(
                cfg,
                source,
                Mock(),
                Counters(cfg),
                roadmap="any",
                skip=[],
                deep=True,
                stages=["rebase"],
                numbers=(),
                count_all_roadmaps=True,
            )
        proof.assert_called_once_with(source, cfg.state, 1, "a" * 40)

    def test_authoring_has_no_pull_request_for_the_native_source_to_authorize(self):
        worker = self.native_worker()
        self.assertTrue(still_actionable("roadmap", worker, None, Candidate(0, "", "Algebra")))
        self.assertFalse(still_actionable("review", worker, None, Candidate(1, "a" * 40)))

    def test_dispatch_authors_on_the_native_source(self):
        from tauceti_worker import work_units
        from tauceti_worker.config import NoProgress

        worker = self.native_worker()
        worker.gh.pr_list = Mock(return_value=[])
        options = work_units.RoundOpts(
            only=["roadmap"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False
        )
        with (
            patch.dict(os.environ),
            patch.object(work_units.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(work_units, "_check_stage_read_quota"),
            patch.object(work_units, "do_roadmap", return_value=0) as author,
            patch("tauceti_worker.activity.select"),
            # The stubbed round opens no pull request, so the progress guard ends it afterwards.
            self.assertRaisesRegex(NoProgress, "nothing landed"),
        ):
            work_units.dispatch("roadmap", worker, Survey("worker"), Candidate(0, "", "Algebra"), options)
        author.assert_called_once()

    def test_native_authoring_after_a_declined_github_review_pushes_to_the_native_fork(self):
        from tauceti_worker import github, work_units
        from tauceti_worker.config import NoProgress
        from tauceti_worker.multi_source import run_round

        for cached in (github.ensure_fork, github._resolve_claims_repo):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        reviewing = SimpleNamespace(
            cfg=self.cfg,
            gh=Mock(repo=github.TAUCETI, worker_capable=False),
            rs=Mock(),
            counters=Mock(read=Mock(return_value=0)),
        )
        review_work = Survey("worker")
        review_work.reviewable.actionable.append(Candidate(1, "a" * 40))
        self.source.token_file = self.root / "token"
        self.source.api.request.return_value = {"id": 3, "email": "worker@example.org"}
        self.source.writable_repository.return_value = RepositoryRef(self.repo.forge, "native-user/TauCeti")
        self.source.pr_list.return_value = []
        authoring = SimpleNamespace(cfg=self.cfg, gh=self.source, rs=Mock(), counters=Mock(read=Mock(return_value=0)))
        observations = [("github", reviewing, review_work), ("forgejo", authoring, Survey("worker"))]
        options = work_units.RoundOpts(
            only=["review", "roadmap"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False
        )
        launches = []

        def launch(checkout, prompt, *args, **kwargs):
            launches.append((dict(os.environ), prompt))
            return 0

        with (
            patch.dict(os.environ, {"TAUCETI_NEW_PR_FORGE": "forgejo"}),
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch.object(github, "shared_claims_granted", return_value=False),
            patch.object(github, "_resolve_fork", return_value="github-user/TauCeti"),
            patch.object(github, "can_push", return_value=True),
            patch.object(work_units, "_claim_held_elsewhere", return_value=True),
            patch.object(work_units, "mirror_creds"),
            patch("tauceti_worker.review_diagnostics.refund_stale_head_errors"),
            patch("tauceti_worker.activity.select"),
            patch.object(work_units.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(work_units, "_check_stage_read_quota"),
            patch.object(work_units, "avoid_list", return_value="none"),
            patch.object(work_units, "fetch_ref", return_value=True),
            patch.object(work_units, "stage_rubrics", return_value=None),
            patch.object(work_units, "prepare_checkout", return_value=True),
            patch.object(work_units, "run_agent_host", side_effect=launch),
            # The agent opens no pull request, so the progress guard ends the round afterwards.
            self.assertRaisesRegex(NoProgress, "nothing landed"),
        ):
            os.environ.pop("CLAIM_REPO", None)
            os.environ.pop("TAUCETI_FORK", None)
            run_round(None, options)
        [(environment, prompt)] = launches
        self.assertEqual(environment["TAUCETI_PUSH_REMOTE"], "https://git.example.org/native-user/TauCeti")
        self.assertIn("--head native-user:roadmap/", prompt)
        self.source.writable_repository.assert_called_once_with()

    def test_github_identity_outage_does_not_hide_native_inventory(self):
        worker = SimpleNamespace(cfg=self.cfg, gh=Mock(repo="one/project"), rs=Mock(), counters=Mock(), rc=None)
        native = Survey(worker_id="worker")
        opts = SimpleNamespace(only=["review"], prs=())
        with (
            patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}),
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch("tauceti_worker.multi_source.survey", side_effect=[Die("offline"), native]),
            patch("tauceti_worker.multi_source.ReviewState"),
        ):
            result = collect(worker, opts)
        self.assertTrue(result[0][2].github_failed)
        self.assertIs(result[1][2], native)

    def observations(self, github, native):
        first, second = Survey(worker_id="test"), Survey(worker_id="test")
        first.open_prs, second.open_prs = github, native
        first.reviewable.actionable = [Candidate(pr.number, pr.head_oid) for pr in github]
        second.reviewable.actionable = [Candidate(pr.number, pr.head_oid) for pr in native]
        return [
            ("github", SimpleNamespace(gh=SimpleNamespace(repo="one/project")), first),
            ("forgejo", SimpleNamespace(gh=self.source), second),
        ]

    def test_equal_numbers_on_different_sources_are_independent(self):
        observations = self.observations(
            [PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})],
            [PRInfo.from_json({"number": 1, "headRefOid": "b" * 40})],
        )
        deduplicate(observations)
        self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [1, 1])

    def test_identical_heads_and_target_markers_are_not_paid_twice(self):
        for same_head in (True, False):
            body = '<!--tauceti-target:v1 {"focus":"Algebra","id":"one-target"}-->'
            observations = self.observations(
                [PRInfo.from_json({"number": 1, "headRefOid": "a" * 40, "body": body})],
                [PRInfo.from_json({"number": 2, "headRefOid": ("a" if same_head else "b") * 40, "body": body})],
            )
            deduplicate(observations)
            self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [1, 0])

    def test_explicit_copy_keeps_work_on_its_original_forge(self):
        observations = self.observations(
            [
                PRInfo.from_json(
                    {
                        "number": 1,
                        "headRefOid": "a" * 40,
                        "body": "Origin-PR: https://git.example.org/one/project/pulls/2",
                    }
                )
            ],
            [PRInfo.from_json({"number": 2, "headRefOid": "a" * 40})],
        )
        deduplicate(observations)
        self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [0, 1])

    def test_native_review_entrypoint_delegates_to_the_common_runtime(self):
        from tauceti_worker.review_runtime import ReviewRuntime
        from tauceti_worker.source_review import main

        runtime = ReviewRuntime(self.root / "engine", "a" * 40)
        argv = [
            "review",
            "--pr",
            "1",
            "--head",
            "a" * 40,
            "--work",
            str(self.root / "work"),
            "--store",
            str(self.root / "store"),
            "--reviewer",
            "codex",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("tauceti_worker.source_review.load_source", return_value=self.source),
            patch("tauceti_worker.source_review.resolve", return_value=runtime),
            patch("tauceti_worker.source_review.subprocess.run", return_value=SimpleNamespace(returncode=83)) as run,
        ):
            self.assertEqual(main(), 83)
        command = run.call_args.args[0]
        self.assertEqual(command[1], str(runtime.directory / "runner/cli.py"))
        self.assertEqual(command[command.index("--repo") + 1], self.repo.key)
        self.assertEqual(command[command.index("--expect-head") + 1], "a" * 40)
        self.assertIn("--post", command)
        self.assertIn("--no-sync", command)

    def test_numeric_target_with_inherited_both_still_defaults_to_github(self):
        with patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}, clear=True):
            configure(SimpleNamespace(pr=["1"]))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")

    def test_other_origins_do_not_inherit_the_canonical_private_token(self):
        with patch.dict(os.environ, {"TAUCETI_FORGEJO_ORIGIN": "https://other.example.org"}, clear=True):
            configure(SimpleNamespace())
            self.assertNotIn("TAUCETI_FORGEJO_TOKEN_FILE", os.environ)

    def test_source_claim_failure_before_inference_does_not_consume_review_budget(self):
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock(), rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", return_value=81),
        ):
            self.assertIsNone(review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False))
        worker.counters.incr.assert_not_called()
        worker.counters.write.assert_not_called()

    def test_contest_reactions_follow_the_source_capability_without_false_warnings(self):
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock(), rs=Mock())
        candidate = Candidate(1, "a" * 40, contest="correctness", contest_reply_id=7)
        for supported in (False, True):
            self.source.reset_mock()
            self.source.supports_contest_reactions = supported
            self.source.add_reaction.return_value = False
            self.source.remove_reaction.return_value = False
            with (
                self.subTest(supported=supported),
                patch(
                    "tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])
                ),
                patch("tauceti_worker.work_units.run_to_logfile", return_value=81),
                patch("tauceti_worker.work_units.log") as log,
            ):
                self.assertIsNone(review(worker, None, candidate, SimpleNamespace(work_model="codex"), False))
                warnings = [str(call) for call in log.call_args_list if "contest claim" in str(call)]
            self.assertEqual(self.source.add_reaction.call_count, int(supported))
            self.assertEqual(self.source.remove_reaction.call_count, int(supported))
            self.assertEqual(len(warnings), 2 if supported else 0)

    def test_source_failure_at_dispatch_still_allows_the_other_source(self):
        import contextlib

        from tauceti_worker.config import GitHubUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[0][2].rebaseable.actionable.append(Candidate(1, "a" * 40))
        observations[1][2].reviewable.actionable.append(Candidate(2, "b" * 40))
        for _, worker, _ in observations:
            worker.cfg = self.cfg
        options = RoundOpts(only=["rebase", "review"], agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch("tauceti_worker.work_units.run_round", side_effect=[GitHubUnavailable("offline"), 0]) as execute,
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args.args[1].only, ["review"])

    def test_shared_github_intentions_use_the_github_identity(self):
        from tauceti_worker.intentions import own_identities

        self.source.shared_github_login.return_value = "github-worker"
        with (
            patch("tauceti_worker.worker_sources.current", return_value=self.source),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_EXTRA_IDENTITIES": ""}),
        ):
            self.assertEqual(own_identities(), {"github-worker"})

    def test_multi_source_collection_preserves_fleet_focus(self):
        worker = SimpleNamespace(cfg=self.cfg, gh=Mock(repo="one/project"), rs=Mock(), counters=Mock(), rc=None)
        opts = SimpleNamespace(only=["review"], prs=())
        with (
            patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}),
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch(
                "tauceti_worker.multi_source.survey", side_effect=[Survey(worker_id="test"), Survey(worker_id="test")]
            ) as discover,
            patch("tauceti_worker.multi_source.ReviewState"),
            patch("tauceti_worker.work_units.round_scope", return_value=("Algebra", False, True)),
        ):
            result = collect(worker, opts)
        self.assertTrue(all(call.kwargs["selected_roadmap"] == "Algebra" for call in discover.call_args_list))
        self.assertTrue(all(call.kwargs["count_all_roadmaps"] for call in discover.call_args_list))
        self.assertTrue(all(sv._round_scope == (False, True) for _, _, sv in result))

    def test_unchanged_native_review_does_not_charge_error_allowance(self):
        from tauceti_worker.source_execution import review

        counters = Counters(self.cfg)
        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=counters, rs=Mock())
        worker.rs.review_rounds.return_value = 0

        def engine(command, log, *args):
            result = log.parent / "review-results" / (log.stem + ".json")
            result.write_text(json.dumps(dict(head_sha="a" * 40, changed=False)))
            return 0

        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", side_effect=engine),
        ):
            self.assertIsNone(review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False))
        self.assertEqual(counters.read("review-err-1"), 0)
        self.assertTrue(counters.unchanged_review_recent(1, "a" * 40))

    def test_native_review_publication_runs_without_provider_admission(self):
        import contextlib

        from tauceti_worker.multi_source import publish_native_reviews

        with (
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch("tauceti_worker.multi_source.ReviewState"),
            patch("tauceti_worker.source_publication.pending", return_value=True) as pending,
            patch("tauceti_worker.cli.resolve_work_model", side_effect=AssertionError("provider admission")),
        ):
            self.source.anonymous_discovery = False
            self.assertTrue(publish_native_reviews(self.cfg))
            pending.assert_called_once()

    def test_a_round_publishes_the_authoring_backpressure_it_enforces(self):
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import NoEligibleWork, RoundOpts

        observations = self.observations([], [])
        observations[0][2].n_mine_open, observations[1][2].n_mine_open = 3, 4
        for _, _, result in observations:
            result.max_open_prs = 7
            result.roadmap_skip = ["B", "A", ""]
        options = RoundOpts(only=["review"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.runtime_status.report_runtime") as report,
            patch("tauceti_worker.work_units.run_round", return_value=0),
            self.assertRaises(NoEligibleWork),  # nothing actionable: reported all the same
        ):
            run_round(None, options)
        authoring = report.call_args.kwargs["authoring"]
        names = [name for name, _, _ in observations]
        self.assertEqual(authoring["sources"], {names[0]: 3, names[1]: 4})
        self.assertEqual(
            {k: authoring[k] for k in ("count", "limit", "blocked", "scope", "skip")},
            dict(count=7, limit=7, blocked=True, scope="any", skip="A,B"),
        )
        for _, _, result in observations:  # a Fleet-focused round counts every roadmap
            result.roadmap_only, result._automatic_open_count = "Algebra", result.n_mine_open
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.runtime_status.report_runtime") as report,
            patch("tauceti_worker.work_units.run_round", return_value=0),
            self.assertRaises(NoEligibleWork),
        ):
            run_round(None, options)
        self.assertEqual(report.call_args.kwargs["authoring"]["scope"], "any")
        for _, _, result in observations:  # an explicit area counts only that area
            del result._automatic_open_count
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.runtime_status.report_runtime") as report,
            patch("tauceti_worker.work_units.run_round", return_value=0),
            self.assertRaises(NoEligibleWork),
        ):
            run_round(None, options)
        self.assertEqual(report.call_args.kwargs["authoring"]["scope"], "Algebra")

    def test_native_execution_failure_does_not_invalidate_github_authoring_inventory(self):
        import contextlib

        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[1][2].reviewable.actionable = [Candidate(2, "b" * 40)]
        for _, worker, _ in observations:
            worker.cfg = self.cfg
        options = RoundOpts(
            only=["review", "roadmap"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True
        )
        with (
            patch.dict(os.environ, {"TAUCETI_NEW_PR_FORGE": "github"}),
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch(
                "tauceti_worker.work_units.run_round", side_effect=[SourceUnavailable("claim unavailable"), 0]
            ) as execute,
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertEqual(execute.call_args.args[1].only, ["roadmap"])
        self.assertIsNone(observations[1][2].dependency_wait)

    def test_multi_source_fleet_backpressure_falls_back_to_global_maintenance(self):
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        scoped, global_work = self.observations([], []), self.observations([], [])
        for _, _, result in scoped:
            result._round_scope = (False, True)
            result.n_mine_open = result.max_open_prs
        global_work[0][2].reviewable.actionable = [Candidate(1, "a" * 40)]
        options = RoundOpts(
            only=["review", "roadmap"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True
        )
        with (
            patch("tauceti_worker.multi_source.collect", side_effect=[scoped, global_work]) as collect,
            patch("tauceti_worker.work_units.run_round", return_value=0),
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertFalse(collect.call_args.kwargs["use_fleet_focus"])

    def test_native_branch_claim_outage_refunds_unlaunched_repair(self):
        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.work_units import _do_fixlike

        pr = PRInfo.from_json(
            {
                "number": 1,
                "headRefOid": "a" * 40,
                "headRefName": "proposal",
                "headRepositoryOwner": {"login": "worker"},
                "headRepository": {"name": "project"},
            }
        )
        counters = Counters(self.cfg)
        counters.write("fix-1", 1)
        worker = SimpleNamespace(claims=Mock(), counters=counters)
        worker.claims.begin_branch_work.side_effect = SourceUnavailable("claim unavailable")
        with self.assertRaises(SourceUnavailable):
            _do_fixlike(
                worker,
                SimpleNamespace(open_prs=[pr]),
                Candidate(1, "a" * 40),
                None,
                False,
                prompt_file="fix.md",
                label="fix",
                charged=("fix-1",),
            )
        self.assertEqual(counters.read("fix-1"), 0)

    def test_source_switch_combinations_and_loop_child_handoff(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.worker_sources import selection_arguments

        for flags, expected in (
            ([], "github"),
            (["--github"], "github"),
            (["--forgejo"], "both"),
            (["--github", "--forgejo"], "both"),
            (["--no-github", "--forgejo"], "forgejo"),
            (["--forgejo", "--no-forgejo"], "github"),
        ):
            with (
                self.subTest(flags=flags),
                patch.dict(
                    os.environ, {"TAUCETI_WORKER_FORGES": "both", "TAUCETI_NEW_PR_FORGE": "forgejo"}, clear=True
                ),
            ):
                configure(build_parser().parse_args(["work", *flags]))
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)
                self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "github")
                child = build_parser().parse_args(["_round", *selection_arguments()])
                configure(child)
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)

    def test_source_switches_work_before_or_after_subcommand(self):
        from tauceti_worker.cli import build_parser

        for argv in (["--forgejo", "work"], ["work", "--forgejo"], ["--forgejo", "status"], ["--forgejo"]):
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True):
                configure(build_parser().parse_args(argv))
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")

    def test_empty_source_selection_and_ambiguous_target_are_explicit_errors(self):
        from tauceti_worker.cli import build_parser

        for argv in (["--no-github"], ["--no-github", "--no-forgejo"], ["--forgejo", "--pr", "2"]):
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), self.assertRaises(Die):
                configure(build_parser().parse_args(["work", *argv]))

    def test_explicit_native_destination_survives_loop_handoff(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.worker_sources import selection_arguments

        with patch.dict(os.environ, {}, clear=True):
            configure(build_parser().parse_args(["work", "--forgejo", "--new-pr-forge", "forgejo"]))
            child = build_parser().parse_args(["_round", *selection_arguments()])
            configure(child)
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "forgejo")

    def test_driver_forwards_source_flags_into_the_actual_round_command(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.loop import cmd_loop

        for flags, expected in (([], "github"), (["--forgejo"], "both"), (["--no-github", "--forgejo"], "forgejo")):
            with self.subTest(flags=flags), patch.dict(os.environ, {}, clear=True):
                args = build_parser().parse_args(["work", "--loop", *flags])
                configure(args)
                with (
                    patch("tauceti_worker.loop.run_round_subprocess", side_effect=KeyboardInterrupt) as launch,
                    patch("tauceti_worker.loop.github_budget", return_value={}),
                    patch("tauceti_worker.loop.fleet_admission_reason", return_value=None),
                ):
                    cmd_loop(args, self.cfg, only=["review"], agent="deepseek")
                child = build_parser().parse_args(["_round", *launch.call_args.args[0]])
                configure(child)
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)

    def test_dual_source_idle_preserves_actionable_github_discovery_error(self):
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[0][2].dependency_wait = {"dependency": "github-state", "reason": "auth", "actionable": True}
        observations[0][2].errors = ["shared state credential rejected"]
        options = RoundOpts(only=["review"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            self.assertRaisesRegex(Die, "operator action"),
        ):
            run_round(None, options)

    def test_dual_source_idle_preserves_github_wait_deadline(self):
        from tauceti_worker.config import GitHubUnavailable, SharedStateUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        options = RoundOpts(only=["review"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True)
        observations[0][2].dependency_wait = {"dependency": "github-state", "reason": "transport", "retry_at": 1234}
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            self.assertRaises(SharedStateUnavailable) as caught,
        ):
            run_round(None, options)
        self.assertEqual(caught.exception.retry_at, 1234)
        observations[0][2].dependency_wait = None
        observations[0][2].reviewable.actionable = [Candidate(1, "a" * 40)]
        wait = GitHubUnavailable("rate limited", retry_at=2345)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.work_units.run_round", side_effect=wait),
            self.assertRaises(GitHubUnavailable) as caught,
        ):
            run_round(None, options)
        self.assertIs(caught.exception, wait)

    def test_contest_limit_is_not_reported_as_a_review_error(self):
        from tauceti_worker.constants import MAX_REVIEW_CONTESTS

        cfg = source_config(self.cfg, self.source)
        Counters(cfg).write("review-contest-1", MAX_REVIEW_CONTESTS)
        self.source.open_prs.return_value = [self.row()]
        rs = Mock()
        rs.gh_meta.return_value = Meta({"head_sha": "a" * 40, "states": {"style": "blocking_request"}}, "fresh")
        rs.ledger_blocking.return_value = True
        rs.ledger_clean_head.return_value = "a" * 40
        rs.newest_contest_reply.return_value = {"id": 5, "rubric": "style"}
        result = worker_survey(
            cfg,
            self.source,
            rs,
            Counters(cfg),
            roadmap="any",
            skip=[],
            deep=True,
            stages=["review"],
            numbers=(),
            count_all_roadmaps=True,
        )
        self.assertEqual([candidate.pr for candidate in result.reviewable.suppressed], [1])
        self.assertEqual(result.review_stuck, [])

    def test_shared_preparation_failure_does_not_consume_inference_error_allowance(self):
        from tauceti_worker.config import NoProgress
        from tauceti_worker.source_execution import review

        counters = Counters(self.cfg)
        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=counters, rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", return_value=83),
        ):
            with self.assertRaises(NoProgress):
                review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False)
        self.assertEqual(counters.read("review-err-1"), 0)

    def test_shared_workspace_retains_paid_publication_after_interruption(self):
        from tauceti_worker.retention import collect_review_scratch, fresh_report, review_workspace

        with review_workspace(self.cfg) as work:
            receipt = work / "publication.json"
            receipt.write_text(json.dumps(dict(state="prepared", threads=[dict(body="paid finding")])))
        collect_review_scratch(self.cfg, fresh_report())
        self.assertTrue(work.is_dir())
        self.assertEqual(json.loads(receipt.read_text())["threads"][0]["body"], "paid finding")
        receipt.write_text(json.dumps(dict(state="published")))
        with patch("tauceti_worker.live_use.live_users", return_value=[]):
            collect_review_scratch(self.cfg, fresh_report())
        self.assertFalse(work.exists())

    def test_orphan_cleanup_requires_stopped_owner_and_no_live_users(self):
        from tauceti_worker.source_publication import pending

        work = self.cfg.state / "review-work/orphan"
        work.mkdir(parents=True)
        (work / "code").mkdir()
        (work / "review-context.json").write_text(
            json.dumps({"source": self.identity.record(), "store": str(self.cfg.store), "pr": 1, "process": {"pid": 7}})
        )
        with patch("tauceti_worker.live_use.owner_stopped", return_value=False):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertTrue(work.exists())
        with (
            patch("tauceti_worker.live_use.owner_stopped", return_value=True),
            patch("tauceti_worker.live_use.live_users", return_value=["active reviewer"]),
        ):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertTrue(work.exists())
        with (
            patch("tauceti_worker.live_use.owner_stopped", return_value=True),
            patch("tauceti_worker.live_use.live_users", return_value=[]),
        ):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertFalse(work.exists())

    def test_native_review_uses_common_coordinator(self):
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock(), rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with patch("tauceti_worker.work_units.do_review", return_value=0) as common:
            self.assertEqual(
                review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False), 0
            )
        self.assertIs(common.call_args.args[0], worker)

    def test_compact_github_survey_recovers_exact_head_target_markers_from_cached_inventory(self):
        from tauceti_worker.multi_source import enrich_targets

        github = PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})
        body = '<!--tauceti-target:v1 {"focus":"Algebra","id":"one-target"}-->\nOrigin-PR: https://git.example.org/one/project/pulls/2'
        observations = self.observations(
            [github], [PRInfo.from_json({"number": 2, "headRefOid": "b" * 40, "body": body})]
        )
        with patch(
            "tauceti_worker.github_state.read",
            return_value=(True, [{"number": 1, "headRefOid": "a" * 40, "body": body}]),
        ) as read:
            enrich_targets(observations[0][1], observations[0][2])
        self.assertTrue(observations[0][2].target_inventory_available)
        self.assertEqual(observations[0][2].open_prs[0].origin_pr, "https://git.example.org/one/project/pulls/2")
        deduplicate(observations)
        self.assertEqual([len(item[2].reviewable.actionable) for item in observations], [0, 1])
        read.assert_called_once_with("one/project", "open_prs", _optional=True)

    def test_marker_inventory_cannot_apply_a_body_from_another_head(self):
        from tauceti_worker.multi_source import enrich_targets

        observations = self.observations([PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})], [])
        with patch(
            "tauceti_worker.github_state.read",
            return_value=(
                True,
                [
                    {
                        "number": 1,
                        "headRefOid": "b" * 40,
                        "body": "Origin-PR: https://git.example.org/one/project/pulls/2",
                    }
                ],
            ),
        ):
            enrich_targets(observations[0][1], observations[0][2])
        self.assertFalse(observations[0][2].target_inventory_available)
        self.assertFalse(observations[0][2].open_prs[0].origin_pr)


if __name__ == "__main__":
    unittest.main()
