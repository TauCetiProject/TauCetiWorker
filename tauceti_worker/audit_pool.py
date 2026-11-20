"""Parallel isolated inspection of consecutive audit batches with in-order advancement.

The pool is the only cursor writer while it holds `pool.lock`; ordinary Worker
audit rounds defer to it. Each batch is the exact `plan_batch` input that a
sequential run would build from that batch's `from_sha`. Every batch is
inspected by its own isolated reviewer in its own attempt directory. The cursor
moves only through `advance`, one batch at a time, and only past batches with a
validated pass. Each attempt's outcome is the shared evidence classification: a
recorded concern or unreadable evidence is binding (it is never re-inspected, and
nothing beyond it can advance), and only an attempt with no judgment is retried.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audit_evidence as evidence
from . import audit_watchdog
from . import security_audit as sa
from .audit_alerts import notify_block, save
from .audit_evidence import NotJudged, Unreadable, concern, holds
from .config import Die, NoProgress, log

MAX_RETRIES = 2
RETRY_SECONDS = 60
ADMIN_TTL = 300
POLL_SECONDS = 300
LOOKAHEAD = 8
RANGES = "ranges.json"


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
    resume: dict | None = None  # The stopped attempt to continue in the same session.
    # Inherited from the Worker's sequential audit, which already ran it without soft pacing.
    inherited: bool = False

    @property
    def digest(self) -> str:
        return self.batch["input_digest"]

    @property
    def target(self) -> str:
        return self.batch["commits"][-1]["sha"]

    def record(self) -> dict:
        """The durable form: a range is its exact commits; its input is rebuilt from them."""
        return {
            "from_sha": self.batch["from_sha"],
            "main_sha": self.batch["main_sha"],
            "commits": [c["sha"] for c in self.batch["commits"]],
            "input_digest": self.digest,
            "status": self.status,
            "failures": self.failures,
            "retry_at": self.retry_at,
            "verdict": self.verdict,
            "failure": self.failure,
            "attempts": self.attempts,
            "provider": self.provider,
            "model": sa.reviewer_model(self.provider) if self.provider else None,
            "credential": self.credential,
        }


def operational(exc: BaseException) -> bool:
    """An attempt with no judgment, or a failure before any reviewer ran. Never a recorded concern."""
    return isinstance(exc, (NotJudged, NoProgress, OSError, subprocess.TimeoutExpired))


class Pool:
    def __init__(self, state: Path, *, parallel: int, chooser, min_commits: int = 1):
        if parallel < 1:
            raise Die("security-audit: pool parallelism must be positive")
        self.work = state
        self.repo = state / "objects.git"
        self.store = state / "pool"
        self.parallel = parallel
        # Plan ahead of the running reviewers, so a binding block awaiting human
        # review does not idle the pool: inspection continues, advancement waits.
        self.window = LOOKAHEAD * parallel
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
        # (block, retained batch, provider, credential) of a stopped Worker attempt to continue.
        self.inherited: tuple[dict, dict, str, str] | None = None
        self.fetched: tuple[str, str] | None = None
        self.holding: str | None = None  # Cursor of a block awaiting human review, logged once.
        # The latest (cursor, pending) a pass observed, or the error that prevented it: the watchdog's input.
        self.observation: tuple[str, int] | BaseException = NoProgress("security-audit pool: not yet observed")

    # Persistence -----------------------------------------------------------
    def persist(self) -> None:
        """Atomically replace the ordered list of active ranges: the pool's whole scheduling state.

        Every change (planning, launch, result, retry, advancement) rewrites the list in one
        step, so a restart never loses a range's status or reverts a replacement of ranges."""
        self.store.mkdir(parents=True, exist_ok=True)
        save(self.store / RANGES, {"ranges": [slot.record() for slot in self.slots]})

    def restored(self) -> list[Slot]:
        """The persisted ranges, inputs rebuilt from their exact commits; `aligned` places them.

        A pass whose rebuilt input differs is inspected again; a block stays binding. A range
        left running by a stopped pool keeps that status and is settled from its attempt's own
        evidence (`settle_interrupted`) before anything else is admitted."""
        path = self.store / RANGES
        if not path.exists():
            return []
        slots = []
        try:
            for record in json.loads(path.read_text())["ranges"]:
                batch = sa.build_batch(self.repo, record["from_sha"], record["main_sha"], record["commits"])
                status = record["status"]
                if status == "pass" and batch["input_digest"] != record["input_digest"]:
                    status = "planned"
                slot = Slot(
                    batch,
                    status=status,
                    failures=record["failures"],
                    retry_at=record["retry_at"],
                    verdict=record["verdict"] if status != "planned" else None,
                    failure=record["failure"],
                    attempts=list(record["attempts"]),
                    provider=record["provider"],
                    credential=record["credential"],
                )
                if slot.status == "pass":
                    sa.validate_verdict(slot.verdict, batch)
                    if slot.verdict["verdict"] != "pass":
                        raise ValueError("a passed range records a non-passing verdict")
                if slot.status == "running" and not slot.attempts:
                    slot.status = "planned"  # Persisted before any attempt existed.
                slots.append(slot)
        except (OSError, ValueError, KeyError, TypeError, IndexError, Die) as exc:
            # The durable record of binding ranges cannot be trusted or replaced: hold, and
            # let the cursor watchdog report the stall, rather than replan over a block.
            raise NoProgress(f"security-audit pool: {path} cannot be restored; preserve it for review: {exc}") from exc
        return slots

    def aligned(self, slots: list[Slot], cursor: str, main: str) -> list[Slot]:
        """The ranges that are still the next consecutive main prefix from the cursor.

        Ranges the cursor has passed are dropped. A range the cursor entered (a human attested
        part of it) keeps only its remaining commits: a block, or an attempt in flight, still
        binds them, and anything else is inspected afresh. The first range that does not continue
        the prefix ends the window, and the commits after it are planned afresh."""
        pending = sa.validate_history(self.repo, cursor, main)
        waiting = set(pending)
        kept, frm, offset = [], cursor, 0
        for slot in slots:
            shas = [c["sha"] for c in slot.batch["commits"]]
            rest = [sha for sha in shas if sha in waiting]
            if not rest:
                continue
            if pending[offset : offset + len(rest)] != rest:
                break
            if rest != shas:
                slot.batch = sa.build_batch(self.repo, frm, slot.batch["main_sha"], rest)
                if slot.status not in ("blocked", "running"):
                    slot.status, slot.verdict = "planned", None
            kept.append(slot)
            frm, offset = rest[-1], offset + len(rest)
        return kept

    def settle_interrupted(self, cursor: str) -> None:
        """Classify every range left running by a stopped pool before admitting more work.

        Its evidence is read with the range's own input: a recorded concern binds whatever the
        attempt directory still holds, and only an attempt with no judgment is retried."""
        launched = {id(slot) for slot, _ in self.running.values()}
        for slot in [s for s in self.slots if s.status == "running" and id(s) not in launched]:
            attempt = Path(slot.attempts[-1])
            log(f"security-audit pool: settling interrupted attempt {attempt.name} from its evidence")
            try:
                verdict = sa.judged(attempt, slot.batch, "security-audit: interrupted when the pool stopped")
            except Exception as exc:  # noqa: BLE001 - classified exactly like a finished attempt.
                self.stopped(slot, attempt, exc, cursor)
            else:
                self.concluded(slot, attempt, verdict, cursor)

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
        """Align the window with the remote cursor, restoring the durable list after a restart."""
        before = [slot.record() for slot in self.slots]
        if not self.slots and not self.running:
            self.slots = self.restored()
            if self.slots:
                log(f"security-audit pool: restored {len(self.slots)} persisted range(s)")
        self.slots = self.aligned(self.slots, cursor, main)
        if [slot.record() for slot in self.slots] != before:
            self.persist()
        self.settle_interrupted(cursor)

    def refill(self, cursor: str, main: str) -> None:
        # Batches beyond a binding block may be inspected; advance() never passes it.
        frm = self.slots[-1].target if self.slots else cursor
        if frm == self.planning_blocked:
            return
        if not self.slots and len(sa.validate_history(self.repo, cursor, main)) < self.min_commits:
            return
        if not self.slots and self.inherited is not None:
            previous, batch, provider, credential = self.inherited
            self.inherited = None
            commits = [c["sha"] for c in batch["commits"]]
            if batch["from_sha"] == cursor and sa.validate_history(self.repo, cursor, main)[: len(commits)] == commits:
                slot = Slot(batch, provider=provider, credential=credential, resume=previous, inherited=True)
                self.slots.append(slot)
                self.persist()
                frm = slot.target
                log(
                    f"security-audit pool: continuing the Worker's {sa.reviewer_model(provider)} inspection"
                    f" {cursor[:12]}..{frm[:12]}"
                )
        while len(self.slots) < self.window:
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
            self.persist()
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
            recorded = evidence.recorded(self.work, slot.batch)
            if recorded.concerns() or recorded.unreadable:
                refused = "security-audit: this batch already has a binding judgment; it is never inspected again"
                slot.status = "blocked"
                verdict = evidence.finding(slot.batch, recorded)
                failure = {
                    "from_sha": slot.batch["from_sha"],
                    "to_sha": slot.target,
                    "reason": refused,
                    "outcome": "finding" if verdict else "unreadable",
                    "verdict": verdict,
                }
                sa.record(self.work / "ledger.jsonl", failure)
                self.block(slot, failure, head=slot.batch["from_sha"] == self.slots[0].batch["from_sha"])
                log(f"security-audit pool: {slot.batch['from_sha'][:12]}..{slot.target[:12]} {refused}")
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
            self.persist()
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
        if provider == "claude" and resume is not None:
            continued = self.continue_claude(batch, attempt, protocol, credential, resume)
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

    def continue_claude(self, batch, attempt, protocol, credential, resume) -> dict | None:
        """Continue a stopped Haiku attempt in its own retained Claude session.

        Without a valid retained session, the attempt starts fresh: only an attempt
        whose evidence holds no concern is ever retried."""
        from .audit_progress import claude_continuable
        from .audit_recovery import retained_claude

        try:
            original, retained_batch, execution = retained_claude(self.work, resume)
        except (Die, OSError, ValueError, KeyError, TypeError, AttributeError):
            # Its classification found no concern, so a fresh attempt discards none.
            return None
        if retained_batch != batch:
            raise Die("security-audit: retained pool attempt belongs to another batch")
        # Lineage: a later pass in this attempt cannot discard the stopped attempt's output.
        (attempt / "previous-block.json").write_text(sa.serialize(resume))
        transcript = original / "model-transcript.jsonl"
        progress = claude_continuable(
            transcript.read_text() if transcript.exists() else "",
            batch,
            original / "read-receipt.json",
            original,
            paged=execution["paged"],
        )
        return sa.inspect(
            batch,
            "claude",
            attempt,
            resume_from=original,
            continue_reading=progress,
            protocol=protocol,
            claude_credential=credential,
        )

    def finished(self, future, cursor: str) -> None:
        slot, attempt = self.running.pop(future)
        try:
            verdict = future.result()
        except BaseException as exc:  # noqa: BLE001 - classified below; never silently dropped.
            self.stopped(slot, attempt, exc, cursor)
        else:
            self.concluded(slot, attempt, verdict, cursor)

    def stopped(self, slot: Slot, attempt: Path, exc: BaseException, cursor: str) -> None:
        """An attempt that ended without a binding verdict: retry it, or block the range."""
        span = f"{slot.batch['from_sha'][:12]}..{slot.target[:12]}"
        failure = {
            "from_sha": slot.batch["from_sha"],
            "to_sha": slot.target,
            "reason": str(exc) or type(exc).__name__,
            "attempt": str(attempt),
            "outcome": "unreadable" if isinstance(exc, Unreadable) else "not-judged",
            **self.reviewer(slot),
        }
        if isinstance(exc, subprocess.TimeoutExpired) or isinstance(exc.__cause__, subprocess.TimeoutExpired):
            failure["failure_kind"] = "provider-timeout"
        sa.record(self.work / "ledger.jsonl", failure)
        if operational(exc) and slot.failures < MAX_RETRIES:
            slot.failures += 1
            slot.status = "planned"
            # Both reviewers continue the same retained session when it is valid.
            slot.resume = failure
            slot.retry_at = time.time() + RETRY_SECONDS * 2 ** (slot.failures - 1)
            self.persist()
            log(
                f"security-audit pool: {span} operational failure on {slot.provider} ({exc}); "
                f"retry {slot.failures}/{MAX_RETRIES}"
            )
            return
        if operational(exc):
            failure["pool_retries_exhausted"] = True
        slot.status = "blocked"
        self.block(slot, failure, head=slot.batch["from_sha"] == cursor)
        log(f"security-audit pool: {span} blocked: {exc}")

    def concluded(self, slot: Slot, attempt: Path, verdict: dict, cursor: str, *, batch: dict | None = None) -> None:
        """A binding verdict: a pass may advance; any recorded concern blocks the range for review."""
        span = f"{slot.batch['from_sha'][:12]}..{slot.target[:12]}"
        launched = (attempt / "execution.json").exists()
        sa.record(
            self.work / "ledger.jsonl",
            sa.review_record(
                self.actor or "",
                slot.provider,
                sa.attempt_protocol(attempt) if launched else self.protocol(slot.provider),
                batch or slot.batch,
                attempt,
                verdict,
                slot.credential,
            ),
        )
        slot.verdict = verdict
        if verdict["verdict"] == "pass" and verdict["input_digest"] != slot.digest:
            # The cursor entered this range while it ran; its remaining commits are inspected afresh.
            slot.status, slot.verdict = "planned", None
            self.persist()
            return
        if verdict["verdict"] == "pass":
            slot.status = "pass"
            self.persist()
            log(f"security-audit pool: {span} pass")
            return
        slot.status = "blocked"
        failure = {
            "from_sha": slot.batch["from_sha"],
            "to_sha": slot.target,
            "reason": "security-audit: " + "; ".join(verdict["findings"]),
            "attempt": str(attempt),
            "outcome": "finding",
            "verdict": verdict,
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
        self.persist()
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
            self.persist()
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
        """Archive a block whose evidence binds nothing, so a fresh isolated attempt can inspect the batch.

        A recorded concern or unreadable evidence (in the attempt, its lineage or the ledger),
        a planning block and exhausted pool retries remain binding."""
        if previous.get("manual_inspection_required") or previous.get("pool_retries_exhausted"):
            return False
        span = (previous.get("from_sha"), previous.get("to_sha"))
        ledger = self.work / "ledger.jsonl"
        for line in ledger.read_text().splitlines() if ledger.exists() else ():
            row = json.loads(line)
            if (row.get("from_sha"), row.get("to_sha")) == span and concern(row.get("verdict")):
                return False
        for key in ("attempt", "continuation_of"):
            if previous.get(key) and holds(self.work, Path(previous[key])):
                return False
        try:
            from .audit_recovery import retained, retained_claude

            if (Path(previous["attempt"]) / sa.CLAUDE_SESSIONS).exists():
                _, batch, execution = retained_claude(self.work, previous)
                credential = "api-key" if "ANTHROPIC_API_KEY" in execution.get("environment", {}) else "subscription"
                # A batch keeps its reviewer for life: continue this Haiku session in the pool.
                self.inherited = (previous, batch, "claude", credential)
            else:
                _, batch, execution, _ = retained(self.work, previous)
                if execution.get("argv", [""])[0] == "codex":
                    # A batch keeps its reviewer for life: continue this Codex session in the pool.
                    self.inherited = (previous, batch, "codex", "subscription")
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
        """One control pass. A binding block at the cursor holds advancement, never the pool."""
        try:
            cursor, main = sa.remote_sha(sa.CURSOR), sa.remote_sha(os.environ.get("TAUCETI_BASE_BRANCH", "main"))
            if (cursor, main) != self.fetched:
                sa.refresh(self.repo, cursor, main)
                self.fetched = (cursor, main)
            self.observation = (cursor, len(sa.validate_history(self.repo, cursor, main)))
        except BaseException as exc:
            self.observation = exc
            raise
        self.sync(cursor, main)
        blocked = self.work / "blocked.json"
        hold = False
        if blocked.exists():
            previous = json.loads(blocked.read_text())
            # A launch journals its own attempt in blocked.json. One of this pool's
            # own attempts is not a block: its slot's status already binds it.
            mine = {str(attempt) for _, attempt in self.running.values()}
            mine.update(a for slot in self.slots for a in slot.attempts)
            ours = previous.get("attempt") in mine
            hold = previous.get("from_sha") == cursor and not ours and not self.supersede(previous)
        # Delivery is idempotent and backs off on its own receipts. Re-offer every held block on
        # each pass, so a failed DM is retried rather than only attempted when the block is created.
        if hold:
            notify_block(self.work, previous)
        for slot in self.slots:
            if slot.status == "blocked" and slot.failure:
                notify_block(self.work, slot.failure)
        if hold and not (self.slots and self.slots[0].status in ("pass", "blocked")):
            # The batch at the cursor holds a binding judgment this pool cannot
            # identify in its window; never plan or inspect it again. Wait for review.
            if self.holding != cursor:
                log(f"security-audit pool: unresolved block at the cursor; read {blocked}")
                self.holding = cursor
            return True
        if self.slots and self.slots[0].status == "blocked":
            if self.holding != cursor:
                log("security-audit pool: block at the cursor awaits human review; inspecting ahead")
                self.holding = cursor
        else:
            self.holding = None
        cursor = self.advance(cursor)
        self.observation = (cursor, len(sa.validate_history(self.repo, cursor, main)))
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

    def observed(self) -> tuple[str, int]:
        if isinstance(self.observation, BaseException):
            raise self.observation
        return self.observation

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
                    finally:
                        # Every pass, including one that fails with any exception.
                        audit_watchdog.check(self.work, self.observed, min_commits=self.min_commits)
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
