# Worker operation across GitHub and Forgejo

TauCeti workers discover PR work on GitHub and Forgejo by default. New authored
PRs initially go to GitHub. Review and repair follow each existing PR's original
forge. Native user IDs determine ownership on Forgejo.

```sh
tauceti status --json
tauceti work --dry-run
tauceti work --only review,fix
tauceti work --forges github
tauceti work --forges both --new-pr-forge forgejo
tauceti work --forges forgejo --pr 2 --only review
```

`TAUCETI_WORKER_FORGES` accepts `github`, `forgejo`, or `both`.
`TAUCETI_NEW_PR_FORGE` accepts `github` or `forgejo`. Managed workers can set
these through their existing environment table. CLI flags override those values.
A numeric `--pr` uses GitHub by default. Choose `--forges forgejo` to target a
Forgejo PR. An explicit `--forges both --pr NUMBER` requires a single forge
selection to disambiguate the number.

Other projects with an existing GitHub configuration continue using GitHub.
Native Forgejo content configurations are supported with shared roadmap, rubric,
tool, and archive repositories on GitHub.

## Forgejo access

Authenticated native work needs a private token with `read:user`,
`write:repository`, and `write:issue` scopes. The `issue` scope covers PR
discussion reads and comments. The server enforces repository permissions and
protected-branch rules for each write.

```sh
export TAUCETI_FORGEJO_ORIGIN=https://git.taucetiproject.org
export TAUCETI_FORGEJO_REPO=TauCetiProject/TauCeti
export TAUCETI_FORGEJO_REPOSITORY_ID=1
export TAUCETI_FORGEJO_TOKEN_FILE="$HOME/.config/tauceti-forgejo/worker-token"
```

The canonical instance defaults to those values. Other instances require
explicit bindings. Token files must be private. A missing canonical credential
allows public Forgejo discovery. Authenticated native work becomes available
when a suitable credential is configured.

Agents use familiar `gh pr` and `gh api` commands through a source-bound wrapper
during native rounds. Explicit GitHub selections and shared-tool operations use
the GitHub CLI. Native commands validate the server, native repository ID, and
selected native account. Unsupported commands report their missing contract.

## Scheduling and recovery

Each source has separate PR identity, counters, checkout, review store, and
retained publication records. GitHub's existing paths and counters continue to
be used. Qualified native Git leases coordinate reviews and branch repair.
Existing expected-head Git pushes protect branch updates. Author target leases
use the existing shared GitHub namespace on both sources. Authoring also reads
the union of known target markers. Native reviews require write access to the
shared native claim repository (the content repository in Stage 1B).

Selection compares both inventories. Matching heads, matching roadmap target
markers, and an explicit `Origin-PR: URL` line identify copied work. Discovery
prefers the GitHub proposal when matching work is open on both sources. A copy
that names its originating Forgejo PR keeps work on that original. Each PR's
reviews and publication authority stay bound to its source.

An unavailable forge is reported independently. Verified PR work on the other
source can continue. Authoring new roadmap work requires the selected inventories
to be available so the worker can check existing targets. Open-PR pressure is
counted across both sources.

Native review uses the installed review engine with exact source commits,
pinned rubrics, source-qualified provenance, and its existing ledger and archive
outbox. Inference receives prepared files and provider credentials. Source
credentials stay in the trusted preparation and publication process.

Publication retains the rendered plan before writing. Findings are published
and read back before the scoreboard. Retrying adopts the same remote markers.
A changed head supersedes an older publication. Pending publication is reconciled
before provider selection or another paid review on that head. Repair recovery checks the original
source repository and native actor IDs.

## Execution coverage and validation

Native agent rounds currently run in host mode. GitHub workers retain host and
Bubble execution. A Bubble worker can discover both inventories and execute
eligible GitHub work. Native reviews report execution CI as unknown until
Stage 1C supplies trusted evidence for mixed queue admission and landing.

`tests/worker_sources.py` covers defaults, destinations, identities, duplicates,
outages, counters, and the installed engine with a deterministic reviewer fixture.
`tests/integration_forgejo_worker.py` starts stock Forgejo over HTTPS and tests
the actual token scopes, PR creation, Git claims, source preparation, publication,
retry recovery, repair selection, replies, and stale-head refusal.

```sh
python tests/worker_sources.py
FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo_worker.py
```
