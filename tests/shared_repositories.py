#!/usr/bin/env python3
"""Shared read-only snapshots retain old rounds and Git provenance inside sandbox mounts."""

import os
import subprocess
import sys
import tempfile
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
        assert (old / ".git").is_dir() and git("-C", old, "rev-parse", "HEAD").stdout.strip() == old.name
        assert (old / "data").stat().st_mode & 0o222 == 0
        before = len(calls)
        assert shared.snapshot("org/roadmap") == old and len(calls) == before
        (source / "data").write_text("new")
        git("-C", source, "add", "data")
        git("-C", source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "new")
        new = shared.snapshot("org/roadmap", ttl=0)
        assert new != old and (new / "data").read_text() == "new" and (old / "data").read_text() == "old"
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
print("PASS: shared snapshot TTL, immutability, old-round retention and self-contained Git metadata")
