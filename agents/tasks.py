"""Durable leases, conservative reservations and a write intent before network I/O."""
import uuid,json
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from materials.types import AccessContext,MaterialScope,MaterialError,canonical
from materials.derivatives import DerivativeRepository
from projects.repository import ProjectRepository
from agents.planner import Plan

def decoded(v): return json.loads(v) if isinstance(v,str) else v
def task_actor(row): return AccessContext(MaterialScope(**decoded(row['scope'])),row['owner_id'],'user:'+str(row['owner_id']))

class TaskRepository:
    def __init__(self,materials,registry): self.materials=materials; self.pool=materials.pool; self.registry=registry; self.projects=ProjectRepository(materials); self.derivatives=DerivativeRepository(materials)
    async def create(self,actor,project_id,plan,sources,*,id=None,max_calls=40,max_cost='1',max_bytes=8*1024**2,seconds=600,native_request_id=None):
        plan=Plan(plan,self.registry)
        if native_request_id:
            from agents.native_requests import NativeRequestRepository,RequestScope
            found=await NativeRequestRepository(self.materials).get(native_request_id,actor)
            if not found or native_request_id!=id or found[0]['project_id']!=project_id: raise MaterialError('native_request_identity_conflict')
            if plan.value['goal']!=found[1]['goal']: raise MaterialError('native_request_identity_conflict')
            boundary=RequestScope(self.materials,actor,*found)
            async with self.pool.acquire() as conn:
                original=await self.derivatives._chain(conn,found[0]['binding_id'],actor)
                boundary.assets.update({r['asset_id']:r['asset_version'] for r in original})
                for input_id in plan.value.get('inputs',[]):
                    await boundary.validate_sources(await self.derivatives._chain(conn,input_id,actor))
                    boundary.derivative_ids.add(input_id)
            await boundary.validate_sources(sources)
            await boundary.validate_plan(plan)
        if not 1<=max_calls<=100 or not 0<=Decimal(max_cost)<=100 or not 1024<=max_bytes<=32*1024**2 or not 10<=seconds<=86400: raise MaterialError('task_budget_invalid')
        p=await self.projects.get(project_id,actor); p.require('edit')
        plan_id=await self.derivatives.save(actor,'task_plan',plan.to_dict(),sources,inputs=plan.value.get('inputs',[]))
        id=id or uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); p=await self.projects._get(conn,project_id,actor,lock=True); p.require('edit')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,plan_id,actor))
            old=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1',id)
            if old:
                if old['realm']!=actor.realm or old['owner_id']!=actor.user_id or old['plan_id']!=plan_id or old['project_id']!=project_id or old['native_request_id']!=native_request_id: raise MaterialError('task_identity_conflict')
                return dict(old)
            if await conn.fetchval("SELECT COUNT(*) FROM arti_tasks WHERE realm=$1 AND status IN ('queued','running','waiting','paused')",actor.realm)>=100: raise MaterialError('task_scope_quota')
            await conn.execute('''INSERT INTO arti_tasks(id,realm,scope,owner_id,project_id,access_generation,plan_id,max_calls,max_cost,max_bytes,deadline,max_replans,native_request_id,native_origin_plan_id)
             VALUES($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)''',id,actor.realm,canonical(asdict(actor.scope)),actor.user_id,project_id,p.access_generation,plan_id,max_calls,Decimal(max_cost),max_bytes,datetime.now(timezone.utc)+timedelta(seconds=seconds),plan.value.get('max_replans',2),native_request_id,plan_id if native_request_id else None)
        return await self.get(id,actor)
    async def get(self,id,actor):
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1',id)
            if not row or row['realm']!=actor.realm: raise MaterialError('task_unavailable')
            (await self.projects._get(conn,row['project_id'],actor)).require('view',active=False)
        return dict(row)
    async def control(self,id,actor,expected,action):
        states={'pause':'paused','resume':'queued','cancel':'cancelled'}
        if action not in states: raise MaterialError('task_control_invalid')
        old=await self.get(id,actor)
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self.projects._get(conn,old['project_id'],actor,lock=True); p.require('edit')
            row=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1 FOR UPDATE',id)
            if row['owner_id']!=actor.user_id and p.role not in ('owner','manager'): raise MaterialError('task_control_denied')
            if row['revision']!=expected or row['status'] in ('succeeded','cancelled'): raise MaterialError('stale_task_revision')
            if action=='resume' and (row['deadline']<=datetime.now(timezone.utc) or row['status'] not in ('paused','waiting','partial')): raise MaterialError('task_resume_denied')
            if action=='resume' and await conn.fetchval("SELECT 1 FROM arti_task_calls WHERE task_id=$1 AND status IN ('started','unknown') AND effect='external'",id): raise MaterialError('external_outcome_unknown')
            await conn.execute('UPDATE arti_tasks SET status=$2,revision=revision+1,lease_token=NULL,lease_until=NULL WHERE id=$1',id,states[action])
        return await self.get(id,actor)
    async def claim(self,id=None,*,lease_seconds=120):
        token=uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            row=await conn.fetchrow("""SELECT * FROM arti_tasks WHERE ($1::text IS NULL OR id=$1) AND (status='queued' OR (status='running' AND lease_until<NOW())) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""",id)
            if not row: return None
            if row['deadline']<=datetime.now(timezone.utc):
                await conn.execute("UPDATE arti_tasks SET status='failed',diagnostics='deadline' WHERE id=$1",row['id']); return None
            unknown=await conn.fetchval("SELECT 1 FROM arti_task_calls WHERE task_id=$1 AND effect='external' AND status IN ('started','unknown')",row['id'])
            if unknown:
                await conn.execute("UPDATE arti_tasks SET status='unknown',diagnostics='external_outcome_unknown',lease_token=NULL,lease_until=NULL WHERE id=$1",row['id'])
                await conn.execute("UPDATE arti_task_calls SET status='unknown' WHERE task_id=$1 AND effect='external' AND status='started'",row['id']); return None
            await conn.execute("UPDATE arti_tasks SET status='running',fence=fence+1,lease_token=$2,lease_until=NOW()+$3*INTERVAL '1 second' WHERE id=$1",row['id'],token,lease_seconds)
            return dict(await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1',row['id']))
    async def replan(self,id,actor,expected,plan,sources,*,reason):
        if reason not in ('new_evidence','tool_error','missing_input'): raise MaterialError('task_replan_reason_invalid')
        old=await self.get(id,actor); plan=Plan(plan,self.registry)
        if old['owner_id']!=actor.user_id: raise MaterialError('task_replan_denied')
        from agents.native_requests import RequestScope
        boundary=await RequestScope.for_task(self.materials,actor,old)
        if boundary:
            await boundary.validate_sources(sources)
            await boundary.validate_plan(plan)
        head=await self.derivatives.save(actor,'task_plan',plan.to_dict(),sources,inputs=plan.value.get('inputs',[]))
        if head==old['plan_id']: raise MaterialError('task_replan_stagnation')
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); p=await self.projects._get(conn,old['project_id'],actor,lock=True); p.require('edit')
            row=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected or row['status'] in ('unknown','cancelled','succeeded') or row['replans']>=row['max_replans'] or row['deadline']<=datetime.now(timezone.utc): raise MaterialError('task_replan_denied')
            if await conn.fetchval("SELECT 1 FROM arti_task_calls WHERE task_id=$1 AND effect='external' AND status IN('started','unknown','success')",id): raise MaterialError('task_replan_external_effect')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,head,actor))
            # Changed plans don't silently reuse outputs computed under different inputs.
            await conn.execute("UPDATE arti_task_calls SET status='obsolete' WHERE task_id=$1 AND status NOT IN('unknown')",id)
            await conn.execute("UPDATE arti_tasks SET plan_id=$2,status='queued',revision=revision+1,fence=fence+1,replans=replans+1,access_generation=$3,lease_token=NULL,lease_until=NULL,diagnostics=$4 WHERE id=$1",id,head,p.access_generation,reason)
        return await self.get(id,actor)
    async def preview_effect(self,id,actor,step_id):
        from agents.planner import resolve_args
        from agents.permissions import content_digest
        row=await self.get(id,actor)
        if row['owner_id']!=actor.user_id: raise MaterialError('task_grant_owner_required')
        (await self.projects.get(row['project_id'],actor)).require('approve')
        plan=await self.derivatives.load(row['plan_id'],actor,'task_plan')
        step=next((s for s in plan['steps'] if s['id']==step_id),None)
        if not step: raise MaterialError('task_step_missing')
        tool=self.registry.get(step['tool'],step['version'])
        if tool.effect!='external': raise MaterialError('task_external_step_required')
        args=resolve_args(step['args'],await self.outputs(row))
        from agents.tools.registry import validate_schema
        validate_schema(tool.input_schema,args)
        return dict(task_id=id,revision=row['revision'],step_id=step_id,tool=tool.name,version=tool.version,args=args,resources=list(tool.resources(args)),recipient=str(tool.recipient(args)),audience=actor.scope.key,cost_ceiling=tool.max_cost,digest=content_digest(tool,args))
    async def authorize_effect(self,id,actor,expected,step_id,digest,request_id):
        from agents.permissions import CapabilityRepository
        preview=await self.preview_effect(id,actor,step_id)
        if preview['revision']!=expected or preview['digest']!=digest: raise MaterialError('task_preview_changed')
        old=await self.get(id,actor); plan=await self.derivatives.load(old['plan_id'],actor,'task_plan')
        tool=self.registry.get(preview['tool'],preview['version'])
        grant=await CapabilityRepository(self.pool).issue(actor,tool,preview['args'],request_id=request_id,expires_at=datetime.now(timezone.utc)+timedelta(minutes=30),max_cost=tool.max_cost,origin='user')
        for step in plan['steps']:
            if step['id']==step_id: step['grant_id']=grant
        async with self.pool.acquire() as conn: refs=await self.derivatives._chain(conn,old['plan_id'],actor)
        head=await self.derivatives.save(actor,'task_plan',plan,refs,inputs=plan.get('inputs',[]))
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,old['project_id'],actor,lock=True)).require('approve')
            row=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected or row['status'] not in ('waiting','partial','queued') or row['owner_id']!=actor.user_id: raise MaterialError('task_authorization_stale')
            if await conn.fetchval("SELECT 1 FROM arti_task_calls WHERE task_id=$1 AND step_id=$2 AND effect='external' AND status IN('started','unknown','success')",id,step_id): raise MaterialError('external_already_attempted')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,head,actor))
            await conn.execute("UPDATE arti_tasks SET plan_id=$2,status='queued',revision=revision+1,fence=fence+1,lease_token=NULL,lease_until=NULL,diagnostics=NULL WHERE id=$1",id,head)
        return await self.get(id,actor)
    async def _fenced(self,conn,lease):
        row=await conn.fetchrow('SELECT *,lease_until>NOW() AS lease_valid FROM arti_tasks WHERE id=$1 FOR UPDATE',lease['id'])
        if not row or row['status']!='running' or not row['lease_valid'] or row['lease_token']!=lease['lease_token'] or row['fence']!=lease['fence'] or row['deadline']<=datetime.now(timezone.utc): raise MaterialError('task_lease_lost')
        return row
    async def reconcile_effect(self,id,actor,step_id,service):
        from agents.planner import resolve_args
        from agents.tools.registry import ToolContext
        row=await self.get(id,actor)
        if row['owner_id']!=actor.user_id or row['status']!='unknown': raise MaterialError('task_reconcile_denied')
        (await self.projects.get(row['project_id'],actor)).require('approve')
        plan=await self.derivatives.load(row['plan_id'],actor,'task_plan'); step=next((s for s in plan['steps'] if s['id']==step_id),None)
        if not step: raise MaterialError('task_step_missing')
        tool=self.registry.get(step['tool'],step['version'])
        if not tool.reconcile: raise MaterialError('reconciliation_unavailable')
        args=resolve_args(step['args'],await self.outputs(row)); token=uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            current=await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1 FOR UPDATE',id)
            if current['status']!='unknown' or current['revision']!=row['revision']: raise MaterialError('task_reconcile_stale')
            call=await conn.fetchrow("SELECT * FROM arti_task_calls WHERE task_id=$1 AND step_id=$2 AND status='unknown' ORDER BY attempt DESC LIMIT 1 FOR UPDATE",id,step_id)
            if not call: raise MaterialError('task_call_missing')
            await conn.execute("UPDATE arti_tasks SET status='running',fence=fence+1,lease_token=$2,lease_until=NOW()+INTERVAL '120 seconds' WHERE id=$1",id,token)
            lease=dict(await conn.fetchrow('SELECT * FROM arti_tasks WHERE id=$1',id))
            await conn.execute("UPDATE arti_task_calls SET status='started',fence=$4 WHERE task_id=$1 AND step_id=$2 AND attempt=$3",id,step_id,call['attempt'],lease['fence'])
        try:
            ctx=ToolContext(actor,service,row['project_id'],id,f'{id}:{step_id}',lambda:self.guard(lease))
            import asyncio
            result=await asyncio.wait_for(tool.reconcile(args,ctx,ctx.idempotency_key),60)
            if result.outcome!='success': raise MaterialError('reconciliation_unavailable')
            await self.complete(lease,step,call['attempt'],result); await self.finish(lease,'partial','effect_reconciled_resume_remaining_steps')
        except Exception:
            await self.failure(lease,step,call['attempt'],'reconciliation_unavailable')
            raise MaterialError('reconciliation_unavailable') from None
        return await self.get(id,actor)
    async def guard(self,lease):
        actor=task_actor(lease)
        from agents.native_requests import RequestScope
        await RequestScope.for_task(self.materials,actor,lease)
        from agents.scope_guard import guard_scope
        await guard_scope(actor,self.pool)
        p=await self.projects.get(lease['project_id'],actor); p.require('edit')
        if p.access_generation!=lease['access_generation']: raise MaterialError('task_access_changed')
        await self.derivatives.load(lease['plan_id'],actor,'task_plan')
        async with self.pool.acquire() as conn:
            stale=await conn.fetchval('''WITH RECURSIVE ancestors(id) AS(SELECT $1::text UNION SELECT l.input_id FROM material_derivative_links l JOIN ancestors a ON l.derivative_id=a.id)
             SELECT 1 FROM ancestors a JOIN arti_workflow_versions v ON v.head=a.id JOIN arti_workflow_objects o ON o.id=v.object_id
             WHERE o.kind IN('procedure','subscription') AND (o.status<>'active' OR o.head<>a.id) LIMIT 1''',lease['plan_id'])
            run_stale=await conn.fetchval('''SELECT 1 FROM arti_subscription_runs r JOIN arti_workflow_objects o ON o.id=r.subscription_id WHERE r.task_id=$1 AND (o.status<>'active' OR o.revision<>r.revision)''',lease['id'])
            if stale or run_stale: raise MaterialError('task_workflow_changed')
        async with self.pool.acquire() as conn,conn.transaction(): await self._fenced(conn,lease)
    async def heartbeat(self,lease,seconds=120):
        async with self.pool.acquire() as conn,conn.transaction():
            await self._fenced(conn,lease); await conn.execute("UPDATE arti_tasks SET lease_until=NOW()+$2*INTERVAL '1 second' WHERE id=$1",lease['id'],seconds)
    async def outputs(self,lease):
        actor=task_actor(lease); outputs={}
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("SELECT DISTINCT ON(step_id) * FROM arti_task_calls WHERE task_id=$1 AND status='success' ORDER BY step_id,attempt DESC",lease['id'])
        for row in rows: outputs[row['step_id']]=(await self.derivatives.load(row['output_id'],actor,'tool_output'))['outputs']
        return outputs
    async def begin(self,lease,step,args):
        from agents.permissions import content_digest
        tool=self.registry.get(step['tool'],step['version']); await self.guard(lease)
        async with self.pool.acquire() as conn,conn.transaction():
            row=await self._fenced(conn,lease)
            all_calls=await conn.fetch('SELECT * FROM arti_task_calls WHERE task_id=$1 AND step_id=$2 ORDER BY attempt',lease['id'],step['id'])
            calls=[c for c in all_calls if c['status']!='obsolete' and c['input_digest']==content_digest(tool,args)]
            if calls and (any(c['status']=='success' for c in calls) or (tool.effect!='read' and not (tool.effect=='write' and tool.idempotent)) or len(calls)>=3): raise MaterialError('task_step_not_retryable')
            if row['used_calls']>=row['max_calls'] or row['used_cost']+Decimal(tool.max_cost)>row['max_cost'] or row['used_bytes']+tool.max_bytes>row['max_bytes']: raise MaterialError('task_budget_exhausted')
            attempt=max((c['attempt'] for c in all_calls),default=0)+1
            await conn.execute("INSERT INTO arti_task_calls(task_id,step_id,attempt,fence,tool,version,input_digest,effect,status,reserved_cost) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'started',$9)",lease['id'],step['id'],attempt,lease['fence'],tool.name,tool.version,content_digest(tool,args),tool.effect,Decimal(tool.max_cost))
            await conn.execute('UPDATE arti_tasks SET used_calls=used_calls+1,used_cost=used_cost+$2,used_bytes=used_bytes+$3 WHERE id=$1',lease['id'],Decimal(tool.max_cost),tool.max_bytes)
            return attempt
    async def complete(self,lease,step,attempt,result):
        actor=task_actor(lease); tool=self.registry.get(step['tool'],step['version']); result.validate(tool)
        # Upstream outputs remain linked for physical erasure of intermediate results.
        async with self.pool.acquire() as conn: previous=await conn.fetch('SELECT output_id FROM arti_task_calls WHERE task_id=$1 AND step_id=ANY($2::text[]) AND status=\'success\'',lease['id'],step.get('depends',[]))
        output_id=await self.derivatives.save(actor,'tool_output',dict(outcome=result.outcome,outputs=result.outputs,evidence=result.evidence,diagnostics=result.diagnostics),result.evidence,inputs=list(dict.fromkeys([lease['plan_id'],*[r['output_id'] for r in previous],*result.dependencies])))
        async with self.pool.acquire() as conn,conn.transaction():
            row=await self._fenced(conn,lease)
            changed=await conn.fetchval("UPDATE arti_task_calls SET status=$5,output_id=$6,receipt=$7::jsonb WHERE task_id=$1 AND step_id=$2 AND attempt=$3 AND fence=$4 AND status='started' RETURNING task_id",lease['id'],step['id'],attempt,lease['fence'],result.outcome,output_id,canonical(result.receipt) if result.receipt else None)
            if not changed: raise MaterialError('task_call_fence_lost')
            await conn.execute('UPDATE arti_tasks SET used_cost=used_cost-$2+$3,used_bytes=used_bytes-$4+$5 WHERE id=$1',lease['id'],Decimal(tool.max_cost),Decimal(result.cost),tool.max_bytes,len(canonical(result.outputs).encode()))
        return output_id
    async def finish(self,lease,status,diagnostic=None):
        if status not in ('queued','succeeded','partial','waiting','failed','unknown'): raise MaterialError('task_status_invalid')
        async with self.pool.acquire() as conn,conn.transaction():
            await self._fenced(conn,lease)
            await conn.execute('UPDATE arti_tasks SET status=$2,diagnostics=$3,revision=revision+1,lease_token=NULL,lease_until=NULL WHERE id=$1',lease['id'],status,diagnostic)
    async def failure(self,lease,step,attempt,code,*,finish=True):
        effect=self.registry.get(step['tool']).effect
        async with self.pool.acquire() as conn,conn.transaction():
            # Preserve an ambiguous external intent even when the task was cancelled.
            await conn.execute("UPDATE arti_task_calls SET status=$4 WHERE task_id=$1 AND step_id=$2 AND attempt=$3 AND status='started'",lease['id'],step['id'],attempt,'unknown' if effect=='external' else 'failed')
        if not finish: return
        try: await self.finish(lease,'unknown' if effect=='external' else 'partial',code)
        except MaterialError: pass
