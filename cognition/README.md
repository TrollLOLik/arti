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

Default authority is `shadow`; enabling `active` requires an explicit operational
switch. Global `legacy` starts without an interpreter/worker and overrides prior
active context flags for rollback. Importing this package does not start the bot.

See [results and all twenty batches](../docs/ARTI_IMPLEMENTATION_PROGRESS.md),
[operations](../docs/ARTI_COGNITION_RUNBOOK.md), and [machine contract](contract.json).
Scientific coefficients remain engineering hypotheses. Human ratings and a real
chat pilot window have not been completed.

```powershell
python -m tools.run_cognition_tests
python -m tools.evaluate_mechanisms
python -m tools.evaluate_full_cognition --split final_held_out --run-name frozen_final_v4
python -m tools.cognition_admin verify-copy
```

Offline suites mock providers and use new `arti_cognition_test_<uuid>` databases.
Verify-copy reads the configured DB locally but does not modify it or export raw
text. Live evaluation drivers accept only their fixed synthetic input corpora.
Never relabel a previously inspected hold-out as a fresh experiment.
