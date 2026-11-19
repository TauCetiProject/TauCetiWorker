"""forgejo-restore against fake remotes: bare repositories standing in for Forgejo, GitHub, the
off-host mirror and the fleet's shared repository."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / "services/forgejo-mirror"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


restore = load("forgejo_restore")
mirror = load("forgejo_mirror")

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


def bare(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


def refs(repo):
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    return dict(reversed(line.split(" ", 1)) for line in out.splitlines())


class RestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.forge = self.tmp / "forge"
        self.target = bare(self.forge / "Org/TauCeti.git")
        self.github = bare(self.tmp / "github/TauCeti.git")
        self.work = self.tmp / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        self.n = 0
        self.main = self.commit()
        self.github_main = self.commit()  # GitHub main is ahead of Forgejo main
        self.put(self.github, "refs/heads/main", self.github_main)
        self.before = {
            "refs/heads/main": self.main,
            "refs/heads/roadmap/a": self.commit(),
            "refs/heads/roadmap/b": self.commit(),
            "refs/heads/roadmap/c": self.commit(),
            "refs/heads/tauceti-ci/x/candidate": self.commit(),
            "refs/tauceti-claims/v2/abc": self.commit(),
            "refs/pull/1/head": self.commit(),
        }
        for ref, sha in self.before.items():
            self.put(self.target, ref, sha)
        self.alice_sha = self.commit()
        self.put(bare(self.forge / "alice/TauCeti.git"), "refs/heads/feature", self.alice_sha)
        self.mirror = self.tmp / "mirror/TauCeti.git"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = mirror.main(
                ["--repo", str(self.mirror), "fetch", "--base-url", str(self.forge), "--repository", "Org/TauCeti"]
                + ["--no-fork-discovery", "--fork", "alice"]
            )
        self.assertEqual(code, 0)
        # The fleet's shared repository: a newer push of roadmap/d that the mirror never saw.
        self.fleet = bare(self.tmp / "fleet/TauCeti-1.git")
        git(self.fleet, "config", "remote.origin.url", str(self.target).removesuffix(".git"))
        self.d_old = self.commit()
        self.d_new = self.commit()
        self.put(self.fleet, "refs/remotes/prscan/4", self.d_old)
        self.put(self.fleet, "refs/tauceti/published/push/roadmap/d", self.d_new)
        self.pulls = [
            self.pull(2, "roadmap/a", self.before["refs/heads/roadmap/a"]),
            self.pull(4, "roadmap/d", self.d_new),
            self.pull(5, "feature", self.alice_sha, owner="alice"),
        ]
        # The incident: every ref gone.
        for ref in self.before:
            git(self.target, "update-ref", "-d", ref)
        self.tokens = {}
        for name in ("main", "worker"):
            path = self.tmp / f"{name}-token"
            path.write_text("token\n")
            self.tokens[name] = path
        patches = [
            mock.patch.object(restore, "open_pulls", lambda *args: self.pulls),
            mock.patch.object(restore, "token_login", self.login),
            mock.patch.object(restore.time, "sleep", lambda seconds: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def login(self, base_url, token_file):
        # The main sync token may not read its own user, as on the real server.
        return "kim-em" if Path(token_file).name == "worker-token" else None

    def commit(self):
        self.n += 1
        git(self.work, "commit", "-q", "--allow-empty", "-m", f"c{self.n}")
        return git(self.work, "rev-parse", "HEAD")

    def put(self, repo, ref, sha):
        git(self.work, "push", "-q", "--force", str(repo), f"{sha}:{ref}")

    def pull(self, number, branch, sha, owner="Org"):
        return {"number": number, "head": {"ref": branch, "sha": sha, "repo": {"owner": {"login": owner}}}}

    def run_restore(self, *extra, command="restore", fork=None):
        argv = ["--base-url", str(self.forge), "--repository", "Org/TauCeti"]
        argv += ["--results", str(self.tmp / "results.json")]
        if fork:
            argv += ["--fork", fork]
        argv.append(command)
        if command == "restore":
            argv += ["--mirror", str(self.mirror), "--github-url", str(self.github), "--fleet-repo", str(self.fleet)]
            argv += ["--main-token-file", str(self.tokens["main"]), "--token-file", str(self.tokens["worker"])]
            argv += ["--batch-size", "2"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.main(argv + list(extra))
        return code, out.getvalue()

    def results(self):
        return {item["ref"]: item for item in json.loads((self.tmp / "results.json").read_text())}

    def expected(self):
        expected = {ref: sha for ref, sha in self.before.items() if not ref.startswith("refs/pull/")}
        expected["refs/heads/main"] = self.github_main
        expected["refs/heads/roadmap/d"] = self.d_new
        return expected

    def test_dry_run_plans_without_pushing(self):
        code, out = self.run_restore()
        self.assertEqual(code, 0, out)
        self.assertIn("dry run", out)
        self.assertEqual(refs(self.target), {})
        plan = self.results()
        self.assertEqual({ref: item["sha"] for ref, item in plan.items()}, self.expected())
        self.assertEqual(plan["refs/heads/main"]["source"], "github")
        self.assertEqual(plan["refs/heads/roadmap/a"]["source"], "mirror")
        self.assertEqual(plan["refs/heads/roadmap/d"]["source"], "fleet")
        self.assertIn("descendant", plan["refs/heads/roadmap/d"]["note"])
        self.assertNotIn("refs/heads/feature", plan)  # a fork PR's head is in the fork
        self.assertFalse(any(ref.startswith(("refs/pull/", "refs/forks/")) for ref in plan))

    def test_execute_restores_everything_and_verifies(self):
        code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        self.assertEqual(refs(self.target), self.expected())
        self.assertTrue(all(item["status"] == "pushed" for item in self.results().values()))
        self.assertIn("2/2 base-repository PRs show the expected head", out)
        # Rerunning is harmless: everything is present.
        code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        self.assertTrue(all(item["status"] == "present" for item in self.results().values()))
        self.assertEqual(self.run_restore(command="verify")[0], 0)

    def test_never_overwrites_an_existing_ref(self):
        other = self.commit()
        self.put(self.target, "refs/heads/roadmap/b", other)
        self.put(self.target, "refs/heads/roadmap/c", self.before["refs/heads/roadmap/c"])
        code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        target = refs(self.target)
        self.assertEqual(target["refs/heads/roadmap/b"], other)
        plan = self.results()
        self.assertEqual(plan["refs/heads/roadmap/b"]["status"], "exists-different")
        self.assertEqual(plan["refs/heads/roadmap/c"]["status"], "present")
        self.assertIn("exists-different", out)
        self.pulls.append(self.pull(12, "roadmap/b", other))
        self.assertEqual(self.run_restore(command="verify")[0], 0)
        self.pulls[-1]["head"]["sha"] = ""  # Forgejo has not recomputed the PR yet
        self.assertEqual(self.run_restore(command="verify")[0], 1)
        self.pulls[-1]["head"]["sha"] = other
        git(self.target, "update-ref", "-d", "refs/heads/roadmap/b")
        code, out = self.run_restore(command="verify")
        self.assertEqual(code, 1)
        self.assertIn("left-as-found ref changed: refs/heads/roadmap/b", out)

    def test_a_ref_created_during_the_push_is_not_overwritten(self):
        other = self.commit()
        real = restore.ls_remote
        calls = []

        def stale(url, env, *patterns, **kw):
            # The target gains roadmap/a after the first batch's listing but before its push.
            seen = real(url, env, *patterns, **kw)
            calls.append(url)
            if len(calls) == 2:
                self.put(self.target, "refs/heads/roadmap/a", other)
            return seen

        with mock.patch.object(restore, "ls_remote", stale):
            code, out = self.run_restore("--execute", "--skip-main")
        self.assertEqual(refs(self.target)["refs/heads/roadmap/a"], other)
        self.assertEqual(self.results()["refs/heads/roadmap/a"]["status"], "failed")
        self.assertEqual(code, 1, out)

    def test_retries_a_transient_failure(self):
        real = restore.push
        calls = []

        def flaky(stage, url, env, batch, timeout):
            calls.append(len(batch))
            if len(calls) == 2:
                return subprocess.CompletedProcess([], 1, "", "error: RPC failed; HTTP 502"), {}
            return real(stage, url, env, batch, timeout)

        with mock.patch.object(restore, "push", flaky):
            code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        self.assertEqual(refs(self.target), self.expected())
        self.assertGreater(len(calls), 4)

    def test_main_needs_the_main_sync_account(self):
        self.tokens["main"], self.tokens["worker"] = self.tokens["worker"], self.tokens["main"]
        code, out = self.run_restore("--execute")
        self.assertEqual(code, 2, out)
        self.assertIn("belongs to kim-em, not tauceti-main-sync", out)
        self.assertEqual(refs(self.target), {})

    def test_diverged_fleet_copies_pick_the_newest_and_say_so(self):
        git(self.work, "checkout", "-q", "--detach", self.d_old)
        sideways = self.commit()
        self.put(self.fleet, "refs/remotes/prscan/4", sideways)
        code, out = self.run_restore()
        self.assertEqual(code, 0, out)
        self.assertIn("DIVERGED", self.results()["refs/heads/roadmap/d"]["note"])
        self.assertIn("DIVERGED", out)

    def test_exclude_and_mirror_only(self):
        code, out = self.run_restore("--exclude", "refs/tauceti-claims/", "--no-fallback")
        self.assertEqual(code, 0, out)
        plan = self.results()
        self.assertNotIn("refs/tauceti-claims/v2/abc", plan)
        self.assertNotIn("refs/heads/roadmap/d", plan)

    def test_restore_a_fork_from_its_namespace(self):
        git(self.forge / "alice/TauCeti.git", "update-ref", "-d", "refs/heads/feature")
        code, out = self.run_restore("--execute", fork="alice")
        self.assertEqual(code, 0, out)
        self.assertEqual(refs(self.forge / "alice/TauCeti.git"), {"refs/heads/feature": self.alice_sha})
        self.assertEqual(refs(self.target), {})

    def test_check_prints_root_commands_for_broken_refs(self):
        broken = {
            "refs/heads/tauceti-ci/x/candidate": restore.ZERO,
            "refs/tauceti-claims/v2/abc": restore.ZERO,
        }
        with mock.patch.object(restore, "ls_remote", lambda *args, **kw: broken):
            code, out = self.run_restore(command="check")
        self.assertEqual(code, 1)
        self.assertIn("main MISSING", out)
        self.assertIn("rm -f -- refs/heads/tauceti-ci/x/candidate refs/tauceti-claims/v2/abc", out)
        self.assertIn("cd -- /var/lib/forgejo/repositories/org/tauceti.git", out)
        self.assertIn("find refs -type f -empty -delete", out)
        # restore refuses while the server still has broken refs
        with mock.patch.object(restore, "ls_remote", lambda *args, **kw: broken):
            self.assertEqual(self.run_restore("--execute")[0], 1)
        self.put(self.target, "refs/heads/main", self.main)
        code, out = self.run_restore(command="check")
        self.assertEqual(code, 0, out)
        # A fork's commands enter the fork's directory.
        with mock.patch.object(restore, "ls_remote", lambda *args, **kw: broken):
            code, out = self.run_restore(command="check", fork="alice/Other")
        self.assertIn("cd -- /var/lib/forgejo/repositories/alice/other.git", out)
        self.assertNotIn("org/tauceti.git", out)

    def test_root_commands_quote_ref_names_and_stop_on_failure(self):
        lines = restore.root_commands("/srv/repo dir", ["refs/heads/a;id"])
        script = "\n".join(lines)
        self.assertIn("rm -f -- 'refs/heads/a;id'", script)
        self.assertIn("cd -- '/srv/repo dir'", script)
        self.assertTrue(lines[0].startswith("( set -e"))
        # The block really stops when the directory is wrong.
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("uid=", proc.stdout)

    def test_main_never_comes_from_the_fleet_or_past_an_exclusion(self):
        self.pulls.append(self.pull(9, "main", self.main))
        self.put(self.fleet, "refs/tauceti/published/push/main", self.main)
        code, out = self.run_restore("--skip-main")
        self.assertEqual(code, 0, out)
        self.assertNotIn("refs/heads/main", self.results())
        code, out = self.run_restore("--exclude", "refs/heads/")
        self.assertEqual(code, 0, out)
        self.assertFalse(any(ref.startswith("refs/heads/") for ref in self.results()))

    def test_a_forks_main_uses_the_fork_token(self):
        fork = self.forge / "alice/TauCeti.git"
        self.put(fork, "refs/heads/main", self.main)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mirror.main(
                ["--repo", str(self.mirror), "fetch", "--base-url", str(self.forge), "--repository", "Org/TauCeti"]
                + ["--no-fork-discovery", "--fork", "alice", "--accept-loss"]
            )
        git(fork, "update-ref", "-d", "refs/heads/main")
        git(fork, "update-ref", "-d", "refs/heads/feature")
        self.tokens["main"].unlink()  # not needed for a fork
        code, out = self.run_restore("--skip-main", fork="alice")
        self.assertEqual(code, 0, out)
        self.assertNotIn("refs/heads/main", self.results())
        code, out = self.run_restore("--execute", fork="alice")
        self.assertEqual(code, 0, out)
        self.assertEqual(refs(fork), {"refs/heads/main": self.main, "refs/heads/feature": self.alice_sha})

    def test_relative_paths_work(self):
        relative = os.path.relpath(self.mirror)
        argv = ["--base-url", str(self.forge), "--results", str(self.tmp / "results.json"), "restore"]
        argv += ["--mirror", relative, "--github-url", str(self.github), "--no-fallback"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = restore.main(["--repository", "Org/TauCeti"] + argv)
        self.assertEqual(code, 0, out.getvalue())
        self.assertFalse(any(item["status"] == "missing-object" for item in self.results().values()))

    def test_verify_fails_on_unrecovered_work(self):
        self.pulls.append(self.pull(11, "roadmap/lost", self.main))
        self.assertEqual(self.run_restore("--execute")[0], 1)
        code, out = self.run_restore(command="verify")
        self.assertEqual(code, 1)
        self.assertIn("PR #11: branch roadmap/lost was not restored", out)
        self.pulls.pop()
        self.assertEqual(self.run_restore(command="verify")[0], 0)
        # An unreadable PR list is not a pass.
        with mock.patch.object(restore, "open_pulls", mock.Mock(side_effect=OSError("down"))):
            self.assertEqual(self.run_restore(command="verify")[0], 1)
        # Nor is a planned ref whose commit no copy has.
        plan = json.loads((self.tmp / "results.json").read_text())
        plan.append({"ref": "refs/heads/roadmap/gone", "sha": "1" * 40, "source": "mirror", "status": "missing-object"})
        (self.tmp / "results.json").write_text(json.dumps(plan))
        self.assertEqual(self.run_restore(command="verify")[0], 1)

    def test_an_interrupted_push_keeps_the_whole_plan(self):
        real = restore.push
        calls = []

        def dies(stage, url, env, batch, timeout):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real(stage, url, env, batch, timeout)

        with mock.patch.object(restore, "push", dies), self.assertRaises(KeyboardInterrupt):
            self.run_restore("--execute")
        self.assertEqual(set(self.results()), set(self.expected()))
        self.assertEqual(self.run_restore(command="verify")[0], 1)
        self.assertEqual(self.run_restore("--execute")[0], 0)

    def test_main_accepted_by_the_server_despite_a_local_timeout(self):
        real = restore.git

        def slow(repo, *args, **kw):
            proc = real(repo, *args, **kw)
            if args and args[0] == "push" and args[-1].endswith(":refs/heads/main"):
                raise subprocess.TimeoutExpired(["git", "push"], 1)  # after the server took it
            return proc

        with mock.patch.object(restore, "git", slow):
            code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        self.assertEqual(refs(self.target), self.expected())

    def test_a_push_timeout_is_retried(self):
        real = restore.git
        calls = []

        def slow(repo, *args, **kw):
            if args and args[0] == "push":
                calls.append(1)
                if len(calls) == 2:
                    raise subprocess.TimeoutExpired(["git", "push"], 1)
            return real(repo, *args, **kw)

        with mock.patch.object(restore, "git", slow):
            code, out = self.run_restore("--execute")
        self.assertEqual(code, 0, out)
        self.assertIn("exit 124", out)
        self.assertEqual(refs(self.target), self.expected())

    def test_verify_reports_wrong_pr_heads(self):
        self.assertEqual(self.run_restore("--execute")[0], 0)
        self.pulls[0]["head"]["sha"] = self.main
        code, out = self.run_restore(command="verify")
        self.assertEqual(code, 1)
        self.assertIn("PR #2", out)

    def test_fleet_repositories_are_found_by_origin(self):
        pattern = str(self.tmp / "fleet/TauCeti-*.git")
        unrelated = bare(self.tmp / "fleet/TauCeti-2.git")
        git(unrelated, "config", "remote.origin.url", "https://github.com/TauCetiProject/TauCeti")
        target = restore.Target(str(self.forge), "Org/TauCeti", None)
        self.assertEqual(restore.fleet_repositories(pattern, target), [self.fleet])


if __name__ == "__main__":
    unittest.main()
