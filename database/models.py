"""
Модели данных для работы с PostgreSQL
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict, Any
import asyncpg

from .connection import get_db

logger = logging.getLogger(__name__)

# Выделенный логгер для логов заряда в logs/emotional.log
emotional_logger = logging.getLogger("emotional.state")
emotional_logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.FileHandler) for h in emotional_logger.handlers):
    import os
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/emotional.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    emotional_logger.addHandler(fh)


class ChatHistory:
    """Работа с историей чатов"""
    
    @staticmethod
    async def save(chat_id: int, user_name: str, message_text: str, timestamp: Optional[datetime] = None):
        """Сохранить сообщение в историю чата"""
        if timestamp is None:
            timestamp = datetime.now()
        
        # L-18: вставку и чистку «хвоста» делаем атомарно в одной транзакции.
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO chat_history (chat_id, timestamp, user_name, message_text, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                """, chat_id, timestamp, user_name, message_text)

                # Очищаем старые записи (оставляем только последние 30)
                await conn.execute("""
                    DELETE FROM chat_history
                    WHERE chat_id = $1
                    AND id NOT IN (
                        SELECT id FROM chat_history
                        WHERE chat_id = $1
                        ORDER BY timestamp DESC
                        LIMIT 30
                    )
                """, chat_id)
    
    @staticmethod
    async def get_recent(chat_id: int, limit: int = 30) -> List[Tuple[datetime, str]]:
        """Получить последние сообщения чата"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT timestamp, user_name || ': ' || message_text as message
                FROM chat_history
                WHERE chat_id = $1
                ORDER BY timestamp DESC
                LIMIT $2
            """, chat_id, limit)
            
            return [(row['timestamp'], row['message']) for row in reversed(rows)]
    
    @staticmethod
    async def clear(chat_id: int):
        """Очистить историю чата"""
        async with get_db() as conn:
            await conn.execute("DELETE FROM chat_history WHERE chat_id = $1", chat_id)


class ChatHistoryRP:
    """Работа с историей RP-чатов"""

    @staticmethod
    async def save(chat_id: int, user_name: str, message_text: str, timestamp: Optional[datetime] = None):
        """Сохранить сообщение в RP-историю чата"""
        if timestamp is None:
            timestamp = datetime.now()

        # L-18: вставку и чистку «хвоста» делаем атомарно в одной транзакции.
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO chat_history_rp (chat_id, timestamp, user_name, message_text, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                """, chat_id, timestamp, user_name, message_text)

                await conn.execute("""
                    DELETE FROM chat_history_rp
                    WHERE chat_id = $1
                    AND id NOT IN (
                        SELECT id FROM chat_history_rp
                        WHERE chat_id = $1
                        ORDER BY timestamp DESC
                        LIMIT 30
                    )
                """, chat_id)

    @staticmethod
    async def get_recent(chat_id: int, limit: int = 30) -> List[Tuple[datetime, str]]:
        """Получить последние сообщения RP-чата"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT timestamp, user_name || ': ' || message_text as message
                FROM chat_history_rp
                WHERE chat_id = $1
                ORDER BY timestamp DESC
                LIMIT $2
            """, chat_id, limit)

            return [(row['timestamp'], row['message']) for row in reversed(rows)]

    @staticmethod
    async def clear(chat_id: int):
        """Очистить RP-историю чата"""
        async with get_db() as conn:
            await conn.execute("DELETE FROM chat_history_rp WHERE chat_id = $1", chat_id)



class SpamProtection:
    """Работа со спам-защитой"""
    
    @staticmethod
    async def get_or_create(chat_id: int, user_id: int) -> dict:
        """Получить или создать запись спам-защиты"""
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT blocked_until, warnings_sent, last_command_time, command_count, command_timestamps
                FROM spam_protection
                WHERE chat_id = $1 AND user_id = $2
            """, chat_id, user_id)
            
            if row is None:
                await conn.execute("""
                    INSERT INTO spam_protection (chat_id, user_id)
                    VALUES ($1, $2)
                """, chat_id, user_id)
                return {
                    'blocked_until': None,
                    'warnings_sent': False,
                    'last_command_time': None,
                    'command_count': 0,
                    'command_timestamps': []
                }
            
            # Парсим JSONB массив времен в список datetime
            import json
            timestamps_json = row['command_timestamps'] or []
            if isinstance(timestamps_json, str):
                try:
                    timestamps_json = json.loads(timestamps_json)
                except (ValueError, TypeError):
                    timestamps_json = []
            timestamps = []
            for ts in timestamps_json:
                if isinstance(ts, str):
                    try:
                        timestamps.append(datetime.fromisoformat(ts))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(ts, datetime):
                    timestamps.append(ts)
            
            return {
                'blocked_until': row['blocked_until'],
                'warnings_sent': row['warnings_sent'],
                'last_command_time': row['last_command_time'],
                'command_count': row['command_count'],
                'command_timestamps': timestamps
            }
    
    @staticmethod
    async def update(chat_id: int, user_id: int, **kwargs):
        """Обновить данные спам-защиты"""
        updates = []
        values = []
        param_idx = 1
        
        for key, value in kwargs.items():
            if key in ['blocked_until', 'warnings_sent', 'last_command_time', 'command_count']:
                updates.append(f"{key} = ${param_idx}")
                values.append(value)
                param_idx += 1
            elif key == 'command_timestamps':
                # Конвертируем список datetime в JSON
                import json
                timestamps_str = [ts.isoformat() if isinstance(ts, datetime) else str(ts) for ts in value]
                updates.append(f"{key} = ${param_idx}::jsonb")
                values.append(json.dumps(timestamps_str))
                param_idx += 1
        
        if not updates:
            return
        
        values.extend([chat_id, user_id])
        
        async with get_db() as conn:
            await conn.execute(f"""
                UPDATE spam_protection
                SET {', '.join(updates)}
                WHERE chat_id = ${param_idx} AND user_id = ${param_idx + 1}
            """, *values)
    
    @staticmethod
    async def clear(chat_id: int, user_id: int):
        """Очистить данные спам-защиты"""
        async with get_db() as conn:
            await conn.execute("""
                UPDATE spam_protection
                SET blocked_until = NULL,
                    warnings_sent = FALSE,
                    last_command_time = NULL,
                    command_count = 0,
                    command_timestamps = '[]'::jsonb
                WHERE chat_id = $1 AND user_id = $2
            """, chat_id, user_id)


class ResponseStatus:
    """Статус ответов бота в чате"""
    
    @staticmethod
    async def get(chat_id: int) -> bool:
        """Получить статус ответов"""
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT enabled FROM response_status WHERE chat_id = $1
            """, chat_id)
            
            return row['enabled'] if row else False
    
    @staticmethod
    async def set(chat_id: int, enabled: bool):
        """Установить статус ответов"""
        async with get_db() as conn:
            await conn.execute("""
                INSERT INTO response_status (chat_id, enabled, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id)
                DO UPDATE SET enabled = $2, updated_at = NOW()
            """, chat_id, enabled)



class ChatModel:
    """Выбор модели ИИ для чата"""

    @staticmethod
    async def get(chat_id: int, default_model: str) -> str:
        """Получить выбранную модель"""
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT model_id FROM chat_models WHERE chat_id = $1
            """, chat_id)

            return row['model_id'] if row else default_model

    @staticmethod
    async def set(chat_id: int, model_id: str):
        """Установить модель"""
        async with get_db() as conn:
            await conn.execute("""
                INSERT INTO chat_models (chat_id, model_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id)
                DO UPDATE SET model_id = $2, updated_at = NOW()
            """, chat_id, model_id)


