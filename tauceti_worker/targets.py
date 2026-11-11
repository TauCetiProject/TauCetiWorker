"""Operator target lists for roadmap authoring (`--roadmap-targets FILE`).

The file format and the selection rules are documented in docs/target-lists.md. What the rest of
the worker uses:

    load_targets(path)                 read and parse the file; Die when it is not a target list
    live_targets(targets, gh, others)  statuses overlaid from the open and merged pull request markers
                                       of `gh` and of the other sources a round sees
    inventory_keys(rows)               the items a multi-source round's open-PR inventory names
    candidates(targets, only, skip)    the eligible items, in the order a worker tries to claim them
    landed_pr(item, gh)                the merged pull request that completed an item, at any age
    marked_prs(targets, prs)           the pull requests among `prs` whose marker names an item
    GitHubMarkerReads()                the GitHub repository's marked pull requests, for a round
                                       whose sources do not include GitHub
    render_assignment(targets, item)   the roadmap prompt's bullet assigning one item
    target_markers(item)               the item's complete and partial markers, one of which the
                                       pull request body must carry

`parse_targets`, `marker_keys`, `landed`, `overlay_live`, `eligible_items` and `eligible_areas`
are the pure pieces those are built from; none of them reads GitHub or the environment.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .config import Die, NoProgress, SourceUnavailable, log
from .survey import TARGET_MARKER_RE

MARKER_RE = re.compile(r"<!--\s*tauceti-targets:v1\s*-->")
MARKER_TAG = "tauceti-target:v1"
MERGED_SEARCH = f'"{MARKER_TAG}" in:body'
MERGED_LIMIT = 200
_ID_PHRASE = re.compile(r'"id":"(.*)"')
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
                needs = tuple(s.strip().strip("`").strip() for s in value.split(",") if s.strip("` \t"))
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


def inventory_keys(rows: Iterable[dict]) -> set[Key]:
    """The (focus, id) pairs of a multi-source round's open-PR target inventory
    (`cross_source_targets`), whose `targets` are `work_target_keys` strings."""
    keys: set[Key] = set()
    for row in rows:
        for target in row.get("targets") or ():
            try:
                focus, ident = json.loads(target)
            except (TypeError, ValueError):
                continue
            if isinstance(focus, str) and isinstance(ident, str):
                keys.add((focus, ident))
    return keys


def partial_prs(item: TargetItem) -> set[int]:
    """The pull requests an item's `partial:` clause names; a merge of one never completes the item."""
    return {int(n) for clause in item.meta if _is_partial(clause) for n in re.findall(r"#(\d+)", clause)}


def _is_partial(clause: str) -> bool:
    return clause.partition(":")[0].strip().lower() == "partial"


def ref_key(ref: str) -> str:
    """What identifies a pull request reference: `#N` as written, a URL in any case and without a
    trailing slash, since a forge's owner and repository names are not case-sensitive."""
    if ref.startswith("#"):
        return ref
    parts = urlsplit(ref)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/').casefold()}"


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
    marker names the item and is not partial, and the item's `partial:` clause does not name it.
    A row with a `url` (a Forgejo pull request) is named by that URL, any other by `#N`."""
    items = list(items)
    excused = {(it.area, it.slug): partial_prs(it) for it in items}
    urls = {
        (it.area, it.slug): {
            ref_key(u) for u in re.findall(r"https?://[^\s,;]+", " ".join(c for c in it.meta if _is_partial(c)))
        }
        for it in items
    }
    done: dict[Key, int] = {}
    for d in merged:
        keys, partial = marker_keys(d.get("body"))
        for key in keys - partial:
            if d.get("url"):
                named = ref_key(d["url"]) in urls.get(key, ())
            else:
                named = d.get("number") in excused.get(key, ())
            if key in excused and not named:
                done.setdefault(key, d.get("number"))
    return done


def live_targets(targets: Targets, gh, others: Iterable = ()) -> Targets:
    """`overlay_live` from the pull requests of `gh` and of each source in `others` (GitHub or
    Forgejo): an open one whose marker names an item puts it in flight, a merged one that completes
    it (`landed`) makes it done. The merged side sees each source's MERGED_LIMIT most recent closed
    pull requests that carry a marker. Raises GitHubError or SourceUnavailable when a listing fails."""
    inflight: set[Key] = set()
    done: set[Key] = set()
    for source in (gh, *others):
        rows = source.open_prs()
        if rows is None:
            raise SourceUnavailable("open pull requests are unavailable for the target list")
        for d in rows:
            inflight |= marker_keys(d.get("body"))[0]
        recent = source.merged_prs(f"{MERGED_SEARCH} sort:updated-desc", limit=MERGED_LIMIT)
        done |= set(landed(targets.items(), recent))
    return overlay_live(targets, inflight, done)


def search_phrases(search: str) -> list[str]:
    """The quoted phrases of a GitHub-style search, unescaped; what a body must contain to match."""
    return [p.replace('\\"', '"') for p in re.findall(r'"((?:[^"\\]|\\.)*)"', search)]


def search_id(phrases: list[str]) -> str | None:
    """The slug of an `"id":"<slug>"` phrase, as `landed_pr` searches for one item."""
    return next((m[1] for p in phrases if (m := _ID_PHRASE.fullmatch(p))), None)


def body_matches(body: str, phrases: list[str]) -> bool:
    """Whether a body satisfies a search's phrases: an `"id":"<slug>"` phrase by a parsed marker
    with that id, whatever its JSON spacing, any other phrase as a substring."""
    ident = search_id(phrases)
    if ident is not None and ident not in {i for _, i in marker_keys(body)[0]}:
        return False
    return all(p in body for p in phrases if not _ID_PHRASE.fullmatch(p))


def github_history_enabled() -> bool:
    """TAUCETI_ROADMAP_TARGETS_GITHUB: whether a round with no GitHub source still reads the GitHub
    content repository's marked pull requests (`GitHubMarkerReads`). On unless set to 0."""
    return os.environ.get("TAUCETI_ROADMAP_TARGETS_GITHUB", "1").strip().lower() not in ("0", "false", "no", "off")


