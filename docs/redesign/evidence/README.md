# Redesign baseline evidence

The redesign plans for approximately 2,000 added Lean lines per contribution and 100 landed contributions/hour. The canonical source sample below shows substantially smaller contributions at present. Keep the proposed workload separate from the measured distribution when sizing inference, review context, and builds.

## Canonical history sample

The [retained measurement](baseline-2026-11-01.json) covers 500 consecutive first-parent commits ending at [`008df473`](https://github.com/TauCetiProject/TauCeti/commit/008df47307b7cc5018672dec8387e5e78ef5d816), captured on 2026-11-01. Commit timestamps run from 05:50:55 through 21:53:27 UTC that day. Every sampled commit has a distinct final `(#PR)` subject suffix and changes Lean source; there are no unassociated commits in this particular sample.

| Quantity per canonical contribution commit | Mean | Median | p90 | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Added Lean lines | 288.12 | 243.5 | 545 | 654 | 1,279 |
| Deleted Lean lines | 40.90 | 3 | 112 | 240 | 1,052 |
| Lean churn, additions plus deletions | 329.02 | 254.5 | 610 | 850 | 2,331 |

No sampled contribution adds 1,500 or more Lean lines. Two have at least 2,000 lines of churn. The sample contains 144,060 added and 20,451 deleted Lean lines. Thus 2,000 **added** lines is about 6.9 times the current sample mean and 8.2 times its median. “2,000 LOC” must not silently switch between additions, net additions, and churn.

The 15 complete clock hours between 06:00 and 21:00 UTC contain 473 sampled canonical commits by their commit timestamps, averaging 31.5/hour, with a maximum of 75 in one hour. This is a **commit-timestamp rate proxy**, not a directly observed production landing-rate measurement: staging construction time can differ from actual branch publication time, batching creates bursts, and an immutable history sample does not show queue arrival or processing latency. It cannot establish a sustainable capacity or the original 100-times baseline.

## Comparison samples and bias

The retained JSON also includes 500 GitHub-classified merged PRs and 100 open PRs from default `gh pr list` ordering. Their counts include all file types:

| Sample | Mean additions | Median additions | p95 additions |
| --- | ---: | ---: | ---: |
| GitHub-classified merged, 500 PRs | 200.98 | 162 | 512 |
| Open, 100 PRs | 266.02 | 219.5 | 564 |

The merged sample's timestamps range from October 5 through October 28. Bors squash landings may close PRs without GitHub calling them merged, so that sample is biased and misses much of recent mathematical integration. Default listing order is not a random sample and does not select by actual canonical landing time. Use canonical history or positive landing evidence for contribution accounting.

Canonical diffs count each integrated contribution against its first parent. They can include staging repair and differ from the original PR's diff against its original base. PR identities are inferred from subject suffixes, not authenticated by those strings alone. For a production accounting pipeline, join source ancestry with stored acceptance/publication records and report ambiguous attribution. The present 500 unique inferred PRs are sufficient to compare observed change sizes, without claiming a complete historical PR ledger.

LOC here counts textual `.lean` file additions/deletions, including comments and blank lines. `--no-renames` counts a move as deletion plus addition. This script refuses quoted paths it cannot classify and reports binary files separately; this sample has neither. These counts are not a measure of mathematical quality, theorem difficulty, or agent productivity.

## Reproduce

Use an existing suitable checkout/object store, or make an isolated bare sample:

```bash
git clone --bare --filter=blob:none --depth=600 --single-branch --branch main \
  https://github.com/TauCetiProject/TauCeti.git /tmp/tauceti-baseline.git

python docs/redesign/evidence/measure-baseline.py \
  --git-dir /tmp/tauceti-baseline.git \
  --ref 008df47307b7cc5018672dec8387e5e78ef5d816 \
  --limit 500 \
  --output /tmp/tauceti-baseline.json
```

If that exact revision is no longer within the shallow window, fetch the recorded revision and enough parents into the isolated clone before rerunning. A partial clone may fetch required blobs during `numstat`. No running worker checkout needs alteration.

Optional comparison inputs can be collected once and passed with `--merged-json` and `--open-json`:

```bash
gh pr list --repo TauCetiProject/TauCeti --state merged --limit 500 \
  --json number,title,additions,deletions,changedFiles,mergedAt,createdAt,baseRefName,headRefName \
  > /tmp/tauceti-merged.json

gh pr list --repo TauCetiProject/TauCeti --state open --limit 100 \
  --json number,title,additions,deletions,changedFiles,createdAt,headRefName \
  > /tmp/tauceti-open.json
```

The [measurement script](measure-baseline.py) records immutable commit IDs, selection rules, counts, and individual sample records. Re-running comparison queries later changes those samples; the committed JSON preserves the original observations. The script does not trigger CI or agent work.

## Missing measurements for capacity planning

Collect CPU seconds and peak memory per verification phase, warm/cold preparation bytes and duration, review and repair model calls, agent-hours per landed contribution including failures, staging repair service time, queue arrival rates, actual publication times, and cross-library edit overlap. Join them by stable change/job/attempt IDs.

Existing historical sources provide useful initial instrumentation and failure examples: [GitHub state capacity](../../github-state-capacity.md), [Mathlib freshness and recovery](../../mathlib-freshness-capacity.md), [verification](../../verification.md), and [activity accounting](../../activity.md). Their dates and measurement scope remain part of their evidence.

The current-system description also reads pinned public project configuration and upstream service documentation. Source references in those documents identify the inspected revisions. This evidence collection does not assert a live fleet revision, backend override, admission state, or funded capacity.
