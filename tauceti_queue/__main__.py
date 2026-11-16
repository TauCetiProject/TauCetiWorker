"""`tauceti-queue`: the queue service process and its operator commands.

    tauceti-queue serve [--config FILE]
        Load the configuration, open the store, authenticate the GitHub App, build the controller,
        seed the store with the file's runtime controls (only for keys the store has no value for,
        or whose stored value is an earlier seed; an operator's value is never replaced), start the
        HTTP server and the reconciliation loop; record the running revision and start
        time for `/status`. SIGTERM or SIGINT finishes the current reconciliation step, stops the
        server and closes the store before exiting 0.
    tauceti-queue status [--url URL] [--json] [--wait SECONDS] [--expect-revision SHA]
        Print the status document (or its one-screen summary). `--wait` retries until the service
        answers; `--expect-revision` exits 2 when the running revision differs, which is how
        `services/queue/install.sh` verifies a deployment.
    tauceti-queue check-config [--config FILE] [--json] [--print KEY]
        Print the effective configuration with the source of every value and the problems that
        would stop `serve`; exit 1 when there are any. For the runtime controls (`[settings]` and
        `publication_enabled`) it reads the store without writing and shows the effective value:
        the store's, with the actor that wrote it, or the file's while the store has none.
    tauceti-queue settings show [--url URL]
    tauceti-queue settings [--url URL] set KEY VALUE [--token TOKEN | --token-file FILE]
        Read or change the runtime controls through the service's admin endpoint. KEY is any key of
        the controller's `DEFAULT_SETTINGS`; VALUE is parsed by the type of that key's initial value
        (a non-negative integer, at least 1 for the `[settings]` keys of the configuration file,
        `true`/`false`, or text). The command posts `{"KEY": VALUE}` to `/admin/settings`, whose body
        is one JSON object of setting keys to values. The token is the operator's GitHub token
        (`--token`, `GITHUB_TOKEN`, or `gh auth token`); the service checks repository-admin
        permission with the same predicate as the security audit. `--token-file` instead reads a
        private dedicated bearer, requires an explicit queue origin and refuses redirects. Native
        controls still require source-bound enrollment and current source admin permission.
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
from functools import partial
from pathlib import Path
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

    def __init__(self, config: Config, runtime: rt.ServiceRuntime, *, native=None) -> None:
        self.config = config
        self.runtime = runtime
        self.stop = rt.StopSignal()
        self.store: Any = None
        self.github: Any = None
        self.app: Any = None
        self.controller: Any = None
        self.server: Any = None
        self.poller: Any = None
        self.forgejo_poller: Any = None
        self.loop_module: ModuleType | None = None
        self.unavailable_reason: str | None = None
        self.native = native
        if native is not None and config.forgejo_mode != "disabled":
            raise ValueError("native-only launch cannot also enable a mixed source")
        self.native_http = None
        self._native_store_lock = None
        self._native_listener = None
        if native is not None:
            from .native_bootstrap import NativeBootstrap

            if not isinstance(native, NativeBootstrap):
                raise ValueError("native service requires an explicit trusted bootstrap")

    # Stream E and F integration points. Each is a small function so reconciling an interface
    # change is one edit here.

    def open_store(self) -> None:
        # Reserve the existing store before a constructor can commit any
        # binding or publisher rotation. Two queue services must not share
        # a writable queue, even if they listen on different ports.
        import fcntl
        import stat

        database = self.config.database.resolve()
        database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(database) + ".native-service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError("native service lock must be an owned private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._native_store_lock = fd
        module = _optional("store")
        if module is None:
            self.unavailable_reason = "tauceti_queue.store is not installed"
            return
        self.config.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        opener = getattr(module, "open", None) or module.Store
        self.store = opener(self.config.database)
        if self.store.get_setting("queue:operating-mode") == "stage1-operator":
            self.store.close()
            self.store = None
            raise ValueError("Stage 1 operator ledger cannot be opened by the queue service")

    def open_github(self) -> None:
        if self.native is not None:
            return  # execution credentials were explicitly supplied by the trusted bootstrap
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
        if self.native is not None:
            if self.store is None:
                raise ValueError("native service requires its existing durable queue store")
            import socket

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.config.listen_host, self.config.listen_port))
                listener.listen()
                wired = self.native.build(self.config, self.store, revision=self.runtime.revision.sha)
            except BaseException:
                listener.close()
                raise
            self._native_listener = listener
            self.github, self.controller, self.poller, self.native_http = (
                wired.source,
                wired.controller,
                wired.poller,
                wired.http,
            )
            self.native_http.prebound_socket = listener
            self.loop_module = _optional("loop")
            return
        module = _optional("controller")
        if module is None or self.store is None or self.github is None:
            self.unavailable_reason = self.unavailable_reason or "tauceti_queue.controller is not installed"
            return
        clone = self.config.content_clone
        if not (clone / "HEAD").exists() and not (clone / ".git").exists():
            self.unavailable_reason = f"content clone missing at {clone}; see docs/queue/operations.md"
            return
        mixed = None
        if self.config.forgejo_mode != "disabled":
            from .mixed_profile import MixedProfile
            from .mixed_sources import MixedSources

            profile = MixedProfile.load(self.config.forgejo_profile, self.config)
            mixed = MixedSources(profile, self.config, self.store, self.github, clone)
        self.controller = module.Controller(
            self.store,
            self.github,
            str(clone),
            self.config.repository,
            revision=self.runtime.revision.sha,
            canonical_branch=self.config.canonical_branch or None,
            mixed=mixed,
        )
        self.loop_module = _optional("loop")
        poll_module = _optional("approval_poll")
        if poll_module is not None and hasattr(self.github, "list_open_pull_requests"):
            if self.config.admission_source == "policy":
                from .admission import AdmissionPolicy, AdmissionPoller, policy

                root = self.config.admission_lean_root
                self.poller = AdmissionPoller(
                    self.controller,
                    self.github,
                    self.config.repository,
                    AdmissionPolicy(
                        rubrics=self.config.admission_rubrics.split(","),
                        repository=self.config.repository,
                        prefix=root.replace(".", "/") + "/",
                        allow_files=policy.default_allow_files(root)
                        + [s for s in self.config.admission_extra_files.split(",") if s],
                    ),
                )
            else:
                self.poller = poll_module.ApprovalPoller(
                    self.controller, self.github, self.config.repository, review_app_id=self.config.review_app_id
                )

        if mixed is not None:
            self.forgejo_poller = mixed.connect(self.controller, self.poller)

    def seed_settings(self) -> None:
        """Seed the store with the configuration file's runtime controls (`serve` only).

        A file value is written only when the store has no value for the key, or when the stored
        value is an earlier seed the file has since changed; a value an operator set through
        `/admin/settings` or `tauceti-queue settings set` is kept (`Controller.seed_setting`)."""
        if self.controller is None:
            return
        for key, value in configuration.runtime_settings(self.config).items():
            try:
                if self.controller.seed_setting(key, value):
                    _log(f"configuration seeded {key} = {value!r}")
            except Exception as error:  # noqa: BLE001 - an unknown key in the file is reported, not fatal
                _log(f"configuration setting {key} not applied: {error}")

    def status_document(self, *, wait: float | None = None) -> dict[str, Any]:
        """The status envelope; `wait` bounds the wait for the controller lock (`Controller.status`)."""
        controller_status = None
        if self.controller is not None:
            controller_status = self.controller.status() if wait is None else self.controller.status(wait=wait)
        document = rt.status_document(
            self.runtime, self.config.settings.as_dict(), controller_status, self.unavailable_reason
        )
        document["forgejo"] = {"mode": self.config.forgejo_mode}
        if self.forgejo_poller is not None:
            document["forgejo"].update(
                source=self.forgejo_poller.registry.profile.forgejo.record(),
                observation=self.forgejo_poller.last_summary,
            )
        if self.native is not None:
            policy = self.native.execution_policy
            document["service"].update(
                source_repository=policy.source.record(),
                execution_repository=policy.execution.record(),
                workflow_revision=self.native.workflow_revision,
            )
            # Compatibility feeds use the GitHub ID namespace and are refused
            # by the native server until qualified feeds are implemented.
            document["feeds"] = None
        return document

    def effective_settings(self) -> dict[str, int]:
        if self.controller is not None:
            return dict(self.controller.status().get("settings") or self.config.settings.as_dict())
        return self.config.settings.as_dict()

    def start_server(self) -> None:
        module = _optional("server")
        if module is not None and self.controller is not None:
            self.server = module.create_server(
                self.controller,
                self.native_http if self.native_http is not None else self.config,
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
            native_loop = None
            if self.forgejo_poller is not None and poll_module is not None:
                native_loop = poll_module.ApprovalPollLoop(self.forgejo_poller)
                native_loop.start()
            try:
                while not self.stop.wait(self.config.reconcile_interval_seconds):
                    pass
            finally:
                if native_loop is not None:
                    native_loop.stop(timeout=DEFAULT_STOP_GRACE)
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
        if self._native_listener is not None:
            self._native_listener.close()
            self._native_listener = None
        if self._native_store_lock is not None:
            os.close(self._native_store_lock)
            self._native_store_lock = None


def _install_signal_handlers(stop: rt.StopSignal) -> None:
    def handler(signum: int, _frame: Any) -> None:
        stop.set(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def cmd_serve(args: argparse.Namespace) -> int:
    launch = None
    if getattr(args, "native_launch", None):
        from .native_launch import NativeLaunch

        launch = NativeLaunch.load(args.native_launch)
        config = launch.config
    else:
        config = configuration.load(args.config)
    problems = configuration.validate(config, check_credentials=launch is None)
    if problems:
        for problem in problems:
            _log(f"configuration problem: {problem}")
        return 1
    if not config.canonical_branch:
        config.canonical_branch = configuration.discover_canonical_branch(config)
        config.sources["canonical_branch"] = "discovered"
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = rt.ServiceRuntime.start(config)
    service = Service(config, runtime, native=launch.bootstrap) if launch else Service(config, runtime)
    _install_signal_handlers(service.stop)
    _log(
        f"revision {runtime.revision.sha}{' (dirty)' if runtime.revision.dirty else ''} "
        f"({runtime.revision.source}) started {runtime.started_at} on {config.host}; "
        f"{config.repository}@{config.canonical_branch}; mode {config.mode}"
    )
    try:
        service.open_store()
        service.open_github()
        service.build_controller()
        service.seed_settings()
        if service.controller is None:
            _log(f"controller unavailable: {service.unavailable_reason}; serving status only")
        service.start_server()
        _log(f"listening on {config.listen_url} (public {config.public_url})")
        service.run_loop()
    except Exception as error:
        if launch is None:
            raise
        from .control_policy_file import ControlPolicyError
        from .source_authority import SourceAccessDenied, SourceAuthorityUnavailable

        detail = type(error).__name__
        if isinstance(error, SourceAccessDenied | SourceAuthorityUnavailable | ControlPolicyError):
            detail = str(error)  # These exceptions contain fixed local diagnostics.
        elif isinstance(error, OSError):
            import errno

            detail = errno.errorcode.get(error.errno, "OSError")
        _log(f"native queue startup/runtime failed ({detail})")
        return 1
    finally:
        _log(f"stopping ({service.stop.reason or 'loop ended'})")
        service.shutdown()
        _log("stopped")
    return 0


# --- status ----------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(
    url: str,
    timeout: float = 10,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
    *,
    allow_redirects: bool = True,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.urlopen
        if not allow_redirects:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if not allow_redirects:
            exc.close()
            raise RuntimeError(f"operator request failed (HTTP {exc.code})") from None
        with exc:
            detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = {"error": detail.strip()}
        raise RuntimeError(
            f"{method} {url}: HTTP {exc.code}: {parsed.get('detail') or parsed.get('error') or detail}"
        ) from None
    except (OSError, ValueError):
        if allow_redirects:
            raise
        raise RuntimeError("operator request unavailable or returned invalid JSON") from None


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


def _stored_settings(config: Config) -> dict[str, dict[str, Any]] | None:
    """The store's settings rows, read without writing; None when there is no store yet."""
    module = _optional("store")
    reader = getattr(module, "read_settings", None)
    if reader is None or not config.database.is_file():
        return None
    return reader(str(config.database))


