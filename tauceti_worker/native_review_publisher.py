"""Bridge the common Review publisher to the queue's existing native receipts.

The private host policy supplies public authority bindings, never queue writer
credentials. All workers on this host share one durable publication outbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Incomplete, PrincipalRef

from tauceti_queue.admission import policy
from tauceti_queue.control_policy_file import read_control_policy
from tauceti_queue.forgejo_source import (
    ForgejoSourceAdapter,
    NativeAdmissionPolicy,
    NativePublishers,
    PublicationSuperseded,
)
from tauceti_queue.store import Store

from .retention import locked


class QueueReviewPublisher:
    def __init__(self, transport):
        self.transport = transport
        try:
            raw = json.loads(read_control_policy(Path(os.environ["TAUCETI_NATIVE_REVIEW_POLICY"])))
            if set(raw) != {"schema", "publishers", "ledger"} or raw["schema"] != "tauceti-native-review-publisher/v1":
                raise ValueError("unknown publisher policy")
            binding = raw["publishers"]
            if set(binding) != {"source", "generation", "reviews", "statuses", "recoveries"}:
                raise ValueError("invalid publisher binding")
            identity = RepositoryIdentity.parse(binding["source"])
            if identity != transport.source.identity:
                raise ValueError("review and publisher repositories differ")
            principals = {}
            for kind in ("reviews", "statuses", "recoveries"):
                ids = binding[kind]
                if (
                    not isinstance(ids, list)
                    or any(type(i) is not int or i < 1 for i in ids)
                    or len(set(ids)) != len(ids)
                ):
                    raise ValueError("invalid publisher principals")
                principals[kind] = frozenset(PrincipalRef(identity.repository.forge, str(i)) for i in ids)
            self.publishers = NativePublishers(identity, generation=binding["generation"], **principals)
            self.ledger = Path(raw["ledger"])
            if not self.ledger.is_absolute():
                raise ValueError("publication ledger must be absolute")
            self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (
                self.ledger.parent.is_symlink()
                or self.ledger.parent.stat().st_uid != os.getuid()
                or self.ledger.parent.stat().st_mode & 0o077
            ):
                raise ValueError("publication ledger directory must be owned and private")
            fd = os.open(self.ledger, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("publication ledger must be owned and private")
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise Incomplete("native review publication policy is unavailable or invalid") from error

    def adapter(self):
        source = self.transport.source
        # Publishing uses only the shared native API protocol. The adapter's Git
        # directory is unused, and no clone or protected-main credential is needed.
        policy = NativeAdmissionPolicy(self.publishers, rubrics=("correctness",), prefix="TauCeti/", allow_files=())
        return ForgejoSourceAdapter(
            self.publishers.source, source.api, self.ledger.parent, token=source.api._token, admission_policy=policy
        )

    def publish(self, body):
        transport = self.transport
        metadata = policy.parse_meta(body)
        if (
            not metadata
            or metadata.get("head_sha") != transport.head
            or metadata.get("repo") != self.publishers.source.repository.key
        ):
            raise Incomplete("native review scoreboard belongs to another source or head")
        # A body and exact source/head are immutable retry identity. In-progress,
        # red and green boards all use this same protocol and sequence allocator.
        key = hashlib.sha256(
            json.dumps([self.publishers.record(), transport.number, transport.head, body], sort_keys=True).encode()
        ).hexdigest()
        try:
            with locked(self.ledger.with_suffix(".lock")):
                store = Store(self.ledger)
                try:
                    adapter = self.adapter()
                    adapter.configure_publisher_policy(store)
                    return self.deliver(store, adapter, key, body)
                finally:
                    store.close()
        except (OSError, ValueError, PermissionError, RuntimeError) as error:
            if isinstance(error, Incomplete):
                raise
            raise Incomplete("native review receipt retained for publication retry") from error

    def deliver(self, store, adapter, key, body):
        transport = self.transport
        intent_key = "review-publication:intent:" + key
        intent = store.get_setting(intent_key)
        if intent is None:
            sequence_key = (
                "review-publication:sequence:"
                + hashlib.sha256(
                    json.dumps(
                        [
                            self.publishers.source.record(),
                            self.publishers.generation,
                            transport.number,
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
            )
            # Seed a new outbox from existing registration once, at preparation.
            # A retry always reuses its persisted intent, including its sequence.
            latest = adapter._publication_latest(
                adapter.client.statuses(adapter.repository, transport.head), "tauceti-review/" + str(transport.number)
            )
            remote = latest[1]["_sequence"] if latest is not None else 0
            with store.transaction():
                sequence = max(store.get_setting(sequence_key, 0), remote) + 1
                intent = adapter.prepare_publication(
                    adapter.repository.name, transport.number, transport.head, body, "review-engine", sequence=sequence
                )
                store.set_setting(sequence_key, sequence)
                store.set_setting(intent_key, intent)
        try:
            identifier = adapter.publish(intent)
        except PublicationSuperseded:
            return {"superseded": True}
        return {"id": int(identifier)}
