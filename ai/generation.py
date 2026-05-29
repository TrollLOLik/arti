"""
Генерация текстовых ответов: гибридный роутинг (Google AI Studio + OmniRoute для Qwen)
"""
import re
import base64
import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from openai import AsyncOpenAI
from google.genai import types
from config import genai_client, ARTI_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

def filter_streaming_text(text: str) -> str:
    """
    1. Вырезаем мысли <think>, ДАЖЕ если закрывающий тег еще не пришел!
    2. Прячем технические теги, пока они печатаются
    3. Убираем HTML, чтобы не крашнуть ТГ незакрытым тегом
    """
    cleaned = re.sub(r'<think>.*?(?:</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'\{(?:image|video|music)[^}]*(?:\}|$)', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = re.sub(r'</?[a-zA-Z]*$', '', cleaned)
    return cleaned.strip()


async def analyze_intent(prompt: str) -> dict:
    """
    Гибридный классификатор намерений: отсекаем очевидное по антипаттернам,
    затем спрашиваем быструю модель gemini-3.1-flash-lite-preview.
    Возвращает: {"web_search": bool, "maps": bool}
    """
    default_result = {"web_search": False, "maps": False}
    text = prompt.strip().lower()
    
    if len(text) < 10:
        return default_result

    # Антипаттерны — точно ничего не нужно
    NO_SEARCH_PATTERNS = [
        # Творчество и код
        "нарисуй", "сгенерируй", "напиши код", "напиши песн", "сочини",
        "спой", "расскажи анекдот", "стих", "сказк", "придумай",
        # Личное общение и Ролеплей
        "привет", "как дела", "погладить", "обнять", "кофе", "мур",
        "любишь", "нравит", "почему ты", "кто ты", "расскажи о себе",
        "твое мнение", "что думаешь о", "согласна",
        # Инструменты Арти
        "{image", "{video", "{music", "{tts",
        # Технические действия
        "переведи", "исправь", "сделай короче", "перефразируй", "удали",
        # Эмоции
        "не грусти", "успокойся", "прости", "спасибо", "пожалуйста"
    ]
    
    if any(p in text for p in NO_SEARCH_PATTERNS):
        return default_result
        
    # БЫСТРЫЙ ПРОХОД — точно поиск
    FAST_SEARCH_KEYWORDS = [
        "новост", "погод", "курс валют", "доллар", "евро", 
        "цена на", "стоимость", "кто такой", "что такое", "кто выиграл матч"
    ]
    
    if any(k in text for k in FAST_SEARCH_KEYWORDS):
        logger.info("🔍 Быстрый проход: поиск нужен (по ключевым словам)")
        return {"web_search": True, "maps": False}

    # БЫСТРЫЙ ПРОХОД — точно карты
    FAST_MAP_KEYWORDS = [
        "где поблизости", "рядом со мной", "ближайш", "как добраться",
        "проложи маршрут", "где тут", "где здесь", "поблизости",
        "рядом есть", "куда сходить", "где поесть", "где выпить",
        "ближайшая аптека", "ближайший банк", "ближайшая заправка",
        "покажи на карте", "на карте"
    ]

    if any(k in text for k in FAST_MAP_KEYWORDS):
        logger.info("🗺 Быстрый проход: карты нужны (по ключевым словам)")
        return {"web_search": False, "maps": True}

    # Серая зона — спрашиваем Gemini
    system_prompt = """Ты — системный классификатор намерений. Проанализируй запрос пользователя.
Ответь строго одним словом:
- "SEARCH" — если запрос касается новостей, погоды, фактов, курсов, цен, биографий, результатов спорта, актуальной информации.
- "MAPS" — если запрос связан с геолокацией: поиск мест рядом, маршруты, адреса, "где находится", кафе/рестораны/аптеки/магазины поблизости.
- "NO" — если это обычная беседа, ролевая игра, шутка, код, перевод.
Ответь строго одним словом: SEARCH, MAPS или NO."""

    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-3.1-flash-lite-preview",
            contents=f"{system_prompt}\n\nЗапрос пользователя: '{prompt}'",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=5
            )
        )
        
        if response.text:
            answer = response.text.strip().upper()
            logger.debug(f"gemini-3.1-flash-lite-preview router response: {answer}")
            
            if "SEARCH" in answer:
                logger.info("🔍 gemini-3.1-flash-lite-preview: поиск нужен")
                return {"web_search": True, "maps": False}
            elif "MAPS" in answer or "MAP" in answer:
                logger.info("🗺 gemini-3.1-flash-lite-preview: карты нужны")
                return {"web_search": False, "maps": True}
                
    except Exception as e:
        logger.warning(f"Исключение в роутере намерений gemini-3.1-flash-lite-preview: {e}")

    return default_result


