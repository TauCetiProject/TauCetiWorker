# Security audit without copied tokens

Status: implemented in the working branch. Deployment and restarting the paused
audit remain separate actions.

## Outcome and trust assumption

The model requests a numbered source chunk and returns only its verdict and
findings. The Worker tracks which chunks the reader served. Remove continuation
tokens from tool arguments, tool responses, prompts and final answers.

Trust the supported client to deliver a successful, suitably sized tool response
intact. The reader's receipt establishes that it served the source; it does not
prove model receipt or understanding. This deliberately replaces the current
token-echo safeguard with ordinary tool delivery, conservative response sizes
and integration tests. No client patches or new acknowledgment protocol.

## Implementation

1. **Simplify the reader.** Keep `read_page({page: integer})`. Allow the next
   unserved chunk or a reread of an earlier chunk; reject skipping ahead. Return
   the chunk number, source, next unserved number and remaining count. Serialize
   calls so concurrent requests cannot corrupt progress.

2. **Keep responses comfortably below client limits.** Bound the complete encoded
   response, including metadata and escaping, with explicit headroom below each
   supported client's output allowance. Construct and validate it before updating
   the receipt. Oversized or invalid responses fail without crediting the chunk.
   Preserve every source character and its commit, file and version identity.

3. **Retain ordinary progress records.** Keep the existing private receipt tied
   to the immutable input and retained inspection. Record served chunk numbers
   atomically. Rereads do not count as new progress. A crash can require rereading
   a chunk; recovery must not manufacture coverage. Reconcile each launch's new
   coverage with successful client tool-call reports, retaining the contiguous
   confirmed prefix even after timeouts. For Codex recovery, require the matching
   tool output in the saved native conversation that will be resumed. Replay
   unconfirmed reads when no transcript survives. Call this served coverage, rather than proof of delivery
   or review.

4. **Simplify verdict validation.** Require only `verdict` and `findings` from
   the model. The Worker attaches the batch identity. A pass requires complete
   served coverage and a valid final judgment from the same inspection. Preserve
   earlier findings and uncertainty, reviewer isolation, quota checks, and fresh
   admin and ancestry checks before advancing the cursor. Incomplete coverage
   continues the same conversation through the existing recovery mechanism.

5. **Introduce a new protocol version.** Update the reader, launcher, schema,
   prompts, recovery checks and documentation together. New inspections use it;
   existing inspections retain their original protocol and evidence. Do not
   reinterpret an old failed token check as a pass or silently migrate the paused
   release inspection. Remove token-repair paths from the new protocol while
   retaining historical validation needed by existing inspections.

## Validation and rollout

- Test numbered reads, rereads, skipped and concurrent requests, restart recovery,
  wrong-batch receipts, and oversized responses that must not gain coverage.
- Exercise each supported client against a test reader with near-limit chunks,
  Unicode and heavily escaped text. Capture the actual reviewer request at a test
  endpoint and compare its complete source payload with the expected chunk.
  For Codex, check that the saved native conversation preserves those chunks too.
  Public console output alone is insufficient. Require this test for supported
  client upgrades. Run it automatically when missing or stale, serialize probes,
  and cache failures briefly before retrying. A failed check keeps new inspections
  on the existing protocol; retained inspections retry without changing protocol.
- Test premature passes, missing chunks, findings before compaction, and retained
  conversation identity. Complete coverage must never override a source concern.
- Run the security-audit regression suite and repository CI, then review the
  change before a separate deployment. Keep normal fleet admissions enabled.

Success means an inspection can read, resume and finish without copying any
opaque identifier, while missing source and source concerns still block approval.
Changing audit scope, shrinking the source bundle, restarting the paused audit
and publishing the release tag are outside this implementation plan.
