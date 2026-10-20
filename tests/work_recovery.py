#!/usr/bin/env python3
"""Uncertain generations retain source while fresh work continues at distinct canonical paths.

Local git and detached writer fixtures cover v1 reconstruction, unknown legacy ownership,
late relative/absolute writes after final inspection, private writable caches, repeated uncertain
rounds, crash-safe selection and partial clones, storage/permission failures, and rollback.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import errno
import json
import os
import subprocess
import sys
import tempfile
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
    # This fixture's bare repository is canonical: prepare_checkout restores any other origin to
    # CHECKOUT_URL, which would otherwise send these tests to GitHub.
    tc.agents.CHECKOUT_URL = str(origin)
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

    # Unknown ownership: save a v1 snapshot, retain the original tree, and select a fresh path.
    d = wr.guard(cfg, co)
    record = cfg.state / "recovery" / "round-1"
    check("dirty, no proof: allowed after saving", d.ok and d.state == "retained" and d.record == record)
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
    check("status retained", json.loads((record / "status.json").read_text())["state"] == "retained")

    clone = reconstruct(origin, record, root / "full", "rebuilt")
    expected_tree = {k: v for k, v in before_tree.items() if not k.startswith("node_modules")}
    check("reconstructed source bytes are exact", snapshot_tree(clone) == expected_tree)
    check("reconstructed HEAD is the local commit", git(clone, "rev-parse", "HEAD").strip() == before_head)
    check("reconstructed index matches staged state", git(clone, "diff", "--cached", "--binary") == staged)

    d2 = wr._record(cfg, co, wr.inspect(co, wr._preserved(cfg)), "TauCeti", tc.TAUCETI)
    check("repeat: record reused", d2 == record and len(list((cfg.state / "recovery").iterdir())) == 1)

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
    check("commits only: allowed after saving", d.ok and d.state == "retained" and d.record is not None)
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
    check("commits only: not bundled twice", not wr.inspect(co, wr._preserved(cfg)).needs_record)
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


def test_generations(root):
    from unittest.mock import patch

    origin, old, cfg = fixture(root / "generations")
    (old / "Draft.lean").write_text("original source\n")
    # Unknown legacy owner; neither a missing PID nor a round diagnostic proves this generation.
    cfg.state.mkdir(parents=True)
    (cfg.state / "quiescence.json").write_text('{"round_id":"legacy","empty":true}')
    wr.owner_path(cfg).parent.mkdir()
    wr.owner_path(cfg).write_text('{"round_id":"legacy","pid":999999999}')
    previous_paths = [old]
    previous_caches = []
    with patch.object(tc.agents, "sync_mathlib_pool", side_effect=AssertionError("unsafe mutable cache pool")):
        for i in range(4):
            check(f"uncertain round {i} continues", tc.agents.prepare_checkout(cfg, work="fix", pr=i))
            active = wr.active_checkout(cfg)
            check(f"round {i}: canonical distinct path", active == active.resolve() and active not in previous_paths)
            check(f"round {i}: fresh main", git(active, "branch", "--show-current").strip() == "main")
            check(f"round {i}: old source preserved", (previous_paths[-1] / "Draft.lean").exists())
            check(f"round {i}: no old source in candidate", not (active / "Draft.lean").exists())
            cache = Path(os.environ["LAKE_CACHE_DIR"])
            check(f"round {i}: private caches", cache not in previous_caches and cache.is_relative_to(active.parent))
            check(
                f"round {i}: private verification outputs",
                Path(os.environ["TAUCETI_VERIFY_STATE"]).is_relative_to(active.parent),
            )
            (active / "Draft.lean").write_text(f"round {i} unfinished\n")
            previous_paths.append(active)
            previous_caches.append(cache)
    check(
        "reconstructed configuration resolves same generation",
        wr.active_checkout(SimpleNamespace(**vars(cfg))) == active,
    )
    owner = json.loads(wr.owner_path(cfg).read_text())
    check("owner binds exact active generation", owner["checkout"] == str(active) and owner["work"] == "fix")
    retained = json.loads(wr.selection_path(cfg).read_text())["retained"]
    check("no one-retained-checkout cap", len(retained) == 4 and all(Path(x["checkout"]).exists() for x in retained))
    check("status reports all retained generations", wr.summary(cfg.state.parent)["retained"] == 4)
    with patch.dict(os.environ, {"TAUCETI_WORK_RECOVERY": "off"}):
        check("rollback continues", tc.agents.prepare_checkout(cfg))
    check("rollback preserves every old generation", all(path.exists() for path in previous_paths))


def test_detached_writers(root):
    from unittest.mock import patch

    origin, old, cfg = fixture(root / "writers")
    (old / "Draft.lean").write_text("original\n")
    # Neither fixture writes until preparation has completed its FINAL inspection and cloned.
    script = (
        "import pathlib,sys; print('ready',flush=True); sys.stdin.readline(); "
        "pathlib.Path(sys.argv[1]).write_text('late source\\n'); "
        "pathlib.Path(sys.argv[2]).write_text('late cache\\n')"
    )
    cache = root / "old-cache"
    cache.mkdir()
    (old / ".lake").symlink_to(cache, target_is_directory=True)
    relative = subprocess.Popen(
        [sys.executable, "-c", script, "Relative.lean", ".lake/relative.olean"],
        cwd=old,
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    absolute = subprocess.Popen(
        [sys.executable, "-c", script, str(old / "Absolute.lean"), str(cache / "absolute.olean")],
        cwd="/",
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        check(
            "detached writers ready",
            relative.stdout.readline().strip() == absolute.stdout.readline().strip() == "ready",
        )
        check(
            "absolute writer holds no checkout reference in proc",
            not any(x.startswith(str(absolute.pid) + " ") for x in wr.live_users(old)),
        )
        with patch.dict(
            os.environ, {"LAKE_CACHE_DIR": str(cache), "MATHLIB_CACHE_DIR": str(cache), "XDG_CACHE_HOME": str(cache)}
        ):
            check("fresh work ready while both writers wait", tc.agents.prepare_checkout(cfg, work="fix"))
            fresh = wr.active_checkout(cfg)
            (fresh / "Candidate.lean").write_text("new work\n")
            for writer in (relative, absolute):
                writer.stdin.write("go\n")
                writer.stdin.flush()
                check("fixture writer finishes naturally", writer.wait(timeout=10) == 0)
            check("new candidate survives late writes", (fresh / "Candidate.lean").read_text() == "new work\n")
            check(
                "neither writer reaches new checkout",
                not (fresh / "Relative.lean").exists() and not (fresh / "Absolute.lean").exists(),
            )
            check(
                "original and both late sources recoverable",
                all((old / name).exists() for name in ("Draft.lean", "Relative.lean", "Absolute.lean")),
            )
            check("absolute old path still names old tree", old.is_dir() and old != fresh and not old.is_symlink())
            check(
                "old cache alias not imported",
                not (fresh / ".lake").is_symlink() and not list(Path(os.environ["LAKE_CACHE_DIR"]).glob("*.olean")),
            )
            check(
                "old writable cache changes stay in old cache",
                (cache / "relative.olean").exists() and (cache / "absolute.olean").exists(),
            )
        # Snapshotting the late bytes uses the same v1 archive, without authorizing deletion.
        record = wr._record(cfg, old, wr.inspect(old), "TauCeti", tc.TAUCETI)
        restored = reconstruct(origin, record, root / "writers", "restored")
        check(
            "late bytes reconstruct from v1 record",
            all(
                (restored / name).read_bytes() == (old / name).read_bytes()
                for name in ("Draft.lean", "Relative.lean", "Absolute.lean")
            ),
        )
    finally:
        for writer in (relative, absolute):
            if writer.poll() is None:
                writer.stdin.write("go\n")
                writer.stdin.flush()
                writer.wait(timeout=10)
            writer.stdin.close()
            writer.stdout.close()


def test_selection_failures(root):
    from unittest.mock import patch

    for err in (errno.ENOSPC, errno.EACCES):
        origin, old, cfg = fixture(root / f"save-{err}")
        (old / "Draft.lean").write_text("valuable\n")
        with patch.object(wr, "_archive", side_effect=OSError(err, os.strerror(err))):
            check(f"save failure {err}: continues fresh", tc.agents.prepare_checkout(cfg))
        check(f"save failure {err}: original source kept", (old / "Draft.lean").read_text() == "valuable\n")
        origin, old, cfg = fixture(root / f"select-{err}")
        (old / "Draft.lean").write_text("valuable\n")
        real_atomic = wr.atomic_json

        def fail_selection(path, value, cfg=cfg, err=err, real_atomic=real_atomic):
            if path == wr.selection_path(cfg):
                raise OSError(err, os.strerror(err))
            real_atomic(path, value)

        with patch.object(wr, "atomic_json", side_effect=fail_selection):
            check(
                f"selector failure {err}: reports actual error",
                not tc.agents.prepare_checkout(cfg) and os.strerror(err) in wr.LAST_REFUSAL,
            )
        check(
            f"selector failure {err}: original source kept",
            (old / "Draft.lean").exists() and wr.active_checkout(cfg) == old,
        )
        check(f"selector failure {err}: retry succeeds", tc.agents.prepare_checkout(cfg))

    # A clone failure is retried with its partial generation retained, never removed/reused.
    origin, old, cfg = fixture(root / "clone")
    real_run = subprocess.run

    def broken_clone(args, **kw):
        if args[:3] == ["git", "clone", "-q"]:
            partial = Path(args[-1])
            partial.mkdir()
            (partial / "Partial.lean").write_text("partial bytes\n")
            raise SystemExit("crash during clone")
        return real_run(args, **kw)

    with patch.object(tc.agents.subprocess, "run", side_effect=broken_clone):
        try:
            tc.agents.prepare_checkout(cfg)
        except SystemExit:
            pass
    partial = wr.active_checkout(cfg)
    check("crash during clone: partial generation selected", (partial / "Partial.lean").exists())
    check("crash during clone: retry continues", tc.agents.prepare_checkout(cfg))
    check(
        "crash during clone: partial source preserved",
        (partial / "Partial.lean").exists() and wr.active_checkout(cfg) != partial,
    )
    # Before selector commit and after selector commit: either pointer is safe to recover.
    for after in (False, True):
        origin, old, cfg = fixture(root / f"selection-crash-{after}")
        (old / "Draft.lean").write_text("source\n")
        real_atomic = wr.atomic_json

        def crash(path, value, cfg=cfg, after=after, real_atomic=real_atomic):
            if path == wr.selection_path(cfg):
                if after:
                    real_atomic(path, value)
                raise SystemExit("crash at selector commit")
            real_atomic(path, value)

        with patch.object(wr, "atomic_json", side_effect=crash):
            try:
                tc.agents.prepare_checkout(cfg)
            except SystemExit:
                pass
        check(f"selector crash {after}: old source intact", (old / "Draft.lean").exists())
        check(f"selector crash {after}: next preparation recovers", tc.agents.prepare_checkout(cfg))
        check(
            f"selector crash {after}: legacy path never reused",
            (old / "Draft.lean").exists() and wr.active_checkout(cfg) != old,
        )
    # Malformed selector must never silently fall back to a legacy or retained generation.
    wr.selection_path(cfg).write_text("{truncated")
    try:
        wr.active_checkout(cfg)
        check("invalid selector refuses legacy fallback", False)
    except wr.RecordError:
        check("invalid selector refuses legacy fallback", (old / "Draft.lean").exists())
    check("invalid selector automatically isolates on retry", tc.agents.prepare_checkout(cfg))
    check("invalid selector evidence retained", bool(list(wr.selection_path(cfg).parent.glob("*.invalid-*"))))
    aliased = wr.active_checkout(cfg)
    physical = aliased.with_name("old-source")
    (aliased / "Valuable.lean").write_text("keep physical source\n")
    aliased.rename(physical)
    aliased.symlink_to(physical, target_is_directory=True)
    check("aliased generation automatically isolates", tc.agents.prepare_checkout(cfg))
    check("aliased physical source preserved", (physical / "Valuable.lean").read_text() == "keep physical source\n")
    check("aliased path never routes fresh work", wr.active_checkout(cfg) != aliased and aliased.resolve() == physical)
    # Clone and writable-path failures report actual storage/permission errors to status.
    origin, old, cfg = fixture(root / "clone-enospc")
    run = subprocess.run

    def full_clone(args, **kwargs):
        if args[:3] == ["git", "clone", "-q"]:
            return subprocess.CompletedProcess(args, 128, "", "No space left on device")
        return run(args, **kwargs)

    with (
        patch.object(tc.agents.subprocess, "run", side_effect=full_clone),
        patch("tauceti_worker.runtime_status.report_runtime") as status,
    ):
        check("clone ENOSPC is reported", not tc.agents.prepare_checkout(cfg))
        check("clone ENOSPC prepared status", status.call_args.kwargs["workspace_error"] == "No space left on device")
    check("clone ENOSPC old tree preserved", old.exists())
    with (
        patch.object(wr, "private_environment", side_effect=PermissionError(errno.EACCES, "Permission denied")),
        patch("tauceti_worker.runtime_status.report_runtime") as status,
    ):
        check("cache directory permission failure is reported", not tc.agents.prepare_checkout(cfg))
        check("cache failure prepared status", "Permission denied" in status.call_args.kwargs["workspace_error"])
    check("cache directory permission retry continues", tc.agents.prepare_checkout(cfg))
    # Legacy cleanup markers are not evidence that a detached owner cannot write later.
    origin, old, cfg = fixture(root / "legacy-retained")
    doomed = old.parent / "retained-checkouts" / "TauCeti-old.deleting"
    doomed.mkdir(parents=True)
    (doomed / "Draft.lean").write_text("still valuable\n")
    check("legacy retained preparation continues", tc.agents.prepare_checkout(cfg))
    check("legacy deleting tree preserved", (doomed / "Draft.lean").exists())


def test_archive_integrity(root):
    from unittest.mock import patch

    origin, old, cfg = fixture(root / "integrity")
    wr.note_owner(cfg, round_id="round-x")
    (old / "Draft.lean").write_text("valuable source\n")
    corrupt = cfg.state / "recovery" / "round-x"
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("{broken")
    decision = wr.guard(cfg, old)
    check("corrupt record not reused", decision.record.name == "round-x.2")
    (decision.record / "untracked.tar").write_bytes(b"damaged")
    replacement = wr._record(cfg, old, wr.inspect(old), "TauCeti", tc.TAUCETI)
    check("damaged payload not trusted", replacement != decision.record)
    restored = reconstruct(origin, replacement, root / "integrity", "restored")
    check(
        "new archive reconstructs valuable source",
        (restored / "Draft.lean").read_bytes() == (old / "Draft.lean").read_bytes(),
    )
    (cfg.state / "recovery" / ".tmp-crashed-save").mkdir()
    status = wr.summary(cfg.state.parent)
    check("corrupt and interrupted archives remain visible", status["corrupt"] == 2 and status["incomplete"] == 1)
    # A real bound refusal still retains the entire original source tree.
    origin, old, cfg = fixture(root / "bound")
    (old / "large.dat").write_bytes(b"x" * 2048)
    with patch.object(wr, "MAX_UNTRACKED_BYTES", 1024):
        check("oversized archive continues fresh", tc.agents.prepare_checkout(cfg))
    check("oversized source retained intact", (old / "large.dat").stat().st_size == 2048)
    # Preparation must never reuse a generation path just because its checkout disappeared.
    origin, old, cfg = fixture(root / "absent")
    check("first preparation succeeds", tc.agents.prepare_checkout(cfg))
    previous = wr.active_checkout(cfg)
    import shutil

    shutil.rmtree(previous)
    check("absent exposed checkout continues fresh", tc.agents.prepare_checkout(cfg))
    check("absent exposed path is not reused", previous != wr.active_checkout(cfg) and not previous.exists())


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
        check(
            "host roadmap: agent launched after saving",
            len(launched) == 1 and (co / "Draft.lean").exists() and launched[0][0] == wr.active_checkout(cfg),
        )
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

    from tauceti_worker import progress_publication as publication
    from tauceti_worker.survey import progress_due

    fresh = wr.progress_checkout(cfg)
    subprocess.run(["git", "clone", "-q", str(origin), str(fresh)], check=True)
    work = wr.progress_work(cfg)
    work.mkdir()
    for name in publication.FILES:
        (work / name).write_text(
            json.dumps({k: k for k in ("roadmap", "from_sha", "to_sha", "status_path", "progress_path")})
            if name.endswith(".json")
            else "new prose"
        )
    publication.save(cfg, "author", "version")
    check(
        "progress receipt uses active generation outputs",
        publication.retained(cfg) is not None and work.parent == fresh.parent,
    )
    (work / "section-body.md").write_text("changed without matching receipt")
    due, reason = progress_due(cfg, SimpleNamespace(read=lambda _: 0))
    check("survey resolves same generation receipt", not due and "needs recovery" in reason)
    check(
        "progress legacy source remains intact", (roadmap / "status.md").read_text() == "unpublished progress section\n"
    )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["TAUCETI_ACTIVITY_DIR"] = str(root / "activity")
        for test in (
            test_full_record,
            test_commits_only,
            test_hidden_source,
            test_generations,
            test_detached_writers,
            test_selection_failures,
            test_archive_integrity,
            test_work_paths,
        ):
            (root / test.__name__).mkdir()
            from unittest.mock import patch

            with patch.dict(os.environ):
                test(root / test.__name__)
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
