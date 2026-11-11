# Stuck review diagnostics, 2026-09-17

The three-error stop remains unchanged. A head change does not reset it. A
classified diagnostic is evidence, not a reason to grant another attempt.

## What changed

The worker reads a bounded 128 KiB log tail and preserves a specific error over a
later generic command-failed or cleanup line. Both the worker's failure report and
its retained JSON use that diagnostic. Issues publish fixed descriptions only;
raw log text, paths, and credentials remain local. Specific public descriptions
include GitHub rate limits/permissions, unavailable reviewer models, argument-size
limits, exhausted disk space, and timeouts.

An issue containing only `review command failed` may now be upgraded to an actual
diagnosis. Equally informative peer reports do not overwrite one another. No
failure counter or retry policy changes as a result of classification.

## Evidence available without another operator's machine

[Issue #7109](https://github.com/TauCetiProject/TauCeti/issues/7109) reports three
Codex command exits on PR #7044, at 11:34:10, 11:50:25, and 14:03:07 UTC on
September 16. It contains no specific cause. During investigation the PR had no
review scoreboard. The public archive had no `records/rounds/7044`,
`records/runs/7044`, or `records/posts/7044` directory.

Input preparation was reproduced locally using TauCetiReview commit
`603b28011f779bd341d0a08e788498b81542bd7d` and exact PR head
`841813f05fbf468e6e2b0c0425fbb3ace9b9ec2e`, with a fresh scratch store,
`--no-archive`, and no `--post`. The diagnostic harness stopped at the review
subprocess boundary, before any model invocation. The following succeeded:

- GitHub PR metadata, diff, build-status, and thread-context reads;
- cloning/checking out the exact PR source;
- cloning the roadmap and fetching the PR's pinned Mathlib source;
- preparing the review workspace and engine arguments;
- starting the engine with a fresh scratch store and constructing the first
  correctness rubric prompt (27,946 bytes), stopping at the provider-call boundary.

This rules out a currently reproducible failure in those preparation stages on
this host. It does **not** prove that the original operator's credentials, macOS
process, persisted store, inference calls, or posting step worked. It is not a
successful review and changes no review verdict or worker budget.

The temporary harness and log were retained locally at
`/tmp/tauceti-7044-diagnostic/prepare.py` and
`/tmp/tauceti-7044-diagnostic/preparation.log` for this investigation. The separate
engine-startup harness and result are `engine-startup.py` and `engine-startup.log`
in the same directory. Neither harness invoked a model or posted a verdict.

Do not infer a rubric contradiction from exit 1. Historical private diagnostics
cannot be reconstructed from an issue that never published them. Upgraded workers
will retain the operative failure and publish a fixed classification if it recurs.
A further end-to-end reproduction should be one explicitly targeted scratch run,
with no posting or archival; do not repeatedly reset the failing worker's counter.
