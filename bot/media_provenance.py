"""Provider-free, owner-scoped source fences for accepted disk media work."""
import hashlib
import json
from cognition.repositories import SuppressedEvidence
from cognition.serialization import load_event
from cognition.types import CognitiveEvent,EvidenceRef,Origin,AudienceScope,ExpressionPlan


def reference_source(message,scope):
    """Metadata from the actual attachment/replied message; never the temp path."""
    if message is None or scope is None: return None
    chat=getattr(message,'chat',None)
    chat_id=getattr(message,'chat_id',None) or getattr(chat,'id',None)
    mid=getattr(message,'message_id',None)
    if chat_id!=scope.chat_id or type(mid) is not int or mid<=0: return None
    sender_chat=getattr(message,'sender_chat',None)
    user=getattr(message,'from_user',None)
    if sender_chat:
        owner=None; kind='chat'
    elif user is not None and not getattr(user,'is_bot',False):
        owner=getattr(user,'id',None); kind='user'
    else: return None  # No fabricated uploader for a bot/library item.
    topic=getattr(message,'message_thread_id',None)
    if topic is None: topic=-1 if scope.chat_type=='private' or getattr(chat,'is_forum',False) else 0
    return dict(chat_id=chat_id,topic_id=topic,message_id=mid,user_id=owner,sender_kind=kind)


async def _observe(runtime,context,identity,owner,text,kind,origin,audience):
    """Use the ordinary immutable source repository, but create no model job."""
    async def existing():
        async with runtime.pool.acquire() as conn:
            return await conn.fetchrow('''SELECT e.* FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
                WHERE c.persona_id=$1 AND c.chat_id=$2 AND c.mode=$3 AND c.scene_id=$4 AND c.topic_id=$5 AND e.event_key=$6''',*context.identity(),identity)
    row=await existing()
    if row is None:
        at=runtime.clock()
        event=CognitiveEvent(identity,context,EvidenceRef(identity,identity,origin,owner),at,at,text,
            owner if origin==Origin.USER else None,event_kind=kind,audience=audience,addressed_to_arti=False)
        try: cid,eid=await runtime.repo.observe(event)
        except ValueError:
            # Concurrent identical acceptance can have a different observation
            # timestamp. Reuse only the same authorized immutable identity.
            row=await existing()
            if row is None: raise
        else: return cid,eid,event
    if row['suppressed_at'] is not None or row['payload'] is None or row['owner_id']!=owner:
        raise SuppressedEvidence()
    event=load_event(row['payload'])
    if event.context!=context or event.evidence.origin!=origin: raise SuppressedEvidence()
    if kind=='media_request' and (event.event_kind!=kind or event.text!=text): raise SuppressedEvidence()
    return row['context_id'],row['id'],event


