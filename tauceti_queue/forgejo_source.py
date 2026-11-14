"""Direct Forgejo source operations and native publication authority for the queue.

Primary admission uses the installed shared review policy. GitHub execution and
App check-run approvals are separate capabilities and never inferred here.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from tauceti_tools.ci_candidate import RepositoryIdentity, read_json
from tauceti_tools.forge import ForgeError, Incomplete, PrincipalRef, Unsupported

from tauceti_worker.source_identity import SourceActor

from . import ports
from .admission import AdmissionPolicy, digest, policy, timestamp
from .eligibility import KEEP
from .forgejo_git import ForgejoGitSource
from .github_adapter import comment_marker
from .task_identity import NATIVE_GIT_REFS

BINDING = re.compile(r"<!--tauceti-source:v2 (.*?)-->", re.S)
PUBLICATION_SCHEMA = "tauceti-source-publication/v2"
INTENT_SCHEMA = "tauceti-source-publication-intent/v3"
PUB_DESCRIPTION = "tauceti-pub/v3:"
REVIEW_CONTEXT = "tauceti-review/"
RECOVERY_CONTEXT = "tauceti-recovery/"


class PublicationSuperseded(RuntimeError):
    """A newer native publication superseded this frozen intent; do not resend it."""

    def __init__(self, message, *, sequence=None, nonce=None, actor=None):
        super().__init__(message)
        self.sequence, self.nonce, self.actor = sequence, nonce, actor


class PublisherNotGranted(PermissionError):
    """Trusted publisher configuration refuses this operation; operator reconciliation is required."""


class PublicationDamaged(Incomplete):
    """Confirmed publication was moderated; prepare a new intent, never rerun inference."""

    def __init__(self, message, *, number=None, nonce=None, publication_kind=None):
        super().__init__(message)
        self.number, self.nonce, self.publication_kind = number, nonce, publication_kind
        self.head = None


def principal(actor):
    return f"{actor.principal.forge.key}/users/{actor.principal.native_id}"


@dataclass(frozen=True)
class NativePublishers:
    source: RepositoryIdentity
    reviews: frozenset[PrincipalRef]
    statuses: frozenset[PrincipalRef]
    recoveries: frozenset[PrincipalRef] = frozenset()
    generation: str = field(kw_only=True)

    def __post_init__(self):
        if not isinstance(self.source, RepositoryIdentity) or self.source.repository.forge.kind != "forgejo":
            raise ValueError("native publisher policy requires a qualified source")
        if (
            not isinstance(self.generation, str)
            or re.fullmatch(r"[0-9a-f]{32}", self.generation) is None
            or self.generation == "0" * 32
        ):
            raise ValueError("native publisher policy requires a fresh random authority generation")
        for name in ("reviews", "statuses", "recoveries"):
            values = getattr(self, name)
            if (
                type(values) is not frozenset
                or len(values) > 256
                or (name != "recoveries" and not values)
                or any(
                    not isinstance(p, PrincipalRef)
                    or p.forge != self.source.repository.forge
                    or re.fullmatch(r"[1-9][0-9]*", p.native_id) is None
                    for p in values
                )
            ):
                raise ValueError("native publishers require bounded source-local principal IDs")

    def record(self):
        return {
            "source": self.source.record(),
            "generation": self.generation,
            **{
                name: sorted(int(p.native_id) for p in getattr(self, name))
                for name in ("reviews", "statuses", "recoveries")
            },
        }


class NativeAdmissionPolicy(AdmissionPolicy):
    human_owned_approvals = False

    def __init__(self, publishers, *, clock=time.time, pending_timeout_seconds=21600, base_branch="main", **kwargs):
        if not isinstance(publishers, NativePublishers):
            raise ValueError("native admission requires explicit publisher identities")
        # Each forge numbers its own PRs, so a rubric being phased in has a cutoff for this source.
        kwargs.setdefault("repository", publishers.source.repository.key)
        super().__init__(**kwargs)
        if type(pending_timeout_seconds) is not int or not 1 <= pending_timeout_seconds <= 86400 or not base_branch:
            raise ValueError("native admission needs a bounded review wait and base branch")
        self.publishers = publishers
        self.clock, self.pending_timeout_seconds, self.base_branch = clock, pending_timeout_seconds, base_branch
        self.revision = digest(
            {
                "review_policy": self.revision,
                "publishers": publishers.record(),
                "pending_timeout_seconds": pending_timeout_seconds,
                "base_branch": base_branch,
                "native_protocol": INTENT_SCHEMA,
            }
        )

    def binding(self, actor):
        if actor.principal.forge != self.publishers.source.repository.forge:
            raise ValueError("publication actor belongs to another source")
        return {
            "schema": PUBLICATION_SCHEMA,
            "source": self.publishers.source.record(),
            "issuer": principal(actor),
            "generation": self.publishers.generation,
            "policy_revision": self.source_revision,
        }

    @staticmethod
    def append_provenance(body, block):
        # Review's existing provenance block remains the final line, as its
        # readers require. Native source authority is additional provenance.
        metadata = list(policy.META_RE.finditer(body))
        if metadata and not body[metadata[-1].end() :].strip():
            last = metadata[-1]
            return body[: last.start()].rstrip() + "\n\n" + block + "\n" + last.group(0) + "\n"
        return body.rstrip() + "\n\n" + block + "\n"

    def bind_comment(self, body, actor):
        # Model/user quotations are prose, never another authority block.
        body = body.replace("<!--tauceti-source:", "&lt;!--tauceti-source:")
        block = (
            "<!--tauceti-source:v2 " + json.dumps(self.binding(actor), sort_keys=True, separators=(",", ":")) + "-->"
        )
        return self.append_provenance(body, block)

    def evaluate(self, snapshot):
        board = policy.latest_current_scoreboard(snapshot.comments, snapshot.pull.head_sha)
        pending = snapshot.statuses.get("_native_review") == "pending" or board and board[1].get("mode") == "init"
        result, authorization, evidence, comment, meta = super().evaluate(snapshot)
        # Adapter comments carry only authenticated, source-bound publisher rows.
        heads = set()
        for row in snapshot.comments:
            actor = SourceActor.from_native(self.publishers.source.repository.forge, row.get("user"))
            if actor.principal not in self.publishers.recoveries:
                continue
            body = row.get("body") or ""
            if body.startswith("Merge-queue recovery for head `"):
                heads.update(policy.REBASE_MARKER_RE.findall(body))
                heads.update(policy.STALLED_MARKER_RE.findall(body))
        if snapshot.statuses.get("_native_recovery", "absent") not in {"absent", "clear", "success"}:
            heads.add(snapshot.pull.head_sha)
        result["recovery_heads"] = sorted(heads)
        result["recovery_authority_hold"] = snapshot.statuses.get("_native_recovery") == "revoked"
        if snapshot.pull.head_sha in heads:
            result.update(
                merge=False,
                reason=(
                    "native recovery authority needs reconciliation"
                    if result["recovery_authority_hold"]
                    else "waiting on a trusted native recovery request for this head"
                ),
            )
        if (
            pending
            and snapshot.pull.head_sha not in heads
            and snapshot.pull.state == "open"
            and not snapshot.pull.draft
            and snapshot.pull.base_ref == self.base_branch
            and not {s.lower() for s in snapshot.pull.labels} & KEEP
        ):
            started = timestamp(snapshot.statuses.get("_native_review_started"))
            if not started or started > self.clock() + 60:
                raise Incomplete("native in-progress review has no trustworthy start time")
            if self.clock() - started < self.pending_timeout_seconds:
                raise Incomplete("native review publication has not completed this round")
            result.update(
                review_safe=False, merge=False, reason="native review publication expired; publish a new intent"
            )
        evidence.update(source_repository=self.publishers.source.record(), publisher_policy=self.revision)
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
        self._publisher_configured = False
        self._recovery_grants = {admission_policy.publishers.generation: set(admission_policy.publishers.recoveries)}

    def configure_admission_polling(self, *, clock, refresh_seconds):
        self.clock, self.refresh_seconds = clock, max(1, int(refresh_seconds))

    def configure_publisher_policy(self, store):
        """Fence trust changes in the existing queue settings, before serving work."""
        key = "state:native-publisher-policy:" + self.repository.key + ":" + str(self.repository_identity.native_id)
        current = self.admission_policy.publishers.record()
        with store.transaction():
            saved = store.get_setting(key)
            if saved is None:
                saved = {"current": current, "retired_generations": [], "recovery_grants": {}}
            if (
                not isinstance(saved, dict)
                or set(saved) != {"current", "retired_generations", "recovery_grants"}
                or not isinstance(saved["current"], dict)
                or not isinstance(saved["retired_generations"], list)
                or len(saved["retired_generations"]) > 4096
                or not isinstance(saved["recovery_grants"], dict)
                or len(saved["recovery_grants"]) > 4097
            ):
                raise ValueError("invalid native publisher configuration history")
            previous, retired = saved["current"], saved["retired_generations"]
            if previous != current:
                if current["generation"] == previous.get("generation") or current["generation"] in retired:
                    raise ValueError("publisher changes require a fresh, never reused authority generation")
                retired = [*retired, previous["generation"]]
                if len(retired) > 4096:
                    raise ValueError("native publisher configuration history exceeds its bound")
            grants = saved["recovery_grants"]
            for record in (previous, current):
                grants[record["generation"]] = record["recoveries"]
            for generation, ids in grants.items():
                if (
                    not isinstance(generation, str)
                    or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                    or not isinstance(ids, list)
                    or len(ids) > 256
                    or any(type(i) is not int or i < 1 for i in ids)
                ):
                    raise ValueError("invalid native recovery publisher history")
            self._recovery_grants = {
                g: {PrincipalRef(self.repository.forge, str(i)) for i in ids} for g, ids in grants.items()
            }
            store.set_setting(key, {"current": current, "retired_generations": retired, "recovery_grants": grants})
            self._publisher_configured = True
            return previous != current

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

    def _binding_matches(self, body, actor):
        bindings = BINDING.findall(body)
        if len(bindings) != 1:
            return False
        try:
            bound = read_json(bindings[0].encode(), max_bytes=16 * 1024)
        except ValueError:
            return False
        if not isinstance(bound, dict) or set(bound) != {"schema", "source", "issuer", "policy_revision", "generation"}:
            return False
        reported = bound["policy_revision"]
        return (
            bound["schema"] == PUBLICATION_SCHEMA
            and bound["source"] == self.repository_identity.record()
            and isinstance(bound["generation"], str)
            and bound["generation"] == self.admission_policy.publishers.generation
            and bound["issuer"] == principal(actor)
            and isinstance(reported, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})", reported) is not None
        )

    def _publication_latest(self, rows, context):
        """Select current publishers' semantic order before checking their live ACL.

        Unresolved issuers deny older registrations without resetting the highest
        trusted sequence. A fresh registration of that intent or a newer one can
        reconcile the barrier; no unverified sequence can grant or poison order.
        """
        if not self._publisher_configured:
            raise Incomplete("native publication requires the persisted publisher-policy binding")
        entries = []
        retired_recovery = False
        unknown_barrier = 0
        seen = set()
        generation = self.admission_policy.publishers.generation
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("native statuses are incomplete")
            if row.get("context") != context:
                continue
            status_id = self._positive(row.get("id"))
            if status_id in seen:
                raise Incomplete("native publication statuses are ambiguous")
            seen.add(status_id)
            description = row.get("description") or ""
            try:
                actor = SourceActor.from_native(self.repository.forge, row.get("creator"))
            except Incomplete:
                actor = None
            if actor is None:
                # Missing/deleted issuers deny older facts, without letting an
                # unverified sequence poison the namespace forever. A fresh
                # trusted registration after this row can reconcile the barrier.
                if description.startswith("tauceti-pub/"):
                    unknown_barrier = max(unknown_barrier, status_id)
                continue
            record_generation = description.split(":", 2)[1:2]
            if (
                context.startswith(RECOVERY_CONTEXT)
                and description.startswith("tauceti-pub/")
                and record_generation
                and record_generation[0] != generation
            ):
                # A restored backup may have lost both a generation and its
                # publisher account. Unattributable retired facts can only deny,
                # until a fresh explicit current-authority decision reconciles
                # them. No old record can override current-generation evidence.
                retired_recovery = True
            allowed = (
                self.admission_policy.publishers.recoveries
                if context.startswith(RECOVERY_CONTEXT)
                else self.admission_policy.publishers.reviews
            )
            if actor.principal not in allowed:
                continue
            if not description.startswith(PUB_DESCRIPTION + str(generation) + ":"):
                if description.startswith("tauceti-pub/") and description.split(":", 2)[1:2] == [generation]:
                    raise Incomplete("unsupported native publication protocol for the current authority generation")
                continue  # Old authority generations and unsupported protocols grant nothing.
            parts = description[len(PUB_DESCRIPTION) :].split(":")
            if (
                len(parts) != 5
                or re.fullmatch(r"[1-9][0-9]*", parts[1]) is None
                or int(parts[1]) >= 2**63
                or re.fullmatch(r"[0-9a-f]{32}", parts[2]) is None
                or re.fullmatch(r"pending|clear-pending|clear|[1-9][0-9]*", parts[3]) is None
                or re.fullmatch(r"[0-9a-f]{64}", parts[4]) is None
            ):
                raise Incomplete("native publication receipt is malformed")
            entries.append((int(parts[1]), parts[2], parts[3], parts[4], actor, row))
        floor = max((r[0] for r in entries), default=0)
        floor_rows = [r for r in entries if r[0] == floor]
        if len({(r[1], r[3], principal(r[4])) for r in floor_rows}) > 1:
            raise Incomplete("native publication sequence names different intents")
        if unknown_barrier:
            entries = [r for r in entries if r[5]["id"] > unknown_barrier and r[0] >= floor]
            if not any(r[2] in {"pending", "clear-pending"} for r in entries):
                row = floor_rows[0] if floor_rows else None
                return (
                    unknown_barrier,
                    {
                        "_stage": "unverified",
                        "_sequence": floor,
                        "_nonce": row[1] if row else "",
                        "_digest": row[3] if row else "",
                    },
                    row[4] if row else None,
                )
        if not entries:
            if retired_recovery:
                return 0, {"_stage": "retired", "_sequence": 0, "_nonce": "", "_digest": ""}, None
            return None
        sequence = max(r[0] for r in entries)
        selected = [r for r in entries if r[0] == sequence]
        identities = {(r[1], r[3], principal(r[4]) if r[4] is not None else None) for r in selected}
        if len(identities) != 1:
            raise Incomplete("native publication sequence names different intents")
        registrations = [r for r in selected if r[2] in {"pending", "clear-pending"}]
        if not registrations:
            raise Incomplete("native publication has no intent registration")
        if len({r[2] for r in registrations}) != 1:
            raise Incomplete("native publication operation changed")
        first = min(registrations, key=lambda r: r[5]["id"])
        completed = [r for r in selected if r[2] not in {"pending", "clear-pending"}]
        if completed:
            if len({r[2] for r in completed}) != 1 or (first[2] == "clear-pending") != (completed[0][2] == "clear"):
                raise Incomplete("native publication completion is ambiguous")
            chosen = max(completed, key=lambda r: r[5]["id"])
            if (chosen[5].get("status") or chosen[5].get("state")) != "success":
                raise Incomplete("native publication completion is unavailable")
        else:
            chosen = first
        # Status IDs only reconcile duplicate deliveries of ONE immutable intent.
        # They never decide whether an older or newer verdict has precedence.
        stamp = dict(
            chosen[5],
            _sequence=sequence,
            _nonce=first[1],
            _digest=first[3],
            _stage=chosen[2],
            _started_at=first[5].get("created_at"),
        )
        return first[5]["id"], stamp, first[4]

    def _publication_comment(self, number, rows, context, allowed, comments, *, own_actor=None):
        latest = self._publication_latest(rows, context)
        if latest is None:
            return None, "absent"
        _id, stamp, actor = latest
        if stamp["_stage"] in {"unverified", "retired"}:
            return None, "revoked"
        if actor is None or actor.principal not in allowed:
            return None, "revoked"
        try:
            writable = actor == own_actor or self._permission(actor) in {"write", "admin", "owner"}
        except ForgeError as error:
            if error.status != 404:
                raise
            writable = False
        if not writable:
            return None, "revoked"
        stage = stamp["_stage"]
        if stage in {"pending", "clear-pending"}:
            return None, "pending"
        if stage == "clear":
            if not context.startswith(RECOVERY_CONTEXT):
                raise Incomplete("native review cannot be a recovery clear")
            return None, "clear"
        selected = [row for row in comments if isinstance(row, dict) and row.get("id") == int(stage)]
        if len(selected) != 1:
            raise PublicationDamaged(
                "native publication comment was removed or is unavailable",
                number=number,
                nonce=stamp["_nonce"],
                publication_kind="recovery" if context.startswith(RECOVERY_CONTEXT) else "review",
            )
        row = selected[0]
        body = row.get("body")
        if (
            not isinstance(body, str)
            or SourceActor.from_native(self.repository.forge, row.get("user")) != actor
            or hashlib.sha256(body.encode()).hexdigest() != stamp["_digest"]
            or not self._binding_matches(body, actor)
        ):
            raise PublicationDamaged(
                "native publication content no longer matches its issuer's receipt",
                number=number,
                nonce=stamp["_nonce"],
                publication_kind="recovery" if context.startswith(RECOVERY_CONTEXT) else "review",
            )
        return row, "success"

    def _comments(self, number, head, rows=None):
        rows = self.client.statuses(self.repository, head) if rows is None else rows
        comments = self.client.comments(self.repository, number)
        if not isinstance(comments, list):
            raise Incomplete("native discussion is unavailable")
        publishers = self.admission_policy.publishers
        review_error = None
        try:
            review, review_state = self._publication_comment(
                number, rows, REVIEW_CONTEXT + str(number), publishers.reviews, comments
            )
        except PublicationDamaged as error:
            review_error = error
        try:
            recovery, recovery_state = self._publication_comment(
                number, rows, RECOVERY_CONTEXT + str(number), publishers.recoveries, comments
            )
        except PublicationDamaged as error:
            error.head = head
            raise
        if review_error is not None:
            raise review_error
        return tuple(row for row in (review, recovery) if row is not None), review_state, recovery_state

    def _statuses(self, head, rows=None):
        if not self._publisher_configured:
            raise Incomplete("native statuses require the persisted publisher-policy binding")
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
                    permissions[actor.principal] = (
                        actor.principal in self.admission_policy.publishers.statuses
                        and self._permission(actor) in {"write", "admin", "owner"}
                    )
                except ForgeError as error:
                    if error.status != 404:
                        raise
                    permissions[actor.principal] = False
            state = row.get("status") or row.get("state")
            valid = (
                permissions[actor.principal]
                and row.get("description") == self.ci_status_description
                and state in {"pending", "success", "error", "failure", "warning"}
            )
            result[context] = state if valid else "error"
        return result

    @property
    def ci_status_description(self):
        return "tauceti-ci-status/v1:" + str(self.admission_policy.publishers.generation)

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
        rows = self.client.statuses(self.repository, pull.head_sha)
        try:
            comments, review_state, recovery_state = self._comments(number, pull.head_sha, rows)
        except PublicationDamaged as error:
            if error.publication_kind != "review":
                raise
            # Known moderation is a negative verdict, not an API outage. It
            # withdraws waiting inputs and refuses an inserted exact candidate.
            try:
                recovery, recovery_state = self._publication_comment(
                    number,
                    rows,
                    RECOVERY_CONTEXT + str(number),
                    self.admission_policy.publishers.recoveries,
                    self.client.comments(self.repository, number),
                )
            except PublicationDamaged as recovery_error:
                recovery_error.head = pull.head_sha
                raise
            comments, review_state = (() if recovery is None else (recovery,)), "damaged"
        statuses = self._statuses(pull.head_sha, rows)
        statuses["_native_review"] = review_state
        statuses["_native_recovery"] = recovery_state
        latest = self._publication_latest(rows, REVIEW_CONTEXT + str(number))
        statuses["_native_review_started"] = (latest[1].get("_started_at") or "") if latest is not None else ""
        fresh = self.fetch_pr(repo, number)
        # Ordinary discussion/title timestamps cannot invalidate a complete
        # source read. Relevant publications are bound by immutable receipts;
        # source head/base/state/labels/body still fence this observation.
        fields = ("head_sha", "base_ref", "state", "draft", "labels", "body", "base_sha", "merged")
        if any(getattr(fresh, key, None) != getattr(pull, key, None) for key in fields):
            raise Incomplete("native pull changed while reading admission evidence")
        version = base + ":" + digest({"comments": comments, "policy": self.admission_policy.revision})
        return ports.AdmissionSnapshot(fresh, base, paths, comments, statuses, version, self._discovery_version(fresh))

    def _discovery_version(self, pull):
        # The bounded polling backstop rechecks permissions and source evidence
        # without refetching
        # unchanged immutable Git objects on every discovery.
        return digest(
            {
                "source": self.repository_identity.record(),
                "policy": self.admission_policy.revision,
                "pull": pull.__dict__,
                "refresh": int(
                    (self.clock() + int(digest(pull.number)[:8], 16) % self.refresh_seconds) // self.refresh_seconds
                ),
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
                details = {"discovery_version": self._discovery_version(pull)}
                item = ports.OpenPullRequest(pull.number, pull.head_sha, pull.base_ref, pull.draft, (), details)
            except (ForgeError, ValueError, TypeError, KeyError):
                item = ports.OpenPullRequest(
                    number, "", "", False, (), {"incomplete": "native pull metadata unavailable"}
                )
            result.append(item)
            if on_page is not None:
                on_page([item])
        self._bound(repo)
        return result

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

    def prepare_publication(self, repo, number, head, body, marker, *, sequence, kind="review", clear=False):
        """Freeze this record in the existing outbox before any remote write.

        sequence is a durable, strictly increasing number for this PR, kind and
        authority generation, shared by its publishers. It orders creation of
        intents, including intents not yet delivered. Never derive it from the
        observed remote state at retry time.
        """
        if not self._publisher_configured:
            raise Incomplete("native publication requires the persisted publisher-policy binding")
        if kind not in {"review", "recovery"} or type(clear) is not bool or clear and kind != "recovery":
            raise ValueError("unknown native publication operation")
        if type(sequence) is not int or not 0 < sequence < 2**63:
            raise ValueError("native publication requires a durable monotonic outbox sequence")
        if not isinstance(body, str) or not isinstance(marker, str) or not marker:
            raise ValueError("native publication needs a body and marker")
        self._sha(head)
        self._positive(number)
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = getattr(self.admission_policy.publishers, "reviews" if kind == "review" else "recoveries")
        if actor.principal not in allowed:
            raise PublisherNotGranted("native actor is not a configured publisher")
        nonce = secrets.token_hex(16)
        unique_marker = marker + ":" + nonce
        full = (
            ""
            if clear
            else self.admission_policy.bind_comment(
                self.admission_policy.append_provenance(body, comment_marker(unique_marker)), actor
            )
        )
        return {
            "schema": INTENT_SCHEMA,
            "source": self.repository_identity.record(),
            "generation": self.admission_policy.publishers.generation,
            "issuer": int(actor.principal.native_id),
            "number": number,
            "head": head,
            "body": full,
            "digest": hashlib.sha256(full.encode()).hexdigest(),
            "marker": unique_marker,
            "kind": kind,
            "nonce": nonce,
            "sequence": sequence,
            "clear": clear,
        }

    @staticmethod
    def _intent_prefix(intent):
        return PUB_DESCRIPTION + f"{intent['generation']}:{intent['sequence']}:{intent['nonce']}:"

    def publish(self, intent):
        required = {
            "schema",
            "source",
            "generation",
            "issuer",
            "number",
            "head",
            "body",
            "digest",
            "marker",
            "kind",
            "nonce",
            "sequence",
            "clear",
        }
        if (
            not isinstance(intent, dict)
            or set(intent) != required
            or intent["schema"] != INTENT_SCHEMA
            or intent["source"] != self.repository_identity.record()
            or not isinstance(intent["generation"], str)
            or intent["generation"] != self.admission_policy.publishers.generation
            or intent["kind"] not in {"review", "recovery"}
            or type(intent["clear"]) is not bool
            or intent["clear"]
            and intent["kind"] != "recovery"
            or type(intent["sequence"]) is not int
            or not 0 < intent["sequence"] < 2**63
            or not isinstance(intent["body"], str)
            or len(intent["body"].encode()) > 16 * 1024 * 1024
            or not isinstance(intent["marker"], str)
            or not isinstance(intent["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", intent["nonce"]) is None
            or not intent["marker"].endswith(":" + intent["nonce"])
            or hashlib.sha256(intent["body"].encode()).hexdigest() != intent["digest"]
            or intent["clear"]
            and intent["body"] != ""
            or not intent["clear"]
            and comment_marker(intent["marker"]) not in intent["body"]
        ):
            raise ValueError("invalid frozen native publication")
        self._positive(intent["number"])
        self._sha(intent["head"])
        self._bound(self.repository.name)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = getattr(self.admission_policy.publishers, "reviews" if intent["kind"] == "review" else "recoveries")
        if (
            type(intent["issuer"]) is not int
            or intent["issuer"] != int(actor.principal.native_id)
            or actor.principal not in allowed
            or not intent["clear"]
            and not self._binding_matches(intent["body"], actor)
        ):
            raise PublisherNotGranted("frozen native publication belongs to another issuer or source")
        # Facts belong to the accepted commit, including while a PR is closed or
        # temporarily points elsewhere. Dropping a red intent would revive old
        # green evidence if that PR later returned to the same head.
        context = (REVIEW_CONTEXT if intent["kind"] == "review" else RECOVERY_CONTEXT) + str(intent["number"])
        prefix = self._intent_prefix(intent)
        pending = prefix + ("clear-pending:" if intent["clear"] else "pending:") + intent["digest"]
        rows = self.client.statuses(self.repository, intent["head"])
        latest = self._publication_latest(rows, context)
        if latest is not None:
            _id, stamp, issuer = latest
            if stamp["_sequence"] > intent["sequence"]:
                raise PublicationSuperseded(
                    "newer native publication superseded this intent",
                    sequence=stamp["_sequence"],
                    nonce=stamp["_nonce"],
                    actor=issuer,
                )
            if stamp["_sequence"] == intent["sequence"]:
                if issuer != actor or stamp["_nonce"] != intent["nonce"] or stamp["_digest"] != intent["digest"]:
                    raise Incomplete("native publication sequence was reused for another intent")
                if stamp["_stage"] not in {"pending", "clear-pending", "unverified"}:
                    comment, state = self._publication_comment(
                        intent["number"],
                        rows,
                        context,
                        allowed,
                        self.client.comments(self.repository, intent["number"]),
                        own_actor=actor,
                    )
                    if state == "clear":
                        return "clear"
                    if state == "success" and comment is not None:
                        return str(comment["id"])
        if latest is None or latest[1]["_sequence"] < intent["sequence"] or latest[1]["_stage"] == "unverified":
            self.client.request(
                "POST",
                f"repos/{self.repository.name}/statuses/{intent['head']}",
                {"context": context, "state": "pending", "description": pending},
            )
        # Each intent owns its comment; late writes cannot corrupt newer bodies.
        # Even a success POST racing a new registration remains an older sequence.
        self._check_publication_current(intent, context)
        comment_id = (
            "clear"
            if intent["clear"]
            else self._upsert_comment(intent["number"], intent["body"], intent["marker"], actor)
        )
        self._check_publication_current(intent, context)
        self.client.request(
            "POST",
            f"repos/{self.repository.name}/statuses/{intent['head']}",
            {"context": context, "state": "success", "description": prefix + comment_id + ":" + intent["digest"]},
        )
        self._check_publication_current(intent, context)
        return comment_id

    def _check_publication_current(self, intent, context):
        latest = self._publication_latest(self.client.statuses(self.repository, intent["head"]), context)
        if latest is None or latest[1]["_stage"] in {"unverified", "retired"}:
            raise Incomplete("native publication registration needs observation or exact-intent reconciliation")
        stamp = latest[1]
        if stamp["_nonce"] != intent["nonce"]:
            raise PublicationSuperseded(
                "native publication intent changed", sequence=stamp["_sequence"], nonce=stamp["_nonce"], actor=latest[2]
            )

    def prepare_outbox_comment(self, repo, number, body, marker, *, sequence, publication=None):
        if publication is None:
            return None
        if (
            not isinstance(publication, dict)
            or set(publication) != {"kind", "head", "clear"}
            or publication["kind"] != "recovery"
            or type(publication["clear"]) is not bool
        ):
            raise ValueError("queue publications require an explicit trusted recovery operation")
        return self.prepare_publication(
            repo,
            number,
            publication["head"],
            body,
            marker,
            sequence=sequence,
            kind="recovery",
            clear=publication["clear"],
        )

    def post_comment(self, repo, number, body, marker):
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        body = body.replace("<!--tauceti-", "&lt;!--tauceti-")
        full = self.admission_policy.bind_comment(
            self.admission_policy.append_provenance(body, comment_marker(marker)), actor
        )
        return self._upsert_comment(number, full, marker, actor)

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