class GitHubMarkerReads:
    """A source for `live_targets` and `landed_pr` made of the GitHub content repository's
    marker-bearing pull requests, for a round whose sources do not include GitHub (a list carried
    over from GitHub). One listing (`GitHub.target_marker_prs`) serves the open side, the merged
    side and every item's landing search it can answer. A failed read is logged and counts as no
    pull requests, which leaves the list's own marks."""

    def __init__(self, gh=None):
        from .github import GitHub

        self.gh = gh if gh is not None else GitHub()
        self._rows: list[dict] | None = None

    def _read(self, fetch) -> list[dict]:
        from .config import GitHubUnavailable
        from .github import GitHubError
        from .worker_sources import shared_github_environment

        try:
            with shared_github_environment():
                return fetch()
        except (GitHubError, GitHubUnavailable, SourceUnavailable, OSError, ValueError) as e:
            log(f"target list: GitHub pull requests unreadable ({e}); using the list's own marks for them")
            return []

    def _listing(self) -> list[dict]:
        if self._rows is None:
            self._rows = self._read(lambda: self.gh.target_marker_prs(limit=MERGED_LIMIT))
        return self._rows

    def open_prs(self) -> list[dict]:
        return [d for d in self._listing() if d.get("state") == "OPEN"]

    def merged_prs(self, search: str, *, limit: int = MERGED_LIMIT) -> list[dict]:
        """The listing's merged rows matching `search`. One item's search (`landed_pr`) that the
        listing cannot answer, because it holds MERGED_LIMIT rows and so may not reach back far
        enough, goes to GitHub as `GitHub.merged_prs`."""
        phrases = search_phrases(search)
        listing = self._listing()
        rows = [d for d in listing if d.get("state") == "MERGED" and body_matches(d.get("body") or "", phrases)]
        if rows or search_id(phrases) is None or len(listing) < MERGED_LIMIT:
            return rows[:limit]
        return self._read(lambda: self.gh.merged_prs(search, limit=limit))


def landed_pr(item: TargetItem, gh) -> int | None:
    """The merged pull request that completed `item`, however long ago: one search for its id
    among the closed pull requests that carry a marker. Raises GitHubError when it fails."""
    ident = '\\"id\\":\\"' + item.slug.replace('"', "") + '\\"'
    return landed([item], gh.merged_prs(f'"{ident}" {MERGED_SEARCH}', limit=30)).get((item.area, item.slug))


def marked_prs(targets: Targets, prs: Iterable[dict]) -> set[int]:
    """The numbers of the pull requests among `prs` whose body carries a target marker, complete or
    partial, for an item of the list."""
    listed = {(it.area, it.slug) for it in targets.items()}
    return {d["number"] for d in prs if marker_keys(d.get("body"))[0] & listed}


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


def target_markers(item: TargetItem) -> tuple[str, str]:
    """The item's complete marker and its `"partial":true` marker. Joined by a newline as
    TAUCETI_TARGET_MARKER, they make gh-safe-pr-create refuse a body that carries neither."""
    ident = json.dumps({"focus": item.area, "id": item.slug}, separators=(",", ":"), ensure_ascii=False)[:-1]
    return f"<!--tauceti-target:v1 {ident}}}-->", f'<!--tauceti-target:v1 {ident},"partial":true}}-->'


def render_assignment(targets: Targets, item: TargetItem) -> str:
    """The bullet that opens the roadmap prompt's "Choose a target" section under a target list."""
    area, slug = item.area, item.slug
    marker, partial = target_markers(item)
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
