"""Inspect exact retained material fragments without promoting them to beliefs."""
from materials.runtime import enabled,actor_for_current,service_for_bot,CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,guard_current
from materials.retrieval import recall
from materials.types import MaterialError

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
