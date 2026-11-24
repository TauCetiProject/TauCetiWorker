#!/usr/bin/env bash
# The TauCetiTools commit CI tests against: pyproject.toml's [tool.tauceti-ci] tools-rev.
#
#   tools-pin.sh apply   point this checkout's TauCetiTools dependency at the pin (CI only; it edits
#                        pyproject.toml and the ./tauceti script header in place)
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

pinned() {
  local rev
  rev="$(sed -n 's/^tools-rev = "\([0-9a-f]\{40\}\)"$/\1/p' "$PYPROJECT")"
  [ "$(printf '%s\n' "$rev" | grep -c .)" = 1 ] || die "pyproject.toml needs exactly one tools-rev = \"<40-hex commit>\""
  printf '%s\n' "$rev"
}

apply() {
  local rev file
  rev="$(pinned)"
  for file in "$PYPROJECT" "$ROOT/tauceti"; do
    grep -qF "tauceti-tools @ $TOOLS_URL@main" "$file" || die "$file does not depend on TauCetiTools main"
    sed "s|tauceti-tools @ $TOOLS_URL@main|tauceti-tools @ $TOOLS_URL@$rev|" "$file" >"$file.pin"
    cat "$file.pin" >"$file" && rm "$file.pin"
  done
  grep -q '^\[tool\.uv\]' "$PYPROJECT" && die "pyproject.toml already has [tool.uv]; merge the override into it"
  # TauCetiReview depends on TauCetiTools main; the override replaces every requirement on it.
  printf '\n[tool.uv]\noverride-dependencies = ["tauceti-tools @ %s@%s"]\n' "$TOOLS_URL" "$rev" >>"$PYPROJECT"
  echo "tools-pin: TauCetiTools pinned to $rev"
}

bump() {
  local old new branch
  command -v gh >/dev/null || die "gh is required"
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || die "the checkout has uncommitted changes"
  git -C "$ROOT" fetch --quiet origin main
  old="$(pinned)"
  new="$(git ls-remote --exit-code "$TOOLS_REPO" refs/heads/main | cut -f1)"
  [[ "$new" =~ ^[0-9a-f]{40}$ ]] || die "cannot resolve TauCetiTools main"
  [ "$new" != "$old" ] || { echo "tools-pin: already at TauCetiTools main ($old)"; return 0; }
  branch="chore/bump-tools-${new:0:12}"
  git -C "$ROOT" switch --quiet -c "$branch" origin/main
  old="$(pinned)"
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
  apply) apply ;;
  bump) bump ;;
  *) die "usage: tools-pin.sh apply|bump" ;;
esac
