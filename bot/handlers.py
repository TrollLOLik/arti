"""
Обработчики сообщений: текст, фото, голосовые, документы
"""
import re
import os
import base64
import asyncio
import logging
import random
from pathlib import Path
from datetime import datetime, timedelta

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    AUTO_REPLY_TIMEOUT, AUTO_REPLY_THRESHOLD,
    TTS_ENABLED,
    music_flow_state, waiting_for_image_prompt, pending_image_inputs, waiting_for_video_prompt, pending_video_inputs,
    pending_photo_action, pending_doc_action, rp_mode_state,
    pending_video_url_action, waiting_for_model_search
)
from utils.response_status import is_responses_enabled
from utils.chat_history import (
    save_chat_message, get_chat_context, get_dialog_history_as_text, get_recent_messages,
    save_chat_message_rp, get_chat_context_rp, get_dialog_history_as_text_rp
)
from utils.text_processing import (
    contains_arti, extract_urls_and_make_keyboard, 
    repeat_chat_action, fix_html_tags
)
from utils.document_parser import extract_document_text, extract_text_from_file
from ai.generation import generate_response_stream, is_message_for_arti
from ai.tts import text_to_speech_telegram
from ai.stt import transcribe_audio_groq
from bot.queue import enqueue_reply, enqueue_generation, _extract_photo_urls, _track_task
from memory.storage import build_memory_context, remember_exchange
from bot.commands import (
    handle_music_flow, handle_video_flow, handle_image_flow,
    _enqueue_video_from_flow, _enqueue_image_from_flow,
    handle_dub_flow, handle_dub_attachment, classify_dub_media,
    handle_vclone_flow, handle_vclone_attachment, classify_vclone_media,
    handle_vclone_save_flow, _vclone_caption_fastpath,
    _show_model_menu,
)

logger = logging.getLogger(__name__)

# Per-user rate limiter for LLM text generation (S-04).
# Ограничивает число текстовых сообщений, запускающих LLM, до _TEXT_RATE_LIMIT за _TEXT_RATE_WINDOW секунд.
_TEXT_RATE_LIMIT = 30
_TEXT_RATE_WINDOW = 60  # seconds
_user_text_timestamps: dict[int, list[float]] = {}


def _is_text_rate_limited(user_id: int) -> bool:
    """Возвращает True, если пользователь превысил лимит текстовых LLM-запросов."""
    import time
    now = time.monotonic()
    timestamps = _user_text_timestamps.get(user_id, [])
    # Удаляем старые записи за пределами окна
    timestamps = [t for t in timestamps if now - t < _TEXT_RATE_WINDOW]
    if len(timestamps) >= _TEXT_RATE_LIMIT:
        _user_text_timestamps[user_id] = timestamps
        return True
    timestamps.append(now)
    _user_text_timestamps[user_id] = timestamps
    return False


async def _save_message(chat_id: int, user_name: str, message_text: str, user_id: int = None):
    """Сохраняет сообщение в обычную или RP-историю в зависимости от режима."""
    if rp_mode_state.get(chat_id):
        await save_chat_message_rp(chat_id, user_name, message_text, user_id=user_id)
    else:
        await save_chat_message(chat_id, user_name, message_text, user_id=user_id)


async def _get_dialog_history(chat_id: int) -> str:
    """Возвращает диалоговую историю (обычную или RP)."""
    if rp_mode_state.get(chat_id):
        return await get_dialog_history_as_text_rp(chat_id)
    return await get_dialog_history_as_text(chat_id)


