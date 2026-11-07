# Forgejo preparation: execution record

Tracker: [Worker #602](https://github.com/TauCetiProject/TauCetiWorker/issues/602).
Accepted architecture and ordered work packages:
[TauCetiTools preparation document](https://github.com/TauCetiProject/TauCetiTools/blob/8f30faab8f3545ce688c199c34efa780c0603111/docs/forgejo-preparation.md).

Forgejo will own development and merges; GitHub Actions initially continues running
CI and fetching retained candidates directly from Forgejo. Agents keep familiar
`gh` commands through the local compatibility layer. Forgejo clients use direct
reads. The shared GitHub state service is retired after its production consumers
migrate. Preparation includes a full isolated rehearsal before production cutover.

## Foundation implementation

[Tools PR #179](https://github.com/TauCetiProject/TauCetiTools/pull/179) introduces
qualified identities, schema v2 with independent source and execution repositories,
a direct Forgejo REST client, an experimental `tauceti-gh` read entrypoint, and a
disposable real-server smoke. Existing schema v1 defaults/state keys are preserved.
Legacy worker bindings reject Forgejo activation until their adapters are ready.

The [Forgejo development fork](https://github.com/TauCetiProject/forgejo) retains
official Codeberg history, the official default branch, and the v17.0.1 baseline.
Actions are disabled. [Fork issue #1](https://github.com/TauCetiProject/forgejo/issues/1)
tracks future unmodified-gh server compatibility.

A native Forgejo 17.0.1 smoke runs under an ordinary account, using SQLite and
private temporary state. It needs no Docker access or administrator permissions.
Its loopback server disables SSH, registration and Actions and is stopped afterward.
This is a client/API smoke, not the complete migration rehearsal.

## Source integration inventory

[Machine-readable inventory](evidence/forgejo-integration-inventory-20261107.json)
records immutable upstream revisions and line locations for forge URLs, GraphQL,
CLI launch sites/literal commands, wrapper argument lists, credential references,
source identities, workflow events, Actions context and shared-state mentions.
It covers Worker, Tools, Fleet, Review, Progress, CI, content, Roadmap and Data.
Extensionless executable/shebang scripts, Dockerfiles, service units, JSON/JSONC,
lake manifests/configuration and example configurations are included. Forge configuration
directories (`.github/`, `.forgejo/`, `.gitea/`) and `CODEOWNERS` files are listed even
when no regex category matches, including issue forms, PR templates, workflows,
and `docs/CODEOWNERS`. The report records its scanner digest,
matching/filter policy, and any size/encoding omissions. Archived records and
build artifacts are deliberately excluded.

Fetch repositories so the recorded commits are available, then replay those pinned
revisions. Omit `--replay` to inventory current default-branch refs instead:

```sh
python scripts/forge-inventory.py \
  --replay docs/evidence/forgejo-integration-inventory-20261107.json \
  --output REPORT.json \
  TauCetiWorker=/home/kim/TauCetiWorker \
  TauCetiTools=/home/kim/TauCetiTools \
  TauCetiFleet=/home/kim/TauCetiFleet \
  TauCetiReview=/home/kim/TauCetiReview \
  TauCetiProgress=/home/kim/TauCetiProgress \
  TauCetiCI=/home/kim/TauCetiCI \
  TauCeti=/home/kim/TauCeti \
  TauCetiRoadmap=/home/kim/TauCetiRoadmap \
  TauCetiData=/home/kim/TauCetiData
```

Inputs are tracked files at the selected Git revision, not local modifications or
credential files. Reports contain line numbers rather than source snippets.
Change checkout paths for another machine. `--replay` selects the recorded commit
for each repository; omit it to inventory current `origin/main` refs. Canonical
shared assets default to the exact repository label `TauCetiTools`; use
`--canonical-shared-assets NAME` when providing a different label.
Lexical findings guide manual contract review: URLs can be intentional upstream or
archive links, and dynamic command construction needs inspection. Tools' shared
assets are canonical implementations; copies in downstream projects are callers
or generated installations. Findings do not authorize global URL replacement.

## Command and query discovery

[Contract candidates](evidence/forgejo-contract-candidates-20261107.json) expand the
integration inventory into source-linked command forms and GraphQL literals.
Every row identifies its repository revision, Git blob and physical source range.
`content_id` identifies a source form within a blob; `id` distinguishes its
repository/revision/role/path occurrence. Columns distinguish calls on one line,
and editorial review text is excluded from both keys. The report records both scanner digests and the input
inventory and pin-manifest digests, Python implementation/version and Git version.
Inventory scanner digests and default file selections must match before extraction.
The path policy is then expanded to eligible text without requiring lexical hits,
so wrapper-only modules are included. It is generated from committed objects; imports, shell commands,
environment expansion and credential files are never evaluated.

Literal argv definitions and conditional extensions are recorded independently
of their uses. Call sites link to candidate definitions in lexical/enclosing scopes. Name-based
assignments, `+=`, concatenations and append/extend/insert calls retain their source
forms, including builder functions that return a command. Attribute state, imports,
global/nonlocal directives and runtime control flow still need manual inspection.
This is not a reaching-definition or execution analysis. Direct `subprocess`
argv is distinguished from recognized CLI and HTTP wrappers, other executors,
callbacks, nested launchers, unresolved executable expressions, shell snippets and
prompt instructions (including instructions embedded in Python string literals).
For embedded text, the source range identifies the literal and `template_line`
identifies the command within its decoded text. These categories require different review: a wrapper name is
not proof of prefix/argv semantics, a shell snippet is not a parsed simple command,
and an executable expression can ultimately select a tool other than `gh`.
Variadic launchers, execv-style handoffs and Python/uv-script shebangs are included.
Shell/text contexts collect quote continuations up to the recorded line bound.
Markdown and decoded Python instruction strings join only backslash continuations;
quote-spanning instructions remain manual source-literal/code-block review. Every
physical line is scanned independently, and a range with unparsed tokens covers
only its first line in reconciliation.
Literal flag scans remain lexical because a flag-looking token may be option data.
F-string conversions, format specs, concatenations and list spreads retain explicit
unevaluated expressions. Python GraphQL literal candidates include field-prefixed
queries, anonymous operations and named fragments. Other languages, including
TypeScript builders and direct HTTP GraphQL clients, produce text candidates that
need language-specific extraction. Python HTTP-wrapper candidates preserve raw positional
and keyword arguments, plus candidate query bindings; these do not assume CLI
prefix or HTTP-method semantics. Composition and connection to an executed
operation still require review.
Known wrapper names, argument-keyword rules, command groups and endpoint prefixes
are recorded in `policy`. Same-file function names are propagated from descendant
`gh` literals and known/derived wrapper calls; resulting calls retain definition
locations as candidates. Name matching does not prove class dispatch, imports or
execution, and a derived helper may also invoke programs other than `gh`.

The report lists the scanned path/blob/language set, including files with no rows.
Python parse failures retain a text fallback and an explicit omission record.
Every inventory `gh_cli`/`graphql` hit and literal `api.github.com` line must be
covered by a discovery range or an `uncovered_inventory_hit` review item. This
reconciles extraction with lexical evidence; it does not prove the range is an
executed command or that all runtime integrations have a lexical hit.

Replay the nine inventory revisions and three known source inputs with:

```sh
python scripts/forge-contracts.py \
  --inventory docs/evidence/forgejo-integration-inventory-20261107.json \
  --output REPORT.json \
  --pin-manifest docs/evidence/forgejo-contract-source-pins-20261107.json \
  TauCetiWorker=/home/kim/TauCetiWorker \
  TauCetiTools=/home/kim/TauCetiTools \
  TauCetiFleet=/home/kim/TauCetiFleet \
  TauCetiReview=/home/kim/TauCetiReview \
  TauCetiProgress=/home/kim/TauCetiProgress \
  TauCetiCI=/home/kim/TauCetiCI \
  TauCeti=/home/kim/TauCeti \
  TauCetiRoadmap=/home/kim/TauCetiRoadmap \
  TauCetiData=/home/kim/TauCetiData
```

The Worker input is a source revision from a selected configured runtime marker;
the Tools and Review inputs are its declared dependencies. The pin manifest records
a timestamped runtime-marker observation inline, its digest and target description,
and verifies dependency declarations against immutable `pyproject.toml` blobs of
an inspected source pin. Marker provenance rejects fields claiming running-process
verification. The receipt is operator-supplied evidence,
not independent verification of a running process. The digest provides integrity,
not authenticity; its parent-observation digest is an opaque operator reference
to the retained private observation, not a public evidence chain. Additional `--pin NAME=COMMIT`
arguments are explicitly labeled operator selections. These are source inputs,
not a full audit of installed wheels, running workers, fleet launchers or all hosts.
Repeated source revisions deliberately retain their inventory/pin roles.

This discovery report is not the accepted golden corpus. Each executable caller
still needs a verified command-specific parse, source-forge/CI ownership, output
field and identity contract, pagination/error behavior, and representative parity
fixtures. Conditional construction and unrecognized launch mechanisms require
manual inspection. Compatibility or activation cannot be inferred from row counts
or a clean scan.

## Direct review-state reads

`GitHub.for_repository(repository, api=api)` accepts an explicit Tools
`RepositoryRef` and a Forgejo client bound to that origin. Its Forgejo result is a
read adapter for PR metadata, issue discussion and reviews. It selects neither
credentials nor a source from the environment. Existing GitHub construction and
production bootstrap remain unchanged; this factory does not enable a complete
Forgejo worker.

`ReviewState` uses this adapter directly and never consults the shared-state
service or unqualified survey seeds. Repository, origin and credential identity
scope its caches. Rotating the client's credential or origin makes reads
unavailable until a new adapter is constructed, including reads from existing
memory or disk. Tokens and server error bodies are not persisted in cache keys or
failure records.

Triage reads use a bounded process-local JSON cache per adapter or explicitly
shared cache instance (128 entries, 4 MiB by default, 15-second TTL measured from
request start). Identical triage requests coalesce; unrelated requests fetch
independently. Fresh reads bypass entries and coalescing. Concurrent fresh reads
can both return, while only the latest-started request may publish its result.
Write invalidation targets that qualified repository and PR across credential
scopes, including list observations, and rejects fetches overlapping the write.
Other PRs continue independently; coalesced waiters are released immediately.
Failed requests are not cached, and a verified empty list differs from an
unavailable read. Cached review metadata retains assumed or stale provenance;
it supplies no fresh approval authority. Revision-scoped read contexts use
separate cache keys.

Disposable review-state sidecars live below `sbcache/sources/<digest>/`, in private
leaf directories with atomic private-file replacement. Best-effort pruning bounds
recognized cache files across credential scopes to 384 files and 16 MiB after
quiescent writes. It ignores unrelated files, outboxes and symlinks. These files
are disposable observations, not publication journals or durable decisions.
Source file ages start before the request. Atomic replacements attempt a
nonblocking directory lock and retain a later existing record when an older
request finishes last. Lock contention skips the cache write.
Source sidecars expire at the adapter's triage TTL even if the PR's update clock
stays unchanged. Native Forgejo 17 does not move that clock for comment edits or
deletions; forced reads bypass sidecars and the memory cache immediately.

The adapter normalizes discussion/review timestamps to fixed-width UTC. Scoreboard
selection orders equal timestamps by native comment ID. Typed PR projections
validate native number, discussion URL, flow and lifecycle, then require the
requested fields' proofs; they do not require all historical head/stat proofs of
a whole raw REST record. Neither projection nor mergeability observations supply
approval or exact-candidate merge authority.

`tests/integration_forgejo_reads.py`, when given `FORGEJO_BINARY`, starts an
isolated temporary server and exercises the actual adapter and `ReviewState`
against a private PR, multiple discussion pages, submitted and pending reviews,
credential denial, comment edits and deletion. Shared-state and GitHub fallback
calls fail the fixture. This loopback test is separate from the required hosted
HTTPS and fleet-load rehearsal.

Inline thread/reply reads deliberately remain unavailable. Authoring, claims,
reactions, trusted publication, scheduling and approval decisions still require
their qualified adapters before production Forgejo bootstrap can be enabled.
Worker owns an additional activation refusal so a Tools update cannot enable it
implicitly. Large survey histories and fleet concurrency still require the
representative load tests below; these cache bounds are not a capacity proof.
Cross-process cache deletion/order and invalidation still require tests and
coordination before activation. Cross-credential write invalidation currently
covers the shared in-process memory cache; private disk observations in another
credential scope can remain assumed until their TTL expires. Decisions requiring
current state must force a source read.

## Outstanding integration and completion criteria

The worker's existing `gh` launcher, credentials, forks, claims, publication recovery
and scheduling still need qualified forge adapters. Review and Progress need
equivalent operations and trusted publication. Native review replies and resolution
must be covered by server/API integration tests and upstream patches as required.

The queue still needs separate source-forge and CI backends, versioned exact-candidate
request/result contracts, retained refs, trusted result verification, and Forgejo
webhook/reconciliation support. Workflow scripts must handle Forgejo provenance,
labels and comments for both PR and post-merge execution. Fleet, reporting, archives,
installers and pins require the same source/execution distinction.

The final rehearsal must exercise authoring, review, repair, queue admission, hosted
GitHub CI, exact-revision merge, main mirroring, cache/docs, archives and progress;
repeat across faults and restarts with the shared state endpoint blocked. Load tests
must use representative PR histories and fleet concurrency before service retirement.

Installed production environments, development forge, admission settings, mirror
configuration and shared-state lifecycle are unchanged by this preparation.
