"""
Управление настроениями Арти, кэшированием стикеров и их семантическим картированием.
"""
import os
import base64
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
    # Страховка: вырезаем служебный тег интроспекции (и его незакрытый хвост), если он дошёл
    # сюда из какого-либо пути генерации, минуя явный strip_introspection_tags.
    text = re.sub(r'<!--\s*emotional_introspection\s*:\s*\{.*?\}\s*-->', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<!--\s*emotional_introspection\b.*$', '', text, flags=re.DOTALL | re.IGNORECASE)
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
    """Classify up to 15 stickers through the default database vision route."""
    from config import DEFAULT_MODEL
    from ai.providers.connections import load_model_routes, provider_http_client
    from ai.providers.contracts import GenerationRequest, ImageInput
    from ai.providers.google import google_client_for, google_config
    logger.info(f"Запуск Gemini Vision для визуальной классификации '{pack_name}'...")
    
    # Анализируем первые 15 стикеров для оптимизации
    target_stickers = stickers[:15]
    
    system_prompt = (
        "Перед тобой набор стикеров с персонажем Арти. Проанализируй визуальные эмоции на каждом изображении "
        "и распредели их по следующим категориям настроения:\n"
        "happy (радость), sad (грусть), angry (злость), love (любовь), teasing (игривость), shock (шок), blush (смущение), bored (скука), thinking (задумчивость).\n"
        "Ответь строго в формате JSON, где ключи — это названия настроений, а значения — списки индексов картинок (начиная с 1).\n"
        "Пример: {\"happy\": [1, 2], \"teasing\": [3, 4], ...}"
    )
    try:
        images = []
        for sticker in target_stickers:
            file = await bot.get_file(sticker.file_id)
            file_bytes = await file.download_as_bytearray()
            images.append(ImageInput.from_base64(base64.b64encode(bytes(file_bytes)).decode()))
        if not images:
            return None
        routes = await load_model_routes(DEFAULT_MODEL)
        mappings = {mood: [] for mood in SUPPORTED_MOODS}
        for offset in range(0, len(images), 8):
            request = GenerationRequest(system_prompt, images=tuple(images[offset:offset+8]))
            route = routes.route(request)
            connection = routes.connection_for(route)
            async with asyncio.timeout(45):
                if route.endpoint.provider == 'gemini':
                    schema = {'response_json_schema': {
                        'type':'object', 'additionalProperties':False,
                        'properties':{mood:{'type':'array','items':{
                            'type':'integer','minimum':1,'maximum':len(request.images)}}
                            for mood in sorted(SUPPORTED_MOODS)}}} if 'structured_output' in connection.capabilities.get('features',[]) else {}
                    async with google_client_for(connection, timeout=40) as client:
                        response = await client.aio.models.generate_content(model=connection.model,
                            contents=request.gemini_parts(), config=google_config(connection,2048,.1,structured=True,**schema))
                        text = response.text
                else:
                    from openai import AsyncOpenAI, Omit
                    async with AsyncOpenAI(base_url=connection.base_url, api_key=connection.api_key,
                            timeout=min(40,connection.timeout_seconds), max_retries=0,
                            default_headers={'Authorization':Omit()} if not connection.api_key else None,
                            http_client=provider_http_client()) as client:
                        response = await client.chat.completions.create(
                            **connection.payload(request.openai_messages(),2048,.1,structured=True))
                        text = response.choices[0].message.content
            classification_data = json.loads(text)
            for mood, indices in classification_data.items():
                if mood not in SUPPORTED_MOODS or not isinstance(indices, list):
                    continue
                for idx in indices:
                    if type(idx) is int and 1 <= idx <= len(request.images):
                        mappings[mood].append(target_stickers[offset+idx-1].file_id)
        return mappings
    except Exception as e:
        logger.error('Ошибка классификации стикеров: %s', type(e).__name__)
    
    return None


async def send_mood_sticker_task(bot: Bot, chat_id: int, user_id: int, mood: str, message_id: int, mode: str = "default", force: bool = False, user_message_id: Optional[int] = None):
    """Express the current cognitive plan; receipt delivery owns the cooldown."""
    from cognition.runtime import CURRENT_TURN
    turn = CURRENT_TURN.get()
    if not STICKERS_ENABLED or mood not in SUPPORTED_MOODS or turn is None or not turn.active:
        return
    if turn.event.context.chat_id != chat_id or turn.expression.sticker_mood != mood:
        return
    pack = await load_sticker_pack(bot)
    choices = (pack or {}).get(mood,[])
    if not choices:
        return
    # Transport metadata is not emotional memory. Respect the last three
    # confirmed/ambiguous sends without reviving charge or receptivity.
    async with turn.runtime.pool.acquire() as conn:
        recent = await conn.fetchval("""SELECT ARRAY_AGG(payload->>'sticker_id') FROM
            (SELECT payload FROM cognitive_outbox WHERE context_id=$1 AND channel='sticker'
             AND status IN ('sending','delivered','delivery_unknown')
             AND payload ? 'sticker_id' ORDER BY id DESC LIMIT 3) s""",turn.context_id) or []
    available = [value for value in choices if value not in recent] or choices
    import hashlib
    index = int.from_bytes(hashlib.sha256(turn.event.event_id.encode()).digest()[:4],'big') % len(available)
    await bot.send_sticker(chat_id=chat_id,sticker=available[index],reply_to_message_id=message_id)


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
