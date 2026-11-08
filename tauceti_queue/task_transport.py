"""Bound native task HTTP exchanges, including DNS and trickling peers.

One exchange runs in a disposable stdlib-only process. Its source is captured
with the parent module, so a live update cannot swap in a different wire helper.
A timeout is uncertain delivery; this transport never resends a POST.
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
import sys
import time

RESPONSE_LIMIT = 16 * 1024 * 1024


class TaskTransportError(OSError):
    """A native exchange failed; the delivery classification is conservative."""

    def __init__(self, message: str, *, delivery_uncertain=False):
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain


def check_timeout(timeout: float) -> None:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("native task timeout must be finite and between 0 and 3600 seconds")


# No site initialization, project imports, external commands or credentials in
# this constant. Keep the byte bound and framing version with the parent code.
_HELPER_SOURCE = r"""
import base64
import json
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 16 * 1024 * 1024

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None

def fail(category):
    sys.stdout.buffer.write(category.encode("ascii"))
    return 70

def exchange():
    value = json.load(sys.stdin)
    remaining = value["deadline"] - time.monotonic()
    if remaining <= 0:
        return fail("before-deadline")
    # Also bound the helper if its parent dies. The parent normally kills and
    # reaps sooner; the margin avoids racing a complete response at the limit.
    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, remaining + 0.25)
    else:
        import os
        import threading
        timer = threading.Timer(remaining + 0.25, lambda: os._exit(124))
        timer.daemon = True
        timer.start()
    try:
        url = urllib.parse.urlsplit(value["url"])
        _ = url.port
    except ValueError:
        return fail("url-refused")
    if (
        not url.hostname or url.username is not None or url.password is not None
        or url.query or url.fragment
        or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}))
    ):
        return fail("url-refused")
    handlers = [NoRedirect()]
    if url.hostname in {"127.0.0.1", "::1"}:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        value["url"], data=base64.b64decode(value["data"], validate=True), headers=value["headers"], method="POST"
    )
    try:
        response = opener.open(request, timeout=remaining)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.code
        payload = response.read(LIMIT + 1)
    if len(payload) > LIMIT:
        return fail("oversize")
    sys.stdout.buffer.write(str(status).encode("ascii") + b"\n" + payload)
    return 0

try:
    code = exchange()
except urllib.error.URLError as error:
    reason = error.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = fail("tls-verify")
    elif isinstance(reason, socket.gaierror):
        code = fail("dns")
    elif isinstance(reason, ConnectionRefusedError):
        code = fail("connect")
    else:
        code = fail("transport")
except Exception:
    code = fail("transport")
raise SystemExit(code)
"""

_ERROR_CATEGORIES = {
    b"before-deadline": ("native task deadline elapsed before HTTP send", False),
    b"url-refused": ("native task helper refused the service URL", False),
    b"tls-verify": ("native task TLS certificate verification failed", False),
    b"dns": ("native task DNS resolution failed", False),
    b"connect": ("native task connection was refused", False),
    b"oversize": ("native task response exceeds its byte bound", True),
    b"transport": ("native task HTTP exchange failed; delivery is uncertain", True),
}


def post(url: str, data: bytes, headers: dict[str, str], *, timeout: float) -> tuple[int, bytes]:
    """Send once, with secrets on stdin. Kill and reap before reporting timeout.

    The total budget includes encoding and spawn. Responses from helpers that
    already exited cleanly are retained if the parent was descheduled. OS
    process creation/reaping itself cannot be interrupted by a Python timeout.
    """
    check_timeout(timeout)
    if not isinstance(sys.executable, str) or not sys.executable:
        raise TaskTransportError("native task transport needs an available Python interpreter")
    deadline = time.monotonic() + timeout
    request = json.dumps(
        {"url": url, "data": base64.b64encode(data).decode("ascii"), "headers": headers, "deadline": deadline}
    ).encode()
    sent = False
    try:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _HELPER_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TaskTransportError("native task deadline elapsed before HTTP send")
                sent = True
                output, _ = process.communicate(request, timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
                # communicate can notice its timeout after the child already
                # flushed a complete response and exited. Do not lose that
                # assignment merely because the parent was descheduled.
                if process.returncode != 0:
                    raise
            except BaseException:
                process.kill()
                process.communicate()
                raise
    except subprocess.TimeoutExpired:
        raise TaskTransportError(
            "native task request exceeded its total deadline; delivery is uncertain", delivery_uncertain=True
        ) from None
    except TaskTransportError:
        raise
    except OSError:
        raise TaskTransportError("native task transport process failed", delivery_uncertain=sent) from None
    if process.returncode == 70 and output in _ERROR_CATEGORIES:
        message, uncertain = _ERROR_CATEGORIES[output]
        raise TaskTransportError(message, delivery_uncertain=uncertain)
    if process.returncode != 0:
        raise TaskTransportError("native task helper failed; delivery is uncertain", delivery_uncertain=True)
    status, separator, payload = output.partition(b"\n")
    if not separator or not status.isdigit() or len(status) != 3 or not 100 <= int(status) <= 599:
        raise TaskTransportError("native task transport returned an invalid response", delivery_uncertain=True)
    if len(payload) > RESPONSE_LIMIT:
        raise TaskTransportError("native task response exceeds its byte bound", delivery_uncertain=True)
    return int(status), payload
