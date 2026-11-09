"""Operator-driven Forgejo CI and promotion using the existing request ledger.

This does not start the autonomous controller or use its native authority API.
An explicitly configured human accepts one current PR revision; existing CI
contracts, Git refs, request snapshots and verified run evidence do the rest.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable, NoRedirect
from tauceti_tools.forge import ForgeError, Forgejo
from tauceti_tools.project import parse
from tauceti_tools.system import ensure_ssl_cert_file

from . import chain, github_app
from .ci_execution import ExecutionEvidence, GitHubSourceExecution
from .ci_requests import RequestLedger
from .control_policy_file import read_control_policy
from .forgejo_git import ForgejoGitSource
from .github_adapter import UrllibTransport
from .ports import DispatchNotAccepted
from .source_ci import SourceCandidateCI
from .store import Store

SCHEMA = "tauceti-stage1.operator/v1"


class ReadOnlyExecution(GitHubSourceExecution):
    """Reuse verified discovery/results without granting a read token dispatch authority."""

    def __init__(self, policy, token, *, clock=time.time):
        self.policy, self.read_token, self.clock, self.max_pages = policy, token, clock, 10
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=github_app.ssl_context()),
            NoRedirect(),
        )

    def _reader(self, frozen, *, token=None):
        if frozen.policy != self.policy or token is not None:
            raise ValueError("execution observation differs from the approved read-only policy")
        frozen.request.check_policy(self.policy)
        return ExecutionEvidence(self.policy, token=self.read_token, token_origin=cc.GITHUB, opener=self.opener)

    def _post(self, *args, **kwargs):
        raise ValueError("read-only execution cannot send HTTP writes")

    def preflight(self, frozen):
        raise DispatchNotAccepted(
            "workflow mode requires explicit operator handoff, not a read-token dispatch", retryable=False
        )

    def dispatch(self, frozen, *, prepared=None):
        raise DispatchNotAccepted("read-only execution cannot send candidate events", retryable=False)

    def cancel(self, *args, **kwargs):
        raise ValueError("read-only execution cannot cancel runs")


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("a full immutable 40-hex commit is required")
    return value


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("operator profile paths must be absolute")
    return Path(value)


def _read_json(path):
    return cc.read_json(read_control_policy(_path(str(path))), max_bytes=128 * 1024)


class Operator:
    def __init__(self, profile, *, execution=None, client=None, source=None, clock=time.time):
        expected = {
            "schema",
            "configuration",
            "execution_policy",
            "workflow_revision",
            "clone",
            "database",
            "source_token_file",
            "execution_credentials_dir",
            "reviewer_ids",
        }
        optional = {"execution_mode", "execution_read_token_file"}
        if not isinstance(profile, dict) or set(profile) - optional != expected or profile["schema"] != SCHEMA:
            raise ValueError("invalid Stage 1 operator profile")
        reviewers = profile["reviewer_ids"]
        if (
            not isinstance(reviewers, list)
            or not reviewers
            or len(reviewers) > 32
            or any(type(value) is not int or value < 1 for value in reviewers)
            or len(set(reviewers)) != len(reviewers)
        ):
            raise ValueError("operator profile needs explicit distinct native reviewer IDs")
        self.configuration = read_control_policy(_path(profile["configuration"]))
        self.policy = cc.ExecutionPolicy.parse(profile["execution_policy"], configuration=self.configuration)
        self.project = parse(self.configuration)
        self.workflow_revision = _sha(profile["workflow_revision"])
        self.clone, self.database = _path(profile["clone"]), _path(profile["database"])
        if self.clone.is_symlink() or not self.clone.is_dir() or self.database.is_symlink():
            raise ValueError("operator clone/database must use explicit real paths")
        self.reviewers, self.clock = frozenset(reviewers), clock
        raw_token = read_control_policy(_path(profile["source_token_file"]))
        token = raw_token.decode("ascii").strip()
        ensure_ssl_cert_file()
        self.api = client or Forgejo(self.policy.credential_origin, token=token)
        self.source = source or ForgejoGitSource(self.policy.source, self.api, self.clone, token=token)
        if getattr(self.source, "repository_identity", None) != self.policy.source:
            raise ValueError("operator Git source differs from its approved policy")
        mode = profile.get("execution_mode", "app")
        if mode not in {"app", "workflow"}:
            raise ValueError("operator execution mode must be app or workflow")
        if execution is None and mode == "workflow":
            read_token = read_control_policy(_path(profile.get("execution_read_token_file"))).decode("ascii").strip()
            execution = ReadOnlyExecution(self.policy, read_token, clock=clock)
        elif execution is None:
            # Existing App credentials mint a token restricted to this exact
            # execution repository; no ambient gh/PAT fallback is accepted.
            credentials = github_app.load_execution_credentials(_path(profile["execution_credentials_dir"]))
            tokens = github_app.InstallationTokenSource(
                credentials,
                UrllibTransport(),
                repository_id=self.policy.execution.native_id,
                permissions={"contents": "write", "actions": "write"},
            )
            execution = GitHubSourceExecution(tokens)
        self.execution = execution
        self.execution_mode = mode
        self.store = None

    @contextmanager
    def session(self):
        """One operator process per existing ledger, including uncertain sends."""
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.database.with_name(self.database.name + ".native-service.lock")
        with open(lock, "a", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)) as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError("operator lock must be an owned private regular file")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store = Store(str(self.database))
            try:
                # These commands reuse the queue's tables, but must never share
                # a writable ledger with its autonomous controller. Both entry
                # points claim this setting in a transaction before other writes.
                with self.store.transaction():
                    owner = self.store.get_setting("queue:operating-mode")
                    if owner not in {None, "stage1-operator"}:
                        raise ValueError("controller-owned ledger cannot be used by the Stage 1 operator")
                    if owner is None and any(
                        self.store.get_setting(f"stage1:acceptance:{row['id']}") is None
                        for row in self.store.rows("ci_attempts")
                    ):
                        raise ValueError("use an empty ledger; existing controller work requires explicit transfer")
                    self.store.set_setting("queue:operating-mode", "stage1-operator")
                previous = self.store.get_setting("stage1:reviewers")
                current = sorted(self.reviewers)
                with self.store.transaction():
                    # A typo before any request exists must not permanently
                    # bind an unused ledger. Frozen requests retain their own
                    # policy; populated ledgers keep the existing role binding.
                    if self.store.count("ci_attempts") == 0:
                        self.store.set_setting("source-ci:binding", None)
                    self.ledger = RequestLedger(
                        self.store,
                        self.clone,
                        self.policy,
                        self.configuration,
                        workflow_revision=self.workflow_revision,
                    )
                    if previous is not None and previous != current:
                        self.store.journal(
                            self.clock(), "stage1-reviewer-policy-changed", previous=previous, current=current
                        )
                    self.store.set_setting("stage1:reviewers", current)
                self.ci = SourceCandidateCI(self.ledger, self.execution, clock=self.clock)
                yield self
            finally:
                self.store.close()
                self.store = None

    def _actor(self):
        self.source._bound(self.policy.source.repository.key)
        actor = self.api.current_user()
        native_id = actor.get("id")
        if type(native_id) is not int or native_id not in self.reviewers:
            raise ValueError("authenticated source actor is not an approved Stage 1 reviewer")
        access = self.api.repository(self.policy.source.repository)
        if access.get("permissions", {}).get("push") is not True:
            raise ValueError("operator source actor lacks current repository write permission")
        return native_id

    def _pull(self, number, head):
        row = self.api.pull(self.policy.source.repository, number)
        if (
            row.get("state") != "open"
            or row.get("merged") is not False
            or row.get("draft") is not False
            or row.get("head", {}).get("sha") != head
            or row.get("base", {}).get("ref") != self.policy.base_branch
            or row.get("base", {}).get("repo", {}).get("id") != self.policy.source.native_id
        ):
            raise ValueError("PR is not an open, non-draft proposal at the accepted head/base repository")
        if self.api.pull_ref(self.policy.source.repository, number) != head:
            raise ValueError("PR retained head has not synchronized; retry after re-observing")
        return row

    def prepare(self, number, accepted_head):
        """Explicit human acceptance, immutable candidate creation, then durable freeze."""
        if type(number) is not int or number < 1:
            raise ValueError("PR number must be positive")
        head = _sha(accepted_head)
        actor = self._actor()
        row = self._pull(number, head)
        base = self.source.ref_sha(self.policy.source.repository.key, "refs/heads/" + self.policy.base_branch)
        _sha(base)
        # Exact repeats return the prior request; a retry does not allocate a
        # new request or dispatch merely because the operator command restarted.
        for attempt in self.store.rows("ci_attempts", {"base_sha": base}, order="id DESC"):
            if attempt["outcome"] == "superseded":
                continue
            frozen = self.ledger.load(attempt["id"])
            accepted = self.store.get_setting(f"stage1:acceptance:{attempt['id']}") or {}
            if (
                frozen.request.base == base
                and frozen.request.accepted_members == (cc.Member(number, head),)
                and frozen.policy == self.policy
                and frozen.request.workflow_revision == self.workflow_revision
                and accepted.get("reviewer_id") in self.reviewers
            ):
                return self._summary(frozen)
        self.source.fetch_refs(
            self.policy.source.repository.key, [base, f"+refs/pull/{number}/head:refs/heads/tauceti-stage1-head"]
        )
        if chain.resolve_commit(self.clone, "refs/heads/tauceti-stage1-head") != head:
            raise ValueError("PR head changed while fetching; accept the new revision explicitly")
        member = chain.Member(
            self.policy.source.repository.key,
            number,
            head,
            title=row.get("title") or f"PR #{number}",
            description=row.get("body") or "",
        )
        built = chain.build(self.clone, base, [member])
        if built.status != chain.CLEAN:
            raise ValueError("candidate conflicts with its captured base; repair and accept a new head")
        for record in built.commits:
            if record.landed_on_base:
                message = chain.git(self.clone, "show", "-s", "--format=%B", record.landed_on_base)
                if set(chain.trailer_values(message, chain.ACCEPTED_HEAD_TRAILER)) != {head}:
                    raise ValueError(
                        "an earlier revision of this PR landed; preserve the new work and ask the contributor "
                        "to rebase onto main or open a new PR before fresh review"
                    )
        if not built.commits or any(record.empty or record.landed_on_base for record in built.commits):
            raise ValueError("PR is already landed or has no changes to apply; settle it without another CI run")
        # CI executes the trusted policy and sandbox; local preparation does
        # no checkout or execution of contributor-provided code.
        self._pull(number, head)
        if self.source.ref_sha(self.policy.source.repository.key, "refs/heads/" + self.policy.base_branch) != base:
            raise ValueError("source base changed while preparing; prepare against the new base")
        now, request_id = self.clock(), secrets.token_hex(16)
        evidence = {
            "schema": "tauceti-stage1.acceptance/v1",
            "source": self.policy.source.record(),
            "pr": number,
            "head": head,
            "base": base,
            "candidate": built.tip,
            "policy_digest": self.policy.digest,
            "reviewer_id": actor,
            "accepted_at": now,
        }
        with self.store.transaction():
            batch = self.store.insert(
                "batches",
                repository=self.policy.source.repository.key,
                version=1,
                base=base,
                tip=built.tip,
                state="head",
                created_at=now,
            )
            attempt_id = self.store.insert(
                "ci_attempts",
                batch_id=batch,
                batch_version=1,
                head_sha=built.tip,
                base_sha=base,
                base_ref=self.policy.base_branch,
                request_id=request_id,
                dispatch_state="prepared",
                dispatched_at=now,
            )
            self.store.set_setting(f"stage1:acceptance:{attempt_id}", evidence, actor=str(actor), at=now)
            self.ledger.freeze(attempt_id, (cc.Member(number, head),), now=now)
            self.store.journal(now, "stage1-accepted", batch_id=batch, **evidence)
        return self._summary(self.ledger.load(attempt_id))

    def _summary(self, frozen):
        attempt = self.store.row("ci_attempts", {"id": frozen.attempt_id})
        return {
            "attempt_id": frozen.attempt_id,
            "request": frozen.request.record(),
            "dispatch_state": attempt["dispatch_state"],
            "owner_run_id": self.ledger.owner_run(frozen.attempt_id),
        }

    def dispatch(self, attempt_id):
        self._actor()
        self._current(attempt_id)
        attempt = self.store.row("ci_attempts", {"id": attempt_id})
        if attempt["dispatch_state"] != "prepared":
            # A missing owner after a lost response never licenses another POST.
            return self.observe(attempt_id)
        prepared = self.ci.prepare_source(attempt_id, self.source)
        self._current(attempt_id)
        with self.store.transaction():
            claimed = self.store.update(
                "ci_attempts",
                {"id": attempt_id, "dispatch_state": "prepared"},
                dispatch_state="sending",
                sent_at=self.clock(),
            )
            if claimed != 1:
                raise ValueError("dispatch send claim changed; observe the original attempt")
        try:
            self.ci.dispatch(attempt_id, prepared=prepared)
        except DispatchNotAccepted as error:
            with self.store.transaction():
                self.store.update(
                    "ci_attempts",
                    {"id": attempt_id},
                    dispatch_state="prepared",
                    dispatch_error=str(error),
                    dispatch_retryable=int(error.retryable),
                )
            raise
        self.store.update("ci_attempts", {"id": attempt_id}, dispatch_state="accepted")
        return self._summary(self.ledger.load(attempt_id))

    def handoff(self, attempt_id):
        """Retain refs and persist a single manual workflow handoff before returning its payload."""
        self._actor()
        if self.execution_mode != "workflow":
            raise ValueError("manual handoff requires workflow execution mode")
        frozen = self._current(attempt_id)
        row = self.store.row("ci_attempts", {"id": attempt_id})
        if row["dispatch_state"] != "prepared":
            raise ValueError("request was already handed off or sent; observe the original instead of resending")
        # Read-only repository/default-branch checks use the existing evidence
        # reader. The protected parent, not this credential, owns the POST.
        reader = self.execution._reader(frozen)
        metadata = reader.repository()
        current = reader.get("/branches/" + quote(self.policy.execution_branch, safe=""))
        if (
            metadata.get("default_branch") != self.policy.execution_branch
            or current.get("commit", {}).get("sha") != frozen.request.workflow_revision
        ):
            raise ValueError("protected execution caller changed before workflow handoff")
        self.source.retain_candidate(frozen)
        self._current(attempt_id)
        with self.store.transaction():
            self.store.update("ci_attempts", {"id": attempt_id}, dispatch_state="handoff", sent_at=self.clock())
            self.store.journal(
                self.clock(),
                "stage1-manual-handoff",
                batch_id=frozen.request.batch_id,
                request_id=frozen.request.request_id,
            )
        return self.export_handoff(attempt_id)

    def export_handoff(self, attempt_id):
        """Recover the same payload without changing state or sending an event.

        Exporting is not permission to invoke a second parent workflow. Observe
        an uncertain original; a known failed child can be rerun in place.
        """
        frozen = self.ledger.load(attempt_id)
        row = self.store.row("ci_attempts", {"id": attempt_id})
        if self.execution_mode != "workflow" or row["dispatch_state"] != "handoff":
            raise ValueError("only an existing workflow handoff can be exported")
        return {"request": json.dumps(frozen.request.record(), sort_keys=True, separators=(",", ":"))}

    def _current(self, attempt_id):
        frozen = self.ledger.load(attempt_id)
        attempt = self.store.row("ci_attempts", {"id": attempt_id})
        if attempt["outcome"] == "superseded":
            raise ValueError("request was abandoned; late results cannot authorize promotion")
        if frozen.policy != self.policy:
            raise ValueError("archived request differs from the selected policy")
        if frozen.request.workflow_revision != self.workflow_revision:
            raise ValueError("protected caller revision changed; accept and test a new request")
        accepted = self.store.get_setting(f"stage1:acceptance:{attempt_id}")
        if (
            not isinstance(accepted, dict)
            or accepted.get("reviewer_id") not in self.reviewers
            or accepted.get("policy_digest") != frozen.policy.digest
            or accepted.get("candidate") != frozen.request.head
            or accepted.get("source") != frozen.policy.source.record()
            or accepted.get("base") != frozen.request.base
            or [(accepted.get("pr"), accepted.get("head"))]
            != [(member.pr, member.head) for member in frozen.request.accepted_members]
        ):
            raise ValueError("candidate has no matching explicit Stage 1 acceptance")
        for member in frozen.request.accepted_members:
            self._pull(member.pr, member.head)
        current = self.source.ref_sha(self.policy.source.repository.key, "refs/heads/" + self.policy.base_branch)
        if current != frozen.request.base:
            raise ValueError("source main changed; prepare and test a new candidate")
        return frozen

    def observe(self, attempt_id, *, run_id=None):
        frozen = self.ledger.load(attempt_id)
        if self.store.row("ci_attempts", {"id": attempt_id})["dispatch_state"] == "prepared" and run_id is None:
            raise ValueError("candidate is prepared but has not been dispatched or handed off")
        if run_id is not None:
            observed = self.execution.observe(frozen, str(run_id))
            if observed.ownership is None:
                raise EvidenceUnavailable("named run has no verified request ownership")
            self.ledger.record_owned_run(attempt_id, observed.run, observed.ownership, now=self.clock())
        observation = self.ci.observe(attempt_id)
        result = {
            **self._summary(frozen),
            "run_id": observation.run.run_id,
            "run_attempt": observation.run.attempt,
            "run_status": observation.run.status,
            "classification": (observation.result or {}).get("classification", "unavailable"),
            "result_absent": getattr(observation, "result_absent", False),
        }
        if observation.result is not None:
            self.store.set_setting(f"stage1:result:{attempt_id}", observation.result, at=self.clock())
        return result

    def promote(self, attempt_id):
        actor = self._actor()
        frozen = self.ledger.load(attempt_id)
        current = self.source.ref_sha(self.policy.source.repository.key, "refs/heads/" + self.policy.base_branch)
        intent = self.store.get_setting(f"stage1:promotion-intent:{attempt_id}")
        if isinstance(intent, dict):
            self.source.fetch_refs(self.policy.source.repository.key, [current])
        if current == frozen.request.head or (
            isinstance(intent, dict) and chain.is_ancestor(self.clone, frozen.request.head, current)
        ):
            if (
                not isinstance(intent, dict)
                or intent.get("request_digest") != frozen.request.digest
                or intent.get("classification") != "passed"
            ):
                raise ValueError(
                    "source already moved without a recorded verified promotion intent; inspect before adoption"
                )
            # A lost push acknowledgement is a read-only recovery result.
            self.store.journal(
                self.clock(),
                "stage1-promotion-confirmed",
                batch_id=frozen.request.batch_id,
                candidate=frozen.request.head,
                actor=actor,
            )
            self._completed(frozen, intent["run_id"])
            return {"promoted": True, "candidate": frozen.request.head, "current_main": current, "reconciled": True}
        frozen = self._current(attempt_id)
        observed = self.observe(attempt_id)
        if observed["classification"] in {"source_failure", "policy_failure"}:
            raise ValueError("candidate failed CI; repair the source and accept its new head")
        if observed["classification"] == "infrastructure" or observed["result_absent"]:
            raise ValueError("owning child has an infrastructure failure or no result; inspect and rerun that child")
        if observed["classification"] != "passed":
            raise EvidenceUnavailable("promotion requires current verified passing GitHub result evidence")
        # Re-read native heads/base after execution observation and immediately
        # before the expected-old Git update. Rename/deletion is an operator
        # operation and must not overlap this manual publication window.
        self._current(attempt_id)
        self.store.set_setting(
            f"stage1:promotion-intent:{attempt_id}",
            {
                "request_digest": frozen.request.digest,
                "classification": "passed",
                "candidate": frozen.request.head,
                "run_id": observed["run_id"],
                "actor": actor,
            },
            actor=str(actor),
            at=self.clock(),
        )
        result = self.source.fast_forward(
            self.policy.source.repository.key, self.policy.base_branch, frozen.request.base, frozen.request.head
        )
        if not result.ok:
            raise ValueError("source main was not promoted; reconcile its exact current revision")
        self.store.journal(
            self.clock(),
            "stage1-promoted",
            batch_id=frozen.request.batch_id,
            candidate=frozen.request.head,
            actor=actor,
            run_id=observed["run_id"],
        )
        self._completed(frozen, observed["run_id"])
        return {"promoted": True, "candidate": frozen.request.head, "run_id": observed["run_id"]}

    def _completed(self, frozen, run_id):
        with self.store.transaction():
            self.store.update("batches", {"id": frozen.request.batch_id}, state="done")
            self.store.update(
                "ci_attempts", {"id": frozen.attempt_id}, outcome="pass", run_id=str(run_id), needs_observation=0
            )

    def abandon(self, attempt_id, reason):
        """Retire an uncertain request without reusing its identity or paid output."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("abandonment requires a nonempty reason of at most 1000 characters")
        actor = self._actor()
        frozen = self.ledger.load(attempt_id)
        if self.store.get_setting(f"stage1:promotion-intent:{attempt_id}") is not None:
            raise ValueError("promotion may already have happened; reconcile it before abandoning this request")
        with self.store.transaction():
            self.store.update(
                "ci_attempts",
                {"id": attempt_id},
                outcome="superseded",
                dispatch_state="rejected",
                outcome_detail="operator abandoned: " + reason.strip(),
                needs_observation=0,
            )
            self.store.update("batches", {"id": frozen.request.batch_id}, state="superseded")
            self.store.journal(
                self.clock(),
                "stage1-abandoned",
                actor=actor,
                request_id=frozen.request.request_id,
                batch_id=frozen.request.batch_id,
                reason=reason.strip(),
            )
        return {"abandoned": True, "attempt_id": attempt_id, "request_id": frozen.request.request_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="absolute private operator profile JSON")
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", help="record explicit human acceptance and freeze one PR candidate")
    prepare.add_argument("--pr", type=int, required=True)
    prepare.add_argument("--accept-head", required=True, help="full revision reviewed by the authenticated operator")
    for name in ("dispatch", "handoff", "export-handoff", "observe", "promote", "abandon"):
        command = actions.add_parser(name)
        command.add_argument("--attempt", type=int, required=True)
        if name == "observe":
            command.add_argument("--run-id", help="adopt only after verifying the named run's receipt/result")
        if name in {"handoff", "export-handoff"}:
            command.add_argument("--out", required=True, help="private JSON input file for gh workflow run --json")
        if name == "abandon":
            command.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        # Reserve/validate the output before recording a handoff. If a later
        # write fails, export-handoff recovers the durable frozen payload.
        from contextlib import nullcontext

        writing = args.action in {"handoff", "export-handoff"}
        target = _path(args.out) if writing else None
        output_context = (
            open(target, "x", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600))
            if writing
            else nullcontext()
        )
        with output_context as output:
            with Operator(_read_json(args.profile)).session() as operator:
                result = (
                    operator.prepare(args.pr, args.accept_head)
                    if args.action == "prepare"
                    else operator.observe(args.attempt, run_id=args.run_id)
                    if args.action == "observe"
                    else operator.abandon(args.attempt, args.reason)
                    if args.action == "abandon"
                    else getattr(operator, args.action.replace("-", "_"))(args.attempt)
                )
            if writing:
                json.dump(result, output)
                output.flush()
                os.fsync(output.fileno())
        if writing:
            print(json.dumps({"input_file": str(target), "attempt_id": args.attempt, "dispatch_state": "handoff"}))
            return 0
        print(json.dumps(result, sort_keys=True))
        return 0
    except EvidenceUnavailable as error:
        print(f"waiting: {error}", file=sys.stderr)
        return 77
    except (ValueError, ForgeError, OSError, DispatchNotAccepted) as error:
        print(f"operator recovery required: {error}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
