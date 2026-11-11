#!/usr/bin/env python3
"""Regression guard for roadmap-scoped authoring backpressure."""

import importlib
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

survey_module = importlib.import_module("tauceti_worker.survey")


def pr(number, *labels, body="", head_ref=""):
    return tc.PRInfo.from_json(
        {
            "number": number,
            "body": body,
            "headRefName": head_ref,
            "labels": [{"name": label} for label in labels],
            "headRepositoryOwner": {"login": tc.TAUCETI_OWNER},
            "statusCheckRollup": [],
        }
    )


def main():
    fails = 0

    def check(message, actual, expected):
        nonlocal fails
        ok = actual == expected
        print(f"[{'OK ' if ok else 'XX '}] {message}: {actual!r} (want {expected!r})")
        fails += not ok

    prs = [
        pr(1, "roadmap/Topology"),
        pr(2, "roadmap/Topology", "awaiting-review"),
        pr(3, "roadmap/PDE"),
        pr(4, "roadmap/Algebra"),
        pr(5, "enhancement"),  # our non-roadmap PR does not consume roadmap capacity
        pr(6, "roadmap/Topology", "roadmap/PDE"),  # counted once in any/all scopes
        pr(7, "roadmap/"),  # malformed empty area is not a roadmap scope
    ]

    check("one pinned roadmap counts only that exact area", tc.roadmap_open_count(prs, "Topology", []), 3)
    check("another pinned roadmap has its independent count", tc.roadmap_open_count(prs, "PDE", []), 2)
    check("area matching is exact and case-sensitive", tc.roadmap_open_count(prs, "topology", []), 0)
    check("all areas count roadmap PRs once", tc.roadmap_open_count(prs, "any", []), 5)
    check("auto has the same eligible-area scope", tc.roadmap_open_count(prs, "auto", []), 5)
    check(
        "skips narrow the all-areas scope",
        tc.roadmap_open_count(prs, "any", ["Topology", "Algebra"]),
        2,
    )
    check(
        "a multiply-labelled PR counts if any selected area remains",
        tc.roadmap_open_count(prs, "auto", ["Topology"]),
        3,
    )
    check(
        "explicit only overrides an overlapping skip",
        tc.roadmap_open_count(prs, "Topology", ["Topology"]),
        3,
    )
    marker = '<!--tauceti-target:v1 {"focus":"Topology","id":"target"}-->'
    check("target marker covers asynchronous label lag", tc.roadmap_open_count([pr(8, body=marker)], "Topology", []), 1)
    check("target marker remains scoped", tc.roadmap_open_count([pr(8, body=marker)], "PDE", []), 0)
    any_marker = '<!--tauceti-target:v1 {"focus":"any","id":"target"}-->'
    check(
        "all-areas marker defers to its derived area label",
        tc.roadmap_open_count([pr(12, "roadmap/Topology", body=any_marker)], "Topology", []),
        1,
    )
    check(
        "derived area label wins over a stale marker focus",
        tc.roadmap_open_count([pr(13, "roadmap/PDE", body=marker)], "Topology", []),
        0,
    )
    id_only_marker = '<!--tauceti-target:v1 {"id":"target"}-->'
    check(
        "focus-less marker on a roadmap branch stays fail-closed",
        tc.roadmap_open_count([pr(14, body=id_only_marker, head_ref="roadmap/target")], "Topology", []),
        1,
    )
    check("roadmap/none is not authoring pressure", tc.roadmap_open_count([pr(9, "roadmap/none")], "any", []), 0)
    check(
        "roadmap/Unknown counts conservatively in a pinned scope",
        tc.roadmap_open_count([pr(10, "roadmap/Unknown")], "Topology", []),
        1,
    )
    check(
        "an unlabeled roadmap branch counts conservatively",
        tc.roadmap_open_count([pr(11, head_ref="roadmap/new-target")], "PDE", []),
        1,
    )

    # Exercise survey() as well as the pure scope counter: only our non-draft PRs feed it, and the
    # scoped count is what drives the threshold boolean used by run_round and the dashboard.
    raw = []
    for number in range(1, tc.MAX_OPEN_PRS + 1):
        raw.append(
            {
                "number": number,
                "author": {"login": "me"},
                "labels": [{"name": "roadmap/Topology"}],
                "statusCheckRollup": [],
            }
        )
    raw += [
        {
            "number": 20,
            "author": {"login": "me"},
            "labels": [{"name": "roadmap/PDE"}],
            "statusCheckRollup": [],
        },
        {
            "number": 21,
            "author": {"login": "peer"},
            "labels": [{"name": "roadmap/Topology"}],
            "statusCheckRollup": [],
        },
        {
            "number": 22,
            "author": {"login": "me"},
            "isDraft": True,
            "labels": [{"name": "roadmap/Topology"}],
            "statusCheckRollup": [],
        },
        {
            "number": 23,
            "author": {"login": "me"},
            "labels": [{"name": "enhancement"}],
            "statusCheckRollup": [],
        },
    ]

    class FakeGH:
        def open_prs(self):
            return raw

    class FakeCounters:
        def read(self, name):
            return 0

    old_me = survey_module.me
    old_only = os.environ.get("TAUCETI_ROADMAP_ONLY")
    old_skip = os.environ.get("TAUCETI_ROADMAP_SKIP")
    survey_module.me = lambda: "me"
    try:
        os.environ["TAUCETI_ROADMAP_ONLY"] = "Topology"
        os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        sv = survey_module.survey(types.SimpleNamespace(wid="test"), FakeGH(), None, FakeCounters(), deep=False)
        check("survey reaches backpressure at the pinned area's limit", sv.n_mine_open, tc.MAX_OPEN_PRS)
        check("the scoped survey count drives backpressure", sv.roadmap_backpressure, True)

        os.environ["TAUCETI_ROADMAP_ONLY"] = "PDE"
        sv = survey_module.survey(types.SimpleNamespace(wid="test"), FakeGH(), None, FakeCounters(), deep=False)
        check("peer, draft, non-roadmap, and other-area PRs stay out", sv.n_mine_open, 1)
        check("another area's low count does not backpressure", sv.roadmap_backpressure, False)

        # The dashboard changes these fields in place; rescoping must update both displayed values
        # immediately instead of leaving the previous area's count around until its next refresh.
        sv.roadmap_only = "Topology"
        sv.rescope_roadmap()
        check("live rescope recomputes the selected area's count", sv.n_mine_open, tc.MAX_OPEN_PRS)
        check("live rescope recomputes the backpressure flag", sv.roadmap_backpressure, True)

        os.environ["TAUCETI_ROADMAP_ONLY"] = ""
        os.environ["TAUCETI_ROADMAP_SKIP"] = "Topology"
        sv = survey_module.survey(types.SimpleNamespace(wid="test"), FakeGH(), None, FakeCounters(), deep=False)
        check("survey wires roadmap-skip into an all-areas count", sv.n_mine_open, 1)
    finally:
        survey_module.me = old_me
        if old_only is None:
            os.environ.pop("TAUCETI_ROADMAP_ONLY", None)
        else:
            os.environ["TAUCETI_ROADMAP_ONLY"] = old_only
        if old_skip is None:
            os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
        else:
            os.environ["TAUCETI_ROADMAP_SKIP"] = old_skip

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} assertion failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
