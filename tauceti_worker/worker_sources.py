"""Worker source selection and per-source runtime bindings.

Each selected unit runs through its original source. The GitHub worker keeps
its existing paths. Other sources use qualified state below that same worker.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from .config import Die, SourceUnavailable
from .constants import TAUCETI

_selected = contextvars.ContextVar("worker_source", default=None)


def current():
    return _selected.get()


def repository_url(name=TAUCETI):
    source = current()
    return (
        source.repository
        if source and name == TAUCETI
        else RepositoryRef(source.repository.forge if source else GITHUB, name)
    ).url


def pull_url(number):
    source = current()
    return (source.repository if source else RepositoryRef(GITHUB, TAUCETI)).pull_url(number)


def configure(args):
    github = getattr(args, "github", None)
    forgejo = getattr(args, "forgejo", None)
    legacy = getattr(args, "forges", None)
    if legacy is not None and (github is not None or forgejo is not None):
        raise Die("choose --github/--forgejo switches or the legacy --forges selector")
    if legacy is not None:
        if legacy not in ("github", "forgejo", "both"):
            raise Die("worker forges must be github, forgejo or both")
        github, forgejo = legacy in ("github", "both"), legacy in ("forgejo", "both")
    else:
        # Public invocations opt in with flags. Inherited runtime state or a
        # project configuration must never turn an ordinary worker into a trial.
        github = True if github is None else github
        forgejo = False if forgejo is None else forgejo
    if not github and not forgejo:
        raise Die("enable at least one source with --github or --forgejo")
    requested = "both" if github and forgejo else "github" if github else "forgejo"
    destination = getattr(args, "new_pr_forge", None) or "github"
    if destination not in ("github", "forgejo"):
        raise Die("new PR forge must be github or forgejo")
    if requested != "both" and destination != requested and getattr(args, "new_pr_forge", None) is not None:
        raise Die("the new PR forge must be among the selected forges")
    if (getattr(args, "pr", None) or os.environ.get("TAUCETI_PR", "").strip()) and requested == "both":
        raise Die("numeric --pr targets need one source: use --no-github --forgejo or --no-forgejo")
    if (
        "TAUCETI_FORGEJO_TOKEN_FILE" not in os.environ
        and TAUCETI.lower() == "taucetiproject/tauceti"
        and os.environ.get("TAUCETI_FORGEJO_ORIGIN", "https://git.taucetiproject.org")
        == "https://git.taucetiproject.org"
    ):
        os.environ["TAUCETI_FORGEJO_TOKEN_FILE"] = str(Path.home() / ".config/tauceti-forgejo/worker-token")
    os.environ["TAUCETI_WORKER_FORGES"] = requested
    os.environ["TAUCETI_NEW_PR_FORGE"] = destination


def selection_arguments():
    """Carry the parent's explicit selection into isolated loop children."""
    selected = os.environ.get("TAUCETI_WORKER_FORGES", "github")
    arguments = [
        "--github" if selected in ("github", "both") else "--no-github",
        "--forgejo" if selected in ("forgejo", "both") else "--no-forgejo",
    ]
    destination = os.environ.get("TAUCETI_NEW_PR_FORGE", "github")
    if selected == "both" or selected == destination:
        arguments += ["--new-pr-forge", destination]
    return arguments


def enabled():
    return current() is None and os.environ.get("TAUCETI_WORKER_FORGES", "github") != "github"


def native_source(cfg):
    from tauceti_tools.forge import Forgejo

    from .forgejo_worker import ForgejoWorkerSource

    origin = os.environ.get("TAUCETI_FORGEJO_ORIGIN")
    canonical = TAUCETI.lower() == "taucetiproject/tauceti"
    if not origin and canonical:
        origin = "https://git.taucetiproject.org"
    if not origin:
        raise Die("set TAUCETI_FORGEJO_ORIGIN for the worker's Forgejo source")
    repository = RepositoryRef(Forge("forgejo", origin), os.environ.get("TAUCETI_FORGEJO_REPO", TAUCETI))
    raw_id = os.environ.get("TAUCETI_FORGEJO_REPOSITORY_ID")
    if raw_id is None and repository.key == "forgejo:https://git.taucetiproject.org/taucetiproject/tauceti":
        raw_id = "1"
    if raw_id is None or not raw_id.isascii() or not raw_id.isdigit() or int(raw_id) < 1:
        raise Die("set TAUCETI_FORGEJO_REPOSITORY_ID to the expected native repository ID")
    path = os.environ.get("TAUCETI_FORGEJO_TOKEN_FILE")
    if path is None and canonical and repository.forge.url == "https://git.taucetiproject.org":
        path = str(cfg.home / ".config/tauceti-forgejo/worker-token")
    if path is None:
        raise Die("set TAUCETI_FORGEJO_TOKEN_FILE to a private Forgejo credential file")
    from tauceti_queue.control_policy_file import read_control_policy

    try:
        token = read_control_policy(Path(path)).decode("ascii").strip()
    except FileNotFoundError:
        token = None
        path = None
    except (OSError, ValueError):
        raise Die("the Forgejo worker credential file is unavailable or has unsafe permissions") from None
    from .paths import ensure_ssl_cert_file

    ensure_ssl_cert_file()
    return ForgejoWorkerSource(
        RepositoryIdentity(repository, int(raw_id)),
        Forgejo(repository.forge, token=token),
        token_file=Path(path) if path else None,
    )


