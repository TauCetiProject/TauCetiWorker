#!/usr/bin/env python3
"""Contest-claim dedup logic (the 👀-on-the-reply cross-fleet lock).

A fresh author CONTEST reply re-opens a cleanly-reviewed PR. Two workers surveying within the same
window both used to fire (the old suppressor was a per-worker counter that couldn't coordinate across
isolated stores). The claim now lives on GitHub: a 👀 reaction on the contesting reply, TTL'd so a
crashed worker's claim frees itself. This harness pins the pure decision — given a comment's reactions,
is the contest claimed? — without touching the live API (the reaction round-trip is exercised live in
the GitHub class; here we stub `reactions()` and assert the survey-time skip/fire decision).

Exit 0 = all cases agree; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from datetime import UTC
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

sys.path.insert(0, str(REPO))
import tauceti_worker as tc

NOW = int(tc.time.time())


def iso(epoch: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub(tc.GitHub):
    """A GitHub whose reaction list is a fixture — every other method is the real one (unused here)."""

    def __init__(self, reactions):
        super().__init__("owner/repo")
        self._reactions = reactions

    def reactions(self, comment_id):
        return self._reactions


# (name, reactions-fixture, want_claimed) — claimed => a peer holds it => this worker SKIPS.
CASES = [
    ("no reactions", [], False),
    ("fetch failed (fail-open)", None, False),
    ("unrelated emoji only", [{"content": "rocket", "created_at": iso(NOW - 5)}], False),
    ("fresh 👀", [{"content": "eyes", "created_at": iso(NOW - 5)}], True),
    ("👀 just under TTL", [{"content": "eyes", "created_at": iso(NOW - (tc.CONTEST_CLAIM_TTL - 30))}], True),
    ("👀 expired past TTL", [{"content": "eyes", "created_at": iso(NOW - (tc.CONTEST_CLAIM_TTL + 30))}], False),
    (
        "stale 👀 + a fresh one",
        [{"content": "eyes", "created_at": iso(NOW - 99999)}, {"content": "eyes", "created_at": iso(NOW - 5)}],
        True,
    ),
]


def claimed(reactions) -> bool:
    """Mirror the survey-time decision: a 👀 newer than the TTL means a peer is mid-review → skip."""
    age = FakeGitHub(reactions).fresh_claim_age(123)
    return age is not None and age < tc.CONTEST_CLAIM_TTL


def main() -> int:
    fails = 0
    for name, reactions, want in CASES:
        got = claimed(reactions)
        ok = got == want
        fails += not ok
        verb = "SKIP" if got else "fire"
        print(f"[{'OK ' if ok else 'BAD'}] {name:<26} -> {verb} (want {'SKIP' if want else 'fire'})")
    # _parse_iso8601 round-trips GitHub's whole-second Z timestamps.
    assert tc._parse_iso8601("2026-06-19T08:03:48Z") == int(
        __import__("datetime").datetime(2026, 6, 19, 8, 3, 48, tzinfo=__import__("datetime").timezone.utc).timestamp()
    )
    assert tc._parse_iso8601(None) is None and tc._parse_iso8601("nonsense") is None
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
