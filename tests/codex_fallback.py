#!/usr/bin/env python3
"""Default Codex authoring probes Sol safely, caches access, and runs no authoring prompt itself."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

SOL = "gpt-6.1-sol"
LUNA = "gpt-6-luna"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def unavailable(status=400, message=None):
    message = message or f"The '{SOL}' model is not supported when using Codex with a ChatGPT account."
    payload = {"status": status, "error": {"type": "invalid_request_error", "message": message}}
    return json.dumps({"type": "turn.failed", "error": {"message": json.dumps(payload)}})


OK = SimpleNamespace(returncode=0, stdout='{"type":"turn.completed","usage":{}}\n', stderr="")
UNAVAILABLE = SimpleNamespace(returncode=1, stdout=unavailable() + "\n", stderr="")
ORDINARY = SimpleNamespace(
    returncode=1,
    stdout=json.dumps({"type": "turn.failed", "error": {"message": "ordinary agent failure"}}) + "\n",
    stderr="",
)
CONTEXT = SimpleNamespace(
    returncode=1,
    stdout=unavailable(400, "This request exceeds the context window.") + "\n",
    stderr="",
)
TRANSIENT = SimpleNamespace(returncode=1, stdout=unavailable(500) + "\n", stderr="")
# An older Codex CLI that lacks metadata for the model warns, then the request fails by error code.
METADATA_MISS = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "thread-test"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "error", "message": f"Model metadata for `{SOL}` not found."},
            }
        ),
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": json.dumps(
                        {
                            "type": "error",
                            "status": 400,
                            "error": {
                                "type": "invalid_request_error",
                                "code": "model_not_found",
                                "message": f"The requested model '{SOL}' does not exist.",
                            },
                        }
                    )
                },
            }
        ),
    ]
)
RAW_FALSE_POSITIVE = SimpleNamespace(returncode=1, stdout="error: model not found\n", stderr="")


def make_cfg(root, *, account=True):
    home = root / "home"
    (home / ".codex").mkdir(parents=True)
    if account:
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {"account_id": "acct-test", "access_token": "secret"}})
        )
    return SimpleNamespace(home=home, quota_cache=root / "cache", state=root / "state")


def make_profile(explicit=False):
    return tc.AuthoringProfile(
        provider="codex",
        model=SOL,
        effort="high",
        model_source="--author-model" if explicit else "repository default",
        effort_source="repository default",
        fallback_model=None if explicit else LUNA,
    )


def run(sequence, *, repeat=False, explicit=False):
    with tempfile.TemporaryDirectory() as td:
        cfg = make_cfg(Path(td))
        profile = make_profile(explicit)
        calls = []
        outcomes = list(sequence)
        saved_run = tc.agents.run_process
        saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "must-not-reach-subscription-probe"

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return outcomes.pop(0)

        tc.agents.run_process = fake_run
        try:
            try:
                selected = tc.resolve_codex_model_access(cfg, profile)
                error = None
            except tc.NoProgress as exc:
                selected, error = None, str(exc)
            if repeat and selected is not None:
                selected_again = tc.resolve_codex_model_access(cfg, profile)
            else:
                selected_again = None
        finally:
            tc.agents.run_process = saved_run
            if saved_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = saved_key
        return selected, selected_again, error, calls, outcomes


selected, again, error, calls, remaining = run([OK], repeat=True)
check("successful Sol probe selects Sol", (selected.model, error), (SOL, None))
check("successful Sol access is cached", (again.model, len(calls), len(remaining)), (SOL, 1, 0))
argv, kwargs = calls[0]
check("probe uses the requested Sol model", argv[argv.index("--model") + 1], SOL)
check("probe is JSON + read-only + ephemeral", all(x in argv for x in ("--json", "read-only", "--ephemeral")), True)
check("probe ignores user config and rules", all(x in argv for x in ("--ignore-user-config", "--ignore-rules")), True)
check("probe prompt is trivial, not an authoring prompt", argv[-1], "Reply with exactly OK. Do not use tools.")
check("probe closes stdin", kwargs.get("stdin"), subprocess.DEVNULL)
check("probe strips API-key billing", "OPENAI_API_KEY" in kwargs.get("env", {}), False)

selected, again, error, calls, remaining = run([UNAVAILABLE, UNAVAILABLE], repeat=True)
check("two confirmed entitlement misses select Luna", (selected.model, error), (LUNA, None))
check("Luna decision is cached without a third request", (again.model, len(calls), len(remaining)), (LUNA, 2, 0))
check("both confirmations probe Sol only", [c[0][c[0].index("--model") + 1] for c in calls], [SOL, SOL])

selected, _, error, calls, _ = run([UNAVAILABLE, OK])
check("a successful confirmation keeps Sol", (selected.model, len(calls), error), (SOL, 2, None))

for name, outcome in (
    ("ordinary failure", ORDINARY),
    ("context 400", CONTEXT),
    ("model-looking 5xx", TRANSIENT),
    ("raw transcript false positive", RAW_FALSE_POSITIVE),
):
    selected, _, error, calls, _ = run([outcome])
    check(f"{name} does not downgrade", (selected, len(calls), error is not None), (None, 1, True))

selected, _, error, calls, remaining = run([], explicit=True)
check("explicit model bypasses probe and fallback", (selected.model, len(calls), error), (SOL, 0, None))

check(
    "model_not_found code after a metadata miss confirms unavailability",
    tc.agents._codex_model_unavailable(1, METADATA_MISS),
    True,
)


def round_outcome(transcript, *, rc=1, account=True, selected_model=SOL):
    """Feed a round transcript through the renderer, note it, then resolve the next round's model."""
    renderer = tc.agents.AgentTranscriptRenderer("codex")
    for line in transcript.splitlines():
        renderer.render_line(line + "\n")
    with tempfile.TemporaryDirectory() as td:
        cfg = make_cfg(Path(td), account=account)
        profile = make_profile()
        tc.agents._LAST_AGENT_RUN = {
            "provider": "codex",
            "rc": rc,
            "saw_work": renderer.saw_work,
            "terminal_failure": renderer.terminal_failure_text,
        }
        calls = []
        saved_run, saved_warn = tc.agents.run_process, tc.agents.warn_red
        tc.agents.run_process = lambda argv, **kwargs: calls.append(argv) or OK
        tc.agents.warn_red = lambda msg: calls.append(("warn", msg))
        try:
            tc.agents.note_codex_round_rejection(
                cfg, profile, tc.agents.replace(profile, model=selected_model, fallback_model=None)
            )
            following = tc.resolve_codex_model_access(cfg, profile)
        finally:
            tc.agents.run_process, tc.agents.warn_red = saved_run, saved_warn
        probes = sum(1 for call in calls if call[0] != "warn")
        warned = any(call[0] == "warn" for call in calls)
        return following.model, probes, warned, tc.agents._LAST_AGENT_RUN


