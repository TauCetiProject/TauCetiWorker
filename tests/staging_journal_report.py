#!/usr/bin/env python3
"""The staging journal report classifies real candidates the way the triage spec counts them.

The fixture holds 21 real candidates reduced from the 2026-11-03 and 2026-11-04 feed snapshots
(see `scripts/staging_journal_report.py --reduce`). Expected numbers below were checked by hand
against the raw snapshots: dispatch and terminal timestamps, member counts, retry reasons and the
partition and repair events of each candidate.
"""

import copy
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import staging_journal_report as sjr  # noqa: E402

FIXTURE = REPO / "tests/fixtures/staging-journal-fixture.json"


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def load():
    return json.loads(FIXTURE.read_text())


def report_for(snapshots, **window):
    merged = sjr.merge_snapshots(snapshots, [f"s{i}" for i in range(len(snapshots))])
    return merged, sjr.analyse(merged, sjr.select_window(merged["candidates"], **window))


def test_fixture_shape():
    doc = load()
    assert doc["schema"] == sjr.FEED_SCHEMA, doc["schema"]
    assert FIXTURE.stat().st_size < 150_000, FIXTURE.stat().st_size
    ids = sorted(c["id"] for c in doc["candidates"])
    assert ids == [
        1843, 1861, 1862, 1863, 1864, 1866, 1876, 1883, 1884, 1885, 1894,
        1906, 1908, 1911, 1914, 1917, 1918, 1920, 1929, 1936, 1937,
    ], ids  # fmt: skip
    # The reducer keeps only what the report reads: no status polls, trimmed members.
    kinds = {e["kind"] for c in doc["candidates"] for e in c["events"]}
    assert not kinds & sjr.DROPPED_EVENT_KINDS, kinds & sjr.DROPPED_EVENT_KINDS
    for cand in doc["candidates"]:
        for member in cand["snapshot"]["members"]:
            assert set(member) == set(sjr.MEMBER_FIELDS), member
    # Regenerating from the fixture itself is a fixed point of the reducer.
    merged = sjr.merge_snapshots([doc], ["fixture"])
    again = sjr.reduce_snapshot(merged, [c["id"] for c in doc["candidates"]])
    assert again["candidates"] == doc["candidates"]
    assert again["admissions"] == doc["admissions"]


def test_slot_occupancy():
    _merged, report = report_for([load()])
    rows = report["slot_occupancy"]["rows"]
    assert report["window"]["runs"] == 19 and report["window"]["non_runs"] == 2, report["window"]
    # 1861 (7.3) 1862 (26.3) 1863 (32.7) 1864 (26.6) 1876 (14.4) 1918 (54.6): 161.9 minutes.
    assert rows["large_failed"]["candidates"] == [1861, 1862, 1863, 1864, 1876, 1918]
    assert rows["large_failed"]["slot_minutes"] == 161.9, rows["large_failed"]
    assert rows["large_promoted"]["candidates"] == [1894] and rows["large_promoted"]["slot_minutes"] == 24.2
    assert rows["large_cancelled_or_invalidated"]["runs"] == 0
    # 1885 (14.8) 1920 (23.8) 1929 (33.6)
    assert rows["singleton_promoted"]["candidates"] == [1885, 1920, 1929]
    assert rows["singleton_promoted"]["slot_minutes"] == 72.2, rows["singleton_promoted"]
    # 1884 invalidated (1.2) 1906 invalidated (41.5) 1908 failed (68.3) 1911 failed (10.4) 1937 cancelled (19.4)
    assert rows["singleton_failed_or_invalidated"]["candidates"] == [1884, 1906, 1908, 1911, 1937]
    assert rows["singleton_failed_or_invalidated"]["slot_minutes"] == 140.8
    # 1843 failed (7.7) 1866 promoted (22.5) 1914 failed (48.3) 1936 cancelled (22.6)
    assert rows["small_any"]["candidates"] == [1843, 1866, 1914, 1936]
    assert rows["small_any"]["slot_minutes"] == 101.1
    assert rows["no_outcome"]["runs"] == 0
    assert report["slot_occupancy"]["total_slot_minutes"] == 500.2
    assert abs(sum(r["share"] for r in rows.values()) - 1.0) < 0.01
    # Two runs never got a terminal_observed; their trusted failure_classified closes them.
    assert report["slot_occupancy"]["timing_sources"] == {
        "dispatch_to_terminal": 17,
        "dispatch_to_failure_classified": 2,
    }
    by_id = {r["id"]: r for r in report["runs"]}
    assert by_id[1862]["timing_source"] == "dispatch_to_failure_classified" and by_id[1862]["minutes"] == 26.3
    assert by_id[1911]["timing_source"] == "dispatch_to_failure_classified" and by_id[1911]["minutes"] == 10.4
    assert by_id[1862]["dispatched_at"] == "2026-11-03T06:00:50Z" and by_id[1862]["ended_at"] == "2026-11-03T06:27:06Z"
    assert by_id[1884]["outcome"] == "invalidated" and by_id[1936]["outcome"] == "cancelled"
    assert report["slot_occupancy"]["red_run_minutes"]["median"] == 26.5
    assert report["slot_occupancy"]["green_run_minutes"]["count"] == 5


