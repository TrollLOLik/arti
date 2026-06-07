"""
Управление подключением к PostgreSQL
"""
import logging
import os
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Глобальный пул соединений
_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Инициализация пула соединений с PostgreSQL"""
    global _pool
    
    if _pool is not None:
        logger.info("Пул соединений уже инициализирован")
        return
    
    # Параметры подключения из .env
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = int(os.getenv("DB_PORT", "5432"))
    db_name = os.getenv("DB_NAME", "arti_bot")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "")
    
    if not db_password:
        logger.warning("DB_PASSWORD не установлен, используем пустой пароль")
    
    try:
        logger.info(f"Подключение к PostgreSQL: {db_host}:{db_port}/{db_name}")
        
        _pool = await asyncpg.create_pool(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        
        logger.info("Пул соединений PostgreSQL успешно создан")
        
        # Создаем схемы таблиц
        await create_tables()
        
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")
        raise


async def create_tables(conn=None):
    """Создание таблиц в базе данных"""
    if conn is None:
        async with get_db() as db_conn:
            await _create_tables_internal(db_conn)
    else:
        await _create_tables_internal(conn)


async def _create_tables_internal(conn):
    """Внутренняя функция создания таблиц"""
    # История чатов (с датами)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
            user_name VARCHAR(255) NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id_timestamp
        ON chat_history(chat_id, timestamp DESC)
    """)

    # История RP-чатов (отдельная таблица для режима ролеплея)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history_rp (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
            user_name VARCHAR(255) NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_history_rp_chat_id_timestamp
        ON chat_history_rp(chat_id, timestamp DESC)
    """)


    
    # Активные пользователи
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_users (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            last_activity TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id, user_id)
        )
    """)
    
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_active_users_chat_user 
        ON active_users(chat_id, user_id)
    """)
    
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_active_users_last_activity 
        ON active_users(last_activity)
    """)
    
    # Спам-защита
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spam_protection (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            blocked_until TIMESTAMP,
            warnings_sent BOOLEAN DEFAULT FALSE,
            last_command_time TIMESTAMP,
            command_count INTEGER DEFAULT 0,
            command_timestamps JSONB DEFAULT '[]'::jsonb,
            UNIQUE(chat_id, user_id)
        )
    """)
    
    # Миграция: добавляем столбец command_timestamps если его нет
    await conn.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'spam_protection' 
                AND column_name = 'command_timestamps'
            ) THEN
                ALTER TABLE spam_protection 
                ADD COLUMN command_timestamps JSONB DEFAULT '[]'::jsonb;
            END IF;
        END $$;
    """)
    
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_spam_protection_chat_user 
        ON spam_protection(chat_id, user_id)
    """)
    
    # Статус ответов бота
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS response_status (
            chat_id BIGINT PRIMARY KEY,
            enabled BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    

    
    # Выбор модели ИИ для чата
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_models (
            chat_id BIGINT PRIMARY KEY,
            model_id VARCHAR(50) NOT NULL DEFAULT 'gemini-3.1-flash-lite-preview',
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # Геолокация пользователей (персистентная)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_locations (
            user_id BIGINT PRIMARY KEY,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            address TEXT,
            city TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_locations_updated_at
        ON user_locations(updated_at)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_voices (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            name VARCHAR(80) NOT NULL,
            catbox_url TEXT NOT NULL,
            catbox_file_id VARCHAR(255),
            source_kind VARCHAR(80),
            cleaned BOOLEAN DEFAULT FALSE,
            duration_sec DOUBLE PRECISION,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMP,
            UNIQUE(user_id, name)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_saved_voices_user_id_created_at
        ON saved_voices(user_id, created_at DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_saved_voices_user_id_name
        ON saved_voices(user_id, name)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT,
            user_name VARCHAR(255) NOT NULL,
            role VARCHAR(32) NOT NULL DEFAULT 'user',
            mode VARCHAR(32) NOT NULL DEFAULT 'default',
            source VARCHAR(64) NOT NULL DEFAULT 'chat',
            message_text TEXT NOT NULL,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_messages_chat_created
        ON memory_messages(chat_id, created_at DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_messages_user_created
        ON memory_messages(user_id, created_at DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_messages_text_ru
        ON memory_messages USING GIN (
            to_tsvector('russian', coalesce(message_text, ''))
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_entities (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            entity_type VARCHAR(64) NOT NULL DEFAULT 'unknown',
            mention_count INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id, normalized_name)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_entities_chat_last_seen
        ON memory_entities(chat_id, last_seen_at DESC)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_entity_aliases (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            entity_id BIGINT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id, normalized_alias)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_entity_aliases_entity
        ON memory_entity_aliases(entity_id)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_facts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT,
            mode VARCHAR(32) NOT NULL DEFAULT 'default',
            summary TEXT,
            fact_text TEXT NOT NULL,
            importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            source_message_id BIGINT REFERENCES memory_messages(id) ON DELETE SET NULL,
            metadata JSONB DEFAULT '{}'::jsonb,
            used_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TIMESTAMP,
            cooldown_until TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        ALTER TABLE memory_facts
        ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP
    """)

    await conn.execute("""
        ALTER TABLE memory_facts
        ADD COLUMN IF NOT EXISTS archive_reason TEXT
    """)

    await conn.execute("""
        ALTER TABLE memory_facts
        ADD COLUMN IF NOT EXISTS superseded_by BIGINT REFERENCES memory_facts(id) ON DELETE SET NULL
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_facts_chat_created
        ON memory_facts(chat_id, created_at DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_facts_chat_cooldown
        ON memory_facts(chat_id, cooldown_until)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_facts_chat_mode_archived
        ON memory_facts(chat_id, mode, archived_at)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_facts_consolidation
        ON memory_facts(chat_id, mode, importance, created_at)
        WHERE archived_at IS NULL
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_facts_text_ru
        ON memory_facts USING GIN (
            to_tsvector('russian', coalesce(summary, '') || ' ' || coalesce(fact_text, ''))
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_fact_entities (
            fact_id BIGINT NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
            entity_id BIGINT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
            PRIMARY KEY (fact_id, entity_id)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_fact_entities_entity
        ON memory_fact_entities(entity_id)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_relations (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            source_entity_id BIGINT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
            target_entity_id BIGINT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
            relation_type VARCHAR(80) NOT NULL,
            description TEXT,
            weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_relations_source
        ON memory_relations(source_entity_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_relations_target
        ON memory_relations(target_entity_id)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_user_profiles (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            mode VARCHAR(32) NOT NULL DEFAULT 'default',
            profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            profile_text TEXT NOT NULL DEFAULT '',
            source_fact_ids BIGINT[] NOT NULL DEFAULT '{}',
            source_entity_ids BIGINT[] NOT NULL DEFAULT '{}',
            facts_version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id, user_id, mode)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_user_profiles_lookup
        ON memory_user_profiles(chat_id, user_id, mode)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_timelines (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT,
            mode VARCHAR(32) NOT NULL DEFAULT 'default',
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            topics TEXT[] NOT NULL DEFAULT '{}',
            source_message_ids BIGINT[] NOT NULL DEFAULT '{}',
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_timelines_chat_mode_period
        ON memory_timelines(chat_id, mode, period_end DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_timelines_topics
        ON memory_timelines USING GIN(topics)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_timelines_text_ru
        ON memory_timelines USING GIN (
            to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(summary, ''))
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_wiki_pages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            mode VARCHAR(32) NOT NULL DEFAULT 'default',
            page_key VARCHAR(80) NOT NULL,
            title VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            category VARCHAR(64) NOT NULL,
            importance DOUBLE PRECISION DEFAULT 0.5,
            is_verified BOOLEAN DEFAULT TRUE,
            last_verified_at TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(chat_id, mode, page_key)
        )
    """)

    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_wiki_pages_global_unique
        ON memory_wiki_pages (mode, page_key)
        WHERE chat_id IS NULL
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_wiki_pages_lookup
        ON memory_wiki_pages(chat_id, mode, page_key)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_wiki_pages_text_ru
        ON memory_wiki_pages USING GIN (
            to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(content, ''))
        )
    """)

    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as e:
        logger.warning(f"Расширение pgvector недоступно, vector RAG будет отключен: {e}")

    vector_available = await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_type
            WHERE typname = 'vector'
        )
    """)

    if vector_available:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                mode VARCHAR(32) NOT NULL DEFAULT 'default',
                chunk_text TEXT NOT NULL,
                message_ids BIGINT[] NOT NULL DEFAULT '{}',
                token_estimate INTEGER NOT NULL DEFAULT 0,
                embedding vector(1536),
                embedding_model VARCHAR(80),
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                embedded_at TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_chat_mode_created
            ON memory_chunks(chat_id, mode, created_at DESC)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_message_ids
            ON memory_chunks USING GIN(message_ids)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_text_ru
            ON memory_chunks USING GIN (
                to_tsvector('russian', coalesce(chunk_text, ''))
            )
        """)

        try:
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_chunks_embedding_hnsw
                ON memory_chunks USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL
            """)
        except Exception as e:
            logger.warning(f"HNSW индекс pgvector недоступен, vector search будет работать без ANN-индекса: {e}")
    # Таблица эмоциональных состояний чата
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_emotional_states (
            chat_id BIGINT PRIMARY KEY,
            charge DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            mood_state JSONB NOT NULL DEFAULT '{"happy": 0.0, "sad": 0.0, "angry": 0.0, "love": 0.0, "teasing": 0.0, "shock": 0.0, "blush": 0.0, "bored": 0.0, "thinking": 0.0}'::jsonb,
            last_sticker_time TIMESTAMP,
            last_activity_time TIMESTAMP NOT NULL DEFAULT NOW(),
            sticker_history JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_sent_sticker_mood VARCHAR(32),
            last_proactive_push_time TIMESTAMP,
            conversation_stage VARCHAR(32) DEFAULT 'active',
            user_tz INTEGER
        )
    """)

    # Миграция: добавляем столбцы user_tz, last_proactive_push_time, conversation_stage в chat_emotional_states если их нет
    await conn.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'chat_emotional_states' 
                AND column_name = 'user_tz'
            ) THEN
                ALTER TABLE chat_emotional_states 
                ADD COLUMN user_tz INTEGER;
            END IF;
            
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'chat_emotional_states' 
                AND column_name = 'last_proactive_push_time'
            ) THEN
                ALTER TABLE chat_emotional_states 
                ADD COLUMN last_proactive_push_time TIMESTAMP;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'chat_emotional_states' 
                AND column_name = 'conversation_stage'
            ) THEN
                ALTER TABLE chat_emotional_states 
                ADD COLUMN conversation_stage VARCHAR(32) DEFAULT 'active';
            END IF;
        END $$;
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_emotional_states_activity 
        ON chat_emotional_states(last_activity_time DESC)
    """)

    # Таблица структурированных событий и напоминаний пользователя
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_events (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES chat_emotional_states(chat_id) ON DELETE CASCADE,
            event_date DATE NOT NULL,
            event_type VARCHAR(50) NOT NULL,
            note TEXT NOT NULL,
            notified BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # Миграция: добавляем notified и UNIQUE ограничение в user_events
    await conn.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'user_events' 
                AND column_name = 'notified'
            ) THEN
                ALTER TABLE user_events 
                ADD COLUMN notified BOOLEAN DEFAULT FALSE;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints 
                WHERE constraint_name = 'uq_chat_event_date_type'
            ) THEN
                ALTER TABLE user_events 
                ADD CONSTRAINT uq_chat_event_date_type UNIQUE (chat_id, event_date, event_type);
            END IF;
        END $$;
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_events_lookup
        ON user_events(chat_id, event_date)
    """)

    # Таблица кэша классификации стикеров
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sticker_pack_mappings (
            pack_name VARCHAR(128) NOT NULL,
            mood_name VARCHAR(32) NOT NULL,
            file_id VARCHAR(255) NOT NULL,
            emoji VARCHAR(32),
            PRIMARY KEY (pack_name, file_id)
        )
    """)

    # Таблица моделей ИИ
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_models (
            key VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            model VARCHAR(255) NOT NULL,
            provider VARCHAR(100),
            speed VARCHAR(10),
            intelligence VARCHAR(10),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_maintenance BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    # Миграция: добавляем столбец is_maintenance если его нет
    await conn.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'ai_models' 
                AND column_name = 'is_maintenance'
            ) THEN
                ALTER TABLE ai_models 
                ADD COLUMN is_maintenance BOOLEAN NOT NULL DEFAULT FALSE;
            END IF;
        END $$;
    """)

    # Сидирование начальных моделей ИИ
    count = await conn.fetchval("SELECT COUNT(*) FROM ai_models")
    if count == 0:
        logger.info("Сидирование начальных моделей ИИ в таблицу ai_models...")
        seed_data = [
            ("gemini", "Gemini 3.1 Flash", "gemini-3.1-flash-lite-preview", "Google", "S+", "A", False),
            ("sonnet", "Sonnet 4.6 (maintenance)", "kr/claude-sonnet-4.6", "Anthropic", "S", "S+", True),
            ("opus", "Opus 4.8 (maintenance)", "kr/claude-opus-4.7", "Anthropic", "S", "S++", True),
            ("deepseek", "DeepSeek v4 Pro", "nvidia/deepseek-ai/deepseek-v4-pro", "DeepSeek", "S", "S", False),
            ("geminipro", "Gemini 3.1 Pro (maintenance)", "capy/gemini-3.1-pro-preview", "Google", "A+", "S+", True),
            ("minimax", "MiniMax M3", "opencode-zen/minimax-m3-free", "MiniMax", "B", "S+", False),
            ("kimi", "Kimi K2.6", "nvidia/moonshotai/kimi-k2.6", "Moonshot", "A", "S", False),
            ("glm", "GLM 5.1", "nvidia/z-ai/glm-5.1", "GLM", "B", "S+", False),
            ("gpt", "GPT 5.5", "fmd/gpt-5.5", "OpenAI", "A", "S++", False),
            ("grok", "Grok 4.3", "pol/grok-4.3", "xAI", "S", "A", False),
            ("qwen", "Qwen 3.6 Plus", "fireworks/qwen3p6-plus", "Qwen", "A", "S", False),
            ("step", "Step 3.7 Flash", "nvidia/stepfun-ai/step-3.7-flash", "Step", "A", "A+", False),
            ("mimo", "Mimo V2.5 Pro", "opencode-zen/mimo-v2.5-free", "Mimo", "A", "A+", False),
            ("nemotron", "Nemotron 3 Ultra", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "Nvidia", "A", "S", False),
        ]
        for key, name, model, provider, speed, intelligence, is_maintenance in seed_data:
            await conn.execute("""
                INSERT INTO ai_models (key, name, model, provider, speed, intelligence, is_maintenance, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
            """, key, name, model, provider, speed, intelligence, is_maintenance)
        logger.info(f"Успешно добавлено {len(seed_data)} начальных моделей ИИ")
    else:
        # Миграция существующих строк: обновляем признак обслуживания для начальных моделей
        await conn.execute("""
            UPDATE ai_models 
            SET is_maintenance = TRUE 
            WHERE key IN ('sonnet', 'opus', 'geminipro')
        """)

    logger.info("Таблицы базы данных созданы/проверены")



@asynccontextmanager
async def get_db():
    """Получить соединение с базой данных"""
    global _pool
    
    if _pool is None:
        await init_db()
    
    async with _pool.acquire() as conn:
        yield conn


async def close_db():
    """Закрыть пул соединений"""
    global _pool
    
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Пул соединений PostgreSQL закрыт")

