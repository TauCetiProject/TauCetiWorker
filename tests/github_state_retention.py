"""Recovery CLI request contracts; all service calls are mocked."""

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "services/github-state/scripts/storage_recovery.py"
spec = importlib.util.spec_from_file_location("storage_recovery", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class RetentionCLI(unittest.TestCase):
    def invoke(self, args):
        with (
            patch.object(sys, "argv", [str(SCRIPT), *args]),
            patch.object(cli, "call", return_value={"ok": True}) as call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.main()
        return call

    def test_inspection_does_not_send_policy(self):
        self.invoke(["retention"]).assert_called_once_with("test", "/admin/storage/retention", {})

    def test_policy_is_explicit_and_complete(self):
        self.invoke(
            [
                "--environment",
                "production",
                "retention",
                "--mode",
                "enforce",
                "--disable",
                "expired_changes",
                "--pin",
                "daily_archives",
            ]
        ).assert_called_once_with(
            "production",
            "/admin/storage/retention",
            {
                "policy": {
                    "mode": "enforce",
                    "disabled": ["expired_changes"],
                    "pinned": ["daily_archives"],
                }
            },
        )

    def test_overrides_without_mode_never_contact_service(self):
        for flag in [["--pin", "expired_deliveries"], ["--disable", "raw_webhooks"]]:
            with (
                self.subTest(flag=flag),
                patch.object(sys, "argv", [str(SCRIPT), "retention", *flag]),
                patch.object(cli, "call") as call,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                cli.main()
            self.assertEqual(error.exception.code, 2)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
