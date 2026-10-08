#!/usr/bin/env python3
"""Host coding rounds fetch both caches before launch and stop on download failures."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    co = root / "project"
    co.mkdir()
    (co / "lakefile.toml").write_text('name = "TauCeti"\n')
    calls = root / "calls.jsonl"
    shim = root / "lake"
    shim.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['CACHE_TEST_CALLS'])
a = sys.argv[1:]
label = 'Mathlib' if a[:2] == ['exe', 'cache'] else 'TauCeti'
prior = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
config = Path(os.environ['LAKE_CONFIG']).read_text() if label == 'TauCeti' else ''
with path.open('a') as f:
 f.write(json.dumps({'label': label, 'args': a, 'config': config,
                     'cache_dir': os.environ.get('LAKE_CACHE_DIR'),
                     'restore': os.environ.get('LAKE_RESTORE_ARTIFACTS')}) + '\\n')
mode = os.environ.get('CACHE_TEST_MODE', '')
if mode == label + '-retry' and not any(c['label'] == label for c in prior):
 print('error: connection reset'); sys.exit(1)
if mode == label + '-fail':
 print('error: artifact transfer failed'); sys.exit(1)
if mode == 'miss' and label == 'TauCeti':
 print('error: TauCetiProject/TauCeti: no outputs found in 100 revisions from HEAD'); sys.exit(1)
print('cache ready')
""")
    shim.chmod(0o755)
    launched = []
    env = {
        "PATH": f"{root}:{os.environ['PATH']}",
        "CACHE_TEST_CALLS": str(calls),
        "LAKE_CACHE_DIR": str(root / "private-lake-store"),
        "LAKE_ARTIFACT_CACHE": "1",
        "LAKE_RESTORE_ARTIFACTS": "1",
    }

    def launch(*args, **kwargs):
        launched.append((args, kwargs))
        return 0

    def run(mode="", cwd=co, **extra):
        calls.unlink(missing_ok=True)
        launched.clear()
        output = io.StringIO()
        with patch.dict(os.environ, {**env, "CACHE_TEST_MODE": mode, **extra}, clear=True):
            with (
                patch.object(agents, "run_agent_proc", launch),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                rc = agents.run_agent_host(cwd, "prompt", "claude", root / "logs")
        recorded = [json.loads(s) for s in calls.read_text().splitlines()] if calls.exists() else []
        return rc, recorded, output.getvalue()

    rc, records, _ = run()
    assert rc == 0 and len(launched) == 1
    assert [r["label"] for r in records] == ["Mathlib", "TauCeti"]
    assert "--repo" in records[1]["args"] and "TauCetiProject/TauCeti" in records[1]["args"]
    assert "--max-revs=100" in records[1]["args"]
    assert "cache.taucetiproject.org/artifacts" in records[1]["config"]
    assert "cache.taucetiproject.org/revisions" in records[1]["config"]
    assert all(r["cache_dir"] == env["LAKE_CACHE_DIR"] and r["restore"] == "1" for r in records)
    assert "LAKE_CONFIG" not in launched[0][1]["env"]  # temporary download config never leaks
    print("[OK] both caches fetched before launch in the selected workspace")

    for label in ("Mathlib", "TauCeti"):
        rc, records, _ = run(label + "-retry")
        assert rc == 0 and len(launched) == 1
        assert sum(r["label"] == label for r in records) == 2
        rc, records, text = run(label + "-fail")
        assert rc == 75 and not launched
        assert label in agents.take_last_agent_infra_failure()
        assert "artifact transfer failed" in text
    print("[OK] transient fetch retries; persistent or partial download failures block the agent")

    rc, records, text = run("miss")
    assert rc == 0 and len(launched) == 1 and len(records) == 2
    assert "uncached modules will compile from source" in text
    print("[OK] genuinely unpublished history warns explicitly without pointless retries")

    rc, records, _ = run(LAKE_ARTIFACT_CACHE="false")
    assert rc == 0 and len(records) == 1 and records[0]["label"] == "Mathlib"
    rc, records, _ = run(LAKE_CACHE_MAX_REVS="0")
    assert rc == 0 and "--max-revs=0" in records[1]["args"]
    rc, records, _ = run(LAKE_CACHE_MAX_REVS="bad")
    assert rc == 75 and not records and not launched
    print("[OK] operator opt-out and revision search limit are respected")

    prose = root / "prose"
    prose.mkdir()
    rc, records, _ = run(cwd=prose)
    assert rc == 0 and len(launched) == 1 and not records
    rc, records, _ = run(TAUCETI_AGENT_ECHO="1")
    assert rc == 0 and not launched and not records
    print("[OK] prose jobs and echo-only probes perform no cache work")