# ============================================================================
# ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ
# ============================================================================

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный обработчик любого текстового сообщения.
    """
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user = update.message.from_user
    if not user:
        return
    user_id = user.id
    user_name = user.first_name or user.username
    message_id = update.message.message_id

    if not await is_responses_enabled(chat_id):
        return

    # Per-user rate limit для LLM-вызовов (S-04: cost abuse protection)
    if _is_text_rate_limited(user_id):
        logger.warning(f"Rate limit: user {user_id} в чате {chat_id} превысил лимит текстовых запросов")
        return

    user_text = update.message.text or ""

    # --- Проверяем, ожидает ли бот поисковый запрос для моделей ---
    if waiting_for_model_search.get(chat_id, {}).get(user_id):
        if user_text.startswith("/"):
            waiting_for_model_search[chat_id].pop(user_id, None)
        else:
            waiting_for_model_search[chat_id][user_id] = False
            if "model_flow" in context.user_data:
                flow = context.user_data["model_flow"]
                flow["query"] = user_text.strip()
                flow["page"] = 0
                flow["menu_mode"] = "list"
                try:
                    await update.message.delete()
                except Exception:
                    pass
                await _show_model_menu(update, context, edit=True)
            return

    # --- Проверяем ожидающие фото без caption ---
    photo_key = (chat_id, user_id)
    if photo_key in pending_photo_action:
        # Если это команда — отменяем ожидание фото, пропускаем дальше
        if user_text.startswith("/"):
            pending = pending_photo_action.pop(photo_key, None)
            if pending and pending.get("bot_message_id"):
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=pending["bot_message_id"], reply_markup=None
                    )
                except Exception:
                    pass
        else:
            # Текстовое сообщение → используем как caption к ожидающим фото
            pending = pending_photo_action.pop(photo_key)
            if pending.get("bot_message_id"):
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=pending["bot_message_id"],
                        text="<i>внимательно рассматривает...</i>",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass
            caption = user_text.strip() or "Что на фото?"
            await _process_images(
                context.bot, chat_id, user_id, user_name, message_id,
                pending["images"], caption,
                pending["replied_to_bot"], pending["is_private"]
            )
            return

    image_flow = context.user_data.get("image_flow", {})
    if image_flow and image_flow.get("chat_id") == chat_id and not image_flow.get("waiting"):
        handled = await handle_image_flow(update, context)
        if handled:
            return

    is_waiting_for_image = (
        waiting_for_image_prompt.get(chat_id, {}).get(user_id, False)
        or (image_flow.get("waiting") and image_flow.get("chat_id") == chat_id)
    )

    # Если ожидается описание изображения
    if is_waiting_for_image:
        prompt = update.message.text or ""
        image_urls = pending_image_inputs[chat_id].get(user_id, []) or image_flow.get("image_urls", [])
        # Подхватываем base64 изображения из фото-кнопок (photo_act:gen_image)
        pending_b64 = context.user_data.pop("pending_base64_for_gen", None)
        if not image_urls and pending_b64:
            image_urls = [f"data:image/jpeg;base64,{b64}" for b64 in pending_b64]
        if not prompt:
            await update.message.reply_text("✍️ Жду текстовый промпт для изображения или /cancel.", parse_mode='HTML')
            return
        logger.info(f"Получено описание изображения от пользователя {user_id} в чате {chat_id} (len={len(prompt)}, images={len(image_urls)})")  # PRIV-01: без полного текста
        await _enqueue_image_from_flow(update, context, prompt, image_urls)
        return

    video_flow = context.user_data.get("video_flow", {})
    if video_flow and video_flow.get("chat_id") == chat_id and not video_flow.get("waiting"):
        handled = await handle_video_flow(update, context)
        if handled:
            return

    is_waiting_for_video = (
        waiting_for_video_prompt.get(chat_id, {}).get(user_id, False)
        or (video_flow.get("waiting") and video_flow.get("chat_id") == chat_id)
    )

    if is_waiting_for_video:
        prompt = update.message.text or ""
        image_urls = pending_video_inputs[chat_id].get(user_id, []) or video_flow.get("image_urls", [])
        # Подхватываем base64 изображения из фото-кнопок (photo_act:gen_video)
        pending_b64 = context.user_data.pop("pending_base64_for_gen", None)
        if not image_urls and pending_b64:
            image_urls = [f"data:image/jpeg;base64,{b64}" for b64 in pending_b64]
        if not prompt:
            await update.message.reply_text("✍️ Жду текстовый промпт для видео или /cancel.", parse_mode='HTML')
            return
        logger.info(f"Получено описание видео от пользователя {user_id} в чате {chat_id} (len={len(prompt)}, images={len(image_urls)})")  # PRIV-01: без полного текста

        await _enqueue_video_from_flow(update, context, prompt, image_urls)
        return

    # Если сообщение содержит фото – вызываем отдельный обработчик
    if update.message.photo:
        await handle_image_message(update, context)
        return

    # Если пользователь в процессе диалога /music
    if music_flow_state.get(chat_id, {}).get(user_id):
        user_text = update.message.text or ""
        handled = await handle_music_flow(update, context, chat_id, user_id, user_text)
        if handled:
            return

    # Если пользователь в процессе диалога /dub
    from config import dub_flow_state
    if dub_flow_state.get(chat_id, {}).get(user_id):
        handled = await handle_dub_flow(update, context)
        if handled:
            return

    # Если пользователь вводит имя сохраняемого голоса
    from config import vclone_save_flow_state
    if vclone_save_flow_state.get(chat_id, {}).get(user_id):
        handled = await handle_vclone_save_flow(update, context)
        if handled:
            return

    # Если пользователь в процессе диалога /vclone
    from config import vclone_flow_state
    if vclone_flow_state.get(chat_id, {}).get(user_id):
        handled = await handle_vclone_flow(update, context)
        if handled:
            return

    # Обычное текстовое сообщение
    user_message = update.message.text or ""

    # Ограничение длины пользовательского ввода (S-08: защита от cost abuse)
    _MAX_USER_MESSAGE_LEN = 8000
    if len(user_message) > _MAX_USER_MESSAGE_LEN:
        user_message = user_message[:_MAX_USER_MESSAGE_LEN] + "…[обрезано]"

    # --- БЛОК ЛОГИКИ REPLY ---
    base64_image_reply = None
    document_text_reply = None
    telegram_video_file_id = None
    
    if update.message.reply_to_message:
        original = update.message.reply_to_message
        logger.info(f"Обработка reply на сообщение типа: {original.effective_attachment}")
        
        orig_text = original.text or original.caption
        if orig_text:
            user_message = (
                f"Исходный текст сообщения:\n«{orig_text}»\n\n"
                f"Запрос пользователя к этому тексту: {user_message}"
            )
            
        if original.photo or (original.sticker and not original.sticker.is_animated and not original.sticker.is_video):
            file_id = original.photo[-1].file_id if original.photo else original.sticker.file_id
            try:
                file = await context.bot.get_file(file_id)
                file_bytes = await file.download_as_bytearray()
                base64_image_reply = base64.b64encode(file_bytes).decode("utf-8")
                if not user_message:
                    user_message = "Проанализируй это изображение."
            except Exception as e:
                logger.error(f"Ошибка при загрузке медиа из reply: {e}")

        elif original.document:
            document_text_reply = await extract_document_text(context, original.document)
            if document_text_reply:
                if not user_message:
                    user_message = "Проанализируй этот документ."
                    
        elif original.video:
            telegram_video_file_id = original.video.file_id
            if not user_message:
                user_message = "Проанализируй это видео."

    await _save_message(chat_id, user_name, user_message, user_id=user_id)

    # === Перехват одиночного URL на видео: предлагаем инлайн-меню ===
    # Условия: ЛС, сообщение — только URL, известный видеохост, нет reply/forward/упоминания.
    # VAL-01: реагируем на reply/forward именно от САМОЙ Арти, а не от любого бота.
    bot_id = context.bot.id
    forwarded_from_bot = bool(
        getattr(update.message, "forward_from", None)
        and update.message.forward_from.id == bot_id
    )
    replied_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == bot_id
    )
    # VAL-01: упоминание по границе слова, иначе «квАРТИра»/«пАРТИя» ложно триггерят.
    has_arti_mention = bool(re.search(r'\bарти\b', user_message.lower()))

    is_private_chat = update.effective_chat.type == "private"

    from ai.video_url import is_message_only_url, find_first_url, is_known_video_url
    raw_text = update.message.text or ""
    if (
        is_private_chat
        and is_message_only_url(raw_text)
        and not has_arti_mention
        and not forwarded_from_bot
        and not replied_to_bot
        and not base64_image_reply
        and not document_text_reply
        and not telegram_video_file_id
    ):
        candidate_url = find_first_url(raw_text)
        if candidate_url and is_known_video_url(candidate_url):
            await _offer_video_url_actions(context.bot, chat_id, user_id, message_id, candidate_url)
            return

    # В личных сообщениях — отвечаем на всё
    is_private = is_private_chat
    if is_private:
        await enqueue_reply(chat_id, user_id, user_name, user_message, message_id, context, is_voice=True, base64_image=base64_image_reply, document_text=document_text_reply, video_file_id=telegram_video_file_id)
        return

    # Определяем триггеры (только для групп)
    # forwarded_from_bot/replied_to_bot/has_arti_mention уже посчитаны выше для URL-перехвата

    # Явный триггер: упоминание, реплай или пересылка от бота
    if has_arti_mention or forwarded_from_bot or replied_to_bot:
        await enqueue_reply(chat_id, user_id, user_name, user_message, message_id, context, is_voice=True, base64_image=base64_image_reply, document_text=document_text_reply, video_file_id=telegram_video_file_id)
        return

    # Автоответ: если за AUTO_REPLY_TIMEOUT пришло много сообщений
    recent_messages = await get_recent_messages(chat_id, AUTO_REPLY_TIMEOUT)
    non_bot_messages = [msg for msg in recent_messages if not msg[1].startswith("Арти:")]

    if len(non_bot_messages) >= AUTO_REPLY_THRESHOLD:
        last_message_text = non_bot_messages[-1][1]
        await enqueue_reply(chat_id, 0, "Автоответ", last_message_text, message_id, context, is_voice=True, base64_image=base64_image_reply, document_text=document_text_reply, video_file_id=telegram_video_file_id)
        return

    # LLM-фильтр: если Арти недавно отвечала в этом чате, проверяем — адресовано ли сообщение ей
    recent_context = await get_chat_context(chat_id)
    # Проверяем, есть ли недавние ответы Арти в контексте
    if recent_context and "Арти:" in recent_context:
        # Умная проверка: насколько недавно был ответ Арти?
        lines = [line.strip() for line in recent_context.split("\n") if line.strip()]
        arti_index = -1
        last_arti_line = ""
        for idx, line in enumerate(reversed(lines)):
            if "] Арти:" in line:
                arti_index = idx
                last_arti_line = line
                break
        
        # Если Арти отвечала в пределах последних 3 сообщений
        if 0 <= arti_index <= 2:
            is_recent_time = False
            try:
                # Извлекаем метку времени из строки формата "[YYYY-MM-DD HH:MM:SS] Арти: ..."
                dt_str = last_arti_line[1:20]
                arti_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                time_diff = datetime.now() - arti_time
                if time_diff.total_seconds() <= 180:  # в течение последних 3 минут
                    is_recent_time = True
            except Exception as e:
                logger.warning(f"Ошибка при парсинге времени последнего ответа Арти: {e}")
                # На всякий случай разрешаем, если парсинг сломался
                is_recent_time = True

            if is_recent_time:
                if await is_message_for_arti(user_message, recent_context, user_name):
                    await enqueue_reply(chat_id, user_id, user_name, user_message, message_id, context, is_voice=True, base64_image=base64_image_reply, document_text=document_text_reply, video_file_id=telegram_video_file_id)


# ============================================================================
# ОБРАБОТЧИК ФОТО
# ============================================================================

import time

_media_group_cache = {}
_processed_media_groups = {}


def _photo_action_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-кнопки быстрых действий с фото."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Что на фото?", callback_data="photo_act:analyze"),
            InlineKeyboardButton("📝 Описать детально", callback_data="photo_act:describe"),
        ],
        [
            InlineKeyboardButton("🎨 Сгенерировать картинку", callback_data="photo_act:gen_image"),
            InlineKeyboardButton("🎬 Сгенерировать видео", callback_data="photo_act:gen_video"),
        ],
        [
            InlineKeyboardButton("❌ Отмена", callback_data="photo_act:cancel"),
        ],
    ])


async def _send_photo_action_prompt(bot, chat_id, user_id, message_id, base64_images, replied_to_bot, is_private):
    """Отправляет сообщение с инлайн-кнопками, сохраняет фото в pending."""
    count = len(base64_images)
    img_word = "картинку" if count == 1 else f"{count} картинок" if count < 5 else f"{count} картинок"

    sent = await bot.send_message(
        chat_id=chat_id,
        text=(
            f"<i>ловит {img_word}, раскладывает на столе</i>\n\n"
            f"<blockquote>«Получила. И что мне с этим делать? "
            f"Выбери действие или напиши свой запрос текстом.»</blockquote>"
        ),
        reply_to_message_id=message_id,
        reply_markup=_photo_action_keyboard(),
        parse_mode='HTML'
    )

    pending_photo_action[(chat_id, user_id)] = {
        "images": base64_images,
        "message_id": message_id,
        "bot_message_id": sent.message_id,
        "replied_to_bot": replied_to_bot,
        "is_private": is_private,
        "user_name": None,
    }


async def _process_images(
    bot, chat_id, user_id, user_name, message_id,
    base64_images, user_caption, replied_to_bot, is_private
):
    """Фоновая задача: отправляет все собранные картинки в ИИ и шлёт ответ."""
    try:
        # VAL-01: упоминание по границе слова, а не подстрокой.
        if not (is_private or bool(re.search(r'\bарти\b', user_caption.lower())) or replied_to_bot):
            logger.info("Триггер для ответа на фото не сработал — пропускаем")
            return

        # Если caption пустой — спрашиваем что делать (ЛС или reply на бота)
        if not user_caption.strip():
            await _send_photo_action_prompt(bot, chat_id, user_id, message_id, base64_images, replied_to_bot, is_private)
            pending_photo_action[(chat_id, user_id)]["user_name"] = user_name
            return

        # Обновление эмоционального состояния чата
        from database.models import ChatEmotionalState, MemoryUserProfile
        import json
        
        closeness = 0.1
        mode = "rp" if rp_mode_state.get(chat_id) else "default"
        user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
        if user_profile and user_profile.get("profile_json"):
            prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
            closeness = prof_json.get("affective", {}).get("closeness", 0.1)
            
        # Защита от инъекций: вырезаем тег интроспекции из ВВОДА юзера (парсим только из ответа Арти).
        from database.models import strip_introspection_tags
        user_caption = strip_introspection_tags(user_caption)
        # defer_sentiment=True: словарный сдвиг отложен до apply_turn_sentiment пост-генерации.
        img_state = await ChatEmotionalState.update_state(chat_id, user_caption, closeness, user_id=user_id, defer_sentiment=True)

        image_details = "изображение" if len(base64_images) == 1 else f"{len(base64_images)} изображений"
        await _save_message(chat_id, user_name, f"Пользователь прислал {image_details}. Подпись: {user_caption}", user_id=user_id)

        dialog_history_str = await _get_dialog_history(chat_id)
        memory_context = await build_memory_context(
            chat_id=chat_id,
            user_id=user_id,
            user_message=user_caption,
            mode="rp" if rp_mode_state.get(chat_id) else "default",
        )
        if memory_context:
            dialog_history_str = f"{dialog_history_str}\n\n{memory_context}" if dialog_history_str else memory_context
        await bot.send_chat_action(chat_id=chat_id, action="typing")

        response_text, used_search, grounding_links, found_search_images = await generate_response_stream(
            chat_id=chat_id,
            prompt=user_caption,
            user_name=user_name,
            chat_context=dialog_history_str,
            base64_images=base64_images,
            user_id=user_id,
            is_rp_mode=rp_mode_state.get(chat_id, False),
            enable_introspection=True,
            emotional_state=img_state,
        )
        logger.debug(f"RAW ИИ ОТВЕТ (фото, {len(base64_images)} шт): {response_text}")  # PRIV-01

        # MEM-06: ответ-заглушку об ошибке покажем, но не сохраняем в историю/память.
        from ai.generation import is_error_response
        generation_failed = is_error_response(response_text)

        # === ЦЕПОЧКА ОЧИСТКИ ТЕКСТА ===
        response_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

        # Извлекаем mood-стикера из ответа
        sticker_mood = None
        sticker_match = re.search(r'<sticker>(.*?)</sticker>', response_text, re.IGNORECASE)
        if sticker_match:
            sticker_mood = sticker_match.group(1).strip().lower()

        # Гибридный сентимент: интроспекция LLM > словарный фолбэк; затем вырезаем служебный тег.
        introspection_sticker = await ChatEmotionalState.apply_turn_sentiment(
            chat_id, response_text, img_state.get("keyword_mood_delta")
        )
        if not sticker_mood and introspection_sticker:
            sticker_mood = introspection_sticker
        response_text = strip_introspection_tags(response_text)

        # Очищаем все теги стикеров из ответа
        from ai.stickers import _clean_deformed_tags
        response_text = _clean_deformed_tags(response_text)

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
            music_request = {
                'instrumental': music_match.group(1).strip().lower() == 'true',
                'style': music_match.group(2).strip(),
                'prompt': music_match.group(3).strip() if music_match.group(3) else ""
            }
            response_text = music_tag_regex.sub('', response_text).strip()

        display_text = re.sub(
            r'(&lt;|<)\s*/?(?:break|speak|prosody)\b.*?(&gt;|>)',
            '', response_text, flags=re.IGNORECASE | re.DOTALL
        ).strip()

        display_text = re.sub(r'\[[^\]]+\]', '', display_text).strip()
        display_text, reply_markup = extract_urls_and_make_keyboard(display_text, extra_links=grounding_links)
        display_text = fix_html_tags(display_text)

        # Если ответ состоял только из стикера, сохраняем в историю понятное текстовое представление
        history_response_text = response_text
        if not response_text.strip() and sticker_mood:
            history_response_text = f"[Стикер: {sticker_mood}]"

        # MEM-06: заглушку об ошибке не пишем в историю.
        if not generation_failed:
            await _save_message(chat_id, "Арти", history_response_text)

        # === ОТПРАВКА СООБЩЕНИЯ (ТЕКСТ/ГОЛОС) ===
        sent_msg = None
        has_text = bool(display_text.strip())

        if has_text:
            if used_search or not TTS_ENABLED:
                sent_msg = await bot.send_message(
                    chat_id=chat_id, text=display_text,
                    reply_to_message_id=message_id,
                    reply_markup=reply_markup, parse_mode='HTML'
                )
            else:
                record_action_task = asyncio.create_task(
                    repeat_chat_action(bot, chat_id, 'record_audio', interval=4)
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
                            sent_msg = await bot.send_voice(
                                chat_id=chat_id, voice=voice_input,
                                caption=safe_caption,
                                reply_to_message_id=message_id,
                                reply_markup=reply_markup,
                                parse_mode='HTML' if len(display_text) <= 1000 else None
                            )
                    else:
                        record_action_task.cancel()
                        sent_msg = await bot.send_message(
                            chat_id=chat_id, text=display_text,
                            reply_markup=reply_markup,
                            reply_to_message_id=message_id,
                            parse_mode='HTML'
                        )
                except Exception as e:
                    record_action_task.cancel()
                    logger.exception("Ошибка при генерации голоса для фото:")
                    sent_msg = await bot.send_message(
                        chat_id=chat_id, text=display_text,
                        reply_markup=reply_markup,
                        reply_to_message_id=message_id,
                        parse_mode='HTML'
                    )

        # === ОТПРАВКА СТИКЕРА В ФОНЕ ===
        if sticker_mood:
            from ai.stickers import send_mood_sticker_task
            sticker_reply_to_message_id = sent_msg.message_id if sent_msg else message_id
            
            asyncio.create_task(
                send_mood_sticker_task(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    mood=sticker_mood,
                    message_id=sticker_reply_to_message_id,
                    mode=mode,
                    user_message_id=message_id
                )
            )

        # MEM-06: не учим память на заглушке об ошибке.
        if not generation_failed:
            memory_task = asyncio.create_task(
                remember_exchange(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_name=user_name,
                    user_message=user_caption,
                    response_text=history_response_text,
                    mode="rp" if rp_mode_state.get(chat_id) else "default",
                    metadata={"message_id": message_id, "used_search": used_search, "source": "image"},
                )
            )
            _track_task(memory_task)

        # === ОТПРАВЛЯЕМ КАРТИНКИ ИЗ ПОИСКА ===
        if found_search_images:
            for img_url in found_search_images:
                try:
                    await bot.send_photo(
                        chat_id=chat_id, photo=img_url,
                        caption="<i>[Вот что я нашла в интернете по теме:]</i>",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить картинку из поиска {img_url}: {e}")

        # VAL-07: LLM-инициированная генерация считается в per-user медиа-квоту.
        media_allowed = True
        if image_requests or video_request_prompt or music_request:
            from bot.queue import _llm_media_quota_ok
            media_allowed = _llm_media_quota_ok(user_id)
            if not media_allowed:
                logger.info(f"LLM-медиа-теги (фото) пропущены: квота медиа исчерпана для user {user_id}")

        if image_requests and media_allowed:
            source_image_urls = [f"data:image/jpeg;base64,{base64_images[0]}"] if base64_images else []
            logger.info(f"Ставлю в очередь {len(image_requests)} image-запросов из ответа на фото, reference_images={len(source_image_urls)}")
            for prompt in image_requests[:5]:
                task = {
                    'type': 'image', 'chat_id': chat_id, 'prompt': prompt,
                    'context': None, 'message_id': message_id,
                    'image_urls': source_image_urls, 'user_name': user_name, 'bot': bot
                }
                await enqueue_generation(task, bot, chat_id)

        if video_request_prompt and media_allowed:
            source_image_urls = [f"data:image/jpeg;base64,{base64_images[0]}"] if base64_images else []
            logger.info(f"Ставлю в очередь video-запрос из ответа на фото, reference_images={len(source_image_urls)}")
            task = {
                'type': 'video', 'chat_id': chat_id, 'prompt': video_request_prompt,
                'context': None, 'message_id': message_id,
                'image_urls': source_image_urls, 'user_name': user_name, 'bot': bot,
                'video_model': 'openai/sora-2', 'video_duration': '8', 'video_aspect_ratio': '16:9'
            }
            await enqueue_generation(task, bot, chat_id)

        if music_request and media_allowed:
            logger.info("Ставлю в очередь music-запрос из ответа на фото")
            task = {
                'type': 'music', 'chat_id': chat_id, 'prompt': music_request['prompt'],
                'style': music_request['style'], 'instrumental': music_request['instrumental'],
                'context': None, 'message_id': message_id,
                'user_name': user_name, 'bot': bot
            }
            await enqueue_generation(task, bot, chat_id)

        logger.info("Ответ Арти отправлен в чат")

    except Exception as e:
        logger.exception("Ошибка при обработке ответа на фото:")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Ошибка... Но я тут! 😉",
                reply_to_message_id=message_id
            )
        except Exception:
            pass


async def _collect_and_process_media_group(
    bot, media_group_id, chat_id, user_id, user_name, message_id,
    replied_to_bot, is_private
):
    """Ждёт окно сбора, собирает все картинки из альбома, запускает обработку."""
    # L-06: окно 4с (было 2.5с) — большие альбомы/медленная сеть успевают доехать,
    # иначе поздние части группы терялись.
    await asyncio.sleep(4.0)

    group_data = _media_group_cache.pop(media_group_id, None)
    if not group_data:
        return

    _processed_media_groups[media_group_id] = time.time()

    base64_images = group_data['images']
    user_caption = group_data['caption']
    message_id = group_data['message_id']

    logger.info(f"Медиа-группа {media_group_id} собрана: {len(base64_images)} картинок")

    await _process_images(
        bot, chat_id, user_id, user_name, message_id,
        base64_images, user_caption, replied_to_bot, is_private
    )


# --- Промпты для быстрых действий с фото ---
_PHOTO_ACTION_PROMPTS = {
    "analyze": "Что изображено на этих фотографиях? Кратко опиши основное.",
    "describe": "Опиши максимально детально всё, что видишь на изображениях: объекты, людей, цвета, настроение, композицию.",
    "gen_image": None,
    "gen_video": None,
}


async def photo_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик инлайн-кнопок для фото без caption."""
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("photo_act:"):
        return

    action = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name or query.from_user.username or "Пользователь"

    key = (chat_id, user_id)
    # L-07: если для этого пользователя нет ожидания — это либо чужая кнопка, либо
    # ожидание уже обработано. Тихо отвечаем и НЕ редактируем сообщение владельца.
    if key not in pending_photo_action:
        await query.answer("Эта кнопка не для тебя или уже неактуальна.", show_alert=False)
        return
    pending = pending_photo_action.pop(key, None)

    if not pending:
        await query.answer("Уже неактуально.", show_alert=False)
        return

    base64_images = pending["images"]
    message_id = pending["message_id"]
    replied_to_bot = pending["replied_to_bot"]
    is_private = pending["is_private"]

    # --- Отмена ---
    if action == "cancel":
        await query.edit_message_text(
            "<i>аккуратно складывает картинки обратно</i>\n\n"
            "<blockquote>«Ладно, забыли. Если что — присылай снова.»</blockquote>",
            parse_mode='HTML'
        )
        return

    # --- Генерация картинки по фото ---
    if action == "gen_image":
        await query.edit_message_text(
            "<i>настраивает генератор изображений</i>\n\n"
            "<blockquote>«Принято! Теперь напиши, что именно сгенерировать на основе этих фото.»</blockquote>",
            parse_mode='HTML'
        )
        # Конвертируем base64 в URL-подобный формат для image flow
        from bot.commands import _start_image_settings_flow
        # Сохраняем base64 в pending для дальнейшего использования
        waiting_for_image_prompt[chat_id][user_id] = True
        pending_image_inputs[chat_id][user_id] = []
        # Сохраняем base64 images в user_data для позднего использования
        context.user_data["pending_base64_for_gen"] = base64_images
        return

    # --- Генерация видео по фото ---
    if action == "gen_video":
        await query.edit_message_text(
            "<i>включает режим режиссёра</i>\n\n"
            "<blockquote>«Отлично, будет кино! Напиши, какое видео сделать на основе этих фото.»</blockquote>",
            parse_mode='HTML'
        )
        waiting_for_video_prompt[chat_id][user_id] = True
        pending_video_inputs[chat_id][user_id] = []
        context.user_data["pending_base64_for_gen"] = base64_images
        return

    # --- Анализ / Описание (LLM) ---
    prompt = _PHOTO_ACTION_PROMPTS.get(action, "Что на фото?")
    await query.edit_message_text(
        "<i>внимательно рассматривает...</i>",
        parse_mode='HTML'
    )

    await _process_images(
        context.bot, chat_id, user_id, user_name, message_id,
        base64_images, prompt, replied_to_bot, is_private
    )


