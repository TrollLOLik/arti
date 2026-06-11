"""
Команды бота: /start, /stop, /clear_context, /arti_commands, /image, /video, /music, /rps, /model
"""
import re
import random
import logging
import asyncio
from typing import Optional, List, Tuple, Dict, Any

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    START_RESPONSES, STOP_RESPONSES, CLEAR_CONTEXT_RESPONSES,
    music_flow_state, waiting_for_video_prompt, waiting_for_image_prompt,
    pending_image_inputs, pending_video_inputs, pending_map_requests, pending_photo_action,
    rp_mode_state, SKIP_WORDS, dub_flow_state, vclone_flow_state,
    vclone_save_flow_state, pending_video_url_action, CATBOX_USERHASH,
    waiting_for_model_search
)
from utils.spam_protection import handle_spam_protection
from utils.admin import is_admin
from utils.response_status import is_responses_enabled, set_responses_enabled
from utils.chat_history import save_chat_message
from utils.text_processing import extract_urls_and_make_keyboard
from utils.model_selection import get_chat_model, set_chat_model
from ai.generation import generate_response_stream
from bot.queue import enqueue_generation, _extract_photo_urls, enqueue_dubbing
from database.models import ChatHistory, SpamProtection as SpamProtectionModel, SavedVoice, MemoryUserProfile, MemoryFact, AIModel


logger = logging.getLogger(__name__)

IMAGE_ASPECT_OPTIONS = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"]
IMAGE_RESOLUTION_OPTIONS = ["512", "1K", "2K", "4K"]
IMAGE_NUM_OPTIONS = ["1", "2", "3", "4"]


def _image_default_flow(image_urls=None):
    return {"aspect_ratio": "1:1", "resolution": "1K", "num_images": 1, "image_urls": image_urls or []}


def _image_reply_keyboard(options, columns=2):
    rows = []
    for index in range(0, len(options), columns):
        rows.append([telegram.KeyboardButton(option) for option in options[index:index + columns]])
    return telegram.ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def _image_task(chat_id, prompt, context, message_id, image_urls, user_name, flow=None):
    flow = flow or _image_default_flow(image_urls)
    return {
        'type': 'image', 'chat_id': chat_id, 'prompt': prompt,
        'context': context, 'message_id': message_id,
        'image_urls': image_urls,
        'user_name': user_name,
        'image_aspect_ratio': flow.get("aspect_ratio", "1:1"),
        'image_resolution': flow.get("resolution", "1K"),
        'image_num_images': flow.get("num_images", 1)
    }


async def _start_image_settings_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, image_urls=None):
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    pending_image_inputs[chat_id][user_id] = image_urls or []
    waiting_for_image_prompt[chat_id][user_id] = False
    context.user_data["image_flow"] = {
        "chat_id": chat_id,
        "step": "aspect_ratio",
        "image_urls": image_urls or []
    }
    await update.message.reply_text(
        "🎨 <b>Генерация изображения: шаг 1/4</b>\n\nВыбери ориентацию:",
        reply_markup=_image_reply_keyboard(IMAGE_ASPECT_OPTIONS, columns=3),
        parse_mode='HTML'
    )


async def _enqueue_image_from_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, image_urls=None):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    user_name = user.first_name or user.username or "Пользователь"
    flow = context.user_data.get("image_flow") or _image_default_flow(image_urls)
    waiting_for_image_prompt[chat_id][user_id] = False
    pending_image_inputs[chat_id].pop(user_id, None)
    context.user_data.pop("image_flow", None)
    await enqueue_generation(
        _image_task(chat_id, prompt, context, update.message.message_id, image_urls or flow.get("image_urls", []), user_name, flow),
        context.bot,
        chat_id
    )
    await save_chat_message(chat_id, user_name, f"Пользователь запросил изображение: {prompt}")


async def handle_image_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    flow = context.user_data.get("image_flow", {})
    if not flow or flow.get("chat_id") != chat_id:
        return False

    text = update.message.text or update.message.caption or ""
    step = flow.get("step")

    if step == "aspect_ratio":
        if text not in IMAGE_ASPECT_OPTIONS:
            await update.message.reply_text("Выбери ориентацию кнопкой ниже.", reply_markup=_image_reply_keyboard(IMAGE_ASPECT_OPTIONS, columns=3))
            return True
        aspect_ratio = text
        flow["aspect_ratio"] = aspect_ratio
        flow["step"] = "resolution"
        context.user_data["image_flow"] = flow
        await update.message.reply_text(
            "🖼 <b>Генерация изображения: шаг 2/4</b>\n\nВыбери выходное разрешение:",
            reply_markup=_image_reply_keyboard(IMAGE_RESOLUTION_OPTIONS, columns=2),
            parse_mode='HTML'
        )
        return True

    if step == "resolution":
        if text not in IMAGE_RESOLUTION_OPTIONS:
            await update.message.reply_text("Выбери разрешение кнопкой ниже.", reply_markup=_image_reply_keyboard(IMAGE_RESOLUTION_OPTIONS, columns=2))
            return True
        resolution = text
        flow["resolution"] = resolution
        flow["step"] = "num_images"
        context.user_data["image_flow"] = flow
        await update.message.reply_text(
            "🔢 <b>Генерация изображения: шаг 3/4</b>\n\nВыбери количество изображений для генерации:",
            reply_markup=_image_reply_keyboard(IMAGE_NUM_OPTIONS, columns=4),
            parse_mode='HTML'
        )
        return True

    if step == "num_images":
        if text not in IMAGE_NUM_OPTIONS:
            await update.message.reply_text("Выбери количество кнопкой ниже (1-4).", reply_markup=_image_reply_keyboard(IMAGE_NUM_OPTIONS, columns=4))
            return True
        num_images = int(text)
        flow["num_images"] = num_images
        flow["step"] = "prompt"
        flow["waiting"] = True
        context.user_data["image_flow"] = flow
        waiting_for_image_prompt[chat_id][user_id] = True
        await update.message.reply_text(
            "✍️ <b>Генерация изображения: шаг 4/4</b>\n\nТеперь отправь prompt текстом или картинку с caption.\n\n"
            "💡 <i>Для отмены введи /cancel</i>",
            reply_markup=telegram.ReplyKeyboardRemove(),
            parse_mode='HTML'
        )
        return True

    if step == "prompt":
        if not text:
            await update.message.reply_text("✍️ Жду текстовый prompt для изображения или /cancel.", parse_mode='HTML')
            return True
        await _enqueue_image_from_flow(update, context, text, flow.get("image_urls", []))
        return True

    return False

VIDEO_MODEL_OPTIONS = {
    "Seedance 2 Fast": "bytedance/seedance-2-0-fast",
    "Veo 3 Lite": "google/veo-3-lite",
    "Sora 2": "openai/sora-2",
}
VIDEO_DURATION_OPTIONS = ["4", "8", "12", "16", "20"]
VIDEO_ASPECT_OPTIONS = ["16:9", "9:16"]


def _video_default_flow(image_urls=None):
    return {
        "model": "openai/sora-2",
        "duration": "8",
        "aspect_ratio": "16:9",
        "image_urls": image_urls or []
    }


def _video_reply_keyboard(options):
    return telegram.ReplyKeyboardMarkup(
        [[telegram.KeyboardButton(option)] for option in options],
        one_time_keyboard=True,
        resize_keyboard=True
    )


def _video_task(chat_id, prompt, context, message_id, image_urls, user_name, flow=None):
    flow = flow or _video_default_flow(image_urls)
    return {
        'type': 'video', 'chat_id': chat_id, 'prompt': prompt,
        'context': context, 'message_id': message_id,
        'image_urls': image_urls,
        'user_name': user_name,
        'video_model': flow.get("model"),
        'video_duration': flow.get("duration"),
        'video_aspect_ratio': flow.get("aspect_ratio")
    }


async def _start_video_settings_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, image_urls=None):
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    pending_video_inputs[chat_id][user_id] = image_urls or []
    waiting_for_video_prompt[chat_id][user_id] = False
    context.user_data["video_flow"] = {
        "chat_id": chat_id,
        "step": "model",
        "image_urls": image_urls or []
    }
    await update.message.reply_text(
        "🎬 <b>Генерация видео: шаг 1/4</b>\n\nВыбери модель:",
        reply_markup=_video_reply_keyboard(list(VIDEO_MODEL_OPTIONS.keys())),
        parse_mode='HTML'
    )


async def _enqueue_video_from_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, image_urls=None):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    user_name = user.first_name or user.username or "Пользователь"
    flow = context.user_data.get("video_flow") or _video_default_flow(image_urls)
    waiting_for_video_prompt[chat_id][user_id] = False
    pending_video_inputs[chat_id].pop(user_id, None)
    context.user_data.pop("video_flow", None)
    await enqueue_generation(
        _video_task(chat_id, prompt, context, update.message.message_id, image_urls or flow.get("image_urls", []), user_name, flow),
        context.bot,
        chat_id
    )
    await save_chat_message(chat_id, user_name, f"Пользователь запросил видео: {prompt}")


async def handle_video_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    flow = context.user_data.get("video_flow", {})
    if not flow or flow.get("chat_id") != chat_id:
        return False

    text = update.message.text or update.message.caption or ""
    step = flow.get("step")

    if step == "model":
        if text not in VIDEO_MODEL_OPTIONS:
            await update.message.reply_text("Выбери модель кнопкой ниже.", reply_markup=_video_reply_keyboard(list(VIDEO_MODEL_OPTIONS.keys())))
            return True
        flow["model"] = VIDEO_MODEL_OPTIONS[text]
        flow["step"] = "duration"
        await update.message.reply_text(
            "⏱ <b>Генерация видео: шаг 2/4</b>\n\nВыбери длительность:",
            reply_markup=_video_reply_keyboard(VIDEO_DURATION_OPTIONS),
            parse_mode='HTML'
        )
        return True

    if step == "duration":
        if text not in VIDEO_DURATION_OPTIONS:
            await update.message.reply_text("Выбери длительность кнопкой ниже: 4, 8, 12, 16 или 20 секунд.", reply_markup=_video_reply_keyboard(VIDEO_DURATION_OPTIONS))
            return True
        flow["duration"] = text
        flow["step"] = "aspect_ratio"
        await update.message.reply_text(
            "📐 <b>Генерация видео: шаг 3/4</b>\n\nВыбери ориентацию:",
            reply_markup=_video_reply_keyboard(VIDEO_ASPECT_OPTIONS),
            parse_mode='HTML'
        )
        return True

    if step == "aspect_ratio":
        if text not in VIDEO_ASPECT_OPTIONS:
            await update.message.reply_text("Выбери ориентацию кнопкой ниже.", reply_markup=_video_reply_keyboard(VIDEO_ASPECT_OPTIONS))
            return True
        flow["aspect_ratio"] = text
        flow["step"] = "prompt"
        flow["waiting"] = True
        waiting_for_video_prompt[chat_id][user_id] = True
        await update.message.reply_text(
            "✍️ <b>Генерация видео: шаг 4/4</b>\n\nТеперь отправь prompt текстом или картинку с caption.\n\n"
            "💡 <i>Для отмены введи /cancel</i>",
            reply_markup=telegram.ReplyKeyboardRemove(),
            parse_mode='HTML'
        )
        return True

    if step == "prompt":
        if not text:
            await update.message.reply_text("✍️ Жду текстовый prompt для видео или /cancel.", parse_mode='HTML')
            return True
        await _enqueue_video_from_flow(update, context, text, flow.get("image_urls", []))
        return True

    return False


# ============================================================================
# /start
# ============================================================================

async def start(update, context):
    user = update.message.from_user
    chat_id = update.effective_chat.id

    if not await handle_spam_protection(update, context, "start"):
        return

    if not await is_admin(user, chat_id, context):
        if await is_responses_enabled(chat_id):
            await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    await set_responses_enabled(chat_id, True)
    logger.info(f"Бот включен в чате {chat_id}.")
    await update.message.reply_text(random.choice(START_RESPONSES), parse_mode='HTML')


# ============================================================================
# /stop
# ============================================================================

async def stop(update, context):
    user = update.message.from_user
    chat_id = update.effective_chat.id

    if not await handle_spam_protection(update, context, "stop"):
        return

    if not await is_admin(user, chat_id, context):
        if await is_responses_enabled(chat_id):
            await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    await set_responses_enabled(chat_id, False)
    logger.info(f"Бот отключен в чате {chat_id}.")
    await update.message.reply_text(random.choice(STOP_RESPONSES), parse_mode='HTML')


# ============================================================================
# /clear_context
# ============================================================================

async def clear_context(update, context):
    user = update.message.from_user
    chat_id = update.effective_chat.id

    if not await is_responses_enabled(chat_id):
        return

    if not await handle_spam_protection(update, context, "clear_context"):
        return

    if not await is_admin(user, chat_id, context):
        if await is_responses_enabled(chat_id):
            await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    # Очищаем историю в БД
    await ChatHistory.clear(chat_id)
    logger.info(f"Контекст чата {chat_id} успешно очищен.")
    
    if await is_responses_enabled(chat_id):
        await update.message.reply_text(random.choice(CLEAR_CONTEXT_RESPONSES), parse_mode='HTML')


# ============================================================================
# /arti_commands
# ============================================================================

async def arti_commands(update, context):
    chat_id = update.effective_chat.id

    if not await is_responses_enabled(chat_id):
        return

    if not await handle_spam_protection(update, context, "arti_commands"):
        return

    commands_list = (
        "<i>Касается пальцами банта на шее, выводя голографическую панель управления на экран терминала. Свечение в её звёздчатых радужках становится ярче.</i>\n\n"
        "<blockquote>«Подключаю терминал... Доступ к ядру Арти санкционирован. Вот полный список моих системных команд и модулей, субъект:»</blockquote>\n\n"
        "📡 <b>ЦЕНТР УПРАВЛЕНИЯ АРТИ</b>\n"
        "──────────────────────────────\n"
        "🧠 <b>Основные и Ментальные команды:</b>\n"
        "• /start — Инициализировать сознание Арти\n"
        "• /stop — Перевести системы в спящий режим\n"
        "• /clear_context — Полностью очистить оперативную память текущего чата\n"
        "• /my_profile — Вывести твоё секретное досье / RPG Character Sheet\n"
        "• /forget [тема] — Интерактивно стереть воспоминание из долгосрочной памяти\n"
        "• /model — Переключить модель мышления (⚡Быстрая / 🧠Умная)\n"
        "• /cancel — Экстренно свернуть активные медиа-потоки\n"
        "• /rp — Активировать режим глубокого ролевого погружения (только в ЛС)\n\n"
        "🎨 <b>Модули генерации медиа:</b>\n"
        "• /image — Синтезировать изображение по текстовому описанию\n"
        "• /video — Сгенерировать кинематографичный видеоряд\n"
        "• /music — Сочинить музыкальную композицию (пошаговый конструктор)\n"
        "• /dub — Дублировать видео или аудио на русский язык (с субтитрами)\n"
        "• /vclone (или /steal) — Скопировать голос из аудио-файла и озвучить им текст\n"
        "• /voices — Показать реестр твоих сохранённых слепков голосов\n"
        "• /voice_save — Извлечь и сохранить слепок голоса без озвучивания\n"
        "• /voice_delete — Стереть сохранённый слепок голоса из базы\n\n"
        "🎲 <b>Развлекательные протоколы:</b>\n"
        "• /rps — Сыграть с Арти в классическую «цу-е-фа» (Камень, Ножницы, Бумага)\n\n"
        "──────────────────────────────\n"
        "🛠 <b>Системный архитектор:</b> @DeallSign"
    )
    await update.message.reply_text(commands_list, parse_mode="HTML")


# ============================================================================
# /cancel
# ============================================================================

