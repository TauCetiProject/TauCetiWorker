"""A share of GitHub reviews posts as the review App, inert until configured."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
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

CREDENTIALS = SimpleNamespace(app_id=4242, slug="tauceti-review-poster", installation_id=99, private_key_pem=b"k")


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

    def test_the_app_environment_pins_the_bot_and_paces_every_token_as_one_identity(self):
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one"):
            values = review_app.environment({**os.environ, review_app.PUBLISH_ENV: "1"})
        self.assertEqual(values["GH_TOKEN"], "ghs_one")
        self.assertNotIn("GITHUB_TOKEN", values)
        self.assertNotIn(review_app.PUBLISH_ENV, values)
        self.assertEqual(values["TAUCETI_WORKER_ID"], "w1")
        for name in ("TAUCETI_GITHUB_ACCOUNT", "_TAUCETI_PINNED_GITHUB_ACCOUNT", "TAUCETI_REVIEW_APP_LOGIN"):
            self.assertEqual(values[name], "tauceti-review-poster[bot]")
        self.assertEqual(values["_TAUCETI_PINNED_GITHUB_SCOPE"], github_transport.scope(values))
        self.assertEqual(
            github_transport.quota_scope(github_transport.scope(values)), review_app.shared_scope(CREDENTIALS)
        )

    def test_engine_overrides_check_the_app_and_carry_no_token(self):
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one") as mint:
            values = review_app.engine_overrides()
        self.assertEqual(mint.call_count, 1)
        self.assertEqual(values, {review_app.PUBLISH_ENV: "1", review_app.LOGIN_ENV: "tauceti-review-poster[bot]"})

    def test_a_near_expiry_token_is_replaced_before_a_send(self):
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one", "ghs_two"):
            first = review_app.environment()
            self.assertIs(review_app.refreshed(first), first)
            path = review_app._cache_path(CREDENTIALS)
            value = json.loads(path.read_text())
            value["expires_at"] = time.time() + review_app.TOKEN_MARGIN - 1
            path.write_text(json.dumps(value))
            second = review_app.refreshed(first)
        self.assertEqual(second["GH_TOKEN"], "ghs_two")
        self.assertEqual(second["_TAUCETI_PINNED_GITHUB_SCOPE"], github_transport.scope(second))

    def test_a_rejected_token_is_forgotten_and_another_installation_never_reuses_it(self):
        with patch.object(review_app, "_credentials", return_value=CREDENTIALS), self.minted("ghs_one", "ghs_two"):
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            review_app.invalidate("ghs_other")
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            review_app.invalidate("ghs_one")
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_two")
        reinstalled = SimpleNamespace(**{**vars(CREDENTIALS), "installation_id": 100})
        self.assertNotEqual(review_app._cache_path(reinstalled), review_app._cache_path(CREDENTIALS))
        rekeyed = SimpleNamespace(**{**vars(CREDENTIALS), "private_key_pem": b"other"})
        self.assertNotEqual(review_app._cache_path(rekeyed), review_app._cache_path(CREDENTIALS))

    def test_the_token_is_shared_by_the_host_privately_and_refreshed_before_expiry(self):
        with self.minted("ghs_one", "ghs_two") as mint:
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_one")
            self.assertEqual(mint.call_count, 1)
            path = review_app._cache_path(CREDENTIALS)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            value = json.loads(path.read_text())
            value["expires_at"] = time.time() + review_app.TOKEN_MARGIN - 1
            path.write_text(json.dumps(value))
            self.assertEqual(review_app.token(CREDENTIALS), "ghs_two")

    def test_unavailable_credentials_defer_without_charging_the_pr(self):
        with patch.object(review_app, "_credentials", side_effect=ValueError("secret detail")):
            with self.assertRaises(NoProgress) as caught:
                review_app.engine_overrides()
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
            review_app.environment()
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
            patch.object(review_app, "environment", return_value={"GH_TOKEN": "ghs_one"}),
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

    def context_only(self, root, cfg, name, pr, publisher):
        """Paid output whose process stopped before writing a publication receipt."""
        work = root / "review-work" / name
        work.mkdir(parents=True)
        (work / "post_plan.json").write_text(json.dumps(dict(head_sha="a" * 40)))
        context = dict(
            source=dict(forge=dict(kind="github", url="https://github.com"), name=TAUCETI),
            store=str(cfg.store),
            pr=pr,
            head="a" * 40,
            lifecycle=2,
            publisher=publisher,
        )
        (work / "review-context.json").write_text(json.dumps(context))

    def test_each_identity_recovers_its_own_retained_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = SimpleNamespace(state=root, store=root / "ledger.json", store_dir=root)
            worker = SimpleNamespace(cfg=cfg, gh=SimpleNamespace(repo=TAUCETI))
            self.work(root, cfg, "account-receipt", 1, "tauceti-ai-for-science")
            self.work(root, cfg, "app-receipt", 2, "tauceti-review-poster[bot]")
            self.work(root, cfg, "routed", 3)
            self.work(root, cfg, "unrouted", 4)
            self.context_only(root, cfg, "app-context", 5, "tauceti-review-poster[bot]")
            runtime = review_runtime.ReviewRuntime(root / "engine", "a" * 40)
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs.get("env")))
                return SimpleNamespace(returncode=0)

            with (
                patch.object(review_runtime, "resolve", return_value=runtime),
                patch.object(review_app, "app_login", return_value="tauceti-review-poster[bot]"),
                patch.object(review_app, "routes", side_effect=lambda repo, pr: pr in (1, 3)),
                patch.object(review_app, "engine_overrides", return_value={review_app.PUBLISH_ENV: "1"}),
                patch("subprocess.run", side_effect=run),
            ):
                self.assertTrue(review_runtime.recover(worker))
            self.assertEqual(len(calls), 2)
            account, app = calls
            self.assertIsNone(account[1])
            self.assertEqual(app[1][review_app.PUBLISH_ENV], "1")
            self.assertIn("account-receipt", account[0])
            self.assertIn("unrouted", account[0])
            self.assertIn("app-receipt", app[0])
            self.assertIn("routed", app[0])
            self.assertIn("app-context", app[0])
            self.assertNotIn("app-receipt", account[0])


class Proxy(unittest.TestCase):
    def test_publication_requests_are_the_review_comment_writes(self):
        writes = (
            ["api", "-X", "POST", "/repos/o/r/issues/1/comments", "-F", "body=@x"],
            ["api", "--method", "PATCH", "/repos/o/r/issues/comments/9"],
            ["api", "-X", "POST", "/repos/o/r/pulls/1/comments/9/replies"],
            ["api", "-X", "DELETE", "/repos/o/r/issues/comments/9"],
            ["api", "graphql", "-f", "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){x}}"],
        )
        reads = (
            ["api", "--paginate", "/repos/o/r/issues/1/comments?per_page=100"],
            ["api", "-X", "DELETE", "/repos/o/r/issues/1/labels/x"],
            ["api", "graphql", "-f", "query=query{viewer{login}}"],
            ["pr", "view", "1"],
        )
        for args in writes:
            self.assertTrue(review_app.publication_request(args), args)
        for args in reads:
            self.assertFalse(review_app.publication_request(args), args)

    def test_only_a_publishing_engine_sends_writes_as_the_app(self):
        write = ["api", "-X", "POST", "/repos/o/r/issues/1/comments"]
        with patch.dict(os.environ, {review_app.DIR_ENV: "/app"}):
            os.environ.pop(review_app.PUBLISH_ENV, None)
            self.assertIsNone(review_app.request_environment(write))
            os.environ[review_app.PUBLISH_ENV] = "1"
            self.assertIsNone(review_app.request_environment(["api", "/repos/o/r/pulls/1"]))
            with patch.object(review_app, "environment", return_value={"GH_TOKEN": "ghs_app"}):
                self.assertEqual(review_app.request_environment(write), {"GH_TOKEN": "ghs_app"})
            from tauceti_worker.config import GitHubUnavailable

            with patch.object(review_app, "_credentials", side_effect=OSError("detail")):
                with self.assertRaises(GitHubUnavailable):
                    review_app.request_environment(write)

    def test_a_publishing_engine_is_told_it_is_the_app_bot(self):
        query = ["api", "user", "--jq", ".login"]
        env = {review_app.DIR_ENV: "/app", review_app.PUBLISH_ENV: "1", review_app.LOGIN_ENV: "poster[bot]"}
        with patch.dict(os.environ, env), patch.object(github_transport, "execute", side_effect=AssertionError):
            self.assertEqual(github_transport.migrated_identity(query).stdout, "poster[bot]\n")
            os.environ[review_app.LOGIN_ENV] = "kim-em"
            self.assertEqual(github_transport.migrated_identity(query).returncode, 1)


class Rejection(unittest.TestCase):
    def test_a_rejected_send_forgets_the_token_it_actually_used(self):
        old = {"GH_TOKEN": "ghs_old", "_TAUCETI_REVIEW_APP_TOKEN": "1", "TAUCETI_GH_PROXY_WAIT": "0"}
        new = {**old, "GH_TOKEN": "ghs_new"}
        sent = []

        def run(argv, **kwargs):
            sent.append(kwargs["env"]["GH_TOKEN"])
            return SimpleNamespace(returncode=1, stdout="", stderr="gh: Bad credentials (HTTP 401)\n")

        with (
            patch.object(github_transport, "cooldown", return_value=0),
            patch.object(github_transport, "observed_budget", return_value={}),
            patch.object(github_transport, "mutation_slot", return_value=0),
            patch.object(github_transport, "invalidate"),
            patch.object(github_transport, "secondary_result", return_value=0),
            patch.object(github_transport, "parse_trace", side_effect=lambda text, **k: text),
            patch.object(github_transport.subprocess, "run", side_effect=run),
            patch.object(review_app, "refreshed", return_value=new),
            patch.object(review_app, "invalidate") as invalidate,
        ):
            github_transport._execute(["gh", "api", "-X", "PATCH", "/repos/o/r/issues/comments/1"], env=old)
        self.assertEqual(sent, ["ghs_new"])
        invalidate.assert_called_with("ghs_new")


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
                patch.object(
                    review_app,
                    "engine_overrides",
                    return_value={review_app.PUBLISH_ENV: "1", review_app.LOGIN_ENV: "bot[bot]"},
                ),
            ):
                work_units.do_review(
                    worker, SimpleNamespace(), candidate, SimpleNamespace(work_model="claude"), bubble=False
                )
        ((argv, env),) = calls
        return argv, env

    def test_a_routed_review_publishes_as_the_app_and_keeps_the_account_as_submitter(self):
        argv, env = self.drive(True)
        self.assertEqual(env, {review_app.PUBLISH_ENV: "1", review_app.LOGIN_ENV: "bot[bot]"})
        self.assertEqual(argv[argv.index("--submitted-by") + 1], "tauceti-ai-for-science")

    def test_other_reviews_are_unchanged(self):
        argv, env = self.drive(False)
        self.assertIsNone(env)
        self.assertNotIn("--submitted-by", argv)


if __name__ == "__main__":
    unittest.main()
