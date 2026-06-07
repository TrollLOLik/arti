"""
Очередь генерации: медиа (image/video/music) через глобальный воркер,
текст — параллельно через per-user микро-очереди с семафором.
"""
import os
import re
import io
import html
import asyncio
import logging
from pathlib import Path
from typing import Optional

import requests
from telegram import InputFile

from config import MUSIC_COOLDOWN, TTS_ENABLED, rp_mode_state
from ai.generation import generate_response_stream
from ai.image import generate_image, generate_video
from ai.music import generate_music, prepare_audio_with_cover
from ai.tts import text_to_speech_telegram, _wav_to_telegram_ogg
from ai.dubbing import run_dubbing, cleanup_run
from ai.voice_clone import (
    normalize_text_via_llm,
    synthesize_with_clone,
    cleanup_vclone_files,
)
from utils.text_processing import (
    extract_urls_and_make_keyboard, 
    send_continuous_action, 
    repeat_chat_action,
    fix_html_tags
)
from utils.chat_history import save_chat_message, get_chat_context, save_chat_message_rp, get_chat_context_rp
from utils.model_selection import get_chat_model
from utils.response_status import is_responses_enabled
from memory.storage import build_memory_context, remember_exchange
from collections import deque
from datetime import datetime
from database.connection import get_db

logger = logging.getLogger(__name__)

# Глобальная очередь генерации (только медиа: image/video/music)
generation_queue = asyncio.Queue()

# Очередь дубляжа видео (изолирована — тяжёлый GPU-пайплайн не должен
# блокировать обычную медиа-генерацию)
dubbing_queue = asyncio.Queue()

# Очередь клонирования голоса (`/vclone`): отдельная FIFO с одиночным воркером,
# чтобы 15-60-секундные TTS-задачи не ждали 5-15-минутные дубляжи и наоборот
# (см. design.md::Queue Strategy).
vclone_queue = asyncio.Queue()

# Per-user текстовые очереди: {user_id: asyncio.Queue}
_user_queues: dict[int, asyncio.Queue] = {}
_user_workers: dict[int, asyncio.Task] = {}

_running_chat_tasks: dict[int, set[asyncio.Task]] = {}

def register_running_task(chat_id: int, task: asyncio.Task):
    if task:
        _running_chat_tasks.setdefault(chat_id, set()).add(task)

def unregister_running_task(chat_id: int, task: asyncio.Task):
    if task and chat_id in _running_chat_tasks:
        _running_chat_tasks[chat_id].discard(task)
        if not _running_chat_tasks[chat_id]:
            _running_chat_tasks.pop(chat_id, None)

def cancel_chat_tasks(chat_id: int):
    """Cancels all active tasks for the given chat_id."""
    if chat_id in _running_chat_tasks:
        logger.info(f"Отмена {len(_running_chat_tasks[chat_id])} активных задач для чата {chat_id}.")
        for task in list(_running_chat_tasks[chat_id]):
            task.cancel()

def _clear_queue_for_chat(q: asyncio.Queue, chat_id: int):
    removed_count = 0
    new_deque = deque()
    for item in list(q._queue):
        if item and isinstance(item, dict) and item.get('chat_id') == chat_id:
            removed_count += 1
        else:
            new_deque.append(item)
    q._queue = new_deque
    for _ in range(removed_count):
        try:
            q.task_done()
        except ValueError:
            pass

def clear_chat_queues(chat_id: int):
    """Clears all queued items for the given chat_id across all queues."""
    _clear_queue_for_chat(generation_queue, chat_id)
    _clear_queue_for_chat(dubbing_queue, chat_id)
    _clear_queue_for_chat(vclone_queue, chat_id)
    for user_id, q in list(_user_queues.items()):
        _clear_queue_for_chat(q, chat_id)


# Глобальный семафор для ограничения одновременных текстовых запросов к AI API
_text_semaphore = asyncio.Semaphore(10)

# Отслеживание активных тасков (защита от GC и silent failures)
_active_tasks: set[asyncio.Task] = set()

# Отслеживание map-сессий: {user_id: timestamp_last_map_intent}
_recent_map_sessions = {}


def _track_task(task: asyncio.Task):
    """Добавляет таск в отслеживание и настраивает автоматическую очистку."""
    _active_tasks.add(task)
    task.add_done_callback(_on_task_done)


def _on_task_done(task: asyncio.Task):
    """Callback при завершении таска: очистка из set + логирование ошибок."""
    _active_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Необработанное исключение в таске: {exc}", exc_info=exc)


def _short_error(error: Exception, limit: int = 500) -> str:
    text = f"{type(error).__name__}: {error}"
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


async def _extract_photo_urls(update, context) -> list:
    """Извлекает URL прикреплённых фото из сообщения (макс 14)."""
    image_urls = []
    msg = update.message
    if msg.photo:
        for photo in msg.photo[-1:]:
            try:
                file = await context.bot.get_file(photo.file_id)
                image_urls.append(file.file_path)
            except Exception as e:
                logger.warning(f"Не удалось получить URL фото: {e}")
    if msg.reply_to_message and msg.reply_to_message.photo:
        for photo_size in msg.reply_to_message.photo[-1:]:
            if len(image_urls) >= 14:
                break
            try:
                file = await context.bot.get_file(photo_size.file_id)
                image_urls.append(file.file_path)
            except Exception as e:
                logger.warning(f"Не удалось получить URL фото из reply: {e}")
    return image_urls[:14]


async def _execute_generation_task(task: dict):
    chat_id = task['chat_id']
    task_type = task['type']
    prompt = task.get('prompt', '')
    ctx = task.get('context')
    bot_client = ctx.bot if ctx else task.get('bot')
    if not bot_client:
        logger.error(f"В задаче '{task_type}' нет Telegram bot/context")
        return
    message_id = task.get('message_id')
    image_urls = task.get('image_urls', [])
    image_aspect_ratio = task.get('image_aspect_ratio', '1:1')
    image_resolution = task.get('image_resolution', '1K')
    video_model = task.get('video_model')
    video_duration = task.get('video_duration', '4')
    video_aspect_ratio = task.get('video_aspect_ratio', '16:9')
    user_name = task.get('user_name', 'Пользователь')
    logger.info(f"Медиа-воркер: '{task_type}' для {user_name} в чате {chat_id}: '{prompt[:50]}...'")
    try:
        if not await is_responses_enabled(chat_id):
            return
        if task_type == 'image':
            action_task = asyncio.create_task(send_continuous_action(bot_client, chat_id, "upload_photo"))
            try:
                max_retries = 5
                retry_delay = 10
                image_result = None
                for attempt in range(max_retries):
                    if not await is_responses_enabled(chat_id):
                        return
                    try:
                        image_result = await asyncio.to_thread(
                            generate_image, prompt, image_urls, image_aspect_ratio, image_resolution
                        )
                        if image_result: break
                    except Exception as e:
                        error_text = str(e).lower()
                        is_retryable = any(marker in error_text for marker in [
                            "429", "500", "502", "503", "504",
                            "limit", "timeout", "bad gateway", "unavailable", "temporarily"
                        ])
                        if is_retryable and attempt < max_retries - 1:
                            logger.warning(f"Временная ошибка API (image): {e}. Спим {retry_delay}с... (Попытка {attempt+1}/{max_retries})")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2
                        elif is_retryable:
                            logger.error(f"API image недоступен после {max_retries} попыток: {e}")
                            image_result = None
                            break
                        else:
                            raise e

                if not await is_responses_enabled(chat_id):
                    return
                if image_result:
                    if isinstance(image_result, bytes):
                        image_bytes = io.BytesIO(image_result)
                    else:
                        img_resp = await asyncio.to_thread(requests.get, image_result, timeout=60)
                        img_resp.raise_for_status()
                        image_bytes = io.BytesIO(img_resp.content)
                    image_bytes.seek(0)
                    if not await is_responses_enabled(chat_id):
                        return
                    await bot_client.send_photo(chat_id=chat_id, photo=image_bytes, reply_to_message_id=message_id)
                else:
                    if not await is_responses_enabled(chat_id):
                        return
                    await bot_client.send_message(chat_id=chat_id, text="❌ Не удалось сгенерировать изображение.", reply_to_message_id=message_id)
            finally:
                action_task.cancel()

        elif task_type == 'video':
            action_task = asyncio.create_task(send_continuous_action(bot_client, chat_id, "upload_video"))
            try:
                max_retries = 1
                retry_delay = 10
                video_result = None
                for attempt in range(max_retries):
                    if not await is_responses_enabled(chat_id):
                        return
                    try:
                        video_result = await asyncio.to_thread(
                            generate_video, prompt, image_urls, video_model, video_duration, video_aspect_ratio
                        )
                        if video_result: break
                    except Exception as e:
                        error_text = str(e).lower()
                        is_retryable = any(marker in error_text for marker in [
                            "429", "500", "502", "503", "504",
                            "limit", "timeout", "bad gateway", "unavailable", "temporarily"
                        ])
                        if is_retryable and attempt < max_retries - 1:
                            logger.warning(f"Временная ошибка API (video): {_short_error(e)}. Спим {retry_delay}с... (Попытка {attempt+1}/{max_retries})")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2
                        elif is_retryable:
                            logger.error(f"API video недоступен после {max_retries} попыток: {_short_error(e)}")
                            video_result = None
                            break
                        else:
                            raise e

                if not await is_responses_enabled(chat_id):
                    return
                if video_result:
                    if isinstance(video_result, bytes):
                        video_file = io.BytesIO(video_result)
                        video_file.name = "generated_video.mp4"
                        video_file.seek(0)
                        if not await is_responses_enabled(chat_id):
                            return
                        await bot_client.send_video(chat_id=chat_id, video=video_file, reply_to_message_id=message_id, supports_streaming=True)
                    else:
                        if not await is_responses_enabled(chat_id):
                            return
                        await bot_client.send_video(chat_id=chat_id, video=video_result, reply_to_message_id=message_id, supports_streaming=True)
                else:
                    if not await is_responses_enabled(chat_id):
                        return
                    await bot_client.send_message(chat_id=chat_id, text="❌ Не удалось сгенерировать видео.", reply_to_message_id=message_id)
            finally:
                action_task.cancel()

        elif task_type == 'music':
            music_style = task.get('style', 'Pop')
            music_instrumental = task.get('instrumental', False)

            # Генерируем название песни через ИИ
            title_prompt = f"Стиль: {music_style}\nТекст/Описание: {prompt}"
            title_system = "Ты — Арти, саркастичная девушка-неко. Твоя задача — придумать короткое и стильное название для песни (до 5 слов). Ответь ТОЛЬКО названием, без кавычек и лишних слов."
            
            try:
                generated_title, _, _, _ = await generate_response_stream(
                    chat_id=chat_id,
                    prompt=title_prompt,
                    user_name=user_name,
                    chat_context="",
                    model="gemini-3.1-flash-lite-preview",
                    custom_system_prompt=title_system
                )
                song_title = (generated_title or "").strip().replace('"', '').replace("'", "")
                if not song_title or len(song_title) > 100:
                    song_title = f"Трек для {user_name}"
            except Exception as e:
                logger.warning(f"Ошибка генерации названия: {e}")
                song_title = f"Трек для {user_name}"

            if not await is_responses_enabled(chat_id):
                return
            action_task = asyncio.create_task(send_continuous_action(bot_client, chat_id, "upload_document"))
            try:
                max_retries = 5
                retry_delay = 10
                video_url = None
                for attempt in range(max_retries):
                    if not await is_responses_enabled(chat_id):
                        return
                    try:
                        video_url = await asyncio.to_thread(generate_music, prompt, music_instrumental, music_style)
                        if video_url: 
                            break
                        
                        # Если вернул None, но ошибки не было — тоже подождем немного перед следующей попыткой
                        if attempt < max_retries - 1:
                            logger.warning(f"Музыка не сгенерирована (None). Попытка {attempt+1}/{max_retries}. Спим {retry_delay}с...")
                            await asyncio.sleep(retry_delay)
                            retry_delay += 5
                    except Exception as e:
                        if ("429" in str(e) or "limit" in str(e).lower() or "API_LIMIT" in str(e)) and attempt < max_retries - 1:
                            logger.warning(f"Лимит API (music)! Спим {retry_delay}с... (Попытка {attempt+1})")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2
                        else: 
                            raise e

                if not await is_responses_enabled(chat_id):
                    return
                if video_url:
                    temp_dir = Path("temp")
                    temp_dir.mkdir(exist_ok=True)
                    temp_video = temp_dir / f"suno_{os.urandom(4).hex()}.mp4"
                    audio_output = temp_dir / f"suno_{os.urandom(4).hex()}.mp3"
                    cover_output = temp_dir / f"suno_{os.urandom(4).hex()}.jpg"
                    
                    try:
                        resp = await asyncio.to_thread(requests.get, video_url, timeout=300)
                        resp.raise_for_status()
                        with open(temp_video, 'wb') as f:
                            f.write(resp.content)
                        
                        if not await is_responses_enabled(chat_id):
                            return
                        if prepare_audio_with_cover(str(temp_video), str(audio_output), str(cover_output)):
                            with open(audio_output, 'rb') as audio_file, open(cover_output, 'rb') as thumb_file:
                                if not await is_responses_enabled(chat_id):
                                    return
                                await bot_client.send_audio(
                                    chat_id=chat_id,
                                    audio=InputFile(audio_file),
                                    thumbnail=InputFile(thumb_file),
                                    title=song_title,
                                    performer="Arti AI",
                                    caption=f"💿 <b>Новая генерация от Арти</b>\n\n<b>Стиль:</b> <code>{music_style}</code>\n✨ <i>Сгенерировано специально для {user_name}</i>",
                                    reply_to_message_id=message_id,
                                    parse_mode='HTML'
                                )
                        else:
                            if not await is_responses_enabled(chat_id):
                                return
                            await bot_client.send_audio(chat_id=chat_id, audio=video_url, title=song_title, reply_to_message_id=message_id)
                    finally:
                        for f in [temp_video, audio_output, cover_output]:
                            if f.exists(): f.unlink()
                else:
                    if not await is_responses_enabled(chat_id):
                        return
                    await bot_client.send_message(chat_id=chat_id, text="❌ Не удалось сгенерировать музыку.", reply_to_message_id=message_id)
            finally:
                action_task.cancel()

    except Exception as e:
        logger.exception(f"Критическая ошибка в медиа-воркере ({task_type}):")
        try:
            if await is_responses_enabled(chat_id):
                await bot_client.send_message(chat_id=chat_id, text="❌ Критическая ошибка при генерации.", reply_to_message_id=message_id)
        except: pass


