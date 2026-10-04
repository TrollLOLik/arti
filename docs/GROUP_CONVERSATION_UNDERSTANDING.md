# Semantic group conversation understanding

## Scope

This phase builds on merged master `b9bf0a7f6c4fe382f8d370dd5c88eb103687fedb`
and preserves the prior initiative/refusal safeguards. It adds a rebuildable
semantic conversation projection for the exact public audience Arti actually
observed. It does not add a promise executor, transport architecture, numerical
habituation model, or changes to videotrans/audio processing.

## What the projection records

The group's selected model identifies parallel threads by meaning, including
paraphrases without word overlap and practical requests without `?`. It can link
replyless turns to prior supplied sources and observed human addressees. Ambiguous
addressees remain unknown; a nearby message or shared vocabulary is not proof.
Lexical branch IDs remain separate, fallible hints for existing compatibility.
An ambiguous replyless lexical resolution cannot veto semantic arbitration;
explicit scoped refusals and reply-backed resolution retain their safeguards.

The bounded ledger records questions, proposals, decisions and commitments:

- Exact Unicode source spans support every item and later update.
- Authorship comes from observed transport metadata. A report about someone else
  cannot bind them, and an anonymous/bot claim cannot establish a human's consent.
- Acceptance is scoped to the named actor. One person's acceptance is never
  promoted to group consensus.
- A possible answer, receipt, thanks or silence does not prove resolution.
- A refusal/resolution survives unrelated activity and model omissions. A later
  same-actor supported reopening is needed before returning to a nonterminal state.
- Fully supported durable items survive omission from the next model response,
  subject to whole-object capacity limits. Missing items never imply completion.

The extraction prompt, source schema and recorded-output corpus are inspectable
in `ai/group_understanding.py`, `cognition/group_understanding.py` and
`tests/fixtures/group_conversation_understanding.json`.

## Bounded background processing

Understanding runs in an owned background loop alongside the existing initiative
loop. No new model call occurs on Telegram ingress, frame/history reads, or direct
response preparation. There is one selected-model request per admitted extraction,
without retries or a schema-repair provider call.

| Bound | Value |
|---|---:|
| New raw observations per extraction | 24 |
| Raw sources, including retained evidence anchors | 72 |
| Raw input packet | 48,000 UTF-8 bytes |
| Per-source excerpt | 2,400 characters, explicit truncation |
| Threads / links / durable items | 12 / 32 / 24 |
| Evidence spans per object / updates per item | 8 / 8 |
| Full normalized semantic output | 12,000 UTF-8 bytes |
| Semantic display within public packet | 5,500 UTF-8 bytes |
| Whole existing public participation packet | 14,999 UTF-8 bytes |
| Complete cumulative source lineage | 4,096 sources |
| Extractor / whole refresh wall budget | 8 / 12 seconds |
| Frame and final context guard wall budget | 1.5 seconds |
| Frame/guard SQL statement budget | 750 ms |
| Understanding calls per context / rolling hour | 6 |
| Shared optional provider concurrency / rolling hour | 2 / 60 |

The snapshot cursor, leases, generation, schema version, dependency hashes and
source gaps survive restart. Two workers cannot claim the same extraction.
Append-only arrivals can coexist with a safe historical prefix, avoiding perpetual
restart in an active chat. The exposed `current=false`, as-of cursor and coverage
bounds tell consumers that more recent evidence may supersede the projection.
Late observation attachment behind the cursor invalidates it and replays surviving
raw history. Malformed or individually over-budget sources are recorded as coverage
gaps so later independent sources continue. They never become evidence.

Each new extraction uses freshly authorized raw wording, including retained
origins and outcome anchors. Previous model prose is not sent back to the model.
Local reconciliation can retain an old item only when all its exact raw evidence
and thread support can be revalidated. Whole item groups are retained or omitted;
a summary is not kept after dropping its qualifying refusal/correction.

All prior consulted sources and prior anchor-selection influences remain in the
complete lineage, even when the current model no longer cites them. At the lineage
cap, the next compression starts independently from deterministic new raw sources.
It explicitly reports the reset and incomplete historical coverage. It cannot keep
old model-selected context while silently trimming its hidden dependencies.

