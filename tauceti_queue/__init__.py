"""tauceti_queue: the AI merge queue service for TauCetiProject/TauCeti.

This package is being assembled in increments (see docs/redesign/specs/ai-merge-queue.md on the
`overhaul` branch). The first module is `chain`, the attributed per-PR squash chain tool that the
historical repair experiments and, later, the controller use to build, replay, validate and compare
batch chains. Everything here is stdlib plus `git` subprocesses.
"""
