#!/usr/bin/env python3
"""A green standalone bump recovers a proven old-base singleton, without an editor."""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import bump_recovery as recovery
from tauceti_worker import work_units
from tauceti_worker.config import NoProgress
from tauceti_worker.survey import Candidate, PRInfo

survey_mod = importlib.import_module("tauceti_worker.survey")
HEAD, BASE, OLD, STAGED = (c * 40 for c in "abcd")
PR = 14180


def raw():
    return {
        "number": PR,
        "headRefOid": HEAD,
        "baseRefOid": BASE,
        "headRefName": "bump-mathlib/fix-old",
        "headRepositoryOwner": {"login": "TauCetiProject"},
        "headRepository": {"name": "TauCeti"},
        "mergeable": "MERGEABLE",
        "author": {"login": "app/tauceti-review-bot", "is_bot": True},
        "statusCheckRollup": [
            {"context": "build", "state": "SUCCESS"},
            {"context": "bors", "state": "FAILURE", "startedAt": "2026-10-10T00:00:00Z"},
        ],
        "labels": [],
    }


def observation():
    return {
        "schema": "tauceti-bors.observation/v1",
        "repo": "TauCetiProject/TauCeti",
        "base": "main",
        "batches": [],
        "held": [],
        "queue_stops": [],
        "outcomes": [{"pr": PR, "head_sha": HEAD, "state": "error", "batch_id": 417}],
        "requested_batch": {
            "id": 417,
            "state": "error",
            "base": "main",
            "base_sha": OLD,
            "head_sha": STAGED,
            "members": [{"pr": PR, "head_sha": HEAD}],
        },
    }


def worker():
    values = {}
    counters = SimpleNamespace(
        read=lambda k: values.get(k, 0),
        write=lambda k, v: values.__setitem__(k, v),
        incr=lambda k: values.__setitem__(k, values.get(k, 0) + 1),
    )
    live = {
        "state": "open",
        "draft": False,
        "labels": [],
        "user": {"login": "tauceti-review-bot[bot]"},
        "head": {"sha": HEAD, "ref": raw()["headRefName"], "repo": {"full_name": "TauCetiProject/TauCeti"}},
        "base": {"ref": "main", "sha": BASE},
    }
    replies = {
        f"pulls/{PR}": live,
        f"commits/{HEAD}/status": {
            "statuses": [{"context": k, "state": "success"} for k in ("build", "scope", "bump-guard")]
            + [{"context": "bors", "state": "failure"}]
        },
        f"compare/{OLD}...{BASE}": {"status": "ahead"},
        f"compare/{BASE}...{HEAD}": {"behind_by": 1},
        f"git/commits/{STAGED}": {"parents": [{"sha": OLD}]},
        f"pulls/{PR}/update-branch": {"message": "Updating branch"},
    }
    calls = []

    def gh(args):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, json.dumps(replies[args[1].removeprefix("repos/TauCetiProject/TauCeti/")]), ""
        )

    w = SimpleNamespace(
        counters=counters,
        claims=SimpleNamespace(begin_branch_work=Mock(return_value=True)),
        gh=SimpleNamespace(_gh=gh),
        rs=SimpleNamespace(bust=Mock()),
    )
    return w, values, replies, calls


def candidate():
    return Candidate(PR, HEAD, bump_recovery_key="selected-state")


def run(w, obs):
    with patch.object(recovery, "read_bors", return_value=obs):
        return recovery.recover(w, SimpleNamespace(open_prs=[PRInfo.from_json(raw())]), candidate(), None, False)


def test_recovery_and_provider_bypass():
    w, values, replies, calls = worker()
    with (
        patch.object(recovery, "read_bors", return_value=observation()),
        patch.object(work_units, "_bubble", return_value=False),
        patch.object(work_units, "_effective_authoring_profile", side_effect=AssertionError("provider launch")),
        patch.object(work_units, "report_runtime"),
    ):
        rc = work_units.dispatch(
            "bump", w, SimpleNamespace(open_prs=[PRInfo.from_json(raw())]), candidate(), SimpleNamespace(dry_run=False)
        )
    assert rc == 0 and values == {"selected-state": 1}
    write = next(a for a in calls if "--method" in a)
    assert write[-2:] == ["-f", f"expected_head_sha={HEAD}"]
    assert len(calls) == 5  # selected PR/status, two ancestry reads, CAS; no fleet survey reads
    w.rs.bust.assert_called_once_with(PR)


