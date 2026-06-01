"""
Управление настроениями Арти, кэшированием стикеров и их семантическим картированием.
"""
import os
import re
import json
import random
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import Bot, ReactionTypeEmoji
from config import ARTI_STICKER_SET, STICKERS_ENABLED
from database.connection import get_db
from database.models import ChatEmotionalState, MemoryUserProfile

logger = logging.getLogger(__name__)
emotional_logger = logging.getLogger("emotional.state")

# Белый список поддерживаемых эмоциональных настроений
SUPPORTED_MOODS = {"happy", "sad", "angry", "love", "teasing", "shock", "blush", "bored", "thinking"}

# Базовый семантический маппинг эмодзи к настроениям
MOOD_EMOJI_MAP = {
    "happy": ["😊", "😀", "😄", "😸", "✨", "👍", "👌", "😂", "😺", "🥳"],
    "sad": ["😢", "😭", "😿", "🥺", "💔", "😞", "😔", "😭", "😩", "😿"],
    "angry": ["😠", "😡", "👿", "😾", "🖕", "👊", "👺", "🗯️", "😤", "🤬"],
    "love": ["❤️", "😘", "😍", "🥰", "😻", "🫶", "🤍", "🖤", "💞", "😚"],
    "teasing": ["😏", "😜", "😉", "😼", "😛", "😈", "🤡", "😎", "👅", "🤪"],
    "shock": ["😮", "😲", "🙀", "😳", "❗", "❓", "🤯", "😱", "🫣", "👻"],
    "blush": ["😳", "😊", "🫣", "👉👈", "💖", "☺", "🙈", "😻", "💕"],
    "bored": ["🥱", "😴", "😑", "💤", "🙄", "😒", "☠️", "💀", "😪", "🤐"],
    "thinking": ["🤔", "🧐", "💭", "🤖", "💻", "🧠", "📝", "🧐", "🔍", "🕵️"],
}

# In-memory кэш стикеров во избежание постоянных запросов к БД/API
# Формат: {"pack_name": {"mood_name": ["file_id1", "file_id2"]}}
_sticker_pack_cache: Dict[str, Dict[str, List[str]]] = {}
_cache_expire_time: Dict[str, datetime] = {}


