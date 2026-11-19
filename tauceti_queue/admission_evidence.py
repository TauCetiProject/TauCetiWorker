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

The merge base the shadow uses comes from the local clone (`git merge-base --all`), against the
newest canonical tip a `push` delivery or full read reported, and must be unique. Anything the
cache cannot vouch for (never seeded, a comment count that disagrees with GitHub's, an ambiguous
or uncomputable merge base) is recorded as such instead of as a verdict.

`admission_evidence_mode` (a runtime setting): `off` (default) writes and compares nothing;
`shadow` fills the cache and compares. Later slices add a mode that acts on the cache.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime

from . import chain
from .store import dumps, loads

log = logging.getLogger(__name__)

MODES = ("off", "shadow")
CONTEXTS = ("build", "scope", "bump-guard")
ADMISSION_LABELS = frozenset({"needs-rebase", "self-tested", "full-lint", "lint-repair"})
MISMATCH_LIMIT = 20


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

    # -- writes from deliveries

    def apply_event(self, kind, payload) -> None:
        """Record what one verified delivery says. Never reads GitHub, never decides."""
        now = self.clock()
        with self.store.transaction():
            if kind == "pull_request":
                self._pull(payload.get("pull_request") or {}, now, payload.get("number"))
            elif kind == "issue_comment":
                issue = payload.get("issue") or {}
                if "pull_request" not in issue or not isinstance(issue.get("number"), int):
                    return
                self._comment(issue["number"], payload.get("comment") or {}, payload.get("action"), now)
                if isinstance(issue.get("comments"), int):
                    self._ensure(issue["number"], now)
                    self.store.update(
                        "admission_evidence",
                        {"repository": self.repository, "number": issue["number"]},
                        comments_count=issue["comments"],
                        received_at=now,
                    )
            elif kind == "status":
                self._status(payload, now)
            elif kind == "push" and payload.get("ref") == f"refs/heads/{self.base_branch}":
                after = str(payload.get("after") or "")
                if len(after) == 40:
                    self.store.set_setting(self.tip_key, {"sha": after, "at": now}, "admission evidence", now)

    def _ensure(self, number, now):
        if self.store.row("admission_evidence", {"repository": self.repository, "number": number}) is None:
            self.store.insert("admission_evidence", repository=self.repository, number=number, received_at=now)

    def _pull(self, pull, now, number=None, *, seeded_at=None):
        number = pull.get("number") or number
        if type(number) is not int or number <= 0:
            return
        self._ensure(number, now)
        row = self.store.row("admission_evidence", {"repository": self.repository, "number": number})
        updated = _time(pull.get("updated_at"))
        if seeded_at is None and row["pr_updated_at"] and updated < row["pr_updated_at"]:
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
            "received_at": now,
        }
        if isinstance(pull.get("comments"), int):
            values["comments_count"] = pull["comments"]
        if seeded_at is not None:
            values["seeded_at"] = seeded_at
        elif updated == row["pr_updated_at"] and any(
            row[k] != values[k] for k in ("head_sha", "base_sha", "base_ref", "state", "draft", "body", "labels")
        ):
            values["seeded_at"] = 0  # two versions with one timestamp: order unknown, so fail closed
        self.store.update("admission_evidence", {"id": row["id"]}, **values)
        if values["base_ref"] == self.base_branch and len(values["base_sha"]) == 40:
            self._tip(values["base_sha"], now)

    def _tip(self, sha, now):
        current = self.store.get_setting(self.tip_key, {})
        if not current.get("sha") or (
            current["sha"] != sha and self._git("merge-base", "--is-ancestor", current["sha"], sha) is not None
        ):
            self.store.set_setting(self.tip_key, {"sha": sha, "at": now}, "admission evidence", now)

    def _comment(self, number, comment, action, now, *, seeded_at=None):
        if type(comment.get("id")) is not int:
            return
        key = {"repository": self.repository, "number": number, "comment_id": comment["id"]}
        row = self.store.row("admission_evidence_comments", key)
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
        if (
            seeded_at is None
            and row is not None
            and updated == row["updated_at"]
            and row["relevant"]
            and row["body"] != str(comment.get("body") or "")
        ):
            # Two bodies with one timestamp: order unknown, so the PR needs a full read.
            self.store.update("admission_evidence", {"repository": self.repository, "number": number}, seeded_at=0)
        user = comment.get("user") or {}
        author = comment.get("author") or user.get("login") or ""
        relevant = bool(self.relevant_comment(comment))
        values = {
            "author": str(author),
            "author_id": int(user.get("id") or 0),
            "app_id": int((comment.get("performed_via_github_app") or {}).get("id") or 0),
            "relevant": int(relevant),
            "body": str(comment.get("body") or "") if relevant else "",
            "created_at": str(comment.get("created_at") or ""),
            "updated_text": str(comment.get("updated_at") or ""),
            "html_url": str(comment.get("html_url") or ""),
            "updated_at": updated,
            "received_at": now,
        }
        if row is None:
            self.store.insert("admission_evidence_comments", **key, **values)
        else:
            self.store.update("admission_evidence_comments", {"id": row["id"]}, **values)

    def _status(self, payload, now):
        sha, context, status_id = str(payload.get("sha") or ""), payload.get("context"), payload.get("id")
        if len(sha) != 40 or not relevant_context(context) or type(status_id) is not int:
            return
        key = {"repository": self.repository, "sha": sha, "context": context}
        row = self.store.row("admission_evidence_statuses", key)
        if row is not None and row["status_id"] >= status_id:
            return
        values = {
            "state": str(payload.get("state") or "").lower(),
            "status_id": status_id,
            "sender": str((payload.get("sender") or {}).get("login") or ""),
            "received_at": now,
        }
        if row is None:
            self.store.insert("admission_evidence_statuses", **key, **values)
        else:
            self.store.update("admission_evidence_statuses", {"id": row["id"]}, **values)

    # -- writes from full reads

    def seed(self, snapshot, started) -> None:
        """Replace the cached evidence for one PR with an authoritative full read that began at
        `started`, keeping anything a delivery wrote after that moment."""
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
            for row in self.store.rows(
                "admission_evidence_comments", {"repository": self.repository, "number": pull.number, "deleted": 0}
            ):
                if row["comment_id"] not in seen and row["received_at"] <= started:
                    self.store.update("admission_evidence_comments", {"id": row["id"]}, deleted=1, received_at=now)
            for context, state in snapshot.statuses.items():
                key = {"repository": self.repository, "sha": pull.head_sha, "context": context}
                row = self.store.row("admission_evidence_statuses", key)
                if row is None:
                    self.store.insert("admission_evidence_statuses", **key, state=state, status_id=0, received_at=now)
                elif row["received_at"] <= started:
                    self.store.update("admission_evidence_statuses", {"id": row["id"]}, state=state, received_at=now)

    # -- reads

    def _git(self, *args):
        if not self.clone:
            return None
        try:
            return chain.git(self.clone, *args)
        except Exception:  # noqa: BLE001 - an unanswerable git question is "unknown"
            return None

    def view(self, number):
        """The cached evidence for one PR, or why the cache cannot vouch for it."""
        row = self.store.row("admission_evidence", {"repository": self.repository, "number": number})
        if row is None or not row["seeded_at"]:
            return {"status": "unseeded"}
        comments = self.store.rows(
            "admission_evidence_comments",
            {"repository": self.repository, "number": number},
            order="comment_id",
        )
        live = [c for c in comments if not c["deleted"]]
        if row["state"] == "open" and not row["draft"] and row["comments_count"] != len(live):
            return {"status": "gap", "detail": f"{len(live)} cached comments, GitHub counts {row['comments_count']}"}
        statuses = {
            s["context"]: s["state"]
            for s in self.store.rows(
                "admission_evidence_statuses", {"repository": self.repository, "sha": row["head_sha"]}
            )
            if relevant_context(s["context"], number)
        }
        return {
            "status": "seeded",
            "row": row,
            "labels": tuple(loads(row["labels"], [])),
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
            "statuses": statuses,
            "received_at": max([row["received_at"], *(c["received_at"] for c in comments)]),
        }

    def merge_base(self, head, base_sha):
        """The unique local merge base of `head` with the newest known canonical tip, or None."""
        tip = self.store.get_setting(self.tip_key, {}).get("sha") or base_sha
        if not tip:
            return None, tip
        bases = (self._git("merge-base", "--all", tip, head) or "").split()
        return (bases[0] if len(bases) == 1 else None), tip

    def paths(self, base, head):
        raw = self._git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head, "--")
        return None if raw is None else tuple(p for p in raw.split("\0") if p)


