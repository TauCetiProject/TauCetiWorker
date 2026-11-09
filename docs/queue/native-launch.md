# Native queue launch profiles

This startup path is preparation for the isolated Forgejo rehearsal. Production
activation still requires operator recovery holds/commands, narrower worker
credentials, ingress bounds, Review publication integration, and the full hosted
CI/fault/load evidence described in [Forgejo preparation](../forgejo-preparation.md).
Use isolated rehearsal destinations while those gates remain open.

The ordinary queue CLI keeps its GitHub configuration path. An explicit private
profile selects the existing native bootstrap:

```sh
tauceti-queue check-config --native-launch /absolute/private/launch.json --json
tauceti-queue serve --native-launch /absolute/private/launch.json
```

`--config` and `--native-launch` are mutually exclusive. Native startup takes no
`TAUCETI_QUEUE_*` overrides and never discovers source or execution credentials
through `gh`, a PAT environment variable, or a default credential directory.
Transport proxy and TLS trust settings still follow the process environment.

The profile is one JSON object with exactly these fields:

| Field | Value |
| --- | --- |
| `schema` | `tauceti-queue.native-launch/v1` |
| `queue_configuration` | Absolute path to the approved queue TOML |
| `queue_configuration_sha256` | SHA-256 of those exact TOML bytes |
| `project_configuration` | Absolute path to the approved project TOML |
| `project_configuration_sha256` | SHA-256 of those exact project bytes |
| `execution_policy` | Approved `ExecutionPolicy.record(with_config=False)` object |
| `workflow_revision` | Immutable GitHub execution caller commit, 40 hex characters |
| `publishers` | Object with `generation`, `reviews`, `statuses`, and `recoveries` |
| `control_policy_file` | Absolute path to the existing source-bound control policy |
| `source_credentials` | Object with `origin`, `api_token_file`, and `git_token_file` |
| `source_webhook_secret_file` | Absolute path to the separate Forgejo webhook secret |

`publishers.generation` is the approved fresh 32-hex authority generation. Its
three roles are nonempty arrays of source-local numeric user IDs, with no
duplicates. `source_credentials.origin` has exactly `kind` and `url`; it must match
the independently approved policy before token files are read. Source API/Git
tokens and webhook secrets are visible ASCII; the webhook secret has at least
32 characters. Credentials never appear in command arguments or status.

Every referenced input uses the existing checked-descriptor private-file reader:
absolute paths, no symlink components, trusted ancestors, a private user-owned
immediate parent, and one user-owned regular file of mode 0400/0600 without extra
hard links. Each read is bounded to 128 KiB. Replace files atomically and restart
with a newly reviewed profile; the process retains its startup snapshots.
Control enrollment and credentials can rotate independently of the two TOML
digests. A restart reads their current private files and revalidates source and
role bindings; changing them requires the same trusted operator access.

The queue TOML must explicitly name `repository`, `canonical_branch`,
`content_clone`, `database`, `credentials_dir`, `log_dir`, `listen_host`,
`listen_port`, `public_url`, `admission_source`, `admission_rubrics`, and
`admission_lean_root`. Paths are absolute. Source/branch, project Lean root,
and source/execution policy must agree. The project must declare a nonempty
`review.rubrics` list matching `admission_rubrics` in the same order.
`admission_source` must be `policy`. Start rehearsal profiles with
`publication_enabled = false`; persisted runtime controls keep their existing
precedence after startup.

`credentials_dir` is the GitHub execution App directory, containing the existing
`github-app.json` and `github-private-key.pem` files. The RSA key is parsed locally;
the execution App needs no webhook secret on this path.
The execution provider mints tokens restricted to the approved immutable execution
repository ID and requests only contents/actions write permissions. Source
credentials are loaded independently. Live native startup checks their effective
source repository identity, actor, write/admin access, enrolled publisher roles,
and exclusion from queue-control enrollment before constructing the controller.

`check-config` proves local configuration consistency, not live source permissions
or GitHub dispatch readiness. Execution API preflight remains part of dispatch.
`/status` identifies both qualified repositories and the pinned workflow revision.
It reports `feeds: null`: GitHub compatibility feeds reject native requests with
503 pending qualified feed support. The native configuration report identifies
`/webhook/forgejo` as the source webhook. It emits JSON in both display modes.
Dispatch requires the pinned workflow revision to remain the execution default
branch tip; a push there requires a new approved profile and service restart.

Operator commands (`settings`, `import-bors`, `reconcile-approvals`) still select
GitHub credentials. They are not native administration interfaces; native
token-file selection and recovery commands remain activation prerequisites.

Run `python tests/queue_native_launch.py` for file/binding/CLI checks.
`tests/integration_forgejo_native_launch.py` starts and restarts the actual CLI on
disposable HTTPS Forgejo while blocking legacy/shared-state network paths and
`gh` discovery. Set `FORGEJO_BINARY`, optionally `FORGEJO_STATIC_ROOT`, and
`FORGEJO_EFFECTIVE_ACCESS=1` for the private API build. Stock Forgejo rejects
startup without switching to GitHub. This startup fixture performs no hosted CI.
