#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

printf '%s\n' '#!/usr/bin/env bash' 'printf "<%s>\\n" "$@"' >"$temporary/tauceti"
chmod +x "$temporary/tauceti"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 \
    TAUCETI_WORKER_ARGS='--only roadmap --roadmap-only "Some Area"' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
expected=$'<work>\n<--loop>\n<--only>\n<roadmap>\n<--roadmap-only>\n<Some Area>'
test "$output" = "$expected"
echo "[OK ] worker options are appended as distinct arguments"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS='' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
test "$output" = $'<work>\n<--loop>'
echo "[OK ] an empty option setting leaves the worker command unchanged"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS='--only roadmap' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti doctor
})"
test "$output" = '<doctor>'
echo "[OK ] worker options do not affect other container commands"

marker="$temporary/should-not-exist"
output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 \
    TAUCETI_WORKER_ARGS="--roadmap-only '\$(touch $marker)'" \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
test ! -e "$marker"
test "$output" = $'<work>\n<--loop>\n<--roadmap-only>\n<$(touch '"$marker"$')>'
echo "[OK ] worker options are parsed without shell evaluation"

set +e
error="$(
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS="'" \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop 2>&1
)"
status=$?
set -e
test "$status" -ne 0
[[ "$error" == tauceti-entrypoint:*TAUCETI_WORKER_ARGS*invalid\ quoting* ]]
[[ "$error" != *Traceback* ]]
echo "[OK ] malformed quoting fails closed with a useful error"

# An identity halt stops the container before any GitHub contact, on every restart.
bin="$temporary/bin"
mkdir -p "$bin" "$temporary/data/state/docker"
ln -s "$ROOT/tauceti_worker" "$temporary/tauceti_worker"
printf '%s\n' '#!/usr/bin/env bash' 'echo "$*" >>"$TEST_GH_LOG"; [[ "$1" == api ]] && echo 11; exit 0' >"$bin/gh"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$bin/git"
printf '%s\n' '#!/usr/bin/env bash' 'echo "<idle $*>"' >"$bin/sleep"
chmod +x "$bin/gh" "$bin/git" "$bin/sleep"
record="$temporary/data/state/docker/halt.json"
printf '%s\n' '{"reason": "account-halt", "detail": "suspended", "at": "then"}' >"$record"
launch() {
  (
    cd "$temporary"
    PATH="$bin:$PATH" TEST_GH_LOG="$temporary/gh.log" TAUCETI_LEGACY_DATA_ROOT="$temporary/data" \
      TAUCETI_WORKER_ID=docker TAUCETI_WORKER_ARGS="$1" \
      "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop 2>"$temporary/stderr"
  )
}
for _ in 1 2; do
  output="$(launch '--github-account alice')"
  test "$output" = '<idle infinity>'
  test ! -e "$temporary/gh.log"
  grep -q 'docker compose exec tauceti ./tauceti work --worker-id docker --clear-halt' "$temporary/stderr"
done
test -e "$record"
echo "[OK ] a halted worker idles before any gh call, on every restart"

output="$(launch '--only roadmap')"
test "$(tail -1 <<<"$output")" = '<roadmap>'
grep -q '^auth status' "$temporary/gh.log"
echo "[OK ] a halt record without a configured account leaves startup unchanged"
