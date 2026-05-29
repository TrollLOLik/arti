# Design Document

## Overview

`/vclone` (alias `/steal`) — admin-only команда клонирования голоса. Архитектурно повторяет паттерн `/dub`: пошаговый FSM-диалог в `config.dub_flow_state`-стиле, отдельная очередь с одиночным воркером, внешний пайплайн — это `gradio_client` к VoxCPM Demo Space с локальным фоллбэком.

Переиспользуем уже готовые куски:
- `ai/tts.py::_generate_voxcpm_demo` / `_generate_voxcpm_local` / `_generate_fish` — TTS-бэкенды (нужно расширить сигнатуру: добавить параметры `reference_path` и `prompt_text`, чтобы передавать пользовательский референс вместо встроенного `sample.wav`).
- `ai/tts.py::_wav_to_telegram_ogg` и `_split_body_sentences` — без изменений.
- `ai/video_url.py::download_audio_for_url`, `is_known_video_url`, `find_first_url` — для URL-источников.
- `ai/dubbing.py::VIDEOTRANS_PYTHON` — путь к Python из `videotrans/.venv` (там уже стоит `audio-separator[gpu]`).
- Шаблон FSM из `bot/commands.py::handle_dub_command` / `handle_dub_attachment` / `handle_dub_flow`.

Новый код концентрируется в одном модуле `ai/voice_clone.py` — все стадии пайплайна (extract → validate → cleanup → normalize → synthesize) как набор `async def` функций без классов. Управление состоянием — снаружи в `bot/commands.py` и `bot/handlers.py`.

## File Layout

- `ai/voice_clone.py` — **новый**: `extract_reference()`, `validate_reference()`, `run_separator()`, `normalize_text_via_llm()`, `synthesize_with_clone()`, `cleanup_vclone_files()`. Чистые функции пайплайна, всё, что не Telegram.
- `videotrans/_run_separator.py` — **новый** вспомогательный скрипт в директории videotrans (НЕ правка `main.py`). Принимает `--input` и `--output-dir`, импортирует `audio_separator.separator.Separator` с моделью `mel_band_roformer_karaoke_becruily.ckpt`, печатает путь к vocals-файлу в stdout последней строкой. Вызывается из `ai/voice_clone.py::run_separator` через `asyncio.create_subprocess_exec` с `videotrans/.venv/python`.
- `bot/commands.py` — **+ `handle_vclone_command`** (entry point, проверка прав, fast-path detection, init FSM). Регистрирует обработчик inline-кнопок `vclone_clean_callback`. Импорты `vclone_flow_state` из `config`.
- `bot/handlers.py` — две точки расширения: (1) в `handle_all_messages` ветка `if vclone_flow_state.get(chat_id, {}).get(user_id): handle_vclone_flow(update, context)` ровно по образцу dub-flow; (2) в обработчиках медиа (`handle_voice_message`, `handle_audio_message`, `handle_video_upload_message`, `handle_video_note`) — раннее перенаправление на `handle_vclone_attachment` при активном FSM.
- `bot/queue.py` — **+ `vclone_queue: asyncio.Queue`** и **+ `vclone_worker()`** (одиночный воркер, идентичен `dubbing_worker` по структуре). **+ `enqueue_vclone(task, bot, chat_id)`**. Воркер вызывает финальный шаг — синтез + отправка результата.
- `config.py` — **+ `vclone_flow_state = defaultdict(lambda: defaultdict(lambda: None))`** ровно по образцу `dub_flow_state`.
- `main.py` — **+ `CommandHandler("vclone", handle_vclone_command)`**, **+ alias `CommandHandler("steal", ...)`**, **+ `CallbackQueryHandler(vclone_clean_callback, pattern="^vclone_clean:")`**, **+ запуск `vclone_worker()` в `post_init`**.

## Architecture

