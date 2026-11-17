"""Parallel isolated inspection of consecutive audit batches with in-order advancement.

The pool is the only cursor writer while it holds `pool.lock`; ordinary Worker
audit rounds defer to it. Each batch is the exact `plan_batch` input that a
sequential run would build from that batch's `from_sha`. Every batch is
inspected by its own isolated reviewer in its own attempt directory. The cursor
moves only through `advance`, one batch at a time, and only past batches with a
validated pass. A finding, an incomplete verdict or any non-operational failure
is binding: it is never re-inspected, and nothing beyond it can advance.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import security_audit as sa
from .audit_alerts import notify_block, save
from .config import Die, NoProgress, log

# Only these failures, with no recorded judgment, may be retried with a fresh
# attempt. Everything else (unauthorized tools, malformed or inconsistent
# judgments, retained-history conflicts) blocks for human review.
OPERATIONAL = (
    re.compile(r"security-audit: (codex|claude) failed; see .*"),
    re.compile(r"security-audit: inspection unfinished; retained for same-session continuation"),
    re.compile(r"security-audit: reader has not served the complete input"),
    re.compile(r"security-audit: reviewer did not read the complete input"),
    re.compile(r"security-audit: incomplete model response"),
    re.compile(r"security-audit: missing final reviewer result"),
    re.compile(r"security-audit: reviewer failed"),
    re.compile(r"security-audit: model failed"),
    re.compile(r"security-audit: awaiting client delivery verification"),
)
MAX_RETRIES = 2
RETRY_SECONDS = 60
ADMIN_TTL = 300
POLL_SECONDS = 300


@dataclass
class Slot:
    batch: dict
    status: str = "planned"  # planned, running, pass, blocked
    failures: int = 0
    retry_at: float = 0.0
    verdict: dict | None = None
    failure: dict | None = None
    attempts: list[str] = field(default_factory=list)
    # A batch keeps its reviewer for life; retries never switch provider.
    provider: str | None = None
    credential: str | None = None
    resume: dict | None = None  # Codex: the stopped attempt to continue in the same session.
    # Inherited from the Worker's sequential audit, which already ran it without soft pacing.
    inherited: bool = False

    @property
    def digest(self) -> str:
        return self.batch["input_digest"]

    @property
    def target(self) -> str:
        return self.batch["commits"][-1]["sha"]


def operational(exc: BaseException, attempt: Path, provider: str) -> bool:
    if isinstance(exc, (subprocess.TimeoutExpired, OSError)):
        clean = True
    elif isinstance(exc, (Die, NoProgress)):
        clean = any(p.fullmatch(str(exc)) for p in OPERATIONAL)
    else:
        return False
    return clean and clean_history(attempt)


def clean_history(attempt: Path) -> bool:
    if not attempt.is_dir() or not sa.native_rollouts(attempt):
        return True
    # A fresh attempt must never discard a judgment already in the native history.
    from .audit_recovery import native_finals

    try:
        finals = native_finals(attempt)
    except (Die, OSError, ValueError, TypeError, AttributeError):
        return False
    return all(
        isinstance(f, dict) and f.get("findings") == [] and f.get("verdict") in ("unfinished", "pass") for f in finals
    )


class Pool:
    def __init__(self, state: Path, *, parallel: int, chooser, min_commits: int = 1):
        if parallel < 1:
            raise Die("security-audit: pool parallelism must be positive")
        self.work = state
        self.repo = state / "objects.git"
        self.store = state / "pool"
        self.parallel = parallel
        self.chooser = chooser  # Picks (provider, credential) for new batches and retries, or None to wait.
        self.min_commits = min_commits
        self.slots: list[Slot] = []
        self.running: dict[concurrent.futures.Future, tuple[Slot, Path]] = {}
        self.admin_at = 0.0
        self.api_wait_until = 0.0
        self.actor: str | None = None
        self.protocols: dict[str, int] = {}
        self.started = time.time()
        self.advanced_commits = 0
        self.planning_blocked: str | None = None
        self.inherited: tuple[dict, dict] | None = None  # (block, retained batch) of a Worker Codex attempt.
        self.fetched: tuple[str, str] | None = None

    # Persistence -----------------------------------------------------------
    def persist(self, slot: Slot) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / f"{slot.digest}.input.json").write_text(sa.serialize(slot.batch))
        save(
            self.store / f"{slot.digest}.json",
            {
                "input_digest": slot.digest,
                "from_sha": slot.batch["from_sha"],
                "to_sha": slot.target,
                "status": "running" if slot.status == "running" else slot.status,
                "failures": slot.failures,
                "verdict": slot.verdict,
                "failure": slot.failure,
                "attempts": slot.attempts,
                "provider": slot.provider,
                "model": sa.reviewer_model(slot.provider) if slot.provider else None,
                "credential": slot.credential,
            },
        )

    def restored(self, cursor: str, main: str) -> list[Slot]:
        """Reuse persisted passes only for byte-identical inputs that are still the next main prefix."""
        if not self.store.is_dir():
            return []
        by_from = {}
        for path in self.store.glob("*.input.json"):
            try:
                batch = json.loads(path.read_text())
                meta = json.loads((self.store / path.name.replace(".input.json", ".json")).read_text())
            except (OSError, ValueError):
                continue
            by_from.setdefault(batch["from_sha"], []).append((batch, meta))
        chain, frm = [], cursor
        pending = sa.validate_history(self.repo, cursor, main)
        offset = 0
        while frm in by_from and len(chain) < self.parallel:
            candidates = [(b, m) for b, m in by_from[frm] if m.get("status") in ("pass", "blocked")]
            if len(candidates) != 1:
                break
            batch, meta = candidates[0]
            shas = [c["sha"] for c in batch["commits"]]
            if pending[offset : offset + len(shas)] != shas:
                break
            rebuilt = {
                "from_sha": batch["from_sha"],
                "main_sha": batch["main_sha"],
                "commits": [sa.commit_input(self.repo, sha) for sha in shas],
            }
            rebuilt["input_digest"] = sa.hashlib.sha256(sa.serialize(rebuilt).encode()).hexdigest()
            if rebuilt != batch or sa.serialize(rebuilt) != sa.serialize(batch):
                break
            slot = Slot(
                batch,
                status=meta["status"],
                failures=meta.get("failures", 0),
                verdict=meta.get("verdict"),
                failure=meta.get("failure"),
                provider=meta.get("provider"),
                credential=meta.get("credential"),
            )
            if slot.status == "pass" and (not slot.verdict or slot.verdict.get("verdict") != "pass"):
                break
            if slot.verdict is not None:
                sa.validate_verdict(slot.verdict, batch)
            chain.append(slot)
            offset += len(shas)
            frm = shas[-1]
            if slot.status == "blocked":
                break
        return chain

    # GitHub ----------------------------------------------------------------
    def github(self, action):
        """Run an authenticated step, deferring it until a rate-limit reset."""
        if time.time() < self.api_wait_until:
            return None
        try:
            return action()
        except NoProgress as exc:
            if "rate limit" not in str(exc).lower():
                raise
            reset = time.time() + 600
            try:
                reset = float(sa.api("rate_limit")["resources"]["core"]["reset"]) + 5
            except (NoProgress, KeyError, TypeError, ValueError):
                pass
            self.api_wait_until = min(reset, time.time() + 3600)
            log(
                f"security-audit pool: GitHub rate limit; deferring authenticated steps for {self.api_wait_until - time.time():.0f}s"
            )
            return None

    def admin(self) -> bool:
        if time.time() - self.admin_at < ADMIN_TTL:
            return True

        def check():
            login = getattr(sa, "admin_login", None)
            if login is not None:
                self.actor = login()  # Checks ADMIN and returns the viewer in one request.
            else:
                sa.require_admin()
                if self.actor is None:
                    self.actor = sa.api("user")["login"]
            return True

        if self.github(check):
            self.admin_at = time.time()
            return True
        return False

    # Window ----------------------------------------------------------------
    def sync(self, cursor: str, main: str) -> None:
        """Align the window with the remote cursor; discard it if the chain no longer applies."""
        index = next((i for i, s in enumerate(self.slots) if s.batch["from_sha"] == cursor), None)
        if index is None and self.slots:
            log(f"security-audit pool: cursor {cursor[:12]} is outside the planned window; replanning")
            self.slots = []
        elif index:
            self.slots = self.slots[index:]
        if not self.slots:
            self.slots = self.restored(cursor, main)
            if self.slots:
                log(f"security-audit pool: reusing {len(self.slots)} persisted batch result(s)")
        if self.slots:
            pending = sa.validate_history(self.repo, cursor, main)
            shas = [c["sha"] for s in self.slots for c in s.batch["commits"]]
            if pending[: len(shas)] != shas:
                log("security-audit pool: planned window is no longer the next main prefix; replanning")
                self.slots = []

    def refill(self, cursor: str, main: str) -> None:
        if any(s.status == "blocked" for s in self.slots):
            return  # Nothing beyond a binding block is inspected.
        frm = self.slots[-1].target if self.slots else cursor
        if frm == self.planning_blocked:
            return
        if not self.slots and len(sa.validate_history(self.repo, cursor, main)) < self.min_commits:
            return
        if not self.slots and self.inherited is not None:
            previous, batch = self.inherited
            self.inherited = None
            commits = [c["sha"] for c in batch["commits"]]
            if batch["from_sha"] == cursor and sa.validate_history(self.repo, cursor, main)[: len(commits)] == commits:
                slot = Slot(batch, provider="codex", credential="subscription", resume=previous, inherited=True)
                self.slots.append(slot)
                frm = slot.target
                log(f"security-audit pool: continuing the Worker's Codex inspection {cursor[:12]}..{frm[:12]}")
        while len(self.slots) < self.parallel:
            try:
                batch = sa.plan_batch(self.repo, frm, main)
            except NoProgress:
                return  # Caught up with main.
            except (Die, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                self.planning_blocked = frm
                self.block_planning(frm, exc, head=frm == cursor)
                return
            slot = Slot(batch)
            self.slots.append(slot)
            frm = slot.target

    def block_planning(self, frm: str, exc: BaseException, *, head: bool) -> None:
        failure = {
            "from_sha": frm,
            "reason": str(exc),
            "manual_inspection_required": True,
            "input_policy": "compacting-session-v1",
        }
        sa.record(self.work / "ledger.jsonl", failure)
        if head:
            (self.work / "blocked.json").write_text(sa.serialize(failure))
        notify_block(self.work, failure)
        log(f"security-audit pool: cannot plan a batch from {frm[:12]}: {exc}")

    # Inspection ------------------------------------------------------------
    def protocol(self, provider: str) -> int:
        if provider not in self.protocols:
            from .audit_clients import selected_protocol

            self.protocols[provider] = selected_protocol(provider, self.work)
        return self.protocols[provider]

    def submit(self, executor) -> None:
        busy = len(self.running)
        now = time.time()
        for slot in self.slots:
            if busy >= self.parallel:
                return
            if slot.status != "planned" or slot.retry_at > now:
                continue
            choice = self.chooser.retry(slot) if slot.provider else self.chooser.choose()
            if choice is None:
                if not slot.provider:
                    return  # No reviewer under pace and no API key: wait.
                continue  # This batch's provider is unavailable; never switch it.
            if slot.provider and choice[0] != slot.provider:
                raise Die("security-audit: a retry may not change a batch's reviewer")
            slot.provider, slot.credential = choice
            attempt = self.work / f"attempt-{time.time_ns()}"
            attempt.mkdir()
            (attempt / "input.json").write_text(sa.serialize(slot.batch))
            slot.status = "running"
            slot.attempts.append(str(attempt))
            self.persist(slot)
            commits = len(slot.batch["commits"])
            resume = f" continuing {Path(slot.resume['attempt']).name}" if slot.resume else ""
            log(
                f"security-audit pool: inspect {slot.batch['from_sha'][:12]}..{slot.target[:12]} ({commits} commits)"
                f" with {slot.provider}/{sa.reviewer_model(slot.provider)} ({slot.credential}){resume} in {attempt.name}"
            )
            future = executor.submit(
                self.inspect,
                slot.batch,
                attempt,
                slot.provider,
                slot.credential,
                self.protocol(slot.provider),
                slot.resume,
            )
            self.running[future] = (slot, attempt)
            busy += 1

    def inspect(self, batch, attempt, provider, credential, protocol, resume) -> dict:
        if not any(c["files"] for c in batch["commits"]):
            return {
                "input_digest": batch["input_digest"],
                "commits": [c["sha"] for c in batch["commits"]],
                "verdict": "pass",
                "findings": [],
            }
        if provider == "codex" and resume is not None:
            continued = self.continue_codex(batch, attempt, protocol, resume)
            if continued is not None:
                return continued
        if provider == "claude":
            return sa.inspect(batch, provider, attempt, protocol=protocol, claude_credential=credential)
        return sa.inspect(batch, provider, attempt, protocol=protocol)

    def continue_codex(self, batch, attempt, protocol, resume) -> dict | None:
        """Continue a stopped Codex attempt in its own native session, as ordinary recovery does."""
        from .audit_progress import continuable
        from .audit_recovery import retained

        try:
            original, retained_batch, execution, legacy = retained(self.work, resume)
        except (Die, OSError, ValueError, KeyError, TypeError):
            return None  # Nothing launched or nothing valid to continue: start a fresh session.
        if retained_batch != batch:
            raise Die("security-audit: retained pool attempt belongs to another batch")
        transcript = original / "model-transcript.jsonl"
        allowed = execution.get("_allowed_history", ())
        progress = continuable(
            transcript.read_text() if transcript.exists() else "",
            batch,
            original / "read-receipt.json",
            original,
            legacy=legacy,
            allowed=allowed,
            paged=execution["paged"],
            protocol=protocol,
        )
        return sa.inspect(
            batch,
            "codex",
            attempt,
            resume_from=original,
            legacy_history=legacy,
            allowed_history=allowed,
            continue_reading=progress,
            protocol=protocol,
        )

    def finished(self, future, cursor: str) -> None:
        slot, attempt = self.running.pop(future)
        span = f"{slot.batch['from_sha'][:12]}..{slot.target[:12]}"
        try:
            verdict = future.result()
        except BaseException as exc:  # noqa: BLE001 - classified below; never silently dropped.
            failure = {
                "from_sha": slot.batch["from_sha"],
                "to_sha": slot.target,
                "reason": str(exc) or type(exc).__name__,
                "attempt": str(attempt),
                **self.reviewer(slot),
            }
            if isinstance(exc, subprocess.TimeoutExpired):
                failure["failure_kind"] = "provider-timeout"
            sa.record(self.work / "ledger.jsonl", failure)
            if operational(exc, attempt, slot.provider) and slot.failures < MAX_RETRIES:
                slot.failures += 1
                slot.status = "planned"
                # Codex continues the same native session; Claude retries in a fresh one.
                slot.resume = failure if slot.provider == "codex" else None
                slot.retry_at = time.time() + RETRY_SECONDS * 2 ** (slot.failures - 1)
                self.persist(slot)
                log(
                    f"security-audit pool: {span} operational failure on {slot.provider} ({exc}); "
                    f"retry {slot.failures}/{MAX_RETRIES}"
                )
                return
            if operational(exc, attempt, slot.provider):
                failure["pool_retries_exhausted"] = True
            slot.status = "blocked"
            self.block(slot, failure, head=slot.batch["from_sha"] == cursor)
            log(f"security-audit pool: {span} blocked: {exc}")
            return
        sa.record(
            self.work / "ledger.jsonl",
            sa.review_record(
                self.actor or "",
                slot.provider,
                self.protocol(slot.provider),
                slot.batch,
                attempt,
                verdict,
                slot.credential,
            ),
        )
        slot.verdict = verdict
        if verdict["verdict"] == "pass":
            slot.status = "pass"
            self.persist(slot)
            log(f"security-audit pool: {span} pass")
            return
        slot.status = "blocked"
        failure = {
            "from_sha": slot.batch["from_sha"],
            "to_sha": slot.target,
            "reason": "security-audit: " + "; ".join(verdict["findings"]),
            "attempt": str(attempt),
            **self.reviewer(slot),
        }
        sa.record(self.work / "ledger.jsonl", failure)
        self.block(slot, failure, head=slot.batch["from_sha"] == cursor)
        log(f"security-audit pool: {span} {verdict['verdict']}; human review required")

    @staticmethod
    def reviewer(slot: Slot) -> dict:
        return {"provider": slot.provider, "model": sa.reviewer_model(slot.provider), "credential": slot.credential}

    def block(self, slot: Slot, failure: dict, *, head: bool) -> None:
        slot.failure = failure
        self.persist(slot)
        if head:
            (self.work / "blocked.json").write_text(sa.serialize(failure))
        notify_block(self.work, failure)

    # Advancement -----------------------------------------------------------
    def advance(self, cursor: str) -> str:
        landed = None
        while self.slots and self.slots[0].status == "pass" and self.slots[0].batch["from_sha"] == cursor:
            slot = self.slots[0]
            if not self.admin():
                break
            result = self.github(lambda slot=slot: sa.advance(self.repo, slot.batch))
            if result is None:
                break
            sa.record(self.work / "ledger.jsonl", {"actor": self.actor, "advanced": result, "from_sha": cursor})
            self.slots.pop(0)
            (self.store / f"{slot.digest}.json").unlink(missing_ok=True)
            (self.store / f"{slot.digest}.input.json").unlink(missing_ok=True)
            commits = len(slot.batch["commits"])
            self.advanced_commits += commits
            rate = self.advanced_commits / max((time.time() - self.started) / 3600, 1e-6)
            log(
                f"security-audit pool: advanced {cursor[:12]}..{result[:12]} (+{commits}); {self.advanced_commits} commits at {rate:.0f}/h"
            )
            cursor = landed = result
        if landed is not None:
            sa.dispatch_advanced(self.work, landed)
        if self.slots and self.slots[0].status == "blocked" and self.slots[0].batch["from_sha"] == cursor:
            failure = self.slots[0].failure
            if failure and not (self.work / "blocked.json").exists():
                (self.work / "blocked.json").write_text(sa.serialize(failure))
        return cursor

    def supersede(self, previous: dict) -> bool:
        """Archive an operational-only block so a fresh isolated attempt can inspect the batch.

        Findings, incomplete verdicts, planning blocks, exhausted pool retries and any
        attempt whose native history holds a judgment remain binding."""
        reason = previous.get("reason", "")
        if (
            previous.get("manual_inspection_required")
            or previous.get("pool_retries_exhausted")
            or not (
                any(p.fullmatch(reason) for p in OPERATIONAL)
                or reason == "security-audit: operational recovery interrupted"
                or reason.startswith("security-audit: GitHub request failed:")
            )
        ):
            return False
        span = (previous.get("from_sha"), previous.get("to_sha"))
        ledger = self.work / "ledger.jsonl"
        for line in ledger.read_text().splitlines() if ledger.exists() else ():
            row = json.loads(line)
            if (row.get("from_sha"), row.get("to_sha")) == span and row.get("verdict", {}).get("verdict") not in (
                None,
                "pass",
            ):
                return False
        for key in ("attempt", "continuation_of"):
            if previous.get(key) and not clean_history(Path(previous[key])):
                return False
        try:
            from .audit_recovery import retained

            _, batch, execution, _ = retained(self.work, previous)
            if execution.get("argv", [""])[0] == "codex":
                # A batch keeps its reviewer for life: continue this Codex session in the pool.
                self.inherited = (previous, batch)
        except (Die, OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            self.inherited = None  # Nothing valid to continue; a fresh inspection is allowed.
        archived = self.work / f"superseded-by-pool-block-{time.time_ns()}.json"
        (self.work / "blocked.json").rename(archived)
        sa.record(ledger, {"superseded_operational_block": previous, "archived": str(archived)})
        log(
            f"security-audit pool: superseded operational block {span[0][:12]}..{(span[1] or '')[:12]}; fresh isolated inspection"
        )
        return True

    # Loop ------------------------------------------------------------------
    def step(self, executor) -> bool:
        """One control pass. Returns False when a binding block at the cursor stops the pool."""
        cursor, main = sa.remote_sha(sa.CURSOR), sa.remote_sha(os.environ.get("TAUCETI_BASE_BRANCH", "main"))
        if (cursor, main) != self.fetched:
            sa.refresh(self.repo, cursor, main)
            self.fetched = (cursor, main)
        blocked = self.work / "blocked.json"
        if blocked.exists():
            previous = json.loads(blocked.read_text())
            if previous.get("from_sha") == cursor and not self.supersede(previous):
                if not self.running and (not self.slots or self.slots[0].status != "pass"):
                    log(f"security-audit pool: unresolved block at the cursor; read {blocked}")
                    return False
        self.sync(cursor, main)
        cursor = self.advance(cursor)
        self.refill(cursor, main)
        if self.slots:
            pending = len(sa.validate_history(self.repo, cursor, main))
            states = ",".join(s.status[0] for s in self.slots)
            log(
                f"security-audit pool: cursor {cursor[:12]}, {pending} pending, window [{states}], {len(self.running)} running"
            )
        if self.admin():
            self.submit(executor)
        return True

    def serve(self, *, stop=None) -> None:
        """Run until a binding block holds the cursor, or `stop(pool)` returns true."""
        self.work.mkdir(parents=True, exist_ok=True)
        with (self.work / "pool.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Die("security-audit: another pool already owns this audit state") from None
            log(f"security-audit pool: up to {self.parallel} isolated reviewers; {self.chooser.describe()}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel) as executor:
                while True:
                    try:
                        if not self.step(executor) and not self.running:
                            return
                    except NoProgress as exc:
                        log(f"security-audit pool: {exc}; retrying")
                    if stop is not None and stop(self):
                        return
                    timeout = 30 if self.slots else POLL_SECONDS
                    if not self.running:
                        time.sleep(timeout)
                        continue
                    done, _ = concurrent.futures.wait(
                        list(self.running), timeout=timeout, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    if done:
                        cursor = sa.remote_sha(sa.CURSOR)
                        for future in done:
                            self.finished(future, cursor)


class Chooser:
    """Pick each new batch's reviewer: Codex Luna under pace, then subscription Haiku under pace,
    then Haiku billed to the configured API key regardless of pace. None means wait.

    Pacing uses the ordinary `resolve_work_model` verdict. `--ignore-quota` overrides soft
    pacing for subscription reviewers, never hard limits. `pin` restricts the run to one provider."""

    TTL = 300

    def __init__(self, cfg, *, pin: str | None = None, ignore_quota: bool = False):
        if pin not in (None, "codex", "claude"):
            raise Die("security-audit: pool reviewer must be codex or claude")
        self.cfg = cfg
        self.pin = pin
        self.ignore_quota = ignore_quota
        self.checked: dict[str, tuple[float, bool]] = {}

    def describe(self) -> str:
        order = {
            None: "codex, then subscription haiku, then API-key haiku",
            "codex": "codex only",
            "claude": "subscription haiku, then API-key haiku",
        }[self.pin]
        return f"reviewer order: {order}" + (" (--ignore-quota)" if self.ignore_quota else "")

    def under_pace(self, provider: str) -> bool:
        at, ok = self.checked.get(provider, (0.0, False))
        if time.time() - at < self.TTL:
            return ok
        from .loop import resolve_work_model

        try:
            resolve_work_model(self.cfg, provider, dry=False, ignore_quota=self.ignore_quota, fresh=True)
            ok = True
        except (NoProgress, SystemExit) as exc:
            log(f"security-audit pool: {provider} subscription unavailable for new launches: {exc}")
            ok = False
        self.checked[provider] = (time.time(), ok)
        return ok

    @staticmethod
    def api_key() -> bool:
        try:
            return bool(getattr(sa, "anthropic_api_key", lambda: None)())
        except Die as exc:
            log(f"security-audit pool: API-key Haiku unavailable: {exc}")
            return False

    def claude(self, prefer: str | None = None):
        if prefer == "api-key" and self.api_key():
            return ("claude", "api-key")
        if self.under_pace("claude"):
            return ("claude", "subscription")
        if self.api_key():
            return ("claude", "api-key")
        return None

    def choose(self):
        if self.pin != "claude" and self.under_pace("codex"):
            return ("codex", "subscription")
        if self.pin == "codex":
            return None
        return self.claude()

    def retry(self, slot: Slot):
        """Same provider for life; Claude may move from subscription to the API key."""
        if slot.provider == "codex":
            return ("codex", "subscription") if slot.inherited or self.under_pace("codex") else None
        return self.claude(prefer=slot.credential)
