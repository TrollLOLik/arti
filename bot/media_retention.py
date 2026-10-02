"""Bounded owner/scope-bound result and voice-reference retention.

Only a dedicated single-file namespace is retained, never an entire generator
workdir. References expire; terminal cancellation or source erasure overrides
retention. No filesystem path is stored or returned by this module.
"""
import asyncio
import json
import re
from bot.request_store import RequestStore
from bot.media_spool import MediaSpool

TTL={'result':86400,'voice_reference':900}


class RetentionUnavailable(ValueError): pass


async def initialize(conn):
    await conn.execute('''CREATE TABLE IF NOT EXISTS arti_media_retained (
        request_id TEXT NOT NULL REFERENCES arti_requests(id),
        purpose TEXT NOT NULL CHECK(purpose IN ('result','voice_reference')),
        owner_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, topic_id BIGINT NOT NULL,
        namespace TEXT NOT NULL REFERENCES arti_request_resources(namespace),
        descriptor JSONB NOT NULL, context_ids BIGINT[] NOT NULL DEFAULT '{}',
        source_event_ids BIGINT[] NOT NULL DEFAULT '{}',material_ids TEXT[] NOT NULL DEFAULT '{}',
        expires_at TIMESTAMPTZ NOT NULL, invalidated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(request_id,purpose)
    );
    ALTER TABLE arti_media_retained ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;
    CREATE INDEX IF NOT EXISTS arti_media_retained_expiry ON arti_media_retained(expires_at)
        WHERE invalidated_at IS NULL;
    CREATE INDEX IF NOT EXISTS arti_media_retained_namespace ON arti_media_retained(namespace);
    ''')


def _object(value): return json.loads(value) if isinstance(value,str) else value


def _descriptor(value):
    if not isinstance(value,dict) or set(value)!={'version','namespace','leaf','size','sha256'}:
        raise RetentionUnavailable('retained_descriptor_invalid')
    if type(value['version']) is not int or value['version']!=1 or type(value['size']) is not int or not 0<value['size']<=2*1024**3:
        raise RetentionUnavailable('retained_descriptor_invalid')
    for field,pattern in (('namespace',r'[0-9a-f]{32}'),('leaf',r'[0-9a-f]{32}\.[a-z0-9]{2,4}'),('sha256',r'[0-9a-f]{64}')):
        if not isinstance(value[field],str) or not re.fullmatch(pattern,value[field]): raise RetentionUnavailable('retained_descriptor_invalid')
    return dict(value)


def _owner(payload):
    payload=_object(payload)
    if not isinstance(payload,dict) or payload.get('codec')!=1 or payload.get('kind')!='request': return None
    value=payload.get('value',{})
    if not isinstance(value,dict) or value.get('codec')!=1 or value.get('kind')!='dict': return None
    owner=value.get('items',{}).get('user_id')
    return owner if type(owner) is int and owner>0 else None


def _deps(row): return {key:list(row[key]) for key in ('context_ids','source_event_ids','material_ids')}


def _row(row):
    if row is None: return None
    return {**dict(row),'descriptor':_object(row['descriptor'])}


def _contains_descriptor(value, descriptor, depth=0):
    if depth>32: return False
    if isinstance(value,dict):
        if value==descriptor: return True
        if value.get('codec')==1 and value.get('kind')=='dict' and value.get('items')==descriptor: return True
        return any(_contains_descriptor(child,descriptor,depth+1) for child in value.values())
    if isinstance(value,list): return any(_contains_descriptor(child,descriptor,depth+1) for child in value)
    return False


