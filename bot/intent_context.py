"""Small owner-scoped routing context, separate from document instructions."""
import asyncio
from cognition.serialization import load_event


async def routing_context(request, turn, *, timeout=.75):
    materials = request.get('_material_uses', ())
    kinds=[]
    if request.get('document_text'): kinds.append('document')
    if request.get('base64_image'): kinds.append('image')
    if request.get('video_file_id'): kinds.append('video')
    result=dict(dialogue=[],materials=dict(count=len(materials) or len(kinds),kinds=kinds),
                actions=dict(project_selected=bool(request.get('_project_context')),reply_to_result=False))
    if turn is None or turn.event.evidence.owner_id is None:
        return result
    try:
        async with asyncio.timeout(timeout),turn.runtime.pool.acquire() as conn:
            rows=await conn.fetch('''SELECT payload FROM cognitive_events WHERE context_id=$1
                AND owner_id=$2 AND suppressed_at IS NULL AND id<>$3
                AND origin IN ('user','delivered_action') ORDER BY observed_at DESC,id DESC LIMIT 6''',
                turn.context_id,turn.event.evidence.owner_id,turn.event_id)
            for row in reversed(rows):
                event=load_event(row['payload'])
                result['dialogue'].append(dict(role='assistant' if event.evidence.origin.value=='delivered_action' else 'user',text=event.text[:700]))
            scope=request.get('_telegram_scope')
            if scope and scope.reply_to_id:
                result['actions']['reply_to_result']=bool(await conn.fetchval('''SELECT 1 FROM cognitive_events
                    WHERE context_id=$1 AND owner_id=$2 AND suppressed_at IS NULL AND origin='delivered_action'
                    AND event_key=$3''',turn.context_id,turn.event.evidence.owner_id,
                    f'telegram:{scope.chat_id}:{scope.reply_to_id}:delivered_action'))
    except (TimeoutError, ConnectionError):
        # Missing context is explicit absence, never a made-up reference.
        pass
    return result
