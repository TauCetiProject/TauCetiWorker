# Umbrellas

An umbrella is a named set of roadmap areas: everything one goal needs, for
example the roadmap for the Riemann hypothesis for Dirichlet L-functions and the
roadmaps it cites. `--roadmap-only` accepts an umbrella name where it accepts an
area name, so one command points a worker at the whole goal:

```bash
uv tool install git+https://github.com/TauCetiProject/TauCetiWorker
tauceti work --loop --full-quality --roadmap-only GRH
```

`TAUCETI_ROADMAP_ONLY=GRH` and `roadmap_only = "GRH"` in `workers.toml` do the
same.

## The file

The umbrella `NAME` is the file `umbrellas/NAME.toml` on the default branch of
the [roadmap repository](https://github.com/TauCetiProject/TauCetiRoadmap), the
repository whose `TauCetiRoadmap/` directories are the roadmap areas. The worker
reads it at the start of every round the way it reads the area list: from the
host's shared snapshot of that repository (refreshed at most hourly), else with
one GitHub contents request.

```toml
description = "Riemann hypothesis for Dirichlet L-functions and the roadmaps it cites"

[[area]]
name = "AnalyticNumberTheoryPartI"
sections = ["§1.6", "§3.2"]
weight = 4

[[area]]
name = "RiemannHypothesisForDirichletLFunctions"
```

- `description` is one line, shown to the agent and in logs. It may be omitted.
- Each `[[area]]` names one member, a top-level directory of `TauCetiRoadmap/`.
  `sections` (optional) lists what the goal cites in that area. `weight`
  (optional, a positive integer, default 1) makes the member that many times as
  likely to be drawn.
- Unknown keys are ignored, so the format can grow.

A file that does not parse (invalid TOML, a value of the wrong type, an area
listed twice) or lists no area stops every round with a message naming the
file, before any model starts. A loop backs off and retries as after any failed
round, so a fix merged into the roadmap repository takes effect without a
restart.

`NAME` is an umbrella only when no roadmap area has that name (an area always
wins) and the file exists. Any other name, including one whose file cannot be
read this round, behaves exactly as `--roadmap-only` did before umbrellas: it
names one area.

## Selection

A round with the roadmap phase enabled draws one member area at random, by
weight, among the members that are roadmap areas now and are not in
`--roadmap-skip`. Members whose roadmaps have not merged yet are skipped
silently, so the same command picks them up once they land. The log names the
draw:

```
ROADMAP area: AnalyticNumberTheoryPartI (umbrella GRH: picked from 2 of 3 members)
```

If no member is eligible the round ends with that reason before any model
starts. A round without the roadmap phase (`--only review,fix`) draws nothing
and does not check the members.

Authoring in the drawn area is that of `--roadmap-only AREA`: claims, the
intentions board and administrative holds apply to that area. The agent's
prompt names the umbrella and its description and, when the drawn member lists
`sections`, adds one sentence: within this area prefer the targets of those
sections, which the umbrella's goal cites; if all of them are done or claimed,
any other target of the area is acceptable. Nothing else in the prompt changes.

## Maintenance

Rebase, fix-ci, fix and review consider the pull requests of every member area
outside `--roadmap-skip`, not only of the area drawn this round, so the
umbrella's pull requests keep being maintained whichever area a round draws.
As for any scope, pull requests with no roadmap area stay in the shared pool.
With the shared GitHub state service, a round reads each member area's survey
as a worker pinned to that area would, so it asks for no history outside the
umbrella.
The open-PR authoring limit (`--max-open-prs`, see
[roadmap backpressure](reference.md#roadmap-backpressure)) counts your open
roadmap pull requests across those member areas together.

## With the other roadmap settings

| Setting | With an umbrella |
| --- | --- |
| `--roadmap-skip AREA[,AREA...]` | Removes those members from the umbrella: from the draw, from maintenance, and from a target list. A pinned area wins over the skip list; an umbrella does not, because skipping is how you leave one member to other workers. |
| `--roadmap-targets FILE` | The [target list](target-lists.md) chooses the item, restricted to the members a draw could pick (roadmap areas now, outside `--roadmap-skip`), as it is restricted to the area of `--roadmap-only AREA`. Items under a member that has not merged wait for it; when the list offers nothing else the round ends before any claim or model, naming the areas its eligible items are in. No area is drawn; the prompt names the umbrella but adds no section sentence, since the item is assigned. Maintenance still covers every member. |
| `--pr N[,N...]` | The named pull requests override the umbrella, as they override an area. |
| Fleet roadmap focus | Not applied: an umbrella is an explicit `--roadmap-only`. |
| `--extra-roadmap-prompt TEXT` | Still added, steering the choice of target within the drawn area. |
| `--source PATH_OR_URL` | Refused before any model starts: it needs one roadmap area. |

## Where it shows

Logs carry the draw above. A managed worker's runtime status names the round's
target as `new PR (area: AnalyticNumberTheoryPartI, umbrella GRH)`. `tauceti
status` and the dashboard show `GRH (umbrella)` and count and list the work of
its member areas; in the dashboard's expanded roadmap row the members are marked
`← umbrella`. The dashboard's skip dial (`x`) takes a member out of the count and
the marks at once; bringing one back also starts a fresh survey, because the
last one did not read that member's pull requests. `tauceti workers` shows the
configured name.

## Examples

```bash
# Author and maintain everything GRH needs, with the full quality pass:
tauceti work --loop --full-quality --roadmap-only GRH
# Leave one member to other workers:
tauceti work --loop --roadmap-only GRH --roadmap-skip AnalyticNumberTheoryPartI
# Only tend GRH's open pull requests; no area is drawn:
tauceti work --loop --only rebase,fix-ci,fix,review --roadmap-only GRH
# Author GRH items from a target list:
tauceti work --loop --roadmap-only GRH --roadmap-targets ~/grh-targets.md
# A persistent worker:
tauceti workers add grh --roadmap-only GRH
```
