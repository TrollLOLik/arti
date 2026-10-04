"""Conservative, source-linked private followups, checked again at transport.

Time passing makes a goal eligible for consideration, not newly relevant,
successful, or approved. This module performs no inference/provider call and
never turns Arti's own unexecuted promise into a question for the user.
"""
from datetime import datetime

from cognition.serialization import object_value,load_event,dump


def _at(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError,ValueError):
        return None


def materially_revised(old,item):
    """Only grounded operational changes renew permission for a single inquiry.

    Description/cue similarity is not reliable evidence of a changed goal. A
    semantic rewrite under the same key therefore conservatively keeps its slot;
    an explicitly different goal must have a different extracted stable key.
    """
    active = ('open','reminder')
    if item['status'] not in active:
        return False
    if old.get('status') in ('fulfilled','cancelled'):
        return True
    if old.get('status') in active and old['status']!=item['status']:
        return True
    deadline = _at(item.get('deadline'))
    return deadline is not None and _at(old.get('deadline'))!=deadline


def _situation(row):
    perception = object_value(row.get('perception')) or {}
    return perception.get('situation') or {}


async def reminder_authorization(conn,row,payload,source,context):
    """A neutral update cannot create, reschedule, or broaden a reminder grant."""
    source_id = payload.get('reminder_request_source_id') or payload.get('source_id')
    authorization = source if source_id==source['source_id'] else await conn.fetchrow('''
        SELECT * FROM cognitive_events WHERE context_id=$1 AND source_id=$2
        AND owner_id=$3 AND suppressed_at IS NULL''',row['context_id'],source_id,row['owner_id'])
    if not authorization or not authorization['payload'] or authorization['origin']!='user':
        return None
    try:
        event = load_event(authorization['payload'])
    except (ValueError,KeyError,TypeError):
        return None
    owner = row['owner_id']
    if (event.evidence.owner_id!=owner or event.actor_id!=owner
            or event.context.chat_id!=owner or event.context.topic_id!=context['topic_id']
            or event.context.mode!=context['mode'] or event.context.scene_id!=context['scene_id']
            or event.audience.kind!='private' or event.audience.chat_id!=owner
            or event.audience.topic_id!=context['topic_id']):
        return None
    if not await conn.fetchval('''SELECT 1 FROM cognitive_provenance
        WHERE context_id=$1 AND artifact_id=$2 AND source_event_id=$3''',
        row['context_id'],row['id'],authorization['id']):
        return None
    situation = _situation(authorization)
    if situation.get('kind')!='request' or situation.get('modality') in ('quoted','hypothetical'):
        return None
    deadline = _at(payload.get('deadline'))
    if not deadline or not any(item.get('key')==payload.get('key') and item.get('actor',owner)==owner
        and item.get('status')=='reminder' and _at(item.get('deadline'))==deadline
        for item in situation.get('intentions',[])):
        return None
    return authorization['id']


def current_relevance(payload,source,recent,now):
    """No contact is a neutral absence of evidence; unrelated contact defers.

    Missing interpretation is deliberately not replaced with word spotting.
    Latest explicit goal state wins; broad topic continuity alone cannot revive
    a resolved outcome or an unanswered question from Arti.
    """
    created = _at(payload.get('created_at'))
    if (not created or not 86400 <= (now-created).total_seconds() <= 14*86400
            or not payload.get('cue') or float(payload.get('confidence',0))<.7):
        return False
    deadline = _at(payload.get('deadline'))
    if deadline and deadline>now:
        return False
    if not recent:
        return False
    latest = recent[0]
    if (now-latest['observed_at']).total_seconds()<1800:
        return False
    latest_user = next((r for r in recent if r['origin']=='user'),None)
    if latest_user is None or not latest_user.get('perception'):
        return False
    for row in recent:
        if row['id']==latest_user['id']:
            break
        if row['origin']=='delivered_action':
            # Later bot narration does not answer an earlier bot question.
            text = (object_value(row['payload']) or {}).get('text','')
            if '?' in text or '？' in text:
                return False
    situation = _situation(latest_user)
    if (not situation or situation.get('modality') in ('quoted','hypothetical')
            or situation.get('kind') in ('loss','threat','conflict')
            or situation.get('outcome') in ('confirmed','resolved')):
        return False
    matching = [i for i in situation.get('intentions',[]) if i.get('key')==payload.get('key')
                and i.get('actor',latest_user['owner_id'])==payload.get('actor_id')]
    if matching:
        return matching[-1].get('status')=='open'
    if latest_user['id']==source['id']:
        return True
    topic = payload.get('topic') or _situation(source).get('topic')
    # Topic labels are source-backed semantic projections, not lexical guesses.
    return bool(topic and topic!='unspecified' and situation.get('topic')==topic)


