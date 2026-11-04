#!/usr/bin/env python3
"""The Bors-compatible feeds (tauceti_queue.feeds) against the schema fixture derived from saved
production responses.

Covered: the committed schema accepts the committed production sample and (when the operator's
saved responses are present at ~/tmp/bors-journal) the full responses; the validator rejects a
missing key, an unknown key and a type change; feeds generated from a fake controller view validate
with the same top-level keys, schema strings and timestamp units as Bors; the generated observation
satisfies the TauCetiTools `bors_observation.py` and `bors_scope_approval.py` readers that the
scope-approval check and the workers use; and a `batch_id` query fills `requested_batch`.
"""

import copy
import importlib.util
import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import feeds  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
SAVED = pathlib.Path.home() / "tmp" / "bors-journal"
TAUCETI = "TauCetiProject/TauCeti"
HEAD = "a" * 40
BASE = "b" * 40
TIP = "c" * 40
MERGE_BASE = "d" * 40


class Schema(unittest.TestCase):
    def setUp(self):
        self.schema = feeds.load_schema()
        self.sample = json.loads((FIXTURES / "bors-feeds-sample.json").read_text())

    def test_fixture_is_small_and_names_its_sources(self):
        self.assertLess((FIXTURES / "bors-feeds-schema.json").stat().st_size, 200 * 1024)
        self.assertLess((FIXTURES / "bors-feeds-sample.json").stat().st_size, 200 * 1024)
        self.assertEqual(set(self.schema), {"derived_from", "observation", "candidates"})
        self.assertEqual(
            sorted(self.schema["observation"]["keys"]),
            [
                "base",
                "batch_ids",
                "batches",
                "bump_repairs",
                "handoff",
                "held",
                "outcomes",
                "repo",
                "requested_batch",
                "schema",
                "scope_approvals",
            ],
        )
        self.assertEqual(
            sorted(self.schema["candidates"]["keys"]),
            [
                "admissions",
                "base",
                "batch_id",
                "candidates",
                "historical_unknown_reason",
                "queue_authority",
                "repo",
                "schema",
                "timestamp_unit",
            ],
        )

    def test_production_sample_validates(self):
        self.assertEqual(feeds.validate_observation(self.sample["observation"], self.schema), [])
        self.assertEqual(feeds.validate_candidates(self.sample["candidates"], self.schema), [])

    def test_full_saved_responses_validate_when_present(self):
        observation = SAVED / "active-batches-2026-11-04T0745Z.json"
        if not observation.is_file():
            self.skipTest("saved production responses are not on this machine")
        self.assertEqual(feeds.validate_observation(json.loads(observation.read_text()), self.schema), [])
        for name in ("candidates-latest.json", "candidates-2026-11-04T0416Z.json"):
            path = SAVED / name
            if path.is_file():
                self.assertEqual(feeds.validate_candidates(json.loads(path.read_text()), self.schema), [], name)

    def test_validator_catches_drift(self):
        doc = copy.deepcopy(self.sample["observation"])
        del doc["batches"][0]["members"]
        self.assertTrue(
            any("missing required key 'members'" in e for e in feeds.validate_observation(doc, self.schema))
        )
        doc = copy.deepcopy(self.sample["observation"])
        doc["batches"][0]["extra"] = 1
        self.assertTrue(any("unknown key 'extra'" in e for e in feeds.validate_observation(doc, self.schema)))
        doc = copy.deepcopy(self.sample["observation"])
        doc["batches"][0]["id"] = "2650"
        self.assertTrue(
            any("$.batches[0].id: str is not one of" in e for e in feeds.validate_observation(doc, self.schema))
        )
        doc = copy.deepcopy(self.sample["observation"])
        doc["schema"] = "tauceti-bors.observation/v2"
        self.assertTrue(any("$.schema" in e for e in feeds.validate_observation(doc, self.schema)))
        doc = copy.deepcopy(self.sample["candidates"])
        doc["timestamp_unit"] = "unix_seconds"
        self.assertTrue(any("timestamp_unit" in e for e in feeds.validate_candidates(doc, self.schema)))
        doc = copy.deepcopy(self.sample["candidates"])
        doc["candidates"][0]["snapshot"]["members"][0]["original_admitted_at"] = "2026-11-04T00:00:00Z"
        self.assertTrue(feeds.validate_candidates(doc, self.schema))
        self.assertTrue(feeds.validate_observation([], self.schema))

    def test_bools_are_not_ints(self):
        self.assertEqual(feeds.validate(True, {"types": ["bool"]}), [])
        self.assertTrue(feeds.validate(True, {"types": ["int"]}))
        self.assertTrue(feeds.validate(1, {"types": ["bool"]}))


