# The decide phase

`decide` rules on [incident records](incidents.md): the `declined` and
`budget-spent` records that repair rounds leave when a pull request cannot be
moved forward. For each one it asks a model to choose a ruling, checks the
answer, carries it out, and writes the ruling back as a record of its own, so
that the records still waiting for a person are the ones a person has to
decide.

It is outside the cascade: a worker runs it only when its `only` list names
`decide`. A round costs one listing of open pull requests; when there is
something to rule on it adds one model conversation for up to six pull
requests, and for each of those one read of its comments and one read per pull
request the incident names.

## Turning it on

```toml
[[workers]]
id = "decider"
only = ["decide"]

[workers.env]
TAUCETI_INCIDENTS_DIR = "/srv/tauceti/incidents"
```

`tauceti work --only decide` is the same thing by hand. `decide` may share a
worker with other phases (`only = ["decide", "fix", "rebase"]`): it then goes
first on the rounds where it is due, and the round moves on to the other phases
when it starts no model. One worker per incident directory is enough; a second
one finds the directory's lock held and stands aside.

| Variable | Default | Effect |
| --- | --- | --- |
| `TAUCETI_INCIDENTS_DIR` | _(unset)_ | Required. The directory whose records are ruled on. |
| `TAUCETI_DECIDE_GAP` | `600` | Seconds between rounds on one directory. |
| `TAUCETI_DECIDE_MAX_CASES` | `6` | Incidents put to the model in one round. |
| `TAUCETI_ROADMAP_TARGETS` | _(unset)_ | With a [target list](target-lists.md) set, `prerequisite` is offered and may add items to it. |
| `TAUCETI_DECIDE_CLOSE` | _(unset)_ | `1` lets a `close` ruling close a pull request. |
| `TAUCETI_DECIDE_MAX_CLOSES_PER_DAY` | `3` | Closures per UTC day across the directory. |

The phase is written for workers that also set `TAUCETI_HOLD_DECLINED=1`: a
declined head then waits for its ruling, and `retry` is what releases it.
Without the hold the rulings are still recorded, and the stage keeps taking the
pull request on its own.

## A round

A round is due when `TAUCETI_DECIDE_GAP` has passed since the last one and the
directory holds a live incident or a live ruling. It then:

1. Lists the open pull requests and acknowledges every live incident and ruling
   whose pull request has closed, merged, or moved to another head. No model is
   involved. The listing may come from a mirror or a short-lived cache, so a
   record it shows off its head is acknowledged only once a fresh read of that
   pull request agrees; one that cannot be read stays live.
2. Looks again at each live ruling that waits on something (see below).
3. Takes the live incidents, oldest first, up to the limit. One whose pull
   request carries `keep`, `hold`, `wip`, `human`, `on-hold` or `do-not-close`
   is left for a person, and one whose branch another worker has claimed is
   left for a later round.
4. Writes those cases to a file, runs the model on it, and checks the answer.
5. Applies each ruling, writes its record, and acknowledges the incident. An
   incident whose record a repair round rewrote while the model was out stays
   live, and is put to the model in its own right on a later round.

With a Forgejo source enabled as well, the phase runs on the GitHub source after
both sources are surveyed and ahead of either source's work, so a hold it lifts
is gone from the next round's survey rather than this one's. Incident records
are ruled on only for the worker's GitHub repository; a round in which GitHub
could not be surveyed does not rule. A record of another source, whose key
carries that source's digest (see [incidents](incidents.md)), is never put to the
model, acknowledged or settled by the phase; rulings are keyed the same way, so
a GitHub ruling neither lifts that record's hold nor adds a note to its prompt.

## The rulings

