"""Attributed per-PR squash chains for the AI merge queue.

A batch is a linear chain of single-parent commits on a recorded base, exactly one squash commit per
member PR. Each commit's message ends with the trailers that carry the member's identity through
every later rewrite:

    Source-PR: TauCetiProject/TauCeti#123
    Accepted-Head: <original accepted SHA>

Operations (importable functions and a CLI, `python -m tauceti_queue.chain <op> ... --json`):

- `build`: squash each member head onto the chain in order, as an explicit empty commit when the
  member adds nothing, stopping at the first conflict with the chain built so far.
- `replay`: rebase an existing chain onto a new base, keeping empty commits, messages and authorship.
- A member whose PR the base already carries (a commit in the base's new history whose subject ends
  in ` (#N)` or whose `Source-PR` trailer names it: an admin merge, or Bors landing it during the
  shadow period) is not cherry-picked by either: it becomes an empty commit with the same subject
  and trailers plus a one-line note naming the landing commit, so the chain keeps exactly one commit
  per member and never conflicts on changes that are already in the base.
- `validate`: check single-parent history from the declared base, the trailers, and the bijection
  between commits and recorded members (by PR identity, not position).
- `compare`: pair a prior and a replacement chain by PR identity and report provenance per member
  (clean or edited) plus the append-only message check that yields the agent's explanation.

Nothing here touches a checked-out branch, the index or the working tree: merges are computed with
`git merge-tree --write-tree`, commits are written with `git commit-tree`, and the only ref written is
`refs/tauceti-queue/<chain-id>/tip`. A conflict therefore leaves no partial state to clean up.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCE_PR_TRAILER = "Source-PR"
ACCEPTED_HEAD_TRAILER = "Accepted-Head"
COAUTHOR_TRAILER = "Co-authored-by"
REF_NAMESPACE = "refs/tauceti-queue"
DEFAULT_COMMITTER = ("TauCeti queue", "tauceti-queue@users.noreply.github.com")

# Per-member statuses in a build/replay result and provenances in a comparison.
CLEAN = "clean"
CONFLICT = "conflict"
PENDING = "pending"  # not attempted: an earlier member conflicted
EDITED = "edited"
MISSING = "missing"  # in the prior chain, absent from the replacement
ADDED = "added"  # in the replacement chain, absent from the prior

# Appended to the message of a member that landed outside the queue; `{sha}` is the landing commit.
LANDED_NOTE = "Landed on main as {sha} outside the queue; nothing left to apply."

_IDENT_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>]*)>\s*$")
_SUBJECT_PR_RE = re.compile(r"\(#(?P<number>\d+)\)\s*$")  # Bors's and GitHub's squash subject suffix
_LANDED_NOTE_RE = re.compile(r"^Landed on main as [0-9a-f]{7,40} outside the queue; nothing left to apply\.$", re.M)
_TRAILER_RE = re.compile(r"^(?P<token>[A-Za-z][A-Za-z0-9-]*):[ \t]+(?P<value>\S.*?)\s*$")
_CHAIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ChainError(Exception):
    """A usage or repository error: missing objects, a non-linear chain handed to replay, bad input.

    Expected outcomes (a conflicting member, a validation violation) are reported in result
    structures, never raised.
    """


# --- git -----------------------------------------------------------------------------------------


def git(repo: str | os.PathLike[str], *args: str, input: str | None = None, env: dict[str, str] | None = None) -> str:
    """Run git in `repo` with a controlled environment and return stdout; raise ChainError on failure."""
    result = _git(repo, *args, input=input, env=env)
    if result.returncode != 0:
        raise ChainError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _git(
    repo: str | os.PathLike[str], *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = dict(os.environ)
    full_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        input=input,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
        env=full_env,
        check=False,
    )


def object_exists(repo: str | os.PathLike[str], sha: str, kind: str = "commit") -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{{kind}}}").returncode == 0


def resolve_commit(repo: str | os.PathLike[str], rev: str) -> str:
    """The full SHA of a commit-ish, or ChainError when it does not exist."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if result.returncode != 0:
        raise ChainError(f"no such commit in {os.fspath(repo)}: {rev}")
    return result.stdout.strip()


def tree_of(repo: str | os.PathLike[str], commit: str) -> str:
    return git(repo, "rev-parse", f"{commit}^{{tree}}").strip()


def is_ancestor(repo: str | os.PathLike[str], ancestor: str, descendant: str) -> bool:
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in (0, 1):
        raise ChainError(f"git merge-base --is-ancestor failed: {result.stderr.strip()}")
    return result.returncode == 0


# --- member records -------------------------------------------------------------------------------


