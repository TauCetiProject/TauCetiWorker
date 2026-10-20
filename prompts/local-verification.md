### How to run local checks
Other agents share this machine, and a whole-library audit loads every TauCeti and Mathlib module.
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
  the plain whole-library `lake build`: a hand-picked module list skips downstream importers that
  your change can break, so it never counts as verification.
- **Scope, not guesswork.** Compute the audit scope with the recipe below, exactly as written, after
  your last source edit, merge, or commit. It reads only local Git state: do not fetch for it, do not
  query PR labels for it, and never run `scripts/lint-scope.sh` (it makes live GitHub requests). This
  round's scope policy, from the worker's survey of this PR's labels and branch, is
  `__SCOPE_POLICY__`; any policy other than `ordinary` audits the whole library.

The recipe compares your working tree (committed, staged, unstaged and untracked files, deletions,
and both ends of every rename) with its merge base on the canonical `TauCetiProject/TauCeti` main,
writes the changed existing modules to an absolute module list, and prints the scope it chose:
`scoped` (the auditor then also checks every module importing the listed ones, plus global
duplicate declarations), `full: <reason>` (a pin, lakefile or toolchain change, a deleted module, a
Lean file outside the module inventory, or a broad policy), or `unknown: <reason>` (the base or the
changed files could not be determined: also a full audit, and no local environment lint). An empty
`scoped` list (no Lean change at all) is a valid result; a failure to list files is never empty.
```bash
bash <<'TAUCETI_SCOPE'
set -uo pipefail
export LC_ALL=C
policy='__SCOPE_POLICY__'
root="$(git rev-parse --show-toplevel)" || exit 1
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
for remote in $(git remote); do
  [[ "$(git remote get-url "$remote" 2>/dev/null)" =~ github\.com[:/]+TauCetiProject/TauCeti(\.git)?/*$ ]] \
    || continue
  main="$(git rev-parse --verify --quiet "refs/remotes/$remote/main^{commit}")" && break
done
shopt -u nocasematch
[ -n "$main" ] || finish "unknown: no local canonical TauCetiProject/TauCeti main ref"
[ "$(git rev-parse --is-shallow-repository)" = false ] || finish "unknown: shallow history"
base="$(git merge-base "$main" HEAD)" || finish "unknown: no merge base with main $main"
git -c core.quotePath=false diff --no-ext-diff --no-textconv --no-renames --name-only -z "$base" -- \
  > "$out/changed.nul" || finish "unknown: cannot diff against $base"
git ls-files -z --others --exclude-standard >> "$out/changed.nul" \
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
    printf '%s\n' "${module//\//.}" >> "$out/modules.txt"
  fi
done < "$out/changed.nul"
sort -u -o "$out/modules.txt" "$out/modules.txt" || finish "unknown: cannot sort the module list"
[ "$policy" = ordinary ] || reason="${reason:-$policy policy}"
[ -z "$reason" ] || finish "full: $reason"
finish "scoped against $base"
TAUCETI_SCOPE
```
Then run the axiom and duplicate-declaration audit in the scope just written:
```bash
scope="$(git rev-parse --show-toplevel)/.lake/tauceti-scope"
case "$(head -n 1 "$scope/mode" 2>/dev/null)" in
  scoped*) AXIOMS_ONLY_MODULES="$scope/modules.txt" lake exe axioms ;;
  full*|unknown*) lake exe axioms ;;
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

In your final report, quote the `scope:` line and say plainly that these were local checks (scoped
or full, as printed). They are not full CI, which still runs every check on the pushed head.
