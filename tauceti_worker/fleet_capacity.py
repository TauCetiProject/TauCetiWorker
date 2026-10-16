"""Optional fleet admission policy. Missing or stale fleet policy fails closed.

Ordinary workers have no policy path and retain their existing behavior. A fleet
controller owns the private JSON file; it does not rewrite workers.toml or signal
an active agent merely to reduce concurrency.
"""

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
