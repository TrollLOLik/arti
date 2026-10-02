"""Durable request crash/fencing/privacy tests; disposable PostgreSQL only."""
import asyncio
import os
import unittest
from bot.request_store import RequestStore


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class RequestStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database()
        self.pool=await self.db.__aenter__()
        self.store=RequestStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.db.__aexit__(None,None,None)

    async def enqueue(self, key='one', chat=1, topic=-1, kind='text'):
        return await self.store.enqueue(kind,chat,topic,key,{'text':'private '+key})

    async def expire_lease(self, id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1",id)

    async def test_initialize_duplicate_and_restart(self):
        await self.store.initialize()
        a,b=await asyncio.gather(self.enqueue(),self.enqueue())
        self.assertEqual(a['id'],b['id'])
        self.store=RequestStore(self.pool)
        row=await self.store.claim(['text'])
        self.assertEqual(a['id'],row['id'])
        self.assertEqual('private one',row['payload']['text'])
        self.assertTrue(await self.store.finish(row['id'],row['token'],'succeeded'))
        duplicate=await self.enqueue()
        self.assertEqual('succeeded',duplicate['state'])
        self.assertEqual({},duplicate['payload'])
        self.assertIsNone(await self.store.claim(['text']))

    async def test_fifo_within_kind_and_independent_media_lanes(self):
        image=await self.enqueue('image',kind='image')
        first=await self.enqueue('first')
        second=await self.enqueue('second')
        other=await self.enqueue('other',chat=2)
        claims=await asyncio.gather(*(self.store.claim(['text','image']) for _ in range(4)))
        claimed=[r for r in claims if r]
        self.assertEqual({image['id'],first['id'],other['id']},{r['id'] for r in claimed})
        self.assertEqual(3,len(claimed))
        self.assertIsNone(await self.store.claim(['text']))
        row=next(r for r in claimed if r['id']==first['id'])
        await self.store.finish(row['id'],row['token'],'succeeded')
        self.assertEqual(second['id'],(await self.store.claim(['text']))['id'])

    async def test_expired_lease_fences_old_worker_and_preserves_checkpoint(self):
        await self.enqueue()
        old=await self.store.claim(['text'])
        self.assertTrue(await self.store.checkpoint(old['id'],old['token'],'answer',{'text':'ready'}))
        await self.expire_lease(old['id'])
        new=await RequestStore(self.pool).claim(['text'])
        self.assertNotEqual(old['token'],new['token'])
        self.assertEqual({'answer':{'text':'ready'}},new['checkpoints'])
        self.assertEqual(2,new['attempts'])
        self.assertFalse(await self.store.guard(old['id'],old['token']))
        self.assertFalse(await self.store.renew(old['id'],old['token']))
        self.assertFalse(await self.store.checkpoint(old['id'],old['token'],'answer','stale'))
        self.assertFalse(await self.store.finish(old['id'],old['token'],'succeeded'))
        self.assertFalse(await self.store.release(old['id'],old['token']))
        self.assertIsNone(await self.store.prepare_send(old['id'],old['token'],1,{}))
        self.assertTrue(await self.store.guard(new['id'],new['token']))

    async def test_prepared_send_resumes_but_sending_becomes_unknown(self):
        await self.enqueue()
        old=await self.store.claim(['text'])
        send=await self.store.prepare_send(old['id'],old['token'],1,{'text':'reply'})
        self.assertEqual('prepared',send['state'])
        again=await self.store.prepare_send(old['id'],old['token'],1,{'text':'changed'})
        self.assertEqual({'text':'reply'},again['payload'])
        await self.expire_lease(old['id'])
        new=await self.store.claim(['text'])
        self.assertTrue(await self.store.begin_send(new['id'],new['token'],1))
        self.assertFalse(await self.store.begin_send(new['id'],new['token'],1))
        await self.expire_lease(new['id'])
        self.assertIsNone(await self.store.claim(['text']))
        status=await self.store.status(new['id'])
        self.assertEqual('delivery_unknown',status['state'])
        self.assertNotIn('payload',status)
        self.assertFalse(await self.store.finish_send(new['id'],new['token'],1,'delivered',{'message_id':7}))
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_request_sends WHERE request_id=$1',new['id'])
            self.assertEqual('delivery_unknown',row['state'])
            self.assertEqual('{}',row['payload'])

    async def test_delivered_send_is_not_repeated_after_generation_crash(self):
        await self.enqueue()
        old=await self.store.claim(['text'])
        await self.store.prepare_send(old['id'],old['token'],1,{'text':'reply'})
        await self.store.begin_send(old['id'],old['token'],1)
        self.assertTrue(await self.store.finish_send(old['id'],old['token'],1,'delivered',{'message_id':7}))
        await self.expire_lease(old['id'])
        new=await self.store.claim(['text'])
        send=await self.store.prepare_send(new['id'],new['token'],1,{'text':'reply'})
        self.assertEqual('delivered',send['state'])
        self.assertEqual({'message_id':7},send['receipt'])
        self.assertFalse(await self.store.begin_send(new['id'],new['token'],1))

    async def test_cancel_scrubs_active_payloads_and_does_not_touch_other_topic(self):
        a=await self.enqueue()
        b=await self.enqueue('next')
        c=await self.enqueue('other',topic=5)
        row=await self.store.claim(['text'])
        await self.store.checkpoint(row['id'],row['token'],'private','answer')
        await self.store.prepare_send(row['id'],row['token'],1,{'text':'private'})
        async with self.pool.acquire() as conn,conn.transaction():
            self.assertEqual(2,await self.store.cancel_chat(1,-1,conn=conn))
        self.assertFalse(await self.store.guard(row['id'],row['token']))
        self.assertFalse(await self.store.begin_send(row['id'],row['token'],1))
        duplicate=await self.enqueue()
        self.assertEqual('cancelled',duplicate['state'])
        self.assertEqual({},duplicate['payload'])
        self.assertEqual({},duplicate['checkpoints'])
        self.assertEqual(c['id'],(await self.store.claim(['text']))['id'])
        async with self.pool.acquire() as conn:
            self.assertEqual('{}',await conn.fetchval('SELECT payload FROM arti_request_sends WHERE request_id=$1',a['id']))

    async def test_deadline_terminalizes_and_unblocks_fifo(self):
        a=await self.enqueue()
        b=await self.enqueue('next')
        row=await self.store.claim(['text'])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET deadline_at=NOW()-INTERVAL '1 second' WHERE id=$1",a['id'])
        self.assertFalse(await self.store.guard(row['id'],row['token']))
        self.assertFalse(await self.store.renew(row['id'],row['token']))
        self.assertEqual(b['id'],(await self.store.claim(['text']))['id'])
        self.assertEqual('expired',(await self.store.status(a['id']))['state'])

    async def test_shutdown_and_cancel_do_not_requeue_ambiguous_send(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        self.assertTrue(await self.store.release(row['id'],row['token']))
        row=await self.store.claim(['text'])
        await self.store.prepare_send(row['id'],row['token'],1,{'text':'reply'})
        await self.store.begin_send(row['id'],row['token'],1)
        self.assertTrue(await self.store.release(row['id'],row['token']))
        self.assertEqual('delivery_unknown',(await self.store.status(row['id']))['state'])
        self.assertIsNone(await self.store.claim(['text']))

    async def test_cancel_during_send_preserves_unknown(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        await self.store.prepare_send(row['id'],row['token'],1,{'text':'reply'})
        await self.store.begin_send(row['id'],row['token'],1)
        await self.store.cancel_chat(1)
        self.assertEqual('delivery_unknown',(await self.store.status(row['id']))['state'])
        self.assertFalse(await self.store.finish_send(row['id'],row['token'],1,'delivered'))

    async def test_forget_scrubs_completed_receipts_without_resurrection(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        await self.store.prepare_send(row['id'],row['token'],1,{'text':'reply'})
        await self.store.begin_send(row['id'],row['token'],1)
        await self.store.finish_send(row['id'],row['token'],1,'delivered',{'message_id':7})
        self.assertTrue(await self.store.finish(row['id'],row['token'],'completed'))
        self.assertEqual(1,await self.store.erase_chat(1))
        self.assertEqual('completed',(await self.enqueue())['state'])
        async with self.pool.acquire() as conn:
            self.assertIsNone(await conn.fetchval('SELECT receipt FROM arti_request_sends WHERE request_id=$1',row['id']))
        self.assertIsNone(await self.store.claim(['text']))

    async def test_checkpoint_merge_renew_and_error_privacy(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        self.assertTrue(await self.store.renew(row['id'],row['token']))
        await self.store.checkpoint(row['id'],row['token'],'one',{'a':1})
        await self.store.checkpoint(row['id'],row['token'],'two',[2])
        await self.store.release(row['id'],row['token'])
        row=await self.store.claim(['text'])
        self.assertEqual({'one':{'a':1},'two':[2]},row['checkpoints'])
        with self.assertRaises(ValueError):
            await self.store.finish(row['id'],row['token'],'failed','provider error containing private text')
        self.assertTrue(await self.store.finish(row['id'],row['token'],'failed','provider_unavailable'))

    async def test_unknown_send_blocks_any_fallback_send(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        await self.store.prepare_send(row['id'],row['token'],1,{'text':'first'})
        await self.store.prepare_send(row['id'],row['token'],3,{'text':'preprepared'})
        await self.store.begin_send(row['id'],row['token'],1)
        self.assertFalse(await self.store.begin_send(row['id'],row['token'],3))
        self.assertIsNone(await self.store.prepare_send(row['id'],row['token'],2,{'text':'fallback'}))
        await self.store.finish_send(row['id'],row['token'],1,'delivery_unknown')
        self.assertIsNone(await self.store.prepare_send(row['id'],row['token'],2,{'text':'fallback'}))

    def envelope(self, text='hello', owner=1, message=1, sources=(), context=None):
        return {'codec':1,'kind':'request','fence':{'context_id':context,'epoch':0} if context else None,
            'value':{'codec':1,'kind':'dict','items':{'type':'text','chat_id':1,'user_id':owner,
                'message_id':message,'user_message':text,'_cognitive_source_ids':{'codec':1,'kind':'list','items':list(sources)}}}}

    async def ready(self):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET available_at=NOW()-INTERVAL '1 second'")

    async def test_cosmetic_notice_unknown_does_not_block_answer(self):
        await self.enqueue()
        row=await self.store.claim(['text'])
        await self.store.prepare_send(row['id'],row['token'],0,{'text':'thinking'})
        await self.store.begin_send(row['id'],row['token'],0)
        await self.expire_lease(row['id'])
        recovered=await self.store.claim(['text'])
        self.assertEqual(row['id'],recovered['id'])
        self.assertIsNotNone(await self.store.prepare_send(recovered['id'],recovered['token'],1,{'text':'answer'}))
        self.assertTrue(await self.store.begin_send(recovered['id'],recovered['token'],1))
        await self.store.finish_send(recovered['id'],recovered['token'],1,'delivered')
        await self.store.finish(recovered['id'],recovered['token'],'completed')
        self.assertEqual('completed',(await self.store.status(row['id']))['state'])

    async def test_debounce_coalesces_and_retains_child_dedupe(self):
        first=await self.store.enqueue('text',1,-1,'A',self.envelope('A'))
        self.assertIsNone(await self.store.claim(['text']))
        second=await self.store.enqueue('text',1,-1,'B',self.envelope('B',message=2))
        self.assertEqual(first['id'],second['id'])
        self.assertEqual('A\nB',second['payload']['value']['items']['user_message'])
        again=await self.store.enqueue('text',1,-1,'B',self.envelope('B',message=2))
        self.assertEqual('A\nB',again['payload']['value']['items']['user_message'])
        await self.ready()
        row=await self.store.claim(['text'])
        self.assertEqual(first['id'],row['id'])
        await self.store.finish(row['id'],row['token'],'completed')
        self.assertEqual('completed',(await self.store.enqueue('text',1,-1,'B',self.envelope('B',message=2)))['state'])
        self.assertIsNone(await self.store.claim(['text']))

    async def test_debounce_never_crosses_another_author(self):
        a=await self.store.enqueue('text',1,-1,'A',self.envelope('A',owner=1))
        b=await self.store.enqueue('text',1,-1,'B',self.envelope('B',owner=2,message=2))
        c=await self.store.enqueue('text',1,-1,'C',self.envelope('C',owner=1,message=3))
        self.assertEqual(3,len({a['id'],b['id'],c['id']}))
        await self.ready()
        for expected in (a,b,c):
            row=await self.store.claim(['text'])
            self.assertEqual(expected['id'],row['id'])
            await self.store.finish(row['id'],row['token'],'completed')

    async def test_scoped_source_erasure_and_late_registration_rejected(self):
        from cognition.repositories import CognitiveRepository,ensure_schema
        from tests.cognition.test_affect import event
        await ensure_schema(self.pool)
        cid,eid=await CognitiveRepository(self.pool).observe(event())
        _,other=await CognitiveRepository(self.pool).observe(event('other'))
        a=await self.store.enqueue('text',1,-1,'A',self.envelope('private',sources=[eid],context=cid))
        b=await self.store.enqueue('text',1,-1,'B',self.envelope('other',owner=2,sources=[other],context=cid))
        await self.ready()
        row=await self.store.claim(['text'])
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('UPDATE cognitive_events SET suppressed_at=NOW() WHERE id=$1',eid)
            self.assertEqual(1,await self.store.erase_sources([eid],conn))
        self.assertEqual('cancelled',(await self.store.status(a['id']))['state'])
        self.assertEqual('queued',(await self.store.status(b['id']))['state'])
        with self.assertRaisesRegex(ValueError,'suppressed_request_source'):
            await self.store.enqueue('text',1,-1,'late',self.envelope('private',sources=[eid],context=cid))
        row=await self.store.claim(['text'])
        self.assertFalse(await self.store.checkpoint(row['id'],row['token'],'stale',self.envelope('private',sources=[eid],context=cid)))
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1',cid)
        self.assertFalse(await self.store.checkpoint(row['id'],row['token'],'rebuild',self.envelope('memory',context=cid)))

    async def test_concurrent_source_erasure_blocks_new_checkpoint_dependency(self):
        from cognition.repositories import CognitiveRepository,ensure_schema
        from tests.cognition.test_affect import event
        await ensure_schema(self.pool)
        cid,eid=await CognitiveRepository(self.pool).observe(event())
        await self.enqueue()
        row=await self.store.claim(['text'])
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1',cid)
                pending=asyncio.create_task(self.store.checkpoint(row['id'],row['token'],'memory',self.envelope('secret',sources=[eid],context=cid)))
                await asyncio.sleep(.05)
                self.assertFalse(pending.done())
                await conn.execute('UPDATE cognitive_events SET suppressed_at=NOW() WHERE id=$1',eid)
                await self.store.erase_sources([eid],conn)
            self.assertFalse(await pending)
        async with self.pool.acquire() as conn:
            self.assertEqual('{}',await conn.fetchval('SELECT checkpoints FROM arti_requests WHERE id=$1',row['id']))

    async def test_material_erasure_is_scoped_and_rejects_late_checkpoint(self):
        import tempfile
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from materials.types import AccessContext,MaterialScope
        from materials.lifecycle import erase_locked
        await ensure_schema(self.pool)
        with tempfile.TemporaryDirectory() as directory:
            service=MaterialService(MaterialRepository(self.pool),LocalBlobStore(directory))
            actor=AccessContext(MaterialScope('arti',1,-1,'private'),1,'user:1')
            asset=await service.ingest(b'private document','document.txt',actor,'test','test')
            encoded={'codec':1,'kind':'MaterialUse','value':{'codec':1,'kind':'dict','items':{'asset_id':asset['id']}}}
            a=await self.store.enqueue('text',1,-1,'material',encoded)
            b=await self.enqueue('unrelated')
            async with self.pool.acquire() as conn,conn.transaction():
                await erase_locked(conn,[asset['id']])
            self.assertEqual('cancelled',(await self.store.status(a['id']))['state'])
            self.assertEqual('queued',(await self.store.status(b['id']))['state'])
            row=await self.store.claim(['text'])
            self.assertFalse(await self.store.checkpoint(row['id'],row['token'],'private',encoded))

    async def test_debounce_never_extends_two_second_budget(self):
        a=await self.store.enqueue('text',1,-1,'A',self.envelope('A'))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET created_at=NOW()-INTERVAL '1.8 seconds' WHERE id=$1",a['id'])
        b=await self.store.enqueue('text',1,-1,'B',self.envelope('B',message=2))
        self.assertEqual(a['id'],b['id'])
        self.assertLessEqual((b['available_at']-b['created_at']).total_seconds(),2)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET created_at=NOW()-INTERVAL '2.1 seconds' WHERE id=$1",a['id'])
        c=await self.store.enqueue('text',1,-1,'C',self.envelope('C',message=3))
        self.assertNotEqual(a['id'],c['id'])
