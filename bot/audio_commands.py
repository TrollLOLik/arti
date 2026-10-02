"""Timed transcript inspection, explicit author correction and source replay."""
import base64
from io import BytesIO
import shlex
from types import SimpleNamespace
from materials.runtime import enabled,capture_document,actor_for_current,service_for_bot,CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,DerivativeUse,guard_current
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError,EvidenceRef,Locator

HELP='Ответь на голосовое или аудиофайл:\n/transcript — реплики, время и версия\n/listen turn_1 — переслушать исходный отрезок\n/transcript_fix turn_1 "исправленный текст" version=ID — подтвердить текст без изменения записи.'


async def _run(update,context,action):
    message=update.effective_message
    if not enabled(): await message.reply_text('Сохранённая работа с аудио сейчас отключена.'); return
    original=getattr(message,'reply_to_message',None)
    audio=getattr(original,'voice',None) or getattr(original,'audio',None) or getattr(original,'document',None)
    if not original or not audio: await message.reply_text(HELP); return
    token=derivative_token=None
    try:
        descriptor=SimpleNamespace(file_id=audio.file_id,file_size=audio.file_size,file_name=getattr(audio,'file_name',None) or 'voice.ogg',mime_type=getattr(audio,'mime_type',None))
        material=await capture_document(context,descriptor,original,slot='audio' if getattr(original,'voice',None) or getattr(original,'audio',None) else 'document')
        use=material.material_uses[0]; token=CURRENT_MATERIAL_USE.set(material.material_uses)
        actor=await actor_for_current(); service=await service_for_bot()
        id,timeline,root=await service.transcript(use.asset_id,actor)
        derivative_token=CURRENT_DERIVATIVE_USE.set((DerivativeUse(id,actor,DerivativeRepository(service.repository),'transcript'),))
        parts=shlex.split(message.text)[1:]
        if action=='transcript':
            text='Расшифровка '+id[:12]+'; говорящие обозначены только внутри записи.\n'
            text+='\n'.join(f'{s.id}: {s.start_ms/1000:g}–{s.end_ms/1000:g} с; {s.speaker or "говорящий неизвестен"}; {s.status}\n{s.text}' for s in timeline.segments[:24])
            if not timeline.segments: text+='Нет доступной расшифровки с достоверными временными метками.'
            if timeline.limitations: text+='\nРаспознавание и разделение голосов могут быть неточными; проверь спорный отрезок.'
            await guard_current(message.chat_id); await message.reply_text(text[:3900]); return
        if not parts: raise MaterialError('segment_required')
        segment=next((s for s in timeline.segments if s.id==parts[0]),None)
        if segment is None: raise MaterialError('segment_missing')
        if action=='fix':
            if len(parts)!=3 or not parts[2].startswith('version=') or parts[2][8:] not in (id,id[:12]): raise MaterialError('stale_transcript_correction')
            new,_=await service.confirm_transcript(use.asset_id,actor,id,segment.id,parts[1])
            CURRENT_DERIVATIVE_USE.set((DerivativeUse(new,actor,DerivativeRepository(service.repository),'transcript'),))
            await guard_current(message.chat_id)
            await message.reply_text('Исправление подтверждено; исходный звук сохранён. Версия '+new[:12]+'. Зависимые результаты отозваны.'); return
        if len(parts)!=1: raise MaterialError('invalid_audio_command')
        # The turn itself is an actual immutable extraction block. Confirmed text
        # preserves its source interval and never fabricates new word alignment.
        bundle=await service.repository.evidence_bundle(root,actor)
        block=next((b for b in bundle.blocks if b.metadata.get('segment_id')==segment.id),None)
        if block is None: raise MaterialError('segment_missing')
        ref=EvidenceRef(use.asset_id,use.version,root.extraction_id,block.block_id,block.locator)
        clip=await service.audio_clip(ref,actor)
        await guard_current(message.chat_id)
        output=BytesIO(base64.b64decode(clip['audio_base64'])); output.name=segment.id+'.mp3'
        await message.reply_audio(output,caption=f'Исходный отрезок {segment.start_ms/1000:g}–{segment.end_ms/1000:g} с. Speaker ID не подтверждает личность.')
    except (MaterialError,ValueError) as exc:
        if token is not None: CURRENT_MATERIAL_USE.reset(token); token=None
        if derivative_token is not None: CURRENT_DERIVATIVE_USE.reset(derivative_token); derivative_token=None
        code=getattr(exc,'code','invalid_audio_command')
        text='Версия изменилась или не указана: сначала открой /transcript.' if code=='stale_transcript_correction' else 'Не удалось выполнить операцию: проверь доступ, реплику и наличие временной расшифровки.'
        await message.reply_text(text+'\n'+HELP)
    finally:
        if token is not None: CURRENT_MATERIAL_USE.reset(token)
        if derivative_token is not None: CURRENT_DERIVATIVE_USE.reset(derivative_token)


async def transcript_command(update,context): await _run(update,context,'transcript')
async def transcript_fix_command(update,context): await _run(update,context,'fix')
async def listen_command(update,context): await _run(update,context,'listen')
