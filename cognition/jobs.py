"""Durable leases. Job payloads contain source ids, never copies of private text."""
import uuid


class JobQueue:
    def __init__(self, pool):
        self.pool = pool

    async def enqueue(self, context_id: int, event_id: int, kind: str = 'interpret', max_attempts: int = 3):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO cognitive_jobs(context_id,event_id,kind,max_attempts)
                SELECT $1,$2,$3,$4 FROM cognitive_events WHERE context_id=$1 AND id=$2 AND suppressed_at IS NULL
                ON CONFLICT(context_id,event_id,kind) DO UPDATE SET kind=EXCLUDED.kind RETURNING cognitive_jobs.id
            """, context_id, event_id, kind, max_attempts)

    async def claim(self, lease_seconds: float = 300, context_id=None):
        if not 1 <= lease_seconds <= 600:
            raise ValueError('Lease duration outside bounds')
        token = uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            # A crash on the last attempt must become dead-letter after lease expiry.
            await conn.execute("""
                UPDATE cognitive_jobs SET status='dead',lease_until=NULL,lease_token=NULL,last_error_code='lease_exhausted'
                WHERE status='running' AND lease_until<=NOW() AND attempts>=max_attempts
            """)
            async with conn.transaction():
                context = await conn.fetchrow("""
                    SELECT c.id FROM cognitive_contexts c
                    CROSS JOIN LATERAL (
                        SELECT j.id,j.available_at FROM cognitive_jobs j JOIN cognitive_events e ON e.context_id=j.context_id AND e.id=j.event_id
                        WHERE j.context_id=c.id AND e.suppressed_at IS NULL
                          AND (NOT c.rebuilding OR j.kind='rebuild')
                          AND j.attempts<j.max_attempts AND j.available_at<=NOW()
                          AND (j.status='pending' OR (j.status='running' AND j.lease_until<=NOW()))
                          AND (j.kind='rebuild' OR NOT EXISTS (
                            SELECT 1 FROM cognitive_jobs older JOIN cognitive_events oe ON oe.context_id=older.context_id AND oe.id=older.event_id
                            WHERE older.context_id=j.context_id AND older.status IN ('pending','running') AND oe.suppressed_at IS NULL
                              AND (older.kind!='replay' OR j.kind='replay')
                              AND (oe.observed_at<e.observed_at OR (oe.observed_at=e.observed_at AND older.id<j.id))
                          ))
                        ORDER BY CASE WHEN j.kind='rebuild' THEN -1 WHEN j.kind='replay' THEN 1 ELSE 0 END,e.observed_at,j.id
                        LIMIT 1
                    ) head
                    WHERE (c.worker_lease_until IS NULL OR c.worker_lease_until<=NOW())
                      AND ($1::bigint IS NULL OR c.id=$1)
                    -- Rank ready head jobs by queue age, not permanent context
                    -- IDs: fresh work in busy old chats cannot starve another
                    -- chat's already eligible job. Context-local order is intact.
                    ORDER BY head.available_at,head.id,c.id FOR UPDATE OF c SKIP LOCKED LIMIT 1
                """,context_id)
                if not context:
                    return None
                row = await conn.fetchrow("""
                    WITH candidate AS (
                        SELECT j.id FROM cognitive_jobs j JOIN cognitive_events e ON e.context_id=j.context_id AND e.id=j.event_id
                        WHERE j.context_id=$3 AND e.suppressed_at IS NULL AND j.attempts<j.max_attempts AND j.available_at<=NOW()
                          AND (NOT (SELECT rebuilding FROM cognitive_contexts WHERE id=$3) OR j.kind='rebuild')
                          AND (j.status='pending' OR (j.status='running' AND j.lease_until<=NOW()))
                          AND (j.kind='rebuild' OR NOT EXISTS (
                            SELECT 1 FROM cognitive_jobs older JOIN cognitive_events oe ON oe.context_id=older.context_id AND oe.id=older.event_id
                            WHERE older.context_id=j.context_id AND older.status IN ('pending','running') AND oe.suppressed_at IS NULL
                              AND (older.kind!='replay' OR j.kind='replay')
                              AND (oe.observed_at<e.observed_at OR (oe.observed_at=e.observed_at AND older.id<j.id))
                          ))
                        ORDER BY CASE WHEN j.kind='rebuild' THEN -1 WHEN j.kind='replay' THEN 1 ELSE 0 END,e.observed_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1
                    )
                    UPDATE cognitive_jobs j SET status='running',attempts=attempts+1,
                        lease_until=NOW()+make_interval(secs=>$1),lease_token=$2
                    FROM candidate c WHERE j.id=c.id RETURNING j.*
                """, float(lease_seconds), token, context['id'])
                if row:
                    await conn.execute("""
                        UPDATE cognitive_contexts SET worker_lease_until=$2,worker_token=$3 WHERE id=$1
                    """, context['id'], row['lease_until'], token)
                return dict(row) if row else None

    async def _release(self, conn, context_id, token):
        await conn.execute("""
            UPDATE cognitive_contexts SET worker_lease_until=NULL,worker_token=NULL
            WHERE id=$1 AND worker_token=$2
        """, context_id, token)

    async def renew(self,job_id,token,lease_seconds=300):
        if not 1 <= lease_seconds <= 600:
            raise ValueError('Lease duration outside bounds')
        async with self.pool.acquire() as conn,conn.transaction():
            cid = await conn.fetchval('SELECT context_id FROM cognitive_jobs WHERE id=$1',job_id)
            if cid is None:
                return False
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            until = await conn.fetchval('''UPDATE cognitive_jobs SET lease_until=NOW()+make_interval(secs=>$3)
                WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>NOW() RETURNING lease_until''',job_id,token,float(lease_seconds))
            if until is None:
                return False
            await conn.execute('UPDATE cognitive_contexts SET worker_lease_until=$3 WHERE id=$1 AND worker_token=$2',cid,token,until)
            return True

    async def finish(self, job_id: int, token: str):
        async with self.pool.acquire() as conn, conn.transaction():
            cid = await conn.fetchval('SELECT context_id FROM cognitive_jobs WHERE id=$1', job_id)
            if cid is None:
                return False
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE', cid)
            row = await conn.fetchrow("""
                UPDATE cognitive_jobs SET status='done',lease_until=NULL,lease_token=NULL,last_error_code=NULL
                WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>NOW() RETURNING context_id
            """, job_id, token)
            if row:
                await self._release(conn,row['context_id'],token)
            return row is not None

    async def fail(self, job_id: int, token: str, code: str):
        if code not in ('timeout', 'provider_unavailable', 'invalid_perception', 'output_truncated', 'stale_revision', 'internal_error'):
            raise ValueError('Use an error category; do not persist provider errors or private payloads')
        async with self.pool.acquire() as conn, conn.transaction():
            cid = await conn.fetchval('SELECT context_id FROM cognitive_jobs WHERE id=$1', job_id)
            if cid is None:
                return False
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE', cid)
            row = await conn.fetchrow("""
                UPDATE cognitive_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead' ELSE 'pending' END,
                    available_at=NOW()+make_interval(secs=>LEAST(300,POWER(2,attempts)::int)),
                    lease_until=NULL,lease_token=NULL,last_error_code=$3
                WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>NOW() RETURNING context_id
            """, job_id, token, code)
            if row:
                await self._release(conn,row['context_id'],token)
            return row is not None
