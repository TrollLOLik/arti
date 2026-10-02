"""Durable ordinary requests. Database rows, never asyncio queues, own work."""
import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.request_store import RequestStore

CURRENT_REQUEST = contextvars.ContextVar('arti_request', default=None)
logger = logging.getLogger(__name__)


class MediaContextBusy(Exception):
    """Temporary shared rebuild, not erasure of this request's sources."""


async def _media_context_busy(job):
    if job['kind'] not in ('dubbing','vclone') or not job.get('context_ids'): return False
    async with store().pool.acquire() as conn:
        return bool(await conn.fetchval('SELECT EXISTS(SELECT 1 FROM cognitive_contexts WHERE id=ANY($1::bigint[]) AND rebuilding)',job['context_ids']))


def store():
    from database import connection
    if connection._pool is None:
        raise RuntimeError('request_database_unavailable')
    return RequestStore(connection._pool)


def diagnostic(job, stage, started=None, **fields):
    # Intentionally exclude chat/user IDs, prompts, media, URLs and exception text.
    record = dict(request_id=job['id'], kind=job['kind'], stage=stage,
                  attempt=job.get('attempts', 0), **fields)
    if started is not None:
        record['duration_ms'] = round((time.monotonic() - started) * 1000)
    logger.info('request_lifecycle', extra={'request_diagnostic': record})


async def submit(request, *, resources=()):
    from bot.request_codec import encode_request
    from cognition.scope import CURRENT_SCOPE
    scope = request.get('_telegram_scope') or CURRENT_SCOPE.get()
    topic = scope.topic_id if scope else -1
    kind = request['type']
    parent = CURRENT_REQUEST.get()
    # Repeated Telegram intake and regenerated media tags address the same job.
    identity = [request['chat_id'], topic, request.get('message_id'), kind]
    if kind != 'text':
        identity += [{key: request.get(key) for key in ('prompt', 'style', 'instrumental',
                      'image_urls', 'image_aspect_ratio', 'image_resolution', 'image_num_images',
                      'video_model', 'video_duration', 'video_aspect_ratio')}, parent['id'] if parent else None]
    if kind in ('dubbing','vclone'):
        identity += [{key: request.get(key) for key in ('url','synthesis_text','cleaned','with_subs','audio_only')},
                     {key: request[key].get('sha256') for key in ('input_media','reference_media') if request.get(key)}]
    if request.get('message_id') is None:
        raise ValueError('durable_request_requires_message_id')
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    payload = await encode_request(request)
    budget = float(os.getenv('ARTI_REQUEST_TEXT_BUDGET_SECONDS' if kind == 'text'
                              else 'ARTI_REQUEST_MEDIA_BUDGET_SECONDS', '180' if kind == 'text' else '900'))
    if kind in ('dubbing', 'vclone'):
        budget = float(os.getenv('ARTI_REQUEST_DISK_MEDIA_BUDGET_SECONDS', '604800'))
        if not 60 <= budget <= 604800:
            raise ValueError('invalid_disk_media_budget')
    job = await store().enqueue(kind, request['chat_id'], topic, key, payload, budget_seconds=budget, resources=resources)
    from bot.intake import note_accepted
    note_accepted(job['id'])
    diagnostic(job, 'accepted')
    return job


async def checkpoint(name, factory):
    """Persist completed provider work before delivery. A pre-commit crash may repeat it."""
    job = CURRENT_REQUEST.get()
    if job is None:
        return await factory()
    from bot.request_codec import encode_value, decode_value
    from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
    from cognition.runtime import CURRENT_TURN
    if name in job['checkpoints']:
        saved = await decode_value(job['checkpoints'][name])
        CURRENT_MATERIAL_USE.set(saved['materials'])
        CURRENT_DERIVATIVE_USE.set(saved['derivatives'])
        CURRENT_COMPUTATION_USE.set(saved['computations'])
        if saved['turn'] is not None:
            CURRENT_TURN.set(saved['turn'])
        return saved['value']
    started = time.monotonic()
    value = await factory()
    if name.endswith('_result') and not value:
        diagnostic(job, name, started, error_code='empty_result')
        return value
    encoded = await encode_value(dict(value=value, materials=CURRENT_MATERIAL_USE.get(),
                                      derivatives=CURRENT_DERIVATIVE_USE.get(), computations=CURRENT_COMPUTATION_USE.get(),
                                      turn=CURRENT_TURN.get() if name != 'cognitive_turn' else None))
    if not await store().checkpoint(job['id'], job['token'], name, encoded):
        from cognition.delivery import DeliverySuppressed
        raise DeliverySuppressed()
    job['checkpoints'][name] = encoded
    diagnostic(job, name, started)
    return value


