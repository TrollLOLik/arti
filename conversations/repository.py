"""PostgreSQL conversation boundary. No AI calls or transport sends here."""
import uuid

from .types import ContextRef, ConversationError, identifier, message_input, page_size, profile, user_id


def public_conversation(row):
    return {key: row[key] for key in ('id', 'kind', 'title', 'created_at', 'updated_at', 'role')} | dict(
        message_count=row['message_seq'], unread_count=0, last_message=None)


def public_message(row):
    return {key: row[key] for key in ('id', 'conversation_id', 'seq', 'author_id', 'author_kind', 'text',
                                    'client_message_id', 'reply_to_id', 'created_at')}


class Repository:
    def __init__(self, pool):
        self.pool = pool

    async def ensure_user(self, verified_profile):
        actor = profile(verified_profile)
        async with self.pool.acquire() as conn, conn.transaction():
            # Updating the actor row also serializes simultaneous first logins.
            await conn.execute('''INSERT INTO app_users(id,first_name,username) VALUES($1,$2,$3)
                ON CONFLICT(id) DO UPDATE SET first_name=EXCLUDED.first_name,username=EXCLUDED.username''',
                actor['id'], actor['first_name'], actor['username'])
            cid = await conn.fetchval("SELECT id FROM app_conversations WHERE owner_id=$1 AND kind='arti_private' AND persona_id='arti'", actor['id'])
            if cid is None:
                cid = uuid.uuid4()
                await conn.execute("INSERT INTO app_conversations(id,kind,owner_id,title) VALUES($1,'arti_private',$2,'Арти')", cid, actor['id'])
                await conn.execute("INSERT INTO app_memberships(conversation_id,user_id,role) VALUES($1,$2,'owner')", cid, actor['id'])
                await conn.execute("INSERT INTO app_context_bindings(conversation_id,kind,telegram_chat_id) VALUES($1,'telegram_private',$2)", cid, actor['id'])
        return cid

    async def list(self, actor_id, limit=50):
        actor_id, limit = user_id(actor_id), page_size(limit)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT c.*,m.role FROM app_conversations c JOIN app_memberships m ON m.conversation_id=c.id
                WHERE m.user_id=$1 AND m.state='active' AND (c.kind!='arti_private' OR c.owner_id=$1)
                ORDER BY c.created_at,c.id LIMIT $2''', actor_id, limit)
        return [public_conversation(row) for row in rows]

    async def get(self, actor_id, conversation_id):
        async with self.pool.acquire() as conn:
            row = await self._member(conn, user_id(actor_id), identifier(conversation_id))
        return public_conversation(row)

    async def _member(self, conn, actor_id, cid):
        row = await conn.fetchrow('''SELECT c.*,m.role,m.visible_from_seq FROM app_conversations c
            JOIN app_memberships m ON m.conversation_id=c.id WHERE c.id=$1 AND m.user_id=$2 AND m.state='active' ''', cid, actor_id)
        if row is None:
            # Same result for nonexistent and unauthorized resources.
            raise ConversationError('not_found', 404)
        if row['kind'] == 'arti_private' and row['owner_id'] != actor_id:
            raise ConversationError('not_found', 404)
        return row

    async def context(self, actor_id, conversation_id):
        async with self.pool.acquire() as conn:
            member = await self._member(conn, user_id(actor_id), identifier(conversation_id))
            ref = await conn.fetchrow('SELECT * FROM app_context_bindings WHERE conversation_id=$1', member['id'])
        if not ref:
            raise ConversationError('context_unavailable', 409)
        result = ContextRef(ref['kind'], telegram_chat_id=ref['telegram_chat_id'], native_id=ref['native_id'])
        if member['kind'] == 'arti_private' and result.telegram_chat_id != actor_id:
            raise ConversationError('context_unavailable', 409)
        return result

    async def messages(self, actor_id, conversation_id, *, before=None, limit=50):
        actor_id, cid, limit = user_id(actor_id), identifier(conversation_id), page_size(limit)
        if before is not None and (type(before) is not int or not 0 < before < 2**63):
            raise ConversationError('invalid_cursor')
        async with self.pool.acquire() as conn:
            # Authorization and rows share one statement/snapshot. A later revoked
            # request cannot reuse a cached membership or read an unrelated scope.
            await self._member(conn, actor_id, cid)
            rows = await conn.fetch('''SELECT msg.* FROM app_messages msg JOIN app_memberships m
                ON m.conversation_id=msg.conversation_id JOIN app_conversations c ON c.id=msg.conversation_id
                WHERE msg.conversation_id=$1 AND m.user_id=$2 AND m.state='active'
                AND (c.kind!='arti_private' OR c.owner_id=$2) AND msg.seq>=m.visible_from_seq
                AND msg.suppressed_at IS NULL AND ($3::bigint IS NULL OR msg.seq<$3)
                ORDER BY msg.seq DESC LIMIT $4''', cid, actor_id, before, limit+1)
        more = len(rows) > limit
        selected = rows[:limit]
        return dict(items=[public_message(r) for r in reversed(selected)], next_before=selected[-1]['seq'] if more else None)

    async def append_user_message(self, actor_id, conversation_id, text, client_message_id, *, reply_to_id=None):
        """Internal primitive. HTTP sending stays disabled until MA03–MA04 intake exists."""
        actor_id, cid = user_id(actor_id), identifier(conversation_id)
        key, reply, digest = message_input(text, client_message_id, reply_to_id)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE', cid)
            member = await self._member(conn, actor_id, cid)
            old = await conn.fetchrow('''SELECT * FROM app_messages
                WHERE conversation_id=$1 AND author_id=$2 AND client_message_id=$3''', cid, actor_id, key)
            if old:
                if old['payload_hash'] != digest:
                    raise ConversationError('idempotency_conflict', 409)
                if old['suppressed_at'] is not None or old['seq'] < member['visible_from_seq']:
                    raise ConversationError('message_unavailable', 410)
                return public_message(old)
            if reply is not None and not await conn.fetchval('''SELECT 1 FROM app_messages WHERE conversation_id=$1
                AND id=$2 AND seq>=$3 AND suppressed_at IS NULL''', cid, reply, member['visible_from_seq']):
                raise ConversationError('invalid_reply')
            seq = await conn.fetchval('''UPDATE app_conversations SET message_seq=message_seq+1,updated_at=now()
                WHERE id=$1 RETURNING message_seq''', cid)
            mid = uuid.uuid4()
            row = await conn.fetchrow('''INSERT INTO app_messages(id,conversation_id,seq,author_id,author_kind,text,
                client_message_id,payload_hash,source_key,reply_to_id) VALUES($1,$2,$3,$4,'user',$5,$6,$7,$8,$9) RETURNING *''',
                mid, cid, seq, actor_id, text, key, digest, f'miniapp:{actor_id}:{key}:user', reply)
            await self._event(conn, cid, 'message.created', mid)
        return public_message(row)

    async def _event(self, conn, cid, kind, resource_id=None):
        version = await conn.fetchrow('''UPDATE app_conversations SET event_seq=event_seq+1
            WHERE id=$1 RETURNING event_seq,access_revision''', cid)
        eid = uuid.uuid4()
        await conn.execute('''INSERT INTO app_events(id,conversation_id,seq,kind,resource_id,access_revision)
            VALUES($1,$2,$3,$4,$5,$6)''', eid, cid, version['event_seq'], kind, resource_id, version['access_revision'])
        await conn.execute('INSERT INTO app_outbox(event_id) VALUES($1)', eid)

    async def create_room(self, actor_id, title, creation_key):
        """Domain foundation only: invites/room HTTP become available in MA06."""
        actor_id, key = user_id(actor_id), identifier(creation_key)
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
            raise ConversationError('invalid_title')
        title = title.strip()
        async with self.pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval('SELECT id FROM app_users WHERE id=$1 FOR UPDATE', actor_id):
                raise ConversationError('not_found', 404)
            old = await conn.fetchrow('SELECT * FROM app_conversations WHERE owner_id=$1 AND creation_key=$2', actor_id, key)
            if old:
                if old['kind'] != 'room' or old['title'] != title:
                    raise ConversationError('idempotency_conflict', 409)
                return old['id']
            cid = uuid.uuid4()
            await conn.execute("INSERT INTO app_conversations(id,kind,owner_id,title,creation_key) VALUES($1,'room',$2,$3,$4)", cid, actor_id, title, key)
            await conn.execute("INSERT INTO app_memberships(conversation_id,user_id,role) VALUES($1,$2,'owner')", cid, actor_id)
            await conn.execute("INSERT INTO app_context_bindings(conversation_id,kind,native_id) VALUES($1,'native_room',$1)", cid)
            from .native import allocate
            await allocate(conn, cid)
        return cid

    async def set_member(self, actor_id, conversation_id, member_id, *, active):
        """Owner-only primitive; never accept membership identity from a public chat POST."""
        actor_id, member_id, cid = user_id(actor_id), user_id(member_id), identifier(conversation_id)
        if type(active) is not bool:
            raise ConversationError('invalid_membership')
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE', cid)
            owner = await self._member(conn, actor_id, cid)
            if owner['kind'] != 'room' or owner['role'] != 'owner' or member_id == owner['owner_id']:
                raise ConversationError('forbidden', 403)
            if not await conn.fetchval('SELECT 1 FROM app_users WHERE id=$1', member_id):
                raise ConversationError('not_found', 404)
            old = await conn.fetchrow('SELECT * FROM app_memberships WHERE conversation_id=$1 AND user_id=$2', cid, member_id)
            if old and (old['state'] == 'active') == active:
                return
            if active:
                await conn.execute('''INSERT INTO app_memberships(conversation_id,user_id,role,visible_from_seq)
                    VALUES($1,$2,'member',$3) ON CONFLICT(conversation_id,user_id) DO UPDATE
                    SET state='active',role='member',visible_from_seq=$3,joined_at=now()''', cid, member_id, owner['message_seq']+1)
            elif old:
                await conn.execute("UPDATE app_memberships SET state='removed' WHERE conversation_id=$1 AND user_id=$2", cid, member_id)
            else:
                return
            await conn.execute('UPDATE app_conversations SET access_revision=access_revision+1 WHERE id=$1', cid)
            await self._event(conn, cid, 'membership.changed')
