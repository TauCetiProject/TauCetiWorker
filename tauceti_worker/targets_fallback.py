"""Authoring outside an operator target list in a round the list offers nothing.

docs/target-lists.md ("Authoring outside the list") documents the behaviour. do_roadmap calls
`admit` with the reason the list gave: it returns when the round may author as a worker without a
list does, and raises NoProgress otherwise.

    cutoff()                          this round's cutoff: the cap file's integer, else the setting
    outside_list(targets, rows, owns)  the account's open pull requests that serve no item of the list
    slot_dir(source, login)           where the rounds authoring on one forge as one account hold places
    hold_slot(directory, wid, n)      one of `n` places among the workers that share `directory`
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from tauceti_tools.forge import ForgeError, Incomplete

from . import github_transport as transport
from .config import NoProgress, SourceUnavailable, log, targets_fallback_cap_file, targets_fallback_max_open
from .github import GitHubError, me
from .targets import Targets, load_targets, marker_keys


def cutoff() -> int | None:
    """None while TAUCETI_TARGETS_FALLBACK_MAX_OPEN is unset, whatever the cap file holds."""
    configured = targets_fallback_max_open()
    path = targets_fallback_cap_file()
    if configured is None or path is None:
        return configured
    try:
        text = path.read_text().strip()
        if re.fullmatch(r"[0-9]+", text):
            return int(text)
        problem = "it does not hold one non-negative integer"
    except (OSError, UnicodeDecodeError) as e:
        problem = getattr(e, "strerror", None) or str(e)
    log(f"roadmap: cap file {path}: {problem}; the cutoff stays {configured}")
    return configured


def outside_list(targets: Targets, rows: Iterable[dict], owns: Callable[[dict | None], bool]) -> list[int]:
    """The open, non-draft pull requests among `rows` whose author `owns` accepts and none of whose
    target markers names an item of the list."""
    listed = {(it.area, it.slug) for it in targets.items()}
    return [
        d["number"]
        for d in rows
        if owns(d.get("author")) and not d.get("isDraft") and not marker_keys(d.get("body"))[0] & listed
    ]


def slot_dir(source, login: str) -> Path:
    """GitHub keeps the per-login directory; another forge's account gets one under its forge."""
    base = transport.root() / "targets-fallback"
    if not getattr(source, "direct_source", False):
        return base / login.lower()
    forge = source.repository.forge
    return base / f"{forge.kind}:{urlsplit(forge.url).netloc.lower()}" / login.lower()


def hold_slot(directory: Path, wid: str, room: int) -> tuple[int | None, int]:
    """Take a place for one round, unless `room` other processes hold one. Returns the descriptor
    whose lock is the place (closing it, or the process ending, frees it) or None, and how many
    places were held. Raises OSError when `directory` cannot be used."""
    with transport.locked(directory / "lock"):
        held = 0
        for path in directory.glob("*.slot"):
            with open(path, "a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held += 1
                else:
                    path.unlink(missing_ok=True)
        if held >= room:
            return None, held
        fd, _ = tempfile.mkstemp(dir=directory, prefix=f"{wid}.", suffix=".slot")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd, held


def _shared_github():
    from .worker_sources import current, shared_github_environment

    return shared_github_environment() if current() is not None else contextlib.nullcontext()


def _account(source, sv):
    """The login `source` acts as and a test of a row's author against it."""
    if getattr(source, "direct_source", False):
        actor = source.authenticated_actor()
        if actor is None:
            raise SourceUnavailable(f"the account of {source.repository.url} is unavailable")
        forge = source.repository.forge

        def owns(author):
            from .source_identity import SourceActor

            try:
                return isinstance(author, dict) and SourceActor.from_projection(forge, author) == actor
            except Incomplete:
                return False

        return actor.login, owns
    with _shared_github():
        login = (sv is not None and getattr(sv, "me_login", "")) or me()
    return login, lambda author: (author or {}).get("login") == login


def count_outside(targets: Targets, w, sv) -> tuple[str, int]:
    """The login the round authors as, and how many open pull requests outside the list the account
    holds across the round's sources: its own and, in a multi-source round, the others."""
    from .work_units import _target_reads

    authoring = None
    n_open = 0
    for source in (w.gh, *getattr(w, "target_sources", ())):
        login, owns = _account(source, sv if authoring is None else None)
        authoring = authoring or login
        rows = _target_reads(source).open_prs()
        if rows is None:
            raise SourceUnavailable("open pull requests are unavailable")
        n_open += len(outside_list(targets, rows, owns))
    return authoring, n_open


def admit(w, sv, path: Path, nothing: NoProgress) -> None:
    """Let this round author outside the list at `path`, which offered it nothing for the reason
    `nothing`: the account's pull requests outside the list on every source of the round, plus the
    rounds on this host now authoring one on the same forge as the same account, are fewer than
    `cutoff()`. The slot taken here is released with the round. Raises `nothing` itself when the
    setting is off, and NoProgress with both reasons when there is no room."""
    limit = cutoff()
    if limit is None:
        raise nothing
    try:
        login, n_open = count_outside(load_targets(path), w, sv)
    except (GitHubError, ForgeError, SourceUnavailable) as e:
        raise NoProgress(f"{nothing}; cannot count the pull requests outside the list ({e})") from None
    fd, held = None, 0
    if n_open < limit:
        try:
            fd, held = hold_slot(slot_dir(w.gh, login), w.cfg.wid, limit - n_open)
        except OSError as e:
            raise NoProgress(f"{nothing}; cannot take a slot for authoring outside the list ({e})") from None
    count = f"{n_open} open outside it" + (f" + {held} being authored" if held else "")
    if fd is None:
        raise NoProgress(f"{nothing}; not authoring outside the list either ({count}, cutoff {limit})")
    rc = getattr(w, "rc", None)
    if rc is not None:
        rc.add_cleanup(lambda: os.close(fd))
    log(f"{nothing} — authoring outside the target list ({count}, cutoff {limit})")
