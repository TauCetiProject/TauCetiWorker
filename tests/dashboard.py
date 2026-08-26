#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich", "textual>=8,<9"]
# ///
"""Headless smoke for the Textual dashboard: build the app with a STUB survey loader (no GitHub, no
tty) and drive it with App.run_test(). Covers the lag fix (cursor/dials are local state), expand/
collapse, the launch follow-up, the roadmap-only picker (exercises the OptionList.OptionSelected
path end-to-end, so attribute-name drift across textual versions is caught), the stale roadmap-row
redraw, prefs persistence + env-override precedence, and the stale-survey load-token guard.
Exit 0 = all checks pass, 1 = a failure."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# Isolate the prefs file: persistence must land here, not in the operator's real ~/.config.
_CFGDIR = tempfile.mkdtemp(prefix="tauceti-prefs-")
for _key in (
    "TAUCETI_CONFIG_HOME",
    "TAUCETI_WORKERS_CONFIG",
    "TAUCETI_WORKERS_STATE_DIR",
    "TAUCETI_RUNTIME_DIR",
):
    os.environ.pop(_key, None)
os.environ["XDG_CONFIG_HOME"] = _CFGDIR
os.environ["XDG_STATE_HOME"] = str(Path(_CFGDIR) / "state-home")
os.environ["TAUCETI_RUNTIME_DIR"] = str(Path(_CFGDIR) / "runtime")
os.environ.pop("TAUCETI_ROADMAP_ONLY", None)  # start from a known (unset) area state
os.environ.pop("TAUCETI_ROADMAP_SKIP", None)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

assert tc.default_workers_config().is_relative_to(Path(_CFGDIR)), (
    f"dashboard test configuration escaped its temporary root: {tc.default_workers_config()}"
)

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    if not cond:
        fails += 1


def fake_survey(next_stage="review"):
    sv = tc.Survey(worker_id="tester")
    sv.open_prs = [
        tc.PRInfo(
            number=n,
            head_oid="x",
            head_ref="b",
            head_owner="o",
            head_repo="r",
            is_draft=False,
            mergeable="MERGEABLE",
            author="me",
            build_success=True,
            build_failed=False,
            title=f"PR {n}",
        )
        for n in (101, 102)
    ]
    sv.n_open_nondraft = 2
    sv.n_reviewable = 2
    sv.status_labels = [
        ("awaiting-CI", 0, 0),
        ("awaiting-review", 2, 1),
        ("review-in-progress", 0, 0),
        ("ci-failed", 0, 0),
        ("awaiting-author", 0, 0),
        ("ready-to-merge", 0, 0),
    ]
    sv.reviewable.actionable = [tc.Candidate(101, "x"), tc.Candidate(102, "x")]
    _f = tc.roadmap_only()  # mirror survey()'s sanitization: None → "auto"
    sv.roadmap_only = "auto" if _f is None else (_f or "any")
    sv.roadmap_skip = tc.roadmap_skip()
    sv._mine_open_prs = [
        tc.PRInfo(
            number=200 + n,
            head_oid="x",
            head_ref="roadmap/x",
            head_owner="o",
            head_repo="r",
            is_draft=False,
            mergeable="MERGEABLE",
            author="me",
            build_success=True,
            build_failed=False,
            labels=("roadmap/algebra" if n < tc.MAX_OPEN_PRS else "roadmap/topology",),
        )
        for n in range(tc.MAX_OPEN_PRS + 1)
    ]
    sv.rescope_roadmap()
    sv.next_auto_stage = next_stage
    return sv


def loader():
    return fake_survey(), {}, ["algebra", "topology"]


CFG = SimpleNamespace(logdir=Path("/tmp/tauceti-test"), home=Path(_CFGDIR), state=Path(_CFGDIR) / "state")


async def await_survey(app, pilot):
    for _ in range(40):
        if app.sv is not None:
            return
        await pilot.pause(0.05)


async def test_dashboard():
    app = tc._dashboard_app(CFG, loader=loader)
    async with app.run_test() as pilot:
        await await_survey(app, pilot)
        check("survey loaded", app.sv is not None)
        check("cursor defaults to next_auto (review)", tc.ALLOWED_TASKS[app.sel] == "review")
        table = app.query_one("#tbl")
        check("table has the 6 kinds", table.row_count == len(tc.ALLOWED_TASKS))

        await pilot.press("down")
        check("down moves to fix-ci", tc.ALLOWED_TASKS[app.sel] == "fix-ci")
        await pilot.press("up")
        await pilot.press("up")
        check("up wraps past review to rebase", tc.ALLOWED_TASKS[app.sel] == "rebase")

        await pilot.press("m")
        check("m cycles model auto->codex", app.model_dial == "codex")
        await pilot.press("s")
        check("s toggles sandbox to bubble", app.bubble is True)

        before = table.row_count
        await pilot.press("down")  # onto review (index 1)
        await pilot.press("right")
        check("review is expanded", "review" in app.expanded)
        check("expand added PR sub-rows", table.row_count == before + 2)
        await pilot.press("left")
        check("collapse removed them", table.row_count == before)

        # roadmap-only picker: open, move to "topology", select. Drives OptionList end-to-end so a
        # textual rename of the OptionSelected attribute would fail here, not silently at runtime.
        await pilot.press("o")
        await pilot.pause(0.15)
        await pilot.press("down")  # (all areas) -> algebra
        await pilot.press("down")  # algebra -> topology
        await pilot.press("enter")
        await pilot.pause(0.1)
        check("only picker set env area to topology", os.environ.get("TAUCETI_ROADMAP_ONLY") == "topology")
        check("roadmap row area updated immediately (no stale redraw)", app.sv.roadmap_only == "topology")
        check("roadmap row count rescoped immediately", app.sv.n_mine_open == 1)
        check("roadmap row backpressure rescoped immediately", app.sv.roadmap_backpressure is False)

        # a digit jumps the cursor to that kind and launches a one-round follow-up (then exits)
        await pilot.press("2")
        check("digit 2 selects review", tc.ALLOWED_TASKS[app.sel] == "review")
        await pilot.pause(0.1)
    check("one-round launch set an exec follow-up", app.followup is not None and app.followup[0] == "exec")
    check("follow-up runs review", app.followup is not None and "review" in app.followup[1])

    # persistence: the m/s/f changes wrote a prefs file under XDG_CONFIG_HOME
    prefs_file = tc._prefs_path(CFG)
    check("prefs file written under XDG_CONFIG_HOME", prefs_file.exists() and str(prefs_file).startswith(_CFGDIR))
    saved = json.loads(prefs_file.read_text())
    check("prefs persisted model=codex", saved.get("model") == "codex")
    check("prefs persisted bubble=True", saved.get("bubble") is True)
    check("prefs persisted user-chosen only=topology", saved.get("roadmap_only") == "topology")

    app2 = tc._dashboard_app(CFG, loader=loader)
    async with app2.run_test() as pilot:
        await pilot.pause(0.05)
        check("restart restores model=codex", app2.model_dial == "codex")
        check("restart restores sandbox=bubble", app2.bubble is True)


async def test_cursor_before_load():
    """#2: a cursor move made before the first survey lands must not be snapped back by the
    auto-select. Use a slow loader whose next_auto (roadmap, idx 5) differs from where we move."""

    def slow():
        time.sleep(0.6)
        return fake_survey(next_stage="roadmap"), {}, []

    app = tc._dashboard_app(CFG, loader=slow)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)  # load worker is still sleeping
        check("no survey yet (slow loader)", app.sv is None)
        await pilot.press("down")  # rebase(0) -> review(1), and marks _sel_init
        moved = app.sel
        await await_survey(app, pilot)  # first survey now lands
        check("user cursor survives the initial load", app.sel == moved == 1)
        check("auto-select did not snap to next_auto (roadmap)", tc.ALLOWED_TASKS[app.sel] != "roadmap")


async def test_sticky_env_focus():
    """#5: a transient TAUCETI_ROADMAP_ONLY override must NOT be written into prefs by an unrelated
    dial change, or it becomes sticky on later runs that have no env override."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-env-")
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    os.environ["TAUCETI_ROADMAP_ONLY"] = "EnvOnly"
    try:
        cfg = SimpleNamespace(logdir=Path("/tmp/x"), home=Path(cfgdir), state=Path(cfgdir) / "state")
        app = tc._dashboard_app(cfg, loader=loader)
        async with app.run_test() as pilot:
            await await_survey(app, pilot)
            await pilot.press("m")  # change model only — must not persist the env area
            await pilot.pause(0.05)
        saved = json.loads(tc._prefs_path(cfg).read_text())
        check("model-only change saved the model", saved.get("model") == "codex")
        check("env-only area NOT persisted as sticky", saved.get("roadmap_only") != "EnvOnly")
    finally:
        os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR


