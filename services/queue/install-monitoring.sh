#!/usr/bin/env bash
# Install or update the Tau Ceti queue's public monitoring on this host (docs/queue/monitoring.md).
#
# Idempotent and live: it materialises the requested revision as its own release directory,
# ~/TauCetiQueue-releases/<sha>-<time>/ (a worktree of the shared clone at $TAUCETI_QUEUE_CHECKOUT,
# default ~/TauCetiQueue, whose own working tree is left alone), hands off to that release's copy
# of this script, builds the release's uv environment, writes
# ~/.config/tauceti-queue/monitoring-environment from the example when absent, installs the
# monitoring units (the live publisher, the live repair-phase observer, the hourly history report
# with its timer, and the Forgejo contributor-fleets team sync with its timer) pointing at the
# release, reloads, restarts the two long-running units and checks that they stay active and run
# from the release. The controller and the repair workers are not
# touched, and keep running from their own release directories; none of these units can change
# queue state. Unused release directories are then removed (services/queue/release.sh).
#
# Usage: services/queue/install-monitoring.sh [--revision REF | --release DIR] [--checkout DIR] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). A revision that does
#                    not descend from the one the units run now fails rather than being forced.
#   --release DIR    point the units at an existing release directory instead (rollback).
#   --checkout DIR   the shared clone releases are made from (default: ~/TauCetiQueue).
#   --no-restart     install files and units but leave running units alone.
#   --wait SECONDS   how long the restarted units must stay active (default: 20).
#
# WARNING: monitoring runs origin/main HEAD, each install in its own ~/TauCetiQueue-releases
# directory. Never use a systemd drop-in (~/.config/systemd/user/<unit>.d/*.conf) for these units:
# this script refuses to run while one exists. Running any other revision (--revision, --release,
# --checkout or TAUCETI_QUEUE_CHECKOUT) requires explicit permission from a human operator, and
# whoever does it must rerun this script with no arguments as soon as their change lands on main.
#
# Exit status: 0 when the long-running units are active; 1 on a setup failure; 2 when a unit is
# not active after the restart.
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RELEASE_ARG=""
RESTART=1
WAIT=20
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"
LONG_RUNNING=(tauceti-queue-monitoring.service tauceti-queue-task-activity.service)
UNITS=("${LONG_RUNNING[@]}" tauceti-queue-monitoring-history.service tauceti-queue-monitoring-history.timer
       tauceti-queue-forgejo-team-sync.service tauceti-queue-forgejo-team-sync.timer)

ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --release) RELEASE_ARG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "install-monitoring.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

RELEASE_SCRIPT="install-monitoring.sh"
# shellcheck source=services/queue/release.sh
. "$(dirname "$0")/release.sh"
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

# --- release --------------------------------------------------------------------------------------
# The first pass (whichever copy of this script was started) creates or picks the release and runs
# the release's own copy, which does the rest.
if [ -z "${TAUCETI_QUEUE_RELEASE:-}" ]; then
  if [ -n "$RELEASE_ARG" ] && [ "$REVISION" != "origin/main" ]; then die "--release and --revision are exclusive"; fi
  if [ "$REVISION" != "origin/main" ]; then override_warning "installing --revision $REVISION, not origin/main"; fi
  if [ -n "$RELEASE_ARG" ]; then override_warning "pointing the units at the existing release $RELEASE_ARG"; fi
  if [ "$CHECKOUT" != "$HOME/TauCetiQueue" ]; then override_warning "installing from $CHECKOUT, not ~/TauCetiQueue"; fi
  command -v git >/dev/null || die "git is required"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
  command -v systemctl >/dev/null || die "systemd user services are required"
  RELEASE_UNIT="tauceti-queue-monitoring.service" release_handoff "$CHECKOUT" "$REVISION" "$RELEASE_ARG" "$0" "${ARGS[@]}"
fi
release_enter "$0"
release_build "$RELEASE" "$target"

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
  release_render_unit "$unit"
done
systemctl --user daemon-reload
systemctl --user enable --quiet "${LONG_RUNNING[@]}" tauceti-queue-monitoring-history.timer tauceti-queue-forgejo-team-sync.timer
systemctl --user start tauceti-queue-monitoring-history.timer tauceti-queue-forgejo-team-sync.timer

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
  if ! systemctl --user is-active --quiet "$unit"; then
    log "$unit is not active; see: journalctl --user -u $unit -n 50"
    status=2
  elif release_verify_running "$unit"; then
    log "ok: $unit is active at revision ${target:0:12} from $RELEASE"
  elif [ "$RESTART" = 1 ]; then
    log "$unit is active but does not run from $RELEASE (it runs from $(release_unit_cwd "$unit"))"
    status=2
  else
    log "$unit keeps running from $(release_unit_cwd "$unit") until restarted"
  fi
done
if [ "$status" = 0 ]; then release_gc; fi
exit "$status"