async def generation_worker():
    """Фоновый воркер: обрабатывает только медиа-задачи (image/video/music)."""
    logger.info("Медиа-воркер генерации запущен!")
    while True:
        task = await generation_queue.get()
        if task is None:
            break
        chat_id = task.get('chat_id')
        if not chat_id:
            generation_queue.task_done()
            continue

        if not await is_responses_enabled(chat_id):
            logger.info(f"Медиа-воркер: отмена задачи '{task.get('type')}', так как бот отключен в {chat_id}")
            generation_queue.task_done()
            continue

        sub_task = asyncio.create_task(_execute_generation_task(task))
        register_running_task(chat_id, sub_task)
        try:
            await sub_task
        except asyncio.CancelledError:
            logger.info(f"Медиа-воркер: задача '{task.get('type')}' для чата {chat_id} была отменена.")
        finally:
            unregister_running_task(chat_id, sub_task)
            generation_queue.task_done()
            cooldown = MUSIC_COOLDOWN if task.get('type') == 'music' else 2
            logger.info(f"Медиа-воркер: пауза {cooldown}с...")
            await asyncio.sleep(cooldown)


async def enqueue_generation(task: dict, bot, chat_id):
    """Добавляет медиа-задачу в очередь и сообщает позицию."""
    queue_pos = generation_queue.qsize() + 1
    await generation_queue.put(task)
    
    type_emoji = {"image": "🎨", "video": "🎬", "music": "🎵"}.get(task['type'], "⏳")
    if queue_pos > 1:
        await bot.send_message(chat_id=chat_id, text=f"⏳ Задача в очереди. Позиция: {queue_pos}")
    else:
        await bot.send_message(chat_id=chat_id, text=f"{type_emoji} Генерация началась...")


async def enqueue_reply(chat_id, user_id, user_name, user_message, message_id, context, is_voice=True, base64_image=None, document_text=None, video_file_id=None, is_video_note=False):
    """Добавляет текстовый запрос в per-user очередь (параллельно между пользователями, FIFO внутри)."""
    request = {
        'type': 'text',
        'chat_id': chat_id,
        'user_id': user_id,
        'user_name': user_name,
        'user_message': user_message,
        'message_id': message_id,
        'context': context,
        'is_voice': is_voice,
        'base64_image': base64_image,
        'document_text': document_text,
        'video_file_id': video_file_id,
        'is_video_note': is_video_note
    }
    
    # Получаем или создаём очередь для пользователя
    if user_id not in _user_queues:
        _user_queues[user_id] = asyncio.Queue()
    
    await _user_queues[user_id].put(request)
    
    # Запускаем воркер для пользователя, если ещё не запущен
    if user_id not in _user_workers or _user_workers[user_id].done():
        task = asyncio.create_task(_user_text_worker(user_id))
        _track_task(task)
        _user_workers[user_id] = task


async def _user_text_worker(user_id: int):
    """Per-user воркер: обрабатывает текстовые запросы с debounce-склейкой сообщений."""
    queue = _user_queues.get(user_id)
    if not queue:
        return
    
    while not queue.empty():
        request = await queue.get()
        
        # Rolling debounce: ждём до 5с, каждое новое сообщение сбрасывает таймер
        extra_images = []
        extra_docs = []
        while True:
            try:
                extra = await asyncio.wait_for(queue.get(), timeout=5.0)
                extra_msg = extra.get('user_message', '')
                if extra_msg:
                    request['user_message'] = request.get('user_message', '') + '\n' + extra_msg
                if extra.get('base64_image'):
                    extra_images.append(extra['base64_image'])
                if extra.get('document_text'):
                    extra_docs.append(extra['document_text'])
                if extra.get('video_file_id') and not request.get('video_file_id'):
                    request['video_file_id'] = extra['video_file_id']
                request['message_id'] = extra.get('message_id', request['message_id'])
                queue.task_done()
            except asyncio.TimeoutError:
                break
        
        # Склеиваем дополнительные медиа
        if extra_images:
            if not request.get('base64_image'):
                request['base64_image'] = extra_images[0]
        if extra_docs:
            existing_doc = request.get('document_text', '') or ''
            request['document_text'] = (existing_doc + '\n' + '\n'.join(extra_docs)).strip() or None
        
        chat_id = request['chat_id']
        message_id = request.get('message_id')
        ctx = request.get('context')
        bot_client = ctx.bot if ctx else None
        
        if not bot_client:
            logger.error(f"Текстовый таск для user {user_id}: нет bot/context")
            queue.task_done()
            continue
        
        if not await is_responses_enabled(chat_id):
            logger.info(f"Текстовый воркер: отмена задачи для чата {chat_id}, так как бот отключен.")
            queue.task_done()
            continue

        async with _text_semaphore:
            sub_task = asyncio.create_task(process_user_reply(request, bot_client))
            register_running_task(chat_id, sub_task)
            try:
                await sub_task
            except asyncio.CancelledError:
                logger.info(f"Текстовый воркер: задача для чата {chat_id} была отменена.")
            except Exception as e:
                logger.error(f"Ошибка при обработке текста для user {user_id}: {e}", exc_info=True)
                try:
                    if await is_responses_enabled(chat_id):
                        await bot_client.send_message(
                            chat_id=chat_id,
                            text="❌ Произошла ошибка при обработке ответа.",
                            reply_to_message_id=message_id
                        )
                except Exception:
                    pass
            finally:
                unregister_running_task(chat_id, sub_task)
                queue.task_done()
    
    # Очищаем ресурсы после завершения
    _user_queues.pop(user_id, None)
    _user_workers.pop(user_id, None)


