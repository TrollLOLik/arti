"""Typed, resumable forms. Normal use never asks for object IDs or JSON."""
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from html import escape
from materials.types import MaterialError


def field(key, label, prompt=None, kind='text', *, choices=(), optional=False, limit=2000):
    return dict(key=key, label=label, prompt=prompt or label, kind=kind, choices=list(choices), optional=optional, limit=limit)


FILE = field('file', 'Исходный файл', 'Пришли файл сюда. В группе отправь его ответом на меню.', 'file')
SHEET = field('sheet', 'Лист таблицы', 'Название или номер листа. Если лист один, нажми «Единственный лист».',
              optional=True, choices=[('Единственный лист', '')], limit=100)
RANGE = field('range', 'Ячейки', 'Какие ячейки взять? Например: B2:B12.', 'range')
TITLE = field('title', 'Название', 'Как назовём?', limit=150)
REASON = field('reason', 'Причина', 'Почему? Коротко, своими словами.', optional=True)
FORMS = {
    'project_new': [TITLE, field('goal', 'Цель', 'Что хотим получить в этом проекте?', limit=4000)],
    'project_edit': [field('goal', 'Новая цель', 'Какая теперь цель проекта?', limit=4000)],
    'project_attach': [FILE],
    'project_member': [field('user', 'Участник', 'Перешли сообщение участника с открытым автором или пришли его числовой Telegram ID.', 'user'),
        field('role', 'Права', 'Что разрешим участнику?', choices=[('Смотреть', 'viewer'), ('Добавлять материалы', 'contributor'),
            ('Редактировать', 'editor'), ('Управлять проектом', 'manager'), ('Принимать результаты', 'approver')])],
    'project_publish': [field('chat', 'Группа назначения', 'Перешли сообщение целевой группы с открытым источником или укажи её @название. Для закрытой группы можно указать числовой ID.', 'chat'),
        field('topic', 'Тема группы', 'Номер темы. Для группы без тем — 0.', 'integer')],
    'infographic': [field('goal', 'Задание', 'Что показать на инфографике? Возьму материалы текущего проекта. Файлы можно добавить в разделе «Проекты».', limit=4000)],
    'agent': [field('goal', 'Задание', 'Что сделать и какой результат тебе нужен?', limit=4000)],
    'task_revise': [field('goal', 'Новое задание', 'Что нужно изменить в задании? Сохраню новую версию плана.', limit=4000)],
    'dataset': [FILE, SHEET],
    'calc': [FILE, SHEET, RANGE],
    'datafix': [FILE, SHEET, field('cell', 'Ячейка', 'Адрес ячейки, например B2.', 'cell'), field('value', 'Правильное значение', 'Какое значение верное?', limit=200)],
    'transcript': [FILE],
    'listen': [field('segment', 'Реплика', 'Номер реплики из расшифровки, например turn_1.', limit=80)],
    'transcript_fix': [field('segment', 'Реплика', 'Номер реплики, которую исправляем.', limit=80), field('text', 'Исправленный текст', 'Как на самом деле прозвучала реплика?')],
    'storyboard': [FILE],
    'moment': [FILE, field('seconds', 'Момент', 'Сколько секунд от начала? Например 12.5.', 'number')],
    'material_search': [field('query', 'Что найти', 'Какие слова или тему искать в доступных материалах?')],
    'material_review': [FILE, field('position', 'Моя оценка', choices=[('Предпочитаю этот вариант', 'preferred'),
        ('Не подходит', 'rejected'), ('Пока не решил', 'pending'), ('Предлагаю вариант', 'proposal')]),
        field('summary', 'Краткое описание'), REASON],
    'archive': [field('query', 'Тема', 'О чём найти исходную запись?')],
    'forget': [field('query', 'Что забыть', 'Какие сведения или материалы убрать? Сначала покажу найденное — выберешь сам.')],
    'decision_new': [field('text', 'Вопрос', 'Какой вопрос решаем?'), field('options', 'Варианты', 'Каждый вариант с новой строки. От 2 до 12.', 'lines')],
    'decision_support': [REASON], 'decision_object': [REASON],
    'decision_confirm': [REASON], 'decision_revoke': [REASON],
    'assignment_new': [field('user', 'Исполнитель', 'Перешли сообщение исполнителя с открытым автором или пришли его Telegram ID.', 'user'),
        field('text', 'Поручение', 'Что предлагаем сделать?'),
        field('due', 'Срок', 'Дата и время с часовым поясом, например 2026-10-05 18:00 +05:00. Можно без срока.', 'datetime', optional=True, choices=[('Без срока', '')])],
    'assignment_accept': [REASON], 'assignment_decline': [REASON], 'assignment_complete': [REASON],
    'procedure_save': [TITLE], 'procedure_revise': [TITLE],
    'subscription_new': [field('timezone', 'Часовой пояс', 'Например Asia/Yekaterinburg или Europe/Moscow.', 'timezone', choices=[('Екатеринбург', 'Asia/Yekaterinburg'), ('Москва', 'Europe/Moscow')]),
        field('schedule', 'Когда запускать', choices=[('Каждый день', 'daily'), ('По будням', 'weekdays'), ('Через интервал', 'interval')]),
        field('time', 'Время или интервал', 'Для ежедневного запуска: 18:00. Для интервала: число минут.', limit=30),
        field('runs', 'Количество запусков', 'Сколько раз повторить? От 1 до 10000.', 'integer', choices=[('10 раз', '10'), ('30 раз', '30')]),
        field('calls', 'Лимит шагов на запуск', 'От 1 до 100 вызовов инструментов.', 'integer', choices=[('До 20', '20'), ('До 40', '40')]),
        field('cost', 'Лимит затрат на запуск', 'Внутренний лимит допуска вызовов, от 0 до 100. Точный счёт зависит от провайдера.', 'number', choices=[('1', '1'), ('5', '5')])],
    'subscription_change': [],
    'scenario_new': [TITLE, field('scenario', 'Формат', choices=[('Учебный квест', 'quest'), ('Художественная история', 'story'), ('Визуальный обзор', 'visual_review')]),
        field('stages', 'Этапы', 'Напиши этапы. Раздели их пустой строкой. До 30 этапов. Можно прикрепить небольшой текстовый файл. Для обзора непроверенные сведения будут отмечены как неизвестные.', 'stages',limit=15000)],
    'scenario_answer': [field('answer', 'Ответ', 'Твой ответ на выбранный этап.')],
    'artifact_replace': [field('text', 'Новый текст', 'Какой текст поставить в выбранный блок?')],
    'artifact_title': [TITLE],
    'group_timezone': [field('timezone', 'Часовой пояс', 'Например Asia/Yekaterinburg.', 'timezone')],
    'group_limits': [field('daily', 'Сообщений за день', 'Сколько инициативных сообщений в день?', 'integer'),
        field('spacing', 'Пауза между сообщениями', 'Минимальная пауза в минутах.', 'integer')],
    'group_hours': [field('start', 'Начало тихих часов', 'Час от 0 до 23.', 'integer'), field('end', 'Конец тихих часов', 'Час от 0 до 23.', 'integer')],
    'group_assessments': [field('count', 'Оценок за час', 'Сколько раз в час оценивать возможность инициативы?', 'integer')],
}
FORMS['subscription_change'] = FORMS['subscription_new']


