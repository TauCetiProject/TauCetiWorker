"""Bors-compatible command parsing and execution for the AI merge queue.

The queue is a drop-in replacement for Bors's approval intake: the same comment lines from the
same people do the same thing. The grammar follows bors-ng's COMMAND_PARSING.md for the commands
the queue runs (`r+`, `merge`, `r=`, `merge=`, `p=`, `single`, `r-`, `merge-`, `cancel`, `ping`)
and keeps its principles: a line that is an attempt at a command either runs exactly as written or
gets a reply saying what did not run and why; a line that is prose gets nothing; a line that could
mean two things is refused, never guessed. Bors commands the queue has no equivalent for (`try`,
`retry`, delegation, `link`, `stack`) are recognised so they are refused with a reply rather than
read as prose.

The review App (`tauceti-review-bot[bot]`) approves with `bors r+ sha=<head>` (or
`bors r+ single sha=<head>`, `bors r- sha=<head>`); that exact-head form is accepted only from the
App that Bors accepts it from, and only while the live head is that SHA.

`run_comment` applies a comment: permission first (Bors's reviewer check, a collaborator with push
access), then the draft rule, then each command in order against the controller, recording the
evidence Bors records (actor, time, comment, head at approval, and the merge base when the actor
is a repository admin so the scope-approval check keeps working).
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Sequence

log = logging.getLogger("tauceti_queue.commands")

TRIGGER = "bors"
MIN_PRIORITY = -2_147_483_648
MAX_PRIORITY = 2_147_483_647
SHA = re.compile(r"[0-9a-f]{40}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?|[A-Za-z0-9]")
TRIGGER_LINE = re.compile(rf"^(?P<trigger>{TRIGGER}|bros):?\s(?P<command>.+)", re.IGNORECASE)
UNSPACED_LINE = re.compile(rf"^{TRIGGER}:(?P<command>\S.*)", re.IGNORECASE)
# A command word running on into a longer word is prose (`bors merged this`); see COMMAND_PARSING.md.
RUN_ON = re.compile(
    r"^(?:(?:merge|link)-?[^\W_]|try(?:[^\W_]|-\S)|(?:single|ping|retry|cancel|unlink|stack)[\w-]|(?:delegate|d(?!elegate))[^\s+=-])",
    re.UNICODE,
)
UNSUPPORTED = ("try-", "try", "retry", "delegate", "d", "link-", "link", "unlink", "stack")
ACCEPTED = "`bors r+`, `bors merge`, `bors r=<login>`, `bors p=<n>`, `bors r-` and `bors cancel`"


@dataclasses.dataclass(frozen=True)
class Command:
    """One parsed command line, or the reply it earns instead."""

    kind: str  # approve | unapprove | cancel | priority | single | ping | refusal | autocorrect | unsupported | bros
    typed: str = ""
    reviewer: str | None = None  # the login named by `r=`; None means the commenter
    priority: int | None = None
    single: bool | None = None
    sha: str | None = None  # exact-head binding from the review App's `sha=` form
    reply: str = ""
    level: str = "reviewer"  # none | member | reviewer

    @property
    def runs(self) -> bool:
        return self.kind in ("approve", "unapprove", "cancel", "priority", "single", "ping")

    @property
    def merge_bound(self) -> bool:
        """Commands a draft refuses: everything that can lead to a merge."""
        return self.kind in ("approve", "priority", "single", "unsupported") or (
            self.kind in ("refusal", "autocorrect") and self.level == "reviewer"
        )


# ---------------------------------------------------------------------------------------------
# Parsing


def parse(body: str | None, *, review_app: bool = False) -> list[Command]:
    """Every command a comment spells, line by line, with replies for what cannot run."""
    if not body:
        return []
    commands: list[Command] = []
    for line in body.splitlines():
        match = TRIGGER_LINE.match(line)
        if match:
            if match.group("trigger").lower() == "bros":
                if parse_cmd(match.group("command").strip(), review_app=review_app):
                    commands.append(Command("bros", typed=line.strip(), level="none"))
                continue
            commands.extend(_parse_trigger_line(match.group("command").strip(), review_app))
            continue
        unspaced = UNSPACED_LINE.match(line)
        if unspaced:
            commands.extend(_parse_unspaced(unspaced.group("command").strip(), review_app))
    return commands


def _real(commands: list[Command]) -> bool:
    return bool(commands) and all(c.runs for c in commands)


def _parse_trigger_line(cmd: str, review_app: bool) -> list[Command]:
    commands = parse_cmd(cmd, review_app=review_app)
    if _real(commands):
        return commands
    corrected, hints = _correction(cmd, review_app)
    if corrected is not None:
        return [_autocorrect(corrected)]
    if hints and not commands:
        return hints
    return commands


def _parse_unspaced(cmd: str, review_app: bool) -> list[Command]:
    commands = parse_cmd(cmd, review_app=review_app)
    if _real(commands):
        return [_autocorrect(cmd)]
    if commands:
        return commands
    corrected, hints = _correction(cmd, review_app)
    if corrected is not None:
        return [_autocorrect(corrected)]
    return hints


def _autocorrect(corrected: str) -> Command:
    level = required_level(parse_cmd(corrected))
    return Command("autocorrect", typed=corrected, reply=f"Did you mean `{TRIGGER} {corrected}`?", level=level)


def _correction(cmd: str, review_app: bool) -> tuple[str | None, list[Command]]:
    """The text Bors would read if the command word were lowercased or its operators closed up."""
    lowered = re.sub(r"^[A-Za-z]+", lambda m: m.group(0).lower(), cmd)
    closed = re.sub(r"^([a-z]+)\s+([+-])(?=\s|=|$)", r"\1\2", lowered)
    closed = re.sub(r"^([a-z]+[+-]?)\s*=\s*", r"\1=", closed)
    swapped = re.sub(r"^([+-])r?(?![\w])", r"r\1", cmd)
    candidates = []
    for text in (lowered, closed, swapped):
        if text != cmd and text not in candidates:
            candidates.append(text)
    parsed = [(text, parse_cmd(text, review_app=review_app)) for text in candidates]
    for text, commands in parsed:
        if _real(commands):
            return text, []
    for _, commands in parsed:
        if commands:
            return None, commands
    return None, []


def parse_cmd(cmd: str, *, review_app: bool = False) -> list[Command]:
    """A trimmed command line after the trigger: commands, replies, or nothing for prose."""
    if not cmd.startswith("try"):
        cmd = re.sub(r"\s+", " ", cmd)
    if RUN_ON.match(cmd):
        return []
    for word in ("r=", "merge="):
        if cmd.startswith(word):
            return _activation_by(word, cmd[len(word) :])
    for word, kind, level in (
        ("r-", "unapprove", "member"),
        ("merge-", "unapprove", "member"),
        ("cancel", "cancel", "member"),
    ):
        if cmd.startswith(word):
            rest = cmd[len(word) :]
            if review_app and kind == "unapprove":
                sha_match = re.fullmatch(r" sha=([0-9a-f]{40})", rest)
                if sha_match:
                    return [Command(kind, typed=cmd, sha=sha_match.group(1), level=level)]
            return _no_args(Command(kind, typed=cmd, level=level), word, rest)
    for word in ("r+", "merge"):
        if cmd.startswith(word):
            return _activation(word, cmd[len(word) :], review_app)
    if cmd.startswith("p="):
        return _priority("p=", cmd[2:], cmd)
    if cmd.startswith("single"):
        return _single("single", cmd[len("single") :], cmd)
    if cmd.startswith("ping"):
        return _no_args(Command("ping", typed=cmd, level="none"), "ping", cmd[4:])
    for word in UNSUPPORTED:
        if (
            cmd == word
            or cmd.startswith(word + " ")
            or (word in ("d", "delegate") and cmd.startswith(word) and cmd[len(word) : len(word) + 1] in "+-=")
        ):
            return [
                Command(
                    "unsupported",
                    typed=cmd,
                    reply=f":-1: {TRIGGER} did not run `{TRIGGER} {cmd}`: the merge queue does not run `{word.rstrip('-') or word}`. It accepts {ACCEPTED}.",
                    level="reviewer",
                )
            ]
    return []


def _no_args(command: Command, understood: str, rest: str) -> list[Command]:
    if rest == "":
        return [command]
    return [_leftover(understood + rest, understood, rest, command.level)]


def _leftover(typed: str, understood: str, rest: str, level: str = "reviewer") -> Command:
    shown = rest.strip()
    return Command(
        "refusal",
        typed=typed,
        reply=(
            f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: `{TRIGGER} {understood}` takes nothing after it, but `{shown}` "
            f"followed. Reply with just `{TRIGGER} {understood}`, and put any other command on a line of its own."
        ),
        level=level,
    )


def _activation(word: str, rest: str, review_app: bool) -> list[Command]:
    if rest == "":
        return [Command("approve", typed=word)]
    if review_app:
        sha_match = re.fullmatch(r" (single )?sha=([0-9a-f]{40})", rest)
        if sha_match:
            return [
                Command(
                    "approve", typed=word + rest, sha=sha_match.group(2), single=True if sha_match.group(1) else None
                )
            ]
    if rest.startswith(" p="):
        priority = _priority(f"{word} p=", rest[3:], word + rest)
        if priority[0].kind != "priority":
            return priority
        return [Command("approve", typed=word + rest, priority=priority[0].priority)]
    if rest.startswith(" single"):
        single = _single(f"{word} single", rest[len(" single") :], word + rest)
        if single[0].kind != "single":
            return single
        return [Command("approve", typed=word + rest, single=single[0].single)]
    return [_leftover(word + rest, word, rest)]


def _activation_by(word: str, arguments: str) -> list[Command]:
    typed = word + arguments
    # Names separated by commas (`, ` too); a space ends them, so `r=alice thanks` never approves
    # for a user called `thanks`.
    names_part, _, tail = re.sub(r",\s*", ",", arguments.strip()).partition(" ")
    names = [name.replace("@", "") for name in names_part.split(",") if name.replace("@", "")]
    if not names:
        return [
            Command(
                "refusal",
                typed=typed,
                reply=f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: `{TRIGGER} {word}` needs at least one login after it, such as `{TRIGGER} {word}alice`.",
            )
        ]
    bad = [name for name in names if not LOGIN.fullmatch(name)]
    if bad:
        shown = ", ".join(f"`{name}`" for name in bad)
        return [
            Command(
                "refusal",
                typed=typed,
                reply=f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: {shown} cannot be a GitHub login. Separate several logins with commas.",
            )
        ]
    mentions = ",".join(names)
    if tail == "":
        return [Command("approve", typed=typed, reviewer=mentions)]
    if tail.startswith("p="):
        priority = _priority(f"{word}{mentions} p=", tail[2:], typed)
        if priority[0].kind != "priority":
            return priority
        return [Command("approve", typed=typed, reviewer=mentions, priority=priority[0].priority)]
    return [_leftover(typed, word + mentions, " " + tail)]


def _priority(prefix: str, text: str, typed: str) -> list[Command]:
    match = re.match(r"^([+-]?\d+)(.*)$", text)
    if not match or (match.group(2) and not match.group(2)[0].isspace()):
        return [
            Command(
                "refusal",
                typed=typed,
                reply=f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: `p=` needs a whole number, such as `{TRIGGER} {prefix}10`.",
            )
        ]
    value = int(match.group(1))
    if not MIN_PRIORITY <= value <= MAX_PRIORITY:
        return [
            Command(
                "refusal",
                typed=typed,
                reply=f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: a priority must lie between {MIN_PRIORITY} and {MAX_PRIORITY}.",
            )
        ]
    rest = match.group(2)
    if rest:
        return [_leftover(typed, f"{prefix}{value}", rest)]
    return [Command("priority", typed=typed, priority=value)]


def _single(prefix: str, text: str, typed: str) -> list[Command]:
    match = re.match(r"^ (on|off)((?: .*)?)$", text)
    if not match:
        return [
            Command(
                "refusal",
                typed=typed,
                reply=f":-1: {TRIGGER} did not run `{TRIGGER} {typed}`: `single` takes `on` or `off`, as in `{TRIGGER} {prefix} on`.",
            )
        ]
    if match.group(2):
        return [_leftover(typed, f"{prefix} {match.group(1)}", match.group(2))]
    return [Command("single", typed=typed, single=match.group(1) == "on")]


LEVELS = {"none": 0, "member": 1, "reviewer": 2}


def required_level(commands: Sequence[Command]) -> str:
    """The permission a whole comment needs: that of its most demanding command or reply."""
    level = "none"
    for command in commands:
        if LEVELS[command.level] > LEVELS[level]:
            level = command.level
    return level


# ---------------------------------------------------------------------------------------------
# Execution


@dataclasses.dataclass(frozen=True)
class Evidence:
    """What Bors records about an admission: who, when, through which comment, at which head."""

    kind: str  # issue_comment | pull_request_review | review_app
    actor_login: str
    actor_id: int
    created_at: str
    comment_id: int | None = None
    comment_url: str = ""
    head_sha: str = ""
    merge_base_sha: str | None = None
    admin: bool = False
    single: bool | None = None
    command: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CommentContext:
    repo: str
    pr_number: int
    actor_login: str
    actor_id: int
    body: str
    created_at: str
    comment_id: int | None = None
    comment_url: str = ""
    kind: str = "issue_comment"
    review_app: bool = False
    # Set for pull request reviews: the head the review was submitted against.
    review_head: str | None = None


def live_pull_request(adapter, repo: str, number: int):
    """The live PR, or None when GitHub does not know it (`fetch_pr` raises a `LookupError`)."""
    try:
        return adapter.fetch_pr(repo, number)
    except LookupError:
        return None


PERMISSION_DENIED = (
    ":lock: Permission denied\n\nOnly collaborators with push access to the repository can run that command."
)


def draft_reply(commands: Sequence[Command]) -> str:
    blocked = ", ".join(f"`{TRIGGER} {c.typed}`" for c in commands if c.merge_bound)
    return (
        f":-1: {TRIGGER} did not run {blocked}: this pull request is a draft. Mark it ready for review, then "
        "approve it again."
    )


def run_comment(controller, adapter, context: CommentContext, *, is_admin=None) -> list[str]:
    """Apply one comment's commands; returns the replies posted (for logs and tests).

    `controller` provides `approve(repo, pr, head_sha, reviewer, priority, evidence)` (evidence as
    a dict), `unapprove(repo, pr, actor)`, `cancel(repo, pr, actor)` and `set_priority(repo, pr,
    priority)`; `adapter` is the GitHub port, used for the live PR, the reviewer check and replies.
    """
    commands = parse(context.body, review_app=context.review_app)
    if not commands:
        return []
    replies: list[str] = []
    repo, number = context.repo, context.pr_number
    level = required_level(commands)

    def reply(text: str, key: str) -> None:
        replies.append(text)
        adapter.post_comment(repo, number, text, f"reply {key}")

    reply_key = f"{context.kind}:{context.comment_id or context.created_at}"

    if level != "none":
        pr = live_pull_request(adapter, repo, number)
        if pr is None:
            log.warning("command on %s#%s ignored: pull request unavailable", repo, number)
            return []
        permitted = context.review_app or adapter.reviewer(context.actor_login, repo)
        if not permitted:
            reply(PERMISSION_DENIED, reply_key)
            return replies
        if pr.draft and any(c.merge_bound for c in commands):
            reply(draft_reply(commands), reply_key)
            commands = [c for c in commands if not c.merge_bound]
    else:
        pr = None

    admin = None
    for index, command in enumerate(commands):
        key = f"{reply_key}:{index}"
        if command.kind in ("refusal", "autocorrect", "unsupported"):
            reply(command.reply, key)
            continue
        if command.kind in ("bros", "ping"):
            if command.kind == "ping":
                reply(":pong: pong", key)
            continue
        if command.sha is not None and pr is not None and (pr.head_sha != command.sha or pr.state != "open"):
            log.info("stale exact-head command on %s#%s ignored: live head %s", repo, number, pr.head_sha)
            continue
        if command.kind == "approve":
            head = command.sha or (context.review_head if context.review_head else pr.head_sha)
            if admin is None:
                if context.review_app:
                    admin = False
                else:
                    admin = (
                        bool(is_admin) if is_admin is not None else adapter.repository_admin(context.actor_login, repo)
                    )
            merge_base = adapter.pr_merge_base(repo, number, head) if admin else None
            evidence = Evidence(
                kind="review_app" if context.review_app else context.kind,
                actor_login=context.actor_login,
                actor_id=context.actor_id,
                created_at=context.created_at,
                comment_id=context.comment_id,
                comment_url=context.comment_url,
                head_sha=head,
                merge_base_sha=merge_base,
                admin=admin,
                single=command.single,
                command=f"{TRIGGER} {command.typed}",
            )
            reviewer = command.reviewer or context.actor_login
            controller.approve(repo, number, head, reviewer, command.priority or 0, evidence.as_dict())
        elif command.kind == "unapprove":
            controller.unapprove(
                repo,
                number,
                context.actor_login,
                **({"head_sha": pr.head_sha} if getattr(controller, "admission", None) is not None else {}),
            )
        elif command.kind == "cancel":
            controller.cancel(
                repo,
                number,
                context.actor_login,
                **({"head_sha": pr.head_sha} if getattr(controller, "admission", None) is not None else {}),
            )
        elif command.kind == "priority":
            controller.set_priority(repo, number, command.priority)
        elif command.kind == "single":
            # The queue has no singleton builds; `single on` is accepted for compatibility and recorded
            # with the next approval's evidence only.
            log.info("single %s on %s#%s accepted without effect", command.single, repo, number)
    return replies