async def prepare_turn(*args, **kwargs):
    from cognition.runtime import prepare_turn as original, CURRENT_TURN
    async def prepare():
        turn = await original(*args, **kwargs)
        if CURRENT_REQUEST.get() is not None:
            # The initial prepared memory is opaque text. Conservatively retain
            # provenance for this owner's live projections so erasure can scrub
            # it even before final prompt inclusion has been recorded.
            async with turn.runtime.pool.acquire() as conn:
                sources = await conn.fetchval('''SELECT ARRAY_AGG(DISTINCT p.source_event_id)
                    FROM cognitive_provenance p JOIN cognitive_artifacts a ON a.id=p.artifact_id
                    WHERE a.context_id=$1 AND a.owner_id IS NOT DISTINCT FROM $2
                    AND a.suppressed_at IS NULL''', turn.context_id, turn.event.evidence.owner_id) or []
            turn.supporting_event_ids = sorted(set(getattr(turn, 'supporting_event_ids', ())) | set(sources))
        return turn
    turn = await checkpoint('cognitive_turn', prepare)
    CURRENT_TURN.set(turn)
    return turn


async def agent_handoff(factory):
    """Agent work has its own durable engine; never replay a partial side effect."""
    job = CURRENT_REQUEST.get()
    if job is None:
        return await factory()
    if 'agent_result' in job['checkpoints']:
        return await checkpoint('agent_result', factory)
    if job['checkpoints'].get('agent_started'):
        from materials.types import MaterialError
        raise MaterialError('interrupted_agent_handoff')
    if not await store().checkpoint(job['id'], job['token'], 'agent_started', True):
        from cognition.delivery import DeliverySuppressed
        raise DeliverySuppressed()
    job['checkpoints']['agent_started'] = True
    return await checkpoint('agent_result', factory)


async def send(method, args, kwargs, channel):
    job = CURRENT_REQUEST.get()
    lock = job.setdefault('_send_lock', asyncio.Lock())
    async with lock:
        return await _send(method, args, kwargs, channel)


async def _send(method, args, kwargs, channel):
    """One attempt after a durable intent. An unknown result is never retried."""
    from cognition.delivery import DeliveryUnknown, DeliverySuppressed, send_with_receipt
    from bot.request_codec import encode_value, decode_value
    job = CURRENT_REQUEST.get()
    job['_ordinal'] = job.get('_ordinal', 0) + 1
    ordinal = job['_ordinal']
    payload = await encode_value(dict(args=args, kwargs=kwargs, channel=channel))
    row = await store().prepare_send(job['id'], job['token'], ordinal, payload)
    if row is None:
        raise DeliverySuppressed()
    if row['state'] == 'delivered':
        # Advance the cognitive ordinal, too: later sends keep their original keys.
        from cognition.runtime import CURRENT_TURN
        turn = CURRENT_TURN.get()
        if turn and turn.tracks_delivery:
            turn.send_ordinal += 1
        def restore(receipt):
            if receipt is True: return True
            if isinstance(receipt, list): return [restore(item) for item in receipt]
            return SimpleNamespace(message_id=receipt['message_id'])
        return restore(row['receipt'])
    if row['state'] in ('sending', 'delivery_unknown'):
        raise DeliveryUnknown()
    if row['state'] != 'prepared':
        raise DeliverySuppressed()
    # Always use the original persisted payload after interruption.
    prepared = await decode_value(row['payload'])
    if prepared['channel'] != channel:
        raise DeliverySuppressed()
    # Open staged bytes only after the request/namespace ownership check. The
    # descriptor, never a local path or copied 50 MiB blob, is the durable intent.
    from contextlib import ExitStack
    files = ExitStack()
    try:
        for key, value in list(prepared['kwargs'].items()):
            if isinstance(value, dict) and '_arti_spooled_file' in value:
                if set(value) != {'_arti_spooled_file'} or key not in ('audio','video','voice','document'):
                    raise DeliverySuppressed()
                from bot.media_jobs import spool
                descriptor = value['_arti_spooled_file']
                await store().assert_resources(job['id'], job['token'], [descriptor['namespace']])
                prepared['kwargs'][key] = files.enter_context(spool().open_verified(descriptor))
        if await _media_context_busy(job):
            raise MediaContextBusy()
        if not await store().begin_send(job['id'], job['token'], ordinal):
            raise DeliverySuppressed()
    except BaseException:
        files.close()
        raise
    started = time.monotonic()
    try:
        result = await send_with_receipt(method, prepared['args'], prepared['kwargs'], prepared['channel'])
        def minimal_receipt(value):
            if value is True: return True
            if isinstance(value, (tuple, list)): return [minimal_receipt(item) for item in value]
            mid = getattr(value, 'message_id', None)
            if mid is None: raise DeliveryUnknown()
            return {'message_id': mid}
        receipt = minimal_receipt(result)
        if not await store().finish_send(job['id'], job['token'], ordinal, 'delivered', receipt):
            raise DeliveryUnknown()
    except BaseException:
        try:
            await asyncio.shield(store().finish_send(job['id'], job['token'], ordinal, 'delivery_unknown'))
        except Exception:
            pass  # The durable sending marker remains non-retryable on recovery.
        raise
    finally:
        files.close()
    diagnostic(job, 'delivery', started)
    return result


