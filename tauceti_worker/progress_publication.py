"""Private receipts for prepared reports; resume publication before paying for prose."""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid

from . import github_outbox as outbox
from . import github_transport as tr
from .constants import PROGRESS_REF, ROADMAP
from .work_recovery import RecordError, progress_checkout, progress_work

MAX_ATTEMPTS = 6
MAX_WAIT_AGE = 7 * 86400

FILES = ("plan.json", "status-body.md", "section-body.md")


def fingerprints(work):
    values = {}
    for name in FILES:
        body = (work / name).read_bytes()
        if not body.strip():
            raise ValueError("empty prepared report")
        values[name] = hashlib.sha256(body).hexdigest()
    plan = json.loads((work / "plan.json").read_text())
    if not isinstance(plan, dict) or not all(
        plan.get(k) for k in ("roadmap", "from_sha", "to_sha", "status_path", "progress_path")
    ):
        raise ValueError("incomplete prepared plan")
    return values


def retained(cfg):
    """Local-only discovery, including pre-receipt reports tied to a retained fork intent."""
    try:
        work = progress_work(cfg)
    except RecordError:
        return None
    receipt = work / "publication.json"
    try:
        hashes = fingerprints(work)
        if receipt.exists():
            value = json.loads(receipt.read_text())
            if (
                isinstance(value, dict)
                and isinstance(value.get("actor"), str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value["actor"])
                and isinstance(value.get("retry_at", 0), (int, float))
                and isinstance(value.get("started", 0), (int, float))
                and isinstance(value.get("waits", 0), int)
                and value.get("waits", 0) >= 0
                and isinstance(value.get("attempts", 0), int)
                and value.get("attempts", 0) >= 0
                and (value.get("version") is None or isinstance(value["version"], str))
                and value.get("files") == hashes
            ):
                return value
            return None
        done = work / "publication-done.json"
        if done.exists() and json.loads(done.read_text()).get("files") == hashes:
            return None
        if (work / "writing.json").exists():
            return None  # fresh-writer attempts never acquire legacy publication authority
        # Old versions wrote prose then failed creating the fork. Do not adopt
        # arbitrary leftover model files without the matching publication evidence.
        env = dict(os.environ)
        paths = outbox.intent_paths(env)
        paths |= outbox.intent_paths(env, suffix="complete")
        plan = json.loads((work / "plan.json").read_text())
        for path in paths:
            try:
                value = json.loads(path.read_text())
                if (outbox.fork_upstream(value["argv"]) or "").lower() != ROADMAP.lower():
                    continue
                who = value.get("context", {}).get("actor") or env.get("TAUCETI_GITHUB_ACCOUNT")
                if who and legacy_branch_matches(cfg, plan):
                    return {
                        "actor": who.lower(),
                        "files": hashes,
                        "legacy": True,
                        "retry_at": 0,
                        "version": PROGRESS_REF,
                        "attempts": 0,
                        "fork_intent": str(path),
                    }
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue

    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def begin_writing(cfg):
    """Fence legacy adoption before changing any canonical inputs or output bodies."""
    tr.atomic(progress_work(cfg) / "writing.json", {"version": 1})


def legacy_branch_matches(cfg, plan):
    """The old proxy recorded Worker cwd, not the roadmap cwd. Bind the saved
    window to its actual local report commit instead of guessing from cwd/mtime.
    """
    try:
        branch = f"progress/{plan['from_sha'][:7]}-{plan['to_sha'][:7]}/{plan['roadmap']}"
        result = subprocess.run(
            ["git", "-C", str(progress_checkout(cfg)), "show", f"{branch}:{plan['progress_path']}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            return False
        headers = re.findall(r"<!--\s*tauceti-progress:v1\s*(\{.*?\})\s*-->", result.stdout, re.S)
        header = json.loads(headers[-1])
        return all(header.get(k) == plan.get(k) for k in ("roadmap", "from_sha", "to_sha")) and header["prs"] == sorted(
            set(plan["prs"])
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError):
        return False


def park(cfg, value, reason):
    """Keep a private recovery bundle before allowing a new report to be planned."""
    work = progress_work(cfg)
    saved = cfg.state / "progress/retained" / uuid.uuid4().hex
    saved.mkdir(parents=True, mode=0o700)
    for name in (*FILES, "facts.json", "progress-prompt.md"):
        source = work / name
        if source.is_file():
            with open(saved / name, "wb", opener=lambda p, f: os.open(p, f, 0o600)) as target:
                target.write(source.read_bytes())
                target.flush()
                os.fsync(target.fileno())
    record = {**value, "status": "parked", "reason": reason, "saved": str(saved)}
    tr.atomic(saved / "publication.json", record)
    complete(cfg, record)
    return saved


def parked_window(cfg, plan):
    """Never pay to regenerate the exact window already saved for inspection."""
    for path in (cfg.state / "progress/retained").glob("*/plan.json"):
        try:
            previous = json.loads(path.read_text())
            if all(previous.get(key) == plan.get(key) for key in ("roadmap", "from_sha", "to_sha", "prs")):
                return path.parent
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def save(cfg, actor, version):
    work = progress_work(cfg)
    value = {"actor": actor.lower(), "files": fingerprints(work), "version": version, "retry_at": 0}
    tr.atomic(work / "publication.json", value)
    return value


def defer(cfg, value):
    value = {
        **value,
        "started": value.get("started") or time.time(),
        "version": value.get("version") or PROGRESS_REF,
        "attempts": value.get("attempts", 0) + 1,
        "retry_at": time.time() + 300,
    }
    tr.atomic(progress_work(cfg) / "publication.json", value)
    return value


def temporary_wait(cfg, value):
    """Infrastructure waits never spend a report attempt or paid error budget."""
    waits = value.get("waits", 0) + 1
    delay = (300, 900, 3600, 14400)[min(waits - 1, 3)]
    value = {
        **value,
        "attempts": max(0, value.get("attempts", 1) - 1),
        "waits": waits,
        "started": value.get("started") or time.time(),
        "retry_at": time.time() + delay,
    }
    if time.time() - value["started"] >= MAX_WAIT_AGE:
        return park(cfg, value, "publication unavailable for seven days; artifacts retained for recovery")
    tr.atomic(progress_work(cfg) / "publication.json", value)
    return None


def complete(cfg, value):
    work = progress_work(cfg)
    tr.atomic(work / "publication-done.json", {**value, "completed": time.time()})
    (work / "publication.json").unlink(missing_ok=True)
