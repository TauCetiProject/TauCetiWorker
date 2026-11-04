#!/usr/bin/env bash
# Online backup of the queue service's SQLite database (docs/queue/operations.md, "Backups").
#
# Uses SQLite's backup API through Python's stdlib, so the service keeps running (WAL readers
# and the backup do not block each other) and the copy is a consistent snapshot. Writes
# queue-YYYYmmddTHHMMSSZ.sqlite3 into the backup directory, verifies it with `PRAGMA
# integrity_check`, and keeps the newest $KEEP copies.
#
# Usage: services/queue/backup.sh [--database PATH] [--dir DIR] [--keep N]
#   --database PATH  defaults to the configured database (tauceti-queue check-config --print database)
#   --dir DIR        defaults to ~/.local/state/tauceti-queue/backups
#   --keep N         copies to retain (default 14)
set -euo pipefail

DATABASE=""
DIR="${TAUCETI_QUEUE_BACKUP_DIR:-$HOME/.local/state/tauceti-queue/backups}"
KEEP=14

while [ $# -gt 0 ]; do
  case "$1" in
    --database) DATABASE="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "backup.sh: unknown argument $1" >&2; exit 1 ;;
  esac
done

if [ -z "$DATABASE" ]; then
  if command -v tauceti-queue >/dev/null; then
    DATABASE="$(tauceti-queue check-config --print database)"
  else
    DATABASE="$(cd "${TAUCETI_QUEUE_CHECKOUT:-$HOME/TauCetiQueue}" && uv run tauceti-queue check-config --print database)"
  fi
fi
[ -f "$DATABASE" ] || { echo "backup.sh: no database at $DATABASE (nothing to back up yet)" >&2; exit 0; }
case "$KEEP" in ''|*[!0-9]*) echo "backup.sh: --keep takes a positive integer" >&2; exit 1 ;; esac

mkdir -p "$DIR" && chmod 0700 "$DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$DIR/queue-$stamp.sqlite3"

python3 - "$DATABASE" "$target" <<'EOF'
import sqlite3, sys, os
source_path, target_path = sys.argv[1], sys.argv[2]
partial = target_path + ".partial"
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
target = sqlite3.connect(partial)
with target:
    source.backup(target)
source.close()
check = target.execute("PRAGMA integrity_check").fetchone()[0]
target.close()
if check != "ok":
    os.unlink(partial)
    sys.exit(f"backup.sh: integrity check failed on the copy: {check}")
os.chmod(partial, 0o600)
os.replace(partial, target_path)
print(f"backup.sh: wrote {target_path} ({os.path.getsize(target_path)} bytes)")
EOF

# Rotation: keep the newest $KEEP copies (names sort chronologically).
mapfile -t copies < <(ls -1 "$DIR"/queue-*.sqlite3 2>/dev/null | sort -r)
if [ "${#copies[@]}" -gt "$KEEP" ]; then
  for old in "${copies[@]:$KEEP}"; do
    rm -f -- "$old"
    echo "backup.sh: removed $old" >&2
  done
fi
