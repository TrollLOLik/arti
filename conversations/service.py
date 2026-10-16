"""Shared private transcript and atomic, provider-free Mini App intake."""
import hashlib
import uuid

from .repository import Repository, public_conversation, public_message
from .types import ConversationError, identifier, message_input, page_size, user_id

NAMESPACE = uuid.UUID('a71a0465-7180-4252-827a-5c6c4cf301ec')
VISIBLE = '''msg.suppressed_at IS NULL AND (msg.cognitive_event_id IS NULL OR
 (e.id IS NOT NULL AND e.payload IS NOT NULL AND e.suppressed_at IS NULL AND NOT ctx.rebuilding AND e.id>ctx.history_after_event_id
 AND NOT EXISTS(SELECT 1 FROM cognitive_event_dependencies dep JOIN cognitive_events src
 ON src.id=dep.source_event_id WHERE dep.event_id=e.id AND src.suppressed_at IS NOT NULL)))'''
JOINS = '''LEFT JOIN cognitive_events e ON e.id=msg.cognitive_event_id
 LEFT JOIN cognitive_contexts ctx ON ctx.id=msg.cognitive_context_id
 LEFT JOIN arti_requests req ON req.id=msg.request_id'''
EXTRAS = '''req.state AS request_state,(SELECT d.state FROM app_deliveries d WHERE d.message_id=msg.id
 AND d.target='telegram' ORDER BY CASE d.state WHEN 'delivery_unknown' THEN 0 WHEN 'sending' THEN 1
 WHEN 'prepared' THEN 2 WHEN 'cancelled' THEN 3 ELSE 4 END LIMIT 1) AS mirror_state'''


def message(row):
    return public_message(row) | dict(source=row['source_channel'], request_id=row['request_id'],
        request_state=row.get('request_state'), mirror_state=row.get('mirror_state'))


