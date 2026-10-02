"""Conversation history from permitted cognitive sources and confirmed receipts."""
import logging
from datetime import datetime, timedelta, timezone
logger = logging.getLogger(__name__)
# Kept as invalidation targets for old callers; history itself is source-scoped.
_context_cache = {}
_recent_messages_cache = {}
_dialog_history_cache = {}
_context_cache_rp = {}
_recent_messages_cache_rp = {}
_dialog_history_cache_rp = {}

async def _save_cognitive_history(chat_id,user_name,text,user_id,message_id,mode,occurred_at):
    from cognition.runtime import get_runtime
    runtime = get_runtime()
    if not runtime or runtime.mode=='legacy' or message_id is None or user_name in ('Арти','Память'):
        return True
    from cognition.scope import CURRENT_SCOPE
    scope=CURRENT_SCOPE.get()
    if scope and scope.group and scope.chat_id==chat_id:
        if user_name=='Арти' or scope.sender_kind=='bot' or scope.topic_id<0: return True
        from dataclasses import replace
        from cognition.serialization import load_event
        cid=await runtime.groups.observe(replace(scope,message_id=message_id),text,mode,occurred_at)
        async with runtime.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT e.id,e.payload FROM cognitive_events e JOIN group_observations o ON o.event_id=e.id WHERE o.context_id=$1 AND o.message_id=$2 AND e.suppressed_at IS NULL',cid,message_id)
        if not row: return True
        eid=row['id']; event=load_event(row['payload'])
    else:
        if user_id is None: return True
        cid,eid,event = await runtime.ingest(chat_id,user_id,text,message_id,mode,occurred_at)
    from cognition.history import save_source_history,invalidate_history
    async with runtime.pool.acquire() as conn,conn.transaction():
        await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
        await save_source_history(conn,cid,eid,event,user_name,event.text,message_id)
    invalidate_history(chat_id)
    return True


async def save_chat_message(chat_id,user_name,message_text,user_id=None,*,message_id=None,occurred_at=None):
    await _save_cognitive_history(chat_id,user_name,message_text,user_id,message_id,'default',occurred_at)

async def save_chat_message_rp(chat_id,user_name,message_text,user_id=None,*,message_id=None,occurred_at=None):
    await _save_cognitive_history(chat_id,user_name,message_text,user_id,message_id,'rp',occurred_at)

async def _group_history(chat_id,mode):
    from cognition.scope import CURRENT_SCOPE
    from cognition.runtime import get_runtime
    scope=CURRENT_SCOPE.get(); runtime=get_runtime()
    if scope and scope.group and scope.chat_id==chat_id:
        return await runtime.groups.history(scope,mode) if runtime else ''
    return None

async def _source_messages(chat_id,mode,limit):
    from cognition.runtime import get_runtime
    runtime=get_runtime()
    if runtime is None or runtime.mode=='legacy': return []
    context=await runtime.context(chat_id,mode)
    async with runtime.pool.acquire() as conn:
        rows=await conn.fetch("""SELECT e.occurred_at,e.origin,e.owner_id,e.payload->>'text' AS text,
            (SELECT m.user_name FROM cognitive_legacy_map l JOIN memory_messages m ON m.id=l.legacy_id
             WHERE l.source_table='memory_messages' AND l.context_id=c.id AND l.event_id=e.id
             AND l.status='imported' AND m.user_id IS NOT DISTINCT FROM e.owner_id
             ORDER BY m.id LIMIT 1) AS name
            FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
            WHERE c.persona_id=$1 AND c.chat_id=$2 AND c.mode=$3 AND c.scene_id=$4 AND c.topic_id=$5
            AND NOT c.rebuilding AND e.id>c.history_after_event_id
            AND e.suppressed_at IS NULL AND e.origin IN ('user','delivered_action')
            ORDER BY e.occurred_at DESC,e.id DESC LIMIT $6""",*context.identity(),min(100,max(1,limit)))
    return [(r['occurred_at'],('Арти' if r['origin']=='delivered_action' else r['name'] or 'Участник '+str(r['owner_id']))+': '+r['text']) for r in reversed(rows)]

async def _context(chat_id,mode,limit,dates):
    group=await _group_history(chat_id,mode)
    if group is not None: return group
    messages=await _source_messages(chat_id,mode,limit)
    return '\n'.join(f"[{at.strftime('%Y-%m-%d %H:%M:%S')}] {text}" if dates else text for at,text in messages)

async def get_chat_context(chat_id,limit=20):
    return await _context(chat_id,'default',limit,True)

async def get_chat_context_rp(chat_id,limit=20):
    return await _context(chat_id,'rp',limit,True)

async def get_dialog_history_as_text(chat_id,limit=20):
    return await _context(chat_id,'default',limit,False)

async def get_dialog_history_as_text_rp(chat_id,limit=20):
    return await _context(chat_id,'rp',limit,False)

async def get_recent_messages(chat_id,timeout):
    timeout=timedelta(seconds=timeout) if isinstance(timeout,(int,float)) else timeout
    now=datetime.now(timezone.utc)
    return [(at,text) for at,text in await _source_messages(chat_id,'default',100) if now-at<=timeout]
