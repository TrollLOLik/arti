import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, List

from config import (
    MEMORY_CONSOLIDATION_APPLY,
    MEMORY_CONSOLIDATION_AUTO,
    MEMORY_CONSOLIDATION_INTERVAL,
    MEMORY_PROFILES_ENABLED,
    MEMORY_TIMELINE_ENABLED,
)
from database.models import MemoryChunk, MemoryEntity, MemoryFact, MemoryMessage, MemoryRelation, MemoryWikiPage
from memory.chunking import build_compact_chunks
from memory.consolidator import maybe_consolidate
from memory.embeddings import EMBEDDING_MODEL, embed_document, embed_query
from memory.extractor import extract_memory
from memory.normalizer import compact_text, keyword_query, normalize_entity_name
from memory.profiles import get_profile_context
from memory.timeline import get_timeline_context

logger = logging.getLogger(__name__)


def _format_relation(row: Dict[str, Any]) -> str:
    source = compact_text(row.get("source_name") or "Сущность", 80)
    target = compact_text(row.get("target_name") or "Сущность", 80)
    relation_type = compact_text(row.get("relation_type") or "связано с", 80)
    description = compact_text(row.get("description") or "", 180)
    if description:
        return f"{source} — {description} — {target}"
    return f"{source} — {relation_type} — {target}"


