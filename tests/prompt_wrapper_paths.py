#!/usr/bin/env python3
"""The write wrappers are named by absolute path in every prompt that pushes.

PATH is not a reliable channel to a codex round. Codex runs its shell as `bash -lc`, and a login
shell re-runs the system profile; on NixOS /etc/profile REPLACES $PATH unless $__ETC_PROFILE_SOURCED
arrives with the environment, so the `HERE/scripts` prefix host_agent_argv exports is dropped. The
field evidence: `git-safe-push: command not found` on 131 of 1525 codex transcripts and none of 1161
claude ones (claude uses a non-login shell); 120 of those rounds burned turns locating the wrapper
after a green build, and 11 never pushed at all. The prompts therefore spell the wrapper as
`__BIN__/git-safe-push`, which no shell startup file can take away.

This harness pins the four properties that keep that true:

  1. `wrapper_bin()` names the staged directory on the host and `/opt/round` in bubble — the two
     places run_in_bubble copies the wrappers to.
  2. Every prompt that invokes a wrapper invokes it through `__BIN__`, never bare.
  3. Every bundled prompt renders with no placeholder left, using exactly the keys its call site
     passes — a leaked `__BIN__/git-safe-push` is the very failure being fixed.
  4. The bump and CI-repair prompts run the expired-shim gate and tell the worker how to migrate.
  5. `fill_prompt` raises on an unfilled placeholder in a bundled prompt, and stays quiet for a
     prompt served from elsewhere (TauCetiProgress owns the progress prompt's placeholder set).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fill_prompt = tc.agents.fill_prompt
wrapper_bin = tc.agents.wrapper_bin
PROMPTS = tc.paths.HERE / "prompts"
WRAPPERS = ("git-safe-push", "gh-safe-pr-create", "claim.sh")

# The keys each call site passes, so a prompt that grows a placeholder without a substitution fails
# here rather than reaching an agent. Mirrors work_units._do_fixlike / do_roadmap.
CALL_SITES = {
    "fix.md": dict(PR=123, AGENT="Claude Code", BIN="/bin"),
    "fix-ci.md": dict(PR=123, AGENT="Claude Code", BIN="/bin"),
    "rebase.md": dict(PR=123, AGENT="Claude Code", BIN="/bin"),
    "bump.md": dict(PR=123, AGENT="Claude Code", BIN="/bin"),
    "lint-repair.md": dict(PR=123, AGENT="Claude Code", BIN="/bin"),
    "roadmap.md": dict(
        ONLY="CFSGStatement",
        SKIP="none",
        CLAIMED="none",
        AGENT="Claude Code",
        FORK="alice",
        WORKERID="worker5",
        ROADMAP_DIR="/opt/roadmap/TauCetiRoadmap",
        REVIEW_DIR="/opt/review",
        RUBRICS="/opt/rubrics/rubrics.md",
        SOURCE_GUIDANCE="",
        BIN="/bin",
    ),
}

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def main():
    # 1) The two staging locations.
    check("host wrapper_bin is the shipped scripts dir", wrapper_bin() == str(tc.paths.HERE / "scripts"))
    check("bubble wrapper_bin is the read-only mount", wrapper_bin(bubble=True) == "/opt/round")
    for w in WRAPPERS:
        check(f"{w} is staged where wrapper_bin points", (Path(wrapper_bin()) / w).is_file())

    # 2) No bare wrapper invocation survives in any prompt. A bare name in prose ("only the
    # wrapper", "raw `git push`") is fine; what must not appear is a command line STARTING with the
    # wrapper name, which is what a shell would resolve through PATH.
    for p in sorted(PROMPTS.glob("*.md")):
        text = p.read_text()
        bare = [
            line.strip()
            for line in text.splitlines()
            if any(re.match(rf"^\s*\"?{re.escape(w)}\b", line) for w in WRAPPERS)
        ]
        check(f"{p.name}: no bare wrapper invocation", not bare)
        for w in WRAPPERS:
            if w in text:
                check(f"{p.name}: {w} is invoked through a quoted __BIN__", f'"__BIN__/{w}"' in text)

    # 3) Every bundled prompt renders clean with its call site's keys, and the wrapper lines come
    # out as absolute paths.
    for name, subs in CALL_SITES.items():
        out = fill_prompt(PROMPTS / name, **subs)
        left = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", out)))
        check(f"{name}: renders with no placeholder left", not left)
        invocations = [ln.strip() for ln in out.splitlines() if any(f"/{w}" in ln for w in WRAPPERS)]
        check(f"{name}: at least one wrapper invocation", bool(invocations))
        # Quoted, so an install directory containing a space stays one word.
        check(
            f"{name}: every wrapper invocation is absolute and quoted",
            all(ln.startswith(f'"{subs["BIN"]}/') for ln in invocations),
        )
    check("every bundled prompt has a call site", {p.name for p in PROMPTS.glob("*.md")} == set(CALL_SITES))

    # 4) Shim expiry is an autonomous repair input, not a notification-only dead end. Both workers
    # reproduce and verify the gate, and both know the registry is part of the source-only fix.
    shim_command = "python3 scripts/check-expired-mathlib-shims.py"
    for name in ("bump.md", "fix-ci.md"):
        prompt = (PROMPTS / name).read_text()
        check(f"{name}: reproduces and verifies shim expiry", prompt.count(shim_command) == 2)
        check(f"{name}: uses strict shim expiry mode", prompt.count("--fail-on-available") == 2)
        check(f"{name}: validates the base registry ratchet", prompt.count("--base-manifest") == 2)
        check(f"{name}: refreshes the base ref", prompt.count("git fetch -q origin main") == 2)
        check(f"{name}: migrates the AI-owned registry", "TauCeti/mathlib-shims.json" in prompt)
        check(f"{name}: forbids weakening exact targets", "never make the check green" in prompt)
    check(
        "fix-ci.md: source PRs probe only changed obligations",
        (PROMPTS / "fix-ci.md").read_text().count("--only-new") == 2,
    )
    for name in ("bump.md", "fix-ci.md"):
        prompt = (PROMPTS / name).read_text()
        check(
            f"{name}: shim baseline is the PR merge base",
            prompt.count('git show "$base_ref":TauCeti/mathlib-shims.json') == 2,
        )
        check(
            f"{name}: shim ratchet reads merge-base sources",
            prompt.count('--base-root "$base_root"') == 2,
        )

    # 5) Substituted VALUES are not templates and are not validated. `__CLAIMED__` carries text
    # copied from other contributors' intention issues, which is untrusted and documented as
    # fail-open: a claim containing a `__WORD__` must not abort the round, and one containing a real
    # placeholder must not be rewritten by a later substitution.
    hostile = dict(CALL_SITES["roadmap.md"], CLAIMED="avoid the __FOO__ lemma and __BIN__ helpers")
    try:
        out = fill_prompt(PROMPTS / "roadmap.md", **hostile)
        check("an untrusted claim containing __WORD__ does not abort the round", True)
        check("that claim survives verbatim", "avoid the __FOO__ lemma and __BIN__ helpers" in out)
        check("a claim's __BIN__ is not rewritten", out.count(hostile["BIN"] + "/git-safe-push") == 1)
    except tc.config.Die:
        check("an untrusted claim containing __WORD__ does not abort the round", False)
    # A value carrying a regex replacement escape is taken literally.
    esc = dict(CALL_SITES["roadmap.md"], CLAIMED=r"\g<0> and \1 and \\")
    check("a claim with regex escapes is literal", r"\g<0> and \1 and \\" in fill_prompt(PROMPTS / "roadmap.md", **esc))

    # 6) The guard fires for our prompts and stays out of the way for a foreign one.
    try:
        fill_prompt(PROMPTS / "fix.md", PR=1, AGENT="x")  # BIN omitted
        check("fill_prompt rejects an unfilled bundled placeholder", False)
    except tc.config.Die as e:
        check("fill_prompt rejects an unfilled bundled placeholder", "__BIN__" in str(e))
    with tempfile.TemporaryDirectory() as d:
        foreign = Path(d) / "progress.md"
        foreign.write_text("write __STATUS_OUT__ from __FACTS_FILE__\n")
        try:
            out = fill_prompt(foreign, STATUS_OUT="/tmp/s")
            check("fill_prompt leaves a foreign prompt's placeholders alone", "__FACTS_FILE__" in out)
        except tc.config.Die:
            check("fill_prompt leaves a foreign prompt's placeholders alone", False)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
