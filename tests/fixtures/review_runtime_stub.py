"""The review orchestration tests supply an engine; runtime preparation has its own suite."""

import atexit
from unittest.mock import patch

from tauceti_worker.review_runtime import ReviewRuntime


def install_runtime_stub():
    lease = patch(
        "tauceti_worker.review_runtime.resolve",
        side_effect=lambda cfg: ReviewRuntime(cfg.state / "refs/review-engine", "fixture"),
    )
    lease.start()
    atexit.register(lease.stop)
