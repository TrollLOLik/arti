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
    'stale_item':'Запись уже изменилась. Проверь её в списке и повтори действие с точным ID.',
    'item_not_found':'Запись не найдена. Укажи точный ID своей записи.',
    'item_closed':'Запись уже завершена или отменена. Создай новую запись, если она нужна снова.',
    'task_has_no_schedule':'У задачи нет расписания уведомления. Создай отдельное напоминание с нужным временем.',
    'notification_terminal':'Отправка уже началась, завершилась или её результат неизвестен. Это напоминание нельзя перенести или переименовать; создай новое, если оно нужно.',
    'request_cancelled':'Это уточнение уже отменено. Отправь новую просьбу отдельным сообщением.',
    'source_erased':'Источник записи удалён. Повторное выполнение заблокировано.',
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
    text=str(request.get('_native_user_message',request.get('user_message','')))
    if scope is None or scope.chat_type!='private' or scope.user_id!=scope.chat_id or request.get('user_id')!=scope.user_id or scope.sender_kind!='user' or request.get('chat_id')!=scope.chat_id or type(request.get('message_id')) is not int or request['message_id']<=0 or scope.message_id!=request['message_id'] or scope.topic_id>=0:
        if parse(text) is None: return False
        await bot.send_message(chat_id=request['chat_id'],text='Для личных задач и напоминаний открой личный чат с Арти.',parse_mode=None)
        return True
    repo=repository()
    async def perform():
        from organizer.scenarios import perform as native_perform
        try:
            return await native_perform(repo,scope.user_id,scope.chat_id,text,
                f'telegram:{scope.chat_id}:{request["message_id"]}',reply_to_id=scope.reply_to_id)
        except OrganizerError as exc:
            return dict(response=ERRORS.get(str(exc),'Не удалось сохранить запись: проверь название, дату и часовой пояс.'),clear_source=None)
    outcome=await checkpoint('organizer_result',perform)
    if outcome is None: return False
    from organizer.scenarios import current_outcome
    outcome=await current_outcome(repo,scope.user_id,scope.chat_id,outcome)
    sent=await bot.send_message(chat_id=scope.chat_id,text=outcome['response'],parse_mode=None)
    await repo.record_reply(scope.user_id,scope.chat_id,getattr(sent,'message_id',None),outcome.get('items',[]),source_key=outcome.get('source_key'),generation=outcome.get('generation'))
    if outcome['clear_source']:
        await clear_pending(repo,scope.user_id,scope.chat_id,outcome['clear_source'])
    return True
