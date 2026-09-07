"""tauceti — the Tau Ceti worker.

Bare `tauceti` opens a dashboard + launcher; `tauceti work [--loop]` does the work
(one round, or the driver loop); `tauceti status` prints the read-only survey.

The worker acts on TauCetiProject/TauCeti as the authenticated `gh` account, and
treats that account's own PRs as the ones it tends. Each round does exactly ONE unit
of work, chosen in priority order: rebase → bump → progress → fix-ci → fix → review → roadmap.
The `bump` step adapts a red bump-mathlib PR (the review bot opens those; the worker
never authors a bump). Merging, abandoning, and de-duplicating PRs is the repo's CI,
not the worker.

(This module is the CLI entry point; `argparse` shows this docstring as `tauceti --help`.)"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .agents import (
    BUBBLE_MIN_VERSION,
    BUBBLE_REPO,
    KIRO_BUBBLE_MIN_VERSION,
    bubble_cmd_is_disposable,
    bubble_supports_allow_push,
    bubble_supports_lake_cache_service,
    bubble_version_meets_minimum,
    ensure_fork_proxy_current,
    installed_bubble_version,
    isolate_home,
    resolve_authoring_profile,
    run_in_bubble,
)
from .config import (
    Config,
    Die,
    NoProgress,
    acquire_slot,
    auto_assign_wid,
    is_git_url,
    log,
    roadmap_only,
    sanitize_wid,
    set_log_file,
    warn_red,
)
from .constants import (
    AGENTS,
    ALLOWED_TASKS,
    CLAIMS,
    EX_NOPROGRESS,
    OPENROUTER_MODELS,
    PR_TASKS,
    TAUCETI,
    WORK_TASKS,
)
from .github import GitHub, shared_claims_granted
from .loop import cmd_loop, resolve_work_model
from .paths import HERE, ensure_ssl_cert_file
from .quota import Quota, _claude_keychain_creds, _safe_exists, claude_dir, codex_dir, parse_pace_curve
from .review_state import ReviewState
from .round import Claims, RoundContext, cmd_heartbeat
from .runtime_status import report_failure
from .survey import Counters, survey
from .tui import cmd_tui, render_survey
from .usage import kiro_data_dir, usage_snapshot
from .work_units import RoundOpts, Worker, _bubble, raise_on_account_mismatch, run_round, want
from .worker_manager import WorkersError, add_workers_parser, cmd_managed_runner, cmd_workers

# ============================================================================
# CLI
# ============================================================================

WORK_EPILOG = """\
the cascade (priority order; a round does the first that applies):
  rebase    bring our open PRs up to date with their base branch
  bump      adapt a red bump-mathlib PR (the worker never authors one)
  progress  write a roadmap's STATUS.md / PROGRESS.md report (globally paced at 8h)
  fix-ci    fix red CI on one of our PRs
  fix       address review feedback on one of our PRs
  review    review an open PR (runs the tauceti-review engine)
  roadmap   open a new PR for a roadmap item

  With no --only a round walks the whole cascade; --only pins it to a subset,
  --skip drops a subset (and the two combine by subtraction). --pr narrows the
  same round to named pull requests: it filters what the round would already
  have done, so progress and roadmap (which name no existing PR) drop out.

examples:
  tauceti work                          one round: auto agent, on the host
  tauceti work --loop                   the driver: keep picking the best job
  tauceti work --loop --only review     a focused reviewer
  tauceti work --loop --skip roadmap    the whole cascade except authoring new PRs
  tauceti work --pr 412                 whatever the cascade wants to do to PR #412
  tauceti work --pr 412,415 --only review,fix   only those two PRs, only those two units
  tauceti work --only roadmap --roadmap-only ReductiveGroups
  tauceti work --loop --roadmap-skip OneParameterSemigroups   leave that area to other workers
  tauceti work --only review --agent claude --bubble
                                        review with Opus inside the Bubble sandbox
  tauceti work --dry-run                show what it WOULD do; act on nothing

multiple workers (share a host and coordinate through GitHub; a distinct id namespaces each):
  tauceti work --loop                   auto-assigns worker1, worker2, ... per terminal
  tauceti work --loop --worker-id alice --only review
  tauceti work --loop --worker-id bob   --only roadmap

environment (flags win; full reference linked below):
  TAUCETI_AGENT          default for --agent
  TAUCETI_WORKER_ID      pins the worker id (else `work` auto-assigns worker1, worker2, ...)
  TAUCETI_ROADMAP_ONLY   single roadmap area (unset = a fresh random area each round; "" = all areas)
  TAUCETI_ROADMAP_SKIP   comma-separated roadmap areas to exclude from selection
  TAUCETI_PR             comma-separated PR numbers; default for --pr
  TAUCETI_QUOTA_CMD      default for --quota-cmd
  TAUCETI_PACE           pacing curve "t:b,..." (default = 60:40); see --pace
  TAUCETI_AUTHORING_CODEX_MODEL / _EFFORT   exact Codex authoring profile
  TAUCETI_AUTHORING_CLAUDE_MODEL / _EFFORT exact Claude authoring profile
  TAUCETI_STREAM=1       same as --stream
  TAUCETI_AUTO_REFRESH=1 same as --auto-refresh (renew an expired Claude token; see --auto-refresh)
  TAUCETI_ACCOUNT        default for --account (require a specific Codex account)
  CLAUDE_CONFIG_DIR      Claude config/credential source (Bubble uses a private macOS handoff)
                         (account switching, where the creds live in a file)
  CODEX_HOME             Codex config/credential source; point it at a private directory to give
                         TauCeti its own Codex account without disturbing your interactive one

full reference:
  https://github.com/kim-em/TauCetiWorker/blob/main/docs/reference.md
