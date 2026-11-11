"""Metadata-only live sampling, accounting rebases and publication races. No paid calls."""

import copy
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import activity_live as live
from tauceti_worker.budget import Budget
from tauceti_worker.runtime_status import atomic_json, read_json
from tauceti_worker.usage_accounting import UsageJournal, read_summary

MODEL = "claude-opus-5-5"
NOW = 1793562000


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(
            os.environ, TAUCETI_ACTIVITY_DIR=str(self.root / "activity"), TAUCETI_FLEET_MACHINE_ID="machine"
        )
        env.start()
        self.addCleanup(env.stop)
        self.key = "a" * 32
        self.at = NOW - 120
        self.j = UsageJournal(self.root / "usage", self.key, model=MODEL, clock=lambda: self.at)
        self.response(100000)
        self.summary = read_summary(self.j.path)
        self.session = dict(
            id=self.key,
            worker="w",
            scope="machine",
            model=MODEL,
            phase="fix",
            started=NOW - 600,
            status="active",
            cost="0",
            lower_bound_cost="0",
            resume=None,
            external=False,
            live=True,
            ledger_sequence=2,
            journal=str(self.j.log),
            summary=self.summary,
        )
        self.base = dict(cost="1", worker="w", scope="machine", started=NOW - 600, observed_at=NOW - 300)
        self.view = dict(ledger_identity=[1, 2], ledger_sequence=2, observed_at=NOW, sessions={self.key: self.session})
        self.value = dict(
            all=dict(
                costs=dict(
                    accounting_version=3,
                    live_baseline=dict(
                        generation="g1", ledger_identity=[1, 2], ledger_sequence=1, sessions={self.key: self.base}
                    ),
                    spending={
                        key: dict(
                            spent_usd="11",
                            covered_seconds=3600,
                            worker_seconds=7200,
                            usd_per_hour="11",
                            usd_per_worker_hour="5.5",
                        )
                        for key in live.WINDOW_SECONDS
                    },
                )
            )
        )
        self.rates = {live.cohort(MODEL, "fix", self.summary): dict(usd_per_byte="0.001", samples=10)}

    def response(self, output):
        self.j.observe(
            dict(
                type="assistant",
                session_id="provider",
                message=dict(id="m", model=MODEL, usage=dict(input_tokens=0, output_tokens=output)),
            )
        )

    def test_metadata_growth_and_unchanged_file_freeze_without_content_reads(self):
        self.response(200000)
        with patch("tauceti_worker.usage_accounting._read", side_effect=AssertionError("no contents")):
            estimated = live.estimate(self.session, self.base, self.rates, NOW)
            later = live.estimate(self.session, self.base, self.rates, NOW + 3600)
        expected = Decimal("2") + Decimal("0.001") * (self.j.log.stat().st_size - self.summary["journal_offset"])
        self.assertEqual(Decimal(estimated["total"]), expected)
        self.assertEqual(estimated["source"], "journal-size")
        self.assertEqual(estimated["total"], later["total"])
        self.assertEqual(estimated["observed_at"], NOW - 120)

    def test_summary_anchor_duplicate_counters_and_missing_cohort(self):
        self.response(100000)
        result = live.estimate(self.session, self.base, {}, NOW)
        self.assertEqual(Decimal(result["total"]), 2)
        self.assertEqual(result["source"], "usage-summary")
        self.j.flush()
        self.assertEqual(read_summary(self.j.path)["cost"], "2.0")

    def test_resume_unknown_initialization_and_invalid_files_keep_known_prefix(self):
        self.session["resume"] = "provider"
        self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["total"], "1")
        self.session["resume"] = None
        for summary in (None, {**self.summary, "usage_seen": False}):
            self.session["summary"] = summary
            self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["total"], "1")
        self.session["summary"] = self.summary
        self.j.log.write_bytes(b"")
        self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["source"], "journal-unavailable")
        self.j.log.rename(self.root / "previous-journal")
        self.j.log.write_bytes(b"x" * (self.summary["journal_offset"] + 1000))
        self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["source"], "journal-unavailable")
        self.j.log.unlink()
        self.j.log.symlink_to(self.j.path)
        self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["source"], "journal-unavailable")
        self.j.log.unlink()
        self.assertEqual(live.estimate(self.session, self.base, self.rates, NOW)["total"], "1")

    def test_calibration_requires_native_sample_cohorts_and_enough_observations(self):
        row = dict(
            model=MODEL,
            phase="fix",
            cost="2",
            bytes=100,
            **{k: self.summary[k] for k in ("journal_format", "prices_sha256")},
        )
        self.assertFalse(live.calibrate([row, row]))
        rates = live.calibrate([row, row, {**row, "cost": "4"}])
        self.assertEqual(Decimal(rates[live.cohort(MODEL, "fix", row)]["usd_per_byte"]), Decimal(".02"))
        self.assertIn(live.cohort(MODEL, None, row), rates)
        self.assertNotIn(live.cohort(MODEL, "review", row), rates)

    def total(self, value, key="total"):
        return Decimal(value["all"]["costs"]["spending_including_live"][key]["spent_usd"])

    def test_overlay_adds_only_missing_prefix_and_repeated_polls_do_not_duplicate(self):
        output, state = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        self.assertEqual(self.total(output), 12)
        output2, state = live.apply_overlay(self.value, self.view, {}, state, NOW + 60)
        self.assertEqual(self.total(output2), 12)
        self.assertEqual(output2["all"]["costs"]["spending"]["total"]["spent_usd"], "11")
        self.assertEqual(Decimal(output2["all"]["costs"]["spending_including_live"]["total"]["usd_per_worker_hour"]), 6)
        baseline = copy.deepcopy(self.value)
        baseline["all"]["costs"]["spending"]["total"]["spent_usd"] = "12"
        baseline["all"]["costs"]["live_baseline"].update(generation="g2", ledger_sequence=2)
        baseline["all"]["costs"]["live_baseline"]["sessions"][self.key]["cost"] = "2"
        output, state = live.apply_overlay(baseline, self.view, {}, state, NOW + 120)
        self.assertEqual(self.total(output), 12)

    def test_native_total_can_lower_heuristic_and_terminal_survives_until_baseline(self):
        self.response(200000)
        output, state = live.apply_overlay(self.value, self.view, dict(rates=self.rates), {}, NOW)
        self.assertGreater(self.total(output), 12)
        self.session.update(status="settled", cost="1.5", finished=NOW + 10, live=False)
        output, state = live.apply_overlay(self.value, self.view, {}, state, NOW + 60)
        self.assertEqual(self.total(output), Decimal("11.5"))
        output, state = live.apply_overlay(self.value, self.view, {}, state, NOW + 120)
        self.assertEqual(self.total(output), Decimal("11.5"))
        baseline = copy.deepcopy(self.value)
        baseline["all"]["costs"]["live_baseline"].update(generation="g2", ledger_sequence=2, sessions={})
        baseline["all"]["costs"]["spending"]["total"]["spent_usd"] = "11.5"
        output, state = live.apply_overlay(baseline, self.view, {}, state, NOW + 180)
        self.assertEqual(self.total(output), Decimal("11.5"))
        self.assertFalse(state["sessions"])

    def test_negative_native_correction_without_prior_overlay(self):
        self.session.update(status="settled", cost=".5", finished=NOW, live=False)
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        self.assertEqual(self.total(output), Decimal("10.5"))

    def test_downward_native_correction_preserves_original_window_allocation(self):
        self.base["windows"] = {key: "1" for key in live.WINDOW_SECONDS}
        self.base["windows"]["1h"] = "0"
        self.value["all"]["costs"]["spending"]["1h"]["spent_usd"] = "0"
        self.session.update(status="settled", cost=".5", finished=NOW, live=False)
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        self.assertEqual(self.total(output, "1h"), 0)
        self.assertEqual(self.total(output, "total"), Decimal("10.5"))

    def test_interval_windows_generation_identity_and_scope(self):
        self.base["observed_at"] = NOW - 7200
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        self.assertEqual(self.total(output, "1h"), Decimal("11.5"))
        self.assertEqual(self.total(output, "3h"), Decimal("12"))
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW, scope="elsewhere")
        self.assertEqual(self.total(output), 11)
        bad = {**self.view, "ledger_identity": [1, 3]}
        output, _ = live.apply_overlay(self.value, bad, {}, {}, NOW)
        self.assertNotIn("spending_including_live", output["all"]["costs"])
        output, _ = live.apply_overlay(self.value, {**self.view, "observed_at": NOW - 1000}, {}, {}, NOW)
        self.assertTrue(output["all"]["costs"]["live_estimate"]["stale"])

    def test_manager_samples_once_a_minute_and_stats_once_across_all_scopes(self):
        path = live.root() / "summary.json"
        group = live.root() / "groups" / "machine.json"
        live.publish_report(path, self.value)
        live.publish_report(group, {**self.value, "live_subject": dict(scope="machine")})
        live.publish_inputs(self.value, [path, group])
        directory = self.root / "budget"
        atomic_json(directory / "live-sessions.json", self.view)
        original = Path.lstat
        count = []

        def lstat(path, *args, **kwargs):
            if path == self.j.log:
                count.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            live.sample(budget_directory=directory, now=NOW)
            live.sample(budget_directory=directory, now=NOW + 59)
        self.assertEqual(len(count), 1)
        self.assertEqual(self.total(read_json(path)), 12)
        self.assertEqual(self.total(read_json(group)), 12)
        self.assertEqual(read_json(path)["all"]["costs"]["live_estimate"]["observed_at"], NOW)

    def test_new_baseline_wins_publication_race(self):
        path = live.root() / "summary.json"
        live.publish_report(path, self.value)
        live.publish_inputs(self.value, [path])
        directory = self.root / "budget"
        atomic_json(directory / "live-sessions.json", self.view)
        original = live.apply_overlay

        def apply(*args, **kwargs):
            value, state = original(*args, **kwargs)
            fresh = copy.deepcopy(self.value)
            fresh["all"]["costs"]["live_baseline"]["generation"] = "g2"
            live.publish_report(path, fresh)
            return value, state

        with patch.object(live, "apply_overlay", side_effect=apply):
            live.sample(budget_directory=directory, now=NOW)
        result = read_json(path)["all"]["costs"]
        self.assertEqual(result["live_baseline"]["generation"], "g2")
        self.assertNotIn("spending_including_live", result)

    def test_stale_inputs_and_summary_cannot_reduce_a_new_prepared_prefix(self):
        inputs = dict(generation="older")
        output, _ = live.apply_overlay(self.value, self.view, inputs, {}, NOW)
        self.assertNotIn("spending_including_live", output["all"]["costs"])
        old = {self.key: dict(total=".5", observed=".5", source="known-prefix", observed_at=NOW - 120)}
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW, estimates=old)
        self.assertEqual(self.total(output), 11)

    def test_historical_publication_carries_estimates_without_resampling(self):
        path = live.root() / "summary.json"
        output, state = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        carry = dict(view=self.view, overlay=dict(observed_at=NOW, reports={str(path): state}))
        fresh = copy.deepcopy(self.value)
        fresh["all"]["costs"]["live_baseline"].update(generation="g2", ledger_sequence=3)
        with patch.object(Path, "lstat", side_effect=AssertionError("publication must not sample journals")):
            live.publish_report(path, fresh, carry=carry)
        published = read_json(path)
        self.assertEqual(self.total(published), 12)
        self.assertEqual(published["all"]["costs"]["live_estimate"]["observed_at"], NOW)
        self.assertNotIn("spending_including_live", read_json(live.baseline_path(path))["all"]["costs"])

    def test_new_baseline_does_not_double_count_older_active_view(self):
        fresh = copy.deepcopy(self.value)
        fresh["all"]["costs"]["live_baseline"].update(generation="g2", ledger_sequence=3, sessions={})
        fresh["all"]["costs"]["spending"]["total"]["spent_usd"] = "12"
        output, _ = live.apply_overlay(fresh, self.view, {}, {}, NOW)
        self.assertEqual(self.total(output), 12)

    def test_recovered_terminal_string_timestamp_can_mix_with_live_summary(self):
        other = "b" * 32
        self.view["sessions"][other] = {
            **self.session,
            "id": other,
            "status": "settled",
            "cost": "1",
            "finished": str(NOW),
        }
        output, _ = live.apply_overlay(self.value, self.view, {}, {}, NOW)
        self.assertEqual(output["all"]["costs"]["live_estimate"]["oldest_usage_at"], NOW - 120)

    def test_supervisor_terminal_view_is_retained_then_acknowledged(self):
        b = Budget(self.root / "budget", clock=lambda: NOW)
        b.configure(grant=100)
        b.admit(self.key, "w", MODEL, "fix")
        cache = {}
        live.publish_sessions(b, cache, NOW)
        b.settle(self.key, 2)
        live.publish_sessions(b, cache, NOW + 60)
        terminal = read_json(b.directory / "live-sessions.json")["sessions"][self.key]
        b.configure(grant=1)
        live.publish_sessions(b, cache, NOW + 120)
        self.assertEqual(
            read_json(b.directory / "live-sessions.json")["sessions"][self.key]["ledger_sequence"],
            terminal["ledger_sequence"],
        )
        atomic_json(live.root() / "live-inputs.json", dict(ledger_sequence=terminal["ledger_sequence"], sessions={}))
        live.publish_sessions(b, cache, NOW + 180)
        self.assertFalse(read_json(b.directory / "live-sessions.json")["sessions"])

    def test_short_invocation_settled_between_samples_stays_visible(self):
        b = Budget(self.root / "budget", clock=lambda: NOW)
        b.configure(grant=100)
        cache = {}
        live.publish_sessions(b, cache, NOW)
        b.admit(self.key, "w", MODEL, "fix")
        b.settle(self.key, 2)
        with b.locked():
            sequence = b.current()["seq"]
        # The existing launch-sidecar pass supplies this terminal notification.
        cache["live_pending"] = {self.key: dict(cost="2", ledger_sequence=sequence)}
        live.publish_sessions(b, cache, NOW + 60)
        result = read_json(b.directory / "live-sessions.json")
        self.assertEqual(result["sessions"][self.key]["status"], "settled")
        self.assertEqual(result["sessions"][self.key]["cost"], "2")


if __name__ == "__main__":
    unittest.main()
