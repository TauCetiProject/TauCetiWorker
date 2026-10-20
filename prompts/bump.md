You are adapting TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib, to a Mathlib bump on pull request #__PR__. You are in a checkout of the repo, already on the PR's branch. A bot opened this PR to move the Lake pins (`lake-manifest.json` and/or `lean-toolchain`) forward to a newer Mathlib, and the `build` check is red because `TauCeti/` has not caught up to the Mathlib API at the new pin. Work toward making CI green by adapting `TauCeti/`, without reverting the bump and without weakening the library.

## The pins are the point — keep them
- The bumped `lake-manifest.json` / `lean-toolchain` on this branch ARE the change under review. Do NOT revert them, do NOT re-pin to an older Mathlib, do NOT touch the lakefile. Your job is to make `TauCeti/` build against the Mathlib the bot pinned.
- For a difficult API migration, preserve completed source repairs and describe the remaining root failure so the next session can continue. Never revert the target or gut the library.

## Reproduce and adapt
Read the failing CI log first and reproduce the stage that failed, not every check. The usual
failure is the build: use the helper's `--stage build`. If the expired-shim check failed:
```
"$TAUCETI_VERIFY_PYTHON" "__BIN__/tauceti-verify" --stage shim-expiry
```
The helper uses `--fail-on-available`, checks the merge-base registry with `--base-manifest`
and `--base-root`, and probes all obligations after a pin change. It never fetches.
- Read the build failures. The usual cause is a renamed/moved/retyped Mathlib lemma or a changed signature. Fix each by updating the `TauCeti/` proof or statement to the new Mathlib API. Prefer the smallest correct change.
- The shim-expiry command may be the only failing check even when `lake build` succeeds. Its annotations name exact Mathlib replacements and affected sources. Migrate only the superseded declarations/imports, preserve or re-home source-only API, and update `TauCeti/mathlib-shims.json` in the same source-only change. The checker derives each inherited source's declaration surface from the PR merge base and ratchets its probes until that surface is migrated, deleted, or re-homed under an entry preserving those probes, so never make the check green by merely deleting probes or changing an exact target to a speculative/landing sentinel.
- For a failing check's logs: `gh pr checks __PR__ --repo TauCetiProject/TauCeti`, then `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed`.
- If the failure is genuinely transient infra (e.g. a cache fetch timeout), both `lake build` and the shim-expiry command succeed locally, and the failed logs contain no actionable migration, push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so.

## Rules of the repo (hard constraints)
- Adapt code under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/` without it. The only files outside `TauCeti/` you may leave changed are the pins the bot already bumped. Do NOT touch `Scripts/`, `.github/`, or the lakefile (`lakefile.toml`/`lakefile.lean`).
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, follow the durable blocked-PR procedure below.
- The final PR must be green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter (e.g. with `set_option ... false`) to force the build green.

## Preserve progress between sessions

Reproduce the first root failure, inspect the relevant upstream Mathlib change, and repair a coherent
module group. Build that group and its dependencies where possible. Read previous repair commits
and avoid repeating an approach that already failed.

Prepare one coherent source repair checkpoint per session. Push it once through the safe wrapper,
then end the session, even when unrelated modules still fail. Keep the pins fixed and never introduce `sorry`, `admit`, `native_decide`, axioms, or
option overrides. Describe what compiled and what remains broken in the commit body. The branch
and CI logs are the next session's starting point; no separate progress file is needed.

Work within the round deadline supplied below. Stop starting new groups ten minutes before it,
and save completed repairs before the worker terminates the session. Run builds synchronously in
the foreground; a background build will be abandoned when the session ends. Push the checkpoint
before ending your turn; no later turn will recover unpushed edits. Full-library compilation and
all audits remain required for merging; a partial checkpoint must never be described as green.

When the migration is complete, run the final verification below if time permits. Otherwise
push the completed repairs and let full CI verify them. Do not spend the session waiting for CI.

## Final verification when time permits
__LOCAL_VERIFICATION__
A pin bump changes every module's dependencies, so the helper prints `scope: full: ...`.
Once the migration is complete, run its default all stages and inspect the receipt.

Full CI runs these checks even when a partial repair cannot run the full library yet.

## Submit
- Commit the adaptation (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `--force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so; do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch files outside `TauCeti/` (and the already-bumped pins).

## Report
End with a concise summary: which Mathlib changes broke or superseded `TauCeti/`, how you adapted each, and the exact shim-expiry, `scope:`, `lake build` and `local-axioms:` results observed, distinguishing partial checkpoints from complete local verification, and local checks from CI. Do not claim green unless you saw it.