async def handle_image_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    user_name = user.first_name or user.username
    message_id = update.message.message_id
    media_group_id = update.message.media_group_id

    if not await is_responses_enabled(chat_id):
        return

    video_flow = context.user_data.get("video_flow", {})
    is_waiting_for_video = (
        waiting_for_video_prompt.get(chat_id, {}).get(user_id, False)
        or (video_flow.get("waiting") and video_flow.get("chat_id") == chat_id)
        or (video_flow.get("chat_id") == chat_id and video_flow.get("step"))
    )

    if is_waiting_for_video:
        user_caption = update.message.caption or ""
        image_urls = pending_video_inputs[chat_id].get(user_id, []) or video_flow.get("image_urls", [])
        new_image_urls = await _extract_photo_urls(update, context)

        if user_caption:
            await _enqueue_video_from_flow(update, context, user_caption, new_image_urls or image_urls)
            return

        if image_urls and video_flow.get("step") == "prompt":
            await update.message.reply_text("✍️ Я уже получила картинку и теперь ожидаю текстовый промпт для видео или /cancel.", parse_mode='HTML')
            return

        if new_image_urls:
            pending_video_inputs[chat_id][user_id] = new_image_urls
            video_flow["image_urls"] = new_image_urls
            context.user_data["video_flow"] = video_flow
            if video_flow.get("step") == "prompt":
                await update.message.reply_text("🖼 Картинку получила. Теперь напиши промпт для видео следующим сообщением.\n\n"
                                                "💡 <i>Для отмены введи /cancel</i>", parse_mode='HTML')
            else:
                await update.message.reply_text("🖼 Картинку получила. Продолжи выбор параметров видео кнопками ниже или введи /cancel.", parse_mode='HTML')
            return

        await update.message.reply_text("❌ Не удалось получить картинку. Отправь её ещё раз или /cancel.")
        return

    image_flow = context.user_data.get("image_flow", {})
    is_waiting_for_image = (
        waiting_for_image_prompt.get(chat_id, {}).get(user_id, False)
        or (image_flow.get("waiting") and image_flow.get("chat_id") == chat_id)
    )

    if is_waiting_for_image:
        user_caption = update.message.caption or ""
        image_urls = pending_image_inputs[chat_id].get(user_id, []) or image_flow.get("image_urls", [])
        new_image_urls = await _extract_photo_urls(update, context)

        if user_caption:
            await _enqueue_image_from_flow(update, context, user_caption, new_image_urls or image_urls)
            return

        if image_urls:
            await update.message.reply_text("✍️ Я уже получила картинку и теперь ожидаю текстовый промпт или /cancel.", parse_mode='HTML')
            return

        if new_image_urls:
            pending_image_inputs[chat_id][user_id] = new_image_urls
            image_flow["image_urls"] = new_image_urls
            context.user_data["image_flow"] = image_flow
            await update.message.reply_text("📷 Картинку получила. Теперь напиши промпт следующим сообщением.\n\n"
                                            "💡 <i>Для отмены введи /cancel</i>", parse_mode='HTML')
            return

        await update.message.reply_text("❌ Не удалось получить картинку. Отправь её ещё раз или /cancel.")
        return

    # --- Скачиваем и кодируем фото ---
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    try:
        base64_image = base64.b64encode(file_bytes).decode("utf-8")
        logger.info("Изображение успешно закодировано в base64")
    except Exception as e:
        logger.error(f"Ошибка при base64-кодировании изображения: {e}")
        return

    replied_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id  # VAL-01
    )
    is_private = update.effective_chat.type == "private"

    # --- Медиа-группа (альбом) ---
    if media_group_id:
        # Чистим старые записи
        current_time = time.time()
        for k in [k for k, v in _processed_media_groups.items() if current_time - v > 60]:
            del _processed_media_groups[k]

        if media_group_id in _processed_media_groups:
            return  # Группа уже полностью обработана

        is_leader = media_group_id not in _media_group_cache

        if is_leader:
            _media_group_cache[media_group_id] = {
                'images': [base64_image],
                'caption': update.message.caption or "",
                'message_id': message_id
            }
            # Запускаем фоновую задачу и СРАЗУ возвращаемся,
            # чтобы фреймворк обработал следующие фото из альбома
            asyncio.create_task(
                _collect_and_process_media_group(
                    context.bot, media_group_id, chat_id, user_id, user_name, message_id,
                    replied_to_bot, is_private
                )
            )
        else:
            # Фолловер: просто добавляем картинку в кеш
            _media_group_cache[media_group_id]['images'].append(base64_image)
            if update.message.caption:
                _media_group_cache[media_group_id]['caption'] = update.message.caption

        return  # И лидер, и фолловер возвращаются сразу

    # --- Одиночная картинка ---
    user_caption = update.message.caption or ""
    await _process_images(
        context.bot, chat_id, user_id, user_name, message_id,
        [base64_image], user_caption, replied_to_bot, is_private
    )


