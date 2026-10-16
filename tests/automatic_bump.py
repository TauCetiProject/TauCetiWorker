#!/usr/bin/env python3
"""Dependency repair remains actionable past legacy caps, with ordinary gates intact."""

import importlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
s = importlib.import_module("tauceti_worker.survey")
w = importlib.import_module("tauceti_worker.work_units")
r = importlib.import_module("tauceti_worker.round")
HEAD = "a" * 40


def raw(**changes):
    return (
        dict(
            number=1,
            headRefOid=HEAD,
            headRefName="bump-mathlib/" + "b" * 40,
            headRepositoryOwner={"login": "TauCetiProject"},
            headRepository={"name": "TauCeti"},
            author={"login": "tauceti-review-bot[bot]", "is_bot": True},
            mergeable="MERGEABLE",
            labels=[],
            statusCheckRollup=[{"context": "build", "state": "FAILURE"}],
        )
        | changes
    )


class Reviews:
    def __init__(self, clean=False, contest=None):
        self.clean, self.contest = clean, contest

    def observe(self, prs):
        pass

    def gh_meta(self, *args, **kwargs):
        return s.Meta({"head_sha": HEAD, "states": {"reuse": "blocking_request"}}, "fresh")

    def ledger_clean_head(self, pr):
        return HEAD if self.clean else ""

    def ledger_blocking(self, *args):
        return True

    def inflight_review(self, *args, **kwargs):
        return set()

    def newest_contest_reply(self, *args, **kwargs):
        return self.contest


def classify(record, stages, reviews=None, rounds=0, attempts=100):
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(s, "me", return_value="me"),
        patch.object(s, "can_push", return_value=True),
    ):
        (Path(directory) / "ledger.json").write_text(
            json.dumps({"prs": {"1": {"rounds": [{"ts": datetime.now(UTC).isoformat()} for _ in range(rounds)]}}})
        )
        return s.survey(
            SimpleNamespace(wid="test", store_dir=Path(directory)),
            SimpleNamespace(open_prs=lambda: [record], fresh_claim_age=lambda _: None),
            reviews or Reviews(),
            SimpleNamespace(read=lambda _: attempts),
            stages=stages,
        )


assert classify(raw(), ["bump"]).bump.count == 1
assert classify(raw(mergeable="CONFLICTING"), ["rebase"]).rebaseable.count == 1
assert not classify(raw(headRefName="feature"), ["fix-ci"]).red_ci.count
assert not classify(raw(author={"login": "me"}, headRepositoryOwner={"login": "me"}), ["bump"]).bump.count
assert not s.PRInfo.from_json(raw(headRepository={"name": "other"})).automatic_bump
for label in ["keep", "hold", "wip", "human", "do-not-close"]:
    assert not classify(raw(labels=[{"name": label}]), ["bump", "rebase", "review", "fix"]).bump.count
green = raw(statusCheckRollup=[{"context": "build", "state": "SUCCESS"}])
assert classify(green, ["review"]).reviewable.count == 1
assert classify(green, ["review"], rounds=100).reviewable.count == 1
assert (
    classify(
        green | {"headRefName": "feature", "author": {"login": "me"}}, ["review"], rounds=100, attempts=0
    ).reviewable.count
    == 1
)
assert classify(green, ["fix"]).needs_fix.count == 1
contest = {"id": 42, "rubric": "reuse", "created_at": "2026-10-14T00:00:00Z"}
assert classify(green, ["review"], Reviews(clean=True, contest=contest)).reviewable.count == 1
# Dispatch revalidation must apply the same exemption as the survey.
sv = classify(green, ["fix"])
worker = SimpleNamespace(
    rs=Reviews(),
    counters=SimpleNamespace(read=lambda _: 100),
    gh=SimpleNamespace(pr_view=lambda *args: {"state": "OPEN", "isDraft": False, "headRefOid": HEAD}),
)
assert w._still_actionable("fix", worker, sv, sv.needs_fix.actionable[0])
# Repair prompts carry the deadline through both host and bubble preparation.
worker.claims = SimpleNamespace(begin_branch_work=lambda *args: True)
worker.rc = SimpleNamespace(deadline=1123)
worker.rs.bust = lambda *args: None
with patch.object(w, "run_in_bubble", return_value=0) as launch:
    assert (
        w._do_fixlike(
            worker,
            sv,
            sv.needs_fix.actionable[0],
            SimpleNamespace(agent_name="Codex"),
            True,
            prompt_file="fix.md",
            label="fix",
        )
        == 0
    )
    prompt = launch.call_args.args[2]
    assert "1970-01-01 00:18:43 UTC" in prompt
    assert "push it once" in prompt
    assert "CI red" in prompt

# Comment-only activity cannot classify an automatic repair as productive.
worker.gh.pr_progress_state = lambda _: {"head": HEAD, "ncomments": 2}
pre = {"head": HEAD, "ncomments": 1, "require_push": True}
assert not w._progressed_direct(worker, sv.needs_fix.actionable[0], pre)
assert w._progressed_direct(worker, sv.needs_fix.actionable[0], pre | {"require_push": False})
worker.gh.pr_progress_state = lambda _: {"head": "b" * 40, "ncomments": 2}
assert w._progressed_direct(worker, sv.needs_fix.actionable[0], pre)

# Deadline begins before preparation, and uses the actual timeout selected by the loop.
with patch.object(r.time, "time", return_value=1000), patch.object(r.subprocess, "Popen") as spawn:
    r.spawn_round([], timeout=123)
    assert float(spawn.call_args.kwargs["env"]["TAUCETI_ROUND_DEADLINE"]) == 1123
with patch.dict(os.environ, {"TAUCETI_ROUND_DEADLINE": "1123"}):
    assert r.RoundContext(SimpleNamespace()).deadline == 1123
print("automatic bump: PASS")