def test_singleton_kinds():
    _merged, report = report_for([load()])
    kinds = report["singletons"]["by_kind"]
    assert kinds["conflict"]["candidates"] == [1884, 1885, 1920], kinds["conflict"]
    assert kinds["confirmation"]["candidates"] == [1906, 1908, 1911]
    assert kinds["remainder"]["candidates"] == [1937]
    assert kinds["admitted_alone"]["candidates"] == [1929]
    assert kinds["bisection"]["runs"] == 0 and kinds["unknown"]["runs"] == 0
    assert kinds["conflict"]["promoted"] == 2 and kinds["conflict"]["cancelled_or_invalidated"] == 1
    evidence = {r["id"]: r["singleton_evidence"] for r in report["runs"] if r["size"] == 1}
    # conflict_isolation child of a conflict singleton whose local_conflict names the PR
    assert evidence[1920] == "conflict_isolation; parent 1883 local_conflict names PR 25514", evidence[1920]
    # structured_conflict whose parent is not loaded: still a conflict, with the gap recorded
    assert evidence[1884] == "structured_conflict; parent candidate 1879 not in the journal"
    # neutral retries inherit the parent's kind
    assert evidence[1885].startswith("train_invalidated retry of candidate 1884: structured_conflict")
    assert evidence[1908].startswith("train_invalidated retry of candidate 1906: compiler_diagnostics")
    # a neutral retry of a larger parent is a remainder
    assert evidence[1937] == "cancellation retry of candidate 1936 (2 members)"
    assert report["singletons"]["per_day"] == {
        "2026-11-03": {"conflict": 3, "confirmation": 3, "admitted_alone": 1, "total": 7},
        "2026-11-04": {"remainder": 1, "total": 1},
    }, report["singletons"]["per_day"]


def test_lineages_and_failed_rounds():
    _merged, report = report_for([load()])
    fr = report["failed_rounds"]
    assert fr["large_failed_runs"] == 6 and fr["large_promoted_runs"] == 1 and fr["window_aggregate"] == 6.0, fr
    assert fr["complete_large_lineages"] == 0 and fr["complete_lineage_aggregate"] is None
    trees = {t["root"]: t for t in report["lineages"]}
    # 1861 -> 1862 (handoff 26251, remainder 1863) -> 1864 and 1894 -> 1866 and 1914
    t = trees[1861]
    assert t["candidates"] == [1861, 1862, 1863, 1864, 1866, 1894, 1914]
    assert t["failed_rounds"] == 5 and [p["size"] for p in t["promoted_runs"]] == [13, 26]
    assert t["landed_prs"] == 39 and t["handed_off_prs"] == [26251]
    assert t["open_leaves"] == [1914] and not t["complete"] and t["failed_per_promoted"] == 2.5
    # 1876 (bisection) -> 1917 (superseded generation) and 1918 (failed, handoff, remainder outside window)
    t = trees[1876]
    assert t["root_parent_outside_window"] == 1874 and t["candidates"] == [1876, 1917, 1918]
    assert t["runs"] == 2 and t["failed_rounds"] == 2 and t["open_leaves"] == [1918]
    # 1906 -> 1908: the quarantined head closes the lineage
    t = trees[1906]
    assert t["candidates"] == [1906, 1908] and t["quarantined_prs"] == [26442] and t["complete"]
    # 1883 (construction conflict, non-run) -> 1920 promoted
    t = trees[1883]
    assert t["runs"] == 1 and t["landed_prs"] == 1 and t["complete"]
    assert report["non_runs"] == {"superseded before dispatch": 1, "construction conflict": 1}


