# Restoring the Forgejo TauCeti repository after ref loss

On 18 November 2026 a hard power-off of the Forgejo server left `packed-refs` empty and a few loose
refs pointing at nothing. Every branch, `refs/pull/*` and claim ref of
`TauCetiProject/TauCeti` vanished, while its objects and Forgejo's database (PRs, reviews,
comments) survived. This runbook restores the refs with `forgejo_restore.py`, whose primary
source is the off-host mirror on tauchonki ([services/forgejo-mirror](../services/forgejo-mirror/README.md)).
Times below are what the recovery on 19 November needed.

## 1. Recognise it

- `git ls-remote https://git.taucetiproject.org/TauCetiProject/TauCeti` lists a handful of refs,
  some with SHA `0000000000000000000000000000000000000000`, and no `refs/heads/main`.
- The API lists PRs with an empty `head.sha`.
- Fleet workers log `waiting-github: source survey unavailable (inventory_projection)`; staging
  dispatches of batches with Forgejo members fail with `AmbiguousWrite`.
- The mirror's unit fails with a `refused` journal line: it keeps the last good copy, as intended.
  Do not run `forgejo_mirror.py fetch --accept-loss` during an incident.

On tauchonki:

```sh
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_restore.py check
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_mirror.py check
```

The first reports the target's refs and prints the root commands for step 3. The second shows how
old the mirror's copy is.

## 2. Same hour: keep work moving on GitHub

Switch the tauchonki fleet to GitHub, then restart its workers. Workers adopt routing changes between
rounds, and a worker stuck waiting on a Forgejo survey never finishes its round:

```sh
tauceti-fleet workers tauchonki --forgejo-workers 0
```

If staging is blocked by a batch with Forgejo members, keep Forgejo inputs out of new batches and
break up the live ones (queue operator, [mixed-sources.md](queue/mixed-sources.md), "Landing GitHub
work while Forgejo is unavailable"):

```sh
# on tauchonki2, in ~/TauCetiQueue
uv run tauceti-queue settings set forgejo_placement_enabled false
# then POST /admin/evict-native with body {} as a repository admin
```

Evicted members wait again at their original age with their approvals.

## 3. Get root to clear the broken refs

Forgejo refuses every push while a broken ref exists, and git cannot delete one remotely. Someone
with root on the server (Joscha or Sebastian in November) pastes the block `check` printed. It
runs in a subshell with `set -e`, changes to the repository directory
(`/var/lib/forgejo/repositories/taucetiproject/tauceti.git`, derived from the repository or the
`--fork`; override with `check --server-path`)
and stops at the first failure. It removes the listed broken refs (shell-quoted) and any empty
loose ref files, moves an empty `packed-refs` aside, and shows `git for-each-ref` and
`git fsck --connectivity-only` output.
A `packed-refs` that is non-empty but truncated shows up as an error from `git for-each-ref`; it
needs the same move aside. Rerun `check` until it reports no broken refs. An empty repository with
no main is fine at this point.

## 4. Restore

Run on tauchonki, where the mirror and the fleet's repositories are. The command reads two tokens:

- `~/.config/tauceti-forgejo/worker-token` (`kim-em`, already on tauchonki) pushes every ref except
  main; `tauceti-ci/**` is protected for `kim-em`.
- `~/.config/tauceti-forgejo/main-sync-token` (`tauceti-main-sync`, the only account allowed to
  push main) is on chungus2. Copy it for the restore and delete it afterwards:

  ```sh
  ssh chungus2 cat .config/tauceti-forgejo/main-sync-token | install -m 600 /dev/stdin ~/.config/tauceti-forgejo/main-sync-token
  ```

First a dry run, which pushes nothing and writes the plan to
`~/.local/share/tauceti-forgejo-mirror/restore-results.json`:

```sh
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_restore.py restore
```

The plan contains:

- `refs/heads/main` at GitHub's current main (`git ls-remote`, no REST), pushed as
  `tauceti-main-sync`;
- every other ref in the mirror's base namespace: branches, tags, `refs/tauceti-claims/*`,
  `tauceti-ci/*`. It never includes `refs/pull/*`, which Forgejo regenerates when a PR branch is
  pushed, or the mirror's `refs/forks/*` and staging refs;
- for each open PR from the base repository whose branch the mirror lacks, the fleet's copies in
  its shared repositories (`refs/tauceti/published/push/<branch>`, `refs/remotes/prscan/<N>`,
  `refs/remotes/origin/<branch>`). When they disagree it takes the copy that descends from all the
  others, otherwise the newest commit, marked `DIVERGED`. PRs whose head is in a fork are skipped,
  because the fork is a separate repository.

Review the lines it prints: `exists-different` (the target already has that ref at another SHA,
and it is left alone), `missing-object`, and `DIVERGED`. `--exclude PREFIX` (repeatable) drops a
namespace, for example `--exclude refs/tauceti-claims/` to let workers claim afresh. `--no-fallback`
uses the mirror only; `--skip-main` leaves main alone. Then push:

```sh
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_restore.py restore --execute
```

It checks each token's account through the API (the main sync token may not read its own user; the
command says so and continues), pushes main, then the rest in batches of 50. Every pushed ref is
sent with `--force-with-lease=<ref>:`, so git refuses it if the ref appeared on the target in the
meantime. A batch that fails with a transient error is listed again and retried with backoff.
Results are saved after each batch, and rerunning `restore --execute` only sends what is still
missing. In November 740 branches took about 6 minutes.

The command ends with `verify`. It fails unless every planned ref matches on `ls-remote`, no
planned ref lacks its commit, the open PR list can be read, every open base-repository PR has a
restored branch, and each such PR's API `head.sha` equals the planned SHA. Refs left alone because
the target had another SHA are reported, not counted as failures. Forgejo recomputes PR heads and
`refs/pull/N/head` shortly after the push, so rerun it after a few minutes if some lag:

```sh
python3 -I ~/.local/share/tauceti-forgejo-mirror/bin/forgejo_restore.py verify
rm ~/.config/tauceti-forgejo/main-sync-token
```

If a fork lost refs too, `forgejo_restore.py --fork OWNER[/NAME] restore` restores it from the
mirror's `refs/forks/OWNER/*`, with a token that can write to that fork (`--user`, `--token-file`).

What cannot come back: refs created in the last minutes before the loss (after the mirror's last
accepted fetch), and, without the mirror, anything no fleet host had a copy of.

## 5. Switch back

```sh
tauceti-fleet workers tauchonki --forgejo-workers 60
# on tauchonki2, in ~/TauCetiQueue
uv run tauceti-queue settings set forgejo_placement_enabled true
```

Restart the fleet's workers again, as in step 2. Then check that staging dispatches batches with
Forgejo members, that workers reach roadmap selection rather than `source survey unavailable`, and
that the mirror's unit succeeds again (`systemctl --user is-failed tauceti-forgejo-mirror.service`).
