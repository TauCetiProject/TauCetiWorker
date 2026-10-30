"""tauceti_worker.loop — the driver loop: pace against quota, run one round as a child under a hard
timeout, then settle (short pause if productive, escalating back-off otherwise)."""

from __future__ import annotations

import contextlib
import math
import os
import signal
import subprocess
import sys
import time

from .agents import resolve_authoring_profile
from .budget import ACCOUNTING_ERRORS, Budget, BudgetError, admission_pause
from .claude_api import api_mode
from .config import Config, NoProgress, log
from .constants import (
    BACKOFF_BASE,
    BACKOFF_MAX,
    EX_ADMISSION_WAIT,
    EX_CLAIM_COLLISION,
    EX_DOCUMENTATION_WAIT,
    EX_GITHUB_WAIT,
    EX_NOPROGRESS,
    EX_RECOVERY_IDLE,
    GH_MIN_BUDGET,
    INTERROUND,
    OPENROUTER_MODELS,
    POLL,
)
from .fleet_capacity import admission_reason as fleet_admission_reason
from .github import github_budget
from .github_state import enabled as github_state_enabled
from .quota import Provider, Quota, _glyph, _hours, _pace_reason, _unavail_reason, quota_line
from .round import run_round_subprocess
from .runtime_status import report_runtime, runtime_snapshot


class _LoopTerminated(KeyboardInterrupt):
    """SIGTERM translated to the same teardown path as Ctrl-C, with the right exit code."""


def _recheck_in(prov: Provider | None, now: float) -> int:
    """How long until it is worth reading this provider's usage again, while waiting for quota.

    Wake at a known telemetry deadline, rounded UP so an early wake cannot turn a
    remaining fraction of a second into another full poll interval. Long waits are
    revisited hourly without bypassing the fetcher's persisted cooldown."""
    if prov is None:
        return POLL
    if prov.telemetry_unavailable and prov.next_eligible:
        # An observed exhaustion may reset before the telemetry endpoint permits a
        # retry. Re-evaluate it then; --ignore-quota can resume without another fetch.
        deadline = math.ceil(prov.next_eligible - now)
        if prov.retry_after and math.isfinite(prov.retry_after) and prov.retry_after > 0:
            deadline = min(deadline, math.ceil(prov.retry_after))
        return max(1, min(deadline, 3600))
    if prov.retry_after and math.isfinite(prov.retry_after) and prov.retry_after > 0:
        return max(1, min(math.ceil(prov.retry_after), 3600))
    if prov.next_eligible:
        return max(POLL, min(int(prov.next_eligible - now) + 5, 3600))
    return POLL


def _wait_quota_line(snap: dict, *, markup: bool = True) -> str:
    """Render the immediate pacing bottleneck when it defers an otherwise-initializable idle window.

    The provider remains HARD-blocked for launch-control purposes until the window is initialized; this
    is display-only. But when the only hard state is a plain post-reset idle window and a sibling window
    is pacing-blocked, the sibling is what must clear first. Show that condition first instead of hiding
    it behind the latent idle state.

    `markup=False` for a plain destination; see quota_line.
    """
    line = quota_line(snap, markup=markup)
    prov = snap.get("claude")
    if prov is None or prov.error or prov.available:
        return line

    windows = prov.windows or []
    idle = [w for w in windows if w.status == "idle"]
    paced = [w for w in windows if w.status in ("at-budget", "over-pace")]
    other_hard = [w for w in windows if w.status not in ("under-pace", "at-budget", "over-pace", "idle")]
    if not idle or not paced or other_hard or any(w.detail != "window reset; awaiting initialization" for w in idle):
        return line

    why = "; ".join(
        [
            *(_pace_reason(w) for w in paced),
            *(f"{w.name} window reset — initialization deferred until pacing permits" for w in idle),
        ]
    )
    old = quota_line({"claude": prov}, markup=markup)
    new = f"claude {_glyph('~', 'yellow', markup)} ({why})"
    return line.replace(old, new, 1)


