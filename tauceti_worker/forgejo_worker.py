"""Native Forgejo operations used by a selected Worker unit."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tauceti_tools.forge import ForgeError, Incomplete, RepositoryRef

from .forgejo_reads import ForgejoReadSource


class ForgejoWorkerSource(ForgejoReadSource):
    worker_capable = True

    def __init__(self, identity, api, *, token_file, cache=None):
        super().__init__(identity.repository, api, cache=cache)
        self.identity = identity
        self.token_file = Path(token_file) if token_file else None
        self.anonymous_discovery = token_file is None
        self.repo = self.repository.name

    def validate_binding(self):
        row = self.api.repository(self.repository)
        if (
            type(row.get("id")) is not int
            or row["id"] != self.identity.native_id
            or row.get("full_name", "").casefold() != self.repo.casefold()
            or row.get("archived") is not False
        ):
            raise Incomplete("the Forgejo worker repository binding changed")
        return row

    def writable_repository(self):
        """Use an explicit writable fork, or authorized same-repository branches."""
        from tauceti_tools.forge_write import ForgejoWriter

        actor = self.authenticated_actor()
        if actor is None:
            raise Incomplete("Forgejo author identity unavailable")
        override = os.environ.get("TAUCETI_FORGEJO_FORK")
        if override:
            target = RepositoryRef(self.repository.forge, override)
            row = self.api.repository(target)
            if target != self.repository and (
                row.get("fork") is not True or row.get("parent", {}).get("id") != self.identity.native_id
            ):
                raise Incomplete("the configured Forgejo fork has a different parent")
        else:
            row = self.validate_binding()
            target = self.repository
            if row.get("permissions", {}).get("push") is not True:
                target = RepositoryRef(self.repository.forge, actor.login + "/" + self.repo.split("/")[1])
                try:
                    row = self.api.repository(target)
                except ForgeError as error:
                    if error.status != 404:
                        raise
                    ForgejoWriter(self.api, expected_user_id=actor.principal.native_id).fork(self.repository)
                    row = self.api.repository(target)
                if row.get("fork") is not True or row.get("parent", {}).get("id") != self.identity.native_id:
                    raise Incomplete("the Forgejo fork parent could not be verified")
        if row.get("permissions", {}).get("push") is not True or row.get("archived"):
            raise Incomplete("the Forgejo contributor cannot push the selected branch repository")
        return target

    def _gh(self, args):
        from .source_cli import execute

        return execute(args, self)

    def live_pull(self, pr):
        return self._read("live_pull", [pr], lambda: self.api.pull(self.repository, pr), fresh=True, number=pr)

    def pr_list(self, fields, *, author=None, state="open"):
        rows = self._read(
            "pr_list",
            [fields, author, state],
            lambda: self.reader.pulls(self.repository, fields, state=state, author=author),
        )
        if rows is None:
            raise Incomplete("Forgejo PR listing is unavailable")
        return rows

    def merged_prs(self, search, *, limit=200):
        """`GitHub.merged_prs` on this source: number, title, body and URL of merged pull requests whose
        body contains every quoted phrase of the GitHub-style `search` (its qualifiers are not
        native and are ignored). A phrase other than the target-marker tag goes to the native
        keyword search, which reaches a pull request of any age; otherwise the `limit` most
        recently created merged pull requests are read. Raises SourceUnavailable when it fails."""
        import urllib.parse

        from .config import SourceUnavailable
        from .targets import MARKER_TAG, search_phrases

        phrases = search_phrases(search)
        keyword = next((p for p in phrases if p != MARKER_TAG), None)

        def fetch():
            if keyword is None:
                rows = self.api.pulls(self.repository, state="merged", limit=limit)
            else:
                query = urllib.parse.urlencode({"type": "pulls", "state": "closed", "q": keyword})
                rows = []
                for batch in self.api.pages(f"repos/{self.repository.name}/issues?{query}"):
                    rows += [r for r in batch if (r.get("pull_request") or {}).get("merged") is True]
                    if len(rows) >= limit:
                        break
            return [
                {
                    "number": r["number"],
                    "title": r.get("title") or "",
                    "body": r.get("body") or "",
                    "url": self.repository.pull_url(r["number"]),
                }
                for r in rows[:limit]
                if all(p in (r.get("body") or "") for p in phrases)
            ]

        rows = self._read("merged_prs", [search, limit], fetch)
        if rows is None:
            raise SourceUnavailable("Forgejo merged pull request listing is unavailable")
        return rows

    def pr_diff(self, pr):
        """`GitHub.pr_diff` on this source: the unified diff of the pull request, open or merged, from
        its merge base to its head, made from both commits fetched into a scratch repository (the
        API returns no patch text). The merge base is the one Forgejo recorded for the pull request,
        since after a merge `base.sha` is the target branch's tip, which already holds the head; it
        is computed from `base.sha` only where none is recorded. None when unreadable."""
        import subprocess
        import tempfile

        row = self.live_pull(pr)
        if row is None:
            return None
        recorded = row.get("merge_base") or ""
        base = recorded if re.fullmatch(r"[0-9a-f]{40}", recorded) else row["base"]["sha"]
        head = row["head"]["sha"]
        if not all(re.fullmatch(r"[0-9a-f]{40}", value or "") for value in (base, head)):
            return None
        with tempfile.TemporaryDirectory(prefix="tauceti-diff-") as work:

            def git(*arguments):
                return subprocess.run(
                    ["git", "--no-replace-objects", "-C", work, "-c", "core.hooksPath=/dev/null", *arguments],
                    capture_output=True,
                    check=True,
                    timeout=600,
                ).stdout

            try:
                git("init", "--quiet")
                git("fetch", "--quiet", "--no-tags", self.repository.clone_url, base)
                git("fetch", "--quiet", "--no-tags", self.repository.clone_url, f"refs/pull/{pr}/head")
                if git("rev-parse", "FETCH_HEAD").decode().strip() != head:
                    return None
                merge_base = base if base == recorded else git("merge-base", base, head).decode().strip()
                return git("diff", "--no-ext-diff", "--no-textconv", merge_base, head).decode(errors="replace")
            except (OSError, subprocess.SubprocessError):
                return None

    def close_pr(self, pr, comment):
        """`GitHub.close_pr` on this source: close with a comment; True once a fresh read shows it closed."""
        from tauceti_tools.forge_write import ForgejoWriter

        actor = self.authenticated_actor()
        if actor is None:
            return False
        try:
            self.validate_binding()
            writer = ForgejoWriter(self.api, expected_user_id=actor.principal.native_id)
            writer.lifecycle(self.repository, pr, pull=True, state="closed", comment=comment)
        except ForgeError:
            pass
        finally:
            self.invalidate(pr)
        row = self.live_pull(pr)
        return row is not None and row.get("state") == "closed"

    def issue_list(self, repository, **options):
        from .github import GitHub

        return GitHub().issue_list(repository, **options)

    def shared_github_login(self):
        result = self._gh(["api", "--hostname", "github.com", "user", "--jq", ".login"])
        if result.returncode:
            raise Incomplete("the GitHub roadmap account is unavailable")
        return result.stdout.strip()

    def add_label(self, pr, name):
        from tauceti_tools.forge_write import ForgejoWriter

        self.validate_binding()
        ForgejoWriter(self.api).edit(self.repository, pr, pull=True, add_labels=[name])
        self.invalidate(pr)
        return True

    def commit_status(self, sha, context):
        # Source statuses remain observations. Stage 1C supplies execution authority.
        return ""

    def rebase_requested(self, pr, head):
        return False

    def rebase_request_body(self, pr, head):
        return ""

    def bump_repair_requested(self, pr, head):
        return None

    def pr_progress_state(self, pr):
        from .worker_sources import pull_url

        row = self.live_pull(pr)
        if row is None:
            return None
        head = row["head"]["sha"]
        comments = self.issue_comments(pr, fresh=True)
        reviews = self.reviews(pr, fresh=True)
        findings = self.review_comments(pr, fresh=True)
        if comments is None or reviews is None or findings is None:
            return None
        value = {
            "state": row["state"].upper(),
            "head": head,
            "comments": comments,
            "reviews": reviews,
            "url": pull_url(pr),
        }
        return {
            "head": head,
            "state": value["state"],
            "ncomments": len({item["id"] for item in comments + findings}) + len(reviews),
            "signature": json.dumps(value, sort_keys=True, default=str),
        }

    def review_comments(self, number, *, fresh=False):
        def fetch():
            comments = []
            for review in self.api.reviews(self.repository, number):
                rows = self.api.review_comments(self.repository, number, review["id"])
                for row in rows:
                    row = dict(row)
                    row["pull_request_review_id"] = review["id"]
                    # Forgejo's code comment commit_id names the last change to
                    # that line. The containing review records the reviewed head.
                    reviewed_head = review.get("commit_id")
                    if not isinstance(reviewed_head, str) or not re.fullmatch(r"[0-9a-f]{40}", reviewed_head):
                        raise Incomplete("native review commit identity unavailable")
                    row["line_commit_id"] = row.get("commit_id")
                    row["commit_id"] = reviewed_head
                    comments.append(row)
            conversation = self.issue_comments(number, fresh=fresh)
            if conversation is None:
                raise Incomplete("native review conversation unavailable")
            for row in conversation:
                body = row.get("body", "")
                head = re.search(r"<!--tauceti-source-head:([0-9a-f]{40})-->", body)
                reply = re.search(r"<!--tauceti-source-reply:root:([1-9][0-9]*)-->", body)
                if head or reply:
                    row = dict(row, thread_kind="issue-comment", path="")
                    if head:
                        row["commit_id"] = head[1]
                    if reply:
                        row["in_reply_to_id"] = int(reply[1])
                    comments.append(row)
            for row in comments:
                reply = re.search(r"<!--tauceti-source-reply:root:([1-9][0-9]*)-->", row.get("body", ""))
                if reply:
                    row["in_reply_to_id"] = int(reply[1])
            roots = [row for row in comments if "<!--tauceti-rubric:" in row.get("body", "")]
            for row in comments:
                if row in roots or row.get("in_reply_to_id") or row.get("thread_kind") == "issue-comment":
                    continue
                parents = [
                    root
                    for root in roots
                    if root["id"] < row["id"]
                    and root.get("pull_request_review_id") == row.get("pull_request_review_id")
                    and root.get("path") == row.get("path")
                    and root.get("position") == row.get("position")
                    and root.get("original_position") == row.get("original_position")
                ]
                if len(parents) == 1:
                    row["in_reply_to_id"] = parents[0]["id"]
                elif parents:
                    raise Incomplete("ambiguous native review reply parent")
            return comments

        return self._read("review_comments", [number], fetch, number=number, fresh=fresh)

    def api_jq(self, path, jq):
        result = self._gh(["api", path, "--jq", jq])
        return result.stdout.strip() if result.returncode == 0 else None

    def ensure_stuck_issue(self, pr, reason, diagnostic=""):
        # Use the originating PR conversation so an unavailable issue tracker cannot
        # route a diagnostic to the same-numbered GitHub PR.
        from tauceti_tools.forge_write import ForgejoWriter

        marker = "<!--tauceti-worker-stuck-->"
        comments = self.issue_comments(pr, fresh=True)
        if comments is None or any(marker in row.get("body", "") for row in comments):
            return
        ForgejoWriter(self.api).comment(
            self.repository, pr, marker + "\nWorker review requires attention. " + reason, pull=True
        )

    def fresh_claim_age(self, comment_id, emoji="eyes"):
        return None

    def add_reaction(self, comment_id, emoji="eyes"):
        return False

    def remove_reaction(self, comment_id, emoji="eyes"):
        return False

    def write_cli_configuration(self, path):
        from tauceti_tools.project import active

        project = active()
        raw = project.raw if project else {}
        repos = raw.get("repositories", {})
        # These are CLI routing bindings only. Worker keeps its human-owned project snapshot.
        lines = [
            "schema_version = 2",
            "[forges.worker_source]",
            'kind = "forgejo"',
            "url = " + json.dumps(self.repository.forge.url),
            "[repositories]",
            'content = { forge = "worker_source", name = ' + json.dumps(self.repo) + " }",
        ]
        for role, default in (
            ("roadmap", "TauCetiProject/TauCetiRoadmap"),
            ("review", "TauCetiProject/TauCetiReview"),
            ("data", "TauCetiProject/TauCetiData"),
            ("claims", "TauCetiProject/tauceti-claims"),
        ):
            value = project.repository_ref(role).name if role in repos else default
            lines.append(role + " = " + json.dumps(value))
        lines.extend(
            [
                "[ci]",
                'backend = "github-actions"',
                "repository = " + json.dumps(os.environ.get("TAUCETI_REPO", self.repo)),
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
