# Arti Long-Term Memory — Полный контекст для AI ассистента

## Проект
Telegram-бот "Арти" с многослойной долговременной памятью на PostgreSQL + pgvector.

---

## Архитектура памяти (слоёная)

```
┌─────────────────────────────────────────┐
│  Prompt Injection Layer                 │  ← build_memory_context()
│  - Core profile (user)                   │
│  - Timeline events                       │
│  - Entity graph (mentions + relations)   │
│  - Facts (top-5)                         │
│  - Semantic chunks (top-1 fallback)      │
│  - Raw messages (fallback)               │
├─────────────────────────────────────────┤
│  Retrieval Layer                          │
│  - Vector search (HNSW)                  │
│  - FTS + text search                     │
│  - Entity mention matching               │
│  - Relation graph traversal              │
├─────────────────────────────────────────┤
│  Storage Layer                            │
│  - memory_messages                       │
│  - memory_entities / entity_aliases      │
│  - memory_facts (soft-archive)            │
│  - memory_relations                      │
│  - memory_chunks (+ embeddings)          │
│  - memory_user_profiles                  │
│  - memory_timelines                      │
│  - [FUTURE] memory_wiki_pages            │
├─────────────────────────────────────────┤
│  Maintenance Layer                        │
│  - Fact consolidation (Gemini)           │
│  - Profile refresh (Gemini)              │
│  - Timeline build (Gemini)               │
│  - Chunk backfill                        │
└─────────────────────────────────────────┘
```

---

## Stage 1: Core Memory Tables (ГОТОВО)

### Таблицы

- **`memory_messages`** — все сообщения чата (roles: user, assistant, system, memory, memory_extractor)
- **`memory_entities`** — именованные сущности с `canonical_name`, `normalized_name`, `mention_count`, `last_seen_at`
- **`entity_aliases`** — альтернативные имена сущностей → `entity_id`
- **`memory_facts`** — извлечённые факты: `fact_text`, `summary`, `importance`, `confidence`, `archived_at`, `archive_reason`, `superseded_by`
- **`fact_entity_links`** — many-to-many: `fact_id` ↔ `entity_id`
- **`memory_relations`** — граф: `source_entity_id`, `target_entity_id`, `relation_type`, `weight`, `description`

### Soft-archive pattern (факты)

```sql
archived_at TIMESTAMP,
archive_reason VARCHAR(32),  -- 'consolidated', 'outdated', 'superseded'
superseded_by BIGINT         -- id нового факта
```

Фильтр `archived_at IS NULL` применяется во всех SELECT.

---

## Stage 2: Vector RAG (ГОТОВО)

### Чанкинг

- `memory_chunks` — семантические чанки с `embedding vector(1536)`
- Gemini `gemini-embedding-2`
- HNSW pgvector индекс (без ivfflat)
- asyncpg сериализация: `_vector_to_pg(list[float])` → строка для SQL
- Compact chunking: сообщения группируются в чанки ~1000-1500 токенов

### Retrieval fallback chain

```
1. MemoryChunk.search_vector(chat_id, mode, query_vector, limit=2)
   ↓ если ошибка
2. MemoryChunk.search_text(chat_id, mode, query, limit=2)
   ↓ если пусто
3. MemoryMessage.search(chat_id, query, mode, limit=2)
```

### Backfill

- `backfill_memory_chunks.py` — скрипт для создания чанков и embeddings из СУЩЕСТВУЮЩИХ `memory_messages`
- **ВАЖНО**: код есть, но НЕОБХОДИМО реально прогнать:
  ```powershell
  python setup_database.py      # создаст pgvector extension + таблицы
  python backfill_memory_chunks.py  # заполнит chunks для старых сообщений
  ```
- Без бэкфилла старые сообщения НЕ будут доступны через vector search.

---

## Stage 3: Entity Graph + Fact Consolidation (ГОТОВО, но есть пробелы)

### Что реализовано

**Entity mention detection:**
- `MemoryEntity.find_mentions(chat_id, text, limit=5)` — ищет по `canonical_name`, `normalized_name`, `entity_aliases`
- Работает на основе keyword matching, НЕ semantic

