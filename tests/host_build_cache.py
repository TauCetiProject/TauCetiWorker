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
if mode == 'disk-full' and not prior:
 print('error: No space left on device'); sys.exit(1)
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
        "TAUCETI_SHARED_ARTIFACTS": "0",
        "TAUCETI_SHARED_MATHLIB_ROOT": str(root / "shared-pool"),
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

    from tauceti_worker import shared_mathlib

    @contextlib.contextmanager
    def shared_view(cwd, env, logdir, fetch):
        yield ["env"]  # a transparent stand-in for the namespace prefix

    def run(mode="", cwd=co, **extra):
        calls.unlink(missing_ok=True)
        launched.clear()
        output = io.StringIO()
        with patch.dict(os.environ, {**env, "CACHE_TEST_MODE": mode, **extra}, clear=True):
            with (
                patch.object(shared_mathlib, "view", shared_view),
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
    assert [r["label"] for r in records] == ["TauCeti"]  # Mathlib comes from the shared view
    assert "--repo" in records[0]["args"] and "TauCetiProject/TauCeti" in records[0]["args"]
    assert "--max-revs=100" in records[0]["args"]
    assert "cache.taucetiproject.org/artifacts" in records[0]["config"]
    assert "cache.taucetiproject.org/revisions" in records[0]["config"]
    assert all(r["cache_dir"] == env["LAKE_CACHE_DIR"] and r["restore"] == "1" for r in records)
    assert not Path(launched[0][1]["env"]["LAKE_CONFIG"]).exists()  # cleaned after the agent exits
    assert all(r["cwd"] == str(co) for r in records)
    print("[OK] TauCeti's cache fetched before launch in the selected workspace, Mathlib never")

    rc, records, _ = run("Mathlib-fail")
    assert rc == 0 and len(launched) == 1 and not any(r["label"] == "Mathlib" for r in records)
    for label in ("TauCeti",):
        rc, records, _ = run(label + "-retry")
        assert rc == 0 and len(launched) == 1
        assert sum(r["label"] == label for r in records) == 2
        rc, records, text = run(label + "-fail")
        assert rc == 75 and not launched
        assert agents.take_last_agent_infra_failure() is None
        assert "artifact transfer failed" in text
    print("[OK] transient fetch retries; persistent or partial download failures block the agent")

    with patch("tauceti_worker.retention.pressure_cleanup") as cleanup:
        rc, records, _ = run("disk-full")
        assert rc == 0 and len(launched) == 1
        cleanup.assert_called_once_with()
        assert sum(r["label"] == "TauCeti" for r in records) == 2
    print("[OK] disk pressure cleans once before the existing cache retry")

    rc, records, text = run("miss")
    assert rc == 75 and not launched and len(records) == 1
    print("[OK] uncached main stops instead of silently compiling the entire library")

    rc, records, text = run("rebase")
    assert rc == 0 and len(launched) == 1 and len(records) == 3
    assert records[1]["args"][-4:] == ["--rev", "b" * 40, "--toolchain", "leanprover/lean4:v4.35.0-rc3"]
    assert records[2]["args"][-4:] == ["--rev", "c" * 40, "--toolchain", "leanprover/lean4:v4.35.0-rc3"]
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
    assert rc == 0 and not records
    rc, records, _ = run(LAKE_CACHE_MAX_REVS="0")
    assert rc == 0 and "--max-revs=0" in records[0]["args"]
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

    def prepare(cfg, **_):
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

    # The owned Mathlib archive pool must not resurrect stale private links forever.
    from tauceti_worker import cache_storage

    owned = root / "owned-cache"
    cache_storage.initialize(owned)
    isolated = root / "isolated"
    mathlib = isolated / ".cache/mathlib"
    mathlib.mkdir(parents=True)
    (mathlib / ("a" * 16 + ".ltar")).write_bytes(b"current-tree-archive")

    @contextlib.contextmanager
    def private_preparation(cwd, warm_env, **kwargs):
        yield warm_env

    with patch("tauceti_worker.expanded_cache.preparation", private_preparation):
        overrides = dict(
            TAUCETI_SHARED_ARTIFACTS="1",
            TAUCETI_BUILD_CACHE_ROOT=str(owned),
            HOME=str(isolated),
            MATHLIB_CACHE_DIR=str(mathlib),
        )
        rc, records, text = run(**overrides)
        assert rc == 0, text
        assert sum(r["args"] == ["exe", "cache", "clean"] for r in records) == 1
        assert (owned / "mathlib" / ("a" * 16 + ".ltar")).exists()
        rc, records, text = run(**overrides)
        assert rc == 0, text
        assert not any(r["args"] == ["exe", "cache", "clean"] for r in records)
    print("[OK] Mathlib prunes stale private archive links once daily and records current-tree pool reuse")

    waits = []
    signals = []

    def interrupted_wait(**kwargs):
        waits.append(kwargs)
        if len(waits) == 1:
            raise SystemExit(143)  # RoundContext's SIGTERM handler
        return 0

    proc = SimpleNamespace(
        pid=12345, wait=interrupted_wait, terminate=lambda: signals.append((12345, agents.signal.SIGTERM))
    )
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
    assert waits[1]["timeout"] == 35  # Let the guardian reap detached descendants before the fallback.
    print("[OK] round interruption cleans up the complete downloader process group")

# Without a TauCeti cache service the round must still enter the shared Mathlib view.
with tempfile.TemporaryDirectory() as tmp:
    co = Path(tmp)
    (co / "lakefile.toml").write_text('name = "fixture"\n')
    entered = []

    @contextlib.contextmanager
    def fake_view(cwd, env, logdir, fetch):
        entered.append(cwd)
        yield ["bwrap", "fixture"]

    from tauceti_worker import shared_mathlib

    with (
        patch.object(agents, "TAUCETI_CACHE_SERVICE", ""),
        patch.object(shared_mathlib, "view", fake_view),
    ):
        env = {}
        with agents.host_build_cache_context(co, env, co / "logs") as prefix:
            assert prefix == ["bwrap", "fixture"], prefix
            assert json.loads(env["TAUCETI_SHARED_MATHLIB_VIEW"]) == prefix
        assert "TAUCETI_SHARED_MATHLIB_VIEW" not in env

        @contextlib.contextmanager
        def refused(cwd, env, logdir, fetch):
            raise shared_mathlib.Unavailable("fixture refusal")
            yield

        with patch.object(shared_mathlib, "view", refused):
            try:
                with agents.host_build_cache_context(co, {}, co / "logs"):
                    raise AssertionError("round started without shared Mathlib")
            except agents.BuildCacheUnavailable as error:
                assert "fixture refusal" in str(error)
    assert entered == [co]
print("[OK] a round without the TauCeti cache service still requires the shared Mathlib view")

# A workspace without Mathlib never runs Mathlib's cache executable.
with tempfile.TemporaryDirectory() as tmp:
    co = Path(tmp)
    (co / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "batteries", "url": "x"}]}))
    commands = []

    def record(command, *args, **kwargs):
        commands.append(list(command))
        return 0

    with patch.object(agents, "_cache_fetch", record):
        try:
            agents.warm_host_build_caches(co, {}, co / "logs")
        except Exception:
            pass  # later TauCeti steps need a real workspace; only the Mathlib step matters here
    assert not any(c[-4:] == ["lake", "exe", "cache", "get"] for c in commands), commands