def test_legacy_base_and_deferred_states():
    obs = observation()
    obs["requested_batch"]["base_sha"] = None
    w, _, _, calls = worker()
    assert run(w, obs) == 0
    assert any(f"git/commits/{STAGED}" in a[1] for a in calls)
    variants = []
    for field in ("queue_stops", "outcomes"):
        x = observation()
        del x[field]
        variants.append(x)
    for reason in ("requested", "cancel_all", "automatic_review_rejected", "closed"):
        x = observation()
        x["queue_stops"] = [{"pr": PR, "head_sha": HEAD, "state": "withdrawn", "reason": reason}]
        variants.append(x)
    x = observation()
    x["held"] = [{"pr": PR, "head_sha": HEAD}]
    variants.append(x)
    x = observation()
    x["batches"] = [{"members": [{"pr": PR, "head_sha": HEAD}]}]
    variants.append(x)
    x = observation()
    x["requested_batch"]["members"].append({"pr": 2, "head_sha": OLD})
    variants.append(x)
    x = observation()
    x["outcomes"][0]["head_sha"] = OLD
    variants.append(x)
    x = observation()
    x["requested_batch"]["state"] = "canceled"
    variants.append(x)
    x = observation()
    x["outcomes"] += [{"pr": PR, "head_sha": str(i) * 40, "state": "error", "batch_id": i} for i in (1, 2, 3)]
    variants.append(x)
    x = observation()
    x["requested_batch"]["base_sha"] = BASE
    variants.append(x)
    for x in variants:
        w, _, _, calls = worker()
        assert run(w, x) is None, x
        assert not any("--method" in a for a in calls)
    for changed in ("head", "draft", "hold", "base", "green"):
        w, _, replies, calls = worker()
        live = replies[f"pulls/{PR}"]
        if changed == "head":
            live["head"]["sha"] = OLD
        if changed == "draft":
            live["draft"] = True
        if changed == "hold":
            live["labels"] = [{"name": "hold"}]
        if changed == "base":
            live["base"]["sha"] = OLD
        if changed == "green":
            replies[f"commits/{HEAD}/status"]["statuses"][0]["state"] = "pending"
        assert run(w, observation()) is None
        assert not any("--method" in a for a in calls)
    for state in ({"status": "behind"}, {"status": "diverged"}):
        w, _, replies, calls = worker()
        replies[f"compare/{OLD}...{BASE}"] = state
        assert run(w, observation()) is None and not any("--method" in a for a in calls)
    w, _, replies, calls = worker()
    replies[f"compare/{BASE}...{HEAD}"] = {"behind_by": 0}
    assert run(w, observation()) is None and not any("--method" in a for a in calls)


def test_races_and_infrastructure():
    w, values, _, calls = worker()
    stopped = observation()
    stopped["queue_stops"] = [{"pr": PR, "head_sha": HEAD, "state": "withdrawn", "reason": "requested"}]
    with patch.object(recovery, "read_bors", side_effect=[observation(), stopped]):
        assert (
            recovery.recover(w, SimpleNamespace(open_prs=[PRInfo.from_json(raw())]), candidate(), None, False) is None
        )
    assert not any("--method" in a for a in calls)
    w, values, _, _ = worker()
    with patch.object(recovery, "read_bors", side_effect=OSError("temporary outage")):
        try:
            recovery.recover(w, SimpleNamespace(open_prs=[PRInfo.from_json(raw())]), candidate(), None, False)
        except NoProgress:
            pass
        else:
            raise AssertionError("outage was swallowed")
    assert values["selected-state"] == 0
    w, values, _, calls = worker()
    w.claims.begin_branch_work.return_value = False
    assert run(w, observation()) is None and not calls and not values


def test_survey_without_new_requests():
    w, values, _, _ = worker()
    data = raw()
    gh = SimpleNamespace(open_prs=lambda: [data])

    def survey():
        with patch.object(survey_mod, "me", return_value="me"), patch.object(survey_mod, "can_push", return_value=True):
            return survey_mod.survey(SimpleNamespace(wid="test"), gh, None, w.counters, deep=False)

    sv = survey()
    assert len(sv.bump.actionable) == 1 and sv.bump.actionable[0].bump_recovery_key
    key = sv.bump.actionable[0].bump_recovery_key
    values[key] = 1
    assert not survey().bump.actionable
    data["baseRefOid"] = OLD
    assert survey().bump.actionable
    data["labels"] = [{"name": "hold"}]
    assert not survey().bump.actionable
    data["labels"] = []
    data["headRefName"] = "hopscotch/lkg-bump"
    assert survey().bump.actionable
    data["statusCheckRollup"] = [{"context": "build", "state": "FAILURE"}]
    assert survey().bump.actionable[0].bump_recovery_key == ""


for test in (
    test_recovery_and_provider_bypass,
    test_legacy_base_and_deferred_states,
    test_races_and_infrastructure,
    test_survey_without_new_requests,
):
    test()
print("PASS: exact-head singleton recovery, legacy base, withdrawals, bounds, races and zero AI launch")