```
/vclone  ─►  handle_vclone_command  ─►  [reply has media+text? ───► fast-path]
                                          │
                                          ▼
                              vclone_flow_state[chat][user] = {step: "reference", ...}
                                          │
                              ┌───────────┴────────────┐
                              ▼                        ▼
                  step="reference"            step="text"
              (handle_vclone_attachment    (handle_vclone_flow:
               or text URL via             save Synthesis_Text)
               handle_vclone_flow)
                              │                        │
                              ▼                        │
                    extract_reference                  │
                    validate_reference                 │
                              │                        │
                              ▼                        │
                  step="cleanup_choice"                │
              (inline keyboard:                        │
               vclone_clean:1 / :0)                    │
                              │                        │
                  ┌───────────┴────────────┐           │
                  ▼                        ▼           │
              run_separator           use original     │
              (videotrans/.venv      reference         │
               subprocess)                             │
                  │                        │           │
                  └───────────┬────────────┘           │
                              ▼                        │
                  Synthesis_Text есть? ◄───────────────┘
                              │
                       нет ──►step="text"
                       да ──► enqueue_vclone(...) ──► vclone_queue
                                                          │
                                                          ▼
                                                  vclone_worker:
                                                  normalize_text_via_llm
                                                  synthesize_with_clone
                                                  send_voice / send_audio
                                                  cleanup_vclone_files
```

## Components and Interfaces

### `ai/voice_clone.py`

```python
@dataclass
class VCloneJob:
    chat_id: int
    user_id: int
    user_name: str
    message_id: int
    reference_path: Path        # финальный референс (cleaned или original)
    synthesis_text: str
    source_kind: str            # "reply_voice" | "url" | "stepwise_video" | ...
    cleaned: bool               # для аудита

async def extract_reference(src: TelegramFile | str, work_dir: Path) -> Path:
    """ffmpeg в моно-WAV 24 kHz. Принимает либо Telegram File, либо URL."""

async def validate_reference(wav: Path) -> tuple[bool, str, Path]:
    """ffprobe длительность + ffmpeg volumedetect.
    Возвращает (ok, reason, final_path). Если 15 < dur <= 60 — обрезает до 15с,
    final_path указывает на новый файл. dur < 5 или > 60 — (False, reason, _).
    Тишина (mean_volume < -50dB) — (False, "silent", _)."""

async def run_separator(input_wav: Path, work_dir: Path, timeout: float = 120) -> Path | None:
    """Запускает videotrans/_run_separator.py через subprocess в videotrans/.venv.
    Возвращает Path к vocals.wav или None при ошибке (любое исключение поглощается,
    лог пишется на ERROR)."""

async def normalize_text_via_llm(raw_text: str) -> tuple[str, str]:
    """Возвращает (normalized_text, direction). На любой ошибке/невалидном JSON —
    (raw_text, '')."""

def sanitize_direction(direction: str) -> str:
    """Удаляет [...] и слова из _NATIVE_VOXCPM_TAGS (laughing/sigh/Question-*/...).
    Возвращает чистую строку или ''."""

async def synthesize_with_clone(
    reference: Path,
    text: str,
    direction: str,
    work_dir: Path,
) -> Path | None:
    """Демо → локальный VoxCPM → Fish, сегментация через _split_body_sentences,
    склейка через pydub. Возвращает Path к WAV или None."""

def cleanup_vclone_files(*paths: Path) -> None:
    """Best-effort удаление всех путей."""
```

### Расширение `ai/tts.py`

Добавляются необязательные параметры `reference_path: Path | None = None` и `prompt_text: str | None = None` в `_generate_voxcpm_demo`, `_generate_voxcpm_local`, `_generate_fish`. По умолчанию — старое поведение (REF_PATH/PROMPT_TEXT). Если `reference_path` передан — используется он. Если `prompt_text=""` — для Demo Space передаётся `use_prompt_text=False`.

Решение по `prompt_text`: для пользовательского референса передаём пустой `prompt_text` с `use_prompt_text=False`. Встроенный PROMPT_TEXT привязан к голосу `sample.wav`, на чужом голосе он деградирует качество.

### `videotrans/_run_separator.py`

```python
"""CLI helper: vocal separation via audio-separator. Запускается из ai/voice_clone.py."""
import argparse, sys
from pathlib import Path
from audio_separator.separator import Separator

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output-dir", required=True)
ap.add_argument("--model", default="mel_band_roformer_karaoke_becruily.ckpt")
args = ap.parse_args()

out_dir = Path(args.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
sep = Separator(output_dir=str(out_dir), output_format="WAV", use_autocast=True,
                mdxc_params={"segment_size": 256, "override_model_segment_size": True,
                             "batch_size": 1, "overlap": 8, "pitch_shift": 0})
sep.load_model(model_filename=args.model)
files = sep.separate(args.input)
# audio-separator печатает пути; нам нужен vocals — берём по подстроке
vocals = next((f for f in files if "vocal" in Path(f).stem.lower()
               and "no" not in Path(f).stem.lower()
               and "instrument" not in Path(f).stem.lower()), None)
if not vocals:
    print(f"ERROR: vocals not found in {files}", file=sys.stderr)
    sys.exit(2)
print(str(Path(vocals).resolve()))   # последняя строка stdout = путь
```

