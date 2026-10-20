"""tauceti_worker.round — round lifecycle: the per-worker lock, signal/cleanup handling, the
branch-claim heartbeat, and the loop→child round spawn with process-group teardown."""

from __future__ import annotations

import atexit
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

from .config import Config, Die, log
from .constants import CLAIM_HEARTBEAT_S, CLAIM_TTL_S, ROUND_TIMEOUT
from .github import claims_repo
from .paths import CLAIM_SH, self_argv, self_env

# ============================================================================
# Round lifecycle — flock (one round per worker), signal handling, cleanup, and
# the loop→child spawn with process-group teardown.
#
# Python's default close_fds=True means children never inherit the round.lock fd,
# so the old shell worker's hand-managed `9>&-` fd-leak fix (commit 3e4828b) is automatic; we
# also mark the lock fd non-inheritable as belt-and-suspenders. Running each round
# as a child of the loop in its OWN session is what makes timeout teardown, the
# cleanup-on-exit, and a SIGKILL-self-cleaning bubble behave like the shell.
# ============================================================================


class RoundContext:
    """Holds the per-worker round lock for the life of a round, runs cleanup on any exit, and routes
    SIGTERM→143 / SIGINT→130 through that cleanup (so a loop-sent SIGTERM still releases the lease)."""

    def __init__(self, cfg: Config, *, record_activity=True):
        self.cfg = cfg
        self.deadline = float(os.environ.get("TAUCETI_ROUND_DEADLINE") or (time.time() + ROUND_TIMEOUT))
        os.environ["TAUCETI_ROUND_DEADLINE"] = str(self.deadline)
        self._fd: int | None = None
        self._cleanups: list = []
        self._done = False
        self.interrupted = False
        self.activity = None
        self.record_activity = record_activity

    def __enter__(self) -> RoundContext:
        self.cfg.state.mkdir(parents=True, exist_ok=True)
        path = self.cfg.state / "round.lock"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Die(
                f"another round for worker '{self.cfg.wid}' holds {path} — one round per worker at a time"
            ) from None
        self._fd = fd
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        atexit.register(self._cleanup)
        if self.record_activity:
            from .retention import recycle

            try:
                result = recycle(self.cfg)
                for error in result["errors"]:
                    log(f"retention: {error}")
                if result["removed"]:
                    log(f"retention: recycled {result.get('removed_count', len(result['removed']))} generated paths")
            except Exception as error:
                log(f"retention: cache recycling deferred: {error}")
        if self.record_activity:
            from .cache_storage import admit

            try:
                admit(self.cfg)
            except BaseException:
                self._cleanup()
                raise
            from .activity import Round

            self.activity = Round(self.cfg.wid)
        # Register first: LIFO cleanup records resources after claim/build teardown.
        try:
            from .resources import RoundResources

            monitor = RoundResources(self.cfg).start()
            self.add_cleanup(monitor.finish)
        except Exception as error:
            log(f"resources: could not start sampling ({type(error).__name__}: {error})")
        return self

    def add_cleanup(self, fn) -> None:
        self._cleanups.append(fn)

    def _cleanup(self) -> None:
        if self._done:
            return
        self._done = True
        if self.activity:
            self.activity.finish()
        for fn in reversed(self._cleanups):  # LIFO: stop heartbeat, pop bubble, release claim
            try:
                fn()
            except Exception as e:
                log(f"cleanup step failed: {e}")
        if self._fd is not None:
            try:
                if self.record_activity:
                    try:
                        os.utime(self._fd, None)  # last use, without another lifecycle record
                    except OSError as error:
                        log(f"retention: could not record checkout use: {error}")
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __exit__(self, *exc) -> bool:
        self.interrupted = exc[0] in (SystemExit, KeyboardInterrupt)
        if self.activity:
            self.activity.finish(exc[1])
        self._cleanup()
        return False