async def needs_web_search(prompt: str) -> bool:
    """Обратная совместимость: обёртка над analyze_intent."""
    intent = await analyze_intent(prompt)
    return intent.get("web_search", False)


async def is_message_for_arti(user_message: str, recent_context: str, user_name: str = "Пользователь") -> bool:
    """
    LLM-фильтр: определяет, адресовано ли сообщение ИИ-ассистенту или другому человеку в чате.
    Используется для групповых чатов, когда пользователь недавно общался с Арти.
    """
    text = user_message.strip()
    if len(text) < 2:
        return False
    
    # Быстрый проход: явные обращения
    text_lower = text.lower()
    if "арти" in text_lower:
        return True
    
    system_prompt = (
        "Ты — фильтр сообщений в групповом чате. Есть ИИ-ассистент по имени Арти.\n"
        "Определи, адресовано ли новое сообщение ИИ-ассистенту Арти или оно является частью обычного разговора между другими людьми в группе.\n"
        "Ответь строго одним словом: ДА (адресовано Арти) или НЕТ (обращено к кому-то другому / общая беседа)."
    )
    
    prompt = (
        f"Контекст беседы в группе:\n{recent_context}\n\n"
        f"Новое сообщение от {user_name}:\n«{text}»\n\n"
        f"Определи: это сообщение ({user_name} -> Арти) или это просто разговор людей между собой?\n"
        f"Ответь строго ДА или НЕТ."
    )
    
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-3.1-flash-lite-preview",
            contents=f"{system_prompt}\n\n{prompt}",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=5
            )
        )
        
        if response.text:
            answer = response.text.strip().upper()
            result = "ДА" in answer or "DA" in answer or "YES" in answer
            logger.debug(f"LLM-фильтр для '{text[:50]}': {answer} → {result}")
            return result
    except Exception as e:
        logger.warning(f"Ошибка LLM-фильтра: {e}")
    
    # При ошибке — лучше не отвечать (меньше спама в группе)
    return False


