# Requirements Document

## Introduction

Команда `/vclone` (Voice Cloning, «Вор голосов») добавляет в Telegram-бота Арти возможность клонировать голос по короткому референсу и озвучивать произвольный текст этим голосом. Пользователь либо отвечает (`reply`) на чужое голосовое/видео/аудио сообщение, либо запускает пошаговый диалог и присылает референс отдельно. Бот извлекает аудио, валидирует длительность, нормализует текст через LLM, синтезирует речь через VoxCPM Demo Space (с фоллбэком на локальный VoxCPM и Fish Speech) и возвращает результат как голосовое сообщение или аудиофайл.

Команда разделяет инфраструктуру с уже существующей `/dub` (admin-only, FSM-паттерн `dub_flow_state`, `dubbing_queue` с retry, `gradio_client`-обёртка `_generate_voxcpm_demo`, `download_audio_for_url`) и подчиняется тем же правилам этики и логирования.

## Glossary

- **VClone_Command**: Telegram-команда `/vclone` (с alias `/steal`), главная точка входа фичи.
- **VClone_FSM**: конечный автомат пошагового диалога команды; ключи состояния — `(chat_id, user_id)`. По аналогии с `dub_flow_state` хранится в `vclone_flow_state` в `config.py`.
- **Reference_Audio**: пользовательский голосовой сэмпл, по которому клонируется тембр. Источник — Telegram-медиа (`voice`, `audio`, `video`, `video_note`) или прямая ссылка.
- **Synthesis_Text**: целевой текст, который должен быть произнесён клонированным голосом.
- **Normalized_Text**: результат пропуска `Synthesis_Text` через **LLM_Normalizer**.
- **Direction_Instruction**: строка `control_instruction` для VoxCPM Demo (например, `"warm aristocratic tone"`), описывающая интонацию/манеру.
- **LLM_Normalizer**: вызов генеративной модели (через `ai.generation.generate_response_stream` с `custom_system_prompt`), который превращает сырой текст в JSON `{"text": Normalized_Text, "control_instruction": Direction_Instruction}`.
- **Audio_Extractor**: ffmpeg-обёртка, извлекающая моно-WAV (16/24 kHz) из любого исходного контейнера.
- **Reference_Validator**: проверка длительности референса; допустимый диапазон — 5–30 секунд, при длине > 15 секунд выполняется обрезка до первых 15 секунд.
- **Reference_Cleanup_Choice**: этап FSM (`step="cleanup_choice"`) между Reference_Validator и LLM_Normalizer/VClone_Engine, на котором пользователь inline-кнопкой выбирает «очистить звук» или «оставить как есть».
- **Vocal_Separator**: subprocess-обёртка над audio-separator-моделью `mel_band_roformer_karaoke_becruily.ckpt` (та же модель, что используется в `videotrans/main.py::separate_audio_roformer`). Запускается из Python в `videotrans/.venv`, принимает Reference_Audio, возвращает Cleaned_Reference_Audio. Имя файла модели зафиксировано пользователем.
- **Cleaned_Reference_Audio**: моно-WAV, полученный пропуском Reference_Audio через Vocal_Separator; содержит только вокальную дорожку без фоновой музыки/шумов/чужих голосов. Сохраняется в `temp/`.
- **VClone_Engine**: TTS-бэкенд, по приоритету: VoxCPM Demo Space → локальный VoxCPM → Fish Speech. Переиспользует `ai/tts.py::_generate_voxcpm_demo` и `_generate_voxcpm_local`. На вход принимает Cleaned_Reference_Audio (если пользователь выбрал «очистить» и Vocal_Separator отработал) либо исходное Reference_Audio.
- **VClone_Queue**: `asyncio.Queue` с одиночным воркером, последовательно обрабатывающая задачи клонирования. Может быть отдельной очередью `vclone_queue` или переиспользовать `dubbing_queue` — выбор фиксируется на этапе дизайна, но требование «не более одного активного запроса к VoxCPM Demo Space одновременно» обязательно.
- **VClone_Audit_Log**: запись в логи о каждом запросе клонирования. Поля: `chat_id`, `user_id`, `username`, `источник_референса`, `длина_референса_сек`, `длина_текста_симв`, `direction`, `результат`.
- **Admin_User**: пользователь из `PRIVILEGED_USER_IDS` (см. `config.py`); проверяется через `utils.admin.is_admin`.
- **Bot_Persona_Reply**: ответ Арти, сформированный по правилам `arti_card.md` (HTML-теги `<i>`, `<blockquote>`, `<b>`; короткие реплики; «аристократический» тон).

