#!/usr/bin/env python3
"""Cross-worker cache isolation, mutation pacing, throttle retention and stable state roots."""

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_outbox as outbox
from tauceti_worker import github_transport as tr


def reserve(queue):
    queue.put(tr.mutation_slot(os.environ))


with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"GH_TOKEN": "test", "TAUCETI_GITHUB_SHARED_DIR": tmp}),
):
    key = ["permissions", "org/repo"]
    assert tr.shared_read(key, lambda: True, ttl=300)
    tr.invalidate(["api", "repos/org/repo/issues/7/comments", "-f", "body=x"])
    assert tr.shared_read(key, lambda: False, ttl=300)
    tr.invalidate_permissions("org/repo")
    assert tr.shared_read(key, lambda: False, ttl=300) is False
    for k in (
        ["issue_comments", "org/repo", 7],
        ["issue_comments", "org/repo", 8],
        ["open_prs", "org/repo"],
        ["open_prs", "else/repo"],
    ):
        assert tr.shared_read(k, lambda: "old") == "old"
    tr.invalidate(["api", "repos/org/repo/issues/7/comments", "-f", "body=x"])
    assert tr.shared_read(["issue_comments", "org/repo", 7], lambda: "new") == "new"
    for k in (["issue_comments", "org/repo", 8], ["open_prs", "org/repo"], ["open_prs", "else/repo"]):
        assert tr.shared_read(k, lambda: "wrong") == "old"
    assert tr.shared_read(["open_prs", "org/repo", 100], lambda: "old page") == "old page"
    tr.invalidate(["pr", "close", "7", "--repo", "org/repo"])
    assert tr.shared_read(["open_prs", "org/repo"], lambda: "closed") == "closed"

    assert tr.shared_read(["open_prs", "org/repo", 100], lambda: "new page") == "new page"

    tr.invalidate(["api", "-X", "PATCH", "repos/org/repo/issues/comments/123", "-f", "body=scoreboard"])
    assert tr.shared_read(["open_prs", "org/repo", 100], lambda: "wrong") == "new page"
    assert tr.shared_read(["issue_comments", "org/repo", 8], lambda: "updated comments") == "updated comments"

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=reserve, args=(q,)) for _ in range(5)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(5)
        assert p.exitcode == 0
    assert sum(q.get(timeout=1) == 0 for _ in procs) == 1
    # Success does not erase a reserved pacing interval.
    tr.secondary_result(os.environ)
    assert tr.mutation_slot(os.environ) > 0
    base = time.time() + 10
    with patch.object(tr.time, "time", return_value=base):
        assert tr.secondary_result(os.environ, rejected=True) == 60
        for _ in range(5):
            assert tr.secondary_result(os.environ, rejected=True) == 60
    with patch.object(tr.time, "time", return_value=base + 61):
        assert tr.secondary_result(os.environ, rejected=True) == 120
    with patch.object(tr.time, "time", return_value=base + 182):
        assert tr.mutation_slot(os.environ) == 0
        assert tr.mutation_slot(os.environ) > 0  # exactly one recovery probe
        tr.secondary_result(os.environ)

for error in (
    {"resource": "PullRequestReview", "code": "abuse", "field": "base"},
    {"resource": "PullRequestReviewComment", "message": "was submitted too quickly"},
):
    body = json.dumps({"message": "Validation Failed", "errors": [error], "status": "422"})
    assert tr.content_throttle(body)
    assert tr.content_throttle("HTTP/2.0 422\nheader: value\n\n" + body)
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.dict(os.environ, {"GH_TOKEN": "test", "TAUCETI_GITHUB_SHARED_DIR": tmp}),
    ):
        trace = "* Request at now\n* Request to https://api.github.com/repos/org/repo/pulls/7/comments\n> POST /repos/org/repo/pulls/7/comments HTTP/1.1\n< HTTP/2.0 422 Unprocessable Entity\n* Request took 1ms\n"
        with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, body, trace)):
            result = tr.execute(["gh", "api", "repos/org/repo/pulls/7/comments", "-f", "body=hello"])
        assert result.github_retry_safe and result.github_content_throttle
        assert tr.cooldown() == 0  # Comment-specific spam throttling does not block reads.
        assert tr.mutation_slot(os.environ) > 0
        with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as read:
            assert tr.execute(["gh", "api", "repos/org/repo"]).returncode == 0
            read.assert_called_once()
        paths = list(outbox.intent_paths())
        assert len(paths) == 1 and json.loads(paths[0].read_text())["status"] == "rejected"
        assert not list(Path(tmp).rglob("*.failed"))
