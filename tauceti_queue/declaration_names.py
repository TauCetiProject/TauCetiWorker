"""Compiled declaration names from one trusted PR build, bound to its accepted revision.

These are admission predictions, never a replacement for the worker/CI audits. A record says
which names the accepted PR added to its tested base; it does not describe later agent edits.
Missing, incomplete, ambiguous or malformed records remain unknown and use text-derived context.
"""

from __future__ import annotations

import json
import re

SHA = re.compile(r"[0-9a-f]{40}")
MODULE = re.compile(r"TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+")
PREFIX = "DECLARATION-NAMES "
MAX_MODULES = 4096
MAX_NAMES = 16384


def unknown(reason: str) -> dict:
    return {"state": "unknown", "reason": reason}


def _unique(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def validate(data: object, accepted_head: str) -> dict:
    """Validate an untrusted producer record, preserving the tested source identities."""
    if not isinstance(data, dict) or set(data) != {"version", "head", "base", "tested", "complete", "added"}:
        return unknown("invalid_record")
    if (
        type(data["version"]) is not int
        or data["version"] != 1
        or any(not isinstance(data[k], str) or not SHA.fullmatch(data[k]) for k in ("head", "base", "tested"))
        or type(data["complete"]) is not bool
        or not isinstance(data["added"], dict)
    ):
        return unknown("invalid_record")
    if data["head"] != accepted_head:
        return unknown("head_mismatch")
    if not data["complete"]:
        return (
            {"state": "incomplete", "reason": "producer_incomplete"} if not data["added"] else unknown("invalid_record")
        )
    if len(data["added"]) > MAX_MODULES:
        return unknown("record_limit")
    added = {}
    total = 0
    for module, names in data["added"].items():
        if (
            not isinstance(module, str)
            or not MODULE.fullmatch(module)
            or len(module) > 512
            or not isinstance(names, list)
        ):
            return unknown("invalid_record")
        for name in names:
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 1024
                or any(ord(c) < 33 or ord(c) == 127 for c in name)
            ):
                return unknown("invalid_record")
        clean = sorted(set(names))
        total += len(clean)
        if total > MAX_NAMES:
            return unknown("record_limit")
        if clean:
            added[module] = clean
    return {**data, "added": added, "state": "known" if added else "empty"}


def parse_log(log: str, accepted_head: str) -> dict:
    """Exactly one timestamp/ANSI-stripped producer line is required, as in Bors."""
    records = []
    for raw in log.splitlines():
        line = STAMP.sub("", ANSI.sub("", raw.lstrip("\ufeff")))
        if line.startswith(PREFIX):
            records.append(line[len(PREFIX) :])
            if len(records) > 1:
                return unknown("multiple_records")
    if not records:
        return unknown("record_missing")
    try:
        data = json.loads(records[0], object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return unknown("invalid_json")
    return validate(data, accepted_head)


def accepted(record: object, head: str) -> dict | None:
    """Revalidate a persisted/transported record before treating its added names as exact."""
    if not isinstance(record, dict) or record.get("state") not in ("known", "empty"):
        return None
    data = {key: record.get(key) for key in ("version", "head", "base", "tested", "complete", "added")}
    checked = validate(data, head)
    return record if checked.get("state") in ("known", "empty") and checked["state"] == record["state"] else None
