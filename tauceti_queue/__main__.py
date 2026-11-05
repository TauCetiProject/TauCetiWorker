"""`tauceti-queue`: the queue service process and its operator commands.

    tauceti-queue serve [--config FILE]
        Load the configuration, open the store, authenticate the GitHub App, build the controller,
        start the HTTP server and the reconciliation loop; record the running revision and start
        time for `/status`. SIGTERM or SIGINT finishes the current reconciliation step, stops the
        server and closes the store before exiting 0.
    tauceti-queue status [--url URL] [--json] [--wait SECONDS] [--expect-revision SHA]
        Print the status document (or its one-screen summary). `--wait` retries until the service
        answers; `--expect-revision` exits 2 when the running revision differs, which is how
        `services/queue/install.sh` verifies a deployment.
    tauceti-queue check-config [--config FILE] [--json] [--print KEY]
        Print the effective configuration with the source of every value and the problems that
        would stop `serve`; exit 1 when there are any.
    tauceti-queue settings show [--url URL]
    tauceti-queue settings set KEY VALUE [--url URL] [--token TOKEN]
        Read or change the runtime controls through the service's admin endpoint. The token is the
        operator's GitHub token (`--token`, `GITHUB_TOKEN`, or `gh auth token`); the service checks
        repository-admin permission with the same predicate as the security audit.
    tauceti-queue import-bors --feed URL|FILE [--candidates URL|FILE ...] [--dry-run] [--release-head]
        The cutover import (`tauceti_queue.bors_import`): read Bors's observation feed and candidate
        journals, post them to the service's `/admin/import-bors` endpoint, print what was (or, with
        `--dry-run`, would be) imported, kept, refused and recorded as Bors's running head.
    tauceti-queue reconcile-approvals [--url URL] [--token TOKEN]
        One approval reconciliation pass now (`tauceti_queue.approval_poll`), through
        `/admin/reconcile-approvals`; prints the pass summary.

The controller, store, loop and GitHub adapters are the deliverables of other streams
(`controller.py`, `store.py`, `loop.py`, `github_app.py`, `github_adapter.py`, `server.py`). They
are imported with guards: while absent, `serve` runs the status-only fallback server from
`runtime.py` so the host, unit, tunnel and install procedure can be verified ahead of them, and
`/status` reports `controller: unavailable` with the reason.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any

from . import config as configuration
from . import runtime as rt
from .config import Config, ConfigError

PROGRAM = "tauceti-queue"
DEFAULT_STOP_GRACE = 90.0  # seconds to wait for the loop's current step before exiting anyway


def _log(message: str) -> None:
    print(f"{PROGRAM}: {message}", file=sys.stderr, flush=True)


def _optional(module: str) -> ModuleType | None:
    """Import a sibling module that another stream delivers; None while it does not exist."""
    try:
        return importlib.import_module(f"tauceti_queue.{module}")
    except ModuleNotFoundError as exc:
        if exc.name in (f"tauceti_queue.{module}", "tauceti_queue"):
            return None
        raise


# --- serve -----------------------------------------------------------------------------------------


class Service:
    """The wired process: configuration, runtime facts, and whichever components are installed."""

    def __init__(self, config: Config, runtime: rt.ServiceRuntime) -> None:
        self.config = config
        self.runtime = runtime
        self.stop = rt.StopSignal()
        self.store: Any = None
        self.github: Any = None
        self.app: Any = None
        self.controller: Any = None
        self.server: Any = None
        self.poller: Any = None
        self.loop_module: ModuleType | None = None
        self.unavailable_reason: str | None = None

    # Stream E and F integration points. Each is a small function so reconciling an interface
    # change is one edit here.

    def open_store(self) -> None:
        module = _optional("store")
        if module is None:
            self.unavailable_reason = "tauceti_queue.store is not installed"
            return
        self.config.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        opener = getattr(module, "open", None) or module.Store
        self.store = opener(self.config.database)

    def open_github(self) -> None:
        app_module, adapter_module = _optional("github_app"), _optional("github_adapter")
        if app_module is None or adapter_module is None:
            self.unavailable_reason = (
                self.unavailable_reason or "tauceti_queue.github_app/github_adapter are not installed"
            )
            return
        try:
            app = app_module.GitHubApp.load(self.config.credentials_dir)
        except Exception as error:  # noqa: BLE001 - status reports the reason instead of the process dying
            self.unavailable_reason = self.unavailable_reason or f"GitHub App credentials unavailable: {error}"
            return
        self.app = app
        self.github = adapter_module.GitHubAdapter(app, self.config.repository, clone_dir=self.config.content_clone)

    def build_controller(self) -> None:
        module = _optional("controller")
        if module is None or self.store is None or self.github is None:
            self.unavailable_reason = self.unavailable_reason or "tauceti_queue.controller is not installed"
            return
        clone = self.config.content_clone
        if not (clone / "HEAD").exists() and not (clone / ".git").exists():
            self.unavailable_reason = f"content clone missing at {clone}; see docs/queue/operations.md"
            return
        self.controller = module.Controller(
            self.store,
            self.github,
            str(clone),
            self.config.repository,
            revision=self.runtime.revision.sha,
            canonical_branch=self.config.canonical_branch or None,
        )
        # Configuration supplies the initial runtime controls; the admin endpoint changes them later.
        for key, value in {
            **self.config.settings.as_dict(),
            "publication_enabled": self.config.publication_enabled,
        }.items():
            try:
                self.controller.set_setting(key, value, "configuration")
            except Exception as error:  # noqa: BLE001 - an unknown key in the file is reported, not fatal
                _log(f"configuration setting {key} not applied: {error}")
        self.loop_module = _optional("loop")
        poll_module = _optional("approval_poll")
        if poll_module is not None and hasattr(self.github, "list_open_pull_requests"):
            self.poller = poll_module.ApprovalPoller(
                self.controller, self.github, self.config.repository, review_app_id=self.config.review_app_id
            )

    def status_document(self, *, wait: float | None = None) -> dict[str, Any]:
        """The status envelope; `wait` bounds the wait for the controller lock (`Controller.status`)."""
        controller_status = None
        if self.controller is not None:
            controller_status = self.controller.status() if wait is None else self.controller.status(wait=wait)
        return rt.status_document(
            self.runtime, self.config.settings.as_dict(), controller_status, self.unavailable_reason
        )

    def effective_settings(self) -> dict[str, int]:
        if self.controller is not None:
            return dict(self.controller.status().get("settings") or self.config.settings.as_dict())
        return self.config.settings.as_dict()

    def start_server(self) -> None:
        module = _optional("server")
        if module is not None and self.controller is not None:
            self.server = module.create_server(
                self.controller,
                self.config,
                status=self.status_document,
                adapter=self.github,
                app=self.app,
                poller=self.poller,
            )
        else:
            self.server = rt.fallback_server(self.config, self.status_document, self.effective_settings)
        threading.Thread(target=self.server.serve_forever, name="http", daemon=True).start()

    def run_loop(self) -> None:
        """Reconcile until stopped; the current step always completes before we return."""
        if self.loop_module is not None and self.controller is not None:
            loop = self.loop_module.ReconciliationLoop(self.controller)
            loop.start()
            poll_loop = None
            poll_module = _optional("approval_poll")
            if self.poller is not None and poll_module is not None:
                poll_loop = poll_module.ApprovalPollLoop(self.poller)
                poll_loop.start()
            try:
                while not self.stop.wait(self.config.reconcile_interval_seconds):
                    pass
            finally:
                if poll_loop is not None:
                    poll_loop.stop(timeout=DEFAULT_STOP_GRACE)
                loop.stop(timeout=DEFAULT_STOP_GRACE)
            return
        while not self.stop.wait(self.config.reconcile_interval_seconds):
            pass

    def shutdown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            close = getattr(self.server, "server_close", None)
            if close:
                close()
        if self.store is not None:
            self.store.close()


def _install_signal_handlers(stop: rt.StopSignal) -> None:
    def handler(signum: int, _frame: Any) -> None:
        stop.set(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def cmd_serve(args: argparse.Namespace) -> int:
    config = configuration.load(args.config)
    problems = configuration.validate(config)
    if problems:
        for problem in problems:
            _log(f"configuration problem: {problem}")
        return 1
    if not config.canonical_branch:
        config.canonical_branch = configuration.discover_canonical_branch(config)
        config.sources["canonical_branch"] = "discovered"
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = rt.ServiceRuntime.start(config)
    service = Service(config, runtime)
    _install_signal_handlers(service.stop)
    _log(
        f"revision {runtime.revision.sha}{' (dirty)' if runtime.revision.dirty else ''} "
        f"({runtime.revision.source}) started {runtime.started_at} on {config.host}; "
        f"{config.repository}@{config.canonical_branch}; mode {config.mode}"
    )
    service.open_store()
    service.open_github()
    service.build_controller()
    if service.controller is None:
        _log(f"controller unavailable: {service.unavailable_reason}; serving status only")
    service.start_server()
    _log(f"listening on {config.listen_url} (public {config.public_url})")
    try:
        service.run_loop()
    finally:
        _log(f"stopping ({service.stop.reason or 'loop ended'})")
        service.shutdown()
        _log("stopped")
    return 0


# --- status ----------------------------------------------------------------------------------------


def fetch_json(
    url: str, timeout: float = 10, method: str = "GET", body: Any = None, token: str | None = None
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        with exc:
            detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = {"error": detail.strip()}
        raise RuntimeError(
            f"{method} {url}: HTTP {exc.code}: {parsed.get('detail') or parsed.get('error') or detail}"
        ) from None


def _status_url(args: argparse.Namespace) -> str:
    base = args.url
    if not base:
        base = configuration.load(getattr(args, "config", None)).listen_url
    return base.rstrip("/")


def cmd_status(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    url = _status_url(args) + "/status"
    try:
        document = rt.wait_for_status(url, args.wait, fetch)
    except RuntimeError as exc:
        _log(str(exc))
        return 1
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(rt.summarize(document))
    if args.expect_revision:
        running = str((document.get("service") or {}).get("revision") or "")
        expected = args.expect_revision.strip().lower()
        if not running or not running.startswith(expected) or len(expected) < 7:
            _log(f"running revision {running or 'unknown'} does not match expected {args.expect_revision}")
            return 2
        _log(f"running revision matches {expected}")
    return 0


# --- check-config ----------------------------------------------------------------------------------


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        config = configuration.load(args.config)
    except ConfigError as exc:
        _log(str(exc))
        return 1
    problems = configuration.validate(config)
    if args.print:
        values = config.as_dict()
        if args.print not in values:
            _log(f"unknown key {args.print!r}; known: {', '.join(sorted(values))}")
            return 1
        value = values[args.print]
        print(json.dumps(value) if isinstance(value, dict | list | bool) or value is None else value)
        return 0
    if args.json:
        print(json.dumps({**config.as_dict(), "problems": problems}, indent=2, sort_keys=True))
    else:
        print(configuration.format_effective(config, problems))
    return 1 if problems else 0


# --- settings --------------------------------------------------------------------------------------


def _operator_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("no GitHub token: pass --token, set GITHUB_TOKEN, or log in with gh") from None
    return token.strip()


def cmd_settings(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    base = _status_url(args)
    try:
        if args.action == "show":
            document = fetch(base + "/settings")
            print(json.dumps(document, indent=2, sort_keys=True))
            return 0
        if args.key not in configuration.SETTING_FIELDS:
            _log(f"unknown setting {args.key!r}; known: {', '.join(configuration.SETTING_FIELDS)}")
            return 1
        try:
            value = int(args.value)
        except ValueError:
            _log(f"{args.key} takes an integer, got {args.value!r}")
            return 1
        if value < 1:
            _log(f"{args.key} must be at least 1")
            return 1
        document = fetch(
            base + "/admin/settings", method="POST", body={args.key: value}, token=_operator_token(args.token)
        )
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        _log(str(exc))
        return 1


# --- import-bors and reconcile-approvals -----------------------------------------------------------


def cmd_import_bors(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    from . import bors_import

    base = _status_url(args)
    try:
        observation = bors_import.load_document(args.feed)
        candidates = [bors_import.reduce_candidates(bors_import.load_document(spec)) for spec in args.candidates or []]
    except (OSError, ValueError) as exc:
        _log(f"cannot read the feed: {exc}")
        return 1
    body = {
        "observation": observation,
        "candidates": candidates,
        "dry_run": bool(args.dry_run),
        "release_head": bool(args.release_head),
    }
    try:
        report = fetch(
            base + "/admin/import-bors", method="POST", body=body, token=_operator_token(args.token), timeout=3600
        )
    except RuntimeError as exc:
        _log(str(exc))
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(bors_import.format_report(report))
    return 0


def cmd_reconcile_approvals(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    base = _status_url(args)
    try:
        summary = fetch(
            base + "/admin/reconcile-approvals", method="POST", body={}, token=_operator_token(args.token), timeout=3600
        )
    except RuntimeError as exc:
        _log(str(exc))
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


# --- parser ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Tau Ceti AI merge queue service")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the service")
    serve.add_argument(
        "--config", help=f"TOML file (default ${configuration.CONFIG_ENV} or {configuration.DEFAULT_CONFIG_PATH})"
    )

    status = commands.add_parser("status", help="print the status document or its summary")
    status.add_argument("--url", help="service base URL (default: the configured listen address)")
    status.add_argument("--config", help="configuration file used to find the listen address")
    status.add_argument("--json", action="store_true", help="print the full document instead of the summary")
    status.add_argument("--wait", type=float, default=0.0, help="seconds to keep retrying until the service answers")
    status.add_argument("--expect-revision", help="exit 2 unless the running revision starts with this SHA")

    check = commands.add_parser("check-config", help="print the effective configuration and its problems")
    check.add_argument("--config", help="TOML file")
    check.add_argument("--json", action="store_true")
    check.add_argument("--print", metavar="KEY", help="print one effective value and nothing else")

    settings = commands.add_parser("settings", help="read or change the runtime controls")
    settings.add_argument("--url", help="service base URL (default: the configured listen address)")
    settings.add_argument("--config", help="configuration file used to find the listen address")
    actions = settings.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="print the effective settings")
    setter = actions.add_parser("set", help="change one setting through the admin endpoint")
    setter.add_argument("key", choices=configuration.SETTING_FIELDS)
    setter.add_argument("value")
    setter.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")

    importer = commands.add_parser("import-bors", help="import Bors's approved PRs and running head at the cutover")
    importer.add_argument("--feed", required=True, help="Bors's observation feed (active-batches): a URL or a file")
    importer.add_argument(
        "--candidates",
        action="append",
        metavar="URL|FILE",
        help="a candidate journal for admission ages, reviewers and priorities; may be repeated",
    )
    importer.add_argument("--dry-run", action="store_true", help="validate and report without recording anything")
    importer.add_argument(
        "--release-head",
        action="store_true",
        help="release the members of Bors's running head to approved inputs instead of waiting for its landing",
    )
    importer.add_argument("--url", help="service base URL (default: the configured listen address)")
    importer.add_argument("--config", help="configuration file used to find the listen address")
    importer.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")
    importer.add_argument("--json", action="store_true", help="print the full report instead of the summary")

    reconcile = commands.add_parser("reconcile-approvals", help="run one approval reconciliation pass now")
    reconcile.add_argument("--url", help="service base URL (default: the configured listen address)")
    reconcile.add_argument("--config", help="configuration file used to find the listen address")
    reconcile.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "check-config":
            return cmd_check_config(args)
        if args.command == "settings":
            return cmd_settings(args)
        if args.command == "import-bors":
            return cmd_import_bors(args)
        if args.command == "reconcile-approvals":
            return cmd_reconcile_approvals(args)
    except ConfigError as exc:
        _log(str(exc))
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
