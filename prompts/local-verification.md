### How to run local checks
Other agents share this machine. Never run `lake exe axioms` locally: its scope filter still loads
every TauCeti and Mathlib module. The local checker below reads only changed modules' own artifacts.
Verify once, at the end, scoped to what you actually changed:

- **Foreground only, real exit statuses.** Run each command in the foreground and wait for it to
  exit; judge it by its own exit status, not by its output looking finished. Whenever you pipe a
  command's output (`| tee`, `| tail`, `| grep`), start that shell with `set -o pipefail` so the
  pipe reports the command's status. Never background a build or audit, never wait for one with
  `pgrep`, `pkill`, `ps`, process-name matching or sleep loops, and never end your turn expecting a
  background job to notify or resume you: nothing will.
- **No repeats.** Run `lake exe cache get` once per session, and again only after `lake-manifest.json`
  or `lean-toolchain` changes. Do not rerun a check that already passed unless its inputs (source,
  pins, or the integrated main) changed since. While iterating on a failure, rerun only the failing
  step; `lake build` is incremental, so rerun it after edits. The final verification always uses
  the plain whole-library `lake build --iofail`, as CI does: a hand-picked module list skips downstream importers that
  your change can break, so it never counts as verification.
- **Scope, not guesswork.** Compute the audit scope with the recipe below, exactly as written, after
  your last source edit, merge, or commit. It reads only local Git state: do not fetch for it, do not
  query PR labels for it, and never run `scripts/lint-scope.sh` (it makes live GitHub requests). This
  round's scope policy, from the worker's survey of this PR's labels and branch, is
  `__SCOPE_POLICY__`; this describes CI requirements, never a reason for a whole-library local audit.

The recipe compares your working tree (committed, staged, unstaged and untracked files, deletions,
and both ends of every rename) with its merge base on the canonical `TauCetiProject/TauCeti` main,
writes the changed existing modules to an absolute module list, and prints the scope it chose:
`scoped` (changed modules), `full: <reason>` (CI needs a full check: a pin, lakefile or toolchain change, a deleted module, a
Lean file outside the module inventory, or a broad policy), or `unknown: <reason>` (the base or the
changed files could not be determined: stop the local audit, and no local environment lint). An empty
`scoped` list (no Lean change at all) is a valid result; a failure to list files is never empty.
```bash
bash <<'TAUCETI_SCOPE'
set -uo pipefail
export LC_ALL=C
policy='__SCOPE_POLICY__'
root="$(git rev-parse --show-toplevel)" && cd "$root" || exit 1
out="$root/.lake/tauceti-scope"
rm -rf "$out" && mkdir -p "$out" && : > "$out/modules.txt" || exit 1
finish() {
  printf '%s\n' "$1" > "$out/mode" || exit 1
  echo "scope: $1 (module list $out/modules.txt)"
  sed 's/^/  /' "$out/modules.txt"
  exit 0
}
main=""
shopt -s nocasematch
# Raw config, not `git remote get-url`: sandboxes rewrite github.com through a proxy with insteadOf.
for remote in $(git remote); do
  [[ "$(git config --get "remote.$remote.url")" =~ \
     ^(https://([^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)TauCetiProject/TauCeti(\.git)?/?$ ]] \
    || continue
  main="$(git rev-parse --verify --quiet "refs/remotes/$remote/main^{commit}")" && break
done
shopt -u nocasematch
[ -n "$main" ] || finish "unknown: no local canonical TauCetiProject/TauCeti main ref"
[ "$(git rev-parse --is-shallow-repository)" = false ] || finish "unknown: shallow history"
base="$(git merge-base "$main" HEAD)" || finish "unknown: no merge base with main $main"
git diff --no-ext-diff --no-textconv --no-renames --no-relative --name-only -z "$base" -- \
  > "$out/changed.nul" || finish "unknown: cannot diff against $base"
# Untracked files, including ignored ones under TauCeti/ (Lake builds every source there).
{ git ls-files -z --others --exclude-standard -- . \
    && git ls-files -z --others --ignored --exclude-standard -- TauCeti; } >> "$out/changed.nul" \
  || finish "unknown: cannot list untracked files"
module_re="^TauCeti(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$"
reason=""
while IFS= read -r -d '' file; do
  case "$file" in
    lake-manifest.json|lean-toolchain|lakefile.toml|lakefile.lean)
      reason="${reason:-dependency, toolchain or lakefile change: $file}"; continue ;;
    *.lean) ;;
    *) continue ;;
  esac
  if ! [[ "$file" =~ $module_re ]]; then
    reason="${reason:-Lean file outside the module inventory: $file}"
  elif [ ! -f "$root/$file" ]; then
    reason="${reason:-deleted module: $file}"
  else
    module="${file%.lean}"
    printf '%s\n' "${module//\//.}" >> "$out/modules.txt" || reason="${reason:-cannot write the module list}"
  fi
done < "$out/changed.nul" || finish "unknown: cannot read the changed-file list"
sort -u -o "$out/modules.txt" "$out/modules.txt" || finish "unknown: cannot sort the module list"
[ "$policy" = ordinary ] || reason="${reason:-$policy policy}"
[ -z "$reason" ] || finish "full: $reason"
finish "scoped against $base"
TAUCETI_SCOPE
```
Then run the cheap local axiom check, even for broad CI policies. It checks the listed modules'
own compiled declarations, including private proofs, for new axioms and direct uses of `sorryAx`
or Lean's native-decision axioms. It does not follow imports or run candidate initializers.
Full transitive axiom, downstream-importer and global duplicate checks run in CI.
```bash
root="$(git rev-parse --show-toplevel)" && cd "$root" || exit 1
scope="$(git rev-parse --show-toplevel)/.lake/tauceti-scope"
case "$(head -n 1 "$scope/mode" 2>/dev/null)" in
  scoped*|full*) AXIOMS_ONLY_MODULES= __LOCAL_AXIOMS__ --modules "$scope/modules.txt" ;;
  unknown*) echo "local-axioms: changed modules unknown; preserve edits and repair local base/history through checkout preparation. Do not retry unchanged scope or run a full audit." >&2; false ;;
  *) echo "no audit scope: run the scope recipe first" >&2; false ;;
esac
```
Where this prompt asks for the environment lint, run it on the same list:
```bash
scope="$(git rev-parse --show-toplevel)/.lake/tauceti-scope"
case "$(head -n 1 "$scope/mode" 2>/dev/null)" in
  unknown*) echo "lint-env: changed modules unknown; CI runs the environment lint" ;;
  scoped*|full*)
    if [ -s "$scope/modules.txt" ]; then LINT_ONLY_MODULES="$scope/modules.txt" bash scripts/lint-env.sh
    else echo "lint-env: no changed TauCeti module to lint"; fi ;;
  *) echo "no lint scope: run the scope recipe first" >&2; false ;;
esac
```
Never run `bash scripts/lint-env.sh` without `LINT_ONLY_MODULES`; CI owns the whole-library lint.

In your final report, quote the `scope:` and `local-axioms:` lines and label them as local changed-file
checks. A `full:` scope means full CI is required; it does not claim a full local audit ran.
These checks are not full CI, which still runs every check on the pushed head.
