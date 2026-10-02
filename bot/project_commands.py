"""Persistent project selection and scoped role commands, no Mini App."""
import shlex
from materials.runtime import enabled,actor_for_current,service_for_bot,capture_document
from materials.types import MaterialError
from projects.repository import ProjectRepository

HELP='/project new "название" "цель"; list; use ID; show [ID]; edit version=N "цель"; member version=N USER role; archive|resume|delete [ID] version=N; attach version=N (reply документ); publish_preview CHAT TOPIC version=N; publish PLAN_ID. Роли: viewer/contributor/editor/manager/approver.'

async def project_command(update,context):
    message=update.effective_message
    if not enabled(): await message.reply_text('Долговечная работа с проектами отключена.'); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); repo=ProjectRepository(service.repository)
        args=shlex.split(message.text)[1:]
        if not args: args=['show']
        if args[0]=='new' and len(args) in (2,3):
            from hashlib import sha256
            id=sha256(f'project-command:{actor.realm}:{actor.user_id}:{getattr(message,"_menu_event_id",message.message_id)}'.encode()).hexdigest()[:32]
            p=await repo.create(actor,args[1],args[2] if len(args)==3 else '',id=id)
        elif args[0]=='list' and len(args)==1:
            projects=await repo.list(actor)
            await message.reply_text('\n'.join(f'{p.id} — {p.title}; {p.status}; v{p.revision}; {p.role}' for p in projects) or 'Нет доступных проектов в этой области.'); return
        elif args[0]=='use' and len(args)==2: p=await repo.select(args[1],actor)
        elif args[0]=='publish' and len(args)==2:
            from projects.publication import ProjectPublication
            publication=ProjectPublication(service); _,body=await publication._load(args[1],actor)
            scope=body['destination']; chat=await context.bot.get_chat(scope['chat_id'])
            member=await context.bot.get_chat_member(chat.id,actor.user_id)
            if chat.type!=scope['chat_type'] or member.status not in ('member','administrator','creator'): raise MaterialError('publication_destination_denied')
            p=await publication.publish(args[1],actor)
            await message.reply_text(f'Проект создан в указанной аудитории: {p.id}, {p.scope.chat_id}, топик {p.scope.topic_id}. Разрешены только проверенные копии; исходные права не расширены.'); return
        else:
            if args[0] in ('show','archive','resume','delete') and len(args)>1 and not args[1].startswith('version='):
                p=await repo.get(args[1],actor); args=[args[0]]+args[2:]
            else: p=await repo.current(actor)
            if p is None: raise MaterialError('project_selection_required')
            if args[0]=='show' and len(args)==1: pass
            elif args[0]=='publish_preview' and len(args)==4 and args[3].startswith('version='):
                from projects.publication import ProjectPublication
                from materials.types import AccessContext,MaterialScope,canonical
                chat=await context.bot.get_chat(int(args[1]))
                member=await context.bot.get_chat_member(chat.id,actor.user_id)
                if chat.type not in ('group','supergroup') or member.status not in ('member','administrator','creator') or (getattr(chat,'is_forum',False) and int(args[2])<=0): raise MaterialError('publication_destination_denied')
                destination=AccessContext(MaterialScope(actor.scope.persona_id,chat.id,int(args[2]),chat.type,actor.scope.mode,actor.scope.scene_id),actor.user_id,actor.sender_ref)
                preview=await ProjectPublication(service).preview(p.id,actor,int(args[3][8:]),destination,f'telegram:{message.chat_id}:{message.message_id}:user')
                from io import BytesIO
                file=BytesIO(canonical(preview).encode()); file.name='publication-preview.json'
                await ProjectPublication(service)._load(preview['id'],actor)
                await message.reply_document(file,caption='Полный состав переноса: цель, аудитория, исходники и версии. Подтвердить этот состав: /project publish '+preview['id']); return
            else:
                if len(args)<2 or not args[1].startswith('version='): raise MaterialError('project_version_required')
                expected=int(args[1][8:])
                if args[0]=='edit' and len(args)==3: p=await repo.edit(p.id,actor,expected,goal=args[2])
                elif args[0]=='member' and len(args)==4: p=await repo.member(p.id,actor,expected,int(args[2]),args[3])
                elif args[0] in ('archive','resume','delete') and len(args)==2:
                    p=await repo.status(p.id,actor,expected,{'archive':'archived','resume':'active','delete':'deleted'}[args[0]])
                    if p is None: await message.reply_text('Проект удалён. Исходные материалы с независимыми правами управляются отдельно через /forget.'); return
                    if p.status=='active': await repo.select(p.id,actor)
                elif args[0]=='attach' and len(args)==2:
                    source=getattr(message,'reply_to_message',None); document=getattr(source,'document',None)
                    if not document: raise MaterialError('project_material_required')
                    text=await capture_document(context,document,source)
                    p=await repo.attach(p.id,actor,expected,text.material_uses[0].asset_id)
                else: raise MaterialError('project_command_invalid')
        materials=await repo.materials_for(p.id,actor)
        from projects.types import ProjectUse
        await ProjectUse(p.id,actor,repo,p.access_generation,p.revision,True).validate()
        from materials.runtime import MaterialUse
        for m in materials:
            if m['status']!='unavailable': await MaterialUse(m['asset_id'],actor,m['current_version'],m['generation'],service).validate()
        await message.reply_text((f'{p.title}\nID {p.id}; версия {p.revision}; {p.status}; роль {p.role}\nЦель: {p.goal[:2000]}\nМатериалов: {len(materials)}; '+', '.join(m.get('filename',m['status'])+' ('+m['status']+')' for m in materials[:5]))[:3900])
    except (MaterialError,ValueError) as exc:
        code=getattr(exc,'code','project_command_invalid')
        await message.reply_text(('Версия проекта изменилась; открой /project show.\n' if code=='stale_project_revision' else 'Не удалось выполнить операцию с указанными правами и областью.\n')+HELP)
