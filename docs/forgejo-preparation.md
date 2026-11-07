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

No production pins, development forge, admission settings, mirror configuration or
shared-state lifecycle have changed as part of this foundation.
