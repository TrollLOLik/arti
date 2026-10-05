import asyncio
from agents.tasks import task_actor
from agents.planner import Plan,resolve_args
from agents.tools.registry import ToolContext
from agents.permissions import CapabilityRepository
from agents.verifier import verify
from materials.types import MaterialError

class Executor:
    def __init__(self,repository,service,*,authorize=None): self.repository=repository; self.service=service; self.registry=repository.registry; self.authorize=authorize
    async def run(self,id=None):
        lease=await self.repository.claim(id)
        if not lease: return None
        actor=task_actor(lease)
        from cognition.scope import CURRENT_SCOPE,TransportScope
        scope_token=CURRENT_SCOPE.set(TransportScope(actor.scope.chat_id,actor.scope.topic_id,actor.scope.chat_type,actor.user_id,sender_ref=actor.sender_ref))
        async def guard():
            await self.repository.guard(lease)
            if self.authorize: await self.authorize(actor)
        try:
            await guard()
            plan=Plan(await self.repository.derivatives.load(lease['plan_id'],actor,'task_plan'),self.registry).value
            outputs=await self.repository.outputs(lease)
            async def transition():
                for step in plan['steps']:
                    result=outputs.get(step['id'],{})
                    if step['tool']=='workflow.plan' and result.get('kind')=='task' and result.get('plan'):
                        new=Plan(result['plan'],self.registry).to_dict()
                        async with self.repository.pool.acquire() as conn:
                            output=await conn.fetchval("SELECT output_id FROM arti_task_calls WHERE task_id=$1 AND step_id=$2 AND status='success' ORDER BY attempt DESC LIMIT 1",lease['id'],step['id'])
                            refs=await self.repository.derivatives._chain(conn,lease['plan_id'],actor)
                        new['inputs']=list(dict.fromkeys([*new.get('inputs',[]),output]))
                        await self.repository.replan(lease['id'],actor,lease['revision'],new,refs,reason='new_evidence')
                        return True
                return False
            if await transition(): return dict(status='queued',diagnostic='plan_ready')
            async def beat():
                while True:
                    await asyncio.sleep(30); await self.repository.heartbeat(lease)
            heartbeat=asyncio.create_task(beat())
            try:
                errors=[]
                async def execute(step):
                    args=resolve_args(step['args'],outputs); attempt=None
                    try:
                        ctx=ToolContext(actor,self.service,lease['project_id'],lease['id'],f"{lease['id']}:{step['id']}",guard,step.get('grant_id'),CapabilityRepository(self.repository.pool))
                        from agents.native_requests import RequestScope
                        ctx.request_scope=await RequestScope.for_task(self.repository.materials,actor,lease)
                        if ctx.request_scope: await ctx.request_scope.validate_args(step['tool'],args)
                        from agents.tools.registry import validate_schema
                        tool=self.registry.get(step['tool'],step['version']); validate_schema(tool.input_schema,args)
                        if tool.effect=='external':
                            if not ctx.grant_id: raise MaterialError('capability_required')
                            await ctx.grants.validate(ctx.grant_id,actor,tool,args)
                        from agents.resource_locks import effect_lock
                        async with effect_lock(self.repository.pool,tool,args):
                            attempt=await self.repository.begin(lease,step,args)
                            result=await self.registry.call(step['tool'],args,ctx,version=step['version'])
                            await self.repository.complete(lease,step,attempt,result)
                        if result.outcome!='success': return result.outcome
                        outputs[step['id']]=result.outputs; return 'success'
                    except Exception as exc:
                        code=getattr(exc,'code','tool_execution_failed')
                        errors.append((code,attempt is not None and self.registry.get(step['tool']).effect=='external'))
                        if attempt is not None: await self.repository.failure(lease,step,attempt,code,finish=False)
                        return 'failed'
                pending=[s for s in plan['steps'] if s['id'] not in outputs]
                while pending:
                    ready=[s for s in pending if set(s.get('depends',[]))<=outputs.keys()]
                    if not ready: await self.repository.finish(lease,'partial','dependency_stagnation'); return dict(status='partial')
                    # Reads may share a wave; all writes use one resource/expected version at a time.
                    reads=[s for s in ready if self.registry.get(s['tool']).effect=='read']
                    wave=reads[:2] if reads else ready[:1]
                    results=await asyncio.gather(*(execute(s) for s in wave))
                    if any(r!='success' for r in results):
                        status='unknown' if any(e[1] for e in errors) else ('queued' if errors and all(e[0]=='step_resource_busy' for e in errors) else 'waiting' if 'waiting' in results or any(e[0] in ('capability_required','capability_denied') for e in errors) else 'partial')
                        try: await self.repository.finish(lease,status,errors[0][0] if errors else 'tool_incomplete')
                        except MaterialError: pass
                        return dict(status=status,outputs=outputs)
                    if await transition(): return dict(status='queued',diagnostic='plan_ready')
                    pending=[s for s in pending if s['id'] not in outputs]
                check=verify(plan,outputs); status='succeeded' if check['complete'] else 'partial'
                await self.repository.finish(lease,status,None if check['complete'] else 'completion_check_failed')
                return dict(status=status,outputs=outputs,verification=check)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
        except Exception as exc:
            try: await self.repository.finish(lease,'partial',getattr(exc,'code','task_execution_failed'))
            except MaterialError: pass
            return dict(status='partial',diagnostic=getattr(exc,'code','task_execution_failed'))
        finally: CURRENT_SCOPE.reset(scope_token)
