#!/usr/bin/env python3
"""A Bubble repair round that ends before its agent starts gives its attempt back, three times a head.

The bootstrap prints agents.AGENT_STARTING as its last act before the agent. A failed round without
that line and without a structured agent event is classified agents.AGENT_NOT_STARTED; a round whose
agent started and then failed is not, whatever it printed. _refund_infra_failure hands such a round's
counters back MAX_SANDBOX_REFUNDS times per head, counts each in the pull request's provider-refund
allowance, and charges from then on. The round that uses the allowance up writes one `sandbox-failed`
incident when TAUCETI_INCIDENTS_DIR is set; unset, the refunds are the same and nothing is written.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_tools.forge import Forge, RepositoryRef

import tauceti_worker as tc
from tauceti_worker import agents, incidents, work_units

survey_mod = importlib.import_module("tauceti_worker.survey")
NoProgress = tc.config.NoProgress
LIMIT = work_units.MAX_SANDBOX_REFUNDS

fails = 0


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}" + ("" if ok else f": got={got!r} want={want!r}"))


def settings(root=None):
    env = {k: v for k, v in os.environ.items() if k not in (incidents.ENV, "TAUCETI_STREAM")}
    if root is not None:
        env[incidents.ENV] = str(root)
    return patch.dict(os.environ, env, clear=True)


INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s", "model": "opus"})


def test_classification(tmp):
    """The real bootstrap under a fake `lake`, read by the real run_agent_proc."""
    shims = tmp / "bin"
    shims.mkdir()
    lake = shims / "lake"
    lake.write_text('#!/bin/sh\n[ "$*" = "exe cache get" ] && exit "${MATHLIB_RC:-0}"\nexit 0\n')
    lake.chmod(0o755)

    def round_of(inner="", *, mathlib=0, marker=agents.AGENT_STARTING, command=None, cancel=None):
        logdir = Path(tempfile.mkdtemp(dir=tmp))
        env = {**os.environ, "PATH": f"{shims}:{os.environ.get('PATH', '')}", "MATHLIB_RC": str(mathlib)}
        argv = ["bash", "-c", command or agents.bubble_work_cmd(inner)]
        with settings():
            rc = agents.run_agent_proc(
                argv,
                env=env,
                logdir=logdir,
                label="agent-claude",
                provider="claude",
                start_marker=marker,
                cancel_event=cancel,
            )
        transcript = next(logdir.glob("agent-claude-*.log")).read_text()
        return rc, agents.take_last_agent_infra_failure(), transcript

    rc, reason, transcript = round_of("sh -c 'exit 0'", mathlib=1)
    check("a failed Mathlib fetch ends the round before the agent", (rc != 0, reason), (True, agents.AGENT_NOT_STARTED))

    rc, reason, transcript = round_of("sh -c 'echo working; exit 0'")
    check("a round whose agent starts and succeeds is not classified", (rc, reason), (0, None))
    check("the start line stays out of the transcript", agents.AGENT_STARTING in transcript, False)
    check("…and the agent's own output stays in", "working" in transcript, True)

    rc, reason, _ = round_of("sh -c 'echo Failed to authenticate. API Error: 401; exit 1'")
    check("an agent that starts and exits at once stays charged", (rc, reason), (1, None))

    rc, reason, _ = round_of(command=f"echo '{INIT}'; exit 1")
    check("a structured agent event without the start line stays charged", (rc, reason), (1, None))

    rc, reason, _ = round_of(command="echo 'mount source does not exist' >&2; exit 1")
    check("a sandbox that never ran its command is a sandbox failure", (rc, reason), (1, agents.AGENT_NOT_STARTED))

    rc, reason, _ = round_of(command="exit 1", marker=None)
    check("a host run, which names no start line, is classified as before", (rc, reason), (1, None))

    rc, reason, transcript = round_of(command=f"printf 'fetching'; echo '{agents.AGENT_STARTING}'; exit 1")
    check("a start line glued to unterminated output still counts", reason, None)
    check("…and that line is kept", "fetching" in transcript, True)

    lost = threading.Event()
    threading.Timer(0.2, lost.set).start()
    rc, reason, _ = round_of(command="sleep 30", cancel=lost)
    check("a bootstrap stopped for a lost lease is not a sandbox failure", (rc != 0, reason), (True, None))

    with patch.object(agents, "TAUCETI_CACHE_SERVICE", ""):
        bare = agents.bubble_work_cmd("agent --run")
    check(
        "the bootstrap without a TauCeti cache service prints the start line too",
        bare.endswith(f"echo '{agents.AGENT_STARTING}'; exec agent --run"),
        True,
    )


HEAD = "a" * 40
KEY = f"fix-412-{HEAD[:12]}"


def worker(state, logdir="/logs/w1"):
    cfg = SimpleNamespace(state=Path(state), logdir=Path(logdir))
    return SimpleNamespace(cfg=cfg, counters=work_units.Counters(cfg))


def sandbox_round(w, head=HEAD, *, reason=None, key=KEY, label="fix", source=None, source_id=None):
    """One failed round at `head`: charge, fail, ask for the refund. True when it was handed back."""
    w.counters.incr(key)
    agents._LAST_AGENT_FAILURE = reason or agents.AGENT_NOT_STARTED
    agents._LAST_AGENT_RUN = {"tail": ["Error: instance failed to start"], "transcript": "/logs/w1/agent.log"}
    c = SimpleNamespace(pr=412, head=head, source_repository=source, source_repository_id=source_id)
    try:
        work_units._refund_infra_failure(w, c, label, (key,))
    except NoProgress:
        return True
    return False


def test_refund(tmp):
    root = tmp / "records"
    with settings():
        w = worker(tmp / "unset")
        check(
            f"unset: {LIMIT} rounds at one head are handed back and the loop backs off",
            [sandbox_round(w) for _ in range(LIMIT)],
            [True] * LIMIT,
        )
        check("unset: none of them is charged", w.counters.read(KEY), 0)
        check("unset: the next one is charged", (sandbox_round(w), w.counters.read(KEY)), (False, 1))
        check("unset: no record and no directory", (incidents.records(), root.exists()), ([], False))

    with settings(root):
        w = worker(tmp / "set")
        handed_back = [sandbox_round(w) for _ in range(LIMIT - 1)]
        check("set: no record before the allowance is used up", incidents.records(), [])
        handed_back.append(sandbox_round(w))
        check("set: the same rounds are handed back", handed_back, [True] * LIMIT)
        path = root / "sandbox-failed-fix-412.json"
        value = json.loads(path.read_text())
        check(
            "the round that uses the allowance up records the head and the sandbox's last lines",
            {k: value[k] for k in ("schema", "kind", "key", "stage", "pr", "head", "failures", "tail", "transcript")},
            {
                "schema": incidents.SCHEMA,
                "kind": "sandbox-failed",
                "key": "fix-412",
                "stage": "fix",
                "pr": 412,
                "head": HEAD,
                "failures": LIMIT,
                "tail": ["Error: instance failed to start"],
                "transcript": "/logs/w1/agent.log",
            },
        )
        check("each refund counts in the PR's provider-refund allowance", w.counters.read("infra-fix-412"), LIMIT)
        check("later rounds at that head are charged", (sandbox_round(w), sandbox_round(w)), (False, False))
        check("…both of them", w.counters.read(KEY), 2)
        check("…without using the PR's allowance", w.counters.read("infra-fix-412"), LIMIT)
        check("…or writing again", json.loads(path.read_text())["count"], 1)
        check(
            "a provider outage at that head is still handed back",
            (sandbox_round(w, reason="provider returned 529"), w.counters.read(KEY)),
            (True, 2),
        )
        check("another stage at that head has its own allowance", sandbox_round(w, key="rebase-k", label="rebase"))

        moved = "b" * 40
        moved_key = f"fix-412-{moved[:12]}"
        check(
            "a new head has a new allowance",
            [sandbox_round(w, moved, key=moved_key) for _ in range(LIMIT + 1)],
            [True] * LIMIT + [False],
        )
        value = json.loads(path.read_text())
        check("…and its record replaces the old head's", (value["head"], value["count"]), (moved, 2))

        # A peer keeps its own count, as it keeps its own attempt budget; the record is per head.
        (root / incidents.ACKED).mkdir()
        os.replace(path, root / incidents.ACKED / path.name)
        peer = worker(tmp / "peer")
        check(
            "a peer's rounds are handed back too",
            [sandbox_round(peer, moved, key=moved_key) for _ in range(LIMIT)],
            [True] * LIMIT,
        )
        check("an acknowledged record of that head is not written again", path.exists(), False)

    with settings(root):
        w = worker(tmp / "lifetime")
        w.counters.write("infra-fix-412", work_units.MAX_INFRA_REFUNDS)
        check("a PR past its provider-refund allowance is charged at any head", sandbox_round(w, "c" * 40), False)

    agents._LAST_AGENT_FAILURE = agents._LAST_AGENT_RUN = None


FORGEJO = RepositoryRef(Forge("forgejo", "https://forge.example"), "TauCetiProject/TauCeti")


def test_other_source(tmp):
    """A pull request of another source is recorded under that source's key, beside a GitHub record
    of the same number and head."""
    root = tmp / "sources"
    with settings(root):
        for name in ("github", "forgejo"):
            w = worker(tmp / f"source-{name}")
            for _ in range(LIMIT):
                sandbox_round(
                    w, source=FORGEJO if name == "forgejo" else None, source_id=9 if name == "forgejo" else None
                )
        digest = hashlib.sha256(FORGEJO.key.encode()).hexdigest()[:12]
        github = json.loads((root / "sandbox-failed-fix-412.json").read_text())
        native = json.loads((root / f"sandbox-failed-fix-412.{digest}.json").read_text())
        check(
            "a GitHub record keeps its name and names TAUCETI on GitHub",
            (github["key"], github["repository"], github["forge"]["kind"], "repository_id" in github),
            ("fix-412", tc.TAUCETI, "github", False),
        )
        check(
            "a Forgejo record is keyed by its source and names it",
            (native["key"], native["repository"], native["forge"], native["repository_id"], native["count"]),
            (f"fix-412.{digest}", FORGEJO.name, {"kind": "forgejo", "url": "https://forge.example"}, 9, 1),
        )
    agents._LAST_AGENT_FAILURE = agents._LAST_AGENT_RUN = None


def test_repair_round(tmp):
    """_do_fixlike under Bubble: the refund is reached, and a Bubble that cannot be spawned is not charged."""
    from tauceti_worker.github import GitHub

    gh = GitHub()
    raw = {
        "number": 20,
        "headRefOid": "head-20",
        "headRefName": "feature",
        "headRepositoryOwner": {"login": "me"},
        "headRepository": {"name": "TauCeti"},
        "statusCheckRollup": [],
        "author": {"login": "me"},
        "mergeable": "CONFLICTING",
        "labels": [],
    }
    w = worker(tmp / "repair")
    w.gh = gh
    w.claims = SimpleNamespace(begin_branch_work=lambda *args: True)
    w.rs = SimpleNamespace(bust=lambda *args: None)

    def sandbox_fails(*_args, **_kwargs):
        agents._LAST_AGENT_FAILURE = agents.AGENT_NOT_STARTED
        return 1

    def repair(bubble_result):
        w.counters.write("rebase-key", 1)
        with (
            settings(),
            patch.object(survey_mod, "me", return_value="me"),
            patch.object(work_units, "me", return_value="me"),
            patch.object(gh, "open_prs", return_value=[raw]),
            patch.object(gh, "issue_comments", return_value=[]),
            patch.object(gh, "pr_progress_state", return_value=None),
            patch.object(gh, "pr_view", return_value={**raw, "state": "OPEN", "isDraft": False}),
            patch.object(work_units, "run_in_bubble", side_effect=bubble_result),
        ):
            sv = survey_mod.survey(SimpleNamespace(wid="test"), gh, None, SimpleNamespace(read=lambda _: 0), deep=False)
            try:
                work_units._do_fixlike(
                    w,
                    sv,
                    sv.rebaseable.actionable[0],
                    SimpleNamespace(agent_name="Codex"),
                    True,
                    prompt_file="rebase.md",
                    label="rebase",
                    charged=("rebase-key",),
                )
            except NoProgress as error:
                return type(error).__name__, w.counters.read("rebase-key")
        return None, w.counters.read("rebase-key")

    check("a sandbox failure in a repair round is handed back", repair(sandbox_fails), ("NoProgress", 0))
    check(
        "a Bubble process that cannot be created is not charged",
        repair(agents.AgentLaunchUnavailable("Agent process could not start: [Errno 23]")),
        ("AgentLaunchUnavailable", 0),
    )
    check("…and does not use the head's allowance", w.counters.read("infra-rebase-20-head-20"), 1)
    agents._LAST_AGENT_FAILURE = None


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test_classification(tmp)
        test_refund(tmp)
        test_other_source(tmp)
        test_repair_round(tmp)
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