| Ruling | Meaning | What the worker does |
| --- | --- | --- |
| `retry` | The round stopped for a reason that is not about the pull request, or there is evidence it did not have. | Lifts the hold at that head, and adds the ruling's note to the stage's next prompt for it. Once per head, and for `declined` incidents only. |
| `wait` | Other open pull requests are doing the missing work. | Records them. When all have merged the ruling becomes a `retry`; when one closes unmerged the incident is put to the model again. |
| `escalate` | Anything else. | Nothing: the record carries the analysis and a recommendation for a person. |
| `prerequisite` | A roadmap milestone nobody is building blocks it. | Adds the milestone to the target list and waits for it as `wait` does. Offered only with a target list and a readable roadmap checkout. |
| `roadmap` | Only a change to a roadmap resolves it. | Writes the proposed change to a local file. Nothing is opened. Offered only with a readable roadmap checkout. |
| `close` | Merged pull requests already contain everything it adds. | Closes it after the checks below. Offered only with `TAUCETI_DECIDE_CLOSE=1`, on the account's own pull requests. |

A ruling that cannot be applied is recorded as `escalate` with the reason and
`recommend` set to the ruling that was asked for. So is a case the answer leaves
out or fills in against the schema. If the model run itself fails, nothing is
recorded and the incidents wait for the next round.

A `budget-spent` incident is never offered `retry`: attempt counts live in each
worker's own state, where a ruling cannot restore them. When what such a pull
request waited on has landed, the ruling becomes `escalate`.

## Ruling records

