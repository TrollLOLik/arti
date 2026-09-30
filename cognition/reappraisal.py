"""Replaceable causal interpretation slots; a clarification is not a second cause."""
from dataclasses import replace
from cognition.affect import appraise,advance,initial_state,replay_ledger
from cognition.serialization import dump,load_event,load_state,object_value
from cognition.types import Perception,DEFAULT_GOALS,Goal,MODEL_VERSION
from cognition.repositories import SuppressedEvidence,StaleRevision


class ReappraisalRepository:
    def __init__(self,pool):
        self.pool = pool

    async def revise(self,cid,cause_source,support_eid,perception,*,expected_epoch=None,worker_token=None):
        async with self.pool.acquire() as conn,conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            if expected_epoch is not None and current['suppression_epoch']!=expected_epoch:
                raise SuppressedEvidence()
            if worker_token is not None and current['worker_token']!=worker_token:
                raise StaleRevision()
            cause = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND source_id=$2 AND suppressed_at IS NULL',cid,cause_source)
            support = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,support_eid)
            if not cause or not support:
                raise SuppressedEvidence()
            if cause['owner_id']!=support['owner_id'] or cause['origin']!='user' or support['origin']!='user':
                raise ValueError('Reappraisal requires same-owner external evidence')
            event = load_event(cause['payload'])
            original = Perception.from_dict(object_value(cause['perception']))
            recorded_ids = {a.goal_id for a in original.appraisals}
            goals = DEFAULT_GOALS + tuple(Goal(g,'Recorded situational goal',.7) for g in recorded_ids-{g.id for g in DEFAULT_GOALS})
            perception.validate_for(event,goals)
            # Revisions must remain pure appraisals of the original cause. The
            # supporting explanation is independently recorded in the revision row.
            inserted = await conn.fetchval('''INSERT INTO cognitive_reappraisals(context_id,cause_event_id,support_event_id,perception,created_at)
                VALUES($1,$2,$3,$4::jsonb,$5) ON CONFLICT DO NOTHING RETURNING cause_event_id''',cid,cause['id'],support_eid,dump(perception),support['observed_at'])
            if inserted is None:
                return False
            await self.rebuild_locked(conn,cid,current)
            await self.rebuild_relationship_locked(conn,cid,cause['owner_id'])
            return True

    async def rebuild_relationship_locked(self,conn,cid,owner):
        from cognition.relationships import initial_relationship,relationship_transition
        from cognition.memory_repository import MemoryRepository,key
        rows = await conn.fetch('''SELECT e.*,(SELECT r.perception FROM cognitive_reappraisals r JOIN cognitive_events s ON s.id=r.support_event_id
            WHERE r.context_id=e.context_id AND r.cause_event_id=e.id AND r.perception IS NOT NULL AND s.suppressed_at IS NULL
            ORDER BY r.created_at DESC,r.support_event_id DESC LIMIT 1) AS revised FROM cognitive_events e
            WHERE e.context_id=$1 AND e.owner_id IS NOT DISTINCT FROM $2
            AND e.suppressed_at IS NULL AND e.perception IS NOT NULL
            AND EXISTS(SELECT 1 FROM cognitive_effects f WHERE f.context_id=e.context_id AND f.event_id=e.id AND f.model_version=$3)
            ORDER BY e.observed_at,e.id''',cid,owner,MODEL_VERSION)
        relationship = initial_relationship()
        state = None
        for row in rows:
            p = Perception.from_dict(object_value(row['revised'] or row['perception']))
            ev = load_event(row['payload'])
            state = state or initial_state(ev.context,ev.observed_at)
            state = replace(appraise(state,ev,p),applied_groups=frozenset())
            if p.situation:
                relationship = relationship_transition(relationship,ev,p.situation)
        if rows:
            repo = MemoryRepository(self.pool)
            sources = [r['id'] for r in rows]
            await repo._put(conn,cid,'relationship',key('relationship',owner),relationship,owner,sources)
            await repo._put(conn,cid,'personal_state',key('personal_state',owner),dict(state=object_value(dump(state))),owner,sources)

    async def rebuild_locked(self,conn,cid,current=None):
        current = current or await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        old = load_state(current['state'])
        rows = await conn.fetch('''SELECT e.*,(
            SELECT r.perception FROM cognitive_reappraisals r
            JOIN cognitive_events support ON support.id=r.support_event_id
            WHERE r.context_id=e.context_id AND r.cause_event_id=e.id AND r.perception IS NOT NULL
              AND support.suppressed_at IS NULL ORDER BY r.created_at DESC,r.support_event_id DESC LIMIT 1) AS revised
            FROM cognitive_events e JOIN cognitive_effects f ON f.context_id=e.context_id AND f.event_id=e.id
            WHERE e.context_id=$1 AND e.suppressed_at IS NULL AND f.model_version=$2 ORDER BY e.observed_at,e.id''',cid,MODEL_VERSION)
        entries = [(load_event(row['payload']),Perception.from_dict(object_value(row['revised'] or row['perception']))) for row in rows]
        state = replay_ledger(old.context,entries,old.last_at)
        state = advance(state,max(old.last_at,state.last_at))
        state = replace(state,revision=current['revision']+1,applied_groups=frozenset())
        await conn.execute('UPDATE cognitive_contexts SET revision=$2,state=$3::jsonb WHERE id=$1',cid,state.revision,dump(state))
        return state

    async def reinterpret_trace(self,cid,cause_source,support_eid,explanation,confidence,*,expected_epoch=None,worker_token=None):
        """Preserve the source-backed original and append a subjective version."""
        from cognition.memory_repository import MemoryRepository,key
        repo = MemoryRepository(self.pool)
        async with self.pool.acquire() as conn,conn.transaction():
            current = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            if expected_epoch is not None and current['suppression_epoch']!=expected_epoch:
                raise SuppressedEvidence()
            if worker_token is not None and current['worker_token']!=worker_token:
                raise StaleRevision()
            support = await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',cid,support_eid)
            row = await conn.fetchrow("SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND kind='trace' AND payload->>'source_id'=$2 AND suppressed_at IS NULL",cid,cause_source)
            if not row or not support or row['owner_id']!=support['owner_id']:
                raise SuppressedEvidence()
            p = object_value(row['payload'])
            marker = key('memory_version',row['id'],support_eid)
            previous,_ = await repo._get(conn,cid,marker)
            if previous:
                return False
            await repo._put(conn,cid,'memory_version',marker,p,row['owner_id'],[],[row['id']])
            p = {**p,'interpretation':explanation,'interpretation_confidence':confidence,'version':p['version']+1}
            await repo._put(conn,cid,'trace',row['artifact_key'],p,row['owner_id'],[support_eid],[row['id']])
            concerns = await conn.fetch("SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND kind='concern' AND payload->>'source_id'=$2 AND suppressed_at IS NULL",cid,cause_source)
            for concern in concerns:
                value = {**object_value(concern['payload']),'status':'reinterpreted','resolution_source':support['source_id']}
                await repo._put(conn,cid,'concern',concern['artifact_key'],value,row['owner_id'],[support_eid],[concern['id']])
            return True