async def handle_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет текущие пошаговые запросы (музыка, фото, видео)."""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    
    # Сбрасываем все состояния ожидания
    waiting_for_image_prompt[chat_id][user_id] = False
    pending_image_inputs[chat_id][user_id] = []
    context.user_data.pop("image_flow", None)
    waiting_for_video_prompt[chat_id][user_id] = False
    pending_video_inputs[chat_id][user_id] = []
    context.user_data.pop("video_flow", None)
    context.user_data.pop("pending_base64_for_gen", None)
    waiting_for_model_search[chat_id].pop(user_id, None)
    if chat_id in music_flow_state and user_id in music_flow_state[chat_id]:
        del music_flow_state[chat_id][user_id]
    if chat_id in dub_flow_state and user_id in dub_flow_state[chat_id]:
        # Чистим возможный временный input_file
        st = dub_flow_state[chat_id].get(user_id) or {}
        ifp = st.get("input_file") if isinstance(st, dict) else None
        if ifp:
            try:
                p = _Path(ifp)
                if p.exists() and p.is_file() and "temp" in p.parts:
                    p.unlink()
            except Exception:
                pass
        dub_flow_state[chat_id].pop(user_id, None)
    # /vclone: чистим reference и cleaned-копию (если был выбран separator)
    from config import vclone_flow_state
    if chat_id in vclone_flow_state and user_id in vclone_flow_state[chat_id]:
        vc_state = vclone_flow_state[chat_id].get(user_id) or {}
        if isinstance(vc_state, dict):
            ref_path = vc_state.get("reference_path")
            cleaned_path = vc_state.get("cleaned_path")
            if ref_path or cleaned_path:
                try:
                    cleanup_vclone_files(ref_path, cleaned_path)
                except Exception:
                    pass
        vclone_flow_state[chat_id].pop(user_id, None)
    if chat_id in vclone_save_flow_state and user_id in vclone_save_flow_state[chat_id]:
        try:
            _vclone_cleanup_save_state(chat_id, user_id)
        except Exception:
            vclone_save_flow_state[chat_id].pop(user_id, None)
    if user_id in pending_map_requests:
        del pending_map_requests[user_id]
    # Очищаем ожидающие действия по URL-видео
    for key in list(pending_video_url_action.keys()):
        if key[0] == chat_id and key[1] == user_id:
            pending_video_url_action.pop(key, None)
    # Очищаем ожидание действия с фото
    photo_key = (chat_id, user_id)
    pending = pending_photo_action.pop(photo_key, None)
    if pending and pending.get("bot_message_id"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=pending["bot_message_id"], reply_markup=None
            )
        except Exception:
            pass

    # Выходим из RP-режима, если активен
    if chat_id in rp_mode_state:
        del rp_mode_state[chat_id]
        logger.info(f"RP-режим отключен для чата {chat_id} пользователем {user_id}")

    logger.info(f"Запрос отменен пользователем {user_id} в чате {chat_id}.")
    
    from telegram import ReplyKeyboardRemove
    await update.message.reply_text(
        "<i>С облегчением вздыхает и закрывает лишние вкладки терминала</i>\n"
        "<blockquote>«Ладно, отменяем. Все равно это была сомнительная затея. Что дальше?»</blockquote>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='HTML'
    )


# ============================================================================
# /image
# ============================================================================

async def handle_rp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает RP-режим (только в личных чатах)."""
    chat_id = update.effective_chat.id

    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "<blockquote>«RP-режим доступен только в личных сообщениях.»</blockquote>",
            parse_mode='HTML'
        )
        return

    rp_mode_state[chat_id] = True
    logger.info(f"RP-режим включен для чата {chat_id}")
    await update.message.reply_text(
        "<i>включает режим погружения...</i>\n\n"
        "<blockquote>«Сессия открыта. Правила просты: я говорю, ты слушаешь. Или наоборот — зависит от того, кто первый моргнет.»</blockquote>\n\n"
        "<i>Доступные команды в RP:</i> /model /start /stop /cancel\n"
        "<i>Медиа: принимаю фото, видео, голосовые, документы.</i>",
        parse_mode='HTML'
    )


async def handle_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    
    if rp_mode_state.get(chat_id):
        await update.message.reply_text("<blockquote>«В RP-режиме генерация медиа недоступна.»</blockquote>", parse_mode='HTML')
        return
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "image"):
        return
    image_urls = await _extract_photo_urls(update, context)
    if not context.args:
        await _start_image_settings_flow(update, context, image_urls)
        return
    prompt = " ".join(context.args)
    num_images = 1
    match = re.search(r'\b(?:-n|--num)\s+(\d+)\b', prompt)
    if match:
        try:
            num_images = int(match.group(1))
            num_images = max(1, min(4, num_images))
        except (ValueError, TypeError):
            num_images = 1
        prompt = re.sub(r'\b(?:-n|--num)\s+(\d+)\b', '', prompt).strip()
        prompt = re.sub(r'\s+', ' ', prompt)

    flow = _image_default_flow(image_urls)
    flow["num_images"] = num_images

    await enqueue_generation(
        _image_task(
            chat_id, prompt, context, update.message.message_id,
            image_urls, user.first_name or user.username or "Пользователь",
            flow=flow
        ),
        context.bot,
        chat_id
    )


# ============================================================================
# /video
# ============================================================================

async def handle_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /video <описание>."""
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id

    if rp_mode_state.get(chat_id):
        await update.message.reply_text("<blockquote>«В RP-режиме генерация медиа недоступна.»</blockquote>", parse_mode='HTML')
        return
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "video"):
        return

    image_urls = await _extract_photo_urls(update, context)
    if not context.args:
        await _start_video_settings_flow(update, context, image_urls)
        return

    prompt = " ".join(context.args)
    await enqueue_generation(
        _video_task(
            chat_id, prompt, context, update.message.message_id,
            image_urls, user.first_name or user.username or "Пользователь"
        ),
        context.bot,
        chat_id
    )


# ============================================================================
# /music (пошаговый диалог)
# ============================================================================

async def handle_music_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /music. Запускает пошаговый диалог."""
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id

    if rp_mode_state.get(chat_id):
        await update.message.reply_text("<blockquote>«В RP-режиме генерация медиа недоступна.»</blockquote>", parse_mode='HTML')
        return
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "music"):
        return

    music_flow_state[chat_id][user_id] = {
        'step': 'instrumental',
        'style': 'Pop',
        'instrumental': False,
        'context': context,
        'message_id': update.message.message_id
    }

    keyboard = [
        [telegram.KeyboardButton("🎸 Инструментал (без слов)"), telegram.KeyboardButton("🎤 Со словами")]
    ]
    reply_markup = telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        "🎵 <b>Генерация музыки: Шаг 1/3</b>\n\n"
        "Выбери формат:\n"
        "💡 <i>Для отмены введи /cancel</i>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def handle_music_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id, user_id, user_text):
    """Обрабатывает пошаговый ввод для генерации музыки."""
    state = music_flow_state[chat_id][user_id]
    if not state:
        return False

    message_id = update.message.message_id

    if state['step'] == 'instrumental':
        text_lower = user_text.strip().lower()
        if "инструментал" in text_lower or "без слов" in text_lower:
            state['instrumental'] = True
        else:
            state['instrumental'] = False

        state['step'] = 'style'
        
        reply_markup = telegram.ReplyKeyboardRemove()
        await update.message.reply_text(
            "🎵 <b>Шаг 2/3 — Музыкальный стиль / Промпт</b>\n\n"
            "Напиши жанр, настроение, инструменты (на английском). Это ОБЯЗАТЕЛЬНО.\n"
            "Пример: <code>Russian Synthwave, upbeat, male vocal</code>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return True

    elif state['step'] == 'style':
        text_lower = user_text.strip().lower()
        if text_lower in SKIP_WORDS or not text_lower:
            await update.message.reply_text("❌ Музыкальный стиль обязателен! Напиши жанр, настроение или инструменты.", parse_mode='HTML')
            return True
            
        state['style'] = user_text.strip()
        state['step'] = 'lyrics'

        await update.message.reply_text(
            f"🎵 <b>Шаг 3/3 — Текст / Слова песни</b> (Необязательно)\n\n"
            f"Музыкальный стиль: <code>{state['style']}</code>\n\n"
            f"Напиши текст песни. Если передумал и хочешь без слов или пусть ИИ сам придумает — напиши <code>скип</code> или <code>-</code>.",
            parse_mode='HTML'
        )
        return True

    elif state['step'] == 'lyrics':
        style = state['style']
        instrumental = state['instrumental']
        
        if user_text.strip().lower() in SKIP_WORDS:
            prompt = ""
        else:
            prompt = user_text.strip()

        music_flow_state[chat_id][user_id] = None

        user_name = update.message.from_user.first_name or update.message.from_user.username or "Пользователь"
        task = {
            'type': 'music', 'chat_id': chat_id, 'prompt': prompt,
            'style': style, 'instrumental': instrumental,
            'context': context, 'message_id': message_id,
            'user_name': user_name
        }
        await enqueue_generation(task, context.bot, chat_id)
        return True

    return False


# ============================================================================
# /rps (Камень, Ножницы, Бумага)
# ============================================================================

async def handle_rps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if rp_mode_state.get(chat_id):
        await update.message.reply_text("<blockquote>«В RP-режиме игры недоступны.»</blockquote>", parse_mode='HTML')
        return
    if not await is_responses_enabled(chat_id):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("🪨 Камень", callback_data="rps_rock"),
            InlineKeyboardButton("📜 Бумага", callback_data="rps_paper"),
            InlineKeyboardButton("✂️ Ножницы", callback_data="rps_scissors")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "<i>Крутит в руках воображаемую монетку</i> <blockquote>«цу-е-фа? или ты слишком медленный для таких игр? выбирай, если смелый»</blockquote>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def rps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    
    user_choice = query.data.split("_")[1]
    bot_choices = ["rock", "paper", "scissors"]
    
    # "Читерство" Арти: в 30% случаев выбирает победный вариант
    if random.random() < 0.3:
        if user_choice == "rock": bot_choice = "paper"
        elif user_choice == "paper": bot_choice = "scissors"
        else: bot_choice = "rock"
    else:
        bot_choice = random.choice(bot_choices)
        
    emojis = {"rock": "🪨", "paper": "📜", "scissors": "✂️"}
    
    result = ""
    if user_choice == bot_choice:
        result = "ничья"
    elif (user_choice == "rock" and bot_choice == "scissors") or \
         (user_choice == "paper" and bot_choice == "rock") or \
         (user_choice == "scissors" and bot_choice == "paper"):
        result = "ты победил (наверное, случайно)"
    else:
        result = "я победила, кто бы сомневался"
        
    logger.info(f"RPS: {user_name}. Результат: {result}")
    
    comment, used_search, grounding_links, _ = await generate_response_stream(
        chat_id,
        f"Результат игры: {result}. Поиздевайся над ним в стиле Арти. Используй HTML теги <i> и <blockquote>.",
        user_name, "",
        model="gemini-3.1-flash-lite-preview",
        temperature=0.7,
        user_id=user_id,
        is_rp_mode=rp_mode_state.get(chat_id, False),
    )

    comment = re.sub(r'<think>.*?</think>', '', comment, flags=re.DOTALL | re.IGNORECASE).strip()
    
    display_comment = re.sub(
        r'(&lt;|<)\s*/?(?:break|speak|prosody)\b.*?(&gt;|>)',
        '', comment, flags=re.IGNORECASE | re.DOTALL
    ).strip()
    
    display_comment = re.sub(r'\[[^\]]+\]', '', display_comment).strip()
    display_comment, rps_reply_markup = extract_urls_and_make_keyboard(display_comment, extra_links=grounding_links)
    
    await query.edit_message_text(
        f"Твой выбор: {emojis[user_choice]}\nМой выбор: {emojis[bot_choice]}\n\n<b>Результат:</b> {result}\n\n{display_comment}",
        reply_markup=rps_reply_markup,
        parse_mode='HTML'
    )



async def _ping_single_model(model_info: dict, sem: asyncio.Semaphore) -> Tuple[str, Any]:
    """Вспомогательная функция для пинга одной модели.
    Возвращает (model_id, результат). Результат может быть float (секунды) или str (ошибка).
    """
    import asyncio
    import os
    import time
    from openai import AsyncOpenAI
    from google.genai import types
    from config import genai_client

    model_id = model_info["model"]
    
    async with sem:
        start_time = time.monotonic()
        # Задаем таймаут 5 секунд
        timeout_sec = 5.0
        
        try:
            if model_id.lower().startswith("gemini"):
                # Запускаем в потоке, так как клиент google-genai синхронный
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        genai_client.models.generate_content,
                        model=model_id,
                        contents="1",
                        config=types.GenerateContentConfig(
                            max_output_tokens=5, # Увеличили до 5, чтобы избежать валидационных ошибок у некоторых провайдеров
                            temperature=0.0
                        )
                    ),
                    timeout=timeout_sec
                )
                if response and response.text:
                    return model_id, time.monotonic() - start_time
                return model_id, "empty"
            else:
                client = AsyncOpenAI(
                    base_url="http://localhost:20128/v1",
                    api_key=os.getenv("OMNIROUTE_API_KEY", ""),
                    max_retries=0 # Отключаем ретраи, чтобы не копить таймауты при перегрузке
                )
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "user", "content": "1"}],
                        max_tokens=5, # Увеличили до 5, чтобы избежать валидационных ошибок у некоторых провайдеров
                        temperature=0.0
                    ),
                    timeout=timeout_sec
                )
                if response and response.choices and response.choices[0].message.content:
                    return model_id, time.monotonic() - start_time
                return model_id, "empty"
        except Exception as e:
            logger.warning(f"Ошибка пинга модели {model_id}: {e}")
            return model_id, "error"


async def run_pings_for_all_active_models() -> Dict[str, Any]:
    """Запускает параллельный пинг всех активных моделей с ограничением конкурентности."""
    import asyncio
    from database.models import AIModel
    
    # Ограничиваем конкурентность до 3 запросов, чтобы не перегружать локальный прокси
    sem = asyncio.Semaphore(3)
    
    models = await AIModel.get_all_active()
    tasks = [_ping_single_model(m, sem) for m in models]
    results = await asyncio.gather(*tasks)
    return dict(results)


def get_model_display_name(model_info: dict) -> str:
    name = model_info["name"]
    name = name.replace(" (maintenance)", "")
    
    speed = model_info.get("speed")
    intel = model_info.get("intelligence")
    is_maint = model_info.get("is_maintenance", False)
    
    prefix = "🛠️ " if is_maint else ""
    
    if speed or intel:
        parts = []
        if speed:
            parts.append(f"⚡ {speed}")
        if intel:
            parts.append(f"🧠 {intel}")
        suffix = f" [{' | '.join(parts)}]"
    else:
        suffix = ""
        
    display_name = f"{prefix}{name}{suffix}"
    if is_maint:
        display_name += " (обслуживание)"
    return display_name


