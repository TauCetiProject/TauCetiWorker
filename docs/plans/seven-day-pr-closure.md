# Repair seven-day automatic PR closure

Implementation authorized; use the steps below for implementation and deployment verification. Independent of [autonomous resolution of PR disagreements](autonomous-pr-disagreements.md).

## Observed behaviour

The deployed `housekeeping.py` in TauCetiTools already closes review-blocked PRs after seven days without any PR activity, plus PRs that exhaust the review budget. It does not measure seven days continuously in `awaiting-author`; bot comments and label changes reset its inactivity clock. Explicit hold labels, drafts, and canonical dependency bumps are exempt.

The successful run [39522640274](https://github.com/TauCetiProject/TauCeti/actions/runs/39522640274) found nine PRs untouched for more than seven days and closed none. A confirmed gap: status accepts contributor-posted scoreboards, but housekeeping only accepts OWNER/MEMBER/COLLABORATOR scoreboards. Five of the eight old awaiting-author PRs have only contributor scoreboards and are skipped by that policy. Three have member scoreboards under the current authenticated read; their exact production skip reason remains to be established. Repository association also differs by viewer: #8282's reviewer appears as MEMBER with our credentials and CONTRIBUTOR anonymously.

## Implementation plan

1. Establish the production skip reason for all eight observed cases using the housekeeping identity. Distinguish inactivity from time continuously awaiting the author; identify whether automated activity is preventing otherwise intended closure.
2. Repair housekeeping to act consistently on authoritative review evidence, without trusting arbitrary comments. Preserve explicit holds and existing exemptions. Expose skipped-candidate reasons in the existing run log. Prefer existing cached evidence and add no polling task.
3. Test the observed cases, exemptions, and activity-clock behaviour. Inspect a dry run under the production identity, deploy through the normal TauCetiTools pin update, and verify actual closure receipts from the existing housekeeping workflow.

Age-based housekeeping is a fallback. A worker resolving a disagreement now must repair or close its own PR through the separate disagreement policy, rather than waiting seven days.
