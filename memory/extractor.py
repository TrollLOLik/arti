import asyncio
import json
import logging
import re
from typing import Any, Dict

from google.genai import types

from config import genai_client
from memory.normalizer import compact_text

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


_SYSTEM_PROMPT = """Ты — модуль памяти Telegram-бота Арти. Извлеки только устойчивые воспоминания из пары user/assistant.
Ответь строго JSON без markdown:
{
  "summary": "краткое содержание диалога в 1 предложении или пустая строка",
  "facts": [{"text": "самостоятельный факт", "importance": 0.0-1.0}],
  "entities": [{"name": "каноническое имя", "type": "person|topic|place|project|preference|other", "aliases": []}],
  "relations": [{"source": "имя сущности", "target": "имя сущности", "type": "тип связи", "description": "короткое описание"}]
}
Не сохраняй одноразовые эмоции, приветствия, технический мусор, HTML-теги и пустые факты."""


def _parse_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        match = _JSON_RE.search(raw)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}


def _fallback(user_message: str, response_text: str) -> Dict[str, Any]:
    joined = compact_text(f"Пользователь: {user_message} Арти: {response_text}", 700)
    if len(joined) < 80:
        return {"summary": "", "facts": [], "entities": [], "relations": []}
    return {
        "summary": joined,
        "facts": [{"text": joined, "importance": 0.35}],
        "entities": [],
        "relations": [],
    }


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    facts = payload.get("facts") or []
    entities = payload.get("entities") or []
    relations = payload.get("relations") or []

    if isinstance(facts, list):
        normalized_facts = []
        for item in facts[:8]:
            if isinstance(item, str):
                text = item
                importance = 0.5
            elif isinstance(item, dict):
                text = item.get("text") or item.get("fact") or ""
                importance = item.get("importance", 0.5)
            else:
                continue
            text = compact_text(text, 600)
            if text:
                normalized_facts.append({"text": text, "importance": importance})
        facts = normalized_facts
    else:
        facts = []

    if isinstance(entities, list):
        normalized_entities = []
        for item in entities[:12]:
            if isinstance(item, str):
                name = item
                entity_type = "unknown"
                aliases = []
            elif isinstance(item, dict):
                name = item.get("name") or item.get("canonical_name") or ""
                entity_type = item.get("type") or item.get("entity_type") or "unknown"
                aliases = item.get("aliases") or []
            else:
                continue
            name = compact_text(name, 120)
            aliases = [compact_text(alias, 120) for alias in aliases[:8] if isinstance(alias, str)]
            if name:
                normalized_entities.append({"name": name, "type": entity_type, "aliases": aliases})
        entities = normalized_entities
    else:
        entities = []

    if isinstance(relations, list):
        normalized_relations = []
        for item in relations[:12]:
            if not isinstance(item, dict):
                continue
            source = compact_text(item.get("source") or "", 120)
            target = compact_text(item.get("target") or "", 120)
            relation_type = compact_text(item.get("type") or item.get("relation_type") or "related_to", 80)
            description = compact_text(item.get("description") or "", 240)
            if source and target:
                normalized_relations.append({
                    "source": source,
                    "target": target,
                    "type": relation_type,
                    "description": description,
                })
        relations = normalized_relations
    else:
        relations = []

    return {
        "summary": compact_text(payload.get("summary") or "", 500),
        "facts": facts,
        "entities": entities,
        "relations": relations,
    }


async def extract_memory(user_message: str, response_text: str, user_name: str, mode: str = "default") -> Dict[str, Any]:
    mode_instruction = ""
    if mode == "rp":
        mode_instruction = (
            "[ИНСТРУКЦИЯ РЕЖИМА ROLEPLAY]:\n"
            "Сейчас активен игровой режим отыгрыша роли (RP). Извлекай исключительно сюжетные события, "
            "выборы персонажей, их фракции, способности, игровые достижения и лорные подробности вымышленной "
            "вселенной комплекса. Игнорируй любые технические утверждения о багах, программировании или о том, "
            "что Арти — ИИ-модель. Все извлеченные факты должны принадлежать фэнтези/игровой вселенной.\n\n"
        )
    else:
        mode_instruction = (
            "[ИНСТРУКЦИЯ ОБЫЧНОГО РЕЖИМА]:\n"
            "Сейчас активен обычный режим общения. Извлекай только реальные факты о пользователе: его вкусы, "
            "предпочтения, часовой пояс, хобби, реальную манеру общения и отношение к Арти как к созданному "
            "Александром андроиду. Не извлекай фэнтези-игровые сюжеты или выдуманный лор.\n\n"
        )

    prompt = (
        f"{mode_instruction}"
        f"mode: {mode}\n"
        f"user_name: {user_name}\n\n"
        f"USER:\n{compact_text(user_message, 1800)}\n\n"
        f"ARTI:\n{compact_text(response_text, 1800)}"
    )

    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-3.1-flash-lite-preview",
            contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=900,
                response_mime_type="application/json",
            ),
        )
        payload = _parse_json(response.text if response and response.text else "")
        if not payload:
            return _fallback(user_message, response_text)
        return _normalize_payload(payload)
    except Exception as e:
        logger.warning(f"Ошибка extractor-памяти: {e}")
        return _fallback(user_message, response_text)
