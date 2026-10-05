# Durable ordinary requests and latency budgets

## Scope

This document describes ordinary text and image/video/music requests in PostgreSQL `arti_requests`. The old process-local queues are no longer their source of truth. Startup starts ten text consumers and one consumer for each media kind. Accepted disk-backed dubbing and voice-cloning jobs use the same durable engine with the separate recovery policy in `windows_media_recovery.md`. Menu intake before acceptance and independent agent/task workflows remain outside the ordinary-request guarantee.

An accepted request is a committed row. A Telegram update that has not reached enqueue, downloaded input that has not been committed, and an in-progress provider operation whose result was not checkpointed can still need to be repeated. This is not exactly-once generation or delivery.

**Important intake boundary:** Telegram polling can acknowledge an update while its handler is still preparing it in memory. Per-conversation intake sequencing does not make those updates durable. A crash before the request commit can still lose an input; downloads, transcription and unfinished menu setup are not a universal restart-recoverable inbox. Only committed requests receive the accepted-input recovery guarantees below.

## Ordering and recovery

- Ordinary Telegram handlers reserve their chat/topic lane before menu lookup, database observation, transcription or other expensive preprocessing. Later same-lane inputs cannot overtake them; other lanes use the normal 32-work limit. Admission is bounded to 64 pending handlers per lane and 512 overall. Overflow is explicitly refused as not saved, with a bounded best-effort notice.
- `/cancel`, `/stop` and `/request` use four separate bounded control permits. Existing handler authorization remains authoritative. Owner cancellation joins preceding matching intake before cancelling accepted work, so interrupted preprocessing cannot submit an old request afterwards. Other owners and topics remain independent.
- This adapter intentionally wraps PTB's public `process_update` despite its `typing.final` annotation, because the standard `do_process_update` hook runs after the global semaphore. It preserves awaited handler/error/cancellation semantics and does not detach intake tasks or mutate PTB's private semaphore. Pending-coroutine closure, control bounds and shutdown behavior were tested against the installed PTB version. The dependency range allows PTB 21–22; this is not a full version-matrix validation, so retest the adapter on upgrades.
- FIFO is per chat, topic, and request kind. Image/video/music never hold up text; two text requests in a topic cannot execute concurrently. Different topics/chats remain independent.
- Compatible same-author text inputs coalesce only while still queued, within the existing 0.6-second rolling debounce and two-second total bound. The merge, source/access provenance and per-source deduplication tombstones are committed atomically; running work is never changed. Different authors, topics and contexts never merge. A coalesced request ID resolves to its parent status.
- Telegram source identity, kind, topic, and media prompt produce a stable deduplication key. Child media also includes the parent request ID. Duplicate intake cannot resurrect cancelled/finished work.
- Sixty-second leases, renewed every fifteen seconds, and random fencing tokens protect state changes and the pre-send boundary. An expired worker cannot checkpoint, claim completion, or start a new send.
- A clean shutdown cancels execution and releases safe work for another startup. An abrupt crash is recovered after lease expiry. Remaining wall-clock budget is not reset by either path.
- Request cancellation cleanup is owned and joined through repeated cancellation, with a ten-second operation timeout; pool shutdown waits for worker cleanup. Lease-health failure stops polling first, completes shutdown, then retries database/lease acquisition through the existing bounded backoff. Explicit stop and polling conflicts remain terminal; a competing poller's lease prevents restart admission.
- Completed cognitive preparation, final generated text, ordinary TTS reply bytes and image/video/music provider results are checkpointed. Guarded material/project/workflow/computation references are reconstructed and validated against current access/generations. Generated replies retain their corresponding access guards.

## Telegram delivery

Each response send has a durable `prepared` intent, a committed `sending` marker before the API call, and an identifier-only confirmed receipt. Confirmed receipts are replayed locally; Telegram is not called again. Recovery uses the stored prepared body rather than a changed regenerated body. Cognitive outbox keys additionally include request IDs so separate child media requests do not collide.

Ordinary text and unavailable-TTS fallback replies use a checkpointed HTML part plan. Parts are bounded to 4,000 UTF-16 units after entity decoding; tags/entities/code points are not split, invalid nesting is normalized, and formatting is closed/reopened at each boundary. Each confirmed part has its own stable durable ordinal and real assistant/delivered-action receipt linked to the original input. It does not become new user evidence or a fictional single-message receipt. Recovery skips confirmed parts. Voice captions use the same sanitizer within their smaller limit.

Explicit Telegram `RetryAfter` rejections restore the same prepared identity and persist the next allowed attempt in both delivery ledgers. Delays survive restart and all source/lease guards run again before transport. Cognitive-only sends also preserve the server's delay. Explicit validation/authorization rejections are safely failed rather than labelled ambiguous; no blind format-changing resend is performed. Network timeouts, missing receipts and cancellation while an API call may have succeeded remain ambiguous. A crash between the two ledger writes can conservatively suppress a safe send; it never grants permission to resend an uncertain one.

If Telegram may have accepted a send but no durable confirmation exists, the request becomes `delivery_unknown`. It is deliberately not resent, including after restart. Cancellation or failure during a send also preserves that uncertainty. A cosmetic waiting notice has a separate ordinal and cannot make the final reply unknown. Native Telegram cannot prove exactly-once delivery; a crash immediately after the pre-send marker can conservatively suppress a send that never reached Telegram.

