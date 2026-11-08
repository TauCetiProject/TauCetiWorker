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

## Qualified actor and repository ACL reads

The direct source's `authenticated_actor()` reads the native authenticated user;
`repository_access()` reads that actor and the selected repository's native user
ACL. Both bypass cache lookup, storage and coalescing on every call. PR-content
invalidation does not interrupt these independent identity reads. Denied, malformed, imported
or missing identities remain unavailable and never fall back to a GitHub account.
Only the native ID and display login are retained from the user profile.
Token-based actor reads require `read:user`; repository ACL reads also require
`read:repository` or broader native scopes. A repository-only token cannot be
substituted for an authenticated actor observation.

`SourceActor` compares the qualified origin and native user ID; a rename does not
change identity, and a reused login on another origin does not match. Native
integer IDs and Tools' qualified projected IDs use distinct parsers. Nonpositive
sentinel IDs cannot become authenticated native principals.

`SourceRepositoryAccess` binds the actor, qualified repository, native repository
ID, Code-unit read/write ACL, repository admin role and archive/mirror flags.
`user_code_can_push` describes only Code-unit access; it certifies neither token
write scopes, other units' permissions, branch protection nor publication/merge
authority. Admin role does not imply that the Code unit is enabled.
The disposable real-server fixture checks the distinction: an owner using a
read-only token can see a true push ACL, while the native server denies its
branch-write request. The user-only token can identify the actor while repository
access remains denied. Each mutation must still pass its native scope and
permission checks.
The same fixture exercises a non-admin collaborator gaining read access, gaining
write access and losing repository access, without reusing an earlier ACL. It
also checks the explicit archive flag after the repository is archived.

The source inventory consumer below uses these qualified actor inputs.
Production still uses GitHub; bot repair, source/CI status authority, inline
contests and remaining source operations require their integrated adapters.

## Direct source inventory and ownership

The ordinary `survey()` consumer accepts the direct source adapter. It exhausts
bounded native open-PR pagination and reads the authenticated native actor fresh.
Inventory reads share the source's bounded, credential-scoped triage cache and
PR write invalidation. No GitHub identity, ACL, shared-state survey or per-PR
history/status/diff hydration supplies this inventory. Pagination failure,
malformed source identity, credential rotation or denied identity reads produce
an explicit unavailable result, never a successful empty inventory.

Source survey and PR records carry their qualified repository and principal
identities. Ownership, roadmap pressure, status-label counts and repair ownership
checks compare native IDs on the selected origin. A login rename preserves
ownership; login reuse, unknown/ghost authors and another origin do not grant it.
The existing GitHub survey JSON shape stays unchanged.

Native `build` statuses are excluded from source survey CI classification. Native
false mergeability remains unknown. This inventory provides no verified execution,
claim or publication authority: its queues and next automatic stage remain empty,
including after TUI rescoping, and it reports a source-scheduling dependency.
Deep scheduling requests report unavailable until those adapters are integrated.
Production bootstrap still refuses Forgejo activation. This read consumer is not
the isolated author/review/CI/merge rehearsal or a capacity proof.

## Queue source and execution boundary

