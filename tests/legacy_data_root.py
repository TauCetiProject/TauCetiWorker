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

    def test_default_and_invalid_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            self.assertEqual(paths.legacy_data_root(Path("/old-layout")), Path("/old-layout"))
            os.environ["TAUCETI_LEGACY_DATA_ROOT"] = "relative"
            with self.assertRaisesRegex(ValueError, "absolute path"):
                paths.legacy_data_root()


if __name__ == "__main__":
    unittest.main()
