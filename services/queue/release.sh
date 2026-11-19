#!/usr/bin/env bash
# One directory per installed revision for the Tau Ceti queue's systemd units
# (docs/queue/operations.md, "Install and update").
#
# Sourced by install.sh, install-repair.sh, install-monitoring.sh and install-repair-cleanup.sh;
# also a command for operators:
#
#   services/queue/release.sh status   which release each installed queue unit names, and which one
#                                      its running process actually runs from
#   services/queue/release.sh gc       remove release directories nothing uses (as after an install)
#
# Each install materialises the requested revision as a detached git worktree of the shared clone
# (~/TauCetiQueue by default) in ~/TauCetiQueue-releases/<sha>-<UTC time>/, builds its own uv
# environment there and bakes tauceti_queue/_revision.py. The installer then hands off to that
# release's copy of itself, renders only the units it installs so that they name the release
# directory, and restarts only those units. Nothing ever changes inside a release a unit points at,
# so a running unit never sees code it was not started with, and a crash restarts the same code.
#
# Garbage collection removes release directories whose name has the <sha>-<time> form, that no file
# under ~/.config/systemd/user names and that no running process uses as its working directory,
# keeping the newest $TAUCETI_QUEUE_RELEASE_KEEP (default 5) of those for rollback. Other entries in
# ~/TauCetiQueue-releases (hand-made worktrees) are never touched.
# shellcheck disable=SC2034  # variables used by the scripts that source this file

# Failures inside $(...) (fetch, worktree add) must stop the installer, not be swallowed.
shopt -s inherit_errexit

RELEASES_DIR="$HOME/TauCetiQueue-releases"
RELEASE_MARKER=".tauceti-queue-release"
RELEASE_KEEP="${TAUCETI_QUEUE_RELEASE_KEEP:-5}"
RELEASE_UNIT_DIR="$HOME/.config/systemd/user"
RELEASE_NAME_RE='^[0-9a-f]{40}-[0-9]{8}T[0-9]{6}Z$'

_release_log() { echo "${RELEASE_SCRIPT:-release.sh}: $*" >&2; }
_release_die() { _release_log "$*"; exit 1; }

# The physical path of a directory (symlinks resolved), or nothing.
_release_real() { (cd "$1" 2>/dev/null && pwd -P); }

# release_lock: hold a shared lock on the releases directory for the rest of the install (fd 9,
# inherited across the handoff), from choosing or creating a release until its units name it, so
# that a concurrent garbage collection, which takes the lock exclusively, cannot remove it.
release_lock() {
  mkdir -p "$RELEASES_DIR"
  if ! { true >&9; } 2>/dev/null; then exec 9>>"$RELEASES_DIR/.lock"; fi
  flock -s 9 || _release_die "cannot lock $RELEASES_DIR/.lock"
}

# release_sha DIR: the revision a release directory was built from (its marker), or nothing.
release_sha() {
  { sed -n 's/^sha=//p' "$1/$RELEASE_MARKER" 2>/dev/null || true; } | head -n 1
}

# release_unit_dir UNIT: the directory the installed unit file runs from (WorkingDirectory, with
# %h expanded), or nothing when the unit is not installed.
release_unit_dir() {
  local file="$RELEASE_UNIT_DIR/$1" dir
  # An instance without its own unit file runs the template (the layout before releases).
  case "$1" in *@?*) [ -f "$file" ] || file="$RELEASE_UNIT_DIR/${1%%@*}@.service" ;; esac
  [ -f "$file" ] || return 0
  dir="$(sed -n 's/^WorkingDirectory=//p' "$file" | tail -n 1)"
  echo "${dir//%h/$HOME}"
}

