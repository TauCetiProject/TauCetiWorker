"""Host review cache configuration survives old Review versions and explicit overrides."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, claude_api, fleet_capacity, github, shared_repositories

with tempfile.TemporaryDirectory() as tmp:
    for value in (None, "", tmp + "/custom"):
        env = {"TAUCETI_STREAM": "1", "TAUCETI_SHARED_REPOSITORIES_DIR": tmp + "/repositories"}
        if value is not None:
            env["TAUCETI_REVIEW_MATHLIB_CACHE"] = value
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(claude_api, "review_environment", return_value=None),
            patch.object(fleet_capacity, "check_launch_admission"),
            patch.object(github, "claims_repo", return_value="org/claims"),
            patch.object(agents, "run_process", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            argv = ["tauceti-review", "7"]
            assert agents.run_to_logfile(argv, Path(tmp) / "review.log", "review #7") == 0
            selected = run.call_args.kwargs["env"]["TAUCETI_REVIEW_MATHLIB_CACHE"]
            assert selected == (tmp + "/repositories/review-mathlib" if value is None else value)
            assert run.call_args.args[0] == argv, "older Review must not receive an unknown CLI flag"
            assert shared_repositories.review_mathlib_cache() == selected
print("host review shared-source configuration and compatibility checks passed")
