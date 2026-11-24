#!/usr/bin/env bash
# The TauCetiTools commit CI tests against: pyproject.toml's [tool.tauceti-ci] tools-rev.
#
#   tools-pin.sh apply   point this checkout's TauCetiTools dependency at the pin (CI only; it edits
#                        pyproject.toml and the ./tauceti script header in place)
#   tools-pin.sh apply --path DIR
#                        the same, at the TauCetiTools checkout in DIR instead of the pin
#                        (TauCetiTools' worker-contract CI tests a Tools pull request this way)
#   tools-pin.sh bump    open a Worker pull request that moves the pin to TauCetiTools main
#
# The declared dependency stays TauCetiTools main, which production installs follow (see the
# comment on [tool.tauceti-ci] in pyproject.toml). The pin keeps a TauCetiTools merge from changing
# Worker CI's verdict: a new Tools commit reaches Worker CI only through a bump pull request, whose
# CI runs the Worker suite against it.
set -euo pipefail

TOOLS_URL="git+https://github.com/TauCetiProject/TauCetiTools"
TOOLS_REPO="https://github.com/TauCetiProject/TauCetiTools"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYPROJECT="$ROOT/pyproject.toml"

die() { echo "tools-pin: $*" >&2; exit 1; }

# pinned [-]: the tools-rev in pyproject.toml, or in the pyproject.toml on stdin given -.
pinned() {
  local rev
  if [ "${1:-}" = - ]; then
    rev="$(sed -n 's/^tools-rev = "\([0-9a-f]\{40\}\)"$/\1/p')"
  else
    rev="$(sed -n 's/^tools-rev = "\([0-9a-f]\{40\}\)"$/\1/p' "$PYPROJECT")"
  fi
  [ "$(printf '%s\n' "$rev" | grep -c .)" = 1 ] || die "pyproject.toml needs exactly one tools-rev = \"<40-hex commit>\""
  printf '%s\n' "$rev"
}

# apply [--path DIR]
apply() {
  local target file
  if [ "$#" = 0 ]; then
    target="$TOOLS_URL@$(pinned)"
  elif [ "$#" = 2 ] && [ "$1" = --path ]; then
    [ -f "$2/pyproject.toml" ] || die "$2 is not a TauCetiTools checkout (no pyproject.toml)"
    target="$(cd -- "$2" && pwd)"
    # The path goes into a file:// URL unencoded and through sed, so allow only plain characters.
    [[ "$target" =~ ^[A-Za-z0-9/._+-]+$ ]] || die "unsupported characters in the path $target"
    target="file://$target"
  else
    die "usage: tools-pin.sh apply [--path DIR]"
  fi
  # Check both files before changing either, so a refusal leaves the checkout as it was.
  for file in "$PYPROJECT" "$ROOT/tauceti"; do
    grep -qF "tauceti-tools @ $TOOLS_URL@main" "$file" || die "$file does not depend on TauCetiTools main"
  done
  if grep -q '^\[tool\.uv\]' "$PYPROJECT"; then die "pyproject.toml already has [tool.uv]; merge the override into it"; fi
  for file in "$PYPROJECT" "$ROOT/tauceti"; do
    sed "s|tauceti-tools @ $TOOLS_URL@main|tauceti-tools @ $target|" "$file" >"$file.pin"
  done
  # TauCetiReview depends on TauCetiTools main; the override replaces every requirement on it.
  printf '\n[tool.uv]\noverride-dependencies = ["tauceti-tools @ %s"]\n' "$target" >>"$PYPROJECT.pin"
  for file in "$PYPROJECT" "$ROOT/tauceti"; do
    cat "$file.pin" >"$file" && rm "$file.pin"
  done
  echo "tools-pin: TauCetiTools pinned to $target"
}

bump() {
  local old new branch
  command -v gh >/dev/null || die "gh is required"
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || die "the checkout has uncommitted changes"
  git -C "$ROOT" fetch --quiet origin main
  old="$(git -C "$ROOT" show origin/main:pyproject.toml | pinned -)"
  new="$(git ls-remote --exit-code "$TOOLS_REPO" refs/heads/main | cut -f1)"
  [[ "$new" =~ ^[0-9a-f]{40}$ ]] || die "cannot resolve TauCetiTools main"
  if [ "$new" = "$old" ]; then echo "tools-pin: Worker main already pins TauCetiTools main ($old)"; return 0; fi
  branch="chore/bump-tools-${new:0:12}"
  git -C "$ROOT" switch --quiet -c "$branch" origin/main
  sed "s/^tools-rev = \"$old\"$/tools-rev = \"$new\"/" "$PYPROJECT" >"$PYPROJECT.pin"
  cat "$PYPROJECT.pin" >"$PYPROJECT" && rm "$PYPROJECT.pin"
  [ "$(pinned)" = "$new" ] || die "failed to rewrite tools-rev"
  git -C "$ROOT" commit --quiet -m "chore: bump the TauCetiTools pin to ${new:0:12}" -- pyproject.toml
  git -C "$ROOT" push --quiet -u origin "$branch"
  gh pr create --repo TauCetiProject/TauCetiWorker --base main --head "$branch" \
    --title "chore: bump the TauCetiTools pin to ${new:0:12}" \
    --body "This PR moves the TauCetiTools commit that Worker CI tests against from ${old:0:12} to ${new:0:12} (TauCetiTools main): $TOOLS_REPO/compare/$old...$new

If CI fails here, the TauCetiTools changes in that range break the Worker: fix the Worker in this PR, or fix TauCetiTools and bump again.

Opened by \`.github/scripts/tools-pin.sh bump\`."
}

case "${1:-}" in
  apply) shift; apply "$@" ;;
  bump) bump ;;
  *) die "usage: tools-pin.sh apply [--path DIR] | bump" ;;
esac
