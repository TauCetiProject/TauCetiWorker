"""Cancel ordinary maintenance on queue ownership or verification resource failure."""

from __future__ import annotations

import os
import time
import uuid

from .config import AdmissionUnavailable, NoProgress, log
from .verification_resources import DEFAULTS, WAIT_ENV, clear_wait, defaults, queue_owned, wait_reason  # noqa: F401


class ResourceUnavailable(AdmissionUnavailable):
    def __init__(self, message):
        super().__init__(message, state="waiting-resource")


class QueueOwned(NoProgress):
    pass


# Round outcome when the round's own agent closed its PR (a verified supersession or a final
# resolution). The work is done: it is a completion, never a failure.
CLOSED_BY_AGENT = "closed-by-agent"


def closed_by_round(candidate) -> bool:
    """Whether this round's own agent closed the candidate PR, from the mutation journal."""
    progress = getattr(candidate, "progress", None)
    if not progress:
        return False
    from .work_units import _round_mutated

    return _round_mutated(candidate, progress) == "closure"


class MaintenanceStop:
    """Reuse the agent's cancellation hook; observe shared state, never poll GitHub directly."""

    def __init__(self, worker, candidate, *, explicit=False):
        self.worker, self.candidate, self.explicit = worker, candidate, explicit
        self.reason = None
        self.closed = False
        self.outcome = None
        self.next_check = 0.0
        self.path = worker.cfg.state / "resource-waits" / (uuid.uuid4().hex + ".json")

    def __enter__(self):
        self.old = os.environ.get(WAIT_ENV)
        os.environ[WAIT_ENV] = str(self.path)
        return self

    def __exit__(self, kind, error, traceback):
        reason = self.reason
        try:
            resource = wait_reason()
            clear_wait()
        finally:
            if self.old is None:
                os.environ.pop(WAIT_ENV, None)
            else:
                os.environ[WAIT_ENV] = self.old
        if resource:
            raise ResourceUnavailable(resource + "; candidate retained; retry after backoff") from error
        if reason and self.closed and error is None and closed_by_round(self.candidate):
            # The agent finished its job by closing the PR; stopping it afterwards is not a failure.
            log(f"PR #{self.candidate.pr} closed by this round's agent; stopping it as completed")
            self.outcome = CLOSED_BY_AGENT
            return
        if reason:
            raise QueueOwned(reason) from error

    def is_set(self):
        lost = getattr(getattr(self.worker, "claims", None), "lost", None)
        if lost is not None and lost.is_set():
            return True
        if self.reason or wait_reason():
            return True
        if time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + 60
        if getattr(self.worker.gh, "direct_source", False) is True:
            return False
        try:
            from .github_state import read

            available, live = read(self.worker.gh.repo, "pr", number=self.candidate.pr)
            if available and live:
                if live.get("state") in ("CLOSED", "MERGED"):
                    self.closed = live.get("state") == "CLOSED"
                    self.reason = f"PR #{self.candidate.pr} closed while maintenance ran; stopping retained work"
                elif not self.explicit and queue_owned(live.get("labels")):
                    self.reason = f"PR #{self.candidate.pr} belongs to the merge queue; stopping ordinary maintenance"
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Unknown state does not cancel useful work.
        return bool(self.reason)
