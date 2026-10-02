import asyncio,base64,os,socket,tempfile,unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from materials.extractors.video import VideoExtractor
from materials.extractors.images import ImageExtractor
from materials.types import AccessContext,MaterialScope,EvidenceRef,MaterialError
from tests.materials.video_fixtures import video


class VideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_scene_frames_native_pts_determinism_and_sparse_coverage(self):
        data=video(with_audio=False); extractor=VideoExtractor(max_frames=6,image=ImageExtractor(ocr_enabled=False))
        first=await extractor.extract_async('v',1,data,'video/mp4'); second=await extractor.extract_async('v',1,data,'video/mp4')
        self.assertEqual(first,second); self.assertEqual('partial',first.manifest.coverage)
        frames=[b for b in first.blocks if b.metadata.get('role')=='video_frame']
        self.assertEqual([0,900,1000,1500,2000,2900],[b.locator.start_ms for b in frames])
        self.assertEqual(1,first.blocks[0].metadata['adaptive_frame_budget'])
        self.assertIn('adaptive_reinspection_is_bounded_not_exhaustive',first.manifest.limitations)
        self.assertIn('audio_not_available',first.manifest.limitations)
        self.assertIn('sparse_frames_do_not_prove_event_absence',first.manifest.limitations)

    async def test_dense_view_catches_transient_and_bounds_duration(self):
        data=video(with_audio=False); extractor=VideoExtractor(max_frames=12,image=ImageExtractor(ocr_enabled=False),max_seconds=1)
        initial=await extractor.extract_async('v',1,data,'video/mp4')
        self.assertEqual(1000,initial.manifest.processed_units)
        dense=await extractor.extract_authorized('v',1,data,'video/mp4',validate=AsyncMock(),start_ms=1350,end_ms=1500,dense=True)
        self.assertIn(1400,[b.locator.start_ms for b in dense.blocks if b.metadata.get('role')=='video_frame'])

    async def test_audio_offset_uses_actual_stream_start(self):
        from materials.extractors.audio import AudioExtractor
        from materials.timeline import Timeline,Segment
        async def transcribe(data,mime,duration_ms,validate):
            return Timeline(duration_ms,(Segment('t','voice over',100,300),),method='fixture')
        provider=SimpleNamespace(identity='fixture',transcribe=AsyncMock(side_effect=transcribe))
        bundle=await VideoExtractor(max_frames=3,image=ImageExtractor(ocr_enabled=False),audio=AudioExtractor(transcriber=provider)).extract_authorized('v',1,video(offset=True),'video/mp4',validate=AsyncMock())
        root=next(b for b in bundle.blocks if b.metadata.get('role')=='audio_timeline')
        turn=next(b for b in bundle.blocks if b.metadata.get('role')=='speaker_turn')
        self.assertGreater(root.locator.start_ms,0); self.assertEqual(root.locator.start_ms+100,turn.locator.start_ms)
        self.assertIsNone(turn.metadata['speaker'])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class VideoPersistenceTests(unittest.IsolatedAsyncioTestCase):
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
        self.asset=await self.service.ingest(video(),'video.mp4',self.actor,'video','video')
        self.extractor=VideoExtractor(max_frames=6,image=ImageExtractor(ocr_enabled=False))
        self.eid,self.bundle=await self.service.extract(self.asset['id'],self.actor,self.extractor)
    async def asyncTearDown(self): self.temp.cleanup(); await self.db.__aexit__(None,None,None)
    async def test_immutable_refinement_replay_and_erasure(self):
        id,bundle=await self.service.observe_video_interval(self.asset['id'],self.actor,1350,1500,extractor=self.extractor)
        self.assertNotEqual(self.eid,id)
        frame=next(b for b in bundle.blocks if b.metadata.get('role')=='video_frame')
        ref=EvidenceRef(self.asset['id'],1,id,frame.block_id,frame.locator)
        value=await self.service.video_frame(ref,self.actor,extractor=self.extractor)
        self.assertEqual(frame.locator.start_ms,value['timestamp_ms'])
        with self.assertRaises(MaterialError): await self.service.video_frame(ref,replace(self.actor,scope=replace(self.actor.scope,topic_id=5)),extractor=self.extractor)
        await self.lifecycle.forget(self.asset['id'],self.actor)
        with self.assertRaises(MaterialError): await self.service.video_frame(ref,self.actor,extractor=self.extractor)
    async def test_forget_during_decoder_blocks_visual_provider(self):
        import materials.extractors.video as module
        original=module.run_worker
        async def erase(*args,**kwargs):
            value=await original(*args,**kwargs); await self.lifecycle.forget(self.asset['id'],self.actor); return value
        analyzer=SimpleNamespace(identity='mock',observe=AsyncMock())
        with patch.object(module,'run_worker',side_effect=erase):
            with self.assertRaises(MaterialError): await self.service.extract(self.asset['id'],self.actor,VideoExtractor(max_frames=3,image=ImageExtractor(ocr_enabled=False,analyzer=analyzer)))
        analyzer.observe.assert_not_awaited()
    async def test_storyboard_mock_delivery_and_scope(self):
        from bot.video_commands import _run
        from materials.runtime import MaterialText,MaterialUse
        source=SimpleNamespace(video=SimpleNamespace(file_id='fixture',file_size=100,file_name='video.mp4',mime_type='video/mp4'))
        message=SimpleNamespace(reply_to_message=source,text='/storyboard',chat_id=-100,reply_text=AsyncMock(),reply_photo=AsyncMock())
        material=MaterialText('video',MaterialUse(self.asset['id'],self.actor,1,self.asset['generation'],self.service))
        with patch('bot.video_commands.enabled',return_value=True),patch('bot.video_commands.capture_document',new=AsyncMock(return_value=material)),patch('bot.video_commands.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.video_commands.service_for_bot',new=AsyncMock(return_value=self.service)),patch('materials.extractors.documents.configured_extractor',return_value=self.extractor):
            await _run(SimpleNamespace(effective_message=message),SimpleNamespace(),False)
        message.reply_photo.assert_awaited_once(); self.assertGreater(message.reply_photo.call_args.args[0].getbuffer().nbytes,100)


