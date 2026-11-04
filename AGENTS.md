# Production operations

- Prefer live updates when deploying TauCeti workers or fleet changes. Keep normal worker and CI admissions enabled; the operator prefers live repair and cleanup of any resulting problems over draining the system.
- Do not drain the fleet, pause admissions, or wait for the whole fleet to become idle as a routine deployment precaution. These changes confuse other agents monitoring production.
- Use a drain only when the user explicitly requests it or a concrete constraint makes a live update unsafe or impossible. Explain that specific constraint before changing admissions; a deployment command requiring a drain is a reason to find a live update path, not automatic authorization to drain.
- After a live update, verify the running revisions, useful completions, and any interrupted or stranded work, and repair problems within the authorized scope.
- Changes to bors or staging behaviour (bors-ng, the TauCetiTools staging scripts, and the TauCeti pin bumps or workflow edits that deploy them) are the highest-priority work. Land them by admin merge as soon as they are green instead of waiting for the staging queue; do not queue them behind content batches. (Operator instruction, 2026-11-04.)
