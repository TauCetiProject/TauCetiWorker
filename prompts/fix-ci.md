You are fixing FAILING CI on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The `build` check is red, or the build job finished without posting it. Work autonomously to completion: make CI green without weakening the PR.

## Find out what's actually failing
- See which checks failed and read their logs:
  - `gh pr checks __PR__ --repo TauCetiProject/TauCeti`
  - `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed` (use the run id from the failing check)
- If the head has no `build` check at all, the `sandboxed-build` job died before its report step. When
  its log ends in `The runner has received a shutdown signal` (exit code 143) during `lake build`, the
  build almost always ran the 16 GB runner out of memory, and re-triggering CI repeats it. Once a
  changed module's imports are built, measure its peak memory with
  `python3 -c 'import resource, subprocess, sys; subprocess.run(sys.argv[1:]); print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)' lake build <Module>`
  (kilobytes on Linux) and bring every changed module well under 16 GB, typically by restructuring a
  large `decide` over a finite type so that the kernel checks less at once.
- Reproduce locally — this is the source of truth, not the log alone. The single `build` check bundles
  the sandboxed build, the audits, and the lint, so run the WHOLE suite, not just `lake build`:
  ```
  lake exe cache get
  git fetch -q origin main
  shim_args=(--fail-on-available); base_shims="$(mktemp)"; base_root="$(mktemp -d)"; have_base=0
  base_ref="$(git merge-base origin/main HEAD)"
  if git show "$base_ref":TauCeti/mathlib-shims.json > "$base_shims" 2>/dev/null; then git archive "$base_ref" TauCeti | tar -x -C "$base_root"; shim_args+=(--base-manifest "$base_shims" --base-root "$base_root"); have_base=1; fi
  if [ "$have_base" = 1 ] && git diff --quiet "$base_ref" -- lake-manifest.json lean-toolchain; then shim_args+=(--only-new); fi
  if [ -f scripts/check-expired-mathlib-shims.py ]; then python3 scripts/check-expired-mathlib-shims.py "${shim_args[@]}"; fi
  rm -f "$base_shims"; rm -rf "$base_root"
  lake build
  lake exe axioms
  lake exe module-system
  ```
  The `build` check also runs the environment lint, on the TauCeti modules the PR changes. Run it
  locally scoped the same way:
  ```
  lint_modules="$(mktemp)"
  {
    git diff --name-only --diff-filter=d origin/main...HEAD -- 'TauCeti/*.lean'
    # Only if the PR carries the `full-lint` label (see below):
    # for name in <last component of each declaration the failing log flags>; do
    #   grep -rlw --include='*.lean' -- "$name" TauCeti
    # done
  } | sed 's/\.lean$//; s|/|.|g' | sort -u > "$lint_modules"
  LINT_ONLY_MODULES="$lint_modules" bash scripts/lint-env.sh
  ```
  Never run `bash scripts/lint-env.sh` without `LINT_ONLY_MODULES`: that lints the whole library, which takes minutes on every core of a machine other agents share. CI runs the environment lint; leave the whole-library lint to it.
  If the PR carries the `full-lint` label, CI lints the whole library instead, so the changed
  modules are not enough: uncomment the loop above, listing the last name component of each
  declaration the failing log flags (`<linter> <declaration>`), so their defining modules are
  linted too.
  If `lint-env` flags a declaration that is NOT in your diff, your branch is likely behind main (CI
  overlays your `TauCeti/` onto current main): merge `main` into the branch and re-check.

## Fix it on its merits
- Diagnose the real cause (a broken proof, a renamed/missing Mathlib lemma, a linter error, an axiom-audit failure, a flaky/transient infra error). Fix the underlying problem.
- If the shim-expiry command fails, its annotations name exact Mathlib replacements and affected sources. Migrate only the superseded declarations/imports, preserve or re-home source-only API, and update `TauCeti/mathlib-shims.json` in the same source-only change. The checker derives each inherited source's declaration surface from the PR merge base and ratchets its probes until that surface is migrated, deleted, or re-homed under an entry preserving those probes, so never make the check green by merely deleting probes or changing an exact target to a speculative/landing sentinel.
- If the failure is genuinely transient/infra (e.g. cache fetch timeout; a runner shutdown is not, see above), the code and shim-expiry command are green locally, and the failed logs contain no actionable migration, do NOT hack the code — push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so in your report.
- Prefer the smallest correct fix. If a declaration is unsalvageable, it is better to remove it than to leave the PR red — but never gut the PR into vacuity; if almost nothing survives, stop and report that rather than pushing an empty shell.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force the build green — that defeats the point.

## Verify before pushing (ALL of these MUST pass — they are exactly what the `build` check runs)
```
lake exe cache get
git fetch -q origin main
shim_args=(--fail-on-available); base_shims="$(mktemp)"; base_root="$(mktemp -d)"; have_base=0
base_ref="$(git merge-base origin/main HEAD)"
if git show "$base_ref":TauCeti/mathlib-shims.json > "$base_shims" 2>/dev/null; then git archive "$base_ref" TauCeti | tar -x -C "$base_root"; shim_args+=(--base-manifest "$base_shims" --base-root "$base_root"); have_base=1; fi
if [ "$have_base" = 1 ] && git diff --quiet "$base_ref" -- lake-manifest.json lean-toolchain; then shim_args+=(--only-new); fi
if [ -f scripts/check-expired-mathlib-shims.py ]; then python3 scripts/check-expired-mathlib-shims.py "${shim_args[@]}"; fi
rm -f "$base_shims"; rm -rf "$base_root"
lake build
lake exe axioms
lake exe module-system
```
Then run the scoped environment lint above and confirm it prints `LINT-ENV: PASS`.
Iterate until every one is green. A green `lake build` alone is NOT enough — the `build` check also
fails on an axiom-audit, module-system, or lint-env violation (e.g. a missing docstring). Never push red.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the fix (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report (the next round re-syncs); do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: what was failing, the root cause, what you changed (or that you only re-triggered transient CI), and the exact shim-expiry / `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
