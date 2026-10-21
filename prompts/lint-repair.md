You are repairing the environment lint of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib, on pull request #__PR__. You are in a checkout of the repo, already on the PR's branch. TauCeti's daily full lint (`.github/workflows/lint-full.yml`) opened this PR because `main` carries environment-lint violations that are not in the grandfathered baseline. PR builds lint only the modules a change touches, so a change can break lint in a module it did not touch; this PR's build lints the whole library (it is on a `lint-repair/` branch and labelled `full-lint`), and it is red until those violations are fixed. Work autonomously to completion: make its build green by fixing `TauCeti/`, without weakening the library.

## Bring the branch up to date first
The daily lint comments on an open repair PR rather than replacing it, so `main` may have moved since this branch was cut. Start by merging it, then work from the current full lint:
```
git fetch -q origin main
git merge --no-edit origin/main
```
If the merge conflicts, resolve it (conflicts can only be in `TauCeti/`), or follow the PR outcome instructions below.

## What the violations usually are
- **simpNF**: a `@[simp]` lemma whose left-hand side simp now rewrites, usually because a newer simp lemma elsewhere took it out of simp normal form, or because a newer `attribute [simp]` made an old lemma a simp lemma. The linter's message says which lemmas simp used. Fix it by restating the lemma so its left-hand side is in simp normal form, or by removing `@[simp]` from whichever of the two lemmas is redundant or points the wrong way. Check what uses a lemma (`grep -rn` for its name) before removing `@[simp]`.
- **simpComm**: a commutativity lemma marked `@[simp]`. Remove the attribute.
- **Other linters** (checkType, synTaut, unusedArguments, docBlame, ...): the message explains the problem; fix the declaration.

Repair existing declarations only: restate, re-annotate, or (if redundant) remove them. Do NOT add new lemmas or definitions: new mathematics needs a roadmap target, which a repair PR does not have. If a violation can only be fixed by adding one, follow the PR outcome instructions below.

The PR body, comments and failing CI log list the violations to repair. Find each declaration's
defining module, inspect the source, and fix the underlying issue. The helper lints changed modules
and performs fast changed-file axiom checks; CI owns the full audit under this branch's policy. For a declaration flagged in an unchanged module, write its dotted module name to an absolute
list outside this checkout, then run `"$TAUCETI_VERIFY_PYTHON" "__BIN__/tauceti-verify" --stage lint --lint-modules /absolute/list`.
Use that same list on the final all-stage call. Whole-library environment lint stays
in CI; a local success does not assert that every unchanged module passed it.

- For a failing check's CI logs: `gh pr checks __PR__ --repo TauCetiProject/TauCeti`, then `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed`.
- If the failure is genuinely transient infra (e.g. a cache fetch timeout) and everything below passes locally, push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so.

## Rules of the repo (hard constraints)
- Fix code under `TauCeti/` only. Do NOT edit `scripts/` (including `scripts/lint-baseline.txt` and `scripts/lint-nolints-allowlist.txt`), `.github/`, the lakefile, or the Lake pins. Do NOT add `@[nolint ...]`: every nolint must be allowlisted in a human-owned file, so CI rejects it. If a violation genuinely should be an exception, follow the PR outcome instructions below instead of silencing it.
- Do NOT edit the root `TauCeti.lean`: it is intentionally empty.
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
  Tau Ceti does not preserve backwards compatibility: if you rename or restate a lemma, update every
  use in the repository in the same change.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), and no `set_option` in `TauCeti/` at all (CI rejects it; it is how `maxHeartbeats` overrides and linter silencing happen).

## Verify before pushing (ALL of these MUST pass; they are what the `build` check runs)
__LOCAL_VERIFICATION__
Run the helper's default all stages after the final edit. It owns scoped environment lint,
full build and audits, style lint and the merge-base shim ratchet. Inspect the phase exits and receipt;
fix failures before pushing.

**Do this synchronously, in this one turn.** You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the repair (message `<type>: <subject>`, imperative present, e.g. `fix: restore simp normal form for ...`; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `--force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so; do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch files outside `TauCeti/` (the merge from `main` is the only exception).

## Report
End with a concise summary: each violation you fixed and how, any remaining violations, and the exact `scope:` and result lines of the verification commands proving your local checks green, labelled as local rather than CI. Do not claim green unless you saw it.
