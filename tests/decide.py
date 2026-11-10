#!/usr/bin/env python3
"""The decide phase: off unless `--only` names it, then one ruling per incident, each a record.

GitHub is a stub and the model is a function that returns the answer file's contents. Covered: the
phase's selection and its due check; the answer schema; a round for each ruling, with the incident
acknowledged and the hold lifted or kept; a refused or malformed ruling becoming an escalation; the
later look at a ruling that waits; and what the model process is given.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker import agents, cli, decide, incidents, work_units
from tauceti_worker.constants import TAUCETI

fails = 0
clock = [1768469400]
real_gmtime = time.gmtime


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}" + ("" if ok else f": got={got!r} want={want!r}"))


def later():
    clock[0] += 60


def settings(root=None, **env):
    drop = (incidents.ENV, incidents.HOLD_ENV, "TAUCETI_ROADMAP_TARGETS", "GH_TOKEN")
    base = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("TAUCETI_DECIDE_")}
    if root is not None:
        base.update({incidents.ENV: str(root), incidents.HOLD_ENV: "1"})
    return patch.dict(os.environ, {**base, **env}, clear=True)


def load(path):
    return json.loads(Path(path).read_text())


class Hub:
    """Pull requests by number, each a dict of what differs from an open one of this account's."""

    repo = TAUCETI

    def __init__(self, **prs):
        self.prs = {int(k[1:]): v for k, v in prs.items()}

    def _row(self, n):
        v = self.prs[n]
        return {
            "number": n,
            "state": v.get("state", "OPEN"),
            "headRefOid": v.get("head", f"head-{n}"),
            "title": v.get("title", f"PR {n}"),
            "body": "body",
            "isDraft": v.get("draft", False),
            "author": {"login": v.get("author", "me")},
            "labels": [{"name": name} for name in v.get("labels", [])],
        }

    def open_prs(self):
        return [self._row(n) for n, v in self.prs.items() if v.get("state", "OPEN") == "OPEN"]

    def pr_view(self, n, fields):
        return self._row(n) if n in self.prs else None

    def issue_comments(self, n):
        return [{"user": {"login": "reviewer"}, "created_at": "t", "body": "x" * 5000}] * 9


def declined(*numbers):
    for number in numbers:
        incidents.record_declined("fix", number, f"head-{number}", message=f"#{number} cannot move")


def rounds(tmp, hub, answer=None, *, rc=0, claimed=()):
    """One `decide.run`; `answer` is the answer file's parsed contents. Returns the run's result
    as `rc`, and the packet when the model was asked."""
    seen = {}

    def ask(_w, _opts, packet):
        seen.update(packet=packet)
        return rc, answer

    w = SimpleNamespace(gh=hub, cfg=SimpleNamespace(state=tmp / "state", wid="w1"))
    with (
        patch.object(decide, "_ask", side_effect=ask),
        patch.object(work_units, "_branch_claimed_elsewhere", side_effect=lambda _w, n: n in claimed),
    ):
        seen["rc"] = decide.run(w, SimpleNamespace())
    later()
    return seen


def answer(*rows):
    return {"rulings": [{"case": f"declined-fix-{n}", "ruling": r, "note": f"because of {r}", **f} for n, r, f in rows]}


def ruling(root, case="declined-fix-1"):
    return load(root / f"ruling-{case}.json")


def where(root, name):
    folders = (("live", root / name), ("acked", root / incidents.ACKED / name))
    return [folder for folder, path in folders if path.exists()]


