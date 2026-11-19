#!/usr/bin/env bash
# Install lifecycle cleanup on a host with the authoritative database and repair worktrees.
# Usage: install-repair-cleanup.sh [--revision REF | --release DIR] [--checkout DIR]
#                                  --database PATH --clone PATH --root PATH [--root PATH ...] [--cache-root PATH]
#
# Like the other queue installers, it runs from its own release directory,
# ~/TauCetiQueue-releases/<sha>-<time>/, made from the shared clone (default ~/TauCetiQueue) at
# --revision (default origin/main), and points only the cleanup units at it (services/queue/release.sh).
# Any revision other than origin/main HEAD requires explicit permission from a human operator.
set -euo pipefail

CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RELEASE_ARG=""
ARGS=("$@")
CONFIG_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --release) RELEASE_ARG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    *) CONFIG_ARGS+=("$1"); shift ;;
  esac
done

RELEASE_SCRIPT="install-repair-cleanup.sh"
# shellcheck source=services/queue/release.sh
. "$(dirname "$0")/release.sh"
log() { echo "install-repair-cleanup.sh: $*" >&2; }
die() { log "$*"; exit 1; }
override_warning() {
  log "WARNING: $1. Running repair cleanup at anything other than origin/main HEAD requires explicit"
  log "WARNING: permission from a human operator, and must be undone as soon as the change lands on main."
}
for dropin in "$HOME"/.config/systemd/user/tauceti-queue-repair-cleanup.{service,timer}.d/*.conf; do
  if [ -e "$dropin" ]; then
    die "systemd drop-in $dropin overrides the installed unit; queue units must not use drop-ins. Move it out of $(dirname "$dropin"), then rerun"
  fi
done
if [ -z "${TAUCETI_QUEUE_RELEASE:-}" ]; then
  if [ -n "$RELEASE_ARG" ] && [ "$REVISION" != "origin/main" ]; then die "--release and --revision are exclusive"; fi
  if [ "$REVISION" != "origin/main" ]; then override_warning "installing --revision $REVISION, not origin/main"; fi
  if [ -n "$RELEASE_ARG" ]; then override_warning "pointing cleanup at the existing release $RELEASE_ARG"; fi
  if [ "$CHECKOUT" != "$HOME/TauCetiQueue" ]; then override_warning "installing from $CHECKOUT, not ~/TauCetiQueue"; fi
  RELEASE_UNIT="tauceti-queue-repair-cleanup.service" release_handoff "$CHECKOUT" "$REVISION" "$RELEASE_ARG" "$0" "${ARGS[@]}"
fi
release_enter "$0"
release_build "$RELEASE" "$target"

python3 - "${CONFIG_ARGS[@]}" <<'PY'
import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path, required=True)
parser.add_argument('--clone', type=Path, required=True)
parser.add_argument('--root', type=Path, action='append', required=True)
parser.add_argument('--cache-root', type=Path, help='one shared repair cache root; migrate idle private pools')
args = parser.parse_args()
if not args.database.is_file() or not (args.clone / '.git').is_dir():
    parser.error('database and repair clone must already exist on this host')
config = Path.home() / '.config/tauceti-queue/repair-cleanup.json'
config.parent.mkdir(parents=True, exist_ok=True)
with open(config, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
    data = {'database': str(args.database.resolve()), 'clone': str(args.clone.resolve()),
            'roots': [str(root.resolve()) for root in args.root]}
    if args.cache_root:
        data['cache_root'] = str(args.cache_root.resolve())
    json.dump(data, output, indent=2)
    output.write('\n')
config.chmod(0o600)
PY
release_render_unit tauceti-queue-repair-cleanup.service
release_render_unit tauceti-queue-repair-cleanup.timer
systemctl --user daemon-reload
systemctl --user enable --now tauceti-queue-repair-cleanup.timer
systemctl --user start tauceti-queue-repair-cleanup.service
release_gc
