import os,unittest,asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from tests.materials import test_agents as _agents
from tests.materials.test_agents import plan
from agents.procedures import ProcedureRepository
from agents.subscriptions import SubscriptionRepository
from agents.scheduler import Scheduler
from materials.types import MaterialError,EvidenceRef,MaterialScope

@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL')
class WorkflowSQLTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=_agents.AgentSQLTests.asyncSetUp
    asyncTearDown=_agents.AgentSQLTests.asyncTearDown
    create=_agents.AgentSQLTests.create
    successful=_agents.AgentSQLTests.successful
    async def procedure(self):
        task=await self.successful(); repo=ProcedureRepository(self.materials,self.registry)
        recipe=plan(); recipe['steps'][0]['args']['text']={'$input':'text'}; recipe['checks']=[dict(step='s1',path=['text'],op='nonempty')]
        body=dict(title='Echo report',recipe=recipe,input_schema=dict(type='object',properties=dict(text=dict(type='string')),required=['text'],additionalProperties=False),examples=[dict(bindings=dict(text='hello'),expected_tools=['test.echo'],outputs=dict(s1=dict(text='hello')))],constraints=dict(max_calls=10))
        row=await repo.save_success(task['id'],self.actor,body,self.refs,confirmed=True,origin='user'); return repo,row,body
    async def test_procedure_confirmed_success_versions_inputs_and_no_grants(self):
        repo,row,body=await self.procedure()
        p,_=await repo.instantiate(row['id'],self.actor,dict(text='hello')); self.assertEqual('hello',p['steps'][0]['args']['text'])
        with self.assertRaises(MaterialError): await repo.instantiate(row['id'],self.actor,{})
        body['recipe']['steps'][0]['grant_id']='old-consent'
        with self.assertRaises(MaterialError): await repo.revise_confirmed(row['id'],self.actor,row['revision'],body,confirmed=True,origin='user')
        with self.assertRaises(MaterialError): await repo.control(row['id'],replace(self.actor,user_id=8,sender_ref='user:8'),row['revision'],'delete')
    async def test_subscription_restart_change_dedup_unknown_and_unsubscribe(self):
        _,procedure,_=await self.procedure(); repo=SubscriptionRepository(self.materials,self.registry)
        from agents.executor import Executor
        from agents.tools.registry import ToolResult
        self.measurement='5'
        async def measure(args,c): return ToolResult('success',dict(text=self.measurement))
        self.registry.tools['test.echo']=replace(self.registry.get('test.echo'),handler=measure)
        async def publish(run):
            result=await Executor(self.repo,self.service).run(run['task_id'])
            return await repo.record_result(sub['id'],self.actor,1,run['occurrence'],result['outputs'],verified=True)
        anchor=datetime.now(timezone.utc); schedule=dict(kind='interval',timezone='Asia/Yekaterinburg',seconds=300,anchor=anchor.isoformat())
        sub=await repo.subscribe(self.actor,procedure['id'],dict(text='hello'),schedule,self.refs,confirmed=True,origin='user')
        now=anchor+timedelta(seconds=950)
        async with self.pool.acquire() as conn: await conn.execute('UPDATE arti_subscription_cursor SET next_at=$2 WHERE subscription_id=$1',sub['id'],anchor+timedelta(seconds=300))
        scheduler=Scheduler(self.service,self.registry); runs=await scheduler.tick(now); self.assertEqual(1,len(runs)); self.assertEqual(anchor+timedelta(seconds=900),runs[0]['occurrence'])
        self.assertEqual([],await scheduler.tick(now))
        self.assertTrue(await publish(runs[0]))
        await repo.delivery_result(sub['id'],self.actor,1,runs[0]['occurrence'],'delivered','first')
        runs=await scheduler.tick(anchor+timedelta(seconds=1250)); self.assertEqual(1,len(runs))
        self.assertFalse(await publish(runs[0]))
        self.measurement='6'; runs=await scheduler.tick(anchor+timedelta(seconds=1550)); self.assertTrue(await publish(runs[0]))
        await repo.delivery_result(sub['id'],self.actor,1,runs[0]['occurrence'],'unknown','ambiguous')
        runs=await scheduler.tick(anchor+timedelta(seconds=1850)); self.assertFalse(await publish(runs[0]))
        await repo.manage(sub['id'],self.actor,1,'delete'); self.assertEqual([],await scheduler.tick(anchor+timedelta(days=1)))
    async def test_procedure_revision_pauses_dependent_subscription_and_task(self):
        proc,p,body=await self.procedure(); subs=SubscriptionRepository(self.materials,self.registry)
        sub=await subs.subscribe(self.actor,p['id'],dict(text='hello'),dict(kind='daily',timezone='UTC',hour=8,minute=0),self.refs,confirmed=True,origin='user')
        body['title']='New method'; await proc.revise_confirmed(p['id'],self.actor,p['revision'],body,confirmed=True,origin='user')
        async with self.pool.acquire() as conn: self.assertEqual('paused',await conn.fetchval('SELECT status FROM arti_workflow_objects WHERE id=$1',sub['id']))
        with self.assertRaises(MaterialError): await subs.manage(sub['id'],self.actor,1,'resume')
    async def test_source_revocation_erases_workflow_payloads(self):
        repo,row,_=await self.procedure()
        from materials.lifecycle import MaterialLifecycle
        await MaterialLifecycle(self.materials,self.service.store).forget(self.asset['id'],self.actor)
        with self.assertRaises(MaterialError): await repo.instantiate(row['id'],self.actor,dict(text='hello'))
        async with self.pool.acquire() as conn: self.assertEqual(0,await conn.fetchval('SELECT COUNT(*) FROM material_derivatives WHERE payload IS NOT NULL'))
    async def test_group_positions_assignment_acceptance_and_role_revocation(self):
        from projects.decisions import DecisionRepository
        from projects.assignments import AssignmentRepository
        actor=replace(self.actor,scope=MaterialScope('arti',-100,4,'supergroup')); p=await self.projects.create(actor,'Public')
        p=await self.projects.member(p.id,actor,1,8,'viewer'); viewer=replace(actor,user_id=8,sender_ref='user:8')
        async def sources(who,key):
            asset=await self.service.ingest(key.encode(),'request.txt',who,key,key); id,b=await self.service.extract(asset['id'],who); return [EvidenceRef(asset['id'],1,id,b.blocks[0].block_id,b.blocks[0].locator)]
        own=await sources(actor,'propose'); other=await sources(viewer,'response')
        decisions=DecisionRepository(self.materials); d=await decisions.propose(p.id,actor,'Choose a date',own,options=['Friday','Saturday'])
        d=await decisions.act(d['id'],viewer,1,'object',reason='Busy',sources=other); self.assertEqual('proposed',d['body']['status']); self.assertFalse(d['body']['consensus_inferred'])
        with self.assertRaises(MaterialError): await decisions.act(d['id'],viewer,2,'confirm',sources=other)
        d=await decisions.act(d['id'],actor,2,'confirm',option='Friday',sources=own); self.assertEqual(7,d['body']['confirmation']['actor_id'])
        assignments=AssignmentRepository(self.materials); a=await assignments.offer(p.id,actor,8,'Book a place',own); self.assertEqual('offered',a['body']['status'])
        with self.assertRaises(MaterialError): await assignments.respond(a['id'],actor,1,'accept',sources=own)
        a=await assignments.respond(a['id'],viewer,1,'decline',sources=other); self.assertEqual('declined',a['body']['status'])
        await self.projects.member(p.id,actor,p.revision,8,'remove')
        with self.assertRaises(MaterialError): await assignments.respond(a['id'],viewer,2,'accept',sources=other)
    async def test_work_action_foreign_scope_stale_and_unknown_send(self):
        from artifacts.revisions import ArtifactRepository
        from tests.materials.test_artifacts import fixture
        from bot.work_cards import WorkCards
        from types import SimpleNamespace
        cards=WorkCards(self.service); row=await cards.artifacts.create(self.p.id,self.actor,fixture(),sources=self.refs)
        markup=await cards.actions(row,self.actor); id=markup.inline_keyboard[0][0].callback_data[5:]
        with self.assertRaises(MaterialError): await cards.resolve(id,replace(self.actor,user_id=8,sender_ref='user:8'))
        await cards.artifacts.revise(row['id'],self.actor,1,[dict(op='title',value='New')])
        with self.assertRaises(MaterialError): await cards.resolve(id,self.actor)
        calls=[]
        async def send(**kw): calls.append(kw); raise ConnectionError('ambiguous')
        async def guard(): pass
        with self.assertRaises(ConnectionError): await cards.send(self.actor,self.p.id,'target',1,'key',send,dict(chat_id=55),guard)
        with self.assertRaises(MaterialError): await cards.send(self.actor,self.p.id,'target',1,'key',send,dict(chat_id=55),guard)
        self.assertEqual(1,len(calls))
    async def test_learning_progress_is_not_mastery_and_story_is_explicit_fiction(self):
        from projects.learning import LearningRepository
        repo=LearningRepository(self.materials); q=await repo.start(self.p.id,self.actor,'Lesson',[dict(id='one',prompt='2+2',expected='4')],self.refs)
        q=await repo.answer(q['id'],self.actor,1,'one','4'); self.assertTrue(q['body']['progress'][0]['completed']); self.assertEqual('not_measured',q['body']['mastery'])
        with self.assertRaises(MaterialError): await repo.start(self.p.id,self.actor,'Story',[dict(id='one',prompt='An invented event')],self.refs,scenario='story')
    async def test_group_subscription_uses_g_arbiter_receipts_and_late_revoke(self):
        from cognition.runtime import CognitiveRuntime
        from cognition.scope import TransportScope
        from tests.cognition.test_full_model import RecordedInterpreter
        from tests.cognition import test_groups as _groups
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock,patch
        from agents.executor import Executor
        from agents.group_tasks import propose_subscription
        clock=datetime.now(timezone.utc)
        runtime=await CognitiveRuntime(self.pool,RecordedInterpreter(),'active',clock=lambda:clock).initialize(False)
        judge=_groups.RecordedJudge(); runtime.groups.judge=judge
        bot=NS(send_message=AsyncMock(return_value=NS(message_id=900,chat=NS(id=-10))),get_chat_member=AsyncMock(return_value=NS(status='member')))
        try:
            self.actor=replace(self.actor,scope=MaterialScope('arti',-10,5,'supergroup')); self.p=await self.projects.create(self.actor,'Public report')
            source='telegram:-10:77:user'; self.asset=await self.service.ingest(b'Explicit public subscription','request.txt',self.actor,source,source)
            eid,b=await self.service.extract(self.asset['id'],self.actor); self.refs=[EvidenceRef(self.asset['id'],1,eid,b.blocks[0].block_id,b.blocks[0].locator)]
            await runtime.groups.observe(TransportScope(-10,5,'supergroup',7,77,True),'Подписка на общий обзор проекта')
            async with self.pool.acquire() as conn: await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-10,TRUE)')
            await runtime.groups.policies.set(-10,dict(mode='useful',execution='live',full_visibility=True,spacing_seconds=60))
            with patch('cognition.runtime.get_runtime',return_value=runtime):
                _,procedure,_=await self.procedure(); subs=SubscriptionRepository(self.materials,self.registry)
                sub=await subs.subscribe(self.actor,procedure['id'],dict(text='hello'),dict(kind='interval',timezone='UTC',seconds=300,anchor=clock.isoformat()),self.refs,confirmed=True,origin='user')
                clock+=timedelta(seconds=350); runs=await Scheduler(self.service,self.registry).tick(clock); run=runs[0]
                result=await Executor(self.repo,self.service).run(run['task_id']); self.assertEqual('succeeded',result['status'])
                await subs.record_result(sub['id'],self.actor,1,run['occurrence'],result['outputs'],verified=True)
                row=dict(run,access_generation=self.p.access_generation,revision=1)
                await propose_subscription(self.service,row,result['outputs'],self.actor,'group-run',bot)
                await runtime.groups.run_cycle(bot)
                bot.send_message.assert_awaited_once(); self.assertEqual(5,bot.send_message.await_args.kwargs['message_thread_id']); self.assertEqual(0,judge.compose_calls)
                async with self.pool.acquire() as conn: self.assertEqual('delivered',await conn.fetchval('SELECT status FROM arti_subscription_runs WHERE task_id=$1',run['task_id']))
                await runtime.groups.run_cycle(bot); bot.send_message.assert_awaited_once()
                await subs.manage(sub['id'],self.actor,1,'pause')
                with self.assertRaises(MaterialError): await propose_subscription(self.service,row,result['outputs'],self.actor,'revoked-run',bot)
        finally: await runtime.close()
