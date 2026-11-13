"""A code upgrade must reuse worker state, credentials, recovery and cleanup ownership."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, config, paths, retention, work_recovery


class LegacyDataRoot(unittest.TestCase):
    def test_code_upgrade_reuses_data_and_cleanup_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = root / "durable"
            worker = data / "state" / "existing"
            worker.mkdir(parents=True)
            (worker / "instance.lock").touch()
            recovery = worker / "recovery" / "saved"
            recovery.mkdir(parents=True)
            (recovery / "status.json").write_text(json.dumps({"state": "eligible"}))
            checkout = data / "checkouts" / "existing" / "TauCeti"
            checkout.mkdir(parents=True)
            with patch.dict(os.environ, {"TAUCETI_LEGACY_DATA_ROOT": str(data)}):
                for revision in ["old-code", "new-code"]:
                    assets = root / revision
                    with (
                        patch.object(config, "HERE", assets),
                        patch.object(agents, "HERE", assets),
                        patch.object(retention, "HERE", assets),
                        patch.object(work_recovery, "HERE", assets),
                        patch("tauceti_tools.project.active", return_value=None),
                    ):
                        cfg = config.Config.resolve("existing", home=root / "login")
                        self.assertEqual(cfg.state, worker)
                        self.assertEqual(cfg.checkout, checkout)
                        self.assertEqual(cfg.logdir, data / "logs" / "existing")
                        self.assertEqual(agents._worker_iso_home("existing"), worker / "home")
                        self.assertEqual(config.HERE, assets)
                        self.assertEqual([(c.wid, c.state) for c in retention.worker_configs()], [("existing", worker)])
                        self.assertEqual(next(work_recovery.records())[1], recovery)
                child = subprocess.check_output(
                    [
                        sys.executable,
                        "-c",
                        "from tauceti_worker.paths import legacy_data_root; print(legacy_data_root())",
                    ],
                    env=paths.self_env(),
                    text=True,
                )
                self.assertEqual(child.strip(), str(data))

    def test_cleanup_discovers_current_and_newly_installed_legacy_roots(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            fleet = home / ".local/share/tauceti-fleet"
            old = fleet / "worker-data/retained/old"
            current = fleet / "worker-data/current"
            installed = fleet / "worker-runtimes/new/lib/python3.14/site-packages/tauceti_worker"
            for path in (old, current):
                (path / "state/w").mkdir(parents=True)
            (fleet / "worker-data/roots.json").write_text(json.dumps(dict(roots={"old": str(old)})))
            state = old / "state/w"
            (state / "recovery-prepared.json").write_text(json.dumps(dict(cleanup_backlog=4)))
            (state / "recovery-cleanup/discarded/old-copy").mkdir(parents=True)
            (state / "verification-pending").mkdir()
            (state / "verification-pending/job.json").write_text("{}")
            with (
                patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(current)),
                patch.object(retention.Config, "resolve", return_value=SimpleNamespace(home=home)),
                patch.object(retention, "legacy_project", return_value=True),
            ):
                self.assertEqual(retention.data_roots(), [current, old])
                (installed / "state/w").mkdir(parents=True)
                self.assertEqual(set(retention.data_roots()), {current, old, installed})
                seen = []
                shared = []

                def collect(**kwargs):
                    seen.append(paths.legacy_data_root())
                    shared.append(kwargs["shared"])
                    return retention.fresh_report()

                with patch.object(retention, "sweep", side_effect=collect):
                    report = retention.run_all(dry=True)
                self.assertEqual(set(seen), {current, old, installed})
                self.assertEqual(len(report["roots"]), 3)
                self.assertEqual(shared, [True, False, False])
                self.assertIsInstance(report["free_before_bytes"], int)
                self.assertIsInstance(report["free_bytes"], int)
                by_root = {item["path"]: item for item in report["roots"]}
                self.assertEqual(
                    by_root[str(old)]["pending_work"], dict(recovery_backlog=4, discarded_trees=1, verification_jobs=1)
                )
                self.assertEqual(
                    by_root[str(current)]["pending_work"],
                    dict(recovery_backlog=0, discarded_trees=0, verification_jobs=0),
                )
                for item in report["roots"]:
                    self.assertIsInstance(item["free_before_bytes"], int)
                    self.assertIsInstance(item["free_bytes"], int)
                self.assertEqual(paths.legacy_data_root(), current)

    def test_cleanup_locks_every_root_it_collects(self):
        import fcntl
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            fleet = home / ".local/share/tauceti-fleet"
            old = fleet / "worker-data/retained/old"
            current = fleet / "worker-data/current"
            for path in (old, current):
                (path / "state/w").mkdir(parents=True)
            (fleet / "worker-data/roots.json").write_text(json.dumps(dict(roots={"old": str(old)})))
            with (
                patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(current), TAUCETI_ACTIVITY_DIR=temporary + "/a"),
                patch.object(retention.Config, "resolve", return_value=SimpleNamespace(home=home)),
                patch.object(retention, "legacy_project", return_value=True),
                patch.object(retention, "sweep", return_value=retention.fresh_report()),
                patch.object(retention, "drain_pending"),
                open(old / "state/.cleanup.lock", "a") as held,
            ):
                # Another collector, selecting the old root, is collecting it right now.
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    retention.run_all()

    def test_installed_package_keeps_data_outside_the_install(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_DATA_HOME": temporary}):
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            with patch.object(paths, "INSTALLED", True):
                self.assertEqual(paths.legacy_data_root(), Path(temporary) / "tauceti")
                self.assertEqual(paths.legacy_data_root(paths.HERE), Path(temporary) / "tauceti")
                self.assertEqual(paths.legacy_data_root(Path("/fixture")), Path("/fixture"))
            with patch.object(paths, "INSTALLED", False):
                self.assertEqual(paths.legacy_data_root(), paths.HERE)

    def retired_fixture(self, root):
        (root / "state/w/recovery/saved").mkdir(parents=True)
        (root / "state/w/instance.lock").touch()
        (root / "state/w/recovery/saved/patch.diff").write_text("unpublished")
        build = root / "checkouts/w/TauCeti/.lake/build"
        build.mkdir(parents=True)
        (build / "A.olean").write_bytes(b"x")
        build.chmod(0o500)  # Lake and Git leave read-only directories behind
        (root / "logs/w").mkdir(parents=True)
        (root / "logs/w/round.log").write_text("log")
        (root / "prompts").mkdir(exist_ok=True)  # code beside the data is never touched

    def test_collector_deletes_idle_retired_roots_and_keeps_busy_ones(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            selected, old, busy = base / "selected", base / "old-install", base / "busy-runtime"
            for root in (old, busy):
                self.retired_fixture(root)
            (selected / "state").mkdir(parents=True)
            with (
                patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(selected)),
                patch.object(paths, "INSTALLED", True),
                patch.object(retention, "HERE", old),
                patch.object(retention, "legacy_project", return_value=True),
                patch.object(retention, "data_roots", return_value=[selected, busy]),
            ):
                self.assertEqual(retention.retired_roots(), [busy, old])
                with open(busy / "state/w/instance.lock") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    report = retention.fresh_report()
                    retention.retire_roots(report, dry=True)
                    self.assertTrue((old / "state").exists())
                    report = retention.fresh_report()
                    retention.retire_roots(report)
                self.assertIn(str(busy), report["protected"])
                self.assertTrue((busy / "state/w/recovery/saved/patch.diff").exists())
                for part in ("state", "checkouts", "logs"):
                    self.assertFalse((old / part).exists())
                self.assertEqual(sorted(p.name for p in old.iterdir()), ["prompts"])
                self.assertEqual(report["errors"], [])
                retention.retire_roots(retention.fresh_report())  # released: now deleted too
                self.assertEqual(sorted(p.name for p in busy.iterdir()), ["prompts"])
                self.assertEqual(retention.retired_roots(), [])

    def test_collector_keeps_a_retired_root_that_a_lockless_process_uses(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            selected, old = base / "selected", base / "old-install"
            self.retired_fixture(old)
            (selected / "state").mkdir(parents=True)
            service = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=old / "logs/w")
            self.addCleanup(service.wait)
            self.addCleanup(service.kill)
            with (
                patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(selected)),
                patch.object(paths, "INSTALLED", True),
                patch.object(retention, "HERE", old),
                patch.object(retention, "legacy_project", return_value=False),
            ):
                report = retention.fresh_report()
                retention.retire_roots(report)
                self.assertIn(str(old), report["protected"])
                self.assertTrue((old / "state/w/recovery/saved/patch.diff").exists())
                service.kill()
                service.wait()
                retention.retire_roots(retention.fresh_report())
            self.assertEqual(sorted(p.name for p in old.iterdir()), ["prompts"])

    def test_purge_removes_only_dedicated_directories_after_a_plan(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        from tauceti_worker import purge

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            env = {
                "HOME": str(home),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "TAUCETI_ACTIVITY_DIR": str(home / ".local/state/tauceti/activity"),
                "TAUCETI_BUDGET_DIR": str(home / ".local/state/tauceti/budget"),
                "TAUCETI_GITHUB_SHARED_DIR": str(home / ".local/state/tauceti/github-shared"),
                "TAUCETI_SHARED_REPOSITORIES_DIR": str(home / ".local/state/tauceti/github-shared/repositories"),
                "TAUCETI_WORKERS_STATE_DIR": str(home / ".local/state/tauceti/workers"),
                "TAUCETI_RUNTIME_DIR": str(base / "run"),
                "TAUCETI_BUILD_CACHE_ROOT": str(home / ".cache/tauceti/builds"),
                "TAUCETI_SHARED_MATHLIB_ROOT": str(home / ".cache/tauceti/mathlib-builds"),
                "TAUCETI_VERIFY_STORAGE_DIR": str(home / ".cache/tauceti/verification-storage"),
                "TAUCETI_VERIFY_CACHE_DIR": str(base / "tmp/tauceti-verification"),
            }
            data = home / ".local/share/tauceti"
            self.retired_fixture(data)
            old = base / "venv/site-packages/tauceti_worker"
            self.retired_fixture(old)
            for path in (
                home / ".local/state/tauceti/budget",
                home / ".cache/tauceti/builds",
                base / "tmp/tauceti-verification",
            ):
                path.mkdir(parents=True)
            (home / ".config/tauceti").mkdir(parents=True)
            (home / ".config/tauceti/workers.toml").write_text("")
            (home / ".elan/toolchains").mkdir(parents=True)
            with (
                patch.dict(os.environ, env),
                patch.object(paths, "INSTALLED", True),
                patch.object(purge, "HERE", old),
                patch.object(retention, "HERE", old),
                patch.object(retention, "legacy_project", return_value=False),
                patch.object(purge, "_remove_services") as services,
            ):
                os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
                planned = purge.plan()
                # Every helper must resolve inside the fixture before anything may be deleted.
                for path in planned:
                    self.assertTrue(path.is_relative_to(base), path)
                self.assertIn(data, planned)
                self.assertIn(old / "state", planned)
                self.assertNotIn(home / ".config/tauceti", planned)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(purge.cmd_purge(SimpleNamespace(yes=False, config=False)), 0)
                self.assertTrue(data.exists())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(purge.cmd_purge(SimpleNamespace(yes=True, config=False)), 0)
                services.assert_called_once()
            self.assertFalse(data.exists())
            self.assertFalse((home / ".local/state/tauceti").exists())
            self.assertFalse((home / ".cache/tauceti").exists())
            self.assertFalse((base / "tmp/tauceti-verification").exists())
            self.assertEqual(sorted(p.name for p in old.iterdir()), ["prompts"])
            self.assertTrue((home / ".config/tauceti/workers.toml").exists())
            self.assertTrue((home / ".elan/toolchains").exists())

    def test_purge_refuses_while_a_worker_runs(self):
        import fcntl
        from types import SimpleNamespace

        from tauceti_worker import purge

        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary).resolve() / "data"
            self.retired_fixture(data)
            with (
                patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(data), TAUCETI_RUNTIME_DIR=temporary + "/run"),
                patch.object(purge, "plan", return_value=[data]),
                patch.object(purge, "_remove_services") as services,
                open(data / "state/w/instance.lock") as lock,
            ):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(config.Die, "worker or round is running"):
                    purge.cmd_purge(SimpleNamespace(yes=True, config=False))
                services.assert_not_called()
            self.assertTrue((data / "state").exists())

    def test_default_and_invalid_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            self.assertEqual(paths.legacy_data_root(Path("/old-layout")), Path("/old-layout"))
            os.environ["TAUCETI_LEGACY_DATA_ROOT"] = "relative"
            with self.assertRaisesRegex(ValueError, "absolute path"):
                paths.legacy_data_root()


if __name__ == "__main__":
    unittest.main()