## Requirements

### Requirement 1: Admin-only entry point

**User Story:** Как админ Арти, я хочу, чтобы команда `/vclone` запускалась только привилегированными пользователями, чтобы исключить злоупотребление voice cloning со стороны случайных участников чата.

#### Acceptance Criteria

1. WHEN пользователь отправляет `/vclone` или `/steal`, THE VClone_Command SHALL вызвать `utils.admin.is_admin` для определения привилегий.
2. IF вызывающий пользователь не является Admin_User, THEN THE VClone_Command SHALL отправить ответ с отказом в стиле Bot_Persona_Reply и НЕ переходить к шагу сбора референса.
3. WHILE чат находится в RP-режиме (`rp_mode_state[chat_id] == True`), THE VClone_Command SHALL отказать в выполнении сообщением Bot_Persona_Reply и НЕ создавать запись в VClone_FSM.
4. WHEN бот отключён в чате (`is_responses_enabled(chat_id) == False`), THE VClone_Command SHALL завершиться без ответа пользователю.

### Requirement 2: Fast-path via reply

**User Story:** Как админ, я хочу одним сообщением `/vclone <текст>` в reply на чужое голосовое/видео/аудио клонировать его голос, чтобы это работало в один шаг.

#### Acceptance Criteria

1. WHEN админ отправляет `/vclone <Synthesis_Text>` reply-ом на сообщение, содержащее `voice`, `audio`, `video` или `video_note`, THE VClone_Command SHALL извлечь медиа-источник из `update.message.reply_to_message`, скачать файл и использовать его как Reference_Audio.
2. WHEN админ отправляет `/vclone <Synthesis_Text>` reply-ом на сообщение, содержащее URL поддерживаемого видеохостинга (см. `ai/video_url.py::is_known_video_url`) или прямую ссылку на mp3/mp4, THE VClone_Command SHALL вызвать `download_audio_for_url` и использовать результат как Reference_Audio.
3. WHEN reply-сообщение содержит и медиа-источник, и `<Synthesis_Text>` после команды, THE VClone_Command SHALL пропустить шаги «сбор референса» и «сбор текста» и сразу перейти к Reference_Validator.
4. IF reply-сообщение не содержит ни медиа, ни поддерживаемого URL, THEN THE VClone_Command SHALL ответить Bot_Persona_Reply с пояснением, что нужен голосовой источник, и НЕ переходить в режим ожидания.

### Requirement 3: Stepwise FSM dialog

**User Story:** Как админ, я хочу запускать `/vclone` без аргументов и присылать референс и текст отдельными сообщениями, чтобы воспользоваться командой даже когда reply невозможен.

#### Acceptance Criteria

1. WHEN админ отправляет `/vclone` без аргументов и без reply, THE VClone_Command SHALL создать запись в VClone_FSM со `step="reference"` и отправить Bot_Persona_Reply, приглашающий прислать голосовое, аудиофайл, видео или ссылку.
2. WHILE VClone_FSM находится в `step="reference"`, THE VClone_FSM SHALL принимать одно из: `voice`, `audio`, `video`, `video_note`, `document` с MIME `audio/*` или `video/*`, либо текст, начинающийся с `http://`/`https://`.
3. WHEN Reference_Audio успешно получено и Synthesis_Text ещё не был передан, THE VClone_FSM SHALL перейти в `step="text"` и отправить Bot_Persona_Reply, запрашивающий текст для озвучки.
4. WHILE VClone_FSM находится в `step="text"`, THE VClone_FSM SHALL принять следующее текстовое сообщение пользователя как Synthesis_Text и перейти в `step="synthesis"`.
5. IF в `step="reference"` пришло сообщение неподдерживаемого типа (фото, стикер, текст без URL), THEN THE VClone_FSM SHALL ответить Bot_Persona_Reply с пояснением допустимых типов и остаться в `step="reference"`.
6. WHEN пользователь отправляет `/cancel` в любом активном состоянии VClone_FSM, THE VClone_FSM SHALL удалить временные файлы Reference_Audio из `temp/`, очистить запись и подтвердить отмену в стиле Bot_Persona_Reply.
7. IF VClone_FSM не получает следующее сообщение в течение 600 секунд после последнего перехода, THEN THE VClone_FSM SHALL очистить состояние, удалить временные файлы и отправить Bot_Persona_Reply о таймауте.

