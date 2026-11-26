#!/usr/bin/env python3
"""Build affinity: a PR's builders get first refusal on its next agent round (decision 210 C)."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import build_affinity, work_units

# These records' fixtures use a fixed future clock while touched files keep real mtimes, so the
# occasional prune would delete a record just written.
build_affinity.random = types.SimpleNamespace(random=lambda: 1.0)

NOW = 2_000_000_000
GRACE = build_affinity.FIX_AFFINITY_GRACE_S
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def candidate(pr):
    return tc.Candidate(pr, f"{pr:040x}")


def prs(items):
    return [c.pr for c in items]


with tempfile.TemporaryDirectory() as tmp:
    os.environ["TAUCETI_GITHUB_SHARED_DIR"] = tmp
    scope = "github.com/TauCetiProject/TauCeti"

    def note(pr, worker, at=NOW):
        build_affinity.note_builder(scope, pr, worker)
        path = build_affinity._directory(scope, pr) / "workers" / worker
        os.utime(path, (at, at))

    note(1, "w1")
    note(2, "w2")
    note(3, "w1", at=NOW - build_affinity.BUILDER_TTL_S - 1)  # stale record
    batch = [candidate(n) for n in (4, 3, 2, 1)]
    ordered, deferred = build_affinity.prioritize(scope, "w1", batch, now=NOW)
    check("a builder offers its own PR first, others keep their order", prs(ordered), [1, 4, 3])
    check("peers defer another worker's PR", prs(deferred), [2])
    ordered, deferred = build_affinity.prioritize(scope, "w3", batch, now=NOW)
    check("a third worker defers both built PRs", (prs(ordered), prs(deferred)), ([4, 3], [2, 1]))
    check("records are per repository scope", build_affinity.builders("forgejo/other", 1, now=NOW), set())

    # The grace counts from the first deferral of that exact head, not from each worker's look.
    ordered, deferred = build_affinity.prioritize(scope, "w3", [candidate(2)], now=NOW + GRACE - 1)
    check("still deferred just inside the grace", prs(deferred), [2])
    ordered, deferred = build_affinity.prioritize(scope, "w3", [candidate(2)], now=NOW + GRACE)
    check("shared once the grace from the first deferral expires", (prs(ordered), prs(deferred)), ([2], []))
    moved = tc.Candidate(2, "f" * 40)
    ordered, deferred = build_affinity.prioritize(scope, "w3", [moved], now=NOW + GRACE)
    check("a new head starts a new grace", prs(deferred), [2])

    # Failing open: no shared directory, odd names, and a marker dated in the future never defer.
    del os.environ["TAUCETI_GITHUB_SHARED_DIR"]
    check(
        "no shared directory: nothing deferred", build_affinity.prioritize(scope, "w3", [candidate(1)], now=NOW)[1], []
    )
    os.environ["TAUCETI_GITHUB_SHARED_DIR"] = tmp
    build_affinity.note_builder(scope, 9, "../escape")
    check("a worker name with a path separator is not recorded", build_affinity.builders(scope, 9, now=NOW), set())
    note(10, "w1")
    odd = tc.Candidate(10, "not/a/head")
    check("an unusable head is never deferred", build_affinity.prioritize(scope, "w2", [odd], now=NOW)[1], [])
    marker = build_affinity._directory(scope, 1) / "deferred" / f"{1:040x}"
    os.utime(marker, (NOW + 10 * GRACE, NOW + 10 * GRACE))
    check(
        "a future-dated marker does not defer",
        prs(build_affinity.prioritize(scope, "w3", [candidate(1)], now=NOW)[0]),
        [1],
    )

    # Pruning drops a PR directory only when every record is past its TTL.
    note(20, "w1", at=NOW - 2 * build_affinity.BUILDER_TTL_S)
    build_affinity._prune(build_affinity._directory(scope, 20).parent, NOW)
    check("pruning removes a fully stale PR", build_affinity._directory(scope, 20).exists(), False)
    check("pruning keeps a live PR", build_affinity._directory(scope, 1).exists(), True)

    # Round selection: operator-named PRs, bumps and paused authoring never wait.
    note(30, "w2", at=NOW)
    note(31, "w2", at=NOW)

    def survey(*numbers, bumps=(), backpressure=False):
        sv = types.SimpleNamespace(
            needs_fix=tc.WorkKind("fix", [candidate(n) for n in numbers]),
            rebaseable=tc.WorkKind("rebase"),
            red_ci=tc.WorkKind("fix-ci"),
            open_prs=[types.SimpleNamespace(number=n, automatic_bump=n in bumps) for n in numbers],
            roadmap_backpressure=backpressure,
        )
        sv.kind = lambda name: {"fix": sv.needs_fix, "rebase": sv.rebaseable, "fix-ci": sv.red_ci}[name]
        return sv

    w = types.SimpleNamespace(cfg=types.SimpleNamespace(wid="w1"), gh=types.SimpleNamespace(repo=scope))
    opts = types.SimpleNamespace(dry_run=False, prs=(), sandbox_host=True, only=[])
    real_time = work_units.time.time
    work_units.time.time = lambda: NOW
    try:
        sv = survey(30, 31, 32, bumps={31})
        work_units._apply_build_affinity(w, sv, opts)
        check("a round defers a peer's PR but not a bump", sorted(prs(sv.needs_fix.actionable)), [31, 32])
        sv = survey(30, 32, backpressure=True)
        work_units._apply_build_affinity(w, sv, opts)
        check("paused authoring keeps deferred PRs", sorted(prs(sv.needs_fix.actionable)), [30, 32])
        sv = survey(30, 32)
        work_units._apply_build_affinity(
            w, sv, types.SimpleNamespace(dry_run=False, prs=(30,), sandbox_host=True, only=[])
        )
        check("operator-named PRs bypass affinity", prs(sv.needs_fix.actionable), [30, 32])
        sv = survey(30, 32)
        work_units._apply_build_affinity(w, sv, types.SimpleNamespace(dry_run=True, prs=(), sandbox_host=True, only=[]))
        check("dry runs bypass affinity", prs(sv.needs_fix.actionable), [30, 32])
        sv = survey(30, 32)
        work_units._apply_build_affinity(
            w, sv, types.SimpleNamespace(dry_run=False, prs=(), sandbox_host=True, only=["review"])
        )
        check("a worker not taking fixes neither defers nor starts a grace", prs(sv.needs_fix.actionable), [30, 32])
        # A prepared survey offered again after the grace brings the deferred PR back.
        note(33, "w2", at=NOW)
        sv = survey(33, 34)
        work_units._apply_build_affinity(w, sv, opts)
        check("first pass defers the peer's PR", prs(sv.needs_fix.actionable), [34])
        work_units.time.time = lambda: NOW + GRACE
        work_units._apply_build_affinity(w, sv, opts)
        check("a later pass after the grace restores it", sorted(prs(sv.needs_fix.actionable)), [33, 34])
    finally:
        work_units.time.time = real_time

    # The builder's own priority expires with the grace too, so a warm backlog cannot starve others.
    note(40, "w1", at=NOW)
    ordered, _ = build_affinity.prioritize(scope, "w1", [candidate(41), candidate(40)], now=NOW)
    check("a builder's priority within the grace", prs(ordered), [40, 41])
    ordered, _ = build_affinity.prioritize(scope, "w1", [candidate(41), candidate(40)], now=NOW + GRACE)
    check("no builder priority after the grace", prs(ordered), [41, 40])

    # Only a round whose agent built counts as building.
    state = Path(tmp) / "state"
    check("no receipts: not built", build_affinity.built_since(state, NOW), False)
    for name, started, phases in (("old", NOW - 5, {"build": {}}), ("lint", NOW + 5, {"lint": {}})):
        receipt = state / "verification" / name / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(dict(started_at=started, phases=phases)))
        os.utime(receipt.parent, (started, started))
    check(
        "an earlier build or a lint-only call is not this round's build", build_affinity.built_since(state, NOW), False
    )
    receipt = state / "verification" / "new" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(dict(started_at=NOW + 9, phases={"build": {"status": "failed"}})))
    os.utime(receipt.parent, (NOW + 9, NOW + 9))
    check("a build in this round counts, even a failed one", build_affinity.built_since(state, NOW), True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
