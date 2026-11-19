"""Optional second-source discovery and original-forge routing for one queue."""

from __future__ import annotations

import hashlib
import threading
import time

from . import chain
from .admission_state import AdmissionState
from .controller import ControllerError
from .mixed_identity import SourceNumber, source_record
from .store import dumps, loads


class SourcePorts:
    repository_identity = None  # The controller is a mixed queue, never a native-only impersonation.

    def __init__(self, registry):
        self.registry = registry

    def __getattr__(self, name):
        return getattr(self.registry.github, name)

    def _pull(self, repo, number):
        registry = self.registry
        if isinstance(number, SourceNumber) or repo == registry.profile.forgejo.repository.key:
            registry.check_native(number)
            return registry.native, registry.profile.forgejo.repository.name, int(number)
        if repo != registry.repository:
            raise ValueError("unknown source repository")
        return registry.github, registry.repository, int(number)

    def fetch_pr(self, repo, number):
        port, name, native = self._pull(repo, number)
        return port.fetch_pr(name, native)

    def pr_merge_base(self, repo, number, head):
        port, name, native = self._pull(repo, number)
        return port.pr_merge_base(name, native, head)

    def post_comment(self, repo, number, *args, **kwargs):
        port, name, native = self._pull(repo, number)
        return port.post_comment(name, native, *args, **kwargs)

    def set_labels(self, repo, number, *args, **kwargs):
        port, name, native = self._pull(repo, number)
        return port.set_labels(name, native, *args, **kwargs)

    def close_pr(self, repo, number, *args, **kwargs):
        port, name, native = self._pull(repo, number)
        return port.close_pr(name, native, *args, **kwargs)

    def ensure_label(self, repo, *args, **kwargs):
        if repo == self.registry.profile.forgejo.repository.key:
            self.registry.check_native()
            return self.registry.native.ensure_label(self.registry.profile.forgejo.repository.name, *args, **kwargs)
        return self.registry.github.ensure_label(repo, *args, **kwargs)

    def fetch_refs(self, repo, specs):
        # Repair and queue-retention refs retain their existing GitHub home,
        # independently of which main is primary. Only canonical reads follow primary.
        canonical = any(spec.removeprefix("+").startswith("refs/heads/" + self.registry.branch + ":") for spec in specs)
        if canonical and self.registry.primary == "forgejo":
            self.registry.check_native()
            return self.registry.native.fetch_refs(self.registry.profile.forgejo.repository.name, specs)
        return self.registry.github.fetch_refs(repo, specs)

    def fast_forward(self, repo, branch, expected, tip):
        registry = self.registry
        if registry.primary == "github":
            return registry.github.fast_forward_leased(repo, branch, expected, tip)
        registry.check_native()
        registry.check_writer()
        return registry.native_main.fast_forward(registry.profile.forgejo.repository.name, branch, expected, tip)


class AdmissionInputs:
    """Keep existing GitHub admission and native admission in their own namespaces."""

    def __init__(self, github, native):
        self.github, self.native = github, native

    def __getattr__(self, name):
        return getattr(self.github, name)

    def _for(self, number):
        return self.native if isinstance(number, SourceNumber) else self.github

    def is_current(self, number, head):
        port = self._for(number)
        return port is not None and port.is_current(int(number), head)

    def withdraw(self, number, head):
        port = self._for(number)
        if port is not None:
            return port.withdraw(int(number), head)

    def insertion_check(self, number, head):
        port = self._for(number)
        check = getattr(port, "insertion_check", None)
        return None if check is None else check(int(number), head)

    def invalidate(self, number, **kwargs):
        port = self._for(number)
        if port is not None:
            return port.invalidate(int(number), **kwargs)

    def accept_manual(self, number, head):
        port = self._for(number)
        if port is not None:
            return port.accept_manual(int(number), head)

    def readmit(self, number, head):
        port = self._for(number)
        return port is not None and port.readmit(int(number), head)


