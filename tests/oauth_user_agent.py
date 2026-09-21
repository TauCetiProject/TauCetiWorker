#!/usr/bin/env python3
"""The OAuth refresher identifies itself. Offline: urlopen is replaced by a recorder that answers a
canned JSON body, so the test pins the OUTGOING request (a Request object with the worker's
User-Agent, POST, JSON content type and Accept, the given URL and timeout, the payload as the body)
without depending on either token endpoint's live CDN rules. Both endpoints and two non-default
timeouts are exercised, so a helper that hard-coded either would be caught."""

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


def fake_urlopen(req, data=None, timeout=None, **kwargs):
    # urlopen(url, data=None, timeout=...): the first positional after the request is DATA.
    seen["req"] = req
    seen["timeout"] = timeout
    return _Resp(json.dumps({"access_token": "x", "refresh_token": "y", "expires_in": 3600}).encode())


PAYLOAD = {"grant_type": "refresh_token", "refresh_token": "r"}
# Both token endpoints, each with a timeout that is NOT the helper's default (15): a helper that
# hard-coded the Claude URL, or 15 s, would pass a single-case test and fail here.
CASES = ((O.CLAUDE_TOKEN_URL, 7), (O.CODEX_TOKEN_URL, 23))

for url, timeout in CASES:
    seen.clear()
    ur.urlopen = fake_urlopen
    try:
        code, payload = O._post_json(url, PAYLOAD, timeout=timeout)
    finally:
        ur.urlopen = real
    tag = f"[{url.split('/')[2]}, timeout={timeout}]"
    req = seen.get("req")
    ok_req = isinstance(req, ur.Request)
    check(f"{tag} refresher sends a Request object", ok_req)
    check(f"{tag} refresher targets the given token URL", ok_req and req.full_url == url)
    check(f"{tag} refresher is a POST", ok_req and req.get_method() == "POST")
    check(f"{tag} refresher carries the worker User-Agent", ok_req and req.get_header("User-agent") == O.USER_AGENT)
    check(f"{tag} refresher sends JSON", ok_req and req.get_header("Content-type") == "application/json")
    check(f"{tag} refresher accepts JSON", ok_req and req.get_header("Accept") == "application/json")
    check(f"{tag} body is the payload as JSON", ok_req and json.loads(req.data) == PAYLOAD)
    check(f"{tag} the caller's timeout is passed through", seen.get("timeout") == timeout)
    check(
        f"{tag} the canned response is parsed",
        code == 200 and isinstance(payload, dict) and payload.get("access_token") == "x",
    )

if failures:
    print("oauth_user_agent: FAILED", failures)
    sys.exit(1)
print("oauth_user_agent: all cases passed")
