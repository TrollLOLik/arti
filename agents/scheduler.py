from datetime import datetime,timezone,timedelta
from hashlib import sha256
from materials.types import MaterialScope,AccessContext,MaterialError
from agents.tasks import TaskRepository
from agents.subscriptions import SubscriptionRepository,schedule_after
from agents.procedures import ProcedureRepository

class Scheduler:
    def __init__(self,service,registry): self.service=service; self.registry=registry; self.subscriptions=SubscriptionRepository(service.repository,registry); self.tasks=TaskRepository(service.repository,registry); self.pool=service.repository.pool
    async def tick(self,now=None):
        now=now or datetime.now(timezone.utc); spawned=[]
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("""SELECT o.*,c.next_at,p.scope FROM arti_workflow_objects o JOIN arti_subscription_cursor c ON c.subscription_id=o.id JOIN arti_projects p ON p.id=o.project_id WHERE o.kind='subscription' AND o.status='active' AND p.status='active' AND c.next_at<=$1 ORDER BY c.next_at LIMIT 10""",now)
        from agents.tasks import decoded
        for r in rows:
            actor=AccessContext(MaterialScope(**decoded(r['scope'])),r['owner_id'],'user:'+str(r['owner_id']))
            try:
                sub=await self.subscriptions.get(r['id'],actor,'subscription'); body=sub['body']
                async with self.pool.acquire() as conn:
                    count=await conn.fetchval('SELECT COUNT(*) FROM arti_subscription_runs WHERE subscription_id=$1',sub['id'])
                    pending=await conn.fetchval("SELECT COUNT(*) FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND status IN('pending','ready')",sub['id'],sub['revision'])
                if (body.get('until') and now>=datetime.fromisoformat(body['until'])) or (body.get('max_runs') is not None and count>=body['max_runs']):
                    if pending: continue
                    async with self.pool.acquire() as conn,conn.transaction():
                        await conn.execute("UPDATE arti_workflow_objects SET status='paused' WHERE id=$1 AND revision=$2",sub['id'],sub['revision'])
                        await conn.execute("UPDATE arti_subscription_cursor SET paused_reason='subscription_complete' WHERE subscription_id=$1 AND revision=$2",sub['id'],sub['revision'])
                    continue
                project=await self.subscriptions.projects.get(sub['project_id'],actor); project.require('edit')
                if body['access_generation']!=project.access_generation or body['audience']!=actor.scope.key: raise MaterialError('subscription_access_changed')
                plan,procedure=await ProcedureRepository(self.service.repository,self.registry).instantiate(body['procedure_id'],actor,body['bindings'])
                if procedure['revision']!=body['procedure_revision']: raise MaterialError('subscription_procedure_changed')
                # Collapse missed intervals to the most recent due occurrence; no notification backlog.
                occurrence=r['next_at']; steps=0
                if body['schedule']['kind']=='interval':
                    anchor=datetime.fromisoformat(body['schedule']['anchor']).astimezone(timezone.utc); seconds=body['schedule']['seconds']
                    occurrence=anchor+timedelta(seconds=max(0,int((now-anchor).total_seconds()//seconds))*seconds)
                elif occurrence<now-timedelta(days=9): occurrence=schedule_after(body['schedule'],now-timedelta(days=9))
                while True:
                    nxt=schedule_after(body['schedule'],occurrence)
                    if nxt>now: break
                    occurrence=nxt; steps+=1
                    if steps>10000: raise MaterialError('subscription_missed_budget')
                async with self.pool.acquire() as conn,conn.transaction():
                    object=await conn.fetchrow('SELECT * FROM arti_workflow_objects WHERE id=$1 FOR UPDATE',r['id'])
                    if object['status']!='active' or object['revision']!=sub['revision']: continue
                    await conn.execute('INSERT INTO arti_subscription_runs(subscription_id,revision,occurrence) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',sub['id'],sub['revision'],occurrence)
                    run=await conn.fetchrow('SELECT * FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3',sub['id'],sub['revision'],occurrence)
                if run['task_id']: task_id=run['task_id']
                else:
                    task_id=sha256(f"subscription:{sub['id']}:{sub['revision']}:{occurrence.isoformat()}".encode()).hexdigest()[:32]
                    plan['inputs']=list(dict.fromkeys([*plan.get('inputs',[]),sub['head']]))
                    # The source-bound subscription is itself an input, never an external write grant.
                    async with self.pool.acquire() as conn: refs=await self.subscriptions.derivatives._chain(conn,sub['head'],actor)
                    await self.tasks.create(actor,sub['project_id'],plan,refs,id=task_id,max_calls=body['max_calls'],max_cost=body['max_cost'],max_bytes=body['max_bytes'])
                async with self.pool.acquire() as conn,conn.transaction():
                    await conn.execute('UPDATE arti_subscription_runs SET task_id=$4 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3 AND task_id IS NULL',sub['id'],sub['revision'],occurrence,task_id)
                    await conn.execute('UPDATE arti_subscription_cursor SET next_at=$3 WHERE subscription_id=$1 AND revision=$2 AND next_at<=$4',sub['id'],sub['revision'],nxt,occurrence)
                spawned.append(dict(subscription_id=sub['id'],revision=sub['revision'],occurrence=occurrence,task_id=task_id,actor=actor))
            except Exception as exc:
                async with self.pool.acquire() as conn,conn.transaction():
                    await conn.execute("UPDATE arti_workflow_objects SET status='paused' WHERE id=$1 AND revision=$2",r['id'],r['revision'])
                    await conn.execute('UPDATE arti_subscription_cursor SET paused_reason=$2 WHERE subscription_id=$1',r['id'],getattr(exc,'code','subscription_run_failed'))
        return spawned