async def evaluate(runtime,conn,row,*,expected_revision=None,expected_key=None):
    """Return current source/relevance, using the caller's transaction if any."""
    cid,owner = row['context_id'],row['owner_id']
    if row.get('suppressed_at') is not None or not row.get('payload') or row.get('kind')!='intention':
        return None
    context = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',cid)
    if (not context or context['chat_id']<=0 or context['chat_id']!=owner or context['topic_id']>=0
            or context['authority']!='active' or context['rebuilding']
            or context['suppression_epoch']!=row['projection_epoch']):
        return None
    if context['mode']=='rp':
        from cognition.scope import CURRENT_SCOPE,TransportScope
        from config import rp_mode_state
        scene = await conn.fetchval('SELECT scene_id FROM cognitive_scenes WHERE chat_id=$1 AND topic_id=$2',
                                    context['chat_id'],context['topic_id'])
        token = CURRENT_SCOPE.set(TransportScope(context['chat_id'],context['topic_id'],'private'))
        try:
            if scene!=context['scene_id'] or not rp_mode_state.get(context['chat_id']):
                return None
        finally:
            CURRENT_SCOPE.reset(token)
    payload = object_value(row['payload'])
    if (payload.get('status') not in ('open','reminder') or payload.get('delivered')
            or payload.get('actor_id')=='arti' or not payload.get('delivery_key')
            or (expected_revision is not None and row['revision']!=expected_revision)
            or (expected_key is not None and payload['delivery_key']!=expected_key)):
        return None
    source = await conn.fetchrow('''SELECT * FROM cognitive_events WHERE context_id=$1
        AND source_id=$2 AND owner_id=$3 AND suppressed_at IS NULL''',cid,payload.get('source_id'),owner)
    if (not source or not source['payload'] or source['origin']!='user'
            or (payload['status']!='reminder' and source['id']<=context['history_after_event_id'])):
        return None
    try:
        event = load_event(source['payload'])
    except (ValueError,KeyError,TypeError):
        return None
    if (event.evidence.owner_id!=owner or event.actor_id!=owner or payload.get('actor_id')!=owner
            or event.context.chat_id!=context['chat_id'] or event.context.topic_id!=context['topic_id']
            or event.context.mode!=context['mode'] or event.context.scene_id!=context['scene_id']
            or event.audience.kind!='private' or event.audience.chat_id!=context['chat_id']
            or event.audience.topic_id!=context['topic_id']):
        return None
    supported = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM cognitive_provenance
        WHERE context_id=$1 AND artifact_id=$2 AND source_event_id=$3)
        AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id
            WHERE p.context_id=$1 AND p.artifact_id=$2 AND (e.suppressed_at IS NOT NULL OR e.owner_id IS DISTINCT FROM $4))''',
        cid,row['id'],source['id'],owner)
    if not supported:
        return None
    if await conn.fetchval('''SELECT 1 FROM cognitive_outbox WHERE context_id=$1
        AND starts_with(delivery_key,$2) AND status IN ('delivered','sending','delivery_unknown')''',
        cid,payload['delivery_key']+':'):
        return None
    from organizer.ownership import owns_intention
    if await owns_intention(conn,cid,row['id']):
        return None
    situation = _situation(source)
    if not situation or situation.get('modality') in ('quoted','hypothetical'):
        return None
    # This applies to explicit reminders too: a cancellation/reschedule may
    # already have arrived while its interpretation is still pending.
    if await conn.fetchval('''SELECT 1 FROM cognitive_events e WHERE e.context_id=$1 AND e.owner_id=$2
        AND e.origin='user' AND e.suppressed_at IS NULL AND e.id>$3 AND (
            e.perception IS NULL OR EXISTS(SELECT 1 FROM cognitive_jobs j WHERE j.event_id=e.id
                AND j.kind IN ('interpret','encode') AND j.status IN ('pending','running')))''',cid,owner,source['id']):
        return None
    now = runtime.clock()
    if payload['status']=='reminder':
        deadline = _at(payload.get('deadline'))
        if not deadline or deadline>now:
            return None
        authorization_id = await reminder_authorization(conn,row,payload,source,context)
        if authorization_id is None:
            return None
        return dict(payload=payload,source=source,event=event,
                    supporting_event_ids=tuple(sorted({source['id'],authorization_id})))
    if not await conn.fetchval('SELECT enabled FROM response_status WHERE chat_id=$1 FOR SHARE',context['chat_id']):
        return None
    # Read the current explicit preference inside the send fence, never reuse
    # the discovery snapshot or infer permission from silence/old engagement.
    relationship = await conn.fetchval('''SELECT payload FROM cognitive_artifacts
        WHERE context_id=$1 AND owner_id=$2 AND kind='relationship' AND suppressed_at IS NULL
        AND projection_epoch=$3''',cid,owner,context['suppression_epoch'])
    if not relationship or (object_value(relationship).get('preferences') or {}).get('proactive') is not True:
        return None
    from cognition.initiative_policy import private_reason
    if await private_reason(conn,owner,now):
        return None
    recent = await conn.fetch('''SELECT * FROM cognitive_events WHERE context_id=$1 AND owner_id=$2
        AND origin IN ('user','delivered_action') AND suppressed_at IS NULL AND id>$3
        ORDER BY observed_at DESC,id DESC LIMIT 32''',cid,owner,context['history_after_event_id'])
    if not current_relevance(payload,source,recent,now):
        return None
    topic = payload.get('topic') or situation.get('topic')
    # A neutral continuation cannot erase earlier completion/decline evidence.
    # Search all newer semantic observations, not just the recent window. A
    # genuine new source-backed goal update reanchors source_id naturally.
    if await conn.fetchval('''SELECT 1 FROM cognitive_events e
        WHERE e.context_id=$1 AND e.owner_id=$2 AND e.origin='user'
        AND e.suppressed_at IS NULL AND e.id>$3
        AND coalesce(e.perception->'situation'->>'modality','') NOT IN ('quoted','hypothetical')
        AND ((coalesce($4::text,'') NOT IN ('','unspecified')
            AND e.perception->'situation'->>'topic'=$4
            AND e.perception->'situation'->>'outcome' IN ('confirmed','resolved'))
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(e.perception->'situation'->'intentions','[]'::jsonb)) i
            WHERE i->>'key'=$5 AND i->>'status' IN ('fulfilled','cancelled')
            AND coalesce(i->>'actor',$2::bigint::text)=$2::bigint::text))''',cid,owner,source['id'],topic,payload.get('key')):
        return None
    return dict(payload=payload,source=source,event=event,
                supporting_event_ids=tuple(sorted({source['id'],*(r['id'] for r in recent[:2])})))


async def delivery_guard(turn,conn):
    """Final check under the same context lock as outbox insertion."""
    row = await conn.fetchrow('SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND id=$2',
                              turn.context_id,turn.private_intention_id)
    if not row:
        return False
    current = await evaluate(turn.runtime,conn,row,expected_revision=turn.private_intention_revision,
                             expected_key=turn.private_delivery_key)
    if not current or current['source']['id']!=turn.event_id:
        return False
    turn.supporting_event_ids = current['supporting_event_ids']
    return True


async def confirm_delivery(turn,conn):
    """Only the exact confirmed send revision gets marked; pending is not done."""
    row = await conn.fetchrow('SELECT * FROM cognitive_artifacts WHERE context_id=$1 AND id=$2',
                              turn.context_id,turn.private_intention_id)
    if not row or not row['payload'] or row['suppressed_at'] is not None or row['projection_epoch']!=turn.epoch:
        return
    payload = object_value(row['payload'])
    if payload.get('delivery_key')!=turn.private_delivery_key:
        return
    payload['delivered'] = True
    payload['delivered_at'] = turn.runtime.clock().isoformat()
    await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb,revision=revision+1 WHERE id=$1',row['id'],dump(payload))


def followup_text(payload):
    """A bounded question, without pretending an outcome or permission exists."""
    return 'Ты планировал(а): '+payload['description']+'. Это ещё актуально, нужна помощь со следующим шагом?'
