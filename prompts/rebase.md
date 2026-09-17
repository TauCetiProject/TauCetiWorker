You are reconciling the branch with current main on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The branch either conflicts with current `main`, or the merge sweep handed off an update it cannot perform on this contributor-owned fork. A mergeable fork may still need this update; do not stop just because Git reports no textual conflict. Bring it up to date with `main` and resolve the conflicts so it can merge again. Work autonomously to completion.

## Rebase onto current main
- Fetch and integrate the latest `main`:
  ```
  git fetch origin
  git merge origin/main      # (or: git rebase origin/main — either is fine; merge is simpler to resolve)
  ```
- Resolve every conflict on its merits:
  - **`TauCeti.lean` (the intentionally empty root module)**: preserve `main`'s version; do not add imports or reconstruct it.
  - **A source file under `TauCeti/`**: resolve so both the upstream change and your PR's intent are preserved. If `main` now provides something your PR duplicated, prefer the upstream version and drop the duplicate.
- Do NOT discard upstream work to "win" a conflict, and do NOT weaken or delete your PR's real content to dodge one. If a conflict is genuinely irreconcilable (your PR's target no longer makes sense because `main` subsumed it), stop and say so in your report rather than forcing a merge.

If the branch already includes current `main` and no concrete repair is needed, report that no update is needed and stop. Do not manufacture a commit or push an empty change just to satisfy the submission instructions.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT hand-edit the root `TauCeti.lean` — it stays intentionally empty (see above). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Everything under `namespace TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter.

Merging upstream workflow or pin changes as part of bringing in `main` is expected. Do not author independent changes to those human-owned files. The sweep request is bound to the old head; after a successful push it no longer schedules rebase work. Do not remove the request label yourself or reset any attempt counter.

## Verify before pushing (all three MUST pass, after the merge/rebase)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red — a botched conflict resolution that builds red is worse than the conflict.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. When a repair is needed, do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the merge/resolution (if `git merge` left a merge commit, keep its default message; otherwise `<type>: <subject>`, ending the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  It compare-and-swaps the PR branch against the head you started from (so it works whether you merged or rebased, and never clobbers a concurrent push). Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report; do not work around it.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which files conflicted, how you resolved each, and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