def test_selection(root, tmp):
    check("an unrestricted round does not want it", work_units.want([], "decide"), False)
    check("--only decide does", work_units.want(["decide", "fix"], "decide"), True)
    check("the flag accepts it", cli.parse_only(["decide,fix"]), ["decide", "fix"])
    check("it runs on the host", tc.SANDBOX_DEFAULT["decide"], False)
    for name, env in (("the directory", {}), ("a well-formed TAUCETI_DECIDE_GAP", {"TAUCETI_DECIDE_GAP": "soon"})):
        with settings(root if env else None, **env):
            try:
                decide.check_settings()
                check(f"without {name} the worker stops at startup", False)
            except tc.Die:
                check(f"without {name} the worker stops at startup", True)
    with settings():
        check("…and nothing is due", decide.due(), (False, f"{incidents.ENV} is not set"))

    ran, surveyed, asked = [], [], []
    real_due = decide.due
    worker = SimpleNamespace(cfg=SimpleNamespace(wid="w1", state=tmp / "state"), counters=None, gh=None, rs=None)

    def go(only, prs=(), dry_run=False):
        ran.clear(), surveyed.clear(), asked.clear()
        opts = tc.RoundOpts(only=only, agent="claude", work_model="claude", sandbox_host=True, dry_run=dry_run, prs=prs)
        survey = patch.object(
            work_units, "survey", side_effect=lambda *a, **k: surveyed.append(k["stages"]) or tc.Survey("w1")
        )
        with (
            survey,
            patch.object(work_units, "mirror_creds"),
            patch("tauceti_worker.review_diagnostics.refund_stale_head_errors"),
            patch.object(work_units, "_host_agent_binary", return_value="python3"),
            patch.object(work_units, "do_decide", side_effect=lambda *a: ran.append("decide") or 0),
            patch.object(decide, "due", side_effect=lambda: asked.append(1) or real_due()),
            patch("tauceti_worker.github_transport.observed_budget", return_value={}),
            patch("tauceti_worker.claude_api.api_mode", return_value=False),
        ):
            try:
                return work_units.run_round(worker, opts)
            except tc.NoProgress as error:
                return str(error)

    idle = "no eligible work this round under --only=fix"
    with settings(root), patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": str(tmp / "shared")}):
        check("nothing recorded: a decide-only round says so", go(["decide"]), "decide: no live incident or ruling")
        check("…and a mixed one goes on to its other phases", (go(["decide", "fix"]), surveyed), (idle, [["fix"]]))
        declined(1)
        check("a round that does not name it never looks", (go(["fix"]), asked, ran), (idle, [], []))
        check(
            "a live incident makes it due, ahead of the survey",
            (go(["decide", "fix"]), ran, surveyed),
            (0, ["decide"], []),
        )
        check("--pr leaves it out", (go(["decide"], prs=(1,)), ran), ("decide: it takes no --pr", []))
        check("a dry run starts nothing", (go(["decide"], dry_run=True), ran), (0, []))
        with decide._exclusive(root):
            pass
        check("the gap is measured from the last round", decide.due()[0], False)
        check("…and passes", decide.due(now=time.time() + decide.gap() + 1)[0], True)
    with settings(root, TAUCETI_DECIDE_GAP="0"):
        check("TAUCETI_DECIDE_GAP sets it", decide.due()[0], True)


def verdict(row, allowed=None):
    """How `parse_rulings` takes one entry for case `c`: `ok`, or its objection."""
    got = decide.parse_rulings({"rulings": [row]}, {"c": list(decide.SCHEMA if allowed is None else allowed)})["c"]
    return got if isinstance(got, str) else "ok"


def test_schema():
    shown = re.search(r'^\{"rulings": \[.*?^\]\}', (tc.paths.HERE / "prompts/decide.md").read_text(), re.S | re.M)[0]
    filler = "Layer 2 of the README states the milestone, and no open pull request or list item covers it."
    example = json.loads(shown.replace('"..."', json.dumps(filler)))
    parsed = decide.parse_rulings(example, {row["case"]: list(decide.SCHEMA) for row in example["rulings"]})
    got = sorted(value if isinstance(value, str) else value["ruling"] for value in parsed.values())
    check("the prompt's example passes the schema, one ruling of each kind", got, sorted(decide.SCHEMA))

    wait = {"case": "c", "ruling": "wait", "note": "n", "blocked_on": [5]}
    check("a well-formed ruling is kept", verdict(wait), "ok")
    check("an extra field is refused", "exactly the fields" in verdict({**wait, "comment": "x"}), True)
    check(
        "a missing field is refused",
        "exactly the fields" in verdict({"case": "c", "ruling": "wait", "note": "n"}),
        True,
    )
    check(
        "a wrong type is refused",
        verdict({**wait, "blocked_on": ["5"]}),
        "the answer's `wait` ruling has a malformed blocked_on",
    )
    check("…a boolean is not a number", "malformed" in verdict({**wait, "blocked_on": [True]}), True)
    check("an empty note is refused", "malformed note" in verdict({**wait, "note": " "}), True)
    check("an unknown ruling is refused", verdict({**wait, "ruling": "merge"}), "the answer names no known ruling")
    check("one that is not offered is refused", "not available" in verdict(wait, ["escalate"]), True)
    several = decide.parse_rulings({"rulings": [wait, wait, {**wait, "case": "x"}]}, {"c": ["wait"], "d": ["wait"]})
    check(
        "a repeated case and an omitted one",
        several,
        {"c": "the answer rules on this case twice", "d": "the answer leaves this case out"},
    )
    whole = {decide.parse_rulings(value, {"c": ["wait"]})["c"] for value in ({"rulings": [wait], "x": 1}, None, [wait])}
    check(
        "anything but one `rulings` list is refused whole",
        whole,
        {"the answer is not an object holding one `rulings` list"},
    )


