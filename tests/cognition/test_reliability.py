import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from utils.instance_lock import InstanceLock,AlreadyRunning,PollerLease
from ai.intents import direct_intent,resolve_intent


class InstanceTests(unittest.TestCase):
    def test_process_lock_stop_nonce_and_crash_release(self):
        with tempfile.TemporaryDirectory() as directory:
            script = "import sys,time; from utils.instance_lock import InstanceLock; l=InstanceLock('fixture',sys.argv[1]).acquire(); print('ready',flush=True); time.sleep(30)"
            process=subprocess.Popen([sys.executable,'-c',script,directory],stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'ready')
                rival=InstanceLock('fixture',directory)
                with self.assertRaises(AlreadyRunning): rival.acquire()
                self.assertEqual(rival.status()['pid'],process.pid)
                other=InstanceLock('other',directory).acquire(); other.close()
                self.assertTrue(rival.request_stop())
            finally:
                process.kill(); process.wait(timeout=5); process.stdout.close()
            newer=InstanceLock('fixture',directory).acquire()
            self.assertFalse(newer.stop_requested())
            self.assertTrue(InstanceLock('fixture',directory).request_stop())
            self.assertTrue(newer.stop_requested()); newer.close()
            self.assertIsNone(InstanceLock('fixture',directory).status())


class IntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_requests_politeness_and_non_actions(self):
        cases=[
            ('Какая погода сегодня?','search'),
            ('Пожалуйста, какая погода сегодня?','search'),
            ('Привет, какие новости сегодня?','search'),
            ('Спасибо, найди ближайшую аптеку','maps'),
            ('Собери из этих файлов наглядную схему','artifact'),
            ('Помоги сравнить эти документы и подготовить итоговую таблицу','artifact'),
            ('Хочу, чтобы ты превратила это в наглядную схему',None),
            ('Не создавай инфографику, просто поговорим',None),
            ('Как сделать инфографику?',None),
            ('Переведи «Найди ближайшую аптеку»',None),
            ('Без поиска в интернете расскажи о погоде',None),
            ('Придумай сказку про погоду',None),
            ('Привет! Как дела?',None),
            ('Помоги понять, зачем вообще нужны инфографики',None),
            ('Можешь объяснить, как устроена эта схема?',None),
            ('Узнай, почем сейчас билет до Казани','search'),
        ]
        for prompt,expected in cases:
            with self.subTest(prompt=prompt):
                result=await resolve_intent(prompt,allow_work=True)
                actual='maps' if result['maps'] else 'search' if result['web_search'] else result['work']
                self.assertEqual(actual,expected)

    async def test_ambiguous_request_uses_selected_model_once(self):
        import httpx
        client=NS(model_for=AsyncMock(return_value='chat-choice'),complete=AsyncMock(return_value=
            httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(dict(route='artifact',confidence=.96))}}]})))
        result=await resolve_intent('Хочу это в наглядном виде',7,has_materials=True,allow_work=True,client=client)
        self.assertEqual(result['work'],'artifact')
        self.assertEqual(client.complete.await_args.args[0],'chat-choice')
        client.model_for.assert_awaited_once_with(7)
        result=await resolve_intent('Хочу это в наглядном виде',7,client=client)
        self.assertIsNone(result['work'])

    async def test_slow_router_does_not_hold_conversation(self):
        async def slow(*args): await asyncio.sleep(10)
        client=NS(model_for=AsyncMock(return_value='choice'),complete=slow)
        started=time.monotonic()
        result=await resolve_intent('Помоги разобраться',7,client=client,timeout=.02)
        self.assertLess(time.monotonic()-started,.3); self.assertFalse(any(result.values()))

    async def test_local_semantics_requires_materials_and_enabled_work(self):
        import httpx
        encoder=NS(intent_references=[[1.,0.]]*5+[[0.,1.]]*5,encode=AsyncMock(return_value=[[1.,0.]]))
        client=NS(model_for=AsyncMock(return_value='choice'),complete=AsyncMock(return_value=httpx.Response(503)))
        for enabled,materials in ((False,True),(True,False)):
            output=await resolve_intent('Хочу это в наглядном виде',7,allow_work=enabled,has_materials=materials,local_encoder=encoder,client=client)
            self.assertIsNone(output['work']); encoder.encode.assert_not_awaited()
        output=await resolve_intent('Хочу это в наглядном виде',7,allow_work=True,has_materials=True,local_encoder=encoder,client=client)
        self.assertEqual(output['work'],'artifact'); encoder.encode.assert_awaited_once()

    async def test_uncertain_local_semantics_and_quoted_action_do_not_execute(self):
        from ai.intent_semantics import local_artifact_intent
        encoder=NS(intent_references=[[.70,0.]]*5+[[.65,0.]]*5,encode=AsyncMock(return_value=[[1.,0.]]))
        self.assertFalse(await local_artifact_intent('Хочу обсудить «создай схему»',encoder))
        self.assertNotIn('создай схему',encoder.encode.await_args.args[0][-1])

    async def test_location_does_not_hijack_small_talk(self):
        self.assertFalse(direct_intent('Как дела?',recent_maps=True)[0]['maps'])
        self.assertTrue(direct_intent('А поближе?',recent_maps=True)[0]['maps'])

    async def test_channel_constraints_do_not_wait_for_emotional_interpretation(self):
        from ai.intents import channel_restrictions
        self.assertEqual(channel_restrictions('Пожалуйста, ответь только текстом и без стикеров'),
                         dict(voice=False,text=True,stickers=False))
        self.assertEqual(channel_restrictions('Переведи «ответь только текстом»'),{})

    async def test_fast_classification_does_not_change_cognition_thinking(self):
        from ai.providers.structured import SelectedModelClient
        google=NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=NS(text='{}',candidates=[])))))
        client=SelectedModelClient(google_client=google,fast=True)
        await client.complete('gemini-3.1-flash-lite-preview',[dict(role='user',content='fixture')],128)
        self.assertEqual(google.aio.models.generate_content.await_args.kwargs['config'].thinking_config.thinking_level.value,'MINIMAL')
        client.fast=False
        await client.complete('gemini-3.1-flash-lite-preview',[dict(role='user',content='fixture')],128)
        self.assertIsNone(google.aio.models.generate_content.await_args.kwargs['config'].thinking_config)

    async def test_worker_shutdown_propagates_cancellation(self):
        from bot import queue as module
        key=-987654321; started=asyncio.Event()
        async def slow(*args): started.set(); await asyncio.sleep(10)
        pending=asyncio.Queue()
        await pending.put(dict(chat_id=10,user_id=1,message_id=1,context=NS(bot=NS()),user_message='fixture'))
        module._user_queues[key]=pending
        try:
            with patch.object(module,'_DEBOUNCE_WINDOW_SEC',.001),patch.object(module,'process_user_reply',new=slow),patch.object(module,'is_responses_enabled',new=AsyncMock(return_value=True)):
                task=asyncio.create_task(module._user_text_worker(key))
                await asyncio.wait_for(started.wait(),1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task,.5)
        finally:
            module._user_queues.pop(key,None); module._user_workers.pop(key,None); module._user_queue_locks.pop(key,None)

    async def test_conflict_stops_poller_with_actionable_private_log(self):
        import logging
        from telegram.error import Conflict
        from bot.handlers import error_handler
        from cognition.logging import PrivatePayloadFilter
        from unittest.mock import Mock
        context=NS(error=Conflict('PRIVATE provider text'),application=NS(stop_running=Mock()))
        with self.assertLogs('bot.handlers',level='ERROR') as records:
            await error_handler(None,context)
        context.application.stop_running.assert_called_once()
        record=records.records[0]
        PrivatePayloadFilter().filter(record)
        self.assertIn('polling_conflict_stop',record.getMessage())
        self.assertNotIn('PRIVATE',record.getMessage()); self.assertIsNone(record.exc_info)

    async def test_generation_deadline_cancels_async_provider(self):
        from ai import generation
        cancelled=asyncio.Event()
        async def slow(**kwargs):
            try: await asyncio.sleep(10)
            finally: cancelled.set()
        with patch.object(generation,'GENERATION_TIMEOUT',.03),patch.object(generation.genai_client.aio.models,'generate_content',new=slow):
            result=await generation.generate_response_stream(1,'Привет','User','',custom_system_prompt='Hello',request_intent={})
        self.assertTrue(generation.is_error_response(result[0])); self.assertTrue(cancelled.is_set())


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','isolated SQL tests')
class ReliabilitySQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.runtime import CognitiveRuntime
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active',strict=True,prepare_budget=.05).initialize(start_worker=False)

    async def asyncTearDown(self):
        from cognition.runtime import CURRENT_TURN
        CURRENT_TURN.set(None)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def test_database_poller_lease_excludes_another_session(self):
        first=await PollerLease(self.pool,'fixture').acquire()
        second=PollerLease(self.pool,'fixture')
        try:
            with self.assertRaises(AlreadyRunning): await second.acquire()
            self.assertIsNone(second.conn); self.assertTrue(await first.healthy())
        finally: await first.close()
        await second.acquire(); await second.close()

    async def test_slow_cognition_does_not_block_reply_or_other_chat(self):
        from tests.cognition.test_full_model import RecordedInterpreter
        gate=asyncio.Event(); entered=asyncio.Event(); real=RecordedInterpreter()
        async def interpret(ev,**kwargs):
            if ev.context.chat_id==10:
                entered.set(); await gate.wait()
            return await real.interpret(ev,**kwargs)
        self.runtime.interpreter=NS(interpret=interpret)
        self.runtime.worker.poll_seconds=.01; self.runtime.worker.start()
        started=time.monotonic()
        turn=await self.runtime.prepare(10,1,'Slow observation',1)
        self.assertLess(time.monotonic()-started,2.5)
        self.assertTrue(entered.is_set()); self.assertIsNone(turn.expression.sticker_mood)
        other=await self.runtime.prepare(20,2,'Independent observation',1)
        await asyncio.sleep(.2)
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT perception IS NOT NULL FROM cognitive_events WHERE id=$1',other.event_id))
        gate.set()
        for _ in range(100):
            async with self.pool.acquire() as conn:
                done=await conn.fetchval("SELECT status='done' FROM cognitive_jobs WHERE event_id=$1 AND kind='interpret'",turn.event_id)
            if done: break
            await asyncio.sleep(.02)
        self.assertTrue(done)