def forgejo_identity():
    """The native user ID the selected Forgejo source must act as, or None when none is configured."""
    expected = os.environ.get("TAUCETI_FORGEJO_USER_ID")
    if os.environ.get("TAUCETI_WORKER_FORGES", "github") == "github" or not expected:
        return None
    if not expected.isascii() or not expected.isdigit() or int(expected) < 1:
        raise Die("TAUCETI_FORGEJO_USER_ID must be a native Forgejo user ID")
    return expected


def verify_forgejo_identity():
    """Ask the Forgejo source which account its credential belongs to, once; halt on a verdict.

    A wrong user, HTTP 401, and HTTP 403 raise `Halted`. Forgejo answers `GET /user` with 403 for a
    suspended, login-prohibited or unactivated account and for a token without `read:user`; the
    response message that tells them apart does not reach `ForgeError`, and none is fixed by a retry.
    An unavailable binding or forge is left to discovery, which reports the source unavailable.
    """
    from types import SimpleNamespace

    from tauceti_tools.forge import ForgeError

    from .config import Halted
    from .identity import ACCOUNT_HALT, INVALID_CREDENTIALS

    expected = forgejo_identity()
    if expected is None:
        return
    try:
        source = native_source(SimpleNamespace(home=Path.home()))
    except (Die, ValueError, OSError):
        return
    origin = source.repository.forge.url
    if source.anonymous_discovery:
        raise Die(f"no Forgejo credential for user {expected} at {origin}; set TAUCETI_FORGEJO_TOKEN_FILE")
    try:
        row = source.api.current_user()
    except ForgeError as error:
        if error.status == 401:
            raise Halted(
                INVALID_CREDENTIALS,
                f"Forgejo {origin} rejected the credential for user {expected} (HTTP 401); issue a new token",
                forge="forgejo",
            ) from None
        if error.status == 403:
            raise Halted(
                ACCOUNT_HALT,
                f"Forgejo {origin} refused the account behind user {expected}'s credential (HTTP 403): suspended, "
                "prohibited from login or not activated, or the token lacks read:user",
                forge="forgejo",
            ) from None
        return
    if str(row["id"]) != expected:
        raise _forgejo_mismatch(origin, expected, row["login"], row["id"])


def _forgejo_mismatch(origin, expected, login, user_id):
    from .config import Halted
    from .identity import IDENTITY_MISMATCH

    return Halted(
        IDENTITY_MISMATCH,
        f"Forgejo {origin}: expected user {expected}, but the credential belongs to {login} "
        f"(user {user_id}); point TAUCETI_FORGEJO_TOKEN_FILE at a token for user {expected}",
        forge="forgejo",
    )


def source_config(cfg, source):
    suffix = hashlib.sha256(
        json.dumps({"repository": source.repository.key, "id": source.identity.native_id}, sort_keys=True).encode()
    ).hexdigest()[:24]
    state = cfg.state / "sources" / suffix
    return dataclasses.replace(
        cfg,
        state=state,
        checkout=state / "checkout",
        store_dir=cfg.store_dir / "sources" / suffix,
        sbcache=state / "cache/scoreboard",
        logdir=cfg.logdir / "sources" / suffix,
    )


