"""Bounded, transactional publication of prepared operations observations.

Records are changed by stable identity, never by table position. A collector advances
its acknowledged hashes only after the receiver commits every frame of the snapshot.
An incomplete upload cannot replace a previously published generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

UPDATE_SCHEMA = "tauceti.operations.queue-update/v1"
COLLECTIONS = ("batches", "inputs", "members", "attempts", "tasks", "events")
MAX_FRAME_BYTES = 256_000
MAX_SNAPSHOT_BYTES = 15_000_000


class PublicationRejected(ValueError):
    def __init__(self, status, code):
        self.status = status
        self.code = (
            code
            if isinstance(code, str)
            and code
            in (
                "source_changed",
                "superseded",
                "missing_batch",
                "invalid_frame",
                "invalid_snapshot",
                "staging_budget",
                "scope_mismatch",
                "authentication",
                "rate_limit",
                "receiver_unavailable",
            )
            else "invalid_snapshot"
        )
        super().__init__(f"HTTP {status} {self.code}")


class ResyncRequired(ValueError):
    """The receiver lost the delta base; resend current retained facts in full."""


def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


class PublisherIdentity:
    """Persist an identity and monotonic restart epoch; hold one local publisher lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                saved = json.loads(self.path.read_text())
                identity = str(uuid.UUID(saved["id"]))
                epoch = saved["epoch"]
                if type(epoch) is not int or not 0 < epoch < 2**53 - 1:
                    raise ValueError("invalid publisher epoch")
            else:
                identity, epoch = str(uuid.uuid4()), 0
            state = {"id": identity, "epoch": max(epoch + 1, time.time_ns() // 1_000_000)}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encode(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return state
        except BaseException:
            self.lock.close()
            raise

    def __exit__(self, *_):
        self.lock.close()


class SnapshotPublisher:
    def __init__(self, source, *, max_frame_bytes=MAX_FRAME_BYTES):
        self.source = dict(source)
        self.sequence = 0
        self.hashes = {}
        self.base = None
        self.max_frame_bytes = max_frame_bytes

    def prepare(self, observation):
        self.sequence += 1
        source = dict(self.source, sequence=self.sequence)
        metadata = {k: v for k, v in observation.items() if k not in COLLECTIONS and k != "source"}
        metadata["collection_counts"] = {k: len(observation.get(k, [])) for k in COLLECTIONS}
        metadata["retained_batch_ids"] = [b["id"] for b in observation.get("batches", [])]
        metadata["active_batch_ids"] = [
            b["id"] for b in observation.get("batches", []) if b["state"] in ("head", "open")
        ]
        snapshot = str(uuid.uuid4())
        base = {"schema": UPDATE_SCHEMA, "source": source, "snapshot": snapshot, "base": self.base}
        # Reserve the complete commit envelope on every frame, so even a large
        # metadata/heartbeat cannot push the final request past the wire limit.
        budget = self.max_frame_bytes - len(encode(dict(base, metadata=metadata))) - 1024
        if budget < 128:
            raise ValueError("publication metadata exceeds frame limit")
        proposed = dict(self.hashes)
        chunks, chunk, used = [], [], 0
        for collection in COLLECTIONS:
            complete = observation.get("collection_complete", {}).get(collection, False)
            present = {r["id"] for r in observation.get(collection, [])}
            if complete:
                for key in self.hashes.keys() - {(collection, i) for i in present}:
                    if key[0] == collection:
                        proposed.pop(key, None)
                        if used + 120 > budget or len(chunk) >= 500:
                            chunks.append(chunk)
                            chunk, used = [], 0
                        chunk.append({"collection": collection, "id": key[1], "value": None})
                        used += 120
            for record in observation.get(collection, []):
                identity = record["id"]
                if not isinstance(identity, str) or not identity.isdigit():
                    raise ValueError("record identity must be a decimal string")
                key = (collection, identity)
                fingerprint = hashlib.sha256(encode(record)).hexdigest()
                proposed[key] = fingerprint
                if self.hashes.get(key) == fingerprint:
                    continue
                change = {"collection": collection, "id": identity, "value": record}
                length = len(encode(change)) + 1
                if length > budget:
                    raise ValueError("one observation exceeds frame limit")
                if used + length > budget or len(chunk) >= 500:
                    chunks.append(chunk)
                    chunk, used = [], 0
                chunk.append(change)
                used += length
        if chunk or not chunks:
            chunks.append(chunk)
        frames = []
        for index, changes in enumerate(chunks):
            frame = dict(base, part=index, parts=len(chunks), changes=changes)
            if index == len(chunks) - 1:
                frame["metadata"] = metadata
            if len(encode(frame)) > self.max_frame_bytes:
                raise ValueError("publication frame exceeds limit")
            frames.append(frame)
        if sum(len(encode(f)) for f in frames) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds total publication budget")
        return frames, proposed

    def publish(self, observation, send):
        frames, proposed = self.prepare(observation)
        response = None
        try:
            for frame in frames:
                response = send(frame)
        except ResyncRequired:
            self.hashes.clear()
            self.base = None
            raise
        if (
            not isinstance(response, dict)
            or response.get("committed") is not True
            or response.get("snapshot") != frames[-1]["snapshot"]
        ):
            raise ValueError("snapshot commit was not acknowledged")
        # Capped source history must not make the in-memory delta cache grow forever.
        self.hashes = {}
        for collection in COLLECTIONS:
            keys = sorted((k for k in proposed if k[0] == collection), key=lambda k: int(k[1]), reverse=True)[:20000]
            self.hashes.update({k: proposed[k] for k in keys})
        self.base = {"epoch": frames[-1]["source"]["epoch"], "sequence": frames[-1]["source"]["sequence"]}
        return response
