# Admission knowledge for repair

Admission evidence helps the repair agent find interactions before CI reports them. It never
grants admission, removes a member, or supplies a CI verdict. Missing evidence is unknown.

## Compiled declaration names

The service fetches the trusted PR build's `DECLARATION-NAMES` record in bounded background
work, using its own GitHub App. Records are bound to the accepted head, with their base, tested
integration, run and job identities retained. Incomplete, stale or malformed records cannot
establish exact names. Admission and insertion keep moving while a record is unavailable.

The record describes names added by the accepted source. It does not describe names removed or
renamed, or the source after agent repair. The worker uses compiled added names for clean members
and retains the approximate diff-derived removals and renames. An edited member keeps the accepted
compiled record as historical context while its current names come from the current source scan.

## Merge-selection evidence

The insertion probe is already the queue's equivalent of Bors's sequential merge-selection pass.
When it conflicts, the input retains the tested batch, base, tip, paths and the other members
touching those paths in its admission evidence. If a later insertion succeeds on another batch,
that evidence travels into the repair task; it is an observation about the recorded attempt,
not proof that the current source still conflicts.

`admission_planning.py` reads the actual attributed commits to build file ownership, shared-path
groups and connected import components. It reads Git blobs without checking out or executing the
candidate. The import-header scan is conservative and explicitly approximate. Immutable Git
evidence is cached; blob reads are limited to 256 KiB per file and 8 MiB per load. A budget or
parsing failure marks the context incomplete and does not stop the queue. Oversized blobs are
skipped while other headers are retained. Total-byte limits advance through further passes,
and transient failures retry after a minute without requiring a source change. Task context is capped at 4,096 records, 2,048 import
edges, 262,144 compiled-name observations and 512 KiB; compiled collisions take priority when
truncation is necessary. Prompt excerpts show at most 40 records. Operational contexts are removed
when a batch lands or is removed; its journal and task evidence remain retained. Ownership and
controller bookkeeping stay out of task payloads; a truncated owner list is explicitly incomplete.

The inventory of `bors-ng` at `8021558cd` covered `lib/worker/batcher/`,
`lib/worker/declaration_names.ex`, `lib/worker/local_git.ex` and `priv/local_git.py`:

- `ConflictSelection` combines compiled declaration collisions with sequential textual replay.
- That replay also detects already-merged source. The queue already handles landed members by
  retaining their identity as empty commits.
- The assembly diagnostic pass supplies shared paths, import edges, dependency paths and missing
  imports; the queue now carries the overlap/component information into agent navigation.
- `Diversity` is explicitly an offline planner with no scheduler caller, rather than another
  production admission predicate. Bundle, queue-age, priority, configuration and capacity rules
  are scheduling policy; they do not supply another compiled source finding.

## Proactive repairs and CI

At insertion and when late declaration records arrive, exact accepted added-name intersections
become predicted collision diagnostics on the batch. Edited and empty members do not trigger
another repair from their historical accepted names. Persisted per-member path ownership makes
conflict attribution independent of the in-memory Git cache after a service restart. Shared paths and import components accompany
tasks as navigation; overlap alone does not imply a source failure.

A predicted compiled collision makes an open batch eligible for preparation without waiting for
the usual size and quiet thresholds. On a head, the controller dispatches the first CI attempt
and exposes a repair task alongside it. A collision digest bypasses preparation thresholds once
per base once a worker actually claims it, so each insertion does not trigger another speculative
build. An unclaimed offer superseded by insertion remains eligible at the new version; creating
a task is not an attempted build. The candidate build continues while the worker repairs
its private chain. CI observation and infrastructure retries continue while the agent works. A passing candidate
retires even a claimed speculative task and lands immediately. The next batch can still pre-rebase
speculatively; adoption of a changed head supersedes stale work. An adoption that
changes the tip requires the replacement candidate's own CI; the earlier result cannot approve it.

A same-tip submission resolves the speculative task without changing the version, restarting CI
or adding a comment. If CI passes before an unclaimed speculative task starts, the task is retired
and the audited candidate can land. If it fails first, its actual diagnostics join the same task and an unchanged result can no longer
resolve that source failure. Its old claim is released immediately so the real diagnostics can
be claimed without waiting for expiry, while a useful changed submission can still be adopted.
Claims, authorization, membership, append-only explanations and opt-out rules apply normally.

The full context is retained in task diagnostics and written to `.tauceti-repair/admission.json`
beside the worker's other listings. The prompt contains a concise excerpt and a link to that file.

## Measurement

The journal records `admission-predicted`, `admission-repair-started` and
`admission-repair-result`, including declaration names, involved members, task and CI-attempt
identities, submitted tip and edited members. Reporters compare the result time with the run's
actual completion time; a last-observed pending status alone cannot prove a repair preceded the
verdict. A changed contribution is not by itself proof that a predicted collision was fixed.
The worker reports duplicate names actually observed by scripted checks, the exact verified tip
and whether the final build and audits passed. A result counts as a fixed prediction only when
that duplicate was observed, its owning contribution changed, and those final checks passed.
Missing or unavailable audits remain unknown. Background results are compared with the first
subsequent CI run of that same batch; head results use the current attempt after any
infrastructure retry. None of these measurements authorizes publication without required CI.