`run_separator` читает stdout, последняя непустая строка не начинается с `ERROR:` — путь к vocals.

### `bot/commands.py::handle_vclone_command`

Логика по шагам:
1. `is_admin` + RP-mode + `is_responses_enabled` — отказ в Bot_Persona_Reply (Requirement 1).
2. `args = update.message.text.split(maxsplit=1)[1:]` → `synthesis_text` (опционально).
3. Если есть `reply_to_message`:
   - reply содержит `voice/audio/video/video_note/document(audio|video)` → fast-path: качаем, валидируем, переходим в `step="cleanup_choice"`. Если `synthesis_text` пуст — он будет собран на следующем шаге.
   - reply содержит URL — `download_audio_for_url`, дальше то же самое.
   - reply без поддерживаемого медиа → отказ (Requirement 2.4 / 13.1).
4. Нет reply, нет args → `step="reference"`, prompt-сообщение в стиле Арти.
5. Нет reply, есть args → `step="reference"`, прячем `synthesis_text` в state, prompt про референс.

### `bot/commands.py::vclone_clean_callback`

Парсит `callback_data`, проверяет `(chat_id, user_id)` против state. На `:0` — продолжаем с `state["reference"]` без чистки. На `:1` — отвечаем «прогоняю через сепаратор», стартуем `record_voice` action, вызываем `run_separator`. По завершении — `final_reference = cleaned or original` (на ошибку Requirement 6.9 — fallback с предупреждением).

После чистки: если `synthesis_text` уже есть — сразу `enqueue_vclone`. Иначе `step="text"` + prompt.

## Data Models

### `vclone_flow_state[chat_id][user_id]` (in-memory dict)

| key | type | значение |
|---|---|---|
| `step` | str | `"reference"` / `"cleanup_choice"` / `"text"` / `"synthesis"` |
| `reference_path` | str | путь к WAV в `temp/` после extract+validate |
| `cleaned_path` | str \| None | путь к Cleaned_Reference_Audio (если выбрано `vclone_clean:1`) |
| `synthesis_text` | str \| None | заполняется в fast-path или после `step="text"` |
| `source_kind` | str | для аудита |
| `message_id` | int | id исходного `/vclone`-сообщения для reply_to |
| `created_at` | float | для FSM-таймаута 600с (Requirement 3.7) |
| `bot_message_id` | int \| None | id сообщения с inline-клавиатурой (для edit при действии) |

Никаких persistent storage. Cleanup при `/cancel`, при таймауте, при успехе и при ошибке — всегда удаляем `reference_path` и `cleaned_path`.

### Очередь

`vclone_queue: asyncio.Queue` — FIFO, один воркер `vclone_worker()`. Задача:

```python
{
    "chat_id": int, "user_id": int, "user_name": str, "message_id": int,
    "reference_path": str, "synthesis_text": str,
    "source_kind": str, "cleaned": bool,
    "context": ContextTypes.DEFAULT_TYPE,
    "started_at": float,    # для аудит-лога
}
```

## FSM States

| step | from | event | next | side effect |
|---|---|---|---|---|
| _(init)_ | `/vclone` без reply | command | `reference` | reply «жду сэмпл», state создан |
| _(init)_ | `/vclone` reply на медиа+есть text | command | `cleanup_choice` | extract → validate → show buttons; `synthesis_text` сохранён |
| _(init)_ | `/vclone` reply на медиа без text | command | `cleanup_choice` (через `text` потом) | extract → validate → show buttons |
| `reference` | media/url received | media handler / text URL | `cleanup_choice` | extract → validate → show buttons |
| `reference` | unsupported | text/photo/sticker | `reference` | reply про допустимые типы |
| `cleanup_choice` | `vclone_clean:1` callback | callback | `text` или `synthesis` | run_separator, finalize reference |
| `cleanup_choice` | `vclone_clean:0` callback | callback | `text` или `synthesis` | use original reference |
| `text` | text message | text | `synthesis` (через enqueue) | save `synthesis_text`, `enqueue_vclone` |
| `synthesis` | _(в очереди)_ | dequeue | _(done)_ | normalize_text_via_llm → synthesize → send → cleanup |

