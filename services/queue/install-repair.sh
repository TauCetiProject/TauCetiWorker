#!/usr/bin/env bash
# Install or update the Tau Ceti AI merge queue repair worker on this host
# (docs/queue/operations.md, "Repair worker").
#
# Idempotent and live: it materialises the requested revision as its own release directory,
# ~/TauCetiQueue-releases/<sha>-<time>/ (a worktree of the shared clone at $TAUCETI_QUEUE_CHECKOUT,
# default ~/TauCetiQueue, whose own working tree is left alone), hands off to that release's copy
# of this script, builds the release's uv environment, writes the worker's environment file from
# the example when absent, runs the worker's host checks (`--check`: tools, token, repository admin,
# service, clone, Claude credentials, disk), clones the content repository when absent, installs
# the worker's own systemd user unit pointing at the release (an instance gets its own
# tauceti-queue-repair@NAME.service, which takes precedence over the template), reloads, restarts
# the worker and then verifies that the unit is active, runs from the release, and that the
# restarted process reports itself serving at the release's revision in the task root's
# worker.json. Nothing else on the host is touched: the queue service, the other repair workers,
# monitoring and the fleet units keep running from their own directories. A restart interrupts a
# running agent; the worker keeps the task's claim and checkpoint and resumes the same session when
# it comes back (SIGTERM handling in tauceti_queue/repair_task.py). Unused release directories are
# then removed (services/queue/release.sh).
#
# Usage: services/queue/install-repair.sh [--revision REF | --release DIR] [--checkout DIR] [--instance NAME]
#                                          [--no-restart | --when-idle [--idle-timeout SECONDS]] [--wait SECONDS]
#
#   --revision REF   commit, branch or tag to install (default: origin/main). A revision that does
#                    not descend from the one the worker runs now fails rather than being forced.
#   --release DIR    point the worker at an existing release directory instead (rollback).
#   --checkout DIR   the shared clone releases are made from (default: ~/TauCetiQueue).
#   --instance NAME  another worker, using repair-environment-NAME and its own task root.
#   --no-restart     install files and the unit but leave a running worker alone (the next
#                    restart picks up the new release).
#   --when-idle      restart only once the queue shows the running worker holding no claim: no row
#                    in the controller's claims table (~/TauCetiQueue-data/queue.sqlite3, read-only)
#                    for the TAUCETI_QUEUE_WORKER the unit's process runs as with released_at unset
#                    (an expired claim counts until the controller releases it, since a renewal
#                    waiting for its lock keeps it). It checks every 60s before installing
#                    anything, and again right before the restart.
#   --idle-timeout SECONDS  give up waiting after SECONDS (default: wait for as long as it takes).
#   --wait SECONDS   how long to wait for the restarted worker to report serving (default: 60).
#
# WARNING: repair workers run origin/main HEAD, each install in its own ~/TauCetiQueue-releases
# directory. Never use a systemd drop-in (~/.config/systemd/user/tauceti-queue-repair*.service.d/*.conf)
# for them: this script refuses to run while one exists for the unit it installs. Running any other
# revision (--revision, --release, --checkout or TAUCETI_QUEUE_CHECKOUT) requires explicit permission
# from a human operator, and whoever does it must rerun this script with no arguments as soon as
# their change lands on main.
#
# Exit status: 0 when the worker is active and serving; 1 on a setup or host-check failure;
# 2 when the unit is not active, does not run from the release, or does not report serving within
# --wait; 3 when --idle-timeout passed with the worker still holding a claim (it was not restarted).
set -euo pipefail

REPO_URL="https://github.com/TauCetiProject/TauCetiWorker.git"
CHECKOUT="${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}"
REVISION="origin/main"
RELEASE_ARG=""
RESTART=1
WAIT=60
INSTANCE=""
WHEN_IDLE=0
IDLE_TIMEOUT=""
IDLE_POLL=60
QUEUE_DB="$HOME/TauCetiQueue-data/queue.sqlite3"
UNIT="tauceti-queue-repair.service"
UNIT_SOURCE="tauceti-queue-repair.service"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/tauceti-queue"
ENV_FILE="$CONFIG_DIR/repair-environment"

ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --release) RELEASE_ARG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --wait) WAIT="$2"; shift 2 ;;
    --when-idle) WHEN_IDLE=1; shift ;;
    --idle-timeout) IDLE_TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,48p' "$0"; exit 0 ;;
    *) echo "install-repair.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

RELEASE_SCRIPT="install-repair.sh"
# shellcheck source=services/queue/release.sh
. "$(dirname "$0")/release.sh"
log() { echo "install-repair.sh: $*" >&2; }
die() { log "$*"; exit 1; }

