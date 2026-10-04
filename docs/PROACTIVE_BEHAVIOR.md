# Proactive behavior: current evidence, bounded initiative

## Scope

This package revises when Arti may act without a new direct request. It does not
turn silence, message volume, closeness, a generated answer, or delivery into
consent or successful completion. Explicit reminders retain a separate delivery
contract. The changes build on master `0a547baebd237f95102791085eaafc25f2fef183`.

## Before a group contribution

- Read a coherent, bounded public conversation snapshot with its revision and
  suppression epoch. Preserve the source question, reply ancestry, and relevant
  outcomes within the packet budget. Report missing/truncated context explicitly.
- Distinguish uncertainty, a proposed answer, explicit resolution, and refusal.
  Question/branch flags are fallible hints; the arbiter reads the actual messages.
  A bot answer does not prove that the issue was resolved. One participant's
  refusal cannot close unrelated questions through a shared lexical branch.
- Assess current relevance and concrete added value. If conversation changes
  during assessment or composition, discard the stale text and reassess/recompose
  from fresh evidence, with at most one semantic retry. Continued churn abstains.
  Never make an old composition look current by stamping the latest revision.
- Recheck frozen sources, epoch, policy, and permission before provider dispatch,
  after the provider returns, and at the final outbox boundary. Forgetting an
  unrelated-but-included public message invalidates the old frame, even when the
  judge did not cite that message. Policy opt-out and delivery share a chat fence.
- Include source-validated recent contributions and observed feedback as outcome
  evidence. Delivered and delivery-unknown are transport states, not proof of
  usefulness. No message body or evidence from another scope is imported.

The public frame contains at most 64 observations; the provider packet contains
at most 32 messages and stays below 15,000 UTF-8 bytes including hypotheses.
Recent contribution text keeps its complete internal dependency lineage even
when display IDs are shortened. Outcomes exceeding 128 supporting sources, or
256 total selected outcome sources, are omitted whole. The available bounded
history is not an all-time conversation model.

## Private follow-ups and durable goals

An unsolicited private inquiry requires an explicit current proactive preference,
a known personal timezone, a high-confidence open goal with a cue, and current
source-linked relevance. It waits at least one day after the goal's material
revision, does not revive goals older than 14 days, and leaves a 30-minute gap
from recent conversation. A changed topic, terminal outcome, sensitive current
situation, unfinished interpretation, or an unanswered bot question can defer it.
Terminal-outcome barriers inspect all newer structured observations; only the
recent conversational relevance window is bounded.
It offers help with the next step without asserting progress, success, or consent.

The last check uses the current artifact revision, exact delivery identity,
owner/private destination, source provenance, current epoch, history boundary,
preference, and response status. Confirmation marks only the matching goal's
send as delivered. It does not mark the goal fulfilled. Concurrent scheduler
passes and a restart cannot duplicate a delivered or ambiguous attempt.

An explicit reopening, reschedule, or change between reminder/open renews the
stable delivery identity. Repeated cues, confidence changes, and description-only
paraphrases keep the existing slot. Omitting a previously explicit deadline does
not remove the schedule or create a new send identity. A new description alone is insufficient
proof of a new goal; a genuinely distinct goal needs a distinct extracted key.
Unknown/legacy audience is not silently promoted to a private destination.

Explicit reminders skip unsolicited quiet-hour and initiative-budget checks;
their authorization, current source/goal, ownership, pending cancellation, and
outbox guards remain. Unchanged reminders retain separately verified exact
request provenance when later conversation merely paraphrases the goal. Clearing conversational history retains explicit stored
reminders; an already prepared old-epoch attempt is still rejected. Unsolicited
follow-ups cannot revive a pre-reset conversational source. Native organizer
ownership continues to prevent duplicate cognitive scheduling.

Arti's own unexecuted promises remain excluded from automatic user status
inquiries. There is no new executor that can actually fulfill an arbitrary
promise; pretending otherwise or asking the user to do that work would be wrong.

## Quiet hours and limits

Unknown timezone fails closed for unsolicited initiative. No host/UTC timezone
is substituted. Group administrators configure it with:

```
/proactivity tz Europe/Moscow
/proactivity hours 23 9
```

The example is a setting syntax, not an inferred user timezone. The group menu and
setting confirmation show the saved timezone and current policy blocker. Private
users can supply their real IANA timezone with `/timezone <IANA zone>`.
Quiet-hour checks use `zoneinfo` and therefore follow the configured region's DST.
Default quiet hours are 23:00–09:00 local time.

