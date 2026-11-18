# Loop termination: failure fingerprints and escalation

The queue never re-dispatches an unchanged candidate after the same failure. Every failure of the
head is followed by an adopted repair, a withdrawal, a split (bisection) or an alert. None of these
states waits for an operator: everything the queue holds it retries on its own with a bounded
backoff, so the queue keeps working while nobody is watching. This bounds how
long one failure can hold the head: on 2026-11-16 batch 450 failed the member review-size policy four
times between 05:16 and 07:22 UTC, its repair task was claimed 27 times without a submission, and the
queue stalled for about seven hours with no alert.

## Failure fingerprints

Each failed CI attempt of a head is recorded once in the `failures` table with its fingerprint
(`tauceti_queue/escalation.py`):

- the verdict class: the classification verified source CI reported (`policy_failure`,
  `source_failure`), if any, and the verdict contexts that failed (`build`, `scope`, `bump-guard`);
- the members the findings name: a finding's `source_pr`, `source_prs`, `pr` or `number`, resolved
  against the head's members, and the members the member review-size policy rejects (see
  "Attribution");
- a digest of the findings' key lines (`kind|context|module|declaration|message`), normalised so that
  line and column numbers, commit SHAs, URLs and whitespace do not count. Infrastructure findings are
  left out.

Two failures of the same batch are "the same failure" when their fingerprints are equal, whatever
repairs, rebases or bisections happened between them.

## The ladder

1. **Repair.** The first occurrence of a fingerprint on a batch becomes a `repair` task with the
   exact findings, including any member attribution CI reported. The agent may fix it, or leave out a
   member it cannot repair by reducing it to an empty commit and saying why in its message; the
   queue then withdraws that member at adoption and tells its author
   ([`reject-empty-commits.md`](reject-empty-commits.md)). An adopted repair that leaves the head's
   tree on the same base as it was when CI failed is not dispatched: the controller escalates that
   failure at once, since the same content would fail the same way.
   Every member of a control-file lane head is `no_repair` ([mixed-sources.md](mixed-sources.md),
   "The control-file lane"), so a repair there can only empty the culprit, and otherwise the
   failure goes on to escalation.
   A member review-size policy rejection is repairable when the policy measured the member's own
   commit in the batch chain against its parent (the record's `measured` is `batch-commit`), which
   TauCetiTools does since it maps chain commits to members by their trailers: it goes to repair
   like any other failure, with the exact numbers, and splitting the member's change inside the batch
   (for example a too-long new file into smaller files) is a valid repair, which the repair worker
   checks locally before it submits. If the repair does not satisfy the policy, the thresholds below
   withdraw the member. A rejection measured on the member's accepted head (`accepted-head`, or a
   record without `measured` from an older TauCetiTools) skips this step
   (`policy_rejection_withdraw`): no repair can change that head, so the member is withdrawn at once.
2. **Escalate** when any of these holds:
   - the same fingerprint has now occurred `failure_repeat_limit` times on the batch (default 2,
     that is at the first repeat);
   - the batch's repair task has had `repair_fruitless_claim_limit` claims (default 3) released or
     expired without an adopted submission (releases for an inference outage, a checkpoint, a
     supersession or a landing do not count; the inference hold answers for outages);
   - the batch has failed `source_failure_round_limit` times (default 6) since its last escalation,
     whatever the fingerprints, which bounds a sequence of different failures.

   A `rebase_conflict` task is escalated on the same fruitless-claim count; its conflict names the
   member whose replay conflicts.

Escalation acts on the head, in this order:

- **Withdraw named or attributed members.** If the failure names active members, or attribution
  (below) finds the members that caused it, the controller replays the
  head's chain on its own base with those members' commits replaced by empty ones (their messages
  keep a note why), and withdraws them (`_withdraw_member`, reason `queue-withdrawn`): the member
  is `withdrawn`, its input cancelled, its approval withdrawn, its staging label removed, and its
  author gets one comment with what failed, the verdict, the findings, the CI run, the accepted
  revision, the fingerprint and what to do next. The PR stays open. A later member whose replay then
  conflicts goes back to waiting with every member after it.
