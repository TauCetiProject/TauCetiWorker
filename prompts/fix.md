You are addressing AI code review on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. Work autonomously to completion.

## Read the review
- The review is posted as a sticky scoreboard comment plus one thread per flagged rubric. Read them:
  - `gh pr view __PR__ --repo TauCetiProject/TauCeti --json comments`
  - `gh api "/repos/TauCetiProject/TauCeti/pulls/__PR__/comments?per_page=100"` (the per-rubric review threads; each root carries a `<!--tauceti-rubric:NAME-->` marker, and the finding text + suggested fix).
- The blocking rubrics are the ones marked ⛔ (block) or 🟡 (changes requested) on the scoreboard. The other rubrics are already ✅ approved — note which ones.

## Do not regress what is already green
The scoreboard shows several rubrics already approved (✅). A re-review re-runs the rubrics you touched, so a change that fixes one blocker but degrades an approved rubric will turn that rubric red and the PR will not converge — this is the single most common reason a nearly-done PR is eventually abandoned. So:
- Make the SMALLEST change that clears each blocker; do not refactor or restructure beyond what the finding requires.
- Before pushing, re-read the approved rubrics (scope, reuse, generality, api-design, placement, naming, documentation, proof-quality, …) and confirm your change does not undermine any of them — e.g. don't add a less-general lemma (generality), a duplicate of Mathlib (reuse), an unexposed/ misplaced declaration (placement/api-design), or an undocumented public def (documentation).
- If clearing a blocker would genuinely force a regression of an approved rubric, that tension is a sign the finding may be wrong — contest it (below) with that trade-off as evidence, rather than pushing a change that just moves the redness around.

## Decide, per finding, on its merits
For each finding, judge whether it is actually correct:
- **If it is correct**, fix the code. Verify the fix empirically (does it build? does the claimed Mathlib lemma actually exist — `grep`/`#check`? does the suggested `@[simp]` lemma have a variable head, which the linter forbids?). Reviewers are sometimes confidently wrong; do not blindly comply.
- **If it is wrong**, do NOT comply. Reply on that rubric's thread explaining why, with evidence (a synth-check, a Mathlib citation, a build error). Post the reply to the thread root:
  `gh api -X POST "/repos/TauCetiProject/TauCeti/pulls/__PR__/comments/<ROOT_ID>/replies" -f body="..."`
  (A re-review reads these replies, so a well-evidenced contest can clear a wrong finding.)

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it (if a reviewer claims your API is not reachable from the root, the glob already covers it). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` by default. For dot-notation APIs whose first explicit argument has an
  existing Lean or Mathlib type, put declarations in that type's existing namespace (e.g. root
  `Set` or `RingEquiv`), as required by the naming rubric; do not nest it under `TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If a finding means the PR's target is not on any roadmap, say so in your report and stop.
- Must stay green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force a change through — that is itself a reason to push back on the finding.

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the fixes (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report (the next round re-syncs and retries); do not work around it. A successful push updates the PR; a re-review runs separately.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which findings you fixed (and how you verified each), which you contested (and the evidence), and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
