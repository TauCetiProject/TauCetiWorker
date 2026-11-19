"""The Forgejo mirror against fake remotes: plain bare repositories laid out like a forge."""

import contextlib
import datetime as dt
import fcntl
import gzip
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "services/forgejo-mirror/forgejo_mirror.py"
SPEC = importlib.util.spec_from_file_location("forgejo_mirror", SOURCE)
mirror = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mirror  # dataclasses look the module up
SPEC.loader.exec_module(mirror)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
    ).stdout.strip()


class Forge:
    """<root>/<owner>/<name>.git bare repositories, reachable as base URL <root>."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        self.counter = 0

    def repo(self, full_name):
        path = self.root / f"{full_name}.git"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        return path

    def commit(self):
        self.counter += 1
        git(self.work, "commit", "-q", "--allow-empty", "-m", f"c{self.counter}")
        return git(self.work, "rev-parse", "HEAD")

    def set_ref(self, full_name, ref, sha):
        repo = self.repo(full_name)
        git(self.work, "push", "-q", "--force", str(repo), f"{sha}:{ref}")

    def delete_ref(self, full_name, ref):
        git(self.repo(full_name), "update-ref", "-d", ref)

    def refs(self, full_name):
        out = git(self.repo(full_name), "for-each-ref", "--format=%(objectname) %(refname)")
        return dict(reversed(line.split(" ", 1)) for line in out.splitlines())


class MirrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        (self.tmp / "forge").mkdir()
        self.forge = Forge(self.tmp / "forge")
        self.mirror = self.tmp / "mirror" / "TauCeti.git"
        main = self.forge.commit()
        self.base_refs = {
            "refs/heads/main": main,
            "refs/heads/roadmap/a": self.forge.commit(),
            "refs/heads/tauceti-ci/x/y/candidate": self.forge.commit(),
            "refs/pull/7/head": self.forge.commit(),
            "refs/tauceti-claims/v2/abc": self.forge.commit(),
        }
        for ref, sha in self.base_refs.items():
            self.forge.set_ref("Org/TauCeti", ref, sha)
        self.fork_sha = self.forge.commit()
        self.forge.set_ref("alice/TauCeti", "refs/heads/feature", self.fork_sha)

    def run_mirror(self, *extra, command="fetch"):
        argv = ["--repo", str(self.mirror), command]
        if command == "fetch":
            argv += ["--base-url", str(self.forge.root), "--repository", "Org/TauCeti", "--no-fork-discovery"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mirror.main(argv + list(extra))
        events = [
            json.loads(line[3:]) for line in (out.getvalue() + err.getvalue()).splitlines() if line.startswith("<")
        ]
        return code, events

    def mirrored(self):
        out = git(self.mirror, "for-each-ref", "--format=%(objectname) %(refname)")
        refs = dict(reversed(line.split(" ", 1)) for line in out.splitlines())
        return {name: sha for name, sha in refs.items() if not name.startswith("refs/tauceti-mirror/")}

    def health(self):
        return json.loads((self.mirror.parent / "health.json").read_text())

    def test_mirrors_every_ref_and_fork_into_its_namespace(self):
        code, events = self.run_mirror("--fork", "alice")
        self.assertEqual(code, 0, events)
        expected = dict(self.base_refs)
        expected["refs/forks/alice/heads/feature"] = self.fork_sha
        self.assertEqual(self.mirrored(), expected)
        self.assertEqual(git(self.mirror, "config", "core.logAllRefUpdates"), "always")
        self.assertEqual(git(self.mirror, "config", "gc.pruneExpire"), "never")
        self.assertEqual(self.health()["forks"], ["alice/TauCeti"])
        self.assertIsNone(self.health()["sources"]["base"]["last_error"])
        snapshots = list((self.mirror.parent / "snapshots").glob("refs-*.txt.gz"))
        self.assertEqual(len(snapshots), 1)
        lines = gzip.open(snapshots[0], "rt").read().splitlines()
        self.assertIn(f"{self.fork_sha} refs/forks/alice/heads/feature", lines)
        self.assertFalse(any("tauceti-mirror" in line for line in lines))
        # A known fork stays mirrored without being named again.
        newer = self.forge.commit()
        self.forge.set_ref("alice/TauCeti", "refs/heads/feature", newer)
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertEqual(self.mirrored()["refs/forks/alice/heads/feature"], newer)

    def test_follows_ordinary_deletions_and_force_pushes(self):
        self.assertEqual(self.run_mirror()[0], 0)
        self.forge.delete_ref("Org/TauCeti", "refs/heads/roadmap/a")
        moved = self.forge.commit()
        self.forge.set_ref("Org/TauCeti", "refs/pull/7/head", moved)
        code, events = self.run_mirror()
        self.assertEqual(code, 0, events)
        refs = self.mirrored()
        self.assertNotIn("refs/heads/roadmap/a", refs)
        self.assertEqual(refs["refs/pull/7/head"], moved)
        mirrored = [event for event in events if event["event"] == "mirrored"][0]
        self.assertEqual((mirrored["lost"], mirrored["changed"]), (1, 1))
        # The reflog keeps the old position of a moved ref.
        self.assertIn(
            self.base_refs["refs/pull/7/head"], git(self.mirror, "reflog", "show", "--format=%H", "refs/pull/7/head")
        )

    def test_ref_that_becomes_a_directory(self):
        self.assertEqual(self.run_mirror()[0], 0)
        self.forge.delete_ref("Org/TauCeti", "refs/heads/roadmap/a")
        sha = self.forge.commit()
        self.forge.set_ref("Org/TauCeti", "refs/heads/roadmap/a/b", sha)
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertEqual(self.mirrored()["refs/heads/roadmap/a/b"], sha)
        self.assertNotIn("refs/heads/roadmap/a", self.mirrored())

    def test_reverse_directory_change_and_failed_second_transaction(self):
        self.forge.set_ref("Org/TauCeti", "refs/heads/roadmap/x/y", self.base_refs["refs/heads/main"])
        self.assertEqual(self.run_mirror()[0], 0)
        # roadmap/x/y becomes roadmap/x, which needs two transactions.
        self.forge.delete_ref("Org/TauCeti", "refs/heads/roadmap/x/y")
        self.forge.set_ref("Org/TauCeti", "refs/heads/roadmap/x", self.base_refs["refs/heads/main"])
        moved = self.forge.commit()
        self.forge.set_ref("Org/TauCeti", "refs/heads/roadmap/a", moved)
        before = self.mirrored()
        # A held lock makes the second transaction fail; the first must be undone.
        lock = self.mirror / "refs/heads/roadmap/a.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        code, events = self.run_mirror()
        self.assertEqual(code, 1, events)
        self.assertEqual(self.mirrored(), before)
        self.assertEqual(list(self.mirror.parent.glob("apply-backup-*")), [])
        lock.unlink()
        self.assertEqual(self.run_mirror()[0], 0)
        refs = self.mirrored()
        self.assertEqual(refs["refs/heads/roadmap/a"], moved)
        self.assertIn("refs/heads/roadmap/x", refs)
        self.assertNotIn("refs/heads/roadmap/x/y", refs)

    def test_base_refs_cannot_enter_the_mirror_namespaces(self):
        sha = self.base_refs["refs/heads/main"]
        self.forge.set_ref("Org/TauCeti", "refs/forks/alice/heads/feature", sha)
        self.forge.set_ref("Org/TauCeti", "refs/tauceti-mirror/archive", sha)
        for _ in range(2):
            code, events = self.run_mirror("--fork", "alice")
            self.assertEqual(code, 0, events)
            refs = self.mirrored()
            self.assertEqual(refs["refs/forks/alice/heads/feature"], self.fork_sha)
            self.assertNotIn("refs/tauceti-mirror/archive", git(self.mirror, "for-each-ref", "--format=%(refname)"))

    def test_reserved_namespace_roots_are_dropped(self):
        sha = self.base_refs["refs/heads/main"]
        git(self.forge.repo("Org/TauCeti"), "update-ref", "refs/forks", sha)
        git(self.forge.repo("Org/TauCeti"), "update-ref", "refs/tauceti-mirror", sha)
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertEqual(self.run_mirror("--fork", "alice")[0], 0)
        self.assertEqual(self.mirrored()["refs/forks/alice/heads/feature"], self.fork_sha)
        self.assertNotIn("refs/forks", self.mirrored())

    def test_an_interrupted_apply_is_replayed_before_the_next_assessment(self):
        for i in range(30):
            self.forge.set_ref("Org/TauCeti", f"refs/heads/roadmap/b{i}", self.base_refs["refs/heads/main"])
        self.assertEqual(self.run_mirror()[0], 0)
        accepted = self.mirrored()
        # Upstream deleted 8 refs; a run deleted them in its first step and died. Upstream then
        # lost 8 more.
        doomed = [f"refs/heads/roadmap/b{i}" for i in range(8)]
        backup = self.mirror.parent / "apply-backup-base.txt"
        backup.write_text("".join(f"{accepted[name]} {name}\n" for name in doomed))
        for name in doomed:
            git(self.mirror, "update-ref", "-d", name)
        for i in range(16):
            self.forge.delete_ref("Org/TauCeti", f"refs/heads/roadmap/b{i}")
        self.assertEqual(self.run_mirror(command="check")[0], 1)
        code, events = self.run_mirror()
        # Against the full baseline 16 refs disappear, more than the allowance of 10.
        self.assertEqual(code, 1, events)
        self.assertIn("recovered-interrupted-apply", [event["event"] for event in events])
        self.assertEqual(self.mirrored(), accepted)
        self.assertFalse(backup.exists())

    def test_fork_with_its_own_name(self):
        self.forge.set_ref("bob/Other", "refs/heads/work", self.fork_sha)
        self.forge.set_ref("bob/TauCeti", "refs/heads/unrelated", self.base_refs["refs/heads/main"])
        self.assertEqual(self.run_mirror("--fork", "bob/Other")[0], 0)
        self.assertEqual(self.mirrored()["refs/forks/bob/heads/work"], self.fork_sha)
        self.assertNotIn("refs/forks/bob/heads/unrelated", self.mirrored())

    def test_deleted_refs_keep_their_objects_through_gc(self):
        doomed = self.forge.commit()
        self.forge.set_ref("Org/TauCeti", "refs/heads/doomed", doomed)
        self.assertEqual(self.run_mirror()[0], 0)
        self.forge.delete_ref("Org/TauCeti", "refs/heads/doomed")
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertNotIn("refs/heads/doomed", self.mirrored())
        git(self.mirror, "gc", "--quiet")
        self.assertEqual(git(self.mirror, "cat-file", "-t", doomed), "commit")

    def assert_refused(self, reason_part):
        before = self.mirrored()
        code, events = self.run_mirror()
        self.assertEqual(code, 1, events)
        refused = [event for event in events if event["event"] in ("refused", "fetch-failed")]
        self.assertTrue(refused and refused[0]["level"] == "error", events)
        self.assertIn(reason_part, refused[0].get("reason", refused[0].get("error", "")))
        self.assertEqual(self.mirrored(), before)
        self.assertIn(reason_part, self.health()["sources"]["base"]["last_error"])
        return before

    def test_refuses_a_repository_that_lost_every_ref(self):
        self.assertEqual(self.run_mirror()[0], 0)
        for ref in self.base_refs:
            self.forge.delete_ref("Org/TauCeti", ref)
        self.assert_refused("no refs")
        # Still refused on the next run: the accepted copy never decays.
        self.assert_refused("no refs")

    def test_refuses_a_repository_without_main(self):
        self.assertEqual(self.run_mirror()[0], 0)
        self.forge.delete_ref("Org/TauCeti", "refs/heads/main")
        self.assert_refused("refs/heads/main")

    def test_refuses_a_large_loss_unless_the_operator_accepts_it(self):
        for i in range(40):
            self.forge.set_ref("Org/TauCeti", f"refs/heads/roadmap/b{i}", self.base_refs["refs/heads/main"])
        self.assertEqual(self.run_mirror()[0], 0)
        for i in range(12):
            self.forge.delete_ref("Org/TauCeti", f"refs/heads/roadmap/b{i}")
        before = self.assert_refused("12 accepted refs would disappear")
        self.assertEqual(len(before), 45)
        # Below the allowance it is ordinary churn.
        code, events = self.run_mirror("--loss-allowance", "12")
        self.assertEqual(code, 0, events)
        self.assertEqual(len(self.mirrored()), 33)

    def test_accept_loss_override(self):
        self.assertEqual(self.run_mirror()[0], 0)
        for ref in list(self.base_refs)[1:]:
            self.forge.delete_ref("Org/TauCeti", ref)
        code, events = self.run_mirror("--loss-allowance", "0", "--accept-loss")
        self.assertEqual(code, 0, events)
        self.assertIn("loss-accepted-by-operator", [event["event"] for event in events])
        self.assertEqual(list(self.mirrored()), ["refs/heads/main"])

    def test_an_unreachable_fork_does_not_stop_the_base(self):
        code, events = self.run_mirror("--fork", "nobody")
        self.assertEqual(code, 1)
        self.assertEqual(self.mirrored(), self.base_refs)
        self.assertIn("fetch-failed", [event["event"] for event in events])
        self.assertIsNone(self.health()["sources"]["base"]["last_error"])
        self.assertTrue(self.health()["sources"]["fork-nobody"]["last_error"])

    def test_check_reports_stale_sources(self):
        code, _ = self.run_mirror(command="check")
        self.assertEqual(code, 1)
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertEqual(self.run_mirror(command="check")[0], 0)
        health = self.health()
        health["sources"]["base"]["last_success"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        (self.mirror.parent / "health.json").write_text(json.dumps(health))
        self.assertEqual(self.run_mirror(command="check")[0], 1)

    def test_concurrent_run_is_busy(self):
        self.assertEqual(self.run_mirror()[0], 0)
        with open(self.mirror / "tauceti-mirror.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.run_mirror()[0], 75)

    def test_old_snapshots_expire(self):
        self.assertEqual(self.run_mirror()[0], 0)
        snapshots = self.mirror.parent / "snapshots"
        (snapshots / "refs-2000-01-01.txt.gz").write_bytes(b"")
        self.assertEqual(self.run_mirror()[0], 0)
        self.assertEqual(len(list(snapshots.glob("refs-*"))), 1)

    def test_token_travels_in_the_environment_only(self):
        token = self.tmp / "token"
        token.write_text("secret-token\n")
        env = mirror.auth_env("https://forge.example", token, "reader")
        values = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertIn("http.https://forge.example/.extraHeader", keys)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(any(value.startswith("Authorization: Basic ") for value in values))
        self.assertNotIn("secret-token", " ".join(values))  # base64-encoded with the user name

    def test_discovered_forks_use_their_full_names(self):
        self.forge.set_ref("carol/TauCeti-work", "refs/heads/x", self.fork_sha)
        argv = ["--repo", str(self.mirror), "fetch", "--base-url", str(self.forge.root), "--repository", "Org/TauCeti"]
        original = mirror.discover_forks
        mirror.discover_forks = lambda *args: ["carol/TauCeti-work"]
        self.addCleanup(setattr, mirror, "discover_forks", original)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mirror.main(argv), 0)
        self.assertEqual(self.mirrored()["refs/forks/carol/heads/x"], self.fork_sha)
        self.assertEqual(self.health()["forks"], ["carol/TauCeti-work"])

    def test_a_renamed_fork_is_followed(self):
        self.assertEqual(self.run_mirror("--fork", "alice/TauCeti")[0], 0)
        renamed = self.forge.repo("alice/TauCeti").rename(self.forge.root / "alice/Renamed.git")
        self.assertTrue(renamed.exists())
        argv = ["--repo", str(self.mirror), "fetch", "--base-url", str(self.forge.root), "--repository", "Org/TauCeti"]
        original = mirror.discover_forks
        mirror.discover_forks = lambda *args: ["alice/Renamed"]
        self.addCleanup(setattr, mirror, "discover_forks", original)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mirror.main(argv), 0)
        self.assertEqual(self.health()["forks"], ["alice/Renamed"])
        self.assertEqual(self.mirrored()["refs/forks/alice/heads/feature"], self.fork_sha)

    def test_fork_discovery_failure_keeps_known_forks(self):
        self.assertEqual(self.run_mirror("--fork", "alice")[0], 0)
        argv = ["--repo", str(self.mirror), "fetch", "--base-url", str(self.forge.root), "--repository", "Org/TauCeti"]
        original = mirror.discover_forks
        mirror.discover_forks = lambda *args: (_ for _ in ()).throw(OSError("api down"))
        self.addCleanup(setattr, mirror, "discover_forks", original)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(mirror.main(argv), 0)
        self.assertIn("fork-discovery-failed", err.getvalue())
        self.assertIn("fork-alice", self.health()["sources"])


if __name__ == "__main__":
    unittest.main()
