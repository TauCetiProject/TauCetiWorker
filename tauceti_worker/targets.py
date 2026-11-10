"""Operator target lists for roadmap authoring (`--roadmap-targets FILE`).

The file format and the selection rules are documented in docs/target-lists.md. What the rest of
the worker uses:

    load_targets(path)                 read and parse the file; Die when it is not a target list
    live_targets(targets, gh)          statuses overlaid from open and merged pull request markers
    candidates(targets, only, skip)    the eligible items, in the order a worker tries to claim them
    landed_pr(item, gh)                the merged pull request that completed an item, at any age
    render_assignment(targets, item)   the roadmap prompt's bullet assigning one item
    marker_stem(item)                  what the pull request body must contain for that item

`parse_targets`, `marker_keys`, `landed`, `overlay_live`, `eligible_items` and `eligible_areas`
are the pure pieces those are built from; none of them reads GitHub or the environment.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Die, NoProgress, log
from .survey import TARGET_MARKER_RE

MARKER_RE = re.compile(r"<!--\s*tauceti-targets:v1\s*-->")
MERGED_SEARCH = '"tauceti-target:v1" in:body'
MERGED_LIMIT = 200
_AREA_RE = re.compile(r"[A-Za-z0-9_-]+")
_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_ITEM_RE = re.compile(r"^\s*-\s*\[([ xX~])\]\s*(.*?)\s*$")
_SLUG_RE = re.compile(r"`([^`]+)`")
_META_RE = re.compile(r"\(([^()]*)\)\s*$")
_STATUS = {" ": "open", "~": "inflight", "x": "done", "X": "done"}
_BOX = {"open": "[ ]", "inflight": "[~]", "done": "[x]"}

Key = tuple[str, str]  # (area, slug): what a pull request's target marker names


@dataclass(frozen=True)
class TargetItem:
    area: str
    slug: str
    status: str  # "open" | "inflight" | "done"
    text: str
    needs: tuple[str, ...] = ()  # slugs, looked up across every area
    meta: tuple[str, ...] = ()  # the other `key: value` clauses, verbatim


@dataclass(frozen=True)
class Targets:
    preamble: str
    areas: dict[str, list[TargetItem]]  # areas and their items in file order

    def items(self) -> Iterable[TargetItem]:
        return (it for items in self.areas.values() for it in items)

    def find(self, slug: str) -> TargetItem | None:
        return next((it for it in self.items() if it.slug == slug), None)


def _parse_item(line: str, area: str) -> TargetItem | None:
    m = _ITEM_RE.match(line)
    slug = _SLUG_RE.search(m.group(2)) if m else None
    if slug is None:
        return None
    text = m.group(2)[slug.end() :]
    sep = re.match(r"\s*[—–-]\s+", text)
    text = text[sep.end() :] if sep else text.strip()
    needs: tuple[str, ...] = ()
    meta = []
    clauses = _META_RE.search(text)
    if clauses:
        text = text[: clauses.start()].rstrip()
        for clause in filter(None, (c.strip() for c in clauses.group(1).split(";"))):
            key, _, value = clause.partition(":")
            if key.strip().lower() != "needs":
                meta.append(clause)
            elif value.strip().lower() != "none":
                needs = tuple(s.strip() for s in _SLUG_RE.findall(value) or value.split(",") if s.strip())
    return TargetItem(area, slug.group(1).strip(), _STATUS[m.group(1)], text, needs, tuple(meta))


def parse_targets(text: str) -> Targets:
    """Parse a target list. Die when the `tauceti-targets:v1` marker is missing; an item line that
    does not parse, or a `needs:` slug the file never defines, is logged and otherwise ignored."""
    if not MARKER_RE.search(text):
        raise Die("the file lacks the `<!-- tauceti-targets:v1 -->` marker, so it is not a target list")
    preamble: list[str] = []
    areas: dict[str, list[TargetItem]] = {}
    area = None
    started = False
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        heading = _HEADING_RE.match(line)
        if heading:
            started = True
            area = heading.group(1) if _AREA_RE.fullmatch(heading.group(1)) else None
            if area is not None:
                areas.setdefault(area, [])
        elif not started:
            if not (MARKER_RE.search(line) or line.startswith("# ")):
                preamble.append(line.rstrip())
        elif area is not None and re.match(r"\s*-\s*\[", line):
            item = _parse_item(line, area)
            if item is None:
                bad.append(n)
            else:
                areas[area].append(item)
    targets = Targets("\n".join(preamble).strip(), areas)
    if bad:
        log(f"target list: ignoring {len(bad)} malformed item line(s), first at line {bad[0]}")
    undefined = {s for it in targets.items() for s in it.needs} - {it.slug for it in targets.items()}
    if undefined:
        log(f"target list: `needs:` names undefined slug(s), which block nothing: {', '.join(sorted(undefined))}")
    return targets


def load_targets(path: Path) -> Targets:
    try:
        return parse_targets(Path(path).read_text())
    except (OSError, UnicodeDecodeError, Die) as e:
        raise Die(f"roadmap targets: {path}: {getattr(e, 'strerror', None) or e}") from None


def marker_keys(body: str) -> tuple[set[Key], set[Key]]:
    """The (focus, id) pairs of a pull request body's target markers, and the subset whose marker
    says `"partial": true`: a part or a prerequisite of the item, which merging does not complete."""
    keys: set[Key] = set()
    partial: set[Key] = set()
    for match in TARGET_MARKER_RE.finditer(body or ""):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        focus, ident = data.get("focus"), data.get("id")
        if isinstance(focus, str) and focus and isinstance(ident, str) and ident:
            keys.add((focus, ident))
            if data.get("partial") is True:
                partial.add((focus, ident))
    return keys, partial


def partial_prs(item: TargetItem) -> set[int]:
    """The pull requests an item's `partial:` clause names; a merge of one never completes the item."""
    return {
        int(n)
        for clause in item.meta
        if clause.partition(":")[0].strip().lower() == "partial"
        for n in re.findall(r"#(\d+)", clause)
    }


