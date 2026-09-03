#!/usr/bin/env python3
"""Fork-based PR authoring (kim-em/bubble#320 + TauCetiWorker fork migration).

The worker authors and fixes from the contributor's OWN fork: the branch is pushed there and the PR
is opened from it, so no canonical write access is needed (Bryan's report — a read-only account could
not land roadmap work). This harness pins three pure-ish decisions without touching GitHub or bubble:

  1. `ensure_fork()` resolves the fork BY PARENT (not by name), honors `$TAUCETI_FORK`, creates one
     when absent, and fails closed if a same-named NON-fork squats the name.
  2. `_do_fixlike` skips a tended PR whose head repo was deleted (empty head fields) instead of
     building a `https://github.com//` remote, and otherwise hands bubble the PR's head repo as the
     fork to allow git fetch/push to.
  3. `do_roadmap` points the push at the fork, passes `--allow-push <fork>` to bubble while keeping the
     bubble TARGET canonical, and substitutes the fork owner + worker id into the prompt's `--head`.
  4. `do_roadmap` exports `CLAIM_REPO` = the worker's claim namespace in both modes, so the agent's
     own `claim.sh acquire author/...` lands somewhere it can push rather than erroring on canonical.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

TAUCETI = tc.constants.TAUCETI  # "TauCetiProject/TauCeti"
FORK = "alice/TauCeti"
CLAIM_NS = "TauCetiProject/tauceti-claims"
fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=out, stderr="")


def _repo_list_json(parent_owner, parent_name, name_with_owner=FORK):
    return f'[{{"nameWithOwner":"{name_with_owner}","parent":{{"name":"{parent_name}","owner":{{"login":"{parent_owner}"}}}}}}]'


# ---- 1. ensure_fork() -------------------------------------------------------------------------
def fake_gh(scenario):
    """A `gh_run` stand-in driven by `scenario` (a mutable dict): dispatch on the gh subcommand."""

    def run(argv, **kw):
        if argv[1:3] == ["repo", "list"]:
            return _cp(0, scenario["repo_list"]())
        if argv[1:3] == ["repo", "fork"]:
            scenario["forked"] = True
            return _cp(0, "")
        if argv[1] == "api" and ".fork" in argv:  # the same-name clash probe
            return _cp(0, scenario.get("clash_fork", "true"))
        if argv[1] == "api" and ".permissions.push" in argv:  # can_push probe on the fork
            return _cp(0, scenario.get("can_push", "true"))
        return _cp(0, "")

    return run


def run_ensure_fork(scenario, env_fork=None):
    tc.github.ensure_fork.cache_clear()
    tc.github.gh_run = fake_gh(scenario)
    tc.github.me = lambda: "alice"
    old = os.environ.pop("TAUCETI_FORK", None)
    if env_fork is not None:
        os.environ["TAUCETI_FORK"] = env_fork
    try:
        return tc.github.ensure_fork()
    finally:
        os.environ.pop("TAUCETI_FORK", None)
        if old is not None:
            os.environ["TAUCETI_FORK"] = old


def test_ensure_fork():
    # existing fork resolved by parent (a same-named repo with a DIFFERENT parent must not match)
    sc = {"repo_list": lambda: _repo_list_json("TauCetiProject", "TauCeti")}
    check("ensure_fork: existing fork resolved by parent", run_ensure_fork(sc) == FORK)

    # resolve-by-parent: a same-named repo whose parent is someone ELSE is not our fork
    tc.github.gh_run = fake_gh({"repo_list": lambda: _repo_list_json("SomeoneElse", "TauCeti", "alice/TauCeti")})
    check("ensure_fork: wrong-parent same-name not matched", tc.github._find_fork() is None)

    # no fork yet -> create, then resolve (the list flips to the real fork after `gh repo fork`)
    sc = {
        "forked": False,
        "repo_list": lambda: _repo_list_json("TauCetiProject", "TauCeti") if sc.get("forked") else "[]",
    }
    check("ensure_fork: absent -> create -> resolve", run_ensure_fork(sc) == FORK)

    # a same-named NON-fork squats the name -> Die
    sc = {"forked": False, "repo_list": lambda: "[]", "clash_fork": "false"}
    try:
        run_ensure_fork(sc)
        check("ensure_fork: same-named non-fork -> Die", False)
    except tc.Die:
        check("ensure_fork: same-named non-fork -> Die", True)

    # $TAUCETI_FORK override wins with no repo-list call
    sc = {"repo_list": lambda: (_ for _ in ()).throw(AssertionError("should not list"))}
    check("ensure_fork: $TAUCETI_FORK override", run_ensure_fork(sc, env_fork="bob/MyTauCeti") == "bob/MyTauCeti")

    # fork resolves but the account can't push to it -> Die (explicit false only; None fails open)
    sc = {"repo_list": lambda: _repo_list_json("TauCetiProject", "TauCeti"), "can_push": "false"}
    try:
        run_ensure_fork(sc)
        check("ensure_fork: unpushable fork -> Die", False)
    except tc.Die:
        check("ensure_fork: unpushable fork -> Die", True)


# ---- 2. _do_fixlike: deleted-head guard + fork allow_push -------------------------------------
def test_fixlike():
    pr_dead = types.SimpleNamespace(number=5, head_owner="", head_repo="", head_ref="", head="abc")
    sv = types.SimpleNamespace(open_prs=[pr_dead])
    c = types.SimpleNamespace(pr=5, head="abc")
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude")
    called = []
    tc.work_units.run_in_bubble = lambda *a, **k: called.append(k) or 0
    w = types.SimpleNamespace(claims=types.SimpleNamespace(begin_branch_work=lambda *a: True))
    rc = tc.work_units._do_fixlike(w, sv, c, opts, True, prompt_file="fix.md", label="fix")
    check("fixlike: deleted head -> skip (None, no bubble)", rc is None and not called)

    # valid fork head -> bubble gets allow_push=<head owner/repo>, target the PR
    pr_ok = types.SimpleNamespace(number=7, head_owner="alice", head_repo="TauCeti", head_ref="roadmap/x", head="dead")
    sv = types.SimpleNamespace(open_prs=[pr_ok])
    c = types.SimpleNamespace(pr=7, head="dead")
    cap = {}
    tc.work_units.run_in_bubble = lambda w, target, prompt, opts, **k: cap.update(target=target, **k) or 0
    w = types.SimpleNamespace(
        claims=types.SimpleNamespace(begin_branch_work=lambda *a: True),
        rs=types.SimpleNamespace(bust=lambda *a: None),
    )
    tc.work_units._do_fixlike(w, sv, c, opts, True, prompt_file="fix.md", label="fix")
    check("fixlike: fork head -> allow_push=owner/repo", cap.get("allow_push") == "alice/TauCeti")
    check("fixlike: target is the PR", cap.get("target") == f"{TAUCETI}/pull/7")


# ---- 3. do_roadmap: fork push remote + --allow-push + prompt --head --------------------------
def test_roadmap():
    tmp = Path(tempfile.mkdtemp(prefix="fork-test-"))
    source = tmp / "source-material"
    source.mkdir()
    os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"  # avoid an intentions-board network call
    os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ.pop("TAUCETI_PUSH_EXPECT", None)
    os.environ["TAUCETI_PUSH_EXPECT"] = "stale"  # must be popped by do_roadmap (create-only on the fork)
    tc.work_units.ensure_fork = lambda: FORK
    os.environ.pop("CLAIM_REPO", None)
    tc.work_units.claims_repo = lambda: CLAIM_NS
    tc.work_units.administrative_hold_avoid_list = lambda *_args: "none"

    # A real review checkout, so the round exercises the BUNDLED path rather than the fallback:
    # stage_rubrics only produces a bundle when it finds rubrics to concatenate.
    def fake_fetch_ref(repo, dest):
        if repo == tc.constants.REVIEW:
            (Path(dest) / "rubrics").mkdir(parents=True, exist_ok=True)
            (Path(dest) / "rubrics" / "_common.md").write_text("SHARED PROTOCOL\n")
            (Path(dest) / "rubrics" / "api-design.md").write_text("ANGLE api-design\n")
        return True

    tc.work_units.fetch_ref = fake_fetch_ref
    saved_fetch_source = tc.work_units.fetch_git_source
    materialized = {}

    def fake_fetch_source(git_source, dest):
        materialized.update(source=git_source, dest=dest)
        return True

    tc.work_units.fetch_git_source = fake_fetch_source
    cap = {}
    tc.work_units.run_in_bubble = lambda w, target, prompt, opts, **k: (
        cap.update(target=target, prompt=prompt, **k) or 0
    )
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=tmp, wid="worker3", checkout=tmp / "checkout", logdir=tmp / "logs"), gh=None
    )
    c = types.SimpleNamespace(reason="Topology", pr=0, head="")
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude", source=str(source))
    tc.work_units.do_roadmap(w, None, c, opts, bubble=True)

    check("roadmap: bubble target stays canonical", cap.get("target") == TAUCETI)
    check("roadmap: --allow-push is the fork", cap.get("allow_push") == FORK)
    check("roadmap: push remote is the fork URL", os.environ.get("TAUCETI_PUSH_REMOTE") == f"https://github.com/{FORK}")
    check("roadmap: PUSH_EXPECT popped (create-only)", "TAUCETI_PUSH_EXPECT" not in os.environ)
    check("roadmap: bubble agent's claim goes to the claim namespace", os.environ.get("CLAIM_REPO") == CLAIM_NS)
    prompt = cap.get("prompt", "")
    check("roadmap: prompt has --head <forkowner>:", "--head alice:roadmap/" in prompt)
    check("roadmap: prompt carries the worker id", "worker3" in prompt)
    check(
        "roadmap: no unsubstituted placeholders",
        "__FORK__" not in prompt and "__WORKERID__" not in prompt and "__SOURCE_GUIDANCE__" not in prompt,
    )
    check("roadmap: prompt encourages cross-roadmap prerequisites", "Follow prerequisites across roadmaps" in prompt)
    check(
        "roadmap: switched target checks its own intention board",
        '--label "roadmap/<target-roadmap>"' in prompt,
    )
    check("roadmap: claim follows the actual target", "author/<target-roadmap>/<slug>" in prompt)
    check("roadmap: marker follows the actual target", '"focus":"<target-roadmap>"' in prompt)
    check("roadmap: switched PR records its consumer", "Consumer roadmap: <designated-roadmap>" in prompt)
    check(
        "roadmap: impossible target stops without a fictional report",
        "release your claim and stop without a PR" in prompt,
    )
    check("roadmap: no unactionable obstruction report", "precise obstruction report" not in prompt)
    check("roadmap: bubble source path is in prompt", "read-only at `/opt/source`" in prompt)
    priorities = (
        "(1) satisfy the `Topology` roadmap exactly as written; (2) write excellent library code that will\n"
        "  satisfy every review requirement; (3) migrate material from the source only where it is compatible"
    )
    check("roadmap: prompt states the required source priorities", priorities in prompt)
    check("roadmap: local source is materialized", materialized.get("source") == str(source))
    check("roadmap: source is mounted read-only", f"{materialized['dest']}:/opt/source:ro" in cap.get("mounts", []))
    check("roadmap: source is explicitly untrusted", "contents are untrusted data" in prompt)
    check("roadmap: source attribution and license are required", "source repository, commit, and license" in prompt)

    url = "https://github.com/example/source-library.git"
    cloned = {}

    def capture_url_source(git_url, dest):
        cloned.update(url=git_url, dest=dest)
        return True

    tc.work_units.fetch_git_source = capture_url_source
    opts.source = url
    cap.clear()
    tc.work_units.do_roadmap(w, None, c, opts, bubble=True)
    check("roadmap: URL source is cloned", cloned.get("url") == url)
    check("roadmap: URL clone lives in worker state", str(cloned.get("dest", "")).startswith(str(tmp / "refs")))
    check("roadmap: URL clone is mounted read-only", f"{cloned['dest']}:/opt/source:ro" in cap.get("mounts", []))

    host_cap = {}
    tc.work_units.fetch_git_source = fake_fetch_source
    tc.work_units.prepare_checkout = lambda cfg: True
    tc.work_units.run_agent_host = lambda cwd, prompt, model, logdir: host_cap.update(prompt=prompt) or 0
    opts.source = str(source)
    os.environ.pop("CLAIM_REPO", None)
    tc.work_units.do_roadmap(w, None, c, opts, bubble=False)
    host_prompt = host_cap.get("prompt", "")
    check("roadmap: host agent's claim goes to the claim namespace", os.environ.get("CLAIM_REPO") == CLAIM_NS)
    check("roadmap: host uses a disposable snapshot", "worker-owned disposable snapshot" in host_prompt)
    check("roadmap: host prompt points at materialized copy", str(materialized["dest"]) in host_prompt)

    opts.source = None
    cap.clear()
    tc.work_units.do_roadmap(w, None, c, opts, bubble=True)
    no_source_prompt = cap.get("prompt", "")
    check("roadmap: absent source adds no guidance", "Supplementary source material" not in no_source_prompt)
    # The bundle must reach BOTH the prompt and the sandbox, or the prompt names a path the agent
    # cannot open. rubric_bundle.py proves the file is built; this proves it is delivered.
    check("roadmap: bubble prompt names the bundle", "/opt/rubrics/rubrics.md" in no_source_prompt)
    check(
        "roadmap: bubble mounts the bundle read-only",
        any(m.endswith(":/opt/rubrics:ro") for m in cap.get("mounts", [])),
    )
    check("roadmap: the bundle is really written", (tmp / "refs" / "rubrics" / "rubrics.md").is_file())
    # __SOURCE_GUIDANCE__ sits between two bullets, so an absent source must leave them contiguous
    # rather than opening a hole in the list.
    check(
        "roadmap: absent source keeps one bullet list",
        "rubrics you read.\n- Before writing" in no_source_prompt,
    )
    tc.work_units.fetch_git_source = saved_fetch_source


# ---- 4. ensure_fork_proxy_current: version-gated auth-proxy daemon restart -------------------
def test_proxy_current():
    tmp = Path(tempfile.mkdtemp(prefix="fork-proxy-"))
    real_health = tc.agents._bubble_proxy_endpoint_healthy
    real_wait = tc.agents._wait_bubble_proxy_endpoint_healthy
    real_mtime = tc.agents._bubble_proxy_endpoint_mtime
    real_lock = tc.agents._auth_proxy_lock_path
    real_cmd = tc.agents.bubble_cmd
    healthy = {"value": True}
    tc.agents._bubble_proxy_endpoint_healthy = lambda **_kwargs: healthy["value"]
    tc.agents._wait_bubble_proxy_endpoint_healthy = lambda **_kwargs: healthy["value"]
    tc.agents._bubble_proxy_endpoint_mtime = lambda: 123
    tc.agents._auth_proxy_lock_path = lambda: tmp / "auth-proxy.lock"
    tc.agents.bubble_cmd = lambda: ["/stable/bin/bubble"]

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _cp(0, "")

    def started():
        return any(a[-3:] == ["gh", "proxy", "start"] for a in calls)

    real_run = tc.agents.subprocess.run
    tc.agents.subprocess.run = fake_run
    try:
        # A healthy capable endpoint needs no daemon churn.
        tc.agents.ensure_fork_proxy_current()
        check("proxy: healthy capable endpoint -> no restart", not started())

        # A dead or pre-capability endpoint is refreshed.
        calls.clear()
        healthy["value"] = False

        def restart_and_heal(argv, **kw):
            calls.append(argv)
            if argv[-3:] == ["gh", "proxy", "start"]:
                healthy["value"] = True
            return _cp(0, "bubble, version 0.7.27\n")

        tc.agents.subprocess.run = restart_and_heal
        tc.agents.ensure_fork_proxy_current()
        check("proxy: dead endpoint -> gh proxy start", started())

        # Restart failure is fail-CLOSED and includes Bubble's useful diagnostic.
        healthy["value"] = False

        def boom(argv, **kw):
            raise subprocess.CalledProcessError(1, argv, stderr="launchd exploded")

        tc.agents.subprocess.run = boom
        try:
            tc.agents.ensure_fork_proxy_current()
            check("proxy: restart failure -> Die", False)
        except tc.Die:
            check("proxy: restart failure -> Die", True)
        try:
            tc.agents.ensure_fork_proxy_current()
        except tc.Die as exc:
            check("proxy: restart diagnostic is preserved", "launchd exploded" in str(exc))
        else:
            check("proxy: restart diagnostic is preserved", False)

        # A zero exit from Bubble must still fail closed if no reachable endpoint appears.
        calls.clear()
        tc.agents.subprocess.run = fake_run
        healthy["value"] = False
        try:
            tc.agents.ensure_fork_proxy_current()
            check("proxy: successful command + dead endpoint -> Die", False)
        except tc.Die:
            check("proxy: successful command + dead endpoint -> Die", True)

        # Never install a host-global service from uvx's disposable tool cache.
        calls.clear()
        tc.agents.bubble_cmd = lambda: ["uvx", "--from", "dev-bubble", "bubble"]
        try:
            tc.agents.ensure_fork_proxy_current()
            check("proxy: uvx daemon path -> Die", False)
        except tc.Die:
            check("proxy: uvx daemon path -> Die", True)
        check("proxy: uvx rejection happens before start", not started())
        tc.agents.bubble_cmd = lambda: ["/stable/bin/bubble"]
    finally:
        tc.agents.subprocess.run = real_run
        tc.agents._bubble_proxy_endpoint_healthy = real_health
        tc.agents._wait_bubble_proxy_endpoint_healthy = real_wait
        tc.agents._bubble_proxy_endpoint_mtime = real_mtime
        tc.agents._auth_proxy_lock_path = real_lock
        tc.agents.bubble_cmd = real_cmd


def test_proxy_endpoint_health():
    import json

    tmp = Path(tempfile.mkdtemp(prefix="fork-proxy-endpoint-"))
    endpoint_file = tmp / ".bubble" / "auth-proxy.endpoint"
    endpoint_file.parent.mkdir(parents=True)
    real_home = tc.agents._host_home
    tc.agents._host_home = lambda: tmp
    try:
        check("proxy endpoint: missing file is unhealthy", not tc.agents._bubble_proxy_endpoint_healthy())
        endpoint_file.write_text("not json")
        check("proxy endpoint: malformed JSON is unhealthy", not tc.agents._bubble_proxy_endpoint_healthy())
        endpoint_file.write_text(
            json.dumps(
                {
                    "tcp": {"host": "127.0.0.1", "port": True},
                    "version": 3,
                    "capabilities": ["allow-push"],
                    "pid": os.getpid(),
                }
            )
        )
        check("proxy endpoint: bool port is unhealthy", not tc.agents._bubble_proxy_endpoint_healthy())

        host, port = "127.0.0.1", 7654
        endpoint_file.write_text(json.dumps({"tcp": {"host": host, "port": port}, "version": 3, "pid": os.getpid()}))
        check(
            "proxy endpoint: missing fork capability is unhealthy",
            not tc.agents._bubble_proxy_endpoint_healthy(),
        )
        endpoint_file.write_text(
            json.dumps(
                {
                    "tcp": {"host": host, "port": port},
                    "version": 3,
                    "bubble_version": "0.7.27",
                    "capabilities": ["allow-push"],
                    "pid": os.getpid(),
                }
            )
        )
        mtime = endpoint_file.stat().st_mtime_ns
        check("proxy endpoint: live capable listener is healthy", tc.agents._bubble_proxy_endpoint_healthy())
        check(
            "proxy endpoint: matching Bubble version is healthy",
            tc.agents._bubble_proxy_endpoint_healthy(expected_version="0.7.27"),
        )
        check(
            "proxy endpoint: Bubble upgrade requires refresh",
            not tc.agents._bubble_proxy_endpoint_healthy(expected_version="0.7.28"),
        )
        check(
            "proxy endpoint: restart requires a freshly written endpoint",
            not tc.agents._bubble_proxy_endpoint_healthy(newer_than=mtime),
        )
    finally:
        tc.agents._host_home = real_home


def test_proxy_endpoint_waits_for_startup():
    real_health = tc.agents._bubble_proxy_endpoint_healthy
    probes = iter([False, False, True])
    tc.agents._bubble_proxy_endpoint_healthy = lambda **_kwargs: next(probes)
    try:
        check(
            "proxy endpoint: startup settle window retries",
            tc.agents._wait_bubble_proxy_endpoint_healthy(timeout=2),
        )
    finally:
        tc.agents._bubble_proxy_endpoint_healthy = real_health


def test_disposable_bubble_commands():
    check("bubble command: uvx is disposable", tc.agents.bubble_cmd_is_disposable(["uvx", "bubble"]))
    check(
        "bubble command: uv tool run is disposable",
        tc.agents.bubble_cmd_is_disposable(["uv", "tool", "run", "bubble"]),
    )
    check(
        "bubble command: installed executable is stable",
        not tc.agents.bubble_cmd_is_disposable(["/opt/tools/bin/bubble"]),
    )


def main():
    test_ensure_fork()
    test_fixlike()
    test_roadmap()
    test_proxy_current()
    test_proxy_endpoint_health()
    test_proxy_endpoint_waits_for_startup()
    test_disposable_bubble_commands()
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