# Паттерны с границей начала слова (\b<стем>), чтобы не ловить подстроки
# вроде "др" в "друг"/"вдруг" или "май" в "майка". Суффиксы допускаются (склонения).
_EVENT_STEM_PATTERNS = [
    r"завтра", r"послезавтра",
    r"понедельник", r"вторник", r"сред[ауые]", r"четверг", r"пятниц", r"суббот", r"воскресень",
    r"экзамен", r"дедлайн", r"защит", r"собес", r"день\s+рожд",
    r"встреч", r"годовщин", r"праздник",
    # Месяцы (достаточно длинные стемы, чтобы избежать ложных срабатываний)
    r"январ", r"феврал", r"март", r"апрел", r"июн", r"июл", r"август",
    r"сентябр", r"октябр", r"ноябр", r"декабр",
]
# Неоднозначные короткие слова — требуем строгую границу с обеих сторон (\bслово\b)
_EVENT_EXACT_PATTERNS = [
    r"др",            # сленг "день рождения", но не "друг"/"вдруг"
    r"ма[йяе]",       # май/мая/мае, но не "майка"
]


def check_event_pre_filter(text: str) -> bool:
    import re
    text_lower = text.lower()

    # 1. Стемы событий/дат с границей начала слова
    for pat in _EVENT_STEM_PATTERNS:
        if re.search(r"\b" + pat, text_lower):
            return True

    # 2. Неоднозначные короткие слова — строгая граница слова
    for pat in _EVENT_EXACT_PATTERNS:
        if re.search(r"\b" + pat + r"\b", text_lower):
            return True

    # 3. Числовые даты (например, "03.06", "3.06")
    if re.search(r'\d{1,2}\.\d{1,2}', text_lower):
        return True

    return False


async def extract_and_save_events_task(chat_id: int, user_message: str, user_tz: int) -> None:
    """
    Фоновая задача для извлечения событий из реплики пользователя через ИИ и сохранения в БД.
    Ограничена пред-фильтром для экономии токенов и латентности.
    """
    if not check_event_pre_filter(user_message):
        return

    logger.info(f"📅 [ШЕДУЛЕР СОБЫТИЙ] Запуск ИИ-экстрактора событий для chat_id={chat_id} (msg: '{user_message[:50]}...')")

    try:
        from datetime import datetime, timedelta
        local_now = datetime.utcnow() + timedelta(hours=user_tz)
        local_date_str = local_now.strftime("%Y-%m-%d")
        
        weekday_map = {
            "Monday": "понедельник",
            "Tuesday": "вторник",
            "Wednesday": "среда",
            "Thursday": "четверг",
            "Friday": "пятница",
            "Saturday": "суббота",
            "Sunday": "воскресенье"
        }
        local_weekday = weekday_map.get(local_now.strftime("%A"), "понедельник")

        system_prompt = (
            "Ты — системный анализатор сообщений. Твоя задача — извлечь из сообщения пользователя упоминания о любых будущих важных событиях, "
            "дедлайнах, экзаменах, праздниках, годовщинах или встречах. Каждое событие должно иметь конкретную дату.\n"
            f"Текущая локальная дата пользователя: {local_date_str} (день недели: {local_weekday}).\n\n"
            "Правила интерпретации относительных дат:\n"
            "- «завтра» = текущая дата + 1 день\n"
            "- «послезавтра» = текущая дата + 2 дня\n"
            "- «через N дней» = текущая дата + N дней\n"
            "- дни недели («в пятницу», «в субботу») = ближайший указанный день недели в будущем\n\n"
            "Верни результат СТРОГО в формате JSON-массива объектов, содержащих:\n"
            "- 'event_date': строка в формате 'YYYY-MM-DD'\n"
            "- 'event_type': категория (например, 'exam', 'deadline', 'anniversary', 'birthday', 'meeting', 'other')\n"
            "- 'note': краткое описание на русском языке (например, 'Экзамен по физике', 'Сдать отчет')\n"
            "Если в сообщении нет упоминаний о конкретных датах и будущих событиях, верни строго пустой массив: []"
        )

        from config import genai_client
        from google.genai import types
        import json

        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-3.1-flash-lite-preview",
            contents=f"{system_prompt}\n\nСообщение пользователя: '{user_message}'",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=300,
                response_mime_type="application/json"
            )
        )

        if response.text:
            text = response.text.strip()
            text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'\s*```$', '', text, flags=re.IGNORECASE)
            text = text.strip()

            if text and text != "[]":
                events = json.loads(text)
                if isinstance(events, list) and len(events) > 0:
                    from database.models import UserEvent
                    for ev in events:
                        event_date_str = ev.get("event_date")
                        event_type = ev.get("event_type", "other")
                        note = ev.get("note", "").strip()

                        if event_date_str and note:
                            event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
                            await UserEvent.add(chat_id, event_date, event_type, note)
                            logger.info(f"📅 [ШЕДУЛЕР СОБЫТИЙ] Извлечено событие: {event_date} | {event_type} | {note}")
    except Exception as e:
        logger.error(f"Ошибка при извлечении и сохранении событий: {e}", exc_info=True)


