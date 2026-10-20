#!/usr/bin/env python3
"""S2 admin CLI defaults to read-only and requires explicit identities to mutate."""

import contextlib
import importlib.util
import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services/github-state/scripts"))
spec = importlib.util.spec_from_file_location("state_migrate", ROOT / "services/github-state/scripts/migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


def run(*args):
    calls = []

    def call(environment, body):
        calls.append((environment, body))
        return {"phase": "source_verified"}

    with patch.object(sys, "argv", ["migrate.py", *args]), patch.object(migrate, "call", call):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            migrate.main()
    return calls


for command in ["preflight", "freeze", "resume", "abandon"]:
    assert run("--environment", "production", command) == [
        ("production", {"repository": "TauCetiProject/TauCeti", "environment": "production", "operation": "preflight"})
    ]
for args in [[], ["--environment", "test", "--apply", "freeze"], ["--environment", "test", "--steps", "0"]]:
    try:
        run(*args)
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("CLI accepted missing environment/identity or invalid bounds")

calls = run(
    "--environment",
    "test",
    "--apply",
    "--generation",
    "c707ddba-076d-4a63-8696-96533031ce88",
    "--target",
    "neon-test-branch-database",
    "--steps",
    "10",
    "resume",
)
assert len(calls) == 1, "stop when the source is verified"
assert calls[0][1]["operation"] == "step" and calls[0][1]["apply"] is True


# A credential-bearing proxy error body must never be read or printed.
class SecretBody:
    def close(self):
        pass

    def read(self, *args):
        raise AssertionError("must not read arbitrary error body")


with patch.object(migrate, "service", return_value=("https://service.test", Path("/private"))):
    with patch.object(Path, "read_text", return_value="private-admin-token"):
        for error in [
            urllib.error.HTTPError("https://service.test", 500, "private password", {}, SecretBody()),
            urllib.error.URLError("private database credential"),
        ]:
            with patch.object(migrate.urllib.request, "urlopen", side_effect=error):
                try:
                    migrate.call("test", {})
                except SystemExit as result:
                    assert "private" not in str(result) and "credential" not in str(result)
                else:
                    raise AssertionError("transport error was ignored")

print("S2 migration CLI safety checks passed")
