import os,asyncio,unittest,tempfile
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from agents.tools.registry import Registry,Tool,ToolResult,object_schema,STRING
from agents.planner import Plan
from agents.tasks import TaskRepository
from agents.executor import Executor
from materials.types import MaterialError,AccessContext,MaterialScope,EvidenceRef

def plan(tool='test.echo',args=None): return dict(goal='Verified work',steps=[dict(id='s1',tool=tool,version='1',args=args or dict(text='hello'),depends=[])],checks=[dict(step='s1',path=['text'],op='equals',value='hello')])

class AgentPureTests(unittest.TestCase):
    def test_plan_schema_dependencies_and_closed_tools(self):
        r=Registry(); r.register(Tool('test.echo','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),None,max_bytes=1000))
        Plan(plan(),r)
        for field,value in [('tool','shell.exec'),('version','2'),('args',dict(text='hi',grant=True)),('depends',['later'])]:
            p=plan(); p['steps'][0][field]=value
            with self.assertRaises(MaterialError): Plan(p,r)
    def test_sandbox_denies_host_code_and_bounds(self):
        from agents.sandbox import run
        self.assertEqual('3.3',run([dict(op='sum',input='numbers',output='total')],dict(numbers=['1.1','2.2']))['total'])
        for op in ['exec','import','open','eval']:
            with self.assertRaises(MaterialError): run([dict(op=op,input='numbers',output='x')],dict(numbers=[]))
    def test_schedule_dst_fold_gap_and_coalescing_math(self):
        from agents.subscriptions import schedule_after
        s=dict(kind='daily',timezone='America/New_York',hour=2,minute=30)
        got=schedule_after(s,datetime(2026,3,8,5,0,tzinfo=timezone.utc)); self.assertEqual(datetime(2026,3,8,7,0,tzinfo=timezone.utc),got)
        s.update(hour=1,minute=30)
        got=schedule_after(s,datetime(2026,11,1,4,0,tzinfo=timezone.utc)); self.assertEqual(datetime(2026,11,1,5,30,tzinfo=timezone.utc),got)
        second=schedule_after(s,got); self.assertEqual(2,second.day)
        with self.assertRaises(MaterialError): schedule_after(s,datetime(2026,1,1))
    def test_semantic_fingerprint_ignores_images_not_numbers(self):
        from agents.subscriptions import semantic_fingerprint as f
        self.assertEqual(f(dict(value='1',files=['pixels'],read_at='today')),f(dict(value='1',files=['other'],read_at='tomorrow')))
        self.assertNotEqual(f(dict(value='1')),f(dict(value='2')))

@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class AgentSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        from materials.repository import MaterialRepository
        from materials.service import MaterialService
        from materials.storage import LocalBlobStore
        from projects.repository import ProjectRepository
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); await ensure_schema(self.pool); self.temp=tempfile.TemporaryDirectory()
        self.materials=MaterialRepository(self.pool); self.service=MaterialService(self.materials,LocalBlobStore(self.temp.name)); self.actor=AccessContext(MaterialScope('arti',55,-1,'private'),7,'user:7')
        self.projects=ProjectRepository(self.materials); self.p=await self.projects.create(self.actor,'Workflow')
        self.asset=await self.service.ingest(b'Explicit user request','request.txt',self.actor,'request','request'); id,b=await self.service.extract(self.asset['id'],self.actor); self.refs=[EvidenceRef(self.asset['id'],1,id,b.blocks[0].block_id,b.blocks[0].locator)]
        self.registry=Registry(); self.calls=[]
        async def echo(args,c): self.calls.append(args); return ToolResult('success',dict(text=args['text']))
        self.registry.register(Tool('test.echo','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),echo,max_bytes=1000))
        self.repo=TaskRepository(self.materials,self.registry)
    async def asyncTearDown(self): self.temp.cleanup(); await self.db.__aexit__(None,None,None)
    async def create(self,p=None,**kw): return await self.repo.create(self.actor,self.p.id,p or plan(),self.refs,**kw)
    async def successful(self):
        task=await self.create(); result=await Executor(self.repo,self.service).run(task['id']); self.assertEqual('succeeded',result['status']); return task
    async def test_execution_restart_reuses_verified_outputs(self):
        task=await self.create(); leases=await asyncio.gather(self.repo.claim(task['id']),self.repo.claim(task['id'])); lease=next(x for x in leases if x)
        self.assertEqual(1,sum(x is not None for x in leases)); attempt=await self.repo.begin(lease,plan()['steps'][0],dict(text='hello'))
        await self.repo.complete(lease,plan()['steps'][0],attempt,ToolResult('success',dict(text='hello')))
        async with self.pool.acquire() as conn: await conn.execute("UPDATE arti_tasks SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1",task['id'])
        new=await self.repo.claim(task['id']); self.assertGreater(new['fence'],lease['fence'])
        with self.assertRaises(MaterialError): await self.repo.finish(lease,'succeeded')
        self.assertEqual(dict(s1=dict(text='hello')),await self.repo.outputs(new))
        await self.repo.finish(new,'succeeded'); self.assertEqual(0,len(self.calls))
    async def test_independent_read_failure_preserves_other_completed_output(self):
        async def bad(args,c): raise MaterialError('source_missing')
        self.registry.register(Tool('test.badread','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),bad,max_bytes=1000))
        p=plan(); p['steps'].append(dict(id='bad',tool='test.badread',version='1',args=dict(text='fail'),depends=[]))
        row=await self.create(p); result=await Executor(self.repo,self.service).run(row['id'])
        self.assertEqual('partial',result['status']); self.assertEqual(dict(s1=dict(text='hello')),await self.repo.outputs(row))
        from agents.runtime import deliver_task
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock
        import json
        current=await self.repo.get(row['id'],self.actor); bot=NS(send_document=AsyncMock(return_value=NS(message_id=80,chat=NS(id=55))))
        await deliver_task(self.service,self.repo,current,bot,'partial-download',reader=self.actor)
        packet=json.loads(bot.send_document.await_args.kwargs['document'].getvalue()); self.assertFalse(packet['overall_verified']); self.assertEqual('partial',packet['status']); self.assertEqual('hello',packet['outputs']['s1']['text'])
    async def test_reviewed_grant_and_supported_reconciliation(self):
        async def send(args,c): self.calls.append('remote'); raise ConnectionError('receipt lost')
        async def reconcile(args,c,key): return ToolResult('success',dict(text='hello'),receipt=dict(remote_id='42'))
        tool=Tool('test.remote','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),send,'external',max_bytes=1000,reconcile=reconcile)
        self.registry.register(tool); row=await self.create(plan('test.remote'))
        await Executor(self.repo,self.service).run(row['id']); row=await self.repo.get(row['id'],self.actor)
        preview=await self.repo.preview_effect(row['id'],self.actor,'s1')
        with self.assertRaises(MaterialError): await self.repo.authorize_effect(row['id'],self.actor,row['revision'],'s1','wrong','request')
        row=await self.repo.authorize_effect(row['id'],self.actor,row['revision'],'s1',preview['digest'],'request')
        await Executor(self.repo,self.service).run(row['id']); self.assertEqual('unknown',(await self.repo.get(row['id'],self.actor))['status'])
        row=await self.repo.reconcile_effect(row['id'],self.actor,'s1',self.service)
        row=await self.repo.control(row['id'],self.actor,row['revision'],'resume')
        result=await Executor(self.repo,self.service).run(row['id']); self.assertEqual('succeeded',result['status']); self.assertEqual(['remote'],self.calls)
    async def test_planning_transition_is_durable_and_uses_same_budget(self):
        async def planning(args,c): return ToolResult('success',dict(id=c.task_id,kind='task',plan=plan()),cost='.25')
        self.registry.register(Tool('workflow.plan','1',object_schema(dict(text=STRING)),object_schema(dict(id=STRING,kind=STRING,plan=dict(type='object'))),planning,'write',max_cost='.5',max_bytes=10000,idempotent=True))
        root=plan('workflow.plan'); root['checks']=[dict(step='s1',path=['id'],op='nonempty')]
        row=await self.create(root)
        result=await Executor(self.repo,self.service).run(row['id']); self.assertEqual('queued',result['status'])
        row=await self.repo.get(row['id'],self.actor); self.assertEqual(1,row['replans']); self.assertEqual('.25',str(row['used_cost']).lstrip('0'))
        result=await Executor(self.repo,self.service).run(row['id']); self.assertEqual('succeeded',result['status']); self.assertEqual(1,len(self.calls))
    async def test_unknown_external_write_never_repeats(self):
        from agents.permissions import CapabilityRepository
        async def send(args,c): self.calls.append('sent'); raise ConnectionError('lost after remote commit')
        tool=Tool('test.send','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),send,'external',max_bytes=1000,recipient=lambda a:'recipient')
        self.registry.register(tool)
        grants=CapabilityRepository(self.pool); grant=await grants.issue(self.actor,tool,dict(text='hello'),request_id='human-send',expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),max_cost='0',origin='user')
        p=plan('test.send'); p['steps'][0]['grant_id']=grant; task=await self.create(p)
        await Executor(self.repo,self.service).run(task['id']); self.assertEqual('unknown',(await self.repo.get(task['id'],self.actor))['status'])
        self.assertIsNone(await Executor(self.repo,self.service).run(task['id'])); self.assertEqual(['sent'],self.calls)
        with self.assertRaises(MaterialError): await self.repo.control(task['id'],self.actor,2,'resume')
    async def test_capability_preflight_denial_without_write_intent(self):
        async def send(args,c): self.calls.append('sent'); return ToolResult('success',dict(text='hello'),receipt=dict(id='42'))
        self.registry.register(Tool('test.send','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),send,'external',max_bytes=1000))
        task=await self.create(plan('test.send')); await Executor(self.repo,self.service).run(task['id'])
        self.assertEqual('waiting',(await self.repo.get(task['id'],self.actor))['status']); self.assertFalse(self.calls)
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM arti_task_calls'))
    async def test_grant_recipient_digest_expiry_and_revoke(self):
        from agents.permissions import CapabilityRepository
        grants=CapabilityRepository(self.pool); tool=self.registry.get('test.echo'); args=dict(text='hello')
        id=await grants.issue(self.actor,tool,args,request_id='grant',expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),max_cost='0',origin='user')
        await grants.validate(id,self.actor,tool,args)
        with self.assertRaises(MaterialError): await grants.validate(id,self.actor,tool,dict(text='changed'))
        with self.assertRaises(MaterialError): await grants.validate(id,replace(self.actor,user_id=8,sender_ref='user:8'),tool,args)
        await grants.revoke(id,self.actor)
        with self.assertRaises(MaterialError): await grants.validate(id,self.actor,tool,args)
    async def test_budget_and_bad_tool_output(self):
        async def bad(args,c): return ToolResult('success',dict(wrong='yes'))
        self.registry.register(Tool('test.bad','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),bad,max_cost='2',max_bytes=1000))
        task=await self.create(plan('test.bad')); await Executor(self.repo,self.service).run(task['id'])
        row=await self.repo.get(task['id'],self.actor); self.assertEqual(0,row['used_calls']); self.assertEqual('task_budget_exhausted',row['diagnostics'])
        task=await self.create(plan('test.bad'),max_cost='5'); await Executor(self.repo,self.service).run(task['id']); self.assertEqual('partial',(await self.repo.get(task['id'],self.actor))['status'])
    async def test_cancel_during_call_blocks_output(self):
        started=asyncio.Event(); release=asyncio.Event()
        async def slow(args,c): started.set(); await release.wait(); return ToolResult('success',dict(text='hello'))
        self.registry.register(Tool('test.slow','1',object_schema(dict(text=STRING)),object_schema(dict(text=STRING)),slow,max_bytes=1000))
        task=await self.create(plan('test.slow')); running=asyncio.create_task(Executor(self.repo,self.service).run(task['id'])); await started.wait()
        await self.repo.control(task['id'],self.actor,1,'cancel'); release.set(); await running
        self.assertEqual('cancelled',(await self.repo.get(task['id'],self.actor))['status'])
        async with self.pool.acquire() as conn: self.assertIsNone(await conn.fetchval('SELECT output_id FROM arti_task_calls'))
    async def test_forget_clears_plan_and_outputs(self):
        from materials.lifecycle import MaterialLifecycle
        task=await self.successful(); await MaterialLifecycle(self.materials,self.service.store).forget(self.asset['id'],self.actor)
        with self.assertRaises(MaterialError): await self.repo.outputs(task)
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_derivatives WHERE payload IS NOT NULL'))
    async def test_full_dataset_calculation_graph_report_pipeline(self):
        from agents.tools.core import build_registry
        from tests.materials.test_artifacts import fixture
        self.registry=build_registry(); self.repo=TaskRepository(self.materials,self.registry)
        asset=await self.service.ingest(b'value\n2.25\n3.25\n','numbers.csv',self.actor,'numbers','numbers')
        spec=fixture(); spec['format']='statistical'; spec['axis']=dict(unit='1',scale='linear')
        spec['elements'][0].update(status='observed',quantity={'$step':'calculate','path':['result']},proof=dict(kind='computation',computation_id={'$step':'calculate','path':['id']}))
        p=dict(goal='CSV to exact report',steps=[
         dict(id='extract',tool='dataset.extract',version='1',args=dict(asset_id=asset['id'],policy=dict(columns=[dict(index=0,locale='en')])),depends=[]),
         dict(id='calculate',tool='dataset.compute',version='1',args=dict(dataset_id={'$step':'extract','path':['datasets',0,'id']},spec=dict(operation='sum',selection='A2:A3')),depends=['extract']),
         dict(id='graph',tool='artifact.create',version='1',args=dict(spec=spec),depends=['calculate']),
         dict(id='report',tool='documents.report',version='1',args=dict(artifact_id={'$step':'graph','path':['id']},revision={'$step':'graph','path':['revision']},format='pdf'),depends=['graph'])],
         checks=[dict(step='calculate',path=['result','value'],op='equals',value='5.5'),dict(step='report',path=['files'],op='nonempty')])
        task=await self.create(p,max_bytes=12*1024**2); result=await Executor(self.repo,self.service).run(task['id'])
        self.assertEqual('succeeded',result['status'],result)
        import base64
        from io import BytesIO
        from pypdf import PdfReader
        text=''.join(p.extract_text() for p in PdfReader(BytesIO(base64.b64decode(result['outputs']['report']['files'][0]['base64']))).pages); self.assertIn('5.5 1',text)