A ruling is an incident record of kind `ruling`, written to the same directory
as `ruling-<incident kind>-<stage>-<pr>.json`. Beside the
[common fields](incidents.md#fields) it has:

| Field | Type | Meaning |
| --- | --- | --- |
| `repository`, `stage`, `pr`, `head` | | Copied from the incident |
| `incident` | string | `declined` or `budget-spent` |
| `incident_at` | string | The incident's `last_at` when it was ruled on |
| `incident_version` | string | `incidents.version()` of the incident record it rules on: two writes of a record never share it, even within one second. A `retry` lifts the hold only while the latest `declined` record at its head has this version. |
| `ruling` | string | The ruling's name |
| `by` | string | `model`, or `recheck` for the follow-up to a ruling that waited |
| `note` | string | The reasoning, for whoever acts next; at most 1500 characters |
| `retried` | string list | Heads at which this pull request has had a `retry` |
| `blocked_on` | integer list | `wait`: the pull requests waited on |
| `recommend` | string | `escalate`: what the model would do, or the ruling that was refused |
| `blocked_on_targets`, `added` | string lists | `prerequisite`: the target slugs waited on, and those this ruling added to the list |
| `area`, `draft` | strings | `roadmap`: the roadmap, and the path of the drafted change |
| `subsumed_by`, `evidence` | integer list, string | `close`: the merged pull requests, and what was checked |

```json
{
 "blocked_on": [431],
 "by": "model",
 "count": 1,
 "first_at": "2026-01-15T09:42:00Z",
 "head": "5f2c9d0e8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d",
 "incident": "declined",
 "incident_at": "2026-01-15T09:30:00Z",
 "incident_version": "3c6d1f0a9b2e4d5c7a81",
 "key": "declined-rebase-412",
 "kind": "ruling",
 "last_at": "2026-01-15T09:42:00Z",
 "note": "The conflict is with #431, which rewrites the same file and is green.",
 "pr": 412,
 "repository": "TauCetiProject/TauCeti",
 "retried": [],
 "ruling": "wait",
 "schema": "tauceti-incident/v1",
 "stage": "rebase",
 "worker": "decider"
}
```

Live and acknowledged mean the following for the two kinds together:

- A live `declined` or `budget-spent` record has not been ruled on.
- A live `ruling` is in force.
  A live `roadmap` or `escalate` ruling is waiting for a person.
- The phase acknowledges an incident when it rules on it, and a ruling when its
  pull request closes or leaves that head. A person acknowledges a ruling that
  waited for them once they have dealt with it.

To have a pull request ruled on again, delete its ruling record and move the
incident from `acked/` back into the directory.

## The model's part

The model receives one file and writes one file. The first holds, per case:
the pull request's number, head, title, labels and description, the stage and
kind of the incident, the agent's final message, the state of each pull request
that message names (at most six), the six latest comments, the earlier ruling
if there was one, and the rulings it may choose from. Every text is cut to a
fixed length.
The file also names a read-only checkout of the roadmaps and the target list
file, when those are available.

The answer is `{"rulings": [...]}` with one object per case: `case`, `ruling`,
`note`, and exactly the fields of that ruling.

| Ruling | Fields |
| --- | --- |
| `retry` | none |
| `wait` | `blocked_on`: 1 to 5 pull request numbers |
| `escalate` | `recommend`: one line, may be empty |
| `prerequisite` | `items`: 1 to 3 of `{area, slug, text, needs}` |
| `roadmap` | `area`; `title`: one line; `proposal`: 80 to 12000 characters |
| `close` | `subsumed_by`: 1 to 3 pull request numbers; `comment`: 40 to 1500 characters |

The model runs on the host, as `progress` and `overlap-scan` do, with
`GH_TOKEN` and `GITHUB_TOKEN` removed from its environment, and with them the
copy of the environment from before `--github-account` selected an account.
Every write is made by the worker, after the checks on this page.

## What `prerequisite` writes

Each new item is added as the first item of its `## <Area>` section, so the
list's authors reach it before the area's other open items; an area the list
does not have gets a section at the end of the file:

```markdown
- [ ] `kolmogorov-zero-one` — Layer 2: tail events have probability 0 or 1 (unblocks: #412; needs: `borel-cantelli`; added by: decide)
```

The area must be a roadmap directory or a section of the list, every `needs`
slug must be on the list or an earlier item of the same ruling, never the item
itself, and a slug the list already has open or in flight is waited on without
being added. The file is rewritten by rename while holding an exclusive `flock`
on `.<file name>.lock` beside it; anything else that writes the list should take
the same lock. Under that lock each new item's `needs` are checked again against
the file as it then is, and a ruling one of whose needs has left the list
meanwhile writes nothing and is escalated. The phase does not commit the file,
and does not remove an item once the pull request it unblocks has gone.

## Roadmap drafts

A `roadmap` ruling writes `drafts/roadmap-<pr>.md` under the incident
directory: the title, the roadmap, the pull request it would unblock, and the
proposed change in the model's words. The worker opens nothing in the roadmap
repository; a person reads the draft and files the change, or does not.

## What `close` checks

`close` is the phase's only write to a pull request, and it is made only when
all of these hold at the moment of closing:

- `TAUCETI_DECIDE_CLOSE` is on, and fewer than
  `TAUCETI_DECIDE_MAX_CLOSES_PER_DAY` closures were attempted today (UTC),
  counted from `closures.log` in the incident directory. Each attempt appends
  one line (time, pull request, head) before the pull request is closed; a log
  that cannot be read or written refuses the closure.
- The worker holds the pull request's branch claim. Where the claim cannot be
  registered at all, a repair round goes on unclaimed; a closure does not.
- A fresh read shows the pull request open, not a draft, at the head the
  incident names, authored by the worker's own account, and under none of the
  hold labels listed above. It is made twice: before the diffs below are read,
  and again after, immediately before closing.
- Every pull request the ruling names as subsuming it has merged; a Bors
  landing counts.
- Every non-private declaration the pull request's diff adds to a `.lean` file
  (`theorem`, `lemma`, `def`, `abbrev`, `instance`, `structure`, `class`,
  `inductive`, `opaque`, `axiom`, compared by name as written) is also added by
  the diff of one of those merged pull requests. A pull request that adds no
  named declaration cannot be closed this way.

The closing comment is the model's `comment` followed by the evidence checked
and a line saying the closure was automatic and can be reopened. The comparison
is by name: a subsuming pull request that states the same result under another
name fails it and is escalated, and one that reuses a name for something else
passes it.
