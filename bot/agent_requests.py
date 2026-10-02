import re
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace
from materials.types import MaterialError,EvidenceRef

def intent(text):
    from ai.intents import direct_intent
    return direct_intent(text)[0]['work']

async def handle_agent_request(request,bot):
    import os
    if os.getenv('ARTI_AGENTS_ENABLED','0').lower() not in ('1','true','yes'): return False
    if await reply_patch(request,bot): return True
    kind=request['_intent'].get('work') if '_intent' in request else intent(request['user_message'])
    if not kind: return False
    from materials.runtime import actor_for_current,service_for_bot,guard_current,CURRENT_DERIVATIVE_USE,CURRENT_MATERIAL_USE
    from projects.repository import ProjectRepository
    from bot.work_cards import request_sources,WorkCards
    from agents.model_planner import ModelPlanner
    from agents.tools.core import build_registry
    service=await service_for_bot(); actor=await actor_for_current(); projects=ProjectRepository(service.repository); registry=build_registry()
    try:
        project=await projects.current(actor)
        if not project: project=await projects.create(actor,'Работа с материалами',request['user_message'],id=sha256(f'natural-project:{actor.realm}:{request["message_id"]}'.encode()).hexdigest()[:32])
        project.require('edit')
        # The opt-in studio attaches actual selected materials under the current role/CAS.
        attached=await projects.materials_for(project.id,actor); ids={m['asset_id'] for m in attached}
        for use in request.get('_material_uses',()):
            if use.asset_id not in ids:
                project=await projects.attach(project.id,actor,project.revision,use.asset_id); ids.add(use.asset_id)
        from projects.types import ProjectUse
        CURRENT_DERIVATIVE_USE.set(tuple(u for u in CURRENT_DERIVATIVE_USE.get() if not isinstance(u,ProjectUse))+(ProjectUse(project.id,actor,projects,project.access_generation,project.revision),))
        message=SimpleNamespace(chat_id=actor.scope.chat_id,message_id=request['message_id'],text=request['user_message'],caption=None)
        refs=await request_sources(service,actor,message); blocks=[]; numeric=[]
        for id in sorted(ids)[:8]:
            extraction,bundle=await service.extract(id,actor)
            for b in bundle.blocks[:12]: blocks.append(dict(text=b.text[:2500],source=asdict(EvidenceRef(id,bundle.asset_version,extraction,b.block_id,b.locator)),quality=b.quality))
            try:
                for d in await service.datasets(id,actor):
                    for cell in d.cells:
                        if cell.normalized.kind=='number' and not cell.formula:
                            n=cell.normalized; numeric.append(dict(quantity=dict(value=n.value,lower=n.lower or n.value,upper=n.upper or n.value,unit=n.unit),proof=dict(kind='dataset',dataset_id=d.id,address=cell.address)))
                            if len(numeric)>=60: break
            except MaterialError: pass
        from projects.types import WorkflowUse
        workflow_uses=[u for u in CURRENT_DERIVATIVE_USE.get() if isinstance(u,WorkflowUse)]
        known_data=list(dict.fromkeys(n['proof']['dataset_id'] for n in numeric))[:max(0,16-len(workflow_uses))]
        numeric=[n for n in numeric if n['proof']['dataset_id'] in known_data]
        context=dict(project_id=project.id,goal=project.goal,materials=blocks[:30],numeric=numeric[:60],workflows=request.get('_project_context',{}).get('workflows',[]),allowed_input_derivatives=[*[u.head for u in workflow_uses],*known_data])
        while len(__import__('materials.types',fromlist=['canonical']).canonical(context))>55000: context['materials'].pop()
        from agents.tasks import TaskRepository
        plan=dict(goal=request['user_message'],steps=[dict(id='plan',tool='workflow.plan',version='1',args=dict(goal=request['user_message'],kind=kind,context=context),depends=[])],checks=[dict(step='plan',path=['id'],op='nonempty')],inputs=context['allowed_input_derivatives'])
        source_refs=refs+[b['source'] for b in context['materials']]
        row=await TaskRepository(service.repository,registry).create(actor,project.id,plan,source_refs,id=sha256(f'natural-task:{actor.realm}:{request["message_id"]}'.encode()).hexdigest()[:32])
        await WorkCards(service).show_task(row,actor,bot,f'natural-task-card:{actor.realm}:{request["message_id"]}')
        return True
    except MaterialError as exc:
        explanations={'artifact_unit_required':'Укажи единицы измерения.','artifact_quantity_mismatch':'Число не совпало с проверенным расчётом.','artifact_evidence_required':'Нужен источник утверждения.','artifact_cause_claim_not_explicit':'Причинная связь не подтверждена источником.','planner_unavailable':'Планировщик сейчас недоступен.','planner_provider_unavailable':'Планировщик сейчас недоступен.'}
        await bot.send_message(chat_id=actor.scope.chat_id,text=explanations.get(exc.code,'Для результата не хватает проверяемых данных либо изменились права или версии.')+' Подготовленные материалы сохранены в проекте.',**({'message_thread_id':actor.scope.topic_id>0 and actor.scope.topic_id} if actor.scope.topic_id>0 else {})); return True