@dataclass
class Member:
    """An accepted PR revision: what the squash commit's message and attribution are built from.

    `author` and each co-author are `Name <email>` identities; a bare GitHub login is accepted and
    given the `login@users.noreply.github.com` address. An empty `author` means the accepted head's
    own author; `coauthors=None` means the other authors of the PR's commits (`()` means none).
    `evidence` is admission evidence passed through untouched; it never enters the commit.
    """

    repository: str
    number: int
    head_sha: str
    title: str = ""
    description: str = ""
    author: str = ""
    coauthors: tuple[str, ...] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def source_pr(self) -> str:
        return f"{self.repository}#{self.number}"

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["coauthors"] = None if self.coauthors is None else list(self.coauthors)
        data["source_pr"] = self.source_pr
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], repository: str | None = None) -> Member:
        """A member from a plain dict, either this module's own `to_dict` form or a journal entry.

        Journal entries name the PR as `pr` and may carry `body` instead of `description`; the
        repository comes from the entry or the `repository` argument. Unknown keys are kept as
        evidence.
        """
        data = dict(data)
        data.pop("source_pr", None)
        repo = data.pop("repository", None) or repository
        if not repo:
            raise ChainError("member record lacks a repository and none was given")
        number = data.pop("number", None)
        if number is None:
            number = data.pop("pr", None)
        if number is None:
            raise ChainError(f"member record for {repo} lacks a PR number")
        head = data.pop("head_sha", None) or data.pop("head", None)
        if not head:
            raise ChainError(f"member record for {repo}#{number} lacks an accepted head SHA")
        description = data.pop("description", None)
        if description is None:
            description = data.pop("body", None) or ""
        coauthors = data.pop("coauthors", None)
        evidence = dict(data.pop("evidence", None) or {})
        known = dict(
            title=data.pop("title", None) or "",
            author=data.pop("author", None) or "",
        )
        evidence.update(data)  # whatever else the snapshot carried
        return cls(
            repository=repo,
            number=int(number),
            head_sha=str(head),
            description=description,
            coauthors=None if coauthors is None else tuple(coauthors),
            evidence=evidence,
            **known,
        )


@dataclass
class CandidateMembers:
    """A Bors candidate journal entry turned into member records plus its declared base."""

    base_sha: str | None
    members: list[Member]


def members_from_candidate(snapshot: dict[str, Any], repository: str) -> CandidateMembers:
    """Member records from a candidate journal entry (`members[].pr`, `members[].head_sha`, `base_sha`).

    Title and author are usually absent there; the squash message then carries what is available,
    and the Source-PR and Accepted-Head trailers are always present.
    """
    members = [Member.from_dict(entry, repository) for entry in snapshot.get("members", [])]
    return CandidateMembers(base_sha=snapshot.get("base_sha"), members=members)


def member_key(member: Member | str) -> str:
    """The `owner/repo#N` identity a commit's Source-PR trailer must carry."""
    return member if isinstance(member, str) else member.source_pr


def load_members(data: Any, repository: str | None = None) -> CandidateMembers:
    """Members from parsed JSON: a list of member dicts or a candidate snapshot with a `members` list."""
    if isinstance(data, dict) and "members" in data:
        if repository is None:
            # A snapshot whose members all name their repository needs none; otherwise it is required.
            members = [Member.from_dict(entry) for entry in data["members"]]
            return CandidateMembers(base_sha=data.get("base_sha"), members=members)
        return members_from_candidate(data, repository)
    if isinstance(data, list):
        return CandidateMembers(base_sha=None, members=[Member.from_dict(entry, repository) for entry in data])
    raise ChainError("members must be a JSON list of member records or a candidate snapshot")


# --- messages and trailers ------------------------------------------------------------------------


def parse_trailers(message: str) -> dict[str, list[str]]:
    """Every `Token: value` line in the message, keyed by lower-cased token, in order of appearance.

    Trailers are looked for anywhere, not only in the final paragraph: an agent appends its
    explanation after the existing message, which puts the identity trailers before it.
    """
    found: dict[str, list[str]] = {}
    for line in message.splitlines():
        match = _TRAILER_RE.match(line)
        if match:
            found.setdefault(match["token"].lower(), []).append(match["value"])
    return found


def trailer_values(message: str, token: str) -> list[str]:
    return parse_trailers(message).get(token.lower(), [])


def unique_trailer(message: str, token: str) -> str | None:
    """The trailer's value when it appears with exactly one distinct value, else None."""
    values = set(trailer_values(message, token))
    return values.pop() if len(values) == 1 else None


def format_identity(identity: str) -> tuple[str, str]:
    """`Name <email>` -> (name, email); a bare login gets GitHub's noreply address."""
    match = _IDENT_RE.match(identity)
    if match:
        return match["name"] or match["email"].split("@")[0], match["email"]
    login = identity.strip().lstrip("@")
    return login, f"{login}@users.noreply.github.com"


