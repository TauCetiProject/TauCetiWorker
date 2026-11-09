# Scheduled Stage 1 main repair

These opt-in systemd user units call the existing operator `mirror` command. They
do not start the autonomous queue or dispatch CI, and they reuse its publication
lock and durable promotion records. Installing the files does not enable the timer.

Use a reviewed Worker release that supports mirror-only profiles with nonempty
`mirror_actor_ids`. Create the human ledger first. The scheduled profile uses the
same ledger, clone, source/execution policy, and approved control anchor, but a
dedicated source reader's token and disjoint `mirror_actor_ids`. That account must
have no repository write role. Keep the human reviewers in `reviewer_ids` for
documentation; the mirror checks its actors against the authoritative ledger and
does not rotate reviewer policy. The mirror's GitHub App token remains restricted
to the execution repository and main writer role. See the
[operator guide](../../../docs/forgejo-stage1-operator.md) for profile details.

Both source and execution main must already match the approved bootstrap anchor.
Provision and test exact App repository selection and main permissions before
activation. Profile/configuration/credential files are trusted private operator
state; same-OS-user access is not an isolation boundary.

Run the following from `services/queue/stage1-mirror` in the reviewed checkout.
The named host owner must confirm Git is on the service's fixed system `PATH` and
check user-manager persistence:

```sh
loginctl show-user "$USER" -p Linger
```

Production scheduling requires lingering enabled by the host owner, or an explicitly
staffed restart duty after logout/reboot. Record that decision and owner in the
cutover package, then test a user-manager restart or reboot in the appropriate
isolated host. Preparation does not authorize changing this host-wide setting.

On the named operator host, prepare files without activating them:

```sh
install -d -m 700 "$HOME/.config/tauceti-stage1" "$HOME/.config/systemd/user"
install -m 600 mirror.env.example "$HOME/.config/tauceti-stage1/mirror.env"
# Set both paths in mirror.env to the actual absolute reviewed installation/profile.
install -m 600 tauceti-stage1-mirror.service tauceti-stage1-mirror.timer \
  "$HOME/.config/systemd/user/"
systemd-analyze --user verify "$HOME/.config/systemd/user/tauceti-stage1-mirror.service" \
  "$HOME/.config/systemd/user/tauceti-stage1-mirror.timer"
systemctl --user daemon-reload
```

The service refuses a relative/non-executable interpreter or a relative profile
path before loading credentials. Validate the selected interpreter explicitly:

```sh
"$operator_python" -I -c 'import tauceti_queue, importlib.metadata; print(tauceti_queue.__file__); print(importlib.metadata.distribution("tauceti").read_text("direct_url.json"))'
```

Only after the separately authorized cutover, and after inspecting the environment
file/profile, start one confirmation and then the timer:

```sh
if systemctl --user start tauceti-stage1-mirror.service \
  && [ "$(systemctl --user show tauceti-stage1-mirror.service -p ExecMainStatus --value)" = 0 ]; then
  systemctl --user enable --now tauceti-stage1-mirror.timer
  systemctl --user list-timers tauceti-stage1-mirror.timer
else
  systemctl --user show tauceti-stage1-mirror.service -p Result -p ExecMainStatus
  journalctl --user -u tauceti-stage1-mirror.service -n 30
  false
fi
```

Require `ExecMainStatus=0` and equality of both main refs, using the reviewed
credential-scoped Git access for private sources:

```sh
git ls-remote "$source_clone_url" refs/heads/main
git ls-remote "$execution_clone_url" refs/heads/main
```

The service uses isolated Python mode, which excludes its working directory,
environment-supplied Python paths and user site packages from imports. Its fixed
system `PATH` excludes user Git wrappers. It keeps a private umask and kills its owned
process group on timeout. All nonzero results remain visible; the next timer
invocation retries reconciliation without merging or dispatching. A failed
confirmation is a reason to repair the recorded issue, not permission to enable
the timer and ignore it. The host owner must monitor failures and availability.
Keep the trusted-path checks enabled. A user service's `PrivateTmp` can imply a
user namespace whose remapped ownership makes those checks refuse the profile;
the supplied service deliberately uses the ordinary trusted operator-host view.

Promotion mirrors inline; the five-minute timer is a recovery backstop. A human
command returning exit 75 may overlap it and should be retried after that invocation.
The named owner must check service failures, recent history, and remote equality:

```sh
systemctl --user is-failed tauceti-stage1-mirror.service
journalctl --user -u tauceti-stage1-mirror.service --since -1h
```

Verify the journal command actually returns this user's entries on the target
host. Where the user journal filter is unavailable, use the tested host journal
selector `journalctl _SYSTEMD_USER_UNIT=tauceti-stage1-mirror.service --since -1h`.
A later successful invocation can clear current failure status; inspect history
and both main refs instead of treating one green service snapshot as continuous health.

Before rollback or maintenance that transfers source authority, first stop future
timer invocations and wait for any current service invocation to finish. With
native writes fenced, run one final authorized mirror and confirm equality before
returning GitHub merge authority:

```sh
systemctl --user disable --now tauceti-stage1-mirror.timer
# Wait for the current invocation to finish; do not race a final reconciliation.
systemctl --user start tauceti-stage1-mirror.service
systemctl --user stop tauceti-stage1-mirror.service
systemctl --user is-enabled tauceti-stage1-mirror.timer
systemctl --user is-active tauceti-stage1-mirror.service
```

The last two commands should report disabled and inactive/failed and return nonzero.
`linked`/`not-found` can appear for isolated linked test units; check that no future
timer elapse remains. Preserve
the ledger, profiles, and source refs for recovery. Re-enabling needs the reviewed
authority decision; a routine queue deployment must not restart the old writer.
