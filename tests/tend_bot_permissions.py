#!/usr/bin/env python3
"""Only identities that can push to canonical tend its bot PRs."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
survey_mod = importlib.import_module("tauceti_worker.survey")


def pr(number, author, *, bot=False, owner="contributor", head="feature", failed=False, conflicting=False):
    return {
        "number": number,
        "headRefOid": f"head-{number}",
        "headRefName": head,
        "headRepositoryOwner": {"login": owner},
        "headRepository": {"name": "TauCeti"},
        "statusCheckRollup": ([{"context": "build", "state": "FAILURE"}] if failed else []),
        "author": {"login": author, "is_bot": bot},
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
    }


RAW = [
    pr(1, "me", failed=True),
    pr(2, "review-bot", bot=True, owner=survey_mod.TAUCETI_OWNER, failed=True, conflicting=True),
    pr(3, "review-bot", bot=True, owner=survey_mod.TAUCETI_OWNER, head="bump-mathlib/test", failed=True),
    pr(6, "review-bot", bot=True, owner=survey_mod.TAUCETI_OWNER, head="lint-repair/main", failed=True),
    pr(4, "external-bot", bot=True, owner="external", conflicting=True),
    pr(5, "peer", owner=survey_mod.TAUCETI_OWNER, conflicting=True),
]


class Counters:
    def read(self, _name):
        return 0


def classify(access, raw=RAW):
    saved_me, saved_can_push = survey_mod.me, survey_mod.can_push
    survey_mod.me = lambda: "me"
    survey_mod.can_push = access if callable(access) else lambda _repo: access
    try:
        sv = survey_mod.survey(
            SimpleNamespace(wid="test"),
            SimpleNamespace(open_prs=lambda: raw),
            None,
            Counters(),
            deep=False,
        )
        return {
            "rebase": [c.pr for c in sv.rebaseable.actionable],
            "fix-ci": [c.pr for c in sv.red_ci.actionable],
            "bump": [c.pr for c in sv.bump.actionable],
            "lint-repair": [c.pr for c in sv.lint_repair.actionable],
        }
    finally:
        survey_mod.me, survey_mod.can_push = saved_me, saved_can_push


def main():
    expected_own = {"rebase": [], "fix-ci": [1], "bump": [], "lint-repair": []}
    expected_with_bot = {"rebase": [2], "fix-ci": [1, 2], "bump": [3], "lint-repair": [6]}
    checks = [
        ("denied", classify(False), expected_own),
        ("unknown", classify(None), expected_own),
        ("allowed", classify(True), expected_with_bot),
    ]

    def no_query(_repo):
        raise AssertionError("unexpected permission query")

    checks.append(("no bot PR", classify(no_query, [RAW[0]]), expected_own))

    failures = 0
    for name, got, want in checks:
        ok = got == want
        print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
        failures += not ok
    print(f"\n{'PASS' if not failures else 'FAIL'}: {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
