#!/usr/bin/env python3
"""The review rubrics reach an authoring round as one readable file.

Measured over 230 rounds that opened a PR: codex named a rubric file in 90% of them, claude in 4%,
against a prompt that told both to read them. Reading eleven files is a turn of orientation every
round pays, and 79% of PRs need a second review round afterwards. So the worker concatenates the
rubrics once, per round, and the prompt points at that one path.

Pinned here:

  1. The bundle contains every angle rubric plus the shared protocol, and each is labelled with the
     file it came from.
  2. The reference documents are excluded: the engine splices those into ONE rubric's prompt, and the
     largest of them is bigger than every rubric put together.
  3. It is written OUTSIDE the review checkout. fetch_ref resets that checkout hard and cleans it at
     the start of every round, so a bundle written inside would be deleted before the agent read it.
  4. It is byte-identical between rounds that fetched the same rubrics, so a round's prompt does not
     churn for no reason.
  5. A rubrics directory that is missing or empty yields None rather than raising: the round should
     fall back to the glob, not die.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

stage_rubrics = tc.work_units.stage_rubrics
ANGLES = (
    "api-design",
    "attribution",
    "correctness",
    "documentation",
    "generality",
    "naming",
    "placement",
    "proof-quality",
    "reuse",
    "scope",
)
fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def fake_review_checkout(root: Path) -> Path:
    d = root / "review" / "rubrics"
    (d / "references").mkdir(parents=True)
    (d / "_common.md").write_text("SHARED PROTOCOL\n")
    (d / "README.md").write_text("how the rubrics work, for humans\n")
    for a in ANGLES:
        (d / f"{a}.md").write_text(f"ANGLE {a}\n")
    (d / "references" / "naming-conventions.md").write_text("REFERENCE " + "x" * 25000 + "\n")
    return root / "review"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review = fake_review_checkout(root)
        out = stage_rubrics(review, root / "rubrics")
        check("a bundle is produced", out is not None and out.is_file())
        text = out.read_text()

        # 1) everything an author is judged against, labelled by source file.
        check("the shared protocol leads", text.index("SHARED PROTOCOL") < text.index("ANGLE api-design"))
        for a in ANGLES:
            check(f"{a} is present and labelled", f"# rubrics/{a}.md" in text and f"ANGLE {a}" in text)

        # 1b) the preface says whose instructions these are. _common.md assigns its reader the role
        # of a review agent and demands a JSON verdict; an author told to read it in full, first,
        # must be told not to adopt either.
        head = text[: text.index("# rubrics/_common.md")]
        check("the preface disclaims the reviewer role", "ADDRESS THE REVIEWERS" in head)
        check("...and the verdict output format", "output is a pull request" in head)

        # 2) the reference documents are NOT inlined.
        check("the naming reference is excluded", "REFERENCE" not in text)
        check("the human README is excluded", "for humans" not in text)
        check("the bundle stays small", len(text) < 60_000)

        # 3) written outside the review checkout, which fetch_ref wipes each round.
        check("written outside the review checkout", review not in out.parents)

        # 4) deterministic.
        again = stage_rubrics(review, root / "rubrics2")
        check("byte-identical across runs", again.read_text() == text)

        # 5) degrade to None, never raise.
        (root / "empty").mkdir()
        check("a missing rubrics dir yields None", stage_rubrics(root / "empty", root / "o1") is None)
        (root / "bare" / "rubrics").mkdir(parents=True)
        check("an empty rubrics dir yields None", stage_rubrics(root / "bare", root / "o2") is None)

    # Against the REAL rubrics, if a worker has a review checkout staged: the bundle must cover every
    # angle the engine actually runs, or the prompt would promise an author something it does not get.
    real = next((p for p in Path(REPO / "state").glob("*/refs/review/rubrics") if p.is_dir()), None)
    if real is None:
        print("[SKIP] no staged review checkout to check the real rubrics against")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            out = stage_rubrics(real.parent, Path(tmp))
            text = out.read_text()
            missing = [
                p.name for p in real.glob("*.md") if p.name not in ("README.md",) and f"# rubrics/{p.name}" not in text
            ]
            check(f"every real rubric is bundled (missing: {missing})", not missing)
            print(f"       bundle is {len(text) // 1024} KB (~{len(text) // 4000}k tokens)")

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