The existing group chat-wide daily/spacing/assessment/share limits remain. Shared
final-send ledgers add durable per-context and cross-context owner limits:

| Guard | Default |
|---|---:|
| Private unsolicited sends/context/rolling day | 2 |
| Unsolicited sends/owner across contexts/rolling day | 3 |
| Minimum unsolicited interval for the same owner | 1 hour |
| Private context interval | 1 hour |
| Group context interval/daily limit | Group policy |
| Global unsolicited burst | 3 attempts/minute |
| Simultaneous optional provider calls | 2 |
| Optional provider calls globally/rolling hour | 60 |
| Continuation assessments/context/rolling hour | 6 |
| Group assess and compose calls, each/context/hour | Policy assessment limit |
| Provider-slot SQL admission | 0.35 seconds |
| Group assessment / composition | 8 / 12 seconds |
| Speculative ingress continuation, total | 1 second |
| Direct-send coordinator wait | 1 second |
| Group membership/visibility lookup | 3 seconds |

Timeouts above are work budgets; owned provider-slot cleanup is separately
bounded at 0.5 seconds and can extend wall-clock cancellation latency.

Provider failures and abstentions spend provider quota, but not delivered-message
quota. An abstention no longer imposes the contextual 90-second sent-message
cooldown on a fresh opportunity. Optional judges make one transport request per
call; hidden internal retries are removed. Expiring durable provider leases
recover after restart. Ordinary requested replies do not enter these optional
provider/admission quotas.

Final-send charging occurs in the same transaction as the validated outbox
attempt. A rollback consumes no send slot; exact retries consume one slot;
delivery-unknown remains charged and is never blindly retried. Definitive
transport rejection is conservatively charged. Content-free rate metadata is
cleaned after two days during later activity; it is not outcome-learning data.
Migration 035 seeds recent eligible prior delivery metadata when available.

A direct reply can preempt a speculative group lease before transport begins.
It does not steal an already-started/ambiguous send or another direct lease.
If transport genuinely remains busy, bounded suppression is safer than duplicate
or concurrent irreversible sends; there is no guarantee that every unaddressed
continuation is recognized within the one-second budget.

## Verification

All test conversations, perceptions, provider results, and transport receipts are
synthetic. SQL checks use disposable PostgreSQL. Provider and Telegram calls are
mocked; production data and credentials are not used. The final aggregate and
source fingerprint are recorded in
[`evaluation/proactive_behavior_validation.json`](evaluation/proactive_behavior_validation.json).

Final frozen run: **1,137/1,137 passed**, no failures/errors/skips, 513.388 seconds.
This includes 137 new regressions and the unchanged 24 videotrans tests. Two
independent reviews found no unresolved blocking issue. The frozen 1,000-message
load (40 participants, five topics) stayed within 128 queued candidates and 12
recorded assessments; synthetic ingress p50/p95 was 12.29/18.27 ms. These local
measurements do not measure live provider latency, cost, or production quality.

Focused checks:

```
python -m unittest tests.cognition.test_proactive_context
ARTI_TEST_DB=1 python -m unittest tests.cognition.test_initiative_limits tests.cognition.test_proactive_private tests.cognition.test_proactive_groups
python -m tools.run_cognition_tests
```

## Limits and remaining architecture

- These checks prove deterministic contracts and races, not live LLM judgement,
  naturalness, calibrated social quality, or empirical psychological accuracy.
- Semantic private relevance depends on already recorded structured interpretation
  and a bounded recent window. It deliberately abstains when evidence is missing;
  it is not an all-history planning engine or arbitrary promise executor.
- A provider request or Telegram operation already in flight cannot be recalled
  when permission/source state changes. Dispatch/final-send checks and receipt
  fences prevent later stale reuse, not retroactive network cancellation.
- A pre-upgrade reminder already reanchored to a neutral source without saved
  request lineage conservatively abstains unless its current source itself
  supplies the exact explicit reminder authorization. No arbitrary old request
  is treated as permission for a new schedule.
- Rate and relevance thresholds are conservative engineering defaults. They are
  not learned preferences, user consent, or evidence that silence means success.
- The coordinator does not add a global fair queue across all chats; bounded
  provider concurrency and direct priority limit interference, but a busy or
  high-latency context can still defer optional opportunities.
- No new external integration, production rollout, merge, deployment, or
  videotrans/unrelated audio change is part of this package.