### Requirement 4: Reference acquisition from Telegram media

**User Story:** Как админ, я хочу присылать любой стандартный тип Telegram-аудио или видео и получать корректное извлечение голосовой дорожки, чтобы не думать о форматах.

#### Acceptance Criteria

1. WHEN источник Reference_Audio имеет тип `voice` (Opus), THE Audio_Extractor SHALL сконвертировать файл в моно-WAV 24 kHz и сохранить в `temp/`.
2. WHEN источник Reference_Audio имеет тип `audio` (произвольный аудиофайл), THE Audio_Extractor SHALL сконвертировать файл в моно-WAV 24 kHz и сохранить в `temp/`.
3. WHEN источник Reference_Audio имеет тип `video` или `video_note`, THE Audio_Extractor SHALL извлечь аудиодорожку и сохранить как моно-WAV 24 kHz в `temp/`.
4. WHEN Reference_Audio получено по URL через `download_audio_for_url`, THE Audio_Extractor SHALL сконвертировать результат `yt-dlp` в моно-WAV 24 kHz и сохранить в `temp/`.
5. IF размер исходного Telegram-файла превышает 20 МБ (`tg_file.file_size > 20*1024*1024`), THEN THE VClone_FSM SHALL отказать в скачивании, отправить Bot_Persona_Reply с просьбой прислать ссылку и НЕ запускать пайплайн.
6. WHEN Audio_Extractor завершает работу, THE VClone_FSM SHALL сохранить путь к WAV-файлу в `temp/` для последующих шагов.
7. IF ffmpeg возвращает ненулевой код выхода при конвертации, THEN THE VClone_FSM SHALL отправить Bot_Persona_Reply с описанием ошибки, удалить промежуточные файлы и завершить запрос.

### Requirement 5: Reference validation and trimming

**User Story:** Как админ, я хочу, чтобы бот сам проверял длину референса и обрезал слишком длинные сэмплы, чтобы не получать упавший VoxCPM из-за неподходящего входа.

#### Acceptance Criteria

1. WHEN Reference_Audio сконвертировано в WAV, THE Reference_Validator SHALL измерить длительность через ffprobe.
2. IF длительность Reference_Audio меньше 5 секунд, THEN THE Reference_Validator SHALL отказать в синтезе, отправить Bot_Persona_Reply с указанием минимальной длины и удалить временные файлы.
3. IF длительность Reference_Audio больше 15 секунд, THEN THE Reference_Validator SHALL обрезать файл до первых 15 секунд через ffmpeg и продолжить пайплайн.
4. IF длительность Reference_Audio находится между 30 и 60 секундами и обрезка до 15 секунд уже выполнена, THEN THE Reference_Validator SHALL продолжить с обрезанной версией без дополнительного предупреждения.
5. IF длительность Reference_Audio больше 60 секунд, THEN THE Reference_Validator SHALL отказать в синтезе с Bot_Persona_Reply (сэмпл слишком длинный, бот не доверяет такому источнику) и удалить временные файлы.
6. WHEN Reference_Validator успешно завершает проверку, THE Reference_Validator SHALL передать путь финального WAV в Reference_Cleanup_Choice (см. Requirement 6).

### Requirement 6: Reference cleanup choice

**User Story:** Как админ, я хочу после извлечения и валидации референса нажать одну из двух кнопок — «очистить звук» (отделить вокал от фона) или «оставить как есть» — чтобы избежать ситуации, когда VoxCPM клонирует не только голос, но и фоновую музыку, аплодисменты или чужие голоса из YouTube/TikTok-сэмпла.

#### Acceptance Criteria

