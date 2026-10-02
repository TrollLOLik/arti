"""Opaque spool resource ownership and cleanup fencing, disposable SQL only."""
import asyncio
import os
import unittest
import uuid
from bot.request_store import RequestStore


def namespace(): return uuid.uuid4().hex


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class RequestResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); self.store=RequestStore(self.pool)
    async def asyncTearDown(self): await self.db.__aexit__(None,None,None)
    async def enqueue(self,key='a',chat=1,topic=-1,resources=(),kind='dubbing'):
        return await self.store.enqueue(kind,chat,topic,key,{},resources=resources)
    async def claim(self): return await self.store.claim(['dubbing','vclone'])
    async def resource(self,ns):
        async with self.pool.acquire() as conn: return await conn.fetchrow('SELECT * FROM arti_request_resources WHERE namespace=$1',ns)

    async def test_input_adoption_is_atomic_and_duplicate_does_not_adopt_copy(self):
        original,unused=namespace(),namespace()
        first=await self.enqueue(resources=[original]); duplicate=await self.enqueue(resources=[unused])
        self.assertEqual(first['id'],duplicate['id'])
        self.assertEqual(await self.store.retained_resource_namespaces(),{original})
        self.assertEqual(await self.store.resource_request(original),first['id'])
        self.assertIsNone(await self.store.resource_request(unused))
        self.assertEqual(await self.store.adopted_resources(first['id']),{original})
        self.assertIsNone(await self.resource(unused)); self.assertIsNone(await self.store.claim_resource_cleanup(grace_seconds=0))

    async def test_namespace_cannot_move_between_requests_or_owners(self):
        ns=namespace(); first=await self.enqueue(resources=[ns])
        with self.assertRaisesRegex(ValueError,'resource_already_owned'):
            await self.enqueue('other',chat=2,resources=[ns])
        async with self.pool.acquire() as conn: self.assertEqual(await conn.fetchval('SELECT count(*) FROM arti_requests'),1)
        second=await self.enqueue('other',chat=2)
        a=await self.claim(); b=await self.claim()
        self.assertTrue(await self.store.resources_owned(a['id'],a['token'],[ns]))
        self.assertFalse(await self.store.resources_owned(b['id'],b['token'],[ns]))
        with self.assertRaisesRegex(ValueError,'request_resource_unavailable'):
            await self.store.assert_resources(b['id'],b['token'],[ns])

    async def test_attempt_binding_checkpoint_and_recovery_keep_ownership(self):
        original,attempt=namespace(),namespace()
        await self.enqueue(resources=[original]); job=await self.claim()
        self.assertFalse(await self.store.bind_resources(job['id'],'stale',[attempt]))
        self.assertIsNone(await self.resource(attempt))
        self.assertTrue(await self.store.bind_resources(job['id'],job['token'],[attempt]))
        self.assertTrue(await self.store.checkpoint(job['id'],job['token'],'media',{},resources=[attempt]))
        await self.store.release(job['id'],job['token'])
        self.assertIsNone(await self.store.claim_resource_cleanup(grace_seconds=0))
        resumed=await self.claim()
        self.assertFalse(await self.store.resources_owned(job['id'],job['token'],[original,attempt]))
        self.assertTrue(await self.store.resources_owned(resumed['id'],resumed['token'],[original,attempt]))

    async def test_terminal_cleanup_is_leased_and_namespace_is_never_reused(self):
        ns=namespace(); await self.enqueue(resources=[ns]); job=await self.claim()
        await self.store.finish(job['id'],job['token'],'completed')
        self.assertEqual((await self.resource(ns))['state'],'cleanup_pending')
        self.assertFalse(await self.store.bind_resources(job['id'],job['token'],[namespace()]))
        first=await self.store.claim_resource_cleanup(grace_seconds=0); self.assertIsNone(await self.store.claim_resource_cleanup(grace_seconds=0))
        self.assertFalse(await self.store.finish_resource_cleanup(ns,'wrong'))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_request_resources SET lease_until=NOW()-INTERVAL '1 second' WHERE namespace=$1",ns)
        second=await self.store.claim_resource_cleanup(grace_seconds=0); self.assertNotEqual(first['token'],second['token'])
        self.assertFalse(await self.store.finish_resource_cleanup(ns,first['token']))
        self.assertTrue(await self.store.finish_resource_cleanup(ns,second['token']))
        self.assertEqual(await self.store.retained_resource_namespaces(),set())
        self.assertEqual(await self.store.resource_request(ns),job['id'])
        with self.assertRaisesRegex(ValueError,'resource_already_owned'): await self.enqueue('reuse',resources=[ns])

    async def test_cleanup_failure_is_retryable_without_reviving_request(self):
        ns=namespace(); await self.enqueue(resources=[ns]); await self.store.cancel_chat(1)
        claim=await self.store.claim_resource_cleanup(grace_seconds=0)
        self.assertTrue(await self.store.finish_resource_cleanup(ns,claim['token'],success=False))
        self.assertEqual((await self.resource(ns))['state'],'cleanup_pending')
        retry=await self.store.claim_resource_cleanup(grace_seconds=0); self.assertNotEqual(retry['token'],claim['token'])
        self.assertEqual((await self.store.status(retry['request_id']))['state'],'cancelled')

    async def test_cancel_is_topic_scoped_and_forget_scrubs_terminal_resources(self):
        a,b=namespace(),namespace()
        first=await self.enqueue('one',topic=1,resources=[a]); second=await self.enqueue('two',topic=2,resources=[b])
        await self.store.cancel_chat(1,1)
        self.assertEqual((await self.resource(a))['state'],'cleanup_pending')
        self.assertEqual((await self.resource(b))['state'],'bound')
        await self.store.erase_chat(1,2)
        self.assertEqual((await self.resource(b))['state'],'cleanup_pending')
        for row in (first,second):
            async with self.pool.acquire() as conn:
                self.assertEqual(await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',row['id']),'{}')

    async def test_source_and_material_erasure_queue_only_owned_copies(self):
        a,b,c=namespace(),namespace(),namespace()
        first=await self.enqueue('one',resources=[a]); second=await self.enqueue('two',chat=2,resources=[b]); await self.enqueue('three',chat=3,resources=[c])
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE arti_requests SET source_event_ids=ARRAY[17::bigint] WHERE id=$1',first['id'])
            await conn.execute("UPDATE arti_requests SET material_ids=ARRAY['asset'] WHERE id=$1",second['id'])
        await self.store.erase_sources([17]); await self.store.erase_materials(['asset'])
        self.assertEqual((await self.resource(a))['state'],'cleanup_pending')
        self.assertEqual((await self.resource(b))['state'],'cleanup_pending')
        self.assertEqual((await self.resource(c))['state'],'bound')

    async def test_expiry_and_ambiguous_send_both_release_disk_copies(self):
        a,b=namespace(),namespace()
        expired=await self.enqueue('expiry',resources=[a])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET deadline_at=NOW()-INTERVAL '1 second' WHERE id=$1",expired['id'])
        self.assertIsNone(await self.claim()); self.assertEqual((await self.resource(a))['state'],'cleanup_pending')
        await self.enqueue('unknown',resources=[b]); job=await self.claim()
        await self.store.prepare_send(job['id'],job['token'],1,{})
        await self.store.begin_send(job['id'],job['token'],1)
        await self.store.release(job['id'],job['token'])
        self.assertEqual((await self.store.status(job['id']))['state'],'delivery_unknown')
        self.assertEqual((await self.resource(b))['state'],'cleanup_pending')

    async def test_binding_racing_cancel_never_leaves_bound_terminal_resource(self):
        await self.enqueue(); job=await self.claim(); ns=namespace()
        await asyncio.gather(self.store.bind_resources(job['id'],job['token'],[ns]),self.store.cancel_chat(1))
        row=await self.resource(ns)
        self.assertTrue(row is None or row['state']=='cleanup_pending')
        self.assertFalse(await self.store.resources_owned(job['id'],job['token'],[ns]))

    async def test_paths_and_excessive_namespaces_rejected(self):
        for values in (['../private'],['A'*32],['a/b'],['a'*31],['a'*33],'a'*32,[namespace() for _ in range(65)]):
            with self.assertRaises(ValueError): await self.enqueue(resources=values)

    async def test_cancel_reclaims_only_registered_spool_copy_not_original(self):
        from pathlib import Path
        import tempfile
        from bot.media_spool import MediaSpool
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); original=root/'synthetic-original.wav'
            original.write_bytes(b'RIFF synthetic input')
            spool=MediaSpool(root/'spool')
            target=spool.create_namespace(); other=spool.create_namespace()
            descriptor=spool.stage(original,namespace=target)
            spool.stage(original,namespace=other)
            await self.enqueue('cancel',resources=[target]); await self.enqueue('retain',chat=2,resources=[other])
            await self.store.cancel_chat(1)
            cleanup=await self.store.claim_resource_cleanup(grace_seconds=0)
            self.assertEqual(cleanup['namespace'],target)
            self.assertTrue(spool.cleanup(cleanup['namespace']))
            self.assertTrue(await self.store.finish_resource_cleanup(target,cleanup['token']))
            self.assertFalse((spool.root/target).exists())
            self.assertTrue((spool.root/other).exists()); self.assertEqual(original.read_bytes(),b'RIFF synthetic input')
            self.assertEqual(await self.store.retained_resource_namespaces(),{other})

    async def test_checkpoint_resource_conflict_rolls_back_payload_and_partial_binds(self):
        existing,fresh='f'*32,'0'*32
        await self.enqueue('one',resources=[existing]); await self.enqueue('two',chat=2)
        first=await self.claim(); second=await self.claim()
        with self.assertRaisesRegex(ValueError,'resource_already_owned'):
            await self.store.checkpoint(second['id'],second['token'],'output',{'private':'never committed'},resources=[fresh,existing])
        self.assertIsNone(await self.resource(fresh))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT checkpoints::text FROM arti_requests WHERE id=$1',second['id']),'{}')
        self.assertEqual((await self.resource(existing))['request_id'],first['id'])

    async def test_pause_resume_requires_owner_and_new_control_and_preserves_deadline(self):
        from bot.request_codec import encode_request
        ns=namespace()
        payload=await encode_request(dict(type='vclone',chat_id=1,user_id=1,message_id=1))
        row=await self.store.enqueue('vclone',1,-1,'pause',payload,budget_seconds=604800,resources=[ns])
        job=await self.claim()
        await self.store.checkpoint(job['id'],job['token'],'disk_generation_started',True)
        await self.store.checkpoint(job['id'],job['token'],'unrelated',{'keep':1})
        self.assertTrue(await self.store.pause(job['id'],job['token']))
        self.assertIsNone(await self.claim()); self.assertIsNone(await self.store.claim_resource_cleanup(grace_seconds=0))
        self.assertFalse(await self.store.resume(job['id'],1,-1,2,'wrong-owner'))
        self.assertFalse(await self.store.resume(job['id'],1,5,1,'wrong-topic'))
        self.assertTrue(await self.store.resume(job['id'],1,-1,1,'control-1'))
        resumed=await self.claim()
        self.assertEqual(resumed['deadline_at'],row['deadline_at'])
        self.assertNotIn('disk_generation_started',resumed['checkpoints']); self.assertIn('unrelated',resumed['checkpoints'])
        self.assertTrue(await self.store.pause(resumed['id'],resumed['token']))
        self.assertFalse(await self.store.resume(job['id'],1,-1,1,'control-1'))
        self.assertTrue(await self.store.resume(job['id'],1,-1,1,'control-2'))

    async def test_paused_expiry_and_cancel_release_resources_with_production_grace(self):
        for key in ('expiry','cancel'):
            ns=namespace(); row=await self.enqueue(key,resources=[ns]); job=await self.claim()
            await self.store.pause(job['id'],job['token'])
            if key=='expiry':
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE arti_requests SET deadline_at=NOW()-INTERVAL '1 second' WHERE id=$1",row['id'])
                self.assertIsNone(await self.claim())
            else: await self.store.cancel_chat(1)
            self.assertIsNone(await self.store.claim_resource_cleanup())
            cleanup=await self.store.claim_resource_cleanup(grace_seconds=0)
            self.assertEqual(cleanup['namespace'],ns)
            await self.store.finish_resource_cleanup(ns,cleanup['token'])

    async def test_interrupted_delivery_is_not_resumable_generation(self):
        await self.enqueue(); job=await self.claim()
        await self.store.prepare_send(job['id'],job['token'],1,{})
        await self.store.begin_send(job['id'],job['token'],1)
        self.assertFalse(await self.store.pause(job['id'],job['token']))
        self.assertEqual((await self.store.status(job['id']))['state'],'delivery_unknown')
        self.assertFalse(await self.store.resume(job['id'],1,-1,1,'retry'))