class UserLocation:
    """Работа с геолокацией пользователей"""

    @staticmethod
    async def save(user_id: int, lat: float, lng: float, address: str = None, city: str = None):
        """Сохранить или обновить геопозицию пользователя"""
        async with get_db() as conn:
            await conn.execute("""
                INSERT INTO user_locations (user_id, lat, lng, address, city, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET lat = $2, lng = $3, address = $4, city = $5, updated_at = NOW()
            """, user_id, lat, lng, address, city)

    @staticmethod
    async def get(user_id: int, conn=None) -> Optional[dict]:
        """Получить геопозицию пользователя из БД.

        conn: если передано существующее соединение — переиспользуем его и НЕ
        захватываем новое из пула. Это важно при вызове изнутри уже открытой
        транзакции (см. ChatEmotionalState.update_state): иначе вложенный
        get_db() забирает второе соединение пула и под нагрузкой пул может
        самозаблокироваться (RACE-01).
        """
        async def _fetch(c):
            return await c.fetchrow("""
                SELECT lat, lng, address, city, updated_at
                FROM user_locations WHERE user_id = $1
            """, user_id)

        if conn is not None:
            row = await _fetch(conn)
        else:
            async with get_db() as db_conn:
                row = await _fetch(db_conn)

        if row:
            return {
                "lat": row["lat"],
                "lng": row["lng"],
                "address": row["address"],
                "city": row["city"],
                "updated_at": row["updated_at"]
            }
        return None

    @staticmethod
    async def get_with_ttl(user_id: int, ttl_seconds: int = 14400) -> Optional[dict]:
        """Получить геопозицию, если она не протухла (по умолчанию 4 часа)"""
        # DB-01: TTL передаём параметром через make_interval, а не интерполяцией
        # строки в SQL (единственное место в проекте со %-форматированием запроса).
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT lat, lng, address, city, updated_at
                FROM user_locations
                WHERE user_id = $1 AND updated_at > NOW() - make_interval(secs => $2)
            """, user_id, float(ttl_seconds))
            if row:
                return {
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "address": row["address"],
                    "city": row["city"],
                    "updated_at": row["updated_at"]
                }
            return None

    @staticmethod
    async def update_address(user_id: int, address: str, city: str = None):
        """Обновить адрес после геокодирования"""
        async with get_db() as conn:
            await conn.execute("""
                UPDATE user_locations
                SET address = $2, city = $3, updated_at = NOW()
                WHERE user_id = $1
            """, user_id, address, city)


class SavedVoice:
    @staticmethod
    async def save(
        user_id: int,
        chat_id: int,
        name: str,
        catbox_url: str,
        catbox_file_id: str = None,
        source_kind: str = None,
        cleaned: bool = False,
        duration_sec: float = None,
    ) -> dict:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO saved_voices (
                    user_id, chat_id, name, catbox_url, catbox_file_id,
                    source_kind, cleaned, duration_sec, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (user_id, name)
                DO UPDATE SET
                    chat_id = $2,
                    catbox_url = $4,
                    catbox_file_id = $5,
                    source_kind = $6,
                    cleaned = $7,
                    duration_sec = $8,
                    created_at = NOW(),
                    last_used_at = NULL
                RETURNING *
            """, user_id, chat_id, name, catbox_url, catbox_file_id, source_kind, cleaned, duration_sec)
            return dict(row)

    @staticmethod
    async def list_for_user(user_id: int, limit: int = 20) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *
                FROM saved_voices
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_id, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def get(user_id: int, voice_id: int) -> Optional[dict]:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT *
                FROM saved_voices
                WHERE user_id = $1 AND id = $2
            """, user_id, voice_id)
            return dict(row) if row else None

    @staticmethod
    async def get_by_name(user_id: int, name: str) -> Optional[dict]:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT *
                FROM saved_voices
                WHERE user_id = $1 AND name = $2
            """, user_id, name)
            return dict(row) if row else None

    @staticmethod
    async def delete(user_id: int, voice_id: int) -> Optional[dict]:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                DELETE FROM saved_voices
                WHERE user_id = $1 AND id = $2
                RETURNING *
            """, user_id, voice_id)
            return dict(row) if row else None

    @staticmethod
    async def delete_by_name(user_id: int, name: str) -> Optional[dict]:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                DELETE FROM saved_voices
                WHERE user_id = $1 AND name = $2
                RETURNING *
            """, user_id, name)
            return dict(row) if row else None

    @staticmethod
    async def touch(user_id: int, voice_id: int):
        async with get_db() as conn:
            await conn.execute("""
                UPDATE saved_voices
                SET last_used_at = NOW()
                WHERE user_id = $1 AND id = $2
            """, user_id, voice_id)


