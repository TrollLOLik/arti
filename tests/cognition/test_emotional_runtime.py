"""Synthetic delayed appraisals and response-boundary privacy/restart contracts.

No Telegram, provider requests, private conversations or production database.
"""
import asyncio
import os
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from cognition.types import Perception, PERCEPTION_VERSION, Origin
from cognition.serialization import object_value
from cognition.repositories import SuppressedEvidence
from tests.cognition.test_full_model import RecordedInterpreter, situation


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class EmotionalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import CURRENT_REQUEST
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.tokens=[(v,v.set(None)) for v in (CURRENT_TURN,CURRENT_SCOPE,CURRENT_REQUEST)]
        self.interpreter=RecordedInterpreter()
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,'active',strict=True,
                                           prepare_budget=.01).initialize(False)
        self.runtime_patch=patch('cognition.runtime.get_runtime',return_value=self.runtime)
        self.runtime_patch.start()

    async def asyncTearDown(self):
        self.runtime_patch.stop()
        await self.runtime.close()
        for var,token in reversed(self.tokens): var.reset(token)
        await self.db.__aexit__(None,None,None)

    async def delayed(self,text='I lost my project.',kind='loss',*,owner=1,chat=10,message=1):
        gate=asyncio.Event(); entered=asyncio.Event()
        async def interpret(ev,**kwargs):
            entered.set(); await gate.wait()
            return NS(perception=Perception(ev.event_id,PERCEPTION_VERSION,(),situation(ev,kind=kind)))
        self.runtime.interpreter=NS(interpret=interpret)
        turn=await self.runtime.prepare(chat,owner,text,message)
        self.assertTrue(entered.is_set())
        self.assertTrue(turn.expression_pending)
        self.assertEqual(turn.expression.behaviors,())
        self.assertFalse(turn.expression.uncertain_intent)
        return turn,gate

    async def finish_interpretation(self,gate):
        tasks=tuple(self.runtime.foreground)
        gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks),3)

    async def test_late_interpretation_refreshes_current_response_without_model_retry(self):
        turn,gate=await self.delayed()
        await self.finish_interpretation(gate)
        self.assertEqual(turn.expression.behaviors,())
        self.assertTrue(await self.runtime.refresh_expression(turn))
        self.assertEqual(turn.expression.behaviors,('acknowledge_loss','offer_choice'))
        self.assertFalse(turn.expression_pending)
        self.assertTrue(turn.expression_frozen)
        async with self.pool.acquire() as conn:
            record=object_value(await conn.fetchval("SELECT payload FROM cognitive_artifacts WHERE kind='regulation' AND context_id=$1",turn.context_id))
            self.assertFalse(record['interpretation_pending'])
            self.assertEqual(record['behaviors'],['acknowledge_loss','offer_choice'])
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_effects WHERE event_id=$1",turn.event_id),1)

    async def test_still_pending_is_bounded_neutral_and_never_restyles_after_generation(self):
        turn,gate=await self.delayed()
        started=time.monotonic()
        self.assertFalse(await self.runtime.refresh_expression(turn))
        self.assertLess(time.monotonic()-started,.5)
        frozen=turn.expression
        await self.finish_interpretation(gate)
        self.assertFalse(await self.runtime.refresh_expression(turn))
        self.assertEqual(turn.expression,frozen)
        self.assertIsNone(turn.expression.sticker_mood)
        self.assertEqual(turn.expression.regulation,'acknowledge')

    async def test_refresh_timeout_leaves_turn_unmodified_and_joins_cancelled_work(self):
        turn,gate=await self.delayed(); original=turn.expression
        cancelled=asyncio.Event()
        async def busy(*args):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch.object(self.runtime,'_prepare_expression',new=busy):
            self.assertFalse(await self.runtime.refresh_expression(turn,budget=.01))
        self.assertTrue(cancelled.is_set()); self.assertEqual(turn.expression,original)
        self.assertTrue(turn.expression_frozen)
        await self.finish_interpretation(gate)

    async def test_concurrent_snapshots_do_not_acquire_nested_pool_connections(self):
        self.runtime.strict=False
        count=self.pool.get_max_size()
        turns=[await self.runtime.prepare(100+i,i+1,'Synthetic input',1) for i in range(count)]
        barrier=asyncio.Barrier(count)
        original=self.runtime.personal_state
        async def synchronized(cid,owner,**kwargs):
            await barrier.wait()
            return await original(cid,owner,**kwargs)
        with patch.object(self.runtime,'personal_state',new=synchronized):
            await asyncio.wait_for(asyncio.gather(*(self.runtime._prepare_expression(turn) for turn in turns)),3)
        self.assertEqual(self.pool.get_idle_size(),self.pool.get_size())

    async def test_real_generation_prompt_uses_fresh_plan_and_delivery_style_same_object(self):
        from ai import generation
        turn,gate=await self.delayed(); await self.finish_interpretation(gate)
        provider=AsyncMock(return_value=NS(text='Synthetic response',candidates=[]))
        with patch.object(generation.genai_client.aio.models,'generate_content',new=provider):
            result=await generation.generate_response_stream(10,turn.event.text,'User','',
                custom_system_prompt='Synthetic persona.',expression_plan=turn.expression,request_intent={})
        self.assertEqual(result[0],'Synthetic response')
        self.assertIn('without forced optimism',provider.await_args.kwargs['config'].system_instruction)
        self.assertIs(CURRENT_TURN.get(),turn)
        self.assertEqual(turn.expression.behaviors,('acknowledge_loss','offer_choice'))

    async def test_direct_generator_refreshes_after_its_own_intent_routing(self):
        from ai import generation
        turn,gate=await self.delayed()
        async def route(*args):
            await self.finish_interpretation(gate)
            return {}
        provider=AsyncMock(return_value=NS(text='Synthetic direct response',candidates=[]))
        with patch.object(generation,'analyze_intent',new=route),patch.object(generation.genai_client.aio.models,'generate_content',new=provider):
            result=await generation.generate_response_stream(10,turn.event.text,'User','',
                custom_system_prompt='Synthetic persona.',expression_plan=turn.expression)
        self.assertEqual(result[0],'Synthetic direct response')
        self.assertFalse(turn.expression_pending)
        self.assertIn('without forced optimism',provider.await_args.kwargs['config'].system_instruction)

    async def test_auxiliary_generation_without_plan_does_not_freeze_or_inherit_emotion(self):
        from ai import generation
        turn,gate=await self.delayed(); await self.finish_interpretation(gate)
        provider=AsyncMock(return_value=NS(text='Short title',candidates=[]))
        with patch.object(generation.genai_client.aio.models,'generate_content',new=provider):
            result=await generation.generate_response_stream(10,'Write a title','User','',
                custom_system_prompt='Return only a title.',request_intent={})
        self.assertEqual(result[0],'Short title')
        self.assertNotIn('Regulation:',provider.await_args.kwargs['config'].system_instruction)
        self.assertFalse(turn.expression_frozen)
        self.assertTrue(await self.runtime.refresh_expression(turn))
        self.assertEqual(turn.expression.behaviors,('acknowledge_loss','offer_choice'))

    async def test_revoked_material_is_rejected_before_direct_intent_provider(self):
        from ai import generation
        from materials.types import MaterialError
        route=AsyncMock(return_value={})
        with patch('materials.runtime.guard_current',new=AsyncMock(side_effect=MaterialError('access_revoked'))),patch.object(generation,'analyze_intent',new=route):
            with self.assertRaises(MaterialError):
                await generation.generate_response_stream(10,'Synthetic revoked source','User','',
                    custom_system_prompt='Synthetic persona.')
        route.assert_not_awaited()

    async def test_restart_before_response_can_refresh_but_response_checkpoint_stays_frozen(self):
        from bot.request_runtime import CURRENT_REQUEST,checkpoint,store
        from bot.request_codec import encode_value,decode_value
        turn,gate=await self.delayed(); saved=await encode_value(turn)
        await self.finish_interpretation(gate)
        restored=await decode_value(saved)
        self.assertFalse(restored.expression_frozen)
        self.assertTrue(await self.runtime.refresh_expression(restored))
        CURRENT_TURN.set(restored)
        await store().enqueue('text',10,-1,'emotional-restart',{})
        job=await store().claim(['text']); CURRENT_REQUEST.set(job)
        provider=AsyncMock(return_value='Frozen response')
        self.assertEqual(await checkpoint('response',provider),'Frozen response')
        await store().release(job['id'],job['token'])
        CURRENT_REQUEST.set(await store().claim(['text'])); CURRENT_TURN.set(None)
        with patch.object(self.runtime,'refresh_expression',new=AsyncMock(side_effect=AssertionError('restyling cached output'))):
            self.assertEqual(await checkpoint('response',provider),'Frozen response')
        provider.assert_awaited_once()
        frozen=CURRENT_TURN.get()
        self.assertTrue(frozen.expression_frozen)
        self.assertEqual(frozen.expression,restored.expression)
        self.assertEqual(frozen.expression_support_event_ids,restored.expression_support_event_ids)

    async def test_reset_and_source_erasure_fence_refresh_and_saved_turn(self):
        from bot.request_codec import encode_value,decode_value
        turn,gate=await self.delayed(); await self.finish_interpretation(gate)
        saved=await encode_value(turn)
        await self.runtime.reset_history(10)
        with self.assertRaises(SuppressedEvidence): await self.runtime.refresh_expression(turn)
        with self.assertRaises(SuppressedEvidence): await decode_value(saved)
        self.runtime.strict=False
        self.runtime.interpreter=self.interpreter
        current=await self.runtime.prepare(10,1,'Fresh input',2)
        from cognition.forgetting import forget_cognitive_sources
        await forget_cognitive_sources(self.pool,current.context_id,1,[current.event.evidence.source_id])
        with self.assertRaises(SuppressedEvidence): await self.runtime.refresh_expression(current)

    async def test_public_owner_scope_and_emotional_provenance_survive_memory_budget(self):
        self.runtime.strict=False
        CURRENT_SCOPE.set(TransportScope(-10,4,'supergroup',1,1,True,'user'))
        first=await self.runtime.prepare(-10,1,'PRIVATE_OWNER_ONE',1)
        CURRENT_SCOPE.set(TransportScope(-10,4,'supergroup',2,2,True,'user'))
        second=await self.runtime.prepare(-10,2,'Owner two request',2)
        self.assertNotIn(first.event_id,second.expression_support_event_ids)
        self.assertEqual(second.expression.disclosure,0.)
        await self.runtime.mark_included(second,[])
        self.assertIn(second.event_id,second.supporting_event_ids)
        self.assertNotIn(first.event_id,second.supporting_event_ids)

    async def test_own_action_review_requires_current_owner_source_and_remains_a_report(self):
        from cognition.memory_repository import key
        self.runtime.strict=False
        first=await self.runtime.prepare(10,1,'Initial input',1)
        cid,action_id,action=await self.runtime.ingest(10,1,'Synthetic delivered answer',90,origin=Origin.DELIVERED_ACTION)
        await self.runtime.process(cid,action_id)
        review_turn=await self.runtime.prepare(10,1,'Check your earlier answer',2)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            await self.runtime.memory._put(conn,cid,'own_action_review',key('synthetic_review',action_id),
                dict(action_source=action.evidence.source_id,explanation='User disputes earlier answer',confidence=.9,status='reported_review'),
                1,[action_id,review_turn.event_id])
        self.assertTrue(await self.runtime.refresh_expression(review_turn))
        self.assertIn('repair',review_turn.expression.behaviors)
        self.assertIn('revise_understanding',review_turn.expression.behaviors)
        self.assertIn(action_id,review_turn.expression_support_event_ids)
        later=await self.runtime.prepare(10,1,'New request',3)
        self.assertNotIn('repair',later.expression.behaviors)
        other=await self.runtime.prepare(10,2,'Other owner request',4)
        self.assertNotIn('repair',other.expression.behaviors)
        self.assertNotIn(action_id,other.expression_support_event_ids)

    async def test_regulation_feedback_closes_only_explicit_source_linked_uncertainty(self):
        from cognition.memory_repository import key
        self.runtime.strict=False
        text='Was that meant as a joke?'
        cid,eid,event=await self.runtime.ingest(10,1,text,1)
        self.interpreter.frames[text]=situation(event,kind='conflict',intention_evidence='ambiguous')
        first=await self.runtime.prepare(10,1,text,1)
        async def record():
            async with self.pool.acquire() as conn:
                return (await self.runtime.memory._get(conn,cid,key('regulation',first.event.event_id)))[1]
        self.assertTrue((await record())['pending'])
        # A later unrelated message is not evidence the uncertainty resolved.
        await self.runtime.prepare(10,1,'Another topic',2)
        self.assertTrue((await record())['pending'])
        correction='I meant that as an accidental typo.'
        _,feedback_id,feedback=await self.runtime.ingest(10,1,correction,3)
        revised=replace(situation(feedback,kind='clarification'),revisions=(dict(
            span=0,source_id=event.evidence.source_id,interpretation='The wording was accidental',
            confidence=.9,attribution='accidental'),))
        self.interpreter.frames[correction]=revised
        await self.runtime.process(cid,feedback_id)
        result=await record()
        self.assertFalse(result['pending'])
        self.assertEqual(result['feedback_status'],'reported_resolution')
        self.assertEqual(result['resolution_event_id'],feedback_id)
        await self.runtime.refresh_expression(first)
        self.assertFalse(first.expression.uncertain_intent)
        self.assertFalse((await record())['pending'])
        self.assertEqual((await record())['resolution_event_id'],feedback_id)
        async with self.pool.acquire() as conn:
            aid=await conn.fetchval('SELECT id FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2',cid,key('regulation',event.event_id))
            sources=await conn.fetchval('SELECT ARRAY_AGG(source_event_id) FROM cognitive_provenance WHERE artifact_id=$1',aid)
        self.assertIn(eid,sources); self.assertIn(feedback_id,sources)

    async def test_delivered_reply_then_grounded_user_revision_enters_repair_pipeline(self):
        from cognition.delivery import send_with_receipt
        self.runtime.strict=False
        initial=await self.runtime.prepare(10,1,'Initial question',1)
        await send_with_receipt(AsyncMock(return_value=NS(message_id=91,text='Synthetic earlier answer')),(),
                               dict(chat_id=10,text='Synthetic earlier answer'),'message')
        async with self.pool.acquire() as conn:
            action=await conn.fetchrow("SELECT id,source_id FROM cognitive_events WHERE context_id=$1 AND origin='delivered_action'",initial.context_id)
        await self.runtime.process(initial.context_id,action['id'])
        text='That earlier answer misses the stated condition.'
        cid,eid,event=await self.runtime.ingest(10,1,text,2)
        self.interpreter.frames[text]=replace(situation(event,kind='clarification'),revisions=(dict(
            span=0,source_id=action['source_id'],interpretation='The answer is disputed by the user',
            confidence=.9,attribution='unknown'),))
        turn=await self.runtime.prepare(10,1,text,2)
        self.assertEqual(turn.expression.behaviors,('revise_understanding','repair'))
        self.assertIn('if the claim is unsupported',turn.expression.instruction())
        self.assertNotIn('The answer is disputed',turn.expression.instruction())
        self.assertIn(action['id'],turn.expression_support_event_ids)
        records=await self.runtime.memory.artifacts(cid,1,'own_action_review')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['payload']['status'],'reported_review')
