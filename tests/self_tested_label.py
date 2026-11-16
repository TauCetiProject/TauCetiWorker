#!/usr/bin/env python3
"""The worker labels a round's pushed head self-tested only with this round's receipt for that exact commit."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tauceti_worker.self_tested_label as stl  # noqa: E402
from tauceti_worker import verification  # noqa: E402

failures = []
LABELLED = ("self-tested",)


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r}")
    if not ok:
        failures.append(name)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def checkout(tmp, *, infra=False, middle=False, symlink=False, base_symlink=False):
    """origin/main is the base; HEAD edits one module, deletes another and adds an executable.

    `middle` puts an infrastructure commit between main and the change; `symlink` adds one."""
    root = Path(tmp) / "code"
    (root / "TauCeti").mkdir(parents=True)
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "TauCeti/A.lean").write_text("a\n")
    (root / "TauCeti/B.lean").write_text("b\n")
    (root / "lakefile.toml").write_text("name = 't'\n")
    if base_symlink:
        (root / "AGENTS.md").write_text("instructions\n")
        (root / ".claude").mkdir()
        (root / ".claude/CLAUDE.md").symlink_to("../AGENTS.md")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "remote", "add", "origin", "https://github.com/TauCetiProject/TauCeti.git")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    if middle:
        (root / "lakefile.toml").write_text("name = 'm'\n")
        git(root, "commit", "-q", "-am", "infrastructure")
    (root / "TauCeti/A.lean").write_text("a2\n")
    git(root, "rm", "-q", "TauCeti/B.lean")
    (root / "TauCeti/run.sh").write_text("#!/bin/sh\n")
    (root / "TauCeti/run.sh").chmod(0o755)
    if infra:
        (root / "lakefile.toml").write_text("name = 'u'\n")
    if symlink:
        if symlink in ("external", "escaping"):
            outside = Path(tmp) / "outside.lean"
            outside.write_text("external input\n")
            target = str(outside) if symlink == "external" else "../../outside.lean"
        else:
            target = "A.lean"
        (root / "TauCeti/Link.lean").symlink_to(target)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "change")
    return root, base, git(root, "rev-parse", "HEAD")


def write_receipt(state, root, base, *, status="success", phases=verification.STAGES, policy="ordinary", extra=None):
    """A receipt as `tauceti-verify` writes it, with the real verifier's inventory of `root`."""
    sources = verification.inventory(root, base)
    if extra:
        sources[extra] = {"kind": "file", "mode": 0o644, "sha256": "0" * 64}
    inputs = {"base": base, "sources": sources}
    fingerprint = verification.digest(inputs)
    inputs_path = state / "verification-inputs" / f"{fingerprint}.json"
    inputs_path.parent.mkdir(parents=True, exist_ok=True)
    inputs_path.write_text(json.dumps(inputs))
    rid = os.urandom(8).hex()
    path = state / "verification" / rid / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": verification.SCHEMA,
                "id": rid,
                "status": status,
                "requested_phases": list(phases),
                "phases": {p: {"status": "success" if status == "success" else status} for p in phases},
                "scope": {"policy": policy},
                "inputs": {"path": str(inputs_path), "sha256": fingerprint},
                "fingerprint": fingerprint,
            }
        )
    )
    return path


def pull(head, *, labels=(), ref="feature"):
    return {
        "state": "open",
        "draft": False,
        "base": {"ref": "main"},
        "head": {"sha": head, "ref": ref, "repo": {"full_name": "someone/TauCeti"}},
        "labels": [{"name": name} for name in labels],
    }


class FakeGitHub:
    """Serves scripted PR reads in order (repeating the last), head lookups."""

    def __init__(self, pulls, heads=None):
        self.pulls, self.labelled, self.heads = list(pulls), 0, heads or {}
        self.reads = 0

    def live_pull(self, pr):
        self.reads += 1
        return self.pulls.pop(0) if len(self.pulls) > 1 else self.pulls[0]

    def add_label(self, pr, name):
        assert name == "self-tested"
        self.labelled += 1
        return True

    def pull_for_head(self, owner, branch):
        return self.heads.get(f"{owner}:{branch}")