def cmd_loop(args, cfg: Config, *, only: list[str], agent: str, prs: tuple[int, ...] = ()) -> int:
    """The driver: pace against quota (codex preferred), run ONE round as a child under a hard timeout,
    then settle (short pause if productive, escalating back-off otherwise). Ctrl-C stops the current
    round and exits. Keeps the escalating back-off that stopped ~700 no-op rounds hammering a
    rate-limited GitHub."""
    unpaced = agent in OPENROUTER_MODELS or agent == "kiro" or api_mode()
    ignore_quota = getattr(args, "ignore_quota", False)
    bubble = getattr(args, "bubble", False)
    quota_cmd = getattr(args, "quota_cmd", None)
    targeted = f" pr={','.join(str(n) for n in prs)}" if prs else ""
    log(
        f"loop start: worker={cfg.wid} only={','.join(only) or '(all)'}{targeted} "
        f"agent={agent}{' [bubble]' if bubble else ''}"
    )
    report_runtime("idle", detail="loop started", phase=None, target=None, next_action_at=None)
    streak = 0
    admission_streak = 0
    recovery_probe_at = 0.0
    collision_streak = 0
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame) -> None:
        # run_round_subprocess catches KeyboardInterrupt and tears down the round's process
        # group before re-raising, so use that same proven cleanup path for Compose SIGTERM.
        raise _LoopTerminated

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            from .config import rotate_console_log

            rotate_console_log()
            from .verification_jobs import reap_continuations, resume_pending

            reap_continuations(cfg)
            if reason := fleet_admission_reason(cfg.wid):
                report_runtime("waiting-capacity", detail=reason, next_action_at=time.time() + 5)
                time.sleep(5)
                continue
            # Verification owns an isolated candidate and a separate job lease. Its execution
            # must not block model selection, and host CI reservations still precede launch.
            resume_pending(cfg, background=True)
            from .repair_recovery import due

            recovery_only = (
                set(only) != {"security-audit"}
                and time.time() >= recovery_probe_at
                and due(cfg, only=only, prs=prs, sandbox_host=not bubble, agent=agent)
            )
            if not recovery_only and api_mode() and os.environ.get("TAUCETI_USE_BUDGET") == "1":
                snapshot = Budget().runtime_snapshot(worker=cfg.wid)
                reason = admission_pause(snapshot, worker=cfg.wid) if snapshot else None
                blocked = reason is not None
                if blocked:
                    with contextlib.suppress(*ACCOUNTING_ERRORS):
                        Budget().refresh_waiter(cfg.wid)
                if blocked:
                    wake = snapshot.get("funding_at")
                    if reason == "API throughput cooldown":
                        waiter = next((item for item in snapshot.get("waiting", []) if item["worker"] == cfg.wid), {})
                        wake = snapshot.get("cooldowns", {}).get(waiter.get("model"))
                    limit = 60 if reason in ("waiting for funding", "API throughput cooldown") else 5
                    nap = max(1, min(limit, (wake - time.time() + 1) if wake else limit))
                    report_runtime("waiting-budget", detail=reason, budget=snapshot, next_action_at=time.time() + nap)
                    time.sleep(nap)
                    continue
            report_runtime(
                "checking-quota",
                detail="checking provider availability",
                phase=None,
                target=None,
                failure_reason=None,
                failure_code=None,
                failure_log=None,
            )
            # 1) Decide the model and whether to run this cycle. `pending_init` means Claude was picked
            # while a window of it is reset-but-unopened: the round is authorized to spend ONE small
            # request to open it, but only once it has found work (see work_units.dispatch).
            pending_init = False
            if recovery_only:
                model = agent  # The child attempts recovery before any model selection.
            elif unpaced:
                model = agent  # explicit unpaced provider; no subscription quota wait
            elif ignore_quota and not quota_cmd:
                # An explicit pacing override may run through a transient telemetry outage.
                # Known exhaustion, auth failures and malformed readings still block. Actual
                # model refusals stop the round and use the existing no-progress backoff.
                if agent == "auto":
                    raise SystemExit("--ignore-quota --loop needs an explicit --agent (codex/claude)")
                _chosen, snap = choose_model(cfg, agent, quota_cmd, refresh=True, renew=True)
                prov = snap.get(agent)
                verdict = _ignore_quota_verdict(_chosen, prov)
                # An unopened window is the one hard block --ignore-quota may still clear, because the
                # clearing is a bounded, pace-respecting request rather than an override of a real limit.
                if verdict == "wait" and agent == "claude" and claude_pending_init(snap):
                    verdict, pending_init = "run", True
                if verdict == "wait":
                    why = prov.error if (prov and prov.error) else (_unavail_reason(prov)[1] if prov else "unavailable")
                    nap = _recheck_in(prov, time.time())
                    # A loop DOES wait this out, so the wording stays — but a rejected credential is not
                    # something waiting fixes, and an unattended loop can poll on one indefinitely, so
                    # name the command that ends it here too.
                    log(
                        f"quota: {agent} hard-blocked ({why}) — --ignore-quota still waits out a hard "
                        f"block; sleeping {nap}s{_credential_hint(agent, prov)}"
                    )
                    report_runtime("waiting-quota", detail=why, next_action_at=time.time() + nap)
                    time.sleep(nap)
                    continue
                if verdict == "over-pace":
                    log(f"quota: {agent} over-pace; --ignore-quota set — running anyway")
                elif verdict == "telemetry-unavailable":
                    log(f"quota: {agent} usage telemetry temporarily unavailable; --ignore-quota set — attempting work")
                model = agent
            else:
                model, snap = choose_model(cfg, agent, quota_cmd, refresh=True, renew=True)
                if model is None and claude_pending_init(snap):
                    model, pending_init = "claude", True
                if model is None:
                    # Wake for whichever provider can change first. An idle/unknown alternative may
                    # become usable at the next poll, so another provider's distant reset or long
                    # Retry-After must not hide that. Waking early for one provider cannot re-trip
                    # another's rate limit: the pacer will not ask an endpoint that is still cooling
                    # down after a 429 (Quota._cooldown_left), it reuses the last answer instead. A
                    # pace-blocked window reports when the budget overtakes its usage, which is usually
                    # well before its reset, so a long wait sleeps to the line, not past it.
                    now = time.time()
                    nap = min((_recheck_in(p, now) for p in snap.values()), default=POLL)
                    # Neither destination renders Rich markup: log() writes to stderr and a file, and a
                    # runtime-status detail is read back as data.
                    waiting = _wait_quota_line(snap, markup=False)
                    log(f"quota: {waiting} — sleeping {nap}s")
                    report_runtime("waiting-quota", detail=waiting, next_action_at=time.time() + nap)
                    time.sleep(nap)
                    continue

            # Mirrored discovery owns a separate read quota. Only a recorded
            # exhaustion of direct REST should pause a mirrored worker: fresh
            # dispatch reads and publication still need that credential. Unknown
            # quota and a low direct GraphQL pool must not block the mirror.
            mirrored = github_state_enabled()
            if mirrored:
                from .github_transport import observed_budget, scope

                # gh's credential store has no transport identity to persist
                # observations under. Retain its existing preflight probe;
                # a failed probe remains unknown and never prevents launch.
                gb = observed_budget() if scope() else github_budget()
            else:
                gb = github_budget()
            buckets = ("core",) if mirrored or set(only) == {"overlap-scan"} else ("core", "graphql")
            minimum = 1 if mirrored else GH_MIN_BUDGET
            low = {k: v for k, v in (gb or {}).items() if k in buckets and v[0] < minimum}
            if low:
                reset = max(v[1] for v in low.values())
                nap = max(POLL, min(reset - int(time.time()) + 5, 3600))
                detail = ", ".join(f"{k}={gb[k][0]}" for k in low)
                log(
                    f"github: REST budget low ({detail} remaining < {minimum}) — "
                    f"waiting {nap}s for the reset before launching a round"
                )
                report_runtime("waiting-github", detail=detail, next_action_at=time.time() + nap)
                time.sleep(nap)
                continue

            # 2) Run ONE round as a child in its own process group, under the hard timeout.
            tail = ["--worker-id", cfg.wid]
            if overlap_batch := os.environ.get("TAUCETI_OVERLAP_BATCH_COMMITS"):
                tail += ["--overlap-batch-commits", overlap_batch]
            if api_mode():
                tail += ["--claude-billing", "api"]
                if os.environ.get("TAUCETI_USE_BUDGET") == "1":
                    tail.append("--budget")
                if keyfile := os.environ.get("TAUCETI_ANTHROPIC_KEY_FILE"):
                    tail += ["--anthropic-api-key-file", keyfile]
            if only:
                tail += ["--only", ",".join(only)]
            # --pr must travel to the child for the same reason --account does: the child is what
            # surveys and picks, this argv is built explicitly rather than inherited, and a targeting
            # flag omitted here would turn every round of a `--loop --pr` into a free-running one.
            if prs:
                tail += ["--pr", ",".join(str(n) for n in prs)]
            tail += ["--recovery-agent", agent]
            if not (getattr(args, "author_model", None) or getattr(args, "author_effort", None)):
                tail.append("--recovery-unpinned-profile")
            if recovery_only:
                tail += ["--recovery-only", "--agent", agent]
            elif model:
                tail += ["--agent", model, "--loop-paced"]  # soft pacing already decided by the parent
                if ignore_quota:
                    tail.append("--ignore-quota")  # only the operator grants outage permission
            if recovery_only:
                if getattr(args, "author_model", None):
                    tail += ["--author-model", args.author_model]
                if getattr(args, "author_effort", None):
                    tail += ["--author-effort", args.author_effort]
                if ignore_quota:
                    tail.append("--ignore-quota")
            if pending_init:
                # Authorization travels with the round, not with the pacer: the child asks for it only
                # after it has surveyed and picked a work unit, so a round that finds nothing to do never
                # spends the request. The GitHub preflight above has already passed at this point.
                tail.append("--claude-bootstrap")
            # Audit-capable workers use a fixed Luna/Haiku inspection profile and reject
            # authoring overrides. Their maintenance fallback resolves its normal defaults.
            if model and not recovery_only and "security-audit" not in only:
                profile = resolve_authoring_profile(
                    model,
                    cli_model=getattr(args, "author_model", None),
                    cli_effort=getattr(args, "author_effort", None),
                )
                # Pin the exact parent-resolved profile into the isolated child. The
                # child must not re-read a different HOME or upstream CLI default. Preserve fallback
                # provenance separately: --author-model alone looks like an operator pin to the child.
                tail += ["--author-model", profile.model]
                if profile.effort:
                    tail += ["--author-effort", profile.effort]
                if profile.fallback_model:
                    tail += ["--resolved-author-fallback-model", profile.fallback_model]
            # --account must travel to the child: the child is what actually spends, and this argv is
            # built explicitly rather than inherited, so a flag omitted here is a check that silently
            # does not happen for the entire loop.
            account = getattr(args, "account", None)
            if account:
                tail += ["--account", account]
            github_account = getattr(args, "github_account", None)
            if github_account:
                tail += ["--github-account", github_account]
            if bubble:
                tail.append("--bubble")
            source = getattr(args, "source", None)
            if source is not None:
                tail += ["--source", source]
            report_runtime(
                "surveying",
                detail="selecting the next work unit",
                next_action_at=None,
                wait_dependency=None,
                wait_reason=None,
                wait_detail=None,
                wait_cached=None,
            )
            rc = run_round_subprocess(tail)
            if recovery_only:
                recovery_probe_at = time.time() + 300  # At most one quota-independent probe per five minutes.
            if rc == EX_RECOVERY_IDLE:
                # Busy claims and other unavailable recovery records already have local retry
                # scheduling. They must not delay unrelated reviews, fixes or roadmap work.
                streak = 0
                report_runtime("idle", detail="recovery deferred; selecting other work", next_action_at=None)
                continue
            if rc == EX_CLAIM_COLLISION:
                streak = 0
                collision_streak += 1
                # First collision reselects immediately. Repeated all-busy passes get only
                # a tiny capped pause to avoid hammering shared claims or GitHub.
                nap = min(30, 5 * (collision_streak - 1))
                report_runtime(
                    "idle", detail="peer claims busy; selecting other work", next_action_at=time.time() + nap
                )
                if nap:
                    time.sleep(nap)
                continue
            collision_streak = 0
            if rc == EX_DOCUMENTATION_WAIT:
                streak = 0
                paused = runtime_snapshot()
                report_runtime(
                    paused.get("state") or "waiting-documentation",
                    detail=paused.get("detail") or "progress preparation deferred; selecting other work",
                    next_action_at=paused.get("next_action_at"),
                )
                time.sleep(5)
                continue
            if rc == EX_ADMISSION_WAIT:
                paused = runtime_snapshot()
                reason = fleet_admission_reason(cfg.wid)
                waiting = paused.get("state") in ("waiting-capacity", "waiting-budget", "waiting-storage")
                state = "waiting-capacity" if reason else (paused["state"] if waiting else "waiting-budget")
                admission_streak = admission_streak + 1 if state == "waiting-budget" else 0
                nap = 60 if state == "waiting-storage" else min(60, 5 * (1 << min(max(0, admission_streak - 1), 4)))
                with contextlib.suppress(BudgetError, OSError):
                    if api_mode():
                        Budget().refresh_waiter(cfg.wid)
                report_runtime(
                    state,
                    detail=reason or (paused.get("detail") if waiting else None) or "rechecking local API admission",
                    next_action_at=time.time() + nap,
                    failure_reason=None,
                    failure_code=None,
                    failure_log=None,
                )
                time.sleep(nap)
                continue
            admission_streak = 0

            if rc == EX_GITHUB_WAIT:
                streak = 0
                paused = runtime_snapshot()
                retry_at = paused.get("next_action_at")
                now = time.time()
                nap = (
                    max(1, retry_at - now + 1) if isinstance(retry_at, (int, float)) and math.isfinite(retry_at) else 5
                )
                reason = paused.get("detail") or "GitHub publication recovery pending"
                log(f"github: {reason} — rechecking after {nap:.1f}s")
                report_runtime(
                    "waiting-github",
                    detail=reason,
                    next_action_at=now + nap,
                    failure_reason=None,
                    failure_code=None,
                    failure_log=None,
                )
                time.sleep(nap)
                continue

            # 3) Settle: productive → short pause; no-progress/timeout/error → escalating back-off.
            if rc == 0:
                streak = 0
                report_runtime(
                    "idle", detail="round completed", phase=None, target=None, next_action_at=time.time() + INTERROUND
                )
                time.sleep(INTERROUND)
            else:
                streak += 1
                nap = min(BACKOFF_BASE * (1 << min(streak, 5)), BACKOFF_MAX)
                tag = "timed out" if rc in (124, 137) else ("no progress" if rc == EX_NOPROGRESS else f"rc={rc}")
                failed = runtime_snapshot()
                published = failed.get("failure_reason")
                reason = (
                    str(published)
                    if published
                    else ("round timed out" if rc in (124, 137) else f"round exited with status {rc}")
                )
                log(f"round {tag}; no-progress streak={streak} — backing off {nap}s")
                report_runtime(
                    "backoff",
                    detail=reason,
                    phase=failed.get("phase"),
                    target=failed.get("target"),
                    next_action_at=time.time() + nap,
                )
                time.sleep(nap)
    except _LoopTerminated:
        log("loop terminated — stopping")
        return 143
    except KeyboardInterrupt:
        log("loop interrupted — stopping")
        return 130
    finally:
        from .verification_jobs import stop_continuations

        stop_continuations(cfg)
        report_runtime("stopping", detail="loop stopping", phase=None, target=None, next_action_at=None)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _ignore_quota_verdict(chosen: str | None, prov: Provider | None) -> str:
    """What --ignore-quota does with the pacer snapshot for its pinned agent — applied both by the loop
    between rounds and by the round deciding what it will launch.

    --ignore-quota overrides pacing and transient telemetry outages:
      "run"       — the provider is available (under pace), launch as usual.
      "over-pace" — a SOFT block: real quota remains, we are only ahead of the burn line. This is the
                    block --ignore-quota exists to override, so launch anyway.
      "telemetry-unavailable" — explicitly classified transient failure; try useful work.
      "wait"      — exhaustion, auth/configuration failures, or an unreadable quota response.
    """
    if chosen is not None:
        return "run"
    if prov is not None and prov.telemetry_unavailable and not any(w.status == "exhausted" for w in prov.windows):
        return "telemetry-unavailable"
    soft = prov is not None and _unavail_reason(prov)[0]
    return "over-pace" if soft else "wait"


