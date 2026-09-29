"""Persistence boundary: compare-and-swap, causal ledger and source suppression.

No provider calls occur in a transaction. Suppression commits before any rebuild
can expose projections; every derivative requires explicit source provenance.
"""
import hashlib
from dataclasses import replace
from pathlib import Path

from cognition.affect import advance, appraise, initial_state
from cognition.serialization import dump, load_event, load_state, object_value
from cognition.types import CognitiveEvent, CognitiveState, MODEL_VERSION, Perception


class StaleRevision(Exception):
    pass


class SuppressedEvidence(Exception):
    pass


async def ensure_schema(pool):
    sql = Path(__file__).with_name('migrations').joinpath('001_cognitive_kernel.sql').read_text(encoding='utf-8')
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_cognitive_schema')::bigint)")
        await conn.execute(sql)


class CognitiveRepository:
    def __init__(self, pool):
        self.pool = pool

    async def observe(self, event: CognitiveEvent) -> tuple[int, int]:
        payload = dump(event)
        fingerprint = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        state = initial_state(event.context, event.observed_at)
        async with self.pool.acquire() as conn, conn.transaction():
            cid = await conn.fetchval("""
                INSERT INTO cognitive_contexts(persona_id, chat_id, mode, scene_id, model_version, state)
                VALUES($1,$2,$3,$4,$5,$6::jsonb)
                ON CONFLICT(persona_id,chat_id,mode,scene_id) DO NOTHING RETURNING id
            """, *event.context.identity(), MODEL_VERSION, dump(state))
            if cid is None:
                cid = await conn.fetchval("""
                    SELECT id FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4
                """, *event.context.identity())
            # Serialize registration and suppression inside one context.
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE', cid)
            group = await conn.fetchrow('SELECT owner_id,suppressed_at FROM cognitive_events WHERE context_id=$1 AND independent_group=$2 LIMIT 1',
                                        cid, event.evidence.independent_group)
            if group and group['owner_id'] != event.evidence.owner_id:
                raise ValueError('An independence group cannot change owner')
            if group and group['suppressed_at'] is not None:
                raise SuppressedEvidence()
            existing = await conn.fetchrow("""
                SELECT * FROM cognitive_events WHERE context_id=$1 AND (event_key=$2 OR (source_id=$3 AND origin=$4))
            """, cid, event.event_id, event.evidence.source_id, event.evidence.origin.value)
            if existing:
                if existing['suppressed_at'] is not None:
                    raise SuppressedEvidence()
                if existing['fingerprint'] != fingerprint:
                    raise ValueError('An event id was reused with a different payload')
                return cid, existing['id']
            eid = await conn.fetchval("""
                INSERT INTO cognitive_events(context_id,event_key,source_id,independent_group,origin,owner_id,
                                             occurred_at,observed_at,payload,fingerprint)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10) RETURNING id
            """, cid, event.event_id, event.evidence.source_id, event.evidence.independent_group,
                event.evidence.origin.value, event.evidence.owner_id, event.occurred_at, event.observed_at, payload, fingerprint)
            return cid, eid

    async def state(self, context_id: int) -> CognitiveState:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT state FROM cognitive_contexts WHERE id=$1', context_id)
        if not row:
            raise KeyError('Unknown context')
        return load_state(row['state'])

    async def applied(self, context_id: int, group: str) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT 1 FROM cognitive_effects WHERE context_id=$1 AND independent_group=$2 AND model_version=$3',
                                       context_id, group, MODEL_VERSION) is not None

    async def commit(self, context_id: int, event_id: int, perception: Perception,
                     expected_revision: int, state: CognitiveState) -> bool:
        """Commit a pure transition once; False means the same cause was already applied."""
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE', context_id)
            source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2', context_id, event_id)
            if not source or source['suppressed_at'] is not None:
                raise SuppressedEvidence()
            stored_event = load_event(source['payload'])
            if state.context != stored_event.context or state.model_version != current['model_version']:
                raise ValueError('Transition context/version mismatch')
            duplicate = await conn.fetchval("""
                SELECT 1 FROM cognitive_effects WHERE context_id=$1 AND independent_group=$2 AND model_version=$3
            """, context_id, source['independent_group'], MODEL_VERSION)
            if duplicate:
                return False
            if current['revision'] != expected_revision or state.revision != expected_revision + 1:
                raise StaleRevision()
            # Validate correspondence, rather than trusting a caller's numerical payload.
            expected = appraise(load_state(current['state']), stored_event, perception)
            if state != expected:
                raise ValueError('Transition differs from the pure model')
            # The SQL ledger provides durable idempotence; do not copy its unbounded
            # history into every state snapshot.
            snapshot = replace(state, applied_groups=frozenset())
            await conn.execute('UPDATE cognitive_contexts SET revision=$2,state=$3::jsonb WHERE id=$1',
                               context_id, state.revision, dump(snapshot))
            await conn.execute('UPDATE cognitive_events SET perception=$2::jsonb WHERE id=$1', event_id, dump(perception))
            await conn.execute("""
                INSERT INTO cognitive_effects(context_id,event_id,independent_group,model_version,applied_revision)
                VALUES($1,$2,$3,$4,$5)
            """, context_id, event_id, source['independent_group'], MODEL_VERSION, state.revision)
            return True

    async def artifact(self, context_id: int, kind: str, payload: dict, owner_id: int | None,
                       source_events: list[int], parents: list[int] | None = None) -> int:
        if not kind or len(kind) > 64:
            raise ValueError('Artifact kind is required')
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE', context_id)
            sources = set(source_events)
            for parent in parents or []:
                valid = await conn.fetchval("""
                    SELECT 1 FROM cognitive_artifacts WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL
                """, context_id, parent)
                if not valid:
                    raise SuppressedEvidence()
                inherited = await conn.fetch("""
                    SELECT source_event_id FROM cognitive_provenance WHERE context_id=$1 AND artifact_id=$2
                """, context_id, parent)
                sources.update(r['source_event_id'] for r in inherited)
            if not sources:
                raise ValueError('Derivatives require raw sources')
            allowed = await conn.fetch("""
                SELECT id FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL
            """, context_id, sorted(sources))
            if {r['id'] for r in allowed} != sources:
                raise SuppressedEvidence()
            aid = await conn.fetchval("""
                INSERT INTO cognitive_artifacts(context_id,owner_id,kind,model_version,payload)
                VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id
            """, context_id, owner_id, kind, MODEL_VERSION, dump(payload))
            await conn.executemany("""
                INSERT INTO cognitive_provenance(context_id,artifact_id,source_event_id) VALUES($1,$2,$3)
            """, [(context_id, aid, sid) for sid in sorted(sources)])
            return aid

    async def forget(self, context_id: int, source_id: str, owner_id: int) -> dict:
        """Erase one owned raw source and projections, rebuild affect from allowed evidence.

        This operates only on the new schema. Legacy /forget migration is a
        separate gate and must not claim that this method cleans legacy storage.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE', context_id)
            if not current:
                return {'events': 0, 'artifacts': 0}
            rows = await conn.fetch("""
                SELECT id FROM cognitive_events WHERE context_id=$1 AND owner_id=$3
                AND independent_group IN (SELECT independent_group FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND owner_id=$3)
                AND suppressed_at IS NULL
            """, context_id, source_id, owner_id)
            ids = [r['id'] for r in rows]
            if not ids:
                return {'events': 0, 'artifacts': 0}
            invalid = await conn.fetch("""
                UPDATE cognitive_artifacts SET suppressed_at=NOW(), payload=NULL
                WHERE context_id=$1 AND id IN (
                    SELECT artifact_id FROM cognitive_provenance WHERE context_id=$1 AND source_event_id=ANY($2::bigint[])
                ) AND suppressed_at IS NULL RETURNING id
            """, context_id, ids)
            await conn.execute("""
                UPDATE cognitive_events SET suppressed_at=NOW(), payload=NULL, perception=NULL, fingerprint=NULL
                WHERE context_id=$1 AND id=ANY($2::bigint[])
            """, context_id, ids)
            await conn.execute("""
                UPDATE cognitive_contexts SET worker_lease_until=NULL,worker_token=NULL
                WHERE id=$1 AND worker_token IN (
                    SELECT lease_token FROM cognitive_jobs WHERE context_id=$1 AND event_id=ANY($2::bigint[])
                )
            """, context_id, ids)
            await conn.execute("""
                UPDATE cognitive_jobs SET status='cancelled', lease_token=NULL, lease_until=NULL, last_error_code=NULL
                WHERE context_id=$1 AND event_id=ANY($2::bigint[])
            """, context_id, ids)
            await conn.execute('DELETE FROM cognitive_effects WHERE context_id=$1 AND event_id=ANY($2::bigint[])', context_id, ids)
            old = load_state(current['state'])
            state = initial_state(old.context, old.last_at)
            allowed = await conn.fetch("""
                SELECT e.* FROM cognitive_events e JOIN cognitive_effects f ON f.context_id=e.context_id AND f.event_id=e.id
                WHERE e.context_id=$1 AND e.suppressed_at IS NULL ORDER BY e.observed_at,e.id
            """, context_id)
            if allowed:
                state = initial_state(old.context, allowed[0]['observed_at'])
                for row in allowed:
                    ev = load_event(row['payload'])
                    p = Perception.from_dict(object_value(row['perception']))
                    state = appraise(state, ev, p)
                state = advance(state, old.last_at)
            state = replace(state, revision=current['revision'] + 1, applied_groups=frozenset())
            await conn.execute('UPDATE cognitive_contexts SET revision=$2,state=$3::jsonb WHERE id=$1', context_id, state.revision, dump(state))
            return {'events': len(ids), 'artifacts': len(invalid)}
