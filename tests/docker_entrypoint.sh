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

# The shared Mathlib opt-in: the entrypoint re-runs itself once through tauceti-userns.
mkdir "$temporary/bin"
printf '%s\n' '#!/usr/bin/env bash' 'echo "userns $(basename "$1")"' \
  'TAUCETI_IN_USER_NAMESPACE=1 exec "$@"' >"$temporary/bin/tauceti-userns"
chmod +x "$temporary/bin/tauceti-userns"
output="$({
  cd "$temporary"
  PATH="$temporary/bin:$PATH" TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_USER_NAMESPACE=1 \
    TAUCETI_WORKER_ARGS='--only roadmap' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
test "$output" = $'userns docker-entrypoint\n<work>\n<--loop>\n<--only>\n<roadmap>'
echo "[OK ] the opt-in enters the user namespace once, before anything else"

output="$({
  cd "$temporary"
  PATH="$temporary/bin:$PATH" TAUCETI_SKIP_GIT_IDENTITY=1 \
    "$ROOT/scripts/docker-entrypoint" ./tauceti doctor
})"
test "$output" = '<doctor>'
echo "[OK ] without the opt-in no user namespace is requested"

# The real helper, where it cannot or need not create the namespace, still runs the command and
# says which case applied, so the entrypoint does not ask again.
ln -sf "$ROOT/scripts/docker_userns.py" "$temporary/bin/tauceti-userns"
set +e
output="$(
  cd "$temporary"
  PATH="$temporary/bin:$PATH" TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_USER_NAMESPACE=1 \
    "$ROOT/scripts/docker-entrypoint" sh -c 'echo "marker=$TAUCETI_IN_USER_NAMESPACE"' 2>"$temporary/stderr"
)"
status=$?
set -e
if [[ "$(id -u)" != 0 ]]; then
  test "$status" -eq 0
  test "$output" = 'marker=0'
  test ! -s "$temporary/stderr"
  echo "[OK ] an unprivileged user is left to bubblewrap's own user namespace"
elif [[ "$output" == 'marker=1' ]]; then
  test "$status" -eq 0
  echo "[OK ] root entered an identity-mapped user namespace"
else
  test "$status" -eq 0
  test "$output" = 'marker=0'
  grep -F 'tauceti-userns: WARNING: no user namespace' "$temporary/stderr" >/dev/null
  echo "[OK ] root without the seccomp profile continues with a warning"
fi
