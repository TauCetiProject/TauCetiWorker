#!/usr/bin/env python3
"""Unfinished source is saved before checkout preparation destroys it.

Pinned here, with local git fixtures only (no GitHub, no agents):

  1. A clean checkout whose commits are all published needs no record and is reset as before.
  2. Untracked/new files, staged versus unstaged edits, deletions, binary content, symlinks (inside and
     pointing outside the checkout), and local commits after the expected remote head are saved in a
     complete tauceti-work-recovery/v1 record, and a fresh clone reconstructs the exact source bytes.
  3. Dirty source with unknown quiescence is retained and recovery-blocked; repeated preparation
     reuses the record rather than writing another. Operator confirmation for the exact source lets
     the reset proceed and the record is retained.
  4. Commits-only leftovers are bundled once and preparation proceeds; commits marked published are not.
  5. A merge in progress with a conflicted path is saved and its bytes reconstructed.
  6. Interrupted saves, no-space and permission errors leave the checkout intact and explain why.
  7. A corrupt manifest is never reused or deleted; summaries count it.
  8. A live process using the checkout blocks the reset even after operator confirmation.
  9. Dependency caches and credential-like files are excluded by name.
 10. Host work (`prepare_checkout`, roadmap) and progress preparation, which runs on the host for
     sandboxed workers too, refuse without starting the agent or the destructive git commands.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import errno
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import work_recovery as wr

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def git(repo, *args, check_rc=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check_rc and p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


def identity(repo):
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")


def fixture(root: Path):
    """An `origin` bare repository with main, and a worker checkout cloned from it."""
    seed = root / "seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    identity(seed)
    (seed / ".gitignore").write_text(".lake/\n*.olean\n")
    (seed / "A.lean").write_text("theorem a : True := trivial\n")
    (seed / "B.lean").write_text("theorem b : True := trivial\n")
    (seed / "gone.lean").write_text("delete me\n")
    (seed / "blob.bin").write_bytes(bytes(range(256)) * 4)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    origin = root / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    co = root / "checkouts" / "w" / "TauCeti"
    co.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
    identity(co)
    cfg = SimpleNamespace(wid="w", state=root / "state" / "w", checkout=co, data_home=root / "home")
    return origin, co, cfg


def snapshot_tree(repo: Path) -> dict:
    """Every worktree path outside .git and ignored caches: bytes for files, target for links."""
    out = {}
    for dirpath, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = [d for d in dirs if d not in (".git", ".lake") and not os.path.islink(os.path.join(dirpath, d))]
        names = files + [d for d in os.listdir(dirpath) if os.path.islink(os.path.join(dirpath, d)) and d not in files]
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, repo)
            if rel.endswith(".olean") or rel == ".env":
                continue
            out[rel] = ("link", os.readlink(path)) if os.path.islink(path) else ("file", Path(path).read_bytes())
    return out


def reconstruct(origin: Path, record: Path, root: Path, name: str) -> Path:
    clone = root / name
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    identity(clone)
    wr.restore(record, clone)
    return clone


def make_dirty(co: Path, outside: Path):
    # A local commit after the expected remote head.
    (co / "A.lean").write_text("theorem a : True := by trivial\n")
    git(co, "commit", "-qam", "local work")
    # Staged versus unstaged edits to the same file, a staged deletion, a staged new binary file.
    (co / "B.lean").write_text("staged version\n")
    git(co, "add", "B.lean")
    (co / "B.lean").write_text("staged version\nplus unstaged line\n")
    git(co, "rm", "-q", "gone.lean")
    (co / "new.bin").write_bytes(b"\x00\xff\x10binary\x00" * 50)
    git(co, "add", "new.bin")
    (co / "blob.bin").write_bytes(bytes(reversed(range(256))) * 4)  # unstaged binary edit
    # Untracked source, nested directory, symlinks inside and pointing outside the checkout.
    (co / "Notes").mkdir()
    (co / "Notes" / "Draft.lean").write_text("-- unfinished proof\n")
    os.symlink("Draft.lean", co / "Notes" / "link-inside")
    os.symlink(str(outside), co / "link-outside")
    # Excluded: ignored caches, a dependency cache, and a credential-like file.
    (co / ".lake").mkdir()
    (co / ".lake" / "build.olean").write_bytes(b"cache")
    (co / "node_modules").mkdir()
    (co / "node_modules" / "dep.js").write_text("cache\n")
    (co / ".env").write_text("SECRET=1\n")


def test_clean_and_published(root):
    origin, co, cfg = fixture(root / "clean")
    d = wr.guard(cfg, co)
    check("clean published checkout: allowed without a record", d.ok and d.record is None and d.state == "clean")
    check("clean: no recovery directory", not (cfg.state / "recovery").exists())
    # A commit the worker observed on a remote (PR head) or pushed through git-safe-push is published.
    (co / "A.lean").write_text("pushed\n")
    git(co, "commit", "-qam", "pushed")
    wr.mark_published(co, "push/feature", git(co, "rev-parse", "HEAD").strip())
    d = wr.guard(cfg, co)
    check("commit marked published: no record", d.ok and d.record is None)


def test_full_record(root):
    origin, co, cfg = fixture(root / "full")
    outside = root / "outside-secret"
    outside.write_text("must not be archived\n")
    expected = git(co, "rev-parse", "HEAD").strip()
    wr.note_owner(cfg, round_id="round-1", work="fix", pr=42, expected_head=expected, roadmap=None)
    make_dirty(co, outside)
    before_tree = snapshot_tree(co)
    before_status = git(co, "status", "--porcelain=v2", "-uall")
    before_head = git(co, "rev-parse", "HEAD").strip()

    d = wr.guard(cfg, co)
    check("dirty + unknown quiescence: refused", not d.ok and d.state == "recovery-blocked")
    check("refusal is actionable", "tauceti recovery confirm round-1" in d.reason and "retained" in d.reason)
    check("refusal reason exposed for callers", wr.LAST_REFUSAL == d.reason)
    check("checkout untouched after refusal", git(co, "status", "--porcelain=v2", "-uall") == before_status)
    record = cfg.state / "recovery" / "round-1"
    check("record directory named for the source round", d.record == record and record.is_dir())
    manifest = json.loads((record / "manifest.json").read_text())
    check("manifest schema", manifest["schema"] == "tauceti-work-recovery/v1")
    check(
        "manifest identity",
        manifest["pr"] == 42
        and manifest["work"] == "fix"
        and manifest["expected_remote_head"] == expected
        and manifest["local_head"] == before_head
        and manifest["source_round"] == "round-1"
        and manifest["base"] == expected
        and manifest["repository"] == "TauCetiProject/TauCeti"
        and manifest["publication"]["state"] == "unpublished"
        and manifest["publication"]["unpublished_commits"] == 1
        and isinstance(manifest["created_at"], float)
        and manifest["fingerprint"],
    )
    for name in ("commits.bundle", "staged.patch", "unstaged.patch", "untracked.tar"):
        check(f"record has {name}", (record / name).is_file() and manifest["files"][name]["bytes"] >= 0)
    excluded = {e["path"]: e["reason"] for e in manifest["excluded"]}
    check("credential-like file excluded by name", ".env" in excluded and "secret" in excluded[".env"])
    check("dependency cache excluded", any(p.startswith("node_modules") for p in excluded))
    check("ignored build cache never listed", not any(p.startswith(".lake") for p in manifest["untracked"]))
    import tarfile

    with tarfile.open(record / "untracked.tar") as tar:
        members = {m.name: m for m in tar.getmembers()}
    check("outside link archived as a link", members["link-outside"].issym())
    check("outside target content not archived", b"must not be archived" not in (record / "untracked.tar").read_bytes())
    check("status recovery-blocked", json.loads((record / "status.json").read_text())["state"] == "recovery-blocked")
    check("summary counts the blocked record", wr.summary(root / "full" / "state")["blocked"] == 1)

    clone = reconstruct(origin, record, root / "full", "rebuilt")
    rebuilt = snapshot_tree(clone)
    expected_tree = {k: v for k, v in before_tree.items() if not k.startswith("node_modules")}
    check("reconstructed source bytes are exact", rebuilt == expected_tree)
    check("reconstructed HEAD is the local commit", git(clone, "rev-parse", "HEAD").strip() == before_head)
    check(
        "reconstructed index matches staged state",
        git(clone, "diff", "--cached", "--binary") == git(co, "diff", "--cached", "--binary"),
    )

    # Re-preparation of the unchanged checkout reuses the durable record.
    d2 = wr.guard(cfg, co)
    check("repeat: still refused", not d2.ok)
    check("repeat: record reused", d2.record == record and len(list((cfg.state / "recovery").iterdir())) == 1)

    # Rollback switch: no new records, but the blocked checkout is still not reset.
    os.environ["TAUCETI_WORK_RECOVERY"] = "off"
    try:
        d_off = wr.guard(cfg, co)
        other = fixture(root / "rollback")
        (other[1] / "Draft.lean").write_text("x\n")
        d_other = wr.guard(other[2], other[1])
    finally:
        del os.environ["TAUCETI_WORK_RECOVERY"]
    check("rollback: pending recovery still refused", not d_off.ok and "pending recovery" in d_off.reason)
    check("rollback: no record for checkouts without pending recovery", d_other.ok and d_other.state == "disabled")

    # Operator confirms quiescence for exactly this source; the reset may then proceed.
    print("  " + wr.confirm(cfg, "round-1"))
    d3 = wr.guard(cfg, co)
    check("confirmed: allowed", d3.ok and d3.state == "checkout-reset" and d3.record == record)
    # Once the source changes, the confirmation no longer applies.
    (co / "Notes" / "Draft.lean").write_text("-- changed after confirmation\n")
    d4 = wr.guard(cfg, co)
    check("changed after confirmation: new record and blocked", not d4.ok and d4.record != record)
    check("superseded record retained", record.is_dir() and (record / "manifest.json").is_file())
    check(
        "older blocked record superseded",
        json.loads((record / "status.json").read_text())["state"] in ("superseded", "checkout-reset"),
    )


def test_commits_only(root):
    origin, co, cfg = fixture(root / "commits")
    wr.note_owner(cfg, round_id="round-c", work="roadmap", roadmap="algebra")
    git(co, "checkout", "-q", "-b", "roadmap/x")
    (co / "A.lean").write_text("one\n")
    git(co, "commit", "-qam", "one")
    (co / "A.lean").write_text("two\n")
    git(co, "commit", "-qam", "two")
    tip = git(co, "rev-parse", "HEAD").strip()
    git(co, "checkout", "-q", "--detach", "HEAD~1")
    (co / "B.lean").write_text("detached\n")
    git(co, "commit", "-qam", "detached")
    detached = git(co, "rev-parse", "HEAD").strip()
    d = wr.guard(cfg, co)
    check("commits only: allowed after saving", d.ok and d.state == "preserved" and d.record is not None)
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("commits only: roadmap recorded", manifest["work"] == "roadmap" and manifest["roadmap"] == "algebra")
    check(
        "commits only: bundle names branch and detached head",
        set(manifest["bundle_refs"]) >= {"HEAD", "refs/heads/roadmap/x"},
    )
    check(
        "commits now marked preserved",
        git(co, "rev-parse", f"refs/tauceti/preserved/{d.record.name}/refs/heads/roadmap/x").strip() == tip,
    )
    check("no pending refs left", git(co, "for-each-ref", "refs/tauceti-pending") == "")
    d2 = wr.guard(cfg, co)
    check("commits only: not bundled twice", d2.ok and d2.record is None)
    clone = reconstruct(origin, d.record, root / "commits", "rebuilt")
    check("bundle restores the detached commit", git(clone, "rev-parse", "HEAD").strip() == detached)
    check("bundle restores the branch commits", git(clone, "cat-file", "-t", tip).strip() == "commit")


def test_conflicted_merge(root):
    origin, co, cfg = fixture(root / "merge")
    seed = root / "merge" / "seed"
    (seed / "A.lean").write_text("upstream\n")
    git(seed, "commit", "-qam", "upstream")
    git(seed, "push", "-q", str(origin), "main")
    (co / "A.lean").write_text("local\n")
    git(co, "commit", "-qam", "local")
    git(co, "fetch", "-q")
    git(co, "merge", "origin/main", check_rc=False)
    check("fixture: merge conflicted", "UU A.lean" in git(co, "status", "--porcelain"))
    before = snapshot_tree(co)
    d = wr.guard(cfg, co)
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("conflicted merge: blocked and saved", not d.ok and manifest["conflicted"] == ["A.lean"])
    check("merge operation recorded", "MERGE_HEAD" in manifest["operations"])
    clone = reconstruct(origin, d.record, root / "merge", "rebuilt")
    check("conflicted bytes reconstructed", snapshot_tree(clone) == before)


def test_failures(root):
    origin, co, cfg = fixture(root / "fail")
    make_dirty(co, root / "fail" / "nowhere")
    status = git(co, "status", "--porcelain=v2", "-uall")
    refs = git(co, "for-each-ref")

    real_archive = wr._archive

    def no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    wr._archive = no_space
    try:
        d = wr.guard(cfg, co)
    finally:
        wr._archive = real_archive
    check("no space: refused", not d.ok and "No space left on device" in d.reason and "left intact" in d.reason)
    check("no space: checkout intact", git(co, "status", "--porcelain=v2", "-uall") == status)
    check("no space: no refs changed", git(co, "for-each-ref") == refs)
    rec = cfg.state / "recovery"
    check("no space: no partial or final record left", not any(rec.iterdir()))

    # A crash mid-save leaves a .tmp directory: never mistaken for a record, counted as incomplete.
    (rec / ".tmp-crashed-1-abcd").mkdir()
    (rec / ".tmp-crashed-1-abcd" / "staged.patch").write_text("partial")
    s = wr.summary(root / "fail" / "state")
    check("interrupted save counted incomplete", s["incomplete"] == 1 and s["records"] == 0)

    # Corrupt manifest: neither reused nor deleted; a new record gets its own name.
    wr.note_owner(cfg, round_id="round-x")
    corrupt = rec / "round-x"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{not json")
    d = wr.guard(cfg, co)
    check("corrupt manifest not reused", d.record is not None and d.record.name == "round-x.2")
    check("corrupt manifest retained", (corrupt / "manifest.json").read_text() == "{not json")
    check("summary counts corrupt", wr.summary(root / "fail" / "state")["corrupt"] == 1)

    # Permission error creating the recovery root.
    origin2, co2, cfg2 = fixture(root / "perm")
    make_dirty(co2, root / "perm" / "nowhere")
    cfg2.state.mkdir(parents=True)
    (cfg2.state / "recovery").write_text("not a directory")
    status2 = git(co2, "status", "--porcelain=v2", "-uall")
    d = wr.guard(cfg2, co2)
    check("unwritable recovery root: refused", not d.ok and "could not create" in d.reason)
    check("unwritable recovery root: checkout intact", git(co2, "status", "--porcelain=v2", "-uall") == status2)
    if os.geteuid() != 0:
        (cfg2.state / "recovery").unlink()
        os.chmod(cfg2.state, 0o500)
        try:
            d = wr.guard(cfg2, co2)
        finally:
            os.chmod(cfg2.state, 0o700)
        check("permission denied: refused", not d.ok and "Permission denied" in d.reason)

    # Archive bound: oversized untracked content fails closed.
    origin3, co3, cfg3 = fixture(root / "big")
    (co3 / "huge.dat").write_bytes(b"x" * 2048)
    bound = wr.MAX_UNTRACKED_BYTES
    wr.MAX_UNTRACKED_BYTES = 1024
    try:
        d = wr.guard(cfg3, co3)
    finally:
        wr.MAX_UNTRACKED_BYTES = bound
    check("oversized untracked: refused", not d.ok and "archive bound" in d.reason and (co3 / "huge.dat").exists())


def test_live_process(root):
    if not Path("/proc/self").exists():
        print("[SKIP] live-process detection needs /proc")
        return
    origin, co, cfg = fixture(root / "live")
    wr.note_owner(cfg, round_id="round-live")
    (co / "Draft.lean").write_text("unfinished\n")
    child = subprocess.Popen(["sleep", "60"], cwd=co)
    try:
        time.sleep(0.2)
        d = wr.guard(cfg, co)
        check("live process: blocked", not d.ok and f"{child.pid} (sleep)" in d.reason)
        wr.confirm(cfg, d.record.name)
        d = wr.guard(cfg, co)
        check("live process: confirmation cannot override", not d.ok and "processes still use" in d.reason)
        (co / "Draft.lean").unlink()
        git(co, "commit", "-q", "--allow-empty", "-m", "local")
        d = wr.guard(cfg, co)
        check("live process: commits-only reset also blocked", not d.ok and d.state == "recovery-blocked")
    finally:
        child.kill()
        child.wait()
    d = wr.guard(cfg, co)
    check("process gone: commits-only proceeds", d.ok)


def test_containment_proof(root):
    origin, co, cfg = fixture(root / "contained")
    wr.note_owner(cfg, round_id="round-k")
    (co / "Draft.lean").write_text("unfinished\n")
    (cfg.state / "quiescence.json").write_text(json.dumps(dict(round_id="other", empty=True, boundary="pid-ns")))
    check("containment proof for another round ignored", not wr.guard(cfg, co).ok)
    (cfg.state / "quiescence.json").write_text(json.dumps(dict(round_id="round-k", empty=True, boundary="pid-ns")))
    d = wr.guard(cfg, co)
    check("containment proof for the exact round allows reset", d.ok and d.state == "checkout-reset")


def test_prepare_checkout(root):
    origin, co, cfg = fixture(root / "prep")
    agents = tc.agents
    saved = agents.sync_mathlib_pool
    agents.sync_mathlib_pool = lambda cfg: None
    try:
        (co / "Draft.lean").write_text("unfinished\n")
        git(co, "checkout", "-q", "-b", "pr-branch")
        status = git(co, "status", "--porcelain=v2", "-uall")
        ok = agents.prepare_checkout(cfg, work="fix", pr=7)
        check("prepare_checkout refuses dirty unknown-quiescence checkout", ok is False)
        check("prepare_checkout left the branch", git(co, "symbolic-ref", "--short", "HEAD").strip() == "pr-branch")
        check("prepare_checkout left the source", git(co, "status", "--porcelain=v2", "-uall") == status)
        wr.confirm(cfg, wr.LAST_REFUSAL.split("tauceti recovery confirm ")[1].split()[0])
        ok = agents.prepare_checkout(cfg, work="fix", pr=7)
        check("prepare_checkout resets after confirmation", ok and not (co / "Draft.lean").exists())
        check("prepare_checkout lands on main", git(co, "symbolic-ref", "--short", "HEAD").strip() == "main")
        owner = json.loads(wr.owner_path(cfg).read_text())
        check("prepare_checkout records the new owner", owner["work"] == "fix" and owner["pr"] == 7)
        check("clean re-preparation needs no new record", agents.prepare_checkout(cfg, work="review"))
    finally:
        agents.sync_mathlib_pool = saved


def test_work_paths(root):
    """Host roadmap work and progress preparation (host-side even for sandboxed workers) refuse
    without launching an agent or running the destructive refresh."""
    origin, co, cfg = fixture(root / "paths")
    cfg.logdir = root / "paths" / "logs"
    (co / "Draft.lean").write_text("unfinished\n")
    wu = tc.work_units
    names = ("fetch_ref", "run_agent_host", "roadmap_areas", "ensure_fork", "administrative_hold_avoid_list")
    saved = {k: getattr(wu, k) for k in names}
    saved_sync = tc.agents.sync_mathlib_pool
    launched = []
    wu.fetch_ref = lambda *a, **k: True
    wu.ensure_fork = lambda: "alice/TauCeti"
    wu.administrative_hold_avoid_list = lambda *_a: "none"
    wu.roadmap_areas = lambda gh: ["algebra"]
    wu.run_agent_host = lambda *a, **k: launched.append(a) or 0
    tc.agents.sync_mathlib_pool = lambda cfg: None
    w = SimpleNamespace(cfg=cfg, gh=object())
    opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", authoring_profile=None)
    try:
        try:
            tc.do_roadmap(w, None, tc.Candidate(0, "", "algebra"), opts, False)
            raised = ""
        except tc.Die as error:
            raised = str(error)
        check("host roadmap: refused with the reason", "recovery confirm" in raised)
        check("host roadmap: agent not launched", not launched)
        check("host roadmap: source kept", (co / "Draft.lean").exists())
    finally:
        for k, v in saved.items():
            setattr(wu, k, v)
        tc.agents.sync_mathlib_pool = saved_sync
        os.environ.pop("TAUCETI_REQUIRE_TARGET_MARKER", None)

    # Progress keeps a writable roadmap clone that its tool commits in; the same guard protects it.
    roadmap = cfg.state / "progress" / "roadmap"
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(roadmap)], check=True)
    (roadmap / "status.md").write_text("unpublished progress section\n")
    counters = SimpleNamespace(write=lambda *a: None)
    saved_bust = wu.bust_progress_cache
    wu.bust_progress_cache = lambda cfg: None
    try:
        try:
            wu._do_progress_inner(SimpleNamespace(cfg=cfg, counters=counters), opts)
            raised = ""
        except tc.Die as error:
            raised = str(error)
    finally:
        wu.bust_progress_cache = saved_bust
    check("progress: refused before refreshing", "refused" in raised and (roadmap / "status.md").exists())


def test_activity(root):
    """Prepared activity carries local counts; display and fleet aggregation read only that JSON."""
    import contextlib
    import io
    from unittest.mock import patch

    from tauceti_worker import activity, activity_summary

    origin, co, cfg = fixture(root / "act")
    wr.note_owner(cfg, round_id="round-a")
    (co / "Draft.lean").write_text("unfinished\n")
    wr.guard(cfg, co)
    (cfg.state / "recovery" / ".tmp-x").mkdir()
    with patch.object(wr, "HERE", root / "act"), patch.object(activity, "_refresh_prs", lambda *a: None):
        value = activity.prepare()
    rec = value["all"]["work_recovery"]
    check("activity: blocked and incomplete counted", rec["blocked"] == 1 and rec["incomplete"] == 1)
    check("activity: reason names the worker", any(r.startswith("w: ") for r in rec["reasons"]))
    summary = activity.cached()
    check("activity: cached summary carries recovery", summary["summary"]["work_recovery"]["blocked"] == 1)
    combined = activity_summary.combine([summary, summary])
    check("fleet aggregate sums host counts", combined["summary"]["work_recovery"]["blocked"] == 2)
    with (
        patch.object(activity, "_connect", side_effect=AssertionError("SQL in status")),
        contextlib.redirect_stdout(io.StringIO()) as out,
    ):
        activity.display(summary)
    check("display shows recovery-blocked", "1 recovery-blocked" in out.getvalue())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["TAUCETI_ACTIVITY_DIR"] = str(root / "activity")
        for test in (
            test_clean_and_published,
            test_full_record,
            test_commits_only,
            test_conflicted_merge,
            test_failures,
            test_live_process,
            test_containment_proof,
            test_prepare_checkout,
            test_work_paths,
            test_activity,
        ):
            (root / test.__name__).mkdir()
            test(root / test.__name__)
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
