"""Optional fleet admission policy. Missing or stale fleet policy fails closed.

Ordinary workers have no policy path and retain their existing behavior. A fleet
controller owns the private JSON file; it does not rewrite workers.toml or signal
an active agent merely to reduce concurrency.
"""

import contextlib
import json
import math
import os
import time
from pathlib import Path


def admission_reason(worker: str, *, env=None, now=None) -> str | None:
    env = os.environ if env is None else env
    path = env.get("TAUCETI_FLEET_ADMISSION_FILE")
    if not path:
        return None
    now = time.time() if now is None else now
    try:
        policy = json.loads(Path(path).read_text())
        if not isinstance(policy, dict):
            raise ValueError("invalid policy")
        expires = policy["expires_at"]
        allowed = policy["workers"]
        if (
            policy.get("version") != 1
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or not isinstance(allowed, list)
            or not all(isinstance(w, str) for w in allowed)
        ):
            raise ValueError("invalid policy")
        if expires <= now:
            return "fleet controller heartbeat expired; waiting for capacity"
        if expires > now + 30:
            return "fleet admission clock is inconsistent; waiting for controller"
        if worker not in allowed:
            return str(policy.get("reason") or "fleet capacity reserved for CI")[:500]
    except (OSError, ValueError, KeyError, TypeError):
        return "fleet admission policy unavailable; waiting for controller"
    return None


_CURRENT_UNIT = None


@contextlib.contextmanager
def admission_unit(worker):
    """Keep late launch checks scoped to the selected unit, including continuations."""
    global _CURRENT_UNIT
    previous = _CURRENT_UNIT
    _CURRENT_UNIT = dict(worker=worker, admitted=False)
    try:
        yield
    finally:
        _CURRENT_UNIT = previous


def unit_admitted():
    return _CURRENT_UNIT["admitted"] if _CURRENT_UNIT is not None else None


def check_launch_admission():
    from .claude_api import CURRENT_CONTEXT, admission_failure

    # The API bridge checks at actual inference admission, including budget policy.
    if CURRENT_CONTEXT is not None or unit_admitted():
        return
    worker = _CURRENT_UNIT["worker"] if _CURRENT_UNIT is not None else os.environ.get("TAUCETI_WORKER_ID", "default")
    if reason := admission_reason(worker):
        raise admission_failure(reason, state="waiting-capacity")
    if _CURRENT_UNIT is not None:
        _CURRENT_UNIT["admitted"] = True
