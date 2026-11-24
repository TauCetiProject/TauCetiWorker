#!/usr/bin/env bash
# Install or update the Tau Ceti security-audit pool on this host (README.md, "security audit").
#
# Idempotent and live: it materialises the requested revision as its own release directory,
# ~/TauCetiQueue-releases/<sha>-<time>/ (a worktree of the shared clone at $TAUCETI_QUEUE_CHECKOUT,
# default ~/TauCetiQueue, whose own working tree is left alone), hands off to that release's copy
# of this script, builds the release's uv environment, installs tauceti-queue-security-audit.service
# pointing at the release, reloads, restarts it and checks that it stays active and runs from the
# release. The pool restores its ranges from its state after a restart.
# No other unit is touched. Unused release directories are then removed (services/queue/release.sh).
#
# Usage: services/queue/install-security-audit.sh [--revision REF | --release DIR] [--checkout DIR] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). A revision that does
#                    not descend from the one the unit runs now fails rather than being forced.
#   --release DIR    point the unit at an existing release directory instead (rollback).
#   --checkout DIR   the shared clone releases are made from (default: ~/TauCetiQueue).
#   --no-restart     install files and the unit but leave the running pool alone.
#   --wait SECONDS   how long the restarted unit must stay active (default: 20).
#
# WARNING: the pool runs origin/main HEAD from its own ~/TauCetiQueue-releases directory. Never use
# a systemd drop-in (~/.config/systemd/user/<unit>.d/*.conf) for it: this script refuses to run
# while one exists. Running any other revision (--revision, --release, --checkout or
# TAUCETI_QUEUE_CHECKOUT) requires explicit permission from a human operator, and whoever does it
# must rerun this script with no arguments as soon as their change lands on main.
#
# Exit status: 0 when the unit is active; 1 on a setup failure; 2 when it is not active after the
# restart.
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RELEASE_ARG=""
RESTART=1
WAIT=20
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"
LONG_RUNNING=(tauceti-queue-security-audit.service)
UNITS=("${LONG_RUNNING[@]}")

ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --release) RELEASE_ARG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "install-security-audit.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

RELEASE_SCRIPT="install-security-audit.sh"
# shellcheck source=services/queue/release.sh
. "$(dirname "$0")/release.sh"
log() { echo "install-security-audit.sh: $*" >&2; }
die() { log "$*"; exit 1; }
override_warning() {
  log "WARNING: $1. Running the audit pool at anything other than origin/main HEAD requires explicit"
  log "WARNING: permission from a human operator, and must be undone (rerun install-security-audit.sh with"
  log "WARNING: no arguments) as soon as the change lands on main."
}
# Drop-ins silently override the installed unit, so the running code stops being the release's. Refuse before touching anything.
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
  RELEASE_UNIT="tauceti-queue-security-audit.service" release_handoff "$CHECKOUT" "$REVISION" "$RELEASE_ARG" "$0" "${ARGS[@]}"
fi
release_enter "$0"
release_build "$RELEASE" "$target"

# --- configuration --------------------------------------------------------------------------------
for name in github-app.json github-private-key.pem; do
  [ -f "$HOME/.config/tauceti-security-audit/$name" ] || die "$HOME/.config/tauceti-security-audit/$name is missing (the security-audit GitHub App)"
done
[ -f "$CONFIG_DIR/security-audit-anthropic-api-key" ] || die "$CONFIG_DIR/security-audit-anthropic-api-key is missing (mode 0600, the API key for Haiku reviews)"
[ -f "$CONFIG_DIR/security-audit-zulip.json" ] || die "$CONFIG_DIR/security-audit-zulip.json is missing (README.md, private Zulip alerts)"
mkdir -p "$HOME/TauCetiQueue-data/security-audit-pool/state/pool/security-audit"

# --- unit -----------------------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  release_render_unit "$unit"
done
systemctl --user daemon-reload
systemctl --user enable --quiet "${LONG_RUNNING[@]}"

if [ "$RESTART" = 1 ]; then
  log "restarting ${LONG_RUNNING[*]}"
  systemctl --user restart "${LONG_RUNNING[@]}"
else
  systemctl --user start "${LONG_RUNNING[@]}"
  log "not restarting; the running pool keeps its previous revision until restarted"
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