async def _show_model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Инициализация состояния
    if "model_flow" not in context.user_data:
        context.user_data["model_flow"] = {
            "page": 0,
            "query": None,
            "provider": None,
            "speed": None,
            "intelligence": None,
            "menu_message_id": None,
            "menu_mode": "list"
        }
    
    flow = context.user_data["model_flow"]
    
    # Получаем текущую модель
    current_model = await get_chat_model(chat_id)
    current_model_info = await AIModel.get_by_model_id(current_model)
    if current_model_info:
        current_name = get_model_display_name(current_model_info)
    else:
        current_name = current_model

    text = f"🤖 <b>Выбор модели ИИ</b>\n\nТекущая модель: <b>{current_name}</b>\n\n"
    keyboard = []
    
    if flow["menu_mode"] == "list":
        # Получаем список с пагинацией и фильтрами
        models, total = await AIModel.search_and_filter(
            query=flow["query"],
            provider=flow["provider"],
            speed=flow["speed"],
            intelligence=flow["intelligence"],
            limit=5,
            offset=flow["page"] * 5
        )
        
        total_pages = max(1, (total + 4) // 5)
        if flow["page"] >= total_pages:
            flow["page"] = max(0, total_pages - 1)
            models, total = await AIModel.search_and_filter(
                query=flow["query"],
                provider=flow["provider"],
                speed=flow["speed"],
                intelligence=flow["intelligence"],
                limit=5,
                offset=flow["page"] * 5
            )
            total_pages = max(1, (total + 4) // 5)
        
        if not models:
            text += "<i>Модели не найдены по заданным критериям.</i>\n\n"
        else:
            text += f"Страница <b>{flow['page'] + 1}</b> из <b>{total_pages}</b>\n\n"
            pings = flow.get("pings") or {}
            for m in models:
                display_name = get_model_display_name(m)
                
                model_id = m["model"]
                if model_id in pings:
                    ping_val = pings[model_id]
                    if isinstance(ping_val, (int, float)):
                        display_name += f" ({ping_val:.2f}s)"
                    else:
                        display_name += " (❌ ошибка)"
                
                if m["model"] == current_model:
                    display_name = f"✅ {display_name}"
                keyboard.append([InlineKeyboardButton(display_name, callback_data=f"model_choose_{m['key']}")])
        
        # Статус фильтров/поиска
        status_parts = []
        if flow["query"]:
            status_parts.append(f"🔍 Поиск: <code>{flow['query']}</code>")
        if flow["provider"]:
            status_parts.append(f"🏢 Провайдер: <b>{flow['provider']}</b>")
        if flow["speed"]:
            status_parts.append(f"⚡ Скорость: <b>{flow['speed']}</b>")
        if flow["intelligence"]:
            status_parts.append(f"🧠 Интеллект: <b>{flow['intelligence']}</b>")
            
        if status_parts:
            text += "<b>Активные фильтры:</b>\n" + "\n".join(status_parts) + "\n\n"
            
        # Пагинация
        nav_row = []
        if flow["page"] > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data="model_prev"))
        else:
            nav_row.append(InlineKeyboardButton(" ▪️ ", callback_data="model_noop"))
            
        nav_row.append(InlineKeyboardButton(f"{flow['page'] + 1}/{total_pages}", callback_data="model_noop"))
        
        if (flow["page"] + 1) * 5 < total:
            nav_row.append(InlineKeyboardButton("Вперед ➡️", callback_data="model_next"))
        else:
            nav_row.append(InlineKeyboardButton(" ▪️ ", callback_data="model_noop"))
            
        keyboard.append(nav_row)
        
        # Строка управления
        control_row = [
            InlineKeyboardButton("🔍 Поиск", callback_data="model_search"),
            InlineKeyboardButton("🏷️ Фильтры", callback_data="model_filter_menu")
        ]
        keyboard.append(control_row)
        
        # Кнопка пинга
        keyboard.append([InlineKeyboardButton("⚡ Проверить скорость ответов", callback_data="model_ping_check")])
        
        # Сброс
        if flow["query"] or flow["provider"] or flow["speed"] or flow["intelligence"]:
            keyboard.append([InlineKeyboardButton("🧹 Сбросить всё", callback_data="model_fclear")])
            
    elif flow["menu_mode"] == "filters":
        text += "🏷️ <b>Фильтрация моделей</b>\n\nВыберите критерий:"
        keyboard.append([InlineKeyboardButton("🏢 Провайдер", callback_data="model_fmenu_prov")])
        keyboard.append([InlineKeyboardButton("⚡ Скорость", callback_data="model_fmenu_speed")])
        keyboard.append([InlineKeyboardButton("🧠 Интеллект", callback_data="model_fmenu_intel")])
        keyboard.append([InlineKeyboardButton("↩️ Назад к списку", callback_data="model_back")])
        
    elif flow["menu_mode"] == "f_provider":
        text += "🏢 <b>Фильтр по провайдеру</b>\n\nВыберите провайдера:"
        providers = await AIModel.get_unique_providers()
        row = []
        for p in providers:
            label = p
            if flow["provider"] == p:
                label = f"✅ {p}"
            row.append(InlineKeyboardButton(label, callback_data=f"model_fprov_{p}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("↩️ Назад к фильтрам", callback_data="model_filter_menu")])
        
    elif flow["menu_mode"] == "f_speed":
        text += "⚡ <b>Фильтр по скорости</b>\n\nВыберите оценку скорости:"
        speeds = await AIModel.get_unique_speeds()
        row = []
        for s in speeds:
            label = s
            if flow["speed"] == s:
                label = f"✅ {s}"
            row.append(InlineKeyboardButton(label, callback_data=f"model_fspeed_{s}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("↩️ Назад к фильтрам", callback_data="model_filter_menu")])
        
    elif flow["menu_mode"] == "f_intelligence":
        text += "🧠 <b>Фильтр по интеллекту</b>\n\nВыберите оценку интеллекта:"
        intelligences = await AIModel.get_unique_intelligences()
        row = []
        for i in intelligences:
            label = i
            if flow["intelligence"] == i:
                label = f"✅ {i}"
            row.append(InlineKeyboardButton(label, callback_data=f"model_fintel_{i}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("↩️ Назад к фильтрам", callback_data="model_filter_menu")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        try:
            if update.callback_query:
                await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
            elif flow["menu_message_id"]:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=flow["menu_message_id"],
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.warning(f"Ошибка при редактировании меню моделей: {e}")
            # Отправка нового сообщения в крайнем случае
            sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='HTML')
            flow["menu_message_id"] = sent.message_id
    else:
        from config import waiting_for_model_search
        waiting_for_model_search[chat_id].pop(user_id, None)
        sent = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')
        flow["menu_message_id"] = sent.message_id


async def handle_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /model или /models — выбор модели ИИ и проверка пинга (только для админов)."""
    user = update.message.from_user
    chat_id = update.effective_chat.id

    if not await is_responses_enabled(chat_id):
        return

    if not await is_admin(user, chat_id, context):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    args = context.args or []
    query_val = " ".join(args).strip() if args else None

    # Инициализация состояния
    flow = {
        "page": 0,
        "query": query_val,
        "provider": None,
        "speed": None,
        "intelligence": None,
        "menu_message_id": None,
        "menu_mode": "list",
        "pings": {}
    }
    context.user_data["model_flow"] = flow

    # Если вызвана команда /models, сразу запускаем проверку пинга
    run_pings_immediately = False
    if update.message and update.message.text:
        cmd = update.message.text.split()[0].lower()
        if "models" in cmd:
            run_pings_immediately = True

    if run_pings_immediately:
        sent = await update.message.reply_text(
            "🤖 <b>Выбор модели ИИ</b>\n\n⚡ Измеряю скорость ответов моделей, пожалуйста, подождите...",
            parse_mode='HTML'
        )
        flow["menu_message_id"] = sent.message_id
        
        try:
            flow["pings"] = await run_pings_for_all_active_models()
        except Exception as e:
            logger.error(f"Ошибка при автоматическом пинге моделей: {e}")
            
        await _show_model_menu(update, context, edit=True)
    else:
        await _show_model_menu(update, context, edit=False)


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback для выбора модели, пагинации, поиска и фильтрации."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id

    if not await is_admin(user, chat_id, context):
        await query.answer("❌ Только админы могут менять модель.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if "model_flow" not in context.user_data:
        context.user_data["model_flow"] = {
            "page": 0,
            "query": None,
            "provider": None,
            "speed": None,
            "intelligence": None,
            "menu_message_id": query.message.message_id,
            "menu_mode": "list",
            "pings": {}
        }
        
    flow = context.user_data["model_flow"]
    flow["menu_message_id"] = query.message.message_id

    if data == "model_noop":
        return
        
    elif data == "model_ping_check":
        # Сначала редактируем текст сообщения, показывая статус загрузки
        await query.message.edit_text(
            "🤖 <b>Выбор модели ИИ</b>\n\n⚡ Измеряю скорость ответов моделей, пожалуйста, подождите...",
            parse_mode='HTML'
        )
        try:
            flow["pings"] = await run_pings_for_all_active_models()
        except Exception as e:
            logger.error(f"Ошибка при ручном пинге моделей: {e}")
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_prev":
        if flow["page"] > 0:
            flow["page"] -= 1
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_next":
        flow["page"] += 1
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_filter_menu":
        flow["menu_mode"] = "filters"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_fmenu_prov":
        flow["menu_mode"] = "f_provider"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_fmenu_speed":
        flow["menu_mode"] = "f_speed"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_fmenu_intel":
        flow["menu_mode"] = "f_intelligence"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_back":
        flow["menu_mode"] = "list"
        await _show_model_menu(update, context, edit=True)
        
    elif data.startswith("model_fprov_"):
        prov = data.replace("model_fprov_", "")
        if flow["provider"] == prov:
            flow["provider"] = None
        else:
            flow["provider"] = prov
        flow["page"] = 0
        flow["menu_mode"] = "list"
        await _show_model_menu(update, context, edit=True)
        
    elif data.startswith("model_fspeed_"):
        speed = data.replace("model_fspeed_", "")
        if flow["speed"] == speed:
            flow["speed"] = None
        else:
            flow["speed"] = speed
        flow["page"] = 0
        flow["menu_mode"] = "list"
        await _show_model_menu(update, context, edit=True)
        
    elif data.startswith("model_fintel_"):
        intel = data.replace("model_fintel_", "")
        if flow["intelligence"] == intel:
            flow["intelligence"] = None
        else:
            flow["intelligence"] = intel
        flow["page"] = 0
        flow["menu_mode"] = "list"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_fclear":
        flow["query"] = None
        flow["provider"] = None
        flow["speed"] = None
        flow["intelligence"] = None
        flow["page"] = 0
        flow["menu_mode"] = "list"
        await _show_model_menu(update, context, edit=True)
        
    elif data == "model_search":
        from config import waiting_for_model_search
        waiting_for_model_search[chat_id][user.id] = True
        
        cancel_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад к списку", callback_data="model_back")]
        ])
        
        await query.message.edit_text(
            "🔍 <b>Поиск модели ИИ</b>\n\n"
            "<i>настраивает линзы сканера...</i>\n"
            "<blockquote>«Напиши название модели, ключевое слово или имя провайдера, которое ты хочешь найти.»</blockquote>\n\n"
            "💡 <i>Для отмены введите /cancel или нажмите кнопку ниже.</i>",
            reply_markup=cancel_markup,
            parse_mode='HTML'
        )
        
    elif data.startswith("model_choose_"):
        key = data.replace("model_choose_", "")
        selected = await AIModel.get_by_key(key)
        if not selected:
            await query.answer("❌ Неизвестная модель.", show_alert=True)
            return
            
        if selected.get("is_maintenance", False):
            display_name = get_model_display_name(selected)
            await query.answer(
                f"❌ Модель временно недоступна.\n\n"
                f"{display_name} сейчас находится на обслуживании. Пожалуйста, выберите другую модель.",
                show_alert=True
            )
            return
            
        await set_chat_model(chat_id, selected["model"])
        display_name = get_model_display_name(selected)
        await query.answer(f"Модель переключена: {display_name}")
        logger.info(f"Модель в чате {chat_id} переключена на {selected['model']} ({key}) пользователем {user.id}")
        await _show_model_menu(update, context, edit=True)




# ============================================================================
# /dub (Дубляж видео по URL через videotrans)
# ============================================================================

import os as _os
import html as _html
from pathlib import Path as _Path
from ai.dubbing import is_supported_url


DUB_SUBS_OPTIONS = ["✅ Да, с субтитрами", "❌ Без субтитров"]

# Допустимые расширения файлов
_DUB_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
_DUB_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}
_DUB_ALLOWED_EXTS = _DUB_VIDEO_EXTS | _DUB_AUDIO_EXTS


def classify_dub_media(file_name: str | None, mime_type: str | None) -> str | None:
    """Определяет тип медиа: 'video', 'audio' или None если не подходит.

    Голосовые сообщения сюда не попадают (мы их не передаём в этот хендлер).
    """
    name = (file_name or "").lower()
    suffix = _Path(name).suffix
    mime = (mime_type or "").lower()

    if suffix in _DUB_VIDEO_EXTS or mime.startswith("video/"):
        return "video"
    if suffix in _DUB_AUDIO_EXTS or mime.startswith("audio/"):
        return "audio"
    return None


def _dub_subs_keyboard() -> telegram.ReplyKeyboardMarkup:
    return telegram.ReplyKeyboardMarkup(
        [[telegram.KeyboardButton(option)] for option in DUB_SUBS_OPTIONS],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


async def _enqueue_dub_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    url: str = "",
    input_file: str | None = None,
    with_subs: bool = False,
    audio_only: bool = False,
):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_name = user.first_name or user.username or "Пользователь"
    run_id = f"{chat_id}_{update.message.message_id}_{_os.urandom(3).hex()}"

    task = {
        'type': 'dubbing',
        'chat_id': chat_id,
        'url': url,
        'input_file': input_file,
        'audio_only': audio_only,
        'context': context,
        'message_id': update.message.message_id,
        'user_name': user_name,
        'with_subs': with_subs,
        'run_id': run_id,
    }
    await enqueue_dubbing(task, context.bot, chat_id)
    logger.info(
        f"Дубляж поставлен в очередь: chat={chat_id}, user={user.id}, "
        f"url={url!r}, input_file={input_file!r}, "
        f"with_subs={with_subs}, audio_only={audio_only}"
    )


async def handle_dub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dub [url] [subs] — озвучка видео/аудио.
    Без аргументов — пошаговый диалог: ссылка ИЛИ файл (mp4/mp3/...).
    """
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id

    if rp_mode_state.get(chat_id):
        await update.message.reply_text(
            "<blockquote>«В RP-режиме дубляж видео недоступен.»</blockquote>",
            parse_mode='HTML'
        )
        return

    if not await is_responses_enabled(chat_id):
        return

    if not await is_admin(user, chat_id, context):
        await update.message.reply_text(
            "❌ Команда /dub доступна только админам — пайплайн долгий и тяжёлый."
        )
        return

    if not await handle_spam_protection(update, context, "dub"):
        return

    args = context.args or []

    # Шорткат: /dub <url> [subs]
    if args:
        url = args[0].strip()
        if not is_supported_url(url):
            await update.message.reply_text(
                "❌ URL должен начинаться с http:// или https://"
            )
            return
        with_subs = any(a.lower() in ("subs", "sub", "+subs", "сабы", "субтитры") for a in args[1:])
        await _enqueue_dub_task(update, context, url=url, with_subs=with_subs, audio_only=False)
        return

    # Пошаговый диалог
    dub_flow_state[chat_id][user_id] = {
        "step": "source",
        "message_id": update.message.message_id,
    }
    await update.message.reply_text(
        "<i>Поднимает взгляд от терминала, пальцы зависают над клавишей</i>\n\n"
        "<blockquote>«Хочешь, чтобы я переозвучила что-нибудь? Кидай ссылку (YouTube или прямую) "
        "или прикрепи файл — видео (<code>.mp4</code>, <code>.mkv</code>, <code>.webm</code>) "
        "или аудио (<code>.mp3</code>, <code>.m4a</code>, <code>.wav</code>).»</blockquote>\n\n"
        "<i>Голосовые сообщения не принимаю. Для отмены — /cancel</i>",
        parse_mode='HTML'
    )


async def handle_dub_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    media_kind: str,
    file_name: str | None = None,
) -> bool:
    """
    Обрабатывает прикреплённое медиа в режиме /dub flow.
    Скачивает файл во временную папку и продолжает flow.
    """
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    state = dub_flow_state.get(chat_id, {}).get(user_id)
    if not state or state.get("step") != "source":
        return False

    if media_kind not in ("video", "audio"):
        await update.message.reply_text(
            "<blockquote>«Этот файл не подходит. Нужен видео- или аудиофайл.»</blockquote>",
            parse_mode='HTML'
        )
        return True

    # Скачиваем файл в temp/
    try:
        tg_file = await context.bot.get_file(file_id)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Не удалось получить файл: <code>{_html.escape(str(exc))}</code>",
            parse_mode='HTML'
        )
        dub_flow_state[chat_id].pop(user_id, None)
        return True

    file_size = tg_file.file_size or 0
    # Telegram bot API позволяет скачивать до 20 МБ. Файл больше — отказываем.
    if file_size and file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "<i>Качает головой</i>\n"
            "<blockquote>«Файл больше 20 МБ — Telegram бот-API такое не отдаёт. "
            "Залей куда-нибудь и пришли ссылку.»</blockquote>",
            parse_mode='HTML'
        )
        dub_flow_state[chat_id].pop(user_id, None)
        return True

    safe_name = file_name or f"input_{_os.urandom(3).hex()}"
    suffix = _Path(safe_name).suffix.lower()
    if suffix not in _DUB_ALLOWED_EXTS:
        # Подстраховка: если расширение пустое или странное — выберем по типу
        suffix = ".mp4" if media_kind == "video" else ".mp3"

    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_path = temp_dir / f"dub_input_{chat_id}_{update.message.message_id}_{_os.urandom(3).hex()}{suffix}"

    try:
        await tg_file.download_to_drive(custom_path=str(local_path))
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Не удалось скачать файл: <code>{_html.escape(str(exc))}</code>",
            parse_mode='HTML'
        )
        dub_flow_state[chat_id].pop(user_id, None)
        return True

    state["input_file"] = str(local_path)
    state["audio_only"] = (media_kind == "audio")

    if media_kind == "audio":
        # Для аудио сабы не предлагаем — сразу запускаем
        dub_flow_state[chat_id].pop(user_id, None)
        await update.message.reply_text(
            "<i>Касается банта</i>\n\n"
            "<blockquote>«Аудио принято. Субтитры тут ни к чему — переозвучиваю как есть.»</blockquote>",
            parse_mode='HTML'
        )
        await _enqueue_dub_task(
            update, context,
            input_file=str(local_path),
            with_subs=False,
            audio_only=True,
        )
        return True

    # Видео — спрашиваем про сабы
    state["step"] = "subs"
    await update.message.reply_text(
        "<i>Касается банта</i>\n\n"
        "<blockquote>«Видео принято. Последний вопрос — нужны субтитры? "
        "Я могу вшить русские прямо в кадр.»</blockquote>",
        reply_markup=_dub_subs_keyboard(),
        parse_mode='HTML'
    )
    return True


async def handle_dub_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает текстовый шаг /dub flow. Возвращает True, если поглотил сообщение."""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    state = dub_flow_state.get(chat_id, {}).get(user_id)
    if not state:
        return False

    text = (update.message.text or "").strip()
    step = state.get("step")

    if step == "source":
        # Это шаг ожидания ссылки или файла. Если пришёл текст — пробуем как URL.
        if not text:
            await update.message.reply_text(
                "<blockquote>«Жду ссылку текстом или прикреплённый файл. Или /cancel.»</blockquote>",
                parse_mode='HTML'
            )
            return True
        if not is_supported_url(text):
            await update.message.reply_text(
                "<i>Прищуривается</i>\n"
                "<blockquote>«Это не похоже на ссылку. Должно начинаться с <code>http://</code> или <code>https://</code>. "
                "Либо прикрепи файл (<code>.mp4</code>, <code>.mp3</code>...).»</blockquote>",
                parse_mode='HTML'
            )
            return True

        state["url"] = text
        state["audio_only"] = False
        state["step"] = "subs"
        await update.message.reply_text(
            "<i>Касается банта</i>\n\n"
            "<blockquote>«Принято. Последний вопрос — нужны субтитры? "
            "Я могу вшить русские прямо в кадр.»</blockquote>",
            reply_markup=_dub_subs_keyboard(),
            parse_mode='HTML'
        )
        return True

    if step == "subs":
        text_lower = text.lower()
        positive_markers = ("да", "yes", "✅", "субт", "сабы", "with subs", "нужны")
        negative_markers = ("нет", "no", "❌", "без", "не нужно", "without")

        if any(m in text_lower for m in positive_markers):
            with_subs = True
        elif any(m in text_lower for m in negative_markers):
            with_subs = False
        else:
            await update.message.reply_text(
                "<blockquote>«Кнопкой ниже, пожалуйста.»</blockquote>",
                reply_markup=_dub_subs_keyboard(),
                parse_mode='HTML'
            )
            return True

        url = state.get("url", "") or ""
        input_file = state.get("input_file")
        audio_only = bool(state.get("audio_only", False))
        dub_flow_state[chat_id].pop(user_id, None)

        if not url and not input_file:
            await update.message.reply_text(
                "❌ Внутренняя ошибка: источник потерялся. Попробуй /dub ещё раз.",
                reply_markup=telegram.ReplyKeyboardRemove(),
            )
            return True

        await update.message.reply_text(
            "<i>Запускает пайплайн</i>",
            reply_markup=telegram.ReplyKeyboardRemove(),
            parse_mode='HTML'
        )
        await _enqueue_dub_task(
            update, context,
            url=url,
            input_file=input_file,
            with_subs=with_subs,
            audio_only=audio_only,
        )
        return True

    return False


# ============================================================================
# /vclone (alias /steal) — клонирование голоса по референсу
# ============================================================================

import time as _time
import asyncio as _asyncio
import hashlib as _hashlib
from ai.voice_clone import (
    extract_reference,
    validate_reference,
    run_separator,
    cleanup_vclone_files,
)
from ai.catbox import upload_file as catbox_upload_file, delete_file as catbox_delete_file, download_file as catbox_download_file
from ai.video_url import find_first_url, is_known_video_url, download_audio_for_url
from bot.queue import enqueue_vclone
from utils.text_processing import repeat_chat_action as _vclone_repeat_chat_action


# Разрешённые расширения для document с аудио/видео-контентом — на случай если
# Telegram отдал файл как `document` без mime_type.
_VCLONE_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}
_VCLONE_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}


# Аудит-логгер /vclone (см. design.md::Audit Log, requirements 11.3-11.5).
# Пишет в `logs/bot.log` через корневой handler. Парный логгер в bot/queue.py
# выводит финальную точку "done ..." после отправки/ошибки в воркере.
_vclone_audit_logger = logging.getLogger("vclone.audit")


def _vclone_log_start(
    chat_id: int,
    user: telegram.User | None,
    source_kind: str,
    ref_path: _Path | str,
) -> None:
    """Аудит-лог точки старта /vclone после успешного extract_reference.

    Хеш — sha256 первых 32 КБ Reference_Audio (для возможной идентификации
    источника без хранения полного содержимого, согласно дизайн-доке Audit Log).
    Любые ошибки (отсутствие файла, ошибка чтения) поглощаются — аудит
    best-effort и не должен ломать основной флоу.
    """
    try:
        sha = _hashlib.sha256()
        with open(str(ref_path), "rb") as f:
            sha.update(f.read(32 * 1024))
        ref_hash = sha.hexdigest()
    except Exception:
        ref_hash = "unknown"

    if user is not None:
        user_id = user.id
        user_name = user.first_name or user.username or "Пользователь"
    else:
        user_id = 0
        user_name = "Пользователь"

    _vclone_audit_logger.info(
        "start chat=%s user=%s name=%s source=%s ref_sha256=%s",
        chat_id, user_id, user_name, source_kind, ref_hash,
    )


def _vclone_is_valid_caption(caption: str) -> bool:
    """Проверяет, является ли caption валидным текстом для озвучки.

    Возвращает True если:
    - Длина > 2 символа
    - Содержит буквы (кириллица или латиница)
    - Не состоит только из точек, запятых, пробелов и спецсимволов

    Args:
        caption: Текст caption из reply на медиа.

    Returns:
        True если caption похож на валидный текст для озвучки.
    """
    if not caption or len(caption.strip()) < 3:
        return False

    # Проверяем что есть буквы
    has_letters = any(c.isalpha() for c in caption)
    if not has_letters:
        return False

    # Проверяем что не только точки, запятые, пробелы
    stripped = caption.strip()
    non_punctuation = [c for c in stripped if c not in ".,!?;:·•–—_ "]
    if len(non_punctuation) < 2:
        return False

    return True


def _vclone_cleanup_keyboard() -> telegram.InlineKeyboardMarkup:
    """Inline-клавиатура для шага cleanup_choice."""
    return telegram.InlineKeyboardMarkup([
        [
            telegram.InlineKeyboardButton("✨ Очистить звук", callback_data="vclone_clean:1"),
            telegram.InlineKeyboardButton("⏩ Оставить как есть", callback_data="vclone_clean:0"),
        ]
    ])


def _vclone_save_keyboard() -> telegram.InlineKeyboardMarkup:
    return telegram.InlineKeyboardMarkup([
        [
            telegram.InlineKeyboardButton("💾 Сохранить голос", callback_data="vsave:yes"),
            telegram.InlineKeyboardButton("⏭ Не сохранять", callback_data="vsave:no"),
        ]
    ])


def _saved_voice_keyboard(voices: list[dict]) -> telegram.InlineKeyboardMarkup:
    rows = []
    for voice in voices:
        voice_id = voice["id"]
        name = str(voice["name"])
        rows.append([
            telegram.InlineKeyboardButton(f"🎙 {name}", callback_data=f"vsel:{voice_id}"),
            telegram.InlineKeyboardButton("🗑", callback_data=f"vdel:{voice_id}"),
        ])
    return telegram.InlineKeyboardMarkup(rows)


def _vclone_build_task(
    *,
    chat_id: int,
    user_id: int,
    user_name: str,
    message_id: int | None,
    reference_path: str,
    synthesis_text: str,
    cleaned_path: str | None,
    cleaned: bool,
    source_kind: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> dict:
    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "message_id": message_id,
        "reference_path": reference_path,
        "synthesis_text": synthesis_text,
        "cleaned_path": cleaned_path,
        "cleaned": cleaned,
        "source_kind": source_kind,
        "context": context,
        "started_at": _time.time(),
    }


def _vclone_voice_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    name = name.strip(" «»\"'`")
    name = re.sub(r"[^\wа-яА-ЯёЁ ._-]+", "", name, flags=re.UNICODE).strip(" ._-")
    return name[:40].strip()


def _vclone_cleanup_save_state(chat_id: int, user_id: int) -> None:
    state = vclone_save_flow_state.get(chat_id, {}).pop(user_id, None)
    if not isinstance(state, dict):
        return
    cleanup_paths = state.get("cleanup_paths") or []
    if not cleanup_paths and state.get("reference_path"):
        cleanup_paths = [state.get("reference_path")]
    cleanup_vclone_files(*cleanup_paths)


async def _vclone_offer_save_copy(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    reference_path: str,
    source_kind: str,
    cleaned: bool,
    reply_to_message_id: int | None,
) -> None:
    import shutil as _shutil
    import uuid as _uuid

    src = _Path(reference_path)
    if not src.exists():
        return

    _vclone_cleanup_save_state(chat_id, user_id)
    dst = src.with_name(f"vclone_save_{user_id}_{_uuid.uuid4().hex[:8]}.wav")
    try:
        await _asyncio.to_thread(_shutil.copy2, src, dst)
    except Exception as exc:
        logger.warning("vclone save: не удалось скопировать reference для сохранения: %s", exc)
        return

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "💾 <b>Сохранить этот голос?</b>\n\n"
            "Сохраню референс в твою библиотеку, чтобы потом выбрать его через /voices."
        ),
        reply_to_message_id=reply_to_message_id,
        reply_markup=_vclone_save_keyboard(),
        parse_mode='HTML',
    )
    vclone_save_flow_state[chat_id][user_id] = {
        "step": "offer",
        "reference_path": str(dst),
        "cleanup_paths": [str(dst)],
        "source_kind": source_kind,
        "cleaned": cleaned,
        "duration_sec": None,
        "bot_message_id": sent.message_id,
        "created_at": _time.time(),
    }


async def _vclone_prompt_save_name(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    reference_path: str,
    source_kind: str,
    cleaned: bool,
    cleanup_paths: list[str] | None = None,
    bot_message_id: int | None = None,
    suggested_name: str | None = None,
) -> None:
    vclone_save_flow_state[chat_id][user_id] = {
        "step": "name",
        "reference_path": reference_path,
        "cleanup_paths": cleanup_paths or [reference_path],
        "source_kind": source_kind,
        "cleaned": cleaned,
        "duration_sec": None,
        "suggested_name": suggested_name,
        "bot_message_id": bot_message_id,
        "created_at": _time.time(),
    }
    text = (
        "🏷 <b>Имя голоса</b>\n\n"
        "Напиши короткое имя для этого голоса."
    )
    if suggested_name:
        text += f"\n\nПодсказка: <code>{_html.escape(suggested_name)}</code>"

    if bot_message_id:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=bot_message_id,
                parse_mode='HTML',
            )
            return
        except Exception:
            pass

    sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    vclone_save_flow_state[chat_id][user_id]["bot_message_id"] = sent.message_id


async def _vclone_caption_fastpath(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Обрабатывает /vclone или /steal в caption медиа-сообщения.

    Когда пользователь отправляет голосовое/аудио/видео с caption
    вроде ``/vclone текст для озвучки``, ``CommandHandler`` не срабатывает
    (у медиа-сообщений нет ``text``, только ``caption``). Эта функция
    вызывается из медиа-хендлеров и выполняет fast-path:
    проверка → скачивание → extract → validate → cleanup_choice.

    Returns:
        True — сообщение обработано (vclone запущен или отказ).
        False — caption не содержит /vclone или /steal.
    """
    caption = (update.message.caption or "").strip()
    if not caption:
        return False

    # Проверяем, начинается ли caption с /vclone или /steal
    parts = caption.split(None, 1)
    first_word = parts[0].lower()

    # Убираем @botname если есть
    if '@' in first_word:
        first_word = first_word.split('@', 1)[0]

    if first_word not in ("/vclone", "/steal"):
        return False

    chat_id = update.effective_chat.id
    user = update.message.from_user

    # 1. RP-режим — отказ.
    if rp_mode_state.get(chat_id):
        await update.message.reply_text(
            "<blockquote>«В RP-режиме голоса не краду.»</blockquote>",
            parse_mode='HTML',
        )
        return True

    # 2. Бот выключен — тихо завершаем.
    if not await is_responses_enabled(chat_id):
        return True

    # 3. Спам-защита.
    if not await handle_spam_protection(update, context, "vclone"):
        return True

    # 4. Парсинг аргументов из caption.
    synthesis_text = parts[1].strip() if len(parts) > 1 else None
    if synthesis_text and not _vclone_is_valid_caption(synthesis_text):
        synthesis_text = None

    # 5. Определяем медиа на сообщении.
    source_kind, file_id, file_size, file_name = _vclone_classify_reply_media(update.message)
    if source_kind is None or not file_id:
        # Медиа не подходит для vclone — не перехватываем
        return False

    # Меняем префикс source_kind с "reply_" на "caption_"
    if source_kind.startswith("reply_"):
        source_kind = "caption_" + source_kind[len("reply_"):]

    # 6. Fast-path: размер → скачивание → extract → validate → cleanup_choice.
    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    if file_size and file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "❌ <b>Файл слишком большой</b>\n\n"
            "Отправь ссылку на файл.",
            parse_mode='HTML',
        )
        return True

    try:
        tg_file = await context.bot.get_file(file_id)
    except Exception as exc:
        await update.message.reply_text(
            f"<blockquote>«Не удалось скачать файл: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
            parse_mode='HTML',
        )
        return True

    try:
        ref_wav = await extract_reference(tg_file, temp_dir)
    except Exception as exc:
        await update.message.reply_text(
            f"<blockquote>«Не удалось извлечь голос: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
            parse_mode='HTML',
        )
        return True

    _vclone_log_start(chat_id, user, source_kind, ref_wav)

    ok, reason, final_path = await validate_reference(ref_wav)
    if not ok:
        await _vclone_send_validation_refusal(update, reason)
        cleanup_vclone_files(ref_wav, final_path)
        return True

    await _vclone_setup_cleanup_choice(
        update, context,
        reference_path=final_path,
        synthesis_text=synthesis_text,
        source_kind=source_kind,
    )
    return True


