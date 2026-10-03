# Source-grounded memory correctness

This document describes source-grounded recall, resumable indexing, privacy
fences and verified operational limits. The changes do not alter emotion,
initiative policy, interpretation models or the authority to send messages.
Verification uses synthetic observations and disposable databases.

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

Public retrieval now combines Russian PostgreSQL FTS with a separate local
MiniLM index (`cognitive_public_semantic_vectors`). Public vectors refer directly
to authorized observed sources, including observations without any private
projection. No private vector or belief is joined into public search. Semantic
candidates must pass the same recursive `_VISIBLE` predicate and strict source
wire validation as lexical candidates and final delivery.

A local query embedding gets at most 250 ms; the subsequent public read has a
one-second total deadline, including pool/lock waits. Optional semantic SQL uses
a savepoint with a 150 ms budget (120 ms per statement), allowing lexical results
to survive semantic degradation. Public validation still fails closed on timeout.
These are workload limits, not claims that every allowed source can be scanned
within them. Existing observed-message access does not grant initiative rights.

## Accessibility versus fidelity

Rehearsal can change accessibility and its stability, but quality ages from the
original observation against a frozen `fidelity_stability_days` baseline.
Existing payloads without that field use their stored stability until their next
reactivation freezes it. `last_recalled` never establishes renewed fidelity.
Only actually reconstructed, unchanged details receive recall reinforcement;
empty or masked details are not rehearsed. Explicit archive verification can
show source wording but does not reset the ordinary trace's fidelity.

## Long-source windows

Migrations `033_semantic_coverage.sql` and `034_public_semantic.sql` add durable
progress for separate private/public indexes. Migration 033 discards old sampled
vectors because they cannot certify full coverage. The original source and
selective trace remain in place. Both indexes record source fingerprint/hash,
embedding version, suppression epoch, unique work generation, next window and
total windows. Vectors and the cursor advance in the same transaction. A restart
resumes incomplete sources; an early vector is never a completion marker.

Sources longer than 420 characters use deterministic 640-character windows with
100-character overlap in the private index; short private sources are embedded in full while
their returned selective trace and fidelity remain unchanged. Public indexing always reads original observed source windows.
Logical windows are also subdivided against the pinned model's actual tokenizer
limit. Each part is verified to fit without truncation, embedded locally, and
pooled into the window vector. This avoids hidden token-limit gaps inside a
character window; it can still dilute an isolated semantic signal, so coverage
is not a promise that every query will retrieve it. The embedding version includes
this encoding change.
There is no 32-window source cap or distributed sampling. For example a
100,000-character source has 185 contiguous windows. Those vectors contain about
555 KiB of float data before PostgreSQL/index overhead, so full coverage uses
more storage than the old 32-window sample. Each worker invocation is
still bounded to 32 encoded windows, shared across selected sources, with durable
least-recently-attempted scheduling. Actual inference batches shrink after
background timeouts, down to one logical window, so dense multilingual inputs
do not repeatedly overrun the same batch. Public gathering reserves time for
encoding/commit and rotates past locked contexts. Model failure leaves progress incomplete and
the background worker retries. Private and public work share one local encoder;
there is no network service or production message upload.

Every batch rechecks permissions and source identity after encoding. Deletion,
semantic correction, changed dependency, epoch or scope invalidates cached work;
a fresh generation prevents a delayed old batch from committing after a source
was changed and restored. Retrieval also applies the fences independently of
invalidation. Rehearsal-only private revisions do not discard semantic coverage.

A 500 ms raw-source lexical fallback reaches unindexed private text. Excerpt
positions use the same PostgreSQL Russian FTS stemmer that selected the source,
including inflected words that are not literal query substrings. Cyrillic case
normalization is explicit and one-character-to-one-character even in PostgreSQL
C locale; displayed source text is unchanged. A full lexical conjunction takes
precedence over topical semantic distractors, preserving exact-name queries. Highlight
markers are collision-checked and the entire original text must reconstruct
exactly before offsets are accepted. Up to three distinct relevant passages per
source can survive hybrid merging; total response and prompt budgets still apply.
The first matching vector no longer replaces all other excerpts from a source.
Multiple excerpts retain one private artifact identity and do not count as
independent corroboration or multiply rehearsal/link activation.

