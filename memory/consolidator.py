import asyncio
import json
import logging
import re
from typing import Any, Dict, List

from google.genai import types

from config import genai_client
from database.models import MemoryFact, MemoryWikiPage
from memory.normalizer import compact_text

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MIN_FACTS_TO_CONSOLIDATE = 8

_SYSTEM_PROMPT = """Ты — модуль консолидации долговременной памяти Telegram-бота Арти.
Изучи список фактов о пользователе/чате. Объедини семантические дубликаты.
Если факты противоречат друг другу, оставь более свежий факт по created_at.
Не трогай фундаментальные факты: на вход уже подаются только importance < 0.8.

Также выяви темы или факты, которые повторяются, стабильны и представляют собой важные знания о характере Арти/Телемы, правилах мира или устойчивых отношениях с пользователем. Предложи перенести их в вечную базу знаний Wiki (wiki_suggestions).
Верни строго JSON без markdown:
{
  "facts": [
    {"text": "актуальный самостоятельный факт", "importance": 0.0-0.79, "source_ids": [1,2]}
  ],
  "archive_ids": [1,2],
  "wiki_suggestions": [
    {
      "page_key": "page_key_on_english_lowercase_with_underscores",
      "title": "заголовок страницы лора на русском",
      "content": "структурированный, подробный текст страницы лора на основе фактов (600-1500 символов на русском)",
      "category": "personality|world_lore|rp_rules|relationships",
      "source_fact_ids": [1,2]
    }
  ]
}
archive_ids должны содержать только ID фактов, которые заменяются или переносятся в Wiki.
Если консолидация и предложения не нужны, верни {"facts": [], "archive_ids": [], "wiki_suggestions": []}."""


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


