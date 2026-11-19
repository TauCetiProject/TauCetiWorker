"""Server-side PR-level diff guards for admission, from the queue's own clone.

The measure is TauCetiTools' shared `ci_git.pr_guards`, the one PR CI, `lake lint` and the
staging member policy run, so a self-tested PR that skipped PR CI is held to the same
review-size and new-file caps before it can join a batch. It reads only local Git objects,
so GitHub and Forgejo PRs are measured identically, without either forge's PR totals.
Path scope is not repeated here: admission's own path policy already routes it to a human.
"""

from __future__ import annotations

from tauceti_tools.assets.scripts import ci_git

UNMEASURABLE = "could not measure the PR's diff from local Git objects"


def violations(clone_dir, merge_base: str, head: str) -> tuple[str, ...]:
    """Failed guard sentences for the exact (merge base, head) diff; empty when every guard passes.

    Unreadable statistics fail closed as a violation, never as a pass.
    """
    with ci_git.repository_reader(clone_dir) as git:
        try:
            return tuple(ci_git.pr_guards(git, merge_base, head, scope=False)["violations"])
        except ValueError:
            return (UNMEASURABLE,)
