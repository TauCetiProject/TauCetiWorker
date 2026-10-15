#!/usr/bin/env python3
"""Cross-process cache correctness, complete GraphQL histories and safe exhaustion behavior."""

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_transport as tr

for kind in ("code", "commits", "issues", "prs", "repos"):
    assert tr.proxy_readonly(["search", kind, "query", "--json", "url"])
assert not tr.proxy_readonly(["search", "unknown", "query"])
from tauceti_worker.github import GitHub, github_budget
from tauceti_worker.review_state import ReviewState

assert tr.proxy_readonly(
    ["api", "graphql", "-f", 'query=query{search(query:"mutation",type:ISSUE,first:1){issueCount}}']
)
assert tr.proxy_readonly(["api", "graphql", "-f", "query=query{viewer{login}}", "-f", "body=mutation"])
assert not tr.proxy_readonly(
    ["api", "graphql", "-f", 'query=mutation{addComment(input:{subjectId:"id",body:"body"}){clientMutationId}}']
)
assert not tr.proxy_readonly(
    ["api", "graphql", "-f", 'query=query{viewer{login}} mutation{deleteIssue(input:{issueId:"id"}){clientMutationId}}']
)


def coalesced_child(counter):
    def fetch():
        with open(counter, "a") as f:
            f.write("network\n")
        time.sleep(0.1)
        return [1, 2]

    assert tr.shared_read(["concurrent", "repo"], fetch) == [1, 2]


def comment(cid, *, body="hello", bot=False, reply=None):
    return {
        "id": f"node-{cid}",
        "fullDatabaseId": str(cid),
        "body": body,
        "createdAt": "2026-10-10T00:00:00Z",
        "updatedAt": "2026-10-10T00:00:00Z",
        "authorAssociation": "CONTRIBUTOR",
        "author": {"login": "peer", "__typename": "Bot" if bot else "User"},
        "replyTo": {"fullDatabaseId": str(reply)} if reply else None,
    }


def conn(nodes, more=False, cursor="next"):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def response(data, *, errors=None, rc=0):
    return subprocess.CompletedProcess([], rc, json.dumps({"data": data, **({"errors": errors} if errors else {})}), "")


