import asyncio
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import unittest
import uuid
import time
import asyncpg
from aiohttp.test_utils import TestClient, TestServer

from conversations.repository import Repository
from conversations.schema import ensure_schema
from conversations.types import ConversationError, ContextRef, private_context
from miniapp.auth import AuthError
from miniapp.sessions import SessionStore, csrf, digest
from tests.conversations.database import isolated_database
from tests.miniapp.test_auth import TOKEN, signed
from miniapp.server import create_app, Settings


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'requires disposable PostgreSQL: ARTI_TEST_DB=1')
class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.repo = Repository(self.pool)
        self.cid = await self.repo.ensure_user(dict(id=42, first_name='Тест'))
        await self.repo.ensure_user(dict(id=43, first_name='Другой'))
        await self.repo.ensure_user(dict(id=44, first_name='Третий'))

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    async def test_migration_is_idempotent_and_first_login_race_creates_one_chat(self):
        await ensure_schema(self.pool)
        ids = await asyncio.gather(*(self.repo.ensure_user(dict(id=45, first_name='Одновременно')) for _ in range(6)))
        self.assertEqual(1, len(set(ids)))
        self.assertEqual(1, len(await self.repo.list(45)))
        self.assertEqual(42, (await self.repo.context(42, self.cid)).telegram_chat_id)
        async with self.pool.acquire() as conn:
            self.assertEqual(1, await conn.fetchval("SELECT count(*) FROM cognitive_schema_migrations WHERE name='055_miniapp_conversations.sql'"))

    async def test_foreign_ids_rejected_for_read_write_context(self):
        # A stray membership must not turn a private conversation into a shared one.
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO app_memberships(conversation_id,user_id,role) VALUES($1,43,'member')", self.cid)
        self.assertNotIn(self.cid, [row['id'] for row in await self.repo.list(43)])
        for fn in (lambda: self.repo.get(43, self.cid), lambda: self.repo.messages(43, self.cid),
                   lambda: self.repo.context(43, self.cid), lambda: self.repo.append_user_message(43, self.cid, 'Чужое', uuid.uuid4())):
            with self.assertRaises(ConversationError) as caught:
                await fn()
            self.assertEqual(404, caught.exception.status)

    async def test_duplicate_submission_and_conflict_are_atomic(self):
        key = uuid.uuid4()
        rows = await asyncio.gather(*(self.repo.append_user_message(42, self.cid, 'Один источник', key) for _ in range(5)))
        self.assertEqual(1, len({r['id'] for r in rows}))
        with self.assertRaises(ConversationError) as caught:
            await self.repo.append_user_message(42, self.cid, 'Изменённый текст', key)
        self.assertEqual('idempotency_conflict', caught.exception.code)
        async with self.pool.acquire() as conn:
            for table in ('app_messages', 'app_events', 'app_outbox'):
                self.assertEqual(1, await conn.fetchval('SELECT count(*) FROM '+table))
            self.assertEqual(1, await conn.fetchval('SELECT message_seq FROM app_conversations WHERE id=$1', self.cid))
        # A fresh repository sees the committed result; no process-local dedup map.
        self.assertEqual(rows[0]['id'], (await Repository(self.pool).append_user_message(42, self.cid, 'Один источник', key))['id'])

    async def test_failed_outbox_insert_rolls_back_message_and_sequence(self):
        # A real constraint failure after message insertion simulates transaction failure.
        async with self.pool.acquire() as conn:
            await conn.execute("ALTER TABLE app_outbox ADD CONSTRAINT reject_test_event CHECK (false)")
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.repo.append_user_message(42, self.cid, 'Не подтверждать', uuid.uuid4())
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM app_messages'))
            self.assertEqual(0, await conn.fetchval('SELECT message_seq FROM app_conversations WHERE id=$1', self.cid))

    async def test_pagination_order_reply_validation_and_suppression(self):
        writes = await asyncio.gather(*(self.repo.append_user_message(42, self.cid, str(i), uuid.uuid4()) for i in range(12)))
        self.assertEqual(list(range(1, 13)), sorted(r['seq'] for r in writes))
        before, found = None, []
        while True:
            page = await self.repo.messages(42, self.cid, before=before, limit=5)
            found = page['items']+found
            before = page['next_before']
            if before is None:
                break
        self.assertEqual(list(range(1, 13)), [r['seq'] for r in found])
        foreign = await self.repo.append_user_message(43, (await self.repo.list(43))[0]['id'], 'Иной чат', uuid.uuid4())
        with self.assertRaises(ConversationError):
            await self.repo.append_user_message(42, self.cid, 'Недопустимая ссылка', uuid.uuid4(), reply_to_id=foreign['id'])
        hidden = found[0]
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE app_messages SET suppressed_at=now() WHERE id=$1', hidden['id'])
        self.assertNotIn(hidden['id'], [r['id'] for r in (await self.repo.messages(42, self.cid))['items']])
        with self.assertRaises(ConversationError) as caught:
            await self.repo.append_user_message(42, self.cid, hidden['text'], hidden['client_message_id'])
        self.assertEqual(410, caught.exception.status)

    async def test_native_room_membership_visibility_and_revocation(self):
        room = await self.repo.create_room(42, 'Комната', uuid.uuid4())
        ref = await self.repo.context(42, room)
        self.assertEqual(('native_room', room, None), (ref.kind, ref.native_id, ref.telegram_chat_id))
        await self.repo.append_user_message(42, room, 'До приглашения', uuid.uuid4())
        await self.repo.set_member(42, room, 43, active=True)
        self.assertEqual([], (await self.repo.messages(43, room))['items'])
        visible = await self.repo.append_user_message(43, room, 'После приглашения', uuid.uuid4())
        self.assertEqual([visible['id']], [r['id'] for r in (await self.repo.messages(43, room))['items']])
        with self.assertRaises(ConversationError):
            await self.repo.set_member(43, room, 44, active=True)
        await self.repo.set_member(42, room, 43, active=False)
        with self.assertRaises(ConversationError):
            await self.repo.messages(43, room)
        with self.assertRaises(ConversationError):
            await self.repo.append_user_message(43, room, 'После отзыва', uuid.uuid4())
        self.assertNotIn(room, [r['id'] for r in await self.repo.list(43)])
        await self.repo.set_member(42, room, 43, active=True)
        self.assertEqual([], (await self.repo.messages(43, room))['items'])
        with self.assertRaises(ConversationError) as caught:
            await self.repo.append_user_message(43, room, visible['text'], visible['client_message_id'])
        self.assertEqual(410, caught.exception.status)

    async def test_sessions_survive_store_restart_expire_and_revoke(self):
        store = SessionStore(self.pool)
        token = await store.create(dict(id=42), datetime.now(timezone.utc)+timedelta(minutes=5))
        self.assertEqual(42, (await SessionStore(self.pool).read(token))['user']['id'])
        async with self.pool.acquire() as conn:
            hashes = await conn.fetch('SELECT token_hash FROM app_sessions')
            self.assertEqual([digest(token)], [r['token_hash'] for r in hashes])
        await store.revoke(token)
        with self.assertRaises(AuthError):
            await store.read(token)
        expired = await store.create(dict(id=42), datetime.now(timezone.utc)-timedelta(seconds=1))
        with self.assertRaises(AuthError):
            await store.read(expired)

    async def test_signed_http_login_uses_real_repository_and_revocable_pg_session(self):
        client = TestClient(TestServer(create_app(Settings(bot_token=TOKEN, bot_username='arti_test_bot'), pool=self.pool)))
        await client.start_server()
        try:
            origin = str(client.make_url('')).rstrip('/')
            result = await client.post('/api/auth/telegram', json=dict(init_data=signed(date=int(time.time()))), headers={'Origin': origin})
            self.assertEqual(200, result.status)
            auth = await result.json()
            cookie = result.cookies['__Host-arti_session'].value
            headers = {'Cookie': '__Host-arti_session='+cookie, 'Origin': origin, 'X-CSRF-Token': auth['csrf_token']}
            result = await client.get('/api/conversations', headers=headers)
            self.assertEqual(200, result.status)
            self.assertEqual(str(self.cid), (await result.json())['items'][0]['id'])
            result = await client.get('/api/conversations/'+str(self.cid)+'/messages', headers=headers)
            self.assertEqual([], (await result.json())['items'])
            await client.post('/api/auth/logout', json={}, headers=headers)
            self.assertEqual(401, (await client.get('/api/session', headers=headers)).status)
            with self.assertRaises(AuthError):
                await SessionStore(self.pool).read(cookie)
        finally:
            await client.close()