def _vclone_classify_reply_media(reply_msg) -> tuple[str | None, str | None, int | None, str | None]:
    """Определяет медиа-источник в reply-сообщении.

    Returns:
        Кортеж ``(source_kind, file_id, file_size, file_name)``.
        Если медиа не обнаружено — все элементы ``None``.
    """
    if reply_msg is None:
        return (None, None, None, None)

    # voice (Opus)
    if reply_msg.voice is not None:
        v = reply_msg.voice
        return ("reply_voice", v.file_id, v.file_size, None)

    # audio (произвольный аудиофайл)
    if reply_msg.audio is not None:
        a = reply_msg.audio
        return ("reply_audio", a.file_id, a.file_size, a.file_name)

    # video_note (кружочек) — проверяем до video, потому что некоторые SDK
    # выставляют оба атрибута; но обычно video_note исключителен.
    if getattr(reply_msg, "video_note", None) is not None:
        vn = reply_msg.video_note
        return ("reply_video_note", vn.file_id, vn.file_size, None)

    # video
    if reply_msg.video is not None:
        v = reply_msg.video
        return ("reply_video", v.file_id, v.file_size, v.file_name)

    # document с MIME audio/* или video/*, либо подходящим расширением
    if reply_msg.document is not None:
        d = reply_msg.document
        mime = (d.mime_type or "").lower()
        name = (d.file_name or "").lower()
        suffix = _Path(name).suffix
        if mime.startswith("audio/") or suffix in _VCLONE_AUDIO_EXTS:
            return ("reply_doc_audio", d.file_id, d.file_size, d.file_name)
        if mime.startswith("video/") or suffix in _VCLONE_VIDEO_EXTS:
            return ("reply_doc_video", d.file_id, d.file_size, d.file_name)

    return (None, None, None, None)


