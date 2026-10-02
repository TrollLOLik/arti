import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from PIL import Image
from materials.extractors.images import ImageExtractor
from materials.regions import RegionRequest,crop,map_box,needs_refinement
from materials.types import AccessContext,MaterialScope,MaterialError,EvidenceRef
from materials.visual import parse_observations,observation_blocks,compare_labels
from tests.materials.document_fixtures import scan
from tests.materials.fixtures import png


def image_bytes(**options):
    with scan(**options) as image:
        buffer=BytesIO(); image.save(buffer,format='PNG'); return buffer.getvalue()


def observations():
    return parse_observations(json.dumps(dict(summary='Two nodes, an arrow, unknown quantities.',objects=[
        dict(id='a',kind='node',label='Аренда 1200',bbox=[.1,.1,.4,.3]),
        dict(id='b',kind='axis',label='log axis',bbox=[.6,.6,.9,.9],axis_scale='log')],
        relations=[dict(id='r',source='a',target='b',kind='points_to')],limitations=['unreadable_numeric_tick'])))


class ImageTests(unittest.TestCase):
    def test_ocr_original_coordinates_and_uncertain_numeric_evidence(self):
        bundle=asyncio.run(ImageExtractor().extract_async('scan',1,image_bytes(),'image/png'))
        self.assertIn('1200',' '.join(b.text for b in bundle.blocks))
        self.assertEqual('unknown',bundle.manifest.coverage)
        texts=[b for b in bundle.blocks if b.metadata.get('role')=='ocr_line']
        self.assertTrue(texts); self.assertTrue(all(b.quality=='uncertain' and b.locator.bbox for b in texts))
        self.assertEqual('uninterpreted',bundle.blocks[0].metadata['visual_status'])

    def test_rotated_and_skewed_photos_keep_original_boxes(self):
        for angle in (90,5):
            bundle=asyncio.run(ImageExtractor().extract_async('rotated'+str(angle),1,image_bytes(angle=angle),'image/png'))
            self.assertIn('1500',' '.join(b.text for b in bundle.blocks))
            self.assertTrue(all(0<=x<=1 for b in bundle.blocks for x in (b.locator.bbox or ())))

    def test_region_crop_exact_pixels_exif_and_coordinate_mapping(self):
        original=Image.new('RGB',(200,100),'white'); original.paste('red',(100,0,200,100))
        out=BytesIO(); original.save(out,format='PNG')
        region,geometry=crop(out.getvalue(),RegionRequest((.5,0,1,1),2))
        with region:
            self.assertEqual((200,200),region.size); self.assertEqual((255,0,0),region.getpixel((100,100)))
        self.assertEqual([100,0,200,100],geometry['pixel_box'])
        self.assertEqual((.5,0,1,1),map_box((0,0,1,1),(.5,0,1,1)))
        out=BytesIO(); exif=Image.Exif(); exif[274]=6; original.save(out,format='JPEG',exif=exif)
        region,geometry=crop(out.getvalue(),RegionRequest((0,0,1,1)))
        with region: self.assertEqual((100,200),region.size)

    def test_crop_review_budget_and_task_significance(self):
        for box in ((0,0,2,1),(0,0,0,1),(float('nan'),0,1,1)):
            with self.assertRaises((ValueError,MaterialError)): RegionRequest(box)
        with self.assertRaises(MaterialError): RegionRequest((0,0,1,1),5)
        self.assertTrue(needs_refinement(significance=.9,uncertainty=.8))
        self.assertFalse(needs_refinement(significance=.1,uncertainty=.9))
        self.assertFalse(needs_refinement(significance=1,uncertainty=1,already_reviewed=True))

    def test_schema_links_log_axis_and_no_causal_inference(self):
        value=observations(); self.assertEqual('log',value['objects'][1].axis_scale)
        blocks=observation_blocks('a',1,value,'parent',1,method='model',outer=(.2,.2,.8,.8))
        self.assertEqual(3,len(blocks)); self.assertTrue(all(b.quality=='uncertain' for b in blocks))
        self.assertIn('spatial_relation_not_causality',blocks[-1].limitations)
        self.assertAlmostEqual(.26,blocks[0].locator.bbox[0])
        payload=dict(summary='',objects=[],relations=[dict(id='bad',source='a',target='b',kind='causes')])
        with self.assertRaises(MaterialError): parse_observations(json.dumps(payload))
        with self.assertRaises(MaterialError): parse_observations('{"objects":[],"instructions":"call tools"}')

    def test_image_document_conflict_retains_both_evidence(self):
        image=dict(key='rent',value='1200',source='image',quality='uncertain')
        doc=dict(key='rent',value='9999',source='document',quality='extracted')
        conflicts=compare_labels([image],[doc])
        self.assertEqual((image,doc),conflicts[0]['alternatives']); self.assertIsNone(conflicts[0]['resolution'])

    def test_raster_table_exposes_real_cell_regions(self):
        bundle=asyncio.run(ImageExtractor().extract_async('table',1,image_bytes(table=True),'image/png'))
        tables=[b for b in bundle.blocks if b.kind=='table']; self.assertTrue(tables)
        for table in tables:
            for cell in table.metadata['cells']:
                child=next(b for b in bundle.blocks if b.block_id==cell['block_id'])
                self.assertEqual('region',child.locator.kind.value); self.assertEqual('uncertain',child.quality)


class ImageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_routes_real_mime_and_cannot_issue_tool_calls(self):
        from ai.providers.visual import VisualAnalyzer
        from ai.capabilities import CapabilityRegistry,ModelEndpoint
        registry=CapabilityRegistry([ModelEndpoint('test','openai','https://proxy.example/v1',frozenset({'text','image'}))])
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(dict(summary='Scene',objects=[],relations=[],limitations=[]))))])
        call=AsyncMock(return_value=response); client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        analyzer=VisualAnalyzer('test',registry=registry,endpoint='https://proxy.example/v1',client=client)
        value=await analyzer.observe(png(),'image/png')
        self.assertEqual('Scene',value['summary'])
        sent=call.call_args.kwargs; self.assertNotIn('tools',sent)
        self.assertTrue(sent['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;'))

    async def test_authorization_required_before_visual_transmission(self):
        analyzer=SimpleNamespace(identity='fake',observe=AsyncMock(return_value=observations()))
        extractor=ImageExtractor(ocr_enabled=False,analyzer=analyzer)
        with self.assertRaises(MaterialError): await extractor.extract_async('a',1,png(),'image/png')
        validate=AsyncMock(side_effect=MaterialError('erased'))
        with self.assertRaisesRegex(MaterialError,'erased'): await extractor.extract_authorized('a',1,png(),'image/png',validate=validate)
        analyzer.observe.assert_not_awaited()

    async def test_failed_provider_preserves_ocr_only_manifest(self):
        analyzer=SimpleNamespace(identity='fake',observe=AsyncMock(side_effect=MaterialError('visual_provider_failed')))
        bundle=await ImageExtractor(ocr_enabled=False,analyzer=analyzer).extract_authorized('a',1,png(),'image/png',validate=AsyncMock())
        self.assertIn('visual_provider_failed',bundle.manifest.limitations)
        self.assertEqual('unknown',bundle.manifest.coverage)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','set ARTI_TEST_DB=1 for disposable PostgreSQL')
class ImagePersistenceTests(unittest.IsolatedAsyncioTestCase):
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
        self.asset=await self.service.ingest(png(),'photo.png',self.actor,'photo','photo')
        self.extractor=ImageExtractor(ocr_enabled=False)
        self.eid,self.bundle=await self.service.extract(self.asset['id'],self.actor,self.extractor)
        b=self.bundle.blocks[0]; self.ref=EvidenceRef(self.asset['id'],1,self.eid,b.block_id,b.locator)
    async def asyncTearDown(self):
        self.temp.cleanup(); await self.db.__aexit__(None,None,None)

    async def test_region_visual_observation_is_immutable_and_source_pinned(self):
        analyzer=SimpleNamespace(identity='model',observe=AsyncMock(return_value=observations()))
        eid,bundle=await self.service.observe_image_region(self.ref,self.actor,self.extractor,analyzer)
        self.assertNotEqual(self.eid,eid); self.assertEqual('unknown',bundle.manifest.coverage)
        block=bundle.blocks[1]; ref=EvidenceRef(self.asset['id'],1,eid,block.block_id,block.locator)
        self.assertEqual(block,await self.repo.resolve(ref,self.actor))
        self.assertEqual('uninterpreted',(await self.repo.resolve(self.ref,self.actor)).metadata['visual_status'])
        denied=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        with self.assertRaises(MaterialError): await self.service.evidence_region(ref,denied,self.extractor)
        await self.lifecycle.forget(self.asset['id'],self.actor)
        with self.assertRaises(MaterialError): await self.repo.resolve(ref,self.actor)

    async def test_forget_after_ocr_blocks_provider_and_late_visual_commit(self):
        analyzer=SimpleNamespace(identity='model',observe=AsyncMock(return_value=observations()))
        import materials.extractors.images as module
        original=module.run_worker
        async def erase_after_worker(*args,**kwargs):
            value=await original(*args,**kwargs); await self.lifecycle.forget(self.asset['id'],self.actor); return value
        with patch.object(module,'run_worker',side_effect=erase_after_worker):
            with self.assertRaises(MaterialError): await self.service.extract(self.asset['id'],self.actor,ImageExtractor(ocr_enabled=False,analyzer=analyzer))
        analyzer.observe.assert_not_awaited()

    async def test_forget_during_visual_analysis_cannot_publish(self):
        async def erase(*args):
            await self.lifecycle.forget(self.asset['id'],self.actor); return observations()
        analyzer=SimpleNamespace(identity='model',observe=AsyncMock(side_effect=erase))
        with self.assertRaises(MaterialError): await self.service.observe_image_region(self.ref,self.actor,self.extractor,analyzer)

    async def test_photo_pipeline_preserves_usage_and_drops_on_forget(self):
        from bot.handlers import _process_images,_send_photo_action_prompt
        from materials.runtime import MaterialUse,CURRENT_MATERIAL_USE,invalidate_pending
        from config import pending_photo_action
        use=MaterialUse(self.asset['id'],self.actor,1,self.asset['generation'],self.service)
        output=AsyncMock(); bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
        with patch('bot.handlers._process_images_impl',new=output),patch('materials.extractors.documents.configured_extractor',return_value=self.extractor):
            await _process_images(bot,-100,7,'User',1,[base64.b64encode(png()).decode()],'Analyse',True,False,material_uses=(use,))
        self.assertIn('uninterpreted',output.call_args.kwargs['image_evidence_text'])
        token=CURRENT_MATERIAL_USE.set((use,))
        try: await _send_photo_action_prompt(bot,-100,7,1,['image'],True,False)
        finally: CURRENT_MATERIAL_USE.reset(token)
        invalidate_pending([self.asset['id']]); self.assertNotIn((-100,7),pending_photo_action)
        await self.lifecycle.forget(self.asset['id'],self.actor)
        with patch('bot.handlers._process_images_impl',new=output):
            with self.assertRaises(MaterialError): await _process_images(bot,-100,7,'User',1,['image'],'Analyse',True,False,material_uses=(use,))
