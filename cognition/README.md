# Cognitive memory and emotion

The B00–B19 implementation lives here. Runtime shares one source-backed event
cycle across Telegram adapters and disposable simulations. Interpretation proposes
grounded situations/appraisals; Python computes state and validates all effects.
Owner, persona, chat, mode, scene, model version and suppression epoch scope reads.

`affect.py` provides analytic fast episodes/slow mood, concerns, effort/circadian
resources, learned expectations, bounded residues and expression projections.
`relationships.py` keeps independent social dimensions, preferences and implicit
associations. `memory_repository.py` encodes details/episodes, conditional beliefs,
versions, autobiography/intentions, scoped retrieval and bounded replay.

Persistence uses an immutable raw ledger, CAS, leases, provenance, dependencies,
suppression and recoverable projection rebuilds. Actual Telegram receipts produce
own-action events. Delivery failures are not user evidence; ambiguous sends are
never retried automatically. Legacy mutation entry points enforce one authority.

Production always uses active cognition. The runtime promotes current stored
contexts with epoch/lease fencing at startup; retired RP scenes stay retired.
`ARTI_COGNITION_MODE` and `ARTI_COGNITION_MODEL` are obsolete and ignored.
Interpretation, group arbitration/composition and agent planning follow the
chat model selected through the Telegram menu or `/model`, using the same
Gemini or configured OpenAI-compatible provider. OpenRouter is not required.
Offline simulations retain explicit authority variants for regression tests;
they cannot enable legacy emotional mutation APIs in the application.
See `docs/ARTI_COGNITION_RUNBOOK.md` for source migration and recovery.
