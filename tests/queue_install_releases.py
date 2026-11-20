#!/usr/bin/env python3
"""The queue installers give each install its own release directory and touch only their units.

Each test runs the real installers in a temporary HOME against a local origin, with fake `uv` and
`systemctl` on PATH. The fake systemd starts a `sleep` per service whose working directory is the
unit's `WorkingDirectory`, so "which code does the running process use" is the process's real cwd;
the fake repair worker writes the `worker.json` heartbeat a started worker writes, and the fake
`tauceti-queue status` answers from the baked revision of the controller process's directory.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FAKE_SYSTEMCTL = r"""#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys
from pathlib import Path
home = Path(os.environ["HOME"])
units = home / ".config/systemd/user"
state = Path(os.environ["FAKE_SYSTEMD"])
state.mkdir(parents=True, exist_ok=True)
args = [a for a in sys.argv[1:] if a not in ("--user", "--quiet", "--now", "--value", "--plain", "--no-legend", "--all")]
with open(state / "calls", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")

def fragment(unit):
    path = units / unit
    if not path.exists() and "@" in unit:
        path = units / (unit.split("@")[0] + "@.service")
    return path

def expand(value, unit):
    instance = unit.split("@", 1)[1].rsplit(".", 1)[0] if "@" in unit else ""
    return value.replace("%h", str(home)).replace("%i", instance)

def setting(unit, key):
    text = fragment(unit).read_text()
    found = re.findall(rf"^{key}=-?(.*)$", text, re.M)
    return expand(found[-1], unit) if found else None

def pid_of(unit):
    try:
        pid = int((state / f"{unit}.pid").read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0

def start(unit):
    if not unit.endswith(".service") or pid_of(unit):
        return
    cwd = setting(unit, "WorkingDirectory")
    repair = unit.startswith("tauceti-queue-repair") and not unit.startswith("tauceti-queue-repair-cleanup")
    env = {}
    if repair:
        for line in Path(setting(unit, "EnvironmentFile")).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
    # the process gets its unit's EnvironmentFile, as under systemd
    proc = subprocess.Popen(["sleep", "600"], cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, **env})
    (state / f"{unit}.pid").write_text(str(proc.pid))
    if repair:
        root = Path(env["TAUCETI_QUEUE_TASK_ROOT"])
        root.mkdir(parents=True, exist_ok=True)
        baked = Path(cwd) / "tauceti_queue/_revision.py"
        revision = re.search(r'SHA = "(\w+)"', baked.read_text()).group(1) if baked.exists() else None
        heartbeat = {"pid": proc.pid, "state": "idle", "worker": env.get("TAUCETI_QUEUE_WORKER"), "service": "s"}
        if revision:
            heartbeat["revision"] = revision
        (root / "worker.json").write_text(json.dumps(heartbeat))

def stop(unit):
    pid = pid_of(unit)
    if pid:
        os.kill(pid, signal.SIGTERM)
    (state / f"{unit}.pid").unlink(missing_ok=True)

command, rest = args[0], args[1:]
if command in ("daemon-reload", "enable", "reenable"):
    sys.exit(0)
if command == "start":
    for unit in rest:
        start(unit)
elif command == "restart":
    for unit in rest:
        stop(unit)
        start(unit)
elif command == "is-active":
    sys.exit(0 if pid_of(rest[0]) else 3)
elif command == "show":
    prop, unit = rest[1], rest[2]
    if prop == "ControlGroup":
        # where systemd put the running process: its unit's slice, under tauceti.slice when it names one
        slice_ = None if os.environ.get("FAKE_SYSTEMD_IGNORES_SLICE") else setting(unit, "Slice")
        print((f"/user.slice/tauceti.slice/{slice_}/{unit}" if slice_ else f"/user.slice/app.slice/{unit}") if pid_of(unit) else "")
    else:
        print(pid_of(unit) if prop == "MainPID" else fragment(unit) if prop == "FragmentPath" else "")
elif command == "list-units":
    pass
else:
    sys.exit(f"fake systemctl: unsupported {sys.argv[1:]}")
"""

FAKE_UV = r"""#!/usr/bin/env bash
echo "$PWD UV_NO_SYNC=${UV_NO_SYNC:-} $*" >> "$FAKE_SYSTEMD/uv-calls"
case "$1" in
  sync) mkdir -p .venv/bin; exit 0 ;;
  run) shift ;;
  *) exit 0 ;;
