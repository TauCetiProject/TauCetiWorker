#!/usr/bin/env python3
"""spread_candidates randomizes the review pick so concurrent workers don't all converge on the
same lowest-numbered PR and collide on the reviewer's in-progress marker.

It must (a) preserve the candidate set exactly (never drop, duplicate, or invent a PR), and (b) actually
vary the order across workers — modelled here as two independent RNG streams (each worker is its own
process). The engine's marker remains the real de-contention backstop; this only spreads the first pick.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


cands = list(range(1, 15))  # 14 PRs, stand-ins for Candidate objects (shuffle is order-only)

# (a) Set preservation: same elements, no loss/dup, regardless of seed.
for seed in (0, 1, 7, 42):
    out = tc.spread_candidates(cands, rng=random.Random(seed))
    check(f"seed {seed}: preserves the exact candidate set", sorted(out) == cands)
    check(f"seed {seed}: returns a new list (input untouched)", cands == list(range(1, 15)))

# (b) Spread: different workers (different RNG streams) pick different first candidates most of the time.
#     Model 4 workers as 4 seeds; assert they don't all land on the same first PR.
firsts = [tc.spread_candidates(cands, rng=random.Random(s))[0] for s in range(4)]
check("4 workers don't all pick the same first PR", len(set(firsts)) > 1)

# (c) Determinism for a fixed RNG (so behavior is reproducible/testable).
a = tc.spread_candidates(cands, rng=random.Random(123))
b = tc.spread_candidates(cands, rng=random.Random(123))
check("same seed -> same order (reproducible)", a == b)

# (d) Degenerate inputs are safe.
check("empty list -> empty", tc.spread_candidates([]) == [])
check("single candidate -> unchanged", tc.spread_candidates([99]) == [99])

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
