# Useful local checks, then self-tested PRs

## Stage 1 — Make local verification useful

Authors use:

```bash
lake exe cache get && lake cache get && lake build --iofail && lake test && lake lint
```

| Command | Behaviour |
| --- | --- |
| Cache commands | Keep their existing behaviour. |
| `lake build --iofail` | Build the library and affected importers; fail on informational diagnostics. TauCeti already treats warnings as errors. |
| `lake test` | Check changed modules for forbidden axioms and run the existing module-system audit. |
| `lake lint` | Run source style, changed-module environment/documentation/`nolint` checks, applicable shim checks, and the cheap CI source guards: forbidden options/metaprogramming, import boundaries/headers, source shape and new-file length. |

Make these changes:

1. **Put shared local checkers in TauCetiTools.** Add Lake test/lint drivers that call them. Reuse CI implementations where the checks overlap, and align the existing Tools pins in the checkout, worker and affected workflows.
2. **Replace the worker's separate recipes with those shared checks.** Run cheap source checks before its existing build and artifact checks afterwards. Update authoring, fix, rebase and bump prompts; remove the replaced recipes so checks run once. Keep the verifier's existing receipts, logs and reuse.
3. **Keep the existing offline scope calculation.** Include uncommitted and new files, retaining its existing handling of missing history and broad changes.

Local checks cover the author's sources. Staging keeps the expensive global ownership and transitive axiom audits and tests the integrated result. Stage 1 does not change PR admission or CI policy, and does not add a local synthetic merge with main.

Land Tools support before its TauCeti and Worker consumers. Use the existing tests to check command wiring and failure propagation, plus a real `lake test` / `lake lint` smoke test. Deploy workers live using the existing deployment path.

**Result:** the familiar commands catch the common CI failures locally, and workers use the same checkers. No extra author command, receipt format, upload, service or verification pass is introduced.

## Stage 2 — Let trusted authors skip expensive PR CI

An author runs the Stage 1 commands and applies `self-tested`. The label asserts that the current PR head passed. Workers can apply it after their normal successful verification.

Accept it for PRs whose **author belongs to a designated GitHub team**, initially **Kim, David, Chris and the ai-for-science account**. Resolve the logins and team slug during implementation. A team member labelling somebody else's PR does not make it eligible.

The route applies to ordinary mathematics changes entirely under `TauCeti/`, with unchanged pins and lakefile. Infrastructure changes and explicit full-lint/lint-repair requests retain their existing CI route.

Change the existing path as follows:

| Step | Behaviour for an eligible `self-tested` PR |
| --- | --- |
| PR CI | Keep the cheap scope decision; skip the ordinary expensive build before runner allocation and cache preparation. Cancel redundant ordinary builds already queued or running where practical. |
| Review and admission | Require the existing reviews and admission rules, accepting `self-tested` in place of green ordinary PR `build`. Update review scheduling and worker selection so they do not wait for or repair the skipped build. |
| Reporting | Say “PR CI skipped: self-tested.” |
| Staging | Admit the reviewed PR through the existing staging path, with all current candidate tests and repair behaviour. |

A push clears the label; the author reapplies it after testing. Main advancing does not clear it. Removing the label or leaving the allowed scope returns an unadmitted PR to ordinary CI. Use the existing event/reconciliation machinery to handle missed events and membership changes; unavailable membership information falls back to ordinary CI. Accepted staging members retain their existing membership rules.

Update the workflow and all callers that currently require green PR `build` together, so skipped CI does not strand PRs. Exercise the changed routes in their existing test suites, then enable the initial team and deploy live.

**Result:** trusted authors test, label and obtain the existing review approval, then enter staging without an expensive PR build. The label is the attestation; there is no additional submission or approval protocol.

## Existing CI distinctions

TauCeti's ordinary PR build currently integrates with captured main, following [#17052](https://github.com/TauCetiProject/TauCeti/pull/17052). This plan leaves that policy alone for PRs using ordinary CI.

Staging's axiom/ownership audit is global. Its environment lint uses changed declarations, downstream `unusedArguments` and global documentation checks, supplemented by the separate full-lint workflow. This plan does not expand that coverage.

Implementation references: [worker verification](../verification.md), [local recipes](../../prompts/local-verification.md), [CI build body](https://github.com/TauCetiProject/TauCetiTools/blob/d288926c619a7247c92a1000d44ff9157220ff54/tauceti_tools/assets/scripts/sandbox-build.sh), and [admission policy](https://github.com/TauCetiProject/TauCetiReview/blob/f2c57dda135775ec0117a6ae18ae9443f3e56601/runner/admission_policy.py).
