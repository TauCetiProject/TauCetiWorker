#!/usr/bin/env python3
"""Authoring profiles are explicit, provider-specific, and backend-independent."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


keys = [
    "TAUCETI_AUTHORING_CODEX_MODEL",
    "TAUCETI_AUTHORING_CODEX_EFFORT",
    "TAUCETI_AUTHORING_CLAUDE_MODEL",
    "TAUCETI_AUTHORING_CLAUDE_EFFORT",
    "TAUCETI_AUTHORING_KIRO_MODEL",
    "TAUCETI_AUTHORING_KIRO_EFFORT",
    "TAUCETI_CODEX_MODEL",
    "TAUCETI_AUTHORING_DEEPSEEK_EFFORT",
]
saved_env = {k: os.environ.get(k) for k in keys}
for key in keys:
    os.environ.pop(key, None)

try:
    codex = tc.resolve_authoring_profile("codex")
    claude = tc.resolve_authoring_profile("claude")
    kiro = tc.resolve_authoring_profile("kiro")
    check("committed Codex default", (codex.model, codex.effort), ("gpt-6-sol", "medium"))
    check("committed Codex fallback", codex.fallback_model, "gpt-6-luna")
    check("committed Claude default is exact", (claude.model, claude.effort), ("claude-opus-5-5", "medium"))
    check("committed Kiro default is exact Sol", (kiro.model, kiro.effort), ("gpt-5.6-sol", "high"))
    default_host, _ = tc.host_agent_argv("PROMPT", codex)
    default_bubble = tc.agent_inner_cmd(codex)
    default_claude_host, _ = tc.host_agent_argv("PROMPT", claude)
    default_claude_bubble = tc.agent_inner_cmd(claude)
    check("default Codex host launch is direct", default_host[:4], ["codex", "exec", "--json", "--model"])
    check("default Codex host launch prefers Sol", "gpt-6-sol" in default_host, True)
    check("default Codex host launch carries one model", "gpt-6-luna" in default_host, False)
    check("default Codex bubble launch is direct", "codex exec" in default_bubble, True)
    check("default Codex bubble launch prefers Sol", "--model gpt-6-sol" in default_bubble, True)
    check("default Codex bubble launch carries one model", "gpt-6-luna" in default_bubble, False)
    check(
        "Codex host requests detailed reasoning summaries", 'model_reasoning_summary="detailed"' in default_host, True
    )
    check("Codex bubble requests detailed reasoning summaries", "model_reasoning_summary" in default_bubble, True)
    check("Codex host disables raw reasoning", "show_raw_agent_reasoning=false" in default_host, True)
    check("Codex bubble disables raw reasoning", "show_raw_agent_reasoning=false" in default_bubble, True)
    check(
        "Claude host requests verbose JSONL",
        all(flag in default_claude_host for flag in ("--output-format", "stream-json", "--verbose")),
        True,
    )
    check(
        "Claude bubble requests verbose JSONL",
        "--output-format stream-json --verbose" in default_claude_bubble,
        True,
    )
    check("Claude host avoids partial token events", "--include-partial-messages" in default_claude_host, False)

    kiro_host, _ = tc.host_agent_argv("PROMPT", kiro)
    kiro_bubble = tc.agent_inner_cmd(kiro)
    check("Kiro host pins model", kiro_host[kiro_host.index("--model") + 1], "gpt-5.6-sol")
    check("Kiro host never invokes Auto", "auto" in [arg.lower() for arg in kiro_host], False)
    check("Kiro bubble pins model", "--model gpt-5.6-sol" in kiro_bubble, True)
    check("Kiro bubble never invokes Auto", "--model auto" in kiro_bubble.lower(), False)

    os.environ["TAUCETI_AUTHORING_KIRO_MODEL"] = "claude-opus-5"
    os.environ["TAUCETI_AUTHORING_KIRO_EFFORT"] = "high"
    kiro_opus = tc.resolve_authoring_profile("kiro")
    check("Kiro can explicitly select Opus", kiro_opus.model, "claude-opus-5")
    check("Kiro Opus selection reaches host", "claude-opus-5" in tc.host_agent_argv("P", kiro_opus)[0], True)
    check("Kiro Opus selection reaches Bubble", "--model claude-opus-5" in tc.agent_inner_cmd(kiro_opus), True)

    for provider, retired in (("claude", "claude-opus-4-8"), ("kiro", "claude-opus-4.8")):
        try:
            tc.resolve_authoring_profile(provider, cli_model=retired)
        except tc.Die as exc:
            check(f"{provider}: retired Opus is rejected", "claude-opus-5" in str(exc), True)
        else:
            check(f"{provider}: retired Opus is rejected", False, True)
    os.environ.pop("TAUCETI_AUTHORING_KIRO_MODEL")
    os.environ.pop("TAUCETI_AUTHORING_KIRO_EFFORT")

    for source, kwargs in (
        ("CLI", {"cli_model": "Auto"}),
        ("environment", {}),
    ):
        if source == "environment":
            os.environ["TAUCETI_AUTHORING_KIRO_MODEL"] = "auto-premium"
        try:
            tc.resolve_authoring_profile("kiro", **kwargs)
            kiro_auto_rejected = False
        except tc.Die:
            kiro_auto_rejected = True
        check(f"Kiro {source} cannot select Auto", kiro_auto_rejected, True)
        os.environ.pop("TAUCETI_AUTHORING_KIRO_MODEL", None)

    os.environ["TAUCETI_AUTHORING_CODEX_MODEL"] = "env-model"
    os.environ["TAUCETI_AUTHORING_CODEX_EFFORT"] = "medium"
    env_profile = tc.resolve_authoring_profile("codex")
    check("provider environment overrides defaults", (env_profile.model, env_profile.effort), ("env-model", "medium"))
    check(
        "environment sources are visible",
        (env_profile.model_source, env_profile.effort_source),
        ("$TAUCETI_AUTHORING_CODEX_MODEL", "$TAUCETI_AUTHORING_CODEX_EFFORT"),
    )

    cli_profile = tc.resolve_authoring_profile("codex", cli_model="cli-model", cli_effort="xhigh")
    check("CLI overrides environment", (cli_profile.model, cli_profile.effort), ("cli-model", "xhigh"))
    check("explicit model disables automatic fallback", cli_profile.fallback_model, None)

    os.environ.pop("TAUCETI_AUTHORING_CODEX_MODEL")
    os.environ["TAUCETI_CODEX_MODEL"] = "legacy-author"
    legacy = tc.resolve_authoring_profile("codex")
    check("legacy variable remains authoring fallback", legacy.model, "legacy-author")
    check("legacy variable does not pin review", tc._codex_review_model_override("codex"), None)
    check("legacy authoring model disables automatic fallback", legacy.fallback_model, None)
    os.environ.pop("TAUCETI_CODEX_MODEL")
    os.environ.pop("TAUCETI_AUTHORING_CODEX_EFFORT")

    try:
        tc.resolve_authoring_profile("codex", cli_effort='high"\nmodel="surprise')
        unsafe_rejected = False
    except tc.Die:
        unsafe_rejected = True
    check("effort is safe to forward as a Codex config value", unsafe_rejected, True)

    os.environ["TAUCETI_AUTHORING_DEEPSEEK_EFFORT"] = "high"
    try:
        tc.resolve_authoring_profile("deepseek")
        openrouter_effort_rejected = False
    except tc.Die:
        openrouter_effort_rejected = True
    check("OpenRouter effort env fails clearly instead of poisoning loop children", openrouter_effort_rejected, True)

    try:
        tc.cmd_work(
            SimpleNamespace(host=False, author_model="provider-specific", author_effort=None),
            only=[],
            agent="auto",
            one_round=False,
        )
        auto_rejected = False
    except tc.Die:
        auto_rejected = True
    check("generic override with auto provider is rejected", auto_rejected, True)

    # Both launchers consume the same already-resolved object. Neither can
    # inspect HOME or pick another model/effort.
    host, _ = tc.host_agent_argv("PROMPT", cli_profile)
    bubble = tc.agent_inner_cmd(cli_profile)
    check("host carries exact model", host[host.index("--model") + 1], "cli-model")
    check("host carries exact effort", 'model_reasoning_effort="xhigh"' in host, True)
    check("explicit host model remains direct", host[0], "codex")
    check("bubble carries exact model", "--model cli-model" in bubble, True)
    check("bubble carries exact effort", "model_reasoning_effort" in bubble and "xhigh" in bubble, True)
    check("explicit bubble model remains direct", "codex-author" in bubble, False)

    # Loop parent pins the exact resolved profile into its isolated _round child.
    captured = []
    saved_choose = tc.loop.choose_model
    saved_budget = tc.loop.github_budget
    saved_round = tc.loop.run_round_subprocess
    tc.loop.choose_model = lambda *_a, **_k: ("claude", {})
    tc.loop.github_budget = lambda: {}

    def capture(tail):
        captured.extend(tail)
        raise KeyboardInterrupt

    tc.loop.run_round_subprocess = capture
    try:
        args = SimpleNamespace(
            ignore_quota=False,
            bubble=False,
            quota_cmd=None,
            source=None,
            author_model="claude-custom",
            author_effort="max",
        )
        tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
    finally:
        tc.loop.choose_model = saved_choose
        tc.loop.github_budget = saved_budget
        tc.loop.run_round_subprocess = saved_round

    check(
        "loop child receives exact profile",
        captured[captured.index("--author-model") : captured.index("--author-model") + 4],
        ["--author-model", "claude-custom", "--author-effort", "max"],
    )

    # The default Codex profile remains fallback-eligible after the loop parent pins Sol into its child.
    captured.clear()
    tc.loop.choose_model = lambda *_a, **_k: ("codex", {})
    tc.loop.github_budget = lambda: {}
    tc.loop.run_round_subprocess = capture
    try:
        args = SimpleNamespace(
            ignore_quota=False,
            bubble=False,
            quota_cmd=None,
            source=None,
            author_model=None,
            author_effort=None,
        )
        tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="codex")
    finally:
        tc.loop.choose_model = saved_choose
        tc.loop.github_budget = saved_budget
        tc.loop.run_round_subprocess = saved_round
    check(
        "loop child retains default Codex fallback provenance",
        captured[captured.index("--resolved-author-fallback-model") - 2 :],
        ["--author-effort", "medium", "--resolved-author-fallback-model", "gpt-6-luna"],
    )
    child_profile = tc.resolve_authoring_profile("codex", cli_model="gpt-6-sol", resolved_fallback_model="gpt-6-luna")
    check("loop child restores fallback eligibility", child_profile.fallback_model, "gpt-6-luna")
finally:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
