#!/usr/bin/env python3
"""Host coding rounds fetch both caches before launch and stop on download failures."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents
from tauceti_worker import work_units as wu

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
                     'restore': os.environ.get('LAKE_RESTORE_ARTIFACTS'), 'cwd': str(Path.cwd())}) + '\\n')
mode = os.environ.get('CACHE_TEST_MODE', '')
if mode == label + '-retry' and not any(c['label'] == label for c in prior):
 print('error: connection reset'); sys.exit(1)
if mode == label + '-fail':
 print('error: artifact transfer failed'); sys.exit(1)
if mode == 'miss' and label == 'TauCeti':
 print('error: TauCetiProject/TauCeti: no outputs found in 100 revisions from HEAD'); sys.exit(1)
if mode == 'timeout':
 import time; time.sleep(20)
if mode == 'dependency-error' or mode.startswith('dependency-network'):
 if mode == 'dependency-error': print('info: fatal: not our ref abc')
 errors={'dependency-network':'HTTP response status code 503',
         'dependency-network-connect':"Failed to connect to github.com port 443: Couldn't connect to server",
         'dependency-network-timeout':'Operation timed out after 30000 milliseconds',
         'dependency-network-eof':'fatal: the remote end hung up unexpectedly; early EOF',
         'dependency-network-empty':'Empty reply from server',
         'dependency-network-unknown':'unrecognized git failure'}
 if mode in errors: print(errors[mode])
 print('error: mathlib: failed to fetch the package revision abc from the Git repository at https://example.invalid/mathlib')
 sys.exit(1)
if mode == 'invalid-toolchain':
 print("error: could not download nonexistent lean version 'leanprover-lean4-v0.bad'"); sys.exit(1)
if mode == 'workspace-error':
 print('error: lakefile.toml: expected string'); sys.exit(1)
if mode == 'rebase' and '--rev' in a and a[a.index('--rev')+1] == 'b'*40:
 print('error: TauCetiProject/TauCeti: outputs not found for revision ' + 'b'*40); sys.exit(1)
print('cache ready')
""")
    shim.chmod(0o755)
    git = root / "git"
    git.write_text("""#!/usr/bin/env python3
import os, sys
args=sys.argv[1:]
if args[0]=='rev-parse':
 print(('b' if os.environ.get('CACHE_TEST_MODE')=='rebase' and args[-1]=='origin/main' else 'a')*40)
elif args[0]=='show': print('leanprover/lean4:v4.35.0-rc3')
elif args[0]=='rev-list': print('b'*40+'\\n'+'c'*40)
""")
    git.chmod(0o755)
    launched = []
    env = {
        "PATH": f"{root}:{os.environ['PATH']}",
        "CACHE_TEST_CALLS": str(calls),
        "LAKE_CACHE_DIR": str(root / "private-lake-store"),
        "LAKE_ARTIFACT_CACHE": "1",
        "LAKE_RESTORE_ARTIFACTS": "1",
    }

    def launch(*args, **kwargs):
        if "LAKE_CONFIG" in kwargs["env"]:
            assert Path(kwargs["env"]["LAKE_CONFIG"]).is_file()
            assert "cache.taucetiproject.org" in Path(kwargs["env"]["LAKE_CONFIG"]).read_text()
            assert all(
                kwargs["env"][k] == os.environ.get(k, env[k])
                for k in ("LAKE_CACHE_DIR", "LAKE_ARTIFACT_CACHE", "LAKE_RESTORE_ARTIFACTS")
            )
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
                try:
                    rc = agents.run_agent_host(cwd, "prompt", "claude", root / "logs")
                except agents.BuildCacheUnavailable:
                    rc = 75
                except agents.Die:
                    rc = 1
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
    assert not Path(launched[0][1]["env"]["LAKE_CONFIG"]).exists()  # cleaned after the agent exits
    assert all(r["cwd"] == str(co) for r in records)
    print("[OK] both caches fetched before launch in the selected workspace")

    for label in ("Mathlib", "TauCeti"):
        rc, records, _ = run(label + "-retry")
        assert rc == 0 and len(launched) == 1
        assert sum(r["label"] == label for r in records) == 2
        rc, records, text = run(label + "-fail")
        assert rc == 75 and not launched
        assert agents.take_last_agent_infra_failure() is None
        assert "artifact transfer failed" in text
    print("[OK] transient fetch retries; persistent or partial download failures block the agent")

    rc, records, text = run("miss")
    assert rc == 75 and not launched and len(records) == 2
    print("[OK] uncached main stops instead of silently compiling the entire library")

    rc, records, text = run("rebase")
    assert rc == 0 and len(launched) == 1 and len(records) == 4
    assert records[2]["args"][-4:] == ["--rev", "b" * 40, "--toolchain", "leanprover/lean4:v4.35.0-rc3"]
    assert records[3]["args"][-4:] == ["--rev", "c" * 40, "--toolchain", "leanprover/lean4:v4.35.0-rc3"]
    rc, records, text = run("workspace-error")
    assert rc == 0 and len(launched) == 1 and len(records) == 1
    for mode in ("dependency-error", "invalid-toolchain"):
        rc, records, _ = run(mode)
        assert rc == 0 and len(launched) == 1 and len(records) == 1
    for mode in (
        "dependency-network",
        "dependency-network-connect",
        "dependency-network-timeout",
        "dependency-network-eof",
        "dependency-network-empty",
        "dependency-network-unknown",
    ):
        rc, records, _ = run(mode)
        assert rc == 75 and not launched
    print("[OK] broken manifests, revisions and toolchains reach repair; wrapped transport failures still block")

    rc, records, _ = run(LAKE_ARTIFACT_CACHE="false")
    assert rc == 0 and len(records) == 1 and records[0]["label"] == "Mathlib"
    rc, records, _ = run(LAKE_CACHE_MAX_REVS="0")
    assert rc == 0 and "--max-revs=0" in records[1]["args"]
    rc, records, _ = run(LAKE_CACHE_MAX_REVS="bad")
    assert rc == 1 and not records and not launched
    print("[OK] operator opt-out and revision search limit are respected")

    prose = root / "prose"
    prose.mkdir()
    rc, records, _ = run(cwd=prose)
    assert rc == 0 and len(launched) == 1 and not records
    rc, records, _ = run(TAUCETI_AGENT_ECHO="1")
    assert rc == 0 and not launched and not records
    print("[OK] prose jobs and echo-only probes perform no cache work")

    with patch.object(agents, "HOST_CACHE_TIMEOUT", 0.1):
        rc, records, text = run("timeout")
    assert rc == 75 and not launched
    assert "deadline exceeded" in text or "timed out" in text
    print("[OK] a single bootstrap deadline blocks launch and terminates the downloader")

    worker = SimpleNamespace(
        cfg=SimpleNamespace(checkout=co, logdir=root / "logs", state=root / "state"),
        claims=SimpleNamespace(begin_branch_work=lambda *a: True),
        rs=SimpleNamespace(bust=lambda *a: None),
    )
    worker.counters = wu.Counters(worker.cfg)
    candidate = SimpleNamespace(pr=1434, head="a" * 40)
    survey = SimpleNamespace(
        open_prs=[SimpleNamespace(number=1434, head_owner="fork", head_repo="TauCeti", head_ref="branch")]
    )
    key = "ci-pr-1434"
    worker.counters.write("infra-fix-ci-1434", 99)
    real_run = agents.subprocess.run
    events = []

    def checkout(command, **kwargs):
        if command[:3] == ["gh", "pr", "checkout"]:
            events.append("checkout")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(command, **kwargs)

    def prepare(cfg):
        events.append("prepare")
        return True

    def observe_agent(*args, **kwargs):
        assert events == ["prepare", "checkout"]
        return agents.run_agent_host(*args, **kwargs)

    calls.unlink(missing_ok=True)
    with patch.dict(os.environ, {**env, "CACHE_TEST_MODE": "TauCeti-fail"}, clear=True):
        with (
            patch.object(wu, "prepare_checkout", prepare),
            patch.object(wu, "run_agent_host", observe_agent),
            patch.object(agents.subprocess, "run", checkout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            worker.counters.write(key, 1)
            try:
                wu._do_fixlike(
                    worker,
                    survey,
                    candidate,
                    SimpleNamespace(agent_name="Claude", work_model="claude"),
                    False,
                    prompt_file="fix-ci.md",
                    label="fix-ci",
                    charged=(key,),
                )
            except agents.BuildCacheUnavailable:
                pass
            else:
                raise AssertionError("cache outage must pause the round")
    assert worker.counters.read(key) == 0
    assert worker.counters.read("infra-fix-ci-1434") == 99
    print("[OK] fix checkout precedes warming; even an exhausted provider-refund allowance cannot charge cache outages")

    waits = []
    signals = []

    def interrupted_wait(**kwargs):
        waits.append(kwargs)
        if len(waits) == 1:
            raise SystemExit(143)  # RoundContext's SIGTERM handler
        return 0

    proc = SimpleNamespace(pid=12345, wait=interrupted_wait)
    with (
        patch.object(agents.subprocess, "Popen", lambda *a, **kw: proc),
        patch.object(agents.os, "killpg", lambda pid, sig: signals.append((pid, sig))),
        (root / "interrupt.log").open("ab") as output,
    ):
        try:
            agents._cache_fetch(["lake"], co, env, output, 10)
        except SystemExit as e:
            assert e.code == 143
        else:
            raise AssertionError("round interruption must propagate")
    assert signals == [(12345, agents.signal.SIGTERM), (12345, agents.signal.SIGKILL)]
    print("[OK] round interruption cleans up the complete downloader process group")
