#!/usr/bin/env python3
"""Unfinished source is saved before checkout preparation destroys it, and preparation never stops.

Pinned here, with local git fixtures only (no GitHub, no agents):

  1. A clean checkout whose commits are all published needs no record and is reset as before.
  2. Untracked/new files, staged versus unstaged edits, deletions, binary content, symlinks (inside and
     pointing outside the checkout), and local commits after the expected remote head are saved in a
     complete tauceti-work-recovery/v1 record before the reset, and a fresh clone reconstructs the
     exact source bytes.
  3. Leftovers of an OOM or SIGKILL (no proof of anything) are saved and the checkout reset: nothing
     waits for a human. A write after the snapshot is saved too.
  4. Commits-only leftovers are bundled once; commits marked published or pushed are not.
  5. A conflicted merge, ignored drafts and assume-unchanged edits are saved exactly.
  6. Save failures (no space, permission, archive bound), a live process using the checkout, and an
     unstable tree move the whole checkout aside unmodified and preparation continues on a fresh
     clone; a later preparation saves and removes the retained copy once nothing uses it.
  7. Interrupted saves, corrupt manifests and damaged payloads are never trusted or deleted.
  8. Host work (`prepare_checkout`, roadmap) and progress preparation proceed after saving.

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
    before_head = git(co, "rev-parse", "HEAD").strip()
    staged = git(co, "diff", "--cached", "--binary")

    # Nothing proves the previous round is gone (as after a SIGKILL): save, then allow the reset.
    d = wr.guard(cfg, co)
    record = cfg.state / "recovery" / "round-1"
    check("dirty, no proof: allowed after saving", d.ok and d.state == "checkout-reset" and d.record == record)
    check("guard itself changed nothing in the checkout", snapshot_tree(co) == before_tree)
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
    check("status checkout-reset", json.loads((record / "status.json").read_text())["state"] == "checkout-reset")

    clone = reconstruct(origin, record, root / "full", "rebuilt")
    expected_tree = {k: v for k, v in before_tree.items() if not k.startswith("node_modules")}
    check("reconstructed source bytes are exact", snapshot_tree(clone) == expected_tree)
    check("reconstructed HEAD is the local commit", git(clone, "rev-parse", "HEAD").strip() == before_head)
    check("reconstructed index matches staged state", git(clone, "diff", "--cached", "--binary") == staged)

    d2 = wr.guard(cfg, co)
    check("repeat: record reused", d2.ok and d2.record == record and len(list((cfg.state / "recovery").iterdir())) == 1)

    os.environ["TAUCETI_WORK_RECOVERY"] = "off"
    try:
        other = fixture(root / "rollback")
        (other[1] / "Draft.lean").write_text("x\n")
        d_off = wr.guard(other[2], other[1])
    finally:
        del os.environ["TAUCETI_WORK_RECOVERY"]
    check("rollback: dirty tree moved aside, not reset", d_off.ok and d_off.state == "retained")
    check("rollback: no record written", not (other[2].state / "recovery").exists())


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


def conflicted_fixture(root):
    origin, co, cfg = fixture(root)
    seed = root / "seed"
    (seed / "A.lean").write_text("upstream\n")
    git(seed, "commit", "-qam", "upstream")
    git(seed, "push", "-q", str(origin), "main")
    (co / "A.lean").write_text("local\n")
    git(co, "commit", "-qam", "local")
    git(co, "fetch", "-q")
    git(co, "merge", "origin/main", check_rc=False)
    return origin, co, cfg


def test_hidden_source(root):
    origin, co, cfg = conflicted_fixture(root / "merge")
    check("fixture: merge conflicted", "UU A.lean" in git(co, "status", "--porcelain"))
    before = snapshot_tree(co)
    d = wr.guard(cfg, co)
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("conflicted merge: saved", d.ok and manifest["conflicted"] == ["A.lean"])
    check("merge operation recorded", "MERGE_HEAD" in manifest["operations"])
    clone = reconstruct(origin, d.record, root / "merge", "rebuilt")
    check("conflicted bytes reconstructed", snapshot_tree(clone) == before)

    origin, co, cfg = conflicted_fixture(root / "conflict-deleted")
    (co / "A.lean").unlink()
    d = wr.guard(cfg, co)
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("deleted conflicted path recorded absent", manifest["absent"] == ["A.lean"])
    clone = reconstruct(origin, d.record, root / "conflict-deleted", "rebuilt")
    check("deleted conflicted path stays deleted", not (clone / "A.lean").exists())

    origin, co, cfg = fixture(root / "hidden")
    (co / ".git" / "info" / "exclude").write_text("Scratch*.lean\n")
    (co / "Scratch.lean").write_text("-- ignored but unfinished\n")
    (co / "Main.olean").write_bytes(b"cache")
    git(co, "update-index", "--assume-unchanged", "B.lean")
    (co / "B.lean").write_text("hidden edit\n")
    check("fixture: plain diff hides the assume-unchanged edit", git(co, "diff") == "")
    before = snapshot_tree(co)
    d = wr.guard(cfg, co)
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("ignored draft saved", "Scratch.lean" in manifest["untracked"])
    check("ignored build output excluded", any(e["path"] == "Main.olean" for e in manifest["excluded"]))
    check("assume-unchanged edit saved", manifest["flagged"] == ["B.lean"])
    clone = reconstruct(origin, d.record, root / "hidden", "rebuilt")
    check("hidden source reconstructed exactly", snapshot_tree(clone) == before)


def retained_dirs(co):
    parent = co.parent / wr.RETAINED_DIR
    return sorted(p for p in parent.iterdir() if p.is_dir()) if parent.is_dir() else []


def test_move_aside(root):
    """Whatever cannot be saved or reset safely is moved aside whole; nothing ever blocks."""
    origin, co, cfg = fixture(root / "nospace")
    make_dirty(co, root / "nospace" / "nowhere")
    before = snapshot_tree(co)
    refs = git(co, "for-each-ref")
    real_archive = wr._archive

    def no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    wr._archive = no_space
    try:
        d = wr.guard(cfg, co)
    finally:
        wr._archive = real_archive
    moved = retained_dirs(co)
    check("no space: allowed, checkout moved aside", d.ok and d.state == "retained" and len(moved) == 1)
    check("no space: reason kept", "No space left on device" in d.reason)
    check("no space: original path free for a fresh clone", not co.exists())
    check("no space: retained checkout byte-identical", snapshot_tree(moved[0]) == before)
    check("no space: no refs changed", git(moved[0], "for-each-ref") == refs)
    check("no space: no partial record left", not any((cfg.state / "recovery").iterdir()))
    check(
        "no space: owner kept beside the retained checkout",
        moved[0].with_name(moved[0].name + ".owner.json").exists() or not wr.owner_path(cfg).exists(),
    )

    # The next preparation saves the retained copy (space is back) and removes it.
    subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
    d = wr.guard(cfg, co)
    check("retained copy saved and removed", d.ok and not retained_dirs(co))
    records = [p for p in (cfg.state / "recovery").iterdir() if p.is_dir()]
    check("retained copy now has a record", len(records) == 1 and (records[0] / "manifest.json").exists())
    clone = reconstruct(origin, records[0], root / "nospace", "rebuilt")
    expected = {k: v for k, v in before.items() if not k.startswith("node_modules")}
    check("retained copy reconstructed exactly", snapshot_tree(clone) == expected)

    # Permission error creating the recovery root, or an oversized tree: moved aside too.
    origin, co, cfg = fixture(root / "perm")
    (co / "Draft.lean").write_text("x\n")
    cfg.state.mkdir(parents=True)
    (cfg.state / "recovery").write_text("not a directory")
    d = wr.guard(cfg, co)
    check("unwritable recovery root: moved aside", d.ok and d.state == "retained" and "could not create" in d.reason)
    origin, co, cfg = fixture(root / "big")
    (co / "huge.dat").write_bytes(b"x" * 2048)
    bound = wr.MAX_UNTRACKED_BYTES
    wr.MAX_UNTRACKED_BYTES = 1024
    try:
        d = wr.guard(cfg, co)
    finally:
        wr.MAX_UNTRACKED_BYTES = bound
    check("oversized untracked: moved aside", d.ok and d.state == "retained" and "archive bound" in d.reason)
    check("oversized untracked: bytes kept", (retained_dirs(co)[0] / "huge.dat").stat().st_size == 2048)

    # Neither saved nor movable: the only refusal, and the checkout is untouched.
    origin, co, cfg = fixture(root / "stuck")
    (co / "Draft.lean").write_text("x\n")
    real_rename, real_archive = wr.os.rename, wr._archive
    wr._archive = no_space

    def no_rename(*_a):
        raise OSError(errno.EACCES, "Permission denied")

    wr.os.rename = no_rename
    try:
        d = wr.guard(cfg, co)
    finally:
        wr.os.rename, wr._archive = real_rename, real_archive
    check("unsavable and unmovable: refused, retried next round", not d.ok and "left intact" in d.reason)
    check("unsavable and unmovable: checkout intact", (co / "Draft.lean").read_text() == "x\n")

    # A crash mid-save leaves a .tmp directory: never mistaken for a record, counted as incomplete.
    rec = cfg.state / "recovery"
    rec.mkdir(parents=True, exist_ok=True)
    (rec / ".tmp-crashed-1-abcd").mkdir()
    s = wr.summary(root / "stuck" / "state")
    check("interrupted save counted incomplete", s["incomplete"] == 1 and s["records"] == 0)

    # Corrupt manifest and damaged payloads: neither reused nor deleted.
    origin, co, cfg = fixture(root / "corrupt")
    wr.note_owner(cfg, round_id="round-x")
    (co / "Draft.lean").write_text("x\n")
    corrupt = cfg.state / "recovery" / "round-x"
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("{not json")
    d = wr.guard(cfg, co)
    check("corrupt manifest not reused", d.record is not None and d.record.name == "round-x.2")
    check("corrupt manifest retained", (corrupt / "manifest.json").read_text() == "{not json")
    (d.record / "untracked.tar").write_bytes(b"truncated")
    d2 = wr.guard(cfg, co)
    check("damaged record not reused", d2.record not in (None, d.record) and (d.record / "manifest.json").exists())
    check("summary counts corrupt and damaged", wr.summary(root / "corrupt" / "state")["corrupt"] == 2)


def test_broken_git_state(root):
    """Crash debris never wedges preparation: it is moved aside and a fresh clone used."""
    origin, co, cfg = fixture(root / "broken")
    agents = tc.agents
    saved_sync, saved_url = agents.sync_mathlib_pool, agents.CHECKOUT_URL
    agents.sync_mathlib_pool = lambda cfg: None
    agents.CHECKOUT_URL = str(origin)
    try:
        (co / ".git" / "index.lock").write_text("")
        (co / "Draft.lean").write_text("x\n")
        ok = agents.prepare_checkout(cfg, work="fix")
        check("stale index.lock: preparation succeeds", ok and not (co / ".git" / "index.lock").exists())
        check(
            "stale index.lock: old checkout retained or saved",
            bool(retained_dirs(co)) or any((cfg.state / "recovery").iterdir()),
        )
        subprocess.run(["rm", "-rf", str(co)], check=True)
        co.mkdir()
        (co / "partial").write_text("interrupted clone\n")
        ok = agents.prepare_checkout(cfg, work="fix")
        check("interrupted clone: preparation succeeds", ok and (co / ".git").is_dir())
        check(
            "interrupted clone: debris retained, not deleted", any((d / "partial").exists() for d in retained_dirs(co))
        )
    finally:
        agents.sync_mathlib_pool, agents.CHECKOUT_URL = saved_sync, saved_url

    # A retained copy that cannot be saved keeps its source but loses its caches; a `*.deleting`
    # leftover from an interrupted removal is finished without git.
    origin, co, cfg = fixture(root / "evict")
    parent = co.parent / wr.RETAINED_DIR
    big = parent / "TauCeti-20260101T000000-aaaaaa"
    big.mkdir(parents=True)
    (big / "Source.lean").write_text("unsaved source\n")
    (big / ".lake").mkdir()
    (big / ".lake" / "huge.olean").write_bytes(b"x" * 100)
    doomed = parent / "TauCeti-20250101T000000-bbbbbb.deleting"
    doomed.mkdir()
    (doomed / "rest").write_text("half deleted\n")
    check("eviction fixture ready", (big / ".lake").exists())
    wr.guard(cfg, co)
    check("unsavable retained copy keeps its source", (big / "Source.lean").exists())
    check("unsavable retained copy loses its caches", not (big / ".lake").exists())
    check("interrupted removal finished", not doomed.exists())


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
        moved = retained_dirs(co)
        check(
            "live process: moved aside, not blocked",
            d.ok and d.state == "retained" and f"{child.pid} (sleep)" in d.reason,
        )
        check("live process: its source saved too", d.record is not None and (moved[0] / "Draft.lean").exists())
        check(
            "live process: still working in the retained copy", os.readlink(f"/proc/{child.pid}/cwd") == str(moved[0])
        )
        subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
        check("live process: retained copy kept while in use", wr.guard(cfg, co).ok and retained_dirs(co) == moved)
    finally:
        child.kill()
        child.wait()
    d = wr.guard(cfg, co)
    check("process gone: retained copy removed", d.ok and not retained_dirs(co))

    _, co2, cfg2 = fixture(root / "live-clean")
    child = subprocess.Popen(["sleep", "60"], cwd=co2)
    try:
        time.sleep(0.2)
        d = wr.guard(cfg2, co2)
    finally:
        child.kill()
        child.wait()
    check("clean but in use: moved aside rather than reset", d.ok and d.state == "retained")
    check(
        "process gone: record reused, not duplicated",
        len([p for p in (cfg.state / "recovery").iterdir() if p.is_dir()]) == 1,
    )


def test_late_write(root):
    origin, co, cfg = fixture(root / "late")
    wr.note_owner(cfg, round_id="round-late")
    (co / "Draft.lean").write_text("early\n")
    real = wr.live_users
    calls = []

    def late_writer(path):
        if path == co and not calls:
            (co / "Late.lean").write_text("written after the snapshot\n")
            calls.append(1)
        return real(path)

    wr.live_users = late_writer
    try:
        d = wr.guard(cfg, co)
    finally:
        wr.live_users = real
    manifest = json.loads((d.record / "manifest.json").read_text())
    check("late write: newer state saved before allowing reset", d.ok and "Late.lean" in manifest["untracked"])
    check("late write: earlier record retained", len([p for p in (cfg.state / "recovery").iterdir()]) == 2)


def test_push_and_owner(root):
    origin, co, cfg = fixture(root / "push")
    (co / "A.lean").write_text("pushed\n")
    git(co, "commit", "-qam", "pushed")
    pushed = git(co, "rev-parse", "HEAD").strip()
    env = {**os.environ, "TAUCETI_PUSH_REF": "feature", "TAUCETI_PUSH_REMOTE": str(origin), "TAUCETI_CLAIM_KEY": ""}
    env.pop("TAUCETI_PUSH_EXPECT", None)
    rc = subprocess.run([str(REPO / "scripts" / "git-safe-push")], cwd=co, env=env, capture_output=True).returncode
    check("git-safe-push: pushed", rc == 0 and git(origin, "rev-parse", "feature").strip() == pushed)
    check(
        "git-safe-push: published ref is the pushed oid",
        git(co, "rev-parse", "refs/tauceti/published/push/feature").strip() == pushed,
    )
    check("pushed work needs no record", wr.guard(cfg, co).state == "clean")

    saved_sync, saved_atomic = tc.agents.sync_mathlib_pool, wr.atomic_json
    tc.agents.sync_mathlib_pool = lambda cfg: None

    def fail(*_a, **_k):
        raise OSError(errno.EACCES, "Permission denied")

    wr.atomic_json = fail
    try:
        ok = tc.agents.prepare_checkout(cfg, work="fix", pr=1)
    finally:
        tc.agents.sync_mathlib_pool, wr.atomic_json = saved_sync, saved_atomic
    check("owner write failure never stops preparation", ok is True)


def test_prepare_checkout(root):
    origin, co, cfg = fixture(root / "prep")
    agents = tc.agents
    saved_sync, saved_url = agents.sync_mathlib_pool, agents.CHECKOUT_URL
    agents.sync_mathlib_pool = lambda cfg: None
    agents.CHECKOUT_URL = str(origin)
    try:
        (co / "Draft.lean").write_text("unfinished\n")
        git(co, "checkout", "-q", "-b", "pr-branch")
        ok = agents.prepare_checkout(cfg, work="fix", pr=7)
        check("prepare_checkout: dirty checkout saved and reset", ok and not (co / "Draft.lean").exists())
        check("prepare_checkout lands on main", git(co, "symbolic-ref", "--short", "HEAD").strip() == "main")
        records = [p for p in (cfg.state / "recovery").iterdir() if p.is_dir()]
        check(
            "prepare_checkout saved the draft",
            len(records) == 1 and "Draft.lean" in json.loads((records[0] / "manifest.json").read_text())["untracked"],
        )
        owner = json.loads(wr.owner_path(cfg).read_text())
        check("prepare_checkout records the new owner", owner["work"] == "fix" and owner["pr"] == 7)
        if Path("/proc/self").exists():
            (co / "Draft2.lean").write_text("x\n")
            child = subprocess.Popen(["sleep", "60"], cwd=co)
            try:
                time.sleep(0.2)
                ok = agents.prepare_checkout(cfg, work="fix", pr=8)
                check(
                    "prepare_checkout: in-use checkout moved aside, fresh clone ready",
                    ok and (co / ".git").is_dir() and not (co / "Draft2.lean").exists(),
                )
                check(
                    "prepare_checkout: fresh clone on main",
                    git(co, "symbolic-ref", "--short", "HEAD").strip() == "main",
                )
            finally:
                child.kill()
                child.wait()
    finally:
        agents.sync_mathlib_pool, agents.CHECKOUT_URL = saved_sync, saved_url


def test_work_paths(root):
    """Host roadmap work and progress preparation save leftovers and carry on."""
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
        tc.do_roadmap(w, None, tc.Candidate(0, "", "algebra"), opts, False)
        check("host roadmap: agent launched after saving", len(launched) == 1 and not (co / "Draft.lean").exists())
        check("host roadmap: draft saved", any((cfg.state / "recovery").iterdir()))
    finally:
        for k, v in saved.items():
            setattr(wu, k, v)
        tc.agents.sync_mathlib_pool = saved_sync
        os.environ.pop("TAUCETI_REQUIRE_TARGET_MARKER", None)

    roadmap = cfg.state / "progress" / "roadmap"
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(roadmap)], check=True)
    (roadmap / "status.md").write_text("unpublished progress section\n")
    decision = wr.guard(cfg, roadmap, label="progress-roadmap", repository="TauCetiProject/TauCetiRoadmap")
    manifest = json.loads((decision.record / "manifest.json").read_text())
    check("progress clone: section saved", decision.ok and manifest["untracked"] == ["status.md"])
    check("progress clone: repository recorded", manifest["repository"] == "TauCetiProject/TauCetiRoadmap")


def test_activity(root):
    """Prepared activity carries local counts; display and fleet aggregation read only that JSON."""
    import contextlib
    import io
    from unittest.mock import patch

    from tauceti_worker import activity, activity_summary

    origin, co, cfg = fixture(root / "act")
    wr.note_owner(cfg, round_id="round-a")
    (co / "Draft.lean").write_text("unfinished\n")
    real_archive = wr._archive
    wr._archive = lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
    try:
        wr.guard(cfg, co)  # moved aside, no record
    finally:
        wr._archive = real_archive
    subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
    (co / "Other.lean").write_text("x\n")
    real_users = wr.live_users
    wr.live_users = lambda path: ["123 (lake)"] if path == co else []
    try:
        wr.guard(cfg, co)  # saved first, then moved aside: a retained record
    finally:
        wr.live_users = real_users
    (cfg.state / "recovery" / ".tmp-x").mkdir()
    with patch.object(wr, "HERE", root / "act"), patch.object(activity, "_refresh_prs", lambda *a: None):
        value = activity.prepare()
    rec = value["all"]["work_recovery"]
    check("activity: retained and incomplete counted", rec["retained"] >= 1 and rec["incomplete"] == 1)
    check("activity: reason names the worker", any(r.startswith("w: ") for r in rec["reasons"]))
    summary = activity.cached()
    combined = activity_summary.combine([summary, summary])
    check("fleet aggregate sums host counts", combined["summary"]["work_recovery"]["retained"] == 2 * rec["retained"])
    with (
        patch.object(activity, "_connect", side_effect=AssertionError("SQL in status")),
        contextlib.redirect_stdout(io.StringIO()) as out,
    ):
        activity.display(summary)
    check("display shows moved-aside checkouts", "checkouts moved aside" in out.getvalue())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["TAUCETI_ACTIVITY_DIR"] = str(root / "activity")
        for test in (
            test_clean_and_published,
            test_full_record,
            test_commits_only,
            test_hidden_source,
            test_move_aside,
            test_live_process,
            test_broken_git_state,
            test_late_write,
            test_push_and_owner,
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
