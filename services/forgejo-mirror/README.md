# Off-host mirror of the Forgejo TauCeti repository

A systemd user timer keeps a bare copy of every ref of
`https://git.taucetiproject.org/TauCetiProject/TauCeti` and of its forks, refreshed every five
minutes. If the server loses its refs again (decision 42: a hard power-off emptied
`packed-refs`), the mirror is the source to push them back from. It covers branches, `refs/pull/*`,
the claims refs, the queue's `tauceti-ci/*` refs, and fork PR heads, including pushes by humans.

The repository is public, so the mirror fetches anonymously. `--token-file` (with `--user`, default
`tauceti-mirror-reader`) switches to a read-only token, which is sent in git's environment and
never on a command line.

## Layout

The mirror lives at `~/.local/share/tauceti-forgejo-mirror/TauCeti.git`:

| refs | content |
| --- | --- |
| `refs/heads/*`, `refs/pull/*`, `refs/tauceti-claims/*`, ... | the base repository's refs, exactly as Forgejo had them at the last accepted fetch |
| `refs/forks/<owner>/*` | every ref of `<owner>`'s fork, whatever it is named (forks are discovered through the Forgejo API each run, and remembered) |
| `refs/tauceti-mirror/staging/*` | the raw result of the latest fetch; never restore from these |

`health.json` (per-source last attempt, last success, ref count and last error) and
`snapshots/refs-YYYY-MM-DD.txt.gz` (one list of accepted refs per day, kept 60 days) sit next to it.

## Safety net

Each source is fetched with `--prune` into its staging namespace only. The accepted refs change
in one ref transaction, and only when the staged result passes these checks:

- the fetch succeeded and returned at least one ref;
- the base repository still has `refs/heads/main`;
- at most 10% of the accepted refs (or 10 refs, whichever is larger) disappeared
  (`--max-loss-percent`, `--loss-allowance`).

Base refs that would land in or at the root of `refs/forks` or `refs/tauceti-mirror` are dropped. A source that
fails a check keeps its previous accepted refs, and the run exits 1, so the unit
shows as failed. Every run writes one JSON line per source to the journal, at priority `err`
for a refusal. A refused state stays refused on later runs; the accepted copy never decays.

Git cannot delete `refs/heads/a` and create `refs/heads/a/b` (or the reverse) in one transaction.
Only in that case the run deletes the conflicting refs first and applies the rest in a second
transaction, putting the conflicting refs back if that fails. It lists those refs in
`apply-backup-<source>.txt` beforehand and removes the file afterwards. If a run dies in between,
the next run recreates them before assessing its fetch, so the loss check still compares against
the full accepted copy; `check` reports a leftover file.

The repository also keeps reflogs for every ref (`core.logAllRefUpdates=always`). Automatic gc is
off and `gc.pruneExpire=never`: the daily `git gc` packs objects but never deletes one, so every
commit a snapshot names stays available.

## Install and update

On tauchonki, from any copy of this repository:

```sh
services/forgejo-mirror/install.sh
```

The installer copies the script and units from TauCetiWorker `origin/main` (fetched into
`~/TauCetiForgejoMirror`, cloned when absent), whatever revision the copy that runs it is at. It
refuses systemd drop-ins and less than 10 GiB of free space, enables the timer and starts a first
run. The first run clones about 1 GB; later runs fetch only changes. Rerun it after a change to
this directory lands on main.

## Operate

```sh
systemctl --user list-timers tauceti-forgejo-mirror.timer
systemctl --user is-failed tauceti-forgejo-mirror.service
journalctl --user -u tauceti-forgejo-mirror.service --since -1h
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_mirror.py check   # exit 1 if a source is over 30 minutes old
```

A refusal means Forgejo returned something that looks like data loss. Check the server before doing
anything to the mirror: `git ls-remote https://git.taucetiproject.org/TauCetiProject/TauCeti`. If
the change is genuine (for example a mass branch cleanup), accept it once:

```sh
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_mirror.py fetch --accept-loss
```

To put the mirror back to a day's snapshot (the objects are still present):

```sh
cd ~/.local/share/tauceti-forgejo-mirror
zcat snapshots/refs-2026-11-19.txt.gz | awk '{ print "update " $2 " " $1 }' | git -C TauCeti.git update-ref --stdin
```

That restores and moves refs but does not delete refs created since; the next timer run fetches
again and applies the usual checks.

To stop the mirror: `systemctl --user disable --now tauceti-forgejo-mirror.timer`.

To push the refs back to Forgejo after a loss, follow [the restore runbook](../../docs/forgejo-restore.md)
(`forgejo_restore.py`, installed next to the mirror script).
