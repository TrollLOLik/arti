"""Versioned, bounded JSON for durable work. No executable or path codecs.

The queue owns retention/deletion of encoded plaintext. Decoding revives no client
or permission grant: guarded sources are checked against the current repositories.
"""
import base64
import io
import json
import math
from dataclasses import asdict
from types import SimpleNamespace

VERSION = 1
MAX_BINARY = 32 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024
MAX_DEPTH = 48


class CodecError(ValueError):
    def __init__(self, code='unsupported_durable_value'):
        super().__init__(code)


REQUEST_FIELDS = frozenset('type chat_id user_id user_name user_message message_id is_voice base64_image base64_images document_text video_file_id is_video_note prompt image_urls image_aspect_ratio image_resolution image_num_images video_model video_duration video_aspect_ratio style instrumental _cognitive_context _cognitive_source_ids _material_uses _derivative_uses _computation_uses _cognitive_turn _telegram_scope _request_mode _request_no_coalesce url input_media reference_media synthesis_text cleaned source_kind with_subs audio_only'.split())
REQUEST_FIELDS = REQUEST_FIELDS | {'saved_voice_id','saved_voice_version','_native_user_message'}
# These are process-local bookkeeping, never input to the resumed operation.
TRANSIENT_FIELDS = frozenset(('bot', 'context', 'enqueued_at', 'started_at'))
TELEGRAM_TYPES = frozenset(('Message', 'ReplyParameters', 'MessageEntity', 'InputMediaPhoto',
    'InputMediaVideo', 'InputMediaAudio', 'InputMediaDocument', 'InlineKeyboardMarkup',
    'InlineKeyboardButton', 'ReplyKeyboardMarkup', 'KeyboardButton', 'ReplyKeyboardRemove',
    'ForceReply', 'LinkPreviewOptions', 'ReactionTypeEmoji', 'ReactionTypeCustomEmoji'))
TURN_EXTRA = ('supporting_event_ids', 'group_candidate_id', 'group_lease_token',
              'group_policy_revision', 'group_frame_revision', 'preferences', 'retrieval_diagnostics', 'private_memory_ids',
              'task_serious', 'expression_pending', 'expression_frozen', 'expression_support_event_ids',
              'group_context_source_ids', 'group_context_event_ids', 'group_understanding_generation',
              'group_context_manifest', 'group_context_revision')


def _bounded(value):
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError, RecursionError):
        raise CodecError('invalid_durable_json') from None
    if size > MAX_PAYLOAD:
        raise CodecError('durable_payload_too_large')
    return value


def _tag(kind, **values):
    return {'codec': VERSION, 'kind': kind, **values}


def _binary(value, filename=None):
    if len(value) > MAX_BINARY:
        raise CodecError('durable_binary_too_large')
    # A filename is metadata only and must not carry a filesystem path.
    if filename:
        filename = str(filename).replace('\\', '/').rsplit('/', 1)[-1][:255]
    return _tag('bytes', data=base64.b64encode(value).decode('ascii'), filename=filename)


async def encode_value(value):
    return _bounded(await _encode(value, 0))


