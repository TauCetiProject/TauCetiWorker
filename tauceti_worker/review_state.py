"""tauceti_worker.review_state — read the PR scoreboard comment (the multi-agent source of truth)
behind a short-TTL cache, with the predicates the cascade gates on."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .github_state import enabled as github_state_enabled

if TYPE_CHECKING:  # annotation only; survey is a higher layer
    from .survey import Counters

from .config import Config
from .constants import COMMENTS_MEMO_S, SBCACHE_BACKSTOP_S, SBCACHE_TTL
from .github import GitHub
from .github_transport import cache_hit, invalidate, read_context
from .survey_facts import newest_contest, scoreboard

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
    # "fresh"       — read from GitHub during this pass
    # "assumed"     — served from cache because the PR's `updatedAt` has not moved (see observe)
    # "stale"       — a fetch failed and a prior value was served instead
    # "missing"     — a successful read found no scoreboard
    # "fetch_failed"— a fetch failed with nothing cached to fall back on
    # Only `fresh` may authorize a mutation. `assumed` is deliberately NOT `fresh`: the `updatedAt`
    # rule cannot see a DELETED comment, so a board that was deleted or forged could be served under
    # it for up to the backstop. Triage tolerates that; spending money on it does not.
    provenance: str


class ReviewState:
    def __init__(self, cfg: Config, gh: GitHub):
        self.cfg = cfg
        self.gh = gh
        self._direct_source = getattr(gh, "direct_source", False) is True
        self.sbcache = cfg.sbcache
        if self._direct_source:
            # Numeric PRs, logins and worker IDs are not repository/credential
            # boundaries. Recovered sidecars must remain in the source scope.
            if not re.fullmatch(r"[0-9a-f]{64}", gh.cache_namespace):
                raise ValueError("direct source cache namespace must be a qualified identity digest")
            self.sbcache = self.sbcache / "sources" / gh.cache_namespace
        self._comments: dict[int, tuple[float, list[dict] | None]] = {}
        self._observed: dict[int, str] = {}
        self._shared_issue_hit: dict[int, bool] = {}
        self._survey_facts: dict[int, dict] = {}

    def seed_survey(self, rows=()):
        """Facts last only for this pass. Force reads and post-write busts bypass them."""
        if self._direct_source:
            # Existing survey facts are GitHub-service records with no qualified
            # source/credential provenance. They cannot entitle this reader.
            self._survey_facts = {}
            return
        self._survey_facts = {row["pr"]["number"]: row["review"] for row in rows}

    def observe(self, prs) -> None:
        """Record each PR's `updatedAt` from the survey's own list query, as the freshness key for the
        comment reads below.

        A PR's comments cannot change without GitHub moving this clock, so an unmoved clock means a
        cached read is still the right answer and the round can skip the fetch. That is what takes the
        survey's per-PR reads off the SIZE of the project and onto its rate of CHANGE: of ~100 open PRs,
        a handful move between rounds and the rest are free.

        REPLACES rather than merges, so the map always describes the pass that is running: a PR that has
        left the open list must go back to being unobserved rather than keep an answer from a previous
        pass. An empty `updated_at` is not recorded — "no clock" has to read as "cannot tell", which
        falls back to the plain TTL, not as a key that might match another blank."""
        seen = {p.number: p.updated_at for p in prs if getattr(p, "updated_at", "")}
        # Drop memoized comments for any PR whose clock moved. The memo exists to coalesce the two
        # readers inside ONE pass; carried across a change it would hand a stale response to the fetch
        # that a moved clock just forced, and that response would then be written under the NEW key and
        # entitled for the whole backstop — the one way a wrong answer could outlive the thing that
        # should have corrected it.
        for number, key in list(self._observed.items()):
            if seen.get(number) != key:
                self._comments.pop(number, None)
        self._observed = seen

    def _cache_path(self, pr: int) -> Path:
        return self.sbcache / f"{pr}.json"

    def _key_path(self, pr: int) -> Path:
        return self.sbcache / f"{pr}.key.json"

    def _contest_path(self, pr: int) -> Path:
        return self.sbcache / f"{pr}.contest.json"

    def _sidecar(self, path: Path, pr: int) -> dict | None:
        """A sidecar that is still ENTITLED to answer for `pr`: same `updatedAt` as this pass observed,
        and inside the backstop. None when it cannot answer, whatever the reason.

        The record is SELF-CONTAINED: it carries the payload it is entitled to serve, not a pointer to
        `<pr>.json`. Two files cannot be replaced as one, so a key paired with a payload written by a
        different process at a different `updatedAt` would entitle data nobody ever read together.
        `<pr>.json` still exists, written after, for the stale-on-fetch-failure path and for an operator
        reading the cache by hand; nothing entitled is ever served out of it. Separate files per reader
        keep two processes sharing a worker id (a round and the dashboard) off each other's half."""
        # The shared service can observe deletions and reactions without a PR updatedAt change.
        # Let it answer each pass instead of hiding those changes behind this local age cache.
        if self._direct_source and not self.gh.binding_current():
            return None
        if self._uses_state_service():
            return None
        key = self._observed.get(pr)
        if not key:
            return None
        try:
            sc = json.loads(path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(sc, dict) or sc.get("updated_at") != key:
            return None
        fetched = sc.get("fetched_at")
        backstop = min(SBCACHE_BACKSTOP_S, self.gh.ttl) if self._direct_source else SBCACHE_BACKSTOP_S
        if not isinstance(fetched, (int, float)) or not 0 <= time.time() - fetched < backstop:
            return None  # also rejects a future timestamp: a clock we cannot place is not a fresh one
        return sc if not self._direct_source or self.gh.binding_current() else None

    def _write_sidecar(self, path: Path, pr: int, payload: dict) -> None:
        """Atomically, and only ever after a SUCCESSFUL read. A failed fetch must leave the previous key
        in place: advancing it would let the next pass treat the failure as a confirmed answer."""
        payload = {**payload, "updated_at": self._observed.get(pr, ""), "fetched_at": time.time()}
        try:
            if self._direct_source:
                self._write_source_cache(
                    path, payload, prune=path != self._key_path(pr) or payload.get("status") != "present"
                )
                return
            self.sbcache.mkdir(parents=True, exist_ok=True)
            # A temp name unique per WRITE, not per process: `status`, the dashboard and a round can
            # share a worker id, and the dashboard refreshes on a background thread, so neither a
            # shared path nor a per-pid one keeps two writers out of the same temp file.
            tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(payload) + "\n")
            os.replace(tmp, path)
        except OSError:
            pass  # a cache we cannot write is a slow round, not a wrong one

    def _prune_source_cache(self):
        if self._direct_source:
            from .forgejo_reads import prune_state_cache

            prune_state_cache(self.sbcache.parent)

    def _write_source_cache(self, path, value, *, prune=True):
        """Private atomic source cache writes, without following a target symlink."""
        self.sbcache.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(self.sbcache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp").name
        try:
            os.fchmod(directory, 0o700)
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            with os.fdopen(descriptor, "w") as output:
                output.write(json.dumps(value) + "\n")
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            finally:
                os.close(directory)
        if prune:
            self._prune_source_cache()

    def _issue_comments(self, pr: int, *, force: bool = False) -> list[dict] | None:
        """Memoize legacy reads within a pass; direct sources own their bounded cache."""
        if self._direct_source and not self.gh.binding_current():
            return None
        # Direct sources already provide bounded coalescing and failure handling.
        # Retaining whole histories here would defeat that cache's byte bound.
        hit = None if force or self._direct_source else self._comments.get(pr)
        if hit and time.time() - hit[0] < COMMENTS_MEMO_S:
            return hit[1]
        with read_context(fresh=force, revision=self._observed.get(pr, "")):
            cs = self.gh.issue_comments(pr)
            self._shared_issue_hit[pr] = cache_hit()
        if self._direct_source:
            while len(self._shared_issue_hit) > 128:
                self._shared_issue_hit.pop(next(iter(self._shared_issue_hit)))
        else:
            self._comments[pr] = (time.time(), cs)
        return cs

    def _uses_state_service(self):
        return not self._direct_source and github_state_enabled()

    def _meta(self, data, provenance):
        if self._direct_source and not self.gh.binding_current():
            return Meta({}, "fetch_failed")
        return Meta(data, provenance)

    def gh_meta(self, pr: int, *, force: bool = False) -> Meta:
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
        if self._direct_source and not self.gh.binding_current():
            return self._meta({}, "fetch_failed")
        if force:
            self._survey_facts.pop(pr, None)
        facts = self._survey_facts.get(pr)
        if facts and facts.get("issue_complete"):
            meta = facts.get("meta")
            return self._meta(meta if meta is not None else {}, "fresh" if meta is not None else "missing")
        cache = self._cache_path(pr)
        sc = None if force else self._sidecar(self._key_path(pr), pr)
        if sc is not None:
            # The PR has not changed since this was read, so re-reading it would return the same answer.
            # `assumed`, not `fresh` — and a recorded ABSENCE stays an absence: a cached `missing` must
            # not come back as a present-but-empty scoreboard, which reads as a real one to callers.
            if sc.get("status") == "present" and isinstance(sc.get("meta"), dict):
                return self._meta(sc["meta"], "assumed")
            if sc.get("status") == "missing":
                return self._meta({}, "missing")
        if not force and not self._uses_state_service() and not self._observed.get(pr) and cache.exists():
            # No clock to compare against (a caller outside a survey pass): the plain TTL, as before.
            # `assumed`, not `fresh` — it was not read from GitHub during this pass, and `fresh` is the
            # word the mutating path trusts.
            age = time.time() - cache.stat().st_mtime
            cached = self._load(cache)
            ttl = min(SBCACHE_TTL, self.gh.ttl) if self._direct_source else SBCACHE_TTL
            if age < ttl and cached is not None and (not self._direct_source or age >= 0):
                return self._meta(cached, "assumed")

        comments = self._issue_comments(pr, force=force)
        fetch_failed = comments is None
        # Source timestamps are normalized to fixed-width UTC by the adapter.
        # Stable timestamp sorting in the reducer then breaks ties by native ID.
        if self._direct_source and comments is not None:
            comments = sorted(comments, key=lambda comment: comment["id"], reverse=True)
        data = scoreboard(comments)

        if data is None:
            # Serve a prior value ONLY on a fetch failure (transient); a successful fetch that parsed no
            # scoreboard means there genuinely isn't one now — don't keep serving a now-absent meta.
            prior = self._load(cache) if fetch_failed else None
            if prior is not None:
                return self._meta(prior, "stale")
            if not fetch_failed:
                # A confirmed absence is worth caching: without it, every pass re-reads every PR that
                # has no scoreboard yet, which is most of a healthy queue. Drop the payload with it —
                # left behind, the TTL path would keep serving a board we just confirmed is gone.
                if not self._direct_source or not self._shared_issue_hit.get(pr, True):
                    self._write_sidecar(self._key_path(pr), pr, {"status": "missing"})
                cache.unlink(missing_ok=True)
            return self._meta({}, "fetch_failed" if fetch_failed else "missing")
        if self._direct_source and self._shared_issue_hit.get(pr, True):
            return self._meta(data, "assumed")  # Do not reset a reused observation's disk TTL.
        self._write_sidecar(self._key_path(pr), pr, {"status": "present", "meta": data})
        try:  # the legacy copy: the stale-on-failure fallback reads it, nothing entitled does
            if self._direct_source:
                self._write_source_cache(cache, data, prune=False)
            else:
                self.sbcache.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data) + "\n")
        except OSError:
            pass
        finally:
            self._prune_source_cache()
        return self._meta(data, "assumed" if self._shared_issue_hit.get(pr, self._direct_source) else "fresh")

    @staticmethod
    def _load(cache: Path) -> dict | None:
        """The cached meta, or None when there isn't a usable one. `{}` is a legitimate scoreboard (an
        engine that posted a skeleton), so it must not double as the couldn't-read sentinel."""
        try:
            data = json.loads(cache.read_text() or "null")
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def bust(self, pr: int) -> None:
        """Forget everything about this PR: the meta, both sidecars, and the in-memory comment memo.

        The memo matters as much as the files. bust() is called right after the worker itself posts,
        and a memo left warm would rebuild the cache from the comments as they were BEFORE that post,
        re-caching the state we just invalidated."""
        if self._direct_source:
            self.gh.invalidate()
        else:
            invalidate()
        self._cache_path(pr).unlink(missing_ok=True)
        self._key_path(pr).unlink(missing_ok=True)
        self._contest_path(pr).unlink(missing_ok=True)
        self._comments.pop(pr, None)
        self._shared_issue_hit.pop(pr, None)
        self._survey_facts.pop(pr, None)

    # --- predicates over the meta (the cascade's clean-head / blocking rules) ---
    def ledger_clean_head(self, pr: int) -> str:
        meta = self.gh_meta(pr).data
        if meta.get("mode") == "init":
            return ""
        states = meta.get("states") or {}
        if states:
            # Carried verdicts can complete a head without generating any new runs.
            completed = {"green", "blocking_request", "blocking_block"}
            # Adverse findings defer stale approvals; a block also defers absent rubrics.
            allowed = completed | (
                {"stale"} if any(v in ("blocking_request", "blocking_block") for v in states.values()) else set()
            )
            if "blocking_block" in states.values():
                allowed.add("absent")
            return str(meta.get("head_sha") or "") if all(v in allowed for v in states.values()) else ""
        runs = meta.get("runs") or []
        if runs and all(r.get("verdict") != "error" for r in runs):
            return str(meta.get("head_sha") or "")
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

    def newest_contest_reply(self, pr: int, *, force: bool = False):
        """The newest author CONTEST reply on this PR's rubric threads, or None. A contest reply has
        its `in_reply_to_id` pointing at a thread root carrying a `<!--tauceti-rubric:NAME-->`
        marker. Our own comments are dropped by MARKER, never by author login: a contest answer
        carries `tauceti-reply:` and a root carries `tauceti-rubric:`, so both are skipped, while a
        human contest (even one sharing the worker's login) is never wrongly dropped. "Newest" is by
        the monotonic comment `id` (not the second-resolution timestamp, which can't separate two
        replies in one second). Its creation time is returned for review-affinity aging. Returns
        {'id', 'rubric', 'created_at'} of the newest such reply."""
        if force:
            self._survey_facts.pop(pr, None)
        facts = self._survey_facts.get(pr)
        if facts and facts.get("review_complete"):
            return facts.get("contest")
        sc = None if force else self._sidecar(self._contest_path(pr), pr)
        if sc is not None and "contest" in sc:
            return sc["contest"]
        with read_context(fresh=force, revision=self._observed.get(pr, "")):
            rcs = self.gh.review_comments(pr)
        if rcs is None:
            return None  # a fetch FAILURE answers nothing and must not be cached as "no contest"
        best = self._newest_contest(rcs)
        self._write_sidecar(self._contest_path(pr), pr, {"status": "present" if best else "missing", "contest": best})
        return best

    @staticmethod
    def _newest_contest(rcs: list[dict]):
        """The newest contest reply in a PR's review comments; the pure part of newest_contest_reply."""
        return newest_contest(rcs)

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
