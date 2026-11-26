#!/usr/bin/env python3
"""Which repository a worker publishes its cooperative claims to.

The ladder is: `$CLAIM_REPO` verbatim, else the shared namespace once this account can push to it,
else the contributor's own fork. Canonical is never chosen — nobody outside the org can push there,
so naming it means every acquire errors and a whole fleet works unclaimed. Everything here stubs
`gh_run`, so no test touches GitHub.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker import github as gh_mod  # noqa: E402
from tauceti_worker.config import Die  # noqa: E402
from tauceti_worker.constants import CLAIMS, TAUCETI  # noqa: E402


class Stub:
    """Replaces `gh_run` with a table of canned results and records every call.

    `push` is what `repos/<CLAIMS>` reports for `.permissions.push` ("true" / "false" / a failure).
    `invitations` is the id `gh api /user/repository_invitations` yields for CLAIMS ("" for none).
    """

    def __init__(self, *, push="false", invitations="", accept_ok=True, fork="alice/TauCeti"):
        self.push = push
        self.invitations = invitations
        self.accept_ok = accept_ok
        self.fork = fork
        self.calls = []
        self.saved = {name: getattr(gh_mod, name) for name in ("gh_run", "ensure_fork")}

    def __enter__(self):
        gh_mod.gh_run = self._gh_run
        gh_mod.ensure_fork = self._ensure_fork
        gh_mod._resolve_claims_repo.cache_clear()
        return self

    def __exit__(self, *_exc):
        for name, fn in self.saved.items():
            setattr(gh_mod, name, fn)
        gh_mod._resolve_claims_repo.cache_clear()

    def _ok(self, out=""):
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def _fail(self, err="boom"):
        return SimpleNamespace(returncode=1, stdout="", stderr=err)

    def _ensure_fork(self):
        if self.fork is None:
            raise Die("resolved your fork, but this `gh` account cannot push to it")
        return self.fork

    def _gh_run(self, argv, **_kwargs):
        self.calls.append(argv)
        if argv[:3] == ["gh", "api", f"repos/{CLAIMS}"]:
            return self._fail() if self.push is None else self._ok(self.push)
        if argv[:3] == ["gh", "api", "/user/repository_invitations"]:
            self.invitation_filter = argv[-1]
            return self._ok(self.invitations)
        if argv[:4] == ["gh", "api", "-X", "PATCH"]:
            self.accepted = argv[-1]
            return self._ok() if self.accept_ok else self._fail("410 Gone")
        raise AssertionError(f"unexpected gh call: {argv}")


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def override_wins_without_asking_github():
    import os

    saved = os.environ.get("CLAIM_REPO")
    os.environ["CLAIM_REPO"] = "coordination/claims"
    try:
        with Stub() as s:
            assert gh_mod.claims_repo() == "coordination/claims"
            assert s.calls == [], "an operator override must not cost an API call"
    finally:
        if saved is None:
            os.environ.pop("CLAIM_REPO", None)
        else:
            os.environ["CLAIM_REPO"] = saved


def shared_when_granted():
    with Stub(push="true"):
        assert gh_mod.claims_repo() == CLAIMS


def fork_when_not_granted():
    with Stub(push="false") as s:
        assert gh_mod.claims_repo() == "alice/TauCeti"
        assert not any(TAUCETI in " ".join(c) for c in s.calls), "canonical is never probed as a claim repo"


def fork_when_access_is_unknown():
    # A failed probe (network, rate limit, or a CLAIMS we cannot see) must pick the repo that always
    # works, not the one that would error on every acquire for the rest of the round.
    with Stub(push=None):
        assert gh_mod.claims_repo() == "alice/TauCeti"


def pending_invitation_is_accepted_then_shared():
    class Accepting(Stub):
        def _gh_run(self, argv, **kwargs):
            r = super()._gh_run(argv, **kwargs)
            if argv[:4] == ["gh", "api", "-X", "PATCH"]:
                self.push = "true"  # the grant lands the moment the invitation is accepted
            return r

    with Accepting(push="false", invitations="4242") as s:
        assert gh_mod.claims_repo() == CLAIMS
        assert s.accepted == "/user/repository_invitations/4242"


def only_the_claims_invitation_is_ever_accepted():
    with Stub(push="false", invitations="") as s:
        assert gh_mod.claims_repo() == "alice/TauCeti"
        # No invitation matched, so nothing was accepted, and the filter that decides is an exact
        # full_name match: the worker must never become an accept-anything button.
        assert not hasattr(s, "accepted")
        assert f'.repository.full_name == "{CLAIMS}"' in s.invitation_filter


def an_unacceptable_invitation_falls_through():
    with Stub(push="false", invitations="4242", accept_ok=False):
        assert gh_mod.claims_repo() == "alice/TauCeti"


def no_writable_namespace_never_raises():
    # Claims are [COOP]. If neither the shared repo nor a fork is available, the worker names the
    # shared repo, fails every acquire, and proceeds unclaimed — it does not fail the round.
    with Stub(push="false", fork=None):
        assert gh_mod.claims_repo() == CLAIMS


def resolution_is_cached_per_process():
    with Stub(push="true") as s:
        assert gh_mod.claims_repo() == CLAIMS
        assert gh_mod.claims_repo() == CLAIMS
        assert len(s.calls) == 1, "the claim namespace is resolved once, not once per task"


fails = sum(
    check(name, case)
    for name, case in (
        ("$CLAIM_REPO wins verbatim, with no API call", override_wins_without_asking_github),
        ("the shared namespace is used once push access is granted", shared_when_granted),
        ("an ungranted account falls back to its own fork", fork_when_not_granted),
        ("an unknown access answer falls back to the fork", fork_when_access_is_unknown),
        ("a pending invitation is accepted and the namespace re-checked", pending_invitation_is_accepted_then_shared),
        ("no other repository's invitation is ever accepted", only_the_claims_invitation_is_ever_accepted),
        ("an invitation that cannot be accepted falls through to the fork", an_unacceptable_invitation_falls_through),
        ("no writable namespace proceeds unclaimed rather than raising", no_writable_namespace_never_raises),
        ("the namespace is resolved once per process", resolution_is_cached_per_process),
    )
)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
