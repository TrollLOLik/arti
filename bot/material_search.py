"""Inspect exact retained material fragments without promoting them to beliefs."""
import asyncio
import logging
from artifacts.material_cards import COVERAGE, UNIT_LABELS, extraction_card, locator_text, render_card
from materials.runtime import enabled,actor_for_current,service_for_bot,CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,guard_current
from materials.retrieval import recall
from materials.types import MaterialError

logger = logging.getLogger(__name__)


async def send_material_card(message, context, card, output, *, validate=None):
    """One optional preview, followed by copyable text, with late source fences.

    Render errors fall back to text. A photo delivery with an unknown outcome is
    never retried or followed by another variant. Bytes stay in memory here.
    """
    async def guard():
        await guard_current(message.chat_id)
        if validate is not None:
            await validate()
    await guard()
    actual = getattr(context, '_menu_real_context', context)
    bot = getattr(actual, 'bot', None)
    photo_method = getattr(bot, 'send_photo', None)
    pixels = None
    if card is not None and callable(photo_method):
        try:
            pixels = await asyncio.to_thread(render_card, card)
        except Exception:
            # Log no source text, values, exception payload or identifiers.
            logger.warning('Material preview rendering failed; using text')
    await guard()
    if pixels is not None:
        from bot.retry_bot import RetryBot
        from bot.request_runtime import CURRENT_REQUEST, send as send_request
        from cognition.delivery import send_with_receipt
        from cognition.scope import CURRENT_SCOPE
        from telegram import ReplyParameters
        if isinstance(bot, RetryBot):
            photo_method = super(RetryBot, bot).send_photo
        kwargs = dict(chat_id=message.chat_id, photo=pixels,
                      caption=(card.kind + '\n' + card.caution + '\nЗначения, ограничения и источники — в тексте.')[:900],
                      parse_mode=None)
        if getattr(message, 'message_id', None) is not None:
            kwargs['reply_parameters'] = ReplyParameters(message_id=message.message_id)
        scope = CURRENT_SCOPE.get()
        topic = scope.topic_id if scope is not None else getattr(message, 'message_thread_id', None)
        if topic and topic > 0:
            kwargs['message_thread_id'] = topic
        async def send_photo(*args, **kw):
            await guard()  # after receipt preparation and immediately at transport
            return await photo_method(*args, **kw)
        if CURRENT_REQUEST.get() is not None:
            await send_request(send_photo, (), kwargs, 'photo')
        else:
            await send_with_receipt(send_photo, (), kwargs, 'photo')
    await guard()
    await message.reply_text(output, parse_mode=None)


def _bounded(value, limit):
    text = str(value)
    encoded = text.encode('utf-16-le')
    return text if len(encoded) <= limit*2 else encoded[:(limit-1)*2].decode('utf-16-le', errors='ignore') + '…'


def bounded_report(body, footer='', limit=3900):
    """Keep provenance/limitations visible even if detail rows must be cut."""
    combined = body + footer
    if len(combined.encode('utf-16-le')) // 2 <= limit:
        return combined
    notice = '\n[Список сокращён. Выбери конкретный лист/диапазон или проверь оригинал.]'
    reserved = len((notice + footer).encode('utf-16-le')) // 2
    return _bounded(body, limit-reserved) + notice + footer


