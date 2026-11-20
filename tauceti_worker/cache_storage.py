"""Local cache headroom policy. Slow inventory/GC is never part of status or a paid round."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import time
from pathlib import Path

from . import expanded_cache
from .runtime_status import atomic_json, read_json

LOW = 0.15
TARGET = 0.20
CRITICAL = 0.05
MIN_PRIVATE_RECLAIM = 256 * 1024**2


def headroom(total, fraction):
    """Percentage thresholds on small disks, bounded free-byte reserves on fleet disks."""
    ceiling = {CRITICAL: 20, LOW: 40, TARGET: 60}[fraction] * 1024**3
    return min(int(total * fraction), ceiling)


def initialize(pool):
    """An explicit ownership record is required before collecting a shared directory."""
    if pool.is_symlink():
        raise ValueError(f"build cache must not be a symlink: {pool}")
    pool.mkdir(parents=True, exist_ok=True, mode=0o700)
    if pool.stat().st_uid != os.getuid():
        raise ValueError(f"build cache is not owned by this user: {pool}")
    marker = pool / "owner.json"
    if marker.exists():
        if read_json(marker) != dict(version=1, uid=os.getuid()):
            raise ValueError(f"unrecognized build cache owner: {pool}")
    else:
        if any(not path.name.startswith(".owner.json.") for path in pool.iterdir()):
            raise ValueError(f"refusing to adopt nonempty build cache: {pool}")
        # Competing first users may publish the identical record; the replacement is atomic.
        atomic_json(marker, dict(version=1, uid=os.getuid()))
    if pool.is_symlink() or pool.stat().st_uid != os.getuid():
        raise ValueError(f"build cache must be an owned directory, not a symlink: {pool}")


def legacy_lock(pool, name, *, dry=False):
    """The canonical object lock, exclusive and non-blocking, before deleting a pool copy."""
    from .canonical_store import object_lock, store

    if dry:
        return contextlib.nullcontext()
    return object_lock(store(pool), name, exclusive=True, blocking=False)


def roots(configs, pool):
    from .retention import cache_roots

    yield pool
    for cfg in configs:
        for path, _ in cache_roots(cfg):
            yield path
        for name in (".lake", "lake-packages", "build"):
            yield cfg.checkout / name
        yield cfg.checkout.with_name(cfg.checkout.name + ".lake-between-rounds")


class InventoryDeferred(Exception):
    """The run's deadline passed during the walk; nothing was decided from a partial inventory."""


INVENTORY_CHECK_EVERY = 4096  # entries between deadline checks: cheap, yet well under a second


class _Clock:
    """One deadline counter for a whole inventory, across every root it walks."""

    def __init__(self, deadline):
        self.deadline, self.entries = deadline, 0

    def check(self, *, now=False):
        self.entries += 1
        if (now or self.entries % INVENTORY_CHECK_EVERY == 0) and self.deadline is not None:
            if time.monotonic() >= self.deadline:
                raise InventoryDeferred("inventory deadline")


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _scan(root, clock):
    """Every non-directory, non-symlink entry under ``root`` with its lstat, never following links.

    One lstat per entry (scandir caches it). A directory is opened without following a final
    symbolic link and must still be the inode that was queued, so one swapped for a link meanwhile
    is skipped. A directory that cannot be opened or listed is skipped, as os.walk does; a failing
    stat of an entry other than a vanished one propagates rather than leave a silent gap."""
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    pending = [(str(root), info.st_dev, info.st_ino)]
    while pending:
        path, device, inode = pending.pop()
        try:
            descriptor = os.open(path, _DIRECTORY_FLAGS)
        except OSError:
            continue
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (device, inode):
                continue  # replaced since it was queued
            try:
                entries = os.scandir(descriptor)
            except OSError:
                continue
            with entries:
                while True:
                    try:
                        entry = next(entries)
                    except StopIteration:
                        break
                    except OSError:
                        break  # an unreadable listing: skip the rest of it, as os.walk does
                    clock.check()
                    try:
                        found = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(found.st_mode):
                        continue
                    if stat.S_ISDIR(found.st_mode):
                        pending.append((os.path.join(path, entry.name), found.st_dev, found.st_ino))
                    else:
                        yield found
        finally:
            os.close(descriptor)


