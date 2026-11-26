#!/usr/bin/env python3
"""The two UNDOCUMENTED review throttles: `--review-min-queue N` holds review until at least N PRs are
awaiting it, `--review-min-age M` leaves a PR alone until it has been awaiting review for M minutes.

Both are expert-only knobs that may only ever REMOVE review candidates, so the properties that matter
are: off by default (the whole worker behaves exactly as before), never touching another stage, the
queue depth measured BEFORE the age filter (so "at least N awaiting review" means what an operator
reads off the dashboard), and fail-OPEN on a PR whose `build` status carries no readable timestamp —
an unknown waiting time must not make a worker silently never review it.

`build_status_at` is where the waiting time comes from: `gh` normalizes a StatusContext's createdAt to
`startedAt`, so it is the instant the authoritative `build` status was posted for THIS head — which is
also the instant the PR became reviewable, and it resets when a push moves the head.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import argparse
import dataclasses
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

NOW = 1_760_000_000.0
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def pr_json(number, *, posted="2026-08-16T02:32:37Z", state="SUCCESS"):
    rollup = [{"__typename": "StatusContext", "context": "build", "state": state}]
    if posted is not None:
        rollup[0]["startedAt"] = posted
    return {"number": number, "statusCheckRollup": rollup}


def survey_with(prs, *, minutes_ago):
    """A Survey whose review queue is `prs`, each green since `minutes_ago[pr]` minutes ago (None = a
    build status with no readable timestamp)."""
    sv = tc.Survey(worker_id="t")
    for n in prs:
        ago = minutes_ago[n]
        sv.open_prs.append(
            tc.PRInfo(
                number=n,
                head_oid=f"head{n}",
                head_ref=f"r{n}",
                head_owner="TauCetiProject",
                head_repo="TauCeti",
                is_draft=False,
                mergeable="MERGEABLE",
                author="kim-em",
                build_success=True,
                build_failed=False,
                build_status_at=None if ago is None else int(NOW - ago * 60),
            )
        )
        sv.reviewable.actionable.append(tc.Candidate(n, f"head{n}", "build-green, head not cleanly reviewed"))
    return sv


class Opts:
    """The two fields throttle_review reads (it is tolerant of a lightweight options object)."""

    def __init__(self, min_queue=0, min_age=0):
        self.review_min_queue = min_queue
        self.review_min_age = min_age


def throttled(prs, minutes_ago, opts):
    sv = survey_with(prs, minutes_ago=minutes_ago)
    tc.throttle_review(sv, opts, now=NOW)
    return [c.pr for c in sv.reviewable.actionable]


# --- build_status_at: the waiting-time source -----------------------------------------------------
check(
    "StatusContext startedAt -> build_status_at",
    tc.PRInfo.from_json(pr_json(1, posted="2026-08-16T02:32:37Z")).build_status_at,
    int(tc._parse_iso8601("2026-08-16T02:32:37Z")),
)
check("no timestamp -> None", tc.PRInfo.from_json(pr_json(1, posted=None)).build_status_at, None)
check("no build status at all -> None", tc.PRInfo.from_json({"number": 1}).build_status_at, None)
# Several `build` contexts: the LATEST is when the head became fully green.
check(
    "several build statuses -> the latest one",
    tc.PRInfo.from_json(
        {
            "number": 1,
            "statusCheckRollup": [
                {
                    "__typename": "StatusContext",
                    "context": "build",
                    "state": "SUCCESS",
                    "startedAt": "2026-08-16T02:00:00Z",
                },
                {
                    "__typename": "StatusContext",
                    "context": "build",
                    "state": "SUCCESS",
                    "startedAt": "2026-08-16T03:00:00Z",
                },
            ],
        }
    ).build_status_at,
    int(tc._parse_iso8601("2026-08-16T03:00:00Z")),
)

# --- off by default -------------------------------------------------------------------------------
ages = {1: 1, 2: 5, 3: 600}
check("both throttles off -> the queue is untouched", throttled([1, 2, 3], ages, Opts()), [1, 2, 3])

# --- --review-min-queue ---------------------------------------------------------------------------
check("queue below the minimum -> no review at all", throttled([1, 2], ages, Opts(min_queue=3)), [])
check("queue exactly at the minimum -> reviews", throttled([1, 2, 3], ages, Opts(min_queue=3)), [1, 2, 3])
check("queue above the minimum -> reviews", throttled([1, 2, 3], ages, Opts(min_queue=2)), [1, 2, 3])
check("empty queue stays empty", throttled([], {}, Opts(min_queue=1)), [])

# --- --review-min-age -----------------------------------------------------------------------------
check("younger than the minimum age -> skipped", throttled([1, 2, 3], ages, Opts(min_age=60)), [3])
check("exactly at the minimum age -> reviewed", throttled([3], {3: 60}, Opts(min_age=60)), [3])
check("all too young -> nothing to review", throttled([1, 2], ages, Opts(min_age=60)), [])
# Fail-open: an unknown waiting time must not strand a PR forever.
check("unknown waiting time -> reviewed anyway", throttled([1, 4], {1: 1, 4: None}, Opts(min_age=60)), [4])

# --- the two together -----------------------------------------------------------------------------
# The queue depth is measured BEFORE the age filter: three PRs ARE awaiting review, so a min-queue of
# 3 is satisfied and the one old enough is reviewed.
check("queue counted before the age filter", throttled([1, 2, 3], ages, Opts(min_queue=3, min_age=60)), [3])
check("queue gate wins when it is not met", throttled([1, 3], ages, Opts(min_queue=3, min_age=60)), [])

# --- no other stage is touched --------------------------------------------------------------------
sv = survey_with([1, 2], minutes_ago=ages)
sv.needs_fix.actionable.append(tc.Candidate(1, "head1", "blocking review at head"))
sv.red_ci.actionable.append(tc.Candidate(2, "head2", "build failed at head"))
sv.reviewable.suppressed.append(tc.Candidate(9, "head9", "suppressed"))
tc.throttle_review(sv, Opts(min_queue=5, min_age=999), now=NOW)
check("review queue emptied", [c.pr for c in sv.reviewable.actionable], [])
check("fix untouched", [c.pr for c in sv.needs_fix.actionable], [1])
check("fix-ci untouched", [c.pr for c in sv.red_ci.actionable], [2])
check("suppressed review candidates untouched", [c.pr for c in sv.reviewable.suppressed], [9])

# --- the flag/env resolver ------------------------------------------------------------------------
os.environ.pop("TC_TEST_THROTTLE", None)
check("unset -> 0 (off)", tc.resolve_review_throttle(None, "TC_TEST_THROTTLE", "--f"), 0)
check("the flag wins over the environment", tc.resolve_review_throttle(7, "TC_TEST_THROTTLE", "--f"), 7)
os.environ["TC_TEST_THROTTLE"] = "4"
check("environment used when the flag is absent", tc.resolve_review_throttle(None, "TC_TEST_THROTTLE", "--f"), 4)
check("the flag still wins", tc.resolve_review_throttle(0, "TC_TEST_THROTTLE", "--f"), 0)
os.environ["TC_TEST_THROTTLE"] = "  "
check("blank environment -> 0 (off)", tc.resolve_review_throttle(None, "TC_TEST_THROTTLE", "--f"), 0)


def raises_die(cli_value, env_value):
    if env_value is None:
        os.environ.pop("TC_TEST_THROTTLE", None)
    else:
        os.environ["TC_TEST_THROTTLE"] = env_value
    try:
        tc.resolve_review_throttle(cli_value, "TC_TEST_THROTTLE", "--f")
    except tc.Die:
        return True
    return False


check("a malformed environment value fails loudly", raises_die(None, "soon"), True)
check("a negative environment value fails loudly", raises_die(None, "-1"), True)
check("a negative flag value fails loudly", raises_die(-3, None), True)

# --- the flags exist, and are undocumented --------------------------------------------------------
# add_work_flags is what populates both `work` and the internal `_round`, so probing it covers both.
probe = argparse.ArgumentParser(prog="probe")
tc.add_work_flags(probe)
check("hidden from --help", "review-min" in probe.format_help(), False)
args = probe.parse_args(["--review-min-queue", "2", "--review-min-age", "30"])
check("--review-min-queue parses", args.review_min_queue, 2)
check("--review-min-age parses", args.review_min_age, 30)
check(
    "absent flags default to None (the environment may still supply them)", probe.parse_args([]).review_min_queue, None
)

# The docs must not mention them: an undocumented flag is one we can retire without a deprecation.
names = ("review-min-queue", "review-min-age", "TAUCETI_REVIEW_MIN")
docs = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md")), REPO / "workers.toml.example"]
mentioned = sorted(p.name for p in docs if p.exists() and any(n in p.read_text() for n in names))
check("no user-facing doc mentions them", mentioned, [])

# Sanity: the age filter uses wall-clock seconds, so a real `now` behaves like the injected one.
sv = survey_with([1], minutes_ago={1: 0})
sv.open_prs[0] = dataclasses.replace(sv.open_prs[0], build_status_at=int(time.time()))
tc.throttle_review(sv, Opts(min_age=10))
check("a just-green PR is skipped with the real clock", [c.pr for c in sv.reviewable.actionable], [])

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
