"""Versioned saved-library references, independently of unavailable uploader history."""
import hashlib
import json
from cognition.serialization import dump,load_event
from cognition.types import CognitiveEvent,EvidenceRef,Origin
from cognition.repositories import SuppressedEvidence


def version_of(row):
    values={k:row.get(k) for k in ('id','user_id','catbox_url','catbox_file_id','source_kind','cleaned','created_at')}
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=lambda v:v.isoformat(),ensure_ascii=False).encode()).hexdigest()


async def initialize(conn):
    # Source markers have no audio, URLs or names. Keep tombstones after library
    # deletion so old queued descriptors cannot regain authority.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_saved_voice_sources_schema')::bigint)")
    await conn.execute('''CREATE TABLE IF NOT EXISTS arti_saved_voice_sources (
        voice_id BIGINT NOT NULL,owner_id BIGINT NOT NULL,version TEXT NOT NULL,
        context_id BIGINT NOT NULL,event_id BIGINT NOT NULL,
        PRIMARY KEY(voice_id,owner_id,context_id,event_id)
    )''')


async def bind(runtime,context,context_id,owner_id,voice_id,expected_version,audience):
    if type(voice_id) is not int or voice_id<=0 or not isinstance(expected_version,str) or len(expected_version)!=64:
        raise ValueError('saved_voice_version_required')
    async with runtime.pool.acquire() as conn,conn.transaction():
        voice=await conn.fetchrow('SELECT * FROM saved_voices WHERE id=$1 AND user_id=$2 FOR UPDATE',voice_id,owner_id)
        if not voice or version_of(dict(voice))!=expected_version: raise SuppressedEvidence()
        current=await conn.fetchrow('SELECT * FROM cognitive_contexts WHERE id=$1 FOR UPDATE',context_id)
        if not current or current['rebuilding'] or current['authority']!='active': raise SuppressedEvidence()
        await runtime._validate_current_scene(conn,context_id)
        identity=f'saved-voice:{owner_id}:{voice_id}:{expected_version}'
        row=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1 AND event_key=$2',context_id,identity)
        if row:
            if row['suppressed_at'] is not None or row['payload'] is None or row['owner_id']!=owner_id: raise SuppressedEvidence()
            eid=row['id']
        else:
            at=runtime.clock()
            event=CognitiveEvent(identity,context,EvidenceRef(identity,identity,Origin.USER,owner_id),at,at,
                'Выбран сохранённый голос для принятого запроса',owner_id,event_kind='media_source',audience=audience,addressed_to_arti=False)
            payload=dump(event)
            eid=await conn.fetchval('''INSERT INTO cognitive_events(context_id,event_key,source_id,independent_group,origin,owner_id,occurred_at,observed_at,payload,fingerprint)
                VALUES($1,$2,$2,$2,'user',$3,$4,$4,$5::jsonb,$6) RETURNING id''',context_id,identity,owner_id,at,payload,hashlib.sha256(payload.encode()).hexdigest())
        await conn.execute('INSERT INTO arti_saved_voice_sources VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',voice_id,owner_id,expected_version,context_id,eid)
        return eid


async def revoke(conn,owner_id,voice_id):
    """Called while the voice row is locked; context -> request lock order follows."""
    rows=await conn.fetch('SELECT context_id,event_id FROM arti_saved_voice_sources WHERE voice_id=$1 AND owner_id=$2 ORDER BY context_id,event_id',voice_id,owner_id)
    if not rows: return
    ids=[r['event_id'] for r in rows]; contexts=sorted({r['context_id'] for r in rows})
    await conn.fetch('SELECT id FROM cognitive_contexts WHERE id=ANY($1::bigint[]) ORDER BY id FOR UPDATE',contexts)
    await conn.execute("UPDATE cognitive_events SET suppressed_at=COALESCE(suppressed_at,NOW()),payload=NULL,perception=NULL,fingerprint=NULL WHERE id=ANY($1::bigint[])",ids)
    # Any projection retaining the marker is invalid too; no new emotion or
    # interpretation is synthesized for either capture or revocation.
    await conn.execute('''UPDATE cognitive_artifacts a SET suppressed_at=COALESCE(suppressed_at,NOW()),payload=NULL
        WHERE EXISTS(SELECT 1 FROM cognitive_provenance p WHERE p.artifact_id=a.id AND p.source_event_id=ANY($1::bigint[]))''',ids)
    from bot.request_store import RequestStore
    await RequestStore(None).erase_sources(ids,conn)