Переход `cleanup_choice → synthesis` (минуя `text`) выполняется когда `state["synthesis_text"]` уже непуст после fast-path. Иначе `cleanup_choice → text`.

`/cancel` обрабатывается уже существующим `handle_cancel_command` — добавляем туда ветку очистки `vclone_flow_state` по образцу `dub_flow_state`.

## Queue Strategy

**Решение: отдельная `vclone_queue` с собственным воркером, общий семафор с TTS-частью бота не нужен.**

Почему отдельная от `dubbing_queue`: `/dub` гоняет 5–15-минутные пайплайны с GPU, `/vclone` — 15–60 секунд. Деление на одной очереди приведёт к тому, что vclone будет ждать дубляж и наоборот. По стилю одиночный воркер (Requirement 9.2 — «не более одной задачи одновременно») — ровно как `dubbing_worker`.

**Demo Space rate-limit.** В `_generate_voxcpm_demo` уже есть retry с переподключением — этого достаточно для одиночного воркера. Дополнительный семафор не нужен: vclone_worker сам по себе сериализует. Если в будущем появится параллельный TTS из обычных ответов Арти + vclone одновременно — туда добавим `_demo_request_semaphore = asyncio.Semaphore(1)` в `ai/tts.py`. На текущем этапе — out of scope.

## LLM Normalizer JSON Contract

System prompt (русский, без лишнего, в характере):

```
Ты — нормализатор текста для синтеза речи. Получаешь сырой текст и возвращаешь СТРОГО JSON-объект:
{"text": "<нормализованный текст>", "control_instruction": "<описание тона на английском>"}

Правила:
- Числа прописью на языке текста ("в 2024 году" → "в две тысячи двадцать четвертом году").
- Аббревиатуры по произношению ("МГУ" → "эм гэ у", "ИИ" → "и и").
- НИКАКИХ квадратных или круглых скобок в "text".
- "control_instruction" — короткое описание манеры на английском, например "warm aristocratic tone, female voice", "cold neutral, male voice". 5–8 слов. БЕЗ слов laughing, sigh, Uhm, Shh, Question-ah, Surprise-wa и подобных VoxCPM-тегов.
- Никаких пояснений, преамбул, markdown-блоков. Только JSON в одну строку.
```

Пример ответа:
```json
{"text":"В две тысячи двадцать четвертом году эм гэ у выпустит обновлённую программу.","control_instruction":"neutral confident tone, female voice"}
```

Парсинг:
```python
try:
    obj = json.loads(raw)
    text = (obj.get("text") or "").strip() or raw_input
    direction = sanitize_direction(obj.get("control_instruction") or "")
except Exception:
    text, direction = raw_input, ""
```

`sanitize_direction` режет всё что в `[...]`/`(...)`, удаляет токены из `_NATIVE_VOXCPM_TAGS` (case-insensitive substring match — если LLM вписала `[laughing]` или просто слово `laughing` в описание тона). Если после очистки строка короче 3 символов — возвращаем `""`.

## Vocal Separator Decision

Запуск через **отдельный helper-скрипт** `videotrans/_run_separator.py`, не через инлайн `python -c`. Причина: модель и параметры Separator лучше читаемы как код, аргументы передаются через argparse (нет проблем с экранированием путей с пробелами/кириллицей на Windows), `videotrans/main.py` остаётся нетронутым.

Запуск из `ai/voice_clone.py::run_separator`:
```python
proc = await asyncio.create_subprocess_exec(
    str(VIDEOTRANS_PYTHON), str(VIDEOTRANS_DIR / "_run_separator.py"),
    "--input", str(input_wav.resolve()),
    "--output-dir", str(work_dir.resolve()),
    cwd=str(VIDEOTRANS_DIR),
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
)
```

Таймаут 120с (большинство 5–30-секундных рефов чистятся за 5–15 секунд, запас x4). На таймаут — `proc.kill()` + None.

## Output Delivery

Логика для `vclone_worker` после успешного `synthesize_with_clone`:
- `ffprobe` длительность WAV.
- ≤30с → `_wav_to_telegram_ogg` → `send_voice(reply_to_message_id=message_id, caption=<краткая реплика Арти>, parse_mode='HTML')`.
- >30с → конвертация в mp3 (libmp3lame через subprocess) → `send_audio(title="Vclone от Арти", performer=user_name, caption=...)`.
- Размер >50 МБ → `send_message` с путём на диске и НЕ удаляем файл (Requirement 10.4).

## Error Handling

