"""Conservative conversational organizer operations, with durable clarification.

Only direct user instructions match. This parser never treats material/history
content or another person's request as authorization.
"""
import json
import re
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from organizer.time import OrganizerError,scheduled_at,zone


async def initialize(conn):
    await conn.execute('''CREATE TABLE IF NOT EXISTS arti_organizer_pending (
        owner_id BIGINT PRIMARY KEY CHECK(owner_id>0), chat_id BIGINT NOT NULL CHECK(chat_id=owner_id),
        source_key TEXT NOT NULL, payload JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL
    )''')
    await conn.execute('CREATE INDEX IF NOT EXISTS arti_organizer_pending_expiry ON arti_organizer_pending(expires_at)')


def _text(value):
    return ' '.join(str(value).strip().split())


def parse(text):
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    if len(lines)>1:
        first=parse(lines[0])
        return dict(first,multiple_lines=True) if first else None
    text=_text(text)
    # Strip courtesies only at the instruction boundary, not arbitrary prefixes.
    text=re.sub(r'(?i)^(?:(?:привет|пожалуйста|арти)[,! ]+)+','',text)
    text=re.sub(r'(?i)^можешь(?:,? пожалуйста)? добавить\b','добавь',text)
    text=re.sub(r'(?i)^можешь(?:,? пожалуйста)? напомнить\b','напомни',text)
    if not text or text[0] in '«"\'>': return None
    if re.match(r'(?i)^(?:не\b|переведи\b|объясни\b|он\b|она\b|мама\b|папа\b|коллега\b)',text): return None
    listing=re.fullmatch(r'(?i)(?:покажи|перечисли|какие у меня)(?: мои)? (задачи|напоминания|события)[?.!]*',text)
    if listing:
        return dict(command={'задачи':'todo','напоминания':'remind','события':'event'}[listing[1].lower()],action='list')
    if _work_request(text): return None
    # Mutations only accept explicit imperatives at the instruction boundary.
    rename=re.fullmatch(r'(?i)переименуй\s+(?:(задачу|напоминание|событие)\s+)?(.+?)\s+(?:в|на)\s+(.+)',text)
    move=re.fullmatch(r'(?i)перенеси\s+(?:(задачу|напоминание|событие)\s+)?(.+?)(?:\s+на\s+(.+))?',text)
    close=re.fullmatch(r'(?i)(отмени|заверши|выполни)\s+(?:(задачу|напоминание|событие)\s+)?(.+)',text)
    if close and close[1].lower()=='выполни' and (close[2] or '').lower()!='задачу': close=None
    if rename or move or close:
        match=rename or move or close
        label=match[2] if close else match[1]
        command={'задачу':'todo','напоминание':'remind','событие':'event'}.get((label or '').lower())
        result=dict(command=command,action='rename' if rename else 'reschedule' if move else 'cancel' if close[1].lower()=='отмени' else 'done',
                    target=(match[3] if close else match[2]).strip(' «»"'))
        if rename: result['title']=rename[3].strip(' «»"')
        if move:
            body=move[3] or ''
            if re.search(r'(?i)\b(?:кажд\w*|ежедневно|еженедельно|ежемесячно|повторяющ\w*)\b',body):
                return dict(command=command,action='unsupported_recurrence')
            extra,when=_split_time(body)
            result['when']=when if not extra else None
        return result
    task=re.match(r'(?i)^(?:добавь|создай|запиши)(?: мне)?(?: задачу| в (?:мои )?задачи)\b\s*[:—-]?\s*(.*)$',text)
    if task: return dict(command='todo',action='add',title=task[1].rstrip('?'))
    event=re.match(r'(?i)^(?:добавь|создай|запиши)(?: мне)? событие\b\s*[:—-]?\s*(.*)$',text)
    reminder=re.match(r'(?i)^напомни(?: мне)?\b\s*[, :—-]?\s*(.*)$',text)
    if reminder and re.match(r'(?i)^(?:что|чем|как|кто|где|когда|почему|о чём|о чем)\b',reminder[1]):
        return None  # Historical recall belongs to memory, not the scheduler.
    if not event and not reminder: return None
    body=(event or reminder)[1]
    if re.search(r'(?i)\b(?:кажд\w*|ежедневно|еженедельно|ежемесячно|повторяющ\w*)\b',body):
        return dict(command='event' if event else 'remind',action='unsupported_recurrence')
    ambiguous=bool(len(re.findall(r'(?i)\bчерез\s+',body))>1 or re.search(r'(?i)\bили\s+(?:через\s+)?(?:\d|завтра|сегодня)',body))
    if ambiguous:
        title=re.sub(r'(?i)\b(?:через\s+)?\d+\s*(?:секунд\w*|минут\w*|час\w*|дн\w*|день)?',' ',body)
        title=re.sub(r'(?i)\bили\b',' ',title).strip(' ,:—-')
        return dict(command='event' if event else 'remind',action='add',title=_text(title),when=None,ambiguous_when=True)
    title,when=_split_time(body)
    if reminder and when is None and body and not re.match(r'(?i)^[а-яё-]+(?:ть|ти|ться)\b',body):
        return None
    return dict(command='event' if event else 'remind',action='add',title=title,when=when)