def overlay_live(targets: Targets, inflight: set[Key], done: set[Key]) -> Targets:
    """The list with each item at its effective status: done if the file says so or its key is in
    `done`; else in flight if the file says so or its key is in `inflight`; else open."""

    def effective(it: TargetItem) -> str:
        if it.status == "done" or (it.area, it.slug) in done:
            return "done"
        return "inflight" if (it.area, it.slug) in inflight else it.status

    return Targets(
        targets.preamble, {a: [replace(it, status=effective(it)) for it in items] for a, items in targets.areas.items()}
    )


def landed(items: Iterable[TargetItem], merged: Iterable[dict]) -> dict[Key, int]:
    """The items a merged pull request among `merged` completes, each with that pull request: its
    marker names the item and is not partial, and the item's `partial:` clause does not name it."""
    excused = {(it.area, it.slug): partial_prs(it) for it in items}
    done: dict[Key, int] = {}
    for d in merged:
        keys, partial = marker_keys(d.get("body"))
        for key in keys - partial:
            if key in excused and d.get("number") not in excused[key]:
                done.setdefault(key, d.get("number"))
    return done


def live_targets(targets: Targets, gh) -> Targets:
    """`overlay_live` from GitHub: an open pull request whose marker names an item puts it in flight,
    a merged one that completes it (`landed`) makes it done. The merged side sees the MERGED_LIMIT
    most recently updated closed pull requests that carry a marker. Raises GitHubError when either
    listing fails."""
    inflight: set[Key] = set()
    for d in gh.open_prs():
        inflight |= marker_keys(d.get("body"))[0]
    recent = gh.merged_prs(f"{MERGED_SEARCH} sort:updated-desc", limit=MERGED_LIMIT)
    return overlay_live(targets, inflight, set(landed(targets.items(), recent)))


def landed_pr(item: TargetItem, gh) -> int | None:
    """The merged pull request that completed `item`, however long ago: one search for its id
    among the closed pull requests that carry a marker. Raises GitHubError when it fails."""
    ident = '\\"id\\":\\"' + item.slug.replace('"', "") + '\\"'
    return landed([item], gh.merged_prs(f'"{ident}" {MERGED_SEARCH}', limit=30)).get((item.area, item.slug))


