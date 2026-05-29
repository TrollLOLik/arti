# Implementation Plan: voice-clone-vclone

## Overview

Реализация admin-only команды `/vclone` (alias `/steal`) — клонирование голоса по референсу с озвучкой произвольного текста. Архитектурно повторяет паттерн `/dub`: FSM в `vclone_flow_state`, отдельная очередь с одиночным воркером, переиспользование TTS-бэкендов из `ai/tts.py`.

Слоистая разбивка: helper script → core module skeleton → pure pipeline functions → TTS extension → queue/worker → Telegram handlers → wiring → audit/timeout → tests.

> **PBT not applicable per design.md::Testing Strategy** — `/vclone` это workflow-orchestration вокруг внешних сервисов, универсальных свойств нет. Используются только example-based unit-тесты (опциональные).

## Tasks

- [x] 1. Foundation — helper script, модуль-скелет, FSM-состояние

  - [x] 1.1 Создать helper-скрипт `videotrans/_run_separator.py` для запуска audio-separator
    - Создать новый файл `videotrans/_run_separator.py` с argparse (`--input`, `--output-dir`, `--model`).
    - Импорт `audio_separator.separator.Separator`, дефолтная модель `mel_band_roformer_karaoke_becruily.ckpt`, параметры segment_size=256, batch_size=1, overlap=8.
    - В stdout последней строкой печатать абсолютный путь к vocals-файлу; ошибки в stderr с префиксом `ERROR:` и `sys.exit(2)`.
    - Скрипт запускается через `videotrans/.venv/python` — не требует установки `audio-separator` в основной venv бота.
    - **Файлы**: `videotrans/_run_separator.py` (новый).
    - **Дизайн**: design.md::File Layout, design.md::Vocal Separator Decision.
    - _Requirements: 6.6, 6.7_

  - [x] 1.2 Создать модуль-скелет `ai/voice_clone.py` с сигнатурами и docstring'ами
    - Создать новый файл `ai/voice_clone.py`.
    - Объявить dataclass `VCloneJob` (chat_id, user_id, user_name, message_id, reference_path, synthesis_text, source_kind, cleaned).
    - Объявить пустые сигнатуры всех функций пайплайна с docstring'ами по контракту из design.md: `extract_reference`, `validate_reference`, `run_separator`, `normalize_text_via_llm`, `sanitize_direction`, `synthesize_with_clone`, `cleanup_vclone_files`.
    - Импортировать `VIDEOTRANS_PYTHON`, `VIDEOTRANS_DIR` из `ai.dubbing`; константы `_NATIVE_VOXCPM_TAGS`, `_split_body_sentences` из `ai.tts`.
    - Тела функций — `raise NotImplementedError` (заполняются в задачах 2.x и 3.2).
    - **Файлы**: `ai/voice_clone.py` (новый).
    - **Дизайн**: design.md::Components and Interfaces / `ai/voice_clone.py`.
    - _Requirements: 4.1-4.7, 5.1-5.6, 6.5-6.9, 7.1-7.8, 8.1-8.6, 11.1-11.2_

  - [x] 1.3 Добавить `vclone_flow_state` в `config.py`
    - Добавить строку `vclone_flow_state = defaultdict(lambda: defaultdict(lambda: None))` рядом с `dub_flow_state`.
    - Никаких persistent storage. In-memory dict со схемой ключей из design.md::Data Models.
    - **Файлы**: `config.py` (правка).
    - **Дизайн**: design.md::File Layout, design.md::Data Models / `vclone_flow_state[chat_id][user_id]`.
    - _Requirements: 3.1, 3.6, 3.7_