following, probes, warned, record = round_outcome(METADATA_MISS)
check("a round's model rejection makes the next round take Luna", (following, probes, warned), (LUNA, 0, True))
check("the round record is consumed", record, None)

worked = METADATA_MISS.replace(
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "turn.started"})
    + "\n"
    + json.dumps({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "working"}}),
)
for name, outcome in (
    ("ordinary round failure", round_outcome(ORDINARY.stdout)),
    ("context-window round failure", round_outcome(CONTEXT.stdout)),
    ("model-looking 5xx round failure", round_outcome(TRANSIENT.stdout)),
    ("rejection after the agent worked", round_outcome(worked)),
):
    check(f"{name} is not cached as a miss", outcome[:3], (SOL, 1, False))

check(
    "a rejected fallback is reported, not cached", round_outcome(METADATA_MISS, selected_model=LUNA)[:3], (SOL, 1, True)
)
check("no account fingerprint: reported, not cached", round_outcome(METADATA_MISS, account=False)[2], True)


def consecutive_dispatches(*, decline_first):
    """One run_round's opts across a declined progress candidate and a roadmap round the model rejects."""
    from unittest.mock import patch

    wu = tc.work_units
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = make_cfg(root)
        cfg.wid, cfg.checkout, cfg.logdir = "test", root / "checkout", root / "logs"
        w = SimpleNamespace(cfg=cfg, counters=SimpleNamespace(read=lambda key: 0, write=lambda *a: None))
        opts = tc.RoundOpts(only=[], agent="codex", work_model="codex", sandbox_host=True, dry_run=False)
        renderer = tc.agents.AgentTranscriptRenderer("codex")
        for line in METADATA_MISS.splitlines():
            renderer.render_line(line + "\n")

        def rejected_round(*args):
            tc.agents._LAST_AGENT_RUN = {
                "provider": "codex",
                "rc": 1,
                "saw_work": renderer.saw_work,
                "terminal_failure": renderer.terminal_failure_text,
            }
            return 1

        with (
            patch.object(tc.agents, "run_process", return_value=OK),
            patch.object(tc.agents, "warn_red"),
            patch.object(wu, "do_progress", return_value=None),
            patch.object(wu, "do_roadmap", side_effect=rejected_round),
            patch.object(wu, "_bubble", return_value=False),
            patch.object(wu.shutil, "which", return_value="/usr/bin/codex"),
            patch.object(wu, "_still_actionable", return_value=True),
            patch.object(wu, "_check_stage_read_quota"),
            patch.object(wu, "_progress_snapshot", return_value=None),
            patch.object(wu, "_checkout_head", return_value=None),
            patch.object(wu, "log_round_file_changes"),
            patch.object(wu, "report_runtime"),
            patch("tauceti_worker.activity.select"),
            patch("tauceti_worker.claude_api.api_mode", return_value=False),
        ):
            if decline_first:
                wu.dispatch("progress", w, SimpleNamespace(open_prs=[]), tc.Candidate(0, "", "report"), opts)
            try:
                wu.dispatch("roadmap", w, SimpleNamespace(open_prs=[]), tc.Candidate(0, "", "any"), opts)
            except tc.NoProgress:
                pass
            following = tc.resolve_codex_model_access(cfg, make_profile())
        return following.model


check("a rejected round records its miss", consecutive_dispatches(decline_first=False), LUNA)
check("a rejected round after a declined candidate records its miss", consecutive_dispatches(decline_first=True), LUNA)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
