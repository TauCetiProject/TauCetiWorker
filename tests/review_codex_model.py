#!/usr/bin/env python3
"""Review model defaults and overrides reach host and Bubble launches.

Only $TAUCETI_REVIEW_CODEX_MODEL is forwarded to the engine; authoring model
overrides must leave its default/fallback policy untouched.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents, work_units  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    if not cond:
        fails += 1


# --- the pure decision helper -----------------------------------------------------------------------
claude = agents._claude_review_model
os.environ.pop("TAUCETI_CLAUDE_MODEL", None)
check("Claude review default is exact Opus 5.5", claude("claude") == "claude-opus-5-5")
check("mixed review pins Claude too", claude("codex, claude") == "claude-opus-5-5")
check("explicit Sonnet is not overridden", claude("sonnet") is None)
check("non-Claude review gets no Claude model", claude("codex") is None)
os.environ["TAUCETI_AUTHORING_CLAUDE_MODEL"] = "author-only"
check("Claude authoring override stays independent", claude("claude") == "claude-opus-5-5")
os.environ["TAUCETI_CLAUDE_MODEL"] = "   "
check("blank Claude override falls back to Opus 5.5", claude("claude") == "claude-opus-5-5")
os.environ["TAUCETI_CLAUDE_MODEL"] = "  claude-fable-5-1  "
check("explicit Claude review override is trimmed", claude("claude") == "claude-fable-5-1")
os.environ.pop("TAUCETI_CLAUDE_MODEL", None)

f = agents._codex_review_model_override
os.environ.pop("TAUCETI_REVIEW_CODEX_MODEL", None)
check("unset -> None", f("codex") is None)
os.environ["TAUCETI_AUTHORING_CODEX_MODEL"] = "author-only"
check("authoring override does not affect review", f("codex") is None)
os.environ["TAUCETI_REVIEW_CODEX_MODEL"] = "gpt-5.6-terra"
check("set + codex -> value", f("codex") == "gpt-5.6-terra")
check("set + claude -> None (not a codex reviewer)", f("claude") is None)
check("set + 'claude,codex' -> value", f("claude,codex") == "gpt-5.6-terra")

k = agents._kiro_review_model
os.environ.pop("TAUCETI_REVIEW_KIRO_MODEL", None)
os.environ["TAUCETI_AUTHORING_KIRO_MODEL"] = "claude-opus-5"
check("Kiro review defaults to exact Sol", k("kiro") == "gpt-5.6-sol")
check("Kiro authoring override does not affect review", k("kiro") != "claude-opus-5")
check("non-Kiro review gets no Kiro model", k("codex") is None)
os.environ["TAUCETI_REVIEW_KIRO_MODEL"] = "claude-opus-5"
check("Kiro review can explicitly select Opus", k("claude,kiro") == "claude-opus-5")

# --- end-to-end: the flag threads into the real review_in_bubble inner command ----------------------
captured = {}


def fake_run_in_bubble(w, target, prompt, opts, mounts=None, inner_cmd=None, cred_model=None):
    captured["inner"] = inner_cmd
    captured["cred"] = cred_model
    return 0


agents.run_in_bubble = fake_run_in_bubble
agents.fetch_ref = lambda repo, d: True  # no network
agents.me = lambda: "tester"  # no gh call

tmp = Path(tempfile.mkdtemp())
os.environ["TAUCETI_REVIEW_ENGINE_DIR"] = str(tmp / "engine")  # skip the engine fetch
w = types.SimpleNamespace(cfg=types.SimpleNamespace(state=tmp / "state", store_dir=tmp / "store"))
opts = types.SimpleNamespace()

os.environ.pop("TAUCETI_REVIEW_CODEX_MODEL", None)
agents.review_in_bubble(w, 470, "abc123", "codex", opts)
check("bubble: unset -> no --codex-model, engine default stands", "--codex-model" not in captured["inner"])
check("bubble: codex reviewer still seeds codex creds", captured["cred"] == "codex")

os.environ["TAUCETI_REVIEW_CODEX_MODEL"] = "gpt-5.6-terra"
agents.review_in_bubble(w, 470, "abc123", "codex", opts)
check("bubble: set -> --codex-model gpt-5.6-terra forwarded", "--codex-model gpt-5.6-terra" in captured["inner"])

agents.review_in_bubble(w, 470, "abc123", "claude", opts)
check("bubble: claude reviewer -> no codex flag even when set", "--codex-model" not in captured["inner"])
check("bubble: default Claude model is pinned", "--claude-model claude-opus-5-5" in captured["inner"])
os.environ["TAUCETI_CLAUDE_MODEL"] = "claude-fable-5-1"
agents.review_in_bubble(w, 470, "abc123", "claude", opts)
check("bubble: explicit Claude override is forwarded", "--claude-model claude-fable-5-1" in captured["inner"])
os.environ.pop("TAUCETI_CLAUDE_MODEL", None)

agents.review_in_bubble(w, 470, "abc123", "kiro", opts)
check("bubble: Kiro exact model is forwarded", "--kiro-model claude-opus-5" in captured["inner"])
check("bubble: Kiro reviewer seeds only Kiro creds", captured["cred"] == "kiro")
check("bubble: Kiro credential bootstrap is present", "kiro-auth.sqlite3" in captured["inner"])

# --- host: exercise the actual do_review command construction without a provider/network ----------
host_commands = []
work_units.run_to_logfile = lambda argv, *_: host_commands.append(argv) or 0
work_units._sync_review_outbox = lambda *_: 0
work_units.clear_review_failure = lambda *_: None
work_units.me = lambda: "tester"
w.cfg.logdir = tmp
w.rs = types.SimpleNamespace(review_rounds=lambda *_: 0, bust=lambda *_: None)
w.counters = types.SimpleNamespace(write=lambda *_: None)
c = types.SimpleNamespace(pr=470, head="abc123", contest=None, contest_reply_id=None)
for configured, provider, expected in (
    ("", "claude", "claude-opus-5-5"),
    ("claude-fable-5-1", "claude", "claude-fable-5-1"),
    ("claude-fable-5-1", "codex", None),
):
    os.environ["TAUCETI_CLAUDE_MODEL"] = configured
    work_units.do_review(w, None, c, types.SimpleNamespace(work_model=provider), False)
    argv = host_commands[-1]
    actual = argv[argv.index("--claude-model") + 1] if "--claude-model" in argv else None
    check(f"host: {provider} override {configured!r} -> {expected!r}", actual == expected)

os.environ.pop("TAUCETI_CLAUDE_MODEL", None)
os.environ.pop("TAUCETI_AUTHORING_CLAUDE_MODEL", None)
os.environ.pop("TAUCETI_REVIEW_ENGINE_DIR", None)
os.environ.pop("TAUCETI_REVIEW_CODEX_MODEL", None)
os.environ.pop("TAUCETI_AUTHORING_CODEX_MODEL", None)
os.environ.pop("TAUCETI_REVIEW_KIRO_MODEL", None)
os.environ.pop("TAUCETI_AUTHORING_KIRO_MODEL", None)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