1. WHEN Reference_Validator успешно завершает проверку (см. Requirement 5.6), THE VClone_FSM SHALL перейти в `step="cleanup_choice"` и отправить Bot_Persona_Reply с inline-клавиатурой `InlineKeyboardMarkup`, содержащей ровно две кнопки: `[✨ Очистить звук]` с `callback_data="vclone_clean:1"` и `[⏩ Оставить как есть]` с `callback_data="vclone_clean:0"`.
2. THE VClone_FSM SHALL показывать Reference_Cleanup_Choice одинаково в fast-path (Сценарий А, `/vclone <текст>` reply на медиа) и в stepwise-сценарии — даже если Synthesis_Text уже получен.
3. WHILE VClone_FSM находится в `step="cleanup_choice"`, THE VClone_FSM SHALL принимать только callback-запросы с `callback_data ∈ {"vclone_clean:1", "vclone_clean:0"}` от того же `(chat_id, user_id)`, что инициировал команду.
4. WHEN пользователь нажимает `[⏩ Оставить как есть]` (`vclone_clean:0`), THE VClone_FSM SHALL передать исходное Reference_Audio в следующий шаг пайплайна (LLM_Normalizer/VClone_Engine) без вызова Vocal_Separator и записать в VClone_Audit_Log поле `cleaned=False`.
5. WHEN пользователь нажимает `[✨ Очистить звук]` (`vclone_clean:1`), THE VClone_FSM SHALL отправить Bot_Persona_Reply с короткой репликой о начале обработки (например, «Прогоняю через сепаратор…»), запустить Telegram chat action `record_voice` (`ChatAction.RECORD_VOICE`) на время обработки и вызвать Vocal_Separator с путём Reference_Audio.
6. THE Vocal_Separator SHALL запускаться как subprocess в окружении `videotrans/.venv` (CUDA, audio-separator) и НЕ требовать установки `audio-separator` в основной venv бота.
7. THE Vocal_Separator SHALL использовать модель `mel_band_roformer_karaoke_becruily.ckpt` (имя зафиксировано) и возвращать единственный моно-WAV с вокальной дорожкой как Cleaned_Reference_Audio в `temp/`.
8. WHEN Vocal_Separator успешно завершает работу, THE VClone_FSM SHALL передать путь Cleaned_Reference_Audio в следующий шаг пайплайна (вместо исходного Reference_Audio), записать в VClone_Audit_Log поля `cleaned=True` и `cleaned_duration_sec=<длительность>`, и (если Synthesis_Text ещё не получен) перейти в `step="text"` с короткой репликой Bot_Persona_Reply («Голос отделён. Что озвучивать?»), либо (если fast-path и текст уже есть) сразу запустить LLM_Normalizer и VClone_Engine.
9. IF Vocal_Separator завершается с ошибкой (ненулевой код выхода, CUDA OOM, повреждённый WAV, отсутствующая модель, таймаут), THEN THE VClone_FSM SHALL продолжить пайплайн с исходным Reference_Audio, отправить одну Bot_Persona_Reply-реплику о том, что чистка не удалась но пайплайн продолжается, и записать в VClone_Audit_Log поля `cleaned=False`, `cleanup_error=<краткая причина, ≤200 символов>`.
10. THE VClone_FSM SHALL НЕ вводить отдельный таймер на `step="cleanup_choice"`; общий FSM-таймаут из Requirement 3.7 (600 секунд бездействия) SHALL покрывать этот шаг — при срабатывании удаляются временные файлы Reference_Audio и (если есть) Cleaned_Reference_Audio.
11. WHILE Vocal_Separator выполняет работу, THE VClone_FSM SHALL отправить ровно одно прогресс-сообщение в стиле Bot_Persona_Reply без последующих edit'ов с процентами (separator работает 5–30 секунд в зависимости от длины референса).
12. THE Bot_Persona_Reply, отправляемая на этом шаге, SHALL соответствовать общему стилю Арти из Requirement 12 (HTML-теги, короткая реплика, без слов «промпт», «модель», «нейросеть»).

### Requirement 7: Text normalization via LLM

