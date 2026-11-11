#!/usr/bin/env python3
"""--extra-roadmap-prompt / extra_roadmap_prompt: validation, env propagation, prompt rendering, and worker specs,
without GitHub or model access."""

import os
import sys
import tempfile
import textwrap
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import worker_manager as wm

fails = 0
GUIDANCE = "Work on the material relevant to Higher Topos Theory"


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def rejects(name, fn, needle):
    try:
        fn()
    except (ValueError, wm.WorkersError, SystemExit) as e:
        check(name, needle in str(e))
    else:
        check(name, False)


saved = {
    k: os.environ.get(k) for k in ("TAUCETI_EXTRA_ROADMAP_PROMPT", "TAUCETI_ROADMAP_SKIP", "TAUCETI_RESPECT_CLAIMS")
}
try:
    # ---- the validator and the live accessor -------------------------------------------------
    check("validator strips surrounding whitespace", tc.validate_extra_roadmap_prompt(f"  {GUIDANCE}\n") == GUIDANCE)
    rejects("blank guidance is rejected", lambda: tc.validate_extra_roadmap_prompt("   \n"), "must not be empty")
    rejects("NUL bytes are rejected", lambda: tc.validate_extra_roadmap_prompt("a\0b"), "NUL byte")
    rejects(
        "oversized guidance is rejected",
        lambda: tc.validate_extra_roadmap_prompt("x" * (tc.EXTRA_ROADMAP_PROMPT_MAX_CHARS + 1)),
        f"the limit is {tc.EXTRA_ROADMAP_PROMPT_MAX_CHARS}",
    )
    os.environ.pop("TAUCETI_EXTRA_ROADMAP_PROMPT", None)
    check("unset env → no guidance", tc.extra_roadmap_prompt() is None)
    os.environ["TAUCETI_EXTRA_ROADMAP_PROMPT"] = "  \n"
    check("blank env → no guidance", tc.extra_roadmap_prompt() is None)
    os.environ["TAUCETI_EXTRA_ROADMAP_PROMPT"] = f" {GUIDANCE} "
    check("env value is read live and stripped", tc.extra_roadmap_prompt() == GUIDANCE)

    # ---- the flag is accepted by `work` and by the loop child -----------------------------------
    parsed = tc.build_parser().parse_args(["work", "--only", "roadmap", "--extra-roadmap-prompt", GUIDANCE])
    check("work parser accepts --extra-roadmap-prompt", parsed.extra_roadmap_prompt == GUIDANCE)
    check(
        "loop-child parser accepts --extra-roadmap-prompt",
        tc.build_parser().parse_args(["_round", "--extra-roadmap-prompt", "x"]).extra_roadmap_prompt == "x",
    )
    check(
        "flag defaults to None so the env is not clobbered",
        tc.build_parser().parse_args(["work"]).extra_roadmap_prompt is None,
    )

    # ---- do_roadmap renders the guidance as its own section ---------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="extra-roadmap-prompt-"))
    os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"  # no intentions-board call
    os.environ["TAUCETI_ROADMAP_SKIP"] = "Algebra"
    tc.work_units.ensure_fork = lambda: "alice/TauCeti"
    tc.work_units.avoid_list = lambda *_args: "none"
    tc.work_units.fetch_ref = lambda repo, dest: Path(dest).mkdir(parents=True, exist_ok=True) or True
    tc.work_units.prepare_checkout = lambda cfg, **_: True
    cap = {}
    tc.work_units.run_agent_host = lambda cwd, prompt, model, logdir, **_kwargs: cap.update(prompt=prompt) or 0
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=tmp, wid="worker3", checkout=tmp / "checkout", logdir=tmp / "logs"), gh=None
    )
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)

    def render(area):
        cap.clear()
        tc.work_units.do_roadmap(w, None, types.SimpleNamespace(reason=area, pr=0, head=""), opts, bubble=False)
        return cap.get("prompt", "")

    os.environ["TAUCETI_EXTRA_ROADMAP_PROMPT"] = GUIDANCE
    prompt = render("Topology")
    check("guidance text is in the roadmap prompt", GUIDANCE in prompt)
    check("guidance has its own section", "## Operator guidance for this round" in prompt)
    check(
        "guidance section precedes target choice",
        prompt.index("## Operator guidance") < prompt.index("## Choose a target"),
    )
    check("guidance names the designated roadmap", "if `Topology` is `any`" in prompt)
    check("guidance cannot override the resolved skip list", "cannot override the `Algebra` exclusions" in prompt)
    check("no placeholder survives", "__EXTRA_ROADMAP_PROMPT__" not in prompt and "__SKIP__" not in prompt)

    # The text is shipped verbatim: a placeholder-shaped token in the operator's guidance is not
    # rewritten by a later substitution pass (fill_prompt makes one pass over the template).
    os.environ["TAUCETI_EXTRA_ROADMAP_PROMPT"] = "Prefer __BIN__ style names"
    check("guidance is not re-substituted", "Prefer __BIN__ style names" in render("Topology"))

    os.environ["TAUCETI_ROADMAP_SKIP"] = ""
    prompt = render("Topology")
    check("no skip list → no `none` in the guidance", "cannot override the exclusions below" in prompt)

    os.environ.pop("TAUCETI_EXTRA_ROADMAP_PROMPT", None)
    prompt = render("Topology")
    check("no guidance → no section", "Operator guidance" not in prompt)
    check("no guidance → placeholder still filled", "__EXTRA_ROADMAP_PROMPT__" not in prompt)
    check("no guidance → sections stay adjacent", "Work autonomously to completion.\n\n## Choose a target" in prompt)

    # ---- workers.toml: the field validates at the boundary and round-trips to the work argv -------
    spec = wm.WorkerSpec.from_dict({"id": "w", "only": ["roadmap"], "extra_roadmap_prompt": f"  {GUIDANCE}  "}, 0)
    check("spec normalises the guidance", spec.extra_roadmap_prompt == GUIDANCE)
    check("spec serialises the guidance", spec.as_dict()["extra_roadmap_prompt"] == GUIDANCE)
    argv = spec.work_argv()
    check("spec passes --extra-roadmap-prompt to the worker", f"--extra-roadmap-prompt={GUIDANCE}" in argv)
    dashed = wm.WorkerSpec(id="w", only=("roadmap",), extra_roadmap_prompt="--Topology first, then Algebra")
    parsed = tc.build_parser().parse_args(dashed.work_argv()[3:])
    check(
        "guidance that starts with a dash survives the work argv",
        parsed.extra_roadmap_prompt == "--Topology first, then Algebra",
    )
    check(
        "a plain spec passes no flag",
        not any(a.startswith("--extra-roadmap-prompt") for a in wm.WorkerSpec(id="plain").work_argv()),
    )
    check("unset field stays unset", wm.WorkerSpec.from_dict({"id": "w"}, 0).extra_roadmap_prompt is None)
    check(
        "guidance changes the fingerprint (restarts the worker)",
        wm.WorkerSpec(id="w").fingerprint() != wm.WorkerSpec(id="w", extra_roadmap_prompt=GUIDANCE).fingerprint(),
    )
    rejects(
        "blank field is a config error",
        lambda: wm.WorkerSpec.from_dict({"id": "w", "extra_roadmap_prompt": " "}, 0),
        "workers[0].extra_roadmap_prompt: extra roadmap prompt must not be empty",
    )
    rejects(
        "non-string field is a config error",
        lambda: wm.WorkerSpec.from_dict({"id": "w", "extra_roadmap_prompt": 3}, 0),
        "workers[0].extra_roadmap_prompt must be a string",
    )
    rejects(
        "oversized field is a config error",
        lambda: wm.WorkerSpec.from_dict({"id": "w", "extra_roadmap_prompt": "x" * 5000}, 0),
        "the limit is",
    )

    toml_path = tmp / "workers.toml"
    toml_path.write_text(
        textwrap.dedent(
            f'''
            version = 1

            [[workers]]
            id = "worker3"
            only = ["roadmap"]
            extra_roadmap_prompt = "{GUIDANCE}"
            '''
        )
    )
    loaded = wm.load_worker_specs(toml_path)
    check("workers.toml field is loaded", loaded[0].extra_roadmap_prompt == GUIDANCE)

    legacy = tmp / "workers.sh"
    legacy.write_text(f'./tauceti work --loop --worker-id worker9 --only roadmap --extra-roadmap-prompt "{GUIDANCE}"\n')
    check(
        "legacy argv import maps --extra-roadmap-prompt",
        wm.parse_legacy_config(legacy)[0].extra_roadmap_prompt == GUIDANCE,
    )
    legacy.write_text(f'./tauceti work --loop --worker-id worker9 --only roadmap "--extra-roadmap-prompt={GUIDANCE}"\n')
    check(
        "legacy argv import accepts the = form work_argv emits",
        wm.parse_legacy_config(legacy)[0].extra_roadmap_prompt == GUIDANCE,
    )
    legacy.write_text('./tauceti work --loop --worker-id worker9 --extra-roadmap-prompt "first line\nsecond line"\n')
    rejects(
        "a value spanning lines is a located config error, not a crash",
        lambda: wm.parse_legacy_config(legacy),
        "workers.sh:1:",
    )

    from tauceti_worker import tui

    cmd = tui.launch_cmd("roadmap", "auto", False, True, "Topology", None, extra_roadmap_prompt="--Topology first")
    check(
        "dashboard launch command carries the guidance as one argument",
        "--extra-roadmap-prompt=--Topology first" in cmd,
    )
    check(
        "dashboard launch command without guidance omits the flag",
        not any(a.startswith("--extra-roadmap-prompt") for a in tui.launch_cmd(None, "auto", False, False, None, None)),
    )
    saved_mgr = wm.ensure_manager
    wm.ensure_manager = lambda config: None
    try:
        added = wm.add_dashboard_worker(
            tmp / "dash.toml",
            only="roadmap",
            agent="auto",
            bubble=False,
            roadmap_only="Topology",
            roadmap_skip=None,
            extra_roadmap_prompt=GUIDANCE,
        )
    finally:
        wm.ensure_manager = saved_mgr
    check(
        "dashboard-added worker keeps the guidance",
        wm.load_worker_specs(tmp / "dash.toml")[0].extra_roadmap_prompt == GUIDANCE
        and added.extra_roadmap_prompt == GUIDANCE,
    )

    status = "\n".join(
        wm._worker_configuration_lines(
            {
                "id": "worker3",
                "only": ["roadmap"],
                "spec": {"id": "worker3", "only": ["roadmap"], "extra_roadmap_prompt": f"{GUIDANCE}\n  on two lines"},
            },
            80,
        )
    )
    check("status shows the guidance, whitespace collapsed", f"  guidance: {GUIDANCE} on two lines" in status)
    check("status does not call enabled guidance unused", "unused" not in status)
    status = "\n".join(
        wm._worker_configuration_lines(
            {"id": "w", "only": ["review"], "spec": {"id": "w", "only": ["review"], "extra_roadmap_prompt": GUIDANCE}},
            200,
        )
    )
    check(
        "status flags guidance no phase will use",
        f"guidance: {GUIDANCE} (unused: the roadmap phase is not enabled)" in status,
    )
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

raise SystemExit(bool(fails))
