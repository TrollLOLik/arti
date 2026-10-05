"""Independent organizer race, reply provenance, reset and erasure checks."""
import os
import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from organizer.repository import Repository
from organizer.time import OrganizerError
from tests.cognition.test_full_model import RecordedInterpreter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class NativeOrganizerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter()).initialize(False)
        self.repo = Repository(self.pool)
        self.tokens = [(v, v.set(value)) for v, value in (
            (CURRENT_TURN, None), (CURRENT_SCOPE, TransportScope(1, -1, 'private', 1, 1)))]
        self.bot = NS(send_message=AsyncMock(return_value=NS(message_id=901)))

    async def asyncTearDown(self):
        for variable, token in reversed(self.tokens):
            variable.reset(token)
        await self.runtime.close()
        await self.db.__aexit__(None, None, None)

    async def say(self, text, mid, *, reply=None):
        from bot.organizer_commands import handle_natural
        request = dict(chat_id=1, user_id=1, message_id=mid, user_message=text,
                       _telegram_scope=TransportScope(1, -1, 'private', 1, mid, reply_to_id=reply))
        with patch('bot.organizer_commands.repository', return_value=self.repo):
            return await handle_natural(request, self.bot)

    async def reminder(self, key='telegram:1:10'):
        return await self.repo.create(1, 1, 'reminder', 'Private reminder', key,
            due_at=datetime.now(timezone.utc) + timedelta(hours=1), timezone_name='UTC')

    async def test_stale_edit_rolls_back_action_marker_and_current_item(self):
        row = await self.repo.create(1, 1, 'todo', 'Original', 'create')
        renamed = await self.repo.edit(1, 1, row['id'], title='New title', expected_version=row['version'], source_key='rename')
        with self.assertRaisesRegex(OrganizerError, 'stale_item'):
            await self.repo.edit(1, 1, row['id'], title='Stale title', expected_version=row['version'], source_key='stale-rename')
        self.assertEqual(renamed, await self.repo.get(1, 1, row['id']))
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval("SELECT 1 FROM arti_organizer_actions WHERE source_key='stale-rename'"))

    async def test_explicit_reply_target_keeps_shown_version_and_owner(self):
        first = await self.repo.create(1, 1, 'todo', 'First', 'first')
        second = await self.repo.create(1, 1, 'todo', 'Second', 'second')
        await self.repo.record_reply(1, 1, 55, [first])
        await self.repo.edit(1, 1, first['id'], title='First changed', expected_version=first['version'])
        selected = await self.repo.resolve(1, 1, 'это', reply_to_id=55)
        self.assertEqual([first['id']], [r['id'] for r in selected])
        self.assertEqual(first['version'], selected[0]['expected_version'])
        self.assertEqual([], await self.repo.resolve(1, 1, 'это'))
        self.assertEqual([], await self.repo.resolve(1, 1, 'это', reply_to_id=999))
        self.assertEqual([], await self.repo.resolve(2, 2, 'это', reply_to_id=55))
        self.assertEqual('Second', (await self.repo.get(1, 1, second['id']))['title'])

    async def test_reschedule_revokes_claim_and_old_lease_cannot_send(self):
        row = await self.reminder()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1", row['id'])
        claimed = await self.repo.claim_due()
        edited = await self.repo.edit(1, 1, row['id'], expected_version=row['version'],
            due_at=datetime.now(timezone.utc) + timedelta(days=1), timezone_name='UTC', source_key='reschedule')
        self.assertGreater(edited['version'], row['version'])
        self.assertIsNone(await self.repo.begin_send(row['id'], claimed['token']))
        self.assertIsNone(await self.repo.claim_due())

    async def test_edit_cannot_reopen_inflight_or_uncertain_delivery(self):
        row = await self.reminder()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1", row['id'])
        claimed = await self.repo.claim_due()
        await self.repo.begin_send(row['id'], claimed['token'])
        with self.assertRaises(OrganizerError):
            await self.repo.edit(1, 1, row['id'], title='Too late', expected_version=row['version'])
        await self.repo.change(1, 1, row['id'], 'cancelled', expected_version=row['version'])
        self.assertFalse(await self.repo.finish_send(row['id'], claimed['token'], delivered=True, message_id=991))
        self.assertEqual('delivery_unknown', (await self.repo.get(1, 1, row['id']))['notification_state'])
        self.assertIsNone(await self.repo.claim_due())

    async def test_real_history_reset_clears_pending_and_old_reply_targets(self):
        saved = await self.reminder('saved-reminder')
        await self.repo.record_reply(1, 1, 51, [saved])
        await self.runtime.ingest(1, 1, 'Напомни через 30 минут', 10)
        await self.say('Напомни через 30 минут', 10)
        await self.runtime.reset_history(1)
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM arti_organizer_pending'))
        self.assertEqual([], await self.repo.resolve(1, 1, 'это', reply_to_id=51))
        await self.say('Позвонить маме', 11)
        await self.say('Напомни через 30 минут', 10)
        await self.say('Позвонить маме', 12)
        self.assertEqual([saved['id']], [r['id'] for r in await self.repo.list(1, 1)])

    async def test_real_source_forget_erases_pending_and_denies_root_replay(self):
        from cognition.forgetting import forget_cognitive_sources
        cid, _, event = await self.runtime.ingest(1, 1, 'Напомни позвонить СекретномуКонтакту', 20)
        await self.say('Напомни позвонить СекретномуКонтакту', 20)
        await forget_cognitive_sources(self.pool, cid, 1, [event.evidence.source_id])
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM arti_organizer_pending'))
            retained = await conn.fetch('SELECT outcome FROM arti_organizer_turns WHERE outcome IS NOT NULL')
        self.assertNotIn('СекретномуКонтакту', str(retained))
        await self.say('Напомни позвонить СекретномуКонтакту', 20)
        await self.say('через 30 минут', 21)
        self.assertEqual([], await self.repo.list(1, 1))

    async def test_source_tombstone_before_observation_prevents_native_creation(self):
        from cognition.forgetting import forget_cognitive_sources
        cid = await self.runtime.ensure_context(await self.runtime.context(1))
        await forget_cognitive_sources(self.pool, cid, 1, ['telegram:1:30:user'])
        await self.say('Добавь задачу потерянный секрет', 30)
        self.assertEqual([], await self.repo.list(1, 1))

    async def test_real_source_forget_invalidates_claimed_notification(self):
        from cognition.forgetting import forget_cognitive_sources
        cid, _, event = await self.runtime.ingest(1, 1, 'Напомни через 30 минут секрет', 40)
        await self.say('Напомни через 30 минут секрет', 40)
        row = (await self.repo.list(1, 1))[0]
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1", row['id'])
        claimed = await self.repo.claim_due()
        await forget_cognitive_sources(self.pool, cid, 1, [event.evidence.source_id])
        self.assertIsNone(await self.repo.begin_send(row['id'], claimed['token']))
        self.assertIsNone(await self.repo.claim_due())
        async with self.pool.acquire() as conn:
            retained = await conn.fetch('SELECT title FROM arti_organizer_items')
        self.assertNotIn('секрет', str(retained))

    async def test_foreign_scene_tombstone_cannot_erase_default_item(self):
        from materials.lifecycle import forget_sources
        from materials.types import context_identity
        row = await self.reminder('telegram:1:60')
        rp_identity = context_identity('arti', 1, -1, 'rp', 'separate-fictional-scene')
        await forget_sources(self.pool, rp_identity, 1, ['telegram:1:60:user'])
        self.assertEqual(row, await self.repo.get(1, 1, row['id']))

    async def test_erasure_redacts_cached_list_outcomes_and_list_replay(self):
        from cognition.forgetting import forget_cognitive_sources
        secret = 'СекретноеНазваниеДляСписка'
        cid, _, event = await self.runtime.ingest(1, 1, 'Добавь задачу ' + secret, 70)
        await self.say('Добавь задачу ' + secret, 70)
        await self.say('Покажи мои задачи', 71)
        self.assertIn(secret, self.bot.send_message.await_args.kwargs['text'])
        await forget_cognitive_sources(self.pool, cid, 1, [event.evidence.source_id])
        async with self.pool.acquire() as conn:
            retained = await conn.fetch('SELECT outcome FROM arti_organizer_turns WHERE outcome IS NOT NULL')
        self.assertNotIn(secret, str(retained))
        await self.say('Покажи мои задачи', 71)
        self.assertNotIn(secret, self.bot.send_message.await_args.kwargs['text'])

    async def test_replayed_old_receipt_cannot_pin_new_unseen_item_version(self):
        from bot.request_runtime import CURRENT_REQUEST, send
        from bot.request_store import RequestStore
        requests = RequestStore(self.pool)
        await requests.enqueue('text', 1, -1, 'reply-binding-crash', {})
        token = CURRENT_REQUEST.set(await requests.claim(['text']))
        transport = AsyncMock(return_value=NS(message_id=991, chat=NS(id=1)))
        async def durable_send(**kwargs):
            return await send(transport, (), kwargs, 'message')
        self.bot = NS(send_message=AsyncMock(side_effect=durable_send))
        try:
            with patch.object(self.repo, 'record_reply', new=AsyncMock(side_effect=RuntimeError('crash after delivery'))):
                with self.assertRaisesRegex(RuntimeError, 'crash after delivery'):
                    await self.say('Добавь задачу Original', 80)
            item = (await self.repo.list(1, 1))[0]
            job = CURRENT_REQUEST.get()
            await requests.release(job['id'], job['token'])
            CURRENT_REQUEST.set(None)
            await self.repo.edit(1, 1, item['id'], title='Changed after delivery', expected_version=1, source_key='later')
            CURRENT_REQUEST.set(await requests.claim(['text']))
            await self.say('Добавь задачу Original', 80)
            transport.assert_awaited_once()
            self.assertIn('Original', transport.await_args.kwargs['text'])
            self.assertNotIn('Changed after delivery', transport.await_args.kwargs['text'])
            references = await self.repo.resolve(1, 1, 'это', reply_to_id=991)
            self.assertTrue(all(reference['expected_version'] == 1 for reference in references))
        finally:
            CURRENT_REQUEST.reset(token)

    async def test_cancel_and_native_commit_follow_same_lock_order(self):
        from bot.request_runtime import CURRENT_REQUEST
        from bot.request_store import RequestStore
        from bot.queue import cancel_chat_generation
        requests = RequestStore(self.pool)
        await requests.enqueue('text', 1, -1, 'cancel-lock-order', {})
        token = CURRENT_REQUEST.set(await requests.claim(['text']))
        native_at_owner_lock = asyncio.Event()
        cancellation_reaches_request = asyncio.Event()
        release_native = asyncio.Event()
        original_connection = self.repo.connection
        original_cancel = RequestStore.cancel_chat
        paused = False

        class Proxy:
            def __init__(self, conn): self.conn = conn
            def __getattr__(self, name): return getattr(self.conn, name)
            async def execute(self, sql, *args):
                nonlocal paused
                if not paused and 'pg_advisory_xact_lock' in sql and args == ('organizer:1',):
                    paused = True
                    native_at_owner_lock.set()
                    await release_native.wait()
                return await self.conn.execute(sql, *args)

        @asynccontextmanager
        async def paused_connection():
            async with original_connection() as conn:
                yield Proxy(conn)

        async def cancellation(repo, *args, **kwargs):
            cancellation_reaches_request.set()
            return await original_cancel(repo, *args, **kwargs)

        running = []
        try:
            with patch.object(self.repo, 'connection', paused_connection), \
                 patch.object(RequestStore, 'cancel_chat', cancellation), \
                 patch('bot.queue.cancel_chat_tasks'):
                running.append(asyncio.create_task(self.say('Добавь задачу Atomic', 90)))
                await asyncio.wait_for(native_at_owner_lock.wait(), 3)
                running.append(asyncio.create_task(cancel_chat_generation(1)))
                await asyncio.wait_for(cancellation_reaches_request.wait(), 3)
                release_native.set()
                results = await asyncio.wait_for(asyncio.gather(*running, return_exceptions=True), 5)
                from cognition.delivery import DeliverySuppressed
                for result in results:
                    if isinstance(result, BaseException):
                        self.assertIsInstance(result, DeliverySuppressed)
        finally:
            release_native.set()
            for task in running:
                if not task.done(): task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            CURRENT_REQUEST.reset(token)

    async def test_reset_during_send_cannot_recreate_old_reply_target(self):
        await self.runtime.ingest(1, 1, 'Добавь задачу Old reply', 100)
        async def send_then_reset(**kwargs):
            await self.runtime.reset_history(1)
            return NS(message_id=995)
        self.bot = NS(send_message=AsyncMock(side_effect=send_then_reset))
        await self.say('Добавь задачу Old reply', 100)
        self.assertEqual(1, len(await self.repo.list(1, 1)))
        self.assertEqual([], await self.repo.resolve(1, 1, 'это', reply_to_id=995))

    async def test_erased_legacy_item_cannot_escape_through_prepared_list_send(self):
        from bot.request_runtime import CURRENT_REQUEST, send
        from bot.request_store import RequestStore
        from cognition.runtime import PreparedTurn
        from cognition.forgetting import forget_cognitive_sources
        from cognition.delivery import DeliverySuppressed
        secret = 'LegacySecretBeforeCognitiveSources'
        await self.repo.create(1, 1, 'todo', secret, 'telegram:1:200')
        cid, eid, event = await self.runtime.ingest(1, 1, 'Покажи мои задачи', 201)
        CURRENT_TURN.set(PreparedTurn(self.runtime, cid, eid, event, None, '', 0, 'active'))
        requests = RequestStore(self.pool)
        await requests.enqueue('text', 1, -1, 'legacy-list-prepared', {})
        token = CURRENT_REQUEST.set(await requests.claim(['text']))
        transport = AsyncMock(return_value=NS(message_id=999, chat=NS(id=1)))
        async def durable_send(**kwargs):
            return await send(transport, (), kwargs, 'message')
        self.bot = NS(send_message=AsyncMock(side_effect=durable_send))
        try:
            with patch('cognition.runtime.get_runtime', return_value=self.runtime):
                with patch.object(RequestStore, 'begin_send', new=AsyncMock(side_effect=RuntimeError('before network'))):
                    with self.assertRaisesRegex(RuntimeError, 'before network'):
                        await self.say('Покажи мои задачи', 201)
                transport.assert_not_awaited()
                job = CURRENT_REQUEST.get()
                await requests.release(job['id'], job['token'])
                CURRENT_REQUEST.set(None)
                CURRENT_TURN.set(None)
                await forget_cognitive_sources(self.pool, cid, 1, ['telegram:1:200:user'])
                resumed = await requests.claim(['text'])
                if resumed:
                    CURRENT_REQUEST.set(resumed)
                    try:
                        await self.say('Покажи мои задачи', 201)
                    except DeliverySuppressed:
                        pass
                for call in transport.await_args_list:
                    self.assertNotIn(secret, call.kwargs.get('text', ''))
                async with self.pool.acquire() as conn:
                    retained = await conn.fetch('SELECT payload,checkpoints FROM arti_requests WHERE id=$1', job['id'])
                    retained += await conn.fetch('SELECT payload FROM arti_request_sends WHERE request_id=$1', job['id'])
                self.assertNotIn(secret, str(retained))
        finally:
            CURRENT_REQUEST.reset(token)

    async def test_erased_legacy_item_has_no_delivered_cognitive_copy(self):
        from bot.request_runtime import CURRENT_REQUEST, send
        from bot.request_store import RequestStore
        from cognition.runtime import PreparedTurn
        from cognition.forgetting import forget_cognitive_sources
        secret = 'LegacyDeliveredSecretMissingOriginalEvent'
        await self.repo.create(1, 1, 'todo', secret, 'telegram:1:300')
        cid, eid, event = await self.runtime.ingest(1, 1, 'Покажи мои задачи', 301)
        CURRENT_TURN.set(PreparedTurn(self.runtime, cid, eid, event, None, '', 0, 'active'))
        requests = RequestStore(self.pool)
        await requests.enqueue('text', 1, -1, 'legacy-list-delivered', {})
        token = CURRENT_REQUEST.set(await requests.claim(['text']))
        transport = AsyncMock(return_value=NS(message_id=998, chat=NS(id=1)))
        async def durable_send(**kwargs):
            return await send(transport, (), kwargs, 'message')
        self.bot = NS(send_message=AsyncMock(side_effect=durable_send))
        try:
            with patch('cognition.runtime.get_runtime', return_value=self.runtime):
                await self.say('Покажи мои задачи', 301)
                transport.assert_awaited_once()
                async with self.pool.acquire() as conn:
                    delivered = await conn.fetchval("SELECT id FROM cognitive_events WHERE context_id=$1 AND source_id='telegram:1:998:delivered_action'", cid)
                await self.runtime.repo.artifact(cid, 'safety_derived_copy', dict(text=secret), 1, [delivered])
                job = CURRENT_REQUEST.get()
                await requests.finish(job['id'], job['token'], 'completed')
                CURRENT_REQUEST.set(None)
                CURRENT_TURN.set(None)
                await forget_cognitive_sources(self.pool, cid, 1, ['telegram:1:300:user'])
                async with self.pool.acquire() as conn:
                    retained = await conn.fetch('SELECT payload FROM cognitive_events WHERE suppressed_at IS NULL')
                    retained += await conn.fetch('SELECT payload FROM cognitive_outbox WHERE payload IS NOT NULL')
                    retained += await conn.fetch('SELECT payload FROM cognitive_artifacts WHERE payload IS NOT NULL')
                    retained += await conn.fetch('SELECT message_text FROM chat_history WHERE chat_id=1')
                self.assertNotIn(secret, str(retained))
        finally:
            CURRENT_REQUEST.reset(token)

    async def test_legacy_erasure_during_transport_fences_late_confirmation(self):
        from bot.request_runtime import CURRENT_REQUEST, send
        from bot.request_store import RequestStore
        from cognition.runtime import PreparedTurn
        from cognition.forgetting import forget_cognitive_sources
        from cognition.delivery import DeliverySuppressed, DeliveryUnknown
        secret = 'LegacySecretErasedDuringTransport'
        await self.repo.create(1, 1, 'todo', secret, 'telegram:1:400')
        cid, eid, event = await self.runtime.ingest(1, 1, 'Покажи мои задачи', 401)
        CURRENT_TURN.set(PreparedTurn(self.runtime, cid, eid, event, None, '', 0, 'active'))
        requests = RequestStore(self.pool)
        await requests.enqueue('text', 1, -1, 'legacy-inflight-erasure', {})
        token = CURRENT_REQUEST.set(await requests.claim(['text']))
        async def in_flight(**kwargs):
            await forget_cognitive_sources(self.pool, cid, 1, ['telegram:1:400:user'])
            return NS(message_id=997, chat=NS(id=1))
        transport = AsyncMock(side_effect=in_flight)
        async def durable_send(**kwargs):
            return await send(transport, (), kwargs, 'message')
        self.bot = NS(send_message=AsyncMock(side_effect=durable_send))
        try:
            with patch('cognition.runtime.get_runtime', return_value=self.runtime):
                try:
                    await self.say('Покажи мои задачи', 401)
                except (DeliverySuppressed, DeliveryUnknown):
                    pass
                transport.assert_awaited_once()
                self.assertIsNone(await requests.claim(['text']))
                async with self.pool.acquire() as conn:
                    retained = await conn.fetch('SELECT payload FROM cognitive_events WHERE suppressed_at IS NULL')
                    retained += await conn.fetch('SELECT payload FROM cognitive_outbox WHERE payload IS NOT NULL')
                    retained += await conn.fetch('SELECT payload FROM cognitive_artifacts WHERE payload IS NOT NULL')
                    retained += await conn.fetch('SELECT message_text FROM chat_history WHERE chat_id=1')
                    retained += await conn.fetch('SELECT payload,checkpoints FROM arti_requests')
                    retained += await conn.fetch('SELECT payload FROM arti_request_sends')
                    self.assertEqual(0, await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE source_id='telegram:1:997:delivered_action' AND suppressed_at IS NULL"))
                self.assertNotIn(secret, str(retained))
        finally:
            CURRENT_REQUEST.reset(token)