The optional source-bound queue has a separate native HTTP authority path. Its
authority requires an explicit, immutable `QueueControlPolicy` from trusted
operator configuration. Register only dedicated, independently generated
high-entropy queue-control bearers using `control_token_fingerprint(source, token)`.
Its private file uses `tauceti-queue.control-policy/v1` with exactly `schema`,
`source` (the qualified origin/name/native-ID record) and a `fingerprints` array.
`QueueControlPolicy.load(path, source=trusted_source)` validates the file against
the independently configured source; file contents cannot select another source.
`NativeQueueAuthority.from_control_file` constructs one startup snapshot for both
the server and controller. Every ancestor is opened through a pinned directory
descriptor without following symlinks, with trusted ownership and write modes.
Root-owned sticky temporary ancestors are allowed, but the immediate parent must
be private and owned by the service user. The policy must be one user-owned
regular file of mode 0600 or 0400 with no extra hard links. Reads are bounded to
128 KiB and detect most in-place changes; atomic replacement is required because
coarse filesystem timestamps cannot prove that every same-size edit was absent.
Duplicate fields, duplicate fingerprints,
unknown fields and malformed source identities are refused. Errors omit private
contents. Write replacements atomically, then restart as described below; the
loader never reloads policy during a request or caches a successful source ACL.
The example `services/queue/source-control-policy.json.example` is a placeholder,
not an active enrollment. Native startup and the installer do not yet select this
backend; wiring this loader does not enable native execution.
All path components must be real directories: an operator using a symlinked home
or configuration tree must choose its verified canonical path explicitly. File
permissions protect against other Unix users, not the owner or root. The owner can
chmod a 0400 policy back to writable. Queue policy, source publication credentials
and service code must therefore be isolated from agent accounts before serving
less-trusted workers; the current user-unit layout is not that privilege boundary.
The policy holds a bounded nonempty set of SHA-256 fingerprints, domain-separated
by the native source origin and repository ID. It never accepts enrollment from
request JSON, task metadata or a Forgejo profile. Fingerprints are private
configuration and are omitted from policy repr, tasks, public progress and journals.
An unenrolled token is refused before the HTTP body is read or any source request,
even when it otherwise has effective admin and Code write privileges. For incident
revocation, revoke the bearer at Forgejo first: removing a fingerprint only removes queue enrollment,
and does not revoke the credential's source permissions. Then remove the old
fingerprint and restart the queue process with the new immutable policy and one
authority shared by its HTTP server and controller. Do not swap only the
controller's authority or mutate an existing instance. Omitted old tokens cannot
authorize queue calls; process-local evidence from the previous instance is invalid.
Enrolled credentials still need fresh native
scope/resource/ACL checks on every operation; enrollment does not cache a grant.
For routine rotation, enroll old and new fingerprints together, restart with the
shared authority, roll workers to the new credential, then revoke the old token
at Forgejo only after no running worker process still holds the old bearer.
Workers read their bearer once at process startup; confirm each process has
restarted and resumed its claims with the replacement, or has finished its work.
Remove the old fingerprint on the next restart. This overlap keeps
normal admissions and running work enabled. The replacement bearer must identify
the same native actor to resume that actor's claims; a bot-user change requires
reconciling the previous actor's work. Removing enrollment immediately
returns 403 and stops affected claims; revocation of an enrolled bearer returns
401, so existing renewal retries can continue only until lease expiry and cannot
submit work. Reconcile affected work explicitly after incident revocation.

Each mutation reads its bearer actor, repository user ACL and effective credential
access directly from the configured Forgejo origin by native repository ID. The
required `GET /api/v1/repositories/{id}/effective_access` response comes from a
private native API addition. It includes current repository restrictions and
read/write repository scopes. Unsupported endpoints, missing flags and inconsistent
responses refuse controls without falling back to ordinary repository metadata.
The client also checks the configured repository name, origin and native ID. It uses neither the
GitHub identity/admin caches nor a previous successful native observation. A denied
or unavailable read refuses the operation. Queue controls require current repository
admin and Code write ACL together with effective credential admin and Code write
access, with archive and pull-mirror states refused. A read-only token cannot borrow
its user's admin privileges. The native endpoint requires explicit scopes including
`read:repository`. Its actor contains only the native ID and login, so repository-limited
tokens can use it without `read:user`, which their creation rules forbid.
Unscoped password, session and Actions authentication, implicit OAuth API scope
fallback and impersonation cannot supply this evidence. Explicitly scoped OAuth
and authorized-integration tokens can supply effective-access evidence, but they
remain unenrolled for queue control unless the operator deliberately approves that
exact bearer for this source. Integration scope or a third-party OAuth grant alone
does not enroll it. The returned user ACL
already includes credential repository-resource reduction; it excludes read/write
scope, which the effective flags additionally enforce. Protected branch, approval and merge
checks remain necessary when publishing to the source.

