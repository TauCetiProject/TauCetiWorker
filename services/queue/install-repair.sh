#!/usr/bin/env bash
# Install or update the Tau Ceti AI merge queue repair worker on this host
# (docs/queue/operations.md, "Repair worker").
#
# Idempotent and live: it fast-forwards the checkout at $TAUCETI_QUEUE_CHECKOUT (default
# ~/TauCetiQueue) to the requested revision, syncs the uv environment, writes the worker's
# environment file from the example when absent, runs the worker's host checks (`--check`: tools,
# token, repository admin, service, clone, Claude credentials, disk), clones the content repository
# when absent, installs the systemd user unit, reloads, restarts the worker and then verifies that
# the unit is active and that the restarted process reports itself serving in the task root's
# worker.json. Nothing else on the host is touched: the queue service, its backup timer and the
# fleet units keep running. A restart interrupts a running agent; the worker keeps the task's claim
# and checkpoint and resumes the same session when it comes back (SIGTERM handling in
# tauceti_queue/repair_task.py).
#
# Usage: services/queue/install-repair.sh [--revision REF] [--checkout DIR] [--instance NAME] [--no-restart] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). The checkout's
#                    branch is fast-forwarded to it; a revision that is not a descendant fails
#                    rather than being forced.
#   --checkout DIR   where the checkout lives (default: ~/TauCetiQueue).
#   --instance NAME  another worker, using repair-environment-NAME and its own task root.
#   --no-restart     install files and the unit but leave a running worker alone (the next
#                    restart picks up the new revision).
#   --wait SECONDS   how long to wait for the restarted worker to report serving (default: 60).
#
# Exit status: 0 when the worker is active and serving; 1 on a setup or host-check failure;
# 2 when the unit is not active or does not report serving within --wait.
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RESTART=1
WAIT=60
INSTANCE=""
UNIT="tauceti-queue-repair.service"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"
ENV_FILE="$CONFIG_DIR/repair-environment"

while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "install-repair.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

log() { echo "install-repair.sh: $*" >&2; }
die() { log "$*"; exit 1; }

if [ -n "$INSTANCE" ]; then
  [[ "$INSTANCE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die "invalid instance name: $INSTANCE"
  UNIT="tauceti-queue-repair@$INSTANCE.service"
  ENV_FILE="$CONFIG_DIR/repair-environment-$INSTANCE"
fi

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

# --- configuration --------------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR" && chmod 0700 "$CONFIG_DIR"
if [ ! -f "$ENV_FILE" ]; then
  if [ -n "$INSTANCE" ] && [ -f "$CONFIG_DIR/repair-environment" ]; then
    # Preserve provider configuration and credential locations without printing their values.
    cp "$CONFIG_DIR/repair-environment" "$ENV_FILE.tmp"
  else
    sed "s|/home/operator|$HOME|g; s|repair@tauchonki|repair@$(hostname -s)|" services/queue/repair-environment.example > "$ENV_FILE.tmp"
  fi
  if [ -n "$INSTANCE" ]; then
    # Shared clone/cache configuration is safe; identities and checkpoints must be separate.
    printf '\nTAUCETI_QUEUE_WORKER=repair-%s@%s\nTAUCETI_QUEUE_TASK_ROOT=%s/TauCetiQueue-data/repair-%s\n' \
      "$INSTANCE" "$(hostname -s)" "$HOME" "$INSTANCE" >> "$ENV_FILE.tmp"
  fi
  chmod 0600 "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  log "wrote $ENV_FILE from the example; review it (service URL, worker name, clone, task root, credentials)"
fi
chmod 0600 "$ENV_FILE"

# The unit's environment, for the checks and the clone below: the file's assignments, then the
# unit's PATH additions so the same `lake`, `claude` and `gh` are found.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export PATH="$HOME/.local/bin:$HOME/.elan/bin:$HOME/.nix-profile/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -n "${TAUCETI_QUEUE_SERVICE:-}" ] || die "TAUCETI_QUEUE_SERVICE is not set in $ENV_FILE"
[ -n "${TAUCETI_QUEUE_CLONE:-}" ] || die "TAUCETI_QUEUE_CLONE is not set in $ENV_FILE"

# --- content clone --------------------------------------------------------------------------------
# The worker fetches the task's retained refs into this clone and checks its worktrees out from it.
repository="${TAUCETI_QUEUE_REPOSITORY:-TauCetiProject/TauCeti}"
if [ ! -e "$TAUCETI_QUEUE_CLONE/.git" ]; then
  log "cloning https://github.com/$repository.git into $TAUCETI_QUEUE_CLONE"
  mkdir -p "$(dirname "$TAUCETI_QUEUE_CLONE")"
  git clone --quiet "https://github.com/$repository.git" "$TAUCETI_QUEUE_CLONE"
else
  log "content clone present at $TAUCETI_QUEUE_CLONE"
fi

# --- host checks ----------------------------------------------------------------------------------
log "running the worker's host checks"
if ! UV_NO_SYNC=1 uv run python -m tauceti_queue.repair_task --check; then
  die "host checks failed; fix the FAIL lines above (docs/queue/operations.md, \"Repair worker\") and rerun"
fi

# --- unit -----------------------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for unit_file in tauceti-queue-repair.service tauceti-queue-repair@.service; do
  install -m 0644 "services/queue/$unit_file" "$UNIT_DIR/$unit_file"
done
systemctl --user daemon-reload
systemctl --user enable --quiet "$UNIT"

if [ "$RESTART" = 1 ]; then
  log "restarting $UNIT"
  systemctl --user restart "$UNIT"
else
  systemctl --user start "$UNIT"
  log "not restarting; a running worker keeps its previous revision until restarted"
fi

# --- verify ---------------------------------------------------------------------------------------
# Active, and the process systemd started is the one writing worker.json in the task root.
root="${TAUCETI_QUEUE_TASK_ROOT:-$HOME/.local/state/tauceti/queue-repair}"
pid=""
heartbeat="$root/worker.json"
log "waiting up to ${WAIT}s for $UNIT to report serving ($heartbeat)"
deadline=$(( $(date +%s) + WAIT ))
while :; do
  if ! systemctl --user is-active --quiet "$UNIT"; then
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "$UNIT is not active; see: journalctl --user -u ${UNIT%.service} -n 50"
      exit 2
    fi
  else
    pid="$(systemctl --user show -p MainPID --value "$UNIT")"
    if [ -f "$heartbeat" ]; then
      state="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("pid"), d.get("state"), d.get("worker"), d.get("service"))' "$heartbeat" 2>/dev/null || true)"
      case "$state" in
        "$pid "*)
          log "ok: $UNIT (pid $pid) is serving at revision ${target:0:12}: ${state#"$pid" }"
          exit 0 ;;
      esac
    fi
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "$UNIT is active but pid $pid has not reported serving in $heartbeat; see: journalctl --user -u ${UNIT%.service} -n 50"
    exit 2
  fi
  sleep 2
done
