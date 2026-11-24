"""Verify a queue-closed target against its configured source and content branch.

A Bors-looking title is only a discovery hint. GitHub requires the queue App's
canonical landing comment and ancestry. Native comments only nominate a commit:
the commit on the source's base must bind the qualified PR in its queue trailers.
All I/O is supplied by the selected source adapter; failures establish no landing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import quote

_SHA = re.compile(r"[0-9a-f]{40}")
_RECORD = re.compile(
    r"(?P<record><!-- tauceti-queue landed:(?P<subject>\S+#[1-9][0-9]*):[1-9][0-9]*:"
    r"(?P<sha>[0-9a-f]{40}) -->)"
    r"\r?\n\r?\n<!-- tauceti-queue (?P=record) -->\s*\Z"
)


def github_queue_comment(comment: dict) -> bool:
    """The queue App's source-authenticated REST or GraphQL author identity."""
    author = comment.get("user") or comment.get("author")
    if not isinstance(author, dict):
        return False
    login = author.get("login")
    if author.get("__typename") == "Bot" and isinstance(login, str) and not login.endswith("[bot]"):
        login += "[bot]"
    return isinstance(login, str) and login.casefold() == "tauceti-queue[bot]"


def landing_candidates(comments: Iterable[dict], subject: str, branch: str, *, native: bool = False) -> tuple[str, ...]:
    """Commit IDs nominated by complete canonical records for this exact PR/base.

    Native candidates are untrusted until their landed commit's identity is read.
    A quoted marker in a queue reply cannot pass the narrative and terminal wrapper.
    """
    if not subject or not branch or any(ch in subject + branch for ch in "\r\n"):
        return ()
    prefix = f"Pull request successfully merged into {branch}."
    found = []
    for comment in comments:
        if not isinstance(comment, dict) or (not native and not github_queue_comment(comment)):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not (body.startswith(prefix + "\n") or body.startswith(prefix + "\r\n")):
            continue
        record = _RECORD.search(body)
        # Native repository names, origin and numeric repository ID are all part
        # of the caller-supplied subject; never project them to an unqualified #N.
        if record and record["subject"].casefold() == subject.casefold() and record["sha"] not in found:
            found.append(record["sha"])
    return tuple(found)


def commit_on_base(read_json: Callable[[str], object], repository: str, branch: str, sha: str, *, native=False) -> bool:
    """Prove ancestry against one immutable observation of the configured branch.

    Forgejo Compare has no GitHub status, and zero commits can also mean no common
    history. Distinct native tips therefore need a nonempty reverse comparison.
    The caller owns caching, authorization and the API origin of ``read_json``.
    """
    try:
        if not _SHA.fullmatch(sha) or not branch or len(repository.split("/")) != 2:
            return False
        path = "repos/" + "/".join(quote(part, safe="") for part in repository.split("/"))
        row = read_json(f"{path}/branches/{quote(branch, safe='')}")
        if not isinstance(row, dict) or row.get("name") != branch or not isinstance(row.get("commit"), dict):
            return False
        tip = row["commit"].get("id" if native else "sha")
        if not isinstance(tip, str) or not _SHA.fullmatch(tip):
            return False
        if sha == tip:
            return True
        suffix = "?files=false&verification=false" if native else ""
        forward = read_json(f"{path}/compare/{tip}...{sha}{suffix}")
        if not isinstance(forward, dict):
            return False
        if not native:
            return forward.get("status") in ("behind", "identical")
        if (
            type(forward.get("total_commits")) is not int
            or forward["total_commits"] != 0
            or forward.get("commits") != []
        ):
            return False
        reverse = read_json(f"{path}/compare/{sha}...{tip}{suffix}")
        return (
            isinstance(reverse, dict)
            and type(reverse.get("total_commits")) is int
            and reverse["total_commits"] > 0
            and isinstance(reverse.get("commits"), list)
            and bool(reverse["commits"])
        )
    except Exception:  # unavailable or malformed proof cannot complete a milestone
        return False


def native_commit_binds(read_json: Callable[[str], object], repository: str, sha: str, subject: str) -> bool:
    """A nonempty queue squash uniquely binds this exact source-qualified PR.

    A contributor can copy a landing comment, but cannot put an arbitrary commit
    on canonical base. A withdrawn member's empty queue commit must not count.
    Identity trailers may precede the queue agent's appended explanation.
    """
    try:
        path = "repos/" + "/".join(quote(part, safe="") for part in repository.split("/"))
        row = read_json(f"{path}/git/commits/{sha}")
        if not isinstance(row, dict) or row.get("sha") != sha or not isinstance(row.get("message"), str):
            return False
        sources = {value.strip() for value in re.findall(r"(?im)^Source-PR:[ \t]*([^\r\n]*)", row["message"])}
        heads = {value.strip() for value in re.findall(r"(?im)^Accepted-Head:[ \t]*([^\r\n]*)", row["message"])}
        if (
            {value.casefold() for value in sources} != {subject.casefold()}
            or len(heads) != 1
            or not _SHA.fullmatch(next(iter(heads)))
        ):
            return False
        parents = row.get("parents")
        tree = row.get("tree")
        if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
            return False
        parent = parents[0].get("sha")
        if not isinstance(parent, str) or not _SHA.fullmatch(parent) or not isinstance(tree, dict):
            return False
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            return False
        before = read_json(f"{path}/git/commits/{parent}")
        before_tree = before.get("tree") if isinstance(before, dict) and before.get("sha") == parent else None
        old = before_tree.get("sha") if isinstance(before_tree, dict) else None
        return isinstance(old, str) and bool(_SHA.fullmatch(old)) and old != tree_sha
    except Exception:
        return False


def queue_landed(
    repository: str,
    number: int,
    branch: str,
    *,
    read_comments: Callable[[], Iterable[dict]],
    read_json: Callable[[str], object],
    native: bool = False,
    subject: str | None = None,
    alternate_subjects: tuple[str, ...] = (),
) -> bool:
    """Prove a queue landing using source-bound, optionally cached adapter reads.

    GitHub defaults to ``owner/repo#N``. Native callers must supply the queue's
    full ``forgejo:<origin>/owner/repo@repository-id#N`` identity. Real API-native
    merges should be accepted by the adapter before calling this helper.
    """
    if type(number) is not int or number <= 0 or (native and subject is None):
        return False
    subject = subject or f"{repository}#{number}"
    subjects = (subject, *alternate_subjects)
    if any(not isinstance(value, str) or not value.endswith(f"#{number}") for value in subjects):
        return False
    try:
        comments = read_comments()
        if not isinstance(comments, (list, tuple)):
            return False
        observed = {}

        def read(path):
            if path not in observed:
                observed[path] = read_json(path)
            return observed[path]

        for expected in subjects:
            for sha in landing_candidates(comments, expected, branch, native=native):
                if (not native or native_commit_binds(read, repository, sha, expected)) and commit_on_base(
                    read, repository, branch, sha, native=native
                ):
                    return True
    except Exception:
        return False
    return False