print("[OK] a workspace without Mathlib skips Mathlib's cache fetch")

# A rebase round chooses the shared view on main's pins, and the agent starts on its branch's files.
with tempfile.TemporaryDirectory() as tmp:
    from tauceti_worker import shared_mathlib

    co = Path(tmp)
    git = ["git", "-C", str(co)]
    agents.subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        agents.subprocess.run([*git, "config", key, value], check=True)

    def commit(pin):
        (co / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": pin}]}))
        (co / "lakefile.toml").write_text('name = "fixture"\n')
        agents.subprocess.run([*git, "add", "-A"], check=True)
        agents.subprocess.run([*git, "commit", "-qm", pin], check=True)

    commit("o" * 40)
    agents.subprocess.run([*git, "switch", "-q", "-c", "branch"], check=True)
    agents.subprocess.run([*git, "switch", "-q", "main"], check=True)
    commit("m" * 40)
    agents.subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "main"], check=True)
    agents.subprocess.run([*git, "switch", "-q", "branch"], check=True)
    seen = []

    @contextlib.contextmanager
    def pin_view(cwd, env, logdir, fetch):
        seen.append(shared_mathlib._manifest_pin((cwd / "lake-manifest.json").read_text()))
        yield ["bwrap", "fixture"]

    shared_mathlib.set_round_purpose(co, rebase=True)
    try:
        with patch.object(agents, "TAUCETI_CACHE_SERVICE", ""), patch.object(shared_mathlib, "view", pin_view):
            with agents.host_build_cache_context(co, {}, co / "logs") as prefix:
                assert prefix == ["bwrap", "fixture"]
                assert shared_mathlib._manifest_pin((co / "lake-manifest.json").read_text()) == "o" * 40
    finally:
        shared_mathlib.set_round_purpose(co)
    assert seen == ["m" * 40], seen
    assert agents.subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True).stdout == ""
