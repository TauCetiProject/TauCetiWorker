#!/usr/bin/env bash
# Git maintenance of the controller's content clone (docs/queue/operations.md, "Clone maintenance");
# started hourly by tauceti-queue-clone-maintenance.timer.
#
# The controller fetches, updates refs and builds chains in this clone all the time. Git's own
# automatic maintenance ran detached from those fetches inside the service, so a service restart
# could kill it mid-run and leave objects/maintenance.lock behind, after which Git silently skipped
# all maintenance: loose refs, loose objects and packs piled up until one fetch took 20 to 30 s.
# This runs only the tasks that are safe beside a live user of the repository (pack-refs,
# loose-objects, incremental-repack, commit-graph), never gc or a prune of unreachable objects the
# queue may still reference, and it turns automatic maintenance off in the clone. A lock older than
# --stale seconds, held by no running Git maintenance, is removed first.
#
# Usage: services/queue/clone-maintenance.sh [--clone DIR] [--stale SECONDS]
#   --clone DIR      defaults to the configured content clone (tauceti-queue check-config --print content_clone)
#   --stale SECONDS  age past which an unheld maintenance lock is stale (default 21600, six hours)
set -euo pipefail

CLONE=""
STALE=21600

while [ $# -gt 0 ]; do
  case "$1" in
    --clone) CLONE="$2"; shift 2 ;;
    --stale) STALE="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "clone-maintenance.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done
case "$STALE" in ''|*[!0-9]*) echo "clone-maintenance.sh: --stale takes a number of seconds" >&2; exit 1 ;; esac

if [ -z "$CLONE" ]; then
  if command -v tauceti-queue >/dev/null; then
    CLONE="$(tauceti-queue check-config --print content_clone)"
  else
    # The code this copy of the script belongs to: the controller's release when the timer runs it.
    CLONE="$(cd "$(dirname "$0")/../.." && UV_NO_SYNC=1 uv run tauceti-queue check-config --print content_clone)"
  fi
fi
if ! git -C "$CLONE" rev-parse --git-dir >/dev/null 2>&1; then
  echo "clone-maintenance.sh: no Git repository at $CLONE (nothing to maintain yet)" >&2
  exit 0
fi

git -C "$CLONE" config maintenance.auto false

# Fail closed: any Git maintenance or gc on this host may be this clone's (a linked worktree shares
# its objects without naming it), and a process that cannot be inspected counts as one.
maintenance_running() {
  local status=0
  command -v pgrep >/dev/null || return 0
  pgrep -f '(^|/)(git( [^ ]+)* (maintenance|gc)|git-(maintenance|gc))( |$)' >/dev/null || status=$?
  # pgrep: 0 found, 1 none found; anything else is an error, which counts as found.
  [ "$status" -ne 1 ]
}

# The common directory, which every linked worktree shares with the objects and their lock.
git_dir="$(git -C "$CLONE" rev-parse --path-format=absolute --git-common-dir)"
# One run of this script at a time, held until Git finishes: a second run cannot remove the
# lock of a run that has just started.
exec 9>"$git_dir/tauceti-clone-maintenance.lock"
if ! flock -n 9; then
  echo "clone-maintenance.sh: another clone maintenance is running; skipping" >&2
  exit 0
fi

lock="$(git -C "$CLONE" rev-parse --path-format=absolute --git-path objects/maintenance.lock)"
if [ -e "$lock" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$lock") ))
  if [ "$age" -lt "$STALE" ] || maintenance_running; then
    echo "clone-maintenance.sh: maintenance already running in $CLONE (lock ${age}s old); skipping" >&2
    exit 0
  fi
  echo "clone-maintenance.sh: removing a stale maintenance lock (${age}s old, no maintenance running)" >&2
  rm -f "$lock"
fi

exec git -C "$CLONE" maintenance run \
  --task=pack-refs --task=loose-objects --task=incremental-repack --task=commit-graph
