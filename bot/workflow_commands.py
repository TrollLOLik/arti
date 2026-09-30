import shlex
from hashlib import sha256
from io import BytesIO
from materials.runtime import enabled,actor_for_current,service_for_bot
from materials.types import MaterialError,canonical
from projects.repository import ProjectRepository
from projects.workflows import WorkflowRepository
from bot.work_cards import request_sources
from bot.artifact_commands import read_json

HELP='/decision propose "текст"; support|object|confirm|revoke ID VERSION "причина". /assignment offer USER "дело"; accept|decline|complete ID VERSION. /procedure save TASK (reply JSON recipe); run ID (reply inputs); show|pause|resume|delete ID [VERSION]. /subscription new PROCEDURE (reply JSON bindings/schedule); list; show|last|pause|resume|delete ID [VERSION]; change ID VERSION (reply JSON изменений). /scenario new (reply JSON); answer ID VERSION STAGE "ответ"; visual ID.'

def workflow_summary(row):
    b=row['body']; lines=[]
    if row['kind']=='decision':
        lines=['Решение: '+b['text'], 'Статус: '+b['status']]
        if b.get('options'): lines.append('Варианты: '+', '.join(b['options']))
        for p in b.get('positions',[]): lines.append(f"Участник {p['actor_id']}: {p['position']} — {p.get('reason','')}")
        if b.get('confirmation'): lines.append('Подтверждено организатором '+str(b['confirmation']['actor_id'])+'; выбранный вариант: '+str(b['confirmation'].get('option') or 'не указан'))
        lines.append('Общее согласие не выводится из молчания.')
    elif row['kind']=='assignment':
        labels=dict(offered='Предложено',accepted='Принято участником',declined='Участник отказался',completed='Участник подтвердил выполнение')
        lines=['Поручение: '+b['text'],labels.get(b['status'],b['status']), 'Исполнитель: '+str(b['recipient'])]
        if b.get('due'): lines.append('Срок: '+b['due'])
    elif row['kind']=='procedure': lines=['Подтверждённый способ работы: '+b['title'], 'Шагов: '+str(len(b['recipe']['steps']))]
    elif row['kind']=='subscription': lines=['Подписка на процедуру '+b['procedure_id'], 'Часовой пояс: '+b['schedule']['timezone'],'Неизменные выпуски проходят без уведомления.']
    elif row['kind']=='learning':
        completed=sum(p['completed'] for p in b['progress']); lines=[b['title'],f"Пройдено заданий: {completed}/{len(b['stages'])}",'Усвоение отдельно не измерялось.']
        if b['scenario']=='story': lines.append('История является художественной частью.')
    lines.append(f"{row['id']}, версия {row['revision']}; {row['status']}.\nПодробности и источники: /{'scenario' if row['kind']=='learning' else row['kind']} json {row['id']}")
    return '\n'.join(lines)[:3800]

