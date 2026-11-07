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
