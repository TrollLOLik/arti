"""Active-only production, selected providers and retirement regression tests."""
import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx

from ai.providers.structured import SelectedModelClient
from cognition.interpreter import SelectedModelInterpreter, InterpreterFailure
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from cognition.repositories import SuppressedEvidence
from cognition.types import ContextKey
from tests.cognition.test_affect import event, AT
from tests.cognition.test_full_model import RecordedInterpreter


def completion(body):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(body)}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 5}})


class SelectedProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_uses_chat_selection_and_configured_endpoint(self):
        captured = []
        models = {10: 'fixture/one', 20: 'fixture/two'}
        def handler(request):
            captured.append((str(request.url), json.loads(request.content)))
            return completion({'appraisals': []})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        transport = SelectedModelClient(resolver=AsyncMock(side_effect=lambda cid: models[cid]), client=client)
        interpreter = SelectedModelInterpreter(transport, max_attempts=1)
        with patch('config.OMNIROUTE_BASE_URL', 'https://selected.invalid/v1'):
            await asyncio.gather(interpreter.interpret(event()),
                interpreter.interpret(replace(event(), context=ContextKey('arti',20))))
            models[10] = 'fixture/changed'
            await interpreter.interpret(event())
        self.assertEqual([url for url, _ in captured], ['https://selected.invalid/v1/chat/completions'] * 3)
        self.assertEqual({body['model'] for _, body in captured[:2]}, {'fixture/one', 'fixture/two'})
        self.assertEqual(captured[-1][1]['model'], 'fixture/changed')
        self.assertTrue(all(body['response_format'] == {'type': 'json_object'} for _, body in captured))

    async def test_gemini_uses_selected_model_without_openrouter_credentials(self):
        google = NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=NS(
            text='{"appraisals":[]}', usage_metadata=NS(prompt_token_count=4,candidates_token_count=6),
            candidates=[NS(finish_reason='STOP')])))))
        resolver = AsyncMock(return_value='gemini-fixture-selected')
        transport = SelectedModelClient(resolver=resolver, google_client=google)
        interpreter = SelectedModelInterpreter(transport, max_attempts=1)
        with patch.dict(os.environ, {'OPENROUTER_API_KEY':'', 'OPENROUTER_API_KEYS':'',
                'ARTI_COGNITION_MODEL':'ignored/old-model'}):
            result = await interpreter.interpret(event())
        kwargs = google.aio.models.generate_content.await_args.kwargs
        self.assertEqual(kwargs['model'], 'gemini-fixture-selected')
        self.assertEqual(kwargs['config'].response_mime_type, 'application/json')
        self.assertEqual(result.prompt_tokens, 4)
        self.assertEqual(result.completion_tokens, 6)
        self.assertIsNone(transport.client)

    async def test_retry_pins_selection_and_never_changes_provider(self):
        transport = NS(model_for=AsyncMock(return_value='fixture/first'),
            complete=AsyncMock(side_effect=[httpx.Response(503),completion({'appraisals':[]})]), close=AsyncMock())
        interpreter = SelectedModelInterpreter(transport, max_attempts=2)
        with patch('cognition.interpreter.asyncio.sleep',new=AsyncMock()):
            result = await interpreter.interpret(event())
        self.assertEqual(result.attempts, 2)
        transport.model_for.assert_awaited_once_with(10)
        self.assertEqual([call.args[0] for call in transport.complete.await_args_list], ['fixture/first'] * 2)

    async def test_invalid_json_is_bounded_and_cannot_supply_old_emotion(self):
        transport = NS(model_for=AsyncMock(return_value='fixture/model'),
            complete=AsyncMock(return_value=completion({'charge':1,'mood_delta':{'angry':1}})),close=AsyncMock())
        interpreter = SelectedModelInterpreter(transport, max_attempts=2)
        with patch('cognition.interpreter.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(InterpreterFailure) as caught:
                await interpreter.interpret(event())
        self.assertEqual(caught.exception.code, 'invalid_perception')
        self.assertEqual(transport.complete.await_count, 2)

    async def test_group_judgement_and_composition_follow_group_selection(self):
        from ai.group_participation import SelectedModelGroupJudge
        body=dict(action='speak',reason='useful_answer',usefulness=.9,interruption=.1,confidence=.9,
            evidence_ids=['source'],channel='text',defer_seconds=30)
        transport=NS(model_for=AsyncMock(side_effect=['fixture/group-a','fixture/group-b']),
            complete=AsyncMock(side_effect=[completion(body),completion({'text':'A useful answer.'})]),close=AsyncMock())
        frame=NS(chat_id=-20,public_packet=lambda _: {'messages':[{'source_id':'source'}]})
        judge=SelectedModelGroupJudge(transport)
        judgement=await judge.assess(frame,{})
        self.assertEqual(await judge.compose(frame,{},judgement),'A useful answer.')
        self.assertEqual([call.args[0] for call in transport.model_for.await_args_list],[-20,-20])
        self.assertEqual([call.args[0] for call in transport.complete.await_args_list],['fixture/group-a','fixture/group-b'])

    async def test_absent_runtime_cannot_open_a_legacy_response(self):
        from cognition.runtime import prepare_turn
        from cognition.authority import legacy_permitted
        with patch('cognition.runtime.get_runtime',return_value=None):
            with self.assertRaises(SuppressedEvidence):
                await prepare_turn(10,1,'Input',1)
            self.assertFalse(await legacy_permitted(10))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class CutoverSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.interpreter=RecordedInterpreter()
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:AT,strict=True).initialize(start_worker=False)
        self.scope_token=CURRENT_SCOPE.set(None)
        self.model_cache=patch.dict('utils.model_selection._model_cache',{},clear=True)
        self.model_cache.start()

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        CURRENT_SCOPE.reset(self.scope_token)
        self.model_cache.stop()
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def test_startup_ignores_old_env_modes_and_activates_existing_contexts(self):
        cid,eid,_=await self.runtime.ingest(10,1,'Input',1)
        await self.runtime.process(cid,eid)
        async with self.pool.acquire() as conn:
            before=await conn.fetchval('SELECT state FROM cognitive_contexts WHERE id=$1',cid)
            await conn.execute("UPDATE cognitive_contexts SET authority='shadow',authority_explicit=TRUE WHERE id=$1",cid)
        from cognition.runtime import start_runtime,stop_runtime
        with patch.dict(os.environ,{'ARTI_COGNITION_MODE':'legacy','ARTI_COGNITION_MODEL':'ignored/old'}),\
                patch('cognition.runtime.CognitiveWorker.start') as start:
            runtime=await start_runtime(self.pool,interpreter=self.interpreter)
            try:
                self.assertTrue(runtime.strict); self.assertEqual(runtime.mode,'active')
                start.assert_called_once()
                async with self.pool.acquire() as conn:
                    row=await conn.fetchrow('SELECT authority,state,suppression_epoch FROM cognitive_contexts WHERE id=$1',cid)
                self.assertEqual(row['authority'],'active')
                self.assertEqual(row['state'],before)
                self.assertEqual(row['suppression_epoch'],1)
            finally:
                await stop_runtime()

    async def test_activation_fences_old_turn_and_is_idempotent(self):
        turn=await self.runtime.prepare(10,1,'Input',1)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_contexts SET authority='shadow' WHERE id=$1",turn.context_id)
        self.assertEqual(await self.runtime.activate_current_contexts(),1)
        self.assertEqual(await self.runtime.activate_current_contexts(),0)
        from cognition.delivery import send_with_receipt,DeliverySuppressed
        send=AsyncMock(return_value=NS(message_id=50))
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(send,(),dict(chat_id=10,text='Stale'),'message')
        send.assert_not_awaited()
        with self.assertRaises(ValueError):
            await self.runtime.set_authority(turn.context_id,'legacy')

    async def test_retired_rp_scene_stays_retired_and_new_scene_is_active(self):
        first=await self.runtime.prepare(10,1,'First',1,'rp')
        CURRENT_TURN.set(None)
        await self.runtime.new_scene(10)
        second=await self.runtime.prepare(10,1,'Second',2,'rp')
        await self.runtime.activate_current_contexts()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE id=$1',first.context_id),'shadow')
            self.assertEqual(await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE id=$1',second.context_id),'active')
        with self.assertRaises(SuppressedEvidence):
            await self.runtime.ingest(10,1,'Late input',3,'rp',context=first.event.context)
        with self.assertRaises(SuppressedEvidence):
            await self.runtime.ensure_context(first.event.context)
        with self.assertRaises(SuppressedEvidence):
            await self.runtime.set_authority(first.context_id,'active')

    async def test_empty_diagnostics_context_does_not_create_fake_observation(self):
        from cognition.diagnostics import active_context,profile_text
        cid=await active_context(self.runtime,10,'default')
        self.assertIn('Пока нет',await profile_text(self.runtime,cid,1))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_events'),0)
            self.assertEqual(await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE id=$1',cid),'active')

    async def test_retired_apis_cannot_write_or_retrieve_old_memory(self):
        from database.models import ChatEmotionalState,MemoryUserProfile,MemoryFact,UserEvent
        from memory.storage import build_memory_context,remember_exchange
        from memory.profiles import refresh_user_profile
        from memory.consolidator import consolidate_chat_facts
        from memory.timeline import build_timeline_events
        fid=await MemoryFact.create(10,'OLD_DERIVATIVE',user_id=1)
        with patch('config.genai_client.models.generate_content',side_effect=AssertionError('legacy provider call')):
            await ChatEmotionalState.get_or_create(10)
            await ChatEmotionalState.update_state(10,'angry',user_id=1)
            await ChatEmotionalState.apply_mood_delta(10,{'angry':1})
            await ChatEmotionalState.apply_turn_sentiment(10,'<!-- emotional_introspection: {} -->')
            await ChatEmotionalState.record_sticker_sent(10,'file','happy')
            await MemoryUserProfile.apply_reinforcement(10,1,'default','positive')
            await MemoryUserProfile.grow_closeness(10,1,'default')
            await MemoryUserProfile.upsert(10,1,'default',{},'Old profile')
            await UserEvent.add(10,AT.date(),'event','old')
            self.assertEqual(await build_memory_context(10,1,'OLD_DERIVATIVE'),'')
            await remember_exchange(10,1,'User','Hello','Output')
            self.assertEqual((await refresh_user_profile(10,1))['status'],'retired')
            self.assertEqual((await consolidate_chat_facts(10,dry_run=False))['status'],'retired')
            self.assertEqual((await build_timeline_events(10,dry_run=False))['status'],'retired')
        async with self.pool.acquire() as conn:
            for table in ('chat_emotional_states','memory_user_profiles','memory_timelines','user_events'):
                self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM '+table),0)
            self.assertIsNone(await conn.fetchval('SELECT archived_at FROM memory_facts WHERE id=$1',fid))

    async def test_menu_selection_is_shared_by_new_interpreter_and_can_change(self):
        from utils.model_selection import set_chat_model
        await set_chat_model(10,'fixture/first')
        calls=[]
        def handler(request):
            calls.append(json.loads(request.content)['model']); return completion({'appraisals':[]})
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)); self.addAsyncCleanup(client.aclose)
        interpreter=SelectedModelInterpreter(SelectedModelClient(client=client),max_attempts=1)
        with patch('config.OMNIROUTE_BASE_URL','https://fixture.invalid/v1'):
            await interpreter.interpret(event())
            await set_chat_model(10,'fixture/second')
            await interpreter.interpret(event())
        self.assertEqual(calls,['fixture/first','fixture/second'])

    async def test_history_excludes_old_summaries_unconfirmed_replies_and_erased_sources(self):
        from utils.chat_history import get_chat_context,save_chat_message
        from database.models import ChatHistory
        await ChatHistory.save(10,'Память','OLD_SUMMARY',AT.replace(tzinfo=None))
        await ChatHistory.save(10,'Арти','UNCONFIRMED_REPLY',AT.replace(tzinfo=None))
        with patch('cognition.runtime.get_runtime',return_value=self.runtime):
            await save_chat_message(10,'User','PERMITTED_SOURCE',1,message_id=1,occurred_at=AT)
            text=await get_chat_context(10)
            self.assertIn('PERMITTED_SOURCE',text)
            self.assertNotIn('OLD_SUMMARY',text); self.assertNotIn('UNCONFIRMED_REPLY',text)
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE cognitive_events SET suppressed_at=NOW()')
            self.assertEqual(await get_chat_context(10),'')

    async def test_clear_context_resets_dialogue_without_erasing_long_term_sources(self):
        from utils.chat_history import get_chat_context,save_chat_message
        from cognition.delivery import send_with_receipt,DeliverySuppressed
        with patch('cognition.runtime.get_runtime',return_value=self.runtime):
            await save_chat_message(10,'User','EARLIER_DIALOGUE',1,message_id=1,occurred_at=AT)
            turn=await self.runtime.prepare(10,1,'EARLIER_DIALOGUE',1)
            traces=await self.runtime.memory.artifacts(turn.context_id,1,'trace')
            await self.runtime.reset_history(10)
            self.assertEqual(await get_chat_context(10),'')
            remaining=await self.runtime.memory.artifacts(turn.context_id,1,'trace')
            self.assertEqual([(r['id'],r['payload']) for r in traces],[(r['id'],r['payload']) for r in remaining])
            with self.assertRaises(DeliverySuppressed):
                await send_with_receipt(AsyncMock(),(),dict(chat_id=10,text='Stale answer'),'message')
            CURRENT_TURN.set(None)
            await save_chat_message(10,'User','LATER_DIALOGUE',1,message_id=2,occurred_at=AT)
            text=await get_chat_context(10)
            self.assertIn('LATER_DIALOGUE',text); self.assertNotIn('EARLIER_DIALOGUE',text)

    async def test_group_chat_migration_fences_delivery_without_restoring_shadow_cognition(self):
        scope=TransportScope(-10,0,'group',1,1,addressed=True)
        token=CURRENT_SCOPE.set(scope)
        try:
            turn=await self.runtime.prepare(-10,1,'Input',1)
            await self.runtime.groups.migrate_chat(-10,-20)
            async with self.pool.acquire() as conn:
                row=await conn.fetchrow('SELECT authority,suppression_epoch FROM cognitive_contexts WHERE id=$1',turn.context_id)
            self.assertEqual(row['authority'],'active'); self.assertGreater(row['suppression_epoch'],turn.epoch)
            policy,_=await self.runtime.groups.policies.get(-20,0)
            self.assertEqual(policy.execution,'shadow'); self.assertFalse(policy.full_visibility)
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_photo_and_text_use_selected_model_and_new_expression(self):
        from utils.model_selection import set_chat_model
        from bot.queue import process_user_reply
        from bot.handlers import _process_images_impl
        await set_chat_model(10,'fixture/chosen')
        bot=NS(send_message=AsyncMock(return_value=NS(message_id=80)),send_chat_action=AsyncMock())
        response=AsyncMock(return_value=('A reply.',False,[],[]))
        with patch('cognition.runtime.get_runtime',return_value=self.runtime),\
                patch('materials.runtime.enabled',return_value=False),\
                patch('ai.generation.analyze_intent',new=AsyncMock(return_value={})),\
                patch('bot.queue.generate_response_stream',new=response):
            await process_user_reply(dict(chat_id=10,user_id=1,user_name='User',user_message='Hello there',
                message_id=1,context=NS(bot=bot),is_voice=False),bot)
        self.assertEqual(response.await_args.kwargs['model'],'fixture/chosen')
        self.assertIsNotNone(response.await_args.kwargs['expression_plan'])
        response.reset_mock(); CURRENT_TURN.set(None)
        with patch('cognition.runtime.get_runtime',return_value=self.runtime),\
                patch('bot.handlers.TTS_ENABLED',False),\
                patch('bot.handlers.generate_response_stream',new=response):
            await _process_images_impl(bot,10,1,'User',2,['synthetic-image'],'Describe this picture',False,True)
        self.assertEqual(response.await_args.kwargs['model'],'fixture/chosen')
        self.assertIsNotNone(response.await_args.kwargs['expression_plan'])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM chat_emotional_states'),0)
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM memory_user_profiles'),0)

    async def test_sticker_uses_transport_anti_repeat_and_cannot_bypass_plan(self):
        from ai.stickers import send_mood_sticker_task
        turn=await self.runtime.prepare(10,1,'Input',1)
        turn.expression=replace(turn.expression,sticker_mood='happy')
        async with self.pool.acquire() as conn:
            for i,value in enumerate(('a','b'),1):
                await conn.execute("""INSERT INTO cognitive_outbox(context_id,event_id,delivery_key,channel,payload,suppression_epoch,status,created_at)
                    VALUES($1,$2,$3,'sticker',$4::jsonb,$5,'delivered',$6)""",turn.context_id,turn.event_id,
                    'earlier:'+str(i),json.dumps({'sticker_id':value}),turn.epoch,AT-timedelta(minutes=10))
        bot=NS(send_sticker=AsyncMock())
        with patch('ai.stickers.STICKERS_ENABLED',True),patch('ai.stickers.load_sticker_pack',new=AsyncMock(return_value={'happy':['a','b','c']})):
            await send_mood_sticker_task(bot,10,1,'happy',1)
            self.assertEqual(bot.send_sticker.await_args.kwargs['sticker'],'c')
            bot.send_sticker.reset_mock()
            await send_mood_sticker_task(bot,10,1,'angry',1,force=True)
            bot.send_sticker.assert_not_awaited()
            CURRENT_TURN.set(None)
            await send_mood_sticker_task(bot,10,1,'happy',1,force=True)
            bot.send_sticker.assert_not_awaited()