async def generate_response_stream(
    chat_id,
    prompt,
    user_name,
    chat_context,
    base64_image=None,
    base64_images=None,
    uploaded_video_file=None,
    user_location=None,
    model="gemini-3.1-flash-lite-preview",
    temperature=0.7,
    custom_system_prompt=None,
    user_id=None,
    is_rp_mode=False,
):
    """
    Генерация ответа: гибридный роутинг (Google AI Studio + OmniRoute для Qwen)
    Возвращает: (response_text, used_search, grounding_links, found_image_urls)
    """
    if base64_image and not base64_images:
        base64_images = [base64_image]
    elif not base64_images:
        base64_images = []

    # RP-режим: переопределяем системный промпт и отключаем поиск/карты
    if is_rp_mode:
        from config import RP_SYSTEM_PROMPT
        actual_role = custom_system_prompt if custom_system_prompt else RP_SYSTEM_PROMPT
        should_search = False
        user_location = None
    else:
        actual_role = custom_system_prompt if custom_system_prompt else ARTI_SYSTEM_PROMPT

        # --- 0. ИНЖЕКТ ГЕОЛОКАЦИИ В СИСТЕМНЫЙ ПРОМПТ (всегда, если есть) ---
        # Добавляем в начало, чтобы не затирать инструкции по форматированию HTML
        if user_id is not None:
            from utils.location_manager import get_user_location_context
            location_context = await get_user_location_context(user_id)
            if location_context:
                actual_role = location_context + "\n\n" + actual_role

    # --- ДИНАМИЧЕСКИЕ НАВЫКИ (SKILLS) ---
    from ai.skills import get_active_skills_instructions
    skills_prompt = get_active_skills_instructions(prompt)
    if skills_prompt:
        logger.info("🛠 Подмешиваем инструкции навыков в системный промпт...")
        actual_role += "\n" + skills_prompt

    # --- 1. ОБЩАЯ ПОДГОТОВКА КОНТЕКСТА ---
    context_lines = chat_context.split("\n")
    if context_lines and context_lines[-1].strip() == prompt.strip():
        context_lines = context_lines[:-1]
    formatted_context = "\n".join(context_lines[-20:])
    
    final_prompt = f"Контекст:\n{formatted_context}\n\nПользователь ({user_name}) говорит:\n{prompt}"

    # --- 2. ОПРЕДЕЛЯЕМ НУЖДАЕТСЯ ЛИ ЗАПРОС В ПОИСКЕ ---
    should_search = False
    if not base64_images and not user_location:
        intent = await analyze_intent(prompt)
        should_search = intent.get("web_search", False)

    # Qwen — исключительно текстовая модель, поэтому переключаем на Gemini
    if ("qwen" in model.lower() or "qw" in model.lower()) and (base64_images or should_search or uploaded_video_file or user_location):
        logger.info("Медиа, карты или поиск в запросе, переключаем Qwen обратно на Gemini.")
        model = "gemini-3.1-flash-lite-preview"

    # Map-запросы с геолокацией: только Gemini имеет Google Maps Grounding.
    # Non-Gemini модели при наличии координат начинают галлюцинировать места.
    if user_location and not model.lower().startswith("gemini"):
        logger.info("🗺 Map-запрос с геолокацией для non-Gemini модели — переключаем на Gemini для Google Maps Grounding.")
        model = "gemini-2.5-flash"

    # =====================================================================
    # 🌟 ВЕТКА OMNIROUTE (Claude, Qwen, DeepSeek, etc.)
    # =====================================================================
    if not model.lower().startswith("gemini"):
        client = AsyncOpenAI(
            base_url="http://localhost:20128/v1",
            api_key="sk-5d8d8294f9d6911b-3eb135-8f0e8f4f"
        )

        # Если есть координаты — добавляем в промпт для non-Gemini моделей
        omni_prompt = final_prompt
        if user_location:
            loc_city = user_location.get("city") or "неизвестный город"
            loc_lat = user_location["lat"]
            loc_lng = user_location["lng"]
            omni_prompt = (
                f"[ГЕОЛОКАЦИЯ]: Пользователь находится в {loc_city}, "
                f"координаты {loc_lat:.5f}, {loc_lng:.5f}. "
                f"Если запрос связан с местами поблизости — учитывай это.\n\n"
                + final_prompt
            )

        messages = [
            {"role": "system", "content": actual_role},
            {"role": "user", "content": omni_prompt}
        ]

        try:
            logger.info(f"🤖 Генерация через OmniRoute: {model}")
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature
            )
            return response.choices[0].message.content, False, [], []
            
        except Exception as e:
            logger.error(f"Ошибка генерации через OmniRoute ({model}): {e}")
            return f"К сожалению, модель {model} сейчас недоступна или отдыхает.", False, [], []


    # =====================================================================
    # 🔵 ВЕТКА GOOGLE AI STUDIO (GEMINI)
    # =====================================================================
    parts = []
    
    if uploaded_video_file:
        logger.info("В запросе присутствует обработанное загруженное видео, добавляем в payload. Используем gemini-3.1-flash-lite-preview.")
        model = "gemini-3.1-flash-lite-preview"
        parts.append(uploaded_video_file)
    
    if base64_images:
        logger.info(f"В запросе есть картинки ({len(base64_images)} шт), добавляем в payload. Используем gemini-3.1-flash-lite-preview.")
        model = "gemini-3.1-flash-lite-preview"
        
        for b64 in base64_images:
            if "," in b64:
                b64 = b64.split(",")[1]
            image_bytes = base64.b64decode(b64)
            parts.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type='image/jpeg' 
                )
            )

    parts.append(types.Part.from_text(text=final_prompt))

    active_tools = None
        
    if should_search:
        logger.info("🔍 Активирован встроенный поиск Google (переключаем на gemini-2.5-flash)")
        model = "gemini-2.5-flash"
        active_tools = [types.Tool(google_search=types.GoogleSearch())]
        
    if user_location:
        logger.info(f"🗺 Активирован Google Maps Grounding для координат {user_location['lat']}, {user_location['lng']}.")
        # Для заземления на картах лучше всего подходит 2.0-flash
        model = "gemini-2.5-flash"
        
        if active_tools is None:
            active_tools = []
        
        active_tools.append(types.Tool(google_maps=types.GoogleMaps()))
        actual_role += "\n\n[СИСТЕМНОЕ УВЕДОМЛЕНИЕ]: Ты используешь Google Maps. Подскажи пользователю крутые места поблизости, основываясь на данных инструмента, и сохрани свой дерзкий характер."

    FALLBACK_MODELS = {
        "gemini-3.1-flash-lite-preview": "gemini-3-flash-preview",
        "gemini-2.5-flash": "gemma-4-26b-a4b-it",
    }

    # Настройка конфигурации инструментов (для передачи координат)
    tool_config = None
    if user_location:
        tool_config = types.ToolConfig(
            retrieval_config=types.RetrievalConfig(
                lat_lng=types.LatLng(
                    latitude=user_location["lat"],
                    longitude=user_location["lng"]
                )
            )
        )

    config = types.GenerateContentConfig(
        system_instruction=actual_role,
        temperature=temperature,
        tools=active_tools,
        tool_config=tool_config,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]
    )

    current_model = model
    max_retries = 3
    switched_to_fallback = False

    for attempt in range(max_retries):
        try:
            logger.info(f"🤖 Генерация через Google AI Studio: {current_model}")
            response = await asyncio.to_thread(
                genai_client.models.generate_content,
                model=current_model,
                contents=parts,
                config=config
            )
            
            if response.text:
                used_search = False
                grounding_links = []
                found_image_urls = []
                
                if response.candidates and response.candidates[0].grounding_metadata:
                    metadata = response.candidates[0].grounding_metadata
                    
                    if metadata.grounding_chunks:
                        used_search = True
                        logger.info("🌐 Google использовал инструменты заземления (Поиск/Карты) для этого ответа.")
                        seen_urls = set()
                        for chunk in metadata.grounding_chunks:
                            if hasattr(chunk, 'web') and chunk.web and chunk.web.uri:
                                uri = chunk.web.uri
                                if uri in seen_urls: continue
                                seen_urls.add(uri)
                                domain = urlparse(uri).netloc.replace('www.', '')
                                title = chunk.web.title or domain
                                grounding_links.append((uri, title))

                    if hasattr(metadata, 'search_entry_point') and hasattr(metadata, 'grounding_chunks'):
                        if hasattr(metadata.search_entry_point, 'rendered_content') and metadata.search_entry_point.rendered_content:
                            img_tags = re.findall(r'<img[^>]+src=["\']([^"\'>]+)["\']', metadata.search_entry_point.rendered_content)
                            for img_url in img_tags:
                                if img_url.startswith('http') and img_url not in found_image_urls:
                                    found_image_urls.append(img_url)
                                    if len(found_image_urls) >= 3:
                                        break
                            if found_image_urls:
                                logger.info(f"🖼 Найдено {len(found_image_urls)} картинок в rendered_content")

                return response.text, used_search, grounding_links, found_image_urls
                
            raise Exception("Пустой ответ от Google API")

        except Exception as e:
            error_str = str(e)
            is_overloaded = "503" in error_str or "UNAVAILABLE" in error_str or "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            
            logger.warning(f"Попытка {attempt+1}/{max_retries} провалена (модель: {current_model}): {e}")
            
            if is_overloaded and not switched_to_fallback and current_model in FALLBACK_MODELS:
                fallback = FALLBACK_MODELS[current_model]
                logger.info(f"⚡ Модель {current_model} перегружена, переключаемся на фолбэк: {fallback}")
                current_model = fallback
                switched_to_fallback = True
                await asyncio.sleep(1)
            elif attempt < max_retries - 1:
                await asyncio.sleep(2)
            else:
                logger.error("Все попытки генерации провалены.")
                return "К сожалению, произошла ошибка. Попробуйте позже.", False, [], []

