"""Rounds wait for a TauCeti cache with their own dependencies instead of rebuilding TauCeti.

A fake `lake cache get` behaves like Lake: it walks `git rev-list HEAD -n <max-revs>` (or takes
`--rev`), uses a revision map already in LAKE_CACHE_DIR or a "published" one, and writes it there.
"""

import contextlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, build_cache_wait, cli
from tauceti_worker import work_units as wu
from tauceti_worker.constants import EX_DOCUMENTATION_WAIT

FAKE_LAKE = """#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
maps = Path(os.environ["LAKE_CACHE_DIR"]) / "revisions/TauCeti"
published = set(os.environ.get("FAKE_PUBLISHED", "").split())
def use(rev):
    path = maps / (rev + ".jsonl")
    if path.is_file():
        return True
    if rev not in published:
        return False
    maps.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\\n")
    return True
if "--rev" in a:
    rev = a[a.index("--rev") + 1]
    if use(rev):
        sys.exit(0)
    print("error: TauCetiProject/TauCeti: outputs not found for revision " + rev); sys.exit(1)
n = next(x.split("=", 1)[1] for x in a if x.startswith("--max-revs="))
revs = subprocess.run(["git", "rev-list", "HEAD", *([] if n == "0" else ["-n", n])],
                      capture_output=True, text=True).stdout.split()
if any(use(rev) for rev in revs):
    sys.exit(0)
print("error: TauCetiProject/TauCeti: no outputs found in " + n + " revisions from HEAD"); sys.exit(1)
"""


def manifest(batteries: str) -> str:
    return json.dumps(
        dict(version="1.1.0", name="TauCeti", packages=[dict(name="batteries", rev=batteries, url="https://x/b")])
    )


