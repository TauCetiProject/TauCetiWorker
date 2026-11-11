#!/usr/bin/env python3
"""Offline acceptance fixture: real source diagnosis/build/axioms/CAS, simulated GitHub.

Run inside an Elixir/Lean environment:
  python tests/integration_repair_fixture.py --bors-worktree /path/to/bors-ng

All Git writes target a temporary local bare repository. No GitHub calls, candidate
credentials, shared compiled caches, model launch, or production mutation occurs.
GitHub CI/review state is simulated; this is not deployed repair acceptance.
"""

import argparse
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tauceti_worker import work_units
from tauceti_worker.github import GitHub

survey_mod = importlib.import_module("tauceti_worker.survey")


def run_fixture(bors):
    with tempfile.TemporaryDirectory(prefix="integration-repair-") as directory:
        root = Path(directory)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        env.update(
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )

        def run(*args, cwd=None, check=True, extra=None):
            return subprocess.run(
                args, cwd=cwd or root, env=env | (extra or {}), text=True, capture_output=True, check=check
            )

        def git(*args, cwd=None):
            return run("git", *args, cwd=cwd).stdout.strip()

        bare, source, repair = root / "remote.git", root / "source", root / "repair"
        git("init", "--bare", str(bare))
        git("init", "-b", "main", str(source))
        (source / "TauCeti").mkdir()
        (source / "lakefile.toml").write_text(
            'name = "fixture"\ndefaultTargets = ["TauCeti"]\n[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n'
        )
        (source / ".gitignore").write_text(".lake/\nlake-manifest.json\nAudit.lean\n")
        (source / "TauCeti.lean").write_text("")
        (source / "TauCeti/VariableChange.lean").write_text("namespace Fixture\ndef value : Nat := 1\n")
        git("add", ".", cwd=source)
        git("commit", "-qm", "fork point", cwd=source)
        fork = git("rev-parse", "HEAD", cwd=source)
        git("checkout", "-b", "feature", cwd=source)
        consumer = "import TauCeti.VariableChange\nnamespace Fixture\ntheorem branch_value : value = 1 := rfl\n"
        (source / "TauCeti/Consumer.lean").write_text(consumer)
        git("add", ".", cwd=source)
        git("commit", "-qm", "consumer", cwd=source)
        head = git("rev-parse", "HEAD", cwd=source)
        run("lake", "build", cwd=source)  # the existing head really is green
        git("checkout", "main", cwd=source)
        assert git("rev-parse", "HEAD", cwd=source) == fork
        (source / "TauCeti/VariableChange.lean").unlink()
        (source / "TauCeti/CurrentVariableChange.lean").write_text("namespace Fixture\ndef canonicalValue : Nat := 1\n")
        git("add", "-A", cwd=source)
        git("commit", "-qm", "move source and rename API on main", cwd=source)
        base = git("rev-parse", "HEAD", cwd=source)
        git("remote", "add", "origin", str(bare), cwd=source)
        git("push", "origin", "main", "feature", cwd=source)

        spec = importlib.util.spec_from_file_location("fixture_git", bors / "test/local_git_test.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        request = dict(
            operation="compatibility",
            root=str(root / "mirrors"),
            repository="fixture/repository",
            timeout=30,
            max_bytes=100 * 1024**2,
            base_sha=base,
            members=[dict(pr=1, head_sha=head)],
        )
        finding = helper.TestMirror(request, bare).run()["results"][0]
        assert finding["status"] == "missing_source", finding
        assert finding["missing_imports"][0]["path"] == "TauCeti/Consumer.lean"
        finding_path = root / "finding.json"
        finding_path.write_text(json.dumps(finding))
        code = (
            'r=Jason.decode!(File.read!(System.fetch_env!("FIXTURE_FINDING"))); '
            'IO.puts(Jason.encode!(%{body: BorsNG.Worker.SourceCompatibility.request(r["head_sha"], r)}))'
        )
        rendered = run(
            "mix",
            "run",
            "--no-start",
            "-e",
            code,
            cwd=bors,
            extra={"MIX_ENV": "test", "FIXTURE_FINDING": str(finding_path)},
        )
        body = json.loads(rendered.stdout.strip().splitlines()[-1])["body"]
        raw = {
            "number": 1,
            "headRefOid": head,
            "headRefName": "feature",
            "headRepositoryOwner": {"login": "fixture"},
            "headRepository": {"name": "repository"},
            "author": {"login": "fixture"},
            "mergeable": "MERGEABLE",
            "labels": [{"name": "needs-rebase"}],
            "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
        }
        gh = GitHub()
        claim_calls, checks = [], {}
        worker = SimpleNamespace(
            gh=gh,
            claims=SimpleNamespace(begin_branch_work=lambda *args: claim_calls.append(args) or True),
            rs=SimpleNamespace(bust=lambda *args: None),
        )

        def author(_worker, _target, prompt, _opts, **kwargs):
            assert body in prompt and base in prompt and head in prompt
            assert kwargs["allow_push"] == "fixture/repository"
            git("clone", "-b", "feature", str(bare), str(repair))
            git("merge", "--no-edit", "origin/main", cwd=repair)
            failed = run("lake", "build", cwd=repair, check=False)
            assert failed.returncode, "a blind merge unexpectedly repaired the fixture"
            (repair / "TauCeti/Consumer.lean").write_text(
                consumer.replace("TauCeti.VariableChange", "TauCeti.CurrentVariableChange").replace(
                    " : value =", " : canonicalValue ="
                )
            )
            checks["build"] = run("lake", "build", cwd=repair).stdout.strip()
            (repair / "Audit.lean").write_text("import TauCeti.Consumer\n#print axioms Fixture.branch_value\n")
            checks["axioms"] = run("lake", "env", "lean", "Audit.lean", cwd=repair).stdout.strip()
            assert "does not depend on any axioms" in checks["axioms"]
            git("add", "TauCeti/Consumer.lean", cwd=repair)
            git("commit", "-qm", "repair the moved import and renamed API", cwd=repair)
            run(
                str(ROOT / "scripts/git-safe-push"),
                cwd=repair,
                extra={"TAUCETI_PUSH_REF": "feature", "TAUCETI_PUSH_EXPECT": head},
            )
            return 0

        with (
            patch.object(survey_mod, "me", return_value="fixture"),
            patch.object(gh, "open_prs", return_value=[raw]),
            patch.object(gh, "issue_comments", return_value=[{"user": {"login": "tau-ceti-bors[bot]"}, "body": body}]),
        ):
            survey = survey_mod.survey(
                SimpleNamespace(wid="fixture"), gh, None, SimpleNamespace(read=lambda _: 0), deep=False
            )
            candidate = survey.rebaseable.actionable[0]
            with patch.object(work_units, "run_in_bubble", side_effect=author):
                assert (
                    work_units._do_fixlike(
                        worker,
                        survey,
                        candidate,
                        SimpleNamespace(agent_name="Fixture"),
                        True,
                        prompt_file="rebase.md",
                        label="rebase",
                    )
                    == 0
                )
            new_head = git("rev-parse", "refs/heads/feature", cwd=bare)
            assert new_head != head and len(claim_calls) == 1
            request["members"][0]["head_sha"] = new_head
            assert helper.TestMirror(request, bare).run()["results"][0]["status"] == "pass"
            # Ordinary fresh-head green CI/review eligibility is simulated, not published.
            raw["headRefOid"], raw["labels"] = new_head, []
            updated = survey_mod.survey(
                SimpleNamespace(wid="fixture"), gh, None, SimpleNamespace(read=lambda _: 0), deep=False
            )
            assert not updated.rebaseable.actionable
            assert [c.pr for c in updated.reviewable.actionable] == [1]
        return {
            "external_mutations": False,
            "github_ci_review": "simulated",
            "starting_head": head,
            "tested_main": base,
            "repaired_head": new_head,
            "finding": finding,
            "checks": checks,
            "result": "source finding -> authenticated request -> owned worker -> semantic repair -> actual CAS push -> fresh-head eligibility",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bors-worktree", required=True, type=Path)
    print(json.dumps(run_fixture(parser.parse_args().bors_worktree.resolve()), indent=2))
