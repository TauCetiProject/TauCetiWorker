# Loop termination: failure fingerprints and escalation

The queue never re-dispatches an unchanged candidate after the same failure. Every failure of the
head is followed by an adopted repair, a withdrawal, a split (bisection) or an alert. This bounds how
long one failure can hold the head: on 2026-11-16 batch 450 failed the member review-size policy four
times between 05:16 and 07:22 UTC, its repair task was claimed 27 times without a submission, and the
queue stalled for about seven hours with no alert.

## Failure fingerprints

Each failed CI attempt of a head is recorded once in the `failures` table with its fingerprint
(`tauceti_queue/escalation.py`):

- the verdict class: the classification verified source CI reported (`policy_failure`,
  `source_failure`), if any, and the verdict contexts that failed (`build`, `scope`, `bump-guard`);
- the members the findings name: a finding's `source_pr`, `source_prs`, `pr` or `number`, resolved
  against the head's members;
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

- **Withdraw named members.** If the failure names active members, the controller replays the
  head's chain on its own base with those members' commits replaced by empty ones (their messages
  keep a note why), and withdraws them (`_withdraw_member`, reason `queue-withdrawn`): the member
  is `withdrawn`, its input cancelled, its approval withdrawn, its staging label removed, and its
  author gets one comment with what failed, the verdict, the findings, the CI run, the accepted
  revision, the fingerprint and what to do next. The PR stays open. A later member whose replay then
  conflicts goes back to waiting with every member after it.
- **Bisect.** If nothing names a member and more than one is active, the head keeps the first half of
  its active members (its chain prefix up to the last kept member); the second half's members are
  `removed` (`removed_reason` `bisected`) and their inputs wait again at their original age, to be
  inserted into the open batches. The head's next failure with the same fingerprint bisects again at
  once, so the cause is isolated in about log2(n) CI rounds; if the kept half passes, it lands.
- **Withdraw the last member.** A single active member that still fails is withdrawn as above.
- **Hold.** If nothing is left to withdraw, the head version holds with an alert until it changes
  (a rebase, or an operator's `rebuild-head`).

Each escalation advances the head's version (open tasks superseded as `escalated`, pre-rebases onto
the old tip discarded, live runs cancelled), is journaled as `escalated` (and `bisected`,
`queue-withdrawn`, `member-returned`), and counted in `escalations`, `bisections` and
`queue_withdrawals`.

## Infrastructure failures

Infrastructure failures of one head version are re-dispatched with backoff at most
`infrastructure_failure_limit` times (default 5; proven non-sends are not counted). Then the head
holds: no automatic dispatch, a reason line and an alert, until an operator runs `retry-ci`, which
supersedes the counted attempts and so lifts the hold.

## Alerts and notifications

Every escalation and every infrastructure hold:

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
| `infrastructure_failure_limit` | `5` | counted infrastructure failures of a head version before it holds; 0 disables |
| `escalation_alert_seconds` | `86400` | how long an escalation stays among the status alerts |

## Limits

Bisection loses repairs agents made to the members it returns to waiting: they are squashed afresh
from their accepted heads when inserted again. A failure whose cause is in the returned half shows
up again in a later batch, which starts its own ladder at the repair step. Withdrawal relies on CI
naming members; today the member review-size policy does not name the oversized member, so such a
failure is isolated by bisection.