Native HTTP handlers reject malformed bearer headers without reading the body or
contacting Forgejo. They read the bounded body under a ten-second total deadline
before observing authority. Process-local observations expire after 15 seconds,
including the native reads; they are checked after acquiring the controller lock.
Claims persist a separate qualified native
actor record, leaving the legacy GitHub-login field empty. Renewal, release and
submission require the same native actor ID and origin plus fresh authority; a
login rename preserves ownership and a reused login does not. After fresh
authentication, a renewal entering the controller before expiry retains its
arrival time while waiting for the lock, refreshing expired authority outside the
lock while its renewal registration stays active. Reacquisition uses priority and
the remaining evidence lifetime, with at most three refresh/acquisition attempts; overlapping
renewals keep independent registrations. Source authentication and ACL
outages fail closed; an unauthenticated request cannot extend a lease.
All claim-expiry decisions honour a timely renewal that is still in progress,
including concurrent renew, submit and explicit release requests. Current owner
and authority checks still apply. Exhausting refresh attempts can delay an error
until a current lock holder finishes; it never permits stale evidence to grant work.
Submission refreshes authority outside the lock after validation and before
adopting a chain, then checks current claim, task-kind rules, head target and
covered membership again. Unrelated metadata and valid append operations for
pre-rebase and pre-rebased-prepare tasks do not discard work. Ordinary preparation
still requires its exact batch version. Stale or late results are recorded as
retained rejections; that audit bookkeeping uses the initially authenticated
request and never grants adoption or source-write authority.
Operator retry and rebuild refresh and recheck state after their slow reads too.
Existing unqualified
claim records cannot acquire a native owner implicitly.

Source-bound HTTP startup refuses a missing or mismatched native authority. Native
feeds, GitHub webhook intake, unqualified Bors imports and legacy approval polling
remain unavailable on this path. Native task execution, remaining reporting
consumers, source publication and approval, and production bootstrap are still activation gates.
Native task claims use `tauceti-queue.source-task/v2`. Their persisted binding
contains qualified source and execution repository IDs, the CI policy digest and
the queue's durable epoch. Member identities include the source origin, native
repository ID and pull number; their chain attribution labels remain separate.
The wire record and checkpoint omit the legacy `repository` field, so older
runners cannot interpret the source name as a GitHub target. Missing, malformed
or different persisted bindings are not claimed or silently reinterpreted after
a policy change. Each tick supersedes stale open tasks, releases their claims and
creates fresh tasks for still-current work under the new policy. The old task
snapshot stays intact. Owners can release stale-policy work; late results are
retained and recorded as rejected without adoption. This keeps policy upgrades
from leaving an unclaimable task blocking the head batch.

