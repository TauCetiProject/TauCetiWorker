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
if os.environ.get("TAUCETI_TEST_GH_DENY"):
    for line in ["* Request at fixture", "* Request to https://api.github.com/repos/org/repo/issues/99/comments",
                 "> POST /repos/org/repo/issues/99/comments HTTP/1.1", "< HTTP/2.0 422", "* Request took 1ms"]:
        print(line,file=sys.stderr)
    print("validation failed",file=sys.stderr);sys.exit(1)
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
    # A definitive rejection crosses the shell boundary distinctly from a temporary wait.
    denial_args = ["api", "repos/org/repo/issues/99/comments", "--method", "POST", "-f", "body=invalid"]
    denied = subprocess.run(
        command + denial_args, env={**env, "TAUCETI_TEST_GH_DENY": "1"}, cwd=work, capture_output=True, text=True
    )
    assert denied.returncode == 78, denied.stderr
    assert list((work / "shared/outbox").glob("*/*/*.failed")), "rejection evidence must remain retained"

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
for state, expected in (("open", None), ("closed", True)):
    with patch.object(
        tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps({"state": state}), "")
    ) as request:
        assert outbox.reconcile({**value, "context": {}}, {}) is expected
        assert request.call_count == 1

# Successful lookup reads do not hide a definitive rejection, while partial writes stay ambiguous.
with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "rejection-fixture"}),
):
    for accepted in (False, True):
        argv = ["gh", "pr", "comment", "7", "--repo", "org/repo", "--body", "retained"]
        path = outbox.prepare(argv, dict(os.environ), tmp)
        failure = subprocess.CompletedProcess(argv, 1, "", "validation failed")
        records = [{"method": "GET", "status": 200}, {"method": "POST", "status": 422}]
        if accepted:
            records.insert(1, {"method": "POST", "status": 201})
        outbox.finish(path, failure, records)
        if accepted:
            assert path.exists() and json.loads(path.read_text())["status"] == "ambiguous"
        else:
            assert not path.exists() and path.with_suffix(".failed").exists()
            assert failure.github_permanent_failure is True

# A wholly rejected 401 can recover after credential refresh without parking paid prose.
with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "auth-fixture"}),
):
    argv = ["gh", "api", "-X", "POST", "repos/org/repo/issues/7/comments", "-f", "body=retained"]
    path = outbox.prepare(argv, dict(os.environ), tmp)
    rejected = subprocess.CompletedProcess(argv, 1, "", "Bad credentials")
    outbox.finish(path, rejected, [{"method": "GET", "status": 200}, {"method": "POST", "status": 401}])
    value = json.loads(path.read_text())
    assert value["status"] == "rejected" and value["retry_at"] >= time.time() + 295
    assert not getattr(rejected, "github_permanent_failure", False)

