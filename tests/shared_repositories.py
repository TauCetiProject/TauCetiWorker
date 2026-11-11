#!/usr/bin/env python3
"""Shared read-only snapshots retain old rounds and Git provenance inside sandbox mounts."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import shared_repositories as shared

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_SHARED_REPOSITORIES_DIR": tmp + "/shared"}):
    source = Path(tmp) / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(["git", *map(str, args)], capture_output=True, text=True, check=True)

    git("init", "-b", "main", source)
    (source / "data").write_text("old")
    git("-C", source, "add", "data")
    git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
    original = shared._git
    calls = []

    def local_git(*args):
        args = [source.as_uri() if str(a) == "https://github.com/org/roadmap" else a for a in args]
        calls.append(tuple(args))
        return original(*args)

    with patch.object(shared, "_git", side_effect=local_git):
        old = shared.snapshot("org/roadmap")
        assert old and (old / "data").read_text() == "old"
        assert old.parent.name == "snapshots-v3"
        legacy = old.parent.parent / "snapshots-v2" / ("e" * 40)
        legacy.mkdir(parents=True)
        (legacy / "old-consumer").write_text("legacy input")
        os.utime(legacy, (time.time() - 2 * 86400,) * 2)
        assert (old / ".git").is_dir() and git("-C", old, "rev-parse", "HEAD").stdout.strip() == old.name
        assert (old / "data").stat().st_mode & 0o222 == 0
        before = len(calls)
        assert shared.snapshot("org/roadmap") == old and len(calls) == before
        expired = time.time() - 2 * 86400
        os.utime(old, (expired, expired))
        garbage = old.parent / ("0" * 40)
        garbage.mkdir()
        (garbage / "unleased").write_text("disposable")
        os.utime(garbage, (expired, expired))
        (source / "data").write_text("new")
        git("-C", source, "add", "data")
        git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "new")
        new = shared.snapshot("org/roadmap", ttl=0)
        assert new != old and (new / "data").read_text() == "new" and (old / "data").read_text() == "old"
        assert not garbage.exists(), "retaining earlier leases must not disable collection of unleased trees"
        assert (legacy / "old-consumer").read_text() == "legacy input", "old consumers keep their separate namespace"
        destination = Path(tmp) / "worker/refs/roadmap"
        assert shared.attach("org/roadmap", destination, ttl=3600)
        assert destination.is_symlink() and destination.resolve() == new
        assert git("-C", destination, "config", "remote.origin.pushurl").stdout.strip() == "no_push"
        assert not (new / ".git/objects/info/alternates").exists()

        def failing_fetch(*args):
            if "fetch" in args:
                return subprocess.CompletedProcess(args, 1, "", "offline")
            return local_git(*args)

        with patch.object(shared, "_git", side_effect=failing_fetch):
            assert shared.snapshot("org/roadmap", ttl=0) == new, (
                "fetch failure must not revert to the bare mirror's original HEAD"
            )
        (source / "data").write_text("third")
        git("-C", source, "add", "data")
        git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "third")
        third_sha = git("-C", source, "rev-parse", "HEAD").stdout.strip()

        def interrupted_clone(*args):
            if args[0] == "clone" and "--no-local" in args:
                Path(args[-1]).mkdir()
                (Path(args[-1]) / "partial").write_text("incomplete")
                raise subprocess.TimeoutExpired("git", 120)
            return local_git(*args)

        with patch.object(shared, "_git", side_effect=interrupted_clone):
            try:
                shared.snapshot("org/roadmap", ttl=0)
            except subprocess.TimeoutExpired:
                pass
            else:
                raise AssertionError("expected interrupted clone")
        assert not new.with_name(third_sha).exists(), "partial snapshots are never published"
        assert not list(new.parent.glob(".staging-*"))
        assert (shared.snapshot("org/roadmap", ttl=0) / "data").read_text() == "third"
        assert (old / "data").read_text() == "old", "a newer process lease must not replace an earlier one"
    # Attachments follow their destinations, while borrowed readers retain their
    # own versions only until the context exits.
    with (
        patch.dict(os.environ, TAUCETI_SHARED_REPOSITORIES_DIR=tmp + "/scoped"),
        patch.object(shared, "_git", side_effect=local_git),
    ):
        first_dest, second_dest = Path(tmp) / "first", Path(tmp) / "second"
        assert shared.attach("org/roadmap", first_dest, ttl=3600)
        first = first_dest.resolve()
        with shared.borrowed_snapshot("org/roadmap") as borrowed:
            assert borrowed == first
            os.utime(first, (expired, expired))
            (source / "data").write_text("fourth")
            git("-C", source, "add", "data")
            git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "fourth")
            assert shared.attach("org/roadmap", second_dest, ttl=0)
            assert shared.attach("org/roadmap", first_dest, ttl=3600)
            assert first_dest.resolve() == second_dest.resolve() != first
            assert (borrowed / "data").read_text() == "third"
        with shared.borrowed_snapshot("org/roadmap"):
            assert not first.exists(), "released readers and superseded attachments must not pin old snapshots"
        # Releasing a borrowed reader after an exception does not keep its
        # version pinned. Other attachments continue to own their current input.
        try:
            with shared.borrowed_snapshot("org/roadmap") as failed:
                os.utime(failed, (expired, expired))
                raise RuntimeError("reader failed")
        except RuntimeError:
            pass
        (source / "data").write_text("fifth")
        git("-C", source, "add", "data")
        git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "fifth")
        assert shared.attach("org/roadmap", first_dest, ttl=0)
        assert (failed / "data").read_text() == "fourth", "the other destination still owns this revision"
        assert shared.attach("org/roadmap", second_dest, ttl=3600)
        with shared.borrowed_snapshot("org/roadmap"):
            assert not failed.exists(), "failed readers and superseded destinations must release their version"
    with (
        patch.dict(os.environ, TAUCETI_SHARED_REPOSITORIES_DIR=tmp + "/process-lifetime"),
        patch.object(shared, "_git", side_effect=local_git),
    ):
        ready = Path(tmp) / "reader-ready"
        program = """import sys,time,os