# ============================================================================
# ОБРАБОТЧИК ГОЛОСОВЫХ СООБЩЕНИЙ
# ============================================================================

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    message_id = update.message.message_id

    # AUTH-01: в выключенном (/stop) чате не делаем ничего — иначе бот скачивал бы
    # и транскрибировал чужие голосовые (затраты STT, передача аудио вовне) и отвечал
    # в обход выключателя. Ставим проверку первой, как в остальных медиа-хендлерах.
    if not await is_responses_enabled(chat_id):
        return

    # Перехват /vclone или /steal в caption голосового сообщения
    caption_handled = await _vclone_caption_fastpath(update, context)
    if caption_handled:
        return

    # Перехват для /vclone flow: голосовое как источник референса
    from config import vclone_flow_state
    state_vclone = vclone_flow_state.get(chat_id, {}).get(user_id)
    if state_vclone and state_vclone.get("step") == "reference":
        voice = update.message.voice
        if voice:
            handled = await handle_vclone_attachment(
                update, context,
                file_id=voice.file_id,
                file_size=voice.file_size,
                file_name=None,
                source_kind="stepwise_voice",
            )
            if handled:
                return

    # AUTH-01 / S-04: лимит дорогих LLM/STT-операций (как для текстового пути).
    # Ставим после vclone-fast-path'ов, чтобы загрузка референса не съедала бюджет.
    if _is_text_rate_limited(user_id):
        logger.warning(f"Rate limit: голосовое от user {user_id} в чате {chat_id} превысило лимит")
        return

    temp_audio_path = None
    try:
        # Ретраим скачивание при flaky-сети
        from telegram.error import TimedOut, NetworkError
        last_exc = None
        for attempt in range(3):
            try:
                file = await context.bot.get_file(update.message.voice.file_id)
                file_bytes = await file.download_as_bytearray()
                last_exc = None
                break
            except (TimedOut, NetworkError) as exc:
                last_exc = exc
                if attempt < 2:
                    delay = 2.0 * (attempt + 1)
                    logger.warning(
                        "Скачивание голосового: %s, повтор через %.1fс (%d/3)",
                        exc, delay, attempt + 1,
                    )
                    await asyncio.sleep(delay)
        if last_exc:
            raise last_exc

        temp_dir = Path("temp")
        temp_dir.mkdir(exist_ok=True)
        
        temp_audio_path = temp_dir / f"voice_{chat_id}_{user_id}_{message_id}.ogg"
        
        with open(temp_audio_path, 'wb') as f:
            f.write(file_bytes)
        
        if not temp_audio_path.exists():
            logger.error(f"Файл {temp_audio_path} не был создан")
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Ошибка при сохранении голосового сообщения. Попробуйте снова.",
                reply_to_message_id=message_id
            )
            return
        
        file_size = temp_audio_path.stat().st_size
        if file_size == 0:
            logger.error(f"Файл {temp_audio_path} пустой")
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Ошибка при сохранении голосового сообщения. Попробуйте снова.",
                reply_to_message_id=message_id
            )
            return
        
        logger.info(f"Аудио файл сохранен: {temp_audio_path.resolve()} (размер: {file_size} байт)")

        transcription = await transcribe_audio_groq(str(temp_audio_path.resolve()))

        if not transcription or not transcription.strip():
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Не удалось распознать голосовое сообщение. Попробуйте снова.",
                reply_to_message_id=message_id
            )
            return

        await _save_message(chat_id, update.message.from_user.first_name, transcription, user_id=user_id)

        is_private = update.effective_chat.type == "private"
        replied_to_bot = bool(
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id  # VAL-01
        )
        if is_private or replied_to_bot or contains_arti(transcription):
            await enqueue_reply(
                chat_id=chat_id, user_id=user_id,
                user_name=update.message.from_user.first_name,
                user_message=transcription,
                message_id=message_id, context=context, is_voice=True
            )
        else:
            logger.info(f"Голосовое сообщение от {user_id} не требует ответа (нет упоминания 'арти').")

    except FileNotFoundError as e:
        logger.error(f"Файл не найден при обработке голосового сообщения: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Ошибка при обработке голосового сообщения. Попробуйте снова.",
            reply_to_message_id=message_id
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке голосового сообщения: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Произошла ошибка при обработке голосового сообщения. Попробуйте позже.",
                reply_to_message_id=message_id
            )
        except Exception:
            pass

    finally:
        if temp_audio_path and temp_audio_path.exists():
            try:
                temp_audio_path.unlink()
            except Exception as e:
                logger.warning(f"Не удалось удалить временный файл {temp_audio_path}: {e}")


