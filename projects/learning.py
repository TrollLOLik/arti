from projects.workflows import WorkflowRepository
from materials.types import MaterialError
from artifacts.spec import ID

class LearningRepository(WorkflowRepository):
    async def start(self,project_id,actor,title,stages,sources,*,scenario='quest'):
        if scenario not in ('quest','story','visual_review') or not isinstance(title,str) or not 0<len(title)<=150 or not isinstance(stages,list) or not 1<=len(stages)<=30 or len(set(s['id'] for s in stages))!=len(stages): raise MaterialError('scenario_invalid')
        for s in stages:
            if set(s)-{'id','prompt','expected','fiction','proof','label','illustration'} or not ID.fullmatch(s.get('id','')) or not isinstance(s.get('prompt'),str) or not 0<len(s['prompt'])<=2000 or len(s.get('label',''))>200 or (scenario=='story' and s.get('fiction') is not True): raise MaterialError('scenario_fiction_required')
        # Validate source assertions and decorative illustrations before persisting
        # a scenario, rather than waiting for the user to request an export.
        from artifacts.spec import ArtifactSpec
        from artifacts.validation import validate_evidence
        body=dict(title=title,scenario=scenario,stages=stages,progress=[],mastery='not_measured',owner=actor.user_id)
        facts,inputs,_=await validate_evidence(ArtifactSpec(self.visual_spec(body)),actor,self.materials)
        return await self.create(project_id,actor,'learning',body,[*sources,*facts],inputs=inputs)
    async def answer(self,id,actor,expected,stage,answer,*,sources=()):
        old=await self.get(id,actor,'learning'); b=old['body']
        if b['owner']!=actor.user_id: raise MaterialError('scenario_owner_required')
        s=next((x for x in b['stages'] if x['id']==stage),None)
        if not s or len(answer)>2000: raise MaterialError('scenario_stage_invalid')
        completed=answer.strip()==str(s.get('expected','')).strip() if 'expected' in s else bool(answer.strip())
        b['progress']=[p for p in b['progress'] if p['stage']!=stage]+[dict(stage=stage,completed=completed,answer=answer)]
        return await self.update(id,actor,expected,b,sources=sources)
    @staticmethod
    def visual_spec(body):
        elements=[]; illustrations=[]; progress={p['stage']:p for p in body['progress']}
        for n,s in enumerate(body['stages'],1):
            e=dict(id=s['id'],label=s.get('label') or f'Этап {n}',text=s['prompt'],status='fiction' if body['scenario']=='story' else ('observed' if s.get('proof') else 'proposed' if body['scenario']=='quest' else 'unknown'))
            if s.get('proof'): e['proof']=s['proof']
            elements.append(e)
            if s.get('illustration'): illustrations.append(s['illustration'])
            p=progress.get(s['id'])
            if p:
                elements.append(dict(id='progress_'+s['id'][:54],label='Записанный учебный прогресс',text=('Задание выполнено. ' if p['completed'] else 'Задание требует повторения. ')+'Усвоение не измерялось. Ответ: '+p['answer'][:1800],status='proposed'))
        return dict(contract='artifact-1',title=body['title'],format='teaching' if body['scenario']=='quest' else 'cards',elements=elements,relations=[],style={},illustrations=list(dict.fromkeys(illustrations))[:4],questions=[])
    async def visualize(self,id,actor,*,sources=()):
        row=await self.get(id,actor,'learning')
        from artifacts.revisions import ArtifactRepository
        from hashlib import sha256
        async with self.pool.acquire() as conn: refs=await self.derivatives._chain(conn,row['head'],actor)
        refs.extend(sources)
        artifacts=ArtifactRepository(self.materials)
        return await artifacts.create(row['project_id'],actor,self.visual_spec(row['body']),sources=refs,inputs=[row['head']],id=sha256(('scenario:'+id+':'+row['head']).encode()).hexdigest()[:32])