esac
case "$*" in
  *"check-config --print content_clone"*) echo "$HOME/content" ;;
  *"check-config --print repository"*) echo "TauCetiProject/TauCeti" ;;
  *"status"*"--expect-revision"*)
    expected="${*: -1}"
    pid="$(cat "$FAKE_SYSTEMD/tauceti-queue.service.pid")"
    running="$(sed -n 's/^SHA = "\(.*\)"$/\1/p' "/proc/$pid/cwd/tauceti_queue/_revision.py")"
    [ "$running" = "$expected" ] || exit 2 ;;
esac
exit 0
"""


class Releases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.units = self.home / ".config/systemd/user"
        self.units.mkdir(parents=True)
        self.state = self.root / "systemd"
        self.addCleanup(self.stop_all)
        # ~/.local/bin, because install-repair.sh puts the unit's PATH (which starts there) first
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True)
        self.state.mkdir()
        for name, text in (("systemctl", FAKE_SYSTEMCTL), ("uv", FAKE_UV)):
            (bindir / name).write_text(text)
            (bindir / name).chmod(0o755)
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_SYSTEMD=str(self.state),
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        self.env.pop("TAUCETI_QUEUE_CHECKOUT", None)
        self.env.pop("TAUCETI_QUEUE_RELEASE", None)
        # origin: this working tree's queue scripts and units, as a commit
        self.origin = self.root / "origin"
        (self.origin / "services").mkdir(parents=True)
        shutil.copytree(REPO / "services/queue", self.origin / "services/queue")
        (self.origin / "tauceti_queue").mkdir()
        (self.origin / "tauceti_queue/__init__.py").write_text("")
        (self.origin / ".gitignore").write_text("/tauceti_queue/_revision.py\n/.venv/\n")
        self.git(self.origin, "init", "-q", "-b", "main")
        self.first = self.commit("first")
        self.checkout = self.home / "TauCetiQueue"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.checkout))
        # data and configuration the installers expect on the host
        config = self.home / ".config/tauceti-queue"
        config.mkdir(parents=True)
        for name in ("config.toml", "environment", "monitoring-environment"):
            (config / name).write_text("")
        for instance in ("", "-2", "-3"):
            (config / f"repair-environment{instance}").write_text(
                f"TAUCETI_QUEUE_SERVICE=http://127.0.0.1:8780\nTAUCETI_QUEUE_CLONE={self.home}/clone\n"
                f"TAUCETI_QUEUE_WORKER=repair{instance}@host\n"
                f"TAUCETI_QUEUE_TASK_ROOT={self.home}/TauCetiQueue-data/repair{instance}\n"
            )
        (self.home / "clone/.git").mkdir(parents=True)
        (self.home / "content").mkdir()
        (self.home / "content/HEAD").write_text("")
        (self.home / "TauCetiQueue-data/monitoring").mkdir(parents=True)
        (self.home / "TauCetiQueue-data/monitoring/token").write_text("")
        (self.home / "TauCetiQueue-data/queue.sqlite3").write_text("")
        self.releases = self.home / "TauCetiQueue-releases"

    # --- helpers ---------------------------------------------------------------------------------

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, env=getattr(self, "env", None), check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message):
        (self.origin / "marker.txt").write_text(message)
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-qm", message)
        return self.git(self.origin, "rev-parse", "HEAD")

    def stop_all(self):
        for pidfile in self.state.glob("*.pid") if self.state.exists() else ():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def run_script(self, name, *args, expect=0):
        result = subprocess.run(
            ["bash", str(self.checkout / "services/queue" / name), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stderr

    def old_layout(self):
        """Today's layout: every unit runs from ~/TauCetiQueue, the instances from the template."""
        for unit in (REPO / "services/queue").glob("tauceti-queue*.service"):
            shutil.copy(unit, self.units / unit.name)
        shutil.copy(REPO / "services/queue/tauceti-queue-backup.timer", self.units)
        for unit in (
            "tauceti-queue.service",
            "tauceti-queue-repair.service",
            "tauceti-queue-repair@2.service",
            "tauceti-queue-repair@3.service",
            "tauceti-queue-monitoring.service",
            "tauceti-queue-task-activity.service",
        ):
            subprocess.run(["systemctl", "--user", "start", unit], env=self.env, check=True)

    def pid(self, unit):
        return int((self.state / f"{unit}.pid").read_text())

    def cwd(self, unit):
        return os.path.realpath(f"/proc/{self.pid(unit)}/cwd")

    def release_dirs(self, sha=None):
        pattern = re.compile(r"^[0-9a-f]{40}-\d{8}T\d{6}Z$")
        return sorted(
            p for p in self.releases.glob("*") if pattern.match(p.name) and (sha is None or p.name.startswith(sha))
        )

    def setting(self, unit, key):
        return re.findall(rf"^{key}=(.*)$", (self.units / unit).read_text(), re.M)[-1]

    # --- tests -----------------------------------------------------------------------------------

    def test_first_install_moves_only_its_unit_off_the_shared_checkout(self):
        self.old_layout()
        before = {
            u: self.pid(u)
            for u in ("tauceti-queue.service", "tauceti-queue-repair@3.service", "tauceti-queue-monitoring.service")
        }
        head_before = self.git(self.checkout, "rev-parse", "HEAD")
        second = self.commit("second")
        log = self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")

        [release] = self.release_dirs(second)
        self.assertIn("handing off", log)
        self.assertEqual((release / "tauceti_queue/_revision.py").read_text(), f'SHA = "{second}"\nDIRTY = False\n')
        self.assertIn(f"sha={second}", (release / ".tauceti-queue-release").read_text())
        # the instance has its own unit file naming the release; the template still names the checkout
        self.assertEqual(self.setting("tauceti-queue-repair@2.service", "WorkingDirectory"), str(release))
        self.assertEqual(
            self.setting("tauceti-queue-repair@2.service", "ExecStart"),
            f"{release}/.venv/bin/python -m tauceti_queue.repair_task",
        )
        self.assertIn("repair-environment-%i", self.setting("tauceti-queue-repair@2.service", "EnvironmentFile"))
        self.assertEqual(
            (self.units / "tauceti-queue-repair@.service").read_bytes(),
            (REPO / "services/queue/tauceti-queue-repair@.service").read_bytes(),
        )
        self.assertEqual(self.cwd("tauceti-queue-repair@2.service"), str(release))
        # nothing else moved, and the shared checkout's working tree did not change
        for unit, pid in before.items():
            self.assertEqual(self.pid(unit), pid, unit)
            self.assertEqual(self.cwd(unit), str(self.checkout), unit)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head_before)
        self.assertEqual((self.checkout / "marker.txt").read_text(), "first")
        heartbeat = json.loads((self.home / "TauCetiQueue-data/repair-2/worker.json").read_text())
        self.assertEqual(heartbeat["revision"], second)

    def test_each_install_has_its_own_release_and_leaves_the_others_alone(self):
        self.run_script("install-repair.sh", "--wait", "10")
        [repair_release] = self.release_dirs(self.first)
        repair_pid = self.pid("tauceti-queue-repair.service")
        second = self.commit("second")
        self.run_script("install-monitoring.sh", "--wait", "0")
        [monitoring_release] = self.release_dirs(second)
        for unit in (
            "tauceti-queue-monitoring.service",
            "tauceti-queue-task-activity.service",
            "tauceti-queue-monitoring-history.service",
        ):
            self.assertEqual(self.setting(unit, "WorkingDirectory"), str(monitoring_release))
            exec_start = self.setting(unit, "ExecStart")
            self.assertTrue(exec_start.startswith(f"{monitoring_release}/.venv/bin/python "), exec_start)
            self.assertIn("%h/TauCetiQueue-data/", exec_start, "data paths stay where they are")
        self.assertEqual(self.cwd("tauceti-queue-monitoring.service"), str(monitoring_release))
        self.assertEqual(self.pid("tauceti-queue-repair.service"), repair_pid)
        self.assertEqual(self.cwd("tauceti-queue-repair.service"), str(repair_release))
        self.assertEqual(self.setting("tauceti-queue-repair.service", "WorkingDirectory"), str(repair_release))
        # the installer's own uv calls never resync a built release
        calls = (self.state / "uv-calls").read_text().splitlines()
        self.assertTrue(all("UV_NO_SYNC=1" in c for c in calls if " run " in f" {c.split(' ', 2)[2]} "), calls)

    def test_controller_install_verifies_status_and_moves_the_backup_with_it(self):
        self.old_layout()
        log = self.run_script("install.sh", "--wait", "5")
        [release] = self.release_dirs(self.first)
        self.assertIn(f"running revision {self.first[:12]} from {release}", log)
        self.assertEqual(self.cwd("tauceti-queue.service"), str(release))
        self.assertEqual(
            self.setting("tauceti-queue-backup.service", "ExecStart"),
            f"/usr/bin/env bash {release}/services/queue/backup.sh",
        )
        self.assertEqual(self.cwd("tauceti-queue-repair.service"), str(self.checkout))
        self.assertIn("--upgrade-package tauceti-tools", (self.state / "uv-calls").read_text())
        self.assertEqual(
            self.setting("tauceti-queue-clone-maintenance.service", "ExecStart"),
            f"/usr/bin/env bash {release}/services/queue/clone-maintenance.sh",
        )
        calls = (self.state / "calls").read_text()
        self.assertIn("tauceti-queue-clone-maintenance.timer", re.findall(r"^.*enable.*$", calls, re.M)[-1])

    def test_override_warns_and_rollback_reuses_an_existing_release(self):
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        [old] = self.release_dirs(self.first)
        second = self.commit("second")
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        [new] = self.release_dirs(second)
        self.assertEqual(self.cwd("tauceti-queue-repair@2.service"), str(new))
        # moving backwards needs an explicit release, which warns and does not rebuild it
        log = self.run_script(
            "install-repair.sh", "--instance", "2", "--revision", self.first, "--wait", "10", expect=1
        )
        self.assertIn("WARNING: installing --revision", log)
        self.assertIn("not an ancestor", log)
        syncs = (self.state / "uv-calls").read_text().count(" sync ")
        log = self.run_script("install-repair.sh", "--instance", "2", "--release", str(old), "--wait", "10")
        self.assertIn("WARNING: pointing tauceti-queue-repair@2.service at the existing release", log)
        self.assertEqual(self.cwd("tauceti-queue-repair@2.service"), str(old))
        self.assertEqual(self.setting("tauceti-queue-repair@2.service", "WorkingDirectory"), str(old))
        self.assertEqual((self.state / "uv-calls").read_text().count(" sync "), syncs)
        self.assertEqual(len(self.release_dirs(self.first)), 1, "the rollback made no new release")

    def test_drop_in_is_refused_before_anything_changes(self):
        dropins = self.units / "tauceti-queue.service.d"
        dropins.mkdir()
        (dropins / "override.conf").write_text("[Service]\n")
        log = self.run_script("install.sh", expect=1)
        self.assertIn("drop-in", log)
        self.assertFalse(self.releases.exists())

    def claims(self, *rows):
        """The controller's claims table with ROWS of (worker, expires_at, released_at)."""
        import sqlite3

        path = self.home / "TauCetiQueue-data/queue.sqlite3"
        path.unlink()
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE claims (id INTEGER PRIMARY KEY, operation TEXT, worker TEXT, task_id INTEGER,"
                " expires_at REAL, released_at REAL)"
            )
            for worker, expires_at, released_at in rows:
                db.execute(
                    "INSERT INTO claims (operation, worker, task_id, expires_at, released_at) VALUES ('repair', ?, 7, ?, ?)",
                    (worker, expires_at, released_at),
                )

    def test_when_idle_waits_while_the_worker_holds_a_claim(self):
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        pid = self.pid("tauceti-queue-repair@2.service")
        self.commit("second")
        self.claims(("repair-2@host", time.time() + 900, None))
        log = self.run_script(
            "install-repair.sh", "--instance", "2", "--when-idle", "--idle-timeout", "1", "--wait", "10", expect=3
        )
        self.assertIn("waiting before restarting tauceti-queue-repair@2.service: repair-2@host holds claim", log)
        self.assertIn("(task 7, repair, expires_at +", log)
        self.assertIn("was not restarted", log)
        self.assertEqual(self.pid("tauceti-queue-repair@2.service"), pid)
        self.assertEqual(len(self.release_dirs()), 1)

    def test_when_idle_asks_about_the_identity_the_running_worker_has(self):
        """An environment file edited since the worker started does not hide the running worker's claim."""
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        pid = self.pid("tauceti-queue-repair@2.service")
        env_file = self.home / ".config/tauceti-queue/repair-environment-2"
        env_file.write_text(env_file.read_text() + "TAUCETI_QUEUE_WORKER=renamed@host\n")
        self.claims(("repair-2@host", time.time() + 900, None))
        log = self.run_script("install-repair.sh", "--instance", "2", "--when-idle", "--idle-timeout", "1", expect=3)
        self.assertIn("repair-2@host holds claim", log)
        self.assertEqual(self.pid("tauceti-queue-repair@2.service"), pid)

    def test_when_idle_waits_for_an_expired_claim_the_controller_has_not_released(self):
        """A renewal waiting for the controller's lock keeps an expired claim; only the release ends it."""
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        self.claims(("repair-2@host", time.time() - 5, None))
        log = self.run_script("install-repair.sh", "--instance", "2", "--when-idle", "--idle-timeout", "1", expect=3)
        self.assertIn("repair-2@host holds claim", log)

    def test_when_idle_ignores_released_and_other_workers_claims(self):
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        pid = self.pid("tauceti-queue-repair@2.service")
        second = self.commit("second")
        now = time.time()
        self.claims(
            ("repair-2@host", now + 900, now - 5),
            ("repair-2@host", now - 900, now - 1000),
            ("repair@host", now + 900, None),
        )
        log = self.run_script("install-repair.sh", "--instance", "2", "--when-idle", "--wait", "10")
        self.assertEqual(
            log.count("tauceti-queue-repair@2.service holds no claim"), 2, log
        )  # before the install, before the restart
        self.assertNotEqual(self.pid("tauceti-queue-repair@2.service"), pid)
        [release] = self.release_dirs(second)
        self.assertEqual(self.cwd("tauceti-queue-repair@2.service"), str(release))

    def test_when_idle_needs_a_restart_and_a_readable_database(self):
        log = self.run_script("install-repair.sh", "--when-idle", "--no-restart", expect=1)
        self.assertIn("exclusive", log)
        (self.home / "TauCetiQueue-data/queue.sqlite3").unlink()
        log = self.run_script("install-repair.sh", "--when-idle", expect=1)
        self.assertIn("does not exist", log)
        self.assertFalse(self.releases.exists())

    def test_repair_workers_run_in_the_low_priority_slice(self):
        """Every repair unit the installer writes runs niced, at the lowest best-effort IO priority and in tauceti-repair.slice, below
        the fleet's VMs; the slice is installed as it is, naming no release."""
        self.run_script("install-repair.sh", "--wait", "10")
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        for unit in ("tauceti-queue-repair.service", "tauceti-queue-repair@2.service"):
            self.assertEqual(self.setting(unit, "Slice"), "tauceti-repair.slice", unit)
            self.assertEqual(self.setting(unit, "Nice"), "10", unit)
            self.assertEqual(self.setting(unit, "IOSchedulingClass"), "best-effort", unit)
            self.assertEqual(self.setting(unit, "IOSchedulingPriority"), "7", unit)
            self.assertEqual(self.setting(unit, "OOMScoreAdjust"), "1000", unit)
        slice_unit = (self.units / "tauceti-repair.slice").read_text()
        self.assertEqual(slice_unit, (REPO / "services/queue/tauceti-repair.slice").read_text())
        self.assertNotIn("TauCetiQueue-releases", slice_unit, "the slice must not keep a release alive")
        self.assertEqual(re.findall(r"^CPUWeight=(\d+)$", slice_unit, re.M), ["10"])
        self.assertEqual(re.findall(r"^IOWeight=(\d+)$", slice_unit, re.M), ["10"])
        # TauCetiFleet sets memory.high live under its shared ceiling; a unit value would fight it.
        self.assertNotRegex(slice_unit, r"(?m)^Memory(High|Max)=")

    def test_repair_install_fails_when_the_worker_is_not_in_its_slice(self):
        self.env["FAKE_SYSTEMD_IGNORES_SLICE"] = "1"
        log = self.run_script("install-repair.sh", "--wait", "4", expect=2)
        self.assertIn("not tauceti-repair.slice", log)

    def test_security_audit_install_moves_only_the_pool(self):
        self.run_script("install-monitoring.sh", "--wait", "0")
        monitoring_pid = self.pid("tauceti-queue-monitoring.service")
        (self.home / ".config/tauceti-queue/security-audit-zulip.json").write_text("{}")
        (self.home / ".config/tauceti-queue/security-audit-anthropic-api-key").write_text("key")
        (self.home / ".config/tauceti-security-audit").mkdir(exist_ok=True)
        for name in ("github-app.json", "github-private-key.pem"):
            (self.home / ".config/tauceti-security-audit" / name).write_text("{}")
        second = self.commit("second")
        self.run_script("install-security-audit.sh", "--wait", "0")
        [release] = self.release_dirs(second)
        unit = "tauceti-queue-security-audit.service"
        self.assertEqual(self.setting(unit, "WorkingDirectory"), str(release))
        exec_start = self.setting(unit, "ExecStart")
        self.assertTrue(
            exec_start.startswith(f"{release}/.venv/bin/python -m tauceti_worker.security_audit pool "), exec_start
        )
        self.assertIn("--state %h/TauCetiQueue-data/security-audit-pool/state/pool/security-audit", exec_start)
        self.assertEqual(self.cwd(unit), str(release))
        self.assertEqual(self.pid("tauceti-queue-monitoring.service"), monitoring_pid)

    def test_security_audit_unit_state_is_the_pool_workers_audit_state(self):
        text = (REPO / "services/queue/tauceti-queue-security-audit.service").read_text().replace("%h", str(self.home))
        root = re.search(r"^Environment=TAUCETI_LEGACY_DATA_ROOT=(\S+)$", text, re.M).group(1)
        state = Path(re.search(r"--state (\S+)", text).group(1))
        script = (
            "import sys; from pathlib import Path; from tauceti_worker.config import Config; "
            "s = Path(sys.argv[1]); print(Config.resolve(s.parent.name).state / 'security-audit')"
        )
        env = dict(os.environ, TAUCETI_LEGACY_DATA_ROOT=root, HOME=str(self.home))
        env.pop("TAUCETI_PROJECT_STATE", None)
        resolved = subprocess.run(
            [sys.executable, "-c", script, str(state)], cwd=REPO, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(Path(resolved), state)

    def test_security_audit_drop_in_is_refused(self):
        (self.home / ".config/tauceti-queue/security-audit-zulip.json").write_text("{}")
        (self.home / ".config/tauceti-queue/security-audit-anthropic-api-key").write_text("key")
        (self.home / ".config/tauceti-security-audit").mkdir(exist_ok=True)
        for name in ("github-app.json", "github-private-key.pem"):
            (self.home / ".config/tauceti-security-audit" / name).write_text("{}")
        dropins = self.units / "tauceti-queue-security-audit.service.d"
        dropins.mkdir(parents=True)
        (dropins / "override.conf").write_text("[Service]\n")
        log = self.run_script("install-security-audit.sh", "--wait", "0", expect=1)
        self.assertIn("drop-in", log)
        self.assertFalse((self.units / "tauceti-queue-security-audit.service").exists())

    def test_repair_slice_drop_in_is_refused(self):
        dropins = self.units / "tauceti-repair.slice.d"
        dropins.mkdir()
        (dropins / "override.conf").write_text("[Slice]\nCPUWeight=100\n")
        log = self.run_script("install-repair.sh", "--wait", "10", expect=1)
        self.assertIn("drop-in", log)
        self.assertFalse(self.releases.exists())
        self.assertFalse((self.units / "tauceti-repair.slice").exists())

    def test_failed_fetch_stops_the_install_rather_than_using_the_cached_origin(self):
        self.origin.rename(self.root / "origin-gone")
        log = self.run_script("install-monitoring.sh", "--wait", "0", expect=1)
        self.assertIn("cannot fetch origin", log)
        self.assertEqual(self.release_dirs(), [])

    def test_gc_waits_for_an_install_that_holds_the_lock(self):
        self.releases.mkdir()
        old = self.releases / f"{'c' * 40}-20260101T000000Z"
        old.mkdir()
        if True:
            holder = subprocess.Popen(
                ["flock", "-s", str(self.releases / ".lock"), "sleep", "30"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(holder.wait)
            self.addCleanup(holder.kill)
            time.sleep(0.5)
            env = dict(self.env, TAUCETI_QUEUE_RELEASE_KEEP="0")
            result = subprocess.run(
                ["bash", str(REPO / "services/queue/release.sh"), "gc"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertIn("not collecting", result.stderr)
        self.assertTrue(old.exists())

    def test_gc_keeps_referenced_running_and_newest_releases(self):
        self.releases.mkdir()
        names = [f"{'a' * 40}-2026010{day}T000000Z" for day in range(1, 9)]
        for name in names:
            (self.releases / name).mkdir()
        handmade = self.releases / "hand-made-release-20261110"
        handmade.mkdir()
        # day 1 is named by a unit, day 2 is some process's working directory
        (self.units / "tauceti-queue-x.service").write_text(f"WorkingDirectory={self.releases / names[0]}\n")
        busy = subprocess.Popen(
            ["sleep", "60"], cwd=self.releases / names[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.addCleanup(busy.wait)
        self.addCleanup(busy.kill)
        env = dict(self.env, TAUCETI_QUEUE_RELEASE_KEEP="2")
        subprocess.run(
            ["bash", str(REPO / "services/queue/release.sh"), "gc"], env=env, check=True, capture_output=True
        )
        left = sorted(p.name for p in self.releases.iterdir() if p.name != ".lock")
        self.assertEqual(left, sorted([names[0], names[1], names[6], names[7], handmade.name]))

    def test_gc_removes_worktrees_from_the_shared_clone(self):
        env = dict(self.env, TAUCETI_QUEUE_RELEASE_KEEP="0")
        self.env = env
        self.run_script("install-repair.sh", "--wait", "10")
        [first] = self.release_dirs(self.first)
        self.commit("second")
        self.run_script("install-repair.sh", "--wait", "10")
        self.assertFalse(first.exists())
        self.assertNotIn(str(first), self.git(self.checkout, "worktree", "list"))
        self.assertEqual(len(self.release_dirs()), 1)

    def test_status_names_installed_and_running_revisions(self):
        self.old_layout()
        self.run_script("install-repair.sh", "--instance", "2", "--wait", "10")
        result = subprocess.run(
            ["bash", str(REPO / "services/queue/release.sh"), "status"],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        rows = {line.split()[0]: line.split() for line in result.stdout.splitlines()[1:]}
        [release] = self.release_dirs(self.first)
        self.assertEqual(rows["tauceti-queue-repair@2.service"][1:], [self.first[:12], self.first[:12], str(release)])
        self.assertEqual(rows["tauceti-queue.service"][3], str(self.checkout))

    def test_cleanup_installer_writes_private_configuration_and_points_units_at_a_release(self):
        database = self.home / "TauCetiQueue-data/queue.sqlite3"
        worker = self.home / "TauCetiQueue-data/repair"
        worker.mkdir(parents=True)
        self.run_script(
            "install-repair-cleanup.sh",
            "--database",
            str(database),
            "--clone",
            str(self.home / "clone"),
            "--root",
            str(worker),
        )
        config = self.home / ".config/tauceti-queue/repair-cleanup.json"
        self.assertEqual(
            json.loads(config.read_text()),
            {
                "database": str(database.resolve()),
                "clone": str((self.home / "clone").resolve()),
                "roots": [str(worker.resolve())],
            },
        )
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        [release] = self.release_dirs(self.first)
        self.assertEqual(self.setting("tauceti-queue-repair-cleanup.service", "WorkingDirectory"), str(release))
        self.assertTrue((self.units / "tauceti-queue-repair-cleanup.timer").exists())


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class CloneMaintenance(unittest.TestCase):
    """`clone-maintenance.sh` runs only concurrency-safe tasks and clears a lock nothing holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clone = Path(self.tmp.name) / "clone"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
        env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.env = env
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True, env=env)
        for index in range(3):
            (self.clone / f"f{index}").write_text(f"{index}\n")
            subprocess.run(["git", "-C", str(self.clone), "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", str(self.clone), "commit", "-qm", str(index)], check=True, env=env)
            subprocess.run(["git", "-C", str(self.clone), "update-ref", f"refs/x/{index}", "HEAD"], check=True)
        self.lock = self.clone / ".git/objects/maintenance.lock"

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(REPO / "services/queue/clone-maintenance.sh"), "--clone", str(self.clone), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.clone), *args], check=True, capture_output=True, text=True).stdout

    def test_maintains_the_clone_and_turns_automatic_maintenance_off(self):
        for _ in range(2):  # loose-objects packs them, and deletes the packed copies the next time
            result = self.run_script()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("config", "maintenance.auto").strip(), "false")
        self.assertIn("refs/x/2", (self.clone / ".git/packed-refs").read_text(), "pack-refs ran")
        self.assertEqual(self.git("count-objects", "-v").split("count: ")[1].split("\n")[0], "0", "loose-objects ran")
        self.assertFalse(self.lock.exists())

    def test_a_stale_unheld_lock_is_removed_and_a_recent_one_is_left(self):
        self.lock.write_text("")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running", result.stderr)
        self.assertTrue(self.lock.exists(), "a recent lock may belong to a live run")
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("removing a stale maintenance lock", result.stderr)
        self.assertFalse(self.lock.exists())
        self.assertIn("refs/x/2", (self.clone / ".git/packed-refs").read_text())

    def test_an_old_lock_is_kept_while_any_git_maintenance_runs(self):
        self.lock.write_text("")
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        # A maintenance run elsewhere (a linked worktree's, say) that names no path of this clone.
        fake = Path(self.tmp.name) / "bin/git"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(0o755)
        direct = fake.with_name("git-maintenance")
        shutil.copy(fake, direct)
        for argv in ([str(fake), "maintenance", "run"], [str(direct), "run"]):
            holder = subprocess.Popen(argv, start_new_session=True)
            self.addCleanup(_kill_group, holder.pid)
            time.sleep(0.2)
            result = self.run_script()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already running", result.stderr, argv)
            self.assertTrue(self.lock.exists())
            _kill_group(holder.pid)
            holder.wait()

    def test_a_second_run_waits_for_none_and_touches_nothing(self):
        git_dir = self.clone / ".git"
        holder = subprocess.Popen(
            ["flock", str(git_dir / "tauceti-clone-maintenance.lock"), "sleep", "30"], start_new_session=True
        )
        self.addCleanup(_kill_group, holder.pid)
        time.sleep(0.2)
        self.lock.write_text("")
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another clone maintenance is running", result.stderr)
        self.assertTrue(self.lock.exists(), "the running script's run owns what happens to it")

    def test_the_unit_reads_the_controllers_environment(self):
        unit = (REPO / "services/queue/tauceti-queue-clone-maintenance.service").read_text()
        controller = (REPO / "services/queue/tauceti-queue.service").read_text()
        environment = re.findall(r"^EnvironmentFile=.*$", controller, re.M)
        self.assertTrue(environment)
        for line in environment:
            self.assertIn(line, unit)

    def test_no_clone_yet_is_not_an_error(self):
        result = subprocess.run(
            ["bash", str(REPO / "services/queue/clone-maintenance.sh"), "--clone", str(self.clone / "missing")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to maintain yet", result.stderr)


class Revision(unittest.TestCase):
    def test_server_reports_the_baked_revision_read_at_startup(self):
        from tauceti_queue import server

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "tauceti_queue"
            package.mkdir()
            (package / "_revision.py").write_text(f'SHA = "{"b" * 40}"\nDIRTY = False\n')
            self.assertEqual(server.running_revision(package), "b" * 40)
            (package / "_revision.py").unlink()
            self.assertEqual(server.running_revision(package), "")


if __name__ == "__main__":
    unittest.main()
