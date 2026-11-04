"""tauceti_queue: the AI merge queue service for TauCetiProject/TauCeti.

This package is being assembled in increments (see docs/redesign/specs/ai-merge-queue.md on the
`overhaul` branch). Modules:

- `chain`: the attributed per-PR squash chain tool (build, replay, validate, compare).
- `ports`: the `GitHubPort` protocol and the records the controller exchanges with adapters and workers.
- `store`: the queue's records in one SQLite database.
- `controller`: the increment 1 state machine (inputs, batches, claims, tasks, CI, promotion, status).
- `loop`: the reconciliation loop with restart reconciliation and bounded polling.
- `fakes`: a fake GitHub over a bare repository, scripted CI and scripted agents for the fixture.

Everything here is stdlib plus `git` subprocesses.
"""