The native task client requires its expected binding from trusted runner
configuration and an explicit native bearer. It refuses redirects and remote
plain HTTP, bounds responses, rejects duplicate JSON fields, and checks the binding
before claim adoption, renewal, submission or release. No binding is inferred from
the claim response. The native claim request must identify its expected contract;
missing or mismatched contracts are refused before source API reads or lease
allocation, with a second policy check under the controller lock.
Checkpoints and compact progress retain the qualified binding;
monitoring matches progress to the SQL task binding and reports task identity even
without a worker observation. These identity helpers do not enable public native
export: the existing GitHub allowlist and URL policy still refuse source-bound ledgers.
Native metadata does not enable the existing runner:
native execution, legacy admin-cache use, Git fetch/push, agent work and resume or
release retries through an unqualified client remain refused until the native
Git transport, source validation and runner adapters are implemented.
Native requests run once in a disposable HTTP helper process. Their total deadline
includes DNS resolution, TLS, request writes, response headers and success/error
bodies. On timeout the child is killed and reaped; delivery remains uncertain and
the transport does not resend the POST. Uncertain native delivery, malformed
native claim responses and native 5xx outcomes stop the generic service retry
wrapper. Responses from helpers that already exited cleanly are preserved if the
parent was descheduled at the deadline. A helper still running is killed and its
delivery remains uncertain. Bearers travel on stdin, never process arguments. The helper source
is captured at module import, skips site initialization and has its own lifetime
bound if the parent dies. Redirect, HTTPS and response-size limits apply inside it.
OS process creation and reaping cannot themselves be interrupted by a Python
timeout. Interpreter startup and memory costs still need representative load tests.
Native claim reconciliation/idempotency and the outer runner's restart/poll behavior
must be integrated before execution: a no-retry exception alone cannot recover an
assignment whose response was lost.
Native checkpoint directory/cache namespaces still need integration before
enabling execution.
Ingress rate/concurrency limits still require integration before activation.
Dedicated control credentials and their provisioning, revocation and rotation
must also be integrated with the operator installer; native bootstrap stays disabled.
The installer must generate repository-limited credentials for dedicated bot users
with minimal scopes, not enroll a shared personal/CI bearer. Enrollment narrows
what the queue accepts; it does not constrain use of that bearer directly against
Forgejo. The present admin/Code-write requirement therefore still grants source
power to the credential holder. Separating task credentials from source publication
authority needs a queue-issued credential or a separately authorized native task
role before exposing task controls to less-trusted workers.
OAuth/integration refresh and its restart/enrollment lifecycle are not implemented
by the installer and are not supported operationally for queue-control credentials.
No client or integration ID is implicitly trusted across credential rotation.
The native effective-access API addition remains private pending its upstream
contribution handoff. The disposable native integration checks effective access
with the private native build and verifies that stock Forgejo refuses controls.
Set `FORGEJO_EFFECTIVE_ACCESS=1` with `FORGEJO_BINARY` to require that capability;
`FORGEJO_STATIC_ROOT` selects matching unbundled assets. Separate HTTP/controller
regressions cover read-only scopes, revocation, slow bodies, lock waits, name reuse
and claim ownership. These checks do not constitute the full hosted HTTPS
or fleet-capacity rehearsal.

The native read/authority fixture also supports `FORGEJO_HTTPS=1`. It generates a
temporary loopback certificate, trusts that certificate only for the fixture's
lifetime, and runs the actual Forgejo client over verified HTTPS. Untrusted-CA
and hostname-mismatch probes must fail before the normal read/authority cycle
runs. The profile restarts its server with the same database and certificate,
then checks repository identity, archive state and revoked collaborator access
again using the existing client objects. This profile requires `openssl`; it does not provide the remaining
author/review/repair/hosted-CI/merge/mirror lifecycle or fleet-load evidence.

The controller routes repository reads, refs, publication and repository authority
through `SourcePort`; dispatches, workflow observations, cancellations, verdicts
and compiled declaration evidence use `ExecutionPort`. The existing combined
GitHub adapter supplies both by default. Its `github` attribute remains an alias
for source-authentication callers. This separation does not select another
execution repository or enable Forgejo. The internal `SourceCandidateCI` coordinator
adds a separate qualified request/result path; the production service does not
construct it or enable a native source backend.

A CI attempt now persists a request identity before source or execution I/O.
Preparation can resume the same attempt after a restart. Before the one dispatch
send, its state becomes `sending`; acknowledgement records `accepted`, a proven
rejection records `rejected`, and an ambiguous exception records `uncertain`.
The GitHub dispatch transport does not automatically retransmit on server errors,
401 or rate limits; rejected credentials are invalidated for a later attempt.

Unknown sends retain their attempt ID and candidate branch for reconciliation.
If the run becomes visible, the existing title/time lookup associates it with the
attempt; this is not a producer-verified request binding. A missing run
alone does not prove rejection, so normal ticks do not retransmit uncertain sends
after a timeout. An explicit operator `retry_ci` can supersede one after the same
live-run check and observation of any completed run. Ordinary source preparation
failures and temporary dispatch rejections retry with capped exponential backoff,
respecting a persisted server retry deadline. Permanent HTTP rejections use a
separate finite budget and raise an operator alert. These non-sends do not exhaust
the retry budget for failed CI runs. Credential preparation failures are proven
non-sends; other transport failures remain uncertain unless the backend can prove
rejection. Existing ledger rows migrate with the
`legacy` dispatch state and retain their previous observation behavior.

