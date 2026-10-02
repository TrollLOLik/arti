import asyncio
import base64
from dataclasses import asdict,replace
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from materials.derivatives import DerivativeRepository
from materials.extractors.audio import AudioExtractor
from materials.observations import ObservationRepository
from materials.timeline import Timeline,Segment,Word,assembly_timeline,groq_timeline
from materials.types import AccessContext,MaterialScope,MaterialError,EvidenceRef
from tests.materials.audio_fixtures import wav,assembly_result


class TimelineTests(unittest.TestCase):
    def test_overlap_mixed_language_and_local_ids_preserve_timestamps(self):
        timeline=assembly_timeline(assembly_result(),3000)
        self.assertEqual(('speaker_1','speaker_2'),tuple(s.speaker for s in timeline.segments))
        self.assertEqual((1100,1600),(timeline.overlaps()[0]['start_ms'],timeline.overlaps()[0]['end_ms']))
        self.assertEqual(.52,timeline.segments[0].words[1].score)
        self.assertEqual('I disagree.',timeline.at(1700,2400)[0].text)
        self.assertEqual(timeline,Timeline.from_dict(json.loads(json.dumps(asdict(timeline)))))

    def test_untimed_plain_text_does_not_gain_fake_timestamps(self):
        for timeline in (assembly_timeline({'text':'1200'},3000),groq_timeline({'text':'1200'},3000)):
            self.assertEqual((),timeline.segments); self.assertIn('timed_transcript_unavailable',timeline.limitations)

    def test_groq_real_words_and_unknown_speaker(self):
        timeline=groq_timeline(dict(segments=[dict(start=.4,end=1.6,text='Сумма 1200')],words=[dict(start=.4,end=.8,word='Сумма'),dict(start=.9,end=1.6,word='1200')]),3000)
        self.assertIsNone(timeline.segments[0].speaker)
        self.assertEqual((900,1600),(timeline.segments[0].words[1].start_ms,timeline.segments[0].words[1].end_ms))

    def test_confirmation_creates_version_preserves_original_word_alignment(self):
        original=assembly_timeline(assembly_result(),3000)
        corrected=original.confirm('turn_1','Сумма 1500.',actor_ref='user:7',expected_id=original.id)
        self.assertEqual('Сумма 1200.',original.segments[0].text)
        self.assertNotEqual(original.id,corrected.id); self.assertEqual(original.id,corrected.parent_id)
        self.assertEqual(original.segments[0].words,corrected.segments[0].words)
        self.assertEqual('confirmed',corrected.segments[0].status)
        with self.assertRaisesRegex(MaterialError,'stale'): corrected.confirm('turn_1','new',actor_ref='user:7',expected_id=original.id)

    def test_invalid_alignment_and_human_identity_fail(self):
        with self.assertRaises(MaterialError): Word('hello',100,90)
        with self.assertRaises(MaterialError): Word('hello',100,200,speaker='user:7')
        with self.assertRaises(MaterialError): Segment('a','x',100,200,(Word('x',50,120),))
        result=assembly_result(); result['utterances'][0]['end']=4000
        with self.assertRaises(MaterialError): assembly_timeline(result,3000)


class AudioDecoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_decoder_energy_intervals_and_bounded_partial(self):
        extractor=AudioExtractor(max_seconds=1)
        bundle=await extractor.extract_async('audio',1,wav(),'audio/wav')
        self.assertEqual((3000,1000,'partial'),(bundle.manifest.total_units,bundle.manifest.processed_units,bundle.manifest.coverage))
        self.assertIn('audio_duration_budget_reached',bundle.manifest.limitations)
        root=bundle.blocks[0]; self.assertEqual((0,1000),(root.locator.start_ms,root.locator.end_ms))
        self.assertTrue(root.metadata['timeline']['acoustic'])
        self.assertIn('transcript_unavailable',bundle.manifest.limitations)

    async def test_structured_source_word_refs_scores_silence_and_overlap(self):
        provider=SimpleNamespace(identity='fixture-asr',transcribe=AsyncMock(return_value=assembly_timeline(assembly_result(),3000)))
        bundle=await AudioExtractor(transcriber=provider).extract_authorized('audio',1,wav(),'audio/wav',validate=AsyncMock())
        self.assertEqual('unknown',bundle.manifest.coverage)
        turns=[b for b in bundle.blocks if b.metadata.get('role')=='speaker_turn']
        words=[b for b in bundle.blocks if b.metadata.get('role')=='timed_word']
        self.assertEqual((2,4),(len(turns),len(words)))
        self.assertTrue(all(b.quality=='uncertain' and b.locator.kind.value=='time' for b in words))
        self.assertIn('overlapping_speaker_turns_unverified',bundle.manifest.limitations)
        self.assertTrue(any(b.metadata.get('role')=='low_energy_interval' for b in bundle.blocks))
        from materials.extractors.basic import render_text
        text=render_text(bundle); self.assertIn('time_ms:400-1600',text); self.assertIn('speaker_1 (local, identity unconfirmed)',text)
        self.assertEqual(1,text.count('Сумма 1200.'))

    async def test_provider_upload_requires_post_decoder_authorization(self):
        provider=SimpleNamespace(identity='fixture-asr',transcribe=AsyncMock())
        extractor=AudioExtractor(transcriber=provider)
        with self.assertRaises(MaterialError): await extractor.extract_async('a',1,wav(),'audio/wav')
        with self.assertRaisesRegex(MaterialError,'erased'):
            await extractor.extract_authorized('a',1,wav(),'audio/wav',validate=AsyncMock(side_effect=MaterialError('erased')))
        provider.transcribe.assert_not_awaited()

    async def test_groq_requests_timed_verbose_json_and_rechecks(self):
        from ai.stt import StructuredTranscriber
        result=dict(segments=[dict(start=.4,end=1.6,text='1200')],words=[dict(start=.4,end=1.6,word='1200')])
        call=AsyncMock(return_value=SimpleNamespace(model_dump=lambda:result))
        provider=SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=call)))
        validate=AsyncMock()
        timeline=await StructuredTranscriber(assembly_key='',groq=provider).transcribe(wav(),'audio/wav',3000,validate=validate)
        self.assertEqual('1200',timeline.segments[0].text); self.assertGreaterEqual(validate.await_count,3)
        self.assertEqual('verbose_json',call.call_args.kwargs['response_format'])
        self.assertEqual(['word','segment'],call.call_args.kwargs['timestamp_granularities'])

    async def test_large_timeline_stays_inside_block_budget(self):
        timeline=Timeline(3000,tuple(Segment('s'+str(i),'word',0,100,(Word('word',0,100),)*3) for i in range(2000)),method='fixture')
        analysis=dict(start_ms=0,end_ms=3000,duration_ms=3000,acoustic=[],streams=[],silence=[],limitations=[])
        bundle=AudioExtractor().bundle('a',1,analysis,timeline)
        self.assertLessEqual(len(bundle.blocks),3800)
        self.assertIn('turn_block_budget_reached',bundle.manifest.limitations)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','set ARTI_TEST_DB=1 for disposable PostgreSQL')
class AudioPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from materials.lifecycle import MaterialLifecycle
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); await ensure_schema(self.pool)
        self.temp=tempfile.TemporaryDirectory(); self.repo=MaterialRepository(self.pool)
        self.service=MaterialService(self.repo,LocalBlobStore(self.temp.name)); self.lifecycle=MaterialLifecycle(self.repo,self.service.store)
        self.actor=AccessContext(MaterialScope('arti',-100,4,'supergroup'),7,'user:7')
        self.asset=await self.service.ingest(wav(),'recording.wav',self.actor,'recording','recording')
        self.provider=SimpleNamespace(identity='fixture-asr',transcribe=AsyncMock(return_value=assembly_timeline(assembly_result(),3000)))
        self.extractor=AudioExtractor(transcriber=self.provider)
        self.id,self.timeline,self.ref=await self.service.transcript(self.asset['id'],self.actor,extractor=self.extractor)
        self.derivatives=DerivativeRepository(self.repo)
    async def asyncTearDown(self):
        self.temp.cleanup(); await self.db.__aexit__(None,None,None)

    async def test_restart_confirmation_and_recursive_dependencies_are_revoked(self):
        first=await self.derivatives.save(self.actor,'summary',{'text':'1200'},[],inputs=(self.id,))
        second=await self.derivatives.save(self.actor,'report',{'text':'summary'},[],inputs=(first,))
        corrected_id,corrected=await self.service.confirm_transcript(self.asset['id'],self.actor,self.id,'turn_1','Сумма 1500.')
        for id in (self.id,first,second):
            with self.assertRaises(MaterialError): await self.derivatives.load(id,self.actor)
        with self.assertRaises(MaterialError): await self.derivatives.save(self.actor,'stale',{},[],inputs=(self.id,))
        restarted_id,restarted,_=await self.service.transcript(self.asset['id'],self.actor,extractor=self.extractor)
        self.assertEqual(corrected_id,restarted_id); self.assertEqual('Сумма 1500.',restarted.segments[0].text)
        original=await self.repo.evidence_bundle(self.ref,self.actor)
        self.assertEqual('Сумма 1200.',next(b for b in original.blocks if b.metadata.get('role')=='timeline_chunk').metadata['segments'][0]['text'])
        new=await self.derivatives.save(self.actor,'new',{'text':'1500'},[],inputs=(corrected_id,))
        await self.lifecycle.forget(self.asset['id'],self.actor)
        with self.assertRaises(MaterialError): await self.derivatives.load(new,self.actor)
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_derivatives WHERE payload IS NOT NULL'))

    async def test_permissions_topic_source_owner_tombstone_and_late_guard(self):
        reader=replace(self.actor,user_id=8,sender_ref='user:8')
        self.assertEqual(self.id,(await ObservationRepository(self.repo).current(reader,self.asset['id']))[0])
        with self.assertRaises(MaterialError): await self.service.confirm_transcript(self.asset['id'],reader,self.id,'turn_1','fake')
        other=replace(reader,scope=replace(reader.scope,topic_id=5))
        with self.assertRaises(MaterialError): await self.derivatives.load(self.id,other)
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO material_source_tombstones(scope_key,owner_id,source_id) VALUES($1,$2,$3)',self.actor.scope.identity_key,7,'recording')
        with self.assertRaisesRegex(MaterialError,'source_erased'): await self.derivatives.load(self.id,reader)
        with self.assertRaisesRegex(MaterialError,'source_erased'): await self.repo.resolve(self.ref,reader)

    async def test_source_revision_and_competing_confirmations(self):
        results=await asyncio.gather(self.service.confirm_transcript(self.asset['id'],self.actor,self.id,'turn_1','one'),self.service.confirm_transcript(self.asset['id'],self.actor,self.id,'turn_1','two'),return_exceptions=True)
        self.assertEqual(1,sum(isinstance(r,MaterialError) for r in results))
        await self.service.revise(self.asset['id'],wav(noise=True),'recording.wav',self.actor,1)
        id,timeline,ref=await self.service.transcript(self.asset['id'],self.actor,extractor=self.extractor)
        self.assertNotEqual(self.id,id); self.assertEqual(2,ref.asset_version); self.assertEqual('Сумма 1200.',timeline.segments[0].text)

    async def test_replay_clip_resolves_actual_turn_interval(self):
        bundle=await self.repo.evidence_bundle(self.ref,self.actor)
        turn=next(b for b in bundle.blocks if b.metadata.get('segment_id')=='turn_1')
        ref=EvidenceRef(self.asset['id'],1,self.ref.extraction_id,turn.block_id,turn.locator)
        clip=await self.service.audio_clip(ref,self.actor,extractor=self.extractor)
        self.assertEqual((400,1600,1200),(clip['start_ms'],clip['end_ms'],clip['processed_ms']))
        self.assertTrue(base64.b64decode(clip['audio_base64']).startswith((b'ID3',b'\xff')))

    async def test_head_change_blocks_queued_derivative_usage(self):
        from materials.runtime import CURRENT_DERIVATIVE_USE,DerivativeUse,guard_current
        token=CURRENT_DERIVATIVE_USE.set((DerivativeUse(self.id,self.actor,self.derivatives,'transcript'),))
        try:
            await guard_current(-100)
            await self.service.confirm_transcript(self.asset['id'],self.actor,self.id,'turn_1','1500')
            with self.assertRaises(MaterialError): await guard_current(-100)
        finally: CURRENT_DERIVATIVE_USE.reset(token)

    async def test_forget_after_decode_blocks_asr_transmission(self):
        import materials.extractors.audio as module
        original=module.run_worker
        async def erase(*args,**kwargs):
            value=await original(*args,**kwargs); await self.lifecycle.forget(self.asset['id'],self.actor); return value
        provider=SimpleNamespace(identity='other-asr',transcribe=AsyncMock())
        with patch.object(module,'run_worker',side_effect=erase):
            with self.assertRaises(MaterialError): await self.service.transcript(self.asset['id'],self.actor,extractor=AudioExtractor(transcriber=provider))
        provider.transcribe.assert_not_awaited()

    async def test_forget_during_asr_blocks_commit(self):
        async def erase(*args,**kwargs):
            await self.lifecycle.forget(self.asset['id'],self.actor)
            return assembly_timeline(assembly_result(),3000)
        provider=SimpleNamespace(identity='erase-during-asr',transcribe=AsyncMock(side_effect=erase))
        with self.assertRaises(MaterialError): await self.service.transcript(self.asset['id'],self.actor,extractor=AudioExtractor(transcriber=provider))

    async def test_telegram_commands_fix_cas_and_replay_mock_transport(self):
        from bot.audio_commands import _run
        from materials.runtime import MaterialText,MaterialUse
        material=MaterialText('audio',MaterialUse(self.asset['id'],self.actor,1,self.asset['generation'],self.service))
        source=SimpleNamespace(voice=SimpleNamespace(file_id='fixture',file_size=10,mime_type='audio/wav'),audio=None,document=None)
        message=SimpleNamespace(reply_to_message=source,text='/transcript',chat_id=-100,reply_text=AsyncMock(),reply_audio=AsyncMock())
        original=self.service.transcript
        async def transcript(*args,**kwargs): return await original(*args,extractor=self.extractor)
        with patch('bot.audio_commands.enabled',return_value=True),patch('bot.audio_commands.capture_document',new=AsyncMock(return_value=material)),patch('bot.audio_commands.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.audio_commands.service_for_bot',new=AsyncMock(return_value=self.service)),patch.object(self.service,'transcript',side_effect=transcript):
            await _run(SimpleNamespace(effective_message=message),SimpleNamespace(),'transcript')
            self.assertIn(self.id[:12],message.reply_text.call_args.args[0])
            message.text='/listen turn_1'; await _run(SimpleNamespace(effective_message=message),SimpleNamespace(),'listen')
            message.reply_audio.assert_awaited_once()
            message.text='/transcript_fix turn_1 "1500" version='+self.id[:12]
            await _run(SimpleNamespace(effective_message=message),SimpleNamespace(),'fix')
            self.assertIn('подтверждено',message.reply_text.call_args.args[0])
            await _run(SimpleNamespace(effective_message=message),SimpleNamespace(),'fix')
            self.assertIn('Версия изменилась',message.reply_text.call_args.args[0])

    async def test_appraisal_context_is_scoped_unknown_and_revocable(self):
        from datetime import datetime,timezone
        from cognition.types import CognitiveEvent,ContextKey,EvidenceRef as CognitiveRef,Origin,AudienceScope
        from cognition.sensory import acoustic_context
        now=datetime.now(timezone.utc)
        event=CognitiveEvent('recording',ContextKey('arti',-100,'default','',4),CognitiveRef('recording','recording',Origin.USER,7),now,now,'material',7,audience=AudienceScope('topic',-100,4))
        context=await acoustic_context(self.pool,event)
        self.assertEqual('unknown',context[0]['speaker_identity']); self.assertNotIn('emotion',context[0]['samples'][0])
        self.assertEqual((),await acoustic_context(self.pool,replace(event,context=replace(event.context,topic_id=5),audience=AudienceScope('topic',-100,5))))
        await self.lifecycle.forget(self.asset['id'],self.actor)
        self.assertEqual((),await acoustic_context(self.pool,event))