def _clean_deformed_tags(text: str) -> str:
    """Вырезает любые недоформированные или осиротевшие теги стикеров из ответа."""
    text = re.sub(r'<sticker>[^<]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<sticker>.*?</sticker>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Осиротевшие закрывающие теги
    text = re.sub(r'</sticker>', '', text, flags=re.IGNORECASE)
    return text.strip()


async def load_sticker_pack(bot: Bot, pack_name: str = ARTI_STICKER_SET) -> Dict[str, List[str]]:
    """
    Загружает кэш стикеров для указанного стикерпака.
    Использует многослойное извлечение:
    1. In-memory кэш (с TTL 1 час).
    2. Полноценный кэш в PostgreSQL (`sticker_pack_mappings`).
    3. При промахе: парсинг `.env` оверрайда или вызов Gemini для классификации.
    """
    global _sticker_pack_cache, _cache_expire_time
    
    if not pack_name:
        return {}

    now = datetime.now()
    if pack_name in _sticker_pack_cache and _cache_expire_time.get(pack_name, now) > now:
        return _sticker_pack_cache[pack_name]

    logger.info(f"Загрузка стикерпака '{pack_name}'...")
    
    # Сначала пытаемся прочесть из PostgreSQL
    db_mappings = {}
    try:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT mood_name, file_id FROM sticker_pack_mappings
                WHERE pack_name = $1
            """, pack_name)
            
            if rows:
                for row in rows:
                    db_mappings.setdefault(row["mood_name"], []).append(row["file_id"])
                
                logger.info(f"Стикерпак '{pack_name}' успешно загружен из БД PostgreSQL.")
                _sticker_pack_cache[pack_name] = db_mappings
                _cache_expire_time[pack_name] = now + timedelta(hours=1)
                return db_mappings
    except Exception as e:
        logger.error(f"Не удалось загрузить стикеры из БД: {e}")

    # Запрашиваем стикерпак у Telegram API
    try:
        sticker_set = await bot.get_sticker_set(pack_name)
        if not sticker_set or not sticker_set.stickers:
            logger.warning(f"Стикерпак '{pack_name}' пуст или не найден в Telegram.")
            return {}
    except Exception as e:
        logger.error(f"Ошибка запроса стикерпака '{pack_name}' у Telegram API: {e}")
        return {}

    stickers = sticker_set.stickers
    mappings = {mood: [] for mood in SUPPORTED_MOODS}

    # 1. Проверяем наличие ручного оверрайда в .env (Index Override Mapping)
    override_env = os.getenv("ARTI_STICKER_OVERRIDE", "").strip()
    if override_env:
        try:
            override_data = json.loads(override_env)
            logger.info(f"Обнаружен ручной оверрайд стикеров в .env.")
            
            for mood, indices in override_data.items():
                if mood not in SUPPORTED_MOODS:
                    continue
                for idx in indices:
                    if 0 <= idx < len(stickers):
                        mappings[mood].append(stickers[idx].file_id)
            
            # Сохраняем в PostgreSQL
            await _save_mappings_to_db(pack_name, mappings, stickers)
            _sticker_pack_cache[pack_name] = mappings
            _cache_expire_time[pack_name] = now + timedelta(hours=1)
            return mappings
        except Exception as e:
            logger.error(f"Ошибка при парсинге ARTI_STICKER_OVERRIDE: {e}")

    # Проверяем, "грязный" ли стикерпак (все стикеры привязаны к одному эмодзи)
    emojis = [s.emoji for s in stickers if s.emoji]
    dirty_pack = False
    if emojis:
        most_common_count = max(emojis.count(x) for x in set(emojis))
        if most_common_count / len(emojis) > 0.5:
            dirty_pack = True
            logger.warning(f"Стикерпак '{pack_name}' определён как 'грязный' (один эмодзи привязан к более чем 50% стикеров).")

    # 2. Если пак "грязный" и стикеры статичные (WebP), пробуем Gemini Vision классификацию
    if dirty_pack and not stickers[0].is_animated and not stickers[0].is_video:
        try:
            classified = await _classify_via_gemini_vision(bot, pack_name, stickers)
            if classified:
                await _save_mappings_to_db(pack_name, classified, stickers)
                _sticker_pack_cache[pack_name] = classified
                _cache_expire_time[pack_name] = now + timedelta(hours=1)
                return classified
        except Exception as e:
            logger.error(f"Не удалось классифицировать пак через Gemini Vision: {e}")

    # 3. Базовый семантический маппинг по привязанным эмодзи (Emoji-based Semantic Mapping)
    logger.info("Применяем стандартный семантический маппинг эмодзи для стикерпака.")
    filled_moods = set()
    for sticker in stickers:
        st_emoji = sticker.emoji
        if not st_emoji:
            continue
        
        # Находим категорию по эмодзи
        for mood, emoji_list in MOOD_EMOJI_MAP.items():
            if any(e in st_emoji for e in emoji_list):
                mappings[mood].append(sticker.file_id)
                filled_moods.add(mood)

    # Логируем статистику заполнения
    logger.info(f"Стикерпак '{pack_name}' классифицирован. Заполнено настроений: {len(filled_moods)} из {len(SUPPORTED_MOODS)}")
    
    # Сохраняем в БД
    await _save_mappings_to_db(pack_name, mappings, stickers)
    
    _sticker_pack_cache[pack_name] = mappings
    _cache_expire_time[pack_name] = now + timedelta(hours=1)
    return mappings


async def _save_mappings_to_db(pack_name: str, mappings: Dict[str, List[str]], stickers: List):
    """Вспомогательный метод сохранения сопоставлений в PostgreSQL."""
    try:
        async with get_db() as conn:
            async with conn.transaction():
                # Удаляем старые маппинги для этого пака
                await conn.execute("DELETE FROM sticker_pack_mappings WHERE pack_name = $1", pack_name)
                
                # Пишем новые
                for mood, file_ids in mappings.items():
                    for file_id in file_ids:
                        # Ищем оригинальный эмодзи
                        emoji = next((s.emoji for s in stickers if s.file_id == file_id), None)
                        await conn.execute("""
                            INSERT INTO sticker_pack_mappings (pack_name, mood_name, file_id, emoji)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT DO NOTHING
                        """, pack_name, mood, file_id, emoji)
    except Exception as e:
        logger.error(f"Ошибка сохранения маппинга стикеров в БД: {e}")


async def _classify_via_gemini_vision(bot: Bot, pack_name: str, stickers: List) -> Optional[Dict[str, List[str]]]:
    """Использует Gemini Vision для анализа лиц на первых 15 стикерах пака."""
    logger.info(f"Запуск Gemini Vision для визуальной классификации '{pack_name}'...")
    
    # Анализируем первые 15 стикеров для оптимизации
    target_stickers = stickers[:15]
    parts = []
    
    system_prompt = (
        "Перед тобой набор стикеров с персонажем Арти. Проанализируй визуальные эмоции на каждом изображении "
        "и распредели их по следующим категориям настроения:\n"
        "happy (радость), sad (грусть), angry (злость), love (любовь), teasing (игривость), shock (шок), blush (смущение), bored (скука), thinking (задумчивость).\n"
        "Ответь строго в формате JSON, где ключи — это названия настроений, а значения — списки индексов картинок (начиная с 1).\n"
        "Пример: {\"happy\": [1, 2], \"teasing\": [3, 4], ...}"
    )
    parts.append(system_prompt)

    # Скачиваем и прикрепляем WebP файлы в Parts
    for idx, sticker in enumerate(target_stickers, 1):
        try:
            file = await bot.get_file(sticker.file_id)
            file_bytes = await file.download_as_bytearray()
            parts.append(f"Изображение {idx}:")
            from google.genai import types
            parts.append(
                types.Part.from_bytes(
                    data=bytes(file_bytes),
                    mime_type='image/webp'
                )
            )
        except Exception as e:
            logger.error(f"Не удалось скачать стикер {idx} для Vision: {e}")
            return None

    # Вызываем Gemini
    from config import genai_client
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json"
            )
        )
        
        if response.text:
            classification_data = json.loads(response.text)
            logger.info(f"Gemini Vision успешно распределила стикеры: {classification_data}")
            
            # Собираем file_id по категориям
            mappings = {mood: [] for mood in SUPPORTED_MOODS}
            for mood, indices in classification_data.items():
                if mood not in SUPPORTED_MOODS:
                    continue
                for idx in indices:
                    # Корректируем 1-based индекс к 0-based
                    zero_idx = idx - 1
                    if 0 <= zero_idx < len(target_stickers):
                        mappings[mood].append(target_stickers[zero_idx].file_id)
            
            return mappings
    except Exception as e:
        logger.error(f"Исключение при вызове Gemini Vision: {e}")
    
    return None


async def send_mood_sticker_task(bot: Bot, chat_id: int, user_id: int, mood: str, message_id: int, mode: str = "default", force: bool = False):
    """
    Фоновая асинхронная задача: рассчитывает вероятность P_send, 
    выбирает стикер с защитой от повторов и отправляет его после имитации раздумья.

    force=True — проактивный пуш: вероятностный гейт по conversational-charge пропускается
    (после долгого молчания заряд ≈ 0, иначе проактивный стикер почти никогда не уходит).
    Жёсткий 2-минутный анти-спам сохраняется в любом случае.
    """
    if not STICKERS_ENABLED or mood not in SUPPORTED_MOODS:
        return

    try:
        # 1. Загружаем эмоциональное состояние
        emo_state = await ChatEmotionalState.get_or_create(chat_id)
        charge = emo_state.get("charge", 0.0)
        
        # Получаем аффективный профиль пользователя
        closeness = 0.1
        sticker_receptivity = 0.5
        user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
        if user_profile and user_profile.get("profile_json"):
            prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
            aff = prof_json.get("affective", {})
            closeness = aff.get("closeness", 0.1)
            sticker_receptivity = aff.get("sticker_receptivity", 0.5)

        # Фактор времени (RecencyFactor) — жёсткий анти-спам (не чаще 1 стикера в 2 минуты).
        # Дельту берём из БД (seconds_since_sticker), чтобы не смешивать datetime.now() с временем БД.
        recency_factor = 1.0
        diff_sec = emo_state.get("seconds_since_sticker")
        if diff_sec is not None:
            if diff_sec < 120:
                logger.info("Отмена отправки стикера: сработал жесткий временной анти-спам (2 минуты).")
                return
            elif diff_sec < 600:
                # В промежутке от 2 до 10 минут вероятность плавно растёт
                recency_factor = (diff_sec - 120) / 480

        if force:
            # Проактивный пуш: модель уже решила прислать стикер, поэтому обходим
            # вероятностный гейт по conversational-charge (после долгого молчания он ≈ 0).
            emotional_logger.info(
                f"[STICKER_GATE] chat_id={chat_id} user_id={user_id} | Mood={mood} | "
                f"Charge={charge:.3f} | Closeness={closeness:.3f} | Verdict=FORCED"
            )
            logger.info("Проактивный стикер: вероятностный гейт пропущен (force=True).")
        else:
            # Вероятностный гейт
            p_send = charge * sticker_receptivity * recency_factor

            # Близким друзьям повышаем базовую вероятность
            if closeness > 0.6:
                p_send = min(p_send * 1.3, 1.0)

            random_val = random.random()
            logger.info(f"Вероятностный гейт стикера: p_send={p_send:.2f}, roll={random_val:.2f}, charge={charge:.2f}")

            verdict = "PASS" if random_val <= p_send else "FAIL"
            flat_log_entry = (
                f"[STICKER_GATE] chat_id={chat_id} user_id={user_id} | "
                f"Mood={mood} | "
                f"Charge={charge:.3f} | "
                f"Receptivity={sticker_receptivity:.3f} | "
                f"RecencyFactor={recency_factor:.3f} | "
                f"Closeness={closeness:.3f} | "
                f"P_send={p_send:.3f} | "
                f"Roll={random_val:.3f} | "
                f"Verdict={verdict}"
            )
            emotional_logger.info(flat_log_entry)

            if random_val > p_send:
                logger.info("Стикер заблокирован вероятностным гейтом.")
                # FALLBACK TO REACTION: если заряд средний (> 0.15), шлем нативную реакцию
                if charge > 0.15:
                    await try_send_telegram_reaction(bot, chat_id, message_id, mood)
                return

        # 2. Загружаем стикерпак
        pack = await load_sticker_pack(bot)
        if not pack or mood not in pack or not pack[mood]:
            logger.warning(f"В стикерпаке нет стикеров для настроения '{mood}'.")
            return

        # Защита от повторов (Anti-repeat)
        history = json.loads(emo_state["sticker_history"]) if isinstance(emo_state["sticker_history"], str) else emo_state["sticker_history"]
        if not isinstance(history, list):
            history = []
            
        available_stickers = [file_id for file_id in pack[mood] if file_id not in history]
        if not available_stickers:
            available_stickers = pack[mood] # если все были использованы — сбрасываем

        selected_sticker = random.choice(available_stickers)

        # 3. Эффект раздумья (Имитация человека)
        delay = random.uniform(1.5, 4.0)
        logger.info(f"Имитация раздумья перед отправкой стикера: {delay:.2f} сек.")
        await bot.send_chat_action(chat_id=chat_id, action="choose_sticker")
        await asyncio.sleep(delay)

        # 4. Отправляем стикер
        await bot.send_sticker(
            chat_id=chat_id,
            sticker=selected_sticker,
            reply_to_message_id=message_id
        )
        logger.info(f"Стикер '{mood}' успешно отправлен в чат {chat_id}.")

        # Обновляем состояние в БД
        await ChatEmotionalState.record_sticker_sent(chat_id, selected_sticker, mood)

    except Exception as e:
        logger.error(f"Сбой отправки стикера: {e}", exc_info=True)


async def try_send_telegram_reaction(bot: Bot, chat_id: int, message_id: int, mood: str):
    """
    Отправляет нативную Telegram-реакцию на сообщение для выражения фоновых мелких эмоций.
    """
    reaction_map = {
        "happy": "👍",
        "love": "❤️",
        "teasing": "🔥",
        "blush": "🥰",
        "shock": "😱",
        "sad": "😢",
    }
    
    emoji = reaction_map.get(mood)
    if not emoji:
        return
        
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)]
        )
        logger.info(f"Отправлена фоновая реакция '{emoji}' на сообщение {message_id} в чате {chat_id}")
    except Exception as e:
        logger.warning(f"Не удалось поставить реакцию в TG: {e}")
