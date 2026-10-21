## Durable PR outcomes: this run is unattended

Nobody reads your final transcript to decide what happens next. Before ending, leave the
result on GitHub: a verified repair pushed to this PR, a justified closure, or an evidence-based reply to a review finding. A recommendation in your final answer alone does not complete maintenance.

### Fully superseded PR
__CLOSE_AUTHORITY__

When current main has already merged ALL useful content of this PR, verify that fact
against the actual source. Identify the merged replacement PR(s), the main commit inspected,
and a per-declaration mapping from this PR to the definitions/theorems and files on main.
Verify each cited replacement PR is actually merged (`gh pr view N --repo TauCetiProject/TauCeti --json state,mergeCommit`). A matching title, an agent's previous assertion,
a textual conflict, or overlapping roadmap targets are not sufficient. If any useful
additional result remains, preserve and repair it in this PR; do not close merely because much
overlaps. Remove duplicates and adapt the useful remainder to main. Do not abandon useful
existing content as a "different PR" just because the original headline result has landed.

Immediately before changing PR state, re-read PR #__PR__ in TauCetiProject/TauCeti. It
must still be open at starting head `__HEAD__`, authored by the authenticated account,
and have no `keep`/`hold`/`wip`/`human`/`on-hold`/`do-not-close` label. Honor any narrower authorization
above. If the head moved or a hold appeared, leave it untouched for a fresh selection.

If fully superseded and closure is authorized, write the evidence to a temporary file. Include
`<!--tauceti-superseded:v1 head=__HEAD__ main=<full-main-SHA> by=#<replacement>-->`.
Then:
```
gh pr comment __PR__ --repo TauCetiProject/TauCeti --body-file <evidence-file>
gh pr close __PR__ --repo TauCetiProject/TauCeti
```
Verify the close succeeded. Do not delete the branch, manufacture a commit, clear Bors
requests, or ask a maintainer to perform this closure for you. Stop once the closure is
confirmed; build/push requirements apply to repairs, not to a verified supersession closure.

### Resolve review disagreements

Use the current review rubric, not an obsolete finding's description of the rules. Repair the
PR, narrow or split its scope, or answer a mistaken finding with concrete source and rubric
evidence through the existing review process. A roadmap restriction is a scope constraint:
choose work supported by the existing roadmaps; do not change roadmaps.

Continue repairs or the existing evidence-based review response process within the bounded
attempts. When instructed that this is the final resolution attempt, repair and push or close
your own PR with a concise explanation of the unresolved finding and what a fresh attempt should
do differently. Preserve the branch. Re-read the live author, starting head and hold labels before
commenting and closing, using the same checks above, then confirm closure succeeded.
The closure authority above applies here too; never close somebody else's PR or a specially
scoped automatic bump. For those repairs, preserve the existing retry/checkpoint policy.

Transient provider/GitHub failures, lost claims and deadlines use existing retry and checkpoint
handling. A withdrawn request, changed head or lost claim needs no repetitive comment: stop
safely; the changed shared state already routes future work.
