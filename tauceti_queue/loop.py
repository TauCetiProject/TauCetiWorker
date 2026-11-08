"""The controller's reconciliation loop: restart reconciliation, then event-driven passes with bounded polling.

The service is event-driven: `Controller.handle_event` (webhooks), the command methods and the task
API set the controller's wake event, and the loop runs a pass as soon as it is set. Without events
the loop still runs a pass every `polling_interval_seconds` (a runtime setting) so a lost webhook
cannot strand the head; the pass itself only re-reads a CI run when that interval has elapsed or an
event named it, which bounds the polling cost. The shared GitHub client applies actual resource
deadlines to each call; a quota snapshot never suppresses unrelated or local controller work.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .controller import Controller

log = logging.getLogger("tauceti_queue.loop")


class ReconciliationLoop:
    """Runs `controller.recover()` once, then `controller.tick()` on wake or on the polling interval."""

    def __init__(
        self,
        controller: Controller,
        *,
        on_error: Callable[[BaseException], None] | None = None,
        max_interval: float | None = None,
    ) -> None:
        self.controller = controller
        self.on_error = on_error
        self.max_interval = max_interval
        self.stop_event = threading.Event()
        self.passes = 0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None

    def interval(self) -> float:
        interval = float(self.controller.setting("polling_interval_seconds"))
        if self.max_interval is not None:
            interval = min(interval, self.max_interval)
        return max(interval, 0.05)

    def run_once(self, woken: bool) -> bool:
        """Run due controller work; individual requests obey their own resource deadlines."""
        try:
            self.controller.tick()
            self.passes += 1
            self.last_error = None
        except Exception as error:  # noqa: BLE001 - the loop must survive any adapter or git failure
            self.last_error = f"{type(error).__name__}: {error}"
            log.exception("reconciliation pass failed")
            if self.on_error:
                self.on_error(error)
        return True

    def run(self) -> None:
        self.controller.recover()
        wake = self.controller.wake_event()
        woken = True
        while not self.stop_event.is_set():
            self.run_once(woken)
            woken = wake.wait(timeout=self.interval())
            wake.clear()

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="tauceti-queue-loop", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float | None = 10) -> None:
        self.stop_event.set()
        self.controller.wake_event().set()
        if self._thread is not None:
            self._thread.join(timeout)