from pathlib import Path
from tauceti_worker import shared_repositories as s
original=s._git
def local(*args):
    return original(*[sys.argv[1] if str(a)=='https://github.com/org/roadmap' else a for a in args])
s._git=local
with s.borrowed_snapshot('org/roadmap') as source:
    temporary=Path(sys.argv[2]+'.tmp');temporary.write_text(str(source));temporary.replace(sys.argv[2])
    time.sleep(60)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", program, source.as_uri(), str(ready)],
            env={**os.environ, "PYTHONPATH": str(Path(shared.__file__).resolve().parents[1])},
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "reader failed to acquire its snapshot"
            pinned = Path(ready.read_text())
            os.utime(pinned, (expired, expired))
            (source / "data").write_text("sixth")
            git("-C", source, "add", "data")
            git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "sixth")
            with shared.borrowed_snapshot("org/roadmap", ttl=0):
                assert (pinned / "data").read_text() == "fifth"
            child.kill()
            child.wait(timeout=5)
            with shared.borrowed_snapshot("org/roadmap"):
                assert not pinned.exists(), "owner death must release its version without a stale-PID policy"
                assert not (pinned.parent.parent / "locks-v3" / (pinned.name + ".lock")).exists()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    with (
        patch.dict(os.environ, TAUCETI_SHARED_REPOSITORIES_DIR=tmp + "/attach-failure"),
        patch.object(shared, "_git", side_effect=local_git),
    ):
        destination = Path(tmp) / "failure-destination"
        assert shared.attach("org/roadmap", destination, ttl=3600)
        retained = destination.resolve()
        os.utime(retained, (expired, expired))
        (source / "data").write_text("seventh")
        area = source / "areas/Algebra/Groups"
        area.mkdir(parents=True)
        (area / "Suggested.lean").write_text("-- group roadmap")
        git("-C", source, "add", ".")
        git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "seventh")
        replace = Path.replace

        def fail_destination(path, target):
            if target == destination:
                raise OSError("link replacement failed")
            return replace(path, target)

        with patch.object(Path, "replace", fail_destination):
            assert not shared.attach("org/roadmap", destination, ttl=0)
        with shared.borrowed_snapshot("org/roadmap"):
            assert destination.resolve() == retained and (retained / "data").read_text() == "sixth"
        assert shared.attach("org/roadmap", destination, ttl=3600)
        with shared.borrowed_snapshot("org/roadmap"):
            assert not retained.exists()

    from tauceti_worker import config

    with (
        patch.dict(os.environ, TAUCETI_SHARED_REPOSITORIES_DIR=tmp + "/config-readers"),
        patch.object(shared, "_git", side_effect=local_git),
        patch.object(config, "ROADMAP", "org/roadmap"),
        patch.object(config, "ROADMAP_DIR", "areas"),
    ):

        class NoAPI:
            def api_jq(self, *args):
                raise AssertionError("local source listing fell back to the network")

        assert config.roadmap_areas(NoAPI()) == ["Algebra"]
        assert config.roadmap_subareas(NoAPI(), "Algebra") == ["Groups"]
    with (
        patch.dict(os.environ, TAUCETI_SHARED_REPOSITORIES_DIR=tmp + "/optional-cleanup"),
        patch.object(shared, "_git", side_effect=local_git),
    ):
        with shared.borrowed_snapshot("org/roadmap") as current:
            garbage = current.parent / ("0" * 40)
            garbage.mkdir()
            os.utime(garbage, (expired, expired))
        remove = shared.shutil.rmtree

        def failed_cleanup(path, *args, **kwargs):
            if path == garbage:
                raise OSError("temporary old snapshot deletion failure")
            return remove(path, *args, **kwargs)

        with patch.object(shared.shutil, "rmtree", side_effect=failed_cleanup):
            with shared.borrowed_snapshot("org/roadmap") as current:
                assert (current / "data").read_text() == "seventh"
                assert garbage.is_dir()
        with shared.borrowed_snapshot("org/roadmap"):
            assert not garbage.exists(), "optional cleanup failures must be retried without failing readers"
print("PASS: shared snapshot TTL, immutability, old-round retention and self-contained Git metadata")