def choose_model(
    cfg: Config, agent: str, quota_cmd: str | None, *, refresh: bool = False, renew: bool = False
) -> tuple[str | None, dict]:
    """Decide which model to run now. With --quota-cmd / TAUCETI_QUOTA_CMD set, consult that external
    command instead of the built-in pacer (the escape hatch for e.g. a multi-account scheme): run
    `<quota_cmd> <agent>`; its first stdout token is the model to run
    (codex/claude/kiro/deepseek/minimax)
    or empty = none available. Otherwise use the self-contained pacer.

    This is a pure READ: choosing a model never spends quota. In particular an `auto` selection that
    inspects Claude and then picks codex makes no Claude request. `renew` is separate from that promise
    and off by default — it lets a caller that is about to run something rotate an expiring Claude
    access token, which spends no quota but does consume the operator's single-use refresh token."""
    if quota_cmd:
        import shlex

        r = subprocess.run(shlex.split(quota_cmd) + [agent], capture_output=True, text=True)
        out = (r.stdout or "").split()
        model = out[0] if (r.returncode == 0 and out) else None
        return (model or None), {"quota-cmd": Provider("quota-cmd", bool(model), model)}
    return Quota(cfg).choose(None if agent == "auto" else agent, refresh=refresh, renew=renew)


