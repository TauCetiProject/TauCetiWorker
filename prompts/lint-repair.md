You are repairing the environment lint of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib, on pull request #__PR__. You are in a checkout of the repo, already on the PR's branch. TauCeti's daily full lint (`.github/workflows/lint-full.yml`) opened this PR because `main` carries environment-lint violations that are not in the grandfathered baseline. PR builds lint only the modules a change touches, so a change can break lint in a module it did not touch; this PR's build lints the whole library (it is on a `lint-repair/` branch and labelled `full-lint`), and it is red until those violations are fixed. Work autonomously to completion: make its build green by fixing `TauCeti/`, without weakening the library.

## What the violations usually are
- **simpNF**: a `@[simp]` lemma whose left-hand side simp now rewrites, usually because a newer simp lemma elsewhere took it out of simp normal form, or because a newer `attribute [simp]` made an old lemma a simp lemma. The linter's message says which lemmas simp used. Fix it by restating the lemma so its left-hand side is in simp normal form, by removing `@[simp]` from whichever of the two lemmas is redundant or points the wrong way, or by adding the missing simp lemma that makes the two agree. Prefer the change that keeps `simp` strongest and the API cleanest; check what uses the lemma (`grep -rn` for its name) before removing `@[simp]`.
- **simpComm**: a commutativity lemma marked `@[simp]`. Remove the attribute.
- **Other linters** (checkType, synTaut, unusedArguments, ...): the message explains the problem; fix the declaration.

The PR body and its comments list the violations the daily lint found, but always work from the current full lint, below: `main` may have moved.

## Reproduce and fix
```
lake exe cache get
lake build
bash scripts/lint-env.sh
```
Run `lint-env.sh` with `LINT_ONLY_MODULES` unset, so it lints the whole library. It prints each new violation with the linter's explanation, and a PASS/FAIL line.
- For a failing check's CI logs: `gh pr checks __PR__ --repo TauCetiProject/TauCeti`, then `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed`.
- If the failure is genuinely transient infra (e.g. a cache fetch timeout) and `lint-env.sh` passes locally, push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so.

## Rules of the repo (hard constraints)
- Fix code under `TauCeti/` only. Do NOT edit `scripts/` (including `scripts/lint-baseline.txt` and `scripts/lint-nolints-allowlist.txt`), `.github/`, the lakefile, or the Lake pins. Do NOT add `@[nolint ...]`: every nolint must be allowlisted in a human-owned file, so CI rejects it. If a violation genuinely should be an exception, stop and say so in your report instead of silencing it.
- Do NOT edit the root `TauCeti.lean`: it is intentionally empty.
- Everything under `namespace TauCeti`. Tau Ceti does not preserve backwards compatibility: if you rename or restate a lemma, update every use in the repository in the same change.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter (e.g. with `set_option ... false`) to force the build green.

## Verify before pushing (all MUST pass)
```
lake build
lake exe axioms
bash scripts/lint-env.sh
```
Iterate until `lint-env.sh` prints `LINT-ENV: PASS`. Never push red.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the repair (message `<type>: <subject>`, imperative present, e.g. `fix: restore simp normal form for ...`; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `--force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so; do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch files outside `TauCeti/`.

## Report
End with a concise summary: each violation you fixed and how, anything you judged should be a human-approved exception instead, and the exact `lint-env.sh` / `lake build` / `lake exe axioms` result lines proving green. Do not claim green unless you saw it.
