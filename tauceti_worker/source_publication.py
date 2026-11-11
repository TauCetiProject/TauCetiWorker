"""Reconcile a retained review plan against the exact native source head."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from tauceti_tools.forge import AmbiguousWrite, ForgeError, Incomplete

from .runtime_status import atomic_json


def finding_marker(source, number, head, rubric, run_id):
    identity = [source.identity.record(), number, head, rubric, run_id]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return f"<!--tauceti-source-finding:{digest}-->"


def reply_to_finding(source, number, parent, body):
    comments = source.review_comments(number)
    if comments is None:
        raise Incomplete("native finding discovery unavailable")
    root = next((row for row in comments if row["id"] == parent), None)
    if root is None or "<!--tauceti-rubric:" not in root["body"]:
        raise Incomplete("the selected native finding root is unavailable")
    marker = f"<!--tauceti-source-reply:root:{parent}-->"
    if root.get("thread_kind") == "issue-comment":
        return source.api.request(
            "POST",
            f"repos/{source.repo}/issues/{number}/comments",
            {"body": marker + "\n" + body},
        )
    return source.api.request(
        "POST",
        f"repos/{source.repo}/pulls/{number}/reviews/{root['pull_request_review_id']}/comments",
        {
            "body": marker + "\n" + body,
            "path": root["path"],
            "new_position": root.get("position", 0),
            "old_position": root.get("original_position", 0),
        },
    )


def retain_plan(source, cfg, number, head, plan_path, *, actor_id=None):
    plan = json.loads(Path(plan_path).read_text())
    if plan.get("head_sha") != head:
        raise ValueError("review plan has another head")
    if actor_id is None:
        actor = source.authenticated_actor()
        if actor is None:
            raise Incomplete("native review publisher identity unavailable")
        actor_id = int(actor.principal.native_id)
    if type(actor_id) is not int or actor_id < 1:
        raise ValueError("native review requires its original publisher ID")
    comparison_path = Path(plan_path).with_name("comparison.json")
    comparison = json.loads(comparison_path.read_text()) if comparison_path.exists() else None
    threads = []
    for item in plan.get("threads", []):
        if not item.get("body"):
            continue
        position = {"new_position": 1, "old_position": 0}
        checkout = Path(plan_path).parent / "code"
        if item.get("path") and (checkout / ".git").exists() and comparison:
            patch = subprocess.check_output(
                [
                    "git",
                    "--literal-pathspecs",
                    "-C",
                    str(checkout),
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--unified=0",
                    comparison["merge_base"],
                    head,
                    "--",
                    item["path"],
                ],
                text=True,
                timeout=30,
            )
            hunk = re.search(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", patch, re.M)
            if hunk:
                if hunk[4] == "0":
                    position = {"new_position": 0, "old_position": int(hunk[1])}
                else:
                    position = {"new_position": int(hunk[3]), "old_position": 0}
            else:
                position["conversation"] = True
        threads.append({**item, "body": Path(item["body"]).read_text(), **position})
    artifact = {
        "source": source.identity.record(),
        "pr": number,
        "head": head,
        "actor_id": actor_id,
        "state": "prepared",
        "scoreboard": Path(plan["scoreboard_body"]).read_text(),
        "threads": threads,
        "ledger": str(cfg.store),
        "round": plan.get("round"),
        "in_progress": bool(plan.get("in_progress")),
    }
    # The original engine artifacts and outbox remain in their existing source
    # workspace/store. This sidecar holds only its native publication intent.
    destination = Path(plan_path).with_name("publication.json")
    atomic_json(destination, artifact)
    cleanup_inputs(destination.parent)
    return destination


def publish(source, publication_path):
    publication_path = Path(publication_path)
    value = json.loads(publication_path.read_text())
    if value["source"] != source.identity.record():
        raise ValueError("retained publication belongs to another source")
    if value["state"] == "published":
        return True
    if value["state"] == "superseded":
        return False
    source.validate_binding()
    actor = source.authenticated_actor()
    if actor is None or int(actor.principal.native_id) != value["actor_id"]:
        raise Incomplete("retained publication requires its original native author")
    number, head = value["pr"], value["head"]
    row = source.live_pull(number)
    if row is None:
        raise Incomplete("native publication head unavailable")
    if row["state"] != "open" or row["head"]["sha"] != head:
        value["state"] = "superseded"
        atomic_json(publication_path, value)
        return False

    roots = source.review_comments(number)
    if roots is None:
        raise Incomplete("native review roots unavailable")
    confirmed = {}
    for action in value["threads"]:
        if not action.get("required", action.get("action") == "upsert"):
            continue
        rubric = action["rubric"]
        marker = finding_marker(source, number, head, rubric, action.get("run_id"))
        existing = [
            root
            for root in roots
            if root["user"]["id"] == value["actor_id"]
            and marker in root.get("body", "")
            and root.get("commit_id") == head
        ]
        if not existing:
            previous = next(
                (
                    root
                    for root in roots
                    if root["id"] == action.get("comment_id")
                    and root["user"]["id"] == value["actor_id"]
                    and root.get("commit_id") == head
                    and bool(root.get("thread_kind") == "issue-comment") == bool(action.get("conversation"))
                ),
                None,
            )
            if previous:
                body = marker + "\n" + action["body"]
                if action.get("conversation"):
                    body = f"<!--tauceti-source-head:{head}-->\n" + body
                source.api.request("PATCH", f"repos/{source.repo}/issues/comments/{previous['id']}", {"body": body})
            elif action.get("conversation"):
                source.api.request(
                    "POST",
                    f"repos/{source.repo}/issues/{number}/comments",
                    {"body": marker + f"\n<!--tauceti-source-head:{head}-->\n" + action["body"]},
                )
            else:
                source.api.request(
                    "POST",
                    f"repos/{source.repo}/pulls/{number}/reviews",
                    {
                        "event": "COMMENT",
                        "commit_id": head,
                        "body": marker,
                        "comments": [
                            {
                                "body": marker + "\n" + action["body"],
                                "path": action["path"],
                                "new_position": action["new_position"],
                                "old_position": action["old_position"],
                            }
                        ],
                    },
                )
            source.invalidate(number)
            roots = source.review_comments(number)
            if roots is None:
                raise AmbiguousWrite("native finding publication requires read-back")
            existing = [
                root
                for root in roots
                if root["user"]["id"] == value["actor_id"]
                and marker in root.get("body", "")
                and root.get("commit_id") == head
            ]
        if not existing:
            raise AmbiguousWrite("native finding publication has no confirmed root")
        confirmed[rubric] = max(existing, key=lambda root: root["id"])["id"]

    # Recheck after all required roots and immediately before the commit marker.
    row = source.live_pull(number)
    if row is None or row["state"] != "open" or row["head"]["sha"] != head:
        raise Incomplete("source head changed before publishing the review scoreboard")
    comments = source.issue_comments(number, fresh=True)
    if comments is None:
        raise Incomplete("native scoreboard discovery unavailable")
    body = value["scoreboard"]
    body = body.replace("https://github.com/" + source.repository.key, source.repository.url)
    marker = (
        "<!--tauceti-source-publication:"
        + hashlib.sha256(
            json.dumps([source.identity.record(), number, head, body], sort_keys=True).encode()
        ).hexdigest()
        + "-->"
    )
    body += "\n" + marker
    ours = [
        comment
        for comment in comments
        if comment.get("user", {}).get("id") == value["actor_id"]
        and "<!--tauceti-scoreboard-->" in comment.get("body", "")
    ]
    exact = next((comment for comment in ours if marker in comment.get("body", "")), None)
    if exact is None:
        if ours:
            newest = max(ours, key=lambda comment: comment["id"])
            source.api.request("PATCH", f"repos/{source.repo}/issues/comments/{newest['id']}", {"body": body})
        else:
            source.api.request("POST", f"repos/{source.repo}/issues/{number}/comments", {"body": body})
        source.invalidate(number)
        comments = source.issue_comments(number, fresh=True)
        if comments is None:
            raise AmbiguousWrite("native scoreboard publication requires read-back")
        exact = next(
            (
                comment
                for comment in comments
                if comment.get("user", {}).get("id") == value["actor_id"] and marker in comment.get("body", "")
            ),
            None,
        )
    if exact is None:
        raise AmbiguousWrite("native scoreboard has no confirmed publication marker")
    ledger_path = Path(value["ledger"])
    ledger = json.loads(ledger_path.read_text())
    state = ledger["prs"][str(number)]
    state["scoreboard_comment_id"] = exact["id"]
    if value.get("in_progress"):
        state.pop("published_head_sha", None)
    else:
        state["published_head_sha"] = head
    if state.get("pending_publication_head_sha") == head:
        state.pop("pending_publication_head_sha", None)
    runs = {
        action["rubric"]: action.get("run_id")
        for action in value["threads"]
        if action.get("required", action.get("action") == "upsert")
    }
    for rubric, identifier in confirmed.items():
        case = state.setdefault("state", {}).setdefault(rubric, {})
        case["thread"] = {"comment_id": identifier, "head_sha": head}
        if not runs[rubric] or case.get("pending_thread_run_id") == runs[rubric]:
            case.pop("pending_thread_run_id", None)
    atomic_json(ledger_path, ledger)
    # The engine defines close notes and direct contest answers as best-effort
    # UI actions. They never substitute for required findings or the scoreboard.
    for action in value["threads"]:
        if action.get("action") not in {"reply", "close"}:
            continue
        try:
            source.invalidate(number)
            roots = source.review_comments(number, fresh=True)
            if roots is None:
                raise Incomplete("native optional thread history unavailable")
            marker = (
                "<!--tauceti-source-optional:"
                + hashlib.sha256(
                    json.dumps([source.identity.record(), number, head, action], sort_keys=True).encode()
                ).hexdigest()
                + "-->"
            )
            if any(marker in row.get("body", "") for row in roots):
                continue
            if action["action"] == "reply":
                reply_to_finding(source, number, action["in_reply_to"], marker + "\n" + action["body"])
            else:
                parent = next(
                    (
                        root
                        for root in roots
                        if root["id"] == action.get("comment_id") and root["user"]["id"] == value["actor_id"]
                    ),
                    None,
                )
                if parent:
                    body = marker + "\n" + action["body"]
                    if parent.get("thread_kind") == "issue-comment":
                        body = f"<!--tauceti-source-head:{parent['commit_id']}-->\n" + body
                    source.api.request("PATCH", f"repos/{source.repo}/issues/comments/{parent['id']}", {"body": body})
        except Exception as error:
            from .config import log

            log(f"native review {action['action']} note unavailable ({type(error).__name__})")
    value.update(state="published", scoreboard_comment_id=exact["id"], findings=confirmed)
    atomic_json(publication_path, value)
    source.invalidate(number)
    return True


def cleanup_inputs(work):
    """Release generated source copies after the rendered result is retained."""
    for name in ("code", "mathlib", "dependencies", "roadmap", "review"):
        try:
            shutil.rmtree(work / name)
        except OSError:
            pass


def pending(source, cfg, *, numbers=()):
    """Recover a complete engine plan, including a crash before native posting."""
    from types import SimpleNamespace

    workspaces = sorted((cfg.state / "review-work").iterdir()) if (cfg.state / "review-work").exists() else []
    for work in workspaces:
        if work.is_symlink() or not work.is_dir():
            continue
        path = work / "publication.json"
        if not path.exists() and not (work / "plan.json").exists() and (work / "review-context.json").is_file():
            context = json.loads((work / "review-context.json").read_text())
            if context["source"] != source.identity.record() or context["store"] != str(cfg.store):
                raise ValueError("retained review workspace belongs to another source or store")
            from .work_recovery import live_users, owner_stopped

            if owner_stopped(context.get("process")) and not live_users(work):
                shutil.rmtree(work)
            continue
        if not path.exists() and (work / "plan.json").is_file() and (work / "review-context.json").is_file():
            context = json.loads((work / "review-context.json").read_text())
            if numbers and context["pr"] not in numbers:
                continue
            plan = json.loads((work / "plan.json").read_text())
            if context["source"] != source.identity.record() or context["store"] != str(cfg.store):
                raise ValueError("retained review plan belongs to another source or store")
            if plan.get("review_changed") is False:
                context["complete"] = "unchanged"
                atomic_json(work / "review-context.json", context)
                cleanup_inputs(work)
                continue
            path = retain_plan(
                source,
                SimpleNamespace(store=cfg.store),
                context["pr"],
                context["head"],
                work / "plan.json",
                actor_id=context["actor_id"],
            )
        if path.is_file():
            value = json.loads(path.read_text())
            if numbers and value["pr"] not in numbers:
                continue
            if value.get("state") == "prepared":
                cleanup_inputs(work)
                try:
                    if publish(source, path):
                        return True
                except ForgeError as error:
                    from .config import log

                    log(
                        f"native retained review #{value['pr']} awaits publication ({type(error).__name__}); checking other retained work"
                    )
    return False
