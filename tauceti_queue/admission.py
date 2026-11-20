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

from .admission_state import AdmissionState
from .controller import COMMENT_MARKER
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
        "missing-source-recheck",
        "landed-externally",
        "problem-anticipated",
        "empty-commit",
        "queue-withdrawn",
    }
)
# Admission dispositions under which a queue-refused head is not re-admitted (`readmit`).
READMIT_REFUSED = frozenset({"closed", "draft", "review-unsafe"})
CONTEXTS = ("build", "scope", "bump-guard")
HUMAN_INFRASTRUCTURE_APPROVAL_VERSION = 1
LARGE_REFACTOR_APPROVAL_VERSION = 1


# The Lake pins a Mathlib bump changes; a waived large refactor may change them only with a passing bump check.
LAKE_PINS = frozenset({"lake-manifest.json", "lean-toolchain"})


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
                    | {"needs-rebase", "self-tested", "full-lint", "lint-repair", "large-refactor"}
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
                    set(CONTEXTS) | {k for k in source.get("statuses", {}) if k.startswith("self-tested/")}
                )
            },
        }
    )


def recovery_heads(comments):
    return policy.rebase_request_heads(comments) | policy.rebase_request_heads(comments, stalled=True)


def relevant_comment(comment, *, native=False):
    """Native recovery requests are author-checked when evaluated, not here."""
    body = comment.get("body") or ""
    return (
        policy.SCOREBOARD_MARKER in body
        or bool(recovery_heads([comment]))
        or native
        and body.startswith("Merge-queue recovery for head `")
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
                "large_refactor_approval": LARGE_REFACTOR_APPROVAL_VERSION,
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
            (
                "success"
                if snapshot.self_tested and set(snapshot.paths) & {"lean-toolchain", "lake-manifest.json"}
                else snapshot.statuses.get("bump-guard", "")
            ),
            merge_path_prefix=self.prefix,
            merge_allow_file=self.allow_files,
            table_rubrics=rubrics,
            scope=snapshot.statuses.get("scope", ""),
            merge_base_sha=snapshot.merge_base_sha,
            self_tested=snapshot.self_tested,
        )
        if snapshot.diff_guard:
            # The PR-level diff guards PR CI would have run. A violating head is never admitted;
            # review state is untouched, so a head a human already queued is not withdrawn here.
            result = {
                **result,
                "merge": False,
                "diff_guard": list(snapshot.diff_guard),
                "reason": "PR-level diff guards failed: " + "; ".join(snapshot.diff_guard),
            }
            result.pop("self_tested", None)
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
        if snapshot.large_refactor:
            # Repair reads this from the input, so its review-size checks skip the same caps.
            evidence["large_refactor"] = True
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
        self._discovery_timing = None  # the current discovery pass's timing, see `_discover`
        self.wake = lambda: None
        self.last_summary = None
        self.stopping = threading.Event()
        self._prefer_urgent = True
        self._insertion_held = {}  # number -> (head, evaluated version, no re-read before)
        self._delivery = threading.local()  # per webhook thread: a delivery that changed nothing
        from .admission_evidence import ADMISSION_LABELS, EvidenceCache, Shadow

        fake_repo = getattr(adapter, "repo", None)
        clone = getattr(adapter, "clone_dir", None) or getattr(fake_repo, "clone", None)
        self.evidence = EvidenceCache(
            controller.store,
            repo,
            clock=clock,
            clone=str(clone) if clone else None,
            relevant_comment=relevant_comment,
            relevant_labels=lambda: KEEP | controller._no_repair_labels() | ADMISSION_LABELS,
            base_branch=controller.canonical_branch,
        )
        self.shadow = Shadow(
            self.evidence,
            self.policy,
            journal=controller.store.journal,
            blocking_labels=controller._blocking_labels,
        )
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

    def insertion_check(self, number, head):
        """None when the PR's admission inputs still match those admission evaluated for `head`, else
        why not. Runs without the controller lock, before an approved head joins a batch.

        `is_current` only consults local records, so a missed webhook would let a revoked review or
        a failed status reach a batch until the next discovery. Behind `admission_insertion_check`,
        this re-reads the inputs and compares their digest with the evaluated one. Any difference,
        or a read that fails, holds the insertion (fail closed); a difference also has the PR
        evaluated again. If the next read differs from that same evaluation again, reads wait an
        approval-poll interval, so a digest that can never match costs no quota on every pass.
        """
        if not self.controller.setting("admission_insertion_check"):
            return None
        read = getattr(self.adapter, "admission_insertion_source", None)
        if read is None:
            return None  # an adapter without the read keeps today's local check
        work = self.state.get(number)
        if not work or work["head_sha"] != head or work["generation"] != work["evaluated_generation"]:
            return "not evaluated at this head"
        held = self._insertion_held.get(number)
        if held and held[:2] == (head, work["evaluated_version"]) and self.clock() < held[2]:
            # The last read disagreed with this very evaluation: wait before reading again, so a
            # digest that never matches holds the head without spending quota on every pass.
            return "inputs differed from this evaluation at the last check; waiting before reading again"
        try:
            source = read(self.repo, number, head)
        except Exception as error:  # noqa: BLE001 - an unreadable source never admits
            return f"unavailable: {error}"
        if source.get("head") != head:
            self.invalidate(number, urgent=True)
            return "head moved"
        if self._version(source) != work["evaluated_version"]:
            if held and held[:2] == (head, work["evaluated_version"]):
                log.warning("admission #%s: insertion check differs again from an unchanged evaluation", number)
            retry = self.clock() + self.controller.setting("approval_poll_interval_seconds")
            self._insertion_held[number] = (head, work["evaluated_version"], retry)
            self.invalidate(number, head=head, urgent=True)
            return "inputs changed since evaluation"
        self._insertion_held.pop(number, None)
        return None

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

    def readmit(self, number, head):
        """Called under the controller lock when a `missing-source` refusal of `head` may no longer hold:
        the PR is read again, and the evaluation restores the input if it would admit the head
        (`Controller.restore_missing_source`). False when admission last saw another head, or the PR
        closed, drafted or not review-safe."""
        work = self.state.get(number)
        if work is None or work["head_sha"] != head or work["disposition"] in READMIT_REFUSED:
            return False
        self.state.dirty(number, head=head, urgent=True)
        self.wake()
        return True

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

    def evidence_mode(self):
        return self.controller.setting("admission_evidence_mode")

    def evidence_event(self, kind, payload):
        """Record a verified delivery in the evidence cache (shadow only); never fails the delivery."""
        if kind not in ("pull_request", "issue_comment", "status", "push") or self.evidence_mode() == "off":
            return
        self._delivery.unchanged = None
        try:
            changed = self.evidence.apply_event(kind, payload)
        except Exception as error:  # noqa: BLE001 - a failed write only means the PR is dirtied as before
            log.warning("admission evidence: %s delivery not recorded: %s", kind, error)
            return
        if changed is False:
            self._delivery.unchanged = payload  # this thread's delivery; see `handle_event`

    def evidence_status(self):
        mode = self.evidence_mode()
        return {"mode": mode} if mode == "off" else {"mode": mode, "shadow": self.shadow.summary()}

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
                    | {"needs-rebase", "self-tested", "full-lint", "lint-repair", "large-refactor"}
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
            ):
                return
            if kind == "check_run" and detail.get("name") == "merge eligibility":
                return
            sha = detail.get("head_sha") or detail.get("sha")
            if self.evidence_mode() == "events" and getattr(self._delivery, "unchanged", None) is payload:
                rows = self.controller.store.rows("admission_work", {"repository": self.repo, "head_sha": sha})
                cached = self.controller.store.row(
                    "admission_evidence_statuses",
                    {"repository": self.repo, "sha": sha, "context": detail.get("context")},
                )
                if (
                    len(rows) == 1  # a head shared by PRs: one PR's seed proves nothing about another's evaluation
                    and cached is not None
                    and all(
                        r["generation"] == r["evaluated_generation"] and cached["seeded_at"] >= r["attempted_at"]
                        for r in rows
                    )
                ):
                    # The last completed evaluation of the only PR at this head installed exactly
                    # this state from its own read, and no delivery has changed it since.
                    return
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
            self.state.observe(
                pull.number, pull.head_sha, version, recheck_after=pull.admission.get("recheck_seconds", 0)
            )

    def run_once(self, *, reason="interval"):
        with self._lock:
            client = getattr(self.adapter, "client", None)
            deferred = getattr(client, "deferred_requests", None)
            context = deferred() if deferred is not None else nullcontext()
            with context:
                return self._run(reason)

    def _run(self, reason):
        client = getattr(self.adapter, "client", None)
        floor = int(self.controller.setting("github_background_floor"))
        if client is not None and getattr(client, "background_floor", floor) != floor:
            # Waits recorded under the old floor end at the next read, which checks the new one.
            client.background_floor = floor
            client.release_background_holds()
            self.state.release_background_waits()
            if self.controller.store.get_setting("state:admission_discovery", {}).get("retry_at"):
                with self._discovery_lock:
                    self._discovery_at = min(self._discovery_at, self.clock())
        if hasattr(self.adapter, "local_merge_base"):
            self.adapter.local_merge_base = bool(self.controller.setting("admission_local_merge_base"))
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
            # Where a pass's time goes: pages read, and the urgent evaluations made between them.
            # A pass resumed after a quota wait continues its listing, so it continues these too.
            if self._discovery_timing is None:
                self._discovery_timing = {"seconds": 0.0, "pages": 0, "evaluation_seconds": 0.0}
            timing, started = self._discovery_timing, time.monotonic()

            def on_page(pulls):
                self._observe_page(pulls)
                timing["pages"] += 1
                evaluating = time.monotonic()
                self._process_one(summary, urgent_only=True)
                timing["evaluation_seconds"] += time.monotonic() - evaluating

            try:
                listed = self.adapter.list_open_pull_requests(
                    self.repo, review_app_id=0, admission=True, on_page=on_page
                )
                numbers = {p.number for p in listed}
                for row in self.controller.store.rows("admission_work", {"repository": self.repo}):
                    if row["number"] not in numbers and row["disposition"] != "closed":
                        self.state.observe(
                            row["number"], row["head_sha"], "absent:" + self.policy.revision, urgent=True
                        )
                next_at = self.clock() + self.controller.setting("approval_poll_interval_seconds")
                self._discovery_timing = None
                self.controller.store.journal(
                    self.clock(),
                    "admission-discovered",
                    prs=len(listed),
                    pages=timing["pages"],
                    seconds=round(timing["seconds"] + time.monotonic() - started, 1),
                    evaluation_seconds=round(timing["evaluation_seconds"], 1),
                )
            except Exception as error:
                timing["seconds"] += time.monotonic() - started
                if not isinstance(error, RequestDeferred):
                    self._discovery_timing = None  # the listing starts again at page one
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
            revocation = bool(work.get("emergency")) or bool(
                work["urgent"]
                and self.controller.store.row(
                    "inputs", {"repository": self.repo, "number": work["number"], "state": ["waiting", "reserved"]}
                )
            )
            deferred = getattr(client, "deferred_requests", None)
            with deferred(urgent=revocation) if deferred is not None else nullcontext():
                self._evaluate(work, summary)
        except Exception as error:
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
            withdrawn = self.controller.store.query(
                "SELECT at FROM journal WHERE kind='member-withdrawn-empty' AND batch_id=? "
                "AND json_extract(detail, '$.number')=? ORDER BY id DESC LIMIT 1",
                (current["batch_id"], current["number"]),
            )
            boundary = withdrawn[0]["at"] if withdrawn else (rows[0]["at"] if rows else None)
        elif reason == "queue-withdrawn":
            rows = self.controller.store.query(
                "SELECT at FROM journal WHERE kind='queue-withdrawn' AND batch_id=? "
                "AND json_extract(detail, '$.number')=? ORDER BY id DESC LIMIT 1",
                (current["batch_id"], current["number"]),
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
        started = self.clock()
        began = time.monotonic()
        fetch_seconds = getattr(self.adapter, "fetch_seconds", lambda: 0.0)
        fetched = fetch_seconds()
        single = getattr(self.adapter, "admission_snapshot_single", None)
        read = single if self.evidence_mode() == "events" and single is not None else self.adapter.admission_snapshot
        snapshot = read(self.repo, work["number"], generation=work["generation"])
        timing = {"read_seconds": time.monotonic() - began, "fetch_seconds": fetch_seconds() - fetched}
        result, authorization, evidence, comment, meta = self.policy.evaluate(snapshot)
        if self.evidence_mode() in ("shadow", "events"):
            try:
                self.shadow.compare(snapshot, result, started, authorization)
            except Exception as error:  # noqa: BLE001 - the shadow never changes or blocks a decision
                log.warning("admission shadow #%s: %s", work["number"], error)
        current_input = self.controller.input_for(self.repo, work["number"])
        human_review = self._human_owned_approval(snapshot, current_input, comment)
        large_refactor_review = not human_review and self._large_refactor_approval(snapshot, current_input)
        if large_refactor_review:
            # An admin-labelled large refactor that an admin approved in person: that approval stands
            # in for machine review and PR CI, which cannot cover a diff this size. Staging still
            # builds, audits and checks the bump of the batch before anything lands.
            result = {**result, "review_safe": True, "reason": "large refactor approved by repository admin"}
            approval = loads(current_input["evidence"], {})
            evidence = {
                **evidence,
                "command": result["reason"],
                "large_refactor_approval": {
                    **{
                        key: approval.get(key)
                        for key in ("actor_id", "actor_login", "comment_id", "head_sha", "merge_base_sha")
                    },
                    "bump_guard": bool(set(snapshot.paths) & LAKE_PINS),
                    "evaluated_at": self.clock(),
                },
            }
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
        locking = time.monotonic()
        # All reads/fetches, including the PR recheck inside the adapter, precede this lock.
        with self.controller._lock, self.controller.store.transaction():
            timing["lock_seconds"] = time.monotonic() - locking
            current_work = self.state.get(work["number"])
            if current_work["generation"] != work["generation"]:
                return
            if self._source_epoch() != epoch:
                self.state.dirty(work["number"], urgent=True)
                return
            disposition = "waiting"
            pull = snapshot.pull
            current = self.controller._input_for(work["number"])
            if (human_review or large_refactor_review) and approval_binding(current) != approval_binding(current_input):
                self.state.dirty(work["number"], urgent=True)
                return
            self._sync_large_refactor(current, pull.head_sha, snapshot.large_refactor)
            if result.get("diff_guard") and pull.state == "open" and not pull.draft:
                self._diff_guard_comment(pull, result["diff_guard"])
            requested = result["recovery_heads"]
            labels = {label.lower() for label in pull.labels}
            if (
                pull.state == "open"
                and not pull.draft
                and pull.base_ref == self.controller.canonical_branch
                and result["review_safe"]
                and not labels & KEEP
                and requested
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
            if current and current["head_sha"] == pull.head_sha:
                # Corrects a missed label delivery, whatever this evaluation decides.
                self.controller.note_blocking_labels(current, pull.labels)
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
            elif not result["review_safe"]:
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
                elif (restored := self.controller.restore_missing_source(pull.number, pull.head_sha, pull)) is not None:
                    disposition = restored
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
                # Where this evaluation's time went: the source read, the clone fetches within it,
                # and the wait for the controller lock; `seconds` is the whole evaluation.
                seconds=round(time.monotonic() - began, 2),
                **{key: round(value, 2) for key, value in timing.items()},
            )
            self.controller._count("admission_evaluations")
            if disposition == "approved":
                self.controller._count("admission_approvals")
            summary["evaluated"] += 1
        self.controller._wake.set()

    def _diff_guard_comment(self, pull, violations):
        """Tell the author once per head why admission refused it, through the existing outbox."""
        if not self.controller.setting("publication_enabled"):
            return
        body = (
            "This PR cannot join the merge queue: it fails the PR-level diff guards that PR CI runs, "
            "which admission checks itself because the PR skips PR CI or carries the large-refactor label.\n\n"
            + "\n".join(f"- {line}" for line in violations)
            + "\n\nRun `lake lint` locally to reproduce. Push a fix and admission will reconsider the new head."
        )
        self.controller._enqueue(
            "comment",
            f"diff-guard:{pull.number}:{pull.head_sha}",
            {
                "number": pull.number,
                "body": body,
                "marker": f"<!-- {COMMENT_MARKER} diff-guard:{pull.head_sha} -->",
            },
        )

    def _sync_large_refactor(self, current, head, exempt):
        """Keep the input's `large_refactor` evidence (which repair reads) equal to this evaluation's,
        for the same head: a label applied or removed after approval reaches the next repair task."""
        if current is None or current["head_sha"] != head:
            return
        evidence = loads(current["evidence"], {})
        if bool(evidence.get("large_refactor")) == bool(exempt):
            return
        if exempt:
            evidence["large_refactor"] = True
        else:
            evidence.pop("large_refactor", None)
        self.controller.store.update("inputs", {"id": current["id"]}, evidence=dumps(evidence))

    def _admin_approval(self, snapshot, current):
        """The live input's approval, when command intake recorded it as an admin's own command or
        review bound to this exact head and merge base; otherwise None."""
        if not current or current["state"] not in ("waiting", "reserved", "inserted"):
            return None
        pull = snapshot.pull
        if pull.state != "open" or pull.draft or pull.base_ref != self.controller.canonical_branch:
            return None
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
            return None
        return approval

    def _large_refactor_approval(self, snapshot, current):
        """Keep an admin's exact approval of an admin-labelled large refactor without machine review.

        The waiver needs all of: the `large-refactor` label verified on this head (an admin applied it
        after the head's commit, with no force push since); an approval that command intake recorded
        as an admin's own, bound to this head and merge base; every changed path under the library
        prefix or one of the two Lake pins; no failed diff guard (the label never lifts the new-file
        cap); and, when a pin changes, a passing live Mathlib-bump check. Any scoreboard, even a
        negative one, is ignored: an admin chose to approve this head in person. An unreadable bump
        check leaves the evaluation pending.
        """
        if not getattr(self.policy, "human_owned_approvals", True) or not snapshot.large_refactor:
            return False
        if snapshot.diff_guard or not snapshot.paths:
            return False
        if not all(path.startswith(self.policy.prefix) or path in LAKE_PINS for path in snapshot.paths):
            return False
        if self._admin_approval(snapshot, current) is None:
            return False
        return not (set(snapshot.paths) & LAKE_PINS) or bool(self.adapter.bump_guard(self.repo, snapshot.pull.head_sha))

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