"""


def add_work_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--loop",
        action="store_true",
        help="run the driver: keep doing rounds (pacing against quota between them) "
        "instead of the default single round",
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TASKS",
        help="restrict the round to these work units, a comma list of: "
        + ", ".join(ALLOWED_TASKS)
        + " (default: walk the whole cascade)",
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="TASKS",
        help="drop these work units from the round (the inverse of --only); a comma list of "
        "the same names. Combines with --only by subtraction (--only review,fix --skip "
        "fix runs only review)",
    )
    p.add_argument(
        "--pr",
        action="append",
        default=[],
        metavar="N[,N...]",
        help="work only on these pull requests: a comma list (or repeated flag) of PR numbers. "
        "Filters what the round would already have done — it can never make a PR actionable that "
        "the survey passed over, and never bypasses claims, attempt budgets, or the review caps. "
        "Combines with --only by intersection; drops the two work units that name no existing PR "
        "(progress, roadmap), so a targeted round never falls through to unrelated work. When none "
        "of the named PRs are actionable the round says why, per PR, and stops "
        "(or $TAUCETI_PR)",
    )
    p.add_argument(
        "--agent",
        choices=AGENTS,
        default=None,
        help="which agent to run: auto (Codex preferred, Opus fallback), codex, claude, "
        "kiro (exact model, subscription credits), or deepseek/minimax (pay-per-token "
        "OpenRouter). Kiro/OpenRouter are explicit-only and unpaced "
        "(default: $TAUCETI_AGENT or auto)",
    )
    p.add_argument(
        "--account",
        default=os.environ.get("TAUCETI_ACCOUNT"),
        metavar="EMAIL_OR_ID",
        help="require the agent's credential to be this account (email, or the workspace UUID "
        "`tauceti doctor` prints), and refuse to run otherwise. TauCeti never switches accounts; "
        "this only checks, and the error says how to switch. Codex only, so it needs an explicit "
        "--agent codex (or $TAUCETI_AGENT=codex). Defaults to $TAUCETI_ACCOUNT",
    )
    p.add_argument(
        "--author-model",
        default=None,
        metavar="MODEL",
        help="exact authoring model for an explicit --agent; overrides the provider-specific environment default",
    )
    p.add_argument(
        "--author-effort",
        default=None,
        metavar="EFFORT",
        help="authoring reasoning effort for an explicit --agent; overrides the provider-specific environment default",
    )
    p.add_argument(
        "--resolved-author-fallback-model",
        default=None,
        help=argparse.SUPPRESS,  # internal loop-parent -> isolated _round provenance handoff
    )
    p.add_argument(
        "--bubble",
        action="store_true",
        help="run eligible code and review agents inside the Bubble sandbox, with scoped "
        "credentials and network, instead of directly on the host. Progress reports remain "
        "on the host (default: run on the host)",
    )
    p.add_argument(
        "--host",
        action="store_true",
        help="DEPRECATED, now the default: the agent already runs directly on the host, so this "
        "is a no-op that only warns. Pass --bubble to run inside the sandbox instead",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="stream the agent's (noisy) conversation log to the terminal; "
        "default redirects it to a file under logs/ and prints the path (or $TAUCETI_STREAM=1)",
    )
    p.add_argument(
        "--roadmap-only",
        dest="roadmap_only",
        default=None,
        metavar="AREA",
        help="for roadmap rounds, the single roadmap area to steer toward: a subdirectory of "
        "the TauCetiRoadmap repo. List them by opening the dashboard (bare `tauceti`) and "
        "expanding the roadmap row, or browse github.com/TauCetiProject/TauCetiRoadmap. "
        "Empty string = all areas; omit entirely (and leave $TAUCETI_ROADMAP_ONLY "
        "unset) to pick a fresh random area each round. Overrides "
        "$TAUCETI_ROADMAP_ONLY for this run",
    )
    p.add_argument(
        "--source",
        default=None,
        metavar="PATH_OR_URL",
        help="supplementary source material from a local Git repository directory or Git repository URL "
        "(checked-out/default HEAD) for a new roadmap PR. Requires the roadmap phase to be enabled and "
        "one specific --roadmap-only area; the source is mounted read-only in bubble mode and treated "
        "as non-definitive reference material",
    )
    p.add_argument(
        "--roadmap-skip",
        dest="roadmap_skip",
        default=None,
        metavar="AREA[,AREA...]",
        help="for roadmap rounds, a comma-separated list of roadmap areas to exclude from "
        "selection (so concurrent workers can divide the roadmap) — e.g. "
        "--roadmap-skip OneParameterSemigroups. Excludes them from the auto-random pick and the "
        "all-areas case; --roadmap-only takes precedence if it names a skipped area. Overrides "
        "$TAUCETI_ROADMAP_SKIP for this run",
    )
    p.add_argument(
        "--roadmap-extra-identities",
        dest="roadmap_extra_identities",
        default=None,
        metavar="LOGIN[,LOGIN...]",
        help="additional GitHub logins, beyond your `gh auth` identity, whose registered "
        "intentions this worker should treat as its own (so it won't avoid targets they've "
        "claimed on the intentions board). Comma-separated. Overrides "
        "$TAUCETI_ROADMAP_EXTRA_IDENTITIES for this run",
    )
    p.add_argument(
        "--ignore-claims",
        dest="respect_claims",
        action="store_false",
        default=None,
        help="opt OUT of avoiding roadmap targets other contributors have claimed on the "
        "intentions board (claim-respect is on by default). Sets $TAUCETI_RESPECT_CLAIMS=false",
    )
    p.add_argument(
        "--auto-refresh",
        dest="auto_refresh",
        action="store_true",
        default=None,
        help="renew this worker's Claude access token when it expires, instead of reporting Claude "
        "unavailable until a human runs `claude` again. ONLY safe when nothing else uses the same "
        "credential file: the refresh token is single-use, so an interactive `claude`, a second "
        "refresher, or a copy of the credential on another host can be logged out by the rotation. "
        "Off by default; sets $TAUCETI_AUTO_REFRESH=1 (inherited by loop children)",
    )
    p.add_argument(
        "--ignore-quota",
        dest="ignore_quota",
        action="store_true",
        help="ignore the quota PACER (run the requested --agent even when ahead of the burn pace); "
        "a HARD block — a window at 100%%, unreadable usage, or the usage endpoint refusing to answer — "
        "still backs off (needs an explicit paced --agent codex|claude — 'auto' can't choose without the pacer)",
    )
    # Internal: the loop sets this on a round it selected while a Claude window was reset-but-unopened.
    # It authorizes the round's launch stage to spend ONE small claude request to open that window,
    # once it has actually found work; it is not a way to bypass pacing (see Quota.authorize_claude_launch).
    p.add_argument(
        "--claude-bootstrap",
        dest="claude_bootstrap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    # UNDOCUMENTED, expert use only: two throttles on WHEN a worker reviews (see throttle_review in
    # work_units.py). Suppressed from --help, and left out of the README and docs/reference.md on
    # purpose — a default worker must never need them, and an undocumented flag can be retired
    # without a deprecation. $TAUCETI_REVIEW_MIN_QUEUE / $TAUCETI_REVIEW_MIN_AGE give a managed
    # worker the same control through its `env` table, since workers.toml has a closed schema.
    p.add_argument("--review-min-queue", dest="review_min_queue", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--review-min-age", dest="review_min_age", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--quota-cmd",
        default=os.environ.get("TAUCETI_QUOTA_CMD"),
        help="external command that decides agent availability, run as '<cmd> <agent>' "
        "(first stdout token = model to run; empty output or nonzero exit = wait); "
        "overrides the built-in pacer (or $TAUCETI_QUOTA_CMD)",
    )
    p.add_argument(
        "--pace",
        dest="pace",
        default=None,
        metavar="T:B[,T:B...]",
        help="pacing curve as `time%%:budget%%` control points, e.g. `0:10,50:70,90:90`: cap used%% at "
        "budget%% once time%% of the window has elapsed, linearly interpolated between points. An "
        "unspecified time 0 defaults to budget 0 and time 100 to budget 100. Default (unset) is "
        "`60:40` — 40%% of the quota through the first 60%% of a window, then a ramp to the full quota "
        "by the reset; `0:0,100:100` restores the plain used%% < elapsed%% rule. Overrides "
        "$TAUCETI_PACE for this run (inherited by loop children)",
    )
    p.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        metavar="ID",
        help="run an independent worker under this name: it namespaces the worker's state, "
        "checkout, review store, and logs, and (for any id other than 'default') isolates "
        "its credential directories; on non-macOS hosts that includes a private $HOME. "
        "With neither this flag nor "
        "$TAUCETI_WORKER_ID, `work` auto-assigns the lowest free slot (worker1, worker2, "
        "...) so several terminals on one host coexist without hand-numbering",
    )
    p.add_argument(
        "--isolate-home",
        dest="isolate_home",
        action="store_true",
        help="force credential isolation for the 'default' worker id (a distinct id already implies it)",
    )
    p.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="survey + print the picker's decision; act on nothing"
    )


def parse_only(values: list[str], flag: str = "--only") -> list[str]:
    """Flatten/validate a task flag (comma lists or repeats). Empty = the full cascade."""
    tasks: list[str] = []
    for v in values:
        for tok in v.replace(" ", "").split(","):
            if not tok:
                continue
            if tok not in ALLOWED_TASKS:
                raise SystemExit(
                    f"unknown {flag} task '{tok}' (valid: {', '.join(ALLOWED_TASKS)})"
                )  # exit 1; argparse uses 2
            tasks.append(tok)
    return tasks


def resolve_tasks(only_vals: list[str], skip_vals: list[str]) -> list[str]:
    """Fold --only and --skip into one effective allow-list. With no --skip the --only list passes
    through unchanged (empty ⇒ the whole cascade); --skip materializes the complement in stable
    ALLOWED_TASKS order, so everything downstream — the loop child's argv, want(), the displayed
    --only — needs to know about `only` alone. Combining them subtracts:
    `--only review,fix --skip fix` ⇒ ['review']."""
    only = parse_only(only_vals, "--only")
    skip = set(parse_only(skip_vals, "--skip"))
    if not skip:
        return only
    allowed = set(only) if only else set(ALLOWED_TASKS)
    tasks = [t for t in ALLOWED_TASKS if t in allowed and t not in skip]
    if not tasks:
        raise SystemExit("--only/--skip leave no work units enabled")
    return tasks


def resolve_pr_targets(values: list[str]) -> tuple[int, ...]:
    """Flatten/validate --pr (comma lists or repeated flags), falling back to $TAUCETI_PR. Empty =
    untargeted, which is every ordinary round.

    A leading `#` is accepted because that is how a PR number is written everywhere else — in the
    round's own log lines, on GitHub, and in the sentence the operator just read. Anything else is a
    hard error rather than a silently ignored token: a typo'd target must not read as "no targeting"
    and quietly turn a `--pr` round back into a free-running one.

    De-duplicated, in the order given. The round works down its own cascade rather than this list, so
    the order steers nothing; keeping it stable only makes the per-PR "why not" report read back the
    way it was typed."""
    env_used = not values and (os.environ.get("TAUCETI_PR") or "").strip()
    raw = values or ([os.environ["TAUCETI_PR"]] if env_used else [])
    where = "$TAUCETI_PR" if env_used else "--pr"
    out: list[int] = []
    for value in raw:
        for tok in value.replace(" ", "").split(","):
            if not tok:
                continue
            digits = tok.lstrip("#")
            if not digits.isdigit() or int(digits) <= 0:
                raise SystemExit(f"{where} value {tok!r} is not a pull request number")
            if int(digits) not in out:
                out.append(int(digits))
    return tuple(out)


def raise_on_untargetable_tasks(prs: tuple[int, ...], only: list[str]) -> None:
    """Refuse `--pr` alongside a task selection that leaves no PR-bearing work unit enabled.

    `--pr roadmap` cannot be honoured in any reading: the roadmap authors a PR that does not exist
    yet, and a progress round writes a roadmap's generated reports. Silently doing nothing would be
    the round's answer, one no-progress back-off at a time; saying so at the CLI costs one command."""
    if prs and only and not any(t in PR_TASKS for t in only):
        raise SystemExit(
            f"--pr targets pull requests, but --only/--skip leave only {', '.join(only)} enabled and "
            f"none of those act on an existing PR (PR work units: {', '.join(PR_TASKS)})"
        )