async def _heartbeat(job, execution):
    while True:
        await asyncio.sleep(15)
        try:
            alive = await asyncio.wait_for(store().renew(job['id'], job['token']), 10)
        except Exception:
            alive = False
        if not alive:
            execution.cancel()
            return


async def _execute(job, bot):
    from bot.request_codec import decode_request
    from bot.queue import process_user_reply, _execute_generation_task
    from utils.response_status import is_responses_enabled
    if await _media_context_busy(job):
        raise MediaContextBusy()
    request = await decode_request(job['payload'], bot)
    if not await is_responses_enabled(request['chat_id']):
        await store().finish(job['id'], job['token'], 'cancelled')
        return
    if job['kind'] == 'text':
        await process_user_reply(request, bot)
    elif job['kind'] in ('dubbing', 'vclone'):
        from bot.media_jobs import execute
        await execute(request, bot)
        outcome = await store().status(job['id'])
        if outcome and outcome['state']=='paused':
            return
    else:
        await _execute_generation_task(request)
    await store().finish(job['id'], job['token'], 'completed')


async def worker(bot, kinds):
    from cognition.delivery import DeliveryUnknown, DeliverySuppressed
    from cognition.repositories import SuppressedEvidence
    from materials.types import MaterialError
    while True:
        job = await store().claim(kinds)
        if job is None:
            await asyncio.sleep(.5)
            continue
        started = time.monotonic()
        diagnostic(job, 'started', queue_ms=round((datetime.now(timezone.utc)-job['created_at']).total_seconds()*1000))
        job['_send_lock'] = asyncio.Lock()
        token = CURRENT_REQUEST.set(job)
        execution = asyncio.create_task(_execute(job, bot))
        from bot.queue import register_running_task, unregister_running_task
        register_running_task(job['chat_id'], execution)
        progress = asyncio.create_task(_progress(job, bot))
        heartbeat = asyncio.create_task(_heartbeat(job, execution))
        try:
            remaining = max(0, (job['deadline_at']-datetime.now(timezone.utc)).total_seconds())
            async with asyncio.timeout(max(0, remaining-min(2,remaining*.1))):
                await execution
        except asyncio.TimeoutError:
            try:
                async with asyncio.timeout(1.5):
                    await _notice(job, bot, 1000000, f"Запрос {job['id']} остановлен: истёк срок ожидания. Можно отправить новый запрос.")
            except Exception:
                pass
            await store().finish(job['id'], job['token'], 'failed', 'deadline_exceeded')
            diagnostic(job, 'deadline_exceeded', started)
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await asyncio.shield(store().release(job['id'], job['token']))
            if asyncio.current_task().cancelling():
                raise
        except MediaContextBusy:
            await store().release(job['id'],job['token'],delay_seconds=1)
            diagnostic(job,'waiting_for_source_rebuild',started)
        except (DeliveryUnknown, DeliverySuppressed, SuppressedEvidence, MaterialError) as exc:
            if not isinstance(exc,DeliveryUnknown) and await _media_context_busy(job):
                await store().release(job['id'],job['token'],delay_seconds=1)
                diagnostic(job,'waiting_for_source_rebuild',started)
            else:
                await store().finish(job['id'], job['token'], 'delivery_unknown' if isinstance(exc, DeliveryUnknown) else 'cancelled', type(exc).__name__)
                diagnostic(job, 'blocked', started, error_code=type(exc).__name__)
        except Exception as exc:
            if job['kind'] in ('dubbing', 'vclone'):
                try:
                    async with asyncio.timeout(2):
                        await _failure_notice(job, bot)
                except Exception:
                    pass
            # No blind provider retries after an unknown side effect. Crash recovery
            # is distinct from an explicit application failure.
            await store().finish(job['id'], job['token'], 'failed', type(exc).__name__)
            diagnostic(job, 'failed', started, error_code=type(exc).__name__)
        else:
            outcome = await store().status(job['id'])
            diagnostic(job, outcome['state'] if outcome else 'unconfirmed', started)
        finally:
            unregister_running_task(job['chat_id'], execution)
            heartbeat.cancel()
            progress.cancel()
            await asyncio.gather(heartbeat, progress, return_exceptions=True)
            CURRENT_REQUEST.reset(token)
        if job['kind'] in ('image', 'video', 'music'):
            from config import MUSIC_COOLDOWN
            await asyncio.sleep(MUSIC_COOLDOWN if job['kind'] == 'music' else 2)


