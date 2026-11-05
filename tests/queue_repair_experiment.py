#!/usr/bin/env python3
"""The historical repair experiment harness: selection, diagnostics, chains, measurement, reporting.

No inference and no Lake here: the agent and build calls are never reached. The chain functions run
against a throwaway Git repository.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_queue import repair_experiment as rx

FIXTURE = REPO / "tests" / "fixtures" / "bors_candidates_repair_sample.json"
fails = 0


def check(name, condition):
    global fails
    ok = bool(condition)
    print(f"[{'OK ' if ok else 'XX '}] {name}")
    fails += not ok


# ---- selection and lineage grouping ---------------------------------------------------------

journal = rx.load_journal([FIXTURE])
check("fixture loads seven candidates", sorted(journal) == [1863, 1909, 1916, 1926, 1931, 1932, 1934])
selected = rx.select_candidates(journal)
ids = [f.candidate_id for f in selected]
check("failed candidates with compiler/audit/lint diagnostics are selected", ids == [1863, 1916, 1926, 1931, 1932])
check("a construction conflict is not selected", 1909 not in ids)
check(
    "a promoted candidate is not selected, even when forced",
    1934 not in [f.candidate_id for f in rx.select_candidates(journal, (1934,))],
)
by_id = {f.candidate_id: f for f in selected}
check("compiler failure kind", by_id[1931].kinds == ["compiler"])
check("lint failure kind", by_id[1863].kinds == ["lint"])
check("audit failure kind", by_id[1926].kinds == ["audit"])
check(
    "compiler failing module from paths",
    by_id[1931].failing_modules == ["TauCeti/AlgebraicGeometry/Modules/PseudoCoherent/Pullback.lean"],
)
check(
    "lint failing module decoded from the private declaration",
    by_id[1863].failing_modules == ["TauCeti/LowDimTopology/DehnSurgery/Slope.lean"],
)
check("audit failing modules are both sides", len(by_id[1926].failing_modules) == 2)
check(
    "the provisional suspect is recorded with its head",
    by_id[1931].suspects == [26269] and by_id[1931].suspect_heads[26269].startswith("91d9dca0"),
)
check(
    "the lint partition's component is not a direct suspect list",
    25177 in by_id[1863].suspects and len(by_id[1863].suspects) == 6,
)
check(
    "retained commit and toolchain come from the constructed event",
    by_id[1931].commit.startswith("8c596001ee") and by_id[1931].toolchain == "leanprover/lean4:v4.36.0-rc2",
)
check("CI run and job ids are bound", by_id[1931].run_id == 41725530762 and by_id[1931].job_ids == [126229633128])
check(
    "lineage follows parent candidates through the journal",
    by_id[1916].lineage == by_id[1931].lineage == by_id[1932].lineage == 1871,
)
check("a candidate whose parent is outside the journal roots at that parent", by_id[1863].lineage == 1862)
groups = rx.lineages(selected)
check("lineage grouping", groups[1871] == [1916, 1931, 1932] and groups[1862] == [1863] and groups[1882] == [1926])
table = rx.selection_table(selected)
check("selection table lists every candidate", all(f"| {cid} |" in table for cid in ids))
record = by_id[1931].to_json()
check(
    "failure serialises with kinds and modules",
    record["kinds"] == ["compiler"] and record["failing_modules"] and record["members"],
)

check("private declaration decoding", rx.declaration_module("_private.TauCeti.A.B.0.TauCeti.c") == "TauCeti/A/B.lean")
check("plain declarations have no module", rx.declaration_module("TauCeti.A.b") is None)

# ---- CI log diagnostics ------------------------------------------------------------------------

LOG = "\n".join(
    [
        "2026-11-03T22:06:37.7886292Z \x1b[36;1m✔ [11266/11779] Built TauCeti.Algebra.Polynomial.Taylor (3.2s)\x1b[0m",
        "2026-11-03T22:06:38.0000000Z ✔ [11267/11779] Built TauCeti.Data.Finsupp.Weight (3.3s)",
        "2026-11-03T22:14:18.2431910Z ✖ [35800/36315] Building TauCeti.AlgebraicGeometry.Modules.PseudoCoherent.Pullback (10s)",
        "2026-11-03T22:14:18.2431910Z trace: .> LEAN_PATH=... lean TauCeti/AlgebraicGeometry/Modules/PseudoCoherent/Pullback.lean",
        "2026-11-03T22:14:18.2431910Z error: TauCeti/AlgebraicGeometry/Modules/PseudoCoherent/Pullback.lean:101:5: Unknown identifier `restrictPullbackObjIso`",
        "2026-11-03T22:14:18.2431910Z   with context",
        "2026-11-03T22:14:18.2431910Z ⊘ [35810/36675] Canceled TauCeti.AlgebraicGeometry.Modules.Quasicoherent",
        "2026-11-03T22:14:18.2431910Z audit-driver: phase audit-duplicates",
        "2026-11-03T22:14:18.2431910Z duplicate-declarations: SimpleGraph.Walk.IsPath.start_notMem_tail_support is declared in both TauCeti.Combinatorics.SimpleGraph.Connectivity.Menger and TauCeti.Combinatorics.SimpleGraph.Paths",
        "2026-11-03T22:14:18.2431910Z audit-driver: import TauCeti.Algebra.Lie.NormTwoShell.D4.Triality failed, environment already contains 'TauCeti.D4Triality.normTwoShellCongr_root' from TauCeti.LinearAlgebra.Matrix.NormTwoShell.TrialityD4",
        '2026-11-03T22:14:18.2431910Z LINT-ENV-LOCATIONS {"version":1,"complete":true,"findings":[{"linter":"unusedArguments","declaration":"_private.TauCeti.LowDimTopology.DehnSurgery.Slope.0.TauCeti.slopeSetoid","unknown":"unsupported_origin"}]}',
        "2026-11-03T22:14:18.2431910Z lint-env: NEW violation(s) not in the grandfathered baseline (as '<linter> <declaration>'):",
        "2026-11-03T22:14:18.2431910Z   [unusedArguments]",
        "2026-11-03T22:14:18.2431910Z   #check TauCeti.slopeSetoid✝ /- 1 unused argument:",
        "2026-11-03T22:14:18.2431910Z ##[error]lint-env: 1 new violation(s); see the list above",
    ]
)
findings = rx.extract_log_findings(LOG)
check(
    "compiler error with its continuation line",
    findings["compiler_errors"]
    == [
        {
            "path": "TauCeti/AlgebraicGeometry/Modules/PseudoCoherent/Pullback.lean",
            "line": 101,
            "column": 5,
            "message": "Unknown identifier `restrictPullbackObjIso`\n  with context",
        }
    ],
)
check(
    "failed module from the cross",
    findings["failed_modules"] == ["TauCeti.AlgebraicGeometry.Modules.PseudoCoherent.Pullback"],
)
check(
    "duplicate declaration finding",
    findings["duplicate_declarations"][0]["modules"]
    == ["TauCeti.Combinatorics.SimpleGraph.Connectivity.Menger", "TauCeti.Combinatorics.SimpleGraph.Paths"],
)
check(
    "import collision finding",
    findings["import_collisions"][0]["declaration"] == "TauCeti.D4Triality.normTwoShellCongr_root",
)
check(
    "lint locations and report",
    findings["lint"]["findings"][0]["linter"] == "unusedArguments" and len(findings["lint"]["report"]) == 3,
)
check("built and canceled counters", findings["built_modules"] == 2 and findings["canceled_jobs"] == 1)
modules = rx.findings_modules(findings)
check(
    "modules named by findings",
    modules[0].endswith("Pullback.lean")
    and "TauCeti/Combinatorics/SimpleGraph/Paths.lean" in modules
    and "TauCeti/LowDimTopology/DehnSurgery/Slope.lean" in modules,
)

build_counts = rx.parse_lake_build_log(
    "✔ [1/3] Unpacked TauCeti.A\n✔ [2/3] Built TauCeti.B (1s)\n✖ [3/3] Building TauCeti.C\n⊘ [3/3] Canceled TauCeti.D\n"
)
check(
    "lake build log counters",
    build_counts["fetched"] == 1
    and build_counts["built"] == 1
    and build_counts["failed_modules"] == ["TauCeti.C"]
    and build_counts["canceled"] == 1
    and build_counts["total_jobs"] == 3,
)

# ---- chain construction, validation and comparison on a throwaway repository ------------------


def sh(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True, env=env
    ).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            sh(repo, "rm", "--quiet", name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            sh(repo, "add", name)
    sh(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    sh(repo, "config", "user.name", "t")
    sh(repo, "config", "user.email", "t@x")
    base = commit(repo, "base", {"TauCeti/A.lean": "theorem a : True := trivial\n"})
    first = commit(
        repo,
        "feat: improve A (#1)\n\nBody of one.\n",
        {"TauCeti/A.lean": "theorem a : True := by trivial\ntheorem a2 : True := trivial\n"},
    )
    second = commit(
        repo,
        "feat: add B (#2)\n\nBody of two.\n\n🤖 Generated with Claude\n",
        {"TauCeti/B.lean": "theorem b : True := trivial\n"},
    )
    members = [rx.Member(1, "a" * 40, None, None, None), rx.Member(2, "b" * 40, None, None, None)]
    tip, chain = rx.build_attributed_chain(repo, base, second, members)
    check("attributed chain has one commit per member in order", [c.pr for c in chain] == [1, 2])
    check(
        "trailers carry the source PR and accepted head",
        chain[1].accepted_head == "b" * 40
        and "Source-PR: TauCetiProject/TauCeti#2" in sh(repo, "log", "-1", "--format=%B", tip),
    )
    check(
        "the attributed tip tree equals the retained tree",
        sh(repo, "rev-parse", f"{tip}^{{tree}}") == sh(repo, "rev-parse", f"{second}^{{tree}}"),
    )
    check(
        "message without trailers is the original", chain[0].message.strip() == "feat: improve A (#1)\n\nBody of one."
    )
    check("attributed chain validates", rx.validate_chain(repo, base, tip, [1, 2]) == [])
    check(
        "validation reports a missing member and a non-member",
        any("members without a commit: [3]" in p for p in rx.validate_chain(repo, base, tip, [1, 3]))
        and any("non-members: [2]" in p for p in rx.validate_chain(repo, base, tip, [1, 3])),
    )
    check(
        "the retained chain without trailers is rejected as unattributed",
        "a commit has no Accepted-Head trailer" in rx.validate_chain(repo, base, second, [1, 2]),
    )
    try:
        rx.build_attributed_chain(repo, base, second, members[:1])
        check("member mismatch raises", False)
    except RuntimeError as error:
        check("member mismatch raises", "unexpected [2]" in str(error))
    check(
        "file ownership names the PR whose commit touched the path",
        rx.owners_of(repo, chain, "TauCeti/A.lean") == [1] and rx.owners_of(repo, chain, "TauCeti/B.lean") == [2],
    )

    # An agent edit: PR 1's content changes with an appended explanation; PR 2 is reduced to an empty commit.
    sh(repo, "checkout", "--quiet", "--detach", base)
    message_one = sh(repo, "log", "-1", "--format=%B", chain[0].sha)
    one_body, _ = rx._split_trailers(repo, message_one)
    new_one = commit(
        repo,
        one_body.rstrip()
        + "\n\nRepair: use `trivial` directly; the `by` block no longer elaborates.\n\nSource-PR: TauCetiProject/TauCeti#1\nAccepted-Head: "
        + "a" * 40
        + "\n",
        {"TauCeti/A.lean": "theorem a : True := trivial\ntheorem a2 : True := trivial\n"},
    )
    two_body, _ = rx._split_trailers(repo, sh(repo, "log", "-1", "--format=%B", chain[1].sha))
    new_two = commit(
        repo,
        two_body.rstrip()
        + "\n\nReduced to an empty commit: B duplicates a Mathlib lemma.\n\nSource-PR: TauCetiProject/TauCeti#2\nAccepted-Head: "
        + "b" * 40
        + "\n",
        {},
    )
    after = rx.read_chain(repo, base, new_two)
    check(
        "the replacement chain validates with its empty commit",
        rx.validate_chain(repo, base, new_two, [1, 2]) == [] and after[1].empty,
    )
    rows = rx.compare_chains(repo, chain, after)
    one, two = rows
    check(
        "PR 1 is edited with its explanation collected",
        one["status"] == "edited" and one["message_preserved"] and one["explanation"].startswith("Repair:"),
    )
    check("PR 1 is not a shrink event", not one["shrink"])
    check(
        "PR 2 became empty and is a shrink event",
        two["status"] == "edited" and two["empty_after"] and not two["empty_before"] and two["shrink"],
    )
    check("diffstats are recorded", one["diffstat_before"]["insertions"] == 2 and two["diffstat_after"]["files"] == 0)
    unchanged = rx.compare_chains(repo, chain, chain)
    check("an identical chain is clean everywhere", all(r["status"] == "clean" and not r["shrink"] for r in unchanged))
    # A message rewritten rather than appended fails the append-only check.
    sh(repo, "checkout", "--quiet", "--detach", base)
    rewritten = commit(
        repo,
        "feat: improve A (#1)\n\nRewritten body.\n\nSource-PR: TauCetiProject/TauCeti#1\nAccepted-Head: "
        + "a" * 40
        + "\n",
        {"TauCeti/A.lean": "theorem a : True := trivial\n"},
    )
    rewritten_rows = rx.compare_chains(repo, chain[:1], rx.read_chain(repo, base, rewritten))
    check(
        "a rewritten message is flagged",
        rewritten_rows[0]["message_preserved"] is False and rewritten_rows[0]["explanation"] is None,
    )
    # An explanation appended after the trailer block (what the plan's instruction produces) keeps the identity.
    sh(repo, "checkout", "--quiet", "--detach", base)
    appended = commit(
        repo,
        "feat: improve A (#1)\n\nBody of one.\n\nSource-PR: TauCetiProject/TauCeti#1\nAccepted-Head: "
        + "a" * 40
        + "\n\nBatch repair: use `trivial`.\n",
        {"TauCeti/A.lean": "theorem a : True := trivial\n"},
    )
    appended_chain = rx.read_chain(repo, base, appended)
    check(
        "trailers followed by an appended explanation are still read",
        appended_chain[0].pr == 1 and appended_chain[0].accepted_head == "a" * 40,
    )
    appended_rows = rx.compare_chains(repo, chain[:1], appended_chain)
    check(
        "the explanation after the trailers is collected",
        appended_rows[0]["message_preserved"] and appended_rows[0]["explanation"] == "Batch repair: use `trivial`.",
    )
    check(
        "the chain tool reads the same identity",
        rx.chain_tool.validate(repo, base, appended, ["TauCetiProject/TauCeti#1"]).ok,
    )
    # A merge commit is not a chain.
    sh(repo, "checkout", "--quiet", "--detach", base)
    side = commit(repo, "side", {"TauCeti/C.lean": "theorem c : True := trivial\n"})
    sh(repo, "checkout", "--quiet", "--detach", chain[1].sha)
    sh(repo, "merge", "--quiet", "--no-ff", "-m", "merge", side, env=env)
    merged = sh(repo, "rev-parse", "HEAD")
    check(
        "a merge in the history is rejected", any("parents" in p for p in rx.validate_chain(repo, base, merged, [1, 2]))
    )

    # The prompt: the plan's instruction verbatim, the diagnostics with owners, the commits, the rules.
    prompt = rx.compose_prompt(by_id[1931], findings, chain, repo, base[:12])
    check("prompt opens with the plan's instruction verbatim", prompt.startswith(rx.AGENT_INSTRUCTION))
    check(
        "prompt says what happened and that `gh` is not needed",
        "## What happened" in prompt
        and "`gh` is not needed" in prompt
        and "do not introduce changes" in prompt.lower(),
    )
    check(
        "prompt lists constituent commits with PR identities",
        f"`{chain[0].sha[:12]}` #1:" in prompt and f"`{chain[1].sha[:12]}` #2:" in prompt,
    )
    check(
        "prompt carries the compiler error and its line",
        "Pullback.lean:101:5" in prompt and "Unknown identifier" in prompt,
    )
    check("prompt names Bors's suspect as a hint", "#26269" in prompt and "not a verdict" in prompt)
    check(
        "prompt explains verification without the lint script",
        "lake build --iofail" in prompt and "scripts/lint-env.sh" not in prompt and "maxHeartbeats" not in prompt,
    )
    check(
        "prompt states the aim before the hard rules",
        prompt.index("as its author intended") < prompt.index("no `sorry`") and "no `set_option`" in prompt,
    )
    check(
        "prompt gives one editing instruction with the owning commit",
        "(no commit of this batch modifies this file)" in prompt
        and "--squash=<sha>" in prompt
        and f"`{chain[0].sha[:12]}` #1: feat: improve A" in prompt
        and "Any method is fine" not in prompt,
    )
    check(
        "prompt explains that the result is read from the worktree",
        "nothing to push, publish or report anywhere else" in prompt
        and "Nothing you do here is published" not in prompt,
    )
    check("prompt says the artifacts are warm by default", "Artifacts are warm" in prompt)
    cold = rx.compose_prompt(
        by_id[1931],
        findings,
        chain,
        repo,
        base[:12],
        artifacts={"available": False, "unavailable_reason": "no published cache mapping for 0123456789ab"},
    )
    check(
        "a cold prompt says so, with the reason, and suggests the failing module first",
        "The build is cold" in cold
        and "no published cache mapping for 0123456789ab" in cold
        and "`lake build TauCeti.AlgebraicGeometry.Modules.PseudoCoherent.Pullback`" in cold
        and "Artifacts are warm" not in cold,
    )

# ---- chain construction through the chain tool, with the retained-chain fallback -----------------

with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    sh(repo, "config", "user.name", "t")
    sh(repo, "config", "user.email", "t@x")
    base = commit(repo, "base", {"TauCeti/A.lean": "theorem a : True := trivial\n"})
    sh(repo, "checkout", "--quiet", "-b", "pr1")
    head1 = commit(repo, "wip one", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
    sh(repo, "checkout", "--quiet", "--detach", base)
    head2 = commit(repo, "wip two", {"TauCeti/B.lean": "theorem b : True := trivial\n"})
    # Bors's retained chain: squash of #1 then #2, subjects carrying the PR number.
    sh(repo, "checkout", "--quiet", "--detach", base)
    retained1 = commit(repo, "feat: improve A (#1)\n\nOne.\n", {"TauCeti/A.lean": "theorem a : True := by trivial\n"})
    retained2 = commit(repo, "feat: add B (#2)\n\nTwo.\n", {"TauCeti/B.lean": "theorem b : True := trivial\n"})
    failure = rx.Failure(
        1,
        1,
        1,
        base,
        retained2,
        None,
        [rx.Member(1, head1, None, None, None), rx.Member(2, head2, None, None, None)],
        [],
        [],
        {},
        None,
        None,
        [],
        None,
        None,
    )
    tip, built_chain, outcome = rx.construct_chain(repo, failure, retained2)
    check(
        "the chain tool rebuilds the retained tree from the accepted heads",
        outcome["source"] == "chain.build"
        and outcome["tree_matches_retained"]
        and sh(repo, "rev-parse", f"{tip}^{{tree}}") == sh(repo, "rev-parse", f"{retained2}^{{tree}}"),
    )
    check(
        "chain tool commits carry the PR titles and trailers",
        built_chain[1].subject == "feat: add B (#2)"
        and built_chain[1].accepted_head == head2
        and built_chain[0].pr == 1,
    )
    check(
        "the chain tool's chain validates", outcome["validate_ok"] and rx.validate_chain(repo, base, tip, [1, 2]) == []
    )
    # A head that differs from what Bors merged: the tool's tree diverges and the retained chain is reworded instead.
    sh(repo, "checkout", "--quiet", "--detach", head2)
    head2b = commit(repo, "more two", {"TauCeti/B.lean": "theorem b : True := trivial\ntheorem b2 : True := trivial\n"})
    failure.members[1].head_sha = head2b
    tip2, chain2, outcome2 = rx.construct_chain(repo, failure, retained2)
    check(
        "a divergent tool chain falls back to the reworded retained chain",
        outcome2["source"] == "retained_reword"
        and outcome2["tree_matches_retained"] is False
        and outcome2["first_divergent_prs"] == [2],
    )
    check(
        "the fallback keeps the retained tree and the member heads",
        sh(repo, "rev-parse", f"{tip2}^{{tree}}") == sh(repo, "rev-parse", f"{retained2}^{{tree}}")
        and chain2[1].accepted_head == head2b,
    )
    check("the tree difference is recorded", "B.lean" in outcome2["tree_difference"])

# ---- the agent launch: a long prompt on stdin, a process that cannot start -----------------------

ECHO_STDIN = (
    "import json, sys; text = sys.stdin.read(); "
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': f'stdin {len(text)} chars',"
    " 'session_id': 's1', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))"
)
with tempfile.TemporaryDirectory() as raw:
    logs = Path(raw) / "logs"
    long_prompt = "y" * 200_000
    record = rx.run_agent(Path(raw), [sys.executable, "-c", ECHO_STDIN], dict(os.environ), logs, 60, long_prompt)
    check("run_agent delivers a stdin prompt and exits cleanly", (record["rc"], record["outcome"]) == (0, "exited"))
    check("run_agent records the prompt channel", record["prompt_via"] == "stdin")
    check(
        "the whole prompt reached the agent",
        f"stdin {len(long_prompt)} chars" in (logs / "agent-stream.jsonl").read_text(),
    )
    record = rx.run_agent(Path(raw), [sys.executable, "-c", ECHO_STDIN], dict(os.environ), logs, 60)
    check(
        "without a stdin prompt the agent reads an empty, closed stdin",
        "stdin 0 chars" in (logs / "agent-stream.jsonl").read_text(),
    )
    check("an argv prompt is recorded as such", record["prompt_via"] == "argv")
    record = rx.run_agent(Path(raw), [str(Path(raw) / "no-such-agent")], dict(os.environ), logs, 60)
    check(
        "a process that cannot start is agent_launch_failed, not an exception",
        record["outcome"] == "agent_launch_failed",
    )
    check("the launch error is named", record["rc"] is None and "FileNotFoundError" in record["launch_error"])
    check("the launch error is in the stderr log", "could not start" in (logs / "agent-stderr.log").read_text())


# ---- agent usage accounting from a stream-json transcript --------------------------------------

with tempfile.TemporaryDirectory() as raw:
    stream = Path(raw) / "agent-stream.jsonl"
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 50000,
        "output_tokens": 300,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 1000},
    }
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {
            "type": "assistant",
            "session_id": "s-1",
            "message": {
                "id": "m1",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "text", "text": "a"}],
            },
        },
        {
            "type": "assistant",
            "session_id": "s-1",
            "message": {
                "id": "m1",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "lake build"}}],
            },
        },
        {
            "type": "assistant",
            "session_id": "s-1",
            "message": {
                "id": "m2",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "text", "text": "done"}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "total_cost_usd": 0.42,
            "result": "summary",
            "session_id": "s-1",
            "usage": usage,
        },
    ]
    stream.write_text("".join(json.dumps(e) + "\n" for e in events))
    accounted = rx.agent_usage(stream)
    check(
        "usage counted once per message id",
        accounted["tokens"]["output_tokens"] == 600 and accounted["tokens"]["cache_read_input_tokens"] == 100000,
    )
    events[-1]["modelUsage"] = {
        "claude-opus-5-5": {
            "inputTokens": 44,
            "outputTokens": 8294,
            "cacheReadInputTokens": 1062044,
            "cacheCreationInputTokens": 50570,
            "cacheCreationEphemeral1hInputTokens": 50570,
            "costUSD": 0.78,
        }
    }
    stream.write_text("".join(json.dumps(e) + "\n" for e in events))
    reported = rx.agent_usage(stream)
    check(
        "the result event's per-model totals win over streamed usage",
        reported["tokens"]["output_tokens"] == 8294
        and reported["models"]["claude-opus-5-5"]["cache_creation_1h_input_tokens"] == 50570
        and reported["estimated_cost_usd"] > accounted["estimated_cost_usd"],
    )
    del events[-1]["modelUsage"]
    stream.write_text("".join(json.dumps(e) + "\n" for e in events))
    accounted = rx.agent_usage(stream)
    check(
        "model, turns and Claude's own cost are kept",
        accounted["model"] == "claude-opus-5-5"
        and accounted["num_turns"] == 2
        and accounted["claude_reported_cost_usd"] == 0.42,
    )
    check(
        "estimated cost is priced from the repository table",
        isinstance(accounted["estimated_cost_usd"], float) and accounted["estimated_cost_usd"] > 0,
    )
    check(
        "session id and final text are recorded",
        accounted["session_id"] == "s-1" and accounted["final_text"] == "summary",
    )
    check(
        "a missing stream yields zero usage", rx.agent_usage(Path(raw) / "missing.jsonl")["tokens"]["total_tokens"] == 0
    )

# ---- the textual duplicate-declaration check ------------------------------------------------------

with tempfile.TemporaryDirectory() as raw:
    wt = Path(raw) / "wt"
    (wt / "TauCeti" / "A").mkdir(parents=True)
    (wt / "TauCeti" / "A" / "Menger.lean").write_text("import X\n\ntheorem other : True := trivial\n")
    (wt / "TauCeti" / "A" / "Paths.lean").write_text(
        "namespace SimpleGraph.Walk\n\nlemma IsPath.start_notMem_tail_support (p : W) : True := trivial\n"
    )
    exp = Path(raw) / "exp"
    exp.mkdir()
    (exp / "ci-findings.json").write_text(
        json.dumps(
            {
                "duplicate_declarations": [
                    {
                        "declaration": "SimpleGraph.Walk.IsPath.start_notMem_tail_support",
                        "modules": ["TauCeti.A.Menger", "TauCeti.A.Paths"],
                    }
                ],
                "import_collisions": [],
            }
        )
    )
    dup = rx.duplicate_declaration_check(wt, exp)
    check(
        "a dotted declaration head is counted in its remaining module",
        dup["ok"] and dup["rows"][0]["declared_in"] == ["TauCeti.A.Paths"],
    )
    (wt / "TauCeti" / "A" / "Menger.lean").write_text("theorem IsPath.start_notMem_tail_support : True := trivial\n")
    check("a name still declared on both sides fails the check", not rx.duplicate_declaration_check(wt, exp)["ok"])

# ---- pool pruning --------------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    pool, shared = root / "lake-cache", root / "shared"
    (pool / "artifacts").mkdir(parents=True)
    (pool / "revisions" / "TauCeti").mkdir(parents=True)
    (pool / "outputs" / "TauCeti").mkdir(parents=True)
    (shared / "artifacts").mkdir(parents=True)
    (pool / "artifacts" / "base.ltar").write_bytes(b"b" * 10)
    (shared / "artifacts" / "seed.ltar").write_bytes(b"s" * 10)
    os.link(shared / "artifacts" / "seed.ltar", pool / "artifacts" / "seed.ltar")
    (pool / "artifacts" / "built.ltar").write_bytes(b"x" * 1000)
    (pool / "revisions" / "TauCeti" / "abc.jsonl").write_text('"2026-03-17"\n["0011", "base.ltar"]\n')
    (pool / "outputs" / "TauCeti" / "0011.json").write_text(json.dumps({"data": "base.ltar"}))
    (pool / "outputs" / "TauCeti" / "0022.json").write_text(json.dumps({"data": "built.ltar"}))
    (pool / "artifacts" / "fresh.ltar").write_bytes(b"f" * 10)
    protected = rx.prune_pool(root, shared, newer_than=os.path.getmtime(pool / "artifacts" / "fresh.ltar"))
    check(
        "a build in progress keeps its fresh artifacts",
        protected["removed"] == 1 and (pool / "artifacts" / "fresh.ltar").exists(),
    )
    pruned = rx.prune_pool(root, shared, drop_revisions=("abc",))
    check(
        "dropping a finished base prunes its artifacts too",
        pruned["revisions_dropped"] == 1 and sorted(p.name for p in (pool / "artifacts").iterdir()) == ["seed.ltar"],
    )
    check("pruning removes stale output entries", not (pool / "outputs" / "TauCeti" / "0022.json").exists())

# ---- results table and summary ----------------------------------------------------------------

record = {
    "candidate": by_id[1931].to_json(),
    "status": "complete",
    "started_at": "2026-11-04T05:00:00+00:00",
    "finished_at": "2026-11-04T06:00:00+00:00",
    "host_start": {"disk_free_gb": 40.0, "load_1m": 100.0},
    "sources": {"chain_tree_matches_retained": True, "ci_findings": findings},
    "artifacts": {
        "seconds": 1500.0,
        "base_cache": {"seconds": 30.0, "mapping_found": True},
        "candidate_outputs_published": {"published": False},
        "incremental_build": {
            "seconds": 1400.0,
            "rc": 1,
            "built": 1189,
            "replayed": 0,
            "fetched": 22000,
            "failed_modules": ["TauCeti.X"],
            "load_before": {"load_1m": 110.0},
        },
    },
    "agent": {
        "wall_seconds": 1300.0,
        "rc": 0,
        "timed_out": False,
        "num_turns": 40,
        "model": "claude-opus-5-5",
        "tokens": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 10,
        },
        "estimated_cost_usd": 3.5,
        "claude_reported_cost_usd": 3.4,
        "load_before": {"load_1m": 105.0},
        "load_after": {"load_1m": 95.0},
    },
    "result": {
        "green": True,
        "confirm_build": {"seconds": 400.0, "built": 12, "failed_modules": []},
        "edited_prs": [26269],
        "shrink_prs": [],
        "emptied_prs": [],
        "messages_preserved": True,
        "edited_without_explanation": [],
        "validation_problems": [],
        "diffstat": "1 file changed",
    },
}
row = rx.result_row(record)
check(
    "result row flattens the record",
    row["candidate"] == 1931
    and row["green"] is True
    and row["edited_prs"] == [26269]
    and row["preparation"]["modules_built"] == 1189,
)
check("time to green adds agent and confirming build", row["time_to_green_seconds"] == 1700.0)
table = rx.results_table([row])
check(
    "results table renders the row",
    "| 1931 |" in table and "| yes |" in table and "#26269" in table and "1189 built" in table,
)
summary = rx.summary([row, {**row, "status": "error", "green": None}])
check(
    "summary counts completes, greens and costs",
    summary["complete"] == 1
    and summary["green"] == 1
    and summary["estimated_cost_usd_total"] == 3.5
    and summary["time_to_green_minutes"]["median"] == 28.3,
)
check("distribution of an empty list", rx.distribution([]) == {"count": 0})
with tempfile.TemporaryDirectory() as raw:
    doc = Path(raw) / "doc.md"
    doc.write_text("# Title\n\n## Results\n\n<!-- results-table -->\nold\n<!-- /results-table -->\n\n## After\n")
    rx.update_document(doc, [row], summary)
    text = doc.read_text()
    check(
        "the document section is rewritten between its markers",
        "old" not in text
        and "| 1931 |" in text
        and text.endswith("<!-- /results-table -->\n\n## After\n")
        and "median 28.3" in text,
    )
check("host state has load and disk", {"load_1m", "disk_free_gb", "cpus"} <= set(rx.host_state()))

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