def test_rounds(tmp):
    root = tmp / "retry"
    with settings(root):
        declined(1)
        hub = Hub(p1={})
        check("held before any ruling", incidents.declined("fix", 1, "head-1"), True)
        seen = rounds(tmp, hub, answer((1, "retry", {})))
        case = seen["packet"]["cases"][0]
        check(
            "the case names the incident and the agent's words",
            (case["case"], case["final_message"]),
            ("declined-fix-1", "#1 cannot move"),
        )
        check("…and what may be ruled", case["allowed"][:3], ["retry", "wait", "escalate"])
        check("…with bounded evidence", [len(c["body"]) for c in case["latest_comments"]], [1500] * decide.MAX_COMMENTS)
        value = ruling(root)
        check(
            "retry: a ruling record",
            (seen["rc"], value["kind"], value["ruling"], value["by"]),
            (0, "ruling", "retry", "model"),
        )
        check(
            "…about the incident",
            (value["pr"], value["head"], value["incident"], value["note"]),
            (1, "head-1", "declined", "because of retry"),
        )
        check("…the incident is acknowledged", where(root, "declined-fix-1.json"), ["acked"])
        check("…the ruling is live", where(root, "ruling-declined-fix-1.json"), ["live"])
        check("…the hold is lifted", incidents.declined("fix", 1, "head-1"), False)
        check(
            "…and the next repair prompt carries the note",
            "because of retry" in decide.retry_note("fix", 1, "head-1"),
            True,
        )
        check("…at that head only", decide.retry_note("fix", 1, "other"), "")
        check("nothing new: the model is not started", rounds(tmp, hub), {"rc": None})
        declined(1)
        check("a second decline at the head holds again", incidents.declined("fix", 1, "head-1"), True)
        seen = rounds(tmp, hub, answer((1, "retry", {})))
        check("…and retry is no longer offered", "retry" in seen["packet"]["cases"][0]["allowed"], False)
        check(
            "…so asking for it escalates",
            (ruling(root)["ruling"], "not available" in ruling(root)["note"]),
            ("escalate", True),
        )
        hub.prs[1]["head"] = "next"
        rounds(tmp, hub)
        check("a new head retires the ruling", where(root, "ruling-declined-fix-1.json"), ["acked"])

    root = tmp / "settle"
    with settings(root):
        declined(1, 2, 3, 4, 5)
        later()
        incidents.note_budget_spent("fix-ci", 6, "head-6", "build failed at head; 3/3 attempts used")
        incidents.record(incidents.DECLINED, "roadmap-0", stage="roadmap", pr=0, head="", repository=TAUCETI)
        hub = Hub(p1={"state": "MERGED"}, p2={"head": "moved"}, p3={"labels": ["hold"]}, p4={}, p5={}, p6={})
        spent = {"case": "budget-spent-fix-ci-6", "ruling": "escalate", "note": "n", "recommend": "rebuild it"}
        seen = rounds(tmp, hub, {"rulings": [spent]}, claimed={4})
        check(
            "a closed or moved pull request is acknowledged unasked",
            [where(root, f"declined-fix-{n}.json") for n in (1, 2)],
            [["acked"]] * 2,
        )
        left = [where(root, f"declined-{key}.json") for key in ("fix-3", "fix-4", "roadmap-0")]
        check("a hold label, a claimed branch and a foreign record are left alone", left, [["live"]] * 3)
        cases = seen["packet"]["cases"]
        check(
            "the model sees the rest, oldest first",
            [c["case"] for c in cases],
            ["declined-fix-5", "budget-spent-fix-ci-6"],
        )
        check("a spent budget cannot be retried", cases[1]["allowed"][:2], ["wait", "escalate"])
        check("escalate keeps the recommendation", ruling(root, "budget-spent-fix-ci-6")["recommend"], "rebuild it")
        check("…and acknowledges the incident", where(root, "budget-spent-fix-ci-6.json"), ["acked"])
        check(
            "a case the answer leaves out is escalated",
            ruling(root, "declined-fix-5")["note"],
            "No ruling: the answer leaves this case out.",
        )

    root = tmp / "cap"
    with settings(root, TAUCETI_DECIDE_MAX_CASES="2"):
        for n in (1, 2, 3):
            declined(n)
            later()
        hub = Hub(p1={}, p2={}, p3={})
        check("TAUCETI_DECIDE_MAX_CASES bounds a round", len(rounds(tmp, hub, {"rulings": []})["packet"]["cases"]), 2)
        check(
            "a failed model run writes no ruling",
            (rounds(tmp, hub, rc=3)["rc"], where(root, "declined-fix-3.json")),
            (3, ["live"]),
        )
        fd = os.open(root / decide.LOCK, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        check("a second worker on the directory stands aside", rounds(tmp, hub, {"rulings": []}), {"rc": None})
        os.close(fd)

    root = tmp / "wait"
    with settings(root):
        declined(1, 2, 3)
        incidents.note_budget_spent("fix", 4, "head-4", "blocking review; 3/3 attempts used")
        hub = Hub(p1={}, p2={}, p3={}, p4={}, p20={}, p21={}, p22={"state": "CLOSED"})
        waits = answer(
            (1, "wait", {"blocked_on": [20]}), (2, "wait", {"blocked_on": [22]}), (3, "wait", {"blocked_on": [20, 21]})
        )
        waits["rulings"].append({"case": "budget-spent-fix-4", "ruling": "wait", "note": "n", "blocked_on": [20]})
        rounds(tmp, hub, waits)
        check(
            "wait: the blockers are recorded and the hold stays",
            (ruling(root)["blocked_on"], incidents.declined("fix", 1, "head-1")),
            ([20], True),
        )
        refused = ruling(root, "declined-fix-2")
        check(
            "waiting on a closed pull request is refused",
            (refused["ruling"], refused["recommend"]),
            ("escalate", "wait"),
        )
        check(
            "blockers still open: no model, nothing changes",
            (rounds(tmp, hub), ruling(root)["ruling"]),
            ({"rc": None}, "wait"),
        )
        hub.prs[20]["state"] = "MERGED"
        check("the blocker merged: no model either", rounds(tmp, hub), {"rc": None})
        value = ruling(root)
        check(
            "…a retry by recheck",
            (value["ruling"], value["by"], incidents.declined("fix", 1, "head-1")),
            ("retry", "recheck", False),
        )
        check(
            "…saying what landed and what the ruling said",
            ("#20 merged" in value["note"], "because of wait" in value["note"]),
            (True, True),
        )
        check("one of two has merged: still waiting", ruling(root, "declined-fix-3")["ruling"], "wait")
        spent = ruling(root, "budget-spent-fix-4")
        check(
            "a spent budget whose blocker merged is escalated", (spent["ruling"], spent["by"]), ("escalate", "recheck")
        )
        hub.prs[21]["state"] = "CLOSED"
        seen = rounds(tmp, hub, answer((3, "escalate", {"recommend": ""})))
        case = seen["packet"]["cases"][0]
        check(
            "a blocker closed unmerged: back to the model",
            (case["case"], case["earlier_ruling"]["ruling"]),
            ("declined-fix-3", "wait"),
        )
        check("…whose new ruling replaces the old", ruling(root, "declined-fix-3")["ruling"], "escalate")


def test_model_call(tmp):
    captured = {}

    def proc(argv, *, env, cwd, logdir, label, provider, quota_cfg):
        captured.update(env=env, cwd=cwd, prompt=argv[-1])
        (cwd / "rulings.json").write_text('{"rulings": []}')
        return 0

    w = SimpleNamespace(cfg=SimpleNamespace(state=tmp / "state", wid="w1"))
    for n in range(decide.KEPT_RUNS + 3):
        (tmp / "state" / "decide" / f"2020010{n:02d}").mkdir(parents=True)
    with (
        patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret"}),
        patch.object(agents, "run_agent_proc", side_effect=proc),
        patch.object(
            agents, "host_agent_argv", side_effect=lambda prompt, _profile: (["agent", prompt], dict(os.environ))
        ),
        patch.object(work_units, "_effective_authoring_profile", return_value=SimpleNamespace(provider="claude")),
    ):
        got = decide._ask(w, SimpleNamespace(), {"cases": []})
    work = captured["cwd"]
    check("the model's answer file is read back", got, (0, {"rulings": []}))
    check(
        "the prompt names the case file and the answer file",
        all(str(work / name) in captured["prompt"] for name in ("cases.json", "rulings.json")),
        True,
    )
    check("the case file holds the packet", load(work / "cases.json"), {"cases": []})
    check("the process is given no GitHub token", {"GH_TOKEN", "GITHUB_TOKEN"} & set(captured["env"]), set())
    check("older run directories are pruned", len(list((tmp / "state" / "decide").iterdir())), decide.KEPT_RUNS)


def main():
    def gmtime(seconds=None):
        return real_gmtime(clock[0] if seconds is None else seconds)

    with tempfile.TemporaryDirectory() as tmp, patch("time.gmtime", gmtime):
        tmp = Path(tmp)
        test_selection(tmp / "selection", tmp)
        test_schema()
        test_rounds(tmp)
        test_model_call(tmp / "model")
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
