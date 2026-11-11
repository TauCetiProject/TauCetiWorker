#!/usr/bin/env python3
"""Review-command failures retain useful public-safe diagnostics and enrich stuck issues."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents
from tauceti_worker import github as gh_mod
from tauceti_worker.review_diagnostics import (
    clear_review_failure,
    failure_summary,
    public_review_failure,
    read_review_failure,
    record_review_failure,
    recover_review_failures,
    sanitize_failure,
)

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


with tempfile.TemporaryDirectory() as raw:
    state = Path(raw)
    log = state / "review.log"
    log.write_text("setup\nNot logged in · Please run /login\n")
    value = record_review_failure(
        state,
        worker="worker7",
        pr=1388,
        head="deadbeef",
        provider="claude",
        code=1,
        log_file=log,
    )
    attempt = value["attempts"][-1]
    check("log tail classified", attempt["category"], "reviewer-auth")
    check("log path reduced to basename", attempt["log"], "review.log")
    check("public diagnostic names provider", "via `claude`" in public_review_failure(value), True)

    # The CLI appends a long, generic command failure AFTER the useful error.
    # Cleanup chatter can follow that too; the final line is not the diagnosis.
    log.write_text(
        "setup\nOSError: [Errno 7] Argument list too long: 'codex'\n"
        + "tauceti-review: command failed (1): python runner/review.py "
        + "--option " * 200
        + "\ncleanup finished\n"
    )
    value = record_review_failure(
        state,
        worker="w",
        pr=42,
        head="a" * 40,
        provider="codex",
        code=1,
        reason="review #42 exited with status 1: cleanup finished",
        log_file=log,
    )
    check("wrapper does not hide E2BIG", "Argument list too long" in value["attempts"][-1]["summary"], True)
    check("public E2BIG survives wrapper", "OS argument limit" in public_review_failure(value), True)
    log.write_text("gh: API rate limit exceeded for user\ntauceti-review: command failed (1): gh pr diff 42\n")
    value = record_review_failure(state, worker="w", pr=42, head="a" * 40, provider="codex", code=1, log_file=log)
    check(
        "GitHub failure is distinguished from provider quota", value["attempts"][-1]["category"], "checkout-or-network"
    )
    check("GitHub diagnosis is publishable", "GitHub API rate limit" in public_review_failure(value), True)
    log.write_text(
        "OSError: Argument list too long\n" + "ordinary output\n" * 20000 + "fatal: connection reset\ncleanup\n"
    )
    check("bounded tail excludes ancient errors", failure_summary(log), "fatal: connection reset")
    check("missing log preserves reason", failure_summary(state / "missing", "Not logged in"), "Not logged in")

    generic = "tauceti-review: command failed (1): python runner/review.py"
    for prefix in (
        "$ git clone -q https://github.com/TauCetiProject/TauCeti /tmp/code\n",
        "git clone completed successfully\n",
        "[correctness]   ! Request timed out\n",
        "=" * 72 + "\nThe model is not available and github has a rate limit\n" + "=" * 72 + "\n",
        "gh: API rate limit exceeded\n$ python runner/post.py\n",
    ):
        log.write_text(prefix + generic + "\n")
        check("non-terminal output is not the diagnosis", failure_summary(log), generic)
    for error, detail in (
        ("gh: Resource not accessible by integration (HTTP 403)", "lack of permission"),
        ("Error: unknown model", "reviewer model is unavailable"),
        ("OSError: No space left on device", "disk space"),
        ("subprocess.TimeoutExpired: command exceeded limit", "timed out"),
        ("OSError: Argument list too long: " + "x" * 1000, "OS argument limit"),
    ):
        log.write_text(error + "\n" + generic + "\ncleanup\n")
        value = record_review_failure(state, worker="w", pr=43, head="a" * 40, provider="codex", code=1, log_file=log)
        check("specific failure survives wrapper and truncation", detail in public_review_failure(value), True)

    # Redirected stdout is buffered: post.py's stderr lands inside the scoreboard
    # dump, whose remaining prose flushes only after the terminal CLI diagnostic.
    log.write_text(
        "=" * 72 + "\nReview prose: unknown model\n"
        "$ python runner/post.py\n"
        "gh: Resource not accessible by integration (HTTP 403)\n"
        "tauceti-review: command failed (1): python runner/post.py\n"
        "More prose: No space left on device\n" + "=" * 72 + "\n"
    )
    value = record_review_failure(state, worker="w", pr=44, head="a" * 40, provider="codex", code=1, log_file=log)
    check(
        "interleaved post failure retains permission diagnosis",
        "lack of permission" in public_review_failure(value),
        True,
    )
    for error in (
        "review-root lookup failed: GitHub rejected the request (HTTP 403)",
        "gh api POST /repos/o/r/pulls/1/comments FAILED: gh: Validation Failed (HTTP 422)",
    ):
        log.write_text("=" * 72 + "\nprose\n$ python runner/post.py\n" + error + "\n" + generic + "\n" + "=" * 72)
        value = record_review_failure(state, worker="w", pr=46, head="a" * 40, provider="codex", code=1, log_file=log)
        check(
            "post-layer HTTP failures retain their category", value["attempts"][-1]["category"], "checkout-or-network"
        )
    log.write_text("=" * 72 + "\nunknown model\ntauceti-review: review step wrote no post plan\n" + "=" * 72)
    check(
        "missing post plan excludes buffered prose",
        failure_summary(log),
        "tauceti-review: review step wrote no post plan",
    )
    value = record_review_failure(
        state,
        worker="w",
        pr=45,
        head="a" * 40,
        provider="codex",
        code=1,
        reason="review #45 exited with status 1: gh: You have exceeded a secondary rate limit",
    )
    check(
        "bubble reason retains GitHub rate-limit diagnosis",
        "GitHub API rate limit" in public_review_failure(value),
        True,
    )

    # Exercise the actual subprocess path, not just the extraction helper.
    reports = []
    original_report = agents.report_failure
    stream_setting = os.environ.pop("TAUCETI_STREAM", None)
    agents.report_failure = lambda reason, **kwargs: reports.append(reason)
    try:
        agents.run_to_logfile(
            [sys.executable, "-c", "print('OSError: [Errno 7] Argument list too long'); print('cleanup'); exit(1)"],
            state / "subprocess.log",
            "review #42",
        )
    finally:
        agents.report_failure = original_report
        if stream_setting is not None:
            os.environ["TAUCETI_STREAM"] = stream_setting
    check("subprocess report retains operative error", "Argument list too long" in reports[-1], True)

    for i in range(4):
        record_review_failure(
            state,
            worker="worker7",
            pr=1388,
            head="deadbeef",
            provider="codex",
            code=i + 2,
            reason=f"review #1388 exited with status {i + 2}: engine error: attempt {i}",
        )
    check("history capped at three", len(read_review_failure(state, 1388)["attempts"]), 3)
    clear_review_failure(state, 1388)
    check("successful review clears diagnostic", read_review_failure(state, 1388), {})

    old_log = state / "review-1500-20260730-185500.log"
    old_log.write_text("clone setup\nfatal: could not resolve host github.com\n")
    recovered = recover_review_failures(state, state, worker="worker7", pr=1500, head="cafebabe")
    check("legacy log is recovered", len(recovered["attempts"]), 1)
    check("legacy log is classified", recovered["attempts"][0]["category"], "checkout-or-network")
    check("legacy log records provenance", recovered["attempts"][0]["recovered"], True)
    check("legacy log does not invent an exit code", "(exit unknown)" in public_review_failure(recovered), True)

secret = "OPENAI_API_KEY=sk-secretvalue123 /Users/alice/work https://token@example.com/repo"
clean = sanitize_failure(secret)
check("API key redacted", "secretvalue" in clean, False)
check("home user redacted", "alice" in clean, False)
check("credential URL redacted", "token@" in clean, False)

adversarial = {
    "schema": "tauceti.review-failure/v1",
    "attempts": [
        {
            "at": "now ](https://example.com)",
            "provider": "codex` token",
            "code": 1,
            "category": "review-engine`",
            "summary": (
                "Authorization: Basic dXNlcjpwYXNzd29yZA== "
                "ANTHROPIC_API_KEY = supersecretvalue "
                '{"api_key":"jsonsecret"} client_secret: oauthsecret '
                "cookie=sessionsecret eyJhbGciOiJIUzI1NiJ9.payload.signature"
            ),
        }
    ],
}
public = public_review_failure(adversarial)
for leaked in (
    "dXNlcjpwYXNzd29yZA",
    "supersecretvalue",
    "jsonsecret",
    "oauthsecret",
    "sessionsecret",
    "eyJhbGci",
    "https://example.com",
    "codex` token",
):
    check(f"public output excludes {leaked}", leaked in public, False)
check(
    "unknown public fields fail closed",
    public,
    "- unknown time: `review-command` via `unknown` (exit 1): review command failed",
)

e2big = {
    "attempts": [
        {
            "at": "2026-07-30T23:00:00Z",
            "provider": "codex",
            "code": 1,
            "category": "review-engine",
            "summary": "OSError: [Errno 7] Argument list too long: 'codex' SECRET=do-not-copy",
        }
    ]
}
public = public_review_failure(e2big)
check("E2BIG has fixed public diagnostic", "review prompt exceeded the OS argument limit" in public, True)
check("E2BIG raw summary stays private", "do-not-copy" in public, False)


class FakeGitHub(gh_mod.GitHub):
    def __init__(self, existing=None):
        super().__init__("TauCetiProject/TauCeti")
        self.existing = existing or []
        self.calls = []

    def _gh(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.existing))
        return SimpleNamespace(returncode=0, stdout="")


diagnostic = "- 2026-07-30T18:55:00Z: `reviewer-auth` via `claude` (exit 1): reviewer authentication failed"
client = FakeGitHub()
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("missing issue is created", client.calls[-1][:2], ["issue", "create"])
body = client.calls[-1][client.calls[-1].index("--body") + 1]
check("created issue carries diagnostic", diagnostic in body, True)
check("created issue asks for infrastructure repair", "infrastructure repair" in body, True)

existing_body = gh_mod.GitHub._stuck_issue_body(1388, "its review errored", diagnostic)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": existing_body}])
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("unchanged issue is not edited", len(client.calls), 1)

client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": "old"}])
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("existing issue is enriched", client.calls[-1][:2], ["issue", "edit"])
check("right issue is edited", client.calls[-1][2], "1504")

other_diagnostic = "- 2026-07-30T18:56:00Z: `review-command` via `codex` (exit 1): review command failed"
existing_body = gh_mod.GitHub._stuck_issue_body(1388, "its review errored", diagnostic)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": existing_body}])
client.ensure_stuck_issue(1388, "its review errored again", other_diagnostic)
check("peer diagnostic does not clobber existing evidence", len(client.calls), 1)

generic_body = gh_mod.GitHub._stuck_issue_body(1388, "its review errored", other_diagnostic)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": generic_body}])
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("specific diagnosis replaces generic issue", client.calls[-1][:2], ["issue", "edit"])
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": generic_body}])
client.ensure_stuck_issue(1388, "another peer failed", other_diagnostic.replace("codex", "claude"))
check("equally generic peers do not churn", len(client.calls), 1)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": existing_body}])
client.ensure_stuck_issue(1388, "another peer failed", diagnostic.replace("claude", "codex"))
check("equally specific peers do not churn", len(client.calls), 1)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
