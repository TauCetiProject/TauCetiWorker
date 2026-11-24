"""A share of GitHub reviews posts as the review App, inert until configured."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import github_transport, review_app, review_runtime
from tauceti_worker.config import NoProgress
from tauceti_worker.constants import TAUCETI

CREDENTIALS = SimpleNamespace(app_id=4242, slug="tauceti-review-poster", installation_id=99)


class Routing(unittest.TestCase):
    def test_inert_without_a_configured_app(self):
        with patch.dict(os.environ, {"TAUCETI_REVIEW_APP_SHARE": "1"}, clear=False):
            os.environ.pop(review_app.DIR_ENV, None)
            self.assertFalse(any(review_app.routes(TAUCETI, pr) for pr in range(200)))
            self.assertIsNone(review_app.app_login())
            self.assertEqual(review_app.drain(), [])

    def test_assignment_is_fixed_per_pr_and_follows_the_share(self):
        with patch.dict(os.environ, {review_app.DIR_ENV: "/nonexistent"}):
            os.environ.pop(review_app.SHARE_ENV, None)
            first = [review_app.routes(TAUCETI, pr) for pr in range(1000)]
            self.assertEqual(first, [review_app.routes(TAUCETI, pr) for pr in range(1000)])
            self.assertTrue(400 < sum(first) < 600)
            for value, expected in (("0", 0), ("1", 1000), ("-3", 0), ("7", 1000), ("nan", 0), ("x", 0)):
                with patch.dict(os.environ, {review_app.SHARE_ENV: value}):
                    self.assertEqual(sum(review_app.routes(TAUCETI, pr) for pr in range(1000)), expected, value)

    def test_only_the_tauceti_github_repository_routes(self):
        with patch.dict(os.environ, {review_app.DIR_ENV: "/nonexistent", review_app.SHARE_ENV: "1"}):
            self.assertTrue(review_app.routes(TAUCETI.upper(), 5))
            self.assertFalse(review_app.routes("someone/else", 5))
            self.assertFalse(review_app.routes("forgejo:https://forge.example/" + TAUCETI.lower(), 5))


class Identity(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_GITHUB_SHARED_DIR": str(self.root),
                review_app.DIR_ENV: str(self.root / "app"),
                "GH_TOKEN": "account-token",
                "GITHUB_TOKEN": "other-token",
                "TAUCETI_GITHUB_ACCOUNT": "tauceti-ai-for-science",
                "TAUCETI_WORKER_ID": "w1",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.directory.cleanup)
        review_app._source = None

    def minted(self, *tokens):
        values = iter(tokens)
        return patch.object(
            review_app,
            "_mint",
            side_effect=lambda credentials: {"token": next(values), "expires_at": time.time() + 3600},
        )

    def test_overrides_pin_the_bot_and_pace_every_token_as_one_identity(self):
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one"):
            values = review_app.overrides()
        self.assertEqual(values["GH_TOKEN"], "ghs_one")
        self.assertIsNone(values["GITHUB_TOKEN"])
        for name in ("TAUCETI_GITHUB_ACCOUNT", "_TAUCETI_PINNED_GITHUB_ACCOUNT", "TAUCETI_REVIEW_APP_LOGIN"):
            self.assertEqual(values[name], "tauceti-review-poster[bot]")
        self.assertEqual(values["_TAUCETI_PINNED_GITHUB_SCOPE"], github_transport.scope(values))
        self.assertEqual(
            github_transport.quota_scope(github_transport.scope(values)), review_app.shared_scope(CREDENTIALS)
        )
        with patch.object(review_app, "overrides", return_value=values):
            merged = review_app.environment()
        self.assertNotIn("GITHUB_TOKEN", merged)
        self.assertEqual(merged["TAUCETI_WORKER_ID"], "w1")

    def test_the_token_is_shared_by_the_host_privately_and_refreshed_before_expiry(self):
        with self.minted("ghs_one", "ghs_two") as mint:
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            self.assertEqual(mint.call_count, 1)
            path = review_app._cache_path(CREDENTIALS.app_id)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            value = json.loads(path.read_text())
            value["expires_at"] = time.time() + review_app.TOKEN_MARGIN - 1
            path.write_text(json.dumps(value))
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_two")

    def test_unavailable_credentials_defer_without_charging_the_pr(self):
        with patch.object(review_app, "_credentials", side_effect=ValueError("secret detail")):
            with self.assertRaises(NoProgress) as caught:
                review_app.overrides()
        self.assertNotIn("secret detail", str(caught.exception))

    def test_expired_token_mappings_are_pruned(self):
        folder = self.root / "identities"
        folder.mkdir(parents=True)
        shared = review_app.shared_scope(CREDENTIALS)
        old, foreign = folder / "old.json", folder / "foreign.json"
        old.write_text(json.dumps({"quota_scope": shared}))
        foreign.write_text(json.dumps({"quota_scope": "user"}))
        stale = time.time() - review_app.IDENTITY_TTL - 60
        for path in (old, foreign):
            os.utime(path, (stale, stale))
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one"):
            review_app.overrides()
        self.assertFalse(old.exists())
        self.assertTrue(foreign.exists())

    def test_drain_needs_no_token_while_the_app_outbox_is_empty(self):
        with (
            patch.object(review_app, "_credentials", return_value=CREDENTIALS),
            patch.object(review_app, "token", side_effect=AssertionError("minted")),
        ):
            self.assertEqual(review_app.drain(), [])
        outbox = self.root / "outbox" / review_app.shared_scope(CREDENTIALS) / "w1"
        outbox.mkdir(parents=True)
        (outbox / "intent.json").write_text("{}")
        with (
            patch.object(review_app, "_credentials", return_value=CREDENTIALS),
            patch.object(review_app, "overrides", return_value={"GH_TOKEN": "ghs_one", "GITHUB_TOKEN": None}),
            patch("tauceti_worker.github_outbox.drain", return_value=["intent"]) as drain,
        ):
            self.assertEqual(review_app.drain(), ["intent"])
        self.assertEqual(drain.call_args.args[0]["GH_TOKEN"], "ghs_one")


class Recovery(unittest.TestCase):
    def work(self, root, cfg, name, pr, actor=None):
        work = root / "review-work" / name
        work.mkdir(parents=True)
        record = dict(
            source=dict(forge=dict(kind="github", url="https://github.com"), name=TAUCETI),
            ledger=str(cfg.store),
            state="prepared",
            pr=pr,
            head="a" * 40,
        )
        if actor:
            record["actor"] = actor
        (work / "publication.json").write_text(json.dumps(record))
        (work / "review-context.json").write_text(json.dumps(dict(lifecycle=2)))

    def test_each_identity_recovers_its_own_retained_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = SimpleNamespace(state=root, store=root / "ledger.json", store_dir=root)
            worker = SimpleNamespace(cfg=cfg, gh=SimpleNamespace(repo=TAUCETI))
            self.work(root, cfg, "account-receipt", 1, "tauceti-ai-for-science")
            self.work(root, cfg, "app-receipt", 2, "tauceti-review-poster[bot]")
            self.work(root, cfg, "routed", 3)
            self.work(root, cfg, "unrouted", 4)
            runtime = review_runtime.ReviewRuntime(root / "engine", "a" * 40)
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs.get("env")))
                return SimpleNamespace(returncode=0)

            with (
                patch.object(review_runtime, "resolve", return_value=runtime),
                patch.object(review_app, "app_login", return_value="tauceti-review-poster[bot]"),
                patch.object(review_app, "routes", side_effect=lambda repo, pr: pr in (1, 3)),
                patch.object(review_app, "environment", return_value={"GH_TOKEN": "ghs_app"}),
                patch("subprocess.run", side_effect=run),
            ):
                self.assertTrue(review_runtime.recover(worker))
            self.assertEqual(len(calls), 2)
            account, app = calls
            self.assertIsNone(account[1])
            self.assertEqual(app[1], {"GH_TOKEN": "ghs_app"})
            self.assertIn("account-receipt", account[0])
            self.assertIn("unrouted", account[0])
            self.assertIn("app-receipt", app[0])
            self.assertIn("routed", app[0])
            self.assertNotIn("app-receipt", account[0])


class EngineEnvironment(unittest.TestCase):
    def test_overrides_replace_the_github_identity_of_the_engine_only(self):
        from tauceti_worker import agents

        seen = {}

        def run(argv, **kwargs):
            seen.update(kwargs["env"])
            return SimpleNamespace(returncode=0)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GH_TOKEN": "account", "GITHUB_TOKEN": "account", "TAUCETI_STREAM": "1"}),
            patch("tauceti_worker.fleet_capacity.check_launch_admission"),
            patch("tauceti_worker.claude_api.review_environment", return_value=None),
            patch.object(agents, "run_process", side_effect=run),
        ):
            agents.run_to_logfile(
                ["engine"],
                Path(directory) / "review-1.log",
                "review #1",
                env_overrides={"GH_TOKEN": "ghs_app", "GITHUB_TOKEN": None},
            )
            self.assertEqual(os.environ["GH_TOKEN"], "account")
        self.assertEqual(seen["GH_TOKEN"], "ghs_app")
        self.assertNotIn("GITHUB_TOKEN", seen)


class Review(unittest.TestCase):
    def drive(self, routed):
        from fixtures.review_runtime_stub import install_runtime_stub

        install_runtime_stub()
        from tauceti_worker import work_units

        calls = []

        def run(argv, logf, label, **kwargs):
            calls.append((argv, kwargs.get("env_overrides")))
            return 0

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            worker = SimpleNamespace(
                cfg=SimpleNamespace(state=state, logdir=state, wid="w", store_dir=state / "store"),
                counters=SimpleNamespace(read=lambda k: 0, incr=lambda k: 1, write=lambda k, n: None),
                rs=SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
                gh=SimpleNamespace(repo=TAUCETI, add_reaction=lambda i: True, remove_reaction=lambda i: True),
            )
            candidate = SimpleNamespace(pr=42, head="a" * 40, contest=None, contest_reply_id=None)
            with (
                patch.object(work_units, "run_to_logfile", side_effect=run),
                patch.object(work_units, "_sync_review_outbox", return_value=0),
                patch.object(work_units, "review_publisher", return_value="tauceti-ai-for-science"),
                patch.object(review_app, "routes", return_value=routed),
                patch.object(review_app, "overrides", return_value={"TAUCETI_GITHUB_ACCOUNT": "bot[bot]"}),
            ):
                work_units.do_review(
                    worker, SimpleNamespace(), candidate, SimpleNamespace(work_model="claude"), bubble=False
                )
        ((argv, env),) = calls
        return argv, env

    def test_a_routed_review_posts_as_the_app_and_keeps_the_account_as_submitter(self):
        argv, env = self.drive(True)
        self.assertEqual(env, {"TAUCETI_GITHUB_ACCOUNT": "bot[bot]"})
        self.assertEqual(argv[argv.index("--submitted-by") + 1], "tauceti-ai-for-science")

    def test_other_reviews_are_unchanged(self):
        argv, env = self.drive(False)
        self.assertIsNone(env)
        self.assertNotIn("--submitted-by", argv)


if __name__ == "__main__":
    unittest.main()