Resuming preparation respects publication-disabled mode, including while a
prediction repair is available or claimed. Run lookup and the no-run timeout use
the actual send time after a long interruption. Acknowledgement and discovery
share one acceptance transition and its accounting. Supersession preserves the
old outcome across a late acknowledgement or failure; unknown old sends get a
best-effort lookup and cancellation. A run that appears only after that lookup
still requires later cleanup in the integrated request/result reconciler.
Late live runs attach to a previously sent attempt and its existing candidate ref;
newer rejected receipts remain in history without hiding that active attempt.
The monitoring exporter also reads older ledgers before controller migration.
The candidate feed excludes preparations and rejected sends, so they cannot hide
the retained ref of a late run. Exhausted retry budgets continue looking for late
live runs at the normal observation interval. The intent compare-and-set prevents
two connections from sending the same preparation; the service still requires a
single active controller for its other operations.

Before a future rollout, take a ledger backup and settle unresolved sends before
rolling back to older queue code. Older code does not understand these dispatch
states and can retransmit an unresolved send after its legacy no-run timeout.

The legacy dispatch ID and title lookup provide no producer-verified execution
proof. They remain confined to the existing combined GitHub path.

### Frozen source requests and execution ownership

The internal source CI path freezes a complete `source-candidate/v2` request in SQLite
in the same transaction that inserts its prepared attempt. Immutable Git reads run
before that transaction, with the controller lock released; a failed preparation records
a reason and leaves the remainder of the queue pass running. Missing accepted heads are
fetched by their retained original identity before freezing; repeated preparation
failures use capped backoff and a deduplicated journal reason. A source-bound ledger
refuses a restart through the legacy coordinator. Each request keeps its source,
execution and harness native repository identities, queue epoch and nonce, ordered
accepted PR heads, candidate/base/tree, trusted caller and harness revisions, and
approved policy and project configuration bytes. Restart recovery loads these bytes
rather than recomputing a request from current configuration or membership. An approved
pipeline upgrade can serve older pending requests with their archived policy. Repository
role or branch changes cannot silently rebind the ledger, and a new binding refuses
existing unqualified source work without deleting it.

Requests with up to 128 members retain the candidate and original accepted heads. Larger
requests use a canonical membership document in a metadata Git commit whose parents
retain the distinct original heads, limiting publication to two refs. The ledger keeps
the exact membership and commit bytes so real Git garbage collection cannot change the
metadata object's identity during recovery. This restores metadata, not missing original
source history; admission must retain those heads. The 8,192 member maximum is a
transport bound, not a demonstrated fleet capacity.

`GitHubSourceExecution` requires installation tokens minted with exactly the native
execution repository ID and explicit `contents:write`/`actions:write` permissions. A
name preflight alone cannot protect a write against a repository name being reused.
Dispatch validates the current repository identity, default branch and frozen trusted
caller revision before source retention and before its single POST. The final preflight
returns a process-local prepared dispatch with a hidden credential; after the durable
`sending` claim the adapter performs only its one POST, with no token refresh or
metadata reads. A crash during preflight leaves the same prepared request resumable.
Retryable proven non-sends resume the same prepared request with persisted capped
backoff, preserving its nonce, snapshot and retained refs. Scope/identity errors and
permanent token-mint rejections have a finite rejection budget; they do not create a
fresh set of source refs on every poll. The execution repository can be separate from
the GitHub main mirror, avoiding caller revision changes on every source landing. Token
refresh retains the native ID scope; no source credential is sent to GitHub.

