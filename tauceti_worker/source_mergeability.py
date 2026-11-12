"""Resolve ambiguous native API mergeability in a private Git object store."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

from tauceti_tools.forge import ForgeError

from tauceti_queue.forgejo_git import ForgejoGitSource


def observe(source, state, number, expected_head):
    source.validate_binding()
    row = source.live_pull(number)
    if row is None or row.get("state") != "open" or row.get("draft"):
        return "UNKNOWN"
    head, base = row["head"]["sha"], row["base"]["sha"]
    if head != expected_head:
        return "UNKNOWN"
    if row.get("mergeable") is True:
        return "MERGEABLE"
    if row["base"]["ref"] != os.environ.get("TAUCETI_BASE_BRANCH", "main"):
        return "UNKNOWN"
    key = (head, base)
    cache = source._mergeability_cache
    if key in cache:
        return cache[key]
    root = Path(state) / "mergeability"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        clone = root / "objects.git"
        if not (clone / "HEAD").is_file():
            env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME"}}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="0")
            subprocess.run(
                ["git", "init", "--bare", "--template=", str(clone)],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )
        git = ForgejoGitSource(source.identity, source.api, clone, token=source.api._token)
        # Retained local tips provide negotiation haves on the next fetch. The
        # base repository's pull ref also covers heads authored in forks.
        head_ref, base_ref = "refs/mergeability/head", "refs/mergeability/base"
        git.fetch_refs(
            source.repo,
            [
                f"+refs/pull/{number}/head:{head_ref}",
                f"+refs/heads/{row['base']['ref']}:{base_ref}",
            ],
        )
        if (git.local_ref_sha(head_ref), git.local_ref_sha(base_ref)) != key:
            return "UNKNOWN"
        result = git.mergeability(source.repo, head, base)
    current = source.live_pull(number)
    if current is None or (current["head"]["sha"], current["base"]["sha"]) != key:
        return "UNKNOWN"
    if len(cache) >= 128:
        cache.clear()
    cache[key] = result
    return result


def resolve(source, state, number, expected_head):
    try:
        return observe(source, state, number, expected_head)
    except (ForgeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Failed Git/API observation must not become rebase authority or spend a
        # model/repair allowance. The next ordinary survey can retry it.
        from .config import log

        log(f"Forgejo #{number}: mergeability observation unavailable ({type(error).__name__})")
        return "UNKNOWN"
