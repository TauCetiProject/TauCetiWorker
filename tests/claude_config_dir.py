#!/usr/bin/env python3
"""Honor $CLAUDE_CONFIG_DIR: the pacer reads Claude creds from it, isolation copies from there and
repoints it at the per-worker copy, and the bubble path does not block a non-default value
(bubble honors the var itself)."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got}")
    if not ok:
        print(f"      expected: {expect}")
        fails += 1


home = Path("/home/example")
os.environ.pop("CLAUDE_CONFIG_DIR", None)
check("default falls back to <home>/.claude", tc.claude_dir(home), home / ".claude")
os.environ["CLAUDE_CONFIG_DIR"] = "/custom/work-claude"
check("env var wins", tc.claude_dir(home), Path("/custom/work-claude"))

# Isolation: copy creds from the operator's config dir, then repoint $CLAUDE_CONFIG_DIR at the copy so
# both the pacer (claude_dir) and the spawned claude read the isolated creds.
tmp = Path(tempfile.mkdtemp())
wid = f"claude-config-dir-test-{os.getpid()}"
shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)  # a prior interrupted run mustn't taint us
try:
    real, cfgdir = tmp / "realhome", tmp / "work-claude"
    cfgdir.mkdir(parents=True)
    (cfgdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "T"}}))
    (cfgdir / "CLAUDE.md").write_text("x")
    (real / ".codex").mkdir(parents=True)
    os.environ["HOME"] = str(real)
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
    iso_home = tc.isolate_home(wid)
    iso_claude = iso_home / ".claude"
    check("isolation copies creds from the config dir", (iso_claude / ".credentials.json").exists(), True)
    # The credential is seeded; the operator's instruction surface deliberately is NOT — the round
    # must not depend on whose CLAUDE.md/skills happen to sit in the real config dir. Details and
    # the migration of already-seeded homes are in tests/worker_claude_config.py.
    check("isolation leaves the operator's CLAUDE.md behind", (iso_claude / "CLAUDE.md").exists(), False)
    check("isolation writes its own settings.json", (iso_claude / "settings.json").is_file(), True)
    check("isolation repoints $CLAUDE_CONFIG_DIR at the copy", os.environ["CLAUDE_CONFIG_DIR"], str(iso_claude))
    check("claude_dir agrees with the spawned claude", tc.claude_dir(iso_home), iso_claude)
    creds = json.loads((tc.claude_dir(iso_home) / ".credentials.json").read_text())
    check("isolated creds are the operator's", creds["claudeAiOauth"]["accessToken"], "T")
    check("isolation records the source", (iso_claude / ".tauceti-creds-source").read_text().strip(), str(cfgdir))
finally:
    shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

# The env handed to the bubble subprocess must carry $CLAUDE_CONFIG_DIR (that's how bubble, which now
# honors the var per kim-em/bubble#317, seeds the matching creds). Point TAUCETI_BUBBLE_HOME at a home
# whose verified cache-isolation setting is already present, then check the env it returns.
bhome = Path(tempfile.mkdtemp())
try:
    (bhome / "config.toml").write_text('[security]\nshared_cache = "overlay"\n')
    os.environ["TAUCETI_BUBBLE_HOME"] = str(bhome)
    os.environ["CLAUDE_CONFIG_DIR"] = "/custom/work-claude"
    benv = tc.ensure_bubble_home(types.SimpleNamespace(home=bhome, wid="w"))
    check("bubble subprocess env carries $CLAUDE_CONFIG_DIR", benv.get("CLAUDE_CONFIG_DIR"), "/custom/work-claude")
finally:
    os.environ.pop("TAUCETI_BUBBLE_HOME", None)
    shutil.rmtree(bhome, ignore_errors=True)


# Bubble must NOT block a non-default $CLAUDE_CONFIG_DIR: bubble honors the var itself, so run_in_bubble
# gets past the (removed) guard. The guard sat before ensure_bubble_home, so a sentinel there proves we
# pass it (no Die) for both the non-default and default cases.
class PastGuard(Exception):
    pass


tc.agents.ensure_bubble_home = lambda cfg: (_ for _ in ()).throw(PastGuard())  # the first call after the removed guard
w = types.SimpleNamespace(cfg=types.SimpleNamespace(home=Path("/home/example"), state=Path("/tmp"), wid="w"))
opts = types.SimpleNamespace(work_model="claude")

for label, cfgdir in (("non-default", "/custom/work-claude"), ("default", str(Path("/home/example") / ".claude"))):
    os.environ["CLAUDE_CONFIG_DIR"] = cfgdir
    try:
        tc.run_in_bubble(w, "review", "PROMPT", opts)
        check(f"bubble + {label} config dir gets past the guard", "no raise", "PastGuard")
    except tc.Die:
        check(f"bubble + {label} config dir must NOT Die", "raised Die", "PastGuard")
    except PastGuard:
        check(f"bubble + {label} config dir gets past the guard (no Die)", True, True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