def run(tmp, pulls, *, stage="fix", receipt=None, since=None, env="1", **shape):
    """One fix round on PR 9 that pushed HEAD; `receipt(state, root, base, head)` writes the evidence."""
    root, base, head = checkout(tmp, **shape)
    state = Path(tmp) / "state"
    state.mkdir()
    started = time.time() - 5 if since is None else since
    if receipt:
        receipt(state, root, base, head)
    gh = FakeGitHub(pulls(head))
    w = SimpleNamespace(cfg=SimpleNamespace(checkout=root, state=state, logdir=state), gh=gh)
    c = SimpleNamespace(pr=9, progress={"published": head})
    saved = os.environ.get("TAUCETI_SELF_TESTED")
    os.environ["TAUCETI_SELF_TESTED"] = env
    try:
        results = stl.label_round_pushes(w, c, stage, started)
    finally:
        if saved is None:
            os.environ.pop("TAUCETI_SELF_TESTED", None)
        else:
            os.environ["TAUCETI_SELF_TESTED"] = saved
    return results, gh


def verified(state, root, base, head):
    write_receipt(state, root, base)


def other_commit(state, root, base, head):
    git(root, "checkout", "-q", "--detach", base)
    write_receipt(state, root, base)
    git(root, "checkout", "-q", head)


def stale(state, root, base, head):
    path = write_receipt(state, root, base)
    os.utime(path, (time.time() - 3600, time.time() - 3600))


def malformed_mode(state, root, base, head):
    path = write_receipt(state, root, base)
    receipt = json.loads(path.read_text())
    inputs_path = Path(receipt["inputs"]["path"])
    inputs = json.loads(inputs_path.read_text())
    inputs["sources"]["TauCeti/A.lean"]["mode"] = ["not a mode"]
    inputs_path.write_text(json.dumps(inputs))
    receipt["fingerprint"] = receipt["inputs"]["sha256"] = verification.digest(inputs)
    path.write_text(json.dumps(receipt))


CASES = [
    ("verified push is labelled", {"receipt": verified}, lambda h: [pull(h), pull(h, labels=LABELLED)], "labelled", 1),
    (
        "receipt for a different commit",
        {"receipt": other_commit},
        lambda h: [pull(h)],
        "no matching receipt (verified sources differ",
        0,
    ),
    (
        "untracked file in the verified sources",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, b, extra="TauCeti/Scratch.lean")},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    (
        "failed verification",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, b, status="failed")},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    (
        "incomplete verification",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, b, status="incomplete")},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    (
        "stage-only verification",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, b, phases=("build",))},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    (
        "full-lint policy verification",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, b, policy="full-lint")},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    ("receipt from an earlier round", {"receipt": stale}, lambda h: [pull(h)], "no matching receipt", 0),
    ("no receipt at all", {}, lambda h: [pull(h)], "no matching receipt (no verification this round)", 0),
    (
        "malformed source mode is not a match",
        {"receipt": malformed_mode},
        lambda h: [pull(h)],
        "no matching receipt",
        0,
    ),
    (
        "infrastructure change",
        {"receipt": verified, "infra": True},
        lambda h: [pull(h)],
        "not eligible (changes outside TauCeti/: lakefile.toml)",
        0,
    ),
    ("already labelled", {"receipt": verified}, lambda h: [pull(h, labels=LABELLED)], "already labelled", 0),
    (
        "persistent label is retained without another receipt",
        {},
        lambda h: [pull(h, labels=LABELLED)],
        "already labelled",
        0,
    ),
    (
        "live head moved since the push",
        {"receipt": verified},
        lambda h: [pull("d" * 40, labels=LABELLED)],
        "not eligible (PR head moved",
        0,
    ),
    (
        "head moving during the label write stops",
        {"receipt": verified},
        lambda h: [pull(h), pull(h), pull("d" * 40)],
        "self-tested stopped",
        1,
    ),
    (
        "head moving during receipt discovery adds no label",
        {"receipt": verified},
        lambda h: [pull(h), pull("d" * 40)],
        "not eligible (PR head moved",
        0,
    ),
    (
        "receipt verified against a base past an infrastructure commit",
        {"receipt": lambda s, r, b, h: write_receipt(s, r, git(r, "rev-parse", "HEAD~1")), "middle": True},
        lambda h: [pull(h)],
        "not eligible (changes outside TauCeti/: lakefile.toml)",
        0,
    ),
    ("internal file symlink", {"receipt": verified, "symlink": True}, lambda h: [pull(h)], "labelled", 1),
    ("unchanged repository symlink", {"receipt": verified, "base_symlink": True}, lambda h: [pull(h)], "labelled", 1),
    ("external file symlink", {"receipt": verified, "symlink": "external"}, lambda h: [pull(h)], "no matching", 0),
    ("escaping file symlink", {"receipt": verified, "symlink": "escaping"}, lambda h: [pull(h)], "no matching", 0),
    (
        "lint-repair label",
        {"receipt": verified},
        lambda h: [pull(h, labels=("lint-repair",))],
        "not eligible (full-lint or lint-repair",
        0,
    ),
    (
        "full-lint label",
        {"receipt": verified},
        lambda h: [pull(h, labels=("full-lint",))],
        "not eligible (full-lint or lint-repair",
        0,
    ),
    (
        "lint-repair branch",
        {"receipt": verified},
        lambda h: [pull(h, ref="lint-repair/x")],
        "not eligible (full-lint or lint-repair",
        0,
    ),
    ("lint-repair round", {"receipt": verified, "stage": "lint-repair"}, lambda h: [pull(h)], "not eligible", 0),
    ("disabled", {"receipt": verified, "env": "0"}, lambda h: [pull(h)], "not eligible (TAUCETI_SELF_TESTED", 0),
]