class MemoryMessage:
    @staticmethod
    async def save(
        chat_id: int,
        user_name: str,
        message_text: str,
        user_id: int = None,
        role: str = "user",
        mode: str = "default",
        source: str = "chat",
        metadata: Dict[str, Any] = None,
    ) -> Optional[int]:
        if not message_text:
            return None

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO memory_messages (
                    chat_id, user_id, user_name, role, mode, source, message_text, metadata, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
                RETURNING id
            """, chat_id, user_id, user_name, role, mode, source, message_text, metadata_json)
            return row["id"] if row else None

    @staticmethod
    async def search(chat_id: int, query: str, mode: str = "default", limit: int = 5) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (
                    SELECT plainto_tsquery('russian', $2) AS query
                )
                SELECT *,
                    ts_rank(to_tsvector('russian', coalesce(message_text, '')), q.query) AS rank
                FROM memory_messages
                CROSS JOIN q
                WHERE chat_id = $1
                AND mode = $3
                AND role IN ('user', 'assistant', 'memory')
                AND (
                    to_tsvector('russian', coalesce(message_text, '')) @@ q.query
                    OR message_text ILIKE '%' || $2 || '%'
                )
                ORDER BY rank DESC, created_at DESC
                LIMIT $4
            """, chat_id, query, mode, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def fetch_for_chunking(limit: int = 5000, after_id: int = 0) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT id, chat_id, user_id, user_name, role, mode, message_text, created_at
                FROM memory_messages
                WHERE id > $1
                AND role IN ('user', 'assistant', 'memory')
                ORDER BY chat_id, mode, created_at, id
                LIMIT $2
            """, after_id, limit)
            return [dict(row) for row in rows]


def _vector_to_pg(value: List[float]) -> str:
    return "[" + ",".join(f"{float(item):.8f}" for item in value) + "]"


class MemoryChunk:
    @staticmethod
    async def create(
        chat_id: int,
        chunk_text: str,
        message_ids: List[int],
        user_id: int = None,
        mode: str = "default",
        token_estimate: int = 0,
        metadata: Dict[str, Any] = None,
    ) -> Optional[int]:
        if not chunk_text or not message_ids:
            return None

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with get_db() as conn:
            exists = await conn.fetchval("""
                SELECT id
                FROM memory_chunks
                WHERE message_ids = $1::bigint[]
                LIMIT 1
            """, message_ids)
            if exists:
                return exists

            # MEM-07: ON CONFLICT по UNIQUE(message_ids) — закрывает гонку двух
            # параллельных вставок одинакового набора message_ids.
            row = await conn.fetchrow("""
                INSERT INTO memory_chunks (
                    chat_id, user_id, mode, chunk_text, message_ids,
                    token_estimate, metadata, created_at
                )
                VALUES ($1, $2, $3, $4, $5::bigint[], $6, $7::jsonb, NOW())
                ON CONFLICT (message_ids) DO NOTHING
                RETURNING id
            """, chat_id, user_id, mode, chunk_text, message_ids, token_estimate, metadata_json)
            if row:
                return row["id"]
            # Проиграли гонку — возвращаем id уже существующего чанка.
            return await conn.fetchval("""
                SELECT id FROM memory_chunks WHERE message_ids = $1::bigint[] LIMIT 1
            """, message_ids)

    @staticmethod
    async def bulk_create(chunks: List[Dict[str, Any]]) -> List[int]:
        chunk_ids = []
        for chunk in chunks:
            chunk_id = await MemoryChunk.create(**chunk)
            if chunk_id:
                chunk_ids.append(chunk_id)
        return chunk_ids

    @staticmethod
    async def set_embedding(chunk_id: int, vector: List[float], model: str):
        if not chunk_id or not vector:
            return

        async with get_db() as conn:
            await conn.execute("""
                UPDATE memory_chunks
                SET embedding = $2::vector,
                    embedding_model = $3,
                    embedded_at = NOW()
                WHERE id = $1
            """, chunk_id, _vector_to_pg(vector), model)

    @staticmethod
    async def search_vector(chat_id: int, mode: str, query_vector: List[float], limit: int = 5) -> List[dict]:
        if not query_vector:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *,
                    1 - (embedding <=> $3::vector) AS similarity
                FROM memory_chunks
                WHERE chat_id = $1
                AND mode = $2
                AND embedding IS NOT NULL
                ORDER BY embedding <=> $3::vector
                LIMIT $4
            """, chat_id, mode, _vector_to_pg(query_vector), limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def search_text(chat_id: int, mode: str, query: str, limit: int = 5) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (
                    SELECT plainto_tsquery('russian', $3) AS query
                )
                SELECT *,
                    ts_rank(to_tsvector('russian', coalesce(chunk_text, '')), q.query) AS rank
                FROM memory_chunks
                CROSS JOIN q
                WHERE chat_id = $1
                AND mode = $2
                AND (
                    to_tsvector('russian', coalesce(chunk_text, '')) @@ q.query
                    OR chunk_text ILIKE '%' || $3 || '%'
                )
                ORDER BY rank DESC, created_at DESC
                LIMIT $4
            """, chat_id, mode, query, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def get_unembedded(limit: int = 100) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *
                FROM memory_chunks
                WHERE embedding IS NULL
                ORDER BY created_at ASC, id ASC
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def latest_message_id() -> int:
        # MEM-05: настоящий максимум по ВСЕМ элементам массива, а не последний
        # элемент (порядок внутри message_ids не гарантирован).
        async with get_db() as conn:
            value = await conn.fetchval("""
                SELECT COALESCE(MAX(m), 0)
                FROM memory_chunks, LATERAL unnest(message_ids) AS m
            """)
            return int(value or 0)


class MemoryEntity:
    @staticmethod
    async def get_or_create(
        chat_id: int,
        canonical_name: str,
        normalized_name: str,
        entity_type: str = "unknown",
        aliases: List[str] = None,
    ) -> Optional[dict]:
        if not canonical_name or not normalized_name:
            return None

        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO memory_entities (
                    chat_id, canonical_name, normalized_name, entity_type, mention_count, created_at, last_seen_at
                )
                VALUES ($1, $2, $3, $4, 1, NOW(), NOW())
                ON CONFLICT (chat_id, normalized_name)
                DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    entity_type = COALESCE(NULLIF(EXCLUDED.entity_type, 'unknown'), memory_entities.entity_type),
                    mention_count = memory_entities.mention_count + 1,
                    last_seen_at = NOW()
                RETURNING *
            """, chat_id, canonical_name, normalized_name, entity_type or "unknown")

            if row and aliases:
                for alias in aliases:
                    alias_value = (alias or "").strip()
                    if not alias_value:
                        continue
                    normalized_alias = alias_value.lower().replace("ё", "е")
                    await conn.execute("""
                        INSERT INTO memory_entity_aliases (
                            chat_id, entity_id, alias, normalized_alias, created_at
                        )
                        VALUES ($1, $2, $3, $4, NOW())
                        ON CONFLICT (chat_id, normalized_alias) DO NOTHING
                    """, chat_id, row["id"], alias_value, normalized_alias)

            return dict(row) if row else None

    @staticmethod
    async def find_related(chat_id: int, query: str, limit: int = 8) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT e.*
                FROM memory_entities e
                LEFT JOIN memory_entity_aliases a ON a.entity_id = e.id
                WHERE e.chat_id = $1
                AND (
                    e.normalized_name ILIKE '%' || $2 || '%'
                    OR $2 ILIKE '%' || e.normalized_name || '%'
                    OR a.normalized_alias ILIKE '%' || $2 || '%'
                    OR $2 ILIKE '%' || a.normalized_alias || '%'
                )
                ORDER BY e.last_seen_at DESC
                LIMIT $3
            """, chat_id, query.lower().replace("ё", "е"), limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def find_mentions(chat_id: int, text: str, limit: int = 5) -> List[dict]:
        normalized_text = (text or "").strip().lower().replace("ё", "е")
        if not normalized_text:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT e.*
                FROM memory_entities e
                LEFT JOIN memory_entity_aliases a ON a.entity_id = e.id
                WHERE e.chat_id = $1
                AND (
                    $2 ILIKE '%' || e.normalized_name || '%'
                    OR e.normalized_name ILIKE '%' || $2 || '%'
                    OR $2 ILIKE '%' || a.normalized_alias || '%'
                    OR a.normalized_alias ILIKE '%' || $2 || '%'
                )
                ORDER BY e.last_seen_at DESC
                LIMIT $3
            """, chat_id, normalized_text, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def find_related_entities(chat_id: int, entity_ids: List[int], limit: int = 15) -> List[dict]:
        """
        Находит 2-hop связанные сущности для заданного списка ID сущностей.
        Сортирует по суммарному весу связей.
        """
        entity_ids = [int(eid) for eid in entity_ids if eid]
        if not entity_ids:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH hop1 AS (
                    SELECT id FROM memory_entities WHERE id = ANY($2::bigint[]) AND chat_id = $1
                ),
                hop1_relations AS (
                    SELECT 
                        CASE 
                            WHEN source_entity_id IN (SELECT id FROM hop1) THEN target_entity_id
                            ELSE source_entity_id
                        END AS entity_id,
                        weight
                    FROM memory_relations 
                    WHERE (source_entity_id IN (SELECT id FROM hop1) OR target_entity_id IN (SELECT id FROM hop1))
                    AND chat_id = $1
                ),
                hop1_neighbors AS (
                    SELECT entity_id AS id, MAX(weight) AS weight
                    FROM hop1_relations
                    WHERE entity_id NOT IN (SELECT id FROM hop1)
                    GROUP BY entity_id
                ),
                hop2_relations AS (
                    SELECT 
                        CASE 
                            WHEN source_entity_id IN (SELECT id FROM hop1_neighbors) THEN target_entity_id
                            ELSE source_entity_id
                        END AS entity_id,
                        r.weight * hn.weight AS weight
                    FROM memory_relations r
                    JOIN hop1_neighbors hn ON (hn.id = r.source_entity_id OR hn.id = r.target_entity_id)
                    WHERE r.chat_id = $1
                ),
                hop2_neighbors AS (
                    SELECT entity_id AS id, MAX(weight) AS weight
                    FROM hop2_relations
                    WHERE entity_id NOT IN (SELECT id FROM hop1)
                    AND entity_id NOT IN (SELECT id FROM hop1_neighbors)
                    GROUP BY entity_id
                ),
                all_connected AS (
                    SELECT id, 10.0 AS score FROM hop1
                    UNION ALL
                    SELECT id, weight AS score FROM hop1_neighbors
                    UNION ALL
                    SELECT id, weight * 0.5 AS score FROM hop2_neighbors
                )
                SELECT e.*, c.score
                FROM memory_entities e
                JOIN all_connected c ON c.id = e.id
                ORDER BY c.score DESC
                LIMIT $3
            """, chat_id, entity_ids, limit)
            return [dict(row) for row in rows]


class MemoryFact:
    @staticmethod
    async def create(
        chat_id: int,
        fact_text: str,
        user_id: int = None,
        mode: str = "default",
        summary: str = None,
        importance: float = 0.5,
        source_message_id: int = None,
        metadata: Dict[str, Any] = None,
        entity_ids: List[int] = None,
        conn=None,
    ) -> Optional[int]:
        """Создаёт факт (с дедупом по lower(fact_text)).

        conn: если передано соединение — работаем в нём (для атомарной консолидации
        в одной транзакции, MEM-01). Внутренний conn.transaction() в этом случае
        становится savepoint'ом существующей транзакции.
        """
        fact_text = (fact_text or "").strip()
        if not fact_text:
            return None

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        importance_value = max(0.0, min(float(importance or 0.5), 1.0))

        async def _do(c):
            async with c.transaction():
                existing_id = await c.fetchval("""
                    SELECT id
                    FROM memory_facts
                    WHERE chat_id = $1
                    AND mode = $2
                    AND lower(fact_text) = lower($3)
                    LIMIT 1
                """, chat_id, mode, fact_text)

                if existing_id:
                    return existing_id

                # MEM-07: ON CONFLICT по partial-unique индексу активных фактов —
                # закрывает гонку двух параллельных вставок одинакового факта.
                row = await c.fetchrow("""
                    INSERT INTO memory_facts (
                        chat_id, user_id, mode, summary, fact_text, importance,
                        source_message_id, metadata, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
                    ON CONFLICT (chat_id, mode, lower(fact_text)) WHERE archived_at IS NULL
                    DO NOTHING
                    RETURNING id
                """, chat_id, user_id, mode, summary, fact_text, importance_value, source_message_id, metadata_json)

                if not row:
                    # Проиграли гонку — возвращаем id уже вставленного активного факта.
                    return await c.fetchval("""
                        SELECT id FROM memory_facts
                        WHERE chat_id = $1 AND mode = $2
                        AND lower(fact_text) = lower($3) AND archived_at IS NULL
                        ORDER BY id LIMIT 1
                    """, chat_id, mode, fact_text)

                fact_id = row["id"]
                for entity_id in entity_ids or []:
                    if not entity_id:
                        continue
                    await c.execute("""
                        INSERT INTO memory_fact_entities (fact_id, entity_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    """, fact_id, entity_id)

                return fact_id

        if conn is not None:
            return await _do(conn)
        async with get_db() as db_conn:
            return await _do(db_conn)

    @staticmethod
    async def search(chat_id: int, query: str, mode: str = "default", limit: int = 5) -> List[dict]:
        query = (query or "").strip()
        if not query:
            async with get_db() as conn:
                rows = await conn.fetch("""
                    SELECT *
                    FROM memory_facts
                    WHERE chat_id = $1
                    AND mode = $2
                    AND archived_at IS NULL
                    AND (cooldown_until IS NULL OR cooldown_until < NOW())
                    ORDER BY importance DESC, created_at DESC
                    LIMIT $3
                """, chat_id, mode, limit)
                return [dict(row) for row in rows]

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (
                    SELECT plainto_tsquery('russian', $2) AS query
                ),
                -- 1. Находим непосредственно упомянутые сущности
                hop1 AS (
                    SELECT DISTINCT e.id, 10.0 AS score
                    FROM memory_entities e
                    LEFT JOIN memory_entity_aliases a ON a.entity_id = e.id
                    WHERE e.chat_id = $1
                    AND (
                        $4 ILIKE '%' || e.normalized_name || '%'
                        OR e.normalized_name ILIKE '%' || $4 || '%'
                        OR $4 ILIKE '%' || a.normalized_alias || '%'
                        OR a.normalized_alias ILIKE '%' || $4 || '%'
                    )
                ),
                -- 2. Находим 1-hop связи
                hop1_relations AS (
                    SELECT 
                        CASE 
                            WHEN source_entity_id IN (SELECT id FROM hop1) THEN target_entity_id
                            ELSE source_entity_id
                        END AS entity_id,
                        weight
                    FROM memory_relations 
                    WHERE (source_entity_id IN (SELECT id FROM hop1) OR target_entity_id IN (SELECT id FROM hop1))
                    AND chat_id = $1
                ),
                hop1_neighbors AS (
                    SELECT entity_id AS id, MAX(weight) AS weight
                    FROM hop1_relations
                    WHERE entity_id NOT IN (SELECT id FROM hop1)
                    GROUP BY entity_id
                ),
                -- 3. Находим 2-hop связи
                hop2_relations AS (
                    SELECT 
                        CASE 
                            WHEN source_entity_id IN (SELECT id FROM hop1_neighbors) THEN target_entity_id
                            ELSE source_entity_id
                        END AS entity_id,
                        r.weight * hn.weight AS weight
                    FROM memory_relations r
                    JOIN hop1_neighbors hn ON (hn.id = r.source_entity_id OR hn.id = r.target_entity_id)
                    WHERE r.chat_id = $1
                ),
                hop2_neighbors AS (
                    SELECT entity_id AS id, MAX(weight) AS weight
                    FROM hop2_relations
                    WHERE entity_id NOT IN (SELECT id FROM hop1)
                    AND entity_id NOT IN (SELECT id FROM hop1_neighbors)
                    GROUP BY entity_id
                ),
                all_entities AS (
                    SELECT id, 10.0 AS score FROM hop1
                    UNION ALL
                    SELECT id, weight AS score FROM hop1_neighbors
                    UNION ALL
                    SELECT id, weight * 0.5 AS score FROM hop2_neighbors
                ),
                ranked_facts AS (
                    SELECT f.*,
                        ts_rank(
                            to_tsvector('russian', coalesce(f.summary, '') || ' ' || coalesce(f.fact_text, '')),
                            q.query
                        ) AS rank,
                        (
                            f.fact_text ILIKE '%' || $2 || '%'
                            OR f.summary ILIKE '%' || $2 || '%'
                        ) AS is_direct_match,
                        COALESCE((
                            SELECT SUM(ae.score)
                            FROM memory_fact_entities fe
                            JOIN all_entities ae ON ae.id = fe.entity_id
                            WHERE fe.fact_id = f.id
                        ), 0.0) AS entity_boost
                    FROM memory_facts f
                    CROSS JOIN q
                    WHERE f.chat_id = $1
                    AND f.mode = $3
                    AND f.archived_at IS NULL
                    AND (
                        to_tsvector('russian', coalesce(f.summary, '') || ' ' || coalesce(f.fact_text, '')) @@ q.query
                        OR f.fact_text ILIKE '%' || $2 || '%'
                        OR f.summary ILIKE '%' || $2 || '%'
                        OR EXISTS (
                            SELECT 1 FROM memory_fact_entities fe
                            WHERE fe.fact_id = f.id
                            AND fe.entity_id IN (SELECT id FROM all_entities)
                        )
                    )
                )
                SELECT *
                FROM ranked_facts
                WHERE (
                    cooldown_until IS NULL
                    OR cooldown_until < NOW()
                    -- L-22: bypass cooldown по rank убран — даже релевантный факт «остывает»
                    -- после использования. Релевантность всё равно учтена в ORDER BY
                    -- (rank*2.0), а recency-штраф (- used_count*0.05) мягко понижает
                    -- надоевшие факты.
                )
                ORDER BY (rank * 2.0 + importance + (entity_boost * 0.15) - (used_count * 0.05)) DESC, created_at DESC
                LIMIT $5
            """, chat_id, query, mode, query.lower().replace("ё", "е"), limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def fetch_for_consolidation(chat_id: int, mode: str = "default", limit: int = 80) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *
                FROM memory_facts
                WHERE chat_id = $1
                AND mode = $2
                AND archived_at IS NULL
                AND importance < 0.8
                ORDER BY created_at ASC, id ASC
                LIMIT $3
            """, chat_id, mode, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def archive_many(fact_ids: List[int], reason: str = "consolidated", superseded_by: int = None, conn=None) -> int:
        fact_ids = [int(fact_id) for fact_id in fact_ids if fact_id]
        if not fact_ids:
            return 0

        async def _do(c):
            result = await c.execute("""
                UPDATE memory_facts
                SET archived_at = NOW(),
                    archive_reason = $2,
                    superseded_by = $3
                WHERE id = ANY($1::bigint[])
                AND archived_at IS NULL
            """, fact_ids, reason, superseded_by)
            return int(result.split()[-1])

        if conn is not None:
            return await _do(conn)
        async with get_db() as db_conn:
            return await _do(db_conn)

    @staticmethod
    async def archive_for_user(fact_id: int, chat_id: int, user_id: int, reason: str = "user_request") -> bool:
        """Архивирует факт ТОЛЬКО если он принадлежит этому чату и пользователю
        (или это общий факт чата с user_id IS NULL). Защита от IDOR в /forget:
        нельзя стереть личный факт другого участника группы по чужому fact_id."""
        try:
            fact_id = int(fact_id)
            chat_id = int(chat_id)
            user_id = int(user_id)
        except (TypeError, ValueError):
            return False

        async with get_db() as conn:
            result = await conn.execute("""
                UPDATE memory_facts
                SET archived_at = NOW(),
                    archive_reason = $4
                WHERE id = $1
                AND chat_id = $2
                AND (user_id = $3 OR user_id IS NULL)
                AND archived_at IS NULL
            """, fact_id, chat_id, user_id, reason)
            return result.split()[-1] != "0"

    @staticmethod
    async def mark_used(fact_ids: List[int], cooldown_seconds: int = 3600):
        fact_ids = [int(fact_id) for fact_id in fact_ids if fact_id]
        if not fact_ids:
            return

        # cooldown_until считаем на стороне БД (NOW() + interval), чтобы не смешивать
        # наивное локальное время приложения с временем сервера БД (M-07).
        async with get_db() as conn:
            await conn.execute("""
                UPDATE memory_facts
                SET used_count = used_count + 1,
                    last_used_at = NOW(),
                    cooldown_until = NOW() + make_interval(secs => $2)
                WHERE id = ANY($1::bigint[])
            """, fact_ids, float(cooldown_seconds))


class MemoryRelation:
    @staticmethod
    async def create(
        chat_id: int,
        source_entity_id: int,
        target_entity_id: int,
        relation_type: str,
        description: str = None,
        weight: float = 1.0,
    ) -> Optional[int]:
        if not source_entity_id or not target_entity_id or not relation_type:
            return None

        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO memory_relations (
                    chat_id, source_entity_id, target_entity_id, relation_type, description, weight, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                RETURNING id
            """, chat_id, source_entity_id, target_entity_id, relation_type, description, float(weight or 1.0))
            return row["id"] if row else None

    @staticmethod
    async def find_for_entities(chat_id: int, entity_ids: List[int], limit: int = 8) -> List[dict]:
        entity_ids = [int(entity_id) for entity_id in entity_ids if entity_id]
        if not entity_ids:
            return []

        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT
                    r.*,
                    s.canonical_name AS source_name,
                    t.canonical_name AS target_name
                FROM memory_relations r
                JOIN memory_entities s ON s.id = r.source_entity_id
                JOIN memory_entities t ON t.id = r.target_entity_id
                WHERE r.chat_id = $1
                AND (
                    r.source_entity_id = ANY($2::bigint[])
                    OR r.target_entity_id = ANY($2::bigint[])
                )
                ORDER BY r.weight DESC, r.created_at DESC
                LIMIT $3
            """, chat_id, entity_ids, limit)
            return [dict(row) for row in rows]


class MemoryUserProfile:
    @staticmethod
    async def get(chat_id: int, user_id: int, mode: str = "default") -> Optional[dict]:
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT *
                FROM memory_user_profiles
                WHERE chat_id = $1
                AND user_id = $2
                AND mode = $3
            """, chat_id, user_id, mode)
            return dict(row) if row else None

    @staticmethod
    async def upsert(
        chat_id: int,
        user_id: int,
        mode: str,
        profile_json: Dict[str, Any],
        profile_text: str,
        source_fact_ids: List[int] = None,
        source_entity_ids: List[int] = None,
    ) -> Optional[int]:
        if not chat_id or not user_id or not profile_text:
            return None

        source_fact_ids = [int(item) for item in source_fact_ids or [] if item]
        source_entity_ids = [int(item) for item in source_entity_ids or [] if item]
        incoming = dict(profile_json or {})

        async with get_db() as conn:
            async with conn.transaction():
                # MEM-02: аффективный блок (closeness/receptivity) ведут grow_closeness /
                # apply_reinforcement под FOR UPDATE. Этот upsert (смысловой профиль из LLM)
                # перезаписывает profile_json целиком, и без блокировки затёр бы параллельное
                # обновление близости устаревшим значением (lost update). Берём СВЕЖИЙ
                # affective из БД под тем же FOR UPDATE-локом и сохраняем его.
                existing = await conn.fetchrow("""
                    SELECT profile_json
                    FROM memory_user_profiles
                    WHERE chat_id = $1 AND user_id = $2 AND mode = $3
                    FOR UPDATE
                """, chat_id, user_id, mode)
                if existing:
                    ex_json = existing["profile_json"]
                    if isinstance(ex_json, str):
                        try:
                            ex_json = json.loads(ex_json)
                        except Exception:
                            ex_json = {}
                    if isinstance(ex_json, dict) and isinstance(ex_json.get("affective"), dict):
                        incoming["affective"] = ex_json["affective"]

                profile_json_text = json.dumps(incoming, ensure_ascii=False)
                row = await conn.fetchrow("""
                    INSERT INTO memory_user_profiles (
                        chat_id, user_id, mode, profile_json, profile_text,
                        source_fact_ids, source_entity_ids, facts_version, updated_at
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6::bigint[], $7::bigint[], 1, NOW())
                    ON CONFLICT (chat_id, user_id, mode)
                    DO UPDATE SET
                        profile_json = EXCLUDED.profile_json,
                        profile_text = EXCLUDED.profile_text,
                        source_fact_ids = EXCLUDED.source_fact_ids,
                        source_entity_ids = EXCLUDED.source_entity_ids,
                        facts_version = memory_user_profiles.facts_version + 1,
                        updated_at = NOW()
                    RETURNING id
                """, chat_id, user_id, mode, profile_json_text, profile_text, source_fact_ids, source_entity_ids)
                return row["id"] if row else None

    @staticmethod
    async def fetch_source_material(
        chat_id: int,
        user_id: int,
        mode: str = "default",
        fact_limit: int = 40,
        entity_limit: int = 20,
    ) -> Dict[str, List[dict]]:
        async with get_db() as conn:
            facts = await conn.fetch("""
                SELECT *
                FROM memory_facts
                WHERE chat_id = $1
                AND mode = $2
                AND archived_at IS NULL
                AND (user_id = $3 OR user_id IS NULL)
                ORDER BY importance DESC, created_at DESC
                LIMIT $4
            """, chat_id, mode, user_id, fact_limit)

            # MEM-03: берём не ВСЕ сущности чата, а только связанные с фактами ИМЕННО
            # этого пользователя (или общими фактами чата user_id IS NULL). Иначе в
            # групповом чате в «досье» пользователя A попадали бы сущности участников
            # B и C — и логическая ошибка, и утечка приватных данных.
            entities = await conn.fetch("""
                SELECT DISTINCT e.*
                FROM memory_entities e
                JOIN memory_fact_entities fe ON fe.entity_id = e.id
                JOIN memory_facts f ON f.id = fe.fact_id
                WHERE e.chat_id = $1
                AND f.chat_id = $1
                AND f.mode = $2
                AND f.archived_at IS NULL
                AND (f.user_id = $3 OR f.user_id IS NULL)
                ORDER BY e.mention_count DESC, e.last_seen_at DESC
                LIMIT $4
            """, chat_id, mode, user_id, entity_limit)

            return {
                "facts": [dict(row) for row in facts],
                "entities": [dict(row) for row in entities],
            }

    @staticmethod
    async def apply_reinforcement(chat_id: int, user_id: int, mode: str, feedback_type: str):
        """
        Применяет положительное или отрицательное подкрепление к аффективному профилю пользователя.
        Увеличивает или уменьшает близость (closeness) и восприимчивость к стикерам (sticker_receptivity).
        """
        async with get_db() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT * FROM memory_user_profiles
                    WHERE chat_id = $1 AND user_id = $2 AND mode = $3
                    FOR UPDATE
                """, chat_id, user_id, mode)
                
                profile_json = {}
                profile_text = ""
                if row:
                    profile_json = json.loads(row["profile_json"]) if isinstance(row["profile_json"], str) else row["profile_json"]
                    profile_text = row["profile_text"]
                else:
                    profile_text = "Новый субъект общения."
                
                if "affective" not in profile_json:
                    profile_json["affective"] = {
                        "closeness": 0.1,
                        "sticker_receptivity": 0.5,
                        "dominant_sentiment": "neutral",
                        "emotional_triggers": {},
                        "last_reinforcement_time": None
                    }
                
                aff = profile_json["affective"]
                
                # Проверяем кулдаун (5 минут = 300 секунд)
                last_time_str = aff.get("last_reinforcement_time")
                if last_time_str:
                    try:
                        from datetime import datetime as _dt
                        last_time = _dt.fromisoformat(last_time_str)
                        elapsed = (_dt.now() - last_time).total_seconds()
                        if elapsed < 300:
                            log_entry = (
                                f"[REINFORCEMENT_IGNORED] chat_id={chat_id} user_id={user_id} | "
                                f"Feedback={feedback_type} | CooldownActive={300 - elapsed:.1f}s remaining"
                            )
                            logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] {log_entry}")
                            emotional_logger.info(log_entry)
                            return
                    except Exception as e:
                        logger.warning(f"Ошибка при парсинге времени последнего подкрепления: {e}")

                # Начисляем сбалансированные шаги
                if feedback_type == "positive":
                    aff["closeness"] = min(aff.get("closeness", 0.1) + 0.01, 1.0)
                    aff["sticker_receptivity"] = min(aff.get("sticker_receptivity", 0.5) + 0.02, 1.0)
                else:
                    aff["closeness"] = max(aff.get("closeness", 0.1) - 0.01, 0.0)
                    aff["sticker_receptivity"] = max(aff.get("sticker_receptivity", 0.5) - 0.03, 0.0)
                
                from datetime import datetime as _dt
                aff["last_reinforcement_time"] = _dt.now().isoformat()
                profile_json["affective"] = aff
                
                # Логируем положительное/отрицательное подкрепление
                log_entry = (
                    f"[REINFORCEMENT] chat_id={chat_id} user_id={user_id} | "
                    f"Feedback={feedback_type} | "
                    f"New Closeness={aff['closeness']:.3f} | "
                    f"New Receptivity={aff['sticker_receptivity']:.3f}"
                )
                logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] {log_entry}")
                emotional_logger.info(log_entry)
                
                await conn.execute("""
                    INSERT INTO memory_user_profiles (
                        chat_id, user_id, mode, profile_json, profile_text, updated_at
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, NOW())
                    ON CONFLICT (chat_id, user_id, mode)
                    DO UPDATE SET
                        profile_json = EXCLUDED.profile_json,
                        updated_at = NOW()
                """, chat_id, user_id, mode, json.dumps(profile_json, ensure_ascii=False), profile_text)

    @staticmethod
    async def grow_closeness(chat_id: int, user_id: int, mode: str, proactive_reply: bool = False):
        """
        Растит близость (closeness) и восприимчивость к стикерам от ОБЫЧНОГО общения,
        а не только от эмодзи-реакций. Пассивный рост троттлится (не чаще 1 раза в 90 секунд),
        чтобы серия быстрых сообщений не накручивала близость. Бонус за ответ на проактивный
        пуш начисляется без троттлинга как сильный позитивный сигнал.
        """
        if not chat_id or not user_id:
            return
        async with get_db() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT * FROM memory_user_profiles
                    WHERE chat_id = $1 AND user_id = $2 AND mode = $3
                    FOR UPDATE
                """, chat_id, user_id, mode)

                profile_json = {}
                profile_text = "Новый субъект общения."
                if row:
                    profile_json = json.loads(row["profile_json"]) if isinstance(row["profile_json"], str) else (row["profile_json"] or {})
                    profile_text = row["profile_text"] or profile_text

                if "affective" not in profile_json:
                    profile_json["affective"] = {
                        "closeness": 0.1,
                        "sticker_receptivity": 0.5,
                        "dominant_sentiment": "neutral",
                        "emotional_triggers": {},
                        "last_reinforcement_time": None,
                    }
                aff = profile_json["affective"]

                from datetime import datetime as _dt
                now = _dt.now()
                changed = False

                # Пассивный рост от обычного общения (троттлинг 90 сек)
                throttle_ok = True
                last_passive_str = aff.get("last_passive_growth_time")
                if last_passive_str:
                    try:
                        throttle_ok = (now - _dt.fromisoformat(last_passive_str)).total_seconds() >= 90
                    except Exception:
                        throttle_ok = True
                if throttle_ok:
                    aff["closeness"] = min(aff.get("closeness", 0.1) + 0.01, 1.0)
                    aff["sticker_receptivity"] = min(aff.get("sticker_receptivity", 0.5) + 0.01, 1.0)
                    aff["last_passive_growth_time"] = now.isoformat()
                    changed = True

                # Бонус за ответ на проактивный пуш (без троттлинга)
                if proactive_reply:
                    aff["closeness"] = min(aff.get("closeness", 0.1) + 0.05, 1.0)
                    aff["sticker_receptivity"] = min(aff.get("sticker_receptivity", 0.5) + 0.03, 1.0)
                    changed = True

                if not changed:
                    return

                profile_json["affective"] = aff
                log_entry = (
                    f"[CLOSENESS_GROWTH] chat_id={chat_id} user_id={user_id} | "
                    f"ProactiveReply={proactive_reply} | "
                    f"Closeness={aff['closeness']:.3f} | Receptivity={aff['sticker_receptivity']:.3f}"
                )
                logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] {log_entry}")
                emotional_logger.info(log_entry)

                await conn.execute("""
                    INSERT INTO memory_user_profiles (
                        chat_id, user_id, mode, profile_json, profile_text, updated_at
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, NOW())
                    ON CONFLICT (chat_id, user_id, mode)
                    DO UPDATE SET
                        profile_json = EXCLUDED.profile_json,
                        updated_at = NOW()
                """, chat_id, user_id, mode, json.dumps(profile_json, ensure_ascii=False), profile_text)


