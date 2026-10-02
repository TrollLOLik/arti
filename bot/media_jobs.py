"""Disk-backed accepted dubbing/voice-clone requests.

Only immutable, application-owned descriptors cross a restart. GPU generation
is restartable from its input, not resumable inside a model invocation.
"""
import asyncio
import html
import os
import re
import shutil
from contextlib import ExitStack
from pathlib import Path

from bot.request_runtime import CURRENT_REQUEST, checkpoint, store


def spool():
    from bot.media_spool import MediaSpool
    return MediaSpool()


async def submit_media(task, bot, chat_id, kind):
    from config import TTS_ENABLED
    from bot.queue import _detach_menu_context
    from bot.request_runtime import submit
    from cognition.scope import CURRENT_SCOPE
    from cognition.runtime import CURRENT_TURN
    from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
    if not TTS_ENABLED:
        await bot.send_message(chat_id=chat_id, text='TTS и озвучка временно отключены.')
        return None
    _detach_menu_context(task)
    request = {k: task[k] for k in ('chat_id','user_id','user_name','message_id','url',
        'synthesis_text','cleaned','source_kind','with_subs','audio_only','reference_source','saved_voice_id','saved_voice_version') if k in task}
    request.update(type=kind, chat_id=chat_id, _telegram_scope=CURRENT_SCOPE.get(),
        _cognitive_turn=CURRENT_TURN.get(), _material_uses=CURRENT_MATERIAL_USE.get(),
        _derivative_uses=CURRENT_DERIVATIVE_USE.get(), _computation_uses=CURRENT_COMPUTATION_USE.get())
    disk = spool()
    namespace = await asyncio.to_thread(disk.create_namespace)
    enqueue_attempted = False
    adopted = False
    try:
        source = task.get('reference_path' if kind == 'vclone' else 'input_file')
        if source:
            descriptor = await asyncio.to_thread(disk.stage, Path(source), namespace=namespace)
            request['reference_media' if kind == 'vclone' else 'input_media'] = descriptor
        elif kind == 'vclone':
            raise ValueError('missing_voice_reference')
        elif not request.get('url'):
            raise ValueError('missing_dubbing_source')
        from bot.media_provenance import capture
        request = await capture(request, CURRENT_SCOPE.get(), CURRENT_TURN.get())
        enqueue_attempted = True
        job = await submit(request, resources=[namespace])
        adopted = namespace in await store().adopted_resources(job['id'])
        # Never remove intake copies on uncertain/failed acceptance.
        from bot.media_intake import OwnedIntake
        owned=task.get('_owned_intake')
        if type(owned) is OwnedIntake:
            await asyncio.to_thread(owned.cleanup)
        if not adopted:
            await asyncio.to_thread(disk.cleanup, namespace)
        # Cosmetic receipt cannot turn a committed request into an intake failure.
        try:
            from telegram.ext import ExtBot
            async with asyncio.timeout(5):
                await ExtBot.send_message(bot, chat_id=chat_id,
                    text=f"Запрос {job['id']} сохранён. Статус: /request {job['id']}",
                    reply_to_message_id=request.get('message_id'))
        except Exception:
            pass
        return job
    finally:
        if not enqueue_attempted:
            await asyncio.to_thread(disk.cleanup, namespace)


async def _owned_path(descriptor):
    from cognition.delivery import DeliverySuppressed
    job = CURRENT_REQUEST.get()
    if not job or not await store().resources_owned(job['id'], job['token'], [descriptor['namespace']]):
        raise DeliverySuppressed()
    return await asyncio.to_thread(spool().resolve, descriptor)


async def _attempt():
    from cognition.delivery import DeliverySuppressed
    job = CURRENT_REQUEST.get()
    disk = spool()
    namespace = await asyncio.to_thread(disk.create_namespace)
    if not await store().bind_resources(job['id'], job['token'], [namespace]):
        disk.cleanup(namespace)
        raise DeliverySuppressed()
    return disk, namespace, await asyncio.to_thread(disk.workdir,namespace)


