"""Synthetic group races, privacy fences and direct-response fairness; no providers."""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import patch

from ai.group_participation import GroupJudgement
from cognition.delivery import DeliverySuppressed, send_with_receipt
from cognition.forgetting import forget_cognitive_sources
from cognition.proactivity import GroupService
from cognition.scope import TransportScope
from cognition.serialization import dump, load_event, object_value
from tests.cognition import test_groups as group_tests


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class ProactiveGroupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=group_tests.GroupDatabaseTests.asyncSetUp
    asyncTearDown=group_tests.GroupDatabaseTests.asyncTearDown
    observe=group_tests.GroupDatabaseTests.observe
    candidate=group_tests.GroupDatabaseTests.candidate
    tick=group_tests.GroupDatabaseTests.tick

    async def scalar(self,sql,*args):
        async with self.pool.acquire() as conn: return await conn.fetchval(sql,*args)

    async def change(self,text='The migration now passes; we are already shipping it.',i=2,**kwargs):
        self.at+=timedelta(seconds=1)
        return await self.observe(i,text,**kwargs)

    async def test_semantic_resolution_during_assessment_is_reassessed_before_compose(self):
        cid=await self.observe()
        original=self.judge.assess
        async def assess(frame,p):
            answer=await original(frame,p)
            if self.judge.assess_calls==1:
                await self.change()
                return answer
            return GroupJudgement('abstain','already_answered',.1,.8,.95,())
        self.judge.assess=assess
        await self.tick()
        self.assertEqual(self.judge.assess_calls,2)
        self.assertEqual(self.judge.compose_calls,0)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.scalar('SELECT status FROM group_candidates WHERE context_id=$1 ORDER BY id LIMIT 1',cid),'abstained')

    async def test_semantic_resolution_during_compose_discards_old_plan(self):
        cid=await self.observe()
        original=self.judge.assess
        async def assess(frame,p):
            answer=await original(frame,p)
            if any('already shipping' in m['text'] for m in frame.messages):
                return GroupJudgement('abstain','already_answered',.1,.8,.95,())
            return answer
        self.judge.assess=assess
        self.judge.hook=self.change
        await self.tick()
        self.assertEqual(self.judge.assess_calls,2)
        self.assertEqual(self.judge.compose_calls,1)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.scalar('SELECT status FROM group_candidates WHERE context_id=$1 ORDER BY id LIMIT 1',cid),'abstained')

    async def test_changed_requirement_recomposes_instead_of_restamping_old_text(self):
        cid=await self.observe()
        generated=[]
        async def compose(frame,*args):
            result='Use blue.' if any('blue environment' in m['text'] for m in frame.messages) else 'Use red.'
            generated.append(result)
            if len(generated)==1: await self.change('We now need the blue environment.')
            return result
        self.judge.compose=compose
        await self.tick()
        self.assertEqual(generated,['Use red.','Use blue.'])
        self.assertEqual(self.judge.assess_calls,2)
        self.assertEqual(self.bot.send_message.await_args.kwargs['text'],'Use blue.')
        self.assertEqual(await self.scalar('SELECT status FROM group_candidates WHERE context_id=$1 ORDER BY id LIMIT 1',cid),'delivered')

    async def test_continuous_changes_exhaust_one_retry_and_abstain(self):
        cid=await self.observe()
        async def churn(): await self.change('Additional changed requirement '+str(self.judge.compose_calls),i=10+self.judge.compose_calls)
        self.judge.hook=churn
        await self.tick()
        self.assertEqual(self.judge.assess_calls,2)
        self.assertEqual(self.judge.compose_calls,2)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.scalar('SELECT status FROM group_candidates WHERE context_id=$1 ORDER BY id LIMIT 1',cid),'cancelled')
        self.assertEqual(await self.scalar('SELECT reason FROM group_decisions WHERE context_id=$1 ORDER BY id DESC LIMIT 1',cid),'conversation_changed')

    async def test_final_guard_rejects_change_after_last_semantic_snapshot(self):
        cid=await self.observe()
        async def intervene(method,args,kwargs,channel):
            await self.change('We have a different request now.',directed=True)
            return await send_with_receipt(method,args,kwargs,channel)
        with patch('cognition.delivery.send_with_receipt',side_effect=intervene): await self.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_outbox'),0)
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')

    async def test_forgotten_nonanchor_during_assessment_never_reaches_composer(self):
        cid=await self.observe()
        await self.change('PUBLIC_CANARY_TO_FORGET',owner=2)
        original=self.judge.assess
        async def forget(frame,p):
            judgement=await original(frame,p)
            source=next(m['source_id'] for m in frame.messages if m['text']=='PUBLIC_CANARY_TO_FORGET')
            await forget_cognitive_sources(self.pool,cid,2,[source])
            return judgement
        self.judge.assess=forget
        await self.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.judge.compose_calls,0)

    async def test_forgotten_uncited_nonanchor_during_compose_cannot_escape_new_epoch(self):
        cid=await self.observe()
        await self.change('UNREFERENCED_CANARY',owner=2)
        async def assess(frame,p):
            self.judge.assess_calls+=1
            return GroupJudgement('speak','useful_answer',.9,.1,.9,(frame.messages[0]['source_id'],))
        async def compose(frame,*args):
            source=next(m['source_id'] for m in frame.messages if m['text']=='UNREFERENCED_CANARY')
            await forget_cognitive_sources(self.pool,cid,2,[source])
            return 'UNREFERENCED_CANARY'
        self.judge.assess=assess; self.judge.compose=compose
        await self.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_outbox'),0)

    async def test_provider_admission_rechecks_suppression_before_composing(self):
        cid=await self.observe()
        await self.change('CANARY_BEFORE_PROVIDER',owner=2)
        from cognition.initiative_policy import provider_slot
        @asynccontextmanager
        async def slot(runtime,cid,owner,kind,**kwargs):
            async with provider_slot(runtime,cid,owner,kind,**kwargs) as admitted:
                if kind=='group_compose':
                    source=await self.scalar('SELECT source_id FROM cognitive_events WHERE context_id=$1 AND owner_id=2',cid)
                    await forget_cognitive_sources(self.pool,cid,2,[source])
                yield admitted
        with patch('cognition.initiative_policy.provider_slot',slot): await self.tick()
        self.assertEqual(self.judge.compose_calls,0)
        self.bot.send_message.assert_not_awaited()

    async def test_frame_never_stamps_old_observations_with_new_revision(self):
        cid=await self.observe()
        original=self.runtime.groups.policies.get
        fired=False
        async def interleaved(chat,topic,connection=None):
            nonlocal fired
            result=await original(chat,topic,connection)
            if connection is not None and not fired:
                fired=True
                await self.change('Concurrent observation',directed=True)
            return result
        with patch.object(self.runtime.groups.policies,'get',side_effect=interleaved):
            frame=await self.runtime.groups.frame(cid)
        self.assertEqual(frame.revision,1)
        self.assertEqual([m['message_id'] for m in frame.messages],[1])
        latest=await self.runtime.groups.frame(cid)
        self.assertEqual(latest.revision,2)
        self.assertEqual([m['message_id'] for m in latest.messages],[1,2])

    async def test_ambiguous_question_and_nonanswer_are_semantically_assessed(self):
        cid=await self.observe()
        await self.change('I do not know either.',owner=2,reply=1)
        captured=[]; original=self.judge.assess
        async def assess(frame,p):
            captured.append(frame.questions[1]['status'])
            return await original(frame,p)
        self.judge.assess=assess
        await self.tick()
        self.assertEqual(len(captured),1)
        self.assertIn(captured[0],('open','uncertain','possibly_answered'))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.candidate(cid))['status'],'delivered')

    async def test_abstention_does_not_impose_ninety_second_new_opportunity_blackout(self):
        cid=await self.observe(text='A passing observation from the walk.')
        self.judge.action='abstain'; self.judge.reason='no_added_value'
        await self.tick()
        old=await self.candidate(cid)
        self.assertEqual(old['status'],'abstained')
        self.judge.action='speak'; self.judge.reason='useful_answer'
        await self.change('The import stalls on the same line with every file.')
        newest=await self.candidate(cid)
        self.assertNotEqual(old['id'],newest['id'])
        await self.tick()
        self.assertEqual(self.judge.assess_calls,2)
        self.bot.send_message.assert_awaited_once()

    async def test_direct_lease_preempts_slow_proactive_provider(self):
        cid=await self.observe()
        entered=asyncio.Event(); release=asyncio.Event()
        original=self.judge.assess
        async def blocked(*args):
            entered.set(); await release.wait(); return await original(*args)
        self.judge.assess=blocked
        task=asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(),2)
            token=await asyncio.wait_for(self.runtime.groups.direct_lease(cid),.5)
            self.assertEqual((await self.candidate(cid))['status'],'cancelled')
            await self.runtime.groups.release(cid,token)
        finally:
            release.set(); await task
        self.assertEqual(self.judge.compose_calls,0)
        self.bot.send_message.assert_not_awaited()

    async def test_direct_lease_does_not_steal_another_direct_turn(self):
        cid=await self.observe(directed=True)
        token=await self.runtime.groups.direct_lease(cid)
        try:
            with patch('cognition.proactivity.DIRECT_LEASE_TIMEOUT_SECONDS',.03):
                with self.assertRaises(DeliverySuppressed): await self.runtime.groups.direct_lease(cid)
            self.assertEqual(await self.scalar('SELECT token FROM group_action_leases WHERE context_id=$1',cid),token)
        finally: await self.runtime.groups.release(cid,token)

    async def test_direct_lease_never_preempts_transport_already_in_flight(self):
        cid=await self.observe()
        entered=asyncio.Event(); release=asyncio.Event()
        original=self.bot.send_message.return_value
        async def blocked(**kwargs): entered.set(); await release.wait(); return original
        self.bot.send_message.side_effect=blocked
        task=asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(),2)
            with patch('cognition.proactivity.DIRECT_LEASE_TIMEOUT_SECONDS',.03):
                with self.assertRaises(DeliverySuppressed): await self.runtime.groups.direct_lease(cid)
            self.assertEqual((await self.candidate(cid))['status'],'claimed')
            self.assertEqual(await self.scalar('SELECT status FROM cognitive_outbox'),'sending')
        finally: release.set(); await task
        self.assertEqual((await self.candidate(cid))['status'],'delivered')

    async def continuation_seed(self):
        cid=await self.observe(text='Арти, помоги',directed=True)
        self.at+=timedelta(seconds=1)
        await self.runtime.groups.observe(TransportScope(-10,5,'supergroup',1,900,True,'bot'),
            'Here is an explanation.',at=self.at,is_bot=True)
        return cid

    async def test_continuation_timeout_is_charged_and_releases_provider_capacity(self):
        cid=await self.continuation_seed()
        async def slow(*args): await asyncio.Event().wait()
        self.judge.assess=slow
        with patch('cognition.proactivity.CONTINUATION_TIMEOUT_SECONDS',.08):
            result=await asyncio.wait_for(self.runtime.groups.continuation(TransportScope(-10,5,'supergroup',1,2,False),'And then'),.6)
        self.assertFalse(result)
        self.assertEqual(await self.scalar("SELECT count(*) FROM cognitive_initiative_calls WHERE context_id=$1 AND kind='continuation'",cid),1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_initiative_calls WHERE completed_at IS NULL'),0)

    async def test_continuation_quota_survives_service_restart(self):
        await self.continuation_seed()
        self.judge.action='abstain'; self.judge.reason='uncertain'
        scope=TransportScope(-10,5,'supergroup',1,2,False)
        for _ in range(6): self.assertFalse(await self.runtime.groups.continuation(scope,'And then'))
        replacement=GroupService(self.runtime,self.judge)
        self.assertFalse(await replacement.continuation(scope,'And then'))
        self.assertEqual(self.judge.assess_calls,6)

    async def test_parallel_continuations_have_nonblocking_durable_concurrency_limit(self):
        await self.continuation_seed()
        entered=asyncio.Event(); release=asyncio.Event(); active=0; peak=0; calls=0
        async def blocked(frame,p):
            nonlocal active,peak,calls
            calls+=1; active+=1; peak=max(peak,active); entered.set()
            try: await release.wait(); return GroupJudgement('abstain','uncertain',0.,0.,.9,())
            finally: active-=1
        self.judge.assess=blocked
        scope=TransportScope(-10,5,'supergroup',1,2,False)
        first=asyncio.create_task(self.runtime.groups.continuation(scope,'And then'))
        await asyncio.wait_for(entered.wait(),1)
        second=asyncio.create_task(self.runtime.groups.continuation(scope,'And then'))
        # Wait for a durable lease rather than an assumed scheduling delay.
        for _ in range(100):
            if calls==2: break
            await asyncio.sleep(.002)
        self.assertEqual(calls,2)
        try:
            self.assertFalse(await asyncio.wait_for(GroupService(self.runtime,self.judge).continuation(scope,'And then'),.5))
            self.assertEqual(peak,2); self.assertEqual(calls,2)
        finally: release.set(); await asyncio.gather(first,second)

    async def test_intention_completion_during_composition_suppresses_followup(self):
        cid=await self.observe(text='I intend to finish the migration.',directed=True)
        async with self.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            payload=dict(status='open',description='Finish migration',delivered=False,source_id=source['source_id'],delivery_key='goal:test')
            artifact=await self.runtime.memory._put(conn,cid,'intention','goal:test',payload,1,[source['id']])
        await self.runtime.groups.propose_intention(dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=artifact,payload=payload),load_event(source['payload']))
        async def complete():
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE cognitive_artifacts SET payload=jsonb_set(payload,'{status}','\"completed\"'::jsonb),revision=revision+1 WHERE id=$1",artifact)
        self.judge.hook=complete
        await self.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(object_value(await self.scalar('SELECT payload FROM cognitive_artifacts WHERE id=$1',artifact))['delivered'])

    async def test_old_delivery_mark_cannot_complete_materially_revised_intention(self):
        cid=await self.observe(directed=True)
        async with self.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            artifact=await self.runtime.memory._put(conn,cid,'intention','goal:mark',dict(status='open',delivered=False,delivery_key='goal:new'),1,[source['id']])
            await self.runtime.groups.mark_intention_delivered(dict(intention_id=artifact,intention_key='goal:old'),conn)
        self.assertFalse(object_value(await self.scalar('SELECT payload FROM cognitive_artifacts WHERE id=$1',artifact))['delivered'])

    async def test_continuation_optout_during_assessment_never_promotes_to_direct(self):
        await self.continuation_seed()
        async def revoke(frame,p):
            await self.runtime.groups.policies.opt_out(-10,1,True)
            return GroupJudgement('speak','continuation',.9,.1,.9,(frame.messages[-1]['source_id'],))
        self.judge.assess=revoke
        result=await self.runtime.groups.continuation(TransportScope(-10,5,'supergroup',1,2,False),'And then')
        self.assertFalse(result)

    async def test_delivered_contribution_and_actual_feedback_are_distinct_evidence(self):
        cid=await self.observe(); await self.tick()
        packet=(await self.runtime.groups.frame(cid)).public_packet(1)
        contributions=packet['recent_contributions']
        self.assertEqual(len(contributions),1)
        self.assertEqual(contributions[0]['delivery_status'],'delivered')
        self.assertEqual(contributions[0]['effect'],'unknown')
        self.assertEqual(contributions[0]['feedback'],[])
        self.assertIn('sorted',contributions[0]['text'])
        self.at+=timedelta(seconds=1)
        self.assertTrue(await self.runtime.groups.feedback(TransportScope(-10,5,'supergroup'),900,2,.7))
        contributions=(await self.runtime.groups.frame(cid)).public_packet(1)['recent_contributions']
        self.assertEqual(len(contributions[0]['feedback']),1)
        self.assertEqual(contributions[0]['feedback'][0]['owner_id'],2)
        self.assertEqual(contributions[0]['effect'],'unknown')

    async def test_unknown_delivery_is_visible_without_imagined_success_or_repeat(self):
        cid=await self.observe(); self.bot.send_message.side_effect=TimeoutError()
        await self.tick()
        packet=(await self.runtime.groups.frame(cid)).public_packet(1)
        self.assertEqual(packet['recent_contributions'][0]['delivery_status'],'delivery_unknown')
        self.assertEqual(packet['recent_contributions'][0]['effect'],'unknown')
        await self.tick(); self.bot.send_message.assert_awaited_once()

    async def test_forgetting_support_removes_delivery_and_feedback_outcomes(self):
        cid=await self.observe(); await self.tick()
        await self.runtime.groups.feedback(TransportScope(-10,5,'supergroup'),900,2,.7)
        source=await self.scalar("SELECT source_id FROM cognitive_events WHERE context_id=$1 AND origin='user' ORDER BY id LIMIT 1",cid)
        await forget_cognitive_sources(self.pool,cid,1,[source])
        packet=(await self.runtime.groups.frame(cid)).public_packet()
        self.assertEqual(packet['recent_contributions'],[])

    async def test_old_group_goal_cannot_be_first_proposed_after_history_reset(self):
        cid=await self.observe(directed=True)
        async with self.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            p=dict(status='open',description='Old goal',delivered=False,source_id=source['source_id'],delivery_key='goal:reset')
            artifact=await self.runtime.memory._put(conn,cid,'intention','goal:reset',p,1,[source['id']])
            await conn.execute('UPDATE cognitive_contexts SET history_after_event_id=$2 WHERE id=$1',cid,source['id'])
        await self.runtime.groups.propose_intention(dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=artifact,payload=p),load_event(source['payload']))
        self.assertIsNone(await self.candidate(cid))

    async def test_stop_serializes_with_final_send_guard(self):
        from database.models import ResponseStatus
        await self.observe()
        checked=asyncio.Event(); release=asyncio.Event()
        original=self.runtime.groups.delivery_guard
        async def guarded(turn,conn):
            allowed=await original(turn,conn)
            checked.set(); await release.wait(); return allowed
        with patch.object(self.runtime.groups,'delivery_guard',side_effect=guarded):
            task=asyncio.create_task(self.tick())
            await asyncio.wait_for(checked.wait(),2)
            stop=asyncio.create_task(ResponseStatus.set(-10,False))
            try:
                with self.assertRaises(TimeoutError): await asyncio.wait_for(asyncio.shield(stop),.08)
            finally:
                release.set(); await task; await stop
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(await self.scalar('SELECT enabled FROM response_status WHERE chat_id=-10'))

    async def test_quoted_refusal_does_not_cancel_an_open_question(self):
        cid=await self.observe()
        await self.change('В документации Python написано «не вмешивайся»',reply=1)
        await self.tick()
        self.assertEqual(self.judge.assess_calls,1)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(await self.scalar('SELECT status FROM group_candidates WHERE context_id=$1 ORDER BY id LIMIT 1',cid),'delivered')

    async def test_owner_refusal_to_second_question_does_not_close_first(self):
        cid=await self.observe()
        original=await self.candidate(cid)
        await self.change('Как сортировать большой список Python?',i=2)
        await self.change('Не возвращайся к этому',i=3,reply=2)
        frame=await self.runtime.groups.frame(cid)
        policy,revision=await self.runtime.groups.policies.get(-10,5)
        self.assertIsNone(await self.runtime.groups.valid(original,frame,policy,revision))
        self.assertEqual(frame.question(1)['status'],'open')
        self.assertEqual(frame.question(2)['status'],'closed')

    async def test_many_new_questions_do_not_evict_anchored_refusal(self):
        cid=await self.observe(); original=await self.candidate(cid)
        await self.change('Не возвращайся к этому',reply=1)
        for i in range(3,21): await self.change('Distinct question '+str(i)+'?',i=i,owner=2)
        frame=await self.runtime.groups.frame(cid)
        policy,revision=await self.runtime.groups.policies.get(-10,5)
        self.assertEqual(await self.runtime.groups.valid(original,frame,policy,revision),'question_resolved')

    async def test_lexical_topic_drift_does_not_bypass_semantic_arbiter(self):
        cid=await self.observe()
        for i in range(2,12): await self.change('Unrelated contextual wording '+str(i),i=i,owner=2,directed=True)
        await self.tick()
        self.assertEqual(self.judge.assess_calls,1)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.candidate(cid))['status'],'delivered')

    async def test_hung_reminder_membership_probe_releases_lease_promptly(self):
        cid=await self.observe(text='Remind me about the meeting.',directed=True)
        async with self.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            p=dict(status='reminder',description='Meeting',delivered=False,source_id=source['source_id'],delivery_key='reminder:hung')
            artifact=await self.runtime.memory._put(conn,cid,'intention','reminder:hung',p,1,[source['id']])
        await self.runtime.groups.propose_intention(dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=artifact,payload=p),load_event(source['payload']))
        async def slow(*args): await asyncio.Event().wait()
        self.bot.get_chat_member.side_effect=slow
        with patch('cognition.proactivity.MEMBERSHIP_TIMEOUT_SECONDS',.04):
            await asyncio.wait_for(self.tick(),.6)
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
        token=await asyncio.wait_for(self.runtime.groups.direct_lease(cid),.3)
        await self.runtime.groups.release(cid,token)
        self.bot.send_message.assert_not_awaited()

    async def test_compact_contribution_provenance_retains_hidden_source_erasure(self):
        from types import SimpleNamespace as NS
        self.bot.send_message.side_effect=[NS(message_id=900,chat=NS(id=-10)),NS(message_id=901,chat=NS(id=-10))]
        cid=await self.observe()
        for i in range(2,12): await self.change('Original support '+str(i),i=i,owner=3,directed=True)
        await self.tick()
        hidden=await self.scalar("SELECT source_id FROM cognitive_events WHERE context_id=$1 AND source_id LIKE '%:10:user'",cid)
        self.at+=timedelta(hours=2)
        for i in range(20,84): await self.change('Later bounded context '+str(i),i=i,owner=2,directed=True)
        await self.change('What about the revised deployment?',i=100,owner=2)
        frame=await self.runtime.groups.frame(cid)
        packet=frame.public_packet(100)
        exported=packet['recent_contributions'][0]
        self.assertTrue(exported['source_ids_truncated'])
        self.assertNotIn(hidden,[m['source_id'] for m in packet['messages']])
        self.assertIn(hidden,self.runtime.groups.frame_sources(frame,100))
        await self.tick()
        self.assertEqual(self.bot.send_message.await_count,2)
        await forget_cognitive_sources(self.pool,cid,3,[hidden])
        self.assertEqual(await self.scalar("SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND origin='delivered_action' AND suppressed_at IS NULL",cid),0)

    async def test_outcome_over_dependency_cap_is_omitted_instead_of_partly_trusted(self):
        cid=await self.observe()
        await self.change('Additional provenance',owner=2,directed=True)
        await self.tick()
        self.assertEqual(len((await self.runtime.groups.frame(cid)).outcomes),1)
        with patch('cognition.proactivity.MAX_OUTCOME_DEPENDENCIES',1):
            frame=await self.runtime.groups.frame(cid)
        self.assertEqual(frame.outcomes,[])
        self.assertEqual(frame.public_packet(1)['recent_contributions'],[])

    async def test_feedback_during_compose_invalidates_the_semantic_plan(self):
        cid=await self.observe(); await self.tick()
        self.at+=timedelta(hours=2)
        await self.change('How should the deployment be configured?',i=2)
        async def feedback():
            await self.runtime.groups.feedback(TransportScope(-10,5,'supergroup'),900,2,-.7)
            self.judge.action='abstain'; self.judge.reason='interrupting'
        self.judge.hook=feedback
        await self.tick()
        self.assertEqual(self.judge.assess_calls,3)
        self.assertEqual(self.judge.compose_calls,2)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.candidate(cid))['status'],'abstained')

    async def test_topic_closure_serializes_with_the_final_send_transaction(self):
        await self.observe()
        checked=asyncio.Event(); release=asyncio.Event()
        original=self.runtime.groups.delivery_guard
        async def guarded(turn,conn):
            allowed=await original(turn,conn)
            checked.set(); await release.wait(); return allowed
        with patch.object(self.runtime.groups,'delivery_guard',side_effect=guarded):
            task=asyncio.create_task(self.tick())
            await asyncio.wait_for(checked.wait(),2)
            close=asyncio.create_task(self.runtime.groups.set_closed(TransportScope(-10,5,'supergroup'),True))
            try:
                with self.assertRaises(TimeoutError): await asyncio.wait_for(asyncio.shield(close),.08)
            finally:
                release.set(); await task; await close
        self.bot.send_message.assert_awaited_once()
        self.assertTrue(await self.scalar('SELECT closed FROM group_topic_runtime'))

    async def test_confirmed_reaction_advances_the_frame_revision_without_fake_message(self):
        await self.runtime.groups.policies.set(-10,dict(reactions=True))
        self.judge.channel='reaction'
        cid=await self.observe()
        before=await self.runtime.groups.frame(cid)
        await self.tick()
        after=await self.runtime.groups.frame(cid)
        self.assertGreater(after.revision,before.revision)
        self.assertEqual(len(after.messages),len(before.messages))
        self.assertEqual(after.outcomes[0]['delivery_status'],'delivered')
        self.assertEqual(after.outcomes[0]['channel'],'reaction')

    async def seed_followup(self):
        cid=await self.observe(text='I intend to finish the migration.',directed=True)
        async with self.pool.acquire() as conn:
            source=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            p=dict(status='open',description='Finish migration',delivered=False,source_id=source['source_id'],delivery_key='goal:revision')
            artifact=await self.runtime.memory._put(conn,cid,'intention','goal:revision',p,1,[source['id']])
        return dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=artifact,payload=p),load_event(source['payload'])

    async def test_benign_same_key_revision_can_reschedule_an_unsent_followup_once(self):
        row,event=await self.seed_followup()
        await self.runtime.groups.propose_intention(row,event)
        async def revise():
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE cognitive_artifacts SET payload=jsonb_set(payload,'{description}','\"Updated migration wording\"'::jsonb),revision=revision+1 WHERE id=$1",row['id'])
        self.judge.hook=revise
        await self.tick()
        old=await self.candidate(row['context_id'])
        self.assertEqual(old['status'],'cancelled')
        self.assertEqual(await self.scalar('SELECT reason FROM group_decisions WHERE candidate_id=$1 ORDER BY id DESC LIMIT 1',old['id']),'intention_changed')
        self.judge.hook=None
        await self.runtime.groups.propose_intention(row,event)
        refreshed=await self.candidate(row['context_id'])
        self.assertEqual(refreshed['id'],old['id'])
        self.assertEqual(refreshed['status'],'pending')
        self.assertEqual(refreshed['attempts'],old['attempts'])
        self.assertEqual(object_value(refreshed['payload'])['description'],'Updated migration wording')
        await self.tick()
        self.bot.send_message.assert_awaited_once()
        await self.runtime.groups.propose_intention(row,event); await self.tick()
        self.bot.send_message.assert_awaited_once()

    async def test_same_key_revision_never_requeues_a_semantic_abstention(self):
        row,event=await self.seed_followup()
        self.judge.action='abstain'; self.judge.reason='no_added_value'
        await self.runtime.groups.propose_intention(row,event); await self.tick()
        old=await self.candidate(row['context_id'])
        self.assertEqual(old['status'],'abstained')
        async with self.pool.acquire() as conn: await conn.execute('UPDATE cognitive_artifacts SET revision=revision+1 WHERE id=$1',row['id'])
        await self.runtime.groups.propose_intention(row,event); await self.tick()
        self.assertEqual((await self.candidate(row['context_id']))['status'],'abstained')
        self.assertEqual(self.judge.assess_calls,1)
        self.bot.send_message.assert_not_awaited()

    async def test_reset_during_transport_does_not_mark_unconfirmed_group_goal_delivered(self):
        row,event=await self.seed_followup()
        await self.runtime.groups.propose_intention(row,event)
        receipt=self.bot.send_message.return_value
        async def reset(**kwargs):
            await self.runtime.reset_history(-10)
            return receipt
        self.bot.send_message.side_effect=reset
        await self.tick()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(await self.scalar('SELECT status FROM cognitive_outbox'),'cancelled')
        self.assertFalse(object_value(await self.scalar('SELECT payload FROM cognitive_artifacts WHERE id=$1',row['id']))['delivered'])
        self.assertEqual((await self.candidate(row['context_id']))['status'],'cancelled')