A matching run title is only a discovery hint. The trusted artifact reader verifies
request receipts or final results against authenticated GitHub metadata, repository IDs,
workflow revisions and run attempts. SQLite selects one owner with a compare and set and
records additional proven runs without replacing it. Stored ownership permits
observation and cancellation after a receipt expires; it grants no verdict. A malformed
final result cannot erase ownership independently proved by a valid receipt. Completed
runs need a freshly verified result for their current attempt. Legacy source statuses
cannot make a source candidate pass. Promotion rechecks current membership, current
passing evidence and fresh source approval before its source compare and set; an already
visible promotion is reconciled without another write.

Absent, expired, malformed or unavailable evidence keeps the request pending at the
normal polling interval. A single transient observation failure on an already owned run
does not trigger the request-age alert. Liveness checks read current run metadata
without downloading expired artifacts. Automatic retries skip repeated repository-wide
discovery for already owned completed attempts; explicit operator retries additionally
check for newly discovered duplicates. Discovery uses closed creation-time windows and
splits ranges before the [filtered 1,000-run cap](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow). Limits on requests, records, pages and
time precision yield an explicit unavailable result; exceeding them never proves a
dispatch did not happen. An acknowledged or ambiguous send is never automatically
repeated because no run is found. After its evidence deadline the status includes
`reconciliation_required` and the exact request nonce. `retry_ci` refuses when ownership
cannot be verified. For an owned completed run, an operator must name the exact current
request nonce shown in status to retire missing evidence and authorize a fresh dispatch;
all discovered verified duplicates must also have finished. Fresh promotion reads
release the controller lock and recheck local bindings on return. Source CI observation
and live-run discovery also release that lock, then discard observations whose
request or head changed while the reads were in flight. A typed authoritative
approval or membership refusal settles that promotion entry and holds the request for
explicit reconciliation; a changed result returns it to observation. The pending
outcome, hold, promotion generation and journal update share one transaction so a crash
cannot strand the head behind a finished outbox key. The existing admin retry endpoint
accepts the exact `request_id` nonce; native HTTP authentication still requires its
separate qualified authority integration. After publication grace, a verified owned
completed run with a failure, cancellation, timeout or startup failure enters the
bounded infrastructure retry policy only after a complete authenticated artifact
listing and fresh run metadata confirm the result is absent. Read failures and
present-but-unreadable artifacts stay pending. Grace starts at the controller's
first completed observation, tracked separately from GitHub's completion time so
status and feed timestamps retain their original meaning. A successful run without a usable result
still requires reconciliation. Repeated preparation/freeze failures raise alerts, and an
exact-nonce operator retry can retire a request that remains prepared. Re-observing a
deferred passing result does not count the same CI run twice; transient per-attempt
settings are removed after landing.

Permanently missing ownership, automatic late and duplicate run cleanup, richer repair
diagnostics, the native source port, qualified task/reporting metadata, native HTTP
actor/approval authority, protected ref publication and Git transport identity binding
remain activation gates.

The existing public monitoring projection accepts GitHub owner/name allowlists. It
explicitly refuses a source-bound ledger until a qualified public policy and projection
are implemented, avoiding incorrect GitHub URLs and cross-host disclosure. Internal CI
verdicts retain the existing structured monitoring representation. Execution-repository
rules must also prevent the dispatch credential from editing the trusted caller branch.
The current execution-scoped metadata reads require a public harness repository; private
harness access needs a separate read credential.

These paths are exercised with local Git, real SQLite restart/connection races and
trusted-boundary fakes. They are not the real HTTPS source/hosted-CI rehearsal. No
production deployment or admissions change is part of this work.

## Outstanding integration and completion criteria

The worker's existing `gh` launcher, credentials, forks, claims, publication recovery
and scheduling still need qualified forge adapters. Review and Progress need
equivalent operations and trusted publication. Native review replies and resolution
must be covered by server/API integration tests and upstream patches as required.

The queue's internal source/execution ports and frozen request/result integration
still need the native source backend, protected retained refs, native authority,
Forgejo webhook reconciliation and explicit operator recovery. Workflow scripts must handle Forgejo provenance,
labels and comments for both PR and post-merge execution. Fleet, reporting, archives,
installers and pins require the same source/execution distinction.

