"""Inventory regressions use committed Git fixtures, independent of live accounts."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts/forge-inventory.py"
SPEC = importlib.util.spec_from_file_location("forge_inventory", SOURCE)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def fixture(self, files):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value if isinstance(value, bytes) else value.encode())
        self.git(root, "init", "-q")
        self.git(root, "add", ".")
        self.git(
            root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "-qm",
            "Fixture",
        )
        return root

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    def rows(self, root):
        result = inventory.inspect("Fixture", root, "HEAD")
        return result, {row["path"]: row["categories"] for row in result["files"]}

    def test_ownership_and_forge_configuration_need_no_regex_hits(self):
        files = {
            ".github/CODEOWNERS": "* @Example/humans\n",
            "docs/CODEOWNERS": "* @Example/docs\n",
            ".github/ISSUE_TEMPLATE/plain.yml": "name: Plain\n",
            ".github/pull_request_template.md": "Describe change\n",
            ".github/workflows/test_plain.yml": "name: Test\n",
            ".forgejo/options.ini": "key=value\n",
        }
        _, rows = self.rows(self.fixture(files))
        self.assertEqual(set(rows), set(files))

    def test_extensionless_entrypoints_and_configuration_types(self):
        files = {
            "build": "#!/bin/sh\ngh pr list\n",
            "scripts/publish": "#!/bin/sh\ngh --repo X pr list\n",
            "Dockerfile": "RUN gh auth setup-git\n",
            "worker.service": "Environment=GH_TOKEN\n",
            "worker.example": "GH_TOKEN=x\n",
            "config.json": '{"repo":"TauCetiProject/TauCeti"}',
            "lakefile.lean": "-- https://github.com/one/dependency\n",
        }
        _, rows = self.rows(self.fixture(files))
        self.assertEqual(set(rows), set(files))

    def test_actions_environment_and_variable_families(self):
        files = {
            "publication.py": 'run = "GITHUB_RUN_ID"\nattempt = "GITHUB_RUN_ATTEMPT"\n',
            "summary.py": 'summary = "GITHUB_STEP_SUMMARY"\n',
            "settings.yml": "cap: ${{ vars.CAP }}\n",
            "credentials.sh": "export GH_CONFIG_DIR=x GH_REPO=one/project\n",
        }
        _, rows = self.rows(self.fixture(files))
        self.assertEqual(set(rows), set(files))
        self.assertEqual(rows["publication.py"]["actions_context"], [1, 2])
        self.assertEqual(rows["credentials.sh"]["credentials"], [1])

    def test_directory_exclusions_and_markdown_allowlist(self):
        files = {
            "tests/hidden.py": '"GITHUB_RUN_ID"',
            "records/hidden.json": '"GITHUB_RUN_ID"',
            "docs/hidden.md": "gh pr list",
            "math/statement.md": "gh pr list",
            "README.md": "gh pr list",
            "prompts/task.md": "gh pr list",
            "AGENTS.md": "gh pr list",
            "CLAUDE.md": "gh pr list",
        }
        _, rows = self.rows(self.fixture(files))
        self.assertEqual(set(rows), {"prompts/task.md", "AGENTS.md", "CLAUDE.md"})

    def test_raw_blob_line_numbers_and_encoding_skips(self):
        root = self.fixture({"lines.py": "# text\u2028GITHUB_RUN_ID\n", "bad.py": b"\xff"})
        result, rows = self.rows(root)
        self.assertEqual(rows["lines.py"]["actions_context"], [1])
        self.assertEqual(result["skipped"], [{"path": "bad.py", "reason": "non_utf8", "size": 1}])

    def test_replay_is_independent_of_later_head_changes(self):
        root = self.fixture({"first.py": '"GITHUB_RUN_ID"'})
        first, _ = self.rows(root)
        (root / "second.py").write_text('"GITHUB_SHA"')
        self.git(root, "add", ".")
        self.git(
            root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "-qm",
            "Next",
        )
        self.assertEqual(inventory.inspect("Fixture", root, first["source_revision"]), first)


if __name__ == "__main__":
    unittest.main()
