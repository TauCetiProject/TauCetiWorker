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
from dataclasses import dataclass
from urllib.parse import quote

from tauceti_tools.ci_candidate import RepositoryIdentity, read_json
from tauceti_tools.forge import ForgeError, Incomplete, PrincipalRef, Unsupported

from tauceti_worker.source_identity import SourceActor

from . import ports
from .admission import AdmissionPolicy, digest, policy
from .eligibility import KEEP
from .forgejo_git import ForgejoGitSource
from .github_adapter import comment_marker
from .task_identity import NATIVE_GIT_REFS

BINDING = re.compile(r"<!--tauceti-source:v1 (.*?)-->", re.S)
PUBLICATION_SCHEMA = "tauceti-source-publication/v1"
INTENT_SCHEMA = "tauceti-source-publication-intent/v2"
PUB_DESCRIPTION = "tauceti-pub/v1:"
PUB_V2 = "tauceti-pub/v2:"
REVIEW_CONTEXT = "tauceti-review/"
RECOVERY_CONTEXT = "tauceti-recovery/"


class PublicationSuperseded(RuntimeError):
    """A newer native publication superseded this frozen intent; do not resend it."""


def principal(actor):
    return f"{actor.principal.forge.key}/users/{actor.principal.native_id}"


@dataclass(frozen=True)
class NativePublishers:
    source: RepositoryIdentity
    reviews: frozenset[PrincipalRef]
    statuses: frozenset[PrincipalRef]
    recoveries: frozenset[PrincipalRef] = frozenset()

    def __post_init__(self):
        if not isinstance(self.source, RepositoryIdentity) or self.source.repository.forge.kind != "forgejo":
            raise ValueError("native publisher policy requires a qualified source")
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
            **{
                name: sorted(int(p.native_id) for p in getattr(self, name))
                for name in ("reviews", "statuses", "recoveries")
            },
        }