def _overlapping(roots):
    """Whether one name can be reached through two walked roots: a root repeated (by any
    spelling) or lying inside another."""
    resolved = [Path(os.path.realpath(root)) for root in roots]
    unique = set(resolved)
    return len(unique) < len(resolved) or any(parent in unique for root in unique for parent in root.parents)


def inventory(paths, *, protected=(), owners=None, details=None, private=None, deadline=None):
    """Allocated bytes, deduplicated by inode across both pool and private hard links.

    `named_bytes` counts every name, as `du --count-links` would: hard-linked files count once per
    link there, so it overstates what the disk holds. `managed_bytes` counts each inode once, and
    `unique_inodes` is how many inodes that is.

    Memory stays bounded by the hard-linked inodes: a file with one link has one name, so it is
    totalled at once and never remembered, unless the roots overlap and that name can be reached
    twice. ``details`` receives the remembered inodes as
    [bytes, names seen, links, protected, {owner: names}]. ``private`` receives, per
    (device, owner), the bytes of that owner's unprotected single-link files that were not
    remembered. Raises InventoryDeferred once ``deadline`` (time.monotonic) has passed, checked
    before, during and after the walk."""
    seen, result = {}, {}
    owners = owners or {}
    clock = _Clock(deadline)
    clock.check(now=True)
    roots = [root for root in paths if root.is_dir() and not root.is_symlink()]
    remember_all = _overlapping(roots)

    def row(device):
        return result.setdefault(
            device,
            dict(managed_bytes=0, externally_linked_bytes=0, protected_bytes=0, named_bytes=0, unique_inodes=0),
        )

    for root in roots:
        busy, owner = root in protected, owners.get(root)
        for info in _scan(root, clock):
            size = info.st_blocks * 512
            if info.st_nlink <= 1 and not remember_all:
                totals = row(info.st_dev)
                totals["managed_bytes"] += size
                totals["named_bytes"] += size
                totals["unique_inodes"] += 1
                if busy:
                    totals["protected_bytes"] += size
                elif owner is not None and private is not None:
                    key = info.st_dev, owner
                    private[key] = private.get(key, 0) + size
                continue
            key = info.st_dev, info.st_ino
            if key in seen:
                seen[key][1] += 1
                seen[key][3] = seen[key][3] or busy
            else:
                seen[key] = [size, 1, info.st_nlink, busy, {}]
            if owner is not None:
                labels = seen[key][4]
                labels[owner] = labels.get(owner, 0) + 1
    clock.check(now=True)
    for (device, _), (size, count, links, busy, _) in seen.items():
        clock.check()
        totals = row(device)
        totals["managed_bytes"] += size
        totals["named_bytes"] += size * count
        totals["unique_inodes"] += 1
        if busy:
            totals["protected_bytes"] += size
        if links > count:
            totals["externally_linked_bytes"] += size
    clock.check(now=True)  # nothing is decided on an inventory finished past the deadline
    if details is not None:
        details.update(seen)
    return result


def limits(path):
    usage = shutil.disk_usage(path)
    override = os.environ.get("TAUCETI_CACHE_MAX_GIB")
    cap = int(float(override) * 1024**3) if override else int(usage.total * 0.25)
    if cap <= 0:
        raise ValueError("TAUCETI_CACHE_MAX_GIB must be positive")
    return usage, cap


def admit(cfg):
    """Only a genuine critical disk threshold defers a new round; retries need no operator."""
    from .activity import root as activity_root
    from .config import AdmissionUnavailable

    checked = set()
    for location in (cfg.state, getattr(cfg, "checkout", cfg.state)):
        while not location.exists():
            location = location.parent
        device = location.stat().st_dev
        if device in checked:
            continue
        checked.add(device)
        usage = shutil.disk_usage(location)
        marker = activity_root() / f"critical-{location.stat().st_dev}.json"
        critical = usage.free < headroom(usage.total, CRITICAL)
        if usage.free < headroom(usage.total, LOW):
            from .retention import request_cleanup

            request_cleanup()
        if critical:
            # Keep the hysteresis marker separate from GC requests so collection cannot clear
            # the critical threshold while free space is still below the recovery threshold.
            with contextlib.suppress(OSError, ValueError):
                atomic_json(marker, dict(active=True))
        critical = critical or read_json(marker).get("active", False)
        if critical and usage.free < headroom(usage.total, LOW):
            raise AdmissionUnavailable(
                f"disk headroom low: {usage.free / 1024**3:.1f} GiB free; new work retries automatically above {headroom(usage.total, LOW) / 1024**3:.1f} GiB free",
                state="waiting-storage",
            )
        if critical:
            with contextlib.suppress(OSError):
                marker.unlink(missing_ok=True)


