"""Connect the common Review publisher to the queue's existing native receipts.

The private host policy supplies public authority bindings, never queue writer
credentials. All workers on this host share one durable publication outbox.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Incomplete, PrincipalRef

from tauceti_queue.admission import policy
from tauceti_queue.control_policy_file import read_control_policy
from tauceti_queue.forgejo_source import (
    INTENT_SCHEMA,
    ForgejoSourceAdapter,
    NativePublishers,
    PublicationDamaged,
    PublicationSequenceConflict,
    PublicationSuperseded,
    PublisherNotGranted,
)
from tauceti_queue.store import Store

from .github_transport import locked


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
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise PublisherNotGranted("native review publication policy is unavailable or invalid") from error

    def adapter(self):
        source = self.transport.source
        # Publishing uses only the shared native API protocol. The adapter's Git
        # directory is unused, and no clone or protected-main credential is needed.
        return ForgejoSourceAdapter.for_publication(
            self.publishers, source.api, self.ledger.parent, token=source.api._token
        )

    @contextlib.contextmanager
    def outbox(self):
        try:
            with locked(self.ledger.with_suffix(".lock"), timeout=30):
                store = Store(self.ledger)
                try:
                    adapter = self.adapter()
                    adapter.configure_publisher_policy(store)
                    yield store, adapter
                finally:
                    store.close()
        except PublisherNotGranted:
            raise
        except (OSError, sqlite3.Error) as error:
            raise Incomplete(f"native review outbox unavailable ({type(error).__name__})") from error
        except (ValueError, TypeError, AttributeError) as error:
            raise PublisherNotGranted(
                f"native review outbox requires operator repair ({type(error).__name__})"
            ) from error

    def preflight(self):
        with self.outbox() as (_store, adapter):
            adapter.validate_review_publisher()
        return True  # The common Review CLI publishes its existing init plan before inference.

    def scope_key(self):
        return hashlib.sha256(json.dumps(self.publishers.record(), sort_keys=True).encode()).hexdigest()

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
            json.dumps(
                [INTENT_SCHEMA, self.publishers.record(), transport.number, transport.head, body], sort_keys=True
            ).encode()
        ).hexdigest()
        with self.outbox() as (store, adapter):
            return self.deliver(store, adapter, key, body, transport.number, transport.head)

    def prepare(self, store, adapter, key, body, number, head):
        sequence_key = "review-publication:sequence:" + self.scope_key() + ":" + str(number)
        remote = adapter.latest_publication_sequence(number, head)
        with store.transaction():
            sequence = max(store.get_setting(sequence_key, 0), remote) + 1
            intent = adapter.prepare_publication(
                adapter.repository.name, number, head, body, "review-engine", sequence=sequence
            )
            intent_key = "review-publication:intent:" + key
            previous = store.get_setting(intent_key)
            if previous is not None:
                store.set_setting(intent_key + ":history:" + previous["nonce"], previous)
            store.set_setting(sequence_key, sequence)
            store.set_setting(intent_key, intent)
            store.set_setting(intent_key + ":rendered", body)
            active_key = "review-publication:active:" + self.scope_key()
            active = store.get_setting(active_key, {})
            active[str(number)] = intent_key
            store.set_setting(active_key, active)
        return intent

    def deliver(self, store, adapter, key, body, number, head):
        intent_key = "review-publication:intent:" + key
        intent = store.get_setting(intent_key)
        if intent is None:
            intent = self.prepare(store, adapter, key, body, number, head)
        repaired = False
        try:
            try:
                identifier = adapter.publish(intent)
            except PublicationDamaged:
                # A moderated immutable comment needs a fresh nonce/sequence,
                # reusing the exact paid body. Keep the old intent for diagnosis.
                intent = self.prepare(store, adapter, key, body, number, head)
                identifier = adapter.publish(intent)
                repaired = True
        except PublicationSuperseded:
            return {"superseded": True}
        except PublicationSequenceConflict as error:
            raise PublisherNotGranted("native review publication sequence requires operator reconciliation") from error
        store.set_setting(intent_key + ":published", True)
        result = {"id": int(identifier)}
        if repaired:
            result["repaired"] = True
        return result

    def reconcile(self, *, now=None):
        """Check current retained receipts periodically, including after scratch cleanup."""
        now = time.time() if now is None else now
        with self.outbox() as (store, adapter):
            check_key = "review-publication:checked:" + self.scope_key()
            if now - store.get_setting(check_key, 0) < 300:
                return False
            store.set_setting(check_key, now)
            active_key = "review-publication:active:" + self.scope_key()
            active = store.get_setting(active_key, {})
            repaired = False
            for number, intent_key in list(active.items()):
                intent = store.get_setting(intent_key)
                if not intent or intent.get("schema") != INTENT_SCHEMA:
                    raise PublisherNotGranted("native review outbox requires protocol migration")
                pull = adapter.client.pull(adapter.repository, int(number))
                if pull.get("state") != "open" or pull.get("head", {}).get("sha") != intent["head"]:
                    del active[number]
                    continue
                delivered = store.get_setting(intent_key + ":published", False)
                # The paid body in the frozen intent includes its old binding.
                # Use the original pre-binding body stored alongside its intent.
                body = store.get_setting(intent_key + ":rendered")
                if body is None:
                    raise PublisherNotGranted("native review outbox has no retained rendered body")
                result = self.deliver(
                    store,
                    adapter,
                    intent_key.removeprefix("review-publication:intent:"),
                    body,
                    int(number),
                    intent["head"],
                )
                repaired |= bool(result.get("repaired") or not delivered and result.get("id"))
            store.set_setting(active_key, active)
            return repaired
