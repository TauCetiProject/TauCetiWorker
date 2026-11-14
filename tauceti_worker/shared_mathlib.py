"""One immutable Mathlib per host version, shared by every round through a per-round view.

Layout (``LAYOUT`` under the pool, written only by trusted publication code in this module):

- ``sources/<rev>/<package>/``: the source tree of Mathlib ``rev`` and of every package in its
  dependency closure (Batteries, Aesop, Qq, ProofWidgets, ...), detached at the pinned commit with
  ``origin`` set to the URL Lake expects. Files and directories are read-only. Each package has an
  empty, read-only ``.lake/build``: the mount point of its build inside a view.
- ``entries/<key>/build/<package>/``: the extracted build of every closure package for one build
  key (revision, toolchain, ABI, platform, options), with ``ready.json`` recording its inventory.
- ``locks/``, ``staging/``, ``trash/``.

A checkout attaches by symlinking ``.lake/packages/<package>`` to ``sources/<rev>/<package>``; Lake
resolves the link, so every Lake run, inside a view or not, sees the shared source and refuses a
revision or URL mismatch instead of fetching (the tree is read-only). A view is a rootless mount
namespace that overlays each package's published build onto its read-only ``.lake/build``, with a
private upper under ``.lake/tauceti-mathlib/<generation>/`` that the next round discards. Outside a
view the build directories are empty and read-only, so a build fails rather than copying Mathlib.

Agent workspaces are NEVER publication sources. Locks outside entries protect publication and
leases against eviction and collection; no root or daemon.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .paths import HERE
from .processes import run as run_process
from .runtime_status import atomic_json, read_json


class Unavailable(RuntimeError):
    pass


class NoFallback(Unavailable):
    """Sharing is impossible for a reason that must never lead to a private Mathlib build."""


class PinRefused(NoFallback):
    """The checkout pins a Mathlib revision outside the host's allowed versions."""


class CapContention(NoFallback):
    """Waiting for the version cap took too long; the round stops instead of building privately."""


class LayoutRefused(NoFallback):
    """The checkout's .lake cannot carry the shared layout (a linked directory); it never will."""


class Disabled(NoFallback):
    """An operator switched sharing off: rounds that need Mathlib do not run on this host."""


class IntegrityFailure(Unavailable):
    pass


LOCK_WAIT_SECONDS = 1500  # fresh extraction can take up to 1200 seconds
SOURCE_FETCH_SECONDS = 900
LAYOUT = "v4"  # pool subdirectory of the current layout; anything else in the pool is legacy
META = "tauceti-mathlib"  # private per-checkout view metadata under .lake/
REVISION = re.compile(r"[0-9a-f]{40}")
KEY = re.compile(r"[0-9a-f]{64}")
# Environment that selects a different compiler or compiler options for Mathlib's modules.
BUILD_ENVIRONMENT = ("LAKE_OVERRIDE_LEAN", "LEAN", "LEAN_SYSROOT", "LEAN_GITHASH", "LEAN_CC", "LEAN_AR")


def disk_reserve(usage):
    # A percentage alone reserves hundreds of GiB on large fleet disks and
    # disables sharing precisely when removing duplicate builds would help.
    # Keep the small-filesystem policy, capped at 20 GiB of actual free space.
    return min(int(usage.total * 0.1), 20 * 1024**3)


def root(env=None):
    env = os.environ if env is None else env
    login = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if value := env.get("TAUCETI_SHARED_MATHLIB_ROOT"):
        return (login / value[2:] if value.startswith("~/") else Path(value)).resolve()
    return (login / ".cache/tauceti/mathlib-builds").resolve()


def layout(pool):
    return Path(pool) / LAYOUT


def source_dir(P, revision):
    return Path(P) / "sources" / revision


def pin_root(env):
    env.setdefault("TAUCETI_SHARED_MATHLIB_ROOT", str(root(env)))


def selection(env):
    """("required", reason), or Unavailable explaining why rounds that need Mathlib cannot run.

    Sharing is the only way a round gets Mathlib. ``auto`` is an old spelling of ``required``.
    ``off`` (environment or policy) is an operator's emergency switch: it blocks those rounds,
    it never selects a private build."""
    pool = root(env)
    path = pool / "policy.json"
    policy = read_json(path)
    if path.exists() and (
        not policy
        or policy.get("mode", "required") not in ("off", "auto", "required")
        or not isinstance(policy.get("expires_at", 0), (int, float))
        or not math.isfinite(policy.get("expires_at", 0))
        or policy.get("expiry_mode", "required") not in ("off", "auto", "required")
        or not isinstance(policy.get("workers", []), list)
        or any(not isinstance(worker, str) for worker in policy.get("workers", []))
    ):
        raise Unavailable("invalid shared Mathlib policy; repair policy.json")
    expired = policy.get("expires_at", float("inf")) <= time.time()
    value = env.get(
        "TAUCETI_SHARED_MATHLIB_BUILDS",
        policy.get("expiry_mode", "required") if expired else policy.get("mode", "required"),
    ).lower()
    if value not in {"auto", "off", "required"}:
        raise Unavailable("TAUCETI_SHARED_MATHLIB_BUILDS must be required or off")
    if value == "off":
        where = (
            "TAUCETI_SHARED_MATHLIB_BUILDS=off"
            if "TAUCETI_SHARED_MATHLIB_BUILDS" in env
            else "policy.json expired into mode off"
            if expired
            else "policy.json mode off"
        )
        raise Disabled(
            f"shared Mathlib is switched off on this host ({where}); rounds that need Mathlib do not run"
            " until an operator restores sharing"
        )
    allowed = env.get("TAUCETI_SHARED_MATHLIB_WORKERS", ",".join(policy["workers"]) if "workers" in policy else None)
    if allowed is not None and env.get("TAUCETI_WORKER_ID", "default") not in allowed.split(","):
        raise Unavailable("worker is not allowlisted for shared Mathlib, which is required; fix the policy")
    stop_path = pool / "safety-stop.json"
    stop = read_json(stop_path)
    reason = stop.get("reason") or policy.get("safety_stop")
    if stop_path.exists() and (not isinstance(reason, str) or not reason):
        reason = "unreadable safety-stop.json"
    if reason:
        raise Unavailable(
            "shared Mathlib safety stop: "
            + reason
            + "; investigate before clearing safety-stop.json and restoring policy"
        )
    return "required", "sharing enabled"


