import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from ai.group_participation import GroupJudgement
from cognition.group_context import build_frame
from cognition.group_policy import GroupPolicy
from cognition.scope import CURRENT_SCOPE,TransportScope,ScopedDict,ScopedDefaultDict,TopicUserData,addressing,from_update
from cognition.serialization import dump,load_event,object_value
from cognition.types import AudienceScope,ContextKey
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.delivery import send_with_receipt,DeliverySuppressed
from cognition.forgetting import forget_cognitive_sources
from tests.cognition.test_affect import AT,event
from tests.cognition.test_full_model import RecordedInterpreter


class RecordedJudge:
    def __init__(self):
        self.assess_calls=0; self.compose_calls=0; self.channel='text'; self.action='speak'; self.hook=None
        self.confidence=.9; self.reason='useful_answer'
    async def assess(self,frame,candidate):
        self.assess_calls+=1
        return GroupJudgement(self.action,self.reason,.9,.1,self.confidence,(frame.messages[-1]['source_id'],),self.channel,30)
    async def compose(self,frame,candidate,judgement,expression=''):
        self.compose_calls+=1
        if self.hook: await self.hook()
        return '👍' if judgement.channel=='reaction' else 'Можно использовать sorted(items).'
    async def close(self): pass


def message(i,text,owner=1,reply=None,**extra):
    return dict(message_id=i,source_id=str(i),event_id=i,text=text,owner_id=owner,reply_to_id=reply,sender_kind='user',
                directed=False,is_bot=False,at=(AT+timedelta(seconds=i)).isoformat(),**extra)


