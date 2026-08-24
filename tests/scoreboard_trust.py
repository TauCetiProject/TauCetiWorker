#!/usr/bin/env python3
"""gh_meta must accept a scoreboard regardless of the comment author's repo association.

`author_association` is viewer-dependent: a reviewer who is a PRIVATE org member reads as MEMBER to
themselves but as CONTRIBUTOR (or NONE) to an outside contributor. The old trust filter
({OWNER, MEMBER, COLLABORATOR}) therefore silently discarded legitimate scoreboards for every
unprivileged contributor — Bryan's PR #470 had a real kim-em scoreboard with four blocking rubrics that
his worker treated as "no scoreboard at this head", so `fix` never ran. gh_meta now identifies the
scoreboard by the <!--tauceti-scoreboard--> marker alone and parses the newest one's meta. This mirrors
the live merge gate's deliberate no-access-bar policy: the scoreboard supplies review state, while
trusted CI independently supplies build, scope, and bump-guard status.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def sb(head, updated, assoc="CONTRIBUTOR"):
    """A scoreboard comment: the marker + a tauceti-meta block, authored with `assoc` association."""
    meta = json.dumps({"head_sha": head, "states": {"reuse": "blocking_request"}})
    return {
        "body": f"<!--tauceti-scoreboard-->\nscores here\n<!--tauceti-meta:v1 {meta}-->",
        "updated_at": updated,
        "author_association": assoc,
    }


def plain(updated, assoc="MEMBER"):
    """A non-scoreboard comment (no marker) — must be ignored even from a 'trusted' author."""
    meta = json.dumps({"head_sha": "FORGED"})
    return {"body": f"just chatting <!--tauceti-meta:v1 {meta}-->", "updated_at": updated, "author_association": assoc}


def empty_marker(updated, garbage=False):
    """A scoreboard MARKER with no valid meta — a newer one of these must not mask an older real board."""
    tail = "<!--tauceti-meta:v1 {not json}-->" if garbage else ""
    return {"body": f"<!--tauceti-scoreboard--> {tail}", "updated_at": updated, "author_association": "MEMBER"}


def nondict_marker(updated, literal="[]"):
    """A scoreboard marker whose meta is valid JSON but NOT an object (list/string/number/null) — not a
    usable scoreboard, and must never be cached (callers call meta.data.get(...))."""
    return {"body": f"<!--tauceti-scoreboard--> <!--tauceti-meta:v1 {literal}-->", "updated_at": updated}


def make_rs(comments):
    cfg = types.SimpleNamespace(sbcache=Path(tempfile.mkdtemp()) / "sb")
    gh = types.SimpleNamespace(issue_comments=lambda pr: comments)
    return tc.ReviewState(cfg, gh)


# 1) A scoreboard from a CONTRIBUTOR (the outside-contributor view of a private member) is trusted.
m = make_rs([sb("HEAD1", "2026-06-26T13:49:44Z", assoc="CONTRIBUTOR")]).gh_meta(470)
check("CONTRIBUTOR-authored scoreboard is parsed", m.data.get("head_sha") == "HEAD1")
check("CONTRIBUTOR-authored scoreboard is fresh", m.provenance == "fresh")

# 2) Even a NONE-association author is trusted (we deliberately don't gate on association anymore).
m = make_rs([sb("HEAD2", "2026-06-26T13:49:44Z", assoc="NONE")]).gh_meta(471)
check("NONE-authored scoreboard is parsed", m.data.get("head_sha") == "HEAD2")

# 3) Newest marked scoreboard (by updated_at) wins, regardless of author association ordering.
m = make_rs(
    [
        sb("OLD", "2026-06-26T10:00:00Z", assoc="MEMBER"),
        sb("NEW", "2026-06-26T14:00:00Z", assoc="CONTRIBUTOR"),
    ]
).gh_meta(472)
check("newest marked scoreboard wins", m.data.get("head_sha") == "NEW")

# 4) A comment WITHOUT the scoreboard marker is ignored even if it carries a meta block and a MEMBER author.
m = make_rs([plain("2026-06-26T15:00:00Z", assoc="MEMBER")]).gh_meta(473)
check("non-marker comment is not treated as a scoreboard", m.data == {} and m.provenance == "missing")

# 4b) ...and a real scoreboard alongside a (newer) non-marker comment still wins.
m = make_rs([sb("REAL", "2026-06-26T12:00:00Z"), plain("2026-06-26T16:00:00Z")]).gh_meta(474)
check("marker comment used despite a newer plain comment", m.data.get("head_sha") == "REAL")

# 5) No scoreboard at all -> empty/missing.
m = make_rs([plain("2026-06-26T09:00:00Z")]).gh_meta(475)
check("no scoreboard -> missing", m.data == {} and m.provenance == "missing")

# 6) Fetch failure (issue_comments None) with no cache -> fetch_failed sentinel.
m = make_rs(None).gh_meta(476)
check("fetch failure -> fetch_failed", m.data == {} and m.provenance == "fetch_failed")

# 7) A NEWER bare/garbage marker must not mask an OLDER comment that carries a valid scoreboard.
m = make_rs([sb("REALHEAD", "2026-06-26T12:00:00Z"), empty_marker("2026-06-26T18:00:00Z")]).gh_meta(477)
check("newer empty marker doesn't mask older valid scoreboard", m.data.get("head_sha") == "REALHEAD")
m = make_rs([sb("REALHEAD", "2026-06-26T12:00:00Z"), empty_marker("2026-06-26T18:00:00Z", garbage=True)]).gh_meta(478)
check("newer garbage-meta marker doesn't mask older valid scoreboard", m.data.get("head_sha") == "REALHEAD")
# valid JSON but non-dict meta (list / null) is not a usable scoreboard: skip to the older real one...
for lit in ("[]", "null", '"x"', "123"):
    m = make_rs([sb("REALHEAD", "2026-06-26T12:00:00Z"), nondict_marker("2026-06-26T18:00:00Z", lit)]).gh_meta(480)
    check(f"non-dict meta {lit!r} doesn't mask older valid scoreboard", m.data.get("head_sha") == "REALHEAD")
# ...and a lone non-dict marker yields a safe empty dict (provenance missing), never a non-dict .data
m = make_rs([nondict_marker("2026-06-26T18:00:00Z", "[]")]).gh_meta(481)
check("lone non-dict meta -> missing with dict data", m.data == {} and m.provenance == "missing")

# 8) Cache poisoning: once a scoreboard is cached, a later SUCCESSFUL fetch that finds none returns
#    'missing' (not the cached value) past the TTL — a forged-then-deleted board can't linger.
cfg = types.SimpleNamespace(sbcache=Path(tempfile.mkdtemp()) / "sb")
box = {"comments": [sb("CACHED", "2026-06-26T12:00:00Z")]}
rs = tc.ReviewState(cfg, types.SimpleNamespace(issue_comments=lambda pr: box["comments"]))
cache_file = cfg.sbcache / "479.json"
old = time.time() - (tc.SBCACHE_TTL + 5)


def reread():  # age the cache past the TTL and drop the per-pass comment memo, then re-read
    if cache_file.exists():
        os.utime(cache_file, (old, old))
    rs._comments.clear()
    return rs.gh_meta(479)


check("scoreboard cached on first read", rs.gh_meta(479).data.get("head_sha") == "CACHED")
box["comments"] = [plain("2026-06-26T20:00:00Z")]  # the board is now gone from the PR
after = reread()  # successful fetch, no board
check("deleted scoreboard not served from cache past TTL", after.data == {} and after.provenance == "missing")
# a FETCH FAILURE still serves the stale cache (transient — don't lose real state on a blip)
cache_file.write_text(json.dumps({"head_sha": "CACHED"}))  # missing-path above didn't rewrite it
box["comments"] = None
stale = reread()
check("fetch failure still serves stale cache", stale.data.get("head_sha") == "CACHED" and stale.provenance == "stale")

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
