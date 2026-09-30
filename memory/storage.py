import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, List

from config import (
    MEMORY_CHUNK_MIN_SIMILARITY,
    MEMORY_CONSOLIDATION_APPLY,
    MEMORY_CONSOLIDATION_AUTO,
    MEMORY_CONSOLIDATION_INTERVAL,
    MEMORY_PROFILE_AUTO,
    MEMORY_PROFILE_MIN_INTERVAL_SEC,
    MEMORY_PROFILES_ENABLED,
    MEMORY_TIMELINE_APPLY,
    MEMORY_TIMELINE_CHECK_INTERVAL,
    MEMORY_TIMELINE_ENABLED,
)
from database.models import MemoryChunk, MemoryEntity, MemoryFact, MemoryMessage, MemoryRelation, MemoryWikiPage
from memory.context import memory_payload
from memory.chunking import build_compact_chunks
from memory.consolidator import maybe_consolidate
from memory.embeddings import EMBEDDING_MODEL, embed_document, embed_query
from memory.extractor import extract_memory
from memory.normalizer import compact_text, keyword_query, normalize_entity_name, text_contains_entity
from memory.profiles import get_profile_context, maybe_refresh_user_profile
from memory.timeline import build_timeline_events, get_timeline_context

logger = logging.getLogger(__name__)

# Удерживаем ссылки на fire-and-forget задачи, иначе их может собрать GC
# до завершения (см. docs asyncio.create_task). Снимаем в done-callback.
_BACKGROUND_TASKS: set = set()

# MEM-04: счётчик новых фактов ПО ЧАТУ (chat_id, mode) -> int с момента последней
# консолидации. Заменяет старый триггер по глобальному id % N, при котором частота
# обслуживания чата зависела от трафика других чатов. Память процесса; сброс при
# рестарте лишь отложит ближайшую консолидацию (безопасно).
_consolidation_counters: dict = {}

# Счётчик экзченджей ПО ЧАТУ (chat_id, mode) -> int с момента последней ПОПЫТКИ
# построить timeline. Раз в MEMORY_TIMELINE_CHECK_INTERVAL экзченджей дёргаем
# build_timeline_events; сама функция пропускает работу, пока новых сообщений
# меньше MEMORY_TIMELINE_MIN_MESSAGES, поэтому LLM-вызов происходит редко.
_timeline_counters: dict = {}


