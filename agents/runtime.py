"""Opt-in supervised runtime; offline tests call Executor/Scheduler directly."""
import asyncio,os,logging
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

async def deliver_task(service,repo,row,bot,key,*,reader=None):
    actor=reader or task_actor(row); outputs=await repo.outputs(row)
    from bot.work_cards import WorkCards
    cards=WorkCards(service)
    async def guard():
        from agents.scope_guard import guard_scope
        await guard_scope(actor,repo.pool)
        current=await repo.get(row['id'],actor)
        allowed=('succeeded','partial','waiting','paused','unknown','failed') if reader else ('succeeded',)
        if current['status'] not in allowed or current['status']!=row['status']: raise MaterialError('task_not_verified')
        p=await repo.projects.get(row['project_id'],actor); p.require('view')
        if p.access_generation!=row['access_generation']: raise MaterialError('task_access_changed')
        await repo.derivatives.load(row['plan_id'],actor,'task_plan'); await repo.outputs(row)
        if actor.scope.chat_type!='private':
            member=await bot.get_chat_member(actor.scope.chat_id,actor.user_id)
            if member.status not in ('member','administrator','creator'): raise MaterialError('task_member_left')
    for output in outputs.values():
        if row['status']=='succeeded' and output.get('kind')=='artifact' and output.get('id'):
            from artifacts.revisions import ArtifactRepository
            artifact=await ArtifactRepository(service.repository).get(output['id'],actor)
            await guard(); await cards.show(artifact,actor,bot,key,extra_guard=guard); return
    kwargs=dict(chat_id=actor.scope.chat_id)
    if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
    # One reviewable payload avoids a restart duplicating a partially sent file batch.
    if not outputs: raise MaterialError('task_output_missing')
    data=canonical(dict(task_id=row['id'],status=row['status'],overall_verified=row['status']=='succeeded',outputs=outputs)).encode()
    if len(data)>12*1024**2: raise MaterialError('task_delivery_budget')
    file=BytesIO(data); file.name='task-result.json'; kwargs.update(document=file,caption=('Проверенный результат задачи ' if row['status']=='succeeded' else 'Сохранённые результаты незавершённой задачи ')+row['id'])
    await cards.send(actor,row['project_id'],row['id'],row['revision'],key,bot.send_document,kwargs,guard)

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
