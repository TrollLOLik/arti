"""Offline integration of semantic hypotheses with public prompts and delivery.

Recorded annotations exercise contracts; they are not a model-quality score.
"""
import copy
import json
import os
import unittest
from datetime import timedelta
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.group_context import build_frame, public_understanding, PUBLIC_PACKET_BYTE_LIMIT, SEMANTIC_PACKET_BYTE_LIMIT
from cognition.group_understanding import parse_understanding
from cognition.proactivity import GroupService
from cognition.repositories import SuppressedEvidence
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from tests.cognition.test_affect import AT
from tests.cognition.test_full_model import RecordedInterpreter
from tests.cognition.test_proactive_context import message


def recorded_annotation(messages):
    """An explicitly recorded scenario: message 1 opens, 3 resolves, 75 reopens."""
    anchor=next((m for m in messages if m['message_id']==1),None)
    if anchor is None:
        return dict(threads=[],links=[],items=[])
    def evidence(m):
        return [dict(source_id=m['source_id'],start=0,end=len(m['text']),quote=m['text'])]
    item=dict(kind='question',thread_id=anchor['source_id'],origin_source_id=anchor['source_id'],
              summary='The import problem needs investigation.',actor_id=1,attribution='speaker',
              status='open',confidence=.9,evidence=evidence(anchor),updates=[])
    for m in messages:
        status={3:'resolved',75:'reopened'}.get(m['message_id'])
        if status:
            item['updates'].append(dict(source_id=m['source_id'],status=status,actor_id=m['owner_id'],
                                       attribution='speaker',confidence=.9,evidence=evidence(m)))
    return parse_understanding(dict(threads=[dict(thread_id=anchor['source_id'],label='Import problem',
          confidence=.9,evidence=evidence(anchor))],links=[],items=[item]),messages)


class RecordedAnalyzer:
    def __init__(self): self.calls=[]
    async def analyze(self,messages,chat_id):
        self.calls.append(copy.deepcopy(messages))
        return recorded_annotation(messages)
    async def close(self): pass


def semantic_snapshot():
    messages=[dict(source_id='public:1',message_id=1,owner_id=1,sender_kind='user',
        directed=False,reply_to_id=None,at=AT.isoformat(),text='Import crashes.',text_truncated=False)]
    return dict(payload=recorded_annotation(messages),generation=1,current=False,as_of_event_id=1,
                source_ids=['public:1','public:uncited'],source_event_ids=[1,999])


