"""Allocate typed native cognitive state; room inference is gated until MA06."""
from cognition.types import NativeContextKey
from cognition.serialization import dump,load_state
from cognition.affect import initial_state
from datetime import datetime,timezone


async def allocate(conn,conversation_id):
    key=NativeContextKey(conversation_id)
    state=initial_state(key,datetime.now(timezone.utc))
    await conn.execute('''INSERT INTO cognitive_native_contexts(id,persona_id,mode,state)
        VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING''',key.native_id,key.persona_id,key.mode,dump(state))
    return key


async def load(conn,conversation_id):
    value=await conn.fetchval('SELECT state FROM cognitive_native_contexts WHERE id=$1',conversation_id)
    return load_state(value) if value else None