The final rehearsal must exercise authoring, review, repair, queue admission, hosted
GitHub CI, exact-revision merge, main mirroring, cache/docs, archives and progress;
repeat across faults and restarts with the shared state endpoint blocked. Load tests
must use representative PR histories and fleet concurrency before service retirement.

Installed production environments, development forge, admission settings, mirror
configuration and shared-state lifecycle are unchanged by this preparation.

### Native queue Git transport

`ForgejoGitSource` supplies the Git part of the queue's source adapter. It uses
ordinary Forgejo repositories and HTTP Git, with an explicit source bearer scoped
to the configured clone URL. It disables redirects, interactive credentials,
client hooks and inherited Git credential/trace overrides. The controller clone
is trusted service state; candidate checkouts and their configuration must not be
used as the controller clone.

Candidate and member refs are created atomically with explicit absent-ref leases.
Existing identical refs reconcile without another push; a different existing tip
is refused. Retention accepts the queue's existing local `refs/tauceti-queue/`
names and CI's `refs/heads/tauceti-ci/` names. The former map to remote branches under
`refs/heads/tauceti-ci/queue/`; fetch maps back into the original local names.
This lets Forgejo's ordinary branch rules protect every remote retained ref.
A frozen request's source identity, candidate tree, base ancestry and commit
objects are checked before retention. CI refs for one immutable request share a
bounded API lookup. Other queue refs use exact-name lookups, so a batch spanning
many inputs never scans the growing retained namespace.
Configure a `tauceti-ci/**` branch-protection rule that permits the dedicated controller to create refs and refuses contributor writes;
GitHub builds receive a source read credential. The real-server fixture exercises
this rule, including a contributor's attempted replacement of a retained ref.

Promotion checks local commit ancestry and pushes with an explicit expected-old
lease. A competing update is refused even when the candidate already contains the
competing commit. Forgejo's receive hooks still enforce branch protection. A
transport timeout terminates the complete Git process group and leaves publication
uncertain. SIGTERM allows Git to clean lock files, with SIGKILL after a two-second
grace period if needed. An exact visible tip reconciles a lost acknowledgement without another
push. A definitive server acknowledgement records success even if another writer
then advances the branch; an unacknowledged outcome stays uncertain so the
controller's existing landed-commit reconciliation runs before retry. Source API
identity checks bracket remote operations; they do not make a repository rename/deletion concurrent with Git publication atomic. Such operator
operations must not overlap publication.

Run the real stock-server HTTPS test with:

```sh
FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo_git.py
```

It uses disposable repositories, users, credentials and a local TLS certificate;
`openssl` and Git must be on PATH. It exercises fetch, frozen request retention,
idempotent reconciliation, competing promotion, a rejected non-fast-forward,
server branch protection, and a lost accepted publication response. The portable
validation tests run in `tests/queue_forgejo_git.py`.

This transport does not itself establish review approval, CI success or webhook
admission. The source adapter below connects native review checks; service
bootstrap and trusted publisher integration are still required before activation.


### Native source adapter and admission

`ForgejoSourceAdapter` connects PR metadata, immutable Git comparisons, comments,
labels, publication and fresh repository permissions to the existing controller.
`NativeAdmissionPolicy` uses the installed Review admission policy. Explicit
`NativePublishers` lists identify review, status and recovery publishers by
server-local principal ID, bound to the native source repository. The source API
credential needs repository-admin permission to query other users' current
permissions; Git publication still uses its separately supplied source bearer.