class MemoryTimeline:
    @staticmethod
    async def create(
        chat_id: int,
        summary: str,
        user_id: int = None,
        mode: str = "default",
        period_start=None,
        period_end=None,
        title: str = "",
        topics: List[str] = None,
        source_message_ids: List[int] = None,
        metadata: Dict[str, Any] = None,
    ) -> Optional[int]:
        summary = (summary or "").strip()
        if not chat_id or not summary:
            return None

        topics = [str(item).strip() for item in topics or [] if str(item).strip()]
        source_message_ids = [int(item) for item in source_message_ids or [] if item]
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        async with get_db() as conn:
            row = await conn.fetchrow("""
                INSERT INTO memory_timelines (
                    chat_id, user_id, mode, period_start, period_end, title,
                    summary, topics, source_message_ids, metadata, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9::bigint[], $10::jsonb, NOW(), NOW())
                RETURNING id
            """, chat_id, user_id, mode, period_start, period_end, title or "", summary, topics, source_message_ids, metadata_json)
            return row["id"] if row else None

    @staticmethod
    async def latest(chat_id: int, mode: str = "default", limit: int = 3) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *
                FROM memory_timelines
                WHERE chat_id = $1
                AND mode = $2
                ORDER BY COALESCE(period_end, updated_at) DESC, id DESC
                LIMIT $3
            """, chat_id, mode, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def search(chat_id: int, mode: str, query: str, limit: int = 3) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return await MemoryTimeline.latest(chat_id=chat_id, mode=mode, limit=limit)

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (
                    SELECT plainto_tsquery('russian', $3) AS query
                )
                SELECT *,
                    ts_rank(to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(summary, '')), q.query) AS rank
                FROM memory_timelines
                CROSS JOIN q
                WHERE chat_id = $1
                AND mode = $2
                AND (
                    to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(summary, '')) @@ q.query
                    OR title ILIKE '%' || $3 || '%'
                    OR summary ILIKE '%' || $3 || '%'
                    OR $3 = ANY(topics)
                )
                ORDER BY rank DESC, COALESCE(period_end, updated_at) DESC
                LIMIT $4
            """, chat_id, mode, query, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def fetch_messages_for_period(chat_id: int, mode: str = "default", after_id: int = 0, limit: int = 200) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT id, chat_id, user_id, user_name, role, mode, message_text, created_at
                FROM memory_messages
                WHERE chat_id = $1
                AND mode = $2
                AND id > $3
                AND role IN ('user', 'assistant', 'memory')
                ORDER BY id ASC
                LIMIT $4
            """, chat_id, mode, after_id, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def latest_source_message_id(chat_id: int, mode: str = "default") -> int:
        # MEM-05: настоящий максимум по ВСЕМ элементам source_message_ids, а не
        # последний элемент — LLM возвращает id в произвольном порядке, и заниженный
        # after_id приводил к повторной обработке сообщений и дублям событий.
        async with get_db() as conn:
            value = await conn.fetchval("""
                SELECT COALESCE(MAX(m), 0)
                FROM memory_timelines t, LATERAL unnest(t.source_message_ids) AS m
                WHERE t.chat_id = $1
                AND t.mode = $2
            """, chat_id, mode)
            return int(value or 0)


class MemoryWikiPage:
    @staticmethod
    async def save(
        page_key: str,
        title: str,
        content: str,
        category: str,
        chat_id: Optional[int] = None,
        mode: str = "default",
        importance: float = 0.5,
        is_verified: bool = True,
        is_default: bool = False,
        conn=None,
    ) -> Optional[int]:
        page_key = (page_key or "").strip()
        title = (title or "").strip()
        content = (content or "").strip()
        if not page_key or not title or not content:
            return None

        async def _do(c):
            if chat_id is None:
                row = await c.fetchrow("""
                    INSERT INTO memory_wiki_pages (
                        chat_id, mode, page_key, title, content, category,
                        importance, is_verified, is_default, last_verified_at, created_at
                    )
                    VALUES (NULL, $1, $2, $3, $4, $5, $6, $7, $8, NOW(), NOW())
                    ON CONFLICT (mode, page_key) WHERE chat_id IS NULL
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        category = EXCLUDED.category,
                        importance = EXCLUDED.importance,
                        is_verified = EXCLUDED.is_verified,
                        is_default = EXCLUDED.is_default,
                        last_verified_at = NOW()
                    RETURNING id
                """, mode, page_key, title, content, category, float(importance or 0.5), is_verified, is_default)
            else:
                row = await c.fetchrow("""
                    INSERT INTO memory_wiki_pages (
                        chat_id, mode, page_key, title, content, category,
                        importance, is_verified, is_default, last_verified_at, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW())
                    ON CONFLICT (chat_id, mode, page_key)
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        category = EXCLUDED.category,
                        importance = EXCLUDED.importance,
                        is_verified = EXCLUDED.is_verified,
                        is_default = EXCLUDED.is_default,
                        last_verified_at = NOW()
                    RETURNING id
                """, chat_id, mode, page_key, title, content, category, float(importance or 0.5), is_verified, is_default)
            return row["id"] if row else None

        if conn is not None:
            return await _do(conn)
        async with get_db() as db_conn:
            return await _do(db_conn)

    @staticmethod
    async def get_by_key(chat_id: Optional[int], mode: str, page_key: str, conn=None) -> Optional[dict]:
        async def _do(c):
            row = await c.fetchrow("""
                SELECT *
                FROM memory_wiki_pages
                WHERE (chat_id = $1 OR (chat_id IS NULL AND $1 IS NULL))
                AND mode = $2
                AND page_key = $3
            """, chat_id, mode, page_key)
            return dict(row) if row else None

        if conn is not None:
            return await _do(conn)
        async with get_db() as db_conn:
            return await _do(db_conn)

    @staticmethod
    async def search(chat_id: int, mode: str, query: str, limit: int = 3) -> List[dict]:
        query = (query or "").strip()
        if not query:
            async with get_db() as conn:
                rows = await conn.fetch("""
                    SELECT *
                    FROM memory_wiki_pages
                    WHERE (chat_id = $1 OR chat_id IS NULL)
                    AND mode = $2
                    ORDER BY importance DESC, created_at DESC
                    LIMIT $3
                """, chat_id, mode, limit)
                return [dict(row) for row in rows]

        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (
                    SELECT plainto_tsquery('russian', $3) AS query
                )
                SELECT w.*,
                    ts_rank(to_tsvector('russian', coalesce(w.title, '') || ' ' || coalesce(w.content, '')), q.query) AS rank
                FROM memory_wiki_pages w
                CROSS JOIN q
                WHERE (w.chat_id = $1 OR w.chat_id IS NULL)
                AND w.mode = $2
                AND (
                    to_tsvector('russian', coalesce(w.title, '') || ' ' || coalesce(w.content, '')) @@ q.query
                    OR w.title ILIKE '%' || $3 || '%'
                    OR w.content ILIKE '%' || $3 || '%'
                    OR w.page_key ILIKE '%' || $3 || '%'
                )
                ORDER BY rank DESC, w.importance DESC
                LIMIT $4
            """, chat_id, mode, query, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def delete(chat_id: Optional[int], mode: str, page_key: str) -> bool:
        async with get_db() as conn:
            result = await conn.execute("""
                DELETE FROM memory_wiki_pages
                WHERE (chat_id = $1 OR (chat_id IS NULL AND $1 IS NULL))
                AND mode = $2
                AND page_key = $3
            """, chat_id, mode, page_key)
            return result.startswith("DELETE") and not result.endswith("0")


