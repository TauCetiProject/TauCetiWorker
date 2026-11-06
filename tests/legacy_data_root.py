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

    def test_default_and_invalid_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            self.assertEqual(paths.legacy_data_root(Path("/old-layout")), Path("/old-layout"))
            os.environ["TAUCETI_LEGACY_DATA_ROOT"] = "relative"
            with self.assertRaisesRegex(ValueError, "absolute path"):
                paths.legacy_data_root()

    def test_installed_cleanup_keeps_the_selected_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            units = root / "units"
            with (
                patch.dict(
                    os.environ,
                    {
                        "TAUCETI_LEGACY_DATA_ROOT": str(root / "durable"),
                        "TAUCETI_ACTIVITY_DIR": str(root / "activity"),
                    },
                ),
                patch("tauceti_worker.worker_manager._service_path", return_value=units / "workers.service"),
                patch.object(retention.subprocess, "run"),
            ):
                retention.install_timer()
            unit = (units / "tauceti-cleanup.service").read_text()
            self.assertIn("TAUCETI_LEGACY_DATA_ROOT=" + str(root / "durable"), unit)


if __name__ == "__main__":
    unittest.main()
