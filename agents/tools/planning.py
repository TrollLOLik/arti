import os
from hashlib import sha256
from agents.tools.registry import Tool,ToolResult,object_schema,STRING
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError
from agents.subscriptions import semantic_fingerprint

def register_planning(registry):
    async def prepare(args,c):
        from agents.tasks import TaskRepository
        from artifacts.revisions import ArtifactRepository
        from agents.model_planner import ModelPlanner
        id=sha256(c.idempotency_key.encode()).hexdigest()[:32]
        async with c.service.repository.pool.acquire() as conn:
            prior=await conn.fetchval('SELECT id FROM arti_artifacts WHERE id=$1',id) if args['kind']=='artifact' else None
        if prior:
            row=await ArtifactRepository(c.service.repository).get(id,c.actor) if args['kind']=='artifact' else await TaskRepository(c.service.repository,registry).get(id,c.actor)
            return ToolResult('success',dict(id=id,kind=args['kind'],revision=row['revision'],content_hash=semantic_fingerprint(row['spec'])),dependencies=(row['head'],))
        from artifacts.validation import validate_evidence
        async def validate_artifact(spec):
            if c.request_scope: await c.request_scope.validate_args('artifact.create',dict(spec=spec.to_dict()))
            await validate_evidence(spec,c.actor,c.service.repository)
        async def validate_plan(plan):
            if c.request_scope: await c.request_scope.validate_plan(plan)
        planner=ModelPlanner(registry,chat_id=c.actor.scope.chat_id); contract=await planner.propose(args['goal'],args['context'],kind='artifact' if args['kind']=='artifact' else 'plan',guard=c.validate,validator=validate_artifact if args['kind']=='artifact' else validate_plan)
        async with c.service.repository.pool.acquire() as conn:
            task=await conn.fetchrow('SELECT plan_id FROM arti_tasks WHERE id=$1',c.task_id); refs=await DerivativeRepository(c.service.repository)._chain(conn,task['plan_id'],c.actor)
        if args['kind']=='artifact':
            from projects.workflows import WorkflowRepository
            async with c.service.repository.pool.acquire() as conn:
                preferred=await conn.fetchval("SELECT id FROM arti_workflow_objects WHERE project_id=$1 AND kind='style' AND owner_id=$2 AND status='active' AND accepted=head ORDER BY created_at DESC,id LIMIT 1",c.project_id,c.actor.user_id)
            if preferred and not c.request_scope:
                from artifacts.spec import ArtifactSpec
                preferences=await WorkflowRepository(c.service.repository).get(preferred,c.actor,'style')
                contract=ArtifactSpec({**contract.to_dict(),'style':preferences['body']['style']})
                async with c.service.repository.pool.acquire() as conn: refs.extend(await DerivativeRepository(c.service.repository)._chain(conn,preferences['head'],c.actor))
            row=await ArtifactRepository(c.service.repository).create(c.project_id,c.actor,contract,id=id,sources=refs); dependency=row['head']
        else:
            if any(s['tool']=='workflow.plan' for s in contract.value['steps']): raise MaterialError('recursive_planning_denied')
            row=await TaskRepository(c.service.repository,registry).get(c.task_id,c.actor); dependency=None
        cost=str(planner.metrics['cost']) if planner.metrics['cost'] is not None else os.getenv('ARTI_PLANNER_COST_CEILING','1')
        diagnostics=() if planner.metrics['cost'] is not None else ('provider_cost_not_reported_reserved_ceiling',)
        outputs=dict(id=id if dependency else c.task_id,kind=args['kind'],revision=row['revision'])
        if not dependency: outputs['plan']=contract.to_dict()
        else: outputs['content_hash']=semantic_fingerprint(row['spec'])
        return ToolResult('success',outputs,diagnostics=diagnostics,cost=cost,dependencies=(dependency,) if dependency else ())
    schema=dict(oneOf=[object_schema(dict(id=STRING,kind=dict(const='artifact'),revision=dict(type='integer',minimum=1),content_hash=STRING)),object_schema(dict(id=STRING,kind=dict(const='task'),revision=dict(type='integer',minimum=1),plan=dict(type='object')))])
    registry.register(Tool('workflow.plan','1',object_schema(dict(goal=STRING,kind=dict(enum=['artifact','task']),context=dict(type='object'))),schema,prepare,'write',timeout=295,max_cost=os.getenv('ARTI_PLANNER_COST_CEILING','1'),max_bytes=500000,idempotent=True))
