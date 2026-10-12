# Mathlib overlap commissioning — October 12, 2026

Current default: **160 Mathlib PR commits per scan**, revised after the managed
catch-up experiments below. The measured profile is Codex `gpt-6.1-sol` at high
effort.

These are consecutive live scans from Tau Ceti's first Mathlib pin, using
Codex `gpt-6.1-sol` at high effort. Each supplied commit appears in the completed
structured result, and each cursor update was accepted by the shared claim
repository. I inspected the conversation logs, including patch examination,
statement comparisons, search behavior, and final results.

| Run | Commits | Patch bytes | Minutes | Uncached input tokens¹ | Output tokens | Reported compactions | Findings | Transcript bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 20 | 99,639 | 4.6 | 173,916 | 8,157 | 0 | 1 | 644,434 |
| 2 | 10 | 36,201 | 5.0 | 105,110 | 8,692 | 0 | 1 | 313,234 |
| 3 | 40 | 221,124 | 5.7 | 168,756 | 8,311 | 0 | 0 | 452,738 |
| 4 | 60 | 313,360 | 8.1 | 197,732 | 12,784 | 0 | 0 | 620,738 |
| 5 | 40 | 200,836 | 5.4 | 147,760 | 8,984 | 0 | 0 | 416,849 |

¹ Provider totals accumulated over tool turns; these are not peak context size.
The batches cover different topics, and the prompt improved between runs, so
these are operational samples rather than a controlled benchmark. Zero reported
compactions does not rule out unreported internal compaction.

## Conversation observations

The first two runs found actionable overlaps but spent too much output on broad
search results and complete source files. The prompt now asks for topic-scoped
file discovery, statements before proofs, and bounded output. The fourth run
still extracted generic words and unqualified declaration names from diffs,
leading to irrelevant matches. The fifth run's prompt explicitly excludes
keywords and restricts searches for names such as `map`, `mono`, and `tail`.
Proof-only changes are examined without demanding a fresh semantic search when
the public statements and instances remain unchanged.

The 40- and 60-commit runs examined plausible counterparts rather than relying
on titles or name matches. For example, the fourth run compared portmanteau and
tightness statements, representation products, and meromorphic contour APIs,
and rejected them as actionable refactors after checking the current source.
The results are evidence of an investigation, not proof of exhaustive discovery.

## Issues and failure recovery

