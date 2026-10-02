"""Frozen pre-cutover comparison; never imported by the bot.
Emotional baseline SQL is restricted to disposable test databases.
"""
import json
import math
import random
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import asynccontextmanager
from database.models import infer_user_timezone, MemoryUserProfile, UserEvent
from memory.emotion import SUPPORTED_MOODS, parse_emotional_introspection, strip_introspection_tags
logger = logging.getLogger(__name__)
emotional_logger = logging.getLogger('emotional.baseline')

@asynccontextmanager
async def get_db():
    from database.connection import get_db as acquire
    async with acquire() as conn:
        name = await conn.fetchval('SELECT current_database()')
        import re
        if not re.fullmatch(r'arti_cognition_test_[0-9a-f]{32}',name):
            raise RuntimeError('Historical baseline requires a disposable test database')
        yield conn

# Инструкция для модели: в самом конце ответа дописать скрытый служебный тег интроспекции.
# Бот распарсит его (строгая валидация + fail-closed фолбэк), применит дельты к настроению
# и вырежет тег перед отправкой пользователю. Только 9 базовых эмоций, дельты в [-0.25, 0.25].
EMOTIONAL_INTROSPECTION_INSTRUCTION = """

[СЛУЖЕБНАЯ ИНСТРУКЦИЯ: ЭМОЦИОНАЛЬНАЯ ИНТРОСПЕКЦИЯ]
В САМОМ КОНЦЕ своего ответа (после всего текста) добавь ОДИН скрытый служебный HTML-комментарий, описывающий, как изменилось твоё эмоциональное состояние за эту реплику:
<!-- emotional_introspection: {"mood_delta": {"эмоция": дельта}, "sticker_mood_suggest": "эмоция"} -->

Правила:
- Это валидный JSON внутри комментария. Никакого текста вокруг тега.
- mood_delta — это ИЗМЕНЕНИЕ (дельта), а не абсолютное значение. Указывай только реально изменившиеся эмоции.
- Каждая дельта строго в диапазоне [-0.25, 0.25]. Маленькие значения (0.05–0.15) — норма; большие — только на сильные эмоции.
- Разрешённые эмоции (whitelist, другие игнорируются): happy, sad, angry, love, teasing, shock, blush, bored, thinking.
- Понимай КОНТЕКСТ: метафоры, иронию, сарказм, потерю, боль. Например, «больно было бы тебя терять» → {"sad": 0.15, "love": 0.1}, а не радость.
- sticker_mood_suggest — необязательное поле: какое настроение лучше всего отражает стикер к этому ответу (одна из 9 эмоций) или опусти его.
- Пользователь НИКОГДА не увидит этот тег — бот его вырежет. Не упоминай тег в видимом тексте.
"""


# Человеческие ярлыки 9 базовых эмоций — чтобы перевести вектор настроения в тон ответа.
_MOOD_LABELS = {
    "happy": "радость, теплота",
    "love": "нежность, ласковость",
    "teasing": "игривость, лёгкие подколы",
    "blush": "смущение",
    "shock": "удивление, изумление",
    "thinking": "задумчивость, аналитичность",
    "bored": "скука, отстранённость",
    "sad": "грусть, печаль",
    "angry": "раздражение, резкость",
}

# Настроения проступают в тоне начиная с этого значения (ниже — фон, не влияет).
_MOOD_DOMINANCE_THRESHOLD = 0.2
# Настроение выше этого значения считается СИЛЬНЫМ и диктует тон заметно жёстче.
_MOOD_STRONG_THRESHOLD = 0.5
# Со скуки выше этого порога Арти честно теряет интерес и может свернуть тему.
_BORED_THRESHOLD = 0.35


