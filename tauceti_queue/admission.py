"""Primary deterministic intake in the existing queue service and approval-poll thread.

GitHub reads happen outside the controller/store locks. Only the generation-fenced application
of a decision joins the controller's existing admission transaction. A request failure leaves the
same PR pending in SQLite, rather than turning it into a policy refusal.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import logging
import threading
import time
from contextlib import nullcontext
from datetime import datetime

from runner import admission_policy as policy
from tauceti_tools.self_tested import accepted
from tauceti_tools.self_tested import context as local_context

from .admission_state import AdmissionState
from .eligibility import KEEP
from .github_adapter import RequestDeferred
from .store import dumps, loads

log = logging.getLogger(__name__)
EXPLICIT_WITHDRAWALS = frozenset(
    {
        "unapproved",
        "cancelled",
        "conflicts-with-main",
        "missing-source",
        "landed-externally",
        "problem-anticipated",
        "empty-commit",
    }
)
CONTEXTS = ("build", "scope", "bump-guard")
HUMAN_INFRASTRUCTURE_APPROVAL_VERSION = 1


def approval_binding(current):
    if not current:
        return None
    evidence = loads(current["evidence"], {})
    return (
        current["id"],
        current["state"],
        current["head_sha"],
        *(
            evidence.get(key)
            for key in ("kind", "admin", "actor_id", "actor_login", "head_sha", "merge_base_sha", "comment_id")
        ),
    )


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def timestamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def source_version(source: dict, revision: str, no_repair_labels=()) -> str:
    """Exclude queue output and incidental comments; include CI independently of PR updated_at."""
    if source.get("draft") or source.get("state") != "open":
        return digest({"policy": revision, **{key: source.get(key) for key in ("head", "base_ref", "state", "draft")}})
    return digest(
        {
            "policy": revision,
            **{key: source.get(key) for key in ("head", "base_ref", "state", "draft", "body")},
            "diff": source.get("diff_version") or source.get("merge_base"),
            "labels": sorted(
                {s.lower() for s in source.get("labels", ())}
                & (
                    KEEP
                    | {s.lower() for s in no_repair_labels}
                    | {"needs-rebase", "self-tested", "full-lint", "lint-repair"}
                )
            ),
            "comments": [
                {
                    **{key: c.get(key) for key in ("id", "body", "created_at", "updated_at")},
                    "author": c.get("author") or (c.get("user") or {}).get("login"),
                }
                for c in source.get("comments", ())
                if relevant_comment(c)
            ],
            "statuses": {
                key: (source.get("statuses", {}).get(key) or "").lower()
                for key in sorted(
                    set(CONTEXTS)
                    | {k for k in source.get("statuses", {}) if k.startswith(("self-tested/", "_native_"))}
                )
            },
        }
    )


def recovery_heads(comments):
    return policy.rebase_request_heads(comments) | policy.rebase_request_heads(comments, stalled=True)


def relevant_comment(comment):
    body = comment.get("body") or ""
    return (
        policy.SCOREBOARD_MARKER in body
        or bool(recovery_heads([comment]))
        or body.startswith("Merge-queue recovery for head `")
        and "<!--tauceti-source:v2 " in body
    )


class AdmissionPolicy:
    def __init__(self, *, rubrics, prefix: str, allow_files, repository: str = ""):
        self.rubrics = tuple(rubrics)
        self.repository = repository
        self.prefix = prefix
        self.allow_files = tuple(allow_files)
        if not self.rubrics or not prefix or not all(self.rubrics):
            raise ValueError("admission requires trusted rubrics and a path prefix")
        # The installed, pinned source is trusted service code, never a PR checkout.
        source = inspect.getsource(policy)
        self.source_revision = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        try:
            metadata = loads(importlib.metadata.distribution("tauceti-review").read_text("direct_url.json"), {})
            commit = metadata.get("vcs_info", {}).get("commit_id")
            if isinstance(commit, str) and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit):
                self.source_revision = commit
        except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
            pass  # unpackaged source still records its exact source digest
        self.revision = digest(
            {
                "code": source,
                "rubrics": self.rubrics,
                "prefix": prefix,
                "allow_files": self.allow_files,
                "human_infrastructure_approval": HUMAN_INFRASTRUCTURE_APPROVAL_VERSION,
            }
        )

    def rubrics_for(self, number):
        """The rubrics PR `number` must have green: the configured ones, then any the pinned review
        policy is phasing in for this repository's PRs above its cutoff.

        The review engine runs a rubric being phased in only on those PRs, so requiring it of an
        earlier one would hold that PR for a row no review adds. The table is the pinned policy's:
        it reaches this service when the pin moves, and a pin that predates it requires the
        configured rubrics of every PR. Each forge numbers its own PRs, so the table has a cutoff
        for each repository: the GitHub service names its repository `owner/name`, and a native
        source names its own by its qualified key. A policy that names none takes no phased rubric.
        """
        phased = getattr(policy, "with_phased_rubrics", None)
        if phased is None or not self.repository:
            return self.rubrics
        return tuple(phased(self.rubrics, self.repository, number))

    def evaluate(self, snapshot):
        rubrics = self.rubrics_for(snapshot.pull.number)
        result = policy.decide_from_comments(
            snapshot.comments,
            snapshot.pull.head_sha,
            set(rubrics),
            snapshot.paths,
            snapshot.statuses.get("build", ""),
            snapshot.statuses.get("bump-guard", ""),
            merge_path_prefix=self.prefix,
            merge_allow_file=self.allow_files,
            table_rubrics=rubrics,
            scope=snapshot.statuses.get("scope", ""),
            merge_base_sha=snapshot.merge_base_sha,
            self_tested=accepted(
                snapshot.pull.labels, snapshot.statuses.get(local_context(snapshot.pull.number)), member=True
            ),
        )
        result["recovery_heads"] = sorted(recovery_heads(snapshot.comments))
        if result["review_safe"] and snapshot.pull.head_sha in result["recovery_heads"]:
            result.update(merge=False, reason="waiting on a trusted recovery request for this head")
        board = policy.latest_current_scoreboard(snapshot.comments, snapshot.pull.head_sha)
        comment, meta = board if board else ({}, {})
        states = meta.get("states")
        if not isinstance(states, dict) or not states:
            states = policy.states_from_table(comment.get("body"), rubrics)
        # CI/configuration, formatting, timestamps, costs and token accounting are not approval intent.
        authorization = (
            dumps(
                {
                    "comment": comment.get("id"),
                    "head": snapshot.pull.head_sha,
                    "base": meta.get("merge_base_sha"),
                    "round": meta.get("round"),
                    "mode": meta.get("mode"),
                    "states": states,
                }
            )
            if board
            else ""
        )
        evidence = {
            "kind": "admission_policy",
            "policy_revision": self.source_revision,
            "policy_digest": self.revision,
            "head_sha": snapshot.pull.head_sha,
            "merge_base_sha": snapshot.merge_base_sha,
            "comment_id": comment.get("id"),
            "comment_url": comment.get("html_url", ""),
            "authorization": authorization,
            "single": bool(set(snapshot.paths) & {"lean-toolchain", "lake-manifest.json"}),
            "command": result["reason"],
        }
        return result, authorization, evidence, comment, meta


class AdmissionPoller:
    """Replaces the legacy check consumer; does not run beside a second admission producer."""

    def __init__(self, controller, adapter, repo, policy_config: AdmissionPolicy, *, clock=time.time):
        self.controller, self.adapter, self.repo = controller, adapter, repo
        self.policy = policy_config
        self.clock = clock
        self.state = AdmissionState(controller.store, repo, clock=clock)
        client = getattr(adapter, "client", None)
        restore = getattr(client, "restore_deadlines", None)
        if restore is not None:
            restore(controller.store.get_setting(self.state.resource_key, {}))
        self._lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._discovery_serial = 0
        self.wake = lambda: None
        self.last_summary = None
        self.stopping = threading.Event()
        self._prefer_urgent = True
        # Restore retry deadlines as-is. Discovery is due at startup independently of pending work.
        saved = controller.store.get_setting("state:admission_discovery", {})
        self._discovery_at = float(saved.get("retry_at", 0))
        controller.admission = self
        from .mixed_identity import SourceNumber

        waiting = [
            row
            for row in controller.store.rows("inputs", {"repository": repo, "state": ["waiting", "reserved"]})
            if not isinstance(row["number"], SourceNumber)
        ]
        waiting_numbers = {row["number"] for row in waiting}
        for work in controller.store.rows("admission_work", {"repository": repo}):
            previous_policy = loads(work["decision"], {}).get("evidence", {}).get("policy_digest")
            if work["generation"] == work["evaluated_generation"] and previous_policy != self.policy.revision:
                self.state.dirty(work["number"], head=work["head_sha"], urgent=work["number"] in waiting_numbers)
        for row in waiting:
            if self.state.get(row["number"]) is None:
                self.state.dirty(row["number"], head=row["head_sha"], urgent=True)

    def is_current(self, number, head):
        work = self.state.get(number)
        return bool(work and work["head_sha"] == head and work["generation"] == work["evaluated_generation"])

    def interval(self):
        pending = self.state.summary()["next_attempt_at"]
        deadline = self._discovery_at if pending is None else min(self._discovery_at, pending)
        return max(0, deadline - self.clock())

    def invalidate(self, number, *, head="", urgent=True):
        self.state.dirty(number, head=head, urgent=urgent)
        self.wake()

    def withdraw(self, number, head):
        """Called under the controller lock, in the same transaction as manual withdrawal."""
        self.state.dirty(number, head=head, urgent=True)
        row = self.state.get(number)
        head = head or row["head_sha"] or "*"
        self.controller.store.update(
            "admission_work",
            {"id": row["id"]},
            cancelled_head_sha=head,
            cancelled_at=self.clock(),
            cancelled_authorization=row["authorization_key"],
        )
        self.wake()

    def accept_manual(self, number, head):
        """A successfully applied operator approval is fresh intent, unlike reevaluation."""
        self.state.dirty(number, head=head, urgent=True)
        self.controller.store.update(
            "admission_work",
            {"repository": self.repo, "number": number},
            cancelled_head_sha="",
            cancelled_authorization="",
            cancelled_at=0,
        )
        self.wake()

    def handle_event(self, kind, payload):
        """Persist before webhook acknowledgement. No GitHub call or policy evaluation here."""
        pull = payload.get("pull_request") or {}
        number = payload.get("number") or pull.get("number") or (payload.get("issue") or {}).get("number")
        if kind == "pull_request":
            if payload.get("action") in ("labeled", "unlabeled"):
                label_payload = payload.get("label")
                label = label_payload.get("name") if isinstance(label_payload, dict) else None
                relevant_labels = (
                    KEEP
                    | self.controller._no_repair_labels()
                    | {"needs-rebase", "self-tested", "full-lint", "lint-repair"}
                )
                if isinstance(label, str) and label and label.lower() not in relevant_labels:
                    return  # membership and repair labels are queue output, not admission evidence
            elif payload.get("action") == "edited":
                changes = payload.get("changes")
                if isinstance(changes, dict) and changes and set(changes) <= {"title"}:
                    return
        if kind == "issue_comment":
            comment = payload.get("comment") or {}
            previous_body = ((payload.get("changes") or {}).get("body") or {}).get("from") or ""
            if "pull_request" not in (payload.get("issue") or {}) or (
                not relevant_comment(comment)
                and not relevant_comment(dict(comment, body=previous_body))
                and payload.get("action") != "deleted"
            ):
                return
        if kind in ("pull_request", "issue_comment") and number:
            self.invalidate(int(number), head=(pull.get("head") or {}).get("sha", ""))
        elif kind == "status":
            detail = payload.get(kind) or payload
            if (
                kind == "status"
                and detail.get("context") not in CONTEXTS
                and not str(detail.get("context", "")).startswith("self-tested/")
                and not str(detail.get("context", "")).startswith(("tauceti-review/", "tauceti-recovery/"))
            ):
                return
            if kind == "check_run" and detail.get("name") == "merge eligibility":
                return
            sha = detail.get("head_sha") or detail.get("sha")
            if sha and self.controller.store.row("ci_attempts", {"head_sha": sha}):
                return  # the service's candidate CI is output, not PR review evidence
            numbers = {p["number"] for p in detail.get("pull_requests", ()) if p.get("number")}
            if sha:
                numbers.update(
                    row["number"]
                    for row in self.controller.store.rows("admission_work", {"repository": self.repo, "head_sha": sha})
                )
            for number in numbers:
                self.invalidate(number, urgent=False)
            # Unknown heads are discovered by the existing discovery path, not an extra scan.
            if not numbers and kind == "status":
                self.request_discovery()
        elif kind == "push" and payload.get("ref") == f"refs/heads/{self.controller.canonical_branch}":
            with self.controller.store.transaction():
                self.controller.store.set_setting(
                    "state:admission_source_epoch", self._source_epoch() + 1, "admission", self.clock()
                )
            self.request_discovery()

    def _source_epoch(self):
        return self.controller.store.get_setting("state:admission_source_epoch", 0)

    def request_discovery(self):
        with self._discovery_lock:
            self._discovery_serial += 1
            self._discovery_at = 0
        self.wake()

    def _version(self, source):
        return source_version(source, self.policy.revision, self.controller.setting("no_repair_labels"))

    def _observe_page(self, pulls):
        for pull in pulls:
            if pull.admission is None:
                raise ValueError("discovery did not supply admission source inputs")
            version = (
                ("incomplete:" + digest(pull.admission))
                if "incomplete" in pull.admission
                else pull.admission.get("discovery_version") or self._version(pull.admission)
            )
            self.state.observe(pull.number, pull.head_sha, version)

    def run_once(self, *, reason="interval"):
        with self._lock:
            client = getattr(self.adapter, "client", None)
            deferred = getattr(client, "deferred_requests", None)
            context = deferred() if deferred is not None else nullcontext()
            with context:
                return self._run(reason)

    def _run(self, reason):
        configure_polling = getattr(self.adapter, "configure_admission_polling", None)
        if configure_polling is not None:
            configure_polling(
                clock=self.clock, refresh_seconds=self.controller.setting("approval_poll_interval_seconds")
            )
        summary = {
            "at": self.clock(),
            "reason": reason,
            "evaluated": 0,
            "approved": 0,
            "revoked": 0,
            "unchanged": 0,
            "deferred": 0,
            "errors": [],
        }
        while not self.stopping.is_set():
            if self.clock() >= self._discovery_at:
                self._discover(summary)
            if not self._process_one(summary, prefer_urgent=self._prefer_urgent):
                break
            self._prefer_urgent = not self._prefer_urgent
        self.last_summary = summary | self.state.summary()
        self.controller.record_approval_poll(self.last_summary)
        return self.last_summary

    def _discover(self, summary):
        if self.clock() >= self._discovery_at:
            with self._discovery_lock:
                serial = self._discovery_serial
            retry_at = 0
            try:
                listed = self.adapter.list_open_pull_requests(
                    self.repo,
                    review_app_id=0,
                    admission=True,
                    on_page=lambda pulls: (self._observe_page(pulls), self._process_one(summary, urgent_only=True)),
                )
                numbers = {p.number for p in listed}
                for row in self.controller.store.rows("admission_work", {"repository": self.repo}):
                    if row["number"] not in numbers and row["disposition"] != "closed":
                        self.state.observe(
                            row["number"], row["head_sha"], "absent:" + self.policy.revision, urgent=True
                        )
                next_at = self.clock() + self.controller.setting("approval_poll_interval_seconds")
            except Exception as error:
                next_at = retry_at = self._retry_at(error)
                if isinstance(error, RequestDeferred):
                    self.state.record_resource_wait(error.resource, error.retry_at)
                    summary["deferred"] += 1
                else:
                    summary["errors"].append(f"discovery: {error}")
            with self._discovery_lock:
                self._discovery_at = next_at if serial == self._discovery_serial else 0
            self.controller.store.set_setting(
                "state:admission_discovery",
                {"next_attempt_at": self._discovery_at, "retry_at": retry_at, "errors": summary["errors"]},
                "admission",
                self.clock(),
            )

    def _retry_at(self, error):
        return (
            error.retry_at
            if isinstance(error, RequestDeferred)
            else self.clock() + self.controller.setting("outbox_retry_seconds")
        )

    def _process_one(self, summary, *, prefer_urgent=True, urgent_only=False):
        work = self.state.next_due(prefer_urgent=prefer_urgent, urgent_only=urgent_only)
        if work is None:
            return False
        self.state.attempted(work)
        try:
            client = getattr(self.adapter, "client", None)
            revocation = bool(
                work["urgent"]
                and self.controller.store.row(
                    "inputs", {"repository": self.repo, "number": work["number"], "state": ["waiting", "reserved"]}
                )
            )
            deferred = getattr(client, "deferred_requests", None)
            with deferred(urgent=revocation) if deferred is not None else nullcontext():
                self._evaluate(work, summary)
        except Exception as error:
            if (
                getattr(error, "publication_kind", None) == "recovery"
                and getattr(error, "number", None) == work["number"]
            ):
                reconcile = getattr(self.controller, "reconcile_source_recovery", None)
                if reconcile is not None:
                    reconcile(work["number"], getattr(error, "head", None), getattr(error, "nonce", None))
            self.state.defer(
                work,
                self._retry_at(error),
                str(error),
                error.resource if isinstance(error, RequestDeferred) else "source",
            )
            if isinstance(error, RequestDeferred):
                summary["deferred"] += 1
                log.debug("admission #%s waiting: %s", work["number"], error)
            else:
                log.warning("admission #%s pending: %s", work["number"], error)
                summary["errors"].append(f"#{work['number']}: {error}")
        return True

    def _legacy_withdrawal(self, current):
        """Recover the old boundary from local records, without treating current evidence as old intent."""
        evidence = loads(current["evidence"], {})
        boundary = None
        reason = current["reason"]
        if reason in {"unapproved", "cancelled", "landed-externally"}:
            rows = self.controller.store.query(
                "SELECT at FROM journal WHERE kind='withdrawn' AND at>=? "
                "AND json_extract(detail, '$.number')=? AND json_extract(detail, '$.reason')=? "
                "ORDER BY id DESC LIMIT 1",
                (current["approved_at"], current["number"], reason),
            )
            boundary = rows[0]["at"] if rows else None
        elif reason in {"conflicts-with-main", "missing-source"}:
            kind = "selection_conflict" if reason == "conflicts-with-main" else "missing_source"
            boundary = next(
                (r.get("at") for r in reversed(evidence.get("merge_selection", [])) if r.get("kind") == kind),
                None,
            )
        elif reason == "problem-anticipated":
            boundary = loads(current["anticipated"], {}).get("withdrawn_at")
        elif reason == "empty-commit":
            rows = self.controller.store.rows(
                "journal", {"kind": "landed", "batch_id": current["batch_id"]}, order="id DESC", limit=1
            )
            boundary = rows[0]["at"] if rows else None
        # Missing history is held conservatively. Never infer approval from a comment edit.
        return boundary or self.clock(), evidence.get("authorization", "")

    def _cancelled(self, work, snapshot, authorization, comment, meta):
        current = self.controller._input_for(work["number"])
        if (
            not work["cancelled_head_sha"]
            and current
            and current["state"] == "cancelled"
            and (current["head_sha"] == snapshot.pull.head_sha and current["reason"] in EXPLICIT_WITHDRAWALS)
        ):
            # Migration: the old input, including its withdrawal, remains authoritative.
            cancelled_at, cancelled_authorization = self._legacy_withdrawal(current)
            self.controller.store.update(
                "admission_work",
                {"id": work["id"]},
                cancelled_head_sha=current["head_sha"],
                cancelled_at=cancelled_at,
                cancelled_authorization=cancelled_authorization,
            )
            work = self.state.get(work["number"])
        if work["cancelled_head_sha"] == "*":
            self.controller.store.update(
                "admission_work", {"id": work["id"]}, cancelled_head_sha=snapshot.pull.head_sha
            )
            work = self.state.get(work["number"])
        if work["cancelled_head_sha"] != snapshot.pull.head_sha:
            return False
        old = loads(work["cancelled_authorization"], {})
        fresh = loads(authorization, {})
        new_comment = timestamp(comment.get("created_at")) > work["cancelled_at"]
        new_round = bool(
            old
            and meta.get("round") is not None
            and meta.get("round") != old.get("round")
            and timestamp(meta.get("ts")) > work["cancelled_at"]
        )
        if not old and authorization:
            self.controller.store.update("admission_work", {"id": work["id"]}, cancelled_authorization=authorization)
        return not (fresh and authorization != work["cancelled_authorization"] and (new_comment or new_round))

    def _evaluate(self, work, summary):
        epoch = self._source_epoch()
        snapshot = self.adapter.admission_snapshot(self.repo, work["number"], generation=work["generation"])
        result, authorization, evidence, comment, meta = self.policy.evaluate(snapshot)
        current_input = self.controller.input_for(self.repo, work["number"])
        human_review = self._human_owned_approval(snapshot, current_input, comment)
        if human_review:
            # Human-owned infrastructure does not receive an automatic scoreboard.
            # Preserve the operator's recorded intent; staging must still establish
            # build, bump-guard and scope success before anything can be published.
            result = {**result, "review_safe": True, "reason": "human-owned paths approved by repository admin"}
            approval = loads(current_input["evidence"], {})
            evidence = {
                **evidence,
                "command": result["reason"],
                "human_approval": {
                    **{
                        key: approval.get(key)
                        for key in ("actor_id", "actor_login", "comment_id", "head_sha", "merge_base_sha")
                    },
                    "effective_admin": True,
                    "permission_checked_at": self.clock(),
                },
            }
        version = self._version(snapshot.source())
        # All reads/fetches, including the PR recheck inside the adapter, precede this lock.
        with self.controller._lock, self.controller.store.transaction():
            current_work = self.state.get(work["number"])
            if current_work["generation"] != work["generation"]:
                return
            if self._source_epoch() != epoch:
                self.state.dirty(work["number"], urgent=True)
                return
            disposition = "waiting"
            pull = snapshot.pull
            current = self.controller._input_for(work["number"])
            if human_review and approval_binding(current) != approval_binding(current_input):
                self.state.dirty(work["number"], urgent=True)
                return
            requested = result["recovery_heads"]
            labels = {label.lower() for label in pull.labels}
            if (
                pull.state == "open"
                and not pull.draft
                and pull.base_ref == self.controller.canonical_branch
                and result["review_safe"]
                and not labels & KEEP
                and requested
                and not result.get("recovery_authority_hold")
            ):
                held, labelled = pull.head_sha in requested, "needs-rebase" in labels
                if held != labelled:
                    self.controller._enqueue(
                        "labels",
                        f"admission-rebase-label:{pull.number}:{pull.head_sha}:{work['generation']}",
                        {
                            "number": pull.number,
                            "add": ["needs-rebase"] if held else [],
                            "remove": [] if held else ["needs-rebase"],
                            "head_sha": pull.head_sha,
                            "admission_generation": work["generation"],
                        },
                    )
            if current and current["head_sha"] != pull.head_sha:
                self.controller._pull_request_event(
                    {"number": pull.number, "action": "synchronize", "pull_request": {"head": {"sha": pull.head_sha}}},
                    self.clock(),
                )
            if pull.state != "open" or pull.draft:
                action = "closed" if pull.state != "open" else "converted_to_draft"
                self.controller._pull_request_event(
                    {
                        "number": pull.number,
                        "action": action,
                        "pull_request": {"merged": getattr(pull, "merged", False), "title": pull.title},
                    },
                    self.clock(),
                )
                disposition = "closed" if pull.state != "open" else "draft"
            elif not result["review_safe"] or result.get("recovery_authority_hold"):
                withdrawn = self.controller._withdraw(self.repo, pull.number, "review-unsafe", "tauceti-queue policy")
                summary["revoked"] += int(withdrawn["withdrawn"])
                disposition = "review-unsafe"
            elif result["merge"] and pull.base_ref == self.controller.canonical_branch:
                if (
                    current
                    and current["head_sha"] == pull.head_sha
                    and current["state"] in ("waiting", "reserved", "inserted", "landed")
                ):
                    disposition = (
                        "already-approved" if current["state"] in ("waiting", "reserved") else current["state"]
                    )
                elif self._cancelled(current_work, snapshot, authorization, comment, meta):
                    disposition = "withdrawn"
                else:
                    accepted = self.controller.approve(
                        self.repo, pull.number, pull.head_sha, "published review (queue policy)", 0, evidence, pull=pull
                    )
                    disposition = accepted["reason"]
                    if (
                        not accepted["accepted"]
                        and disposition not in ("member",)
                        and not disposition.startswith("blocked-label:")
                    ):
                        raise RuntimeError(f"admission could not retain head: {disposition}")
                    if accepted["accepted"]:
                        self.controller.store.update(
                            "admission_work",
                            {"id": work["id"]},
                            cancelled_head_sha="",
                            cancelled_authorization="",
                            cancelled_at=0,
                        )
                        summary["approved"] += int(disposition == "approved")
            self.controller.store.update(
                "admission_work",
                {"id": work["id"]},
                head_sha=pull.head_sha,
                observed_version=snapshot.discovery_version or version,
                decision=dumps({**result, "evidence": evidence}),
            )
            self.state.complete(work, version, authorization, disposition)
            self.controller.store.journal(
                self.clock(),
                "admission-evaluated",
                number=pull.number,
                head=pull.head_sha,
                disposition=disposition,
                reason=result["reason"],
                policy_digest=self.policy.revision,
            )
            self.controller._count("admission_evaluations")
            if disposition == "approved":
                self.controller._count("admission_approvals")
            summary["evaluated"] += 1
        self.controller._wake.set()

    def _human_owned_approval(self, snapshot, current, scoreboard):
        """Keep an exact, live admin approval only where machine review is absent.

        Never turn prose, an automatic approval, a stale revision or a negative
        scoreboard into authority. Reads of effective admin permission happen
        outside the controller lock and failures leave this evaluation pending.
        """
        if not getattr(self.policy, "human_owned_approvals", True):
            return False
        if scoreboard or not current or current["state"] not in ("waiting", "reserved", "inserted"):
            return False
        pull = snapshot.pull
        if pull.state != "open" or pull.draft or pull.base_ref != self.controller.canonical_branch:
            return False
        if not snapshot.paths or not all(
            not path.startswith(self.policy.prefix) and path not in self.policy.allow_files for path in snapshot.paths
        ):
            return False
        approval = loads(current["evidence"], {})
        if (
            approval.get("kind") not in ("issue_comment", "pull_request_review")
            or approval.get("admin") is not True
            or type(approval.get("actor_id")) is not int
            or approval["actor_id"] <= 0
            or not approval.get("actor_login")
            or current["head_sha"] != pull.head_sha
            or approval.get("head_sha") != pull.head_sha
            or approval.get("merge_base_sha") != snapshot.merge_base_sha
        ):
            return False
        return self.adapter.repository_admin(approval["actor_login"], self.repo, actor_id=approval["actor_id"])
