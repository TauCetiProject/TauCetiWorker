# Worker operation across GitHub and Forgejo

TauCeti workers look only at GitHub unless Forgejo is explicitly enabled with a
flag. The defaults are `--github --no-forgejo`. This keeps existing workers on
GitHub while an operator supervises individual Forgejo trials. Review and repair
follow each selected PR's original forge. Native user IDs determine ownership
on Forgejo. New authored PRs default to GitHub independently of discovery.

```sh
tauceti work --dry-run                       # GitHub only
tauceti work --forgejo --dry-run             # GitHub and Forgejo
tauceti work --forgejo --loop --worker-id supervised-forgejo
tauceti work --no-github --forgejo --pr 2 --only review
tauceti work --forgejo --new-pr-forge forgejo # Both inputs; new PRs on Forgejo
tauceti status --forgejo --json
```

`--github`, `--no-github`, `--forgejo`, and `--no-forgejo` independently enable
or disable discovery on each source. Each switch takes effect for the current
invocation and its loop children. Repeating opposite switches uses the last
one. Disabling both sources is an error. The switches also work before a
subcommand and select discovery in the bare dashboard.

`--new-pr-forge github|forgejo` independently chooses the destination for new
PRs. Numeric `--pr` targets require a single selected source, so a native target
uses `--no-github --forgejo`. With no source flags, numeric targets use GitHub.
The old `--forges` selector remains an undocumented compatibility alias and
cannot be combined with the new source switches. Inherited runtime source
variables do not enable Forgejo in a new invocation; loop children receive
explicit flags from their parent.

Native Forgejo content configurations remain available with shared roadmap,
rubric, tool, and archive repositories on GitHub, and require explicit source
flags as above.

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
explicit bindings. Set `TAUCETI_FORGEJO_USER_ID` to the native user ID the
token must belong to, and each round first checks it: a different user, an
HTTP 401, or an HTTP 403 from `GET /user` is an
[identity halt](workers.md#identity-halt) that stops the worker until a person
clears it. Token files must be private. A missing canonical credential
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
source repository and native actor IDs. Failed reviews release generated source
inputs after retaining any paid plan; crash cleanup requires a stopped owner
and no surviving users. Reviews reuse the existing shared Mathlib source cache.
Small publication receipts remain alongside the existing ledger and archive
outbox. A crashed native claim may wait for its old lease to expire; each round
keeps a distinct owner nonce so a surviving process cannot be silently replaced.

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

Finding roots retain their reviewed head; a new head receives new roots. Native
UI reply recognition uses the containing review and line location. Replies
started as a separate review, or unmarked replies to a conversation finding,
need the source-bound reply command to identify their parent reliably. The
shared native claim repository must have Actions disabled. Matching target
markers identify the same work target; contributors pursuing independent work
must give their proposals distinct target IDs.
