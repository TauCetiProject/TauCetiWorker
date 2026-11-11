"""Bubble reviewers receive sources at the PR's dependency pin or do not start."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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
        assert not (source / agents._REVIEW_SOURCE_STAGING).exists()
        assert source.stat().st_mode & 0o777 == 0o700
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


def test_failed_staging_scope_is_removed_without_waiting_for_a_later_prune():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        try:
            with agents._review_mathlib_staging(base) as (source, _):
                (source / "partial").write_text("unfinished source")
                raise RuntimeError("interrupted preparation")
        except RuntimeError:
            pass
        assert not list(base.iterdir())


def test_prune_skips_live_staging_then_reclaims_it_after_producer_sigkill():
    code = """
from pathlib import Path
import sys,time
from tauceti_worker import agents
with agents._review_mathlib_staging(Path(sys.argv[1])) as (source, fd):
    (source/'partial').write_text('unfinished source')
    print(source,flush=True)
    time.sleep(100)
"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        producer = subprocess.Popen([sys.executable, "-c", code, tmp], stdout=subprocess.PIPE, text=True)
        try:
            source = Path(producer.stdout.readline().strip())
            assert source.parent.parent == base
            agents._prune_review_mathlib_staging(base)
            assert (source / "partial").read_text() == "unfinished source"
            producer.kill()
            producer.wait(timeout=5)
            agents._prune_review_mathlib_staging(base)
            assert not source.exists()
        finally:
            if producer.poll() is None:
                producer.kill()
            producer.wait(timeout=5)
            producer.stdout.close()


def test_downloader_inherits_staging_lease_after_producer_death():
    code = """
from pathlib import Path
import subprocess,sys,time,json
from tauceti_worker import agents
with agents._review_mathlib_staging(Path(sys.argv[1])) as (source, fd):
    (source/'partial').write_text('unfinished source')
    child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(100)'],
        pass_fds=(fd,), stdout=subprocess.DEVNULL)
    print(json.dumps(dict(path=str(source), child=child.pid)),flush=True)
    time.sleep(100)
"""
    import fcntl

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        producer = subprocess.Popen([sys.executable, "-c", code, tmp], stdout=subprocess.PIPE, text=True)
        child = None
        try:
            row = json.loads(producer.stdout.readline())
            source, child = Path(row["path"]), row["child"]
            producer.kill()
            producer.wait(timeout=5)
            agents._prune_review_mathlib_staging(base)
            assert (source / "partial").is_file()
            os.kill(child, signal.SIGKILL)
            child = None
            with (source.parent / agents._REVIEW_SOURCE_STAGING).open() as lease:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(lease, fcntl.LOCK_UN)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("downloader lease did not release") from None
                        time.sleep(0.01)
            agents._prune_review_mathlib_staging(base)
            assert not source.exists()
        finally:
            if producer.poll() is None:
                producer.kill()
            producer.wait(timeout=5)
            producer.stdout.close()
            if child:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)


def test_producer_scope_exit_does_not_delete_a_surviving_downloader_input():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        child = None
        try:
            with agents._review_mathlib_staging(base) as (source, fd):
                (source / "partial").write_text("downloader input")
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(100)"], pass_fds=(fd,))
            assert (source / "partial").read_text() == "downloader input"
            child.kill()
            child.wait(timeout=5)
            agents._prune_review_mathlib_staging(base)
            assert not source.exists()
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


def test_interrupted_prune_resumes_after_deleting_its_marker():
    from tauceti_worker import shared_mathlib

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        staging = base / "mathlib-src-tmp-owned"
        staging.mkdir()
        marker = staging / agents._REVIEW_SOURCE_STAGING
        marker.write_text(json.dumps(dict(version=1, uid=os.getuid())))
        (staging / "partial").write_text("unfinished deletion")

        def interrupted(path):
            assert path.name.endswith(".retention-deleting")
            (path / agents._REVIEW_SOURCE_STAGING).unlink()
            raise OSError("interrupted removal")

        with patch.object(shared_mathlib, "remove_tree", side_effect=interrupted):
            agents._prune_review_mathlib_staging(base)
        trash = staging.with_name(staging.name + ".retention-deleting")
        assert (trash / "partial").read_text() == "unfinished deletion"
        assert not (trash / agents._REVIEW_SOURCE_STAGING).exists()
        agents._prune_review_mathlib_staging(base)
        assert not trash.exists()


def test_unknown_staging_and_published_sources_remain_while_owned_stale_trash_is_retried():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        legacy = base / "mathlib-src-tmp-legacy"
        legacy.mkdir()
        (legacy / "source").write_text("unattributed")
        published = base / f"mathlib-src-{REV}"
        published.mkdir()
        (published / "Mathlib.lean").write_text("possibly mounted Bubble input")
        (base / "mathlib-src-tmp-link").symlink_to(published, target_is_directory=True)
        stale = base / "mathlib-src-tmp-owned.retention-deleting"
        stale.mkdir()
        marker = stale / agents._REVIEW_SOURCE_STAGING
        marker.write_text(json.dumps(dict(version=1, uid=os.getuid())))
        (stale / "partial").write_text("unused staging")
        markerless = base / "mathlib-src-tmp-disposed.retention-deleting"
        markerless.mkdir()
        (markerless / "partial").write_text("interrupted deletion after marker removal")
        from tauceti_worker import retention, work_recovery

        for unavailable in ("processes", "mounts"):
            with (
                patch.object(
                    work_recovery, "process_references", return_value=None if unavailable == "processes" else []
                ),
                patch.object(retention, "mount_points", return_value=None if unavailable == "mounts" else []),
            ):
                agents._prune_review_mathlib_staging(base)
                assert stale.exists()
                assert markerless.exists()
        agents._prune_review_mathlib_staging(base)
        assert not stale.exists()
        assert not markerless.exists()
        assert (legacy / "source").read_text() == "unattributed"
        assert (published / "Mathlib.lean").read_text() == "possibly mounted Bubble input"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