print("[OK] a rebase round prepares main's Mathlib and hands the agent its own branch")

# Inside a shared Mathlib view the published entry is already a complete `lake exe cache get` result.
# Fetching again re-extracted every archive into the checkout's private overlay upper.
for shared in (True, False):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        commands = []

        def recording(command, cwd, env, output, timeout, commands=commands):
            commands.append(command)
            return 0

        env = {
            "HOME": str(root / "home"),
            "MATHLIB_CACHE_DIR": str(root / "mathlib"),
            "TAUCETI_BUILD_CACHE_ROOT": str(root / "builds"),
        }
        if shared:
            env["TAUCETI_SHARED_MATHLIB_VIEW"] = json.dumps(["bwrap-prefix"])
        with (
            patch.object(agents, "_cache_fetch", side_effect=recording),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                agents.warm_host_build_caches(root, env, root / "logs")
            except Exception:
                pass  # later TauCeti steps need a real workspace; only the Mathlib decision matters here
        mathlib = [c for c in commands if c[-4:] == ["lake", "exe", "cache", "get"]]
        assert not mathlib, (shared, commands)
        if not shared:
            assert not commands, commands  # no shared view: the round stops before any fetch
print("[OK] Mathlib's cache is never fetched into a checkout, shared view or not")

# The agent-facing `lake` wrapper refuses Mathlib cache fetches inside a shared view.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    real = root / "bin"
    real.mkdir()
    (real / "lake").write_text('#!/bin/sh\necho real-lake "$@"\n')
    (real / "lake").chmod(0o755)
    wrapper = Path(__file__).resolve().parents[1] / "scripts"
    co = root / "co"
    marker = co / ".lake/tauceti-mathlib/view.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pool": str(root / "pool"), "key": "k", "rev": "abc"}))

    def lake(*args, view=True, pin="abc", path=None, cwd=None):
        (co / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": pin}]}))
        path = path or f"{wrapper}:{real}"
        env = {**os.environ, "PATH": f"{path}:{os.environ['PATH']}", "TAUCETI_VERIFY_PYTHON": sys.executable}
        env.pop("TAUCETI_SHARED_MATHLIB_VIEW", None)
        if view:
            env["TAUCETI_SHARED_MATHLIB_VIEW"] = json.dumps(["bwrap"])
        return agents.subprocess.run(
            [str(wrapper / "lake"), *args], cwd=cwd or co, env=env, capture_output=True, text=True, timeout=30
        )

    same = lake("exe", "cache", "get")
    assert same.returncode == 0 and "real-lake" not in same.stdout, same
    moved = lake("exe", "cache", "get", pin="def")
    assert moved.returncode == 1 and "pin no longer matches" in moved.stderr, moved
    assert lake("exe", "cache", "get", view=False).stdout.strip() == "real-lake exe cache get"
    assert lake("build", "Foo").stdout.strip() == "real-lake build Foo"
    # On a pin the shared build does not carry (a rebase round before it integrates main),
    # every lake command is refused rather than compiling that Mathlib privately.
    behind = lake("build", "Foo", pin="def")
    assert behind.returncode == 1 and "Integrate origin/main" in behind.stderr and not behind.stdout, behind
    assert lake("--version", pin="def").stdout.strip() == "real-lake --version"
    assert lake("build", "Foo", pin="def", view=False).stdout.strip() == "real-lake build Foo"
    assert lake("exe", "cache", "clean").stdout.strip() == "real-lake exe cache clean"
    # Dependency resolution belongs to the worker: the shared sources are read-only.
    update = lake("update")
    assert update.returncode == 1 and "only the worker resolves" in update.stderr and not update.stdout, update
    assert lake("update", view=False).stdout.strip() == "real-lake update"
    # Every spelling of the extraction is refused: options, aliases, qualified names, forced unpack.
    for args in (
        ("-q", "exe", "cache", "get"),
        ("exe", "-q", "cache", "get"),
        ("exec", "cache", "get"),
        ("exe", "mathlib/cache", "get"),
        ("+leanprover/lean4:v4.36.0", "exe", "cache", "get!"),
        ("exe", "cache", "unpack!"),
        ("exe", "cache", "--repo=x/y", "get"),
        ("--log-level", "info", "exe", "cache", "unpack"),
        ("--config", "cfg.json", "exe", "cache", "get"),
        ("--from-export", "x.json", "exe", "cache", "get"),
        ("--unknown-value", "v", "exe", "cache", "get"),
        ("", "exe", "cache", "get"),
        ("-d=.", "exe", "cache", "get"),
        ("-d .", "exe", "cache", "get"),
    ):
        refused = lake(*args)
        assert refused.returncode == 0 and "real-lake" not in refused.stdout, (args, refused)
    # `-d DIR` selects the workspace whose pin is checked, wherever lake is run from.
    remote = lake("-d", str(co), "exe", "cache", "get", cwd=root)
    assert remote.returncode == 0 and "real-lake" not in remote.stdout, remote
    elsewhere = lake("exe", "cache", "get", cwd=root)
    assert elsewhere.returncode == 1, elsewhere
    # `lake env CMD` puts the wrapper back in front of the toolchain's own lake.
    (real / "lake").write_text(
        '#!/bin/sh\ncase " $* " in *" env "*) while [ "$1" != env ]; do shift; done; shift;'
        ' PATH="' + str(real) + ':$PATH" exec "$@";; esac\necho real-lake "$@"\n'
    )
    nested = lake("env", "lake", "exe", "cache", "get")
    assert nested.returncode == 0 and "real-lake" not in nested.stdout, nested
    assert lake("env", "lake", "build").stdout.strip() == "real-lake build"
    assert lake("--config", "c.json", "env", "lake", "build").stdout.strip() == "real-lake build"
    assert lake("env", "", "lake", "build").stdout.strip() == "real-lake build"
    # Aliases of the wrapper's own directory never make it exec itself.
    for alias in (f"{wrapper}/", f"{wrapper}/.", "scripts"):
        looped = lake("build", path=f"{alias}:{real}", view=False, cwd=wrapper.parent if alias == "scripts" else None)
        assert looped.stdout.strip() == "real-lake build", (alias, looped)
print("[OK] the agent's lake wrapper refuses Mathlib cache fetches inside a shared view")
