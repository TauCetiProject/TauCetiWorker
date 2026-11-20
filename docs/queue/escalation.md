# Loop termination: repair, infrastructure backoff and notifications

A failing head is repaired by agents however often it fails. The queue never withdraws, bisects,
evicts or holds a member of the head on its own: there is no eviction cap, no count of repeated
failures or fruitless claims, and no human in the repair loop.

## Repair

Every source failure of the head becomes a `repair` task with the exact findings, including any
member attribution CI reported (`source_pr`) and any member review-size policy rejection. When the
task is adopted the head is dispatched again; if it fails again, whether with the same findings or
new ones, it becomes a new repair task. An adopted repair that leaves the head's tree as it was is
dispatched again too. A repair or rebase-conflict task that workers give back without a result stays
open for the next claim.

The one way a member leaves the head without landing, short of an operator's command, is the repair
agent's own judgment: it may leave out a member it cannot repair by reducing its contribution to an
empty commit and saying why. The queue then withdraws that member at adoption
(`member-reduced-to-empty`, `member-withdrawn-empty`, input reason `empty-commit`) and tells its
author ([`reject-empty-commits.md`](reject-empty-commits.md)); a `no_repair` member the agent empties
is withdrawn as `problem-anticipated`. Every member of a control-file lane head is `no_repair`
([mixed-sources.md](mixed-sources.md), "The control-file lane"), so a repair there can only empty
the culprit.

A member review-size policy rejection measured on the member's own commit in the batch chain (the
record's `measured` is `batch-commit`) can be repaired by splitting the member's change inside the
batch (for example a too-long new file into smaller files), which the repair worker checks locally
before it submits. A rejection measured on the member's accepted head (`accepted-head`, or a record
without `measured` from an older TauCetiTools) goes to repair like any other failure; the agent can
only satisfy it by emptying the member.

Operators keep their explicit commands: `bors r-`, the admin endpoints `rebuild-head`, `retry-ci`
and `/admin/evict-native` ([mixed-sources.md](mixed-sources.md)), and the emergency lane.

### Earlier state

Before 2026-11-20 a recurring failure fingerprint, a fruitless-claim count, a source-failure round
count, a policy rejection on an accepted head, or an adopted repair that left the tree unchanged
escalated the head: the controller withdrew the members the failure named or attribution found, held
on a broken base, bisected, withdrew the last member, or dissolved the head. That machinery and its
settings (`failure_repeat_limit`, `repair_fruitless_claim_limit`, `source_failure_round_limit`,
`policy_rejection_withdraw`, `attribution_max_members`) are gone. What it left behind still loads:
the `failures` table and its rows are kept as history and no longer written; inputs cancelled as
`queue-withdrawn` are re-admitted by a new approval as before; earlier records in
`state:escalations` stay on the status page until they age out; and a `state:escalation_hold` is no
longer read, so a head held before the change is repaired again at once. A stored value of a removed
setting is ignored. The configuration file never accepted these settings.

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

The start of an infrastructure backoff and its recovery (the recovery is a message and a journal
entry, not an alert):

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

## Stuck PR heads

The hourly CI recovery pass on the queue host (TauCetiTools `services/ci-recovery`) writes the open
PR heads that have neither CI nor a final state three hours after their last push to
`ci_recovery_report` (default `~/.local/state/tauceti-ci-recovery/stuck-heads.json`). The queue reads
that report (`tauceti_queue/stuck_heads.py`):

- `status.stuck_heads` carries its state (`fresh`, `stale` once older than three hours, `missing`,
  `invalid`), the count, the oldest head's age and up to 30 heads; the summary of
  `tauceti-queue status` prints `stuck heads: N, oldest Xh`;
- the public observation publishes one `stuck_head` condition per listed head (with its last push
  time) after a `stuck_heads_observed` condition (and `stuck_heads_partial` after an incomplete
  pass), within its bound of 100 conditions, so `tauceti-fleet status` shows the same line; a
  malformed report is `invalid` and never sends anything;
- once a fresh report's count has stayed above zero for six hours (its `stuck_since`), the status
  gains an alert and the operator gets one Zulip direct message through the outbox, keyed by the
  start of the stuck period (`notify:stuck-heads:<repository>:<stuck_since>`), listing the PRs with
  times in AEDT. A new message needs a new stuck period: a pass that finds no stuck head ends it.

## Settings

| setting | initial | meaning |
| --- | --- | --- |
| `infrastructure_failure_limit` | `5` | counted infrastructure failures of a head version before the automatic backoff; 0: short backoff forever |
| `automatic_retry_seconds` | `1800` | first delay of the infrastructure backoff |
| `automatic_retry_max_seconds` | `14400` | the most that delay doubles to |
| `escalation_alert_seconds` | `86400` | how long an escalation stays among the status alerts |