def explained_perception(original,support):
    """A grounded clarification can revise attribution; it cannot erase actual loss."""
    if not support.situation or not support.situation.revisions:
        return original
    s = support.situation
    if s.kind!='clarification':
        return original
    revision = next((r for r in s.revisions if r['source_id'] in original.appraisals[0].evidence_ids),s.revisions[0]) if original.appraisals else s.revisions[0]
    confidence = revision['confidence']
    attribution = revision['attribution']
    if attribution=='intentional':
        values = tuple(replace(a,intentionality=max(a.intentionality,confidence)) for a in original.appraisals)
    elif attribution in ('accidental','resolved'):
        values = tuple(replace(a,intentionality=a.intentionality*(1-confidence),norm_violation=a.norm_violation*(1-confidence)) for a in original.appraisals)
    else:
        values = tuple(replace(a,confidence=min(a.confidence,.5),intentionality=min(a.intentionality,.35)) for a in original.appraisals)
    revised_s = original.situation
    if revised_s:
        revised_s = replace(revised_s,intention_evidence='explicit' if attribution=='intentional' else 'unobserved',
                            social_signal=revised_s.social_signal if attribution=='intentional' else 'contact',
                            outcome='resolved' if attribution=='resolved' else revised_s.outcome)
    return replace(original,appraisals=values,situation=revised_s)