def test_load_token():
    """#1: a stale survey result (older load token) must be ignored, not clobber fresher state."""
    app = tc._dashboard_app(CFG, loader=loader)
    app._load_seq = 5
    app.sv = "FRESH"
    app._loaded(3, fake_survey(), {}, [], None)  # seq 3 < 5 -> stale
    check("stale load result ignored", app.sv == "FRESH")


def test_random_default():
    """The new default: with no area pinned, do_roadmap resolves "auto" to a random roadmap area
    (and falls back to "all areas" when the area list can't be fetched). --roadmap-skip removes
    areas from the random pick and is injected into the prompt. Stub the IO so only the
    area-resolution + prompt substitution runs."""
    captured = {}
    orig = {
        k: getattr(tc.work_units, k)
        for k in (
            "fetch_ref",
            "prepare_checkout",
            "run_agent_host",
            "roadmap_areas",
            "ensure_fork",
            "administrative_hold_avoid_list",
        )
    }
    orig_choice = tc.random.choice
    tc.work_units.fetch_ref = lambda *a, **k: True
    tc.work_units.ensure_fork = lambda: "alice/TauCeti"  # do_roadmap forks before launching; stub it
    tc.work_units.administrative_hold_avoid_list = lambda *_args: "none"
    tc.work_units.prepare_checkout = lambda cfg: True
    tc.work_units.run_agent_host = lambda cwd, prompt, work_model, logdir: (captured.update(prompt=prompt), 0)[1]
    cfg = SimpleNamespace(
        state=Path("/tmp/tauceti-test/state"),
        checkout=Path("/tmp/tauceti-test/co"),
        logdir=Path("/tmp/tauceti-test"),
        wid="default",
    )
    w = SimpleNamespace(cfg=cfg, gh=object())
    opts = SimpleNamespace(agent_name="Claude Code", work_model="claude")
    try:
        # areas available → a random area is chosen and substituted into the prompt
        tc.work_units.roadmap_areas = lambda gh: ["algebra", "topology"]
        tc.random.choice = lambda seq: seq[-1]  # deterministic: the last remaining area
        tc.do_roadmap(w, None, tc.Candidate(0, "", "auto"), opts, False)
        check("auto leaves no __ONLY__ placeholder", "__ONLY__" not in captured["prompt"])
        check("auto leaves no __SKIP__ placeholder", "__SKIP__" not in captured["prompt"])
        check("auto picked the area from the list", "`topology`" in captured["prompt"])
        check("no skip set → prompt says none", "`none`" in captured["prompt"])
        # --roadmap-skip excludes an area from the random pick (so only "topology" remains)
        captured.clear()
        os.environ["TAUCETI_ROADMAP_SKIP"] = "algebra"
        tc.do_roadmap(w, None, tc.Candidate(0, "", "auto"), opts, False)
        check("auto skips the excluded area", "`topology`" in captured["prompt"])
        check("skipped area named in the prompt", "algebra" in captured["prompt"])
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        # no areas (fetch failed) → fall back to all areas ("any")
        captured.clear()
        tc.work_units.roadmap_areas = lambda gh: []
        tc.do_roadmap(w, None, tc.Candidate(0, "", "auto"), opts, False)
        check("auto falls back to all areas when the list is empty", "`any`" in captured["prompt"])
    finally:
        for k, v in orig.items():
            setattr(tc.work_units, k, v)
        tc.random.choice = orig_choice
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        os.environ.pop("TAUCETI_REQUIRE_TARGET_MARKER", None)


