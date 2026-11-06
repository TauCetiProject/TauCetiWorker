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
        with self.store.transaction():
            return [
                dict(row)
                for row in self.store.execute(
                    "SELECT * FROM admission_work WHERE repository=? AND generation!=evaluated_generation "
                    "AND next_attempt_at<=? ORDER BY urgent DESC, attempted_at, first_pending_at, id",
                    (self.repository, self.clock()),
                ).fetchall()
            ]

    def attempted(self, work: dict) -> None:
        self.store.update("admission_work", {"id": work["id"]}, attempted_at=self.clock())

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
            )
        )

    def defer(self, work: dict, until: float, error: str) -> bool:
        """Preserve the actual deadline, without postponing newly invalidated work."""
        return bool(
            self.store.update(
                "admission_work",
                {"id": work["id"], "generation": work["generation"]},
                next_attempt_at=until,
                last_error=error,
            )
        )

    def summary(self) -> dict:
        pending = [
            r
            for r in self.store.rows("admission_work", {"repository": self.repository})
            if r["generation"] != r["evaluated_generation"]
        ]
        return {
            "pending": len(pending),
            "oldest_pending_at": min((r["first_pending_at"] for r in pending), default=None),
            "next_attempt_at": min((r["next_attempt_at"] for r in pending), default=None),
        }
