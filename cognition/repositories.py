"""Persistence boundary: compare-and-swap, causal ledger and source suppression.

No provider calls occur in a transaction. Suppression commits before any rebuild
can expose projections; every derivative requires explicit source provenance.
"""
import hashlib
from datetime import datetime,timezone
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
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_cognitive_schema')::bigint)")
        await conn.execute('''CREATE TABLE IF NOT EXISTS cognitive_schema_migrations (
            name TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        for path in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
            sql = path.read_text(encoding='utf-8')
            digest = hashlib.sha256(sql.encode()).hexdigest()
            previous_digest = await conn.fetchval('SELECT sha256 FROM cognitive_schema_migrations WHERE name=$1',path.name)
            if previous_digest is not None:
                if previous_digest!=digest:
                    raise ValueError('An applied cognitive migration was edited; add a new migration')
                continue
            await conn.execute(sql)
            await conn.execute('INSERT INTO cognitive_schema_migrations(name,sha256) VALUES($1,$2)',path.name,digest)
        # Explicit compatible snapshot upgrade: old causes/latent mood remain,
        # new working fields begin unknown/empty. Duplicate causes remain applied.
        for previous in ('cognition-2026-09-30.2','cognition-2026-09-30.3'):
            await conn.execute('''INSERT INTO cognitive_effects(context_id,event_id,independent_group,model_version,applied_revision)
                SELECT context_id,event_id,independent_group,$2,applied_revision FROM cognitive_effects
                WHERE model_version=$1 ON CONFLICT DO NOTHING''',previous,MODEL_VERSION)
            await conn.execute("UPDATE cognitive_contexts SET model_version=$2,state=jsonb_set(state,'{model_version}',to_jsonb($2::text)) WHERE model_version=$1",previous,MODEL_VERSION)
            await conn.execute("UPDATE cognitive_artifacts SET model_version=$2,payload=jsonb_set(payload,'{state,model_version}',to_jsonb($2::text)) WHERE kind='personal_state' AND model_version=$1",previous,MODEL_VERSION)


class CognitiveRepository:
    def __init__(self, pool):
        self.pool = pool

    async def goal_registry(self,conn,cid,event,fallback):
        from cognition.memory_repository import MemoryRepository,key
        from cognition.affect import available_goals
        _,cache = await MemoryRepository(self.pool)._get(conn,cid,key('personal_state',event.evidence.owner_id))
        if cache:
            return available_goals(load_state(cache['state']),event.evidence.owner_id)
        # A failed encoding job must not make another participant's bounded
        # working goal stack authoritative for this owner.
        rows = await conn.fetch('''SELECT e.*,(SELECT r.perception FROM cognitive_reappraisals r JOIN cognitive_events s ON s.id=r.support_event_id
            WHERE r.context_id=e.context_id AND r.cause_event_id=e.id AND r.perception IS NOT NULL AND s.suppressed_at IS NULL
            ORDER BY r.created_at DESC,r.support_event_id DESC LIMIT 1) AS revised FROM cognitive_events e
            JOIN cognitive_effects f ON f.context_id=e.context_id AND f.event_id=e.id
            WHERE e.context_id=$1 AND e.owner_id IS NOT DISTINCT FROM $2 AND e.suppressed_at IS NULL AND f.model_version=$3
            ORDER BY e.observed_at,e.id''',cid,event.evidence.owner_id,MODEL_VERSION)
        state = initial_state(event.context,rows[0]['observed_at'] if rows else event.observed_at)
        for row in rows:
            state = replace(appraise(state,load_event(row['payload']),Perception.from_dict(object_value(row['revised'] or row['perception']))),applied_groups=frozenset())
        return available_goals(state,event.evidence.owner_id)

    async def observe(self, event: CognitiveEvent) -> tuple[int, int]:
        payload = dump(event)
        fingerprint = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        state = initial_state(event.context, event.observed_at)
        async with self.pool.acquire() as conn, conn.transaction():
            cid = await conn.fetchval("""
                INSERT INTO cognitive_contexts(persona_id, chat_id, mode, scene_id, topic_id, model_version, state, authority)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
                ON CONFLICT(persona_id,chat_id,mode,scene_id,topic_id) DO NOTHING RETURNING id
            """, *event.context.identity(), MODEL_VERSION, dump(state),
                'shadow' if event.context.mode=='rp' and event.context.scene_id=='legacy-unresolved' else 'active')
            if cid is None:
                cid = await conn.fetchval("""
                    SELECT id FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5
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
                     expected_revision: int, state: CognitiveState, *, worker_token=None, expected_epoch=None) -> bool:
        """Commit a pure transition once; False means the same cause was already applied."""
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE', context_id)
            if current['rebuilding']:
                raise SuppressedEvidence()
            if expected_epoch is not None and current['suppression_epoch']!=expected_epoch:
                raise SuppressedEvidence()
            if worker_token is not None:
                valid = await conn.fetchval('''SELECT 1 FROM cognitive_jobs WHERE context_id=$1 AND event_id=$2
                    AND lease_token=$3 AND status='running' AND lease_until>NOW()''',context_id,event_id,worker_token)
                if not valid or current['worker_token']!=worker_token:
                    raise StaleRevision()
            elif current['worker_lease_until'] is not None and current['worker_lease_until']>datetime.now(timezone.utc):
                raise StaleRevision()
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
            before = load_state(current['state'])
            goals = await self.goal_registry(conn,context_id,stored_event,before)
            expected = appraise(before, stored_event, perception,goals=goals)
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
                SELECT id,owner_id FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL
            """, context_id, sorted(sources))
            if {r['id'] for r in allowed} != sources:
                raise SuppressedEvidence()
            if any(r['owner_id']!=owner_id for r in allowed):
                raise ValueError('A derivative cannot promote private evidence to another owner or common scope')
            aid = await conn.fetchval("""
                INSERT INTO cognitive_artifacts(context_id,owner_id,kind,model_version,payload,projection_epoch)
                VALUES($1,$2,$3,$4,$5::jsonb,(SELECT suppression_epoch FROM cognitive_contexts WHERE id=$1)) RETURNING id
            """, context_id, owner_id, kind, MODEL_VERSION, dump(payload))
            await conn.executemany("""
                INSERT INTO cognitive_provenance(context_id,artifact_id,source_event_id) VALUES($1,$2,$3)
            """, [(context_id, aid, sid) for sid in sorted(sources)])
            return aid

    async def commit_late(self,cid,eid,perception,*,worker_token,expected_epoch):
        """Reinsert a recovered observation in chronology, never move the clock back."""
        async with self.pool.acquire() as conn,conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            source = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,eid)
            valid = await conn.fetchval("SELECT 1 FROM cognitive_jobs WHERE context_id=$1 AND event_id=$2 AND lease_token=$3 AND status='running' AND lease_until>NOW()",cid,eid,worker_token)
            if not source or current['suppression_epoch']!=expected_epoch:
                raise SuppressedEvidence()
            if not valid or current['worker_token']!=worker_token:
                raise StaleRevision()
            duplicate = await conn.fetchval('SELECT 1 FROM cognitive_effects WHERE context_id=$1 AND independent_group=$2 AND model_version=$3',cid,source['independent_group'],MODEL_VERSION)
            if duplicate:
                return False
            await conn.execute('UPDATE cognitive_events SET perception=$2::jsonb WHERE id=$1',eid,dump(perception))
            await conn.execute('INSERT INTO cognitive_effects VALUES($1,$2,$3,$4,$5)',cid,eid,source['independent_group'],MODEL_VERSION,current['revision']+1)
            from cognition.reappraisal import ReappraisalRepository
            await ReappraisalRepository(self.pool).rebuild_locked(conn,cid,current)
            return True

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
                WITH RECURSIVE seeds AS (
                    SELECT id FROM cognitive_events WHERE context_id=$1 AND owner_id=$3
                    AND independent_group IN (SELECT independent_group FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND owner_id=$3)
                    AND suppressed_at IS NULL
                ), affected AS (
                    SELECT id FROM seeds
                    UNION
                    SELECT e.id FROM cognitive_events e JOIN cognitive_event_dependencies d ON d.event_id=e.id
                    JOIN affected r ON r.id=d.source_event_id
                    WHERE e.context_id=$1 AND (e.owner_id=$3 OR e.origin='delivered_action') AND e.suppressed_at IS NULL
                ) SELECT id FROM affected WHERE id IN (SELECT id FROM seeds)
                    OR id IN (SELECT id FROM cognitive_events WHERE origin='delivered_action')
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
            dependent = await conn.fetch('''WITH RECURSIVE affected AS (
                SELECT id FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[])
                UNION
                SELECT e.id FROM cognitive_events e JOIN cognitive_event_dependencies d ON d.event_id=e.id
                JOIN affected a ON a.id=d.source_event_id WHERE d.context_id=$1 AND e.owner_id=$3
            ) SELECT e.id FROM cognitive_events e JOIN affected a ON a.id=e.id
                WHERE e.context_id=$1 AND e.origin='user' AND e.suppressed_at IS NULL''',context_id,ids,owner_id)
            dep_ids = [r['id'] for r in dependent]
            if dep_ids:
                await conn.execute('UPDATE cognitive_events SET perception=NULL WHERE context_id=$1 AND id=ANY($2::bigint[])',context_id,dep_ids)
                await conn.execute('DELETE FROM cognitive_effects WHERE context_id=$1 AND event_id=ANY($2::bigint[])',context_id,dep_ids)
                await conn.execute("UPDATE cognitive_jobs SET status='pending',attempts=0,available_at=NOW(),lease_token=NULL,lease_until=NULL WHERE context_id=$1 AND event_id=ANY($2::bigint[]) AND kind='interpret'",context_id,dep_ids)
                await conn.execute("UPDATE cognitive_artifacts SET payload=NULL,suppressed_at=NOW() WHERE context_id=$1 AND id IN (SELECT artifact_id FROM cognitive_provenance WHERE context_id=$1 AND source_event_id=ANY($2::bigint[]))",context_id,dep_ids)
            await conn.execute("UPDATE cognitive_jobs SET status='pending',lease_token=NULL,lease_until=NULL,last_error_code='stale_revision',available_at=NOW() WHERE context_id=$1 AND status='running'",context_id)
            await conn.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1,worker_token=NULL,worker_lease_until=NULL WHERE id=$1',context_id)
            await conn.execute("UPDATE cognitive_outbox SET status='cancelled',payload=NULL WHERE context_id=$1 AND event_id=ANY($2::bigint[])",context_id,ids)
            await conn.execute('UPDATE group_observations SET payload=NULL,suppressed_at=NOW() WHERE context_id=$1 AND event_id=ANY($2::bigint[])',context_id,ids)
            await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id=$1 AND source_ids && $2::bigint[] AND status IN ('pending','deferred','claimed')",context_id,ids)
            await conn.execute('DELETE FROM group_feedback WHERE context_id=$1 AND source_ids && $2::bigint[]',context_id,ids)
            await conn.execute('DELETE FROM cognitive_embeddings WHERE context_id=$1 AND artifact_id=ANY($2::bigint[])',context_id,[r['id'] for r in invalid])
            await conn.execute('DELETE FROM cognitive_retrievals WHERE context_id=$1 AND artifact_ids && $2::bigint[]',context_id,[r['id'] for r in invalid])
            await conn.execute('UPDATE cognitive_reappraisals SET perception=NULL WHERE context_id=$1 AND (cause_event_id=ANY($2::bigint[]) OR support_event_id=ANY($2::bigint[]))',context_id,ids)
            from cognition.reappraisal import ReappraisalRepository
            await ReappraisalRepository(self.pool).rebuild_locked(conn,context_id)
            return {'events': len(ids), 'artifacts': len(invalid)}