class ArtiConversationService(Repository):
    def __init__(self, pool, runtime):
        super().__init__(pool)
        self.runtime = runtime

    @property
    def available(self):
        return self.runtime is not None and self.runtime.mode == 'active'

    async def _identity(self, actor, cid):
        from cognition.types import ContextKey
        member = await super().get(actor, cid)
        if member['kind'] != 'arti_private' or not self.available:
            raise ConversationError('feature_unavailable', 409)
        ref = await self.context(actor, cid)
        if ref.kind != 'telegram_private' or ref.telegram_chat_id != actor:
            raise ConversationError('context_unavailable', 409)
        key = ContextKey('arti', actor)
        return key, await self.runtime.ensure_context(key)

    async def synchronize(self, actor, cid):
        actor, cid = user_id(actor), identifier(cid)
        key, context_id = await self._identity(actor, cid)
        async with self.pool.acquire() as conn, conn.transaction():
            ctx = await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE', context_id)
            await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE', cid)
            await self._member(conn, actor, cid)
            if ctx['rebuilding']:
                raise ConversationError('history_rebuilding', 409)
            # No max-ID watermark: a lower sequence can commit after a higher one.
            # The context lock serializes source insertion and this projection.
            rows = await conn.fetch('''SELECT e.* FROM cognitive_events e WHERE e.context_id=$1
                AND e.owner_id=$2 AND e.id>$3 AND e.suppressed_at IS NULL AND e.payload IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM cognitive_event_dependencies dep JOIN cognitive_events src
                    ON src.id=dep.source_event_id WHERE dep.event_id=e.id AND src.suppressed_at IS NOT NULL)
                AND e.origin IN ('user','delivered_action')
                AND (e.payload->'audience'->>'kind'='private' OR
                    (e.origin='user' AND e.payload->>'event_kind'='historical'
                     AND COALESCE(e.payload->'audience'->>'kind','unknown')='unknown'
                     AND EXISTS(SELECT 1 FROM cognitive_legacy_map l JOIN memory_messages old ON old.id=l.legacy_id
                         WHERE l.source_table='memory_messages' AND l.status='imported' AND l.context_id=e.context_id
                         AND l.event_id=e.id AND old.chat_id=$2 AND old.user_id=$2 AND old.role='user' AND old.mode='default')))
                AND COALESCE(e.payload->>'addressed_to_arti','true')='true'
                AND NOT EXISTS(SELECT 1 FROM app_messages m WHERE m.cognitive_event_id=e.id)
                ORDER BY e.occurred_at,e.id''', context_id, actor, ctx['history_after_event_id'])
            for source in rows:
                from cognition.serialization import load_event
                event = load_event(source['payload'])
                text = event.text.strip()
                if not text:
                    continue
                if len(text)>8000:
                    text=text[:7960]+'\n[Показан фрагмент длинного сообщения]'
                mid=uuid.uuid5(NAMESPACE, str(context_id)+':'+source['event_key'])
                seq=await conn.fetchval('UPDATE app_conversations SET message_seq=message_seq+1,updated_at=now() WHERE id=$1 RETURNING message_seq', cid)
                is_user=source['origin']=='user'
                await conn.execute('''INSERT INTO app_messages(id,conversation_id,seq,author_id,author_kind,text,
                    client_message_id,payload_hash,source_key,created_at,cognitive_context_id,cognitive_event_id,source_channel)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)''', mid,cid,seq,actor if is_user else None,
                    'user' if is_user else 'arti',text,mid if is_user else None,hashlib.sha256(text.encode()).hexdigest(),
                    source['event_key'],source['occurred_at'],context_id,source['id'],
                    'miniapp' if source['event_key'].startswith('miniapp:') else 'telegram')
                await self._event(conn,cid,'message.created',mid)
        return key, context_id

    async def list(self, actor_id, limit=50):
        rows = await super().list(actor_id, limit)
        result=[]
        for row in rows:
            if row['kind']=='arti_private' and self.available:
                await self.synchronize(actor_id,row['id'])
                result.append(await self.get(actor_id,row['id']))
            else:
                result.append(row)
        return result

    async def get(self, actor_id, conversation_id):
        actor, cid=user_id(actor_id),identifier(conversation_id)
        async with self.pool.acquire() as conn:
            row=await self._member(conn,actor,cid)
            last=await conn.fetchrow(f'''SELECT msg.*,{EXTRAS} FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND msg.seq>=$2 AND {VISIBLE} ORDER BY msg.seq DESC LIMIT 1''',cid,row['visible_from_seq'])
            count=await conn.fetchval(f'''SELECT count(*) FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND msg.seq>=$2 AND {VISIBLE}''',cid,row['visible_from_seq'])
        return public_conversation(row) | dict(message_count=count,last_message=message(last) if last else None,
            event_cursor=row['event_seq'],history_ready=True,history_revision=row['history_revision'])

    async def messages(self, actor_id, conversation_id, *, before=None, after=None, limit=50):
        actor,cid,limit=user_id(actor_id),identifier(conversation_id),page_size(limit)
        if before is not None and after is not None:
            raise ConversationError('invalid_cursor')
        for value in (before,after):
            if value is not None and (type(value) is not int or not 0<value<2**63):
                raise ConversationError('invalid_cursor')
        await self.synchronize(actor,cid)
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read',readonly=True):
            member=await self._member(conn,actor,cid)
            order='ASC' if after is not None else 'DESC'
            rows=await conn.fetch(f'''SELECT msg.*,{EXTRAS} FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND msg.seq>=$2 AND {VISIBLE}
                AND ($3::bigint IS NULL OR msg.seq<$3) AND ($4::bigint IS NULL OR msg.seq>$4)
                ORDER BY msg.seq {order} LIMIT $5''',cid,member['visible_from_seq'],before,after,limit+1)
        selected=rows[:limit]
        return dict(items=[message(r) for r in (selected if after is not None else reversed(selected))],
            next_before=selected[-1]['seq'] if len(rows)>limit and after is None else None,
            history_revision=member['history_revision'])

    async def accept(self, actor_id, conversation_id, text, client_message_id, *, reply_to_id=None, history_revision=None):
        from bot.request_store import RequestStore
        from bot.request_codec import encode_request
        from cognition.scope import TransportScope
        from cognition.types import AudienceScope
        actor,cid=user_id(actor_id),identifier(conversation_id)
        client,reply,digest=message_input(text,client_message_id,reply_to_id)
        if history_revision is not None and (type(history_revision) is not int or not 0<history_revision<2**63):
            raise ConversationError('invalid_revision')
        key,context_id=await self.synchronize(actor,cid)
        async with self.pool.acquire() as conn, conn.transaction():
            # Scheduling authority's existing fence precedes its cognitive
            # delivery guard. Keep that order before acquiring the context.
            from organizer.ownership import _lock
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',_lock(actor))
            ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',context_id)
            if ctx['rebuilding'] or ctx['authority']!='active':
                raise ConversationError('history_rebuilding',409)
            await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE',cid)
            member=await self._member(conn,actor,cid)
            if history_revision is not None and history_revision!=member['history_revision']:
                raise ConversationError('history_reset',409)
            old=await conn.fetchrow('SELECT * FROM app_messages WHERE conversation_id=$1 AND author_id=$2 AND client_message_id=$3',cid,actor,client)
            if old:
                if old['payload_hash']!=digest:
                    raise ConversationError('idempotency_conflict',409)
                if old['suppressed_at'] is not None or old['seq']<member['visible_from_seq'] or not old['request_id']:
                    raise ConversationError('message_unavailable',410)
                job=await conn.fetchrow('SELECT id,state FROM arti_requests WHERE id=$1',old['request_id'])
                return dict(message=message(old),request=dict(job))
            if reply is not None and not await conn.fetchval(f'''SELECT 1 FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND msg.id=$2 AND msg.seq>=$3 AND {VISIBLE}''',cid,reply,member['visible_from_seq']):
                raise ConversationError('invalid_reply')
            mid=uuid.uuid4()
            source=f'miniapp:{cid}:{mid}:user'
            _,eid,_=await self.runtime.ingest(actor,actor,text,None,context=key,
                audience=AudienceScope('private',actor),addressed_to_arti=True,source_key=source,conn=conn)
            from organizer.natural import claim_input
            await claim_input(self.pool,actor,actor,None,text,source_key=source,conn=conn)
            actor_row=await conn.fetchrow('SELECT first_name FROM app_users WHERE id=$1',actor)
            scope=TransportScope(actor,chat_type='private',user_id=actor,channel='miniapp',source_key=source)
            request=dict(type='text',chat_id=actor,user_id=actor,user_name=actor_row['first_name'],user_message=text,
                message_id=None,is_voice=False,_channel='miniapp',_source_key=source,_app_conversation_id=str(cid),
                _app_message_id=str(mid),_telegram_scope=scope,_cognitive_context=key,_cognitive_source_ids=[eid],
                _request_mode='default',_request_no_coalesce=True,_native_user_message=text)
            payload=await encode_request(request,conn=conn)
            job=await RequestStore(self.pool).enqueue('text',actor,-1,'miniapp:'+str(client),payload,180,conn=conn)
            seq=await conn.fetchval('UPDATE app_conversations SET message_seq=message_seq+1,updated_at=now() WHERE id=$1 RETURNING message_seq',cid)
            row=await conn.fetchrow('''INSERT INTO app_messages(id,conversation_id,seq,author_id,author_kind,text,
                client_message_id,payload_hash,source_key,reply_to_id,cognitive_context_id,cognitive_event_id,request_id)
                VALUES($1,$2,$3,$4,'user',$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *''',
                mid,cid,seq,actor,text,client,digest,source,reply,context_id,eid,job['id'])
            await self._event(conn,cid,'message.created',mid)
            from .mirror import prepare_mirror
            await prepare_mirror(conn,mid,'Из Mini App · '+actor_row['first_name']+':\n'+text)
            return dict(message=message(dict(row,request_state=job['state'])),request=dict(id=job['id'],state=job['state']))

    async def events(self, actor_id, conversation_id, *, after=0, limit=100):
        actor,cid,limit=user_id(actor_id),identifier(conversation_id),page_size(limit)
        if type(after) is not int or not 0<=after<2**63:
            raise ConversationError('invalid_cursor')
        await self.synchronize(actor,cid)
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read',readonly=True):
            member=await self._member(conn,actor,cid)
            if after>member['event_seq']:
                raise ConversationError('invalid_cursor',409)
            rows=await conn.fetch('SELECT * FROM app_events WHERE conversation_id=$1 AND seq>$2 ORDER BY seq LIMIT $3',cid,after,limit)
            items=[]
            for event in rows:
                msg=await conn.fetchrow(f'''SELECT msg.*,{EXTRAS} FROM app_messages msg {JOINS}
                    WHERE msg.id=$1 AND msg.conversation_id=$2 AND msg.seq>=$3 AND {VISIBLE}''',event['resource_id'],cid,member['visible_from_seq']) if event['resource_id'] else None
                items.append(dict(seq=event['seq'],kind=event['kind'],resource_id=event['resource_id'],message=message(msg) if msg else None))
            requests=await conn.fetch(f'''SELECT DISTINCT req.id,req.state,msg.id AS message_id FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND msg.seq>=$2 AND msg.request_id IS NOT NULL AND {VISIBLE}
                AND (req.state IN ('queued','running') OR msg.seq>$3) ORDER BY msg.id''',cid,member['visible_from_seq'],max(0,member['message_seq']-100))
        return dict(items=items,cursor=rows[-1]['seq'] if rows else after,requests=[dict(r) for r in requests],history_revision=member['history_revision'])

    async def request_status(self, actor, cid, request_id):
        async with self.pool.acquire() as conn:
            await self._member(conn,user_id(actor),identifier(cid))
            row=await conn.fetchrow(f'''SELECT req.id,req.state,req.error_code FROM app_messages msg {JOINS}
                WHERE msg.conversation_id=$1 AND req.id=$2 AND {VISIBLE}''',identifier(cid),request_id)
        if not row:
            raise ConversationError('not_found',404)
        # Never expose raw provider/internal exception names.
        return dict(request=dict(id=row['id'],state=row['state']))