def squash_message(member: Member, coauthors: Sequence[str]) -> str:
    """The squash commit message: subject, description, then Co-authored-by and identity trailers.

    The subject is Bors's `<title> (#<number>)`, so tooling that attributes commits to PRs by the
    subject suffix (TauCetiTools `lint-scope.sh`, GitHub's commit-to-PR links) keeps working.
    """
    suffix = f"(#{member.number})"
    subject = " ".join(member.title.split()) or f"{member.source_pr} at {member.head_sha[:12]}"
    if not subject.endswith(suffix):
        subject = f"{subject} {suffix}"
    paragraphs = [subject]
    if member.description.strip():
        paragraphs.append(member.description.strip())
    trailers = [f"{COAUTHOR_TRAILER}: {name} <{email}>" for name, email in map(format_identity, coauthors)]
    trailers.append(f"{SOURCE_PR_TRAILER}: {member.source_pr}")
    trailers.append(f"{ACCEPTED_HEAD_TRAILER}: {member.head_sha}")
    paragraphs.append("\n".join(trailers))
    return "\n\n".join(paragraphs) + "\n"


def landed_prs(repo: str | os.PathLike[str], since: str, until: str) -> dict[str, str]:
    """PRs landed by the commits in `since..until`, as `owner/repo#N` or `#N` -> the landing commit.

    A queue commit names its PR in the Source-PR trailer (`owner/repo#N`); a Bors or GitHub squash
    commit carries no trailer and is known by its ` (#N)` subject suffix, recorded as `#N`. The
    newest commit naming a PR wins. Every commit in the range is scanned, not only the first-parent
    line, so a squash commit reached through a merge counts.
    """
    landed: dict[str, str] = {}
    for record in git(repo, "log", "--format=%x1e%H%x1f%s%x1f%B", f"{since}..{until}").split("\x1e"):
        if not record.strip():
            continue
        sha, _, rest = record.partition("\x1f")
        subject, _, body = rest.partition("\x1f")
        sources = set(trailer_values(body, SOURCE_PR_TRAILER))
        if sources:
            for source in sources:
                landed.setdefault(source, sha.strip())
            continue
        match = _SUBJECT_PR_RE.search(subject)
        if match:
            landed.setdefault(f"#{match['number']}", sha.strip())
    return landed


def landed_commit(landed: dict[str, str], source_pr: str) -> str | None:
    """The commit from `landed_prs` that landed `owner/repo#N`, by trailer identity or subject number."""
    sha = landed.get(source_pr)
    if sha is None and "#" in source_pr:
        sha = landed.get("#" + source_pr.rsplit("#", 1)[1])
    return sha


def landed_message(message: str, sha: str) -> str:
    """The message with the landing note appended once; a message that already carries one is returned as is."""
    if _LANDED_NOTE_RE.search(message):
        return message
    return message.rstrip("\n") + "\n\n" + LANDED_NOTE.format(sha=sha) + "\n"


def message_preserved(prior: str, current: str) -> bool:
    """Append-only preservation: the prior message, modulo trailing whitespace, prefixes the current one."""
    return current.rstrip().startswith(prior.rstrip())


def appended_text(prior: str, current: str) -> str:
    """What was appended to a preserved message (the agent's explanation); empty if not preserved."""
    if not message_preserved(prior, current):
        return ""
    return current.rstrip()[len(prior.rstrip()) :].strip()