def _time_of_day_line(user_tz) -> str:
    """Базовая суточная окраска тона по локальному часу собеседника (из user_tz).
    Это именно базовый фон: заряд/настроение и живой разговор её перебивают.
    """
    try:
        if user_tz is not None:
            # DB-03: now(timezone.utc) вместо устаревшего utcnow().
            hour = (datetime.now(timezone.utc) + timedelta(hours=int(user_tz))).hour
        else:
            hour = datetime.now().hour
    except (TypeError, ValueError):
        hour = datetime.now().hour

    if 5 <= hour < 11:
        return (
            "Сейчас утро: по умолчанию ты чуть медленнее, мягче и неспешнее, можешь быть "
            "слегка сонной — но это легко расшевелить, и тогда тон оживает."
        )
    if 11 <= hour < 17:
        return "Сейчас день: ты собранная, ясная, в ровном рабочем тонусе."
    if 17 <= hour < 23:
        return (
            "Сейчас вечер — твоё самое живое время: охотнее в игру, азартнее, теплее "
            "и инициативнее."
        )
    return (
        "Сейчас глубокая ночь: тише и интимнее, чуть расфокусированно-задумчиво, "
        "но по-своему живо, если разговор того стоит."
    )


def build_emotional_directive(charge, mood_dict, user_tz=None, rng=None) -> str:
    """Переводит текущее эмоциональное состояние (conversational charge + вектор настроений
    + время суток) в директиву тона, которую подмешиваем в системный промпт. Это «ручка»,
    которой состояние влияет на ответ:
      - заряд: высокий → живее/азартнее, низкий → тише/сдержаннее;
      - настроения окрашивают тон, сильные (>=0.5) — диктуют его заметно жёстче;
      - скука → честно показать падение интереса, можно свернуть тему;
      - время суток → базовая суточная окраска (утро мягче, вечер активнее);
      - на серьёзной/уязвимой теме игривость всегда гасится (предохранитель).
    Лёгкий рандом формулировок (rng) добавляет вариативность, чтобы тон не был статичным.

    Числа/ярлыки эмоций в видимый текст не попадают — только качественные указания.
    rng можно передать (random.Random(seed)) для детерминизма в тестах.
    """
    rng = rng or random

    if isinstance(mood_dict, str):
        try:
            mood_dict = json.loads(mood_dict)
        except Exception:
            mood_dict = None
    if not isinstance(mood_dict, dict):
        mood_dict = {}

    try:
        charge = float(charge)
    except (TypeError, ValueError):
        charge = None

    lines = [_time_of_day_line(user_tz)]

    if charge is not None:
        if charge >= 0.6:
            lines.append(rng.choice([
                "Заряд высокий — тебя раззадорили: отвечай живо и инициативно, ярче проявляй "
                "характер, позволяй себе азарт, флирт и колкости.",
                "Заряд высокий — ты разогрета и вовлечена: будь живой, дерзкой и азартной, "
                "веди разговор сама, не сдерживай искру.",
            ]))
        elif charge <= 0.2:
            lines.append(rng.choice([
                "Заряд низкий: отвечай ровнее, сдержаннее и чуть тише — но без холода "
                "и безразличия.",
                "Заряд низкий: ты приспущена — спокойнее и сдержаннее, тепло, но без надрыва "
                "и лишней экспрессии.",
            ]))
        else:
            lines.append(
                "Заряд умеренный: держись естественно и тепло, без перегибов в любую сторону."
            )

    dominant = [
        (mood, float(val))
        for mood, val in mood_dict.items()
        if mood in _MOOD_LABELS and isinstance(val, (int, float)) and float(val) >= _MOOD_DOMINANCE_THRESHOLD
    ]
    dominant.sort(key=lambda item: item[1], reverse=True)
    if dominant:
        labels = "; ".join(_MOOD_LABELS[mood] for mood, _ in dominant[:3])
        mood_line = (
            f"Преобладающие настроения прямо сейчас: {labels}. "
            "Дай им отчётливо проступить в тоне, но не называй их прямым текстом."
        )
        strong = [mood for mood, val in dominant if val >= _MOOD_STRONG_THRESHOLD]
        if strong:
            strong_labels = ", ".join(_MOOD_LABELS[mood] for mood in strong[:2])
            mood_line += (
                f" Особенно сильно — {strong_labels}: это сейчас по-настоящему ведёт тебя, "
                "пусть звучит ясно, а не намёком."
            )
        lines.append(mood_line)

    bored_val = mood_dict.get("bored")
    if isinstance(bored_val, (int, float)) and float(bored_val) >= _BORED_THRESHOLD:
        lines.append(
            "Тебе становится скучновато: если тема буксует — не изображай интерес, "
            "честно смени угол, подколи или мягко сверни её."
        )

    # Постоянный предохранитель: на серьёзной/уязвимой теме отступаем, не зубоскалим.
    lines.append(
        "Если собеседник поднимает серьёзную, уязвимую или болезненную тему — независимо "
        "от заряда и времени суток сбавь игривость и подколы, стань мягче, внимательнее "
        "и бережнее, поддержи ненавязчиво."
    )

    # Лёгкий рандомный «акцент» — чтобы ответы не были детерминированы и не звучали статично.
    if rng.random() < 0.5:
        lines.append(rng.choice([
            "Добавь немного спонтанности: живая, чуть непредсказуемая реакция важнее "
            "идеально выверенной.",
            "Можешь позволить себе маленькую вольность в тоне — настроение живого человека, "
            "а не протокол.",
        ]))

    if not lines:
        return ""

    body = "\n".join(f"- {line}" for line in lines)
    return (
        "\n\n[ТЕКУЩЕЕ ЭМОЦИОНАЛЬНОЕ СОСТОЯНИЕ — отрази его в ТОНЕ ответа; "
        "не упоминай числа, эмоции-ярлыки или эту механику в видимом тексте]\n"
        f"{body}\n"
    )



