"""Fair head-of-context scheduling against disposable PostgreSQL only."""
import asyncio
import os
import unittest
from cognition.runtime import CognitiveRuntime
from cognition.scope import CURRENT_SCOPE,TransportScope
from tests.cognition.test_full_model import RecordedInterpreter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class CognitiveJobFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(start_worker=False)
        self.scope=CURRENT_SCOPE.set(None)

    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.scope)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def ingest(self,chat,mid):
        token=CURRENT_SCOPE.set(TransportScope(chat,-1,'private',chat,mid))
        try:
            return await self.runtime.ingest(chat,chat,'Synthetic queue observation',mid)
        finally: CURRENT_SCOPE.reset(token)

    async def test_continuous_old_chats_do_not_starve_ready_new_chat(self):
        contexts={chat:(await self.ingest(chat,1))[0] for chat in (100,200,300,400,500)}
        jobs=[await self.runtime.jobs.claim() for _ in range(4)]
        self.assertEqual({j['context_id'] for j in jobs},{contexts[c] for c in (100,200,300,400)})
        for job in jobs: self.assertTrue(await self.runtime.jobs.finish(job['id'],job['lease_token']))
        for chat in (100,200,300,400): await self.ingest(chat,2)
        next_job=await self.runtime.jobs.claim()
        self.assertEqual(next_job['context_id'],contexts[500])

    async def test_parallel_claims_keep_one_lease_per_context_and_fifo(self):
        first=[]
        for chat in (100,200):
            cid,eid,_=await self.ingest(chat,1); first.append((cid,eid))
            await self.ingest(chat,2)
        claims=await asyncio.gather(*(self.runtime.jobs.claim() for _ in range(4)))
        jobs=[j for j in claims if j]
        self.assertEqual(len(jobs),2)
        self.assertEqual({(j['context_id'],j['event_id']) for j in jobs},set(first))
        self.assertIsNone(await self.runtime.jobs.claim())
        for job in jobs: await self.runtime.jobs.finish(job['id'],job['lease_token'])
        second=[await self.runtime.jobs.claim() for _ in range(2)]
        self.assertEqual(len({j['context_id'] for j in second}),2)
        self.assertTrue(all(j['event_id'] not in {e for _,e in first} for j in second))

    async def test_unavailable_head_preserves_local_order_without_blocking_other_chat(self):
        cid,eid,_=await self.ingest(100,1)
        await self.ingest(100,2)
        other,_,_=await self.ingest(200,1)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_jobs SET available_at=NOW()+INTERVAL '1 hour' WHERE context_id=$1 AND event_id=$2",cid,eid)
        job=await self.runtime.jobs.claim()
        self.assertEqual(job['context_id'],other)
        self.assertIsNone(await self.runtime.jobs.claim(context_id=cid))

    async def test_rebuild_and_nonreplay_priority_within_context_still_hold(self):
        cid,eid,_=await self.ingest(100,1)
        await self.runtime.jobs.enqueue(cid,eid,'replay')
        await self.runtime.jobs.enqueue(cid,eid,'rebuild')
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1',cid)
        job=await self.runtime.jobs.claim(context_id=cid)
        self.assertEqual(job['kind'],'rebuild')
        await self.runtime.jobs.finish(job['id'],job['lease_token'])
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=FALSE WHERE id=$1',cid)
        job=await self.runtime.jobs.claim(context_id=cid)
        self.assertEqual(job['kind'],'interpret')