# --- reading commits ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainCommit:
    """A commit as read from the repository: enough to validate, replay and compare it."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    author_date: str  # "<unix seconds> <tz>" as stored by git
    message: str

    @property
    def source_pr(self) -> str | None:
        return unique_trailer(self.message, SOURCE_PR_TRAILER)

    @property
    def accepted_head(self) -> str | None:
        return unique_trailer(self.message, ACCEPTED_HEAD_TRAILER)

    @property
    def author(self) -> str:
        return f"{self.author_name} <{self.author_email}>"


def read_commit(repo: str | os.PathLike[str], sha: str) -> ChainCommit:
    """Parse a raw commit object (headers, including multi-line ones such as gpgsig, then message)."""
    raw = git(repo, "cat-file", "commit", sha)
    headers: list[tuple[str, str]] = []
    lines = raw.split("\n")
    index = 0
    while index < len(lines) and lines[index] != "":
        line = lines[index]
        if line.startswith(" ") and headers:
            key, value = headers[-1]
            headers[-1] = (key, value + "\n" + line[1:])
        else:
            key, _, value = line.partition(" ")
            headers.append((key, value))
        index += 1
    message = "\n".join(lines[index + 1 :])
    tree = next(value for key, value in headers if key == "tree")
    parents = tuple(value for key, value in headers if key == "parent")
    author_line = next(value for key, value in headers if key == "author")
    ident, _, date = author_line.rpartition("> ")
    name, email = format_identity(ident + ">")
    return ChainCommit(
        sha=sha, tree=tree, parents=parents, author_name=name, author_email=email, author_date=date, message=message
    )


def chain_commits(repo: str | os.PathLike[str], base: str, tip: str) -> list[ChainCommit]:
    """The first-parent commits after `base` up to `tip`, oldest first; ChainError if base is not an ancestor."""
    base, tip = resolve_commit(repo, base), resolve_commit(repo, tip)
    if not is_ancestor(repo, base, tip):
        raise ChainError(f"{base} is not an ancestor of {tip}")
    shas = git(repo, "rev-list", "--first-parent", "--reverse", f"{base}..{tip}").split()
    return [read_commit(repo, sha) for sha in shas]


# --- writing commits ------------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeOutcome:
    """`git merge-tree --write-tree`: the merged tree, with the conflicted paths when it was not clean."""

    tree: str
    conflict_paths: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.conflict_paths


def merge_trees(repo: str | os.PathLike[str], ours: str, theirs: str, merge_base: str | None = None) -> MergeOutcome:
    """Three-way merge `theirs` onto `ours` in the object store only; no index or working tree is touched.

    Without `merge_base` git computes it (a squash of a PR head onto the chain); with it this is a
    cherry-pick (replaying a chain commit, whose parent is the merge base, onto a new tip).
    """
    args = ["merge-tree", "--write-tree", "--name-only", "-z"]
    if merge_base:
        args.append(f"--merge-base={merge_base}")
    result = _git(repo, *args, ours, theirs)
    if result.returncode not in (0, 1):
        raise ChainError(f"git merge-tree failed: {result.stderr.strip()}")
    records = result.stdout.split("\0")
    tree = records[0].strip()
    if result.returncode == 0:
        return MergeOutcome(tree=tree)
    paths: list[str] = []
    for record in records[1:]:
        if record == "":
            break
        paths.append(record)
    return MergeOutcome(tree=tree, conflict_paths=tuple(paths))


def commit_tree(
    repo: str | os.PathLike[str],
    tree: str,
    parent: str,
    message: str,
    author: tuple[str, str],
    author_date: str | None = None,
    committer: tuple[str, str] = DEFAULT_COMMITTER,
) -> str:
    """Write a single-parent commit; like `commit --allow-empty`, an unchanged tree is still committed."""
    env = {
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    }
    if author_date:
        env["GIT_AUTHOR_DATE"] = author_date
    return git(repo, "commit-tree", tree, "-p", parent, "-F", "-", input=message, env=env).strip()


def chain_ref(chain_id: str) -> str:
    if not _CHAIN_ID_RE.match(chain_id) or ".." in chain_id or chain_id.endswith(".lock"):
        raise ChainError(f"chain id is not a valid ref component: {chain_id!r}")
    return f"{REF_NAMESPACE}/{chain_id}/tip"


def new_chain_id() -> str:
    return secrets.token_hex(6)


def update_chain_ref(repo: str | os.PathLike[str], chain_id: str, tip: str) -> str:
    ref = chain_ref(chain_id)
    git(repo, "update-ref", ref, tip)
    return ref


def delete_chain_ref(repo: str | os.PathLike[str], chain_id: str) -> None:
    ref = chain_ref(chain_id)
    if _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
        git(repo, "update-ref", "-d", ref)


# --- build and replay -----------------------------------------------------------------------------


@dataclass
class CommitRecord:
    """One member's outcome in a build or replay."""

    source_pr: str
    accepted_head: str | None
    sha: str | None
    status: str  # CLEAN, CONFLICT or PENDING
    empty: bool = False
    conflict_paths: tuple[str, ...] = ()
    landed_on_base: str | None = None  # the base's own commit that landed this PR outside the queue

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["conflict_paths"] = list(self.conflict_paths)
        return data


@dataclass
class ChainResult:
    """A built or replayed chain: `tip` is the last clean commit (the base when none was made).

    `status` is CLEAN when every member was committed, CONFLICT otherwise; the conflicting member's
    record carries the paths, and the members after it are PENDING. The chain up to `tip` is
    retained under `ref`. A member the base already carries is recorded with `landed_on_base`,
    whether it was committed (as an empty commit) or is still pending behind a conflict.
    """

    status: str
    chain_id: str
    ref: str
    base: str
    tip: str
    commits: list[CommitRecord]

    @property
    def conflict(self) -> CommitRecord | None:
        return next((record for record in self.commits if record.status == CONFLICT), None)

    @property
    def shas(self) -> list[str]:
        return [record.sha for record in self.commits if record.sha]

    @property
    def landed_on_base(self) -> list[dict[str, str]]:
        """The members the base already carries, in chain order: `{source_pr, commit}` per member."""
        return [
            {"source_pr": record.source_pr, "commit": record.landed_on_base}
            for record in self.commits
            if record.landed_on_base
        ]

    def to_dict(self) -> dict[str, Any]:
        conflict = self.conflict
        return {
            "status": self.status,
            "chain_id": self.chain_id,
            "ref": self.ref,
            "base": self.base,
            "tip": self.tip,
            "commits": [record.to_dict() for record in self.commits],
            "conflict": None if conflict is None else conflict.to_dict(),
            "landed_on_base": self.landed_on_base,
        }