class UnderstandingPacketTests(unittest.IsolatedAsyncioTestCase):
    def test_packet_budget_includes_complete_semantic_objects(self):
        snapshot=semantic_snapshot()
        f=build_frame(1,-10,5,[message(i,'Я'*2500) for i in range(1,65)],understanding=snapshot)
        packet=f.public_packet(64)
        self.assertLessEqual(len(json.dumps(packet,ensure_ascii=False).encode()),PUBLIC_PACKET_BYTE_LIMIT)
        semantic=packet['semantic_conversation']
        self.assertLessEqual(len(json.dumps(semantic,ensure_ascii=False).encode()),SEMANTIC_PACKET_BYTE_LIMIT)
        self.assertIn(64,[m['message_id'] for m in packet['messages']])
        self.assertFalse(semantic['current']); self.assertFalse(semantic['bounds']['complete_history'])
        self.assertEqual(semantic['items'][0]['source_ids'],['public:1'])

    def test_all_hidden_consulted_lineage_survives_display_compaction(self):
        f=build_frame(1,-10,5,[message(64,'A different recent topic')],understanding=semantic_snapshot())
        self.assertIn('public:uncited',GroupService.frame_sources(f,64))
        self.assertNotIn('public:uncited',json.dumps(f.public_packet()))

    def test_unknown_nested_fields_are_not_exported(self):
        snapshot=semantic_snapshot()
        snapshot['payload']['items'][0]['evidence'][0]['private_memory']='PRIVATE_SENTINEL'
        snapshot['payload']['items'][0]['private_preferences']='PRIVATE_SENTINEL'
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(public_understanding(snapshot)))

    def test_oversized_item_is_omitted_whole_not_stripped_of_qualifications(self):
        snapshot=semantic_snapshot()
        snapshot['payload']['items'][0]['summary']='X'*7000
        value=public_understanding(snapshot)
        self.assertEqual(value['items'],[])
        self.assertEqual(value['bounds']['omitted_items'],1)

    async def test_judge_can_cite_old_semantic_source_without_raw_window_overlap(self):
        from ai.group_participation import OpenRouterGroupJudge
        judge=OpenRouterGroupJudge()
        judge.request=AsyncMock(return_value=dict(action='abstain',reason='resolved',usefulness=0.,
            interruption=0.,confidence=.9,evidence_ids=['public:1'],channel='text',defer_seconds=30))
        f=build_frame(1,-10,5,[message(64,'A different recent topic')],understanding=semantic_snapshot())
        result=await judge.assess(f,dict(message_id=64,kind='contextual'))
        self.assertEqual(result.evidence_ids,('public:1',))
        self.assertIn('actor-scoped',judge.request.await_args.args[0])

    def test_final_prompt_keeps_recent_correction_before_any_old_semantic_state(self):
        from cognition.group_context import GroupHistory
        from cognition.prompting import assemble_prompt,PromptBudget
        semantic=public_understanding(semantic_snapshot())
        semantic['items'][0]['status']='resolved'
        raw='[now] User 1: Correction: the issue is reopened and the old fix failed.'
        history=GroupHistory(raw,semantic)
        for capacity in (380,500,700,1000,1800,3000):
            final,_=assemble_prompt('System','Current task',history,model='unknown-model',
                budget=PromptBudget(context_tokens=capacity,output_tokens=0,tool_tokens=0,transport_tokens=0))
            self.assertIn('Correction: the issue is reopened',final)
            if GroupHistory.HEADER in final:
                encoded=final.split(GroupHistory.HEADER,1)[1].split('\n\nТекущее сообщение:',1)[0].strip()
                value=json.loads(encoded)
                self.assertTrue(value['hypotheses_only']);self.assertFalse(value['current'])
                self.assertIn('bounds',value)
            else:
                self.assertNotIn('"resolved"',final)

    def test_raw_overflow_omits_semantic_block_entirely(self):
        from cognition.group_context import GroupHistory
        from cognition.prompting import assemble_prompt,PromptBudget
        raw=('Old raw utterance. '*200)+'\nCurrent correction: reopen it.'
        final,_=assemble_prompt('System','Task',GroupHistory(raw,public_understanding(semantic_snapshot())),
            model='unknown-model',budget=PromptBudget(context_tokens=700,output_tokens=0,tool_tokens=0,transport_tokens=0))
        self.assertIn('Current correction: reopen it.',final)
        self.assertNotIn(GroupHistory.HEADER,final)
        self.assertNotIn('hypotheses_only',final)

    def test_human_reply_can_target_bot_without_attributing_it_to_human_owner(self):
        messages=[dict(source_id='b1',message_id=1,owner_id=1,sender_kind='bot',directed=True,reply_to_id=None,
            at=AT.isoformat(),text='I can compare the options.',text_truncated=False),
            dict(source_id='u2',message_id=2,owner_id=2,sender_kind='user',directed=False,reply_to_id=None,
            at=(AT+timedelta(seconds=1)).isoformat(),text='Please compare them, then.',text_truncated=False)]
        def evidence(row): return dict(source_id=row['source_id'],start=0,end=len(row['text']),quote=row['text'])
        result=parse_understanding(dict(threads=[dict(thread_id='b1',label='Comparison',confidence=.9,evidence=[evidence(messages[0])])],
            links=[dict(source_id='u2',target_source_id='b1',thread_id='b1',relation='continuation',
                addressee_ids=[],confidence=.9,evidence=[evidence(m) for m in messages])],items=[]),messages)
        self.assertEqual(result['links'][0]['target_source_id'],'b1')
        self.assertEqual(result['links'][0]['addressee_ids'],[])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class UnderstandingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database();self.pool=await self.db.__aenter__();self.at=AT
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),clock=lambda:self.at).initialize(False)
        self.analyzer=RecordedAnalyzer();self.runtime.groups.understanding.analyzer=self.analyzer
        self.tokens=[(var,var.set(None)) for var in (CURRENT_SCOPE,CURRENT_TURN)]
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-10,TRUE)')
    async def asyncTearDown(self):
        for var,token in self.tokens: var.reset(token)
        await self.runtime.close();await self.db.__aexit__(None,None,None)
    async def observe(self,mid,text,owner=1,directed=False):
        self.at+=timedelta(seconds=1)
        scope=TransportScope(-10,5,'supergroup',owner,mid,directed,'user')
        token=CURRENT_SCOPE.set(scope)
        try: return await self.runtime.groups.observe(scope,text,at=self.at)
        finally: CURRENT_SCOPE.reset(token)
    async def seeded(self):
        cid=await self.observe(1,'The importer crashes on startup.')
        await self.observe(2,'Dinner can start later.',owner=2)
        await self.observe(3,'I verified the importer; the issue is resolved.')
        self.assertEqual(await self.runtime.groups.understanding.refresh(cid),1)
        return cid
    async def direct_turn(self):
        await self.observe(4,'Arti, what did we decide?',directed=True)
        scope=TransportScope(-10,5,'supergroup',1,4,True,'user')
        CURRENT_SCOPE.set(scope)
        turn=await self.runtime.prepare(-10,1,'Arti, what did we decide?',4)
        history=await self.runtime.groups.history(scope)
        return turn,history
    async def execute(self,sql,*args):
        async with self.pool.acquire() as conn: return await conn.execute(sql,*args)

    async def test_history_includes_guarded_snapshot_and_mark_included_preserves_lineage(self):
        cid=await self.seeded();turn,history=await self.direct_turn()
        self.assertIn('Import problem',history);self.assertIn('actor_only',history)
        old=set(turn.group_context_event_ids)
        self.assertGreaterEqual(len(old),4)
        await self.runtime.mark_included(turn,[])
        self.assertTrue(old.issubset(turn.supporting_event_ids))
        await self.runtime.groups.validate_context(turn)
        self.assertEqual(self.analyzer.calls.__len__(),1)

    async def test_unguarded_history_omits_generated_summary(self):
        await self.seeded()
        history=await self.runtime.groups.history(TransportScope(-10,5,'supergroup',1))
        self.assertNotIn('Import problem',history)
        self.assertIn('importer crashes',history)

    async def test_optout_of_uncited_input_rejects_history_and_clears_snapshot(self):
        cid=await self.seeded();turn,_=await self.direct_turn()
        await self.runtime.groups.policies.opt_out(-10,2,True)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)
        self.assertIsNone((await self.runtime.groups.frame(cid)).understanding.get('payload'))
        self.assertNotIn('Dinner',str((await self.runtime.groups.frame(cid)).public_packet()))

    async def test_raw_edit_rejects_previous_history_even_if_old_event_unsuppressed(self):
        await self.seeded();turn,_=await self.direct_turn()
        self.at+=timedelta(seconds=1)
        await self.runtime.groups.observe(TransportScope(-10,5,'supergroup',2,2,False,'user'),
            'The dinner was cancelled.',at=self.at,edited=True)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_snapshot_refresh_rejects_saved_generation(self):
        cid=await self.seeded();turn,_=await self.direct_turn()
        old=turn.group_understanding_generation
        self.assertEqual(await self.runtime.groups.understanding.refresh(cid),1)
        self.assertNotEqual((await self.runtime.groups.frame(cid)).understanding['generation'],old)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_resume_revalidates_saved_history_snapshot(self):
        import cognition.runtime as runtime_module
        from bot.request_codec import encode_value,decode_value
        await self.seeded();turn,_=await self.direct_turn()
        encoded=await encode_value(turn)
        with patch.object(runtime_module,'_runtime',self.runtime):
            restored=await decode_value(encoded)
            self.assertEqual(restored.group_context_source_ids,turn.group_context_source_ids)
            await self.runtime.groups.policies.opt_out(-10,2,True)
            with self.assertRaises(SuppressedEvidence): await decode_value(encoded)

    async def test_dispatch_guard_blocks_revoked_source(self):
        from ai.generation import _guard_group_context
        await self.seeded();await self.direct_turn()
        await self.runtime.groups.policies.opt_out(-10,2,True)
        with self.assertRaises(SuppressedEvidence): await _guard_group_context()

    async def test_final_delivery_guard_blocks_revocation(self):
        from cognition.delivery import send_with_receipt,DeliverySuppressed
        await self.seeded();await self.direct_turn()
        await self.runtime.groups.policies.opt_out(-10,2,True)
        sender=AsyncMock(return_value=NS(message_id=900))
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(sender,(),dict(chat_id=-10,text='Previous summary'),'message')
        sender.assert_not_awaited()

    async def test_retention_expiry_without_writes_rejects_snapshot(self):
        await self.seeded();turn,_=await self.direct_turn()
        self.at+=timedelta(days=31)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_background_only_no_ingress_model_call(self):
        for mid in range(1,16): await self.observe(mid,'A questionless ordinary public utterance.',owner=1+mid%3)
        self.assertEqual(self.analyzer.calls,[])
        self.assertEqual(self.runtime.interpreter.calls,0)

    async def test_new_human_turn_invalidates_direct_historical_snapshot(self):
        await self.seeded();turn,_=await self.direct_turn()
        await self.observe(5,'Correction: we should reopen the importer issue.',owner=1)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_two_parts_of_own_reply_can_advance_only_own_receipt_revision(self):
        from cognition.delivery import send_with_receipt
        await self.seeded();turn,_=await self.direct_turn()
        sender=AsyncMock(side_effect=[NS(message_id=900,text='First part'),NS(message_id=901,text='Second part')])
        await send_with_receipt(sender,(),dict(chat_id=-10,text='First part'),'message')
        await send_with_receipt(sender,(),dict(chat_id=-10,text='Second part'),'message')
        self.assertEqual(sender.await_count,2)
        await self.observe(6,'Wait, the plan changed.',owner=2)
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_raw_only_in_place_edit_detected_by_manifest(self):
        from cognition.serialization import object_value,dump
        await self.observe(1,'The original source text.')
        turn,_=await self.direct_turn()
        self.assertIsNone(getattr(turn,'group_understanding_generation',None))
        async with self.pool.acquire() as conn,conn.transaction():
            row=await conn.fetchrow('SELECT e.id,e.payload,o.payload AS observation FROM cognitive_events e JOIN group_observations o ON o.event_id=e.id WHERE o.context_id=$1 AND o.message_id=1',turn.context_id)
            event=object_value(row['payload']);event['text']='Corrected in place.'
            observation=object_value(row['observation']);observation['text']=event['text']
            await conn.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1',row['id'],dump(event))
            await conn.execute('UPDATE group_observations SET payload=$2::jsonb WHERE event_id=$1',row['id'],dump(observation))
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_raw_only_new_dependency_detected_by_manifest(self):
        cid=await self.observe(1,'The original source text.')
        await self.observe(2,'Related public evidence.',owner=2)
        turn,_=await self.direct_turn()
        async with self.pool.acquire() as conn:
            ids=await conn.fetch('SELECT event_id,message_id FROM group_observations WHERE context_id=$1',cid)
            mapping={r['message_id']:r['event_id'] for r in ids}
            await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,mapping[1],mapping[2])
        with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)

    async def test_optout_before_proactive_provider_dispatch_blocks_raw_and_semantic_packet(self):
        from cognition.initiative_policy import provider_slot as original
        from tests.cognition.test_groups import RecordedJudge
        cid=await self.seeded()
        await self.runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,timezone='UTC',spacing_seconds=60))
        await self.observe(5,'Who can investigate the next deployment?',owner=3)
        judge=RecordedJudge();self.runtime.groups.judge=judge
        @asynccontextmanager
        async def slot(*args,**kwargs):
            async with original(*args,**kwargs) as admitted:
                if args[3]=='group_assess': await self.runtime.groups.policies.opt_out(-10,2,True)
                yield admitted
        self.at+=timedelta(seconds=60)
        bot=NS(send_message=AsyncMock(),set_message_reaction=AsyncMock())
        with patch('cognition.initiative_policy.provider_slot',slot): await self.runtime.groups.run_cycle(bot)
        self.assertEqual(judge.assess_calls,0);self.assertEqual(judge.compose_calls,0)
        bot.send_message.assert_not_awaited()

    async def test_optout_before_compose_dispatch_blocks_already_assessed_frame(self):
        from cognition.initiative_policy import provider_slot as original
        from tests.cognition.test_groups import RecordedJudge
        await self.seeded()
        await self.runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,timezone='UTC',spacing_seconds=60))
        await self.observe(5,'Who can investigate the next deployment?',owner=3)
        judge=RecordedJudge();self.runtime.groups.judge=judge
        @asynccontextmanager
        async def slot(*args,**kwargs):
            async with original(*args,**kwargs) as admitted:
                if args[3]=='group_compose': await self.runtime.groups.policies.opt_out(-10,2,True)
                yield admitted
        self.at+=timedelta(seconds=60)
        bot=NS(send_message=AsyncMock(),set_message_reaction=AsyncMock())
        with patch('cognition.initiative_policy.provider_slot',slot): await self.runtime.groups.run_cycle(bot)
        self.assertEqual(judge.assess_calls,1);self.assertEqual(judge.compose_calls,0)
        bot.send_message.assert_not_awaited()

    async def test_guards_bound_busy_pool(self):
        import asyncio
        await self.seeded();turn,_=await self.direct_turn()
        async def wait(*args,**kwargs): await asyncio.sleep(5)
        with patch.object(self.runtime.groups,'_validate_context',side_effect=wait):
            started=asyncio.get_running_loop().time()
            with self.assertRaises(SuppressedEvidence): await self.runtime.groups.validate_context(turn)
            self.assertLess(asyncio.get_running_loop().time()-started,1.8)

    async def test_ambiguous_replyless_resolution_reaches_semantic_arbiter(self):
        from tests.cognition.test_groups import RecordedJudge
        await self.runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,timezone='UTC',spacing_seconds=60))
        cid=await self.observe(1,'How do I deploy the importer?')
        await self.observe(2,'Does your repaired printer work?',owner=2)
        await self.observe(3,'It works.',owner=1)
        judge=RecordedJudge();judge.action='abstain';judge.reason='human_addressed';self.runtime.groups.judge=judge
        self.at+=timedelta(seconds=60)
        await self.runtime.groups.run_cycle(NS(send_message=AsyncMock()))
        self.assertGreaterEqual(judge.assess_calls,1)
        async with self.pool.acquire() as conn:
            reason=await conn.fetchval('SELECT reason FROM group_decisions WHERE context_id=$1 ORDER BY id LIMIT 1',cid)
        self.assertEqual(reason,'human_addressed')

    async def test_maintenance_skips_context_lock_before_touching_observations(self):
        import asyncio
        cid=await self.observe(1,'A public source to expire.')
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
            await asyncio.wait_for(self.runtime.groups.maintenance(),1)
            self.assertIsNotNone(await conn.fetchval('SELECT payload FROM group_observations WHERE context_id=$1',cid))
        # Use a deterministic retention expiry rather than the process clock.
        await self.execute("UPDATE group_observations SET observed_at=NOW()-INTERVAL '100 days' WHERE context_id=$1",cid)
        await self.runtime.groups.maintenance()
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval('SELECT payload FROM group_observations WHERE context_id=$1',cid))
