"""
Модели данных для работы с PostgreSQL
"""
import json
import logging
import re
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict, Any, NamedTuple
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
        async with get_db() as conn, conn.transaction():
            from bot.saved_voice_sources import revoke
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'saved-voice-name:{user_id}:{name}')
            previous=await conn.fetchrow('SELECT id FROM saved_voices WHERE user_id=$1 AND name=$2 FOR UPDATE',user_id,name)
            if previous: await revoke(conn,user_id,previous['id'])
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
        async with get_db() as conn, conn.transaction():
            from bot.saved_voice_sources import revoke
            row=await conn.fetchrow('SELECT * FROM saved_voices WHERE user_id=$1 AND id=$2 FOR UPDATE',user_id,voice_id)
            if row is None: return None
            await revoke(conn,user_id,voice_id)
            await conn.execute('DELETE FROM saved_voices WHERE user_id=$1 AND id=$2',user_id,voice_id)
            return dict(row)

    @staticmethod
    async def delete_by_name(user_id: int, name: str) -> Optional[dict]:
        async with get_db() as conn, conn.transaction():
            from bot.saved_voice_sources import revoke
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'saved-voice-name:{user_id}:{name}')
            row=await conn.fetchrow('SELECT * FROM saved_voices WHERE user_id=$1 AND name=$2 FOR UPDATE',user_id,name)
            if row is None: return None
            await revoke(conn,user_id,row['id'])
            await conn.execute('DELETE FROM saved_voices WHERE user_id=$1 AND id=$2',user_id,row['id'])
            return dict(row)

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
    async def fetch_for_chunking(limit: int = 5000, after_id: int = 0, snapshot_id: int = None) -> List[dict]:
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT id, chat_id, user_id, user_name, role, mode, message_text, created_at
                FROM memory_messages
                WHERE id > $1
                AND ($3::bigint IS NULL OR id <= $3)
                AND role IN ('user', 'assistant', 'memory')
                ORDER BY id
                LIMIT $2
            """, after_id, limit, snapshot_id)
            return [dict(row) for row in rows]


def _vector_to_pg(value: List[float]) -> str:
    import math
    if not value or any(type(item) not in (int,float) or not math.isfinite(item) for item in value):
        raise ValueError('Embedding must contain finite numeric values')
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
    async def search_vector(
        chat_id: int,
        mode: str,
        query_vector: List[float],
        limit: int = 5,
        min_similarity: float = 0.0,
        embedding_model: str = None,
        user_id: int = None,
    ) -> List[dict]:
        if not query_vector:
            return []
        if embedding_model is None:
            from memory.embeddings import EMBEDDING_MODEL
            embedding_model = EMBEDDING_MODEL

        # min_similarity отсекает заведомо нерелевантные чанки: без порога ORDER BY
        # всегда вернёт top-k ближайших, даже если ближайший фрагмент не имеет
        # отношения к запросу, и мусор попадёт в контекст ответа.
        async with get_db() as conn:
            rows = await conn.fetch("""
                SELECT *,
                    1 - (embedding <=> $3::vector) AS similarity
                FROM memory_chunks
                WHERE chat_id = $1
                AND mode = $2
                AND embedding IS NOT NULL
                AND embedding_model = $6
                AND ($7::bigint IS NULL OR user_id IS NULL OR user_id=$7)
                AND (1 - (embedding <=> $3::vector)) >= $5
                ORDER BY embedding <=> $3::vector
                LIMIT $4
            """, chat_id, mode, _vector_to_pg(query_vector), limit, float(min_similarity),embedding_model,user_id)
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
                AND embedding_attempts < 3
                ORDER BY created_at ASC, id ASC
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

    @staticmethod
    async def record_embedding_failure(chunk_id: int):
        async with get_db() as conn:
            await conn.execute("UPDATE memory_chunks SET embedding_attempts=embedding_attempts+1,embedding_error_code='provider_unavailable' WHERE id=$1 AND embedding IS NULL",chunk_id)

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


class FactWriteResult(NamedTuple):
    id: Optional[int]
    created: bool


class MemoryFact:
    @staticmethod
    async def create(*args, **kwargs) -> Optional[int]:
        """Compatibility API; callers counting insertions use create_with_status."""
        return (await MemoryFact.create_with_status(*args, **kwargs)).id

    @staticmethod
    async def create_with_status(
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
    ) -> FactWriteResult:
        """Создаёт активный факт с дедупом в контексте владельца.

        conn: если передано соединение — работаем в нём (для атомарной консолидации
        в одной транзакции, MEM-01). Внутренний conn.transaction() в этом случае
        становится savepoint'ом существующей транзакции.
        """
        fact_text = (fact_text or "").strip()
        if not fact_text:
            return FactWriteResult(None, False)

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        importance_value = float(importance if importance is not None else 0.5)
        if not math.isfinite(importance_value):
            raise ValueError("Fact importance must be finite")
        importance_value = max(0.0, min(importance_value, 1.0))

        async def _do(c):
            async with c.transaction():
                existing_id = await c.fetchval("""
                    SELECT id
                    FROM memory_facts
                    WHERE chat_id = $1
                    AND mode = $2
                    AND lower(fact_text) = lower($3)
                    AND user_id IS NOT DISTINCT FROM $4
                    AND archived_at IS NULL
                    LIMIT 1
                """, chat_id, mode, fact_text, user_id)

                if existing_id:
                    fact_id = existing_id
                    created = False
                else:
                    fact_id = None
                    created = True

                # MEM-07: ON CONFLICT по partial-unique индексу активных фактов —
                # закрывает гонку двух параллельных вставок одинакового факта.
                row = None if fact_id else await c.fetchrow("""
                    INSERT INTO memory_facts (
                        chat_id, user_id, mode, summary, fact_text, importance,
                        source_message_id, metadata, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
                    ON CONFLICT (chat_id, mode, (user_id IS NULL), (COALESCE(user_id, 0)), lower(fact_text)) WHERE archived_at IS NULL
                    DO NOTHING
                    RETURNING id
                """, chat_id, user_id, mode, summary, fact_text, importance_value, source_message_id, metadata_json)

                if not row and not fact_id:
                    # Проиграли гонку — возвращаем id уже вставленного активного факта.
                    fact_id = await c.fetchval("""
                        SELECT id FROM memory_facts
                        WHERE chat_id = $1 AND mode = $2
                        AND lower(fact_text) = lower($3) AND archived_at IS NULL
                        AND user_id IS NOT DISTINCT FROM $4
                        ORDER BY id LIMIT 1
                    """, chat_id, mode, fact_text, user_id)
                    created = False

                if row:
                    fact_id = row["id"]
                if fact_id is None:
                    return FactWriteResult(None, False)
                for entity_id in entity_ids or []:
                    if not entity_id:
                        continue
                    await c.execute("""
                        INSERT INTO memory_fact_entities (fact_id, entity_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    """, fact_id, entity_id)

                return FactWriteResult(fact_id, created)

        if conn is not None:
            return await _do(conn)
        async with get_db() as db_conn:
            return await _do(db_conn)

    @staticmethod
    async def search(chat_id: int, query: str, mode: str = "default", limit: int = 5,
                     user_id: Optional[int] = None) -> List[dict]:
        """Explicit retrieval ignores expression cooldown; unscoped graph is quarantined."""
        query = (query or "").strip()
        async with get_db() as conn:
            rows = await conn.fetch("""
                WITH q AS (SELECT plainto_tsquery('russian', $2) AS query)
                SELECT f.*, ts_rank(to_tsvector('russian', coalesce(f.summary, '') || ' ' || f.fact_text), q.query) AS rank
                FROM memory_facts f CROSS JOIN q
                WHERE f.chat_id = $1 AND f.mode = $3 AND f.archived_at IS NULL
                  AND ($5::bigint IS NULL OR f.user_id = $5 OR f.user_id IS NULL)
                  AND ($2 = '' OR to_tsvector('russian', coalesce(f.summary, '') || ' ' || f.fact_text) @@ q.query
                       OR f.fact_text ILIKE '%' || $2 || '%' OR f.summary ILIKE '%' || $2 || '%')
                ORDER BY ts_rank(to_tsvector('russian', coalesce(f.summary, '') || ' ' || f.fact_text), q.query) * 2.0 + f.importance DESC,
                         created_at DESC, id DESC
                LIMIT $4
            """, chat_id, query, mode, max(1, min(int(limit), 100)), user_id)
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
        """Retired legacy profile mutation; no writes or provider calls."""
        return None

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
        """Retired legacy profile mutation; no writes or provider calls."""
        return None

    @staticmethod
    async def grow_closeness(chat_id: int, user_id: int, mode: str, proactive_reply: bool = False):
        """Retired legacy profile mutation; no writes or provider calls."""
        return None


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
    async def get_by_key(chat_id: Optional[int], mode: str, page_key: str, conn=None, verified_only: bool = False) -> Optional[dict]:
        async def _do(c):
            row = await c.fetchrow("""
                SELECT *
                FROM memory_wiki_pages
                WHERE (chat_id = $1 OR (chat_id IS NULL AND $1 IS NULL))
                AND mode = $2
                AND page_key = $3
                AND (NOT $4::boolean OR is_verified IS TRUE)
            """, chat_id, mode, page_key, verified_only)
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
                    AND is_verified IS TRUE
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
                AND w.is_verified IS TRUE
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
        """Retired event extractor; new intentions carry source provenance."""
        return None

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
    """Retired compatibility API. It cannot create or mutate emotional state."""
    @staticmethod
    async def get_or_create(chat_id: int) -> dict:
        async with get_db() as conn:
            row = await conn.fetchrow('SELECT * FROM chat_emotional_states WHERE chat_id=$1',chat_id)
            return dict(row) if row else {}

    @staticmethod
    async def update_state(*args, **kwargs) -> dict:
        return {}

    @staticmethod
    async def apply_mood_delta(*args, **kwargs):
        return None

    @staticmethod
    async def apply_turn_sentiment(*args, **kwargs):
        return None

    @staticmethod
    async def record_sticker_sent(*args, **kwargs):
        return None




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



