"""Current, source-scoped acoustic context. No speaker-to-person inference."""
from materials.types import AccessContext,MaterialScope,MaterialError
from materials.repository import MaterialRepository
from materials.observations import ObservationRepository


async def acoustic_context(pool,event):
    if event.evidence.origin.value!='user' or event.actor_id is None or event.audience.kind=='unknown': return ()
    kind='private' if event.audience.kind=='private' else 'supergroup'
    scope=MaterialScope(event.context.persona_id,event.context.chat_id,event.context.topic_id,kind,event.context.mode,event.context.scene_id)
    actor=AccessContext(scope,event.actor_id,'user:'+str(event.actor_id))
    repo=MaterialRepository(pool); observations=ObservationRepository(repo)
    async with pool.acquire() as conn:
        rows=await conn.fetch('''SELECT id,scope FROM material_assets WHERE identity_key=$1 AND source_id=$2
            AND owner_id=$3 AND erased_at IS NULL AND expires_at>NOW() ORDER BY id LIMIT 4''',scope.identity_key,event.evidence.source_id,event.actor_id)
    output=[]
    for row in rows:
        # Stored transport type participates in the full authorization key.
        import json
        stored=json.loads(row['scope']) if isinstance(row['scope'],str) else row['scope']
        actor=AccessContext(MaterialScope(**stored),event.actor_id,'user:'+str(event.actor_id))
        try:
            id,timeline=await observations.current(actor,row['id'])
            if not timeline.acoustic: continue
            output.append(dict(source_id=event.evidence.source_id,observation_id=id,samples=list(timeline.acoustic[:40]),
                status='unassessed_acoustic_observation',speaker_identity='unknown',
                constraints='Energy and zero crossings depend on microphone, distance and noise. They do not prove emotion, intention, identity or consent. No appraisal of a person from acoustics alone.'))
        except MaterialError: continue
    return tuple(output)
