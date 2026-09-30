"""Single authority gate, also enforced at legacy mutation entry points."""
async def legacy_permitted(chat_id,mode=None):
    from cognition.runtime import CURRENT_TURN,get_runtime
    turn = CURRENT_TURN.get()
    if turn is not None and turn.active and turn.event.context.chat_id==chat_id:
        return False
    runtime = get_runtime()
    if not runtime or runtime.mode=='legacy':
        return True
    if mode is None:
        from config import rp_mode_state
        mode = 'rp' if rp_mode_state.get(chat_id) else 'default'
    context = await runtime.context(chat_id,mode)
    async with runtime.pool.acquire() as conn:
        authority = await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4',*context.identity())
    return (authority or runtime.mode)!='active'