@contextlib.contextmanager
def activate(source, cfg):
    """Bind a single round's subprocesses without persisting credentials."""
    from tauceti_tools.git_credentials import scoped_git_environment

    source.validate_binding()
    actor = source.authenticated_actor()
    if actor is None:
        raise SourceUnavailable("the Forgejo worker identity is unavailable")
    # The round's check may have gone unanswered, or the token file may have changed since; the
    # actor that executes is the one bound below, and source_cli enforces this ID in subprocesses.
    expected = forgejo_identity()
    if expected is not None and actor.principal.native_id != expected:
        from .identity import halt_file, write_halt

        halt = _forgejo_mismatch(source.repository.forge.url, expected, actor.login, actor.principal.native_id)
        write_halt(halt_file(), halt)
        raise halt
    original = dict(os.environ)
    marker = _selected.set(source)
    token_file = source.token_file
    try:
        if expected is not None and source.token_file is not None:
            # Git's helper and source subprocesses read their token file on each use. Give them the
            # token verified above, so a file replaced during the round is never presented.
            token_file = _verified_token_copy(source.token_file, source.api._token)
        directory = cfg.state / "bin"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        real_gh = original.get("TAUCETI_REAL_GH") or shutil.which("gh")
        wrapper = directory / "gh"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m tauceti_worker.source_cli "$@"\n')
        wrapper.chmod(0o700)
        source.write_cli_configuration(cfg.state / "gh-project.toml")
        claim_wrapper = directory / "claim-source"
        claim_wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m tauceti_worker.source_claims "$@"\n'
        )
        claim_wrapper.chmod(0o700)
        env = scoped_git_environment(source.repository.forge, token_file=token_file, purpose="workspace")
        profile = source.api.request("GET", "user")
        if profile.get("id") != int(actor.principal.native_id):
            raise SourceUnavailable("native commit author changed")
        email = profile.get("email") or ""
        if profile.get("hide_email") or "@" not in email or any(character.isspace() for character in email):
            email = actor.login + "@noreply." + urlsplit(source.repository.forge.url).hostname
        for role in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{role}_NAME"] = actor.login
            env[f"GIT_{role}_EMAIL"] = email
        count = int(env["GIT_CONFIG_COUNT"])
        for index, (key, value) in enumerate((("user.name", actor.login), ("user.email", email)), count):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        env["GIT_CONFIG_COUNT"] = str(count + 2)
        # Keep provider and worker settings while replacing inherited Git credentials.
        for key in list(os.environ):
            if key.startswith("GIT_CONFIG_") or key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                os.environ.pop(key, None)
        os.environ.update(env)
        os.environ.update(
            TAUCETI_SOURCE_BINDING=json.dumps(source.identity.record(), sort_keys=True),
            TAUCETI_SOURCE_STATE=str(cfg.state),
            TAUCETI_SOURCE_CLAIM_NONCE=uuid.uuid4().hex,
            TAUCETI_SOURCE_PYTHON=sys.executable,
            TAUCETI_SOURCE_GH_CONF=str(cfg.state / "gh-project.toml"),
            TAUCETI_FORGEJO_TOKEN_FILE=str(token_file),
            TAUCETI_FORGEJO_ORIGIN=source.repository.forge.url,
            TAUCETI_FORGEJO_USER_ID=actor.principal.native_id,
            TAUCETI_PUSH_REMOTE=source.repository.clone_url,
            TAUCETI_CLAIM_SH=str(claim_wrapper),
            TAUCETI_REAL_GH=real_gh or "",
            GH_REPO=source.repository.forge.url.removeprefix("https://") + "/" + source.repository.name,
            PATH=str(directory) + os.pathsep + original.get("PATH", ""),
        )
        from .paths import self_env

        os.environ.update(self_env())
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)
        _selected.reset(marker)
        if token_file != source.token_file:
            token_file.unlink(missing_ok=True)


def _verified_token_copy(original, token):
    # Beside the original: native_source already read it through the reader that source
    # subprocesses use, which requires a private parent and no untrusted-writable ancestor.
    path = original.parent / f".{original.name}.round-{uuid.uuid4().hex}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token)
    return path


@contextlib.contextmanager
def shared_github_environment(*, required=True):
    """Use existing GitHub credentials only for shared GitHub coordination/tools."""
    import subprocess

    from tauceti_tools.git_credentials import scoped_git_environment

    account = os.environ.get("TAUCETI_GITHUB_ACCOUNT")
    if account:
        # pin_github_account verified GH_TOKEN for this account at the start of the round.
        pinned = os.environ.get("_TAUCETI_PINNED_GITHUB_ACCOUNT", "").lower() == account.lower()
        token = os.environ.get("GH_TOKEN") if pinned else ""
        if not token and required:
            raise SourceUnavailable(f"the verified GitHub credential for {account} is unavailable")
    else:
        gh = os.environ.get("TAUCETI_REAL_GH") or shutil.which("gh")
        if not gh and required:
            raise SourceUnavailable("shared GitHub credentials unavailable")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token and gh:
            credential = subprocess.run(
                [gh, "auth", "token", "--hostname", "github.com"], capture_output=True, text=True
            )
            if (credential.returncode or not credential.stdout.strip()) and required:
                raise SourceUnavailable("shared GitHub credentials unavailable")
            token = credential.stdout.strip() if credential.returncode == 0 else ""
    original = dict(os.environ)
    marker = _selected.set(None)
    try:
        env = (
            scoped_git_environment(GITHUB, token, purpose="workspace")
            if token
            else {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "0",
            }
        )
        for key in list(os.environ):
            if key.startswith(("GIT_CONFIG_", "TAUCETI_SOURCE_", "TAUCETI_FORGEJO_")) or key in (
                "GH_REPO",
                "TAUCETI_CLAIM_SH",
            ):
                os.environ.pop(key, None)
        os.environ.update(env)
        # The real GitHub CLI must also be used by subprocesses.
        native_bin = str(Path(original.get("TAUCETI_SOURCE_STATE", "/nonexistent")) / "bin")
        os.environ["PATH"] = os.pathsep.join(
            part for part in original.get("PATH", "").split(os.pathsep) if part != native_bin
        )
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)
        _selected.reset(marker)
