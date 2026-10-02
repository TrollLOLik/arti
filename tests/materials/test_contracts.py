import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ai.capabilities import CapabilityRegistry, ModelEndpoint, registry_for
from ai.providers.contracts import GenerationRequest, ImageInput
from materials.extractors.basic import BasicExtractor, render_text
from materials.storage import LocalBlobStore
from materials.types import AccessContext, ContentBlock, ExtractionBundle, ExtractionManifest, Locator, MaterialError, MaterialScope, block_id
from materials.validation import inspect_bytes
from tests.materials.fixtures import docx, pdf, png


class ContractTests(unittest.TestCase):
    def test_realm_separates_users_topics_scenes(self):
        scope = MaterialScope('arti', 1, -1, 'private')
        a = AccessContext(scope, 1, 'user:1')
        self.assertNotEqual(a.realm, AccessContext(scope, 2, 'user:2').realm)
        group = MaterialScope('arti', -1, 4, 'supergroup')
        self.assertEqual(AccessContext(group, 1, 'user:1').realm, AccessContext(group, 2, 'user:2').realm)
        self.assertNotEqual(group.key, replace(group, topic_id=5).key)
        self.assertNotEqual(group.key, replace(group, mode='rp', scene_id='s1').key)

    def test_unknown_sender_and_scope_fail_closed(self):
        for arguments in [('arti', -1, -1, 'supergroup'), ('arti', 1, -1, 'unknown')]:
            with self.assertRaises(MaterialError): MaterialScope(*arguments)
        with self.assertRaises(MaterialError): AccessContext(MaterialScope('arti', 1, -1, 'private'), None, 'chat:1')
        group = AccessContext(MaterialScope('arti', -1, 0, 'group'), None, 'chat:-42')
        self.assertIsNone(group.user_id)

    def test_bad_locators_do_not_become_citations(self):
        for value in [dict(kind='page',page=0),dict(kind='region',bbox=(0,0,2,1)),dict(kind='time',start_ms=20,end_ms=10),dict(kind='cell',sheet='Sheet',cell='A0'),dict(kind='document',page=1)]:
            with self.assertRaises(MaterialError): Locator(**value)

    def test_bundle_roundtrip_parent_graph_and_explicit_coverage(self):
        locator = Locator('region', page=2, bbox=(0.1,0.2,0.8,0.9))
        root = ContentBlock('root','page',Locator('page',page=2))
        child = ContentBlock('child','text',locator,'1200',parent_id='root')
        bundle = ExtractionBundle('a',1,'ocr1',(root,child),ExtractionManifest(3,1,'partial',('budget',)))
        self.assertEqual(bundle, ExtractionBundle.from_dict(bundle.to_dict()))
        with self.assertRaises(MaterialError): replace(bundle,blocks=(replace(root,parent_id='child'),child))
        with self.assertRaises(MaterialError): ExtractionManifest(3,1,'complete')

    def test_mime_comes_from_image_bytes(self):
        image = ImageInput.from_base64(base64.b64encode(png()).decode())
        self.assertEqual('image/png',image.mime)
        self.assertTrue(image.url().startswith('data:image/png;base64,'))
        with self.assertRaises(MaterialError): ImageInput.from_base64('data:image/jpeg;base64,'+base64.b64encode(png()).decode())
        with self.assertRaises(MaterialError): ImageInput.from_base64('not base64!!')

    def test_archive_bomb_and_corrupt_file_are_rejected(self):
        from io import BytesIO
        import zipfile
        data = BytesIO()
        with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml',b'0'*1_000_000)
        with self.assertRaises(MaterialError): inspect_bytes(data.getvalue(),'x.docx')
        with self.assertRaises(MaterialError): inspect_bytes(b'\x89PNG\r\n\x1a\n','x.png')

    def test_blob_path_escape_and_integrity(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as root:
            store = LocalBlobStore(root)
            key = store.put(b'abc')
            self.assertEqual(b'abc',store.read(key,sha256(b'abc').hexdigest(),10))
            with self.assertRaises(MaterialError): store.path('../../.env')
            store.path(key).write_bytes(b'abd')
            with self.assertRaises(MaterialError): store.read(key,sha256(b'abc').hexdigest(),10)

    def test_native_document_table_and_page_sources(self):
        extractor = BasicExtractor()
        word = extractor.extract('word',1,docx(),inspect_bytes(docx(),'budget.docx'))
        self.assertIn('1200',render_text(word))
        self.assertIn('embedded_images_not_extracted',word.manifest.limitations)
        self.assertEqual('table',word.blocks[1].kind)
        pages = extractor.extract('pdf',1,pdf(),'application/pdf')
        self.assertEqual([1,2],[b.locator.page for b in pages.blocks])
        self.assertIn('300',pages.blocks[1].text)

    def test_extraction_budget_is_not_silent_truncation(self):
        bundle = BasicExtractor(max_units=1).extract('a',1,b'first\nsecond','text/plain')
        self.assertEqual('partial',bundle.manifest.coverage)
        self.assertEqual(2,bundle.manifest.total_units)
        self.assertIn('extraction_budget_reached',render_text(bundle))

    def test_route_combines_vision_and_search_without_name_heuristics(self):
        vision = ModelEndpoint('qwen-new','openai','proxy',frozenset({'text','image'}))
        grounded = ModelEndpoint('grounded','gemini','google',frozenset({'text','image'}),frozenset({'search'}))
        registry = CapabilityRegistry([vision,grounded])
        request = GenerationRequest('compare',images=(ImageInput(png(),'image/png'),))
        self.assertEqual(vision,registry.route('qwen-new',request).endpoint)
        self.assertEqual(grounded,registry.route('qwen-new',replace(request,web_search=True)).endpoint)
        content = request.openai_messages()[1]['content']
        self.assertEqual('image_url',content[1]['type'])

    def test_stale_or_unavailable_endpoint_not_used(self):
        old = ModelEndpoint('old','openai','proxy',observed_at=datetime.now(timezone.utc)-timedelta(days=8))
        with self.assertRaises(MaterialError): CapabilityRegistry([old]).route('old',GenerationRequest('hi'))
        with self.assertRaises(MaterialError): CapabilityRegistry([replace(old,available=False)]).route('old',GenerationRequest('hi'))

    def test_unknown_endpoint_is_not_trusted_by_model_name(self):
        with patch.dict('os.environ',{'ARTI_CAPABILITY_MANIFEST':''}):
            registry = registry_for('qwen-future','proxy')
        image = GenerationRequest('hi',images=(ImageInput(png(),'image/png'),))
        decision = registry.route('qwen-future',image,provider='openai',endpoint='proxy')
        self.assertEqual('gemini',decision.endpoint.provider)

    def test_manifest_does_not_authorize_arbitrary_destination(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root)/'manifest.json'
            manifest.write_text(json.dumps({'version':1,'endpoints':[dict(model='private',provider='openai',endpoint='https://other.example',inputs=['text','image'],evidence='configured')]}))
            with patch.dict('os.environ',{'ARTI_CAPABILITY_MANIFEST':str(manifest)}):
                registry=registry_for('private','proxy')
            self.assertFalse(any(c.endpoint=='https://other.example' for c in registry.endpoints))

    def test_media_budget(self):
        with self.assertRaises(MaterialError): GenerationRequest('hi',images=tuple(ImageInput(png(),'image/png') for _ in range(9)))