**User Story:** Как админ, я хочу, чтобы Synthesis_Text перед синтезом проходил через LLM, который раскрывает числа и аббревиатуры, расставляет знаки препинания и подбирает интонацию, чтобы итоговая озвучка звучала живо.

#### Acceptance Criteria

1. WHEN VClone_FSM получило Synthesis_Text, Reference_Audio прошло Reference_Validator и Reference_Cleanup_Choice завершён (с Cleaned_Reference_Audio либо с исходным Reference_Audio в качестве финального референса), THE LLM_Normalizer SHALL вызвать `generate_response_stream` с системным промптом, требующим вернуть строго JSON-объект с полями `text` и `control_instruction`.
2. THE LLM_Normalizer SHALL гарантировать, что в `Normalized_Text` числа записаны прописью на языке исходного текста (например, `"в 2024 году" → "в две тысячи двадцать четвертом году"`).
3. THE LLM_Normalizer SHALL гарантировать, что в `Normalized_Text` аббревиатуры расшифрованы по произношению (например, `"МГУ" → "эм гэ у"`).
4. THE LLM_Normalizer SHALL гарантировать, что `Normalized_Text` НЕ содержит квадратных скобок `[...]` и круглых скобок-инструкций `(...)`.
5. THE LLM_Normalizer SHALL гарантировать, что `Direction_Instruction` содержит описание тона (например, `"cold superior tone, female voice"`) без квадратных скобок и без значений из «нативных» VoxCPM-тегов (`laughing`, `sigh`, `Question-ah` и т. д.).
6. IF LLM возвращает невалидный JSON или отсутствуют поля `text`/`control_instruction`, THEN THE LLM_Normalizer SHALL использовать исходный Synthesis_Text как `Normalized_Text` и пустую строку как `Direction_Instruction`.
7. IF длина `Normalized_Text` после нормализации превышает 200 символов, THEN THE LLM_Normalizer SHALL разбить его на предложения и передать VClone_Engine как список сегментов (используется `_split_body_sentences` из `ai/tts.py`).
8. THE Normalized_Text SHALL передаваться в VClone_Engine как `text_input`, а Direction_Instruction — как отдельный параметр `control_instruction` (без склейки в одну строку).

### Requirement 8: Speech synthesis backend hierarchy

**User Story:** Как админ, я хочу, чтобы бот при недоступности VoxCPM Demo Space переходил на локальный VoxCPM, а затем на Fish Speech, чтобы команда не падала из-за rate limit публичного HF Space.

#### Acceptance Criteria

1. WHEN VClone_Engine получает `(финальный_референс, Normalized_Text, Direction_Instruction)`, где `финальный_референс` — это Cleaned_Reference_Audio (если пользователь выбрал `vclone_clean:1` и Vocal_Separator отработал) либо исходное Reference_Audio (во всех остальных случаях, включая выбор `vclone_clean:0` и fallback по Requirement 6.9), THE VClone_Engine SHALL первой попыткой вызвать `_generate_voxcpm_demo` с переданным финальным референсом вместо встроенного `sample.wav`.
2. IF `_generate_voxcpm_demo` возвращает `False` после трёх попыток с переподключением `gradio_client`, THEN THE VClone_Engine SHALL вызвать `_generate_voxcpm_local` со встроенной строкой `(Direction_Instruction) Normalized_Text`.
3. IF `_generate_voxcpm_local` возвращает `False`, THEN THE VClone_Engine SHALL вызвать `_generate_fish` с `Normalized_Text` без скобочных инструкций.
4. IF все три бэкенда возвращают `False`, THEN THE VClone_Engine SHALL вернуть ошибку, удалить промежуточные файлы и инициировать ответ Bot_Persona_Reply с сообщением об ошибке.
5. WHEN `Normalized_Text` разбит LLM_Normalizer на N сегментов, THE VClone_Engine SHALL синтезировать каждый сегмент через ту же иерархию бэкендов, склеить результаты через `pydub` с паузой 100 мс и вернуть один WAV.
6. WHILE VClone_Engine выполняет запрос, THE VClone_Engine SHALL использовать финальный референс пользователя (Cleaned_Reference_Audio или Reference_Audio, не `sample.wav`) и тот же `PROMPT_TEXT`, что в `ai/tts.py`, ИЛИ пустой `prompt_text` с `use_prompt_text=False` — выбор фиксируется на этапе дизайна.

