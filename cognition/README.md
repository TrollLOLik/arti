# Cognitive kernel

This package is an isolated implementation foundation for the architecture in
`docs/ARTI_COGNITIVE_ARCHITECTURE.md`. It is not the complete human-memory model.
Telegram currently uses the stabilized legacy path. Importing this package does
not initialize a provider, migrate the bot database, send messages, or enable a
new authoritative emotional state.

The current kernel provides explicit context/source/version types, pure appraisal
and analytic affect dynamics, an expression projection, CAS persistence, a causal
ledger, flattened source provenance, suppression with affect rebuild, and durable
job leases. The interpreter proposes dimensions; Python validates them and
computes state. Missing probability mass becomes an explicit unknown branch with
zero asserted consequence. Proposed probabilities are never renormalized upward.

The final development run passed 20/20 synthetic criteria; the first held-out run
passed 6/8. The two failures are preserved. Appraisal attribution, preference
handling, unresolved concerns, dynamic goals, relationship learning, selective
episodic memory, replay and transport integration still require later batches.
The 64-episode quota is a guard; resolved-episode archival is not implemented.
These constants are engineering hypotheses, not fitted human parameters.

Offline tests (provider calls are mocked or read from saved synthetic fixtures):

```powershell
python -m unittest discover -s tests -t . -v
```

PostgreSQL tests require permission to create databases. The driver generates a
new `arti_cognition_test_<uuid>` database, bootstraps that database, and removes
only the exact database it created. It never bootstraps the configured bot DB.

```powershell
$env:ARTI_TEST_DB='1'
$env:PYTHONIOENCODING='utf-8'
python -m unittest discover -s tests -t . -v
```

Reproduce the frozen numerical results without OpenRouter:

```powershell
python -m tools.evaluate_cognition --split development --run-name uncertainty_v2
python -m tools.evaluate_cognition --split held_out --run-name uncertainty_v2
```

The held-out command exits with code 1 because two behavioral criteria failed;
schema validity and deterministic replay are separate checks. Re-running the
provider against that same corpus is no longer a fresh held-out experiment.

Live calls use only the synthetic corpus, `.env` keys in memory, the user's model
`stealth/space-bunny-alpha`, a bounded retry policy, and explicit reasoning/output
budgets. Reports record all successful and failed attempts available to the
evaluator. A provider cost of zero is a reported value, not a future pricing
guarantee. Initial reports have their own historical limitations documented in
the progress record.
