"""This host's fleet-authored Forgejo PRs, observed from the Forgejo API for the activity report.

The Forgejo `gh` wrapper publishes no creation receipt, so the collector reads the
repository's PRs instead. A PR belongs to this host when its branch ends in one of
this host's worker IDs (`roadmap/<slug>-fleet-<machine8>-<n>`). Rows are keyed by the
forge-qualified repository key, which never collides with GitHub's owner/name keys,
so GitHub numbers and Forgejo numbers count separately.

The first pass lists every PR (the backfill); later passes list only PRs updated since
the previous complete pass. Each pass reads the issue listing (state, merge and close
times) and fetches a PR once, when it is first seen, for its branch. Once a PR lands,
`measure` records its Lean LOC and review passes, which the GitHub state service
supplies for GitHub PRs.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from tauceti_tools.forge import Forge, RepositoryRef
from tauceti_tools.survey_facts import SPEC

BORS_MERGED_PREFIX = "[Merged by Bors] - "
INTERVAL = 300
SLACK = 600
DEADLINE = 30
# Measurements of landed PRs: a per-pass time budget and batch, and the retry delay.
MEASURE_SECONDS = 10
MEASURE_LIMIT = 200
RETRY = 3600
WORKER = re.compile(r"-(fleet-[0-9a-f]{8}-[0-9]+)$")


def repository(env):
    """The Forgejo repository a worker with this environment selects (the canonical one by default)."""
    origin = env.get("TAUCETI_FORGEJO_ORIGIN", "https://git.taucetiproject.org")
    return RepositoryRef(Forge("forgejo", origin), env.get("TAUCETI_FORGEJO_REPO", "TauCetiProject/TauCeti"))


def is_forgejo(repo):
    return repo.startswith("forgejo:")


def _get(repo, path, timeout=10):
    request = urllib.request.Request(
        repo.forge.api_url + "/repos/" + repo.name + path,
        headers={"Accept": "application/json", "User-Agent": "TauCetiWorker/1.0 (activity)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
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


SAFE = 2**53 - 1  # The state service's counters are JSON-safe integers.


class _Deadline(Exception):
    pass


def _read(repo, path, stop):
    left = stop - time.monotonic()
    if left <= 0:
        raise _Deadline
    return _get(repo, path, timeout=min(10, left))


def _pages(repo, path, stop):
    rows = []
    for page in range(1, 1000):
        batch = _read(repo, f"{path}?limit=50&page={page}", stop)
        if not batch:
            return rows
        rows.extend(batch)
    raise ValueError("unbounded listing")


def lean_loc(pull, files):
    """Lean lines of a PR's file listing, if it accounts for the PR's whole diff."""
    if (
        sum(f["additions"] for f in files) != pull["additions"]
        or sum(f["deletions"] for f in files) != pull["deletions"]
    ):
        return None  # A truncated listing must not pass for an exact count.
    lean = [f for f in files if f["filename"].endswith(".lean")]
    return dict(added=sum(f["additions"] for f in lean), deleted=sum(f["deletions"] for f in lean))


def review_passes(comments, head):
    """The scoreboards' cumulative round counters, reduced as the GitHub state service does."""
    count = boards = 0
    current = False
    for comment in comments:
        body = comment.get("body") or ""
        found = re.findall(SPEC["meta_pattern"], body)
        if SPEC["scoreboard"] not in body or not found:
            continue
        try:
            meta = json.loads(found[-1])
        except ValueError:
            continue  # An unparsable scoreboard carries no counter.
        if not isinstance(meta, dict) or meta.get("mode") == "init":
            continue
        value = meta.get("round")
        if type(value) is not int or not 0 <= value <= SAFE:
            return None
        count += value
        if count > SAFE:
            return None
        boards += 1
        current = current or bool(head and meta.get("head_sha") == head)
    return count if boards and current else None


def measure(db, now, errors, env):
    """Record LOC and review passes for this host's landed Forgejo PRs.

    The GitHub state service supplies these for GitHub PRs. Each landed PR costs a few
    reads, once: its final head and diff totals, its file listing and its comments.
    PRs never attempted come first; an incomplete or failed measurement is retried
    after RETRY seconds, oldest attempt first, so no PR blocks the others.
    """
    repo = repository(env)
    key = repo.key
    stop = time.monotonic() + MEASURE_SECONDS
    due = db.execute(
        """SELECT p.number,p.head FROM prs p LEFT JOIN pr_review_counts c ON c.repo=p.repo AND c.number=p.number
           WHERE p.repo=? AND p.landed IS NOT NULL AND (
             c.repo IS NULL OR c.head IS NOT p.head
             OR ((c.count IS NULL OR NOT EXISTS (SELECT 1 FROM pr_loc l
                    WHERE l.repo=p.repo AND l.number=p.number AND l.head=p.head))
                 AND c.checked<?))
           ORDER BY c.repo IS NOT NULL AND c.head IS p.head, c.checked, p.landed DESC LIMIT ?""",
        (key, now - RETRY, MEASURE_LIMIT),
    ).fetchall()
    for number, head in due:
        loc = count = None
        unavailable = False
        try:
            pull = _read(repo, f"/pulls/{number}", stop)
            head = pull["head"]["sha"] or head
            loc = lean_loc(pull, _pages(repo, f"/pulls/{number}/files", stop))
            # Forgejo returns all of an issue's comments at once; it ignores paging here.
            count = review_passes(_read(repo, f"/issues/{number}/comments", stop), head)
        except _Deadline:
            return
        except urllib.error.HTTPError:
            pass  # This PR's reads failed; retry it later without blocking the others.
        except OSError:
            unavailable = True
        except (ValueError, KeyError, TypeError, AttributeError):
            pass  # This PR's data is malformed; retry it later.
        db.execute("UPDATE prs SET head=? WHERE repo=? AND number=? AND head IS NOT ?", (head, key, number, head))
        if loc is not None:
            db.execute(
                "INSERT OR REPLACE INTO pr_loc VALUES (?,?,?,?,?,?)",
                (key, number, head, loc["added"], loc["deleted"], now),
            )
        db.execute(
            "INSERT OR REPLACE INTO pr_review_counts(repo,number,head,count,observed,checked,basis) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                key,
                number,
                head,
                count,
                now if count is not None else None,
                now,
                "scoreboard-round-counters" if count is not None else "unavailable",
            ),
        )
        if unavailable:
            errors.append("Forgejo PR measurements unavailable; normal refresh will retry")
            return