### Requirement 9: Queue and rate limiting

**User Story:** Как админ, я хочу, чтобы запросы `/vclone` не запускались параллельно и не ловили 429 от Hugging Face, чтобы команда оставалась стабильной.

#### Acceptance Criteria

1. WHEN VClone_FSM завершает сбор данных, THE VClone_Command SHALL положить задачу в VClone_Queue и сообщить пользователю позицию в очереди в Bot_Persona_Reply.
2. THE VClone_Queue SHALL обрабатывать задачи последовательно, гарантируя, что в один момент времени активна не более одной задачи клонирования.
3. WHEN VClone_Engine получает HTTP 429, ConnectionError или SSL handshake timeout от Demo Space, THE VClone_Engine SHALL выполнить до трёх повторов с экспоненциальным бэкоффом (`2 * attempt` секунд) и пересозданием `gradio_client`, как это уже сделано в `_generate_voxcpm_demo`.
4. WHILE задача находится в VClone_Queue, THE VClone_Command SHALL отправлять Telegram chat action `record_voice` (`ChatAction.RECORD_VOICE`) каждые ~4 секунды до отправки результата.
5. IF VClone_Queue имеет более 5 задач в ожидании, THEN THE VClone_Command SHALL добавить в Bot_Persona_Reply предупреждение о возможном времени ожидания.

### Requirement 10: Output delivery format

**User Story:** Как админ, я хочу получать короткую озвучку как голосовое сообщение, а длинную — как аудиофайл, чтобы Telegram-клиент корректно её отображал.

#### Acceptance Criteria

1. WHEN VClone_Engine возвращает финальный WAV длительностью менее или равной 30 секундам, THE VClone_Command SHALL перекодировать его в OGG/Opus через `_wav_to_telegram_ogg` и отправить через `send_voice` с `reply_to_message_id`, равным id исходного `/vclone`-сообщения.
2. WHEN VClone_Engine возвращает финальный WAV длительностью более 30 секунд, THE VClone_Command SHALL отправить файл через `send_audio` с `title="Vclone от Арти"` и `performer=user_name` и сохранить расширение `.mp3` или `.ogg` (на выбор дизайна).
3. WHEN результат отправлен в Telegram, THE VClone_Command SHALL приложить caption в стиле Bot_Persona_Reply (короткая реплика Арти).
4. IF финальный файл превышает 50 МБ (Telegram bot API), THEN THE VClone_Command SHALL отправить ссылку на путь файла на диске и НЕ удалять файл автоматически (по аналогии с `/dub`).

### Requirement 11: Cleanup and audit

**User Story:** Как owner-разработчик, я хочу, чтобы все временные файлы голосовых сэмплов и результатов удалялись сразу после ответа, а каждый запрос логировался, чтобы соблюдать гигиену хранилища и иметь след для аудита злоупотреблений.

#### Acceptance Criteria

1. WHEN VClone_Command успешно отправляет результат пользователю, THE VClone_Command SHALL удалить из `temp/` все промежуточные файлы (Reference_Audio, Cleaned_Reference_Audio (если был создан), сегменты WAV, склеенный WAV, OGG-вывод).
2. WHEN VClone_Command завершается с ошибкой на любом этапе, THE VClone_Command SHALL удалить все ранее созданные файлы Reference_Audio, Cleaned_Reference_Audio и промежуточные WAV из `temp/`.
3. WHEN запрос `/vclone` стартует, THE VClone_Audit_Log SHALL записать в `logs/bot.log` строку уровня INFO с полями `chat_id`, `user_id`, `username`, `источник_референса` (`reply_voice`/`reply_video`/`url`/`stepwise_voice` и т. д.) и хешем (sha256) первых 32 КБ Reference_Audio.
4. WHEN запрос `/vclone` завершается (успех или ошибка), THE VClone_Audit_Log SHALL записать в `logs/bot.log` строку уровня INFO с полями `chat_id`, `user_id`, `длина_текста_симв`, `направление_тона`, `cleaned` (True/False — итоговый признак, использовался ли Cleaned_Reference_Audio), `результат` (`ok`/`refused_short_ref`/`refused_long_ref`/`backend_error`/`separator_error` и т. п.) и длительностью пайплайна в секундах.
5. THE VClone_Audit_Log SHALL НЕ записывать содержимое Synthesis_Text целиком в логи (записывается только длина и первые 80 символов).

