"""True accepted-command capture, owner-specific forgetting, and reset fences."""
import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from bot.media_provenance import capture,reference_source
from bot.request_codec import encode_request,decode_request
from bot.request_store import RequestStore
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.scope import TransportScope,CURRENT_SCOPE
from cognition.repositories import SuppressedEvidence
from cognition.forgetting import forget_cognitive_sources
from tests.cognition.test_full_model import RecordedInterpreter


class ReferenceMetadataTests(unittest.TestCase):
    def test_actual_reply_author_not_requester_is_recorded(self):
        scope=TransportScope(-10,5,'supergroup',1,20)
        source=NS(chat_id=-10,message_id=12,message_thread_id=5,from_user=NS(id=2,is_bot=False),sender_chat=None)
        self.assertEqual(reference_source(source,scope)['user_id'],2)
        self.assertEqual(reference_source(source,scope)['message_id'],12)
        source.from_user.is_bot=True
        self.assertIsNone(reference_source(source,scope))

    def test_runtime_absent_case_is_covered_by_async_suite(self):
        self.assertIsNone(reference_source(None,None))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class MediaProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(False)
        self.runtime_patch=patch('cognition.runtime.get_runtime',return_value=self.runtime); self.runtime_patch.start()
        self.scope=TransportScope(1,-1,'private',1,11)
        self.tokens=[(CURRENT_SCOPE,CURRENT_SCOPE.set(self.scope)),(CURRENT_TURN,CURRENT_TURN.set(None))]
        self.store=RequestStore(self.pool)
    async def asyncTearDown(self):
        self.runtime_patch.stop()
        for var,token in self.tokens: var.reset(token)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)
    def request(self,scope=None,mid=11):
        scope=scope or self.scope
        return dict(type='dubbing',chat_id=scope.chat_id,message_id=mid,url='https://synthetic.invalid/video',_telegram_scope=scope)
    async def enqueue(self,request,key='fixture'):
        ns=uuid.uuid4().hex
        row=await self.store.enqueue('dubbing',request['chat_id'],request['_telegram_scope'].topic_id,key,await encode_request(request),resources=[ns])
        return row,ns

    async def test_callback_without_turn_captures_neutral_source_without_model_job(self):
        captured=await capture(self.request(),self.scope,None)
        again=await capture(self.request(),self.scope,None)
        turn=captured['_cognitive_turn']
        self.assertEqual(turn.event_id,again['_cognitive_turn'].event_id)
        self.assertEqual(turn.event.event_kind,'media_request'); self.assertEqual(turn.event.evidence.owner_id,1)
        self.assertEqual(turn.memory,''); self.assertEqual(turn.expression.cause_ids,())
        self.assertEqual(captured['user_id'],1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_jobs'),0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_events'),1)
        self.assertEqual(self.runtime.interpreter.calls,0)

    async def test_owner_or_topic_mismatched_existing_turn_is_rejected(self):
        turn=await self.runtime.prepare(1,1,'Synthetic command',2)
        other=TransportScope(-10,5,'supergroup',2,11)
        with self.assertRaises(SuppressedEvidence): await capture(self.request(other),other,turn)
        with self.assertRaises(ValueError): await capture({**self.request(),'user_id':2},self.scope,None)

    async def test_actual_reference_author_is_a_source_dependency(self):
        scope=TransportScope(-10,5,'supergroup',1,20)
        req=self.request(scope,20)
        req['reference_source']=dict(chat_id=-10,topic_id=5,message_id=12,user_id=2,sender_kind='user')
        captured=await capture(req,scope,None)
        self.assertNotIn('reference_source',captured)
        async with self.pool.acquire() as conn:
            original=await conn.fetchrow("SELECT * FROM cognitive_events WHERE source_id='telegram:-10:12:user'")
            self.assertEqual(original['owner_id'],2)
            self.assertIn(original['id'],captured['_cognitive_source_ids'])
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_jobs'),0)
        row,ns=await self.enqueue(captured)
        await forget_cognitive_sources(self.pool,captured['_cognitive_turn'].context_id,2,['telegram:-10:12:user'])
        self.assertEqual((await self.store.status(row['id']))['state'],'cancelled')
        self.assertEqual((await self.store.claim_resource_cleanup(grace_seconds=0))['namespace'],ns)

    async def test_forgetting_one_group_owner_scrubs_only_dependent_request(self):
        a=TransportScope(-10,5,'supergroup',1,11); b=TransportScope(-10,5,'supergroup',2,12)
        first=await capture(self.request(a,11),a,None); second=await capture(self.request(b,12),b,None)
        one,nsa=await self.enqueue(first,'one'); two,nsb=await self.enqueue(second,'two')
        turn=first['_cognitive_turn']
        await forget_cognitive_sources(self.pool,turn.context_id,1,[turn.event.evidence.source_id])
        self.assertEqual((await self.store.status(one['id']))['state'],'cancelled')
        self.assertEqual((await self.store.status(two['id']))['state'],'queued')
        async with self.pool.acquire() as conn:
            self.assertNotEqual(await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',two['id']),'{}')
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',nsb),'bound')
            saved=await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',two['id'])
            await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-10,TRUE)')
        import json
        decoded=await decode_request(json.loads(saved),NS())
        self.assertGreater(decoded['_cognitive_turn'].epoch,turn.epoch)
        # An already running independent media task must also survive; its
        # transport check refreshes under the context lock, not before it.
        from cognition.delivery import send_with_receipt
        stale=second['_cognitive_turn']; CURRENT_TURN.set(stale)
        transport=AsyncMock(return_value=NS(message_id=902,text='Synthetic result',chat=NS(id=-10)))
        await send_with_receipt(transport,(),dict(chat_id=-10,text='Synthetic result'),'message')
        transport.assert_awaited_once()
        self.assertEqual(stale.epoch,decoded['_cognitive_turn'].epoch)

    async def test_reset_blocks_transport_and_worker_reclaims_registry(self):
        captured=await capture(self.request(),self.scope,None); row,ns=await self.enqueue(captured)
        await self.runtime.reset_history(1)
        from cognition.delivery import send_with_receipt,DeliverySuppressed
        CURRENT_TURN.set(captured['_cognitive_turn'])
        transport=AsyncMock()
        with self.assertRaises(DeliverySuppressed):
            await send_with_receipt(transport,(),dict(chat_id=1,text='must not send'),'message')
        transport.assert_not_awaited()
        CURRENT_TURN.set(None)
        with self.assertRaises(SuppressedEvidence): await decode_request(await encode_request(captured),NS())
        from bot.request_runtime import worker
        generated=AsyncMock(side_effect=AssertionError('must not generate after reset'))
        with patch('bot.media_jobs.execute',new=generated):
            task=asyncio.create_task(worker(NS(),['dubbing']))
            try:
                for _ in range(100):
                    if (await self.store.status(row['id']))['state']=='cancelled': break
                    await asyncio.sleep(.01)
                self.assertEqual((await self.store.status(row['id']))['state'],'cancelled')
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
        generated.assert_not_awaited()
        self.assertEqual((await self.store.claim_resource_cleanup(grace_seconds=0))['namespace'],ns)

    async def test_no_runtime_configuration_makes_no_cognitive_claim(self):
        with patch('cognition.runtime.get_runtime',return_value=None):
            captured=await capture(self.request(),self.scope,None)
        self.assertIsNone(captured['_cognitive_turn']); self.assertNotIn('_cognitive_source_ids',captured)

    async def test_recovered_codec_cannot_omit_erased_support_or_change_input(self):
        from bot.request_codec import encode_value,decode_value,CodecError
        scope=TransportScope(-10,5,'supergroup',1,20)
        request=self.request(scope,20)
        request['reference_source']=dict(chat_id=-10,topic_id=5,message_id=12,user_id=2,sender_kind='user')
        captured=await capture(request,scope,None)
        wire=await encode_request(captured)
        wire['value']['items']['url']='https://synthetic.invalid/changed'
        with self.assertRaisesRegex(CodecError,'media_request_content_changed'): await decode_request(wire,NS())
        turn=captured['_cognitive_turn']; encoded=await encode_value(turn)
        encoded['value']['items']['supporting_event_ids']={'codec':1,'kind':'list','items':[]}
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_events SET suppressed_at=NOW(),payload=NULL WHERE source_id='telegram:-10:12:user'")
        with self.assertRaises(SuppressedEvidence): await decode_value(encoded)

    async def test_revoked_material_guard_blocks_neutral_media_recovery(self):
        from materials.types import AccessContext,MaterialScope,MaterialError
        from materials.runtime import MaterialUse
        actor=AccessContext(MaterialScope('arti',1,-1,'private'),1,'user:1')
        service=NS(repository=NS(read=AsyncMock(return_value=({'generation':4,'current_version':1},None))))
        guard=MaterialUse('synthetic-asset',actor,1,3,service)
        captured=await capture({**self.request(),'_material_uses':(guard,)},self.scope,None)
        wire=await encode_request(captured)
        with patch('materials.runtime.service_for_bot',new=AsyncMock(return_value=service)):
            with self.assertRaises(MaterialError): await decode_request(wire,NS())

    async def test_reset_does_not_allow_same_old_intake_marker_to_be_recaptured(self):
        await capture(self.request(),self.scope,None)
        await self.runtime.reset_history(1)
        with self.assertRaises(SuppressedEvidence): await capture(self.request(),self.scope,None)

    async def test_shared_rebuild_defers_without_losing_valid_request(self):
        from bot.request_runtime import CURRENT_REQUEST, _execute, MediaContextBusy
        captured=await capture(self.request(),self.scope,None)
        row,_=await self.enqueue(captured)
        job=await self.store.claim(['dubbing'])
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1',captured['_cognitive_turn'].context_id)
        token=CURRENT_REQUEST.set(job)
        try:
            with self.assertRaises(MediaContextBusy):await _execute(job,NS())
            self.assertTrue(await self.store.release(job['id'],job['token'],delay_seconds=1))
            self.assertIsNone(await self.store.claim(['dubbing']))
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE cognitive_contexts SET rebuilding=FALSE')
                await conn.execute("UPDATE arti_requests SET available_at=NOW()-INTERVAL '1 second'")
            job=await self.store.claim(['dubbing']);CURRENT_REQUEST.set(job)
            with patch('bot.media_jobs.execute',new=AsyncMock()) as execute,patch('utils.response_status.is_responses_enabled',new=AsyncMock(return_value=True)):
                await _execute(job,NS())
                execute.assert_awaited_once()
            self.assertEqual((await self.store.status(row['id']))['state'],'completed')
        finally:CURRENT_REQUEST.reset(token)