with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "GH_TOKEN": "fake-private-token",
            "TAUCETI_GITHUB_SHARED_DIR": tmp,
            "TAUCETI_WORKER_ID": "test",
        },
    ),
):
    os.environ.pop("TAUCETI_REAL_GH", None)
    calls = []

    def fetch():
        calls.append(1)
        return len(calls)

    assert tr.shared_read(["read"], fetch) == tr.shared_read(["read"], fetch) == 1
    with tr.read_context(fresh=True):
        assert tr.shared_read(["read"], fetch) == 2
    with tr.read_context(revision="new-head"):
        assert tr.shared_read(["read"], fetch) == 3
    with patch.dict(os.environ, {"GH_TOKEN": "different-permissions"}):
        assert tr.shared_read(["read"], fetch) == 4
    tr.invalidate()
    assert tr.shared_read(["read"], fetch) == 5
    assert tr.shared_read(["node-id"], lambda: "node", immutable=True) == "node"
    tr.invalidate()
    assert tr.shared_read(["node-id"], lambda: "unexpected", immutable=True) == "node"
    with tr.read_context(fresh=True):
        assert tr.shared_read(["node-id"], lambda: "live", immutable=True) == "live"
    failures = []

    def fail():
        failures.append(1)
        return None

    assert tr.shared_read(["failure"], fail) is None
    assert tr.shared_read(["failure"], fail) is None and len(failures) == 2

    def overlapping_write():
        tr.invalidate()
        return "obsolete"

    assert tr.shared_read(["race"], overlapping_write) == "obsolete"
    assert tr.shared_read(["race"], lambda: "new") == "new"
    counter = str(Path(tmp) / "fetches")
    ctx = multiprocessing.get_context("fork")
    children = [ctx.Process(target=coalesced_child, args=(counter,)) for _ in range(4)]
    for child in children:
        child.start()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    assert Path(counter).read_text().splitlines() == ["network"]

    # Every actual HTTP request/page is counted; secrets/debug bodies never leave the private capture.
    reset = int(time.time()) + 100
    trace = f"""* Request at now
* Request to https://api.github.com/repos/org/repo/issues/123/comments?search=private-query
> GET /repos/org/repo/issues/123/comments HTTP/1.1
> Authorization: fake-private-token
private-request-body
< HTTP/2.0 403 Forbidden
< X-Ratelimit-Resource: core
< X-Ratelimit-Remaining: 0
< X-Ratelimit-Reset: {reset}
< Retry-After: 3
private-response-body
* Request took 1ms
gh: API rate limit exceeded
"""
    records = []
    start = time.time()
    clean = tr.parse_trace(trace, identity=tr.scope(), records=records)
    assert clean == "gh: API rate limit exceeded\n" and len(records) == 1
    evidence = (Path(tmp) / "requests.jsonl").read_text()
    assert all(
        s not in evidence
        for s in ("private-query", "private-request-body", "private-response-body", "fake-private-token")
    )
    assert records[0]["endpoint"] == "/repos/org/repo/issues/{id}/comments"
    # Installed gh pretty-prints JSON debug bodies. Account measured costs and
    # distinguish read POSTs without persisting query text or response bodies.
    pretty = "* Request at now\n* Request to https://api.github.com/graphql\n> POST /graphql HTTP/1.1\n"
    pretty += json.dumps({"query": "query{rateLimit{cost} viewer{login}}"}, indent=2) + "\n< HTTP/2.0 200 OK\n"
    pretty += (
        json.dumps({"data": {"rateLimit": {"cost": 7}, "viewer": {"login": "private-actor"}}}, indent=2)
        + "\n* Request took 2ms\n"
    )
    gql_records = []
    assert tr.parse_trace(pretty, records=gql_records) == ""
    assert gql_records[0]["graphql_operation"] == "read" and gql_records[0]["graphql_cost"] == 7
    assert "private-actor" not in (Path(tmp) / "requests.jsonl").read_text()
    assert tr.blocked_since(start)
    # An optimistic, late-arriving header cannot restore an exhausted bucket.
    tr.remember_quota({"resource": "core", "remaining": 5000, "reset": reset}, tr.scope())
    assert tr.observed_budget()["core"] == (0, reset)
    with patch.object(tr.subprocess, "run", return_value=response({})) as network:
        assert tr.execute(["gh", "api", "user"]).returncode != 0
        network.assert_not_called()
        assert tr.execute(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"]).returncode == 0
        assert network.call_count == 1
    with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as network:
        assert tr.execute(["gh", "pr", "comment", "9", "--repo", "org/repo", "--body", "gql-write"]).returncode == 0
        network.assert_called_once()  # An opaque command can use GraphQL while core is exhausted.
    with patch("tauceti_worker.github.run", return_value=subprocess.CompletedProcess([], 0, '{"core":[5000,1]}', "")):
        assert github_budget()["core"] == (0, reset)
    with patch("tauceti_worker.github.run", return_value=subprocess.CompletedProcess([], 1, "", "offline")):
        assert github_budget()["core"] == (0, reset)

    # Partial commands are not replayed: doing so could duplicate a successful first write.
    tr.invalidate()
    quota_file = Path(tmp) / "quota" / tr.scope() / "core.json"
    quota_file.unlink()
    partial = trace.replace("< HTTP/2.0 403 Forbidden", "< HTTP/2.0 201 Created") + trace
    with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", partial)) as network:
        tr.execute(["gh", "pr", "create"], wait=True)
        assert network.call_count == 1

    # Secondary limits share a cooldown even for an opaque write that cannot safely be replayed.
    with patch.dict(os.environ, {"GH_TOKEN": "secondary-token"}):
        secondary = trace.replace("403 Forbidden", "429 Too Many Requests").replace("Remaining: 0", "Remaining: 42")
        with patch.object(
            tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", secondary)
        ) as network:
            assert tr.execute(["gh", "pr", "create"], wait=True).returncode != 0
            assert tr.execute(["gh", "api", "user"]).returncode != 0
            assert network.call_count == 1 and tr.cooldown() > 0
    # HTTP-200 GraphQL quota responses are safe to wait/retry for reads, including missing headers.
    with patch.dict(os.environ, {"GH_TOKEN": "logical-wait-fixture"}):
        wall = [time.time()]
        waits = []

        def wait_reset(seconds):
            waits.append(seconds)
            wall[0] += seconds

        failure = response(None, errors=[{"extensions": {"code": "RATE_LIMIT"}, "message": "quota"}])
        with (
            patch.object(tr.time, "time", side_effect=lambda: wall[0]),
            patch.object(tr.time, "sleep", side_effect=wait_reset),
            patch.object(
                tr.subprocess, "run", side_effect=[failure, response({"viewer": {"login": "peer"}})]
            ) as network,
        ):
            assert tr.execute(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"], wait=True).returncode == 0
            assert network.call_count == 2 and len(waits) == 1

    # GraphQL can fail logically with HTTP 200: quota attribution must still protect PR counters.
    for quota_type in ("RATE_LIMIT", "RATE_LIMITED"):
        with patch.dict(os.environ, {"GH_TOKEN": "headerless-" + quota_type}):
            logical_error = {
                "data": None,
                "errors": [{"type": quota_type, "message": "API rate limit already exceeded"}],
            }
            start = time.time()
            with patch.object(
                tr.subprocess, "run", return_value=response(None, errors=logical_error["errors"])
            ) as network:
                assert tr.execute(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"]).returncode != 0
                assert tr.blocked_since(start)
                assert tr.execute(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"]).returncode != 0
                assert network.call_count == 1
    with patch.dict(os.environ, {"GH_TOKEN": "secondary-and-primary-token"}):
        secondary_zero = trace.replace("403 Forbidden", "429 Too Many Requests")
        secondary_seen = json.loads((Path(tmp) / "quota-types.json").read_text())["secondary:unspecified"]["count"]
        with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", secondary_zero)):
            assert tr.execute(["gh", "api", "user"]).returncode != 0
            assert tr.cooldown() > 0
        assert (
            json.loads((Path(tmp) / "quota-types.json").read_text())["secondary:unspecified"]["count"] > secondary_seen
        )
    with patch.dict(os.environ, {"GH_TOKEN": "graphql-exhausted-token"}):
        tr.remember_quota({"resource": "graphql", "remaining": 5000, "reset": reset + 1000}, tr.scope())
        gql_trace = trace.replace("403 Forbidden", "200 OK").replace("Resource: core", "Resource: graphql")
        start = time.time()
        with patch.object(tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", gql_trace)):
            assert tr.execute(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"]).returncode != 0
        assert tr.blocked_since(start) and tr.observed_budget()["graphql"] == (0, reset)
        tr.remember_quota({"resource": "graphql", "remaining": 4900, "reset": reset + 1000}, tr.scope())
        assert tr.observed_budget()["graphql"] == (0, reset)
        with patch.object(tr.time, "time", return_value=reset + 1):
            assert tr.observed_budget()["graphql"][0] > 0

    # Missing headers still cause a bounded pause; reset expiry permits the next real request.
    with patch.dict(os.environ, {"GH_TOKEN": "headerless-token"}):
        with patch.object(
            tr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "gh: API rate limit exceeded\n")
        ) as network:
            assert tr.execute(["gh", "api", "user"]).returncode != 0
            assert tr.execute(["gh", "api", "user"]).returncode != 0
            assert network.call_count == 1
        retry_at = tr.observed_budget()["core"][1]
        with (
            patch.object(tr.time, "time", return_value=retry_at + 1),
            patch.object(tr.subprocess, "run", return_value=response({})) as network,
        ):
            assert tr.execute(["gh", "api", "user"]).returncode == 0
            network.assert_called_once()

    # Issue history can exceed 100 comments; a missing later page is a failure, not an empty history.
    gh = GitHub("org/repo")
    pages = [
        response({"repository": {"issueOrPullRequest": {"comments": conn([comment(1, bot=True)], True)}}}),
        response({"repository": {"issueOrPullRequest": {"comments": conn([comment(2)])}}}),
    ]
    with tr.read_context(fresh=True), patch.object(gh, "_gh", side_effect=pages) as query:
        rows = gh.issue_comments(7)
        assert [r["id"] for r in rows] == [1, 2] and rows[0]["user"]["login"] == "peer[bot]"
        assert "cursor=next" in query.call_args.args[0]
        assert all(call.args[0][1] == "graphql" for call in query.call_args_list)
    with tr.read_context(fresh=True), patch.object(gh, "_gh", side_effect=[pages[0], response(None, rc=1)]):
        assert gh.issue_comments(7) is None
    with (
        tr.read_context(fresh=True),
        patch.object(gh, "_gh", return_value=response(None, errors=[{"message": "rate limited"}])),
    ):
        assert gh.issue_comments(7) is None
    with tr.read_context(fresh=True), patch.object(gh, "_gh", return_value=pages[0]):
        assert gh.issue_comments(7) is None  # repeated cursor, no silent loop/truncation

    # Both outer thread pages and inner comment pages preserve REST root/reply identity.
    threads = [
        response(
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": conn([{"id": "thread-1", "comments": conn([comment(1)], True)}], True, "outer")
                    }
                }
            }
        ),
        response(
            {
                "repository": {
                    "pullRequest": {"reviewThreads": conn([{"id": "thread-2", "comments": conn([comment(4)])}])}
                }
            }
        ),
        response(
            {"node": {"comments": conn([comment(1, body="<!--tauceti-rubric:reuse-->"), comment(2, reply=1)], True)}}
        ),
        response({"node": {"comments": conn([comment(3, reply=1)])}}),
    ]
    with tr.read_context(fresh=True), patch.object(gh, "_gh", side_effect=threads):
        rows = gh.review_comments(7)
        assert [r["id"] for r in rows] == [1, 2, 3, 4]
        assert rows[1]["in_reply_to_id"] == 1
        assert ReviewState._newest_contest(rows)["id"] == 3

    # Claims are always live, while their immutable node-id mapping avoids repeated REST lookups.
    reactions = response(
        {
            "node": {
                "reactions": conn(
                    [
                        {
                            "databaseId": 99,
                            "content": "EYES",
                            "createdAt": "2026-10-10T00:00:00Z",
                            "user": {"login": "peer"},
                        }
                    ]
                )
            }
        }
    )
    with patch.object(gh, "_gh", return_value=reactions) as query:
        assert gh.reactions(3)[0]["content"] == "eyes"
        assert gh.reactions(3)[0]["id"] == 99
        assert query.call_count == 2 and all(c.args[0][1] == "graphql" for c in query.call_args_list)

    # A fresh dispatch check must cross the new shared cache, not only the old local sidecar.
    old = comment(55, body='<!--tauceti-scoreboard--><!--tauceti-meta:v1 {"head_sha":"old"}-->')
    new = comment(55, body='<!--tauceti-scoreboard--><!--tauceti-meta:v1 {"head_sha":"new"}-->')
    rs = ReviewState(SimpleNamespace(sbcache=Path(tmp) / "scoreboard"), gh)
    rs.observe([SimpleNamespace(number=9, updated_at="unchanged")])
    with patch.object(
        gh,
        "_gh",
        side_effect=[
            response({"repository": {"issueOrPullRequest": {"comments": conn([old])}}}),
            response({"repository": {"issueOrPullRequest": {"comments": conn([new])}}}),
        ],
    ) as query:
        assert rs.gh_meta(9).data["head_sha"] == "old"
        assert rs.gh_meta(9, force=True).data["head_sha"] == "new" and query.call_count == 2

    # Exhaustion during the engine does not consume a PR's lifetime review-error allowance.
    from tauceti_worker import work_units
    from tauceti_worker.config import NoProgress

    counters = SimpleNamespace(
        write=lambda *a: None, incr=lambda *a: (_ for _ in ()).throw(AssertionError("charged quota failure"))
    )
    worker = SimpleNamespace(
        cfg=SimpleNamespace(state=Path(tmp), logdir=Path(tmp), store_dir=Path(tmp), wid="test"),
        counters=counters,
        rs=SimpleNamespace(review_rounds=lambda *a: 0),
        gh=SimpleNamespace(),
    )
    candidate = SimpleNamespace(pr=7, head="abc", contest=None, contest_reply_id=None, rebase_budget_key="rebase-pr-7")

    def exhausted(*args):
        tr.record_block("core", time.time() + 100)
        return 1

    with (
        patch.object(work_units, "run_to_logfile", side_effect=exhausted),
        patch.object(work_units, "me", return_value="test"),
    ):
        try:
            work_units.do_review(worker, None, candidate, SimpleNamespace(work_model="claude"), False)
            raise AssertionError("expected a quota pause")
        except NoProgress as e:
            assert "not charged" in str(e)

    # The common stage guard covers failures before the agent (checkout/claim), during the
    # agent, and after it (progress apply/publishing), preserving previously accrued counters.
    values = {}
    worker.counters = SimpleNamespace(
        read=lambda key: values.get(key, 0), write=lambda key, value: values.__setitem__(key, value)
    )
    for stage, keys in (
        ("fix", ["fix-7-abc"]),
        ("fix-ci", ["ci-7-abc", "ci-pr-7"]),
        ("rebase", ["rebase-pr-7"]),
        ("bump", ["bump-7-abc", "bump-pr-7"]),
        ("lint-repair", ["lint-repair-7-abc", "lint-repair-pr-7"]),
        ("progress", ["progress-err"]),
        ("roadmap", []),
    ):
        for raises in (False, True):
            for key in keys:
                values[key] = 3

            def failed_stage(*args, keys=keys, raises=raises):
                for key in keys:
                    values[key] += 1
                tr.record_block("core", time.time() + 100)
                if raises:
                    raise RuntimeError("publishing interrupted")
                return 1

            try:
                work_units._run_github_stage(stage, failed_stage, worker, None, candidate, None, False)
                raise AssertionError("expected a quota pause")
            except NoProgress:
                assert all(values[key] == 3 for key in keys)

        def delayed_quota_exit(*args, keys=keys):
            for key in keys:
                values[key] += 1
            tr.record_block("core", time.time() - 1)  # reset expired while the engine was exiting
            return 1

        try:
            work_units._run_github_stage(stage, delayed_quota_exit, worker, None, candidate, None, False)
            raise AssertionError("expected an expired-reset quota pause")
        except NoProgress:
            assert all(values[key] == 3 for key in keys)

        def successful_stage(*args):
            tr.record_block("core", time.time() + 100)  # cleanup hit a limit after successful work
            return 0

        assert work_units._run_github_stage(stage, successful_stage, worker, None, candidate, None, False) == 0

    # A rebase quota refund belongs to the selected epoch, even if the current
    # epoch changes during execution. Never refund a newer epoch or legacy key.
    epoch_a, epoch_b, legacy = "rebase-7-epoch-a", "rebase-7-epoch-b", "rebase-pr-7"
    for raises in (False, True):
        values.update({epoch_a: 2, epoch_b: 5, legacy: 9})
        candidate.rebase_budget_key = epoch_a

        def epoch_failure(*args, raises=raises):
            values[epoch_a] += 1
            candidate.rebase_budget_key = epoch_b
            tr.record_block("core", time.time() + 100)
            if raises:
                raise RuntimeError("quota interrupted publication")
            return 1

        try:
            work_units._run_github_stage("rebase", epoch_failure, worker, None, candidate, None, False)
            raise AssertionError("expected epoch quota pause")
        except NoProgress:
            assert (values[epoch_a], values[epoch_b], values[legacy]) == (2, 5, 9)

    from tauceti_worker.config import Die

    candidate.rebase_budget_key = ""
    with patch.object(worker.counters, "read") as read:
        with patch.object(work_units, "do_rebase") as launch:
            try:
                work_units._run_github_stage("rebase", launch, worker, None, candidate, None, False)
                raise AssertionError("expected missing-budget rejection")
            except Die:
                read.assert_not_called()
                launch.assert_not_called()

    # Host engine compatibility retains jq's compact/raw output and forces a live ownership read.
    with patch.object(GitHub, "issue_comments", return_value=[{"id": 2, "body": "hello"}]) as comments:
        p = tr.migrated_history(
            ["api", "--paginate", "--jq", ".[] | @json", "/repos/org/repo/issues/7/comments?per_page=100"]
        )
        if p is not None:  # jq is optional; absent jq uses the untouched CLI path.
            assert p.returncode == 0 and json.loads(p.stdout)["id"] == 2
        assert tr.migrated_history(["api", "-X", "POST", "/repos/org/repo/issues/7/comments?per_page=100"]) is None

print("PASS: shared cache, complete GraphQL histories, endpoint accounting and exhaustion")
