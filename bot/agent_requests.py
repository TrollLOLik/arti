"""Native, durable material requests. Quoted reply text never becomes authority."""
import re
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace
from materials.types import MaterialError,EvidenceRef


def direct_text(request):
    if '_native_user_message' in request: return request['_native_user_message']
    text=request['user_message']
    # Old queued replies did not preserve the direct request separately. Never
    # parse a quoted message's attacker-controlled delimiter to reconstruct it.
    return '' if text.startswith('Исходный текст сообщения:') else text


def intent(text):
    from ai.intents import direct_intent
    return direct_intent(text)[0]['work']


def request_kind(request):
    from ai.intents import direct_intent
    text=direct_text(request)
    from agents.native_requests import direct_spans
    spans=direct_spans(text)
    trusted=' '.join(spans).strip()
    if not trusted: return None
    decision,certain=direct_intent(trusted)
    if certain: return decision['work']
    # Contextual classification cannot turn quoted/code material into authority.
    if len(spans)!=1: return None
    return request.get('_intent',{}).get('work') if request.get('_intent_raw')==text else None


def validate_request_actor(request,actor):
    scope=request.get('_telegram_scope')
    if scope and (scope.chat_id!=actor.scope.chat_id or scope.topic_id!=actor.scope.topic_id or scope.user_id!=actor.user_id):
        raise MaterialError('native_request_scope_mismatch')
    if request.get('user_id',actor.user_id)!=actor.user_id or request.get('chat_id',actor.scope.chat_id)!=actor.scope.chat_id:
        raise MaterialError('native_request_scope_mismatch')


def patch_command(text):
    from agents.native_requests import direct_spans
    unquoted=' '.join(direct_spans(text)).strip()
    unquoted=re.sub(r'(?i)^(?:арти[, ]+)?(?:пожалуйста[, ]+)?','',unquoted).strip()
    return unquoted


def patch_intent(text):
    unquoted=patch_command(text)
    if re.search(r'(?i)\b(?:не|нельзя|пока)\b',unquoted): return False
    return bool(re.match(r'(?i)^(?:(?:сделай|покрась|измени|поменяй)\b.*(?:синим|синий)|(?:удали|замени|измени)\b.*блок)',unquoted))


async def recover_agent_request(request,bot=None,*,resume_reserved=False):
    """Read-only handoff recovery, including delivered/unknown card receipts.

    Never rerun an interrupted patch, issue a new grant, or resend a card here.
    """
    from materials.runtime import actor_for_current,service_for_bot
    from agents.native_requests import request_id,NativeRequestRepository
    from agents.tasks import TaskRepository
    from agents.tools.core import build_registry
    actor=await actor_for_current(); validate_request_actor(request,actor)
    service=await service_for_bot()
    id=request_id(actor,request['message_id'])
    async with service.repository.pool.acquire() as conn:
        exists=await conn.fetchval('SELECT id FROM arti_tasks WHERE id=$1',id)
    if not exists:
        if resume_reserved and bot is not None:
            from agents.runtime import enabled as agents_enabled
            from materials.runtime import enabled as materials_enabled
            binding=await NativeRequestRepository(service.repository).get(id,actor)
            if binding and agents_enabled() and materials_enabled():
                NativeRequestRepository.check_identity(binding[1],direct_text(request),request_kind(request))
                return await handle_agent_request(request,bot,_resume_binding=True)
        return False
    row=await TaskRepository(service.repository,build_registry()).get(id,actor)
    if row['owner_id']!=actor.user_id: raise MaterialError('native_request_unavailable')
    if row.get('native_request_id'):
        binding=await NativeRequestRepository(service.repository).get(id,actor)
        if binding[1]['goal']!=direct_text(request): raise MaterialError('native_request_identity_conflict')
    return True


