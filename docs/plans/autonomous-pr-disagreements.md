# Autonomous resolution of PR disagreements

Implementation authorized; use the steps below for implementation and deployment verification.

## Required behaviour

Workers resolve review disagreements through repair, narrowing/splitting the change, or an evidence-based response under the current rubric. If the disagreement remains unresolved after the existing bounded attempts, close their own PR with a concise explanation so another attempt can start afresh. No human appeal or agent-created permanent pause is an outcome.

Explicit operator holds remain binding. This does not grant permission to close another author's PR or broaden scoped repair permissions. Transient infrastructure failures continue through existing retry handling.

## Implementation order

1. **Worker instructions and selection:** replace the blocked-PR instructions in `prompts/pr-outcome.md` and all referencing prompts. Authorize closure for unresolved disagreements, not just supersession. Remove `maintenance_blocked` filtering from survey, dispatch, and recovery, so old pause comments cannot strand PRs. Use the last permitted fix attempt for repair-or-close. If the reviewed head remains unresolved after the existing attempts, the controller closes the owned PR without another model call; reuse existing attempt accounting.
2. **Review and shared state:** change `rubrics/scope.md:26`, which explicitly tells reviewers to say a human must add material to a roadmap, to state the scope finding without an escalation path. Change `rubrics/_common.md:50`, which tells reviewers to raise missing mechanical checks with humans, so that it does not suggest an unattended escalation. Remove equivalent wording from recovery comments. Retire blocked-marker projection in the shared state server and Python survey facts. Deploy Worker changes first so existing comments become harmless without editing PR history or adding API traffic.
3. **Verify and deploy:** test unresolved disagreement, exhausted attempts, obsolete pause comments, current-rubric reconsideration, explicit holds, and foreign-PR restrictions. Inspect rendered prompts and real fleet selection/output. Verify a repair or confirmed closure is recorded as the outcome; posting a pause comment must not masquerade as completed maintenance.

Seven-day automatic closure is separate work: see [the housekeeping repair plan](seven-day-pr-closure.md). Neither implementation depends on the other.
