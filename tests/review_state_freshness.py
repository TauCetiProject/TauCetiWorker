#!/usr/bin/env python3
"""A PR that has not changed is not re-read.

The survey used to fetch every build-green PR's comments every round: 150 REST calls and ~45s of a
47s survey, growing with the number of open PRs rather than with how many of them moved. ReviewState
now keys those reads on each PR's `updatedAt`, which the survey's own list query returns for free, so
a round pays for the handful of PRs that changed and nothing for the rest.

`updatedAt` is a strong signal, not a promise: a DELETED comment moves no clock. This harness pins
both halves of that — the saving when nothing changed, and the bounded wrongness when something
changed invisibly — plus the rule that a cached read is `assumed`, never `fresh`, so it can never be
what authorizes a mutation. No network: a fake GitHub counts fetches over a temp cache.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


def board(head="abc123"):
    """A scoreboard comment shaped exactly like the engine's: the marker, then the meta blob."""
    return {"body": "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + json.dumps({"head_sha": head}) + "-->"}


def marker(head, provider, expires_at):
    return {
        "body": "<!--tauceti-review-in-progress "
        + json.dumps({"head": head, "providers": [provider], "expires_at": expires_at})
        + "-->"
    }


def contest(cid=7, root=3):
    return [
        {"id": root, "in_reply_to_id": None, "body": "<!--tauceti-rubric:reuse-->"},
        {"id": cid, "in_reply_to_id": root, "body": "this finding is wrong", "created_at": "2026-09-17T00:00:00Z"},
    ]


class FakeGH:
    """Counts each endpoint read; `issue` / `review` are swapped out between reads to simulate the PR
    changing (or a fetch failing, as None)."""

    def __init__(self, issue=None, review=None):
        self.issue, self.review = issue, review
        self.n_issue = self.n_review = 0

    def issue_comments(self, pr):
        self.n_issue += 1
        return self.issue

    def review_comments(self, pr):
        self.n_review += 1
        return self.review


def state(gh, tmp):
    cfg = SimpleNamespace(sbcache=Path(tmp) / "scoreboard")
    rs = tc.ReviewState(cfg, gh)
    rs.sbcache = cfg.sbcache
    return rs


def pr(number=1, updated_at="2026-09-17T01:00:00Z"):
    return SimpleNamespace(number=number, updated_at=updated_at)


# --- the saving: an unmoved PR costs nothing ------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")])
    rs = state(gh, tmp)
    rs.observe([pr()])
    first = rs.gh_meta(1)
    check(
        "a cold read goes to GitHub", (first.data.get("head_sha"), first.provenance, gh.n_issue), ("abc123", "fresh", 1)
    )
    rs._comments.clear()  # drop the in-pass memo: only the sidecar can answer now
    again = rs.gh_meta(1)
    check("an unmoved PR is not re-read", (again.data.get("head_sha"), gh.n_issue), ("abc123", 1))
    check("...and says so: assumed, not fresh", again.provenance, "assumed")

    gh.issue = [board("def456")]
    rs.observe([pr(updated_at="2026-09-17T02:00:00Z")])  # the PR moved
    moved = rs.gh_meta(1)
    check("a moved PR is re-read", (moved.data.get("head_sha"), gh.n_issue), ("def456", 2))

# --- the backstop: an invisible change is bounded, not permanent -----------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")])
    rs = state(gh, tmp)
    rs.observe([pr()])
    rs.gh_meta(1)
    rs._comments.clear()
    gh.issue = []  # the scoreboard was DELETED, which moves no clock at all
    check("a deleted board is still served while the backstop holds", rs.gh_meta(1).data.get("head_sha"), "abc123")
    check("...as assumed, so it cannot authorize a mutation", rs.gh_meta(1).provenance, "assumed")
    sc = json.loads((rs.sbcache / "1.key.json").read_text())
    (rs.sbcache / "1.key.json").write_text(json.dumps({**sc, "fetched_at": time.time() - tc.SBCACHE_BACKSTOP_S - 1}))
    check("...and the backstop expires it", (rs.gh_meta(1).data, rs.gh_meta(1).provenance), ({}, "missing"))

# --- absence is a real answer, and stays one -------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[{"body": "just a comment"}])
    rs = state(gh, tmp)
    rs.observe([pr()])
    check("no scoreboard reads as missing", (rs.gh_meta(1).data, rs.gh_meta(1).provenance), ({}, "missing"))
    rs._comments.clear()
    check("a cached absence is not re-read", (rs.gh_meta(1).provenance, gh.n_issue), ("missing", 1))

# --- a fetch failure never becomes a cached answer -------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")])
    rs = state(gh, tmp)
    rs.observe([pr()])
    rs.gh_meta(1)
    rs.observe([pr(updated_at="2026-09-17T03:00:00Z")])  # moved, so the sidecar no longer answers
    rs._comments.clear()
    gh.issue = None  # ...and the refresh fails
    failed = rs.gh_meta(1)
    check(
        "a failed refresh serves the prior value as stale",
        (failed.data.get("head_sha"), failed.provenance),
        ("abc123", "stale"),
    )
    check(
        "a failed refresh writes no key",
        json.loads((rs.sbcache / "1.key.json").read_text())["updated_at"],
        "2026-09-17T01:00:00Z",
    )

# --- an unobserved PR keeps the old TTL behaviour exactly -------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")])
    rs = state(gh, tmp)  # never observed: no clock to compare
    rs.gh_meta(1)
    rs._comments.clear()
    check("an unobserved PR uses the plain TTL", (rs.gh_meta(1).provenance, gh.n_issue), ("fresh", 1))
    rs2 = state(gh, tmp)
    rs2.observe([SimpleNamespace(number=1, updated_at="")])  # a blank clock is not a key
    rs2._comments.clear()
    check("a blank updated_at is unobserved, not a match", rs2.gh_meta(1).provenance, "fresh")

# --- markers: cached, but expiry is still judged against the clock ---------------------------------
with tempfile.TemporaryDirectory() as tmp:
    soon = int(time.time()) + 60
    gh = FakeGH(issue=[board("abc123"), marker("abc123", "codex", soon)])
    rs = state(gh, tmp)
    rs.observe([pr()])
    rs.gh_meta(1)  # the same fetch that reads the board distils the markers
    rs._comments.clear()
    check("a cached marker still holds the head", rs.inflight_review(1, "abc123"), {"codex"})
    check("...without a second fetch", gh.n_issue, 1)
    check("a cached marker does not hold a different head", rs.inflight_review(1, "def456"), set())
    sc = json.loads((rs.sbcache / "1.key.json").read_text())
    sc["markers"] = [{**sc["markers"][0], "expires_at": int(time.time()) - 1}]
    (rs.sbcache / "1.key.json").write_text(json.dumps(sc))
    check("an expired cached marker releases the head on its own", rs.inflight_review(1, "abc123"), set())

# --- the contest reply gets the same treatment (it had no cache at all) ----------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")], review=contest())
    rs = state(gh, tmp)
    rs.observe([pr()])
    check("the newest contest reply is found", rs.newest_contest_reply(1)["id"], 7)
    check("an unmoved PR does not re-read review comments", (rs.newest_contest_reply(1)["id"], gh.n_review), (7, 1))
    gh.review = None  # a fetch failure must not be cached as "no contest"
    rs.observe([pr(updated_at="2026-09-17T04:00:00Z")])
    check("a failed review-comment fetch answers None", rs.newest_contest_reply(1), None)
    check(
        "...and is not cached",
        (rs.sbcache / "1.contest.json").exists()
        and json.loads((rs.sbcache / "1.contest.json").read_text())["updated_at"],
        "2026-09-17T01:00:00Z",
    )

# --- bust forgets everything, including the in-memory memo ------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    gh = FakeGH(issue=[board("abc123")], review=contest())
    rs = state(gh, tmp)
    rs.observe([pr()])
    rs.gh_meta(1)
    rs.newest_contest_reply(1)
    gh.issue = [board("def456")]  # what the worker itself just posted
    rs.bust(1)
    check("bust drops the meta file", (rs.sbcache / "1.json").exists(), False)
    check(
        "bust drops both sidecars",
        ((rs.sbcache / "1.key.json").exists(), (rs.sbcache / "1.contest.json").exists()),
        (False, False),
    )
    after = rs.gh_meta(1)
    check("bust drops the in-pass memo, so the re-read sees the new board", after.data.get("head_sha"), "def456")
    # dispatch()'s pre-launch revalidation is exactly bust() + this read, and it refuses anything that
    # is not a live answer — so the FIRST read after a bust has to be `fresh`.
    check("the first read after bust is fresh, which is what may authorize work", after.provenance, "fresh")
    check("a repeat read in the same pass is assumed again", rs.gh_meta(1).provenance, "assumed")

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
