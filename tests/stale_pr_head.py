"""A PR whose recorded head lags its branch tip is released before any push, then backed off."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.review_runtime_stub import install_runtime_stub

install_runtime_stub()

from tauceti_worker import state_repair
from tauceti_worker import work_units as work
from tauceti_worker.constants import TAUCETI
from tauceti_worker.github import GitHub
from tauceti_worker.survey import Candidate, Counters

BRANCH = "roadmap/feature"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class StalePrHead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A head repository with two commits: the PR record names the first, the branch holds the second.
        self.remote = self.root / "head.git"
        git("init", "-q", "--bare", str(self.remote))
        work_tree = self.root / "src"
        git("init", "-q", str(work_tree))
        identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
        git(*identity, "commit", "-q", "--allow-empty", "-m", "recorded", cwd=work_tree)
        self.recorded = git("rev-parse", "HEAD", cwd=work_tree)
        git(*identity, "commit", "-q", "--allow-empty", "-m", "tip", cwd=work_tree)
        self.tip = git("rev-parse", "HEAD", cwd=work_tree)
        git("push", "-q", str(self.remote), f"HEAD:refs/heads/{BRANCH}", cwd=work_tree)

        self.cfg = SimpleNamespace(state=self.root, logdir=self.root, store_dir=self.root / "store", wid="test")
        self.counters = Counters(self.cfg)
        env = patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        for key in ("TAUCETI_PUSH_REMOTE", "TAUCETI_PUSH_REF"):
            os.environ.pop(key, None)

    def worker(self):
        def begin_branch_work(pr, head, refname, owner, repo):
            # Both forges' claims export the push arbiter's destination this way.
            os.environ.update(TAUCETI_PUSH_REMOTE=str(self.remote), TAUCETI_PUSH_REF=refname)
            return True

        return SimpleNamespace(
            cfg=self.cfg,
            counters=self.counters,
            gh=GitHub(TAUCETI),
            claims=SimpleNamespace(begin_branch_work=begin_branch_work, held=None),
            rs=SimpleNamespace(bust=Mock()),
        )

    def test_branch_tip_reads_the_real_ref(self):
        self.assertEqual(work._branch_tip(str(self.remote), BRANCH), self.tip)
        self.assertEqual(work._branch_tip(str(self.remote), "refs/heads/" + BRANCH), self.tip)
        self.assertEqual(work._branch_tip(str(self.remote), "absent"), "")
        self.assertIsNone(work._branch_tip(str(self.root / "missing.git"), BRANCH))

    def test_matching_head_proceeds(self):
        w = self.worker()
        os.environ.update(TAUCETI_PUSH_REMOTE=str(self.remote), TAUCETI_PUSH_REF=BRANCH)
        with patch.object(state_repair, "repair_selected") as repair:
            self.assertFalse(work._stale_pr_head(w, Candidate(7, self.tip), "rebase"))
        repair.assert_not_called()

    def test_unknown_tip_proceeds(self):
        # An unreadable head repository is not evidence of staleness; the push CAS still protects.
        w = self.worker()
        os.environ.update(TAUCETI_PUSH_REMOTE=str(self.root / "missing.git"), TAUCETI_PUSH_REF=BRANCH)
        with patch.object(state_repair, "repair_selected") as repair:
            self.assertFalse(work._stale_pr_head(w, Candidate(7, self.recorded), "rebase"))
        repair.assert_not_called()

    def test_stale_head_is_released_before_launch_and_backed_off(self):
        w = self.worker()
        p = SimpleNamespace(number=7, head_owner="fork", head_repo="TauCeti", head_ref=BRANCH, labels=())
        sv = SimpleNamespace(open_prs=[p])
        c = Candidate(7, self.recorded)
        key = "rebase-pr-7"
        self.counters.write(key, 1)  # charged by do_rebase before the claim
        logs = []
        with (
            patch.object(work, "run_in_bubble") as bubble,
            patch.object(work, "run_agent_host") as host,
            patch.object(work, "prepare_checkout") as checkout,
            patch.object(work, "log", side_effect=logs.append),
        ):
            rc = work._do_fixlike(
                w, sv, c, SimpleNamespace(prs=()), False, prompt_file="rebase.md", label="rebase", charged=(key,)
            )
        self.assertIsNone(rc)
        bubble.assert_not_called()
        host.assert_not_called()
        checkout.assert_not_called()
        self.assertEqual(self.counters.read(key), 0, "an attempt that never launched is not charged")
        self.assertTrue(
            any(
                f"PR head is stale: the forge has {self.recorded[:12]}, branch tip is {self.tip[:12]}" in m
                for m in logs
            ),
            logs,
        )
        # The existing per-head source-refresh retry keeps selection off this head for now.
        self.assertGreater(self.counters.read(state_repair.retry_key(7, self.recorded)), time.time())
        self.assertTrue(w.selection_changed)
        # The real tip has its own key, so selection is free to pick it as soon as the forge reports it.
        self.assertEqual(self.counters.read(state_repair.retry_key(7, self.tip)), 0)

    def test_rejected_push_during_the_round_backs_off(self):
        # The tip matched at launch; a peer moved it while the agent worked, so its CAS push was rejected.
        co = self.root / "checkout"
        git("clone", "-q", str(self.remote), str(co))
        git("checkout", "-q", "--detach", self.recorded, cwd=co)
        git("push", "-q", "--force", str(self.remote), f"{self.recorded}:refs/heads/{BRANCH}", cwd=co)
        self.cfg.checkout = co
        w = self.worker()
        p = SimpleNamespace(number=7, head_owner="fork", head_repo="TauCeti", head_ref=BRANCH, labels=())
        sv = SimpleNamespace(open_prs=[p])
        c = Candidate(7, self.recorded)
        real_run = subprocess.run

        def run(command, **kwargs):
            if command[:3] == ["gh", "pr", "checkout"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(command, **kwargs)

        def agent(cwd, *args, **kwargs):
            git("push", "-q", "--force", str(self.remote), f"{self.tip}:refs/heads/{BRANCH}", cwd=cwd)
            scope = os.environ["TAUCETI_PUSH_SCOPE"]
            git("update-ref", f"refs/tauceti/push-rejected-scope/{scope}/{BRANCH}", self.recorded, cwd=cwd)
            return 1

        with (
            patch.object(work, "prepare_checkout", return_value=True),
            patch.object(work, "run_agent_host", agent),
            patch.object(work, "_refund_infra_failure"),
            patch.object(work.subprocess, "run", run),
        ):
            work._do_fixlike(
                w,
                sv,
                c,
                SimpleNamespace(prs=(), agent_name="Claude", work_model="claude"),
                False,
                prompt_file="fix-ci.md",
                label="fix-ci",
            )
        self.assertGreater(self.counters.read(state_repair.retry_key(7, self.recorded)), time.time())


if __name__ == "__main__":
    unittest.main()