def fields(kind, data):
    result = [dict(x) for x in FORMS.get(kind, [])]
    if kind == 'calc':
        if data['operation'] in ('percent', 'change', 'ratio', 'difference', 'compare', 'reconcile'):
            result.append(field('reference', 'С чем сравнить', 'Второй диапазон или ячейка, например C2:C12.', 'range'))
        if data['operation'] == 'convert':
            result.append(field('unit', 'Новая единица', 'Например m или kg.', limit=30))
        result.extend([field('locale', 'Формат чисел', choices=[('Не угадывать', ''), ('Русский', 'ru'), ('Английский', 'en'), ('Немецкий', 'de')], optional=True),
            field('missing', 'Пропущенные значения', choices=[('Остановить расчёт', 'error'), ('Явно исключить', 'exclude')])])
    if kind in ('procedure_run', 'subscription_new'):
        result = binding_fields(data.get('input_schema', {}))+result
    return result


def binding_fields(schema):
    import json
    result=[]
    def walk(value,path,label,required=True):
        if value.get('type')=='object' and value.get('properties') is not None:
            for key,child in value['properties'].items():
                walk(child,[*path,key],(label+' · ' if label else '')+(child.get('title') or key),
                     required and key in value.get('required',[]))
            return
        kind={'number':'number','integer':'integer','boolean':'boolean','array':'binding_list'}.get(value.get('type'),'text')
        if kind=='binding_list' and value.get('items',{}).get('type') in ('object','array'):
            raise MaterialError('menu_complex_binding')
        choices=[(str(x),str(x)) for x in value.get('enum',[])]
        if kind=='boolean':
            choices=[('Да','true'),('Нет','false')]
        f=field('binding:'+json.dumps(path,ensure_ascii=False,separators=(',',':')),label,
                value.get('description') or ('Каждое значение с новой строки.' if kind=='binding_list' else 'Значение для '+label),
                kind,choices=choices,optional=not required)
        f.update(binding_path=path,binding_schema=value)
        result.append(f)
    walk(schema,[],'')
    if len(result)>64:
        raise MaterialError('menu_form_budget')
    return result


def bindings(draft):
    result={}
    for f in draft['fields']:
        if 'binding_path' not in f:
            continue
        value=draft['values'].get(f['key'],'')
        if value=='':
            continue
        if f['binding_schema'].get('type')=='number':
            value=float(value)
        cursor=result
        for part in f['binding_path'][:-1]:
            cursor=cursor.setdefault(part,{})
        cursor[f['binding_path'][-1]]=value
    return result