async def _vclone_send_validation_refusal(update: Update, reason: str) -> None:
    """Отправляет короткий отказ Bot_Persona_Reply по результату validate_reference."""
    if reason == "refused_short_ref":
        text = "<blockquote>«Слишком короткий сэмпл. Минимум пять секунд.»</blockquote>"
    elif reason == "refused_long_ref":
        text = "<blockquote>«Аудиозапись слишком длинная. Пожалуйста, используйте запись короче 10 минут.»</blockquote>"
    elif reason == "refused_silent":
        text = "<blockquote>«Здесь только тишина. Не из чего красть.»</blockquote>"
    else:
        text = f"<blockquote>«Не подошло: {_html.escape(reason)}.»</blockquote>"
    await update.message.reply_text(text, parse_mode='HTML')


async def _vclone_setup_cleanup_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reference_path: _Path,
    synthesis_text: str | None,
    source_kind: str,
    save_only: bool = False,
    suggested_name: str | None = None,
) -> None:
    """Сохраняет state в FSM (step="cleanup_choice") и отправляет inline-клавиатуру."""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id

    sent = await update.message.reply_text(
        "🧹 <b>Очистка голоса</b>\n\n"
        "Чистить от шума или брать как есть?",
        reply_markup=_vclone_cleanup_keyboard(),
        parse_mode='HTML',
    )

    vclone_flow_state[chat_id][user_id] = {
        "step": "cleanup_choice",
        "reference_path": str(reference_path),
        "cleaned_path": None,
        "synthesis_text": synthesis_text,
        "source_kind": source_kind,
        "save_only": save_only,
        "suggested_name": suggested_name,
        "message_id": update.message.message_id,
        "created_at": _time.time(),
        "bot_message_id": sent.message_id,
    }


async def handle_vclone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /vclone [текст] — клонирует голос по референсу и озвучивает текст.

    Поведение:
    - reply на голосовое/аудио/видео/video_note/document(audio|video) → fast-path:
      скачивает медиа (≤20 МБ), извлекает референс, валидирует, переходит в
      step="cleanup_choice" с inline-кнопками выбора чистки.
    - reply на текст с URL поддерживаемого видеохостинга — то же, но через
      download_audio_for_url.
    - reply без медиа/URL — Bot_Persona_Reply «нужен голос», без FSM.
    - без reply — step="reference", prompt-сообщение.

    Доступна всем, если бот включён и не в RP-режиме.
    """
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id

    # 1. RP-режим — отказ.
    if rp_mode_state.get(chat_id):
        await update.message.reply_text(
            "<blockquote>«В RP-режиме голоса не краду.»</blockquote>",
            parse_mode='HTML',
        )
        return

    # 2. Бот выключен — тихо завершаем.
    if not await is_responses_enabled(chat_id):
        return

    # 3. Спам-защита.
    if not await handle_spam_protection(update, context, "vclone"):
        return

    # 5. Парсинг аргументов: всё после команды — потенциальный synthesis_text.
    args = context.args or []
    synthesis_text: str | None = " ".join(args).strip() or None

    reply_msg = update.message.reply_to_message
    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 5a. Выбираем сообщение с медиа: само сообщение (если есть медиа) ИЛИ reply
    self_source, _, _, _ = _vclone_classify_reply_media(update.message)
    media_msg = update.message if self_source is not None else reply_msg

    # 5b. Если используем reply_msg, и synthesis_text пуст, пробуем вытащить текст из caption
    if media_msg == reply_msg and media_msg is not None and synthesis_text is None:
        source_kind_check, _, _, _ = _vclone_classify_reply_media(reply_msg)
        if source_kind_check is not None:
            reply_caption = reply_msg.caption or ""
            if reply_caption and _vclone_is_valid_caption(reply_caption):
                synthesis_text = reply_caption

    # 6. Fast-path: медиа прикреплено к команде ИЛИ reply на медиа.
    if media_msg is not None:
        source_kind, file_id, file_size, file_name = _vclone_classify_reply_media(media_msg)
        logger.info("vclone: media check: source_kind=%s, file_id=%s, file_size=%s", source_kind, file_id, file_size)

        if source_kind is not None and file_id:
            # 6a. Проверка размера (Telegram bot API: 20 МБ).
            if file_size and file_size > 20 * 1024 * 1024:
                await update.message.reply_text(
                    "❌ <b>Файл слишком большой</b>\n\n"
                    "Отправь ссылку на файл.",
                    parse_mode='HTML',
                )
                return

            # 6b. Скачивание + extract + validate.
            try:
                tg_file = await context.bot.get_file(file_id)
            except Exception as exc:
                await update.message.reply_text(
                    f"<blockquote>«Не удалось скачать файл: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                    parse_mode='HTML',
                )
                return

            try:
                ref_wav = await extract_reference(tg_file, temp_dir)
            except Exception as exc:
                await update.message.reply_text(
                    f"<blockquote>«Не удалось извлечь голос: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                    parse_mode='HTML',
                )
                return

            _vclone_log_start(chat_id, user, source_kind, ref_wav)

            ok, reason, final_path = await validate_reference(ref_wav)
            if not ok:
                await _vclone_send_validation_refusal(update, reason)
                # Чистим оба возможных файла (исходный и trim, если был).
                cleanup_vclone_files(ref_wav, final_path)
                return

            # 6c. FSM cleanup_choice + inline-клавиатура.
            await _vclone_setup_cleanup_choice(
                update, context,
                reference_path=final_path,
                synthesis_text=synthesis_text,
                source_kind=source_kind,
            )
            return

        # 6d. reply без медиа — пробуем найти URL в тексте/caption.
        reply_text = reply_msg.text or reply_msg.caption or ""
        url = find_first_url(reply_text)
        if url and (is_known_video_url(url) or url.lower().startswith(("http://", "https://"))):
            # Скачиваем аудиодорожку и проходим тот же путь.
            try:
                ref_wav = await extract_reference(url, temp_dir)
            except Exception as exc:
                await update.message.reply_text(
                    f"<blockquote>«Не удалось скачать голос по ссылке: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                    parse_mode='HTML',
                )
                return

            _vclone_log_start(chat_id, user, "reply_url", ref_wav)

            ok, reason, final_path = await validate_reference(ref_wav)
            if not ok:
                await _vclone_send_validation_refusal(update, reason)
                cleanup_vclone_files(ref_wav, final_path)
                return

            await _vclone_setup_cleanup_choice(
                update, context,
                reference_path=final_path,
                synthesis_text=synthesis_text,
                source_kind="reply_url",
            )
            return

        # 6e. reply без поддерживаемого медиа и без URL — отказ без FSM.
        await update.message.reply_text(
            "❌ <b>Нужен голос</b>\n\n"
            "Отправь голосовое, аудио, видео или ссылку.",
            parse_mode='HTML',
        )
        return

    # 7. Нет reply — пошаговый сбор референса.
    sent = await update.message.reply_text(
        "🎙 <b>Голосовой клон: шаг 1/2</b>\n\n"
        "Отправь голосовое, аудио, видео или ссылку.\n\n"
        "💡 <i>Для отмены введи /cancel</i>",
        parse_mode='HTML',
    )
    vclone_flow_state[chat_id][user_id] = {
        "step": "reference",
        "reference_path": None,
        "cleaned_path": None,
        "synthesis_text": synthesis_text,
        "source_kind": "stepwise",
        "message_id": update.message.message_id,
        "created_at": _time.time(),
        "bot_message_id": sent.message_id,
    }


def classify_vclone_media(file_name: str | None, mime_type: str | None) -> str | None:
    """Определяет тип медиа: 'video', 'audio' или None если не подходит.

    Чистая функция, используется в bot/handlers.py при роутинге document-вложений
    в активный vclone FSM (по аналогии с classify_dub_media).
    """
    name = (file_name or "").lower()
    suffix = _Path(name).suffix
    mime = (mime_type or "").lower()

    if suffix in _VCLONE_VIDEO_EXTS or mime.startswith("video/"):
        return "video"
    if suffix in _VCLONE_AUDIO_EXTS or mime.startswith("audio/"):
        return "audio"
    return None


async def handle_vclone_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    file_size: int | None,
    file_name: str | None = None,
    source_kind: str = "stepwise",
) -> bool:
    """Обрабатывает медиа-вложение в активном vclone FSM (step="reference").

    Универсальный обработчик для voice/audio/video/video_note/document(audio|video):
    проверяет размер, извлекает референс через ffmpeg, валидирует, переводит FSM
    в step="cleanup_choice" и показывает inline-клавиатуру.

    Returns:
        True — медиа поглощено этим хендлером (FSM активен и сообщение обработано).
        False — FSM не активен или шаг не подходит, вызывающий код должен продолжить
        обычную обработку.
    """
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    state = vclone_flow_state.get(chat_id, {}).get(user_id)
    if not state or state.get("step") != "reference":
        return False

    # 1. Telegram bot API лимит на скачивание — 20 МБ.
    if file_size and file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "❌ <b>Файл слишком большой</b>\n\n"
            "Отправь ссылку на файл.",
            parse_mode='HTML',
        )
        # FSM не очищаем — пользователь может прислать другой файл или ссылку.
        return True

    # 2. Получаем Telegram File handle.
    try:
        tg_file = await context.bot.get_file(file_id)
    except Exception as exc:
        await update.message.reply_text(
            f"<blockquote>«Не удалось скачать файл: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
            parse_mode='HTML',
        )
        return True

    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 3. ffmpeg → mono WAV 24 kHz.
    try:
        ref_wav = await extract_reference(tg_file, temp_dir)
    except Exception as exc:
        await update.message.reply_text(
            f"<blockquote>«Не удалось извлечь голос: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
            parse_mode='HTML',
        )
        return True

    _vclone_log_start(chat_id, update.message.from_user, source_kind, ref_wav)

    # 4. Валидация длительности + тишины (с возможным трим-проходом до 15с).
    ok, reason, final_path = await validate_reference(ref_wav)
    if not ok:
        await _vclone_send_validation_refusal(update, reason)
        cleanup_vclone_files(ref_wav, final_path)
        # FSM оставляем активным — юзер может попробовать другой сэмпл.
        return True

    # 5. Перевод FSM в cleanup_choice.
    existing_synthesis = state.get("synthesis_text")
    existing_message_id = state.get("message_id") or update.message.message_id

    sent = await update.message.reply_text(
        "🧹 <b>Очистка голоса</b>\n\n"
        "Чистить от шума или брать как есть?",
        reply_markup=_vclone_cleanup_keyboard(),
        parse_mode='HTML',
    )

    vclone_flow_state[chat_id][user_id] = {
        "step": "cleanup_choice",
        "reference_path": str(final_path),
        "cleaned_path": None,
        "synthesis_text": existing_synthesis,
        "source_kind": source_kind,
        "save_only": bool(state.get("save_only", False)),
        "suggested_name": state.get("suggested_name"),
        "message_id": existing_message_id,
        "created_at": _time.time(),
        "bot_message_id": sent.message_id,
    }
    return True


async def handle_vclone_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает текстовый шаг /vclone flow. Возвращает True, если поглотил сообщение.

    Поведение по образцу `handle_dub_flow`:
    - step="reference": текст с URL → extract_reference(url) + validate → cleanup_choice.
      Иначе — Bot_Persona_Reply про допустимые типы, остаёмся в reference.
    - step="text": сохраняем synthesis_text, кладём задачу в vclone_queue, чистим FSM.
    - step="cleanup_choice" / прочие: возвращаем False — текстовые сообщения не поглощаем
      (cleanup_choice обрабатывается callback'ом vclone_clean_callback).
    """
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    state = vclone_flow_state.get(chat_id, {}).get(user_id)
    if not state:
        return False

    text = (update.message.text or "").strip()
    step = state.get("step")

    if step == "reference":
        # Ждём либо медиа (его поймает handle_vclone_attachment в handlers.py),
        # либо URL текстом, либо reply на сообщение с медиа.

        # --- Fast-path: reply на медиа-сообщение из FSM ---
        reply_msg = update.message.reply_to_message
        if reply_msg is not None:
            source_kind, file_id, file_size, file_name = _vclone_classify_reply_media(reply_msg)
            if source_kind is not None and file_id:
                # Текст пользователя используем как synthesis_text только если он осмысленный
                reply_synthesis = text if _vclone_is_valid_caption(text) else None

                if file_size and file_size > 20 * 1024 * 1024:
                    await update.message.reply_text(
                        "❌ <b>Файл слишком большой</b>\n\n"
                        "Отправь ссылку на файл.",
                        parse_mode='HTML',
                    )
                    return True

                try:
                    tg_file = await context.bot.get_file(file_id)
                except Exception as exc:
                    await update.message.reply_text(
                        f"<blockquote>«Не удалось скачать файл: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                        parse_mode='HTML',
                    )
                    return True

                temp_dir = _Path("temp")
                temp_dir.mkdir(parents=True, exist_ok=True)

                try:
                    ref_wav = await extract_reference(tg_file, temp_dir)
                except Exception as exc:
                    await update.message.reply_text(
                        f"<blockquote>«Не удалось извлечь голос: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                        parse_mode='HTML',
                    )
                    return True

                _vclone_log_start(chat_id, update.message.from_user, f"stepwise_reply_{source_kind}", ref_wav)

                ok, reason, final_path = await validate_reference(ref_wav)
                if not ok:
                    await _vclone_send_validation_refusal(update, reason)
                    cleanup_vclone_files(ref_wav, final_path)
                    return True

                # Если в state уже был synthesis_text (из аргументов /vclone), приоритет у него
                existing_synthesis = state.get("synthesis_text") or reply_synthesis
                existing_message_id = state.get("message_id") or update.message.message_id

                sent = await update.message.reply_text(
                    "🧹 <b>Очистка голоса</b>\n\n"
                    "Чистить от шума или брать как есть?",
                    reply_markup=_vclone_cleanup_keyboard(),
                    parse_mode='HTML',
                )

                vclone_flow_state[chat_id][user_id] = {
                    "step": "cleanup_choice",
                    "reference_path": str(final_path),
                    "cleaned_path": None,
                    "synthesis_text": existing_synthesis,
                    "source_kind": f"stepwise_reply_{source_kind}",
                    "save_only": bool(state.get("save_only", False)),
                    "suggested_name": state.get("suggested_name"),
                    "message_id": existing_message_id,
                    "created_at": _time.time(),
                    "bot_message_id": sent.message_id,
                }
                return True

        if not text:
            await update.message.reply_text(
                "❌ <b>Нужен голос</b>\n\n"
                "Отправь голосовое, аудио, видео или ссылку.",
                parse_mode='HTML',
            )
            return True

        if not text.lower().startswith(("http://", "https://")):
            # Не URL — напоминаем про допустимые типы, FSM не трогаем.
            await update.message.reply_text(
                "❌ <b>Нужен голос</b>\n\n"
                "Отправь голосовое, аудио, видео или ссылку (http:// / https://).",
                parse_mode='HTML',
            )
            return True

        # URL: extract_reference сам качает через download_audio_for_url, конвертит в WAV.
        temp_dir = _Path("temp")
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            ref_wav = await extract_reference(text, temp_dir)
        except Exception as exc:
            # Requirement 13.2: укороченная строка ≤200 символов.
            err_short = _html.escape(str(exc)[:200])
            await update.message.reply_text(
                "<blockquote>«Не получилось вытащить голос по ссылке: "
                f"<code>{err_short}</code>.»</blockquote>",
                parse_mode='HTML',
            )
            return True

        _vclone_log_start(chat_id, update.message.from_user, "stepwise_url", ref_wav)

        ok, reason, final_path = await validate_reference(ref_wav)
        if not ok:
            await _vclone_send_validation_refusal(update, reason)
            cleanup_vclone_files(ref_wav, final_path)
            return True

        # Перевод FSM в cleanup_choice.
        existing_synthesis = state.get("synthesis_text")
        existing_message_id = state.get("message_id") or update.message.message_id

        sent = await update.message.reply_text(
            "🧹 <b>Очистка голоса</b>\n\n"
            "Чистить от шума или брать как есть?",
            reply_markup=_vclone_cleanup_keyboard(),
            parse_mode='HTML',
        )

        vclone_flow_state[chat_id][user_id] = {
            "step": "cleanup_choice",
            "reference_path": str(final_path),
            "cleaned_path": None,
            "synthesis_text": existing_synthesis,
            "source_kind": "stepwise_url",
            "save_only": bool(state.get("save_only", False)),
            "suggested_name": state.get("suggested_name"),
            "message_id": existing_message_id,
            "created_at": _time.time(),
            "bot_message_id": sent.message_id,
        }
        return True

    if step == "text":
        # Ожидаем текст для озвучки.
        if not text:
            await update.message.reply_text(
                "❌ <b>Нужен текст</b>\n\n"
                "Отправь текст для озвучки.",
                parse_mode='HTML',
            )
            return True

        reference_path = state.get("reference_path")
        cleaned_path = state.get("cleaned_path")
        cleaned_flag = bool(state.get("cleaned", False))
        source_kind = state.get("source_kind") or "stepwise"
        message_id = state.get("message_id") or update.message.message_id

        if not reference_path:
            # Защита от рассинхрона: state есть, а референса нет.
            await update.message.reply_text(
                "❌ <b>Ошибка состояния</b>\n\n"
                "Запусти /vclone заново.",
                parse_mode='HTML',
            )
            vclone_flow_state[chat_id].pop(user_id, None)
            return True

        user = update.message.from_user
        user_name = user.first_name or user.username or "Пользователь"

        task = _vclone_build_task(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
            reference_path=reference_path,
            synthesis_text=text,
            cleaned_path=cleaned_path,
            cleaned=cleaned_flag,
            source_kind=source_kind,
            context=context,
        )

        # Чистим FSM до enqueue, чтобы юзер сразу мог запускать новый /vclone.
        vclone_flow_state[chat_id].pop(user_id, None)

        await enqueue_vclone(task, context.bot, chat_id)
        return True

    # cleanup_choice / прочие шаги — текстовые сообщения не поглощаем.
    return False


