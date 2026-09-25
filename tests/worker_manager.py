#!/usr/bin/env python3
"""Integration checks for declarative configuration and portable worker reconciliation."""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

root = Path(tempfile.mkdtemp(prefix="tauceti-workers-test-"))
for key in (
    "TAUCETI_CONFIG_HOME",
    "TAUCETI_WORKERS_CONFIG",
    "TAUCETI_WORKERS_STATE_DIR",
    "TAUCETI_RUNTIME_DIR",
):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["TAUCETI_RUNTIME_DIR"] = str(root / "run")
os.environ["TAUCETI_MANAGER_TEST_COMMAND"] = shlex.join(
    [sys.executable, "-c", "import os; os.read(int(os.environ['TAUCETI_PARENT_PIPE_FD']), 1)"]
)

import tauceti_worker.paths as worker_paths
import tauceti_worker.worker_manager as wm
from tauceti_worker.cli import build_parser
from tauceti_worker.runtime_status import report_failure, report_runtime


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def start_manager(config):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tauceti_worker",
            "workers",
            "--config",
            str(config),
            "manager",
            "--interval",
            "0.05",
        ],
        cwd=REPO,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


config = wm.default_workers_config()
assert config.is_relative_to(root), f"test configuration escaped its temporary root: {config}"
specs = [
    wm.WorkerSpec(id="worker1"),
    wm.WorkerSpec(id="worker2", agent="codex", only=("review",)),
    wm.WorkerSpec(id="worker3", only=("roadmap",), roadmap_only="RepresentationTheory"),
]
manager = None
try:
    wm.save_worker_specs(config, specs)
    assert wm.load_worker_specs(config) == specs

    # Bare `tauceti workers` is the documented shorthand for `workers status`.
    bare_workers = build_parser().parse_args(["workers"])
    assert bare_workers.workers_action is None
    assert bare_workers.json is False and bare_workers.watch is False

    runtime_probe = root / "runtime-probe.json"
    os.environ["TAUCETI_RUNTIME_STATUS"] = str(runtime_probe)
    report_runtime("waiting-quota", detail="test", next_action_at=123)
    assert wm.read_json(runtime_probe)["state"] == "waiting-quota"
    report_failure("claude agent: API Error 529 Overloaded", code=1, log_file="/tmp/agent.log")
    failure = wm.read_json(runtime_probe)
    assert failure["failure_reason"] == "claude agent: API Error 529 Overloaded"
    assert failure["failure_code"] == 1 and failure["failure_log"] == "/tmp/agent.log"
    os.environ.pop("TAUCETI_RUNTIME_STATUS")

    # Legacy command import produces the same semantic settings without retaining shell syntax.
    legacy = root / "workers.conf"
    legacy.write_text(
        "./tauceti work --loop --worker-id worker2 --agent codex --only rebase,review --ignore-quota --auto-refresh\n"
    )
    imported = wm.parse_legacy_config(legacy)
    assert imported[0].id == "worker2"
    assert imported[0].only == ("rebase", "review")
    assert imported[0].ignore_quota is True
    assert imported[0].auto_refresh is True

    # The full semantic model must remain parseable by the real work CLI.
    maximal = wm.WorkerSpec(
        id="maximal",
        agent="codex",
        only=("roadmap", "review"),
        sandbox="bubble",
        ignore_quota=True,
        auto_refresh=True,
        roadmap_only="RepresentationTheory",
        roadmap_skip=("Algebra",),
        roadmap_extra_identities=("Maintainer",),
        respect_claims=False,
        source="https://example.invalid/source",
        author_model="gpt-5",
        author_effort="high",
        pace="50%@1h",
        stream=True,
        isolate_home=True,
    )
    parsed = build_parser().parse_args(maximal.work_argv()[3:])
    assert parsed.cmd == "work" and parsed.worker_id == "maximal"
    assert parsed.auto_refresh is True
    # A worker that has NOT opted in must not emit the flag: the default is to leave the operator's
    # single-use refresh token alone.
    assert "--auto-refresh" not in wm.WorkerSpec(id="plain").work_argv()
    assert wm.WorkerSpec.from_dict({"id": "plain"}, 0).auto_refresh is False
    assert wm.WorkerSpec.from_dict({"id": "opted", "auto_refresh": True}, 0).as_dict()["auto_refresh"] is True

    # --- per-worker environment ---------------------------------------------------------------
    # It reaches the worker's process tree (so a build setting can be A/B'd on one worker), survives
    # the encode/decode the manager uses to hand a spec to its runner, and is part of the fingerprint
    # so editing it restarts that worker and nothing else.
    envd = wm.WorkerSpec.from_dict({"id": "exp", "env": {"LAKE_ARTIFACT_CACHE": "1", "A": "b"}}, 0)
    assert envd.env == (("A", "b"), ("LAKE_ARTIFACT_CACHE", "1")), "normalized to sorted pairs"
    assert envd.as_dict()["env"] == {"A": "b", "LAKE_ARTIFACT_CACHE": "1"}
    assert wm._decode_spec(wm._encode_spec(envd)) == envd, "env survives the runner handoff"
    # It must also survive being WRITTEN back: every `enable`, `disable`, `add` and `remove`
    # rewrites the whole file, so a value the writer cannot encode turns the next such command into
    # a hard failure — with the fleet left in whatever state it was in when it ran.
    roundtrip = root / "env-roundtrip.toml"
    wm.save_worker_specs(roundtrip, [envd])
    assert wm.load_worker_specs(roundtrip) == [envd], "env survives a config rewrite"
    assert 'env = { A = "b", LAKE_ARTIFACT_CACHE = "1" }' in roundtrip.read_text()
    assert wm.WorkerSpec(id="plain").as_dict().get("env") is None, "absent unless configured"
    # Order in the file must not restart a worker; a changed value must.
    assert (
        envd.fingerprint()
        == wm.WorkerSpec.from_dict({"id": "exp", "env": {"A": "b", "LAKE_ARTIFACT_CACHE": "1"}}, 0).fingerprint()
    )
    assert (
        envd.fingerprint()
        != wm.WorkerSpec.from_dict({"id": "exp", "env": {"A": "c", "LAKE_ARTIFACT_CACHE": "1"}}, 0).fingerprint()
    )
    assert envd.fingerprint() != wm.WorkerSpec(id="exp").fingerprint()
    # Rejections: a non-string value (TOML's `1` where the consumer wants `"1"`), an unrepresentable
    # name, and any variable the manager sets itself.
    for bad, why in (
        ({"env": {"LAKE_ARTIFACT_CACHE": 1}}, "non-string value"),
        ({"env": {"": "x"}}, "empty name"),
        ({"env": {"A=B": "x"}}, "name containing ="),
        ({"env": {" A": "x"}}, "name with whitespace"),
        ({"env": {"1A": "x"}}, "name starting with a digit"),
        ({"env": {"TAUCETI_MANAGED": "0"}}, "manager-owned variable"),
        ({"env": {"TAUCETI_PARENT_PIPE_FD": "3"}}, "manager-owned variable"),
        # Presetting the isolation sentinel would make isolate_home() return early and leave the
        # worker on the operator's own credentials.
        ({"env": {"TAUCETI_DATA_HOME": "/tmp/whatever"}}, "credential-isolation sentinel"),
        # TOML accepts a literal NUL escape; execve does not, so a NUL must fail as configuration rather than as an
        # unexplained restart loop at launch.
        ({"env": {"LAKE_ARTIFACT_CACHE": "1\0"}}, "NUL in a value"),
        # The whole spec travels as one command-line argument, so an unbounded table becomes E2BIG.
        ({"env": {"BIG": "x" * (16 * 1024 + 1)}}, "oversized table"),
        ({"env": ["A=b"]}, "array instead of a table"),
    ):
        try:
            wm.WorkerSpec.from_dict({"id": "exp", **bad}, 0)
            raise AssertionError(f"{why} should have been rejected: {bad}")
        except wm.WorkersError:
            pass

    # Desired fields win over a stale actual status generation in CLI/TUI snapshots.
    wm.update_status(
        wm.status_path("snapshot"),
        managed=True,
        agent="codex",
        only=["review"],
        sandbox="bubble",
        spec_hash="old",
    )
    desired_spec = wm.WorkerSpec(id="snapshot", agent="claude")
    desired_snapshot = wm.worker_snapshots([desired_spec])[0]
    assert desired_snapshot["agent"] == "claude"
    assert desired_snapshot["actual_spec_hash"] == "old"
    assert desired_snapshot["spec"] == desired_spec.as_dict()

    # Human status is a scan-friendly block, with URLs and provider quota details on separate lines.
    legacy_log = root / "legacy-worker.log"
    legacy_log.write_text(
        "2026-07-30 06:24:55 tauceti: agent-claude: exited 1; last lines of agent.log:\n"
        "    API Error: 529 Overloaded. This is a temporary server-side issue.\n"
        "2026-07-30 06:24:56 tauceti: round rc=1; no-progress streak=5 — backing off 900s\n"
    )
    human_status = "\n".join(
        wm._worker_status_lines(
            Path("/tmp/workers.toml"),
            [
                {
                    "id": "worker1",
                    "desired": "running",
                    "actual": "waiting-quota",
                    "only": [],
                    "spec": {"id": "worker1", "enabled": True},
                    "detail": "codex ~ (weekly ahead, 84% left)   claude ~ (weekly ahead, 56% left)",
                },
                {
                    "id": "worker2",
                    "desired": "running",
                    "actual": "running",
                    "agent": "codex",
                    "phase": "review",
                    "target": "PR #1441  https://github.com/TauCetiProject/TauCeti/pull/1441",
                    "spec": {
                        "id": "worker2",
                        "enabled": True,
                        "agent": "codex",
                        "only": ["rebase", "review"],
                        "ignore_quota": True,
                    },
                    "detail": "provider=codex, sandbox=host",
                },
                {
                    "id": "worker3",
                    "desired": "running",
                    "actual": "backoff",
                    "only": ["roadmap", "fix", "fix-ci"],
                    "spec": {
                        "id": "worker3",
                        "enabled": True,
                        "agent": "claude",
                        "only": ["roadmap", "fix", "fix-ci"],
                        "sandbox": "bubble",
                        "roadmap_only": "RepresentationTheory",
                        "roadmap_skip": ["Algebra"],
                        "roadmap_extra_identities": ["maintainer"],
                        "respect_claims": False,
                        "source": "https://example.invalid/source",
                        "author_model": "claude-opus-5",
                        "author_effort": "high",
                        "pace": "50%@1h",
                        "stream": True,
                        "isolate_home": True,
                        "restart": "on-failure",
                    },
                    "detail": "rc=1",
                    "log_file": str(legacy_log),
                },
            ],
            True,
            width=80,
        )
    )
    assert (
        human_status
        == """manager: running
config:  /tmp/workers.toml

worker1 — waiting for quota
  phases:   rebase, review, fix-ci, fix, bump, progress, roadmap
  agent:    auto · host sandbox
  pacing:   normal
  roadmap:  auto (random each round)
  activity: —
  quota:    codex ~ (weekly ahead, 84% left)
            claude ~ (weekly ahead, 56% left)

worker2 — running
  phases:   rebase, review
  agent:    codex · host sandbox
  pacing:   ignored (--ignore-quota; hard limits still apply)
  work:     review
            PR #1441
            https://github.com/TauCetiProject/TauCeti/pull/1441
  activity: —
  runtime:  codex

worker3 — backing off
  phases:   roadmap, fix, fix-ci
  agent:    claude · bubble sandbox
  pacing:   normal · curve 50%@1h
  roadmap:  RepresentationTheory
            skip: Algebra
            also treat as self: maintainer
            claims: ignored
  source:   https://example.invalid/source
  author:   model claude-opus-5 · high effort
  options:  stream output
            isolated home
            restart: on-failure
  activity: —
  reason:   claude agent: API Error: 529 Overloaded. This is a temporary
            server-side issue.
  logs:     tauceti workers logs worker3"""
    ), human_status
    assert max(map(len, human_status.splitlines())) <= 80

    if sys.platform != "darwin":
        assert wm._service_path() == root / "config" / "systemd" / "user" / "tauceti-workers.service"
        ca_bundle = root / "ca-bundle.pem"
        ca_bundle.write_text("test CA bundle")
        saved_candidates = worker_paths._ssl_cert_candidates
        worker_paths._ssl_cert_candidates = lambda _env: (str(ca_bundle),)
        try:
            clean_env = worker_paths.self_env({})
            assert clean_env["SSL_CERT_FILE"] == str(ca_bundle)
            saved_ssl_cert = os.environ.pop("SSL_CERT_FILE", None)
            try:
                # cli_main discovers the bundle before dispatching `workers service install`;
                # the generator must not mistake its own value for an operator override.
                os.environ["SSL_CERT_FILE"] = str(ca_bundle)
                unit = wm._systemd_unit(config)
                assert 'Environment="PATH=' in unit and 'Environment="PYTHONPATH=' in unit
                # A bundle discoverable at runtime is deliberately not frozen into a persistent unit.
                assert "SSL_CERT_FILE" not in unit and "NIX_SSL_CERT_FILE" not in unit
                assert "ExecStart=" in unit and str(config.resolve()) in unit

                os.environ.pop("SSL_CERT_FILE", None)
                worker_paths._ssl_cert_candidates = lambda env: (env.get("NIX_SSL_CERT_FILE"),)
                saved_nix_cert = os.environ.get("NIX_SSL_CERT_FILE")
                os.environ["NIX_SSL_CERT_FILE"] = str(ca_bundle)
                try:
                    unit = wm._systemd_unit(config)
                finally:
                    if saved_nix_cert is None:
                        os.environ.pop("NIX_SSL_CERT_FILE", None)
                    else:
                        os.environ["NIX_SSL_CERT_FILE"] = saved_nix_cert
                assert f'Environment="NIX_SSL_CERT_FILE={ca_bundle}"' in unit
                assert 'Environment="SSL_CERT_FILE=' not in unit
            finally:
                if saved_ssl_cert is None:
                    os.environ.pop("SSL_CERT_FILE", None)
                else:
                    os.environ["SSL_CERT_FILE"] = saved_ssl_cert
        finally:
            worker_paths._ssl_cert_candidates = saved_candidates

    manager = start_manager(config)
    wait_for(lambda: wm.manager_request("ping"))

    def healthy_rows():
        rows = wm.worker_snapshots(specs)
        return rows if len(rows) == 3 and all(row.get("alive") and row.get("wrapper_pid") for row in rows) else None

    first = wait_for(healthy_rows)
    pids = {row["id"]: row["wrapper_pid"] for row in first}

    # A hard-killed wrapper cannot strand its child: pipe EOF stops it and the manager repairs the slot.
    worker3 = wm.runner_status("worker3")
    old_worker3_child = worker3["child_pid"]
    os.kill(worker3["wrapper_pid"], 9)
    wait_for(lambda: wm.runner_status("worker3").get("wrapper_pid") not in (None, pids["worker3"]))

    def old_child_gone():
        try:
            os.kill(old_worker3_child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(old_child_gone)

    # An explicit restart replaces exactly one managed wrapper.
    assert wm.manager_request("restart", id="worker2")
    wait_for(lambda: wm.runner_status("worker2").get("wrapper_pid") not in (None, pids["worker2"]))
    assert wm.runner_status("worker1").get("wrapper_pid") == pids["worker1"]

    # Disabling is persistent desired state and stops that worker without disturbing peers.
    wm.set_worker_enabled(config, "worker3", False)
    wait_for(lambda: not wm.runner_status("worker3").get("alive"))
    assert wm.load_worker_specs(config)[2].enabled is False
    assert wm.runner_status("worker1").get("wrapper_pid") == pids["worker1"]

    # A changed definition restarts only the changed worker.
    current = wm.load_worker_specs(config)
    wm.save_worker_specs(
        config,
        [dataclasses.replace(spec, agent="claude") if spec.id == "worker1" else spec for spec in current],
    )
    assert wm.manager_request("apply")
    wait_for(lambda: wm.runner_status("worker1").get("wrapper_pid") not in (None, pids["worker1"]))

    # A malformed generation is rejected wholesale; the last good fleet remains alive.
    worker1_pid = wm.runner_status("worker1").get("wrapper_pid")
    config.write_text("version = 1\n[[workers]]\nid = 42\n")
    time.sleep(0.3)
    assert wm.runner_status("worker1").get("wrapper_pid") == worker1_pid
    response = wm.manager_request("ping")
    assert response and response.get("error")

    # Even after a manager restart, an initially malformed file must not stop surviving workers.
    survivors = {wid: wm.runner_status(wid).get("wrapper_pid") for wid in ("worker1", "worker2")}
    assert wm.manager_request("shutdown", stop_workers=False)
    manager.wait(10)
    manager = start_manager(config)
    wait_for(lambda: (reply := wm.manager_request("ping")) and reply.get("error"))
    assert {wid: wm.runner_status(wid).get("wrapper_pid") for wid in survivors} == survivors

    # Shutdown against an initially malformed config stops surviving wrappers without iterating None.
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    assert manager.returncode == 0
    manager = None
    wait_for(lambda: not any(wm.runner_status(wid).get("alive") for wid in survivors))

    # Explicit restart revives a terminal restart="never" worker without changing its policy.
    never = wm.WorkerSpec(id="never", restart="never")
    wm.save_worker_specs(config, [never])
    manager = start_manager(config)
    wait_for(lambda: wm.runner_status("never").get("alive"))
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    manager = start_manager(config)
    wait_for(lambda: wm.manager_request("ping"))
    time.sleep(0.3)
    assert not wm.runner_status("never").get("alive")
    wm.restart_worker(config, "never")
    wait_for(lambda: wm.runner_status("never").get("alive"))

    # Manager shutdown can stop the complete fleet, leaving readable terminal status records.
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    assert manager.returncode == 0
    manager = None
    wait_for(lambda: not wm.runner_status("never").get("alive"))
    assert wm.cmd_workers(bare_workers) == 1  # the shorthand runs status against the stopped manager
    # End to end through the real runner: the configured variable must be in the environment of the
    # process the runner spawns, which is the only place it does any good. `restart = "never"` lets the
    # runner exit once its one child has finished.
    # Its own state and runtime directories: a status file for this probe left in the shared ones
    # would shift the index-based snapshot assertions above the next time someone adds a case there.
    probe_state, probe_runtime = root / "probe-state", root / "probe-run"
    probe_state.mkdir(parents=True, exist_ok=True)
    probe_runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = root / "env-probe.txt"
    probe_spec = wm.WorkerSpec(id="envprobe", restart="never", env=(("LAKE_ARTIFACT_CACHE", "1"),))
    runner_env = worker_paths.self_env()
    # Write the value to a temporary file and rename it into place: `probe.exists()` must not become
    # true between creating the file and writing its contents, or this reads an empty string.
    runner_env["TAUCETI_MANAGER_TEST_COMMAND"] = shlex.join(
        [
            sys.executable,
            "-c",
            "import os, pathlib, sys; tmp = pathlib.Path(sys.argv[1] + '.tmp'); "
            "tmp.write_text(os.environ.get('LAKE_ARTIFACT_CACHE', '<unset>')); "
            "os.replace(tmp, sys.argv[1])",
            str(probe),
        ]
    )
    runner = subprocess.Popen(
        worker_paths.self_argv(
            "_managed-run",
            "--spec",
            wm._encode_spec(probe_spec),
            "--state-dir",
            str(probe_state),
            "--runtime-dir",
            str(probe_runtime),
        ),
        env=runner_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not probe.exists():
            time.sleep(0.1)
        assert probe.exists(), "the runner never started its worker child"
        seen = probe.read_text()
        assert seen == "1", f"worker child saw LAKE_ARTIFACT_CACHE={seen!r}"
    finally:
        runner.terminate()
        try:
            runner.wait(timeout=30)
        except subprocess.TimeoutExpired:
            runner.kill()
            runner.wait(timeout=10)

    print("worker manager: OK")
finally:
    if manager is not None and manager.poll() is None:
        wm.manager_request("shutdown", stop_workers=True)
        try:
            manager.wait(10)
        except subprocess.TimeoutExpired:
            manager.terminate()
            manager.wait(5)
    for state_file in (root / "state" / "tauceti" / "workers").glob("*.json"):
        wm._stop_runner(state_file.stem)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ids = [path.stem for path in (root / "state" / "tauceti" / "workers").glob("*.json")]
        if not any(wm.runner_status(wid).get("alive") for wid in ids):
            break
        time.sleep(0.05)
    shutil.rmtree(root, ignore_errors=True)
