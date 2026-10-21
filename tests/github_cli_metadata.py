#!/usr/bin/env python3
"""External review CLI metadata shares successful reads without caching failures or live PRs."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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

print("GitHub CLI metadata cache checks passed")