async def handle_agent_request(request,bot,*,_resume_binding=False):
    from agents.runtime import enabled as agents_enabled
    from materials.runtime import enabled as materials_enabled
    if request.get('_request_mode')=='rp': return False
    kind=request_kind(request)
    scope=request.get('_telegram_scope')
    patch_candidate=bool(scope and scope.reply_to_id and patch_intent(direct_text(request)))
    if not kind and not patch_candidate: return False
    if not agents_enabled() or not materials_enabled():
        chat_id=request.get('chat_id') or getattr(scope,'chat_id',None)
        if chat_id is None:
            from materials.runtime import actor_for_current
            chat_id=(await actor_for_current()).scope.chat_id
        topic=getattr(scope,'topic_id',-1)
        await bot.send_message(chat_id=chat_id,text='Агентские задачи и правки сейчас отключены администратором. Новая задача не создана, изменения не выполнялись.',**({'message_thread_id':topic} if topic>0 else {}))
        return True
    if not _resume_binding and await recover_agent_request(request,bot): return True
    if await reply_patch(request,bot): return True
    if not kind: return False
    from materials.runtime import actor_for_current,service_for_bot,CURRENT_DERIVATIVE_USE
    from projects.repository import ProjectRepository
    from projects.types import ProjectUse
    from bot.work_cards import request_sources,WorkCards
    from agents.tools.core import build_registry
    from agents.tasks import TaskRepository
    from agents.native_requests import NativeRequestRepository,request_id
    service=await service_for_bot(); actor=await actor_for_current()
    if actor.scope.mode!='default' or not actor.user_id: return False
    validate_request_actor(request,actor)
    projects=ProjectRepository(service.repository); registry=build_registry()
    native=NativeRequestRepository(service.repository); id=request_id(actor,request['message_id'])
    text=direct_text(request)
    try:
        binding=await native.get(id,actor)
        if not binding:
            project=await projects.current(actor)
            if not project: project=await projects.create(actor,'Работа с материалами',text,id=sha256(f'natural-project:{actor.realm}:{request["message_id"]}'.encode()).hexdigest()[:32])
            project.require('edit')
            selected=request.get('_material_uses',())
            if selected:
                assets=[]
                for use in selected:
                    if use.actor!=actor: raise MaterialError('native_request_scope_mismatch')
                    await use.validate()
                    assets.append(dict(asset_id=use.asset_id,asset_version=use.version))
            else:
                assets=[dict(asset_id=m['asset_id'],asset_version=m['version']) for m in await projects.materials_for(project.id,actor) if m['status']=='current']
            assets=list({a['asset_id']:a for a in assets}.values())
            if len(assets)>8: raise MaterialError('native_source_selection_too_large')
            message=SimpleNamespace(chat_id=actor.scope.chat_id,message_id=request['message_id'],text=text,caption=None)
            refs=await request_sources(service,actor,message)
            binding=await native.reserve(id,actor,project,text,kind,refs,assets)
        request_row,body=binding
        native.check_identity(body,text,kind)
        project=await projects.get(request_row['project_id'],actor)
        CURRENT_DERIVATIVE_USE.set(tuple(u for u in CURRENT_DERIVATIVE_USE.get() if not isinstance(u,ProjectUse))+(ProjectUse(project.id,actor,projects,project.access_generation,project.revision),))
        blocks=[]; numeric=[]
        for asset in body['assets']:
            extraction,bundle=await service.extract(asset['asset_id'],actor)
            if bundle.asset_version!=asset['asset_version']: raise MaterialError('stale_material_result')
            for b in bundle.blocks[:12]: blocks.append(dict(text=b.text[:2500],source=asdict(EvidenceRef(asset['asset_id'],bundle.asset_version,extraction,b.block_id,b.locator)),quality=b.quality))
            try:
                for d in await service.datasets(asset['asset_id'],actor):
                    for cell in d.cells:
                        if cell.normalized.kind=='number' and not cell.formula and len(numeric)<60:
                            n=cell.normalized; numeric.append(dict(quantity=dict(value=n.value,lower=n.lower or n.value,upper=n.upper or n.value,unit=n.unit),proof=dict(kind='dataset',dataset_id=d.id,address=cell.address)))
            except MaterialError: pass
        known_data=list(dict.fromkeys(n['proof']['dataset_id'] for n in numeric))[:15]
        numeric=[n for n in numeric if n['proof']['dataset_id'] in known_data]
        context=dict(project_id=project.id,materials=blocks[:30],numeric=numeric,workflows=[],allowed_input_derivatives=known_data)
        from materials.types import canonical
        while len(canonical(context))>55000:
            if context['materials']: context['materials'].pop()
            elif context['numeric']: context['numeric'].pop()
            else: raise MaterialError('planner_context_budget')
        inputs=[request_row['binding_id'],*known_data]
        plan=dict(goal=text,steps=[dict(id='plan',tool='workflow.plan',version='1',args=dict(goal=text,kind=kind,context=context),depends=[])],checks=[dict(step='plan',path=['id'],op='nonempty')],inputs=inputs)
        async with service.repository.pool.acquire() as conn:
            source_refs=await native.derivatives._chain(conn,request_row['binding_id'],actor)
        row=await TaskRepository(service.repository,registry).create(actor,project.id,plan,source_refs,id=id,native_request_id=id)
        await WorkCards(service).show_task(row,actor,bot,f'natural-task-card:{actor.realm}:{request["message_id"]}')
        return True
    except MaterialError as exc:
        if exc.code.startswith(('work_delivery_','native_request_identity_','native_request_scope_')): raise
        explanations={'artifact_unit_required':'Укажи единицы измерения.','artifact_quantity_mismatch':'Число не совпало с проверенным расчётом.','artifact_evidence_required':'Нужен источник утверждения.','artifact_cause_claim_not_explicit':'Причинная связь не подтверждена источником.','planner_unavailable':'Планировщик сейчас недоступен.','planner_provider_unavailable':'Планировщик сейчас недоступен.','native_source_selection_too_large':'Выбери не больше восьми материалов для одной задачи.'}
        await bot.send_message(chat_id=actor.scope.chat_id,text=explanations.get(exc.code,'Не удалось подготовить задачу: не хватает проверяемых данных либо изменились права или версии.'),**({'message_thread_id':actor.scope.topic_id} if actor.scope.topic_id>0 else {})); return True

async def reply_patch(request,bot):
    from materials.runtime import actor_for_current,service_for_bot,CURRENT_DERIVATIVE_USE
    scope=request.get('_telegram_scope'); text=direct_text(request); command=patch_command(text)
    if not scope or not scope.reply_to_id or not patch_intent(text): return False
    actor=await actor_for_current(); validate_request_actor(request,actor)
    service=await service_for_bot()
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
        if re.search(r'(?i)синим|синий',command):
            from artifacts.styles import StyleProfile
            style=StyleProfile.from_dict(row['spec'].get('style',{})).to_dict()
            style['accent']='#90AEE8' if style['background']=='#172735' else '#254B85'
            operations=[dict(op='style',value=style)]
        else:
            n=next((int(x) for x in re.findall(r'\b\d+\b',command)),2 if re.search(r'(?i)втор',command) else 1 if re.search(r'(?i)перв',command) else 0)
            if not 1<=n<=len(row['spec']['elements']): raise MaterialError('artifact_patch_target_required')
            e=row['spec']['elements'][n-1]
            if re.match(r'(?i)^удали\b',command): operations=[dict(op='remove',id=e['id'])]
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
