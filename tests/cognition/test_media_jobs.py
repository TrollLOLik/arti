"""Disk/SQL recovery with synthetic generation and transport, no GPU/providers."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ExtBot
from bot.retry_bot import RetryBot
from bot.request_runtime import CURRENT_REQUEST, store, send
from bot.media_jobs import submit_media, execute, maintenance_once
from bot.media_spool import MediaSpool
from cognition.runtime import CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class DurableMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.disk=MediaSpool(self.root/'spool')
        self.source=self.root/'original.wav'; self.source.write_bytes(b'private synthetic reference')
        self.tokens=[(v,v.set(x)) for v,x in ((CURRENT_REQUEST,None),(CURRENT_TURN,None),
            (CURRENT_SCOPE,TransportScope(10,-1,'private',10,1)),
            (CURRENT_MATERIAL_USE,()),(CURRENT_DERIVATIVE_USE,()),(CURRENT_COMPUTATION_USE,()))]
        self.patches=[patch('bot.media_jobs.spool',return_value=self.disk),patch('config.TTS_ENABLED',True),patch('config.PRIVILEGED_USER_IDS',{10}),
            patch.object(ExtBot,'send_message',new=AsyncMock(return_value=NS(message_id=90)))]
        for p in self.patches:p.start()
        ExtBot.send_message.__name__='send_message'
        self.bot=RetryBot('123456:offline-test-placeholder')
        self.sent=[]
        async def transport(bot,*args,**kwargs):
            value=kwargs.get('voice') or kwargs.get('audio') or kwargs.get('video')
            self.sent.append(value.read())
            return NS(message_id=100+len(self.sent))
        transport.__name__='send_voice'
        self.media_patch=patch.object(ExtBot,'send_voice',new=transport); self.media_patch.start()
    async def asyncTearDown(self):
        self.media_patch.stop()
        for p in reversed(self.patches):p.stop()
        for v,t in reversed(self.tokens):v.reset(t)
        await self.db.__aexit__(None,None,None); self.temp.cleanup()
    async def submit(self):
        return await submit_media(dict(chat_id=10,user_id=10,user_name='Synthetic',message_id=1,
            reference_path=str(self.source),synthesis_text='Synthetic speech',source_kind='synthetic'),self.bot,10,'vclone')
    async def claim(self):
        from bot.request_codec import decode_request
        job=await store().claim(['vclone']); CURRENT_REQUEST.set(job)
        request=await decode_request(job['payload'],self.bot)
        return job,request
    async def generated(self,reference,text,work):
        self.assertEqual(reference.read_bytes(),b'private synthetic reference')
        result=work/'fixture.ogg'; result.write_bytes(b'synthetic generated voice')
        return result,'voice'

    async def test_restart_retains_input_and_confirmed_output_without_second_generation(self):
        queued=await self.submit(); self.source.unlink()
        job,request=await self.claim()
        provider=AsyncMock(side_effect=self.generated)
        with patch('ai.voice_clone_job.generate_clone',new=provider):
            await execute(request,self.bot)
            await store().release(job['id'],job['token'])
            job,request=await self.claim(); await execute(request,self.bot)
        provider.assert_awaited_once(); self.assertEqual(self.sent,[b'synthetic generated voice'])
        await store().finish(job['id'],job['token'],'completed'); await maintenance_once(cleanup_grace=0)
        # Only the separately retained15-minute reference survives completion.
        self.assertEqual(len([p for p in self.disk.root.iterdir() if p.is_dir()]),1)

    async def test_duplicate_intake_does_not_adopt_or_delete_original_copy(self):
        first=await self.submit(); second=await self.submit()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(len(await store().adopted_resources(first['id'])),1)
        self.assertTrue(self.source.exists())
        self.assertEqual(len([p for p in self.disk.root.iterdir() if p.is_dir()]),1)

    async def test_cancel_scrubs_staged_input_and_preserves_user_original(self):
        await self.submit(); await store().cancel_chat(10)
        await maintenance_once(cleanup_grace=0)
        self.assertEqual(self.source.read_bytes(),b'private synthetic reference')
        self.assertFalse(any(p.is_dir() for p in self.disk.root.iterdir()))

    async def test_uncertain_enqueue_does_not_delete_possibly_committed_input(self):
        from bot.request_runtime import submit as real_submit
        async def lost_reply(*args,**kwargs):
            await real_submit(*args,**kwargs)
            raise OSError('synthetic lost database response')
        with patch('bot.request_runtime.submit',new=lost_reply):
            with self.assertRaises(OSError): await self.submit()
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)):
            await execute(request,self.bot)
        self.assertEqual(len(self.sent),1)

    async def test_cross_request_descriptor_cannot_reach_transport(self):
        await self.submit(); job,request=await self.claim()
        descriptor=request['reference_media']
        await store().enqueue('other',20,-1,'other',{})
        foreign=await store().claim(['other']); CURRENT_REQUEST.set(foreign)
        transport=AsyncMock()
        with self.assertRaisesRegex(ValueError,'request_resource_unavailable'):
            await send(transport,(),dict(chat_id=20,voice={'_arti_spooled_file':descriptor}),'voice')
        transport.assert_not_awaited()

    async def test_unknown_delivery_is_terminal_without_fallback_or_resend(self):
        await self.submit(); job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)), \
             patch.object(ExtBot,'send_voice',new=AsyncMock(side_effect=TimeoutError())) as transport:
            transport.__name__='send_voice'
            with self.assertRaises(TimeoutError): await execute(request,self.bot)
            await store().release(job['id'],job['token'])
            self.assertIsNone(await store().claim(['vclone']))
            transport.assert_awaited_once()
        self.assertEqual((await store().status(job['id']))['state'],'delivery_unknown')
        await maintenance_once(cleanup_grace=0); self.assertTrue(self.source.exists())

    async def test_generation_crash_requires_explicit_retry_from_staged_input(self):
        await self.submit(); job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=OSError('crash'))):
            with self.assertRaises(OSError): await execute(request,self.bot)
        await store().release(job['id'],job['token'])
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)) as provider:
            await execute(request,self.bot)
            provider.assert_not_awaited()
        self.assertEqual((await store().status(job['id']))['state'],'paused')
        self.assertFalse(await store().resume(job['id'],10,-1,99,'foreign-retry'))
        self.assertTrue(await store().resume(job['id'],10,-1,10,'explicit-owner-retry'))
        self.assertFalse(await store().resume(job['id'],10,-1,10,'explicit-owner-retry'))
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)):
            await execute(request,self.bot)
        self.assertEqual(len(self.sent),1)

    async def test_overnight_queue_and_long_downtime_expiry(self):
        queued=await self.submit()
        self.assertGreater((queued['deadline_at']-queued['created_at']).total_seconds(),6*86400)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET created_at=created_at-INTERVAL '2 days',deadline_at=deadline_at-INTERVAL '2 days'")
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)):
            await execute(request,self.bot)
        self.assertEqual(len(self.sent),1)
        await store().release(job['id'],job['token'])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_requests SET deadline_at=NOW()-INTERVAL '1 second'")
        self.assertIsNone(await store().claim(['vclone']))
        self.assertEqual((await store().status(job['id']))['state'],'expired')
        await maintenance_once(cleanup_grace=0)
        self.assertTrue(self.source.exists())

    async def test_disk_budget_stops_writer_before_cleanup(self):
        from bot.media_jobs import bounded_work
        ns=self.disk.create_namespace(); stopped=asyncio.Event()
        async def writer():
            (self.disk.workdir(ns)/'too-big').write_bytes(b'x'*(2*1024**2))
            try: await asyncio.Event().wait()
            finally: stopped.set()
        with patch.dict(os.environ,ARTI_MEDIA_WORK_MAX_BYTES=str(1024**2),ARTI_MEDIA_MIN_FREE_BYTES='0'):
            with self.assertRaisesRegex(RuntimeError,'media_disk_budget_exceeded'):
                await bounded_work(writer,self.disk,ns)
        self.assertTrue(stopped.is_set())
        self.disk.cleanup(ns)

    async def test_explicit_owned_temp_input_is_removed_only_after_staging(self):
        from bot.media_intake import owned_intake
        original=self.root/'temp'/'vclone_ref_12345678.wav';original.parent.mkdir();original.write_bytes(b'private synthetic reference')
        task=dict(chat_id=10,user_id=10,message_id=1,reference_path=str(original),
            synthesis_text='Synthetic',source_kind='synthetic',_owned_intake=owned_intake(original,root=original.parent))
        await submit_media(task,self.bot,10,'vclone')
        self.assertFalse(original.exists())
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)):
            await execute(request,self.bot)
        self.assertEqual(len(self.sent),1)

    async def test_definite_generation_failure_notice_but_no_unknown_fallback(self):
        from bot.request_runtime import _failure_notice
        await self.submit();job,request=await self.claim()
        notice=AsyncMock(return_value=NS(message_id=91));notice.__name__='send_message'
        with patch.object(ExtBot,'send_message',new=notice):
            await _failure_notice(job,self.bot)
            notice.assert_awaited_once()
        transport=AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await send(transport,(),dict(chat_id=10,text='uncertain media outcome'),'message')
        notice=AsyncMock(return_value=NS(message_id=91));notice.__name__='send_message'
        with patch.object(ExtBot,'send_message',new=notice),patch.object(ExtBot,'edit_message_text',new=AsyncMock()) as edit:
            await _failure_notice(job,self.bot)
            notice.assert_not_awaited();edit.assert_not_awaited()

    async def test_retained_voice_button_survives_completion_and_blocks_erased_reference(self):
        from bot.media_retention import Retention
        from bot.media_jobs import save_voice_callback
        from bot.commands import _vclone_save_reference
        from config import vclone_save_flow_state
        await submit_media(dict(chat_id=10,user_id=10,message_id=1,reference_path=str(self.source),
            synthesis_text='Synthetic',source_kind='stepwise'),self.bot,10,'vclone')
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)):
            await execute(request,self.bot)
        await store().finish(job['id'],job['token'],'completed')
        CURRENT_REQUEST.set(None)
        await maintenance_once(cleanup_grace=0)
        retained=await Retention(self.pool).load(job['id'],'voice_reference',10,10,-1)
        self.assertIsNotNone(retained)
        query=NS(from_user=NS(id=10),message=NS(chat_id=10),data='media_voice_save:'+job['id'],answer=AsyncMock())
        with patch('bot.commands._gate_tts_disabled',new=AsyncMock(return_value=False)),patch('bot.commands._gate_vclone_not_privileged',new=AsyncMock(return_value=False)):
            await save_voice_callback(NS(callback_query=query),NS(bot=self.bot))
        state=vclone_save_flow_state[10][10]
        self.assertEqual(state['media_retained_id'],job['id'])
        with patch('bot.media_spool.MediaSpool',return_value=self.disk):
            async with _vclone_save_reference(state,10,10) as path:
                self.assertEqual(Path(path).read_bytes(),b'private synthetic reference')
            await store().erase_chat(10)
            with self.assertRaises(ValueError):
                async with _vclone_save_reference(state,10,10): pass
        vclone_save_flow_state[10].pop(10,None)

    async def test_oversize_result_retained_and_local_export_is_exclusive(self):
        from bot.media_retention import Retention
        from tools.export_media_result import export
        await self.submit();job,request=await self.claim()
        async def large(*args):
            path=args[2]/'large.ogg'
            with path.open('wb') as output: output.truncate(50*1024**2+1)
            return path,'voice'
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=large)):
            await execute(request,self.bot)
        await store().finish(job['id'],job['token'],'completed');CURRENT_REQUEST.set(None)
        await maintenance_once(cleanup_grace=0)
        retained=await Retention(self.pool).load(job['id'],'result',10,10,-1)
        self.assertIsNotNone(retained);self.assertEqual(self.sent,[])
        destination=self.root/'export.ogg'
        await export(self.pool,job['id'],10,destination,disk=self.disk)
        self.assertEqual(destination.stat().st_size,50*1024**2+1)
        with self.assertRaises(FileExistsError):await export(self.pool,job['id'],10,destination,disk=self.disk)
        with self.assertRaises(ValueError):await export(self.pool,job['id'],99,self.root/'foreign.ogg',disk=self.disk)

    async def test_prepared_voice_after_offer_expiry_delivers_checkpoint_once(self):
        from bot.request_store import RequestStore
        from cognition.delivery import DeliverySuppressed
        from bot.media_retention import Retention, expire
        await submit_media(dict(chat_id=10,user_id=10,message_id=1,reference_path=str(self.source),
            synthesis_text='Synthetic',source_kind='stepwise'),self.bot,10,'vclone')
        job,request=await self.claim();provider=AsyncMock(side_effect=self.generated)
        with patch('ai.voice_clone_job.generate_clone',new=provider):
            with patch.object(RequestStore,'begin_send',new=AsyncMock(return_value=False)):
                with self.assertRaises(DeliverySuppressed):await execute(request,self.bot)
            self.assertEqual(self.sent,[])
            await store().release(job['id'],job['token'])
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE arti_media_retained SET expires_at=NOW()-INTERVAL '1 hour'")
            await expire(self.pool);await maintenance_once(cleanup_grace=0)
            job,request=await self.claim()
            await execute(request,self.bot)
            # Simulate another crash after confirmation but before completion.
            await store().release(job['id'],job['token'])
            job,request=await self.claim();await execute(request,self.bot)
        provider.assert_awaited_once();self.assertEqual(self.sent,[b'synthetic generated voice'])

    async def test_optional_unavailable_voice_offer_does_not_drop_ready_media(self):
        from bot.media_retention import RetentionUnavailable
        await submit_media(dict(chat_id=10,user_id=10,message_id=1,reference_path=str(self.source),
            synthesis_text='Synthetic',source_kind='stepwise'),self.bot,10,'vclone')
        job,request=await self.claim()
        with patch('ai.voice_clone_job.generate_clone',new=AsyncMock(side_effect=self.generated)), \
             patch('bot.media_retention.retain_copy',new=AsyncMock(side_effect=RetentionUnavailable('retained_expired'))):
            await execute(request,self.bot)
        self.assertEqual(len(self.sent),1)

    async def test_dubbing_disk_input_checkpoint_uses_existing_delivery_ledger(self):
        await submit_media(dict(chat_id=10,message_id=1,input_file=str(self.source),audio_only=True,url=''),self.bot,10,'dubbing')
        from bot.request_codec import decode_request
        job=await store().claim(['dubbing']);CURRENT_REQUEST.set(job)
        request=await decode_request(job['payload'],self.bot)
        async def generate(*args,**kwargs):
            self.assertEqual(kwargs['input_file'].read_bytes(),b'private synthetic reference')
            result=kwargs['output_root']/'audio.mp3';result.write_bytes(b'dubbed synthetic audio')
            return True,result,''
        async def send_audio(bot,*args,**kwargs):
            self.sent.append(kwargs['audio'].read());return NS(message_id=101)
        provider=AsyncMock(side_effect=generate)
        with patch('ai.dubbing.run_dubbing',new=provider),patch.object(ExtBot,'send_audio',new=send_audio):
            await execute(request,self.bot)
            await store().release(job['id'],job['token'])
            job=await store().claim(['dubbing']);CURRENT_REQUEST.set(job)
            request=await decode_request(job['payload'],self.bot);await execute(request,self.bot)
        provider.assert_awaited_once();self.assertEqual(self.sent,[b'dubbed synthetic audio'])

    async def test_revoked_clone_privilege_blocks_recovered_execution(self):
        from cognition.delivery import DeliverySuppressed
        await self.submit();job,request=await self.claim()
        with patch('config.PRIVILEGED_USER_IDS',set()),patch('ai.voice_clone_job.generate_clone',new=AsyncMock()) as provider:
            with self.assertRaises(DeliverySuppressed):await execute(request,self.bot)
            provider.assert_not_awaited()
        self.assertEqual(self.sent,[])

    async def test_failed_acceptance_preserves_owned_intake_original(self):
        from bot.media_intake import owned_intake
        original=self.root/'temp'/'vclone_ref_12345678.wav';original.parent.mkdir();original.write_bytes(b'private synthetic reference')
        task=dict(chat_id=10,user_id=10,message_id=1,reference_path=str(original),
            synthesis_text='Synthetic',source_kind='synthetic',_owned_intake=owned_intake(original,root=original.parent))
        with patch('bot.media_provenance.capture',new=AsyncMock(side_effect=ValueError('rejected'))):
            with self.assertRaises(ValueError):await submit_media(task,self.bot,10,'vclone')
        self.assertTrue(original.exists())
        self.assertFalse(any(p.is_dir() for p in self.disk.root.iterdir()))