def _author_and_coauthors(
    repo: str | os.PathLike[str], member: Member, since: str
) -> tuple[tuple[str, str], list[str]]:
    """The squash author (member record, else the head's author) and co-authors (record, else other authors)."""
    if member.author:
        author = format_identity(member.author)
    else:
        name, email = git(repo, "log", "-1", "--format=%an%x00%ae", member.head_sha).rstrip("\n").split("\0")
        author = (name, email)
    if member.coauthors is not None:
        return author, list(member.coauthors)
    coauthors: list[str] = []
    seen = {author[1].lower()}
    for line in git(repo, "log", "--format=%an%x00%ae", f"{since}..{member.head_sha}").splitlines():
        name, _, email = line.partition("\0")
        if email.lower() not in seen:
            seen.add(email.lower())
            coauthors.append(f"{name} <{email}>")
    return author, coauthors


def build(
    repo: str | os.PathLike[str],
    base: str,
    members: Sequence[Member],
    chain_id: str | None = None,
    committer: tuple[str, str] = DEFAULT_COMMITTER,
    since: str | None = None,
) -> ChainResult:
    """Squash each member onto the chain in order, as one explicit commit each, under a private ref.

    A member whose squash changes nothing (already in the base or an earlier member) becomes an empty
    commit, never a dropped one. A member whose PR a commit in `since..base` names (see `landed_prs`)
    is not squashed at all: it becomes an empty commit carrying the landing note, so a PR that
    landed outside the queue cannot conflict with the other changes that landed beside it. `since`
    defaults to the common ancestor of the base and every member head. At the first conflict the
    result records the member and its paths and returns the chain built so far; nothing partial is
    left behind.
    """
    chain_id = chain_id or new_chain_id()
    ref = chain_ref(chain_id)
    base = resolve_commit(repo, base)
    keys = [member.source_pr for member in members]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ChainError(f"duplicate members: {', '.join(duplicates)}")
    for member in members:
        if not object_exists(repo, member.head_sha):
            raise ChainError(f"accepted head of {member.source_pr} is not in the repository: {member.head_sha}")
    if since is None and members:
        since = git(repo, "merge-base", "--octopus", base, *(member.head_sha for member in members)).strip() or None
    landed = landed_prs(repo, since, base) if since else {}

    records: list[CommitRecord] = []
    tip = base
    tip_tree = tree_of(repo, tip)
    status = CLEAN
    for index, member in enumerate(members):
        landing = landed_commit(landed, member.source_pr)
        if landing:
            author, coauthors = _author_and_coauthors(
                repo, member, since=git(repo, "merge-base", tip, member.head_sha).strip()
            )
            message = landed_message(squash_message(member, coauthors), landing)
            sha = commit_tree(repo, tip_tree, tip, message, author, committer=committer)
            records.append(
                CommitRecord(member.source_pr, member.head_sha, sha, CLEAN, empty=True, landed_on_base=landing)
            )
            tip = sha
            continue
        outcome = merge_trees(repo, tip, member.head_sha)
        if not outcome.clean:
            records.append(
                CommitRecord(member.source_pr, member.head_sha, None, CONFLICT, conflict_paths=outcome.conflict_paths)
            )
            records.extend(
                CommitRecord(m.source_pr, m.head_sha, None, PENDING, landed_on_base=landed_commit(landed, m.source_pr))
                for m in members[index + 1 :]
            )
            status = CONFLICT
            break
        author, coauthors = _author_and_coauthors(
            repo, member, since=git(repo, "merge-base", tip, member.head_sha).strip()
        )
        sha = commit_tree(repo, outcome.tree, tip, squash_message(member, coauthors), author, committer=committer)
        records.append(CommitRecord(member.source_pr, member.head_sha, sha, CLEAN, empty=outcome.tree == tip_tree))
        tip, tip_tree = sha, outcome.tree
    update_chain_ref(repo, chain_id, tip)
    return ChainResult(status=status, chain_id=chain_id, ref=ref, base=base, tip=tip, commits=records)


