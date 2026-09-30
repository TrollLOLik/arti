"""Raw transcript compatibility, committed under the same suppression fence."""
from cognition.serialization import dump


async def save_source_history(conn,cid,eid,event,name,text,transport_id=None):
    source = await conn.fetchrow('SELECT suppressed_at FROM cognitive_events WHERE context_id=$1 AND id=$2',cid,eid)
    if not source or source['suppressed_at'] is not None:
        return None
    old = await conn.fetchval("SELECT legacy_id FROM cognitive_legacy_map WHERE source_table='memory_messages' AND context_id=$1 AND event_id=$2 AND status='imported'",cid,eid)
    if old:
        return old
    table = 'chat_history_rp' if event.context.mode=='rp' else 'chat_history'
    await conn.execute(f'INSERT INTO {table}(chat_id,timestamp,user_name,message_text) VALUES($1,$2,$3,$4)',event.context.chat_id,event.observed_at.replace(tzinfo=None),name,text)
    await conn.execute(f'DELETE FROM {table} WHERE chat_id=$1 AND id NOT IN (SELECT id FROM {table} WHERE chat_id=$1 ORDER BY timestamp DESC,id DESC LIMIT 30)',event.context.chat_id)
    role = 'user' if event.evidence.origin.value=='user' else 'assistant'
    mid = await conn.fetchval('''INSERT INTO memory_messages(chat_id,user_id,user_name,role,mode,source,message_text,metadata)
        VALUES($1,$2,$3,$4,$5,'confirmed_transport',$6,$7::jsonb) RETURNING id''',event.context.chat_id,event.evidence.owner_id,name,role,event.context.mode,text,
        dump(dict(cognitive_source_id=event.evidence.source_id,transport_id=transport_id,scene_id=event.context.scene_id)))
    await conn.execute("INSERT INTO cognitive_legacy_map VALUES('memory_messages',$1,$2,$3,NULL,'imported','linked_transport_source') ON CONFLICT DO NOTHING",mid,cid,eid)
    return mid


def invalidate_history(chat_id):
    from utils import chat_history
    for name in ('_context_cache','_recent_messages_cache','_dialog_history_cache','_context_cache_rp','_recent_messages_cache_rp','_dialog_history_cache_rp'):
        getattr(chat_history,name).pop(chat_id,None)
