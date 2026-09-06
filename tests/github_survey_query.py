#!/usr/bin/env python3
"""Narrow survey queries retain eligibility and never return incomplete pages."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.github import GitHub, GitHubError
from tauceti_worker.survey import PRInfo

fields = ["number", "headRefOid", "author", "labels", "buildStatus"]
row = {
    "number": 1,
    "headRefOid": "abc",
    "author": {"login": "bot", "__typename": "Bot"},
    "labels": {"nodes": [{"name": "awaiting-review"}], "pageInfo": {"hasNextPage": False}},
    "commits": {
        "nodes": [
            {
                "commit": {
                    "oid": "abc",
                    "status": {
                        "context": {"context": "build", "state": "SUCCESS", "createdAt": "2026-09-06T00:00:00Z"}
                    },
                }
            }
        ]
    },
}


def page(rows, more=False, cursor=None):
    return subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {"nodes": rows, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
                    }
                }
            }
        ),
        "",
    )


gh = GitHub("owner/repo")
with patch.object(gh, "_gh", side_effect=[page([row], True, "next"), page([row])]) as call:
    result = gh.pr_list(fields)
    assert len(result) == 2 and call.call_count == 2
    query = " ".join(call.call_args_list[0].args[0])
    assert "first:25" in query and 'context(name:"build")' in query
    assert "statusCheckRollup" not in query and "checkRuns" not in query
    assert "cursor=next" in call.call_args_list[1].args[0]
info = PRInfo.from_json(result[0])
assert info.build_success and info.author_is_bot and info.build_status_at
legacy = {**result[0], "statusCheckRollup": [{**result[0]["buildStatus"], "startedAt": "2026-09-06T00:00:00Z"}]}
legacy.pop("buildStatus")
assert PRInfo.from_json(legacy) == info
for state in ("FAILURE", "PENDING", None):
    variant = copy.deepcopy(row)
    variant["commits"]["nodes"][0]["commit"]["status"] = {
        "context": None if state is None else {"context": "build", "state": state, "createdAt": "2026-09-06T00:00:00Z"}
    }
    with patch.object(gh, "_gh", return_value=page([variant])):
        info = PRInfo.from_json(gh.pr_list(fields)[0])
    assert info.build_success is False
    assert info.build_failed == (state == "FAILURE")

for status in (502, 503, 504):
    bad = subprocess.CompletedProcess([], 1, "", f"HTTP {status}: unavailable")
    with (
        patch.object(gh, "_gh", side_effect=[bad, bad, page([row])]) as call,
        patch("tauceti_worker.github.time.sleep"),
    ):
        assert len(gh.pr_list(fields)) == 1 and call.call_count == 3
    with patch.object(gh, "_gh", return_value=bad) as call, patch("tauceti_worker.github.time.sleep"):
        try:
            gh.pr_list(fields)
        except GitHubError as exc:
            assert str(status) in str(exc) and call.call_count == 3
        else:
            raise AssertionError("retry budget ignored")

bad = subprocess.CompletedProcess([], 1, "", "HTTP 401: unauthorized")
with patch.object(gh, "_gh", side_effect=[page([row], True, "next"), bad]) as call:
    try:
        gh.pr_list(fields)
    except GitHubError as exc:
        assert "page=2" in str(exc) and call.call_count == 2
    else:
        raise AssertionError("partial result accepted")

for invalid in (
    subprocess.CompletedProcess([], 0, '{"data":null,"errors":[{"message":"resource limit"}]}', ""),
    page([{**row, "headRefOid": "different"}]),
    page([row], True, None),
):
    with patch.object(gh, "_gh", return_value=invalid):
        try:
            gh.pr_list(fields)
        except GitHubError:
            pass
        else:
            raise AssertionError("incomplete response accepted")
print("PASS: narrow query, pagination, build eligibility, bot identity, transient retries and fail-closed responses")
