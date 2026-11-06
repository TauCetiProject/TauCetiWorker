"""Stand-ins for the queue's surroundings: a fake GitHub over a bare repository, a scripted CI, scripted agents.

These power the local end-to-end fixture (`tests/queue_fixture.py`) and the cutover rehearsal. Real
Git history lives in a bare repository (`FakeRepo.origin`) that plays GitHub's copy of the content
repository; the controller's clone fetches from it, promotion fast-forwards its canonical branch,
retained refs are pushed into it. PR metadata, statuses, comments, labels, runs and admin logins are
plain in-memory records on `FakeGitHub`. CI is scripted: `FakeGitHub.ci_policy` decides, per
candidate, whether the build passes, fails with diagnostics, suffers an infrastructure failure, is
cancelled, is lost, or completes without a bound status, and whether the `scope` or `bump-guard`
verdict posted beside a green `build` is red. Agents are `ScriptedAgent`s that claim a
task, construct a replacement chain from a script of per-member resolutions and submit it; they use
no inference.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import chain
from .controller import Controller
from .ports import (
    AdmissionSnapshot,
    Adoption,
    CandidatePhase,
    CandidateRun,
    FastForwardResult,
    GitHubUnavailable,
    OpenPullRequest,
    PullRequest,
    Status,
    Task,
)


class FakeClock:
    """A controllable clock: call it for the time, `advance` it to move on."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def run_git(
    repo: str | os.PathLike[str], *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> str:
    full_env = dict(os.environ)
    full_env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        input=input,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {os.fspath(repo)}: {result.stderr.strip()}")
    return result.stdout.strip()


