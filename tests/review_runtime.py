"""Shared review resolution works without installed Review/Worker metadata."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import review_runtime, work_units
from tauceti_worker.config import NoProgress


class ReviewRuntime(unittest.TestCase):
    def engine(self, root):
        (root / "runner").mkdir(parents=True)
        (root / "runner/cli.py").write_text('print("engine")')
        (root / "rubrics").mkdir()
        return root

    def test_source_override_needs_no_distribution_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.engine(Path(directory) / "engine")
            with (
                patch.dict(os.environ, {"TAUCETI_REVIEW_ENGINE_DIR": str(root), "TAUCETI_REVIEW_REF": ""}),
                patch("importlib.metadata.distribution", side_effect=AssertionError("installed distribution assumed")),
                patch("tauceti_worker.agents.fetch_ref", side_effect=AssertionError("override was ignored")),
            ):
                runtime = review_runtime.resolve(SimpleNamespace(state=Path(directory)))
            command = runtime.command("7", "--post")
            self.assertEqual(command[:2], [sys.executable, str(root / "runner/cli.py")])
            self.assertIn("--repo-dir", command)
            self.assertEqual(command[-2:], ["7", "--post"])

    def test_public_engine_resolution_does_not_inherit_native_git_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.engine(state / "refs/review-engine")
            inherited = dict(
                TAUCETI_SOURCE_BINDING="native",
                TAUCETI_FORGEJO_TOKEN_FILE="/private/token",
                GIT_NO_LAZY_FETCH="1",
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="protocol.allow",
                GIT_CONFIG_VALUE_0="never",
            )

            def fetch(repo, path):
                self.assertNotIn("TAUCETI_SOURCE_BINDING", os.environ)
                self.assertNotIn("TAUCETI_FORGEJO_TOKEN_FILE", os.environ)
                settings = {
                    os.environ.get(f"GIT_CONFIG_KEY_{i}"): os.environ.get(f"GIT_CONFIG_VALUE_{i}")
                    for i in range(int(os.environ.get("GIT_CONFIG_COUNT", "0")))
                }
                self.assertNotEqual(settings.get("protocol.allow"), "never")
                self.assertNotEqual(os.environ.get("GIT_NO_LAZY_FETCH"), "1")
                return True

            with (
                patch.dict(os.environ, {**inherited, "TAUCETI_REVIEW_REF": "", "TAUCETI_REVIEW_ENGINE_DIR": ""}),
                patch("tauceti_worker.agents.fetch_ref", side_effect=fetch),
                patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")),
            ):
                review_runtime.resolve(SimpleNamespace(state=state))
                self.assertEqual(os.environ["TAUCETI_SOURCE_BINDING"], "native")

    def test_invalid_configured_revision_is_rejected_before_fetch(self):
        with patch.dict(os.environ, {"TAUCETI_REVIEW_REF": "floating-branch"}), self.assertRaises(NoProgress):
            review_runtime.resolve(SimpleNamespace(state=Path("/unused")))

    def test_archive_uses_the_same_resolved_runtime_and_preserves_failed_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "store/outbox"
            outbox.mkdir(parents=True)
            record = outbox / "record.json"
            record.write_text("{}")
            runtime = review_runtime.ReviewRuntime(root / "engine", "a" * 40)
            worker = SimpleNamespace(
                cfg=SimpleNamespace(store_dir=root / "store", logdir=root / "logs"), review_runtime=runtime
            )
            with (
                patch("tauceti_worker.review_runtime.resolve", side_effect=AssertionError("engine resolved twice")),
                patch.object(work_units, "can_push", return_value=True),
                patch("tauceti_worker.worker_sources.shared_github_environment") as environment,
                patch.object(
                    work_units.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr="offline"),
                ) as run,
            ):
                self.assertEqual(work_units._sync_review_outbox(worker, 7), 1)
            self.assertEqual(run.call_args.args[0][:2], runtime.command()[:2])
            self.assertIn("--sync-only", run.call_args.args[0])
            environment.assert_called_once()
            self.assertTrue(record.exists())

    def test_pending_work_recognizes_both_plan_names_and_unchanged_completion(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            cfg = SimpleNamespace(state=Path(directory))
            for name in ("plan.json", "post_plan.json"):
                work = cfg.state / "review-work" / name
                work.mkdir(parents=True)
                (work / "review-context.json").write_text(json.dumps(dict(pr=7, head="a" * 40)))
                (work / name).write_text(json.dumps(dict(head_sha="a" * 40)))
                self.assertEqual(review_runtime.pending_reviews(cfg), {(7, "a" * 40)})
                (work / name).write_text(json.dumps(dict(head_sha="a" * 40, review_changed=False)))
                self.assertEqual(review_runtime.pending_reviews(cfg), set())


if __name__ == "__main__":
    unittest.main()
