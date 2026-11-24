#!/usr/bin/env python3
"""Contest-claim dedup logic (the 👀-on-the-reply cross-fleet lock).

A fresh author CONTEST reply re-opens a cleanly-reviewed PR. Two workers surveying within the same
window both used to fire (the old suppressor was a per-worker counter that couldn't coordinate across
isolated stores). The claim now lives on GitHub: a 👀 reaction on the contesting reply, TTL'd so a
crashed worker's claim frees itself. This harness pins the pure decision — given a comment's reactions,
is the contest claimed? — without touching the live API (the reaction round-trip is exercised live in
the GitHub class; here we stub `reactions()` and assert the survey-time skip/fire decision).

With TAUCETI_REACTIONS=0 the claim is a file under the shared GitHub state directory instead. The second
half runs the real add/age/remove against a temporary directory and a `_gh` that records its calls,
then has forked workers race for one contest: exactly one may hold it.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import json
import multiprocessing
import os
import sys
import tempfile
import types
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

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
        self.calls = []

    def reactions(self, comment_id):
        return self._reactions

    def _gh(self, args):
        self.calls.append(args)
        return tc.subprocess.CompletedProcess(args, 0, '{"id": 9}', "")


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
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp}):
        return claimed_by(FakeGitHub(reactions))


def claimed_by(gh) -> bool:
    age = gh.fresh_claim_age(123)
    return age is not None and age < tc.CONTEST_CLAIM_TTL


def local_claims() -> list[tuple[str, object, object]]:
    """(name, got, want) for the claim kept on the host when reactions are off."""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        shared = {"TAUCETI_GITHUB_SHARED_DIR": tmp}
        off = {**shared, "TAUCETI_REACTIONS": "0", "TAUCETI_WORKER_ID": "a"}
        mine, peer = FakeGitHub([]), FakeGitHub([])

        def files():
            return sorted(p.name for p in Path(tmp).rglob("*.json"))

        with patch.dict(os.environ, shared):
            os.environ.pop("TAUCETI_REACTIONS", None)
            out.append(
                ("default: the claim is posted", mine.add_reaction(123) and mine.calls[-1][:3], ["api", "-X", "POST"])
            )
            out.append(("default: nothing is written locally", files(), []))
            out.append(
                (
                    "default: the release is a DELETE",
                    mine.remove_reaction(123) and mine.calls[-1][:3],
                    ["api", "-X", "DELETE"],
                )
            )
        mine.calls.clear()

        with patch.dict(os.environ, off):
            out.append(("off: the claim succeeds", mine.add_reaction(123), True))
            out.append(("off: it is one local record", files(), ["123-eyes.json"]))
            out.append(("off: a peer with reactions off skips", claimed_by(peer), True))
            with patch.dict(os.environ, {"TAUCETI_WORKER_ID": "b"}):
                out.append(
                    ("off: a peer's release leaves it", peer.remove_reaction(123) and files(), ["123-eyes.json"])
                )
        with patch.dict(os.environ, shared):
            os.environ.pop("TAUCETI_REACTIONS", None)
            out.append(("off: a peer with reactions on skips too", claimed_by(peer), True))
        with patch.dict(os.environ, off):
            record = Path(tmp) / "contest-claims" / "123-eyes.json"
            stale = json.loads(record.read_text()) | {"at": NOW - tc.CONTEST_CLAIM_TTL - 30}
            record.write_text(json.dumps(stale))
            out.append(("off: a record past the TTL frees the contest", claimed_by(peer), False))
            os.utime(record, (stale["at"], stale["at"]))
            out.append(("off: the next claim removes it", mine.add_reaction(456) and files(), ["456-eyes.json"]))
            out.append(("off: a 👀 from elsewhere still counts", claimed_by(FakeGitHub(CASES[3][1])), True))
            out.append(("off: the holder's release removes it", mine.remove_reaction(456) and files(), []))
            out.append(("off: no write reached GitHub", mine.calls + peer.calls, []))
            out.append(("off: a new claim", mine.add_reaction(789), True))
            with patch.dict(os.environ, {"TAUCETI_WORKER_ID": "b"}):
                out.append(("off: a peer cannot take a held claim", peer.add_reaction(789), None))
            out.append(("off: the holder may claim again", mine.add_reaction(789), True))
            record = Path(tmp) / "contest-claims" / "789-eyes.json"
            record.write_text(json.dumps(json.loads(record.read_text()) | {"at": NOW - tc.CONTEST_CLAIM_TTL - 30}))
            with patch.dict(os.environ, {"TAUCETI_WORKER_ID": "b"}):
                out.append(("off: a peer takes over an expired claim", peer.add_reaction(789), True))
            out.append(
                ("off: the old holder's release leaves it", mine.remove_reaction(789) and files(), ["789-eyes.json"])
            )
    return out


def race_child(tmp, worker, start, results):
    os.environ.update({"TAUCETI_GITHUB_SHARED_DIR": tmp, "TAUCETI_REACTIONS": "0", "TAUCETI_WORKER_ID": worker})
    start.wait(10)
    results.put(FakeGitHub([]).add_reaction(321))


def racing_claims() -> list[tuple[str, object, object]]:
    """(name, got, want) for workers in separate processes claiming one contest at the same moment."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = multiprocessing.get_context("fork")
        start, results = ctx.Barrier(8), ctx.Queue()
        children = [ctx.Process(target=race_child, args=(tmp, f"w{i}", start, results)) for i in range(8)]
        for child in children:
            child.start()
        got = sorted(str(results.get(timeout=20)) for _ in children)
        for child in children:
            child.join(10)
        return [("off: of eight racing workers exactly one holds the claim", got, ["None"] * 7 + ["True"])]


def review_skips_held_contest() -> list[tuple[str, object, object]]:
    """do_review leaves a contest a peer on this host holds without running the engine or releasing it."""
    released, ran = [], []
    w = types.SimpleNamespace(
        gh=types.SimpleNamespace(add_reaction=lambda i: None, remove_reaction=released.append),
        counters=types.SimpleNamespace(read=lambda k: 0),
    )
    c = types.SimpleNamespace(pr=42, head="a" * 40, contest="thread", contest_reply_id=123)
    with patch.object(tc.work_units, "run_to_logfile", side_effect=lambda *a, **k: ran.append(a) or 0):
        out = tc.work_units.do_review(w, None, c, types.SimpleNamespace(work_model="claude"), bubble=False)
    return [("off: a review of a held contest selects other work", (out, ran, released), (None, [], []))]


def main() -> int:
    fails = 0
    for name, reactions, want in CASES:
        got = claimed(reactions)
        ok = got == want
        fails += not ok
        verb = "SKIP" if got else "fire"
        print(f"[{'OK ' if ok else 'BAD'}] {name:<26} -> {verb} (want {'SKIP' if want else 'fire'})")
    for name, got, want in local_claims() + racing_claims() + review_skips_held_contest():
        ok = got == want
        fails += not ok
        print(f"[{'OK ' if ok else 'BAD'}] {name}" + ("" if ok else f": got {got!r}, want {want!r}"))
    # _parse_iso8601 round-trips GitHub's whole-second Z timestamps.
    assert tc._parse_iso8601("2026-06-19T08:03:48Z") == int(
        __import__("datetime").datetime(2026, 6, 19, 8, 3, 48, tzinfo=__import__("datetime").timezone.utc).timestamp()
    )
    assert tc._parse_iso8601(None) is None and tc._parse_iso8601("nonsense") is None
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
