#!/usr/bin/env python3
"""Adopted GitHub authors' PRs get repair rounds when we can push to them, and are never ours to close."""

import importlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
s = importlib.import_module("tauceti_worker.survey")
ident = importlib.import_module("tauceti_worker.source_identity")
HEAD = "a" * 40


def raw(**changes):
    return (
        dict(
            number=7,
            headRefOid=HEAD,
            headRefName="feature",
            headRepositoryOwner={"login": "mo271"},
            headRepository={"name": "TauCeti"},
            author={"login": "mo271", "is_bot": False},
            mergeable="CONFLICTING",
            labels=[{"name": "awaiting-author"}],
            statusCheckRollup=[{"context": "build", "state": "FAILURE"}],
        )
        | changes
    )


def classify(record, stages, *, adopted="Mo271", push=None):
    push = push or {}
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(os.environ, {ident.GITHUB_ADOPTED_AUTHORS_ENV: adopted}),
        patch.object(s, "me", return_value="me"),
        patch.object(s, "can_push", side_effect=lambda repo: push.get(repo, False)),
    ):
        (Path(directory) / "ledger.json").write_text(
            json.dumps({"prs": {"7": {"rounds": [{"ts": datetime.now(UTC).isoformat()}]}}})
        )
        return s.survey(
            SimpleNamespace(wid="test", store_dir=Path(directory), state=Path(directory)),
            SimpleNamespace(open_prs=lambda: [record], fresh_claim_age=lambda _: None),
            SimpleNamespace(observe=lambda prs: None),
            SimpleNamespace(read=lambda _: 0),
            stages=stages,
        )


canonical = {"TauCetiProject/TauCeti": True}
fork = {"mo271/TauCeti": True}

# Rebase and red-build repair, through maintainer edits (push to TauCeti) or direct fork access.
for push in (canonical, fork):
    assert classify(raw(), ["rebase"], push=push).rebaseable.count == 1, push
    assert classify(raw(mergeable="MERGEABLE"), ["fix-ci"], push=push).red_ci.count == 1, push
# No push access, or not listed: nothing to do.
assert classify(raw(), ["rebase"]).rebaseable.count == 0
assert classify(raw(), ["rebase"], adopted="", push=canonical).rebaseable.count == 0
assert classify(raw(author={"login": "someone"}), ["rebase"], push=canonical).rebaseable.count == 0
# Holds still pause adopted PRs.
held = raw(labels=[{"name": "keep"}])
assert classify(held, ["rebase"], push=canonical).rebaseable.count == 0
# Adopted PRs are not "mine": no authoring pressure, no close authority.
sv = classify(raw(), ["rebase"], push=canonical)
pr = sv.open_prs[0]
assert not sv.owns(pr) and not sv.authored(pr) and sv.n_mine_open == 0
assert not s.owns_pr(sv, pr, casefold_legacy=True)

# Parsing: logins are case-folded; anything else stops the worker at startup.
with patch.dict(os.environ, {ident.GITHUB_ADOPTED_AUTHORS_ENV: " mo271, Some-User "}):
    assert ident.github_adopted_authors() == frozenset({"mo271", "some-user"})
for bad in ("mo271;rm", "-lead", "a" * 40, "user/name"):
    with patch.dict(os.environ, {ident.GITHUB_ADOPTED_AUTHORS_ENV: bad}):
        try:
            ident.github_adopted_authors()
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
print("ok")