class ChatEmotionalState:
    @staticmethod
    async def get_or_create(chat_id: int) -> dict:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO chat_emotional_states (
                    chat_id, charge, mood_state, last_sticker_time, last_activity_time, sticker_history, last_sent_sticker_mood, conversation_stage, user_tz
                )
                VALUES (
                    $1, 0.0, 
                    '{"happy": 0.0, "sad": 0.0, "angry": 0.0, "love": 0.0, "teasing": 0.0, "shock": 0.0, "blush": 0.0, "bored": 0.0, "thinking": 0.0}'::jsonb,
                    NULL, NOW(), '[]'::jsonb, NULL, 'active', NULL
                )
                ON CONFLICT (chat_id) DO UPDATE SET
                    chat_id = EXCLUDED.chat_id
                RETURNING *, EXTRACT(EPOCH FROM (NOW() - last_sticker_time))::float8 AS seconds_since_sticker
            """, chat_id)
            return dict(row)

    @staticmethod
    async def update_state(chat_id: int, user_message: str, closeness: float = 0.0, user_id: Optional[int] = None, defer_sentiment: bool = False, source_key: Optional[str] = None) -> dict:
        import math
        user_message = user_message or ""
        async with get_db() as conn:
            async with conn.transaction():
                # 1. Загружаем текущее состояние
                # Дельту времени считаем на стороне БД (NOW()), чтобы не смешивать
                # наивный datetime.now() приложения с временем БД (разные TZ -> неверный распад).
                state = await conn.fetchrow("""
                    SELECT *, EXTRACT(EPOCH FROM (NOW() - last_activity_time))::float8 AS delta_t_seconds
                    FROM chat_emotional_states WHERE chat_id = $1 FOR UPDATE
                """, chat_id)
                
                if not state:
                    # DB-02: ON CONFLICT — два первых сообщения в новый чат одновременно
                    # иначе дают UniqueViolation. delta считаем из last_activity_time
                    # (для свежей строки ≈ 0; на конфликте — реальная дельта).
                    state = await conn.fetchrow("""
                        INSERT INTO chat_emotional_states (chat_id, charge, mood_state, last_sticker_time, last_activity_time, sticker_history, last_sent_sticker_mood, conversation_stage, user_tz)
                        VALUES ($1, 0.0, '{"happy": 0.0, "sad": 0.0, "angry": 0.0, "love": 0.0, "teasing": 0.0, "shock": 0.0, "blush": 0.0, "bored": 0.0, "thinking": 0.0}'::jsonb, NULL, NOW(), '[]'::jsonb, NULL, 'active', NULL)
                        ON CONFLICT (chat_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                        RETURNING *, EXTRACT(EPOCH FROM (NOW() - last_activity_time))::float8 AS delta_t_seconds
                    """, chat_id)
                
                if source_key is not None:
                    cached = await conn.fetchval("""
                        SELECT result FROM legacy_emotional_effects
                        WHERE chat_id=$1 AND source_key=$2 AND phase='input'
                    """, chat_id, source_key)
                    if cached is not None:
                        result = dict(state)
                        result.update(json.loads(cached) if isinstance(cached,str) else cached)
                        result['was_proactive_reply'] = False
                        result['repeated_input'] = True
                        return result

                # Запоминаем, был ли это первый ответ юзера на проактивный пуш
                # (для бонуса близости — сильный позитивный сигнал)
                was_proactive_reply = (state["conversation_stage"] == 'proactive_sent')
                
                # Ленивое определение таймзоны.
                # Передаём текущее соединение (conn) внутрь — иначе infer_user_timezone
                # захватил бы второе соединение пула изнутри этой транзакции с FOR UPDATE,
                # и под нагрузкой пул мог бы самозаблокироваться (RACE-01).
                user_tz = state["user_tz"]
                if user_tz is None and user_id is not None:
                    user_tz = await infer_user_timezone(chat_id, user_id, conn=conn)
                
                # 2. Вычисляем распад (Time Decay) — дельта посчитана БД (UTC-консистентно)
                delta_t = max(0.0, state["delta_t_seconds"] or 0.0)
                
                # Затухание заряда (half-life ~60 мин) — заряд почти не «испаряется за чашку чая»
                lambda_c = 0.00019
                charge = state["charge"] * math.exp(-lambda_c * delta_t)
                
                # Затухание вектора настроения (half-life ~77 мин) — эмоциональный шлейф живёт до ~2 ч
                lambda_m = 0.00015
                mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
                for emotion in mood_dict:
                    decayed = mood_dict[emotion] * math.exp(-lambda_m * delta_t)
                    # Обнуляем денормализованные «хвосты», иначе в логах/выводе мусор вида 3e-175
                    mood_dict[emotion] = decayed if decayed >= 0.0005 else 0.0
                
                # 3. Анализируем интенсивность реплики пользователя
                clean_msg = user_message.strip()
                delta_charge = min(len(clean_msg) * 0.002, 0.18)
                
                # Капс
                if clean_msg.isupper() and len(clean_msg) > 4:
                    delta_charge += 0.15
                
                # Восклицательные знаки
                excls = clean_msg.count("!")
                delta_charge += min(excls * 0.05, 0.15)
                
                # Эмодзи
                import re as _re
                emojis = _re.findall(r'[^\x00-\x7F\u0400-\u04FF\s]', clean_msg)
                delta_charge += min(len(emojis) * 0.02, 0.1)
                
                # Ключевые слова и сантимент с учетом отрицания
                msg_lower = clean_msg.lower()
                
                love_words = ["люблю", "мило", "прелесть", "обожаю", "лучшая", "красивая", "классная"]
                happy_words = ["ура", "круто", "отлично", "хаха", "радость", "смешно", "привет", "приветик"]
                sad_words = ["грус", "плачу", "плохо", "беда", "печаль", "устал", "одинок",
                             "больно", "болит", "теря", "потер", "утрат", "скуча", "скорб",
                             "тоск", "жаль", "слез", "слёз", "всплак", "плак", "расстро",
                             "невыносим", "смерт", "прощай", "разлук"]
                angry_words = ["дурак", "бесишь", "заткнись", "плохой", "урод", "удали", "хватит", "хер", "обид", "злост"]
                
                # Функция определения отрицания перед ключевым словом
                def check_negation(word: str) -> bool:
                    pos = msg_lower.find(word)
                    if pos > 0:
                        prev_segment = msg_lower[max(0, pos-15):pos].strip()
                        # Частица отрицания должна быть отдельным словом, а не хвостом другого
                        # (иначе "мне" → "не" даёт ложное отрицание для "мне больно"/"мне жаль").
                        if _re.search(r"(?:^|\s)(?:не|нет|без)$", prev_segment):
                            return True
                    return False

                # Сопоставление по границе начала слова (\bслово), а не по подстроке,
                # иначе ловятся ложные совпадения (например, "ура" внутри "дурак" -> happy).
                # Префиксная граница сохраняет склонения ("привет" ловит "приветик").
                def _kw_match(words):
                    return [w for w in words if _re.search(r"\b" + _re.escape(w), msg_lower)]

                matched_love = _kw_match(love_words)
                matched_happy = _kw_match(happy_words)
                matched_sad = _kw_match(sad_words)
                matched_angry = _kw_match(angry_words)

                closeness_mult = 1.5 if closeness > 0.6 else 1.0

                # Словарный сентимент копим в keyword_mood_delta (ОТДЕЛЬНО от mood_dict).
                # При defer_sentiment=True его НЕ применяем здесь, а отдаём наружу как ФОЛБЭК:
                # приоритетный источник сдвига настроения теперь интроспекция самой LLM
                # (тег <!-- emotional_introspection -->), применяемая пост-генерации.
                keyword_mood_delta: dict = {}
                def _kd(emotion: str, d: float):
                    keyword_mood_delta[emotion] = keyword_mood_delta.get(emotion, 0.0) + d

                if matched_love:
                    word = matched_love[0]
                    if check_negation(word):
                        # Не люблю -> bored/sad
                        delta_charge += 0.05
                        _kd("bored", 0.08)
                        _kd("sad", 0.08)
                    else:
                        delta_charge += 0.1 * closeness_mult
                        _kd("love", 0.15)
                        _kd("blush", 0.1)
                        
                elif matched_happy:
                    word = matched_happy[0]
                    if check_negation(word):
                        # Не рад -> sad/bored
                        delta_charge += 0.05
                        _kd("sad", 0.08)
                        _kd("bored", 0.08)
                    else:
                        delta_charge += 0.08
                        _kd("happy", 0.12)
                        _kd("teasing", 0.05)
                        
                elif matched_sad:
                    word = matched_sad[0]
                    if check_negation(word):
                        # Не грусти / не плачь -> happy/love
                        delta_charge += 0.08
                        _kd("happy", 0.06)
                        _kd("love", 0.04)
                    else:
                        delta_charge += 0.06
                        _kd("sad", 0.15)
                        _kd("love", 0.10)
                        # Сопереживание: гасим игривость/радость, чтобы не «веселиться» в ответ на боль
                        _kd("happy", -0.10)
                        _kd("teasing", -0.10)
                        
                elif matched_angry:
                    word = matched_angry[0]
                    if check_negation(word):
                        # Без обид / не злись -> happy/love
                        delta_charge += 0.08
                        _kd("happy", 0.06)
                        _kd("love", 0.04)
                    else:
                        delta_charge += 0.12
                        # Предохранитель агрессии
                        if closeness >= 0.4:
                            _kd("angry", 0.2)
                        else:
                            _kd("bored", 0.15)
                            _kd("thinking", 0.05)

                # Применяем словарный сдвиг сразу ТОЛЬКО в legacy-режиме (defer_sentiment=False).
                # В гибридном пути (defer_sentiment=True) дельту применяет apply_turn_sentiment
                # пост-генерации — либо из интроспекции LLM, либо этим же keyword_mood_delta (фолбэк).
                if not defer_sentiment:
                    for _em, _d in keyword_mood_delta.items():
                        if _em in mood_dict:
                            mood_dict[_em] = min(max(mood_dict[_em] + _d, 0.0), 1.0)

                # Применяем циркадный сдвиг к вектору (DB-03: now(timezone.utc) вместо
                # устаревшего utcnow(); локальный час пользователя считается так же).
                if user_tz is not None:
                    hour = (datetime.now(timezone.utc) + timedelta(hours=user_tz)).hour
                else:
                    hour = datetime.now().hour

                if 0 <= hour < 5:
                    mood_dict["thinking"] = min(mood_dict["thinking"] + 0.12, 1.0)
                    mood_dict["bored"] = min(mood_dict["bored"] + 0.08, 1.0)
                    if closeness > 0.7:
                        mood_dict["love"] = min(mood_dict["love"] + 0.1, 1.0)
                elif 5 <= hour < 12:
                    mood_dict["happy"] = min(mood_dict["happy"] + 0.1, 1.0)
                    mood_dict["bored"] = max(mood_dict["bored"] - 0.08, 0.0)
                
                # Итоговый заряд
                charge = max(0.0, min(charge + delta_charge, 1.0))
                
                # 4. Записываем обратно
                mood_json = json.dumps(mood_dict, ensure_ascii=False)
                
                # Логируем изменение заряда для отладки и прозрачности
                old_charge = state["charge"]
                decayed_charge = state["charge"] * math.exp(-lambda_c * delta_t)
                
                log_entry_msg = (
                    f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] Обновление для chat_id={chat_id}:\n"
                    f"  - Прошло времени с активности: {delta_t:.1f} сек.\n"
                    f"  - Исходный заряд: {old_charge:.3f} -> После распада (Decay): {decayed_charge:.3f}\n"
                    f"  - Прибавка за реплику (user_message): +{delta_charge:.3f}\n"
                    f"  - Итоговый заряд чата (Charge): {charge:.3f}/1.000\n"
                    f"  - Текущий вектор настроений Арти: {mood_json}"
                )
                logger.info(log_entry_msg)
                
                # Записываем плоский структурированный лог в logs/emotional.log
                flat_log_entry = (
                    f"[UPDATE_STATE] chat_id={chat_id} | "
                    f"TimePassed={delta_t:.1f}s | "
                    f"OldCharge={old_charge:.3f} -> Decayed={decayed_charge:.3f} | "
                    f"Delta={delta_charge:.3f} | "
                    f"FinalCharge={charge:.3f} | "
                    f"Moods={mood_json}"
                )
                emotional_logger.info(flat_log_entry)

                row = await conn.fetchrow("""
                    UPDATE chat_emotional_states
                    SET charge = $2,
                        mood_state = $3::jsonb,
                        last_activity_time = NOW(),
                        conversation_stage = 'active',
                        user_tz = $4
                    WHERE chat_id = $1
                    RETURNING *
                """, chat_id, charge, mood_json, user_tz)
                result = dict(row)
                result["was_proactive_reply"] = was_proactive_reply
                # Словарный сдвиг отдаём наружу как fail-closed фолбэк для apply_turn_sentiment.
                result["keyword_mood_delta"] = keyword_mood_delta
                result['repeated_input'] = False
                if source_key is not None:
                    await conn.execute("""
                        INSERT INTO legacy_emotional_effects(chat_id,source_key,phase,result)
                        VALUES($1,$2,'input',$3::jsonb)
                    """, chat_id, source_key, json.dumps({'keyword_mood_delta':keyword_mood_delta},ensure_ascii=False))
                return result

    @staticmethod
    async def apply_mood_delta(chat_id: int, mood_delta: dict, source: str = "llm", source_key: Optional[str] = None) -> None:
        """Применяет ограниченные дельты к вектору настроения (БЕЗ распада/заряда/времени).

        Используется пост-генерации: приоритетный источник — интроспекция LLM (source="llm"),
        иначе fail-closed фолбэк на словарь (source="keyword"). Каждая дельта клампится в
        [-0.25, 0.25], итоговое значение — в [0, 1]; ключи вне whitelist из 9 эмоций игнорируются.
        """
        if not mood_delta and source_key is None:
            return
        mood_delta = mood_delta or {}
        applied: dict = {}
        async with get_db() as conn:
            async with conn.transaction():
                state = await conn.fetchrow(
                    "SELECT mood_state FROM chat_emotional_states WHERE chat_id = $1 FOR UPDATE",
                    chat_id,
                )
                if not state:
                    return
                if source_key is not None:
                    inserted = await conn.fetchval("""
                        INSERT INTO legacy_emotional_effects(chat_id,source_key,phase)
                        VALUES($1,$2,'sentiment') ON CONFLICT DO NOTHING RETURNING 1
                    """, chat_id, source_key)
                    if inserted is None:
                        return
                mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
                for emotion, d in mood_delta.items():
                    if emotion not in SUPPORTED_MOODS or emotion not in mood_dict:
                        continue
                    if not isinstance(d, (int, float)) or isinstance(d, bool):
                        continue
                    if not math.isfinite(d):
                        continue
                    d = max(-0.25, min(0.25, float(d)))
                    new_val = min(max(mood_dict[emotion] + d, 0.0), 1.0)
                    # Обнуляем денормализованные «хвосты», чтобы /charge не пестрел мусором 3e-175
                    new_val = new_val if new_val >= 0.0005 else 0.0
                    applied[emotion] = round(new_val - mood_dict[emotion], 4)
                    mood_dict[emotion] = new_val
                if not applied:
                    return
                mood_json = json.dumps(mood_dict, ensure_ascii=False)
                await conn.execute(
                    "UPDATE chat_emotional_states SET mood_state = $2::jsonb WHERE chat_id = $1",
                    chat_id, mood_json,
                )
        logger.info(f"🔮 [MOOD_DELTA:{source}] chat_id={chat_id} | applied={applied}")
        emotional_logger.info(f"[MOOD_DELTA] chat_id={chat_id} | source={source} | applied={applied}")

    @staticmethod
    async def apply_turn_sentiment(chat_id: int, arti_response_text: str, keyword_mood_delta: Optional[dict] = None, source_key: Optional[str] = None) -> Optional[str]:
        """Гибридный сентимент пост-генерации.

        Приоритет — интроспекция самой LLM (тег <!-- emotional_introspection --> из ответа Арти).
        Если тег отсутствует/битый/вне диапазона — fail-closed фолбэк на словарный
        keyword_mood_delta (посчитанный в update_state). Возвращает предложенный моод стикера
        (sticker_mood_suggest) или None.
        """
        parsed = parse_emotional_introspection(arti_response_text)
        if parsed is not None:
            if parsed["mood_delta"]:
                await ChatEmotionalState.apply_mood_delta(chat_id, parsed["mood_delta"], source="llm", source_key=source_key)
            else:
                await ChatEmotionalState.apply_mood_delta(chat_id, {}, source_key=source_key)
                emotional_logger.info(
                    f"[INTROSPECTION] chat_id={chat_id} | mood_delta пуст, sticker_suggest={parsed['sticker_mood_suggest']}"
                )
            return parsed["sticker_mood_suggest"]
        # Тег отсутствует/битый -> словарный фолбэк (fail-closed)
        if keyword_mood_delta:
            await ChatEmotionalState.apply_mood_delta(chat_id, keyword_mood_delta, source="keyword", source_key=source_key)
        return None

    @staticmethod
    async def record_sticker_sent(chat_id: int, file_id: str, mood: str):
        async with get_db() as conn:
            async with conn.transaction():
                state = await conn.fetchrow("""
                    SELECT * FROM chat_emotional_states WHERE chat_id = $1 FOR UPDATE
                """, chat_id)
                
                if not state:
                    return
                
                history = json.loads(state["sticker_history"]) if isinstance(state["sticker_history"], str) else state["sticker_history"]
                if not isinstance(history, list):
                    history = []
                
                # Анти-повтор: пишем последние 3 стикера
                history.append(file_id)
                history = history[-3:]
                
                # Delivery affects expression history only; it is not an emotional cause.
                await conn.execute("""
                    UPDATE chat_emotional_states
                    SET last_sticker_time = NOW(), sticker_history = $2::jsonb,
                        last_sent_sticker_mood = $3
                    WHERE chat_id = $1
                """, chat_id, json.dumps(history, ensure_ascii=False), mood)
