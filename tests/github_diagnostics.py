#!/usr/bin/env python3
"""Failed PR fetches retain actionable, bounded diagnostics through survey/work/status."""

import contextlib
import importlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

wu = importlib.import_module("tauceti_worker.work_units")
gh = tc.GitHub("owner/repo")


def failure(stderr, stdout="", rc=1):
    with patch.object(gh, "_gh", return_value=subprocess.CompletedProcess([], rc, stdout, stderr)):
        try:
            gh.pr_list(["number"], author="someone", state="all")
        except tc.GitHubError as exc:
            return str(exc)
    raise AssertionError("failed command did not raise")


message = failure("GraphQL: API rate limit exceeded")
assert "repo=owner/repo, state=all, author=someone, exit=1" in message
assert "stderr: GraphQL: API rate limit exceeded" in message
assert "stdout: network unavailable" in failure("", "network unavailable")
assert "exit=-9" in failure(None, None, -9)
assert "no stderr or stdout" in failure(None, None)
assert "[truncated]" in failure("x" * 10000) and len(failure("x" * 10000)) < 2200
with patch.dict("os.environ", {"GH_TOKEN": "test-secret"}):
    sanitized = failure("test-secret ghp_exampletoken github_pat_example Authorization: Bearer abc123\nend")
assert all(secret not in sanitized for secret in ("test-secret", "ghp_exampletoken", "github_pat_example", "abc123"))
assert "\n" not in sanitized
assert "password" not in failure("https://user:password@github.com")

# The real survey must preserve the error and stop before identity/metadata calls.
with patch.object(gh, "pr_list", side_effect=tc.GitHubError(message)):
    sv = tc.survey(SimpleNamespace(wid="test"), gh, None, None)
assert sv.github_failed and sv.errors == [message]

# Regression: work used to discard this stored diagnostic and print a generic message.
with patch.object(wu, "survey", return_value=sv):
    try:
        wu.run_round(SimpleNamespace(cfg=None, gh=None, rs=None, counters=None), SimpleNamespace(dry_run=True))
    except tc.NoProgress as exc:
        assert message in str(exc) and "aborting round" in str(exc)
    else:
        raise AssertionError("failed survey did not abort work")

# Status JSON remains parseable with diagnostics under survey.errors.
stdout = io.StringIO()
with (
    patch.object(
        tc.cli.Config, "resolve", return_value=SimpleNamespace(sbcache=Path("/unused"), state=Path("/unused"))
    ),
    patch.object(tc.cli, "survey", return_value=sv),
    patch.object(tc.cli, "Quota") as quota,
    contextlib.redirect_stdout(stdout),
    contextlib.redirect_stderr(io.StringIO()),
):
    quota.return_value.choose.return_value = (None, {})
    assert tc.cli.cmd_status(SimpleNamespace(json=True)) == 1
assert json.loads(stdout.getvalue())["survey"]["errors"] == [message]
print("PASS: PR-list failure diagnostics survive survey, work, and JSON status; secrets redacted and output bounded")