def parse(f, text, message=None):
    value = str(text).strip()
    if not value and f['optional']:
        return ''
    if f['kind'] == 'file':
        if message is None or not any(getattr(message, k, None) for k in ('document', 'audio', 'voice', 'video', 'photo')):
            raise ValueError('Пришли файл, аудио или видео.')
        # Telegram metadata only; no bytes or secret paths in the form state.
        import json
        raw = message.to_dict()
        raw = {k:v for k,v in raw.items() if k in ('message_id','date','chat','from','document','audio','voice','video','photo','caption')}
        return json.loads(json.dumps(raw, default=lambda x: int(x.timestamp()) if isinstance(x, datetime) else str(x)))
    if f['kind'] == 'user':
        origin = getattr(message, 'forward_origin', None)
        user = getattr(origin, 'sender_user', None)
        if user and not user.is_bot:
            return user.id
        if not re.fullmatch(r'[1-9][0-9]{0,18}', value):
            raise ValueError('Нужен открытый автор пересланного сообщения или числовой ID.')
        return int(value)
    if f['kind']=='chat':
        origin=getattr(message,'forward_origin',None)
        chat=getattr(origin,'chat',None) or getattr(origin,'sender_chat',None)
        if chat and chat.type in ('group','supergroup'):
            return chat.id
        if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}',value):
            return value
        if re.fullmatch(r'-[0-9]{1,18}',value):
            return int(value)
        raise ValueError('Нужна группа: перешли её сообщение, укажи @название или числовой ID.')
    if len(value) > f['limit'] or not value:
        raise ValueError(f"Нужен текст до {f['limit']} символов.")
    if f['choices'] and f['kind'] not in ('number', 'integer', 'timezone'):
        if value not in [str(v) for _, v in f['choices']]:
            raise ValueError('Выбери один из предложенных вариантов.')
    if f['kind'] == 'integer':
        if not re.fullmatch(r'-?[0-9]{1,18}', value):
            raise ValueError('Нужно целое число.')
        return int(value)
    if f['kind'] == 'number':
        try:
            d = Decimal(value.replace(',', '.'))
        except InvalidOperation:
            raise ValueError('Нужно число, например 12.5.')
        if not d.is_finite():
            raise ValueError('Нужно конечное число.')
        return str(d)
    if f['kind'] in ('cell', 'range'):
        pattern = r'[A-Z]{1,3}[1-9][0-9]{0,6}'
        if not re.fullmatch(pattern+(r'(?::'+pattern+r')?' if f['kind']=='range' else ''), value.upper()):
            raise ValueError('Адрес вида B2 или диапазон B2:B12.')
        return value.upper()
    if f['kind'] == 'datetime':
        value = value.replace(' ', 'T', 1).replace(' ', '')
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError('Нужны дата, время и пояс: 2026-10-05 18:00 +05:00.')
        if dt.tzinfo is None:
            raise ValueError('Добавь часовой пояс, например +05:00.')
        return dt.isoformat()
    if f['kind'] == 'timezone':
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Не узнала пояс. Пример: Asia/Yekaterinburg.')
    if f['kind'] == 'lines':
        result = [x.strip() for x in value.splitlines() if x.strip()]
        if not 2 <= len(result) <= 12 or len(set(result)) != len(result):
            raise ValueError('Нужно от 2 до 12 разных вариантов, каждый с новой строки.')
        return result
    if f['kind'] == 'stages':
        result = [x.strip() for x in re.split(r'\n\s*\n', value) if x.strip()]
        if not 1 <= len(result) <= 30 or any(len(x)>2000 for x in result):
            raise ValueError('От 1 до 30 этапов, каждый до 2000 символов.')
        return result
    if f['kind'] == 'boolean':
        return value == 'true'
    if f['kind'] == 'binding_list':
        item=f['binding_schema'].get('items',{})
        lines=[x.strip() for x in value.splitlines() if x.strip()]
        if len(lines)>100:
            raise ValueError('Не больше 100 значений.')
        result=[]
        for line in lines:
            kind={'number':'number','integer':'integer','boolean':'boolean'}.get(item.get('type'),'text')
            nested=field('item','Значение',kind=kind)
            if kind=='boolean':
                nested['choices']=[('Да','true'),('Нет','false')]
            parsed=parse(nested,line)
            result.append(float(parsed) if kind=='number' else parsed)
        return result
    return value


def summary_pages(draft):
    values = draft['values']
    lines = []
    for f in draft['fields']:
        value = values.get(f['key'], '')
        if f['kind'] == 'file':
            doc = value.get('document') or value.get('audio') or {}
            value = doc.get('file_name') or 'Прикреплённый файл'
        elif isinstance(value, list):
            value = '\n'.join(str(x) for x in value)
        elif f['kind'] == 'boolean' and type(value) is bool:
            value = 'Да' if value else 'Нет'
        elif f['choices']:
            value = next((label for label, v in f['choices'] if str(v)==str(value)), value)
        lines.append(f['label']+': '+str(value if value != '' else 'Без значения'))
    # Keep every entered value reviewable, and split only between complete
    # escaped characters. A long goal/stage must not hide later parameters.
    chunks, current = [], ''
    for char in '\n\n'.join(lines):
        piece = escape(char)
        if len(current)+len(piece)>3000:
            chunks.append(current)
            current = ''
        current += piece
    chunks.append(current)
    return ['<b>Проверим перед выполнением</b>\n\n'+chunk for chunk in chunks]