def cmd_check_config(args: argparse.Namespace) -> int:
    launch = None
    try:
        if getattr(args, "native_launch", None):
            from .native_launch import NativeLaunch

            launch = NativeLaunch.load(args.native_launch)
            config = launch.config
        else:
            config = configuration.load(args.config)
    except ConfigError as exc:
        _log(str(exc))
        return 1
    problems = configuration.validate(config, check_credentials=launch is None)
    if config.forgejo_mode in {"observe", "admit"}:
        from .mixed_profile import MixedProfile

        try:
            MixedProfile.load(config.forgejo_profile, config)
        except ConfigError as error:
            problems.append(str(error))
    stored = _stored_settings(config)
    values = config.as_dict()
    if launch:
        values["webhook_url"] = config.public_url.rstrip("/") + "/webhook/forgejo"
        values.pop("review_app_id", None)
        values["sources"].pop("review_app_id", None)
    if args.print:
        if args.print not in values:
            _log(f"unknown key {args.print!r}; known: {', '.join(sorted(values))}")
            return 1
        value = values[args.print]
        print(json.dumps(value) if isinstance(value, dict | list | bool) or value is None else value)
        return 0
    if args.json or launch:
        document = {
            **values,
            "effective_settings": configuration.effective_runtime_settings(config, stored),
            "problems": problems,
        }
        if launch:
            document["native"] = launch.summary()
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(configuration.format_effective(config, problems, stored))
    return 1 if problems else 0