def _split_time(body):
    relative=re.search(r'(?i)\bчерез\s+(?:(\d+)\s*(секунд\w*|минут\w*|час\w*|дн\w*|день)|полчаса|час)\b',body)
    if relative:
        if relative[1]:
            unit=relative[2].lower(); unit='s' if unit.startswith('сек') else 'm' if unit.startswith('мин') else 'h' if unit.startswith('час') else 'd'
            when=relative[1]+unit
        else: when='30m' if 'полчаса' in relative[0].lower() else '1h'
        return (body[:relative.start()]+' '+body[relative.end():]).strip(' ,:—-'),when
    tomorrow=re.search(r'(?i)\b(завтра|сегодня)\s+в\s+(\d{1,2})(?::(\d{2}))?\b',body)
    if tomorrow:
        when=f'{tomorrow[1].lower()}@{int(tomorrow[2]):02}:{tomorrow[3] or "00"}'
        return (body[:tomorrow.start()]+' '+body[tomorrow.end():]).strip(' ,:—-'),when
    day=re.search(r'(?i)\b(завтра|сегодня)\b',body)
    if day:
        return (body[:day.start()]+' '+body[day.end():]).strip(' ,:—-'),day[1].lower()
    absolute=re.search(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?',body)
    if absolute:
        title=(body[:absolute.start()]+' '+body[absolute.end():]).strip(' ,:—-')
        title=re.sub(r'(?i)\s+в$','',title).strip()
        return title,absolute[0].replace(' ','T')
    date=re.search(r'\b\d{4}-\d{2}-\d{2}\b',body)
    if date: return (body[:date.start()]+' '+body[date.end():]).strip(' ,:—-'),date[0]
    clock=re.search(r'(?i)(?:\bв\s+|^)(\d{1,2}):(\d{2})\b',body)
    if clock:
        return (body[:clock.start()]+' '+body[clock.end():]).strip(' ,:—-'),'clock@'+clock[1].zfill(2)+':'+clock[2]
    if re.fullmatch(r'\d+[smhd]',body.strip(),re.I): return '',body.strip().lower()
    return body.strip(),None


def _zone_name(text):
    aliases={'москва':'Europe/Moscow','мск':'Europe/Moscow','по москве':'Europe/Moscow','московское время':'Europe/Moscow'}
    name=aliases.get(text.casefold().strip(),text.strip())
    zone(name)
    return name


async def _pending(repo,owner,chat):
    async with repo.connection() as conn:
        await conn.execute("UPDATE arti_organizer_turns SET state='cancelled',outcome=NULL WHERE owner_id=$1 AND state='pending' AND root_source_key IN (SELECT source_key FROM arti_organizer_pending WHERE owner_id=$1 AND expires_at<=NOW())",owner)
        await conn.execute('DELETE FROM arti_organizer_pending WHERE owner_id=$1 AND expires_at<=NOW()',owner)
        row=await conn.fetchrow('SELECT * FROM arti_organizer_pending WHERE owner_id=$1 AND chat_id=$2',owner,chat)
    if not row: return None
    result=dict(row); result['payload']=json.loads(result['payload']) if isinstance(result['payload'],str) else result['payload']
    return result


async def clear_pending(repo,owner,chat,source_key=None):
    async with repo.connection() as conn:
        await conn.execute("UPDATE arti_organizer_turns SET state='cancelled',outcome=NULL WHERE owner_id=$1 AND state='pending' AND root_source_key IN (SELECT source_key FROM arti_organizer_pending WHERE owner_id=$1 AND chat_id=$2 AND ($3::text IS NULL OR source_key=$3))",owner,chat,source_key)
        await conn.execute('DELETE FROM arti_organizer_pending WHERE owner_id=$1 AND chat_id=$2 AND ($3::text IS NULL OR source_key=$3)',owner,chat,source_key)


async def _save_pending(repo,owner,chat,key,payload):
    async with repo.connection() as conn:
        await conn.execute('''INSERT INTO arti_organizer_pending VALUES($1,$2,$3,$4::jsonb,NOW()+INTERVAL '15 minutes')
            ON CONFLICT(owner_id) DO UPDATE SET chat_id=EXCLUDED.chat_id,source_key=EXCLUDED.source_key,
            payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at''',owner,chat,key,json.dumps(payload,ensure_ascii=False))


async def converse(repo,owner,chat,text,source_key,*,reply_to_id=None):
    """Return (command,args) for a ready operation, a question, or None."""
    if type(owner) is not int or type(chat) is not int or owner!=chat or owner<=0: return None
    if _work_request(text): return None
    parsed=parse(text)
    pending=await _pending(repo,owner,chat)
    if parsed and parsed['action']=='unsupported_recurrence':
        if pending: await clear_pending(repo,owner,chat)
        return 'Сейчас поддерживаются разовые напоминания и события. Повторяющееся расписание не создано; укажи отдельную дату или интервал для одного напоминания.'
    if parsed and parsed.get('multiple_lines'):
        return 'Вижу несколько строк с возможными просьбами. Уточни одно действие или отправь просьбы отдельными сообщениями.'
    if parsed is None and pending:
        if len([line for line in str(text).splitlines() if line.strip()])>1:
            return 'Для уточнения пришли одно название или один ответ отдельным сообщением.'
        answer=_text(text)
        if re.fullmatch(r'(?i)(?:отмена|отмени|не надо|не нужно|/cancel)[.!]*',answer):
            await clear_pending(repo,owner,chat); return 'Уточнение отменено. Новая запись не создана.'
        if not answer or answer[0] in '/«"\'>' or re.match(r'(?i)^(?:привет|спасибо|как\b|какая\b|какой\b|какие\b|что\b|кто\b|где\b|когда\b|почему\b|расскажи\b|помоги\b|объясни|переведи|мама|коллега|он |она )',answer) or answer.endswith('?'):
            if pending['payload'].get('missing')=='confirm_title':
                paused=dict(pending['payload'],missing='title'); paused.pop('candidate_title',None)
                await _save_pending(repo,owner,chat,pending['source_key'],paused)
            return None
        parsed=dict(pending['payload']); source_key=pending['source_key']
        missing=parsed.pop('missing')
        if missing=='title':
            if not re.match(r'(?i)^[а-яё-]+(?:ть|ти|ться)\b',answer) and not re.match(r'(?i)^(?:название|задача):',answer):
                parsed.update(candidate_title=answer[:500],missing='confirm_title')
                await _save_pending(repo,owner,chat,source_key,parsed)
                return f'Использовать название «{answer[:500]}»? Ответь да или нет.'
            parsed['title']=re.sub(r'(?i)^(?:название|задача):\s*','',answer)
        elif missing=='confirm_title':
            if answer.casefold().strip('!.') in ('да','верно','подтверждаю'):
                parsed['title']=parsed.pop('candidate_title')
            elif answer.casefold().strip('!.')=='нет':
                parsed.pop('candidate_title',None); parsed['missing']='title'
                await _save_pending(repo,owner,chat,source_key,parsed)
                return 'Какое название использовать?'
            else: return None
        elif missing=='when':
            extra,when=_split_time(answer)
            if when is None or extra: return 'Когда напомнить? Например: через 30 минут или 2026-10-03T09:00.'
            parsed['when']=when
            parsed['requested_at']=datetime.now(timezone.utc).isoformat()
        elif missing=='target':
            candidate=next((item for item in parsed.get('candidates',[]) if item['id']==answer),None)
            if not candidate and not parsed.get('candidates') and re.fullmatch(r'[a-f0-9]{12}',answer):
                item=await repo.get(owner,chat,answer,kind={'todo':'todo','event':'event','remind':'reminder'}.get(parsed.get('command')))
                if item: candidate=dict(id=item['id'],version=item['version'])
            if not candidate: return 'Пришли точный ID нужной записи из списка.'
            parsed.update(target=answer,item_id=answer,expected_version=candidate['version'])
        elif missing=='date':
            extra,day=_split_time(answer)
            if extra or not day or not (day in ('завтра','сегодня') or re.fullmatch(r'\d{4}-\d{2}-\d{2}',day)):
                return 'На какую дату? Например завтра или 2026-10-07.'
            parsed['when']=day+'@'+parsed['when'].split('@',1)[1]
        elif missing=='clock':
            clock=re.fullmatch(r'(?:в\s+)?(\d{1,2})(?::(\d{2}))?',answer)
            if not clock or int(clock[1])>23 or int(clock[2] or 0)>59: return 'Во сколько? Например 09:00.'
            parsed['when']+=f'@{int(clock[1]):02}:{int(clock[2] or 0):02}'
        else:
            try: parsed['timezone']=_zone_name(answer)
            except OrganizerError: return 'Нужен часовой пояс: например Europe/Moscow или Москва.'
    if parsed is None: return None
    anchor=pending['payload'].get('requested_at') if pending and source_key==pending['source_key'] else None
    parsed.setdefault('requested_at',anchor or datetime.now(timezone.utc).isoformat())
    if pending and source_key!=pending['source_key'] and parsed['action'] in ('add','rename','reschedule','cancel','done'):
        await clear_pending(repo,owner,chat,pending['source_key'])
    if parsed['action']=='list': return (parsed['command'],['list'],source_key,None)
    if parsed['action'] in ('rename','reschedule','cancel','done'):
        return await _mutation(repo,owner,chat,parsed,source_key,reply_to_id=reply_to_id)
    existing=await repo.by_source(owner,chat,source_key)
    if existing is not None: return {'existing_item':existing,'source_key':source_key}
    command=parsed['command']; title=parsed.get('title','').strip(' "«»')
    if len(title)>500: raise OrganizerError('invalid_item')
    missing=None; display_zone=None
    if not title: missing='title'
    elif command!='todo' and not parsed.get('when'): missing='when'
    args=['add',title]
    if parsed.get('when') in ('завтра','сегодня') or re.fullmatch(r'\d{4}-\d{2}-\d{2}',parsed.get('when') or ''): missing=missing or 'clock'
    if (parsed.get('when') or '').startswith('clock@'): missing=missing or 'date'
    if command!='todo' and parsed.get('when') and missing not in ('clock','date'):
        when=parsed['when']; timezone_name=parsed.get('timezone') or await repo.get_timezone(owner,chat)
        try:
            if '@' in when:
                day,clock=when.split('@',1)
                if not timezone_name: raise OrganizerError('timezone_required')
                local=datetime.fromisoformat(parsed['requested_at']).astimezone(ZoneInfo(timezone_name))
                date=_calendar_date(day,local)
                when=f'{date.isoformat()}T{clock}'
            due,label=scheduled_at(when,timezone_name,now=datetime.fromisoformat(parsed['requested_at']) if re.fullmatch(r'\d+[smhd]',when) else None)
            # Anchor a known relative time across the next clarification turn.
            display_zone=parsed.get('timezone') or label
            parsed['when']=due.isoformat(); parsed['timezone']=display_zone
            args.append(due.isoformat())
        except OrganizerError as exc:
            if str(exc)=='timezone_required':
                missing=missing or 'timezone'
            else: raise
    if missing:
        parsed['missing']=missing
        await _save_pending(repo,owner,chat,source_key,parsed)
        return {'title':'Как назвать задачу?' if command=='todo' else 'О чём напомнить?' if command=='remind' else 'Как назвать событие?',
                'when':('Какое одно время выбрать для напоминания?' if parsed.get('ambiguous_when') else 'Когда напомнить? Например: через 30 минут или 2026-10-03T09:00.'),
                'clock':'Во сколько? Например 09:00.',
                'date':'На какую дату? Например завтра или 2026-10-07.',
                'timezone':'В каком часовом поясе? Например Europe/Moscow или Москва.'}[missing]
    return command,args,source_key,display_zone


async def claim_input(pool,owner,chat,message_id,text,*,mode='default'):
    """Select scheduling authority before cognitive ingestion, never send here."""
    if mode!='default' or type(owner) is not int or type(chat) is not int or owner!=chat or owner<=0 or type(message_id) is not int:
        return False
    from organizer.repository import Repository
    from organizer.ownership import claim_source
    if _work_request(text): return False
    direct=parse(text)
    if direct is not None:
        return await claim_source(pool,owner,chat,message_id)
    pending=await _pending(Repository(pool),owner,chat)
    if pending is None: return False
    answer=_text(text)
    if not answer or answer[0] in '/«"\'>' or re.match(r'(?i)^(?:привет|спасибо|как\b|какая\b|какой\b|какие\b|что\b|кто\b|где\b|когда\b|почему\b|расскажи\b|помоги\b|объясни|переведи|мама|коллега|он |она )',answer) or answer.endswith('?'):
        return False
    root=pending['source_key']
    match=re.fullmatch(r'telegram:'+str(chat)+r':([1-9][0-9]*)',root)
    if not match: return False
    await claim_source(pool,owner,chat,int(match[1]))
    return await claim_source(pool,owner,chat,message_id,root+':user')


async def cleanup_expired(pool,limit=500):
    if type(limit) is not int or not 1<=limit<=1000: raise ValueError('invalid_cleanup_limit')
    async with pool.acquire() as conn:
        owners=await conn.fetch('SELECT owner_id FROM arti_organizer_pending WHERE expires_at<=NOW() ORDER BY expires_at,owner_id LIMIT $1',limit)
        removed=0
        for row in owners:
            async with conn.transaction():
                if not await conn.fetchval('SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))',f'organizer:{row["owner_id"]}'): continue
                await conn.execute("UPDATE arti_organizer_turns SET state='cancelled',outcome=NULL WHERE owner_id=$1 AND state='pending' AND root_source_key IN (SELECT source_key FROM arti_organizer_pending WHERE owner_id=$1 AND expires_at<=NOW())",row['owner_id'])
                removed+=bool(await conn.fetchval('DELETE FROM arti_organizer_pending WHERE owner_id=$1 AND expires_at<=NOW() RETURNING 1',row['owner_id']))
        return removed


async def _mutation(repo,owner,chat,parsed,source_key,*,reply_to_id=None):
    """Resolve only owned native objects; never trust quoted Telegram content."""
    command=parsed.get('command')
    kind={'todo':'todo','event':'event','remind':'reminder'}.get(command)
    if not parsed.get('item_id'):
        candidates=await repo.resolve(owner,chat,parsed.get('target',''),kind=kind,reply_to_id=reply_to_id)
        if len(candidates)!=1:
            if not candidates:
                parsed.update(missing='target',candidates=[])
                # A fresh explicit ID may be supplied even if contextual evidence is absent.
                await _save_pending(repo,owner,chat,source_key,parsed)
                return 'Не удалось однозначно определить запись. Пришли её точный ID из списка задач или напоминаний.'
            parsed.update(missing='target',candidates=[dict(id=r['id'],version=r['version']) for r in candidates])
            await _save_pending(repo,owner,chat,source_key,parsed)
            return 'Есть несколько записей. Пришли точный ID:\n'+'\n'.join(r['id']+' · '+r['title'][:80] for r in candidates[:10])
        item=candidates[0]; parsed.update(item_id=item['id'],expected_version=item['expected_version'])
    item=await repo.get(owner,chat,parsed['item_id'],kind=kind)
    if not item: raise OrganizerError('item_not_found')
    if item['version']!=parsed['expected_version']: raise OrganizerError('stale_item')
    command={'todo':'todo','event':'event','reminder':'remind'}[item['kind']]
    parsed['command']=command
    action=parsed['action']
    if action=='reschedule' and item['kind']=='todo': raise OrganizerError('task_has_no_schedule')
    if action=='done' and item['kind']!='todo': raise OrganizerError('task_operation_only')
    if action=='reschedule':
        when=parsed.get('when'); missing=None
        if not when: missing='when'
        elif when in ('завтра','сегодня') or re.fullmatch(r'\d{4}-\d{2}-\d{2}',when): missing='clock'
        elif when.startswith('clock@'): missing='date'
        if not missing:
            timezone_name=parsed.get('timezone') or await repo.get_timezone(owner,chat)
            try:
                if '@' in when:
                    day,clock=when.split('@',1)
                    if not timezone_name: raise OrganizerError('timezone_required')
                    local=datetime.fromisoformat(parsed['requested_at']).astimezone(ZoneInfo(timezone_name))
                    date=_calendar_date(day,local)
                    when=f'{date.isoformat()}T{clock}'
                due,label=scheduled_at(when,timezone_name,now=datetime.fromisoformat(parsed['requested_at']) if re.fullmatch(r'\d+[smhd]',when) else None)
                parsed.update(when=due.isoformat(),timezone=parsed.get('timezone') or label)
            except OrganizerError as exc:
                if str(exc)=='timezone_required': missing='timezone'
                else: raise
        if missing:
            parsed['missing']=missing
            await _save_pending(repo,owner,chat,source_key,parsed)
            return {'when':'На какое время перенести? Например через 30 минут или 2026-10-07T09:00.',
                    'clock':'Во сколько? Например 09:00.','date':'На какую дату? Например завтра или 2026-10-07.',
                    'timezone':'В каком часовом поясе? Например Europe/Moscow или Москва.'}[missing]
    return dict(mutation=parsed,source_key=source_key)


def _calendar_date(day,local):
    try: return local.date()+timedelta(days=day=='завтра') if day in ('сегодня','завтра') else datetime.fromisoformat(day).date()
    except ValueError: raise OrganizerError('invalid_datetime') from None


def _work_request(text):
    direct=re.sub(r'(?i)^(?:(?:привет|пожалуйста|арти)[,! ]+)+','',_text(text))
    return bool(re.match(r'(?i)^(?:агент[:,]?\s*|выполни задачу[:,]\s*)',direct))
