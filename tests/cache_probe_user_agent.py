#!/usr/bin/env python3
"""The cache preflight identifies itself. Offline: urlopen is replaced by a recorder that answers the
synthetic 404 a healthy bucket gives, so the test pins the OUTGOING request (a Request object with the
worker's User-Agent, GET, the revisions URL, a 30 s timeout) without depending on the CDN's live rules.
The probe is lru-cached, so the cache is cleared before and after, as bubble_cache.py does."""

import sys
import urllib.error
import urllib.request as ur
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

failures = []


def check(name, cond):
    (print("[OK ]", name) if cond else (failures.append(name), print("[XX ]", name)))


seen = {}
real = ur.urlopen


def fake_urlopen(req, *a, **k):
    seen["req"] = req
    seen["timeout"] = k.get("timeout", a[0] if a else None)
    raise urllib.error.HTTPError(getattr(req, "full_url", req), 404, "Not Found", {}, None)


tc.agents.tauceti_cache_unreachable_reason.cache_clear()
ur.urlopen = fake_urlopen
try:
    reason = tc.agents.tauceti_cache_unreachable_reason()
finally:
    ur.urlopen = real
    tc.agents.tauceti_cache_unreachable_reason.cache_clear()

req = seen.get("req")
check("probe sends a Request object, not a bare URL", isinstance(req, ur.Request))
check(
    "probe targets the revisions endpoint",
    isinstance(req, ur.Request) and req.full_url.rstrip("/") == tc.agents.TAUCETI_CACHE_REVISION_URL.rstrip("/"),
)
check(
    "probe carries the worker User-Agent",
    isinstance(req, ur.Request) and req.get_header("User-agent") == tc.agents.CACHE_PROBE_USER_AGENT,
)
check("probe is a GET", isinstance(req, ur.Request) and req.get_method() == "GET")
check("probe uses a 30 s timeout", seen.get("timeout") == 30)
check("a 404 means the host is serving us: no unreachable reason", reason is None)

if failures:
    print("cache_probe_user_agent: FAILED", failures)
    sys.exit(1)
print("cache_probe_user_agent: all cases passed")
