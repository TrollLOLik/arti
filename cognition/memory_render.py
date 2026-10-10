"""Natural-language view of structured memory records for the reply model (H2, M2).

Records stay structured JSON in ``PreparedTurn.memory``: selection, provenance,
inclusion checks and tests use the data. Only the prompt view is prose. Each
line keeps the record's status, modality, attribution, time basis and
uncertainty as words, so the distinctions of MEMORY_CORRECTNESS survive without
field names, identifiers or microsecond timestamps in the prompt.
"""
from datetime import datetime, timezone

MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря')
MONTHS_IN = ('январе', 'феврале', 'марте', 'апреле', 'мае', 'июне', 'июле', 'августе',
             'сентябре', 'октябре', 'ноябре', 'декабре')

SECTION_TITLES = {
    'card': 'Кто это для тебя — постоянная карточка собеседника:',
    'recall': 'Всплыло в памяти по теме текущего сообщения:',
}

MODALITY = {
    'interaction': 'собеседник — тебе',
    'reported': 'рассказ собеседника',
    'quoted': 'цитата чужого текста — не факт о собеседнике',
    'hypothetical': 'гипотетически — не факт',
    'delivered_action': 'твоя (Арти) реплика',
    'source_utterance': 'дословная запись сообщения',
}
ASSERTION = {
    'explicit': 'сказано прямо',
    'inferred': 'твой вывод, прямо не сказано',
    'exception': 'исключение из правила',
    'correction': 'поправка',
}
CONCERN_KIND = {'loss': 'утрата', 'threat': 'угроза', 'conflict': 'конфликт'}
STAGE = {
    'new': 'знакомство недавнее',
    'familiar': 'уже знакомы',
    'close': 'давний собеседник',
}
PREFERENCE = {'text': 'текстом', 'voice': 'голосовые', 'stickers': 'стикеры', 'proactive': 'писать первой'}


def _parse(value):
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def day(moment):
    return f'{moment.day} {MONTHS[moment.month - 1]} {moment.year}'


def ago(moment, now):
    if now is None or moment is None:
        return ''
    days = (now.astimezone(timezone.utc).date() - moment.astimezone(timezone.utc).date()).days
    if days < 0:
        return ''
    if days == 0:
        return 'сегодня'
    if days == 1:
        return 'вчера'
    if days < 7:
        return f'{days} {plural(days, "день", "дня", "дней")} назад'
    if days < 30:
        weeks = days // 7
        return 'неделю назад' if weeks == 1 else f'{weeks} {plural(weeks, "неделю", "недели", "недель")} назад'
    if days < 365:
        months = days // 30
        return 'месяц назад' if months == 1 else f'{months} {plural(months, "месяц", "месяца", "месяцев")} назад'
    return 'больше года назад'


def _dated(moment, now):
    relative = ago(moment, now)
    return f'{day(moment)} ({relative})' if relative else day(moment)


def when(record, now=None):
    """When the source message arrived; never presented as the narrated event's date."""
    occurred = _parse(record.get('occurred_at'))
    observed = _parse(record.get('observed_at'))
    precision = record.get('time_precision')
    if occurred is None:
        if observed is None:
            return 'дата сообщения неизвестна'
        return f'дата сообщения неизвестна, получено около {MONTHS[observed.month - 1]} {observed.year}'
    if precision == 'source_record':
        return f'сообщение от {day(occurred)}, {occurred.astimezone(timezone.utc):%H:%M} UTC'
    if precision == 'month':
        return f'сообщение примерно в {MONTHS_IN[occurred.month - 1]} {occurred.year}'
    if precision == 'year':
        return f'сообщение примерно в {occurred.year} году'
    return f'сообщение от {_dated(occurred, now)}'


def confidence(value):
    if not isinstance(value, (int, float)):
        return ''
    return 'уверенность высокая' if value >= .8 else 'уверенность средняя' if value >= .5 else 'уверенность низкая'


