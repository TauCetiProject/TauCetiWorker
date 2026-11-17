"""Refuse a source-candidate dispatch until the runners will admit its harness.

Protected main's `.tauceti/ci-source.json` names the shared harness revision, and its
`.tauceti/ci-source.toml` names the hosted fleet. Restricted runner groups admit a reusable
workflow only at an exact `<harness>/.github/workflows/<file>@<sha>` entry, so a run whose
revision is not yet admitted waits for a runner that never comes. The fleet broker derives
those entries from the same protected main; the queue asks it immediately before the single
dispatch POST and treats anything but an explicit readiness answer as a proven non-send.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from tauceti_tools.project import parse

from . import github_app
from .ports import DispatchNotAccepted

RETRY_AFTER = 15.0
HEX40 = re.compile(r"[0-9a-f]{40}")


class RunnerAdmission:
    def __init__(self, *, opener=None, clock=None, timeout=10):
        self.opener = opener
        self.clock = clock
        self.timeout = timeout

    @staticmethod
    def endpoint(configuration):
        """The hosted fleet's admission URL, or None when no restricted fleet runs this CI."""
        fleet = parse(configuration).service("fleet")
        if fleet.get("mode") != "hosted":
            return None
        url = fleet.get("url")
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username:
            raise ValueError("hosted fleet URL must be an https origin")
        return url.rstrip("/") + "/v1/harness"

    def _open(self):
        if self.opener is None:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=github_app.ssl_context()),
            )
        return self.opener

    def check(self, frozen):
        configuration = getattr(frozen, "configuration", None)
        if configuration is None:
            return
        endpoint = self.endpoint(configuration)
        if endpoint is None:
            return
        revision = frozen.policy.harness_revision
        if not HEX40.fullmatch(revision):
            raise ValueError("harness revision is not a full commit SHA")
        request = urllib.request.Request(
            endpoint + "?revision=" + revision,
            headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "TauCetiQueue"},
        )
        try:
            with self._open().open(request, timeout=self.timeout) as response:
                value = json.loads(response.read(256 * 1024 + 1)[: 256 * 1024])
        except (OSError, ValueError, urllib.error.URLError):
            value = None
        if not isinstance(value, dict) or value.get("revision") != revision or value.get("ready") is not True:
            main = value.get("main_revision") if isinstance(value, dict) else None
            detail = "the fleet broker did not answer" if value is None else f"fleet main is at {main or 'unknown'}"
            retry_at = self.clock() + RETRY_AFTER if self.clock is not None else None
            raise DispatchNotAccepted(
                f"runner groups do not yet admit harness {revision[:12]} ({detail}); retrying before dispatch",
                retry_at=retry_at,
                retryable=True,
            )


def caller_pins(text, harness):
    """Every harness revision literal in a protected caller workflow."""
    name = re.escape(harness)
    found = re.findall(rf"{name}/\.github/(?:actions|workflows)/[^@\s'\"]+@([^\s'\"#]+)", text)
    found += re.findall(r"^\s*tools[-_]ref:\s*['\"]?([^\s'\"#]+)", text, re.MULTILINE)
    return found


def check_caller(text, policy):
    """The caller must run exactly the policy's harness; its literals are derived, never chosen."""
    harness = policy.harness.repository.name
    pins = caller_pins(text, harness)
    reusable = re.findall(rf"{re.escape(harness)}/\.github/workflows/[^@\s'\"]+@", text)
    if not reusable or any(pin != policy.harness_revision for pin in pins):
        raise ValueError(
            f"protected caller pins {sorted(set(pins)) or 'no harness'}, but ci-source.json names "
            f"harness {policy.harness_revision}; derive the literals with tauceti_tools.harness_pins"
        )
