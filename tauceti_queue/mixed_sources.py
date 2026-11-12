"""Optional second-source discovery and original-forge routing for one queue."""

from __future__ import annotations

import threading
import time

from . import chain
from .admission_state import AdmissionState
from .controller import ControllerError
from .mixed_identity import SourceNumber
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

    def invalidate(self, number, **kwargs):
        port = self._for(number)
        if port is not None:
            return port.invalidate(int(number), **kwargs)

    def accept_manual(self, number, head):
        port = self._for(number)
        if port is not None:
            return port.accept_manual(int(number), head)


class NativePoller:
    """Direct native discovery with the existing due/dirty intake ledger."""

    def __init__(self, registry, controller):
        self.registry, self.controller = registry, controller
        self.state = AdmissionState(controller.store, registry.profile.forgejo.repository.key, clock=controller.clock)
        self.last_summary = None
        self._serial = 0
        self._lock = threading.Lock()
        self.stopping = threading.Event()
        self._discovery_at = 0
        self.wake = lambda: None
        configure = getattr(registry.native, "configure_admission_polling", None)
        if configure is not None:
            configure(clock=controller.clock, refresh_seconds=controller.setting("approval_poll_interval_seconds"))

    def interval(self):
        due = self.state.summary()["next_attempt_at"]
        next_at = self._discovery_at if due is None else min(self._discovery_at, due)
        return max(0, next_at - self.controller.clock())

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
            and not decision.get("recovery_authority_hold")
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
                pulls = registry.native.list_open_pull_requests(registry.profile.forgejo.repository.name)
                summary["listed"] = len(pulls)
                seen = set()
                for pull in pulls:
                    seen.add(pull.number)
                    details = pull.admission or {}
                    version = dumps(
                        {
                            "discovery": details.get("discovery_version")
                            or {
                                "head": pull.head_sha,
                                "base": pull.base_ref,
                                "draft": pull.draft,
                                "refresh": int(
                                    controller.clock() // controller.setting("approval_poll_interval_seconds")
                                ),
                            },
                            "policy": registry.native.admission_policy.revision,
                            "mode": registry.mode,
                        }
                    )
                    self.state.observe(pull.number, pull.head_sha, version)
                for row in controller.store.rows("admission_work", {"repository": self.state.repository}):
                    if row["number"] not in seen and row["disposition"] != "closed":
                        self.state.observe(row["number"], row["head_sha"], "absent:" + registry.mode, urgent=True)
                next_at = controller.clock() + registry.poll_seconds
            except Exception as error:
                summary["errors"].append(type(error).__name__)
                next_at = controller.clock() + controller.setting("outbox_retry_seconds")
            self._discovery_at = next_at if serial == self._serial else 0
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
                    or result.get("recovery_authority_hold")
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
        configure_publishers = getattr(self.native, "configure_publisher_policy", None)
        if configure_publishers is not None:
            configure_publishers(controller.store)
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