def _track_background_task(task) -> None:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def build_memory_context(
    chat_id: int,
    user_id: int,
    user_message: str,
    mode: str = "default",
    fact_limit: int = 5,
    log_limit: int = 2,
) -> str:
    from cognition.scope import CURRENT_SCOPE
    scope=CURRENT_SCOPE.get()
    if scope and scope.group and scope.chat_id==chat_id:
        # Public group transcript has a separate provenance-preserving reader.
        # Legacy RAG has no audience/topic metadata and cannot be broadcast.
        return ''
    query = keyword_query(user_message) or compact_text(user_message, 160)
    if not query:
        return ""

    try:
        # 1. Загрузка Wiki Lore
        wiki_personality = None
        relevant_wiki = None
        try:
            wiki_personality = await MemoryWikiPage.get_by_key(chat_id=None, mode=mode, page_key="personality", verified_only=True)
            wiki_pages = await MemoryWikiPage.search(chat_id=chat_id, mode=mode, query=user_message, limit=1)
            if wiki_pages:
                candidate = wiki_pages[0]
                if candidate.get("page_key") != "personality":
                    relevant_wiki = candidate
        except Exception as e:
            logger.warning(f"Wiki retrieval недоступен: {e}")

        profile_context = ""
        if MEMORY_PROFILES_ENABLED:
            try:
                profile_context = await get_profile_context(chat_id=chat_id, user_id=user_id, mode=mode)
            except Exception as e:
                logger.warning(f"Profile retrieval недоступен: {e}")

        timeline_context = ""
        if MEMORY_TIMELINE_ENABLED:
            try:
                timeline_context = await get_timeline_context(chat_id=chat_id, mode=mode, query=user_message, limit=3)
            except Exception as e:
                logger.warning(f"Timeline retrieval недоступен: {e}")

        # The legacy entity graph lacks mode/owner scope. Until B02/B17 migration
        # it must not inject RP relations or another participant's associations.

        facts = []
        try:
            facts = await MemoryFact.search(chat_id=chat_id, query=query, mode=mode, limit=fact_limit, user_id=user_id)
        except Exception as e:
            logger.warning(f"Fact retrieval недоступен: {e}")

        chunks = []
        try:
            query_vector = await embed_query(user_message)
        except Exception as e:
            logger.warning(f"Embedding query недоступен: {e}")
            query_vector = []
        if query_vector:
            try:
                chunks = await MemoryChunk.search_vector(
                    chat_id=chat_id,
                    mode=mode,
                    query_vector=query_vector,
                    limit=log_limit,
                    min_similarity=MEMORY_CHUNK_MIN_SIMILARITY,
                    embedding_model=EMBEDDING_MODEL,
                    user_id=user_id,
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
            try:
                messages = await MemoryMessage.search(chat_id=chat_id, query=query, mode=mode, limit=log_limit)
            except Exception as e:
                logger.warning(f"Message retrieval недоступен: {e}")

        # ЗАГРУЗКА ЭМОЦИОНАЛЬНОГО СОСТОЯНИЯ И АФФЕКТИВНОГО ПРОФИЛЯ.
        # Изолируем: сбой этого блока НЕ должен обнулять уже собранные wiki/факты/чанки.
        from database.models import ChatEmotionalState, MemoryUserProfile
        import json

        charge = 0.0
        dominant_mood = "thinking"
        max_val = 0.0
        closeness = 0.1
        sticker_receptivity = 0.5
        last_sent_mood = "нет"
        try:
            emo_state = await ChatEmotionalState.get_or_create(chat_id)
            charge = emo_state.get("charge", 0.0)
            mood_dict = json.loads(emo_state["mood_state"]) if isinstance(emo_state["mood_state"], str) else emo_state["mood_state"]

            # Поиск доминирующей эмоции (max_val<=0 трактуем как отсутствие выраженной эмоции)
            for emotion, val in (mood_dict or {}).items():
                if val > max_val:
                    max_val = val
                    dominant_mood = emotion

            # Получение аффективного профиля пользователя
            user_profile = await MemoryUserProfile.get(chat_id, user_id, mode)
            if user_profile and user_profile.get("profile_json"):
                prof_json = json.loads(user_profile["profile_json"]) if isinstance(user_profile["profile_json"], str) else user_profile["profile_json"]
                aff = prof_json.get("affective", {})
                closeness = aff.get("closeness", 0.1)
                sticker_receptivity = aff.get("sticker_receptivity", 0.5)

            last_sent_mood = emo_state.get("last_sent_sticker_mood") or "нет"
        except Exception as e:
            logger.warning(f"Эмоциональное состояние недоступно, используются дефолты: {e}")

        emo_line = (
            f"[ЭМОЦИОНАЛЬНЫЙ СТАТУС ДИАЛОГА]\n"
            f"- Твоя текущая близость с собеседником: {closeness:.2f} (0.0 — холодный незнакомец, 1.0 — твой близкий друг Александр).\n"
            f"- Восприимчивость пользователя к стикерам: {sticker_receptivity:.2f} (0.0 — не любит стикеры, 1.0 — обожает эмоции).\n"
            f"- Твой текущий эмоциональный заряд: {charge:.2f}/1.0 (стикеры отправляются только при высоком заряде).\n"
            f"- Твоя доминирующая эмоция сессии: {dominant_mood} (сила: {max_val:.2f}).\n"
            f"- Твой последний отправленный стикер: настроение {last_sent_mood}."
        )

        if not wiki_personality and not relevant_wiki and not profile_context and not timeline_context and not facts and not chunks and not messages:
            return emo_line

        # Each source receives its own quota; profiles cannot crowd out facts.
        # Retrieval does not mean expression, so it never calls mark_used.
        sections = [
            ('Relevant facts', '\n'.join('- ' + compact_text(f.get('fact_text') or f.get('summary') or '', 360) for f in facts), 2100),
            ('Historical excerpts', '\n'.join('- ' + compact_text(c.get('chunk_text') or '', 500) for c in chunks), 1100),
            ('User profile', profile_context, 850),
            ('Timeline', timeline_context, 600),
            ('Past messages', '\n'.join(compact_text(m.get('user_name') or 'Participant', 60) + ': ' + compact_text(m.get('message_text') or '', 250) for m in messages), 600),
        ]
        if wiki_personality:
            sections.append(('Verified persona', wiki_personality.get('content') or '', 1200))
        if relevant_wiki:
            sections.append(('Verified lore', relevant_wiki.get('content') or '', 700))
        payload = memory_payload(sections, limit=6000)
        return emo_line + '\n' + payload

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
        linked_entity_ids = [entity_id for normalized, entity_id in entity_by_name.items() if text_contains_entity(text, normalized)]
        result = await MemoryFact.create_with_status(
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
        if result.created:
            created_facts += 1

    # An exchange summary is episodic context, not a new personal fact.

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

    # MEM-04: триггерим консолидацию по числу НОВЫХ фактов именно этого чата.
    should_consolidate = False
    if MEMORY_CONSOLIDATION_AUTO and MEMORY_CONSOLIDATION_INTERVAL > 0 and created_facts > 0:
        counter_key = (chat_id, mode)
        _consolidation_counters[counter_key] = _consolidation_counters.get(counter_key, 0) + created_facts
        if _consolidation_counters[counter_key] >= MEMORY_CONSOLIDATION_INTERVAL:
            _consolidation_counters[counter_key] = 0
            should_consolidate = True

    if should_consolidate:
        consolidation_task = asyncio.create_task(
            maybe_consolidate(
                chat_id=chat_id,
                mode=mode,
                dry_run=not MEMORY_CONSOLIDATION_APPLY,
            )
        )
        _track_background_task(consolidation_task)

        def _log_consolidation_error(task):
            try:
                task.result()
            except Exception:
                logger.exception("Ошибка фоновой консолидации памяти")

        consolidation_task.add_done_callback(_log_consolidation_error)

    # Авто-построение сжатой хронологии (memory_timeline). Раньше таблица заполнялась
    # ТОЛЬКО вручную (`maintain_memory.py --timeline --apply`) и потому всегда была
    # пустой. Триггерим раз в MEMORY_TIMELINE_CHECK_INTERVAL экзченджей: попытка дёшева,
    # т.к. build_timeline_events сам пропускает работу, пока новых сообщений < порога.
    if MEMORY_TIMELINE_ENABLED and MEMORY_TIMELINE_CHECK_INTERVAL > 0:
        timeline_key = (chat_id, mode)
        _timeline_counters[timeline_key] = _timeline_counters.get(timeline_key, 0) + 1
        if _timeline_counters[timeline_key] >= MEMORY_TIMELINE_CHECK_INTERVAL:
            _timeline_counters[timeline_key] = 0
            timeline_task = asyncio.create_task(
                build_timeline_events(
                    chat_id=chat_id,
                    mode=mode,
                    dry_run=not MEMORY_TIMELINE_APPLY,
                )
            )
            _track_background_task(timeline_task)

            def _log_timeline_error(task):
                try:
                    task.result()
                except Exception:
                    logger.exception("Ошибка фонового построения timeline")

            timeline_task.add_done_callback(_log_timeline_error)

    # Авто-перестроение смыслового профиля пользователя из накопленных фактов.
    # Без этого profile_json содержит только аффективный блок, а /my_profile показывает
    # плейсхолдер «Новый субъект общения.». Троттлинг — внутри maybe_refresh_user_profile.
    if (
        MEMORY_PROFILES_ENABLED
        and MEMORY_PROFILE_AUTO
        and user_id
        and (created_facts > 0 or summary or entity_by_name)
    ):
        profile_task = asyncio.create_task(
            maybe_refresh_user_profile(
                chat_id=chat_id,
                user_id=user_id,
                mode=mode,
                min_interval_sec=MEMORY_PROFILE_MIN_INTERVAL_SEC,
            )
        )
        _track_background_task(profile_task)

        def _log_profile_error(task):
            try:
                task.result()
            except Exception:
                logger.exception("Ошибка фонового перестроения профиля пользователя")

        profile_task.add_done_callback(_log_profile_error)

    logger.info(f"Память обновлена: chat={chat_id}, facts={created_facts}, entities={len(entity_by_name)}")