- **Hold on a broken base.** Before attribution, bisection or withdrawing the last member, that is
  when no finding names a member, the controller asks whether the head's base builds
  (`_base_health`). A base that is the tip of a batch the queue promoted passed the same candidate
  CI and is healthy without any API call. Any other base (an admin push, a direct merge) is judged
  by the newest `ci.yml` push run for that SHA: one REST call, never retried or slept on. `success`
  is healthy and the ladder goes on; `failure` holds the head version (below) with the detail "main
  is broken at ...", naming the run, and withdraws and bisects nobody. Each automatic retry reads
  the run again, so a re-run that turns main CI green releases the hold, and a fix pushed to main
  rebases the head and ends it. Anything else (no run, since `ci.yml` coalesces bursts of pushes; a
  run not yet concluded, cancelled or timed out; a failed lookup; a source other than GitHub) is
  unknown and keeps the ladder as it is, with "base health unknown" and the reason added to the
  escalation's cause. Members a finding names are withdrawn whatever the base: that verdict is
  about the member.
- **Bisect**, the last resort. If nothing names a member, attribution finds none and more than one is
  active, the head keeps the first half of
  its active members (its chain prefix up to the last kept member); the second half's members are
  `removed` (`removed_reason` `bisected`) and their inputs wait again at their original age, to be
  inserted into the open batches. The head's next failure with the same fingerprint bisects again at
  once, so the cause is isolated in about log2(n) CI rounds; if the kept half passes, it lands.
- **Withdraw the last member.** A single active member that still fails is withdrawn as above.
- **Dissolve.** If no active member is left (every one was withdrawn and the head still fails), the
  head's content is its base with nothing to land: the head batch is removed (`head-dissolved`) and
  the next sealed batch takes its place.
- **Hold and retry.** If the base is broken (above) or the escalation itself fails (a Git error
  replaying the chain, say), the head
  version holds with an alert and one direct message, and the same escalation is tried again
  automatically after `automatic_retry_seconds` (default 30 minutes), doubling at each further failure
  up to `automatic_retry_max_seconds` (default 4 hours). A rebase onto a moved base or an operator's
  `rebuild-head` also ends the hold.

Each withdrawal or bisection advances the head's version (open tasks superseded as `escalated`, pre-rebases onto
the old tip discarded, live runs cancelled), is journaled as `escalated` (and `bisected`,
`queue-withdrawn`, `member-returned`), and counted in `escalations`, `bisections`,
`queue_withdrawals` and `attributed_withdrawals`.

## Attribution

Bisection is expensive at our scale: a batch of a hundred members takes about seven CI rounds of an
hour each to isolate one cause, and every member sent back loses the repairs made to it. So before it
bisects, the controller attributes the failure from evidence it already holds
(`tauceti_queue/attribution.py`), strongest first:

1. **Named by the policy.** The member review-size policy (TauCetiTools
   `ci_candidate_workflow.member_size_policy`) prints one `MEMBER-POLICY-REJECTED {json}` line per
   rejected member in the `sandboxed-build` job log, with its PR number, repository identity, forge,
   accepted head, measured sizes and reasons, and where it measured the member (`measured`; for
   `batch-commit` also the `batch_commit` and its `parent`). The log parser (`diagnostics.py`) turns each into a
   `policy` finding; the controller resolves it by accepted head, else by number and repository
   identity, so a Forgejo pull is never confused with a GitHub PR of the same number. A log without
   these lines (an older TauCetiTools) simply attributes nothing this way.
2. **Named by CI.** A finding's own `source_pr`, `source_prs`, `pr` or `number`.
3. **Changed the module.** A finding located in a file (its `path`, or a `TauCeti.*` module) names the
   members whose commits in the failed candidate's chain changed that file.
4. **Changed an import.** A finding in a file no member changed names the members that changed the
   nearest files it imports: the project imports of the failed candidate's tree (one `git grep`, as
   `admission_planning.grep_imports` reads them) are followed breadth first, and the first level that
   holds any member's change gives the candidates.
5. **Changed the build configuration.** A located finding attributed by neither names the members
   that changed `lean-toolchain`, `lake-manifest.json` or the lakefile, if any did.

Each finding so has a set of plausible members; the attribution is the smallest set covering them,
taken greedily (definite findings first; an ambiguous finding another chosen member already explains
adds nobody, else it adds all its candidates). Findings without a location stay unattributed and do
not stop the others from being acted on. Attribution that implicates more than
`attribution_max_members` (default 10) members is not trusted: so many members failing at once looks
systemic, a broken base for instance, and the head is bisected instead. Members are keyed by their
`Source-PR` throughout, never by bare number.

The escalation's `cause` says what attributed each member (for example "attributed by #102: changed
TauCeti/B.lean, where CI reported compiler TauCeti.B: ..."), and so do the withdrawal comment and the
direct message. A bisection's cause says why attribution failed.

