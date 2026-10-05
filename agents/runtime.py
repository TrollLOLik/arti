"""Opt-in supervised runtime; offline tests call Executor/Scheduler directly."""
import asyncio,os,logging,base64,binascii,re,zipfile
from datetime import datetime,timezone
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from agents.tasks import TaskRepository,task_actor
from agents.tools.core import build_registry
from agents.executor import Executor
from agents.scheduler import Scheduler
from agents.subscriptions import SubscriptionRepository
from materials.types import MaterialError,canonical

def enabled(): return os.getenv('ARTI_AGENTS_ENABLED','0').lower() in ('1','true','yes')

TASK_DELIVERY_BYTES=12*1024**2
TASK_DELIVERY_FILES=64


def task_file_payload(outputs):
    """Validate the whole file set before preparing a single transport payload."""
    if not isinstance(outputs,dict) or any(not isinstance(value,dict) for value in outputs.values()): raise MaterialError('task_output_invalid')
    files=[]; total=0
    for output in outputs.values():
        if 'files' not in output: continue
        manifest=output['files']
        if not isinstance(manifest,list) or not manifest: raise MaterialError('task_file_invalid')
        if len(files)+len(manifest)>TASK_DELIVERY_FILES: raise MaterialError('task_delivery_budget')
        for item in manifest:
            if not isinstance(item,dict) or set(item)!={'name','base64','sha256'}: raise MaterialError('task_file_invalid')
            name=item['name']; encoded=item['base64']; digest=item['sha256']
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,149}',name) or name.endswith('.'):
                raise MaterialError('task_file_invalid')
            if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
                raise MaterialError('task_file_invalid')
            if not isinstance(encoded,str) or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest): raise MaterialError('task_file_invalid')
            if len(encoded)>4*((TASK_DELIVERY_BYTES-total+2)//3): raise MaterialError('task_delivery_budget')
            try: data=base64.b64decode(encoded,validate=True)
            except (ValueError,binascii.Error): raise MaterialError('task_file_invalid') from None
            if not data or sha256(data).hexdigest()!=digest or base64.b64encode(data).decode()!=encoded: raise MaterialError('task_file_integrity')
            total+=len(data)
            if total>TASK_DELIVERY_BYTES: raise MaterialError('task_delivery_budget')
            files.append((name,data))
    if not files: return None
    if len(files)==1:
        name,data=files[0]; stream=BytesIO(data); stream.name=name; return stream
    # One ZIP is one durable send. Never start a Telegram file batch that could
    # fail halfway and then be duplicated on a worker restart.
    stream=BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
        for index,(name,data) in enumerate(files,1):
            info=zipfile.ZipInfo(f'{index:03d}-{name}',date_time=(1980,1,1,0,0,0))
            info.external_attr=0o600<<16; archive.writestr(info,data)
            if stream.tell()>TASK_DELIVERY_BYTES: raise MaterialError('task_delivery_budget')
    if stream.tell()>TASK_DELIVERY_BYTES: raise MaterialError('task_delivery_budget')
    stream.seek(0); stream.name='task-results.zip'; return stream


def _presentable_outputs(plan,outputs):
    def references(value):
        if isinstance(value,dict):
            if '$step' in value: return {value['$step']}
            return {ref for item in value.values() for ref in references(item)}
        if isinstance(value,list): return {ref for item in value for ref in references(item)}
        return set()
    ancestors={}; candidates={}; intermediates=set()
    for step in plan['steps']:
        # depends also expresses sequencing. Only explicit dataflow can replace
        # an intermediate artifact/illustration with its finished presentation.
        parents=references(step.get('args',{}))
        ancestors[step['id']]=parents|{parent for dependency in parents for parent in ancestors[dependency]}
        output=outputs.get(step['id'],{})
        artifact=output.get('kind')=='artifact' or (step['tool']=='artifact.patch' and output.get('id'))
        if 'files' in output or artifact: candidates[step['id']]=output
        if artifact or step['tool'].startswith('media.'): intermediates.add(step['id'])
    # Metadata-only checks do not hide files. Requested document/data exports
    # remain deliverable even when later successful work reads them.
    consumed={parent for step in candidates for parent in ancestors[step]}
    return {step:output for step,output in candidates.items() if step not in consumed or step not in intermediates}


async def _delivery_outputs(repo,row,actor):
    # Authorize the reader, not just the original task owner, for every retained
    # output. Each immutable output keeps the plan and full upstream graph.
    async with repo.pool.acquire() as conn:
        calls=await conn.fetch("SELECT DISTINCT ON(step_id) step_id,output_id FROM arti_task_calls WHERE task_id=$1 AND status='success' ORDER BY step_id,attempt DESC",row['id'])
    outputs={}; dependencies=[]
    for call in calls:
        body=await repo.derivatives.load(call['output_id'],actor,'tool_output')
        if body.get('outcome')!='success' or not isinstance(body.get('outputs'),dict): raise MaterialError('task_output_invalid')
        outputs[call['step_id']]=body['outputs']; dependencies.append(call['output_id'])
    return outputs,tuple(dependencies)


async def deliver_task(service,repo,row,bot,key,*,reader=None):
    actor=reader or task_actor(row)
    from bot.work_cards import WorkCards
    cards=WorkCards(service)
    await cards.unattempted(key)
    outputs,dependencies=await _delivery_outputs(repo,row,actor)
    async def guard():
        from agents.scope_guard import guard_scope
        await guard_scope(actor,repo.pool)
        current=await repo.get(row['id'],actor)
        allowed=('succeeded','partial','waiting','paused','unknown','failed') if reader else ('succeeded',)
        if current['status'] not in allowed or current['status']!=row['status']: raise MaterialError('task_not_verified')
        if current['revision']!=row['revision'] or current['plan_id']!=row['plan_id']: raise MaterialError('stale_task_revision')
        p=await repo.projects.get(row['project_id'],actor); p.require('view')
        if p.access_generation!=row['access_generation']: raise MaterialError('task_access_changed')
        await repo.derivatives.load(row['plan_id'],actor,'task_plan')
        _,current_dependencies=await _delivery_outputs(repo,current,actor)
        if current_dependencies!=dependencies: raise MaterialError('task_outputs_changed')
        if actor.scope.chat_type!='private':
            member=await bot.get_chat_member(actor.scope.chat_id,actor.user_id)
            if member.status not in ('member','administrator','creator'): raise MaterialError('task_member_left')
    await guard()
    if not outputs: raise MaterialError('task_output_missing')
    # Present terminal successful outputs. An illustration consumed by an
    # artifact is intermediate; an export consuming that artifact is final.
    plan=await repo.derivatives.load(row['plan_id'],actor,'task_plan')
    steps={step['id']:step for step in plan['steps']}
    final_outputs=_presentable_outputs(plan,outputs)
    output_ids=dict(zip(outputs,dependencies))
    file=task_file_payload(final_outputs)
    if file is None:
        for step_id,output in final_outputs.items():
            artifact_output=output.get('kind')=='artifact' or steps.get(step_id,{}).get('tool')=='artifact.patch'
            if row['status']=='succeeded' and artifact_output and output.get('id'):
                from artifacts.revisions import ArtifactRepository
                artifact=await ArtifactRepository(service.repository).get(output['id'],actor)
                if artifact['revision']!=output.get('revision') or (output.get('derivative_id') is not None and artifact['head']!=output['derivative_id']):
                    raise MaterialError('stale_artifact_revision')
                # workflow.plan omits derivative_id. Its persisted immutable
                # output must still prove the exact rendered revision's head.
                async with repo.pool.acquire() as conn:
                    linked=await conn.fetchval('''WITH RECURSIVE ancestors(id) AS (
                        SELECT $1::text UNION SELECT l.input_id FROM material_derivative_links l JOIN ancestors a ON l.derivative_id=a.id)
                        SELECT 1 FROM ancestors WHERE id=$2''',output_ids[step_id],artifact['head'])
                if not linked: raise MaterialError('task_artifact_dependency_missing')
                await guard()
                await cards.show(artifact,actor,bot,key,extra_guard=guard,dependencies=dependencies); return
        data=canonical(dict(task_id=row['id'],status=row['status'],overall_verified=row['status']=='succeeded',outputs=outputs)).encode()
        if len(data)>TASK_DELIVERY_BYTES: raise MaterialError('task_delivery_budget')
        file=BytesIO(data); file.name='task-result.json'
    caption='Результат готов.' if row['status']=='succeeded' else 'Сохранённые результаты. Задача целиком ещё не проверена.'
    kwargs=dict(chat_id=actor.scope.chat_id,document=file,caption=caption,parse_mode=None)
    if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
    await cards.send(actor,row['project_id'],row['id'],row['revision'],key,bot.send_document,kwargs,guard,dependencies=dependencies)
    await cards.result_panel(bot)

async def agent_worker(bot):
    from materials.runtime import service_for_bot,enabled as materials_enabled
    while True:
        try:
            if enabled() and materials_enabled():
                service=await service_for_bot(); registry=build_registry(); repo=TaskRepository(service.repository,registry); scheduler=Scheduler(service,registry)
                async def authorize(actor):
                    if actor.scope.chat_type!='private':
                        member=await bot.get_chat_member(actor.scope.chat_id,actor.user_id)
                        if member.status not in ('member','administrator','creator'): raise MaterialError('task_member_left')
                await scheduler.tick(); await Executor(repo,service,authorize=authorize).run()
                from bot.work_cards import WorkCards
                await WorkCards(service).refresh_tasks(bot)
                async with repo.pool.acquire() as conn:
                    rows=await conn.fetch("SELECT * FROM arti_tasks WHERE status='succeeded' AND NOT EXISTS(SELECT 1 FROM arti_subscription_runs r WHERE r.task_id=arti_tasks.id) AND NOT EXISTS(SELECT 1 FROM arti_work_delivery d WHERE d.delivery_key='task-result:'||arti_tasks.id) ORDER BY created_at LIMIT 10")
                for row in rows:
                    try: await deliver_task(service,repo,dict(row),bot,'task-result:'+row['id'])
                    except Exception as exc: logging.getLogger(__name__).debug('Task delivery deferred: %s',getattr(exc,'code',type(exc).__name__))
                await subscription_results(service,registry,bot)
        except asyncio.CancelledError: raise
        except Exception as exc: logging.getLogger(__name__).warning('Agent cycle deferred: %s',getattr(exc,'code',type(exc).__name__))
        await asyncio.sleep(5)

async def processing_card_worker(bot):
    from materials.runtime import service_for_bot,enabled as materials_enabled
    from bot.work_cards import WorkCards
    while True:
        try:
            if enabled() and materials_enabled(): await WorkCards(await service_for_bot()).refresh_tasks(bot)
        except asyncio.CancelledError: raise
        except Exception as exc: logging.getLogger(__name__).debug('Processing cards deferred: %s',getattr(exc,'code',type(exc).__name__))
        await asyncio.sleep(5)

async def subscription_results(service,registry,bot):
    repo=TaskRepository(service.repository,registry); subs=SubscriptionRepository(service.repository,registry)
    async with repo.pool.acquire() as conn:
        rows=await conn.fetch("""SELECT r.*,t.id,t.realm,t.scope,t.owner_id,t.project_id,t.access_generation,t.plan_id,t.revision AS task_revision,t.status AS task_status
          FROM arti_subscription_runs r JOIN arti_tasks t ON r.task_id=t.id JOIN arti_workflow_objects o ON o.id=r.subscription_id
          WHERE r.status IN ('pending','ready') AND t.status IN ('succeeded','partial','waiting','failed','unknown') AND o.status='active' LIMIT 10""")
    for row in rows:
        actor=task_actor(row)
        try:
            if row['task_status']!='succeeded':
                async with repo.pool.acquire() as conn:
                    await conn.execute("UPDATE arti_workflow_objects SET status='paused' WHERE id=$1",row['subscription_id'])
                    await conn.execute('UPDATE arti_subscription_cursor SET paused_reason=$2 WHERE subscription_id=$1',row['subscription_id'],'task_'+row['task_status'])
                continue
            outputs=await repo.outputs(row)
            if not await subs.record_result(row['subscription_id'],actor,row['revision'],row['occurrence'],outputs,verified=True): continue
            key=f"subscription:{row['subscription_id']}:{row['revision']}:{row['occurrence'].isoformat()}"
            if actor.scope.chat_type!='private':
                from agents.group_tasks import propose_subscription
                await propose_subscription(service,row,outputs,actor,key,bot); continue
            task=dict(row,revision=row['task_revision'],status='succeeded')
            try:
                async with repo.pool.acquire() as conn: prior=await conn.fetchrow('SELECT * FROM arti_work_delivery WHERE delivery_key=$1',key)
                if not prior: await deliver_task(service,repo,task,bot,key)
                elif prior['status']!='delivered': raise MaterialError('work_delivery_unknown')
                await subs.delivery_result(row['subscription_id'],actor,row['revision'],row['occurrence'],'delivered',key)
            except Exception:
                await subs.delivery_result(row['subscription_id'],actor,row['revision'],row['occurrence'],'unknown',key)
        except Exception: pass