def _quote(text, limit=240):
    text = ' '.join(str(text or '').split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return f'«{text}»'


def _label(value):
    return ' '.join(str(value or '').replace('_', ' ').split())


def _about(record):
    return {'arti': 'О тебе (Арти)', 'person': 'О собеседнике', 'other': 'О другом человеке'}.get(
        record.get('about'), 'Утверждение')


def render_belief(record, now=None):
    parts = [f'{_about(record)}: {_label(record.get("predicate"))} — {_quote(record.get("value"), 160)}']
    if record.get('condition'):
        parts[0] += f' (при условии: {_label(record["condition"])})'
    status = record.get('status')
    validity = []
    start, end = _parse(record.get('valid_from')), _parse(record.get('valid_until'))
    if start:
        validity.append(f'с {day(start)}')
    if end:
        validity.append(f'по {day(end)}')
    if status == 'current':
        parts.append('действует' + (' ' + ' '.join(validity) if validity else ''))
    elif status:
        parts.append('устарело' + (' ' + ' '.join(validity) if validity else ''))
    if record.get('supersedes'):
        parts.append('заменило прежнее значение')
    if record.get('superseded_by'):
        parts.append('позже заменено новым утверждением')
    if record.get('assertion') in ASSERTION:
        parts.append(ASSERTION[record['assertion']])
    if confidence(record.get('confidence')):
        parts.append(confidence(record.get('confidence')))
    return ' · '.join(parts)


def _source_kind(record):
    if record.get('author_id') == 'arti' or record.get('modality') == 'delivered_action':
        return 'твоя (Арти) реплика'
    if record.get('visibility') == 'observed_public_only' or record.get('sender_kind'):
        sender = 'от имени канала' if record.get('sender_kind') == 'chat' else (
            f'от участника {record["author_id"]}' if isinstance(record.get('author_id'), int) else 'от участника')
        return f'сообщение в группе {sender}'
    return MODALITY.get(record.get('modality'), 'сообщение собеседника')


def render_recollection(record, now=None):
    parts = [when(record, now), _source_kind(record)]
    if record.get('evidence_status') == 'observed_message_excerpt_not_personal_fact':
        parts.append('фрагмент сообщения — не утверждение о человеке')
    details = [d for d in record.get('details') or () if d.get('text')]
    if details:
        texts = []
        for detail in details:
            quoted = _quote(detail['text'])
            texts.append(quoted + (' (дословно)' if detail.get('verbatim_verified') else ''))
        parts.append('; '.join(texts))
    else:
        parts.append('что-то знакомое, но детали не вспоминаются')
    if record.get('source_prefix'):
        parts.append('начало того сообщения: ' + _quote(record['source_prefix'], 200))
    if record.get('interpretation'):
        parts.append('позже понято так: ' + _quote(record['interpretation'], 200))
    for history in record.get('belief_history') or ():
        label = _label(history.get('predicate'))
        if history.get('status') == 'current':
            parts.append(f'по теме «{label}» это утверждение действует')
        else:
            until = _parse(history.get('valid_until'))
            parts.append(f'по теме «{label}» это утверждение устарело' + (f' с {day(until)}' if until else '')
                         + ' — есть более новое')
    return ' · '.join(parts)


def _stage_line(record, now):
    parts = []
    first = _parse(record.get('first_contact'))
    if first:
        parts.append(f'знакомы с {_dated(first, now)}')
    messages = record.get('messages')
    if isinstance(messages, int) and messages > 0:
        parts.append(f'{messages} {plural(messages, "сообщение", "сообщения", "сообщений")} от собеседника')
    days = record.get('days')
    if isinstance(days, int) and days > 0:
        parts.append(f'разговоров в {days} {plural(days, "день", "дня", "дней")}')
    if record.get('stage') in STAGE:
        parts.append(STAGE[record['stage']])
    if record.get('warm'):
        parts.append('отношения тёплые')
    preferences = record.get('preferences') or {}
    wanted = [PREFERENCE[k] + (' — да' if v else ' — нет') for k, v in sorted(preferences.items())
              if k in PREFERENCE and isinstance(v, bool)]
    if wanted:
        parts.append('предпочтения: ' + ', '.join(wanted))
    return 'Знакомство: ' + '; '.join(parts) if parts else 'Знакомство: сведений пока мало'


def _intention_line(record, now):
    # The person's grammatical gender is unknown: wording stays gender-neutral.
    owner = 'Твоё обещание' if record.get('actor') == 'arti' else 'План собеседника'
    if record.get('status') == 'reminder':
        owner = 'Напоминание по просьбе собеседника' if record.get('actor') != 'arti' else 'Ты обещала напомнить'
    text = f'{owner}: {_quote(record.get("description"), 160)}'
    deadline = _parse(record.get('deadline'))
    if deadline:
        text += f' · срок: {day(deadline)}, {deadline.astimezone(timezone.utc):%H:%M} UTC'
    created = _parse(record.get('created_at'))
    if created:
        text += f' · с {_dated(created, now)}'
    return text


def _concern_line(record, now):
    kind = CONCERN_KIND.get(record.get('kind'), 'ожидание')
    text = f'Собеседника тревожило: {_label(record.get("topic")) or "без названия"} ({kind})'
    since = _parse(record.get('since'))
    if since:
        text += f' · с {_dated(since, now)}'
    return text + ' · не известно, решилось ли'


def _episode_line(record, now):
    started = _parse(record.get('started_at'))
    head = f'Разговор {_dated(started, now)}' if started else 'Прошлый разговор'
    messages = record.get('messages')
    if isinstance(messages, int) and messages > 0:
        head += f', {messages} {plural(messages, "реплика", "реплики", "реплик")}'
    parts = [head]
    if record.get('first'):
        parts.append('начало: ' + _quote(record['first'], 160))
    if record.get('last_user'):
        parts.append('под конец собеседник: ' + _quote(record['last_user'], 160))
    if record.get('last_arti'):
        parts.append('ты ответила: ' + _quote(record['last_arti'], 160))
    if len(parts) == 1:
        parts.append('детали расплылись')
    return ' · '.join(parts) + ' (дословные обрывки, не полный протокол)'


def render_card(record, now=None):
    kind = record.get('card')
    if kind == 'acquaintance':
        return _stage_line(record, now)
    if kind == 'belief':
        return render_belief(record, now)
    if kind == 'intention':
        return _intention_line(record, now)
    if kind == 'concern':
        return _concern_line(record, now)
    if kind == 'episode':
        return _episode_line(record, now)
    return ''


def render_record(record, now=None):
    """One record → one prose line. Unknown shapes render conservatively."""
    if not isinstance(record, dict):
        return ''
    if record.get('card'):
        return render_card(record, now)
    if 'predicate' in record and 'value' in record:
        return render_belief(record, now)
    return render_recollection(record, now)