# ============================================================================
# ОБРАБОТЧИК ЗАГРУЖЕННОГО ВИДЕО
# ============================================================================

async def handle_video_upload_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    user_name = user.first_name or user.username
    message_id = update.message.message_id
    
    if not await is_responses_enabled(chat_id):
        return

    # Перехват /vclone или /steal в caption видео
    caption_handled = await _vclone_caption_fastpath(update, context)
    if caption_handled:
        return

    # Перехват для /vclone flow: видео как источник референса
    from config import vclone_flow_state
    state_vclone = vclone_flow_state.get(chat_id, {}).get(user_id)
    if state_vclone and state_vclone.get("step") == "reference":
        video = update.message.video
        if video:
            handled = await handle_vclone_attachment(
                update, context,
                file_id=video.file_id,
                file_size=video.file_size,
                file_name=getattr(video, "file_name", None) or f"video_{message_id}.mp4",
                source_kind="stepwise_video",
            )
            if handled:
                return

    # Перехват для /dub flow: видео как источник дубляжа
    from config import dub_flow_state
    if dub_flow_state.get(chat_id, {}).get(user_id):
        video = update.message.video
        if video:
            handled = await handle_dub_attachment(
                update, context,
                file_id=video.file_id,
                media_kind="video",
                file_name=getattr(video, "file_name", None) or f"video_{message_id}.mp4",
            )
            if handled:
                return

    video_file_id = update.message.video.file_id
    user_caption = update.message.caption or "Проанализируй это видео."
    
    replied_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id  # VAL-01
    )
    is_private = update.effective_chat.type == "private"

    if is_private or bool(re.search(r'\bарти\b', user_caption.lower())) or replied_to_bot:
        await _save_message(chat_id, user_name, user_caption, user_id=user_id)
        await enqueue_reply(chat_id, user_id, user_name, user_caption, message_id, context, is_voice=True, video_file_id=video_file_id)
    else:
        logger.info("Видео сообщение не требует ответа")


# ============================================================================
# ОБРАБОТЧИК ВИДЕОЗАМЕТОК (КРУГЛЕШОЧКОВ)
# ============================================================================