def test_skip_edge_cases():
    """do_roadmap edge cases: an explicit --roadmap-only that overlaps --roadmap-skip must NOT tell
    the agent to avoid its own pinned area (a contradiction), and skipping every known area in auto
    mode must raise NoProgress rather than launch a doomed round."""
    captured = {}
    orig = {
        k: getattr(tc.work_units, k)
        for k in (
            "fetch_ref",
            "prepare_checkout",
            "run_agent_host",
            "roadmap_areas",
            "ensure_fork",
            "administrative_hold_avoid_list",
        )
    }
    tc.work_units.fetch_ref = lambda *a, **k: True
    tc.work_units.ensure_fork = lambda: "alice/TauCeti"  # do_roadmap forks before launching; stub it
    tc.work_units.administrative_hold_avoid_list = lambda *_args: "none"
    tc.work_units.prepare_checkout = lambda cfg: True
    tc.work_units.run_agent_host = lambda cwd, prompt, work_model, logdir: (captured.update(prompt=prompt), 0)[1]
    tc.work_units.roadmap_areas = lambda gh: ["algebra", "topology"]
    cfg = SimpleNamespace(
        state=Path("/tmp/tauceti-test/state"),
        checkout=Path("/tmp/tauceti-test/co"),
        logdir=Path("/tmp/tauceti-test"),
        wid="default",
    )
    w = SimpleNamespace(cfg=cfg, gh=object())
    opts = SimpleNamespace(agent_name="Claude Code", work_model="claude")
    try:
        # --roadmap-only algebra overlaps --roadmap-skip algebra → only wins, skip line drops algebra
        os.environ["TAUCETI_ROADMAP_SKIP"] = "algebra,topology"
        tc.do_roadmap(w, None, tc.Candidate(0, "", "algebra"), opts, False)
        check("pinned area still drives the prompt", "`algebra`" in captured["prompt"])
        check("pinned area not listed as skipped", "Never target `topology`" in captured["prompt"])
        # auto mode with every known area skipped → NoProgress (not a fallback to "any")
        captured.clear()
        raised = False
        try:
            tc.do_roadmap(w, None, tc.Candidate(0, "", "auto"), opts, False)
        except tc.NoProgress:
            raised = True
        check("all-areas-skipped auto raises NoProgress", raised)
        check("no doomed round launched", "prompt" not in captured)
    finally:
        for k, v in orig.items():
            setattr(tc.work_units, k, v)
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        os.environ.pop("TAUCETI_REQUIRE_TARGET_MARKER", None)


