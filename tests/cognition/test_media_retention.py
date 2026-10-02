"""Bounded retained media: synthetic disk fixtures and disposable SQL only."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from bot.media_spool import MediaSpool
from bot.media_retention import Retention,RetentionUnavailable,initialize,retain_copy,expire
from bot.request_store import RequestStore


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class MediaRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.store=RequestStore(self.pool); await self.store.initialize()
        async with self.pool.acquire() as conn: await initialize(conn)
        self.retained=Retention(self.pool); self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.disk=MediaSpool(self.root/'spool',max_file_bytes=1024,max_total_bytes=65536)
        self.source=self.root/'source.wav'; self.source.write_bytes(b'synthetic-reference')
        self.descriptor=self.disk.stage(self.source)
        payload={'codec':1,'kind':'request','fence':None,'value':{'codec':1,'kind':'dict','items':{'type':'dubbing','user_id':1,'chat_id':1,'message_id':1}}}
        await self.store.enqueue('dubbing',1,-1,'fixture',payload,resources=[self.descriptor['namespace']])
        self.job=await self.store.claim(['dubbing'])
    async def asyncTearDown(self):
        self.tmp.cleanup(); await self.db.__aexit__(None,None,None)
    async def retain(self,purpose='result'):
        return await retain_copy(self.pool,self.job['id'],self.job['token'],purpose,self.descriptor,1,spool=self.disk)
    async def load(self,purpose='result',owner=1,chat=1,topic=-1):
        return await self.retained.load(self.job['id'],purpose,owner,chat,topic)

    async def test_completed_result_survives_restart_in_dedicated_namespace(self):
        (self.disk.workdir(self.descriptor['namespace'])/'intermediate.tmp').write_bytes(b'unneeded')
        retained=await self.retain()
        self.assertNotEqual(self.descriptor['namespace'],retained['namespace'])
        self.assertEqual({'.owner.json','.use.lock',retained['descriptor']['leaf']},{p.name for p in self.disk.workdir(retained['namespace']).iterdir()})
        await self.store.finish(self.job['id'],self.job['token'],'completed')
        loaded=await Retention(self.pool).load(self.job['id'],'result',1,1,-1)
        self.assertIsNotNone(loaded)
        with self.disk.open_verified(loaded['descriptor']) as stream: self.assertEqual(self.source.read_bytes(),stream.read())
        async with self.pool.acquire() as conn:
            resources={r['namespace']:r['state'] for r in await conn.fetch('SELECT namespace,state FROM arti_request_resources WHERE request_id=$1',self.job['id'])}
        self.assertEqual('bound',resources[retained['namespace']]); self.assertEqual('cleanup_pending',resources[self.descriptor['namespace']])

    async def test_exact_owner_chat_topic_and_live_request_guards(self):
        await self.retain()
        for scope in ((2,1,-1),(1,2,-1),(1,1,7)):
            self.assertIsNone(await self.load(owner=scope[0],chat=scope[1],topic=scope[2]))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1",self.job['id'])
        self.assertIsNone(await self.load())

    async def test_fixed_ttl_idempotency_and_expiry_cleanup(self):
        first=await self.retain('voice_reference'); second=await self.retain('voice_reference')
        self.assertEqual(first['namespace'],second['namespace']); self.assertEqual(first['expires_at'],second['expires_at'])
        self.assertAlmostEqual(900,(first['expires_at']-first['created_at']).total_seconds(),places=3)
        await self.store.finish(self.job['id'],self.job['token'],'completed')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_media_retained SET expires_at=NOW()-INTERVAL '1 second' WHERE request_id=$1",self.job['id'])
        self.assertIsNone(await self.load('voice_reference'))
        self.assertEqual(1,await expire(self.pool))
        async with self.pool.acquire() as conn:
            self.assertEqual('{}',await conn.fetchval('SELECT descriptor FROM arti_media_retained WHERE request_id=$1',self.job['id']))
            self.assertEqual('cleanup_pending',await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',first['namespace']))

    async def test_cancellation_overrides_retention(self):
        retained=await self.retain()
        await self.store.cancel_chat(1,-1)
        self.assertIsNone(await self.load())
        async with self.pool.acquire() as conn:
            self.assertIsNotNone(await conn.fetchval('SELECT invalidated_at FROM arti_media_retained WHERE request_id=$1',self.job['id']))
            self.assertEqual('cleanup_pending',await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',retained['namespace']))

    async def test_forget_completed_request_overrides_retention(self):
        await self.retain(); await self.store.finish(self.job['id'],self.job['token'],'completed')
        await self.store.erase_chat(1,-1)
        self.assertIsNone(await self.load())
        async with self.pool.acquire() as conn:
            self.assertEqual('{}',await conn.fetchval('SELECT descriptor FROM arti_media_retained WHERE request_id=$1',self.job['id']))

    async def test_consumption_release_is_scoped_and_frees_only_retained_file(self):
        result=await self.retain('result'); voice=await self.retain('voice_reference')
        await self.store.finish(self.job['id'],self.job['token'],'completed')
        self.assertFalse(await self.retained.release(self.job['id'],'voice_reference',2,1,-1))
        self.assertTrue(await self.retained.release(self.job['id'],'voice_reference',1,1,-1))
        self.assertIsNone(await self.load('voice_reference')); self.assertIsNotNone(await self.load('result'))

    async def test_source_suppression_blocks_retained_read(self):
        from cognition.repositories import CognitiveRepository,ensure_schema
        from tests.cognition.test_affect import event
        await ensure_schema(self.pool); cid,eid=await CognitiveRepository(self.pool).observe(event())
        encoded={'codec':1,'kind':'PreparedTurn','value':{'codec':1,'kind':'dict','items':{'context_id':cid,'event_id':eid}}}
        await self.store.checkpoint(self.job['id'],self.job['token'],'turn',encoded)
        await self.retain(); await self.store.finish(self.job['id'],self.job['token'],'completed')
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET suppressed_at=NOW() WHERE id=$1',eid)
        self.assertIsNone(await self.load())
        await self.store.erase_sources([eid])
        async with self.pool.acquire() as conn:
            self.assertEqual('{}',await conn.fetchval('SELECT descriptor FROM arti_media_retained WHERE request_id=$1',self.job['id']))

    async def test_wrong_owner_cannot_create_retention(self):
        with self.assertRaises(RetentionUnavailable):
            await retain_copy(self.pool,self.job['id'],self.job['token'],'result',self.descriptor,2,spool=self.disk)
        self.assertIsNone(await self.load())

    async def test_cancel_during_copy_cannot_publish_retained_entry(self):
        entered=threading.Event(); release=threading.Event(); original=self.disk.copy_to
        def delayed(*args):
            entered.set(); release.wait(5); return original(*args)
        with patch.object(self.disk,'copy_to',side_effect=delayed):
            pending=asyncio.create_task(self.retain())
            await asyncio.to_thread(entered.wait,5)
            await self.store.cancel_chat(1,-1); release.set()
            with self.assertRaises(RetentionUnavailable): await pending
        self.assertIsNone(await self.load())

    async def test_release_shared_file_does_not_break_other_live_purpose(self):
        result=await self.retain()
        await self.retained.retain(self.job['id'],self.job['token'],'voice_reference',result['descriptor'],1)
        await self.store.finish(self.job['id'],self.job['token'],'completed')
        await self.retained.release(self.job['id'],'voice_reference',1,1,-1)
        self.assertIsNotNone(await self.load('result'))
        async with self.pool.acquire() as conn:
            self.assertEqual('bound',await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',result['namespace']))

    async def expire_before_first_send(self,purpose='result',sweep=True):
        from bot.request_codec import encode_value
        await self.store.checkpoint(self.job['id'],self.job['token'],'disk_media_result',await encode_value({'media':self.descriptor,'channel':'audio'}))
        if purpose=='voice_reference':
            async with self.pool.acquire() as conn:
                import json
                payload=json.loads(await conn.fetchval('SELECT payload FROM arti_requests WHERE id=$1',self.job['id']))
                payload['value']['items']['reference_media']=await encode_value(self.descriptor)
                await conn.execute('UPDATE arti_requests SET payload=$2::jsonb WHERE id=$1',self.job['id'],json.dumps(payload))
        first=await self.retain(purpose)
        # Advance persisted timestamps by two days, leaving the seven-day job
        # and its main checkpoint live under a renewed worker lease.
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET created_at=NOW()-INTERVAL '2 days',deadline_at=NOW()+INTERVAL '5 days' WHERE id=$1",self.job['id'])
            await conn.execute("UPDATE arti_media_retained SET created_at=created_at-INTERVAL '2 days',expires_at=expires_at-INTERVAL '2 days' WHERE request_id=$1",self.job['id'])
        if sweep: await expire(self.pool)
        return first

    async def test_restart_after_two_days_rebuilds_unsent_expired_result(self):
        first=await self.expire_before_first_send()
        self.disk.cleanup(first['namespace'])
        await self.store.prepare_send(self.job['id'],self.job['token'],0,{'method':'send_message'})
        await self.store.begin_send(self.job['id'],self.job['token'],0)
        await self.store.finish_send(self.job['id'],self.job['token'],0,'delivery_unknown')
        renewed=await self.retain()
        self.assertNotEqual(first['namespace'],renewed['namespace'])
        self.assertIsNotNone(await self.load())
        self.assertAlmostEqual(86400,(renewed['expires_at']-renewed['created_at']).total_seconds(),places=3)
        replay=await self.retain()
        self.assertEqual(renewed['expires_at'],replay['expires_at'])
        self.assertEqual(renewed['namespace'],replay['namespace'])

    async def test_unswept_expired_voice_offer_can_rebuild_from_live_input(self):
        first=await self.expire_before_first_send('voice_reference',sweep=False)
        renewed=await self.retain('voice_reference')
        self.assertNotEqual(first['namespace'],renewed['namespace'])
        self.assertAlmostEqual(900,(renewed['expires_at']-renewed['created_at']).total_seconds(),places=3)

    async def test_expired_retention_never_extends_after_substantive_send_intent(self):
        await self.expire_before_first_send()
        await self.store.prepare_send(self.job['id'],self.job['token'],1,{'method':'send_audio'})
        for state in ('sending','delivered','delivery_unknown'):
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE arti_request_sends SET state=$2 WHERE request_id=$1',self.job['id'],state)
            with self.assertRaises(RetentionUnavailable): await self.retain()
        self.assertIsNone(await self.load())

    async def test_completed_retention_never_renews(self):
        await self.expire_before_first_send()
        await self.store.finish(self.job['id'],self.job['token'],'completed')
        with self.assertRaises(RetentionUnavailable): await self.retain()

    async def test_released_or_missing_checkpoint_cannot_renew(self):
        await self.expire_before_first_send()
        await self.retained.release(self.job['id'],'result',1,1,-1)
        with self.assertRaises(RetentionUnavailable): await self.retain()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_media_retained SET invalidation_reason='expired' WHERE request_id=$1",self.job['id'])
            await conn.execute("UPDATE arti_requests SET checkpoints='{}' WHERE id=$1",self.job['id'])
        with self.assertRaises(RetentionUnavailable): await self.retain()


    async def test_expired_result_renews_with_prepared_never_begun_notice(self):
        first=await self.expire_before_first_send()
        prepared=await self.store.prepare_send(self.job['id'],self.job['token'],1,{'method':'send_message','text':'See request for current expiry'})
        self.assertEqual('prepared',prepared['state'])
        renewed=await self.retain()
        self.assertNotEqual(first['namespace'],renewed['namespace'])
        self.assertIsNotNone(await self.load())
        self.assertTrue(await self.store.begin_send(self.job['id'],self.job['token'],1))
        await self.store.finish_send(self.job['id'],self.job['token'],1,'delivered',{'message_id':123})
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_media_retained SET expires_at=NOW()-INTERVAL '1 second' WHERE request_id=$1",self.job['id'])
        with self.assertRaises(RetentionUnavailable): await self.retain()
