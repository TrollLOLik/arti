import asyncio
import os
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

from cognition.types import Perception,PERCEPTION_VERSION,Origin
from cognition.situations import Situation,calibrated_appraisals
from cognition.relationships import initial_relationship,relationship_transition,relationship_view
from cognition.memory_dynamics import detail_state,reconstruct,encode_details
from cognition.prompting import assemble_prompt,TokenCounter,PromptBudget
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.delivery import send_with_receipt,DeliveryUnknown,DeliverySuppressed
from cognition.forgetting import forget_cognitive_sources
from cognition.serialization import object_value
from tests.cognition.test_affect import event,appraisal,perception,AT


def situation(ev,**changes):
    text = ev.text
    data = dict(topic='project',kind='neutral',modality='interaction',intention_evidence='unobserved',outcome='unknown',
                spans=[dict(start=0,end=len(text),text=text)] if text else [],details=[],beliefs=[],intentions=[],revisions=[],
                preferences={},social_signal='contact')
    data.update(changes)
    return Situation.from_dict(data,ev)


class SemanticDynamicsTests(unittest.TestCase):
    def test_ambiguous_intent_does_not_become_confident_hostility(self):
        ev = event(text='Ничего себе эксперт нашёлся.')
        s = situation(ev,kind='conflict',intention_evidence='ambiguous')
        p = replace(perception(ev,appraisal()),situation=s)
        result = calibrated_appraisals(p).appraisals[0]
        self.assertLessEqual(result.intentionality,.35)
        self.assertLessEqual(result.confidence,.45)

    def test_boundary_updates_preferences_without_emotional_reward(self):
        ev = event(text='Пиши только когда я сам пишу.')
        s = situation(ev,kind='preference',preferences={'proactive':False})
        p = replace(perception(ev,appraisal(congruence=1)),situation=s)
        self.assertEqual(calibrated_appraisals(p).appraisals[0].relevance,0)
        r = relationship_transition(initial_relationship(),ev,s)
        self.assertFalse(r['preferences']['proactive'])
        self.assertEqual(r['dimensions']['reliability'],{'alpha':1.,'beta':1.})

    def test_source_spans_reject_fabricated_wording(self):
        ev = event(text='Я живу в Казани.')
        with self.assertRaises(ValueError):
            situation(ev,spans=[dict(start=0,end=3,text='Я живу в Париже')])

    def test_noise_and_help_have_distinct_social_trajectories(self):
        noise,helped = initial_relationship(),initial_relationship()
        for i in range(100):
            ev = event(str(i),at=AT+timedelta(seconds=i),text='hello')
            noise = relationship_transition(noise,ev,situation(ev))
        ev = event('fulfilled',at=AT+timedelta(hours=1),text='Обещание выполнено.')
        helped = relationship_transition(helped,ev,situation(ev,social_signal='fulfilled',outcome='confirmed',intention_evidence='explicit'))
        self.assertEqual(noise['dimensions']['reliability']['alpha'],1)
        self.assertGreater(helped['dimensions']['reliability']['alpha'],noise['dimensions']['reliability']['alpha'])

    def test_silence_changes_currency_without_negative_evidence(self):
        ev = event(text='Спасибо за помощь.')
        r = relationship_transition(initial_relationship(),ev,situation(ev,social_signal='care'))
        before = relationship_view(r,AT)
        later = relationship_view(r,AT+timedelta(days=30))
        self.assertEqual(before['dimensions']['warmth']['value'],later['dimensions']['warmth']['value'])
        self.assertGreater(before['dimensions']['warmth']['confidence'],later['dimensions']['warmth']['confidence'])

    def test_wording_and_gist_have_different_accessibility(self):
        ev = event(text='Запустили проект 14 мая.')
        s = situation(ev,details=[dict(span=0,kind='gist',centrality=.8,confidence=.9),dict(span=0,kind='wording',centrality=.8,confidence=.9)])
        details = encode_details(ev,s)
        old = AT+timedelta(days=90)
        self.assertGreater(detail_state(details[0],AT,old)['accessibility'],detail_state(details[1],AT,old)['accessibility'])
        self.assertEqual(details[0]['confidence'],detail_state(details[0],AT,old)['confidence'])

    def test_specific_cue_and_archive_recover_source_without_inventing(self):
        ev = event(text='Название: Лунный мост.')
        details = encode_details(ev,situation(ev))
        t = dict(details=details,observed_at=AT.isoformat(),source_id='s',modality='reported')
        old = AT+timedelta(days=3650)
        ordinary = reconstruct(t,old)
        cued = reconstruct(t,old,cue=1)
        archive = reconstruct(t,old,archive=True)
        self.assertGreaterEqual(len(cued['details']),len(ordinary['details']))
        self.assertEqual(archive['details'][0]['text'],ev.text)
        self.assertEqual(archive['time_precision'],'source_record')

    def test_whole_prompt_preserves_task_and_bounds_all_parts(self):
        system,task = 'System instruction','Текущее важное задание'
        result,report = assemble_prompt(system,task,'old\n'*10000,'memory\n'*10000,model='unknown')
        self.assertIn(task,result)
        self.assertLessEqual(report['input_tokens'],report['input_limit'])
        self.assertEqual(result.count('<user_memory>'),result.count('</user_memory>'))
        with self.assertRaises(ValueError):
            assemble_prompt(system,'x'*40000)