def test_launch_cmd_skip_tristate():
    """launch_cmd embeds the raw skip value verbatim: omitted when None, but an explicit empty string
    is preserved so a copied command clears an inherited TAUCETI_ROADMAP_SKIP."""
    check("None skip omits the flag", "--roadmap-skip" not in tc.launch_cmd(None, "auto", False, False, None, None))
    check("empty skip is preserved", tc.launch_cmd(None, "auto", False, False, None, "")[-2:] == ["--roadmap-skip", ""])
    check(
        "non-empty skip passed through",
        tc.launch_cmd(None, "auto", False, False, None, "a,b")[-2:] == ["--roadmap-skip", "a,b"],
    )


def test_roadmap_skip_parse():
    """roadmap_skip() parses the comma-separated env into a deduped, sorted list (empties dropped)."""
    old = os.environ.get("TAUCETI_ROADMAP_SKIP")
    try:
        os.environ["TAUCETI_ROADMAP_SKIP"] = " topology , algebra ,, topology "
        check("skip parsed, deduped, sorted", tc.roadmap_skip() == ["algebra", "topology"])
        os.environ["TAUCETI_ROADMAP_SKIP"] = ""
        check("blank skip → empty list", tc.roadmap_skip() == [])
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        check("unset skip → empty list", tc.roadmap_skip() == [])
    finally:
        if old is None:
            os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        else:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old


def test_bare_cli_ignores_prefs():
    """Requirement 1: a saved dashboard pref must NOT be read by a bare CLI run. roadmap_only()/
    roadmap_skip() consult only the env, so with a prefs file present but the env unset they stay
    at their unset defaults."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-cli-")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    old_only = os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
    old_skip = os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    try:
        cfg = SimpleNamespace(home=Path(cfgdir))
        tc.save_dashboard_prefs(
            cfg, {"model": "auto", "bubble": False, "roadmap_only": "SavedArea", "roadmap_skip": "SkipArea"}
        )
        check("bare CLI only is None (auto), ignoring the saved pref", tc.roadmap_only() is None)
        check("bare CLI skip is empty, ignoring the saved pref", tc.roadmap_skip() == [])
    finally:
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR if old_xdg is None else old_xdg
        if old_only is not None:
            os.environ["TAUCETI_ROADMAP_ONLY"] = old_only
        if old_skip is not None:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old_skip


def test_dashboard_uses_saved_pref():
    """Requirement 1 (other half): the dashboard DOES apply the saved only/skip — on init it seeds the
    process env (so its display and any launched round inherit it) when the env is unset."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-dash-")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    old_only = os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
    old_skip = os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    try:
        cfg = SimpleNamespace(home=Path(cfgdir), state=Path(cfgdir) / "state", logdir=Path("/tmp/x"))
        tc.save_dashboard_prefs(
            cfg, {"model": "auto", "bubble": False, "roadmap_only": "topology", "roadmap_skip": "algebra"}
        )
        tc._dashboard_app(cfg, loader=loader)  # runs the saved-pref restore
        check("dashboard applied the saved only to the env", os.environ.get("TAUCETI_ROADMAP_ONLY") == "topology")
        check("dashboard applied the saved skip to the env", os.environ.get("TAUCETI_ROADMAP_SKIP") == "algebra")
    finally:
        os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR if old_xdg is None else old_xdg
        if old_only is not None:
            os.environ["TAUCETI_ROADMAP_ONLY"] = old_only
        if old_skip is not None:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old_skip


