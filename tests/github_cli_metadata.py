#!/usr/bin/env python3
"""External review CLI metadata shares successful reads without caching failures or live PRs."""

import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import github_state
from tauceti_worker import github_transport as tr

sha = "a" * 40
with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"GH_TOKEN": "one", "TAUCETI_GITHUB_SHARED_DIR": tmp}),
):
    immutable = ["gh", "api", "repos/org/review/commits/" + sha, "--jq", ".sha"]
    success = subprocess.CompletedProcess(immutable, 0, sha + "\n", "")
    with patch.object(tr, "_execute", return_value=success) as network:
        assert tr.execute(immutable).stdout == sha + "\n"
        tr.invalidate(["api", "repos/org/code/issues/7/comments", "-f", "body=hello"])
        assert tr.execute(immutable).stdout == sha + "\n"
        network.assert_called_once()
        with patch.dict(os.environ, {"GH_TOKEN": "two"}):
            assert tr.execute(immutable).stdout == sha + "\n"
        assert network.call_count == 2

    tree = ["gh", "api", "/repos/org/review/git/trees/main?recursive=1"]
    with patch.object(tr, "_execute", return_value=subprocess.CompletedProcess(tree, 0, "{}", "")) as network:
        now = tr.time.time()
        for stamp in (now, now + 30, now + 61):
            with patch.object(tr.time, "time", return_value=stamp):
                assert tr.execute(tree).stdout == "{}"
        assert network.call_count == 2

    failure = subprocess.CompletedProcess([], 1, "", "unavailable")
    with patch.object(tr, "_execute", return_value=failure) as network:
        args = ["gh", "api", "repos/org/review/git/commits/" + sha]
        assert tr.execute(args).returncode == 1
        assert tr.execute(args).returncode == 1
        assert network.call_count == 2

    for args in (
        ["api", "repos/org/code/pulls/7"],
        ["api", "repos/org/review/commits/main"],
        ["api", "repos/org/review/commits/" + sha, "--include"],
        ["api", "repos/org/review/commits/" + sha, "-X", "DELETE"],
    ):
        with patch.object(tr, "_execute", return_value=success) as network:
            tr.execute(["gh", *args])
            tr.execute(["gh", *args])
            assert network.call_count == 2

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=tmp):
    args = ["gh", "pr", "view", "7", "--repo", "org/code", "--json", "title,body"]
    row = {"title": "Title", "body": "Description", "headRefOid": sha}
    with patch.object(github_state, "read", return_value=(True, row)), patch.object(tr, "_execute") as network:
        import json

        assert json.loads(tr.shared_pr_view(args[1:]).stdout) == {"title": "Title", "body": "Description"}
        network.assert_not_called()
        for suffix in (["--jq", ".title"], ["--web"]):
            assert tr.shared_pr_view([*args[1:], *suffix]) is None
        with tr.read_context(fresh=True, direct=True):
            assert tr.shared_pr_view(args[1:]) is None
        for env in ({"TAUCETI_GITHUB_CACHE_BYPASS": "1"}, {"GH_HOST": "enterprise.example"}):
            with patch.dict(os.environ, env):
                assert tr.shared_pr_view(args[1:]) is None
        assert tr.shared_pr_view([*args[1:-1], "headRefOid,state"]) is None
        output = io.StringIO()
        with patch.object(sys, "argv", [tr.__file__, "--proxy", *args[1:]]), contextlib.redirect_stdout(output):
            try:
                runpy.run_path(tr.__file__, run_name="__main__")
            except SystemExit as result:
                assert result.code == 0
        assert json.loads(output.getvalue()) == {"title": "Title", "body": "Description"}
    fallback = subprocess.CompletedProcess(args, 0, '{"title":"Live","body":"Fresh"}', "")
    with (
        patch.object(github_state, "read", return_value=(False, None)) as mirror,
        patch.object(tr, "_execute", return_value=fallback),
    ):
        assert tr.shared_pr_view(args[1:]) is None
        assert tr.execute(args) is fallback
        mirror.assert_called_once()  # Library fallback must not ask the unavailable mirror twice.
    with patch.object(github_state, "read", return_value=(True, {"title": "missing body"})):
        assert tr.shared_pr_view(args[1:]) is None

assert (
    tr.query_operation("query { repository { pullRequests(states: [CLOSED, MERGED]) { nodes { number } } } }")
    == "closed_prs"
)
print("GitHub CLI metadata cache checks passed")