def identity_for(request,context):
    fields={k:request.get(k) for k in ('type','chat_id','user_id','message_id','synthesis_text','url','cleaned','with_subs','audio_only','source_kind','saved_voice_id','saved_voice_version')}
    fields['context']=context.identity()
    fields['reference_sha256']=(request.get('reference_media') or request.get('input_media') or {}).get('sha256')
    return 'media-request:'+hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def capture(request,scope,turn=None):
    from cognition.runtime import get_runtime,PreparedTurn
    from config import rp_mode_state
    result=dict(request); reference=result.pop('reference_source',None)
    voice_id=result.pop('saved_voice_id',None); voice_version=result.pop('saved_voice_version',None)
    if (scope is None or scope.sender_kind!='user' or type(scope.user_id) is not int or scope.user_id<=0
            or scope.chat_id!=result.get('chat_id') or scope.chat_type not in ('private','group','supergroup')
            or scope.group and scope.topic_id<0):
        raise ValueError('media_request_scope_required')
    if result.get('user_id') not in (None,scope.user_id): raise ValueError('media_request_owner_mismatch')
    if result.get('type') not in ('dubbing','vclone') or type(result.get('message_id')) is not int or result['message_id']<=0:
        raise ValueError('media_request_identity_required')
    result['user_id']=scope.user_id; result['_telegram_scope']=scope
    runtime=get_runtime()
    if runtime is None:
        if turn is not None: raise SuppressedEvidence()
        if voice_id is not None or result.get('source_kind')=='saved_voice': raise ValueError('saved_voice_runtime_required')
        result['_cognitive_turn']=None
        return result
    mode='rp' if rp_mode_state.get(scope.chat_id) else 'default'
    context=await runtime.context(scope.chat_id,mode,scope.topic_id)
    cid=await runtime.ensure_context(context)
    ids=set(result.get('_cognitive_source_ids',()))
    if turn is not None:
        if turn.event.context!=context or turn.event.evidence.owner_id!=scope.user_id:
            raise SuppressedEvidence()
        async with runtime.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT suppression_epoch,rebuilding,authority FROM cognitive_contexts WHERE id=$1',turn.context_id)
            if not row or row['rebuilding'] or row['suppression_epoch']!=turn.epoch or row['authority']!=turn.authority: raise SuppressedEvidence()
        ids.add(turn.event_id); ids.update(getattr(turn,'supporting_event_ids',()))
    audience=AudienceScope('topic' if scope.topic_id>0 else 'group' if scope.group else 'private',scope.chat_id,scope.topic_id)
    if voice_id is not None or result.get('source_kind')=='saved_voice':
        from bot.saved_voice_sources import bind
        ids.add(await bind(runtime,context,cid,scope.user_id,voice_id,voice_version,audience))
        result.update(saved_voice_id=voice_id,saved_voice_version=voice_version)
    if reference is not None:
        if (not isinstance(reference,dict) or set(reference)!={'chat_id','topic_id','message_id','user_id','sender_kind'}
                or reference['chat_id']!=scope.chat_id or reference['topic_id']!=scope.topic_id
                or type(reference['message_id']) is not int or reference['message_id']<=0
                or reference['sender_kind'] not in ('user','chat')): raise ValueError('media_reference_scope_mismatch')
        owner=reference['user_id']
        if reference['sender_kind']=='user' and (type(owner) is not int or owner<=0): raise ValueError('media_reference_owner_required')
        if reference['sender_kind']=='chat' and owner is not None: raise ValueError('media_reference_owner_mismatch')
        if scope.chat_type=='private' and owner!=scope.user_id: raise ValueError('private_media_reference_owner_mismatch')
        origin=Origin.USER if owner is not None else Origin.SYSTEM
        _,eid,_=await _observe(runtime,context,f"telegram:{scope.chat_id}:{reference['message_id']}:{origin.value}",
            owner,'Медиа-источник для принятого запроса','media_source',origin,audience)
        ids.add(eid)
    identity=identity_for(result,context)
    text='Принят запрос клонирования голоса' if result['type']=='vclone' else 'Принят запрос озвучки медиа'
    cid,eid,event=await _observe(runtime,context,identity,scope.user_id,text,'media_request',Origin.USER,audience)
    ids.add(eid)
    async with runtime.pool.acquire() as conn,conn.transaction():
        row=await conn.fetchrow('SELECT suppression_epoch,rebuilding,authority,history_after_event_id FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        await runtime._validate_current_scene(conn,cid)
        if not row or row['rebuilding'] or row['authority']!='active' or row['history_after_event_id']>=eid: raise SuppressedEvidence()
        ancestors=await conn.fetchval('''WITH RECURSIVE required(id) AS (
            SELECT unnest($2::bigint[]) UNION SELECT d.source_event_id FROM cognitive_event_dependencies d
                JOIN required r ON d.event_id=r.id WHERE d.context_id=$1)
            SELECT array_agg(id) FROM required''',cid,sorted(ids)) or []
        ids.update(ancestors)
        count=await conn.fetchval('SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL',cid,sorted(ids))
        if count!=len(ids): raise SuppressedEvidence()
        if ids-{eid}:
            await conn.executemany('INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',[(cid,eid,source) for source in sorted(ids-{eid})])
    neutral=ExpressionPlan('acknowledge','calm and attentive',.5,.8,0.,0.,None,'neutral',(),False)
    prepared=PreparedTurn(runtime,cid,eid,event,neutral,'',row['suppression_epoch'],row['authority'])
    prepared.supporting_event_ids=sorted(ids)
    result.update(_cognitive_turn=prepared,_cognitive_context=context,_cognitive_source_ids=sorted(ids),_request_mode=mode)
    return result


async def refresh_neutral_media(conn,turn,context=None):
    """Rebase ONLY dependency-explicit, memory-free accepted media after erasure.

    Shared group epochs change when a different owner forgets something. That
    must not revoke an independent media request, but history/scene reset and
    erased supporting sources always block it. Caller holds the context lock
    on transport; this helper never relaxes ordinary conversational turn fences.
    """
    if (turn.event.event_kind!='media_request' or not turn.event.evidence.source_id.startswith('media-request:')
            or turn.memory!='' or turn.expression.cause_ids or turn.expression.behaviors
            or turn.expression.mixed_affect or turn.expression.disclosure): return False
    context=context or await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',turn.context_id)
    if (not context or context['rebuilding'] or context['authority']!=turn.authority or turn.authority!='active'
            or context['history_after_event_id']>=turn.event_id): return False
    if context['mode']=='rp':
        current=await conn.fetchval('SELECT scene_id FROM cognitive_scenes WHERE chat_id=$1 AND topic_id=$2',context['chat_id'],context['topic_id'])
        if current!=context['scene_id']: return False
    source=await conn.fetchrow('SELECT payload,owner_id FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL',turn.context_id,turn.event_id)
    if not source or source['payload'] is None or source['owner_id']!=turn.event.evidence.owner_id: return False
    original=load_event(source['payload'])
    if (original.event_kind!='media_request' or original.evidence!=turn.event.evidence or original.context!=turn.event.context
            or original.addressed_to_arti or original.evidence.origin!=Origin.USER
            or original.text not in ('Принят запрос клонирования голоса','Принят запрос озвучки медиа')): return False
    # Required provenance is durable, not whatever a recovered codec happens
    # to claim. Missing supporting IDs cannot bypass a forgotten ancestor.
    required=await conn.fetchval('''WITH RECURSIVE ancestors(id) AS (
        SELECT $2::bigint UNION SELECT d.source_event_id FROM cognitive_event_dependencies d
            JOIN ancestors a ON d.event_id=a.id WHERE d.context_id=$1)
        SELECT array_agg(id) FROM ancestors''',turn.context_id,turn.event_id) or []
    ids=sorted(set(getattr(turn,'supporting_event_ids',()))|set(required)|{turn.event_id})
    count=await conn.fetchval('SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL AND payload IS NOT NULL',turn.context_id,ids)
    if count!=len(ids): return False
    turn.supporting_event_ids=ids
    turn.epoch=context['suppression_epoch']
    return True
