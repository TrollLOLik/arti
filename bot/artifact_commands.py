import shlex,json
from hashlib import sha256
from materials.runtime import enabled,actor_for_current,service_for_bot,capture_document
from materials.types import MaterialError
from projects.repository import ProjectRepository
from artifacts.revisions import ArtifactRepository
from bot.work_cards import WorkCards,request_sources

HELP='/artifact create (reply JSON ArtifactSpec); show ID; patch ID VERSION (reply JSON операций); rollback ID VERSION TARGET; accept|reject ID VERSION. /task plan (reply JSON DAG); show|result ID; pause|resume|cancel|revise ID VERSION; preview ID STEP; approve ID VERSION STEP DIGEST.'

async def read_json(message,context):
    source=getattr(message,'reply_to_message',None)
    if not source: raise MaterialError('workflow_json_required')
    if getattr(source,'document',None):
        if source.document.file_size and source.document.file_size>500000: raise MaterialError('workflow_json_budget')
        file=await context.bot.get_file(source.document.file_id); data=bytes(await file.download_as_bytearray())
        if len(data)>500000: raise MaterialError('workflow_json_budget')
        return json.loads(data)
    text=source.text or ''
    if len(text.encode())>500000: raise MaterialError('workflow_json_budget')
    return json.loads(text)

async def artifact_command(update,context):
    message=update.effective_message
    request_id=getattr(message,'_menu_event_id',message.message_id)
    if not enabled(): await message.reply_text('Работа с материалами отключена.'); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); args=shlex.split(message.text)[1:]; repo=ArtifactRepository(service.repository)
        if len(args)==2 and args[0]=='style':
            from artifacts.styles import STYLES
            from projects.workflows import WorkflowRepository
            if args[1] not in STYLES: raise MaterialError('artifact_style_invalid')
            p=await ProjectRepository(service.repository).current(actor)
            if not p: raise MaterialError('project_selection_required')
            styles=WorkflowRepository(service.repository)
            item=await styles.create(p.id,actor,'style',dict(style=STYLES[args[1]].to_dict(),preference_author=actor.user_id),await request_sources(service,actor,message))
            await styles.update(item['id'],actor,1,item['body'],accept=True)
            await message.reply_text('Оформление сохранено для твоих следующих результатов в этом проекте.'); return
        if args==['create']:
            p=await ProjectRepository(service.repository).current(actor)
            if p is None: raise MaterialError('project_selection_required')
            spec=await read_json(message,context); refs=await request_sources(service,actor,message)
            row=await repo.create(p.id,actor,spec,id=sha256(f'artifact:{actor.realm}:{getattr(message,"_menu_event_id",message.message_id)}'.encode()).hexdigest()[:32],sources=refs)
        elif len(args)==2 and args[0]=='show': row=await repo.get(args[1],actor)
        elif len(args)==3 and args[0]=='patch': row,_=await repo.revise(args[1],actor,int(args[2]),await read_json(message,context),sources=await request_sources(service,actor,message))
        elif len(args)==4 and args[0]=='rollback': row,_=await repo.revise(args[1],actor,int(args[2]),rollback=int(args[3]))
        elif len(args)==3 and args[0] in ('accept','reject'): row=await repo.decide(args[1],actor,int(args[2]),'accepted' if args[0]=='accept' else 'rejected')
        else: raise MaterialError('artifact_command_invalid')
        await WorkCards(service).show(row,actor,context.bot,f'work-command:{actor.realm}:{request_id}')
    except (MaterialError,ValueError,TypeError,KeyError): await message.reply_text('Не удалось выполнить операцию с текущей версией и правами.\n'+HELP)

async def task_command(update,context):
    from agents.tasks import TaskRepository
    from agents.tools.core import build_registry
    message=update.effective_message
    request_id=getattr(message,'_menu_event_id',message.message_id)
    if not enabled(): await message.reply_text('Агентские задачи отключены.'); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); repo=TaskRepository(service.repository,build_registry()); args=shlex.split(message.text)[1:]
        if args==['plan']:
            p=await ProjectRepository(service.repository).current(actor)
            if not p: raise MaterialError('project_selection_required')
            refs=await request_sources(service,actor,message)
            row=await repo.create(actor,p.id,await read_json(message,context),refs,id=sha256(f'task:{actor.realm}:{getattr(message,"_menu_event_id",message.message_id)}'.encode()).hexdigest()[:32])
        elif len(args)==2 and args[0]=='show': row=await repo.get(args[1],actor)
        elif len(args)==2 and args[0]=='result':
            from agents.runtime import deliver_task
            row=await repo.get(args[1],actor)
            await deliver_task(service,repo,row,context.bot,f'task-download:{actor.realm}:{request_id}',reader=actor); return
        elif len(args)==3 and args[0]=='preview':
            from io import BytesIO
            from materials.types import canonical
            preview=await repo.preview_effect(args[1],actor,args[2]); file=BytesIO(canonical(preview).encode()); file.name='action-preview.json'
            await message.reply_document(file,caption=f"Состав действия и аудитория. Разрешить: /task approve {args[1]} {preview['revision']} {args[2]} {preview['digest']}"); return
        elif len(args)==5 and args[0]=='approve': row=await repo.authorize_effect(args[1],actor,int(args[2]),args[3],args[4],getattr(message,'_menu_request_source',None) or f'telegram:{message.chat_id}:{message.message_id}:user')
        elif len(args)==3 and args[0]=='reconcile': row=await repo.reconcile_effect(args[1],actor,args[2],service)
        elif len(args)==3 and args[0] in ('pause','resume','cancel'): row=await repo.control(args[1],actor,int(args[2]),args[0])
        elif len(args)==3 and args[0]=='revise': row=await repo.replan(args[1],actor,int(args[2]),await read_json(message,context),await request_sources(service,actor,message),reason='new_evidence')
        else: raise MaterialError('task_command_invalid')
        await WorkCards(service).show_task(row,actor,context.bot,f'task-command:{actor.realm}:{request_id}',force=args[0]=='show')
    except (MaterialError,ValueError,TypeError,KeyError): await message.reply_text('Нужен доступный проект и актуальная версия.\n'+HELP)
