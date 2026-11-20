#!/usr/bin/env bash
# Install or update the Tau Ceti AI merge queue service on this host (docs/queue/operations.md).
#
# Idempotent and live: it materialises the requested revision as its own release directory,
# ~/TauCetiQueue-releases/<sha>-<time>/ (a worktree of the shared clone at $TAUCETI_QUEUE_CHECKOUT,
# default ~/TauCetiQueue, whose own working tree is left alone), hands off to that release's copy
# of this script, builds the release's uv environment and bakes its revision, installs the systemd
# user units pointing at the release, reloads, restarts the service and then verifies through
# /status that the running revision is the release's and that the process runs from it. Nothing is
# drained; the restart is the only interruption, and the service finishes its current
# reconciliation step before it exits (SIGTERM handling in tauceti_queue/__main__.py). The repair
# workers and monitoring keep running from their own release directories. Unused release
# directories are then removed (services/queue/release.sh).
#
# Usage: services/queue/install.sh [--revision REF | --release DIR] [--checkout DIR] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). A revision that does
#                    not descend from the one the service runs now fails rather than being forced.
#   --release DIR    point the units at an existing release directory instead (rollback).
#   --checkout DIR   the shared clone releases are made from (default: ~/TauCetiQueue).
#   --no-restart     install files and units but leave the running service alone (the next
#                    restart picks up the new release; verification then reports the old one).
#   --wait SECONDS   how long to wait for /status after the restart (default: 90).
#
# WARNING: the controller runs origin/main HEAD, each install in its own ~/TauCetiQueue-releases
# directory. Never use a systemd drop-in (~/.config/systemd/user/tauceti-queue.service.d/*.conf) for
# it: this script refuses to run while one exists. Running any other revision (--revision, --release,
# --checkout or TAUCETI_QUEUE_CHECKOUT) requires explicit permission from a human operator, and
# whoever does it must rerun this script with no arguments as soon as their change lands on main.
#
# Exit status: 0 when the service answers with the installed revision; 1 on a setup failure;
# 2 when the service answers with a different revision (or not at all).
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RELEASE_ARG=""
RESTART=1
WAIT=90
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"

ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --release) RELEASE_ARG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

RELEASE_SCRIPT="install.sh"
# shellcheck source=services/queue/release.sh
. "$(dirname "$0")/release.sh"
log() { echo "install.sh: $*" >&2; }
die() { log "$*"; exit 1; }
override_warning() {
  log "WARNING: $1. Running the controller at anything other than origin/main HEAD requires explicit"
  log "WARNING: permission from a human operator, and must be undone (rerun install.sh with no"
  log "WARNING: arguments) as soon as the change lands on main."
}
# Drop-ins silently override the installed unit (and each other), so the running code stops being
# the checkout's. Refuse before touching anything.
for dropin in "$UNIT_DIR"/tauceti-queue.service.d/*.conf "$UNIT_DIR"/tauceti-queue-backup.service.d/*.conf \
  "$UNIT_DIR"/tauceti-queue-clone-maintenance.service.d/*.conf; do
  if [ -e "$dropin" ]; then
    die "systemd drop-in $dropin overrides the installed unit; queue units must not use drop-ins. Move it out of $(dirname "$dropin"), then rerun"
  fi
done

# --- release --------------------------------------------------------------------------------------
# The first pass (whichever copy of this script was started) creates or picks the release and runs
# the release's own copy, which does the rest.
if [ -z "${TAUCETI_QUEUE_RELEASE:-}" ]; then
  if [ -n "$RELEASE_ARG" ] && [ "$REVISION" != "origin/main" ]; then die "--release and --revision are exclusive"; fi
  if [ "$REVISION" != "origin/main" ]; then override_warning "installing --revision $REVISION, not origin/main"; fi
  if [ -n "$RELEASE_ARG" ]; then override_warning "pointing the controller at the existing release $RELEASE_ARG"; fi
  if [ "$CHECKOUT" != "$HOME/TauCetiQueue" ]; then override_warning "installing from $CHECKOUT, not ~/TauCetiQueue"; fi
  command -v git >/dev/null || die "git is required"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
  command -v systemctl >/dev/null || die "systemd user services are required"
  RELEASE_UNIT="tauceti-queue.service" release_handoff "$CHECKOUT" "$REVISION" "$RELEASE_ARG" "$0" "${ARGS[@]}"
fi
release_enter "$0"
# TauCetiTools follows its main branch; a new release takes its current commit.
release_build "$RELEASE" "$target" --upgrade-package tauceti-tools

# --- configuration layout ------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR" && chmod 0700 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  install -m 0600 services/queue/config.toml.example "$CONFIG_DIR/config.toml"
  log "wrote $CONFIG_DIR/config.toml from the example; review it"
fi
if [ ! -f "$CONFIG_DIR/environment" ]; then
  install -m 0600 services/queue/environment.example "$CONFIG_DIR/environment"
fi
if ! uv run tauceti-queue check-config >/dev/null; then
  uv run tauceti-queue check-config || true
  die "configuration problems; fix $CONFIG_DIR and rerun"
fi

# --- content clone --------------------------------------------------------------------------------
# The controller runs chain construction and replay against a local clone of the opted-in
# repository; without one the service serves status only ("content clone missing" in /status).
# Create it on first install; the service fetches into it from then on.
content_clone="$(uv run tauceti-queue check-config --print content_clone)"
repository="$(uv run tauceti-queue check-config --print repository)"
if [ ! -e "$content_clone/HEAD" ] && [ ! -e "$content_clone/.git" ]; then
  log "cloning https://github.com/$repository.git into $content_clone"
  mkdir -p "$(dirname "$content_clone")"
  git clone --quiet "https://github.com/$repository.git" "$content_clone"
else
  log "content clone present at $content_clone"
fi
# Git's automatic maintenance would run detached inside the service, where a restart can kill it and
# strand its lock; tauceti-queue-clone-maintenance.timer maintains the clone instead.
if git -C "$content_clone" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$content_clone" config maintenance.auto false
fi

# --- units ----------------------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for unit in tauceti-queue.service tauceti-queue-backup.service tauceti-queue-backup.timer \
  tauceti-queue-clone-maintenance.service tauceti-queue-clone-maintenance.timer; do
  release_render_unit "$unit"
done
systemctl --user daemon-reload
systemctl --user enable --quiet tauceti-queue.service tauceti-queue-backup.timer tauceti-queue-clone-maintenance.timer
systemctl --user start tauceti-queue-backup.timer tauceti-queue-clone-maintenance.timer

if [ "$RESTART" = 1 ]; then
  log "restarting tauceti-queue.service"
  systemctl --user restart tauceti-queue.service
else
  systemctl --user start tauceti-queue.service
  log "not restarting; the running service keeps its previous revision until restarted"
fi

# --- verify ---------------------------------------------------------------------------------------
log "verifying the running revision through /status"
if uv run tauceti-queue status --wait "$WAIT" --expect-revision "$target"; then
  if release_verify_running tauceti-queue.service; then
    log "ok: tauceti-queue is running revision ${target:0:12} from $RELEASE"
    release_gc
    exit 0
  fi
  log "tauceti-queue reports ${target:0:12} but does not run from $RELEASE (it runs from $(release_unit_cwd tauceti-queue.service))"
  exit 2
fi
log "the running service does not report revision ${target:0:12}; see: journalctl --user -u tauceti-queue -n 50"
exit 2