# Recent listings re-read PRs updated this long before the newest update already seen, so an update
# whose time Forgejo recorded slightly out of order is not skipped.
RECENT_MARGIN_SECONDS = 120
COMMENT_FEED_PAGES = 200  # the most pages of the comment feed one pass reads


class NativePoller:
    """Direct native discovery with the existing due/dirty intake ledger."""

    def __init__(self, registry, controller):
        self.registry, self.controller = registry, controller
        self.state = AdmissionState(
            controller.store,
            registry.profile.forgejo.repository.key,
            clock=controller.clock,
            inputs=(controller.repository, source_record(registry.number(1))),
        )
        self.last_summary = None
        self._serial = 0
        self._lock = threading.Lock()
        self.stopping = threading.Event()
        self._discovery_at = 0
        # Between full listings, the PRs Forgejo says changed since the newest update time seen
        # (its own clock, less a margin) are listed every pass (`forgejo_recent_listing`).
        self._recent_at = 0
        self._recent_since = None
        # Scoreboard edits do not move a Forgejo PR's update time, so between listings the
        # repository comment feed is read too. Its position survives restarts.
        self._comments_key = f"state:forgejo_comment_since:{self.state.repository}"
        self._comments_since = controller.store.get_setting(self._comments_key, None)
        self._comments_seen = {}
        self._comments_at = 0
        self.wake = lambda: None
        configure = getattr(registry.native, "configure_admission_polling", None)
        if configure is not None:
            configure(clock=controller.clock, refresh_seconds=controller.setting("approval_poll_interval_seconds"))

    def interval(self):
        due = self.state.summary()["next_attempt_at"]
        next_at = self._discovery_at if due is None else min(self._discovery_at, due)
        if self._recent_enabled():
            next_at = min(next_at, self._recent_at, self._comments_at)
        return max(0, next_at - self.controller.clock())

    def _recent_enabled(self):
        return (
            self._recent_since is not None
            and callable(getattr(self.registry.native, "list_recent_pull_requests", None))
            and bool(self.controller.setting("forgejo_recent_listing"))
        )

    def _newest(self, pulls, newest=0.0):
        from .forgejo_source import _forge_time

        times = [_forge_time((p.admission or {}).get("updated_at")) for p in pulls]
        return max([newest, *times])

    def _observe_listed(self, pull):
        details = pull.admission or {}
        version = self._listed_version(pull, details.get("discovery_version"))
        # Facts the listing cannot see are rechecked after `forgejo_recheck_seconds`; changes it
        # can see, and comment edits from the feed, are noticed within a pass.
        recheck = self.controller.setting("forgejo_recheck_seconds") if details.get("recheck_seconds") else 0
        self.state.observe(pull.number, pull.head_sha, version, recheck_after=recheck)

    def _reevaluate(self, work, *, urgent=False):
        """Evaluate a PR again for evidence its listed facts do not show, keeping those facts:
        they did not change, and the check before insertion compares them with the evaluated ones."""
        with self.controller.store.transaction():
            self.state.dirty(work["number"], urgent=urgent)
            self.controller.store.update(
                "admission_work", {"id": work["id"]}, observed_version=work["observed_version"]
            )

    def _read_comment_feed(self, summary):
        from .admission import relevant_comment
        from .forgejo_source import _forge_time

        registry, controller = self.registry, self.controller
        since = self._comments_since
        # One read drains the window: 200 pages of 50 is far beyond a pass's comments (about two
        # a minute on production), and a deletion during the read can still shift a comment past it, which the review check before insertion and the rechecks cover.
        rows, newest, complete = registry.native.list_recent_comments(
            registry.profile.forgejo.repository.name, since=since, max_pages=COMMENT_FEED_PAGES
        )
        dirtied = set()
        for number, row in rows:
            body = row.get("body") or ""
            key = (row["id"], row.get("updated_at"), hashlib.sha256(body.encode()).hexdigest())
            if key in self._comments_seen:
                continue
            # A new ordinary comment cannot change a verdict (and moves the PR's update time
            # anyway); an edit can, even one that removes a scoreboard marker.
            edited = _forge_time(row.get("updated_at")) > _forge_time(row.get("created_at"))
            if not edited and not relevant_comment(row, native=True):
                continue
            self._comments_seen[key] = _forge_time(row.get("updated_at"))
            work = self.state.get(number)
            if work is not None and work["disposition"] != "closed" and number not in dirtied:
                self._reevaluate(work)
                dirtied.add(number)
        summary["comment_feed"] = {"read": len(rows), "dirtied": len(dirtied)}
        if not complete:
            # Unread pages may hold older edits: keep the position (rechecks stay the backstop).
            summary["errors"].append("comment feed: more than one bounded read")
        elif newest:
            self._comments_since = max(since, newest - RECENT_MARGIN_SECONDS)
            controller.store.set_setting(
                self._comments_key, self._comments_since, "native queue policy", controller.clock()
            )
            # Edits older than the new position cannot be listed again; forget them.
            self._comments_seen = {k: v for k, v in self._comments_seen.items() if v >= self._comments_since}
        if dirtied:
            self.wake()

    def request_discovery(self):
        self._serial += 1
        self._discovery_at = 0
        self.wake()

    def admission_changed(self):
        """Discard observations for new work, keeping existing acceptance live."""
        for row in self.controller.store.rows("admission_work", {"repository": self.state.repository}):
            if row["disposition"] == "closed" and row["generation"] == row["evaluated_generation"]:
                continue
            current = self.controller._input_for(self.registry.number(row["number"]))
            if (
                current
                and current["head_sha"] == row["head_sha"]
                and current["state"] in {"waiting", "reserved", "inserted", "landed"}
            ):
                continue
            self.state.dirty(row["number"], urgent=False)
            self.controller.store.update("admission_work", {"id": row["id"]}, observed_version=row["observed_version"])
        self.request_discovery()

    def is_current(self, number, head):
        work = self.state.get(int(number))
        decision = loads(work["decision"], {}) if work else {}
        return bool(
            work
            and work["head_sha"] == head
            and work["generation"] == work["evaluated_generation"]
            and decision.get("review_safe")
            and head not in decision.get("recovery_heads", ())
        )

    def invalidate(self, number, **kwargs):
        self.state.dirty(int(number), **kwargs)
        self.wake()

    def withdraw(self, number, head):
        number = int(number)
        self.state.dirty(number, head=head or "", urgent=True)
        row = self.state.get(number)
        self.controller.store.update(
            "admission_work",
            {"id": row["id"]},
            cancelled_head_sha=head or row["head_sha"] or "*",
            cancelled_at=self.controller.clock(),
            cancelled_authorization=row["authorization_key"],
        )
        self.wake()

    def readmit(self, number, head):
        """As `AdmissionPoller.readmit`, for a native PR."""
        from .admission import READMIT_REFUSED

        work = self.state.get(int(number))
        if work is None or work["head_sha"] != head or work["disposition"] in READMIT_REFUSED:
            return False
        self.state.dirty(int(number), head=head, urgent=True)
        self.wake()
        return True

    def accept_manual(self, number, head):
        self.state.dirty(int(number), head=head, urgent=True)
        self.controller.store.update(
            "admission_work",
            {"repository": self.state.repository, "number": int(number)},
            cancelled_head_sha="",
            cancelled_at=0,
            cancelled_authorization="",
        )
        self.wake()

    def _listed_version(self, pull, discovery_version):
        registry, controller = self.registry, self.controller
        return dumps(
            {
                "discovery": discovery_version
                or {
                    "head": pull.head_sha,
                    "base": pull.base_ref,
                    "draft": pull.draft,
                    "refresh": int(controller.clock() // controller.setting("approval_poll_interval_seconds")),
                },
                "policy": registry.native.admission_policy.revision,
                "mode": registry.mode,
            }
        )

    def insertion_check(self, number, head):
        """None when the Forgejo PR still shows the listed facts admission evaluated, else why not.

        The full listing runs only once per approval-poll interval, and mixed landing freezes the
        contribution at admission, so this one read (outside the controller lock) is what keeps a
        closed PR, a moved head, a new label or review activity between listings out of a batch.
        A difference re-observes the PR so it is evaluated again; a failed read holds insertion.
        """
        if not self.is_current(number, head):
            return "not evaluated at this head"
        work = self.state.get(int(number))
        version_of = getattr(self.registry.native, "_discovery_version", None)
        if work is None or work["generation"] != work["evaluated_generation"] or version_of is None:
            return None  # no evaluated listed facts to compare: the local check stands
        try:
            pull = self.registry.native.fetch_pr_or_none(self.registry.profile.forgejo.repository.name, int(number))
        except Exception as error:  # noqa: BLE001 - an unreadable source never admits
            return f"unavailable: {type(error).__name__}"
        if pull is None:
            self.state.observe(int(number), head, "absent:" + self.registry.mode, urgent=True)
            self.wake()
            return "closed"
        version = self._listed_version(pull, version_of(pull))
        if pull.head_sha != head or version != work["observed_version"]:
            self.state.observe(int(number), pull.head_sha, version, urgent=True)
            self.wake()
            return "source changed since evaluation"
        decision = loads(work["decision"], {})
        evaluated_base = (decision.get("evidence") or {}).get("merge_base_sha")
        merge_base = getattr(self.registry.native, "_merge_base", None)
        if evaluated_base and merge_base is not None:
            # The listed facts omit the target tip; a merge base that moved under the same head
            # (part of the PR landed through another) changes the reviewed diff.
            try:
                current_base = merge_base(self.registry.profile.forgejo.repository.name, pull)
            except Exception as error:  # noqa: BLE001 - an unknown merge base never admits
                return f"merge base unavailable: {type(error).__name__}"
            if current_base != evaluated_base:
                self._reevaluate(work, urgent=True)
                self.wake()
                return "merge base changed since evaluation"
        blocked = self.controller._blocking_labels(pull)
        if blocked:
            # Approval refuses these labels; one added after approval keeps the head out too.
            return "blocked-label: " + ", ".join(blocked)
        # Scoreboard edits move neither the PR's update time nor its comment count, and the
        # comment feed is best effort, so the review itself is read and judged once more here.
        snapshot_of = getattr(self.registry.native, "admission_snapshot", None)
        if snapshot_of is not None and work["authorization_key"]:
            try:
                snapshot = snapshot_of(self.registry.profile.forgejo.repository.name, int(number))
                result, authorization, *_ = self.registry.native.admission_policy.evaluate(snapshot)
            except Exception as error:  # noqa: BLE001 - an unreadable review never admits
                return f"review unavailable: {type(error).__name__}"
            if (
                snapshot.pull.head_sha != head
                or not result.get("merge")
                or not result.get("review_safe")
                or head in result.get("recovery_heads", ())
                or authorization != work["authorization_key"]
            ):
                self._reevaluate(work, urgent=True)
                self.wake()
                return "review changed since evaluation"
        return None

    def run_once(self, *, reason="interval"):
        with self._lock:
            return self._run(reason=reason)

    def _run(self, *, reason):
        registry, controller = self.registry, self.controller
        summary = {
            "mode": registry.mode,
            "at": controller.clock(),
            "listed": 0,
            "evaluated": 0,
            "would_admit": 0,
            "approved": 0,
            "errors": [],
        }
        if controller.clock() >= self._discovery_at:
            serial = self._serial
            try:
                listing_started = controller.clock()
                pulls = registry.native.list_open_pull_requests(registry.profile.forgejo.repository.name)
                summary["listed"] = len(pulls)
                seen = set()
                for pull in pulls:
                    seen.add(pull.number)
                    self._observe_listed(pull)
                for row in controller.store.rows("admission_work", {"repository": self.state.repository}):
                    if row["number"] not in seen and row["disposition"] != "closed":
                        self.state.observe(row["number"], row["head_sha"], "absent:" + registry.mode, urgent=True)
                # The full listing (about 50 s for 1,100 open PRs) runs at most once per
                # approval-poll interval, as GitHub discovery does; the time between is spent
                # evaluating. Re-listing every pass left little time for evaluations.
                next_at = controller.clock() + max(
                    registry.poll_seconds, controller.setting("approval_poll_interval_seconds")
                )
                # Recent listings resume from the newest update this listing saw, less a margin
                # for updates made while it paged (the listing took `listing_started` onwards).
                newest = self._newest(pulls)
                if newest:
                    margin = RECENT_MARGIN_SECONDS + (controller.clock() - listing_started)
                    self._recent_since = newest - margin
                self._recent_at = controller.clock() + registry.poll_seconds
            except Exception as error:
                summary["errors"].append(type(error).__name__)
                next_at = controller.clock() + controller.setting("outbox_retry_seconds")
            self._discovery_at = next_at if serial == self._serial else 0
        elif self._recent_enabled() and controller.clock() >= self._recent_at:
            try:
                since = self._recent_since
                pulls, newest, complete = registry.native.list_recent_pull_requests(
                    registry.profile.forgejo.repository.name, since=since
                )
                summary["recent"] = len(pulls)
                for pull in pulls:
                    self._observe_listed(pull)
                if not complete:
                    self._discovery_at = 0  # more changed than a bounded recent listing reads
                elif newest:
                    self._recent_since = max(since, newest - RECENT_MARGIN_SECONDS)
            except Exception as error:
                summary["errors"].append("recent: " + type(error).__name__)
            self._recent_at = controller.clock() + registry.poll_seconds
        if (
            self._recent_enabled()
            and callable(getattr(registry.native, "list_recent_comments", None))
            and controller.clock() >= self._comments_at
        ):
            if self._comments_since is None:
                # First run: start a recheck interval back, so recent edits are not missed.
                self._comments_since = self._recent_since - controller.setting("forgejo_recheck_seconds")
            try:
                self._read_comment_feed(summary)
            except Exception as error:
                summary["errors"].append("comments: " + type(error).__name__)
            self._comments_at = controller.clock() + registry.poll_seconds
        started = time.monotonic()
        while not self.stopping.is_set():
            work = self.state.next_due()
            if work is None:
                break
            self.state.attempted(work)
            try:
                self._evaluate(work, summary)
            except Exception as error:
                summary["errors"].append(f"#{work['number']}: {type(error).__name__}")
                retry = getattr(error, "retry_at", None) or controller.clock() + controller.setting(
                    "outbox_retry_seconds"
                )
                self.state.defer(work, retry, type(error).__name__)
            if time.monotonic() - started >= registry.poll_seconds:
                break
        self.last_summary = summary
        controller.store.set_setting("state:forgejo_observation", summary, "native queue policy", controller.clock())
        if summary["evaluated"]:
            controller._wake.set()
        return summary

    def _evaluate(self, work, summary):
        registry, controller = self.registry, self.controller
        number = registry.number(work["number"])
        if work["observed_version"].startswith("absent:"):
            pull = registry.native.fetch_pr_or_none(registry.profile.forgejo.repository.name, int(number))
            if pull is None:
                with controller._lock, controller.store.transaction():
                    if self.state.get(int(number))["generation"] != work["generation"]:
                        return
                    if registry.mode == "admit":
                        controller._withdraw(
                            registry.profile.forgejo.repository.key, number, "closed", "native queue policy"
                        )
                    self.state.complete(work, work["observed_version"], "", "closed")
                    summary["evaluated"] += 1
                return
        snapshot = registry.native.admission_snapshot(registry.profile.forgejo.repository.name, int(number))
        result, authorization, evidence, comment, meta = registry.native.admission_policy.evaluate(snapshot)
        with controller._lock, controller.store.transaction():
            fresh = self.state.get(int(number))
            if fresh["generation"] != work["generation"]:
                return
            disposition = "observe"
            current = controller._input_for(number)
            if registry.mode == "admit":
                if (
                    snapshot.pull.state != "open"
                    or snapshot.pull.draft
                    or current
                    and current["head_sha"] != snapshot.pull.head_sha
                ):
                    controller._pull_request_event(
                        {
                            "number": number,
                            "action": "closed"
                            if snapshot.pull.state != "open"
                            else "converted_to_draft"
                            if snapshot.pull.draft
                            else "synchronize",
                            "pull_request": {
                                "head": {"sha": snapshot.pull.head_sha},
                                "merged": getattr(snapshot.pull, "merged", False),
                                "title": snapshot.pull.title,
                            },
                        },
                        controller.clock(),
                    )
                if snapshot.pull.state != "open" or snapshot.pull.draft:
                    disposition = "closed" if snapshot.pull.state != "open" else "draft"
                elif (
                    not result["review_safe"]
                    or snapshot.pull.head_sha in result.get("recovery_heads", ())
                    or snapshot.pull.base_ref != controller.canonical_branch
                ):
                    controller._withdraw(
                        registry.profile.forgejo.repository.key, number, "review-unsafe", "native queue policy"
                    )
                    disposition = "review-unsafe"
                elif result["merge"]:
                    if (
                        current
                        and current["head_sha"] == snapshot.pull.head_sha
                        and current["state"] in {"waiting", "reserved", "inserted", "landed"}
                    ):
                        disposition = current["state"]
                    elif (
                        controller.setting("forgejo_admission_enabled")
                        and (
                            restored := controller.restore_missing_source(number, snapshot.pull.head_sha, snapshot.pull)
                        )
                        is not None
                    ):
                        disposition = restored
                    elif self._cancelled(work, snapshot, authorization, comment, meta):
                        disposition = "withdrawn"
                    elif not controller.setting("forgejo_admission_enabled"):
                        disposition = "admission-paused"
                    else:
                        accepted = controller.approve(
                            registry.profile.forgejo.repository.key,
                            number,
                            snapshot.pull.head_sha,
                            "native published review",
                            evidence=evidence,
                            pull=snapshot.pull,
                        )
                        disposition = accepted["reason"]
                        if (
                            not accepted["accepted"]
                            and disposition != "member"
                            and disposition != "admission-paused"
                            and not disposition.startswith("blocked-label:")
                        ):
                            raise RuntimeError("native admission could not retain its exact head")
                        summary["approved"] += int(accepted["accepted"] and disposition == "approved")
            summary["would_admit"] += int(result["merge"])
            controller.store.update(
                "admission_work",
                {"id": work["id"]},
                head_sha=snapshot.pull.head_sha,
                decision=dumps({**result, "evidence": evidence}),
            )
            self.state.complete(work, fresh["observed_version"], authorization, disposition)
            summary["evaluated"] += 1

    def _cancelled(self, work, snapshot, authorization, comment, meta):
        from .admission import EXPLICIT_WITHDRAWALS, timestamp

        current = self.controller._input_for(self.registry.number(work["number"]))
        if (
            not work["cancelled_head_sha"]
            and current
            and current["state"] == "cancelled"
            and current["head_sha"] == snapshot.pull.head_sha
            and current["reason"] in EXPLICIT_WITHDRAWALS
        ):
            self.controller.store.update(
                "admission_work",
                {"id": work["id"]},
                cancelled_head_sha=current["head_sha"],
                cancelled_at=self.controller.clock(),
                cancelled_authorization=authorization,
            )
            return True
        if work["cancelled_head_sha"] not in ("*", snapshot.pull.head_sha):
            return False
        old, fresh = loads(work["cancelled_authorization"], {}), loads(authorization, {})
        changed = authorization != work["cancelled_authorization"]
        newer = timestamp(comment.get("created_at")) > work["cancelled_at"] or bool(
            old and meta.get("round") != old.get("round") and timestamp(meta.get("ts")) > work["cancelled_at"]
        )
        return not (fresh and changed and newer)


class MixedSources:
    def __init__(
        self, profile, config, store, github, clone, *, clock=time.time, native=None, native_main=None, mirror=None
    ):
        from .mixed_native import MixedNativeSource

        self.profile, self.mode, self.store, self.github, self.clone = (
            profile,
            config.forgejo_mode,
            store,
            github,
            str(clone),
        )
        self.repository, self.branch = config.repository, config.canonical_branch or "main"
        self.poll_seconds = config.reconcile_interval_seconds
        self.native = native or MixedNativeSource(profile, config, clone, clock=clock)
        self.native_main, self.mirror = native_main, mirror
        self.port = SourcePorts(self)
        self.primary = store.get_setting("mixed:primary", "github")
        self.generation = store.get_setting("mixed:primary_generation", 0)
        if self.primary not in {"github", "forgejo"} or self.primary == "forgejo" and self.mode != "admit":
            raise ValueError("the current primary requires its active mixed-source configuration")
        self.poller = self.ci = None

    def number(self, number):
        if isinstance(number, SourceNumber):
            self.check_native(number)
            return number
        return SourceNumber(int(number), self.profile.forgejo)

    def supports_candidate(self, base, tip):
        """The existing source CI scope cannot authorize human control-file changes."""
        policy = self.native.admission_policy
        if not hasattr(policy, "prefix"):
            return True
        paths = chain.git(self.clone, "diff", "--name-only", "-z", "--no-renames", base, tip, "--").split("\0")
        return all(not path or path.startswith(policy.prefix) or path in policy.allow_files for path in paths)

    def check_native(self, number=None):
        if self.mode != "admit":
            raise ControllerError("forgejo-inert", "native admission/publication is not enabled")
        if number is not None and isinstance(number, SourceNumber) and number.source != self.profile.forgejo:
            raise ControllerError("unknown-source", str(number))

    def fetch_head(self, number, head):
        self.check_native(number)
        if not chain.object_exists(self.clone, head):
            ref = "refs/tauceti-queue/pulls/" + number.ref_key
            self.native.fetch_refs(self.profile.forgejo.repository.name, [f"+refs/pull/{int(number)}/head:{ref}"])
        if not chain.object_exists(self.clone, head):
            self.native.fetch_refs(self.profile.forgejo.repository.name, [head])
        return chain.object_exists(self.clone, head)

    def check_github(self):
        client = getattr(self.github, "client", None)
        if client is None:
            if getattr(self.github, "repository_identity", None) != self.profile.github:
                raise ValueError("GitHub source port requires its enrolled repository identity")
            return
        response = client.request("GET", "repos/" + self.repository)
        metadata = response.json()
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("id")) is not int
            or metadata["id"] != self.profile.github.native_id
            or metadata.get("full_name") != self.profile.github.repository.name
        ):
            raise ValueError("GitHub source repository identity differs from mixed enrollment")

    def connect(self, controller, github_poller):
        self.controller = controller
        self.check_github()
        self.poller = NativePoller(self, controller)
        if self.mode == "admit":
            from .mixed_ci import MixedCandidateCI

            self.ci = MixedCandidateCI.build(self, controller)
            controller.mixed_ci = self.ci
            if self.native_main is None or self.mirror is None:
                from .mixed_landing import configure

                configure(self)
            controller.admission = AdmissionInputs(github_poller, self.poller)
        return self.poller

    def check_writer(self):
        if self.native_main is None:
            raise ControllerError("primary-writer-unavailable", "explicit Forgejo main writer is required")