Native scoreboard and recovery comments carry a versioned
`tauceti-source:v1` binding naming the source origin/repository ID, authenticated
issuer and producer's policy revision. Comment authorship alone grants no
authority: Forgejo repository writers may moderate another user's comments.
The publisher freezes a versioned intent with a unique ID and the observed
predecessor registration. It registers that intent through the native commit-status
API, publishes a comment owned by that intent, then records its comment ID and
SHA-256 digest in an append-only status. Registrations acquire their expected
predecessor in native status-ID order; competing registrations for an already
acquired predecessor are superseded. Admission orders publications by those
registrations, so a late completion cannot revive an older verdict. It verifies
the issuer's current permission, exact body and source binding.
Edits, deletions, missing receipts and interrupted publications never expose an
older green review as fresh authority. Ordinary contributors' same-name statuses
cannot impersonate the configured issuer.

The consumer applies its current policy to the published facts. Queue-side prefix,
allow-file or policy-code changes do not themselves discard a published review or
recovery hold. A hold needs an explicit clear from a configured recovery publisher;
revocation does not cause fallback to another publisher's older green verdict.
The caller must freeze the versioned publication intent in its existing outbox
before sending. Retries reconcile native statuses and comments; an older intent
superseded by a newer publication stops rather than overwriting that publication.
Each publication uses its own comment rather than editing another intent's
scoreboard. In-progress review publications and initial scoreboards defer
admission and promotion; they do not create permanent promotion holds.
The queue remembers historical publisher IDs in its existing settings when
admission starts. Removed issuers remain revocation barriers across restarts,
including for CI statuses; removing an issuer cannot reveal an older green
result. Preserve those settings when restoring the queue. Publisher bootstrap
and credential rotation must use the same issuer history as the consumer.
Forgejo Actions remain disabled on the source repository.

Promotion rereads the accepted PR heads and current review evidence. The trusted
CI coordinator separately verifies the exact frozen candidate's build, scope and
bump results. The source adapter additionally checks the actual candidate's changed
paths against the current admission prefix and allow-files, so a broader CI scope
configuration cannot bypass that restriction. A repaired candidate does not need
to make the original PR head's failed build green. Source read failures remain unavailable; they do not become
empty discussions or positive approval. GitHub App checks and GitHub quota are
unsupported source capabilities here; Forgejo requires primary-policy admission.
Native human-owned infrastructure approvals remain disabled in admission until
their command intake and matching promotion checks are integrated.

Discovery reads listing metadata and leaves detailed evidence to each PR's
admission work item. Stable discovery metadata does not continuously re-dirty an
evaluated PR. Native publication and CI status events invalidate their PRs;
a fifteen-minute backstop also refreshes permissions and missed events.
Immutable commits already present in the controller clone are reused.
An unreadable or changing PR cannot abort discovery for its siblings.
This is a functional baseline, not a capacity result; query volume,
backstop refreshes and history retrieval still need measurement and bounded
caching before the concurrency rehearsal is complete.

Native queue commits now carry host-qualified `Source-PR` trailers and preserve
the accepted Git commit's author rather than inventing a GitHub noreply address.
Historical GitHub attribution remains readable; native landing checks do not
reuse GitHub PR-number aliases as native authority.

A task produced by the native adapter advertises its protected input refs under
`refs/heads/tauceti-ci/queue/`. Untrusted worker proposals use a separate
`refs/heads/tauceti-submissions/<queue-epoch>/<task>/<claim>` namespace. The
controller fetches and locally retains the exact submitted commit before
validation/adoption, including rejected submissions; its durable outbox publishes
the protected retention ref. Proposal
refs confer no merge authority. Task metadata validates the ref transport version,
queue epoch and claim identity; native execution remains disabled until source
credentials, runner isolation, checkpoint/cache namespaces and claim recovery are
integrated. Trusted Review publication must emit the new source binding, and the
service bootstrap must wire the source and execution adapters independently.

Run the isolated real-source controller test with:

```sh
FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo_source.py
```

It exercises real HTTPS source reads, native publisher revocation, spoofed
statuses, unavailable authority, label/comment publication, primary admission,
protected candidate retention and source branch promotion with the shared-state
and legacy GitHub source paths blocked. CI observations in this fixture are
deterministic; it does not prove the separate real GitHub Actions rehearsal.
