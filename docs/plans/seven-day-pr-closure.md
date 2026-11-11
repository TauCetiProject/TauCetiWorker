# Repair seven-day automatic PR closure

Implementation authorized; use the steps below for implementation and deployment verification. Independent of [autonomous resolution of PR disagreements](autonomous-pr-disagreements.md).

## Observed behaviour

The deployed `housekeeping.py` in TauCetiTools already closes review-blocked PRs after seven days without any PR activity, plus PRs that exhaust the review budget. It does not measure seven days continuously in `awaiting-author`; bot comments and label changes reset its inactivity clock. Explicit hold labels, drafts, and canonical dependency bumps are exempt.

The successful run [39522640274](https://github.com/TauCetiProject/TauCeti/actions/runs/39522640274) found nine PRs untouched for more than seven days and closed none. A [read-only production-identity diagnosis](https://github.com/TauCetiProject/TauCeti/actions/runs/39561701156) established the cause for all eight old awaiting-author PRs: the housekeeping App sees every relevant scoreboard publisher as CONTRIBUTOR, including accounts shown as MEMBER with personal credentials. Status accepts these legitimate reviews; housekeeping's separate association check silently rejects them.


## Implementation plan

1. Establish the production skip reason for all eight observed cases using the housekeeping identity. Distinguish inactivity from time continuously awaiting the author; identify whether automated activity is preventing otherwise intended closure.
2. Use the repository-managed `awaiting-author` status label, following the project's existing status trust policy rather than adding a separate comment parser or viewer-dependent membership check. Preserve explicit holds and existing exemptions. Expose skipped-candidate reasons in the existing run log. Prefer existing cached evidence and add no polling task.
3. Test the observed cases, exemptions, and activity-clock behaviour. Inspect a dry run under the production identity, deploy through the normal TauCetiTools pin update, and verify actual closure receipts from the existing housekeeping workflow.

Age-based housekeeping is a fallback. A worker resolving a disagreement now must repair or close its own PR through the separate disagreement policy, rather than waiting seven days.
