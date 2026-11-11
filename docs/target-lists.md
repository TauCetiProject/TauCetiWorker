# Target lists

A target list is a markdown file an operator writes to steer roadmap authoring
toward one goal that spans several roadmaps: the milestones on the way to it,
grouped by roadmap area, each naming the items that have to land first. A worker
given a list authors only from it. Every roadmap round it takes one open item
whose prerequisites have landed, claims it, and hands the agent that item.

Turn it on with `--roadmap-targets FILE`, `TAUCETI_ROADMAP_TARGETS`, or the
`roadmap_targets` key in `workers.toml`. Several workers, on one host or many,
may be given copies of the same list. Without the setting nothing changes.
A list authors on GitHub: its live view reads GitHub pull requests, so a worker
whose new pull requests go to Forgejo (`--new-pr-forge forgejo`) refuses to
start with one; `--forgejo` alone, which also maintains Forgejo pull requests
while authoring on GitHub, keeps it.

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
  once work has started.
- A trailing parenthesised group holds `key: value` clauses separated by `;`,
  with no parentheses inside. `needs:` is a comma list of slugs from any area,
  or `none`. `partial: #N, #M` names merged pull requests that did not complete
  the item. Every other clause is passed to the agent as written.
- An item line that does not parse is logged and skipped. A `needs:` slug the
  file never defines is logged and blocks nothing.

The file is read at the start of each roadmap round, so an edit takes effect on
the next round. The worker never writes to it.

## Live statuses

The marks in the file go stale between edits, so each round overlays them:

- An open pull request whose body carries
  `<!--tauceti-target:v1 {"focus":"<Area>","id":"<slug>"}-->` puts that item in
  flight.
- A merged one marks it done, unless its marker also says `"partial":true` or
  the item's `partial:` clause names the pull request. A pull request Bors
  landed, closed under a `[Merged by Bors] - ` title, counts as merged.

The overlay only moves an item forward: `[x]` in the file stays done and `[~]`
stays in flight until the file changes. The merged side reads the 200 most
recently updated closed pull requests that carry a target marker. An older
landing is still found before its item is assigned again (see below), but it
no longer unlocks the items that need it, so tick an item `[x]` in the file to
make its landing permanent.

## Selection

An item is eligible when it is open under the live view and every item it needs
is done. With no area pinned, the worker considers every area that has an
eligible item, minus `--roadmap-skip`, in random order, and each area's items
in file order. `--roadmap-only AREA` restricts it to that area. Fleet roadmap
focus does not apply to a worker with a list.

For each candidate in turn the worker first searches the closed pull requests
for that slug's marker, and passes over an item that a merged pull request of
any age completed, logging the pull request so the file can be ticked. It then
acquires `author/<Area>/<slug>` in its
[claim namespace](reference.md#the-claim-namespace) and keeps the lease renewed
until the round ends. A candidate another worker holds is passed over. A claim
that cannot be registered does not stop the round. The claim is cooperative: it
keeps workers that share a list off each other's items, and the live view
covers the rest.

When the list offers nothing (every open item waits on its needs or is in
flight, or eight candidates in a row were passed over) the round ends with that
reason, no model is started, and the loop backs off as after any round without
progress. The same happens when pull requests cannot be read. The list never
changes which pull requests maintenance and review take up, only the order
[below](#maintenance-order).

A roadmap round under a list costs one listing of open pull requests, one
search for recent landings, and one search and one claim push per candidate
tried. The searches are shared between workers of one account on a host for two
minutes.

## Maintenance order

A worker with a list also puts the list first when it tends the account's own
pull requests. A pull request serves the list when its body carries a target
marker, complete or partial, for one of the list's items.

- Across `rebase`, `fix-ci` and `fix`, work on the account's pull requests that
  serve the list is offered before work on its others, so a round repairs a
  red build on a list item before it rebases an unrelated pull request.
- In `review`, among the account's own pull requests, those that serve the
  list are offered first.

Each group keeps the order it would have had, and every other candidate keeps
its position: Mathlib bump work stays in front, `lint-repair`, `progress` and
other accounts' pull requests are offered exactly when they would have been,
and `--pr` and `--only` narrow the round as before. The "Next auto round would"
line of `tauceti status` and the TUI does not take the list into account.

The order costs one more read of the file and one listing of open pull
requests, in rounds where at least two of the account's pull requests have
work to offer. When either cannot be read, the round logs it and keeps the
usual order.

## What the agent sees

The roadmap prompt gains one bullet at the top of "Choose a target": the
assigned slug and area, the instruction to author that item or a prerequisite
inside the same roadmap and nothing else, the exact marker for the pull request
body (with `"partial":true` when the pull request lands only part of the item),
and the area's items with their live statuses under the file's preamble.
`gh-safe-pr-create` refuses a body that does not carry the assigned item's
marker.

## Runtime status

While an authoring round runs, the worker's runtime status file carries
`roadmap_target`, the item as `<Area>/<slug>`. The loop clears it between
rounds.
