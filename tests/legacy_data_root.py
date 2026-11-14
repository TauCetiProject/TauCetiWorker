"""A code upgrade must reuse worker state, credentials, recovery and cleanup ownership."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
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

    def test_installed_root_follows_the_login_home_and_is_pinned_for_children(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            base = Path(temporary).resolve()
            login, package = base / "login", base / "venv/site-packages/tauceti_worker"
            for name in ("TAUCETI_LEGACY_DATA_ROOT", "XDG_DATA_HOME"):
                os.environ.pop(name, None)
            os.environ["HOME"] = str(base / "login/data/state/w/home")  # an isolated worker's HOME
            default = (
                login / "Library/Application Support/tauceti/data"
                if sys.platform == "darwin"
                else login / ".local/share/tauceti"
            )
            with (
                patch.object(paths, "INSTALLED", True),
                patch.object(paths, "HERE", package),
                patch.object(paths, "login_home", return_value=login),
            ):
                self.assertEqual(paths.legacy_data_root(), default)
                self.assertEqual(os.environ["TAUCETI_LEGACY_DATA_ROOT"], str(default))
                child = subprocess.check_output(
                    [
                        sys.executable,
                        "-c",
                        "from tauceti_worker.paths import legacy_data_root; print(legacy_data_root())",
                    ],
                    env=paths.self_env(),
                    text=True,
                )
                self.assertEqual(child.strip(), str(default))
                os.environ.pop("TAUCETI_LEGACY_DATA_ROOT")
                os.environ["XDG_DATA_HOME"] = "relative"  # ignored, as the XDG spec requires
                self.assertEqual(paths.legacy_data_root(), default)
                os.environ.pop("TAUCETI_LEGACY_DATA_ROOT")
                os.environ["XDG_DATA_HOME"] = str(base / "xdg")
                self.assertEqual(paths.legacy_data_root(), base / "xdg/tauceti")
                self.assertEqual(paths.legacy_data_root(Path("/fixture")), base / "xdg/tauceti")
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT")
            with patch.object(paths, "INSTALLED", False), patch.object(paths, "HERE", package):
                self.assertEqual(paths.legacy_data_root(), package)
                self.assertNotIn("TAUCETI_LEGACY_DATA_ROOT", os.environ)
                self.assertEqual(paths.legacy_data_root(Path("/fixture")), Path("/fixture"))

    def test_rounds_of_an_older_loop_keep_its_package_root(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            base = Path(temporary).resolve()
            package = base / "venv/site-packages/tauceti_worker"
            (package / "state/w").mkdir(parents=True)
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            os.environ["XDG_DATA_HOME"] = str(base / "xdg")
            with (
                patch.object(paths, "INSTALLED", True),
                patch.object(paths, "HERE", package),
                open(package / "state/w/instance.lock", "a") as lock,
            ):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the older loop
                self.assertEqual(paths.legacy_data_root(), package)
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.environ.pop("TAUCETI_LEGACY_DATA_ROOT")
                self.assertEqual(paths.legacy_data_root(), base / "xdg/tauceti")

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

    def retiring(self, base, old, *, extra=(), units=(), mounts=None):
        """Context selecting base/selected, with ``old`` as this installation's package root."""
        selected = base / "selected"
        (selected / "state").mkdir(parents=True, exist_ok=True)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=str(selected)))
        stack.enter_context(patch.object(paths, "INSTALLED", True))
        stack.enter_context(patch.object(retention, "HERE", old))
        stack.enter_context(patch.object(retention, "legacy_project", return_value=bool(extra)))
        stack.enter_context(patch.object(retention, "data_roots", return_value=[selected, *extra]))
        stack.enter_context(patch.object(retention, "_unit_texts", return_value=list(units)))
        if mounts is not None:
            stack.enter_context(patch.object(retention, "mount_points", return_value=mounts))
        return stack

    def protected(self, report, root):
        return any(item.startswith(str(root) + ":") for item in report["protected"])

    def test_collector_deletes_idle_retired_roots_and_keeps_locked_ones(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            old, busy = base / "old-install", base / "busy-runtime"
            for root in (old, busy):
                self.retired_fixture(root)
            with self.retiring(base, old, extra=[busy]):
                self.assertEqual(retention.retired_roots(), [busy, old])
                with open(busy / "state/w/instance.lock") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    report = retention.fresh_report()
                    retention.retire_roots(report, dry=True)
                    self.assertTrue((old / "state").exists())
                    report = retention.fresh_report()
                    retention.retire_roots(report)
                self.assertTrue(self.protected(report, busy))
                self.assertTrue((busy / "state/w/recovery/saved/patch.diff").exists())
                self.assertEqual(sorted(p.name for p in old.iterdir()), ["prompts"])
                self.assertEqual(report["errors"], [])
                retention.retire_roots(retention.fresh_report())  # released: now deleted too
                self.assertEqual(sorted(p.name for p in busy.iterdir()), ["prompts"])
                self.assertEqual(retention.retired_roots(), [])

    def test_collector_keeps_roots_with_any_evidence_of_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            used, chosen, unit, mounted, running = (base / name for name in ("used", "chosen", "unit", "mount", "rt"))
            runtime = running / "lib/python3/site-packages/tauceti_worker"
            for root in (used, chosen, unit, mounted, runtime):
                self.retired_fixture(root)
            (running / "pyvenv.cfg").write_text("")
            (running / "bin").mkdir()
            (running / "bin/python").symlink_to(sys.executable)
            sleeper = "import time; time.sleep(60)"
            children = [
                subprocess.Popen([sys.executable, "-c", sleeper], cwd=used / "logs/w"),  # holds no lock
                subprocess.Popen(
                    [sys.executable, "-c", sleeper], env={**os.environ, "TAUCETI_LEGACY_DATA_ROOT": str(chosen)}
                ),
                subprocess.Popen([str(running / "bin/python"), "-c", sleeper]),  # its runtime is running
            ]
            for child in children:
                self.addCleanup(child.wait)
                self.addCleanup(child.kill)
            time.sleep(0.2)
            from tauceti_worker.retention import mount_points

            mounts = [*(mount_points() or []), mounted / "checkouts"]
            extra = [chosen, unit, mounted, runtime]
            with self.retiring(base, used, extra=extra, units=[f"Environment=X={unit}"], mounts=mounts):
                report = retention.fresh_report()
                retention.retire_roots(report)
                for root in (used, chosen, unit, mounted, runtime):
                    self.assertTrue(self.protected(report, root), (root, report["protected"]))
                    self.assertTrue((root / "state/w/recovery/saved/patch.diff").exists())
                for child in children:
                    child.kill()
                    child.wait()
                with patch.object(retention, "_unit_texts", return_value=[]):
                    retention.retire_roots(retention.fresh_report())
            for root in (used, chosen, unit, runtime):
                self.assertEqual(sorted(p.name for p in root.iterdir()), ["prompts"], root)
            self.assertTrue((mounted / "state").exists())

    def test_retirement_stages_at_once_and_deletes_within_the_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            old = base / "old-install"
            self.retired_fixture(old)
            with self.retiring(base, old):
                report = retention.fresh_report()
                retention.retire_roots(report, deadline=time.monotonic() - 1)
                staged = sorted(p.name for p in old.iterdir() if p.name != "prompts")
                self.assertEqual(len(staged), 3)
                self.assertTrue(all(name.startswith(retention.RETIRED_TRASH) for name in staged))
                self.assertEqual(retention.retired_roots(), [old])  # still has staged trash
                retention.retire_roots(retention.fresh_report())
                self.assertEqual(sorted(p.name for p in old.iterdir()), ["prompts"])

    def purging(self, base, home, package, **env):
        from tauceti_worker import purge

        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ))
        for name in ("TAUCETI_LEGACY_DATA_ROOT", "XDG_DATA_HOME", "XDG_STATE_HOME", *purge.OVERRIDES):
            os.environ.pop(name, None)
        os.environ.update(XDG_CONFIG_HOME=str(home / ".config"), TAUCETI_RUNTIME_DIR=str(base / "run"), **env)
        for target in (paths, purge):
            stack.enter_context(patch.object(target, "login_home", return_value=home))
        stack.enter_context(patch.object(paths, "INSTALLED", True))
        stack.enter_context(patch.object(paths, "HERE", package))
        stack.enter_context(patch.object(purge, "HERE", package))
        stack.enter_context(patch.object(retention, "HERE", package))
        stack.enter_context(patch.object(retention, "legacy_project", return_value=False))
        stack.enter_context(patch.object(retention, "_unit_texts", return_value=[]))
        stack.enter_context(patch.object(purge, "VERIFY_TMP", base / "tmp"))
        stack.enter_context(patch.object(purge, "stop_services", return_value=[]))
        return stack

    def test_purge_removes_only_what_is_provably_ours(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        from tauceti_worker import purge

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            package = base / "venv/site-packages/tauceti_worker"
            data = home / (
                "Library/Application Support/tauceti/data" if sys.platform == "darwin" else ".local/share/tauceti"
            )
            state = home / (
                "Library/Application Support/tauceti/state" if sys.platform == "darwin" else ".local/state/tauceti"
            )
            for root in (data, package):
                self.retired_fixture(root)
            shared = base / "shared-budget"  # an operator override: maybe not only ours
            for path in (state / "activity", home / ".cache/tauceti/builds", home / ".cache/tauceti-claims", shared):
                path.mkdir(parents=True)
            (base / "tmp").mkdir()
            (base / "tmp" / f"tauceti-verification-{os.getuid()}-key").mkdir()
            (home / ".config/tauceti").mkdir(parents=True)
            (home / ".config/tauceti/workers.toml").write_text("")
            (home / ".elan/toolchains").mkdir(parents=True)
            with self.purging(base, home, package, TAUCETI_BUDGET_DIR=str(shared)):
                planned, _roots, overridden, refused = purge.plan()
                for path in planned:  # nothing outside the fixture may ever be planned
                    self.assertTrue(path.is_relative_to(base), path)
                self.assertIn(data, planned)
                self.assertIn(package / "state", planned)
                self.assertNotIn(shared, planned)
                self.assertIn(f"TAUCETI_BUDGET_DIR={shared}", overridden)
                self.assertNotIn(home / ".config/tauceti", planned)
                self.assertEqual(refused, [])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(purge.cmd_purge(SimpleNamespace(yes=False, config=False)), 0)
                self.assertTrue(data.exists())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(purge.cmd_purge(SimpleNamespace(yes=True, config=False)), 0)
                purge.stop_services.assert_called_once()
            for gone in (
                data,
                state,
                home / ".cache/tauceti",
                home / ".cache/tauceti-claims",
                base / "tmp" / f"tauceti-verification-{os.getuid()}-key",
            ):
                self.assertFalse(gone.exists(), gone)
            self.assertEqual(sorted(p.name for p in package.iterdir()), ["prompts"])
            for kept in (shared, home / ".config/tauceti/workers.toml", home / ".elan/toolchains"):
                self.assertTrue(kept.exists(), kept)

    def test_purge_refuses_symlinked_ancestors_and_busy_workers(self):
        import fcntl
        from types import SimpleNamespace

        from tauceti_worker import purge

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            package = base / "venv/site-packages/tauceti_worker"
            (base / "elsewhere/tauceti").mkdir(parents=True)
            home.mkdir()
            (home / ".cache").symlink_to(base / "elsewhere")
            data = base / "data"
            self.retired_fixture(data)
            with self.purging(base, home, package, TAUCETI_LEGACY_DATA_ROOT=str(data)):
                _planned, _roots, _overridden, refused = purge.plan()
                self.assertIn(f"{home / '.cache/tauceti'}: reached through a symbolic link", refused)
                with open(data / "state/w/instance.lock") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(config.Die, "uses|holds a lock"):
                        purge.cmd_purge(SimpleNamespace(yes=True, config=False))
            self.assertTrue((data / "state/w/recovery/saved/patch.diff").exists())
            self.assertTrue((base / "elsewhere/tauceti").exists())

    def test_purge_stops_services_before_removing_their_units(self):
        from tauceti_worker import purge

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            units = base / "config/systemd/user"
            units.mkdir(parents=True)
            for name in ("tauceti-workers.service", "tauceti-cleanup.timer", "tauceti-cleanup.service"):
                (units / name).write_text("")
            calls = []

            def run(argv, **kwargs):
                calls.append(argv[2:])
                return SimpleNamespace(returncode=0, stdout="inactive", stderr=b"")

            from types import SimpleNamespace

            with (
                patch.object(purge.sys, "platform", "linux"),
                patch.object(purge, "_unit_directories", return_value=[units]),
                patch.object(purge.shutil, "which", return_value="/bin/systemctl"),
                patch.object(purge.subprocess, "run", side_effect=run),
            ):
                self.assertEqual(purge.stop_services(), [])
            names = ["tauceti-cleanup.service", "tauceti-cleanup.timer", "tauceti-workers.service"]
            self.assertEqual(calls[0], ["stop", *names])
            self.assertEqual(calls[1], ["disable", *names])
            self.assertEqual(list(units.iterdir()), [])

    def test_default_and_invalid_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            self.assertEqual(paths.legacy_data_root(Path("/old-layout")), Path("/old-layout"))
            os.environ["TAUCETI_LEGACY_DATA_ROOT"] = "relative"
            with self.assertRaisesRegex(ValueError, "absolute path"):
                paths.legacy_data_root()


if __name__ == "__main__":
    unittest.main()
