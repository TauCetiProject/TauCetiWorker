"""Incremental Mathlib investigation, issue publication, and issue-driven adoption."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import Die, NoProgress, log, max_open_prs, positive_integer
from .constants import CLAIMS, TAUCETI
from .github import GitHub, can_push, claims_repo, ensure_fork, me, shared_claims_granted
from .overlap_state import Lease, State, environment, git
from .overlap_support import atomic, locked, read_context, root, shared_read, snapshot
from .paths import HERE

MATHLIB = "leanprover-community/mathlib4"
FIRST_PIN = "66748b489336a59ed4b4a4a612615c38de823e9a"
LABEL = "mathlib-overlap"
DEFAULT_BATCH = 160  # calibrated with nine inspected Codex scans; see docs/mathlib-overlap-calibration.md
MARKER = re.compile(r"<!--tauceti-mathlib-overlap:v1 ([0-9a-f]{64})-->")
HOLDS = {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}


class HistoryRangeError(Die):
    """The current pin/cursor cannot form a forward first-parent scan."""


def batch_size() -> int:
    return positive_integer(os.environ.get("TAUCETI_OVERLAP_BATCH_COMMITS", DEFAULT_BATCH))


def state_for(cfg) -> State:
    return State(cfg.state / "overlap-state.git")


def mathlib_history() -> Path:
    """Shared full history, optionally bootstrapped from an existing local clone."""
    directory = root() / "overlap" / "mathlib.git"
    directory.parent.mkdir(parents=True, exist_ok=True)
    with locked(directory.with_suffix(".lock"), timeout=240):
        if not directory.exists():
            source = os.environ.get("TAUCETI_OVERLAP_MATHLIB_SOURCE")
            temporary = directory.with_name("mathlib-clone-" + str(os.getpid()))
            try:
                if source:
                    git("clone", "--bare", "--shared", "--", Path(source).resolve(), temporary)
                else:
                    git("clone", "--bare", "--filter=blob:none", "--", f"https://github.com/{MATHLIB}", temporary)
                git("--git-dir", temporary, "remote", "set-url", "origin", f"https://github.com/{MATHLIB}")
                temporary.rename(directory)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        timestamp = directory / "overlap-fetch-time"
        if not timestamp.exists() or time.time() - timestamp.stat().st_mtime > 120:
            git("--git-dir", directory, "fetch", "--no-tags", "origin", "+refs/heads/master:refs/heads/master")
            timestamp.touch()
    return directory


def history(repo: Path, *args, check=True):
    return git("--git-dir", repo, *args, check=check)


def commit_sha(repo: Path, value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{7,40}", value):
        raise Die("overlap: Mathlib commit must be a hexadecimal SHA")
    return history(repo, "rev-parse", "--verify", value + "^{commit}").stdout.strip()


def select_batch(repo: Path, cursor: str, pin: str, count: int) -> list[str]:
    if history(repo, "merge-base", "--is-ancestor", pin, "master", check=False).returncode:
        raise HistoryRangeError("overlap: pinned Mathlib commit is not on master history")
    if history(repo, "merge-base", "--is-ancestor", cursor, pin, check=False).returncode:
        raise HistoryRangeError("overlap: cursor is not an ancestor of the Mathlib pin")
    # rev-list --max-count would keep the newest commits before reversing, skipping a gap.
    commits = history(repo, "rev-list", "--first-parent", "--reverse", f"{cursor}..{pin}").stdout.splitlines()
    if commits and history(repo, "rev-parse", commits[0] + "^1").stdout.strip() != cursor:
        raise HistoryRangeError("overlap: cursor is not on the pin's first-parent history")
    return commits[: positive_integer(count)]


def sources():
    tauceti = snapshot(TAUCETI, ttl=120)
    if tauceti is None:
        raise Die("overlap: could not fetch current Tau Ceti")
    try:
        manifest = json.loads((tauceti / "lake-manifest.json").read_text())
        pin = next(p["rev"] for p in manifest["packages"] if p["name"] == "mathlib")
    except (ValueError, OSError, KeyError, StopIteration) as error:
        raise Die("overlap: could not read Tau Ceti's Mathlib pin") from error
    tc_sha = git("rev-parse", "HEAD", cwd=tauceti).stdout.strip()
    return tauceti, tc_sha, pin


def pinned_sources(repo: Path, pin: str) -> Path:
    target = root() / "overlap" / "sources" / pin
    with locked(target.with_suffix(".lock"), timeout=240):
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(pin + "-" + str(os.getpid()))
            temporary.mkdir()
            try:
                archive = subprocess.Popen(
                    ["git", "--git-dir", str(repo), "archive", pin, "Mathlib"], stdout=subprocess.PIPE
                )
                assert archive.stdout is not None
                extracted = subprocess.run(["tar", "-x", "-C", str(temporary)], stdin=archive.stdout, check=False)
                archive.stdout.close()
                if archive.wait() or extracted.returncode:
                    raise Die("overlap: could not extract pinned Mathlib sources")
                temporary.rename(target)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
    return target


def issues(gh: GitHub, *, fresh=False) -> list[dict]:
    def fetch():
        # REST pagination has no fixed 200-issue ceiling. Decode all pages before caching.
        result = gh._gh(
            ["api", "--paginate", "--slurp", f"repos/{TAUCETI}/issues?labels={LABEL}&state=all&per_page=100"]
        )
        if result.returncode:
            raise Die("overlap: issue inventory failed: " + result.stderr.strip()[-300:])
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise Die("overlap: incomplete issue inventory")
        return [row for page in pages for row in page if "pull_request" not in row]

    with read_context(fresh=fresh):
        return shared_read(["overlap_issues", TAUCETI], fetch, ttl=120)


def overlap_prs(gh, *, fresh=False):
    """Only body/ownership are needed here; do not spend GraphQL on CI/review state."""

    def fetch():
        result = gh._gh(["api", "--paginate", "--slurp", f"repos/{TAUCETI}/pulls?state=open&per_page=100"])
        if result.returncode:
            raise Die("overlap: open PR inventory failed: " + result.stderr.strip()[-300:])
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise Die("overlap: incomplete PR inventory")
        return [
            {
                "number": row["number"],
                "body": row.get("body"),
                "isDraft": row.get("draft", False),
                "author": row.get("user", {}),
            }
            for page in pages
            for row in page
        ]

    with read_context(fresh=fresh):
        return shared_read(["overlap_prs", TAUCETI], fetch, ttl=120)


def issue_id(finding: dict) -> str:
    value = [finding["change_commit"], sorted(finding["mathlib_declarations"]), sorted(finding["tauceti_declarations"])]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def marker(fingerprint: str) -> str:
    return f"<!--tauceti-mathlib-overlap:v1 {fingerprint}-->"


def validate_scan(result: dict, batch: dict):
    if not isinstance(result, dict) or result.get("complete") is not True:
        raise Die("overlap: investigation incomplete; cursor unchanged")
    if result.get("from") != batch["from"] or result.get("through") != batch["through"]:
        raise Die("overlap: result describes a different batch")
    if result.get("examined_commits") != [row["sha"] for row in batch["commits"]]:
        raise Die("overlap: not every supplied commit was accounted for")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise Die("overlap: malformed findings")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("change_commit") not in result["examined_commits"]:
            raise Die("overlap: finding lacks a supplied Mathlib change commit")
        if finding.get("classification") not in {"exact", "generalization", "equivalent", "partial"}:
            raise Die("overlap: unclassified finding")
        for key in ("mathlib_declarations", "tauceti_declarations"):
            if (
                not isinstance(finding.get(key), list)
                or not finding[key]
                or not all(isinstance(name, str) and name.strip() and "\n" not in name for name in finding[key])
            ):
                raise Die("overlap: missing declaration mapping")
        for key in ("title", "body"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                raise Die("overlap: missing issue text")
        if "\n" in finding["title"] or len(finding["title"]) > 200 or len(finding["body"]) > 30000:
            raise Die("overlap: invalid issue text size")
        if "Preserve:" not in finding["body"] or "https://github.com/" not in finding["body"]:
            raise Die("overlap: finding lacks preservation notes or source evidence")
    return findings


def confirm_publication(row, fingerprint):
    number = row.get("number") if isinstance(row, dict) else None
    if type(number) is not int or number <= 0:
        raise Die("overlap: could not confirm published issue; cursor unchanged")
    labels = row.get("labels")
    if (
        not isinstance(labels, list)
        or LABEL not in {item.get("name") for item in labels if isinstance(item, dict)}
        or marker(fingerprint) not in (row.get("body") or "")
    ):
        raise Die(
            f"overlap: issue #{number} lacks the required {LABEL} label or finding marker; "
            "cursor unchanged. Restore that issue's metadata before retrying; it will not be recreated."
        )
    return number


def publish(gh, findings, lease):
    if not findings:
        return []
    inventory = issues(gh, fresh=True)
    known = {match.group(1): row["number"] for row in inventory for match in MARKER.finditer(row.get("body") or "")}
    numbers = []
    for finding in findings:
        fingerprint = issue_id(finding)
        if fingerprint in known:
            numbers.append(known[fingerprint])
            continue
        receipt = lease.state.directory.parent / ("overlap-issue-" + fingerprint + "-receipt.json")
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            number = saved.get("number")
            if saved.get("fingerprint") != fingerprint or type(number) is not int or number <= 0:
                raise Die("overlap: malformed issue publication receipt; refusing to duplicate a finding")
            result = gh._gh(["api", f"repos/{TAUCETI}/issues/{number}"])
            if result.returncode:
                raise Die(f"overlap: cannot reconcile published issue #{number}; cursor unchanged")
            number = confirm_publication(json.loads(result.stdout), fingerprint)
            known[fingerprint] = number
            numbers.append(number)
            continue
        lease.require()
        body = finding["body"] + "\n\n" + marker(fingerprint) + "\n"
        # Mutations are never blindly retried: the next round reconciles a lost response.
        path = lease.state.directory.parent / ("overlap-issue-" + fingerprint + ".md")
        path.write_text(body)
        payload = path.with_suffix(".json")
        atomic(payload, {"title": finding["title"], "body": body, "labels": [LABEL]})
        result = gh._gh(["api", "--method", "POST", f"repos/{TAUCETI}/issues", "--input", str(payload)])
        if result.returncode:
            raise Die("overlap: issue publication failed; cursor unchanged: " + result.stderr.strip()[-400:])
        try:
            published = json.loads(result.stdout)
            number = published["number"]
        except (ValueError, KeyError, TypeError) as error:
            raise Die("overlap: could not confirm published issue; cursor unchanged") from error
        if type(number) is not int or number <= 0:
            raise Die("overlap: could not confirm published issue; cursor unchanged")
        atomic(receipt, {"number": number, "fingerprint": fingerprint})
        confirm_publication(published, fingerprint)
        known[fingerprint] = number
        numbers.append(number)
    return numbers


def require_shared():
    if not shared_claims_granted():
        raise NoProgress(f"overlap: shared write access to {CLAIMS} required; no private-fork fallback")


def require_publication_rights():
    if can_push(TAUCETI) is not True:
        raise NoProgress(f"overlap-scan: write access to {TAUCETI} required to label findings; scanner not launched")


def initialize(cfg, sha: str):
    require_shared()
    require_publication_rights()
    repo = mathlib_history()
    resolved = commit_sha(repo, sha)
    if history(repo, "merge-base", "--is-ancestor", resolved, "master", check=False).returncode:
        raise Die("overlap: initial commit is not on Mathlib master")
    select_batch(repo, resolved, history(repo, "rev-parse", "master").stdout.strip(), 1)
    state = state_for(cfg)
    with Lease(state, "mathlib-overlap-scan", me(), cfg.wid) as lease:
        if lease is None:
            raise NoProgress("overlap: scanner claimed by another worker")
        oid, cursor = state.cursor()
        if oid:
            log(f"overlap: cursor already initialized at {cursor}; unchanged")
            return 0
        # Creating the label is explicit setup, not something every scanner tries.
        result = GitHub()._gh(
            [
                "label",
                "create",
                LABEL,
                "--repo",
                TAUCETI,
                "--color",
                "5319e7",
                "--description",
                "Adopt a Mathlib API overlapping Tau Ceti",
            ]
        )
        if result.returncode:
            # A pre-existing label is fine; other failures must not look like success.
            existing = GitHub()._gh(["api", f"repos/{TAUCETI}/labels/{LABEL}"])
            if existing.returncode:
                raise Die("overlap: cannot create or read the overlap issue label")
        lease.require()
        if not state.advance("", resolved):
            raise NoProgress("overlap: another initializer won the cursor update")
    log(f"overlap: initialized shared cursor at {resolved}")
    return 0


def survey_overlaps(cfg, gh, sv, *, repair=True):
    """Read-only and TTL-cached; dispatch repeats live checks under its lease."""
    from .survey import Candidate

    try:
        if can_push(CLAIMS) is not True:
            reason = f"shared write access to {CLAIMS} required; no private-fork fallback"
            sv.overlap_scan.suppressed.append(Candidate(0, "", reason))
            sv.overlap_fix.suppressed.append(Candidate(0, "", reason))
            return
        state = state_for(cfg)
        oid, cursor = shared_read(["overlap_cursor", CLAIMS], state.cursor, ttl=120)
        if not oid:
            sv.overlap_scan.suppressed.append(
                Candidate(0, "", "initialize with tauceti overlap init --mathlib-commit " + FIRST_PIN)
            )
            return
        _, _, pin = sources()
        if can_push(TAUCETI) is not True:
            sv.overlap_scan.suppressed.append(
                Candidate(0, cursor, f"write access to {TAUCETI} required to label findings")
            )
        elif cursor != pin:
            sv.overlap_scan.actionable.append(Candidate(0, cursor, f"cursor {cursor[:12]} → pin {pin[:12]}"))
        else:
            sv.overlap_scan.suppressed.append(Candidate(0, cursor, "caught up to pinned Mathlib"))
        if not repair:
            return
        rows = issues(gh)
        prs = overlap_prs(gh)
        in_flight = {m.group(1) for pr in prs for m in MARKER.finditer(pr.get("body") or "")}
        mine = sum(not pr.get("isDraft") and (pr.get("author") or {}).get("login") == me() for pr in prs)
        for row in sorted(rows, key=lambda row: (row.get("created_at", ""), row["number"])):
            match = MARKER.search(row.get("body") or "")
            labels = {label["name"].lower() for label in row.get("labels", [])}
            if row.get("state") != "open" or not match or labels & HOLDS or match.group(1) in in_flight:
                continue
            candidate = Candidate(0, str(row["number"]), f"issue #{row['number']}: {row['title']}")
            if mine >= max_open_prs():
                candidate.reason = f"authoring backpressure: {mine}/{max_open_prs()} open PRs; {candidate.reason}"
                sv.overlap_fix.suppressed.append(candidate)
            else:
                sv.overlap_fix.actionable.append(candidate)
    except (Die, OSError, ValueError, subprocess.SubprocessError) as error:
        sv.overlap_scan.actionable.clear()
        sv.overlap_fix.actionable.clear()
        sv.overlap_scan.suppressed.append(Candidate(0, "", str(error)))
        sv.overlap_fix.suppressed.append(Candidate(0, "", str(error)))


def make_packet(repo, tauceti, tc_sha, pin, cursor, commits, work):
    rows = []
    changed_bytes = 0
    patches = work / "diffs"
    patches.mkdir()
    for sha in commits:
        text = history(repo, "diff", "--no-ext-diff", "--unified=3", sha + "^1", sha, "--", "Mathlib/**/*.lean").stdout
        path = patches / (sha + ".patch")
        path.write_text(text)
        changed_bytes += len(text.encode())
        rows.append(
            {
                "sha": sha,
                "summary": history(repo, "show", "-s", "--format=%s", sha).stdout.strip(),
                "patch": str(path),
                "bytes": len(text.encode()),
            }
        )
    value = {
        "from": cursor,
        "through": commits[-1],
        "pin": pin,
        "tauceti_commit": tc_sha,
        "tauceti_source": str(tauceti),
        "mathlib_source": str(pinned_sources(repo, pin)),
        "mathlib_history": str(repo),
        "commits": rows,
        "changed_bytes": changed_bytes,
    }
    atomic(work / "batch.json", value)
    return value


def do_scan(w, sv, c, opts, bubble):
    from .agents import fill_prompt, host_agent_argv, run_agent_proc
    from .work_units import _effective_authoring_profile

    require_shared()
    require_publication_rights()
    state = state_for(w.cfg)
    with Lease(state, "mathlib-overlap-scan", me(), w.cfg.wid) as lease:
        if lease is None:
            return None
        oid, cursor = state.cursor()
        if not cursor:
            raise NoProgress("overlap: cursor is uninitialized")
        tauceti, tc_sha, pin = sources()
        repo = mathlib_history()
        try:
            commits = select_batch(repo, cursor, pin, batch_size())
        except HistoryRangeError as error:
            if "overlap-scan" in getattr(opts, "only", []):
                raise
            log(f"overlap-scan: skipping infeasible history range: {error}")
            return None
        if not commits:
            return None
        # Reuse a completed local investigation after interrupted publication.
        # The shared cursor remains the sole authoritative coverage state.
        for prior in sorted((w.cfg.state / "overlap-runs").glob("*/result.json"), reverse=True):
            try:
                packet = json.loads(prior.with_name("batch.json").read_text())
                if packet["from"] != cursor or packet["pin"] != pin:
                    continue
                prior_commits = [row["sha"] for row in packet["commits"]]
                if select_batch(repo, cursor, pin, len(prior_commits)) != prior_commits:
                    continue
                findings = validate_scan(json.loads(prior.read_text()), packet)
            except (OSError, ValueError, KeyError, Die):
                continue
            numbers = publish(w.gh, findings, lease)
            lease.require()
            if not state.advance(oid, packet["through"]):
                raise NoProgress("overlap: cursor changed concurrently; stale update rejected")
            metrics_path = prior.with_name("metrics.json")
            metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
            metrics.update(complete=True, findings=len(findings), issues=numbers, publication_retried=True)
            atomic(metrics_path, metrics)
            log(f"overlap-scan: published retained result and advanced to {packet['through'][:12]}; issues {numbers}")
            return 0
        work = (
            w.cfg.state / "overlap-runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + lease.owner.rsplit("/", 1)[-1][:8])
        )
        work.mkdir(parents=True)
        packet = make_packet(repo, tauceti, tc_sha, pin, cursor, commits, work)
        output = work / "result.json"
        prompt = fill_prompt(HERE / "prompts" / "overlap-scan.md", BATCH=str(work / "batch.json"), RESULT=str(output))
        (work / "prompt.md").write_text(prompt)
        profile = _effective_authoring_profile(opts)
        argv, env = host_agent_argv(prompt, profile)
        log(
            f"overlap-scan: {len(commits)} commits, {packet['changed_bytes']} patch bytes, {cursor[:12]}..{commits[-1][:12]}"
        )
        started = time.time()
        metrics = {
            "phase": "overlap-scan",
            "from": cursor,
            "through": commits[-1],
            "pin": pin,
            "tauceti_commit": tc_sha,
            "commits": len(commits),
            "changed_bytes": packet["changed_bytes"],
            "provider": profile.provider,
            "model": profile.model,
            "complete": False,
        }
        try:
            rc = run_agent_proc(
                argv,
                env=env,
                cwd=work,
                logdir=work,
                label="conversation",
                provider=profile.provider,
                cancel_event=lease.lost,
            )
            if rc:
                return rc
            findings = validate_scan(json.loads(output.read_text()), packet)
            numbers = publish(w.gh, findings, lease)
            lease.require()
            if not state.advance(oid, commits[-1]):
                raise NoProgress("overlap: cursor changed concurrently; stale update rejected")
            metrics.update(complete=True, findings=len(findings), issues=numbers)
            log(f"overlap-scan: advanced to {commits[-1][:12]}; issues {numbers or '(none)'}; log {work}")
            return 0
        finally:
            metrics["duration_seconds"] = round(time.time() - started, 1)
            telemetry = work / "conversation-telemetry.json"
            if telemetry.exists():
                metrics.update(json.loads(telemetry.read_text()))
            atomic(work / "metrics.json", metrics)


def do_fix(w, sv, c, opts, bubble):
    from .agents import fill_prompt, local_verification, prepare_checkout, run_agent_host, run_in_bubble, wrapper_bin
    from .work_units import _effective_authoring_profile

    require_shared()
    number = int(c.head)
    state = state_for(w.cfg)
    with Lease(state, f"overlap-fix/{number}", me(), w.cfg.wid) as lease:
        if lease is None:
            return None
        row = next((row for row in issues(w.gh, fresh=True) if row["number"] == number), None)
        match = MARKER.search((row or {}).get("body") or "")
        labels = {label["name"].lower() for label in (row or {}).get("labels", [])}
        if not row or row["state"] != "open" or not match or labels & HOLDS:
            return None
        prs = overlap_prs(w.gh, fresh=True)
        if any(match.group(0) in (pr.get("body") or "") for pr in prs):
            return None
        if sum(not pr.get("isDraft") and (pr.get("author") or {}).get("login") == me() for pr in prs) >= max_open_prs():
            raise NoProgress("overlap-fix: authoring backpressure")
        fork = ensure_fork()
        prompt = fill_prompt(
            HERE / "prompts" / "overlap-fix.md",
            ISSUE=str(number),
            BODY=row["body"],
            FORK=fork,
            FORK_OWNER=fork.split("/", 1)[0],
            BIN=wrapper_bin(bubble=bubble),
            LOCAL_VERIFICATION=local_verification("ordinary", bubble),
        )
        with environment(
            {
                "TAUCETI_VERIFY_POLICY": "ordinary",
                "TAUCETI_WORKER_ID": lease.owner,
                "CLAIM_REPO": claims_repo(),
                "TAUCETI_CLAIM_REPO": claims_repo(),
                "TAUCETI_CLAIM_KEY": lease.key,
                "TAUCETI_AUTHOR_CLAIM_KEY": lease.key,
                "TAUCETI_TARGET_MARKER": match.group(0),
                "TAUCETI_PUSH_REMOTE": f"https://github.com/{fork}",
                "TAUCETI_PUSH_EXPECT": None,
                "TAUCETI_REQUIRE_TARGET_MARKER": None,
            }
        ):
            if bubble:
                rc = run_in_bubble(w, TAUCETI, prompt, opts, allow_push=fork, cancel_event=lease.lost)
            else:
                if not prepare_checkout(w.cfg, work="overlap-fix"):
                    from .work_recovery import LAST_REFUSAL

                    raise Die(f"overlap-fix: checkout preparation failed{': ' + LAST_REFUSAL if LAST_REFUSAL else ''}")
                lease.require()
                rc = run_agent_host(
                    w.cfg.checkout,
                    prompt,
                    _effective_authoring_profile(opts),
                    w.cfg.logdir,
                    cancel_event=getattr(lease, "lost", None),
                )
        if rc:
            return rc
        lease.require()
        with read_context(fresh=True):
            prs = overlap_prs(w.gh, fresh=True)
            current = next((r for r in issues(w.gh, fresh=True) if r["number"] == number), None)
        if any(match.group(0) in (pr.get("body") or "") for pr in prs) or (current and current["state"] == "closed"):
            return 0
        raise NoProgress("overlap-fix: no associated PR or explained issue closure; see conversation log")