- [x] 2. Core pipeline functions — заполнение `ai/voice_clone.py`

  - [x] 2.1 Реализовать `extract_reference()` — ffmpeg → mono WAV 24 kHz
    - После 1.2. Принимает либо Telegram `File` (через `bot.get_file` + `download_to_drive`), либо URL-строку (через `ai.video_url.download_audio_for_url`).
    - Конвертация в моно WAV 24 kHz через subprocess ffmpeg (`-ac 1 -ar 24000`).
    - Сохранение в `temp/vclone_ref_<run_id>.wav`, возврат `Path`.
    - На ненулевой код выхода ffmpeg — `raise RuntimeError(stderr_tail)`.
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `extract_reference`.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 4.7_

  - [x] 2.2 Реализовать `validate_reference()` — duration check + trim + silence detect
    - После 2.1. Использовать `ffprobe` для измерения длительности (через subprocess или pydub).
    - dur < 5 → `(False, "refused_short_ref", path)`; dur > 60 → `(False, "refused_long_ref", path)`.
    - 15 < dur ≤ 60 → trim до 15с через `ffmpeg -t 15`, новый путь `temp/vclone_ref_<run_id>_trim.wav`.
    - Силенс-детект через `ffmpeg -af volumedetect`: парсить stderr на `mean_volume`, если < -50dB → `(False, "refused_silent", path)`.
    - На успех: `(True, "ok", final_path)`.
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `validate_reference`, design.md::Error Handling.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.3_

  - [x] 2.3 Реализовать `run_separator()` — subprocess-обёртка над videotrans/_run_separator.py
    - После 2.2 и 1.1. Использует `asyncio.create_subprocess_exec` с `VIDEOTRANS_PYTHON` и cwd=`VIDEOTRANS_DIR`.
    - Аргументы: `_run_separator.py --input <abs> --output-dir <work_dir>`.
    - Таймаут 120с (`asyncio.wait_for`); на превышение — `proc.kill()`, return None.
    - Парсить stdout: последняя непустая строка не начинающаяся с `ERROR:` — путь к vocals.
    - Любое исключение поглощается, лог на ERROR, return None (Requirement 6.9 fallback).
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `run_separator`, design.md::Vocal Separator Decision.
    - _Requirements: 6.5, 6.6, 6.7, 6.8, 6.9, 6.11_

  - [x] 2.4 Реализовать `normalize_text_via_llm()` + `sanitize_direction()` — LLM нормализатор
    - После 1.2. `sanitize_direction()` — чистая функция: вырезает `[...]`/`(...)`, удаляет токены из `_NATIVE_VOXCPM_TAGS` (case-insensitive substring), возвращает `""` если результат < 3 символов.
    - `normalize_text_via_llm()` вызывает `ai.generation.generate_response_stream` с системным промптом из design.md::LLM Normalizer JSON Contract (модель `gemini-3.1-flash-lite-preview`, temp=0.3).
    - Парсинг: `json.loads(raw)`; на любое исключение или отсутствие полей — `(raw_text, "")`.
    - Применить `sanitize_direction` к `obj["control_instruction"]` перед возвратом.
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces, design.md::LLM Normalizer JSON Contract.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 3. TTS reuse — расширение `ai/tts.py` и `synthesize_with_clone`

  - [x] 3.1 Расширить сигнатуры `_generate_voxcpm_demo/local/fish` параметрами `reference_path` + `prompt_text`
    - В `ai/tts.py`: добавить kwargs `reference_path: Path | None = None`, `prompt_text: str | None = None` к трём приватным функциям.
    - Если `reference_path is None` — fallback на модульный `REF_PATH` (старое поведение, обратно-совместимо).
    - Если `prompt_text is None` — fallback на модульный `PROMPT_TEXT`.
    - В `_generate_voxcpm_demo`: если `prompt_text == ""` → `use_prompt_text=False`; иначе `use_prompt_text=True`.
    - Обновить логи, чтобы отражать какой референс используется.
    - **Файлы**: `ai/tts.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `Расширение ai/tts.py`.
    - _Requirements: 8.1, 8.2, 8.3, 8.6_

  - [x] 3.2 Реализовать `synthesize_with_clone()` — публичная функция в `voice_clone.py`
    - После 3.1 и 2.4. Использует `_split_body_sentences` (импорт из `ai.tts`) для разбивки текста длиной > 200 символов на предложения.
    - Для каждого сегмента: вызвать `_generate_voxcpm_demo(direction, body, wav_path, reference_path=ref, prompt_text="")`.
    - На False → fallback `_generate_voxcpm_local(f"({direction}) {body}", wav_path, reference_path=ref, prompt_text="")`.
    - На False → fallback `_generate_fish(body, wav_path, reference_path=ref, prompt_text="")` (без direction, без `[native_tag]`).
    - Склейка через pydub (100ms gap), как в `text_to_speech_telegram`.
    - Возврат `Path` к финальному WAV или None при провале всех бэкендов.
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `synthesize_with_clone`, design.md::Architecture.
    - _Requirements: 7.7, 7.8, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 3.3 Реализовать `cleanup_vclone_files()` — best-effort удаление временных файлов
    - После 1.2. Функция принимает `*paths: Path | str`, проходит по списку, удаляет существующие файлы.
    - Каждое удаление в `try/except Exception` — best-effort, не падает.
    - **Файлы**: `ai/voice_clone.py` (правка).
    - **Дизайн**: design.md::Components and Interfaces / `cleanup_vclone_files`.
    - _Requirements: 11.1, 11.2_

- [x] 4. Checkpoint — Ensure foundation works
  - Ensure all tests pass, ask the user if questions arise.
  - Проверить: модуль `ai/voice_clone.py` импортируется без ошибок; `_run_separator.py` запускается с `--help`; `vclone_flow_state` доступен из `config`.

- [x] 5. Queue and worker — `bot/queue.py`

  - [x] 5.1 Добавить `vclone_queue` и `vclone_worker()` в `bot/queue.py`
    - После 3.2. Объявить `vclone_queue: asyncio.Queue` рядом с `dubbing_queue`.
    - Реализовать `vclone_worker()` по образцу `dubbing_worker`: одиночный, FIFO. Внутри:
      1. `await vclone_queue.get()` → задача (схема из design.md::Data Models / Очередь).
      2. Запустить chat action `record_voice` (повторение через `repeat_chat_action`).
      3. `normalize_text, direction = await normalize_text_via_llm(synthesis_text)`.
      4. `result_wav = await synthesize_with_clone(reference_path, normalize_text, direction, work_dir)`.
      5. ffprobe длительности → `_wav_to_telegram_ogg` + `send_voice` (если ≤30с) или конвертация в mp3 + `send_audio` (если >30с) или `send_message` с путём (если >50МБ).
      6. Caption в стиле Bot_Persona_Reply, `parse_mode='HTML'`.
      7. `cleanup_vclone_files(reference_path, cleaned_path, result_wav)`.
      8. Audit log при ошибке/успехе (см. задачу 9.1).
    - **Файлы**: `bot/queue.py` (правка).
    - **Дизайн**: design.md::File Layout, design.md::Architecture, design.md::Output Delivery.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.1, 10.2, 10.3, 10.4, 11.1, 11.2_

  - [x] 5.2 Добавить `enqueue_vclone(task, bot, chat_id)`
    - После 5.1. По образцу `enqueue_dubbing`: `vclone_queue.put(task)`, сообщение о позиции в очереди.
    - Если `qsize() > 5` — добавить предупреждение о времени ожидания (Requirement 9.5).
    - **Файлы**: `bot/queue.py` (правка).
    - **Дизайн**: design.md::Queue Strategy.
    - _Requirements: 9.1, 9.5_

- [x] 6. Telegram integration — `bot/commands.py`

  - [x] 6.1 Реализовать `handle_vclone_command` — entry point с admin-проверкой и fast-path
    - После 5.2. Проверки: `is_admin`, `is_responses_enabled`, `not rp_mode_state.get(chat_id)` — на отказе Bot_Persona_Reply.
    - Парсинг args: `update.message.text.split(maxsplit=1)[1:]` → опциональный `synthesis_text`.
    - Если `reply_to_message` есть и содержит `voice/audio/video/video_note/document(audio|video)`:
      - Скачать через `bot.get_file` + проверка `file_size <= 20 МБ`, иначе отказ с просьбой ссылки.
      - Вызвать `extract_reference` + `validate_reference`.
      - Установить FSM `step="cleanup_choice"`, отправить inline-клавиатуру с кнопками `[✨ Очистить звук]` (`vclone_clean:1`) и `[⏩ Оставить как есть]` (`vclone_clean:0`).
      - Если `synthesis_text` есть — сохранить в state.
    - Если `reply_to_message` содержит только URL (`is_known_video_url`) — `download_audio_for_url` + та же ветка.
    - Если reply без поддерживаемого медиа — Bot_Persona_Reply «нужен голос», без FSM.
    - Если нет reply — `step="reference"`, prompt-сообщение.
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: design.md::`bot/commands.py::handle_vclone_command`, design.md::FSM States.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 4.5, 13.1_

  - [x] 6.2 Реализовать `classify_vclone_media()` + `handle_vclone_attachment()` для медиа-роутинга в активном FSM
    - После 6.1. `classify_vclone_media(file_name, mime_type) -> Literal["video","audio",None]` — чистая функция (по аналогии с `classify_dub_media`).
    - `handle_vclone_attachment(update, context, file_id, file_size, file_name=None)` — общий обработчик для voice/audio/video/video_note/document:
      - Проверка `file_size <= 20 МБ`.
      - `extract_reference` + `validate_reference`.
      - Перевод FSM в `step="cleanup_choice"`, отправка inline-клавиатуры.
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: design.md::Architecture / FSM transitions.
    - _Requirements: 3.2, 3.5, 4.1, 4.2, 4.3, 4.5, 4.6, 4.7_

  - [x] 6.3 Реализовать `handle_vclone_flow()` для текстовых сообщений в FSM
    - После 6.2. По образцу `handle_dub_flow`. Возвращает `bool` (поглощено ли сообщение).
    - Если `step="reference"`:
      - Текст начинается с `http://`/`https://` → `download_audio_for_url` + `extract_reference` + `validate_reference` → `step="cleanup_choice"`.
      - Иначе — Bot_Persona_Reply про допустимые типы, остаёмся в `step="reference"`.
    - Если `step="text"`:
      - Сохранить `synthesis_text` в state, вызвать `enqueue_vclone(task)`, очистить FSM.
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: design.md::Architecture, design.md::FSM States.
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 13.2_

  - [x] 6.4 Реализовать `vclone_clean_callback()` — обработчик inline-кнопок `vclone_clean:0/1`
    - После 6.2 и 2.3. Парсит `callback_data`, проверяет `(chat_id, user_id)` против state.
    - На `vclone_clean:0` (skip): использовать `state["reference_path"]` как финальный референс, `cleaned=False`.
    - На `vclone_clean:1` (clean):
      - `query.answer()`, edit message с репликой «Прогоняю через сепаратор…».
      - Запустить `record_voice` chat action.
      - `cleaned_path = await run_separator(reference_path, work_dir)`.
      - На None — Bot_Persona_Reply «чистка не удалась, продолжаю», использовать original (Requirement 6.9 fallback), `cleaned=False`, `cleanup_error="..."`.
      - На успех — `cleaned=True`, финальный референс = cleaned_path.
    - После выбора:
      - Если `state["synthesis_text"]` есть → `enqueue_vclone`, очистить FSM.
      - Иначе → `step="text"`, prompt «Что озвучивать?».
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: design.md::`bot/commands.py::vclone_clean_callback`, design.md::Architecture.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.8, 6.9, 6.11, 6.12_

  - [x] 6.5 Подключить vclone-flow в `bot/handlers.py` — раннее перенаправление медиа и текста
    - После 6.3. В `handle_all_messages` добавить ветку (после `dub_flow_state` проверки):
      ```python
      from config import vclone_flow_state
      if vclone_flow_state.get(chat_id, {}).get(user_id):
          handled = await handle_vclone_flow(update, context)
          if handled: return
      ```
    - В обработчиках медиа (`handle_voice_message`, `handle_audio_message`, обработчик `video`/`video_note` внутри `handle_image_message` или отдельный handler — найти все 4 точки): добавить ранний роутинг на `handle_vclone_attachment` при активном `vclone_flow_state[chat_id][user_id]` со `step="reference"`.
    - Обновить импорты в `bot/handlers.py`: `handle_vclone_flow, handle_vclone_attachment, classify_vclone_media`.
    - **Файлы**: `bot/handlers.py` (правка).
    - **Дизайн**: design.md::File Layout, design.md::Architecture.
    - _Requirements: 3.2, 3.5_

