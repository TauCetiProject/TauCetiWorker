#!/usr/bin/env python3
"""A provider outage must not spend a PR's fix budget.

TauCetiProject/TauCeti#1434 was retired as "fix attempts are spent (3/3) — needs a human" after three
consecutive fix rounds died to `API Error: 529 Overloaded`. The agent never attempted the fix once, so
all three attempts were charged for a failure that had nothing to do with that PR. Rounds 55, 56 and 58
of one day's log, one PR, permanently flagged for a human because the provider was busy.

Two halves are checked here: the classifier only calls a failure transient when it really is one (a
dead credential must stay charged and stay loud), and the refund hands the counters back and pauses the
round rather than letting the loop march on. Exit 0 = every case agrees; 1 = a mismatch.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

agents = tc.agents
wu = tc.work_units
NoProgress = tc.config.NoProgress
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


# --- classification -------------------------------------------------------------------------------
# The 529 wording is Jeremy Kahn's report; the 401 wording is observed verbatim in a real agent log
# (logs/roadmap/agent-claude-20260620-091645.log), where that single line was the entire file.
cases = [
    ("529 overloaded", "API Error: 529 Overloaded", "provider returned 529"),
    ("503 unavailable", "API Error: 503 Service Unavailable", "provider returned 503"),
    ("429 rate limit", "API Error: 429 Too Many Requests", "provider returned 429"),
    (
        "Claude session limit",
        "You've hit your session limit · resets 9:30pm (UTC)",
        "provider subscription limit reached",
    ),
    ("Claude weekly limit", "You've hit your weekly limit", "provider subscription limit reached"),
    ("Codex usage limit", "You've hit your usage limit. Try again later.", "provider subscription limit reached"),
    ("Claude usage limit", "Claude AI usage limit reached", "provider subscription limit reached"),
    ("quoted usage limit", "The test asserts: You've hit your usage limit", None),
    ("500 server error", "API Error: 500 Internal Server Error", "provider returned 500"),
    ("401 stays charged", "Failed to authenticate. API Error: 401 Invalid authentication credentials", None),
    ("403 stays charged", "API Error: 403 Forbidden", None),
    ("400 stays charged", "API Error: 400 Bad Request", None),
    ("404 stays charged", "API Error: 404 model not found", None),
    ("transport reset", "Error: read ECONNRESET", "could not reach the provider"),
    ("socket hang up", "request failed: socket hang up", "could not reach the provider"),
    ("ordinary failure", "error: build failed, 3 errors in Foo.lean", None),
    ("empty log", "", None),
    # A parsed non-transient status wins over any transport word later in the same tail: a 401 that
    # happens to mention a socket is still a 401.
    ("401 beats transport", "API Error: 401 Invalid credentials\nsocket hang up", None),
    ("short preamble still counts", "warming up\nAPI Error: 529 Overloaded", "provider returned 529"),
    # The LAST status is the outcome. Taking the first would refund a run that saw a 529, retried past
    # it, and then died of a dead credential, and would charge the reverse.
    ("last status wins, transient", "API Error: 401 stale\nAPI Error: 529 Overloaded", "provider returned 529"),
    ("last status wins, terminal", "API Error: 529 Overloaded\nAPI Error: 401 Invalid", None),
    # A refund asserts the agent never ran. A transcript means it did, so a quoted status inside real
    # work is a task failure: a recovered network flake, a test asserting on an error string, the
    # agent reading a log. These are the false positives that would let a stuck PR retry for ever.
    (
        "agent worked, then failed",
        "\n".join(
            [
                "fetching mathlib cache",
                "warning: transient failure: API Error: 503, retrying",
                "cache ok",
                "building TauCeti.Analysis",
                "error: TauCeti/Foo.lean:12:0: unknown identifier",
                "build failed with 1 error",
                "agent: giving up",
            ]
        ),
        None,
    ),
    (
        "test output quoting a status",
        "\n".join(
            [
                "running tests",
                "FAIL test_retry: expected 'API Error: 529 Overloaded'",
                "AssertionError",
                "1 failed",
                "5 passed",
                "6 tests run",
                "exit 1",
            ]
        ),
        None,
    ),
    ("colon is required", "API Error529 Overloaded", None),
]
for name, text, want in cases:
    check(f"classify {name}", agents.classify_agent_failure(text), want)

check("bare number is not a status", agents.classify_agent_failure("exit 529"), None)


# --- refund ---------------------------------------------------------------------------------------
def fresh(state):
    """A Worker stub with just the pieces _refund_infra_failure touches."""
    return SimpleNamespace(counters=wu.Counters(SimpleNamespace(state=Path(state))))


CAND = SimpleNamespace(pr=1434, head="a" * 40)
KEYS = ("fix-1434-aaaaaaaaaaaa",)

with tempfile.TemporaryDirectory() as state:
    w = fresh(state)
    w.counters.write(KEYS[0], 1)

    # An agent-side failure is charged exactly as before: no refund, no pause.
    agents._LAST_AGENT_FAILURE = None
    wu._refund_infra_failure(w, CAND, "fix", KEYS)
    check("agent failure stays charged", w.counters.read(KEYS[0]), 1)

    # A provider outage hands the attempt back and pauses the round.
    agents._LAST_AGENT_FAILURE = "provider returned 529"
    raised = None
    try:
        wu._refund_infra_failure(w, CAND, "fix", KEYS)
    except NoProgress as e:
        raised = str(e)
    check("529 refunds the attempt", w.counters.read(KEYS[0]), 0)
    check("529 pauses the round", bool(raised and "not charged" in raised), True)

    # One observed failure grants exactly one refund. Without the read-and-clear accessor a second
    # caller could spend the same failure again, which is how the earlier version double-counted.
    w.counters.write(KEYS[0], 1)
    wu._refund_infra_failure(w, CAND, "fix", KEYS)
    check("one failure, one refund", w.counters.read(KEYS[0]), 1)

    # The refund never drives a counter negative (a stale counter file must not become a credit).
    w.counters.write(KEYS[0], 0)
    agents._LAST_AGENT_FAILURE = "provider returned 529"
    floored = False
    try:
        wu._refund_infra_failure(w, CAND, "fix", KEYS)
    except NoProgress:
        floored = True
    check("refund floors at zero", w.counters.read(KEYS[0]), 0)
    check("still pauses", floored, True)

# The cap stops a misclassified persistent failure from retrying for ever.
with tempfile.TemporaryDirectory() as state:
    w = fresh(state)
    agents._LAST_AGENT_FAILURE = "provider returned 529"
    charged_again = False
    for i in range(wu.MAX_INFRA_REFUNDS + 1):
        w.counters.write(KEYS[0], 3)
        agents._LAST_AGENT_FAILURE = "provider returned 529"
        # A new head each round: the cap is keyed on the PR precisely so pushing cannot reset it
        # while the per-PR lifetime counters keep being handed back.
        moved = SimpleNamespace(pr=CAND.pr, head=f"{i:040x}")
        try:
            wu._refund_infra_failure(w, moved, "fix", KEYS)
        except NoProgress:
            continue
        charged_again = True  # returned without raising ⇒ the cap fired
    check("cap survives a moving head", charged_again, True)
    check("cap leaves the attempt charged", w.counters.read(KEYS[0]), 3)

agents._LAST_AGENT_FAILURE = None
print("\nFAIL" if fails else "\nall infra-failure budget checks passed")
sys.exit(1 if fails else 0)
