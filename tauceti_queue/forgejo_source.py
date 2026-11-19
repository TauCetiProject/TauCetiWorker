"""Direct Forgejo source operations for the queue.

Primary admission uses the installed shared review policy exactly as on GitHub:
the newest completed scoreboard for the current head and merge base counts,
whoever posted it. GitHub execution and App check-run approvals are separate
capabilities and never inferred here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Incomplete, PrincipalRef, Unsupported

from tauceti_worker.source_identity import SourceActor

from . import diff_guard, ports
from .admission import AdmissionPolicy, digest, policy, relevant_comment
from .eligibility import KEEP
from .forgejo_git import ForgejoGitSource
from .github_adapter import comment_marker
from .task_identity import NATIVE_GIT_REFS


def _forge_time(value):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


@dataclass(frozen=True)
class NativePublishers:
    """The queue's own source accounts, whose recovery holds count like GitHub's bot logins."""

    source: RepositoryIdentity
    recoveries: frozenset[PrincipalRef] = frozenset()

    def __post_init__(self):
        if not isinstance(self.source, RepositoryIdentity) or self.source.repository.forge.kind != "forgejo":
            raise ValueError("native publisher policy requires a qualified source")
        if (
            type(self.recoveries) is not frozenset
            or len(self.recoveries) > 256
            or any(
                not isinstance(p, PrincipalRef)
                or p.forge != self.source.repository.forge
                or re.fullmatch(r"[1-9][0-9]*", p.native_id) is None
                for p in self.recoveries
            )
        ):
            raise ValueError("native publishers require bounded source-local principal IDs")

    def record(self):
        return {"source": self.source.record(), "recoveries": sorted(int(p.native_id) for p in self.recoveries)}


class NativeAdmissionPolicy(AdmissionPolicy):
    human_owned_approvals = False

    def __init__(self, publishers, **kwargs):
        if not isinstance(publishers, NativePublishers):
            raise ValueError("native admission requires explicit publisher identities")
        # Each forge numbers its own PRs, so a rubric being phased in has a cutoff for this source.
        kwargs.setdefault("repository", publishers.source.repository.key)
        super().__init__(**kwargs)
        self.publishers = publishers
        self.revision = digest({"review_policy": self.revision, "publishers": publishers.record()})

    def evaluate(self, snapshot):
        result, authorization, evidence, comment, meta = super().evaluate(snapshot)
        # GitHub trusts recovery holds from its bot logins; here, from the queue's own source accounts.
        recoveries = {int(p.native_id) for p in self.publishers.recoveries}
        heads = set()
        for row in snapshot.comments:
            author = (row.get("user") or {}).get("id")
            body = row.get("body") or ""
            if type(author) is int and author in recoveries and body.startswith("Merge-queue recovery for head `"):
                heads.update(policy.REBASE_MARKER_RE.findall(body))
                heads.update(policy.STALLED_MARKER_RE.findall(body))
        result["recovery_heads"] = sorted(heads)
        if snapshot.pull.head_sha in heads:
            result.update(merge=False, reason="waiting on a trusted native recovery request for this head")
        evidence.update(source_repository=self.publishers.source.record())
        return result, authorization, evidence, comment, meta


@dataclass(frozen=True)
class NativePullRequest(ports.PullRequest):
    base_sha: str = ""
    author_id: int = 0
    head_repo: str = ""
    merged: bool = False
    updated_at: str = ""
    comments_count: int = 0


class NativePullNotFound(LookupError):
    pass


class ForgejoSourceAdapter(ForgejoGitSource):
    task_git_transport = NATIVE_GIT_REFS

    def __init__(self, identity, client, clone, *, token, admission_policy, timeout=120, clock=time.time):
        super().__init__(identity, client, clone, token=token, timeout=timeout)
        if not isinstance(admission_policy, NativeAdmissionPolicy) or admission_policy.publishers.source != identity:
            raise ValueError("native source and admission policy differ")
        self.admission_policy = admission_policy
        self.clone_dir = self.clone
        self.clock, self.refresh_seconds = clock, 900

    def configure_admission_polling(self, *, clock, refresh_seconds):
        self.clock, self.refresh_seconds = clock, max(1, int(refresh_seconds))

    @staticmethod
    def _positive(value):
        if type(value) is not int or value < 1:
            raise Incomplete("native record has no positive ID")
        return value

    def _pull(self, row, number=None):
        if not isinstance(row, dict):
            raise Incomplete("native pull metadata is unavailable")
        n = self._positive(row.get("number"))
        if number is not None and n != number:
            raise Incomplete("native source returned another pull request")
        head, base = row.get("head"), row.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise Incomplete("native pull endpoints are unavailable")
        self._sha(head.get("sha"))
        self._sha(base.get("sha"))
        repository = base.get("repo")
        if (
            not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository["id"] != self.repository_identity.native_id
        ):
            raise Incomplete("native pull belongs to another repository")
        actor = SourceActor.from_native(self.repository.forge, row.get("user"))
        if (
            row.get("state") not in {"open", "closed"}
            or type(row.get("draft")) is not bool
            or type(row.get("merged")) is not bool
            or not isinstance(row.get("labels"), list)
            or not isinstance(base.get("ref"), str)
            or not base["ref"]
        ):
            raise Incomplete("native pull state or labels are incomplete")
        labels = []
        for label in row["labels"]:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise Incomplete("native pull label is incomplete")
            labels.append(label["name"])
        if (
            any(not isinstance(row.get(k), str) for k in ("title", "updated_at"))
            or row.get("body") is not None
            and not isinstance(row["body"], str)
        ):
            raise Incomplete("native pull content is incomplete")
        if type(row.get("comments")) is not int or row["comments"] < 0:
            raise Incomplete("native pull comment count is unavailable")
        return NativePullRequest(
            n,
            head["sha"],
            base["ref"],
            row["title"],
            row.get("body") or "",
            actor.login,
            row["state"],
            row["draft"],
            tuple(labels),
            base["sha"],
            int(actor.principal.native_id),
            (head.get("repo") or {}).get("full_name", ""),
            row["merged"],
            row["updated_at"],
            row["comments"],
        )

    def fetch_pr(self, repo, number):
        self.repository.pull_url(number)
        self._bound(repo)
        try:
            row = self.client.pull(self.repository, number)
        except ForgeError as error:
            if error.status == 404:
                raise NativePullNotFound("native pull request is absent") from None
            raise
        pull = self._pull(row, number)
        self._bound(repo)
        return pull

    def fetch_pr_or_none(self, repo, number):
        try:
            return self.fetch_pr(repo, number)
        except NativePullNotFound:
            return None

    def _permission(self, actor):
        row = self.client.request(
            "GET", f"repos/{self.repository.name}/collaborators/{quote(actor.login, safe='')}/permission"
        )
        if not isinstance(row, dict) or SourceActor.from_native(self.repository.forge, row.get("user")) != actor:
            raise Incomplete("publisher permission belongs to another native actor")
        permission = row.get("permission")
        if permission not in {"none", "read", "write", "admin", "owner"}:
            raise Incomplete("native actor permission is unavailable")
        return permission

    def repository_admin(self, login, repo=None, *, actor_id=None):
        if type(actor_id) is not int or actor_id < 1:
            raise ValueError("native admin lookup requires an authenticated actor ID")
        self._bound(repo or self.repository.name)
        actor = SourceActor.from_native(self.repository.forge, {"id": actor_id, "login": login})
        try:
            permission = self._permission(actor)
        except ForgeError as error:
            if error.status == 404:
                return False
            raise
        self._bound(repo or self.repository.name)
        return permission in {"admin", "owner"}

    def _statuses(self, head, rows=None):
        """Newest status per context, counted only if its creator can write here now.

        Forgejo, like GitHub, accepts status POSTs only from repository writers.
        Forgejo Actions also posts statuses for any workflow a PR brings, as its
        system user, under a context the workflow names; those never count.
        """
        rows = self.client.statuses(self.repository, head) if rows is None else rows
        newest = {}
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("native statuses are incomplete")
            context = row.get("context")
            if context not in {"build", "scope", "bump-guard"} and not (
                isinstance(context, str) and context.startswith("self-tested/")
            ):
                continue
            status_id = self._positive(row.get("id"))
            if status_id > newest.get(context, (0, None))[0]:
                newest[context] = (status_id, row)
        permissions = {}
        result = {}
        for context, (_id, row) in newest.items():
            try:
                actor = SourceActor.from_native(self.repository.forge, row.get("creator"))
            except Incomplete:
                result[context] = "error"
                continue
            if actor.principal not in permissions:
                try:
                    permissions[actor.principal] = self._permission(actor) in {"write", "admin", "owner"}
                except ForgeError as error:
                    if error.status != 404:
                        raise
                    permissions[actor.principal] = False
            state = row.get("status") or row.get("state")
            valid = permissions[actor.principal] and state in {"pending", "success", "error", "failure", "warning"}
            result[context] = state if valid else "error"
        return result

    def _merge_base(self, repo, pull):
        missing = [
            sha
            for sha in (pull.head_sha, pull.base_sha)
            if self._git("cat-file", "-e", sha + "^{commit}", check=False).returncode
        ]
        if missing:
            self.fetch_refs(repo, missing)
        bases = self._git("merge-base", "--all", pull.base_sha, pull.head_sha).stdout.decode("ascii").splitlines()
        if len(bases) != 1:
            raise Incomplete("native pull comparison has no unique merge base")
        return self._sha(bases[0])

    def pr_merge_base(self, repo, number, head_sha):
        pull = self.fetch_pr_or_none(repo, number)
        if pull is None or pull.head_sha != head_sha:
            return None
        return self._merge_base(repo, pull)

    def admission_snapshot(self, repo, number, *, generation=0):
        pull = self.fetch_pr(repo, number)
        if pull.state != "open" or pull.draft:
            return ports.AdmissionSnapshot(pull, "", (), (), {}, discovery_version=self._discovery_version(pull))
        base = self._merge_base(repo, pull)
        with self._lock:
            raw = self._git(
                "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, pull.head_sha, "--"
            ).stdout
            paths = tuple(p.decode("utf-8") for p in raw.split(b"\0") if p)
            # Native PRs have no PR-level CI diff guards, so admission runs them for every head.
            guard = diff_guard.violations(self.clone_dir, base, pull.head_sha)
        comments = self.client.comments(self.repository, number)
        if not isinstance(comments, list) or not all(isinstance(row, dict) for row in comments):
            raise Incomplete("native discussion is unavailable")
        comments = tuple(row for row in comments if relevant_comment(row, native=True))
        statuses = self._statuses(pull.head_sha)
        fresh = self.fetch_pr(repo, number)
        # Ordinary discussion/title timestamps cannot invalidate a complete
        # source read; head/base/state/labels/body still fence this observation.
        fields = ("head_sha", "base_ref", "state", "draft", "labels", "body", "base_sha", "merged")
        if any(getattr(fresh, key, None) != getattr(pull, key, None) for key in fields):
            raise Incomplete("native pull changed while reading admission evidence")
        version = base + ":" + digest({"comments": comments, "policy": self.admission_policy.revision})
        return ports.AdmissionSnapshot(
            fresh, base, paths, comments, statuses, version, self._discovery_version(fresh), diff_guard=guard
        )

    def _discovery_version(self, pull):
        # Only listed facts of this PR. A time component, or the base branch tip (which moves
        # for every open PR at each landing), would re-flag every PR at once; like GitHub,
        # admission depends on the merge base, not the tip. Evidence the listing cannot see
        # (statuses, permissions, the merge base) is rechecked after `recheck_seconds`, taking
        # turns with PRs that really changed.
        return digest(
            {
                "source": self.repository_identity.record(),
                "policy": self.admission_policy.revision,
                "pull": {key: value for key, value in pull.__dict__.items() if key != "base_sha"},
            }
        )

    def list_open_pull_requests(self, repo, *, review_app_id=0, admission=False, on_page=None):
        if not admission:
            raise Unsupported("Forgejo admission uses the shared policy, not GitHub App checks")
        self._bound(repo)
        result = []
        seen = set()
        for row in self.client.pulls(self.repository, state="open"):
            if not isinstance(row, dict):
                raise Incomplete("native pull listing is malformed")
            number = self._positive(row.get("number"))
            if number in seen:
                raise Incomplete("native listing repeated a pull request")
            seen.add(number)
            try:
                pull = self._pull(row)
                item = self._listed(pull)
            except (ForgeError, ValueError, TypeError, KeyError):
                item = ports.OpenPullRequest(
                    number, "", "", False, (), {"incomplete": "native pull metadata unavailable"}
                )
            result.append(item)
            if on_page is not None:
                on_page([item])
        self._bound(repo)
        return result

    def _listed(self, pull):
        details = {
            "discovery_version": self._discovery_version(pull),
            "recheck_seconds": self.refresh_seconds,
            "updated_at": pull.updated_at,
        }
        return ports.OpenPullRequest(pull.number, pull.head_sha, pull.base_ref, pull.draft, (), details)

    def list_recent_pull_requests(self, repo, *, since, page_size=50, max_pages=4):
        """Open PRs updated at or after `since` (the forge's own clock), newest first.

        Returns (items, newest update time seen, complete). Pages are read until a PR older than
        `since` appears; `complete` is False when `max_pages` ran out first, and the caller then
        needs a full listing. Closed PRs are not listed: the full listing finds them absent.
        """
        self._bound(repo)
        items, newest = [], 0.0
        for page in range(1, max_pages + 1):
            rows = self.client.request(
                "GET",
                f"repos/{self.repository.name}/pulls?state=open&sort=recentupdate&limit={page_size}&page={page}",
            )
            if not isinstance(rows, list):
                raise Incomplete("native recent pull listing is malformed")
            if not rows:
                # Only an empty page ends the listing: the server may cap pages below `page_size`.
                self._bound(repo)
                return items, newest, True
            for row in rows:
                pull = self._pull(row)
                updated = _forge_time(pull.updated_at)
                if not updated:
                    raise Incomplete("native pull has no update time")
                newest = max(newest, updated)
                if updated < since:
                    self._bound(repo)
                    return items, newest, True
                items.append(self._listed(pull))
        self._bound(repo)
        return items, newest, False

    def list_recent_comments(self, repo, *, since, page_size=50, max_pages=20):
        """PR comments created or edited at or after `since` (the forge's own clock).

        Editing a comment does not change its PR's `updated_at` on Forgejo (checked on production:
        the review bot edits its scoreboard in place), so the recent PR listing cannot see a
        verdict that arrives as an edit. This repository-wide feed can. Returns (rows as
        (PR number, comment), newest comment update seen, complete).
        """
        from datetime import UTC, datetime
        from urllib.parse import urlencode

        self._bound(repo)
        stamp = datetime.fromtimestamp(max(0.0, since), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        found, newest = [], 0.0
        for page in range(1, max_pages + 1):
            query = urlencode({"since": stamp, "limit": page_size, "page": page})
            rows = self.client.request("GET", f"repos/{self.repository.name}/issues/comments?{query}")
            if not isinstance(rows, list):
                raise Incomplete("native comment feed is malformed")
            if not rows:
                self._bound(repo)
                return found, newest, True
            for row in rows:
                if not isinstance(row, dict) or type(row.get("id")) is not int:
                    raise Incomplete("native comment feed row is malformed")
                newest = max(newest, _forge_time(row.get("updated_at")))
                url = row.get("pull_request_url") or ""
                tail = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
                if tail.isdigit():
                    found.append((int(tail), row))
        self._bound(repo)
        return found, newest, False

    def verify_candidate_approval(self, frozen):
        if frozen.policy.source != self.repository_identity or frozen.request.source != self.repository_identity:
            raise ports.PromotionRefused("native candidate belongs to another repository")
        changed = self._git(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            frozen.request.base,
            frozen.request.head,
            "--",
        ).stdout
        paths = [p.decode("utf-8") for p in changed.split(b"\0") if p]
        if not all(
            path.startswith(self.admission_policy.prefix) or path in self.admission_policy.allow_files for path in paths
        ):
            raise ports.PromotionRefused("candidate exceeds the current native admission path policy")
        for member in frozen.request.accepted_members:
            if getattr(member, "source", None) is not None and member.source != self.repository_identity:
                raise ports.PromotionRefused("native-only promotion cannot authorize another source's member")
            snapshot = self.admission_snapshot(self.repository.name, member.pr)
            if (
                snapshot.pull.state != "open"
                or snapshot.pull.draft
                or snapshot.pull.head_sha != member.head
                or snapshot.pull.base_ref != frozen.policy.base_branch
                or set(s.lower() for s in snapshot.pull.labels) & KEEP
            ):
                raise ports.PromotionRefused("native member no longer matches its accepted source")
            result, *_ = self.admission_policy.evaluate(snapshot)
            # The caller has just verified the frozen candidate's trusted build,
            # scope and bump results. Repaired candidates need not turn the
            # original PR head's failed build green. Recheck source review here.
            if not result["review_safe"] or member.head in result["recovery_heads"]:
                raise ports.PromotionRefused("current native review policy refuses promotion")

    def _upsert_comment(self, number, full, marker, actor):
        hidden = comment_marker(marker)
        existing = []
        for row in self.client.comments(self.repository, number):
            if not isinstance(row, dict):
                raise Incomplete("native discussion is incomplete")
            if (
                hidden in (row.get("body") or "")
                and SourceActor.from_native(self.repository.forge, row.get("user")) == actor
            ):
                existing.append(row)
        if len(existing) > 1:
            raise Incomplete("native publication marker is ambiguous")
        if existing:
            row = existing[0]
            if row.get("body") == full:
                return str(self._positive(row.get("id")))
            result = self.client.edit_comment(self.repository, self._positive(row.get("id")), full)
        else:
            result = self.client.post_comment(self.repository, number, full)
        return str(self._positive(result.get("id")) if isinstance(result, dict) else self._positive(None))

    def post_comment(self, repo, number, body, marker):
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        # The queue's account carries recovery authority; quoted text never adds markers.
        body = body.replace("<!--tauceti-", "&lt;!--tauceti-")
        return self._upsert_comment(number, f"{body.rstrip()}\n\n{comment_marker(marker)}\n", marker, actor)

    def _labels(self):
        rows = self.client.collection(f"repos/{self.repository.name}/labels")
        found = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise Incomplete("native labels are incomplete")
            if row["name"] in found:
                raise Incomplete("native label names are ambiguous")
            found[row["name"]] = self._positive(row.get("id"))
        return found

    def set_labels(self, repo, number, add, remove):
        self._bound(repo)
        self.repository.pull_url(number)
        labels = self._labels()
        missing = [name for name in add if name not in labels]
        for name in missing:
            self.ensure_label(repo, name, "ededed", "TauCeti queue label")
        if missing:
            labels = self._labels()
        if any(name not in labels for name in add):
            raise Incomplete("requested native label is absent")
        if add:
            self.client.request(
                "POST", f"repos/{self.repository.name}/issues/{number}/labels", {"labels": [labels[n] for n in add]}
            )
        for name in remove:
            if name in labels:
                try:
                    self.client.request("DELETE", f"repos/{self.repository.name}/issues/{number}/labels/{labels[name]}")
                except ForgeError as error:
                    if error.status != 404:
                        raise

    def ensure_label(self, repo, name, color, description):
        self._bound(repo)
        if name not in self._labels():
            self.client.request(
                "POST",
                f"repos/{self.repository.name}/labels",
                {"name": name, "color": color, "description": description},
            )

    def close_pr(self, repo, number, title):
        self._bound(repo)
        self.repository.pull_url(number)
        self.client.request(
            "PATCH", f"repos/{self.repository.name}/pulls/{number}", {"state": "closed", "title": title}
        )

    def rate_limit(self):
        raise Unsupported("Forgejo has no GitHub rate-limit resource; native admission uses its own bounded polling")

    def eligibility_checks(self, repo, head):
        raise Unsupported("Forgejo approval uses current native primary-policy evidence")