# A description edit whose acknowledgement was lost is settled from the live description, so its PR
# does not wait for manual recovery. A fix round that answers a quality-pass finding makes this edit.
with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "description-fixture"}),
):
    env = dict(os.environ)
    text = "This PR proves foo.\n\nQuality pass:\n- Q1 Mathlib duplicates: checked — none\n"
    report = Path(tmp) / "body.md"
    lost = subprocess.CompletedProcess([], 1, "", "connection reset")

    def description(body, edited, editor=None):
        pull = {"body": body, "lastEditedAt": edited, "editor": {"login": editor} if editor else None}
        return subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": {"pullRequest": pull}}}), "")

    def stamp(offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))

    def retained(*form):
        """An edit of PR 7's description that was sent and never acknowledged."""
        report.write_text(text)
        edit = ["gh", "pr", "edit", "7", "--repo", "org/repo", *form]
        path = outbox.prepare(edit, env, tmp)
        report.unlink()  # the round removes its files
        outbox.finish(path, lost, [])
        assert json.loads(path.read_text())["status"] == "ambiguous"
        assert json.loads(path.read_text())["context"]["body"] == text, form
        assert outbox.pending_for("org/repo", 7, env)
        return path

    # GitHub applied it: one read, nothing sent again, and later work on the PR may proceed.
    for form in (
        ["--body-file", str(report)],
        ["-F", str(report)],
        ["--body-file=" + str(report)],
        ["--body", text],
        ["-b", text],
        ["--body=" + text],
    ):
        path = retained(*form)
        applied = description(text.replace("\n", "\r\n"), stamp(0))
        with patch.object(tr, "execute", return_value=applied) as read:
            assert outbox.drain(env) == [] and not path.exists(), form
            assert read.call_count == 1 and read.call_args.args[0][1:3] == ["api", "graphql"]
        assert not outbox.pending_for("org/repo", 7, env)

    # GitHub never applied it: the edit is sent again, from the copy kept of the file.
    for form, edited in (
        (["--body-file", str(report)], None),
        (["-F", str(report)], "2020-01-01T00:00:00Z"),
        (["--body-file=" + str(report)], stamp(-3600)),
    ):
        path = retained(*form)
        sent = []

        def unapplied(args, edited=edited, sent=sent, **_kwargs):
            if args[1:3] == ["api", "graphql"]:
                return description("This PR proves foo.", edited)
            name = next(a for a in reversed(args) if "body" in a or a.endswith(".body"))
            sent.append(Path(name.removeprefix("--body-file=")).read_text())
            return subprocess.CompletedProcess(args, 0, "{}", "")

        with patch.object(tr, "execute", side_effect=unapplied):
            assert outbox.drain(env) == [] and not path.exists(), form
        assert sent == [text] and not outbox.pending_for("org/repo", 7, env), form
        assert not list(outbox.directory(env).glob("*.body"))

    # A later edit left other text there: this one is set aside, never replayed over it.
    path = retained("--body-file", str(report))
    with patch.object(tr, "execute", return_value=description("A newer description.", stamp(30))) as read:
        assert outbox.drain(env) == [] and not path.exists() and path.with_suffix(".failed").exists()
        assert read.call_count == 1
    assert not outbox.pending_for("org/repo", 7, env)

    # An unreadable description settles nothing; the intent is kept and its PR still waits.
    path = retained("--body-file", str(report))
    value = json.loads(path.read_text())
    for unread in (
        subprocess.CompletedProcess([], 1, "", "timeout"),
        subprocess.CompletedProcess([], 0, "not json", ""),
        subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": {"pullRequest": None}}}), ""),
    ):
        with patch.object(tr, "execute", return_value=unread):
            assert outbox.reconcile(value, env) is None
    with patch.object(tr, "execute", return_value=unread):
        assert outbox.drain(env) == [path.stem] and path.exists()
    assert outbox.pending_for("org/repo", 7, env)
    for live, expected in (
        (description(text, None), True),
        (description(None, None), False),
        (description("other", stamp(-3600)), False),
        (description("other", stamp(-120)), "superseded"),  # within the allowance for clock skew
        (description("other", stamp(120)), "superseded"),
    ):
        with patch.object(tr, "execute", return_value=live):
            assert outbox.reconcile(value, env) == expected, (live.stdout, expected)
    # In the minutes before the intent was written, this clock and GitHub's cannot order two edits,
    # so an edit there counts as later whoever made it: workers share their operator's account.
    author = {**env, "TAUCETI_GITHUB_ACCOUNT": "Author"}
    for editor in ("author", "maintainer", None):
        with patch.object(tr, "execute", return_value=description("other", stamp(-120), editor)):
            assert outbox.reconcile(value, author) == "superseded", editor
    path.unlink()

    # The same check is made before every later attempt, whatever became of the last one.
    edit = ["gh", "pr", "edit", "7", "--repo", "org/repo", "--body-file", str(report)]

    def unsent():
        """An edit of PR 7's description that was written down and never sent."""
        report.write_text(text)
        path = outbox.prepare(edit, env, tmp)
        report.unlink()
        assert json.loads(path.read_text())["status"] == "prepared"
        path.with_suffix(".failed").unlink(missing_ok=True)
        return path

    def rejected():
        """An edit of PR 7's description that was refused before it was sent."""
        path = unsent()
        outbox.finish(path, subprocess.CompletedProcess([], 1, "", "API rate limit exceeded"), before_request=True)
        assert json.loads(path.read_text())["status"] == "rejected"
        return path

    def github(live, sent, answer=None):
        """A GitHub whose description is `live`. The text of every edit sent to it goes in `sent`."""

        def execute(args, **_kwargs):
            if args[1:3] == ["api", "graphql"]:
                return live
            sent.append(Path(args[-1]).read_text())
            return answer or subprocess.CompletedProcess(args, 0, "{}", "")

        return execute

    # A replay that is itself refused before sending leaves the intent `rejected`. A maintainer
    # edits the description meanwhile: the next drain sets the old text aside and sends nothing.
    path = retained(*edit[6:])
    path.with_suffix(".failed").unlink(missing_ok=True)
    sent = []
    refusal = subprocess.CompletedProcess([], 1, "", "gh: secondary rate limit; Retry-After: 30\n")
    refusal.github_before_request = True
    with patch.object(tr, "execute", side_effect=github(description("This PR proves foo.", None), sent, refusal)):
        assert outbox.drain(env) == [path.stem]
    assert sent == [text] and json.loads(path.read_text())["status"] == "rejected"
    newer = description("A maintainer's description.", stamp(30), "maintainer")
    with patch.object(tr, "execute", side_effect=github(newer, sent)):
        assert outbox.drain(env) == [] and not path.exists() and path.with_suffix(".failed").exists()
    assert sent == [text], "an older text went back over the maintainer's description"
    assert not outbox.pending_for("org/repo", 7, env)

    # So it is for an edit refused at its first attempt, and for one never sent.
    for make in (rejected, unsent):
        path, sent = make(), []
        with patch.object(tr, "execute", side_effect=github(newer, sent)):
            assert outbox.drain(env) == [] and not path.exists() and path.with_suffix(".failed").exists()
        assert sent == [], make.__name__

    # While the description is still the one it was written to replace, a refused edit is sent
    # again as before: never edited, or last edited long before.
    for live in (
        description("This PR proves foo.", None),
        description("This PR proves foo.", stamp(-3600), "maintainer"),
    ):
        path, sent = rejected(), []
        with patch.object(tr, "execute", side_effect=github(live, sent)):
            assert outbox.drain(env) == [] and not path.exists(), live.stdout
        assert sent == [text] and not path.with_suffix(".failed").exists(), live.stdout

    # This host's clock runs a minute ahead of GitHub's. The operator, on the account the worker
    # shares, edits the description half a minute after the refused edit was written, so GitHub
    # stamps that later edit half a minute before the intent. The older text is not sent over it.
    ahead = 60
    path, sent = rejected(), []
    operator = description("The operator's later description.", stamp(30 - ahead), "author")
    with patch.object(tr, "execute", side_effect=github(operator, sent)):
        assert outbox.drain(author) == [] and not path.exists() and path.with_suffix(".failed").exists()
    assert sent == [], "an older text went back over a later edit from the same account"

    # The description cannot be read: nothing is sent over a text that was not seen, and the
    # intent waits as it was.
    path, sent = rejected(), []
    unread = subprocess.CompletedProcess([], 1, "", "timeout")
    with patch.object(tr, "execute", side_effect=github(unread, sent)):
        assert outbox.drain(env) == [path.stem] and sent == []
    held = json.loads(path.read_text())
    assert held["status"] == "rejected" and held["retry_at"] > time.time() + 50
    assert outbox.pending_for("org/repo", 7, env)
    path.unlink()

    # The description already carries this text, from another command: the intent is finished.
    path, sent = rejected(), []
    with patch.object(tr, "execute", side_effect=github(description(text, stamp(0), "author"), sent)):
        assert outbox.drain(env) == [] and not path.exists() and sent == []
    assert not path.with_suffix(".failed").exists()

    # The same command issued again finds its retained intent, which is checked in the same way
    # before it is sent. An intent this command wrote itself is sent without a read.
    path = rejected()
    written = json.loads(path.read_text())["created"]
    with patch.object(tr, "execute") as read:
        assert outbox.may_send(path, env, written) and outbox.may_send(path)
        read.assert_not_called()
    for live in (description("This PR proves foo.", None), description(text, stamp(0), "author")):
        with patch.object(tr, "execute", return_value=live) as read:
            assert outbox.may_send(path, env, written + 1) and read.call_count == 1
    with patch.object(tr, "execute", return_value=unread):
        assert not outbox.may_send(path, env, written + 1)
    assert json.loads(path.read_text())["status"] == "rejected" and outbox.retry_at(path) > time.time() + 50
    with patch.object(tr, "execute", return_value=newer):
        try:
            outbox.may_send(path, env, written + 1)
            raise AssertionError("an older text was allowed over a later description")
        except GitHubUnavailable as refused:
            assert "later edit" in str(refused) and refused.retry_at < time.time() + 5
    assert not path.exists() and path.with_suffix(".failed").exists()
    assert not outbox.pending_for("org/repo", 7, env)

    # Through the transport, which tells a retained intent from the command's own by when the
    # command began. Refused once, the same command is a new one, written now, and is sent.
    path, sent = rejected(), []

    def transport(argv, **_kwargs):
        if argv[1:3] == ["api", "graphql"]:
            return newer
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    report.write_text(text)
    with patch.object(tr, "_execute", side_effect=transport):
        try:
            tr.execute(edit, env=env, cwd=tmp)
            raise AssertionError("an older text was sent over a later description")
        except GitHubUnavailable:
            assert sent == [] and not path.exists() and path.with_suffix(".failed").exists()
        assert tr.execute(edit, env=env, cwd=tmp).returncode == 0 and sent == [edit]
    report.unlink()
    assert not outbox.pending_for("org/repo", 7, env)
    # Other intents are sent as before, with no read.
    comment = ["gh", "pr", "comment", "7", "--repo", "org/repo", "--body", "Updated the description."]
    path = outbox.prepare(comment, env, tmp)
    outbox.finish(path, subprocess.CompletedProcess([], 1, "", "API rate limit exceeded"), before_request=True)
    with patch.object(tr, "execute") as read:
        assert outbox.may_send(path, env, time.time() + 1)
        read.assert_not_called()
    path.unlink()

    # Labels and titles are separate writes: an edit that carries one is not settled by its text,
    # and an edit with no description is as it was.
    for extra in (["--add-label", "self-tested"], ["--title", "A title"], ["-t", "A title"], ["--base", "main"]):
        compound = {
            "argv": ["pr", "edit", "7", "--repo", "org/repo", "--body", text, *extra],
            "context": {"body": text},
        }
        with patch.object(tr, "execute") as read:
            assert outbox.reconcile(compound, env) is None, extra
            read.assert_not_called()
    label = {"argv": ["pr", "edit", "7", "--repo", "org/repo", "--add-label", "self-tested"], "context": {}}
    with patch.object(tr, "execute") as read:
        assert outbox.reconcile(label, env) is None
        read.assert_not_called()
    assert outbox.description_edit(["pr", "edit", "7", "-R", "org/repo", "--body", "- starts with a dash"])
    assert outbox.description_edit(["pr", "edit", "https://github.com/org/repo/pull/7", "--repo=org/repo", "-b", "x"])
    assert not outbox.description_edit(["pr", "edit", "7", "--repo", "org/repo"])
    assert not outbox.description_edit(["pr", "comment", "7", "--body", "x"])
print("PASS: description edits are reconciled against the live description")
