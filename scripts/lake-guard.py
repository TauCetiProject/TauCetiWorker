"""The shared-Mathlib-view half of the agent's `lake` wrapper (see scripts/lake).

argv: REAL_LAKE WRAPPER_DIR LAKE_ARGS...

Refuses every spelling of Mathlib's archive extraction (`lake [opts] exe|exec [opts]
[pkg/]cache [opts] get|get!|unpack|unpack!`): if the workspace's Mathlib pin still matches the
shared entry there is nothing to fetch (exit 0); if it has moved, building would compile Mathlib
privately, so the agent must stop and report (exit 1). `lake env CMD...` would put the
toolchain's own `lake` first on CMD's PATH, so CMD runs with this wrapper restored in front.
Everything else runs the real `lake` unchanged. This is a guard against accidental private
copies, not the enforcement boundary: the per-round quota is.
"""

import json
import os
import sys
from pathlib import Path

EXTRACT = {"get", "get!", "unpack", "unpack!"}
# Lake (v4.36) options that consume the next argument; `--opt=value` forms are self-contained.
WITH_VALUE = {
    *("-d", "-f", "-o", "-K", "--dir", "--file", "--package", "--service", "--scope", "--repo"),
    *("--platform", "--toolchain", "--rev", "--max-revs", "--log-level", "--fail-level"),
    *("--packages", "--linters", "--lint-only", "--checks", "--only", "--add-downstream"),
    *("--add-public-downstream", "--challenge-from-export", "--solution-from-export"),
    *("--config", "--from-export"),
}


def parse(args):
    """(workspace, positions of the non-option, non-empty arguments) for Lake's argv.

    This errs towards seeing more words than Lake does: an unknown value-taking option makes
    its value a word, which can only make extraction detection (a subsequence test) stricter."""
    workspace, words, i = ".", [], 0
    while i < len(args):
        arg = args[i]
        if not arg or (i == 0 and arg.startswith("+")):  # Lake skips empties; elan's +toolchain
            pass
        elif arg in ("-d", "--dir") and i + 1 < len(args):
            workspace = args[i + 1]
            i += 1
        elif arg.startswith(("--dir=", "-d=")):
            workspace = arg.split("=", 1)[1]
        elif arg.startswith("-d") and not arg.startswith("--"):
            workspace = arg[2:].strip()
        elif arg in WITH_VALUE:
            i += 1
        elif arg == "--":
            words.extend(j for j in range(i + 1, len(args)) if args[j])
            break
        elif not arg.startswith("-"):
            words.append(i)
        i += 1
    return Path(workspace), words


def extraction(command):
    """The extraction subcommand if `exe|exec`, `[pkg/]cache`, `get|get!|unpack|unpack!`
    appear in that order anywhere among the words, else None."""
    for i, word in enumerate(command):
        if word in ("exe", "exec") and i + 2 < len(command) and command[i + 1].rsplit("/", 1)[-1] == "cache":
            if command[i + 2] in EXTRACT:
                return command[i + 2]
    return None


def pin_matches(workspace):
    try:
        marker = workspace / ".lake/packages/mathlib/.lake/tauceti-shared-mathlib.json"
        marker = json.loads(marker.read_text())
        ready = json.loads((Path(marker["pool"]) / "entries" / marker["key"] / "ready.json").read_text())
        packages = json.loads((workspace / "lake-manifest.json").read_text()).get("packages", [])
        rev = next(p.get("rev") for p in packages if p.get("name") == "mathlib")
        return rev == ready["spec"]["mathlib"]
    except Exception:
        return False


def main(real, wrapper, args):
    workspace, words = parse(args)
    command = [args[i] for i in words]
    if sub := extraction(command):
        if pin_matches(workspace):
            print(
                f"lake exe cache {sub}: Mathlib is provided by this host's shared build; nothing to fetch.",
                file=sys.stderr,
            )
            return 0
        print(
            f"lake exe cache {sub}: this checkout's Mathlib pin no longer matches the host's shared build.",
            file=sys.stderr,
        )
        print("Do not build or fetch Mathlib here. Stop and report that the Mathlib pin changed.", file=sys.stderr)
        return 1
    if command and command[0] == "env" and len(words) > 1:
        cut = words[1]
        shim = 'PATH="$0:$PATH"; export PATH; exec "$@"'
        # Lake ignores empty leading arguments; dropping them keeps CMD the shim's first word.
        os.execv(real, [real, *(a for a in args[:cut] if a), "/bin/sh", "-c", shim, wrapper, *args[cut:]])
    os.execv(real, [real, *args])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
