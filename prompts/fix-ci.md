You are fixing FAILING CI on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The `build` check is red, or the build job finished without posting it. Work autonomously to completion: make CI green without weakening the PR.

## Find out what's actually failing
- See which checks failed and read their logs:
  - `gh pr checks __PR__ --repo TauCetiProject/TauCeti`
  - `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed` (use the run id from the failing check)
- If the head has no `build` check at all, the `sandboxed-build` job ended before its report step, or
  its only runs were cancelled and nothing rebuilt it. A log ending in `The runner has received a
  shutdown signal` (exit code 143) during `lake build` means the runner was stopped, which happens when
  the build runs the 16 GB runner out of memory; re-triggering CI then repeats it. Measure before you
  decide. Once a changed file's imports are built,
  `python3 -c 'import resource, subprocess, sys; subprocess.run(sys.argv[1:]); print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)' lake lean <path/to/File.lean>`
  recompiles the file and prints its peak memory (kilobytes on Linux); a `lake build` of an already-built
  module would measure only Lake. If a changed file comes anywhere near 16 GB, bring it well under,
  typically by restructuring a large `decide` over a finite type so that the kernel checks less at
  once. If every changed file is well under and the build stage below passes locally, treat the stop as
  transient.
- The single `build` check runs these stages in order: the expired-shim check, `lake build --iofail`,
  the audit driver (global duplicate declarations, then axioms), the module-system audit, the
  environment lint (`lint-env`) and the source style lint (`lint-style`). Find the stage that failed
  in the log, then use the corresponding local check when it covers that failure. Local axiom checks
  inspect only changed modules' own declarations: global duplicates, transitive dependency axioms and
  unchanged importer failures must be diagnosed from the CI log and source. A local pass does not
  reproduce or dismiss those CI failures. Do not rerun every stage to diagnose one failure.
  Reproduce the failing phase through the helper, for example:
  ```
  "$TAUCETI_VERIFY_PYTHON" "__BIN__/tauceti-verify" --stage shim-expiry
  ```
  The other stage names are `build`, `axioms`, `module-system` and `lint`. The helper checks
  the merge-base shim registry with `--fail-on-available`, `--base-manifest` and `--base-root`,
  using `--only-new` for source-only PRs. It never fetches. If lint flags unchanged declarations,
  inspect their defining sources and current main before choosing a repair. To reproduce them, write
  their dotted module names to an absolute list outside the checkout and pass `--lint-modules /absolute/list`
  to both the diagnostic lint phase and the final all-stage helper call. Policy: `__SCOPE_POLICY__`.

## Fix it on its merits
- Diagnose the real cause (a broken proof, a renamed/missing Mathlib lemma, a linter error, an axiom-audit failure, a flaky/transient infra error). Fix the underlying problem.
- If the shim-expiry command fails, its annotations name exact Mathlib replacements and affected sources. Migrate only the superseded declarations/imports, preserve or re-home source-only API, and update `TauCeti/mathlib-shims.json` in the same source-only change. The checker derives each inherited source's declaration surface from the PR merge base and ratchets its probes until that surface is migrated, deleted, or re-homed under an entry preserving those probes, so never make the check green by merely deleting probes or changing an exact target to a speculative/landing sentinel.
- Only if the logs establish a transient infrastructure failure (e.g. a cache fetch timeout, or a cancelled build that nothing replaced), with no actionable source/audit violation, push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`). A runner shutdown counts only once you have measured memory as above. A passing local axiom check is never evidence that a CI audit failure was transient.
- Prefer the smallest correct fix. If a declaration is unsalvageable, it is better to remove it than to leave the PR red — but never gut the PR into vacuity; if nothing useful survives, follow the durable PR outcome rules below rather than pushing an empty shell. Partial overlap does not justify deleting valuable remaining work.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`. Resolve roadmap scope findings using the PR outcome instructions below: adapt to an existing target or close your own PR with an explanation.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force the build green — that defeats the point.

## Verify before pushing (ALL MUST pass)
__LOCAL_VERIFICATION__
After the fix, run the helper's default all stages. It covers build, changed-file axiom diagnostics,
module-system, lint and shim expiry; CI owns transitive axiom and global duplicate checks.
A successful build phase alone is insufficient.
If a phase fails, fix its inputs and let exact-input receipts determine which phases need rerunning.
Never push red.

**Do this synchronously, in this one turn.** You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the fix (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report (the next round re-syncs); do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: what was failing, the root cause, what you changed (or that you only re-triggered transient CI), which CI stage you reproduced, and the exact shim-expiry, `scope:`, `lake build` and `local-axioms:` result lines proving your local checks green + axiom-clean, labelled as local changed-file checks rather than full CI. Do not claim green unless you saw it.