def replay(
    repo: str | os.PathLike[str],
    base: str,
    tip: str,
    onto: str,
    chain_id: str | None = None,
    committer: tuple[str, str] = DEFAULT_COMMITTER,
) -> ChainResult:
    """Rebase the chain `base..tip` onto `onto`, one commit per commit, under a new private ref.

    Messages, authorship and trailers are copied; an empty commit stays an empty commit, and a commit
    whose change is already in the new base becomes one. A commit whose PR the new base's history
    `base..onto` names (see `landed_prs`) is not cherry-picked: it becomes an empty commit with the
    same message plus the landing note, so a member that landed outside the queue cannot conflict with
    the changes that landed beside it. Conflicts are reported like `build`'s. The chain must already
    be linear (see `validate`); a merge commit is a ChainError.
    """
    chain_id = chain_id or new_chain_id()
    ref = chain_ref(chain_id)
    onto = resolve_commit(repo, onto)
    commits = chain_commits(repo, base, tip)
    for commit in commits:
        if len(commit.parents) != 1:
            raise ChainError(f"{commit.sha} has {len(commit.parents)} parents; replay needs a linear chain")
    landed = landed_prs(repo, commits[0].parents[0], onto) if commits else {}

    def landing_of(commit: ChainCommit) -> str | None:
        return landed_commit(landed, commit.source_pr) if commit.source_pr else None

    records: list[CommitRecord] = []
    new_tip = onto
    new_tip_tree = tree_of(repo, onto)
    status = CLEAN
    for index, commit in enumerate(commits):
        key = commit.source_pr or commit.sha
        landing = landing_of(commit)
        if landing:
            outcome = MergeOutcome(tree=new_tip_tree)
            message = landed_message(commit.message, landing)
        else:
            outcome = merge_trees(repo, new_tip, commit.sha, merge_base=commit.parents[0])
            message = commit.message
        if not outcome.clean:
            records.append(
                CommitRecord(key, commit.accepted_head, None, CONFLICT, conflict_paths=outcome.conflict_paths)
            )
            records.extend(
                CommitRecord(c.source_pr or c.sha, c.accepted_head, None, PENDING, landed_on_base=landing_of(c))
                for c in commits[index + 1 :]
            )
            status = CONFLICT
            break
        sha = commit_tree(
            repo,
            outcome.tree,
            new_tip,
            message,
            (commit.author_name, commit.author_email),
            author_date=commit.author_date,
            committer=committer,
        )
        records.append(
            CommitRecord(
                key, commit.accepted_head, sha, CLEAN, empty=outcome.tree == new_tip_tree, landed_on_base=landing
            )
        )
        new_tip, new_tip_tree = sha, outcome.tree
    update_chain_ref(repo, chain_id, new_tip)
    return ChainResult(status=status, chain_id=chain_id, ref=ref, base=onto, tip=new_tip, commits=records)


# --- validate -------------------------------------------------------------------------------------


@dataclass
class Violation:
    code: str
    detail: str
    commit: str | None = None
    source_pr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ValidationReport:
    ok: bool
    base: str
    tip: str
    expected_members: int
    commits: list[CommitRecord]
    violations: list[Violation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "base": self.base,
            "tip": self.tip,
            "expected_members": self.expected_members,
            "commit_count": len(self.commits),
            "commits": [record.to_dict() for record in self.commits],
            "violations": [violation.to_dict() for violation in self.violations],
        }


def validate(repo: str | os.PathLike[str], base: str, tip: str, members: Iterable[Member | str]) -> ValidationReport:
    """Check a chain against its recorded members and list every violation rather than stopping at one.

    Checks: base and tip exist; base is an ancestor of tip and the first commit's parent is base;
    every commit has a single parent; every commit carries one Source-PR and one Accepted-Head trailer;
    the Source-PR values and the member list are in bijection (order is irrelevant, so a replacement
    chain in another order passes); the count matches; and where a Member record carries a head SHA,
    the Accepted-Head trailer agrees with it.
    """
    members = list(members)
    expected = {member_key(member): member for member in members}
    violations: list[Violation] = []
    duplicates_declared = len(expected) != len(members)
    if duplicates_declared:
        violations.append(Violation("duplicate-member", "the recorded member list names a PR more than once"))

    try:
        base_sha, tip_sha = resolve_commit(repo, base), resolve_commit(repo, tip)
    except ChainError as error:
        return ValidationReport(
            False, base, tip, len(expected), [], violations + [Violation("missing-object", str(error))]
        )

    if not is_ancestor(repo, base_sha, tip_sha):
        # Without a path from base to tip there is no chain whose membership could be assessed.
        violations.append(Violation("wrong-base", f"declared base {base_sha} is not an ancestor of tip {tip_sha}"))
        return ValidationReport(False, base_sha, tip_sha, len(expected), [], violations)
    commits = chain_commits(repo, base_sha, tip_sha)
    if commits and commits[0].parents[:1] != (base_sha,):
        violations.append(
            Violation(
                "wrong-base",
                f"first commit's parent is {commits[0].parents[0] if commits[0].parents else 'none'}, not the declared base {base_sha}",
                commit=commits[0].sha,
            )
        )

    records: list[CommitRecord] = []
    seen: dict[str, str] = {}
    parent_tree = tree_of(repo, base_sha) if commits else None
    for commit in commits:
        if len(commit.parents) != 1:
            violations.append(
                Violation("merge-commit", f"{commit.sha} has {len(commit.parents)} parents", commit=commit.sha)
            )
        source_values = set(trailer_values(commit.message, SOURCE_PR_TRAILER))
        head_values = set(trailer_values(commit.message, ACCEPTED_HEAD_TRAILER))
        source_pr = source_values.pop() if len(source_values) == 1 else None
        accepted_head = head_values.pop() if len(head_values) == 1 else None
        if source_pr is None:
            code = "missing-source-pr" if not source_values else "ambiguous-source-pr"
            violations.append(
                Violation(code, f"{commit.sha} has {len(source_values)} distinct Source-PR trailers", commit=commit.sha)
            )
        if accepted_head is None:
            code = "missing-accepted-head" if not head_values else "ambiguous-accepted-head"
            violations.append(
                Violation(
                    code, f"{commit.sha} has {len(head_values)} distinct Accepted-Head trailers", commit=commit.sha
                )
            )
        if source_pr is not None:
            if source_pr in seen:
                violations.append(
                    Violation(
                        "duplicate-member",
                        f"{source_pr} is also carried by {seen[source_pr]}",
                        commit=commit.sha,
                        source_pr=source_pr,
                    )
                )
            seen.setdefault(source_pr, commit.sha)
            if source_pr not in expected:
                violations.append(
                    Violation(
                        "unknown-member",
                        f"{commit.sha} carries {source_pr}, which is not a recorded member",
                        commit=commit.sha,
                        source_pr=source_pr,
                    )
                )
            else:
                member = expected[source_pr]
                if isinstance(member, Member) and accepted_head is not None and accepted_head != member.head_sha:
                    violations.append(
                        Violation(
                            "accepted-head-mismatch",
                            f"{commit.sha} records Accepted-Head {accepted_head}; the member's accepted head is {member.head_sha}",
                            commit=commit.sha,
                            source_pr=source_pr,
                        )
                    )
        empty = len(commit.parents) == 1 and commit.tree == parent_tree
        records.append(CommitRecord(source_pr or commit.sha, accepted_head, commit.sha, CLEAN, empty=empty))
        parent_tree = commit.tree

    for source_pr in expected:
        if source_pr not in seen:
            violations.append(Violation("missing-member", f"no commit carries {source_pr}", source_pr=source_pr))
    if len(commits) != len(expected):
        violations.append(Violation("count-mismatch", f"{len(commits)} commits for {len(expected)} members"))
    return ValidationReport(not violations, base_sha, tip_sha, len(expected), records, violations)