assert not tr.content_throttle(
    json.dumps({"errors": [{"resource": "PullRequestReviewComment", "code": "invalid", "field": "line"}]})
)
with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/user-state"}):
    os.environ.pop("TAUCETI_GITHUB_SHARED_DIR", None)
    assert tr.root() == Path("/tmp/user-state/tauceti/github-shared")
assert not tr.content_write(["api", "-X", "DELETE", "repos/org/repo/issues/comments/7"])
assert not tr.content_write(
    ["api", "graphql", "-f", 'query=mutation{resolveReviewThread(input:{threadId:"id"}){thread{id}}}']
)
assert tr.proxy_readonly(["api", "-X", "GET", "repos/org/repo/issues", "-f", "state=open"])
with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"GH_TOKEN": "test", "TAUCETI_GITHUB_SHARED_DIR": tmp}),
):
    old = outbox.prepare(["gh", "api", "-X", "PATCH", "repos/org/repo/issues/comments/7", "-f", "body=old"], os.environ)
    state = json.loads(old.read_text())
    state["status"] = "rejected"
    tr.atomic(old, state)
    with patch.dict(os.environ, {"TAUCETI_WORKER_ID": "another-worker"}):
        new = outbox.prepare(
            ["gh", "api", "-X", "PATCH", "repos/org/repo/issues/comments/7", "-f", "body=new"], os.environ
        )
    assert new.exists() and not old.exists()
    assert not list(Path(tmp).rglob("*.failed"))  # Superseded work is deleted, not archived.
from tauceti_worker import activity

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_ACTIVITY_DIR": tmp}):
    db = activity._connect()
    event = dict(
        version=1,
        kind="review_coordination",
        id="nonce",
        at=time.time(),
        worker="one",
        marker_posted=False,
        git_held=True,
    )
    activity._ingest(db, event)
    activity._ingest(db, event)
    buckets = json.loads(db.execute("SELECT value FROM meta WHERE key='local_coordination'").fetchone()[0])
    assert sum(b["started"] for b in buckets.values()) == 1
    assert sum(b["marker_failed"] for b in buckets.values()) == 1
    db.close()
print("PASS: targeted cache invalidation, shared pacing, recovery probe, both throttles and stable state")

# Real journal import must retain coordination receipts, including those skipped by an older collector.
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_ACTIVITY_DIR": tmp}):
    journal = Path(tmp) / "worker-events.jsonl"
    with patch.dict(os.environ, {"TAUCETI_FLEET_EVENTS": str(journal)}):
        event = dict(
            version=1,
            kind="review_coordination",
            id="journal-review",
            at=time.time(),
            worker="one",
            marker_posted=False,
            git_held=True,
        )
        journal.write_text(json.dumps(event) + "\n")
        db = activity._connect()
        # An older collector advanced past this kind without ingesting it.
        db.execute(
            "INSERT OR REPLACE INTO cursors VALUES (?,?,?)",
            (str(journal), journal.stat().st_ino, journal.stat().st_size),
        )
        db.execute("DELETE FROM meta WHERE key='coordination_journal_schema'")
        db.commit()
        db.close()
        db = activity._connect()
        activity.backfill(db)
        activity.backfill(db)
        buckets = json.loads(db.execute("SELECT value FROM meta WHERE key='local_coordination'").fetchone()[0])
        assert sum(b["started"] for b in buckets.values()) == 1
        assert sum(b["marker_failed"] for b in buckets.values()) == 1
        assert sum(b["git_held"] for b in buckets.values()) == 1
        db.close()
print("PASS: coordination journal import and one-time cursor replay are idempotent")
