"""Bounded excerpts of subprocess diagnostics, with no worker imports so any module can use them."""

from __future__ import annotations

MARK = " … "


def ends(text: str | bytes | None, limit: int = 200) -> str:
    """`text` stripped and, past `limit` characters, cut to both ends around " … ".

    A diagnostic's start often names the failure (a configuration error, the failing command) and
    its end the last detail; keeping only the tail lost the first and left fragments like "i.toml".
    Bytes, which a timed-out subprocess can return even in text mode, are decoded leniently."""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit < len(MARK) + 2:
        return text[: max(limit, 0)]
    keep = limit - len(MARK)
    head = keep // 2
    return text[:head].rstrip() + MARK + text[len(text) - (keep - head) :].lstrip()
