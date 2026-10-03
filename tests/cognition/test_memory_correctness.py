"""Synthetic source-grounding regressions; no answer provider is invoked."""
import json
import os
import unittest
from datetime import datetime,timedelta,timezone

from cognition.prompting import memory_for_prompt,assemble_prompt,TokenCounter
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.serialization import dump,object_value
from cognition.source_chunks import source_chunks,source_chunk_count
from tests.cognition.test_full_model import RecordedInterpreter,situation

AT=datetime(2026,1,1,tzinfo=timezone.utc)


class ChunkWindowTests(unittest.TestCase):
    def test_maximum_source_bounds_include_tail_and_exact_offsets(self):
        text='x'*99980+'meaningful tail text'
        chunks=source_chunks(text)
        self.assertEqual(len(chunks),source_chunk_count(text))
        self.assertGreater(len(chunks),32)
        self.assertTrue(all(right[0]<=left[1] for left,right in zip(chunks,chunks[1:])))
        self.assertEqual(chunks[-1][1],len(text))
        for start,end,excerpt in chunks:
            self.assertEqual(text[start:end],excerpt)
            self.assertLessEqual(len(excerpt),640)

    def test_prompt_keeps_legacy_temporal_uncertainty(self):
        recollection=dict(artifact_id=1,source_id='s',details=[],familiarity=.2,
            observed_at=AT.isoformat(),occurred_at=None,time_basis='observed_at',time_precision='year')
        prompt,_=memory_for_prompt([recollection],[])
        record=json.loads(prompt)
        self.assertIsNone(record['occurred_at'])
        self.assertEqual(record['time_basis'],'observed_at')


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class MemoryCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    encoder_factory = None

    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.semantic import SemanticIndex
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.at=AT
        self.interpreter=RecordedInterpreter()
        self.index=SemanticIndex(self.pool,self.encoder_factory()) if self.encoder_factory else None
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,clock=lambda:self.at,semantic=self.index).initialize(False)
        if self.index:
            # Explicit warm-up; cold encoder startup is not a recall timeout.
            self.assertIsNotNone(await self.index.encoder.encode(['synthetic warmup'],timeout=30))

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def add(self,text,message,belief=None,owner=1,chat=701,modality='interaction',occurred_at=None):
        cid,eid,event=await self.runtime.ingest(chat,owner,text,message,occurred_at=occurred_at)
        self.interpreter.frames[text]=situation(event,modality=modality,
            beliefs=[] if belief is None else [dict(span=0,subject=owner,predicate=belief[0],value=belief[1],
                condition='',assertion='correction' if message==2 else 'explicit',confidence=.9)])
        await self.runtime.process(cid,eid)
        self.at+=timedelta(seconds=1)
        return cid,eid,event

    async def index_sources(self):
        if self.index:
            while await self.index.backfill(16):
                pass

    async def test_current_correction_survives_distractors_in_final_prompt(self):
        cid,_,old=await self.add('I live in Kazan.',1,('city','Kazan'))
        _,_,new=await self.add('Actually, Tomsk.',2,('city','Tomsk'))
        for n in range(3,35):
            await self.add(f'My favorite object number {n} is item{n}.',n,(f'object{n}',f'item{n}') if n<22 else None)
        await self.index_sources()
        old_window=await self.runtime.memory.artifacts(cid,1,'belief',limit=16)
        self.assertFalse(any(r['payload']['predicate']=='city' for r in old_window))
        turn=await self.runtime.prepare(701,1,'Where do I live?',35)
        records=[json.loads(line) for line in turn.memory.splitlines()]
        city=next(r for r in records if r.get('predicate')=='city')
        self.assertEqual(city['value'],'Tomsk'); self.assertEqual(city['status'],'current')
        self.assertEqual(city['source_id'],new.evidence.source_id)
        historical=[r for r in records if r['source_id']==old.evidence.source_id]
        for record in historical:
            self.assertEqual(record['belief_history'][0]['status'],'superseded')
            self.assertEqual(record['belief_history'][0]['superseded_by'],new.evidence.source_id)
        final,budget=assemble_prompt('Answer using source-grounded data.','Where do I live?',memory=turn.memory)
        self.assertIn('Tomsk',final)
        self.assertLessEqual(TokenCounter('').count(turn.memory),4000)
        self.assertLessEqual(budget['input_tokens'],budget['input_limit'])

    async def test_history_retains_original_truth_and_existing_schema_links(self):
        cid,_,old=await self.add('I live in Kazan.',1,('city','Kazan'))
        await self.add('Actually, Tomsk.',2,('city','Tomsk'))
        # Existing stored belief/version payloads have no new explicit link fields.
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_artifacts SET payload=payload-'superseded_by'-'supersedes' WHERE kind IN ('belief','belief_version')")
        recalled=await self.runtime.memory.retrieve(cid,1,'Where did I live in Kazan before?',self.at,'historical')
        beliefs=await self.runtime.memory.beliefs_for_query(cid,1,'Where did I live in Kazan before?',recalled)
        prompt,_=memory_for_prompt(recalled,beliefs)
        self.assertIn('Kazan',prompt); self.assertIn('Tomsk',prompt)
        old_record=next(r for r in recalled if r['source_id']==old.evidence.source_id)
        self.assertEqual(old_record['belief_history'][0]['status'],'superseded')
        self.assertIsNotNone(old_record['belief_history'][0]['valid_until'])

    async def test_distinct_source_dates_and_belief_status_reach_prompt(self):
        occurred=AT-timedelta(days=8,hours=3)
        cid,_,event=await self.add('We decided yesterday to visit the coast.',1,('trip','coast'),occurred_at=occurred)
        recalled=await self.runtime.memory.retrieve(cid,1,'visit coast',self.at,'dates')
        beliefs=await self.runtime.memory.beliefs_for_query(cid,1,'coast',recalled)
        prompt,_=memory_for_prompt(recalled,beliefs)
        rows=[json.loads(line) for line in prompt.splitlines()]
        for row in rows:
            self.assertEqual(row['observed_at'],event.observed_at.isoformat())
            self.assertEqual(row['occurred_at'],occurred.isoformat())
        belief=next(r for r in rows if 'predicate' in r)
        self.assertEqual(belief['valid_from'],occurred.isoformat())
        self.assertEqual(belief['status'],'current')

    async def test_empty_retrieval_never_rehearses_missing_details(self):
        cid,_,_=await self.add('Bridge opening is 14 May 2026.',1)
        await self.index_sources(); self.at+=timedelta(days=400)
        first=await self.runtime.memory.retrieve(cid,1,'bridge opening',self.at,'first')
        second=await self.runtime.memory.retrieve(cid,1,'bridge opening',self.at+timedelta(seconds=1),'second')
        self.assertTrue(first); self.assertTrue(second)
        self.assertFalse(first[0]['details']); self.assertFalse(second[0]['details'])
        trace=(await self.runtime.memory.artifacts(cid,1,'trace'))[0]['payload']
        self.assertEqual(trace['details'][0]['recall_count'],0)
        archived=await self.runtime.memory.retrieve(cid,1,'bridge opening',self.at,'archive',archive=True)
        self.assertTrue(archived[0]['details'][0]['verbatim_verified'])
        third=await self.runtime.memory.retrieve(cid,1,'bridge opening',self.at+timedelta(seconds=2),'third')
        self.assertFalse(third[0]['details'])

    async def test_old_prefix_trace_tail_is_retrievable_without_claim_promotion(self):
        text='General housekeeping commentary. '*30+'My dog is named Nebula.'
        cid,_,event=await self.add(text,1)
        trace=(await self.runtime.memory.artifacts(cid,1,'trace'))[0]['payload']
        self.assertNotIn('Nebula',trace['gist'])
        await self.index_sources()
        recalled=await self.runtime.memory.retrieve(cid,1,'What is my dog named?',self.at,'tail')
        self.assertIn('Nebula',str(recalled))
        found=next(r for r in recalled if 'Nebula' in str(r['details']))
        self.assertTrue(found['source_chunk'])
        self.assertEqual(found['source_id'],event.evidence.source_id)
        self.assertFalse(found['details'][0]['verbatim_verified'])
        self.assertEqual(await self.runtime.memory.artifacts(cid,1,'belief'),[])
        self.assertEqual(await self.runtime.memory.retrieve(cid,2,'dog named',self.at,'foreign'),[])

    async def test_quoted_tail_stays_quoted_and_erasure_removes_every_chunk(self):
        from cognition.forgetting import forget_cognitive_sources
        text='A fictional narrator says: '+('ordinary scene setting. '*30)+'My dog is named Nebula.'
        cid,_,event=await self.add(text,1,('dog_name','Nebula'),modality='quoted')
        await self.index_sources()
        recalled=await self.runtime.memory.retrieve(cid,1,'dog named',self.at,'quote')
        self.assertIn('Nebula',str(recalled)); self.assertEqual(recalled[0]['modality'],'quoted')
        self.assertIn('fictional narrator',recalled[0]['source_prefix'])
        self.assertEqual(recalled[0]['evidence_status'],'observed_message_excerpt_not_personal_fact')
        self.assertEqual(recalled[0]['author_id'],1)
        self.assertEqual(await self.runtime.memory.artifacts(cid,1,'belief'),[])
        await forget_cognitive_sources(self.pool,cid,1,[event.evidence.source_id])
        self.assertEqual(await self.runtime.memory.retrieve(cid,1,'dog named',self.at,'erased'),[])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_semantic_vectors'),0)

    async def test_old_long_source_cannot_leak_faded_prefix_on_empty_recall(self):
        text='My dog is named Nebula. '+('General housekeeping commentary. '*30)
        cid,_,_=await self.add(text,1)
        await self.index_sources(); self.at+=timedelta(days=400)
        for cycle in ('old_long_first','old_long_second'):
            recalled=await self.runtime.memory.retrieve(cid,1,'dog named',self.at,cycle)
            self.assertTrue(recalled)
            self.assertFalse(recalled[0]['details'])
            prompt,_=memory_for_prompt(recalled,[])
            self.assertNotIn('Nebula',prompt)
            self.at+=timedelta(seconds=1)

    async def test_chunk_prefix_does_not_restore_separately_faded_date(self):
        text='Opening date: 14 May 2026. '+('General housekeeping commentary. '*30)+'My dog is named Nebula.'
        cid,_,_=await self.add(text,1)
        row=(await self.runtime.memory.artifacts(cid,1,'trace'))[0]
        payload=row['payload']; value='14 May 2026'; start=text.index(value)
        payload['details'].append(dict(text=value,start=start,end=start+len(value),kind='date',centrality=.8,
            confidence=.9,fidelity=1.,strength=.8,stability_days=8.,vividness=.4,last_recalled=None,recall_count=0))
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1',row['id'],dump(payload))
        await self.index_sources(); self.at+=timedelta(days=90)
        recalled=await self.runtime.memory.retrieve(cid,1,'dog named',self.at,'faded-prefix')
        prompt,_=memory_for_prompt(recalled,[])
        self.assertIn('Nebula',prompt)
        self.assertNotIn('14 May 2026',prompt)
