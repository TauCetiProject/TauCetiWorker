## Durable PR outcomes: this run is unattended

Nobody reads your final transcript to decide what happens next. Before ending, leave the
result on GitHub: a verified repair pushed to this PR, a justified closure, or an explicit
blocked state. A recommendation in your final answer alone does not complete maintenance.

### Fully superseded PR
__CLOSE_AUTHORITY__

When current main has already merged ALL useful content of this PR, verify that fact
against the actual source. Identify the merged replacement PR(s), the main commit inspected,
and how each part of this PR is covered. A matching title, an agent's previous assertion,
a textual conflict, or overlapping roadmap targets are not sufficient. If any useful
additional result remains, preserve and repair it in this PR; do not close merely because much
overlaps. Remove duplicates and adapt the useful remainder to main. Do not abandon useful
existing content as a "different PR" just because the original headline result has landed.

Immediately before changing PR state, re-read PR #__PR__ in TauCetiProject/TauCeti. It
must still be open at starting head `__HEAD__`, authored by the authenticated account,
and have no `keep`/`hold`/`wip`/`human`/`on-hold`/`do-not-close` label. Honor any narrower authorization
above. If the head moved or a hold appeared, leave it untouched for a fresh selection.

If fully superseded and closure is authorized, write the evidence to a temporary file and:
```
gh pr comment __PR__ --repo TauCetiProject/TauCeti --body-file <evidence-file>
gh pr close __PR__ --repo TauCetiProject/TauCeti
```
Verify the close succeeded. Do not delete the branch, manufacture a commit, clear Bors
requests, or ask a maintainer to perform this closure for you. Stop once the closure is
confirmed; build/push requirements apply to repairs, not to a verified supersession closure.

### A real blocker requiring human attention
If a substantive decision or permission outside your task is essential, post a PR comment
with the exact starting head, diagnosis, evidence, any useful remaining work, and the precise
decision required. Then add the existing `on-hold` label with:
```
gh pr edit __PR__ --repo TauCetiProject/TauCeti --add-label on-hold
```
Recheck the live head and pause labels before publishing; never overwrite a newer task's
state. Confirm both operations succeeded. This pauses automatic work instead of paying
another worker to rediscover the blocker. Do not apply this label for an ordinary difficult
proof, a transient provider/GitHub failure, a lost branch lease, or an expired round deadline.
Use the existing retry/lease handling for those cases. If publication fails, retain its durable
outbox intent and report failure; do not claim the task is complete.

A withdrawn request, changed head, or lost claim needs no repetitive comment: stop safely;
the changed shared state already routes future work. Never bypass a hold or close somebody
else's PR. Do not change roadmaps to resolve a blocker.
