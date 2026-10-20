You are reconciling the branch with current main on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The branch either conflicts with current `main`, or an authenticated head-bound recovery request asks for integration repair. A build-green, textually mergeable branch can still import source deleted or moved on main, use renamed APIs, or duplicate declarations now on main. Read the exact request evidence below; a suspect partition containing several PRs is not evidence against this PR. Integrate main and repair the resulting source so it can merge again. Work autonomously to completion.

Before editing and again before pushing, re-read the live PR: it must remain open at the starting head with no `keep`/`hold`/`wip`/`human`/`on-hold`/`do-not-close` pause. For a semantic handoff, confirm its authenticated current-head request is still active. If it was withdrawn or superseded, stop without changing the branch.

## Rebase onto current main
- Fetch once and integrate one current `main` snapshot (record its full SHA; do not chase further main advances during this task):
  ```
  git fetch origin
  git rev-parse origin/main
  git merge origin/main      # (or: git rebase origin/main — either is fine; merge is simpler to resolve)
  ```
- Resolve every conflict on its merits:
  - **`TauCeti.lean` (the intentionally empty root module)**: preserve `main`'s version; do not add imports or reconstruct it.
  - **A source file under `TauCeti/`**: resolve so both the upstream change and your PR's intent are preserved. If `main` now provides something your PR duplicated, prefer the upstream version and drop the duplicate.
- Do NOT discard upstream work to "win" a conflict, and do NOT weaken or delete your PR's real content to dodge one. If `main` has fully subsumed this PR, follow the durable PR outcome rules below: verify the replacement and close your superseded PR with evidence. If useful content remains, preserve it; a conflict alone is not evidence of supersession.

Use the request's fixed tested-main revision and importing file/missing module or authoritative individual failure to reproduce the diagnosis. Check the prospective integrated tree, including modules added by this PR and imports in unchanged dependencies. Repair imports, renamed APIs, overlapping declarations and affected proofs on their merits. A successful Git merge/rebase alone does not prove compatibility: all verification below must pass. If the evidence no longer applies, explain why using the actual source and validation results.

An authenticated request marked `provisional-diagnostic-v1` identifies one suspect from a failed combination, not a proven individual failure. Read its candidate/run/attempt and original evidence main. Reproduce this PR alone against that evidence main before attributing the failure; integrate one frozen current main for any actual repair. An interaction with another member may explain the diagnostic. Never integrate another PR's branch to reproduce or repair it. If the source passes alone, report attribution **unconfirmed**, with the exact tested head/main and full validation results; do not claim you fixed a broken PR. Bors owns the isolated fallback/readmission and request label. Do not post approval/retry commands, clear the request, reset an attempt counter or manufacture a commit.

If the branch already includes current `main`, semantic repair may still be needed. If no concrete repair is needed, still complete all verification below before reporting that no update is needed. A no-change exit is not proof of validation. Do not manufacture a commit or push an empty change just to satisfy the submission instructions.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT hand-edit the root `TauCeti.lean` — it stays intentionally empty (see above). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, follow the durable blocked-PR procedure below rather than leaving the decision in your final report.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter.

Merging upstream workflow or pin changes as part of bringing in `main` is expected. Do not author independent changes to those human-owned files. The sweep request is bound to the old head; after a successful push it no longer schedules rebase work. Do not remove the request label yourself or reset any attempt counter.

## Verify before pushing (all MUST pass, after the merge/rebase)
__LOCAL_VERIFICATION__
Use the request's evidence to reproduce the diagnosis with the specific failing step, not a rerun of
every check. After the merge/rebase and any repair, run the final verification once: `lake exe cache
get` (if you have not run it this session, or the merge changed the pins), `lake build`, then the
scope recipe and the scoped audit above. The recipe's base is the canonical main you integrated, so
it covers this PR's changed modules and everything importing them. Iterate until green. Never push
red: a botched conflict resolution that builds red is worse than the conflict.

**Do this synchronously, in this one turn.** You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. When a repair is needed, do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the merge/resolution (if `git merge` left a merge commit, keep its default message; otherwise `<type>: <subject>`, ending the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  It compare-and-swaps the PR branch against the head you started from (so it works whether you merged or rebased, and never clobbers a concurrent push). Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report; do not work around it.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: the starting head and integrated main SHA, the exact evidence investigated, which source repairs were needed, and the exact `scope:`, `lake build` and `lake exe axioms` result lines proving your local checks green + axiom-clean, labelled as local (scoped or full) rather than CI. Do not claim green unless you saw it.