class Retention:
    def __init__(self,pool): self.pool=pool

    async def retain(self,request_id,token,purpose,descriptor,owner_id,*,origin=None):
        if purpose not in TTL or type(owner_id) is not int or owner_id<=0: raise RetentionUnavailable('retained_scope_invalid')
        descriptor=_descriptor(descriptor); requests=RequestStore(self.pool)
        async with self.pool.acquire() as conn,conn.transaction():
            before=await conn.fetchrow('SELECT * FROM arti_requests WHERE id=$1',request_id)
            if before is None: raise RetentionUnavailable('retained_request_unavailable')
            await requests._lock_dependencies(conn,_deps(before))
            row=await requests._locked(conn,request_id,token)
            if not row or _owner(row['payload'])!=owner_id or not await requests._allowed(conn,_deps(row)):
                raise RetentionUnavailable('retained_request_unavailable')
            bound=await conn.fetchval("SELECT 1 FROM arti_request_resources WHERE namespace=$1 AND request_id=$2 AND state='bound'",descriptor['namespace'],request_id)
            if not bound: raise RetentionUnavailable('retained_resource_unavailable')
            existing=await conn.fetchrow('SELECT *, expires_at>NOW() AS unexpired FROM arti_media_retained WHERE request_id=$1 AND purpose=$2 FOR UPDATE',request_id,purpose)
            if existing:
                if existing['invalidated_at'] is None and existing['unexpired']:
                    return _row(existing)
                # Rebuild an expired offer only before any substantive send
                # has begun, from a still-bound durable input/output checkpoint.
                # Consumption and explicit invalidation never grant renewal.
                renewable=not existing['unexpired'] and (existing['invalidated_at'] is None or existing['invalidation_reason']=='expired')
                if origin is None or not renewable:
                    raise RetentionUnavailable('retained_expired')
                origin=_descriptor(origin)
                persisted=_object(row['checkpoints']) if purpose=='result' else _object(row['payload'])
                if not _contains_descriptor(persisted,origin) or await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivered','delivery_unknown'))",request_id):
                    raise RetentionUnavailable('retained_expired')
                if not await conn.fetchval("SELECT 1 FROM arti_request_resources WHERE namespace=$1 AND request_id=$2 AND state='bound'",origin['namespace'],request_id):
                    raise RetentionUnavailable('retained_resource_unavailable')
                renewed=await conn.fetchrow('''UPDATE arti_media_retained SET namespace=$3,descriptor=$4::jsonb,
                    context_ids=$5,source_event_ids=$6,material_ids=$7,expires_at=NOW()+make_interval(secs=>$8),
                    invalidated_at=NULL,invalidation_reason=NULL,created_at=NOW()
                    WHERE request_id=$1 AND purpose=$2 RETURNING *''',request_id,purpose,descriptor['namespace'],json.dumps(descriptor),
                    row['context_ids'],row['source_event_ids'],row['material_ids'],float(TTL[purpose]))
                await conn.execute("UPDATE arti_request_resources s SET state='cleanup_pending',updated_at=NOW() WHERE namespace=$1 AND state='bound' AND NOT EXISTS(SELECT 1 FROM arti_media_retained t WHERE t.namespace=s.namespace AND t.invalidated_at IS NULL AND t.expires_at>NOW())",existing['namespace'])
                return _row(renewed)
            return _row(await conn.fetchrow('''INSERT INTO arti_media_retained
                (request_id,purpose,owner_id,chat_id,topic_id,namespace,descriptor,context_ids,source_event_ids,material_ids,expires_at)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,NOW()+make_interval(secs=>$11)) RETURNING *''',
                request_id,purpose,owner_id,row['chat_id'],row['topic_id'],descriptor['namespace'],json.dumps(descriptor),
                row['context_ids'],row['source_event_ids'],row['material_ids'],float(TTL[purpose])))

    async def load(self,request_id,purpose,owner_id,chat_id,topic_id):
        if purpose not in TTL or type(owner_id) is not int or owner_id<=0: return None
        requests=RequestStore(self.pool)
        async with self.pool.acquire() as conn,conn.transaction():
            row=await conn.fetchrow('''SELECT t.* FROM arti_media_retained t JOIN arti_requests r ON r.id=t.request_id
                JOIN arti_request_resources s ON s.namespace=t.namespace AND s.request_id=t.request_id
                WHERE t.request_id=$1 AND t.purpose=$2 AND t.owner_id=$3 AND t.chat_id=$4 AND t.topic_id=$5
                AND t.invalidated_at IS NULL AND t.expires_at>NOW() AND s.state='bound'
                AND (r.state IN ('completed','succeeded') OR (r.state='running' AND r.lease_until>NOW() AND r.deadline_at>NOW()))''',request_id,purpose,owner_id,chat_id,topic_id)
            if not row: return None
            await requests._lock_dependencies(conn,_deps(row))
            # Same request -> retained entry lock order as completion/forget.
            state=await conn.fetchval("SELECT CASE WHEN state IN ('completed','succeeded') OR (state='running' AND lease_until>NOW() AND deadline_at>NOW()) THEN state ELSE NULL END FROM arti_requests WHERE id=$1 FOR SHARE",request_id)
            if state not in ('running','completed','succeeded') or not await requests._allowed(conn,_deps(row)): return None
            current=await conn.fetchrow('''SELECT t.* FROM arti_media_retained t JOIN arti_request_resources s
                ON s.namespace=t.namespace AND s.request_id=t.request_id
                WHERE t.request_id=$1 AND t.purpose=$2 AND t.owner_id=$3 AND t.chat_id=$4 AND t.topic_id=$5
                AND t.invalidated_at IS NULL AND t.expires_at>NOW() AND s.state='bound' FOR SHARE OF t''',request_id,purpose,owner_id,chat_id,topic_id)
            return _row(current)

    async def release(self,request_id,purpose,owner_id,chat_id,topic_id):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT id FROM arti_requests WHERE id=$1 FOR UPDATE',request_id)
            row=await conn.fetchrow('''UPDATE arti_media_retained SET invalidated_at=COALESCE(invalidated_at,NOW()),invalidation_reason='released',descriptor='{}'
                WHERE request_id=$1 AND purpose=$2 AND owner_id=$3 AND chat_id=$4 AND topic_id=$5 RETURNING namespace''',request_id,purpose,owner_id,chat_id,topic_id)
            if row: await conn.execute("UPDATE arti_request_resources s SET state='cleanup_pending',updated_at=NOW() WHERE namespace=$1 AND state='bound' AND NOT EXISTS(SELECT 1 FROM arti_media_retained t WHERE t.namespace=s.namespace AND t.invalidated_at IS NULL AND t.expires_at>NOW())",row['namespace'])
            return row is not None


