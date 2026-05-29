import asyncio
import json
import logging
import re
from typing import Any, Dict, List

from google.genai import types

from config import MEMORY_TIMELINE_MIN_MESSAGES, genai_client
from database.models import MemoryTimeline
from memory.normalizer import compact_text, keyword_query

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = """Ты — модуль сжатой хронологии Telegram-бота Арти.
Сожми последовательность сообщений в 1-5 устойчивых timeline events.
Сохраняй только значимые события, решения, изменения отношений, важные обсуждения и долгоживущие темы.
Не сохраняй приветствия, одноразовый флирт без последствий, технический шум и пустые сообщения.
Верни строго JSON без markdown:
{
  "events": [
    {
      "title": "короткий заголовок",
      "summary": "сжатое описание события/периода",
      "topics": ["topic"],
      "source_message_ids": [1,2,3]
    }
  ]
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


def _serialize_messages(messages: List[dict]) -> str:
    payload = []
    for message in messages:
        created_at = message.get("created_at")
        payload.append({
            "id": message.get("id"),
            "user_id": message.get("user_id"),
            "user_name": message.get("user_name"),
            "role": message.get("role"),
            "text": compact_text(message.get("message_text") or "", 600),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_events(payload: Dict[str, Any], allowed_ids: set[int]) -> List[Dict[str, Any]]:
    raw_events = payload.get("events") or []
    events = []
    if not isinstance(raw_events, list):
        return events

    for item in raw_events[:5]:
        if not isinstance(item, dict):
            continue
        summary = compact_text(item.get("summary") or "", 900)
        if not summary:
            continue
        title = compact_text(item.get("title") or "", 160)
        topics = [compact_text(str(topic), 60) for topic in (item.get("topics") or [])[:12] if str(topic).strip()]
        source_ids = []
        for source_id in item.get("source_message_ids") or []:
            try:
                source_id = int(source_id)
            except (TypeError, ValueError):
                continue
            if source_id in allowed_ids and source_id not in source_ids:
                source_ids.append(source_id)
        if not source_ids:
            continue
        events.append({"title": title, "summary": summary, "topics": topics, "source_message_ids": source_ids})
    return events


async def build_timeline_events(
    chat_id: int,
    mode: str = "default",
    limit: int = 200,
    dry_run: bool = True,
) -> Dict[str, Any]:
    after_id = await MemoryTimeline.latest_source_message_id(chat_id=chat_id, mode=mode)
    messages = await MemoryTimeline.fetch_messages_for_period(chat_id=chat_id, mode=mode, after_id=after_id, limit=limit)
    if len(messages) < MEMORY_TIMELINE_MIN_MESSAGES:
        return {
            "status": "skipped",
            "reason": "not_enough_messages",
            "dry_run": dry_run,
            "message_count": len(messages),
            "after_id": after_id,
            "events": [],
        }

    allowed_ids = {int(message["id"]) for message in messages if message.get("id")}
    system_instruction = _SYSTEM_PROMPT
    if mode == "rp":
        system_instruction = (
            "Ты — модуль сжатой хронологии приключений для игрового режима Roleplay (RP) бота Арти (в роли Телемы).\n"
            "Сожми последовательность игровых сообщений в 1-5 устойчивых игровых глав/событий (timeline events).\n"
            "Сохраняй только значимые повороты сюжета, принятые решения, битвы, важные открытия, полученные предметы и изменение отношений персонажей.\n"
            "Не сохраняй технический флуд, OOC-разговоры и пустые сообщения.\n"
            "Верни строго JSON без markdown:\n"
            "{\n"
            "  \"events\": [\n"
            "    {\n"
            "      \"title\": \"заголовок игровой главы/события\",\n"
            "      \"summary\": \"краткое художественное описание произошедшего события/периода на русском\",\n"
            "      \"topics\": [\"игровые темы, например: побег, бой, тайна, фракция\"],\n"
            "      \"source_message_ids\": [1,2,3]\n"
            "    }\n"
            "  ]\n"
            "}"
        )

    response = await asyncio.to_thread(
        genai_client.models.generate_content,
        model="gemini-3.1-flash-lite-preview",
        contents=f"{system_instruction}\n\nMESSAGES:\n{_serialize_messages(messages)}",
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=2200,
            response_mime_type="application/json",
        ),
    )

    payload = _parse_json(response.text if response and response.text else "")
    events = _normalize_events(payload, allowed_ids)
    report = {
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "message_count": len(messages),
        "after_id": after_id,
        "event_count": len(events),
        "events": events,
    }

    if dry_run:
        return report

    created_ids = []
    message_by_id = {int(message["id"]): message for message in messages if message.get("id")}
    for event in events:
        source_messages = [message_by_id[source_id] for source_id in event["source_message_ids"] if source_id in message_by_id]
        period_start = source_messages[0].get("created_at") if source_messages else None
        period_end = source_messages[-1].get("created_at") if source_messages else None
        user_id = source_messages[-1].get("user_id") if source_messages else None
        timeline_id = await MemoryTimeline.create(
            chat_id=chat_id,
            user_id=user_id,
            mode=mode,
            period_start=period_start,
            period_end=period_end,
            title=event["title"],
            summary=event["summary"],
            topics=event["topics"],
            source_message_ids=event["source_message_ids"],
            metadata={"kind": "timeline_event"},
        )
        if timeline_id:
            created_ids.append(timeline_id)
    report["created_ids"] = created_ids
    return report


async def get_timeline_context(chat_id: int, mode: str = "default", query: str = "", limit: int = 3) -> str:
    search_query = keyword_query(query) or compact_text(query, 120)
    rows = await MemoryTimeline.search(chat_id=chat_id, mode=mode, query=search_query, limit=limit)
    if not rows:
        return ""

    lines = ["Сжатая хронология:"]
    for row in rows[:limit]:
        title = compact_text(row.get("title") or "", 100)
        summary = compact_text(row.get("summary") or "", 320)
        period_end = row.get("period_end")
        date = period_end.strftime("%Y-%m-%d") if hasattr(period_end, "strftime") else ""
        prefix = f"[{date}] " if date else ""
        if title and summary:
            lines.append(f"- {prefix}{title}: {summary}")
        elif summary:
            lines.append(f"- {prefix}{summary}")
    return "\n".join(lines)
