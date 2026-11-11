#!/usr/bin/env python3
"""An engine "already published at this head" result stops partial-history PRs being re-dispatched."""

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
            record_unchanged_review=lambda pr, head: recorded.append((pr, head)),
        ),
        rs=SimpleNamespace(review_rounds=lambda *_: 1, bust=lambda _: None),
    )
    c = SimpleNamespace(pr=42, head=HEAD, contest=None)

    def engine(*args, **kwargs):
        path = args[1].parent / "review-results" / (args[1].stem + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(outcome))
        return 0

    for outcome, expected in (
        ({"head_sha": HEAD, "changed": False}, [(42, HEAD)]),
        ({"head_sha": HEAD, "changed": True}, []),
        ({"head_sha": OTHER, "changed": False}, []),
    ):
        recorded.clear()
        with (
            patch.object(wu.time, "strftime", return_value="fixed"),
            patch.object(wu, "run_to_logfile", side_effect=engine),
            patch.object(wu, "me", return_value="tester"),
            patch.object(wu, "_sync_review_outbox", return_value=0),
            patch.object(activity, "emit"),
        ):
            round = Round("w")
            round.select("review")
            rc = wu.do_review(w, None, c, SimpleNamespace(work_model="claude"), False)
            round.finish(NoProgress("other candidates exhausted") if rc is None else None)
        assert recorded == expected, (outcome, recorded)

print("PASS: unchanged review results suppress partial-history re-dispatch for that head only, and expire")
