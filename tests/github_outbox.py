#!/usr/bin/env python3
"""Publication survives REST exhaustion, process restart, and ambiguous acknowledgements."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_outbox as outbox
from tauceti_worker import github_transport as tr

with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "TAUCETI_GITHUB_SHARED_DIR": tmp,
            "GH_TOKEN": "fake",
            "TAUCETI_WORKER_ID": "science",
            "TAUCETI_GITHUB_ACCOUNT": "author",
        },
    ),
):
    env = dict(os.environ)
    # A failed local checkout/clone is not an ambiguous remote publication.
    for local in (["gh", "pr", "checkout", "15467", "--force"], ["gh", "repo", "clone", "org/repo"]):
        assert outbox.prepare(local, env, tmp) is None
    legacy_checkout = outbox.directory(env) / "legacy-checkout.json"
    tr.atomic(legacy_checkout, {"argv": ["pr", "checkout", "15467", "--force"], "cwd": tmp, "status": "ambiguous"})
    with patch.object(tr, "execute") as replay:
        assert outbox.drain(env) == []
        assert not legacy_checkout.exists() and legacy_checkout.with_suffix(".failed").exists()
        replay.assert_not_called()
    argv = ["gh", "api", "-X", "POST", "/repos/org/repo/issues/7/comments", "-f", "body=paid-result"]
    path = outbox.prepare(argv, env, tmp)
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600
    failure = subprocess.CompletedProcess(argv, 1, "", "API rate limit exceeded")
    outbox.finish(path, failure, before_request=True)
    assert json.loads(path.read_text())["status"] == "rejected"
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess(argv, 0, "{}", "")) as publish:
        assert outbox.drain(env) == [] and not path.exists() and publish.call_count == 1
    # Server accepted a comment, but its acknowledgement was lost; reconcile before retrying.
    path = outbox.prepare(argv, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(argv, 1, "", "connection reset"), [])
    published = [[{"body": "paid-result", "user": {"login": "author"}}]]
    with patch.object(
        tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(published), "")
    ) as read:
        assert outbox.drain(env) == [] and not path.exists() and read.call_count == 1
        assert read.call_args.args[0][1] == "api" and "-X" not in read.call_args.args[0]
    # An unknown partially-applied mutation is durable and never blindly replayed.
    argv = ["gh", "api", "graphql", "-f", "query=mutation{unknownWrite{id}}"]
    path = outbox.prepare(argv, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(argv, 1, "", "connection reset"), [])
    with patch.object(tr, "execute") as unsafe:
        assert outbox.drain(env) == [path.stem] and path.exists()
        unsafe.assert_not_called()
    opaque = path
    # A rejected creation retry that loses its response must become ambiguous:
    # the following drain reconciles instead of blindly creating another copy.
    comment = ["gh", "api", "-X", "POST", "/repos/org/repo/issues/7/comments", "-f", "body=retained-result"]
    path = outbox.prepare(comment, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(comment, 1, "", "API rate limit exceeded"), before_request=True)
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess(comment, 1, "", "connection reset")):
        assert set(outbox.drain(env)) == {opaque.stem, path.stem}
        assert json.loads(path.read_text())["status"] == "ambiguous"
    posted = [[{"body": "retained-result", "user": {"login": "author"}}]]
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(posted), "")) as read:
        assert outbox.drain(env) == [opaque.stem]
        assert read.call_count == 1 and "-X" not in read.call_args.args[0]
    # Permanent REST denial during replay is archived just like an initial denial.
    path = outbox.prepare(comment, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(comment, 1, "", "API rate limit exceeded"), before_request=True)
    denied = subprocess.CompletedProcess(comment, 1, "", "Resource not accessible")
    denied.github_records = [{"status": 403, "method": "POST"}]
    with patch.object(tr, "execute", return_value=denied):
        assert outbox.drain(env) == [opaque.stem]
        assert not path.exists() and path.with_suffix(".failed").exists()
    # Permission errors are HTTP 200 GraphQL responses, not ambiguous writes.
    resolve = [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
        "-F",
        "id=thread",
    ]
    path = outbox.prepare(resolve, env, tmp)
    denial = {"data": {"resolveReviewThread": None}, "errors": [{"type": "FORBIDDEN", "path": ["resolveReviewThread"]}]}
    outbox.finish(
        path,
        subprocess.CompletedProcess(resolve, 1, json.dumps(denial), "Forbidden"),
        [{"status": 200, "method": "POST"}],
    )
    assert not path.exists() and path.with_suffix(".failed").exists()
    # HTTP 200 quota errors and partial effects remain recoverable, not terminal.
    for payload in (
        {"data": None, "errors": [{"type": "RATE_LIMITED"}]},
        {"data": {"one": {"id": "created"}, "two": None}, "errors": [{"type": "FORBIDDEN"}]},
    ):
        path = outbox.prepare(argv, env, tmp)
        outbox.finish(
            path, subprocess.CompletedProcess(argv, 1, json.dumps(payload), ""), [{"status": 200, "method": "POST"}]
        )
        assert path.exists() and json.loads(path.read_text())["status"] == "ambiguous"
    # Previously retained permission denials must reconcile and release the worker too.
    path = outbox.prepare(resolve, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(resolve, 1, "", "lost response"), [])
    node = {"data": {"node": {"isResolved": False, "viewerCanResolve": False}}}
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(node), "")) as check:
        assert outbox.drain(env) == [opaque.stem]
        assert not path.exists() and path.with_suffix(".failed").exists()
        assert check.call_count == 1 and check.call_args.args[0][4].startswith("query=query")
    # Already-applied resolutions need no repeat mutation; permitted unresolved ones can retry.
    for resolved, can_resolve, expected in ((True, False, True), (False, True, False)):
        with patch.object(
            tr,
            "execute",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"data": {"node": {"isResolved": resolved, "viewerCanResolve": can_resolve}}}), ""
            ),
        ):
            assert outbox.reconcile({"argv": resolve[1:]}, env) is expected
    # Transient resolution uncertainty retains evidence without parking all future rounds.
    path = outbox.prepare(resolve, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess(resolve, 1, "", "lost response"), [])
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 1, "", "network unavailable")):
        assert outbox.drain(env) == [opaque.stem] and path.exists()
    path.unlink()
    # A temporary body file is copied into the intent before the round can delete it.
    body = Path(tmp) / "body.txt"
    body.write_text("finished model output")
    argv = ["gh", "pr", "create", "--repo", "org/repo", "--head", "author:branch", "--body-file", str(body)]
    path = outbox.prepare(argv, env, tmp)
    body.unlink()
    assert "finished model output" in json.loads(path.read_text())["assets"].values()
    assert "paid-result" not in (Path(tmp) / "requests.jsonl").read_text()
print("PASS: quota deferral, durable publication and ambiguous-write reconciliation")