async def test_skip_dashboard():
    """The [x] skip control: the TextPrompt sets a normalized TAUCETI_ROADMAP_SKIP, updates the
    survey row immediately, and persists the user-chosen skip to prefs."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-skip-")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    old_skip = os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    try:
        cfg = SimpleNamespace(logdir=Path("/tmp/x"), home=Path(cfgdir), state=Path(cfgdir) / "state")
        app = tc._dashboard_app(cfg, loader=loader)
        async with app.run_test() as pilot:
            await await_survey(app, pilot)
            app._apply_skip("topology, algebra,, topology")  # exercise the apply path directly
            await pilot.pause(0.05)
        check("skip env normalized (deduped, sorted)", os.environ.get("TAUCETI_ROADMAP_SKIP") == "algebra,topology")
        check("roadmap row skip updated immediately", app.sv.roadmap_skip == ["algebra", "topology"])
        saved = json.loads(tc._prefs_path(cfg).read_text())
        check("prefs persisted user-chosen skip", saved.get("roadmap_skip") == "algebra,topology")
    finally:
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR if old_xdg is None else old_xdg
        if old_skip is not None:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old_skip


async def test_persistent_workers_view():
    """The dashboard's loop action writes desired state, and the Workers view monitors and edits it."""
    config = tc.default_workers_config()
    tc.save_worker_specs(
        config,
        [tc.WorkerSpec(id="worker1"), tc.WorkerSpec(id="worker2", agent="codex", only=("review",))],
    )
    old_ensure = tc.worker_manager.ensure_manager
    tc.worker_manager.ensure_manager = lambda _config: False
    try:
        app = tc._dashboard_app(CFG, loader=loader)
        async with app.run_test() as pilot:
            await await_survey(app, pilot)
            await pilot.press("w")
            await pilot.pause(0.05)
            check("w opens the persistent Workers view", app.view == "workers")
            check("Workers view lists configured workers", app.query_one("#workers-tbl").row_count == 2)
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause(0.05)
            check("space persists desired stopped state", tc.load_worker_specs(config)[1].enabled is False)
            await pilot.press("w")
            await pilot.press("l")
            await pilot.pause(0.05)
            specs = tc.load_worker_specs(config)
            check("loop action adds a persistent worker", len(specs) == 3 and specs[-1].id == "worker3")
            check("persistent launch carries dashboard model", specs[-1].agent == app.model_dial)
    finally:
        tc.worker_manager.ensure_manager = old_ensure


def test_dashboard_migrates_host_pref():
    """The sandbox pref key was renamed host -> bubble when the default flipped to host. An old prefs
    file (only the `host` key) must migrate as bubble = not host, so a user who was reviewing untrusted
    PRs in bubble (host=false) is NOT silently un-sandboxed on upgrade. A present `bubble` key wins."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-mig-")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    old_only = os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
    old_skip = os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    try:
        cfg = SimpleNamespace(home=Path(cfgdir), state=Path(cfgdir) / "state", logdir=Path("/tmp/x"))
        tc.save_dashboard_prefs(cfg, {"model": "auto", "host": False})  # old bubble default
        check("old host=false migrates to bubble mode", tc._dashboard_app(cfg, loader=loader).bubble is True)
        tc.save_dashboard_prefs(cfg, {"model": "auto", "host": True})  # old host toggle
        check("old host=true migrates to host mode", tc._dashboard_app(cfg, loader=loader).bubble is False)
        tc.save_dashboard_prefs(cfg, {"model": "auto", "bubble": True, "host": False})  # new key wins
        check("new bubble key takes precedence over legacy host", tc._dashboard_app(cfg, loader=loader).bubble is True)
        tc.save_dashboard_prefs(cfg, {"model": "auto"})  # neither key -> new host default
        check("no sandbox pref defaults to host", tc._dashboard_app(cfg, loader=loader).bubble is False)
    finally:
        os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR if old_xdg is None else old_xdg
        if old_only is not None:
            os.environ["TAUCETI_ROADMAP_ONLY"] = old_only
        if old_skip is not None:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old_skip


async def run_all():
    await test_dashboard()
    await test_cursor_before_load()
    await test_sticky_env_focus()
    test_load_token()
    test_random_default()
    test_skip_edge_cases()
    test_launch_cmd_skip_tristate()
    test_roadmap_skip_parse()
    test_bare_cli_ignores_prefs()
    test_dashboard_uses_saved_pref()
    test_dashboard_migrates_host_pref()
    await test_skip_dashboard()
    await test_persistent_workers_view()


asyncio.run(run_all())
sys.exit(1 if fails else 0)
