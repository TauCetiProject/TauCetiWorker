## Durable PR outcomes: this run is unattended

Nobody reads your final transcript to decide what happens next. Before ending, leave the
result on GitHub: a verified repair pushed to this PR, a justified closure, or an evidence-based reply to a review finding. A recommendation in your final answer alone does not complete maintenance.

### Fully superseded PR
__CLOSE_AUTHORITY__

When current main has already merged ALL useful content of this PR, verify that fact
against the actual source. Identify the merged replacement PR(s), the main commit inspected,
and a per-declaration mapping from this PR to the definitions/theorems and files on main.
Verify each replacement through its commit on main, not through a PR number alone. Find the
replacement commit and confirm it is an ancestor of the inspected main
(`git merge-base --is-ancestor <commit> <main-SHA>`). A `#N` in a commit subject is usually a
Forgejo PR number: when the commit has a `Source-PR: forgejo:...#N` trailer, the replacement is
Forgejo PR N, and GitHub PR N is an unrelated PR, so do not look it up with `gh pr view N`. Only a
GitHub PR whose merge commit is on main (`gh pr view N --repo TauCetiProject/TauCeti --json
state,mergeCommit`) is a GitHub replacement. A matching title, an agent's previous assertion,
a textual conflict, or overlapping roadmap targets are not sufficient. If any useful
additional result remains, preserve and repair it in this PR; do not close merely because much
overlaps. Remove duplicates and adapt the useful remainder to main. Do not abandon useful
existing content as a "different PR" just because the original headline result has landed.

Immediately before changing PR state, re-read PR #__PR__ in TauCetiProject/TauCeti. It
must still be open at starting head `__HEAD__`, authored by an account the close authority above allows,
and have no `keep`/`hold`/`wip`/`human`/`on-hold`/`do-not-close` label. Honor any narrower authorization
above. If the head moved or a hold appeared, leave it untouched for a fresh selection.

If fully superseded and closure is authorized, write the evidence to a temporary file. Include
`<!--tauceti-superseded:v2 head=__HEAD__ main=<full-main-SHA> by=<forge>#<N>@<replacement-SHA>-->`,
where `<forge>` is `forgejo` (from the commit's `Source-PR: forgejo:` trailer) or `github`, and
`<replacement-SHA>` is the full SHA of the replacement commit on main. Several replacements are
separated by commas, for example `by=forgejo#114@<SHA>,forgejo#143@<SHA>`. A replacement commit
with no PR is written `by=commit@<SHA>`. Link a Forgejo PR as
`https://git.taucetiproject.org/TauCetiProject/TauCeti/pulls/<N>`, never as GitHub `#N`.

Post the evidence first, then confirm it is visible, and only then close:
```
if gh pr comment __PR__ --repo TauCetiProject/TauCeti --body-file <evidence-file> &&
  [ "$(gh pr view __PR__ --repo TauCetiProject/TauCeti --json comments --jq '[.comments[].body | select(contains("tauceti-superseded:v2 head=__HEAD__"))] | length')" -ge 1 ]; then
  gh pr close __PR__ --repo TauCetiProject/TauCeti
else
  echo "Evidence comment not visible on PR __PR__; leaving it open"
fi
```
Run `gh pr close` only when `gh pr comment` exited 0 and the re-read finds the marker for
`__HEAD__`. If the comment command fails, exits 77 or prints `NOT confirmed as posted`, or the re-read
does not find the marker, treat the comment as not on GitHub: do not retry it and do not close. Leave the PR
open, and say in your final answer that the closure is pending because its evidence comment was
not posted; a later round finishes both steps together. Verify the close succeeded. Do not delete
the branch, manufacture a commit, clear Bors requests, or ask a maintainer to perform this closure
for you. Stop once the posted evidence comment and the closure are both confirmed; build/push
requirements apply to repairs, not to a verified supersession closure.

### Resolve review disagreements

Use the current review rubric, not an obsolete finding's description of the rules. Repair the
PR, narrow or split its scope, or answer a mistaken finding with concrete source and rubric
evidence through the existing review process. A roadmap restriction is a scope constraint:
choose work supported by the existing roadmaps; do not change roadmaps.

Continue repairs or the existing evidence-based review response process within the bounded
attempts. When instructed that this is the final resolution attempt, repair and push or close
your own PR with a concise explanation of the unresolved finding and what a fresh attempt should
do differently. A finding that only the PR description can answer is repaired by updating the
description and replying on its thread. Preserve the branch. Re-read the live author, starting head and hold labels before
commenting and closing, using the same checks above. Close only after re-reading the PR and seeing
your explanation comment posted, as above, then confirm closure succeeded.
The closure authority above applies here too; never close somebody else's PR or a specially
scoped automatic bump.

Transient provider/GitHub failures and lost claims use the existing retry handling. A withdrawn request, changed head or lost claim needs no repetitive comment: stop
safely; the changed shared state already routes future work.