Every bisection still raises an alert and a direct message, and the status page's `loop_termination`
object counts them (`bisections`, `bisections_last_7_days`, `last_bisection_at`) beside
`attributed_withdrawals` and `queue_withdrawals`, so their frequency is visible.

## Infrastructure failures

Infrastructure failures of one head version are re-dispatched with short backoff
(`infrastructure_retry_backoff_seconds`) up to `infrastructure_failure_limit` times (default 5; proven
non-sends are not counted). Then the queue backs off automatically: the next re-dispatch comes
`automatic_retry_seconds` (default 30 minutes) after the last failure, and each further one waits
twice as long, up to `automatic_retry_max_seconds` (default 4 hours), indefinitely. The backoff is one
episode (`state:infrastructure_backoff`) across head versions: a rebase onto a moved base dispatches
the new version at once, but its infrastructure failures keep backing off. The episode ends when a run
completes on source, passing or failing; an operator's `retry-ci` still dispatches at once.

The operator gets one direct message when the backoff starts and one on recovery, never one per
retry. While it lasts the status page shows an alert and `loop_termination.infrastructure_backoff`
(start, retries).

## Alerts and notifications

Every escalation, the start of an infrastructure backoff and its recovery (the recovery is a message
and a journal entry, not an alert):

- appears among the status page's `alerts` for `escalation_alert_seconds` (default one day), and in
  `status.escalations` (the last ten);
- is sent once as a Zulip direct message through the outbox (kind `notify`, key `notify:<identity>`),
  retried with the outbox's backoff until delivered. Each message carries an alert ID; a retry after
  a lost reply first looks for that ID among the bot's recent messages to the recipient, so a
  delivered message is not sent again.

The Zulip client is the security audit's (`tauceti_worker/audit_alerts.py`): HTTPS origins only, no
redirects, bot basic authentication. Configure it by pointing `TAUCETI_QUEUE_ZULIP_CONFIG` in the
service's environment (`~/.config/tauceti-queue/environment`) at a mode-0600 JSON file:

```json
{"site": "https://leanprover.zulipchat.com", "email": "BOT_EMAIL", "api_key": "BOT_API_KEY", "recipient_id": 123}
```

`recipient_id` is the numeric Zulip user ID of the person to notify. The same file format serves
`TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG`, so one bot can serve both. Without the variable, a `notify`
entry completes as `notifier-unconfigured` and only the status alert remains; a file that cannot be
read is itself reported as an alert.

## Settings

| setting | initial | meaning |
| --- | --- | --- |
| `failure_repeat_limit` | `2` | occurrences of one fingerprint on a batch that trigger escalation; 0 disables |
| `repair_fruitless_claim_limit` | `3` | fruitless claims of a head's repair or rebase-conflict task that trigger escalation; 0 disables |
| `source_failure_round_limit` | `6` | source failures of a batch since its last escalation that trigger escalation; 0 disables |
| `policy_rejection_withdraw` | `true` | withdraw members the review-size policy rejects on their accepted head at their first failure, without a repair; rejections measured on the batch commit always go to repair first |
| `attribution_max_members` | `10` | the most members an attribution may name before the head is bisected instead; 0: no bound |
| `infrastructure_failure_limit` | `5` | counted infrastructure failures of a head version before the automatic backoff; 0: short backoff forever |
| `automatic_retry_seconds` | `1800` | first delay of the infrastructure backoff and of a held escalation's retry |
| `automatic_retry_max_seconds` | `14400` | the most either delay doubles to |
| `escalation_alert_seconds` | `86400` | how long an escalation stays among the status alerts |

## Limits

Bisection loses repairs agents made to the members it returns to waiting: they are squashed afresh
from their accepted heads when inserted again. A failure whose cause is in the returned half shows
up again in a later batch, which starts its own ladder at the repair step.

Attribution is a heuristic. A member that changed a failing module is named even when the real cause
is an interaction with another member; the next round then meets the remaining failure as a new
fingerprint, so a wrong guess costs one round and one withdrawal, not a loop. Import attribution sees
project imports only, so an error caused by a dependency bump is attributed through the build
configuration rule, not the import closure. A base that is itself broken makes every batch fail in
modules nobody changed; the import rule can then name members that changed upstream modules. A base
main CI reports failed holds instead, but a base whose health is unknown (no `ci.yml` run, a failed
lookup) is attributed as before, where the `attribution_max_members` bound limits the damage when
many are implicated. A base the queue landed is taken as healthy, so a landed base that later stops
building (a candidate workflow change, a runner image, a cache outage surfacing as source errors) is
not detected.
