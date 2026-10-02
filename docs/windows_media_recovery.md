# Accepted media recovery on a personal PC

This adds durable **accepted** `/dub` and `/vclone` jobs to the existing PostgreSQL request engine. It does not run anything while the PC is asleep/off, install GPU services, provision models, or resume inside an interrupted GPU operation. Native Windows/GPU/Telegram execution has not been verified here; automated tests use Linux, synthetic files, fake providers/transport and disposable PostgreSQL.

## What survives a restart

- At acceptance, the application copies the input into its persistent spool, verifies its size/hash, then atomically commits the request and its namespace ownership. Only after that commit is a request ID reported as saved.
- Separate single-worker dubbing and cloning lanes remain independent of text and other media. Request/source deduplication, leases, cancellation and topic isolation use the same durable engine as ordinary requests.
- Completed outputs are immutable files with versioned descriptors, checkpointed before sending. Descriptors contain a random namespace/leaf, size and SHA-256, never a restored absolute path. Every restored/send descriptor must belong to the current fenced request.
- Confirmed Telegram receipts replay without another send. Ambiguous `sending`/`delivery_unknown` outcomes are never automatically resent; no fallback message claims Telegram rejected a file when acceptance is unknown.
- A crash **after generation starts but before its output checkpoint commits** pauses the job. `/request <id>` offers an explicit owner-only retry with a possible repeat-cost warning. It does not automatically repeat an uncertain provider call. A retry starts generation from the copied input, not from a GPU checkpoint.
- A completed checkpoint can deliver after restart without regenerating it. A URL-only dubbing input still depends on that URL remaining reachable; downloaded provider URLs are not guaranteed permanent.

## Time and space policy

- `ARTI_REQUEST_DISK_MEDIA_BUDGET_SECONDS`: default/max 604800 seconds (7 days), minimum 60. This is wall-clock queue/paused retention, unchanged by restart or explicit retry. Ordinary request budgets are unchanged.
- `ARTI_DUBBING_TIMEOUT_SECONDS`: default 3600 seconds; `ARTI_VCLONE_TIMEOUT_SECONDS`: default 1800 seconds. Each owned generation process is bounded separately (maximum 14400 seconds). Conversion/probe sub-stages have shorter limits.
- Beyond the job deadline, queued/paused work expires and owned staged files are reclaimed when the application next runs. Seven-day recovery is not indefinite storage. Expired status remains discoverable through `/request`.
- Spool defaults: maximum 512 MiB per staged file, 2 GiB measured total, 8192 scan entries. Inputs and immutable output/retained copies count toward staging quota.
- `ARTI_MEDIA_WORK_MAX_BYTES`: default 2 GiB per active attempt; `ARTI_MEDIA_MIN_FREE_BYTES`: default 512 MiB free space reserve. Workdir usage/free space is sampled every second; overflow cancels and awaits the owned child before releasing its disk lock. This is **not a hard filesystem quota**: a fast writer can overshoot between samples. Concurrent lanes and third-party caches outside the owned workdir are separate from this per-attempt bound.
- Cleanup runs every 30 seconds, at most 20 registered namespaces per pass, with a five-second termination grace and retryable busy/failure handling. Unregistered staging orphans require at least one hour without activity. A committed-but-unconfirmed enqueue is never followed by speculative deletion.

## Location and ownership

`ARTI_MEDIA_SPOOL_DIR` can select an absolute persistent directory. The default is `%LOCALAPPDATA%\Arti\media` on Windows, or `$XDG_DATA_HOME/arti/media` (`~/.local/share/arti/media`) on Linux. Keep both PostgreSQL and this directory; copying only the database cannot recover files. Do not put the spool in an automatically cleared temporary directory.

The spool rejects traversal, symlink/reparse/junction paths and special files, validates descriptors and checks request ownership. Atomic staging and namespace leases coordinate generation, transport and cleanup. It preserves user originals and saved samples. Only explicitly claimed application-generated temporary inputs under the known intake directory are deleted after a complete staged copy and confirmed database adoption; file identity is rechecked before deletion. No general filesystem path is restored from a queued request.