async def reply_patch(request,bot):
    from materials.runtime import actor_for_current,service_for_bot,CURRENT_DERIVATIVE_USE
    scope=request.get('_telegram_scope'); text=request['user_message']
    if not scope or not scope.reply_to_id or not re.search(r'(?i)синим|синий|удали.*блок|замени.*блок|измени.*блок',text): return False
    actor=await actor_for_current(); service=await service_for_bot()
    async with service.repository.pool.acquire() as conn:
        delivery=await conn.fetchrow('''SELECT d.* FROM arti_work_delivery d JOIN arti_artifacts a ON a.id=d.target_id WHERE d.realm=$1 AND d.receipt=$2 AND d.status='delivered' ORDER BY d.updated_at DESC LIMIT 1''',actor.realm,scope.reply_to_id)
    if not delivery: return False
    from artifacts.revisions import ArtifactRepository
    from projects.repository import ProjectRepository
    from projects.types import ProjectUse
    from bot.work_cards import WorkCards,request_sources
    from types import SimpleNamespace
    try:
        repo=ArtifactRepository(service.repository); row=await repo.get(delivery['target_id'],actor)
        if row['revision']!=delivery['target_revision']: raise MaterialError('stale_artifact_revision')
        p=await ProjectRepository(service.repository).get(row['project_id'],actor); p.require('edit')
        CURRENT_DERIVATIVE_USE.set(tuple(u for u in CURRENT_DERIVATIVE_USE.get() if not isinstance(u,ProjectUse))+(ProjectUse(p.id,actor,ProjectRepository(service.repository),p.access_generation,p.revision),))
        if re.search(r'(?i)синим|синий',text):
            from artifacts.styles import StyleProfile
            style=StyleProfile.from_dict(row['spec'].get('style',{})).to_dict()
            style['accent']='#90AEE8' if style['background']=='#172735' else '#254B85'
            operations=[dict(op='style',value=style)]
        else:
            n=next((int(x) for x in re.findall(r'\b\d+\b',text)),2 if re.search(r'(?i)втор',text) else 1 if re.search(r'(?i)перв',text) else 0)
            if not 1<=n<=len(row['spec']['elements']): raise MaterialError('artifact_patch_target_required')
            e=row['spec']['elements'][n-1]
            if re.search(r'(?i)удали',text): operations=[dict(op='remove',id=e['id'])]
            else:
                quoted=re.search(r'[«"](.+?)[»"]',text)
                if not quoted: raise MaterialError('artifact_replacement_text_required')
                replacement=dict(e,text=quoted.group(1),status='proposed')
                if replacement.get('proof',{}).get('kind')=='quote': replacement.pop('proof')
                operations=[dict(op='replace',id=e['id'],value=replacement)]
        refs=await request_sources(service,actor,SimpleNamespace(chat_id=scope.chat_id,message_id=request['message_id'],text=text,caption=None))
        revised,_=await repo.revise(row['id'],actor,row['revision'],operations,sources=refs)
        await WorkCards(service).show(revised,actor,bot,f'reply-patch:{actor.realm}:{request["message_id"]}')
    except MaterialError as exc:
        await bot.send_message(chat_id=actor.scope.chat_id,text='Укажи номер блока и текст замены в кавычках либо открой актуальную версию: /artifact show '+delivery['target_id'],**({'message_thread_id':actor.scope.topic_id} if actor.scope.topic_id>0 else {}))
    return True