def member(
    pr,
    head,
    *,
    reviewer="tauceti-review-bot[bot]",
    admitted=1_793_775_283_486_988,
    admission=32568,
    scope=None,
    priority=0,
):
    return feeds.MemberView(
        pr=pr,
        head_sha=head,
        reviewer=reviewer,
        admitted_at=admitted,
        admission_id=admission,
        priority=priority,
        labels=("ready-to-merge",),
        labels_observed_at=admitted // 1_000_000,
        scope_approval=scope,
    )


class FakeSource:
    """A controller's feed view: one running head with a candidate, one open batch."""

    def __init__(self):
        self.members_head = (
            member(100, HEAD),
            member(
                101,
                "1" * 40,
                reviewer="kim-em",
                admission=32569,
                scope={"actor_id": 477956, "head_sha": "1" * 40, "merge_base_sha": MERGE_BASE},
            ),
        )
        self.members_open = (member(102, "2" * 40, admission=32570, priority=10),)
        self.head = feeds.BatchView(
            2650,
            "main",
            "running",
            True,
            TIP,
            BASE,
            self.members_head,
            candidate_id=1950,
            generation=2,
            staging_ref="staging-candidate-1950-2",
        )
        self.open = feeds.BatchView(2651, "main", "waiting", False, None, None, self.members_open)
        self.events = (
            feeds.EventView(1, "constructed", "queue.chain", 1_793_775_300_000_000, {"commit": TIP, "base_sha": BASE}),
            feeds.EventView(
                2,
                "dispatched",
                "github.workflow_dispatch_response",
                1_793_775_301_000_000,
                {"commit": TIP, "base_sha": BASE},
            ),
            feeds.EventView(
                3,
                "status_observed",
                "github.status_webhook",
                1_793_775_900_000_000,
                {
                    "commit": TIP,
                    "identifier": "build",
                    "state": "ok",
                    "url": "https://github.com/TauCetiProject/TauCeti/actions/runs/1",
                    "generation_binding": "commit_only",
                    "runner_lifecycle_authority": False,
                },
            ),
        )

    def repository(self, repo_id):
        return TAUCETI if repo_id == 1 else None

    def active_batches(self, repo, base):
        return [self.head, self.open] if base == "main" else []

    def batch(self, repo, batch_id):
        return {2650: self.head, 2651: self.open}.get(batch_id)

    def outcomes(self, repo, base):
        return [
            feeds.OutcomeView(100, HEAD, 2650, "running"),
            feeds.OutcomeView(100, HEAD, 2640, "error"),
            feeds.OutcomeView(102, "2" * 40, 2651, "waiting"),
        ]

    def scope_approvals(self, repo):
        return [{"pr": 101, "actor_id": 477956, "head_sha": "1" * 40, "merge_base_sha": MERGE_BASE}]

    def candidates(self, repo, base, batch_id, *, promoted):
        candidate = feeds.CandidateView(
            1950,
            2650,
            2,
            "main",
            BASE,
            self.members_head,
            1_793_775_300_000_000,
            self.events,
            previous_generation_id=1949,
            runner_started_at=1_793_775_400_000_000,
        )
        cold = feeds.CandidateView(1949, 2650, 1, "main", BASE, self.members_head, 1_793_775_000_000_000, ())
        rows = [candidate, cold]
        if batch_id is not None:
            rows = [c for c in rows if c.batch_id == batch_id]
        return rows

    def admissions(self, repo, base, batch_id):
        rows = [
            feeds.AdmissionView(m.admission_id, b.id, m.admitted_at, m, "main")
            for b in (self.head, self.open)
            for m in b.members
        ]
        return [a for a in rows if batch_id is None or a.batch_id == batch_id]

    def started_at(self):
        return 1_793_700_000.0