- [x] 7. Wiring — регистрация handlers и worker'а в `main.py`

  - [x] 7.1 Зарегистрировать CommandHandlers и CallbackQueryHandler в `main.py`
    - После 6.1, 6.4. Добавить:
      - `application.add_handler(CommandHandler("vclone", handle_vclone_command))`
      - `application.add_handler(CommandHandler("steal", handle_vclone_command))` (alias)
      - `application.add_handler(CallbackQueryHandler(vclone_clean_callback, pattern="^vclone_clean:"))`
    - Обновить импорты в `main.py`: добавить `handle_vclone_command, vclone_clean_callback` из `bot.commands`.
    - **Файлы**: `main.py` (правка).
    - **Дизайн**: design.md::File Layout / `main.py`.
    - _Requirements: 14.1, 14.2_

  - [x] 7.2 Запустить `vclone_worker()` в `post_init`
    - После 5.1. В `post_init` рядом с `app.create_task(dubbing_worker())` добавить `app.create_task(vclone_worker())`.
    - Обновить импорт: `from bot.queue import generation_worker, dubbing_worker, vclone_worker`.
    - Логировать `"Воркер vclone запущен."`.
    - **Файлы**: `main.py` (правка).
    - **Дизайн**: design.md::File Layout.
    - _Requirements: 9.2_

  - [x] 7.3 Расширить `handle_cancel_command` — очистка `vclone_flow_state` и временных файлов
    - В `bot/commands.py::handle_cancel_command` добавить ветку по образцу `dub_flow_state`:
      - `from config import vclone_flow_state`.
      - При наличии state — `cleanup_vclone_files(state["reference_path"], state.get("cleaned_path"))`.
      - `vclone_flow_state[chat_id].pop(user_id, None)`.
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: design.md::Data Models / Cleanup.
    - _Requirements: 3.6, 11.2_

  - [x] 7.4 Обновить текст `arti_commands` — добавить `/vclone` в список
    - В `bot/commands.py::arti_commands` в раздел «Генерация медиа» добавить строку:
      `• /vclone — Клонировать голос по сэмплу и озвучить текст (admin)`.
    - **Файлы**: `bot/commands.py` (правка).
    - **Дизайн**: requirements.md::Requirement 14.3.
    - _Requirements: 14.3, 14.4_