# --- settings --------------------------------------------------------------------------------------


def _operator_token(explicit: str | None, token_file: Path | None = None) -> str:
    if token_file is not None:
        from .control_policy_file import ControlPolicyError, read_control_policy

        try:
            token = read_control_policy(token_file).strip()
            if not 0 < len(token) <= 4096 or any(not 33 <= char <= 126 for char in token):
                raise ValueError
            return token.decode("ascii")
        except ControlPolicyError as error:
            raise RuntimeError(str(error)) from None
        except ValueError:
            raise RuntimeError("operator token file is unavailable or invalid") from None
    if explicit:
        return explicit
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("no GitHub token: pass --token, set GITHUB_TOKEN, or log in with gh") from None
    return token.strip()


def _operator_target(args, fetch):
    """File credentials require an explicit origin and never follow redirects."""
    if getattr(args, "token_file", None) is not None:
        from tauceti_tools.forge import Forge

        try:
            if not args.url:
                raise ValueError
            origin = Forge("forgejo", args.url).url
        except ValueError:
            raise RuntimeError("--token-file requires an explicit HTTPS --url (HTTP allowed on loopback)") from None
        return origin, partial(fetch, allow_redirects=False)
    return _status_url(args), fetch


def settable_settings() -> dict[str, Any]:
    """Every runtime setting `settings set` may change, with its initial value: the controller's
    `DEFAULT_SETTINGS` (the configuration file's `[settings]` when the controller is not installed)."""
    try:
        from .controller import DEFAULT_SETTINGS
    except ImportError:
        return configuration.Settings().as_dict()
    return dict(DEFAULT_SETTINGS)