# --- compare --------------------------------------------------------------------------------------


@dataclass
class MemberProvenance:
    """How a member fared between a prior chain and its replacement.

    `clean`: the replacement commit is exactly the mechanical replay of the prior commit onto its new
    parent and the message is unchanged. `edited`: the content differs from that replay (including a
    replay that would have conflicted) or text was appended to the message. `explanation` is the
    appended text when the prior message was preserved as a prefix.
    """

    source_pr: str
    provenance: str  # CLEAN, EDITED, MISSING or ADDED
    prior_sha: str | None
    new_sha: str | None
    content_changed: bool
    message_preserved: bool
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ComparisonReport:
    ok: bool
    members: list[MemberProvenance]
    violations: list[Violation]

    @property
    def edited(self) -> list[MemberProvenance]:
        return [member for member in self.members if member.provenance == EDITED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "members": [member.to_dict() for member in self.members],
            "violations": [violation.to_dict() for violation in self.violations],
        }


def _index_by_pr(
    repo: str | os.PathLike[str], base: str, tip: str, label: str, violations: list[Violation]
) -> dict[str, ChainCommit]:
    indexed: dict[str, ChainCommit] = {}
    for commit in chain_commits(repo, base, tip):
        if len(commit.parents) != 1:
            violations.append(
                Violation(
                    "merge-commit", f"{label} commit {commit.sha} has {len(commit.parents)} parents", commit=commit.sha
                )
            )
            continue
        source_pr = commit.source_pr
        if source_pr is None:
            violations.append(
                Violation(
                    "missing-source-pr",
                    f"{label} commit {commit.sha} has no unique Source-PR trailer",
                    commit=commit.sha,
                )
            )
        elif source_pr in indexed:
            violations.append(
                Violation(
                    "duplicate-member",
                    f"{label} chain carries {source_pr} twice",
                    commit=commit.sha,
                    source_pr=source_pr,
                )
            )
        else:
            indexed[source_pr] = commit
    return indexed


def same_change(repo: str | os.PathLike[str], prior: ChainCommit, current: ChainCommit) -> bool:
    """Whether `current` is exactly what replaying `prior` onto `current`'s parent would have produced."""
    outcome = merge_trees(repo, current.parents[0], prior.sha, merge_base=prior.parents[0])
    return outcome.clean and outcome.tree == current.tree


def compare(repo: str | os.PathLike[str], prior_base: str, prior_tip: str, base: str, tip: str) -> ComparisonReport:
    """Pair the prior and replacement chains by PR identity and report provenance per member.

    Violations: `member-missing` (a prior member has no replacement commit), `member-added` (a
    replacement commit for no prior member), `message-not-preserved` (the prior message is not a
    prefix of the replacement's), plus the structural ones shared with `validate`.
    """
    violations: list[Violation] = []
    before = _index_by_pr(repo, prior_base, prior_tip, "prior", violations)
    after = _index_by_pr(repo, base, tip, "replacement", violations)
    members: list[MemberProvenance] = []
    for source_pr, prior in before.items():
        current = after.get(source_pr)
        if current is None:
            violations.append(
                Violation("member-missing", f"{source_pr} has no commit in the replacement chain", source_pr=source_pr)
            )
            members.append(MemberProvenance(source_pr, MISSING, prior.sha, None, False, False, ""))
            continue
        changed = not same_change(repo, prior, current)
        preserved = message_preserved(prior.message, current.message)
        if not preserved:
            violations.append(
                Violation(
                    "message-not-preserved",
                    f"{source_pr}: the prior message is not a prefix of {current.sha}'s message",
                    commit=current.sha,
                    source_pr=source_pr,
                )
            )
        unchanged_message = preserved and current.message.rstrip() == prior.message.rstrip()
        provenance = CLEAN if not changed and unchanged_message else EDITED
        members.append(
            MemberProvenance(
                source_pr,
                provenance,
                prior.sha,
                current.sha,
                changed,
                preserved,
                appended_text(prior.message, current.message),
            )
        )
    for source_pr, current in after.items():
        if source_pr not in before:
            violations.append(
                Violation(
                    "member-added",
                    f"{source_pr} is not a member of the prior chain",
                    commit=current.sha,
                    source_pr=source_pr,
                )
            )
            members.append(MemberProvenance(source_pr, ADDED, None, current.sha, True, False, ""))
    return ComparisonReport(not violations, members, violations)


