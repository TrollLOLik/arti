from copy import deepcopy
from projects.workflows import WorkflowRepository
from agents.planner import Plan
from agents.verifier import verify
from materials.types import MaterialError

def bind(value,bindings):
    if isinstance(value,dict):
        if '$input' in value:
            if set(value)!={'$input'} or value['$input'] not in bindings: raise MaterialError('procedure_input_required')
            return deepcopy(bindings[value['$input']])
        return {k:bind(v,bindings) for k,v in value.items()}
    if isinstance(value,list): return [bind(v,bindings) for v in value]
    return value

class ProcedureRepository(WorkflowRepository):
    def __init__(self,materials,registry): super().__init__(materials); self.registry=registry
    async def propose_change(self,id,actor,expected,body,sources,*,reason=''):
        old=await self.get(id,actor,'procedure')
        (await self.projects.get(old['project_id'],actor)).require('edit')
        if old['owner_id']!=actor.user_id or old['revision']!=expected or not sources or len(reason)>1000: raise MaterialError('procedure_change_denied')
        self.validate_recipe(body)
        async with self.pool.acquire() as conn:
            refs=await self.derivatives._chain(conn,old['head'],actor)
            envelope=await self.derivatives._payload(conn,old['head'],actor,'workflow_procedure')
        return await self.derivatives.save(actor,'procedure_proposal',dict(procedure_id=id,expected=expected,owner=actor.user_id,body=body,reason=reason),[*refs,*sources],inputs=envelope.get('inputs',[]))
    async def approve_change(self,id,actor,expected,proposal,sources):
        old=await self.get(id,actor,'procedure'); candidate=await self.derivatives.load(proposal,actor,'procedure_proposal')
        if not sources or old['owner_id']!=actor.user_id or candidate['owner']!=actor.user_id or candidate['procedure_id']!=id or candidate['expected']!=expected: raise MaterialError('procedure_change_denied')
        self.validate_recipe(candidate['body'])
        return await self.update(id,actor,expected,candidate['body'],accept=True,inputs=[proposal],sources=sources)
    def validate_recipe(self,body):
        from agents.tools.registry import validate_schema
        if set(body)-{'title','recipe','input_schema','examples','origin_task','constraints','template'} or not 0<len(body.get('title',''))<=150 or not 1<=len(body.get('examples',[]))<=8: raise MaterialError('procedure_invalid')
        for example in body['examples']:
            if set(example)-{'bindings','expected_tools','outputs'} or not isinstance(example.get('outputs'),dict): raise MaterialError('procedure_example_invalid')
            validate_schema(body['input_schema'],example['bindings']); plan=Plan(bind(body['recipe'],example['bindings']),self.registry)
            if [s['tool'] for s in plan.value['steps']]!=example['expected_tools']: raise MaterialError('procedure_control_case_failed')
            for step in plan.value['steps']:
                if step['id'] not in example['outputs']: raise MaterialError('procedure_control_case_failed')
                validate_schema(self.registry.get(step['tool'],step['version']).output_schema,example['outputs'][step['id']])
            if not verify(plan.value,example['outputs'])['complete']: raise MaterialError('procedure_control_case_failed')
            if any(s.get('grant_id') for s in plan.value['steps']): raise MaterialError('procedure_cannot_inherit_grants')
        return body
    async def save_success(self,task_id,actor,body,sources,*,confirmed,origin):
        from agents.tasks import TaskRepository
        if not confirmed or origin!='user': raise MaterialError('procedure_confirmation_required')
        tasks=TaskRepository(self.materials,self.registry); task=await tasks.get(task_id,actor)
        if task['owner_id']!=actor.user_id or task['status']!='succeeded': raise MaterialError('procedure_success_required')
        outputs=await tasks.outputs(task); original=await self.derivatives.load(task['plan_id'],actor,'task_plan')
        if not verify(original,outputs)['complete']: raise MaterialError('procedure_success_required')
        body=dict(body,origin_task=task_id); self.validate_recipe(body)
        async with self.pool.acquire() as conn: output_ids=await conn.fetch("SELECT output_id FROM arti_task_calls WHERE task_id=$1 AND status='success'",task_id)
        row=await self.create(task['project_id'],actor,'procedure',body,sources,inputs=[task['plan_id'],*[r['output_id'] for r in output_ids]][:32])
        return await self.update(row['id'],actor,1,body,accept=True)
    async def revise_confirmed(self,id,actor,expected,body,*,confirmed,origin):
        old=await self.get(id,actor,'procedure')
        if old['owner_id']!=actor.user_id or not confirmed or origin!='user': raise MaterialError('procedure_confirmation_required')
        self.validate_recipe(body)
        return await self.update(id,actor,expected,body,accept=True)
    async def instantiate(self,id,actor,bindings):
        from agents.tools.registry import validate_schema
        row=await self.get(id,actor,'procedure')
        if row['status']!='active' or row['accepted']!=row['head']: raise MaterialError('procedure_unconfirmed')
        validate_schema(row['body']['input_schema'],bindings)
        plan=Plan(bind(row['body']['recipe'],bindings),self.registry).to_dict()
        # Grants belong to this run's actual requester, never the stored recipe.
        if any(s.get('grant_id') for s in plan['steps']): raise MaterialError('procedure_cannot_inherit_grants')
        plan['inputs']=list(dict.fromkeys([*plan.get('inputs',[]),row['head']]))
        return plan,row