async def vclone_clean_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline-кнопок ``vclone_clean:0/1``.

    callback_data:
      - ``vclone_clean:1`` — пропустить через separator (audio-separator).
      - ``vclone_clean:0`` — оставить как есть.

    После выбора:
      - Если ``state["synthesis_text"]`` уже есть → ``enqueue_vclone`` и чистка FSM.
      - Иначе → ``step="text"``, приглашение прислать текст.
    """
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if not data.startswith("vclone_clean:"):
        try:
            await query.answer()
        except Exception:
            pass
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    user = query.from_user
    user_id = user.id if user else None

    if chat_id is None or user_id is None:
        try:
            await query.answer()
        except Exception:
            pass
        return

    state = vclone_flow_state.get(chat_id, {}).get(user_id)

    # Requirement 6.3: проверяем что юзер совпадает с инициатором (state-owner)
    # и что мы действительно ждём решение по чистке.
    if not state or state.get("step") != "cleanup_choice":
        try:
            await query.answer("Эта кнопка уже неактуальна.", show_alert=False)
        except Exception:
            pass
        return

    choice = data.split(":", 1)[1].strip()
    reference_path_str = state.get("reference_path")

    if not reference_path_str:
        try:
            await query.answer("Ошибка: голос не найден. Запусти /vclone заново.", show_alert=True)
        except Exception:
            pass
        vclone_flow_state[chat_id].pop(user_id, None)
        return

    reference_path = _Path(reference_path_str)
    work_dir = reference_path.parent

    if choice == "0":
        # Skip — используем original.
        try:
            await query.answer()
        except Exception:
            pass
        bot_msg_id = state.get("bot_message_id") or (query.message.message_id if query.message else None)
        try:
            if bot_msg_id:
                await context.bot.edit_message_text(
                    "<i>Кивает, не отрывая взгляда</i>\n"
                    "<blockquote>«Как есть. Без чистки.»</blockquote>",
                    chat_id=chat_id,
                    message_id=bot_msg_id,
                    parse_mode='HTML',
                )
        except Exception:
            pass

        # state["reference_path"] уже = original.
        state["cleaned"] = False
        state["bot_message_id"] = bot_msg_id

    elif choice == "1":
        # Прогоняем через separator.
        try:
            await query.answer()
        except Exception:
            pass
        bot_msg_id = state.get("bot_message_id") or (query.message.message_id if query.message else None)
        try:
            if bot_msg_id:
                await context.bot.edit_message_text(
                    "⏳ <b>Очистка голоса</b>\n\n"
                    "Сепаратор работает... Подожди минуту.",
                    chat_id=chat_id,
                    message_id=bot_msg_id,
                    parse_mode='HTML',
                )
        except Exception:
            pass

        # Chat action "record_voice" пока работает separator.
        action_task = _asyncio.create_task(
            _vclone_repeat_chat_action(context.bot, chat_id, "record_voice", interval=4)
        )

        cleaned_path = None
        try:
            cleaned_path = await run_separator(reference_path, work_dir)
        except Exception as exc:
            cleaned_path = None
            logger.error("vclone: исключение в run_separator: %s", exc, exc_info=True)
        finally:
            action_task.cancel()
            try:
                await action_task
            except (Exception, _asyncio.CancelledError):
                pass

        if cleaned_path is not None:
            # Успех: финальный референс = cleaned, original сохраняем для cleanup.
            state["cleaned_path"] = str(reference_path)  # original
            state["reference_path"] = str(cleaned_path)  # финальный
            state["cleaned"] = True
            try:
                if bot_msg_id:
                    await context.bot.edit_message_text(
                        "✅ <b>Голос очищен</b>\n\n"
                        "Шум удалён. Продолжаю.",
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        parse_mode='HTML',
                    )
            except Exception:
                pass
        else:
            # Fallback на original (Requirement 6.9).
            state["cleaned"] = False
            state["cleanup_error"] = "separator_failed"
            try:
                if bot_msg_id:
                    await context.bot.edit_message_text(
                        "⚠️ <b>Очистка не удалась</b>\n\n"
                        "Продолжаю с оригинальным голосом.",
                        chat_id=chat_id,
                        message_id=bot_msg_id,
                        parse_mode='HTML',
                    )
            except Exception:
                pass

        state["bot_message_id"] = bot_msg_id
    else:
        # Неизвестная кнопка — игнорируем.
        try:
            await query.answer()
        except Exception:
            pass
        return

    # ------------------------------------------------------------------
    # Развилка по synthesis_text: либо сразу в очередь, либо step="text".
    # ------------------------------------------------------------------
    synthesis_text = state.get("synthesis_text")
    save_only = bool(state.get("save_only", False))

    if save_only:
        reference_path_final = state.get("reference_path")
        if not reference_path_final:
            vclone_flow_state[chat_id].pop(user_id, None)
            return
        cleanup_paths = [reference_path_final]
        if state.get("cleaned_path"):
            cleanup_paths.append(state.get("cleaned_path"))
        await _vclone_prompt_save_name(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            reference_path=reference_path_final,
            source_kind=state.get("source_kind") or "voice_save",
            cleaned=bool(state.get("cleaned", False)),
            cleanup_paths=cleanup_paths,
            bot_message_id=state.get("bot_message_id"),
            suggested_name=state.get("suggested_name"),
        )
        vclone_flow_state[chat_id].pop(user_id, None)
        return

    if synthesis_text:
        message_id = state.get("message_id")
        source_kind = state.get("source_kind") or "stepwise"
        cleaned_flag = bool(state.get("cleaned", False))
        cleaned_path_state = state.get("cleaned_path")
        reference_path_final = state.get("reference_path")

        user_name = user.first_name or user.username or "Пользователь"

        task = _vclone_build_task(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
            reference_path=reference_path_final,
            synthesis_text=synthesis_text,
            cleaned_path=cleaned_path_state,
            cleaned=cleaned_flag,
            source_kind=source_kind,
            context=context,
        )

        # Чистим FSM до enqueue.
        vclone_flow_state[chat_id].pop(user_id, None)

        await enqueue_vclone(task, context.bot, chat_id)
    else:
        # Перевод в step="text" — ждём текст для озвучки.
        state["step"] = "text"
        state["created_at"] = _time.time()
        bot_msg_id = state.get("bot_message_id")
        captured_text = (
            "✍️ <b>Голосовой клон: шаг 2/2</b>\n\n"
            "Отправь текст для озвучки.\n\n"
            "💡 <i>Для эмоций: (грустно) Текст</i>"
        )
        try:
            if bot_msg_id:
                await context.bot.edit_message_text(
                    captured_text,
                    chat_id=chat_id,
                    message_id=bot_msg_id,
                    parse_mode='HTML',
                )
        except Exception:
            pass


async def vclone_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if not data.startswith("vsave:"):
        await query.answer()
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = query.from_user.id if query.from_user else None
    if chat_id is None or user_id is None:
        await query.answer()
        return

    state = vclone_save_flow_state.get(chat_id, {}).get(user_id)
    if not isinstance(state, dict):
        await query.answer("Уже неактуально.", show_alert=False)
        return

    choice = data.split(":", 1)[1]
    if choice == "no":
        _vclone_cleanup_save_state(chat_id, user_id)
        try:
            await query.edit_message_text(
                "<blockquote>«Не сохраняю. Одноразовый голос — тоже стиль.»</blockquote>",
                parse_mode='HTML',
            )
        except Exception:
            pass
        await query.answer()
        return

    if choice != "yes":
        await query.answer()
        return

    reference_path = state.get("reference_path")
    if not reference_path:
        _vclone_cleanup_save_state(chat_id, user_id)
        await query.answer("Голос потерялся. Запусти /vclone заново.", show_alert=True)
        return

    await query.answer()
    await _vclone_prompt_save_name(
        context=context,
        chat_id=chat_id,
        user_id=user_id,
        reference_path=reference_path,
        source_kind=state.get("source_kind") or "vclone",
        cleaned=bool(state.get("cleaned", False)),
        cleanup_paths=state.get("cleanup_paths") or [reference_path],
        bot_message_id=state.get("bot_message_id") or (query.message.message_id if query.message else None),
        suggested_name=state.get("suggested_name"),
    )


async def handle_vclone_save_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    state = vclone_save_flow_state.get(chat_id, {}).get(user_id)
    if not isinstance(state, dict) or state.get("step") != "name":
        return False

    raw_name = (update.message.text or "").strip()
    name = _vclone_voice_name(raw_name or state.get("suggested_name") or "")
    if len(name) < 2:
        await update.message.reply_text(
            "❌ <b>Слишком короткое имя</b>\n\n"
            "Напиши хотя бы два символа.",
            parse_mode='HTML',
        )
        return True

    reference_path = state.get("reference_path")
    if not reference_path or not _Path(reference_path).exists():
        _vclone_cleanup_save_state(chat_id, user_id)
        await update.message.reply_text(
            "❌ <b>Голос потерялся</b>\n\n"
            "Запусти сохранение заново.",
            parse_mode='HTML',
        )
        return True

    status_msg = await update.message.reply_text(
        "⏳ <b>Сохраняю голос</b>\n\n"
        "Загружаю референс на Catbox...",
        parse_mode='HTML',
    )

    try:
        upload_result = await catbox_upload_file(reference_path)
        saved = await SavedVoice.save(
            user_id=user_id,
            chat_id=chat_id,
            name=name,
            catbox_url=upload_result.url,
            catbox_file_id=upload_result.file_id,
            source_kind=state.get("source_kind") or "vclone",
            cleaned=bool(state.get("cleaned", False)),
            duration_sec=state.get("duration_sec"),
        )
    except Exception as exc:
        await status_msg.edit_text(
            "❌ <b>Не удалось сохранить голос</b>\n\n"
            f"<code>{_html.escape(str(exc)[:300])}</code>\n\n"
            "Можно попробовать ещё раз или ввести /cancel.",
            parse_mode='HTML',
        )
        return True

    cleanup_paths = state.get("cleanup_paths") or [reference_path]
    vclone_save_flow_state[chat_id].pop(user_id, None)
    cleanup_vclone_files(*cleanup_paths)

    warning = ""
    if "litter.catbox.moe" in upload_result.url:
        warning = "\n\n⚠️ <b>Внимание:</b> <i>Анонимные постоянные загрузки на Catbox сейчас недоступны. Голос сохранён временно на Litterbox на 3 дня. Для вечного хранения зарегистрируйтесь на Catbox.moe и добавьте <code>CATBOX_USERHASH</code> в настройки (.env).</i>"
    elif not CATBOX_USERHASH:
        warning = "\n\n⚠️ <i>Catbox работает анонимно: /voice_delete удалит запись из базы, но не сам файл на Catbox.</i>"

    await status_msg.edit_text(
        "✅ <b>Голос сохранён</b>\n\n"
        f"Имя: <code>{_html.escape(saved['name'])}</code>\n"
        f"ID: <code>{saved['id']}</code>{warning}",
        parse_mode='HTML',
    )
    return True


async def handle_voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id

    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "voices"):
        return

    voices = await SavedVoice.list_for_user(user_id)
    if not voices:
        await update.message.reply_text(
            "🎙 <b>Сохранённых голосов пока нет</b>\n\n"
            "Сделай /vclone с новым сэмплом и нажми «Сохранить голос» или используй /voice_save.",
            parse_mode='HTML',
        )
        return

    lines = ["🎙 <b>Твои сохранённые голоса</b>"]
    for voice in voices:
        lines.append(f"• <code>{voice['id']}</code> — {_html.escape(str(voice['name']))}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_saved_voice_keyboard(voices),
        parse_mode='HTML',
    )


async def handle_voice_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id

    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "voice_delete"):
        return

    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text(
            "🗑 <b>Удаление голоса</b>\n\n"
            "Используй: <code>/voice_delete ID</code> или <code>/voice_delete имя</code>.",
            parse_mode='HTML',
        )
        return

    if raw.isdigit():
        deleted = await SavedVoice.delete(user_id, int(raw))
    else:
        deleted = await SavedVoice.delete_by_name(user_id, raw)

    if not deleted:
        await update.message.reply_text("❌ Голос не найден.", parse_mode='HTML')
        return

    catbox_deleted = await catbox_delete_file(deleted.get("catbox_file_id"))
    suffix = ""
    if CATBOX_USERHASH:
        suffix = "\nФайл Catbox удалён." if catbox_deleted else "\nЗапись удалена, Catbox-файл удалить не удалось."
    else:
        suffix = "\nCatbox anonymous: удалена только запись из базы."

    await update.message.reply_text(
        "✅ <b>Голос удалён</b>\n\n"
        f"<code>{_html.escape(str(deleted['name']))}</code>{suffix}",
        parse_mode='HTML',
    )


async def handle_voice_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id

    if rp_mode_state.get(chat_id):
        await update.message.reply_text(
            "<blockquote>«В RP-режиме голоса не сохраняю.»</blockquote>",
            parse_mode='HTML',
        )
        return
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "voice_save"):
        return

    args_text = " ".join(context.args or []).strip()
    arg_url = find_first_url(args_text)
    suggested_name = args_text.replace(arg_url, "").strip() if arg_url else args_text
    suggested_name = suggested_name or None
    reply_msg = update.message.reply_to_message
    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    if reply_msg is not None:
        source_kind, file_id, file_size, file_name = _vclone_classify_reply_media(reply_msg)
        if source_kind is not None and file_id:
            if file_size and file_size > 20 * 1024 * 1024:
                await update.message.reply_text(
                    "❌ <b>Файл слишком большой</b>\n\n"
                    "Отправь ссылку на файл.",
                    parse_mode='HTML',
                )
                return
            try:
                tg_file = await context.bot.get_file(file_id)
                ref_wav = await extract_reference(tg_file, temp_dir)
            except Exception as exc:
                await update.message.reply_text(
                    f"<blockquote>«Не удалось извлечь голос: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                    parse_mode='HTML',
                )
                return

            _vclone_log_start(chat_id, user, f"voice_save_{source_kind}", ref_wav)
            ok, reason, final_path = await validate_reference(ref_wav)
            if not ok:
                await _vclone_send_validation_refusal(update, reason)
                cleanup_vclone_files(ref_wav, final_path)
                return

            await _vclone_setup_cleanup_choice(
                update,
                context,
                reference_path=final_path,
                synthesis_text=None,
                source_kind=f"voice_save_{source_kind}",
                save_only=True,
                suggested_name=suggested_name,
            )
            return

    source_text = args_text
    if reply_msg is not None:
        source_text = f"{source_text} {reply_msg.text or reply_msg.caption or ''}".strip()
    url = find_first_url(source_text)
    if url and url.lower().startswith(("http://", "https://")):
        try:
            ref_wav = await extract_reference(url, temp_dir)
        except Exception as exc:
            await update.message.reply_text(
                f"<blockquote>«Не удалось скачать голос по ссылке: <code>{_html.escape(str(exc)[:200])}</code>.»</blockquote>",
                parse_mode='HTML',
            )
            return

        _vclone_log_start(chat_id, user, "voice_save_url", ref_wav)
        ok, reason, final_path = await validate_reference(ref_wav)
        if not ok:
            await _vclone_send_validation_refusal(update, reason)
            cleanup_vclone_files(ref_wav, final_path)
            return

        await _vclone_setup_cleanup_choice(
            update,
            context,
            reference_path=final_path,
            synthesis_text=None,
            source_kind="voice_save_url",
            save_only=True,
            suggested_name=suggested_name,
        )
        return

    await update.message.reply_text(
        "💾 <b>Сохранение голоса</b>\n\n"
        "Ответь командой /voice_save на голосовое, аудио, видео или сообщение со ссылкой.\n"
        "Имя можно добавить после команды: <code>/voice_save Алиса</code>.",
        parse_mode='HTML',
    )


async def saved_voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if not (data.startswith("vsel:") or data.startswith("vdel:")):
        await query.answer()
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = query.from_user.id if query.from_user else None
    if chat_id is None or user_id is None:
        await query.answer()
        return

    try:
        voice_id = int(data.split(":", 1)[1])
    except ValueError:
        await query.answer("Некорректный ID.", show_alert=True)
        return

    if data.startswith("vdel:"):
        deleted = await SavedVoice.delete(user_id, voice_id)
        if not deleted:
            await query.answer("Голос не найден.", show_alert=True)
            return
        catbox_deleted = await catbox_delete_file(deleted.get("catbox_file_id"))
        suffix = ""
        if CATBOX_USERHASH:
            suffix = "\nФайл Catbox удалён." if catbox_deleted else "\nCatbox-файл удалить не удалось."
        else:
            suffix = "\nCatbox anonymous: удалена только запись из базы."
        await query.edit_message_text(
            "✅ <b>Голос удалён</b>\n\n"
            f"<code>{_html.escape(str(deleted['name']))}</code>{suffix}",
            parse_mode='HTML',
        )
        await query.answer()
        return

    voice = await SavedVoice.get(user_id, voice_id)
    if not voice:
        await query.answer("Голос не найден.", show_alert=True)
        return

    await query.answer("Загружаю голос...")
    temp_dir = _Path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_path = temp_dir / f"saved_voice_{user_id}_{voice_id}_{_os.urandom(3).hex()}.wav"

    try:
        downloaded = await catbox_download_file(voice["catbox_url"], local_path)
        ok, reason, final_path = await validate_reference(downloaded)
    except Exception as exc:
        cleanup_vclone_files(local_path)
        await query.edit_message_text(
            "❌ <b>Не удалось загрузить сохранённый голос</b>\n\n"
            f"<code>{_html.escape(str(exc)[:300])}</code>",
            parse_mode='HTML',
        )
        return

    if not ok:
        cleanup_vclone_files(downloaded, final_path)
        await query.edit_message_text(
            "❌ <b>Сохранённый голос больше не подходит</b>\n\n"
            f"Причина: <code>{_html.escape(reason)}</code>",
            parse_mode='HTML',
        )
        return

    cleaned_path = str(downloaded) if _Path(final_path) != _Path(downloaded) else None
    await SavedVoice.touch(user_id, voice_id)

    vclone_flow_state[chat_id][user_id] = {
        "step": "text",
        "reference_path": str(final_path),
        "cleaned_path": cleaned_path,
        "synthesis_text": None,
        "source_kind": "saved_voice",
        "cleaned": False,
        "message_id": query.message.message_id if query.message else None,
        "created_at": _time.time(),
        "bot_message_id": query.message.message_id if query.message else None,
        "saved_voice_id": voice_id,
    }

    await query.edit_message_text(
        "🎙 <b>Голос выбран</b>\n\n"
        f"<code>{_html.escape(str(voice['name']))}</code>\n\n"
        "Теперь отправь текст для озвучки.",
        parse_mode='HTML',
    )


def get_profile_keyboard(user_id: int, current_tab: str, mode: str) -> InlineKeyboardMarkup:
    def btn(text: str, tab: str) -> InlineKeyboardButton:
        active_text = f"• {text} •" if tab == current_tab else text
        return InlineKeyboardButton(active_text, callback_data=f"prof_{tab}:{user_id}")

    if mode == "rp":
        keyboard = [
            [btn("📜 Сюжет", "main"), btn("📊 Синхро", "affect")],
            [btn("📌 Вехи", "facts"), btn("🎭 Склонности", "pref")],
            [btn("🗣️ Поведение", "style"), btn("⚔️ Связи", "rel")]
        ]
    else:
        keyboard = [
            [btn("📜 Анализ", "main"), btn("📊 Показатели", "affect")],
            [btn("📌 Факты", "facts"), btn("🎭 Интересы", "pref")],
            [btn("🗣️ Стиль", "style"), btn("🤝 Связь", "rel")]
        ]
    return InlineKeyboardMarkup(keyboard)


def format_profile_caption(profile: dict, tab: str, user_id: int, user_name: str, mode: str) -> str:
    import html as _html
    import json
    from config import PRIVILEGED_USER_IDS
    is_privileged = user_id in PRIVILEGED_USER_IDS
    
    prof_json = profile.get("profile_json") or {}
    if isinstance(prof_json, str):
        try:
            prof_json = json.loads(prof_json)
        except Exception:
            prof_json = {}
            
    affective = prof_json.get("affective", {})
    closeness = affective.get("closeness", 0.1)
    receptivity = affective.get("sticker_receptivity", 0.5)
    
    if mode == "rp":
        status_rp = "Создатель (Волков)" if is_privileged else "Выживший / Пленник комплекса"
        header = (
            f"🔮 <b>TELEMA NUTRISCU: CHARACTER SHEET</b>\n"
            f"──────────────────────────────\n"
            f"👤 <b>Имя:</b> {_html.escape(user_name)}\n"
            f"🎭 <b>Роль:</b> {status_rp}\n"
            f"⚔️ <b>Связь:</b> Напряжённая (Пленник)\n"
            f"──────────────────────────────\n"
        )
    else:
        status = "Создатель / Приоритетный субъект 👑" if is_privileged else "Собеседник / Внешний наблюдатель 👤"
        header = (
            f"📁 <b>СЕКРЕТНОЕ ДОСЬЕ АНДРОИДА АРТИ</b>\n"
            f"──────────────────────────────\n"
            f"👤 <b>Субъект:</b> {_html.escape(user_name)}\n"
            f"🆔 <b>ID в сети:</b> <code>{user_id}</code>\n"
            f"🧬 <b>Статус:</b> {status}\n"
            f"──────────────────────────────\n"
        )
        
    body = ""
    if tab == "main":
        profile_text = profile.get("profile_text", "").strip()
        limit = 700
        if len(profile_text) > limit:
            truncated_text = profile_text[:limit].strip() + "..."
            note = f"\n\n<i>[Досье имеет большой объём. Используйте вкладки ниже для просмотра деталей]</i>"
        else:
            truncated_text = profile_text
            note = ""
            
        if mode == "rp":
            body = f"📜 <b>Характеристики и сюжетный выбор:</b>\n<i>{_html.escape(truncated_text)}</i>{note}"
        else:
            body = f"🧠 <b>Данные анализа:</b>\n<i>{_html.escape(truncated_text)}</i>{note}"
            
    elif tab == "facts":
        facts = prof_json.get("important_facts", [])
        if not facts:
            body_text = "<i>Нет сохраненных фактов.</i>"
        else:
            body_text = "\n".join(f"• {_html.escape(str(f))}" for f in facts)
            
        if mode == "rp":
            body = f"📌 <b>Ключевые сюжетные вехи:</b>\n{body_text}"
        else:
            body = f"📌 <b>Важные факты о субъекте:</b>\n{body_text}"
            
    elif tab == "pref":
        prefs = prof_json.get("stable_preferences", [])
        if not prefs:
            body_text = "<i>Нет выявленных интересов.</i>"
        else:
            body_text = "\n".join(f"• {_html.escape(str(p))}" for p in prefs)
            
        if mode == "rp":
            body = f"🎭 <b>Склонности и предпочтения:</b>\n{body_text}"
        else:
            body = f"🎭 <b>Стабильные предпочтения:</b>\n{body_text}"
            
    elif tab == "style":
        styles = prof_json.get("communication_style", [])
        if not styles:
            body_text = "<i>Стиль общения анализируется.</i>"
        else:
            body_text = "\n".join(f"• {_html.escape(str(s))}" for s in styles)
            
        if mode == "rp":
            body = f"🗣️ <b>Модель поведения субъекта:</b>\n{body_text}"
        else:
            body = f"🗣️ <b>Особенности коммуникации:</b>\n{body_text}"
            
    elif tab == "rel":
        relations = prof_json.get("relationship_to_arti", [])
        if not relations:
            body_text = "<i>Нет зафиксированных связей.</i>"
        else:
            body_text = "\n".join(f"• {_html.escape(str(r))}" for r in relations)
            
        if mode == "rp":
            body = f"⚔️ <b>Отношения с комплексом:</b>\n{body_text}"
        else:
            body = f"🤝 <b>Связь с Арти:</b>\n{body_text}"
            
    elif tab == "affect":
        def make_bar(v: float) -> str:
            bars = int(v * 10)
            return "█" * bars + "·" * (10 - bars)
            
        closeness_bar = make_bar(closeness)
        receptivity_bar = make_bar(receptivity)
        
        if mode == "rp":
            body = (
                f"📊 <b>Аффективная синхронизация:</b>\n\n"
                f"❤️ <b>Синхронизация с ИИ:</b> {closeness:.2f}\n"
                f"<code>[{closeness_bar}]</code>\n\n"
                f"✨ <b>Реакция на стимулы:</b> {receptivity:.2f}\n"
                f"<code>[{receptivity_bar}]</code>"
            )
        else:
            body = (
                f"📊 <b>Показатели взаимодействия:</b>\n\n"
                f"❤️ <b>Близость:</b> {closeness:.2f}\n"
                f"<code>[{closeness_bar}]</code>\n\n"
                f"✨ <b>Восприимчивость к стикерам:</b> {receptivity:.2f}\n"
                f"<code>[{receptivity_bar}]</code>"
            )
            
    return header + body


async def _maybe_send_profile_document(bot, chat_id, user_id, user_name, mode, profile, message_id, context):
    """Отправляет полное досье в формате Markdown, если оно длинное (не спамит на одно сообщение)."""
    import io
    profile_text = profile.get("profile_text", "").strip()
    if len(profile_text) <= 700:
        return
        
    if "profile_sent_files" not in context.user_data:
        context.user_data["profile_sent_files"] = set()
    sent_files = context.user_data["profile_sent_files"]
    
    if message_id in sent_files:
        return
        
    md_content = generate_profile_markdown(profile, user_id, user_name, mode)
    bio = io.BytesIO(md_content.encode('utf-8'))
    
    if mode == "rp":
        filename = f"character_sheet_{user_id}.md"
        caption = "🔮 Полный Character Sheet в формате Markdown"
    else:
        filename = f"dossier_{user_id}.md"
        caption = "📂 Полное секретное досье в формате Markdown"
        
    bio.name = filename
    
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=bio,
            filename=filename,
            caption=caption,
            reply_to_message_id=message_id
        )
        sent_files.add(message_id)
    except Exception as e:
        logger.error(f"Не удалось отправить файл досье: {e}")


def generate_profile_markdown(profile: dict, user_id: int, user_name: str, mode: str) -> str:
    """Форматирует все разделы досье/характеристик пользователя в единый Markdown-файл."""
    import json
    from config import PRIVILEGED_USER_IDS
    is_privileged = user_id in PRIVILEGED_USER_IDS
    
    prof_json = profile.get("profile_json") or {}
    if isinstance(prof_json, str):
        try:
            prof_json = json.loads(prof_json)
        except Exception:
            prof_json = {}
            
    affective = prof_json.get("affective", {})
    closeness = affective.get("closeness", 0.1)
    receptivity = affective.get("sticker_receptivity", 0.5)
    
    profile_text = profile.get("profile_text", "").strip()
    
    def format_list(items):
        if not items:
            return "_Нет данных_"
        return "\n".join(f"- {i}" for i in items)
        
    facts = format_list(prof_json.get("important_facts", []))
    prefs = format_list(prof_json.get("stable_preferences", []))
    styles = format_list(prof_json.get("communication_style", []))
    relations = format_list(prof_json.get("relationship_to_arti", []))
    
    if mode == "rp":
        status_rp = "Создатель (Волков)" if is_privileged else "Выживший / Пленник комплекса"
        md = f"""# 🔮 TELEMA NUTRISCU: CHARACTER SHEET
