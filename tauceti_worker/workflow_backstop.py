"""Preview or dispatch overdue maintenance workflows from an independent clock."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .verification import atomic

ACTIVE = ("in_progress", "queued", "pending", "waiting", "requested")
EVENTS = ("schedule", "workflow_dispatch", "push", "repository_dispatch")


@dataclass(frozen=True)
class Workflow:
    """Maintenance workflow filename and minimum attempt interval in seconds."""

    name: str
    interval: int


WORKFLOWS = (
    Workflow("update.yml", 3600),
    Workflow("merge-conflicts.yml", 900),
    Workflow("api-docs.yml", 3600),
    Workflow("pages.yml", 10800),
)


class ObservationError(RuntimeError):
    """An authoritative observation or dispatch result is unavailable; defer safely."""


def repository_name(value: str) -> str:
    """Validate the owner/name passed to gh without permitting extra path components."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ObservationError("repository must be OWNER/NAME")
    return value


# An App installation token for the backstop reads workflows and runs and dispatches them.
APP_PERMISSIONS = {"actions": "write", "metadata": "read"}


def app_tokens(directory: Path, repository_id: int | None):
    """Installation tokens from App credentials in ``directory``, restricted to the backstop's needs.

    The directory holds ``github-app.json`` and ``github-private-key.pem``. It is either a private
    directory whose ancestors are owned by this user or root, or ``$CREDENTIALS_DIRECTORY`` (``%d``
    in a unit), where systemd's ``LoadCredential=`` places them for a sandboxed service. A pinned repository ID
    narrows each token to that repository; before the first execution pins it, the token is limited
    only by APP_PERMISSIONS and the installation's repository selection.
    """
    from tauceti_queue import github_app
    from tauceti_queue.github_adapter import UrllibTransport

    # systemd creates $CREDENTIALS_DIRECTORY for LoadCredential=; its ancestors are trusted.
    loaded = os.environ.get("CREDENTIALS_DIRECTORY")
    systemd = bool(loaded) and Path(loaded) == Path(directory)
    try:
        credentials = github_app.load_execution_credentials(directory, systemd_credentials=systemd)
        return github_app.InstallationTokenSource(
            credentials, UrllibTransport(timeout=45), repository_id=repository_id, permissions=APP_PERMISSIONS
        )
    except (OSError, ValueError, github_app.CredentialsError) as exc:
        raise ObservationError(f"GitHub App credentials unavailable: {type(exc).__name__}") from None


