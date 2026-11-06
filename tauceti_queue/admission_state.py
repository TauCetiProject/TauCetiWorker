"""PR-keyed intake state in the queue's existing database; never merge authority."""

from __future__ import annotations

import time
from collections.abc import Callable

from .store import Store, dumps


class AdmissionState:
    def __init__(self, store: Store, repository: str, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.repository = repository
        self.clock = clock
        self.resource_key = f"state:admission_resources:{repository}"

    def get(self, number: int) -> dict | None:
        return self.store.row("admission_work", {"repository": self.repository, "number": number})

    def observe(self, number: int, head: str, version: str, *, urgent: bool = False) -> bool:
        """A complete discovery snapshot supplies a version, not a completed evaluation."""
        with self.store.transaction():
            row = self.get(number)
            if row is not None and row["head_sha"] == head and row["observed_version"] == version:
                return False
            self._dirty(number, head, version, urgent)
            return True

    def dirty(self, number: int, *, head: str = "", urgent: bool = False) -> None:
        """Coalesce invalidations on one row while fencing any in-flight result."""
        with self.store.transaction():
            self._dirty(number, head, "", urgent)

    def _dirty(self, number: int, head: str, version: str, urgent: bool) -> None:
        if type(number) is not int or number <= 0:
            raise ValueError("admission needs a positive PR number")
        row = self.get(number)
        now = self.clock()
        if row is None:
            self.store.insert(
                "admission_work",
                repository=self.repository,
                number=number,
                head_sha=head,
                observed_version=version,
                urgent=int(urgent),
                first_pending_at=now,
                queue_at=now,
            )
            return
        pending = row["generation"] != row["evaluated_generation"]
        self.store.update(
            "admission_work",
            {"id": row["id"]},
            head_sha=head or row["head_sha"],
            observed_version=version,
            generation=row["generation"] + 1,
            urgent=int(urgent or row["urgent"]),
            first_pending_at=row["first_pending_at"] if pending else now,
            queue_at=now,
            next_attempt_at=0,
            retry_resource="",
        )

    def _pending_query(self, columns="a.*"):
        # A shared resource wait is one deadline, not a new failed attempt for every PR.
        # Urgent revocations use essential priority and may bypass background pacing.
        ready = (
            "CASE WHEN a.urgent=1 AND a.retry_resource LIKE '%:background' "
            "AND EXISTS(SELECT 1 FROM inputs i WHERE i.repository=a.repository "
            "AND i.number=a.number AND i.state IN ('waiting','reserved')) THEN a.next_attempt_at "
            "ELSE MAX(a.next_attempt_at,COALESCE(d.value,0)) END"
        )
        query = (
            f"SELECT {columns}, {ready} AS ready_at FROM admission_work a "
            "LEFT JOIN json_each(?) d ON d.key=a.retry_resource "
            "WHERE a.repository=? AND a.generation!=a.evaluated_generation "
        )
        return query, (dumps(self.store.get_setting(self.resource_key, {})), self.repository)

    def due(self) -> list[dict]:
        """A finite current generation; no PR-count ceiling and no lifetime retry limit."""
        query, params = self._pending_query()
        return self.store.query(
            query
            + "AND ready_at<=? ORDER BY a.urgent DESC,COALESCE(a.queue_at,a.first_pending_at),a.first_pending_at,a.id",
            (*params, self.clock()),
        )

    def attempted(self, work: dict) -> None:
        self.store.update("admission_work", {"id": work["id"]}, attempted_at=self.clock())

    def next_due(self, *, prefer_urgent=True, urgent_only=False):
        """Choose after each completed attempt, so a newly arrived revocation need not wait
        behind a stale copy of the backlog. Resource waits retain a logical evaluation's FIFO
        place; source changes and failures rejoin at the current time. Alternating priority and
        FIFO selections give both revocations and ordinary pending PRs service under continuing traffic.
        """
        order = "a.urgent DESC, " if prefer_urgent else ""
        query, params = self._pending_query()
        rows = self.store.query(
            query
            + "AND ready_at<=? "
            + ("AND a.urgent=1 " if urgent_only else "")
            + f"ORDER BY {order}COALESCE(a.queue_at,a.first_pending_at),a.first_pending_at,a.id LIMIT 1",
            (*params, self.clock()),
        )
        return rows[0] if rows else None

    def complete(self, work: dict, version: str, authorization: str, disposition: str) -> bool:
        """CAS: an old observation cannot acknowledge a newer delivered change."""
        return bool(
            self.store.update(
                "admission_work",
                {"id": work["id"], "generation": work["generation"]},
                evaluated_generation=work["generation"],
                evaluated_version=version,
                authorization_key=authorization,
                disposition=disposition,
                urgent=0,
                queue_at=None,
                next_attempt_at=0,
                last_error="",
                retry_resource="",
            )
        )

    def defer(self, work: dict, until: float, error: str, resource: str = "source") -> bool:
        """Preserve the actual deadline, without postponing newly invalidated work."""
        with self.store.transaction():
            updated = self.store.update(
                "admission_work",
                {"id": work["id"], "generation": work["generation"]},
                next_attempt_at=until,
                last_error=error,
                retry_resource=resource,
                **({"queue_at": self.clock()} if resource == "source" else {}),
            )
            if resource != "source":
                # Capacity belongs to the installation, even if this PR changed during the read.
                self.record_resource_wait(resource, until)
            return bool(updated)

    def record_resource_wait(self, resource: str, until: float) -> None:
        """Discovery and individual reads share the same observed absolute deadline."""
        with self.store.transaction():
            deadlines = self.store.get_setting(self.resource_key, {})
            deadlines[resource] = max(deadlines.get(resource, 0), until)
            self.store.set_setting(self.resource_key, deadlines, "admission", self.clock())

    def summary(self) -> dict:
        query, params = self._pending_query(
            "a.number,a.head_sha,a.attempted_at,a.first_pending_at,a.last_error,a.retry_resource"
        )
        pending = self.store.query(query, params)
        for row in pending:
            row["next_attempt_at"] = row.pop("ready_at")
        return {
            "pending": len(pending),
            "oldest_pending_at": min((r["first_pending_at"] for r in pending), default=None),
            "next_attempt_at": min((r["next_attempt_at"] for r in pending), default=None),
            "pending_prs": [
                {
                    key: r[key]
                    for key in ("number", "head_sha", "attempted_at", "next_attempt_at", "last_error", "retry_resource")
                }
                for r in pending
            ],
        }
