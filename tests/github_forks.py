#!/usr/bin/env python3
"""Fork intent recovery requires authenticated ownership, ancestry and complete visibility."""

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
from tauceti_worker.github_forks import ForkUnknown, discover


def page(*, fork=True, name="author/renamed", parent="UPSTREAM", permission="WRITE", collision=False):
    node = {
        "id": "FORK",
        "name": name.split("/")[1],
        "nameWithOwner": name,
        "owner": {"login": "author"},
        "isFork": True,
        "isArchived": False,
        "isLocked": False,
        "viewerPermission": permission,
        "parent": {"id": parent, "nameWithOwner": "org/repo" if parent == "UPSTREAM" else "other/repo", "parent": None},
    }
    return [
        {
            "data": {
                # The shape GitHub returns: every repository the actor owns. A collision is a
                # same-named repository in the listing; a `viewer.repository(name:)` lookup is not
                # used because for a missing name GitHub answers with an error, not null.
                "viewer": {
                    "login": "author",
                    "repositories": {
                        "nodes": ([node] if fork else []) + ([{
                            "id": "COLLISION",
                            "name": "repo",
                            "nameWithOwner": "author/repo",
                            "owner": {"login": "author"},
                            "isFork": False,
                            "isArchived": False,
                            "isLocked": False,
                            "viewerPermission": "ADMIN",
                            "parent": None,
                        }] if collision else []),
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
                "repository": {"id": "UPSTREAM", "nameWithOwner": "org/repo", "isPrivate": False},
            }
        }
    ]


def result(data):
    return subprocess.CompletedProcess([], 0, json.dumps(data), "")


for name in ("author/repo", "author/renamed"):
    found = discover(lambda _, name=name: json.dumps(page(name=name)), "org/repo")
    assert found["fork"] == name and found["actor"] == "author" and found["upstream_id"] == "UPSTREAM"
assert discover(lambda _: json.dumps(page(fork=False)), "org/repo")["absent"] is True
assert discover(lambda _: json.dumps(page(fork=False, collision=True)), "org/repo")["absent"] is False
assert discover(lambda _: json.dumps(page(parent="OTHER", collision=True)), "org/repo")["fork"] is None
# Fork-of-fork ancestry is accepted by exact immutable repository ID.
nested = page()
node = nested[0]["data"]["viewer"]["repositories"]["nodes"][0]
node["parent"] = {"id": "INTERMEDIATE", "nameWithOwner": "peer/repo", "parent": node["parent"]}
assert discover(lambda _: json.dumps(nested), "org/repo")["fork"] == "author/renamed"
for change in ("permission", "archived", "locked", "errors", "truncated", "owner", "ancestry", "upstream-id"):
    data = page()
    node = data[0]["data"]["viewer"]["repositories"]["nodes"][0]
    if change == "permission":
        node["viewerPermission"] = "READ"
    elif change == "archived":
        node["isArchived"] = True
    elif change == "locked":
        node["isLocked"] = True
    elif change == "errors":
        data[0]["errors"] = [{"message": "partial outage"}]
    elif change == "truncated":
        data[0]["data"]["viewer"]["repositories"]["pageInfo"]["hasNextPage"] = True
    elif change == "owner":
        node["owner"]["login"] = "stranger"
    elif change == "ancestry":
        node["parent"] = {"id": "OTHER", "nameWithOwner": "other/repo"}
    else:
        node["parent"]["id"] = "REPLACED"
        del node["parent"]["parent"]
    try:
        discover(lambda _, data=data: json.dumps(data), "org/repo")
    except ForkUnknown:
        pass
    else:
        raise AssertionError(change)

# A deep unrelated fork does not hide a fully verified candidate.
deep = page()
deep[0]["data"]["viewer"]["repositories"]["nodes"].insert(
    0,
    {
        "id": "DEEP",
        "name": "deep",
        "nameWithOwner": "author/deep",
        "owner": {"login": "author"},
        "isFork": True,
        "isArchived": False,
        "isLocked": False,
        "viewerPermission": "ADMIN",
        "parent": {"id": "OTHER", "nameWithOwner": "other/repo"},
    },
)
assert discover(lambda _: json.dumps(deep), "org/repo")["fork"] == "author/renamed"

with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "TAUCETI_GITHUB_SHARED_DIR": tmp,
            "TAUCETI_WORKER_ID": "worker",
            "GH_TOKEN": "fork-test",
            "TAUCETI_GITHUB_ACCOUNT": "author",
        },
    ),
):
    env = dict(os.environ)
    argv = ["gh", "repo", "fork", "org/repo", "--clone=false"]
    with patch.object(tr, "execute", return_value=result(page(fork=False))) as read:
        path = outbox.prepare(argv, env, tmp)
        assert outbox.prepare(argv, env, tmp) == path and read.call_count == 1, "coalesce targeted discovery"
    value = json.loads(path.read_text())
    assert value["context"] == {
        "repository": None,
        "pr": None,
        "operation": "fork",
        "actor": "author",
        "upstream": "org/repo",
        "upstream_id": "UPSTREAM",
    }
    outbox.finish(path, subprocess.CompletedProcess(argv, 1, "", "lost response"))
    tr.invalidate()
    with patch.object(tr, "execute", return_value=result(page())) as read:
        assert outbox.drain(env) == []
        assert not path.exists() and path.with_suffix(".complete").exists()
        assert read.call_count == 1 and read.call_args.args[0][1:3] == ["api", "graphql"]
        assert outbox.drain(env) == [] and read.call_count == 1

    # Legacy invalid CLI intents are migrated with account evidence, never blindly replayed.
    path = outbox.directory(env) / "legacy.json"
    legacy = {
        "argv": ["repo", "fork", "org/repo", "--clone=false", "--remote=false"],
        "cwd": tmp,
        "status": "ambiguous",
        "created": time.time() - 1000,
    }
    tr.atomic(path, legacy)
    with patch.object(tr, "execute") as read:
        assert outbox.prepare(argv, env, tmp) == path, "R0 argv correction must find the ambiguous legacy intent"
        assert outbox.semantic_key(legacy) == outbox.semantic_key({**legacy, "argv": argv[1:]})
    with patch.object(tr, "execute") as read:
        assert outbox.drain(env) == [] and not path.exists()  # coalesced verified fork
        read.assert_not_called()
    for data in (page(parent="OTHER", collision=True), page(permission="READ"), page(fork=False)):
        tr.invalidate()
        tr.atomic(path, legacy)
        with patch.object(tr, "execute", return_value=result(data)) as read:
            assert outbox.drain(env) == [path.stem] and path.exists()
            assert read.call_count == 1 and read.call_args.args[0][1] == "api"
        path.unlink()
    # Read outage and identity changes never clear or replay the retained write.
    for data in (result({"errors": [{"message": "outage"}]}), subprocess.CompletedProcess([], 1, "", "timeout")):
        tr.invalidate()
        tr.atomic(path, legacy)
        with patch.object(tr, "execute", return_value=data) as read:
            assert outbox.drain(env) == [path.stem] and read.call_count == 1
        path.unlink()
    tr.invalidate()
    tr.atomic(path, {**legacy, "context": {"actor": "another"}})
    with patch.object(tr, "execute", return_value=result(page())):
        assert outbox.drain(env) == [path.stem]
    path.unlink()
    # Slow visibility must pass separated complete absence observations; one replay only.
    tr.invalidate()
    tr.atomic(path, legacy)
    with patch.object(tr, "execute", return_value=result(page(fork=False))) as read:
        assert outbox.drain(env) == [path.stem] and read.call_count == 1
    value = json.loads(path.read_text())
    stale = {
        "actor": "author",
        "upstream_id": "UPSTREAM",
        "fork": None,
        "absent": True,
        "observed_at": time.time() - 60,
    }
    with patch.object(outbox, "fork_read", return_value=stale):
        check = {**value, "fork_absent_since": time.time() - 301}
        assert outbox.reconcile(check, env) is None, "cached observations must be separated by the full grace period"
    value.update(fork_absent_since=time.time() - 301, retry_at=0)
    tr.atomic(path, value)
    rejected = subprocess.CompletedProcess([], 1, "", "API rate limit exceeded")
    rejected.github_before_request = True
    with patch.object(tr, "execute", return_value=rejected) as write:
        assert outbox.drain(env) == [path.stem] and write.call_count == 1
    value = json.loads(path.read_text())
    assert value["fork_retries"] == 0 and value["status"] == "rejected", "no request must not spend replay allowance"
    value.update(updated=time.time() - 301, retry_at=0)
    tr.atomic(path, value)
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 1, "", "lost response")) as write:
        assert outbox.drain(env) == [path.stem] and write.call_count == 1
        assert write.call_args.args[0] == argv, "legacy retry uses R0's supported CLI form"
    value = json.loads(path.read_text())
    assert value["fork_retries"] == 1
    value.update(updated=time.time() - 301, retry_at=0)
    tr.atomic(path, value)
    with patch.object(tr, "execute") as write:
        assert outbox.drain(env) == [path.stem]
        write.assert_not_called()
    # Malformed legacy evidence must not hide another PR's repair either.
    broken = outbox.directory(env) / "malformed.json"
    tr.atomic(broken, ["legacy-corrupt"])
    # A different PR's repair can still publish despite this uncertain fork.
    comment = outbox.prepare(["gh", "pr", "comment", "7", "--repo", "org/repo", "--body", "saved"], env, tmp)
    with patch.object(tr, "execute", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as write:
        assert set(outbox.drain(env)) == {path.stem, broken.stem} and write.call_count == 1 and not comment.exists()
    assert path.exists() and broken.exists()

print("PASS: exact fork ancestry, ownership, visibility, legacy migration and bounded reconciliation")

# A bare PR head refers to the canonical repository's owner, not an unknown fork.
value = {
    "argv": ["pr", "create", "--repo", "org/repo", "--head", "progress/window/Area"],
    "context": {"repository": "org/repo", "head": "progress/window/Area"},
}
for nodes, expected in (
    ([{"url": "https://example.invalid/pr/7", "headRepositoryOwner": {"login": "org"}}], True),
    ([], False),
):
    data = {"data": {"repository": {"pullRequests": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}}
    with patch.object(tr, "execute", return_value=result(data)):
        assert outbox.reconcile(value, {}) is expected
