"""tauceti_worker.review_state — read the PR scoreboard comment (the multi-agent source of truth)
behind a short-TTL cache, with the predicates the cascade gates on."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only; survey is a higher layer
    from .survey import Counters

from .config import Config
from .constants import COMMENTS_MEMO_S, REVIEW_INPROGRESS_RE, SBCACHE_TTL
from .github import GitHub

# ============================================================================
# ReviewState — the PR scoreboard comment is the multi-agent source of truth; a
# short-TTL local cache fronts it. Ports gh_meta/bust_meta/ledger_*/review_* with
# the stale-but-real fallback (never serve a phantom {} over a real cached value).
# Provenance is tracked so mutating passes can refuse to act on stale data.
# ============================================================================

META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


@dataclass
class Meta:
    data: dict
    provenance: str  # "fresh" | "stale" | "missing" | "fetch_failed"


class ReviewState:
    def __init__(self, cfg: Config, gh: GitHub):
        self.cfg = cfg
        self.gh = gh
        self.sbcache = cfg.sbcache
        self._comments: dict[int, tuple[float, list[dict] | None]] = {}

    def _cache_path(self, pr: int) -> Path:
        return self.sbcache / f"{pr}.json"

    def _issue_comments(self, pr: int) -> list[dict] | None:
        """A PR's issue comments, memoized briefly so the two readers in one survey pass — the scoreboard
        meta and the in-flight review marker — share ONE fetch (a cold meta read plus a marker check
        would otherwise double-read the same paginated endpoint). The window is a few seconds: long
        enough to coalesce within a pass, far short of the round/dashboard cadence, so each pass still
        reads fresh markers (the loop-breaking guarantee). None (a fetch failure) is memoized too, so a
        blip isn't retried twice in one pass."""
        hit = self._comments.get(pr)
        if hit and time.time() - hit[0] < COMMENTS_MEMO_S:
            return hit[1]
        cs = self.gh.issue_comments(pr)
        self._comments[pr] = (time.time(), cs)
        return cs

    def inflight_review(self, pr: int, head: str) -> set[str]:
        """Providers holding this EXACT head via an unexpired in-progress marker — the engine's
        de-contention, read worker-side so a held head is skipped before the engine is launched. Shares
        the memoized comment fetch with gh_meta so the survey reads each PR's comments at most once."""
        return inflight_review_providers(self._issue_comments(pr), head, int(time.time()))

    def gh_meta(self, pr: int) -> Meta:
        """Newest scoreboard's <!--tauceti-meta:v1 {...}--> JSON, identified by the <!--tauceti-scoreboard-->
        marker, with TTL cache.

        We DON'T gate on the comment author's repo association. `author_association` is viewer-dependent:
        a reviewer who is a PRIVATE org member reads as MEMBER to themselves but as CONTRIBUTOR/NONE to an
        outside contributor, so an association filter silently discarded legitimate scoreboards for every
        unprivileged contributor (Bryan's PR #470: a real kim-em scoreboard, four blocking rubrics, that
        his worker treated as "no scoreboard at this head" — so `fix` never ran). This mirrors the live
        merge gate's deliberate no-access-bar policy: the newest marked scoreboard supplies the review
        verdict, while trusted CI still supplies the build, scope, and bump-guard boundaries. A forged
        all-green scoreboard can therefore satisfy the review part of that policy; reviewer trust is
        social rather than enforced by author association. A FETCH FAILURE with a prior cache value →
        serve the stale value (stale-but-real beats a phantom '{}'); a SUCCESSFUL fetch that finds no scoreboard
        returns '{}' even with a cache, so a scoreboard that was deleted/edited away (or a forged one a
        worker briefly cached) can't be served as fresh past the TTL.
        """
        cache = self._cache_path(pr)
        if cache.exists():
            age = time.time() - cache.stat().st_mtime
            if age < SBCACHE_TTL:
                return Meta(self._load(cache), "fresh")

        comments = self._issue_comments(pr)
        fetch_failed = comments is None
        data = None
        if comments:
            # Newest-first, but skip a marker comment whose meta is missing/garbage: a newer empty or
            # malformed <!--tauceti-scoreboard--> marker must not mask an older comment that does carry a
            # valid scoreboard (without the author gate, anyone can post such a masking marker).
            marked = sorted(
                (c for c in comments if "<!--tauceti-scoreboard-->" in (c.get("body") or "")),
                key=lambda c: c.get("updated_at", ""),
                reverse=True,
            )
            for c in marked:
                matches = META_RE.findall(c.get("body") or "")
                if not matches:
                    continue
                try:
                    parsed = json.loads(matches[-1].strip())
                except json.JSONDecodeError:
                    continue
                # Require an object: a newer marker carrying valid JSON that ISN'T a dict (a list, string,
                # number, or null) is not a usable scoreboard — skip to an older marker rather than cache
                # it as fresh (callers do meta.data.get(...), which would crash on a non-dict).
                if isinstance(parsed, dict):
                    data = parsed
                    break

        if data is None:
            # Serve a prior value ONLY on a fetch failure (transient); a successful fetch that parsed no
            # scoreboard means there genuinely isn't one now — don't keep serving a now-absent meta.
            if fetch_failed and cache.exists():
                return Meta(self._load(cache), "stale")
            return Meta({}, "fetch_failed" if fetch_failed else "missing")
        self.sbcache.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data) + "\n")
        return Meta(data, "fresh")

    @staticmethod
    def _load(cache: Path) -> dict:
        try:
            return json.loads(cache.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def bust(self, pr: int) -> None:
        self._cache_path(pr).unlink(missing_ok=True)

    # --- predicates over the meta (the cascade's clean-head / blocking rules) ---
    def ledger_clean_head(self, pr: int) -> str:
        runs = self.gh_meta(pr).data.get("runs") or []
        if runs and all(r.get("verdict") != "error" for r in runs):
            return str(self.gh_meta(pr).data.get("head_sha") or "")
        return ""

    def review_rounds(self, pr: int, counters: Counters) -> int:
        # Prefer full_rounds (full review passes; excludes reply/contest rounds) so an author's
        # back-and-forth never eats the review-round budget; fall back to the raw round count. Guard
        # each against a null/str meta value (an old or malformed scoreboard) rather than coercing
        # it to 0, which would silently reset the budget.
        m = self.gh_meta(pr).data
        total = m.get("full_rounds")
        if not isinstance(total, int):
            total = m.get("round", 0)
        if not isinstance(total, int):
            total = 0
        base = counters.read(f"round-base-{pr}")
        return max(0, total - base)

    def newest_contest_reply(self, pr: int):
        """The newest author CONTEST reply on this PR's rubric threads, or None. A contest reply has
        its `in_reply_to_id` pointing at a thread root carrying a `<!--tauceti-rubric:NAME-->`
        marker. Our own comments are dropped by MARKER, never by author login: a contest answer
        carries `tauceti-reply:` and a root carries `tauceti-rubric:`, so both are skipped, while a
        human contest (even one sharing the worker's login) is never wrongly dropped. "Newest" is by
        the monotonic comment `id` (not the second-resolution timestamp, which can't separate two
        replies in one second). Its creation time is returned for review-affinity aging. Returns
        {'id', 'rubric', 'created_at'} of the newest such reply."""
        rcs = self.gh.review_comments(pr)
        if not rcs:
            return None
        roots = {}
        for c in rcs:
            if c.get("in_reply_to_id") is None:
                mk = re.search(r"tauceti-rubric:([a-z][a-z-]*?)\s*-->", c.get("body") or "")
                if mk:
                    roots[c["id"]] = mk.group(1)
        best = None
        for c in rcs:
            rubric = roots.get(c.get("in_reply_to_id"))
            if not rubric:
                continue
            body = c.get("body") or ""
            if "tauceti-reply:" in body or "tauceti-rubric:" in body:
                continue
            cid = c.get("id") or 0
            if best is None or cid > best["id"]:
                best = {"id": cid, "rubric": rubric, "created_at": c.get("created_at")}
        return best

    def ledger_blocking(self, pr: int, head: str) -> bool:
        # Consult the durable per-rubric `states`, not just the latest round's `runs`. A reply/partial
        # round only re-runs some rubrics, so `runs` can show an approve for one rubric while another
        # rubric is still `blocking_request` in `states` — keying on `runs` would miss it and `fix`
        # would never address the blocking rubric (the PR strands). `states` is the same signal CI's
        # close reads, so the two agree. Falls back to `runs` only for an older scoreboard with no
        # `states` map. A rubric blocks unless it is green or stale (a carried-forward approval).
        m = self.gh_meta(pr).data
        if str(m.get("head_sha") or "") != head:
            return False
        states = m.get("states") or {}
        if states:
            return any(v not in ("green", "stale") for v in states.values())
        runs = m.get("runs") or []
        return any(r.get("verdict") not in ("approve", "error") for r in runs)


def inflight_review_providers(comments: list[dict] | None, head: str, now: int) -> set[str]:
    """Providers named by any UNEXPIRED in-progress review marker on this EXACT head (a new push is a
    new unit, so the head must match). Mirrors the review engine's de-contention read so the worker can
    skip a head a peer already holds before launching the engine. Empty set when none apply; a fetch
    failure (comments is None) is treated as 'not held' and proceeds — fail-open, matching the engine
    (a rare duplicate review at worst, the engine's own claim still being the backstop)."""
    cov: set[str] = set()
    for c in comments or []:
        m = REVIEW_INPROGRESS_RE.search(c.get("body") or "")
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        exp = d.get("expires_at")
        if not isinstance(exp, int) or exp <= now:
            continue
        if d.get("head") != head:  # exact: a new push is a new unit, not covered by an old marker
            continue
        cov.update(p for p in (d.get("providers") or []) if isinstance(p, str))
    return cov
