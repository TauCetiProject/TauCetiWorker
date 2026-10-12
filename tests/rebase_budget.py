#!/usr/bin/env python3
"""A rebase retry budget renews only at a distinct, verified healthy checkpoint."""

import importlib
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

survey_mod = importlib.import_module("tauceti_worker.survey")
wu = importlib.import_module("tauceti_worker.work_units")
Counters, PRInfo, Candidate = survey_mod.Counters, survey_mod.PRInfo, survey_mod.Candidate


def pr(**changes):
    original = PRInfo(
        number=17,
        head_oid="a" * 40,
        head_ref="feature",
        head_owner="me",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="CONFLICTING",
        author="me",
        build_success=False,
        build_failed=True,
    )
    return replace(original, **changes)


with tempfile.TemporaryDirectory() as directory:
    state = Path(directory)
    counters = Counters(SimpleNamespace(state=state))
    legacy = "rebase-pr-17"
    counters.write(legacy, 3)

    # Unverified pushes, pending/failed CI, unknown mergeability and outstanding
    # reconciliation requests cannot replenish an exhausted legacy budget.
    for observed, handoff in [
        (pr(head_oid="b" * 40), False),
        (pr(mergeable="MERGEABLE"), False),
        (pr(build_success=True, build_failed=False, mergeable="UNKNOWN"), False),
        (pr(build_success=True, build_failed=False), False),
        (pr(build_success=True, build_failed=False, mergeable="MERGEABLE"), True),
        (pr(build_success=True, build_failed=False, mergeable="MERGEABLE", is_draft=True), False),
        (pr(build_success=True, build_failed=False, mergeable="MERGEABLE", build_status_at=-1), False),
    ]:
        counters.observe_rebase_progress(observed, handoff=handoff)
        assert counters.rebase_budget_key(17) == legacy
        assert counters.read(legacy) == 3

    # A proven healthy head gives the retired PR a new allowance, without
    # destroying history or changing another PR's budget.
    healthy = pr(build_success=True, build_failed=False, mergeable="MERGEABLE", build_status_at=100)
    counters.observe_rebase_progress(healthy, handoff=False)
    first = counters.rebase_budget_key(17)
    assert first != legacy and counters.read(first) == 0
    assert counters.read(legacy) == 3
    assert counters.rebase_budget_key(18) == "rebase-pr-18"

    # Spending all three attempts stays exhausted on repeated observations and
    # across process restarts. An atomic checkpoint does not race an integer reset.
    for _ in range(3):
        counters.incr(first)
    before = (state / "rebase-progress-17.json").stat().st_mtime_ns
    for _ in range(3):
        counters.observe_rebase_progress(healthy, handoff=False)
    counters = Counters(SimpleNamespace(state=state))
    assert counters.rebase_budget_key(17) == first
    assert counters.read(first) == 3
    assert (state / "rebase-progress-17.json").stat().st_mtime_ns == before
    counters.observe_rebase_progress(replace(healthy, build_status_at=101), handoff=False)
    assert counters.rebase_budget_key(17) == first and counters.read(first) == 3

    # Failed new revisions and still-open handoffs do not evade this allowance.
    counters.observe_rebase_progress(pr(head_oid="c" * 40), handoff=False)
    counters.observe_rebase_progress(replace(healthy, head_oid="c" * 40), handoff=True)
    assert counters.rebase_budget_key(17) == first and counters.read(first) == 3

    # A changed revision must itself pass CI and have no merge conflict.
    changed = replace(healthy, head_oid="c" * 40, build_status_at=200)
    counters.observe_rebase_progress(changed, handoff=False)
    second = counters.rebase_budget_key(17)
    assert second != first and counters.read(second) == 0 and counters.read(first) == 3

    # Charges/refunds stay attached to the selected allowance even if another
    # survey records newer successful CI while a round is in flight.
    selected = Candidate(17, changed.head_oid, rebase_budget_key=second)
    counters.observe_rebase_progress(replace(changed, head_oid="d" * 40, build_status_at=201), handoff=False)
    third = counters.rebase_budget_key(17)
    worker = SimpleNamespace(counters=counters)
    with patch.object(wu, "_do_fixlike", return_value=1) as action:
        assert wu.do_rebase(worker, None, selected, None, False) == 1
    assert counters.read(second) == 1 and counters.read(third) == 0
    assert action.call_args.kwargs["charged"] == (second,)

    # A storage outage retains the last allowance rather than granting an
    # unpersisted reset or interrupting the whole repository survey.
    existing = set(state.iterdir())
    with patch.object(survey_mod.os, "replace", side_effect=OSError("storage unavailable")):
        counters.observe_rebase_progress(replace(changed, head_oid="e" * 40, build_status_at=300), handoff=False)
    assert counters.rebase_budget_key(17) == third
    assert set(state.iterdir()) == existing

    # A handoff selected by the survey uses its explicit lifetime budget.
    with patch.object(wu, "_do_fixlike", return_value=0):
        wu.do_rebase(worker, None, Candidate(18, "d" * 40, rebase_budget_key="rebase-pr-18"), None, False)
    assert counters.read("rebase-pr-18") == 1

    with patch.object(wu, "_do_fixlike") as action:
        try:
            wu.do_rebase(worker, None, Candidate(18, "d" * 40), None, False)
        except wu.Die:
            pass
        else:
            raise AssertionError("an unbudgeted candidate must not launch work")
        action.assert_not_called()

    # Corrupt or unsupported checkpoint data must never reopen the frozen
    # legacy allowance. Integer-counter publication is atomic too.
    checkpoint = state / "rebase-progress-17.json"
    for invalid in ("{", '{"schema":"v2"}', '{"schema":"tauceti-rebase-progress/v1","checkpoint":{}}'):
        checkpoint.write_text(invalid)
        try:
            counters.rebase_budget_key(17)
        except survey_mod.RebaseBudgetUnavailable:
            pass
        else:
            raise AssertionError("corrupt evidence must fail closed")
    counters.write("rebase-pr-18", 2)
    with patch.object(survey_mod.os, "replace", side_effect=OSError("interrupted write")):
        try:
            counters.write("rebase-pr-18", 3)
        except OSError:
            pass
    assert counters.read("rebase-pr-18") == 2

print("PASS: rebase budget renewal requires distinct exact-head green and mergeable evidence")