### Requirement 12: Bot persona consistency

**User Story:** Как owner-разработчик, я хочу, чтобы все реплики `/vclone` (приглашения, ошибки, подтверждения) были выдержаны в стиле Арти из `arti_card.md`, чтобы фича не выбивалась из общего тона бота.

#### Acceptance Criteria

1. THE VClone_Command SHALL формировать ответы пользователю с использованием HTML-тегов `<i>` (наблюдаемое действие) и `<blockquote>` (реплика), как описано в `arti_card.md`.
2. THE VClone_Command SHALL держать каждое сообщение бота короче 4 строк текста и использовать не более одного `<i>`-такта на сообщение.
3. THE VClone_Command SHALL НЕ упоминать в репликах слова «промпт», «модель», «бот», «ИИ», «нейросеть».
4. WHEN VClone_FSM приглашает прислать референс, THE VClone_Command SHALL использовать формулировку, соответствующую гедонистично-аристократическому тону Арти (примеры: «Давай сюда свой сэмпл», «Голос захвачен. Что этот кожаный мешок должен сказать?»). WHEN VClone_FSM сообщает об успешной чистке через Vocal_Separator, THE VClone_Command MAY использовать реплику в стиле «выкручиваю шум до нуля» / «голос отделён» — выдержанную в том же тоне.
5. THE VClone_Command SHALL отправлять caption и приглашающие сообщения с `parse_mode='HTML'`.

### Requirement 13: Error and edge case handling

**User Story:** Как админ, я хочу понятных сообщений на типовые ошибки (неподдерживаемая ссылка, тишина в сэмпле, упавший Demo Space), чтобы быстро диагностировать проблему.

#### Acceptance Criteria

1. IF reply-источник имеет тип `text` без поддерживаемого URL, THEN THE VClone_Command SHALL ответить Bot_Persona_Reply «нужен голос, не текст» и завершить запрос без перехода в FSM.
2. IF `download_audio_for_url` бросает исключение, THEN THE VClone_FSM SHALL ответить Bot_Persona_Reply с укороченной строкой исключения (≤200 символов) и удалить временную папку.
3. IF Reference_Audio содержит только тишину (RMS < заданного порога после ffmpeg `volumedetect`), THEN THE Reference_Validator SHALL отказать с Bot_Persona_Reply «здесь только тишина» и удалить файл.
4. IF Telegram отказывается принять итоговый OGG (например, из-за длительности > 30 минут), THEN THE VClone_Command SHALL fallback-ом отправить файл через `send_audio` и сообщить причину короткой репликой Bot_Persona_Reply.
5. IF одновременно прилетают `Reference_Audio` и Synthesis_Text в одном reply-сообщении (Сценарий А) и LLM_Normalizer падает, THEN THE VClone_Command SHALL продолжить пайплайн с пустым Direction_Instruction и исходным Synthesis_Text без переспроса пользователя.

### Requirement 14: Command surface and discoverability

**User Story:** Как админ, я хочу видеть `/vclone` в списке команд и в `/arti_commands`, чтобы быстро находить её рядом с другими медиа-командами.

#### Acceptance Criteria

1. WHEN регистрируются Telegram-обработчики в `main.py`, THE Bot SHALL зарегистрировать `CommandHandler("vclone", handle_vclone_command)`.
2. WHERE alias `/steal` включён, THE Bot SHALL зарегистрировать дополнительный `CommandHandler("steal", handle_vclone_command)`, маршрутизирующий на тот же обработчик.
3. WHEN пользователь вызывает `/arti_commands`, THE Bot SHALL включить в выводимый список строку с описанием `/vclone` (admin-only, клонирует голос по сэмплу).
4. WHEN пользователь без прав вызывает `/vclone` через автодополнение, THE VClone_Command SHALL обработать его согласно Requirement 1 (отказ в стиле Bot_Persona_Reply).