def _serialize_candidates(facts: List[dict]) -> str:
    payload = []
    for fact in facts:
        created_at = fact.get("created_at")
        payload.append({
            "id": fact.get("id"),
            "fact_text": compact_text(fact.get("fact_text") or "", 500),
            "summary": compact_text(fact.get("summary") or "", 240),
            "importance": float(fact.get("importance") or 0.5),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_plan(payload: Dict[str, Any], allowed_ids: set[int]) -> Dict[str, Any]:
    raw_facts = payload.get("facts") or []
    raw_archive_ids = payload.get("archive_ids") or []
    raw_suggestions = payload.get("wiki_suggestions") or []

    archive_ids = []
    for item in raw_archive_ids:
        try:
            fact_id = int(item)
        except (TypeError, ValueError):
            continue
        if fact_id in allowed_ids and fact_id not in archive_ids:
            archive_ids.append(fact_id)

    facts = []
    seen_texts = set()
    if isinstance(raw_facts, list):
        for item in raw_facts[:40]:
            if not isinstance(item, dict):
                continue
            text = compact_text(item.get("text") or item.get("fact") or "", 700)
            if not text:
                continue
            key = text.lower().replace("ё", "е").strip()
            if key in seen_texts:
                continue
            seen_texts.add(key)

            source_ids = []
            for source_id in item.get("source_ids") or []:
                try:
                    source_id = int(source_id)
                except (TypeError, ValueError):
                    continue
                if source_id in allowed_ids and source_id not in source_ids:
                    source_ids.append(source_id)
                    if source_id not in archive_ids:
                        archive_ids.append(source_id)

            try:
                importance = float(item.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            importance = max(0.0, min(importance, 0.79))
            facts.append({"text": text, "importance": importance, "source_ids": source_ids})

    suggestions = []
    if isinstance(raw_suggestions, list):
        for item in raw_suggestions[:5]:
            if not isinstance(item, dict):
                continue
            page_key = compact_text(item.get("page_key") or "", 80).strip().lower().replace(" ", "_")
            title = compact_text(item.get("title") or "", 255).strip()
            content = compact_text(item.get("content") or "", 2000).strip()
            category = compact_text(item.get("category") or "world_lore", 64).strip()
            if not page_key or not title or not content:
                continue
            
            source_ids = []
            for source_id in item.get("source_fact_ids") or []:
                try:
                    source_id = int(source_id)
                except (TypeError, ValueError):
                    continue
                if source_id in allowed_ids and source_id not in source_ids:
                    source_ids.append(source_id)
                    if source_id not in archive_ids:
                        archive_ids.append(source_id)
                        
            suggestions.append({
                "page_key": page_key,
                "title": title,
                "content": content,
                "category": category,
                "source_fact_ids": source_ids
            })

    return {"facts": facts, "archive_ids": archive_ids, "wiki_suggestions": suggestions}


async def consolidate_chat_facts(
    chat_id: int,
    mode: str = "default",
    limit: int = 80,
    dry_run: bool = True,
) -> Dict[str, Any]:
    candidates = await MemoryFact.fetch_for_consolidation(chat_id=chat_id, mode=mode, limit=limit)
    if len(candidates) < _MIN_FACTS_TO_CONSOLIDATE:
        return {
            "status": "skipped",
            "reason": "not_enough_candidates",
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "facts": [],
            "archive_ids": [],
        }

    allowed_ids = {int(fact["id"]) for fact in candidates if fact.get("id")}
    prompt = (
        f"chat_id: {chat_id}\n"
        f"mode: {mode}\n\n"
        f"FACTS:\n{_serialize_candidates(candidates)}"
    )

    response = await asyncio.to_thread(
        genai_client.models.generate_content,
        model="gemini-3.1-flash-lite-preview",
        contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=2200,
            response_mime_type="application/json",
        ),
    )

    raw_payload = _parse_json(response.text if response and response.text else "")
    plan = _normalize_plan(raw_payload, allowed_ids)
    report = {
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "new_fact_count": len(plan["facts"]),
        "archive_count": len(plan["archive_ids"]),
        "wiki_suggestion_count": len(plan["wiki_suggestions"]),
        "facts": plan["facts"],
        "archive_ids": plan["archive_ids"],
        "wiki_suggestions": plan["wiki_suggestions"],
    }

    if dry_run:
        return report

    # 1. Сохраняем новые Wiki-предложения как не верифицированные.
    #    M-05: не перезаписываем уже существующую ВЕРИФИЦИРОВАННУЮ страницу —
    #    иначе LLM-предложение сбросило бы ручное подтверждение (is_verified).
    suggested_wiki_ids = []
    skipped_verified = 0
    for sug in plan["wiki_suggestions"]:
        existing = await MemoryWikiPage.get_by_key(chat_id=chat_id, mode=mode, page_key=sug["page_key"])
        if existing and existing.get("is_verified"):
            skipped_verified += 1
            continue
        page_id = await MemoryWikiPage.save(
            page_key=sug["page_key"],
            title=sug["title"],
            content=sug["content"],
            category=sug["category"],
            chat_id=chat_id,
            mode=mode,
            importance=0.6,
            is_verified=False  # Требуется ручное подтверждение!
        )
        if page_id:
            suggested_wiki_ids.append(page_id)
    report["suggested_wiki_ids"] = suggested_wiki_ids
    report["skipped_verified_wiki"] = skipped_verified

    if not plan["archive_ids"]:
        return report

    created_ids = []
    first_new_id = None
    for fact in plan["facts"]:
        fact_id = await MemoryFact.create(
            chat_id=chat_id,
            mode=mode,
            fact_text=fact["text"],
            summary="Сконсолидированный факт памяти",
            importance=fact["importance"],
            metadata={"kind": "consolidated", "source_ids": fact.get("source_ids") or []},
        )
        if fact_id:
            created_ids.append(fact_id)
            if first_new_id is None:
                first_new_id = fact_id

    archived_count = await MemoryFact.archive_many(
        plan["archive_ids"],
        reason="consolidated",
        superseded_by=first_new_id,
    )
    report["created_ids"] = created_ids
    report["archived_count"] = archived_count
    return report


async def maybe_consolidate(
    chat_id: int,
    mode: str = "default",
    limit: int = 80,
    dry_run: bool = True,
) -> Dict[str, Any]:
    try:
        report = await consolidate_chat_facts(chat_id=chat_id, mode=mode, limit=limit, dry_run=dry_run)
        logger.info("Memory consolidation report: %s", json.dumps(report, ensure_ascii=False)[:1200])
        return report
    except Exception as e:
        logger.warning(f"Ошибка консолидации памяти: {e}")
        return {"status": "error", "error": str(e), "dry_run": dry_run}
