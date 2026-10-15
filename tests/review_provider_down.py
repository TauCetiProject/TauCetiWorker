#!/usr/bin/env python3
"""A reviewer-provider outage backs the loop off; it is not charged to whichever PR was next.

TauCetiReview#117 gave the engine a distinct exit status for "I stopped because the provider is
unusable, and I posted nothing": a revoked credential, or an exhausted subscription window. Before
that the engine exited 0 having posted a scoreboard of `error` rows, so do_review took the success
path. With a real non-zero status it would otherwise fall into the ordinary failure branch and
charge `review-err-<pr>` — a per-PR, lifetime counter that only a posted review resets. Three of
them drop the PR from review candidacy (survey) and open a public "Review stuck: PR #N" issue, so an
outage lasting one quota window could strand several unrelated PRs. Review workers run
`--ignore-quota` precisely so they do not pace, which is what makes that a live risk rather than a
theoretical one.

This is the same carve-out the TauCetiData publish failure and the host-binary preflight already
make, in the same words: machine-wide, so warn loudly and raise NoProgress rather than bump a
per-PR counter.

Pinned here:

  1. The provider-down status raises NoProgress, charges nothing, and records no per-PR failure.
  2. An ordinary engine failure is unchanged: charged, recorded, returned.
  3. A successful review is unchanged: the counter is cleared.
  4. classify_failure calls an exhausted plan `provider-unavailable`, so the public stuck-issue text
     says the provider was unavailable rather than "review command failed".

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker.constants import MAX_REVIEW_ERRORS, REVIEW_PROVIDER_DOWN_EXIT
from tauceti_worker.review_diagnostics import classify_failure, read_review_failure

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


class Counters:
    def __init__(self):
        self.v = {}

    def read(self, k):
        return self.v.get(k, 0)

    def incr(self, k):
        self.v[k] = self.v.get(k, 0) + 1
        return self.v[k]

    def write(self, k, n):
        self.v[k] = n


def fake_worker(state):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state, wid="w", store_dir=state / "store"),
        counters=Counters(),
        rs=types.SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
        gh=types.SimpleNamespace(add_reaction=lambda i: True, remove_reaction=lambda i: True),
    )


def drive(rc, state):
    """Run do_review with the engine stubbed to return `rc`; return (worker, raised)."""
    w = fake_worker(state)
    c = types.SimpleNamespace(pr=42, head="a" * 40, contest=None, contest_reply_id=None)
    opts = types.SimpleNamespace(work_model="claude")
    sv = types.SimpleNamespace()
    orig_run, orig_sync, orig_me = tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me
    tc.work_units.run_to_logfile = lambda *a, **k: rc
    tc.work_units._sync_review_outbox = lambda *a, **k: 0
    tc.work_units.me = lambda: "tester"
    try:
        out = tc.work_units.do_review(w, sv, c, opts, bubble=False)
        return w, None, out
    except tc.config.NoProgress as e:
        return w, e, None
    finally:
        tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me = orig_run, orig_sync, orig_me


def main():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        (state / "store").mkdir()

        # 1) provider down: back off, charge nothing, record nothing.
        w, raised, _ = drive(REVIEW_PROVIDER_DOWN_EXIT, state)
        check("provider-down raises NoProgress", raised is not None)
        check("provider-down says it is not charged", "not charged" in str(raised))
        check("provider-down charges no review error", w.counters.read("review-err-42") == 0)
        check("provider-down retains no per-PR failure", not read_review_failure(state, 42))

        # 2) an ordinary engine failure keeps the existing behaviour.
        w2, raised2, out2 = drive(1, state)
        check("an ordinary failure does not raise", raised2 is None)
        check("an ordinary failure returns its status", out2 == 1)
        check("an ordinary failure is charged", w2.counters.read("review-err-42") == 1)

        # 3) a posted review still clears the streak.
        w3, raised3, out3 = drive(0, state)
        check("success does not raise", raised3 is None and out3 == 0)
        check("success clears the streak", w3.counters.read("review-err-42") == 0)

        # 4) the status is distinct from the ordinary failure statuses it must be told apart from.
        check("the carve-out status is not 0 or 1", REVIEW_PROVIDER_DOWN_EXIT not in (0, 1))
        # A run of outages must not be able to strand a PR: that is the whole point of the carve-out.
        charged = sum(
            drive(REVIEW_PROVIDER_DOWN_EXIT, state)[0].counters.read("review-err-42")
            for _ in range(MAX_REVIEW_ERRORS + 2)
        )
        check("repeated outages never reach the stuck cap", charged < MAX_REVIEW_ERRORS)

    # 5) the public category for an exhausted plan.
    cases = {
        "review aborted: the provider's subscription window is exhausted (2 consecutive "
        "`quota_exhausted` failures)": "provider-unavailable",
        "review aborted: the provider is rate limiting this account (2 consecutive `rate_limited` "
        "failures)": "provider-unavailable",
        "You've hit your session limit · resets 9:30pm (UTC)": "provider-unavailable",
        "You've hit your weekly limit · resets 3am (UTC)": "provider-unavailable",
        "review aborted: reviewer authentication failed (2 consecutive `not_authenticated` failures)": "reviewer-auth",
    }
    for line, want in cases.items():
        got = classify_failure(line)
        check(f"{want}: {line[:52]!r}", got == want)
    # Unrelated lines keep their existing classification.
    check(
        "an engine traceback is still review-engine",
        classify_failure("Traceback (most recent call last)") == "review-engine",
    )
    check("an unremarkable line is still review-command", classify_failure("done") == "review-command")

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
