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


def large_refactor(clone_dir, head: str, read, repo: str, number: int, labels) -> bool:
    """Whether `head` of PR `number` carries a repository admin's `large-refactor` label
    (`ci_git.large_refactor_approved`), read only for a PR that carries the label. A failed
    read raises, like the self-tested reads, so admission retries the evaluation later rather
    than reporting the caps as failed."""
    if ci_git.LARGE_REFACTOR_LABEL not in {str(name).lower() for name in labels}:
        return False
    with ci_git.repository_reader(clone_dir) as git:
        return ci_git.large_refactor_approved(read, repo, number, labels, ci_git.committer_time(git, head))


def violations(clone_dir, merge_base: str, head: str, *, large_refactor: bool = False) -> tuple[str, ...]:
    """Failed guard sentences for the exact (merge base, head) diff; empty when every guard passes.

    Unreadable statistics fail closed as a violation, never as a pass. `large_refactor` lifts
    the three review-size caps, never the new-file cap.
    """
    with ci_git.repository_reader(clone_dir) as git:
        try:
            return tuple(
                ci_git.pr_guards(git, merge_base, head, scope=False, large_refactor=large_refactor)["violations"]
            )
        except ValueError:
            return (UNMEASURABLE,)
