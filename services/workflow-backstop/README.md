# Workflow scheduling backstop

GitHub can stop delivering scheduled events even while a workflow remains enabled
and its manual and push triggers work. This optional, independent clock previews
or dispatches overdue maintenance workflows on the repository's current default
branch. Existing GitHub schedules remain enabled.

The default set is dependency updates (hourly), conflict checks (15 minutes),
merge sweep (hourly), API docs (hourly), and website publication (three hours).
The alert reconciler is excluded by default: it edits and sends Zulip alerts.
`--include-alerts` is an explicit opt-in for an operator authorized to run it.

```sh
tauceti-workflow-backstop --repo TauCetiProject/TauCeti --state /path/to/state.json
```

The default command is a read-only preview. It writes no state or GitHub data.
`--execute` permits dispatch and stores accepted requests in the state file.
The helper uses the existing `gh` authentication; it neither changes accounts nor
creates credentials. Each read/dispatch command has a 45-second bound.

A recent attempt or any queued, waiting or running attempt suppresses another
dispatch. An overdue workflow gets explicit active-status probes, so an older
active run outside the latest history page is preserved. Workflow files that an
operator disabled are left disabled. Missing or inconsistent observations defer
that workflow while unrelated work continues. No run is cancelled, rerun, or
modified, and worker/CI admissions are not changed.

Accepted requests are recorded immediately using an atomic write. An uncertain
POST result keeps a five-minute cooldown and is re-observed before another
attempt. GitHub trigger races can still create a duplicate queued run; the
workflow's own concurrency controls remain authoritative. This is not an
exactly-once delivery guarantee. State pins the repository ID so a transferred or
replaced repository cannot silently inherit dispatch authority.

The example systemd user unit and timer are **not installed or enabled by this
package**. Review the policy, install a reviewed revision into an isolated venv,
and point `~/.local/share/tauceti/workflow-backstop/current` to that venv. Confirm
that `gh` authenticates as the intended operator in the user-service environment.
Then copy the units into `~/.config/systemd/user/`, run `systemctl --user
daemon-reload`, and enable the timer only when deployment is authorized.

The timer is independent of worker processes. Installing it does not restart
workers or interrupt paid work. A deployment must verify accepted workflow runs
and useful completions, not just that the timer is active. Journal output is JSON
with each workflow's decision; corrupt state requires operator inspection rather
than being silently discarded.

This backstop does not pretend that `workflow_dispatch` is a native `schedule`
event. Existing native-cron diagnostics remain unchanged, and a GitHub delivery
incident should still be escalated to GitHub Support.
