#!/usr/bin/env python3
"""The decide phase: off unless `--only` names it, then one ruling per incident, each a record.

GitHub is a stub and the model is a function that returns the answer file's contents. Covered: the
phase's selection and its due check; the answer schema; a round for each ruling, with the incident
acknowledged and the hold lifted or kept; a refused or malformed ruling becoming an escalation; the
later look at a ruling that waits; what the model process is given; and each side effect on its own.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_tools.forge import Forge, RepositoryRef

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
    drop = (
        incidents.ENV,
        incidents.HOLD_ENV,
        "TAUCETI_ROADMAP_TARGETS",
        "GH_TOKEN",
        "TAUCETI_WORKER_FORGES",
        "TAUCETI_NEW_PR_FORGE",
    )
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
            "headRefName": f"branch-{n}",
            "headRepositoryOwner": {"login": TAUCETI.split("/")[0]},
            "headRepository": {"name": TAUCETI.split("/")[1]},
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


FORGEJO = RepositoryRef(Forge("forgejo", "https://forge.example"), TAUCETI)


class ForgejoHub(Hub):
    """The same pull requests as `Hub`, served by a Forgejo source of the same repository name."""

    repository = FORGEJO
    direct_source = True

    def authenticated_actor(self):
        from tauceti_worker.source_identity import SourceActor

        return SourceActor.from_native(FORGEJO.forge, {"id": 3, "login": "me"})

    def live_pull(self, n):
        row = self._row(n)
        return {
            "state": row["state"].lower(),
            "head": {"sha": row["headRefOid"], "repo": {"full_name": TAUCETI}},
            "user": {"id": 3},
        }


def declined(*numbers):
    for number in numbers:
        incidents.record_declined("fix", number, f"head-{number}", message=f"#{number} cannot move")


class Claims:
    """The branch claims a round takes: `taken` and `released` list the pull requests, and one in
    `elsewhere` is held by another worker."""

    def __init__(self, elsewhere=(), unregistered=()):
        self.elsewhere, self.unregistered = set(elsewhere), set(unregistered)
        self.taken, self.released, self.held = [], [], None
        self.lost = threading.Event()

    def begin_branch_work(self, pr, head, refname, owner, repo):
        if pr in self.elsewhere:
            return False
        if pr in self.unregistered:
            return True  # as Claims does when claim.sh errors: go on, holding nothing
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
            patch.object(decide, "due", side_effect=lambda **k: asked.append(1) or real_due(**k)),
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
    test_several_sources(root, go, ran)


def test_several_sources(root, go, decided):
    """With a Forgejo source enabled, the phase runs on each source, GitHub first, ahead of either
    source's work; the first that starts the model ends the round."""
    from tauceti_worker import multi_source, worker_sources

    github = SimpleNamespace(cfg=SimpleNamespace(wid="w1", state=root.parent / "state"), gh="github-client")
    forgejo = SimpleNamespace(cfg=SimpleNamespace(wid="w1", state=root.parent / "forgejo"), gh="forgejo-client")

    def observed(*names):
        return [(name, github if name == "github" else forgejo, tc.Survey("w1")) for name in names]

    def ran(only, observations, outcomes):
        opts = tc.RoundOpts(only=only, agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
        activated = []
        with (
            settings(root, TAUCETI_NEW_PR_FORGE="github"),
            patch.object(multi_source, "log"),
            patch.object(multi_source, "collect", return_value=observations),
            patch.object(multi_source, "deduplicate"),
            patch.object(
                multi_source,
                "activate",
                side_effect=lambda gh, cfg: activated.append(gh) or contextlib.nullcontext(),
            ),
            patch.object(work_units, "run_round", side_effect=outcomes) as execute,
        ):
            try:
                result = multi_source.run_round(None, opts)
            except tc.NoProgress as error:
                result = str(error)
        calls = [(c.args[0], c.args[1].only) for c in execute.call_args_list]
        return result, calls, activated

    both = observed("forgejo", "github")
    check("decide alone: GitHub first", ran(["decide"], both, [0]), (0, [(github, ["decide"])], []))
    nothing = tc.NoProgress("decide: no live incident or ruling")
    check(
        "…then Forgejo, under its own activation, when GitHub had nothing",
        ran(["decide"], both, [nothing, 0]),
        (0, [(github, ["decide"]), (forgejo, ["decide"])], ["forgejo-client"]),
    )
    check(
        "…each with the other source's client for the target list",
        (github.target_sources, forgejo.target_sources),
        (["forgejo-client"], ["github-client"]),
    )
    result, calls, _ = ran(["decide", "fix"], both, [nothing, nothing, 0])
    check(
        "…with other work, that work follows when neither ruled",
        (result, [c[1] for c in calls]),
        ("no eligible PR work on the selected forges", [["decide"], ["decide"]]),
    )
    check(
        "…and a decide-only round says why for each",
        ran(["decide"], both, [nothing, nothing])[0],
        f"github: {nothing}; forgejo: {nothing}",
    )
    check(
        "a Forgejo-only round rules on Forgejo",
        ran(["decide"], observed("forgejo"), [0])[:2],
        (0, [(forgejo, ["decide"])]),
    )
    with (
        settings(root, TAUCETI_DECIDE_GAP="0"),
        patch.object(worker_sources, "enabled", return_value=True),
        patch.object(multi_source, "collect", side_effect=lambda w, *a, **k: [("github", w, tc.Survey("w1"))]),
        patch.object(multi_source, "deduplicate"),
    ):
        declined(9)
        check("through the worker's round: the phase is reached", (go(["decide"]), decided), (0, ["decide"]))


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
        declined(1, 2, 3)
        hub = Hub(p1={}, p2={}, p3={})

        def push_and_decline():
            hub.prs[1]["head"] = "head-1b"
            incidents.record_declined("fix", 1, "head-1b", message="declined at the new head")
            incidents.record_declined("fix", 3, "head-3", message="declined again within the second")
            later()
            declined(2)

        rounds(tmp, hub, answer((1, "retry", {}), (2, "retry", {}), (3, "retry", {})), meanwhile=push_and_decline)
        check(
            "an incident rewritten while the model ruled stays live: a new head, a later or same-second refresh",
            [where(root, f"declined-fix-{n}.json") for n in (1, 2, 3)],
            [["live"]] * 3,
        )
        same = incidents.find(incidents.DECLINED, "fix-3")[0]
        check(
            "…the same-second refresh has no ruling of its own",
            (same["count"], same["last_at"] == ruling(root, "declined-fix-3")["incident_at"], decide.ruling_on(same)),
            (2, True, None),
        )
        check(
            "…and each stays held",
            [incidents.declined("fix", n, head) for n, head in ((1, "head-1b"), (2, "head-2"), (3, "head-3"))],
            [True] * 3,
        )
        seen = rounds(tmp, hub, {"rulings": []})
        check(
            "…until it is ruled on in its own right",
            [(c["case"], c["head"]) for c in seen["packet"]["cases"]],
            [("declined-fix-1", "head-1b"), ("declined-fix-3", "head-3"), ("declined-fix-2", "head-2")],
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


TOKEN, BASE_TOKEN = "decide-test-token-7f3a", "decide-test-base-token-91c2"


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
                "GH_TOKEN": TOKEN,
                "GITHUB_TOKEN": TOKEN,
                "_TAUCETI_GITHUB_BASE_ENV": json.dumps({"GH_TOKEN": BASE_TOKEN, "GITHUB_TOKEN": BASE_TOKEN}),
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
        {TOKEN, BASE_TOKEN} & set(github.unselected_github_env(captured["env"]).values()),
        set(),
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
        ([(a, s) for a, s, _, _ in lines], problem),
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
    native = decide.prerequisite_lines([item("x-y-z")], decide.pull_ref(FORGEJO, 7), targets, areas)[0][0][2]
    check(
        "a Forgejo pull request is named by its URL, as the list's other references are",
        f"(unblocks: {FORGEJO.pull_url(7)};" in native,
        True,
    )
    check("…and a GitHub one by number", decide.pull_ref(None, 7), "#7")
    check("the list is written", decide.add_prerequisites(path, lines), (["zero-one", "nets"], ""))
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
        (([], ""), 1),
    )
    check(
        "a list that cannot be read is reported, not raised",
        decide.add_prerequisites(tmp / "absent.md", lines),
        (None, "the target list could not be written"),
    )
    path.write_text(LIST)
    later_lines, _, _ = decide.prerequisite_lines([item("dependent", needs=["strong-law"])], 7, targets, areas)
    path.write_text(LIST.replace("- [ ] `strong-law` — the strong law (needs: `borel-cantelli`)\n", ""))
    check(
        "a need removed from the list after the ruling was asked for refuses it",
        (decide.add_prerequisites(path, later_lines), "dependent" in path.read_text()),
        ((None, "`dependent` needs `strong-law`, which the target list no longer has"), False),
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
    check(
        "every closure is logged",
        (root / decide.CLOSURES).read_text().split()[1:],
        ["#1", "head-1"],
    )
    check(
        "the daily limit counts the log, whatever became of the rulings",
        attempt(TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="1"),
        (False, "today's limit of 1 closure(s) is used", []),
    )
    clock[0] += 86400
    check("…and starts again the next day", attempt(TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="1")[0], True)

    shared = tmp / "close-shared"
    native = ForgejoHub(p8={}, p41={"state": "MERGED"})
    racing = Hub(p1={}, p41={"state": "MERGED"})
    other = []

    def peer_closes(n):
        if n == 41 and not other:
            other.append(decide.close_subsumed(native, 8, "head-8", [41], comment, login="me"))

    racing.on_diff = peer_closes
    with settings(shared, TAUCETI_DECIDE_CLOSE="1", TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="1"):
        mine = decide.close_subsumed(racing, 1, "head-1", [41], comment, login="me")
    check(
        "two sources' rounds share the daily allowance: the one that reserves second does not close",
        (other[0][0], mine, [n for n, _ in native.closed], racing.closed),
        (True, (False, "today's limit of 1 closure(s) is used"), [8], []),
    )

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
    with settings(root, TAUCETI_DECIDE_CLOSE="1", TAUCETI_DECIDE_MAX_CLOSES_PER_DAY="3"):
        (root / decide.CLOSURES).unlink()
        for cycle in range(5):
            hub.prs[1] = {"head": f"reopened-{cycle}"}
            incidents.record_declined("fix", 1, f"reopened-{cycle}", message="reopened")
            rounds(tmp, hub, answer((1, "close", {"subsumed_by": [41], "comment": comment})))
            rounds(tmp, hub)
        check(
            "a pull request reopened and closed again counts each time",
            ([n for n, _ in hub.closed].count(1), len((root / decide.CLOSURES).read_text().splitlines())),
            (4, 3),
        )
    with settings(root, TAUCETI_DECIDE_CLOSE="1"):
        hub.prs[5] = {}
        declined(5)
        refused = rounds(tmp, hub, answer((5, "close", {"subsumed_by": [41], "comment": comment})), claims=Claims({5}))
        check(
            "a branch another worker claims is not closed",
            (5 in [n for n, _ in hub.closed], "branch claim" in ruling(root, "declined-fix-5")["note"]),
            (False, True),
        )
        hub.prs[7] = {}
        declined(7)
        losing = Claims()
        hub.on_diff = lambda n: n == 41 and losing.lost.set()
        with patch.dict(os.environ, {"TAUCETI_DECIDE_MAX_CLOSES_PER_DAY": "99"}):
            rounds(tmp, hub, answer((7, "close", {"subsumed_by": [41], "comment": comment})), claims=losing)
        hub.on_diff = lambda n: None
        check(
            "…nor one whose claim is lost while the diffs are read",
            (7 in [n for n, _ in hub.closed], "claim was lost" in ruling(root, "declined-fix-7")["note"]),
            (False, True),
        )
        hub.prs[6] = {}
        declined(6)
        refused = rounds(
            tmp, hub, answer((6, "close", {"subsumed_by": [41], "comment": comment})), claims=Claims(unregistered={6})
        )
        check(
            "…nor one whose claim could not be registered",
            (6 in [n for n, _ in hub.closed], "could not be taken" in ruling(root, "declined-fix-6")["note"]),
            (False, True),
        )


def forgejo_answer(*rows):
    return {
        "rulings": [
            {"case": f"declined-{incidents.subject('fix', n, FORGEJO)}", "ruling": r, "note": f"because of {r}", **f}
            for n, r, f in rows
        ]
    }


def test_forgejo(tmp):
    """A Forgejo source's incidents are ruled on in a round on that source, apart from GitHub's."""
    root = tmp / "forgejo"
    forgejo_name = f"declined-{incidents.subject('fix', 1, FORGEJO)}.json"
    with settings(root):
        declined(1)
        incidents.record_declined("fix", 1, "head-1", message="#1 declined on Forgejo", repository=FORGEJO)
        seen = rounds(tmp, ForgejoHub(p1={}), forgejo_answer((1, "retry", {})))
        case = seen["packet"]["cases"]
        check(
            "a Forgejo round puts only the Forgejo record to the model, named by its source",
            ([c["case"] for c in case], case[0]["url"], seen["packet"]["repository"]),
            ([forgejo_name[: -len(".json")]], FORGEJO.pull_url(1), FORGEJO.url),
        )
        value = load(root / f"ruling-{forgejo_name}")
        check(
            "…its ruling carries the source and the incident is acknowledged under its own name",
            (value["ruling"], value["forge"]["kind"], where(root, forgejo_name), where(root, "declined-fix-1.json")),
            ("retry", "forgejo", ["acked"], ["live"]),
        )
        check(
            "a retry lifts only that source's hold",
            (incidents.declined("fix", 1, "head-1", FORGEJO), incidents.declined("fix", 1, "head-1")),
            (False, True),
        )
        check(
            "…and notes only that source's prompt",
            (
                "because of retry" in decide.retry_note("fix", 1, "head-1", FORGEJO),
                decide.retry_note("fix", 1, "head-1"),
            ),
            (True, ""),
        )
        check(
            "the GitHub round is not delayed by the Forgejo one",
            (decide.due(source=FORGEJO)[0], decide.due()[0]),
            (False, True),
        )
        seen = rounds(tmp, Hub(p1={"state": "CLOSED"}))
        check(
            "a GitHub round settles only GitHub's records",
            (where(root, "declined-fix-1.json"), where(root, f"ruling-{forgejo_name}")),
            (["acked"], ["live"]),
        )

    root = tmp / "forgejo-close"
    comment = "Everything here landed through #41, in the same file and under the same names."
    with settings(root, TAUCETI_DECIDE_CLOSE="1"):
        hub = ForgejoHub(p8={}, p41={"state": "MERGED"})
        incidents.record_declined("fix", 8, "head-8", message="subsumed", repository=FORGEJO)
        lease = SimpleNamespace(acquire=lambda: 0, released=[])
        lease.release = lambda: lease.released.append(True)
        from tauceti_worker import source_claims

        ctx = SimpleNamespace(add_cleanup=lambda f: None)
        cfg = SimpleNamespace(state=tmp / "forgejo-state", wid="w1")
        with (
            patch.object(source_claims, "lease", return_value=lease) as taken,
            patch.object(source_claims.SourceClaims, "start_heartbeat"),
            patch.dict(os.environ, {}),
        ):
            claims = source_claims.SourceClaims(cfg, ctx, hub)
            seen = rounds(
                tmp, hub, forgejo_answer((8, "close", {"subsumed_by": [41], "comment": comment})), claims=claims
            )
        check(
            "a subsumed Forgejo pull request is offered close", "close" in seen["packet"]["cases"][0]["allowed"], True
        )
        check(
            "…and closed under its own source's branch lease, released after",
            ([n for n, _ in hub.closed], taken.call_args.args[2], lease.released, claims.held),
            ([8], "branch/8", [True], None),
        )
        check(
            "…the closure log names it by its URL",
            (root / decide.CLOSURES).read_text().split()[1:],
            [FORGEJO.pull_url(8), "head-8"],
        )


def test_forgejo_adapters(tmp):
    """`pr_diff` and `close_pr` on the Forgejo worker source."""
    from tauceti_worker import forgejo_worker
    from tauceti_worker.forgejo_worker import ForgejoWorkerSource

    remote = tmp / "remote.git"

    def git(*arguments, cwd=remote):
        return subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True, check=True, text=True).stdout

    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    with patch.dict(os.environ, env):
        work = tmp / "work"
        work.mkdir()
        git("init", "--quiet", "-b", "main", cwd=work)
        (work / "A.lean").write_text("theorem base_fact : True := trivial\n")
        git("add", ".", cwd=work)
        git("commit", "--quiet", "-m", "base", cwd=work)
        base = git("rev-parse", "HEAD", cwd=work).strip()
        (work / "A.lean").write_text("theorem base_fact : True := trivial\ntheorem strong_law : True := trivial\n")
        git("commit", "--quiet", "-am", "pr", cwd=work)
        head = git("rev-parse", "HEAD", cwd=work).strip()
        git("clone", "--quiet", "--bare", str(work), str(remote), cwd=tmp)
        git("update-ref", "refs/pull/5/head", head)
        pulls = {5: {"state": "open", "base": {"sha": base}, "head": {"sha": head}}}
        fake = SimpleNamespace(
            repository=SimpleNamespace(clone_url=str(remote)),
            live_pull=lambda n: pulls.get(n),
        )
        diff = ForgejoWorkerSource.pr_diff(fake, 5)
        check(
            "pr_diff: the merge-base diff from fetched commits", decide.added_declarations(diff or ""), {"strong_law"}
        )
        check("…None for a pull request it cannot read", ForgejoWorkerSource.pr_diff(fake, 6), None)

    calls = []

    class Writer:
        def __init__(self, api, expected_user_id=None):
            calls.append(("writer", expected_user_id))

        def lifecycle(self, repository, number, *, pull, state, comment=None):
            calls.append((number, pull, state, comment))
            pulls[number]["state"] = state

    fake = SimpleNamespace(
        api=None,
        repository=FORGEJO,
        authenticated_actor=ForgejoHub().authenticated_actor,
        validate_binding=lambda: None,
        invalidate=lambda n: calls.append(("invalidate", n)),
        live_pull=lambda n: pulls.get(n),
    )
    with patch("tauceti_tools.forge_write.ForgejoWriter", Writer):
        closed = ForgejoWorkerSource.close_pr(fake, 5, "subsumed")
    check(
        "close_pr: a lifecycle close with the comment, as the source's own actor, confirmed by a fresh read",
        (closed, calls),
        (True, [("writer", "3"), (5, True, "closed", "subsumed"), ("invalidate", 5)]),
    )
    del forgejo_worker


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
        test_forgejo(tmp)
        test_forgejo_adapters(tmp)
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
