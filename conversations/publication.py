"""Atomic native text receipts; Telegram mirroring is an independent transport."""
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import uuid


class GenerationUnavailable(Exception):
    pass


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.links=[]

    def handle_data(self,data):
        self.parts.append(data)

    def handle_starttag(self,tag,attrs):
        if tag=='a':
            href=dict(attrs).get('href','')
            if href.startswith(('https://','http://')):
                self.links.append(href)

    def handle_endtag(self,tag):
        if tag=='a' and self.links:
            href=self.links.pop()
            if href not in ''.join(self.parts):
                self.parts.append(' ('+href+')')


def plain_text(text,parse_mode=None):
    if parse_mode!='HTML':
        return str(text).strip()
    parser=_Plain()
    parser.feed(str(text))
    parser.close()
    return ''.join(parser.parts).strip()


@dataclass(frozen=True)
class PublishedReply:
    id: uuid.UUID
    text: str
    # Explicitly absent: this receipt is not a Telegram Message.
    message_id: None = None


class NativeTextSink:
    def __init__(self,pool,request):
        self.pool,self.request=pool,request
        self.ordinal=0

    async def send_chat_action(self,**kwargs):
        # Running state is published by the durable queue, not a fake Message.
        return True

    async def send_message(self,*,chat_id,text,parse_mode=None,**kwargs):
        from bot.request_runtime import CURRENT_REQUEST
        from bot.request_store import RequestStore
        from cognition.runtime import CURRENT_TURN
        from cognition.delivery import DeliverySuppressed
        from cognition.types import CognitiveEvent,EvidenceRef,Origin
        from cognition.serialization import dump
        from materials.runtime import guard_current
        from .repository import Repository
        from .mirror import prepare_mirror
        job,turn=CURRENT_REQUEST.get(),CURRENT_TURN.get()
        body=plain_text(text,parse_mode)
        if not job or not turn or not turn.tracks_delivery or chat_id!=self.request['user_id'] or not body or len(body)>8000 or len(body.encode())>32000:
            raise DeliverySuppressed()
        await guard_current(chat_id)
        self.ordinal+=1
        ordinal=self.ordinal
        digest=hashlib.sha256(body.encode()).hexdigest()
        cid=uuid.UUID(self.request['_app_conversation_id'])
        owner=self.request['user_id']
        store=RequestStore(self.pool)
        async with self.pool.acquire() as conn,conn.transaction():
            # Same order as intake and erasure; no network operation in this commit.
            deps=await conn.fetchrow('SELECT context_ids,material_ids FROM arti_requests WHERE id=$1',job['id'])
            if not deps:
                raise DeliverySuppressed()
            locked_contexts=sorted(set(list(deps['context_ids'])+[turn.context_id]))
            await conn.fetch('SELECT id FROM cognitive_contexts WHERE id=ANY($1::bigint[]) ORDER BY id FOR UPDATE',locked_contexts)
            if deps['material_ids']:
                await conn.fetch('SELECT id FROM material_assets WHERE id=ANY($1::text[]) ORDER BY id FOR SHARE',deps['material_ids'])
            ctx=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1',turn.context_id)
            support=sorted(set([turn.event_id,*getattr(turn,'supporting_event_ids',())]))
            count=await conn.fetchval('''SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND id=ANY($2::bigint[])
                AND suppressed_at IS NULL''',turn.context_id,support)
            if not ctx or ctx['rebuilding'] or ctx['authority']!='active' or ctx['suppression_epoch']!=turn.epoch or turn.event_id<=ctx['history_after_event_id'] or count!=len(support):
                raise DeliverySuppressed()
            private_ids=getattr(turn,'private_memory_ids',())
            if private_ids:
                allowed,_=await turn.runtime.memory.validate_retrieval(turn.context_id,owner,private_ids,expected_epoch=turn.epoch,connection=conn)
                if set(allowed)!=set(private_ids):
                    raise DeliverySuppressed()
            if getattr(turn,'delivery_guard',None) and not await turn.delivery_guard(turn,conn):
                raise DeliverySuppressed()
            await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE',cid)
            member=await Repository(self.pool)._member(conn,owner,cid)
            if member['kind']!='arti_private' or member['owner_id']!=owner or not await store._locked(conn,job['id'],job['token']):
                raise DeliverySuppressed()
            current=await conn.fetchrow('SELECT * FROM arti_requests WHERE id=$1',job['id'])
            if not set(current['context_ids']).issubset(locked_contexts) or not set(current['material_ids']).issubset(deps['material_ids']):
                raise DeliverySuppressed()
            if not await store._allowed(conn,dict(context_ids=list(current['context_ids']),source_event_ids=list(current['source_event_ids']),material_ids=list(current['material_ids']))):
                raise DeliverySuppressed()
            old=await conn.fetchrow('''SELECT p.body_hash,m.id,m.text,m.suppressed_at FROM app_publications p
                JOIN app_messages m ON m.id=p.message_id WHERE p.request_id=$1 AND p.ordinal=$2''',job['id'],ordinal)
            if old:
                if old['body_hash']!=digest or old['suppressed_at'] is not None:
                    raise DeliverySuppressed()
                return PublishedReply(old['id'],old['text'])
            mid=uuid.uuid4()
            delivery_key=f"{turn.event.event_id}:{job['id']}:miniapp:{ordinal}"
            from cognition.initiative_policy import charge_delivery
            if not await charge_delivery(conn,turn,delivery_key,turn.runtime.clock()):
                raise DeliverySuppressed()
            source=f'miniapp:{mid}:delivered_action'
            at=turn.runtime.clock()
            event=CognitiveEvent(source,turn.event.context,EvidenceRef(source,source,Origin.DELIVERED_ACTION,owner),
                at,at,body,None,owner,event_kind='delivery',audience=turn.event.audience)
            _,eid=await turn.runtime.repo.observe(event,conn=conn)
            await conn.execute('''INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id)
                SELECT $1,$2,unnest($3::bigint[]) ON CONFLICT DO NOTHING''',turn.context_id,eid,support)
            await conn.execute('''INSERT INTO cognitive_event_dependencies(context_id,event_id,source_event_id)
                SELECT $1,$2,source_event_id FROM cognitive_event_dependencies WHERE context_id=$1 AND event_id=$3
                ON CONFLICT DO NOTHING''',turn.context_id,eid,turn.event_id)
            await turn.runtime.jobs.enqueue(turn.context_id,eid,'encode',conn=conn)
            seq=await conn.fetchval('UPDATE app_conversations SET message_seq=message_seq+1,updated_at=now() WHERE id=$1 RETURNING message_seq',cid)
            await conn.execute('''INSERT INTO app_messages(id,conversation_id,seq,author_kind,text,payload_hash,source_key,
                reply_to_id,cognitive_context_id,cognitive_event_id,request_id,created_at)
                VALUES($1,$2,$3,'arti',$4,$5,$6,$7,$8,$9,$10,$11)''',mid,cid,seq,body,digest,source,
                uuid.UUID(self.request['_app_message_id']),turn.context_id,eid,job['id'],at)
            await conn.execute('INSERT INTO app_publications VALUES($1,$2,$3,$4)',job['id'],ordinal,mid,digest)
            await conn.execute("INSERT INTO app_deliveries(id,message_id,target,state,receipt_id) VALUES($1,$2,'miniapp','delivered',$3)",uuid.uuid4(),mid,str(mid))
            await Repository(self.pool)._event(conn,cid,'message.created',mid)
            await conn.execute('''INSERT INTO cognitive_outbox(context_id,event_id,delivery_key,channel,status,payload,suppression_epoch)
                VALUES($1,$2,$3,'miniapp','delivered',NULL,$4)''',turn.context_id,turn.event_id,delivery_key,turn.epoch)
            await prepare_mirror(conn,mid,body)
            # Keep retrieval feedback equivalent to confirmed Telegram delivery.
            included=await conn.fetchval("SELECT artifact_ids FROM cognitive_retrievals WHERE context_id=$1 AND cycle_key=$2 AND stage='included'",turn.context_id,turn.event.event_id) or []
            artifacts=await conn.fetch('SELECT id,payload FROM cognitive_artifacts WHERE id=ANY($1::bigint[]) AND context_id=$2 AND suppressed_at IS NULL',included,turn.context_id)
            from cognition.serialization import object_value
            expressed=[]
            for artifact in artifacts:
                data=object_value(artifact['payload'])
                anchors=[d['text'] for d in data.get('details',[])] if 'details' in data else [data.get('value','')]
                if any(len(a)>=8 and a in body for a in anchors):
                    expressed.append(artifact['id'])
            await conn.execute('''INSERT INTO cognitive_retrievals(context_id,cycle_key,owner_id,stage,artifact_ids,created_at)
                VALUES($1,$2,$3,'expressed',$4,$5) ON CONFLICT(context_id,cycle_key,stage) DO UPDATE
                SET artifact_ids=ARRAY(SELECT DISTINCT unnest(cognitive_retrievals.artifact_ids||EXCLUDED.artifact_ids))''',turn.context_id,turn.event.event_id,owner,expressed,at)
        from cognition.history import invalidate_history
        invalidate_history(chat_id)
        return PublishedReply(mid,body)
