# Fix verification failures and recovery backlog

Repair the verifier's Python environment, keep tooling failures out of source repair, and resolve the resulting backlog. Preserving unpublished source remains a safety net. Repeatedly restoring it, warming caches, and launching source repair for the same missing dependency must stop.

Change no worker counts, admissions, subscriptions, spending allowances, retention targets, or source-expiry rules. Deploy live with normal worker and CI admissions enabled. Use the existing verifier, readiness checks, journals, scheduler, and branch claims; add no daemon, readiness database, fleet-wide failure latch, or new retry ceiling.

## Confirmed failure

On tauchonki on 6 November 2026, seven inspected recovery records had a latest failed shim-expiry command ending with `ModuleNotFoundError: No module named 'tauceti_tools'`. They include unpublished rebase commits and unfinished fixes. Some original rounds exited successfully without a published head; others left dirty source or exited abnormally. This selected sample does not establish the cause of every preserved record.

`verification.py` launches the checkout's `scripts/check-expired-mathlib-shims.py` using `python3` from `PATH`. System Python cannot find Tools, whereas the deployed worker interpreter can. The wrapper imports `tauceti_tools.templates`; an uncaught import failure exits 1 and becomes verification `failed`. Recovery can then invoke source repair. The sampled journals include `exact-source-verification-failed` and exhausted repair allowances.

The wrapper's `run_script()` also launches Python by name. However, the deployed shim implementation and its sibling `lean_source.py` need only standard-library and sibling imports. That nested launch does not need Tools installed, so changing the general launcher is not a prerequisite for this repair.

Evidence: `/home/kim/.local/state/tauceti-fleet/preserved-work-diagnosis-20261106.json` and the adjacent `machine-lake-cache-savings-20261106` directory. Revalidate production revisions before implementation; preserve deployed legacy data-root support and unrelated unmerged changes.

## 1. Fix interpreter selection and record the actual tools

Run the shim wrapper as `[sys.executable, script, ...]` with the verifier's inherited environment. Do not add `-I`, `-E`, or `-P`: Bubble needs its existing `PYTHONPATH=/opt/tools`, and supported standalone checkers may import sibling modules. Add no new global path entries, packages to system Python, or global executable replacements. Preserve policy, arguments, and source files.

Host checks use the Worker's installed Tools assets, materialized with project configuration and policy. They do not automatically load the content repository's Tools pin. Preserve that behavior, record the installed implementation, and keep CI as the authority. Fetching a different Tools version for every saved checkout is outside this fix. List Worker/content pin differences when diagnosing local-only failures.

The verifier already records its own interpreter in `inputs['python']`. Add Tools implementation identity separately, using existing installation/file inventory helpers, and bind the actually used Python, launcher, checker, helper, and materialization/policy inputs to receipt validation. Do not insert extra entries into `inputs['verifier']`: its existing key-set validation would make every run incomplete. Include Tools files in the static change watch and extend existing `verification_jobs.tool_readiness()` with the selected interpreter and relevant Tools files. Avoid new scans of workspace/build trees or mutable `__pycache__` contents.

A general TauCetiTools change from `python3` to `sys.executable` can be a separate follow-up after testing other launchers. Do not require a broad Tools release and Worker pin change to fix this import failure.

## 2. Detect the known infrastructure failure cheaply

Add one stateless prerequisite function that probes `tauceti_tools.templates` in a child using the same interpreter, inherited environment, and working directory as the shim wrapper. Invoke it when the Tools-dependent shim checker is selected, and at the recovery entry point before branch acquisition, attempt charging, and cache warming. Keep supported older standalone checkers working; do not require Tools merely because a file has the same name.

A failed worker-owned probe yields the existing `incomplete`/`tool-missing` result or equivalent infrastructure wait. Reuse existing infrastructure backoff and job readiness. No model continuation, source-repair charge, or failed-source verdict follows from that result. A package repaired under the same interpreter path must be detected automatically; a failed probe has no persistent latch. The probe does no dependency installation, artifact inventory, GitHub request, or project build.

Keep ordinary checker failures classified as failures. Its current exit 2 mixes invalid source/registry data with unavailable Lean probing; exit 3 is a real policy violation, and the wrapper's import traceback exits 1. None can be reclassified wholesale as infrastructure. For this repair, only the explicit prerequisite failure gets the tooling-unavailable classification. A later Tools change may introduce an unambiguous infrastructure result, but must preserve genuine policy failures and test the contract end to end.

Place the probe in existing direct and queued verification paths as well as recovery. A dependency disappearing after preflight must never create a success receipt; use an explicit worker-owned recheck or diagnostic to distinguish a newly missing prerequisite without treating arbitrary project import errors as infrastructure.

## 3. Retry after tooling changes without repeating preparation

Keep full source/dependency fingerprinting at its existing prepared boundary: `inputs()` runs Lake and inventories dependencies, so it cannot simply move before cache preparation. The cheap prerequisite check prevents the confirmed missing-package failure from paying that cost repeatedly. Genuine source failures retain their existing recheck schedule.

