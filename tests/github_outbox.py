#!/usr/bin/env python3
"""Publication survives REST exhaustion, process restart, and ambiguous acknowledgements."""

import json
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

with tempfile.TemporaryDirectory() as standalone_tmp:
    # Agents invoke this file through a generated shell shim, not as an imported
    # package. Exercise that entry point from an unrelated working directory.
    work = Path(standalone_tmp)
    native = work / "gh"
    native.write_text("""#!/usr/bin/env python3
import json,os,sys
with open(os.environ["TAUCETI_TEST_GH_CALLS"],"a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")
if os.environ.get("TAUCETI_TEST_GH_FAIL"):
    print("connection reset",file=sys.stderr);sys.exit(1)
print("{}")
""")
    native.chmod(0o700)
    calls = work / "calls.jsonl"
    env = {
        **os.environ,
        "GH_TOKEN": "standalone-fixture",
        "TAUCETI_GITHUB_ACCOUNT": "author",
        "TAUCETI_WORKER_ID": "standalone",
        "TAUCETI_GITHUB_SHARED_DIR": str(work / "shared"),
        "TAUCETI_REAL_GH": str(native),
        "TAUCETI_TEST_GH_CALLS": str(calls),
    }
    command = [sys.executable, str(Path(tr.__file__).resolve()), "--proxy"]
    for args in (
        ["api", "repos/org/repo/issues/7/comments", "--method", "POST", "-f", "body=one"],
        ["pr", "comment", "8", "--repo", "org/repo", "--body", "two"],
    ):
        result = subprocess.run(command + args, env=env, cwd=work, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 2
    args = ["api", "repos/org/repo/issues/9/comments", "--method", "POST", "-f", "body=ambiguous"]
    result = subprocess.run(
        command + args, env={**env, "TAUCETI_TEST_GH_FAIL": "1"}, cwd=work, capture_output=True, text=True
    )
    assert result.returncode == 1 and "publication retained" in result.stderr
    started = time.time()
    result = subprocess.run(command + args, env=env, cwd=work, capture_output=True, text=True)
    assert result.returncode == 77 and "Traceback" not in result.stderr, result.stderr
    assert len(calls.read_text().splitlines()) == 3, "an ambiguous write cannot be repeated by the shell entry point"
    with patch.dict(os.environ, env):
        block = tr.blocking_event_since(started)
        assert block and block["resource"] == "publication" and block["reset"] > started + 55

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
    assert outbox.prepare(["gh", "search", "prs", "roadmap", "--repo", "org/repo"], env, tmp) is None
    legacy_search = outbox.directory(env) / "legacy-search.json"
    tr.atomic(legacy_search, {"argv": ["search", "prs", "roadmap"], "cwd": tmp, "status": "ambiguous"})
    with patch.object(tr, "execute") as replay:
        assert outbox.drain(env) == []
        assert not legacy_search.exists() and legacy_search.with_suffix(".failed").exists()
        replay.assert_not_called()
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
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    published = [[{"body": "paid-result", "created_at": now, "user": {"login": "author"}}]]
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
    posted = [[{"body": "retained-result", "created_at": now, "user": {"login": "author"}}]]
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
        expected = "rejected" if payload.get("data") is None else "ambiguous"
        assert path.exists() and json.loads(path.read_text())["status"] == expected
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
    # An opaque CLI lookup POST is a read, not evidence of a partially applied mutation.
    separate = {**env, "GH_TOKEN": "opaque-quota-fixture"}
    comment = ["gh", "pr", "comment", "7", "--repo", "org/repo", "--body", "opaque-result"]
    trace = ""
    for query, status, remaining in (
        ("query{viewer{login}}", 200, 20),
        ('mutation{addComment(input:{subjectId:"id",body:"opaque-result"}){clientMutationId}}', 403, 0),
    ):
        trace += (
            "* Request at now\n* Request to https://api.github.com/graphql\n> POST /graphql HTTP/1.1\n"
            + json.dumps({"query": query})
            + f"\n< HTTP/2.0 {status}\n< X-Ratelimit-Resource: graphql\n< X-Ratelimit-Remaining: {remaining}\n< X-Ratelimit-Reset: {int(time.time()) + 60}\n* Request took 1ms\n"
        )
    with patch.object(
        tr.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(comment, 1, "", trace + "API rate limit exceeded\n"),
    ):
        tr.execute(comment, env=separate)
    retained = next(outbox.directory(separate).glob("*.json"))
    assert json.loads(retained.read_text())["status"] == "rejected"
    # A second execute cannot blindly repeat an ambiguous publication.
    from tauceti_worker.config import GitHubUnavailable

    separate = {**env, "GH_TOKEN": "ambiguous-duplicate-fixture"}
    duplicate = ["gh", "api", "-X", "POST", "/repos/org/repo/issues/7/comments", "-f", "body=one-output"]
    retained = outbox.prepare(duplicate, separate, tmp)
    outbox.finish(retained, subprocess.CompletedProcess(duplicate, 1, "", "lost acknowledgement"))
    with patch.object(tr.subprocess, "run") as network:
        try:
            tr.execute(duplicate, env=separate)
        except GitHubUnavailable as error:
            assert error.retry_at >= time.time() + 55, "ambiguous publications cannot poll every five seconds"
        else:
            raise AssertionError("expected retained-publication pause")
        network.assert_not_called()
    assert outbox.pending_for("org/repo", 7, separate) and not outbox.pending_for("org/repo", 8, separate)
    # Typed file fields are copied, reconciled as actual bodies, and removed after replay.
    separate = {**env, "GH_TOKEN": "typed-body-fixture"}
    body = Path(tmp) / "reply.txt"
    body.write_text("retained-reply")
    reply = ["gh", "api", "-X", "POST", "/repos/org/repo/pulls/7/comments/8/replies", "-F", "body=@" + str(body)]
    retained = outbox.prepare(reply, separate, tmp)
    body.unlink()
    value = json.loads(retained.read_text())
    published = [
        [
            {
                "body": "retained-reply",
                "user": {"login": "author"},
                "in_reply_to_id": 8,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value["created"])),
            }
        ]
    ]
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(published), "")):
        assert outbox.reconcile(value, separate) is True
    outbox.finish(retained, subprocess.CompletedProcess(reply, 1, "", "quota"), before_request=True)

    def replay(args, **_kwargs):
        filename = Path(next(a[6:] for a in args if a.startswith("body=@")))
        assert filename.read_text() == "retained-reply"
        return subprocess.CompletedProcess(args, 0, "{}", "")

    with patch.object(tr, "execute", side_effect=replay):
        assert outbox.drain(separate) == []
    assert not list(outbox.directory(separate).glob("*.body"))
    # A JSON input file survives cleanup and reconciles an accepted overlap issue.
    separate = {**env, "GH_TOKEN": "issue-creation-fixture"}
    payload = Path(tmp) / "overlap.json"
    issue_body = "Finding\n\n<!-- tauceti-overlap:fingerprint -->"
    payload.write_text(json.dumps({"title": "Finding", "body": issue_body}))
    creation = ["gh", "api", "--method", "POST", "repos/org/repo/issues", "--input", str(payload)]
    retained = outbox.prepare(creation, separate, tmp)
    payload.unlink()
    value = json.loads(retained.read_text())
    assert value["context"]["body"] == issue_body
    # The local clock can run ahead of GitHub without causing a duplicate issue.
    published = [
        [
            {
                "body": issue_body,
                "user": {"login": "author"},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value["created"] - 120)),
            }
        ]
    ]
    with patch.object(
        tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(published), "")
    ) as read:
        assert outbox.reconcile(value, separate) is True
        assert "creator=author" in read.call_args.args[0][2]
    published[0][0]["created_at"] = "2000-01-01T00:00:00Z"
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(published), "")):
        assert outbox.reconcile(value, separate) is False
    legacy = {**value, "context": {}}
    published[0][0]["created_at"] = now
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(published), "")):
        assert outbox.reconcile(legacy, separate) is True
    # Unpinned clients resolve their actual author once in their credential scope.
    unpinned = {k: v for k, v in separate.items() if k != "TAUCETI_GITHUB_ACCOUNT"}
    published[0][0]["body"] = issue_body.replace("\n", "\r\n") + "\n"
    viewer = subprocess.CompletedProcess([], 0, json.dumps({"data": {"viewer": {"login": "author"}}}), "")
    listing = subprocess.CompletedProcess([], 0, json.dumps(published), "")
    with patch.object(tr, "execute", side_effect=[viewer, listing]) as read:
        assert outbox.reconcile(value, unpinned) is True and read.call_count == 2
    with patch.object(tr, "execute", return_value=listing) as read:
        assert outbox.reconcile(value, unpinned) is True and read.call_count == 1
    assert "paid-result" not in (Path(tmp) / "requests.jsonl").read_text()
print("PASS: quota deferral, durable publication and ambiguous-write reconciliation")


# An uncertain close is reconciled against its exact original head and actor.
head = "a" * 40
value = {
    "argv": ["pr", "close", "7", "--repo", "org/repo"],
    "context": {"pr": "7", "expected_head": head, "actor": "author"},
}
for live, expected in (
    ({"state": "closed"}, True),
    ({"state": "open", "head": {"sha": head}, "user": {"login": "author"}, "labels": []}, False),
    ({"state": "open", "head": {"sha": "b" * 40}, "user": {"login": "author"}}, "obsolete"),
    ({"state": "open", "head": {"sha": head}, "user": {"login": "peer"}}, "obsolete"),
    ({"state": "open", "head": {"sha": head}, "user": {"login": "author"}, "labels": [{"name": "keep"}]}, "obsolete"),
):
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(live), "")):
        assert outbox.reconcile(value, {}) == expected, (live, expected)
with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 1, "", "timeout")):
    assert outbox.reconcile(value, {}) is None
with patch.object(tr, "execute") as request:
    assert outbox.reconcile({**value, "context": {}}, {}) is None
    request.assert_not_called()