The initial scan generated
[#15215](https://github.com/TauCetiProject/TauCeti/issues/15215), adopting
`Function.Injective.starRing` for exact cyclotomic integers while preserving
computable conjugation and the embedding API. The second generated
[#15217](https://github.com/TauCetiProject/TauCeti/issues/15217), using
`IsPGroup.disjoint_of_coprime` while retaining Tau Ceti's prime-to-cardinality
interface and related results.

The first publication attempt hit GraphQL exhaustion. It left the cursor
unchanged. Publication now uses REST and reuses a locally completed result when
its cursor, pin, and contiguous range still match. The retained investigation
then published successfully without another model call. Deduplication includes
closed issues, and ambiguous mutations are reconciled rather than blindly
retried. Zero-finding scans skip the issue inventory entirely. Focused scan
rounds bypass the unrelated PR/review survey, and dedicated scan loops require
only the REST core budget. Shared histories, source snapshots,
and bounded-TTL inventory caches avoid repeating downloads across local workers.

## Repair trial

Ordinary authoring backpressure initially suppressed repair, as intended. A
one-run `--max-open-prs 30` override allowed commissioning without changing the
normal limit. The host toolchain's linker wrapper referenced a missing Nix store
path. A process-local `LEAN_CC` wrapper using system Clang and the installed
Lean libraries resolved the cache-executable build; the shared toolchain and
Mathlib pin were unchanged. Mathlib and Tau Ceti artifact downloads completed
before the repair agent launched.

The first repair produced
[PR #15228](https://github.com/TauCetiProject/TauCeti/pull/15228), targeting upstream
`main` and carrying both `Fixes #15215` and the finding marker. It replaces only
the handwritten star-ring law proofs. A direct use of the transport constructor
initially failed Lean's computability check; `fast_instance%` normalizes the
instance while retaining executable operations. A full build passed (20,007
jobs, chiefly cached restores), changed-module environment lint found no
violations, and the axiom audit checked 296 declarations including importers.
Temporary compiled `#eval` checks exercised conductors 3, 4, and 0. No public
API or additional mathematics was removed. The first agent conversation took
roughly nine minutes after cache preparation, with 94,802 uncached input tokens,
12,615 output tokens, and no reported compaction.

The trial also caught an incorrect PR-head spelling in the new prompt:
`--head owner/repository:branch` must be `--head owner:branch`. The agent recovered
through the required wrapper; the prompt and regression test now use the correct
form. Future repair prompts also avoid repeating the host's complete PR
inventory and explicitly protect executable computations and useful corollaries
with additional hypotheses. The PR remains in the normal CI/review pipeline.

The second repair produced
[PR #15230](https://github.com/TauCetiProject/TauCeti/pull/15230) for #15217. It
retains the cardinality-based corollary, with unchanged hypotheses, conclusion,
and callers, and replaces its element-order argument with Mathlib's coprime
p-group disjointness theorem. The full build passed, environment lint found no
violations, and the full-library axiom audit checked 281,422 declarations within
the allowlist. The agent conversation took about three minutes with warm caches,
65,513 uncached input tokens, 4,288 output tokens, and no reported compaction.

Both repairs reached upstream `main` PRs through the required wrappers under
their shared issue leases. At the initial commissioning checkpoint they remained in the ordinary CI/review
pipeline. The star-ring repair subsequently landed through Bors, closing its
issue; the disjointness repair remained queued at the verified landing milestone. The first
repair's [sandboxed CI build](https://github.com/TauCetiProject/TauCeti/actions/runs/38198175592)
passed; the second [build](https://github.com/TauCetiProject/TauCeti/actions/runs/38198517947)
was still running at the final commissioning check. The normal
cascade now includes `overlap-fix` then `overlap-scan`, after existing PR
maintenance and review and before new roadmap authoring. In the isolated phase
PR checkout, the standalone worker test suite passed 79 checks; the focused
overlap harness passed 33 cases covering races,
lease loss and forced worker termination, pagination, cursor coverage, publication recovery, preservation
metadata, account permissions, and phase/loop integration. A detached cancellable
agent is tied to its worker with a parent-death pipe, so even SIGKILL terminates
its tools; normal completion also sweeps residual processes in its own group.
Per-conversation telemetry files are retained alongside the latest-run alias.

The independent review found snapshot recovery bugs and an issue-label
permission gap in the isolated implementation. Snapshots now clone into unique temporary directories, validate their HEAD
and required manifest, and publish through an atomic rename; failed attempts
are cleaned and invalid legacy caches are repaired. Failed refreshes retain the
latest successful source commit and timestamp, and fail closed without prior
state. Scans now require Tau Ceti write access, validate returned issue labels
and markers, and retain issue receipts to reconcile dropped metadata without
creating another issue. Invalid scan ranges yield to automatic roadmap work
while focused scans report the error; lease cleanup preserves completed work,
and Git commit identity is subprocess-local. Regression tests exercise these
recovery paths and permission boundaries. An authenticated focused
scan dry-run in the isolated checkout recognized the shared cursor and selected
the host scan without launching a model or publishing anything.

## Initial batch size

The commissioning default was **40 first-parent commits**. Both 40-commit conversations finished
in roughly five and a half minutes without reported compaction or transcript
truncation. The 60-commit conversation also completed, but took eight minutes
and produced a larger transcript. Forty leaves headroom for more difficult
patches while amortizing launch and source-inventory costs over more work than
the provisional 20. This is an initial operating choice, not a context-limit
guarantee or a claim that 40 is optimal.

Across the five runs, 170 consecutive commits were examined and two issues
published. There were 2,739 first-parent commits still remaining through the
observed September 29 pin; the initial cursor deliberately includes the backlog.
Logs are retained in `state/overlap-calibration/overlap-runs/`, with
run directory prefixes `20261012-022004`, `023446`, `024013`, `024626`, and
`025444`. The shared cursor reached
`034a5a7a3b0e771b367ab9d544c08d4286e1fa9a` (verify against the live cursor when
resuming; other workers may advance it).

## Follow-up measurement

The default should be revisited on later, denser Mathlib history and other
providers. Inspect completion, statement comparison quality, transcript growth,
cache reuse, elapsed time, and compaction events together. A commit count alone
cannot predict semantic difficulty. The batch override remains available for
smaller or larger runs without altering the cursor protocol.

## Managed catch-up rounds

At the user's request, further consecutive scan rounds began at 03:35 UTC on
October 12. These use the same `gpt-6.1-sol` high-effort profile. Round sizes are
chosen between investigations; only one scanner runs at a time under the shared
lease. Here each commit's subject identifies a Mathlib PR, so the commit count
is also the number of PR changes investigated, including maintenance PRs.

| Run | PR commits | Patch bytes | Minutes | Uncached input tokens | Output tokens | Reported compactions | Findings | Transcript bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 6 | 80 | 969,695 | 10.5 | 263,255 | 17,788 | 0 | 2 | 802,834 |
| 7 | 100 | 1,853,149 | 8.5 | 241,786 | 14,871 | 0 | 0 | 874,947 |
| 8 | 120 | 873,412 | 10.1 | 325,473 | 15,957 | 0 | 0 | 1,089,186 |
| 9 | 160 | 1,559,297 | 9.4 | 282,252 | 14,925 | 0 | 0 | 1,024,712 |

The 80-commit run contained much larger API refactors than the commissioning
samples, including the `ValueGroup` transition and pointwise-application APIs.
It examined all 80 patches and compared relevant hypotheses. It rejected
replacing Tau Ceti's more general pointwise Lp bound with a Mathlib theorem that
requires summable norms. It also preserved the broader hypotheses of Tau Ceti's
Hopf functor-of-points group instances.

It published [#15246](https://github.com/TauCetiProject/TauCeti/issues/15246) for
Mathlib's generic pointwise sums of comodule morphisms and
[#15247](https://github.com/TauCetiProject/TauCeti/issues/15247) for using
`HopfAlgebra.antipodeAlgHomOp` in the surviving antipode-composition operation.
Post-run inspection caught wording recommending compatibility wrappers. I
corrected both issue bodies and the scanner prompt: obsolete sum lemmas should
be removed and callers migrated; useful additional operations and their API
identities should survive. The prompt-contract tests pass.

The subsequent 100-commit experiment contains 1,853,149 patch bytes, including
an 856 KB mechanical `diff` to `sdiff` rename. This is deliberately a larger
stress sample. Patch bytes alone overstate semantic difficulty for mechanical
changes; completion, investigation quality, tool output, token use, and reported
compactions are considered together. The revised sizing decision is recorded below.

The 100-commit run finished in 8.5 minutes with no actionable findings. It checked
that apparent matches for field-range equivalences, Tate cohomology, and
quotient-covering fibres already used Mathlib; Tau Ceti's connecting-map
naturality also allows changing the group, unlike the fixed-group Mathlib
statement in the batch.

I then inspected only allowlisted usage and compaction metadata in these agents'
local Codex rollout files. Native `token_count` events report a 475,000-token
context budget for these sessions. Peak request input was 268,594 tokens for the
80-commit run and 248,778 for the 100-commit run; neither rollout contains a
compaction record. These peaks are distinct from cumulative turn input totals.
Per-run `context-metrics.json` files retain the measurements without storing
private reasoning. This evidence gives substantially more headroom than the
original commissioning estimates suggested.

The first 120-commit launch was refused by soft pacing before an agent started.
The managed trial uses the existing `--ignore-quota` calibration override, which
still respects hard availability limits. No persistent worker settings changed.

The 120-commit round completed in ten minutes without findings. Its comparisons
covered generated APIs, Hopf-ideal quotients, span-commutation helpers, manifold
notation and power-series norms. Existing Tau Ceti uses or additional hypotheses
and generality explained the plausible matches. Peak request input was 326,658
of 475,000 tokens, with no native compaction. The final 160-commit trial then completed in 9.4 minutes without findings. Its
statement comparisons checked that the second derivative along a line and the
twice-differentiated metric product rule already fed Tau Ceti extensions, and
that covering-space results supplied different hypotheses or further operations.
It also accounted for 15 commits with no Lean patch. Peak request input was
285,833 tokens, with no native compaction.

## Revised batch size and catch-up checkpoint

The default is now **160 first-parent Mathlib commits**. It was the largest
completed trial and had the best observed throughput: about 17 PR commits per
minute. Its peak request input stayed at 60% of the reported context budget, and
no native compaction occurred. The 120-commit sample used more context despite
having fewer commits, reinforcing that semantic difficulty and source-search
behavior matter more than raw commit count or patch bytes alone.

| PR commits | Minutes | Peak request input tokens | Reported budget used | Native compactions |
| ---: | ---: | ---: | ---: | ---: |
| 80 | 10.5 | 268,594 | 56.5% | 0 |
| 100 | 8.5 | 248,778 | 52.4% | 0 |
| 120 | 10.1 | 326,658 | 68.8% | 0 |
| 160 | 9.4 | 285,833 | 60.2% | 0 |

All four structured results account for every supplied SHA, their range
boundaries are consecutive, and the live shared cursor was verified after the
last update. The four managed rounds examined **460 additional commits**, for
**630 overall** since the initial cursor, and published the two issues above.
At this managed catch-up checkpoint, the cursor was
`4d0f80f9459e84794a0663e59280e78742331f3b`, a June 23 Mathlib
commit. **2,279 commits remained** through the observed September 29 pin, or about
15 further batches at the new default. This task ran finite, supervised trials;
no new persistent worker was installed and no scan was left running.

The new default is an operating choice from these Codex samples, not a universal
context guarantee. Keep the override available, inspect later or unusually
complex batches, and recalibrate when changing provider/model. Native context
measurements live in each completed run's `context-metrics.json`; the local
`state/overlap-calibration/inspect-context.py --all --write` helper extracts only
allowlisted metadata for this worker's own sessions. No per-commit GitHub API
lookups were needed during the investigations: agents used cached local history
and sources, and zero-finding batches skipped issue inventory and publication.

## Full pin catch-up

At the user's request, a finite queue of consecutive 160-commit scans began at
04:57 UTC on October 12, in parallel with repair commissioning. Each invocation
uses the existing shared scan lease, rereads the cursor and Tau Ceti pin, and
stops on failure or incomplete investigation. The managed queue uses the existing
soft-pacing override; hard availability limits remain enforced. It installs no
persistent worker. All supplied SHAs, consecutive range boundaries, and native
usage metadata are checked between rounds. Observations below are updated as
each scan completes.

| Run | PR commits | Patch bytes | Minutes | Peak request input tokens | Native compactions | Findings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 160 | 1,189,233 | 12.2 | 267,114 | 0 | 1 |
| 11 | 160 | 1,301,771 | 8.5 | 335,035 | 0 | 1 |
| 12 | 160 | 1,895,937 | 11.7 | 312,266 | 0 | 0 |
| 13 | 160 | 8,501,892 | 9.9 | 261,061 | 0 | 0 |
| 14 | 160 | 840,966 | 11.8 | 369,646 | 0 | 0 |
| 15 | 160 | 1,211,457 | 11.6 | 279,519 | 0 | 0 |
| 16 | 160 | 4,440,812 | 11.6 | 255,707 | 0 | 0 |
| 17 | 160 | 2,065,799 | 14.6 | 308,075 | 0 | 1 |
| 18 | 160 | 2,549,120 | 14.4 | 305,398 | 0 | 2 |
| 19 | 160 | 1,440,161 | 16.6 | 256,711 | 0 | 1 |
| 20 | 160 | 2,143,747 | 15.8 | 266,128 | 0 | 1 |
| 21 | 160 | 1,605,215 | 17.7 | 274,751 | 0 | 2 |
| 22 | 160 | 2,353,539 | 18.0 | 221,183 | 0 | 2 |
| 23 | 160 | 5,966,548 | 15.8 | 253,628 | 0 | 3 |
| 24 | 39 | 250,656 | 6.1 | 181,253 | 0 | 0 |

Run 10 published [#15269](https://github.com/TauCetiProject/TauCeti/issues/15269),
using Mathlib's root-of-unity divisibility theorem inside Tau Ceti's useful ideal
congruence criterion. The investigation compared the stronger Mathlib domain
assumption with Tau Ceti's existing `NoZeroDivisors` signature and proposes
installing nontriviality only in the contradiction branch, preserving the public
statement. Its cursor reached `01749d4fe32e56ef026abea2c864b3552d27110f`;
2,119 commits remained through the observed pin after this round.

Run 11 published [#15273](https://github.com/TauCetiProject/TauCeti/issues/15273),
removing a private formal-group substitution certificate and migrating its three
uses to Mathlib's more general `MvPowerSeries.HasSubst.zero_X`. The agent checked
the definitional representation of `PowerSeries.X` and preserved Tau Ceti's
logarithm, exponential, and invariant-differential operations. It also checked
its declaration summary against all supplied diffs and repaired summary
omissions before finishing. The cursor reached
`9e735227e77ebee3594a868a23d1ffd55b5bfd89`; 1,959 commits remained.

Run 12 rejected apparent matches for explicit group presentations, measure
support formulas, and scalar-multiplier determinant identities after comparing
the additional Tau Ceti functionality. It checked existing canonical Hecke
imports and audited large patches and generated APIs. Its cursor reached
`b883e5758cefe396784b08a8444fbdc322737b5f`; 1,799 commits remained.

Run 13 was the largest patch-volume sample so far. Much of the 8.5 MB was
mechanical maintenance; the agent audited all large-patch hunks and declaration
sets, then compared affine geometry, going-up, and restricted-power-series
candidates. It found no actionable overlaps and used less context than the
smaller run 11. Its cursor reached
`216cd98dfcfb87e235f9f0759fe3c563f65419c1`; 1,639 commits remained.

Run 14 compared resolvent and minimal-polynomial statements, divisibility APIs,
and additional bialgebra operations. It found no actionable overlaps. Despite
its smaller patch volume, it used 77.8% of the reported context window, the
highest peak in this catch-up so far; no native compaction occurred. Its cursor
reached `53a560fefea0a9525485d49d9f1c811ed89ae6f0`; 1,479 commits remained.

Run 15 compared hyperbolic geometry, periodic-complex functions, finite
transcendence degree, and ramification candidates. The existing Tau Ceti
operations supplied distinct hypotheses or further functionality. Its cursor
reached `f46a87e8e1b7fd68c94b6e16cad91d47bcc0cc93`; 1,319 commits remained.

Run 16 checked unital normed-ring transport, quadratic discriminants and
simplicial-set APIs, including current-pin counterpart paths. Existing Tau Ceti
operations had different statements or already used the canonical API. Its
cursor reached `79cba2e8b532ff92484dfd99f45987bae8b2fd7e`;
1,159 commits remained.

Run 17 published [#15306](https://github.com/TauCetiProject/TauCeti/issues/15306),
adopting the general sesquilinear span-orthogonality membership API. It identifies
two superseded singleton/pair lemmas and callers across diagonalization,
symplectic-basis, and Witt constructions, while preserving Tau Ceti's additional
splitting, basis, and nondegeneracy results. The agent checked pairing orientation,
scalar homomorphisms, and ring/module generality. Its cursor reached
`20bc12820422504f9e52ee6caebf8182a9015336`; 999 commits remained.

Run 18 published [#15310](https://github.com/TauCetiProject/TauCeti/issues/15310)
for exact Gaussian parameter-measurability forwarding, after checking matrix
representation and measurable-space instances. It also published
[#15311](https://github.com/TauCetiProject/TauCeti/issues/15311), adopting
`Int.IsFundamentalDiscr` throughout the multiquadratic API. That finding compares
the existential and quotient/remainder characterizations for all signed
integers, and explicitly preserves prime-discriminant factorization,
Dirichlet-character construction, and further product/synthesis results.
Its cursor reached `13bd10aec8ea0ece47ff9950d2a4347680b2643b`;
839 commits remained.

Run 19 published [#15316](https://github.com/TauCetiProject/TauCeti/issues/15316),
adopting Mathlib's Fredholm index composition theorem and migrating the power,
restriction, and finite-rank-perturbation callers. It verified that the upstream
theorem needs no completeness hypothesis and has weaker ambient module/topology
assumptions, while preserving Tau Ceti's further power-index formulas. Its cursor
reached `27931b0e3deeb21a07e10cec70cd3fc4cd3758fd`; 679 commits remained.

Run 20 published [#15323](https://github.com/TauCetiProject/TauCeti/issues/15323),
identifying duplicated monic-polynomial certificates in elliptic variable-change
descent. It correctly flags narrower Mathlib assumptions: DVR, fraction field,
and unit scaling, versus Tau Ceti's integrally closed ambient extension and
nonunit coordinate results. Post-run inspection found one local minimal-model
caller whose assumptions do support the pinned Mathlib theorem; the broader
descent results and their global/nonunit consumers must remain until upstream
generalization. The original finding made that upstream work a prerequisite;
operator review corrected the issue to scope the immediate Tau Ceti repair to
`MinimalModel/Basic.lean`'s DVR/unit caller and to retain the broader local core.
Future Mathlib generalization is optional follow-up outside that repair. This
shows why partial-overlap findings need review of live callers as well as a
careful hypothesis comparison. Its cursor reached
`f15b4f161f6ff698ab91b4494faa75925856d0f5`; 519 commits remained.

Run 21 published [#15327](https://github.com/TauCetiProject/TauCeti/issues/15327),
adopting the positive-degree inflation–restriction complex and vanishing-map
lemma while preserving Tau Ceti's higher-degree exactness, injectivity, and
dimension-shifting results. It checked the successor-degree normalization and
generated component API. [#15328](https://github.com/TauCetiProject/TauCeti/issues/15328)
replaces a finite-index union frontier lemma with Mathlib's finite-set union
specialization, preserving the other boundary/image operations. Its cursor
reached `2f5a002dd3aa86da7704e089d8adb473f078d1d1`; 359 commits remained.

The first attempt at run 22 stopped after 9.7 minutes when Codex twice reported
that the selected model was at capacity. Its investigation was incomplete, so
the host published nothing and left the cursor unchanged. The queue stopped as
intended. A same-profile retry began after a brief backoff, covering the exact
same range. The failed attempt's native metadata records 183,790 uncached input,
1,982,976 cached input, and 10,452 output tokens, with a 192,523-token peak and no
native compaction. These costs are reported separately from completed scans;
the failed transcript and metrics remain in run `20261012-073505-0a8044bb`.

The retry completed run 22 and published
[#15349](https://github.com/TauCetiProject/TauCeti/issues/15349), adopting
Mathlib's partition–Young-diagram conversions and conjugation while preserving
the dominance-reversal and special-shape APIs. It also published
[#15350](https://github.com/TauCetiProject/TauCeti/issues/15350), adopting the
formal logarithm series and shifted logarithm while preserving convergence,
analyticity, naturality, inverse, and BCH developments. It compared scalar
normalization and total series sums, including outside the convergence region.
Its cursor reached `1f22924fd517103a8ba25a3b499edc45bea44473`;
199 commits remained.

Run 23 published [#15362](https://github.com/TauCetiProject/TauCeti/issues/15362)
for maximal-subgroup indices in p-groups,
[#15363](https://github.com/TauCetiProject/TauCeti/issues/15363) for modular/cusp
restriction maps, and [#15364](https://github.com/TauCetiProject/TauCeti/issues/15364)
for residue-field identifications over a field and the residue-degree/finrank
relation. The partial findings explicitly preserve the missing cusp-form linear
operation and trace/slash APIs, and the general base-ring algebra structures and
residue-degree tower results. The residue-degree comparison checks agreement of
the scalar structures before transporting finrank. Its cursor reached
`c556e0d77deb4e2de74629f110215292e6783cb7`; 39 commits remained.

Run 24 examined the final 39 commits, including infinite-place completion
extensions, manifold derivative constancy, and echelon/matrix algorithms. It
found no actionable overlaps and reached
`0493595ecd5da8ef14742febab99728f98e948e8`. A fresh authenticated REST read of
Tau Ceti `main`'s `lake-manifest.json` and a fresh read of the authoritative shared
cursor agreed at that SHA; **zero commits remain** through the current pin.
All 24 completed result lists concatenate to exactly the 2,909 consecutive
first-parent commits since the initial cursor, with no skipped or repeated SHA.
The finite queue exited and no scan was left running.

The catch-up added 2,279 commits and 14 issues in 15 completed investigations;
commissioning overall examined **2,909 commits and published 18 issues**, or one
issue per 162 commits. All 24 completed native rollouts contain zero compaction
records. Their maximum request input was 369,646 of the reported 475,000-token
window (77.8%). The 160-commit default remains supported by these observations,
with the existing override available for other model profiles or difficult
batches. API difficulty and search behavior still matter more than patch bytes.

| Scope | Completed scans | Model minutes | Uncached input tokens | Cached input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full pin catch-up (runs 10–24) | 15 | 196.5 | 4,110,770 | 103,150,848 | 278,250 |
| All commissioning (runs 1–24) | 24 | 263.7 | 6,016,810 | 141,750,912 | 388,719 |
| Failed capacity attempt, separately | 0 | 9.7 | 183,790 | 1,982,976 | 10,452 |

The failed attempt is additional spent work, not additional covered commits.
Including it, the commissioning conversations took 273.4 model minutes and
used 6,200,600 uncached input, 143,733,888 cached input, and 399,171 output tokens.
These provider totals accumulate across tool turns; they are not context size
or a monetary cost estimate. The failed attempt's missing final CLI usage was
recovered from allowlisted native `token_count` metadata. Per-batch elapsed
minutes appear in the tables above.

The queue retains host logs and per-round summaries in
`state/overlap-calibration/catchup-20261012-0457.jsonl`, alongside the complete
batch, result, transcript, usage, and native-context metadata for every scan.


## Further repair trials

Two additional `overlap-fix` rounds selected #15246 and then #15247 from the
labelled issue queue. The earlier #15215 and #15217 repairs were skipped because
they already had associated open PRs. These rounds used the same process-local
linker wrapper, a temporary authoring cap of 30, and the finite commissioning
pacing override; normal worker settings were unchanged.

| Issue | Repair PR | Round minutes | Uncached input tokens | Output tokens | Reported compactions |
| --- | --- | ---: | ---: | ---: | ---: |
| [#15246](https://github.com/TauCetiProject/TauCeti/issues/15246) | [#15267](https://github.com/TauCetiProject/TauCeti/pull/15267) | 9.9 | 103,587 | 12,606 | 0 |
| [#15247](https://github.com/TauCetiProject/TauCeti/issues/15247) | [#15271](https://github.com/TauCetiProject/TauCeti/pull/15271) | 4.7 | 65,299 | 4,879 | 0 |

The comodule repair removes both local `sum_apply` declarations and adds the
`FunLike`, `IsZeroApply`, and `IsAddApply` instances needed by Mathlib's generic
API. Temporary Lean checks verify root `sum_apply`, `FunLike.coe_sum`, and `simp`
for both plain and categorical morphisms under the original semiring assumptions.
The full build and governance checks passed. Its
[CI run](https://github.com/TauCetiProject/TauCeti/actions/runs/38206577801)
passed, and the peer reviewer `ldct` approved all rubrics at 05:43 UTC.

The antipode repair constructs the surviving broader `antipodeComp` operation
with `HopfAlgebra.antipodeAlgHomOp` and `AlgHom.fromOpposite`. Its evaluation and
linear-map identities remain definitionally identical; the noncommutative-source
and weaker-target group APIs are retained. The full build, lint, and governance
checks passed, including an axiom audit of 281,420 declarations. Its
[CI run](https://github.com/TauCetiProject/TauCeti/actions/runs/38206924727)
passed, and the peer reviewer `sqrt-of-2` approved all rubrics at 05:34 UTC.

A targeted normal review round for #15271 found the peer's existing review claim
and stopped without spending model tokens. That round nevertheless spent 5.8
minutes surveying the broader PR inventory before targeting applied; this is a
potential efficiency improvement for ordinary maintenance rounds, separate from
the overlap phases' focused surveys.

At the 05:45 UTC checkpoint all four repair PRs were waiting in bors batch 548,
behind the running batch 545. The earlier combined batch 543 passed its build
but failed to fast-forward because main moved; a successful unrelated batch 547
was not evidence that these repairs landed. Completion must be verified against
the actual repair PR, its squash commit on main, and the issue's closure. Bors
uses squash commits and closes PRs with its merged-title marker, so GitHub's
`merged_at` field alone is not a sufficient completion check.

Repair conversations and per-conversation usage metadata are retained under
`logs/overlap-repair-calibration/`, with timestamps `20261012-045846` and
`20261012-050855`.

The combined batch 548 build later failed after 44 minutes 21 seconds: two
`DegreeQuotient` proofs (lines 75 and 217) referenced the missing
`TauCeti.DirectSum.coe_decompose_smul_add_of_right_mem` declaration. This was
outside all four overlap repairs. PR #15096 was withdrawn and Bors created
normal retry batch 550 with the four repair heads unchanged. This event measures
merge-queue delay, rather than a regression in these repairs.

After an external queue reset, a normal merge sweep requeued the repairs into
batch 552 with their original heads. Its full Lean build passed (20,156 jobs),
as did duplicate, module, and axiom audits (283,922 declarations), but the
environment lint rejected the unrelated
`TauCeti.specializationDegeneracyMatrixAtOne_mulVec_kernel` simplification rule
for `Matrix.mulVec_fin_two` normalization. This second combined-batch failure
also occurred outside the four repairs; normal queue recovery continued.

## Verified landing and issue closure

On October 12 at 11:51 UTC, fresh authenticated REST reads verified that two
repair PRs had landed through the ordinary Bors workflow after batch 555 passed
all required CI jobs. [PR #15267](https://github.com/TauCetiProject/TauCeti/pull/15267)
landed as [squash commit 27d6b8c8](https://github.com/TauCetiProject/TauCeti/commit/27d6b8c809e8425a8898922d04d58787e1800760)
on main; its closing reference completed [issue #15246](https://github.com/TauCetiProject/TauCeti/issues/15246)
at 11:45:50 UTC. [PR #15228](https://github.com/TauCetiProject/TauCeti/pull/15228)
landed as [squash commit 257659c0](https://github.com/TauCetiProject/TauCeti/commit/257659c018638cd6343ab64873f9b60dd3bfb7e3),
closing [issue #15215](https://github.com/TauCetiProject/TauCeti/issues/15215)
at 11:45:46 UTC. The finding markers and `Fixes` references survived into both
commits. Bors success comments independently confirmed the landings.

Bors marks these PRs closed with `[Merged by Bors]` titles; GitHub's `merged_at`
is null because the workflow pushes squash commits rather than invoking GitHub's
merge endpoint. Main history plus Bors success and actual issue closure establish
the complete scan → issue → repair PR → review/CI → landing → closure lifecycle.
No manual merge or premature issue closure was used. The other two repair PRs
remained queued at this milestone. The worker feature PR was then published as
requested, without waiting for those additional repairs.
