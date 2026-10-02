"""Durable ownership of private native organizer sources, without content.

A native route owns scheduling even while clarification is pending or after the
native item is cancelled. Cognitive interpretation may retain the statement as
memory, but must never create a second delivery authority for the same cause.
"""
from contextlib import asynccontextmanager
import re
from organizer.time import OrganizerError


async def initialize(conn):
    await conn.execute('''CREATE TABLE IF NOT EXISTS arti_organizer_source_routes (
        owner_id BIGINT NOT NULL CHECK(owner_id>0),
        chat_id BIGINT NOT NULL CHECK(chat_id=owner_id),
        source_key TEXT NOT NULL, root_source_key TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(owner_id,chat_id,source_key)
    )''')
    await conn.execute('CREATE INDEX IF NOT EXISTS arti_organizer_source_roots ON arti_organizer_source_routes(owner_id,chat_id,root_source_key)')


def source_key(chat,message_id):
    if type(chat) is not int or chat<=0 or type(message_id) is not int or message_id<=0:
        raise OrganizerError('invalid_native_source')
    return f'telegram:{chat}:{message_id}:user'


def _validate(owner,chat,key):
    if type(owner) is not int or type(chat) is not int or owner<=0 or owner!=chat:
        raise OrganizerError('private_chat_required')
    if not isinstance(key,str) or not re.fullmatch(r'telegram:'+str(chat)+r':[1-9][0-9]*:user',key):
        raise OrganizerError('invalid_native_source')


def _lock(owner): return f'organizer-cognitive-routing:{owner}'


async def claim_source(pool,owner,chat,message_id,root_source_key=None):
    key=source_key(chat,message_id); root=root_source_key or key
    _validate(owner,chat,key); _validate(owner,chat,root)
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',_lock(owner))
        existing=await conn.fetchval('SELECT 1 FROM arti_organizer_source_routes WHERE owner_id=$1 AND chat_id=$2 AND source_key=$3',owner,chat,key)
        if existing: return True
        if root!=key and not await conn.fetchval('SELECT 1 FROM arti_organizer_source_routes WHERE owner_id=$1 AND chat_id=$2 AND source_key=$3',owner,chat,root):
            raise OrganizerError('native_root_not_claimed')
        await conn.execute('INSERT INTO arti_organizer_source_routes(owner_id,chat_id,source_key,root_source_key) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',owner,chat,key,root)
    return True


async def link_source(pool,owner,chat,root_source_key,current_source_key):
    _validate(owner,chat,current_source_key)
    return await claim_source(pool,owner,chat,int(current_source_key.split(':')[2]),root_source_key)


# Interpretation stores retrieval dependencies too. Merely remembering a native
# reminder must not route every later user request to that organizer. Only actual
# bot descendants or explicit reply edges transfer route ownership; clarification
# answers without a reply are explicitly marked by claim_source/link_source.
async def owns_event(conn,context_id,event_id):
    return bool(await conn.fetchval('''WITH RECURSIVE ancestors(id) AS (
        SELECT id FROM cognitive_events WHERE context_id=$1 AND id=$2
        UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN ancestors a ON d.event_id=a.id
            JOIN cognitive_events child ON child.id=d.event_id
            JOIN cognitive_events parent ON parent.id=d.source_event_id
            WHERE d.context_id=$1 AND (child.origin='delivered_action'
                OR child.payload->>'reply_to_id'=split_part(parent.source_id,':',3)))
        SELECT EXISTS(SELECT 1 FROM ancestors a JOIN cognitive_events e ON e.id=a.id AND e.context_id=$1
            JOIN cognitive_contexts c ON c.id=e.context_id
            JOIN arti_organizer_source_routes r ON r.owner_id=e.owner_id AND r.chat_id=c.chat_id AND r.source_key=e.source_id
            WHERE c.chat_id=e.owner_id AND c.chat_id>0 AND c.topic_id<0)''',context_id,event_id))


async def owns_intention(conn,context_id,artifact_id):
    return bool(await conn.fetchval('''WITH RECURSIVE ancestors(id) AS (
        SELECT p.source_event_id FROM cognitive_provenance p
            JOIN cognitive_artifacts a ON a.id=p.artifact_id AND a.context_id=p.context_id
            WHERE p.context_id=$1 AND p.artifact_id=$2 AND a.kind='intention'
        UNION SELECT d.source_event_id FROM cognitive_event_dependencies d JOIN ancestors a ON d.event_id=a.id
            JOIN cognitive_events child ON child.id=d.event_id
            JOIN cognitive_events parent ON parent.id=d.source_event_id
            WHERE d.context_id=$1 AND (child.origin='delivered_action'
                OR child.payload->>'reply_to_id'=split_part(parent.source_id,':',3)))
        SELECT EXISTS(SELECT 1 FROM ancestors a JOIN cognitive_events e ON e.id=a.id AND e.context_id=$1
            JOIN cognitive_contexts c ON c.id=e.context_id
            JOIN arti_organizer_source_routes r ON r.owner_id=e.owner_id AND r.chat_id=c.chat_id AND r.source_key=e.source_id
            WHERE c.chat_id=e.owner_id AND c.chat_id>0 AND c.topic_id<0)''',context_id,artifact_id))


@asynccontextmanager
async def cognitive_delivery_allowed(pool,owner,chat,context_id,artifact_id):
    # Native routing is private only. Existing group/other-owner intentions keep
    # their ordinary policy. For private delivery serialize the last check with
    # route claims and hold that boundary until the send attempt is recorded.
    if type(owner) is not int or owner<=0 or owner!=chat:
        yield True
        return
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',_lock(owner))
        yield not await owns_intention(conn,context_id,artifact_id)
