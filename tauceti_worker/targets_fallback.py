"""Authoring outside an operator target list in a round the list offers nothing.

docs/target-lists.md ("Authoring outside the list") documents the behaviour. do_roadmap calls
`admit` with the reason the list gave: it returns when the round may author as a worker without a
list does, and raises NoProgress otherwise.

    cutoff()                       this round's cutoff: the cap file's integer, else the setting
    outside_list(targets, gh, me)  the account's open pull requests that serve no item of the list
    hold_slot(directory, wid, n)   one of `n` places among the workers that share `directory`
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from pathlib import Path

from . import github_transport as transport
from .config import NoProgress, log, targets_fallback_cap_file, targets_fallback_max_open
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


def outside_list(targets: Targets, gh, login: str) -> list[int]:
    """The open, non-draft pull requests of `login` none of whose target markers names an item of
    the list. Raises GitHubError when the listing fails."""
    listed = {(it.area, it.slug) for it in targets.items()}
    return [
        d["number"]
        for d in gh.open_prs()
        if (d.get("author") or {}).get("login") == login
        and not d.get("isDraft")
        and not marker_keys(d.get("body"))[0] & listed
    ]


def slot_dir(login: str) -> Path:
    return transport.root() / "targets-fallback" / login.lower()


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


def admit(w, sv, path: Path, nothing: NoProgress) -> None:
    """Let this round author outside the list at `path`, which offered it nothing for the reason
    `nothing`: the account's pull requests outside the list, plus the rounds on this host now
    authoring one, are fewer than `cutoff()`. The slot taken here is released with the round. Raises
    `nothing` itself when the setting is off, and NoProgress with both reasons when there is no room."""
    limit = cutoff()
    if limit is None:
        raise nothing
    login = getattr(sv, "me_login", "") or me()
    try:
        n_open = len(outside_list(load_targets(path), w.gh, login))
    except GitHubError as e:
        raise NoProgress(f"{nothing}; cannot count the pull requests outside the list ({e})") from None
    fd, held = None, 0
    if n_open < limit:
        try:
            fd, held = hold_slot(slot_dir(login), w.cfg.wid, limit - n_open)
        except OSError as e:
            raise NoProgress(f"{nothing}; cannot take a slot for authoring outside the list ({e})") from None
    count = f"{n_open} open outside it" + (f" + {held} being authored" if held else "")
    if fd is None:
        raise NoProgress(f"{nothing}; not authoring outside the list either ({count}, cutoff {limit})")
    rc = getattr(w, "rc", None)
    if rc is not None:
        rc.add_cleanup(lambda: os.close(fd))
    log(f"{nothing} — authoring outside the target list ({count}, cutoff {limit})")