def test_bisections_partitions_and_handoffs():
    _merged, report = report_for([load()])
    assert [b["candidate"] for b in report["bisections"]] == [1843, 1876]
    assert report["bisections"][1]["group_sizes"] == [62, 62]
    assert report["bisections"][1]["unattributed_lines"] == []
    assert report["bisections"][0]["unattributed_lines"] == [
        {"kind": "compiler_error", "paths": ["TauCeti/RingTheory/PowerSeries/Substitution.lean"]}
    ]
    splits = report["component_splits"]
    assert [s["candidate"] for s in splits] == [1863, 1864, 1914]
    assert all(s["unattributed_lines"][0]["unknown"] == "unsupported_origin" for s in splits), splits
    assert [q["candidate"] for q in report["indivisible_quarantines"]] == [1883, 1908, 1911]
    assert report["per_day"]["2026-11-03"] == {
        "singleton_runs": 7,
        "bisections": 2,
        "component_splits": 3,
        "indivisible_quarantines": 3,
    }, report["per_day"]
    parts = report["partitions"]
    assert parts["count"] == 11 and parts["handoffs"] == 2
    assert parts["suspects_distribution"] == {"0": 8, "1": 3}
    assert parts["by_strategy"] == {
        "deterministic_bisection": 5,
        "balanced_dependency_components": 3,
        "compiler_diagnostics": 2,
        "missing_import": 1,
    }
    by_cand = {p["candidate"]: p for p in parts["events"]}
    assert by_cand[1861]["suspect_prs"] == [26274] and not by_cand[1861]["handed_off"]
    assert by_cand[1862]["suspect_prs"] == [26251] and by_cand[1862]["handed_off"]
    h = report["handoffs"]
    assert h["count"] == 2 and h["readmitted"] == 1 and h["still_withdrawn_prs"] == [23638]
    readmitted = h["events"][0]
    assert readmitted["pr"] == 26251 and readmitted["minutes"] == 1294.2
    assert readmitted["evidence"] == "admission 32365 at 42f0712641ff"
    assert h["events"][1]["readmitted_at"] is None


def test_readmission_from_candidate_membership():
    # A later candidate carrying the suspect at a new head counts as the readmission,
    # timed by the member's own admission when it is after the handoff.
    doc = load()
    cand = copy.deepcopy(next(c for c in doc["candidates"] if c["id"] == 1929))
    handoff = next(
        e for c in doc["candidates"] if c["id"] == 1918 for e in c["events"] if e["kind"] == "provisional_repair"
    )
    cand["id"] = 9999
    cand["observed_at"] = handoff["observed_at"] + 120 * 60_000_000
    cand["snapshot"]["members"] = [
        {
            "pr": 23638,
            "head_sha": "f" * 40,
            "source": "bors.admission",
            "retry_reason": None,
            "parent_candidate_id": None,
            "original_admitted_at": handoff["observed_at"] + 90 * 60_000_000,
        }
    ]
    doc["candidates"].append(cand)
    _merged, report = report_for([doc], to_id=1937)
    event = next(e for e in report["handoffs"]["events"] if e["pr"] == 23638)
    assert event["minutes"] == 90.0 and event["evidence"] == "member of candidate 9999 at ffffffffffff", event
    assert report["handoffs"]["still_withdrawn"] == 0
    # The same head is not a readmission.
    cand["snapshot"]["members"][0]["head_sha"] = handoff["details"]["head_sha"]
    _merged, report = report_for([doc], to_id=1937)
    assert next(e for e in report["handoffs"]["events"] if e["pr"] == 23638)["readmitted_at"] is None


def test_multi_snapshot_merge_prefers_later():
    full = load()
    early = copy.deepcopy(full)
    # In the earlier snapshot 1918 had been dispatched but had no outcome yet.
    for cand in early["candidates"]:
        if cand["id"] == 1918:
            cand["events"] = [e for e in cand["events"] if e["kind"] in ("dispatch_requested", "dispatched")]
            cand["observed_at"] = cand["events"][0]["observed_at"]
    early["candidates"] = [c for c in early["candidates"] if c["id"] < 1920]
    early["admissions"] = []
    assert sjr.snapshot_time(early) < sjr.snapshot_time(full)
    _merged, alone = report_for([early])
    assert alone["unclassified"]["candidates"] == [{"id": 1918, "reason": "dispatched, no outcome in journal"}]
    assert alone["slot_occupancy"]["rows"]["no_outcome"]["runs"] == 1
    for order in ([early, full], [full, early]):
        merged, report = report_for(order)
        assert merged["origin"][1918] == ("s1" if order[0] is early else "s0"), merged["origin"][1918]
        assert report["unclassified"]["count"] == 0
        assert 1918 in report["slot_occupancy"]["rows"]["large_failed"]["candidates"]
        assert report["window"]["candidates"] == 21
    assert merged["inputs"][-1]["observed_through"] == sjr.iso(sjr.snapshot_time(full))


