"""Чистая логика эмоциональной машины Арти (ARCH-01).

Вынесено из database/models.py: разбор/очистка скрытого тега интроспекции и
whitelist эмоций. Здесь НЕТ обращений к БД — только чистые функции, их легко
тестировать. database/models.py ре-экспортирует эти имена для обратной
совместимости (внешний код импортирует их как `from database.models import ...`).
"""
from __future__ import annotations

import json
import re
from typing import Optional

# 9 базовых эмоций — единственный whitelist для дельт настроения (и LLM-тега, и словаря).
SUPPORTED_MOODS = {"happy", "sad", "angry", "love", "teasing", "shock", "blush", "bored", "thinking"}

# Скрытый служебный тег интроспекции, который модель дописывает в КОНЕЦ ответа:
# <!-- emotional_introspection: {"mood_delta": {"love": 0.15}, "sticker_mood_suggest": "love"} -->
_INTROSPECTION_RE = re.compile(r"<!--\s*emotional_introspection\s*:\s*(\{.*?\})\s*-->", re.DOTALL | re.IGNORECASE)
# Незакрытый/обрезанный хвост тега (на случай, если модель не дописала комментарий).
_INTROSPECTION_PARTIAL_RE = re.compile(r"<!--\s*emotional_introspection\b.*$", re.DOTALL | re.IGNORECASE)


def parse_emotional_introspection(text: Optional[str]) -> Optional[dict]:
    """Строгий fail-closed парсер тега интроспекции из СГЕНЕРИРОВАННОГО текста Арти.

    Возвращает {"mood_delta": {emotion: float}, "sticker_mood_suggest": str|None}
    либо None, если тег отсутствует, JSON битый, или в нём нет ничего пригодного.
    Дельты клампятся в [-0.25, 0.25]; ключи вне whitelist из 9 эмоций отбрасываются.
    """
    if not text:
        return None
    match = _INTROSPECTION_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    mood_delta: dict = {}
    raw_delta = data.get("mood_delta", {})
    if isinstance(raw_delta, dict):
        for key, val in raw_delta.items():
            if key in SUPPORTED_MOODS and isinstance(val, (int, float)) and not isinstance(val, bool):
                mood_delta[key] = max(-0.25, min(0.25, float(val)))

    suggest = data.get("sticker_mood_suggest")
    if isinstance(suggest, str) and suggest.strip().lower() in SUPPORTED_MOODS:
        suggest = suggest.strip().lower()
    else:
        suggest = None

    # Тег есть, но в нём нет ни валидной дельты, ни валидного стикера -> считаем браком.
    if not mood_delta and suggest is None:
        return None
    return {"mood_delta": mood_delta, "sticker_mood_suggest": suggest}


def strip_introspection_tags(text: Optional[str]) -> str:
    """Вырезает служебный тег интроспекции (и его незакрытый хвост) из текста."""
    if not text:
        return text or ""
    text = _INTROSPECTION_RE.sub("", text)
    text = _INTROSPECTION_PARTIAL_RE.sub("", text)
    return text.strip()