class Shadow:
    """Evaluate the cached evidence beside each real evaluation and record the comparison."""

    def __init__(self, cache, policy, *, journal):
        self.cache, self.policy = cache, policy
        self.journal = journal
        self.key = f"state:admission_shadow:{cache.repository}"

    def compare(self, snapshot, result, started, authorization="") -> dict:
        """Compare cached evidence with a completed full read, then seed the cache from the read."""
        outcome = self._outcome(snapshot, result, started, authorization)
        self._record(snapshot.pull.number, snapshot.pull.head_sha, outcome)
        self.cache.seed(snapshot, started)
        return outcome

    def _outcome(self, snapshot, result, started, authorization):
        pull = snapshot.pull
        view = self.cache.view(pull.number)
        if view["status"] != "seeded":
            return {"outcome": view["status"], "detail": view.get("detail", "")}
        row = view["row"]
        raced = view["received_at"] > started
        relevant = self.cache.relevant_labels()
        cached = {
            "head": row["head_sha"],
            "base_ref": row["base_ref"],
            "state": row["state"],
            "draft": bool(row["draft"]),
            "body": row["body"],
            "labels": sorted({s.lower() for s in view["labels"]} & relevant),
        }
        real = {
            "head": pull.head_sha,
            "base_ref": pull.base_ref,
            "state": pull.state,
            "draft": bool(pull.draft),
            "body": pull.body,
            "labels": sorted({s.lower() for s in pull.labels} & relevant),
        }
        differ = sorted(k for k in cached if cached[k] != real[k])
        if pull.state != "open" or pull.draft:
            return {"outcome": ("race" if raced else "mismatch") if differ else "match", "fields": differ}
        merge_base, tip = self.cache.merge_base(row["head_sha"], row["base_sha"])
        if merge_base is None:
            return {"outcome": "merge-base-unknown", "fields": differ, "tip": tip}
        if merge_base != snapshot.merge_base_sha:
            differ.append("merge_base")
        paths = self.cache.paths(merge_base, row["head_sha"])
        if paths is None:
            return {"outcome": "paths-unknown", "fields": differ}
        if sorted(paths) != sorted(snapshot.paths):
            differ.append("paths")

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
        if differ and any(k in differ for k in ("head", "paths")):
            # Self-tested eligibility and diff guards were measured on the real head and paths only.
            return {"outcome": "race" if raced else "mismatch", "fields": differ}
        shadow_snapshot = dataclasses.replace(
            snapshot,
            pull=dataclasses.replace(
                pull,
                head_sha=row["head_sha"],
                base_ref=row["base_ref"],
                body=row["body"],
                labels=view["labels"],
            ),
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
        outcome = "match" if not differ else ("race" if raced else "mismatch")
        detail = {"outcome": outcome, "fields": differ}
        if "verdict" in differ:
            detail["shadow"] = {k: verdict.get(k) for k in keys}
            if bool(verdict.get("merge")) != bool(result.get("merge")):
                detail["direction"] = "cache-would-admit" if verdict.get("merge") else "cache-would-refuse"
            elif bool(verdict.get("review_safe")) != bool(result.get("review_safe")):
                detail["direction"] = "cache-keeps" if verdict.get("review_safe") else "cache-revokes"
        return detail

    def _record(self, number, head, outcome):
        store = self.cache.store
        now = self.cache.clock()
        with store.transaction():
            summary = store.get_setting(self.key, {})
            counts = summary.setdefault("counts", {})
            counts[outcome["outcome"]] = counts.get(outcome["outcome"], 0) + 1
            if outcome.get("direction"):
                directions = summary.setdefault("directions", {})
                directions[outcome["direction"]] = directions.get(outcome["direction"], 0) + 1
            summary["compared"] = summary.get("compared", 0) + 1
            summary["since"] = summary.get("since") or now
            summary["at"] = now
            if outcome["outcome"] != "match":
                # Races are kept beside disagreements: a race is classified, not excused.
                recent = summary.setdefault("recent", [])
                recent.append({"at": now, "number": number, "head": head, **outcome})
                del recent[:-MISMATCH_LIMIT]
                if outcome["outcome"] == "mismatch":
                    self.journal(now, "admission-shadow", number=number, head=head, **outcome)
            store.set_setting(self.key, summary, "admission shadow", now)

    def summary(self):
        rows = self.cache.store.query(
            "SELECT COUNT(*) AS rows, SUM(seeded_at>0) AS seeded FROM admission_evidence WHERE repository=?",
            (self.cache.repository,),
        )[0]
        return {**self.cache.store.get_setting(self.key, {}), "rows": rows["rows"], "seeded": rows["seeded"] or 0}