def spawn_round(argv_tail: list[str], *, timeout: int = ROUND_TIMEOUT) -> subprocess.Popen:
    """Spawn one round as a child in its OWN session (so the loop can kill the whole group). Invokes
    the current interpreter directly on this file (NOT via the uv shebang) to avoid a uv wrapper
    process between the loop and the round — sys.executable is already the uv-resolved interpreter."""
    cmd = self_argv("_round", *argv_tail)
    env = self_env()
    env["TAUCETI_ROUND_DEADLINE"] = str(time.time() + timeout)
    return subprocess.Popen(cmd, start_new_session=True, env=env)


def signal_group(pgid: int, sig: int) -> str:
    """Send `sig` to process group `pgid`. Returns "sent", "gone", or "denied"; never raises.

    "gone" is ESRCH: no such process group, so there is nothing to clean up.

    "denied" is EPERM: the group exists but killpg found no member it could signal. On Darwin that
    is what a group reports once its survivors are all zombies — XNU's killpg1 skips SZOMB members
    while iterating an explicit process group and then returns EPERM having signalled none — which
    is the state a just-killed leader is in until its parent wait()s it, and is why the round sweep
    blew up on macOS. It is NOT only that. A live member whose real or saved uid no longer matches
    ours (an agent running `sudo -u other`), a MAC policy denial, or the accepted pgid-reuse race
    landing on somebody else's group all report the same thing, and killpg cannot tell them apart.

    So "denied" must not be read as "clean". Callers stop, because a signal that could not be
    delivered will not be delivered by repeating it, but they say so rather than claiming success.
    """
    try:
        os.killpg(pgid, sig)
        return "sent"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"


def kill_round_group(p: subprocess.Popen, term_grace: int = 30) -> None:
    """SIGTERM the round's process group, give it term_grace to clean up, then SIGKILL — the
    `timeout --kill-after=30s` teardown, but reaching the whole group (agent + build daemons)."""
    try:
        pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        return
    sent = signal_group(pgid, signal.SIGTERM)
    if sent == "gone":
        return
    if sent == "denied":
        # Nothing in the group can be signalled, so neither the grace wait nor SIGKILL will achieve
        # anything, and p.wait() below would block for as long as the leader happens to live.
        log(f"WARNING: round group {pgid} refused SIGTERM; leaving teardown to the operator")
        return
    try:
        p.wait(term_grace)
    except subprocess.TimeoutExpired:
        if signal_group(pgid, signal.SIGKILL) == "denied" and p.poll() is None:
            # The leader is still running and we could not signal its group. Nothing here can force
            # it down, and p.wait() below would block forever, so report and leave it to the operator.
            log(f"WARNING: round {p.pid} is still alive but its process group refused SIGKILL; not waiting")
            return
        p.wait()


def reap_round_group(pgid: int, term_grace: float = 2.0) -> None:
    """Sweep any processes still alive in a finished round's process group — the background poll-loops a
    tool-using agent leaves behind. Claude Code's Bash tool, waiting on a backgrounded `lake build`,
    synthesizes `until ... do sleep; done` (and, on a job-control quirk in non-interactive bash, a
    `until ! kill -0 %1; do :; done` that busy-spins a whole core). When the agent exits 0 those loops
    have no parent left and reparent to init, surviving forever. The round runs in its OWN session
    (spawn_round's start_new_session ⇒ the group id equals the leader pid), so signalling `pgid` reaches
    only the round's descendants — never the loop driver or the user's shell. Idempotent: a no-op when
    the group is already empty (the clean, common case) or already torn down by kill_round_group."""
    sent = signal_group(pgid, signal.SIGTERM)
    if sent == "gone":
        return  # group empty — round left nothing behind
    if sent == "denied":
        log(f"round group {pgid}: nothing signalable left to sweep (likely already-exited stragglers)")
        return
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        probe = signal_group(pgid, 0)
        if probe == "gone":
            return  # stragglers died on SIGTERM
        if probe == "denied":
            log(f"round group {pgid}: survivors are no longer signalable; leaving them")
            return
        time.sleep(0.05)
    if signal_group(pgid, signal.SIGKILL) == "denied":
        log(f"WARNING: round group {pgid} ignored SIGTERM and refused SIGKILL; stragglers may survive")


