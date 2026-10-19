"""Private, deduplicated audit-block notifications; never grant the reviewer credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import log
from .constants import TAUCETI

OPERATIONAL_STALL_SECONDS = 3600


def request(config: dict, method: str, params: dict) -> dict:
    site = config["site"].rstrip("/")
    url = urllib.parse.urlsplit(site)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("Zulip site must be an HTTPS origin")
    if type(config["recipient_id"]) is not int or config["recipient_id"] <= 0:
        raise ValueError("Zulip recipient must be a positive user ID")
    encoded = urllib.parse.urlencode(params).encode()
    endpoint = site + "/api/v1/messages"
    auth = base64.b64encode(f"{config['email']}:{config['api_key']}".encode()).decode()
    req = urllib.request.Request(
        endpoint + ("?" + encoded.decode() if method == "GET" else ""),
        data=encoded if method == "POST" else None,
        headers={"Authorization": "Basic " + auth},
        method=method,
    )

    # Do not forward a bot credential through an HTTP redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        result = json.load(response)
    if result.get("result") != "success":
        raise ValueError("Zulip request was not successful")
    return result


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(path)


def report(work: Path, block: dict) -> dict:
    verdict = None
    ledger = work / "ledger.jsonl"
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            row = json.loads(line)
            if (row.get("from_sha"), row.get("to_sha")) == (block.get("from_sha"), block.get("to_sha")):
                if "verdict" in row:
                    verdict = row["verdict"]
    kind = "Operational stall"
    if verdict and verdict["verdict"] != "pass":
        kind = "Source-safety finding" if verdict["verdict"] == "finding" else "Inspection incomplete"
    return {
        "kind": kind,
        "from_sha": block.get("from_sha"),
        "to_sha": block.get("to_sha"),
        "details": verdict["findings"] if verdict else [block.get("reason", "Unknown audit failure")],
    }


def actionable(work: Path, value: dict) -> bool:
    """Source concerns notify now; operational pauses notify after an hour held.

    The cursor, rather than an attempt ID or failure text, identifies the stall.
    Retries and Worker restarts cannot reset the delay. A new cursor resets it.
    This timer controls notifications only; audit blocks/recovery are untouched.
    """
    if value["kind"] != "Operational stall":
        return True
    path = work / "operational-alert.json"
    now = time.time()
    state = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("invalid operational alert state")
    if state.get("from_sha") != value["from_sha"]:
        state = {"from_sha": value["from_sha"], "first_seen": now}
        save(path, state)
    started = state.get("first_seen")
    if type(started) not in (int, float) or not math.isfinite(started):
        raise ValueError("invalid operational alert timestamp")
    return now - started >= OPERATIONAL_STALL_SECONDS


def content(value: dict, work: Path, marker: str) -> str:
    # Model-generated diagnostics are quoted as data, never mentions or Markdown commands.
    detail = "\n".join(value["details"])[:6000].replace("`", "ˋ").replace("@", "＠")
    old, target = value["from_sha"], value["to_sha"]
    location = (
        f"https://github.com/{TAUCETI}/compare/{old}...{target}"
        if old and target
        else f"https://github.com/{TAUCETI}/commits/main"
    )
    attention = (
        "**Attention: the audit cursor has remained held for at least one hour.** "
        "This is an operational stall, with no recorded source-safety finding for this range. "
        "Automatic recovery continues where eligible; investigate if the stall persists."
        if value["kind"] == "Operational stall"
        else "**Human review required:** the inspection recorded a source concern or unresolved "
        "code-safety uncertainty. Automatic recovery will not discard this judgment."
    )
    return (
        f"**Tau Ceti security audit — {value['kind']}**\n\n"
        f"{attention}\n\nInspection: {location}\n\n```text\n{detail}\n```\n\n"
        "The `security-audit` cursor has not advanced past this inspection. "
        "Toolchain releases remain gated by that cursor. Bump/progress work may continue.\n\n"
        f"Evidence on the worker host: `{work / 'blocked.json'}` and `{work / 'ledger.jsonl'}`.\n\n"
        f"Alert ID: {marker}"
    )


def notify_block(work: Path, block: dict) -> None:
    """Notification failure never clears a block or stops maintenance; retry after 15 minutes."""
    configured = os.environ.get("TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG")
    if not configured:
        return
    try:
        value = report(work, block)
        if not actionable(work, value):
            log("security-audit: transient operational pause retained in logs; no private alert before one hour")
            return
        config = json.loads(Path(configured).read_text())
        identity = {key: value[key] for key in ("kind", "from_sha", "to_sha")}
        key = hashlib.sha256(
            json.dumps([config["site"], config["recipient_id"], identity], sort_keys=True).encode()
        ).hexdigest()
        marker = "tauceti-audit-" + key
        path = work / "zulip-alerts.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        previous = state.get(key, {})
        if previous.get("message_id") or previous.get("retry_at", 0) > time.time():
            return
        state[key] = {"retry_at": time.time() + 900}
        save(path, state)  # Back off even if reconciliation fails.
        # A previous POST may have succeeded before its response or local receipt was lost.
        # Reconcile only this bot's configured private conversation before re-sending.
        if previous:
            recent = request(
                config,
                "GET",
                {
                    "narrow": json.dumps([{"operator": "dm", "operand": [config["recipient_id"]]}]),
                    "anchor": "newest",
                    "num_before": 100,
                    "num_after": 0,
                    "apply_markdown": "false",
                },
            )
            found = next((m for m in recent["messages"] if marker in m["content"]), None)
            if found:
                state[key] = {"message_id": found["id"]}
                save(path, state)
                return
        result = request(
            config,
            "POST",
            {
                "type": "direct",
                "to": json.dumps([config["recipient_id"]]),
                "content": content(value, work, marker),
            },
        )
        state[key] = {"message_id": result["id"]}
        save(path, state)
        log(f"security-audit: sent private Zulip alert {result['id']}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        log("security-audit: private Zulip alert delivery pending; retained block remains in force")
