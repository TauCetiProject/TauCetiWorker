#!/usr/bin/env python3
"""The local scope recipe every authoring/repair prompt embeds chooses the right audit scope.

The recipe is extracted from the RENDERED prompt (so this tests the words an agent is given) and run
against small Git fixtures standing in for a TauCeti checkout. It must:

  1. include committed, staged, unstaged and untracked changes, measured from the merge base with the
     canonical main's remote-tracking ref (never fetched);
  2. list only existing, representable changed modules, in an absolute file;
  3. report full CI requirements for a deleted module (including the old end of a rename), a pin, lakefile
     or toolchain change (also when only in the working tree), a Lean file outside the module
     inventory (root, Unicode, newline), and any broad worker policy;
  4. report `unknown` (no full local fallback) when the base or file list cannot be determined;
  5. accept an honestly empty documentation-only scope;
  6. drive only the cheap changed-file checker, never the whole-library audit;
  7. appear, with the foreground/exit-status rules, in every rendered authoring/repair prompt.

The worker passes the policy from surveyed labels/branch; `scope_policy` is checked here too.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import tauceti_worker as tc

CANONICAL = "https://github.com/TauCetiProject/TauCeti.git"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}" + (f"  ({detail})" if not cond and detail else ""))


def blocks(policy):
    from unittest.mock import patch

    with patch.dict(os.environ, TAUCETI_VERIFY_HELPER="0"):
        text = tc.agents.local_verification(policy)
    found = re.findall(r"```bash\n(.*?)```", text, re.S)
    assert len(found) == 3, "expected scope, audit and lint blocks"
    return found


def git(repo, *args, **kw):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env, **kw)


def write(repo, rel, text="x\n"):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture(root, name, *, remote=CANONICAL, track_main=True):
    """A checkout on a feature branch whose canonical main is the initial commit."""
    repo = Path(root) / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel in (
        "TauCeti.lean",
        "TauCeti/A.lean",
        "TauCeti/B.lean",
        "TauCeti/Old.lean",
        "README.md",
        "lake-manifest.json",
        "lean-toolchain",
        "lakefile.toml",
    ):
        write(repo, rel)
    write(repo, ".gitignore", "/.lake/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", remote)
    if track_main:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-qb", "feature")
    return repo


def scope(repo, policy="ordinary", cwd=None, config=None):
    recipe = blocks(policy)[0]
    env = dict(os.environ, GIT_CONFIG_GLOBAL=config or "/dev/null", GIT_CONFIG_NOSYSTEM="1")
    run = subprocess.run(["bash", "-c", recipe], cwd=cwd or repo, capture_output=True, text=True, env=env)
    out = Path(repo) / ".lake" / "tauceti-scope"
    mode = (out / "mode").read_text().strip() if (out / "mode").exists() else f"<none: {run.stderr}>"
    modules = (out / "modules.txt").read_text().split() if (out / "modules.txt").exists() else None
    return run.returncode, mode, modules


def expect(name, repo, kind, modules=None, reason="", policy="ordinary", **kw):
    rc, mode, listed = scope(repo, policy, **kw)
    from unittest.mock import patch

    from tauceti_worker.verification_scope import local_main
    from tauceti_worker.verification_scope import scope as helper_scope

    try:
        with patch.dict(os.environ, GIT_CONFIG_GLOBAL=kw.get("config") or "/dev/null", GIT_CONFIG_NOSYSTEM="1"):
            actual = helper_scope(Path(repo), local_main(Path(repo)), policy)
    except ValueError:
        actual = dict(mode="unknown", modules=[])
    check(
        f"{name}: helper matches V0 scope", actual["mode"] == kind and actual["modules"] == (listed or []), str(actual)
    )
    check(f"{name}: recipe exits 0", rc == 0, mode)
    check(f"{name}: mode {kind}", mode.startswith(kind), mode)
    if reason:
        check(f"{name}: reason mentions {reason!r}", reason in mode, mode)
    if modules is not None:
        check(f"{name}: modules {modules}", listed == modules, str(listed))


def main():
    with tempfile.TemporaryDirectory() as root:
        r = fixture(root, "committed")
        write(r, "TauCeti/A.lean", "changed\n")
        git(r, "commit", "-qam", "edit A")
        expect("committed edit", r, "scoped", ["TauCeti.A"])

        r = fixture(root, "staged")
        write(r, "TauCeti/B.lean", "changed\n")
        git(r, "add", "TauCeti/B.lean")
        expect("staged edit", r, "scoped", ["TauCeti.B"])

        r = fixture(root, "unstaged")
        write(r, "TauCeti/A.lean", "changed\n")
        expect("unstaged edit", r, "scoped", ["TauCeti.A"])

        r = fixture(root, "untracked")
        write(r, "TauCeti/New/C'.lean")
        expect("untracked new module", r, "scoped", ["TauCeti.New.C'"])

        r = fixture(root, "subdirectory")
        write(r, "TauCeti/Sub/X.lean")
        write(r, "TauCeti/Y.lean")
        write(r, "TauCeti/B.lean", "changed\n")
        relative = Path(root) / "relative.gitconfig"
        relative.write_text("[diff]\n\trelative = true\n")
        expect(
            "run from a subdirectory with diff.relative",
            r,
            "scoped",
            ["TauCeti.B", "TauCeti.Sub.X", "TauCeti.Y"],
            cwd=r / "TauCeti" / "Sub",
            config=str(relative),
        )

        r = fixture(root, "ignored")
        write(r, ".gitignore", "/.lake/\nscratch/\n")
        git(r, "commit", "-qam", "ignore scratch")
        git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
        write(r, "TauCeti/scratch/Hidden.lean")
        write(r, "scratch/notes.lean")
        expect("ignored untracked module under TauCeti/", r, "scoped", ["TauCeti.scratch.Hidden"])

        r = fixture(root, "mixed")
        write(r, "TauCeti/A.lean", "changed\n")
        git(r, "commit", "-qam", "edit A")
        write(r, "TauCeti/B.lean", "changed\n")
        git(r, "add", "-A")
        write(r, "TauCeti/Z.lean")
        expect("committed + staged + untracked", r, "scoped", ["TauCeti.A", "TauCeti.B", "TauCeti.Z"])

        r = fixture(root, "deleted")
        git(r, "rm", "-q", "TauCeti/Old.lean")
        expect("staged deletion", r, "full", [], "deleted module: TauCeti/Old.lean")

        r = fixture(root, "deleted-worktree")
        (r / "TauCeti/Old.lean").unlink()
        expect("working-tree deletion", r, "full", [], "deleted module")

        r = fixture(root, "renamed")
        git(r, "mv", "TauCeti/Old.lean", "TauCeti/New.lean")
        git(r, "commit", "-qm", "rename")
        expect("rename: both ends", r, "full", ["TauCeti.New"], "deleted module: TauCeti/Old.lean")

        for pin in ("lean-toolchain", "lake-manifest.json", "lakefile.toml"):
            r = fixture(root, f"pin-{pin}")
            write(r, pin, "dirty\n")
            write(r, "TauCeti/A.lean", "changed\n")
            expect(f"{pin} changed only in the working tree", r, "full", ["TauCeti.A"], pin)
        r = fixture(root, "pin-committed")
        write(r, "lean-toolchain", "bumped\n")
        git(r, "commit", "-qam", "bump")
        expect("committed pin change", r, "full", [], "lean-toolchain")
        r = fixture(root, "lakefile-lean")
        write(r, "lakefile.lean")
        expect("untracked lakefile.lean", r, "full", [], "lakefile.lean")

        r = fixture(root, "root")
        write(r, "TauCeti.lean", "changed\n")
        expect("root module", r, "full", [], "outside the module inventory: TauCeti.lean")

        r = fixture(root, "unicode")
        write(r, "TauCeti/Ünïcode.lean")
        write(r, "TauCeti/A.lean", "changed\n")
        expect("Unicode Lean path", r, "full", ["TauCeti.A"], "TauCeti/Ünïcode.lean")

        r = fixture(root, "newline")
        write(r, "TauCeti/a\nb.lean")
        expect("newline in a Lean path", r, "full", [], "outside the module inventory")

        r = fixture(root, "script-lean")
        write(r, "scripts/Axioms.lean")
        expect("Lean outside TauCeti/", r, "full", [], "scripts/Axioms.lean")

        r = fixture(root, "docs")
        write(r, "README.md", "changed\n")
        write(r, "docs/Ünïcode notes.md")
        expect("documentation only", r, "scoped", [])

        r = fixture(root, "nothing")
        expect("no change at all", r, "scoped", [])

        r = fixture(root, "main-advanced")
        write(r, "TauCeti/A.lean", "changed\n")
        git(r, "commit", "-qam", "edit A")
        git(r, "checkout", "-q", "-b", "upstream", "main")
        write(r, "TauCeti/B.lean", "upstream\n")
        write(r, "lean-toolchain", "upstream\n")
        git(r, "commit", "-qam", "main moves on")
        git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(r, "checkout", "-q", "feature")
        expect("main advanced past the merge base", r, "scoped", ["TauCeti.A"])

        r = fixture(root, "full-lint")
        write(r, "TauCeti/A.lean", "changed\n")
        for policy in ("full-lint", "lint-repair", "bump"):
            expect(f"{policy} policy", r, "full", ["TauCeti.A"], f"{policy} policy", policy=policy)

        # Bubble sandboxes route https://github.com/ through a local proxy with url.insteadOf, and add
        # an untracked SSH-form `github` remote; the canonical origin must still be recognised.
        r = fixture(root, "insteadof")
        git(r, "remote", "add", "github", "git@github.com:TauCetiProject/TauCeti.git")
        write(r, "TauCeti/A.lean", "changed\n")
        proxied = Path(root) / "proxy.gitconfig"
        proxied.write_text('[url "http://127.0.0.1:7777/git/"]\n\tinsteadOf = https://github.com/\n')
        expect("sandbox insteadOf proxy", r, "scoped", ["TauCeti.A"], config=str(proxied))

        r = fixture(root, "ssh-remote", remote="git@github.com:TauCetiProject/TauCeti.git")
        write(r, "TauCeti/A.lean", "changed\n")
        expect("ssh canonical remote", r, "scoped", ["TauCeti.A"])

        r = fixture(root, "no-main", track_main=False)
        write(r, "TauCeti/A.lean", "changed\n")
        expect("missing canonical main ref", r, "unknown", [], "no local canonical")

        for bad in (
            "https://notgithub.com/TauCetiProject/TauCeti.git",
            "/tmp/github.com/TauCetiProject/TauCeti",
            "https://github.com/TauCetiProject/TauCetiWorker.git",
        ):
            r = fixture(root, "lookalike-" + re.sub(r"\W", "_", bad), remote=bad)
            write(r, "TauCeti/A.lean", "changed\n")
            expect(f"lookalike remote {bad}", r, "unknown", [], "no local canonical")

        r = fixture(root, "fork-only", remote="https://github.com/alice/TauCeti.git")
        write(r, "TauCeti/A.lean", "changed\n")
        expect("only a fork remote", r, "unknown", [], "no local canonical")

        r = fixture(root, "unrelated")
        git(r, "checkout", "-q", "--orphan", "orphan")
        git(r, "commit", "-qm", "unrelated")
        expect("no merge base", r, "unknown", [], "no merge base")

        src = fixture(root, "shallow-src")
        shallow = Path(root) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(shallow)], check=True)
        git(shallow, "remote", "set-url", "origin", CANONICAL)
        git(shallow, "update-ref", "refs/remotes/origin/main", "HEAD")
        expect("shallow history", shallow, "unknown", [], "shallow")

        # The audit and lint blocks consume the recipe's files with a stand-in lake.
        bin_dir = Path(root) / "bin"
        bin_dir.mkdir()
        log = Path(root) / "calls.log"
        stub = bin_dir / "lake"
        stub.write_text(f'#!/bin/sh\necho "lake $* AXIOMS=${{AXIOMS_ONLY_MODULES-unset}}" >> {log}\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        local_stub = bin_dir / "local-checker"
        local_stub.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            f"Path({str(log)!r}).write_text('local-checker ' + ' '.join(sys.argv[1:]) + "
            "' AXIOMS=' + os.environ.get('AXIOMS_ONLY_MODULES', 'unset'))\n"
        )
        # An inherited scope must never narrow a full audit.
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", AXIOMS_ONLY_MODULES="/stale/list")
        _, audit, lint = blocks("ordinary")
        audit = audit.replace(str(tc.paths.HERE / "scripts/tauceti-local-axioms"), str(local_stub))

        def audit_call(repo):
            log.write_text("")
            rc = subprocess.run(["bash", "-c", audit], cwd=repo, env=env, capture_output=True).returncode
            return rc, log.read_text().strip()

        r = Path(root) / "committed"
        scope(r)
        rc, call = audit_call(r)
        listed = r / ".lake" / "tauceti-scope" / "modules.txt"
        check(
            "scoped audit passes an absolute module list",
            rc == 0 and "local-checker --modules" in call and str(listed) in call and "exe axioms" not in call,
            call,
        )
        check("module list path is absolute", listed.is_absolute())
        r = Path(root) / "docs"
        scope(r)
        rc, call = audit_call(r)
        check(
            "empty scope uses the cheap local checker",
            "local-checker --modules" in call and "exe axioms" not in call,
            call,
        )
        r = Path(root) / "deleted"
        scope(r)
        rc, call = audit_call(r)
        check(
            "full CI scope still uses changed-file diagnostics",
            rc == 0 and "local-checker --modules" in call and call.endswith("AXIOMS="),
            call,
        )
        r = Path(root) / "no-main"
        scope(r)
        rc, call = audit_call(r)
        check("unknown scope fails without any full-library fallback", rc != 0 and call == "", call)
        lint_run = subprocess.run(["bash", "-c", lint], cwd=r, env=env, capture_output=True, text=True)
        check("unknown scope skips the local lint", "CI runs the environment lint" in lint_run.stdout)
        r = fixture(root, "never-scoped")
        rc, _ = audit_call(r)
        check("audit without a computed scope fails", rc != 0)
        r = Path(root) / "docs"
        scope(r)
        lint_run = subprocess.run(["bash", "-c", lint], cwd=r, env=env, capture_output=True, text=True)
        check("empty scope skips lint-env", "no changed TauCeti module" in lint_run.stdout, lint_run.stdout)

    # The canary helper path is explicitly opted in; V0 remains the dispatch default.
    os.environ["TAUCETI_VERIFY_HELPER"] = "1"
    # Every authoring/repair prompt, rendered as the worker renders it, carries the same rules.
    sys.path.insert(0, str(HERE))
    from prompt_wrapper_paths import CALL_SITES

    for name in ("roadmap.md", "fix.md", "fix-ci.md", "rebase.md", "bump.md", "lint-repair.md", "overlap-fix.md"):
        subs = dict(CALL_SITES[name])
        policy = subs.get("SCOPE_POLICY", "ordinary")
        subs["LOCAL_VERIFICATION"] = tc.agents.local_verification(policy)
        text = tc.agents.fill_prompt(tc.paths.HERE / "prompts" / name, **subs)
        check(f"{name}: dispatches to the absolute helper", '/tauceti-verify"' in text)
        check(f"{name}: names its policy", f"policy is `{policy}`" in text)
        check(f"{name}: forbids name-based waits", "`pgrep`" in text and "notify or resume you" in text)
        check(f"{name}: requires pipefail", "set -o pipefail" in text)
        check(f"{name}: avoids GitHub scope reads", "`scripts/lint-scope.sh` alongside the helper" in text)
        check(f"{name}: report separates local checks from CI", "not full CI" in text)
        check(f"{name}: no unscoped whole-suite rerun", "run the WHOLE suite" not in text)
        check(f"{name}: no bare unscoped audit command block", "\nlake exe axioms\n" not in text)
        check(f"{name}: no stale DuplicateDeclarations script", "DuplicateDeclarations.lean" not in text)

    # Policy comes from surveyed labels and branch only.
    sp = tc.agents.scope_policy
    check("ordinary PR", sp(("roadmap/Foo",), "feature") == "ordinary")
    check("full-lint label", sp(("Full-Lint",), "feature") == "full-lint")
    check("comma-delimited full-lint label, as CI matches it", sp(("foo,full-lint",), "feature") == "full-lint")
    check("full-lint is not a substring match", sp(("not-full-lint",), "feature") == "ordinary")
    check("lint-repair branch", sp((), "lint-repair/main") == "lint-repair")
    check("bump", sp((), "bump/x", bump=True) == "bump")
    try:
        tc.agents.local_verification("whatever")
        check("unknown policy is rejected", False)
    except tc.config.Die:
        check("unknown policy is rejected", True)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