async def material_summary_command(update, context):
    """Explicit extraction overview; selected literal excerpts, no LLM summary."""
    from materials.runtime import capture_document
    from materials.retrieval import verify_quote
    from materials.types import EvidenceRef
    message = update.effective_message
    source = getattr(message, 'reply_to_message', None)
    document = getattr(source, 'document', None)
    if not enabled() or document is None:
        await message.reply_text('Ответь на документ: /material_summary — покрытие извлечения, ограничения и буквальные фрагменты.')
        return
    token = None
    try:
        captured = await capture_document(context, document, source)
        token = CURRENT_MATERIAL_USE.set(captured.material_uses)
        use = captured.material_uses[0]
        # service.extract uses the same configured/cache-versioned extractor as
        # capture_document; reject a newer version instead of mixing snapshots.
        eid, bundle = await use.service.extract(use.asset_id, use.actor)
        if bundle.asset_id != use.asset_id or bundle.asset_version != use.version:
            raise MaterialError('stale_material_result')
        await use.validate()
        manifest = bundle.manifest
        filename = document.file_name or 'Документ'
        lines = ['Обзор извлечения: ' + _bounded(filename, 180),
                 f'Покрытие: {COVERAGE[manifest.coverage]} ({manifest.coverage}).',
                 f'По манифесту: {manifest.processed_units} из {manifest.total_units} {UNIT_LABELS[manifest.unit_kind]}.',
                 'Это не смысловой конспект и не подтверждение точности распознавания.',
                 f'Источник: {use.asset_id[:12]}; v{bundle.asset_version}; извлечение {eid[:12]}.',
                 'Оригинал — документ, на который отвечает команда.']
        if manifest.limitations:
            lines.append('Ограничения извлечения: ' + _bounded('; '.join(manifest.limitations), 750))
        else:
            lines.append('Манифест не сообщает ограничений. Это не гарантия полноты или точности.')
        lines.append('\nБуквальные фрагменты извлечённого текста (не весь документ):')
        count = 0
        for block in bundle.blocks:
            if not block.text.strip() or block.observation != 'extracted' or block.metadata.get('role') in ('table_cell', 'timed_word', 'timeline_chunk'):
                continue
            snippet = block.text[:400]
            ref = EvidenceRef(use.asset_id, bundle.asset_version, eid, block.block_id, block.locator)
            await verify_quote(use.service.repository, use.actor, ref, snippet)
            quality = {'verified': 'пометка извлекателя: verified', 'unassessed': 'качество не оценено',
                       'uncertain': 'требует проверки', 'unreadable': 'нечитаемо'}[block.quality]
            lines.append(f'\n{_bounded(locator_text(block.locator), 140)}; {block.block_id}; {quality}\n{snippet}')
            if len(block.text) > 400:
                lines.append('[Фрагмент сокращён.]')
            count += 1
            if count == 3:
                break
        if not count:
            lines.append('Буквальных текстовых фрагментов нет. Наблюдения модели не показаны как цитаты.')
        # The reserved budgets above keep source and uncertainty labels intact.
        output = '\n'.join(lines)
        if len(output.encode('utf-16-le')) // 2 > 3900:
            output = '\n'.join(lines[:8]) + '\nФрагменты слишком длинные для сообщения. Используй /materials_find текст или оригинал.'
        await send_material_card(message, context, extraction_card(bundle, filename), output, validate=use.validate)
    except MaterialError:
        # Drop expired proof context before sending a content-free error.
        if token is not None:
            CURRENT_MATERIAL_USE.reset(token)
            token = None
        await message.reply_text('Не удалось показать обзор. Источник недоступен, изменён или не содержит читаемых данных. Повтори команду с доступным документом.')
    finally:
        if token is not None:
            CURRENT_MATERIAL_USE.reset(token)

async def material_search_command(update,context):
    message=update.effective_message
    query=message.text.partition(' ')[2].strip()
    if not enabled() or not query: await message.reply_text('/materials_find текст — источники текущего чата и топика.'); return
    token=derivative=None
    try:
        service=await service_for_bot(); actor=await actor_for_current()
        text,uses,_=await recall(service,actor,query)
        if text is None: await message.reply_text('Доступных индексированных свидетельств не найдено. Это не доказывает отсутствие факта в непрочитанных материалах.'); return
        token=CURRENT_MATERIAL_USE.set(text.material_uses); derivative=CURRENT_DERIVATIVE_USE.set(uses)
        hits=text.hits
        output='Свидетельства текущей версии (совпадение текста, смысл требует проверки):\n'
        for h in hits: output+=f'\n{h.filename}; v{h.source.asset_version}; {h.source.block_id}; {h.source.locator}\n{h.text[:450]}\n'
        await guard_current(message.chat_id); await message.reply_text(output[:3900])
    except MaterialError: await message.reply_text('Источник изменён или отозван. Поиск нужно повторить.')
    finally:
        if token is not None: CURRENT_MATERIAL_USE.reset(token)
        if derivative is not None: CURRENT_DERIVATIVE_USE.reset(derivative)

async def material_review_command(update,context):
    from materials.runtime import capture_document
    from materials.interpretations import MaterialReview,InterpretationRepository
    from materials.types import EvidenceRef
    import shlex
    message=update.effective_message
    if not enabled(): await message.reply_text('Материалы отключены.'); return
    try:
        actor=await actor_for_current(); service=await service_for_bot(); notes=InterpretationRepository(service.repository)
        parts=shlex.split(message.text)[1:]
        if len(parts)==2 and parts[0]=='delete':
            await notes.remove(actor,parts[1]); await message.reply_text('Твоя заметка удалена; её зависимые результаты отозваны.'); return
        source=getattr(message,'reply_to_message',None)
        document=getattr(source,'document',None)
        if len(parts)!=3 or not document: raise MaterialError('review_input_required')
        captured=await capture_document(context,document,source)
        use=captured.material_uses[0]; eid,bundle=await service.extract(use.asset_id,actor)
        b=bundle.blocks[0]; ref=EvidenceRef(use.asset_id,bundle.asset_version,eid,b.block_id,b.locator)
        id=await notes.record(actor,MaterialReview(parts[1],parts[0],parts[2]),[ref],at=getattr(message,'date',None))
        await use.validate()
        await message.reply_text('Сохранена твоя оценка материала, отдельно от фактов и общего решения группы. ID '+id)
    except (MaterialError,ValueError):
        await message.reply_text('Ответь на документ: /material_review preferred|rejected|pending|proposal "краткое описание" "причина". Удалить свою заметку: /material_review delete ID.')
