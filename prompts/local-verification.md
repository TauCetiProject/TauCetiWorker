### Local verification

After your last source edit, run the shared local verification once, synchronously:

```bash
__LOCAL_CHECKS__ verify --policy __SCOPE_POLICY__
```

This is the worker-compatible entry point for the same checks exposed by `lake test` and
`lake lint`, including older checkouts whose lakefile does not yet have those drivers. It runs
cheap source guards, the whole-library `lake build --iofail`, shim checks, changed-module
axiom diagnostics, the module-system audit, source style and changed-module environment lint.
It calculates scope from local Git state, including your uncommitted and newly created files;
do not fetch or query GitHub to calculate scope. Unknown scope is incomplete verification.

Run `lake exe cache get` only if preparation has not already fetched Mathlib, or the pins changed.
Do not run the old scope recipe, `scripts/lint-scope.sh`, `scripts/lint-env.sh` or `lake exe axioms`
alongside the shared checks. Local axiom diagnostics check direct references in changed modules;
full transitive axiom and global declaration-ownership audits remain in CI.

Keep commands in the foreground and wait for their actual exit. If Codex returns a session ID,
wait on that session with write_stdin until it exits. If piping output, use `set -o pipefail`.
After a failure, rerun the failing phase (`sources`, `axioms`, `module-system`, `style`,
`environment` or `shim-expiry`) as needed; `lake build --iofail` is the build command. Repeat
successful checks only if their inputs changed. Preserve unfinished work at the round deadline.

In your report quote the `scope:` and `local-axioms:` results and describe the verification as
local checks, not CI. A full CI scope policy never means a full local axiom audit ran.
