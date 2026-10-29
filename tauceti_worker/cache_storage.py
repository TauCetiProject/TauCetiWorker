"""Local cache headroom policy. Slow inventory/GC is never part of status or a paid round."""

from __future__ import annotations

import contextlib
import os
import shutil
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


def roots(configs, pool):
    from .retention import cache_roots

    yield pool
    for cfg in configs:
        for path, _ in cache_roots(cfg):
            yield path
        for name in (".lake", "lake-packages", "build"):
            yield cfg.checkout / name


def inventory(paths, *, protected=(), owners=None, details=None):
    """Allocated bytes, deduplicated by inode across both pool and private hard links."""
    seen = {}
    owners = owners or {}
    for root in paths:
        if not root.is_dir() or root.is_symlink():
            continue
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
            for name in files:
                path = Path(base) / name
                try:
                    if path.is_symlink():
                        continue
                    info = path.stat()
                    key = info.st_dev, info.st_ino
                    if key in seen:
                        seen[key][1] += 1
                        seen[key][3] = seen[key][3] or root in protected
                    else:
                        seen[key] = [info.st_blocks * 512, 1, info.st_nlink, root in protected, {}]
                    if root in owners:
                        labels = seen[key][4]
                        labels[owners[root]] = labels.get(owners[root], 0) + 1
                except FileNotFoundError:
                    continue
    if details is not None:
        details.update(seen)
    result = {}
    for (device, _), (size, count, links, busy, _) in seen.items():
        row = result.setdefault(device, dict(managed_bytes=0, externally_linked_bytes=0, protected_bytes=0))
        row["managed_bytes"] += size
        if busy:
            row["protected_bytes"] += size
        if links > count:
            row["externally_linked_bytes"] += size
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


def collect(configs, *, dry=False, now=None, opened=...):
    from .retention import LIFETIME, cache_roots, fresh_report, note, open_files, remove, workspace_idle

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
    details = {}
    before = inventory(paths, protected=protected, owners=owners, details=details)
    private_candidates = {}
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
    removed_bytes = {device: 0 for device in devices}

    def stamp(candidate):
        try:
            return candidate[0].stat().st_mtime
        except FileNotFoundError:
            return 0

    for clock, entry, key in sorted(candidates, key=stamp):
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
                if not disposable_stage and any(details.get((i.st_dev, i.st_ino), [0, 0, 0, False])[3] for i in infos):
                    note(report, "protected", str(entry))
                    continue
                # Pool-only bundles have an entry and canonical link. Private links
                # cannot be reclaimed by deleting a pool name, so don't churn warm entries.
                reclaimable = sum(i.st_blocks * 512 for i in infos if i.st_nlink <= (2 if entry.is_dir() else 1))
                if not disposable_stage and urgent and now - stamp((clock, entry, key)) < LIFETIME and not reclaimable:
                    continue
                unique = sum(p.stat().st_blocks * 512 for p in names if p.is_file() and p.stat().st_nlink == 1)
                remove(entry, pool, report, dry=dry, check_users=False, mounts=[])
                if not dry and not entry.exists():
                    remaining -= unique
                    removed_bytes[pool_device] = removed_bytes.get(pool_device, 0) + unique
                    for old in names:
                        shared = pool / ("archives" if old.name.endswith(".ltar") else "artifacts") / old.name
                        if shared.is_file() and not shared.is_symlink() and shared.stat().st_nlink == 1:
                            size = shared.stat().st_blocks * 512
                            remaining -= size
                            removed_bytes[pool_device] = removed_bytes.get(pool_device, 0) + size
                            shared.unlink()
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
            try:
                info = path.lstat()
                if path.is_symlink() or info.st_nlink != 1:
                    continue
                remove(path, pool, report, dry=dry, check_users=False, mounts=[])
                if not dry and not path.exists():
                    removed_bytes[info.st_dev] = removed_bytes.get(info.st_dev, 0) + info.st_blocks * 512
            except FileNotFoundError:
                continue
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
    report["protected_bytes"] = sum(row.get("protected_bytes", 0) for row in report["filesystems"])
    report["reclaimed_bytes"] = sum(row["reclaimed_bytes"] for row in report["filesystems"])
    pressured = [
        str(row["device"])
        for row in report["filesystems"]
        if row["managed_bytes"] > row["limit_bytes"] or row["free_bytes"] < headroom(row["total"], LOW)
    ]
    report["pressure"] = bool(pressured)
    if not dry:
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
                candidates.append((generation.get("started_at", 0), cfg.wid))
        selected = min(candidates)[1] if candidates else None
        if selected is None:
            # A critical latch must recover even if only build trees occupy space.
            # The selected worker evicts at its own safe round boundary; no active
            # build is interrupted and the fleet never flushes all working sets.
            fallback = []
            for cfg in configs:
                generation = read_json(cfg.state / "cache-generation.json")
                if now - generation.get("last_pressure_at", 0) < 300:
                    continue
                path = cfg.checkout
                while not path.exists():
                    path = path.parent
                usage = shutil.disk_usage(path)
                latched = read_json(activity_root() / f"critical-{path.stat().st_dev}.json").get("active", False)
                if (
                    usage.free < headroom(usage.total, CRITICAL) or latched and usage.free < headroom(usage.total, LOW)
                ) and any((cfg.checkout / name).is_dir() for name in (".lake", "lake-packages", "build")):
                    fallback.append((generation.get("started_at", 0), cfg.wid))
            selected = min(fallback)[1] if fallback else None
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