For cached failed-source decisions, record a small verification-tool identity alongside the existing `failed_*` journal fields, using the verifier and installed Tools identities already maintained above. Apply the pre-verification source-continuation shortcut only while that identity still matches.

Handle records already blocked by the erroneous source verdict. Some can be held indefinitely by continuation-only reasons such as missing model profile, unsupported continuation provider, or billing-policy mismatch; a fixed verifier alone does not unblock them. Permit one fresh mechanical verification when the relevant tool identity changes. Treat a missing identity in a legacy failed record as needing that first check, then record the new identity so it cannot repeatedly trigger the same replay.

Scope this reopening to records with a prior failed-source verdict that led to continuation admission. Preserve original publication scope, source ownership, sandbox, foreign-work, branch, and authorization checks. A tool update must not bypass unrelated permanent blocks or authorize a model invocation under a different billing policy. No blanket reset of recovery states, attempts, budgets, or historical receipts.

Other records already retry automatically through the existing scheduler, subject to their recorded retry times and failed-source recheck interval, which can reach roughly six hours. Do not build a replay service or enqueue a fleet-wide retry campaign.

## 4. Reconcile the backlog and any unnecessary source edits

Produce a metadata-only report: worker, PR, saved source identity, original round outcome, disposition, last actual failure, retry time, continuation block, tool identity, and source-repair allowance already charged. Separate source bundles/patches from disposable restored workspaces and builds. Distinguish source recovery from the GitHub publication outbox.

For continuations launched after a tooling-error verdict, show their delta against the original checkpoint. An unnecessary repair may have changed otherwise valid source; a later green build alone does not establish that those edits were needed. Assess those deltas under existing source/publication rules, without adding a new approval gate.

Let the existing scheduler revisit eligible records after the canary. Reconcile already published, superseded, or closed-PR work using current shared GitHub facts and normal claims. Multiple sampled records name PR 28151: establish whether they preserve distinct work before choosing an owner or consolidating anything. PR identity alone does not establish duplicate source.

Previously misclassified records get a fresh mechanical check under the repaired tools; old receipts stay historical. Preserve genuine source-repair allowances. Report allowance spent on this infrastructure defect separately; refunds or increases require an operator decision.

After publication, reconciliation, or another valid terminal disposition, reclaim disposable restored checkouts and generated artifacts using existing ownership/reference/source-preservation rules. Keep unpublished source recoverable. Invent no source expiry or cache limit to reduce the record count.

## 5. Tests and live validation

Extend existing verification/recovery tests with real subprocess coverage where interpreter behavior matters:

| Scenario | Required behavior |
| --- | --- |
| Worker Python has Tools; a real PATH Python does not | The checkout wrapper runs successfully with Worker Python. Use a real bare environment, not a fake `python3` executable that breaks the legitimate nested stdlib script. Demonstrate failure on the old command. |
| Bubble and standalone sibling-import checkers | Existing mounted Tools and sibling imports remain usable with the inherited environment. |
| Required Tools missing across repeated scheduler passes | Infrastructure result before cache warming; no source continuation or source charge; existing scheduling allows unrelated work. |
| Tools restored under the same interpreter path | Stateless probe/readiness detects the repair; no manual journal reset. |
| Legacy failed record blocked on continuation admission | Changed tool identity permits one fresh mechanical check while all source/publication authorization checks remain effective. |
| Same tool identity, or unrelated permanent block | No repeated reopening and no policy bypass. |
| Actual shim/source violation | Source failure remains failed; existing allowances and publication requirements apply. |
| Direct, queued, recovery, interruption, and mid-run tool changes | Consistent outcomes, exact tool identities, no false success, and preserved source. |
| Already published, superseded, or concurrently claimed saved work | Reconciliation prevents duplicate publication and protects another worker's source. |

Run the relevant verification, local-audit, recovery, saved-work, and continuation suites, then normal repository CI. Stage immutable runtimes preserving deployed data paths and unrelated changes. Canary at natural worker boundaries without draining or pausing admissions. Demonstrate a previously affected checker gets past the import failure and an eligible saved source reaches a real disposition. Expand through ordinary boundaries while preserving active paid rounds. Rollback selects the previous runtime for subsequent work and retains source and journals.

Observe several real worker rounds and at least two ordinary cleanup publications; these are validation observations, not admission limits. Completion requires no recurrence of the confirmed import failure, no infrastructure-triggered source repair or repeated expensive preparation, and affected records advancing to publication, reconciliation, or specific genuine source/policy blockers. Explain new recovery arrivals and the backlog trend; a smaller queue count or a green process count alone is insufficient. Check for duplicate publication, lost source, or stranded work.

## Outstanding session work

Before closing this session, return explicitly to **retained build trees and shared workspace/dependency sources**, as the operator requested. They remain separate work and must not delay this verification fix. Old-cache consumer retirement is also outstanding: use working verification and measured preparation/storage costs to complete that decision. Repairing imports or deduplicating some blocks does not retire the old pool.