class UserEvent:
    @staticmethod
    async def add(chat_id: int, event_date: Any, event_type: str, note: str):
        """Добавить структурированное событие для пользователя (с UNIQUE дедупликацией)"""
        # Сначала убедимся, что chat_emotional_states существует
        await ChatEmotionalState.get_or_create(chat_id)
        async with get_db() as conn:
            await conn.execute("""
                INSERT INTO user_events (chat_id, event_date, event_type, note, notified, created_at)
                VALUES ($1, $2, $3, $4, FALSE, NOW())
                ON CONFLICT (chat_id, event_date, event_type)
                DO UPDATE SET note = EXCLUDED.note, notified = FALSE
            """, chat_id, event_date, event_type, note)

    @staticmethod
    async def get_upcoming_for_chat(chat_id: int, start_date: Any, end_date: Any) -> List[dict]:
        """Получить события в диапазоне дат для чата"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT event_date, event_type, note, notified 
                FROM user_events 
                WHERE chat_id = $1 AND event_date BETWEEN $2 AND $3
                ORDER BY event_date ASC
            """, chat_id, start_date, end_date)
            return [dict(row) for row in rows]


async def infer_user_timezone(chat_id: int, user_id: Optional[int], conn=None) -> Optional[int]:
    """
    Инферирует таймзону пользователя по геолокации или гистограмме его активности в чате.
    Порог: >= 20 сообщений.
    Смещение: int (офсет относительно UTC).

    conn: если передано существующее соединение (например, из открытой транзакции
    update_state) — переиспользуем его и НЕ захватываем второе соединение пула.
    Без этого вложенный get_db() внутри транзакции с FOR UPDATE приводит к
    самоблокировке пула под нагрузкой (RACE-01).
    """
    if user_id is None:
        return None

    # 1. Проверяем геолокацию (переиспользуем переданное соединение, если есть)
    loc = await UserLocation.get(user_id, conn=conn)
    if loc and loc.get("lng") is not None:
        user_tz = int(round(loc["lng"] / 15.0))
        logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] Таймзона для user_id={user_id} определена по геолокации: {user_tz:+d}")
        return user_tz

    # 2. Анализируем историю чата
    async def _fetch_history(c):
        return await c.fetch("""
            SELECT timestamp 
            FROM chat_history 
            WHERE chat_id = $1 
              AND user_name != 'Арти'
            ORDER BY timestamp DESC
            LIMIT 100
        """, chat_id)

    if conn is not None:
        rows = await _fetch_history(conn)
    else:
        async with get_db() as db_conn:
            rows = await _fetch_history(db_conn)

    # Порог входа в инференс: минимум 20 сообщений для стабильности
    if len(rows) < 20:
        return None

    # Вычисляем смещение времени сервера от UTC
    import time as _time
    server_offset = -_time.timezone if _time.daylight == 0 else -_time.altzone
    server_offset_hours = server_offset / 3600.0

    utc_hours = []
    for r in rows:
        dt = r["timestamp"]
        utc_dt = dt - timedelta(hours=server_offset_hours)
        utc_hours.append(utc_dt.hour)

    best_tz = None
    best_score = -999999

    for tz in range(-12, 15):
        score = 0
        for h in utc_hours:
            local_hour = (h + tz) % 24
            if 12 <= local_hour < 20:
                score += 2
            elif 8 <= local_hour < 23:
                score += 1
            elif 1 <= local_hour < 6:
                score += -5
            else:
                score += -1
        if score > best_score:
            best_score = score
            best_tz = tz

    if best_score > 0:
        logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] Таймзона для user_id={user_id} в чате {chat_id} инферирована из {len(rows)} сообщений: {best_tz:+d} (score={best_score})")
        return best_tz

    return None