# release_dir_revision DIR: the revision a code directory holds: a release's marker, else its baked
# _revision.py, else git HEAD (the shared checkout before migration).
release_dir_revision() {
  local dir="$1" sha
  sha="$(release_sha "$dir")"
  [ -n "$sha" ] || sha="$(sed -n 's/^SHA = "\([0-9a-f]\{40\}\)"$/\1/p' "$dir/tauceti_queue/_revision.py" 2>/dev/null || true)"
  [ -n "$sha" ] || sha="$(git -C "$dir" rev-parse --verify --quiet HEAD 2>/dev/null || true)"
  echo "$sha"
}

# release_fetch CHECKOUT REVISION REPO_URL: clone the shared checkout when absent, fetch, and print
# the commit REVISION resolves to. The checkout's own working tree is never changed.
release_fetch() {
  local checkout="$1" revision="$2" url="$3"
  if [ ! -d "$checkout/.git" ]; then
    _release_log "cloning $url into $checkout"
    git clone --quiet "$url" "$checkout" || _release_die "cannot clone $url"
  fi
  _release_log "fetching origin into $checkout"
  git -C "$checkout" fetch --quiet --prune origin || _release_die "cannot fetch origin into $checkout"
  git -C "$checkout" rev-parse --verify --quiet "${revision}^{commit}" || _release_die "unknown revision $revision"
}

# release_create CHECKOUT SHA: add a new release directory for SHA and print its path.
release_create() {
  local checkout="$1" sha="$2" dir
  mkdir -p "$RELEASES_DIR"
  # mkdir reserves the name atomically; `git worktree add` accepts the empty directory.
  while :; do
    dir="$RELEASES_DIR/$sha-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir "$dir" 2>/dev/null && break
    sleep 1
  done
  _release_log "creating release $dir"
  git -C "$checkout" worktree add --quiet --detach "$dir" "$sha" >&2 || _release_die "cannot create the worktree $dir"
  echo "$dir"
}

# release_build DIR SHA [UV_SYNC_ARGS...]: build the release's uv environment and bake its revision,
# then write the marker that makes it a complete release. A release is built once; reusing one
# (rollback) never resyncs it, because units may already run from it; afterwards UV_NO_SYNC keeps
# the installer's own `uv run` calls from changing the environment.
release_build() {
  local dir="$1" sha="$2"
  shift 2
  if [ -f "$dir/$RELEASE_MARKER" ]; then
    [ "$(release_sha "$dir")" = "$sha" ] || _release_die "$dir is a release of $(release_sha "$dir"), not $sha"
    _release_log "release $dir is already built"
    export UV_NO_SYNC=1
    return 0
  fi
  [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)" = "$sha" ] || _release_die "$dir is not a checkout of $sha"
  _release_log "syncing the uv environment in $dir"
  (cd "$dir" && uv sync --quiet "$@")
  # /status and the workers report the revision the process was started from; tauceti_queue/runtime.py
  # prefers this file over `git rev-parse`.
  printf 'SHA = "%s"\nDIRTY = False\n' "$sha" > "$dir/tauceti_queue/_revision.py.tmp"
  mv "$dir/tauceti_queue/_revision.py.tmp" "$dir/tauceti_queue/_revision.py"
  printf 'sha=%s\ncreated=%s\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$dir/$RELEASE_MARKER.tmp"
  mv "$dir/$RELEASE_MARKER.tmp" "$dir/$RELEASE_MARKER"
  export UV_NO_SYNC=1
}

