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

Preparation already provided Mathlib and its dependencies; do not run `lake exe cache get` or
`lake update`. If the Mathlib pin changes under you, stop and report instead of fetching or building Mathlib.
Do not run the old scope recipe, `scripts/lint-scope.sh`, `scripts/lint-env.sh` or `lake exe axioms`
alongside the shared checks. Local axiom diagnostics check direct references in changed modules;
full transitive axiom and global declaration-ownership audits remain in CI.

Keep commands in the foreground and wait for their actual exit. Allow verification the remaining
round budget: omit a shorter tool timeout, or request the remaining time explicitly. Compiler
capacity waits count toward wall time; do not impose a ten-minute timeout or start a second
verification while the first is running. If Codex returns a session ID,
wait on that session with write_stdin until it exits. If piping output, use `set -o pipefail`.
After a failure, rerun the failing phase (`sources`, `axioms`, `module-system`, `style`,
`environment` or `shim-expiry`) as needed; `lake build --iofail` is the build command. Repeat
successful checks only if their inputs changed. Preserve unfinished work at the round deadline.

If verification reports resource exhaustion or exits 75, stop retrying in this round. Preserve
your changes; the worker will retain them and retry with backoff. Do not edit mathematics to
fix host resource failures, and never describe deferred verification as green.

In your report quote the `scope:` and `local-axioms:` results and describe the verification as
local checks, not CI. A full CI scope policy never means a full local axiom audit ran.