Returned windows preserve source modality, author, offsets and opening framing,
and are labeled observed message excerpts rather than personal assertions.
They use original-age decay and do not restore faded details through their
prefix. Names or dates already known to have faded remain masked. Suppression
removes all windows in the same vector invalidation path; late encoding cannot
restore them. The original selective trace remains unchanged.

## Retrieval health and diagnostics

Retrieval returns a list-compatible `RetrievalResult` with per-call diagnostics:
`status`, `semantic_status`, `lexical_status`, `scope`, and scoped source/window
coverage counts where available. Status distinguishes `complete`, `incomplete`,
`unavailable` and `timeout`. Counts describe the currently permitted owner or
public audience, not a global ledger. An empty list never by itself establishes
that no evidence exists. A completed bounded query also is not proof an event
never happened or that a paraphrase will match the model's .42 threshold.

`PreparedTurn.retrieval_diagnostics` survives the durable request codec. Generation
receives trusted guidance to acknowledge incomplete checking, even when no memory
record is returned; it must not turn partial coverage or timeouts into a confident
"nothing was stored" answer. Private prompt inclusion and final delivery recheck
all selected artifact IDs and their recursive source ownership/payload chain;
revocation after retrieval blocks transmission. Private dialogue reset remains
dialogue-only and preserves autobiographical recall; the public reset boundary
continues to exclude older public observations. `operational_metrics.semantic_cache_progress` exposes
payload-free physical-cache counts for operators; these are explicitly distinct
from query-authorized coverage and can include entries awaiting retention cleanup.
Private semantic reads have a 1.5-second budget and the complete private recall
path has a four-second deadline. Exact SQL dot-product ranking remains O(allowed
vectors); no approximate index or universal latency guarantee is introduced.

## Verification

- Exact aggregate: `python -m tools.run_cognition_tests`
- Coverage: `python -m unittest tests.cognition.test_semantic_coverage tests.cognition.test_public_semantic tests.cognition.test_source_chunks tests.cognition.test_retrieval_coverage`
- Public scope: `python -m unittest tests.cognition.test_public_memory`
- Source/fidelity: `python -m unittest tests.cognition.test_memory_correctness tests.cognition.test_memory_fidelity`
- Existing real local encoder regressions: `python -m tools.verify_memory_correctness --model-dir /path/to/verified/semantic-minilm`
- Public paraphrases and long-source continuity with real local encoder: `python -m tools.verify_hybrid_memory --model-dir /path/to/verified/semantic-minilm`

Run SQL commands only against disposable test PostgreSQL with `ARTI_TEST_DB=1`.
The encoder commands require the pinned files and validate their manifest. They
use synthetic recorded interpretations and never invoke answer providers. These
are not measures of live interpretation or generated-answer accuracy, nor a
production-size throughput benchmark. Reports contain counts and test/encoder
identities, never working conversation contents. Prior validation reports remain
historical evidence for their original versions; the current report is
`evaluation/hybrid_memory_validation.json` with real-encoder details in
`evaluation/hybrid_memory_encoder.json`.

Final provider-free verification (2026-10-03 UTC): 940/940 aggregate tests passed
with no failures, errors or skips; 78 new memory regressions are included. The
unchanged videotrans tests also pass after installing their missing test-only
SoundFile dependency in the disposable environment. Real local-encoder source
regressions passed 8/8. On the frozen public corpus, hybrid top-three recall is
16/20 versus 1/20 lexical; all five exact-name queries rank the intended source
first in both modes. All four paraphrase misses remain in the report. An
82,324-character source indexed all 153 contiguous windows and recovered its
interior paraphrase after index restart. These synthetic measurements do not
establish production-size latency or perfect semantic recall.