async def request_status(update, context):
    """Payload-free, scope-checked status; no database IDs from other chats leak."""
    scope_topic = getattr(update.effective_message, 'message_thread_id', None) or (-1 if update.effective_chat.type == 'private' else 0)
    if context.args:
        row = await store().status(context.args[0])
    else:
        async with store().pool.acquire() as conn:
            latest = await conn.fetchval('SELECT id FROM arti_requests WHERE chat_id=$1 AND topic_id=$2 ORDER BY seq DESC LIMIT 1', update.effective_chat.id, scope_topic)
        row = await store().status(latest) if latest else None
    if not row or row['chat_id'] != update.effective_chat.id or row['topic_id'] != scope_topic:
        await update.effective_message.reply_text('Запрос не найден в этом чате.')
        return
    labels = {'queued':'в очереди', 'running':'обрабатывается', 'prepared':'готовится отправка',
              'paused':'приостановлен после прерывания генерации; нужен явный повтор', 'completed':'завершён', 'expired':'истёк срок ожидания', 'succeeded':'завершён', 'failed':'остановлен', 'cancelled':'отменён',
              'delivery_unknown':'отправка не подтверждена; автоматического повтора не будет'}
    label = 'истёк срок ожидания' if row.get('error_code') == 'deadline_exceeded' else labels.get(row['state'], row['state'])
    text=f"Запрос {row['id']}: {label}."
    markup=None
    if row['kind'] in ('dubbing','vclone'):
        from bot.media_retention import Retention
        owner=update.effective_user.id
        retained=await Retention(store().pool).load(row['id'],'result',owner,row['chat_id'],row['topic_id'])
        if retained:
            text+=f"\nЛокальный результат сохранён до {retained['expires_at'].isoformat()}. ID: {row['id']}. Экспорт на ПК: python -m tools.export_media_result --request {row['id']} --owner {owner} --output ИМЯ_ФАЙЛА"
        if row['state']=='paused':
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            text+=f"\nДанные доступны до {row['deadline_at'].isoformat()}. Прерванный вызов мог уже завершиться у провайдера. Повтор может заново использовать GPU или платный API."
            markup=InlineKeyboardMarkup([[InlineKeyboardButton('Повторить генерацию (возможны расходы)',callback_data='media_retry:'+row['id'])]])
    await update.effective_message.reply_text(text,reply_markup=markup)


async def _notice(job, bot, ordinal, text):
    from cognition.runtime import CURRENT_TURN
    turn_token = CURRENT_TURN.set(None)
    request_token = CURRENT_REQUEST.set(dict(job, _ordinal=ordinal-1))
    kwargs = {'chat_id': job['chat_id'], 'text': text}
    if job['topic_id'] > 0:
        kwargs['message_thread_id'] = job['topic_id']
    try:
        await bot.send_message(**kwargs)
    finally:
        CURRENT_TURN.reset(turn_token)
        CURRENT_REQUEST.reset(request_token)


async def _progress(job, bot):
    """One durable wait notice in a separate ordinal; never changes reply ordinals."""
    await asyncio.sleep(8)
    try:
        await _notice(job, bot, 0,
            f"Запрос {job['id']} ещё обрабатывается в фоне. Статус: /request {job['id']}. Отменить: /cancel.")
    except Exception:
        diagnostic(job, 'wait_notice_unconfirmed')


async def _failure_notice(job, bot, text=None):
    """Update a cosmetic wait receipt; never fall back after unknown media send."""
    if not await store().guard(job['id'], job['token']): return
    async with store().pool.acquire() as conn:
        unknown = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM arti_request_sends WHERE request_id=$1 AND ordinal>0 AND state IN ('sending','delivery_unknown'))",job['id'])
        row = await conn.fetchrow('SELECT state,receipt FROM arti_request_sends WHERE request_id=$1 AND ordinal=0',job['id'])
    if unknown: return
    text = text or f"Запрос {job['id']} остановлен: не удалось подготовить медиа. Статус: /request {job['id']}. Можно повторить запрос."
    if row and row['state']=='delivered':
        receipt = json.loads(row['receipt']) if isinstance(row['receipt'],str) else row['receipt']
        if isinstance(receipt,dict) and receipt.get('message_id'):
            from telegram.ext import ExtBot
            await ExtBot.edit_message_text(bot, chat_id=job['chat_id'],message_id=receipt['message_id'],text=text)
    elif row is None or row['state']=='prepared':
        await _notice(job,bot,0,text)
