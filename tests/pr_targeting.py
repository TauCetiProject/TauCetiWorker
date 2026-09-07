#!/usr/bin/env python3
"""`--pr N[,N...]`: pointing a round at named pull requests instead of letting it pick off the queue.

The flag exists so an operator can say "work on #412" and get that, or an honest answer about why
not. The properties that make it safe to hand to an operator, and that this suite pins down:

  - it only ever REMOVES. A named PR the survey passed over does not become actionable by being
    named, so the attempt budgets, the daily review cap, a peer's in-progress review and the branch
    claims all still hold. `--pr` is a filter over what the round was already willing to do.
  - it composes with `--only` by intersection, and refuses up front when the two cannot both be
    honoured (`--only roadmap --pr 5` names a PR that does not exist yet).
  - it drops the two work units that name no existing PR. A targeted round that finds nothing to do
    on its targets must not fall through to authoring an unrelated roadmap PR; the operator asked
    about those PRs, and doing something else is the wrong answer to that request.
  - when nothing is actionable it says why, PR by PR, and exits without progress.
  - unlike the two review throttles, this one is operator-facing, so it IS documented.

Dependency-free; no network. The end-to-end cases drive the real run_round with survey and dispatch
stubbed, so they test the cascade's actual control flow rather than a re-description of it.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def raises_exit(fn, *args):
    """The message of the SystemExit `fn` raises, or None if it did not raise one."""
    try:
        fn(*args)
    except SystemExit as e:
        return str(e)
    return None


# --- parsing: --pr and $TAUCETI_PR ----------------------------------------------------------------
os.environ.pop("TAUCETI_PR", None)
check("no flag, no environment -> untargeted", tc.resolve_pr_targets([]), ())
check("one number", tc.resolve_pr_targets(["412"]), (412,))
check("a comma list", tc.resolve_pr_targets(["412,415"]), (412, 415))
check("a repeated flag", tc.resolve_pr_targets(["412", "415"]), (412, 415))
check("both forms together", tc.resolve_pr_targets(["412,415", "9"]), (412, 415, 9))
check("spaces are tolerated", tc.resolve_pr_targets(["412, 415"]), (412, 415))
check("a leading # is how PR numbers are written", tc.resolve_pr_targets(["#412,#415"]), (412, 415))
check("empty tokens are skipped", tc.resolve_pr_targets(["412,,415,"]), (412, 415))
check("duplicates collapse, order kept", tc.resolve_pr_targets(["415,412,415"]), (415, 412))
# A typo'd target must not read as "no targeting" and quietly turn the round back into a free-running
# one; that is the failure mode where an operator watches the worker do something else entirely.
check(
    "a non-number is a hard error",
    "not a pull request number" in (raises_exit(tc.resolve_pr_targets, ["abc"]) or ""),
    True,
)
check("zero is a hard error", raises_exit(tc.resolve_pr_targets, ["0"]) is not None, True)
check("a negative number is a hard error", raises_exit(tc.resolve_pr_targets, ["-3"]) is not None, True)
check("a range is a hard error", raises_exit(tc.resolve_pr_targets, ["1-5"]) is not None, True)

# An empty selection is the one outcome this flag must never produce silently: an operator who asked
# for targeting and got an unrestricted worker instead, indefinitely under --loop.
for empty in ("", "   ", ",,", " , "):
    check(
        f"--pr {empty!r} is refused, not read as untargeted",
        "names no pull request" in (raises_exit(tc.resolve_pr_targets, [empty]) or ""),
        True,
    )
check(
    "an empty repeated flag is refused too",
    raises_exit(tc.resolve_pr_targets, ["", ""]) is not None,
    True,
)
# A missing comma must not retarget the round at a different, possibly actionable, PR.
check(
    "internal whitespace is a missing comma, not a number",
    raises_exit(tc.resolve_pr_targets, ["4 12"]),
    "--pr value '4 12' is not a pull request number",
)
check("a repeated # prefix is refused", raises_exit(tc.resolve_pr_targets, ["##412"]) is not None, True)
# `str.isdigit()` accepts these; `int()` then raises an uncaught ValueError, so the token shape is
# validated by pattern rather than by predicate.
check("a non-ASCII digit is refused", raises_exit(tc.resolve_pr_targets, ["\u00b2"]) is not None, True)
check("surrounding whitespace is still fine", tc.resolve_pr_targets([" 412 , 415 "]), (412, 415))

os.environ["TAUCETI_PR"] = "77,78"
check("the environment supplies it when the flag is absent", tc.resolve_pr_targets([]), (77, 78))
check("the flag wins over the environment", tc.resolve_pr_targets(["412"]), (412,))
check(
    "an empty flag does not silently override a valid environment value",
    raises_exit(tc.resolve_pr_targets, [""]) is not None,
    True,
)
os.environ["TAUCETI_PR"] = "nope"
check("a bad environment value names itself", "$TAUCETI_PR" in (raises_exit(tc.resolve_pr_targets, []) or ""), True)
os.environ["TAUCETI_PR"] = ",,"
check(
    "an environment value that names nothing is refused",
    "names no pull request" in (raises_exit(tc.resolve_pr_targets, []) or ""),
    True,
)
os.environ["TAUCETI_PR"] = "   "
check("a blank environment value is untargeted", tc.resolve_pr_targets([]), ())
os.environ.pop("TAUCETI_PR", None)

# --- the flag reaches both `work` and the internal `_round` ---------------------------------------
probe = argparse.ArgumentParser(prog="probe")
tc.add_work_flags(probe)
check("--pr parses as a repeatable flag", probe.parse_args(["--pr", "412", "--pr", "415"]).pr, ["412", "415"])
check("absent -> the empty list", probe.parse_args([]).pr, [])

# --- --pr against a task selection that can never honour it ---------------------------------------
check("no targeting -> nothing to refuse", raises_exit(tc.raise_on_untargetable_tasks, (), ["roadmap"]), None)
check("the full cascade is fine", raises_exit(tc.raise_on_untargetable_tasks, (412,), []), None)
check("a PR work unit is fine", raises_exit(tc.raise_on_untargetable_tasks, (412,), ["fix", "review"]), None)
check(
    "--only roadmap is refused up front",
    "none of those act on an existing PR" in (raises_exit(tc.raise_on_untargetable_tasks, (412,), ["roadmap"]) or ""),
    True,
)
check(
    "--only progress,roadmap is refused too",
    raises_exit(tc.raise_on_untargetable_tasks, (412,), ["progress", "roadmap"]) is not None,
    True,
)
check(
    "one PR work unit among them is enough",
    raises_exit(tc.raise_on_untargetable_tasks, (412,), ["roadmap", "fix"]),
    None,
)


# --- focus_prs: a filter, never an override -------------------------------------------------------
def survey_with(*, actionable=None, suppressed=None, progress_due=False):
    """A Survey holding the given {stage: [pr, ...]} candidates."""
    sv = tc.Survey(worker_id="t")
    for stage, prs in (actionable or {}).items():
        sv.kind(stage).actionable += [tc.Candidate(n, f"head{n}", f"{stage} at head") for n in prs]
    for stage, prs in (suppressed or {}).items():
        sv.kind(stage).suppressed += [
            tc.Candidate(n, f"head{n}", f"{stage} at head", attempts=3, budget=3) for n in prs
        ]
    if progress_due:
        sv.progress.actionable.append(tc.Candidate(0, "", "8h cadence due"))
    return sv


class Opts:
    """The three fields focus_prs reads (it is tolerant of a lightweight options object)."""

    def __init__(self, prs=(), only=()):
        self.prs = tuple(prs)
        self.only = list(only)


def focused(sv, opts):
    tc.focus_prs(sv, opts)
    return {s: [c.pr for c in sv.kind(s).actionable] for s in tc.AUTO_STAGES if sv.kind(s).actionable}


sv = survey_with(actionable={"review": [1, 2, 3], "fix": [2, 4]}, progress_due=True)
check(
    "no --pr -> every stage untouched",
    focused(sv, Opts()),
    {"progress": [0], "fix": [2, 4], "review": [1, 2, 3]},
)

sv = survey_with(actionable={"review": [1, 2, 3], "fix": [2, 4], "fix-ci": [5], "rebase": [2]})
check(
    "every PR-bearing stage is narrowed to the named PRs",
    focused(sv, Opts(prs=[2, 5])),
    {"rebase": [2], "fix-ci": [5], "fix": [2], "review": [2]},
)

sv = survey_with(actionable={"review": [1]}, progress_due=True)
check("progress names no PR, so a targeted round drops it", focused(sv, Opts(prs=[1])), {"review": [1]})

# The core promise: naming a PR cannot resurrect it. A candidate the survey suppressed (its attempt
# budget spent) stays suppressed, and nothing is promoted into an actionable list.
sv = survey_with(actionable={"review": [1]}, suppressed={"fix": [7], "rebase": [7]})
check("a suppressed PR is not promoted by naming it", focused(sv, Opts(prs=[7])), {})
check("...and the suppressed lists are left as they were", [c.pr for c in sv.needs_fix.suppressed], [7])

sv = survey_with(actionable={"review": [1, 2]})
check("naming a PR with no candidate at all adds nothing", focused(sv, Opts(prs=[99])), {})

# --pr and --only intersect: --only decides which units run, --pr which PRs they may run on. The
# filter deliberately leaves a candidate under a disabled stage in place so the round can EXPLAIN it.
sv = survey_with(actionable={"review": [1], "fix": [1]})
tc.focus_prs(sv, Opts(prs=[1], only=["review"]))
check("--only does not empty the other stage's list", [c.pr for c in sv.needs_fix.actionable], [1])
check("...and the enabled stage keeps its candidate", [c.pr for c in sv.reviewable.actionable], [1])


# --- pr_focus_reason: the per-PR answer -----------------------------------------------------------
def reason(sv, opts, pr):
    tc.focus_prs(sv, opts)
    return tc.pr_focus_reason(sv, opts, pr)


sv = survey_with(actionable={"fix": [1]})
check(
    "actionable, but the task selection excludes it",
    "actionable for fix, which this round's --only/--skip excludes" in reason(sv, Opts(prs=[1], only=["review"]), 1),
    True,
)
sv = survey_with(suppressed={"fix": [1]})
check("out of attempts", reason(sv, Opts(prs=[1]), 1), "fix suppressed: fix at head (3/3 attempts spent)")

sv = survey_with()
sv.review_capped.append((1, "3/3"))
check("behind the daily review cap", reason(sv, Opts(prs=[1]), 1), "review: daily cap 3/3 reached")

sv = survey_with()
sv.review_inflight.append((1, "codex"))
check("a peer holds the head", reason(sv, Opts(prs=[1]), 1), "review: a peer reviewer (codex) holds this head")

sv = survey_with()
sv.review_stuck.append(1)
check("escalated for infrastructure repair", "needs infrastructure repair" in reason(sv, Opts(prs=[1]), 1), True)

sv = survey_with()
sv.fix_waiting.append((1, "reviews at head are all green"))
check("nothing to fix yet", reason(sv, Opts(prs=[1]), 1), "fix: reviews at head are all green")

# Several reasons can be true at once, and reporting only the first would send the operator after the
# wrong one.
sv = survey_with(suppressed={"fix": [1]})
sv.review_capped.append((1, "3/3"))
got = reason(sv, Opts(prs=[1]), 1)
check("every reason is reported, not just the first", ("attempts spent" in got, "daily cap" in got), (True, True))


def pr_info(number, *, draft=False, green_since=None):
    """An open PR. `green_since` is when its `build` status was posted, i.e. when it became
    reviewable — the clock --review-min-age reads."""
    return tc.PRInfo(
        number=number,
        head_oid=f"head{number}",
        head_ref=f"r{number}",
        head_owner="TauCetiProject",
        head_repo="TauCeti",
        is_draft=draft,
        mergeable="MERGEABLE",
        author="kim-em",
        build_success=True,
        build_failed=False,
        build_status_at=green_since,
    )


sv = survey_with()
check("a PR the survey never saw", "not an open PR" in reason(sv, Opts(prs=[1]), 1), True)
sv = survey_with()
sv.open_prs.append(pr_info(1, draft=True))
check("a draft", "a draft" in reason(sv, Opts(prs=[1]), 1), True)
sv = survey_with()
sv.open_prs.append(pr_info(1))
check("open with genuinely nothing to do", "no work unit actionable" in reason(sv, Opts(prs=[1]), 1), True)

# --- end to end: the real run_round ---------------------------------------------------------------
# Everything below drives the actual cascade. `dispatch` is stubbed so no model runs, but it can also
# be told to DECLINE a candidate (what a peer's branch claim looks like from here), and the GitHub
# object records the one write run_round makes outside dispatch — the stuck-review tracking issue.
home = Path(tempfile.mkdtemp())
cfg = tc.Config(
    wid="t",
    home=home,
    data_home=home,
    state=home / "state",
    checkout=home / "co",
    store_dir=home / "store",
    sbcache=home / "sb",
    logdir=home / "logs",
    quota_cache=home / "qc",
)


class FakeGitHub:
    """Records the GitHub writes a round makes outside dispatch."""

    def __init__(self):
        self.stuck_issues = []

    def ensure_stuck_issue(self, pr, reason, diagnostic):
        self.stuck_issues.append(pr)


gh = FakeGitHub()
worker = tc.Worker(cfg, gh, None, tc.Counters(cfg), None, None)
tc.work_units.mirror_creds = lambda _cfg: None
tc.work_units.spread_candidates = lambda cs: list(cs)  # the shuffle is not what is under test

dispatched = []
declines = set()
logged = []
tc.work_units.log = lambda msg: logged.append(msg)
tc.work_units.warn_red = lambda msg: logged.append(msg)


def fake_dispatch(stage, w, sv, c, opts):
    """Record the unit, or decline it the way a claimed candidate is declined (rc None)."""
    if (stage, c.pr) in declines:
        return None
    dispatched.append((stage, c.pr))
    return 0


tc.work_units.dispatch = fake_dispatch


def round_over(make_survey, opts, *, decline=()):
    """run_round against `make_survey`, returning (dispatched units, NoProgress message or None)."""
    dispatched.clear()
    logged.clear()
    gh.stuck_issues.clear()
    declines.clear()
    declines.update(decline)
    tc.work_units.survey = lambda *a, **k: make_survey()
    try:
        tc.run_round(worker, opts)
    except tc.NoProgress as e:
        return dispatched[:], str(e)
    return dispatched[:], None


def opts_for(prs=(), only=(), **kw):
    kw.setdefault("dry_run", False)
    return tc.RoundOpts(only=list(only), agent="codex", work_model="codex", sandbox_host=True, prs=tuple(prs), **kw)


def said(fragment):
    return any(fragment in line for line in logged)


units, why = round_over(lambda: survey_with(actionable={"review": [1, 2], "fix": [3]}), opts_for(prs=[2]))
check("the cascade runs the named PR's unit", units, [("review", 2)])
check("...and stops there", why, None)

units, why = round_over(lambda: survey_with(actionable={"review": [1, 2], "fix": [3]}), opts_for(prs=[3]))
check("cascade priority still decides WHICH unit, --pr only which PR", units, [("fix", 3)])

units, why = round_over(lambda: survey_with(actionable={"review": [1, 2], "fix": [3]}), opts_for(prs=[2, 3]))
check("with two targets the cascade's own order wins", units, [("fix", 3)])

# The case the flag exists for: none of the named PRs are actionable. A round with no targeting would
# fall through to authoring a roadmap PR; a targeted one must not.
units, why = round_over(lambda: survey_with(actionable={"review": [1]}), opts_for(prs=[99]))
check("no target actionable -> nothing is dispatched", units, [])
check("...and it says which PRs it was asked about", "#99" in (why or ""), True)
check("...and that it did nothing else", "no unrelated work was done" in (why or ""), True)
check("...having explained #99 by name", said("--pr #99:"), True)

# An untargeted round in the same situation DOES author, which is what makes the case above a choice.
units, why = round_over(lambda: survey_with(), opts_for())
check("without --pr the round still falls through to roadmap", units, [("roadmap", 0)])

units, why = round_over(lambda: survey_with(progress_due=True), opts_for(prs=[1]))
check("a due progress report is not a substitute for the named PR", units, [])

# --only still narrows a targeted round: #1 is actionable for review, but this round only fixes.
units, why = round_over(lambda: survey_with(actionable={"review": [1]}), opts_for(prs=[1], only=["fix"]))
check("--only and --pr intersect", units, [])
check("...and the logged reason names the excluded unit", said("--only/--skip excludes"), True)
check("...while the exception points at those reasons", "see the per-PR reasons above" in (why or ""), True)

# --- a candidate the cascade offers and dispatch turns down (a peer holds its claim) ----------------
units, why = round_over(lambda: survey_with(actionable={"review": [1]}), opts_for(prs=[1]), decline={("review", 1)})
check("a declined target is not worked", units, [])
check("...and the round does not fall through to roadmap", [s for s, _ in units], [])
check(
    "...and the decline is reported for that PR, not left implicit", said("--pr #1: review candidate was offered"), True
)
check("...and the summary still points somewhere real", "see the per-PR reasons above" in (why or ""), True)

# Without targeting the same decline falls through to authoring, as it always has.
units, why = round_over(lambda: survey_with(actionable={"review": [1]}), opts_for(), decline={("review", 1)})
check("without --pr a declined candidate still falls through to roadmap", units, [("roadmap", 0)])

# A declined target does not stop a second target from being worked.
units, why = round_over(
    lambda: survey_with(actionable={"review": [1, 2]}), opts_for(prs=[1, 2]), decline={("review", 1)}
)
check("the cascade moves on to the next target", units, [("review", 2)])


# --- the review throttles are not bypassed ---------------------------------------------------------
def reviewable_survey(prs):
    """A review queue of `prs`, every one of them green just now (so --review-min-age bites)."""
    sv = survey_with(actionable={"review": prs})
    sv.open_prs += [pr_info(n, green_since=int(time.time())) for n in prs]
    return sv


units, why = round_over(lambda: reviewable_survey([1, 2, 3]), opts_for(prs=[2], only=["review"], review_min_queue=5))
check("--pr does not bypass --review-min-queue", units, [])
check("...and the throttle is given as the target's reason", said("--review-min-queue"), True)

# The throttle measures the WHOLE queue, not the targeted subset: three PRs are awaiting review, so a
# minimum of three is met and the named one is reviewed. Filtering first would have made this 1 < 3.
units, why = round_over(lambda: reviewable_survey([1, 2, 3]), opts_for(prs=[2], only=["review"], review_min_queue=3))
check("the throttle counts the whole queue, not just the targets", units, [("review", 2)])

# --review-min-age likewise: #2 went green moments ago, well under the requested hour.
units, why = round_over(
    lambda: reviewable_survey([1, 2, 3]),
    opts_for(prs=[2], only=["review"], review_min_age=60),
)
check("--pr does not bypass --review-min-age", units, [])
check("...and that throttle is reported too", said("--review-min-age"), True)


# --- the survey's own stops hold: a named PR is not reviewed just because it was named ---------------
def capped_survey():
    sv = survey_with()  # a capped PR is not in the review queue at all
    sv.open_prs.append(pr_info(412))
    sv.review_capped.append((412, "3/3"))
    return sv


units, why = round_over(capped_survey, opts_for(prs=[412]))
check("a PR at its daily review cap is not reviewed", units, [])
check("...and the cap is the reported reason", said("daily cap 3/3"), True)


def inflight_survey():
    sv = survey_with()
    sv.open_prs.append(pr_info(412))
    sv.review_inflight.append((412, "codex"))
    return sv


units, why = round_over(inflight_survey, opts_for(prs=[412]))
check("a PR a peer is reviewing is not reviewed", units, [])
check("...and the peer is the reported reason", said("holds this head"), True)

units, why = round_over(lambda: survey_with(suppressed={"fix": [412]}), opts_for(prs=[412]))
check("a PR whose fix budget is spent is not fixed", units, [])
check("...and the spent budget is the reported reason", said("attempts spent"), True)


# --- GitHub writes outside dispatch stay inside the target set --------------------------------------
def stuck_survey():
    sv = survey_with(actionable={"review": [412]})
    sv.open_prs += [pr_info(412), pr_info(999)]
    sv.review_stuck.append(999)
    return sv


units, why = round_over(stuck_survey, opts_for(prs=[412]))
check("a targeted round files no tracking issue for an unrelated PR", gh.stuck_issues, [])
check("...and does not even warn about it", said("#999"), False)
check("...while still doing the work it was asked for", units, [("review", 412)])

units, why = round_over(stuck_survey, opts_for())
check("without --pr the escalation still fires", gh.stuck_issues, [999])

units, why = round_over(stuck_survey, opts_for(prs=[999]))
check("naming the stuck PR does escalate it", gh.stuck_issues, [999])

units, why = round_over(stuck_survey, opts_for(dry_run=True))
check("--dry-run writes no tracking issue", gh.stuck_issues, [])
check("...but still says what it would have filed", said("[dry-run] would open/refresh"), True)


# --- the loop re-applies the targeting every round --------------------------------------------------
# Behavioural, not a source grep: run the real driver for two rounds and read the children's argv.
class Stop(Exception):
    """Ends the driver loop after the second back-off."""


class LoopClock:
    """time for the loop module. Sleeping is the loop backing off; the second one ends the test."""

    naps: list = []

    @staticmethod
    def sleep(n):
        LoopClock.naps.append(n)
        if len(LoopClock.naps) >= 2:
            raise Stop

    time = staticmethod(time.time)


spawned = []
saved = (tc.loop.time, tc.loop.github_budget, tc.loop.run_round_subprocess)
tc.loop.time = LoopClock
tc.loop.github_budget = lambda: {"core": (5000, 0), "graphql": (5000, 0)}
tc.loop.run_round_subprocess = lambda tail: spawned.append(tail) or tc.EX_NOPROGRESS
loop_args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
try:
    # An unpaced provider, so the loop needs no quota endpoint to reach the round spawn.
    tc.loop.cmd_loop(loop_args, SimpleNamespace(wid="t"), only=["review"], agent="deepseek", prs=(412, 415))
except Stop:
    pass

check("the loop ran more than one round", len(spawned), 2)
check(
    "every round of a targeted loop carries the targets",
    [tail[tail.index("--pr") + 1] if "--pr" in tail else None for tail in spawned],
    ["412,415", "412,415"],
)
check("...and a fruitless targeted round backs off rather than widening", LoopClock.naps[0] > 0, True)

spawned.clear()
LoopClock.naps.clear()
try:
    tc.loop.cmd_loop(loop_args, SimpleNamespace(wid="t"), only=["review"], agent="deepseek")
except Stop:
    pass
check("an untargeted loop passes no --pr", ["--pr" in tail for tail in spawned], [False, False])
tc.loop.time, tc.loop.github_budget, tc.loop.run_round_subprocess = saved

# --- this one is documented -------------------------------------------------------------------------
probe = argparse.ArgumentParser(prog="probe")
tc.add_work_flags(probe)
check("--pr appears in --help", "--pr" in probe.format_help(), True)
readme = (REPO / "README.md").read_text()
reference = (REPO / "docs" / "reference.md").read_text()
check("the README documents the flag", "`--pr`" in readme, True)
check("the reference documents the flag", "`--pr N[,N...]`" in reference, True)
check("the reference documents the environment variable", "`TAUCETI_PR`" in reference, True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
