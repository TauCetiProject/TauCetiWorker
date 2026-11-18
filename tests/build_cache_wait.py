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

    def markers(self, commit):
        folder = self.pool / "cache-waits"
        found = [json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []
        return [marker for marker in found if marker.get("commit") == commit]

    def marker(self, commit):
        found = self.markers(commit)
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None

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

    def write_marker(self, commit, name=None, **fields):
        """A wait as `check` would record it for the checkout as it stands (unless `name` says)."""
        folder = self.pool / "cache-waits"
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        marker = dict(
            commit=commit,
            scope="main",
            stages=["*"],
            need=commit,
            toolchain="leanprover/lean4:v4.36.0",
            first_wait_at=now,
        )
        marker.update({"retry_at": now + 300, **fields})
        name = name or build_cache_wait.key(commit, build_cache_wait.working(self.repo))
        (folder / f"{name}.json").write_text(json.dumps(marker))

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
        with patch.object(build_cache_wait, "_claimant", return_value="round-a"):
            self.assertIsNone(build_cache_wait.deferral(scope="main", probe=probe.append))
        with patch.object(build_cache_wait, "_claimant", return_value="round-b"):
            self.assertIsNotNone(build_cache_wait.deferral(scope="main", probe=lambda *a: False))
        self.assertEqual(probe, [])

    def test_a_declined_claim_never_consumes_the_real_check(self):
        """Selection claims an expired wait briefly; only cache preparation re-arms it."""
        retry_at = time.time() - 1
        self.write_marker(self.c4, retry_at=retry_at)
        overlap = SimpleNamespace(pr=0, head="", reason="")
        opts = SimpleNamespace(dry_run=False, sandbox_host=True)
        for expiry in range(3):
            now = time.time() + expiry * (build_cache_wait.CLAIM_SECONDS + 1)
            with (
                patch.object(build_cache_wait.time, "time", return_value=now),
                patch.object(build_cache_wait, "_claimant", return_value=f"decliner-{expiry}"),
            ):
                # overlap-fix takes the claim, then declines later in dispatch.
                self.assertIsNone(wu._build_cache_deferral("overlap-fix", overlap))
                self.assertEqual(self.marker(self.c4)["retry_at"], retry_at)
                # The same round's roadmap still goes through to the real check.
                self.assertIsNone(wu._build_cache_deferral("roadmap", overlap))
            with (
                patch.object(build_cache_wait.time, "time", return_value=now),
                patch.object(build_cache_wait, "_claimant", return_value="other"),
                patch.object(build_cache_wait, "published", return_value=False),
                self.assertRaises(agents.BuildCacheWait),
            ):
                wu.dispatch("roadmap", None, None, overlap, opts)  # claimed elsewhere: waits
        # After the decliners' claims lapse, another round's roadmap gets the real check...
        later = time.time() + 3 * (build_cache_wait.CLAIM_SECONDS + 1)
        with (
            patch.object(build_cache_wait.time, "time", return_value=later),
            patch.object(build_cache_wait, "_claimant", return_value="roadmap-round"),
        ):
            self.assertIsNone(wu._build_cache_deferral("roadmap", overlap))
        self.assertEqual(self.marker(self.c4)["retry_at"], retry_at)
        # ...which preparation re-arms when the cache still does not match.
        with patch.object(build_cache_wait, "_claimant", return_value="roadmap-round"):
            with self.assertRaises(agents.BuildCacheWait):
                self.warm(self.c2)
        marker = self.marker(self.c4)
        self.assertGreater(marker["retry_at"], time.time())
        self.assertNotIn("claimant", marker)

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

    def test_rebase_waits_for_mains_cache_not_the_heads(self):
        head = self.pr_head(self.c2)  # old dependencies; its own cache is published
        for name in ("lake-manifest.json", "lean-toolchain"):  # as shared_mathlib.rebase_target does
            (self.repo / name).write_text(self.git("show", f"{self.c4}:{name}") + "\n")
        start = time.time() - build_cache_wait.WARN_SECONDS - 60
        with (
            patch.dict(os.environ, TAUCETI_GITHUB_PHASE="rebase"),
            patch.object(build_cache_wait.time, "time", return_value=start),
            self.assertRaises(agents.BuildCacheWait) as raised,
        ):
            self.warm(head, self.c1)
        self.assertIn(f"needs {self.c4[:12]}", str(raised.exception))
        marker = self.marker(head)
        self.assertEqual((marker["need"], marker["toolchain"]), (self.c4, "leanprover/lean4:v4.36.0"))
        self.assertEqual(marker["stages"], ["rebase"])
        # The head's own published map does not end the wait: the probe asks for main's.
        probes = []

        def probe(rev, toolchain):
            probes.append((rev, toolchain))
            return rev == head

        rebase = SimpleNamespace(pr=7, head=head)
        self.assertIsNotNone(build_cache_wait.deferral(head, stage="rebase", now=start + 10, probe=probe))
        self.assertEqual(probes, [(self.c4, "leanprover/lean4:v4.36.0")])
        self.assertIsNotNone(self.marker(head))
        # A fix of the same head prepares its own dependencies and is not held by the rebase's wait.
        with (
            patch.object(build_cache_wait, "published", return_value=False),
            patch.object(build_cache_wait.time, "time", return_value=start + 20),
        ):
            self.assertIsNone(wu._build_cache_deferral("fix", rebase))
            self.assertIsNotNone(wu._build_cache_deferral("rebase", rebase))
        # The next rebase round keeps the first wait time, so the one-hour warning fires.
        with patch.dict(os.environ, TAUCETI_GITHUB_PHASE="rebase"), self.assertRaises(agents.BuildCacheWait):
            self.warm(head, self.c1)
        self.assertEqual(self.marker(head)["first_wait_at"], start)
        warnings = [line for line in self.logs if line.startswith("warning:") and head[:12] in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.c4, warnings[0])  # names the map CI should publish: main's

    def test_unwritable_markers_let_the_round_proceed(self):
        # A PR whose head changes dependencies: with no persisted wait the 2h grace could never run out.
        self.git("update-ref", "refs/remotes/origin/main", self.c2)
        self.pr_head(self.c2, "base")
        self.commit("v4.36.0", "newer", "own bump")
        with patch.object(build_cache_wait, "_locked", side_effect=OSError("read-only")):
            for hours in (0, 1, 3):
                with patch.object(build_cache_wait.time, "time", return_value=time.time() + hours * 3600):
                    self.warm(self.c1)
            self.git("checkout", "-q", "main")
            self.warm(self.c2)  # a main-scope wait proceeds too
        self.assertFalse((self.pool / "cache-waits").exists() and list((self.pool / "cache-waits").glob("*.json")))
        self.assertTrue(any("dependency check skipped" in line for line in self.logs))

    def test_unreadable_marker_fails_open(self):
        folder = self.pool / "cache-waits"
        folder.mkdir(parents=True)
        (folder / f"{self.c4}-x.json").write_text("{not json")
        self.assertIsNone(build_cache_wait.deferral(self.c4, stage="fix"))
        with patch.object(build_cache_wait, "_locked", side_effect=OSError("read-only")):
            self.write_marker(self.c3, scope="pr")
            self.assertIsNone(build_cache_wait.deferral(self.c3, stage="fix"))

    def own_bump_pr(self):
        """A PR whose head changes dependencies past main's: the case with a 2h grace."""
        self.git("update-ref", "refs/remotes/origin/main", self.c2)
        self.pr_head(self.c2, "base")
        return self.commit("v4.36.0", "newer", "own bump")

    def test_unreadable_existing_marker_never_restarts_the_wait(self):
        head = self.own_bump_pr()
        name = build_cache_wait.key(head, build_cache_wait.working(self.repo))
        path = self.pool / "cache-waits" / f"{name}.json"
        start = time.time() - 600
        self.write_marker(head, scope="pr", first_wait_at=start)
        real_read = Path.read_text

        def failing_read(self_path, *args, **kwargs):
            if self_path == path:
                raise PermissionError("unreadable")
            return real_read(self_path, *args, **kwargs)

        # Reading fails while writing would succeed: every round proceeds, even past 2h.
        for hours in (0, 1, 3):
            with (
                patch.object(build_cache_wait.time, "time", return_value=time.time() + hours * 3600),
                patch.object(Path, "read_text", failing_read),
            ):
                self.warm(self.c1)
        self.assertEqual(json.loads(path.read_text())["first_wait_at"], start)
        # A corrupt marker is left alone the same way, and does not block selection either.
        path.write_text("{not json")
        for hours in (0, 3):
            with patch.object(build_cache_wait.time, "time", return_value=time.time() + hours * 3600):
                self.warm(self.c1)
        self.assertEqual(path.read_text(), "{not json")
        self.assertIsNone(build_cache_wait.deferral(head, stage="fix"))
        self.assertTrue(any("dependency check skipped" in line for line in self.logs))

    def test_sweep_collects_old_waits_whatever_their_head_or_release(self):
        folder = self.pool / "cache-waits"
        now = time.time()
        old = now - build_cache_wait.FORGET_SECONDS - 60
        self.write_marker(self.c1, name="closed-pr", scope="pr", first_wait_at=old)
        self.write_marker(self.c2, name="released-old", scope="pr", first_wait_at=old, released=True)
        self.write_marker(self.c3, name="released-recent", scope="pr", first_wait_at=now - 86400, released=True)
        self.write_marker(self.c4, name="fresh-main", first_wait_at=now)
        (folder / "corrupt-old.json").write_text("{not json")
        os.utime(folder / "corrupt-old.json", (old, old))
        (folder / "corrupt-recent.json").write_text("{not json")
        # A selection for an unrelated head still sweeps the whole folder.
        with patch.object(build_cache_wait, "published", return_value=False):
            build_cache_wait.deferral("f" * 40, stage="fix", now=now)
        self.assertEqual(
            sorted(path.stem for path in folder.glob("*.json")),
            ["corrupt-recent", "fresh-main", "released-recent"],
        )
        # At most once per interval per host: a new stale marker waits for the next sweep.
        self.write_marker(self.c1, name="closed-later", scope="pr", first_wait_at=old)
        self.assertEqual(build_cache_wait.sweep(now=now + 60), 0)
        self.assertTrue((folder / "closed-later.json").exists())
        self.assertEqual(build_cache_wait.sweep(now=now + build_cache_wait.SWEEP_SECONDS + 1), 1)
        self.assertFalse((folder / "closed-later.json").exists())
        # A damaged stamp means sweep now; a folder that cannot be locked is ignored.
        (folder / ".swept").write_text("garbage")
        self.write_marker(self.c1, name="closed-again", scope="pr", first_wait_at=old)
        self.assertEqual(build_cache_wait.sweep(now=now + 2 * build_cache_wait.SWEEP_SECONDS), 1)
        with patch.object(build_cache_wait, "_locked", side_effect=OSError("read-only")):
            self.assertEqual(build_cache_wait.sweep(now=now + 9 * build_cache_wait.SWEEP_SECONDS), 0)

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