async def execute(request, bot):
    from config import TTS_ENABLED, PRIVILEGED_USER_IDS
    from cognition.scope import CURRENT_SCOPE
    from cognition.runtime import CURRENT_TURN
    from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE, guard_current
    from cognition.delivery import DeliverySuppressed
    if not TTS_ENABLED or (request['type']=='vclone' and request.get('user_id') not in PRIVILEGED_USER_IDS):
        raise DeliverySuppressed()
    CURRENT_SCOPE.set(request.get('_telegram_scope'))
    CURRENT_TURN.set(request.get('_cognitive_turn'))
    CURRENT_MATERIAL_USE.set(request.get('_material_uses', ()))
    CURRENT_DERIVATIVE_USE.set(request.get('_derivative_uses', ()))
    CURRENT_COMPUTATION_USE.set(request.get('_computation_uses', ()))
    await guard_current(request['chat_id'])

    async def generate():
        disk, namespace, work = await _attempt()
        # Readers and live subprocess writers retain a namespace lock. Cleanup
        # retries rather than deleting beneath a still-cancelling child.
        with ExitStack() as holds:
            holds.enter_context(disk.hold(namespace))
            for key in ('input_media','reference_media'):
                if request.get(key):
                    holds.enter_context(disk.hold(request[key]['namespace']))
            async def produce():
                if request['type'] == 'dubbing':
                    from ai.dubbing import run_dubbing
                    source = await _owned_path(request['input_media']) if request.get('input_media') else None
                    ok, output, _ = await run_dubbing(request.get('url',''), 'generation',
                        with_subs=bool(request.get('with_subs')), input_file=source,
                        audio_only=bool(request.get('audio_only')), output_root=work)
                    if not ok or output is None:
                        raise RuntimeError('dubbing_generation_failed')
                    channel = 'audio' if request.get('audio_only') else 'video'
                else:
                    from ai.voice_clone_job import generate_clone
                    source = await _owned_path(request['reference_media'])
                    output, channel = await generate_clone(source, request.get('synthesis_text',''), work)
                return output,channel
            output,channel = await bounded_work(produce, disk, namespace)
            descriptor = await asyncio.to_thread(disk.stage, output, namespace=namespace)
            return dict(media=descriptor, channel=channel)

    job = CURRENT_REQUEST.get()
    if 'disk_media_result' not in job['checkpoints']:
        if job['checkpoints'].get('disk_generation_started'):
            from bot.request_runtime import _failure_notice
            try:
                async with asyncio.timeout(2):
                    await _failure_notice(job,bot,f"Запрос {job['id']} прерван. Чтобы разрешить повтор генерации, открой /request {job['id']}.")
            except Exception:
                pass
            await store().pause(job['id'],job['token'],'generation_interrupted')
            return
        if not await store().checkpoint(job['id'],job['token'],'disk_generation_started',True):
            raise DeliverySuppressed()
        job['checkpoints']['disk_generation_started']=True
    result = await checkpoint('disk_media_result', generate)
    descriptor = result['media']
    await _owned_path(descriptor)
    from bot.media_retention import retain_copy
    if descriptor['size'] > 50 * 1024 * 1024:
        retained = await retain_copy(store().pool,job['id'],job['token'],'result',descriptor,request['user_id'],spool=spool())
        await bot.send_message(chat_id=request['chat_id'],
            text=f"Результат больше лимита Telegram 50 МБ. Сохранён на этом ПК на ограниченный срок. "
                 f"Артефакт: {job['id']}. Статус и локальный экспорт: /request {job['id']}.",
            reply_to_message_id=request.get('message_id'))
        return
    channel = result['channel']
    if channel not in ('audio','video','voice'):
        raise ValueError('invalid_disk_media_channel')
    kwargs = dict(chat_id=request['chat_id'], reply_to_message_id=request.get('message_id'))
    kwargs[channel] = {'_arti_spooled_file': descriptor}
    if channel != 'voice':
        kwargs['caption'] = 'Готово!'
    if request['type']=='vclone' and request.get('source_kind')!='saved_voice':
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from bot.media_retention import RetentionUnavailable
        from bot.media_spool import SpoolError
        try:
            await retain_copy(store().pool,job['id'],job['token'],'voice_reference',request['reference_media'],request['user_id'],spool=spool())
        except (RetentionUnavailable, SpoolError, OSError):
            # An optional expired save offer cannot discard a ready main result.
            # Existing prepared payloads may contain the old, safely expired button.
            pass
        else:
            kwargs['reply_markup']=InlineKeyboardMarkup([[InlineKeyboardButton('Сохранить голос (15 минут)',callback_data='media_voice_save:'+job['id'])]])
    await getattr(bot, 'send_'+channel)(**kwargs)


