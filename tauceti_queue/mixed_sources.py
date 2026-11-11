"""Optional second-source discovery and original-forge routing for one queue."""

from __future__ import annotations

import threading
import time

from . import chain
from .admission_state import AdmissionState
from .controller import INPUT_RESERVED, INPUT_WAITING, ControllerError
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
            return registry.github.fast_forward(repo, branch, expected, tip)
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
    """Direct native policy evaluation; observation mode never reserves an input."""

    def __init__(self, registry, controller):
        self.registry, self.controller = registry, controller
        self.state = AdmissionState(controller.store, registry.profile.forgejo.repository.key, clock=controller.clock)
        self.last_summary = None
        self._serial = 0
        self._lock = threading.Lock()
        self.stopping = threading.Event()

    def interval(self):
        return self.registry.poll_seconds

    def request_discovery(self):
        self._serial += 1

    def is_current(self, number, head):
        work = self.state.get(number)
        return bool(
            work
            and work["head_sha"] == head
            and work["generation"] == work["evaluated_generation"]
            and loads(work["decision"], {}).get("review_safe")
        )

    def invalidate(self, number, **kwargs):
        self.state.dirty(number, **kwargs)

    def withdraw(self, number, head):
        self.state.dirty(number, head=head, urgent=True)
        row = self.state.get(number)
        self.controller.store.update(
            "admission_work",
            {"id": row["id"]},
            cancelled_head_sha=head or row["head_sha"] or "*",
            cancelled_at=self.controller.clock(),
            cancelled_authorization=row["authorization_key"],
        )

    def accept_manual(self, number, head):
        self.state.dirty(number, head=head, urgent=True)
        self.controller.store.update(
            "admission_work",
            {"repository": self.state.repository, "number": number},
            cancelled_head_sha="",
            cancelled_at=0,
            cancelled_authorization="",
        )

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
        try:
            pulls = registry.native.list_open_pull_requests(registry.profile.forgejo.repository.name)
            summary["listed"] = len(pulls)
        except Exception as error:
            summary["errors"].append(type(error).__name__)
            return self._finish(summary)
        seen = set()
        for pull in pulls:
            if self.stopping.is_set():
                break
            seen.add(pull.number)
            try:
                snapshot = registry.native.admission_snapshot(registry.profile.forgejo.repository.name, pull.number)
                result, authorization, evidence, comment, meta = registry.native.admission_policy.evaluate(snapshot)
                number = registry.number(pull.number)
                version = registry.native._discovery_version(snapshot.pull) + ":" + dumps(result)
                with controller._lock, controller.store.transaction():
                    self.state.observe(pull.number, snapshot.pull.head_sha, version)
                    work = self.state.get(pull.number)
                    disposition = "observe"
                    if registry.mode == "admit":
                        current = controller._input_for(number)
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
                                    "pull_request": {"head": {"sha": snapshot.pull.head_sha}},
                                },
                                controller.clock(),
                            )
                        if not result["review_safe"]:
                            controller._withdraw(
                                registry.profile.forgejo.repository.key, number, "review-unsafe", "native queue policy"
                            )
                            disposition = "review-unsafe"
                        elif result["merge"] and not self._cancelled(work, snapshot, authorization, comment, meta):
                            accepted = controller.approve(
                                registry.profile.forgejo.repository.key,
                                number,
                                snapshot.pull.head_sha,
                                "native published review",
                                evidence=evidence,
                                pull=snapshot.pull,
                            )
                            disposition = accepted["reason"]
                            summary["approved"] += int(accepted["accepted"] and disposition == "approved")
                    summary["would_admit"] += int(result["merge"])
                    controller.store.update(
                        "admission_work", {"id": work["id"]}, decision=dumps({**result, "evidence": evidence})
                    )
                    self.state.complete(work, version, authorization, disposition)
                    summary["evaluated"] += 1
            except Exception as error:
                summary["errors"].append(f"#{pull.number}: {type(error).__name__}")
        if registry.mode == "admit" and not self.stopping.is_set():
            for row in controller.store.rows(
                "inputs", {"repository": controller.repository, "state": [INPUT_WAITING, INPUT_RESERVED]}
            ):
                number = row["number"]
                if (
                    isinstance(number, SourceNumber)
                    and number.source == registry.profile.forgejo
                    and int(number) not in seen
                ):
                    try:
                        live = registry.native.fetch_pr_or_none(registry.profile.forgejo.repository.name, int(number))
                        if live is None or live.state == "closed":
                            with controller._lock:
                                controller._withdraw(
                                    registry.profile.forgejo.repository.key, number, "closed", "native queue policy"
                                )
                    except Exception as error:
                        summary["errors"].append(type(error).__name__)
        return self._finish(summary)

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
        old = loads(work["cancelled_authorization"], {})
        fresh = loads(authorization, {})
        changed = authorization != work["cancelled_authorization"]
        newer = timestamp(comment.get("created_at")) > work["cancelled_at"] or bool(
            old and meta.get("round") != old.get("round") and timestamp(meta.get("ts")) > work["cancelled_at"]
        )
        return not (fresh and changed and newer)

    def _finish(self, summary):
        self.last_summary = summary
        self.controller.store.set_setting(
            "state:forgejo_observation", summary, "native queue policy", self.controller.clock()
        )
        self.controller._wake.set()
        return summary


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