class FakeRepo:
    """A bare content repository (GitHub's copy), the service's clone of it, and helpers to author history."""

    def __init__(self, root: str | os.PathLike[str], branch: str = "main", files: dict[str, str] | None = None) -> None:
        self.root = Path(root)
        self.branch = branch
        self.origin = self.root / "origin.git"
        self.clone = self.root / "clone"
        self.scratch = self.root / "scratch"
        self.root.mkdir(parents=True, exist_ok=True)
        run_git(self.root, "init", "-q", "--bare", "-b", branch, str(self.origin))
        run_git(self.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        run_git(self.origin, "config", "receive.denyCurrentBranch", "ignore")
        run_git(self.root, "clone", "-q", str(self.origin), str(self.scratch))
        self.write_files(files or {"README.md": "readme\n", "Shared.lean": "line one\nline two\nline three\n"})
        run_git(self.scratch, "add", "-A")
        run_git(self.scratch, "commit", "-q", "-m", "base")
        run_git(self.scratch, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        run_git(self.origin, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        run_git(self.root, "clone", "-q", str(self.origin), str(self.clone))

    # --- authoring --------------------------------------------------------------------------------

    def write_files(self, files: dict[str, str | None]) -> None:
        for name, text in files.items():
            path = self.scratch / name
            if text is None:
                if path.exists():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def main_sha(self) -> str:
        return run_git(self.origin, "rev-parse", f"refs/heads/{self.branch}")

    def rev_parse(self, rev: str) -> str:
        return run_git(self.origin, "rev-parse", "--verify", rev)

    def commit_branch(
        self,
        files: dict[str, str | None],
        message: str,
        *,
        start: str | None = None,
        author: str = "Alice <alice@example.invalid>",
        push_to: str | None = None,
    ) -> str:
        """Commit `files` on top of `start` (default: the canonical branch) in the scratch clone; optionally push to a ref."""
        run_git(self.scratch, "fetch", "-q", "origin", "+refs/*:refs/remotes/origin/*")
        base = start or run_git(self.scratch, "rev-parse", f"refs/remotes/origin/heads/{self.branch}")
        run_git(self.scratch, "checkout", "-q", "--detach", base)
        self.write_files(files)
        run_git(self.scratch, "add", "-A")
        run_git(self.scratch, "commit", "-q", "--allow-empty", "-m", message, f"--author={author}")
        sha = run_git(self.scratch, "rev-parse", "HEAD")
        if push_to:
            run_git(self.scratch, "push", "-q", "-f", "origin", f"{sha}:{push_to}")
        return sha

    def advance_main(self, files: dict[str, str | None], message: str = "admin change") -> str:
        """An administrative advance of the canonical branch (a commit pushed directly)."""
        sha = self.commit_branch(files, message, push_to=f"refs/heads/{self.branch}")
        return sha

    def file_at(self, rev: str, path: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.origin), "show", f"{rev}:{path}"], capture_output=True, text=True, check=False
        )
        return result.stdout if result.returncode == 0 else None

    def log(self, rev: str, n: int = 20) -> list[str]:
        return run_git(self.origin, "log", "--format=%H", f"-{n}", rev).split()

    def parents(self, sha: str) -> list[str]:
        return run_git(self.origin, "rev-list", "--parents", "-1", sha).split()[1:]

    def message(self, sha: str) -> str:
        return run_git(self.origin, "log", "-1", "--format=%B", sha)


@dataclass
class FakeRun:
    run_id: str
    attempt: int
    head_sha: str
    base_sha: str
    base_ref: str
    batch_id: str
    members: list[int]
    created_at: float
    verdict: str  # pass, fail, infrastructure, scope-fail, bump-guard-fail, build-only, cancel, lost, no-status
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    status: str = "queued"
    conclusion: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    runner_name: str = "fleet-runner-1"
    host: str = "chungus2"
    phases: list[CandidatePhase] = field(default_factory=list)
    updated_at: float | None = None
    # GitHub no longer reports the run at all (neither in the listing nor by id): a lost run.
    vanished: bool = False

    def to_candidate_run(self) -> CandidateRun:
        return CandidateRun(
            run_id=self.run_id,
            attempt=self.attempt,
            status=self.status,
            conclusion=self.conclusion,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            runner_name=self.runner_name if self.status != "queued" else None,
            runner_labels=("self-hosted", "staging"),
            host=self.host if self.status != "queued" else None,
            phases=tuple(self.phases),
            diagnostics=tuple(self.diagnostics) if self.status == "completed" else (),
            display_title=f"bors branch={self.base_ref} batch={self.batch_id} head={self.head_sha} base={self.base_sha}",
            html_url=f"https://github.invalid/actions/runs/{self.run_id}",
            updated_at=self.updated_at,
        )


Verdict = tuple[str, list[dict[str, Any]]]
CIPolicy = Callable[[str, str, Sequence[int]], Verdict]


def always_pass(_head: str, _base: str, _members: Sequence[int]) -> Verdict:
    return "pass", []


class FakeGitHub:
    """`GitHubPort` over a `FakeRepo`, with in-memory PRs, statuses, comments, labels and scripted CI."""

    def __init__(self, repo: FakeRepo, clock: FakeClock, repository: str = "TauCetiProject/TauCeti") -> None:
        self.repo = repo
        self.clock = clock
        self.repository = repository
        self.prs: dict[int, dict[str, Any]] = {}
        self.admins: set[str] = set()
        # When set, `repository_admin` raises `GitHubUnavailable` (a quota outage) with this message.
        self.unavailable: str | None = None
        self.comments: list[dict[str, Any]] = []
        self.review_comments: dict[int, list[dict[str, Any]]] = {}
        self.labels: dict[int, set[str]] = {}
        # Repository labels by name: (color, description).
        self.repository_labels: dict[str, tuple[str, str]] = {}
        self.closed: dict[int, str] = {}
        self.statuses: dict[tuple[str, str], Status] = {}
        self.declaration_records: dict[str, dict] = {}
        self.runs: list[FakeRun] = []
        # pr-build.yml's concurrency group with cancel-in-progress: a new dispatch for a candidate
        # cancels any queued or in-progress run for the same batch and head.
        self.cancel_in_progress = True
        self.dispatches: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.ci_policy: CIPolicy = always_pass
        self.fail_next: dict[str, int] = {}
        self.lose_fast_forward_response = False
        # The review App's `merge eligibility` check runs by head SHA (REST-shaped records), and the
        # remaining API quota `rate_limit` reports; tests script both.
        self.checks: dict[str, list[dict[str, Any]]] = {}
        self.remaining = 4800
        self._check_counter = 0
        self._run_counter = 0
        self._comment_counter = 0
        # The controller loop thread and the test thread share this object; every method that reads
        # or changes the records holds the lock, so a pass never sees a run half-way through a change.
        self.lock = threading.RLock()

    # --- fixture controls -------------------------------------------------------------------------

    def _call(self, name: str, *args: Any) -> None:
        with self.lock:
            self.calls.append((name, args))
            remaining = self.fail_next.get(name, 0)
            if remaining:
                self.fail_next[name] = remaining - 1
                raise RuntimeError(f"injected failure of {name}")

    def create_pr(
        self,
        number: int,
        files: dict[str, str | None],
        *,
        title: str | None = None,
        body: str = "",
        author: str = "alice",
        start: str | None = None,
        labels: Sequence[str] = (),
    ) -> str:
        with self.lock:
            head = self.repo.commit_branch(
                files,
                f"pr {number}",
                start=start,
                author=f"{author} <{author}@example.invalid>",
                push_to=f"refs/pull/{number}/head",
            )
            self.prs[number] = {
                "number": number,
                "head_sha": head,
                "base_ref": self.repo.branch,
                "title": title or f"Change {number}",
                "body": body or f"Body of PR {number}.",
                "author_login": author,
                "state": "open",
                "draft": False,
            }
            self.labels.setdefault(number, set(labels))
            return head

    def push_pr(self, number: int, files: dict[str, str | None]) -> str:
        """The author pushes a new head to an existing PR."""
        with self.lock:
            current = self.prs[number]
            head = self.repo.commit_branch(
                files, f"pr {number} update", start=current["head_sha"], push_to=f"refs/pull/{number}/head"
            )
            current["head_sha"] = head
            return head

    def runs_for(self, head_sha: str) -> list[FakeRun]:
        with self.lock:
            return [run for run in self.runs if run.head_sha == head_sha]

    def start_runs(self) -> list[FakeRun]:
        """Queued runs begin: a runner picks them up and the preparation phase starts. A run with
        verdict `lost` vanishes instead: GitHub reports it neither in the listing nor by id."""
        with self.lock:
            started = []
            now = self.clock()
            for run in self.runs:
                if run.status == "queued" and run.verdict == "lost":
                    run.vanished = True
                elif run.status == "queued":
                    run.status = "in_progress"
                    run.started_at = now
                    run.updated_at = now
                    run.phases = [CandidatePhase("preparation", now, None, "in_progress")]
                    started.append(run)
            return started

    def progress_runs(self, phase: str, detail: str = "") -> None:
        with self.lock:
            now = self.clock()
            for run in self.runs:
                if run.status == "in_progress":
                    if run.phases and run.phases[-1].completed_at is None:
                        last = run.phases[-1]
                        run.phases[-1] = CandidatePhase(last.name, last.started_at, now, "completed", last.detail)
                    run.phases.append(CandidatePhase(phase, now, None, "in_progress", detail))
                    run.updated_at = now

    def complete_runs(self) -> list[FakeRun]:
        """Running (or queued) runs finish according to their verdict; the finalizer posts the bound statuses."""
        with self.lock:
            now = self.clock()
            completed = []
            for run in self.runs:
                if run.status == "completed" or run.verdict == "lost":
                    continue
                if run.status == "queued":
                    run.status = "in_progress"
                    run.started_at = now
                run.phases = [
                    CandidatePhase(p.name, p.started_at, p.completed_at or now, "completed", p.detail)
                    for p in run.phases
                ]
                run.status = "completed"
                run.completed_at = now
                run.updated_at = now
                url = run.to_candidate_run().html_url

                def post(context: str, state: str, description: str, target_url: str = url, run=run) -> None:
                    self.statuses[(run.head_sha, context)] = Status(state, description, target_url, now, run.run_id)

                # The finalizer posts `bump-guard`, `build` and `scope` in one step; every verdict but
                # the one under test is green, as pr-build.yml posts them for an in-scope head.
                policy = {
                    "bump-guard": ("success", "no Lake pin change"),
                    "scope": (
                        "success",
                        "changes are in scope (TauCeti/, the allowed root files, and the bibliography only)",
                    ),
                }
                if run.verdict == "pass":
                    run.conclusion = "success"
                    post("build", "success", "Build succeeded")
                elif run.verdict == "fail":
                    run.conclusion = "failure"
                    post("build", "failure", "Build failed")
                elif run.verdict == "infrastructure":
                    run.conclusion = "failure"
                    post("build", "failure", "infrastructure: runner lost its cache volume", "")
                elif run.verdict == "scope-fail":
                    # A green build whose merge-policy verdict is red (restricted paths without a current
                    # admin approval, an oversized diff, a new file over 1000 lines).
                    run.conclusion = "success"
                    post("build", "success", "Build succeeded")
                    policy["scope"] = ("failure", "restricted paths changed with no valid current bors scope approval")
                elif run.verdict == "bump-guard-fail":
                    run.conclusion = "success"
                    post("build", "success", "Build succeeded")
                    policy["bump-guard"] = ("failure", "Lake pin change failed the trusted bump validator")
                elif run.verdict == "build-only":
                    # The finalizer's step was cut short after `build`: the other two verdicts never appear.
                    run.conclusion = "success"
                    post("build", "success", "Build succeeded")
                    policy = {}
                elif run.verdict == "cancel":
                    run.conclusion = "cancelled"
                elif run.verdict == "no-status":
                    run.conclusion = "failure"
                if run.verdict in ("pass", "fail", "infrastructure", "scope-fail", "bump-guard-fail"):
                    for context, (state, description) in policy.items():
                        post(context, state, description)
                completed.append(run)
            return completed

    def post_status(
        self, sha: str, state: str, run_id: str | None, description: str = "", context: str = "build"
    ) -> None:
        """A status from somewhere other than the candidate's own run (a stale callback, a PR build)."""
        with self.lock:
            self.statuses[(sha, context)] = Status(state, description, "", self.clock(), run_id)

    def cancel_runs(self, head_sha: str) -> None:
        with self.lock:
            for run in self.runs_for(head_sha):
                if run.status != "completed":
                    run.status = "completed"
                    run.conclusion = "cancelled"
                    run.completed_at = self.clock()

    def comments_for(self, number: int) -> list[dict[str, Any]]:
        with self.lock:
            return [c for c in self.comments if c["number"] == number]

    def publish_check(
        self,
        number: int,
        *,
        eligible: bool = True,
        review_safe: bool = True,
        single: bool = False,
        head: str | None = None,
        app_id: int = 3947238,
        merge_base: str | None = None,
    ) -> dict[str, Any]:
        """TauCetiReview publishes a `merge eligibility` decision on the PR's head (no webhook is delivered)."""
        with self.lock:
            import json

            head = head or self.prs[number]["head_sha"]
            if eligible and merge_base is None:
                merge_base = self.pr_merge_base(self.repository, number, head)
            self._check_counter += 1
            check_id = 500 + self._check_counter
            data = {
                "schema": "tauceti-merge.eligibility/v1",
                "repo": self.repository,
                "pr": number,
                "merge_base_sha": merge_base,
                "eligible": eligible,
                "review_safe": review_safe,
                "single": single,
            }
            check = {
                "id": check_id,
                "name": "merge eligibility",
                "app": {"id": app_id},
                "head_sha": head,
                "status": "completed",
                "conclusion": "success" if eligible else ("neutral" if review_safe else "failure"),
                "external_id": json.dumps(data, sort_keys=True, separators=(",", ":")),
                "completed_at": "2026-11-04T07:00:00Z",
                "html_url": f"https://github.invalid/{self.repository}/runs/{check_id}",
            }
            self.checks.setdefault(head, []).append(check)
            return check

    # --- GitHubPort -------------------------------------------------------------------------------

    def fetch_pr(self, repo: str, number: int) -> PullRequest:
        with self.lock:
            self._call("fetch_pr", number)
            pr = self.prs[number]
            return PullRequest(
                number=number,
                head_sha=pr["head_sha"],
                base_ref=pr["base_ref"],
                title=pr["title"],
                body=pr["body"],
                author_login=pr["author_login"],
                state=pr["state"],
                draft=pr["draft"],
                labels=tuple(sorted(self.labels.get(number, set()))),
            )

    def fetch_refs(self, repo: str, refspecs: Sequence[str]) -> None:
        with self.lock:
            self._call("fetch_refs", tuple(refspecs))
            run_git(self.repo.clone, "fetch", "-q", "--no-tags", str(self.repo.origin), *refspecs)

    def dispatch_candidate(
        self,
        repo: str,
        *,
        head_sha: str,
        base_sha: str,
        base_ref: str,
        batch_id: str,
        members: Sequence[int],
        repair_outputs_revs: Sequence[str] = (),
    ) -> str:
        with self.lock:
            self._call("dispatch_candidate", head_sha, base_sha, batch_id)
            self._run_counter += 1
            if self.cancel_in_progress:
                now = self.clock()
                for older in self.runs:
                    if (
                        older.batch_id == batch_id
                        and older.head_sha == head_sha
                        and older.status != "completed"
                        and not older.vanished
                    ):
                        older.status, older.conclusion = "completed", "cancelled"
                        older.completed_at = older.updated_at = now
            verdict, diagnostics = self.ci_policy(head_sha, base_sha, members)
            run = FakeRun(
                run_id=str(1000 + self._run_counter),
                attempt=1,
                head_sha=head_sha,
                base_sha=base_sha,
                base_ref=base_ref,
                batch_id=batch_id,
                members=list(members),
                created_at=self.clock(),
                verdict=verdict,
                diagnostics=list(diagnostics),
            )
            self.runs.append(run)
            dispatch = {
                "event_type": "tauceti-bors-staging",
                "client_payload": {
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "batch_id": batch_id,
                    "members": list(members),
                    "base_ref": base_ref,
                    "repair_outputs_revs": list(repair_outputs_revs),
                },
                "run_id": run.run_id,
            }
            self.dispatches.append(dispatch)
            return f"dispatch-{self._run_counter}"

    def find_candidate_run(
        self, repo: str, *, batch_id: str, head_sha: str, base_sha: str, since: float | None = None
    ) -> CandidateRun | None:
        with self.lock:
            self._call("find_candidate_run", batch_id, head_sha, base_sha)
            matching = [
                r
                for r in self.runs
                if r.batch_id == batch_id and r.head_sha == head_sha and r.base_sha == base_sha and not r.vanished
            ]
            if since is not None:
                matching = [r for r in matching if r.created_at >= since - 60]
            if not matching:
                return None
            return matching[-1].to_candidate_run()

    def read_run(self, repo: str, run_id: str, *, with_jobs: bool = False) -> CandidateRun | None:
        with self.lock:
            self._call("read_run", run_id)
            for run in self.runs:
                if run.run_id == run_id and not run.vanished:
                    return run.to_candidate_run()
            return None

    def cancel_run(self, repo: str, run_id: str) -> str:
        with self.lock:
            self._call("cancel_run", run_id)
            for run in self.runs:
                if run.run_id == run_id and not run.vanished:
                    if run.status == "completed":
                        return "already-completed"
                    run.status, run.conclusion = "completed", "cancelled"
                    run.completed_at = run.updated_at = self.clock()
                    return "cancelled"
            raise RuntimeError(f"HTTP 404 on POST repos/{repo}/actions/runs/{run_id}/cancel")

    def declaration_names(self, repo: str, head_sha: str) -> dict:
        self._call("declaration_names", repo, head_sha)
        return self.declaration_records.get(head_sha, {"state": "unknown", "reason": "record_missing"})

    def commit_status(self, repo: str, sha: str, context: str) -> Status | None:
        with self.lock:
            self._call("commit_status", sha, context)
            return self.statuses.get((sha, context))

    def fast_forward(self, repo: str, branch: str, expected_parent: str, tip: str) -> FastForwardResult:
        with self.lock:
            self._call("fast_forward", branch, expected_parent, tip)
            current = self.repo.main_sha()
            if current != expected_parent:
                return FastForwardResult(False, current, "Update is not a fast forward")
            run_git(self.repo.origin, "update-ref", f"refs/heads/{branch}", tip, expected_parent)
            if self.lose_fast_forward_response:
                self.lose_fast_forward_response = False
                raise RuntimeError("connection reset while reading the push response")
            return FastForwardResult(True, expected_parent)

    def update_ref(self, repo: str, ref: str, sha: str) -> None:
        with self.lock:
            self._call("update_ref", ref, sha)
            self._push_ref(ref, sha)

    def update_refs(self, repo: str, updates: Sequence[tuple[str, str]]) -> None:
        """One recorded call for the whole batch, so tests can count how often each ref was pushed."""
        with self.lock:
            self._call("update_refs", tuple(updates))
            for ref, sha in updates:
                self._push_ref(ref, sha)

    def _push_ref(self, ref: str, sha: str) -> None:
        if not chain.object_exists(self.repo.origin, sha):
            run_git(self.repo.clone, "push", "-q", "-f", str(self.repo.origin), f"{sha}:{ref}")
        else:
            run_git(self.repo.origin, "update-ref", ref, sha)

    def post_comment(self, repo: str, number: int, body: str, marker: str) -> str:
        with self.lock:
            self._call("post_comment", number, marker)
            for comment in self.comments:
                if comment["number"] == number and comment["marker"] == marker:
                    comment["body"] = body
                    comment["updates"] += 1
                    return comment["id"]
            self._comment_counter += 1
            comment = {
                "id": f"c{self._comment_counter}",
                "number": number,
                "body": body,
                "marker": marker,
                "updates": 0,
                "at": self.clock(),
            }
            self.comments.append(comment)
            return comment["id"]

    def set_labels(self, repo: str, number: int, add: Sequence[str], remove: Sequence[str]) -> None:
        with self.lock:
            self._call("set_labels", number, tuple(add), tuple(remove))
            labels = self.labels.setdefault(number, set())
            labels.difference_update(remove)
            labels.update(add)

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        with self.lock:
            self._call("ensure_label", name, color, description)
            self.repository_labels.setdefault(name, (color, description))

    def close_pr(self, repo: str, number: int, title: str) -> None:
        with self.lock:
            self._call("close_pr", number, title)
            self.prs[number]["state"] = "closed"
            self.prs[number]["title"] = title
            self.closed[number] = title

    def repository_admin(self, login: str) -> bool:
        with self.lock:
            self._call("repository_admin", login)
            if self.unavailable:
                raise GitHubUnavailable(f"fake outage: {self.unavailable}")
            return login in self.admins

    def rate_limit(self) -> tuple[int, float]:
        with self.lock:
            return self.remaining, self.clock() + 3600

    def eligibility_checks(self, repo: str, head: str) -> list[dict[str, Any]]:
        with self.lock:
            self._call("eligibility_checks", head)
            return [dict(check) for check in self.checks.get(head, [])]

    def pr_merge_base(self, repo: str, number: int, head_sha: str) -> str | None:
        with self.lock:
            self._call("pr_merge_base", number, head_sha)
            pr = self.prs.get(number)
            if pr is None or pr["head_sha"] != head_sha:
                return None
            result = subprocess.run(
                ["git", "-C", str(self.repo.origin), "merge-base", f"refs/heads/{self.repo.branch}", head_sha],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip() or None if result.returncode == 0 else None

    def _admission_source(self, number):
        pull = self.fetch_pr(self.repository, number)
        base = self.pr_merge_base(self.repository, number, pull.head_sha) or ""
        return {
            "head": pull.head_sha,
            "base_ref": pull.base_ref,
            "state": pull.state,
            "draft": pull.draft,
            "body": pull.body,
            "labels": list(pull.labels),
            "comments": list(self.review_comments.get(number, [])),
            "diff_version": base,
            "statuses": {
                context: status.state
                for (head, context), status in self.statuses.items()
                if head == pull.head_sha and context in ("build", "scope", "bump-guard")
            },
        }

    def admission_snapshot(self, repo, number, *, generation=0):
        self._call("admission_snapshot", number)
        pull = self.fetch_pr(repo, number)
        source = self._admission_source(number)
        base = source["diff_version"]
        if pull.state == "open" and not pull.draft:
            self.fetch_refs(repo, [pull.head_sha, base])
            paths = tuple(
                p
                for p in chain.git(
                    self.repo.clone, "diff", "--name-only", "-z", "--no-renames", base, pull.head_sha
                ).split("\0")
                if p
            )
        else:
            paths = ()
        return AdmissionSnapshot(pull, base, paths, tuple(source["comments"]), source["statuses"], base)

    def list_open_pull_requests(
        self, repo: str, *, review_app_id: int, admission=False, on_page=None
    ) -> list[OpenPullRequest]:
        with self.lock:
            self._call("list_open_pull_requests", review_app_id)
            listed = []
            for number, pr in sorted(self.prs.items()):
                if pr["state"] != "open":
                    continue
                checks = tuple(
                    dict(c)
                    for c in self.checks.get(pr["head_sha"], [])
                    if c.get("name") == "merge eligibility" and (c.get("app") or {}).get("id") == review_app_id
                )
                listed.append(
                    OpenPullRequest(
                        number,
                        pr["head_sha"],
                        pr["base_ref"],
                        pr["draft"],
                        checks,
                        self._admission_source(number) if admission else None,
                    )
                )
            if on_page:
                on_page(listed)
            return listed


# --- scripted agents --------------------------------------------------------------------------------


@dataclass
class Resolution:
    """What the scripted agent does to one member: write files (None deletes), or empty it; and why."""

    files: dict[str, str | None] = field(default_factory=dict)
    empty: bool = False
    explanation: str = "Resolved by the fixture agent."


Script = Callable[[Task], dict[str, Resolution]]


class ScriptedAgent:
    """A worker stand-in: claims the head's task, rebuilds the chain from a script, pushes and submits it.

    `script(task)` returns the resolutions keyed by `source_pr`. Members without a resolution are
    replayed mechanically onto the new tip; a member whose mechanical replay conflicts and has no
    resolution makes the agent give up (it releases the task with the given reason). Explanations are
    appended to the existing message, as the agent instruction requires; `omit_explanation` leaves
    them off to exercise the controller's `explanation-required` rejection.
    """

    def __init__(
        self,
        name: str,
        login: str,
        repo: FakeRepo,
        workdir: str | os.PathLike[str],
        script: Script,
        *,
        omit_explanation: bool = False,
    ) -> None:
        self.name = name
        self.login = login
        self.repo = repo
        self.workdir = Path(workdir)
        self.script = script
        self.omit_explanation = omit_explanation
        self.invocations = 0
        self.tasks: list[Task] = []
        self.results: list[Adoption] = []
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.clone = self.workdir / "agent-clone"
        if not self.clone.exists():
            run_git(self.workdir, "clone", "-q", str(self.repo.origin), str(self.clone))

    def claim(self, controller: Controller) -> Task | None:
        task = controller.claim_task(self.name, self.login)
        if task is not None:
            self.tasks.append(task)
        return task

    def run(self, controller: Controller, *, before_submit: Callable[[Task], None] | None = None) -> Adoption | None:
        """Claim, build the replacement chain, submit. Returns the adoption, or None when no task was available."""
        task = self.claim(controller)
        if task is None:
            return None
        tip = self.produce(task)
        if before_submit:
            before_submit(task)
        result = controller.submit_task(task.task_id, task.claim_token, tip, worker_notes=f"{self.name} fixture run")
        self.results.append(result)
        return result

    def produce(self, task: Task) -> str:
        """Build the replacement chain in the agent's clone and push it to the task's submission ref."""
        self.invocations += 1
        run_git(self.clone, "fetch", "-q", str(self.repo.origin), *task.retained_refs, task.base, task.tip, task.onto)
        resolutions = self.script(task)
        commits = chain.chain_commits(self.clone, task.base, task.tip)
        new_tip = task.onto
        for commit in commits:
            source_pr = commit.source_pr or commit.sha
            resolution = resolutions.get(source_pr)
            outcome = chain.merge_trees(self.clone, new_tip, commit.sha, merge_base=commit.parents[0])
            if resolution is None:
                if not outcome.clean:
                    raise RuntimeError(f"agent {self.name}: no resolution for conflicting member {source_pr}")
                tree = outcome.tree
                message = commit.message
            else:
                if resolution.empty:
                    tree = chain.tree_of(self.clone, new_tip)
                else:
                    tree = outcome.tree if outcome.clean else chain.tree_of(self.clone, new_tip)
                    tree = self._edit_tree(tree, resolution.files)
                message = (
                    commit.message
                    if self.omit_explanation
                    else commit.message.rstrip() + "\n\n" + resolution.explanation.strip() + "\n"
                )
            new_tip = chain.commit_tree(
                self.clone,
                tree,
                new_tip,
                message,
                (commit.author_name, commit.author_email),
                author_date=commit.author_date,
            )
        run_git(self.clone, "push", "-q", "-f", str(self.repo.origin), f"{new_tip}:{task.submission_ref}")
        return new_tip

    def _edit_tree(self, tree: str, files: dict[str, str | None]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            run_git(self.clone, "read-tree", tree, env=env)
            for path, content in files.items():
                if content is None:
                    run_git(self.clone, "update-index", "--force-remove", path, env=env)
                else:
                    blob = run_git(self.clone, "hash-object", "-w", "--stdin", input=content)
                    run_git(self.clone, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            return run_git(self.clone, "write-tree", env=env)


def make_controller(
    root: str | os.PathLike[str],
    *,
    clock: FakeClock | None = None,
    repository: str = "TauCetiProject/TauCeti",
    revision: str = "fixture",
    publication: bool = True,
    seed: int = 7,
    files: dict[str, str] | None = None,
    db_name: str = "queue.sqlite",
) -> tuple[Controller, FakeGitHub, FakeRepo, FakeClock]:
    """A controller over a fresh fake repository under `root`, with publication enabled by default."""
    import random

    from .store import Store

    clock = clock or FakeClock()
    root = Path(root)
    repo = FakeRepo(root / "repo", files=files)
    github = FakeGitHub(repo, clock, repository)
    store = Store(str(root / db_name))
    controller = Controller(
        store, github, str(repo.clone), repository, revision=revision, clock=clock, rng=random.Random(seed)
    )
    if publication:
        controller.set_setting("publication_enabled", True, "fixture")
    return controller, github, repo, clock


def reopen_controller(
    root: str | os.PathLike[str],
    github: FakeGitHub,
    repo: FakeRepo,
    clock: FakeClock,
    *,
    repository: str = "TauCetiProject/TauCeti",
    revision: str = "fixture-restart",
    seed: int = 7,
    db_name: str = "queue.sqlite",
) -> Controller:
    """A second controller over the same database and clone, as after a service restart."""
    import random

    from .store import Store

    store = Store(str(Path(root) / db_name))
    return Controller(
        store, github, str(repo.clone), repository, revision=revision, clock=clock, rng=random.Random(seed)
    )


def cleanup(path: str | os.PathLike[str]) -> None:
    shutil.rmtree(path, ignore_errors=True)