**Relation retrieval:**
- `MemoryRelation.find_for_entities(chat_id, entity_ids, limit=8)` — получает связи вокруг упомянутых сущностей
- Форматируется как: `Имя1 дружит с Имя2`

**Graph context в prompt:**
```
Сущности в текущем контексте: Имя1, Имя2
Связи в текущем контексте:
- Имя1 дружит с Имя2
```

**Fact consolidation:**
- `memory/consolidator.py` — Gemini-based дедупликация
- `consolidate_chat_facts(chat_id, mode, limit, dry_run=True)`
- `maybe_consolidate(...)` — вызывается в `remember_exchange()`, dry-run по умолчанию
- Soft-archive: старые факты архивируются, новые — записываются
- CLI: `python maintain_memory.py --chat-id X --apply`

### Config flags (консолидация)

```python
MEMORY_CONSOLIDATION_AUTO = True       # авто-триггер в remember_exchange
MEMORY_CONSOLIDATION_APPLY = False     # False = dry-run
MEMORY_CONSOLIDATION_INTERVAL = 50      # сообщений между проверками
```

### Что НЕ реализовано / используется не на полную

**1. Entity graph retrieval ограничен**

Сейчас retrieval идёт в основном через:
- `MemoryFact.search(chat_id, query, mode, limit=5)` — top-5 фактов
- `MemoryChunk.search_vector(..., limit=2)` — top-2 семантических чанка
- `MemoryChunk.search_text(..., limit=2)` — fallback text search
- `MemoryMessage.search(..., limit=2)` — fallback raw messages

Graph context (`find_mentions` + `find_for_entities`) добавляется ОТДЕЛЬНО, но:
- НЕТ `MemoryEntity.find_related()` — поиск связанных сущностей через граф (2-hop traversal)
- НЕТ извлечения relations вокруг найденных entities БЕЗ упоминания в текущем сообщении
- Graph block добавляется в prompt, но не участвует в семантическом ранжировании

**2. Semantic retrieval не разделён по приоритетам**

По roadmap должно быть:
- Vector chunks: **top-1** как fallback (сейчас `log_limit=2`)
- Facts: **top-5** (уже так)
- Graph context: добавляется **отдельно**, не в ранжировании

**3. Consolidation неполный**

Реализовано:
- ✅ Объединение похожих фактов через Gemini
- ✅ Soft-archive старых фактов
- ✅ CLI + auto-trigger (dry-run)

НЕ реализовано:
- ❌ Разрешение конфликтов (когда два факта противоречат друг другу)
- ❌ Автоматическое повышение/понижение `importance` на основе использования
- ❌ Полное удаление устаревшего (сейчас только soft-archive)
- ❌ Обновление существующего факта вместо создания нового при незначительных изменениях

---

## Stage 4: Profiles + Timeline (ТОЛЬКО ЧТО РЕАЛИЗОВАНО)

### Новые таблицы

**`memory_user_profiles`**
- `id, chat_id, user_id, mode, profile_json(JSONB), profile_text, source_fact_ids[], source_entity_ids[], facts_version, updated_at`
- UNIQUE(chat_id, user_id, mode)

**`memory_timelines`**
- `id, chat_id, user_id, mode, period_start, period_end, title, summary, topics[], source_message_ids[], metadata(JSONB), created_at, updated_at`
- Индексы: `(chat_id, mode, period_end DESC)`, GIN(topics), GIN FTS (russian)

### Новые модели (`database/models.py`)

**`MemoryUserProfile`**
- `get(chat_id, user_id, mode)` — получить профиль
- `upsert(...)` — INSERT ... ON CONFLICT UPDATE
- `fetch_source_material(chat_id, user_id, mode)` — выборка active facts + top entities для агрегации

**`MemoryTimeline`**
- `create(...)` — создать timeline event
- `latest(chat_id, mode, limit)` — последние события
- `search(chat_id, mode, query, limit)` — FTS + ILIKE + topics search
- `fetch_messages_for_period(...)` — сообщения после последнего timeline
- `latest_source_message_id(...)` — max source_message_ids для продолжения

### Новые модули