class PublicFetchTests(unittest.IsolatedAsyncioTestCase):
    def test_ip_credentials_and_ipv4_mapped_private_are_denied(self):
        from utils.public_fetch import validate_url
        for url in ('http://127.0.0.1/video','http://[::ffff:127.0.0.1]/','https://user:pass@example.com/','http://example.com:8080/'):
            with self.assertRaises(MaterialError,msg=url): validate_url(url)
    async def test_dns_mixed_public_private_and_actual_peer_denied(self):
        import aiohttp
        from utils.public_fetch import PublicResolver,PublicConnector
        info=lambda ip:(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',new=AsyncMock(return_value=[info('8.8.8.8'),info('127.0.0.1')])):
            with self.assertRaises(MaterialError): await PublicResolver().resolve('example.com',443)
        connector=PublicConnector(); transport=Mock(); transport.get_extra_info.return_value=('127.0.0.1',443)
        try:
            with patch.object(aiohttp.TCPConnector,'_wrap_create_connection',new=AsyncMock(return_value=(transport,Mock()))):
                with self.assertRaisesRegex(MaterialError,'peer'): await connector._wrap_create_connection(addr_infos=[info('8.8.8.8')])
            transport.close.assert_called_once()
        finally: await connector.close()
    async def test_redirect_to_private_and_stream_overrun_are_denied(self):
        from utils.public_fetch import fetch_public
        class Content:
            async def iter_chunked(self,n): yield b'12345'; yield b'67890'
        class Response:
            status=200; headers={'Content-Type':'video/mp4'}; content=Content()
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        class Session:
            def __init__(self,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def get(self,*args,**kwargs): return Response()
        with self.assertRaisesRegex(MaterialError,'byte_budget'): await fetch_public('https://example.com/video',max_bytes=5,session_factory=Session)
        Response.status=302; Response.headers={'Location':'http://127.0.0.1/secret'}
        with self.assertRaisesRegex(MaterialError,'url_denied'): await fetch_public('https://example.com/video',session_factory=Session)