def mode(env):
    return selection(env)[0]


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def overlay_preload():
    """Stock bwrap has no overlay-option flag; keep the cache-only shim local.

    Cached beside the pool (``~/.cache/tauceti`` by default), so a pinned test pool never touches
    the host's real cache."""
    source = HERE / "scripts/OverlayMount.c"
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = root().parent / "overlay-mount"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / (key + ".so")
    with (cache / "build.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            compiler = shutil.which("cc")
            if not compiler:
                raise Unavailable("sharing needs a C compiler once to build the cache overlay helper; install cc")
            with tempfile.TemporaryDirectory(dir=cache, prefix="build-") as temporary:
                built = Path(temporary) / "overlay.so"
                result = subprocess.run(
                    [compiler, "-shared", "-fPIC", "-O2", str(source), "-o", str(built), "-ldl"],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode:
                    raise Unavailable(
                        "cannot compile cache overlay helper: " + result.stderr.decode(errors="replace")[-400:]
                    )
                os.replace(built, target)
    return target


def view_command(layers, pool, *, lease_fd=None):
    """A private mount namespace: the pool read-only, then one overlay per ``(lower, upper, work,
    target)`` layer. bwrap applies mounts in argument order, so an overlay whose target lies in
    the pool stacks on top of the read-only bind.

    Deliberately no PID namespace: agents and their tools must see ordinary host PIDs and /proc.
    Tools that report a PID to a service outside the namespace (systemd, D-Bus) break otherwise."""
    original = os.environ.get("LD_PRELOAD")
    preload = str(overlay_preload()) + (":" + original if original else "")
    overlays = []
    for lower, upper, work, target in layers:
        overlays += ["--overlay-src", str(lower), "--overlay", str(upper), str(work), str(target)]
    return [
        "env",
        "LD_PRELOAD=" + preload,
        "bwrap",
        *(["--setenv", "LD_PRELOAD", original] if original else ["--unsetenv", "LD_PRELOAD"]),
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--ro-bind",
        str(pool),
        str(pool),
        *(["--sync-fd", str(lease_fd)] if lease_fd is not None else []),
        *overlays,
        "--",
    ]


def overlay_command(lower, upper, work, target, pool, *, lease_fd=None):
    return view_command([(lower, upper, work, target)], pool, lease_fd=lease_fd)


def capability(directory=None):
    """Exercise the actual filesystem, namespace, OverlayFS and bwrap flag support."""
    if platform.system() != "Linux":
        return (
            False,
            "Linux user namespaces and OverlayFS are required for shared Mathlib; this host runs no rounds that need Mathlib",
        )
    if not shutil.which("bwrap"):
        return False, "install bubblewrap with --overlay support (0.12 or newer) to share extracted Mathlib"
    try:
        with tempfile.TemporaryDirectory(prefix="tauceti-overlay-probe-", dir=directory) as raw:
            here = Path(raw)
            lower, upper, work = (here / n for n in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            (lower / "probe").write_text("shared")
            command = overlay_command(lower, upper, work, upper, lower)
            p = subprocess.run(
                command
                + [
                    "sh",
                    "-c",
                    'test "$(cat "$1/probe")" = shared && printf private > "$1/probe" '
                    '&& ! (printf poison > "$2/probe")',
                    "sh",
                    str(upper),
                    str(lower),
                ],
                capture_output=True,
                timeout=10,
            )
            if (lower / "probe").read_text() != "shared":
                return (
                    False,
                    "isolation failure: overlay probe modified its immutable lower; stop sharing and investigate",
                )
            if p.returncode or (upper / "probe").read_text() != "private":
                return (
                    False,
                    "rootless OverlayFS probe failed: "
                    + p.stderr.decode(errors="replace")[-400:].strip()
                    + "; administrator may need to allow unprivileged user namespaces or provide an OverlayFS-capable filesystem",
                )
        return True, "rootless OverlayFS works; immutable lower with private copy-on-write builds"
    except (Unavailable, OSError, subprocess.TimeoutExpired) as error:
        return (
            False,
            f"rootless OverlayFS unavailable: {error}; use a local OverlayFS-capable filesystem and allow user namespaces",
        )


def git(path, *args):
    p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise Unavailable("cannot identify clean dependency sources: " + p.stderr.strip()[-200:])
    return p.stdout.strip()


def optional_git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else None


# -- The dependency graph a checkout pins ---------------------------------------------------------


def _manifest(cwd):
    try:
        data = json.loads((Path(cwd) / "lake-manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise Unavailable(f"cannot read lake-manifest.json: {error}") from error
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise Unavailable("lake-manifest.json has no package list")
    by_name = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise Unavailable("lake-manifest.json has a malformed package entry")
        by_name[package["name"]] = package
    return data, by_name


def _pinned(package, name):
    """The full commit a git dependency is pinned to; anything else cannot be shared."""
    if (
        Path(name).name != name
        or name in ("", ".", "..")
        or package.get("type") != "git"
        or not isinstance(package.get("rev"), str)
        or not REVISION.fullmatch(package["rev"])
        or not isinstance(package.get("url"), str)
        or not package["url"]
        or package.get("subDir") not in (None, "")
    ):
        raise Unavailable(
            f"dependency {name!r} is not a git package pinned to a full commit; shared Mathlib cannot attach it"
        )
    return package["rev"]


def expected_url(name, url, env):
    """The URL Lake requires as ``origin`` (Materialize.lean): ``LAKE_PKG_URL_MAP``, else the
    manifest's, realpath-resolved when it names an existing path."""
    if mapping := env.get("LAKE_PKG_URL_MAP"):
        try:
            mapped = json.loads(mapping)
        except ValueError as error:
            raise Unavailable("LAKE_PKG_URL_MAP is not valid JSON") from error
        if isinstance(mapped, dict) and isinstance(mapped.get(name), str):
            url = mapped[name]
    return os.path.realpath(url) if os.path.exists(url) else url


def resolve(cwd, env):
    """Mathlib's pin from the checkout's tracked lake-manifest.json: no Lake, no network.

    Only the layout attachment produces is accepted: packages under ``.lake/packages`` and no
    workspace override for any package (Lake would then load that one instead)."""
    data, packages = _manifest(cwd)
    if data.get("packagesDir") not in (None, ".lake/packages"):
        raise LayoutRefused(
            "lake-manifest.json moves packagesDir away from .lake/packages; shared Mathlib cannot attach"
        )
    overrides = Path(cwd) / ".lake/package-overrides.json"
    if overrides.exists() or overrides.is_symlink():
        raise LayoutRefused(f"{overrides} overrides dependency packages; remove it to use shared Mathlib")
    if "mathlib" not in packages:
        raise Unavailable("lake-manifest.json names no Mathlib package; shared Mathlib cannot attach")
    revision = _pinned(packages["mathlib"], "mathlib")
    toolchain = Path(cwd) / "lean-toolchain"
    return dict(
        rev=revision,
        manifest=data,
        packages=packages,
        toolchain=toolchain.read_text().strip() if toolchain.is_file() else None,
    )


DEPENDENCY_FILES = ("lake-manifest.json", "lean-toolchain", "lakefile.toml", "lakefile.lean")
BUMP_PIN_SECONDS = 7 * 86400


def _manifest_pin(text):
    try:
        packages = json.loads(text).get("packages", [])
    except (ValueError, AttributeError, TypeError):
        return None
    return next((p.get("rev") for p in packages if isinstance(p, dict) and p.get("name") == "mathlib"), None)


_PURPOSE = {}


def set_round_purpose(checkout, *, rebase=False, bump=False, repair=False):
    """Record what the current round in ``checkout`` is for. Keyed by the checkout, so cache
    preparation for any other directory (verification jobs, recovery) is unaffected.

    ``repair`` (fix, fix-ci, lint-repair) prepares main's Mathlib only when the branch's own pin
    is refused: its agent is then told by ``lake`` to integrate main first, instead of the round
    backing off on that PR forever."""
    _PURPOSE.clear()
    if checkout is not None and (rebase or bump or repair):
        _PURPOSE[Path(checkout).resolve()] = dict(rebase=rebase, bump=bump, repair=repair)


def _purpose(cwd):
    with contextlib.suppress(OSError):
        return _PURPOSE.get(Path(cwd).resolve(), {})
    return {}


@contextlib.contextmanager
def rebase_target(cwd, env):
    """For a round whose job is to rebase onto main, prepare Mathlib for main, not the branch.

    The branch is on an old Mathlib by definition, and that pin is refused. While the shared view
    is chosen and the dependency checkouts are synchronized, the workspace carries origin/main's
    dependency files; ``restore()`` puts the branch's own files back before the agent starts.
    After the agent's rebase the pins agree with the shared build. Until then the agent's
    ``lake`` refuses to run (scripts/lake-guard.py), so it cannot compile the old Mathlib."""
    restored = [True]

    def restore():
        if restored[0]:
            return
        for name, data in saved.items():
            if data is None:
                (cwd / name).unlink(missing_ok=True)
            else:
                (cwd / name).write_bytes(data)
        restored[0] = True

    saved = {}
    purpose = _purpose(cwd)
    if purpose.get("rebase") or purpose.get("repair"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["git", "-C", str(cwd), "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL,
            )
        target = {}
        for name in DEPENDENCY_FILES:
            shown = subprocess.run(
                ["git", "-C", str(cwd), "show", f"origin/main:{name}"], capture_output=True, timeout=30
            )
            target[name] = shown.stdout if shown.returncode == 0 else None
        try:
            current = (cwd / "lake-manifest.json").read_text()
        except OSError:
            current = None
        main_pin = target["lake-manifest.json"] and _manifest_pin(target["lake-manifest.json"])
        branch_pin = _manifest_pin(current)
        if main_pin and branch_pin != main_pin and not purpose.get("rebase"):
            allowed = allowed_revisions(cwd)
            if allowed is None or branch_pin in allowed | bump_pins(root(env)):
                main_pin = None  # a repair on an allowed pin keeps its own Mathlib
        if main_pin and branch_pin != main_pin:
            saved = {name: (cwd / name).read_bytes() if (cwd / name).is_file() else None for name in DEPENDENCY_FILES}
            restored[0] = False
            from .config import log

            log(
                f"shared Mathlib: preparing main's Mathlib {main_pin[:12]} for this "
                + ("rebase round" if purpose.get("rebase") else "repair round; its pin is refused")
            )
            for name, data in target.items():
                if data is None:
                    (cwd / name).unlink(missing_ok=True)
                else:
                    (cwd / name).write_bytes(data)
    try:
        yield restore
    finally:
        restore()


def bump_pins(pool):
    """Mathlib pins that bump rounds used recently: the next main, so never refused or evicted first."""
    pins = set()
    with contextlib.suppress(OSError):
        for path in (pool / "bump-pins").iterdir():
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime < BUMP_PIN_SECONDS:
                    pins.add(path.name)
    return pins


def allowed_revisions(cwd):
    """The Mathlib revisions pinned by current origin/main and by the pin before it, or None.

    Read from the checkout's own origin/main, without Lake or network. Rounds pinned elsewhere are
    refused; a branch behind main is rebased onto it or abandoned."""

    def pin(commit):
        shown = subprocess.run(
            ["git", "-C", str(cwd), "show", f"{commit}:lake-manifest.json"], capture_output=True, text=True, timeout=30
        )
        if shown.returncode:
            return None
        try:
            packages = json.loads(shown.stdout).get("packages", [])
        except (ValueError, AttributeError):
            return None
        return next((p.get("rev") for p in packages if isinstance(p, dict) and p.get("name") == "mathlib"), None)

    # Queue repair and other callers may not have refreshed origin/main; best effort, bounded.
    try:
        fetched = subprocess.run(
            ["git", "-C", str(cwd), "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main"],
            capture_output=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if fetched.returncode:
        return None  # a stale origin/main must not decide which pins are current
    current = pin("origin/main")
    if not current:
        return None
    allowed = {current}
    history = subprocess.run(
        ["git", "-C", str(cwd), "log", "--format=%H", "-n", "200", "origin/main", "--", "lake-manifest.json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    commits = history.stdout.split() if history.returncode == 0 else []
    for commit in commits:
        revision = pin(commit)
        if revision and revision != current:
            allowed.add(revision)
            return allowed
    if len(commits) >= 200:
        return None  # the previous pin lies beyond the search: unknown, never refused
    shallow = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True, timeout=30
    )
    if history.returncode or shallow.stdout.strip() != "false":
        return None  # too little history to know the previous pin: refuse nothing, the cap still holds
    return allowed


def _holders(lock_path):
    """Processes of this user, other than this one, with ``lock_path`` open."""
    target = os.path.realpath(lock_path)
    uid, me, pids = os.getuid(), os.getpid(), set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid != uid or int(process.name) == me:
                continue
            for fd in (process / "fd").iterdir():
                with contextlib.suppress(OSError):
                    if os.readlink(fd) == target:
                        pids.add(int(process.name))
                        break
        except (OSError, ValueError):
            continue
    return pids


def is_round_argv(argv):
    """`<python> [-I …] -m tauceti_worker _round …`, as spawn_round() invokes a round."""
    try:
        index = argv.index("-m")
    except ValueError:
        return False
    return argv[index + 1 : index + 3] == ["tauceti_worker", "_round"] and all(a.startswith("-") for a in argv[1:index])


def _owning_round(pid):
    """The nearest ancestor (or self) that is an isolated worker round, or None.

    A round is its own session and process-group leader (spawn_round uses start_new_session), so
    signalling its group reaches only that round; anything else is signalled alone."""
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            argv = [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
            if is_round_argv(argv):
                return pid if os.getpgid(pid) == pid == os.getsid(pid) else None
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    return None


def _kill_holders(pids):
    """Abandon what uses an evicted entry, TERM then KILL.

    A holder inside a worker round loses its whole round (the round's own session and process
    group, never the worker loop's). Any other holder, such as a queue service running repairs
    in-process, is signalled alone, never its group."""
    import signal

    groups, singles = set(), set()
    for pid in pids:
        owner = _owning_round(pid)
        with contextlib.suppress(OSError):
            if owner is not None:
                groups.add(os.getpgid(owner))
            else:
                singles.add(pid)
    groups.discard(os.getpgrp())
    singles.discard(os.getpid())
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for group in groups:
            with contextlib.suppress(OSError):
                os.killpg(group, signum)
        for pid in singles:
            with contextlib.suppress(OSError):
                os.kill(pid, signum)
        deadline = time.monotonic() + EVICTION_GRACE
        while signum == signal.SIGTERM and time.monotonic() < deadline:
            if not any(Path(f"/proc/{pid}").exists() for pid in pids):
                return
            time.sleep(0.5)


VERSION_CAP = 2  # Mathlib versions a host may hold at once: source trees, builds and legacy entries
EVICTION_GRACE = 20  # seconds between SIGTERM and SIGKILL for rounds holding an evicted version
EVICTION_WAIT = 40  # each of three waits for an evicted version's exclusive locks


def _entry_revision(entry):
    return (read_json(entry / "ready.json").get("spec") or {}).get("mathlib")


def _age(path):
    with contextlib.suppress(OSError):
        return path.stat().st_mtime
    return 0


@contextlib.contextmanager
def _versions_lock(P):
    """The pool-wide cap lock; a wait that times out stops the round rather than going private."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(key_lock(P, "versions", exclusive=True, timeout=LOCK_WAIT_SECONDS))
        except IntegrityFailure:
            raise
        except Unavailable as error:
            raise CapContention(f"waiting for the Mathlib version cap timed out: {error}") from error
        yield


def _entries(P):
    entries = Path(P) / "entries"
    if not entries.is_dir():
        return []
    return [e for e in entries.iterdir() if e.is_dir() and not e.is_symlink()]


def _versions(P):
    """{revision: last use} for every version present: a source tree or a build of it. Staging is
    not counted: publication is serialized under ``versions`` and evicts first, and a crashed
    publisher's stale staging directory must not force a live version out."""
    present = {}
    sources = Path(P) / "sources"
    if sources.is_dir():
        for source in sources.iterdir():
            if REVISION.fullmatch(source.name) and source.is_dir() and not source.is_symlink():
                present[source.name] = max(present.get(source.name, 0), _age(source))
    for entry in _entries(P):
        if revision := _entry_revision(entry):
            present[revision] = max(present.get(revision, 0), _age(entry))
    return present


def _legacy_entries(pool):
    """Builds of an older pool layout. They are deleted, never migrated."""
    entries = Path(pool) / "entries"
    if not entries.is_dir():
        return []
    return [e for e in entries.iterdir() if KEY.fullmatch(e.name) and e.is_dir() and not e.is_symlink()]


def _excess(P, pool, keep):
    others = {r: t for r, t in _versions(P).items() if r != keep}
    legacy = _legacy_entries(pool)
    return len(others) + len(legacy) + 1 - VERSION_CAP, others, legacy


def enforce_version_cap(P, keep, allowed, env, *, pool=None):
    """Hold at most VERSION_CAP versions, counting ``keep``, the revision about to be used.

    Legacy-layout builds go first, then versions outside ``allowed``, then the oldest. A round
    holding an evicted version is killed and abandoned: keeping a third Mathlib is never the
    better outcome. Source publication calls ``_evict_excess`` itself, holding ``versions``."""
    pool = Path(P).parent if pool is None else Path(pool)
    _collect_legacy(pool)
    if _excess(P, pool, keep)[0] <= 0:
        return  # the common case takes no lock
    with _versions_lock(P):
        _evict_excess(P, pool, keep, allowed, env)


def _evict_excess(P, pool, keep, allowed, env):
    """Called holding the ``versions`` lock."""
    excess, others, legacy = _excess(P, pool, keep)
    if excess <= 0:
        return
    for entry in sorted(legacy, key=_age)[:excess]:
        report(pool, env, mode="evicting", key=entry.name, reason="legacy shared Mathlib layout")
        _retire(pool, entry.name, entry)
        excess -= 1
    ranked = sorted(others, key=lambda r: (allowed is None or r in allowed, others[r]))
    for revision in ranked[:excess]:
        _evict_version(P, pool, revision, env)


def _hide(P, name, path):
    """Rename ``path`` into trash as ``<name>-<32 hex>``: a reader arriving later finds nothing."""
    trash = Path(P) / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    retired = trash / (name + "-" + uuid.uuid4().hex)
    if path.is_dir() and not path.is_symlink():
        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | 0o700)  # renaming a directory updates its ".."
    os.replace(path, retired)
    return retired


def _retire(base, lock_name, path):
    """Hide, then delete once every holder of ``lock_name`` is gone, killing their rounds.

    Overlays already mounted keep their lower through file descriptors: kill those rounds,
    rescanning while waiting, then delete under the exclusive lock."""
    retired = _hide(base, lock_name, path)
    lock_path = Path(base) / "locks" / (lock_name + ".lock")
    for attempt in range(3):
        if lock_path.exists():
            _kill_holders(_holders(lock_path))
        try:
            with key_lock(base, lock_name, exclusive=True, timeout=EVICTION_WAIT):
                remove_tree(retired)
            return
        except IntegrityFailure:
            raise
        except Unavailable as error:
            if attempt == 2:
                raise CapContention(f"evicting Mathlib {lock_name[:20]} timed out: {error}") from error


def _evict_version(P, pool, revision, env):
    """Delete a version: every build of it, then its source tree."""
    report(pool, env, mode="evicting", rev=revision, reason="Mathlib version cap")
    for entry in _entries(P):
        if _entry_revision(entry) == revision:
            _retire(P, entry.name, entry)
    source = source_dir(P, revision)
    if source.exists() or source.is_symlink():
        _retire(P, "source-" + revision, source)


def remove_tree(path):
    """Restore owner write and search on directories (read-only trees, OverlayFS work/work at
    mode 000) without following links, then delete."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    os.chmod(path, 0o700)
    for here, directories, _ in os.walk(path, followlinks=False):
        for name in directories:
            child = Path(here) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(path)


# -- Source trees ---------------------------------------------------------------------------------


def _source_ready(P, revision):
    """The published closure of ``revision`` ({package: {rev, url}}), or None."""
    source = source_dir(P, revision)
    if source.is_symlink() or not source.is_dir():
        return None
    meta = read_json(source / "source.json")
    packages = meta.get("packages")
    if meta.get("rev") != revision or not isinstance(packages, dict) or "mathlib" not in packages:
        return None
    return packages


def _freeze(tree, *, prune=None):
    """Remove every write bit under ``tree`` (not following links), directories last."""
    directories = []
    for here, names, files in os.walk(tree, followlinks=False):
        if prune is not None:
            names[:] = [n for n in names if Path(here) / n != prune]
        for name in files:
            path = Path(here) / name
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.lstat().st_mode) & ~0o222)
        directories.append(Path(here))
    for directory in reversed(directories):
        if not directory.is_symlink():
            os.chmod(directory, 0o555)


def _fetch_package(destination, name, revision, url, seeds, env):
    """A shallow checkout of ``revision`` whose ``origin`` is ``url``; local seeds before the network."""
    destination.mkdir(parents=True)
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    git_env.pop("GIT_DIR", None)
    git_env.pop("GIT_WORK_TREE", None)

    def run(*args, timeout=60):
        return subprocess.run(
            ["git", "-C", str(destination), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_env,
            stdin=subprocess.DEVNULL,
        )

    for args in (["init", "-q"], ["remote", "add", "origin", url]):
        if (result := run(*args)).returncode:
            raise Unavailable(f"cannot prepare the shared {name} source: {result.stderr.strip()[-200:]}")
    failures = []
    for seed in [*seeds, url]:
        try:
            fetched = run(
                "fetch", "--quiet", "--depth", "1", "--no-tags", str(seed), revision, timeout=SOURCE_FETCH_SECONDS
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{seed}: timed out")
            continue
        if fetched.returncode == 0 and optional_git(destination, "rev-parse", "FETCH_HEAD^{commit}") == revision:
            break
        failures.append(f"{seed}: {fetched.stderr.strip()[-200:]}")
    else:
        raise Unavailable(f"cannot fetch {name} {revision[:12]} for the shared source: " + "; ".join(failures)[-400:])
    result = run("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", revision, timeout=300)
    if result.returncode:
        raise Unavailable(f"cannot check out the shared {name} source: {result.stderr.strip()[-200:]}")
    (destination / ".lake/build").mkdir(parents=True)


def _seeds(P, cwd, name, revision):
    """Local repositories that may already hold ``revision`` of ``name``."""
    candidates = [Path(cwd) / ".lake/packages" / name]
    sources = Path(P) / "sources"
    if sources.is_dir():
        candidates += [s / name for s in sources.iterdir() if s.name != revision]
    return [c for c in candidates if not c.is_symlink() and (c / ".git").exists()]


def ensure_source(P, pool, cwd, graph, allowed, env):
    """The closure ({package: {rev, url}}) of the shared source tree for ``graph``'s Mathlib.

    A new version is admitted under the cap first (evicting another before any byte of this one
    is written), then published by rename. Every closure package must be pinned by the checkout
    exactly as Mathlib's own manifest pins it: another graph cannot share Mathlib."""
    revision = graph["rev"]
    closure = _source_ready(P, revision)
    if closure is None:
        with key_lock(P, f"source-{revision}-publisher", timeout=LOCK_WAIT_SECONDS):
            closure = _source_ready(P, revision)
            if closure is None:
                with _versions_lock(P):
                    _collect_legacy(pool)
                    _evict_excess(P, pool, revision, allowed, env)
                    closure = _publish_source(P, cwd, graph, env)
    else:
        enforce_version_cap(P, revision, allowed, env, pool=pool)
    for name, package in closure.items():
        pinned = graph["packages"].get(name)
        if pinned is None:
            raise Unavailable(f"lake-manifest.json lacks {name}, which Mathlib {revision[:12]} requires")
        if _pinned(pinned, name) != package["rev"]:
            raise Unavailable(
                f"{name} is pinned to {pinned['rev'][:12]} but Mathlib {revision[:12]} requires"
                f" {package['rev'][:12]}; restore the pins Mathlib's manifest gives before building"
            )
        if expected_url(name, pinned["url"], env) != package["url"]:
            raise Unavailable(
                f"{name}'s URL in lake-manifest.json differs from the host's shared source; restore main's"
                " lake-manifest.json"
            )
    return closure


def _publish_source(P, cwd, graph, env):
    """Called under the source publisher lock and ``versions``."""
    revision, pinned = graph["rev"], graph["packages"]
    staging = Path(P) / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(P)
    if usage.free < disk_reserve(usage):
        raise Unavailable("shared Mathlib pool is below its free-space reserve (10%, capped at 20 GiB)")
    with tempfile.TemporaryDirectory(prefix=f"source-{revision}-", dir=staging) as raw:
        tree = Path(raw) / "tree"
        tree.mkdir()
        closure = {"mathlib": dict(rev=revision, url=expected_url("mathlib", pinned["mathlib"]["url"], env))}
        _fetch_package(
            tree / "mathlib", "mathlib", revision, closure["mathlib"]["url"], _seeds(P, cwd, "mathlib", revision), env
        )
        try:
            requirements = json.loads((tree / "mathlib/lake-manifest.json").read_text()).get("packages", [])
        except (OSError, ValueError, AttributeError) as error:
            raise Unavailable(f"Mathlib {revision[:12]} has no readable lake-manifest.json: {error}") from error
        for package in requirements:
            name = package.get("name") if isinstance(package, dict) else None
            if not isinstance(name, str):
                raise Unavailable(f"Mathlib {revision[:12]}'s lake-manifest.json is malformed")
            required = _pinned(package, name)
            if name not in pinned:
                raise Unavailable(f"lake-manifest.json lacks {name}, which Mathlib {revision[:12]} requires")
            if _pinned(pinned[name], name) != required:
                raise Unavailable(
                    f"{name} is pinned to {pinned[name]['rev'][:12]} but Mathlib {revision[:12]} requires"
                    f" {required[:12]}; restore the pins Mathlib's manifest gives before building"
                )
            closure[name] = dict(rev=required, url=expected_url(name, pinned[name]["url"], env))
            _fetch_package(tree / name, name, required, closure[name]["url"], _seeds(P, cwd, name, required), env)
        for name in closure:
            package = tree / name
            _freeze(package, prune=package / ".git")
            # chmod changed every ctime; refresh the index now so Lake's `git diff` never needs to.
            subprocess.run(
                ["git", "-C", str(package), "update-index", "-q", "--refresh"], capture_output=True, timeout=300
            )
        atomic_json(tree / "source.json", dict(rev=revision, packages=closure, published_at=time.time()))
        _freeze(tree)
        os.chmod(tree, 0o755)  # renaming a directory updates its ".."
        target = source_dir(P, revision)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tree, target)
        os.chmod(target, 0o555)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return closure


def _meta(cwd):
    return Path(cwd) / ".lake" / META


def attach(cwd, P, revision, closure):
    """Point every closure package of the checkout at the shared source tree.

    A private package directory is retired whole (it is build state, never recovery source); a link
    elsewhere is replaced atomically. Links into the pool for packages outside this closure go, so
    Lake materializes them privately as ordinary root dependencies."""
    lake, meta = Path(cwd) / ".lake", _meta(cwd)
    packages = lake / "packages"
    for directory in (lake, packages, meta):
        if directory.is_symlink():
            raise LayoutRefused(f"{directory} is a symlink; shared Mathlib needs the checkout's own .lake")
        directory.mkdir(exist_ok=True)
    source = source_dir(P, revision)
    for name in closure:
        link, want = packages / name, str(source / name)
        if link.is_symlink():
            if os.readlink(link) == want:
                continue
        elif link.exists():
            os.replace(link, meta / ("retired-" + uuid.uuid4().hex))
        temporary = packages / f".{name}.tauceti-{uuid.uuid4().hex}"
        os.symlink(want, temporary)
        os.replace(temporary, link)
    shared = str(Path(P).parent) + os.sep
    for link in packages.iterdir():
        if link.name not in closure and link.is_symlink() and os.readlink(link).startswith(shared):
            link.unlink()
    if not layout_ok(cwd, P, revision, closure):
        raise Unavailable("the checkout's shared Mathlib layout is not the expected one; repair .lake")
    for name, package in closure.items():
        if optional_git(source / name, "rev-parse", "HEAD") != package["rev"]:
            raise IntegrityFailure(
                f"shared {name} source is not at {package['rev'][:12]}; stop sharing and investigate"
            )


def layout_ok(cwd, P, revision, names):
    """Exactly the attached layout: the checkout's own .lake, packages and metadata directories, and
    each closure package a symlink to its read-only, owner-owned shared source. Nothing else."""
    lake = Path(cwd) / ".lake"
    for directory in (lake, lake / "packages", lake / META):
        if directory.is_symlink() or not directory.is_dir():
            return False
    source = source_dir(P, revision)
    try:
        info = source.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o222:
        return False
    for name in names:
        link = lake / "packages" / name
        if not link.is_symlink() or os.readlink(link) != str(source / name):
            return False
    return True


def view_marker(cwd):
    """The current view's receipt, or {}."""
    return read_json(_meta(cwd) / "view.json")


def attached(cwd):
    """True when a closure package of the checkout is a link into a shared source tree."""
    packages = Path(cwd) / ".lake/packages"
    with contextlib.suppress(OSError):
        return any(
            p.is_symlink() and f"{os.sep}{LAYOUT}{os.sep}sources{os.sep}" in os.readlink(p) for p in packages.iterdir()
        )
    return False


# -- Builds ---------------------------------------------------------------------------------------


def signature(cwd, env, closure):
    """The build key: only what changes the closure's build outputs.

    Revisions of Mathlib and of its dependency closure, the Lean toolchain's identity (version,
    githash and Init artifact digest), the artifact formats it produces, the platform, and
    compiler-selecting environment. Not TauCeti's configuration, its other dependencies, Git remotes,
    or cache transport settings: those decide where Mathlib's archives come from, not what they hold.

    Loading Lake here (``lake env``) runs on the attached read-only sources, so it only checks them;
    root-only dependencies are materialized privately as usual."""
    lean = run_process(
        ["lake", "env", "lean", "--version"], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    if lean.returncode:
        raise Unavailable(
            "Lean toolchain is unavailable; run lake env lean --version: " + (lean.stderr or "").strip()[-300:]
        )
    prefix = run_process(
        ["lake", "env", "lean", "--print-prefix"], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    library = Path(prefix.stdout.strip()) / "lib/lean"
    if prefix.returncode or not (library / "Init.olean").is_file():
        raise Unavailable("cannot identify toolchain artifact format; run lake env lean --print-prefix")
    artifacts = [
        ".olean",
        ".ilean",
        *[
            suffix
            for suffix in (".olean.server", ".olean.private", ".ir", ".ir.sig")
            if (library / ("Init" + suffix)).is_file()
        ],
    ]
    spec = dict(
        version=4,
        artifacts=artifacts,
        mathlib=closure["mathlib"]["rev"],
        closure={name: package["rev"] for name, package in sorted(closure.items())},
        abi=lean.stdout.strip(),
        toolchain_init=hashlib.sha256((library / "Init.olean").read_bytes()).hexdigest(),
        options={name: hashlib.sha256(env[name].encode()).hexdigest() for name in BUILD_ENVIRONMENT if env.get(name)},
        architecture=platform.machine(),
        system=platform.system(),
    )
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), spec


@contextlib.contextmanager
def key_lock(pool, key, exclusive=True, nonblocking=False, timeout=None, readonly=False):
    locks = Path(pool) / "locks"
    path = locks / (key + ".lock")
    if readonly and not path.exists():
        yield None
        return
    if not readonly:
        locks.mkdir(parents=True, exist_ok=True)
    with path.open("r" if readonly else "a+") as stream:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        lease_lock(stream, flags | (fcntl.LOCK_NB if nonblocking else 0), timeout=timeout)
        if not readonly:
            yield stream
            return
    yield None  # dry-run eligibility probe; release before any recursive inspection


def lease_lock(stream, flags, *, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        try:
            fcntl.flock(stream, flags | (fcntl.LOCK_NB if deadline is not None else 0))
            return
        except BlockingIOError:
            if deadline is None:
                raise
            if time.monotonic() >= deadline:
                raise Unavailable("shared Mathlib lock wait timed out; inspect publisher/reader leases") from None
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except OSError as error:
            raise IntegrityFailure(f"shared Mathlib lease failure: {error}; stop sharing and investigate") from error


def _scratch_unused(parent):
    """Check old namespace mounts as well as ordinary process references."""
    from .work_recovery import live_users

    parent = parent.resolve()
    if live_users(parent):
        return False
    if platform.system() != "Linux":
        from .retention import mount_points

        mounts = mount_points()
        return mounts is not None and not any(point == parent or point.is_relative_to(parent) for point in mounts)
    seen = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            # A non-dumpable same-user task's proc directory can be root-owned.
            status = (process / "status").read_text()
            uid = next(line.split()[1] for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid) != os.getuid():
                continue
            if (process / "stat").read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                continue
            try:
                namespace = (process / "ns/mnt").stat().st_ino
            except PermissionError:
                # Non-dumpable same-user processes can still expose mountinfo.
                namespace = process.name
            if namespace in seen:
                continue
            seen.add(namespace)
            for line in (process / "mountinfo").read_text().splitlines():
                # Bind roots and mountpoints both follow a renamed generation.
                for path in line.split()[3:5]:
                    for escaped, literal in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
                        path = path.replace(escaped, literal)
                    point = Path(path)
                    if point == parent or point.is_relative_to(parent):
                        return False
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            if error.errno in {errno.EINVAL, errno.ESRCH}:
                continue  # A task can exit between its status and mountinfo reads.
            return False
        except (ValueError, IndexError, StopIteration):
            return False
    return True


def _discard_scratch(path):
    """Released private view state can be retried without failing a useful round."""
    from .config import log

    if not path.exists() and not path.is_symlink():
        return
    if not _scratch_unused(path):
        log(f"shared Mathlib: cleanup deferred for referenced or unverified build scratch: {path}")
        return
    try:
        remove_tree(path)
    except OSError as error:
        log(f"shared Mathlib: could not remove obsolete build scratch {path}: {error}")
        return str(error)


def discard_private_state(cwd, *, keep=None):
    """Remove earlier generations and retired private package directories that nothing uses."""
    meta = _meta(cwd)
    if meta.is_symlink() or not meta.is_dir():
        return
    try:
        children = list(meta.iterdir())
    except OSError as error:
        from .config import log

        log(f"shared Mathlib: private view state inventory deferred: {error}")
        return
    for child in children:
        if child.name == keep or child.is_symlink() or not child.is_dir():
            continue
        if re.fullmatch(r"[0-9a-f]{32}", child.name):
            # A namespace keeps its generation's lease open for as long as any process in it lives;
            # the overlay's upper and work are passed by descriptor, so mountinfo does not name them.
            try:
                with (child / "lease").open("a+") as lease:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _discard_scratch(child)
            except BlockingIOError:
                continue
            except OSError as error:
                from .config import log

                log(f"shared Mathlib: could not inspect view generation {child}: {error}")
        elif re.fullmatch(r"retired-[0-9a-f]{32}", child.name):
            _discard_scratch(child)


def upper_usage(generation):
    try:
        files = [path.lstat() for path in Path(generation).glob("*/upper/**/*")] if generation else []
        regular = [info for info in files if stat.S_ISREG(info.st_mode)]
        return dict(upper_files=len(regular), upper_bytes=sum(info.st_size for info in regular))
    except OSError:
        return dict(upper_files=None, upper_bytes=None)


def durable_tree(path):
    """Sync immutable data and directories before publishing its ready marker."""
    for here, _, files in os.walk(path, topdown=False):
        for name in files:
            file = Path(here) / name
            if file.is_symlink():
                if not file.resolve().is_relative_to(path.resolve()):
                    raise Unavailable("snapshot has a symlink outside its immutable build tree")
                continue
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(here, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def inventory(build):
    """Fingerprint trusted local files without rereading gigabytes on every hit.

    ctime detects overwrites even if size and mtime are restored. Readers cannot
    change these files through the read-only pool mount. The host owner remains
    trusted, as with the existing cache (this is not a hostile-host boundary).
    Reject links and special files, including symlinked directories.
    """
    result = {}
    device = build.stat().st_dev
    for here, directories, files in os.walk(build):
        for path in [Path(here), *(Path(here) / name for name in directories + files)]:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise Unavailable(f"unsupported snapshot file {path.relative_to(build)}")
            if info.st_dev != device or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise Unavailable(f"snapshot file has a mount or hardlink alias: {path.relative_to(build)}")
            if stat.S_ISREG(info.st_mode):
                result[str(path.relative_to(build))] = [
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
    return result


def ready_entry(entry, key, spec):
    """Called with a lease. Preserve a damaged published lower for investigation."""
    if not entry.exists():
        return False
    ready = read_json(entry / "ready.json")
    try:
        valid = (
            ready.get("key") == key
            and ready.get("spec") == spec
            and bool(ready.get("inventory"))
            and (entry / "build").is_dir()
            and not (entry / "build").is_symlink()
            and all((entry / "build" / name).is_dir() for name in spec.get("closure", {}))
            and inventory(entry / "build") == ready["inventory"]
        )
    except (OSError, Unavailable) as error:
        raise IntegrityFailure(
            f"snapshot integrity failure for {key}: {error}; lower retained for inspection"
        ) from error
    if not valid:
        raise IntegrityFailure(f"snapshot integrity failure for {key}; lower retained for inspection")
    return True


def stop_sharing(pool, reason):
    """Stop future rounds; never disturb existing leases or immutable readers."""
    from .config import log

    recorded = False
    try:
        atomic_json(pool / "safety-stop.json", dict(reason=reason, stopped_at=time.time()))
        recorded = True
    except OSError as error:
        log(f"shared Mathlib SAFETY STOP receipt could not be written: {error}")
    try:
        with key_lock(pool, "policy", timeout=1):
            policy = read_json(pool / "policy.json")
            # The stop blocks rounds through selection(); `off` would block them too, but the
            # stop must survive an operator restoring the mode.
            atomic_json(pool / "policy.json", {**policy, "safety_stop": reason})
            recorded = True
    except (OSError, Unavailable) as error:
        log(f"shared Mathlib SAFETY STOP policy could not be written: {error}")
    if recorded:
        try:
            fd = os.open(pool, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as error:
            log(f"shared Mathlib SAFETY STOP directory sync unavailable: {error}")
    return recorded


def discard_entry(pool, entry):
    """Hide an unlocked entry atomically before deletion can be interrupted."""
    retired = _hide(pool, entry.name, entry)
    for directory in (entry.parent, retired.parent):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    remove_tree(retired)


def _collect_legacy(pool, dry_run=False):
    """Delete every unleased build of an older layout. Leased ones wait for their readers, or for
    the version cap to need their slot."""
    pool = Path(pool)
    removed = []
    for directory in ("entries", "staging", "trash"):
        base = pool / directory
        if not base.is_dir():
            continue
        for item in base.iterdir():
            key = item.name[:64]
            if not KEY.fullmatch(key) or item.is_symlink():
                continue
            try:
                with key_lock(pool, key, exclusive=True, nonblocking=True, readonly=dry_run):
                    if not dry_run:
                        if directory == "entries":
                            discard_entry(pool, item)
                        else:
                            remove_tree(item)
                    removed.append(str(item))
            except (BlockingIOError, FileNotFoundError, Unavailable):
                continue
        if not dry_run:
            with contextlib.suppress(OSError):
                base.rmdir()
    return removed


def collect(pool, *, protect=(), now=None, max_age=7 * 86400, dry_run=False, held=()):
    """Paths removed: legacy-layout data, trash, dead staging, and unused builds and sources."""
    pool = Path(pool)
    if not pool.is_dir():
        return []
    removed = _collect_legacy(pool, dry_run)
    P = layout(pool)
    if not P.is_dir():
        return removed
    try:
        with key_lock(P, "gc", nonblocking=True, readonly=dry_run):
            return removed + _collect(P, protect=protect, now=now, max_age=max_age, dry_run=dry_run, held=held)
    except BlockingIOError:
        return removed


def _collect(P, *, protect=(), now=None, max_age=7 * 86400, dry_run=False, held=()):
    """Bounded by snapshot count. Locked readers/builders are never reclaimed."""
    now = time.time() if now is None else now
    removed = []
    for retired in (P / "trash").glob("*"):
        # Trash is named <lock>-<32 hex>. Its lower may still be mounted by a round that outlived an
        # eviction; delete it only while holding that lock exclusively, and skip it otherwise.
        lock = re.sub(r"-[0-9a-f]{32}$", "", retired.name)
        with contextlib.suppress(FileNotFoundError):
            if dry_run:
                continue
            if lock in held:  # the caller already holds this lock exclusively
                remove_tree(retired)
                continue
            try:
                with key_lock(P, lock, exclusive=True, nonblocking=True):
                    remove_tree(retired)
            except (BlockingIOError, Unavailable):
                continue
    # A killed builder cannot run TemporaryDirectory cleanup. Reclaim its
    # staging tree before evicting useful published snapshots for disk pressure.
    for stage in (P / "staging").glob("*"):
        if match := re.fullmatch(r"source-([0-9a-f]{40})-.*", stage.name):
            lock = f"source-{match[1]}-publisher"
        elif KEY.fullmatch(stage.name.split("-", 1)[0]):
            lock = stage.name.split("-", 1)[0]
        else:
            continue
        if lock in protect:
            continue
        try:
            if not stage.is_dir() or now - stage.stat().st_mtime < 4 * 3600:
                continue
            with key_lock(P, lock, nonblocking=True, readonly=dry_run):
                if not dry_run:
                    remove_tree(stage)
        except (BlockingIOError, FileNotFoundError):
            pass
    entries = P / "entries"
    candidates = sorted(_entries(P), key=_age) if entries.is_dir() else []
    for entry in candidates:
        if entry.name in protect:
            continue
        usage = shutil.disk_usage(P)
        if now - _age(entry) < max_age and usage.free >= disk_reserve(usage):
            continue
        try:
            with key_lock(P, entry.name, nonblocking=True, readonly=dry_run):
                if not dry_run:
                    discard_entry(P, entry)
                removed.append(str(entry))
        except (BlockingIOError, FileNotFoundError):
            pass
    built = {_entry_revision(e) for e in _entries(P)}
    sources = P / "sources"
    for source in sorted(sources.iterdir(), key=_age) if sources.is_dir() else []:
        if source.name in built or source.name in protect or now - _age(source) < max_age:
            continue
        try:
            with key_lock(P, "source-" + source.name, nonblocking=True, readonly=dry_run):
                if not dry_run:
                    remove_tree(_hide(P, "source-" + source.name, source))
                removed.append(str(source))
        except (BlockingIOError, FileNotFoundError):
            pass
    return removed


def verify(build, mathlib, artifacts=(".olean", ".ilean")):
    modules = [mathlib / "Mathlib.lean", *sorted((mathlib / "Mathlib").rglob("*.lean"))]
    if len(modules) < 2:
        raise Unavailable("fresh Mathlib source tree is incomplete")
    count = 0
    for source in modules:
        rel = source.relative_to(mathlib)
        for suffix in artifacts:
            path = build / "lib/lean" / rel.with_suffix(suffix)
            if not path.is_file() or path.stat().st_size == 0:
                raise Unavailable(f"cache extraction incomplete: missing {rel.with_suffix(suffix)}")
            count += 1
    return count


def _stage_root(project, graph, closure, source):
    """A minimal downstream workspace requiring Mathlib, its packages linked to the shared source."""
    project.mkdir()
    mathlib = graph["packages"]["mathlib"]
    manifest = {
        **graph["manifest"],
        "name": "tauceti_mathlib_stage",
        "packagesDir": ".lake/packages",
        "packages": [graph["packages"][name] for name in closure],
    }
    (project / "lake-manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    (project / "lakefile.toml").write_text(
        'name = "tauceti_mathlib_stage"\n\n[[require]]\nname = "mathlib"\n'
        f"git = {json.dumps(mathlib['url'])}\nrev = {json.dumps(mathlib.get('inputRev') or mathlib['rev'])}\n"
    )
    if graph.get("toolchain"):
        (project / "lean-toolchain").write_text(graph["toolchain"] + "\n")
    (project / ".lake/packages").mkdir(parents=True)
    for name in closure:
        (project / ".lake/packages" / name).symlink_to(source / name, target_is_directory=True)


def publish(P, key, spec, graph, closure, env, logdir, fetch):
    """Called under exclusive key lock. A failed download never publishes a lower.

    Extraction runs from a staging workspace whose packages are the shared source tree, with each
    package's read-only ``.lake/build`` bound to a writable staging directory, so Mathlib's
    ``cache get`` fills the staging build of the whole closure and nothing else."""
    P = Path(P)
    pool = P.parent
    entry = P / "entries" / key
    if ready_entry(entry, key, spec):
        os.utime(entry, None)
        return entry, True
    collect(pool, protect=(key,), held=(key,))
    usage = shutil.disk_usage(P)
    if usage.free < disk_reserve(usage):
        raise Unavailable(
            "shared snapshot pool is below its free-space reserve (10%, capped at 20 GiB) and no unused snapshots can be removed"
        )
    staging = P / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    source = source_dir(P, spec["mathlib"])
    with tempfile.TemporaryDirectory(prefix=f"{key}-{spec['mathlib']}-", dir=staging) as raw:
        stage = Path(raw)
        project, builds = stage / "root", stage / "build"
        _stage_root(project, graph, closure, source)
        binds = []
        for name in closure:
            (builds / name).mkdir(parents=True)
            binds += ["--bind", str(builds / name), str(source / name / ".lake/build")]
        logdir.mkdir(parents=True, exist_ok=True)
        log = logdir / "shared-mathlib-prepare.log"
        with log.open("ab") as output:
            # Keep the pool read-only for the extraction tool; only its staging is writable.
            prefix = [
                "bwrap",
                "--die-with-parent",
                "--bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "--ro-bind",
                str(pool),
                str(pool),
                "--bind",
                str(stage),
                str(stage),
                *binds,
                "--",
            ]
            rc = fetch([*prefix, "lake", "exe", "cache", "get"], project, env, output, 1200)
        if rc:
            raise Unavailable(f"fresh Mathlib extraction failed (exit {rc}); see {log}")
        count = verify(builds / "mathlib", source / "mathlib", spec["artifacts"])
        if _source_ready(P, spec["mathlib"]) != closure:
            raise Unavailable("the shared Mathlib source changed during cache preparation")
        ready = stage / "ready"
        ready.mkdir()
        os.replace(builds, ready / "build")
        files = inventory(ready / "build")
        durable_tree(ready / "build")
        atomic_json(
            ready / "ready.json",
            dict(key=key, spec=spec, files_verified=count, inventory=files, published_at=time.time()),
        )
        collect(pool, protect=(key,), held=(key,))
        usage = shutil.disk_usage(P)
        if usage.free < disk_reserve(usage):
            raise Unavailable(
                "snapshot extraction would leave less than the free-space reserve (10%, capped at 20 GiB)"
            )
        entry.parent.mkdir(parents=True, exist_ok=True)
        os.replace(ready, entry)
        fd = os.open(entry.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry, False


def process_identity(pid):
    try:
        if platform.system() == "Linux":
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return dict(pid=pid, start=fields[19], boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        os.kill(pid, 0)
        return dict(pid=pid)
    except (OSError, ValueError, IndexError):
        return None


def report(pool, env, **fields):
    worker = env.get("TAUCETI_WORKER_ID", "default")
    identity = hashlib.sha256(worker.encode()).hexdigest()
    from .activity import current_round_id

    try:
        atomic_json(
            pool / "status" / (identity + ".json"),
            dict(
                worker=worker,
                scope=env.get("TAUCETI_FLEET_MACHINE_ID", "local"),
                round_id=current_round_id(),
                supervisor=process_identity(os.getpid()),
                observed_at=time.time(),
                **fields,
            ),
        )
    except OSError:
        from .config import log

        log("shared Mathlib reporting unavailable; " + fields.get("reason", fields.get("mode", "unknown")))


def summary(env=None, *, scope=None, worker=None):
    """Background preparation only; status itself reads the resulting summary JSON."""
    pool = root(env)
    rows = [read_json(p) for p in (pool / "status").glob("*.json")]
    rows = [
        r
        for r in rows
        if time.time() - r.get("observed_at", 0) < 86400
        and (scope is None or r.get("scope", "local") == scope)
        and (worker is None or r.get("worker") == worker)
    ]
    for row in rows:
        supervisor = row.get("supervisor")
        if row.get("mode") in {"preparing", "shared", "overlay-ready"} and (
            not isinstance(supervisor, dict)
            or not isinstance(supervisor.get("pid"), int)
            or process_identity(supervisor["pid"]) != supervisor
        ):
            row.update(mode="idle", reason="shared preparation supervisor exited; namespace leases remain protected")
        if row.get("mode") in {"shared", "overlay-ready"} and row.get("key"):
            if not isinstance(row["key"], str) or not re.fullmatch("[0-9a-f]{64}", row["key"]):
                row.update(mode="blocked", reason="invalid shared Mathlib key in preparation receipt")
                continue
            try:
                with key_lock(layout(pool), row["key"], nonblocking=True):
                    row.update(mode="idle", reason="shared lease no longer active")
            except BlockingIOError:
                pass  # Includes a namespace retaining its lease after supervisor death.
            except (OSError, Unavailable):
                row.update(mode="blocked", reason="shared Mathlib lease status unavailable")
    reasons_by_mode = {}
    for row in rows:
        if row.get("reason"):
            mode = "preparing" if row.get("mode") == "overlay-ready" else row.get("mode", "unknown")
            reasons_by_mode.setdefault(mode, set()).add(row["reason"])
    return dict(
        shared=sum(r.get("mode") == "shared" for r in rows),
        private=sum(r.get("mode") == "private" for r in rows),
        cache_hits=sum(r.get("cache_hit", False) for r in rows),
        preparing=sum(r.get("mode") in {"preparing", "overlay-ready"} for r in rows),
        blocked=sum(r.get("mode") == "blocked" for r in rows),
        idle=sum(r.get("mode") == "idle" for r in rows),
        preparation_seconds=sum(r.get("preparation_seconds", 0) for r in rows),
        reasons=sorted({r["reason"] for r in rows if r.get("reason")}),
        reasons_by_mode={mode: sorted(reasons) for mode, reasons in sorted(reasons_by_mode.items())},
    )


def _names_mathlib(cwd):
    """False only when a well-formed manifest proves the workspace has no Mathlib anywhere.

    Anything doubtful counts as Mathlib, so the ordinary checks decide (and block) instead of
    the round getting an unrestricted view: an unreadable or malformed manifest, Mathlib as the
    root package, or a dependency that looks like a renamed Mathlib (its URL or name)."""
    try:
        manifest = json.loads((cwd / "lake-manifest.json").read_text())
        packages = manifest.get("packages")
    except (OSError, ValueError, AttributeError):
        return True
    if not isinstance(packages, list) or str(manifest.get("name", "")).lower() == "mathlib":
        return True
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            return True
        if "mathlib" in package["name"].lower() or "mathlib" in str(package.get("url", "")).lower():
            return True
    return False


def _clear_env(env):
    for name in (
        "TAUCETI_SHARED_MATHLIB_LEASE_FD",
        "TAUCETI_SHARED_MATHLIB_KEY",
        "TAUCETI_SHARED_MATHLIB_PREP_STARTED",
    ):
        env.pop(name, None)


@contextlib.contextmanager
def view(cwd, env, logdir, fetch):
    """Yield an argv prefix and keep the version leased until the whole agent exits.

    There is no private branch: a project that depends on Mathlib either gets the shared view or
    raises Unavailable with the reason, before any inference."""
    pool = root(env)
    P = layout(pool)
    if not _names_mathlib(cwd):
        # Nothing to share and nothing to copy: the project does not depend on Mathlib.
        report(pool, env, mode="not-applicable", reason="project does not depend on Mathlib")
        yield []
        return
    try:
        selection(env)
    except Unavailable as error:
        report(pool, env, mode="blocked", reason=str(error))
        raise
    started = time.monotonic()
    prefix, generation = [], None
    safety_stop_recorded = None
    with contextlib.ExitStack() as stack:
        try:
            report(pool, env, mode="preparing", reason="checking namespace capability and dependency identity")
            pool.mkdir(parents=True, exist_ok=True, mode=0o700)
            P.mkdir(exist_ok=True)
            # Resolve Mathlib from the tracked manifest and attach the shared source before any Lake
            # run: Lake then never clones Mathlib or its dependencies into this checkout. A manifest
            # or layout this host cannot share is reported as such, ahead of host capability.
            graph = resolve(cwd, env)
            ok, reason = capability(pool)
            if not ok:
                if reason.startswith("isolation failure"):
                    raise IntegrityFailure(reason)
                raise Unavailable(reason)
            revision = graph["rev"]
            allowed = allowed_revisions(cwd)
            if _purpose(cwd).get("bump"):
                # A bump round adapts TauCeti to the next Mathlib; recording its pin keeps every
                # round's cap ranking from evicting it as if it were an abandoned old version.
                bumps = pool / "bump-pins"
                bumps.mkdir(parents=True, exist_ok=True)
                (bumps / revision).touch()
            if allowed is not None:
                allowed |= bump_pins(pool)
            if allowed is not None and revision not in allowed:
                report(pool, env, mode="blocked", rev=revision, reason="Mathlib pin is not current or previous main")
                raise PinRefused(f"Mathlib {revision[:12]} is not pinned by current or previous main; rebase onto main")
            source_lease = stack.enter_context(
                key_lock(P, "source-" + revision, exclusive=False, timeout=LOCK_WAIT_SECONDS)
            )
            closure = ensure_source(P, pool, cwd, graph, allowed, env)
            attach(cwd, P, revision, closure)
            key, spec = signature(cwd, env, closure)
            report(pool, env, mode="preparing", key=key, rev=revision)
            lease = stack.enter_context(key_lock(P, key, exclusive=False, timeout=LOCK_WAIT_SECONDS))
            entry = P / "entries" / key
            hit = ready_entry(entry, key, spec)
            if hit:
                os.utime(entry, None)
            else:
                # Serialize publishers separately: a waiting first user must not
                # wait for the winner's entire paid round to release its lease.
                lease_lock(lease, fcntl.LOCK_UN)
                with key_lock(P, key + "-publisher", timeout=LOCK_WAIT_SECONDS):
                    lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
                    hit = ready_entry(entry, key, spec)
                    if not hit:
                        lease_lock(lease, fcntl.LOCK_UN)
                        lease_lock(lease, fcntl.LOCK_EX, timeout=LOCK_WAIT_SECONDS)
                        entry, hit = publish(P, key, spec, graph, closure, env, logdir, fetch)
                        lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
            meta = _meta(cwd)
            ok, reason = capability(meta)
            if not ok:
                if reason.startswith("isolation failure"):
                    raise IntegrityFailure(reason)
                raise Unavailable(reason)
            # Earlier generations and retired private packages must not outlive their namespaces.
            discard_private_state(cwd)
            generation = meta / uuid.uuid4().hex
            generation.mkdir()
            generation_lease = stack.enter_context((generation / "lease").open("a+"))
            fcntl.flock(generation_lease, fcntl.LOCK_SH)
            source = source_dir(P, revision)
            layers = []
            for name in closure:
                for part in ("source-upper", "source-work", "upper", "work"):
                    (generation / name / part).mkdir(parents=True)
                # The package's source first: Lake writes `.hash` files beside input files (such as
                # ProofWidgets' widget/package-lock.json), so the round gets a private upper over
                # the immutable tree. Then its build, mounted inside that view.
                layers.append(
                    (
                        source / name,
                        generation / name / "source-upper",
                        generation / name / "source-work",
                        source / name,
                    )
                )
                layers.append(
                    (
                        entry / "build" / name,
                        generation / name / "upper",
                        generation / name / "work",
                        source / name / ".lake/build",
                    )
                )
            atomic_json(
                meta / "view.json",
                dict(
                    key=key,
                    rev=revision,
                    pool=str(pool),
                    layout=str(P),
                    generation=generation.name,
                    packages=sorted(closure),
                    boot_id=boot_id(),
                    activated_at=time.time(),
                ),
            )
            candidate = view_command(layers, pool, lease_fd=lease.fileno())
            fds = (lease.fileno(), source_lease.fileno(), generation_lease.fileno())
            build = source / "mathlib/.lake/build"
            probe = subprocess.run(
                candidate + ["sh", "-c", 'test -f "$1/lib/lean/Mathlib.olean" || exit 42', "sh", str(build)],
                capture_output=True,
                timeout=15,
                pass_fds=fds,
            )
            if probe.returncode == 42:
                raise IntegrityFailure("verified lower missing through fresh overlay; stop sharing and investigate")
            if probe.returncode:
                raise Unavailable("shared Mathlib view unavailable: " + probe.stderr.decode(errors="replace")[-300:])
            lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
            prefix = candidate
            env["TAUCETI_SHARED_MATHLIB_LEASE_FD"] = ",".join(str(fd) for fd in fds)
            env["TAUCETI_SHARED_MATHLIB_KEY"] = key
            env["TAUCETI_SHARED_MATHLIB_PREP_STARTED"] = str(started)
            report(
                pool,
                env,
                mode="overlay-ready",
                key=key,
                rev=revision,
                cache_hit=hit,
                preparation_seconds=time.monotonic() - started,
            )
        except (Unavailable, OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, IntegrityFailure):
                safety_stop_recorded = stop_sharing(pool, str(error))
            report(
                pool,
                env,
                mode="blocked",
                reason=str(error),
                preparation_seconds=time.monotonic() - started,
                safety_stop_recorded=safety_stop_recorded,
            )
            stack.close()
            _clear_env(env)
            if isinstance(error, Unavailable):
                raise
            raise Unavailable(str(error)) from error
        # The body is outside the preparation handler: never retry paid work.
        body_error = None
        try:
            yield prefix
        except BaseException as error:
            body_error = type(error).__name__
            raise
        finally:
            stack.close()
            record_result(
                env,
                None,
                phase="cleanup",
                body_error=body_error,
                cleanup_success=body_error is None,
                safety_stop_recorded=safety_stop_recorded,
                **upper_usage(generation),
            )
            previous = read_json(
                pool
                / "status"
                / (hashlib.sha256(env.get("TAUCETI_WORKER_ID", "default").encode()).hexdigest() + ".json")
            )
            if previous.get("mode") != "blocked":
                report(
                    pool,
                    env,
                    mode="idle",
                    key=env["TAUCETI_SHARED_MATHLIB_KEY"],
                    reason=read_json(pool / "safety-stop.json").get("reason")
                    or "shared view released; awaiting next natural round",
                    preparation_seconds=previous.get("preparation_seconds", 0),
                    cache_hit=previous.get("cache_hit", False),
                )
            _clear_env(env)


def preparation_status(env, *, error=None):
    """Mark ready only after both cache warmers pass, before any inference starts."""
    worker = env.get("TAUCETI_WORKER_ID", "default")
    pool = root(env)
    previous = read_json(pool / "status" / (hashlib.sha256(worker.encode()).hexdigest() + ".json"))
    elapsed = time.monotonic() - float(env.get("TAUCETI_SHARED_MATHLIB_PREP_STARTED", time.monotonic()))
    report(
        pool,
        env,
        mode="blocked" if error else "shared",
        reason=str(error) if error else None,
        key=env.get("TAUCETI_SHARED_MATHLIB_KEY"),
        cache_hit=previous.get("cache_hit", False),
        preparation_seconds=elapsed,
    )


def lease_fds(env):
    """The view's lease descriptors (build, source, generation) that its children must inherit."""
    value = env.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
    return tuple(int(fd) for fd in value.split(",") if fd) if value else ()


def record_result(env, code, *, phase="invocation", **fields):
    key = env.get("TAUCETI_SHARED_MATHLIB_KEY")
    if not key:
        return
    from .activity import current_round_id

    pool = root(env)
    previous = read_json(
        pool / "status" / (hashlib.sha256(env.get("TAUCETI_WORKER_ID", "default").encode()).hexdigest() + ".json")
    )
    record = dict(
        at=time.time(),
        worker=env.get("TAUCETI_WORKER_ID", "default"),
        round_id=current_round_id(),
        key=key,
        exit_code=code,
        phase=phase,
        preparation_seconds=previous.get("preparation_seconds"),
        cache_hit=previous.get("cache_hit"),
        **fields,
    )
    try:
        with (pool / "results.jsonl").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        from .config import log

        log("shared Mathlib result receipt unavailable; paid invocation exit status preserved")