def _credential_hint(agent: str, prov: Provider | None) -> str:
    """What to do about a provider that refused the credential, or "" when that is not what happened.

    A 401 is not a quota condition and waiting does not fix it: something has to renew the token. The
    worker will not do that behind the operator's back — refresh tokens are single-use, so rotating one
    can log out an interactive session sharing it — which leaves two doors, and this says so rather than
    reporting a wait that would never end."""
    error = (prov.error if prov else None) or ""
    # Match what THIS pacer writes, not any text mentioning a token: each provider phrases its own
    # refusal (see Quota.codex / Quota._claude_pass), and a transport error that happens to carry `401`
    # or "token expired" from something in between is not our credential being rejected.
    if "usage HTTP 401" not in error and "token expired; refresh left to the operator" not in error:
        return ""
    if agent != "claude":
        return ". Run `codex login` to renew the credential"
    if sys.platform == "darwin":
        # The Keychain is the store here and the worker never writes it, so --auto-refresh does nothing
        # and offering it would send the operator after a flag that cannot help.
        return ". Run `claude` to renew the Keychain credential"
    return (
        ". Run `claude` to renew the credential, or --auto-refresh to let an unattended worker rotate it"
        " (see docs/quota.md)"
    )


def _retry_hint(prov: Provider | None) -> str:
    """When it is worth coming back, for a round that is about to exit rather than wait: the endpoint's
    own Retry-After if it gave one, else the moment the blocking window frees. Empty when the snapshot
    says nothing about timing — better silent than invented."""
    after = prov.retry_after if prov else None
    if not after and prov and prov.next_eligible:
        after = prov.next_eligible - time.time()
    if not after or after <= 0:
        return ""
    if after < 60:
        return f"; retry in ~{max(1, round(after))}s"
    return f"; retry in ~{round(after / 60)}m" if after < 90 * 60 else f"; retry in ~{_hours(after)}"


