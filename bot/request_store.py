"""Durable per-chat/topic/kind FIFO requests and fenced delivery intents (no provider calls).

A committed sending intent is never automatically retried after lease loss.
Terminal records retain identifiers/diagnostics, never request or send bodies.
"""
import json
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
                        ('queued','running','completed','succeeded','failed','cancelled','expired','delivery_unknown','coalesced')),
                    token TEXT, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
                    deadline_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), error_code TEXT,
                    parent_id TEXT, available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    context_ids BIGINT[] NOT NULL DEFAULT '{}', source_event_ids BIGINT[] NOT NULL DEFAULT '{}',
                    material_ids TEXT[] NOT NULL DEFAULT '{}',
                    UNIQUE(chat_id,topic_id,dedupe_key)
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

    async def enqueue(self, kind, chat_id, topic_id, dedupe_key, payload, budget_seconds=900):
        if not isinstance(payload, dict) or not dedupe_key or not 1 <= budget_seconds <= 86400:
            raise ValueError('invalid_request')
        deps=self._dependencies(payload)
        debounce=kind=='text' and payload.get('kind')=='request'
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
            return _row(await conn.fetchrow("""INSERT INTO arti_requests
                (id,kind,chat_id,topic_id,dedupe_key,payload,deadline_at,available_at,context_ids,source_event_ids,material_ids)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,NOW()+make_interval(secs=>$7),NOW()+make_interval(secs=>$8),$9,$10,$11) RETURNING *""",
                uuid.uuid4().hex,kind,chat_id,topic_id,dedupe_key,_json(payload),float(budget_seconds),.6 if debounce else 0.,deps['context_ids'],deps['source_event_ids'],deps['material_ids']))

    async def _terminal(self, conn, id, state, code=None):
        await conn.execute('''UPDATE arti_requests SET state=$2,error_code=$3,payload='{}',checkpoints='{}',
            token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=$1''',id,state,code)
        await conn.execute('''UPDATE arti_request_sends SET payload='{}',
            state=CASE WHEN state='sending' THEN 'delivery_unknown' WHEN state='prepared' THEN 'cancelled' ELSE state END,
            updated_at=NOW() WHERE request_id=$1''',id)

    async def claim(self, kinds, lease_seconds=60):
        self._lease(lease_seconds)
        async with self.pool.acquire() as conn, conn.transaction():
            # Lock requests first everywhere to avoid send/request lock inversions.
            expired = await conn.fetch('''SELECT * FROM arti_requests WHERE state IN ('queued','running')
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

    async def checkpoint(self, id, token, key, value):
        if not isinstance(key,str) or not key: raise ValueError('invalid_checkpoint')
        deps=self._dependencies(value)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_dependencies(conn,deps)
            if not await self._locked(conn,id,token): return False
            if not await self._allowed(conn,deps): return False
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

    async def release(self, id, token):
        async with self.pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn,id,token): return False
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",id):
                await self._terminal(conn,id,'delivery_unknown','interrupted_send')
            else:
                await conn.execute("UPDATE arti_requests SET state='queued',token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=$1",id)
            return True

    async def cancel_chat(self, chat_id, topic_id=None, conn=None):
        if conn is None:
            async with self.pool.acquire() as connection, connection.transaction():
                return await self.cancel_chat(chat_id,topic_id,connection)
        rows=await conn.fetch("SELECT id FROM arti_requests WHERE chat_id=$1 AND ($2::bigint IS NULL OR topic_id=$2) AND state IN ('queued','running') ORDER BY seq FOR UPDATE",chat_id,topic_id)
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
            if state in ('queued','running'):
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
