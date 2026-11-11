#!/usr/bin/env python3
"""A pushed bump head is labelled self-tested only after the worker verifies that exact commit."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tauceti_worker.bump_attestation as ba  # noqa: E402

failures = []
LABELLED = ("self-tested",)


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r}")
    if not ok:
        failures.append(name)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def checkout(tmp):
    """A checkout whose origin/main is the base commit and whose HEAD is one repair on top."""
    root = Path(tmp) / "code"
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "a.lean").write_text("a\n")
    git(root, "add", "a.lean")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    (root / "a.lean").write_text("b\n")
    git(root, "commit", "-q", "-am", "repair")
    return root, base, git(root, "rev-parse", "HEAD")


def pull(head, *, author="tauceti-review-bot[bot]", labels=()):
    return {
        "state": "open",
        "draft": False,
        "base": {"ref": "main"},
        "head": {"sha": head, "ref": "bump-mathlib/" + "c" * 40, "repo": {"full_name": ba.TAUCETI}},
        "user": {"login": author},
        "labels": [{"name": name} for name in labels],
    }


class FakeGitHub:
    """Serves the scripted PR reads in order (repeating the last) and commit-status reads."""

    def __init__(self, pulls, statuses):
        self.pulls, self.statuses, self.labelled = list(pulls), list(statuses), 0

    def live_pull(self, pr):
        return self.pulls.pop(0) if len(self.pulls) > 1 else self.pulls[0]

    def add_label(self, pr, name):
        self.labelled += 1
        return True

    def commit_status(self, sha, context):
        return self.statuses.pop(0) if self.statuses else ""


def attest(tmp, pulls, statuses, *, verified="success", unchanged=False, bubble=False, dirty=False):
    root, base, head = checkout(tmp)
    if dirty:
        (root / "b.lean").write_text("x\n")
    gh = FakeGitHub(pulls(head), statuses)
    calls = []
    saved = ba.verify
    ba.verify = lambda _w, _checkout, got_base: calls.append(got_base) or {"status": verified}
    ticks = iter(range(0, 10**6, 5))
    try:
        w = SimpleNamespace(cfg=SimpleNamespace(checkout=root, state=Path(tmp), logdir=Path(tmp)), gh=gh)
        outcome = ba.attest(
            w, 9, head if unchanged else base, bubble=bubble, sleep=lambda _s: None, clock=lambda: next(ticks)
        )
    finally:
        ba.verify = saved
    return outcome, calls, gh.labelled, base


CASES = [
    # name, options, PR reads, status reads, outcome prefix, verifier calls, labels added
    (
        "verified head is attested",
        {},
        lambda h: [pull(h), pull(h), pull(h, labels=LABELLED)],
        ["", "success"],
        "attested",
        1,
        1,
    ),
    ("bubble rounds keep ordinary CI", {"bubble": True}, lambda h: [pull(h)], [], "skipped: bubble", 0, 0),
    ("dirty checkout is not attested", {"dirty": True}, lambda h: [pull(h)], [], "skipped: checkout has", 0, 0),
    ("unchanged head is not verified", {"unchanged": True}, lambda h: [pull(h)], [], "skipped: the round pushed", 0, 0),
    ("moved head is not verified", {}, lambda h: [pull("d" * 40)], [], "skipped: PR head moved", 0, 0),
    ("non-bot author is refused", {}, lambda h: [pull(h, author="someone")], [], "skipped: not authored", 0, 0),
    ("held bump is refused", {}, lambda h: [pull(h, labels=("hold",))], [], "skipped: held", 0, 0),
    ("failed verification adds no label", {"verified": "failed"}, lambda h: [pull(h)], [], "not attested: local", 1, 0),
    (
        "head moving during verification adds no label",
        {},
        lambda h: [pull(h), pull("d" * 40)],
        [],
        "not attested after verification",
        1,
        0,
    ),
    ("picker refusal is reported", {}, lambda h: [pull(h)], ["failure"], "picker refused", 1, 1),
    # The push's own picker run can remove a label applied while it is in flight: label again once.
    (
        "removed label is applied again",
        {},
        lambda h: [pull(h), pull(h), pull(h), pull(h, labels=LABELLED)],
        ["", "", "success"],
        "attested",
        1,
        2,
    ),
    ("no assertion after two labels falls back", {}, lambda h: [pull(h)], [], "assertion not observed", 1, 2),
]

for name, options, pulls, statuses, prefix, verify_calls, labels in CASES:
    with tempfile.TemporaryDirectory() as tmp:
        outcome, calls, labelled, base = attest(tmp, pulls, statuses, **options)
    check(name, outcome, lambda got, p=prefix: got.startswith(p))
    check(f"{name}: verifier calls", len(calls), verify_calls)
    check(f"{name}: labels added", labelled, labels)
    if calls:
        check(f"{name}: verified against origin/main", calls[0], base)

# The kill switch keeps every bump on ordinary CI.
os.environ["TAUCETI_BUMP_SELF_TESTED"] = "0"
try:
    w = SimpleNamespace(cfg=SimpleNamespace(checkout=Path("/nonexistent")), gh=None)
    check("disabled by environment", ba.attest(w, 9, "", bubble=False), lambda got: "disabled" in got)
finally:
    del os.environ["TAUCETI_BUMP_SELF_TESTED"]

sys.exit(1 if failures else 0)