async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик видеосообщений (круглешочков)."""
    chat_id = update.effective_chat.id
    user = update.message.from_user
    if not user:
        return
    user_id = user.id
    user_name = user.first_name or user.username
    message_id = update.message.message_id
    
    if not await is_responses_enabled(chat_id):
        return

    # Перехват для /vclone flow: видеосообщение как источник референса
    from config import vclone_flow_state
    state_vclone = vclone_flow_state.get(chat_id, {}).get(user_id)
    if state_vclone and state_vclone.get("step") == "reference":
        vn = update.message.video_note
        if vn:
            handled = await handle_vclone_attachment(
                update, context,
                file_id=vn.file_id,
                file_size=vn.file_size,
                file_name=f"video_note_{message_id}.mp4",
                source_kind="stepwise_video_note",
            )
            if handled:
                return

    video_note_id = update.message.video_note.file_id
    # У круглешков нет подписей, поэтому используем дефолтный промпт
    user_prompt = "Проанализируй этот круглешочек."
    
    await _save_message(chat_id, user_name, "[Прислал видеосообщение]", user_id=user_id)
    
    # Видеозаметки всегда обрабатываем как видео для Gemini
    await enqueue_reply(
        chat_id, user_id, user_name, user_prompt, message_id, context, 
        is_voice=True, video_file_id=video_note_id, is_video_note=True
    )



def _doc_action_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-кнопки быстрых действий с документом."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Прочитай", callback_data="doc_act:read"),
            InlineKeyboardButton("📋 Суммаризируй", callback_data="doc_act:summarize"),
        ],
        [
            InlineKeyboardButton("🔍 Найди ключевое", callback_data="doc_act:keypoints"),
            InlineKeyboardButton("✍️ Перепиши кратко", callback_data="doc_act:rewrite"),
        ],
        [
            InlineKeyboardButton("❌ Отмена", callback_data="doc_act:cancel"),
        ],
    ])


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов (.txt, .pdf, .docx) — reply с inline-кнопками."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    if not await is_responses_enabled(chat_id):
        return

    document = update.message.document
    if not document:
        return

    # Перехват /vclone или /steal в caption документа (аудио/видео файл)
    caption_handled = await _vclone_caption_fastpath(update, context)
    if caption_handled:
        return

    # Перехват для /vclone flow: видео/аудио файл как источник референса
    from config import vclone_flow_state
    state_vclone = vclone_flow_state.get(chat_id, {}).get(user_id)
    if state_vclone and state_vclone.get("step") == "reference":
        media_kind = classify_vclone_media(document.file_name, document.mime_type)
        if media_kind in ("video", "audio"):
            handled = await handle_vclone_attachment(
                update, context,
                file_id=document.file_id,
                file_size=document.file_size,
                file_name=document.file_name,
                source_kind=f"stepwise_doc_{media_kind}",
            )
            if handled:
                return

    # Перехват для /dub flow: видео/аудио файл как источник дубляжа
    from config import dub_flow_state
    if dub_flow_state.get(chat_id, {}).get(user_id):
        media_kind = classify_dub_media(document.file_name, document.mime_type)
        if media_kind in ("video", "audio"):
            handled = await handle_dub_attachment(
                update, context,
                file_id=document.file_id,
                media_kind=media_kind,
                file_name=document.file_name,
            )
            if handled:
                return
    doc = update.message.document
    message_id = update.message.message_id

    if not await is_responses_enabled(chat_id):
        return

    # VAL-02: в группах не реагируем на КАЖДЫЙ документ. Нужен явный триггер —
    # ЛС, reply на бота или упоминание «арти» в caption. В ЛС работаем как раньше.
    is_private = update.effective_chat.type == "private"
    replied_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    caption_mentions_arti = bool(re.search(r'\bарти\b', (update.message.caption or "").lower()))
    if not (is_private or replied_to_bot or caption_mentions_arti):
        logger.info("Документ в группе без триггера (нет reply/упоминания) — пропускаем.")
        return

    if doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text(
            "<i>смотрит на размер файла с осуждением</i>\n\n"
            "<blockquote>«Слишком тяжело. Я читаю только до 10 МБ — "
            "сожми или разбей на части.»</blockquote>",
            parse_mode='HTML'
        )
        return

    # Читаем документ
    status_msg = await update.message.reply_text(
        "<i>подхватывает файл, разворачивает первую страницу...</i>",
        parse_mode='HTML'
    )

    extracted_text = await extract_document_text(context, doc)

    if not extracted_text:
        await status_msg.edit_text(
            "<i>хмурится, листая пустые страницы</i>\n\n"
            "<blockquote>«Ничего не вышло — документ пустой или защищён. "
            "Попробуй другой формат.»</blockquote>",
            parse_mode='HTML'
        )
        return

    # Сохраняем в pending и показываем кнопки
    key = (chat_id, user_id)
    pending_doc_action[key] = {
        "text": extracted_text,
        "file_name": doc.file_name or "документ",
        "message_id": message_id,
        "bot_message_id": status_msg.message_id,
        "user_name": user_name,
    }

    short_name = (doc.file_name or "документ")[:40]
    await status_msg.edit_text(
        f"<i>откладывает в сторону, поднимает взгляд</i>\n\n"
        f"<blockquote>«<b>{short_name}</b> — получила, прочла первый абзац.\n"
        f"Что с ним делать?»</blockquote>",
        reply_markup=_doc_action_keyboard(),
        parse_mode='HTML'
    )


_DOC_ACTION_PROMPTS = {
    "read":      "Внимательно прочитай этот документ и подробно расскажи, о чём он. Не упускай важные детали.",
    "summarize": "Сделай краткое саммари документа: главная идея, ключевые тезисы, выводы — в 5–7 предложениях.",
    "keypoints": "Выдели ключевые мысли, факты и тезисы документа. Оформи списком.",
    "rewrite":   "Перепиши документ кратко своими словами, сохраняя смысл, но убрав воду и повторы.",
}


async def document_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик инлайн-кнопок для документов."""
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("doc_act:"):
        return

    action = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name or query.from_user.username or "Пользователь"

    key = (chat_id, user_id)
    # L-07: чужая/неактуальная кнопка — тихо отвечаем, не редактируя сообщение владельца.
    if key not in pending_doc_action:
        await query.answer("Эта кнопка не для тебя или уже неактуальна.", show_alert=False)
        return
    pending = pending_doc_action.pop(key, None)

    if not pending:
        await query.answer("Уже неактуально.", show_alert=False)
        return

    # --- Отмена ---
    if action == "cancel":
        await query.edit_message_text(
            "<i>аккуратно закрывает документ и откладывает в сторону</i>\n\n"
            "<blockquote>«Ладно, отложим. Понадоблюсь — знаешь, где найти.»</blockquote>",
            parse_mode='HTML'
        )
        return

    prompt_text = _DOC_ACTION_PROMPTS.get(action, "Прочитай и проанализируй этот документ.")
    file_name = pending["file_name"]
    extracted_text = pending["text"]
    message_id = pending["message_id"]

    await query.edit_message_text(
        "<i>склоняется над страницами, начинает читать...</i>",
        parse_mode='HTML'
    )

    final_prompt = f"Документ '{file_name}':\n\n{extracted_text}\n\nЗадание: {prompt_text}"

    await enqueue_reply(
        chat_id, user_id, user_name,
        final_prompt, message_id, context,
        is_voice=False, document_text=extracted_text
    )


# ============================================================================
# ОБРАБОТЧИК ГЕОЛОКАЦИИ
# ============================================================================

