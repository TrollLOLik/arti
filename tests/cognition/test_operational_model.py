import asyncio
import json
import math
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from cognition.affect import initial_state,appraise,advance,affect
from cognition.delivery import send_with_receipt,DeliverySuppressed
from cognition.forgetting import forget_cognitive_sources
from cognition.history import save_source_history
from cognition.migration import HistoricalMigration
from cognition.memory_dynamics import reconstruct
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.serialization import dump,load_state,object_value
from cognition.types import Perception,PERCEPTION_VERSION
from tests.cognition.test_affect import AT,event,appraisal,perception
from tests.cognition.test_full_model import RecordedInterpreter,situation


class OperationalPureTests(unittest.TestCase):
    def test_same_outcome_is_more_surprising_against_a_different_learned_expectation(self):
        ev = event(text='The promised task is completed.')
        state = initial_state(ev.context,AT)
        goal = dict(id='goal:expected',owner=1,actor=1,description='Complete task',priority=.7,status='open',source_group='earlier',expectation=1.)
        expected = replace(state,situational_goals=(goal,))
        unexpected = replace(state,situational_goals=({**goal,'expectation':0.},))
        p = perception(ev,appraisal(goal_id=goal['id'],novelty=.05,congruence=1.,agency_other=0.,intentionality=0.,norm_violation=0.))
        normal = appraise(expected,ev,p)
        surprising = appraise(unexpected,ev,p)
        amount = lambda s:sum(e.intensity for e in s.episodes if e.emotion=='surprise')
        self.assertGreater(amount(surprising),amount(normal))
    def test_explicit_null_actor_cannot_create_an_ownerless_promise(self):
        ev = event(text='Promise')
        item = dict(span=0,key='promise',description='Promise',cue='promise',deadline=None,status='open',confidence=.9,actor=None)
        with self.assertRaises(ValueError):
            situation(ev,intentions=[item])
    def test_operational_log_does_not_keep_conversation_or_transport_credentials(self):
        import logging
        from cognition.logging import PrivatePayloadFilter
        record = logging.LogRecord('bot.queue',logging.ERROR,'file',1,'USER_SECRET api-key=%s',('CREDENTIAL',),None)
        PrivatePayloadFilter().filter(record)
        self.assertNotIn('USER_SECRET',record.getMessage())
        self.assertNotIn('CREDENTIAL',record.getMessage())
    def test_unavailable_date_cannot_leak_through_accessible_gist(self):
        text = 'Мы запустили проект 14 мая.'
        detail = dict(text=text,kind='gist',strength=.8,stability_days=45.,fidelity=1.,confidence=.8,vividness=.4,last_recalled=None,recall_count=0)
        date = {**detail,'text':'14 мая','kind':'date','stability_days':8.}
        trace = dict(details=[detail,date],observed_at=AT.isoformat(),source_id='source',modality='reported')
        recalled = reconstruct(trace,AT+timedelta(days=90))
        self.assertTrue(recalled['details'])
        self.assertNotIn('14 мая',str(recalled))
        self.assertIn('14 мая',str(reconstruct(trace,AT+timedelta(days=90),archive=True)))
        self.assertFalse(recalled['details'][0]['verbatim_verified'])

    def test_long_busy_trajectory_is_bounded_and_keeps_exact_exponential_tail(self):
        state = initial_state(event().context,AT)
        for i in range(600):
            ev = event(str(i),at=AT+timedelta(seconds=i))
            p = perception(ev,appraisal(evidence_ids=(ev.evidence.source_id,)))
            state = appraise(state,ev,p)
        self.assertLessEqual(len(state.episodes),128)
        self.assertTrue(state.residues)
        self.assertEqual(load_state(dump(state)),state)
        direct = advance(state,AT+timedelta(hours=3))
        split = advance(advance(state,AT+timedelta(hours=1)),AT+timedelta(hours=3))
        for axis in affect(direct):
            self.assertAlmostEqual(affect(direct)[axis],affect(split)[axis],places=10)
        self.assertTrue(all(math.isfinite(v) for v in affect(direct).values()))

    def test_circadian_attention_depends_on_clock_without_contact_reward(self):
        state = initial_state(event().context,AT)
        night = advance(state,AT+timedelta(hours=15))
        next_day = advance(state,AT+timedelta(days=1))
        self.assertEqual(state.revision,next_day.revision)
        self.assertNotEqual(affect(state)['attention'],affect(night)['attention'])
        self.assertAlmostEqual(affect(state)['circadian'],affect(next_day)['circadian'])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class OperationalDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.at = AT
        self.interpreter = RecordedInterpreter()
        self.runtime = await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at).initialize(start_worker=False)

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def observe(self,text,mid=1,owner=1,mode='default'):
        cid,eid,ev = await self.runtime.ingest(10,owner,text,mid,mode)
        await self.runtime.process(cid,eid)
        self.at += timedelta(seconds=1)
        return cid,eid,ev

    async def test_two_processes_share_one_interpretation_lease(self):
        cid,eid,_ = await self.runtime.ingest(10,1,'A shared input',1)
        second = CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at)
        await asyncio.gather(self.runtime.process(cid,eid),second.process(cid,eid))
        self.assertEqual(self.interpreter.calls,1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_effects'),1)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_jobs WHERE status='done' AND kind='interpret'"),1)

    async def test_concurrent_restart_does_not_repeat_ddl_over_a_live_transition(self):
        cid,eid,_ = await self.runtime.ingest(10,1,'Input',1)
        second = CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at)
        await asyncio.wait_for(asyncio.gather(second.initialize(start_worker=False),self.runtime.process(cid,eid)),10)
        async with self.pool.acquire() as conn:
            from pathlib import Path
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_schema_migrations'),len(list(Path('cognition/migrations').glob('*.sql'))))
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_effects'),1)

    async def test_forget_during_send_cannot_resurrect_confirmed_history(self):
        turn = await self.runtime.prepare(10,1,'ERASE_DURING_SEND',1)
        async def send(**kwargs):
            await forget_cognitive_sources(self.pool,turn.context_id,1,[turn.event.evidence.source_id])
            return SimpleNamespace(message_id=200)
        await send_with_receipt(send,(),dict(chat_id=10,text='ERASE_DELIVERY'), 'message')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM memory_messages'),0)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_events WHERE origin='delivered_action'"),0)
            self.assertEqual(await conn.fetchval("SELECT status FROM cognitive_outbox"),'cancelled')

    async def test_same_text_has_transport_provenance_not_text_matching(self):
        first = await self.observe('SAME_TEXT',1)
        second = await self.observe('SAME_TEXT',2)
        for cid,eid,ev in (first,second):
            async with self.pool.acquire() as conn,conn.transaction():
                await conn.fetchval('SELECT revision FROM cognitive_contexts WHERE id=$1 FOR UPDATE',cid)
                await save_source_history(conn,cid,eid,ev,'User',ev.text)
        await forget_cognitive_sources(self.pool,first[0],1,[first[2].evidence.source_id])
        async with self.pool.acquire() as conn:
            surviving = await conn.fetch('SELECT metadata FROM memory_messages')
        self.assertEqual(len(surviving),1)
        self.assertEqual(object_value(surviving[0]['metadata'])['cognitive_source_id'],second[2].evidence.source_id)

    async def test_deleted_context_memory_requires_fresh_interpretation(self):
        cid,_,ev = await self.observe('THE_SOURCE',1)
        _,eid2,_ = await self.observe('REFERENCE',2)
        _,eid3,_ = await self.observe('LATER_REFERENCE',3)
        calls = self.interpreter.calls
        await forget_cognitive_sources(self.pool,cid,1,[ev.evidence.source_id])
        await self.runtime.process(cid,eid2)
        await self.runtime.process(cid,eid3)
        self.assertGreater(self.interpreter.calls,calls)
        self.assertNotIn('THE_SOURCE',str(await self.runtime.memory.artifacts(cid,1)))
        self.assertTrue(await self.runtime.repo.applied(cid,'telegram:10:3:user'))

    async def test_rp_restart_preserves_scene_and_new_scene_fences_old_turn(self):
        old = await self.runtime.prepare(10,1,'Old scene',1,'rp')
        restarted = CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at)
        self.assertEqual(await restarted.context(10,'rp'),old.event.context)
        await self.runtime.new_scene(10)
        self.assertNotEqual(await self.runtime.context(10,'rp'),old.event.context)
        async def send(**kwargs):
            self.fail('old scene was sent into the new scene')
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(send,(),dict(chat_id=10,text='Old scene answer'),'message')

    async def test_own_promise_requires_confirmed_delivery_and_has_arti_actor(self):
        turn = await self.runtime.prepare(10,1,'Please follow up.',1)
        async def send(**kwargs):
            return SimpleNamespace(message_id=200,text=kwargs['text'])
        await send_with_receipt(send,(),dict(chat_id=10,text='Я обещаю проверить результат.'),'message')
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM cognitive_events WHERE origin='delivered_action'")
        from cognition.serialization import load_event
        ev = load_event(row['payload'])
        self.interpreter.frames[ev.text] = situation(ev,intentions=[dict(span=0,key='check_result',description='Проверить результат',cue='результат',deadline=None,status='open',confidence=.9)])
        await self.runtime.process(turn.context_id,row['id'])
        # Arti must not ask the user for progress on her own promised work.
        from cognition.intentions import due_intentions
        self.at += timedelta(days=2)
        relationship = await self.runtime.memory.relationship(turn.context_id,1)
        relationship['preferences']['proactive'] = True
        with patch.object(self.runtime.memory,'relationship',AsyncMock(return_value=relationship)):
            self.assertEqual(await due_intentions(self.runtime),[])
        promises = await self.runtime.memory.artifacts(turn.context_id,1,'intention')
        self.assertEqual(promises[0]['payload']['actor_id'],'arti')
        self.assertEqual(await self.runtime.memory.artifacts(turn.context_id,1,'belief'),[])
        self.assertEqual((await self.runtime.memory.relationship(turn.context_id,1))['dimensions']['reliability']['alpha'],1)

    async def test_reminder_delivers_once_and_closed_intention_does_not_recur(self):
        cid,eid,ev = await self.runtime.ingest(10,1,'Напомни о встрече '+(AT+timedelta(seconds=2)).isoformat(),1)
        self.interpreter.frames[ev.text] = situation(ev,kind='request',intentions=[dict(span=0,key='meeting',description='Встреча',cue='',deadline=(AT+timedelta(seconds=2)).isoformat(),status='reminder',confidence=.9)])
        await self.runtime.process(cid,eid)
        self.at = AT+timedelta(seconds=3)
        from cognition.intentions import run_intention_cycle
        calls = []
        async def send(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=200)
        await run_intention_cycle(self.runtime,SimpleNamespace(send_message=send))
        await run_intention_cycle(self.runtime,SimpleNamespace(send_message=send))
        self.assertEqual(len(calls),1)

    async def test_migration_snapshot_resume_quarantines_unknown_author(self):
        async with self.pool.acquire() as conn:
            for owner,role,mode in ((1,'user','default'),(None,'user','default'),(1,'assistant','default'),(1,'user','rp')):
                await conn.execute("INSERT INTO memory_messages(chat_id,user_id,user_name,role,mode,source,message_text) VALUES(10,$1,'User',$2,$3,'old','Historical')",owner,role,mode)
        first = await HistoricalMigration(self.pool).run(max_rows=2)
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO memory_messages(chat_id,user_id,user_name,role,mode,source,message_text) VALUES(10,1,'User','user','default','new','Beyond snapshot')")
        done = await HistoricalMigration(self.pool).run()
        again = await HistoricalMigration(self.pool).run()
        self.assertFalse(first['complete'])
        self.assertTrue(done['complete'])
        self.assertEqual(done['snapshot_id'],4)
        self.assertEqual(again['rows_this_run'],0)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_legacy_map WHERE source_table='memory_messages'"),4)
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_events WHERE payload->>'text'='Beyond snapshot'"),0)

    async def test_timeline_empty_output_advances_processed_checkpoint(self):
        from memory import timeline
        async with self.pool.acquire() as conn:
            for i in range(3):
                await conn.execute("INSERT INTO memory_messages(chat_id,user_id,user_name,role,mode,source,message_text) VALUES(10,1,'User','user','default','test','Hello')")
        with patch.object(timeline,'MEMORY_TIMELINE_MIN_MESSAGES',1),patch.object(timeline.genai_client.models,'generate_content',return_value=SimpleNamespace(text='{"events":[]}')) as call:
            first = await timeline.build_timeline_events(10,dry_run=False)
            second = await timeline.build_timeline_events(10,dry_run=False)
        self.assertEqual(first['event_count'],0)
        self.assertEqual(second['message_count'],0)
        self.assertEqual(call.call_count,1)

    async def test_final_generator_uses_whole_memory_objects_and_actual_inclusion(self):
        turn = await self.runtime.prepare(10,1,'Remember this project.',1)
        from ai import generation
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Reply'))]))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(generation,'AsyncOpenAI',return_value=fake),patch.object(generation,'analyze_intent',new=AsyncMock(return_value={'web_search':False})):
            output = await generation.generate_response_stream(10,'Remember this project.','User','Recent dialogue',
                model='synthetic-chat',custom_system_prompt='Be helpful.',memory_context=turn.memory,expression_plan=turn.expression)
        self.assertEqual(output[0],'Reply')
        submitted = create.call_args.kwargs
        self.assertEqual(submitted['max_tokens'],8192)
        self.assertIn('Текущее сообщение:',submitted['messages'][1]['content'])
        self.assertIn('Use a friendly',submitted['messages'][0]['content'])
        async with self.pool.acquire() as conn:
            included = await conn.fetchval("SELECT artifact_ids FROM cognitive_retrievals WHERE stage='included'")
        self.assertTrue(included)
        self.assertEqual(set(included),{json.loads(line)['artifact_id'] for line in turn.memory.splitlines()})

    async def test_literal_expression_is_separate_from_prompt_inclusion(self):
        turn = await self.runtime.prepare(10,1,'A memorable project title.',1)
        artifact_ids = {json.loads(line)['artifact_id'] for line in turn.memory.splitlines()}
        await self.runtime.mark_included(turn,artifact_ids)
        async def send(**kwargs):
            return SimpleNamespace(message_id=200,text=kwargs['text'])
        await send_with_receipt(send,(),dict(chat_id=10,text='A memorable project title.'),'message')
        async with self.pool.acquire() as conn:
            expressed = await conn.fetchval("SELECT artifact_ids FROM cognitive_retrievals WHERE stage='expressed'")
        self.assertEqual(set(expressed),artifact_ids)

    async def test_same_turn_cannot_send_fallback_after_ambiguous_transport(self):
        await self.runtime.prepare(10,1,'Input',1)
        calls = []
        async def send(**kwargs):
            calls.append(1)
            raise TimeoutError()
        from cognition.delivery import DeliveryUnknown
        with self.assertRaises(DeliveryUnknown):
            await send_with_receipt(send,(),dict(chat_id=10,text='Output'),'message')
        with self.assertRaises(DeliveryUnknown):
            await send_with_receipt(send,(),dict(chat_id=10,text='Fallback'),'voice')
        self.assertEqual(len(calls),1)

    async def test_text_preference_reaches_transport_plan(self):
        cid,eid,ev = await self.runtime.ingest(10,1,'Только текст, без голоса.',1)
        self.interpreter.frames[ev.text] = situation(ev,kind='preference',preferences={'voice':False,'text':True})
        turn = await self.runtime.prepare(10,1,ev.text,1)
        self.assertFalse(turn.preferences['voice'])
        self.assertTrue(turn.preferences['text'])
        self.assertEqual((await self.runtime.repo.state(cid)).episodes,())

    async def test_user_can_cancel_an_existing_arti_promise_without_claiming_it(self):
        turn = await self.runtime.prepare(10,1,'Please follow up.',1)
        async def send(**kwargs):
            return SimpleNamespace(message_id=200,text=kwargs['text'])
        await send_with_receipt(send,(),dict(chat_id=10,text='Я обещаю проверить результат.'),'message')
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM cognitive_events WHERE origin='delivered_action'")
        from cognition.serialization import load_event
        ev = load_event(row['payload'])
        item = dict(span=0,key='check_result',description='Проверить результат',cue='результат',deadline=None,status='open',confidence=.9,actor='arti')
        self.interpreter.frames[ev.text] = situation(ev,intentions=[item])
        await self.runtime.process(turn.context_id,row['id'])
        self.at += timedelta(seconds=1)
        cid,eid,cancel = await self.runtime.ingest(10,1,'Отмени свою проверку результата.',2)
        self.interpreter.frames[cancel.text] = situation(cancel,kind='request',intentions=[{**item,'status':'cancelled'}])
        await self.runtime.process(cid,eid)
        commitments = await self.runtime.memory.artifacts(cid,1,'intention')
        self.assertEqual(len(commitments),1)
        self.assertEqual(commitments[0]['payload']['status'],'cancelled')

    async def test_arti_failure_does_not_reduce_user_reliability(self):
        cid,eid,ev = await self.runtime.ingest(10,1,'Ты не выполнила обещание.',1)
        self.interpreter.frames[ev.text] = situation(ev,kind='conflict',social_signal='breach',social_signal_actor='arti',outcome='confirmed')
        await self.runtime.process(cid,eid)
        r = await self.runtime.memory.relationship(cid,1)
        self.assertEqual(r['dimensions']['reliability'],{'alpha':1.,'beta':1.})

    async def test_archive_recovers_unencoded_tail_but_not_deleted_source(self):
        cid,eid,ev = await self.observe('x '*300+'UNENCODED_TAIL',1)
        ordinary = await self.runtime.memory.retrieve(cid,1,'UNENCODED_TAIL',self.at,'ordinary')
        self.assertNotIn('UNENCODED_TAIL',str(ordinary))
        archived = await self.runtime.memory.retrieve(cid,1,'UNENCODED_TAIL',self.at,'archive',archive=True)
        self.assertIn('UNENCODED_TAIL',str(archived))
        await forget_cognitive_sources(self.pool,cid,1,[ev.evidence.source_id])
        self.assertEqual(await self.runtime.memory.retrieve(cid,1,'UNENCODED_TAIL',self.at,'erased',archive=True),[])

    async def test_authority_switch_is_explicit_and_legacy_mutations_are_fenced(self):
        from database.models import ChatEmotionalState,MemoryUserProfile
        turn = await self.runtime.prepare(10,1,'Input',1)
        await ChatEmotionalState.update_state(10,'тяжёлая злость',user_id=1)
        await ChatEmotionalState.apply_mood_delta(10,{'angry':1.})
        await MemoryUserProfile.grow_closeness(10,1,'default')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM chat_emotional_states'),0)
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM memory_user_profiles'),0)
        await self.runtime.set_authority(turn.context_id,'legacy')
        CURRENT_TURN.set(None)
        await self.runtime.ingest(10,1,'After rollback',2)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT authority FROM cognitive_contexts WHERE id=$1',turn.context_id),'legacy')

    async def test_goals_survive_another_participants_busy_stream_and_rebuild(self):
        cid,eid,ev = await self.runtime.ingest(10,1,'MY_PROMISE',1)
        item = dict(span=0,key='mine',description='My promise',cue='promise',deadline=None,status='open',confidence=.9)
        self.interpreter.frames[ev.text] = situation(ev,intentions=[item])
        await self.runtime.process(cid,eid)
        first_goal = (await self.runtime.personal_state(cid,1)).situational_goals[0]['id']
        noise_source = None
        for i in range(40):
            self.at += timedelta(seconds=1)
            _,eid,noise = await self.runtime.ingest(10,2,f'OTHER_PROMISE_{i}',i+2)
            noise_source = noise.evidence.source_id
            self.interpreter.frames[noise.text] = situation(noise,intentions=[{**item,'key':str(i)}])
            await self.runtime.process(cid,eid)
        self.assertNotIn(first_goal,[g['id'] for g in (await self.runtime.repo.state(cid)).situational_goals])
        self.at += timedelta(seconds=1)
        _,eid,answer = await self.runtime.ingest(10,1,'MY_OUTCOME',100)
        self.interpreter.frames[answer.text] = Perception(answer.event_id,PERCEPTION_VERSION,
            (appraisal(goal_id=first_goal,evidence_ids=(answer.evidence.source_id,)),),situation(answer,kind='success',outcome='confirmed'))
        await self.runtime.process(cid,eid)
        self.assertTrue(await self.runtime.repo.applied(cid,answer.evidence.independent_group))
        await forget_cognitive_sources(self.pool,cid,2,[noise_source])
        self.assertIn(first_goal,[g['id'] for g in (await self.runtime.personal_state(cid,1)).situational_goals])

    async def test_failed_rebuild_is_hidden_and_recovers_before_later_observations(self):
        turn = await self.runtime.prepare(10,1,'FORGET_SECRET',1)
        with patch('cognition.forgetting.rebuild_allowed',side_effect=RuntimeError('simulated process interruption')):
            with self.assertRaises(RuntimeError):
                await forget_cognitive_sources(self.pool,turn.context_id,1,[turn.event.evidence.source_id])
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT rebuilding FROM cognitive_contexts WHERE id=$1',turn.context_id))
        self.assertEqual(await self.runtime.memory.artifacts(turn.context_id,1),[])
        send = AsyncMock()
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(send,(),dict(chat_id=10,text='stale output'),'message')
        send.assert_not_awaited()
        self.at += timedelta(seconds=1)
        cid,eid,_ = await self.runtime.ingest(10,2,'LATER_ALLOWED_INPUT',2)
        job = await self.runtime.jobs.claim(context_id=cid)
        self.assertEqual(job['kind'],'rebuild')
        await self.runtime.handle_job(job)
        await self.runtime.jobs.finish(job['id'],job['lease_token'])
        await self.runtime.process(cid,eid)
        self.assertNotIn('FORGET_SECRET',str(await self.runtime.memory.artifacts(cid,1)))
        self.assertIn('LATER_ALLOWED_INPUT',str(await self.runtime.memory.artifacts(cid,2)))

    async def test_receipt_database_failure_blocks_fallback_after_actual_send(self):
        turn = await self.runtime.prepare(10,1,'Input',1)
        send = AsyncMock(return_value=SimpleNamespace(message_id=200,text='Output'))
        from cognition.delivery import DeliveryUnknown
        with patch('cognition.delivery._confirm_transaction',side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(DeliveryUnknown):
                await send_with_receipt(send,(),dict(chat_id=10,text='Output'),'message')
        with self.assertRaises(DeliveryUnknown):
            await send_with_receipt(send,(),dict(chat_id=10,text='Fallback'),'voice')
        self.assertEqual(send.await_count,1)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT status,receipt_id FROM cognitive_outbox WHERE context_id=$1',turn.context_id)
        self.assertEqual(dict(row),dict(status='delivery_unknown',receipt_id=200))

    async def test_debounce_preserves_chat_and_scene_boundaries_and_queue_accounting(self):
        import bot.queue as queue_module
        queue = asyncio.Queue()
        context = SimpleNamespace(bot=object())
        for chat,scene,text in ((10,'a','first'),(20,'b','second'),(20,'c','third')):
            await queue.put(dict(chat_id=chat,message_id=1,context=context,user_message=text,_cognitive_context=scene))
        seen = []
        async def process(request,bot):
            seen.append((request['chat_id'],request['_cognitive_context'],request['user_message']))
        with patch.dict(queue_module._user_queues,{991:queue},clear=True),patch.object(queue_module,'_DEBOUNCE_WINDOW_SEC',.001),\
             patch.object(queue_module,'is_responses_enabled',AsyncMock(return_value=True)),patch.object(queue_module,'process_user_reply',process):
            await asyncio.wait_for(queue_module._user_text_worker(991),5)
            await asyncio.wait_for(queue.join(),1)
        self.assertEqual(seen,[(10,'a','first'),(20,'b','second'),(20,'c','third')])

    async def test_forget_selection_erases_repeated_sources_and_preserves_another_owner(self):
        import bot.commands as commands
        first = await self.observe('PRIVATE_ORCHID',1)
        await self.observe('PRIVATE_ORCHID',2)
        await self.observe('PRIVATE_ORCHID',3,owner=2)
        context = SimpleNamespace(args=['PRIVATE_ORCHID'],user_data={})
        message = SimpleNamespace(from_user=SimpleNamespace(id=1),message_id=80,reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=10),message=message)
        with patch('cognition.runtime.get_runtime',return_value=self.runtime),patch.object(commands,'is_responses_enabled',AsyncMock(return_value=True)),\
             patch.object(commands,'handle_spam_protection',AsyncMock(return_value=True)):
            await commands.handle_forget_command(update,context)
            nonce = next(iter(context.user_data['forget_selections']))
            query = SimpleNamespace(data='forget_set:'+nonce+':1',from_user=SimpleNamespace(id=1),answer=AsyncMock(),edit_message_text=AsyncMock())
            callback = SimpleNamespace(callback_query=query,effective_chat=SimpleNamespace(id=10))
            await commands.forget_callback(callback,context)
        self.assertNotIn('PRIVATE_ORCHID',str(await self.runtime.memory.artifacts(first[0],1)))
        self.assertIn('PRIVATE_ORCHID',str(await self.runtime.memory.artifacts(first[0],2)))
        self.assertFalse(context.user_data['forget_selections'])

    async def test_forgetting_unmapped_legacy_fact_keeps_valid_new_projections(self):
        from cognition.forgetting import forget_legacy_fact
        cid,_,_ = await self.observe('KEEP_VALID_NEW_SOURCE',1)
        async with self.pool.acquire() as conn:
            fid = await conn.fetchval("INSERT INTO memory_facts(chat_id,user_id,mode,fact_text) VALUES(10,1,'default','UNMAPPED_LEGACY') RETURNING id")
        self.assertTrue(await forget_legacy_fact(self.pool,10,1,fid))
        self.assertIn('KEEP_VALID_NEW_SOURCE',str(await self.runtime.memory.artifacts(cid,1)))

    async def test_foundation_repository_cannot_promote_private_evidence_to_common_scope(self):
        cid,eid,_ = await self.observe('PRIVATE_OWNER_ONE',1)
        for owner in (None,2):
            with self.assertRaises(ValueError):
                await self.runtime.repo.artifact(cid,'belief',{'value':'PRIVATE_OWNER_ONE'},owner,[eid])

    async def test_implicit_topic_association_influences_expression_with_a_bounded_causal_record(self):
        cid,eid,ev = await self.runtime.ingest(10,1,'Спасибо за заботу о проекте.',1)
        self.interpreter.frames[ev.text] = situation(ev,social_signal='care',intention_evidence='explicit')
        await self.runtime.process(cid,eid)
        self.at += timedelta(seconds=1)
        turn = await self.runtime.prepare(10,1,'Продолжим проект.',2)
        records = await self.runtime.memory.artifacts(cid,1,'regulation')
        record = next(r['payload'] for r in records if r['artifact_key']==__import__('cognition.memory_repository',fromlist=['key']).key('regulation',turn.event.event_id))
        self.assertGreater(record['implicit_expression_bias'],0.)
        self.assertLessEqual(record['implicit_expression_bias'],.06)

    async def test_global_rollback_needs_no_provider_and_overrides_persisted_active_authority(self):
        from cognition.authority import legacy_permitted
        from cognition.diagnostics import active_context
        from cognition.intentions import due_intentions
        await self.runtime.prepare(10,1,'Input',1)
        CURRENT_TURN.set(None)
        rollback = CognitiveRuntime(self.pool,None,'legacy',clock=lambda:self.at)
        with patch.object(rollback.worker,'start') as start,patch('cognition.runtime.get_runtime',return_value=rollback):
            await rollback.initialize()
            start.assert_not_called()
            self.assertTrue(await legacy_permitted(10))
            self.assertIsNone(await active_context(rollback,10,'default'))
            self.assertEqual(await due_intentions(rollback),[])