class Claims:
    """[COOP] branch claims + the [HARD] push-arbiter env. Mutating tasks take a branch/<pr> claim and
    heartbeat it (dedup only; git-safe-push's branch CAS is the real guarantee). The heartbeat is a
    detached child that dies with the parent via an inherited pipe (EOF when the parent goes, even on
    SIGKILL), never runs the round's cleanup, and never holds the round.lock fd (pass_fds keeps only
    the pipe; the lock fd is non-inheritable + closed by close_fds)."""

    def __init__(self, cfg: Config, ctx: RoundContext):
        self.cfg = cfg
        self.ctx = ctx
        self.held: tuple[str, str] | None = None
        self._hb: subprocess.Popen | None = None
        self._hb_wfd: int | None = None
        # Set when the heartbeat reports the lease lost; branch work passes it to the agent as its
        # cancellation event, so a lost claim cancels the owned agent process group.
        self.lost = threading.Event()
        self._stopped = threading.Event()

    def begin_branch_work(self, pr: int, head: str, refname: str, owner: str, repo: str) -> bool:
        """Take the branch claim and set the push-arbiter env. Returns False if claimed elsewhere
        (caller skips this PR — dedup). A claim error is non-fatal: proceed unclaimed (CAS still protects).

        The claim goes to the worker's claim namespace, NOT to the PR's head repository: `branch/<pr>`
        is keyed on the canonical PR number, so two workers contend for it wherever they are, whereas a
        claim in someone else's fork is one only its owner can push. `owner`/`repo` still name the head
        repo, because that is where the push arbiter's branch CAS runs."""
        key = f"branch/{pr}"
        claim_repo = claims_repo()
        claim_env = {**os.environ, "CLAIM_REPO": claim_repo}
        rc = subprocess.run([CLAIM_SH, "acquire", key, str(CLAIM_TTL_S)], capture_output=True, env=claim_env).returncode
        if rc == 1:
            log(f"branch #{pr} claimed by another worker — skipping (COOP dedup)")
            return False
        os.environ["TAUCETI_PUSH_REF"] = refname
        os.environ["TAUCETI_PUSH_EXPECT"] = head
        os.environ["TAUCETI_PUSH_REMOTE"] = f"https://github.com/{owner}/{repo}"
        os.environ["TAUCETI_CLAIM_SH"] = CLAIM_SH
        if rc == 0:
            self.held = (key, claim_repo)
            # Keep this scoped to the push arbiter: unrelated agent-invoked claims remain canonical.
            os.environ["TAUCETI_CLAIM_REPO"] = claim_repo
            os.environ["TAUCETI_CLAIM_KEY"] = key
            self.ctx.add_cleanup(self.release)
            self.start_heartbeat(key, claim_repo)
        else:
            log(
                f"claim acquire #{pr} errored (rc={rc}) against {claim_repo} — proceeding unclaimed "
                f"(branch CAS still protects). If this repeats, this account cannot push there; set "
                f"CLAIM_REPO=<a repo your whole fleet can push to> to pick the namespace yourself."
            )
            os.environ.pop("TAUCETI_CLAIM_KEY", None)
            os.environ.pop("TAUCETI_CLAIM_REPO", None)
        return True

    def start_heartbeat(self, key: str, claim_repo: str) -> None:
        rfd, wfd = os.pipe()
        os.set_inheritable(rfd, True)
        cmd = self_argv("_heartbeat", key, "--ppipe", str(rfd))
        env = self_env(
            {
                **os.environ,
                "CLAIM_REPO": claim_repo,
                "TAUCETI_CLAIM_SH": CLAIM_SH,
                "CLAIM_TTL": str(CLAIM_TTL_S),
            }
        )
        self._hb = subprocess.Popen(cmd, pass_fds=[rfd], env=env)
        os.close(rfd)  # parent keeps only the write end; its closure (or death) is the EOF signal
        self._hb_wfd = wfd
        self.ctx.add_cleanup(self.stop_heartbeat)
        # Each heartbeat has its own lost/stopped state, so one claim's loss never cancels the next.
        self.lost = lost = threading.Event()
        self._stopped = stopped = threading.Event()

        def watch(hb=self._hb):
            if hb.wait(None) == HEARTBEAT_LOST and not stopped.is_set():
                log(f"claim {key}: lease lost; cancelling the branch work that depends on it")
                lost.set()

        threading.Thread(target=watch, daemon=True).start()

    def stop_heartbeat(self) -> None:
        self._stopped.set()
        if self._hb_wfd is not None:
            try:
                os.close(self._hb_wfd)  # EOF → the heartbeat child exits on its own
            except OSError:
                pass
            self._hb_wfd = None
        if self._hb is not None:
            try:
                self._hb.terminate()
                self._hb.wait(5)
            except Exception:
                pass
            self._hb = None

    def release(self) -> None:
        if self.held:
            key, claim_repo = self.held
            subprocess.run(
                [CLAIM_SH, "release", key], capture_output=True, env={**os.environ, "CLAIM_REPO": claim_repo}
            )
            self.held = None
            os.environ.pop("TAUCETI_CLAIM_KEY", None)
            os.environ.pop("TAUCETI_CLAIM_REPO", None)


