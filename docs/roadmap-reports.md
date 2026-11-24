# Roadmap reports

Workers author against human-owned roadmaps in
[TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap), and sometimes the
roadmap is wrong: a README statement is false or lacks a hypothesis, a `Suggested.lean`
pin is false, unprovable or ill-typed, a pinned definition has the wrong body, a
prescribed route is circular, or the roadmap cites a renamed declaration. Workers never
write to the roadmap repository or to the job board. Before roadmap reports they said so
only in the prose of a PR description, where nothing picked it up, and the roadmap stayed
wrong for weeks. A roadmap report is the one sanctioned channel: a marker in the
description of a library pull request, which the job board's maintenance harvests,
checks and turns into a `REPORT` job for the roadmap's maintainers.

## What the agent does

The authoring prompt, and the PR outcome instructions every repair round receives, tell
the agent: do not quietly prove a weaker statement under the roadmap's name. Prove the
corrected statement and, where feasible, the counterexample or obstruction as a theorem,
then add to the PR description a `### Roadmap report` section (the one heading an
authored PR description may have) saying what is wrong, why, and the corrected statement,
followed by one marker line per error:

```
<!--tauceti-roadmap-report:v1 {"roadmap":"QuadraticFormInvariants","file":"README.md","roadmap_sha":"<40 hex>","anchor":"...","kind":"false-statement","effect":"worked-around","refutes":["TauCeti.exists_rankOne_equivalent_not_diagonalChain"],"replaces":["TauCeti.diagonalChain_iff_equivalent_of_two_le"]}-->
```

| Field | Required | Value |
|---|---|---|
| `roadmap` | yes | The roadmap's folder under the roadmap repository's active directory, `/`-separated when nested; each segment matches `[A-Za-z][A-Za-z0-9]*`. |
| `file` | yes | `README.md` or `Suggested.lean`. |
| `roadmap_sha` | yes | The 40-hex roadmap commit the agent read. Authoring and repair rounds are given the commit of the read-only roadmap checkout they read (see below). |
| `anchor` | yes | For `Suggested.lean` the declaration's name, for `README.md` a verbatim quote of the statement or its distinctive part, at most 300 characters. It must occur in that file at `roadmap_sha`, compared with runs of whitespace as one space. |
| `kind` | yes | `false-statement`, `missing-hypothesis`, `unprovable-pin`, `wrong-definition`, `circular-dependency` or `stale-name`. |
| `effect` | yes | `worked-around` (the PR proves a corrected statement in its place) or `blocked` (it lands only a partial piece, and the error still blocks the target). |
| `refutes`, `replaces` | no | At most 10 fully qualified declaration names each: the counterexample theorems, and the corrected statements. |

A reader finds the literal prefix `<!--tauceti-roadmap-report:v1 `, takes the text up to
the next `-->`, and decodes it as one JSON object; unknown fields are ignored. A body
carries at most five markers: a sixth and later marker, or one that fails to parse or
validate, is invalid and the others still count. Two reports are the same report when
they agree on roadmap, file and anchor, the anchor compared case-folded with whitespace
normalised. Generated files (`STATUS.md`, `PROGRESS.md`) are out of scope.

`tauceti_worker/roadmap_reports.py` implements this reader. Agents check a body with
`roadmap-report-check [--roadmaps DIR] [FILE]`, staged beside the other wrappers; with
`--roadmaps`, the roadmap repository's active directory at the reported commit, it also
checks anchors. `gh-safe-pr-create` runs the same check on any body that carries a
report marker. A malformed report never stops the pull request: the wrapper prints what
is wrong and opens it anyway, so the agent can correct the description with
`gh pr edit`. That is deliberately weaker than the target marker, whose absence the
wrapper refuses: a report is a side channel, and a bad one must not cost a good pull
request. Repair rounds have no wrapper on their description edits; the prompt asks them
to run the checker, and the job board validates every marker again when it harvests.

Authoring and repair rounds read the roadmaps from the worker's read-only roadmap
checkout (`<state>/refs/roadmap`, mounted at `/opt/roadmap` under `--bubble`, whose
GitHub scope covers only the pull request's repositories). It is the shared snapshot
the roadmap and review phases already use: one shallow fetch per host and hour. The
prompt names its directory and commit, which is the report's `roadmap_sha`. If a repair
round cannot have the checkout, the repair goes ahead, and the prompt tells the agent to
write the `### Roadmap report` section without a marker and say so; a later round adds
the marker. An authoring round without the checkout does not start, as before.

A round that opens no pull request leaves no report: reports travel only in pull request
descriptions, where the work behind them is checked and reviewed.
