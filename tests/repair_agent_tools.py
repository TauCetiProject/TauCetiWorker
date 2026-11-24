"""The repair agent's toolchain commands run in the build sandbox, and no credential is visible there."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import repair, repair_agent_tools, repair_sandbox
from tauceti_worker import agents
from tauceti_worker.config import environment

BWRAP_STUB = """#!{python}
import json, os, sys
with open({log!r}, "a") as out:
    out.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
"""
REAL_STUB = '#!/bin/sh\necho "real $(basename "$0") $*"\n'


def real_bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    run = subprocess.run([bwrap, "--ro-bind", "/", "/", "--unshare-user", "--unshare-net", "true"], capture_output=True)
    return run.returncode == 0


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-tools-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.elan = self.home / ".elan"
        self.toolchain = self.elan / "toolchains" / "leanprover--lean4---v4.0.0" / "bin"
        self.toolchain.mkdir(parents=True)
        (self.elan / "bin").mkdir()
        for name in ("lean", "lake", "leanchecker", "lean-extra", "clang"):
            (self.toolchain / name).write_text(REAL_STUB)
            (self.toolchain / name).chmod(0o755)
        self.bin = self.tmp / "bin"  # the host's PATH: the toolchain's commands and a recording bwrap
        self.bin.mkdir()
        for name in ("lake", "lean", "elan"):
            (self.bin / name).write_text(REAL_STUB)
            (self.bin / name).chmod(0o755)
        self.calls = self.tmp / "bwrap-calls.jsonl"
        (self.bin / "bwrap").write_text(BWRAP_STUB.format(python=sys.executable, log=str(self.calls)))
        (self.bin / "bwrap").chmod(0o755)
        self.worktree = self.tmp / "worktree"
        (self.worktree / "TauCeti" / "Sub").mkdir(parents=True)
        (self.worktree / "lakefile.toml").write_text('name = "probe"\n')
        self.root = self.tmp / "root"
        # The host's tools the commands need, and nothing else: no real bwrap, elan or Lean.
        system = self.tmp / "system"
        system.mkdir()
        for name in ("bash", "sh", "env", "readlink", "basename", "grep", "nice", "ls", "mkdir", "cat", "echo"):
            if where := shutil.which(name):
                (system / name).symlink_to(where)
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:{system}", "LANG": "C.UTF-8"}
        self.tools = repair_agent_tools.write_tools(self.root, self.env)
        self.agent_env = {
            **self.env,
            "PATH": f"{self.tools}:{self.env['PATH']}",
            "BASH_ENV": str(self.tools / repair_agent_tools.GUARD),
        }

    def bwrap_calls(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def run_agent(self, command: str, cwd: Path, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shutil.which("bash"), "-c", command],
            cwd=cwd,
            env={**self.agent_env, **extra},
            capture_output=True,
            text=True,
        )


class Shims(Fixture):
    def test_every_toolchain_command_has_a_shim(self):
        names = {p.name for p in self.tools.iterdir() if p.is_symlink()}
        self.assertTrue(set(repair_agent_tools.COMMANDS) <= names)
        self.assertIn("lean-extra", names, "lean* executables of installed toolchains")
        self.assertNotIn("clang", names)
        for name in names:
            self.assertEqual(os.readlink(self.tools / name), repair_agent_tools.SHIM)
        self.assertEqual(repair_agent_tools.write_tools(self.root, self.env), self.tools, "rewriting is idempotent")

    def test_lake_runs_in_the_sandbox_from_the_callers_directory(self):
        cwd = self.worktree / "TauCeti" / "Sub"
        run = self.run_agent("lake env lean --run X.lean; lean --version; elan run v4 lean", cwd)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("real", run.stdout, "no command reached the host's toolchain")
        calls = self.bwrap_calls()
        self.assertEqual(
            [c["argv"][c["argv"].index("--") + 1 :] for c in calls],
            [["lake", "env", "lean", "--run", "X.lean"], ["lean", "--version"], ["elan", "run", "v4", "lean"]],
        )
        for call in calls:
            argv = call["argv"]
            self.assertIn("--unshare-net", argv)
            self.assertEqual(argv[argv.index("--chdir") + 1], str(cwd))
            binds = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--ro-bind"]
            self.assertIn((str(self.worktree), str(self.worktree)), binds, "the worktree is the Lake project")
            path = dict((argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--setenv")["PATH"]
            self.assertNotIn(str(self.tools), path)

    def test_a_command_inside_lake_state_belongs_to_its_worktree(self):
        packages = self.worktree / ".lake" / "packages" / "mathlib"
        packages.mkdir(parents=True)
        (packages / "lakefile.lean").write_text("")
        self.assertEqual(repair_agent_tools.project_root(packages), self.worktree)
        self.assertEqual(repair_agent_tools.project_root(self.worktree / "TauCeti"), self.worktree)
        self.assertIsNone(repair_agent_tools.project_root(self.tmp))

    def test_outside_a_lake_project_the_shims_refuse(self):
        run = self.run_agent("lean --version", self.tmp)
        self.assertEqual(run.returncode, 126)
        self.assertIn("runs only inside the repair worktree", run.stderr)
        self.assertEqual(self.bwrap_calls(), [])

    def test_without_bwrap_the_shims_fail_closed(self):
        which = repair_sandbox.shutil.which
        cwd = os.getcwd()
        os.chdir(self.worktree)
        self.addCleanup(os.chdir, cwd)
        with (
            patch.dict(os.environ, self.agent_env, clear=True),
            patch.object(
                repair_sandbox.shutil, "which", lambda name, **kw: None if name == "bwrap" else which(name, **kw)
            ),
            patch.object(repair_agent_tools.os, "execvpe", side_effect=AssertionError("ran")),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(repair_agent_tools.shim_main([str(self.tools / "lake"), "build"]), 127)
        self.assertIn("build sandbox is unavailable", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_the_disabled_sandbox_runs_the_real_command_with_a_warning(self):
        run = self.run_agent("lake build X", self.worktree, TAUCETI_QUEUE_SANDBOX="0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "real lake build X", "the shim does not call itself")
        self.assertIn("WITHOUT the bwrap sandbox", run.stderr)
        self.assertEqual(self.bwrap_calls(), [])


class ShellGuard(Fixture):
    def test_a_toolchain_executable_named_by_path_is_refused(self):
        for command in (
            f"{self.toolchain}/lean --version",
            '"$HOME/.elan/toolchains/leanprover--lean4---v4.0.0/bin/lean" --version',
            "~/.elan/toolchains/leanprover--lean4---v4.0.0/bin/lake build",
            f"env FOO=1 {self.toolchain}/leanchecker x",
            f"f() {{ {self.toolchain}/lean; }}; f",
            f"echo $({self.toolchain}/lean)",
            f"set -e; {self.toolchain}/lean; echo survived",
            f"PATH=/nonexistent; {self.toolchain}/lean --version",
        ):
            with self.subTest(command=command):
                run = self.run_agent(command, self.worktree)
                self.assertNotIn("real", run.stdout)
                self.assertNotIn("survived", run.stdout)
                self.assertIn("bypasses the build sandbox", run.stderr)

    def test_a_path_that_skips_the_shims_is_refused(self):
        for command in (
            f'PATH="{self.bin}:$PATH" lake build',
            f"PATH={self.bin} lean --version",
            f"env PATH={self.bin} lake build",
            "env -i lake build",
            "env -u PATH lake build",
            "command -p lean --version",
            f"FOO=1 PATH='{self.bin}' elan show",
        ):
            with self.subTest(command=command):
                run = self.run_agent(command, self.worktree)
                self.assertNotIn("real", run.stdout)
                self.assertIn("sets or resets PATH", run.stderr)
        run = self.run_agent(f'PATH="{self.tools}:$PATH" lake build; grep -ci lean lakefile.toml', self.worktree)
        self.assertEqual(run.stdout.split(), ["0"], run.stderr)
        self.assertEqual(len(self.bwrap_calls()), 1)

    def test_ordinary_commands_and_named_tools_pass(self):
        run = self.run_agent(
            "echo TauCeti/Lean/Foo.lean; ls lakefile.toml; lake build; mkdir -p d/lean; echo ok", self.worktree
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.split(), ["TauCeti/Lean/Foo.lean", "lakefile.toml", "ok"])
        self.assertEqual(run.stderr, "")
        self.assertEqual(len(self.bwrap_calls()), 1)

    def test_the_shims_stay_first_on_path(self):
        run = self.run_agent(
            f'PATH="{self.bin}:$PATH"; type -P lake; export PATH={self.bin}; type -P lean', self.worktree
        )
        self.assertEqual(run.stdout.split(), [str(self.tools / "lake"), str(self.tools / "lean")], run.stderr)


class Credentials(Fixture):
    def test_credentials_inside_a_bound_tree_are_masked(self):
        pool = self.tmp / "pool"
        (pool / "app").mkdir(parents=True)
        key = pool / "repair-cache-key"
        key.write_text("secret")
        env = {
            **self.env,
            "LAKE_CACHE_DIR": str(pool),
            "TAUCETI_REPAIR_CACHE_KEY_FILE": str(key),
            "TAUCETI_QUEUE_GITHUB_APP": str(pool / "app"),
        }
        argv = repair_sandbox.sandbox_argv(["true"], self.worktree, env, bwrap=str(self.bin / "bwrap"))
        tail = argv[argv.index("--bind", argv.index(str(pool)) - 1) :]
        self.assertIn(["--ro-bind", "/dev/null", str(key)], [tail[i : i + 3] for i in range(len(tail))])
        self.assertIn(["--tmpfs", str(pool / "app")], [tail[i : i + 2] for i in range(len(tail))])
        self.assertLess(argv.index(str(key)), argv.index("--remount-ro"))

    def test_a_credential_under_a_symlinked_bind_is_masked_where_it_appears(self):
        data = self.tmp / "data"
        (data / "pool").mkdir(parents=True)
        key = data / "pool" / "repair-cache-key"
        key.write_text("secret")
        (self.tmp / "pool").symlink_to(data / "pool")
        usage = data / "pool" / "usage.json"
        usage.write_text("secret")
        env = {
            **self.env,
            "LAKE_CACHE_DIR": str(self.tmp / "pool"),
            "TAUCETI_REPAIR_CACHE_KEY_FILE": str(key),
            "TAUCETI_CLAUDE_USAGE_CREDENTIALS": str(usage),
        }
        argv = repair_sandbox.sandbox_argv(["true"], self.worktree, env, bwrap=str(self.bin / "bwrap"))
        triples = [argv[i : i + 3] for i in range(len(argv))]
        for name in ("repair-cache-key", "usage.json"):
            self.assertIn(["--ro-bind", "/dev/null", str(self.tmp / "pool" / name)], triples)

    def test_a_credential_value_that_is_not_a_path_is_never_copied(self):
        env = {**self.env, "TAUCETI_REPAIR_CACHE_KEY_FILE": "id:secret"}
        self.assertNotIn("secret", " ".join(map(str, repair_sandbox.credential_paths(env))))

    def test_a_bound_tree_inside_a_credential_directory_is_refused(self):
        secrets = self.home / ".config" / "tauceti-queue"
        (secrets / "pool").mkdir(parents=True)
        env = {**self.env, "LAKE_CACHE_DIR": str(secrets / "pool")}
        with self.assertRaisesRegex(repair_sandbox.SandboxUnavailable, "inside the credential path"):
            repair_sandbox.sandbox_argv(["true"], self.worktree, env, bwrap=str(self.bin / "bwrap"))

    def test_the_agent_learns_the_workers_credential_paths(self):
        app = self.tmp / "app-dir"
        with patch.dict(os.environ, {**self.env, "TAUCETI_QUEUE_GITHUB_APP": str(app)}, clear=True):
            changes = repair.isolation_env(self.root, {}, "claude")
            self.assertIsNone(changes["TAUCETI_QUEUE_GITHUB_APP"])
            hidden = changes[repair_sandbox.HIDDEN_VARIABLE].split(os.pathsep)
            self.assertIn(str(app), hidden)
            agent = {**self.env, **{k: v for k, v in changes.items() if v is not None}}
            self.assertIn(app, repair_sandbox.credential_paths(agent))
            self.assertEqual(changes["TAUCETI_AGENT_TOOLS"], str(self.tools))
            self.assertEqual(changes["BASH_ENV"], str(self.tools / repair_agent_tools.GUARD))
            startup = Path(changes["TAUCETI_REPAIR_CLAUDE_ENV_FILE"]).read_text()
            self.assertIn(f". {self.tools / repair_agent_tools.GUARD}\n", startup)
            with environment(**changes):
                _, env = agents.host_agent_argv("test", "claude")
            self.assertTrue(env["PATH"].startswith(f"{self.tools}:"), env["PATH"])

    @unittest.skipUnless(real_bwrap_works(), "needs bwrap with unprivileged user namespaces")
    def test_the_real_sandbox_reads_no_credential(self):
        secrets = self.home / ".config" / "tauceti-queue"
        secrets.mkdir(parents=True)
        (secrets / "github-private-key.pem").write_text("secret")
        pool = self.tmp / "pool"
        pool.mkdir()
        key = pool / "repair-cache-key"
        key.write_text("secret")
        env = {
            **os.environ,
            "HOME": str(self.home),
            "LAKE_CACHE_DIR": str(pool),
            "TAUCETI_REPAIR_CACHE_KEY_FILE": str(key),
        }
        env.pop(repair_sandbox.DISABLE_VARIABLE, None)
        probe = f"cat {secrets}/github-private-key.pem {key} 2>/dev/null; echo done"
        run = subprocess.run(
            repair_sandbox.sandbox_argv(["sh", "-c", probe], self.worktree, env), capture_output=True, text=True
        )
        self.assertEqual(run.stdout.strip(), "done", run.stderr)
        ok, detail = repair_sandbox.check(self.tmp / "scratch", env)
        self.assertTrue(ok, detail)
        # The same key exposed through a symlinked pool is masked, and probed, under the pool's name.
        (self.tmp / "pool-link").symlink_to(pool)
        linked = {**env, "LAKE_CACHE_DIR": str(self.tmp / "pool-link")}
        run = subprocess.run(
            repair_sandbox.sandbox_argv(
                ["sh", "-c", f"cat {self.tmp}/pool-link/repair-cache-key; echo done"], self.worktree, linked
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.stdout.strip(), "done", run.stderr)
        ok, detail = repair_sandbox.check(self.tmp / "scratch", linked)
        self.assertTrue(ok, detail)
        # A credential inside a bound tree that the masks did not cover is reported.
        with patch.object(repair_sandbox, "_credential_masks", return_value=[]):
            ok, detail = repair_sandbox.check(self.tmp / "scratch", env)
        self.assertFalse(ok)
        self.assertIn(str(key), detail)


if __name__ == "__main__":
    unittest.main()