class NativeAdmissionPolicy(AdmissionPolicy):
    human_owned_approvals = False

    def __init__(self, publishers, **kwargs):
        if not isinstance(publishers, NativePublishers):
            raise ValueError("native admission requires explicit publisher identities")
        super().__init__(**kwargs)
        self.publishers = publishers
        self.revision = digest({"review_policy": self.revision, "publishers": publishers.record()})

    def binding(self, actor):
        if actor.principal.forge != self.publishers.source.repository.forge:
            raise ValueError("publication actor belongs to another source")
        return {
            "schema": PUBLICATION_SCHEMA,
            "source": self.publishers.source.record(),
            "issuer": principal(actor),
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
            "<!--tauceti-source:v1 " + json.dumps(self.binding(actor), sort_keys=True, separators=(",", ":")) + "-->"
        )
        return self.append_provenance(body, block)

    def evaluate(self, snapshot):
        if snapshot.statuses.get("_native_review") == "pending":
            raise Incomplete("native review publication is still pending")
        board = policy.latest_current_scoreboard(snapshot.comments, snapshot.pull.head_sha)
        if board and board[1].get("mode") == "init":
            raise Incomplete("native review has not completed this round")
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
        if snapshot.pull.head_sha in heads:
            result.update(merge=False, reason="waiting on a trusted native recovery request for this head")
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

    def __init__(self, identity, client, clone, *, token, admission_policy, timeout=120):
        super().__init__(identity, client, clone, token=token, timeout=timeout)
        if not isinstance(admission_policy, NativeAdmissionPolicy) or admission_policy.publishers.source != identity:
            raise ValueError("native source and admission policy differ")
        self.admission_policy = admission_policy
        self.clone_dir = self.clone
        self._publisher_history = {
            name: set(getattr(admission_policy.publishers, name)) for name in ("reviews", "statuses", "recoveries")
        }

    def bind_publisher_history(self, store):
        """Remember former issuers in the queue's existing settings across rotation.

        Former issuers are barriers, never current authority. This prevents a
        publisher configuration change from exposing an older green verdict.
        """
        key = "state:native-publishers:" + self.repository.key + ":" + str(self.repository_identity.native_id)
        with store.transaction():
            saved = store.get_setting(key, {})
            if not isinstance(saved, dict) or set(saved) - set(self._publisher_history):
                raise ValueError("invalid native publisher history")
            for name, known in self._publisher_history.items():
                ids = saved.get(name, [])
                if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
                    raise ValueError("invalid native publisher history")
                known.update(PrincipalRef(self.repository.forge, str(i)) for i in ids)
                if len(known) > 4096:
                    raise ValueError("native publisher history exceeds its bound")
            store.set_setting(
                key, {name: sorted(int(p.native_id) for p in known) for name, known in self._publisher_history.items()}
            )

    def _known_publishers(self, name):
        known = self._publisher_history[name]
        known.update(getattr(self.admission_policy.publishers, name))
        return known

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
        if not isinstance(bound, dict) or set(bound) != {"schema", "source", "issuer", "policy_revision"}:
            return False
        reported = bound["policy_revision"]
        return (
            bound["schema"] == PUBLICATION_SCHEMA
            and bound["source"] == self.repository_identity.record()
            and bound["issuer"] == principal(actor)
            and isinstance(reported, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})", reported) is not None
        )

    def _publication_latest(self, rows, context, allowed):
        candidates = []
        completions = []
        seen = set()
        role = "reviews" if context.startswith(REVIEW_CONTEXT) else "recoveries"
        known = self._known_publishers(role)
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("native statuses are incomplete")
            if row.get("context") != context:
                continue
            status_id = self._positive(row.get("id"))
            if status_id in seen:
                raise Incomplete("native publication statuses are ambiguous")
            seen.add(status_id)
            try:
                actor = SourceActor.from_native(self.repository.forge, row.get("creator"))
            except Incomplete:
                actor = None  # An unknown/deleted issuer is a barrier, never fallback authority.
            if actor is not None and actor.principal not in known:
                continue
            description = row.get("description") or ""
            if description.startswith(PUB_V2):
                fields = description[len(PUB_V2) :].split(":")
                if len(fields) not in {3, 4} or re.fullmatch(r"[0-9a-f]{32}", fields[0]) is None:
                    raise Incomplete("native publication receipt is malformed")
                if fields[1] != "pending":
                    if len(fields) != 3 or not fields[1].isdigit() or re.fullmatch(r"[0-9a-f]{64}", fields[2]) is None:
                        raise Incomplete("native publication receipt is malformed")
                    completions.append((status_id, row, actor))
                    continue
                if len(fields) != 4 or re.fullmatch(r"0|[1-9][0-9]*", fields[3]) is None:
                    raise Incomplete("native publication predecessor is malformed")
            candidates.append((status_id, row, actor))
        if not candidates:
            if completions:
                raise Incomplete("native publication has no intent registration")
            return None
        latest = None
        for candidate in sorted(candidates, key=lambda r: r[0]):
            description = candidate[1].get("description") or ""
            if description.startswith(PUB_V2):
                expected = int(description.rsplit(":", 1)[1])
                if expected != (latest[0] if latest else 0):
                    continue  # A competing or stale intent did not acquire this predecessor.
            latest = candidate
        if latest is None:
            raise Incomplete("native publication predecessor history is unavailable")
        start_id, start, actor = latest
        description = start.get("description") or ""
        if description.startswith(PUB_V2):
            nonce, _, body_hash, _ = description[len(PUB_V2) :].split(":")
            if re.fullmatch(r"[0-9a-f]{64}", body_hash) is None:
                raise Incomplete("native publication digest is malformed")
            finished = [
                (i, row, issuer)
                for i, row, issuer in completions
                if i > start_id
                and issuer == actor
                and re.fullmatch(
                    re.escape(PUB_V2 + nonce + ":") + r"[1-9][0-9]*:" + body_hash, row.get("description") or ""
                )
                and (row.get("status") or row.get("state")) == "success"
            ]
            if finished:
                # Ordering belongs to immutable pending registrations, not the
                # arrival of completions. A late completion cannot revive old work.
                return start_id, max(finished, key=lambda r: r[0])[1], actor
        return latest

    def _publication_comment(self, number, rows, context, allowed, comments, *, own_actor=None):
        latest = self._publication_latest(rows, context, allowed)
        if latest is None:
            return None, "absent"
        _id, stamp, actor = latest
        if actor is None:
            raise Incomplete("latest native publication issuer is unavailable")
        if actor.principal not in allowed:
            return None, "revoked"
        # Select latest first: revocation or corrupt negative evidence must never
        # expose an older green publication from another issuer.
        try:
            writable = actor == own_actor or self._permission(actor) in {"write", "admin", "owner"}
        except ForgeError as error:
            if error.status != 404:
                raise
            writable = False
        if not writable:
            return None, "revoked"
        state = stamp.get("status") or stamp.get("state")
        if state != "success":
            return None, "pending"
        description = stamp.get("description")
        if context.startswith(RECOVERY_CONTEXT) and description == PUB_DESCRIPTION + "clear":
            return None, "clear"
        match = re.fullmatch(
            r"(?:"
            + re.escape(PUB_DESCRIPTION)
            + "|"
            + re.escape(PUB_V2)
            + r"[0-9a-f]{32}:)"
            + r"([1-9][0-9]*):([0-9a-f]{64})",
            description or "",
        )
        if match is None:
            raise Incomplete("native publication receipt is unavailable")
        selected = [row for row in comments if isinstance(row, dict) and row.get("id") == int(match[1])]
        if len(selected) != 1:
            raise Incomplete("native publication comment was removed or is unavailable")
        row = selected[0]
        body = row.get("body")
        if (
            not isinstance(body, str)
            or SourceActor.from_native(self.repository.forge, row.get("user")) != actor
            or hashlib.sha256(body.encode()).hexdigest() != match[2]
            or not self._binding_matches(body, actor)
        ):
            raise Incomplete("native publication content no longer matches its issuer's receipt")
        return row, "success"

    def _comments(self, number, head, rows=None):
        rows = self.client.statuses(self.repository, head) if rows is None else rows
        comments = self.client.comments(self.repository, number)
        if not isinstance(comments, list):
            raise Incomplete("native discussion is unavailable")
        publishers = self.admission_policy.publishers
        review, review_state = self._publication_comment(
            number, rows, REVIEW_CONTEXT + str(number), publishers.reviews, comments
        )
        recovery, recovery_state = self._publication_comment(
            number, rows, RECOVERY_CONTEXT + str(number), publishers.recoveries, comments
        )
        return tuple(row for row in (review, recovery) if row is not None), review_state, recovery_state

    def _statuses(self, head, rows=None):
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
            actor = SourceActor.from_native(self.repository.forge, row.get("creator"))
            if actor.principal not in self._known_publishers("statuses"):
                continue
            context, state = row.get("context"), row.get("status") or row.get("state")
            if (
                not isinstance(context, str)
                or not context
                or state not in {"pending", "success", "error", "failure", "warning"}
            ):
                raise Incomplete("native status context or outcome is unavailable")
            status_id = self._positive(row.get("id"))
            if status_id > newest.get(context, (0, "", None))[0]:
                newest[context] = (status_id, state, actor)
        permissions = {}
        result = {}
        for context, (_id, state, actor) in newest.items():
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
            result[context] = state if permissions[actor.principal] else "error"
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
        rows = self.client.statuses(self.repository, pull.head_sha)
        comments, review_state, recovery_state = self._comments(number, pull.head_sha, rows)
        statuses = self._statuses(pull.head_sha, rows)
        statuses["_native_review"] = review_state
        statuses["_native_recovery"] = recovery_state
        fresh = self.fetch_pr(repo, number)
        if fresh != pull:
            raise Incomplete("native pull changed while reading admission evidence")
        version = base + ":" + digest({"comments": comments, "policy": self.admission_policy.revision})
        return ports.AdmissionSnapshot(fresh, base, paths, comments, statuses, version, self._discovery_version(fresh))

    def _discovery_version(self, pull):
        # Webhooks invalidate status/publication changes immediately. A bounded
        # backstop also rechecks permissions and missed events without refetching
        # unchanged immutable Git objects on every discovery.
        return digest(
            {
                "source": self.repository_identity.record(),
                "policy": self.admission_policy.revision,
                "pull": pull.__dict__,
                "refresh": int(time.time() // 900),
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

    def prepare_publication(self, repo, number, head, body, marker, *, kind="review"):
        """Freeze this record in the caller's existing outbox before publish()."""
        if kind not in {"review", "recovery"}:
            raise ValueError("unknown native publication kind")
        self._sha(head)
        self._positive(number)
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = getattr(self.admission_policy.publishers, "reviews" if kind == "review" else "recoveries")
        if actor.principal not in allowed:
            raise PermissionError("native actor is not a configured publisher")
        context = (REVIEW_CONTEXT if kind == "review" else RECOVERY_CONTEXT) + str(number)
        latest = self._publication_latest(self.client.statuses(self.repository, head), context, allowed)
        nonce = secrets.token_hex(16)
        # Each intent owns its comment. Concurrent publishers cannot modify the
        # body named by another immutable native receipt.
        unique_marker = marker + ":" + nonce
        full = self.admission_policy.bind_comment(
            self.admission_policy.append_provenance(body, comment_marker(unique_marker)), actor
        )
        return {
            "schema": INTENT_SCHEMA,
            "source": self.repository_identity.record(),
            "issuer": int(actor.principal.native_id),
            "number": number,
            "head": head,
            "body": full,
            "digest": hashlib.sha256(full.encode()).hexdigest(),
            "marker": unique_marker,
            "kind": kind,
            "nonce": nonce,
            "predecessor": latest[0] if latest else 0,
        }

    def publish(self, intent):
        required = {
            "schema",
            "source",
            "issuer",
            "number",
            "head",
            "body",
            "digest",
            "marker",
            "kind",
            "nonce",
            "predecessor",
        }
        if (
            not isinstance(intent, dict)
            or set(intent) != required
            or intent["schema"] != INTENT_SCHEMA
            or intent["source"] != self.repository_identity.record()
            or intent["kind"] not in {"review", "recovery"}
            or not isinstance(intent["body"], str)
            or len(intent["body"].encode()) > 16 * 1024 * 1024
            or not isinstance(intent["marker"], str)
            or not intent["marker"]
            or not isinstance(intent["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", intent["nonce"]) is None
            or not intent["marker"].endswith(":" + intent["nonce"])
            or comment_marker(intent["marker"]) not in intent["body"]
            or type(intent["predecessor"]) is not int
            or intent["predecessor"] < 0
            or hashlib.sha256(intent["body"].encode()).hexdigest() != intent["digest"]
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
            or not self._binding_matches(intent["body"], actor)
        ):
            raise PermissionError("frozen native publication belongs to another issuer or source")
        pull = self.fetch_pr(self.repository.name, intent["number"])
        if pull.head_sha != intent["head"] or pull.state != "open":
            raise PublicationSuperseded("native publication head changed")
        context = (REVIEW_CONTEXT if intent["kind"] == "review" else RECOVERY_CONTEXT) + str(intent["number"])
        prefix = PUB_V2 + intent["nonce"] + ":"
        pending = prefix + "pending:" + intent["digest"] + ":" + str(intent["predecessor"])
        rows = self.client.statuses(self.repository, intent["head"])
        latest = self._publication_latest(rows, context, allowed)
        prior = [
            r
            for r in rows
            if r.get("context") == context
            and r.get("description") == pending
            and (r.get("creator") or {}).get("id") == intent["issuer"]
        ]
        if latest is not None:
            _id, stamp, _actor = latest
            if (
                prior
                and _id in {self._positive(r.get("id")) for r in prior}
                and (stamp.get("status") or stamp.get("state")) == "success"
            ):
                comment, state = self._publication_comment(
                    intent["number"],
                    rows,
                    context,
                    allowed,
                    self.client.comments(self.repository, intent["number"]),
                    own_actor=actor,
                )
                if state == "success" and comment is not None:
                    return str(comment["id"])
            if prior and _id not in {self._positive(r.get("id")) for r in prior}:
                raise PublicationSuperseded("newer native publication superseded this intent")
        if not prior and (latest[0] if latest else 0) != intent["predecessor"]:
            raise PublicationSuperseded("native publication changed since this intent was prepared")
        if latest is None or latest[1].get("description") != pending:
            self.client.request(
                "POST",
                f"repos/{self.repository.name}/statuses/{intent['head']}",
                {"context": context, "state": "pending", "description": pending},
            )
        latest = self._publication_latest(self.client.statuses(self.repository, intent["head"]), context, allowed)
        if latest is None or latest[1].get("description") != pending:
            raise PublicationSuperseded("another native intent acquired this publication predecessor")
        comment_id = self._upsert_comment(intent["number"], intent["body"], intent["marker"], actor)
        latest = self._publication_latest(self.client.statuses(self.repository, intent["head"]), context, allowed)
        if latest is None or latest[1].get("description") != pending:
            raise PublicationSuperseded("native publication intent changed")
        self.client.request(
            "POST",
            f"repos/{self.repository.name}/statuses/{intent['head']}",
            {
                "context": context,
                "state": "success",
                "description": prefix + comment_id + ":" + intent["digest"],
            },
        )
        return comment_id

    def clear_recovery(self, repo, number, head):
        self._positive(number)
        self._sha(head)
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = self.admission_policy.publishers.recoveries
        if actor.principal not in allowed:
            raise PermissionError("native actor cannot clear recovery holds")
        context = RECOVERY_CONTEXT + str(number)
        latest = self._publication_latest(self.client.statuses(self.repository, head), context, allowed)
        if (
            latest is not None
            and latest[2] == actor
            and latest[1].get("status") == "success"
            and latest[1].get("description") == PUB_DESCRIPTION + "clear"
        ):
            return
        self.client.request(
            "POST",
            f"repos/{self.repository.name}/statuses/{head}",
            {"context": context, "state": "success", "description": PUB_DESCRIPTION + "clear"},
        )

    def post_comment(self, repo, number, body, marker):
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        if policy.SCOREBOARD_MARKER in body:
            meta = policy.parse_meta(body)
            if not meta or not isinstance(meta.get("head_sha"), str):
                raise ValueError("native scoreboard has no accepted head")
            return self.publish(self.prepare_publication(repo, number, meta["head_sha"], body, marker, kind="review"))
        heads = set(policy.REBASE_MARKER_RE.findall(body)) | set(policy.STALLED_MARKER_RE.findall(body))
        if body.startswith("Merge-queue recovery for head `") and heads:
            if len(heads) != 1:
                raise ValueError("native recovery publication has ambiguous heads")
            return self.publish(self.prepare_publication(repo, number, heads.pop(), body, marker, kind="recovery"))
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