# --- CLI ------------------------------------------------------------------------------------------


def _read_members(path: str, repository: str | None) -> CandidateMembers:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ChainError(f"members file is not JSON: {error}") from error
    return load_members(data, repository)


def _summary(result: ChainResult | ValidationReport | ComparisonReport) -> str:
    if isinstance(result, ChainResult):
        lines = [f"{result.status}: chain {result.chain_id} base {result.base} tip {result.tip} ({result.ref})"]
        for record in result.commits:
            note = " (empty)" if record.empty else ""
            if record.landed_on_base:
                note = f" (empty; landed on the base as {record.landed_on_base[:12]})"
            if record.status == CONFLICT:
                note = f" conflicts in: {', '.join(record.conflict_paths) or '(unknown paths)'}"
            lines.append(f"  {record.status:8} {record.sha or '-':40} {record.source_pr}{note}")
        return "\n".join(lines)
    if isinstance(result, ValidationReport):
        lines = [
            f"{'ok' if result.ok else 'invalid'}: {len(result.commits)} commits for {result.expected_members} members"
        ]
        lines.extend(f"  {v.code}: {v.detail}" for v in result.violations)
        return "\n".join(lines)
    lines = [f"{'ok' if result.ok else 'violations'}: {len(result.members)} members, {len(result.edited)} edited"]
    for member in result.members:
        lines.append(
            f"  {member.provenance:7} {member.source_pr}" + (f": {member.explanation}" if member.explanation else "")
        )
    lines.extend(f"  {v.code}: {v.detail}" for v in result.violations)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tauceti_queue.chain", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True, help="path to the Git repository holding base and member heads")
    common.add_argument("--json", action="store_true", help="print the result structure as JSON")

    p = sub.add_parser("build", parents=[common], help="squash members onto a base as an attributed chain")
    p.add_argument("--base", help="base commit; defaults to the snapshot's base_sha")
    p.add_argument(
        "--members", required=True, help="JSON file (or - for stdin): member records or a candidate snapshot"
    )
    p.add_argument("--repository", help="owner/repo for snapshot members that do not name one")
    p.add_argument("--chain-id", help="ref component under refs/tauceti-queue/; random when omitted")
    p.add_argument(
        "--since",
        help="commits in since..base that name a member PR make it an empty commit; "
        "defaults to the common ancestor of the base and the member heads",
    )

    p = sub.add_parser("replay", parents=[common], help="rebase an existing chain onto a new base")
    p.add_argument("--base", required=True)
    p.add_argument("--tip", required=True)
    p.add_argument("--onto", required=True, help="the new base commit")
    p.add_argument("--chain-id")

    p = sub.add_parser("validate", parents=[common], help="check a chain against its recorded members")
    p.add_argument("--base", required=True)
    p.add_argument("--tip", required=True)
    p.add_argument(
        "--members", required=True, help="JSON file (or - for stdin): member records or a candidate snapshot"
    )
    p.add_argument("--repository")

    p = sub.add_parser("compare", parents=[common], help="provenance of a replacement chain against a prior one")
    p.add_argument("--prior-base", required=True)
    p.add_argument("--prior-tip", required=True)
    p.add_argument("--base", required=True, help="the replacement chain's base")
    p.add_argument("--tip", required=True, help="the replacement chain's tip")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            candidate = _read_members(args.members, args.repository)
            base = args.base or candidate.base_sha
            if not base:
                raise ChainError("no --base given and the members file has no base_sha")
            result: ChainResult | ValidationReport | ComparisonReport = build(
                args.repo, base, candidate.members, args.chain_id, since=args.since
            )
            ok = result.status == CLEAN
        elif args.command == "replay":
            result = replay(args.repo, args.base, args.tip, args.onto, args.chain_id)
            ok = result.status == CLEAN
        elif args.command == "validate":
            result = validate(args.repo, args.base, args.tip, _read_members(args.members, args.repository).members)
            ok = result.ok
        else:
            result = compare(args.repo, args.prior_base, args.prior_tip, args.base, args.tip)
            ok = result.ok
    except ChainError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2) if args.json else _summary(result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
