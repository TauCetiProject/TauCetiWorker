#!/usr/bin/env python3
"""--source validation and loop propagation, without GitHub or model access."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def rejects(name, args, only, needle):
    try:
        tc.resolve_source(args, only)
    except SystemExit as e:
        check(name, needle in str(e))
    else:
        check(name, False)


tmp = Path(tempfile.mkdtemp(prefix="tauceti-source-"))
source = tmp / "material"
source.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
subprocess.run(
    [
        "git",
        "-C",
        str(source),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "source",
    ],
    check=True,
)
args = types.SimpleNamespace(source=str(source))
old_area = os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
try:
    check(
        "omitted source needs no roadmap restrictions",
        tc.resolve_source(types.SimpleNamespace(source=None), []) is None,
    )
    rejects("non-roadmap goal is rejected", args, ["review"], "roadmap' phase")

    rejects("unpinned roadmap is rejected", args, ["roadmap"], "single roadmap area")
    rejects("unpinned full cascade is rejected", args, [], "single roadmap area")
    os.environ["TAUCETI_ROADMAP_ONLY"] = ""
    rejects("all-roadmaps selection is rejected", args, ["roadmap"], "single roadmap area")
    os.environ["TAUCETI_ROADMAP_ONLY"] = "any"
    rejects("explicit any selection is rejected", args, ["roadmap"], "single roadmap area")

    os.environ["TAUCETI_ROADMAP_ONLY"] = "Topology"
    resolved = tc.resolve_source(args, ["roadmap"])
    check(
        "pinned roadmap resolves source absolutely", resolved == str(source.resolve()) and Path(resolved).is_absolute()
    )
    check("pinned full cascade accepts source", tc.resolve_source(args, []) == str(source.resolve()))
    check(
        "pinned mixed phases accept source",
        tc.resolve_source(args, ["review", "roadmap"]) == str(source.resolve()),
    )
    check(
        "effective cascade with another phase skipped accepts source",
        tc.resolve_source(args, tc.resolve_tasks([], ["review"])) == str(source.resolve()),
    )
    rejects(
        "effective cascade with roadmap skipped rejects source",
        args,
        tc.resolve_tasks([], ["roadmap"]),
        "roadmap' phase",
    )
    check(
        "Git repository URL is accepted",
        tc.resolve_source(types.SimpleNamespace(source="https://github.com/example/library.git"), ["roadmap"])
        == "https://github.com/example/library.git",
    )
    check("file Git URL is accepted", tc.is_git_url(f"file://{source}"))
    check("scp-style Git URL is accepted", tc.is_git_url("git@example.com:owner/repo.git"))
    check("malformed URL is rejected without raising", not tc.is_git_url("http://[oops"))
    local_file = tmp / "one-file.lean"
    local_file.write_text("example")
    rejects(
        "local file is rejected (source paths are directories)",
        types.SimpleNamespace(source=str(local_file)),
        ["roadmap"],
        "must name a directory",
    )
    rejects(
        "missing source path is rejected",
        types.SimpleNamespace(source=str(tmp / "missing")),
        ["roadmap"],
        "neither a Git repository URL nor an existing Git repository directory",
    )
    not_git = tmp / "not-git"
    not_git.mkdir()
    rejects(
        "non-Git local directory is rejected",
        types.SimpleNamespace(source=str(not_git)),
        ["roadmap"],
        "is not a Git repository",
    )

    parsed = tc.build_parser().parse_args(
        ["work", "--only", "roadmap", "--roadmap-only", "Topology", "--source", str(source)]
    )
    check("work parser accepts --source", parsed.source == str(source))
    parsed_child = tc.build_parser().parse_args(["_round", "--source", str(source)])
    check("loop-child parser accepts --source", parsed_child.source == str(source))

    origin = tmp / "git-origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    (origin / "version").write_text("one")
    subprocess.run(["git", "-C", str(origin), "add", "version"], check=True)
    subprocess.run(
        ["git", "-C", str(origin), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "one"],
        check=True,
    )
    clone = tmp / "git-clone"
    origin_url = f"file://{origin}"
    check("Git URL materializer shallow-clones source", tc.fetch_git_source(origin_url, clone))
    check("cloned source has expected content", (clone / "version").read_text() == "one")
    pushurl = subprocess.run(
        ["git", "-C", str(clone), "remote", "get-url", "--push", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    check("materialized source disables origin pushes", pushurl == "no_push")
    (origin / "version").write_text("two")
    subprocess.run(["git", "-C", str(origin), "add", "version"], check=True)
    subprocess.run(
        ["git", "-C", str(origin), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "two"],
        check=True,
    )
    check("Git URL materializer refreshes cached source", tc.fetch_git_source(origin_url, clone))
    check("refreshed source has latest content", (clone / "version").read_text() == "two")

    captured = {}
    old_budget = tc.loop.github_budget
    old_run = tc.loop.run_round_subprocess
    tc.loop.github_budget = lambda: {"core": (1000, 0), "graphql": (1000, 0)}

    def capture_round(tail):
        captured["tail"] = tail
        raise KeyboardInterrupt

    tc.loop.run_round_subprocess = capture_round
    loop_args = types.SimpleNamespace(ignore_quota=False, bubble=True, quota_cmd=None, source=str(source.resolve()))
    rc = tc.loop.cmd_loop(loop_args, types.SimpleNamespace(wid="worker1"), only=["roadmap"], agent="deepseek")
    tc.loop.github_budget = old_budget
    tc.loop.run_round_subprocess = old_run
    tail = captured.get("tail", [])
    check("loop exits cleanly after test interrupt", rc == 130)
    check("loop forwards source to child", tail[-2:] == ["--source", str(source.resolve())])
finally:
    if old_area is None:
        os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
    else:
        os.environ["TAUCETI_ROADMAP_ONLY"] = old_area

raise SystemExit(bool(fails))