async def workflow_command(update,context):
    message=update.effective_message
    if not enabled(): await message.reply_text('Долговечная работа отключена.'); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); projects=ProjectRepository(service.repository); generic=WorkflowRepository(service.repository)
        parts=shlex.split(message.text); command=parts[0].split('@')[0].lstrip('/'); args=parts[1:]; row=None
        project=await projects.current(actor)
        from agents.tools.core import build_registry
        registry=build_registry()
        if args==['list']:
            kind='learning' if command=='scenario' else command
            async with generic.pool.acquire() as conn: rows=await conn.fetch("SELECT id FROM arti_workflow_objects WHERE realm=$1 AND kind=$2 AND status<>'deleted' LIMIT 50",actor.realm,kind)
            visible=[]
            for r in rows:
                try:
                    x=await generic.get(r['id'],actor,kind); visible.append(f"{x['id']} / v{x['revision']} / {x['status']}")
                except MaterialError: pass
            await message.reply_text('\n'.join(visible) or 'Нет доступных записей.'); return
        if args and args[0] in ('show','json','pause','resume','delete'):
            if args[0] in ('show','json') and len(args)==2:
                row=await generic.get(args[1],actor,'learning' if command=='scenario' else command)
                if command=='subscription' and args[0]=='show':
                    from agents.subscriptions import SubscriptionRepository
                    row,details=await SubscriptionRepository(service.repository,registry).describe(args[1],actor)
                    await message.reply_text('Следующий запуск: '+str(details['next_at'])+'\nПоследний выпуск: '+str(details['last_result'])+'\nПричина паузы: '+str(details['paused_reason']))
            elif len(args)==3:
                if command=='subscription':
                    from agents.subscriptions import SubscriptionRepository
                    row=await SubscriptionRepository(service.repository,registry).manage(args[1],actor,int(args[2]),args[0])
                else: row=await generic.control(args[1],actor,int(args[2]),args[0])
                if row is None: await message.reply_text('Удалено. Связанные запуски и результаты отозваны.'); return
            else: raise MaterialError('workflow_command_invalid')
        elif command=='decision':
            from projects.decisions import DecisionRepository
            repo=DecisionRepository(service.repository)
            if 2<=len(args)<=14 and args[0]=='propose' and project: row=await repo.propose(project.id,actor,args[1],await request_sources(service,actor,message),options=args[2:])
            elif len(args) in (3,4,5): row=await repo.act(args[1],actor,int(args[2]),args[0],option=args[3] if len(args)==5 else None,reason=args[-1] if len(args)>3 else '',sources=await request_sources(service,actor,message))
        elif command=='assignment':
            from projects.assignments import AssignmentRepository
            repo=AssignmentRepository(service.repository)
            if len(args) in (3,4) and args[0]=='offer' and project: row=await repo.offer(project.id,actor,int(args[1]),args[2],await request_sources(service,actor,message),due=args[3] if len(args)==4 else None)
            elif len(args) in (3,4): row=await repo.respond(args[1],actor,int(args[2]),args[0],sources=await request_sources(service,actor,message),reason=args[3] if len(args)==4 else '')
        elif command=='procedure':
            from agents.procedures import ProcedureRepository
            repo=ProcedureRepository(service.repository,registry)
            if len(args)==2 and args[0]=='save': row=await repo.save_success(args[1],actor,await read_json(message,context),await request_sources(service,actor,message),confirmed=True,origin='user')
            elif len(args)==2 and args[0]=='run':
                from agents.tasks import TaskRepository
                plan,procedure=await repo.instantiate(args[1],actor,await read_json(message,context))
                task=await TaskRepository(service.repository,registry).create(actor,procedure['project_id'],plan,await request_sources(service,actor,message),id=sha256(f'procedure-run:{actor.realm}:{message.message_id}'.encode()).hexdigest()[:32])
                await message.reply_text('Запуск сохранён: '+task['id']); return
            elif len(args)==3 and args[0]=='revise': row=await repo.revise_confirmed(args[1],actor,int(args[2]),await read_json(message,context),confirmed=True,origin='user')
            elif len(args)==3 and args[0]=='propose':
                proposal=await repo.propose_change(args[1],actor,int(args[2]),await read_json(message,context),await request_sources(service,actor,message)); current=await repo.get(args[1],actor)
                from bot.work_cards import WorkCards
                async def proposal_guard(): await repo.derivatives.load(proposal,actor,'procedure_proposal')
                kwargs=dict(chat_id=actor.scope.chat_id,text=f'Предлагаемая правка {proposal}. Принятый способ работы продолжает действовать.\nПринять: /procedure approve {args[1]} {args[2]} {proposal}')
                if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
                await WorkCards(service).send(actor,current['project_id'],proposal,int(args[2]),f'procedure-proposal:{actor.realm}:{message.message_id}',context.bot.send_message,kwargs,proposal_guard); return
            elif len(args)==4 and args[0]=='approve': row=await repo.approve_change(args[1],actor,int(args[2]),args[3],await request_sources(service,actor,message))
        elif command=='subscription':
            from agents.subscriptions import SubscriptionRepository
            if len(args)==2 and args[0]=='new':
                value=await read_json(message,context)
                if set(value)-{'bindings','schedule','max_cost','max_calls','max_bytes','until','max_runs'}: raise MaterialError('subscription_input_invalid')
                row=await SubscriptionRepository(service.repository,registry).subscribe(actor,args[1],value['bindings'],value['schedule'],await request_sources(service,actor,message),confirmed=True,origin='user',**{k:v for k,v in value.items() if k.startswith('max_') or k=='until'})
            elif len(args)==3 and args[0]=='change': row=await SubscriptionRepository(service.repository,registry).revise(args[1],actor,int(args[2]),await read_json(message,context),await request_sources(service,actor,message))
            elif len(args)==2 and args[0]=='last':
                _,details=await SubscriptionRepository(service.repository,registry).describe(args[1],actor)
                if not details['last_result'] or not details['last_result']['task_id']: await message.reply_text('Выпуск ещё не подготовлен.'); return
                from agents.runtime import deliver_task
                from agents.tasks import TaskRepository
                tasks=TaskRepository(service.repository,registry); task=await tasks.get(details['last_result']['task_id'],actor)
                await deliver_task(service,tasks,task,context.bot,f'subscription-download:{actor.realm}:{message.message_id}',reader=actor); return
        elif command=='scenario':
            from projects.learning import LearningRepository
            repo=LearningRepository(service.repository)
            if args==['new'] and project:
                v=await read_json(message,context); row=await repo.start(project.id,actor,v['title'],v['stages'],await request_sources(service,actor,message),scenario=v.get('scenario','quest'))
            elif len(args)==5 and args[0]=='answer': row=await repo.answer(args[1],actor,int(args[2]),args[3],args[4],sources=await request_sources(service,actor,message))
            elif len(args)==2 and args[0]=='visual':
                from bot.work_cards import WorkCards
                row=await repo.visualize(args[1],actor,sources=await request_sources(service,actor,message))
                await WorkCards(service).show(row,actor,context.bot,f'scenario-visual:{actor.realm}:{message.message_id}'); return
        if row is None: raise MaterialError('workflow_command_invalid')
        from bot.work_cards import WorkCards
        async def guard():
            fresh=await generic.get(row['id'],actor)
            if fresh['revision']!=row['revision'] or fresh['status']!=row['status']: raise MaterialError('stale_workflow_revision')
        kwargs=dict(chat_id=actor.scope.chat_id)
        if actor.scope.topic_id>0: kwargs['message_thread_id']=actor.scope.topic_id
        if args and args[0]=='json':
            async with generic.pool.acquire() as conn: refs=await generic.derivatives._chain(conn,row['head'],actor)
            body=canonical(dict(id=row['id'],revision=row['revision'],status=row['status'],body=row['body'],sources=refs)).encode(); file=BytesIO(body); file.name=command+'.json'
            kwargs.update(document=file,caption=f"{command} {row['id']}; версия {row['revision']}"); method=context.bot.send_document
        else: kwargs['text']=workflow_summary(row); method=context.bot.send_message
        await WorkCards(service).send(actor,row['project_id'],row['id'],row['revision'],f'workflow-command:{actor.realm}:{message.message_id}',method,kwargs,guard)
    except (MaterialError,ValueError,TypeError,KeyError): await message.reply_text('Нужны актуальная версия, входные данные и права проекта.\n'+HELP)
