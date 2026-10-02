import asyncio
import base64
from dataclasses import asdict, replace
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from materials.extractors.basic import render_text
from materials.extractors.documents import DocumentExtractor, DOCX, configured_extractor
from materials.extractors.isolation import WorkerLimits, run_worker
from materials.types import ExtractionBundle, MaterialError, Locator
from tests.materials.document_fixtures import scanned_pdf, structured_pdf, rich_docx, damaged_page_pdf, conflicting_text_layer_pdf


class AffineGeometryTests(unittest.TestCase):
    def test_ocr_caps_native_threads_before_processing(self):
        import cv2
        from materials.extractors.ocr import OCR
        previous=cv2.getNumThreads(); observed=[]
        def probe(*args,**kwargs):
            observed.append(cv2.getNumThreads())
            raise MaterialError('stop_thread_probe')
        try:
            cv2.setNumThreads(2)
            with Image.new('RGB',(20,20),'white') as image, patch.object(OCR,'_call',side_effect=probe):
                with self.assertRaisesRegex(MaterialError,'stop_thread_probe'):
                    OCR().read(image)
            self.assertTrue(observed)
            self.assertEqual({1},set(observed))
        finally:
            cv2.setNumThreads(previous)

    def test_scalar_composition_and_inverse_match_matrix_reference(self):
        import math
        import numpy as np
        from materials.extractors.ocr import compose_affine,inverse_affine
        matrix=lambda t: np.array([t[:3],t[3:],(0,0,1)],dtype=float)
        for angle in (0,3,-3,90,180,270):
            radians=math.radians(angle); cosine=math.cos(radians); sine=math.sin(radians)
            transform=(cosine,-sine,320,sine,cosine,-117)
            right=(1.2,0,37,0,.8,29)
            combined=compose_affine(transform,right)
            np.testing.assert_allclose(matrix(combined),matrix(transform) @ matrix(right),atol=1e-12)
            inverse=inverse_affine(combined)
            np.testing.assert_allclose(matrix(inverse),np.linalg.inv(matrix(combined)),atol=1e-12)
            np.testing.assert_allclose(matrix(compose_affine(inverse,combined)),np.eye(3),atol=1e-12)
            for x,y in ((0,0),(480,0),(480,270),(0,270),(123.25,56.75)):
                a,b,c,d,e,f=combined; xx,yy=a*x+b*y+c,d*x+e*y+f
                a,b,c,d,e,f=inverse
                self.assertAlmostEqual(x,a*xx+b*yy+c,places=10)
                self.assertAlmostEqual(y,d*xx+e*yy+f,places=10)

    def test_singular_affine_is_rejected(self):
        from materials.extractors.ocr import inverse_affine
        with self.assertRaisesRegex(MaterialError,'ocr_geometry_invalid'):
            inverse_affine((1,2,0,2,4,0))


class StructuredDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor=DocumentExtractor()
        async def prepare():
            return [await cls.extractor.extract_async(name,1,data,mime) for name,data,mime in
                [('layout',structured_pdf(),'application/pdf'),('scan',scanned_pdf(table=True),'application/pdf'),
                 ('word',rich_docx(),DOCX),('heldout',scanned_pdf(heldout=True),'application/pdf'),
                 ('rotated',scanned_pdf(angle=90),'application/pdf'),('inverted',scanned_pdf(angle=180),'application/pdf'),
                 ('skew',scanned_pdf(angle=3),'application/pdf')]]
        cls.layout,cls.scan,cls.word,cls.heldout,cls.rotated,cls.inverted,cls.skew=asyncio.run(prepare())

    def test_columns_keep_column_reading_order(self):
        text=[b.text for b in self.layout.blocks if 'колонка' in b.text]
        self.assertEqual(['Левая колонка 1','Левая колонка 2','Левая колонка 3',
                          'Правая колонка 1','Правая колонка 2','Правая колонка 3'],text)

    def test_headings_lists_margins_and_captions(self):
        roles={b.metadata.get('role') for b in self.layout.blocks}
        self.assertTrue({'heading','list_item','header','footer','caption','embedded_image'}<=roles)
        images=[b for b in self.layout.blocks if b.kind=='image']
        self.assertEqual(2,len(images))
        for image in images:
            caption=next(b for b in self.layout.blocks if b.block_id==image.metadata['caption_id'])
            self.assertEqual('caption',caption.metadata['role'])
            self.assertEqual(image.locator.page,caption.locator.page)

    def test_multipage_tables_keep_originals_and_continuation(self):
        tables=[b for b in self.layout.blocks if b.kind=='table']
        self.assertEqual(2,len(tables))
        self.assertEqual(tables[0].block_id,tables[1].metadata['continues'])
        cell=next(c for c in tables[0].metadata['cells'] if c['text']=='1200')
        self.assertEqual((1,1),(cell['row'],cell['column']))
        self.assertTrue(.5<cell['bbox'][0]<cell['bbox'][2]<1)
        self.assertEqual(1,tables[0].locator.page)

    def test_mixed_page_keeps_native_and_ocr_separate(self):
        native=[b for b in self.layout.blocks if b.metadata.get('method')=='native_glyphs']
        ocr=[b for b in self.layout.blocks if b.metadata.get('method')=='tesseract']
        self.assertTrue(native and ocr)
        self.assertTrue(any('Предложение отменено' in b.text and b.locator.page==2 for b in ocr))
        self.assertEqual(2,sum(b.text=='Отчёт Арти' for b in self.layout.blocks))
        self.assertTrue(all(b.quality!='verified' for b in ocr))

    def test_real_russian_ocr_and_grid_cells(self):
        text='\n'.join(b.text for b in self.scan.blocks)
        self.assertTrue(all(s in text for s in ('Аренда: 1200','Доставка: 300','Итого: 1500','ещё не принято')))
        table=next(b for b in self.scan.blocks if b.kind=='table')
        self.assertEqual([['Статья','Рубли'],['Аренда','1200'],['Доставка','300']],table.metadata['rows'])
        self.assertEqual('raster_grid',table.metadata['method'])
        self.assertTrue(all(c['bbox'] for c in table.metadata['cells']))

    def test_heldout_numbers_are_not_development_answers(self):
        text='\n'.join(b.text for b in self.heldout.blocks)
        self.assertTrue(all(s in text for s in ('2750','840','3590','отменено','18 ноября')))
        self.assertNotIn('1200',text)

    def test_orientation_and_skew_preserve_line_order(self):
        for bundle,rotation in ((self.rotated,90),(self.inverted,180),(self.skew,0)):
            lines=[b for b in bundle.blocks if b.metadata.get('method')=='tesseract']
            self.assertIn('Арти',lines[0].text)
            self.assertTrue(any('Аренда: 1200' in b.text for b in lines))
            self.assertTrue(any('Доставка: 300' in b.text for b in lines))
            self.assertEqual(rotation,lines[0].metadata['ocr']['rotation_clockwise'])
            for block in lines:
                for word in block.metadata['words']:
                    x0,y0,x1,y1=word['bbox']
                    self.assertTrue(0<=x0<x1<=1 and 0<=y0<y1<=1)

    def test_docx_styles_merged_cells_nested_tables_and_unknown_pages(self):
        roles={b.metadata.get('role') for b in self.word.blocks}
        self.assertTrue({'heading','list_item','caption','header','footer'}<=roles)
        tables=[b for b in self.word.blocks if b.kind=='table']
        self.assertEqual(2,len(tables))
        self.assertEqual(tables[0].block_id,tables[1].parent_id)
        self.assertEqual(3,tables[0].metadata['cells'][0]['colspan'])
        self.assertTrue(any(c['merge']=='continue' for c in tables[0].metadata['cells']))
        self.assertTrue(all(b.locator.page is None for b in self.word.blocks))
        self.assertIn('docx_pagination_unknown',self.word.manifest.limitations)
        self.assertEqual('body_node',self.word.manifest.unit_kind)

    def test_roundtrip_and_projection_show_evidence_and_coverage(self):
        self.assertEqual(self.layout,ExtractionBundle.from_dict(self.layout.to_dict()))
        rendered=render_text(self.scan)
        self.assertIn('region:',rendered); self.assertIn('ocr_printed_text_only',rendered)
        self.assertIn('visual content uninterpreted',rendered)

    def test_ocr_disabled_scan_is_partial_not_empty_success(self):
        b=asyncio.run(DocumentExtractor(ocr_enabled=False).extract_async('off',1,scanned_pdf(),'application/pdf'))
        self.assertEqual('partial',b.manifest.coverage)
        self.assertEqual(0,b.manifest.processed_units)
        self.assertIn('ocr_disabled:1',b.manifest.limitations)

    def test_budget_preserves_partial_document_and_units(self):
        b=asyncio.run(DocumentExtractor(max_units=1).extract_async('budget',1,structured_pdf(),'application/pdf'))
        self.assertEqual((2,1,'partial'),(b.manifest.total_units,b.manifest.processed_units,b.manifest.coverage))
        self.assertIn('page_budget_reached',b.manifest.limitations)
        b=asyncio.run(DocumentExtractor(max_chars=30).extract_async('budget',1,structured_pdf(),'application/pdf'))
        self.assertEqual('partial',b.manifest.coverage)
        self.assertTrue(any(s.startswith('content_budget') for s in b.manifest.limitations))

    def test_recovered_and_encrypted_documents(self):
        data=structured_pdf(); recovered=data[:data.rfind(b'%%EOF')]
        b=asyncio.run(self.extractor.extract_async('recovered',1,recovered,'application/pdf'))
        self.assertIn('pdf_recovered_missing_eof',b.manifest.limitations)
        self.assertIn('1200',' '.join(b.text for b in b.blocks))
        from pypdf import PdfReader, PdfWriter
        writer=PdfWriter(); writer.append_pages_from_reader(PdfReader(BytesIO(data))); writer.encrypt('private')
        out=BytesIO(); writer.write(out)
        with self.assertRaisesRegex(MaterialError,'encrypted_document'):
            asyncio.run(self.extractor.extract_async('locked',1,out.getvalue(),'application/pdf'))

    def test_broken_page_does_not_discard_later_page(self):
        b=asyncio.run(self.extractor.extract_async('damaged',1,damaged_page_pdf(),'application/pdf'))
        self.assertEqual((2,1,'partial'),(b.manifest.total_units,b.manifest.processed_units,b.manifest.coverage))
        self.assertTrue(any(s.endswith(':1') for s in b.manifest.limitations))
        self.assertTrue(any('840' in block.text and block.locator.page==2 for block in b.blocks))

    def test_soft_deadline_preserves_completed_pages(self):
        # Controlled clock tests the boundary without racing wall-time or slowing CI.
        from tests.materials.fixtures import pdf
        extractor=DocumentExtractor(ocr_enabled=False)
        with patch('materials.extractors.documents.time.monotonic',side_effect=[0,0,100]):
            b=extractor.extract('deadline',1,pdf(),'application/pdf')
        self.assertEqual((2,1,'partial'),(b.manifest.total_units,b.manifest.processed_units,b.manifest.coverage))
        self.assertTrue(any('1200' in block.text for block in b.blocks))
        self.assertIn('parser_soft_deadline_reached',b.manifest.limitations)

    def test_stale_hidden_text_layer_keeps_conflicting_readings(self):
        b=asyncio.run(self.extractor.extract_async('conflict',1,conflicting_text_layer_pdf(),'application/pdf'))
        alternatives=[block for block in b.blocks if block.metadata.get('role')=='disagreement']
        self.assertTrue(any(block.metadata['native_text']=='9999' and block.metadata['ocr_text']=='1200' for block in alternatives))
        self.assertTrue(all(block.quality=='uncertain' for block in alternatives))
        self.assertTrue(any('9999' in block.text and block.metadata.get('method')=='native_glyphs' for block in b.blocks))

    def test_native_cell_region_can_be_reread(self):
        table=next(b for b in self.layout.blocks if b.kind=='table')
        cell=next(c for c in table.metadata['cells'] if c['text']=='1200')
        locator=Locator('region',page=1,bbox=tuple(cell['bbox']))
        result=asyncio.run(self.extractor.region_async(structured_pdf(),'application/pdf',asdict(locator),reread=True))
        self.assertIn('1200',' '.join(w['text'] for w in result['observation']['words']))
        for word in result['observation']['words']:
            self.assertTrue(cell['bbox'][0]<=word['bbox'][0]<word['bbox'][2]<=cell['bbox'][2])
        image=Image.open(BytesIO(base64.b64decode(result['image_base64'])))
        self.assertLess(image.width*image.height,600*800*4**2)

    def test_rotated_word_locator_points_to_the_original_crop(self):
        block=next(b for b in self.rotated.blocks if 'Аренда: 1200' in b.text)
        result=asyncio.run(self.extractor.region_async(scanned_pdf(angle=90),'application/pdf',asdict(block.locator),reread=True,
            orientation_hint=block.metadata['ocr']['rotation_clockwise']))
        self.assertIn('1200',' '.join(w['text'] for w in result['observation']['words']))
        self.assertEqual(90,result['observation']['rotation_clockwise'])

    def test_docx_image_is_retrievable_from_its_relationship(self):
        image=next(b for b in self.word.blocks if b.kind=='image')
        result=asyncio.run(self.extractor.region_async(rich_docx(),DOCX,asdict(image.locator),resource=image.metadata['resource']))
        pixels=Image.open(BytesIO(base64.b64decode(result['image_base64'])))
        self.assertEqual((240,120),pixels.size)
        self.assertEqual((0,0,255),pixels.getpixel((30,30)))

    def test_rollback_changes_extraction_namespace(self):
        with patch.dict(os.environ,{'ARTI_DOCUMENTS_ENABLED':'0'}):
            self.assertNotEqual(self.extractor.cache_version,configured_extractor().cache_version)
            native=asyncio.run(configured_extractor().extract_async('rollback',1,rich_docx(),DOCX))
            self.assertIn('2750',render_text(native))
            self.assertTrue(native.extractor.startswith('isolated-native-document-1'))
        self.assertNotEqual(self.extractor.cache_version,DocumentExtractor(ocr_enabled=False).cache_version)