for name, options, pulls, prefix, labels in CASES:
    with tempfile.TemporaryDirectory() as tmp:
        results, gh = run(tmp, pulls, **options)
    check(f"{name}: one outcome", len(results), 1)
    check(name, results[0][1] if results else "", lambda got, p=prefix: got.startswith(p))
    check(f"{name}: labels added", gh.labelled, labels)
    if prefix.startswith("not eligible (TAUCETI") or name == "lint-repair round":
        check(f"{name}: no GitHub reads", gh.reads, 0)

# Committed sources in the receipt match the git tree, including deletions and the executable bit.
with tempfile.TemporaryDirectory() as tmp:
    root, base, head = checkout(tmp)
    records = stl.commit_sources(root, head, base)
    check("deleted base path is missing", records.get("TauCeti/B.lean"), {"kind": "missing"})
    check("executable bit", records.get("TauCeti/run.sh", {}).get("executable"), True)
    real = {k: stl._comparable(v) for k, v in verification.inventory(root, base).items()}
    check("mirror of the verifier inventory", records, real)
    (root / "TauCeti/run.sh").chmod(0o644)
    real = {k: stl._comparable(v) for k, v in verification.inventory(root, base).items()}
    check("a dropped executable bit differs", records == real, False)

# Link text and transitive committed contents must match; unsupported links stay ineligible.
with tempfile.TemporaryDirectory() as tmp:
    root, base, head = checkout(tmp, symlink=True, base_symlink=True)
    (root / "TauCeti/Alias.lean").symlink_to("Link.lean")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "link chain")
    head = git(root, "rev-parse", "HEAD")
    real = {k: stl._comparable(v) for k, v in verification.inventory(root, base).items()}
    records = stl.commit_sources(root, head, base)
    check("committed link chains mirror verifier inventory", records, real)
    check("relative parent link binds its target", records[".claude/CLAUDE.md"]["target"], "../AGENTS.md")
    (root / "TauCeti/Link.lean").unlink()
    (root / "TauCeti/Link.lean").symlink_to("Alias.lean")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "cycle")
    records = stl.commit_sources(root, git(root, "rev-parse", "HEAD"), base)
    check("link cycles never match", records["TauCeti/Link.lean"]["kind"], "unsupported")
    (root / "TauCeti/Link.lean").unlink()
    (root / "TauCeti/Link.lean").symlink_to("../TauCeti")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "directory link")
    records = stl.commit_sources(root, git(root, "rev-parse", "HEAD"), base)
    check("directory links never match", records["TauCeti/Link.lean"]["kind"], "unsupported")