This is application-level isolation, not encryption or a security boundary against the OS account/admin. Windows inherits the per-user application-data directory permissions; native ACL behavior is not verified by Linux tests. Database backups and OS backups have independent retention.

## Completed results and voice saving

Results over Telegram's configured 50 MiB delivery limit retain **only a dedicated final-file copy**, not the generator's entire workspace, for 24 hours. `/request <id>` shows the artifact ID and expiry to its owner. On the same PC, with the bot's database environment, export it to an explicit new destination:

```
python -m tools.export_media_result --request REQUEST_ID --owner TELEGRAM_USER_ID --output chosen-output.mp4
```

The tool validates owner/source/expiry, does not contact Telegram/providers, and refuses to overwrite an existing destination. Local database/OS access remains privileged. Chat messages do not disclose the spool's filesystem paths.

Successful clones from a fresh sample retain a 15-minute, owner/topic-bound “save voice” button. The offer survives restart; selecting it revalidates the retained reference before opening the existing naming/save flow, and the name reply revalidates again before upload. Clicking does not automatically save or share a sample. The existing named-voice storage behavior and its access controls remain unchanged. An expired optional offer cannot block delivery of an already-checkpointed main result. Before any actual send attempt, an expired retained copy may be rebuilt from its still-authorized original; confirmed/ambiguous sends do not renew its lifetime. The interactive name-entry dialog itself is still in memory; after restart, the user can click the retained offer again until expiry.

Cancellation and source/material forgetting override retention. The source marker uses the actual requester and known attachment author, not the filename or a bot callback's author. Source-backed neutral media may survive an unrelated group participant's erasure, but reset, deletion of any supporting source, scene/authority changes, and material/reference revocation still fence restoration/delivery. Deleting or replacing a saved-voice library record revokes its versioned accepted copies. Legacy records still do not retroactively acquire missing original-uploader provenance.

## Owned process boundary

Windows 10+ launches payloads suspended with an atomic Job Object assignment (`PROC_THREAD_ATTRIBUTE_JOB_LIST`) and kill-on-close ownership before execution. Parent-death lease loss and cancellation close that job; unsupported job assignment fails closed. It never enumerates/kills unrelated processes or uses `taskkill` against a recycled PID.

POSIX uses a parent-death lease watchdog and an owned process group, retaining the group leader until cleanup to avoid PID reuse. Parent exit, cancellation and descendant cleanup are exercised with real synthetic child processes. Arbitrary `SIGKILL` of the watchdog itself cannot provide kernel Job Object/cgroup guarantees; no cgroups or security settings are installed.

The voice-clone provider cascade now runs in its own owned process, so Python client threads are contained locally. This **does not cancel work already accepted by an independently running local GPU HTTP server or remote provider**. That external work/charge may finish; interrupted-generation retry requires the owner's decision.

## Verification and remaining limits

Default tests contain cross-platform contracts and real POSIX/fake-provider execution, with no intentional Windows skip. A separate opt-in native process smoke is available:

```
python -m tools.media_process_smoke --platform windows
```

It must run on Windows and exercises synthetic owned child/descendant containment, not GPU generation. Linux validation ran the analogous `--platform posix`. Native Windows job/ACL behavior, real videotrans dependencies, model/server compatibility, actual Telegram uploads and GPU cancellation remain unrun.

Downloads, extraction/noise-cleaning and unfinished interactive setup **before durable acceptance** remain a separate boundary. Pre-acceptance legacy temp files can outlive a crash; this change does not sweep arbitrary old temp directories. Main worker progress/failure notices are bounded/sanitized, but no universal end-to-end guarantee is claimed for every menu callback or external provider. After definite pre-send failure, status is updated and a safe failure notice is attempted; cancellation or ambiguous media delivery never triggers a fallback resend.
