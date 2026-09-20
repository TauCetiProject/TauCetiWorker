#!/usr/bin/env python3
"""The OAuth refresher identifies itself. Offline: urlopen is replaced by a recorder that answers a
canned JSON body, so the test pins the OUTGOING request (a Request object with the worker's
User-Agent, POST, JSON content type, the given URL and timeout, the payload as the body) without
depending on either token endpoint's live CDN rules."""

import io
import json
import sys
import urllib.request as ur
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import oauth as O  # noqa: E402

failures = []


def check(name, cond):
    (print("[OK ]", name) if cond else (failures.append(name), print("[XX ]", name)))


seen = {}
real = ur.urlopen


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return 200


def fake_urlopen(req, *a, **k):
    seen["req"] = req
    seen["timeout"] = k.get("timeout", a[0] if a else None)
    return _Resp(json.dumps({"access_token": "x", "refresh_token": "y", "expires_in": 3600}).encode())


ur.urlopen = fake_urlopen
try:
    code, payload = O._post_json(O.CLAUDE_TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": "r"}, timeout=15)
finally:
    ur.urlopen = real

req = seen.get("req")
check("refresher sends a Request object", isinstance(req, ur.Request))
check("refresher targets the given token URL", isinstance(req, ur.Request) and req.full_url == O.CLAUDE_TOKEN_URL)
check("refresher is a POST", isinstance(req, ur.Request) and req.get_method() == "POST")
check(
    "refresher carries the worker User-Agent",
    isinstance(req, ur.Request) and req.get_header("User-agent") == O.USER_AGENT,
)
check("refresher sends JSON", isinstance(req, ur.Request) and req.get_header("Content-type") == "application/json")
check(
    "body is the payload as JSON",
    isinstance(req, ur.Request) and json.loads(req.data) == {"grant_type": "refresh_token", "refresh_token": "r"},
)
check("timeout is passed through", seen.get("timeout") == 15)
check("the canned response is parsed", code == 200 and isinstance(payload, dict) and payload.get("access_token") == "x")

if failures:
    print("oauth_user_agent: FAILED", failures)
    sys.exit(1)
print("oauth_user_agent: all cases passed")