**`memory/profiles.py`**
- `refresh_user_profile(chat_id, user_id, mode, dry_run=True)` — Gemini агрегирует facts/entities в профиль
- `get_profile_context(...)` — формирует `Core profile: ...` для prompt
- Возвращает JSON: display_name, stable_preferences, communication_style, important_facts, relationship_to_arti, profile_text
- Profile text: 600-1200 символов

**`memory/timeline.py`**
- `build_timeline_events(chat_id, mode, limit, dry_run=True)` — Gemini сжимает сообщения в timeline events
- `get_timeline_context(...)` — формирует `Сжатая хронология: ...` для prompt
- Минимум `MEMORY_TIMELINE_MIN_MESSAGES = 30` сообщений для запуска
- Возвращает JSON: events[] с title, summary, topics, source_message_ids

### Интеграция в prompt

В `memory/storage.py::build_memory_context()` теперь в начало добавляется:

```
[Долговременная память Арти: используй только если релевантно текущему ответу]

Core profile:
Пользователь: Александр. Любит... стиль общения...

Сжатая хронология:
- [2026-05-28] Обсуждали настройку памяти Арти...

Сущности в текущем контексте: ...
Связи в текущем контексте: ...
Ассоциации: ...
...
```

### Config flags (profiles/timeline)

```python
MEMORY_PROFILES_ENABLED = True         # подключать profile к prompt
MEMORY_TIMELINE_ENABLED = True        # подключать timeline к prompt
MEMORY_PROFILE_APPLY = False          # False = dry-run в maintenance
MEMORY_TIMELINE_APPLY = False         # False = dry-run в maintenance
MEMORY_TIMELINE_MIN_MESSAGES = 30     # мин. сообщений для timeline build
```

### Maintenance CLI

`maintain_memory.py` расширен:

```bash
# Консолидация (как раньше)
python maintain_memory.py --chat-id 1883131998 --mode default
python maintain_memory.py --chat-id 1883131998 --mode default --apply

# Profile (dry-run по умолчанию)
python maintain_memory.py --chat-id 1883131998 --mode default --profile --user-id 1883131998

# Timeline (dry-run по умолчанию)
python maintain_memory.py --chat-id 1883131998 --mode default --timeline

# Всё сразу + apply
python maintain_memory.py --chat-id 1883131998 --mode default --profile --timeline --apply --user-id 1883131998
```

---

## Будущие этапы (TODO)

### 1. Усилить Entity Graph Retrieval

- `MemoryEntity.find_related(chat_id, entity_id, depth=2)` — 2-hop graph traversal
- Добавить relations для entities, найденных НЕ через упоминание, а через semantic search
- Graph-based reranking: связанные сущности получают boost в ранжировании
- Добавить блок "Связанные сущности / отношения" в `build_memory_context` отдельно от прямых mentions

### 2. Semantic retrieval top-1

- Vector chunks: `limit=1` для compact fallback
- Facts: оставить `limit=5`
- Graph context: добавлять отдельно, не считать в лимитах retrieval

### 3. Дополнить Consolidation

- Разрешение конфликтов (contradiction detection)
- Автоматическое повышение/понижение `importance` на основе:
  - Количества использований (`usage_count`)
  - Возраста факта
  - Подтверждения / опровержения в новых сообщениях
- Hard-delete для soft-archived фактов старше N дней (опционально)
- Обновление существующего факта при незначительных изменениях (patch вместо create)

### 4. Wiki / Lore Арти (СЛЕДУЮЩИЙ БОЛЬШОЙ ЭТАП)

- Таблица: `memory_wiki_pages` или `arti_wiki`
- Поля: `page_key`, `title`, `content`, `category`, `importance`, `last_verified_at`, `created_by` (manual/auto)
- Постоянные знания о:
  - Характере Арти (личность, манера речи, предпочтения)
  - Мире (сеттинг, правила, каноны)
  - Отношениях (с конкретными пользователями, группами)
  - Правилах RP (что можно/нельзя, границы)
- Ручная/полуавтоматическая модерация:
  - Flag `is_verified` — подтверждено вручную
  - CLI для review и approve suggested wiki entries
  - Gemini может предлагать новые wiki entries на основе часто упоминаемых фактов
- Подключение к prompt через config flag: `MEMORY_WIKI_ENABLED`

### 5. Авто-обновление Profiles и Timeline

