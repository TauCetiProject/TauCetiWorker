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
