# Inside the sandbox

`tauceti work --bubble` runs code and review agents inside a
[Bubble](https://github.com/kim-em/bubble) container instead of on the host.
Progress-report rounds always run on the host. This page describes what the
container boundary enforces; the README covers when to use it.

## What happens in the container

The outer worker surveys GitHub, selects a task, and coordinates claims from the
host. Once it selects a Bubble-eligible task, the task checkout, the agent, and
the agent's git and gh commands run inside the container. Code-writing rounds
also run their cache downloads and advisory `lake build` there.

- GitHub traffic goes through Bubble's auth proxy. It permits access to
  `TauCetiProject/TauCeti` and, for authoring and fixes, git push access to the
  configured fork. Other repository access is rejected by the proxy. Your `gh`
  token never enters the container.
- Only the one credential the agent needs is seeded. The other models'
  credentials, and all of your host config (`CLAUDE.md`, skills), stay out.
- Review runs the `tauceti-review` engine inside the container too, offline. The
  engine, the roadmap, and the review store are mounted in, and it runs on the
  image's `python3` with no PyPI or cross-repo fetch. The only traffic crossing
  the proxy is the TauCeti clone, the PR API, and the scoreboard post.

That isolation matters most for review, where the agent reads untrusted PRs.

## Lake caches

Before the work agent starts, the worker fetches Mathlib's prebuilt outputs with
`lake exe cache get`, fetches TauCeti's own main-built outputs with
`lake cache get`, and runs an advisory `lake build`. A red tree still reaches the
repair agent.

Bubble routes both download-only caches through its host-global proxy, so
TauCeti's public R2 host is never reachable from inside the container. The exact
revision and content-addressed GET routes are shared across rounds; containers
cannot select another origin or upload, and their writable Lake views are
discarded when the container is popped.

## Requirements

Bubble needs a working [Incus](https://linuxcontainers.org/incus/) runtime.
Kiro rounds require Bubble 0.7.31 or newer; existing agents require 0.7.30.
Real sandbox rounds need a stable
installed executable because Bubble owns a host-global auth daemon. For dry-run
capability checks only, `tauceti` can fetch it with `uvx`. `tauceti doctor`
reports whether the Bubble and Incus executables are present; the round preflight
checks the Bubble version and capabilities. `TAUCETI_BUBBLE` overrides the
executable, and `TAUCETI_BUBBLE_HOME` overrides the private Bubble home.

## OpenRouter agents

`--agent deepseek|minimax` runs in the bubble too. The image ships
[`pi`](https://github.com/badlogic/pi-mono) and allows openrouter.ai egress
([kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299)), and
`OPENROUTER_API_KEY` is staged read-only into the container.

## Kiro

`--agent kiro` is available in Bubble with the same exact-model entitlement
check as host mode. Bubble installs the checksum-pinned native Kiro CLI and
allows only Kiro's documented authentication, runtime, management, telemetry,
and download domains. TauCeti snapshots either the selected `KIRO_API_KEY` or
the browser-login SQLite database into the read-only round mount; Kiro receives
a private, writable copy inside the disposable container. Other provider
credentials do not cross the boundary.

## macOS Claude credentials

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file, but bubble seeds the in-container `claude` from a `.credentials.json`.
So `tauceti` copies the current credential out of the Keychain into a private,
transient config directory used only by the bubble subprocess. The Keychain is
read, never written; the first round unlocks it interactively if it is locked.
The directory is removed after the bubble exits, or before the next round if the
worker was hard-killed. Your `$CLAUDE_CONFIG_DIR` (or `~/.claude`) is never
created or overwritten.

Worker versions before this private handoff may already have left a
`.credentials.json` snapshot in your configured Claude directory. This version
does not delete that file because it may be operator-owned. If host subscription
reviews fail with a 401 while an interactive `claude` still works, move that old
file aside once so the review can fall back to the live Keychain. A headless
worker whose Keychain cannot be unlocked still needs a file fallback: point
`CLAUDE_CONFIG_DIR` at a dedicated directory holding a current
`.credentials.json` instead of moving away its only credential source.

## Claude API billing

With `--claude-billing api`, Bubble receives a private read-only API key file
and a Claude shim. The host bridge controls admission and records costs through
a read-write requests/receipts mount and a separate read-only replies/heartbeat
mount. The host shim, host key file, and budget ledger are not mounted. Subscription
credentials are not seeded. Keys are removed after the round and on normal
signal cleanup. SIGKILL may leave private staging files requiring cleanup, as
with other abruptly interrupted filesystem work.

The same shim intercepts review-engine Claude calls even when the engine
constructs a clean subprocess environment. The review engine runs in API
authentication mode. Local grants never terminate an admitted session.
Costs reported inside the sandbox are self-reported accounting estimates. The
mailbox prevents access to host credential files and the ledger; it is not an
independent attestation of the container's spending. After a crash, use
`tauceti budget --recover --note TEXT` to recover durable receipts and remove
stale keys for recorded sessions.
