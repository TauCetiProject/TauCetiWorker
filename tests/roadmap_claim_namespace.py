"""Free-choice GitHub authors use their worker namespace without changing other claim routes."""

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_tools.forge import Forge, ForgeError, RepositoryRef

from tauceti_worker.config import Die, GitHubUnavailable, SourceUnavailable
from tauceti_worker.github import GitHubError
from tauceti_worker.survey import Candidate

work = importlib.import_module("tauceti_worker.work_units")
github = importlib.import_module("tauceti_worker.github")
FORK = "alice/TauCeti"
NAMESPACE = "shared/claims"


class Namespace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="roadmap-namespace-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = SimpleNamespace(
            state=self.root / "state", wid="claim-test", checkout=self.root / "checkout", logdir=self.root / "logs"
        )
        (self.cfg.checkout / ".git").mkdir(parents=True)
        self.worker = SimpleNamespace(cfg=self.cfg, gh=None)
        self.candidate = Candidate(0, "", "Algebra")
        self.opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        self.launch = Mock(return_value=0)
        self.publications = Mock(return_value={"roadmap/algebra-test": "a" * 40})
        for context in (
            patch.dict(
                os.environ,
                {
                    "TAUCETI_ROADMAP_TARGETS": "",
                    "TAUCETI_RESPECT_CLAIMS": "false",
                    "TAUCETI_ROADMAP_SKIP": "",
                    "TAUCETI_WORKER_ID": self.cfg.wid,
                },
            ),
            patch.object(work, "ensure_fork", return_value=FORK),
            patch.object(work, "avoid_list", return_value="none"),
            patch.object(
                work, "fetch_ref", side_effect=lambda repo, dest: Path(dest).mkdir(parents=True, exist_ok=True) or True
            ),
            patch.object(work, "prepare_checkout", return_value=True),
            patch.object(work, "run_agent_host", self.launch),
            patch("tauceti_worker.worker_sources.current", return_value=None),
            patch.object(github, "_resolve_claims_repo", return_value=NAMESPACE),
            patch("tauceti_worker.activity.current_round_id", return_value="test-publication-scope"),
            patch("tauceti_worker.self_tested_label.scoped_publications", self.publications),
        ):
            context.start()
            self.addCleanup(context.stop)
        os.environ.pop("CLAIM_REPO", None)
        os.environ.pop("TAUCETI_SOURCE_BINDING", None)

    def author(self):
        return work.do_roadmap(self.worker, None, self.candidate, self.opts, bubble=False)

    def test_default_namespace_and_publication_tracking_are_scoped_to_the_launch(self):
        seen = {}
        self.launch.side_effect = lambda *a, **kw: seen.update(os.environ) or 0
        self.assertEqual(self.author(), 0)
        self.assertEqual(seen["CLAIM_REPO"], NAMESPACE)
        self.assertEqual(seen["TAUCETI_PUSH_SCOPE"], "test-publication-scope")
        self.assertEqual(seen["TAUCETI_WORKER_ID"], self.cfg.wid)
        self.assertNotIn("CLAIM_REPO", os.environ)
        self.assertNotIn("TAUCETI_PUSH_SCOPE", os.environ)
        self.assertEqual(self.candidate.progress["published_branches"], self.publications.return_value)
        self.assertEqual(self.candidate.progress["published_owner"], "alice")

    def test_explicit_namespace_is_preserved_without_lookup(self):
        os.environ["CLAIM_REPO"] = "operator/private-claims"
        seen = []
        self.launch.side_effect = lambda *a, seen=seen, **kw: seen.append(os.environ["CLAIM_REPO"]) or 0
        self.author()
        github._resolve_claims_repo.assert_not_called()
        self.assertEqual(seen, ["operator/private-claims"])
        self.assertEqual(os.environ["CLAIM_REPO"], "operator/private-claims")

    def test_agent_failure_restores_environment_and_keeps_publication_receipts(self):
        os.environ["CLAIM_REPO"] = "operator/private-claims"
        os.environ["TAUCETI_PUSH_SCOPE"] = "outer-scope"
        self.launch.side_effect = RuntimeError("agent failed after a push")
        with self.assertRaisesRegex(RuntimeError, "agent failed"):
            self.author()
        self.assertEqual(os.environ["CLAIM_REPO"], "operator/private-claims")
        self.assertEqual(os.environ["TAUCETI_PUSH_SCOPE"], "outer-scope")
        self.publications.assert_called_once_with(self.cfg.checkout, "test-publication-scope")
        self.assertEqual(self.candidate.progress["published_branches"], self.publications.return_value)

    def test_lookup_outage_uses_the_already_selected_fork_and_still_launches(self):
        for error in (
            Die("offline"),
            GitHubError("offline"),
            GitHubUnavailable("offline"),
            SourceUnavailable("offline"),
            ForgeError("offline"),
            OSError("offline"),
        ):
            with self.subTest(error=type(error).__name__), patch.object(work, "claims_repo", side_effect=error):
                seen = []
                self.launch.side_effect = lambda *a, seen=seen, **kw: seen.append(os.environ["CLAIM_REPO"]) or 0
                self.assertEqual(self.author(), 0)
                self.assertEqual(seen, [FORK])
                self.assertNotIn("CLAIM_REPO", os.environ)

    def test_native_source_preserves_its_wrapper_route_and_never_resolves_the_github_namespace(self):
        for override in (None, "operator/private-claims"):
            with (
                self.subTest(override=override),
                patch(
                    "tauceti_worker.worker_sources.current",
                    return_value=SimpleNamespace(
                        repository=RepositoryRef(Forge("forgejo", "https://git.example.org"), "native/project"),
                        repo="native/project",
                    ),
                ),
                patch.object(
                    work, "claims_repo", side_effect=AssertionError("native namespace must stay source-bound")
                ),
            ):
                if override is None:
                    os.environ.pop("CLAIM_REPO", None)
                else:
                    os.environ["CLAIM_REPO"] = override
                seen = []
                self.launch.side_effect = lambda *a, seen=seen, **kw: seen.append(os.environ.get("CLAIM_REPO")) or 0
                self.author()
                self.assertEqual(seen, [override])
                self.assertEqual(os.environ.get("CLAIM_REPO"), override)

    def test_target_list_uses_the_claim_already_taken_by_the_worker(self):
        from tauceti_worker.targets import parse_targets

        targets = parse_targets("<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `goal` — Prove it (needs: none)\n")
        self.worker.claims = SimpleNamespace(begin_target_work=Mock(return_value=0))
        with (
            patch.object(work, "roadmap_targets", return_value=self.root / "targets.md"),
            patch.object(work, "assign_target", return_value=(targets, next(targets.items()))) as assign,
            patch.object(work, "claims_repo", side_effect=AssertionError("assigned target must not reacquire")),
        ):
            seen = []
            self.launch.side_effect = lambda *a, seen=seen, **kw: seen.append((os.environ.get("CLAIM_REPO"), a[1])) or 0
            self.author()
        assign.assert_called_once()
        self.assertIsNone(seen[0][0])
        self.assertIn("Skip the `claim.sh acquire` step", seen[0][1])
        self.worker.claims.begin_target_work.assert_not_called()

    def test_separate_claim_processes_reacquire_and_release_the_same_local_lease(self):
        bare = self.root / "claim-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        script = Path(__file__).resolve().parents[1] / "scripts/claim.sh"
        env = {
            "CLAIM_GITDIR": str(self.root / "claim-cache.git"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "all",
            "GIT_CONFIG_KEY_1": f"url.{bare}.insteadOf",
            "GIT_CONFIG_VALUE_1": f"https://github.com/{NAMESPACE}",
            "GIT_CONFIG_KEY_2": "protocol.file.allow",
            "GIT_CONFIG_VALUE_2": "always",
        }

        def agent(*args, **kwargs):
            self.assertEqual(os.environ["CLAIM_REPO"], NAMESPACE)
            for operation in ("acquire", "acquire", "release"):
                result = subprocess.run([str(script), operation, "author/Algebra/goal"], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            return 0

        with patch.dict(os.environ, env):
            self.launch.side_effect = agent
            self.author()
        refs = subprocess.run(
            ["git", "--git-dir", str(bare), "for-each-ref", "refs/tauceti-claims"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(refs.stdout, "")


if __name__ == "__main__":
    unittest.main()
