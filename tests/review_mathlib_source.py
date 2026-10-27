"""Bubble reviewers receive sources at the PR's dependency pin or do not start."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, github  # noqa: E402
from tauceti_worker.config import NoProgress  # noqa: E402

HEAD, REV = "a" * 40, "b" * 40


def test_sources_match_the_reviewed_head_and_cached_snapshots_are_reused():
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        if "checkout" in command:
            (Path(command[2]) / "Mathlib.lean").write_text("-- pinned sources\n")
        return SimpleNamespace(returncode=0)

    manifest = SimpleNamespace(returncode=0, stdout=json.dumps({"packages": [{"name": "mathlib", "rev": REV}]}))
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(github, "gh_run", return_value=manifest) as api,
        patch.object(agents.subprocess, "run", side_effect=git),
    ):
        cfg = SimpleNamespace(state=Path(tmp))
        source = agents._review_mathlib_source(cfg, HEAD)
        assert source == Path(tmp) / "refs" / f"mathlib-src-{REV}"
        assert (source / "Mathlib.lean").is_file() and not (source / ".git").exists()
        assert f"?ref={HEAD}" in api.call_args.args[0][2]
        assert any(command[-1] == REV and "fetch" in command for command in calls)
        assert agents._review_mathlib_source(cfg, HEAD) == source
        assert len(calls) == 3


def test_staging_errors_stop_review_instead_of_omitting_required_sources():
    good = {"packages": [{"name": "mathlib", "rev": REV}]}
    cases = [
        (1, "unavailable", 0),
        (0, "not json", 0),
        (0, json.dumps({"packages": "invalid"}), 0),
        (0, json.dumps({"packages": [{"name": "mathlib", "rev": "master"}]}), 0),
        (0, json.dumps(good), 1),
        (0, json.dumps(good), subprocess.TimeoutExpired("git", 600)),
        (0, json.dumps(good), FileNotFoundError("git")),
        (0, json.dumps(good), 0),  # checkout exits cleanly but no Mathlib source exists
    ]
    for rc, body, git_result in cases:
        response = SimpleNamespace(returncode=rc, stdout=body)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(github, "gh_run", return_value=response),
            patch.object(agents.subprocess, "run") as git,
        ):
            if isinstance(git_result, Exception):
                git.side_effect = git_result
            else:
                git.return_value = SimpleNamespace(returncode=git_result)
            try:
                agents._review_mathlib_source(SimpleNamespace(state=Path(tmp)), HEAD)
            except NoProgress as error:
                assert "no review started" in str(error)
            else:
                raise AssertionError("missing required sources did not stop the review")
            assert not list(Path(tmp).glob("refs/mathlib-src-tmp-*"))


def test_projects_without_mathlib_can_review_without_a_mathlib_mount():
    response = SimpleNamespace(returncode=0, stdout='{"packages": []}')
    with patch.object(github, "gh_run", return_value=response), patch.object(agents.subprocess, "run") as git:
        assert agents._review_mathlib_source(SimpleNamespace(state=Path("/unused")), HEAD) is None
        git.assert_not_called()


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