- [x] 8. Checkpoint — End-to-end smoke
  - Ensure all tests pass, ask the user if questions arise.
  - Ручная проверка: `/vclone <текст>` reply на голосовое сообщение → cleanup choice buttons → синтез → `send_voice`. Проверить успех на обеих ветках (`:0` и `:1`).

- [x] 9. Audit logging и FSM-таймаут

  - [x] 9.1 Добавить аудит-логгер `vclone.audit` с двумя точками
    - После 5.1. В `bot/queue.py::vclone_worker` (и в `bot/commands.py` после `extract_reference`):
      - **Старт** (после успешного `extract_reference`, в `handle_vclone_command`/`handle_vclone_attachment`/callback): `logger_audit.info("start chat=%s user=%s name=%s source=%s ref_sha256=%s", ...)`. Хеш — sha256 первых 32 КБ Reference_Audio.
      - **Финал** (в `vclone_worker` после отправки/ошибки): `logger_audit.info("done chat=%s user=%s text_len=%d text_preview=%s direction=%s cleaned=%s result=%s elapsed_sec=%.2f", ...)`. `text_preview` — первые 80 символов.
    - Логгер: `logger_audit = logging.getLogger("vclone.audit")` — пишет в `logs/bot.log` через корневой handler.
    - **Файлы**: `bot/queue.py` (правка), `bot/commands.py` (правка).
    - **Дизайн**: design.md::Audit Log.
    - _Requirements: 11.3, 11.4, 11.5_

  - [x] 9.2 FSM-таймаут watchdog (фоновая задача, опциональное улучшение)
    - **Optional** — без этого фича работает, но stale state может накапливаться. Добавляется в next iteration если будет проблема.
    - В `main.py::post_init` запустить `app.create_task(vclone_fsm_timeout_watchdog())`.
    - Watchdog в `bot/queue.py` (или новом `utils/vclone_watchdog.py`): каждые 60с сканирует `vclone_flow_state`, удаляет записи где `created_at` старше 600с, чистит файлы через `cleanup_vclone_files`, отправляет Bot_Persona_Reply о таймауте.
    - **Файлы**: `bot/queue.py` или `utils/vclone_watchdog.py` (новый), `main.py` (правка).
    - **Дизайн**: design.md::FSM States / общий FSM-таймаут, design.md::Error Handling.
    - _Requirements: 3.7, 6.10_

