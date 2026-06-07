import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from google.genai import types

from config import genai_client
from database.models import MemoryUserProfile
from memory.normalizer import compact_text

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# Плейсхолдер, который кладут аффективные методы (grow_closeness / apply_reinforcement)
# в свежесозданный профиль. Пока profile_text равен ему, смыслового профиля ещё нет.
PROFILE_PLACEHOLDER = "Новый субъект общения."

_SYSTEM_PROMPT = """Ты — модуль Core Memory Telegram-бота Арти.
Собери устойчивый профиль пользователя из фактов и сущностей.
Не включай одноразовые эмоции, случайные фразы, технический мусор и сомнительные выводы.
Верни строго JSON без markdown:
{
  "display_name": "имя пользователя или пустая строка",
  "stable_preferences": ["..."],
  "communication_style": ["..."],
  "important_facts": ["..."],
  "relationship_to_arti": ["..."],
  "profile_text": "короткий цельный профиль на русском, 600-1200 символов максимум"
}"""


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


def _serialize_source(material: Dict[str, List[dict]]) -> str:
    facts = []
    for fact in material.get("facts") or []:
        created_at = fact.get("created_at")
        facts.append({
            "id": fact.get("id"),
            "text": compact_text(fact.get("fact_text") or fact.get("summary") or "", 500),
            "importance": float(fact.get("importance") or 0.5),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        })

    entities = []
    for entity in material.get("entities") or []:
        entities.append({
            "id": entity.get("id"),
            "name": compact_text(entity.get("canonical_name") or entity.get("normalized_name") or "", 120),
            "type": entity.get("entity_type") or "unknown",
            "mention_count": int(entity.get("mention_count") or 0),
        })

    return json.dumps({"facts": facts, "entities": entities}, ensure_ascii=False, indent=2)


def _normalize_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = {}
    for key in ["display_name", "profile_text"]:
        profile[key] = compact_text(str(payload.get(key) or ""), 1200 if key == "profile_text" else 120)

    for key in ["stable_preferences", "communication_style", "important_facts", "relationship_to_arti"]:
        values = payload.get(key) or []
        if not isinstance(values, list):
            values = []
        profile[key] = [compact_text(str(item), 240) for item in values[:12] if str(item).strip()]

    if not profile["profile_text"]:
        parts = []
        for key in ["stable_preferences", "communication_style", "important_facts", "relationship_to_arti"]:
            parts.extend(profile[key])
        profile["profile_text"] = compact_text("; ".join(parts), 1200)

    return profile


