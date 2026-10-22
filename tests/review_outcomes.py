#!/usr/bin/env python3
"""An explicit unchanged review immediately selects other work without counting a review."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity, agents
from tauceti_worker import work_units as wu
from tauceti_worker.activity import Round
from tauceti_worker.config import NoProgress

HEAD = "a" * 40


def drive(directory, result, *, bubble=False):
    values = {"review-err-42": 2}
    busted, synced, events = [], [], []
    w = SimpleNamespace(
        cfg=SimpleNamespace(state=directory, logdir=directory, store_dir=directory / "store", wid="w"),
        counters=SimpleNamespace(read=lambda k: values.get(k, 0), write=lambda k, v: values.update({k: v})),
        rs=SimpleNamespace(review_rounds=lambda *_: 1, bust=busted.append),
    )
    c = SimpleNamespace(pr=42, head=HEAD, contest=None)
    opts = SimpleNamespace(work_model="claude")
    target = directory / "review-results" / "review-42-fixed.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({"head_sha": HEAD, "changed": False}))

    def engine(*args, **kwargs):
        path = kwargs["result_file"] if bubble else args[1].parent / "review-results" / (args[1].stem + ".json")
        assert path == target and not path.exists(), "stale invocation result was not removed"
        if result is not None:
            path.write_text(json.dumps(result))
        return 0

    with (
        patch.object(wu.time, "strftime", return_value="fixed"),
        patch.object(wu, "run_to_logfile", side_effect=engine),
        patch.object(wu, "review_in_bubble", side_effect=engine),
        patch.object(wu, "me", return_value="tester"),
        patch.object(wu, "_sync_review_outbox", side_effect=lambda *_: synced.append(True) or 0),
        patch.object(activity, "emit", side_effect=lambda kind, **kw: events.append((kind, kw))),
    ):
        round = Round("w")
        round.select("review")
        rc = wu.do_review(w, None, c, opts, bubble)
        round.finish(NoProgress("other candidates exhausted") if rc is None else None)
    assert not target.exists(), "result files must be removed after every invocation"
    return rc, values, busted, synced, events


with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    for bubble in (False, True):
        rc, values, busted, synced, events = drive(directory, {"head_sha": HEAD, "changed": False}, bubble=bubble)
        assert rc is None and values["review-err-42"] == 0 and busted == [42] and not synced
        finish = next(v for k, v in events if k == "round_finished")
        assert finish["round_type"] == "no-work" and finish["outcome"] != "completed"
    for result in ({"head_sha": HEAD, "changed": True}, {"head_sha": "other", "changed": False}, None, []):
        rc, values, busted, synced, _ = drive(directory, result)
        assert rc == 0 and values["review-err-42"] == 0 and synced and busted == [42]
    # The real host launcher passes the same path to the engine, including streaming mode.
    with (
        patch.object(agents, "run_process", return_value=SimpleNamespace(returncode=0)) as run,
        patch("tauceti_worker.fleet_capacity.check_launch_admission"),
        patch("tauceti_worker.claude_api.review_environment", return_value=None),
        patch.dict(os.environ, {"TAUCETI_STREAM": "1"}),
    ):
        agents.run_to_logfile(["engine"], directory / "review.log", "review #42")
        assert run.call_args.kwargs["env"]["TAUCETI_REVIEW_RESULT_FILE"] == str(
            directory / "review-results" / "review.json"
        )
print("PASS: unchanged host/bubble reviews select other work; real and older-engine outcomes still count")
