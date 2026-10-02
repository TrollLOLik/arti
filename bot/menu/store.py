"""One durable, user-owned panel per chat/topic; native updates are consumed once."""
import json
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from materials.types import MaterialError, canonical

# Reference-counted per-pool gates disappear after the last waiter. Reserve
# connections for reads/mutations performed while a session lock is held.
_LOCK_GATES = {}


class MenuStore:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def locked(self, chat_id, topic_id, user_id):
        # Session lock covers network edits too, without holding a transaction open.
        key = f'arti-menu:{chat_id}:{topic_id}:{user_id}'
        size = self.pool.get_max_size()
        if size<2:
            raise MaterialError('menu_pool_capacity')
        pool_key = id(self.pool)
        gate = _LOCK_GATES.setdefault(pool_key,dict(semaphore=asyncio.Semaphore(max(1,size//3)),users=0))
        gate['users'] += 1
        try:
            async with gate['semaphore'], self.pool.acquire() as conn:
                await conn.execute('SELECT pg_advisory_lock(hashtextextended($1,0))', key)
                try:
                    yield self
                finally:
                    await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', key)
        finally:
            gate['users'] -= 1
            if gate['users']==0:
                _LOCK_GATES.pop(pool_key,None)

    def decode(self, row):
        if row is None:
            return None
        result = dict(row)
        for key in ('state', 'actions'):
            if isinstance(result[key], str):
                result[key] = json.loads(result[key])
        return result

    async def get(self, chat_id, topic_id, user_id):
        async with self.pool.acquire() as conn:
            return self.decode(await conn.fetchrow(
                'SELECT * FROM arti_menu_sessions WHERE chat_id=$1 AND topic_id=$2 AND user_id=$3',
                chat_id, topic_id, user_id))

    async def by_id(self, id):
        async with self.pool.acquire() as conn:
            return self.decode(await conn.fetchrow('SELECT * FROM arti_menu_sessions WHERE id=$1', id))

    async def public_entry(self,chat_id,topic_id,scope_key):
        async with self.pool.acquire() as conn:
            return self.decode(await conn.fetchrow('''SELECT * FROM arti_menu_sessions
                WHERE chat_id=$1 AND topic_id=$2 AND scope_key=$3 AND screen='home'
                AND status='active' AND state->>'shared'='true' AND expires_at>NOW()
                ORDER BY updated_at DESC LIMIT 1''',chat_id,topic_id,scope_key))

    async def open(self, chat_id, topic_id, user_id, scope_key):
        async with self.pool.acquire() as conn:
            # Clear expired drafts, including retained Telegram file handles.
            await conn.execute("DELETE FROM arti_menu_sessions WHERE expires_at<NOW()-INTERVAL '1 day'")
            row = await conn.fetchrow('''INSERT INTO arti_menu_sessions(id,chat_id,topic_id,user_id,scope_key)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(chat_id,topic_id,user_id)
                DO UPDATE SET scope_key=EXCLUDED.scope_key,state='{}',actions='{}',screen='home',content_hash=NULL,
                expires_at=NOW()+INTERVAL '1 day' RETURNING *''', uuid.uuid4().hex, chat_id, topic_id, user_id, scope_key)
            return self.decode(row)

    async def save(self, row, **changes):
        allowed = {'message_id', 'revision', 'status', 'screen', 'state', 'actions', 'content_hash', 'scope_key'}
        if not changes or set(changes)-allowed:
            raise ValueError('Invalid panel fields')
        values = []
        setters = []
        for key, value in changes.items():
            if key in ('state', 'actions'):
                value = canonical(value)
                if len(value.encode()) > 100000:
                    raise MaterialError('menu_state_budget')
            values.append(value)
            setters.append(f'{key}=${len(values)+1}' + ('::jsonb' if key in ('state', 'actions') else ''))
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow('UPDATE arti_menu_sessions SET '+','.join(setters)+
                ",updated_at=NOW(),expires_at=NOW()+INTERVAL '1 day' WHERE id=$1 RETURNING *", row['id'], *values)
        row.update(self.decode(updated))

    async def consume(self, row, request_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM arti_menu_requests WHERE session_id=$1 AND created_at<NOW()-INTERVAL '2 days'",row['id'])
            return bool(await conn.fetchval('''INSERT INTO arti_menu_requests(session_id,request_id)
                VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING TRUE''', row['id'], request_id))

    def action(self, row, token, *, user_id, chat_id, topic_id, message_id, scope_key):
        if (row['user_id'], row['chat_id'], row['topic_id'], row['message_id'], row['scope_key']) != (
                user_id, chat_id, topic_id, message_id, scope_key):
            raise MaterialError('menu_wrong_owner_or_scope')
        if row['expires_at'] <= datetime.now(timezone.utc) or row['status'] != 'active':
            raise MaterialError('menu_expired')
        value = row['actions'].get(token)
        if not value:
            raise MaterialError('menu_stale_button')
        return value