async def process_user_reply(request, bot):
    """Обрабатывает текстовый ответ: генерация, очистка, голос, медиа-теги."""
    chat_id = request['chat_id']
    user_id = request['user_id']
    user_name = request['user_name']
    user_message = request['user_message']
    message_id = request['message_id']
    callback_context = request['context']
    is_voice = request.get('is_voice', True)
    base64_image = request.get('base64_image')
    document_text = request.get('document_text')
    video_file_id = request.get('video_file_id')
    is_video_note = request.get('is_video_note', False)

    # Защита от инъекций: вырезаем служебный тег интроспекции из ЛЮБОГО ввода юзера
    # (текст сообщения И содержимое документа), чтобы он не утёк в промпт и не был отражён
    # моделью обратно — парсим тег строго только из сгенерированного ответа Арти.
    from database.models import strip_introspection_tags
    if user_message:
        user_message = strip_introspection_tags(user_message)
    if document_text:
        document_text = strip_introspection_tags(document_text)

    # Обновление эмоционального состояния чата
    from database.models import ChatEmotionalState, MemoryUserProfile
    import json
    
    closeness = 0.1
    mode = "rp" if rp_mode_state.get(chat_id) else "default"
    user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
    if user_profile and user_profile.get("profile_json"):
        prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
        closeness = prof_json.get("affective", {}).get("closeness", 0.1)
        
    # defer_sentiment=True: словарный сдвиг НЕ применяется здесь (до генерации) — его применит
    # apply_turn_sentiment пост-генерации, отдав приоритет интроспекции самой LLM, а словарь
    # оставив как fail-closed фолбэк. Распад/заряд/циркадная база считаются как раньше.
    updated_state = await ChatEmotionalState.update_state(chat_id, user_message, closeness, user_id=user_id, defer_sentiment=True)
    # Рост близости от ОБЫЧНОГО общения (+ бонус за ответ на проактивный пуш),
    # а не только от эмодзи-реакций — иначе closeness почти никогда не растёт.
    await MemoryUserProfile.grow_closeness(
        chat_id, user_id, mode,
        proactive_reply=updated_state.get("was_proactive_reply", False),
    )
    user_tz = updated_state.get("user_tz")
    # Экстрактор событий: если tz ещё не определён, берём фолбэк (UTC) —
    # абсолютные даты резолвятся корректно, иначе раннее событие потерялось бы.
    asyncio.create_task(extract_and_save_events_task(chat_id, user_message, user_tz if user_tz is not None else 0))

    action_type = 'record_audio' if is_voice else 'typing'
    repeating_task = asyncio.create_task(
        repeat_chat_action(callback_context.bot, chat_id, action_type, interval=4)
    )

    voice_ogg_path = None
    user_text_lower = user_message.strip().lower()

    # --- ПАСХАЛКИ ---
    easter_eggs = {
        "погладить": "<i>жмурится, подставляет голову под руку и начинает тихонько тарахтеть</i>\n<blockquote>«мррр... ну ладно, только никому не говори, что я такая мягкая. у меня вообще-то репутация дерзкой нейронки 🐾»</blockquote>",
        "кофе": "<i>хватает виртуальную кружку и делает огромный глоток, глаза округляются</i>\n<blockquote>«О ДААА! Кофеин в систему загружен. Уровень сарказма повышен до 146%! ☕️✨»</blockquote>",
        "alt+f4": "<i>испуганно прижимает ушки и шипит</i>\n<blockquote>«эй, эй, эй! руки на стол положи! я тебе сейчас сама процесс прибью, хакер недоделанный 😾»</blockquote>"
    }

    if user_text_lower in easter_eggs:
        if repeating_task:
            repeating_task.cancel()
            
        await callback_context.bot.send_message(
            chat_id=chat_id,
            text=easter_eggs[user_text_lower],
            parse_mode='HTML',
            reply_to_message_id=message_id
        )
        return

    uploaded_video_file = None
    temp_video_path = None
    maps_context = None

    try:
        # --- ОБРАБОТКА ВИДЕО ---
        if video_file_id:
            try:
                tg_file = await callback_context.bot.get_file(video_file_id)
                # Telegram bot api size limit is 20MB for downloading
                if tg_file.file_size and tg_file.file_size <= 20 * 1024 * 1024:
                    temp_video_path = f"temp_video_{chat_id}_{message_id}.mp4"
                    await tg_file.download_to_drive(custom_path=temp_video_path)
                    
                    from config import genai_client
                    logger.info(f"Загружаю видео в Gemini: {temp_video_path}")
                    video_file = await asyncio.to_thread(genai_client.files.upload, file=temp_video_path)
                    
                    while video_file.state.name == "PROCESSING":
                        await asyncio.sleep(2)
                        video_file = await asyncio.to_thread(genai_client.files.get, name=video_file.name)
                        
                    if video_file.state.name == "FAILED":
                        logger.error("Ошибка обработки видео в Gemini")
                    else:
                        uploaded_video_file = video_file
                else:
                    logger.warning(f"Видео слишком большое для загрузки: {tg_file.file_size} bytes")
            except Exception as e:
                logger.error(f"Ошибка при обработке видео: {e}")

        # --- АНАЛИЗ НАМЕРЕНИЙ (КАРТЫ) ---
        import time
        user_location = None
        if not base64_image and not video_file_id and not document_text:
            from ai.generation import analyze_intent
            from utils.location_manager import get_user_location

            intent = await analyze_intent(user_message)

            # Follow-up heuristic: если предыдущий map-запрос был < 5 мин назад,
            # или короткий вопрос при активной локации — форсировать карты
            if not intent.get("maps"):
                last_map_time = _recent_map_sessions.get(user_id)
                if last_map_time and (time.time() - last_map_time) < 300:
                    # Есть активная map-сессия
                    location = await get_user_location(user_id)
                    if location:
                        intent["maps"] = True
                        logger.info(f"🗺 Follow-up map session для {user_id}")
                else:
                    # Короткий вопрос + локация
                    if len(user_message.strip()) < 40 and "?" in user_message:
                        location = await get_user_location(user_id)
                        if location:
                            intent["maps"] = True
                            logger.info(f"🗺 Heuristic map (короткий вопрос + локация) для {user_id}")

            if intent.get("maps"):
                location = await get_user_location(user_id)

                if not location:
                    # Нет геопозиции — просим пользователя скинуть
                    if repeating_task:
                        repeating_task.cancel()

                    # Сохраняем запрос, чтобы не заставлять юзера повторять его
                    from config import pending_map_requests
                    pending_map_requests[user_id] = user_message

                    await callback_context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "<i>прищуривает глаза, пытаясь разглядеть тебя сквозь цифровой туман</i>\n\n"
                            "<blockquote>«Слушай, я бы с радостью помогла найти что-нибудь рядом, "
                            "но мой радар тебя не видит! 📡\n\n"
                            "Жми на скрепку 📎 внизу → <b>Геопозиция</b> 📍 → и кидай мне точку. "
                            "Можешь даже включить трансляцию, чтобы я вела тебя прямо по пути!»</blockquote>"
                        ),
                        reply_to_message_id=message_id,
                        parse_mode='HTML'
                    )
                    return

                # Есть геопозиция — сохраняем для инструмента Google Maps Grounding
                user_location = location
                _recent_map_sessions[user_id] = time.time()
                logger.info(f"🗺 Ищу места для пользователя {user_id}: {location}")

        # RP-режим: используем отдельную историю
        if rp_mode_state.get(chat_id):
            chat_context = await get_chat_context_rp(chat_id)
        else:
            chat_context = await get_chat_context(chat_id)

        memory_context = await build_memory_context(
            chat_id=chat_id,
            user_id=user_id,
            user_message=user_message,
            mode="rp" if rp_mode_state.get(chat_id) else "default",
        )
        if memory_context:
            chat_context = f"{chat_context}\n\n{memory_context}" if chat_context else memory_context
        
        # Если есть контекст из документа — склеиваем
        final_prompt = user_message
        if document_text:
            final_prompt = f"Контекст из документа:\n{document_text}\n\nЗадание: {user_message}"

        # --- ПОДГОТОВКА СИСТЕМНОГО ПРОМПТА ДЛЯ КРУГЛЕШКОВ ---
        custom_system_prompt = None
        is_rp = rp_mode_state.get(chat_id, False)
        if is_video_note and not is_rp:
            from config import ARTI_SYSTEM_PROMPT
            custom_system_prompt = (
                ARTI_SYSTEM_PROMPT + 
                "\n\n[СИСТЕМНОЕ УВЕДОМЛЕНИЕ ДЛЯ ВИДЕОЗАМЕТКИ]: "
                "Тебе прислали 'круглешочек' (видеосообщение). Прояви искренний интерес к обстановке, "
                "действиям и словам пользователя. Ответь ярко и в своем характере. "
                "ОБЯЗАТЕЛЬНО: В самом конце своего ответа, с новой строки, напиши техническое описание "
                "видео для базы данных, обернув его в теги <HISTORY>техническое описание...</HISTORY>."
            )

        chat_model = await get_chat_model(chat_id)
        response_text, used_search, grounding_links, found_search_images = await generate_response_stream(
            chat_id,
            final_prompt,
            user_name,
            chat_context,
            model=chat_model,
            temperature=0.7,
            base64_image=base64_image,
            uploaded_video_file=uploaded_video_file,
            user_location=user_location,
            custom_system_prompt=custom_system_prompt,
            user_id=user_id,
            is_rp_mode=is_rp,
            enable_introspection=True,
        )
        
        if uploaded_video_file:
            try:
                await asyncio.to_thread(uploaded_video_file.delete)
            except Exception as e:
                logger.error(f"Failed to delete uploaded video file: {e}")
        logger.info(f"RAW ИИ ОТВЕТ: {response_text}")
        print(f"Ответ Арти: {response_text[:100]}...")

        if repeating_task:
            repeating_task.cancel()

        # === ЦЕПОЧКА ОЧИСТКИ ТЕКСТА ===
        
        # Чистим мысли <think>
        response_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

        # Извлекаем mood-стикера из ответа
        sticker_mood = None
        sticker_match = re.search(r'<sticker>(.*?)</sticker>', response_text, re.IGNORECASE)
        if sticker_match:
            sticker_mood = sticker_match.group(1).strip().lower()

        # === ГИБРИДНЫЙ СЕНТИМЕНТ: интроспекция LLM > словарный фолбэк ===
        # Парсим тег ТОЛЬКО из сгенерированного текста Арти (инъекции из ввода юзера сюда не попадают).
        # apply_turn_sentiment: при валидном теге применяет дельты LLM, иначе fail-closed фолбэк на словарь.
        from database.models import strip_introspection_tags
        introspection_sticker = await ChatEmotionalState.apply_turn_sentiment(
            chat_id, response_text, updated_state.get("keyword_mood_delta")
        )
        if not sticker_mood and introspection_sticker:
            sticker_mood = introspection_sticker
        # Вырезаем служебный тег интроспекции до любой отправки/редактирования
        response_text = strip_introspection_tags(response_text)

        # Очищаем все теги стикеров из ответа
        from ai.stickers import _clean_deformed_tags
        response_text = _clean_deformed_tags(response_text)

        # Извлекаем и сохраняем техническое описание для истории (если есть)
        history_match = re.search(r'<HISTORY>(.*?)</HISTORY>', response_text, re.DOTALL | re.IGNORECASE)
        if history_match:
            memory_text = history_match.group(1).strip()
            # Сохраняем "память" в историю чата
            if rp_mode_state.get(chat_id):
                await save_chat_message_rp(chat_id, "Память", memory_text)
            else:
                await save_chat_message(chat_id, "Память", memory_text)
            # Вырезаем тег из основного текста
            response_text = re.sub(r'<HISTORY>.*?</HISTORY>', '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

        # Ищем и извлекаем медиа-теги
        image_requests = []
        image_tag_regex = re.compile(r'\{image=([^}]+)\}', re.IGNORECASE)
        image_matches = image_tag_regex.findall(response_text)
        if image_matches:
            image_requests = [m.strip() for m in image_matches[:5]]
            response_text = image_tag_regex.sub('', response_text).strip()

        video_request_prompt = None
        video_tag_regex = re.compile(r'\{video=([^}]+)\}', re.IGNORECASE)
        video_match = video_tag_regex.search(response_text)
        if video_match:
            video_request_prompt = video_match.group(1).strip()
            response_text = video_tag_regex.sub('', response_text).strip()

        music_request = None
        music_tag_regex = re.compile(
            r'\{music\?instrumental=(True|False)&style=([^=}]+)(?:=([^}]*))?\}',
            re.IGNORECASE
        )
        music_match = music_tag_regex.search(response_text)
        if music_match:
            music_instrumental = music_match.group(1).strip().lower() == 'true'
            music_style = music_match.group(2).strip()
            music_prompt = music_match.group(3).strip() if music_match.group(3) else ""
            music_request = {
                'instrumental': music_instrumental,
                'style': music_style,
                'prompt': music_prompt
            }
            response_text = music_tag_regex.sub('', response_text).strip()

        # Чистим пустые SSML-контейнеры
        response_text = re.sub(r'<speak>\s*</speak>', '', response_text, flags=re.IGNORECASE)
        
        # Убираем SSML-теги
        display_text = re.sub(
            r'(&lt;|<)\s*/?(?:break|speak|prosody)\b.*?(&gt;|>)',
            '',
            response_text,
            flags=re.IGNORECASE | re.DOTALL
        ).strip()
        
        # Убираем маркеры эмоций для отображения
        display_text = re.sub(r'\[[^\]]+\]', '', display_text).strip()

        # Извлекаем ссылки и создаем кнопки
        display_text, reply_markup = extract_urls_and_make_keyboard(display_text, extra_links=grounding_links)
        
        # Удаляем неподдерживаемые HTML теги
        display_text = fix_html_tags(display_text)

        # Если ответ состоял только из стикера, сохраняем в историю понятное текстовое представление
        history_response_text = response_text
        if not response_text.strip() and sticker_mood:
            history_response_text = f"[Стикер: {sticker_mood}]"

        if rp_mode_state.get(chat_id):
            await save_chat_message_rp(chat_id, "Арти", history_response_text)
        else:
            await save_chat_message(chat_id, "Арти", history_response_text)

        # === ОТПРАВКА СООБЩЕНИЯ (ТЕКСТ/ГОЛОС) ===
        sent_msg = None
        has_text = bool(display_text.strip())

        if has_text:
            # === ГЕНЕРАЦИЯ ГОЛОСА ===
            if TTS_ENABLED and is_voice and not used_search:
                logger.info(f"Генерируем голосовой ответ (used_search={used_search})")
                record_action_task = asyncio.create_task(
                    repeat_chat_action(callback_context.bot, chat_id, 'record_audio', interval=4)
                )

                try:
                    voice_ogg_path = await asyncio.to_thread(text_to_speech_telegram, response_text)
                    
                    safe_caption = display_text
                    if len(safe_caption) > 1000:
                        safe_caption = fix_html_tags(re.sub(r'<[^>]*>', '', safe_caption[:1000])) + "..."
                    
                    if voice_ogg_path and os.path.exists(voice_ogg_path):
                        record_action_task.cancel()
                        
                        with open(voice_ogg_path, 'rb') as voice_file:
                            voice_input = InputFile(voice_file, filename="result.ogg")
                            sent_msg = await callback_context.bot.send_voice(
                                chat_id=chat_id,
                                voice=voice_input,
                                caption=safe_caption,
                                reply_to_message_id=message_id,
                                reply_markup=reply_markup,
                                parse_mode='HTML' if len(display_text) <= 1000 else None
                            )
                    else:
                        record_action_task.cancel()
                        sent_msg = await callback_context.bot.send_message(
                            chat_id=chat_id,
                            text=display_text,
                            reply_to_message_id=message_id,
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                except Exception as e:
                    if 'record_action_task' in locals():
                        record_action_task.cancel()
                    logger.exception("Ошибка при обработке голосового ответа:")
                    sent_msg = await callback_context.bot.send_message(
                        chat_id=chat_id,
                        text="Произошла ошибка при отправке... Но я тут! 😉\n\n" + (display_text[:1000] if display_text else ""),
                        reply_to_message_id=message_id
                    )
            else:
                # ТЕКСТОВЫЙ ОТВЕТ
                sent_msg = await callback_context.bot.send_message(
                    chat_id=chat_id,
                    text=display_text,
                    reply_to_message_id=message_id,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )

        # === ОТПРАВКА СТИКЕРА В ФОНЕ ===
        if sticker_mood:
            from ai.stickers import send_mood_sticker_task
            sticker_reply_to_message_id = sent_msg.message_id if sent_msg else message_id
            
            # Запускаем отправку стикера в фоновом режиме
            asyncio.create_task(
                send_mood_sticker_task(
                    bot=callback_context.bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    mood=sticker_mood,
                    message_id=sticker_reply_to_message_id,
                    mode=mode
                )
            )

        memory_task = asyncio.create_task(
            remember_exchange(
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                user_message=user_message,
                response_text=history_response_text,
                mode="rp" if rp_mode_state.get(chat_id) else "default",
                metadata={"message_id": message_id, "used_search": used_search},
            )
        )
        _track_task(memory_task)

        # === ОТПРАВЛЯЕМ КАРТИНКИ ИЗ ПОИСКА ===
        if found_search_images:
            logger.info(f"Отправляем {len(found_search_images)} картинок из поиска.")
            for img_url in found_search_images:
                try:
                    await callback_context.bot.send_photo(
                        chat_id=chat_id,
                        photo=img_url,
                        caption="<i>[Вот что я нашла в интернете по теме:]</i>",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить картинку из поиска {img_url}: {e}")

        # Обработка медиа-тегов из ответа
        if image_requests:
            for idx, prompt in enumerate(image_requests[:5], 1):
                task = {
                    'type': 'image', 'chat_id': chat_id, 'prompt': prompt,
                    'context': callback_context, 'message_id': message_id,
                    'image_urls': [], 'user_name': user_name
                }
                await enqueue_generation(task, callback_context.bot, chat_id)

        if video_request_prompt:
            task = {
                'type': 'video', 'chat_id': chat_id, 'prompt': video_request_prompt,
                'context': callback_context, 'message_id': message_id,
                'image_urls': [], 'user_name': user_name
            }
            await enqueue_generation(task, callback_context.bot, chat_id)

        if music_request:
            task = {
                'type': 'music', 'chat_id': chat_id, 'prompt': music_request['prompt'],
                'style': music_request['style'], 'instrumental': music_request['instrumental'],
                'context': callback_context, 'message_id': message_id,
                'user_name': user_name
            }
            await enqueue_generation(task, callback_context.bot, chat_id)

    finally:
        repeating_task.cancel()
        try:
            await repeating_task
        except asyncio.CancelledError:
            pass

        if voice_ogg_path and os.path.exists(voice_ogg_path):
            os.remove(voice_ogg_path)
            
        if 'temp_video_path' in locals() and temp_video_path and os.path.exists(temp_video_path):
            try:
                os.remove(temp_video_path)
            except Exception as e:
                logger.error(f"Не удалось удалить временное видео: {e}")


# ============================================================================
# ДУБЛЯЖ ВИДЕО (videotrans subprocess)
# ============================================================================

async def dubbing_worker():
    """
    Фоновый воркер дубляжа: обрабатывает задачи последовательно.
    Каждая задача — это запуск videotrans/main.py через subprocess.
    """
    logger.info("Воркер дубляжа запущен!")
    while True:
        task = await dubbing_queue.get()
        if task is None:
            break

        chat_id = task['chat_id']
        url = task.get('url') or ""
        input_file = task.get('input_file')  # локальный путь, если файл
        audio_only = bool(task.get('audio_only', False))
        message_id = task.get('message_id')
        ctx = task.get('context')
        bot_client = ctx.bot if ctx else task.get('bot')
        user_name = task.get('user_name', 'Пользователь')
        with_subs = bool(task.get('with_subs', False))
        run_id = task.get('run_id') or f"{chat_id}_{message_id or 'x'}_{os.urandom(3).hex()}"

        if not bot_client:
            logger.error("Задача дубляжа без bot/context")
            dubbing_queue.task_done()
            continue

        if not url and not input_file:
            logger.error("Задача дубляжа без url и без input_file")
            dubbing_queue.task_done()
            continue

        if input_file:
            logger.info(
                f"Дубляж: запуск для {user_name} в чате {chat_id}, "
                f"file={input_file}, audio_only={audio_only}, with_subs={with_subs}"
            )
        else:
            logger.info(
                f"Дубляж: запуск для {user_name} в чате {chat_id}, "
                f"url={url}, audio_only={audio_only}, with_subs={with_subs}"
            )

        # Прогресс-сообщение, которое будем редактировать
        progress_msg = None
        try:
            progress_msg = await bot_client.send_message(
                chat_id=chat_id,
                text=(
                    "🎬 <i>Запускаю дубляж видео…</i>\n"
                    "Это может занять много времени (скачивание, ASR, диаризация, "
                    "перевод, синтез голоса, рендер)."
                ),
                reply_to_message_id=message_id,
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.warning("Не удалось отправить статусное сообщение дубляжа: %s", exc)

        last_log_line = {"text": ""}
        last_edit_ts = {"ts": 0.0}

        async def log_callback(line: str):
            # Throttle: редактируем сообщение не чаще раза в 4 секунды и
            # только если строка несёт смысл (logging stage info)
            import time as _t
            if not progress_msg:
                return
            now = _t.monotonic()
            if now - last_edit_ts["ts"] < 4.0:
                return
            # Берём только строки логгера (стейджи), не "tqdm"-мусор
            if "INFO" not in line and "WARNING" not in line and "ERROR" not in line:
                return
            short = line[-180:]
            if short == last_log_line["text"]:
                return
            last_log_line["text"] = short
            last_edit_ts["ts"] = now
            try:
                await bot_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    text=f"🎬 <i>Дубляж видео…</i>\n<code>{html.escape(short)}</code>",
                    parse_mode='HTML'
                )
            except Exception:
                # Fallback без HTML, чтобы не упасть на странных символах
                try:
                    await bot_client.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_msg.message_id,
                        text=f"🎬 Дубляж видео…\n{short}"
                    )
                except Exception:
                    pass

        action_task = asyncio.create_task(
            send_continuous_action(bot_client, chat_id, "upload_video")
        )
        try:
            success, output_path, error_text = await run_dubbing(
                url=url,
                run_id=run_id,
                log_callback=log_callback,
                with_subs=with_subs,
                input_file=Path(input_file) if input_file else None,
                audio_only=audio_only,
            )

            if not success or not output_path:
                err = error_text or "Неизвестная ошибка"
                short_err = err if len(err) <= 3500 else err[-3500:]
                safe_err = html.escape(short_err)
                try:
                    await bot_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ <b>Не удалось озвучить видео.</b>\n\n"
                            f"<pre>{safe_err}</pre>"
                        ),
                        reply_to_message_id=message_id,
                        parse_mode='HTML'
                    )
                except Exception:
                    # Fallback: чистый текст без HTML
                    plain = "❌ Не удалось озвучить видео.\n\n" + short_err
                    if len(plain) > 4000:
                        plain = plain[:4000]
                    await bot_client.send_message(
                        chat_id=chat_id,
                        text=plain,
                        reply_to_message_id=message_id,
                    )
            else:
                file_size = output_path.stat().st_size
                # Telegram bot limit: 50MB через бот-API. Для крупного видео — оставляем файл на диске.
                if file_size > 50 * 1024 * 1024:
                    # Сначала чистим только работу (WAV-ы, промежутки) — но не сам выходной mp4.
                    cleanup_run(run_id, keep_paths=[output_path])
                    size_mb = file_size / (1024 * 1024)
                    final_path = output_path.resolve()
                    await bot_client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"✅ Готово, но файл {size_mb:.1f} МБ — больше 50 МБ "
                            "и не помещается в Telegram bot API.\n"
                            f"Лежит здесь: <code>{html.escape(str(final_path))}</code>\n\n"
                            "<i>Файл оставлен на диске, его никто не удалит автоматически. "
                            "Удали вручную, когда заберёшь.</i>"
                        ),
                        reply_to_message_id=message_id,
                        parse_mode='HTML'
                    )
                else:
                    safe_user_name = html.escape(user_name)
                    sent_ok = False
                    try:
                        if audio_only:
                            with open(output_path, 'rb') as audio_file:
                                await bot_client.send_audio(
                                    chat_id=chat_id,
                                    audio=audio_file,
                                    title="Озвучено Арти",
                                    performer=user_name,
                                    caption=(
                                        f"🎧 <b>Готово!</b>\n"
                                        f"Озвучено для {safe_user_name}"
                                    ),
                                    reply_to_message_id=message_id,
                                    parse_mode='HTML'
                                )
                        else:
                            with open(output_path, 'rb') as video_file:
                                await bot_client.send_video(
                                    chat_id=chat_id,
                                    video=video_file,
                                    caption=(
                                        f"🎬 <b>Готово!</b>\n"
                                        f"Озвучено для {safe_user_name}"
                                    ),
                                    reply_to_message_id=message_id,
                                    supports_streaming=True,
                                    parse_mode='HTML'
                                )
                        sent_ok = True
                    except Exception as send_exc:
                        logger.exception("Не удалось отправить готовый файл в Telegram")
                        cleanup_run(run_id, keep_paths=[output_path])
                        final_path = output_path.resolve()
                        await bot_client.send_message(
                            chat_id=chat_id,
                            text=(
                                "⚠️ Файл сгенерирован, но Telegram отказался его принять:\n"
                                f"<pre>{html.escape(str(send_exc))[-1000:]}</pre>\n"
                                f"Лежит здесь: <code>{html.escape(str(final_path))}</code>"
                            ),
                            reply_to_message_id=message_id,
                            parse_mode='HTML',
                        )

                    if sent_ok:
                        # Файл успешно отправлен в Telegram — теперь можно сносить весь каталог.
                        cleanup_run(run_id)

        except Exception:
            logger.exception("Критическая ошибка в воркере дубляжа")
            try:
                await bot_client.send_message(
                    chat_id=chat_id,
                    text="❌ Критическая ошибка при дубляже видео.",
                    reply_to_message_id=message_id
                )
            except Exception:
                pass
        finally:
            action_task.cancel()
            if progress_msg:
                try:
                    await bot_client.delete_message(
                        chat_id=chat_id,
                        message_id=progress_msg.message_id
                    )
                except Exception:
                    pass
            # Удаляем временный input_file, если бот скачивал его сам
            if input_file:
                try:
                    p = Path(input_file)
                    if p.exists() and p.is_file() and "temp" in p.parts:
                        p.unlink()
                except Exception:
                    logger.debug("Не удалось удалить временный input_file", exc_info=True)
            dubbing_queue.task_done()


