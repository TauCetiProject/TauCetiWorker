# Incident records

An incident record is a small JSON file a worker writes when a round concludes
something about a pull request that its forge does not show. Two kinds exist:

- `declined`: a `rebase`, `fix`, or `fix-ci` round finished and left no push,
  closure, or reply. The record keeps the agent's final message.
- `budget-spent`: a stage has used its attempts on a pull request and will not
  take it again at that head.

Nothing is written unless `TAUCETI_INCIDENTS_DIR` is set, and records alone
change no selection.

The opt-in [decide phase](decide.md) reads these two kinds and writes a third,
`ruling`, into the same directory.

## Settings

| Variable | Default | Effect |
| --- | --- | --- |
| `TAUCETI_INCIDENTS_DIR` | _(unset)_ | Directory for the records. Give every worker of a fleet the same one. Created with mode `0700` when missing. |
| `TAUCETI_HOLD_DECLINED` | _(unset)_ | `1` keeps a stage off a pull request head that a `declined` record names. Needs the directory. |

A [managed worker](workers.md) gets both through its `env` table.

## Directory layout

```
<dir>/<kind>-<key>.json          live
<dir>/acked/<kind>-<key>.json    acknowledged
<dir>/acked/<kind>-<key>.json.<n> earlier acknowledged records of that name
<dir>/.decide.lock               the decide phase's lock and last-round time
<dir>/.records.lock              held while a live record is written or acknowledged
<dir>/drafts/roadmap-<pr>.md     roadmap changes the decide phase drafted
<dir>/closures.log               one line per closure the decide phase attempted
```

The worker writes live files only, each by a rename from a temporary file in
the same directory (`.<name>.<random>.tmp`, one per write), so a reader never
sees a partial record. It never deletes a file, and it moves one only in the
decide phase, which acknowledges the records it has settled.

Whoever reads the records acknowledges one by moving it into `acked/` under the
same name, and discards one by deleting it. A hyphen can occur in both `<kind>`
and `<key>`, so take them from the fields inside the file.

### Keys and sources

A record is about one pull request of one source repository, a GitHub or a
Forgejo repository. Its `<key>` is:

- `<stage>-<pr>` for a pull request of the GitHub repository the worker is
  configured for (`TAUCETI_REPO`, `TauCetiProject/TauCeti` by default), so
  `declined-fix-412.json`;
- `<stage>-<pr>.<digest>` for a pull request of any other repository, where
  `<digest>` is the first 12 hexadecimal digits of the SHA-256 of the UTF-8
  string `<forge kind>:<forge origin>/<owner/name in lower case>`, which is
  `RepositoryRef.key` in `tauceti_tools.forge`. For example a Forgejo pull
  request 412 of `TauCetiProject/TauCeti` at `https://forge.example` is keyed
  by the digest of `forgejo:https://forge.example/taucetiproject/tauceti`.

`incidents.subject(stage, pr, repository)` computes the key. The record's
`repository` and `forge` fields name the source, and a reader that needs it
takes it from there rather than from the digest. A directory serves one
configured GitHub repository: workers configured for different ones each name
their own repository's records `<stage>-<pr>`, so they need separate
directories.

`find(kind, key)` reads `<dir>/<kind>-<key>.json` and then
`<dir>/acked/<kind>-<key>.json`, so a key built for one source never finds
another source's record. `declined(stage, pr, head, repository)` and the
`budget-spent` once-per-head check look up `subject(stage, pr, repository)`
that way. `records()` lists every source; filter on `repository` and `forge`.

## Fields

Every record has:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `tauceti-incident/v1` |
| `kind` | string | `declined` or `budget-spent` |
| `key` | string | `<stage>-<pr>`, or `<stage>-<pr>.<digest>` for another source (see above) |
| `worker` | string | Id of the worker that wrote the file last |
| `first_at`, `last_at` | string | UTC times of the first and the latest write, `YYYY-MM-DDTHH:MM:SSZ` |
| `count` | integer | Number of times the live file has been written |
| `repository` | string | `owner/name` of the pull request's repository |
| `forge` | object | `{"kind": "github" or "forgejo", "url": <origin>}` of that repository |
| `repository_id` | integer | The repository's native id on a Forgejo source; absent for GitHub |
| `stage` | string | `rebase`, `fix`, or `fix-ci` |
| `pr` | integer | Pull request number |
| `head` | string | Full head commit the record is about |

A `declined` record adds:

| Field | Type | Meaning |
| --- | --- | --- |
| `reason` | string | Why the survey offered the pull request, for example `conflicting` |
| `summary` | string | The agent's final message, cut to 2000 characters. For an agent without a structured transcript, the last lines of its output |
| `subsumed_by` | integer list | Pull requests named on the last `Subsumed-by:` line of that message |
| `mentions` | integer list | Other pull requests the message refers to as `#N` |
| `transcript` | string | The round's transcript file on the worker's host; its log directory for a streamed round |

A `budget-spent` record adds `detail`, one line naming the survey's reason and
the attempts used.

```json
{
 "count": 1,
 "first_at": "2026-01-15T09:30:00Z",
 "forge": {"kind": "github", "url": "https://github.com"},
 "head": "5f2c9d0e8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d",
 "key": "rebase-412",
 "kind": "declined",
 "last_at": "2026-01-15T09:30:00Z",
 "mentions": [],
 "pr": 412,
 "reason": "conflicting",
 "repository": "TauCetiProject/TauCeti",
 "schema": "tauceti-incident/v1",
 "stage": "rebase",
 "subsumed_by": [398],
 "summary": "Main already has every declaration of this PR ...\nSubsumed-by: #398",
 "transcript": "/home/worker/state/w1/logs/agent-claude-20260115-091200.log",
 "worker": "w1"
}
```

## When a record is written

**`declined`.** The agent exited normally and the pull request then shows the
head it started from, no closure, and no new comment; for a round that had to
push or close, a comment does not count. A later declined round of the same
stage on the same pull request rewrites the file, so it describes the latest
one. Rounds on an automatic Mathlib bump are not recorded.

While the directory is set, the three repair prompts end with one more
paragraph: it tells the agent that a final message with no outcome on GitHub is
recorded, and asks for the reason first and a closing `Subsumed-by: #N [#M ...]`
or `Subsumed-by: none` line.

**`budget-spent`.** At the start of a round, for each pull request the survey
suppressed in one of the three stages because its attempts are used up; with a
Forgejo source enabled, for every source that answered its survey, whether or
not it has work to offer. One
write per head: a record that already names the head, live or acknowledged, is
left alone, and a new head writes a new live record. Attempt budgets are kept
per worker, so the record is the first worker's; another worker may still have
attempts for that head. `--dry-run` writes nothing.

## Holding a declined head

With `TAUCETI_HOLD_DECLINED=1`, a stage takes no further round on a pull request
while a `declined` record for that stage, live or acknowledged, names its
current head. The survey lists the pull request as suppressed with the reason
`an earlier round declined at this head; waiting for a new head`, and dispatch
checks again before spending and once more after taking the branch claim, for a
record written after the survey or by a peer that just released the claim.
Recovery of a repair round's retained source waits on a held head too, after
reconciling any push that round already made and before it launches an agent.

The hold ends when the head moves, when the record is deleted from both
folders, or when the decide phase rules `retry` on the latest record for that
head. It covers every action of the stage at that head, including the final
resolution attempt and the closure after spent attempts that `fix` makes on a
pull request the account authored: a held head is left for a person.

## Stale records

The worker does not remove a record when its pull request merges, closes, or
gets a new head. A record is about `head`; compare it with the live pull request
before acting on it.

## Reading and writing from code

`tauceti_worker/incidents.py` is the only module that reads and writes the
records:

| Function | Use |
| --- | --- |
| `directory()` | The configured directory, or `None` |
| `record(kind, key, **fields)` | Write or refresh a live record; returns its path, or `None` when off or the write failed |
| `subject(stage, pr, repository=None)` | The key for a pull request of `repository` (a `RepositoryRef`, a GitHub `owner/name`, or `None` for `TAUCETI_REPO`) |
| `source_of(gh)` | The repository a worker's forge client serves, for `subject` |
| `find(kind, key)` | The live and the acknowledged record of that name, where they exist |
| `records(kind="", acked=True)` | Every readable record with its path, live ones first |
| `acknowledge(kind, key, version=None)` | Move a live record into `acked/`, keeping the one it replaces as `<name>.<n>`; with `version`, only while the live record is still that one |
| `record_declined(...)`, `note_budget_spent(...)` | The two kinds above; both take `repository=` and `repository_id=` |
| `declined(stage, pr, head, repository=None)` | Whether the hold applies |

A new kind needs only a name and a key that is unique within it.
