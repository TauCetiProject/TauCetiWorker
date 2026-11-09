#!/usr/bin/env bash
# Install lifecycle cleanup on a host with the authoritative database and repair worktrees.
# Usage: install-repair-cleanup.sh --database PATH --clone PATH --root PATH [--root PATH ...]
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 - "$@" <<'PY'
import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path, required=True)
parser.add_argument('--clone', type=Path, required=True)
parser.add_argument('--root', type=Path, action='append', required=True)
parser.add_argument('--cache-root', type=Path, help='one shared repair cache root; migrate idle private pools')
args = parser.parse_args()
if not args.database.is_file() or not (args.clone / '.git').is_dir():
    parser.error('database and repair clone must already exist on this host')
config = Path.home() / '.config/tauceti-queue/repair-cleanup.json'
config.parent.mkdir(parents=True, exist_ok=True)
with open(config, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
    data = {'database': str(args.database.resolve()), 'clone': str(args.clone.resolve()),
            'roots': [str(root.resolve()) for root in args.root]}
    if args.cache_root:
        data['cache_root'] = str(args.cache_root.resolve())
    json.dump(data, output, indent=2)
    output.write('\n')
config.chmod(0o600)
PY
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
install -m 0644 services/queue/tauceti-queue-repair-cleanup.service services/queue/tauceti-queue-repair-cleanup.timer "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable --now tauceti-queue-repair-cleanup.timer
systemctl --user start tauceti-queue-repair-cleanup.service