# release_handoff CHECKOUT REVISION RELEASE SCRIPT [ARGS...]: run SCRIPT's copy from the release,
# with the same arguments. RELEASE (an existing release directory) wins over REVISION; otherwise a
# new release of REVISION is created from CHECKOUT, after checking that it moves $RELEASE_UNIT
# (the installer's main unit) forward. Never returns.
release_handoff() {
  local checkout="$1" revision="$2" release="$3" script="$4" sha
  shift 4
  release_lock
  if [ -n "$release" ]; then
    release="$(_release_real "$release")" || _release_die "no release directory $3"
    sha="$(release_sha "$release")"
    [ -n "$sha" ] || _release_die "$release is not a complete release (no $RELEASE_MARKER)"
  else
    command -v git >/dev/null || _release_die "git is required"
    sha="$(release_fetch "$checkout" "$revision" "${REPO_URL:-https://github.com/TauCetiProject/TauCetiWorker.git}")"
    if [ -n "${RELEASE_UNIT:-}" ]; then release_check_forward "$checkout" "$RELEASE_UNIT" "$sha"; fi
    release="$(release_create "$checkout" "$sha")"
  fi
  local copy
  copy="$release/services/queue/$(basename "$script")"
  [ -f "$copy" ] || _release_die "$release has no $(basename "$script")"
  _release_log "handing off to $copy (revision ${sha:0:12})"
  TAUCETI_QUEUE_RELEASE="$release" TAUCETI_QUEUE_RELEASE_SHA="$sha" exec bash "$copy" "$@"
}

# release_enter SCRIPT: called by the release's own copy after the handoff. Checks that SCRIPT runs
# from $TAUCETI_QUEUE_RELEASE and changes into it.
release_enter() {
  local script_root
  script_root="$(_release_real "$(dirname "$1")/../..")"
  RELEASE="$(_release_real "$TAUCETI_QUEUE_RELEASE")" || _release_die "no release directory $TAUCETI_QUEUE_RELEASE"
  [ "$script_root" = "$RELEASE" ] || _release_die "$1 does not belong to release $RELEASE"
  [[ "$RELEASE" =~ ^[A-Za-z0-9/._@+-]+$ ]] || _release_die "unsupported characters in $RELEASE"
  target="$TAUCETI_QUEUE_RELEASE_SHA"
  [[ "$target" =~ ^[0-9a-f]{40}$ ]] || _release_die "bad release revision $target"
  release_lock
  cd "$RELEASE"
}

# release_check_forward CHECKOUT UNIT TARGET: refuse to move UNIT to a revision that does not
# descend from the one it runs now (the release it names, or the shared checkout before migration).
release_check_forward() {
  local checkout="$1" unit="$2" target="$3" dir current
  dir="$(release_unit_dir "$unit")"
  [ -n "$dir" ] || return 0
  current="$(release_dir_revision "$dir")"
  [ -n "$current" ] || return 0
  if ! git -C "$checkout" merge-base --is-ancestor "$current" "$target" 2>/dev/null; then
    _release_die "$unit runs ${current:0:12}, which is not an ancestor of ${target:0:12}; refusing to move it backwards (to roll back, pass --release DIR)"
  fi
}

# release_render_unit NAME [DEST_NAME]: install services/queue/NAME from the current release as
# ~/.config/systemd/user/DEST_NAME, pointing every %h/TauCetiQueue path at the release.
release_render_unit() {
  local name="$1" dest="${2:-$1}"
  mkdir -p "$RELEASE_UNIT_DIR"
  {
    echo "# Installed from $RELEASE (revision $target) by services/queue/$(basename "${RELEASE_SCRIPT:-install}")."
    echo "# Rerun the installer rather than editing this file (docs/queue/operations.md, \"Install and update\")."
    sed -E "s#%h/TauCetiQueue(/|[[:space:]]|\$)#$RELEASE\\1#g" "$RELEASE/services/queue/$name"
  } > "$RELEASE_UNIT_DIR/$dest.tmp"
  chmod 0644 "$RELEASE_UNIT_DIR/$dest.tmp"
  mv "$RELEASE_UNIT_DIR/$dest.tmp" "$RELEASE_UNIT_DIR/$dest"
}

# release_unit_cwd UNIT: the working directory of the unit's running main process, or nothing.
release_unit_cwd() {
  local pid
  pid="$(systemctl --user show -p MainPID --value "$1" 2>/dev/null || true)"
  [ -n "$pid" ] && [ "$pid" != 0 ] || return 0
  readlink -f "/proc/$pid/cwd" 2>/dev/null || true
}

# release_verify_running UNIT: whether the unit's running process runs from the current release.
release_verify_running() {
  [ "$(release_unit_cwd "$1")" = "$RELEASE" ]
}

# release_gc: remove unused release directories (see the header).
release_gc() {
  [ -d "$RELEASES_DIR" ] || return 0
  case "$RELEASE_KEEP" in ''|*[!0-9]*) _release_die "TAUCETI_QUEUE_RELEASE_KEEP takes a non-negative integer" ;; esac
  # Exclusive against every install between choosing a release and naming it in a unit (release_lock);
  # an install of our own has already named its release, so it gives up its shared lock first.
  flock -u 9 2>/dev/null || true
  exec 8>>"$RELEASES_DIR/.lock"
  if ! flock -x -n 8; then
    _release_log "another install holds $RELEASES_DIR/.lock; not collecting releases now"
    exec 8>&-
    return 0
  fi
  local used name dir kept=0
  # Every visible process's working directory (unreadable ones are other users').
  used="$(find /proc -mindepth 2 -maxdepth 2 -name cwd -printf '%l/\n' 2>/dev/null || true)"
  local candidates=()
  while IFS= read -r name; do
    [[ "$name" =~ $RELEASE_NAME_RE ]] || continue
    dir="$(_release_real "$RELEASES_DIR/$name")" || continue
    if [ -d "$RELEASE_UNIT_DIR" ] && grep -rqsF -- "$dir" "$RELEASE_UNIT_DIR"; then continue; fi
    if [ "$dir" != "$RELEASES_DIR/$name" ] && [ -d "$RELEASE_UNIT_DIR" ] && grep -rqsF -- "$RELEASES_DIR/$name" "$RELEASE_UNIT_DIR"; then continue; fi
    case "$used" in *"$dir/"*) continue ;; esac
    candidates+=("$dir")
  done < <(ls -1 "$RELEASES_DIR" | sort -t- -k2,2r)
  for dir in "${candidates[@]}"; do
    if [ "$kept" -lt "$RELEASE_KEEP" ]; then
      kept=$((kept + 1))
      continue
    fi
    _release_log "removing unused release $dir"
    local common
    common="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
    if [ -n "$common" ] && git --git-dir="$common" worktree remove --force "$dir" 2>/dev/null; then
      git --git-dir="$common" worktree prune 2>/dev/null || true
    else
      rm -rf -- "$dir"
    fi
  done
  exec 8>&-
}

