"""Natural native requests commit real private records; all sends are fake."""
import os
import unittest
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from cognition.scope import TransportScope
from organizer.repository import Repository
from bot.organizer_commands import handle_natural


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class NaturalOrganizerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); self.repo=Repository(self.pool)
        self.bot=NS(send_message=AsyncMock()); self.message=0

    async def asyncTearDown(self): await self.db.__aexit__(None,None,None)

    async def say(self,text,owner=1,mid=None):
        self.message+=1
        request=dict(chat_id=owner,user_id=owner,message_id=mid or self.message,user_message=text,
            _telegram_scope=TransportScope(owner,-1,'private',owner,mid or self.message))
        with patch('bot.organizer_commands.repository',return_value=self.repo):
            return await handle_natural(request,self.bot)

    async def test_polite_task_creation_and_replay_are_real(self):
        text='Пожалуйста, добавь задачу купить хлеб'
        self.assertTrue(await self.say(text,mid=10)); self.assertTrue(await self.say(text,mid=10))
        rows=await self.repo.list(1,1,'todo')
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['title'],'купить хлеб')
        self.assertIn('Сохранено',self.bot.send_message.await_args.kwargs['text'])

    async def test_relative_reminder_needs_no_timezone_and_is_private(self):
        before=datetime.now(timezone.utc)
        self.assertTrue(await self.say('Привет, пожалуйста, напомни через 30 минут позвонить'))
        rows=await self.repo.list(1,1,'reminder')
        self.assertEqual(rows[0]['title'],'позвонить')
        self.assertLess(abs((rows[0]['due_at']-before-timedelta(minutes=30)).total_seconds()),3)
        self.assertEqual(await self.repo.list(2,2),[])
        self.assertTrue(await self.say('Покажи мои напоминания'))
        self.assertIn('позвонить',self.bot.send_message.await_args.kwargs['text'])

    async def test_title_clarification_survives_recreation_and_anchors_time(self):
        await self.say('Напомни через 30 минут',mid=20)
        self.assertIn('О чём',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])
        self.repo=Repository(self.pool)
        await self.say('Позвонить маме',mid=21)
        rows=await self.repo.list(1,1)
        self.assertEqual(rows[0]['title'],'Позвонить маме'); self.assertEqual(rows[0]['source_key'],'telegram:1:20')
        async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM arti_organizer_pending'),0)

    async def test_missing_date_then_timezone_are_explicit(self):
        await self.say('Напомни позвонить')
        self.assertIn('Когда',self.bot.send_message.await_args.kwargs['text'])
        tomorrow=(datetime.now(timezone.utc)+timedelta(days=2)).strftime('%Y-%m-%dT09:00')
        await self.say(tomorrow)
        self.assertIn('часовом поясе',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])
        await self.say('Москва')
        row=(await self.repo.list(1,1))[0]
        self.assertEqual(row['timezone'],'Europe/Moscow')
        self.assertIn('+0300',self.bot.send_message.await_args.kwargs['text'])
        self.assertIsNone(await self.repo.get_timezone(1,1))  # One event is not a lasting preference.

    async def test_partial_day_asks_clock_and_zone(self):
        await self.say('Напомни завтра купить чай')
        self.assertIn('Во сколько',self.bot.send_message.await_args.kwargs['text'])
        await self.say('09:00')
        self.assertIn('часовом поясе',self.bot.send_message.await_args.kwargs['text'])
        await self.say('Europe/Moscow')
        self.assertEqual((await self.repo.list(1,1))[0]['title'],'купить чай')

    async def test_quotes_negations_thirdparty_and_memory_recall_do_not_write(self):
        for text in ('Переведи «добавь задачу купить хлеб»','Не добавляй задачу купить хлеб',
                     'Мама просит добавить задачу купить хлеб','«Напомни через 30 минут позвонить»',
                     'Напомни, что случилось с начальником','Напомни мне имя друга'):
            with self.subTest(text=text): self.assertFalse(await self.say(text))
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_cancel_and_expiration_do_not_create_pending_operation(self):
        await self.say('Добавь задачу')
        await self.say('Отмена')
        self.assertEqual(await self.repo.list(1,1),[])
        self.assertFalse(await self.say('купить чай'))
        await self.say('Добавь задачу')
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_organizer_pending SET expires_at=NOW()-INTERVAL '1 second'")
        self.assertFalse(await self.say('купить чай'))
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_group_and_foreign_identity_cannot_mutate(self):
        request=dict(chat_id=-10,user_id=1,message_id=1,user_message='Добавь задачу купить чай',
            _telegram_scope=TransportScope(-10,4,'supergroup',1,1))
        self.assertTrue(await handle_natural(request,self.bot))
        self.assertIn('личный чат',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_cancel_clears_only_pending_clarification_not_saved_reminders(self):
        from bot.queue import cancel_chat_generation
        await self.say('Напомни через 30 минут купить чай')
        await self.say('Добавь задачу')
        await cancel_chat_generation(1)
        self.assertFalse(await self.say('купить хлеб'))
        self.assertEqual(len(await self.repo.list(1,1,'reminder')),1)

    async def test_changed_topic_does_not_become_pending_task_title(self):
        await self.say('Добавь задачу')
        self.assertFalse(await self.say('Какая погода сегодня?'))
        self.assertEqual(await self.repo.list(1,1),[])
        await self.say('Покупки на выходные')
        self.assertIn('Использовать название',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])
        await self.say('Да')
        self.assertEqual((await self.repo.list(1,1))[0]['title'],'Покупки на выходные')

    async def test_multiline_actions_never_become_one_title(self):
        await self.say('добавь задачу купить хлеб\nкакая погода сегодня?')
        self.assertIn('несколько строк',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])
        await self.say('Добавь задачу')
        await self.say('купить хлеб\nкакая погода сегодня?')
        self.assertIn('одно название',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_alternative_times_require_one_specific_choice(self):
        await self.say('Напомни через 5 минут или через 10 минут позвонить')
        self.assertIn('одно время',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])
        before=datetime.now(timezone.utc)
        await self.say('через 10 минут')
        row=(await self.repo.list(1,1))[0]
        self.assertEqual(row['title'],'позвонить')
        self.assertLess(abs((row['due_at']-before-timedelta(minutes=10)).total_seconds()),3)

    async def test_durable_debounce_keeps_native_request_boundaries(self):
        from bot.request_codec import encode_request
        from bot.request_store import RequestStore
        requests=RequestStore(self.pool)
        async def enqueue(text,key,no_merge=False):
            payload=await encode_request(dict(type='text',chat_id=1,user_id=1,message_id=int(key),user_message=text,
                _request_no_coalesce=no_merge,_telegram_scope=TransportScope(1,-1,'private',1,int(key))))
            return await requests.enqueue('text',1,-1,key,payload)
        first=await enqueue('добавь задачу купить хлеб','1')
        second=await enqueue('какая погода сегодня?','2')
        self.assertNotEqual(first['id'],second['id'])
        third=await enqueue('купить чай','3',True)
        fourth=await enqueue('расскажи новости','4')
        self.assertNotEqual(third['id'],fourth['id'])

    async def test_recurring_request_is_not_silently_saved_once(self):
        await self.say('Напомни через 5 минут и каждый день позвонить')
        self.assertIn('не создано',self.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_title_confirmation_does_not_capture_yes_after_topic_change(self):
        await self.say('Добавь задачу')
        await self.say('Покупки на выходные')
        self.assertFalse(await self.say('Какая погода сегодня?'))
        await self.say('Да')
        self.assertEqual(await self.repo.list(1,1),[])

    async def test_direct_replay_returns_existing_without_reinterpreting_elapsed_date(self):
        text='Напомни '+(datetime.now(timezone.utc)+timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%MZ')+' позвонить'
        await self.say(text,mid=70)
        with patch('organizer.natural.scheduled_at',side_effect=AssertionError('reparsed old date')):
            self.assertTrue(await self.say(text,mid=70))
        self.assertEqual(len(await self.repo.list(1,1)),1)

    async def test_clarification_replay_after_create_before_ack_uses_saved_result(self):
        from bot.request_runtime import CURRENT_REQUEST
        from bot.request_store import RequestStore
        from cognition.runtime import CURRENT_TURN
        await self.say('Напомни через 30 минут',mid=80)
        requests=RequestStore(self.pool)
        await requests.enqueue('text',1,-1,'final-reply',{})
        token=CURRENT_REQUEST.set(await requests.claim(['text'])); turn=CURRENT_TURN.set(None)
        try:
            self.bot.send_message.side_effect=RuntimeError('crash before acknowledgement')
            with self.assertRaises(RuntimeError): await self.say('позвонить',mid=81)
            self.assertEqual(len(await self.repo.list(1,1)),1)
            async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM arti_organizer_pending'),1)
            job=CURRENT_REQUEST.get(); await requests.release(job['id'],job['token'])
            CURRENT_REQUEST.set(await requests.claim(['text']))
            self.bot.send_message.side_effect=None
            with patch.object(self.repo,'create',new=AsyncMock(side_effect=AssertionError('duplicate creation'))):
                self.assertTrue(await self.say('позвонить',mid=81))
            async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM arti_organizer_pending'),0)
            # Crash after ack/clear but before ordinary job completion is safe too.
            job=CURRENT_REQUEST.get(); await requests.release(job['id'],job['token'])
            CURRENT_REQUEST.set(await requests.claim(['text']))
            self.assertTrue(await self.say('позвонить',mid=81))
            self.assertEqual(len(await self.repo.list(1,1)),1)
        finally: CURRENT_REQUEST.reset(token); CURRENT_TURN.reset(turn)

    async def test_creation_before_checkpoint_retains_pending_and_replays_original_item(self):
        from bot.request_runtime import CURRENT_REQUEST
        from bot.request_store import RequestStore
        from cognition.runtime import CURRENT_TURN
        await self.say('Напомни через 30 минут',mid=90)
        requests=RequestStore(self.pool); await requests.enqueue('text',1,-1,'checkpoint-crash',{})
        token=CURRENT_REQUEST.set(await requests.claim(['text'])); turn=CURRENT_TURN.set(None)
        try:
            with patch('bot.request_runtime.RequestStore.checkpoint',new=AsyncMock(side_effect=RuntimeError('checkpoint failed'))):
                with self.assertRaises(RuntimeError): await self.say('позвонить',mid=91)
            self.assertEqual(len(await self.repo.list(1,1)),1)
            job=CURRENT_REQUEST.get(); await requests.release(job['id'],job['token'])
            CURRENT_REQUEST.set(await requests.claim(['text']))
            with patch('organizer.natural.scheduled_at',side_effect=AssertionError('must use existing item')):
                self.assertTrue(await self.say('позвонить',mid=91))
            self.assertEqual(len(await self.repo.list(1,1)),1)
        finally: CURRENT_REQUEST.reset(token); CURRENT_TURN.reset(turn)

    async def test_bounded_expiry_cleanup_removes_inactive_owners_data(self):
        from organizer.natural import cleanup_expired
        async with self.pool.acquire() as conn:
            await conn.executemany("INSERT INTO arti_organizer_pending VALUES($1,$1,'fixture','{}',NOW()-INTERVAL '1 hour')",[(n,) for n in range(1,6)])
        self.assertEqual(await cleanup_expired(self.pool,2),2)
        async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM arti_organizer_pending'),3)
        self.assertEqual(await cleanup_expired(self.pool),3)

    async def test_native_routes_are_claimed_before_ingestion_and_link_clarifications(self):
        from organizer.natural import claim_input
        self.assertTrue(await claim_input(self.pool,1,1,100,'Напомни через 30 минут'))
        await self.say('Напомни через 30 минут',mid=100)
        self.assertFalse(await claim_input(self.pool,1,1,101,'Какая погода сегодня?'))
        self.assertTrue(await claim_input(self.pool,1,1,102,'позвонить'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT root_source_key FROM arti_organizer_source_routes WHERE source_key='telegram:1:102:user'"),'telegram:1:100:user')

    async def test_tomorrow_clarification_is_anchored_across_local_midnight(self):
        from zoneinfo import ZoneInfo
        instant=(datetime.now(timezone.utc)+timedelta(days=2)).replace(hour=20,minute=59,second=0,microsecond=0)
        clock=[instant]
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
        with patch('organizer.natural.datetime',Frozen):
            await self.say('Напомни завтра купить чай')
            clock[0]+=timedelta(minutes=2)
            await self.say('09:00')
            await self.say('Москва')
        row=(await self.repo.list(1,1))[0]
        expected_day=instant.astimezone(ZoneInfo('Europe/Moscow')).date()+timedelta(days=1)
        self.assertEqual(row['due_at'].astimezone(ZoneInfo('Europe/Moscow')).date(),expected_day)
