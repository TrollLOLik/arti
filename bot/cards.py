"""Source-backed Telegram cards and a receipt-compatible Rich Messages adapter."""
from dataclasses import dataclass
from html import escape
import os


def clip(value, limit):
    value = str(value)
    data = value.encode('utf-16-le')
    return value if len(data) <= limit * 2 else data[:max(0, limit-1)*2].decode('utf-16-le', errors='ignore') + '…'


def escaped(value, budget):
    """Bound the encoded HTML too, without cutting entities or Unicode pairs."""
    value = str(value)
    if units(escape(value)) <= budget:
        return escape(value)
    parts, size = [], 0
    for char in value:
        part = escape(char)
        if size + units(part) > budget - 1:
            break
        parts.append(part); size += units(part)
    return ''.join(parts) + '…'


def units(value):
    return len(value.encode('utf-16-le'))//2


@dataclass(frozen=True)
class MessageCard:
    title: str
    status: str = ''
    body: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    footer: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()

    def html(self, limit=3500):
        # Visible cautions and source survive even when long excerpts are cut.
        tail_budget = min(1150, max(120, limit//3))
        tail = '\n\n' + '\n'.join(escaped(x, max(40, tail_budget//len(self.footer)-1)) for x in self.footer) if self.footer else ''
        head = '<b>' + escaped(self.title, 340) + '</b>'
        if self.status:
            head += '\n' + escaped(self.status, 220)
        content = list(self.body)
        if self.rows:
            content.extend(' · '.join(f'{h}: {v}' for h, v in zip(self.headers, row)) for row in self.rows)
        room = limit - units(head) - units(tail) - 130
        lines, shortened = [], False
        for line in content:
            if room < 80:
                shortened = True; break
            item = escaped(line, min(650, room))
            lines.append(item); room -= units(item) + 1
        middle = '\n\n' + '\n'.join(lines) if lines else ''
        if self.details and room > 100:
            details = escaped('\n'.join(self.details), room - 65)
            middle += '\n\n<b>Подробности</b>\n<blockquote expandable>' + details + '</blockquote>'
        elif self.details:
            shortened = True
        if shortened:
            middle += '\n[Обзор сокращён; полные данные — в исходном материале.]'
        return head + middle + tail

    def rich_html(self, *, preview=None):
        parts = ['<h2>' + escaped(self.title, 340) + '</h2>']
        if self.status:
            parts.append('<p><b>' + escaped(self.status, 220) + '</b></p>')
        parts.extend('<p>' + escaped(x, 1800).replace('\n', '<br>') + '</p>' for x in self.body[:12])
        if preview:
            parts.append('<figure><img src="tg://photo?id=preview"/><figcaption>Обзор результата</figcaption></figure>')
        if self.rows:
            parts.append('<table bordered striped compact><tr>' + ''.join('<th>' + escaped(h, 160) + '</th>' for h in self.headers) + '</tr>')
            for row in self.rows[:24]:
                parts.append('<tr>' + ''.join('<td>' + escaped(v, 300) + '</td>' for v in row) + '</tr>')
            parts.append('</table>')
            if len(self.rows) > 24:
                parts.append('<p>Показаны первые 24 строки. Полные данные — в исходном материале или файле результата.</p>')
        if self.details:
            parts.append('<details><summary>Подробности и источники</summary><p>' + escaped('\n'.join(self.details), 6000).replace('\n', '<br>') + '</p></details>')
        parts.extend('<footer>' + escaped(x, 1000) + '</footer>' for x in self.footer)
        return ''.join(parts)


TASK_STATES = dict(queued='🕓 В очереди', running='⏳ Выполняю', waiting='✋ Нужен ввод или разрешение',
    paused='⏸ На паузе', succeeded='✅ Результат проверен', partial='🟡 Готова часть результата',
    failed='⚠️ Работа остановлена', cancelled='⛔ Отменено', unknown='⚠️ Исход внешнего действия неизвестен')
TOOLS = {'materials.read':'Разобрать документ', 'materials.search':'Найти фрагменты в материалах',
    'dataset.extract':'Извлечь таблицы', 'dataset.compute':'Выполнить расчёт', 'dataset.transform':'Подготовить данные',
    'audio.transcript':'Расшифровать запись', 'audio.clip':'Подготовить отрезок записи',
    'artifact.create':'Собрать отчёт', 'artifact.patch':'Обновить отчёт', 'artifact.export':'Подготовить файлы отчёта',
    'workflow.plan':'Составить план', 'documents.report':'Подготовить документ', 'documents.data_export':'Экспортировать данные',
    'research.search':'Найти источники', 'research.fetch':'Прочитать источник', 'research.compare':'Сопоставить сведения',
    'media.image':'Создать изображение', 'media.video':'Создать видео', 'media.music':'Создать музыку'}
TOOL_RESULTS = {'materials.read':'Извлечённый текст', 'materials.search':'Найденные фрагменты',
    'dataset.extract':'Таблицы', 'dataset.compute':'Расчёт', 'dataset.transform':'Подготовленные данные',
    'audio.transcript':'Расшифровка', 'audio.clip':'Отрезок записи', 'artifact.create':'Отчёт',
    'artifact.patch':'Обновлённый отчёт', 'artifact.export':'Файлы отчёта', 'workflow.plan':'План работы',
    'documents.report':'Документ', 'documents.data_export':'Данные для скачивания',
    'research.search':'Список источников', 'research.fetch':'Материал источника', 'research.compare':'Сопоставление',
    'media.image':'Изображение', 'media.video':'Видео', 'media.music':'Музыка'}


def step_label(step):
    return TOOLS.get(step['tool'], 'Выполнить шаг «' + step['tool'] + '»')


def task_card(row, plan, calls, *, worker_enabled=True):
    latest = {c['step_id']: c for c in sorted(calls, key=lambda c: c.get('attempt', 0))}
    completed = {id for id, c in latest.items() if c['status'] == 'success'}
    steps, ready = [], []
    for step in plan['steps']:
        call = latest.get(step['id'], {})
        state = call.get('status')
        if state == 'success':
            mark = '✅'; ready.append(TOOL_RESULTS.get(step['tool'],'Результат шага «'+step['tool']+'»'))
        elif state == 'started':
            mark = '⏳' if row['status'] == 'running' else '⏸'
        elif state in ('failed', 'unknown'):
            mark = '⚠️'
        else:
            mark = '○'
        steps.append(mark + ' ' + step_label(step))
    body = [f"Готово этапов: {len(completed)}/{len(plan['steps'])}"]
    body.extend(steps[:8])
    if len(steps) > 8:
        body.append(f'Ещё этапов: {len(steps)-8}. Полный план — в подробностях.')
    if ready:
        body.append('Сохранено: ' + '; '.join(ready))
    if row['status'] == 'waiting':
        body.append('Открой проверку действия или уточни задание, чтобы продолжить.')
    if row['status'] == 'unknown':
        body.append('Проверь внешний результат перед повтором: действие могло уже выполниться.')
    if not worker_enabled and row['status'] in ('queued', 'running'):
        body.append('Исполнитель отключён администратором; задача сохранена.')
    details = [f"Задача: {row['id']} · версия {row['revision']} · вызовов: {row['used_calls']}"]
    if len(steps) > 8: details.extend(steps)
    if row.get('diagnostics'): details.append('Диагностика: ' + clip(row['diagnostics'], 500))
    return MessageCard(plan['goal'], TASK_STATES.get(row['status'], row['status']), tuple(body), tuple(details))


REQUEST_KINDS = dict(text='Ответ на сообщение', dubbing='Дубляж записи', vclone='Озвучка выбранным голосом',
                     image='Создание изображения', video='Создание видео', music='Создание музыки')
REQUEST_STATES = dict(queued='🕓 В очереди', running='⏳ Обрабатываю запрос', prepared='📦 Готовлю отправку',
    paused='⏸ Генерация прервана', completed='✅ Обработка завершена', succeeded='✅ Обработка завершена',
    failed='⚠️ Работа остановлена', cancelled='⛔ Отменено', expired='⌛ Время ожидания истекло',
    delivery_unknown='⚠️ Отправка не подтверждена', coalesced='↪️ Объединён с другим запросом')


def request_card(row, *, extra=()):
    body = []
    if row['state'] == 'queued': body.append('Запрос сохранён. Начну, когда освободится очередь.')
    if row['state'] == 'running': body.append('Обработка продолжается. Готовые материалы будут отправлены сюда.')
    if row['state'] == 'prepared': body.append('Результат подготовлен. Ожидаю подтверждения отправки Telegram.')
    if row['state'] == 'paused': body.append('Для повторной генерации нужно явное действие. Прерванный вызов мог завершиться у провайдера; повтор может снова использовать GPU или платный API.')
    if row['state'] == 'delivery_unknown': body.append('Telegram не подтвердил отправку. Проверь чат; автоматического повтора не будет.')
    if row.get('error_code'):
        reasons = {'deadline_exceeded':'Не удалось закончить в отведённое время.',
                   'generation_interrupted':'Генерация была прервана.', 'cancelled':'Запрос отменён автором.'}
        body.append(reasons.get(row['error_code'], 'Причина остановки — в подробностях запроса.'))
    body.extend(extra)
    details = [f"Запрос: {row['id']}", '/request ' + row['id']]
    if row.get('error_code'): details.append('Код: ' + row['error_code'])
    if row.get('deadline_at') and row['state'] in ('queued', 'running', 'paused'):
        details.append('Данные доступны до ' + row['deadline_at'].isoformat())
    return MessageCard(REQUEST_KINDS.get(row['kind'], 'Работа по запросу'), REQUEST_STATES.get(row['state'], row['state']), tuple(body), tuple(details))


def media_card(row, request, *, ready=False):
    base=request_card(row)
    source='Исходник: загруженная запись.' if request.get('input_media') else 'Исходник: видео по ссылке.'
    settings=[]
    if request['type']=='vclone':
        source='Голос: сохранённый образец.' if request.get('source_kind')=='saved_voice' else 'Голос: предоставленный образец.'
        settings.append('Текст озвучки: '+clip(request.get('synthesis_text',''),200))
    else:
        settings.append('Формат: аудио.' if request.get('audio_only') else 'Формат: видео.')
        if request.get('with_subs'): settings.append('Субтитры: включены в обработку.')
    return MessageCard(base.title,'✅ Готово' if ready else base.status,
        (() if ready else base.body)+(source,*settings),base.details)


def material_report_card(card, output, *, headers=(), rows=()):
    if card is None:
        return MessageCard('Результат', body=(output,))
    return MessageCard(card.title, '📄 ' + card.subtitle,
        tuple(f'{label}: {value}' for value, label in card.metrics) + card.details, (output,),
        (card.caution, card.source), tuple(headers), tuple(rows))


def document_analysis_html(metadata, result_html):
    """Keep the complete model answer; the existing HTML splitter owns length.

    The heading describes the requested operation, never extraction certainty
    or proof of the model's interpretation. Only native request metadata is used.
    """
    labels=dict(read='Разбор документа готов',summarize='Краткое содержание готово',
                keypoints='Ключевые тезисы готовы',rewrite='Новая редакция готова')
    heading=MessageCard(str(metadata.get('filename') or 'Документ'),
        '✅ '+labels.get(metadata.get('action'),'Анализ документа готов')).html()
    caution='Выводы — интерпретация модели по доступному извлечению. Сверь важные факты с оригиналом.'
    return heading+'\n\n'+result_html+'\n\n'+escape(caution)


def transcript_card(filename, id, timeline):
    states=dict(hypothesis='Не подтверждено',confirmed='Подтверждено пользователем',uncertain='Требует проверки')
    rows = tuple((clip(s.id, 60), f'{s.start_ms/1000:g}–{s.end_ms/1000:g} с',
                  s.speaker or 'Не определён', s.text,states.get(getattr(s,'status',''), 'Не подтверждено')) for s in timeline.segments)
    body = (f'Реплик: {len(rows)}', 'Чтобы переслушать или исправить текст, ответь на исходную запись командой из подробностей. В меню доступны действия для отдельных реплик.') if rows else ('Нет расшифровки с достоверными временными метками.',)
    first = timeline.segments[0].id if rows else 'turn_1'
    details = ('Версия: ' + id[:12], '/listen ' + first + ' — исходный отрезок',
               '/transcript_fix ' + first + ' "исправленный текст" version=' + id[:12])
    caution = 'Говорящие обозначены только внутри этой записи; метка не подтверждает личность.'
    if timeline.limitations: caution += ' Распознавание и разделение голосов могут быть неточными; проверь спорный отрезок.'
    return MessageCard(filename, '🎙 Расшифровка готова' if rows else '⚠️ Нет доступных реплик', body, details,
                       (caution,), ('Реплика', 'Время', 'Говорящий', 'Текст', 'Статус'), rows)


def artifact_card(row):
    spec = row['spec']
    statuses = dict(observed='Из источника', confirmed='Подтверждено', proposed='Предложение', unknown='Неизвестно', fiction='Вымысел')
    rows = []
    for e in spec['elements']:
        value = e.get('text', '')
        if e.get('quantity'):
            q = e['quantity']; value = q['value'] + ' ' + q['unit'] + (' · ' + value if value else '')
            if q.get('lower',q['value'])!=q.get('upper',q['value']):
                value+=' · границы '+q.get('lower',q['value'])+'…'+q.get('upper',q['value'])+' '+q['unit']
        rows.append((e['label'], value, statuses[e['status']]))
    cautions = []
    if spec.get('questions'): cautions.append('Требует уточнения: ' + '; '.join(str(q) for q in spec['questions']))
    if any(e['status'] in ('proposed','unknown','fiction') for e in spec['elements']):
        cautions.append('Предложения, неизвестные сведения и вымысел отмечены отдельно.')
    summary = tuple(e['label'] + ': ' + clip(e.get('text',''),180) + ' (' + statuses[e['status']] + ')' for e in spec['elements'][:2])
    return MessageCard(spec['title'], '📊 Отчёт готов', summary + ('Материалы: обзор, PDF, PNG, SVG и данные JSON.',),
        (f"Версия {row['revision']} · {len(spec['elements'])} блоков", 'Источники и проверки доступны кнопкой «Источники».'),
        tuple(cautions), ('Пункт', 'Содержание', 'Статус'), tuple(rows))


def card_transport(bot):
    """A pure transport: callers own late guards, durable slots and receipts.

    Fallback only follows a definite unsupported-method rejection. Network
    ambiguity is propagated, never followed by a second message format.
    """
    async def send_card(*, chat_id, text, rich_html=None, rich_media=None, **kwargs):
        from telegram import Bot, Message, ReplyParameters
        from telegram.error import BadRequest
        from bot.retry_bot import RetryBot
        native = super(RetryBot, bot).send_message if isinstance(bot, RetryBot) else bot.send_message
        if not isinstance(bot, Bot) or getattr(bot, '_menu_panel', None) is not None or os.getenv('ARTI_TELEGRAM_RICH_MESSAGES', 'true').lower() in ('0','false','no') or not rich_html:
            return await native(chat_id=chat_id, text=text, parse_mode='HTML', **kwargs)
        data = dict(chat_id=chat_id, rich_message=dict(html=rich_html), **kwargs)
        if rich_media: data['rich_message']['media'] = rich_media
        reply_id = data.pop('reply_to_message_id', None)
        if reply_id is not None: data['reply_parameters'] = ReplyParameters(message_id=reply_id)
        try:
            result = await bot._post('sendRichMessage', data)
        except BadRequest as exc:
            reason = str(exc).lower()
            if not any(s in reason for s in ('method not found', 'unknown method', 'rich messages are not supported', 'rich message is not supported')):
                raise
            return await native(chat_id=chat_id, text=text, parse_mode='HTML', **kwargs)
        return Message.de_json(result, bot)
    return send_card


async def reply_card(message, context, card, *, markup=None, validate=None, rich=True):
    from materials.runtime import guard_current
    async def guard():
        await guard_current(message.chat_id)
        if validate: await validate()
    await guard()
    actual = getattr(context, '_menu_real_context', context)
    bot = getattr(actual, 'bot', None)
    from telegram import Bot
    if getattr(context, '_menu_real_context', None) is not None or not isinstance(bot, Bot):
        return await message.reply_text(card.html(), parse_mode='HTML', reply_markup=markup)
    from bot.request_runtime import CURRENT_REQUEST, send as send_request
    from cognition.delivery import send_with_receipt
    from cognition.scope import CURRENT_SCOPE
    transport = card_transport(bot)
    async def send_card(*args, **kwargs):
        await guard()
        return await transport(*args, **kwargs)
    kwargs = dict(chat_id=message.chat_id, text=card.html(), rich_html=card.rich_html() if rich else None, reply_markup=markup)
    if getattr(message, 'message_id', None): kwargs['reply_to_message_id'] = message.message_id
    scope = CURRENT_SCOPE.get()
    topic = scope.topic_id if scope else getattr(message, 'message_thread_id', None)
    if topic and topic > 0: kwargs['message_thread_id'] = topic
    result=await send_request(send_card, (), kwargs, 'card') if CURRENT_REQUEST.get() is not None else await send_with_receipt(send_card, (), kwargs, 'card')
    from bot.retry_bot import _maybe_record_sent
    _maybe_record_sent(result)
    return result