if [ -n "$INSTANCE" ]; then
  [[ "$INSTANCE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die "invalid instance name: $INSTANCE"
  UNIT="tauceti-queue-repair@$INSTANCE.service"
  UNIT_SOURCE="tauceti-queue-repair@.service"
  ENV_FILE="$CONFIG_DIR/repair-environment-$INSTANCE"
fi

if [ -n "$IDLE_TIMEOUT" ]; then
  [[ "$IDLE_TIMEOUT" =~ ^(0|[1-9][0-9]*)$ ]] || die "--idle-timeout takes a number of seconds"
  [ "$WHEN_IDLE" = 1 ] || die "--idle-timeout needs --when-idle"
fi
if [ "$WHEN_IDLE" = 1 ]; then
  [ "$RESTART" = 1 ] || die "--when-idle and --no-restart are exclusive"
  [ -f "$QUEUE_DB" ] || die "--when-idle reads the queue's claims from $QUEUE_DB, which does not exist on this host"
fi

# --when-idle: the unreleased claims, in the queue, of the worker the unit runs now. Its identity is read
# from the running process's own environment, as the worker computes it (default_worker_identity
# in tauceti_queue/repair_task.py), so an edited environment file cannot misname it. Prints
# nothing when the unit has no running process; fails, and so counts as busy, when it cannot tell.
live_claims() {
  local pid
  # Called as a condition, where errexit is off: a failed lookup must not read as "no process".
  pid="$(systemctl --user show -p MainPID --value "$UNIT")" && [ -n "$pid" ] || return 1
  python3 - "$QUEUE_DB" "$pid" <<'PY'
import pathlib, socket, sqlite3, sys, time
if sys.argv[2] == "0":
    sys.exit(0)
env = dict(
    entry.split("=", 1)
    for entry in pathlib.Path(f"/proc/{sys.argv[2]}/environ").read_text().split("\0")
    if "=" in entry
)
worker = env.get("TAUCETI_QUEUE_WORKER") or f"{env.get('TAUCETI_WORKER_ID', 'default')}@{socket.gethostname()}"
db = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
now = time.time()
rows = db.execute(
    "SELECT id, task_id, operation, expires_at FROM claims WHERE worker = ? AND released_at IS NULL ORDER BY id",
    (worker,),
).fetchall()
if rows:
    print(f"{worker} holds " + "; ".join(f"claim {i} (task {t}, {op}, expires_at {e - now:+.0f}s)" for i, t, op, e in rows))
PY
}
# wait_until_idle: return once the queue shows no unreleased claim for the running worker. Never returns
# early on anything it cannot read; exits 3 after --idle-timeout.
wait_until_idle() {
  local claims deadline="" left
  [ -z "$IDLE_TIMEOUT" ] || deadline=$(( $(date +%s) + IDLE_TIMEOUT ))
  while :; do
    if claims="$(live_claims)"; then
      if [ -z "$claims" ]; then
        log "$UNIT holds no claim in $QUEUE_DB"
        return 0
      fi
      log "waiting before restarting $UNIT: $claims"
    else
      log "cannot read $UNIT's claims from $QUEUE_DB; treating it as busy"
    fi
    if [ -n "$deadline" ]; then
      left=$(( deadline - $(date +%s) ))
      if [ "$left" -le 0 ]; then
        log "$UNIT still holds a claim after ${IDLE_TIMEOUT}s; it was not restarted"
        exit 3
      fi
      sleep $(( left < IDLE_POLL ? left : IDLE_POLL ))
    else
      sleep "$IDLE_POLL"
    fi
  done
}

override_warning() {
  log "WARNING: $1. Running a repair worker at anything other than origin/main HEAD requires explicit"
  log "WARNING: permission from a human operator, and must be undone (rerun install-repair.sh with no"
  log "WARNING: arguments) as soon as the change lands on main."
}
# Drop-ins silently override the installed unit (and each other), so the running code stops being
# the checkout's. A template drop-in (tauceti-queue-repair@.service.d) applies to every instance.
dropin_dirs=("$UNIT_DIR/$UNIT.d" "$UNIT_DIR/tauceti-repair.slice.d")
if [ -n "$INSTANCE" ]; then dropin_dirs+=("$UNIT_DIR/tauceti-queue-repair@.service.d"); fi
for dir in "${dropin_dirs[@]}"; do
  for dropin in "$dir"/*.conf; do
    if [ -e "$dropin" ]; then
      die "systemd drop-in $dropin overrides the installed unit; queue units must not use drop-ins. Move it out of $dir, then rerun"
    fi
  done
done

# --- release --------------------------------------------------------------------------------------
# The first pass (whichever copy of this script was started) creates or picks the release and runs
# the release's own copy, which does the rest.
if [ -z "${TAUCETI_QUEUE_RELEASE:-}" ]; then
  if [ -n "$RELEASE_ARG" ] && [ "$REVISION" != "origin/main" ]; then die "--release and --revision are exclusive"; fi
  if [ "$REVISION" != "origin/main" ]; then override_warning "installing --revision $REVISION, not origin/main"; fi
  if [ -n "$RELEASE_ARG" ]; then override_warning "pointing $UNIT at the existing release $RELEASE_ARG"; fi
  if [ "$CHECKOUT" != "$HOME/TauCetiQueue" ]; then override_warning "installing from $CHECKOUT, not ~/TauCetiQueue"; fi
  command -v git >/dev/null || die "git is required"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
  command -v systemctl >/dev/null || die "systemd user services are required"
  # Wait before choosing the revision, so a long wait installs origin/main as it is when it ends.
  if [ "$WHEN_IDLE" = 1 ]; then wait_until_idle; fi
  RELEASE_UNIT="$UNIT" release_handoff "$CHECKOUT" "$REVISION" "$RELEASE_ARG" "$0" "${ARGS[@]}"
fi
release_enter "$0"
# TauCetiTools follows its main branch; a new release takes its current commit.
release_build "$RELEASE" "$target" --upgrade-package tauceti-tools

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
if ! uv run python -m tauceti_queue.repair_task --check; then
  die "host checks failed; fix the FAIL lines above (docs/queue/operations.md, \"Repair worker\") and rerun"
fi

# --- unit -----------------------------------------------------------------------------------------
# Only this worker's unit: an instance gets its own file rendered from the template, so installing
# one worker never changes what another (re)starts from.
release_render_unit "$UNIT_SOURCE" "$UNIT"
# The slice every repair worker runs in (its weights put repairs below the CI and staging VMs). It
# names no release, so it is installed as it is and never keeps a release from being collected.
install -m 0644 services/queue/tauceti-repair.slice "$UNIT_DIR/tauceti-repair.slice"
systemctl --user daemon-reload
fragment="$(systemctl --user show -p FragmentPath --value "$UNIT")"
[ "$fragment" = "$UNIT_DIR/$UNIT" ] || die "systemd loads $UNIT from ${fragment:-nothing}, not $UNIT_DIR/$UNIT"
# reenable: an instance enabled through the template has a wants link to the template file.
systemctl --user reenable --quiet "$UNIT"

if [ "$RESTART" = 1 ]; then
  # The wait above ended before the build and the host checks; check again with nothing left
  # between the check and the restart. (The worker has no switch to stop claiming meanwhile.) The
  # unit file names the release now, so the release lock is not needed to keep it from collection,
  # and is given up so that a long wait does not hold up other installs' collection.
  if [ "$WHEN_IDLE" = 1 ]; then
    flock -u 9 2>/dev/null || true
    wait_until_idle
  fi
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
    if [ "$RESTART" = 1 ] && ! release_verify_running "$UNIT"; then
      if [ "$(date +%s)" -ge "$deadline" ]; then
        log "$UNIT (pid $pid) does not run from $RELEASE (it runs from $(release_unit_cwd "$UNIT")); see: journalctl --user -u ${UNIT%.service} -n 50"
        exit 2
      fi
    elif [ -f "$heartbeat" ]; then
      # The revision the process read at startup; heartbeats from before releases have none.
      state="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("pid"), d.get("revision") or "-", d.get("state"), d.get("worker"), d.get("service"))' "$heartbeat" 2>/dev/null || true)"
      case "$state" in
        "$pid $target "*)
          # The restarted worker runs in its slice, below the VMs (tauceti-repair.slice).
          cgroup="$(systemctl --user show -p ControlGroup --value "$UNIT")"
          case "$cgroup" in
            */tauceti-repair.slice/*) ;;
            *) log "$UNIT (pid $pid) runs in ${cgroup:-no cgroup}, not tauceti-repair.slice; see: systemctl --user status $UNIT"
               exit 2 ;;
          esac
          log "ok: $UNIT (pid $pid) is serving at revision ${target:0:12} from $RELEASE: ${state#"$pid $target" }"
          release_gc
          exit 0 ;;
        "$pid "*)
          if [ "$RESTART" = 0 ]; then
            log "ok: $UNIT (pid $pid) keeps serving its previous revision until restarted: ${state#"$pid" }"
            release_gc
            exit 0
          fi ;;
      esac
    fi
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "$UNIT is active but pid $pid has not reported serving in $heartbeat; see: journalctl --user -u ${UNIT%.service} -n 50"
    exit 2
  fi
  sleep 2
done