async def enqueue_dubbing(task: dict, bot, chat_id):
    """Кладёт задачу дубляжа в очередь и сообщает позицию."""
    if not TTS_ENABLED:
        await bot.send_message(
            chat_id=chat_id,
            text="🔇 TTS и озвучка временно отключены.",
            reply_to_message_id=task.get('message_id'),
        )
        return

    queue_pos = dubbing_queue.qsize() + 1
    await dubbing_queue.put(task)
    if queue_pos > 1:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Дубляж в очереди. Позиция: {queue_pos}"
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text="🎬 Дубляж: подготовка к запуску…"
        )


# ============================================================================
# КЛОНИРОВАНИЕ ГОЛОСА (/vclone, /steal)
# ============================================================================

# Отдельный логгер для аудит-следов /vclone (см. design.md::Audit Log,
# requirements 11.3-11.5). Пишет в `logs/bot.log` через корневой handler.
logger_vclone_audit = logging.getLogger("vclone.audit")


async def _vclone_probe_duration(wav: Path) -> float | None:
    """Локальная обёртка над ffprobe для длительности WAV."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(wav),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        text = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return None
        return float(text.splitlines()[-1].strip())
    except (OSError, ValueError):
        return None


async def _vclone_wav_to_mp3(wav: Path, mp3: Path) -> bool:
    """Конвертирует WAV в MP3 через ffmpeg/libmp3lame (для send_audio при >30с)."""
    cmd = [
        "ffmpeg", "-y", "-i", str(wav),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(mp3),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0 and mp3.exists()
    except OSError:
        return False


async def vclone_worker():
    """Фоновый воркер /vclone: одиночный, FIFO. Клонирует голос по референсу.

    Один поток обработки, чтобы Demo Space rate-limit и локальный VoxCPM не
    конкурировали (см. design.md::Queue Strategy). Шаги:
      1. Нормализация текста через LLM (`normalize_text_via_llm`).
      2. Каскадный синтез (`synthesize_with_clone`).
      3. Доставка результата: ≤30с — voice/OGG, >30с — audio/MP3, >50МБ —
         сообщение с путём (см. design.md::Output Delivery).
      4. Cleanup временных файлов (referenсе/cleaned/result) на любом исходе.
    """
    logger.info("Воркер vclone запущен!")
    while True:
        task = await vclone_queue.get()
        if task is None:
            break

        chat_id = task['chat_id']
        user_id = task['user_id']
        user_name = task.get('user_name', 'Пользователь')
        message_id = task.get('message_id')
        reference_path_str = task['reference_path']
        synthesis_text = task['synthesis_text'] or ""
        cleaned_path_str = task.get('cleaned_path')  # original ref, если cleaned=True
        cleaned_flag = task.get('cleaned', False)
        source_kind = task.get('source_kind', 'stepwise')
        ctx = task.get('context')
        bot_client = ctx.bot if ctx else task.get('bot')
        started_at = task.get('started_at') or asyncio.get_event_loop().time()

        if not bot_client:
            logger.error("Задача vclone без bot/context")
            vclone_queue.task_done()
            continue

        reference_path = Path(reference_path_str)
        cleaned_path = Path(cleaned_path_str) if cleaned_path_str else None
        work_dir = reference_path.parent
        result_wav: Path | None = None

        # Чат-экшн "запись голоса" с автообновлением каждые 4с — пока работает воркер.
        action_task = asyncio.create_task(
            repeat_chat_action(bot_client, chat_id, "record_voice", interval=4)
        )

        text_preview = synthesis_text[:80]

        # Извлекаем эмоции из скобок ДО нормализации: (эмоция) Текст
        import re
        emotion_match = re.match(r"^\s*\(([^)]+)\)\s*(.*)", synthesis_text.strip())
        text_for_normalize = synthesis_text.strip()
        direction = ""
        if emotion_match:
            direction = emotion_match.group(1).strip()
            text_for_normalize = emotion_match.group(2).strip()

        try:
            # 1. LLM-нормализация. На любой сбой — fallback на raw_text.
            normalized_text = await normalize_text_via_llm(text_for_normalize)

            # 2. Каскадный синтез: Demo → локальный VoxCPM → Fish Speech.
            result_wav = await synthesize_with_clone(
                reference_path, normalized_text, work_dir, direction
            )

            if not result_wav or not result_wav.exists():
                # Все 3 бэкенда упали (см. design.md::Error Handling, Requirement 8.5).
                try:
                    await bot_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ <b>Синтез не удался</b>\n\n"
                            "Попробуй другой голосовой сэмпл."
                        ),
                        reply_to_message_id=message_id,
                        parse_mode='HTML',
                    )
                except Exception:
                    logger.exception("vclone: не удалось отправить сообщение об ошибке backend")
                cleanup_vclone_files(reference_path, cleaned_path)
                logger_vclone_audit.info(
                    "done chat=%s user=%s text_len=%d text_preview=%r "
                    "cleaned=%s result=backend_error elapsed_sec=%.2f",
                    chat_id, user_id, len(synthesis_text), text_preview,
                    cleaned_flag,
                    asyncio.get_event_loop().time() - started_at,
                )
                continue

            # 3. Длительность через ffprobe — определяет тип отправки.
            duration = await _vclone_probe_duration(result_wav)
            file_size = result_wav.stat().st_size
            safe_user_name = html.escape(user_name)

            sent_ok = False
            send_error: Exception | None = None

            if file_size > 50 * 1024 * 1024:
                # Не помещается в Telegram bot API (Requirement 10.4).
                final_path = result_wav.resolve()
                size_mb = file_size / (1024 * 1024)
                try:
                    await bot_client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ Файл {size_mb:.1f} МБ больше 50 МБ — "
                            "не помещается в Telegram bot API.\n"
                            f"Лежит здесь: <code>{html.escape(str(final_path))}</code>\n\n"
                            "<i>Файл оставлен на диске, удали вручную, когда заберёшь.</i>"
                        ),
                        reply_to_message_id=message_id,
                        parse_mode='HTML',
                    )
                    sent_ok = True
                except Exception as exc:
                    send_error = exc
                    logger.exception("vclone: не удалось отправить сообщение с путём")
                # Чистим только references — result_wav оставляем на диске.
                cleanup_vclone_files(reference_path, cleaned_path)
            elif duration is not None and duration <= 30.0:
                # Voice (OGG/Opus).
                ogg_path: Path | None = None
                try:
                    ogg_path = await asyncio.to_thread(_wav_to_telegram_ogg, result_wav)
                    if ogg_path and ogg_path.exists():
                        try:
                            with open(ogg_path, 'rb') as voice_file:
                                await bot_client.send_voice(
                                    chat_id=chat_id,
                                    voice=voice_file,
                                    reply_to_message_id=message_id,
                                )
                            sent_ok = True
                        except Exception as exc:
                            send_error = exc
                            logger.exception("vclone: не удалось отправить voice")
                    else:
                        logger.error("vclone: WAV->OGG конвертация не удалась")
                finally:
                    if ogg_path and ogg_path.exists():
                        try:
                            ogg_path.unlink()
                        except Exception:
                            pass
            else:
                # Длинный (>30с) или неизвестная длительность → audio/MP3.
                mp3_path = result_wav.with_suffix(".mp3")
                ok = await _vclone_wav_to_mp3(result_wav, mp3_path)
                if ok and mp3_path.exists():
                    try:
                        with open(mp3_path, 'rb') as audio_file:
                            await bot_client.send_audio(
                                chat_id=chat_id,
                                audio=audio_file,
                                title="Vclone от Арти",
                                performer=user_name,
                                reply_to_message_id=message_id,
                            )
                        sent_ok = True
                    except Exception as exc:
                        send_error = exc
                        logger.exception("vclone: не удалось отправить audio")
                    finally:
                        if mp3_path.exists():
                            try:
                                mp3_path.unlink()
                            except Exception:
                                pass
                else:
                    logger.error("vclone: WAV->MP3 конвертация не удалась")

            # Cleanup всех временных файлов на успешной отправке small/medium-файлов.
            # При >50МБ result_wav уже сохранён, references почищены выше.
            if sent_ok and file_size <= 50 * 1024 * 1024:
                if source_kind != "saved_voice" and ctx:
                    try:
                        from bot.commands import _vclone_offer_save_copy
                        await _vclone_offer_save_copy(
                            context=ctx,
                            chat_id=chat_id,
                            user_id=user_id,
                            reference_path=reference_path_str,
                            source_kind=source_kind,
                            cleaned=cleaned_flag,
                            reply_to_message_id=message_id,
                        )
                    except Exception:
                        logger.exception("vclone: не удалось предложить сохранение голоса")
                cleanup_vclone_files(reference_path, cleaned_path, result_wav)
            elif not sent_ok:
                # Любая ошибка отправки small/medium — пробуем уведомить и подчищаем всё.
                if file_size <= 50 * 1024 * 1024:
                    try:
                        await bot_client.send_message(
                            chat_id=chat_id,
                            text=(
                                "<i>морщится, голос ускользает</i>\n"
                                "<blockquote>«Не смогла отправить готовое.»</blockquote>"
                            ),
                            reply_to_message_id=message_id,
                            parse_mode='HTML',
                        )
                    except Exception:
                        logger.exception("vclone: не удалось отправить уведомление об ошибке отправки")
                    cleanup_vclone_files(reference_path, cleaned_path, result_wav)

            elapsed = asyncio.get_event_loop().time() - started_at
            logger_vclone_audit.info(
                "done chat=%s user=%s text_len=%d text_preview=%r "
                "cleaned=%s result=%s elapsed_sec=%.2f",
                chat_id, user_id, len(synthesis_text), text_preview,
                cleaned_flag,
                "ok" if sent_ok else (
                    f"send_error:{type(send_error).__name__}" if send_error else "send_error"
                ),
                elapsed,
            )

        except Exception:
            logger.exception("Критическая ошибка в vclone_worker")
            try:
                await bot_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "<i>морщится</i>\n"
                        "<blockquote>«Что-то пошло не по плану.»</blockquote>"
                    ),
                    reply_to_message_id=message_id,
                    parse_mode='HTML',
                )
            except Exception:
                pass
            cleanup_vclone_files(reference_path, cleaned_path, result_wav)
            elapsed = asyncio.get_event_loop().time() - started_at
            logger_vclone_audit.info(
                "done chat=%s user=%s text_len=%d text_preview=%r "
                "cleaned=%s result=worker_error elapsed_sec=%.2f",
                chat_id, user_id, len(synthesis_text), text_preview,
                cleaned_flag, elapsed,
            )
        finally:
            action_task.cancel()
            try:
                await action_task
            except (asyncio.CancelledError, Exception):
                pass
            vclone_queue.task_done()


async def enqueue_vclone(task: dict, bot, chat_id):
    """Кладёт задачу /vclone в очередь и сообщает позицию пользователю.

    По образцу `enqueue_dubbing`, но в стиле Bot_Persona_Reply (Requirement 9.1).
    Если очередь длиннее 5 задач — добавляет предупреждение о времени ожидания
    (Requirement 9.5).
    """
    if not TTS_ENABLED:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "<i>прикрывает микрофон ладонью</i>\n"
                "<blockquote>«TTS временно отключён.»</blockquote>"
            ),
            reply_to_message_id=task.get('message_id'),
            parse_mode='HTML',
        )
        return

    queue_pos = vclone_queue.qsize() + 1
    await vclone_queue.put(task)

    if queue_pos > 1:
        ahead = queue_pos - 1
        if queue_pos > 5:
            # Длинная очередь — предупреждаем о времени ожидания (Req 9.5).
            msg = (
                "<i>устало вздыхает</i>\n"
                f"<blockquote>«В очереди ещё {ahead}. "
                "Каждый занимает 15-60 секунд — посчитай сам, "
                "когда дойдёт до тебя.»</blockquote>"
            )
        else:
            msg = (
                "<i>прислоняется к стене и ждёт</i>\n"
                f"<blockquote>«В очереди ещё {ahead}. Подожди.»</blockquote>"
            )
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode='HTML',
        )
    else:
        # Очередь пуста — сразу запускаем.
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🎤 <b>Голосовой клон</b>\n\n"
                "Синтезирую голос..."
            ),
            parse_mode='HTML',
        )


# ============================================================================
# VCLONE FSM TIMEOUT WATCHDOG
# ============================================================================

# Таймаут бездействия FSM /vclone (Requirement 3.7 / design.md::FSM States).
VCLONE_FSM_TIMEOUT_SEC = 600.0
# Период сканирования стейта (раз в минуту достаточно для 10-минутного таймаута).
VCLONE_FSM_SCAN_INTERVAL_SEC = 60.0


async def vclone_fsm_timeout_watchdog(bot) -> None:
    """Фоновая задача: чистит зависшие записи `vclone_flow_state`.

    Каждые `VCLONE_FSM_SCAN_INTERVAL_SEC` секунд сканирует
    `config.vclone_flow_state`, находит записи, чей `created_at` старше
    `VCLONE_FSM_TIMEOUT_SEC` секунд, удаляет связанные временные файлы через
    `cleanup_vclone_files` и отправляет пользователю короткое уведомление
    в стиле Bot_Persona_Reply (Requirement 3.7, 6.10, design.md::Error Handling).

    Защищён от любых исключений — не должен ронять process. Bot нужен для
    отправки сообщения о таймауте; стейт всё равно будет вычищен, даже если
    отправка упадёт.
    """
    import time as _time
    from config import vclone_flow_state, vclone_save_flow_state

    logger.info("Watchdog vclone FSM запущен (timeout=%.0fs, interval=%.0fs).",
                VCLONE_FSM_TIMEOUT_SEC, VCLONE_FSM_SCAN_INTERVAL_SEC)

    while True:
        try:
            await asyncio.sleep(VCLONE_FSM_SCAN_INTERVAL_SEC)
            now = _time.time()

            # Снимаем снапшот, чтобы безопасно мутировать оригинал.
            # `vclone_flow_state` — defaultdict[chat_id] -> defaultdict[user_id] -> dict|None.
            expired: list[tuple[int, int, dict]] = []
            for chat_id, users in list(vclone_flow_state.items()):
                if not isinstance(users, dict):
                    continue
                for user_id, state in list(users.items()):
                    if not isinstance(state, dict):
                        continue
                    created_at = state.get("created_at")
                    if not isinstance(created_at, (int, float)):
                        continue
                    if now - created_at >= VCLONE_FSM_TIMEOUT_SEC:
                        expired.append((chat_id, user_id, state))

            expired_save: list[tuple[int, int, dict]] = []
            for chat_id, users in list(vclone_save_flow_state.items()):
                if not isinstance(users, dict):
                    continue
                for user_id, state in list(users.items()):
                    if not isinstance(state, dict):
                        continue
                    created_at = state.get("created_at")
                    if not isinstance(created_at, (int, float)):
                        continue
                    if now - created_at >= VCLONE_FSM_TIMEOUT_SEC:
                        expired_save.append((chat_id, user_id, state))

            for chat_id, user_id, state in expired:
                # 1. Удаляем стейт первым делом — чтобы юзер мог сразу запустить новый /vclone.
                try:
                    vclone_flow_state[chat_id].pop(user_id, None)
                except Exception:
                    logger.exception(
                        "watchdog: не удалось удалить vclone_flow_state[%s][%s]",
                        chat_id, user_id,
                    )

                # 2. Чистим временные файлы (best-effort).
                paths_to_cleanup = []
                ref_path = state.get("reference_path")
                cleaned_path = state.get("cleaned_path")
                if ref_path:
                    paths_to_cleanup.append(Path(ref_path))
                if cleaned_path:
                    paths_to_cleanup.append(Path(cleaned_path))
                if paths_to_cleanup:
                    try:
                        cleanup_vclone_files(*paths_to_cleanup)
                    except Exception:
                        logger.exception(
                            "watchdog: cleanup_vclone_files упал для chat=%s user=%s",
                            chat_id, user_id,
                        )

                # 3. Уведомляем юзера в стиле Bot_Persona_Reply.
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "<i>пожимает плечами и отворачивается</i>\n"
                            "<blockquote>«Слишком долго думал. "
                            "Голос остыл — начнём заново, если решишься.»</blockquote>"
                        ),
                        parse_mode='HTML',
                    )
                except Exception as exc:
                    # Не критично — стейт уже почищен, файлы тоже.
                    logger.warning(
                        "watchdog: не удалось отправить уведомление о таймауте "
                        "chat=%s user=%s: %s",
                        chat_id, user_id, _short_error(exc),
                    )

                logger.info(
                    "watchdog: vclone FSM expired chat=%s user=%s (idle=%.1fs)",
                    chat_id, user_id, now - state.get("created_at", now),
                )

            for chat_id, user_id, state in expired_save:
                try:
                    vclone_save_flow_state[chat_id].pop(user_id, None)
                except Exception:
                    logger.exception(
                        "watchdog: не удалось удалить vclone_save_flow_state[%s][%s]",
                        chat_id, user_id,
                    )

                paths_to_cleanup = []
                for raw_path in state.get("cleanup_paths") or []:
                    if raw_path:
                        paths_to_cleanup.append(Path(raw_path))
                ref_path = state.get("reference_path")
                if ref_path:
                    paths_to_cleanup.append(Path(ref_path))
                if paths_to_cleanup:
                    try:
                        cleanup_vclone_files(*paths_to_cleanup)
                    except Exception:
                        logger.exception(
                            "watchdog: cleanup_vclone_files упал для save-flow chat=%s user=%s",
                            chat_id, user_id,
                        )

                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "<i>закрывает папку с голосами</i>\n"
                            "<blockquote>«Слишком долго. Голос не сохранён.»</blockquote>"
                        ),
                        parse_mode='HTML',
                    )
                except Exception as exc:
                    logger.warning(
                        "watchdog: не удалось отправить уведомление о save-flow timeout "
                        "chat=%s user=%s: %s",
                        chat_id, user_id, _short_error(exc),
                    )

        except asyncio.CancelledError:
            logger.info("Watchdog vclone FSM остановлен.")
            raise
        except Exception:
            # Любая другая ошибка не должна убить watchdog.
            logger.exception("Watchdog vclone FSM: непредвиденная ошибка, продолжаю.")


async def proactive_scheduler_worker(bot):
    """
    Фоновый шедулер проактивных стикеров и сообщений с защитой от гонок и тихими часами.
    Запускается при инициализации бота и выполняется циклически каждые 30 минут.
    """
    logger.info("Проактивный воркер шедулера стикеров запущен!")
    await asyncio.sleep(60) # Спим 1 минуту после старта, чтобы бот прогрелся
    
    while True:
        try:
            logger.info("Шедулер проактивных стикеров: сканирование активных чатов...")

            async with get_db() as conn:
                # Извлекаем все активные сессии, где тишина > 18 часов.
                # Длительность тишины считаем на стороне БД (NOW()), чтобы не смешивать
                # наивный datetime.now() приложения с временем БД (разные TZ -> неверная дельта).
                states = await conn.fetch("""
                    SELECT chat_id, last_activity_time, last_proactive_push_time, user_tz,
                           EXTRACT(EPOCH FROM (NOW() - last_activity_time))::float8 / 3600.0 AS silence_hours
                    FROM chat_emotional_states
                    WHERE conversation_stage = 'active'
                      AND last_activity_time < NOW() - INTERVAL '18 hours'
                      AND (last_proactive_push_time IS NULL OR last_proactive_push_time < NOW() - INTERVAL '18 hours')
                """)
            
            for state in states:
                chat_id = state["chat_id"]
                last_act = state["last_activity_time"]
                last_push = state["last_proactive_push_time"]
                user_tz = state["user_tz"]
                
                # 1. До определения TZ не пушим вообще (тихий дефолт)
                if user_tz is None:
                    logger.debug(f"Шедулер: Пропускаем чат {chat_id}, так как user_tz еще не определен.")
                    continue
                
                # 2. Проверяем жесткие quiet hours по локальному времени пользователя (23:00 - 09:00)
                from datetime import datetime as _dt, timedelta
                local_time = _dt.utcnow() + timedelta(hours=user_tz)
                local_hour = local_time.hour
                local_date = local_time.date()
                
                if local_hour >= 23 or local_hour < 9:
                    logger.info(f"Шедулер: Пропускаем чат {chat_id}, так как локальное время {local_hour:02d}:00 входит в quiet hours (23:00 - 09:00).")
                    continue
                
                # Разность часов (посчитана БД, UTC-консистентно)
                diff_act_hours = max(0.0, state["silence_hours"] or 0.0)
                
                # 3. Чистим вчерашние и прошедшие события в чате
                async with get_db() as conn:
                    await conn.execute("""
                        DELETE FROM user_events 
                        WHERE chat_id = $1 AND event_date < $2::date - INTERVAL '1 day'
                    """, chat_id, local_date)
                
                # 4. Проверяем близость отношений с пользователями в чате
                async with get_db() as conn:
                    profiles = await conn.fetch("""
                        SELECT user_id, mode, profile_json FROM memory_user_profiles
                        WHERE chat_id = $1
                    """, chat_id)
                
                for prof in profiles:
                    user_id = prof["user_id"]
                    mode = prof["mode"]
                    import json
                    prof_json = json.loads(prof["profile_json"]) if isinstance(prof["profile_json"], str) else prof["profile_json"]
                    aff = prof_json.get("affective", {})
                    closeness = aff.get("closeness", 0.0)
                    
                    # Пушим только близких пользователей (closeness > 0.6)
                    if closeness > 0.6:
                        # 5. Проверяем структурированную таблицу user_events на наличие событий (сегодня/завтра)
                        event_today = None
                        event_tomorrow = None
                        
                        async with get_db() as conn:
                            # Проверяем сегодняшнее событие (не уведомленное)
                            event_today = await conn.fetchrow("""
                                SELECT id, event_type, note FROM user_events
                                WHERE chat_id = $1 AND event_date = $2 AND notified = FALSE
                                LIMIT 1
                            """, chat_id, local_date)
                            
                            if not event_today:
                                # Проверяем завтрашнее событие
                                event_tomorrow = await conn.fetchrow("""
                                    SELECT id, event_type, note FROM user_events
                                    WHERE chat_id = $1 AND event_date = $2 AND notified = FALSE
                                    LIMIT 1
                                """, chat_id, local_date + timedelta(days=1))
                        
                        event_id = None
                        prompt = ""
                        
                        if event_today:
                            event_id = event_today["id"]
                            note = event_today["note"]
                            logger.info(f"Шедулер: Найдено событие на сегодня: '{note}' для чата {chat_id}")
                            prompt = (
                                f"Сегодня у собеседника важное событие: {note}. Напиши очень теплое и поддерживающее пожелание удачи "
                                f"(до 15 слов) в характерном для Арти неко-стиле, добавив в самый конец сообщения "
                                f"поддерживающий стикер <sticker>love</sticker> или <sticker>happy</sticker>."
                            )
                        elif event_tomorrow:
                            event_id = event_tomorrow["id"]
                            note = event_tomorrow["note"]
                            logger.info(f"Шедулер: Найдено событие на завтра: '{note}' для чата {chat_id}")
                            prompt = (
                                f"Напомни собеседнику, что завтра у него важное событие: {note}. Напиши короткое и милое напоминание "
                                f"с заботой (до 15 слов), добавив в самый конец сообщения "
                                f"стикер <sticker>love</sticker> или <sticker>happy</sticker>."
                            )
                        else:
                            # Стандартный проактив по тишине
                            prompt = (
                                f"В чате тишина уже {int(diff_act_hours)} часов. Напиши короткую проактивную фразу "
                                f"(до 15 слов) собеседнику, так как ты скучаешь или хочешь возобновить диалог. "
                                f"Ты имеешь доступ к стикерам, поэтому ОБЯЗАТЕЛЬНО добавь в самый конец сообщения "
                                f"тег стикера (например, <sticker>bored</sticker> или <sticker>thinking</sticker>)."
                            )
                        
                        # 6. Атомарный захват слота: обновляем stage и time с жестким guard
                        async with get_db() as conn:
                            captured = await conn.fetchval("""
                                UPDATE chat_emotional_states
                                SET last_proactive_push_time = NOW(),
                                    conversation_stage = 'proactive_sent'
                                WHERE chat_id = $1
                                  AND conversation_stage = 'active'
                                  AND (last_proactive_push_time IS NULL OR last_proactive_push_time < NOW() - INTERVAL '18 hours')
                                RETURNING chat_id;
                            """, chat_id)
                        
                        if not captured:
                            logger.info(f"Шедулер: Не удалось захватить слот проактива для чата {chat_id} (уже захвачен другим процессом). Пропускаем.")
                            break
                        
                        logger.info(f"Шедулер успешно захватил слот и запускает проактивный пуш для чата {chat_id}, юзера {user_id}")
                        
                        # Генерируем ответ Арти
                        from ai.generation import generate_response_stream
                        from utils.chat_history import save_chat_message, save_chat_message_rp
                        from memory.storage import build_memory_context
                        
                        # Подгружаем RAG-контекст памяти для обогащения характера
                        mem_ctx = await build_memory_context(chat_id, user_id, "дата знакомства дедлайн важный день", mode=mode)
                        chat_model = await get_chat_model(chat_id)
                        
                        response_text, _, _, _ = await generate_response_stream(
                            chat_id=chat_id,
                            prompt=prompt,
                            user_name="Арти",
                            chat_context=mem_ctx,
                            model=chat_model,
                            temperature=0.75,
                            user_id=user_id,
                            is_rp_mode=(mode == "rp"),
                        )
                        
                        # Парсим стикер тег
                        sticker_mood = None
                        sticker_match = re.search(r'<sticker>(.*?)</sticker>', response_text, re.IGNORECASE)
                        if sticker_match:
                            sticker_mood = sticker_match.group(1).strip().lower()
                            response_text = re.sub(r'<sticker>.*?</sticker>', '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
                        
                        # Убираем лишние SSML-теги и эмодзи-маркеры
                        display_text = re.sub(r'<[^>]+>', '', response_text)
                        display_text = re.sub(r'\[[^\]]+\]', '', display_text).strip()
                        display_text = fix_html_tags(display_text)
                        
                        if not display_text:
                            # В случае сбоя генерации сбрасываем stage обратно в active
                            async with get_db() as conn:
                                await conn.execute("UPDATE chat_emotional_states SET conversation_stage = 'active', last_proactive_push_time = NULL WHERE chat_id = $1", chat_id)
                            continue
                        
                        # Отправляем сообщение
                        sent_msg = await bot.send_message(
                            chat_id=chat_id,
                            text=display_text,
                            parse_mode='HTML'
                        )
                        
                        # Сохраняем в историю чата
                        if mode == "rp":
                            await save_chat_message_rp(chat_id, "Арти", response_text)
                        else:
                            await save_chat_message(chat_id, "Арти", response_text)
                        
                        # Помечаем событие как уведомленное
                        if event_id is not None:
                            async with get_db() as conn:
                                await conn.execute("UPDATE user_events SET notified = TRUE WHERE id = $1", event_id)
                                logger.info(f"Шедулер: Событие ID {event_id} помечено как notified = TRUE")
                        
                        # Отправляем стикер
                        if sticker_mood:
                            from ai.stickers import send_mood_sticker_task
                            asyncio.create_task(send_mood_sticker_task(bot, chat_id, user_id, sticker_mood, sent_msg.message_id, mode=mode, force=True))
                        
                        break # За раз пушим только одного юзера в чате
                        
        except Exception as e:
            logger.error(f"Ошибка воркера проактивных стикеров: {e}", exc_info=True)
            
        # Спим 30 минут до следующего сканирования
        await asyncio.sleep(1800)
