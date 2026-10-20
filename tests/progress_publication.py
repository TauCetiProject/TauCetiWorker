#!/usr/bin/env python3
"""Retained progress resumes apply with identical bodies/window and original actor."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import progress_publication as publication
from tauceti_worker import work_units as wu
from tauceti_worker.config import Die, NoProgress
from tauceti_worker.constants import EX_NOPROGRESS, PROGRESS_REF, ROADMAP
from tauceti_worker.github_outbox import directory
from tauceti_worker.github_transport import atomic
from tauceti_worker.survey import progress_due

with (
    tempfile.TemporaryDirectory() as tmp,
    patch.dict(
        os.environ,
        {
            "TAUCETI_GITHUB_SHARED_DIR": tmp + "/shared",
            "TAUCETI_WORKER_ID": "worker",
            "TAUCETI_GITHUB_ACCOUNT": "author",
            "GH_TOKEN": "fixture",
        },
    ),
):
    state = Path(tmp)
    cfg = SimpleNamespace(state=state, logdir=state / "logs")
    work = state / "progress/work"
    work.mkdir(parents=True)
    roadmap = state / "progress/roadmap"
    roadmap.mkdir(parents=True)
    real_run = subprocess.run

    def git(*args):
        return real_run(["git", "-C", str(roadmap), *args], capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    git("commit", "--allow-empty", "-qm", "base")
    plan = {
        "roadmap": "Area",
        "from_sha": "a" * 40,
        "to_sha": "b" * 40,
        "status_path": "Area/STATUS.md",
        "progress_path": "Area/PROGRESS.md",
        "prs": [3, 1, 2],
    }
    (work / "plan.json").write_text(json.dumps(plan))
    (work / "status-body.md").write_text("Prepared status prose\n")
    (work / "section-body.md").write_text("Prepared section prose\n")
    original = publication.fingerprints(work)
    branch = "progress/aaaaaaa-bbbbbbb/Area"
    git("checkout", "-qb", branch)
    (roadmap / "Area").mkdir()
    # This fixture is produced by Progress files.render_section with newest-first [3, 1, 2].
    fixture = Path(__file__).parent / "fixtures/progress-retained-section.md"
    (roadmap / plan["progress_path"]).write_text(fixture.read_text())
    git("add", ".")
    git("commit", "-qm", "prepared report")
    # Upgrade from a legacy retained fork, already reconciled before the survey.
    intent = directory() / "fork.complete"
    atomic(
        intent,
        {
            "argv": ["repo", "fork", ROADMAP, "--clone=false", "--remote=false"],
            "cwd": str(state / "worker-process-directory"),
            "created": time.time() - 1000,
            "updated": time.time() - 500,
            "context": {"actor": "author"},
        },
    )
    # A malformed unrelated intent cannot hide valid recovery evidence.
    atomic(directory() / "broken.json", ["invalid"])
    assert publication.retained(cfg)["legacy"]
    writes, calls = [], []
    worker = SimpleNamespace(
        cfg=cfg,
        counters=SimpleNamespace(
            write=lambda *a: writes.append(a), incr=lambda *a: writes.append(a), read=lambda _: 999
        ),
    )

    def publication_run(args, **kwargs):
        calls.append(args)
        assert not (args[:1] == ["git"] and ("checkout" in args or "clean" in args)), (
            "resume must not reset/clean retained source"
        )
        if args[:1] == ["git"] and "show" in args:
            return real_run(args, **kwargs)
        assert "plan" not in args and "facts" not in args and "prompt" not in args
        if "apply" in args:
            assert publication.fingerprints(work) == original
            assert args[args.index("--plan") + 1] == str(work / "plan.json")
            return subprocess.CompletedProcess(args, 1, "", "fork visibility uncertain")
        return subprocess.CompletedProcess(args, 0, "", "")

    # The legacy report bypasses both a latched prose error breaker and attempt gap.
    assert progress_due(cfg, worker.counters)[0]
    with (
        patch.object(wu, "me", return_value="author"),
        patch.object(wu.subprocess, "run", side_effect=publication_run),
        patch.object(wu, "prepare_checkout") as checkout,
        patch.object(wu, "run_agent_host") as model,
    ):
        try:
            wu._do_progress_inner(worker, None)
        except Die:
            pass
        else:
            raise AssertionError("failed apply must retain report")
        model.assert_not_called()
        checkout.assert_not_called()
    assert publication.retained(cfg)["files"] == original
    assert publication.retained(cfg)["version"] == PROGRESS_REF, "first legacy resume pins its version label"
    assert progress_due(cfg, worker.counters)[0] is False, "publication backoff, without paying for prose"

    # Repeated recovery sees the existing PR and retires only the receipt.
    def resumed(args, **kwargs):
        if "apply" in args:
            return subprocess.CompletedProcess(args, EX_NOPROGRESS, "already open, in flight", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    with (
        patch.object(wu, "me", return_value="author"),
        patch.object(wu.subprocess, "run", side_effect=resumed),
        patch.object(wu, "run_agent_host") as model,
    ):
        try:
            wu._do_progress_inner(worker, None)
        except NoProgress:
            pass
        model.assert_not_called()
    assert publication.retained(cfg) is None
    assert publication.fingerprints(work) == original and intent.exists()
    # A changed author cannot publish the same saved report.
    publication.save(cfg, "author", PROGRESS_REF)
    with patch.object(wu, "me", return_value="another"), patch.object(wu.subprocess, "run") as run:
        assert wu._do_progress_inner(worker, None) is None
        run.assert_not_called()
    with patch.dict(os.environ):
        os.environ.pop("TAUCETI_GITHUB_ACCOUNT", None)
        with patch("tauceti_worker.github_outbox.actor", return_value="another"):
            assert progress_due(cfg, worker.counters)[0] is False
    # Infrastructure waits outlast quota resets without spending report attempts.
    for count in range(1, 8):
        receipt = publication.defer(cfg, publication.retained(cfg))
        assert (
            wu._apply_prepared_progress(
                worker, lambda *a, **kw: subprocess.CompletedProcess([], 77, "", "quota wait"), roadmap, work, receipt
            )
            is None
        )
        pending = publication.retained(cfg)
        assert pending["attempts"] == 0 and pending["waits"] == count
        assert publication.fingerprints(work) == original
    assert pending["retry_at"] >= time.time() + 14390
    assert (
        wu._apply_prepared_progress(
            worker, lambda *a, **kw: subprocess.CompletedProcess([], 0, "published", ""), roadmap, work, pending
        )
        == 0
    )
    assert publication.retained(cfg) is None
    publication.save(cfg, "author", PROGRESS_REF)
    # Exhausted recovery parks all artifacts and releases future rounds.
    receipt = publication.retained(cfg)
    atomic(work / "publication.json", {**receipt, "attempts": publication.MAX_ATTEMPTS - 1})
    with (
        patch.object(wu, "me", return_value="author"),
        patch.object(wu.subprocess, "run", side_effect=publication_run),
        patch.object(wu, "run_agent_host") as model,
    ):
        assert wu._do_progress_inner(worker, None) is None
        model.assert_not_called()
    assert publication.retained(cfg) is None and not (work / "publication.json").exists()
    bundles = list((state / "progress/retained").iterdir())
    assert len(bundles) == 1 and publication.fingerprints(bundles[0]) == original
    assert intent.exists()
    assert publication.parked_window(cfg, plan) == bundles[0]
    assert publication.parked_window(cfg, {**plan, "to_sha": "c" * 40}) is None
    # Deterministic/stale reports are parked immediately.
    receipt = publication.save(cfg, "author", PROGRESS_REF)
    assert (
        wu._apply_prepared_progress(
            worker, lambda *a, **kw: subprocess.CompletedProcess([], 78, "stale", ""), roadmap, work, receipt
        )
        is None
    )
    assert not (work / "publication.json").exists()
    receipt = publication.save(cfg, "author", PROGRESS_REF)
    aged = {**receipt, "started": time.time() - publication.MAX_WAIT_AGE - 1, "attempts": 1}
    assert (
        wu._apply_prepared_progress(
            worker, lambda *a, **kw: subprocess.CompletedProcess([], 77, "", "outage"), roadmap, work, aged
        )
        is None
    )
    assert not (work / "publication.json").exists()
    publication.save(cfg, "author", PROGRESS_REF)
    # Malformed receipt fields never wedge survey selection or delete prose.
    marker = work / "publication.json"
    saved = marker.read_text()
    for malformed in ({"files": original}, {"files": original, "actor": "author", "retry_at": "later"}):
        marker.write_text(json.dumps(malformed))
        assert publication.retained(cfg) is None
        assert progress_due(cfg, worker.counters)[0] is False
    marker.write_text(saved)
    plan_bytes = (work / "plan.json").read_bytes()
    (work / "plan.json").write_text("[]")
    assert publication.retained(cfg) is None
    assert progress_due(cfg, worker.counters)[0] is False
    (work / "plan.json").write_bytes(plan_bytes)
    # Receipt corruption or edited bodies preserve everything for recovery.
    (work / "status-body.md").write_text("changed")
    with patch.object(wu.subprocess, "run") as run:
        assert wu._do_progress_inner(worker, None) is None
        run.assert_not_called()
    assert (work / "publication.json").exists()
print("PASS: saved progress resumes publication without model calls, author changes or duplicate reports")