# release_status: each installed or running queue unit, the revision and directory its unit file
# names, and the revision its running process runs (marked * when that is another directory).
release_status() {
  local unit dir cwd installed running
  printf '%-40s %-13s %-14s %s\n' UNIT INSTALLED RUNNING DIRECTORY
  {
    for unit in "$RELEASE_UNIT_DIR"/tauceti-queue*.service; do
      [ -f "$unit" ] && basename "$unit"
    done
    systemctl --user list-units --all --plain --no-legend 'tauceti-queue*.service' 2>/dev/null | awk '{print $1}'
  } | grep -v '@\.service$' | sort -u | while IFS= read -r unit; do
    dir="$(release_unit_dir "$unit")"
    [ -n "$dir" ] || continue
    installed="$(release_dir_revision "$dir")"
    cwd="$(release_unit_cwd "$unit")"
    running="-"
    if [ -n "$cwd" ]; then
      running="$(release_dir_revision "$cwd")"
      running="${running:0:12}"
      [ "$cwd" = "$(_release_real "$dir")" ] || running="$running*"
    fi
    printf '%-40s %-13s %-14s %s\n' "$unit" "${installed:0:12}" "$running" "$dir"
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  case "${1:-}" in
    status) release_status ;;
    gc) release_gc ;;
    *) echo "usage: services/queue/release.sh status|gc" >&2; exit 1 ;;
  esac
fi