def eligible_items(targets: Targets, area: str) -> list[TargetItem]:
    """The area's open items whose every `needs` slug is done, in file order. A slug the file does
    not define blocks nothing."""

    def met(slug: str) -> bool:
        dep = targets.find(slug)
        return dep is None or dep.status == "done"

    return [it for it in targets.areas.get(area, []) if it.status == "open" and all(map(met, it.needs))]


def eligible_areas(targets: Targets) -> list[str]:
    return [a for a in targets.areas if eligible_items(targets, a)]


def candidates(targets: Targets, only: str, skip: Iterable[str]) -> list[TargetItem]:
    """The eligible items in claim order. `only` is the round's roadmap area, or `auto`, `any` or
    empty for none: a pinned area is taken alone, even when `skip` names it; otherwise every area
    outside `skip`, shuffled so that workers started together spread out, in file order within an
    area. NoProgress, saying why, when the list offers nothing."""
    pinned = only not in ("auto", "any", "")
    eligible = eligible_areas(targets)
    areas = [a for a in eligible if (a == only if pinned else a not in skip)]
    if areas:
        random.shuffle(areas)
        return [it for a in areas for it in eligible_items(targets, a)]
    waiting = sum(it.status == "open" for it in targets.items() if not pinned or it.area == only)
    if eligible and not pinned:
        why = f"every area with an eligible item is in --roadmap-skip: {', '.join(eligible)}"
    elif waiting:
        why = f"{waiting} open item(s) wait on needs that have not landed"
    else:
        why = "no open items"
    raise NoProgress(f"roadmap: the target list offers nothing to author{f' in {only}' if pinned else ''} ({why})")


def render_item(targets: Targets, item: TargetItem) -> str:
    """One item as the prompt shows it, each prerequisite with its current status (`[?]` when the
    file does not define it)."""
    needs = []
    for slug in item.needs:
        dep = targets.find(slug)
        needs.append(f"`{slug}` {_BOX[dep.status] if dep else '[?]'}")
    clauses = [*item.meta, "needs: " + (", ".join(needs) or "none")]
    return f"- {_BOX[item.status]} `{item.slug}` — {item.text} ({'; '.join(clauses)})"


def marker_stem(item: TargetItem) -> str:
    """What the item's target marker starts with, whether it is complete or partial. As
    TAUCETI_TARGET_MARKER it makes gh-safe-pr-create refuse a pull request body without it."""
    ident = json.dumps({"focus": item.area, "id": item.slug}, separators=(",", ":"), ensure_ascii=False)
    return f"<!--tauceti-target:v1 {ident[:-1]}"


def render_assignment(targets: Targets, item: TargetItem) -> str:
    """The bullet that opens the roadmap prompt's "Choose a target" section under a target list."""
    area, slug = item.area, item.slug
    marker, partial = marker_stem(item) + "}-->", marker_stem(item) + ',"partial":true}-->'
    block = [render_item(targets, it) for it in targets.areas.get(area, [])]
    if targets.preamble:
        block = [*targets.preamble.splitlines(), "", *block]
    listing = "\n".join(f"  {line}".rstrip() for line in block)
    return f"""\
- **Your target is assigned: `{slug}` in `{area}`.** This worker authors from an operator's target list: milestones on the path to one goal, with the order they depend on each other. It chose `{slug}` for this round and made the claim `author/{area}/{slug}` before starting you. This replaces your own choice of target in the bullets below; the rest of this prompt applies unchanged.
  - Author `{slug}`, or a prerequisite inside `{area}` that it cannot land without. Do not take another item of the list, and do not switch to another roadmap. If an open or merged PR already covers the target, if it appears among the claimed targets below, or if it waits on work that has not landed, stop without a PR and say which.
  - Skip the `claim.sh acquire` step below, and use `{slug}` as the `<slug>`. The PR body carries exactly `{marker}` when the PR completes the target, and `{partial}` when it lands only a part or a prerequisite of it: a merged marker without `"partial":true` tells every worker on the list that the target is done.
  - The list's entries for `{area}` follow, with the operator's note on the list above them when there is one. `[ ]` is open, `[~]` in flight and `[x]` landed, as of this round; `needs:` names the items an entry waits on, and one marked `[?]` is not in the list.
  ```
{listing}
  ```
"""
