"""Bounded hybrid retrieval integration; synthetic ledger and offline providers."""
import os
import json
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from cognition.retrieval import RetrievalResult,retrieval_guidance
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.semantic import SemanticIndex
from tests.cognition.test_full_model import RecordedInterpreter
from tests.cognition.test_semantic_index import Encoder


class RetrievalHealthTests(unittest.TestCase):
    def test_empty_health_is_per_call_and_distinguishes_failure(self):
        unavailable=RetrievalResult(diagnostics={'status':'unavailable'})
        complete=RetrievalResult(diagnostics={'status':'complete'})
        self.assertEqual(unavailable,[])
        self.assertEqual(complete,[])
        self.assertNotEqual(unavailable.diagnostics,complete.diagnostics)
        self.assertIn('could not finish',retrieval_guidance(unavailable.diagnostics))
        self.assertIn('no matching evidence',retrieval_guidance(complete.diagnostics))
        self.assertNotIn('unavailable',retrieval_guidance(complete.diagnostics))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class RetrievalCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.at=datetime(2026,1,1,tzinfo=timezone.utc)
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),clock=lambda:self.at).initialize(False)

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def source(self,text,message=1):
        cid,eid,event=await self.runtime.ingest(910,1,text,message)
        await self.runtime.process(cid,eid)
        return cid,eid,event

    async def test_inflected_tail_is_selected_without_encoder(self):
        text='Обычное описание погоды. '*1000+'С финансированием экспедиции помогал институт.'
        cid,_,_=await self.source(text)
        rows=await self.runtime.memory.retrieve(cid,1,'финансирование',self.at,'inflected')
        self.assertTrue(rows)
        self.assertIn('финансированием',str(rows[0]['details']))
        self.assertGreater(rows[0]['record_start'],20000)
        self.assertEqual(rows.diagnostics['semantic_status'],'unavailable')
        self.assertEqual(rows.diagnostics['lexical_status'],'complete')

    async def test_two_distant_passages_keep_offsets_and_single_source_identity(self):
        text='A quoted travel note: '+('neutral filler. '*100)+'redtail research vessel. '+('neutral filler. '*100)+'bluefin harbor rendezvous.'
        cid,_,event=await self.source(text)
        rows=await self.runtime.memory.retrieve(cid,1,'redtail bluefin',self.at,'multiple')
        matching=[r for r in rows if r.get('source_chunk')]
        self.assertEqual(len(matching),2)
        self.assertEqual(len({r['artifact_id'] for r in matching}),1)
        self.assertEqual({r['source_id'] for r in matching},{event.evidence.source_id})
        self.assertIn('redtail',str(matching)); self.assertIn('bluefin',str(matching))
        for row in matching:
            self.assertEqual(row['author_id'],1)
            self.assertEqual(row['details'][0]['text'],text[row['record_start']:row['record_end']])
            self.assertFalse(row['details'][0]['verbatim_verified'])
        foreign=await self.runtime.memory.retrieve(cid,2,'redtail bluefin',self.at,'foreign')
        self.assertEqual(foreign,[])

    async def test_semantic_snippets_do_not_overwrite_each_other(self):
        cid,_,_=await self.source('prefix. '+('generic filler. '*150)+'tail of source.')
        index=SemanticIndex(self.pool,Encoder())
        self.runtime.semantic=index; self.runtime.memory.semantic=index
        while await index.backfill():
            pass
        rows=await self.runtime.memory.candidates(cid,1,'paraphrase')
        self.assertGreater(len(rows),1)
        self.assertLessEqual(len(rows),3)
        self.assertEqual(rows.diagnostics['status'],'complete')

    async def test_source_lookup_timeout_reports_failure_not_no_evidence(self):
        cid,_,_=await self.source('Synthetic source for lookup')
        with patch.object(self.runtime.memory,'_retrieve',side_effect=TimeoutError):
            rows=await self.runtime.memory.retrieve(cid,1,'Synthetic',self.at,'timeout')
        self.assertEqual(rows,[])
        self.assertEqual(rows.diagnostics['status'],'timeout')

    async def test_recursive_dependency_revocation_blocks_base_raw_and_archive_paths(self):
        cid,hidden,_=await self.source('Supporting observation.',1)
        _,middle,_=await self.source('Intermediate observation.',2)
        _,short,_=await self.source('Secretneedle in selective trace.',3)
        _,long,_=await self.source('Unrelated filler. '*80+'Secretneedle at the source tail.',4)
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM cognitive_event_dependencies WHERE context_id=$1',cid)
            await conn.executemany('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',
                [(cid,middle,hidden),(cid,short,middle),(cid,long,middle)])
            source_id=await conn.fetchval('SELECT source_id FROM cognitive_events WHERE id=$1',short)
            claim=dict(subject=1,predicate='Secretneedle',value='Secretneedle',condition='',status='current',
                source_id=source_id,source_span={'text':'Secretneedle'})
            await self.runtime.memory._put(conn,cid,'belief','dependent-belief',claim,1,[short])
        self.assertTrue(await self.runtime.memory.retrieve(cid,1,'Secretneedle',self.at,'before'))
        self.assertTrue(await self.runtime.memory.beliefs_for_query(cid,1,'Secretneedle'))
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET payload=NULL WHERE id=$1',hidden)
        self.assertEqual(await self.runtime.memory.candidates(cid,1,'Secretneedle'),[])
        self.assertEqual(await self.runtime.memory.retrieve(cid,1,'Secretneedle',self.at,'after'),[])
        self.assertEqual(await self.runtime.memory.retrieve(cid,1,'Secretneedle',self.at,'archive',archive=True),[])
        self.assertEqual(await self.runtime.memory.beliefs_for_query(cid,1,'Secretneedle'),[])

    async def test_response_composer_receives_degraded_retrieval_guidance_without_records(self):
        turn=await self.runtime.prepare(910,1,'What did I say about the missing project?',1)
        turn.memory=''
        turn.retrieval_diagnostics={'status':'incomplete','indexed_chunks':8,'total_chunks':100}
        from ai import generation
        create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Reply'))]))
        fake=SimpleNamespace(close=AsyncMock(),chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(generation,'AsyncOpenAI',return_value=fake),patch.object(generation,'analyze_intent',new=AsyncMock(return_value={'web_search':False})):
            await generation.generate_response_stream(910,'What did I say about the missing project?','User','',
                model='synthetic-chat',custom_system_prompt='Be helpful.',memory_context='',expression_plan=turn.expression)
        system=create.call_args.kwargs['messages'][0]['content']
        self.assertIn('index is still being filled',system)
        self.assertIn('never claim',system)
        self.assertNotIn('total_chunks',system)

    async def test_private_prompt_inclusion_rechecks_revoked_source(self):
        from cognition.repositories import SuppressedEvidence
        cid,eid,_=await self.source('Secretneedle voyage is planned.',1)
        turn=await self.runtime.prepare(910,1,'Secretneedle voyage',2)
        ids=[json.loads(line)['artifact_id'] for line in turn.memory.splitlines()]
        self.assertTrue(ids)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET payload=NULL WHERE id=$1',eid)
        with self.assertRaises(SuppressedEvidence):
            await self.runtime.mark_included(turn,ids)

    async def test_private_final_send_rechecks_revoked_source_without_epoch_change(self):
        from cognition.delivery import send_with_receipt,DeliverySuppressed
        from cognition.repositories import SuppressedEvidence
        cid,eid,_=await self.source('Secretneedle voyage is planned.',1)
        turn=await self.runtime.prepare(910,1,'Secretneedle voyage',2)
        ids=[json.loads(line)['artifact_id'] for line in turn.memory.splitlines()]
        self.assertTrue(ids)
        await self.runtime.mark_included(turn,ids)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET payload=NULL WHERE id=$1',eid)
        send=AsyncMock(return_value=SimpleNamespace(message_id=999,text='source-dependent reply'))
        with self.assertRaises((DeliverySuppressed,SuppressedEvidence)):
            await send_with_receipt(send,(),dict(chat_id=910,text='source-dependent reply'),'message')
        send.assert_not_awaited()

    async def test_private_dialogue_reset_preserves_autobiographical_recall(self):
        cid,_,_=await self.source('Secretneedle voyage is planned.',1)
        await self.runtime.reset_history(910)
        turn=await self.runtime.prepare(910,1,'Secretneedle voyage',2)
        ids=[json.loads(line)['artifact_id'] for line in turn.memory.splitlines()]
        self.assertTrue(ids)
        await self.runtime.mark_included(turn,ids)
        self.assertEqual(set(turn.private_memory_ids),set(ids))
        archived=await self.runtime.memory.retrieve(cid,1,'Secretneedle',self.at,'after-reset',archive=True)
        self.assertTrue(archived)