async def build_memory_context(
    chat_id: int,
    user_id: int,
    user_message: str,
    mode: str = "default",
    fact_limit: int = 5,
    log_limit: int = 2,
) -> str:
    query = keyword_query(user_message) or compact_text(user_message, 160)
    if not query:
        return ""

    try:
        # 1. Загрузка Wiki Lore
        wiki_personality = await MemoryWikiPage.get_by_key(chat_id=None, mode=mode, page_key="personality")
        wiki_pages = await MemoryWikiPage.search(chat_id=chat_id, mode=mode, query=user_message, limit=1)
        relevant_wiki = None
        if wiki_pages:
            candidate = wiki_pages[0]
            if candidate.get("page_key") != "personality":
                relevant_wiki = candidate

        profile_context = ""
        if MEMORY_PROFILES_ENABLED:
            profile_context = await get_profile_context(chat_id=chat_id, user_id=user_id, mode=mode)

        timeline_context = ""
        if MEMORY_TIMELINE_ENABLED:
            timeline_context = await get_timeline_context(chat_id=chat_id, mode=mode, query=user_message, limit=3)

        graph_entities = await MemoryEntity.find_mentions(chat_id=chat_id, text=user_message, limit=5)
        graph_relations = []
        all_related_entities = []
        if graph_entities:
            entity_ids = [entity.get("id") for entity in graph_entities]
            # Находим 1-hop и 2-hop связанные сущности
            all_related_entities = await MemoryEntity.find_related_entities(chat_id=chat_id, entity_ids=entity_ids, limit=10)
            all_entity_ids = [entity.get("id") for entity in all_related_entities]
            
            # Находим связи для всех этих сущностей (включая связи 2-го порядка)
            graph_relations = await MemoryRelation.find_for_entities(
                chat_id=chat_id,
                entity_ids=all_entity_ids,
                limit=10,
            )

        facts = await MemoryFact.search(chat_id=chat_id, query=query, mode=mode, limit=fact_limit)
        chunks = []
        query_vector = await embed_query(user_message)
        if query_vector:
            try:
                chunks = await MemoryChunk.search_vector(
                    chat_id=chat_id,
                    mode=mode,
                    query_vector=query_vector,
                    limit=log_limit,
                )
            except Exception as e:
                logger.warning(f"Vector retrieval недоступен, fallback на text retrieval: {e}")

        if not chunks:
            try:
                chunks = await MemoryChunk.search_text(chat_id=chat_id, mode=mode, query=query, limit=log_limit)
            except Exception as e:
                logger.warning(f"Chunk text retrieval недоступен: {e}")

        messages = []
        if not chunks:
            messages = await MemoryMessage.search(chat_id=chat_id, query=query, mode=mode, limit=log_limit)

        # ЗАГРУЗКА ЭМОЦИОНАЛЬНОГО СОСТОЯНИЯ И АФФЕКТИВНОГО ПРОФИЛЯ
        from database.models import ChatEmotionalState, MemoryUserProfile
        import json
        
        emo_state = await ChatEmotionalState.get_or_create(chat_id)
        charge = emo_state.get("charge", 0.0)
        mood_dict = json.loads(emo_state["mood_state"]) if isinstance(emo_state["mood_state"], str) else emo_state["mood_state"]
        
        # Поиск доминирующей эмоции
        dominant_mood = "thinking"
        max_val = -1.0
        for emotion, val in mood_dict.items():
            if val > max_val:
                max_val = val
                dominant_mood = emotion
        
        # Получение аффективного профиля пользователя
        closeness = 0.1
        sticker_receptivity = 0.5
        user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
        if user_profile and user_profile.get("profile_json"):
            prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
            aff = prof_json.get("affective", {})
            closeness = aff.get("closeness", 0.1)
            sticker_receptivity = aff.get("sticker_receptivity", 0.5)
        
        last_sent_mood = emo_state.get("last_sent_sticker_mood") or "нет"
        
        emo_line = (
            f"[ЭМОЦИОНАЛЬНЫЙ СТАТУС ДИАЛОГА]\n"
            f"- Твоя текущая близость с собеседником: {closeness:.2f} (0.0 — холодный незнакомец, 1.0 — твой близкий друг Александр).\n"
            f"- Восприимчивость пользователя к стикерам: {sticker_receptivity:.2f} (0.0 — не любит стикеры, 1.0 — обожает эмоции).\n"
            f"- Твой текущий эмоциональный заряд: {charge:.2f}/1.0 (стикеры отправляются только при высоком заряде).\n"
            f"- Твоя доминирующая эмоция сессии: {dominant_mood} (сила: {max_val:.2f}).\n"
            f"- Твой последний отправленный стикер: настроение {last_sent_mood}."
        )

        if not wiki_personality and not relevant_wiki and not profile_context and not timeline_context and not graph_entities and not graph_relations and not facts and not chunks and not messages:
            return emo_line

        lines = ["[Долговременная память Арти: используй только если релевантно текущему ответу]"]
        
        # Инжектируем характер из Wiki
        if wiki_personality:
            personality_title = "Характер Арти" if mode == "default" else "Характер и канон Телемы"
            lines.append(f"[{personality_title}]:\n{compact_text(wiki_personality.get('content') or '', 1500)}")

        # Встраиваем эмоциональный статус
        lines.append(emo_line)

        # Инжектируем релевантный лор
        if relevant_wiki:
            lines.append(f"[Релевантный лор вселенной ({relevant_wiki.get('title')})]:\n{compact_text(relevant_wiki.get('content') or '', 1500)}")

        if profile_context:
            lines.append(profile_context)

        if timeline_context:
            lines.append(timeline_context)

        if all_related_entities:
            # Разделяем на непосредственно упомянутые (score >= 10.0) и связанные (score < 10.0)
            direct_names = []
            related_names = []
            for entity in all_related_entities:
                name = compact_text(entity.get("canonical_name") or entity.get("normalized_name") or "", 80)
                if not name:
                    continue
                score = entity.get("score") or 0.0
                if score >= 10.0:
                    if name not in direct_names:
                        direct_names.append(name)
                else:
                    if name not in related_names and name not in direct_names:
                        related_names.append(name)
            
            if direct_names:
                lines.append("Сущности в текущем контексте: " + ", ".join(direct_names))
            if related_names:
                lines.append("Связанные сущности: " + ", ".join(related_names))

        if graph_relations:
            lines.append("Связи в текущем контексте:")
            seen_relations = set()
            for relation in graph_relations[:8]:
                text = _format_relation(relation)
                if not text or text in seen_relations:
                    continue
                seen_relations.add(text)
                lines.append(f"- {text}")

        fact_ids = []
        if facts:
            lines.append("Ассоциации:")
            for fact in facts:
                fact_ids.append(fact.get("id"))
                text = compact_text(fact.get("fact_text") or fact.get("summary") or "", 320)
                if text:
                    lines.append(f"- {text}")

        if chunks:
            lines.append("Семантически похожие фрагменты истории:")
            for chunk in chunks:
                text = compact_text(chunk.get("chunk_text") or "", 420)
                if text:
                    similarity = chunk.get("similarity")
                    suffix = f" (similarity={similarity:.3f})" if isinstance(similarity, float) else ""
                    lines.append(f"- {text}{suffix}")

        if messages:
            lines.append("Похожие прошлые сообщения:")
            for message in messages:
                user_name = message.get("user_name") or "Участник"
                text = compact_text(message.get("message_text") or "", 240)
                if text:
                    lines.append(f"- {user_name}: {text}")

        if fact_ids:
            await MemoryFact.mark_used(fact_ids)

        return "\n".join(lines[:14])
    except Exception as e:
        logger.warning(f"Ошибка чтения памяти: {e}")
        return ""


