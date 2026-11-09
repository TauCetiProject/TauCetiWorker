"""Replay an adopted repair task locally, using its recorded resolutions as an oracle.

This tests the driver, not an agent's ability to resolve conflicts. No queue API, claims,
pushes, inference, or Lean builds are involved. The source object store is read-only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from . import chain
from .rebase_driver import Driver, DriverError, _atomic


def replay(source, checkpoint, reference, destination, *, keep_repo=False):
    destination = Path(destination).resolve()
    if destination.exists():
        raise DriverError("replay destination must not exist")
    destination.mkdir(mode=0o700, parents=True)
    task = json.loads(Path(checkpoint).read_text())["task"]
    # Never copy the checkpoint: it also contains production credentials/claim tokens.
    base, tip, target = (task[key] for key in ("base", "tip", "onto"))
    repo = destination / "repo"
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--shared",
            "--no-checkout",
            str(Path(source).resolve()),
            str(repo),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    # --no-checkout has an empty index; materialize a clean target before initialization.
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "checkout", "--detach", target],
        check=True,
        capture_output=True,
    )
    originals = chain.chain_commits(repo, base, tip)
    reference = chain.resolve_commit(repo, reference)
    gold = chain.chain_commits(repo, target, reference)
    if [(c.source_pr, c.accepted_head) for c in originals] != [(c.source_pr, c.accepted_head) for c in gold]:
        raise DriverError("reference membership/order/accepted heads differ from the input")
    if len(originals) != len(task["members"]):
        raise DriverError("task membership count differs from input")
    for original, member in zip(originals, task["members"], strict=True):
        if int(original.source_pr.rsplit("#", 1)[1]) != member["number"]:
            raise DriverError("task member order differs from input")
        if member.get("accepted_head", original.accepted_head) != original.accepted_head:
            raise DriverError("task accepted head differs from input")
        if member.get("commit", original.sha) != original.sha:
            raise DriverError("task member commit differs from input")
    driver = Driver.create(
        repo,
        destination / "driver",
        base,
        tip,
        target,
        review_clean=True,
        protected_prs=[m["number"] for m in task["members"] if m.get("no_repair")],
    )
    started = time.monotonic()
    counts = {"git_conflicts": 0, "clean_matches": 0, "recorded_corrections": 0, "restarts": 0}
    corrections = []
    for position, (original, accepted) in enumerate(zip(originals, gold, strict=True)):
        status = driver.advance()
        pending = driver.state["pending"]
        if pending is None or pending["source"] != original.sha:
            raise DriverError("driver skipped or reordered a member")
        if chain.tree_of(repo, driver.state["tip"]) != chain.tree_of(repo, accepted.parents[0]):
            raise DriverError(f"prefix differs before {original.source_pr}")
        changed = list(
            filter(
                None,
                driver.git("diff", "--name-only", "--no-renames", "-z", pending["tree"], accepted.tree).split("\0"),
            )
        )
        if pending["conflict"]:
            counts["git_conflicts"] += 1
        elif changed:
            counts["recorded_corrections"] += 1
            corrections.append({"source_pr": original.source_pr, "paths": changed})
        else:
            counts["clean_matches"] += 1
        # Copy only this member's reference differences; never move HEAD or the driver's ref.
        # This includes build fixes folded into earlier commits by the original worker.
        for path in changed:
            file = repo / path
            entry = driver.git("ls-tree", accepted.tree, "--", path).strip()
            if not entry:
                file.unlink(missing_ok=True)
                continue
            metadata, _ = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise DriverError(f"unsupported historical resolution mode: {path}")
            if file.is_symlink() or any(p.is_symlink() for p in file.parents if p.is_relative_to(repo)):
                raise DriverError(f"symlinked historical path: {path}")
            file.parent.mkdir(parents=True, exist_ok=True)
            blob = driver.git("cat-file", "blob", oid, check=False)
            if blob.returncode:
                raise DriverError(f"missing reference blob: {path}")
            file.write_bytes(blob.stdout)
            file.chmod(0o755 if mode == "100755" else 0o644)
        explanation = chain.appended_text(original.message, accepted.message)
        if not chain.message_preserved(original.message, accepted.message):
            raise DriverError(f"reference changed original message: {original.source_pr}")
        if (changed or pending["conflict"]) and not explanation:
            explanation = "Historical replay oracle: apply the adopted resolution for this PR."
        allowed = set(pending["paths"] + pending["original_paths"])
        driver.resolve(status["packet_id"], explanation, allow_paths=set(changed) - allowed)
        if driver.state["records"][-1]["tree"] != accepted.tree:
            raise DriverError(f"tree differs at {original.source_pr}")
        if (position + 1) % 25 == 0:
            driver = Driver(destination / "driver")
            counts["restarts"] += 1
    driver.advance()
    validation = driver.check()
    if not validation["ok"]:
        raise DriverError(f"driver validation failed: {validation['errors']}")
    report = {
        "schema": "tauceti.rebase-replay/v1",
        "oracle": "recorded adopted per-member trees",
        "base": base,
        "input_tip": tip,
        "target": target,
        "reference": reference,
        "members": len(originals),
        **counts,
        "corrections": corrections,
        "every_member_tree_matches": True,
        "final_tree_matches": chain.tree_of(repo, reference) == chain.tree_of(repo, driver.state["tip"]),
        "seconds": round(time.monotonic() - started, 3),
        "validation": validation,
        "agent_inference": "not run; timing does not measure fresh agent performance",
        "lean_validation": "not run; reference adoption is not proof of a passing build",
        "scratch_repo_retained": keep_repo,
    }
    _atomic(destination / "report.json", report)
    if not keep_repo:
        shutil.rmtree(repo)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--keep-repo", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = replay(args.source, args.checkpoint, args.reference, args.destination, keep_repo=args.keep_repo)
    except (DriverError, chain.ChainError, OSError, subprocess.CalledProcessError) as error:
        print(json.dumps({"error": str(error), "scratch": str(args.destination)}))
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