class GitHub:
    """Bounded gh transport using the ambient gh account, or App installation tokens if given.

    ``tokens`` maps a repository ID (or None before identity is known) to a token source. The
    first repository read narrows an unpinned source to the repository it observed.
    """

    def __init__(self, repo: str, tokens=None, repository_id: int | None = None):
        self.repo = repository_name(repo)
        self.tokens = tokens
        self.repository_id = repository_id
        self.source = None

    def environment(self) -> dict[str, str] | None:
        """The gh environment: inherited, or carrying a fresh installation token with no fallback."""
        if self.tokens is None:
            return None
        from tauceti_queue.github_app import CredentialsError

        try:
            if self.source is None:
                self.source = self.tokens(self.repository_id)
            token = self.source.token()
        except (CredentialsError, OSError, ValueError) as exc:
            raise ObservationError(f"GitHub App token unavailable: {exc}"[:500]) from None
        # gh treats an empty GH_TOKEN as unset and would fall back to stored credentials.
        if not isinstance(token, str) or not token or not all(33 <= ord(char) <= 126 for char in token):
            raise ObservationError("GitHub App returned an invalid installation token")
        # Pin github.com so a configured enterprise host cannot select other credentials.
        environment = {
            k: v
            for k, v in os.environ.items()
            if k not in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")
        }
        environment.update(GH_TOKEN=token, GH_HOST="github.com")
        return environment

    def command(self, *args: str) -> str:
        """Run one gh request with a 45-second bound, returning stdout or an observation error."""
        environment = self.environment()
        try:
            result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=45, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObservationError(f"GitHub command unavailable: {type(exc).__name__}") from None
        if result.returncode:
            raise ObservationError(result.stderr.strip()[:500] or "GitHub command failed")
        return result.stdout

    def read(self, path: str, **parameters: str) -> dict:
        """GET a JSON object, refusing transport errors or incomplete JSON envelopes."""
        args = ["api", "--method", "GET", path]
        for key, value in parameters.items():
            args += ["-f", f"{key}={value}"]
        try:
            value = json.loads(self.command(*args))
        except ValueError:
            raise ObservationError("GitHub returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ObservationError("GitHub returned an incomplete observation")
        return value

    def repository(self) -> dict:
        """Read repository identity, default branch, and admission state."""
        value = self.read(f"repos/{self.repo}")
        if self.tokens is not None and self.repository_id is None and type(value.get("id")) is int:
            # Later requests in this cycle use tokens restricted to the observed repository.
            self.repository_id, self.source = value["id"], None
        return value

    def workflow(self, name: str) -> dict:
        """Read whether the named workflow remains enabled."""
        return self.read(f"repos/{self.repo}/actions/workflows/{name}")

    def runs(self, name: str, branch: str, status: str | None = None, *, repo_id: int) -> list[dict]:
        """Read recent maintenance runs; query each event if a PR obscures the latest row.

        GitHub's branch filter matches names across forks. Event-filtered fallback reads
        avoid allowing a newer same-named PR branch to suppress default-branch maintenance.
        The common path uses one request; active-status reads preserve older active work.
        """
        parameters = {"branch": branch, "per_page": "1"}
        if status:
            parameters["status"] = status

        def page(event=None):
            value = self.read(
                f"repos/{self.repo}/actions/workflows/{name}/runs",
                **parameters,
                **({"event": event} if event else {}),
            )
            rows, total = value.get("workflow_runs"), value.get("total_count")
            if (
                not isinstance(rows, list)
                or type(total) is not int
                or total < 0
                or len(rows) > total
                or (total and not rows)
            ):
                raise ObservationError("GitHub returned an incomplete workflow-run page")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("head_branch") != branch
                    or not isinstance(row.get("head_repository"), dict)
                    or not isinstance(row.get("event"), str)
                    or row.get("status") not in (*ACTIVE, "completed")
                    or (status is not None and row.get("status") != status)
                    or (event is not None and row.get("event") != event)
                ):
                    raise ObservationError("GitHub returned an unexpected workflow run")
            return rows

        def maintenance(row):
            return row["head_repository"].get("id") == repo_id and row["event"] in EVENTS

        rows = page()
        if not rows or all(maintenance(row) for row in rows):
            return rows
        candidates = []
        for event in EVENTS:
            rows = page(event)
            if any(not maintenance(row) for row in rows):
                raise ObservationError("GitHub returned maintenance from another repository")
            candidates.extend(rows)
        return candidates

    def dispatch(self, name: str, branch: str) -> str:
        """Submit one manual workflow request; return its response without claiming completion."""
        return self.command("workflow", "run", name, f"--repo={self.repo}", f"--ref={branch}").strip()


def timestamp(value: object, now: float) -> float:
    """Parse a timezone-aware run creation time, rejecting invalid or future observations."""
    try:
        date = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError
        stamp = date.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ObservationError("workflow run has an invalid creation time") from None
    if stamp > now + 300:
        raise ObservationError("workflow run is ahead of the local clock")
    return stamp


