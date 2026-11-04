#!/usr/bin/env bash
# Install or update the Tau Ceti AI merge queue service on this host (docs/queue/operations.md).
#
# Idempotent and live: it fast-forwards the checkout at $TAUCETI_QUEUE_CHECKOUT (default
# ~/TauCetiQueue) to the requested revision, syncs the uv environment, installs the systemd user
# units, reloads, restarts the service and then verifies through /status that the running
# revision is the checkout's. Nothing is drained; the restart is the only interruption, and the
# service finishes its current reconciliation step before it exits (SIGTERM handling in
# tauceti_queue/__main__.py).
#
# Usage: services/queue/install.sh [--revision REF] [--checkout DIR] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). The checkout's
#                    branch is fast-forwarded to it; a revision that is not a descendant fails
#                    rather than being forced.
#   --checkout DIR   where the checkout lives (default: ~/TauCetiQueue).
#   --no-restart     install files and units but leave the running service alone (the next
#                    restart picks up the new revision; verification then reports the old one).
#   --wait SECONDS   how long to wait for /status after the restart (default: 90).
#
# Exit status: 0 when the service answers with the installed revision; 1 on a setup failure;
# 2 when the service answers with a different revision (or not at all).
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RESTART=1
WAIT=90
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"

while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

log() { echo "install.sh: $*" >&2; }
die() { log "$*"; exit 1; }

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

# --- environment ----------------------------------------------------------------------------------
log "syncing the uv environment"
uv sync --quiet
# Bake the revision so /status reports the installed checkout even if git becomes unavailable
# to the service's environment; tauceti_queue/runtime.py prefers this file over `git rev-parse`.
printf 'SHA = "%s"\nDIRTY = False\n' "$target" > tauceti_queue/_revision.py.tmp
mv tauceti_queue/_revision.py.tmp tauceti_queue/_revision.py

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

# --- units ----------------------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for unit in tauceti-queue.service tauceti-queue-backup.service tauceti-queue-backup.timer; do
  install -m 0644 "services/queue/$unit" "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --quiet tauceti-queue.service tauceti-queue-backup.timer
systemctl --user start tauceti-queue-backup.timer

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
  log "ok: tauceti-queue is running revision ${target:0:12}"
  exit 0
fi
log "the running service does not report revision ${target:0:12}; see: journalctl --user -u tauceti-queue -n 50"
exit 2