## Privacy, erasure and freshness

The projection reads the public ledger through the same exact context/audience,
wire-authorship, opt-out, history boundary, retention, scene and recursive
provenance checks as public memory. It never reads private traces or beliefs.
Complete transitive dependencies are preserved across different human owners.

SQL invalidation erases derived content after source edits/deletion, dependency
edge changes, opt-out, reset, policy/scene/authority changes and response disable.
Fresh reads also enforce time-based retention. Reconstruction starts from surviving
raw evidence. Wrong-audience reads cannot destroy another audience's valid state.
An in-flight provider result cannot recreate an erased generation.

Direct prompts and proactive judges receive bounded source-linked hypotheses.
Exact source/edge hashes, complete lineage, snapshot generation and conversation
revision travel with the prepared response and its durable queue representation.
They are rechecked at actual provider dispatch (including retries), restored queue
work and the final delivery boundary. `mark_included` preserves history lineage.
Delivered derivatives depend on every consulted source, including uncited inputs.
Final direct-prompt budgeting reserves recent raw turns first and keeps semantic
JSON whole. If raw context itself must be truncated, the older semantic block is
omitted; it cannot displace a newer correction or lose its as-of envelope.
Only an exact own confirmed receipt can advance a multi-part reply's revision;
intervening human context or snapshot changes remain blockers. Explicit reminders
that do not consume semantic context do not acquire unrelated snapshot guards.

## Verification

The final frozen aggregate, corpus replay and synthetic load measurements are
recorded in `docs/evaluation/group_understanding_validation.json`,
`group_understanding_corpus.json` and `group_understanding_load.json`.

Final frozen aggregate: **1,282/1,282 passed**, no failures/errors/skips,
552.336 seconds. This includes 145 new regressions and the unchanged 24 videotrans
tests. Two independent reviews found no remaining blocker. The recorded corpus
passed 30/30 episodes and 18/18 adversarial inputs; it is not a live-model score.

The final synthetic load used 1,000 messages, 40 participants and five topics.
Pending candidates stayed at or below 128; twelve recorded initiative assessments
and thirty recorded understanding batches respected the shared limits. Ingress
p50/p95 was 25.25/62.24 ms locally. After six batches/context, 280 observations remained
explicitly pending, demonstrating quota-limited coverage rather than claiming all
1,000 messages had been understood. Provider latency/cost and live semantic quality
were not measured.

Focused commands:

```sh
python -m unittest tests.cognition.test_group_understanding_schema tests.cognition.test_group_understanding_model
ARTI_TEST_DB=1 python -m unittest tests.cognition.test_group_understanding_store tests.cognition.test_group_understanding_integration
python -m tools.evaluate_group_understanding
python -m tools.run_cognition_tests
```

All conversations and semantic outputs are synthetic. Network-blocked test runners
use disposable PostgreSQL, mocked providers and mocked Telegram. No production
history, credentials, live Telegram or paid API calls are used.

## Honest limits

Recorded semantic outputs test schema, attribution, evidence, lifecycle and
integration contracts. They do not establish a live model's topic/addressee
accuracy, social naturalness, calibrated confidence, or production performance.
Labels and inferred relations remain hypotheses after their quotes are verified.

This is a bounded working conversation model, not an all-time transcript or an
unlimited task tracker. Capacity, text truncation, skipped malformed/oversized
sources, the lineage cap, retention and provider quota can omit context or delay
catch-up. Six successful 24-new-source batches cover at most 144 new observations
per context per rolling hour; failures also spend quota. A high-volume context can
remain behind. Current raw messages still accompany historical semantic state,
and permission/relevance must be assessed again before initiative.

Human IDs must already be observed. A name alone does not resolve an identity.
Arti's delivered source can be a relation target, but human commitments and
addressee IDs are not silently assigned to the bot or its request owner. No new
executor fulfills arbitrary promises. Private memory never fills missing public
context.

A provider request or transport call already in flight cannot be recalled.
Dispatch/final-send guards prevent later stale reuse, not retroactive cancellation.
