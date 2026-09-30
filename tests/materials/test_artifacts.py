import unittest,os,tempfile
from copy import deepcopy
from io import BytesIO
from artifacts.spec import ArtifactSpec,choose_format
from artifacts.patches import patch
from artifacts.export import export
from artifacts.styles import STYLES
from materials.types import MaterialError,AccessContext,MaterialScope

def fixture():
    return dict(contract='artifact-1',title='Длинная кириллица: проверяемая карта проекта',format='cards',elements=[dict(id='e1',label='Первый элемент',text='Неизвестное значение не заменяется нулём.',status='proposed')],relations=[],style={})

class ArtifactPureTests(unittest.TestCase):
    def test_chronology_date_evidence_and_reverse_sequence(self):
        v=fixture(); v['format']='timeline'
        v['elements']=[dict(id='first',label='First',text='2026-01-01 event',order=1,when='2026-01-01',status='proposed'),dict(id='second',label='Second',order=2,when='2026-02-01',status='proposed')]
        ArtifactSpec(v)
        v['elements'][1]['when']='2025-02-01'
        with self.assertRaises(MaterialError): ArtifactSpec(v)
        v['elements'][1]['when']='2026-02-01'; v['relations']=[dict(id='r1',kind='sequence',**{'from':'second','to':'first'})]
        with self.assertRaises(MaterialError): ArtifactSpec(v)
    def test_strict_units_cause_scale_and_missing(self):
        v=fixture(); v['elements'][0]['quantity']=dict(value='1',unit='')
        with self.assertRaises(MaterialError): ArtifactSpec(v)
        v=fixture(); v['elements'].append(dict(id='e2',label='Другой',status='unknown'))
        v['relations']=[dict(id='r1',kind='causes',**{'from':'e1','to':'e2'})]
        with self.assertRaises(MaterialError): ArtifactSpec(v)
        v['relations'][0]['kind']='correlates'; ArtifactSpec(v)
        self.assertEqual('statistical',choose_format('numbers'))
    def test_style_patch_factual_hash_stable_and_targeted(self):
        spec=ArtifactSpec(fixture()); result,diff=patch(spec,[dict(op='style',value=STYLES['night'].to_dict())])
        self.assertEqual(spec.factual_hash,result.factual_hash); self.assertFalse(diff['content_changed'])
        with self.assertRaises(MaterialError): patch(spec,[dict(op='replace',id='absent',value={})])
    def test_exports_unicode_pagination_and_all_elements(self):
        from pypdf import PdfReader
        from PIL import Image
        import xml.etree.ElementTree as ET
        v=fixture(); v['elements']=[dict(id='e'+str(i),label='Значимый блок '+str(i),text=('Длинное русское объяснение с источниками и неопределённостью. '*20),status='proposed') for i in range(12)]
        outputs=export(ArtifactSpec(v)); reader=PdfReader(BytesIO(outputs['report.pdf']))
        self.assertGreater(len(reader.pages),3)
        text=' '.join(p.extract_text() for p in reader.pages)
        for i in range(12): self.assertIn('Значимый блок '+str(i),text)
        for name,data in outputs.items():
            if name.endswith('.png'): Image.open(BytesIO(data)).verify()
            if name.endswith('.svg'): ET.fromstring(data)
    def test_statistical_negative_tiny_and_interval_exact_exports(self):
        v=fixture(); v['format']='statistical'; v['axis']=dict(unit='RUB',scale='linear')
        v['elements']=[dict(id='e'+str(i),label='Наблюдение '+str(i),status='observed',quantity=dict(value=s,unit='RUB'),proof=dict(kind='dataset',dataset_id='source',address='A'+str(i+1))) for i,s in enumerate(['-0.000001','2.345','0'])]
        out=export(ArtifactSpec(v)); self.assertIn(b'-0.000001',out['page-1.svg'])
        v['axis']['scale']='log'
        with self.assertRaises(MaterialError): ArtifactSpec(v)

@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class ArtifactSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from projects.repository import ProjectRepository
        from artifacts.revisions import ArtifactRepository
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); await ensure_schema(self.pool)
        self.temp=tempfile.TemporaryDirectory(); self.materials=MaterialRepository(self.pool); self.service=MaterialService(self.materials,LocalBlobStore(self.temp.name))
        self.actor=AccessContext(MaterialScope('arti',55,-1,'private'),7,'user:7'); self.projects=ProjectRepository(self.materials); self.p=await self.projects.create(self.actor,'Studio'); self.repo=ArtifactRepository(self.materials)
        source=await self.service.ingest(b'User requests a proposed card','request.txt',self.actor,'request','request')
        extraction,bundle=await self.service.extract(source['id'],self.actor)
        from materials.types import EvidenceRef
        self.sources=[EvidenceRef(source['id'],1,extraction,bundle.blocks[0].block_id,bundle.blocks[0].locator)]
    async def asyncTearDown(self): self.temp.cleanup(); await self.db.__aexit__(None,None,None)
    async def test_cas_rollback_acceptance_and_foreign_scope(self):
        import asyncio
        from dataclasses import replace
        row=await self.repo.create(self.p.id,self.actor,fixture(),sources=self.sources)
        r=await asyncio.gather(*[self.repo.revise(row['id'],self.actor,1,[dict(op='title',value=t)]) for t in ('one','two')],return_exceptions=True)
        self.assertEqual(1,sum(isinstance(x,MaterialError) for x in r))
        rolled,_=await self.repo.revise(row['id'],self.actor,2,rollback=1); self.assertEqual(fixture(),rolled['spec'])
        accepted=await self.repo.decide(row['id'],self.actor,3,'accepted'); self.assertEqual(accepted['head'],accepted['accepted'])
        with self.assertRaises(MaterialError): await self.repo.get(row['id'],replace(self.actor,user_id=8,sender_ref='user:8'))
    async def test_numeric_and_quote_proof_fences(self):
        from materials.extractors.basic import BasicExtractor
        from materials.dataset_repository import DatasetRepository
        from dataclasses import asdict
        asset=await self.service.ingest(b'value\n2.345\n','values.csv',self.actor,'csv','csv')
        from materials.datasets import DatasetPolicy,ColumnPolicy
        data=(await self.service.datasets(asset['id'],self.actor,policy=DatasetPolicy(columns=(ColumnPolicy(0,locale='en'),))))[0]
        v=fixture(); v['elements'][0].update(status='observed',quantity=dict(value='2.345',unit=data.cell('A2').normalized.unit),proof=dict(kind='dataset',dataset_id=data.id,address='A2'))
        row=await self.repo.create(self.p.id,self.actor,v)
        v['elements'][0]['quantity']['value']='9'
        with self.assertRaises(MaterialError): await self.repo.create(self.p.id,self.actor,v)
        from materials.lifecycle import MaterialLifecycle
        await MaterialLifecycle(self.materials,self.service.store).forget(asset['id'],self.actor)
        with self.assertRaises(MaterialError): await self.repo.decide(row['id'],self.actor,1,'accepted')
