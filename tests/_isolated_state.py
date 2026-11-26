"""Point every test module's activity and state paths at a scratch directory.

Every module under tests/ imports this before anything from the project, so the redirection is in
place however the module runs: under tests/run-all, or directly as `python tests/<module>.py`. A
direct run used to write fixture receipts (repos org/repo, a/b, o/r) into the operator's real
activity database.

tests/run-all sets TAUCETI_TEST_STATE_ROOT to the module's own subtree and removes it afterwards.
Without it, this module makes a fresh directory and removes it when the process that made it exits.
The paths are set unconditionally, so a shell that exports the real paths cannot leak through.

`python tests/_isolated_state.py --exports` prints the same settings as shell `export` lines, for
the bash tests and tests/run-all.
"""

import atexit
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

ROOT_VARIABLE = "TAUCETI_TEST_STATE_ROOT"


def settings(root):
    """The environment that keeps a test's state under `root`."""
    activity = Path(root) / "activity"
    return {
        "TAUCETI_ACTIVITY_DIR": str(activity),
        "TAUCETI_FLEET_EVENTS": str(activity / "worker-events.jsonl"),
        "TAUCETI_SHARED_MATHLIB_ROOT": str(activity / "mathlib-builds"),
        "TAUCETI_BUILD_CACHE_ROOT": str(activity / "builds"),
        "TAUCETI_REVIEW_MATHLIB_CACHE": str(activity / "review-sources"),
        "TAUCETI_VERIFY_STORAGE_DIR": str(activity / "verification-storage"),
        "TAUCETI_BUDGET_DIR": str(activity / "budget"),
        # Unit fixtures use their mocked transport, never the public production mirror. Service
        # tests explicitly override this; default-resolution tests remove it.
        "TAUCETI_GITHUB_STATE_URL": "",
        # Rounds start a background cleanup collector on hosts without the systemd timer. Fixtures
        # must never launch one against the checkout's or the host's real data.
        "TAUCETI_BACKGROUND_CLEANUP": "off",
    }


def _remove(path, owner):
    # A forked child that exits normally runs atexit too; only the creating process removes it.
    if os.getpid() == owner:
        shutil.rmtree(path, ignore_errors=True)


def isolate():
    """Apply `settings` to os.environ and return the root."""
    root = os.environ.get(ROOT_VARIABLE)
    if not root:
        root = tempfile.mkdtemp(prefix="tauceti-test-state-")
        atexit.register(_remove, root, os.getpid())
        os.environ[ROOT_VARIABLE] = root
    (Path(root) / "activity").mkdir(parents=True, exist_ok=True)
    os.environ.update(settings(root))
    return Path(root)


if __name__ == "__main__":
    if sys.argv[1:] != ["--exports"]:
        sys.exit("usage: tests/_isolated_state.py --exports  (needs " + ROOT_VARIABLE + ")")
    if not os.environ.get(ROOT_VARIABLE):
        sys.exit("tests/_isolated_state.py --exports: set " + ROOT_VARIABLE + " to a scratch directory")
    root = os.environ[ROOT_VARIABLE]
    for name, value in {ROOT_VARIABLE: root, **settings(root)}.items():
        print(f"export {name}={shlex.quote(value)}")
else:
    ROOT = isolate()