HEARTBEAT_LOST = 3


def cmd_heartbeat(args) -> int:
    """Internal: renew a claim lease every CLAIM_HEARTBEAT_S until the parent dies (pipe EOF) or the
    lease is lost. Never runs the round's cleanup (default signal disposition, no RoundContext)."""
    import select

    key = args.key
    ppipe = args.ppipe
    while True:
        if ppipe is not None:
            r, _, _ = select.select([ppipe], [], [], CLAIM_HEARTBEAT_S)
            if r:  # readable ⇒ EOF (we never write to the pipe) ⇒ parent gone
                try:
                    if os.read(ppipe, 1) == b"":
                        return 0
                except OSError:
                    return 0
        else:
            time.sleep(CLAIM_HEARTBEAT_S)
        if subprocess.run([CLAIM_SH, "renew", key], capture_output=True).returncode != 0:
            return HEARTBEAT_LOST  # stop renewing so it can expire, and tell the round


def run_round_subprocess(argv_tail: list[str], timeout: int = ROUND_TIMEOUT) -> int:
    """Run one round as a child under a hard timeout; tear down the group on expiry. Used by the loop.
    Maps a timed-out round to rc 124, a SIGKILL-after-grace to 137 (matching the shell's `timeout`)."""
    from .budget import budget_dir, process_identity
    from .claude_api import api_mode
    from .usage_supervisor import ensure_supervisor, record_round_end

    accounting = api_mode()
    if accounting:
        try:
            ensure_supervisor(budget_dir())
        except Exception as error:
            log(f"usage supervisor startup pending: {error}")
    p = spawn_round(argv_tail, timeout=timeout)
    owner = process_identity(p.pid) if accounting else None
    ending = "exit"
    pgid = p.pid  # spawn_round's start_new_session ⇒ the round leads its own group; pgid == leader pid
    try:
        return p.wait(timeout)
    except subprocess.TimeoutExpired:
        ending = "timeout"
        log(f"round timed out after {timeout}s — tearing down")
        kill_round_group(p)
        rc = p.returncode
        return 137 if rc is not None and rc < 0 and -rc == signal.SIGKILL else 124
    except KeyboardInterrupt:
        ending = "signal"
        kill_round_group(p)
        raise
    finally:
        # Even a round that exits 0 can leave the agent's backgrounded build-waiters alive; the timeout
        # path's kill_round_group never runs for it. Sweep the group on EVERY exit so a leaked poll-loop
        # lives at most one round, not forever (a no-op once kill_round_group already cleared the group).
        # Unlike kill_round_group (which signals while the leader PID is still live), p.wait() has already
        # reaped the leader here, so the group is held open only by stragglers. The lone wrong-kill window
        # — the freed leader PID being reused AND the reuser making itself a group leader before this line
        # — is microseconds wide and needs a deliberate setsid; we accept it. Host agents and
        # supervised tools also sweep their own groups, including in one-shot rounds.
        reap_round_group(pgid)
        if accounting:
            try:
                record_round_end(
                    budget_dir(),
                    owner,
                    "signal" if ending == "exit" and p.returncode is not None and p.returncode < 0 else ending,
                    p.returncode,
                )
                ensure_supervisor(budget_dir())
            except Exception as error:
                log(f"usage supervisor recovery pending: {error}")
