"""Local quota wake hints. Observation never authorizes work or calls a provider."""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import log
from .quota import (
    CLAUDE_BOOTSTRAP_RETRY_S,
    CLAUDE_BOOTSTRAP_STALE_MARGIN_S,
    CLAUDE_BOOTSTRAP_TIMEOUT_S,
    RATE_LIMIT_RECHECK_MAX_S,
    Quota,
    _cooldown_left,
    _finite_num,
)
from .runtime_status import report_runtime

LOCAL_CHECK_SECONDS = 25
MAX_FILE_BYTES = 512 * 1024


def _read(path: Path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("quota observation too large")
    return json.loads(data)


@dataclass(frozen=True)
class Observation:
    signature: str
    identity: str
    release_at: float | None
    headroom: bool = field(default=False, compare=False)
    cached: bool = field(default=False, compare=False)
    parts: dict = field(default_factory=dict, compare=False, repr=False)
    credential: str | None = field(default=None, compare=False, repr=False)


def observe(
    cfg, provider: str, identities: dict, previous: Observation | None = None, effective_store=None
) -> Observation | None:
    """Read fixed, bounded paths only. Exceptions mean a missed hint, never a loop failure."""
    try:
        q = Quota(cfg)
        parts = {}

        def record(path):
            key = str(path)
            try:
                value = _read(path)
            except (OSError, ValueError):
                # An unreadable primary store must not hide a readable fallback.
                # Nor should a failed read manufacture a change followed by an
                # identical recovery. These retained values are hints only.
                value = previous.parts.get(key) if previous else None
            parts[key] = value
            return value

        q._cache_reader = record
        # Fully resolved cached windows need no bootstrap annotation for a wake
        # hint. Avoid nested credential/ledger reads in this observational path.
        q._idle_notes = lambda _readings: ({}, False)
        source, credential_data = q.local_credentials(provider, _read, identities)
        if provider == "claude":
            oauth = credential_data
            if not isinstance(oauth, dict) or not oauth.get("accessToken"):
                return None
            fp = q._fingerprint(oauth["accessToken"])
            account = q._claude_account_key(oauth)
            token_fp = fp
            directories = [source / ".tauceti-quota" / "usage", q._claude_usage_fallback()]
        else:
            auth = credential_data
            token = ((auth or {}).get("tokens") or {}).get("access_token")
            if not token:
                return None
            token_fp = q._fingerprint(token)
            fp = q._codex_account_id(auth) or token_fp
            account = fp
            directories = [q.cache_dir]
        q._usage_token_fps = {provider: token_fp}
        records, releases, headroom, has_cache = [], [], False, False
        now = time.time()
        effective_store = effective_store or q._usage_dir(provider)
        for directory in dict.fromkeys(directories):
            if provider == "claude":
                q._claude_usage_directory = directory
            keys = [account, f"forbidden:{token_fp}"]
            if provider == "claude" and str(account).startswith("creds:"):
                keys.append(f"{account}|credential:{fp}")
            cooldown_releases = []
            for key in keys:
                cooldown = record(q._cooldown_path(provider, key))
                records.append(cooldown)
                # Use the same source/account scoping as actual fetches. Countdown
                # values are not part of the signature; expiry is a separate event.
                scoped = q._scope_cooldown(cooldown, provider, key, fp if key == account else None)
                if directory == effective_store and _cooldown_left(scoped)[0] > 0:
                    until = scoped.get("until")
                    cooldown_releases.append(until)
            if cooldown_releases:
                releases.append(max(cooldown_releases))
            records.extend(
                [
                    record(q._raw_path(provider, fp)),
                    record(q._auth_refusal_path(provider, fp)),
                ]
            )
            # This is only a hint. Selection still resolves the effective store
            # and validates its cache before granting any permission.
            if provider == "claude":
                q._claude_account_key = lambda _oauth=None: account
                cached = q._cached_claude(fp)
                if cached is not None:
                    has_cache = True
                    headroom |= q._from_cached_claude(cached)[0].available
            else:
                cached = q._cached_codex(fp)
                if cached is not None:
                    has_cache = True
                    headroom |= q._codex_from_payload(*cached).available
        for path in q._model_refusal_paths(provider, token_fp):
            refusal = record(path)
            records.append(refusal)
            if isinstance(refusal, dict):
                at, until = refusal.get("at"), refusal.get("until")
                if _finite_num(at) and _finite_num(until) and at <= now < until <= at + RATE_LIMIT_RECHECK_MAX_S:
                    releases.append(until)
        if provider == "claude":
            # Bootstrap annotations can change without any new usage sample.
            ledger = record(q._bootstrap_paths()[0]) or {}
            active = {}
            for window in ("session", "weekly"):
                key = q._episode_key(window)
                reservation = ledger.get(key)
                active[key] = reservation
                if isinstance(reservation, dict):
                    at = reservation.get("at")
                    duration = (
                        CLAUDE_BOOTSTRAP_TIMEOUT_S + CLAUDE_BOOTSTRAP_STALE_MARGIN_S
                        if reservation.get("state") == "in-progress"
                        else CLAUDE_BOOTSTRAP_RETRY_S
                    )
                    if isinstance(at, (int, float)) and math.isfinite(at) and at <= now < at + duration:
                        releases.append(at + duration)
            records.append(active)
        identity = q._fingerprint(fp, token_fp, str(source))
        signature = hashlib.sha256(json.dumps([identity, records], sort_keys=True).encode()).hexdigest()
        return Observation(signature, identity, min(releases, default=None), headroom, has_cache, parts, token_fp)
    except Exception:
        return None


class QuotaWait:
    """A wait episode retains provider deadlines across opportunistic selections.

    Only extra HTTP attempts are deduplicated. Ordinary deadlines and the outer
    loop's admission, provider policy, and launch checks remain authoritative.
    """

    def __init__(self, cfg, agent, *, wall=None, monotonic=None, sleep=None):
        self.cfg, self.agent = cfg, agent
        self.wall = wall or time.time
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.providers = ("codex", "claude") if agent == "auto" else (agent,)
        self.identities = {}
        self.reset()

    def reset(self):
        if getattr(self, "deadlines", None):
            report_runtime(quota_local_check_seconds=None, quota_refresh_at=None)
        self.deadlines = {}
        self.monotonic_deadlines = {}
        self.ceilings = {}
        self.monotonic_ceilings = {}
        self.seen = {}
        self.attempted = set()
        self.measured = {}
        self.previous = {}
        self.context = {}
        self.pending = None
        self.last_selection = -math.inf

    def observations(self):
        return {
            p: observe(
                self.cfg, p, self.identities, self.seen.get(p), self.context.get("directory") if p == "claude" else None
            )
            for p in self.providers
        }

    def prepare(self, quota):
        """Called immediately before selection; the returned baseline closes the entry race."""
        baseline = self.observations()
        options = {}
        if self.pending is not None:
            due, changed = self.pending
            now, mono = self.wall(), self.monotonic()
            due = due | {p for p in self.deadlines if now >= self.deadlines[p] or mono >= self.monotonic_deadlines[p]}
            changed = changed | {
                p for p, value in baseline.items() if value is not None and p in self.seen and value != self.seen[p]
            }
            self.pending = due, changed
            inspect = due | changed
            fetch = due | {p for p in changed if baseline.get(p) and (p, baseline[p].identity) not in self.attempted}
            options = dict(inspect=inspect, fetch=fetch, previous=self.previous, refresh_providers=due)
            if self.context.get("directory") is not None:
                quota._wait_cache_directory = self.context["directory"]
            # Cache-only macOS observations must not introduce Keychain polling.
            if sys.platform == "darwin" and "claude" not in due and self.identities.get("claude"):
                oauth = self.identities["claude"]
                quota._claude_creds = lambda: (oauth, True)
                quota._refresh_claude_credential = lambda **_kw: False
        return baseline, options

    def selected(self, quota, snap, baseline, recheck):
        now, mono = self.wall(), self.monotonic()
        due = self.pending[0] if self.pending is not None else set(snap)
        changed = self.pending[1] if self.pending is not None else set()
        inspected = due | changed
        self.identities.update(getattr(quota, "_wait_identities", {}))
        if "claude" in inspected:
            self.context["directory"] = getattr(quota, "_claude_usage_directory", None)
        after = self.observations()
        for p, verdict in snap.items():
            if p not in self.deadlines or p in due:
                next_at, next_mono = now + recheck(verdict, now), mono + recheck(verdict, now)
                if p not in self.ceilings or now >= self.ceilings[p] or mono >= self.monotonic_ceilings[p]:
                    self.ceilings[p], self.monotonic_ceilings[p] = next_at, next_mono
                    self.attempted = {key for key in self.attempted if key[0] != p}
                self.deadlines[p] = min(next_at, self.ceilings[p])
                self.monotonic_deadlines[p] = min(next_mono, self.monotonic_ceilings[p])
            elif p in changed and not (verdict.error or "").startswith("local usage unavailable"):
                self.deadlines[p] = min(self.deadlines[p], now + recheck(verdict, now))
                self.monotonic_deadlines[p] = min(self.monotonic_deadlines[p], mono + recheck(verdict, now))
                if baseline.get(p) and baseline[p].release_at is not None:
                    release = baseline[p].release_at
                    self.deadlines[p] = min(self.deadlines[p], release)
                    self.monotonic_deadlines[p] = min(self.monotonic_deadlines[p], mono + max(0, release - now))
            if p in inspected:
                if baseline.get(p):
                    self.seen[p] = baseline[p]
                elif after.get(p):
                    # First successful observation establishes a baseline. A
                    # failed initial read is never itself a recovery event.
                    self.seen[p] = after[p]
                if p in getattr(quota, "_usage_attempted", set()):
                    measured = getattr(quota, "_usage_token_fps", {}).get(p)
                    self.measured[p] = measured
                    for value in (baseline.get(p), after.get(p)):
                        if value is not None and measured == value.credential:
                            self.attempted.add((p, value.identity))
                    if (
                        after.get(p)
                        and (not baseline.get(p) or after[p].identity == baseline[p].identity)
                        and not after[p].headroom
                    ):
                        # The selection just published its own refusal/blocked
                        # cache. Do not treat that publication as peer recovery.
                        self.seen[p] = after[p]
        self.previous = snap
        self.pending = None
        self.last_selection = mono

    def wait(self):
        original = self.recheck_at()
        report_runtime(quota_local_check_seconds=LOCAL_CHECK_SECONDS, quota_refresh_at=original)
        # Entry is checked immediately. Thereafter all observation-triggered
        # selections are at least one tick apart, even after persistent mismatch.
        while True:
            now, mono = self.wall(), self.monotonic()
            tick_started = mono
            due = {p for p in self.deadlines if now >= self.deadlines[p] or mono >= self.monotonic_deadlines[p]}
            if due:
                self.pending = due, set()
                return
            current = self.observations()
            for p, value in current.items():
                if value is not None and p not in self.seen:
                    self.seen[p] = value
                    if self.measured.get(p) == value.credential:
                        self.attempted.add((p, value.identity))
            changed = {p for p, value in current.items() if value is not None and value != self.seen.get(p)}
            if changed and mono >= self.last_selection + LOCAL_CHECK_SECONDS:
                # Network-only early wakes get a little staggering; cached recovery
                # uses ordinary cache-first selection and is never delayed here.
                if any(not current[p].cached and (p, current[p].identity) not in self.attempted for p in changed):
                    self.sleep(random.uniform(0.01, 5))
                self.pending = set(), changed
                log("quota: local observation changed for " + ", ".join(sorted(changed)) + "; rechecking selection")
                return
            for p in changed:
                value = current[p]
                if value.release_at is not None:
                    self.deadlines[p] = min(self.deadlines[p], value.release_at)
                    self.monotonic_deadlines[p] = min(
                        self.monotonic_deadlines[p], mono + max(0, value.release_at - now)
                    )
            until = min(
                max(0, LOCAL_CHECK_SECONDS - (self.monotonic() - tick_started)),
                min(max(0, d - now) for d in self.deadlines.values()),
                min(max(0, d - mono) for d in self.monotonic_deadlines.values()),
            )
            self.sleep(max(0.01, until))

    def recheck_at(self):
        now, mono = self.wall(), self.monotonic()
        remaining = min(
            min(max(0, d - now) for d in self.deadlines.values()),
            min(max(0, d - mono) for d in self.monotonic_deadlines.values()),
        )
        return now + remaining
