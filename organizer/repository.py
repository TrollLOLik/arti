"""Private owner-bound objects and conservative durable notification ledger."""
import uuid
import contextvars
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from organizer.time import OrganizerError,zone


async def initialize(conn):
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_organizer_schema')::bigint)")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS arti_organizer_preferences (
                owner_id BIGINT PRIMARY KEY CHECK(owner_id>0), timezone TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS arti_organizer_items (
                id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL CHECK(owner_id>0),
                chat_id BIGINT NOT NULL CHECK(chat_id=owner_id),
                kind TEXT NOT NULL CHECK(kind IN ('todo','event','reminder')),
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
                source_key TEXT NOT NULL, due_at TIMESTAMPTZ, timezone TEXT,
                state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','done','cancelled')),
                notification_state TEXT NOT NULL CHECK(notification_state IN
                    ('none','pending','claimed','sending','delivered','delivery_unknown','cancelled')),
                token TEXT, lease_until TIMESTAMPTZ, receipt_message_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(owner_id,source_key),
                CHECK((kind='todo' AND due_at IS NULL) OR (kind IN ('event','reminder') AND due_at IS NOT NULL))
            );
            CREATE TABLE IF NOT EXISTS arti_organizer_actions (
                owner_id BIGINT NOT NULL, source_key TEXT NOT NULL, item_id TEXT NOT NULL,
                operation TEXT NOT NULL, PRIMARY KEY(owner_id,source_key)
            );
            ALTER TABLE arti_organizer_items ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;
            CREATE TABLE IF NOT EXISTS arti_organizer_turns (
                owner_id BIGINT NOT NULL CHECK(owner_id>0), source_key TEXT NOT NULL,
                root_source_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','complete','cancelled','erased')),
                outcome JSONB, item_id TEXT, PRIMARY KEY(owner_id,source_key)
            );
            CREATE INDEX IF NOT EXISTS arti_organizer_turn_roots ON arti_organizer_turns(owner_id,root_source_key);
            CREATE TABLE IF NOT EXISTS arti_organizer_request_links (
                owner_id BIGINT NOT NULL, source_key TEXT NOT NULL, request_id TEXT NOT NULL,
                PRIMARY KEY(owner_id,source_key,request_id)
            );
            CREATE TABLE IF NOT EXISTS arti_organizer_dialogue_state (
                owner_id BIGINT PRIMARY KEY CHECK(owner_id>0), generation BIGINT NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS arti_organizer_reply_refs (
                owner_id BIGINT NOT NULL CHECK(owner_id>0), message_id BIGINT NOT NULL,
                item_id TEXT NOT NULL, version BIGINT NOT NULL, PRIMARY KEY(owner_id,message_id,item_id)
            );
            CREATE TABLE IF NOT EXISTS arti_organizer_erased_sources (
                owner_id BIGINT NOT NULL, source_key TEXT NOT NULL, PRIMARY KEY(owner_id,source_key)
            );
            CREATE INDEX IF NOT EXISTS arti_organizer_due ON arti_organizer_items(due_at)
                WHERE state='active' AND notification_state IN ('pending','claimed','sending');
            CREATE INDEX IF NOT EXISTS arti_organizer_owner ON arti_organizer_items(owner_id,kind,created_at);
        ''')


def _private(owner,chat):
    if type(owner) is not int or owner<=0 or type(chat) is not int or chat!=owner:
        raise OrganizerError('private_chat_required')


class Repository:
    def __init__(self,pool):
        self.pool=pool
        self._connection=contextvars.ContextVar('organizer_connection',default=None)

    @asynccontextmanager
    async def connection(self):
        pinned=self._connection.get()
        if pinned is not None:
            yield pinned
        else:
            async with self.pool.acquire() as conn:
                yield conn

    @asynccontextmanager
    async def transaction(self,owner,chat):
        _private(owner,chat)
        async with self.connection() as conn,conn.transaction():
            # Context -> organizer -> request is also the erasure lock order.
            if await conn.fetchval("SELECT to_regclass('cognitive_contexts')"):
                contexts=await conn.fetch("SELECT * FROM cognitive_contexts WHERE chat_id=$1 AND persona_id='arti' AND mode='default' AND topic_id<0 AND scene_id='' ORDER BY id FOR SHARE",chat)
                from cognition.runtime import CURRENT_TURN
                turn=CURRENT_TURN.get()
                if turn is not None:
                    context=next((row for row in contexts if row['id']==turn.context_id),None)
                    if not context or context['rebuilding'] or context['suppression_epoch']!=turn.epoch:
                        raise OrganizerError('request_cancelled')
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            from bot.request_runtime import CURRENT_REQUEST
            job=CURRENT_REQUEST.get()
            if job is not None:
                valid=await conn.fetchval("SELECT 1 FROM arti_requests WHERE id=$1 AND token=$2 AND chat_id=$3 AND topic_id<0 AND kind='text' AND state='running' AND lease_until>NOW() AND deadline_at>NOW() FOR UPDATE",job['id'],job['token'],chat)
                if not valid: raise OrganizerError('request_cancelled')
            token=self._connection.set(conn)
            try: yield conn
            finally: self._connection.reset(token)

    async def _live_source(self,conn,owner,source_key):
        if not source_key: return
        if await conn.fetchval('SELECT 1 FROM arti_organizer_erased_sources WHERE owner_id=$1 AND source_key=$2',owner,source_key):
            raise OrganizerError('source_erased')
        if await conn.fetchval("SELECT 1 FROM arti_organizer_turns WHERE owner_id=$1 AND source_key=$2 AND state IN ('cancelled','erased')",owner,source_key):
            raise OrganizerError('request_cancelled')
        if await conn.fetchval("SELECT to_regclass('cognitive_events')"):
            if await conn.fetchval('''SELECT 1 FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
                WHERE c.persona_id='arti' AND c.chat_id=$1 AND c.topic_id<0 AND c.mode='default' AND c.scene_id=''
                AND e.owner_id=$1 AND e.source_id=$2 AND (e.suppressed_at IS NOT NULL OR e.id<=c.history_after_event_id)''',owner,source_key+':user'):
                raise OrganizerError('request_cancelled')
        if await conn.fetchval("SELECT to_regclass('material_source_tombstones')"):
            from materials.types import context_identity
            identity=context_identity('arti',owner,-1,'default','')
            if await conn.fetchval('SELECT 1 FROM material_source_tombstones WHERE owner_id=$1 AND source_id=$2 AND scope_key=$3',owner,source_key+':user',identity):
                raise OrganizerError('source_erased')


    async def get(self,owner,chat,id,*,kind=None):
        _private(owner,chat)
        async with self.connection() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE owner_id=$1 AND chat_id=$2 AND id=$3 AND ($4::text IS NULL OR kind=$4)',owner,chat,id,kind)
            return dict(row) if row else None

    async def resolve(self,owner,chat,target,*,kind=None,reply_to_id=None):
        _private(owner,chat)
        async with self.connection() as conn:
            if target.casefold() in ('это','эту задачу','это напоминание','это событие','её','его'):
                if type(reply_to_id) is not int: return []
                rows=await conn.fetch('SELECT i.*,r.version AS expected_version FROM arti_organizer_reply_refs r JOIN arti_organizer_items i ON i.id=r.item_id AND i.owner_id=r.owner_id WHERE r.owner_id=$1 AND r.message_id=$2 AND i.chat_id=$1 AND ($3::text IS NULL OR i.kind=$3)',owner,reply_to_id,kind)
            else:
                rows=await conn.fetch('SELECT *,version AS expected_version FROM arti_organizer_items WHERE owner_id=$1 AND chat_id=$1 AND ($2::text IS NULL OR kind=$2) AND (id=$3 OR lower(title)=lower($3)) ORDER BY id LIMIT 21',owner,kind,target.strip(' «»"'))
            return [dict(row) for row in rows]

    async def record_reply(self,owner,chat,message_id,items,*,source_key=None,generation=None):
        _private(owner,chat)
        if type(message_id) is not int or message_id<=0: return
        async with self.connection() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            if generation is not None:
                current=await conn.fetchval('SELECT generation FROM arti_organizer_dialogue_state WHERE owner_id=$1',owner) or 0
                if current!=generation: return
            if source_key and await conn.fetchval("SELECT 1 FROM arti_organizer_turns WHERE owner_id=$1 AND source_key=$2 AND state IN ('cancelled','erased')",owner,source_key): return
            for item in items:
                # A reply pins what was shown, not a newer unseen version.
                await conn.execute('INSERT INTO arti_organizer_reply_refs(owner_id,message_id,item_id,version) SELECT $1,$2,id,$4 FROM arti_organizer_items WHERE owner_id=$1 AND chat_id=$1 AND id=$3 ON CONFLICT DO NOTHING',owner,message_id,item['id'],item['version'])


    async def set_timezone(self,owner,chat,name,*,source_key=None):
        _private(owner,chat); zone(name)
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=160): raise OrganizerError('invalid_source_key')
        async with self.connection() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            if source_key is not None:
                inserted=await conn.fetchval("INSERT INTO arti_organizer_actions VALUES($1,$2,'preferences','timezone') ON CONFLICT DO NOTHING RETURNING 1",owner,source_key)
                if not inserted:
                    return await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1',owner)
            await conn.execute('INSERT INTO arti_organizer_preferences VALUES($1,$2) ON CONFLICT(owner_id) DO UPDATE SET timezone=EXCLUDED.timezone',owner,name)
        return name

    async def get_timezone(self,owner,chat):
        _private(owner,chat)
        async with self.connection() as conn:
            return await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1',owner)

    async def by_source(self,owner,chat,source_key):
        _private(owner,chat)
        async with self.connection() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE owner_id=$1 AND chat_id=$2 AND source_key=$3',owner,chat,source_key)
            return dict(row) if row else None

    async def create(self,owner,chat,kind,title,source_key,*,due_at=None,timezone_name=None):
        _private(owner,chat)
        if kind not in ('todo','event','reminder') or not isinstance(title,str) or not 1<=len(title.strip())<=500:
            raise OrganizerError('invalid_item')
        if not isinstance(source_key,str) or not 1<=len(source_key)<=160: raise OrganizerError('invalid_source_key')
        now=datetime.now(timezone.utc)
        if kind=='todo' and due_at is not None: raise OrganizerError('invalid_item')
        async with self.connection() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            await self._live_source(conn,owner,source_key)
            existing=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE owner_id=$1 AND source_key=$2',owner,source_key)
            if existing: return dict(existing)
            if kind!='todo' and (not isinstance(due_at,datetime) or due_at.tzinfo is None or not now<due_at<=now+timedelta(days=365)):
                raise OrganizerError('schedule_out_of_range')
            if timezone_name is not None and (not isinstance(timezone_name,str) or len(timezone_name)>100): raise OrganizerError('invalid_timezone')
            if await conn.fetchval("SELECT COUNT(*) FROM arti_organizer_items WHERE owner_id=$1 AND state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending'))",owner)>=200:
                raise OrganizerError('active_item_limit')
            row=await conn.fetchrow('''INSERT INTO arti_organizer_items
                (id,owner_id,chat_id,kind,title,source_key,due_at,timezone,notification_state)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *''',uuid.uuid4().hex[:12],owner,chat,kind,title.strip(),source_key,due_at,timezone_name,'none' if kind=='todo' else 'pending')
            return dict(row)

    async def list(self,owner,chat,kind=None,*,include_closed=False,limit=30):
        _private(owner,chat)
        if kind not in (None,'todo','event','reminder'): raise OrganizerError('invalid_kind')
        if type(limit) is not int or not 1<=limit<=100: raise OrganizerError('invalid_limit')
        async with self.connection() as conn:
            return [dict(r) for r in await conn.fetch('''SELECT * FROM arti_organizer_items
                WHERE owner_id=$1 AND chat_id=$2 AND ($3::text IS NULL OR kind=$3)
                AND ($4 OR (state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending')))) ORDER BY due_at NULLS LAST,created_at,id LIMIT $5''',owner,chat,kind,include_closed,limit)]

    async def change(self,owner,chat,id,state,*,expected_kind=None,source_key=None,expected_version=None):
        _private(owner,chat)
        if state not in ('active','done','cancelled'): raise OrganizerError('invalid_state')
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=160): raise OrganizerError('invalid_source_key')
        async with self.connection() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            await self._live_source(conn,owner,source_key)
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE id=$1 AND owner_id=$2 AND chat_id=$3 FOR UPDATE',id,owner,chat)
            if not row or expected_kind is not None and row['kind']!=expected_kind: return None
            if source_key is not None:
                inserted=await conn.fetchval('INSERT INTO arti_organizer_actions VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING 1',owner,source_key,id,state)
                if not inserted: return dict(row)
            if expected_version is not None and row['version']!=expected_version: raise OrganizerError('stale_item')
            if state!='cancelled' and row['kind']!='todo': raise OrganizerError('task_operation_only')
            if state=='active' and row['state']!='active' and await conn.fetchval("SELECT COUNT(*) FROM arti_organizer_items WHERE owner_id=$1 AND state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending'))",owner)>=200:
                raise OrganizerError('active_item_limit')
            notification=row['notification_state']
            if state=='cancelled':
                notification='delivery_unknown' if notification in ('sending','delivery_unknown') else 'delivered' if notification=='delivered' else 'cancelled'
            elif row['kind']=='todo': notification='none'
            return dict(await conn.fetchrow('''UPDATE arti_organizer_items SET state=$2,notification_state=$3,
                token=NULL,lease_until=NULL,version=version+1,updated_at=NOW() WHERE id=$1 RETURNING *''',id,state,notification))

    async def edit(self,owner,chat,id,*,title=None,due_at=None,timezone_name=None,expected_kind=None,expected_version=None,source_key=None):
        _private(owner,chat)
        if title is not None and (not isinstance(title,str) or not 1<=len(title.strip())<=500): raise OrganizerError('invalid_item')
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=160): raise OrganizerError('invalid_source_key')
        async with self.connection() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            await self._live_source(conn,owner,source_key)
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE id=$1 AND owner_id=$2 AND chat_id=$3 FOR UPDATE',id,owner,chat)
            if not row or expected_kind is not None and row['kind']!=expected_kind: return None
            if source_key and await conn.fetchval('SELECT 1 FROM arti_organizer_actions WHERE owner_id=$1 AND source_key=$2',owner,source_key): return dict(row)
            if expected_version is not None and row['version']!=expected_version: raise OrganizerError('stale_item')
            if row['state']!='active': raise OrganizerError('item_closed')
            if due_at is not None:
                now=datetime.now(timezone.utc)
                if row['kind']=='todo': raise OrganizerError('task_has_no_schedule')
                if row['notification_state'] not in ('pending','claimed'): raise OrganizerError('notification_terminal')
                if not isinstance(due_at,datetime) or due_at.tzinfo is None or not now<due_at<=now+timedelta(days=365): raise OrganizerError('schedule_out_of_range')
                if timezone_name is not None and (not isinstance(timezone_name,str) or len(timezone_name)>100): raise OrganizerError('invalid_timezone')
            elif title is None: raise OrganizerError('invalid_item')
            if row['kind']!='todo' and row['notification_state'] not in ('pending','claimed'): raise OrganizerError('notification_terminal')
            if source_key:
                await conn.execute('INSERT INTO arti_organizer_actions VALUES($1,$2,$3,$4)',owner,source_key,id,'reschedule' if due_at is not None else 'rename')
            return dict(await conn.fetchrow("""UPDATE arti_organizer_items SET title=COALESCE($2,title),due_at=COALESCE($3,due_at),
                timezone=CASE WHEN $3::timestamptz IS NULL THEN timezone ELSE $4 END,
                notification_state=CASE WHEN kind='todo' THEN 'none' ELSE 'pending' END,
                token=NULL,lease_until=NULL,version=version+1,updated_at=NOW() WHERE id=$1 RETURNING *""",id,title.strip() if title else None,due_at,timezone_name))

    async def claim_due(self,lease_seconds=60):
        if not 1<=lease_seconds<=600: raise OrganizerError('invalid_lease')
        async with self.connection() as conn,conn.transaction():
            await conn.execute("""UPDATE arti_organizer_items SET notification_state=CASE WHEN notification_state='sending' THEN 'delivery_unknown' ELSE 'pending' END,
                token=NULL,lease_until=NULL,updated_at=NOW() WHERE notification_state IN ('claimed','sending') AND lease_until<=NOW()""")
            row=await conn.fetchrow("""SELECT id FROM arti_organizer_items WHERE state='active' AND notification_state='pending' AND due_at<=NOW()
                ORDER BY due_at,id FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row: return None
            return dict(await conn.fetchrow("""UPDATE arti_organizer_items SET notification_state='claimed',token=$2,
                lease_until=NOW()+make_interval(secs=>$3),updated_at=NOW() WHERE id=$1 RETURNING *""",row['id'],uuid.uuid4().hex,float(lease_seconds)))

    async def begin_send(self,id,token):
        async with self.connection() as conn:
            row=await conn.fetchrow("""UPDATE arti_organizer_items SET notification_state='sending',updated_at=NOW()
                WHERE id=$1 AND token=$2 AND state='active' AND notification_state='claimed' AND lease_until>NOW() AND due_at<=NOW() RETURNING *""",id,token)
            return dict(row) if row else None

    async def finish_send(self,id,token,*,delivered=False,message_id=None):
        if message_id is not None and (type(message_id) is not int or message_id<=0): raise OrganizerError('invalid_receipt')
        if delivered and message_id is None: raise OrganizerError('missing_receipt')
        async with self.connection() as conn:
            return bool(await conn.fetchval("""UPDATE arti_organizer_items SET notification_state=$3,receipt_message_id=$4,
                state=CASE WHEN kind='reminder' AND $3='delivered' THEN 'done' ELSE state END,
                token=NULL,lease_until=NULL,version=version+1,updated_at=NOW() WHERE id=$1 AND token=$2 AND notification_state='sending' AND lease_until>NOW() RETURNING 1""",
                id,token,'delivered' if delivered else 'delivery_unknown',message_id))