- [ ] 10. Tests — example-based unit tests (PBT not applicable per design.md::Testing Strategy)

  > Эти задачи опциональны: помогают при будущих изменениях, но не блокируют запуск фичи. PBT не применима — фича workflow-orchestration с побочными эффектами.

  - [ ]* 10.1 Unit-тесты для `sanitize_direction` (чистая функция)
    - Создать `tests/test_voice_clone.py`. Покрыть кейсы:
      - `"warm aristocratic tone"` → unchanged.
      - `"[laughing] cold tone"` → `"cold tone"`.
      - `"laughing"` → `""` (нативный тег целиком).
      - `"(female voice) neutral"` → `"neutral"`.
      - `""` → `""`.
      - `"ab"` → `""` (короче 3 символов).
      - `"[Question-ah] warm"` → `"warm"`.
    - Использовать pytest (он уже есть как dev-зависимость? — иначе unittest).
    - **Файлы**: `tests/test_voice_clone.py` (новый).
    - **Дизайн**: design.md::Testing Strategy / Unit tests.
    - _Requirements: 7.5_

  - [ ]* 10.2 Unit-тесты для `classify_vclone_media`
    - В `tests/test_voice_clone.py`. Пары `(filename, mime) → kind`:
      - `("song.mp3", "audio/mpeg")` → `"audio"`.
      - `("clip.mp4", "video/mp4")` → `"video"`.
      - `(None, "audio/ogg")` → `"audio"`.
      - `("doc.pdf", "application/pdf")` → `None`.
      - `(None, None)` → `None`.
      - `("voice.opus", None)` → `"audio"` (по расширению).
    - **Файлы**: `tests/test_voice_clone.py` (правка).
    - **Дизайн**: design.md::Testing Strategy.
    - _Requirements: 3.2, 4.1, 4.2, 4.3_

  - [ ]* 10.3 Unit-тесты для `normalize_text_via_llm` (с моком LLM)
    - В `tests/test_voice_clone.py`. Использовать `unittest.mock.patch` для `ai.voice_clone.generate_response_stream`.
    - Кейсы:
      - Валидный JSON → `(normalized_text, direction)`.
      - JSON без `control_instruction` → `(text, "")`.
      - Невалидный JSON / plain text → `(raw_input, "")`.
      - JSON в markdown-fence (```` ```json ... ``` ````) → парсится корректно (snippet-extraction).
      - LLM возвращает direction с `[laughing]` → после `sanitize_direction` пустая строка.
    - **Файлы**: `tests/test_voice_clone.py` (правка).
    - **Дизайн**: design.md::Testing Strategy / Unit tests, design.md::LLM Normalizer JSON Contract.
    - _Requirements: 7.1, 7.6, 13.5_

- [x] 11. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Финальная проверка: все required tasks (1-9) выполнены, импорты разрешаются, бот запускается, `/vclone` доступен через автодополнение.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP (FSM timeout watchdog, unit tests).
- Each task references specific requirements from `requirements.md` and concrete sections from `design.md` for traceability.
- Checkpoints (4, 8, 11) ensure incremental validation.
- Property tests **not applicable** per design.md::Testing Strategy — `/vclone` is workflow-orchestration with side effects; example-based unit tests are used instead.
- Dependencies are explicit: «После N.M» — caller task must be complete before dependent task starts.
- All temp files use `temp/vclone_*.wav` prefix for easy cleanup tracking.
