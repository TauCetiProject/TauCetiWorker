#!/usr/bin/env python3
"""An engine "already published at this head" result stops partial-history PRs being re-dispatched."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures.review_runtime_stub import install_runtime_stub

install_runtime_stub()

from tauceti_worker import activity
from tauceti_worker import work_units as wu
from tauceti_worker.activity import Round
from tauceti_worker.config import NoProgress

sv = importlib.import_module("tauceti_worker.survey")

HEAD = "a" * 40
OTHER = "b" * 40

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    counters = sv.Counters(SimpleNamespace(state=directory))

    # Nothing recorded, then a record for this head, a different head, and an expired record.
    assert not counters.unchanged_review_recent(42, HEAD)
    counters.record_unchanged_review(42, HEAD)
    assert counters.unchanged_review_recent(42, HEAD)
    assert not counters.unchanged_review_recent(42, OTHER), "a new push must be reviewed at once"
    assert not counters.unchanged_review_recent(43, HEAD)
    with patch.object(sv.time, "time", return_value=time.time() + sv.UNCHANGED_REVIEW_TTL + 1):
        assert not counters.unchanged_review_recent(42, HEAD), "the record must expire"
    (directory / "review-unchanged-44.json").write_text("not json")
    assert not counters.unchanged_review_recent(44, HEAD), "an unreadable record must not suppress review"

    # Survey-side helper tolerates counters without the method (test fakes, older state).
    assert sv._unchanged_review_recent(counters, 42, HEAD)
    assert not sv._unchanged_review_recent(SimpleNamespace(read=lambda _: 0), 42, HEAD)

    # do_review records the unchanged result for exactly the reviewed head.
    recorded = []
    values = {}
    w = SimpleNamespace(
        gh=SimpleNamespace(repo="TauCetiProject/TauCeti"),
        cfg=SimpleNamespace(state=directory, logdir=directory, store_dir=directory / "store", wid="w"),
        counters=SimpleNamespace(
            read=lambda k: values.get(k, 0),
            write=lambda k, v: values.update({k: v}),
            record_unchanged_review=lambda pr, head, at: recorded.append((pr, head, at)),
        ),
        rs=SimpleNamespace(review_rounds=lambda *_: 1, bust=lambda _: None),
    )
    c = SimpleNamespace(pr=42, head=HEAD, contest=None)

    engine_at = []

    def engine(*args, **kwargs):
        engine_at.append(time.time())
        path = args[1].parent / "review-results" / (args[1].stem + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(outcome))
        return 0

    guarded = []
    shared = []
    for outcome, expected, expected_guard in (
        ({"head_sha": HEAD, "changed": False}, [(42, HEAD)], []),
        ({"head_sha": HEAD, "changed": True}, [], []),
        ({"head_sha": OTHER, "changed": False}, [], []),
        ({"head_sha": HEAD, "changed": False, "outcome": "diff-guard"}, [(42, HEAD)], [(42, HEAD)]),
        ({"head_sha": OTHER, "changed": False, "outcome": "diff-guard"}, [], []),
    ):
        recorded.clear()
        guarded.clear()
        shared.clear()
        with (
            patch.object(
                wu.peer_claims, "note_diff_guard", side_effect=lambda _s, pr, head: guarded.append((pr, head))
            ),
            patch.object(
                wu.peer_claims,
                "note_unchanged_review",
                side_effect=lambda _s, pr, head, now: shared.append((pr, head, now)),
            ),
            patch.object(wu.time, "strftime", return_value="fixed"),
            patch.object(wu, "run_to_logfile", side_effect=engine),
            patch.object(wu, "me", return_value="tester"),
            patch.object(wu, "_sync_review_outbox", return_value=0),
            patch.object(activity, "emit"),
        ):
            round = Round("w")
            round.select("review")
            started = time.time()
            rc = wu.do_review(w, None, c, SimpleNamespace(work_model="claude"), False)
            round.finish(NoProgress("other candidates exhausted") if rc is None else None)
        assert [(pr, head) for pr, head, _ in recorded] == expected, (outcome, recorded)
        assert guarded == expected_guard, (outcome, guarded)
        expected_shared = [] if outcome.get("outcome") == "diff-guard" else expected
        assert [(pr, head) for pr, head, _ in shared] == expected_shared, (
            "every worker on the host skips an unchanged head",
            outcome,
            shared,
        )
        # Stamped when the round began, so a contest reply posted while the engine ran is newer.
        assert all(started <= at <= engine_at[-1] for _, _, at in recorded + shared), (recorded, shared, engine_at)

    # A contest round records the published verdict too, stamped at its start, so a survey without
    # the scoreboard does not offer the reply it just answered again.
    values.clear()
    w.counters.incr = lambda k: values.update({k: values.get(k, 0) + 1}) or values[k]
    c = SimpleNamespace(pr=42, head=HEAD, contest="reuse", contest_reply_id=0)
    outcome = {"head_sha": HEAD, "changed": True}
    recorded.clear()
    shared.clear()
    with (
        patch.object(
            wu.peer_claims,
            "note_unchanged_review",
            side_effect=lambda _s, pr, head, now: shared.append((pr, head, now)),
        ),
        patch.object(wu.time, "strftime", return_value="fixed"),
        patch.object(wu, "run_to_logfile", side_effect=engine),
        patch.object(wu, "me", return_value="tester"),
        patch.object(wu, "_sync_review_outbox", return_value=0),
        patch.object(activity, "emit"),
    ):
        round = Round("w")
        round.select("review")
        started = time.time()
        wu.do_review(w, None, c, SimpleNamespace(work_model="claude"), False)
        round.finish(None)
    assert [(pr, head) for pr, head, _ in recorded] == [(42, HEAD)], recorded
    assert [(pr, head) for pr, head, _ in shared] == [(42, HEAD)], shared
    assert all(started <= at <= engine_at[-1] for _, _, at in recorded + shared), (recorded, shared)

print(
    "PASS: unchanged review results suppress partial-history re-dispatch for that head only, and expire;"
    " each is recorded host-wide for that head"
)