class ParserIsolationTests(unittest.IsolatedAsyncioTestCase):
    worker=Path(__file__).parents[1]/'support'/'parser_worker.py'

    async def test_secrets_and_provider_config_are_not_inherited(self):
        with patch.dict(os.environ,{'ARTI_TEST_SECRET':'synthetic-do-not-inherit'}):
            result=await run_worker(dict(operation='environment'),b'x',WorkerLimits(),worker_path=self.worker)
        self.assertFalse(result['secret_present']); self.assertFalse(result['provider_imported'])

    async def test_memory_limit_is_enforced_by_os(self):
        result=await run_worker(dict(operation='memory'),b'x',WorkerLimits(memory_mb=128),worker_path=self.worker)
        self.assertTrue(result['limited'])

    async def test_disk_limit_returns_safe_error(self):
        with self.assertRaisesRegex(MaterialError,'parser_disk_budget'):
            await run_worker(dict(operation='disk'),b'x',WorkerLimits(disk_mb=8),worker_path=self.worker)

    async def test_disk_limit_is_classified_when_worker_exits_between_polls(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        async def exited(*args,**kwargs):
            # Model the actual EFBIG path: one file reaches the OS cap and
            # the worker exits before the parent's first polling iteration.
            (Path(args[-1])/'overflow').write_bytes(b'x'*(8*1024**2))
            return SimpleNamespace(returncode=1)
        with patch('materials.extractors.isolation.asyncio.create_subprocess_exec',new=AsyncMock(side_effect=exited)):
            with self.assertRaisesRegex(MaterialError,'parser_disk_budget'):
                await run_worker(dict(operation='disk'),b'x',WorkerLimits(disk_mb=8),worker_path=self.worker)

    async def test_file_size_signal_is_classified_without_a_result_file(self):
        import signal
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        if not hasattr(signal,'SIGXFSZ'): self.skipTest('POSIX file-size signal')
        with patch('materials.extractors.isolation.asyncio.create_subprocess_exec',new=AsyncMock(return_value=SimpleNamespace(returncode=-signal.SIGXFSZ))):
            with self.assertRaisesRegex(MaterialError,'parser_disk_budget'):
                await run_worker(dict(operation='disk'),b'x',WorkerLimits(disk_mb=8),worker_path=self.worker)

    async def test_timeout_kills_worker_and_child(self):
        import psutil
        with tempfile.TemporaryDirectory() as root:
            pid_path=Path(root)/'child.pid'
            with self.assertRaisesRegex(MaterialError,'parser_timeout'):
                await run_worker(dict(operation='sleep',pid_path=str(pid_path)),b'x',WorkerLimits(wall_seconds=1.5),worker_path=self.worker)
            self.assertTrue(pid_path.exists())
            pid=int(pid_path.read_text())
            await asyncio.sleep(.2)
            self.assertFalse(psutil.pid_exists(pid))

    async def test_cancellation_kills_child_and_cleans_temporary_files(self):
        import psutil
        with tempfile.TemporaryDirectory() as root:
            pid_path=Path(root)/'child.pid'
            task=asyncio.create_task(run_worker(dict(operation='sleep',pid_path=str(pid_path)),b'x',WorkerLimits(),worker_path=self.worker))
            for _ in range(30):
                if pid_path.exists(): break
                await asyncio.sleep(.1)
            self.assertTrue(pid_path.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.sleep(.2)
            self.assertFalse(psutil.pid_exists(int(pid_path.read_text())))
