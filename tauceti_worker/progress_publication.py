"""Private receipts for prepared reports; resume publication before paying for prose."""

import hashlib
import json
import os
import time

from . import github_outbox as outbox
from . import github_transport as tr
from .constants import ROADMAP

FILES = ("plan.json", "status-body.md", "section-body.md")


def fingerprints(work):
    values = {}
    for name in FILES:
        body = (work / name).read_bytes()
        if not body.strip():
            raise ValueError("empty prepared report")
        values[name] = hashlib.sha256(body).hexdigest()
    plan = json.loads((work / "plan.json").read_text())
    if not all(plan.get(k) for k in ("roadmap", "from_sha", "to_sha", "status_path", "progress_path")):
        raise ValueError("incomplete prepared plan")
    return values


def retained(cfg):
    """Local-only discovery, including pre-receipt reports tied to a retained fork intent."""
    work = cfg.state / "progress/work"
    receipt = work / "publication.json"
    try:
        hashes = fingerprints(work)
        if receipt.exists():
            value = json.loads(receipt.read_text())
            return value if value["files"] == hashes else None
        done = work / "publication-done.json"
        if done.exists() and json.loads(done.read_text()).get("files") == hashes:
            return None
        # Old versions wrote prose then failed creating the fork. Do not adopt
        # arbitrary leftover model files without the matching publication evidence.
        env = dict(os.environ)
        paths = outbox.intent_paths(env)
        paths |= set(outbox.directory(env).glob("*.complete"))
        for path in paths:
            value = json.loads(path.read_text())
            if (outbox.fork_upstream(value["argv"]) or "").lower() != ROADMAP.lower():
                continue
            if value.get("cwd") != str((cfg.state / "progress/roadmap").resolve()):
                continue
            if any((work / name).stat().st_mtime > value.get("created", 0) for name in FILES):
                continue
            who = value.get("context", {}).get("actor") or env.get("TAUCETI_GITHUB_ACCOUNT")
            if who:
                return {"actor": who.lower(), "files": hashes, "legacy": True, "retry_at": 0}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save(cfg, actor, version):
    work = cfg.state / "progress/work"
    value = {"actor": actor.lower(), "files": fingerprints(work), "version": version, "retry_at": 0}
    tr.atomic(work / "publication.json", value)
    return value


def defer(cfg, value):
    tr.atomic(cfg.state / "progress/work/publication.json", {**value, "retry_at": time.time() + 300})


def complete(cfg, value):
    work = cfg.state / "progress/work"
    tr.atomic(work / "publication-done.json", {**value, "completed": time.time()})
    (work / "publication.json").unlink(missing_ok=True)
