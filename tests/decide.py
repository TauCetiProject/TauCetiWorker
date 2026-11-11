#!/usr/bin/env python3
"""The decide phase: off unless `--only` names it, then one ruling per incident, each a record.

GitHub is a stub and the model is a function that returns the answer file's contents. Covered: the
phase's selection and its due check; the answer schema; a round for each ruling, with the incident
acknowledged and the hold lifted or kept; a refused or malformed ruling becoming an escalation; the
later look at a ruling that waits; what the model process is given; and each side effect on its own.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import fcntl
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker import agents, cli, decide, github, incidents, work_units
from tauceti_worker.constants import ROADMAP_DIR, TAUCETI
from tauceti_worker.targets import parse_targets

fails = 0
clock = [1768469400]
real_gmtime = time.gmtime
LEAN = "+++ b/TauCeti/A.lean\n+theorem strong_law : True := trivial\n+private lemma aux : True := trivial\n"


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
        self.closed = []
        self.listing = None  # a stale listing: open_prs returns these rows, pr_view the live ones
        self.unreadable = set()

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
        if self.listing is not None:
            return self.listing
        return [self._row(n) for n, v in self.prs.items() if v.get("state", "OPEN") == "OPEN"]

    def pr_view(self, n, fields):
        return self._row(n) if n in self.prs and n not in self.unreadable else None

    def issue_comments(self, n):
        return [{"user": {"login": "reviewer"}, "created_at": "t", "body": "x" * 5000}] * 9

    def merged_prs(self, search, limit=200):
        return []

    def pr_diff(self, n):
        self.on_diff(n)
        return self.prs[n].get("diff", LEAN)

    def on_diff(self, n):
        pass

    def close_pr(self, n, comment):
        self.closed.append((n, comment))
        self.prs[n]["state"] = "CLOSED"
        return True


def declined(*numbers):
    for number in numbers:
        incidents.record_declined("fix", number, f"head-{number}", message=f"#{number} cannot move")


class Claims:
    """The branch claims a round takes: `taken` and `released` list the pull requests, and one in
    `elsewhere` is held by another worker."""

    def __init__(self, elsewhere=()):
        self.elsewhere, self.taken, self.released, self.held = set(elsewhere), [], [], None

    def begin_branch_work(self, pr, head, refname, owner, repo):
        if pr in self.elsewhere:
            return False
        self.taken.append(pr)
        self.held = (f"branch/{pr}", "claims")
        return True

    def stop_heartbeat(self):
        pass

    def release(self):
        if self.held:
            self.released.append(int(self.held[0].split("/")[1]))
        self.held = None


def rounds(tmp, hub, answer=None, *, rc=0, claimed=(), claims=None, meanwhile=None):
    """One `decide.run`; `answer` is the answer file's parsed contents, and `meanwhile` runs while
    the model is out. Returns the run's result as `rc`, and the packet when the model was asked."""
    seen = {}

    def ask(_w, _opts, packet):
        seen.update(packet=packet)
        if meanwhile:
            meanwhile()
        return rc, answer

    def fetch(_repo, checkout):
        (checkout / ROADMAP_DIR / "Probability").mkdir(parents=True, exist_ok=True)
        return True

    w = SimpleNamespace(gh=hub, cfg=SimpleNamespace(state=tmp / "state", wid="w1"), claims=claims or Claims())
    with (
        patch.object(decide, "_ask", side_effect=ask),
        patch.object(work_units, "_branch_claimed_elsewhere", side_effect=lambda _w, n: n in claimed),
        patch.object(agents, "fetch_ref", side_effect=fetch),
        patch("tauceti_worker.github.me", return_value="me"),
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

    root = tmp / "stale"
    with settings(root):
        declined(1, 2)
        hub = Hub(p1={}, p2={})
        hub.listing = [{**hub._row(1), "headRefOid": "older"}]
        hub.unreadable = {2}
        check("a stale listing: nothing to rule on this round", rounds(tmp, hub), {"rc": None})
        check(
            "…and an incident the fresh read confirms, or cannot read, stays live",
            [where(root, f"declined-fix-{n}.json") for n in (1, 2)],
            [["live"]] * 2,
        )
        hub.listing, hub.unreadable = None, set()
        check("the listing catches up: it is ruled on", len(rounds(tmp, hub, {"rulings": []})["packet"]["cases"]), 2)

    root = tmp / "rewritten"
    with settings(root):
        declined(1, 2)
        hub = Hub(p1={}, p2={})

        def push_and_decline():
            hub.prs[1]["head"] = "head-1b"
            incidents.record_declined("fix", 1, "head-1b", message="declined at the new head")
            later()
            declined(2)

        rounds(tmp, hub, answer((1, "retry", {}), (2, "retry", {})), meanwhile=push_and_decline)
        check(
            "an incident rewritten while the model ruled stays live: a new head, a same-head refresh",
            [where(root, f"declined-fix-{n}.json") for n in (1, 2)],
            [["live"]] * 2,
        )
        check(
            "…and stays held",
            (incidents.declined("fix", 1, "head-1b"), incidents.declined("fix", 2, "head-2")),
            (True, True),
        )
        seen = rounds(tmp, hub, {"rulings": []})
        check(
            "…until it is ruled on in its own right",
            [(c["case"], c["head"]) for c in seen["packet"]["cases"]],
            [("declined-fix-1", "head-1b"), ("declined-fix-2", "head-2")],
        )

    root = tmp / "locked"
    with settings(root):
        declined(1)
        version = incidents.find(incidents.DECLINED, "fix-1")[0]
        fd = os.open(root / incidents.LOCK, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        moved = []
        acker = threading.Thread(target=lambda: moved.append(incidents.acknowledge("declined", "fix-1", version)))
        acker.start()
        time.sleep(0.2)
        check("acknowledging waits for a writer's lock", moved, [])
        later()
        incidents._write(root, incidents.DECLINED, "fix-1", {"head": "head-1", "pr": 1})
        os.close(fd)
        acker.join(5)
        check(
            "…and then leaves the record written under it",
            (moved, where(root, "declined-fix-1.json")),
            ([False], ["live"]),
        )

    root = tmp / "displaced"
    with settings(root):
        for head in ("h1", "h2", "h3"):
            incidents.record_declined("fix", 9, head, message=head)
            incidents.acknowledge("declined", "fix-9")
        kept = sorted(path.name for path in (root / incidents.ACKED).iterdir())
        check(
            "an acknowledged record that another replaces is kept beside it",
            kept,
            ["declined-fix-9.json", "declined-fix-9.json.1", "declined-fix-9.json.2"],
        )
        check(
            "…out of the hold's sight",
            [load(root / incidents.ACKED / name)["head"] for name in kept] + [len(incidents.find("declined", "fix-9"))],
            ["h3", "h1", "h2", 1],
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

    root = tmp / "lost"
    with settings(root):
        declined(1)
        hub = Hub(p1={}, p30={}, p31={})
        rounds(tmp, hub, answer((1, "wait", {"blocked_on": [30, 31]})))
        hub.prs[31]["state"] = "CLOSED"
        seen = rounds(tmp, hub, answer((1, "escalate", {"recommend": ""})))
        check(
            "a later blocker closed unmerged while an earlier one is open: back to the model",
            [c["case"] for c in seen.get("packet", {}).get("cases", [])],
            ["declined-fix-1"],
        )
    targets = parse_targets(LIST)
    view = SimpleNamespace(open_prs={20: {}}, gh=Hub(p20={}), listed=(Path("list"), targets))
    check(
        "a target gone from the list after one still open is noticed",
        decide._blockers(view, {"blocked_on_targets": ["strong-law", "gone"]}),
        (None, "`gone` is no longer on the target list"),
    )
    check(
        "…and one still open keeps it waiting",
        decide._blockers(view, {"blocked_on": [20], "blocked_on_targets": ["borel-cantelli"]}),
        (None, ""),
    )


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
        patch.dict(
            os.environ,
            {
                "GH_TOKEN": "secret",
                "GITHUB_TOKEN": "secret",
                "_TAUCETI_GITHUB_BASE_ENV": json.dumps({"GH_TOKEN": "secret-before-selection"}),
            },
        ),
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
    check(
        "…nor one the account selection kept to restore",
        "secret" in json.dumps(github.unselected_github_env(captured["env"])),
        False,
    )
    check("older run directories are pruned", len(list((tmp / "state" / "decide").iterdir())), decide.KEPT_RUNS)


LIST = """# Law of large numbers
<!-- tauceti-targets:v1 -->

## MeasureTheory
- [x] `borel-cantelli` — both lemmas (needs: none)

## Probability
- [ ] `strong-law` — the strong law (needs: `borel-cantelli`)
"""


def item(slug, area="Probability", needs=(), text="Layer 2 (tail events): they have probability 0 or 1."):
    return {"area": area, "slug": slug, "text": text, "needs": list(needs)}


def test_prerequisite(tmp):
    need = {"case": "c", "ruling": "prerequisite", "note": "n", "items": [item("zero-one")]}
    check("the schema takes an item", verdict(need), "ok")
    check("…checked field by field", "malformed items" in verdict({**need, "items": [item("Zero One")]}), True)
    check(
        "…on one line", "malformed items" in verdict({**need, "items": [item("zero-one", text="a\n" + "b" * 30)]}), True
    )

    path = tmp / "targets.md"
    path.write_text(LIST)
    targets, areas = parse_targets(LIST), {"Probability", "MeasureTheory", "Topology"}
    asked = [item("zero-one", needs=["borel-cantelli"]), item("strong-law"), item("nets", "Topology", ["zero-one"])]
    lines, slugs, problem = decide.prerequisite_lines(asked, 7, targets, areas)
    check(
        "new items become lines",
        ([(a, s) for a, s, _ in lines], problem),
        ([("Probability", "zero-one"), ("Topology", "nets")], ""),
    )
    check("…and one the list has is only waited on", slugs, ["zero-one", "strong-law", "nets"])
    check(
        "a done item is refused", decide.prerequisite_lines([item("borel-cantelli")], 7, targets, areas)[2] != "", True
    )
    check(
        "an unknown roadmap is refused",
        decide.prerequisite_lines([item("x-y-z", "Nowhere")], 7, targets, areas)[2],
        "there is no roadmap `Nowhere`",
    )
    check(
        "an unknown need is refused",
        "does not have" in decide.prerequisite_lines([item("x-y-z", needs=["missing"])], 7, targets, areas)[2],
        True,
    )

    check(
        "an item that needs itself is refused",
        decide.prerequisite_lines([item("zero-one", needs=["zero-one"])], 7, targets, areas)[2],
        "`zero-one` needs itself",
    )
    check(
        "…and so is one that needs an item later in the ruling",
        "does not have"
        in decide.prerequisite_lines(
            [item("a-b-c", needs=["d-e-f"]), item("d-e-f", needs=["a-b-c"])], 7, targets, areas
        )[2],
        True,
    )
    check("the list is written", decide.add_prerequisites(path, lines), ["zero-one", "nets"])
    check("…under its lock", (tmp / ".targets.md.lock").exists(), True)
    after = parse_targets(path.read_text())
    added = after.find("zero-one")
    check(
        "the item parses as an open target",
        (added.status, added.needs, added.text),
        ("open", ("borel-cantelli",), item("zero-one")["text"]),
    )
    check("…that names what it unblocks", added.meta, ("unblocks: #7", "added by: decide"))
    check("it is first in its area", [it.slug for it in after.areas["Probability"]], ["zero-one", "strong-law"])
    check("a new area gets a section", [it.slug for it in after.areas["Topology"]], ["nets"])
    kept = [line for line in path.read_text().splitlines() if line and "decide" not in line and "Topology" not in line]
    check("the rest of the file is untouched", kept, [line for line in LIST.splitlines() if line])
    check(
        "an item the list gained meanwhile is not added twice",
        (decide.add_prerequisites(path, lines), path.read_text().count("`zero-one` —")),
        ([], 1),
    )
    check(
        "a list that cannot be read is reported, not raised", decide.add_prerequisites(tmp / "absent.md", lines), None
    )

    root = tmp / "prerequisite"
    path.write_text(LIST)
    with settings(root):
        declined(1)
        check(
            "without a list it is not offered",
            "prerequisite" in rounds(tmp, Hub(p1={}), {"rulings": []})["packet"]["cases"][0]["allowed"],
            False,
        )
    with settings(root, TAUCETI_ROADMAP_TARGETS=str(path)):
        declined(4, 5)
        hub = Hub(p4={}, p5={})
        asked = answer(
            (4, "prerequisite", {"items": [item("zero-one"), item("strong-law")]}),
            (5, "prerequisite", {"items": [item("x-y-z", "Nowhere")]}),
        )
        seen = rounds(tmp, hub, asked)
        check("with a list it is offered", "prerequisite" in seen["packet"]["cases"][0]["allowed"], True)
        check(
            "the packet names the list and the roadmaps",
            (seen["packet"]["target_list"], seen["packet"]["roadmaps"]),
            (str(path), str(tmp / "state" / "refs" / "roadmap" / ROADMAP_DIR)),
        )
        value = ruling(root, "declined-fix-4")
        check(
            "prerequisite: the pull request waits on both items",
            (value["ruling"], value["blocked_on_targets"]),
            ("prerequisite", ["zero-one", "strong-law"]),
        )
        check(
            "…and the list gains the new one",
            (value["added"], parse_targets(path.read_text()).find("zero-one").meta[0]),
            (["zero-one"], "unblocks: #4"),
        )
        refused = ruling(root, "declined-fix-5")
        check(
            "an item that cannot be added escalates",
            (refused["ruling"], refused["recommend"], "Nowhere" in refused["note"]),
            ("escalate", "prerequisite", True),
        )
        path.write_text(path.read_text().replace("- [ ] `zero-one`", "- [x] `zero-one`"))
        check(
            "one item landed: still waiting",
            (rounds(tmp, hub), ruling(root, "declined-fix-4")["ruling"]),
            ({"rc": None}, "prerequisite"),
        )
        path.write_text(path.read_text().replace("- [ ] `strong-law`", "- [x] `strong-law`"))
        rounds(tmp, hub)
        value = ruling(root, "declined-fix-4")
        check(
            "both landed: a retry by recheck",
            (value["ruling"], value["by"], "`strong-law` landed" in value["note"]),
            ("retry", "recheck", True),
        )


def test_roadmap(tmp):
    incident = {"pr": 7, "head": "a" * 40, "repository": TAUCETI}
    check(
        "a draft that cannot be written is reported, not raised",
        decide.write_draft(tmp / "absent", incident, "Probability", "T", "P"),
        None,
    )
    root = tmp / "roadmap"
    proposal = "In Layer 2 of the README, add the zero-one law before the strong law, which uses it. "
    with settings(root):
        declined(5, 6)
        change = {"area": "Probability", "title": "Add the zero-one law", "proposal": proposal}
        seen = rounds(
            tmp, Hub(p5={}, p6={}), answer((5, "roadmap", change), (6, "roadmap", {**change, "area": "Nowhere"}))
        )
        check("it is offered with a roadmap checkout", "roadmap" in seen["packet"]["cases"][0]["allowed"], True)
        value = ruling(root, "declined-fix-5")
        check(
            "roadmap: a draft file, named in the ruling",
            (value["ruling"], value["area"], value["draft"]),
            ("roadmap", "Probability", str(root / "drafts" / "roadmap-5.md")),
        )
        text = Path(value["draft"]).read_text()
        parts = ("# Add the zero-one law", "`Probability`", "/pull/5", "Nothing has been opened", proposal.strip())
        check("…saying what it is for and that nothing was opened", [part in text for part in parts], [True] * 5)
        refused = ruling(root, "declined-fix-6")
        check(
            "an unknown roadmap escalates and drafts nothing",
            (refused["ruling"], refused["recommend"], (root / "drafts" / "roadmap-6.md").exists()),
            ("escalate", "roadmap", False),
        )


def test_close(tmp):
    more = "+++ b/README.md\n+theorem prose : x\n+++ b/B.lean\n+@[simp] noncomputable def Foo.bar := 1\n+instance : Inhabited Nat := ⟨0⟩\n"
    check(
        "added declarations: named ones, in Lean files",
        decide.added_declarations(LEAN + more),
        {"strong_law", "Foo.bar"},
    )
    check("…private ones on request", decide.added_declarations(LEAN, private=True), {"strong_law", "aux"})

    root = tmp / "close"
    comment = "Everything here landed through #41, in the same file and under the same names."

    def attempt(hub=None, by=(41,), **env):
        hub = hub or Hub(p1={}, p41={"state": "MERGED"})
        with settings(root, **{"TAUCETI_DECIDE_CLOSE": "1", **env}):
            done, why = decide.close_subsumed(hub, 1, "head-1", list(by), comment, login="me")
        return done, why, [n for n, _ in hub.closed]

    def merged(**p1):
        return Hub(p1=p1, p41={"state": "MERGED"})

    check("off by default", attempt(TAUCETI_DECIDE_CLOSE=""), (False, "TAUCETI_DECIDE_CLOSE is not on", []))
    check("a moved head", attempt(merged(head="new"))[::2], (False, []))
    check("already closed", attempt(merged(state="CLOSED"))[::2], (False, []))
    check("a draft", attempt(merged(draft=True)), (False, "it is a draft", []))
    check("someone else's", attempt(merged(author="them")), (False, "its author is them, not this account", []))
    check(
        "under a hold label", attempt(merged(labels=["Do-Not-Close"])), (False, "it carries the label do-not-close", [])
    )
    check("the subsuming one is open", attempt(Hub(p1={}, p41={})), (False, "#41 has not merged", []))
    check("…or closed unmerged", attempt(Hub(p1={}, p41={"state": "CLOSED"})), (False, "#41 has not merged", []))
    check("…or itself", attempt(by=(1,)), (False, "#1 has not merged", []))
    other = Hub(p1={}, p41={"state": "MERGED", "diff": "+++ b/A.lean\n+theorem other : True\n"})
    check("it does not add every declaration", attempt(other), (False, "#41 do not add `strong_law`", []))
    check("nothing to compare", attempt(merged(diff="+++ b/README.md\n+words\n"))[::2], (False, []))
    check("an unreadable diff", attempt(merged(diff=None)), (False, "a diff could not be read", []))
    pushed = merged()
    pushed.on_diff = lambda n: n == 41 and pushed.prs[1].update(head="pushed")
    check(
        "a push while the diffs are read",
        attempt(pushed),
        (False, "it is no longer open at the head the ruling is about", []),
    )
    bors = {"state": "CLOSED", "title": "[Merged by Bors] - the strong law"}
    hub = Hub(p1={}, p41=bors, p42={"state": "MERGED", "diff": ""})
    done, why, closed = attempt(hub, by=(41, 42))
    check(
        "every check passes: closed; a Bors landing counts; private helpers are not required",
        (done, closed),
        (True, [1]),
    )
    check("…the evidence", why, "#41, #42 merged and add all 1 declaration(s) this pull request adds")
    check("…posted with the comment", (comment in hub.closed[0][1], why in hub.closed[0][1]), (True, True))
    with settings(root):
        for n in (7, 8):
            incidents.record(incidents.RULING, f"declined-fix-{n}", ruling="close")
    check(
        "the daily limit counts the directory's close rulings",
        attempt(TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="2"),
        (False, "today's limit of 2 closure(s) is used", []),
    )
    clock[0] += 86400
    check("…and starts again the next day", attempt(TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="2")[0], True)

    root = tmp / "close-round"
    asked = answer(
        (1, "close", {"subsumed_by": [41], "comment": comment}), (2, "close", {"subsumed_by": [42], "comment": comment})
    )
    hub = Hub(p1={}, p2={}, p3={"author": "them"}, p41={"state": "MERGED"}, p42={})
    with settings(root):
        declined(1)
        seen = rounds(tmp, hub, asked)
        check("without the switch it is not offered", "close" in seen["packet"]["cases"][0]["allowed"], False)
        check("…and a close answer closes nothing", (hub.closed, ruling(root)["ruling"]), ([], "escalate"))
    with settings(root, TAUCETI_DECIDE_CLOSE="1"):
        declined(1, 2, 3)
        claims = Claims()
        seen = rounds(tmp, hub, asked, claims=claims)
        check("each closure holds the branch claim and lets it go", (claims.taken, claims.released), ([1, 2], [1, 2]))
        allowed = {case["pr"]: "close" in case["allowed"] for case in seen["packet"]["cases"]}
        check("with it, on the account's own pull requests only", allowed, {1: True, 2: True, 3: False})
        value = ruling(root)
        check(
            "close: closed once the checks pass",
            ([n for n, _ in hub.closed], value["ruling"], value["subsumed_by"]),
            ([1], "close", [41]),
        )
        check("…with what was checked", "#41 merged" in value["evidence"], True)
        refused = ruling(root, "declined-fix-2")
        check(
            "a refused close escalates with the reason",
            (refused["ruling"], refused["recommend"], "#42 has not merged" in refused["note"]),
            ("escalate", "close", True),
        )
        rounds(tmp, hub)
        check("the closed pull request's ruling is retired", where(root, "ruling-declined-fix-1.json"), ["acked"])
        hub.prs[5] = {}
        declined(5)
        refused = rounds(tmp, hub, answer((5, "close", {"subsumed_by": [41], "comment": comment})), claims=Claims({5}))
        check(
            "a branch another worker claims is not closed",
            (5 in [n for n, _ in hub.closed], "branch claim" in ruling(root, "declined-fix-5")["note"]),
            (False, True),
        )


def main():
    def gmtime(seconds=None):
        return real_gmtime(clock[0] if seconds is None else seconds)

    with tempfile.TemporaryDirectory() as tmp, patch("time.gmtime", gmtime):
        tmp = Path(tmp)
        test_selection(tmp / "selection", tmp)
        test_schema()
        test_rounds(tmp)
        test_model_call(tmp / "model")
        test_prerequisite(tmp)
        test_roadmap(tmp)
        test_close(tmp)
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
