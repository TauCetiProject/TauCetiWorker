"""Safety and attribution checks for the one-day sizing script (no fleet access)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("worker_sizing", Path(__file__).parents[1] / "scripts/worker_sizing.py")
sizing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sizing)


def observation(at, stall=20, oom=100, events=()):
    return dict(
        at=at, status_at=at, status_error=None, memory=dict(stall_full=stall, oom_kill=oom), oom_events=list(events)
    )


class Safety(unittest.TestCase):
    def simulate(self, plan, external=False, bad_high=False):
        clock = {"at": 1000, "maximum": 40, "peak": 40, "active": 40, "settled": 0}
        changes = []

        def control(maximum):
            changes.append(maximum)
            clock.update(maximum=maximum, peak=max(maximum, clock["peak"]), settled=clock["at"] + 90)

        def sample():
            if external and clock["at"] >= 1120:
                clock["maximum"] = 47
            if clock["at"] >= clock["settled"]:
                clock["active"] = clock["maximum"]
            s = observation(clock["at"], stall=50 if bad_high and clock["maximum"] >= 80 else 5)
            s.update(
                maximum=clock["maximum"],
                enabled=True,
                draining=False,
                admitted=clock["maximum"],
                activity={"prepared_at": clock["at"]},
                workers=[{"state": "running" if i < clock["active"] else "idle"} for i in range(clock["peak"])],
            )
            return s

        def sleep(seconds):
            clock["at"] += seconds

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with (
                patch.object(sizing, "PLAN", plan),
                patch.object(sizing, "sample", sample),
                patch.object(sizing, "control", control),
                patch.object(sizing, "report"),
                patch.object(sizing, "queue_sample"),
                patch.object(sizing.time, "time", lambda: clock["at"]),
                patch.object(sizing.time, "monotonic", lambda: clock["at"]),
                patch.object(sizing.time, "sleep", sleep),
            ):
                sizing.run(directory, 600)
            return sizing.read(directory / "state.json"), sizing.rows(directory / "samples.jsonl"), changes

    def test_schedule_measures_full_blocks_after_gradual_reduction_and_restores_40(self):
        state, samples, changes = self.simulate([40, 80, 40, 20])
        self.assertEqual(state["status"], "complete")
        self.assertEqual(changes, [80, 40, 20, 40])
        self.assertTrue(any(s["segment"] == "transition" and s["block"] == 2 for s in samples))
        for block in state["blocks"]:
            self.assertGreaterEqual(block["ended"] - block["started"], 600)

    def test_external_control_stops_schedule_without_overwriting_operator(self):
        state, _, changes = self.simulate([40, 48], external=True)
        self.assertEqual(state["status"], "stopped: external fleet control")
        self.assertEqual(changes, [])

    def test_bad_80_trial_stops_early_but_still_allows_60(self):
        state, _, changes = self.simulate([40, 80, 40, 60], bad_high=True)
        self.assertEqual(state["rejected_ceiling"], 80)
        self.assertIn("aborted", state["blocks"][1])
        self.assertEqual(state["blocks"][3]["target"], 60)
        self.assertEqual(changes, [80, 40, 60, 40])

    def test_sustained_stalls_trigger_but_transient_stalls_do_not(self):
        samples = [observation(t, 45) for t in range(0, 301, 30)]
        self.assertIn("five minutes", sizing.guard(samples, 20))
        samples[5]["memory"]["stall_full"] = 20
        self.assertIsNone(sizing.guard(samples, 20))

    def test_oom_events_are_new_victims_not_historical_totals(self):
        samples = [observation(0, events=("old",)), observation(30, events=("old", "new1", "new2"))]
        self.assertIn("userspace OOM", sizing.guard(samples, 20))
        samples[-1]["oom_events"] = ["old"]
        self.assertIsNone(sizing.guard(samples, 20))
        samples[-1]["memory"]["oom_kill"] = 102
        self.assertIn("kernel OOM", sizing.guard(samples, 20))

    def test_missing_observations_cannot_establish_sustained_pressure(self):
        self.assertIsNone(sizing.guard([observation(0, 50), observation(300, 50)], 20))

    def test_downscale_waits_for_active_rounds_but_not_idle_processes(self):
        s = observation(0)
        s.update(admitted=32, workers=[dict(state="running") for _ in range(40)])
        self.assertFalse(sizing.ready(s, 32, 40))
        s["workers"][-8:] = [dict(state="idle") for _ in range(8)]
        self.assertTrue(sizing.ready(s, 32, 40))
        # Also applies after a safety return to 40, before the following block.
        self.assertFalse(sizing.ready(dict(s, admitted=40, workers=[dict(state="running") for _ in range(48)]), 40, 40))

    def test_phase_change_does_not_receive_an_entire_interval_of_cpu(self):
        def worker(phase, cpu):
            return dict(
                id="w",
                phase=phase,
                state="running",
                scope_unit="same",
                resources={
                    "cpu": {"usage_usec": cpu},
                    "memory.current": 1024**3,
                    "memory.swap.current": 0,
                    "pressure": {},
                    "io": {},
                },
            )

        a, b = observation(0), observation(30)
        a["workers"], b["workers"] = [worker("review", 0)], [worker("fix", 30_000_000)]
        totals, excluded = sizing.phase_metrics([a, b])
        self.assertEqual(dict(totals), {})
        self.assertEqual(excluded, 1)
        b["workers"][0]["phase"] = "review"
        totals, excluded = sizing.phase_metrics([a, b])
        self.assertEqual(totals["review"]["cpu_seconds"], 30)
        self.assertEqual(excluded, 0)


class ReviewAttribution(unittest.TestCase):
    def test_repeats_contests_and_new_heads_are_separate(self):
        def record(at, head, mode="commit", ran=None):
            return dict(at=at, pr=7, round=at, head=head, mode=mode, ran=["reuse"] if ran is None else ran)

        counts = sizing.review_counts(
            [
                record(1, "old"),
                record(2, "old"),
                record(3, "old", "reply"),
                record(4, "new"),
                record(5, "new", ran=[]),
                record(10, "outside"),
            ],
            1,
            10,
        )
        self.assertEqual(
            counts,
            dict(
                published_operations=5,
                distinct_pr_heads=2,
                repeated_operations=3,
                contest_operations=1,
                unclassified_operations=0,
                inference_operations=4,
                rubric_inferences=4,
            ),
        )

    def test_publication_counts_require_matching_production_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "worker/home/.cache/tauceti-review/worker/store/TauCetiProject__TauCeti"
            for kind in ("posts", "rounds"):
                (store / "outbox/records" / kind / "7").mkdir(parents=True)
            post = dict(
                posted_at="2026-11-09T00:00:00Z",
                repo="TauCetiProject/TauCeti",
                pr=7,
                head_sha="head",
                round=1,
                scoreboard_comment_id=42,
                threads={},
            )
            round_ = dict(repo="TauCetiProject/TauCeti", head_sha="head", arm="production", mode="reply", ran=["reuse"])
            post_path = store / "outbox/records/posts/7/7-1-20261109T000000.json"
            round_path = store / "outbox/records/rounds/7/7-1.json"
            post_path.write_text(json.dumps(post))
            round_path.write_text(json.dumps(round_))
            records = sizing.published_reviews(root, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["mode"], "reply")
            round_path.write_text(json.dumps({**round_, "arm": "shadow"}))
            self.assertEqual(sizing.published_reviews(root, 0), [])
            # A replaced round cannot lend its new contest/inference metadata
            # to an older, still confirmed publication receipt.
            round_path.write_text(json.dumps({**round_, "head_sha": "different"}))
            records = sizing.published_reviews(root, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual((records[0]["mode"], records[0]["ran"]), ("unknown", []))

    def test_a_commit_review_can_also_publish_a_contest_reply(self):
        counts = sizing.review_counts(
            [
                dict(at=1, pr=7, head="head", mode="commit", ran=["reuse"], threads={"reuse:reply": 99}),
            ],
            0,
            2,
        )
        self.assertEqual(counts["contest_operations"], 1)


if __name__ == "__main__":
    unittest.main()