with tempfile.TemporaryDirectory() as tmp:
    root, base, head = checkout(tmp, symlink=True)
    (root / "TauCeti/Same.lean").write_bytes((root / "TauCeti/A.lean").read_bytes())
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "identical target")
    head = git(root, "rev-parse", "HEAD")
    (root / "TauCeti/Link.lean").unlink()
    (root / "TauCeti/Link.lean").symlink_to("Same.lean")
    records = stl.commit_sources(root, head, base)
    observed = {k: stl._comparable(v) for k, v in verification.inventory(root, base).items()}
    check(
        "retargeted link has identical bytes",
        records["TauCeti/Link.lean"]["sha256"],
        observed["TauCeti/Link.lean"]["sha256"],
    )
    check("link text still distinguishes the verified tree", records == observed, False)
    (root / "TauCeti/Sub").mkdir()
    (root / "TauCeti/Sub/Keep").write_text("directory\n")
    link = root / "TauCeti/Prefix.lean"
    link.symlink_to("Sub/../A.lean")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "real prefix")
    records = stl.commit_sources(root, git(root, "rev-parse", "HEAD"), base)
    check("parent resolution through a real directory", records["TauCeti/Prefix.lean"]["kind"], "symlink")
    (root / "TauCeti/Sub/Keep").unlink()
    (root / "TauCeti/Sub").rmdir()
    (root / "TauCeti/Sub").symlink_to("../TauCeti")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "symlink prefix")
    records = stl.commit_sources(root, git(root, "rev-parse", "HEAD"), base)
    check(
        "parent resolution through a directory link is refused", records["TauCeti/Prefix.lean"]["kind"], "unsupported"
    )
    link.unlink()
    link.symlink_to("Missing/../A.lean")
    (root / "TauCeti/Absolute.lean").symlink_to(root / "TauCeti/A.lean")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "missing prefix and absolute link")
    records = stl.commit_sources(root, git(root, "rev-parse", "HEAD"), base)
    check("missing directory prefix is refused", records["TauCeti/Prefix.lean"]["kind"], "unsupported")
    check("absolute link is refused", records["TauCeti/Absolute.lean"]["kind"], "unsupported")

for malformed in (dict(scope=True), dict(requested_phases=1), dict(requested_phases=["build", {}])):
    receipt = dict(
        schema=verification.SCHEMA,
        status="success",
        phases={name: dict(status="success") for name in verification.STAGES},
        requested_phases=list(verification.STAGES),
        scope=dict(policy="ordinary"),
    )
    check("malformed receipt shape is incomplete", stl._complete({**receipt, **malformed}), False)

# A roadmap round labels the PR opened from the branch it published on the fork.
with tempfile.TemporaryDirectory() as tmp:
    root, base, head = checkout(tmp)
    git(root, "update-ref", "refs/tauceti/published/scope/r1/roadmap/x", head)
    git(root, "update-ref", "refs/tauceti/published/scope/r2/other", base)
    published = stl.scoped_publications(root, "r1")
    check("scoped publications", published, {"roadmap/x": head})
    state = Path(tmp) / "state"
    state.mkdir()
    write_receipt(state, root, base)
    gh = FakeGitHub([pull(head), pull(head, labels=LABELLED)], heads={"me:roadmap/x": 9})
    w = SimpleNamespace(cfg=SimpleNamespace(checkout=root, state=state, logdir=state), gh=gh)
    c = SimpleNamespace(pr=None, progress={"published_branches": published, "published_owner": "me"})
    os.environ["TAUCETI_SELF_TESTED"] = "1"
    try:
        results = stl.label_round_pushes(w, c, "roadmap", time.time() - 5)
        check("roadmap PR labelled", results, lambda got: got and got[0][0] == 9 and got[0][1].startswith("labelled"))
        gh = FakeGitHub([pull(head)], heads={})
        w.gh = gh
        results = stl.label_round_pushes(w, c, "roadmap", time.time() - 5)
        check("roadmap branch without a PR", results, lambda got: "no open PR" in got[0][1])
        check("roadmap branch without a PR: no label", gh.labelled, 0)
    finally:
        del os.environ["TAUCETI_SELF_TESTED"]

# A round that pushed nothing reports nothing.
check("no pushes", stl.round_pushes(SimpleNamespace(pr=9, progress={"head": "x"})), [])

sys.exit(1 if failures else 0)
