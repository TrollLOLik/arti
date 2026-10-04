"""Private initiative invariants, using synthetic sources and disposable SQL."""
import asyncio
import os
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from cognition.intentions import due_intentions,run_intention_cycle
from cognition.private_followup import materially_revised
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.serialization import object_value,dump
from cognition.types import Origin,AudienceScope
from organizer.repository import Repository
from tests.cognition.test_full_model import RecordedInterpreter,situation


class IntentionRevisionTests(unittest.TestCase):
    def test_description_cue_confidence_and_equivalent_deadline_do_not_renew_goal(self):
        old = dict(status='open',deadline='2026-10-04T12:00:00+00:00',description='Prepare report',cue='report')
        self.assertFalse(materially_revised(old,{**old,'description':'Write the project summary',
            'cue':'summary','confidence':.99,'deadline':'2026-10-04T14:00:00+02:00'}))
        self.assertFalse(materially_revised(old,{**old,'deadline':None}))

    def test_reopening_rescheduling_or_explicit_reminder_are_material(self):
        old = dict(status='open',deadline=None)
        self.assertTrue(materially_revised({**old,'status':'cancelled'},old))
        self.assertTrue(materially_revised(old,{**old,'deadline':'2026-10-04T12:00:00+00:00'}))
        self.assertTrue(materially_revised(old,{**old,'status':'reminder'}))
        self.assertFalse(materially_revised(old,{**old,'status':'fulfilled'}))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class PrivateFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.now = datetime(2026,10,4,12,tzinfo=timezone.utc)
        self.interpreter = RecordedInterpreter()
        self.runtime = await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.now).initialize(False)
        self.organizer = Repository(self.pool)
        self.bot = NS(send_message=AsyncMock(return_value=NS(message_id=900)))
        self.mid = 0
        self.token = CURRENT_TURN.set(None)

    async def asyncTearDown(self):
        CURRENT_TURN.reset(self.token)
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def observe(self,text,*,owner=1,origin=Origin.USER,audience=None,**frame):
        self.mid += 1
        cid,eid,event = await self.runtime.ingest(owner,owner,text,self.mid,origin=origin,
            audience=audience or AudienceScope('private',owner))
        self.interpreter.frames[text] = situation(event,**frame)
        await self.runtime.process(cid,eid)
        return cid,eid,event

    async def preference(self,enabled=True,owner=1):
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES($1,true) ON CONFLICT DO NOTHING',owner)
        return await self.observe(f'Synthetic proactive preference {enabled} {self.mid}',owner=owner,
                                  kind='preference',preferences={'proactive':enabled})

    async def intention(self,*,key='report',description='Prepare orbital report',cue='report',status='open',
                        deadline=None,owner=1,origin=Origin.USER,modality='interaction',audience=None,kind=None):
        text=f'Synthetic {status}: {description} {deadline or ""} source {self.mid}'
        cid,eid,event = await self.observe(text,owner=owner,origin=origin,audience=audience,topic='orbital-report',
            kind=kind or ('request' if status=='reminder' else 'neutral'),modality=modality,intention_evidence='explicit',outcome='pending',
            intentions=[dict(span=0,key=key,description=description,cue=cue,status=status,deadline=deadline,confidence=.9)])
        rows = await self.runtime.memory.artifacts(cid,owner,'intention')
        return cid,eid,event,next((r for r in rows if r['payload']['key']==key),None)

    async def ready(self,owner=1):
        await self.organizer.set_timezone(owner,owner,'UTC')
        await self.preference(owner=owner)
        value = await self.intention(owner=owner)
        self.now += timedelta(days=2)
        return value

    async def artifact(self,cid,owner=1,key='report'):
        return next(r for r in await self.runtime.memory.artifacts(cid,owner,'intention') if r['payload']['key']==key)

    async def restart(self):
        await self.runtime.close()
        self.runtime = await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.now).initialize(False)

    async def test_relevant_goal_sends_once_without_learning_success_from_silence(self):
        cid,_,_,row = await self.ready()
        before = await self.runtime.memory.relationship(cid,1)
        self.assertEqual(len(await due_intentions(self.runtime)),1)
        await run_intention_cycle(self.runtime,self.bot)
        self.assertTrue((await self.artifact(cid))['payload']['delivered'])
        self.now += timedelta(days=3)
        await self.restart()
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('Prepare orbital report',self.bot.send_message.call_args.kwargs['text'])
        self.assertEqual((await self.artifact(cid))['payload']['status'],'open')
        self.assertEqual(await self.runtime.memory.relationship(cid,1),before)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),1)

    async def test_unknown_timezone_blocks_unsolicited_until_explicitly_known(self):
        await self.preference()
        await self.intention()
        self.now += timedelta(days=2)
        self.assertEqual(await due_intentions(self.runtime),[])
        await self.organizer.set_timezone(1,1,'UTC')
        self.assertEqual(len(await due_intentions(self.runtime)),1)

    async def test_quiet_hours_use_explicit_real_timezone(self):
        await self.ready()
        await self.organizer.set_timezone(1,1,'America/Los_Angeles')
        self.assertEqual(await due_intentions(self.runtime),[])
        await self.organizer.set_timezone(1,1,'Asia/Tokyo')
        self.assertEqual(len(await due_intentions(self.runtime)),1)

    async def test_explicit_reminder_exempts_preference_timezone_quiet_and_quota(self):
        self.now = self.now.replace(hour=2)
        await self.preference(False)
        cid,_,_,_ = await self.intention(status='reminder',cue='',deadline=(self.now-timedelta(minutes=1)).isoformat())
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertTrue((await self.artifact(cid))['payload']['delivered'])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),0)

    async def test_neutral_paraphrase_preserves_exact_reminder_request_and_sends_once(self):
        cid,request_id,request,initial = await self.intention(status='reminder',deadline=self.now.isoformat())
        _,update_id,update,current = await self.intention(status='reminder',kind='neutral',description='Current meeting description')
        self.assertEqual(current['payload']['reminder_request_source_id'],request.evidence.source_id)
        self.assertEqual(current['payload']['source_id'],update.evidence.source_id)
        self.assertEqual(current['payload']['delivery_key'],initial['payload']['delivery_key'])
        await run_intention_cycle(self.runtime,self.bot)
        await self.restart()
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('Current meeting description',self.bot.send_message.call_args.kwargs['text'])
        async with self.pool.acquire() as conn:
            delivered = await conn.fetchval("SELECT id FROM cognitive_events WHERE context_id=$1 AND origin='delivered_action'",cid)
            sources = await conn.fetchval('SELECT array_agg(source_event_id) FROM cognitive_event_dependencies WHERE context_id=$1 AND event_id=$2',cid,delivered)
            self.assertTrue({request_id,update_id}<=set(sources))

    async def test_forgetting_original_request_blocks_neutral_reminder_update(self):
        from cognition.forgetting import forget_cognitive_sources
        cid,_,request,_ = await self.intention(status='reminder',deadline=self.now.isoformat())
        await self.intention(status='reminder',kind='neutral',description='Current reminder description')
        self.assertEqual(len(await due_intentions(self.runtime)),1)
        await forget_cognitive_sources(self.pool,cid,1,[request.evidence.source_id])
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_neutral_reschedule_or_reopening_cannot_borrow_old_request(self):
        cid,_,_,initial = await self.intention(status='reminder',deadline=self.now.isoformat())
        deadline = (self.now+timedelta(days=1)).isoformat()
        await self.intention(status='reminder',kind='neutral',deadline=deadline)
        self.now += timedelta(days=1)
        self.assertEqual(await due_intentions(self.runtime),[])
        self.assertNotEqual((await self.artifact(cid))['payload']['delivery_key'],initial['payload']['delivery_key'])
        await self.intention(status='cancelled')
        await self.intention(status='reminder',kind='neutral',deadline=deadline)
        self.assertEqual(await due_intentions(self.runtime),[])
        await self.intention(status='reminder',deadline=deadline)
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()

    async def test_current_unrelated_conversation_defers_old_goal(self):
        await self.ready()
        await self.observe('Synthetic unrelated dinner question',topic='dinner',kind='request')
        self.now += timedelta(hours=1)
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_current_related_conversation_can_keep_goal_relevant_after_spacing(self):
        await self.ready()
        await self.observe('Synthetic continuing work on report',topic='orbital-report',outcome='pending')
        self.assertEqual(await due_intentions(self.runtime),[])
        self.now += timedelta(hours=1)
        self.assertEqual(len(await due_intentions(self.runtime)),1)

    async def test_current_resolved_outcome_blocks_stale_open_projection(self):
        cid,_,_,_ = await self.ready()
        await self.observe('Synthetic report finished',topic='orbital-report',kind='success',outcome='resolved')
        self.now += timedelta(hours=1)
        self.assertEqual((await self.artifact(cid))['payload']['status'],'open')
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_neutral_continuations_do_not_erase_earlier_resolved_outcome(self):
        cid,_,_,_ = await self.ready()
        await self.observe('Synthetic report finished',topic='orbital-report',kind='success',outcome='resolved')
        # More than the recent context window: terminal evidence remains a
        # source-bounded barrier rather than disappearing through truncation.
        for i in range(33):
            await self.observe(f'Synthetic general project note {i}',topic='orbital-report')
        self.now += timedelta(hours=1)
        self.assertEqual((await self.artifact(cid))['payload']['status'],'open')
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_bot_statement_does_not_answer_earlier_bot_question(self):
        await self.ready()
        await self.observe('Synthetic question about report?',origin=Origin.DELIVERED_ACTION,topic='orbital-report')
        await self.observe('Synthetic later bot statement.',origin=Origin.DELIVERED_ACTION,topic='orbital-report')
        self.now += timedelta(hours=1)
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_uninterpreted_new_input_cannot_be_assumed_receptive(self):
        await self.ready()
        await self.runtime.ingest(1,1,'Synthetic cancellation awaiting interpretation',90)
        self.now += timedelta(hours=1)
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_preference_revoked_after_discovery_blocks_send(self):
        await self.ready()
        discovered = await due_intentions(self.runtime)
        await self.preference(False)
        with patch('cognition.intentions.due_intentions',new=AsyncMock(return_value=discovered)):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_revised_projection_after_discovery_blocks_old_body(self):
        await self.ready()
        discovered = await due_intentions(self.runtime)
        await self.intention(description='Current report wording')
        with patch('cognition.intentions.due_intentions',new=AsyncMock(return_value=discovered)):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_reset_blocks_old_source_even_when_projection_epoch_advanced(self):
        cid,_,_,_ = await self.ready()
        await self.runtime.reset_history(1)
        self.assertTrue(await self.artifact(cid))
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_explicit_reminder_survives_history_reset_with_fresh_epoch(self):
        cid,_,_,_ = await self.intention(status='reminder',cue='',deadline=self.now.isoformat())
        discovered = await due_intentions(self.runtime)
        self.assertEqual(len(discovered),1)
        await self.runtime.reset_history(1)
        with patch('cognition.intentions.due_intentions',new=AsyncMock(return_value=discovered)):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertTrue((await self.artifact(cid))['payload']['delivered'])

    async def test_stop_after_discovery_blocks_unsolicited_final_send(self):
        await self.ready()
        discovered = await due_intentions(self.runtime)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE response_status SET enabled=false WHERE chat_id=1')
        with patch('cognition.intentions.due_intentions',new=AsyncMock(return_value=discovered)):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_uninterpreted_cancellation_defers_explicit_reminder(self):
        await self.intention(status='reminder',cue='',deadline=self.now.isoformat())
        cid,eid,event = await self.runtime.ingest(1,1,'Synthetic cancellation awaiting interpretation',90,
                                                 audience=AudienceScope('private',1))
        self.assertEqual(await due_intentions(self.runtime),[])
        self.interpreter.frames[event.text] = situation(event,topic='orbital-report',intentions=[dict(
            span=0,key='report',description='Prepare orbital report',cue='',status='cancelled',deadline=None,confidence=.9)])
        await self.runtime.process(cid,eid)
        self.assertEqual(await due_intentions(self.runtime),[])
        self.assertEqual((await self.artifact(cid))['payload']['status'],'cancelled')

    async def test_current_conversation_rechecked_at_outbox_fence(self):
        from cognition.delivery import send_with_receipt
        await self.ready()
        async def inject_current_input(method,args,kwargs,channel):
            await self.observe('Synthetic urgent different request',topic='new-priority',kind='request')
            return await send_with_receipt(method,args,kwargs,channel)
        with patch('cognition.delivery.send_with_receipt',new=inject_current_input):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox'),0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),0)

    async def test_reminder_cancelled_at_outbox_fence_never_sends_old_body(self):
        from cognition.delivery import send_with_receipt
        await self.intention(status='reminder',cue='',deadline=self.now.isoformat())
        async def inject_cancellation(method,args,kwargs,channel):
            await self.intention(status='cancelled')
            return await send_with_receipt(method,args,kwargs,channel)
        with patch('cognition.delivery.send_with_receipt',new=inject_cancellation):
            await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_retry_after_preserves_pending_identity_without_double_charge(self):
        from telegram.error import RetryAfter
        cid,_,_,_ = await self.ready()
        self.bot.send_message.side_effect = [RetryAfter(0),NS(message_id=900)]
        with self.assertRaises(RetryAfter):
            await run_intention_cycle(self.runtime,self.bot)
        self.assertFalse((await self.artifact(cid))['payload']['delivered'])
        await run_intention_cycle(self.runtime,self.bot)
        self.assertTrue((await self.artifact(cid))['payload']['delivered'])
        self.assertEqual(self.bot.send_message.await_count,2)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox'),1)

    async def test_omitted_deadline_in_paraphrase_neither_renews_nor_removes_schedule(self):
        cid,_,_,_ = await self.ready()
        deadline = self.now.isoformat()
        await self.intention(deadline=deadline)
        self.now += timedelta(days=2)
        await run_intention_cycle(self.runtime,self.bot)
        initial = await self.artifact(cid)
        self.assertTrue(initial['payload']['delivered'])
        await self.intention(description='Write the orbital summary')
        current = await self.artifact(cid)
        self.assertEqual(current['payload']['delivery_key'],initial['payload']['delivery_key'])
        self.assertTrue(current['payload']['delivered'])
        self.assertEqual(current['payload']['deadline'],deadline)

    async def test_explicit_reminder_rejects_unknown_or_misplaced_private_audience(self):
        await self.intention(status='reminder',deadline=self.now.isoformat(),audience=AudienceScope())
        self.assertEqual(await due_intentions(self.runtime),[])
        cid,eid,event = await self.runtime.ingest(10,1,'Synthetic misplaced reminder '+self.now.isoformat(),91,
                                                 audience=AudienceScope('private',10))
        self.interpreter.frames[event.text] = situation(event,kind='request',intentions=[dict(
            span=0,key='misplaced',description='Private data',cue='',status='reminder',deadline=self.now.isoformat(),confidence=.9)])
        await self.runtime.process(cid,eid)
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_forgotten_source_never_reappears_as_followup(self):
        from cognition.forgetting import forget_cognitive_sources
        cid,_,event,_ = await self.ready()
        await forget_cognitive_sources(self.pool,cid,1,[event.evidence.source_id])
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_unknown_transport_outcome_survives_restart_without_marking_or_retry(self):
        cid,_,_,_ = await self.ready()
        self.bot.send_message.side_effect = TimeoutError()
        await run_intention_cycle(self.runtime,self.bot)
        self.assertFalse((await self.artifact(cid))['payload']['delivered'])
        await self.restart()
        self.now += timedelta(days=1)
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'delivery_unknown')

    async def test_paraphrase_and_changed_cue_do_not_reset_delivered_goal(self):
        cid,_,_,initial = await self.ready()
        await run_intention_cycle(self.runtime,self.bot)
        await self.intention(description='Write the orbital project summary',cue='next step')
        current = await self.artifact(cid)
        self.assertEqual(current['payload']['delivery_key'],initial['payload']['delivery_key'])
        self.assertTrue(current['payload']['delivered'])
        self.now += timedelta(days=2)
        await run_intention_cycle(self.runtime,self.bot)
        self.bot.send_message.assert_awaited_once()

    async def test_reopened_goal_gets_new_key_and_one_new_followup(self):
        cid,_,_,initial = await self.ready()
        await run_intention_cycle(self.runtime,self.bot)
        await self.intention(status='cancelled')
        await self.intention()
        current = await self.artifact(cid)
        self.assertFalse(current['payload']['delivered'])
        self.assertNotEqual(current['payload']['delivery_key'],initial['payload']['delivery_key'])
        self.assertEqual(current['payload']['goal_revision'],2)
        self.now += timedelta(days=2)
        await run_intention_cycle(self.runtime,self.bot)
        await run_intention_cycle(self.runtime,self.bot)
        self.assertEqual(self.bot.send_message.await_count,2)

    async def test_rescheduled_goal_uses_new_key_waits_for_current_deadline(self):
        cid,_,_,initial = await self.ready()
        await run_intention_cycle(self.runtime,self.bot)
        deadline = self.now+timedelta(days=3)
        await self.intention(deadline=deadline.isoformat())
        self.assertNotEqual((await self.artifact(cid))['payload']['delivery_key'],initial['payload']['delivery_key'])
        self.now += timedelta(days=2)
        self.assertEqual(await due_intentions(self.runtime),[])
        self.now = deadline
        await run_intention_cycle(self.runtime,self.bot)
        self.assertEqual(self.bot.send_message.await_count,2)

    async def test_receipt_for_old_revision_cannot_complete_new_goal(self):
        cid,_,_,_ = await self.ready()
        async def send(**kwargs):
            await self.intention(deadline=(self.now+timedelta(days=3)).isoformat())
            return NS(message_id=900)
        self.bot.send_message.side_effect = send
        await run_intention_cycle(self.runtime,self.bot)
        self.assertFalse((await self.artifact(cid))['payload']['delivered'])
        self.assertEqual((await self.artifact(cid))['payload']['goal_revision'],2)

    async def test_simultaneous_ticks_and_multiple_goals_do_not_burst(self):
        await self.organizer.set_timezone(1,1,'UTC')
        await self.preference()
        await self.intention(key='report1')
        await self.intention(key='report2')
        self.now += timedelta(days=2)
        await asyncio.gather(run_intention_cycle(self.runtime,self.bot),run_intention_cycle(self.runtime,self.bot))
        self.bot.send_message.assert_awaited_once()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),1)

    async def test_private_scheduler_respects_shared_burst_without_losing_pending_goal(self):
        for owner in range(1,5):
            await self.ready(owner)
        await run_intention_cycle(self.runtime,self.bot)
        self.assertEqual(self.bot.send_message.await_count,3)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'),3)
            pending = await conn.fetchval("SELECT count(*) FROM cognitive_artifacts WHERE kind='intention' AND payload->>'delivered'='false'")
            self.assertEqual(pending,1)
        self.now += timedelta(minutes=2)
        await run_intention_cycle(self.runtime,self.bot)
        self.assertEqual(self.bot.send_message.await_count,4)

    async def test_private_goal_cannot_use_another_owner_or_audience(self):
        cid,eid,event,row = await self.ready()
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_artifacts SET owner_id=2 WHERE id=$1',row['id'])
        self.assertEqual(await due_intentions(self.runtime),[])
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_artifacts SET owner_id=1 WHERE id=$1',row['id'])
            payload = object_value(dump(event))
            payload['audience'] = dict(kind='private',chat_id=2,topic_id=-1)
            await conn.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1',eid,dump(payload))
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_artis_own_commitment_is_not_shifted_onto_user(self):
        await self.organizer.set_timezone(1,1,'UTC')
        await self.preference()
        await self.intention(origin=Origin.DELIVERED_ACTION)
        self.now += timedelta(days=2)
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_quoted_or_hypothetical_intentions_never_schedule(self):
        cid,_,_,row = await self.intention(modality='quoted',status='reminder',deadline=self.now.isoformat())
        self.assertIsNone(row)
        await self.intention(modality='hypothetical')
        self.now += timedelta(days=2)
        self.assertEqual(await self.runtime.memory.artifacts(cid,1,'intention'),[])
        self.assertEqual(await due_intentions(self.runtime),[])
