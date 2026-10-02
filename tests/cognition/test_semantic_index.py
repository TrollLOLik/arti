import asyncio
import os
import unittest
from unittest.mock import patch

from cognition.semantic import SemanticIndex,validated_vector,VERSION
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.serialization import dump
from tests.cognition.test_full_model import RecordedInterpreter


class VectorTests(unittest.TestCase):
    def test_dimensions_finiteness_and_normalization(self):
        for vector in ([0.]*384,[1.]*192,[float('nan')]*384,[float('inf')]*384):
            with self.assertRaises(ValueError): validated_vector(vector)
        vector=validated_vector([1.]*384)
        self.assertAlmostEqual(sum(x*x for x in vector),1)


class Encoder:
    async def encode(self,texts,timeout=.6):
        return [[1.]+[0.]*383 for _ in texts]
    async def close(self): pass


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','isolated SQL tests')
class SemanticSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.index=SemanticIndex(self.pool,Encoder())
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),semantic=self.index).initialize(start_worker=False)

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def observe(self,owner=1,chat=10,message=1):
        cid,eid,ev=await self.runtime.ingest(chat,owner,'Synthetic permitted memory',message)
        await self.runtime.process(cid,eid)
        return cid,eid,ev

    async def test_owner_context_epoch_and_source_erasure(self):
        from cognition.forgetting import forget_cognitive_sources
        cid,eid,ev=await self.observe()
        await self.observe(owner=2,message=2); other,_,_=await self.observe(chat=20)
        self.assertEqual(await self.index.backfill(),3)
        rows=await self.index.search(cid,1,'paraphrase')
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['owner_id'],1)
        self.assertEqual(len(await self.index.search(other,1,'paraphrase')),1)
        await forget_cognitive_sources(self.pool,cid,1,[ev.evidence.source_id])
        self.assertEqual(await self.index.search(cid,1,'paraphrase'),[])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_semantic_vectors WHERE artifact_id=$1',rows[0]['id']),0)

    async def test_late_embedding_cannot_restore_forgotten_source(self):
        from cognition.forgetting import forget_cognitive_sources
        cid,eid,ev=await self.observe()
        entered=asyncio.Event(); release=asyncio.Event()
        async def delayed(texts,timeout=5):
            entered.set(); await release.wait(); return await Encoder().encode(texts)
        self.index.encoder.encode=delayed
        task=asyncio.create_task(self.index.backfill())
        await entered.wait()
        await forget_cognitive_sources(self.pool,cid,1,[ev.evidence.source_id])
        release.set(); self.assertEqual(await task,0)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_semantic_vectors'),0)

    async def test_correction_invalidates_vector_but_rehearsal_does_not(self):
        cid,_,_=await self.observe(); await self.index.backfill()
        row=(await self.index.search(cid,1,'paraphrase'))[0]
        async with self.pool.acquire() as conn:
            payload=row['payload']; payload['replay_count']+=1
            await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1',row['id'],dump(payload))
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_semantic_vectors'),1)
            payload['gist']='Corrected permitted source'
            await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb,revision=revision+1 WHERE id=$1',row['id'],dump(payload))
            self.assertEqual(await conn.fetchval('SELECT COUNT(*) FROM cognitive_semantic_vectors'),0)
        self.assertEqual(await self.index.backfill(),1)

    async def test_candidates_include_semantic_match_outside_lexical_window(self):
        cid,_,_=await self.observe(); await self.index.backfill()
        with patch.object(self.runtime.memory,'artifacts',return_value=[]):
            rows=await self.runtime.memory.candidates(cid,1,'unrelated wording',limit=1)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['semantic_score'],1)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1 WHERE id=$1',cid)
        self.assertEqual(await self.index.search(cid,1,'paraphrase'),[])

    async def test_missing_encoder_falls_back_without_losing_lexical_results(self):
        cid,_,_=await self.observe()
        async def unavailable(*args,**kwargs): return None
        self.index.encoder.encode=unavailable
        rows=await self.runtime.memory.candidates(cid,1,'Synthetic')
        self.assertEqual(len(rows),1)
