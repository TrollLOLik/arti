# Source-grounded memory correctness

This change addresses five reproducible retrieval/prompt defects. It does not
change emotion, initiative policy, interpretation models, or the authority to
send messages. Verification uses synthetic observations and disposable databases.

## Current claims and historical truth

`beliefs_for_query` ranks current beliefs using both query terms and the exact
subject/predicate/condition keys of recalled historical assertions. A concise
correction can therefore remain relevant after its artifact falls outside the
old last-16 window. New belief versions record supersession source and time;
old stored versions are reconciled by their existing typed key without requiring
an eager migration. Historical utterances retain their original wording and
carry `belief_history` status/validity rather than being rewritten as current.

Prompt assembly prioritizes relevant current beliefs, then whole recollection
objects. The conservative unknown-tokenizer budget remains 4,000 UTF-8 bytes.
Tests check the assembled final prompt, not only persistence or candidate recall.
Retrieval does not create a belief or increment its evidential confidence.

## Source dates and uncertainty

Reconstructed, archived and public records preserve observation time and source
event time separately. `occurred_at` is the source event timestamp, not a parsed
date of the event described by the speaker. Missing legacy occurrence timestamps
remain null; no local timezone is inferred. Precision, modality, status and
validity fields reach the reply prompt. The generation instruction explicitly
preserves these distinctions, including with custom system prompts and RP.

## Public group memory

A separate `PublicMemoryRepository` searches actual `group_observations` joined
to their immutable sources, beyond the last-24/64 dialogue windows. It never
opens private trace/belief retrieval across owners. Access requires the exact
observed chat, topic, persona, mode, current scene and audience, retained source
and observation, active authority, current epoch, original author, no opt-out,
and an allowed dependency chain. Bot/delivered records are conservatively
excluded. Anonymous channel authors retain their sender reference.

Public records are attributed source utterances, not personal facts about the
asker. Quotation/report framing and original source dates remain visible.
Private, unknown-audience, unobserved and other-topic material cannot enter this
path. Inclusion and send-time checks revalidate all public IDs and source
policies; confirmed replies retain source dependencies for later erasure.
Chat advisory locking precedes context locking for public retrieval and delivery.

This public path is a bounded lexical search, not a semantic paraphrase index.
A question with no shared search terms may return no memory. Whole retrieval is
limited to one second (750 ms per owned SQL statement), rolls back partial records
on timeout, and returns no public memory under load. Inclusion/validation timeouts
fail closed. Existing observed-message access does not grant initiative rights.

## Accessibility versus fidelity

Rehearsal can change accessibility and its stability, but quality ages from the
original observation against a frozen `fidelity_stability_days` baseline.
Existing payloads without that field use their stored stability until their next
reactivation freezes it. `last_recalled` never establishes renewed fidelity.
Only actually reconstructed, unchanged details receive recall reinforcement;
empty or masked details are not rehearsed. Explicit archive verification can
show source wording but does not reset the ordinary trace's fidelity.

## Long-source windows

Migration `032_semantic_source_chunks.sql` permits offset-keyed vectors per trace.
The background local index lazily reindexes existing prefix-only traces using a
new version, with revision, epoch and source-erasure rechecks before commit.
Raw text is not duplicated in the vector table. Every candidate is still scoped
by source ownership and context before ranking.

Sources longer than 420 characters use 640-character windows with 100-character
overlap. At most 32 windows per source and 32 encoded windows per backfill call
are processed. Long maximum-size sources use distributed windows including the
end; beyond approximately 17,000 characters, interior gaps are possible. This is
not a full-document recall guarantee. A 500 ms raw-source lexical fallback can
locate exact-term windows even in those gaps or without the optional encoder;
SQL LIMIT alone is not treated as a bound on scan work.

Returned windows preserve source modality, author, offsets and opening framing,
and are labeled observed message excerpts rather than personal assertions.
They use original-age decay and do not restore faded details through their
prefix. Names or dates already known to have faded remain masked. Suppression
removes all windows in the same vector invalidation path; late encoding cannot
restore them. The original selective trace remains unchanged.

## Verification

- Exact aggregate: `python -m tools.run_cognition_tests`
- Public scope: `python -m unittest tests.cognition.test_public_memory`
- Source/fidelity: `python -m unittest tests.cognition.test_memory_correctness tests.cognition.test_memory_fidelity`
- Real local encoder: `python -m tools.verify_memory_correctness --model-dir /path/to/verified/semantic-minilm`

Run SQL commands only against disposable test PostgreSQL with `ARTI_TEST_DB=1`.
The encoder command requires the pinned files and validates their manifest. It
uses recorded synthetic interpretations, calls no answer provider, and is not a
measure of live interpretation or generated-answer accuracy. Reports contain
counts and test/encoder identities, never working conversation contents.

Final provider-free verification (2026-10-02 UTC): 838/838 aggregate tests passed,
with no failures, errors or skips; 48 new memory regressions are included. The
separate real pinned-encoder run passed 8/8. See the counts-only reports
`evaluation/memory_correctness_validation.json` and
`evaluation/memory_correctness_encoder.json`. The aggregate runner regenerated
`evaluation/automated_tests_full.json` for the final code; prior reports remain
in Git history. No production database, real conversation, provider or Telegram
transport was used.