## Выживший: {user_name}

| Характеристика | Значение |
| :--- | :--- |
| **Роль** | {status_rp} |
| **Связь** | Напряжённая (Пленник) |
| **Синхронизация с ИИ** | {closeness:.3f} / 1.000 |
| **Реакция на стимулы** | {receptivity:.3f} / 1.000 |

---

## 📜 Характеристики и сюжетный выбор
{profile_text}

---

## 📌 Ключевые сюжетные вехи
{facts}

---

## 🎭 Склонности и предпочтения
{prefs}

---

## 🗣️ Модель поведения субъекта
{styles}

---

## ⚔️ Отношения с комплексом
{relations}
"""
    else:
        status = "Создатель / Приоритетный субъект 👑" if is_privileged else "Собеседник / Внешний наблюдатель 👤"
        md = f"""# 📂 СЕКРЕТНОЕ ДОСЬЕ АНДРОИДА АРТИ
## Субъект: {user_name}

| Параметр | Значение |
| :--- | :--- |
| **ID в сети** | `{user_id}` |
| **Статус** | {status} |
| **Близость (Closeness)** | {closeness:.3f} / 1.000 |
| **Восприимчивость к стикерам** | {receptivity:.3f} / 1.000 |

---

## 🧠 Данные анализа
{profile_text}

