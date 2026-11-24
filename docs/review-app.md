# The review posting App

GitHub accepts about 500 new or edited comments an hour from one identity. Every worker posts its
reviews as the worker account, so on a busy day review publication waits on that limit. The
`tauceti-review-poster` GitHub App is a second identity: workers post a fixed share of GitHub
reviews through an installation token of the App, and the rest as the account.

## What changes when it is enabled

- `tauceti_worker/review_app.py` assigns each TauCeti PR to the App or the account by a hash of
  its number (`TAUCETI_REVIEW_APP_SHARE`, default 0.5). Every worker posts a PR's threads and
  scoreboard as the same identity, so no PR alternates between them.
- For a routed review the engine still runs as the account, so its reads and claims are
  unchanged. The worker sets `TAUCETI_REVIEW_APP_PUBLISH=1` for it, and the worker's `gh` proxy
  then answers the engine's identity query with the App's bot and sends each publication request
  (comment creates, edits and deletes, and thread resolution) with the App's installation token.
  Fixes, rebases, labels, reactions, the TauCetiData archive, Forgejo and Bubble reviews stay on
  the account.
- The scoreboard's `submitted_by` stays the account, so reviewer affinity still matches.
- Installation tokens are cached per host in a 0600 file under the shared GitHub state directory,
  keyed by App, installation and key, and replaced before each send once fewer than 10 minutes
  remain, or after GitHub answers 401. Every token maps to one pacing identity, so the proxy paces,
  retains and replays the App's writes as one GitHub identity, and the round start drains the
  App's retained writes as it does the account's.
- Retained review output is recovered by the identity that rendered it: the receipt's actor, or
  the review context's publisher before a receipt exists.
- On a PR the App takes over, the App posts its own current-head scoreboard and threads. Readers
  order scoreboards by update time, so the newest wins; the account's older comments stay but no
  longer decide anything. The labels, readiness, survey and merge queue read scoreboards whoever
  wrote them. Housekeeping's destructive checks accept the App's bot by login
  (TauCetiTools `pr_status/core.py`).
- If the App's credentials or token are unavailable, a routed review is deferred (NoProgress) and
  not charged to the PR. Unset the variable to send every review through the account again.

## Setup

An organization owner registers and installs the App once; a posting host does the rest with
`scripts/tauceti_review_app.py` (`manifest`, `convert`, `installation`, `verify`, `copy`). The App
asks for pull requests and issues write, and contents, checks, statuses and metadata read, with no
webhook. Its key lives in `~/.config/tauceti-review-poster/` (directory 0700, files 0600) on each
posting host. Enable it per host, live, with

    tauceti-fleet workers HOST --env TAUCETI_REVIEW_APP_DIR=/home/USER/.config/tauceti-review-poster

Workers pick the change up between rounds; `--unset-env TAUCETI_REVIEW_APP_DIR` turns it off.