async def refresh_user_profile(
    chat_id: int,
    user_id: int,
    mode: str = "default",
    dry_run: bool = True,
) -> Dict[str, Any]:
    material = await MemoryUserProfile.fetch_source_material(chat_id=chat_id, user_id=user_id, mode=mode)
    if not material.get("facts") and not material.get("entities"):
        return {"status": "skipped", "reason": "empty_source", "dry_run": dry_run}

    system_instruction = _SYSTEM_PROMPT
    if mode == "rp":
        system_instruction = (
            "Ты — модуль Core Memory для игрового режима Roleplay (RP) бота Арти (в роли Телемы).\n"
            "Собери устойчивый игровой профиль персонажа пользователя из предоставленных RP-фактов и сущностей.\n"
            "Не включай технические факты о программировании, реальном мире или о том, что Арти — это ИИ.\n"
            "Сфокусируйся на: имени его персонажа, фракции, способностях, игровом выборе и истории отношений с Телемой в RP-мире.\n"
            "Верни строго JSON без markdown:\n"
            "{\n"
            "  \"display_name\": \"имя игрового персонажа пользователя или пустая строка\",\n"
            "  \"stable_preferences\": [\"игровые предпочтения, тактика, любимое оружие/заклинания\"],\n"
            "  \"communication_style\": [\"манера поведения персонажа в игре\"],\n"
            "  \"important_facts\": [\"важные игровые события, фракция, раса, ключевые выборы\"],\n"
            "  \"relationship_to_arti\": [\"отношение его персонажа к Телеме в игровом сеттинге\"],\n"
            "  \"profile_text\": \"короткий цельный профиль игрового персонажа на русском, 600-1200 символов максимум\"\n"
            "}"
        )

    response = await asyncio.to_thread(
        genai_client.models.generate_content,
        model="gemini-3.1-flash-lite-preview",
        contents=f"{system_instruction}\n\nSOURCE:\n{_serialize_source(material)}",
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=1800,
            response_mime_type="application/json",
        ),
    )

    payload = _parse_json(response.text if response and response.text else "")
    profile = _normalize_profile(payload)
    source_fact_ids = [int(row["id"]) for row in material.get("facts") or [] if row.get("id")]
    source_entity_ids = [int(row["id"]) for row in material.get("entities") or [] if row.get("id")]

    report = {
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "profile": profile,
        "source_fact_count": len(source_fact_ids),
        "source_entity_count": len(source_entity_ids),
    }

    if dry_run:
        return report

    # Сохраняем аффективный блок (closeness/receptivity и т.п.), который ведут
    # grow_closeness / apply_reinforcement. Иначе upsert смыслового профиля затрёт
    # его целиком — а от closeness зависит проактив.
    profile_json = dict(profile)
    existing = await MemoryUserProfile.get(chat_id=chat_id, user_id=user_id, mode=mode)
    if existing:
        existing_json = existing.get("profile_json")
        if isinstance(existing_json, str):
            try:
                existing_json = json.loads(existing_json)
            except Exception:
                existing_json = {}
        if isinstance(existing_json, dict) and isinstance(existing_json.get("affective"), dict):
            profile_json["affective"] = existing_json["affective"]

    profile_json["meta"] = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "source_fact_count": len(source_fact_ids),
        "source_entity_count": len(source_entity_ids),
    }

    profile_id = await MemoryUserProfile.upsert(
        chat_id=chat_id,
        user_id=user_id,
        mode=mode,
        profile_json=profile_json,
        profile_text=profile.get("profile_text") or "",
        source_fact_ids=source_fact_ids,
        source_entity_ids=source_entity_ids,
    )
    report["profile_id"] = profile_id
    return report


def _profile_needs_refresh(profile: Dict[str, Any], min_interval_sec: int) -> bool:
    """Профиль нужно перестроить, если смыслового текста ещё нет (плейсхолдер/пусто)
    либо прошло достаточно времени с последнего перестроения (троттлинг)."""
    if not profile:
        return True
    profile_text = (profile.get("profile_text") or "").strip()
    if not profile_text or profile_text == PROFILE_PLACEHOLDER:
        return True

    profile_json = profile.get("profile_json")
    if isinstance(profile_json, str):
        try:
            profile_json = json.loads(profile_json)
        except Exception:
            profile_json = {}
    if not isinstance(profile_json, dict):
        return True

    refreshed_at = (profile_json.get("meta") or {}).get("refreshed_at")
    if not refreshed_at:
        # Смысловой текст есть, но без метки времени (например, старая ручная сборка) —
        # не дёргаем модель сразу, считаем свежим до следующего интервала.
        return False
    try:
        last = datetime.fromisoformat(refreshed_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= min_interval_sec


async def maybe_refresh_user_profile(
    chat_id: int,
    user_id: int,
    mode: str = "default",
    min_interval_sec: int = 1800,
) -> Dict[str, Any]:
    """Перестраивает смысловой профиль из накопленных фактов, если он ещё плейсхолдер
    или устарел. Троттлится по metadata, чтобы не дёргать LLM на каждом сообщении.
    Аффективный блок (closeness/receptivity) сохраняется внутри refresh_user_profile."""
    if not chat_id or not user_id:
        return {"status": "skipped", "reason": "no_ids"}

    profile = await MemoryUserProfile.get(chat_id=chat_id, user_id=user_id, mode=mode)
    if not _profile_needs_refresh(profile, min_interval_sec):
        return {"status": "skipped", "reason": "fresh"}

    return await refresh_user_profile(chat_id=chat_id, user_id=user_id, mode=mode, dry_run=False)


async def get_profile_context(chat_id: int, user_id: int, mode: str = "default") -> str:
    profile = await MemoryUserProfile.get(chat_id=chat_id, user_id=user_id, mode=mode)
    if not profile:
        return ""
    text = compact_text(profile.get("profile_text") or "", 1000)
    if not text:
        return ""
    return f"Core profile:\n{text}"