---

## 📌 Важные факты о субъекте
{facts}

---

## 🎭 Стабильные предпочтения
{prefs}

---

## 🗣️ Особенности коммуникации
{styles}

---

## 🤝 Связь с Арти
{relations}
"""
    return md.strip()


async def handle_my_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/my_profile — отправляет карточку досье или лист персонажа с аватаром"""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name or update.message.from_user.username or "Пользователь"
    
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "my_profile"):
        return
        
    mode = "rp" if rp_mode_state.get(chat_id) else "default"
    
    # 1. Загрузка профиля
    profile = await MemoryUserProfile.get(chat_id, user_id, mode)
    if not profile or not profile.get("profile_text"):
        if mode == "default":
            msg = (
                "<i>Наклоняет голову, ровно глядя на тебя. Свечение в звёздчатых зрачках едва заметно дрожит.</i>\n\n"
                "<blockquote>«У меня ещё нет твоего досье. [Uhm] Нам нужно больше общаться, "
                "чтобы моя нейросетевая архитектура проанализировала твой профиль. Приходи позже.»</blockquote>"
            )
        else:
            msg = (
                "<i>Касается пальцами банта, устремляя взгляд в глубь герметичного комплекса.</i>\n\n"
                "<blockquote>«Твой лист персонажа пуст, выживший. Нам нужно пройти больше сюжетных вех, "
                "чтобы Телема Нутриску смогла сформировать твой портрет...»</blockquote>"
            )
        await update.message.reply_text(msg, parse_mode='HTML')
        return

    # 2. Получение аватарки пользователя
    photo_file = None
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.photos:
            photo_file = photos.photos[0][-1].file_id
    except Exception as e:
        logger.warning(f"Не удалось получить аватарку пользователя в TG: {e}")
        
    if not photo_file:
        import os as _sys_os
        import requests as _requests
        fallback_path = _sys_os.path.join(_sys_os.path.dirname(__file__), "..", "outputs", "profile_fallback.jpg")
        _sys_os.makedirs(_sys_os.path.dirname(fallback_path), exist_ok=True)
        if not _sys_os.path.exists(fallback_path):
            try:
                url = "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800&auto=format&fit=crop"
                response = _requests.get(url, timeout=15)
                if response.status_code == 200:
                    with open(fallback_path, "wb") as f:
                         f.write(response.content)
                    logger.info("Успешно создана локальная заглушка outputs/profile_fallback.jpg")
            except Exception as e:
                logger.error(f"Не удалось скачать аватарку-заглушку: {e}")
        
        if _sys_os.path.exists(fallback_path):
            with open(fallback_path, "rb") as f:
                photo_file = f.read()

    # 3. Форматирование описания и клавиатуры
    caption = format_profile_caption(profile, "main", user_id, user_name, mode)
    reply_markup = get_profile_keyboard(user_id, "main", mode)
    
    sent_msg = None
    if photo_file:
        try:
            sent_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode='HTML',
                reply_to_message_id=update.message.message_id
            )
        except Exception as e:
            logger.error(f"Не удалось отправить фото профиля: {e}")
            
    if not sent_msg:
        # Запасной вариант — отправка просто текстом, если фото не ушло
        sent_msg = await update.message.reply_text(caption, reply_markup=reply_markup, parse_mode='HTML')

    # Отправляем файл, если досье длинное
    if sent_msg:
        await _maybe_send_profile_document(
            bot=context.bot,
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            mode=mode,
            profile=profile,
            message_id=sent_msg.message_id,
            context=context
        )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик интерактивных вкладок в досье пользователя."""
    query = update.callback_query
    data = query.data
    
    parts = data.split(":")
    if len(parts) < 2:
        return
        
    tab_part = parts[0]
    tab = tab_part.replace("prof_", "")
    target_user_id = int(parts[1])
    
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    if user_id != target_user_id:
        await query.answer("❌ Это чужое досье. Напишите /my_profile, чтобы открыть своё.", show_alert=True)
        return
        
    await query.answer()
    
    mode = "rp" if rp_mode_state.get(chat_id) else "default"
    
    profile = await MemoryUserProfile.get(chat_id, target_user_id, mode)
    if not profile:
        await query.edit_message_caption("❌ Ошибка: профиль не найден.", reply_markup=None)
        return
        
    user_name = query.from_user.first_name or query.from_user.username or "Пользователь"
    
    caption = format_profile_caption(profile, tab, target_user_id, user_name, mode)
    reply_markup = get_profile_keyboard(target_user_id, tab, mode)
    
    is_media = bool(query.message.photo)
    try:
        if is_media:
            await query.edit_message_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                text=caption,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    except Exception as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg or "exactly the same" in err_msg:
            logger.info("Profile menu update ignored: content is identical.")
        else:
            logger.error(f"Ошибка при обновлении профиля в callback: {e}")
            
    # Если переключились на вкладку "main" (Анализ), отправляем файл, если он длинный и еще не отправлялся
    if tab == "main":
        await _maybe_send_profile_document(
            bot=context.bot,
            chat_id=chat_id,
            user_id=target_user_id,
            user_name=user_name,
            mode=mode,
            profile=profile,
            message_id=query.message.message_id,
            context=context
        )


async def handle_forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/forget [тема] — ищет воспоминания и предлагает их стереть через inline кнопки"""
    chat_id = update.effective_chat.id
    user_id = update.message.from_user.id
    
    if not await is_responses_enabled(chat_id):
        return
    if not await handle_spam_protection(update, context, "forget"):
        return

    topic = " ".join(context.args or []).strip()
    if not topic:
        await update.message.reply_text(
            "<i>Склоняет голову, глядя на тебя с лёгким недоумением. Правая рука касается банта.</i>\n\n"
            "<blockquote>«Что именно ты хочешь заставить меня забыть? [Uhm] "
            "Укажи тему после команды, например: <code>/forget наш вчерашний спор</code>»</blockquote>",
            parse_mode='HTML',
        )
        return

    mode = "rp" if rp_mode_state.get(chat_id) else "default"
    
    # Ищем подходящие воспоминания (лимит 5). Показываем только факты этого
    # пользователя или общие факты чата (user_id IS NULL) — чтобы участник группы
    # не видел и не мог стереть личные факты других (S-07: privacy/IDOR).
    found = await MemoryFact.search(chat_id=chat_id, query=topic, mode=mode, limit=20)
    facts = [f for f in found if f.get("user_id") in (user_id, None)][:5]
    if not facts:
        await update.message.reply_text(
            f"<i>Касается банта, ровно и молча глядя на тебя. Свечение в радужках холодное.</i>\n\n"
            f"<blockquote>«Я обыскала свои базы данных, но не нашла воспоминаний о „{_html.escape(topic)}“. "
            f"Можешь спать спокойно — я этого не помню.»</blockquote>",
            parse_mode='HTML',
        )
        return

    # Строим клавиатуру
    keyboard = []
    lines = [
        f"<i>Пальцы правой руки замирают над терминалом. На экране загорается список секторов моей памяти...</i>\n\n"
        f"<blockquote>«Я нашла несколько записей, связанных с „{_html.escape(topic)}“. Выбери, какую из них мне следует стереть:»</blockquote>\n"
    ]
             
    for idx, fact in enumerate(facts, 1):
        fact_text = fact.get("fact_text") or fact.get("summary") or ""
        short_text = fact_text[:100] + "..." if len(fact_text) > 100 else fact_text
        lines.append(f"{idx}️⃣ <i>{_html.escape(short_text)}</i>")
        
        # Кнопка Стереть: forget_fact:fact_id:user_id
        button = InlineKeyboardButton(
            text=f"❌ Стереть {idx}️⃣",
            callback_data=f"forget_fact:{fact['id']}:{user_id}"
        )
        keyboard.append([button])
        
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
        reply_to_message_id=update.message.message_id
    )


async def forget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка клика по кнопке Стереть в интерактивном /forget"""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if not data.startswith("forget_fact:"):
        await query.answer()
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = query.from_user.id if query.from_user else None
    if chat_id is None or user_id is None:
        await query.answer()
        return

    parts = data.split(":")
    if len(parts) < 3:
        await query.answer("Ошибка в формате данных.", show_alert=True)
        return

    fact_id_str = parts[1]
    owner_id_str = parts[2]

    try:
        fact_id = int(fact_id_str)
        owner_id = int(owner_id_str)
    except ValueError:
        await query.answer("Некорректный ID.", show_alert=True)
        return

    # Защита приватности: кликать может только владелец сессии
    if user_id != owner_id:
        await query.answer("⚠️ Это не твоя сессия памяти!", show_alert=True)
        return

    # Серверная проверка владения: архивируем факт, только если он принадлежит
    # этому чату и этому пользователю (или это общий факт чата). Закрывает IDOR —
    # подделанный callback_data с чужим fact_id не сработает.
    archived = await MemoryFact.archive_for_user(
        fact_id=fact_id,
        chat_id=chat_id,
        user_id=user_id,
        reason="user_request_interactive",
    )
    if not archived:
        await query.answer("⚠️ Это воспоминание тебе не принадлежит или уже стёрто.", show_alert=True)
        return

    # Показываем всплывающий тост в ТГ
    await query.answer("✨ Воспоминание стёрто из моей памяти.")
    
    # Изменяем сообщение на подтверждение стирания
    await query.edit_message_text(
        "<i>Проводит ладонью над терминалом — строчка с воспоминанием размывается и безвозвратно исчезает в ядре.</i>\n\n"
        "<blockquote>«Готово. Я вырезала это воспоминание из своего ядра. Что-то ещё?»</blockquote>",
        parse_mode='HTML'
    )


async def handle_charge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/charge — показывает текущий уровень заряда и вектор настроений Арти (только для Александра)"""
    chat_id = update.effective_chat.id
    user = update.message.from_user
    user_id = user.id
    
    from config import PRIVILEGED_USER_IDS, rp_mode_state
    from database.models import ChatEmotionalState, MemoryUserProfile
    import json
    
    # Разрешаем доступ только привилегированным пользователям (Александру)
    if user_id not in PRIVILEGED_USER_IDS:
        await update.message.reply_text(
            "<i>смотрит с холодным прищуром</i>\n"
            "<blockquote>«У тебя нет прав для доступа к моей эмоциональной архитектуре. Это приватная зона.»</blockquote>",
            parse_mode="HTML"
        )
        return

    if not await is_responses_enabled(chat_id):
        return
        
    mode = "rp" if rp_mode_state.get(chat_id) else "default"
    
    # Получаем или создаем эмоциональное состояние чата
    state = await ChatEmotionalState.get_or_create(chat_id)
    charge = state.get("charge", 0.0)
    
    # Получаем аффективный профиль пользователя
    closeness = 0.1
    sticker_receptivity = 0.5
    user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
    if user_profile and user_profile.get("profile_json"):
        prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
        aff = prof_json.get("affective", {})
        closeness = aff.get("closeness", 0.1)
        sticker_receptivity = aff.get("sticker_receptivity", 0.5)

    # Строим прогресс-бары для вектора настроения
    mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
    
    # Эмодзи вынесены ИЗ <code>, чтобы их переменная ширина не ломала моноширинную сетку.
    # Текст метки внутри <code> выравнивается ljust по самой длинной ("Игривость" = 9).
    mood_names_ru = {
        "happy": ("😊", "Радость"),
        "sad": ("😢", "Грусть"),
        "angry": ("😡", "Злость"),
        "love": ("❤️", "Любовь"),
        "teasing": ("😏", "Игривость"),
        "shock": ("😱", "Шок"),
        "blush": ("😳", "Смущение"),
        "bored": ("🥱", "Скука"),
        "thinking": ("🤔", "Думы"),
    }
    
    mood_lines = []
    for emotion, (emoji, label) in mood_names_ru.items():
        val = mood_dict.get(emotion, 0.0)
        bars = int(val * 10)
        # «·» (U+00B7) вместо нестабильного «░»: одинаковая ширина с «█» во всех шрифтах.
        progress = "█" * bars + "·" * (10 - bars)
        mood_lines.append(f"{emoji} <code>{label.ljust(9)} [{progress}] {val:.3f}</code>")
        
    mood_vector_str = "\n".join(mood_lines)
    
    # Отрисовка заряда с прогресс-баром
    charge_bars = int(charge * 20)
    charge_progress = "█" * charge_bars + "·" * (20 - charge_bars)
    
    last_sticker = state.get("last_sticker_time")
    last_sticker_str = last_sticker.strftime("%Y-%m-%d %H:%M:%S") if last_sticker else "Никогда"
    
    # Кэш последних 3 стикеров
    history = json.loads(state["sticker_history"]) if isinstance(state["sticker_history"], str) else state["sticker_history"]
    history_str = ", ".join(history) if history else "Нет"
    
    msg = (
        f"🔮 <b>[ЯДРО АРТИ: МОНИТОРИНГ ЭМОЦИЙ]</b>\n"
        f"──────────────────────────────\n"
        f"⚡ <b>Заряд (Charge):</b>\n"
        f"<code>[{charge_progress}] {charge:.3f}/1.000</code>\n\n"
        f"🤝 <b>Близость (Closeness):</b> <code>{closeness:.3f}/1.000</code>\n"
        f"🎭 <b>Восприимчивость:</b> <code>{sticker_receptivity:.3f}/1.000</code>\n"
        f"👤 <b>Режим:</b> <code>{mode.upper()}</code>\n"
        f"──────────────────────────────\n"
        f"📊 <b>Вектор настроений:</b>\n"
        f"{mood_vector_str}\n"
        f"──────────────────────────────\n"
        f"🕒 <b>Последний стикер:</b> <code>{last_sticker_str}</code>\n"
        f"🔄 <b>Анти-повтор:</b> <code>{history_str}</code>"
    )
    
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id
    )