async def bounded_work(factory, disk, namespace):
    """Sampled disk ceiling, not a hard OS/filesystem quota."""
    maximum = int(os.getenv('ARTI_MEDIA_WORK_MAX_BYTES', str(2*1024**3)))
    reserve = int(os.getenv('ARTI_MEDIA_MIN_FREE_BYTES', str(512*1024**2)))
    if not 1024**2 <= maximum <= 32*1024**3 or not 0 <= reserve <= 32*1024**3:
        raise ValueError('invalid_media_disk_budget')
    async def check():
        size = await asyncio.to_thread(disk.size_bytes, namespace)
        free = (await asyncio.to_thread(shutil.disk_usage, disk.root)).free
        if size > maximum or free < reserve:
            raise RuntimeError('media_disk_budget_exceeded')
    await check()
    execution = asyncio.create_task(factory())
    try:
        while not execution.done():
            done,_ = await asyncio.wait([execution],timeout=1)
            await check()
            if done: break
        return await execution
    finally:
        if not execution.done(): execution.cancel()
        await asyncio.gather(execution,return_exceptions=True)


async def maintenance_once(*, cleanup_grace=5):
    """Reclaim only registered terminal namespaces; orphan grace covers intake."""
    from bot.media_spool import SpoolError
    from bot.media_retention import expire
    await expire(store().pool)
    disk = spool()
    for _ in range(20):
        resource = await store().claim_resource_cleanup(grace_seconds=cleanup_grace)
        if resource is None:
            break
        try:
            await asyncio.to_thread(disk.cleanup, resource['namespace'])
        except (OSError, SpoolError):
            await store().finish_resource_cleanup(resource['namespace'], resource['token'], success=False)
            break
        await store().finish_resource_cleanup(resource['namespace'], resource['token'])
    live = await store().retained_resource_namespaces()
    await asyncio.to_thread(disk.collect, live, min_age_seconds=3600, budget=20)


async def maintenance_worker():
    while True:
        await maintenance_once()
        await asyncio.sleep(30)


async def save_voice_callback(update, context):
    from bot.media_retention import Retention
    from bot.commands import _vclone_prompt_save_name, _gate_vclone_not_privileged, _gate_tts_disabled
    from cognition.scope import CURRENT_SCOPE
    query=update.callback_query
    scope=CURRENT_SCOPE.get()
    if (scope is None or scope.user_id!=query.from_user.id or scope.chat_id!=query.message.chat_id):
        await query.answer('Недоступно.',show_alert=True)
        return
    if await _gate_tts_disabled(update) or await _gate_vclone_not_privileged(update,query.from_user.id): return
    request_id=query.data.removeprefix('media_voice_save:')
    retained=await Retention(store().pool).load(request_id,'voice_reference',scope.user_id,scope.chat_id,scope.topic_id)
    if retained is None:
        await query.answer('Референс недоступен или срок хранения истёк. Используй /voice_save с исходным сэмплом.',show_alert=True)
        return
    disk=spool()
    with disk.hold(retained['namespace']):
        path=await asyncio.to_thread(disk.resolve,retained['descriptor'])
        await query.answer()
        await _vclone_prompt_save_name(context=context,chat_id=scope.chat_id,user_id=scope.user_id,
            reference_path=str(path),source_kind='durable_clone',cleaned=False,
            cleanup_paths=[str(path)])
        from config import vclone_save_flow_state
        vclone_save_flow_state[scope.chat_id][scope.user_id]['media_retained_id']=request_id


async def retry_callback(update, context):
    query=update.callback_query
    from cognition.scope import CURRENT_SCOPE
    scope=CURRENT_SCOPE.get()
    if scope is None or scope.user_id!=query.from_user.id or scope.chat_id!=query.message.chat_id:
        await query.answer('Недоступно.',show_alert=True); return
    request_id=query.data.removeprefix('media_retry:')
    resumed=await store().resume(request_id,scope.chat_id,scope.topic_id,scope.user_id,'callback:'+query.id)
    await query.answer('Повторная генерация разрешена.' if resumed else 'Запрос недоступен или уже продолжен.',show_alert=True)