class ContractTests(unittest.TestCase):
    def test_http_and_domain_import_without_provider_configuration(self):
        env = {k: v for k, v in os.environ.items() if k not in ('GROQ_API_KEY', 'GEMINI_API_KEY', 'OPENAI_API_KEY')}
        result = subprocess.run([sys.executable, '-c',
            "import sys; import miniapp.server, conversations.repository; assert 'config' not in sys.modules; assert 'groq' not in sys.modules"],
            env=env, capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode)

    def test_private_context_keeps_existing_scope_hashes(self):
        from materials.types import MaterialScope, AccessContext
        ref = private_context(42)
        old = MaterialScope('arti', 42, -1, 'private', 'default', '')
        new = MaterialScope(ref.persona_id, ref.telegram_chat_id, ref.topic_id, 'private', ref.mode, ref.scene_id)
        self.assertEqual(old.key, new.key)
        self.assertEqual(old.identity_key, new.identity_key)
        self.assertEqual(AccessContext(old, 42, 'user:42').realm, AccessContext(new, 42, 'user:42').realm)
        for value in (-1, True, '42'):
            with self.assertRaises(ConversationError):
                private_context(value)
        with self.assertRaises(ConversationError):
            ContextRef('native_room', telegram_chat_id=-1, native_id=uuid.uuid4())
