"""Real disposable SQL and active cognition, with Telegram transport replaced.

Exercise both delivery ledgers together; no provider or Telegram call is made.
"""
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ExtBot
from bot.retry_bot import RetryBot
from bot.request_runtime import CURRENT_REQUEST, checkpoint, store
from bot.request_store import RequestStore
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from tests.cognition.test_full_model import RecordedInterpreter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class ActiveRequestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.tokens=[(var,var.set(value)) for var,value in (
            (CURRENT_REQUEST,None),(CURRENT_TURN,None),
            (CURRENT_SCOPE,TransportScope(10,-1,'private',1,1)),
            (CURRENT_MATERIAL_USE,()),(CURRENT_DERIVATIVE_USE,()),(CURRENT_COMPUTATION_USE,()))]
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(start_worker=False)
        self.runtime_patch=patch('cognition.runtime.get_runtime',return_value=self.runtime)
        self.runtime_patch.start()
        self.bot=RetryBot('123456:offline-test-placeholder')
        self.calls=[]
        async def send_message(bot,*args,**kwargs):
            self.calls.append(dict(kwargs))
            return NS(message_id=100+len(self.calls),chat=NS(id=10),text=kwargs.get('text'))
        self.transport_patch=patch.object(ExtBot,'send_message',new=send_message)
        self.transport_patch.start()
        self.turn=await self.runtime.prepare(10,1,'Synthetic request',1)
        self.assertTrue(self.turn.active)
        job=await store().enqueue('text',10,-1,'delivery-fixture',{})
        self.job=await store().claim(['text'])
        self.assertEqual(job['id'],self.job['id'])
        CURRENT_REQUEST.set(self.job)

    async def asyncTearDown(self):
        self.transport_patch.stop(); self.runtime_patch.stop()
        await self.runtime.close()
        for var,token in reversed(self.tokens): var.reset(token)
        await self.db.__aexit__(None,None,None)

    async def test_cognitive_confirmation_then_request_receipt_crash_never_replays(self):
        # Crash after Telegram and cognitive confirmation, before the second
        # ledger can record its receipt. Also fail the cleanup write, as on loss
        # of the database connection/process at that boundary.
        with patch.object(RequestStore,'finish_send',new=AsyncMock(side_effect=OSError('synthetic crash'))):
            with self.assertRaises(OSError):
                await self.bot.send_message(chat_id=10,text='First and only transport')
        self.assertEqual(len(self.calls),1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'delivered')
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_sends'),'sending')
            await conn.execute("UPDATE arti_requests SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1",self.job['id'])
        self.assertIsNone(await store().claim(['text']))
        self.assertEqual((await store().status(self.job['id']))['state'],'delivery_unknown')
        self.assertEqual(len(self.calls),1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"),1)

    async def test_response_checkpoint_recovery_preserves_both_delivery_ordinals(self):
        provider=AsyncMock(return_value=('first response','second response'))
        result=await checkpoint('response',provider)
        first=await self.bot.send_message(chat_id=10,text=result[0])
        self.assertEqual(first.message_id,101)
        self.assertEqual(CURRENT_TURN.get().send_ordinal,1)
        self.assertTrue(await store().release(self.job['id'],self.job['token']))
        # A new worker has neither in-memory turn nor per-request send ordinal.
        resumed=await store().claim(['text']); CURRENT_REQUEST.set(resumed); CURRENT_TURN.set(None)
        restored=await checkpoint('response',provider)
        self.assertEqual(CURRENT_TURN.get().send_ordinal,0)
        replay=await self.bot.send_message(chat_id=10,text=restored[0])
        self.assertEqual(replay.message_id,101); self.assertEqual(len(self.calls),1)
        self.assertEqual(CURRENT_TURN.get().send_ordinal,1)
        second=await self.bot.send_message(chat_id=10,text=restored[1])
        self.assertEqual(second.message_id,102); self.assertEqual(len(self.calls),2)
        self.assertEqual(CURRENT_TURN.get().send_ordinal,2)
        provider.assert_awaited_once()
        async with self.pool.acquire() as conn:
            cognitive=await conn.fetch('SELECT delivery_key,status FROM cognitive_outbox ORDER BY id')
            self.assertEqual([r['status'] for r in cognitive],['delivered','delivered'])
            self.assertEqual([r['delivery_key'] for r in cognitive],[
                f'{self.turn.event.event_id}:{self.job["id"]}:message:1',
                f'{self.turn.event.event_id}:{self.job["id"]}:message:2'])
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"),2)
            sends=await conn.fetch('SELECT ordinal,state FROM arti_request_sends ORDER BY ordinal')
            self.assertEqual([(r['ordinal'],r['state']) for r in sends],[(1,'delivered'),(2,'delivered')])
        self.assertTrue(await store().finish(resumed['id'],resumed['token'],'completed'))

    async def test_indirect_derivative_checkpoint_is_scrubbed_on_asset_erasure(self):
        import tempfile
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from materials.derivatives import DerivativeRepository
        from materials.runtime import CURRENT_DERIVATIVE_USE, CURRENT_MATERIAL_USE, DerivativeUse
        from materials.types import AccessContext, MaterialScope
        from materials.lifecycle import MaterialLifecycle
        from bot.request_codec import dependencies,encode_value
        with tempfile.TemporaryDirectory() as directory:
            materials=MaterialRepository(self.pool); blobs=LocalBlobStore(directory)
            service=MaterialService(materials,blobs); derivatives=DerivativeRepository(materials)
            actor=AccessContext(MaterialScope('arti',10,-1,'private'),1,'user:1')
            asset=await service.ingest(b'Synthetic private material','fixture.txt',actor,'material-fixture','material-fixture')
            from projects.repository import ProjectRepository
            from projects.types import ProjectUse
            projects=ProjectRepository(materials)
            project=await projects.create(actor,'Synthetic project')
            project=await projects.attach(project.id,actor,project.revision,asset['id'])
            project_guard=ProjectUse(project.id,actor,projects,project.access_generation,project.revision)
            self.assertEqual(dependencies(await encode_value(project_guard))['material_ids'],[asset['id']])
            ancestor=await derivatives.save(actor,'material_review',{'text':'ancestor'},[{'asset_id':asset['id'],'asset_version':1}])
            child=await derivatives.save(actor,'material_review',{'text':'derived private result'},[],inputs=[ancestor])
            # Drop the redundant flattened child edge to verify recursive
            # ancestor lookup rather than relying only on direct dependencies.
            async with self.pool.acquire() as conn:
                await conn.execute('DELETE FROM material_dependencies WHERE derivative_id=$1',child)
            guard=DerivativeUse(child,actor,derivatives,'material_review')
            encoded=await encode_value(guard)
            self.assertEqual(dependencies(encoded)['material_ids'],[asset['id']])
            CURRENT_DERIVATIVE_USE.set((guard,)); CURRENT_MATERIAL_USE.set(())
            await checkpoint('derived_response',AsyncMock(return_value='derived private result'))
            unrelated=await store().enqueue('text',10,-1,'unrelated',{'ordinary':'keep'})
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval('SELECT material_ids FROM arti_requests WHERE id=$1',self.job['id']),[asset['id']])
            await MaterialLifecycle(materials,blobs).forget(asset['id'],actor)
            async with self.pool.acquire() as conn:
                row=await conn.fetchrow('SELECT state,payload,checkpoints FROM arti_requests WHERE id=$1',self.job['id'])
                self.assertEqual(row['state'],'cancelled')
                self.assertEqual(str(row['payload']),'{}'); self.assertEqual(str(row['checkpoints']),'{}')
                self.assertIn('keep',await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',unrelated['id']))
