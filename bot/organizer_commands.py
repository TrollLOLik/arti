"""Private native organizer commands, independent of agent-job /task."""
import re
import shlex
from organizer.runtime import repository
from organizer.time import OrganizerError,scheduled_at,format_scheduled

HELP='''Личный органайзер Арти:
/todo add "Купить чай"
/todo list | /todo list all
/todo done ID | /todo reopen ID | /todo cancel ID
/timezone Europe/Moscow
/remind add "Позвонить" 30m
/remind add "Позвонить" 2026-10-03T09:00 Europe/Moscow
/event add "Встреча" 2026-10-03T09:00+03:00
/event list | /remind list
/event cancel ID | /remind cancel ID
Можно указать относительное время: 30s, 10m, 2h, 1d.
Локальная дата требует часовой пояс. Список показывает до 20 записей; all включает завершённые и отменённые.
Файлы: используйте существующее меню материалов, если оно включено.'''

ERRORS={
    'timezone_required':'Укажи часовой пояс IANA, например Europe/Moscow: /timezone Europe/Moscow.',
    'nonexistent_local_time':'Это местное время отсутствует при переводе часов. Выбери другое время.',
    'ambiguous_local_time':'Это время встречается дважды при переводе часов. Укажи явный UTC-сдвиг, например 2026-10-25T02:30+02:00.',
    'schedule_out_of_range':'Выбери будущее время в пределах 365 дней.',
    'invalid_datetime':'Нужны дата и время ISO: 2026-10-03T09:00, либо интервал 30m.',
    'active_item_limit':'Достигнут лимит 200 активных записей. Заверши или отмени ненужные.',
    'task_operation_only':'done/reopen доступны только личным задачам /todo.',
}


def summary(row):
    title=row['title'][:80]+('…' if len(row['title'])>80 else '')
    due='; '+format_scheduled(row['due_at'],row.get('timezone')) if row.get('due_at') else ''
    notify='; отправка: '+row['notification_state'] if row['kind']!='todo' else ''
    return f"{row['id']} · {title} · {row['state']}{due}{notify}"


def private_owner(update):
    message=update.effective_message
    user=update.effective_user
    chat=update.effective_chat
    if not message or not user or not chat or chat.type!='private' or user.id!=chat.id:
        return None
    sender=getattr(message,'from_user',None)
    if not sender or sender.id!=user.id or getattr(sender,'is_bot',False): return None
    return user.id,chat.id


async def execute(owner,chat,command,args,source_key,*,repo=None,display_timezone=None):
    repo=repo or repository()
    if command=='organizer': return HELP
    if command=='timezone':
        if not args:
            return 'Часовой пояс: '+((await repo.get_timezone(owner,chat)) or 'не задан')
        if len(args)!=1: raise OrganizerError('timezone_required')
        name=await repo.set_timezone(owner,chat,args[0],source_key=source_key)
        return 'Часовой пояс сохранён: '+name+'. Ранее созданные расписания не меняются.'
    kind={'todo':'todo','event':'event','remind':'reminder'}[command]
    action=args[0] if args else 'list'
    if action=='list':
        if len(args)>2 or len(args)==2 and args[1]!='all': return HELP
        rows=await repo.list(owner,chat,kind,include_closed=len(args)==2,limit=20)
        if not rows: return 'Записей нет.'
        lines=['До 20 записей:']
        for row in rows:
            line=summary(row)
            if len('\n'.join(lines))+len(line)+1>3800: break
            lines.append(line)
        return '\n'.join(lines)
    if action=='add':
        existing=await repo.by_source(owner,chat,source_key)
        if existing is not None: return 'Сохранено:\n'+summary(existing)
        if kind=='todo':
            if len(args)!=2: return 'Формат: /todo add "Текст задачи"'
            row=await repo.create(owner,chat,kind,args[1],source_key)
        else:
            if len(args) not in (3,4): return f'Формат: /{command} add "Название" 2026-10-03T09:00 Europe/Moscow или /{command} add "Название" 30m'
            timezone_name=args[3] if len(args)==4 else await repo.get_timezone(owner,chat)
            due,label=scheduled_at(args[2],timezone_name)
            row=await repo.create(owner,chat,kind,args[1],source_key,due_at=due,timezone_name=display_timezone or label)
        return 'Сохранено:\n'+summary(row)
    if action in ('done','reopen','cancel') and len(args)==2:
        # Repository performs the authoritative lookup; command kind is enforced
        # by passing expected_kind rather than revealing another kind's title.
        row=await repo.change(owner,chat,args[1],{'done':'done','reopen':'active','cancel':'cancelled'}[action],expected_kind=kind,source_key=source_key)
        return 'Текущее состояние:\n'+summary(row) if row else 'Запись не найдена.'
    return HELP


async def command(update,context):
    identity=private_owner(update)
    if identity is None:
        await update.effective_message.reply_text('Личный органайзер доступен в личном чате с Арти.',parse_mode=None)
        return
    message=update.effective_message
    try:
        parts=shlex.split(message.text or '')
        name=parts[0].split('@')[0].lstrip('/')
        text=await execute(*identity,name,parts[1:],f'telegram:{identity[1]}:{message.message_id}')
    except (OrganizerError,ValueError) as exc:
        text=ERRORS.get(str(exc),'Проверь команду, название (1–500 символов), дату и часовой пояс. /organizer — помощь.')
    await message.reply_text(text,parse_mode=None)


def register(app):
    from telegram.ext import CommandHandler
    for name in ('organizer','todo','event','remind','timezone'):
        app.add_handler(CommandHandler(name,command))


async def handle_natural(request,bot):
    """Execute private native requests, checkpoint their result before acknowledgement."""
    from organizer.natural import converse,clear_pending,parse
    from bot.request_runtime import checkpoint
    scope=request.get('_telegram_scope')
    text=str(request.get('user_message',''))
    if scope is None or scope.chat_type!='private' or scope.user_id!=scope.chat_id or request.get('user_id')!=scope.user_id:
        if parse(text) is None: return False
        await bot.send_message(chat_id=request['chat_id'],text='Для личных задач и напоминаний открой личный чат с Арти.',parse_mode=None)
        return True
    repo=repository()
    async def perform():
        try:
            operation=await converse(repo,scope.user_id,scope.chat_id,text,f'telegram:{scope.chat_id}:{request["message_id"]}')
            if operation is None: return None
            if isinstance(operation,str): return dict(response=operation,clear_source=None)
            if isinstance(operation,dict):
                response='Сохранено:\n'+summary(operation['existing_item'])
                return dict(response=response,clear_source=operation['source_key'])
            command,args,key,display_zone=operation
            response=await execute(scope.user_id,scope.chat_id,command,args,key,repo=repo,display_timezone=display_zone)
            return dict(response=response,clear_source=key)
        except OrganizerError as exc:
            return dict(response=ERRORS.get(str(exc),'Не удалось сохранить запись: проверь название, дату и часовой пояс.'),clear_source=None)
    outcome=await checkpoint('organizer_result',perform)
    if outcome is None: return False
    await bot.send_message(chat_id=scope.chat_id,text=outcome['response'],parse_mode=None)
    if outcome['clear_source']:
        await clear_pending(repo,scope.user_id,scope.chat_id,outcome['clear_source'])
    return True