`/cancel` commits cancellation before cancelling local execution. Already-started network/provider operations cannot always be stopped; fencing prevents new sends afterward. Forgetting erases dependent durable input, generated checkpoints, and prepared send bodies, without cancelling unrelated owners' requests. Terminal rows retain deduplication and operational metadata, not conversation bodies.

`/cancel <request-id>` is owner- and chat/topic-scoped, including for ordinary group members. A non-admin group's plain `/cancel` cancels that owner's latest active accepted request plus preceding unaccepted intake. It cannot stop another participant's accepted requests. Status includes the owner-cancel command; admin/private whole-chat behavior remains available without an ID.

## Scoped location retention

Location samples are keyed by receiving chat, topic and user. Pending map prompts also include conversation mode. A private pending prompt cannot be resumed by a group location, and private coordinates cannot enter an unrelated chat/topic's model or Maps request. Legacy user-only cache/database samples do not establish sharing permission and are never restored into the new scoped store.

Static and live samples expire 30 minutes after the original Telegram share/update timestamp; reads, restart and delayed geocoding do not renew that age. A fresh explicit live update starts its own 30-minute window. Expiry is immediate on access; startup and a minute-based worker remove expired scoped rows and old legacy rows. Pending prompts are process-local, expire after 30 minutes and disappear on restart. Database backups retain their separate operator-managed policy.

Cognitive scheduling now ranks eligible context head jobs by queue age rather than permanent context ID. Fresh work continuously arriving in older contexts cannot starve an already eligible newer context; context-local ordering and lease fencing are unchanged. It does not preempt a currently running job or promise a fixed wall-clock wait for a large pre-existing backlog.

## Media and retention limits

The codec is explicit, versioned JSON, with no pickle, executable values, arbitrary path restoration, live client objects, or authentication state. Captured image data, generated byte results and prepared uploads are persisted as bounded base64 (32 MiB per binary value, 64 MiB per encoded value). Telegram file IDs are resolved again on recovery. A remote provider URL can expire; results still represented only by URLs are not guaranteed recoverable. Such a failure is terminal rather than silently claiming a completed output. Accepted disk-backed dubbing/voice-clone work now uses the separate bounded spool and interrupted-generation policy documented in `windows_media_recovery.md`; pre-acceptance setup remains outside this guarantee.

Database operators must treat live request/checkpoint bodies as private conversation and media data. Terminalization scrubs them. Database backups have their own retention policy; this application does not rewrite historical backups. Deduplication tombstones and safe receipt identifiers currently have no automatic expiry. A partially executed handoff to an agent workflow is not replayed automatically. The native material route can recover an already persisted task with the same scoped request identity, without recreating it or resending its card. If no safely recoverable task exists, the ordinary request stops with an explicit failure category; interrupted artifact patches are not blindly replayed. See [native agent scenarios](NATIVE_AGENT_SCENARIOS.md).

Provider calls may have incurred a charge before a crash even when no result was committed. Thread-based provider SDK calls may continue after the Python task's deadline; their late result is discarded and may not be delivered. This change does not promise provider-side cancellation or idempotency.

## Latency and status

Budgets start at durable enqueue, include queue waiting and execution, and survive restart:

- `ARTI_REQUEST_TEXT_BUDGET_SECONDS`: default 180 seconds
- `ARTI_REQUEST_MEDIA_BUDGET_SECONDS`: default 900 seconds

The existing short cognition/intent/generation limits remain sub-stage limits. A short wait is not represented as completion. After eight seconds of execution, one wait notice includes the request ID, `/request <id>` and `/cancel`. `/request` without an ID returns the latest request in the current chat/topic, including queued or expired work. Status lookup does not expose another chat/topic's request. Deadline exhaustion stops the job, unblocks the lane, and attempts a bounded timeout notice if transport is still safe; an ambiguous prior response is never retried for that notice.

Structured diagnostics retain only request ID, kind, stage, attempt, queue delay, stage duration and safe error category. The private-payload log filter preserves these allowlisted fields and removes prompts, URLs, media, provider error text and stack payloads. Logs distinguish accepted, started, prepared stage, delivery, blocked, deadline and completed work; they do not assert that a user read a message.

## Offline verification

Run the standard `python -m tools.run_cognition_tests` in a test-only environment with disposable PostgreSQL and dummy credentials, or `python -m unittest discover -s tests -t .` for the same discovery without modifying tracked evaluation reports. New focused modules:

- `tests.cognition.test_request_store`
- `tests.cognition.test_request_codec`
- `tests.cognition.test_request_runtime`
- `tests.cognition.test_chat_delivery_safety`
- `tests.cognition.test_chat_intake_safety`
- `tests.cognition.test_location_privacy`
- `tests.cognition.test_poller_recovery`
- `tests.cognition.test_job_fairness`

Tests use synthetic content and mocked transport/provider calls. They cover restart checkpoints, leases/fencing, lane ordering, duplicate and ambiguous sends, dependency erasure, media serialization, shutdown, deadlines and safe diagnostics. No live Telegram/provider validation is implied by an offline pass.
