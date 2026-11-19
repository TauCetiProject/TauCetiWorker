#!/usr/bin/env bash
# Install or update the Forgejo TauCeti mirror on this host (services/forgejo-mirror/README.md).
#
# Installs exactly what origin/main of TauCetiWorker contains, whatever copy of this script is run:
# it fetches the shared clone at $TAUCETI_FORGEJO_MIRROR_CHECKOUT (default ~/TauCetiForgejoMirror,
# cloned when absent; its working tree is never touched), copies forgejo_mirror.py,
# forgejo_restore.py and the two units from origin/main, reloads systemd and enables the timer.
# Rerunning it is an update.
#
# Usage: services/forgejo-mirror/install.sh [--no-start]
#
#   --no-start   install files and units but do not enable the timer or start a run.
#
# Exit status: 0 on success; 1 on a setup failure.
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_FORGEJO_MIRROR_CHECKOUT:-$HOME/TauCetiForgejoMirror}"
DATA="$HOME/.local/share/tauceti-forgejo-mirror"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(tauceti-forgejo-mirror.service tauceti-forgejo-mirror.timer)
MIN_FREE_GIB=10
START=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

log() { echo "forgejo-mirror install.sh: $*" >&2; }
die() { log "$*"; exit 1; }

for unit in "${UNITS[@]}"; do
  for dropin in "$UNIT_DIR/$unit.d"/*.conf; do
    if [ -e "$dropin" ]; then
      die "systemd drop-in $dropin overrides the installed unit; remove it, then rerun"
    fi
  done
done

mkdir -p "$DATA/bin" "$UNIT_DIR"
free_kib="$(df -Pk "$DATA" | awk 'NR == 2 { print $4 }')"
[ "$free_kib" -ge $((MIN_FREE_GIB * 1024 * 1024)) ] \
  || die "only $((free_kib / 1024 / 1024)) GiB free under $DATA; the mirror needs about 2 GiB plus growth (want $MIN_FREE_GIB)"

if [ ! -d "$CHECKOUT/.git" ] && [ ! -f "$CHECKOUT/HEAD" ]; then
  log "cloning $REPO_URL into $CHECKOUT"
  git clone --quiet --no-checkout --filter=blob:none "$REPO_URL" "$CHECKOUT"
fi
git -C "$CHECKOUT" fetch --quiet origin main
revision="$(git -C "$CHECKOUT" rev-parse --verify 'refs/remotes/origin/main^{commit}')"

install_from_main() { # path-in-repo destination mode
  local tmp
  tmp="$(mktemp "$2.XXXXXX")"
  git -C "$CHECKOUT" show "$revision:$1" >"$tmp"
  chmod "$3" "$tmp"
  mv -f "$tmp" "$2"
}
install_from_main services/forgejo-mirror/forgejo_mirror.py "$DATA/bin/forgejo_mirror.py" 0755
install_from_main services/forgejo-mirror/forgejo_restore.py "$DATA/bin/forgejo_restore.py" 0755
for unit in "${UNITS[@]}"; do
  install_from_main "services/forgejo-mirror/$unit" "$UNIT_DIR/$unit" 0644
done
echo "$revision" >"$DATA/REVISION"
log "installed TauCetiWorker $revision"

if command -v systemd-analyze >/dev/null; then
  systemd-analyze --user verify "${UNITS[@]/#/$UNIT_DIR/}"
fi
systemctl --user daemon-reload
if [ "$START" = 1 ]; then
  systemctl --user enable --now tauceti-forgejo-mirror.timer
  # A run already in progress keeps the code it started with; the next run uses the new copy.
  systemctl --user start --no-block tauceti-forgejo-mirror.service
  systemctl --user list-timers --no-pager tauceti-forgejo-mirror.timer
fi
