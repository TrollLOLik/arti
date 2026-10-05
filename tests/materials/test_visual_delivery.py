"""Real rendered bytes, synthetic transports, and disposable lifecycle barriers."""
import base64
import os
import unittest
import zipfile
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from materials.types import MaterialError,EvidenceRef
from agents.runtime import task_file_payload,deliver_task
from tests.materials import test_agents as _agents


def file_item(name='report.pdf',data=b'%PDF-1.4\nfixture'):
    return dict(name=name,base64=base64.b64encode(data).decode(),sha256=sha256(data).hexdigest())


class FilePayloadTests(unittest.TestCase):
    def test_single_file_bytes_and_name_are_preserved(self):
        item=file_item('data.csv',b'value\n-0.000001\n')
        result=task_file_payload(dict(export=dict(files=[item])))
        self.assertEqual('data.csv',result.name)
        self.assertEqual(base64.b64decode(item['base64']),result.read())
        self.assertIsNone(task_file_payload(dict(read=dict(text='plain structured result'))))

    def test_multifile_is_one_deterministic_safe_zip_even_with_duplicate_names(self):
        outputs=dict(a=dict(files=[file_item('report.pdf',b'first')]),b=dict(files=[file_item('report.pdf',b'second')]))
        result=task_file_payload(outputs)
        self.assertEqual('task-results.zip',result.name)
        self.assertEqual(result.getvalue(),task_file_payload(outputs).getvalue())
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(['001-report.pdf','002-report.pdf'],archive.namelist())
            self.assertEqual([b'first',b'second'],[archive.read(n) for n in archive.namelist()])

    def test_invalid_filename_base64_digest_and_shapes_fail_closed(self):
        invalid=[dict(file_item(),name=n) for n in ('../secret','/file','a\\b','.', '..','.env','a\n.txt','CON.txt','nul','a.','a'*151)]
        invalid += [dict(file_item(),base64=v) for v in ('%%%','eA===',None,True)]
        invalid += [dict(file_item(),sha256=v) for v in ('0'*64,'x',None,3)]
        invalid += [None,[],dict(file_item(),extra=True),dict(file_item(),name=1)]
        for item in invalid:
            with self.subTest(item=item),self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=[item])))
        for files in ([],{},None,'bad'):
            with self.subTest(files=files),self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=files)))

    def test_full_batch_validates_before_return_and_enforces_budgets(self):
        with self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=[file_item(),dict(file_item(),sha256='bad')])))
        with self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=[file_item()]*65)))
        with patch('agents.runtime.TASK_DELIVERY_BYTES',100):
            with self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=[file_item(data=b'x'*101)])))
            # ZIP metadata must also fit, even when the uncompressed bytes fit.
            with self.assertRaises(MaterialError): task_file_payload(dict(export=dict(files=[file_item(data=b'x'),file_item(data=b'y')])) )

    def test_terminal_presentable_outputs_keep_files_after_checks_and_partial_work(self):
        from agents.runtime import _presentable_outputs
        plan=dict(steps=[dict(id='image',tool='media.image',depends=[]),dict(id='build',tool='artifact.create',args=dict(image={'$step':'image','path':['id']}),depends=['image']),dict(id='report',tool='documents.report',args=dict(id={'$step':'build','path':['id']}),depends=['build']),dict(id='check',tool='runtime.transform',args=dict(file={'$step':'report','path':['files']}),depends=['report'])])
        image=dict(files=[file_item('image.png')]); artifact=dict(kind='artifact',id='artifact'); report=dict(files=[file_item()])
        self.assertEqual(dict(image=image),_presentable_outputs(plan,dict(image=image)))
        self.assertEqual(dict(build=artifact),_presentable_outputs(plan,dict(image=image,build=artifact)))
        self.assertEqual(dict(report=report),_presentable_outputs(plan,dict(image=image,build=artifact,report=report,check=dict(ok=True))))
        # Metadata between two presentable steps still carries transitive edges.
        plan['steps'].insert(2,dict(id='metadata',tool='runtime.transform',args=dict(id={'$step':'build','path':['id']}),depends=['build']))
        plan['steps'][3]['depends']=['metadata']; plan['steps'][3]['args']=dict(id={'$step':'metadata','path':['id']})
        self.assertEqual(dict(report=report),_presentable_outputs(plan,dict(image=image,build=artifact,metadata=dict(ok=True),report=report)))



    def test_ordered_exports_are_not_silently_discarded(self):
        from agents.runtime import _presentable_outputs
        plan=dict(steps=[dict(id='pdf',tool='documents.report',args={},depends=[]),dict(id='xlsx',tool='documents.data_export',args={},depends=['pdf'])])
        outputs=dict(pdf=dict(files=[file_item('report.pdf')]),xlsx=dict(files=[file_item('data.xlsx',b'xlsx fixture')]))
        self.assertEqual(outputs,_presentable_outputs(plan,outputs))
        # Keep the document even when a later export explicitly reads it.
        plan['steps'][1]['args']=dict(input={'$step':'pdf','path':['files']})
        self.assertEqual(outputs,_presentable_outputs(plan,outputs))
        result=task_file_payload(_presentable_outputs(plan,outputs))
        with zipfile.ZipFile(result) as archive: self.assertEqual(['001-report.pdf','002-data.xlsx'],archive.namelist())


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class VisualDeliverySQLTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=_agents.AgentSQLTests.asyncSetUp
    asyncTearDown=_agents.AgentSQLTests.asyncTearDown
    create=_agents.AgentSQLTests.create

    def bot(self):
        self.sent=[]
        async def photo(**kwargs): self.sent.append(('photo',kwargs)); return NS(message_id=701,chat=NS(id=55))
        async def document(**kwargs): self.sent.append(('document',kwargs)); return NS(message_id=702,chat=NS(id=55))
        async def message(**kwargs): self.sent.append(('message',kwargs)); return NS(message_id=703,chat=NS(id=55))
        return NS(send_photo=photo,send_document=document,send_message=message)

    async def artifact(self):
        from artifacts.revisions import ArtifactRepository
        from tests.materials.test_artifacts import fixture
        return await ArtifactRepository(self.materials).create(self.p.id,self.actor,fixture(),sources=self.refs)

    async def files_task(self,*,files=None,dependencies=(),extra=None):
        from agents.tools.registry import Tool,ToolResult,object_schema
        from agents.executor import Executor
        outputs=dict(files=files or [file_item()],**(extra or {}))
        async def make(args,ctx): return ToolResult('success',outputs,dependencies=dependencies)
        schema={key:dict(type='array' if key=='files' else 'integer' if key=='revision' else 'string') for key in outputs}
        self.registry.register(Tool('test.files','1',object_schema({}),object_schema(schema),make,max_bytes=4000000))
        plan=dict(goal='Deliver requested files',steps=[dict(id='export',tool='test.files',version='1',args={},depends=[])],checks=[dict(step='export',path=['files'],op='nonempty')])
        row=await self.create(plan)
        await Executor(self.repo,self.service).run(row['id'])
        row=await self.repo.get(row['id'],self.actor); self.assertEqual('succeeded',row['status'])
        return row

    async def delivery_state(self,key):
        async with self.pool.acquire() as conn: return await conn.fetchval('SELECT status FROM arti_work_delivery WHERE delivery_key=$1',key)

    async def test_artifact_is_one_real_png_caption_and_existing_buttons(self):
        from bot.work_cards import WorkCards
        from PIL import Image
        from artifacts.export import export_current
        row=await self.artifact(); bot=self.bot(); cards=WorkCards(self.service)
        await cards.show(row,self.actor,bot,'preview')
        self.assertEqual(1,len(self.sent)); channel,kwargs=self.sent[0]; self.assertEqual('photo',channel)
        self.assertTrue(kwargs['photo'].getvalue().startswith(b'\x89PNG\r\n\x1a\n'))
        with Image.open(BytesIO(kwargs['photo'].getvalue())) as image:
            image.load(); self.assertEqual((1080,1440),image.size); self.assertGreater(len(image.getcolors(image.width*image.height)),3)
        self.assertEqual((await export_current(cards.artifacts,row['id'],self.actor))[0]['page-1.png'],kwargs['photo'].getvalue())
        self.assertLess(len(kwargs['caption']),500); self.assertIn(row['spec']['title'],kwargs['caption'])
        self.assertIsNone(kwargs['parse_mode']); self.assertNotIn('/artifact',kwargs['caption'])
        self.assertEqual(8,sum(len(line) for line in kwargs['reply_markup'].inline_keyboard))
        self.assertEqual('delivered',await self.delivery_state('preview'))
        with patch('artifacts.export.export_current',new=AsyncMock()) as render:
            with self.assertRaises(MaterialError): await cards.show(row,self.actor,bot,'preview')
            render.assert_not_awaited()
        self.assertEqual(1,len(self.sent))

    async def test_progress_stays_text(self):
        from bot.work_cards import WorkCards
        row=await self.create(); bot=self.bot()
        await WorkCards(self.service).show_task(row,self.actor,bot,'progress')
        self.assertEqual(['message'],[kind for kind,_ in self.sent])
        self.assertIn('В очереди',self.sent[0][1]['text'])

    async def test_artifact_stale_revision_source_erasure_and_access_change_after_render(self):
        from bot.work_cards import WorkCards
        from artifacts.export import export_current
        from artifacts.revisions import ArtifactRepository
        row=await self.artifact(); bot=self.bot()
        async def stale(*args,**kwargs):
            result=await export_current(*args,**kwargs)
            await ArtifactRepository(self.materials).revise(row['id'],self.actor,row['revision'],[dict(op='title',value='New title')])
            return result
        with patch('artifacts.export.export_current',side_effect=stale):
            with self.assertRaises(MaterialError): await WorkCards(self.service).show(row,self.actor,bot,'stale')
        row=await ArtifactRepository(self.materials).get(row['id'],self.actor)
        async def changed(*args,**kwargs):
            result=await export_current(*args,**kwargs)
            async with self.pool.acquire() as conn: await conn.execute('UPDATE arti_projects SET access_generation=access_generation+1 WHERE id=$1',self.p.id)
            return result
        with patch('artifacts.export.export_current',side_effect=changed):
            with self.assertRaises(MaterialError): await WorkCards(self.service).show(row,self.actor,bot,'changed')
        async def erased(*args,**kwargs):
            result=await export_current(*args,**kwargs)
            from materials.lifecycle import MaterialLifecycle
            await MaterialLifecycle(self.materials,self.service.store).forget(self.asset['id'],self.actor)
            return result
        with patch('artifacts.export.export_current',side_effect=erased):
            with self.assertRaises(MaterialError): await WorkCards(self.service).show(row,self.actor,bot,'erased')
        self.assertEqual([],self.sent)

    async def test_file_outputs_take_priority_and_deliver_actual_single_file(self):
        artifact=await self.artifact()
        row=await self.files_task(extra=dict(kind='artifact',id=artifact['id'],revision=artifact['revision'],derivative_id=artifact['head']),dependencies=(artifact['head'],))
        bot=self.bot(); await deliver_task(self.service,self.repo,row,bot,'file')
        self.assertEqual(['document'],[kind for kind,_ in self.sent])
        stream=self.sent[0][1]['document']; self.assertEqual('report.pdf',stream.name)
        self.assertEqual(base64.b64decode(file_item()['base64']),stream.getvalue())
        self.assertEqual('delivered',await self.delivery_state('file'))

    async def test_multiple_files_one_send_and_unknown_never_retries(self):
        row=await self.files_task(files=[file_item('report.pdf'),file_item('data.csv',b'a,b\n1,2\n')]); bot=self.bot()
        async def unknown(**kwargs): self.sent.append(('document',kwargs)); raise TimeoutError('fixture uncertain send')
        bot.send_document=unknown; bot._menu_panel=NS(output=False); bot.controller=NS(capture=AsyncMock())
        with self.assertRaises(TimeoutError): await deliver_task(self.service,self.repo,row,bot,'unknown')
        self.assertEqual('unknown',await self.delivery_state('unknown'))
        self.assertFalse(bot._menu_panel.output); bot.controller.capture.assert_not_awaited()
        self.assertEqual('task-results.zip',self.sent[0][1]['document'].name)
        with zipfile.ZipFile(self.sent[0][1]['document']) as archive: self.assertEqual(2,len(archive.namelist()))
        with patch('agents.runtime.task_file_payload') as prepare:
            with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'unknown')
            prepare.assert_not_called()
        self.assertEqual(1,len(self.sent))

    async def test_revision_or_status_race_after_reservation_cannot_send_or_retry(self):
        from agents.scope_guard import guard_scope
        row=await self.files_task(); bot=self.bot(); raced=False
        async def race(actor,pool):
            nonlocal raced
            if not raced and await self.delivery_state('race')=='sending':
                raced=True
                async with pool.acquire() as conn: await conn.execute("UPDATE arti_tasks SET revision=revision+1,status='paused' WHERE id=$1",row['id'])
            await guard_scope(actor,pool)
        with patch('agents.scope_guard.guard_scope',side_effect=race):
            with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'race')
        self.assertTrue(raced); self.assertEqual([],self.sent); self.assertEqual('unknown',await self.delivery_state('race'))
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'race')
        self.assertEqual([],self.sent)

    async def test_unchanged_status_with_stale_revision_is_rejected(self):
        row=await self.files_task(); bot=self.bot()
        async with self.pool.acquire() as conn: await conn.execute('UPDATE arti_tasks SET revision=revision+1 WHERE id=$1',row['id'])
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'revision')
        self.assertEqual([],self.sent)

    async def test_access_generation_and_reader_acl_are_rechecked(self):
        from dataclasses import replace
        row=await self.files_task(); bot=self.bot()
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'reader',reader=replace(self.actor,user_id=8,sender_ref='user:8'))
        async with self.pool.acquire() as conn: await conn.execute('UPDATE arti_projects SET access_generation=access_generation+1 WHERE id=$1',self.p.id)
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'access')
        self.assertEqual([],self.sent)

    async def test_output_only_source_erasure_blocks_delivery_and_preserves_full_graph(self):
        from materials.derivatives import DerivativeRepository
        from materials.lifecycle import MaterialLifecycle
        from agents.scope_guard import guard_scope
        source=await self.service.ingest(b'Output-only evidence','extra.txt',self.actor,'extra','extra')
        eid,b=await self.service.extract(source['id'],self.actor)
        refs=[EvidenceRef(source['id'],1,eid,b.blocks[0].block_id,b.blocks[0].locator)]
        dep=await DerivativeRepository(self.materials).save(self.actor,'test_result',dict(value='data'),refs)
        row=await self.files_task(dependencies=(dep,)); bot=self.bot(); erased=False
        async def erase(actor,pool):
            nonlocal erased
            if not erased and await self.delivery_state('erased-output')=='sending':
                erased=True; await MaterialLifecycle(self.materials,self.service.store).forget(source['id'],self.actor)
            await guard_scope(actor,pool)
        with patch('agents.scope_guard.guard_scope',side_effect=erase):
            with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'erased-output')
        self.assertEqual([],self.sent); self.assertEqual('unknown',await self.delivery_state('erased-output'))
        async with self.pool.acquire() as conn:
            payload=await conn.fetchval('SELECT d.payload FROM material_derivatives d JOIN arti_task_calls c ON c.output_id=d.id WHERE c.task_id=$1',row['id'])
            self.assertIsNone(payload)
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'erased-output')

    async def test_successful_output_sources_enter_cognitive_receipt_dependencies(self):
        from cognition.runtime import CognitiveRuntime
        from tests.cognition.test_full_model import RecordedInterpreter
        from materials.derivatives import DerivativeRepository
        runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active').initialize(False)
        try:
            refs=[]
            for message_id in (77,78):
                source=f'telegram:55:{message_id}:user'
                asset=await self.service.ingest(f'Source {message_id}'.encode(),'request.txt',self.actor,source,source)
                eid,b=await self.service.extract(asset['id'],self.actor)
                refs.append([EvidenceRef(asset['id'],1,eid,b.blocks[0].block_id,b.blocks[0].locator)])
                await runtime.ingest(55,7,f'Source {message_id}',message_id,'default')
            self.refs=refs[0]
            dep=await DerivativeRepository(self.materials).save(self.actor,'test_result',dict(value='source 78'),refs[1])
            row=await self.files_task(dependencies=(dep,)); bot=self.bot()
            with patch('cognition.runtime.get_runtime',return_value=runtime): await deliver_task(self.service,self.repo,row,bot,'full-graph')
            async with self.pool.acquire() as conn:
                rows=await conn.fetch('SELECT * FROM cognitive_event_dependencies')
                self.assertGreaterEqual(len(rows),2)
                event_ids=await conn.fetch("SELECT id FROM cognitive_events WHERE source_id=ANY($1::text[]) AND origin='user'",['telegram:55:77:user','telegram:55:78:user'])
                parents={r['source_event_id'] for r in rows}
                self.assertTrue({r['id'] for r in event_ids}<=parents)
            self.assertEqual(1,len(self.sent))
        finally: await runtime.close()

    async def test_receipt_preparation_revocation_still_blocks_actual_private_transport(self):
        from cognition.runtime import CURRENT_TURN
        from materials.lifecycle import MaterialLifecycle
        row=await self.files_task(); bot=self.bot()
        # A native receipt turn can exist without a cognitive event for this
        # material. Its outbox preparation must not bypass the material fence.
        async def receipt(method,args,kwargs,channel):
            await MaterialLifecycle(self.materials,self.service.store).forget(self.asset['id'],self.actor)
            return await method(*args,**kwargs)
        token=CURRENT_TURN.set(NS(tracks_delivery=True))
        try:
            with patch('cognition.runtime.get_runtime',return_value=None),patch('cognition.delivery.send_with_receipt',side_effect=receipt) as prepared:
                with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'receipt-race')
                prepared.assert_awaited_once()
        finally: CURRENT_TURN.reset(token)
        self.assertEqual([],self.sent); self.assertEqual('unknown',await self.delivery_state('receipt-race'))
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'receipt-race')

    async def test_workflow_planned_artifact_without_derivative_id_uses_persisted_graph(self):
        from agents.tools.core import build_registry
        from agents.tasks import TaskRepository
        from agents.executor import Executor
        from artifacts.spec import ArtifactSpec
        from artifacts.revisions import ArtifactRepository
        from tests.materials.test_artifacts import fixture
        self.registry=build_registry(); self.repo=TaskRepository(self.materials,self.registry)
        plan=dict(goal='Make a planned artifact',steps=[dict(id='build',tool='workflow.plan',version='1',args=dict(goal='Make a card',kind='artifact',context={}),depends=[])],checks=[dict(step='build',path=['id'],op='nonempty')])
        row=await self.create(plan)
        with patch('agents.model_planner.ModelPlanner.propose',new=AsyncMock(return_value=ArtifactSpec(fixture()))) as model:
            result=await Executor(self.repo,self.service).run(row['id'])
            model.assert_awaited_once()
        self.assertEqual('succeeded',result['status'])
        row=await self.repo.get(row['id'],self.actor); output=(await self.repo.outputs(row))['build']
        self.assertNotIn('derivative_id',output)
        bot=self.bot(); await deliver_task(self.service,self.repo,row,bot,'planned')
        self.assertEqual(['photo'],[kind for kind,_ in self.sent])
        await ArtifactRepository(self.materials).revise(output['id'],self.actor,output['revision'],[dict(op='title',value='Changed after task')])
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'stale-planned')
        self.assertEqual(1,len(self.sent))

    async def test_create_then_patch_delivers_newest_terminal_artifact(self):
        from agents.tools.core import build_registry
        from agents.tasks import TaskRepository
        from agents.executor import Executor
        from tests.materials.test_artifacts import fixture
        self.registry=build_registry(); self.repo=TaskRepository(self.materials,self.registry)
        plan=dict(goal='Create then revise a card',steps=[
            dict(id='create',tool='artifact.create',version='1',args=dict(spec=fixture()),depends=[]),
            dict(id='revise',tool='artifact.patch',version='1',args=dict(id={'$step':'create','path':['id']},revision=1,patches=[dict(op='title',value='Final revised title')]),depends=['create'])],checks=[dict(step='revise',path=['revision'],op='equals',value=2)])
        row=await self.create(plan); result=await Executor(self.repo,self.service).run(row['id']); self.assertEqual('succeeded',result['status'])
        row=await self.repo.get(row['id'],self.actor); bot=self.bot()
        await deliver_task(self.service,self.repo,row,bot,'revised')
        self.assertEqual(['photo'],[kind for kind,_ in self.sent]); self.assertIn('Final revised title',self.sent[0][1]['caption']); self.assertIn('Версия 2',self.sent[0][1]['caption'])

    async def test_missing_artifact_head_dependency_cannot_deliver(self):
        from agents.tools.core import build_registry
        from agents.tasks import TaskRepository
        from agents.executor import Executor
        from tests.materials.test_artifacts import fixture
        self.registry=build_registry(); self.repo=TaskRepository(self.materials,self.registry)
        plan=dict(goal='Make a card',steps=[dict(id='create',tool='artifact.create',version='1',args=dict(spec=fixture()),depends=[])],checks=[dict(step='create',path=['id'],op='nonempty')])
        row=await self.create(plan); await Executor(self.repo,self.service).run(row['id']); row=await self.repo.get(row['id'],self.actor)
        output=(await self.repo.outputs(row))['create']
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM material_derivative_links WHERE derivative_id IN (SELECT output_id FROM arti_task_calls WHERE task_id=$1) AND input_id=$2',row['id'],output['derivative_id'])
        bot=self.bot()
        with self.assertRaisesRegex(MaterialError,'task_artifact_dependency_missing'): await deliver_task(self.service,self.repo,row,bot,'missing-head')
        self.assertEqual([],self.sent)

    async def test_sequenced_pdf_and_xlsx_exports_arrive_in_one_zip(self):
        from agents.tools.registry import Tool,ToolResult,object_schema,STRING
        from agents.executor import Executor
        async def make(args,ctx): return ToolResult('success',dict(files=[file_item('result.'+args['format'],args['format'].encode())]))
        self.registry.register(Tool('test.export','1',object_schema(dict(format=STRING)),object_schema(dict(files=dict(type='array'))),make,max_bytes=10000))
        plan=dict(goal='Both PDF and XLSX',steps=[
            dict(id='pdf',tool='test.export',version='1',args=dict(format='pdf'),depends=[]),
            dict(id='xlsx',tool='test.export',version='1',args=dict(format='xlsx'),depends=['pdf'])],checks=[dict(step='xlsx',path=['files'],op='nonempty')])
        row=await self.create(plan); await Executor(self.repo,self.service).run(row['id']); row=await self.repo.get(row['id'],self.actor)
        bot=self.bot(); await deliver_task(self.service,self.repo,row,bot,'both-files')
        self.assertEqual(1,len(self.sent))
        with zipfile.ZipFile(self.sent[0][1]['document']) as archive:
            self.assertEqual(['001-result.pdf','002-result.xlsx'],archive.namelist())
            self.assertEqual([b'pdf',b'xlsx'],[archive.read(name) for name in archive.namelist()])

    async def test_partial_results_require_explicit_reader_and_keep_incomplete_caption(self):
        row=await self.files_task(); bot=self.bot()
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_tasks SET status='partial',revision=revision+1 WHERE id=$1",row['id'])
        row=await self.repo.get(row['id'],self.actor)
        with self.assertRaises(MaterialError): await deliver_task(self.service,self.repo,row,bot,'auto-partial')
        await deliver_task(self.service,self.repo,row,bot,'requested-partial',reader=self.actor)
        self.assertEqual(1,len(self.sent)); self.assertEqual('report.pdf',self.sent[0][1]['document'].name)
        self.assertIn('ещё не проверена',self.sent[0][1]['caption'])

    async def test_menu_photo_confirmation_keeps_back_home_without_future_fallback(self):
        from bot.work_cards import WorkCards
        from bot.menu.store import MenuStore
        from bot.menu.panel import Panel
        from bot.menu.controller import Controller
        from tests.materials.test_menu import update_for
        artifact=await self.artifact(); bot=self.bot()
        bot.edit_message_text=AsyncMock(return_value=NS(message_id=703,chat=NS(id=55)))
        store=MenuStore(self.pool); menu=await store.open(55,-1,7,self.actor.scope.key)
        panel=Panel(store,menu,bot); await panel.render('Меню',panel.navigation(),allow_create=True)
        controller=Controller(panel,update_for(),NS(bot=bot,user_data={}),self.actor); controller.service=self.service
        self.sent.clear()
        async def show(*args,**kwargs): return await WorkCards(self.service).show(artifact,self.actor,controller.bot,'menu-photo')
        with patch('materials.runtime.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.menu.bridge.command',side_effect=show):
            await controller.execute_command('/artifact show '+artifact['id'])
            self.assertTrue(panel.output); self.assertIn('Результат отправлен',controller.last_text); self.assertNotIn('придёт',controller.last_text)
            navigation=list(panel.row['actions'].values())
            self.assertEqual(2,len(navigation)); self.assertTrue(all('nav' in action for action in navigation))
            self.assertIn(dict(nav='home'),navigation)
            await controller.act(dict(nav='home')); self.assertEqual('home',panel.row['screen'])
        self.assertEqual(['photo'],[kind for kind,_ in self.sent])
        self.assertEqual('delivered',await self.delivery_state('menu-photo'))

    async def test_unknown_photo_does_not_mark_menu_complete_or_retry(self):
        from bot.work_cards import WorkCards
        row=await self.artifact(); bot=self.bot(); panel=NS(output=False)
        bot._menu_panel=panel; bot.controller=NS(capture=AsyncMock())
        async def unknown(**kwargs): self.sent.append(('photo',kwargs)); raise TimeoutError('fixture uncertain photo')
        bot.send_photo=unknown
        with self.assertRaises(TimeoutError): await WorkCards(self.service).show(row,self.actor,bot,'unknown-photo')
        self.assertFalse(panel.output); bot.controller.capture.assert_not_awaited()
        with self.assertRaises(MaterialError): await WorkCards(self.service).show(row,self.actor,bot,'unknown-photo')
        self.assertEqual(1,len(self.sent)); self.assertEqual('unknown',await self.delivery_state('unknown-photo'))

    async def test_menu_file_zip_confirmation_keeps_back_home_without_fallback(self):
        from bot.menu.store import MenuStore
        from bot.menu.panel import Panel
        from bot.menu.controller import Controller
        from tests.materials.test_menu import update_for
        row=await self.files_task(files=[file_item('report.pdf'),file_item('data.csv',b'a,b\n1,2\n')]); bot=self.bot()
        bot.edit_message_text=AsyncMock(return_value=NS(message_id=703,chat=NS(id=55)))
        store=MenuStore(self.pool); menu=await store.open(55,-1,7,self.actor.scope.key)
        panel=Panel(store,menu,bot); await panel.render('Меню',panel.navigation(),allow_create=True)
        controller=Controller(panel,update_for(),NS(bot=bot,user_data={}),self.actor); controller.service=self.service
        self.sent.clear()
        async def deliver(*args,**kwargs): return await deliver_task(self.service,self.repo,row,controller.bot,'menu-files',reader=self.actor)
        with patch('materials.runtime.actor_for_current',new=AsyncMock(return_value=self.actor)),patch('bot.menu.bridge.command',side_effect=deliver):
            await controller.execute_command('/task result '+row['id'])
            self.assertTrue(panel.output); self.assertEqual('Готово. Результат отправлен отдельно.',controller.last_text)
            self.assertNotIn('придёт',controller.last_text)
            navigation=list(panel.row['actions'].values())
            self.assertEqual(2,len(navigation)); self.assertTrue(all('nav' in action for action in navigation))
            self.assertIn(dict(nav='home'),navigation)
            await controller.act(dict(nav='home')); self.assertEqual('home',panel.row['screen'])
        self.assertEqual(['document'],[kind for kind,_ in self.sent]); self.assertEqual('task-results.zip',self.sent[0][1]['document'].name)
        self.assertEqual('delivered',await self.delivery_state('menu-files'))
