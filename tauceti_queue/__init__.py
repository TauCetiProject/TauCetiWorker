"""tauceti_queue: the AI merge queue service for TauCetiProject/TauCeti.

This package is being assembled in increments (see docs/redesign/specs/ai-merge-queue.md on the
`overhaul` branch). `chain` is the attributed per-PR squash chain tool that the historical repair
experiments and, later, the controller use to build, replay, validate and compare batch chains;
`repair_experiment` is the increment 0 harness that runs the repair agent over past failed staging
candidates. Everything here is stdlib plus `git` subprocesses and the worker's own modules.
"""
