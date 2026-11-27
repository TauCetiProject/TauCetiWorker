# Sharing worker Lake caches

The expanded build cache already shares immutable artifacts by hard link. A worker's
`.cache/lake` directory is a private namespace, not necessarily private disk storage.
Adding separate `du` results overcounts hard-linked data. Lake's mutable output and
revision indexes, and its fixed-name download temporaries, must remain separate.

Locally built artifacts can still accumulate as distinct inodes with identical
contents, including links from each worker's checkout. On Linux with XFS reflink
support, `scripts/dedupe-lake-caches` asks the kernel to merge identical physical
blocks. The kernel compares the bytes and coordinates concurrent I/O. Copy-on-write
keeps future changes separate. This preserves every pathname, inode, permission,
and existing hard link; it deletes nothing and needs no worker restart or drain.
The script opens only owned, read-only, regular artifact files, without following
artifact symlinks. It never changes permissions to obtain access. Unsupported
filesystems and insufficient metadata space stop the pass with an error.

Install as the fleet user on a reflink-capable Linux host:

```sh
install -m 755 scripts/dedupe-lake-caches ~/.local/share/tauceti-fleet/bin/dedupe-lake-caches
install -m 644 services/lake-cache-dedupe.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lake-cache-dedupe.timer
systemctl --user start --no-block lake-cache-dedupe.service
```

Each hourly pass runs for at most ten minutes, with a one-core CPU ceiling,
low scheduling/I/O priority, a 256 MiB memory ceiling, and at most 100,000 indexed
names. The starting worker rotates each hour. Large fleets need multiple passes;
the script does not promise to inspect every artifact in one pass. New duplicates
remain private until a later pass finds them. Other filesystems retain the existing
hard-link sharing behavior.

Progress is available in `$XDG_RUNTIME_DIR/tauceti-lake-cache-dedupe.log`. The
`matched_range_bytes` counter measures ranges accepted by the kernel, including
blocks already shared by reflink. It is not a count of newly freed bytes. Concurrent
builds and cleanup also change `df`, so its delta cannot be attributed solely to
deduplication. Nothing runs in `status`, and no GitHub API calls are made.

Run the portable fixtures with `python3 tests/lake_cache_dedupe.py`. Before deploying
on a new filesystem, also verify real equal/different-file behavior and copy-on-write
on disposable files there; the portable fixtures mock the kernel ioctl.
