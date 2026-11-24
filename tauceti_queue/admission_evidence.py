"""Admission evidence kept from webhook payloads, and its shadow comparison with the full read.

Decision 38 step 4 moves admission from "re-read everything from GitHub on any change" to
"evaluate the evidence the queue already holds". This module is the first part: an evidence cache
in the queue's SQLite and a shadow that, at each real evaluation, evaluates the cached evidence
with the same policy and records whether the two agree. It never changes a decision.

Rows come only from HMAC-verified webhook deliveries (`apply_event`) and from the admission
path's own authenticated full reads (`seed`); never from PR content. Three tables:

- `admission_evidence`: one row per PR: head, base sha and ref, state, draft, body, labels,
  author, GitHub's `updated_at`, its comment count, and whether a full read has seeded it.
- `admission_evidence_comments`: every comment the cache has seen, by id, with author login, id
  and App; the body only for comments admission reads (scoreboards, recovery requests). A deleted
  comment keeps a tombstone, so a late `edited` delivery cannot bring it back.
- `admission_evidence_statuses`: the newest status per (commit, context) for the contexts
  admission reads, with GitHub's status id and the sender.

Deliveries can arrive out of order. A PR row moves only forward in GitHub's `updated_at`, a
comment only forward in its own `updated_at`, a status only forward in its status id. A full read
replaces the cached evidence it covers, except rows a delivery wrote after the read began.

The merge base the shadow uses comes from the local clone (`git merge-base --all`, bounded in
time and never inside a store transaction), against the canonical tip of the newest `push`
delivery (by the repository's `pushed_at`), or the PR's cached base when none arrived, and must
be unique. Anything the
cache cannot vouch for (never seeded, a comment count that disagrees with GitHub's, an ambiguous
or uncomputable merge base) is recorded as such instead of as a verdict.

`admission_evidence_mode` (a runtime setting): `off` (default) writes and compares nothing;
`shadow` fills the cache and compares; `events` also evaluates from one GraphQL read
(`GitHubAdapter.admission_snapshot_single`) and lets a status delivery that changes nothing in
the cache leave the PR's evaluation alone.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from datetime import datetime

from .store import dumps, loads

log = logging.getLogger(__name__)

MODES = ("off", "shadow", "events")
CONTEXTS = ("build", "scope", "bump-guard")
ADMISSION_LABELS = frozenset({"needs-rebase", "self-tested", "full-lint", "lint-repair", "large-refactor"})
MISMATCH_LIMIT = 20
GIT_TIMEOUT_SECONDS = 30
# A status delivery for a (sha, context) a full read seeded carries no comparable status id; within
# this many seconds of the read, a different state is "order unknown" rather than newer or older.
STATUS_SKEW_SECONDS = 120


def _time(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def relevant_context(context, number=None) -> bool:
    context = str(context or "")
    return context in CONTEXTS or (
        context.startswith("self-tested/") if number is None else context == f"self-tested/{number}"
    )


class EvidenceCache:
    def __init__(self, store, repository, *, clock, clone, relevant_comment, relevant_labels, base_branch):
        self.store, self.repository, self.clock, self.clone = store, repository, clock, clone
        self.relevant_comment = relevant_comment
        self.relevant_labels = relevant_labels  # callable: the lower-case labels admission reads
        self.base_branch = base_branch
        self.tip_key = f"state:admission_evidence_tip:{repository}"

    def _key(self, number):
        return {"repository": self.repository, "number": number}

    # -- writes from deliveries

    def apply_event(self, kind, payload) -> bool:
        """Record what one verified delivery says. Never reads GitHub or git, never decides.

        False only for a status delivery that leaves the cached state of its (commit, context)
        exactly as it was, unambiguously: admission's evaluation already holds that state."""
        now = self.clock()
        with self.store.transaction():
            if kind == "pull_request":
                self._pull(payload.get("pull_request") or {}, now, payload.get("number"))
            elif kind == "issue_comment":
                issue = payload.get("issue") or {}
                if "pull_request" not in issue or type(issue.get("number")) is not int:
                    return
                self._comment(issue["number"], payload.get("comment") or {}, payload.get("action"), now)
                self._count(issue["number"], issue.get("comments"), _time(issue.get("updated_at")), now)
            elif kind == "status":
                return self._status(payload, now)
            elif kind == "push" and payload.get("ref") == f"refs/heads/{self.base_branch}":
                after = str(payload.get("after") or "")
                pushed = (payload.get("repository") or {}).get("pushed_at")
                pushed = pushed if type(pushed) in (int, float) else _time(pushed)
                current = self.store.get_setting(self.tip_key, {})
                if len(after) == 40 and pushed and pushed >= current.get("pushed_at", 0):
                    # Two pushes with one `pushed_at`: order unknown, so no push tip until the next.
                    tie = pushed == current.get("pushed_at") and after != current.get("sha")
                    value = {"sha": "" if tie else after, "ambiguous": tie, "pushed_at": pushed, "at": now}
                    self.store.set_setting(self.tip_key, value, "admission evidence", now)
        return True

    def _row(self, number):
        row = self.store.row("admission_evidence", self._key(number))
        if row is None:
            # A placeholder, not a delivery: it must not look newer than a read that seeds it.
            self.store.insert("admission_evidence", **self._key(number), received_at=0)
            row = self.store.row("admission_evidence", self._key(number))
        return row

    def _count(self, number, count, at, now, *, seeded_at=None):
        """GitHub's comment count, ordered by the issue's own `updated_at`, never rewound."""
        if type(count) is not int:
            return
        row = self._row(number)
        if at < row["count_updated_at"] or (seeded_at is not None and row["received_at"] > seeded_at):
            return
        if seeded_at is None and at == row["count_updated_at"] and count != row["comments_count"]:
            # Two counts with one timestamp: order unknown, so the PR needs a full read.
            self.store.update("admission_evidence", {"id": row["id"]}, seeded_at=0)
        self.store.update(
            "admission_evidence",
            {"id": row["id"]},
            comments_count=count,
            count_updated_at=at,
            received_at=now if seeded_at is None else max(row["received_at"], seeded_at),
        )

    def _pull(self, pull, now, number=None, *, seeded_at=None):
        number = pull.get("number") or number
        if type(number) is not int or number <= 0:
            return
        row = self._row(number)
        updated = _time(pull.get("updated_at"))
        if seeded_at is None and updated < row["pr_updated_at"]:
            return  # an older delivery than what the cache holds
        if seeded_at is not None and row["received_at"] > seeded_at:
            return  # a delivery after the read began is newer than the read; keep it
        head, base, user = pull.get("head") or {}, pull.get("base") or {}, pull.get("user") or {}
        labels = sorted(str(s.get("name") if isinstance(s, dict) else s) for s in pull.get("labels") or ())
        values = {
            "head_ref": str(head.get("ref") or ""),
            "head_repo": str((head.get("repo") or {}).get("full_name") or ""),
            "head_sha": str(head.get("sha") or ""),
            "base_sha": str(base.get("sha") or ""),
            "base_ref": str(base.get("ref") or ""),
            "state": str(pull.get("state") or ""),
            "draft": int(bool(pull.get("draft"))),
            "body": str(pull.get("body") or ""),
            "labels": dumps(labels),
            "author": str(user.get("login") or ""),
            "pr_updated_at": updated,
            "received_at": now if seeded_at is None else seeded_at,
        }
        if seeded_at is not None:
            values["seeded_at"], values["seeded_until"] = seeded_at, now
        elif updated == row["pr_updated_at"] and any(
            row[k] != values[k]
            for k in ("head_sha", "base_sha", "base_ref", "state", "draft", "body", "labels", "head_ref", "head_repo")
        ):
            values["seeded_at"] = 0  # two versions with one timestamp: order unknown, so fail closed
        self.store.update("admission_evidence", {"id": row["id"]}, **values)
        self._count(number, pull.get("comments"), updated, now, seeded_at=seeded_at)

    def _comment(self, number, comment, action, now, *, seeded_at=None):
        if type(comment.get("id")) is not int:
            return
        key = {**self._key(number), "comment_id": comment["id"]}
        row = self.store.row("admission_evidence_comments", key)
        received = now if seeded_at is None else seeded_at
        updated = _time(comment.get("updated_at") or comment.get("created_at"))
        if action == "deleted":
            if row is None:
                self.store.insert("admission_evidence_comments", **key, deleted=1, updated_at=updated, received_at=now)
            else:
                self.store.update("admission_evidence_comments", {"id": row["id"]}, deleted=1, received_at=now)
            return
        if row is not None and (row["deleted"] or updated < row["updated_at"]):
            return
        if seeded_at is not None and row is not None and row["received_at"] > seeded_at:
            return
        relevant = bool(self.relevant_comment(comment))
        body = str(comment.get("body") or "") if relevant else ""
        if (
            seeded_at is None
            and row is not None
            and updated == row["updated_at"]
            and (row["relevant"] or relevant)
            and (row["relevant"] != int(relevant) or row["body"] != body)
        ):
            # Two versions with one timestamp, at least one of them read by admission: order
            # unknown, so the PR needs a full read.
            self.store.update("admission_evidence", self._key(number), seeded_at=0)
        user = comment.get("user") or {}
        values = {
            "author": str(comment.get("author") or user.get("login") or ""),
            "author_id": int(user.get("id") or 0),
            "app_id": int((comment.get("performed_via_github_app") or {}).get("id") or 0),
            "relevant": int(relevant),
            "body": body,
            "created_at": str(comment.get("created_at") or ""),
            "updated_text": str(comment.get("updated_at") or ""),
            "html_url": str(comment.get("html_url") or ""),
            "updated_at": updated,
            "received_at": received,
        }
        if row is None:
            self.store.insert("admission_evidence_comments", **key, **values)
        else:
            self.store.update("admission_evidence_comments", {"id": row["id"]}, **values)

    def _status(self, payload, now):
        sha, context, status_id = str(payload.get("sha") or ""), payload.get("context"), payload.get("id")
        if len(sha) != 40 or not relevant_context(context) or type(status_id) is not int:
            return True
        key = {"repository": self.repository, "sha": sha, "context": context}
        row = self.store.row("admission_evidence_statuses", key)
        state = str(payload.get("state") or "").lower()
        values = {
            "state": state,
            "status_id": status_id,
            "sender": str((payload.get("sender") or {}).get("login") or ""),
            "received_at": now,
            "ambiguous": 0,
        }
        if row is None:
            self.store.insert("admission_evidence_statuses", **key, **values)
            return True
        # "Unchanged" means more than equal: the cached state must be the one the last full read
        # itself installed (no delivery since), so the evaluation that read it saw exactly this.
        unchanged = not row["ambiguous"] and row["state"] == state and row["status_id"] == 0 and row["seeded_at"] > 0
        if row["status_id"] > 0:
            if row["status_id"] < status_id:
                self.store.update("admission_evidence_statuses", {"id": row["id"]}, **values)
            return True
        # The row came from a full read, which gives no status id: order by creation time instead.
        # Anything created while the read ran (with a margin for clock skew) may or may not be in it.
        created = _time(payload.get("updated_at") or payload.get("created_at"))
        if created > max(row["seeded_until"], row["seeded_at"]) + STATUS_SKEW_SECONDS:
            self.store.update("admission_evidence_statuses", {"id": row["id"]}, **values)
        elif created >= row["seeded_at"] - STATUS_SKEW_SECONDS and state != row["state"]:
            self.store.update("admission_evidence_statuses", {"id": row["id"]}, ambiguous=1, received_at=now)
            return True
        return not unchanged

    # -- writes from full reads

    def seed(self, snapshot, started) -> None:
        """Install an authoritative full read of one PR that began at `started`, keeping anything a
        delivery wrote after that moment."""
        pull = snapshot.pull
        now = self.clock()
        record = {
            "number": pull.number,
            "head": {
                "sha": pull.head_sha,
                "ref": getattr(pull, "head_ref", ""),
                "repo": {"full_name": getattr(pull, "head_repo", "")},
            },
            "base": {"sha": getattr(pull, "base_sha", ""), "ref": pull.base_ref},
            "state": pull.state,
            "draft": pull.draft,
            "body": pull.body,
            "labels": list(pull.labels),
            "user": {"login": pull.author_login},
            "updated_at": getattr(pull, "updated_at", ""),
            "comments": getattr(pull, "comments_count", None),
        }
        with self.store.transaction():
            self._pull(record, now, seeded_at=started)
            if pull.state != "open" or pull.draft:
                return  # a closed or draft read carries no comments or statuses
            seen = set()
            for comment in snapshot.comments:
                if type(comment.get("id")) is int:
                    seen.add(comment["id"])
                    self._comment(pull.number, comment, "seeded", now, seeded_at=started)
            for row in self.store.rows("admission_evidence_comments", {**self._key(pull.number), "deleted": 0}):
                if row["comment_id"] not in seen and row["received_at"] <= started:
                    self.store.update("admission_evidence_comments", {"id": row["id"]}, deleted=1, received_at=started)
            for context, state in snapshot.statuses.items():
                key = {"repository": self.repository, "sha": pull.head_sha, "context": context}
                values = {
                    "state": state,
                    "status_id": 0,
                    "seeded_at": started,
                    "seeded_until": now,
                    "received_at": started,
                    "ambiguous": 0,
                }
                row = self.store.row("admission_evidence_statuses", key)
                if row is None:
                    self.store.insert("admission_evidence_statuses", **key, **values)
                elif row["received_at"] <= started:
                    self.store.update("admission_evidence_statuses", {"id": row["id"]}, **values)

    # -- reads

    def _git(self, *args):
        """A bounded, read-only git question; None when it cannot be answered."""
        if not self.clone:
            return None
        try:
            result = subprocess.run(
                ["git", "-C", self.clone, *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    def view(self, number):
        """The cached evidence for one PR, read in one transaction, or why the cache cannot vouch for it."""
        with self.store.transaction():
            row = self.store.row("admission_evidence", self._key(number))
            if row is None or not row["seeded_at"]:
                return {"status": "unseeded"}
            comments = self.store.rows("admission_evidence_comments", self._key(number), order="comment_id")
            statuses = [
                s
                for s in self.store.rows(
                    "admission_evidence_statuses", {"repository": self.repository, "sha": row["head_sha"]}
                )
                if relevant_context(s["context"], number)
            ]
            tip = self.store.get_setting(self.tip_key, {})
        live = [c for c in comments if not c["deleted"]]
        received = max([row["received_at"], tip.get("at", 0), *(r["received_at"] for r in comments + statuses)])
        if row["state"] == "open" and not row["draft"]:
            if row["comments_count"] != len(live):
                detail = f"{len(live)} cached comments, GitHub counts {row['comments_count']}"
                return {"status": "gap", "detail": detail, "received_at": received}
            if any(s["ambiguous"] for s in statuses):
                return {"status": "status-order-unknown", "received_at": received}
        return {
            "status": "seeded",
            "row": row,
            "tip": self._tip(tip, row),
            "labels": tuple(loads(row["labels"], [])),
            "comment_ids": tuple(c["comment_id"] for c in live),
            "comments": tuple(
                {
                    "id": c["comment_id"],
                    "body": c["body"],
                    "created_at": c["created_at"],
                    "updated_at": c["updated_text"],
                    "html_url": c["html_url"],
                    "user": {"login": c["author"], "id": c["author_id"]},
                }
                for c in live
                if c["relevant"]
            ),
            "statuses": {s["context"]: s["state"] for s in statuses},
            "received_at": received,
        }

    @staticmethod
    def _tip(tip, row):
        """The canonical tip to measure against: a push delivered after the PR's last full read
        ended is newer than that read's base; one delivered during the read, or two pushes with
        one timestamp, leave the order unknown (None); otherwise the read's base holds."""
        at = tip.get("at", 0)
        if not tip or at < row["seeded_at"]:
            return row["base_sha"]
        if at <= max(row["seeded_until"], row["seeded_at"]) or tip.get("ambiguous") or not tip.get("sha"):
            return None
        return tip["sha"]

    def merge_base(self, tip, head):
        """The unique local merge base of `head` with `tip`, or None."""
        if not tip or not head:
            return None
        bases = (self._git("merge-base", "--all", tip, head) or "").split()
        return bases[0] if len(bases) == 1 else None

    def paths(self, base, head):
        raw = self._git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head, "--")
        return None if raw is None else tuple(p for p in raw.split("\0") if p)


class Shadow:
    """Evaluate the cached evidence beside each real evaluation and record the comparison."""

    def __init__(self, cache, policy, *, journal, blocking_labels):
        self.cache, self.policy = cache, policy
        self.journal = journal
        self.blocking_labels = blocking_labels  # callable(pull): labels that make `approve` refuse
        self.key = f"state:admission_shadow:{cache.repository}"

    def compare(self, snapshot, result, started, authorization="") -> dict:
        """Compare the cache as it stood with a completed full read, then seed the cache from the read."""
        outcome = self._outcome(snapshot, result, started, authorization)
        self._record(snapshot.pull.number, snapshot.pull.head_sha, outcome)
        self.cache.seed(snapshot, started)
        return outcome

    def _admits(self, verdict, pull):
        return bool(verdict.get("merge") and pull.base_ref == self.cache.base_branch and not self.blocking_labels(pull))

    def _outcome(self, snapshot, result, started, authorization):
        pull = snapshot.pull
        view = self.cache.view(pull.number)
        raced = view.get("received_at", 0) > started
        if view["status"] != "seeded":
            return {"outcome": view["status"], "detail": view.get("detail", ""), "raced": raced}
        row = view["row"]
        relevant = self.cache.relevant_labels()
        cached = {
            "head": row["head_sha"],
            "head_ref": row["head_ref"],
            "head_repo": row["head_repo"],
            "author": row["author"],
            "base_ref": row["base_ref"],
            "state": row["state"],
            "draft": bool(row["draft"]),
            "body": row["body"],
            "labels": sorted({s.lower() for s in view["labels"]} & relevant),
        }
        real = {
            "head": pull.head_sha,
            "head_ref": getattr(pull, "head_ref", ""),
            "head_repo": getattr(pull, "head_repo", ""),
            "author": pull.author_login,
            "base_ref": pull.base_ref,
            "state": pull.state,
            "draft": bool(pull.draft),
            "body": pull.body,
            "labels": sorted({s.lower() for s in pull.labels} & relevant),
        }
        differ = sorted(k for k in cached if cached[k] != real[k])

        def settle(**extra):
            outcome = "match" if not differ else ("race" if raced else "mismatch")
            return {"outcome": outcome, "fields": differ, **extra}

        if pull.state != "open" or pull.draft:
            return settle()
        if sorted(view["comment_ids"]) != sorted(c["id"] for c in snapshot.comments if type(c.get("id")) is int):
            differ.append("comment_ids")

        def comments(rows):
            return sorted(
                (
                    c["id"],
                    c.get("body") or "",
                    c.get("updated_at") or "",
                    c.get("author") or (c.get("user") or {}).get("login"),
                )
                for c in rows
                if self.cache.relevant_comment(c)
            )

        if comments(view["comments"]) != comments(snapshot.comments):
            differ.append("comments")
        if view["statuses"] != snapshot.statuses:
            differ.append("statuses")
        merge_base = self.cache.merge_base(view["tip"], row["head_sha"])
        if merge_base is None:
            # Not computable here; any difference already found is still recorded below.
            return {"outcome": "merge-base-unknown", "fields": differ, "raced": raced}
        if merge_base != snapshot.merge_base_sha:
            differ.append("merge_base")
        paths = self.cache.paths(merge_base, row["head_sha"])
        if paths is None:
            return {"outcome": "paths-unknown", "fields": differ, "raced": raced}
        if sorted(paths) != sorted(snapshot.paths):
            differ.append("paths")
        if set(differ) & {"head", "head_ref", "head_repo", "author", "labels", "paths", "base_ref", "merge_base"}:
            # Self-tested eligibility and the diff guards were measured only on the real inputs.
            return settle(self_tested="unavailable")
        shadow_pull = dataclasses.replace(pull, body=row["body"], labels=view["labels"])
        shadow_snapshot = dataclasses.replace(
            snapshot,
            pull=shadow_pull,
            merge_base_sha=merge_base,
            paths=paths,
            comments=view["comments"],
            statuses=view["statuses"],
        )
        verdict, cached_authorization, *_ = self.policy.evaluate(shadow_snapshot)
        keys = ("merge", "review_safe", "reason")
        if any(verdict.get(k) != result.get(k) for k in keys):
            differ.append("verdict")
        if cached_authorization != authorization:
            differ.append("authorization")  # the scoreboard that would be recorded as approval intent
        extra = {}
        if "verdict" in differ:
            extra["shadow"] = {k: verdict.get(k) for k in keys}
        cache_admits, real_admits = self._admits(verdict, shadow_pull), self._admits(result, pull)
        if cache_admits != real_admits:
            extra["direction"] = "cache-would-admit" if cache_admits else "cache-would-refuse"
        elif bool(verdict.get("review_safe")) != bool(result.get("review_safe")):
            extra["direction"] = "cache-keeps" if verdict.get("review_safe") else "cache-revokes"
        return settle(**extra)

    def _record(self, number, head, outcome):
        store = self.cache.store
        now = self.cache.clock()
        with store.transaction():
            summary = store.get_setting(self.key, {})
            counts = summary.setdefault("counts", {})
            counts[outcome["outcome"]] = counts.get(outcome["outcome"], 0) + 1
            summary["compared"] = summary.get("compared", 0) + 1
            if outcome.get("fields"):
                # Durable, whatever the outcome: a difference is never hidden by an unknown verdict.
                summary["with_differences"] = summary.get("with_differences", 0) + 1
            if outcome.get("direction"):
                directions = summary.setdefault("directions", {})
                directions[outcome["direction"]] = directions.get(outcome["direction"], 0) + 1
            summary["since"] = summary.get("since") or now
            summary["at"] = now
            if outcome["outcome"] != "match":
                # Races are kept beside disagreements: a race is classified, not excused.
                recent = summary.setdefault("recent", [])
                recent.append({"at": now, "number": number, "head": head, **outcome})
                del recent[:-MISMATCH_LIMIT]
                if outcome.get("fields") and outcome["outcome"] != "race":
                    self.journal(now, "admission-shadow", number=number, head=head, **outcome)
            store.set_setting(self.key, summary, "admission shadow", now)

    def summary(self):
        rows = self.cache.store.query(
            "SELECT COUNT(*) AS rows, SUM(seeded_at>0) AS seeded FROM admission_evidence WHERE repository=?",
            (self.cache.repository,),
        )[0]
        return {**self.cache.store.get_setting(self.key, {}), "rows": rows["rows"], "seeded": rows["seeded"] or 0}
