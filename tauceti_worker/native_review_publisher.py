"""Connect the common Review publisher to the queue's existing native receipts.

The private host policy supplies public authority bindings, never queue writer
credentials. All workers on this host share one durable publication outbox.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Incomplete, PrincipalRef

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
    def __init__(self, transport, *, policy_path=None):
        self.transport = transport
        try:
            raw = json.loads(read_control_policy(Path(policy_path or os.environ["TAUCETI_NATIVE_REVIEW_POLICY"])))
            if (
                not {"schema", "publishers", "ledger"}
                <= set(raw)
                <= {"schema", "publishers", "ledger", "publisher_host_id"}
                or raw["schema"] != "tauceti-native-review-publisher/v1"
            ):
                raise ValueError("unknown publisher policy")
            if "publisher_host_id" in raw and raw["publisher_host_id"] != Path("/etc/machine-id").read_text().strip():
                raise ValueError("publisher policy belongs to another host")
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

    historical = True
    # Each model run owns a sequence interval. Bounded repairs stay before every
    # later run, even if the later run has reserved but not published its verdict.
    STRIDE = 1024
    MAX_REPAIRS = 3

    @contextlib.contextmanager
    def scope(self):
        """Serialize all finding and scoreboard mutations for this PR."""
        owner = (os.getpid(), threading.get_ident())
        if getattr(self, "_lease_owner", None) == owner:
            yield
            return
        with locked(self.ledger.with_suffix(f".pr-{self.transport.number}.lock"), timeout=30):
            self._lease_owner = owner
            try:
                yield
            finally:
                self._lease_owner = None

    @contextlib.contextmanager
    def outbox(self, number=None):
        store = None
        try:
            # Schema/history transitions are short local transactions. Network
            # calls for different PRs never hold this host-wide lock.
            with locked(self.ledger.with_suffix(".schema.lock"), timeout=30):
                store = Store(self.ledger)
                adapter = self.adapter()
                adapter.configure_publisher_policy(store)
            lease = (
                locked(self.ledger.with_suffix(f".pr-{number}.lock"), timeout=30)
                if number is not None and getattr(self, "_lease_owner", None) != (os.getpid(), threading.get_ident())
                else contextlib.nullcontext()
            )
            with lease:
                yield store, adapter
        except PublicationSequenceConflict as error:
            raise PublisherNotGranted("native review publication sequence requires operator reconciliation") from error
        except PublisherNotGranted:
            raise
        except (OSError, sqlite3.Error) as error:
            raise Incomplete(f"native review outbox unavailable ({type(error).__name__})") from error
        except (ValueError, TypeError, AttributeError) as error:
            raise PublisherNotGranted(
                f"native review outbox requires operator repair ({type(error).__name__})"
            ) from error
        finally:
            if store is not None:
                store.close()

    def preflight(self):
        with self.outbox() as (_store, adapter):
            adapter.validate_review_publisher()

    def scope_key(self):
        return hashlib.sha256(json.dumps(self.publishers.record(), sort_keys=True).encode()).hexdigest()

    def source_key(self):
        return hashlib.sha256(json.dumps(self.publishers.source.record(), sort_keys=True).encode()).hexdigest()

    def active_key(self):
        return "review-publication:active:" + self.source_key()

    def sequence_key(self, number):
        return "review-publication:sequence:" + self.scope_key() + ":" + str(number)

    def body_key(self, body, number, head, run=None):
        return hashlib.sha256(
            json.dumps([INTENT_SCHEMA, self.publishers.record(), number, head, run, body], sort_keys=True).encode()
        ).hexdigest()

    def reserve(self):
        """Create the ordered run authorization before inference, with no remote write."""
        number, head = self.transport.number, self.transport.head
        with self.outbox(number) as (store, adapter):
            actor = adapter.validate_review_publisher()
            remote = adapter.latest_publication_sequence(number, head)
            run = secrets.token_hex(16)
            with store.transaction():
                sequence = (
                    max(store.get_setting(self.sequence_key(number), 0), remote) // self.STRIDE + 1
                ) * self.STRIDE
                if sequence >= 2**63:
                    raise PublisherNotGranted("native review sequence space exhausted")
                record = dict(
                    schema=INTENT_SCHEMA,
                    publishers=self.publishers.record(),
                    number=number,
                    head=head,
                    issuer=int(actor.principal.native_id),
                    sequence=sequence,
                    nonce=secrets.token_hex(16),
                )
                store.set_setting(self.sequence_key(number), sequence)
                store.set_setting("review-publication:run:" + self.source_key() + ":" + run, record)
            return run

    def last_key(self, number):
        return "review-publication:last:" + self.scope_key() + ":" + str(number)

    def comments(self):
        # Reads never wait for a writer's network requests. Every returned body
        # is verified against its commit's immutable receipt, including history.
        with self.outbox() as (store, adapter):
            rows = adapter.review_publication_comments(self.transport.number, self.transport.head)
            if rows:
                return rows
            previous = store.get_setting(self.last_key(self.transport.number))
            if previous and previous["head"] != self.transport.head:
                return adapter.review_publication_comments(self.transport.number, previous["head"])
            return []

    def reservation(self, store):
        run = getattr(self.transport, "plan", {}).get("backend_run")
        if not isinstance(run, str) or len(run) != 32:
            return None, None
        record = store.get_setting("review-publication:run:" + self.source_key() + ":" + run)
        if (
            record
            and record.get("publishers") == self.publishers.record()
            and record.get("number") == self.transport.number
            and record.get("head") == self.transport.head
        ):
            return run, record
        return None, None

    def superseded(self, body):
        with self.outbox() as (store, adapter):
            run, reservation = self.reservation(store)
            if reservation is None:
                existing = store.get_setting(
                    "review-publication:intent:" + self.body_key(body, self.transport.number, self.transport.head)
                )
                if existing is None:
                    return bool(store.get_setting(self.sequence_key(self.transport.number), 0))
                reservation = existing
            remote = adapter.latest_publication_sequence(self.transport.number, self.transport.head)
            return remote // self.STRIDE > reservation["sequence"] // self.STRIDE

    def publish(self, body):
        transport = self.transport
        metadata = policy.parse_meta(body)
        if (
            not metadata
            or metadata.get("head_sha") != transport.head
            or metadata.get("repo") != self.publishers.source.repository.key
        ):
            raise PublisherNotGranted("native review scoreboard belongs to another source or head")
        with self.outbox(transport.number) as (store, adapter):
            run, reservation = self.reservation(store)
            key = self.body_key(body, transport.number, transport.head, run)
            # Retained paid output predating reservations/authority rotation may
            # migrate once, unless a current run already supersedes it.
            if (
                reservation is None
                and not store.get_setting("review-publication:intent:" + key)
                and store.get_setting(self.sequence_key(transport.number), 0)
            ):
                return {"superseded": True}
            return self.deliver(store, adapter, key, body, transport.number, transport.head, reservation)

    def prepare(self, store, adapter, key, body, number, head, reservation=None, *, repairs=None):
        if reservation is None:
            remote = adapter.latest_publication_sequence(number, head)
            sequence = (max(store.get_setting(self.sequence_key(number), 0), remote) // self.STRIDE + 1) * self.STRIDE
            reservation = dict(sequence=sequence, nonce=secrets.token_hex(16))
        intent = adapter.prepare_publication(
            adapter.repository.name,
            number,
            head,
            body,
            "review-engine",
            sequence=reservation["sequence"],
            nonce=reservation["nonce"],
        )
        if "issuer" in reservation and intent["issuer"] != reservation["issuer"]:
            raise PublisherNotGranted("native review reservation belongs to another issuer")
        intent_key = "review-publication:intent:" + key
        with store.transaction():
            previous = store.get_setting(intent_key)
            if previous is not None:
                store.set_setting(intent_key + ":history:" + previous["nonce"], previous)
            store.set_setting(
                self.sequence_key(number), max(store.get_setting(self.sequence_key(number), 0), intent["sequence"])
            )
            store.set_setting(intent_key, intent)
            if repairs is not None:
                store.set_setting(intent_key + ":repairs", repairs)
            store.set_setting(intent_key + ":rendered", body)
            active = store.get_setting(self.active_key(), {})
            current = store.get_setting(active.get(str(number), ""))
            if (
                not current
                or current["generation"] != intent["generation"]
                or current["sequence"] <= intent["sequence"]
            ):
                active[str(number)] = intent_key
                store.set_setting(self.active_key(), active)
        return intent

    def deliver(self, store, adapter, key, body, number, head, reservation=None):
        intent_key = "review-publication:intent:" + key
        intent = store.get_setting(intent_key)
        if intent is None:
            intent = self.prepare(store, adapter, key, body, number, head, reservation)
        repaired = False
        try:
            try:
                identifier = adapter.publish(intent)
            except PublicationDamaged:
                repairs = store.get_setting(intent_key + ":repairs", 0)
                if repairs >= self.MAX_REPAIRS:
                    raise PublisherNotGranted(
                        "native review receipt repeatedly damaged; operator repair required"
                    ) from None
                # Repairs remain within their original run's reserved interval.
                repair = dict(sequence=intent["sequence"] + 1, nonce=secrets.token_hex(16), issuer=intent["issuer"])
                if repair["sequence"] // self.STRIDE != intent["sequence"] // self.STRIDE:
                    raise PublisherNotGranted("native review repair interval exhausted") from None
                intent = self.prepare(store, adapter, key, body, number, head, repair, repairs=repairs + 1)
                identifier = adapter.publish(intent)
                repaired = True
        except PublicationSuperseded:
            return {"superseded": True}
        except PublicationSequenceConflict as error:
            raise PublisherNotGranted("native review publication sequence requires operator reconciliation") from error
        with store.transaction():
            store.set_setting(intent_key + ":published", True)
            previous = store.get_setting(self.last_key(number))
            if previous is None or previous["sequence"] <= intent["sequence"]:
                store.set_setting(self.last_key(number), intent)
        return dict(id=int(identifier), **({"repaired": True} if repaired else {}))

    def reconcile(self, *, now=None, numbers=()):
        """Bounded per-PR integrity/retry sweep; one bad PR cannot starve peers."""
        now = time.time() if now is None else now
        with self.outbox() as (store, _adapter):
            selection = hashlib.sha256(json.dumps(sorted(numbers)).encode()).hexdigest()
            check_key = "review-publication:checked:" + self.scope_key() + ":" + selection
            with store.transaction():
                if now - store.get_setting(check_key, 0) < 300:
                    return False
                active = list(store.get_setting(self.active_key(), {}).items())
                if numbers:
                    active = [(n, k) for n, k in active if int(n) in numbers]
                cursor = store.get_setting(check_key + ":cursor", 0)
                selected = (active[cursor % len(active) :] + active[: cursor % len(active)])[:8] if active else []
                store.set_setting(check_key, now)
                store.set_setting(check_key + ":cursor", cursor + len(selected))
        repaired = False
        for number, intent_key in selected:
            try:
                with self.outbox(int(number)) as (store, adapter):
                    if store.get_setting(self.active_key(), {}).get(number) != intent_key:
                        continue
                    intent = store.get_setting(intent_key)
                    if not intent or intent.get("schema") != INTENT_SCHEMA:
                        raise PublisherNotGranted("native review outbox requires protocol migration")
                    delivered = store.get_setting(intent_key + ":published", False)
                    body = store.get_setting(intent_key + ":rendered")
                    if body is None:
                        raise PublisherNotGranted("native review outbox has no retained rendered body")
                    if intent["generation"] != self.publishers.generation:
                        # A current-generation run already reserved for this PR
                        # takes precedence over reauthorizing an older generation.
                        if store.get_setting(self.sequence_key(int(number)), 0):
                            continue
                        key = self.body_key(body, int(number), intent["head"])
                        intent = self.prepare(store, adapter, key, body, int(number), intent["head"])
                        intent_key = "review-publication:intent:" + key
                        delivered = False
                    # Deliver even a closed or moved head: commit-bound negative
                    # facts must survive reopening at the same commit.
                    result = self.deliver(
                        store,
                        adapter,
                        intent_key.removeprefix("review-publication:intent:"),
                        body,
                        int(number),
                        intent["head"],
                    )
                    repaired |= bool(result.get("repaired") or not delivered and result.get("id"))
                    pull = adapter.client.pull(adapter.repository, int(number))
                    if (pull.get("state") == "closed" or pull.get("head", {}).get("sha") != intent["head"]) and (
                        result.get("id") or result.get("superseded")
                    ):
                        with store.transaction():
                            latest = store.get_setting(self.active_key(), {})
                            if latest.get(number) == intent_key:
                                del latest[number]
                                store.set_setting(self.active_key(), latest)
            except (ForgeError, PublisherNotGranted, OSError, ValueError) as error:
                from .config import log

                log(f"native review receipt {number}: retained for reconciliation ({type(error).__name__})")
        return repaired
