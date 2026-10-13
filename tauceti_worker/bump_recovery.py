"""Refresh an integration-failed dependency branch without launching an editor.

The public Bors observation supplies singleton provenance, current withdrawals,
and the fleet-wide failed-head bound. GitHub's expected_head_sha is the write CAS.
New heads must pass ordinary CI and review; this never posts a Bors approval.
"""

import json
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from .config import NoProgress, log
from .constants import BUMP_HEAD_PREFIXES, MAX_BUMP_REFRESHES, TAUCETI
from .github import GitHubError
from .github_transport import read_context

BORS = "https://bors.taucetiproject.org/repositories/1/active-batches?base=main"
PAUSES = {"keep", "hold", "wip", "human", "do-not-close", "needs-rebase"}
SHA = re.compile(r"[0-9a-f]{40}")


def read_bors(pr):
    context = ssl.create_default_context()
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    if system_ca.is_file():
        context.load_verify_locations(cafile=str(system_ca))
    request = urllib.request.Request(BORS + f"&pr={pr}", headers={"User-Agent": "TauCetiWorker/1.0"})
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        return json.load(response)


def singleton_failure(observation, pr, head):
    """Incomplete/old observations, withdrawals and speculative batch failures defer."""
    if (
        not isinstance(observation, dict)
        or observation.get("schema") != "tauceti-bors.observation/v1"
        or observation.get("repo") != TAUCETI
        or observation.get("base") != "main"
        or any(not isinstance(observation.get(k), list) for k in ("batches", "held", "outcomes", "queue_stops"))
    ):
        return None
    if any(m.get("pr") == pr for b in observation["batches"] for m in b.get("members", [])):
        return None
    if any(m.get("pr") == pr for m in observation["held"]):
        return None
    for stop in observation["queue_stops"]:
        if stop.get("pr") == pr and stop.get("head_sha") == head:
            if stop.get("state") != "failed" or stop.get("reason") != "build":
                return None
    failures = [o for o in observation["outcomes"] if o.get("pr") == pr and o.get("state") == "error"]
    if len({o.get("head_sha") for o in failures}) > MAX_BUMP_REFRESHES:
        return None
    latest = next((o for o in observation["outcomes"] if o.get("pr") == pr and o.get("head_sha") == head), {})
    batch = observation.get("requested_batch") or {}
    if not isinstance(batch, dict):
        return None
    if (
        latest.get("state") != "error"
        or batch.get("state") != "error"
        or batch.get("base") != "main"
        or batch.get("id") != latest.get("batch_id")
        or batch.get("members") != [{"pr": pr, "head_sha": head}]
    ):
        return None
    return batch


def api(gh, path, *, method=None, fields=()):
    args = ["api", f"repos/{TAUCETI}/{path}"]
    if method:
        args += ["--method", method]
    for key, value in fields:
        args += ["-f", f"{key}={value}"]
    result = gh._gh(args)
    if result.returncode:
        raise GitHubError(f"bump recovery GitHub read/write failed: {(result.stderr or result.stdout)[-300:]}")
    return json.loads(result.stdout or "{}")


def recover(w, sv, c, opts, bubble):
    """One selected observed state is charged; infrastructure failure is retryable."""
    p = next((p for p in sv.open_prs if p.number == c.pr), None)
    if p is None or not p.head_ref.startswith(BUMP_HEAD_PREFIXES):
        return None
    if not w.claims.begin_branch_work(c.pr, c.head, p.head_ref, p.head_owner, p.head_repo):
        return None
    key = c.bump_recovery_key
    w.counters.incr(key)
    try:
        with read_context(fresh=True):
            live = api(w.gh, f"pulls/{c.pr}")
            if (
                live.get("state") != "open"
                or live.get("draft")
                or live.get("head", {}).get("sha") != c.head
                or live.get("head", {}).get("ref") != p.head_ref
                or live.get("head", {}).get("repo", {}).get("full_name") != TAUCETI
                or live.get("base", {}).get("ref") != "main"
                or live.get("user", {}).get("login") != "tauceti-review-bot[bot]"
                or {str(label.get("name", "")).lower() for label in live.get("labels", [])} & PAUSES
            ):
                return None
            base = live["base"].get("sha", "")
            if base != p.base_oid or not SHA.fullmatch(base):
                return None
            statuses = api(w.gh, f"commits/{c.head}/status").get("statuses", [])
            latest = {}
            for status in statuses:
                latest.setdefault(status.get("context"), status.get("state"))
            if any(latest.get(k) != "success" for k in ("build", "scope", "bump-guard")):
                return None
            if latest.get("bors") != "failure":
                return None
            batch = singleton_failure(read_bors(c.pr), c.pr, c.head)
            if batch is None:
                return None
            failed_base = batch.get("base_sha")
            if failed_base is None and SHA.fullmatch(batch.get("head_sha") or ""):
                # Historical batches predate the stored base. Bors stages onto
                # main as its first parent; never guess from the PR's merge base.
                parents = api(w.gh, f"git/commits/{batch['head_sha']}").get("parents", [])
                failed_base = parents[0].get("sha") if parents else None
            if not SHA.fullmatch(failed_base or "") or failed_base == base:
                return None
            if api(w.gh, f"compare/{failed_base}...{base}").get("status") != "ahead":
                return None
            if api(w.gh, f"compare/{base}...{c.head}").get("behind_by", 0) <= 0:
                return None
            # Recheck live holds/withdrawals immediately before the branch CAS.
            if singleton_failure(read_bors(c.pr), c.pr, c.head) != batch:
                return None
            api(w.gh, f"pulls/{c.pr}/update-branch", method="PUT", fields=(("expected_head_sha", c.head),))
        log(f"bump #{c.pr}: requested current main after singleton integration failure; waiting for CI and review")
        w.rs.bust(c.pr)
        return 0
    except (GitHubError, urllib.error.URLError, ValueError, OSError, TypeError, KeyError, AttributeError) as error:
        # The CAS makes replay safe even if the write response was lost. If it
        # succeeded, the old head will no longer qualify on the next live read.
        w.counters.write(key, max(0, w.counters.read(key) - 1))
        raise NoProgress(str(error)) from error