def claude_pending_init(snap: dict) -> bool:
    """True when Claude is unavailable ONLY because a window has reset and nothing has opened the next
    cycle yet, and initializing it is within the operator's pace curve (Quota decides both, purely).

    Such a provider is not "out of quota" — it is unopened, and only a Claude request can open it. It
    may therefore be selected PROVISIONALLY: nothing is spent by selecting it, and the round makes the
    one bootstrap request at its launch stage, after it has found actual work to do."""
    prov = snap.get("claude")
    return bool(prov and prov.bootstrap_eligible)


def resolve_work_model(
    cfg: Config,
    agent: str,
    *,
    dry: bool,
    ignore_quota: bool,
    quota_cmd: str | None = None,
    fresh: bool = False,
    already_paced: bool = False,
) -> tuple[str, bool]:
    """Turn the --agent dial into (concrete model, needs-launch-stage-bootstrap). 'auto' consults the
    pacer (or --quota-cmd); codex preferred, opus fallback. Kiro and OpenRouter agents are explicit
    and unpaced. Dry-run symbolic. The bootstrap flag never launches anything by itself — it says the round
    must ask for launch authorization once it has a work unit in hand.

    `fresh` forces the usage read rather than accepting a cached one. A cached reading is only evidence
    about the moment it was taken (see Quota._claude_pass), so where nothing has just refreshed it — a
    one-shot `tauceti work` — this is the difference between deciding on current telemetry and refusing
    on an hour-old verdict. A `_round` child leaves it off: the loop driver refreshed seconds ago, and
    re-fetching would only ask the same question twice.

    --ignore-quota still reads usage and respects known hard blocks. A transient
    telemetry outage alone does not prevent an explicitly unpaced attempt.
    `already_paced` carries only the parent's soft pacing decision, never outage permission."""
    if dry:
        return agent, False
    if agent in OPENROUTER_MODELS or agent == "kiro":
        return agent, False
    if ignore_quota and not quota_cmd and agent == "auto":
        raise SystemExit(
            "--ignore-quota needs an explicit paced --agent (codex/claude); 'auto' can't choose without the pacer"
        )
    # The round is deciding what it will actually launch, so the token it hands the agent must be live.
    chosen, snap = choose_model(cfg, agent, quota_cmd, refresh=fresh, renew=True)
    if (ignore_quota or already_paced) and not quota_cmd:
        # Match the loop's decision, including telemetry-only failures.
        verdict = _ignore_quota_verdict(chosen, snap.get(agent))
        if verdict == "telemetry-unavailable" and not ignore_quota:
            verdict = "wait"
        if verdict == "wait" and agent == "claude" and claude_pending_init(snap):
            return "claude", True  # the one hard block a bounded, pace-respecting request may clear
        if verdict == "wait":
            prov = snap.get(agent)
            why = prov.error if (prov and prov.error) else (_unavail_reason(prov)[1] if prov else "unavailable")
            # This round exits rather than sleeping — a loop parent is what waits — so say when to come
            # back rather than implying the round will hold the line itself. An expired credential is
            # the case an operator can fix in one command, and this used to be that command: the round
            # skipped the read, launched, and the agent CLI renewed on its way up. It no longer does, so
            # the message has to hand the recovery back.
            fix = _credential_hint(agent, prov)
            override = "--ignore-quota" if ignore_quota else "loop pacing handoff"
            raise NoProgress(
                f"{agent} hard-blocked ({why}) — {override} overrides pacing, not availability; "
                f"not launching this round{fix or _retry_hint(prov)}"
            )
        if verdict == "over-pace":
            why = "--ignore-quota set" if ignore_quota else "loop already paced this round"
            log(f"quota: {agent} over-pace; {why} — running anyway")
        elif verdict == "telemetry-unavailable":
            log(f"quota: {agent} usage telemetry temporarily unavailable; --ignore-quota set — attempting work")
        return agent, False
    if chosen is None and claude_pending_init(snap):
        return "claude", True
    if chosen is None:
        raise NoProgress(f"no model under pace right now (agent={agent}) — nothing to run this round")
    return chosen, False
