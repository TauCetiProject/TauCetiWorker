#!/usr/bin/env python3
"""Bubble work rounds warm Mathlib's cache and TauCeti's Lake cache before launching the agent.

The review/probe path supplies its own command and must remain untouched: it does not compile and should
not gain project-cache egress. Exit 0 = the bootstrap/config/argv contract agrees; 1 = a mismatch.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


inner = "env OPENAI_API_KEY= agent --do-work"
bootstrap = tc.bubble_work_cmd(inner)
mathlib_i = bootstrap.index("lake exe cache get")
tauceti_i = bootstrap.index("lake cache get")
build_i = bootstrap.index("lake build")
agent_i = bootstrap.index("exec " + inner)
check("cache/build/agent order", mathlib_i < tauceti_i < build_i < agent_i)
check("TauCeti cache uses the canonical repository", f"--repo {tc.TAUCETI}" in bootstrap)
check("Bubble, not the bootstrap, supplies Lake config", "LAKE_CONFIG" not in bootstrap)
check("Bubble, not the bootstrap, supplies Lake restore settings", "LAKE_RESTORE_ARTIFACTS" not in bootstrap)

saved_help = tc.agents._bubble_open_help
try:
    tc.agents._bubble_open_help = lambda: "  --lake-cache-service NAME ARTIFACT_URL REVISION_URL"
    check("exact Bubble cache flag is detected", tc.bubble_supports_lake_cache_service())
    tc.agents._bubble_open_help = lambda: "  --lake-cache-service-url URL"
    check("similarly prefixed Bubble flag is not accepted", not tc.bubble_supports_lake_cache_service())
finally:
    tc.agents._bubble_open_help = saved_help


# Execute the bootstrap against a fake `lake`, so the tests pin the failure semantics rather than just
# matching shell text. A red preliminary build must still launch the repair agent; a Mathlib-cache
# outage must not. The fake records each invocation so the one retry is also observable.
shimdir = Path(tempfile.mkdtemp())
marker = shimdir / "agent-ran"
calls = shimdir / "lake-calls"
lake = shimdir / "lake"
lake.write_text(
    "#!/bin/sh\n"
    'printf "%s\\n" "$*" >> "$LAKE_CALLS"\n'
    'case "$*" in\n'
    '  "exe cache get") exit "${MATHLIB_RC:-0}" ;;\n'
    '  cache\\ get*) [ -n "$TAUCETI_MSG" ] && printf "%s\\n" "$TAUCETI_MSG"; exit "${TAUCETI_RC:-0}" ;;\n'
    '  build) exit "${BUILD_RC:-0}" ;;\n'
    "esac\n"
    "exit 99\n"
)
lake.chmod(0o755)


def run_bootstrap(*, mathlib=0, tauceti=0, build=0, tauceti_msg=""):
    marker.unlink(missing_ok=True)
    calls.unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": f"{shimdir}:{os.environ.get('PATH', '')}",
        "LAKE_CALLS": str(calls),
        "MATHLIB_RC": str(mathlib),
        "TAUCETI_RC": str(tauceti),
        "TAUCETI_MSG": tauceti_msg,
        "BUILD_RC": str(build),
        "MARKER": str(marker),
    }
    cmd = tc.bubble_work_cmd("sh -c 'touch \"$MARKER\"'")
    result = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True)
    return result, calls.read_text().splitlines()


result, lake_calls = run_bootstrap(mathlib=1)
check("Mathlib cache failure blocks the agent", result.returncode != 0 and not marker.exists())
check("Mathlib cache gets one retry", lake_calls == ["exe cache get", "exe cache get"])

result, _ = run_bootstrap(tauceti=1)
check("TauCeti cache miss still launches the agent", result.returncode == 0 and marker.exists())

result, _ = run_bootstrap(build=1)
check("red preliminary build still launches the repair agent", result.returncode == 0 and marker.exists())
check("red preliminary build emits a warning", "agent starts from a red tree" in result.stderr)

# The two reasons a TauCeti fetch comes back empty must not read alike. "No outputs for this revision"
# is the ordinary case on a commit main has not built yet. Anything else means the cache did not answer,
# which is broken infrastructure; it used to print the same line and rebuild the library from source
# every round for as long as the endpoint stayed down.
result, lake_calls = run_bootstrap(tauceti=1, tauceti_msg="error: no outputs found for revision abc123")
check("a cold revision still launches the agent", result.returncode == 0 and marker.exists())
check("a cold revision is reported as cold", "holds no outputs for this revision" in result.stderr)
check("a cold revision is not reported as an endpoint failure", "did not answer" not in result.stderr)
check(
    "a cold revision is not retried", lake_calls.count("cache get --service tauceti-public --repo " + tc.TAUCETI) == 1
)

result, lake_calls = run_bootstrap(tauceti=1, tauceti_msg="curl: (22) The requested URL returned 401")
check("an unreachable cache still launches the agent", result.returncode == 0 and marker.exists())
check("an unreachable cache is reported as a failure", "did not answer" in result.stderr)
check("an unreachable cache is not reported as cold", "holds no outputs" not in result.stderr)
check("an unreachable cache echoes the fetch log", "cache: curl: (22)" in result.stderr)
check(
    "an unreachable cache is retried once",
    lake_calls.count("cache get --service tauceti-public --repo " + tc.TAUCETI) == 2,
)

shutil.rmtree(shimdir, ignore_errors=True)


# The cache is reached over the custom domain, not the bucket's `pub-<id>.r2.dev` development URL.
# That URL was disabled and answered 401 for every path, so `lake cache get` failed on every round.
check("cache uses the custom domain", tc.TAUCETI_CACHE_DOMAIN == "cache.taucetiproject.org")
check("cache does not use the r2.dev development URL", "r2.dev" not in tc.TAUCETI_CACHE_ARTIFACT_URL)


# A bucket with public access switched off answers 401/403 for every path, root included, so no revision
# can ever be found through it. A healthy bucket answers 404 for a path holding no object. Only the
# former is a misconfiguration; the latter is what the probe's own URL returns when all is well.
import urllib.error


def _probe_with(exc):
    tc.agents.tauceti_cache_unreachable_reason.cache_clear()
    import urllib.request as ur

    real = ur.urlopen
    ur.urlopen = lambda *a, **k: (_ for _ in ()).throw(exc)
    try:
        return tc.agents.tauceti_cache_unreachable_reason()
    finally:
        ur.urlopen = real
        tc.agents.tauceti_cache_unreachable_reason.cache_clear()


def _http_error(code):
    return urllib.error.HTTPError(tc.TAUCETI_CACHE_REVISION_URL, code, "Unauthorized", {}, None)


check("401 is reported as unreadable", "401" in (_probe_with(_http_error(401)) or ""))
check("403 is reported as unreadable", "403" in (_probe_with(_http_error(403)) or ""))
check("404 is healthy", _probe_with(_http_error(404)) is None)
check("a network fault does not block the round", _probe_with(urllib.error.URLError("offline")) is None)


# Cache isolation is load-bearing: a failed Bubble setting command must not leave a sentinel that makes
# later rounds assume overlay mode. Conversely, an already-verified config needs no subprocess.
cache_home = Path(tempfile.mkdtemp())
saved_bubble_cmd = tc.agents.bubble_cmd
old_bubble_home = os.environ.get("TAUCETI_BUBBLE_HOME")
os.environ["TAUCETI_BUBBLE_HOME"] = str(cache_home)
cache_cfg = SimpleNamespace(home=cache_home, wid="cache-test")
try:
    tc.agents.bubble_cmd = lambda: ["false"]
    try:
        tc.ensure_bubble_home(cache_cfg)
        blocked = False
    except tc.Die:
        blocked = True
    check("failed overlay configuration blocks the round", blocked)
    check("failed overlay configuration writes no trust sentinel", not (cache_home / ".worker-init").exists())

    (cache_home / "config.toml").write_text('[security]\nshared_cache = "overlay"\n')
    tc.agents.bubble_cmd = lambda: (_ for _ in ()).throw(AssertionError("unexpected Bubble subprocess"))
    env = tc.ensure_bubble_home(cache_cfg)
    check("verified overlay configuration is accepted directly", env["BUBBLE_HOME"] == str(cache_home))
finally:
    tc.agents.bubble_cmd = saved_bubble_cmd
    if old_bubble_home is None:
        os.environ.pop("TAUCETI_BUBBLE_HOME", None)
    else:
        os.environ["TAUCETI_BUBBLE_HOME"] = old_bubble_home
    shutil.rmtree(cache_home, ignore_errors=True)


# Exercise run_in_bubble through its no-side-effect echo path to pin which rounds get the cache
# capability and bootstrap. The normal credential mirror/model launch happens after this early return.
tmp = Path(tempfile.mkdtemp())
cfg = SimpleNamespace(
    state=tmp / "state",
    home=tmp / "home",
    wid="cache-test",
    logdir=tmp / "logs",
)
w = SimpleNamespace(cfg=cfg)
opts = SimpleNamespace(work_model="claude")
saved_home = tc.agents.ensure_bubble_home
saved_pop = tc.agents._bubble_pop
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
os.environ["TAUCETI_AGENT_ECHO"] = "1"
try:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "PROMPT", opts)
    work_argv = out.getvalue()
    check("work echo succeeds", rc == 0)
    cache_grant = (
        f"--lake-cache-service {tc.TAUCETI_CACHE_SERVICE} "
        f"{tc.TAUCETI_CACHE_ARTIFACT_URL} {tc.TAUCETI_CACHE_REVISION_URL}"
    )
    check("work round grants the TauCeti download cache", cache_grant in work_argv)
    check("work round does not expose the upstream domain directly", "--allow-domain" not in work_argv)
    check("work round runs both cache commands", "lake exe cache get" in work_argv and "lake cache get" in work_argv)
    check("work round stages no competing Lake config", not (cfg.state / "bubble-round" / "lake-cache.toml").exists())

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "", opts, inner_cmd="true", cred_model="claude")
    review_argv = out.getvalue()
    check("review/probe echo succeeds", rc == 0)
    check("review/probe does not add a cache capability", "--lake-cache-service" not in review_argv)
    check("review/probe command is not wrapped in a build", "lake exe cache get" not in review_argv)
finally:
    os.environ.pop("TAUCETI_AGENT_ECHO", None)
    tc.agents.ensure_bubble_home = saved_home
    tc.agents._bubble_pop = saved_pop
    shutil.rmtree(tmp, ignore_errors=True)


# A real work round must fail in preflight, before a model is launched, when the installed Bubble lacks
# the host-global Lake cache capability. Review-only rounds intentionally do not need it.
saved_have = tc.cli._have
saved_disposable = tc.cli.bubble_cmd_is_disposable
saved_version = tc.cli.installed_bubble_version
saved_push = tc.cli.bubble_supports_allow_push
saved_cache = tc.cli.bubble_supports_lake_cache_service
saved_proxy = tc.cli.ensure_fork_proxy_current
try:
    tc.cli._have = lambda _tool: True
    tc.cli.bubble_cmd_is_disposable = lambda: False
    tc.cli.installed_bubble_version = lambda: tc.BUBBLE_MIN_VERSION
    tc.cli.bubble_supports_allow_push = lambda: True
    tc.cli.bubble_supports_lake_cache_service = lambda: False
    tc.cli.ensure_fork_proxy_current = lambda: (_ for _ in ()).throw(
        AssertionError("cache capability failure must precede auth-proxy refresh")
    )
    work_opts = tc.RoundOpts(only=["fix"], agent="claude", work_model="claude", sandbox_host=False, dry_run=False)
    try:
        tc.cli.preflight(SimpleNamespace(), work_opts)
        cache_blocked = False
    except tc.Die as exc:
        cache_blocked = "--lake-cache-service" in str(exc)
    check("preflight rejects work rounds without the cache capability", cache_blocked)
finally:
    tc.cli._have = saved_have
    tc.cli.bubble_cmd_is_disposable = saved_disposable
    tc.cli.installed_bubble_version = saved_version
    tc.cli.bubble_supports_allow_push = saved_push
    tc.cli.bubble_supports_lake_cache_service = saved_cache
    tc.cli.ensure_fork_proxy_current = saved_proxy

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
