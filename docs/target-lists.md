# Target lists

A target list is a markdown file an operator writes to steer roadmap authoring
toward one goal that spans several roadmaps: the milestones on the way to it,
grouped by roadmap area, each naming the items that have to land first. A worker
given a list authors only from it. Every roadmap round it takes one open item
whose prerequisites have landed, claims it, and hands the agent that item.

Turn it on with `--roadmap-targets FILE`, `TAUCETI_ROADMAP_TARGETS`, or the
`roadmap_targets` key in `workers.toml`. Several workers, on one host or many,
may be given copies of the same list. Without the setting nothing changes.
A list works on whichever forge new pull requests go to (`--new-pr-forge`, see
[forgejo-worker.md](forgejo-worker.md)), and a worker that discovers both forges
reads both for the live view.
A worker that discovers only Forgejo still reads the GitHub content
repository's pull requests that carry a target marker, so a list carried over
from GitHub keeps its GitHub items in flight or done: one search of the 200 most
recently updated such pull requests per round, shared between workers of one
account on a host for two minutes, and, once that history is longer than 200,
up to two more searches for each candidate the listing does not settle, one for a
landing on the base branch and one for an open pull request on any branch, so an
older GitHub pull request that completed the item or still holds it is found. If GitHub cannot be read the round goes on
with the list's own marks for those items and logs why. Once the file marks
every GitHub-era item, set `TAUCETI_ROADMAP_TARGETS_GITHUB=0` (in the worker's
environment, or its `env` in `workers.toml`) to stop that read.

## The file

```markdown
# Law of large numbers
<!-- tauceti-targets:v1 -->
Goal: the strong law for i.i.d. integrable variables. Everything before the
first `## ` heading is shown to the agent with the items of its area.

## MeasureTheory
- [x] `borel-cantelli` — both Borel–Cantelli lemmas (serves: G1)
- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; needs: `borel-cantelli`)

