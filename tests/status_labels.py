#!/usr/bin/env python3
"""Regression guard for the survey's status-label breakdown — the per-round "open PRs" line.

The line replaced a bare non-draft / build-green count with totals bucketed by TauCeti's status labels
(the STATUS_LABELS pipeline), each paired with the subset the worker itself authored. This locks in:
  - every STATUS_LABELS bucket is present and in that fixed order (a zero bucket is still listed);
  - a bucket's total counts open NON-DRAFT PRs carrying that label; drafts never count;
  - `mine` counts only the subset authored by the passed-in identity;
  - the formatted line spells out 'mine' on the first entry and pairs (total, mine) for every entry;
  - the backlog counts a worker publishes to its runtime status count a PR carrying both
    author-action labels once.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker.survey import Survey, bucket_status_labels

ME = "kim-em"
PEER = "someone-else"


def bucket(prinfos, me):
    # Exercise the SAME function survey() uses, so a regression in the production bucketing fails here.
    nondraft = [p for p in prinfos if not p.is_draft]
    return bucket_status_labels(nondraft, me)


def prinfo(number, author, labels, *, draft=False):
    return tc.PRInfo.from_json(
        {
            "number": number,
            "author": {"login": author},
            "isDraft": draft,
            "labels": [{"name": n} for n in labels],
            "headRepositoryOwner": {"login": tc.TAUCETI_OWNER},
            "statusCheckRollup": [],
        }
    )


def main():
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(f"[{'OK ' if cond else 'XX '}] {msg}")
        if not cond:
            fails += 1

    prs = [
        prinfo(1, ME, ["awaiting-author", "roadmap/PDE"]),
        prinfo(2, PEER, ["awaiting-author"]),
        prinfo(3, ME, ["awaiting-CI"]),
        prinfo(4, PEER, ["ready-to-merge"]),
        prinfo(5, ME, ["awaiting-author"], draft=True),  # draft: excluded from every bucket
        prinfo(6, ME, ["enhancement"]),  # a non-status label: in no bucket
    ]
    sl, unlabeled = bucket(prs, ME)
    counts = {label: (total, mine) for label, total, mine in sl}

    # Order and completeness: exactly STATUS_LABELS, in that order.
    check([label for label, *_ in sl] == list(tc.STATUS_LABELS), "all STATUS_LABELS present in fixed order")

    # Totals ignore drafts; PR #5 (draft awaiting-author) must not be counted.
    check(counts["awaiting-author"] == (2, 1), "awaiting-author: 2 total, 1 mine (draft excluded)")
    check(counts["awaiting-CI"] == (1, 1), "awaiting-CI: 1 total, 1 mine")
    check(counts["ready-to-merge"] == (1, 0), "ready-to-merge: 1 total, 0 mine (peer-authored)")
    check(counts["awaiting-review"] == (0, 0), "awaiting-review: zero bucket still present")
    check(counts["review-in-progress"] == (0, 0), "review-in-progress: zero bucket still present")

    # PR #6 (enhancement only) carries no status label; PR #5 is a draft and excluded entirely.
    check(unlabeled == 1, "unlabeled: 1 (non-status label only; draft excluded)")

    # Formatter: 'mine' spelled out first, then bare parens; total precedes each label; unlabeled tail.
    sv = Survey(worker_id=ME)
    sv.status_labels, sv.n_status_unlabeled = sl, unlabeled
    line = sv.status_label_line()
    check(line.startswith("1 awaiting-CI (1 mine), "), f"first entry spells out 'mine': {line!r}")
    check("2 awaiting-author (1), " in line, "later entry pairs (total, mine) without 'mine' word")
    check("0 review-in-progress (0)" in line, "zero bucket rendered in the line")
    check(line.endswith("1 unlabeled"), "nonzero unlabeled tail is appended")

    # A zero unlabeled count is omitted entirely (no noisy '0 unlabeled').
    sv0 = Survey(worker_id=ME)
    sv0.status_labels, sv0.n_status_unlabeled = sl, 0
    check("unlabeled" not in sv0.status_label_line(), "zero unlabeled is omitted from the line")

    # 'mine' is identity-relative: from the peer's view the mine counts flip.
    sl_peer, _ = bucket(prs, PEER)
    peer_counts = {label: (total, mine) for label, total, mine in sl_peer}
    check(peer_counts["awaiting-author"] == (2, 1), "peer view: awaiting-author 2 total, 1 mine (PR #2)")
    check(peer_counts["awaiting-CI"] == (1, 0), "peer view: awaiting-CI 1 total, 0 mine")

    backlog = Survey(worker_id=ME)
    backlog.n_mine_open = 7
    backlog._mine_open_prs = [
        prinfo(1, ME, ["awaiting-author", "roadmap/PDE"]),
        prinfo(2, ME, ["ci-failed"]),
        prinfo(3, ME, ["ci-failed", "awaiting-author"]),
        prinfo(4, ME, ["awaiting-review"]),
    ]
    check(
        backlog.backlog_counts() == {"mine_open": 7, "mine_awaiting_author": 2, "mine_needs_fix": 3},
        "backlog: authoring count passed through; awaiting-author plus ci-failed is one PR to fix",
    )

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} assertion failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
