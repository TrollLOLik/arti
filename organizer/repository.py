"""Private owner-bound objects and conservative durable notification ledger."""
import uuid
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
            CREATE INDEX IF NOT EXISTS arti_organizer_due ON arti_organizer_items(due_at)
                WHERE state='active' AND notification_state IN ('pending','claimed','sending');
            CREATE INDEX IF NOT EXISTS arti_organizer_owner ON arti_organizer_items(owner_id,kind,created_at);
        ''')


def _private(owner,chat):
    if type(owner) is not int or owner<=0 or type(chat) is not int or chat!=owner:
        raise OrganizerError('private_chat_required')


class Repository:
    def __init__(self,pool): self.pool=pool

    async def set_timezone(self,owner,chat,name,*,source_key=None):
        _private(owner,chat); zone(name)
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=160): raise OrganizerError('invalid_source_key')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            if source_key is not None:
                inserted=await conn.fetchval("INSERT INTO arti_organizer_actions VALUES($1,$2,'preferences','timezone') ON CONFLICT DO NOTHING RETURNING 1",owner,source_key)
                if not inserted:
                    return await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1',owner)
            await conn.execute('INSERT INTO arti_organizer_preferences VALUES($1,$2) ON CONFLICT(owner_id) DO UPDATE SET timezone=EXCLUDED.timezone',owner,name)
        return name

    async def get_timezone(self,owner,chat):
        _private(owner,chat)
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1',owner)

    async def by_source(self,owner,chat,source_key):
        _private(owner,chat)
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE owner_id=$1 AND chat_id=$2 AND source_key=$3',owner,chat,source_key)
            return dict(row) if row else None

    async def create(self,owner,chat,kind,title,source_key,*,due_at=None,timezone_name=None):
        _private(owner,chat)
        if kind not in ('todo','event','reminder') or not isinstance(title,str) or not 1<=len(title.strip())<=500:
            raise OrganizerError('invalid_item')
        if not isinstance(source_key,str) or not 1<=len(source_key)<=160: raise OrganizerError('invalid_source_key')
        now=datetime.now(timezone.utc)
        if kind=='todo' and due_at is not None: raise OrganizerError('invalid_item')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
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
        async with self.pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch('''SELECT * FROM arti_organizer_items
                WHERE owner_id=$1 AND chat_id=$2 AND ($3::text IS NULL OR kind=$3)
                AND ($4 OR (state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending')))) ORDER BY due_at NULLS LAST,created_at,id LIMIT $5''',owner,chat,kind,include_closed,limit)]

    async def change(self,owner,chat,id,state,*,expected_kind=None,source_key=None):
        _private(owner,chat)
        if state not in ('active','done','cancelled'): raise OrganizerError('invalid_state')
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=160): raise OrganizerError('invalid_source_key')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
            row=await conn.fetchrow('SELECT * FROM arti_organizer_items WHERE id=$1 AND owner_id=$2 AND chat_id=$3 FOR UPDATE',id,owner,chat)
            if not row or expected_kind is not None and row['kind']!=expected_kind: return None
            if source_key is not None:
                inserted=await conn.fetchval('INSERT INTO arti_organizer_actions VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING 1',owner,source_key,id,state)
                if not inserted: return dict(row)
            if state!='cancelled' and row['kind']!='todo': raise OrganizerError('task_operation_only')
            if state=='active' and row['state']!='active' and await conn.fetchval("SELECT COUNT(*) FROM arti_organizer_items WHERE owner_id=$1 AND state='active' AND (kind='todo' OR notification_state IN ('pending','claimed','sending'))",owner)>=200:
                raise OrganizerError('active_item_limit')
            notification=row['notification_state']
            if state=='cancelled':
                notification='delivery_unknown' if notification in ('sending','delivery_unknown') else 'delivered' if notification=='delivered' else 'cancelled'
            elif row['kind']=='todo': notification='none'
            return dict(await conn.fetchrow('''UPDATE arti_organizer_items SET state=$2,notification_state=$3,
                token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=$1 RETURNING *''',id,state,notification))

    async def claim_due(self,lease_seconds=60):
        if not 1<=lease_seconds<=600: raise OrganizerError('invalid_lease')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("""UPDATE arti_organizer_items SET notification_state=CASE WHEN notification_state='sending' THEN 'delivery_unknown' ELSE 'pending' END,
                token=NULL,lease_until=NULL,updated_at=NOW() WHERE notification_state IN ('claimed','sending') AND lease_until<=NOW()""")
            row=await conn.fetchrow("""SELECT id FROM arti_organizer_items WHERE state='active' AND notification_state='pending' AND due_at<=NOW()
                ORDER BY due_at,id FOR UPDATE SKIP LOCKED LIMIT 1""")
            if not row: return None
            return dict(await conn.fetchrow("""UPDATE arti_organizer_items SET notification_state='claimed',token=$2,
                lease_until=NOW()+make_interval(secs=>$3),updated_at=NOW() WHERE id=$1 RETURNING *""",row['id'],uuid.uuid4().hex,float(lease_seconds)))

    async def begin_send(self,id,token):
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow("""UPDATE arti_organizer_items SET notification_state='sending',updated_at=NOW()
                WHERE id=$1 AND token=$2 AND state='active' AND notification_state='claimed' AND lease_until>NOW() AND due_at<=NOW() RETURNING *""",id,token)
            return dict(row) if row else None

    async def finish_send(self,id,token,*,delivered=False,message_id=None):
        if message_id is not None and (type(message_id) is not int or message_id<=0): raise OrganizerError('invalid_receipt')
        if delivered and message_id is None: raise OrganizerError('missing_receipt')
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("""UPDATE arti_organizer_items SET notification_state=$3,receipt_message_id=$4,
                state=CASE WHEN kind='reminder' AND $3='delivered' THEN 'done' ELSE state END,
                token=NULL,lease_until=NULL,updated_at=NOW() WHERE id=$1 AND token=$2 AND notification_state='sending' AND lease_until>NOW() RETURNING 1""",
                id,token,'delivered' if delivered else 'delivery_unknown',message_id))