## Probability
- [~] `truncation-bounds` — variance bounds for truncated variables (needs: none; in flight: #1234)
- [ ] `strong-law` — the strong law (serves: G1; needs: `borel-cantelli`, `truncation-bounds`)
> A line that is not an item is ignored.

## Notes (not targets)
- [ ] `second-moment-route` — nothing under this heading is a target
```

- The `<!-- tauceti-targets:v1 -->` marker is required. A file without it is not
  a target list, and the worker stops rather than author against it.
- A `## ` heading that is a roadmap directory name (letters, digits, `-`, `_`)
  opens that area. Under any other `## ` heading nothing is a target.
- An item is `- [ ]` (open), `- [~]` (in flight) or `- [x]` (done), then the
  slug as the first backtick token, then ` — ` and a description. Slugs are
  unique across the file. A slug becomes the claim key `author/<Area>/<slug>`
  and the `id` of the pull request's target marker, so it should not change
  once work has started. Slugs use letters, digits, `_`, `-` and `.`, start
  with a letter or digit, and contain neither `..` nor a trailing `.lock` or `.`.
  This keeps them valid both in Git claim refs and in target markers.
- A trailing parenthesised group holds `key: value` clauses separated by `;`,
  with no parentheses inside. `needs:` is a comma list of slugs from any area,
  or `none`. `partial: #N, #M` names merged GitHub pull requests that did not
  complete the item; a Forgejo pull request is named by its URL, in any case. Every other clause is passed to the agent as written.
- Malformed item lines, empty/invalid or duplicate slugs, undefined `needs:`
  references and dependency cycles reject the list before any model starts.
  Prerequisites may appear later in the file or in another area.

The file is read at the start of each roadmap round, so an edit takes effect on
the next round. The worker never writes to it.

## Live statuses

The marks in the file go stale between edits, so each round overlays them:

- An open pull request, on any forge the worker discovers, whose body carries
  `<!--tauceti-target:v1 {"focus":"<Area>","id":"<slug>"}-->` puts that item in
  flight.
- A merged one, into the branch the worker authors against
  (`TAUCETI_BASE_BRANCH`, default `main`), marks it done, unless its marker also says `"partial":true` or
  the item's `partial:` clause names the pull request. A pull request closed by the queue counts only with verified landing evidence
  on the configured base branch. A `[Merged by Bors] - ` title is only a hint:
  on GitHub the queue App’s canonical landing record must name an ancestor of
  the base; on Forgejo that landed commit must also bind the exact forge,
  repository ID and PR in its queue trailers and contain a nonempty change.
  Missing or forged proof never unlocks dependent work. For older queue
  landings without verifiable records, an operator can explicitly mark `[x]`.
  Native comparisons exceeding the client’s response bound also establish no
  completion; they do not stall the whole source. Per-item searches remain bounded
  (30 matching PRs), so exceptionally long partial-PR histories may likewise need
  an explicit operator completion. Source response bounds remain enforced.

The overlay only moves an item forward: `[x]` in the file stays done and `[~]`
stays in flight until the file changes. The merged side reads, on each forge,
the 200 most recent pull requests merged into the base branch that carry a target marker (on GitHub
the most recently updated, on Forgejo the most recently created among the closed
pull requests its client's page bound reaches). A marker counts whatever the
spacing of its JSON. An older
landing is searched by item before eligibility is computed for any unresolved
prerequisite, so it continues to unlock dependent items after leaving this recent
window. Candidate items are also checked before assignment. These reads preserve
partial-marker, explicit `partial:` and base-branch checks. Complete recent
listings avoid these per-item reads, and completed/in-flight items do not trigger
searches for their own prerequisites. Ticking `[x]` in the
file records an operator completion and avoids repeated historical lookups.

## Selection

An item is eligible when it is open under the live view and every item it needs
is done. With no area pinned, the worker considers every area that has an
eligible item, minus `--roadmap-skip`, in random order, and each area's items
in file order. `--roadmap-only AREA` restricts it to that area. Fleet roadmap
focus does not apply to a worker with a list.

For each candidate in turn the worker first searches each forge's closed pull
requests for that slug's marker (on Forgejo, the keyword search), and passes over an item that a merged pull request of
any age completed, logging the pull request so the file can be ticked. It then
acquires `author/<Area>/<slug>` in its
[claim namespace](reference.md#the-claim-namespace) and keeps the lease renewed
until the round ends. Author claims stay in that GitHub namespace when the pull
request goes to Forgejo, as all author leases do. A candidate another worker holds is passed over. A claim
that cannot be registered does not stop the round. The claim is cooperative: it
keeps workers that share a list off each other's items, and the live view
covers the rest.

When the list offers nothing (every open item waits on its needs or is in
flight, or eight candidates in a row were passed over) the round ends with that
reason, no model is started, and the loop backs off as after any round without
progress. The same happens when pull requests cannot be read. Maintenance and
review are unaffected by the list.

A roadmap round under a list costs, on each forge it reads, one listing of
open pull requests, one search for recent landings, and one search per candidate
tried, plus one item search per unresolved prerequisite missing from the recent
landings, and one claim push per candidate. The GitHub searches are shared
between workers of one account on a host for two minutes.

## What the agent sees

The roadmap prompt gains one bullet at the top of "Choose a target": the
assigned slug and area, the instruction to author that item or a prerequisite
inside the same roadmap and nothing else, the exact marker for the pull request
body (with `"partial":true` when the pull request lands only part of the item),
and the area's items with their live statuses under the file's preamble.
`gh-safe-pr-create` refuses a body that does not carry the assigned item's
marker; in a Forgejo round its `gh pr create` goes through the source-bound
`gh`, so the check is the same on both forges.

## Runtime status

While an authoring round runs, the worker's runtime status file carries
`roadmap_target`, the item as `<Area>/<slug>`. The loop clears it between
rounds.

## Fleet inspection contract

`tauceti _targets-inspect --file /absolute/path/targets.md` reads and validates
the file offline and emits `tauceti-target-list/v1` JSON with canonical items,
areas and saved-file summary counts. It performs no forge, credential or model
reads. Invalid files exit nonzero with a diagnostic rather than a success object.
Fleet uses this interface from a compatible Worker runtime instead of duplicating
the grammar. Its counts describe the file; the worker round additionally overlays
forge evidence. `_fleet-protocol` and the running manager advertise
`targets_inspect: 1` and `roadmap_targets: 1`.

## Maintenance priority

A configured list also gives the account's marked pull requests priority within
`rebase`, `fix-ci`, `fix`, and own-PR `review` queues. Complete and partial target
markers both count when their area and id name an item in the file, including
items marked done: their PRs may still need repair or review. Other authors'
review positions and automatic Mathlib bump priority stay unchanged. Listed work
never jumps across stages: an unlisted rebase still precedes a listed fix, and
lint repair and progress reports keep their existing order. Unlisted eligible
pull requests remain available after the listed ones.

Priority uses the current source's surveyed PR inventory. For a compact GitHub
survey, an optional mirror inventory supplies marker bodies only when the PR
number and head match; unavailable coverage keeps the ordinary ordering without
a direct GitHub fallback. A native source uses its own markers, so equal numbers
on another forge do not confer priority. In a mixed-source round, stage selection
across sources remains unchanged; the selected source's candidates are ordered
within that stage. `--pr` still limits which candidates can run. If the file
cannot be read or parsed, maintenance keeps its usual order and logs the reason;
roadmap authoring retains the foundation's stricter file validation.
