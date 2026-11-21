"""Private, deduplicated audit-block notifications; never grant the reviewer credentials."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import log
from .constants import TAUCETI
from .paths import ensure_ssl_cert_file


def request(config: dict, method: str, params: dict, *, require_recipient: bool = True) -> dict:
    """One Zulip messages API call as the configured bot. `require_recipient` False is for a caller
    that posts to a stream topic, which names no user (the queue's progress alerts)."""
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
    if require_recipient and (type(config.get("recipient_id")) is not int or config["recipient_id"] <= 0):
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

    # Entry points other than the Worker CLI (the pool, attest) never repaired SSL_CERT_FILE, and uv's
    # standalone CPython cannot find the NixOS bundle by default: every DM then failed verification.
    # Trust the host bundle explicitly, so delivery does not depend on how this process was started.
    context = ssl.create_default_context()  # Keeps SSL_CERT_DIR and other operator trust stores.
    bundle = ensure_ssl_cert_file(dict(os.environ))
    if bundle:
        context.load_verify_locations(cafile=bundle)
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=context))
    with opener.open(req, timeout=20) as response:
        result = json.load(response)
    if result.get("result") != "success":
        raise ValueError("Zulip request was not successful")
    return result


def save(path: Path, value: dict) -> None:
    # A unique temporary per call: concurrent pool reviewers can save the same
    # state file, and a shared temporary name would let one rename the other's.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())  # Durable before it replaces the old state, not only atomic.
        temporary.chmod(0o600)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)  # The rename itself survives a host crash.
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def report(work: Path, block: dict) -> dict:
    """The alert for a block, from the outcome its writer classified; never a second reading of evidence.

    `outcome` is `finding` (with its bound `verdict`), `unreadable` or `not-judged`. Blocks written
    before blocks carried their outcome fall back to the latest ledger verdict for the same range."""
    outcome, verdict = block.get("outcome"), block.get("verdict")
    if outcome is None:
        verdict = None
        ledger = work / "ledger.jsonl"
        for line in ledger.read_text().splitlines() if ledger.exists() else ():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # A damaged row never stops the alert for the block that is in force.
            if isinstance(row, dict) and (row.get("from_sha"), row.get("to_sha")) == (
                block.get("from_sha"),
                block.get("to_sha"),
            ):
                verdict = row.get("verdict", verdict)
        outcome = "finding" if isinstance(verdict, dict) and verdict.get("verdict") != "pass" else "not-judged"
    if outcome == "finding":
        kind = "Inspection incomplete" if verdict.get("verdict") == "incomplete" else "Source-safety finding"
        details = verdict.get("findings") or [block.get("reason", "Unexplained finding")]
    elif outcome == "unreadable":
        kind, details = "Unreadable evidence", [block.get("reason", "Unreadable audit evidence")]
    else:
        kind, details = "Operational stall", [block.get("reason", "Unknown audit failure")]
    return {"kind": kind, "from_sha": block.get("from_sha"), "to_sha": block.get("to_sha"), "details": details}


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
        "**Human review required:** the inspection's evidence cannot be read, so it does not establish "
        "that no source concern was recorded. The range will not be inspected again automatically."
        if value["kind"] == "Unreadable evidence"
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


@contextlib.contextmanager
def locked(path: Path):
    """Serialize a read-modify-write of shared audit state across the Worker, pool and timer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def deliver(work: Path, identity: dict, render) -> bool:
    """Send one private DM per identity, durably deduplicated; True once it is known delivered.

    `render(marker)` builds the message; the marker lets an ambiguous earlier POST be reconciled.
    Raises on delivery failure after recording a 15-minute backoff; callers decide how to log.
    The whole receipt/reconcile/send transaction holds a lock, so concurrent callers never both send.
    """
    configured = os.environ.get("TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG")
    if not configured:
        return False
    with locked(work / "zulip-alerts.lock"):
        return _deliver(work, identity, render, configured)


def _deliver(work: Path, identity: dict, render, configured: str) -> bool:
    config = json.loads(Path(configured).read_text())
    key = hashlib.sha256(
        json.dumps([config["site"], config["recipient_id"], identity], sort_keys=True).encode()
    ).hexdigest()
    marker = "tauceti-audit-" + key
    path = work / "zulip-alerts.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    previous = state.get(key, {})
    if previous.get("message_id"):
        return True
    if previous.get("retry_at", 0) > time.time():
        return False
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
            return True
    result = request(
        config,
        "POST",
        {"type": "direct", "to": json.dumps([config["recipient_id"]]), "content": render(marker)},
    )
    state[key] = {"message_id": result["id"]}
    save(path, state)
    log(f"security-audit: sent private Zulip alert {result['id']}")
    return True


ALERT_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


def delivery_pending(exc: BaseException) -> None:
    # The cause, never the request (which carries the bot credential), so a persistent delivery
    # failure such as TLS verification is visible in the log rather than retried blindly.
    log(
        f"security-audit: private Zulip alert delivery pending ({type(exc).__name__}: {str(exc)[:200]}); "
        "retained block remains in force"
    )


def notify_block(work: Path, block: dict) -> None:
    """Source concerns notify now. Notification failure never clears a block or stops maintenance.

    Operational pauses send nothing here: the cursor watchdog (audit_watchdog) alone reports a
    cursor held for an hour, whatever the cause, so one pause never produces two DMs.
    """
    if not os.environ.get("TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG"):
        return
    try:
        value = report(work, block)
        if value["kind"] == "Operational stall":
            return
        identity = {key: value[key] for key in ("kind", "from_sha", "to_sha")}
        deliver(work, identity, lambda marker: content(value, work, marker))
    except ALERT_ERRORS as exc:
        delivery_pending(exc)