def reconcile(
    api: GitHub,
    state: dict,
    *,
    execute: bool = False,
    clock=time.time,
    persist=lambda _state: None,
) -> list[dict]:
    """Preview or dispatch overdue enabled workflows on the current default branch.

    ``state`` is the versioned mutable dictionary returned by load_state. Execution pins
    repository identity and records each accepted/uncertain request immediately through
    ``persist``; callers executing real dispatches must supply durable persistence and
    hold an exclusive state lock. Persistence errors stop the cycle. ``clock`` supplies
    Unix seconds for policy intervals and cooldowns. Preview never mutates state.

    Recent attempts, uncertain cooldowns and any nonterminal run suppress dispatch.
    Observation failures defer that workflow; unrelated workflows can continue.
    """
    repo = api.repository()
    repo_id, branch = repo.get("id"), repo.get("default_branch")
    if type(repo_id) is not int or repo_id <= 0 or not isinstance(branch, str) or not branch:
        raise ObservationError("repository identity or default branch is missing")
    if repo.get("archived") or repo.get("disabled"):
        raise ObservationError("repository is archived or disabled")
    if state["repo_id"] not in (None, repo_id):
        raise ObservationError("repository identity changed; review the state before proceeding")
    if execute and state["repo_id"] is None:
        state["repo_id"] = repo_id
        persist(state)

    results = []
    for policy in WORKFLOWS:
        result = {"workflow": policy.name, "branch": branch}
        try:
            workflow = api.workflow(policy.name)
            if workflow.get("state") not in {"active", "disabled_manually", "disabled_inactivity", "deleted"}:
                raise ObservationError("workflow enabled state is missing or unknown")
            if workflow["state"] != "active":
                results.append({**result, "decision": "disabled"})
                continue
            now = clock()
            rows = api.runs(policy.name, branch, repo_id=repo_id)
            latest = max((timestamp(row.get("created_at"), now) for row in rows), default=0)
            if any(row["status"] in ACTIVE for row in rows):
                results.append({**result, "decision": "active"})
                continue
            previous = state["dispatches"].get(policy.name, {})
            if previous.get("branch") == branch:
                accepted, uncertain = previous.get("accepted_at", 0), previous.get("uncertain_until", 0)
                if any(type(t) not in (int, float) or not 0 <= t <= now + 300 for t in (accepted, uncertain)):
                    raise ObservationError("dispatch state has an invalid time")
                latest = max(latest, accepted)
                if uncertain > now:
                    results.append({**result, "decision": "uncertain-cooldown"})
                    continue
            if now < latest + policy.interval:
                results.append({**result, "decision": "recent"})
                continue

            # An older active run can be absent from the latest history page. Query each
            # nonterminal status explicitly before dispatching; never walk all PR history.
            if any(api.runs(policy.name, branch, status, repo_id=repo_id) for status in ACTIVE):
                results.append({**result, "decision": "active"})
                continue
            if not execute:
                results.append({**result, "decision": "would-dispatch"})
                continue
            try:
                output = api.dispatch(policy.name, branch)
            except ObservationError:
                # A timed-out POST may have been accepted. Re-observe after a bounded
                # cooldown, rather than immediately submitting the same request again.
                state["dispatches"][policy.name] = {"branch": branch, "uncertain_until": clock() + 300}
                persist(state)
                raise
            state["dispatches"][policy.name] = {"branch": branch, "accepted_at": clock()}
            persist(state)
            results.append({**result, "decision": "dispatched", "output": output[:1000]})
        except ObservationError as exc:
            results.append({**result, "decision": "deferred", "reason": str(exc)})
    return results


def load_state(path: Path) -> dict:
    """Read versioned state, initialize an absent file, and reject corruption or symlinks."""
    if path.is_symlink():
        raise ObservationError("state path must not be a symlink")
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "repo_id": None, "dispatches": {}}
    except (OSError, ValueError):
        raise ObservationError("state is unreadable; preserve and inspect it before retrying") from None
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or type(value.get("repo_id")) is not int
        or not isinstance(value.get("dispatches"), dict)
        or any(not isinstance(v, dict) for v in value["dispatches"].values())
    ):
        raise ObservationError("state is incomplete or has an unsupported version")
    return value


def main(argv=None) -> int:
    """Run a read-only preview or an explicitly enabled, locked execution cycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="dispatch overdue workflows; default is a preview")
    parser.add_argument(
        "--github-app-dir",
        type=Path,
        help="authenticate gh with an installation token from this App credential directory "
        "(github-app.json, github-private-key.pem) instead of the ambient gh account",
    )
    args = parser.parse_args(argv)
    try:
        with contextlib.ExitStack() as stack:
            if args.execute:
                args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = stack.enter_context(open(str(args.state) + ".lock", "a"))
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(json.dumps({"decision": "locked"}))
                    return 0
            state = load_state(args.state)
            tokens = None
            if args.github_app_dir is not None:
                tokens = functools.partial(app_tokens, args.github_app_dir)
            rows = reconcile(
                GitHub(args.repo, tokens, state["repo_id"]),
                state,
                execute=args.execute,
                persist=lambda value: atomic(args.state, value),
            )
            print(json.dumps(rows, sort_keys=True))
            return int(any(row["decision"] == "deferred" for row in rows))
    except (ObservationError, OSError) as exc:
        print(json.dumps({"decision": "deferred", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
