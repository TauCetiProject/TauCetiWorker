#!/usr/bin/env python3
"""Host rounds may opt in to the gilgai build-server shim (TAUCETI_GILGAI=1). The shim must sit first on
the agent's PATH with its server, token, repository and a wait budget under the shell cap; without the
opt-in, or with any piece of it missing, the environment must be exactly what it was, so builds stay
local. Exit 0 = every case agrees; 1 = a mismatch."""

import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


with tempfile.TemporaryDirectory() as tmp:
    shim = Path(tmp) / "shim"
    shim.mkdir()
    (shim / "lake").write_text("#!/bin/sh\nexit 0\n")
    (shim / "lake").chmod(stat.S_IRWXU)
    token = Path(tmp) / "token"
    token.write_text("secret-token\n")
    base = {"PATH": "/usr/bin:/bin", "BASH_MAX_TIMEOUT_MS": "600000"}

    check("no opt-in changes nothing", agents.gilgai_shim_env(dict(base)), {})

    env = {
        **base,
        "TAUCETI_GILGAI": "1",
        "TAUCETI_GILGAI_SHIM_DIR": str(shim),
        "TAUCETI_GILGAI_URL": "https://build.example",
        "TAUCETI_GILGAI_TOKEN_FILE": str(token),
    }
    out = agents.gilgai_shim_env(dict(env), Path(tmp) / "logs")
    check("shim first on PATH", out.get("PATH"), f"{shim}:/usr/bin:/bin")
    check("url", out.get("GILGAI_URL"), "https://build.example")
    check("token stripped", out.get("GILGAI_TOKEN"), "secret-token")
    check("repo default", out.get("GILGAI_REPO"), "TauCetiProject/TauCeti")
    check("base ref default", out.get("GILGAI_BASE_REF"), "origin/main")
    check("fallback default", out.get("GILGAI_FALLBACK"), "local")
    check("wait under the shell cap", out.get("GILGAI_WAIT_SEC"), "560")
    check("log file in logdir", out.get("GILGAI_LOG"), str(Path(tmp) / "logs" / "gilgai-shim.log"))

    short = agents.gilgai_shim_env({**env, "BASH_MAX_TIMEOUT_MS": "120000"})
    check("wait derived from a shorter cap", short.get("GILGAI_WAIT_SEC"), "100")
    check("no logdir, no log var", "GILGAI_LOG" in short, False)

    for missing in ("TAUCETI_GILGAI_SHIM_DIR", "TAUCETI_GILGAI_URL", "TAUCETI_GILGAI_TOKEN_FILE"):
        broken = {k: v for k, v in env.items() if k != missing}
        check(f"missing {missing} leaves env untouched", agents.gilgai_shim_env(broken), {})
    check(
        "shim dir without lake leaves env untouched",
        agents.gilgai_shim_env({**env, "TAUCETI_GILGAI_SHIM_DIR": tmp}),
        {},
    )

    # the real entry point threads it through
    os.environ.update(env)
    try:
        argv, full = agents.host_agent_argv("prompt", "claude", Path(tmp) / "logs")
        check("host_agent_argv puts the shim ahead of the scripts dir", full["PATH"].split(":")[0], str(shim))
        check("host_agent_argv keeps the scripts dir", str(agents.HERE / "scripts") in full["PATH"].split(":"), True)
    finally:
        for k in env:
            os.environ.pop(k, None)

print("all ok" if not fails else f"{fails} failures")
sys.exit(1 if fails else 0)
