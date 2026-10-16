"""MA03–04: disposable SQL, real queue/cognition, no external provider or Telegram."""
import asyncio
from datetime import datetime,timezone
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
import uuid

from bot.request_codec import decode_request,encode_request
from bot.request_runtime import CURRENT_REQUEST,store
from cognition.delivery import DeliverySuppressed
from cognition.repositories import SuppressedEvidence
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.scope import CURRENT_SCOPE,TransportScope
from cognition.types import AudienceScope,ContextKey,Origin
from conversations.service import ArtiConversationService
from conversations.publication import NativeTextSink
from conversations.types import ConversationError
from conversations.mirror import MirrorWorker
from tests.cognition.test_full_model import RecordedInterpreter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class LiveChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from materials.runtime import CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,CURRENT_COMPUTATION_USE
        self.db=isolated_database()
        self.pool=await self.db.__aenter__()
        self.tokens=[(var,var.set(value)) for var,value in ((CURRENT_REQUEST,None),(CURRENT_TURN,None),(CURRENT_SCOPE,None),
            (CURRENT_MATERIAL_USE,()),(CURRENT_DERIVATIVE_USE,()),(CURRENT_COMPUTATION_USE,()))]
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(False)
        self.rpatch=patch('cognition.runtime.get_runtime',return_value=self.runtime)
        self.rpatch.start()
        self.service=ArtiConversationService(self.pool,self.runtime)
        self.cid=await self.service.ensure_user(dict(id=42,first_name='Тест'))
        await self.service.ensure_user(dict(id=43,first_name='Другой'))

    async def asyncTearDown(self):
        self.rpatch.stop()
        await self.runtime.close()
        for var,token in reversed(self.tokens):
            var.reset(token)
        await self.db.__aexit__(None,None,None)

    async def accept(self,text='Привет',key=None):
        return await self.service.accept(42,self.cid,text,key or uuid.uuid4())

    async def ready(self):
        accepted=await self.accept()
        job=await store().claim(['text'])
        self.assertEqual(accepted['request']['id'],job['id'])
        CURRENT_REQUEST.set(job)
        request=await decode_request(job['payload'],NS())
        CURRENT_SCOPE.set(request['_telegram_scope'])
        from bot.request_runtime import prepare_turn
        turn=await prepare_turn(42,42,request['user_message'],None,source_context=request['_cognitive_context'],source_key=request['_source_key'])
        CURRENT_TURN.set(turn)
        return accepted,job,request,NativeTextSink(self.pool,request)

    async def test_atomic_duplicate_and_changed_payload(self):
        key=uuid.uuid4()
        results=await asyncio.gather(*(self.accept(key=key) for _ in range(5)))
        self.assertEqual(1,len({r['message']['id'] for r in results}))
        self.assertEqual(1,len({r['request']['id'] for r in results}))
        with self.assertRaises(ConversationError) as caught:
            await self.accept('Другой текст',key)
        self.assertEqual('idempotency_conflict',caught.exception.code)
        async with self.pool.acquire() as conn:
            self.assertEqual(1,await conn.fetchval('SELECT count(*) FROM arti_requests'))
            self.assertEqual(1,await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='user'"))
            self.assertEqual(1,await conn.fetchval('SELECT count(*) FROM app_messages'))

    async def test_accept_failure_rolls_back_source_message_and_job(self):
        async with self.pool.acquire() as conn:
            await conn.execute('ALTER TABLE app_outbox ADD CONSTRAINT test_reject CHECK(false)')
        with self.assertRaises(Exception):
            await self.accept()
        async with self.pool.acquire() as conn:
            for table in ('app_messages','arti_requests','cognitive_events','cognitive_jobs'):
                self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM '+table),table)

    async def test_native_receipt_replay_and_mirror_are_separate(self):
        accepted,job,request,sink=await self.ready()
        first=await sink.send_message(chat_id=42,text='<b>Один ответ</b>',parse_mode='HTML')
        self.assertIsInstance(first.id,uuid.UUID)
        self.assertIsNone(first.message_id)
        again=await NativeTextSink(self.pool,request).send_message(chat_id=42,text='<b>Один ответ</b>',parse_mode='HTML')
        self.assertEqual(first.id,again.id)
        with self.assertRaises(DeliverySuppressed):
            await NativeTextSink(self.pool,request).send_message(chat_id=42,text='Подмена')
        await store().finish(job['id'],job['token'],'completed')
        sender=AsyncMock(side_effect=TimeoutError())
        mirror=MirrorWorker(self.pool,sender)
        self.assertTrue(await mirror.once())
        # Unknown user mirror does not block the independent answer mirror.
        self.assertTrue(await mirror.once())
        self.assertFalse(await mirror.once())
        self.assertEqual(2,sender.await_count)
        rows=(await self.service.messages(42,self.cid))['items']
        self.assertEqual(['Привет','Один ответ'],[r['text'] for r in rows])
        self.assertEqual('delivery_unknown',rows[-1]['mirror_state'])
        self.assertEqual('completed',rows[-1]['request_state'])
        async with self.pool.acquire() as conn:
            self.assertEqual(1,await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"))
            self.assertEqual(1,await conn.fetchval('SELECT count(*) FROM app_publications'))
            self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM arti_request_sends'))

    async def test_publication_rollback_can_retry_same_ordinal(self):
        _,job,request,sink=await self.ready()
        async with self.pool.acquire() as conn:
            await conn.execute("ALTER TABLE app_deliveries ADD CONSTRAINT reject_reply CHECK(target!='miniapp')")
        with self.assertRaises(Exception):
            await sink.send_message(chat_id=42,text='Ответ')
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"))
            self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM app_publications'))
            await conn.execute('ALTER TABLE app_deliveries DROP CONSTRAINT reject_reply')
        result=await NativeTextSink(self.pool,request).send_message(chat_id=42,text='Ответ')
        self.assertIsInstance(result.id,uuid.UUID)

    async def test_stale_lease_cannot_publish(self):
        _,job,_,sink=await self.ready()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET lease_until=now()-interval '1 second' WHERE id=$1",job['id'])
        with self.assertRaises(DeliverySuppressed):
            await sink.send_message(chat_id=42,text='Поздний ответ')

    async def test_reset_before_worker_blocks_source_and_retry(self):
        key=uuid.uuid4()
        accepted=await self.accept(key=key)
        async with self.pool.acquire() as conn:
            raw=await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',accepted['request']['id'])
        import json
        await self.runtime.reset_history(42)
        with self.assertRaises(SuppressedEvidence):
            await decode_request(json.loads(raw),NS())
        self.assertIsNone(await store().claim(['text']))
        self.assertEqual('cancelled',(await store().status(accepted['request']['id']))['state'])
        with self.assertRaises(ConversationError) as caught:
            await self.accept(key=key)
        self.assertEqual('message_unavailable',caught.exception.code)
        page=await self.service.messages(42,self.cid)
        self.assertEqual([],page['items'])
        self.assertGreater(page['history_revision'],1)
        async with self.pool.acquire() as conn:
            self.assertEqual('[Удалено]',await conn.fetchval('SELECT text FROM app_messages WHERE id=$1',accepted['message']['id']))
            self.assertEqual(0,await conn.fetchval("SELECT count(*) FROM app_deliveries WHERE payload IS NOT NULL"))

    async def test_erase_during_generation_scrubs_projection_and_mirrors(self):
        accepted,_,_,sink=await self.ready()
        await sink.send_message(chat_id=42,text='Чувствительный ответ')
        async with self.pool.acquire() as conn,conn.transaction():
            source=await conn.fetchrow('SELECT cognitive_context_id,cognitive_event_id FROM app_messages WHERE id=$1',accepted['message']['id'])
            await conn.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE',source['cognitive_context_id'])
            await conn.execute('UPDATE cognitive_events SET suppressed_at=now() WHERE id=$1',source['cognitive_event_id'])
        self.assertEqual([],(await self.service.messages(42,self.cid))['items'])
        with self.assertRaises(DeliverySuppressed):
            await sink.send_message(chat_id=42,text='Поздняя часть')
        async with self.pool.acquire() as conn:
            self.assertEqual(['[Удалено]','[Удалено]'],[r['text'] for r in await conn.fetch('SELECT text FROM app_messages ORDER BY seq')])
            self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM app_deliveries WHERE payload IS NOT NULL'))
        transport=AsyncMock()
        self.assertFalse(await MirrorWorker(self.pool,transport).once())
        transport.assert_not_awaited()

    async def test_shared_history_backfill_more_than_100_and_incremental(self):
        at=datetime(2026,1,1,tzinfo=timezone.utc)
        context=ContextKey('arti',42)
        for i in range(105):
            await self.runtime.ingest(42,42,'Telegram '+str(i),i+1,context=context,occurred_at=at,audience=AudienceScope('private',42))
        rows=[]
        before=None
        while True:
            page=await self.service.messages(42,self.cid,before=before,limit=40)
            rows=page['items']+rows
            before=page['next_before']
            if before is None:
                break
        self.assertEqual(105,len(rows))
        self.assertEqual(['Telegram '+str(i) for i in range(105)],[r['text'] for r in rows])
        self.assertTrue(all(r['source']=='telegram' for r in rows))
        await self.service.synchronize(42,self.cid)
        await self.runtime.ingest(42,42,'Новое в Telegram',500,context=context,audience=AudienceScope('private',42))
        after=await self.service.messages(42,self.cid,after=105)
        self.assertEqual(['Новое в Telegram'],[r['text'] for r in after['items']])
        self.assertEqual(0,self.runtime.interpreter.calls)

    async def test_backfill_includes_only_verified_historical_user_sources(self):
        context=ContextKey('arti',42)
        cid,eid,_=await self.runtime.ingest(42,42,'Проверенный старый текст',610,context=context,event_kind='historical')
        await self.runtime.ingest(42,42,'Неизвестная старая запись',611,context=context,event_kind='historical')
        async with self.pool.acquire() as conn:
            mid=await conn.fetchval("""INSERT INTO memory_messages(chat_id,user_id,user_name,role,mode,source,message_text)
                VALUES(42,42,'Тест','user','default','legacy','Проверенный старый текст') RETURNING id""")
            await conn.execute("INSERT INTO cognitive_legacy_map(source_table,legacy_id,context_id,event_id,status,reason) VALUES('memory_messages',$1,$2,$3,'imported','test')",mid,cid,eid)
        self.assertEqual(['Проверенный старый текст'],[m['text'] for m in (await self.service.messages(42,self.cid))['items']])

    async def test_same_fifo_lane_and_no_app_coalescing(self):
        context=ContextKey('arti',42)
        scope=TransportScope(42,chat_type='private',user_id=42,message_id=9)
        _,eid,_=await self.runtime.ingest(42,42,'Telegram первый',9,context=context,audience=AudienceScope('private',42))
        payload=await encode_request(dict(type='text',chat_id=42,user_id=42,message_id=9,user_message='Telegram первый',
            _telegram_scope=scope,_cognitive_context=context,_cognitive_source_ids=[eid]))
        telegram=await store().enqueue('text',42,-1,'telegram-first',payload,debounce_seconds=.3)
        app=await self.accept()
        await asyncio.sleep(.35)
        first=await store().claim(['text'])
        self.assertEqual(telegram['id'],first['id'])
        self.assertIsNone(await store().claim(['text']))
        await store().finish(first['id'],first['token'],'completed')
        self.assertEqual(app['request']['id'],(await store().claim(['text']))['id'])

    async def test_event_cursor_replay_revision_and_foreign_access(self):
        await self.accept('Первое')
        await self.accept('Второе')
        first=await self.service.events(42,self.cid,limit=1)
        self.assertEqual(1,first['cursor'])
        second=await self.service.events(42,self.cid,after=first['cursor'])
        self.assertEqual(2,second['cursor'])
        self.assertEqual('Второе',second['items'][0]['message']['text'])
        for action in (lambda:self.service.events(43,self.cid),lambda:self.service.messages(43,self.cid),
            lambda:self.service.request_status(43,self.cid,first['requests'][0]['id'])):
            with self.assertRaises(ConversationError) as caught:
                await action()
            self.assertEqual(404,caught.exception.status)
        await self.runtime.reset_history(42)
        replay=await self.service.events(42,self.cid)
        self.assertTrue(all(item['message'] is None for item in replay['items']))
        self.assertIn('history.reset',[item['kind'] for item in replay['items']])

    async def test_native_room_identity_has_no_telegram_address(self):
        from conversations.native import NativeContextKey,load
        cid=await self.service.create_room(42,'Комната',uuid.uuid4())
        async with self.pool.acquire() as conn:
            self.assertEqual(cid,await conn.fetchval('SELECT id FROM cognitive_native_contexts WHERE id=$1',cid))
            state=await load(conn,cid)
        key=NativeContextKey(cid)
        self.assertEqual(key,state.context)
        self.assertFalse(hasattr(key,'chat_id'))
        self.assertEqual(('arti',42,'default','',-1),ContextKey('arti',42).identity())
        from cognition.types import NativeAudienceScope,CognitiveEvent,EvidenceRef
        from cognition.serialization import dump,load_event
        from bot.request_codec import encode_value,decode_value
        audience=NativeAudienceScope(cid)
        self.assertEqual(key,await decode_value(await encode_value(key)))
        self.assertEqual(audience,await decode_value(await encode_value(audience)))
        at=datetime.now(timezone.utc)
        event=CognitiveEvent('room:test',key,EvidenceRef('room:test','room:test',Origin.USER,42),at,at,'Комната',42,audience=audience)
        self.assertEqual(event,load_event(dump(event)))
        with self.assertRaises(ValueError):
            CognitiveEvent('room:bad',key,event.evidence,at,at,'Личное',42,audience=AudienceScope('private',42))

    async def test_existing_pipeline_uses_one_provider_and_shared_context(self):
        from bot.request_runtime import _execute
        scope=TransportScope(42,chat_type='private',user_id=42,message_id=70)
        CURRENT_SCOPE.set(scope)
        await self.runtime.ingest(42,42,'Раньше в Telegram',70)
        await self.accept('Продолжаем здесь')
        job=await store().claim(['text'])
        CURRENT_REQUEST.set(job)
        provider=AsyncMock(return_value=('Тестовый <b>ответ</b>',False,[],[]))
        transport=NS(send_message=AsyncMock(),send_chat_action=AsyncMock())
        with patch('bot.queue.generate_response_stream',provider),patch('ai.intents.resolve_intent',AsyncMock(return_value={})), \
             patch('bot.queue.get_chat_model',AsyncMock(return_value='offline-model')), \
             patch('bot.queue._initiative_question',AsyncMock()),patch('bot.queue._conversational_reply',return_value=False):
            await _execute(job,transport)
        provider.assert_awaited_once()
        self.assertIn('Раньше в Telegram',str(provider.await_args.args[3]))
        self.assertEqual('completed',(await store().status(job['id']))['state'])
        self.assertEqual('Тестовый ответ',(await self.service.messages(42,self.cid))['items'][-1]['text'])
        transport.send_message.assert_not_awaited()

    async def test_pipeline_restart_after_publication_reuses_provider_checkpoint_and_receipt(self):
        from bot.request_runtime import _execute
        from bot.request_store import RequestStore
        await self.accept('Запрос с перезапуском')
        job=await store().claim(['text'])
        CURRENT_REQUEST.set(job)
        provider=AsyncMock(return_value=('Ответ пережил перезапуск',False,[],[]))
        transport=NS(send_message=AsyncMock(),send_chat_action=AsyncMock())
        with patch('bot.queue.generate_response_stream',provider),patch('ai.intents.resolve_intent',AsyncMock(return_value={})), \
             patch('bot.queue.get_chat_model',AsyncMock(return_value='offline-model')), \
             patch('bot.queue._initiative_question',AsyncMock()),patch('bot.queue._conversational_reply',return_value=False):
            with patch.object(RequestStore,'finish',AsyncMock(side_effect=OSError('synthetic commit acknowledgement loss'))):
                with self.assertRaises(OSError):
                    await _execute(job,transport)
            original=(await self.service.messages(42,self.cid))['items'][-1]
            await store().release(job['id'],job['token'])
            resumed=await store().claim(['text'])
            CURRENT_REQUEST.set(resumed)
            await _execute(resumed,transport)
        provider.assert_awaited_once()
        self.assertEqual(original['id'],(await self.service.messages(42,self.cid))['items'][-1]['id'])
        async with self.pool.acquire() as conn:
            self.assertEqual(1,await conn.fetchval('SELECT count(*) FROM app_publications'))
            self.assertEqual(1,await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"))

    async def test_concurrent_telegram_enqueue_and_native_intake_do_not_invert_locks(self):
        context=ContextKey('arti',42)
        await self.runtime.ensure_context(context)
        scope=TransportScope(42,chat_type='private',user_id=42,message_id=111)
        _,eid,_=await self.runtime.ingest(42,42,'Другой канал',111,context=context,audience=AudienceScope('private',42))
        payload=await encode_request(dict(type='text',chat_id=42,user_id=42,message_id=111,user_message='Другой канал',
            _telegram_scope=scope,_cognitive_context=context,_cognitive_source_ids=[eid]))
        telegram,native=await asyncio.wait_for(asyncio.gather(
            store().enqueue('text',42,-1,'concurrent-tg',payload),self.accept('Параллельный Mini App')),10)
        self.assertNotEqual(telegram['id'],native['request']['id'])

    async def test_native_organizer_source_has_one_owner_and_is_erasable(self):
        from bot.request_runtime import _execute
        from organizer.ownership import owns_event
        result=await self.accept('Добавь задачу купить чай')
        job=await store().claim(['text'])
        CURRENT_REQUEST.set(job)
        with patch('bot.queue.generate_response_stream',AsyncMock()) as provider:
            await _execute(job,NS())
        provider.assert_not_awaited()
        async with self.pool.acquire() as conn:
            src=await conn.fetchrow('SELECT cognitive_context_id,cognitive_event_id FROM app_messages WHERE id=$1',result['message']['id'])
            self.assertTrue(await owns_event(conn,src['cognitive_context_id'],src['cognitive_event_id']))
            self.assertEqual(1,await conn.fetchval('SELECT count(*) FROM arti_organizer_items'))
            await conn.execute('UPDATE cognitive_events SET suppressed_at=now() WHERE id=$1',src['cognitive_event_id'])
            self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM arti_organizer_items'))
        self.assertEqual([],(await self.service.messages(42,self.cid))['items'])