class BuildCacheWait(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pool = self.root / "pool"
        self.repo = self.root / "TauCeti"
        self.download = self.root / "download"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "lake").write_text(FAKE_LAKE)
        (bin_dir / "lake").chmod(0o755)
        isolation = patch.dict(
            os.environ,
            {
                "TAUCETI_BUILD_CACHE_ROOT": str(self.pool),
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "mathlib-builds"),
                "HOME": str(self.root / "home"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        isolation.start()
        self.addCleanup(isolation.stop)
        assert build_cache_wait.directory() == self.pool / "cache-waits", "unisolated test"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "LAKE_CACHE_DIR": str(self.download),
            build_cache_wait.ENABLE: "1",
        }
        self.logs = []
        for module in (build_cache_wait, agents):
            logger = patch.object(module, "log", side_effect=self.logs.append)
            logger.start()
            self.addCleanup(logger.stop)
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "lakefile.toml").write_text('name = "TauCeti"\n')
        # c1, c2 on the old dependencies; c3 bumps them; c4 builds on the bump.
        self.c1 = self.commit("v4.35.0", "old", "one")
        self.c2 = self.commit("v4.35.0", "old", "two")
        self.c3 = self.commit("v4.36.0", "new", "bump")
        self.c4 = self.commit("v4.36.0", "new", "four")
        self.git("update-ref", "refs/remotes/origin/main", self.c4)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, toolchain, batteries, content):
        (self.repo / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
        (self.repo / "lake-manifest.json").write_text(manifest(batteries))
        (self.repo / "Main.lean").write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", content)
        return self.git("rev-parse", "HEAD")

    def warm(self, *published):
        with patch.dict(self.env, FAKE_PUBLISHED=" ".join(published)):
            agents.warm_host_build_caches(self.repo, self.env, self.root / "logs")

    def marker(self, commit):
        path = self.pool / "cache-waits" / f"{commit}.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def pr_head(self, base, content="pr"):
        self.git("checkout", "-q", "-b", "pr", base)
        return self.commit(*(("v4.36.0", "new") if base in (self.c3, self.c4) else ("v4.35.0", "old")), content)

    # -- preparation-time decision ------------------------------------------------------------

    def test_current_commit_cache_proceeds(self):
        self.warm(self.c4, self.c2)
        self.assertIsNone(self.marker(self.c4))
        self.assertFalse(any("fallback" in line for line in self.logs))

    def test_same_dependency_fallback_proceeds_and_is_logged(self):
        self.warm(self.c3)
        self.assertIsNone(self.marker(self.c4))
        self.assertTrue(any("same-dependency fallback from " + self.c3[:12] in line for line in self.logs))

    def test_fallback_across_a_manifest_change_waits(self):
        with self.assertRaises(agents.BuildCacheWait) as raised:
            self.warm(self.c2)
        self.assertEqual(raised.exception.state, "waiting-build-cache")
        self.assertEqual(raised.exception.commit, self.c4)
        self.assertIn(self.c2[:12], str(raised.exception))
        self.assertIsInstance(raised.exception, agents.BuildCacheUnavailable)  # refunded like an outage
        marker = self.marker(self.c4)
        self.assertEqual((marker["scope"], marker["installed"]), ("main", self.c2))
        self.assertAlmostEqual(marker["retry_at"], time.time() + build_cache_wait.RETRY_SECONDS, delta=30)
        self.assertEqual(raised.exception.retry_at, marker["retry_at"])
        # Once CI publishes the commit, the next round proceeds and the host's wait is cleared.
        self.warm(self.c4, self.c2)
        self.assertIsNone(self.marker(self.c4))

    def test_seeded_maps_are_used_as_lake_uses_them(self):
        # Lake takes the first map present in its walk, seeded or downloaded: c3, not the older c1.
        maps = self.download / "revisions/TauCeti"
        maps.mkdir(parents=True)
        (maps / f"{self.c3}.jsonl").write_text("{}\n")
        (maps / f"{self.c1}.jsonl").write_text("{}\n")
        self.warm()
        self.assertIsNone(self.marker(self.c4))

    def test_pr_round_proceeds_when_mains_cache_matches_its_head(self):
        head = self.pr_head(self.c3)  # new dependencies; its own history is cached only at c1
        self.warm(self.c1, self.c4)
        self.assertIsNone(self.marker(head))

    def test_pr_round_waits_when_neither_cache_matches(self):
        head = self.pr_head(self.c3)
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(self.c1)
        self.assertEqual(self.marker(head)["scope"], "pr")

    def test_rebase_round_compares_with_the_main_files_it_prepares(self):
        head = self.pr_head(self.c2)  # old dependencies, behind main's bump
        for name in ("lake-manifest.json", "lean-toolchain"):  # as shared_mathlib.rebase_target does
            (self.repo / name).write_text(self.git("show", f"{self.c4}:{name}") + "\n")
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(head, self.c1)  # the branch's own cache cannot serve the rebased tree
        self.warm(head, self.c4)
        self.assertIsNone(self.marker(head))

    def test_two_hour_grace_only_for_pr_heads_that_change_dependencies(self):
        old = time.time() - build_cache_wait.PR_GRACE_SECONDS - 60
        # A PR whose head bumps dependencies past main's falls back after the grace.
        self.git("update-ref", "refs/remotes/origin/main", self.c2)
        head = self.pr_head(self.c2, "base")
        head = self.commit("v4.36.0", "newer", "own bump")
        self.write_marker(head, scope="pr", first_wait_at=old)
        self.warm(self.c1)
        self.assertTrue(self.marker(head)["released"])
        self.assertTrue(any("changes dependencies and has waited" in line for line in self.logs))
        # ...but not before the grace has passed.
        self.write_marker(head, scope="pr", first_wait_at=time.time() - 600)
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(self.c1)

    def test_no_grace_for_pr_heads_on_mains_dependencies_or_for_main(self):
        old = time.time() - build_cache_wait.PR_GRACE_SECONDS - 60
        head = self.pr_head(self.c3)
        self.write_marker(head, scope="pr", first_wait_at=old)
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(self.c1)
        self.git("checkout", "-q", "main")
        self.write_marker(self.c4, scope="main", first_wait_at=old)
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(self.c1)
        # One warning per host and commit after an hour.
        warnings = [line for line in self.logs if line.startswith("warning:") and self.c4[:12] in line]
        self.assertEqual(len(warnings), 1)
        with self.assertRaises(agents.BuildCacheWait):
            self.warm(self.c1)
        warnings = [line for line in self.logs if line.startswith("warning:") and self.c4[:12] in line]
        self.assertEqual(len(warnings), 1)

    def test_disabled_outside_worker_agent_rounds(self):
        del self.env[build_cache_wait.ENABLE]
        self.warm(self.c2)
        self.assertIsNone(self.marker(self.c4))

    def test_wait_finishes_preparation_before_raising(self):
        events = []

        @contextlib.contextmanager
        def preparation(cwd, env, **kwargs):
            yield env
            events.append("hydrated")

        def warm(*args, **kwargs):
            raise agents.BuildCacheWait("waiting-build-cache: test", commit=self.c4)

        with (
            patch("tauceti_worker.expanded_cache.preparation", preparation),
            patch.object(agents, "warm_host_build_caches", warm),
            self.assertRaises(agents.BuildCacheWait),
        ):
            agents.prepare_host_build_caches(self.repo, self.env, self.root / "logs")
        self.assertEqual(events, ["hydrated"])

    # -- host-wide marker and selection --------------------------------------------------------

    def write_marker(self, commit, **fields):
        folder = self.pool / "cache-waits"
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        marker = dict(commit=commit, scope="main", toolchain="leanprover/lean4:v4.36.0", first_wait_at=now)
        marker.update({"retry_at": now + 300, **fields})
        (folder / f"{commit}.json").write_text(json.dumps(marker))

    def test_fresh_marker_makes_selection_skip(self):
        self.write_marker(self.c4)
        self.write_marker(self.c3, scope="pr")
        with patch.object(build_cache_wait, "published", return_value=False):
            with self.assertRaises(agents.BuildCacheWait) as raised:
                wu.dispatch(
                    "roadmap",
                    None,
                    None,
                    SimpleNamespace(pr=0, head="", reason=""),
                    SimpleNamespace(dry_run=False, sandbox_host=True),
                )
            self.assertEqual(raised.exception.commit, self.c4)
            fix = SimpleNamespace(pr=7, head=self.c3)
            self.assertIsNone(wu.dispatch("fix", None, None, fix, SimpleNamespace(dry_run=False, sandbox_host=True)))
            self.assertIsNone(wu._build_cache_deferral("fix", SimpleNamespace(pr=8, head=self.c2)))
            self.assertIsNone(wu._build_cache_deferral("review", fix))

    def test_expired_marker_allows_one_real_check(self):
        self.write_marker(self.c4, retry_at=time.time() - 1)
        probe = []
        self.assertIsNone(build_cache_wait.deferral(scope="main", probe=probe.append))
        self.assertIsNotNone(build_cache_wait.deferral(scope="main", probe=lambda *a: False))
        self.assertEqual(probe, [])

    def test_published_map_clears_the_marker(self):
        self.write_marker(self.c4)
        self.assertIsNone(build_cache_wait.deferral(scope="main", probe=lambda *a: True))
        self.assertIsNone(self.marker(self.c4))

    def test_probes_are_throttled_per_host(self):
        self.write_marker(self.c4)
        probes = []

        def probe(*args):
            probes.append(args)
            return False

        self.assertIsNotNone(build_cache_wait.deferral(scope="main", probe=probe))
        self.assertIsNotNone(build_cache_wait.deferral(scope="main", probe=probe))
        self.assertEqual(probes, [(self.c4, "leanprover/lean4:v4.36.0")])

    def test_unreadable_marker_fails_open(self):
        folder = self.pool / "cache-waits"
        folder.mkdir(parents=True)
        (folder / f"{self.c4}.json").write_text("{not json")
        self.assertIsNone(build_cache_wait.deferral(self.c4))
        with patch.object(build_cache_wait, "_locked", side_effect=OSError("read-only")):
            self.write_marker(self.c3, scope="pr")
            self.assertIsNone(build_cache_wait.deferral(self.c3))

    def test_revision_url_and_http_probe(self):
        self.assertEqual(
            build_cache_wait.revision_url(self.c4, "leanprover/lean4:v4.36.0-rc2\n"),
            "https://cache.taucetiproject.org/revisions/TauCetiProject/TauCeti/tc/"
            f"leanprover--lean4---v4.36.0-rc2/{self.c4}.jsonl",
        )
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                seen.append(self.path)
                self.send_response(200 if self.path.endswith(f"/{self_test.c4}.jsonl") else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self_test = self
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}/revisions"
        with patch.object(agents, "TAUCETI_CACHE_REVISION_URL", base):
            self.assertTrue(build_cache_wait.published(self.c4, "leanprover/lean4:v4.36.0"))
            self.assertFalse(build_cache_wait.published(self.c3, "leanprover/lean4:v4.36.0"))
        self.assertEqual(seen[0], f"/revisions/TauCetiProject/TauCeti/tc/leanprover--lean4---v4.36.0/{self.c4}.jsonl")

    # -- round outcome --------------------------------------------------------------------------

    def test_wait_exits_as_a_deferral_without_failure(self):
        error = agents.BuildCacheWait("waiting-build-cache: main abc", retry_at=123.0, commit=self.c4)
        with (
            patch.object(cli, "main", side_effect=error),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(cli, "report_runtime") as status,
            patch.object(cli, "report_failure") as failure,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.cli_main(), EX_DOCUMENTATION_WAIT)
        failure.assert_not_called()
        self.assertEqual(status.call_args.args, ("waiting-build-cache",))
        self.assertEqual(status.call_args.kwargs["next_action_at"], 123.0)
        self.assertIsNone(status.call_args.kwargs["failure_reason"])
        self.assertIn("main abc", status.call_args.kwargs["detail"])

    def test_wait_refunds_the_pr_attempt(self):
        co = self.root / "checkout"
        co.mkdir()
        worker = SimpleNamespace(
            cfg=SimpleNamespace(checkout=co, logdir=self.root / "logs", state=self.root / "state"),
            claims=SimpleNamespace(begin_branch_work=lambda *a: True),
            rs=SimpleNamespace(bust=lambda *a: None),
        )
        worker.counters = wu.Counters(worker.cfg)
        survey = SimpleNamespace(
            open_prs=[SimpleNamespace(number=1434, head_owner="fork", head_repo="TauCeti", head_ref="branch")]
        )
        real_run = subprocess.run

        def checkout(command, **kwargs):
            if command[:3] == ["gh", "pr", "checkout"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
            return real_run(command, **kwargs)

        def agent(*args, **kwargs):
            raise agents.BuildCacheWait("waiting-build-cache: PR head", commit="a" * 40)

        worker.counters.write("ci-pr-1434", 1)
        with (
            patch.object(wu, "prepare_checkout", return_value=True),
            patch.object(wu, "run_agent_host", agent),
            patch.object(agents.subprocess, "run", checkout),
            patch.dict(os.environ, TAUCETI_ROUND_WORKTREES="0"),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(agents.BuildCacheWait),
        ):
            wu._do_fixlike(
                worker,
                survey,
                SimpleNamespace(pr=1434, head="a" * 40),
                SimpleNamespace(agent_name="Claude", work_model="claude"),
                False,
                prompt_file="fix-ci.md",
                label="fix-ci",
                charged=("ci-pr-1434",),
            )
        self.assertEqual(worker.counters.read("ci-pr-1434"), 0)


if __name__ == "__main__":
    unittest.main()