async def remember_exchange(
    chat_id: int,
    user_id: int,
    user_name: str,
    user_message: str,
    response_text: str,
    mode: str = "default",
    metadata: Dict[str, Any] = None,
):
    if not user_message or not response_text:
        return

    payload = await extract_memory(user_message, response_text, user_name, mode=mode)
    summary = payload.get("summary") or ""
    facts = payload.get("facts") or []
    entities = payload.get("entities") or []
    relations = payload.get("relations") or []

    if not summary and not facts and not entities and not relations:
        return

    source_message_id = await MemoryMessage.save(
        chat_id=chat_id,
        user_id=user_id,
        user_name="Экстрактор",
        role="memory_extractor",
        mode=mode,
        source="extractor",
        message_text=summary or compact_text(user_message, 500),
        metadata={"kind": "exchange_summary", **(metadata or {})},
    )

    try:
        chunk_messages = [{
            "id": source_message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "user_name": "Память",
            "role": "memory_extractor",
            "mode": mode,
            "message_text": summary or compact_text(f"{user_message}\n{response_text}", 900),
            "created_at": datetime.now(),
        }]
        chunks = build_compact_chunks(chunk_messages)
        for chunk in chunks:
            chunk_id = await MemoryChunk.create(**chunk)
            if not chunk_id:
                continue
            vector = await embed_document(chunk["chunk_text"])
            if vector:
                await MemoryChunk.set_embedding(chunk_id, vector, EMBEDDING_MODEL)
    except Exception as e:
        logger.warning(f"Не удалось создать embedding-чанк для exchange: {e}")

    entity_by_name: Dict[str, int] = {}
    for entity in entities:
        name = entity.get("name") or ""
        normalized = normalize_entity_name(name)
        if not normalized:
            continue
        row = await MemoryEntity.get_or_create(
            chat_id=chat_id,
            canonical_name=name,
            normalized_name=normalized,
            entity_type=entity.get("type") or "unknown",
            aliases=entity.get("aliases") or [],
        )
        if row:
            entity_by_name[normalize_entity_name(name)] = row["id"]
            for alias in entity.get("aliases") or []:
                alias_normalized = normalize_entity_name(alias)
                if alias_normalized:
                    entity_by_name[alias_normalized] = row["id"]

    seen_fact_texts = set()
    created_facts = 0
    for fact in facts:
        text = fact.get("text") if isinstance(fact, dict) else str(fact)
        text = compact_text(text, 700)
        if not text:
            continue
        fact_key = text.lower().replace("ё", "е").strip()
        if not fact_key or fact_key in seen_fact_texts:
            continue
        seen_fact_texts.add(fact_key)
        linked_entity_ids = [entity_id for normalized, entity_id in entity_by_name.items() if normalized in normalize_entity_name(text)]
        fact_id = await MemoryFact.create(
            chat_id=chat_id,
            user_id=user_id,
            mode=mode,
            summary=summary,
            fact_text=text,
            importance=fact.get("importance", 0.5) if isinstance(fact, dict) else 0.5,
            source_message_id=source_message_id,
            metadata=metadata or {},
            entity_ids=list(dict.fromkeys(linked_entity_ids)),
        )
        if fact_id:
            created_facts += 1

    if summary and not facts:
        await MemoryFact.create(
            chat_id=chat_id,
            user_id=user_id,
            mode=mode,
            summary=summary,
            fact_text=summary,
            importance=0.35,
            source_message_id=source_message_id,
            metadata=metadata or {},
            entity_ids=list(dict.fromkeys(entity_by_name.values())),
        )

    for relation in relations:
        source_id = entity_by_name.get(normalize_entity_name(relation.get("source") or ""))
        target_id = entity_by_name.get(normalize_entity_name(relation.get("target") or ""))
        if not source_id or not target_id:
            continue
        await MemoryRelation.create(
            chat_id=chat_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type=relation.get("type") or "related_to",
            description=relation.get("description") or None,
        )

    if (
        MEMORY_CONSOLIDATION_AUTO
        and source_message_id
        and MEMORY_CONSOLIDATION_INTERVAL > 0
        and source_message_id % MEMORY_CONSOLIDATION_INTERVAL == 0
    ):
        consolidation_task = asyncio.create_task(
            maybe_consolidate(
                chat_id=chat_id,
                mode=mode,
                dry_run=not MEMORY_CONSOLIDATION_APPLY,
            )
        )

        def _log_consolidation_error(task):
            try:
                task.result()
            except Exception:
                logger.exception("Ошибка фоновой консолидации памяти")

        consolidation_task.add_done_callback(_log_consolidation_error)

    logger.info(f"Память обновлена: chat={chat_id}, facts={created_facts}, entities={len(entity_by_name)}")
