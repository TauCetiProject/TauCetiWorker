"""Prepare a native PR for the installed review engine and publish its plan."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from .runtime_status import atomic_json
from .source_cli import load_source
from .source_publication import publish, retain_plan


def git(directory, *arguments, input=None):
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(directory), "-c", "core.hooksPath=/dev/null", *arguments],
        input=input,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )


def prepare(source, number, head, work):
    source.validate_binding()
    row = source.live_pull(number)
    if row is None:
        raise RuntimeError("native review metadata unavailable")
    if row["state"] != "open" or row["head"]["sha"] != head:
        return None
    base = row["base"]["sha"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (base, head)):
        raise ValueError("native review requires exact comparison commits")
    code = work / "code"
    code.mkdir()
    git(code, "init", "--quiet")
    git(code, "fetch", "--no-tags", source.repository.clone_url, base)
    git(code, "fetch", "--no-tags", source.repository.clone_url, f"refs/pull/{number}/head")
    if git(code, "rev-parse", "FETCH_HEAD").decode().strip() != head:
        return None
    merge_base = git(code, "merge-base", base, head).decode().strip()
    atomic_json(work / "comparison.json", {"base": base, "head": head, "merge_base": merge_base})
    git(code, "checkout", "--quiet", "--detach", head)
    (work / "diff.txt").write_bytes(git(code, "diff", "--no-ext-diff", "--no-textconv", merge_base, head))
    (work / "paths.z").write_bytes(git(code, "diff", "--name-only", "-z", merge_base, head))
    (work / "pr_desc.txt").write_text(row["title"] + "\n\n" + (row.get("body") or ""))
    return base, merge_base


def engine_command(source, number, head, comparison, work, store, reviewer):
    from tauceti_tools.project import active

    distribution = importlib.metadata.distribution("tauceti-review")
    engine = Path(distribution.locate_file("runner/review.py"))
    if not engine.is_file():
        raise RuntimeError("the pinned review engine is unavailable")
    project = active()
    pin = project.tool_pin("review") if project else None
    repository = pin[0] if pin else None
    revision = pin[1] if pin else os.environ.get("TAUCETI_REVIEW_REF")
    if repository is None:
        from tauceti_tools.forge import GITHUB, RepositoryRef

        repository = RepositoryRef(GITHUB, "TauCetiProject/TauCetiReview")
    if not revision:
        # Match the immutable dependency used by this Worker build.
        requirement = next(item for item in importlib.metadata.requires("tauceti") if item.startswith("tauceti-review"))
        revision = requirement.rsplit("@", 1)[-1].split(";", 1)[0].strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("native review requires an immutable rubric revision")
    rubrics = work / "review"
    rubrics.mkdir()
    git(rubrics, "init", "--quiet")
    git(rubrics, "fetch", "--no-tags", "--depth=1", repository.clone_url, revision)
    git(rubrics, "checkout", "--quiet", "--detach", revision)
    roadmap = work / "roadmap"
    roadmap.mkdir()
    roadmap_repo = project.repository_ref("roadmap") if project else None
    if roadmap_repo is not None:
        git(roadmap, "init", "--quiet")
        git(roadmap, "fetch", "--no-tags", "--depth=1", roadmap_repo.clone_url, "HEAD")
        git(roadmap, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    store.mkdir(parents=True, exist_ok=True)
    comments = source.issue_comments(number, fresh=True)
    if comments is None:
        raise RuntimeError("native review history unavailable")
    from runner.cli import latest_scoreboard, prepare_mathlib_source

    board = latest_scoreboard(comments)
    (work / "prior-board.json").write_text(json.dumps(board))
    replies = {}
    roots = source.review_comments(number)
    if roots is None:
        raise RuntimeError("native finding history unavailable")
    by_id = {row["id"]: row for row in roots}
    for row in roots:
        body = row.get("body", "") or ""
        if "tauceti-reply:" in body or "tauceti-rubric:" in body:
            continue  # Match the engine's GitHub filter: never contest our own answer.
        root = by_id.get(row.get("in_reply_to_id"))
        rubric = re.search(r"<!--tauceti-rubric:([a-z][a-z-]*)-->", root["body"]) if root else None
        if rubric:
            replies.setdefault(rubric[1], []).append(
                {
                    "id": row["id"],
                    "by": row["user"]["login"],
                    "body": row["body"],
                    "root_id": root["id"],
                    "root_body": root["body"],
                    "ts": row.get("created_at", ""),
                }
            )
    (work / "replies.json").write_text(json.dumps(replies))
    mathlib = []
    manifest = work / "code/lake-manifest.json"
    if manifest.is_file():
        prepare_mathlib_source(manifest, work / "mathlib", work / "dependencies")
        if (work / "mathlib/Mathlib").is_dir():
            mathlib = ["--mathlib-path", "mathlib"]
    actor = source.authenticated_actor()
    if actor is None:
        raise RuntimeError("review author identity unavailable")
    base, merge_base = comparison
    from .agents import _claude_review_model, _codex_review_model_override, _kiro_review_model

    models = []
    for flag, value in (
        ("--claude-model", _claude_review_model(reviewer)),
        ("--codex-model", _codex_review_model_override(reviewer)),
        ("--kiro-model", _kiro_review_model(reviewer)),
    ):
        if value:
            models.extend([flag, value])
    return [
        sys.executable,
        str(engine),
        "--repo",
        source.repository.key,
        "--pr",
        str(number),
        "--mode",
        "commit",
        "--no-post",
        "--auth",
        "api" if os.environ.get("TAUCETI_CLAUDE_BILLING") == "api" else "subscription",
        "--providers",
        reviewer,
        "--daily-budget",
        "1000000",
        "--rubrics-dir",
        str(rubrics / "rubrics"),
        "--rubrics-repo",
        repository.name,
        "--rubrics-sha",
        revision,
        "--tool-cwd",
        str(work),
        "--code-path",
        "code",
        "--roadmap-path",
        "roadmap",
        *mathlib,
        "--diff-file",
        str(work / "diff.txt"),
        "--paths-file",
        str(work / "paths.z"),
        "--pr-desc-file",
        str(work / "pr_desc.txt"),
        "--store",
        str(store),
        "--head-sha",
        head,
        "--base-sha",
        base,
        "--merge-base-sha",
        merge_base,
        "--submitted-by",
        source.repository.forge.key + "/users/" + actor.principal.native_id,
        "--ci-build",
        "",
        "--prior-scoreboard-json",
        str(work / "prior-board.json"),
        "--replies-json",
        str(work / "replies.json"),
        "--scoreboard-file",
        str(work / "scoreboard.md"),
        "--threads-dir",
        str(work / "threads"),
        "--post-plan-file",
        str(work / "plan.json"),
        "--archive-dir",
        str(store / "outbox"),
        *models,
    ]


def inference_environment(parent):
    return {
        key: value
        for key, value in parent.items()
        if not key.startswith(("GH_", "GITHUB_", "TAUCETI_SOURCE_", "TAUCETI_FORGEJO_", "GIT_CONFIG_", "CLAIM_"))
        and key not in ("TAUCETI_CONF", "TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT")
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    inference_started = False
    try:
        source = load_source()
        actor = source.authenticated_actor()
        if actor is None:
            raise RuntimeError("native review publisher unavailable")
        atomic_json(
            args.work / "review-context.json",
            {
                "source": source.identity.record(),
                "pr": args.pr,
                "head": args.head,
                "actor_id": int(actor.principal.native_id),
                "store": str(args.store / "ledger.json"),
            },
        )
        comparison = prepare(source, args.pr, args.head, args.work)
        if comparison is None:
            return 82
        command = engine_command(source, args.pr, args.head, comparison, args.work, args.store, args.reviewer)
        # Source transport and publication stay in this trusted parent. Inference
        # receives the prepared files and provider credentials used by the engine.
        environment = inference_environment(os.environ)
        inference_started = True
        status = subprocess.run(command, env=environment).returncode
        if status:
            return status
        result = json.loads((args.work / "plan.json").read_text())
        if result.get("review_changed") is False:
            context = json.loads((args.work / "review-context.json").read_text())
            context["complete"] = "unchanged"
            atomic_json(args.work / "review-context.json", context)
            from .source_publication import cleanup_inputs

            cleanup_inputs(args.work)
            return 84
        cfg = SimpleNamespace(store=args.store / "ledger.json")
        publication = retain_plan(
            source, cfg, args.pr, args.head, args.work / "plan.json", actor_id=int(actor.principal.native_id)
        )
        if not publish(source, publication):
            return 82
        plan = json.loads((args.work / "plan.json").read_text())
        return 1 if plan.get("review_error") else 0
    except Exception as error:
        print(
            f"native review unavailable ({type(error).__name__}); retained work requires reconciliation",
            file=sys.stderr,
        )
        return (85 if (args.work / "plan.json").is_file() else 1) if inference_started else 83


if __name__ == "__main__":
    raise SystemExit(main())