class RecordedInterpreter:
    def __init__(self):
        self.calls = 0
        self.frames = {}

    async def interpret(self,ev,**kwargs):
        self.calls += 1
        s = self.frames.get(ev.text) or situation(ev)
        if isinstance(s,Perception):
            return SimpleNamespace(perception=s)
        return SimpleNamespace(perception=Perception(ev.event_id,PERCEPTION_VERSION,(),s))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class FullCycleTests(unittest.IsolatedAsyncioTestCase):
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

    async def observe(self,text='Remember the lunar bridge.',owner=1,message=1):
        cid,eid,ev = await self.runtime.ingest(10,owner,text,message)
        await self.runtime.process(cid,eid)
        self.at += timedelta(seconds=1)
        return cid,eid,ev

    async def test_full_encode_is_idempotent_and_checkpoints_empty_meaning(self):
        cid,eid,ev = await self.observe()
        await self.runtime.process(cid,eid)
        self.assertEqual(self.interpreter.calls,1)
        self.assertEqual(len(await self.runtime.memory.artifacts(cid,1,'trace')),1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_projection_effects WHERE phase='encode'"),1)

    async def test_scoped_search_and_graph_never_expose_other_owner(self):
        cid,_,_ = await self.observe('PRIVATE_ALPHA',1,1)
        await self.observe('PRIVATE_BETA',2,2)
        recalled = await self.runtime.memory.retrieve(cid,1,'PRIVATE',self.at,'search')
        self.assertTrue(recalled)
        self.assertNotIn('PRIVATE_BETA',str(recalled))

    async def test_semantic_correction_versions_and_repeated_assertion(self):
        for i,text in enumerate(('Казань','Казань','Пермь'),1):
            cid,eid,ev = await self.runtime.ingest(10,1,text,i)
            self.interpreter.frames[text] = situation(ev,beliefs=[dict(span=0,subject=1,predicate='city',value=text,condition='',assertion='correction' if text=='Пермь' else 'explicit',confidence=.9)])
            await self.runtime.process(cid,eid)
            self.at += timedelta(seconds=1)
        current = await self.runtime.memory.artifacts(cid,1,'belief')
        history = await self.runtime.memory.artifacts(cid,1,'belief_version')
        self.assertEqual(current[0]['payload']['value'],'Пермь')
        self.assertEqual(history[0]['payload']['value'],'Казань')
        self.assertEqual(len(history[0]['payload']['support_groups']),1)

    async def test_replay_does_not_create_trust_or_belief_confirmation(self):
        cid,eid,ev = await self.observe()
        before = await self.runtime.memory.relationship(cid,1)
        changed = await self.runtime.memory.replay(cid,eid,self.at+timedelta(days=2))
        self.assertTrue(changed)
        self.assertEqual(before,await self.runtime.memory.relationship(cid,1))
        self.assertEqual(await self.runtime.memory.replay(cid,eid,self.at+timedelta(days=3)),[])

    async def test_forget_rebuilds_social_views_and_prevents_source_resurrection(self):
        cid,eid,ev = await self.observe('ERASE_PRIVATE',1,1)
        await self.observe('KEEP_OTHER',2,2)
        await forget_cognitive_sources(self.pool,cid,1,[ev.evidence.source_id])
        self.assertEqual(await self.runtime.memory.artifacts(cid,1,'trace'),[])
        self.assertEqual(len(await self.runtime.memory.artifacts(cid,2,'trace')),1)
        from cognition.repositories import SuppressedEvidence
        with self.assertRaises(SuppressedEvidence):
            await self.runtime.ingest(10,1,'ERASE_PRIVATE',1)
        async with self.pool.acquire() as conn:
            for table,column in (('cognitive_events','payload'),('cognitive_artifacts','payload'),('cognitive_outbox','payload')):
                values = await conn.fetch(f'SELECT {column} FROM {table}')
                self.assertNotIn('ERASE_PRIVATE',str(values))

    async def test_confirmed_delivery_is_distinct_from_generation_and_survives_restart(self):
        turn = await self.runtime.prepare(10,1,'hello',1)
        async def send(**kwargs):
            return SimpleNamespace(message_id=200,chat=SimpleNamespace(id=10))
        result = await send_with_receipt(send,(),{'chat_id':10,'text':'I delivered help.'},'message')
        self.assertEqual(result.message_id,200)
        turn2 = await self.runtime.prepare(10,1,'hello',1)
        self.assertTrue(turn2.repeated_delivery)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM cognitive_events WHERE origin='delivered_action'"),1)

    async def test_ambiguous_send_is_never_automatically_retried(self):
        await self.runtime.prepare(10,1,'hello',1)
        calls = []
        async def send(**kwargs):
            calls.append(1)
            raise TimeoutError('transport response missing')
        with self.assertRaises(DeliveryUnknown):
            await send_with_receipt(send,(),{'chat_id':10,'text':'output'},'message')
        again = await self.runtime.prepare(10,1,'hello',1)
        self.assertTrue(again.repeated_delivery)
        with self.assertRaises(DeliveryUnknown):
            await send_with_receipt(send,(),{'chat_id':10,'text':'output'},'message')
        self.assertEqual(len(calls),1)

    async def test_forget_fences_prepared_transport(self):
        turn = await self.runtime.prepare(10,1,'ERASE',1)
        await forget_cognitive_sources(self.pool,turn.context_id,1,[turn.event.evidence.source_id])
        async def send(**kwargs):
            self.fail('suppressed event reached external transport')
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(send,(),{'chat_id':10,'text':'output'},'message')

    async def test_delayed_replay_does_not_block_new_input_jobs(self):
        cid,eid,_ = await self.observe()
        self.assertIsNone(await self.runtime.jobs.claim())
        _,eid2,_ = await self.runtime.ingest(10,1,'second',2)
        claimed2 = await self.runtime.jobs.claim()
        self.assertEqual(claimed2['event_id'],eid2)
