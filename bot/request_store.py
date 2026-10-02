"""Durable per-chat/topic/kind FIFO requests and fenced delivery intents (no provider calls).

A committed sending intent is never automatically retried after lease loss.
Terminal records retain identifiers/diagnostics, never request or send bodies.
"""
import json
import re
import uuid


TERMINAL = ('completed', 'succeeded', 'failed', 'cancelled', 'expired', 'delivery_unknown')


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key in ('payload', 'checkpoints', 'receipt'):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


class RequestStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self, conn=None):
        if conn is None:
            async with self.pool.acquire() as connection:
                return await self.initialize(connection)
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_request_schema')::bigint)")
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS arti_requests (
                    id TEXT PRIMARY KEY, seq BIGSERIAL UNIQUE NOT NULL,
                    kind TEXT NOT NULL, chat_id BIGINT NOT NULL, topic_id BIGINT NOT NULL,
                    dedupe_key TEXT NOT NULL, payload JSONB NOT NULL,
                    checkpoints JSONB NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN
                        ('queued','running','completed','succeeded','failed','cancelled','expired','delivery_unknown','coalesced','paused')),
                    token TEXT, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
                    deadline_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), error_code TEXT,
                    parent_id TEXT, available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    context_ids BIGINT[] NOT NULL DEFAULT '{}', source_event_ids BIGINT[] NOT NULL DEFAULT '{}',
                    material_ids TEXT[] NOT NULL DEFAULT '{}',
                    UNIQUE(chat_id,topic_id,dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS arti_request_resources (
                    namespace TEXT PRIMARY KEY CHECK(namespace ~ '^[0-9a-f]{32}$'),
                    request_id TEXT NOT NULL REFERENCES arti_requests(id),
                    state TEXT NOT NULL DEFAULT 'bound' CHECK(state IN ('bound','cleanup_pending','cleaning','deleted')),
                    token TEXT, lease_until TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS arti_request_resources_owner ON arti_request_resources(request_id);
                CREATE INDEX IF NOT EXISTS arti_request_resources_cleanup ON arti_request_resources(state,updated_at)
                    WHERE state IN ('cleanup_pending','cleaning');
                ALTER TABLE arti_requests DROP CONSTRAINT IF EXISTS arti_requests_state_check;
                ALTER TABLE arti_requests ADD CONSTRAINT arti_requests_state_check CHECK(state IN
                    ('queued','running','completed','succeeded','failed','cancelled','expired','delivery_unknown','coalesced','paused'));
                CREATE TABLE IF NOT EXISTS arti_request_controls (
                    request_id TEXT NOT NULL REFERENCES arti_requests(id),control_key TEXT NOT NULL,
                    action TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(request_id,control_key)
                );
                CREATE INDEX IF NOT EXISTS arti_requests_ready ON arti_requests(state,seq);
                CREATE INDEX IF NOT EXISTS arti_requests_lane ON arti_requests(chat_id,topic_id,kind,seq) WHERE state IN ('queued','running');
                CREATE TABLE IF NOT EXISTS arti_request_sends (
                    request_id TEXT NOT NULL REFERENCES arti_requests(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL CHECK(ordinal>=0), payload JSONB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN
                        ('prepared','sending','delivered','delivery_unknown','cancelled')),
                    receipt JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(request_id,ordinal)
                );
            ''')
            from bot.media_retention import initialize as initialize_retention
            await initialize_retention(conn)
            from bot.saved_voice_sources import initialize as initialize_saved_voice_sources
            await initialize_saved_voice_sources(conn)

    @staticmethod
    def _dependencies(value):
        from bot.request_codec import dependencies
        return dependencies(value)

    async def _lock_dependencies(self, conn, deps):
        # Match erasure's context/asset -> request order. Registering a new
        # dependency must not race a scan that has not seen it yet.
        if deps['context_ids'] or deps['source_event_ids']:
            await conn.fetch("SELECT id FROM cognitive_contexts WHERE id=ANY($1::bigint[]) OR id IN (SELECT context_id FROM cognitive_events WHERE id=ANY($2::bigint[])) ORDER BY id FOR SHARE",deps['context_ids'],deps['source_event_ids'])
        if deps['material_ids']:
            await conn.fetch('SELECT id FROM material_assets WHERE id=ANY($1::text[]) ORDER BY id FOR SHARE',deps['material_ids'])

    async def _allowed(self, conn, deps):
        # A concurrent erase either precedes this check or scrubs the request
        # after this transaction releases its request lock. No context lock here:
        # erasure locks context then requests, so reversing it would deadlock.
        if deps['context_ids']:
            count=await conn.fetchval("SELECT COUNT(*) FROM cognitive_contexts WHERE id=ANY($1::bigint[]) AND NOT rebuilding",deps['context_ids'])
            if count!=len(deps['context_ids']): return False
        if deps['source_event_ids']:
            count=await conn.fetchval("SELECT COUNT(*) FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id WHERE e.id=ANY($1::bigint[]) AND e.suppressed_at IS NULL AND NOT c.rebuilding",deps['source_event_ids'])
            if count!=len(deps['source_event_ids']): return False
        if deps['material_ids']:
            count=await conn.fetchval("SELECT COUNT(*) FROM material_assets WHERE id=ANY($1::text[]) AND erased_at IS NULL AND expires_at>NOW()",deps['material_ids'])
            if count!=len(deps['material_ids']): return False
        return True

    async def enqueue(self, kind, chat_id, topic_id, dedupe_key, payload, budget_seconds=900, *, resources=()):
        if not isinstance(payload, dict) or not dedupe_key or not 1 <= budget_seconds <= (604800 if kind in ('dubbing','vclone') else 86400):
            raise ValueError('invalid_request')
        resources=self._resource_names(resources)
        deps=self._dependencies(payload)
        debounce=kind=='text' and payload.get('kind')=='request'
        if debounce and resources: raise ValueError('disk_resources_not_debounceable')
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'request:{chat_id}:{topic_id}:{kind}')
            existing=await conn.fetchrow('SELECT * FROM arti_requests WHERE chat_id=$1 AND topic_id=$2 AND dedupe_key=$3',chat_id,topic_id,dedupe_key)
            if existing:
                if existing['parent_id']:
                    existing=await conn.fetchrow('SELECT * FROM arti_requests WHERE id=$1',existing['parent_id'])
                return _row(existing)
            await self._lock_dependencies(conn,deps)
            if not await self._allowed(conn,deps): raise ValueError('suppressed_request_source')
            parent=None; merged=None
            if debounce:
                # Only newest non-child lane row may absorb input. Never jump
                # across another author's incompatible or already completed turn.
                parent=await conn.fetchrow("SELECT *, updated_at>NOW()-INTERVAL '0.6 seconds' AND created_at>NOW()-INTERVAL '2 seconds' AS recent FROM arti_requests WHERE chat_id=$1 AND topic_id=$2 AND kind=$3 AND parent_id IS NULL ORDER BY seq DESC LIMIT 1 FOR UPDATE",chat_id,topic_id,kind)
                if parent and parent['state']=='queued' and parent['recent']:
                    from bot.request_codec import coalesce_requests
                    merged=coalesce_requests(_row(parent)['payload'],payload)
            if merged is not None:
                combined=self._dependencies(merged)
                if not await self._allowed(conn,combined): raise ValueError('suppressed_request_source')
                await conn.execute("""INSERT INTO arti_requests(id,kind,chat_id,topic_id,dedupe_key,payload,state,parent_id,deadline_at,context_ids,source_event_ids,material_ids)
                    VALUES($1,$2,$3,$4,$5,'{}','coalesced',$6,NOW()+make_interval(secs=>$7),$8,$9,$10)""",uuid.uuid4().hex,kind,chat_id,topic_id,dedupe_key,parent['id'],float(budget_seconds),deps['context_ids'],deps['source_event_ids'],deps['material_ids'])
                return _row(await conn.fetchrow("""UPDATE arti_requests SET payload=$2::jsonb,context_ids=$3,source_event_ids=$4,material_ids=$5,
                    deadline_at=LEAST(deadline_at,NOW()+make_interval(secs=>$6)),updated_at=NOW(),
                    available_at=LEAST(created_at+INTERVAL '2 seconds',NOW()+INTERVAL '0.6 seconds') WHERE id=$1 RETURNING *""",
                    parent['id'],_json(merged),combined['context_ids'],combined['source_event_ids'],combined['material_ids'],float(budget_seconds)))
            row=_row(await conn.fetchrow("""INSERT INTO arti_requests
                (id,kind,chat_id,topic_id,dedupe_key,payload,deadline_at,available_at,context_ids,source_event_ids,material_ids)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,NOW()+make_interval(secs=>$7),NOW()+make_interval(secs=>$8),$9,$10,$11) RETURNING *""",
                uuid.uuid4().hex,kind,chat_id,topic_id,dedupe_key,_json(payload),float(budget_seconds),.6 if debounce else 0.,deps['context_ids'],deps['source_event_ids'],deps['material_ids']))
            await self._bind_resources(conn,row['id'],resources)
            return row

    async def _terminal(self, conn, id, state, code=None):
        await conn.execute('''UPDATE arti_requests SET state=$2,error_code=$3,payload='{}',checkpoints='{}',
            token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=$1''',id,state,code)
        await conn.execute('''UPDATE arti_request_sends SET payload='{}',
            state=CASE WHEN state='sending' THEN 'delivery_unknown' WHEN state='prepared' THEN 'cancelled' ELSE state END,
            updated_at=NOW() WHERE request_id=$1''',id)
        preserve=state in ('completed','succeeded') and code!='source_erased'
        await conn.execute("UPDATE arti_media_retained SET descriptor='{}',invalidated_at=COALESCE(invalidated_at,NOW()) WHERE request_id=$1 AND (NOT $2 OR expires_at<=NOW())",id,preserve)
        await conn.execute('''UPDATE arti_request_resources s SET state='cleanup_pending',updated_at=NOW()
            WHERE request_id=$1 AND state='bound' AND NOT EXISTS(SELECT 1 FROM arti_media_retained m
                WHERE m.namespace=s.namespace AND m.request_id=s.request_id AND m.invalidated_at IS NULL AND m.expires_at>NOW())''',id)

    async def claim(self, kinds, lease_seconds=60):
        self._lease(lease_seconds)
        async with self.pool.acquire() as conn, conn.transaction():
            # Lock requests first everywhere to avoid send/request lock inversions.
            expired = await conn.fetch('''SELECT * FROM arti_requests WHERE state IN ('queued','running','paused')
                AND (deadline_at<=NOW() OR (state='running' AND lease_until<=NOW()))
                ORDER BY seq FOR UPDATE SKIP LOCKED''')
            for row in expired:
                await conn.execute("UPDATE arti_request_sends SET state='delivery_unknown',payload='{}' WHERE request_id=$1 AND ordinal=0 AND state='sending'",row['id'])
                ambiguous = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",row['id'])
                if ambiguous:
                    await self._terminal(conn,row['id'],'delivery_unknown','lease_lost_during_send')
                elif await conn.fetchval('SELECT $1::timestamptz<=NOW()',row['deadline_at']):
                    await self._terminal(conn,row['id'],'expired','deadline_exceeded')
            row = await conn.fetchrow('''SELECT r.* FROM arti_requests r
                WHERE kind=ANY($1::text[]) AND deadline_at>NOW() AND available_at<=NOW()
                  AND (state='queued' OR (state='running' AND lease_until<=NOW()))
                  AND NOT EXISTS(SELECT 1 FROM arti_requests earlier
                    WHERE earlier.chat_id=r.chat_id AND earlier.topic_id=r.topic_id AND earlier.kind=r.kind
                      AND earlier.seq<r.seq AND earlier.state IN ('queued','running'))
                ORDER BY seq FOR UPDATE OF r SKIP LOCKED LIMIT 1''',list(kinds))
            if not row:
                return None
            return _row(await conn.fetchrow('''UPDATE arti_requests SET state='running',token=$2,
                lease_until=NOW()+make_interval(secs=>$3),attempts=attempts+1,updated_at=NOW()
                WHERE id=$1 RETURNING *''',row['id'],uuid.uuid4().hex,float(lease_seconds)))

    @staticmethod
    def _lease(seconds):
        if not 1 <= seconds <= 600:
            raise ValueError('invalid_lease')

    async def _locked(self, conn, id, token):
        return await conn.fetchrow('''SELECT * FROM arti_requests WHERE id=$1 AND token=$2
            AND state='running' AND lease_until>NOW() AND deadline_at>NOW() FOR UPDATE''',id,token)

    async def guard(self, id, token):
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval('''SELECT 1 FROM arti_requests WHERE id=$1 AND token=$2
                AND state='running' AND lease_until>NOW() AND deadline_at>NOW()''',id,token))

    async def renew(self, id, token, lease_seconds=60):
        self._lease(lease_seconds)
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            await conn.execute('UPDATE arti_requests SET lease_until=NOW()+make_interval(secs=>$2),updated_at=NOW() WHERE id=$1',id,float(lease_seconds))
            return True

    async def checkpoint(self, id, token, key, value, *, resources=()):
        if not isinstance(key,str) or not key: raise ValueError('invalid_checkpoint')
        resources=self._resource_names(resources)
        deps=self._dependencies(value)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_dependencies(conn,deps)
            if not await self._locked(conn,id,token): return False
            if not await self._allowed(conn,deps): return False
            await self._bind_resources(conn,id,resources)
            await conn.execute("""UPDATE arti_requests SET checkpoints=checkpoints||$2::jsonb,updated_at=NOW(),
                context_ids=ARRAY(SELECT DISTINCT unnest(context_ids||$3::bigint[])),
                source_event_ids=ARRAY(SELECT DISTINCT unnest(source_event_ids||$4::bigint[])),
                material_ids=ARRAY(SELECT DISTINCT unnest(material_ids||$5::text[])) WHERE id=$1""",
                id,_json({key:value}),deps['context_ids'],deps['source_event_ids'],deps['material_ids'])
            return True

    async def finish(self, id, token, state, error_code=None):
        if state not in TERMINAL: raise ValueError('invalid_terminal_state')
        if error_code is not None and (not isinstance(error_code,str) or not error_code.replace('_','').isalnum() or len(error_code)>80):
            raise ValueError('use_error_category_not_private_text')
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",id):
                state='delivery_unknown'
            await self._terminal(conn,id,state,error_code)
            return True

    async def release(self, id, token, *, delay_seconds=0):
        if not 0 <= delay_seconds <= 60: raise ValueError('invalid_release_delay')
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",id):
                await self._terminal(conn,id,'delivery_unknown','interrupted_send')
            else:
                await conn.execute("UPDATE arti_requests SET state='queued',token=NULL,lease_until=NULL,available_at=NOW()+make_interval(secs=>$2),updated_at=NOW() WHERE id=$1",id,float(delay_seconds))
            return True

    async def cancel_chat(self, chat_id, topic_id=None, conn=None):
        if conn is None:
            async with self.pool.acquire() as connection, connection.transaction():
                return await self.cancel_chat(chat_id,topic_id,connection)
        rows=await conn.fetch("SELECT id FROM arti_requests WHERE chat_id=$1 AND ($2::bigint IS NULL OR topic_id=$2) AND state IN ('queued','running','paused') ORDER BY seq FOR UPDATE",chat_id,topic_id)
        for row in rows:
            unknown=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",row['id'])
            await self._terminal(conn,row['id'],'delivery_unknown' if unknown else 'cancelled','cancelled')
        return len(rows)

    async def erase_chat(self, chat_id, topic_id=None, conn=None):
        """Forget all retained request/send data while preserving dedupe tombstones."""
        if conn is None:
            async with self.pool.acquire() as connection, connection.transaction():
                return await self.erase_chat(chat_id,topic_id,connection)
        await self.cancel_chat(chat_id,topic_id,conn)
        rows=await conn.fetch("SELECT id FROM arti_requests WHERE chat_id=$1 AND ($2::bigint IS NULL OR topic_id=$2) ORDER BY seq FOR UPDATE",chat_id,topic_id)
        ids=[r['id'] for r in rows]
        await conn.execute("UPDATE arti_requests SET payload='{}',checkpoints='{}',error_code=NULL,updated_at=NOW() WHERE id=ANY($1::text[])",ids)
        await conn.execute("UPDATE arti_request_sends SET payload='{}',receipt=NULL,updated_at=NOW() WHERE request_id=ANY($1::text[])",ids)
        await conn.execute("UPDATE arti_media_retained SET descriptor='{}',invalidated_at=COALESCE(invalidated_at,NOW()) WHERE request_id=ANY($1::text[])",ids)
        await conn.execute("UPDATE arti_request_resources SET state='cleanup_pending',updated_at=NOW() WHERE request_id=ANY($1::text[]) AND state='bound'",ids)
        return len(ids)

    async def _erase_dependencies(self, column, ids, conn=None):
        if column not in ('source_event_ids','material_ids'): raise ValueError('invalid_dependency')
        if conn is None:
            async with self.pool.acquire() as connection, connection.transaction():
                return await self._erase_dependencies(column,ids,connection)
        cast='bigint[]' if column=='source_event_ids' else 'text[]'
        rows=await conn.fetch(f"SELECT * FROM arti_requests WHERE {column} && $1::{cast} ORDER BY seq FOR UPDATE",list(ids))
        for row in rows:
            state=row['state']
            if state in ('queued','running','paused'):
                unknown=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",row['id'])
                state='delivery_unknown' if unknown else 'cancelled'
            await self._terminal(conn,row['id'],state,'source_erased')
            await conn.execute('UPDATE arti_request_sends SET receipt=NULL WHERE request_id=$1',row['id'])
        return len(rows)

    async def erase_sources(self, event_ids, conn=None):
        return await self._erase_dependencies('source_event_ids',event_ids,conn)

    async def erase_materials(self, asset_ids, conn=None):
        return await self._erase_dependencies('material_ids',asset_ids,conn)

    async def status(self, id):
        async with self.pool.acquire() as conn:
            parent=await conn.fetchval('SELECT parent_id FROM arti_requests WHERE id=$1',id)
            return _row(await conn.fetchrow('''SELECT id,seq,kind,chat_id,topic_id,state,attempts,
                deadline_at,created_at,updated_at,error_code FROM arti_requests WHERE id=$1''',parent or id))

    async def prepare_send(self, id, token, ordinal, payload):
        if type(ordinal) is not int or ordinal<0 or not isinstance(payload,dict): raise ValueError('invalid_send')
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return None
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",id):
                return None
            await conn.execute('''INSERT INTO arti_request_sends(request_id,ordinal,payload) VALUES($1,$2,$3::jsonb)
                ON CONFLICT(request_id,ordinal) DO NOTHING''',id,ordinal,_json(payload))
            return _row(await conn.fetchrow('SELECT * FROM arti_request_sends WHERE request_id=$1 AND ordinal=$2',id,ordinal))

    async def begin_send(self, id, token, ordinal):
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            return bool(await conn.fetchval("UPDATE arti_request_sends SET state='sending',updated_at=NOW() WHERE request_id=$1 AND ordinal=$2 AND state='prepared' AND NOT EXISTS(SELECT 1 FROM arti_request_sends s WHERE s.request_id=$1 AND s.ordinal>0 AND s.state IN ('sending','delivery_unknown')) RETURNING 1",id,ordinal))

    async def finish_send(self, id, token, ordinal, state, receipt=None):
        if state not in ('delivered','delivery_unknown'): raise ValueError('invalid_send_state')
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            return bool(await conn.fetchval('''UPDATE arti_request_sends SET state=$3,receipt=$4::jsonb,payload='{}',updated_at=NOW()
                WHERE request_id=$1 AND ordinal=$2 AND state='sending' RETURNING 1''',id,ordinal,state,_json(receipt)))


    @staticmethod
    def _resource_names(namespaces):
        if not isinstance(namespaces,(list,tuple,set,frozenset)) or len(namespaces)>64:
            raise ValueError('invalid_resource_namespaces')
        if any(not isinstance(n,str) or not re.fullmatch(r'[0-9a-f]{32}',n) for n in namespaces):
            raise ValueError('invalid_resource_namespace')
        return sorted(set(namespaces))

    async def _bind_resources(self,conn,id,namespaces):
        # Caller has locked the owning request, or has just inserted it. UUID
        # namespaces are one-use capabilities: terminal/deleted ones never bind
        # to another request and are never revived, even for the same owner.
        if not namespaces: return
        count=await conn.fetchval('SELECT count(*) FROM arti_request_resources WHERE request_id=$1 AND namespace<>ALL($2::text[])',id,namespaces)
        if count+len(namespaces)>64: raise ValueError('request_resource_limit')
        for namespace in namespaces:
            await conn.execute('INSERT INTO arti_request_resources(namespace,request_id) VALUES($1,$2) ON CONFLICT DO NOTHING',namespace,id)
            row=await conn.fetchrow('SELECT request_id,state FROM arti_request_resources WHERE namespace=$1 FOR UPDATE',namespace)
            if row['request_id']!=id or row['state']!='bound':
                raise ValueError('resource_already_owned')

    async def bind_resources(self,id,token,namespaces):
        """Register an attempt namespace before writing any generated content."""
        namespaces=self._resource_names(namespaces)
        async with self.pool.acquire() as conn,conn.transaction():
            if not await self._locked(conn,id,token): return False
            await self._bind_resources(conn,id,namespaces)
            return True

    async def resources_owned(self,id,token,namespaces):
        """Check every restored/read/send descriptor against its live request."""
        namespaces=self._resource_names(namespaces)
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM arti_requests r
                WHERE r.id=$1 AND r.token=$2 AND r.state='running' AND r.lease_until>NOW() AND r.deadline_at>NOW()
                  AND (SELECT count(*) FROM arti_request_resources s
                    WHERE s.request_id=r.id AND s.namespace=ANY($3::text[]) AND s.state='bound')=$4)''',id,token,namespaces,len(namespaces)))

    async def assert_resources(self,id,token,namespaces):
        if not await self.resources_owned(id,token,namespaces):
            raise ValueError('request_resource_unavailable')

    async def retained_resource_namespaces(self):
        """Protect all registered nondeleted resources from orphan collection.

        This includes pending cleanup: deletion goes through a fenced claim.
        Fresh unregistered staging must additionally be protected by spool age.
        """
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("SELECT namespace FROM arti_request_resources WHERE state!='deleted'")
        return {r['namespace'] for r in rows}

    async def claim_resource_cleanup(self,lease_seconds=60,*,grace_seconds=5):
        self._lease(lease_seconds)
        if type(grace_seconds) not in (int,float) or not 0<=grace_seconds<=3600: raise ValueError('invalid_cleanup_grace')
        async with self.pool.acquire() as conn,conn.transaction():
            row=await conn.fetchrow('''SELECT s.namespace FROM arti_request_resources s
                JOIN arti_requests r ON r.id=s.request_id
                WHERE r.state NOT IN ('queued','running','paused') AND s.updated_at<=NOW()-make_interval(secs=>$1) AND
                    (s.state='cleanup_pending' OR (s.state='cleaning' AND s.lease_until<=NOW()))
                ORDER BY s.updated_at,s.namespace FOR UPDATE OF s SKIP LOCKED LIMIT 1''',float(grace_seconds))
            if not row: return None
            return dict(await conn.fetchrow('''UPDATE arti_request_resources SET state='cleaning',token=$2,
                lease_until=NOW()+make_interval(secs=>$3),updated_at=NOW() WHERE namespace=$1 RETURNING *''',row['namespace'],uuid.uuid4().hex,float(lease_seconds)))

    async def finish_resource_cleanup(self,namespace,token,success=True):
        self._resource_names([namespace])
        if type(success) is not bool: raise ValueError('invalid_cleanup_result')
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval('''UPDATE arti_request_resources SET state=$3,token=NULL,
                lease_until=NULL,updated_at=NOW() WHERE namespace=$1 AND token=$2 AND state='cleaning'
                AND lease_until>NOW() RETURNING 1''',namespace,token,'deleted' if success else 'cleanup_pending'))

    async def resource_request(self,namespace):
        """Metadata-only adoption lookup, including terminal/deleted tombstones.

        Failure is not absence: callers must retain uncertain staged copies for
        age-graced orphan collection rather than delete after an enqueue error.
        """
        self._resource_names([namespace])
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT request_id FROM arti_request_resources WHERE namespace=$1',namespace)

    async def adopted_resources(self,id):
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('SELECT namespace FROM arti_request_resources WHERE request_id=$1',id)
        return {row['namespace'] for row in rows}


    async def pause(self,id,token,code='generation_interrupted'):
        """Retain accepted inputs, but do not silently repeat an uncertain billable generation."""
        if not isinstance(code,str) or not code.replace('_','').isalnum() or len(code)>80: raise ValueError('invalid_pause_code')
        async with self.pool.acquire() as conn,conn.transaction():
            if not await self._locked(conn,id,token): return False
            outcomes=await conn.fetch('SELECT state FROM arti_request_sends WHERE request_id=$1 AND ordinal>0',id)
            if any(r['state'] in ('sending','delivery_unknown') for r in outcomes):
                await self._terminal(conn,id,'delivery_unknown','interrupted_send'); return False
            if any(r['state']=='delivered' for r in outcomes):
                await self._terminal(conn,id,'failed','cannot_resume_delivered_request'); return False
            await conn.execute("UPDATE arti_requests SET state='paused',token=NULL,lease_until=NULL,error_code=$2,updated_at=NOW() WHERE id=$1",id,code)
            return True

    async def resume(self,id,chat_id,topic_id,owner_id,control_key):
        """A scoped explicit control resumes once; old/replayed callbacks cannot."""
        if type(owner_id) is not int or owner_id<=0 or not isinstance(control_key,str) or not 1<=len(control_key)<=160:
            raise ValueError('explicit_resume_control_required')
        async with self.pool.acquire() as conn,conn.transaction():
            row=await conn.fetchrow('SELECT * FROM arti_requests WHERE id=$1 FOR UPDATE',id)
            if not row or row['chat_id']!=chat_id or row['topic_id']!=topic_id or row['state']!='paused': return False
            payload=_row(row)['payload']; data=payload.get('value',{}).get('items',{})
            if payload.get('kind')!='request' or data.get('user_id')!=owner_id: return False
            if await conn.fetchval('SELECT $1::timestamptz<=NOW()',row['deadline_at']):
                await self._terminal(conn,id,'expired','deadline_exceeded'); return False
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('delivered','sending','delivery_unknown'))",id): return False
            if not await self._allowed(conn,self._dependencies(payload)): return False
            inserted=await conn.fetchval("INSERT INTO arti_request_controls(request_id,control_key,action) VALUES($1,$2,'resume') ON CONFLICT DO NOTHING RETURNING 1",id,control_key)
            if not inserted: return False
            await conn.execute("UPDATE arti_requests SET state='queued',checkpoints=checkpoints-'disk_generation_started',available_at=NOW(),error_code=NULL,updated_at=NOW() WHERE id=$1",id)
            return True
