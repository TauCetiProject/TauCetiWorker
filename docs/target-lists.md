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
  or `none`. `partial: #N, #M` names merged GitHub pull requests that did not
  complete the item; a Forgejo pull request is named by its URL. Every other clause is passed to the agent as written.
- An item line that does not parse is logged and skipped. A `needs:` slug the
  file never defines is logged and blocks nothing.

The file is read at the start of each roadmap round, so an edit takes effect on
the next round. The worker never writes to it.

## Live statuses

The marks in the file go stale between edits, so each round overlays them:

- An open pull request, on any forge the worker discovers, whose body carries
  `<!--tauceti-target:v1 {"focus":"<Area>","id":"<slug>"}-->` puts that item in
  flight.
- A merged one marks it done, unless its marker also says `"partial":true` or
  the item's `partial:` clause names the pull request. A pull request Bors
  landed, closed under a `[Merged by Bors] - ` title, counts as merged.

The overlay only moves an item forward: `[x]` in the file stays done and `[~]`
stays in flight until the file changes. The merged side reads, on each forge,
the 200 most recent merged pull requests that carry a target marker (on GitHub
the most recently updated, on Forgejo the most recently created). An older
landing is still found before its item is assigned again (see below), but it
no longer unlocks the items that need it, so tick an item `[x]` in the file to
make its landing permanent.

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
progress, unless the worker may
[author outside the list](#authoring-outside-the-list). The same happens when
pull requests cannot be read. Maintenance and review are unaffected by the list.

A roadmap round under a list costs, on each forge it reads, one listing of
open pull requests, one search for recent landings, and one search per candidate
tried, plus one claim push per candidate. The GitHub searches are shared
between workers of one account on a host for two minutes.

## Authoring outside the list

`--targets-fallback-max-open N` (`TAUCETI_TARGETS_FALLBACK_MAX_OPEN`, or
`targets_fallback_max_open` in `workers.toml`) gives a round the list offers
nothing something else to do: it authors as a worker without a list does. The
round draws a random area outside `--roadmap-skip`, or keeps the area of
`--roadmap-only`, the agent gets the ordinary roadmap prompt and chooses its
own target, and the pull request serves no item of the list. Fleet roadmap
focus does not apply to it. Without the setting no round authors outside the
list.

Such a round runs only while these two together number fewer than N:

- the account's open, non-draft pull requests whose body carries no target
  marker naming an item of the list, whoever opened them, plus
- the rounds on this host that are authoring outside the list now. Each holds a
  slot, a locked file under `targets-fallback/<login>/` in
  `TAUCETI_GITHUB_SHARED_DIR`, from this check until it ends, so workers that
  reach the check together do not all pass it. A worker on another host is
  counted once its pull request is open.

`--targets-fallback-cap-file FILE` (`TAUCETI_TARGETS_FALLBACK_CAP_FILE`,
`targets_fallback_cap_file`) names a file holding one non-negative integer. A
round that gets this far reads it and uses that number in place of N, so a
controller can move the cutoff while workers run; `0` stops authoring outside
the list. While the file is missing or holds anything else, N applies and the
round logs why. The file does nothing without `--targets-fallback-max-open`.

When there is no room, or the open pull requests cannot be counted, the round
ends without progress, giving the list's reason and this one. The check costs
one more read of the open pull request listing, which workers share, and takes
no claim. `--max-open-prs` still stops every roadmap round, list or not, before
any of this.

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
`roadmap_target`, the item as `<Area>/<slug>`, and `roadmap_work`: `list` for a
round on an item, `outside-list` for a round authoring outside the list. The
loop clears both between rounds, and a worker without a list sets neither.