def resolve_review_throttle(cli_value: int | None, env: str, flag: str) -> int:
    """One of the undocumented review throttles, as an effective non-negative integer: the flag wins,
    else the environment variable, else 0 (off). A malformed value fails loudly rather than silently
    reading as 'off' — a throttle that quietly does nothing is worse than no throttle."""
    if cli_value is None:
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            return 0
        try:
            cli_value = int(raw)
        except ValueError:
            raise Die(f"${env} must be a non-negative integer (got {raw!r})") from None
    if cli_value < 0:
        raise Die(f"{flag} must be a non-negative integer (got {cli_value})")
    return cli_value


def resolve_agent(args) -> str:
    return getattr(args, "agent", None) or os.environ.get("TAUCETI_AGENT") or "auto"


def resolve_source(args, only: list[str]) -> str | None:
    """Validate and resolve --source after CLI roadmap overrides have reached the environment.

    A source is meaningful only when this invocation may author a roadmap PR and the target is pinned
    to one concrete roadmap area. Other phases may remain enabled: they simply ignore the source.
    Resolving it here also gives loop children a stable absolute path even if their cwd differs.
    """
    raw = getattr(args, "source", None)
    if raw is None:
        return None
    if only and "roadmap" not in only:
        raise SystemExit("--source requires the 'roadmap' phase to be enabled")
    area = roadmap_only()
    if area is None or area.strip().lower() in ("", "any", "auto"):
        raise SystemExit("--source requires a single roadmap area (use --roadmap-only AREA)")
    if not raw.strip():
        raise SystemExit("--source value must not be empty")
    if is_git_url(raw):
        return raw
    source = Path(raw).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"--source is neither a Git repository URL nor an existing Git repository directory: {source}")
    if not source.is_dir():
        raise SystemExit(f"--source local path must name a directory: {source}")
    if subprocess.run(["git", "-C", str(source), "rev-parse", "--git-dir"], capture_output=True, text=True).returncode:
        raise SystemExit(f"--source local directory is not a Git repository: {source}")
    return str(source)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tauceti",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'tauceti work -h' for work units and examples, or 'tauceti workers -h'\n"
        "for persistent workers. Guide and reference:\n"
        "  https://github.com/kim-em/TauCetiWorker",
    )
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser(
        "work",
        help="do work: one round, or the driver loop with --loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Do one unit of work and exit, or pass --loop to run the driver. A round picks the\n"
        "first applicable job from the cascade (or from --only), paces subscription agents\n"
        "against your quota, and runs eligible code and review agents on the host or in Bubble.\n"
        "Progress-report rounds always run on the host.",
        epilog=WORK_EPILOG,
    )
    add_work_flags(w)

    s = sub.add_parser("status", help="read-only survey of available work + quota")
    s.add_argument("--json", action="store_true", help="emit the survey as JSON")
    s.add_argument("--worker-id", dest="worker_id", default=None)

    u = sub.add_parser("usage", help="read Kiro/OpenRouter credits without sending a model prompt")
    u.add_argument(
        "--provider",
        action="append",
        choices=["kiro", "openrouter"],
        help="provider to query (repeatable; default: both)",
    )
    u.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    u.add_argument(
        "--kiro-burn-rate",
        default=os.environ.get("TAUCETI_KIRO_BURN_RATE"),
        metavar="CREDITS_PER_ROUND",
        help="report estimated Kiro rounds remaining at this credit burn (observability only)",
    )
    u.add_argument(
        "--openrouter-burn-rate",
        default=os.environ.get("TAUCETI_OPENROUTER_BURN_RATE"),
        metavar="USD_PER_ROUND",
        help="report estimated OpenRouter rounds remaining at this USD burn (observability only)",
    )
    u.add_argument("--timeout", type=float, default=30.0, help="per-provider query timeout in seconds")

    sub.add_parser("doctor", help="check the environment (tools, bubble, quota creds)")

    add_workers_parser(sub)

    # Hidden internal subcommands.
    r = sub.add_parser("_round", add_help=False)
    add_work_flags(r)
    hb = sub.add_parser("_heartbeat", add_help=False)
    hb.add_argument("key")
    hb.add_argument("--ppipe", type=int, default=None)
    ep = sub.add_parser("_egress-probe", add_help=False)
    ep.add_argument("--worker-id", dest="worker_id", default=None)
    mr = sub.add_parser("_managed-run", add_help=False)
    mr.add_argument("--spec", required=True)
    mr.add_argument("--state-dir", required=True)
    mr.add_argument("--runtime-dir", required=True)
    return p


