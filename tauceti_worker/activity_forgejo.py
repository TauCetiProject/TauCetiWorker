"""This host's fleet-authored Forgejo PRs, observed from the Forgejo API for the activity report.

The Forgejo `gh` wrapper publishes no creation receipt, so the collector reads the
repository's PRs instead. A PR belongs to this host when its branch ends in one of
this host's worker IDs (`roadmap/<slug>-fleet-<machine8>-<n>`). Rows are keyed by the
forge-qualified repository key, which never collides with GitHub's owner/name keys,
so GitHub numbers and Forgejo numbers count separately.

The first pass lists every PR (the backfill); later passes list only PRs updated since
the previous complete pass. Each pass reads the issue listing (state, merge and close
times) and fetches a PR once, when it is first seen, for its branch.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from tauceti_tools.forge import Forge, RepositoryRef

BORS_MERGED_PREFIX = "[Merged by Bors] - "
INTERVAL = 300
SLACK = 600
DEADLINE = 30
WORKER = re.compile(r"-(fleet-[0-9a-f]{8}-[0-9]+)$")


def repository(env):
    """The Forgejo repository a worker with this environment selects (the canonical one by default)."""
    origin = env.get("TAUCETI_FORGEJO_ORIGIN", "https://git.taucetiproject.org")
    return RepositoryRef(Forge("forgejo", origin), env.get("TAUCETI_FORGEJO_REPO", "TauCetiProject/TauCeti"))


def is_forgejo(repo):
    return repo.startswith("forgejo:")


def _get(repo, path):
    request = urllib.request.Request(
        repo.forge.api_url + "/repos/" + repo.name + path,
        headers={"Accept": "application/json", "User-Agent": "TauCetiWorker/1.0 (activity)"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def refresh(db, machine, now, errors, env):
    repo = repository(env)
    key = repo.key
    row = db.execute("SELECT value FROM meta WHERE key='forgejo_prs'").fetchone()
    state = json.loads(row[0]) if row else {}
    # The backfill continues every pass until it completes; afterwards passes are spaced.
    if state.get("through") and now - state.get("attempted", 0) < INTERVAL:
        return
    state["attempted"] = now
    db.execute("INSERT OR REPLACE INTO meta VALUES ('forgejo_prs',?)", (json.dumps(state),))
    db.execute("CREATE TABLE IF NOT EXISTS forgejo_prs(repo TEXT,number INTEGER,worker TEXT,PRIMARY KEY(repo,number))")
    query = {"type": "pulls", "state": "all", "limit": 50}
    if state.get("through"):
        query["since"] = datetime.fromtimestamp(state["through"] - SLACK, UTC).isoformat()
    stop = time.monotonic() + DEADLINE
    complete = True
    try:
        for page in range(1, 10_000):
            rows = _get(repo, "/issues?" + urllib.parse.urlencode(dict(query, page=page)))
            if not rows:
                break
            for issue in rows:
                number = issue["number"]
                owner = db.execute("SELECT worker FROM forgejo_prs WHERE repo=? AND number=?", (key, number)).fetchone()
                if owner is None:
                    if time.monotonic() > stop:
                        complete = False
                        continue
                    pull = _get(repo, f"/pulls/{number}")
                    # A landed PR's deleted branch survives only in the head label.
                    match = WORKER.search(pull["head"].get("label") or pull["head"]["ref"])
                    worker = match[1] if match and match[1].startswith("fleet-" + machine[:8] + "-") else None
                    db.execute("INSERT INTO forgejo_prs VALUES (?,?,?)", (key, number, worker))
                    if worker:
                        db.execute(
                            "INSERT OR IGNORE INTO prs(repo,number,worker,scope,created,head) VALUES (?,?,?,?,?,?)",
                            (key, number, worker, machine, _epoch(pull["created_at"]), pull["head"]["sha"] or None),
                        )
                    owner = (worker,)
                if owner[0] is None:
                    continue
                merged = (issue.get("pull_request") or {}).get("merged") is True
                # The queue lands a PR by squash and closes it, unmerged, under the Bors title prefix.
                landed = (
                    issue["pull_request"].get("merged_at")
                    if merged
                    else issue["closed_at"]
                    if issue["state"] == "closed" and (issue.get("title") or "").startswith(BORS_MERGED_PREFIX)
                    else None
                )
                db.execute(
                    "UPDATE prs SET state=?,landed=?,observed=?,checked=?,source='forgejo' WHERE repo=? AND number=?",
                    (
                        "OPEN" if issue["state"] == "open" else "MERGED" if merged else "CLOSED",
                        _epoch(landed),
                        now,
                        now,
                        key,
                        number,
                    ),
                )
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("Forgejo PR observations unavailable; retaining last-known data")
        return
    if complete:
        state["through"] = now
        db.execute("INSERT OR REPLACE INTO meta VALUES ('forgejo_prs',?)", (json.dumps(state),))
        # A complete listing since the last pass shows every other PR unchanged.
        db.execute(
            "UPDATE prs SET observed=?,checked=? WHERE repo=? AND landed IS NULL AND state IS NOT 'CLOSED'",
            (now, now, key),
        )