async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик статической, live-геопозиции и обновлений live-геопозиции."""
    # PTB v20: filters.LOCATION ловит и message, и edited_message
    message = update.message or update.edited_message
    if not message or not message.location:
        return

    chat_id = update.effective_chat.id
    user = message.from_user
    if not user:
        return
    user_id = user.id
    user_name = user.first_name or user.username

    from utils.location_manager import set_user_location

    lat = message.location.latitude
    lng = message.location.longitude

    is_live = message.location.live_period is not None
    await set_user_location(user_id, lat, lng, is_live=is_live)

    # Если это edited_message (обновление live location) — тихо обновляем кеш
    if update.edited_message:
        logger.debug(f"📍 Live-геопозиция обновлена для {user_id}: {lat:.5f}, {lng:.5f}")
        return

    # Для первого сообщения — отвечаем пользователю
    if not await is_responses_enabled(chat_id):
        return

    is_live = message.location.live_period is not None
    
    if is_live:
        await message.reply_text(
            "<i>ловит сигнал, подключается к спутнику в реальном времени</i>\n\n"
            "<blockquote>«Трансляция принята! 📡 Теперь я вижу тебя на радаре. "
            "Спрашивай что угодно — где поесть, куда сходить, как добраться — "
            "пока трансляция работает, мои данные будут актуальными!»</blockquote>",
            parse_mode='HTML'
        )
    else:
        await message.reply_text(
            "<i>ловит координаты, сверяется со спутником</i>\n\n"
            "<blockquote>«📍 Координаты приняты! Теперь я знаю, где ты прячешься. "
            "Спрашивай — где поесть, ближайшая аптека, куда сходить — "
            "я найду всё в округе!\n\n"
            "...геопозиция будет активна 30 минут.»</blockquote>",
            parse_mode='HTML'
        )
    
    logger.info(f"📍 Получена {'live ' if is_live else ''}геопозиция от {user_name} ({user_id}): {lat:.5f}, {lng:.5f}")

    # --- АВТОМАТИЧЕСКОЕ ВОЗОБНОВЛЕНИЕ ЗАПРОСА ---
    from config import pending_map_requests
    pending_prompt = pending_map_requests.pop(user_id, None)
    
    if pending_prompt:
        # Небольшая задержка для естественности
        await asyncio.sleep(1)
        logger.info(f"🔄 Автоматически возобновляю запрос для {user_id}: {pending_prompt}")
        await enqueue_reply(
            chat_id, user_id, user_name, pending_prompt, 
            message.message_id, context, is_voice=False
        )


# ============================================================================
# ОБРАБОТЧИК ОШИБОК
# ============================================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# ============================================================================
# ОБРАБОТЧИК АУДИО-СООБЩЕНИЙ (только для /dub flow)
# ============================================================================

async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает аудиосообщения только если активен /dub flow.
    Голосовые сюда не попадают — у них свой filters.VOICE.
    """
    if not update.message or not update.message.audio:
        return

    chat_id = update.effective_chat.id
    user = update.message.from_user
    if not user:
        return
    user_id = user.id

    if not await is_responses_enabled(chat_id):
        return

    # Перехват /vclone или /steal в caption аудиофайла
    caption_handled = await _vclone_caption_fastpath(update, context)
    if caption_handled:
        return

    # Перехват для /vclone flow: аудио как источник референса
    from config import vclone_flow_state
    state_vclone = vclone_flow_state.get(chat_id, {}).get(user_id)
    if state_vclone and state_vclone.get("step") == "reference":
        audio = update.message.audio
        if audio:
            file_name = getattr(audio, "file_name", None) or f"audio_{update.message.message_id}.mp3"
            handled = await handle_vclone_attachment(
                update, context,
                file_id=audio.file_id,
                file_size=audio.file_size,
                file_name=file_name,
                source_kind="stepwise_audio",
            )
            if handled:
                return

    from config import dub_flow_state
    if not dub_flow_state.get(chat_id, {}).get(user_id):
        return

    audio = update.message.audio
    file_name = getattr(audio, "file_name", None) or f"audio_{update.message.message_id}.mp3"
    await handle_dub_attachment(
        update, context,
        file_id=audio.file_id,
        media_kind="audio",
        file_name=file_name,
    )


# ============================================================================
# /URL-видео: инлайн-меню действий
# ============================================================================

import html as _html
import shutil as _shutil
import tempfile as _tempfile


def _video_url_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Транскрипция", callback_data="vurl:transcribe")],
        [InlineKeyboardButton("📋 Краткий конспект", callback_data="vurl:summary")],
        [InlineKeyboardButton("🎬 Озвучить", callback_data="vurl:dub")],
        [InlineKeyboardButton("❌ Отмена", callback_data="vurl:cancel")],
    ])


async def _offer_video_url_actions(bot, chat_id: int, user_id: int, message_id: int, url: str):
    """Шлём предложение с инлайн-кнопками. URL храним в pending до выбора."""
    sent = await bot.send_message(
        chat_id=chat_id,
        text=(
            "<i>Касается банта, прокручивает превью одним движением</i>\n\n"
            "<blockquote>«Видео без контекста — это интригующе. Что мне с ним сделать?»</blockquote>"
        ),
        reply_to_message_id=message_id,
        reply_markup=_video_url_action_keyboard(),
        parse_mode='HTML',
    )
    pending_video_url_action[(chat_id, user_id, message_id)] = {
        "url": url,
        "bot_message_id": sent.message_id,
    }


async def _process_video_url_transcribe(
    bot, chat_id: int, user_id: int, message_id: int, url: str, *, summarize: bool, user_name: str
):
    """Скачивает аудио, транскрибирует, опционально просит конспект."""
    from ai.video_url import download_audio_for_url, transcribe_url_audio, summarize_transcript
    from utils.text_processing import send_continuous_action

    Path("temp").mkdir(parents=True, exist_ok=True)  # L-15: mkdtemp требует существующий dir
    work_dir = Path(_tempfile.mkdtemp(prefix="vurl_", dir="temp"))
    action_task = asyncio.create_task(
        send_continuous_action(bot, chat_id, "typing")
    )
    progress_msg = None
    try:
        progress_msg = await bot.send_message(
            chat_id=chat_id,
            text="<i>Скачиваю аудиодорожку…</i>",
            reply_to_message_id=message_id,
            parse_mode='HTML',
        )

        audio_path = await download_audio_for_url(url, work_dir)

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text="<i>Прогоняю через Whisper…</i>",
                parse_mode='HTML',
            )
        except Exception:
            pass

        transcript = await transcribe_url_audio(audio_path, language=None)

        if not transcript:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "<i>Разводит руками</i>\n"
                    "<blockquote>«В этом видео не нашлось разборчивой речи. Транскрибировать нечего.»</blockquote>"
                ),
                reply_to_message_id=message_id,
                parse_mode='HTML',
            )
            return

        if summarize:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    text="<i>Перевариваю и собираю конспект…</i>",
                    parse_mode='HTML',
                )
            except Exception:
                pass

            summary = await summarize_transcript(chat_id, user_name, transcript, url)
            await _send_long_html(bot, chat_id, message_id, "📋 <b>Краткий конспект</b>\n\n" + summary)
        else:
            await _send_long_html(bot, chat_id, message_id, "📝 <b>Транскрипция</b>\n\n" + _html.escape(transcript))

    except Exception as exc:
        # VAL-05: детали ошибки (стектрейс/внутренние пути) — только в лог, не в чат.
        logger.exception("Ошибка при обработке URL-видео (transcribe/summary)")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "<i>Разводит руками</i>\n"
                    "<blockquote>«Не получилось обработать это видео. Попробуй другое.»</blockquote>"
                ),
                reply_to_message_id=message_id,
                parse_mode='HTML',
            )
        except Exception:
            pass
    finally:
        action_task.cancel()
        if progress_msg:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=progress_msg.message_id)
            except Exception:
                pass
        try:
            _shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


async def _send_long_html(bot, chat_id: int, message_id: int, text: str, chunk_size: int = 3800):
    """Отправляет длинное HTML-сообщение чанками. Поддерживает только теги <b>/<i>/<blockquote>/<pre>/<code>."""
    if len(text) <= chunk_size:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=message_id,
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return
        except Exception:
            # Фоллбек на plain
            await bot.send_message(
                chat_id=chat_id,
                text=re.sub(r"<[^>]+>", "", text),
                reply_to_message_id=message_id,
                disable_web_page_preview=True,
            )
            return

    # Разбиваем по строкам
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > chunk_size:
            if buf:
                parts.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        parts.append(buf)

    first = True
    for part in parts:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                reply_to_message_id=message_id if first else None,
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        except Exception:
            await bot.send_message(
                chat_id=chat_id,
                text=re.sub(r"<[^>]+>", "", part),
                reply_to_message_id=message_id if first else None,
                disable_web_page_preview=True,
            )
        first = False