def test_window_filters():
    merged = sjr.merge_snapshots([load()], ["fixture"])
    cands = merged["candidates"]
    assert sorted(sjr.select_window(cands, from_id=1861, to_id=1866)) == [1861, 1862, 1863, 1864, 1866]
    # --since/--until apply to the dispatch time, or observed_at for never-dispatched candidates.
    since = sjr.parse_utc("2026-11-03T09:00:00Z")
    until = sjr.parse_utc("2026-11-03T10:00Z")
    chosen = sjr.select_window(cands, since=since, until=until)
    assert sorted(chosen) == [1876, 1883, 1884, 1885], sorted(chosen)
    assert sjr.parse_utc("2026-11-03") == sjr.parse_utc("2026-11-03T00:00:00+00:00")
    report = sjr.analyse(merged, sjr.select_window(cands, from_id=1906, to_id=1911))
    assert report["window"]["first_candidate"] == 1906 and report["window"]["last_candidate"] == 1911
    assert report["slot_occupancy"]["rows"]["singleton_failed_or_invalidated"]["runs"] == 3
    # Lineage parents outside the window are reported, not followed.
    assert {t["root"]: t["root_parent_outside_window"] for t in report["lineages"]} == {1906: 1895, 1911: 1905}


def test_unclassified_accounting():
    doc = load()
    cands = {c["id"]: c for c in doc["candidates"]}
    # Green terminal without promotion, and a dispatched candidate with nothing after dispatch.
    for e in cands[1929]["events"]:
        if e["kind"] == "promoted":
            e["kind"] = "promotion_requested"
    cands[1866]["events"] = [e for e in cands[1866]["events"] if e["kind"] in ("dispatched", "dispatch_requested")]
    # A candidate with no dispatch and no construction record at all.
    cands[1917]["events"] = []
    _merged, report = report_for([doc])
    reasons = {c["id"]: c["reason"] for c in report["unclassified"]["candidates"]}
    assert reasons == {
        1866: "dispatched, no outcome in journal",
        1917: "no dispatch and no construction record",
        1929: "green terminal without promotion",
    }, reasons
    assert report["slot_occupancy"]["rows"]["no_outcome"]["candidates"] == [1866, 1929]
    assert report["slot_occupancy"]["rows"]["small_any"]["candidates"] == [1843, 1914, 1936]
    assert report["non_runs"] == {"construction conflict": 1}
    md = sjr.render_markdown(report)
    assert "3 unclassified: 1866 (dispatched, no outcome in journal); 1917 (no dispatch" in md, md


def test_runner_lifecycle_preferred_when_present():
    doc = load()
    cand = next(c for c in doc["candidates"] if c["id"] == 1894)
    cand["runner_started_at"] = cand["observed_at"]
    cand["runner_finished_at"] = cand["observed_at"] + 10 * 60_000_000
    _merged, report = report_for([doc])
    run = next(r for r in report["runs"] if r["id"] == 1894)
    assert run["timing_source"] == "runner_lifecycle" and run["minutes"] == 10.0, run


def test_markdown_and_json_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        md_path, json_path = Path(tmp, "report.md"), Path(tmp, "out", "report.json")
        rc = sjr.main(["--journal", str(FIXTURE), "--markdown", str(md_path), "--json", str(json_path)])
        assert rc == 0
        report = json.loads(json_path.read_text())
        assert report["schema"] == sjr.SCHEMA
        for key in (
            "slot_occupancy",
            "singletons",
            "failed_rounds",
            "lineages",
            "bisections",
            "partitions",
            "handoffs",
            "unclassified",
            "runs",
        ):
            assert key in report, key
        md = md_path.read_text()
        assert md.startswith("# Staging journal report: candidates 1843 to 1937\n")
        assert "| Large batch, failed | 6 | 162 | 32% |" in md, md
        assert "| Singleton, promoted | 3 | 72 | 14% |" in md
        assert "| Total | 19 | 500 | 100% |" in md
        assert "| conflict | 3 | 40 | 2 | 0 | 1 | 1884, 1885, 1920 |" in md
        assert "Window aggregate: 6 failed large runs / 1 promoted large runs = 6.0." in md
        assert "| 1861 | 57 | 7 | 7 | 5 | 0 | 13, 26 | 39 | 26251 |  | no (open: 1914) | 2.5 |" in md
        assert (
            "candidate 1876 (124 members) split [62, 62]" in md
            and "none recorded (the parser produced no observation)" in md
        )
        assert "1 still withdrawn as of 2026-11-04T04:05:58Z: PRs 23638." in md
        assert "No unclassified candidates." in md
        # The reducer round-trips through the command line too.
        fixture_out = Path(tmp, "fixture.json")
        rc = sjr.main(
            ["--journal", str(FIXTURE), "--reduce", "--candidates", "1843,1929", "--output", str(fixture_out)]
        )
        assert rc == 0
        reduced = json.loads(fixture_out.read_text())
        assert [c["id"] for c in reduced["candidates"]] == [1929, 1843] and reduced["admissions"] == []


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            failures += check(name, fn)
    print("staging_journal_report: all passed" if not failures else f"staging_journal_report: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
