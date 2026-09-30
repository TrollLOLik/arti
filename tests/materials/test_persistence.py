import asyncio
from dataclasses import replace
import os
import tempfile
import unittest
from materials.extractors.basic import BasicExtractor
from materials.lifecycle import MaterialLifecycle, forget_sources
from materials.repository import MaterialRepository, Quotas
from materials.service import MaterialService
from materials.storage import LocalBlobStore
from materials.types import AccessContext, EvidenceRef, MaterialError, MaterialScope, Locator


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','set ARTI_TEST_DB=1 for disposable PostgreSQL')
class MaterialPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.temp = tempfile.TemporaryDirectory()
        self.store = LocalBlobStore(self.temp.name)
        self.repo = MaterialRepository(self.pool)
        self.service = MaterialService(self.repo,self.store)
        self.actor = AccessContext(MaterialScope('arti',-100,4,'supergroup'),7,'user:7')
        self.lifecycle = MaterialLifecycle(self.repo,self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()
        await self.db.__aexit__(None,None,None)

    async def ingest(self,source='message-1',actor=None,data=b'budget 1200'):
        return await self.service.ingest(data,'budget.txt',actor or self.actor,source,source)

    async def test_restart_and_duplicate_update_do_not_duplicate_material(self):
        a,b=await asyncio.gather(self.ingest(),self.ingest())
        self.assertEqual(a['id'],b['id'])
        self.service=MaterialService(MaterialRepository(self.pool),self.store)
        _,_,data=await self.service.read_bytes(a['id'],self.actor)
        self.assertEqual(b'budget 1200',data)
        self.assertEqual(1,len(list(self.store.root.glob('*/*.blob'))))

    async def test_equal_bytes_isolate_other_topics_and_private_owners(self):
        a=await self.ingest()
        other=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        b=await self.ingest(actor=other)
        self.assertNotEqual(a['id'],b['id'])
        with self.assertRaises(MaterialError): await self.service.read_bytes(a['id'],other)
        scope=MaterialScope('arti',7,-1,'private')
        private=AccessContext(scope,7,'user:7')
        p=await self.ingest(actor=private)
        with self.assertRaises(MaterialError): await self.service.read_bytes(p['id'],replace(private,user_id=8,sender_ref='user:8'))

    async def test_group_reader_may_read_but_not_erase_other_author(self):
        a=await self.ingest()
        reader=replace(self.actor,user_id=8,sender_ref='user:8')
        _,_,data=await self.service.read_bytes(a['id'],reader)
        self.assertEqual(b'budget 1200',data)
        with self.assertRaises(MaterialError): await self.lifecycle.forget(a['id'],reader)

    async def test_identity_conflict_and_atomic_quota(self):
        a=await self.ingest()
        with self.assertRaises(MaterialError): await self.ingest(data=b'budget 9000')
        self.repo.quotas=Quotas(max_scope_assets=1)
        with self.assertRaises(MaterialError): await self.ingest('message-2')
        self.assertEqual(1,len(list(self.store.root.glob('*/*.blob'))))

    async def test_extraction_roundtrip_cache_and_locator_validation(self):
        a=await self.ingest(data=b'budget\n1200')
        eid,bundle=await self.service.extract(a['id'],self.actor,BasicExtractor())
        self.assertEqual((eid,bundle),await self.service.extract(a['id'],self.actor,BasicExtractor()))
        block=bundle.blocks[1]
        ref=EvidenceRef(a['id'],1,eid,block.block_id,block.locator)
        self.assertEqual('1200',(await self.repo.resolve(ref,self.actor)).text)
        with self.assertRaises(MaterialError): await self.repo.resolve(replace(ref,locator=Locator('paragraph',paragraph=1)),self.actor)

    async def test_forget_blocks_late_extraction_reupload_and_derivatives(self):
        a=await self.ingest()
        _,bundle=await self.service.extract(a['id'],self.actor,BasicExtractor())
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO material_derivatives(id,kind,payload) VALUES('d','preview','{\"value\":1200}')")
            await conn.execute("INSERT INTO material_dependencies VALUES('d',$1)",a['id'])
        await self.lifecycle.forget(a['id'],self.actor)
        with self.assertRaises(MaterialError): await self.repo.save_extraction(self.actor,bundle,0)
        with self.assertRaises(MaterialError): await self.ingest()
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval("SELECT payload FROM material_derivatives WHERE id='d'"))
        report=await self.lifecycle.collect()
        self.assertEqual(1,report['deleted_blobs'])
        self.assertEqual([],list(self.store.root.glob('*/*.blob')))

    async def test_tombstone_before_ingest_and_expiration(self):
        await forget_sources(self.pool,self.actor.scope.identity_key,7,['pending-message'])
        with self.assertRaises(MaterialError): await self.ingest('pending-message')
        a=await self.ingest()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE material_assets SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",a['id'])
        with self.assertRaises(MaterialError): await self.service.read_bytes(a['id'],self.actor)
        self.assertEqual(1,(await self.lifecycle.collect())['expired'])

    async def test_revision_compare_and_swap_rejects_stale_worker(self):
        a=await self.ingest()
        _,old_bundle=await self.service.extract(a['id'],self.actor,BasicExtractor())
        data=b'budget 1500'
        version=await self.service.revise(a['id'],data,'budget.txt',self.actor,1)
        self.assertEqual(2,version)
        with self.assertRaises(MaterialError): await self.repo.save_extraction(self.actor,old_bundle,0)
        with self.assertRaises(MaterialError): await self.service.revise(a['id'],data,'budget.txt',self.actor,1)
        self.assertEqual(b'budget 1500',(await self.service.read_bytes(a['id'],self.actor))[2])

    async def test_deleting_one_asset_keeps_shared_blob_alive(self):
        a,b=await self.ingest(),await self.ingest('message-2')
        await self.lifecycle.forget(a['id'],self.actor)
        self.assertEqual(0,(await self.lifecycle.collect())['deleted_blobs'])
        self.assertEqual(b'budget 1200',(await self.service.read_bytes(b['id'],self.actor))[2])

    async def test_abandoned_upload_cleanup_does_not_delete_live_reservation(self):
        key=await self.repo.reserve_blob(self.actor)
        self.store.put(b'pending',key)
        self.assertEqual(0,(await self.lifecycle.collect())['abandoned_uploads'])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE material_blob_reservations SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",key)
        self.assertEqual(1,(await self.lifecycle.collect())['abandoned_uploads'])
        self.assertFalse(self.store.path(key).exists())

    async def test_late_guard_blocks_delivery_and_cross_topic_after_forget(self):
        from cognition.scope import CURRENT_SCOPE, TransportScope
        from materials.runtime import CURRENT_MATERIAL_USE, MaterialUse, guard_current
        a=await self.ingest()
        use=MaterialUse(a['id'],self.actor,1,0,self.service)
        token=CURRENT_MATERIAL_USE.set((use,))
        scope_token=CURRENT_SCOPE.set(TransportScope(-100,5,'supergroup',7,sender_ref='user:7'))
        try:
            with self.assertRaises(MaterialError): await guard_current(-100)
            CURRENT_SCOPE.set(TransportScope(-100,4,'supergroup',7,sender_ref='user:7'))
            await guard_current(-100)
            await self.lifecycle.forget(a['id'],self.actor)
            with self.assertRaises(MaterialError): await guard_current(-100)
        finally:
            CURRENT_MATERIAL_USE.reset(token)
            CURRENT_SCOPE.reset(scope_token)

    async def test_cognitive_forget_revokes_material_and_native_forget_revokes_cognitive_source(self):
        from cognition.repositories import CognitiveRepository
        from cognition.forgetting import forget_cognitive_sources
        from tests.cognition.test_affect import event
        actor=AccessContext(MaterialScope('arti',10,-1,'private'),1,'user:1')
        cid,eid=await CognitiveRepository(self.pool).observe(event('source-one'))
        a=await self.ingest('source-one',actor=actor)
        await forget_cognitive_sources(self.pool,cid,1,['source-one'])
        with self.assertRaises(MaterialError): await self.service.read_bytes(a['id'],actor)
        cid,eid=await CognitiveRepository(self.pool).observe(event('source-two'))
        b=await self.ingest('source-two',actor=actor)
        await self.lifecycle.forget(b['id'],actor)
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT suppressed_at IS NOT NULL FROM cognitive_events WHERE id=$1',eid))

    async def test_telegram_capture_keeps_actual_uploader_and_rejects_wrong_topic(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from cognition.scope import CURRENT_SCOPE, TransportScope
        from materials.runtime import capture_document
        from tests.materials.fixtures import docx
        data=docx()
        doc=SimpleNamespace(file_id='file-42',file_size=len(data),file_name='budget.docx',mime_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        file=SimpleNamespace(file_size=len(data),download_as_bytearray=AsyncMock(return_value=bytearray(data)))
        context=SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=file)))
        message=SimpleNamespace(chat_id=-100,message_id=42,message_thread_id=4,sender_chat=None,from_user=SimpleNamespace(id=7))
        reader=replace(self.actor,user_id=8,sender_ref='user:8')
        token=CURRENT_SCOPE.set(TransportScope(-100,4,'supergroup',8,sender_ref='user:8'))
        try:
            with patch('materials.runtime.actor_for_current',new=AsyncMock(return_value=reader)),patch('materials.runtime.service_for_bot',new=AsyncMock(return_value=self.service)),patch('cognition.runtime.get_runtime',return_value=None):
                text=await capture_document(context,doc,message)
                use=text.material_uses[0]
                row,_=await self.repo.read(use.asset_id,reader)
                self.assertEqual(7,row['owner_id'])
                self.assertEqual('user:7',row['sender_ref'])
                with self.assertRaises(MaterialError): await capture_document(context,doc,SimpleNamespace(**{**message.__dict__,'message_thread_id':5}))
        finally:
            CURRENT_SCOPE.reset(token)

    async def test_retry_bot_cannot_send_from_revoked_material(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from bot.retry_bot import RetryBot
        from cognition.scope import CURRENT_SCOPE, TransportScope
        from materials.runtime import CURRENT_MATERIAL_USE, MaterialUse
        a=await self.ingest()
        token=CURRENT_MATERIAL_USE.set((MaterialUse(a['id'],self.actor,1,0,self.service),))
        scope_token=CURRENT_SCOPE.set(TransportScope(-100,4,'supergroup',7,sender_ref='user:7'))
        called=AsyncMock()
        async def send_message(**kwargs):
            return await called(**kwargs)
        try:
            await self.lifecycle.forget(a['id'],self.actor)
            with self.assertRaises(MaterialError): await RetryBot._call_with_retry(SimpleNamespace(MAX_ATTEMPTS=3),send_message,chat_id=-100,text='derived answer')
            called.assert_not_awaited()
        finally:
            CURRENT_MATERIAL_USE.reset(token)
            CURRENT_SCOPE.reset(scope_token)

    async def test_cross_store_cleanup_recovers_after_crash_between_barriers(self):
        from unittest.mock import AsyncMock, patch
        from cognition.repositories import CognitiveRepository
        from tests.cognition.test_affect import event
        actor=AccessContext(MaterialScope('arti',10,-1,'private'),1,'user:1')
        cid,eid=await CognitiveRepository(self.pool).observe(event('crash-source'))
        a=await self.ingest('crash-source',actor=actor)
        with patch.object(self.lifecycle,'_forget_cognition',new=AsyncMock(side_effect=RuntimeError('injected_crash'))):
            with self.assertRaises(RuntimeError): await self.lifecycle.forget(a['id'],actor)
        with self.assertRaises(MaterialError): await self.service.read_bytes(a['id'],actor)
        self.assertEqual(1,(await self.lifecycle.collect())['cognitive_cleanups'])
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval('SELECT suppressed_at IS NOT NULL FROM cognitive_events WHERE id=$1',eid))
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_cognitive_cleanup'))
