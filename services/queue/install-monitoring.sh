#!/usr/bin/env bash
# Install or update the Tau Ceti queue's public monitoring on this host (docs/queue/monitoring.md).
#
# Idempotent and live: it fast-forwards the checkout at $TAUCETI_QUEUE_CHECKOUT (default
# ~/TauCetiQueue) to the requested revision, syncs the uv environment, writes
# ~/.config/tauceti-queue/monitoring-environment from the example when absent, installs the
# monitoring units (the live publisher, the live repair-phase observer, and the hourly history report
# with its timer), reloads, restarts the two long-running units and checks that they stay active.
# The controller and the repair workers are not touched; none of these units can change queue state.
#
# Usage: services/queue/install-monitoring.sh [--revision REF] [--checkout DIR] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). The checkout's
#                    branch is fast-forwarded to it; a revision that is not a descendant fails
#                    rather than being forced.
#   --checkout DIR   where the checkout lives (default: ~/TauCetiQueue).
#   --no-restart     install files and units but leave running units alone.
#   --wait SECONDS   how long the restarted units must stay active (default: 20).
#
# WARNING: monitoring runs origin/main HEAD from ~/TauCetiQueue. Never use a systemd drop-in
# (~/.config/systemd/user/<unit>.d/*.conf) for these units: this script refuses to run while one
# exists. Running any other revision (--revision, --checkout or TAUCETI_QUEUE_CHECKOUT) requires
# explicit permission from a human operator, and whoever does it must rerun this script with no
# arguments as soon as their change lands on main.
#
# Exit status: 0 when the long-running units are active; 1 on a setup failure; 2 when a unit is
# not active after the restart.
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RESTART=1
WAIT=20
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"
LONG_RUNNING=(tauceti-queue-monitoring.service tauceti-queue-task-activity.service)
UNITS=("${LONG_RUNNING[@]}" tauceti-queue-monitoring-history.service tauceti-queue-monitoring-history.timer)

while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "install-monitoring.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

log() { echo "install-monitoring.sh: $*" >&2; }
die() { log "$*"; exit 1; }
override_warning() {
  log "WARNING: $1. Running monitoring at anything other than origin/main HEAD requires explicit"
  log "WARNING: permission from a human operator, and must be undone (rerun install-monitoring.sh with"
  log "WARNING: no arguments) as soon as the change lands on main."
}
# Drop-ins silently override the installed unit (and each other), so the running code stops being
# the checkout's. Refuse before touching anything.
for unit in "${UNITS[@]}"; do
  for dropin in "$UNIT_DIR/$unit.d"/*.conf; do
    if [ -e "$dropin" ]; then
      die "systemd drop-in $dropin overrides the installed unit; queue units must not use drop-ins. Move it out of $UNIT_DIR/$unit.d, then rerun"
    fi
  done
done
if [ "$REVISION" != "origin/main" ]; then override_warning "installing --revision $REVISION, not origin/main"; fi
if [ "$CHECKOUT" != "$HOME/TauCetiQueue" ]; then override_warning "installing from $CHECKOUT, not ~/TauCetiQueue"; fi

command -v git >/dev/null || die "git is required"
command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
command -v systemctl >/dev/null || die "systemd user services are required"

# --- checkout or fast-forward --------------------------------------------------------------------
if [ ! -d "$CHECKOUT/.git" ]; then
  log "cloning $REPO_URL into $CHECKOUT"
  git clone --quiet "$REPO_URL" "$CHECKOUT"
fi
cd "$CHECKOUT"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  die "$CHECKOUT has local modifications; commit or discard them before installing"
fi
log "fetching origin"
git fetch --quiet --prune origin
target="$(git rev-parse --verify --quiet "${REVISION}^{commit}")" || die "unknown revision $REVISION"
current="$(git rev-parse HEAD)"
if [ "$current" != "$target" ]; then
  if git merge-base --is-ancestor "$current" "$target"; then
    log "fast-forwarding $(git rev-parse --abbrev-ref HEAD) from ${current:0:12} to ${target:0:12}"
    git merge --quiet --ff-only "$target"
  else
    die "HEAD ${current:0:12} is not an ancestor of $REVISION (${target:0:12}); refusing a non-fast-forward update"
  fi
else
  log "checkout already at ${target:0:12}"
fi

log "syncing the uv environment"
uv sync --quiet

# --- configuration --------------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR" && chmod 0700 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/monitoring-environment" ]; then
  install -m 0600 services/queue/monitoring-environment.example "$CONFIG_DIR/monitoring-environment"
  log "wrote $CONFIG_DIR/monitoring-environment from the example; review it"
fi
for path in "$HOME/TauCetiQueue-data/monitoring/token" "$HOME/TauCetiQueue-data/queue.sqlite3"; do
  [ -e "$path" ] || die "$path is missing (docs/queue/monitoring.md)"
done

# --- units ----------------------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install -m 0644 "services/queue/$unit" "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --quiet "${LONG_RUNNING[@]}" tauceti-queue-monitoring-history.timer
systemctl --user start tauceti-queue-monitoring-history.timer

if [ "$RESTART" = 1 ]; then
  log "restarting ${LONG_RUNNING[*]}"
  systemctl --user restart "${LONG_RUNNING[@]}"
else
  systemctl --user start "${LONG_RUNNING[@]}"
  log "not restarting; running units keep their previous revision until restarted"
fi

# --- verify ---------------------------------------------------------------------------------------
sleep "$WAIT"
status=0
for unit in "${LONG_RUNNING[@]}"; do
  if systemctl --user is-active --quiet "$unit"; then
    log "ok: $unit is active at revision ${target:0:12}"
  else
    log "$unit is not active; see: journalctl --user -u $unit -n 50"
    status=2
  fi
done
exit "$status"