async def _encode(value, depth):
    if depth > MAX_DEPTH:
        raise CodecError('durable_value_too_deep')
    if isinstance(value, str):
        return str(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CodecError('invalid_durable_number')
        return value
    if isinstance(value, (bytes, bytearray)):
        return _binary(bytes(value))
    if isinstance(value, io.IOBase):
        if not value.seekable() or not value.readable():
            raise CodecError('durable_stream_not_seekable')
        pos = value.tell()
        try:
            value.seek(0)
            raw = value.read(MAX_BINARY + 1)
        finally:
            value.seek(pos)
        if not isinstance(raw, bytes):
            raise CodecError('durable_stream_not_binary')
        return _tag('stream', value=_binary(raw, getattr(value, 'name', None)))
    if type(value) in (list, tuple):
        return _tag('tuple' if isinstance(value, tuple) else 'list', items=[await _encode(v, depth+1) for v in value])
    if isinstance(value, dict):
        if not all(type(k) is str for k in value):
            raise CodecError('durable_key_not_string')
        return _tag('dict', items={k: await _encode(v, depth+1) for k,v in value.items()})
    from cognition.scope import TransportScope
    from cognition.types import ContextKey, ExpressionPlan
    for cls in (TransportScope, ContextKey, ExpressionPlan):
        if type(value) is cls:
            return _tag(cls.__name__, value=await _encode(asdict(value), depth+1))
    from cognition.runtime import PreparedTurn
    if type(value) is PreparedTurn:
        data = {k:getattr(value,k) for k in ('context_id','event_id','epoch','authority','expression',
                 'memory','repeated_delivery','send_ordinal','delivery_blocked')}
        # Event content is loaded from the current source on recovery, never copied.
        data.update({k:getattr(value,k) for k in TURN_EXTRA if hasattr(value,k)})
        return _tag('PreparedTurn', value=await _encode(data, depth+1))
    from materials.runtime import MaterialUse, ComputationUse, DerivativeUse
    from projects.types import ProjectUse, WorkflowUse
    specs = {MaterialUse: ('asset_id','actor','version','generation','source_context_id','source_event_id'),
             ComputationUse: ('computation_id','actor'), DerivativeUse: ('id','actor','kind'),
             ProjectUse: ('project_id','actor','access_generation','revision','allow_archived'),
             WorkflowUse: ('id','actor','revision','head','status')}
    if type(value) in specs:
        data = {k:asdict(value.actor) if k=='actor' else getattr(value,k) for k in specs[type(value)]}
        return _tag(type(value).__name__, material_ids=await _guard_material_ids(value), value=await _encode(data, depth+1))
    import telegram
    if type(value) is telegram.InputFile:
        filename=str(value.filename).replace('\\','/').rsplit('/',1)[-1][:255] if value.filename else None
        return _tag('InputFile', value=await _encode(value.input_file_content,depth+1), filename=filename)
    name = type(value).__name__
    if name in TELEGRAM_TYPES and type(value) is getattr(telegram,name):
        data = value.to_dict()
        # InputMedia.to_dict() must not turn binary upload objects into metadata.
        for field in ('media','thumbnail','cover'):
            if hasattr(value,field) and getattr(value,field) is not None:
                data[field] = getattr(value,field)
        return _tag('telegram', name=name, value=await _encode(data,depth+1))
    raise CodecError()


async def decode_value(value):
    _bounded(value)
    return await _decode(value,0)


async def _decode(value, depth):
    if depth > MAX_DEPTH:
        raise CodecError('durable_value_too_deep')
    if value is None or type(value) in (str,bool,int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if not isinstance(value,dict) or value.get('codec') != VERSION:
        raise CodecError('invalid_durable_version')
    kind=value.get('kind')
    if kind in ('list','tuple'):
        if set(value)!={'codec','kind','items'} or not isinstance(value['items'],list): raise CodecError()
        items=[await _decode(v,depth+1) for v in value['items']]
        return tuple(items) if kind=='tuple' else items
    if kind=='dict':
        if set(value)!={'codec','kind','items'} or not isinstance(value['items'],dict): raise CodecError()
        return {k:await _decode(v,depth+1) for k,v in value['items'].items()}
    if kind=='bytes':
        if set(value)!={'codec','kind','data','filename'} or not isinstance(value['data'],str): raise CodecError()
        if len(value['data']) > (MAX_BINARY+2)//3*4: raise CodecError('durable_binary_too_large')
        try: raw=base64.b64decode(value['data'],validate=True)
        except (ValueError,TypeError): raise CodecError('invalid_durable_binary') from None
        if len(raw)>MAX_BINARY: raise CodecError('durable_binary_too_large')
        return raw
    if kind=='stream':
        if set(value)!={'codec','kind','value'}: raise CodecError()
        stream=io.BytesIO(await _decode(value['value'],depth+1))
        filename=value['value'].get('filename')
        if filename: stream.name=str(filename).replace('\\','/').rsplit('/',1)[-1][:255]
        return stream
    if kind=='InputFile':
        if set(value)!={'codec','kind','value','filename'}: raise CodecError()
        from telegram import InputFile
        content=await _decode(value['value'],depth+1)
        if not isinstance(content,(bytes,io.BytesIO)): raise CodecError()
        filename=value['filename']
        if filename: filename=str(filename).replace('\\','/').rsplit('/',1)[-1][:255]
        return InputFile(content,filename=filename)
    if kind=='telegram':
        if set(value)!={'codec','kind','name','value'} or value['name'] not in TELEGRAM_TYPES: raise CodecError()
        import telegram
        data=await _decode(value['value'],depth+1)
        if value['name'].startswith('InputMedia'):
            data.pop('type',None)
            return getattr(telegram,value['name'])(**data)
        return getattr(telegram,value['name']).de_json(data,bot=None)
    guard_kinds=('MaterialUse','ComputationUse','DerivativeUse','ProjectUse','WorkflowUse')
    if kind in guard_kinds:
        if set(value)!={'codec','kind','value','material_ids'} or not isinstance(value['material_ids'],list) or any(type(v) is not str or not v for v in value['material_ids']): raise CodecError()
    elif set(value)!={'codec','kind','value'}: raise CodecError()
    data=await _decode(value['value'],depth+1)
    if not isinstance(data,dict): raise CodecError()
    from cognition.scope import TransportScope
    from cognition.types import ContextKey, ExpressionPlan
    classes={c.__name__:c for c in (TransportScope,ContextKey,ExpressionPlan)}
    if kind in classes:
        if kind=='ExpressionPlan': data['cause_ids']=tuple(data['cause_ids'])
        return classes[kind](**data)
    if kind=='PreparedTurn':
        return await _restore_turn(data)
    if kind in ('MaterialUse','ComputationUse','DerivativeUse','ProjectUse','WorkflowUse'):
        return await _restore_use(kind,data)
    raise CodecError()


async def _restore_turn(data):
    from cognition.runtime import get_runtime, PreparedTurn
    from cognition.serialization import load_event
    from cognition.repositories import SuppressedEvidence
    runtime=get_runtime()
    if runtime is None: raise CodecError('durable_runtime_unavailable')
    allowed={'context_id','event_id','epoch','authority','expression','memory','repeated_delivery','send_ordinal','delivery_blocked',*TURN_EXTRA}
    if set(data)-allowed: raise CodecError()
    async with runtime.pool.acquire() as conn:
        row=await conn.fetchrow('''SELECT e.payload,c.suppression_epoch,c.authority,c.rebuilding,
                c.history_after_event_id,c.mode,c.scene_id,c.chat_id,c.topic_id
            FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
            WHERE e.context_id=$1 AND e.id=$2 AND e.suppressed_at IS NULL''',data['context_id'],data['event_id'])
        if not row or row['rebuilding'] or row['authority']!=data['authority']: raise SuppressedEvidence()
        extras={k:data[k] for k in TURN_EXTRA if k in data}
        turn=PreparedTurn(runtime=runtime,event=load_event(row['payload']),**{k:v for k,v in data.items() if k not in TURN_EXTRA})
        for k,v in extras.items(): setattr(turn,k,v)
        if turn.event.event_kind=='media_request':
            from bot.media_provenance import refresh_neutral_media
            if not await refresh_neutral_media(conn,turn,row): raise SuppressedEvidence()
        elif row['suppression_epoch']!=data['epoch']: raise SuppressedEvidence()
        support=data.get('supporting_event_ids',[])
        if support and await conn.fetchval('SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL',data['context_id'],support)!=len(set(support)): raise SuppressedEvidence()
        await runtime._validate_current_scene(conn,data['context_id'])
    if turn.event.audience.kind in ('group','topic'):
        await runtime.groups.validate_context(turn)
    return turn


async def _guard_material_ids(use):
    """Capture indirect provenance while the guard is live; never a permission grant."""
    from materials.runtime import MaterialUse
    from projects.types import ProjectUse, WorkflowUse
    if type(use) is MaterialUse:
        return [use.asset_id]
    await use.validate()
    # A failed lookup aborts persistence. Do not retain content whose deletion
    # dependencies could not be determined. UNION terminates cyclic graphs.
    async with use.repository.pool.acquire() as conn:
        if type(use) is ProjectUse:
            rows=await conn.fetch('''WITH RECURSIVE ancestors(id) AS (
                SELECT derivative_id FROM arti_project_results WHERE project_id=$1
                UNION SELECT head FROM arti_workflow_objects WHERE project_id=$1
                UNION SELECT l.input_id FROM material_derivative_links l JOIN ancestors a ON l.derivative_id=a.id)
                SELECT asset_id FROM arti_project_materials WHERE project_id=$1
                UNION SELECT d.asset_id FROM material_dependencies d JOIN ancestors a ON d.derivative_id=a.id''',use.project_id)
        else:
            head=use.head if type(use) is WorkflowUse else getattr(use,'computation_id',None) or use.id
            rows=await conn.fetch('''WITH RECURSIVE ancestors(id) AS (
                SELECT $1::text UNION SELECT l.input_id FROM material_derivative_links l JOIN ancestors a ON l.derivative_id=a.id)
                SELECT DISTINCT d.asset_id FROM material_dependencies d JOIN ancestors a ON d.derivative_id=a.id''',head)
    return sorted({row['asset_id'] for row in rows})


async def _restore_use(kind,data):
    from materials.types import AccessContext, MaterialScope
    from materials.runtime import service_for_bot, MaterialUse, ComputationUse, DerivativeUse
    from materials.derivatives import DerivativeRepository
    from projects.types import ProjectUse, WorkflowUse
    from projects.repository import ProjectRepository
    from projects.workflows import WorkflowRepository
    from materials.dataset_repository import DatasetRepository
    service=await service_for_bot()
    actor=data.pop('actor')
    data['actor']=AccessContext(scope=MaterialScope(**actor['scope']),user_id=actor['user_id'],sender_ref=actor['sender_ref'])
    if kind=='MaterialUse': use=MaterialUse(service=service,**data)
    elif kind=='ComputationUse': use=ComputationUse(repository=DatasetRepository(service.repository),**data)
    elif kind=='DerivativeUse': use=DerivativeUse(repository=DerivativeRepository(service.repository),**data)
    elif kind=='ProjectUse': use=ProjectUse(repository=ProjectRepository(service.repository),**data)
    else: use=WorkflowUse(repository=WorkflowRepository(service.repository),**data)
    await use.validate()
    return use


async def encode_request(request):
    if not isinstance(request,dict) or set(request)-REQUEST_FIELDS-TRANSIENT_FIELDS:
        raise CodecError('unsupported_durable_request_field')
    if request.get('type') not in ('text','image','video','music','dubbing','vclone'):
        raise CodecError('unsupported_durable_request_type')
    fence=None
    context=request.get('_cognitive_context')
    if context:
        from cognition.runtime import get_runtime
        from cognition.repositories import SuppressedEvidence
        runtime=get_runtime()
        if runtime is None: raise CodecError('durable_runtime_unavailable')
        async with runtime.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT id,suppression_epoch,rebuilding FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5',*context.identity())
            if not row or row['rebuilding']: raise SuppressedEvidence()
            await runtime._validate_current_scene(conn,row['id'])
            fence={'context_id':row['id'],'epoch':row['suppression_epoch']}
    return _bounded(_tag('request',fence=fence,value=await encode_value({k:v for k,v in request.items() if k in REQUEST_FIELDS})))


async def decode_request(payload,bot):
    if not isinstance(payload,dict) or set(payload)!={'codec','kind','value','fence'} or payload['codec']!=VERSION or payload['kind']!='request': raise CodecError()
    request=await decode_value(payload['value'])
    if not isinstance(request,dict) or set(request)-REQUEST_FIELDS or request.get('type') not in ('text','image','video','music','dubbing','vclone'): raise CodecError()
    scope=request.get('_telegram_scope'); context=request.get('_cognitive_context')
    if request.get('_request_mode',context.mode if context else 'default') not in ('default','rp'): raise CodecError('durable_mode_mismatch')
    if context and request.get('_request_mode',context.mode)!=context.mode: raise CodecError('durable_mode_mismatch')
    if scope and scope.chat_id!=request.get('chat_id'): raise CodecError('durable_scope_mismatch')
    if context and (context.chat_id!=request.get('chat_id') or scope and context.topic_id!=scope.topic_id): raise CodecError('durable_scope_mismatch')
    turn=request.get('_cognitive_turn')
    if turn is not None and turn.event.event_kind=='media_request':
        from bot.media_provenance import identity_for
        if context is None or turn.event.evidence.source_id!=identity_for(request,context): raise CodecError('media_request_content_changed')
    if turn and (turn.event.context.chat_id!=request.get('chat_id') or scope and turn.event.context.topic_id!=scope.topic_id): raise CodecError('durable_scope_mismatch')
    for field in ('_material_uses','_derivative_uses','_computation_uses'):
        for use in request.get(field,()):
            if use.actor.scope.chat_id!=request.get('chat_id') or scope and use.actor.scope.topic_id!=scope.topic_id:
                raise CodecError('durable_scope_mismatch')
    # Text requests have not prepared a turn yet. Validate their captured sources.
    if context:
        from cognition.runtime import get_runtime
        from cognition.repositories import SuppressedEvidence
        runtime=get_runtime()
        if runtime is None: raise CodecError('durable_runtime_unavailable')
        async with runtime.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5',*context.identity())
            fence=payload['fence']
            refreshed=False
            if turn is not None and turn.event.event_kind=='media_request':
                from bot.media_provenance import refresh_neutral_media
                refreshed=await refresh_neutral_media(conn,turn,row)
                if not refreshed: raise SuppressedEvidence()
            if not row or row['rebuilding'] or not isinstance(fence,dict) or set(fence)!={'context_id','epoch'} or row['id']!=fence['context_id'] or (row['suppression_epoch']!=fence['epoch'] and not refreshed): raise SuppressedEvidence()
            await runtime._validate_current_scene(conn,row['id'])
            ids=request.get('_cognitive_source_ids',[])
            if ids and await conn.fetchval('SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[]) AND suppressed_at IS NULL',row['id'],ids)!=len(set(ids)): raise SuppressedEvidence()
    request['context']=SimpleNamespace(bot=bot)
    request['bot']=bot
    return request


def _items(value, kind='dict'):
    """Read one known container without constructing runtime objects."""
    if not isinstance(value,dict) or value.get('codec')!=VERSION or value.get('kind')!=kind:
        return None
    items=value.get('items')
    return items if isinstance(items,dict if kind=='dict' else list) else None


def dependencies(value):
    """Indexes for privacy invalidation, drawn only from known codec records.

    Free text/ordinary dictionaries cannot masquerade as codec records: dictionary
    values are wrapped at encode time, and only their encoded children are visited.
    """
    _bounded(value)
    contexts=set(); sources=set(); materials=set()
    def integer(target,v):
        if type(v) is int and v>0: target.add(v)
    def sequence(v):
        if not isinstance(v,dict) or v.get('kind') not in ('list','tuple'): return []
        return _items(v,v['kind']) or []
    def visit(v,depth=0):
        if depth>MAX_DEPTH: raise CodecError('durable_value_too_deep')
        if not isinstance(v,dict) or v.get('codec')!=VERSION: return
        kind=v.get('kind')
        if kind=='request':
            fence=v.get('fence')
            if isinstance(fence,dict): integer(contexts,fence.get('context_id'))
            data=_items(v.get('value')) or {}
            for eid in sequence(data.get('_cognitive_source_ids')): integer(sources,eid)
            visit(v.get('value'),depth+1)
        elif kind in ('dict','list','tuple'):
            items=_items(v,kind)
            if items is not None:
                for child in (items.values() if kind=='dict' else items): visit(child,depth+1)
        elif kind in ('PreparedTurn','MaterialUse','ComputationUse','DerivativeUse','ProjectUse','WorkflowUse'):
            data=_items(v.get('value')) or {}
            if kind=='PreparedTurn':
                integer(contexts,data.get('context_id')); integer(sources,data.get('event_id'))
                for eid in sequence(data.get('supporting_event_ids')): integer(sources,eid)
            else:
                ids=v.get('material_ids',[])
                if isinstance(ids,list): materials.update(a for a in ids if type(a) is str and a)
                aid=data.get('asset_id')
                if type(aid) is str and aid: materials.add(aid)
                integer(contexts,data.get('source_context_id')); integer(sources,data.get('source_event_id'))
            visit(v.get('value'),depth+1)
    visit(value)
    return dict(context_ids=sorted(contexts),source_event_ids=sorted(sources),material_ids=sorted(materials))


def coalesce_requests(parent,new):
    """Merge compatible queued text intake, without restoring clients/permissions.

    Refuse rather than discard data at bounds. The store supplies row locking,
    the debounce deadline, and deduplication of the child's Telegram identity.
    """
    import copy
    def request(v):
        if not isinstance(v,dict) or set(v)!={'codec','kind','value','fence'} or v.get('codec')!=VERSION or v.get('kind')!='request': return None
        d=_items(v['value'])
        if d is None or d.get('type')!='text' or set(d)-REQUEST_FIELDS: return None
        return d
    left=request(parent); right=request(new)
    if left is None or right is None: return None
    if left.get('_request_no_coalesce') or right.get('_request_no_coalesce'): return None
    from organizer.natural import parse as native_intent
    if any('_native_user_message' not in d and d.get('user_message','').startswith('Исходный текст сообщения:') for d in (left,right)): return None
    from bot.agent_requests import direct_text
    def direct(d): return direct_text(d)
    if native_intent(direct(left)) or native_intent(direct(right)): return None
    from bot.agent_requests import patch_intent
    if patch_intent(direct(left)) or patch_intent(direct(right)): return None
    from ai.intents import direct_intent
    if direct_intent(direct(left))[0]['work'] or direct_intent(direct(right))[0]['work']: return None
    if parent['fence']!=new['fence']: return None
    for field in ('chat_id','user_id','_cognitive_context','_request_mode'):
        if left.get(field)!=right.get(field): return None
    def scope(d):
        v=d.get('_telegram_scope')
        if v is None: return None
        if not isinstance(v,dict) or v.get('codec')!=VERSION or v.get('kind')!='TransportScope': return False
        return _items(v.get('value'))
    a=scope(left); b=scope(right)
    if a is False or b is False or (a is None)!=(b is None): return None
    if a is not None:
        if any(a.get(k)!=b.get(k) for k in ('chat_id','topic_id','chat_type','user_id','sender_kind','sender_ref','reply_to_id')): return None
    if left.get('_cognitive_turn') or right.get('_cognitive_turn'): return None
    if type(left.get('user_message','')) is not str or type(right.get('user_message','')) is not str: return None
    text='\n'.join(x for x in (left.get('user_message',''),right.get('user_message','')) if x)
    if len(text)>8000: return None
    def seq(d,key):
        v=d.get(key)
        if v is None: return []
        if not isinstance(v,dict) or v.get('kind') not in ('tuple','list'): return None
        return _items(v,v['kind'])
    sources=[]
    for d in (left,right):
        values=seq(d,'_cognitive_source_ids')
        if values is None or any(type(v) is not int or v<=0 for v in values): return None
        sources.extend(values)
    sources=sorted(set(sources))
    if len(sources)>16: return None
    merged=copy.deepcopy(parent); data=merged['value']['items']
    data['_native_user_message']='\n'.join(x for x in (direct(left),direct(right)) if x)
    data['user_message']=text; data['message_id']=right.get('message_id',left.get('message_id'))
    data['_cognitive_source_ids']=_tag('list',items=sources)
    for field in ('document_text',):
        values=[left.get(field),right.get(field)]
        if any(v is not None and type(v) is not str for v in values): return None
        data[field]='\n'.join(v for v in values if v) or None
    for field in ('base64_image','video_file_id'):
        data[field]=left.get(field) or right.get(field)
    for field in ('_material_uses','_derivative_uses','_computation_uses'):
        values=[]; seen=set()
        for d in (left,right):
            items=seq(d,field)
            if items is None: return None
            for item in items:
                fingerprint=json.dumps(item,sort_keys=True,ensure_ascii=False,allow_nan=False)
                if fingerprint not in seen:
                    seen.add(fingerprint); values.append(copy.deepcopy(item))
        data[field]=_tag('tuple',items=values)
    try: return _bounded(merged)
    except CodecError: return None