class GroupPureTests(unittest.TestCase):
    def test_audience_and_legacy_unknown_are_not_public(self):
        self.assertFalse(AudienceScope().permits(-10,5))
        self.assertFalse(AudienceScope('private',1).permits(1,-1))
        self.assertTrue(AudienceScope('topic',-10,5).permits(-10,5))
        self.assertFalse(AudienceScope('topic',-10,5).permits(-10,6))
        self.assertFalse(AudienceScope('group',-10).permits(-11,0))
    def test_topic_identity_and_event_roundtrip(self):
        a=replace(event(),context=ContextKey('arti',-10,topic_id=3),audience=AudienceScope('topic',-10,3),addressed_to_arti=False,reply_to_id=7)
        self.assertEqual(load_event(dump(a)),a)
        self.assertNotEqual(a.context.identity(),replace(a.context,topic_id=4).identity())
    def test_name_quoted_or_reported_is_not_an_invitation(self):
        for text in ('«Арти, объясни это» — так он написал','Арти сегодня отвечала долго','> Арти, помоги','Вчера обсуждали Арти'):
            self.assertFalse(addressing(NS(text=text,entities=[]),9,'arti_bot'),text)
        self.assertTrue(addressing(NS(text='Арти, помоги',entities=[]),9))
        self.assertTrue(addressing(NS(text='Поможешь, Арти?',entities=[]),9))
    def test_real_bot_reply_and_utf16_mention(self):
        self.assertTrue(addressing(NS(text='Спасибо',reply_to_message=NS(from_user=NS(id=9)),entities=[]),9))
        entity=NS(type='mention',offset=3,length=9)
        self.assertTrue(addressing(NS(text='😀 @arti_bot помоги',entities=[entity]),9,'arti_bot'))
        self.assertFalse(addressing(NS(text='@OtherBot помоги',entities=[NS(type='mention',offset=0,length=9)]),9,'arti_bot'))
    def test_scoped_flow_storage_and_deletion(self):
        data=ScopedDict(); flows=ScopedDefaultDict(dict)
        t=CURRENT_SCOPE.set(TransportScope(-10,2,'supergroup'))
        try:
            data[-10]=True; flows[-10]['a']=1
            CURRENT_SCOPE.set(TransportScope(-10,3,'supergroup'))
            self.assertIsNone(data.get(-10)); self.assertEqual(flows[-10],{})
            CURRENT_SCOPE.set(TransportScope(-10,2,'supergroup'))
            self.assertEqual(flows[-10]['a'],1); del data[-10]; self.assertNotIn(-10,data)
        finally: CURRENT_SCOPE.reset(t)
    def test_forum_without_a_known_thread_is_unknown(self):
        update=NS(effective_chat=NS(id=-10,type='supergroup',is_forum=True),effective_user=NS(id=1,is_bot=False),message=NS(message_id=1,text='hello',entities=[]))
        self.assertEqual(from_update(update,9).topic_id,-1)
        update.effective_chat.is_forum=False
        self.assertEqual(from_update(update,9).topic_id,0)
    def test_callback_user_data_does_not_swap_a_shared_dictionary(self):
        raw={}; a=TopicUserData(raw,TransportScope(-10,5,'supergroup')); b=TopicUserData(raw,TransportScope(-10,6,'supergroup'))
        a['image_flow']={'prompt':'TOPIC_A'}; b['image_flow']={'prompt':'TOPIC_B'}
        self.assertEqual(a['image_flow']['prompt'],'TOPIC_A'); self.assertEqual(b['image_flow']['prompt'],'TOPIC_B')
        a.clear(); self.assertEqual(len(a),0); self.assertEqual(len(b),1)
    def test_public_packet_has_a_byte_budget_and_keeps_anchor(self):
        frame=build_frame(1,-10,5,[message(i,'Я'*2500) for i in range(1,65)])
        packet=frame.public_packet(35)
        self.assertLess(len(json.dumps(packet,ensure_ascii=False).encode()),15000)
        self.assertIn(35,[m['message_id'] for m in packet['messages']])
    def test_quiet_hours_and_explicit_reminder(self):
        p=GroupPolicy(mode='useful',full_visibility=True,timezone='UTC')
        night=AT.replace(hour=2)
        self.assertEqual(p.reason(night),'quiet_hours')
        self.assertIsNone(p.reason(night,'reminder'))
        self.assertEqual(replace(p,disabled=True).reason(night,'reminder'),'disabled')
        self.assertEqual(GroupPolicy(mode='useful').reason(AT),'partial_visibility')
    def test_judge_rejects_invented_evidence_and_invalid_scores(self):
        data=dict(action='speak',reason='useful_answer',usefulness=.9,interruption=.1,confidence=.8,evidence_ids=['1'],channel='text',defer_seconds=30)
        self.assertEqual(GroupJudgement.parse(data,{'1'}).action,'speak')
        for change in (dict(evidence_ids=['secret']),dict(confidence=True),dict(confidence=float('nan')),dict(action='publish'),dict(reason='rhetorical'),dict(extra='instruction')):
            with self.assertRaises(ValueError): GroupJudgement.parse({**data,**change},{'1'})
    def test_conversation_resolution_and_parallel_branches(self):
        frame=build_frame(1,-10,0,[message(1,'Как сортировать список Python?'),message(2,'Встреча состоится завтра',2),message(3,'Используй sorted',3,1)])
        self.assertEqual(frame.questions[1]['status'],'possibly_answered')
        self.assertEqual(frame.messages[0]['branch'],frame.messages[2]['branch'])
        self.assertNotEqual(frame.messages[0]['branch'],frame.messages[1]['branch'])
    def test_bounded_frame_and_feedback_not_dominated_by_one_user(self):
        msgs=[message(i,'Разные сообщения '+str(i),i%3) for i in range(1,601)]
        f=build_frame(1,-10,0,msgs,feedback=[dict(user_id=1,signal=1.)]*50+[dict(user_id=2,signal=-1.)])
        self.assertLessEqual(len(f.messages),64); self.assertLessEqual(len(f.branches),8); self.assertLessEqual(len(f.questions),16)
        self.assertEqual(f.norms['receptivity'],0.)
    def test_frozen_rhetorical_and_answered_cases(self):
        scenarios=json.loads(Path('tests/fixtures/group_scenarios.json').read_text(encoding='utf-8'))['scenarios']
        for case in scenarios:
            msgs=[message(i+1,m['text'],m['owner_id'],m.get('reply_to_id'),addressed_elsewhere=m.get('addressed_elsewhere',False)) for i,m in enumerate(case['messages'])]
            frame=build_frame(1,-10,0,msgs)
            if case['id'] in ('human_reply','rhetorical','personal_address','explicit_resolution'):
                self.assertFalse(any(q['status']=='open' for q in frame.questions.values()),case['id'])


    def test_public_packet_accepts_a_new_continuation_without_internal_time(self):
        f=build_frame(1,-10,5,[message(1,'Арти, ответь')])
        f.messages.append(message(2,'И ещё уточнение'))
        self.assertEqual(len(f.public_packet()['messages']),2)
    def test_anonymous_sender_chat_is_not_a_fake_user(self):
        update=NS(effective_chat=NS(id=-10,type='supergroup',is_forum=True),effective_user=NS(id=1087968824,is_bot=True),
                  message=NS(message_id=1,message_thread_id=5,text='Арти, помоги',entities=[],sender_chat=NS(id=-10)))
        scope=from_update(update,9)
        self.assertIsNone(scope.user_id); self.assertEqual(scope.sender_kind,'chat'); self.assertEqual(scope.sender_ref,'chat:-10')
    def test_public_group_audience_cannot_move_to_a_forum_topic(self):
        self.assertTrue(AudienceScope('group',-10,0).permits(-10,0))
        self.assertFalse(AudienceScope('group',-10,0).permits(-10,5))
        self.assertFalse(AudienceScope('group',-10).permits(-10,0))
        with self.assertRaises(ValueError): AudienceScope('topic',-10,True)
    def test_feedback_uses_recent_signals_and_separates_kinds(self):
        feedback=[dict(user_id=1,signal=-1.,kind='social_moment')]*3+[dict(user_id=1,signal=1.,kind='open_question')]*5
        f=build_frame(1,-10,0,[],feedback=feedback)
        self.assertLess(f.norms['receptivity'],0.)
        self.assertGreater(f.norms['by_kind']['open_question']['receptivity'],0.)
        self.assertLess(f.norms['by_kind']['social_moment']['receptivity'],0.)


    def test_media_uses_captured_addressing_and_callback_is_an_explicit_request(self):
        from cognition.scope import requested
        token=CURRENT_SCOPE.set(TransportScope(-10,5,'supergroup',1,1,False))
        try:
            self.assertFalse(requested(True))
            CURRENT_SCOPE.set(TransportScope(-10,5,'supergroup',1,1,True))
            self.assertTrue(requested(False))
        finally: CURRENT_SCOPE.reset(token)
        msg=NS(message_id=1,message_thread_id=5,text='Choose an action',entities=[])
        update=NS(effective_chat=NS(id=-10,type='supergroup',is_forum=True),effective_user=NS(id=1,is_bot=False),effective_message=msg,callback_query=NS(message=msg))
        self.assertTrue(from_update(update,9).addressed)


class GroupIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_is_observed_without_becoming_a_direct_invitation(self):
        from cognition.telegram_scope import CognitiveUpdateProcessor
        groups=NS(observe=AsyncMock(),continuation=AsyncMock(return_value=False),set_closed=AsyncMock(),migrate_chat=AsyncMock())
        runtime=NS(mode='shadow',bot_id=9,bot_username='arti_bot',groups=groups)
        msg=NS(message_id=1,message_thread_id=5,text='@arti_bot help',entities=[NS(type='mention',offset=0,length=9)],date=AT,edit_date=AT)
        update=NS(effective_message=msg,edited_message=msg,effective_user=NS(id=1,is_bot=False),effective_chat=NS(id=-10,type='supergroup',is_forum=True))
        seen=[]
        async def callback(): seen.append(CURRENT_SCOPE.get())
        import sys
        with patch('cognition.telegram_scope.get_runtime',return_value=runtime),patch('utils.response_status.is_responses_enabled',AsyncMock(return_value=True)),patch.dict(sys.modules,{'config':NS(rp_mode_state={})}):
            await CognitiveUpdateProcessor(4).do_process_update(update,callback())
        self.assertFalse(seen[0].addressed); self.assertEqual(seen[0].topic_id,5)
        groups.observe.assert_awaited_once(); groups.continuation.assert_not_called()
        self.assertIsNone(CURRENT_SCOPE.get())
    async def test_service_migration_is_forwarded_to_the_coordinator(self):
        from cognition.telegram_scope import CognitiveUpdateProcessor
        groups=NS(migrate_chat=AsyncMock(),set_closed=AsyncMock())
        runtime=NS(mode='shadow',bot_id=9,bot_username='arti_bot',groups=groups)
        msg=NS(message_id=1,text=None,entities=[],migrate_to_chat_id=-10010)
        update=NS(effective_message=msg,effective_user=NS(id=1,is_bot=False),effective_chat=NS(id=-10,type='group',is_forum=False))
        async def callback(): pass
        with patch('cognition.telegram_scope.get_runtime',return_value=runtime):
            await CognitiveUpdateProcessor(4).do_process_update(update,callback())
        groups.migrate_chat.assert_awaited_once_with(-10,-10010)
    async def test_bot_authored_updates_never_reach_a_normal_handler(self):
        from cognition.scope import wrap_callback
        token=CURRENT_SCOPE.set(TransportScope(-10,5,'supergroup',12,1,False,'bot'))
        callback=AsyncMock()
        try: await wrap_callback(callback)(object(),object())
        finally: CURRENT_SCOPE.reset(token)
        callback.assert_not_awaited()


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class GroupDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); self.at=AT
        self.interpreter=RecordedInterpreter()
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at).initialize(False)
        self.judge=RecordedJudge(); self.runtime.groups.judge=self.judge
        self.bot=NS(send_message=AsyncMock(return_value=NS(message_id=900,chat=NS(id=-10))),
                    set_message_reaction=AsyncMock(return_value=True),get_chat_member=AsyncMock(return_value=NS(status='member')))
        async with self.pool.acquire() as conn: await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-10,TRUE)')
        await self.runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,spacing_seconds=60))
        self.token=CURRENT_SCOPE.set(None); self.turn_token=CURRENT_TURN.set(None)
    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.token); CURRENT_TURN.reset(self.turn_token)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)
    async def observe(self,i=1,text='Кто знает, как сортировать список Python?',owner=1,topic=5,reply=None,directed=False,edited=False):
        scope=TransportScope(-10,topic,'supergroup',owner,i,directed,'user',reply)
        return await self.runtime.groups.observe(scope,text,at=self.at,edited=edited)
    async def candidate(self,cid):
        async with self.pool.acquire() as conn: return await conn.fetchrow('SELECT * FROM group_candidates WHERE context_id=$1 ORDER BY id DESC LIMIT 1',cid)
    async def tick(self):
        self.at+=timedelta(seconds=60); await self.runtime.groups.run_cycle(self.bot)
    async def test_topics_are_isolated_and_public_prompt_has_no_private_memory(self):
        a=await self.observe(text='Публичная тема A',topic=5); b=await self.observe(2,'Публичная тема B',topic=6)
        await self.runtime.ingest(44,1,'DM_SECRET',1)
        await self.runtime.ingest(-10,1,'LEGACY_UNKNOWN',88)
        frame=await self.runtime.groups.frame(a); packet=str(frame.public_packet())
        self.assertNotEqual(a,b); self.assertNotIn('тема B',packet); self.assertNotIn('DM_SECRET',packet); self.assertNotIn('LEGACY_UNKNOWN',packet)
    async def test_ambient_observation_never_calls_emotional_interpreter(self):
        cid=await self.observe(text='Маша, я на тебя злюсь!')
        async with self.pool.acquire() as conn: eid=await conn.fetchval('SELECT event_id FROM group_observations WHERE context_id=$1',cid)
        await self.runtime.process(cid,eid)
        self.assertEqual(self.interpreter.calls,0)
        model=await self.runtime.memory.relationship(cid,1)
        self.assertEqual(model['dimensions']['reliability']['alpha'],1.) if 'dimensions' in model else self.assertFalse(model.get('groups'))
    async def test_live_question_receipt_and_exact_topic(self):
        cid=await self.observe(); await self.tick(); row=await self.candidate(cid)
        self.assertEqual(row['status'],'delivered'); self.bot.send_message.assert_awaited_once()
        kwargs=self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs['message_thread_id'],5); self.assertEqual(kwargs['reply_parameters'].message_id,1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox WHERE status=\'delivered\''),1)
        await self.tick(); self.bot.send_message.assert_awaited_once()
    async def test_answer_from_a_human_cancels_before_judging(self):
        cid=await self.observe(); await self.observe(2,'Используй sorted',2,reply=1); await self.tick()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled'); self.assertEqual(self.judge.assess_calls,0); self.bot.send_message.assert_not_called()
    async def test_answer_during_generation_cancels_the_output(self):
        cid=await self.observe()
        self.judge.hook=lambda:self.observe(2,'Используй sorted',2,reply=1)
        await self.tick(); self.bot.send_message.assert_not_called(); self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_permission_revocation_during_generation_cancels(self):
        cid=await self.observe()
        self.judge.hook=lambda:self.runtime.groups.policies.set(-10,dict(mode='mentions'))
        await self.tick(); self.bot.send_message.assert_not_called(); self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_shadow_records_a_decision_without_generating_or_sending(self):
        await self.runtime.groups.policies.set(-10,dict(execution='shadow'))
        cid=await self.observe(); await self.tick()
        self.assertEqual((await self.candidate(cid))['status'],'shadow'); self.assertEqual(self.judge.compose_calls,0); self.bot.send_message.assert_not_called()
    async def test_partial_visibility_and_optout_prevent_candidates(self):
        await self.runtime.groups.policies.set(-10,dict(full_visibility=False))
        a=await self.observe(); self.assertIsNone(await self.candidate(a))
        await self.runtime.groups.policies.set(-10,dict(full_visibility=True))
        await self.runtime.groups.policies.opt_out(-10,1,True)
        await self.observe(2); self.assertIsNone(await self.candidate(a))
    async def test_two_workers_produce_one_send(self):
        cid=await self.observe(); self.at+=timedelta(seconds=60)
        await asyncio.gather(self.runtime.groups.run_cycle(self.bot),self.runtime.groups.run_cycle(self.bot))
        self.bot.send_message.assert_awaited_once(); self.assertEqual((await self.candidate(cid))['status'],'delivered')
    async def test_group_budget_is_shared_across_topics(self):
        await self.runtime.groups.policies.set(-10,dict(daily_limit=1))
        a=await self.observe(topic=5); b=await self.observe(2,topic=6,owner=2); await self.tick()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual({(await self.candidate(a))['status'],(await self.candidate(b))['status']},{'delivered','cancelled'})
    async def test_timeout_is_unknown_and_not_retried_after_restart(self):
        self.bot.send_message.side_effect=TimeoutError()
        cid=await self.observe(); await self.tick(); self.assertEqual((await self.candidate(cid))['status'],'delivery_unknown')
        await self.runtime.initialize(False); await self.tick(); self.bot.send_message.assert_awaited_once()
    async def test_reaction_has_boolean_confirmation_without_fake_message(self):
        await self.runtime.groups.policies.set(-10,dict(reactions=True))
        self.judge.channel='reaction'; cid=await self.observe(); await self.tick()
        self.bot.set_message_reaction.assert_awaited_once(); self.bot.send_message.assert_not_called()
        self.assertNotIn('message_thread_id',self.bot.set_message_reaction.await_args.kwargs)
        async with self.pool.acquire() as conn:
            out=await conn.fetchrow('SELECT * FROM cognitive_outbox')
            self.assertEqual(out['status'],'delivered'); self.assertIsNone(out['receipt_id'])
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM group_observations'),1)
        self.assertEqual((await self.candidate(cid))['status'],'delivered')
    async def test_forget_erases_public_payload_and_pending_candidate(self):
        cid=await self.observe(); row=await self.candidate(cid)
        async with self.pool.acquire() as conn: source=await conn.fetchval('SELECT source_id FROM cognitive_events WHERE id=$1',row['source_ids'][0])
        await forget_cognitive_sources(self.pool,cid,1,[source]); await self.tick()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled'); self.assertEqual((await self.runtime.groups.frame(cid)).messages,[])
        async with self.pool.acquire() as conn: self.assertIsNone(await conn.fetchval('SELECT payload FROM group_observations'))
    async def test_known_edit_cancels_old_candidate_and_keeps_raw_event_immutable(self):
        cid=await self.observe(); before=await self.candidate(cid); self.at+=timedelta(seconds=1)
        await self.observe(text='Вопрос снят',edited=True); await self.tick()
        async with self.pool.acquire() as conn:
            old=await conn.fetchval('SELECT payload FROM cognitive_events WHERE id=$1',before['source_ids'][0])
        self.assertIn('Кто знает',object_value(old)['text']); self.assertEqual((await self.candidate(cid))['status'],'cancelled'); self.bot.send_message.assert_not_called()
    async def test_bad_provider_abstains_without_public_error(self):
        self.judge.assess=AsyncMock(side_effect=ValueError('malformed'))
        cid=await self.observe(); await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_feedback_requires_a_confirmed_initiative_and_retains_sources(self):
        cid=await self.observe(); await self.tick()
        scope=TransportScope(-10,5,'supergroup',2)
        self.assertFalse(await self.runtime.groups.feedback(scope,888,2,1.))
        self.assertTrue(await self.runtime.groups.feedback(scope,900,2,1.))
        frame=await self.runtime.groups.frame(cid); self.assertEqual(frame.norms['evidence_participants'],1)
    async def test_low_confidence_and_deferral_are_bounded(self):
        self.judge.confidence=.2; cid=await self.observe(); await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'abstained')
    async def test_topic_destination_mismatch_is_blocked(self):
        scope=TransportScope(-10,5,'supergroup',1,1,True)
        t=CURRENT_SCOPE.set(scope)
        try:
            turn=await self.runtime.prepare(-10,1,'Арти, привет',1)
            with self.assertRaises(ValueError): await send_with_receipt(self.bot.send_message,(),dict(chat_id=-10,message_thread_id=6,text='hi'),'message')
            self.bot.send_message.assert_not_called()
        finally: CURRENT_SCOPE.reset(t)
    async def test_repeated_question_by_two_owners_has_one_candidate(self):
        cid=await self.observe(); await self.observe(2,owner=2)
        async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT count(*) FROM group_candidates WHERE context_id=$1',cid),1)
        await self.tick(); self.bot.send_message.assert_awaited_once()
    async def test_closed_topic_cancels_and_reopening_does_not_resend(self):
        cid=await self.observe(); scope=TransportScope(-10,5,'supergroup')
        await self.runtime.groups.set_closed(scope,True); await self.tick(); self.bot.send_message.assert_not_called()
        await self.runtime.groups.set_closed(scope,False); await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_lease_loss_during_generation_fences_the_sender(self):
        cid=await self.observe()
        async def steal():
            async with self.pool.acquire() as conn: await conn.execute("UPDATE group_action_leases SET token='other-worker' WHERE context_id=$1",cid)
        self.judge.hook=steal
        await self.tick(); self.bot.send_message.assert_not_called(); self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_candidate_with_a_private_source_is_rejected(self):
        cid=await self.observe(); private=await self.runtime.ingest(44,1,'PRIVATE_CANDIDATE_SOURCE',4)
        async with self.pool.acquire() as conn: await conn.execute('UPDATE group_candidates SET source_ids=$2 WHERE context_id=$1',cid,[private[1]])
        await self.tick(); self.bot.send_message.assert_not_called(); self.assertEqual(self.judge.assess_calls,0)
    async def test_public_action_is_erased_when_another_owners_support_is_forgotten(self):
        cid=await self.observe(); await self.observe(2,'Важно сохранить порядок элементов',2)
        self.at+=timedelta(seconds=60); await self.runtime.groups.run_cycle(self.bot)
        self.bot.send_message.assert_awaited_once()
        async with self.pool.acquire() as conn:
            support=await conn.fetchval('SELECT source_id FROM cognitive_events WHERE context_id=$1 AND owner_id=2',cid)
        await forget_cognitive_sources(self.pool,cid,2,[support])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE context_id=$1 AND origin='delivered_action' AND suppressed_at IS NULL",cid),0)
        self.assertNotIn('Можно использовать sorted',(await self.runtime.groups.history(TransportScope(-10,5,'supergroup'))))
    async def test_reminder_survives_busy_coordinator_and_is_not_a_random_initiative(self):
        cid=await self.observe(text='Арти, напомни завтра про встречу',directed=True)
        async with self.pool.acquire() as conn:
            ev=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
            artifact=await self.runtime.memory._put(conn,cid,'intention','reminder-test',dict(status='reminder',description='Встреча',delivered=False,source_id=ev['source_id'],deadline=AT.isoformat()),1,[ev['id']])
        row=dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=artifact,payload=dict(status='reminder',delivery_key='reminder:test',description='Встреча'))
        await self.runtime.groups.policies.set(-10,dict(mode='mentions',daily_limit=0))
        await self.runtime.groups.propose_intention(row,load_event(ev['payload']))
        lease=await self.runtime.groups.direct_lease(cid); await self.tick()
        self.bot.send_message.assert_not_called(); self.assertEqual((await self.candidate(cid))['status'],'deferred')
        await self.runtime.groups.release(cid,lease); await self.tick()
        self.bot.send_message.assert_awaited_once(); self.assertEqual(self.judge.assess_calls,0)
        async with self.pool.acquire() as conn: self.assertTrue(object_value(await conn.fetchval('SELECT payload FROM cognitive_artifacts WHERE id=$1',artifact))['delivered'])
    async def test_admin_permission_is_required_to_change_group_policy(self):
        from bot.group_commands import proactivity_command
        scope=TransportScope(-10,5,'supergroup',1,1); t=CURRENT_SCOPE.set(scope)
        update=NS(effective_user=NS(id=1),effective_message=NS(reply_text=AsyncMock()))
        context=NS(args=['social','live'],bot=self.bot)
        try:
            with patch('bot.group_commands.get_runtime',return_value=self.runtime),patch('bot.group_commands.is_admin',new=AsyncMock(return_value=False)):
                await proactivity_command(update,context)
            policy,_=await self.runtime.groups.policies.get(-10,5); self.assertEqual(policy.mode,'useful')
        finally: CURRENT_SCOPE.reset(t)
    async def test_ambient_history_reader_and_legacy_rag_are_topic_safe(self):
        from utils.chat_history import get_chat_context
        from memory.storage import build_memory_context
        await self.observe(text='ONLY_TOPIC_A',topic=5); await self.observe(2,'ONLY_TOPIC_B',topic=6)
        t=CURRENT_SCOPE.set(TransportScope(-10,5,'supergroup',1))
        try:
            with patch('cognition.runtime.get_runtime',return_value=self.runtime): text=await get_chat_context(-10)
            self.assertIn('ONLY_TOPIC_A',text); self.assertNotIn('ONLY_TOPIC_B',text)
            self.assertEqual(await build_memory_context(-10,1,'secret'),'')
        finally: CURRENT_SCOPE.reset(t)

    async def test_responses_disabled_during_composition_cancels(self):
        cid=await self.observe()
        async def disable():
            async with self.pool.acquire() as conn: await conn.execute('UPDATE response_status SET enabled=FALSE WHERE chat_id=-10')
        self.judge.hook=disable
        await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_recovery_preserves_a_confirmed_delivery(self):
        cid=await self.observe(); await self.tick()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE group_candidates SET status='claimed' WHERE context_id=$1",cid)
        await self.tick(); self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.candidate(cid))['status'],'delivered')
    async def test_migration_resets_permission_without_copying_history(self):
        cid=await self.observe()
        await self.runtime.groups.policies.set(-10,dict(mode='social'),5)
        await self.runtime.groups.migrate_chat(-10,-10010)
        old,_=await self.runtime.groups.policies.get(-10,5); new,_=await self.runtime.groups.policies.get(-10010,0)
        self.assertEqual(old.mode,'mentions'); self.assertEqual(new.mode,'mentions'); self.assertFalse(new.full_visibility)
        await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
        async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_contexts WHERE chat_id=-10010'),0)
    async def test_assessment_budget_counts_abstentions_across_topics(self):
        await self.runtime.groups.policies.set(-10,dict(assessment_hourly_limit=1))
        self.judge.confidence=.2
        a=await self.observe(topic=5); b=await self.observe(2,owner=2,topic=6)
        await self.tick(); self.assertEqual(self.judge.assess_calls,1); self.bot.send_message.assert_not_called()
        self.assertEqual({(await self.candidate(a))['status'],(await self.candidate(b))['status']},{'abstained','cancelled'})
    async def test_pending_backpressure_is_bounded(self):
        for i in range(1,72): cid=await self.observe(i,f'Кто подскажет решение задачи номер {i}?')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM group_candidates WHERE context_id=$1 AND status='pending'",cid),64)
    async def test_anonymous_ingress_never_creates_a_user_projection(self):
        from cognition.types import Origin
        scope=TransportScope(-10,5,'supergroup',None,1,True,'chat',None,'chat:-10')
        cid=await self.runtime.groups.observe(scope,'Арти, помоги',at=self.at)
        async with self.pool.acquire() as conn:
            raw=await conn.fetchrow('SELECT e.* FROM cognitive_events e JOIN group_observations o ON o.event_id=e.id WHERE o.context_id=$1',cid)
        self.assertEqual(raw['origin'],Origin.SYSTEM.value); self.assertIsNone(raw['owner_id'])
        await self.runtime.process(cid,raw['id']); self.assertEqual(self.interpreter.calls,0)
    async def test_explicit_refusal_closes_the_open_question(self):
        cid=await self.observe(); await self.observe(2,'Не возвращайся к этому',reply=1)
        await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_group_source_with_a_wrong_declared_audience_is_rejected(self):
        cid=await self.observe(); row=await self.candidate(cid)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_events SET payload=jsonb_set(payload,'{audience,chat_id}','-999'::jsonb) WHERE id=$1",row['source_ids'][0])
        await self.tick(); self.bot.send_message.assert_not_called()
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')

    async def test_anonymous_history_save_keeps_system_authorship(self):
        from utils.chat_history import save_chat_message
        scope=TransportScope(-10,5,'supergroup',None,1,True,'chat',None,'chat:-10')
        token=CURRENT_SCOPE.set(scope)
        try:
            with patch('cognition.runtime.get_runtime',return_value=self.runtime):
                await save_chat_message(-10,'Анонимный участник','Арти, привет',user_id=0,message_id=1,occurred_at=self.at)
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='user'"),0)
                self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='system'"),1)
        finally: CURRENT_SCOPE.reset(token)
    async def test_queue_keeps_authors_topics_and_merged_source_provenance(self):
        import asyncio
        import bot.queue as queue_module
        context=NS(bot=self.bot); seen=[]
        async def process(request,bot): seen.append(dict(request))
        with patch('cognition.runtime.get_runtime',return_value=self.runtime),patch.object(queue_module,'_DEBOUNCE_WINDOW_SEC',.5),patch.object(queue_module,'is_responses_enabled',AsyncMock(return_value=True)),patch.object(queue_module,'process_user_reply',process):
            for i,owner,topic,text in ((1,1,5,'Первое'),(2,1,5,'Уточнение'),(3,2,5,'Другой автор'),(4,1,6,'Другая тема')):
                scope=TransportScope(-10,topic,'supergroup',owner,i,True)
                token=CURRENT_SCOPE.set(scope)
                try: await queue_module.enqueue_reply(-10,owner,'Участник',text,i,context,is_voice=False)
                finally: CURRENT_SCOPE.reset(token)
            await asyncio.wait_for(asyncio.gather(*list(queue_module._user_workers.values())),5)
        a=[r for r in seen if r['_telegram_scope'].topic_id==5]
        self.assertEqual([r['user_id'] for r in a],[1,2]); self.assertEqual(a[0]['user_message'],'Первое\nУточнение')
        self.assertEqual(len(a[0]['_cognitive_source_ids']),2)
        self.assertEqual([r['user_message'] for r in seen if r['_telegram_scope'].topic_id==6],['Другая тема'])
    async def test_forgetting_an_earlier_merged_source_blocks_a_direct_reply(self):
        cid=await self.observe(1,'Ранее сказанное',directed=True); await self.observe(2,'Арти, ответь',directed=True)
        scope=TransportScope(-10,5,'supergroup',1,2,True); token=CURRENT_SCOPE.set(scope)
        try:
            turn=await self.runtime.prepare(-10,1,'Арти, ответь',2)
            async with self.pool.acquire() as conn: rows=await conn.fetch('SELECT id,source_id FROM cognitive_events WHERE context_id=$1 ORDER BY id',cid)
            turn.supporting_event_ids=[r['id'] for r in rows]
            await forget_cognitive_sources(self.pool,cid,1,[rows[0]['source_id']])
            with self.assertRaises(DeliverySuppressed): await send_with_receipt(self.bot.send_message,(),dict(chat_id=-10,text='Старый ответ'),'message')
            self.bot.send_message.assert_not_called()
        finally: CURRENT_SCOPE.reset(token)
    async def test_direct_public_answer_is_tracked_even_in_shadow(self):
        scope=TransportScope(-10,5,'supergroup',1,1,True); token=CURRENT_SCOPE.set(scope)
        try:
            turn=await self.runtime.prepare(-10,1,'Арти, привет',1)
            await self.runtime.set_authority(turn.context_id,'shadow')
            turn=await self.runtime.prepare(-10,1,'Арти, привет',1)
            self.assertFalse(turn.active); self.assertTrue(turn.tracks_delivery)
            await send_with_receipt(self.bot.send_message,(),dict(chat_id=-10,text='Привет'),'message')
            async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_outbox WHERE status='delivered'"),1)
        finally: CURRENT_SCOPE.reset(token)

    async def test_model_cannot_introduce_a_topic_when_seeds_are_disabled(self):
        await self.runtime.groups.policies.set(-10,dict(mode='social',topic_seeds=False))
        self.judge.reason='topic_seed'; cid=await self.observe(); await self.tick()
        self.bot.send_message.assert_not_called(); self.assertEqual(self.judge.compose_calls,0)
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
    async def test_rp_reminder_uses_the_scene_and_setting_of_its_topic(self):
        from cognition.intentions import due_intentions
        from config import rp_mode_state
        scope=TransportScope(-10,5,'supergroup',1,1,True); token=CURRENT_SCOPE.set(scope)
        try:
            rp_mode_state[-10]=True
            cid=await self.runtime.groups.observe(scope,'Reminder request',mode='rp',at=self.at)
            async with self.pool.acquire() as conn:
                ev=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
                artifact=await self.runtime.memory._put(conn,cid,'intention','rp-reminder-test',dict(status='reminder',description='Meeting',
                    delivered=False,source_id=ev['source_id'],deadline=AT.isoformat(),created_at=AT.isoformat(),actor_id='arti',cue='meeting'),1,[ev['id']])
            CURRENT_SCOPE.set(None)
            rows=await due_intentions(self.runtime)
            self.assertIn(artifact,[row['id'] for row,model in rows])
            self.assertEqual(rows[0][0]['topic_id'],5)
        finally:
            CURRENT_SCOPE.set(scope); rp_mode_state.pop(-10,None); CURRENT_SCOPE.reset(token)

    async def test_explicit_reminder_can_reenter_after_a_policy_pause_without_resending(self):
        cid=await self.observe(text='Reminder request',directed=True)
        async with self.pool.acquire() as conn:
            ev=await conn.fetchrow('SELECT * FROM cognitive_events WHERE context_id=$1',cid)
        row=dict(context_id=cid,chat_id=-10,topic_id=5,owner_id=1,id=None,payload=dict(status='reminder',delivery_key='pause:test',description='Meeting',deadline=AT.isoformat()))
        await self.runtime.groups.propose_intention(row,load_event(ev['payload']))
        await self.runtime.groups.policies.set(-10,dict(paused_until=(AT+timedelta(hours=1)).isoformat()))
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
        await self.runtime.groups.propose_intention(row,load_event(ev['payload']))
        self.assertEqual((await self.candidate(cid))['status'],'cancelled')
        await self.runtime.groups.policies.set(-10,dict(paused_until=None))
        await self.runtime.groups.propose_intention(row,load_event(ev['payload'])); await self.tick()
        self.bot.send_message.assert_awaited_once()
        await self.runtime.groups.propose_intention(row,load_event(ev['payload'])); await self.tick()
        self.bot.send_message.assert_awaited_once()
    async def test_group_admin_check_does_not_use_personal_privileged_ids(self):
        from bot.group_commands import is_admin
        self.assertFalse(await is_admin(NS(id=1),-10,NS(bot=self.bot)))
        self.bot.get_chat_member.return_value=NS(status='administrator')
        self.assertTrue(await is_admin(NS(id=1),-10,NS(bot=self.bot)))

    async def test_global_off_and_pause_cannot_be_undone_by_topic_overrides(self):
        await self.runtime.groups.policies.set(-10,dict(mode='social',execution='live',disabled=False,paused_until=None),5)
        await self.runtime.groups.policies.set(-10,dict(disabled=True,paused_until=(AT+timedelta(hours=1)).isoformat()))
        policy,_=await self.runtime.groups.policies.get(-10,5)
        self.assertEqual(policy.reason(AT),'disabled'); self.assertEqual(policy.reason(AT,'reminder'),'disabled')
        await self.runtime.groups.policies.set(-10,dict(disabled=False))
        policy,_=await self.runtime.groups.policies.get(-10,5)
        self.assertEqual(policy.reason(AT),'paused')

    async def test_ambient_photo_does_not_trigger_on_a_quoted_name(self):
        from bot.handlers import _process_images
        scope=TransportScope(-10,5,'supergroup',1,1,False); token=CURRENT_SCOPE.set(scope)
        try:
            with patch('bot.handlers._send_photo_action_prompt',new=AsyncMock()) as prompt:
                await _process_images(self.bot,-10,1,'User',1,['synthetic'],'Здесь обсуждали Арти',False,False)
                prompt.assert_not_awaited(); self.bot.send_message.assert_not_called()
        finally: CURRENT_SCOPE.reset(token)
    async def test_unaddressed_voice_is_not_downloaded_or_transcribed(self):
        from bot.handlers import handle_voice_message
        scope=TransportScope(-10,5,'supergroup',1,1,False); token=CURRENT_SCOPE.set(scope)
        bot=NS(id=900,get_file=AsyncMock())
        update=NS(message=NS(chat_id=-10,from_user=NS(id=1),message_id=1,voice=NS(file_id='synthetic-voice')),effective_chat=NS(id=-10,type='supergroup'))
        context=NS(bot=bot,user_data={})
        try:
            with patch('bot.handlers.is_responses_enabled',new=AsyncMock(return_value=True)),patch('bot.handlers._vclone_caption_fastpath',new=AsyncMock(return_value=False)),patch('bot.handlers.transcribe_audio_groq',new=AsyncMock()) as stt:
                await handle_voice_message(update,context)
                stt.assert_not_awaited(); bot.get_file.assert_not_awaited()
        finally: CURRENT_SCOPE.reset(token)
    async def test_unaddressed_video_note_is_not_enqueued(self):
        from bot.handlers import handle_video_note
        scope=TransportScope(-10,5,'supergroup',1,1,False); token=CURRENT_SCOPE.set(scope)
        update=NS(message=NS(from_user=NS(id=1,first_name='User',username='user'),message_id=1),effective_chat=NS(id=-10,type='supergroup'))
        try:
            with patch('bot.handlers.is_responses_enabled',new=AsyncMock(return_value=True)),patch('bot.handlers.enqueue_reply',new=AsyncMock()) as enqueue:
                await handle_video_note(update,NS(bot=self.bot,user_data={}))
                enqueue.assert_not_awaited()
        finally: CURRENT_SCOPE.reset(token)