def resolve_pace(cmd: str | None, args) -> None:
    """Settle the pacing curve for this process: validate the spec that will actually be used, and
    install a --pace override into $TAUCETI_PACE so the pacer (which reads it live) and any loop child
    see it.

    A malformed curve must fail loudly, not silently degrade to the default, which is not what the
    operator asked for. $TAUCETI_PACE is checked for EVERY subcommand as a configuration lint: most do
    not pace, but a stale export is a mistake worth naming wherever it is noticed, and a child spawned
    from here would inherit it.

    `work`/`_round` are the commands that REPLACE it. Where one carries --pace, that flag is the spec
    this run uses and the environment is overwritten below, so validating the environment as well would
    let a stale export veto the flag documented to override it. `workers add --pace` is not this: it
    records a curve for a worker launched later, leaves this environment alone, and is validated as
    configuration (see WorkerSpec.from_dict).

    Validation and installation live together, one step before dispatch, so no code can run against a
    curve that has been checked but not applied — or applied but not checked."""
    override = getattr(args, "pace", None) if cmd in ("work", "_round") else None
    spec, source = (override, "--pace") if override is not None else (os.environ.get("TAUCETI_PACE"), "$TAUCETI_PACE")
    if spec is not None:
        try:
            parse_pace_curve(spec)
        except ValueError as e:
            raise Die(f"{source}: {e}") from None
    if override is not None:
        os.environ["TAUCETI_PACE"] = override


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd

    resolve_pace(cmd, args)

    if cmd is None:
        return cmd_tui(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "usage":
        return cmd_usage(args)
    if cmd in ("work", "_round"):
        only = resolve_tasks(getattr(args, "only", []), getattr(args, "skip", []))
        prs = resolve_pr_targets(getattr(args, "pr", []))
        raise_on_untargetable_tasks(prs, only)
        agent = resolve_agent(args)
        # --account names a CODEX account, so the round must be committed to Codex before it starts.
        # Under `auto` the pacer may legitimately land on Claude, and there is no honest answer then:
        # silently ignoring the flag would spend on an account the operator did not authorise, and
        # failing at that point would look like TauCeti reneging on `auto`. Refuse up front instead.
        if getattr(args, "account", None) and agent != "codex":
            raise Die(
                f"--account is a Codex account check, so it needs an explicit --agent codex; got "
                f"--agent {agent}. "
                + (
                    "`auto` may pick Claude, which has no account to check against."
                    if agent == "auto"
                    else "TauCeti cannot check a Claude account: unlike Codex, Claude Code keeps no "
                    "account identity in the credential this worker mirrors."
                )
            )
        return cmd_work(args, only=only, prs=prs, agent=agent, one_round=(cmd == "_round"))
    if cmd == "doctor":
        return cmd_doctor(args)
    if cmd == "workers":
        return cmd_workers(args)
    if cmd == "_heartbeat":
        return cmd_heartbeat(args)
    if cmd == "_egress-probe":
        return cmd_egress_probe(args)
    if cmd == "_managed-run":
        return cmd_managed_runner(args)
    parser.print_help()
    return 64


def _usage_burn_rate(value: str | float | None, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise Die(f"{label} must be a non-negative finite number") from None
    if not math.isfinite(rate) or rate < 0:
        raise Die(f"{label} must be a non-negative finite number")
    return rate


def _fmt_usage_number(value) -> str:
    return "?" if value is None else f"{value:g}"


def cmd_usage(args) -> int:
    providers = getattr(args, "provider", None) or ["kiro", "openrouter"]
    timeout = getattr(args, "timeout", 30.0)
    if not math.isfinite(timeout) or timeout <= 0:
        raise Die("--timeout must be a positive finite number")
    snap = usage_snapshot(
        providers=providers,
        kiro_burn_rate=_usage_burn_rate(args.kiro_burn_rate, "--kiro-burn-rate"),
        openrouter_burn_rate=_usage_burn_rate(args.openrouter_burn_rate, "--openrouter-burn-rate"),
        timeout=timeout,
    )
    if getattr(args, "json", False):
        print(json.dumps(snap, indent=2, sort_keys=True))
        return 0 if all(v.get("ok") for v in snap.values()) else 1

    for provider in providers:
        value = snap[provider]
        if not value.get("ok"):
            print(f"{provider}: unavailable ({value.get('error')})")
            continue
        if provider == "kiro":
            print(
                f"kiro: {value.get('plan_name') or 'unknown plan'}"
                + (f"; resets {value['billing_cycle_reset']}" if value.get("billing_cycle_reset") else "")
            )
            for item in value.get("usage_breakdowns", []):
                line = (
                    f"  {item.get('displayName') or item.get('resourceType') or 'credits'}: "
                    f"{_fmt_usage_number(item.get('used'))}/{_fmt_usage_number(item.get('limit'))} "
                    f"{item.get('unit')} ({_fmt_usage_number(item.get('percentage'))}% used, "
                    f"{_fmt_usage_number(item.get('remaining'))} left)"
                )
                if item.get("estimated_rounds_remaining") is not None:
                    line += f"; ~{item['estimated_rounds_remaining']:.1f} rounds at configured burn"
                print(line)
        else:
            print("openrouter:")
            key = value.get("key")
            if key:
                line = f"  key {key.get('label') or '(unnamed)'}: {_fmt_usage_number(key.get('used'))} USD used" + (
                    f", {_fmt_usage_number(key.get('remaining'))} of "
                    f"{_fmt_usage_number(key.get('limit'))} USD key limit left"
                    if key.get("limit") is not None
                    else "; no key spending limit"
                )
                if key.get("estimated_rounds_remaining") is not None:
                    line += f"; ~{key['estimated_rounds_remaining']:.1f} rounds at configured burn"
                print(line)
            account = value.get("account")
            if account:
                line = (
                    f"  account credits: {_fmt_usage_number(account.get('used'))}/"
                    f"{_fmt_usage_number(account.get('purchased'))} USD used, "
                    f"{_fmt_usage_number(account.get('remaining'))} left"
                )
                if account.get("estimated_rounds_remaining") is not None:
                    line += f"; ~{account['estimated_rounds_remaining']:.1f} rounds at configured burn"
                print(line)
            elif value.get("account_unavailable_reason"):
                print(f"  account credits: not queried ({value['account_unavailable_reason']})")
    return 0 if all(v.get("ok") for v in snap.values()) else 1


def cmd_status(args) -> int:
    cfg = Config.resolve(getattr(args, "worker_id", None))
    gh = GitHub()
    rs = ReviewState(cfg, gh)
    counters = Counters(cfg)
    sv = survey(cfg, gh, rs, counters, deep=True)
    _, quota_snap = Quota(cfg).choose(None)

    if getattr(args, "json", False):
        out = {"survey": dataclasses.asdict(sv), "quota": {k: dataclasses.asdict(v) for k, v in quota_snap.items()}}
        print(json.dumps(out, indent=2, default=str))
        return 1 if sv.github_failed else 0

    from rich.console import Console

    render_survey(sv, Console(), quota_snap)
    return 1 if sv.github_failed else 0


def cmd_work(args, *, only: list[str], agent: str, one_round: bool, prs: tuple[int, ...] = ()) -> int:
    # --host used to opt OUT of the bubble sandbox; running on the host is now the default, so the flag
    # is a no-op we only warn about. --bubble is the way to opt back INTO the sandbox.
    if getattr(args, "host", False):
        warn_red(
            "--host is now the default (the agent runs directly on the host); it is a no-op. "
            "Pass --bubble to run inside the sandbox instead"
        )
    author_model = getattr(args, "author_model", None)
    author_effort = getattr(args, "author_effort", None)
    resolved_author_fallback_model = getattr(args, "resolved_author_fallback_model", None)
    if (author_model or author_effort) and agent == "auto":
        raise Die("--author-model/--author-effort require an explicit --agent; auto may choose another provider")
    if author_effort and agent in OPENROUTER_MODELS:
        raise Die(f"--author-effort is not supported for the OpenRouter provider {agent!r}")
    # --roadmap-only overrides the env for this run (and is inherited by loop children, which read it
    # live via roadmap_only()). Empty string is a meaningful value: "all areas".
    if getattr(args, "roadmap_only", None) is not None:
        os.environ["TAUCETI_ROADMAP_ONLY"] = args.roadmap_only
    source = resolve_source(args, only)
    if source is not None:
        args.source = source
    # --roadmap-skip likewise overrides the env and is inherited by loop children (read live via
    # roadmap_skip()).
    if getattr(args, "roadmap_skip", None) is not None:
        os.environ["TAUCETI_ROADMAP_SKIP"] = args.roadmap_skip
    # --roadmap-extra-identities and --ignore-claims override the env and are inherited by loop
    # children (read live via roadmap_extra_identities() / respect_claims()).
    if getattr(args, "roadmap_extra_identities", None) is not None:
        os.environ["TAUCETI_ROADMAP_EXTRA_IDENTITIES"] = args.roadmap_extra_identities
    if getattr(args, "respect_claims", None) is False:
        os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"
    # --auto-refresh likewise, so a loop child renews on the same authority the driver was given.
    if getattr(args, "auto_refresh", None):
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    # --pace is settled in resolve_pace, before dispatch, together with the environment it overrides.
    # --stream restores live agent output (default redirects it to a log file). Set in the env so loop
    # children inherit it.
    if getattr(args, "stream", False):
        os.environ["TAUCETI_STREAM"] = "1"
    # The undocumented review throttles. Resolve them here — before the loop branch below, which never
    # builds a RoundOpts of its own — and pin the effective values in the env, which is how each
    # `_round` child (and a managed worker's process tree) inherits them.
    review_min_queue = resolve_review_throttle(
        getattr(args, "review_min_queue", None), "TAUCETI_REVIEW_MIN_QUEUE", "--review-min-queue"
    )
    review_min_age = resolve_review_throttle(
        getattr(args, "review_min_age", None), "TAUCETI_REVIEW_MIN_AGE", "--review-min-age"
    )
    os.environ["TAUCETI_REVIEW_MIN_QUEUE"] = str(review_min_queue)
    os.environ["TAUCETI_REVIEW_MIN_AGE"] = str(review_min_age)
    # Resolve the worker id. An explicit --worker-id (or $TAUCETI_WORKER_ID, which loop children inherit)
    # pins it; with neither, auto-assign the lowest free slot (worker1, worker2, ...) so several
    # `work --loop` terminals on one host don't collide without the operator hand-numbering them. Pinning
    # the chosen id in the env makes Config.resolve and loop children agree on it.
    explicit = getattr(args, "worker_id", None) or os.environ.get("TAUCETI_WORKER_ID")
    is_loop_driver = getattr(args, "loop", False) and not one_round
    # The managed wrapper holds the write end of this inherited pipe. EOF means that wrapper was
    # hard-killed before it could forward SIGTERM; stop this loop so it cannot become an orphan.
    parent_pipe = os.environ.pop("TAUCETI_PARENT_PIPE_FD", None)
    if parent_pipe is not None and is_loop_driver:
        fd = int(parent_pipe)

        def stop_with_parent() -> None:
            try:
                while os.read(fd, 1):
                    pass
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=stop_with_parent, name="tauceti-parent-watch", daemon=True).start()
    if explicit:
        wid = sanitize_wid(explicit)
        # A top-level loop driver also reserves its slot, so an auto-assigned peer in another terminal
        # can't pick the same id (e.g. you restart `--worker-id worker2` while others auto-assign). The
        # _round children (which inherit this id) and one-shot rounds skip this; they arbitrate via the
        # per-round round.lock instead, so reserving here would only make a child collide with its parent.
        if is_loop_driver and not acquire_slot(wid):
            if os.environ.get("TAUCETI_MANAGED") == "1":
                raise Die(f"worker slot '{wid}' is already held by another loop; the manager will retry after it exits")
            warn_red(
                f"another loop already holds worker slot '{wid}'; they will contend on its state, "
                f"$HOME, and quota pacing — use a distinct --worker-id"
            )
    else:
        wid = auto_assign_wid()
        os.environ["TAUCETI_WORKER_ID"] = wid
        log(f"auto-assigned worker slot {wid} (override with --worker-id)")
    # A distinct worker id implies its own credential directories (so several workers' refreshes don't
    # race); --isolate-home forces it even for the 'default' id. This must run BEFORE Config.resolve so
    # cfg.home and the credential paths derived from it point at the per-worker copy. Off macOS that
    # means moving $HOME; on macOS $HOME deliberately stays put and $CLAUDE_CONFIG_DIR/$CODEX_HOME carry
    # the isolation instead (see isolate_home). Either way it is a no-op once already isolated, so loop
    # children that inherit the environment don't re-isolate.
    if wid != "default" or getattr(args, "isolate_home", False):
        isolate_home(wid)
    cfg = Config.resolve(wid)
    # Tee this process's log() output to a per-worker file so a `work --loop` started in a bare terminal
    # still leaves an on-disk record to monitor. The loop driver picks one session file and exports its
    # path; the _round children it spawns inherit TAUCETI_LOG_FILE and append to the SAME file.
    set_log_file(cfg.logdir)
    if getattr(args, "loop", False) and not one_round:
        # The loop driver never builds a RoundOpts, so the round-level check below is not on this path;
        # its children get theirs. Check here too, so a wrong --account costs one command rather than a
        # full survey, and so the operator sees the message before the loop's own output buries it.
        raise_on_account_mismatch(cfg, getattr(args, "account", None), agent, "account")
        return cmd_loop(args, cfg, only=only, agent=agent, prs=prs)
    dry = getattr(args, "dry_run", False)
    ignore_quota = getattr(args, "ignore_quota", False)
    quota_cmd = getattr(args, "quota_cmd", None)

    with RoundContext(cfg) as ctx:  # one round per worker; cleanup + SIGTERM/SIGINT handling
        # Lifecycle test hooks (no GitHub, no model, no mutation) — exercise lock / fd-leak / timeout.
        hb = os.environ.get("TAUCETI_TEST_HEARTBEAT")
        if hb:
            Claims(cfg, ctx).start_heartbeat("branch/test", os.environ.get("CLAIM_REPO") or TAUCETI)
            log(f"[test] heartbeat started; holding {hb}s then exiting")
            time.sleep(int(hb))
            return 0
        hold = os.environ.get("TAUCETI_TEST_HOLD")
        if hold:
            subprocess.Popen(["sleep", hold])  # grandchild; close_fds=True ⇒ must NOT inherit the lock fd
            log(f"[test] spawned 'sleep {hold}' grandchild, round exiting immediately")
            return 0
        slp = os.environ.get("TAUCETI_TEST_SLEEP")
        if slp:
            log(f"[test] holding the lock and sleeping {slp}s")
            time.sleep(int(slp))
            return 0

        # A `_round` child is spawned by a loop driver that forced a usage read moments ago, so it may
        # use that. A one-shot `tauceti work` has nothing recent behind it and must look for itself,
        # rather than refuse the round on a cached verdict that may be an hour old.
        work_model, pending_init = resolve_work_model(
            cfg, agent, dry=dry, ignore_quota=ignore_quota, quota_cmd=quota_cmd, fresh=not one_round
        )
        authoring_profile = None
        if work_model != "auto":
            authoring_profile = resolve_authoring_profile(
                work_model,
                cli_model=author_model,
                cli_effort=author_effort,
                resolved_fallback_model=resolved_author_fallback_model,
            )
        gh = GitHub()
        w = Worker(cfg, gh, ReviewState(cfg, gh), Counters(cfg), ctx, Claims(cfg, ctx))
        opts = RoundOpts(
            only=only,
            agent=agent,
            work_model=work_model,
            sandbox_host=not getattr(args, "bubble", False),
            dry_run=dry,
            source=source,
            # Either the pacer just selected Claude on an unopened window (one-shot), or the loop did
            # and passed the authorization down (--claude-bootstrap). Same launch-stage gate either way.
            claude_bootstrap=pending_init or getattr(args, "claude_bootstrap", False),
            authoring_profile=authoring_profile,
            account=getattr(args, "account", None),
            review_min_queue=review_min_queue,
            review_min_age=review_min_age,
            prs=prs,
        )
        # Before preflight, and NOT gated on --dry-run: --dry-run is how an operator checks their setup,
        # so it is the one run that most needs to answer "am I on the right account?". The check is a
        # file read, so it costs a dry run nothing.
        raise_on_account_mismatch(cfg, opts.account, opts.work_model, "account")
        if not dry:
            preflight(cfg, opts)
        return run_round(w, opts)  # NoProgress/Die propagate to main()'s handler


def cmd_egress_probe(args) -> int:
    """Open a review-posture bubble (same flags as review_in_bubble) and assert the network boundary:
    the TauCeti-scoped proxy is reachable but arbitrary egress is denied. This is the property that makes
    a tool-using reviewer safe — re-run it whenever the bubble image / security policy changes, and once
    review gains tool use. Prints PROXY_OK + EGRESS_BLOCKED on success; tests/egress.sh asserts both."""
    cfg = Config.resolve(getattr(args, "worker_id", None))
    # set +e so a non-zero curl (the blocked case) doesn't abort the script before we print the verdict.
    probe = (
        "sh -lc 'set +e; "
        "if gh auth token >/dev/null 2>&1 && gh api /repos/TauCetiProject/TauCeti >/dev/null 2>&1; "
        "then echo PROXY_OK; else echo PROXY_FAIL; fi; "
        "if curl -sS --max-time 10 -o /dev/null https://example.com 2>/dev/null; "
        "then echo EGRESS_LEAK; else echo EGRESS_BLOCKED; fi'"
    )
    with RoundContext(cfg) as ctx:
        gh = GitHub()
        w = Worker(cfg, gh, ReviewState(cfg, gh), Counters(cfg), ctx, Claims(cfg, ctx))
        opts = RoundOpts(only=["review"], agent="claude", work_model="claude", sandbox_host=False, dry_run=False)
        # No mounts; cred_model=claude just satisfies bubble's credential flags (the probe runs no model).
        return run_in_bubble(w, TAUCETI, "", opts, inner_cmd=probe, cred_model="claude")


def _have(tool: str) -> bool:
    import shutil

    return shutil.which(tool) is not None


def cmd_doctor(args) -> int:
    """Report what the environment can do. The review engine is fetched on demand; a stable Bubble
    install is required only for real ``--bubble`` rounds."""
    cfg = Config.resolve(getattr(args, "worker_id", None))
    rows: list[tuple[str, bool, str]] = []
    rows.append(("gh", _have("gh"), "required"))
    rows.append(("git", _have("git"), "required"))
    rows.append(("uv/uvx", _have("uvx"), "required (runs tauceti and fetches the review engine)"))
    rows.append(("jq", _have("jq"), "claim.sh needs it"))
    gh_auth = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    rows.append(("gh auth", gh_auth, "the worker acts as this account; its PRs are the ones it tends"))
    # Which claim namespace this account gets, and therefore how far its de-duplication reaches. The
    # row is not a failure: the fork always works, it just does not coordinate beyond your own fleet.
    # Deliberately does not resolve the fork, so `doctor` never creates one as a side effect.
    if gh_auth:
        override = os.environ.get("CLAIM_REPO", "").strip()
        if override:
            note = f"$CLAIM_REPO={override} (operator override; every worker pointed here coordinates)"
        elif shared_claims_granted():
            note = f"{CLAIMS} — shared, so you de-duplicate against every operator"
        else:
            note = f"your fork — de-duplicates within your fleet; {CLAIMS} opens on your first merged PR"
        rows.append(("claims", True, note))
    rows.append(("bubble", _have("bubble"), "stable install required for real --bubble rounds"))
    rows.append(("incus", _have("incus"), "bubble's container runtime — only needed for --bubble"))
    rows.append(("lake", _have("lake"), "host authoring (the default) builds with it"))
    rows.append(("pi", _have("pi"), "for --agent deepseek/minimax"))
    rows.append(("kiro-cli", _have("kiro-cli"), "for --agent kiro"))
    rows.append(("tmux", _have("tmux"), "optional `tauceti workers tmux` log workspace"))
    codex_creds = codex_dir(cfg.home) / "auth.json"
    rows.append(("codex creds", _safe_exists(codex_creds), str(codex_creds)))
    # Which Codex account those credentials spend under. `codex login status` will not tell you (it
    # prints "Logged in using ChatGPT" and no email) and the model cannot introspect its own account,
    # so for anyone juggling several ChatGPT accounts this row is the only way to see where the money
    # is going. It is also where --account's expected value is read off.
    acct = Quota(cfg).codex_account()
    if acct is not None:
        note = acct.describe()
        if acct.conflict:
            note += " — its identity claims disagree; the credential may be half-written"
        elif acct.email and acct.account_id:
            note += f", id {acct.account_id}"
        rows.append(("codex account", acct.describes_an_account and not acct.conflict, note))
    claude_creds = claude_dir(cfg.home) / ".credentials.json"
    if _safe_exists(claude_creds) or sys.platform != "darwin":
        rows.append(("claude creds", _safe_exists(claude_creds), str(claude_creds)))
    else:
        # macOS keeps Claude creds in the login Keychain, not a file; the pacer reads them there.
        rows.append(
            ("claude creds", _claude_keychain_creds() is not None, 'macOS login Keychain ("Claude Code-credentials")')
        )
    kiro_db = kiro_data_dir(cfg.home) / "data.sqlite3"
    rows.append(
        (
            "kiro auth",
            bool((os.environ.get("KIRO_API_KEY") or "").strip()) or _safe_exists(kiro_db),
            "$KIRO_API_KEY" if (os.environ.get("KIRO_API_KEY") or "").strip() else str(kiro_db),
        )
    )
    bad = 0
    print(f"tauceti doctor — worker '{cfg.wid}'")
    for name, ok, note in rows:
        mark = "ok " if ok else "MISSING"
        if not ok and name in ("gh", "git", "uv/uvx", "gh auth"):
            bad += 1
        print(f"  [{mark:7}] {name:14} {note}")
    return 1 if bad else 0


def preflight(cfg: Config, opts: RoundOpts) -> None:
    """Fail a round early if its chosen mode can't run, rather than partway through. bubble and the
    review engine are fetched on demand, so we don't require them on PATH (only uvx + a runtime)."""
    for t in ("gh", "git", "uvx"):
        if not _have(t):
            raise Die(f"preflight: missing '{t}' on PATH")
    # Only authoring/fixing stages build with lake on the host; review runs the fetched-on-demand review
    # engine and never compiles (same reason it's excluded from the fork preflight below). Excluding it
    # keeps a host-default `--only review` worker from being falsely blocked on a machine with no toolchain.
    needs_host_build = any((not _bubble(s, opts)) for s in WORK_TASKS if want(opts.only, s) and s != "review")
    if needs_host_build and not _have("lake") and not opts.dry_run:
        raise Die(
            "preflight: host authoring (the default) needs an elan/lake toolchain on PATH "
            "(or pass --bubble to build inside the sandbox instead)"
        )
    # bubble (the --bubble sandbox) runs each model on untrusted PR content inside an Incus container.
    # Without Incus, bubble fails deep in the round with a terse "Incus is required but not installed";
    # catch it here with a pointer to the two ways out.
    uses_bubble = any(_bubble(s, opts) for s in WORK_TASKS if want(opts.only, s))
    # Rounds that may push to the contributor's fork (everything except review, which only reads/comments).
    # The fork-PR preflight below is scoped to these so a `--only review` worker is never blocked by it.
    uses_fork = any(_bubble(s, opts) for s in WORK_TASKS if want(opts.only, s) and s != "review")
    if uses_bubble and not opts.dry_run and not _have("incus"):
        raise Die(
            "preflight: the bubble sandbox needs a working Incus runtime, but `incus` is not on PATH.\n"
            "  bubble runs each model on untrusted PR content inside an Incus container. Either:\n"
            "    - install Incus (https://linuxcontainers.org/incus/), then re-run; or\n"
            "    - drop --bubble to run on this host directly (the default; the agent then\n"
            "      has your full gh credentials and network, so use it only on trusted/disposable machines).\n"
            "  `tauceti doctor` reports this too."
        )
    if uses_bubble and not opts.dry_run and bubble_cmd_is_disposable():
        raise Die(
            "preflight: --bubble needs Bubble installed at a stable path because its auth proxy is a "
            "host-global service; uvx/`uv tool run` environments are disposable. Install it with\n"
            "    uv tool install git+https://github.com/kim-em/bubble.git\n"
            "  or set $TAUCETI_BUBBLE to a stable Bubble executable, then re-run."
        )
    if uses_bubble and not opts.dry_run:
        bubble_version = installed_bubble_version()
        minimum = KIRO_BUBBLE_MIN_VERSION if opts.work_model == "kiro" else BUBBLE_MIN_VERSION
        if not bubble_version_meets_minimum(bubble_version, minimum):
            found = bubble_version or "unreadable"
            raise Die(
                f"preflight: --bubble needs Bubble {minimum} or newer; found {found}. "
                "Older versions do not fail closed when a requested Lake cache service cannot be "
                "configured. Update the stable install with\n"
                f"    uv tool install --force {BUBBLE_REPO}\n"
                "  or point $TAUCETI_BUBBLE at an updated stable executable, then re-run."
            )
    # The worker authors/fixes from the contributor's fork, handing bubble `--allow-push <fork>` for the
    # fork's git access (kim-em/bubble#320). An older cached bubble rejects that flag only AFTER the model
    # launches — a wasted round — so verify support up front (probes `bubble open --help` once).
    if uses_fork and not opts.dry_run and not bubble_supports_allow_push():
        raise Die(
            "preflight: this bubble is too old for fork-PR authoring — it has no `--allow-push` "
            "(needs kim-em/bubble#320). Install or update Bubble from kim-em/bubble, then re-run "
            "(override the executable with $TAUCETI_BUBBLE)."
        )
    # Work-agent bubble rounds warm both Mathlib's cache and TauCeti's public Lake artifact cache before
    # launching the model. Require Bubble's host-global download proxy rather than exposing the cache's
    # public R2 domain directly to the container.
    if uses_fork and not opts.dry_run and not bubble_supports_lake_cache_service():
        raise Die(
            "preflight: this bubble is too old for TauCeti's Lake artifact cache — it has no "
            "`--lake-cache-service`. Install or update Bubble from kim-em/bubble, then re-run "
            "(override the executable with $TAUCETI_BUBBLE)."
        )
    # The CLI may advertise --allow-push while an older live daemon keeps rejecting fork pushes (403).
    # Require a reachable endpoint that advertises the capability, and refresh it safely when needed.
    # Fork-pushing rounds only (a stale daemon must not block review).
    if uses_fork and not opts.dry_run:
        ensure_fork_proxy_current()


def cli_main() -> int:
    """Console-script entry point (also used by ./tauceti). Maps the worker's exceptions to exit codes."""
    # A service has no login shell to provide NIX_SSL_CERT_FILE, and uv's standalone CPython may not
    # know the host's CA-bundle path. Repair the process environment before quota telemetry and child
    # launches; this also gives other TLS-using subprocesses the same host trust store.
    if ensure_ssl_cert_file() is None:
        log("no system CA bundle found; set SSL_CERT_FILE if TLS verification fails")
    _ensure_scripts_executable()
    try:
        return main() or 0
    except Die as e:
        log(str(e))
        report_failure(str(e), code=1)
        return 1
    except NoProgress as e:
        log(str(e))
        report_failure(str(e), code=EX_NOPROGRESS)
        return EX_NOPROGRESS
    except WorkersError as e:
        print(f"tauceti workers: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _ensure_scripts_executable() -> None:
    """A wheel install drops the execute bit on the bundled scripts/ wrappers; restore it so the agents
    can run git-safe-push / gh-safe-pr-create / claim.sh on PATH. Cheap and idempotent."""
    for f in ("claim.sh", "git-safe-push", "gh-safe-pr-create"):
        p = HERE / "scripts" / f
        try:
            if p.exists() and not os.access(p, os.X_OK):
                p.chmod(0o755)
        except OSError:
            pass