def collect(configs, *, dry=False, now=None, opened=..., deadline=None):
    from .retention import LIFETIME, cache_roots, fresh_report, note, open_files, past, remove, workspace_idle

    now = time.time() if now is None else now
    pool = expanded_cache.root()
    report = fresh_report(now)
    owned = False
    if (pool / "owner.json").is_file():
        try:
            initialize(pool)
            owned = True
        except (OSError, ValueError) as error:
            note(report, "errors", str(error))
    # Private cache reclamation must still work if sharing is disabled/unavailable.
    paths = list(dict.fromkeys(list(roots(configs, pool))[0 if owned else 1 :]))
    from .activity import root as activity_root

    request = activity_root() / "cache-pressure.json"
    if opened is ...:
        opened = open_files()
    protected = {
        cfg.checkout / name
        for cfg in configs
        if not workspace_idle(cfg, opened)
        for name in (".lake", "lake-packages", "build")
    }
    owners = {path: cfg.wid for cfg in configs for path, _ in cache_roots(cfg)}
    from .canonical_store import Guard

    # Pool copies are deleted only while no process holds them open (open fds add no link).
    guard = Guard(opened=opened or ())
    details, private_candidates = {}, {}
    # The walk gets half the time, so deletion always has the rest.
    walk = None if deadline is None else time.monotonic() + max(0.0, deadline - time.monotonic()) / 2
    try:
        before = inventory(
            paths, protected=protected, owners=owners, details=details, private=private_candidates, deadline=walk
        )
    except InventoryDeferred:
        # Without the full picture, still delete oldest-first: each candidate's own link counts and
        # locks decide, and urgency comes from free space alone. Private reclamation waits for a
        # complete walk.
        report.setdefault("deferred", []).append("shared-cache-inventory")
        before, details, private_candidates = {}, {}, {}
    for (device, _), (size, _, _, busy, labels) in details.items():
        if not busy and labels:
            for worker in labels:
                key = device, worker
                private_candidates[key] = private_candidates.get(key, 0) + size // len(labels)
    devices = {}
    for path in paths:
        if not path.exists():
            continue
        device = path.stat().st_dev
        if device in devices:
            continue
        usage, cap = limits(path)
        devices[device] = dict(
            before[device] if device in before else dict(managed_bytes=0),
            device=device,
            total=usage.total,
            free_before=usage.free,
            limit_bytes=cap,
            path=str(path),
        )
    pressured = [
        str(dev)
        for dev, row in devices.items()
        if row["managed_bytes"] > row["limit_bytes"] or row["free_before"] < headroom(row["total"], LOW)
    ]
    pool_device = pool.stat().st_dev if owned else None
    urgent = str(pool_device) in pressured
    if owned and "shared-cache-inventory" in report.get("deferred", []):
        # The pool's size is unknown: free space alone decides, up to the usual target.
        usage = shutil.disk_usage(pool)
        urgent = urgent or usage.free < headroom(usage.total, TARGET)
    # Oldest reuse first. These are disposable entries, not source or accounting records.
    candidates = []
    for entry in (pool / "entries").glob("*") if owned else ():
        if (
            entry.is_dir()
            and not entry.is_symlink()
            and expanded_cache.HASH.fullmatch(entry.name.removesuffix(".retention-deleting"))
        ):
            candidates.append((entry / "entry.json", entry, entry.name.removesuffix(".retention-deleting")))
    for parent, prefix in (
        (pool / "downloads", "download-"),
        (pool / "staging", "stage-"),
        (pool / "helpers", "helperstage-"),
    ):
        for entry in parent.glob("*") if owned else ():
            if entry.is_dir() and not entry.is_symlink():
                candidates.append((entry, entry, prefix + entry.name.removesuffix(".retention-deleting")))
    for path in (pool / "mathlib").glob("*.ltar") if owned else ():
        if path.is_file() and not path.is_symlink():
            candidates.append((path, path, "mathlib-" + path.name))
    remaining = before.get(pool_device, {}).get("managed_bytes", 0)
    protected_roots = [os.path.realpath(path) + os.sep for path in protected]

    def held(path, info):
        """Protected: in a protected root by any name. A single-link file has only this name."""
        row = details.get((info.st_dev, info.st_ino))
        if row is not None:
            return row[3]
        return (os.path.realpath(path) + os.sep).startswith(tuple(protected_roots))

    removed_bytes = {device: 0 for device in devices}

    def stamp(candidate):
        try:
            return candidate[0].stat().st_mtime
        except FileNotFoundError:
            return 0

    def out_of_time():
        if past(deadline):
            report.setdefault("deferred", []).append("shared-cache-deletion")
            return True
        return False

    for clock, entry, key in sorted(candidates, key=stamp):
        if out_of_time():
            break
        usage, cap = limits(pool)
        if not urgent and now - stamp((clock, entry, key)) < LIFETIME:
            continue
        if urgent and not dry and usage.free >= headroom(usage.total, TARGET) and remaining <= cap:
            urgent = False
            if now - stamp((clock, entry, key)) < LIFETIME:
                continue
        if entry.parent.name in ("downloads", "staging", "helpers") and now - stamp((clock, entry, key)) < 4 * 3600:
            continue
        try:
            with expanded_cache.locked(pool, key, readonly=dry):
                disposable_stage = entry.parent.name in ("downloads", "staging", "helpers")
                names = (
                    list(entry.rglob("*"))
                    if disposable_stage
                    else (list(entry.iterdir()) if entry.is_dir() else [entry])
                )
                payload = [p for p in names if p.is_file() and p.name != "entry.json"]
                infos = [p.stat() for p in payload]
                if not disposable_stage and any(held(p, i) for p, i in zip(payload, infos, strict=True)):
                    note(report, "protected", str(entry))
                    continue
                # Pool-only bundles are linked only by pool names: the entry alias, the legacy
                # artifacts/ copy and the canonical object. Private links cannot be reclaimed by
                # deleting a pool name, so don't churn warm entries.
                from .canonical_store import pool_aliases, release

                reclaimable = sum(
                    i.st_blocks * 512
                    for p, i in zip(payload, infos, strict=True)
                    if i.st_nlink <= (1 if entry.is_dir() else 0) + max(1, len(pool_aliases(pool, p.name, i)))
                )
                if not disposable_stage and urgent and now - stamp((clock, entry, key)) < LIFETIME and not reclaimable:
                    continue
                unique = sum(p.stat().st_blocks * 512 for p in names if p.is_file() and p.stat().st_nlink == 1)
                remove(entry, pool, report, dry=dry, check_users=False, mounts=[])
                if not dry and not entry.exists():
                    remaining -= unique
                    removed_bytes[pool_device] = removed_bytes.get(pool_device, 0) + unique
                    for old in names:
                        if old.name == "entry.json" or not expanded_cache.ARTIFACT.fullmatch(old.name):
                            continue
                        # The legacy copy and canonical object go together once only they link it;
                        # a relink holding the object lock while it adopts the copy wins.
                        with contextlib.suppress(BlockingIOError):
                            size = release(pool, old.name, guard)
                            remaining -= size
                            removed_bytes[pool_device] = removed_bytes.get(pool_device, 0) + size
        except BlockingIOError:
            note(report, "protected", str(entry))
        except FileNotFoundError:
            continue
        except OSError as error:
            note(report, "errors", str(error))
    for parent in (pool / "status", pool / "helpers"):
        for path in parent.glob("*") if owned else ():
            try:
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime > LIFETIME:
                    remove(path, pool, report, dry=dry, check_users=False, mounts=[])
            except OSError as error:
                note(report, "errors", str(error))
    # Publication links its private staging file before making a canonical name,
    # so a canonical file with one remaining link is safely unreferenced.
    for directory in ("artifacts", "archives"):
        for path in (pool / directory).glob("*") if owned else ():
            if out_of_time():
                break
            try:
                info = path.lstat()
                if path.is_symlink() or info.st_nlink != 1:
                    continue
                with legacy_lock(pool, path.name, dry=dry):
                    info = path.lstat()
                    if info.st_nlink != 1:
                        continue  # Adopted as a canonical object since the first look.
                    if not guard.idle((path, info), recent_ok=True):
                        note(report, "protected", str(path))
                        continue
                    remove(path, pool, report, dry=dry, check_users=False, mounts=[])
                if not dry and not path.exists():
                    removed_bytes[info.st_dev] = removed_bytes.get(info.st_dev, 0) + info.st_blocks * 512
            except BlockingIOError:
                note(report, "protected", str(path))
            except FileNotFoundError:
                continue
    if owned:
        from .canonical_store import sweep

        # Canonical objects (and their adopted pool alias) go once no checkout or store links them.
        freed = sweep(
            pool,
            report,
            dry=dry,
            urgent=urgent,
            now=now,
            lifetime=LIFETIME,
            remove=remove,
            opened=opened or (),
            deadline=deadline,
        )
        if not dry:
            removed_bytes[pool_device] = removed_bytes.get(pool_device, 0) + freed
    # One inventory per sweep; update the estimate for known last-link removals.
    # Actual free-space recovery is separately observed with statvfs below.
    after = {
        dev: dict(row, managed_bytes=max(0, row["managed_bytes"] - removed_bytes.get(dev, 0)))
        for dev, row in before.items()
    }
    report["filesystems"] = []
    for dev, row in devices.items():
        usage = shutil.disk_usage(row["path"])
        report["filesystems"].append(
            dict(
                row,
                **{k: v for k, v in after.get(dev, {}).items()},
                free_bytes=usage.free,
                reclaimed_bytes=max(0, usage.free - row["free_before"]),
            )
        )
    report["managed_bytes"] = sum(row["managed_bytes"] for row in report["filesystems"])
    report["named_bytes"] = sum(row.get("named_bytes", 0) for row in report["filesystems"])
    report["unique_inodes"] = sum(row.get("unique_inodes", 0) for row in report["filesystems"])
    report["protected_bytes"] = sum(row.get("protected_bytes", 0) for row in report["filesystems"])
    report["reclaimed_bytes"] = sum(row["reclaimed_bytes"] for row in report["filesystems"])
    pressured = [
        str(row["device"])
        for row in report["filesystems"]
        if row["managed_bytes"] > row["limit_bytes"] or row["free_bytes"] < headroom(row["total"], LOW)
    ]
    report["pressure"] = bool(pressured)
    if not dry:
        busy_workers = set()
        for cfg in configs if pressured else ():
            try:
                # Reuse the open-file observation without taking work locks: even a
                # brief probe could make a concurrently starting round lose its lock.
                paths = [cfg.state / "round.lock"]
                for path in paths if opened is not None else ():
                    try:
                        info = path.stat()
                    except FileNotFoundError:
                        continue
                    if (info.st_dev, info.st_ino) in opened:
                        busy_workers.add(cfg.wid)
                        break
            except OSError:
                busy_workers.add(cfg.wid)
                note(report, "protected", str(cfg.state))
        candidates = []
        for cfg in configs:
            generation = read_json(cfg.state / "cache-generation.json")
            last = generation.get("last_pressure_at", 0)
            if now - last < 12 * 3600:
                continue
            potential = sum(
                size
                for (dev, worker), size in private_candidates.items()
                if worker == cfg.wid and str(dev) in pressured
            )
            if potential >= MIN_PRIVATE_RECLAIM:
                candidates.append((cfg.wid in busy_workers, generation.get("started_at", 0), cfg.wid))
        selected = min(candidates)[-1] if candidates else None
        # Request only one useful reclamation, after pool GC has had its chance.
        # Held working sets/unrelated disk use must not flush the fleet every hour.
        atomic_json(
            request,
            dict(
                until=now + 7200 if selected else 0,
                requested_at=now,
                devices=pressured,
                workers=[selected] if selected else [],
            ),
        )
        report["private_reclamation_worker"] = selected
    return report