def load_tool(name):
    import tauceti_tools

    path = pathlib.Path(tauceti_tools.__file__).parent / "assets" / "scripts" / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Generated(unittest.TestCase):
    def setUp(self):
        self.schema = feeds.load_schema()
        self.sample = json.loads((FIXTURES / "bors-feeds-sample.json").read_text())
        self.source = FakeSource()

    def test_observation_feed_matches_bors(self):
        doc = feeds.observation_feed(self.source, TAUCETI, "main", now=1_793_775_999.5)
        self.assertEqual(feeds.validate_observation(doc, self.schema), [])
        self.assertEqual(set(doc), set(self.sample["observation"]))
        self.assertEqual(doc["schema"], "tauceti-bors.observation/v1")
        self.assertEqual(doc["batch_ids"], [2650, 2651])
        head, open_batch = doc["batches"]
        self.assertEqual(set(head), set(self.sample["observation"]["batches"][0]))
        self.assertEqual(
            head["train"],
            {"candidate_id": 1950, "generation": 2, "predecessor_id": None, "staging_ref": "staging-candidate-1950-2"},
        )
        self.assertEqual(head["members"], [{"pr": 100, "head_sha": HEAD}, {"pr": 101, "head_sha": "1" * 40}])
        self.assertIsNone(open_batch["train"])
        self.assertEqual(
            (open_batch["state"], open_batch["sealed"], open_batch["head_sha"], open_batch["base_sha"]),
            ("waiting", False, None, None),
        )
        self.assertEqual(
            doc["outcomes"],
            [
                {"pr": 100, "head_sha": HEAD, "batch_id": 2650, "state": "running"},
                {"pr": 102, "head_sha": "2" * 40, "batch_id": 2651, "state": "waiting"},
            ],
            "one outcome per (pr, head), newest batch first, as Bors's uniq_by",
        )
        self.assertEqual(
            doc["scope_approvals"],
            [{"pr": 101, "actor_id": 477956, "head_sha": "1" * 40, "merge_base_sha": MERGE_BASE}],
        )
        self.assertIsNone(doc["requested_batch"])
        self.assertEqual(set(doc["handoff"]), set(self.sample["observation"]["handoff"]))
        self.assertEqual(doc["handoff"]["observed_at"], "2026-11-04T07:06:39.500000Z")
        self.assertEqual((doc["held"], doc["bump_repairs"]), ([], []))

    def test_requested_batch_for_the_scope_approval_check(self):
        doc = feeds.observation_feed(self.source, TAUCETI, "main", 2650)
        self.assertEqual(feeds.validate_observation(doc, self.schema), [])
        self.assertEqual(doc["requested_batch"]["id"], 2650)
        self.assertIsNone(feeds.observation_feed(self.source, TAUCETI, "main", 9999)["requested_batch"])
        self.assertIsNone(
            feeds.observation_feed(self.source, TAUCETI, "release", 2650)["requested_batch"],
            "a batch on another base is not the requested one",
        )
        observation = load_tool("bors_observation")
        if observation is None:
            self.skipTest("TauCetiTools scripts unavailable")
        self.assertTrue(observation.valid_observation(doc, "main"))
        batch = observation.requested_batch(doc, TIP, BASE, batch_id=2650)
        self.assertEqual(batch["members"][1], {"pr": 101, "head_sha": "1" * 40})
        with self.assertRaises(ValueError):
            observation.requested_batch(doc, HEAD, BASE, batch_id=2650)
        scope = load_tool("bors_scope_approval")
        if scope is None:
            return
        self.assertTrue(
            scope.approved(
                doc["scope_approvals"],
                101,
                "1" * 40,
                MERGE_BASE,
                lambda path: (
                    {"id": 477956, "login": "kim-em"}
                    if path.startswith("/user/")
                    else {"user": {"id": 477956}, "permission": "admin"}
                ),
            )
        )
        self.assertFalse(scope.approved(doc["scope_approvals"], 100, HEAD, MERGE_BASE, lambda path: {}))

    def test_candidates_feed_matches_bors(self):
        doc = feeds.candidates_feed(self.source, TAUCETI, "main")
        self.assertEqual(feeds.validate_candidates(doc, self.schema), [])
        self.assertEqual(set(doc), set(self.sample["candidates"]))
        self.assertEqual(
            (doc["schema"], doc["timestamp_unit"], doc["queue_authority"], doc["batch_id"]),
            ("tauceti-bors.candidates/v1", "unix_microseconds", False, None),
        )
        self.assertEqual(doc["historical_unknown_reason"], self.sample["candidates"]["historical_unknown_reason"])
        warm, cold = doc["candidates"]
        self.assertEqual(set(warm), set(self.sample["candidates"]["candidates"][0]))
        self.assertEqual(set(warm["snapshot"]), set(self.sample["candidates"]["candidates"][0]["snapshot"]))
        self.assertEqual(
            set(warm["snapshot"]["members"][0]),
            set(self.sample["candidates"]["candidates"][0]["snapshot"]["members"][0]),
        )
        self.assertEqual(set(warm["events"][0]), set(self.sample["candidates"]["candidates"][0]["events"][0]))
        self.assertEqual(warm["snapshot"]["contract"], "tauceti-bors.candidate/v1")
        self.assertEqual(
            warm["snapshot"]["members"][1]["manual_approval"],
            {"actor_id": 477956, "head_sha": "1" * 40, "merge_base_sha": MERGE_BASE},
        )
        self.assertIsNone(warm["snapshot"]["members"][0]["manual_approval"])
        self.assertEqual(warm["snapshot"]["members"][0]["scheduling"]["label_hint"]["timestamp_unit"], "unix_seconds")
        self.assertEqual(warm["snapshot"]["members"][0]["original_admitted_at"], 1_793_775_283_486_988)
        self.assertIsNone(warm["unknown_reason"], "the queue knows the runner lifecycle from the run")
        self.assertIsInstance(cold["unknown_reason"], str)
        self.assertEqual([e["kind"] for e in warm["events"]], ["constructed", "dispatched", "status_observed"])
        self.assertEqual(len(doc["admissions"]), 3)
        self.assertEqual(set(doc["admissions"][0]), set(self.sample["candidates"]["admissions"][0]))
        self.assertEqual(
            set(doc["admissions"][0]["snapshot"]), set(self.sample["candidates"]["admissions"][0]["snapshot"])
        )
        self.assertNotIn(
            "assembly_head_sha", doc["admissions"][0]["snapshot"], "admission snapshots predate assembly, as in Bors"
        )

    def test_candidates_feed_filters(self):
        doc = feeds.candidates_feed(self.source, TAUCETI, "main", 2650)
        self.assertEqual(feeds.validate_candidates(doc, self.schema), [])
        self.assertEqual(doc["batch_id"], 2650)
        self.assertEqual({a["batch_id"] for a in doc["admissions"]}, {2650})
        promoted = feeds.candidates_feed(self.source, TAUCETI, "main", promoted=True)
        self.assertEqual(promoted["admissions"], [], "the promoted view carries no admissions, as Bors's does")

    def test_derive_schema_round_trip(self):
        derived = feeds.derive_schema(self.sample["observation"])
        self.assertEqual(feeds.validate(self.sample["observation"], derived), [])
        self.assertEqual(
            derived["keys"]["requested_batch"]["keys"].keys(), derived["keys"]["batches"]["items"]["keys"].keys()
        )
        self.assertIn("null", derived["keys"]["requested_batch"]["types"])


