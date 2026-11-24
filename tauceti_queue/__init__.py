"""tauceti_queue: the AI merge queue service for TauCetiProject/TauCeti.

This package is being assembled in increments (see docs/redesign/specs/ai-merge-queue.md on the
`overhaul` branch). Modules:

- `chain`: the attributed per-PR squash chain tool (build, replay, validate, compare).
- `ports`: the `GitHubPort` protocol and the records the controller exchanges with adapters and workers.
- `store`: the queue's records in one SQLite database.
- `controller`: the increment 1 state machine (inputs, batches, claims, tasks, CI, promotion, status).
- `loop`: the reconciliation loop with restart reconciliation and bounded polling.
- `bors_import`: the cutover import of Bors's approved PRs and running head from its feeds.
- `approval_poll`: the bounded reconciliation poll of `merge eligibility` checks behind the webhooks.
- `fakes`: a fake GitHub over a bare repository, scripted CI and scripted agents for the fixture.
- `repair`: the repair machinery shared by the experiments and the worker-side task (artifact
  preparation, prompt, agent run, chain checks).
- `repair_experiment`: the increment 0 harness that runs the repair agent over past failed staging
  candidates.
- `repair_task`: the worker-side task that claims a repair or rebase-conflict task from the service,
  runs the agent and returns a validated chain.

Everything here is stdlib plus `git` subprocesses; the worker-side modules also use the worker's own
`tauceti_worker` harness.
"""

_RUST_MIN_STACK = 256 * 1024 * 1024


def _raise_stack_limit() -> None:
    # leantar 0.1.21 (Lean v4.37.0-rc1) overflows the default 8 MiB stack on a large olean (exit 139).
    # Every process of this package raises its soft stack limit to the hard limit on import, so the
    # Lake builds, cache staging and restores it starts inherit it, whatever its entry point.
    # RLIMIT_STACK sizes only the main thread: leantar unpacks a multi-archive batch on Rust threads,
    # whose stacks default to 2 MiB whatever the rlimit, and abort with "has overflowed its stack".
    # RUST_MIN_STACK sizes those; it reserves address space, and only touched pages use memory.
    import os

    os.environ.setdefault("RUST_MIN_STACK", str(_RUST_MIN_STACK))
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
        if soft != hard:
            resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))
    except (ImportError, OSError, ValueError):
        pass


_raise_stack_limit()
