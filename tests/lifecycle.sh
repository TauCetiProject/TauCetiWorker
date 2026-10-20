#!/usr/bin/env bash
# Lifecycle tests (plan milestone 6) — flock, fd-leak negative test, timeout teardown, signal codes.
# Read-only: uses the TAUCETI_TEST_* hooks, never touches GitHub. Run from the repo root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY="${PY:-$(command -v python3)}"   # run-all injects the matrix interpreter; fall back to system python3
WID="lifecycle-test"
export TAUCETI_WORKER_ID="$WID"
LIFECYCLE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/tauceti-lifecycle-XXXXXX")
hold_group=""
cleanup() {
  [ -z "$hold_group" ] || kill -TERM -- "-$hold_group" 2>/dev/null || true
  rm -rf "$LIFECYCLE_TMP"
}
trap cleanup EXIT
pass=0; fail=0
ok()  { echo "  [PASS] $1"; pass=$((pass+1)); }
no()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

run() { "$PY" ./tauceti "$@"; }   # plain python3 (lifecycle hooks don't need rich)
# `setsid` is util-linux and absent on macOS. NEWSESS is a portable inline prefix that puts the round in
# its own session (own process group) via Python's os.setsid() before exec, matching how the loop driver
# spawns rounds (subprocess start_new_session=True). It MUST be expanded inline, never wrapped in a shell
# function: under `&` a function body runs in a subshell, so $! would be that wrapper (still in the
# harness's group) instead of the round — and a group-kill would then hit the harness.
NEWSESS=("$PY" -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])')

# Imports can take longer than one second on a busy host. The hook logs readiness only
# after RoundContext owns the lock and has installed both signal handlers.
wait_ready() {
  local pid=$1 logfile=$2 deadline=$((SECONDS + 15))
  while (( SECONDS < deadline )); do
    grep -qF "[test] holding the lock and sleeping" "$logfile" && return 0
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.05
  done
  no "round $pid did not become ready"
  cat "$logfile"
  return 1
}

echo "== 1. flock: a second concurrent round dies on the lock =="
TAUCETI_TEST_SLEEP=4 run _round >"$LIFECYCLE_TMP/lc1.log" 2>&1 &
bg=$!; wait_ready "$bg" "$LIFECYCLE_TMP/lc1.log"
TAUCETI_TEST_SLEEP=4 run _round >"$LIFECYCLE_TMP/lc2.log" 2>&1; rc=$?
grep -q "holds .*round.lock" "$LIFECYCLE_TMP/lc2.log" && (( rc == 1 )) && ok "second round refused (rc=$rc)" || no "second round not refused (rc=$rc)"
wait $bg

echo "== 2. fd-leak negative: a sleeping grandchild must NOT keep the lock =="
# Keep the deliberate surviving grandchild in our own process group for exact cleanup.
TAUCETI_TEST_HOLD=120 "${NEWSESS[@]}" "$PY" ./tauceti _round >"$LIFECYCLE_TMP/lc3.log" 2>&1 &
hold_group=$!; wait "$hold_group"; rc=$?
(( rc == 0 )) || no "holding round exited rc=$rc"
# immediately try to take the lock again; the grandchild (sleep 120) is still alive but must not hold it
TAUCETI_TEST_SLEEP=0 run _round >"$LIFECYCLE_TMP/lc4.log" 2>&1; rc=$?
(( rc == 0 )) && ok "second round took the lock immediately despite live grandchild" \
              || { no "second round blocked (rc=$rc) — grandchild leaked the lock fd"; cat "$LIFECYCLE_TMP/lc4.log"; }

echo "== 3. timeout teardown: a wedged round's process group is killed =="
# Start a round in its own session that holds the lock and sleeps long, then kill its whole group
# (this is what the loop driver's ROUND_TIMEOUT teardown does via os.killpg).
TAUCETI_TEST_SLEEP=60 "${NEWSESS[@]}" "$PY" ./tauceti _round >"$LIFECYCLE_TMP/lc5.log" 2>&1 &
child=$!; wait_ready "$child" "$LIFECYCLE_TMP/lc5.log"
pgid=$(ps -o pgid= -p "$child" | tr -d ' ')
self=$(ps -o pgid= -p $$ | tr -d ' ')
if [ -n "$pgid" ] && [ "$pgid" != "$self" ]; then
  kill -TERM -- "-$pgid" 2>/dev/null
  sleep 1
  kill -0 "$child" 2>/dev/null && { no "round survived SIGTERM"; kill -KILL -- "-$pgid" 2>/dev/null; } \
                               || ok "round group torn down by SIGTERM"
else
  no "could not isolate round in its own process group (pgid=$pgid self=$self)"; kill "$child" 2>/dev/null
fi

echo "== 4. signal exit codes: SIGTERM→143, SIGINT→130 =="
# Direct PID kill (no separate group needed): RoundContext maps SIGTERM→143 / SIGINT→130 via cleanup.
TAUCETI_TEST_SLEEP=30 "$PY" ./tauceti _round >"$LIFECYCLE_TMP/lc6.log" 2>&1 &
c=$!; wait_ready "$c" "$LIFECYCLE_TMP/lc6.log"; kill -TERM "$c"; wait "$c"; rc=$?
(( rc == 143 )) && ok "SIGTERM → 143" || no "SIGTERM → $rc (expected 143)"
TAUCETI_TEST_SLEEP=30 "$PY" ./tauceti _round >"$LIFECYCLE_TMP/lc7.log" 2>&1 &
c=$!
if wait_ready "$c" "$LIFECYCLE_TMP/lc7.log"; then kill -INT "$c"; else kill -TERM "$c"; fi
wait "$c"; rc=$?
(( rc == 130 )) && ok "SIGINT → 130" || no "SIGINT → $rc (expected 130)"

# The EXIT trap tears down only the process group created for the test grandchild.
echo
echo "lifecycle: $pass passed, $fail failed"
exit $(( fail > 0 ))