if __name__ == "__main__":
    unittest.main()


class OverTheController(unittest.TestCase):
    """The feeds generated from the real controller's records (tauceti_queue.controller over the
    end-to-end fakes): an approved PR inserted into the open batch, sealed into a head and built."""

    def test_feeds_from_a_live_controller(self):
        import tempfile

        from tauceti_queue import fakes

        with tempfile.TemporaryDirectory() as tmp:
            controller, github, _repo, _clock = fakes.make_controller(tmp)
            self.addCleanup(controller.store.close)
            head = github.create_pr(101, {"A.lean": "a\n"})
            evidence = {
                "kind": "issue_comment",
                "actor_login": "kim-em",
                "actor_id": 477956,
                "admin": True,
                "merge_base_sha": "d" * 40,
                "single": False,
            }
            controller.approve(TAUCETI, 101, head, "kim-em", 0, evidence)
            controller.tick()
            schema = feeds.load_schema()
            source = feeds.feed_source(controller)
            self.assertIsInstance(source, feeds.ControllerFeedSource)
            doc = feeds.observation_feed(source, TAUCETI, "main")
            self.assertEqual(feeds.validate_observation(doc, schema), [])
            self.assertTrue(doc["batch_ids"], "the approved PR is in a batch")
            members = [m for b in doc["batches"] for m in b["members"]]
            self.assertIn({"pr": 101, "head_sha": head}, members)
            self.assertEqual(
                doc["scope_approvals"], [{"pr": 101, "actor_id": 477956, "head_sha": head, "merge_base_sha": "d" * 40}]
            )
            running = [b for b in doc["batches"] if b["state"] == "running"]
            if running:
                requested = feeds.observation_feed(source, TAUCETI, "main", running[0]["id"])["requested_batch"]
                self.assertEqual(requested["id"], running[0]["id"])
                self.assertEqual(requested["state"], "running")
            cands = feeds.candidates_feed(source, TAUCETI, "main")
            self.assertEqual(feeds.validate_candidates(cands, schema), [])
            self.assertEqual(len(cands["admissions"]), 1)
            self.assertEqual(cands["admissions"][0]["snapshot"]["reviewer"], "kim-em")
            self.assertEqual(cands["admissions"][0]["snapshot"]["manual_approval"]["actor_id"], 477956)
            if cands["candidates"]:
                self.assertEqual(cands["candidates"][0]["snapshot"]["members"][0]["pr"], 101)
            self.assertEqual(feeds.observation_feed(source, TAUCETI, "release")["batches"], [])