_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def parse_setting_value(key: str, raw: str) -> Any:
    """`raw` as the type of `key`'s initial value; raises ValueError with a message for the operator."""
    initial = settable_settings()[key]
    if isinstance(initial, bool):
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise ValueError(f"{key} takes true or false, got {raw!r}")
    if isinstance(initial, int):
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{key} takes an integer, got {raw!r}") from None
        minimum = 1 if key in configuration.SETTING_FIELDS else 0
        if value < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
        return value
    return raw


def cmd_settings(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    try:
        base, fetch = _operator_target(args, fetch)
        if args.action == "show":
            document = fetch(base + "/settings")
            print(json.dumps(document, indent=2, sort_keys=True))
            return 0
        known = settable_settings()
        if args.key not in known:
            _log(f"unknown setting {args.key!r}; known: {', '.join(known)}")
            return 1
        try:
            value = parse_setting_value(args.key, args.value)
        except ValueError as exc:
            _log(str(exc))
            return 1
        document = fetch(
            base + "/admin/settings",
            method="POST",
            body={args.key: value},
            token=_operator_token(args.token, getattr(args, "token_file", None)),
        )
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        _log(str(exc))
        return 1


# --- import-bors and reconcile-approvals -----------------------------------------------------------


def cmd_import_bors(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_json) -> int:
    from . import bors_import

    try:
        base, fetch = _operator_target(args, fetch)
    except RuntimeError as exc:
        _log(str(exc))
        return 1
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
            base + "/admin/import-bors",
            method="POST",
            body=body,
            token=_operator_token(args.token, getattr(args, "token_file", None)),
            timeout=3600,
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
    try:
        base, fetch = _operator_target(args, fetch)
        summary = fetch(
            base + "/admin/reconcile-approvals",
            method="POST",
            body={},
            token=_operator_token(args.token, getattr(args, "token_file", None)),
            timeout=3600,
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
    serve_source = serve.add_mutually_exclusive_group()
    serve_source.add_argument(
        "--config", help=f"TOML file (default ${configuration.CONFIG_ENV} or {configuration.DEFAULT_CONFIG_PATH})"
    )
    serve_source.add_argument("--native-launch", type=Path, help="explicit private Forgejo launch profile")

    status = commands.add_parser("status", help="print the status document or its summary")
    status.add_argument("--url", help="service base URL (default: the configured listen address)")
    status.add_argument("--config", help="configuration file used to find the listen address")
    status.add_argument("--json", action="store_true", help="print the full document instead of the summary")
    status.add_argument("--wait", type=float, default=0.0, help="seconds to keep retrying until the service answers")
    status.add_argument("--expect-revision", help="exit 2 unless the running revision starts with this SHA")

    check = commands.add_parser("check-config", help="print the effective configuration and its problems")
    check_source = check.add_mutually_exclusive_group()
    check_source.add_argument("--config", help="TOML file")
    check_source.add_argument("--native-launch", type=Path, help="validate an explicit private Forgejo launch profile")
    check.add_argument("--json", action="store_true")
    check.add_argument("--print", metavar="KEY", help="print one effective value and nothing else")

    settings = commands.add_parser("settings", help="read or change the runtime controls")
    settings.add_argument("--url", help="service base URL (default: the configured listen address)")
    settings.add_argument("--config", help="configuration file used to find the listen address")
    actions = settings.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="print the effective settings")
    setter = actions.add_parser(
        "set",
        help="change one setting through the admin endpoint",
        description="Post {KEY: VALUE} to /admin/settings. KEY is any controller setting; VALUE is parsed "
        "by the type of its initial value (integer, true/false, or text).",
    )
    setter.add_argument("key", choices=sorted(settable_settings()), metavar="KEY", help="the setting to change")
    setter.add_argument("value", metavar="VALUE", help="the new value")
    setter_token = setter.add_mutually_exclusive_group()
    setter_token.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")
    setter_token.add_argument(
        "--token-file", type=Path, help="private operator bearer file; requires explicit --url; no credential discovery"
    )

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
    importer_token = importer.add_mutually_exclusive_group()
    importer_token.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")
    importer_token.add_argument(
        "--token-file", type=Path, help="private operator bearer file; requires explicit --url; no credential discovery"
    )
    importer.add_argument("--json", action="store_true", help="print the full report instead of the summary")

    reconcile = commands.add_parser("reconcile-approvals", help="run one approval reconciliation pass now")
    reconcile.add_argument("--url", help="service base URL (default: the configured listen address)")
    reconcile.add_argument("--config", help="configuration file used to find the listen address")
    reconcile_token = reconcile.add_mutually_exclusive_group()
    reconcile_token.add_argument("--token", help="operator GitHub token (default: GITHUB_TOKEN, then gh auth token)")
    reconcile_token.add_argument(
        "--token-file", type=Path, help="private operator bearer file; requires explicit --url; no credential discovery"
    )
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
