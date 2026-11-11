"""Compatibility entrypoints for source operations and shared review recovery."""

from __future__ import annotations

from types import SimpleNamespace


def reply_to_finding(source, number, parent, body):
    from tauceti_tools.review_source import reply_to_finding as reply

    return reply(source.api, source.repository, number, parent, body)


def pending(source, cfg, *, numbers=()):
    from .review_runtime import recover

    return recover(SimpleNamespace(cfg=cfg, gh=source), numbers=numbers)