Сейчас обновление ТОЛЬКО через CLI. Авто-триггер в `remember_exchange()`:

```python
# memory/storage.py::remember_exchange()
if MEMORY_PROFILES_ENABLED and MEMORY_PROFILE_APPLY:
    await maybe_refresh_profile(chat_id, user_id, mode)
if MEMORY_TIMELINE_ENABLED and MEMORY_TIMELINE_APPLY:
    await maybe_build_timeline(chat_id, mode)
```

**Рекомендация**: сначала потестировать вручную через CLI, потом включить авто.

### 6. Оптимизации

- **Profile caching**: `get_profile_context()` делает SELECT при КАЖДОМ запросе. LRU cache ~60 сек.
- **Timeline semantic search**: сейчас только FTS + ILIKE. Добавить embeddings для timeline summaries.
- **Batch timeline build**: `limit=200` сообщений — настраивать по размеру чата.
- **Memory context size guard**: лимит на общий размер блока памяти в prompt (сейчас `lines[:14]`)

### 7. Мониторинг

- Размер `profile_text` (max ~1200 символов)
- Количество timeline events per chat
- Время `build_memory_context()`
- Hit rate по каждому retrieval слою (vector / text / messages)

---

## Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `config.py` | Все feature flags |
| `database/connection.py` | Schema (tables + indexes) |
| `database/models.py` | ORM-методы (MemoryEntity, MemoryFact, MemoryRelation, MemoryUserProfile, MemoryTimeline) |
| `memory/storage.py` | `build_memory_context()`, `remember_exchange()` |
| `memory/profiles.py` | `refresh_user_profile()`, `get_profile_context()` |
| `memory/timeline.py` | `build_timeline_events()`, `get_timeline_context()` |
| `memory/consolidator.py` | `consolidate_chat_facts()`, `maybe_consolidate()` |
| `memory/embeddings.py` | `embed_document()`, `embed_query()` |
| `memory/chunking.py` | `build_compact_chunks()` |
| `memory/normalizer.py` | `compact_text()`, `keyword_query()`, `normalize_entity_name()` |
| `memory/extractor.py` | `extract_memory()` — извлечение сущностей/фактов из сообщений |
| `maintain_memory.py` | CLI для ручного maintenance |
| `backfill_memory_chunks.py` | Бэкфилл векторных чанков |
| `setup_database.py` | Инициализация/обновление схемы |

---

## Архитектурные принципы

1. **Dry-run по умолчанию** — любая запись в БД требует явного `dry_run=False` или `--apply`
2. **Soft-archive** — ничего не удаляется, только помечается `archived_at`
3. **Config flags** — все новые фичи включаются/выключаются через флаги в `config.py`
4. **Fallback chains** — vector → text → messages; если слой недоступен, переходим к следующему
5. **Compact text** — все строки в prompt проходят через `compact_text()` с лимитами
6. **Idempotent schema** — `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`

---

## Известные ограничения

- `profile_text` ограничен ~1200 символов — может потеряться деталь при большом количестве фактов
- Timeline ищет только по тексту (FTS + ILIKE), без семантического поиска
- Profile/timeline обновляются только через CLI (нет авто-триггера в `remember_exchange`)
- Wiki/лор Арти ещё не реализован
- Entity graph: нет 2-hop traversal, нет semantic entity search, relations только для direct mentions
- Vector chunks: `limit=2` вместо `limit=1` для compact fallback
- Consolidation: нет conflict resolution, importance auto-adjust, hard-delete

---

## Критический путь (что нужно сделать в первую очередь)

1. **[ОБЯЗАТЕЛЬНО]** Прогнать `setup_database.py` + `backfill_memory_chunks.py` для старых сообщений
2. **[РЕКОМЕНДУЕТСЯ]** Протестировать profile/timeline через CLI dry-run перед apply
3. **[СЛЕДУЮЩИЙ ЭТАП]** Wiki / Lore Арти — `memory_wiki_pages` + manual moderation
4. **[ПОТОМ]** Усилить entity graph: `find_related()`, 2-hop traversal, graph reranking
5. **[ПОТОМ]** Дополнить consolidation: conflict resolution, importance auto-adjust