# ARCH-01: чистая логика эмоц-машины вынесена в memory/emotion.py. Здесь —
# ре-экспорт для обратной совместимости (внешний код импортирует эти имена
# как `from database.models import SUPPORTED_MOODS / parse_emotional_introspection / ...`).
from memory.emotion import (  # noqa: E402
    SUPPORTED_MOODS,
    parse_emotional_introspection,
    strip_introspection_tags,
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
    async def update_state(chat_id: int, user_message: str, closeness: float = 0.0, user_id: Optional[int] = None, defer_sentiment: bool = False) -> dict:
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
                return result

    @staticmethod
    async def apply_mood_delta(chat_id: int, mood_delta: dict, source: str = "llm") -> None:
        """Применяет ограниченные дельты к вектору настроения (БЕЗ распада/заряда/времени).

        Используется пост-генерации: приоритетный источник — интроспекция LLM (source="llm"),
        иначе fail-closed фолбэк на словарь (source="keyword"). Каждая дельта клампится в
        [-0.25, 0.25], итоговое значение — в [0, 1]; ключи вне whitelist из 9 эмоций игнорируются.
        """
        if not mood_delta:
            return
        applied: dict = {}
        async with get_db() as conn:
            async with conn.transaction():
                state = await conn.fetchrow(
                    "SELECT mood_state FROM chat_emotional_states WHERE chat_id = $1 FOR UPDATE",
                    chat_id,
                )
                if not state:
                    return
                mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
                for emotion, d in mood_delta.items():
                    if emotion not in SUPPORTED_MOODS or emotion not in mood_dict:
                        continue
                    if not isinstance(d, (int, float)) or isinstance(d, bool):
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
    async def apply_turn_sentiment(chat_id: int, arti_response_text: str, keyword_mood_delta: Optional[dict] = None) -> Optional[str]:
        """Гибридный сентимент пост-генерации.

        Приоритет — интроспекция самой LLM (тег <!-- emotional_introspection --> из ответа Арти).
        Если тег отсутствует/битый/вне диапазона — fail-closed фолбэк на словарный
        keyword_mood_delta (посчитанный в update_state). Возвращает предложенный моод стикера
        (sticker_mood_suggest) или None.
        """
        parsed = parse_emotional_introspection(arti_response_text)
        if parsed is not None:
            if parsed["mood_delta"]:
                await ChatEmotionalState.apply_mood_delta(chat_id, parsed["mood_delta"], source="llm")
            else:
                emotional_logger.info(
                    f"[INTROSPECTION] chat_id={chat_id} | mood_delta пуст, sticker_suggest={parsed['sticker_mood_suggest']}"
                )
            return parsed["sticker_mood_suggest"]
        # Тег отсутствует/битый -> словарный фолбэк (fail-closed)
        if keyword_mood_delta:
            await ChatEmotionalState.apply_mood_delta(chat_id, keyword_mood_delta, source="keyword")
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
                
                mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
                # Бустим вес отправленного настроения
                if mood in mood_dict:
                    mood_dict[mood] = min(mood_dict[mood] + 0.4, 1.0)
                
                # Сброс заряда (эмоциональный катарсис)
                charge = 0.05
                
                # Логируем отправленный стикер и сброс заряда
                log_entry = (
                    f"[STICKER_SENT] chat_id={chat_id} | "
                    f"Sticker={file_id} | "
                    f"Mood={mood} | "
                    f"Charge Reset to 0.05"
                )
                logger.info(f"🔮 [ЭМОЦИОНАЛЬНАЯ МАШИНА] {log_entry}")
                emotional_logger.info(log_entry)
                
                await conn.execute("""
                    UPDATE chat_emotional_states
                    SET charge = $2,
                        mood_state = $3::jsonb,
                        last_sticker_time = NOW(),
                        sticker_history = $4::jsonb,
                        last_sent_sticker_mood = $5
                    WHERE chat_id = $1
                """, chat_id, charge, json.dumps(mood_dict, ensure_ascii=False), json.dumps(history, ensure_ascii=False), mood)


class AIModel:
    """Работа со списком ИИ моделей"""

    @staticmethod
    async def get_all_active() -> List[Dict[str, Any]]:
        """Получить все активные модели"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT key, name, model, provider, speed, intelligence, is_active, is_maintenance
                FROM ai_models
                WHERE is_active = TRUE
                ORDER BY created_at ASC, key ASC
            """)
            return [dict(r) for r in rows]

    @staticmethod
    async def get_by_key(key: str) -> Optional[Dict[str, Any]]:
        """Получить модель по ключу"""
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT key, name, model, provider, speed, intelligence, is_active, is_maintenance
                FROM ai_models
                WHERE key = $1
            """, key)
            return dict(row) if row else None

    @staticmethod
    async def get_by_model_id(model_id: str) -> Optional[Dict[str, Any]]:
        """Получить модель по ее идентификатору (модели)"""
        async with get_db() as conn:
            row = await conn.fetchrow("""
                SELECT key, name, model, provider, speed, intelligence, is_active, is_maintenance
                FROM ai_models
                WHERE model = $1
            """, model_id)
            return dict(row) if row else None

    @staticmethod
    async def search_and_filter(
        query: Optional[str] = None,
        provider: Optional[str] = None,
        speed: Optional[str] = None,
        intelligence: Optional[str] = None,
        limit: int = 5,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Поиск, фильтрация и пагинация моделей. Возвращает (список моделей, общее количество)"""
        conditions = ["is_active = TRUE"]
        params = []
        param_idx = 1

        if query:
            conditions.append(f"(key ILIKE ${param_idx} OR name ILIKE ${param_idx} OR model ILIKE ${param_idx} OR provider ILIKE ${param_idx})")
            params.append(f"%{query}%")
            param_idx += 1

        if provider:
            conditions.append(f"provider = ${param_idx}")
            params.append(provider)
            param_idx += 1

        if speed:
            conditions.append(f"speed = ${param_idx}")
            params.append(speed)
            param_idx += 1

        if intelligence:
            conditions.append(f"intelligence = ${param_idx}")
            params.append(intelligence)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        async with get_db() as conn:
            # Считаем общее число подходящих записей
            count_query = f"SELECT COUNT(*) FROM ai_models WHERE {where_clause}"
            total = await conn.fetchval(count_query, *params)

            # Получаем страницу записей
            select_query = f"""
                SELECT key, name, model, provider, speed, intelligence, is_active, is_maintenance
                FROM ai_models
                WHERE {where_clause}
                ORDER BY created_at ASC, key ASC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
            rows = await conn.fetch(select_query, *(params + [limit, offset]))
            return [dict(r) for r in rows], total

    @staticmethod
    async def get_unique_providers() -> List[str]:
        """Получить список уникальных провайдеров для фильтрации"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT provider
                FROM ai_models
                WHERE is_active = TRUE AND provider IS NOT NULL AND provider != ''
                ORDER BY provider ASC
            """)
            return [r['provider'] for r in rows]

    @staticmethod
    async def get_unique_speeds() -> List[str]:
        """Получить список уникальных уровней скорости для фильтрации"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT speed
                FROM ai_models
                WHERE is_active = TRUE AND speed IS NOT NULL AND speed != ''
                ORDER BY speed ASC
            """)
            return [r['speed'] for r in rows]

    @staticmethod
    async def get_unique_intelligences() -> List[str]:
        """Получить список уникальных уровней интеллекта для фильтрации"""
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT intelligence
                FROM ai_models
                WHERE is_active = TRUE AND intelligence IS NOT NULL AND intelligence != ''
                ORDER BY intelligence ASC
            """)
            return [r['intelligence'] for r in rows]



