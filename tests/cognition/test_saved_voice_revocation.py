"""Saved library record revocation fences already accepted private copies."""
import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import patch
from database.models import SavedVoice
from bot.saved_voice_sources import version_of
from bot.media_provenance import capture
from bot.request_codec import encode_request,decode_request
from bot.request_store import RequestStore
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.scope import CURRENT_SCOPE,TransportScope
from cognition.repositories import SuppressedEvidence
from tests.cognition.test_full_model import RecordedInterpreter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class SavedVoiceRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(False)
        self.runtime_patch=patch('cognition.runtime.get_runtime',return_value=self.runtime); self.runtime_patch.start()
        self.scope=TransportScope(1,-1,'private',1,40)
        self.tokens=[(CURRENT_SCOPE,CURRENT_SCOPE.set(self.scope)),(CURRENT_TURN,CURRENT_TURN.set(None))]
        self.store=RequestStore(self.pool)
    async def asyncTearDown(self):
        self.runtime_patch.stop()
        for var,token in self.tokens: var.reset(token)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)
    async def voice(self,owner=1,url='https://synthetic.invalid/voice-old.wav'):
        return await SavedVoice.save(owner,owner,'Synthetic voice',url)
    async def accepted(self,voice,owner=1,key='fixture'):
        scope=TransportScope(owner,-1,'private',owner,40)
        request=dict(type='vclone',chat_id=owner,user_id=owner,message_id=40,source_kind='saved_voice',
            synthesis_text='Synthetic output',saved_voice_id=voice['id'],saved_voice_version=version_of(voice))
        result=await capture(request,scope,None)
        payload=await encode_request(result); ns=uuid.uuid4().hex
        job=await self.store.enqueue('vclone',owner,-1,key,payload,resources=[ns])
        return result,payload,job,ns
    async def assert_revoked(self,payload,job,ns):
        self.assertEqual((await self.store.status(job['id']))['state'],'cancelled')
        with self.assertRaises(SuppressedEvidence): await decode_request(payload,NS())
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',ns),'cleanup_pending')
            self.assertEqual(await conn.fetchval('SELECT payload::text FROM arti_requests WHERE id=$1',job['id']),'{}')

    async def test_delete_blocks_accepted_copy_and_preserves_other_owner(self):
        one=await self.voice(); two=await self.voice(2)
        _,payload,job,ns=await self.accepted(one)
        _,other,other_job,other_ns=await self.accepted(two,2,'other')
        self.assertIsNone(await SavedVoice.delete(2,one['id']))
        await SavedVoice.delete(1,one['id']); await self.assert_revoked(payload,job,ns)
        self.assertEqual((await self.store.status(other_job['id']))['state'],'queued')
        await decode_request(other,NS())

    async def test_same_name_replacement_revokes_old_version_and_accepts_new(self):
        old=await self.voice(); prior,payload,job,ns=await self.accepted(old)
        new=await self.voice(url='https://synthetic.invalid/voice-new.wav')
        self.assertEqual(old['id'],new['id']); self.assertNotEqual(version_of(old),version_of(new))
        await self.assert_revoked(payload,job,ns)
        fresh,wire,other,_=await self.accepted(new,key='new-version')
        await decode_request(wire,NS())
        self.assertNotEqual(fresh['_cognitive_turn'].event.evidence.source_id,prior['_cognitive_turn'].event.evidence.source_id)
        stale=dict(type='vclone',chat_id=1,message_id=41,source_kind='saved_voice',saved_voice_id=old['id'],saved_voice_version=version_of(old))
        with self.assertRaises(SuppressedEvidence): await capture(stale,self.scope,None)

    async def test_delete_by_name_invalidates_retained_export_and_reference(self):
        from bot.media_retention import Retention
        voice=await self.voice(); _,wire,job,ns=await self.accepted(voice)
        claimed=await self.store.claim(['vclone'])
        descriptor=dict(version=1,namespace=ns,leaf='b'*32+'.wav',size=10,sha256='c'*64)
        await Retention(self.pool).retain(job['id'],claimed['token'],'voice_reference',descriptor,1)
        await self.store.finish(job['id'],claimed['token'],'completed')
        await SavedVoice.delete_by_name(1,'Synthetic voice')
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT descriptor,invalidated_at FROM arti_media_retained WHERE request_id=$1',job['id'])
            self.assertIsNotNone(row['invalidated_at']); self.assertEqual(str(row['descriptor']),'{}')
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_resources WHERE namespace=$1',ns),'cleanup_pending')

    async def test_capture_racing_delete_cannot_leave_accepted_live_copy(self):
        voice=await self.voice()
        results=await asyncio.gather(self.accepted(voice),SavedVoice.delete(1,voice['id']),return_exceptions=True)
        if not isinstance(results[0],BaseException):
            _,wire,job,ns=results[0]; await self.assert_revoked(wire,job,ns)
        else: self.assertIsInstance(results[0],(SuppressedEvidence,ValueError))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM arti_requests WHERE state IN ('queued','running')"),0)
