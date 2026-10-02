"""Context routing regressions; model responses are recorded fixtures, not accuracy claims."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import httpx
from ai.intents import resolve_intent, _bounded_context


def client(route='artifact',confidence=.96,reason='referent'):
    return NS(model_for=AsyncMock(return_value='fixture'),complete=AsyncMock(return_value=httpx.Response(200,
        json={'choices':[{'message':{'content':json.dumps(dict(route=route,confidence=confidence,reason=reason))}}]})))


class ContextualIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonkeyword_request_reaches_contextual_model(self):
        model=client()
        result=await resolve_intent('Различия удобнее увидеть бок о бок.',7,has_materials=True,allow_work=True,client=model,
            context={'dialogue':[{'role':'user','text':'Сравниваю два договора.'}], 'materials':{'count':2,'kinds':['document']}})
        self.assertEqual(result['work'],'artifact')
        submitted=json.loads(model.complete.await_args.args[1][1]['content'])
        self.assertEqual(submitted['context']['dialogue'][0]['text'],'Сравниваю два договора.')
        self.assertEqual(submitted['context']['materials']['count'],2)

    async def test_missing_reference_clarifies_without_work(self):
        model=client()
        result=await resolve_intent('Сделай это в виде схемы',7,allow_work=True,client=model)
        self.assertIsNone(result['work']); self.assertTrue(result['clarification'])
        model.complete.assert_not_awaited()

    async def test_resolved_reference_uses_recent_confirmed_context(self):
        model=client()
        result=await resolve_intent('И это тоже наглядно.',7,allow_work=True,client=model,
            context={'dialogue':[{'role':'assistant','text':'Есть данные о расходах по месяцам.'}],
                     'actions':{'reply_to_result':True}})
        self.assertEqual(result['work'],'artifact')

    async def test_negation_and_quotes_cannot_be_overridden_by_history(self):
        model=client()
        context={'dialogue':[{'role':'user','text':'Создай схему'}]}
        out=await resolve_intent('Теперь не создавай ничего, пока обсуждаем',7,allow_work=True,client=model,context=context)
        self.assertIsNone(out['work'])
        out=await resolve_intent('Переведи «Создай схему»',7,allow_work=True,client=model,context=context)
        self.assertIsNone(out['work'])
        self.assertEqual(model.complete.await_count,1)

    async def test_low_action_confidence_and_explicit_clarification(self):
        for model in (client(confidence=.7),client(route='clarify',reason='format')):
            result=await resolve_intent('Требуется более удобное представление',7,allow_work=True,client=model)
            self.assertIsNone(result['work']); self.assertIn('clarification',result)

    async def test_local_timeout_cannot_extend_entire_routing_budget(self):
        async def slow(*args,**kwargs): await asyncio.sleep(10)
        encoder=NS(encode=slow); model=client(route='chat')
        started=asyncio.get_running_loop().time()
        result=await resolve_intent('Нужна наглядность',7,allow_work=True,has_materials=True,local_encoder=encoder,client=model,timeout=.09)
        self.assertLess(asyncio.get_running_loop().time()-started,.3)
        self.assertIsNone(result['work'])

    def test_context_allowlist_does_not_copy_unknown_fields(self):
        result=_bounded_context({'token':'SECRET','materials':{'count':999,'kinds':['document','SECRET']},
            'actions':{'reply_to_result':True,'pending_question':'SECRET'},'dialogue':[{'role':'system','text':'SECRET'}]})
        self.assertNotIn('SECRET',json.dumps(result)); self.assertEqual(result['materials']['count'],20)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class RoutingContextSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.runtime import CognitiveRuntime
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(start_worker=False)

    async def asyncTearDown(self):
        from cognition.runtime import CURRENT_TURN
        CURRENT_TURN.set(None); await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def test_owner_and_erased_sources_never_enter_router_context(self):
        from bot.intent_context import routing_context
        first=await self.runtime.prepare(500,1,'PRIVATE_ONE',1)
        await self.runtime.prepare(500,2,'OTHER_OWNER_SECRET',2)
        current=await self.runtime.prepare(500,1,'Текущий запрос',3)
        result=await routing_context({},current)
        self.assertIn('PRIVATE_ONE',str(result)); self.assertNotIn('OTHER_OWNER_SECRET',str(result))
        from cognition.forgetting import forget_cognitive_sources
        await forget_cognitive_sources(self.pool,first.context_id,1,[first.event.evidence.source_id])
        later=await self.runtime.prepare(500,1,'После удаления',4)
        self.assertNotIn('PRIVATE_ONE',str(await routing_context({},later)))

    async def test_process_reply_asks_missing_reference_without_generation_or_agent_work(self):
        from bot.queue import process_user_reply
        from cognition.scope import CURRENT_SCOPE, TransportScope
        scope=TransportScope(500,-1,'private',1,33,True,sender_ref='user:1')
        token=CURRENT_SCOPE.set(scope)
        bot=NS(send_message=AsyncMock(return_value=NS(message_id=34)))
        request=dict(type='text',chat_id=500,user_id=1,user_name='Fixture',
            user_message='Сделай это в виде схемы',message_id=33,context=NS(bot=bot),
            is_voice=False,_telegram_scope=scope,_cognitive_source_ids=[])
        try:
            with patch.dict(os.environ,{'ARTI_AGENTS_ENABLED':'1','ARTI_MATERIALS_ENABLED':'1'}),\
                 patch('cognition.runtime.get_runtime',return_value=self.runtime),\
                 patch('bot.queue.generate_response_stream',new=AsyncMock()) as generation,\
                 patch('bot.agent_requests.handle_agent_request',new=AsyncMock()) as agent:
                await process_user_reply(request,bot)
            generation.assert_not_awaited(); agent.assert_not_awaited()
            self.assertIn('Уточни',bot.send_message.await_args.kwargs['text'])
        finally: CURRENT_SCOPE.reset(token)