async def video_url_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback инлайн-меню для URL-видео: vurl:<action>."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("vurl:"):
        return
    await query.answer()

    action = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    user = query.from_user
    user_id = user.id
    user_name = user.first_name or user.username or "Пользователь"

    # Найти pending по reply_to_message_id (сообщение с URL)
    pending_key = None
    if query.message.reply_to_message:
        candidate = (chat_id, user_id, query.message.reply_to_message.message_id)
        if candidate in pending_video_url_action:
            pending_key = candidate
    if pending_key is None:
        # Резервный поиск: первое подходящее с этим bot_message_id
        for key, info in list(pending_video_url_action.items()):
            if info.get("bot_message_id") == query.message.message_id and key[0] == chat_id and key[1] == user_id:
                pending_key = key
                break

    if pending_key is None:
        try:
            await query.edit_message_text(
                "<i>Оглядывается, не находит видео</i>\n"
                "<blockquote>«Эту ссылку я уже отпустила. Пришли заново.»</blockquote>",
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    info = pending_video_url_action.pop(pending_key)
    url = info["url"]
    src_message_id = pending_key[2]

    if action == "cancel":
        try:
            await query.edit_message_text(
                "<i>Складывает ссылку обратно</i>\n"
                "<blockquote>«Хорошо. Если что — пришли снова.»</blockquote>",
                parse_mode='HTML',
            )
        except Exception:
            pass
        return

    if action == "transcribe":
        try:
            await query.edit_message_text(
                "<i>Принимаюсь за транскрипцию…</i>",
                parse_mode='HTML',
            )
        except Exception:
            pass
        asyncio.create_task(_process_video_url_transcribe(
            context.bot, chat_id, user_id, src_message_id, url,
            summarize=False, user_name=user_name,
        ))
        return

    if action == "summary":
        try:
            await query.edit_message_text(
                "<i>Снимаю транскрипт и собираю конспект…</i>",
                parse_mode='HTML',
            )
        except Exception:
            pass
        asyncio.create_task(_process_video_url_transcribe(
            context.bot, chat_id, user_id, src_message_id, url,
            summarize=True, user_name=user_name,
        ))
        return

    if action == "dub":
        # Переиспользуем dub flow: сразу шаг про сабы
        from config import dub_flow_state, TTS_ENABLED
        from bot.commands import _dub_subs_keyboard
        from utils.admin import is_admin

        # CONF-01: озвучка требует TTS-бэкендов — при выключенном TTS честный отказ.
        if not TTS_ENABLED:
            try:
                await query.edit_message_text(
                    "<i>прикрывает микрофон ладонью</i>\n"
                    "<blockquote>«Голосовые функции сейчас отключены.»</blockquote>",
                    parse_mode='HTML',
                )
            except Exception:
                pass
            return

        # Озвучка — тяжёлый videotrans-pipeline, доступен только админам (как и /dub).
        if not await is_admin(user, chat_id, context):
            try:
                await query.edit_message_text(
                    "<i>Качает головой</i>\n"
                    "<blockquote>«Озвучка — только для администраторов. Не в этот раз.»</blockquote>",
                    parse_mode='HTML',
                )
            except Exception:
                pass
            return

        try:
            await query.edit_message_text(
                "<i>Готовится к озвучке</i>",
                parse_mode='HTML',
            )
        except Exception:
            pass

        dub_flow_state[chat_id][user_id] = {
            "step": "subs",
            "url": url,
            "audio_only": False,
            "message_id": src_message_id,
        }
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<i>Касается банта</i>\n\n"
                "<blockquote>«Принято. Нужны субтитры? Я могу вшить русские прямо в кадр.»</blockquote>"
            ),
            reply_to_message_id=src_message_id,
            reply_markup=_dub_subs_keyboard(),
            parse_mode='HTML',
        )
        return


# Троттлинг ненавязчивого ответа Арти на реакцию (в памяти процесса): не чаще
# одного авто-ответа на чат раз в REACTION_REPLY_MIN_INTERVAL секунд.
_last_reaction_reply: dict = {}
REACTION_REPLY_MIN_INTERVAL = 150.0
REACTION_REPLY_PROB = 0.5

# Анти-спам сдвига настроения от реакций: первая реакция нудит вектор настроения,
# повторные в пределах окна игнорируются — иначе спам реакциями копит настроение
# до максимума (у настроения, в отличие от подкрепления профиля, своего кулдауна нет).
_last_reaction_mood: dict = {}
REACTION_MOOD_COOLDOWN = 60.0


async def handle_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает изменения реакций пользователей на сообщения Арти.

    На добавленную реакцию (ТОЛЬКО к сообщению самого бота, AUTH-03):
      1) подкрепляет аффективный профиль (closeness/receptivity),
      2) сдвигает вектор настроения Арти (влияет на тон следующих ответов),
      3) на эмоционально сильную реакцию иногда (вероятностно, с троттлингом)
         отвечает короткой репликой или стикером — ненавязчиво.
    """
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    chat_id = reaction_update.chat.id
    user = reaction_update.user
    if not user or user.is_bot:
        return
    user_id = user.id
    message_id = reaction_update.message_id

    # AUTH-03: в выключенном (/stop) чате реакции не влияют на состояние.
    if not await is_responses_enabled(chat_id):
        return

    # AUTH-03: реагируем ТОЛЬКО на реакции к сообщениям самого бота. Telegram не
    # сообщает автора сообщения в апдейте реакции, поэтому сверяемся с трекером
    # исходящих сообщений. Иначе лайки участников друг другу накручивали бы
    # close­ness/настроение Арти и открывали проактивные пуши.
    from utils.sent_messages import is_bot_message
    if not is_bot_message(chat_id, message_id):
        logger.debug(
            f"Реакция в чате {chat_id} на сообщение {message_id} проигнорирована: "
            f"это не сообщение бота."
        )
        return

    new_reactions = reaction_update.new_reaction or []
    old_reactions = reaction_update.old_reaction or []

    # Сравниваем списки emoji реакций
    new_emojis = [r.emoji for r in new_reactions if hasattr(r, "emoji") and r.emoji]
    old_emojis = [r.emoji for r in old_reactions if hasattr(r, "emoji") and r.emoji]

    added_emojis = [e for e in new_emojis if e not in old_emojis]
    if not added_emojis:
        return

    from bot.reactions import classify_reactions
    from database.models import MemoryUserProfile, ChatEmotionalState
    mode = "rp" if rp_mode_state.get(chat_id) else "default"

    effect = classify_reactions(added_emojis)
    if not effect:
        logger.info(f"Реакции {added_emojis} от {user_id} в чате {chat_id} не распознаны — пропускаем.")
        return

    logger.info(
        f"Реакция {added_emojis} от {user_id} в чате {chat_id}: "
        f"reinforcement={effect['reinforcement']} mood={effect['mood']} reply_mood={effect['reply_mood']}"
    )

    # 1) Подкрепление аффективного профиля
    if effect["reinforcement"]:
        await MemoryUserProfile.apply_reinforcement(chat_id, user_id, mode, effect["reinforcement"])

    # 2) Сдвиг вектора настроения Арти (с анти-спам кулдауном на чат)
    if effect["mood"]:
        import time as _t
        now_mood = _t.monotonic()
        last_mood = _last_reaction_mood.get(chat_id)
        if last_mood is not None and (now_mood - last_mood) < REACTION_MOOD_COOLDOWN:
            logger.info(
                f"Сдвиг настроения от реакции пропущен (cooldown) в чате {chat_id}: "
                f"{REACTION_MOOD_COOLDOWN - (now_mood - last_mood):.0f}с осталось"
            )
        else:
            _last_reaction_mood[chat_id] = now_mood
            await ChatEmotionalState.apply_mood_delta(chat_id, effect["mood"], source="reaction")

    # 3) Ненавязчивый ответ на сильную реакцию
    await _maybe_reply_to_reaction(context, chat_id, user_id, message_id, mode, effect["reply_mood"])


async def _maybe_reply_to_reaction(context, chat_id, user_id, message_id, mode, reply_mood):
    """Иногда отвечает на сильную эмоциональную реакцию репликой или стикером.

    Срабатывает не на каждую реакцию: только если задан reply_mood, ответы в чате
    включены, прошёл троттлинг и выпала вероятность. Так Арти «замечает» сильную
    реакцию, но не спамит.
    """
    if not reply_mood:
        return
    if not await is_responses_enabled(chat_id):
        return

    import time as _t
    now = _t.monotonic()
    last = _last_reaction_reply.get(chat_id)
    if last is not None and (now - last) < REACTION_REPLY_MIN_INTERVAL:
        return
    if random.random() > REACTION_REPLY_PROB:
        return
    _last_reaction_reply[chat_id] = now

    bot = context.bot
    # Канал ответа: примерно поровну короткая реплика или стикер.
    if random.random() < 0.5:
        from bot.reactions import pick_reaction_reply
        text = pick_reaction_reply(reply_mood)
        if not text:
            return
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(random.uniform(1.2, 2.6))
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=message_id,
                parse_mode='HTML',
            )
            logger.info(f"Арти ответила репликой на реакцию (mood={reply_mood}) в чате {chat_id}.")
        except Exception as e:
            logger.warning(f"Не удалось отправить реплику на реакцию в чате {chat_id}: {e}")
    else:
        from bot.reactions import REPLY_MOOD_TO_STICKER
        from ai.stickers import send_mood_sticker_task
        sticker_mood = REPLY_MOOD_TO_STICKER.get(reply_mood)
        if not sticker_mood:
            return
        asyncio.create_task(
            send_mood_sticker_task(
                bot=bot,
                chat_id=chat_id,
                user_id=user_id,
                mood=sticker_mood,
                message_id=message_id,
                mode=mode,
                force=True,
                user_message_id=message_id,
            )
        )
        logger.info(f"Арти ответила стикером на реакцию (mood={sticker_mood}) в чате {chat_id}.")
