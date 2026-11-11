#!/usr/bin/env python3
"""TAUCETI_OWN_BRANCHES narrows maintenance and the open-PR count to matching head branches."""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
survey_mod = importlib.import_module("tauceti_worker.survey")
from tauceti_worker.github import GitHubError
from tauceti_worker.github_state import SurveyRows


def pr(number, author, head, *, bot=False, owner="me", build="", conflicting=False):
    return {
        "number": number,
        "headRefOid": f"head-{number}",
        "headRefName": head,
        "headRepositoryOwner": {"login": owner},
        "headRepository": {"name": "TauCeti"},
        "statusCheckRollup": ([{"context": "build", "state": build}] if build else []),
        "author": {"login": author, "is_bot": bot},
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
    }


CANONICAL = survey_mod.TAUCETI_OWNER
RAW = [
    pr(1, "me", "roadmap/a-alpha", build="FAILURE"),
    pr(2, "me", "roadmap/b-gamma", build="FAILURE"),
    pr(3, "me", "roadmap/c-alpha", conflicting=True),
    pr(4, "me", "roadmap/d-gamma", conflicting=True),
    pr(5, "review-bot", "bump-mathlib/test", bot=True, owner=CANONICAL, build="FAILURE", conflicting=True),
    pr(6, "peer", "roadmap/e-gamma", owner="peer", build="SUCCESS"),
    pr(7, "peer", "roadmap/f-alpha", owner="peer", conflicting=True),
]


class Counters:
    def read(self, _name):
        return 0


def scope(pattern):
    env = {name: value for name, value in os.environ.items() if name != "TAUCETI_OWN_BRANCHES"}
    if pattern is not None:
        env["TAUCETI_OWN_BRANCHES"] = pattern
    return patch.dict(os.environ, env, clear=True)


def classify(pattern, can_push=lambda _repo: True):
    with scope(pattern), patch.object(survey_mod, "me", lambda: "me"), patch.object(survey_mod, "can_push", can_push):
        sv = survey_mod.survey(
            SimpleNamespace(wid="test"), SimpleNamespace(open_prs=lambda: RAW), None, Counters(), deep=False
        )
    return {
        "rebase": sorted(c.pr for c in sv.rebaseable.actionable),
        "fix-ci": [c.pr for c in sv.red_ci.actionable],
        "bump": [c.pr for c in sv.bump.actionable],
        "review": [c.pr for c in sv.reviewable.actionable],
        "open": sv.n_mine_open,
    }


def automatic_open_count(pattern):
    """The automatic authoring cap, with the compact survey reporting 140 open PRs for the login."""
    rows = SurveyRows([{"pr": RAW[0], "review": {"issue_complete": True, "review_complete": True}}], {"me": 140})
    with (
        scope(pattern),
        patch.object(survey_mod, "me", lambda: "me"),
        patch("tauceti_worker.github_state.read_survey", return_value=(True, rows)),
    ):
        gh = SimpleNamespace(open_prs=lambda: RAW, repo="TauCetiProject/TauCeti")
        return survey_mod.survey(
            SimpleNamespace(wid="test"), gh, None, Counters(), stages=[], count_all_roadmaps=True
        ).n_mine_open


def listing_outage(pattern, totals, *, cached=False):
    """The open-PR listing fails while the compact survey offers the conflicting #3 for rebase."""
    review = {"issue_complete": True, "review_complete": True, "rebase_heads": []}
    rows = SurveyRows([{"pr": RAW[2], "review": review}], totals, cached=cached)

    def fail():
        raise GitHubError("open PR query failed: HTTP 504")

    with (
        scope(pattern),
        patch.object(survey_mod, "me", lambda: "me"),
        patch("tauceti_worker.github_state.read_survey", return_value=(True, rows)),
    ):
        try:
            sv = survey_mod.survey(
                SimpleNamespace(wid="test"),
                SimpleNamespace(open_prs=fail, repo="TauCetiProject/TauCeti"),
                None,
                Counters(),
                stages=["rebase"],
                count_all_roadmaps=True,
                cached_only=cached,
            )
        except GitHubError as e:
            return f"raised: {e}"
    return {"rebase": [c.pr for c in sv.rebaseable.actionable], "open": sv.n_mine_open, "max": sv.max_open_prs}


def main():
    def no_query(_repo):
        raise AssertionError("unexpected permission query")

    checks = [
        (
            "unset: every PR of the login, and bot PRs",
            classify(None),
            {"rebase": [3, 4, 5], "fix-ci": [1, 2], "bump": [5], "review": [6], "open": 4},
        ),
        (
            "set: matching branches of the login only; reviews unchanged; no bot permission query",
            classify("-alpha$", no_query),
            {"rebase": [3], "fix-ci": [1], "bump": [], "review": [6], "open": 2},
        ),
        (
            "a pattern that also names bot branches keeps tending them",
            classify("-alpha$|^bump-mathlib/"),
            {"rebase": [3, 5], "fix-ci": [1], "bump": [5], "review": [6], "open": 2},
        ),
        ("unset: the automatic cap reads the compact per-author total", automatic_open_count(None), 140),
        ("set: the automatic cap counts matching branches from the listing", automatic_open_count("-alpha$"), 2),
        (
            "unset: a failing listing is never read when the compact total is known",
            listing_outage(None, {"me": 1}),
            {"rebase": [3], "open": 1, "max": survey_mod.configured_max_open_prs()},
        ),
        (
            "set, listing fails: maintenance kept, the unscoped total bounds the cap",
            listing_outage("-alpha$", {"me": 1}),
            {"rebase": [3], "open": 1, "max": survey_mod.configured_max_open_prs()},
        ),
        (
            "set, listing fails on a cached snapshot: maintenance kept, the unscoped total bounds the cap",
            listing_outage("-alpha$", {"me": 1}, cached=True),
            {"rebase": [3], "open": 1, "max": survey_mod.configured_max_open_prs()},
        ),
        (
            "set, listing fails with no total: maintenance kept, authoring held at the cap",
            listing_outage("-alpha$", None),
            {"rebase": [3], "open": survey_mod.configured_max_open_prs(), "max": survey_mod.configured_max_open_prs()},
        ),
    ]
    failures = 0
    for name, got, want in checks:
        ok = got == want
        print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
        failures += not ok
    print(f"\n{'PASS' if not failures else 'FAIL'}: {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
