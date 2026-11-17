# Reject PRs whose final staging commits are empty

An adopted repair that reduces an ordinary member to an empty commit withdraws that member at once
(setting `withdraw_emptied_at_adoption`, default on): the member is marked `withdrawn` and keeps its
empty commit in the chain, its input is cancelled as `empty-commit`, its approval is withdrawn, its
staging label is removed, and its author gets one comment through the outbox (key
`withdrawn-comment:<input>`) saying what failed (the repair task's CI run and findings), the accepted
revision, the agent's explanation and what to do next (rebase, fix, approve again). The PR stays open.
A withdrawn member cannot be given content again by a later repair (`no-repair-edited`); one whose PR
lands on `main` by other means before its batch lands is landed with the batch as any externally
merged member is. This is journaled as `member-withdrawn-empty` and counted in `prs_withdrawn_empty`.

The landing-time rejection below remains the backstop for empty final commits that adoption did not
withdraw (the setting off, or a member emptied by a path other than an adopted repair). Its comment
carries the same failure lines and what to do next.

When a staging batch lands, a PR whose final commit is empty should be reported as rejected rather than merged. Use the explanation the repair agent already appended to its commit message. Do not add another explanation channel, repair marker, or agent protocol.

## Implementation

1. In the landing path, preserve the existing handling for externally merged PRs and `dont-repair-in-queue` withdrawals. For the remaining members, compare each final commit's tree with its parent's tree.
2. A nonempty commit follows the existing merged path. An empty commit follows the rejected path. Only the final commit matters: a contribution emptied and later restored is merged normally.
3. Record rejected inputs as cancelled with a rejection reason, leave their landing fields unset, and exclude them from merged counts and successful per-PR feed outcomes. Reuse the existing records and journal.
4. Use the existing retryable outbox to post a rejection comment containing the explanation already derived from the appended commit message, remove the staging label, and close the PR without the merged title prefix. Check that the PR still has the accepted head before closing it. Do not require a new label or confirmation step.
5. Ensure canonical-advance handling also ignores these empty commits as evidence that their PRs landed. Reuse the existing empty-commit check.

Keep staging chains and their one-commit-per-PR validation unchanged. Decide the outcome when the authoritative batch lands, so speculative repairs and intermediate empty commits cause no premature rejection. Empty placeholder commits can still reach `main`; removing them from history is a separate change.

## Verification

Update the existing shrink-to-empty fixture to expect rejection. Cover a mixed batch, an all-empty batch, a contribution restored by a later repair, an externally merged PR, an opt-out withdrawal, and outbox retries or restart. Check that rejected PRs receive one rejection comment, no merged notice or title prefix, and no merged count or successful feed outcome.
