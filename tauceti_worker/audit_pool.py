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
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audit_evidence as evidence
from . import audit_watchdog
from . import security_audit as sa
from .audit_alerts import notify_block, save
from .audit_evidence import NotJudged, Unreadable
from .config import Die, NoProgress, log

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
    provider: str | None = None  # The reviewer of the latest attempt.
    credential: str | None = None
    avoid: str | None = None  # A retry prefers another reviewer than this one, when one is available.

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
            "avoid": self.avoid,
        }


RETRY_CAP = 3600  # A retry backs off up to an hour, and never stops.
KEPT_ATTEMPTS = 2  # Attempts of a range kept while it is retried.


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
        self.started = time.time()
        self.advanced_commits = 0
        self.planning_blocked: str | None = None
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
                    avoid=record.get("avoid"),
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
    def imported(self, main: str) -> None:
        """Carry records written before binding lived in the ranges and the ledger into the ledger.

        A legacy block's attempt, or the attempt of a sequential round that stopped mid-inspection
        (`running.json`), may hold a concern or unreadable evidence that no ledger verdict records.
        Its evidence is classified once and any hold is recorded against its commits, so it binds
        every range containing them; the block is then marked with its outcome."""
        running = self.work / "running.json"  # A sequential round stopped mid-inspection.
        if running.exists():
            try:
                attempt = json.loads(running.read_text())["attempt"]
                stopped = json.loads((Path(attempt) / "input.json").read_text())
                block = {"from_sha": stopped["from_sha"], "to_sha": stopped["commits"][-1]["sha"], "attempt": attempt}
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                # Its range is unknown, so nothing can bind it: hold the pool for review (the watchdog DMs).
                raise NoProgress(
                    f"security-audit pool: {running} cannot be settled; preserve it for review: {exc}"
                ) from exc
            self.import_block(block, main)
            running.unlink()
        # The pool's batch files from before ranges.json: a blocked batch's attempts may hold a
        # concern that no ledger verdict records.
        for path in sorted(self.store.glob("*.json")):
            if path.name == RANGES or path.name.endswith(".input.json"):
                continue
            try:
                old = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise NoProgress(f"security-audit pool: {path} cannot be read; preserve it for review: {exc}") from exc
            if not isinstance(old, dict) or old.get("status") != "blocked" or old.get("imported"):
                continue
            span = {"from_sha": old.get("from_sha"), "to_sha": old.get("to_sha")}
            if isinstance(old.get("verdict"), dict):
                self.import_block({**span, "verdict": old["verdict"]}, main, verdict=old["verdict"])
            attempts = old.get("attempts") or []
            for attempt in attempts:
                self.import_block({**span, "attempt": attempt}, main)
            if not attempts and not isinstance(old.get("verdict"), dict):
                # A block with no evidence to classify: hold its commits rather than forget it.
                self.import_block(
                    {**span, "reason": "security-audit: legacy pool block without evidence"}, main, hold=True
                )
            save(path, {**old, "imported": True})
        path = self.work / "blocked.json"
        try:
            block = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            block = {"reason": "security-audit: unreadable blocked.json"}
        if "outcome" in block or not block:
            return
        save(path, {**block, **self.import_block(block, main)})

    def import_block(self, block: dict, main: str, *, verdict: dict | None = None, hold: bool = False) -> dict:
        """Record in the ledger what one legacy record's attempt holds; return its outcome."""
        outcome = {"outcome": "not-judged"}
        if verdict is not None:
            if not (verdict.get("findings") or verdict.get("verdict") == "finding"):
                return outcome  # Names no concern: the range is inspected afresh.
            outcome = {"outcome": "finding", "verdict": verdict}
        elif hold:
            outcome = {"outcome": "unreadable", "reason": block["reason"]}
        elif block.get("manual_inspection_required"):
            outcome = {}  # A planning block is planned again, and blocks again if it still cannot be.
        elif block.get("attempt") and Path(block["attempt"]).exists():  # A missing attempt recorded nothing.
            attempt = Path(block["attempt"])
            try:
                batch = json.loads((attempt / "input.json").read_text())
                outcome = evidence.outcome_of(sa.judged(attempt, batch, "security-audit: legacy block"), None)
            except evidence.NotJudged:
                pass
            except Exception as exc:  # noqa: BLE001 - evidence that cannot be classified holds.
                outcome = {"outcome": "unreadable", "reason": f"security-audit: legacy block evidence: {exc}"}
        if outcome:
            frm, to = block.get("from_sha"), block.get("to_sha")
            commits = []
            if frm and to:
                try:
                    history = sa.validate_history(self.repo, frm, main)
                except Die:
                    history = []  # Not on main's history: it cannot bind a range the pool plans.
                commits = history[: history.index(to) + 1] if to in history else []
            sa.record(self.work / "ledger.jsonl", {"legacy_block": block, "commits": commits, **outcome})
        return outcome or {"outcome": "not-judged"}

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
    def refused(self, slot: Slot) -> bool:
        """Block a range the ledger already binds: a recorded concern or hold about any of its commits."""
        recorded = evidence.recorded(self.work, slot.batch)
        if not (recorded.concerns() or recorded.unreadable):
            return False
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
        return True

    def submit(self, executor) -> None:
        busy = len(self.running)
        now = time.time()
        for slot in self.slots:
            if busy >= self.parallel:
                return
            if slot.status != "planned" or slot.retry_at > now:
                continue
            if self.refused(slot):
                continue
            choice = self.chooser.choose(avoid=slot.avoid)
            if choice is None:
                return  # No reviewer under pace and no API key: wait.
            slot.provider, slot.credential = choice
            attempt = self.work / f"attempt-{time.time_ns()}"
            attempt.mkdir()
            (attempt / "input.json").write_text(sa.serialize(slot.batch))
            slot.status = "running"
            slot.attempts.append(str(attempt))
            self.persist()
            commits = len(slot.batch["commits"])
            log(
                f"security-audit pool: inspect {slot.batch['from_sha'][:12]}..{slot.target[:12]} ({commits} commits)"
                f" with {slot.provider}/{sa.reviewer_model(slot.provider)} ({slot.credential}) in {attempt.name}"
            )
            future = executor.submit(self.inspect, slot.batch, attempt, slot.provider, slot.credential)
            self.running[future] = (slot, attempt)
            busy += 1

    @staticmethod
    def inspect(batch, attempt, provider, credential) -> dict:
        """One fresh protocol-5 inspection; a batch that changes no audited file passes as is."""
        if not any(c["files"] for c in batch["commits"]):
            return {
                "input_digest": batch["input_digest"],
                "commits": [c["sha"] for c in batch["commits"]],
                "verdict": "pass",
                "findings": [],
            }
        return sa.inspect(batch, provider, attempt, claude_credential=credential if provider == "claude" else None)

    def finished(self, future, cursor: str) -> None:
        slot, attempt = self.running.pop(future)
        try:
            verdict = future.result()
        except BaseException as exc:  # noqa: BLE001 - classified below; never silently dropped.
            self.stopped(slot, attempt, exc, cursor)
        else:
            self.concluded(slot, attempt, verdict, cursor)

    def stopped(self, slot: Slot, attempt: Path, exc: BaseException, cursor: str) -> None:
        """An attempt that ended without a binding verdict.

        Evidence that is unreadable, or that was never classified, holds the range for a human.
        Anything else is retried autonomously, never bound: a range of several commits that the
        reviewer worked on (it answered or was served input) is replaced by its two complete
        halves, inspected afresh; a single commit, or an attempt where no reviewer engaged (a
        provider or setup failure), is inspected afresh, preferably by the other reviewer. Retries
        back off up to an hour and never stop; the cursor watchdog reports a long stall."""
        span = f"{slot.batch['from_sha'][:12]}..{slot.target[:12]}"
        launched = (attempt / "execution.json").exists()
        hold = isinstance(exc, Unreadable) or (launched and not isinstance(exc, NotJudged))
        failure = {
            "from_sha": slot.batch["from_sha"],
            "to_sha": slot.target,
            "reason": str(exc) or type(exc).__name__,
            "attempt": str(attempt),
            "outcome": "unreadable" if hold else "not-judged",
            "batch": slot.digest,
            "commits": [c["sha"] for c in slot.batch["commits"]],
            **self.reviewer(slot),
        }
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            failure["failure_kind"] = "provider-timeout"
        sa.record(self.work / "ledger.jsonl", failure)
        if hold:
            slot.status = "blocked"
            self.block(slot, failure, head=slot.batch["from_sha"] == cursor)
            log(f"security-audit pool: {span} held for review: {exc}")
            return
        engaged = launched and evidence.engaged(attempt)
        if engaged and len(slot.batch["commits"]) > 1:
            self.split(slot, failure)
            return
        slot.failures += 1
        slot.status = "planned"
        slot.avoid = slot.provider
        # Retries never stop, so their storage must not grow: keep the last two attempts. An older
        # attempt was classified as no judgment and its outcome is in the ledger; one whose
        # evidence holds is never removed (it would have bound the range instead).
        for old in slot.attempts[:-KEPT_ATTEMPTS]:
            if not evidence.holds(self.work, Path(old)):
                shutil.rmtree(old, ignore_errors=True)
        slot.attempts = slot.attempts[-KEPT_ATTEMPTS:]
        slot.retry_at = time.time() + min(RETRY_CAP, RETRY_SECONDS * 2 ** (slot.failures - 1))
        self.persist()
        log(f"security-audit pool: {span} not judged on {slot.provider} ({exc}); fresh retry {slot.failures}")

    def split(self, slot: Slot, failure: dict) -> None:
        """Replace a range by its two halves, in one durable update: complete fresh inspections of
        consecutive commit slices, the second starting at the first's last commit."""
        if not any(s is slot for s in self.slots):
            return  # The cursor moved past it while it ran.
        shas = [c["sha"] for c in slot.batch["commits"]]
        half = len(shas) // 2
        left = Slot(sa.build_batch(self.repo, slot.batch["from_sha"], slot.batch["main_sha"], shas[:half]))
        right = Slot(sa.build_batch(self.repo, shas[half - 1], slot.batch["main_sha"], shas[half:]))
        index = next(i for i, s in enumerate(self.slots) if s is slot)
        self.slots[index : index + 1] = [left, right]
        sa.record(
            self.work / "ledger.jsonl",
            {
                "split": slot.digest,
                "from_sha": slot.batch["from_sha"],
                "to_sha": slot.target,
                "into": [left.digest, right.digest],
                "attempt": failure["attempt"],
                "reason": failure["reason"],
            },
        )
        self.persist()
        log(
            f"security-audit pool: {failure['from_sha'][:12]}..{failure['to_sha'][:12]} not judged; "
            f"split into {len(shas[:half])} + {len(shas[half:])} commits"
        )

    def concluded(self, slot: Slot, attempt: Path, verdict: dict, cursor: str) -> None:
        """A binding verdict: a pass may advance; any recorded concern blocks the range for review."""
        span = f"{slot.batch['from_sha'][:12]}..{slot.target[:12]}"
        sa.record(
            self.work / "ledger.jsonl",
            sa.review_record(self.actor or "", slot.provider, slot.batch, attempt, verdict, slot.credential),
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
            if self.refused(slot):
                break  # A hold recorded for its commits after it passed binds it.
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
        self.imported(main)
        self.sync(cursor, main)
        # Binding ranges are the durable list and the ledger's recorded concerns, never blocked.json,
        # which only mirrors the block at the cursor for operators and alerts. Delivery is idempotent
        # and backs off on its own receipts, so every held block is offered again on each pass.
        for slot in self.slots:
            if slot.status == "blocked" and slot.failure:
                notify_block(self.work, slot.failure)
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

    @contextlib.contextmanager
    def owned(self, busy: type[Exception]):
        """Hold the cursor's scheduler lock; `busy` is raised when another scheduler holds it."""
        self.work.mkdir(parents=True, exist_ok=True)
        with (self.work / "pool.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise busy("security-audit: another scheduler owns this audit state") from None
            yield

    def round(self) -> int:
        """One bounded quantum for a Worker round: advance, inspect at most one range, advance again.

        Raises AuditNotDue when no work is due and NoProgress while it waits (a retry's backoff, a
        binding range, or no reviewer), so the round returns and maintenance takes its turn."""
        with self.owned(sa.AuditNotDue), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            self.step(executor)
            if not self.running:
                if not self.slots and self.planning_blocked is None:
                    raise sa.AuditNotDue("security-audit: no audit work is due")
                raise NoProgress("security-audit: audit work is waiting (backoff, a binding range or no reviewer)")
            for future in concurrent.futures.as_completed(list(self.running)):
                self.finished(future, sa.remote_sha(sa.CURSOR))
            self.advance(sa.remote_sha(sa.CURSOR))
        return 0

    def serve(self, *, stop=None) -> None:
        """Run until `stop(pool)` returns true; a binding block holds advancement, never the pool."""
        with self.owned(Die):
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

    def choose(self, avoid: str | None = None):
        """The first available reviewer, preferring one other than `avoid`; None means wait."""
        options = []
        if self.pin != "claude" and self.under_pace("codex"):
            options.append(("codex", "subscription"))
        if self.pin != "codex":
            if self.under_pace("claude"):
                options.append(("claude", "subscription"))
            elif self.api_key():
                options.append(("claude", "api-key"))
        preferred = [option for option in options if option[0] != avoid]
        return (preferred or options or [None])[0]


class Single:
    """The reviewer of a Worker round: already paced by the round itself."""

    def __init__(self, provider: str):
        if provider not in ("codex", "claude"):
            raise Die("security-audit: the audit reviewer must be codex or claude")
        self.provider = provider

    def describe(self) -> str:
        return f"reviewer: {self.provider}"

    def choose(self, avoid: str | None = None):
        return (self.provider, sa.credential_kind(self.provider))
