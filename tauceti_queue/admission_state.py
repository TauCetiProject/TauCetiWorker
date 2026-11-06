"""PR-keyed intake state in the queue's existing database; never merge authority."""

from __future__ import annotations

import time
from collections.abc import Callable

from .store import Store


class AdmissionState:
    def __init__(self, store: Store, repository: str, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.repository = repository
        self.clock = clock

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
        if row is None:
            self.store.insert(
                "admission_work",
                repository=self.repository,
                number=number,
                head_sha=head,
                observed_version=version,
                urgent=int(urgent),
                first_pending_at=self.clock(),
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
            first_pending_at=row["first_pending_at"] if pending else self.clock(),
            next_attempt_at=0,
        )

    def due(self) -> list[dict]:
        """A finite current generation; no PR-count ceiling and no lifetime retry limit."""
        return self.store.query(
            "SELECT * FROM admission_work WHERE repository=? AND generation!=evaluated_generation "
            "AND next_attempt_at<=? ORDER BY urgent DESC, attempted_at, first_pending_at, id",
            (self.repository, self.clock()),
        )

    def attempted(self, work: dict) -> None:
        self.store.update("admission_work", {"id": work["id"]}, attempted_at=self.clock())

    def next_due(self, *, prefer_urgent=True, urgent_only=False):
        """Choose after each completed attempt, so a newly arrived revocation need not wait
        behind a stale copy of the backlog. Alternating priority and oldest-work selections
        gives both revocations and ordinary pending PRs service under continuing traffic.
        """
        order = "urgent DESC, " if prefer_urgent else ""
        rows = self.store.query(
            "SELECT * FROM admission_work WHERE repository=? AND generation!=evaluated_generation "
            "AND next_attempt_at<=? "
            + ("AND urgent=1 " if urgent_only else "")
            + f"ORDER BY {order}attempted_at, first_pending_at, id LIMIT 1",
            (self.repository, self.clock()),
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
                next_attempt_at=0,
                last_error="",
                retry_resource="",
            )
        )

    def defer(self, work: dict, until: float, error: str, resource: str = "source") -> bool:
        """Preserve the actual deadline, without postponing newly invalidated work."""
        return bool(
            self.store.update(
                "admission_work",
                {"id": work["id"], "generation": work["generation"]},
                next_attempt_at=until,
                last_error=error,
                retry_resource=resource,
            )
        )

    def summary(self) -> dict:
        pending = self.store.query(
            "SELECT number, head_sha, attempted_at, first_pending_at, next_attempt_at, last_error, retry_resource "
            "FROM admission_work WHERE repository=? AND generation!=evaluated_generation",
            (self.repository,),
        )
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