async def expire(pool,limit=100):
    if type(limit) is not int or not 1<=limit<=1000: raise ValueError('retention_expiry_budget')
    async with pool.acquire() as conn:
        keys=await conn.fetch('SELECT request_id,purpose FROM arti_media_retained WHERE invalidated_at IS NULL AND expires_at<=NOW() ORDER BY expires_at LIMIT $1',limit)
    count=0
    for key in keys:
        async with pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT id FROM arti_requests WHERE id=$1 FOR UPDATE',key['request_id'])
            row=await conn.fetchrow('''UPDATE arti_media_retained SET invalidated_at=NOW(),invalidation_reason='expired',descriptor='{}'
                WHERE request_id=$1 AND purpose=$2 AND invalidated_at IS NULL AND expires_at<=NOW() RETURNING namespace''',key['request_id'],key['purpose'])
            if row:
                await conn.execute("UPDATE arti_request_resources s SET state='cleanup_pending',updated_at=NOW() WHERE namespace=$1 AND state='bound' AND NOT EXISTS(SELECT 1 FROM arti_media_retained t WHERE t.namespace=s.namespace AND t.invalidated_at IS NULL AND t.expires_at>NOW())",row['namespace'])
                count+=1
    return count


async def retain_copy(pool,request_id,token,purpose,descriptor,owner_id,*,spool=None):
    """Copy only one verified result into a dedicated, registered namespace."""
    disk=spool or MediaSpool(); requests=RequestStore(pool)
    descriptor=_descriptor(descriptor)
    if purpose not in TTL or type(owner_id) is not int or owner_id<=0: raise RetentionUnavailable('retained_scope_invalid')
    async with pool.acquire() as conn:
        payload=await conn.fetchval("SELECT payload FROM arti_requests WHERE id=$1 AND token=$2 AND state='running' AND lease_until>NOW() AND deadline_at>NOW()",request_id,token)
    if _owner(payload)!=owner_id: raise RetentionUnavailable('retained_scope_invalid')
    if not await requests.resources_owned(request_id,token,[descriptor['namespace']]):
        raise RetentionUnavailable('retained_resource_unavailable')
    namespace=disk.create_namespace()
    # Register before writing. An uncertain DB outcome is left to tracked/orphan
    # cleanup, never followed by a potentially destructive speculative unlink.
    if not await requests.bind_resources(request_id,token,[namespace]):
        disk.cleanup(namespace)
        raise RetentionUnavailable('retained_request_unavailable')
    with disk.hold(descriptor['namespace']),disk.hold(namespace):
        copied=await asyncio.to_thread(disk.copy_to,descriptor,namespace)
        if not await requests.resources_owned(request_id,token,[descriptor['namespace'],namespace]):
            raise RetentionUnavailable('retained_request_unavailable')
        return await Retention(pool).retain(request_id,token,purpose,copied,owner_id,origin=descriptor)
