#!/usr/bin/env python3
"""A failing `tauceti-progress` subcommand writes its WHOLE output down, not a clipped prefix.

The regression this pins: `plan`'s output used to be sliced to 400 characters straight into the main
log. A Python traceback is longer than that before it reaches the exception, so every progress
failure logged the entry point, a cache path, and nothing about what went wrong. One such failure
latched the worker's error breaker and stopped all progress reporting for five days; diagnosing it
meant re-running the command by hand, because the error itself had never been written down.

The bounds matter as much as the saving. A line COUNT bounds nothing when a tool dies without
printing a newline, and the resulting text goes both to an operator's terminal and into an exception.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

# A real traceback, long enough that any prefix cap would cut it before the exception line.
FRAMES = "\n".join(
    f'  File "/state/cache/uvx/tauceti-progress/{"f" * 40}/lib/progress/plan.py", line {n}, in step{n}\n'
    f"    result = step{n + 1}()"
    for n in range(1, 12)
)
TRACEBACK = (
    "Traceback (most recent call last):\n"
    + FRAMES
    + (
        "\nprogress.window.GitError: 76db282 is not an ancestor of 11ef09d; the cursor does not "
        "belong to this history (rewritten branch, or a cursor from another repository)"
    )
)
EXCEPTION_LINE = "progress.window.GitError: 76db282 is not an ancestor"

assert len(TRACEBACK) > 400, "the fixture must exceed the old cap or it proves nothing"


def check(name, ok, detail=""):
    print(f"[{'OK ' if ok else 'XX '}] {name}{f': {detail}' if detail and not ok else ''}")
    return ok


def run(sub, stdout="", stderr="", logdir=None, logger=None):
    """Drive the helper, capturing what it sends to the main log. Returns (reason, lines)."""
    lines = []
    w = SimpleNamespace(cfg=SimpleNamespace(logdir=logdir))
    proc = subprocess.CompletedProcess(args=["tauceti-progress", sub], returncode=1, stdout=stdout, stderr=stderr)
    saved = tc.work_units.log
    tc.work_units.log = logger or (lambda msg="": lines.append(str(msg)))
    try:
        return tc.work_units._progress_tool_failed(w, sub, proc), lines
    finally:
        tc.work_units.log = saved


checks = []

# ----- the whole output is kept, and the exception survives into the reason ----------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs" / "worker1"
    reason, lines = run("plan", stderr=TRACEBACK, logdir=logdir)

    saved = sorted(logdir.glob("progress-plan-*.log"))
    checks.append(check("the output is saved beside the round's other logs", len(saved) == 1, str(saved)))
    if saved:
        body = saved[0].read_text()
        checks.append(check("the saved copy is the WHOLE output", TRACEBACK in body, body[:120]))
        checks.append(check("the reason points at the saved copy", str(saved[0]) in reason, reason))
        mode = stat.S_IMODE(saved[0].stat().st_mode)
        checks.append(check("the saved copy is private", mode == 0o600, oct(mode)))

    checks.append(check("the reason names the exception", EXCEPTION_LINE in reason, reason))
    checks.append(check("the reason names the exit code", "rc=1" in reason, reason))
    joined = "\n".join(lines)
    checks.append(check("the main log gets the tail, including the exception", EXCEPTION_LINE in joined))
    checks.append(check("the tail is bounded", len(lines) <= tc.PROGRESS_TOOL_TAIL + 1, str(len(lines))))

# ----- both streams are kept, and neither is fused onto the other --------------------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs"
    # No trailing newline on stdout, no leading one on stderr: concatenation would invent a line
    # belonging to neither stream, and a stderr-only implementation would drop the payload entirely.
    reason, _ = run("apply", stdout="  payload-no-newline", stderr="ERROR: it failed  ", logdir=logdir)
    body = sorted(logdir.glob("progress-apply-*.log"))[0].read_text()
    checks.append(check("stdout is kept too", "payload-no-newline" in body, body))
    checks.append(check("the streams are labelled", "=== stdout ===" in body and "=== stderr ===" in body, body))
    checks.append(check("neither stream is fused onto the other", "payload-no-newlineERROR" not in body, body))
    checks.append(check("the reason still names the error", "it failed" in reason, reason))

# ----- a newline-free flood is bounded in CHARACTERS, not just lines -----------------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs"
    flood = "x" * 2_000_000
    reason, lines = run("prompt", stderr=flood, logdir=logdir)
    checks.append(
        check(
            "a single enormous line does not flood the main log",
            max(len(x) for x in lines) < 2000,
            str(max(len(x) for x in lines)),
        )
    )
    checks.append(check("nor the exception message", len(reason) < 2000, str(len(reason))))
    body = sorted(logdir.glob("progress-prompt-*.log"))[0].read_text()
    checks.append(check("the private copy keeps all of it", flood in body, str(len(body))))

# ----- terminal control sequences never reach the operator's tty ---------------------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs"
    reason, lines = run("plan", stderr="\x1b]0;pwned\x07\x1b[31mboom\x1b[0m", logdir=logdir)
    joined = "\n".join(lines)
    checks.append(check("escape sequences are stripped from the tail", "\x1b" not in joined, repr(joined)))
    checks.append(check("and from the reason", "\x1b" not in reason, repr(reason)))
    checks.append(check("the readable text survives", "boom" in joined, repr(joined)))

# ----- two failures in the same second do not overwrite each other -------------------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs"
    run("plan", stderr="first failure", logdir=logdir)
    run("plan", stderr="second failure", logdir=logdir)
    bodies = sorted(p.read_text() for p in logdir.glob("progress-plan-*.log"))
    both = "".join(bodies)
    checks.append(check("both failures are retained", "first failure" in both and "second failure" in both, both))

# ----- empty and absent output are handled -------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    logdir = Path(tmp) / "logs"
    w = SimpleNamespace(cfg=SimpleNamespace(logdir=logdir))
    proc = subprocess.CompletedProcess(args=[], returncode=3, stdout=None, stderr=None)
    saved_log = tc.work_units.log
    tc.work_units.log = lambda msg="": None
    try:
        reason = tc.work_units._progress_tool_failed(w, "plan", proc)
    finally:
        tc.work_units.log = saved_log
    checks.append(check("a silent failure still reports its exit code", "rc=3" in reason, reason))

# ----- a diagnostic that cannot be written must not replace the error it describes ---------------
with tempfile.TemporaryDirectory() as tmp:
    blocked = Path(tmp) / "not-a-dir"
    blocked.write_text("in the way")
    reason, _ = run("apply", stdout=TRACEBACK, logdir=blocked / "logs")
    checks.append(check("an unwritable logdir still reports the error", EXCEPTION_LINE in reason, reason))
    checks.append(check("and claims no file it did not write", "full output" not in reason, reason))


def exploding_logger(msg=""):
    raise OSError(28, "No space left on device")


with tempfile.TemporaryDirectory() as tmp:
    blocked = Path(tmp) / "not-a-dir"
    blocked.write_text("in the way")
    try:
        # The disk that refused the diagnostic file is the disk the main log is on, so the call that
        # reports "could not save the output" is itself likely to raise. It must not win.
        reason, _ = run("plan", stderr=TRACEBACK, logdir=blocked / "logs", logger=exploding_logger)
        checks.append(check("a logger that raises does not mask the tool failure", EXCEPTION_LINE in reason, reason))
    except OSError as exc:
        checks.append(check("a logger that raises does not mask the tool failure", False, repr(exc)))

# ----- the apply path still distinguishes success, no-progress and failure -----------------------
src = (REPO / "tauceti_worker" / "work_units.py").read_text()
checks.append(
    check("apply treats 0 and EX_NOPROGRESS as non-failures", "if proc.returncode not in (0, EX_NOPROGRESS):" in src)
)
checks.append(
    check(
        "the failure branch runs before the excerpt",
        src.index("if proc.returncode not in (0, EX_NOPROGRESS):") < src.index("log(out[:600])"),
    )
)
checks.append(check("captured subcommands decode leniently", 'errors="replace"' in src))

sys.exit(0 if all(checks) else 1)
