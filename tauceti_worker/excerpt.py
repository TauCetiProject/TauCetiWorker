"""Bounded excerpts of subprocess diagnostics, with no worker imports so any module can use them."""

from __future__ import annotations


def ends(text: str | None, limit: int = 200) -> str:
    """`text` stripped and, past `limit` characters, cut to both ends around " … ".

    A diagnostic's start often names the failure (a configuration error, the failing command) and
    its end the last detail; keeping only the tail lost the first and left fragments like "i.toml"."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keep = max(limit - 3, 2)
    head = keep // 2
    return text[:head].rstrip() + " … " + text[len(text) - (keep - head) :].lstrip()
