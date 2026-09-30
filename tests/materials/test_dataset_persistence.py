import asyncio
from dataclasses import asdict,replace
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from artifacts.computation import ComputationSpec,Conversion
from materials.dataset_repository import DatasetRepository
from materials.datasets import correction_proposal
from materials.lifecycle import MaterialLifecycle
from materials.repository import MaterialRepository
from materials.service import MaterialService
from materials.storage import LocalBlobStore
from materials.types import AccessContext,MaterialScope,MaterialError
from tests.materials.table_fixtures import xlsx


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','set ARTI_TEST_DB=1 for disposable PostgreSQL')
class DatasetPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); await ensure_schema(self.pool)
        self.temp=tempfile.TemporaryDirectory(); self.store=LocalBlobStore(self.temp.name)
        self.repo=MaterialRepository(self.pool); self.service=MaterialService(self.repo,self.store)
        self.actor=AccessContext(MaterialScope('arti',-100,4,'supergroup'),7,'user:7')
        self.derivatives=DatasetRepository(self.repo); self.lifecycle=MaterialLifecycle(self.repo,self.store)
        self.asset=await self.service.ingest(xlsx(),'budget.xlsx',self.actor,'file','file')
        self.budget,self.rates=await self.service.datasets(self.asset['id'],self.actor)
    async def asyncTearDown(self):
        self.temp.cleanup(); await self.db.__aexit__(None,None,None)

    async def test_repeat_and_restart_resolve_same_evidence(self):
        first=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B4'))
        service=MaterialService(MaterialRepository(self.pool),self.store)
        second=await service.compute(self.budget.id,self.actor,ComputationSpec('sum','B4'))
        self.assertEqual(first.id,second.id)
        stored=await self.derivatives.load_computation(first.id,self.actor)
        self.assertEqual('1500.3',stored['result']['value'])
        self.assertEqual('stale',stored['formula_steps'][0]['cache_status'])
        for cell in self.budget.cells[:5]: self.assertEqual(cell.source.block_id,(await self.repo.resolve(cell.source,self.actor)).block_id)

    async def test_scope_topic_and_private_audience_cannot_read_or_compute(self):
        other=replace(self.actor,scope=replace(self.actor.scope,topic_id=5))
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B2'))
        with self.assertRaises(MaterialError): await self.derivatives.load_dataset(self.budget.id,other)
        with self.assertRaises(MaterialError): await self.derivatives.load_computation(result.id,other)
        with self.assertRaises(MaterialError): await self.service.compute(self.budget.id,other,ComputationSpec('sum','B2'))
        private=AccessContext(MaterialScope('arti',7,0,'private'),7,'user:7')
        asset=await self.service.ingest(xlsx(),'private.xlsx',private,'private','private')
        dataset,_=await self.service.datasets(asset['id'],private)
        private_result=await self.service.compute(dataset.id,private,ComputationSpec('sum','B2'))
        intruder=replace(private,user_id=8,sender_ref='user:8')
        for denied in (intruder,self.actor):
            with self.assertRaises(MaterialError): await self.derivatives.load_dataset(dataset.id,denied)
            with self.assertRaises(MaterialError): await self.derivatives.load_computation(private_result.id,denied)

    async def test_author_correction_invalidates_computation_but_keeps_original(self):
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B4'))
        proposal=await self.service.propose_correction(self.budget.id,self.actor,'B2','1300')
        reader=replace(self.actor,user_id=8,sender_ref='user:8')
        with self.assertRaises(MaterialError): await self.service.confirm_correction(reader,proposal)
        corrected=await self.service.confirm_correction(self.actor,proposal)
        self.assertEqual('1200.10',corrected.cell('B2').raw)
        with self.assertRaises(MaterialError): await self.derivatives.load_computation(result.id,self.actor)
        with self.assertRaises(MaterialError): await self.derivatives.load_dataset(self.budget.id,self.actor)
        current,_=await self.service.datasets(self.asset['id'],self.actor)
        self.assertEqual(corrected.id,current.id)
        new=await self.service.compute(corrected.id,self.actor,ComputationSpec('sum','B4'))
        self.assertEqual('1600.2',new.result['value'])
        self.assertEqual('1200.10',(await self.repo.resolve(corrected.cell('B2').source,self.actor)).metadata['raw'])

    async def test_concurrent_corrections_use_head_cas(self):
        one=correction_proposal(self.budget,'B2','1300'); two=correction_proposal(self.budget,'B2','1400')
        results=await asyncio.gather(self.service.confirm_correction(self.actor,one),self.service.confirm_correction(self.actor,two),return_exceptions=True)
        self.assertEqual(1,sum(not isinstance(r,Exception) for r in results))
        self.assertEqual(1,sum(isinstance(r,MaterialError) for r in results))

    async def test_cross_sheet_correction_invalidates_dependents_even_same_value(self):
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','C2'),formula_dataset_ids=(self.rates.id,))
        self.assertEqual('14.4012',result.result['value'])
        # Same value explicitly declared as a decimal, not a thousands separator.
        from materials.datasets import DatasetPolicy,ColumnPolicy
        snapshots=await self.service.datasets(self.asset['id'],self.actor,policy=DatasetPolicy(columns=(ColumnPolicy(1,locale='en'),)))
        rates=snapshots[1]
        alternative=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','C2'),formula_dataset_ids=(rates.id,))
        corrected=await self.service.confirm_correction(self.actor,correction_proposal(rates,'B2','0.012'))
        with self.assertRaises(MaterialError): await self.derivatives.load_computation(alternative.id,self.actor)
        new=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','C2'),formula_dataset_ids=(corrected.id,))
        self.assertEqual(alternative.result,new.result); self.assertNotEqual(alternative.id,new.id)

    async def test_revision_and_forget_revoke_all_related_payloads(self):
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B2:B3'))
        await self.service.revise(self.asset['id'],xlsx(amount='2000.10'),'budget.xlsx',self.actor,1)
        with self.assertRaises(MaterialError): await self.derivatives.load_computation(result.id,self.actor)
        current,_=await self.service.datasets(self.asset['id'],self.actor)
        self.assertNotEqual(self.budget.id,current.id)
        new=await self.service.compute(current.id,self.actor,ComputationSpec('sum','B2:B3'))
        self.assertEqual('2300.3',new.result['value'])
        await self.lifecycle.forget(self.asset['id'],self.actor)
        for id in (result.id,new.id):
            with self.assertRaises(MaterialError): await self.derivatives.load_computation(id,self.actor)
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_derivatives WHERE payload IS NOT NULL'))

    async def test_conversion_uses_exact_rate_evidence_and_rejects_invented_factor(self):
        rate=Conversion('RUB','USD','0.012',asdict(self.rates.cell('B2').source))
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('convert','B2',target_unit='USD',conversions=(rate,)))
        self.assertEqual('14.4012',result.result['value'])
        with self.assertRaisesRegex(MaterialError,'conversion_rate_not_verified'):
            await self.service.compute(self.budget.id,self.actor,ComputationSpec('convert','B2',target_unit='USD',conversions=(replace(rate,factor='80'),)))

    async def test_forget_during_cpu_calculation_cannot_commit_result(self):
        original=asyncio.to_thread
        async def erase_then_return(function,*args,**kwargs):
            result=await original(function,*args,**kwargs)
            if function.__module__=='artifacts.computation': await self.lifecycle.forget(self.asset['id'],self.actor)
            return result
        with patch('materials.service.asyncio.to_thread',side_effect=erase_then_return):
            with self.assertRaises(MaterialError): await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B2'))

    async def test_telegram_reply_calculation_and_revoked_delivery_guard(self):
        from bot.table_commands import calc_command
        from cognition.scope import CURRENT_SCOPE,TransportScope
        from materials.runtime import CURRENT_COMPUTATION_USE,ComputationUse,guard_current
        data=xlsx(); document=SimpleNamespace(file_id='file',file_name='budget.xlsx',file_size=len(data),mime_type=None)
        original_message=SimpleNamespace(chat_id=-100,message_id=42,message_thread_id=4,document=document,sender_chat=None,from_user=SimpleNamespace(id=7))
        message=SimpleNamespace(chat_id=-100,text='/calc sum B2:B3 sheet=1',reply_to_message=original_message,reply_text=AsyncMock())
        context=SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_size=len(data),download_as_bytearray=AsyncMock(return_value=bytearray(data))))))
        update=SimpleNamespace(effective_message=message)
        token=CURRENT_SCOPE.set(TransportScope(-100,4,'supergroup',7,sender_ref='user:7'))
        try:
            with patch.dict(os.environ,{'ARTI_MATERIALS_ENABLED':'1'}),patch('materials.runtime.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.table_commands.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('materials.runtime.service_for_bot',new=AsyncMock(return_value=self.service)),patch('bot.table_commands.service_for_bot',new=AsyncMock(return_value=self.service)),patch('cognition.runtime.get_runtime',return_value=None):
                await calc_command(update,context)
            self.assertIn('1500.3 RUB',message.reply_text.call_args.args[0])
            result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('sum','B2'))
            usage=CURRENT_COMPUTATION_USE.set((ComputationUse(result.id,self.actor,self.derivatives),))
            try:
                await self.service.confirm_correction(self.actor,correction_proposal(self.budget,'B2','1300'))
                with self.assertRaises(MaterialError): await guard_current(-100)
            finally: CURRENT_COMPUTATION_USE.reset(usage)
        finally: CURRENT_SCOPE.reset(token)

    async def test_confirmed_conversion_rate_head_and_interval_are_dependencies(self):
        from materials.datasets import DatasetPolicy,ColumnPolicy
        _,rates=await self.service.datasets(self.asset['id'],self.actor,policy=DatasetPolicy(columns=(ColumnPolicy(1,locale='en'),)))
        rate=Conversion('RUB','USD','0.012',asdict(rates.cell('B2').source),dataset_id=rates.id)
        first=await self.service.compute(self.budget.id,self.actor,ComputationSpec('convert','B2',target_unit='USD',conversions=(rate,)))
        corrected=await self.service.confirm_correction(self.actor,correction_proposal(rates,'B2','0.015 ± 0.001'))
        with self.assertRaises(MaterialError): await self.derivatives.load_computation(first.id,self.actor)
        replacement=replace(rate,factor='0.015',lower='0.014',upper='0.016',dataset_id=corrected.id)
        result=await self.service.compute(self.budget.id,self.actor,ComputationSpec('convert','B2',target_unit='USD',conversions=(replacement,)))
        self.assertEqual(('18.0015','16.8014','19.2016'),tuple(result.result[k] for k in ('value','lower','upper')))
        self.assertIn(corrected.id,result.dependency_datasets)
        with self.assertRaisesRegex(MaterialError,'conversion_rate_not_verified'):
            await self.service.compute(self.budget.id,self.actor,ComputationSpec('convert','B2',target_unit='USD',conversions=(replace(replacement,lower='0.015'),)))
