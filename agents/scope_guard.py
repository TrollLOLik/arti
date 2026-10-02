"""A background task keeps its original realm, including an RP scene boundary."""
from materials.types import MaterialError

async def guard_scope(actor,pool):
    from cognition.runtime import get_runtime
    from config import rp_mode_state
    runtime=get_runtime()
    if runtime is None or runtime.pool is not pool: return
    from cognition.scope import CURRENT_SCOPE,TransportScope
    token=CURRENT_SCOPE.set(TransportScope(actor.scope.chat_id,actor.scope.topic_id,actor.scope.chat_type,actor.user_id,sender_ref=actor.sender_ref))
    try:
        mode='rp' if rp_mode_state.get(actor.scope.chat_id) else 'default'
        if mode!=actor.scope.mode: raise MaterialError('task_scene_changed')
        if mode=='rp':
            current=await runtime.context(actor.scope.chat_id,mode,actor.scope.topic_id)
            if current.scene_id!=actor.scope.scene_id: raise MaterialError('task_scene_changed')
    finally: CURRENT_SCOPE.reset(token)