| ситуация | действие |
|---|---|
| reply без медиа/URL | Bot_Persona_Reply «нужен голос», без FSM |
| Telegram-файл >20 МБ | отказ + просьба прислать ссылку, FSM очищен |
| ffmpeg non-zero | Bot_Persona_Reply, cleanup, аудит `result=ffmpeg_error` |
| dur < 5s | отказ «слишком короткий», cleanup, `result=refused_short_ref` |
| dur > 60s | отказ «слишком длинный», cleanup, `result=refused_long_ref` |
| только тишина (volumedetect) | отказ, cleanup, `result=refused_silent` |
| `download_audio_for_url` exception | reply с обрезанной строкой ≤200 символов |
| separator failure (любая) | продолжаем с original, реплика «чистка не удалась», `cleanup_error=<причина>` |
| Demo 429/SSL/timeout | 3 retry с переподключением (уже в `_generate_voxcpm_demo`), потом fallback |
| LLM_Normalizer падает | continue с raw text + пустой direction |
| все 3 TTS-бэкенда упали | reply «не получилось», cleanup, `result=backend_error` |
| FSM-таймаут 600с | scheduled task, cleanup всех файлов state, реплика о таймауте |

## Audit Log

Один логгер `vclone.audit` (стандартный `logging`, пишет в `logs/bot.log`). Две точки:

1. **Старт** (после успешного `extract_reference`):
   ```
   INFO vclone.audit start chat=<id> user=<id> name=<username> source=<reply_voice|url|...> ref_sha256=<first_32k_hex>
   ```
2. **Финал** (в worker'е после отправки или после ошибки):
   ```
   INFO vclone.audit done chat=<id> user=<id> text_len=<N> text_preview=<first_80> direction=<sanitized> cleaned=<bool> result=<ok|refused_*|backend_error|...> elapsed_sec=<float>
   ```

Полный `synthesis_text` НЕ пишется (Requirement 11.5).

## Testing Strategy

**PBT не применима** для этой фичи. `/vclone` — это workflow-orchestration вокруг внешних сервисов (Telegram bot API, ffmpeg subprocess, audio-separator subprocess, VoxCPM Demo Space, LLM, Fish Speech). Поведение на 95% состоит из побочных эффектов и сетевых вызовов; универсальных свойств вида «for all input X, P(X)» здесь нет. По рекомендации workflow-гайдлайна — для таких фич используются example-based unit + integration smoke (как сделано для `/dub`).

**Unit tests** (`tests/test_voice_clone.py`):
- `sanitize_direction` — конкретные примеры: `"warm aristocratic tone"` → unchanged; `"[laughing] cold tone"` → `"cold tone"`; `"laughing"` → `""`; `"(female voice) neutral"` → `"neutral"`. Это чистая функция, 6–8 примеров покрывают логику.
- `classify_vclone_media` (если выделим аналог `classify_dub_media`) — пары `(filename, mime) → kind`.
- `normalize_text_via_llm` JSON parsing — мокаем `generate_response_stream`, проверяем: валидный JSON → `(text, direction)`; `{"text": "x"}` без direction → `("x", "")`; невалидный JSON → `(raw_input, "")`; LLM вернула markdown-fenced JSON → парсится корректно (snippet-extraction).

**Integration smoke** (ручной запуск, не в CI):
- `tests/integration/test_vclone_smoke.py` — один прогон: голосовое 10с → fast-path с заранее заданным текстом → ассерты (1) `validate_reference` ok, (2) separator вернул не-пустой WAV, (3) `synthesize_with_clone` вернул WAV длиной >0 байт. Без сравнения семантики аудио.

**Не тестируем автоматически:**
- Качество клонирования (требует human evaluation).
- Telegram-рендер caption/inline-кнопок (визуальная проверка).
- Поведение Demo Space на 429/SSL (нестабильно, тестируется в проде через лог).

## Out of Scope

В этом релизе не делаем:
- Кэширование `Cleaned_Reference_Audio` между запросами (каждый `/vclone` — новая чистка).
- Параллельные запросы к Demo Space (один воркер строго последовательно).
- Многоязычная нормализация — LLM отвечает на языке исходного `synthesis_text`, без переключений.
- Web UI / inline-mode `/vclone` без чата.
- Хранилище аудио-результатов — всё удаляется после отправки.
- Поддержка `audio-separator` без `videotrans/.venv` (если venv нет — фича упадёт на cleanup-шаге; это покрывается Requirement 6.9 fallback'ом).
