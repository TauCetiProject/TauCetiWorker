# Incident records

An incident record is a small JSON file a worker writes when a round concludes
something about a pull request that GitHub does not show. Three kinds exist:

- `declined`: a `rebase`, `fix`, or `fix-ci` round finished and left no push,
  closure, or reply. The record keeps the agent's final message.
- `budget-spent`: a stage has used its attempts on a pull request and will not
  take it again at that head.
- `progress-stuck`: the roadmap repository's merge gate has left a progress
  report open through every ask `progress_shepherd` makes, and the worker has
  stopped asking.

Nothing is written unless `TAUCETI_INCIDENTS_DIR` is set, and records alone
change no selection.

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
```

The worker writes live files only, each by a rename from a temporary file in
the same directory (`.<name>.<pid>.tmp`), so a reader never sees a partial
record. It never moves or deletes a file.

Whoever reads the records acknowledges one by moving it into `acked/` under the
same name, and discards one by deleting it. A hyphen can occur in both `<kind>`
and `<key>`, so take them from the fields inside the file.

## Fields

Every record has:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `tauceti-incident/v1` |
| `kind` | string | `declined`, `budget-spent`, or `progress-stuck` |
| `key` | string | `<stage>-<pr>` for every kind |
| `worker` | string | Id of the worker that wrote the file last |
| `first_at`, `last_at` | string | UTC times of the first and the latest write, `YYYY-MM-DDTHH:MM:SSZ` |
| `count` | integer | Number of times the live file has been written |
| `repository` | string | `owner/name` of the pull request's repository |
| `stage` | string | `rebase`, `fix`, or `fix-ci`; `progress` for `progress-stuck` |
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

A `progress-stuck` record is about a pull request in the roadmap repository,
which `repository` names, and adds:

| Field | Type | Meaning |
| --- | --- | --- |
| `url` | string | The report's pull request |
| `area` | string | The roadmap the report is for |
| `opened_at` | string | UTC time the pull request was opened |
| `asks` | integer | Dispatches of the gate workflow this worker made for the report |
| `refusal` | string | The reason in the gate's newest refusal comment on the pull request, or empty when it left none |
| `detail` | string | One line with the report, its age, the asks or the rejected dispatch, and that reason |

```json
{
 "count": 1,
 "first_at": "2026-01-15T09:30:00Z",
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
suppressed in one of the three stages because its attempts are used up. One
write per head: a record that already names the head, live or acknowledged, is
left alone, and a new head writes a new live record. Attempt budgets are kept
per worker, so the record is the first worker's; another worker may still have
attempts for that head. `--dry-run` writes nothing.

**`progress-stuck`.** By a progress round of a worker with
[`progress_shepherd`](reference.md#open-progress-reports) set, when a report of
its account is still open four hours after the last ask, or as soon as GitHub
rejects the dispatch itself. One write per report: a record that exists, live
or acknowledged, is left alone, and no worker sharing the directory asks the
gate about that report again. Closing the pull request, or getting it landed,
is what frees its place under `progress_max_open`; deleting the record from
both folders does not restart the asks of the worker that wrote it.

## Holding a declined head

With `TAUCETI_HOLD_DECLINED=1`, a stage takes no further round on a pull request
while a `declined` record for that stage, live or acknowledged, names its
current head. The survey lists the pull request as suppressed with the reason
`an earlier round declined at this head; waiting for a new head`, and dispatch
checks again before spending, for a record written after the survey.

The hold ends when the head moves, or when the record is deleted from both
folders. It covers every action of the stage at that head, including the final
resolution attempt and the closure after spent attempts that `fix` makes on a
pull request the account authored: a held head is left for a person.

## Stale records

The worker does not remove a record when its pull request merges, closes, or
gets a new head. A record is about `head`; compare it with the live pull request
before acting on it.

## Reading and writing from code

`tauceti_worker/incidents.py` is the only module that touches the directory:

| Function | Use |
| --- | --- |
| `directory()` | The configured directory, or `None` |
| `record(kind, key, **fields)` | Write or refresh a live record; returns its path, or `None` when off or the write failed |
| `find(kind, key)` | The live and the acknowledged record of that name, where they exist |
| `records(kind="", acked=True)` | Every readable record with its path, live ones first |
| `record_declined(...)`, `note_budget_spent(...)` | The two kinds above |
| `declined(stage, pr, head)` | Whether the hold applies |
| `note_progress_stuck(repository, pr, head, **fields)`, `progress_stuck(pr)` | The third kind, and whether a report has a record |

A new kind needs only a name and a key that is unique within it.
