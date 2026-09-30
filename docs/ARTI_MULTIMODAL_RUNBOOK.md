# Эксплуатация ядра материалов и маршрутизации Арти

Дата: 1 октября 2026 года. Область — основание A01–A04 и документы, таблицы, изображения и аудио A05–A08. Видео, проекты, студия инфографики и агентский исполнитель реализуются следующими батчами.

## Включение материалов

`ARTI_MATERIALS_ENABLED=1` включает долговечное хранение и структурированный разбор документов в существующем Telegram-процессе. По умолчанию флаг выключен для постепенного перехода. `ARTI_MATERIALS_DIR` задаёт каталог blob-хранилища; значение по умолчанию — `data/materials`, исключённое из Git. Каталог должен быть доступен только процессу бота и администраторам системы. Его резервная копия должна согласовываться с PostgreSQL.

Миграции `009_materials.sql` и `010_material_cleanup.sql` добавляются обычным `ensure_schema` при запуске когнитивного runtime. Это новые миграции; ранее применённые миграции не меняются. Отключение флага не удаляет данные. Физическая очистка при включённом контуре вызывается супервизированным worker каждые 60 секунд; за один проход обрабатываются ограниченные группы записей. Pending очистка когнитивных зависимостей переживает падение процесса.

Текущие квоты ядра: файл до 10 МиБ, до 200 материалов и 200 МиБ сохранённых версий на область доступа, до 500 МиБ на автора, хранение 30 дней. Эти значения находятся в `materials.repository.Quotas`; сервис принимает явную конфигурацию квот. Большие исходники и расширенная пользовательская настройка лимитов относятся к последующим адаптерам.

Для материала сохраняются настоящий автор загрузки, исходное сообщение, scope, версия, хеш, MIME и срок хранения. Пересылка не делает оригинального автора автором загрузки. Личные материалы доступны владельцу в той же области; групповые — текущей общей области и тому же топику. Изменение и удаление требуют совпадения автора. Групповые роли управления проектом будут добавлены в A11/A21.

## Источники и границы текущего извлечения

Document extractor обрабатывает UTF-8 текст, PDF и DOCX. PDF возвращает native glyphs, OCR, regions, grid tables с отдельными cell evidence, изображения и подписи, порядок колонок и повторяющиеся колонтитулы. Native text и OCR не подменяют друг друга при конфликте. DOCX возвращает headings/lists, merged/nested tables, headers/footers и relationships изображений. DOCX page layout неизвестен; его locators используют абзац и OOXML path. Borderless tables, рукопись и неразобранные OOXML notes/textboxes обозначены как ограничения. Complete coverage означает проход единиц manifest, а не человеческую проверку точности или понимание изображения.

При ограничении числа элементов или объёма текста manifest сообщает о частичном покрытии. Сохранённая полная версия извлечения отличается от краткой текстовой проекции, переданной модели. Цитата указывает на asset version, extraction ID, block ID и locator; разрешение проверяется при раскрытии свидетельства.

`/forget <название или содержимое>` при включённом контуре также показывает собственные материалы с кнопкой удаления. Удаление когнитивного источника отзывает связанные материалы. Сначала фиксируется tombstone и очищаются доступные извлечения/зависимые payload, затем maintenance удаляет неиспользуемые blobs. Повторная доставка старого update не восстанавливает удалённый источник. Один источник не удаляет blob, ещё необходимый другому разрешённому материалу.

Перед генерацией и отправкой derived response выполняются повторные проверки версии и доступа. При удалении очищаются pending-карточки и содержимое ожидающих текстовых запросов. Работа уже начатого сетевого вызова не считается отменённой у провайдера; её результат не публикуется после отзыва. Условия хранения сторонним провайдером этим механизмом не изменяются.

## Маршрутизация моделей

Видео: `ARTI_VIDEO_ENABLED=1` внутри opt-in materials, ffmpeg/ffprobe, до 600 секунд/24 кадров/2 МиБ samples, worker до 240 секунд. Sparse coverage всегда partial. `/moment` и `/storyboard` требуют reply и действующего доступа. Frame replay проверяет hash. ASR/vision используют отдельные флаги A07/A08; звук и visible speaker не сливаются в идентичность.

Direct media URL ограничен 10 МиБ/30 секундами/redirect budget; DNS pinned, actual peer проверяется до request, доверие environment proxy отключено. HTML pages видеохостингов без отдельного permitted bounded stream adapter недоступны. Legacy URL summary помечен audio-only; unbounded yt-dlp downloader удалён. Поддерживаются MP4/QuickTime/WebM/AVI containers и audio MP4/WebM с проверкой потоков decoder. Работа через URL не обходит правила хранения/удаления.

Генератор использует `GenerationRequest`, `ImageInput` и `CapabilityRegistry`. PNG не отправляется как JPEG; запрос с изображением может включать поиск. В RP поиск отключён. Fallback должен поддерживать все модальности и функции запроса; несовместимый fallback не получает неполный запрос.

Консервативные существующие Gemini-профили обозначены как `configured`, а не как результаты live-проверки. Неизвестная модель прокси считается текстовой до явного подтверждения. Имя Qwen не определяет отсутствие зрения. Произвольная запись о capability не разрешает отправлять данные на произвольный URL: учитывается точный настроенный proxy endpoint либо встроенный Google adapter.

`ARTI_CAPABILITY_MANIFEST` может указать JSON-файл вида:

```json
{
  "version": 1,
  "endpoints": [
    {
      "model": "confirmed-model-id",
      "provider": "openai",
      "endpoint": "https://configured-proxy.example/v1",
      "inputs": ["text", "image"],
      "features": [],
      "evidence": "probe",
      "observed_at": "2026-09-30T00:00:00+00:00",
      "max_input_chars": 500000,
      "available": true
    }
  ]
}
```

В примере endpoint нужно заменить точным `OMNIROUTE_BASE_URL`. Результат теста напрямую на OpenRouter не доказывает такую же возможность модели через другой прокси. Записи `metadata`/`probe` требуют времени наблюдения и перестают подходить после семи дней. Metadata сообщает о заявленной возможности; реальный запрос проверяет конкретную комбинацию параметров.

JSON manifest не содержит ключи API. Внешняя функция обновления capabilities и circuit-breaker по live health ещё требуют расширения; ошибочные/недоступные capabilities сейчас приводят к допустимому альтернативному маршруту либо служебному отказу.

## Проверки

A11: миграции 014–019 добавляют проекты, роли, публикации и erasure fences. `/project` создаёт/выбирает проект, меняет цель и роли с ожидаемой версией, прикрепляет разрешённый документ, архивирует/возобновляет/удаляет. Просмотр архива и resume используют явный ID. Private membership не раскрывает другой private realm. Выбранный проект ограничивает material recall его актуальными разрешёнными attachments; goal/questions передаются отдельно от collective decisions. ProjectUse проверяет роль, access generation и revision перед delivery.

Публикация: `/project publish_preview CHAT TOPIC version=N` проверяет текущую native membership и возвращает полный JSON состава. `/project publish PLAN_ID` повторно проверяет аудиторию, полномочия и snapshots; разрешение действительно 30 минут. Каждый оригинал требует своего автора. Новый проект и publication link создаются атомарно под source project fence; копии имеют отдельную область, grants и immutable provenance. Повтор продолжает тот же target и копии. Удаление/новая версия оригинала отзывает копии по SQL graph; удаление source/target project отзывает только копии, созданные его публикацией и её автором. Независимые оригиналы других участников не удаляются. Payload источников, previews, reasons/history и index очищаются связанными triggers. Рабочий бот и база пока не активированы.

A10: миграция `013_material_index.sql` индексирует оригинальные blocks, не создавая belief для каждого OCR token. Scope/realm/current version/expiry/tombstone/cognitive suppression фильтруются до SQL ranking. При первом поиске делается bounded backfill восьми разрешённых извлечений; неиндексированное содержимое не объявляется отсутствующим. Исправленный transcript head перекрывает оригинальную ASR-проекцию; head guard действует перед provider/delivery. Trigger физически удаляет индекс при erase/revise и старые transcript overlays при correction. `/materials_find` возвращает exact evidence; `/material_review` сохраняет авторский выбор/причину/открытые вопросы. Gist availability снижается со временем, точные значения восстанавливаются из current original. Role/authority группы не выводится из индивидуальной оценки.

Аудио требует ffmpeg/ffprobe (на этом ПК `C:\ffmpeg\bin`) и `ARTI_AUDIO_ENABLED=1` внутри opt-in контура материалов. `ARTI_AUDIO_ASR_ENABLED=0` оставляет только acoustic observations; значение 1 включает настроенные AssemblyAI/Groq. Источники до 10 МиБ, анализ первых 600 секунд, отдельный worker 150 секунд, фиксированные decoder operations без сетевых протоколов. ASR timeout ограничен; plain text без timestamps не превращается в timed transcript. Native ASR scores не калиброваны. Providers получают только проверенный ограниченный clip; их retention policy этим механизмом не меняется.

Добавочная миграция `012_material_observations.sql` хранит текущие transcript heads. Исправление требует автора оригинала и ожидаемого head. Root timeline разбит на bounded chunks; ограничения блоков и coverage видимы. Acoustic appraisal context загружается по разрешённому current source, не по сходству голоса. Эксплуатационные флаги в `.env` этой сессией не изменялись.

Запускать из корня проекта:

```powershell
python -m tools.run_materials_tests
python -m tools.run_materials_tests --all
python -m tools.evaluate_materials
python -m tools.check_document_stack
python -m tools.evaluate_documents
python -m tools.evaluate_datasets
python -m tools.evaluate_images
python -m tools.evaluate_audio
python -m tools.evaluate_video
python -m tools.probe_material_provider --model stealth/space-bunny-alpha
```

Все команды, кроме `probe_material_provider`, работают offline относительно провайдеров; SQL-сценарии создают и удаляют отдельную `arti_cognition_test_<uuid>`. Права PostgreSQL должны позволять создание тестовой базы. Конфигурационная база используется для подключения управляющего соединения; её таблицы не изменяются. OCR-проверки требуют действующего Tesseract с `rus`, `eng` и `osd` и не пропускаются незаметно при его отсутствии.

Последняя команда вызывает OpenRouter только на синтетическом тексте и изображении. Проверяет текст, native tool calling с forced/auto choice и изображение, если модель заявляет image input. Она сохраняет метрики и outcome без ключей и исходного содержимого ответа. Она не вызывает Telegram и автоматически не обновляет manifest прокси.

Отчёты расположены в `docs/evaluation/materials_*`. `materials_documents.json` содержит девять синтетических документов, exact facts/CER, раскрытие настоящей cell region, версии/хеши и время/RSS. Этот корпус не заменяет независимую оценку на реальных сканах, видео, поведении агента или читаемости инфографики.

## Отключение и восстановление

Для отключения долговечного Telegram intake установить `ARTI_MATERIALS_ENABLED=0` и перезапустить процесс обычным способом. Старые файлы остаются в хранилище; перед включением после паузы выполнить maintenance, чтобы истёкшие источники не участвовали в работе. Чтение дополнительно проверяет срок хранения даже до физической очистки.

При ошибке manifest убрать `ARTI_CAPABILITY_MANIFEST` либо исправить соответствующую запись; встроенные консервативные профили продолжают работу. Откат к прежнему генератору производится откатом кода, без удаления SQL-таблиц. На время обслуживания можно выключить ответы штатной командой бота. Полноценные отдельные feature flags поздних возможностей вводятся по мере их реализации.

## Установка и управление A05

Python-библиотеки устанавливаются обычным `python -m pip install -r requirements.txt`. OCR engine — отдельная локальная зависимость. На Windows использовать [installer UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) в **новом отдельном каталоге**. На этом ПК установлена версия 5.5.3.20260724 в `%LOCALAPPDATA%\ArtiOCR`; модели `rus`, `eng`, `osd` присутствуют. SHA256 использованного installer: `bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4`. На Debian/Ubuntu: `apt install tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tesseract-ocr-osd`.

`ARTI_TESSERACT_CMD` задаёт абсолютный путь к доверенному engine; `ARTI_TESSDATA_DIR` — к каталогу моделей. Автопоиск поддерживает PATH, `%LOCALAPPDATA%\ArtiOCR`, `C:\Program Files\Tesseract-OCR` и стандартные Linux-каталоги. Перед эксплуатацией выполнить `python -m tools.check_document_stack`. Смена engine, моделей или parser stack создаёт новую cache version; старое извлечение не перезаписывается.

`ARTI_OCR_ENABLED=0` оставляет native structured extraction и явно помечает scans/mixed страницы partial. `ARTI_DOCUMENTS_ENABLED=0` переключает на изолированный native adapter без layout/OCR; лимиты процесса сохраняются. Advanced extraction включено по умолчанию и в transient document parser; долговечное сохранение по-прежнему определяется отдельным `ARTI_MATERIALS_ENABLED`.

Стандартные лимиты: 64 страницы/узла, 300 000 символов, 3500 blocks; два worker, 90 секунд, 768 МиБ/process, 48 МиБ temporary disk, 8 МиБ результата. Windows Job ограничивает child tree и закрывает его при выходе worker; Linux backend использует rlimits/process group. На этом этапе запуск и проверки выполнены на Windows; Linux ещё не прогонялся. Эти меры ограничивают ресурсы, не являются контейнером безопасности. Лог `materials.extractors.isolation` содержит status, elapsed, sampled RSS и temporary disk, без текста документов. В production оставлять каталог оригиналов доступным только процессу бота.

`MaterialService.evidence_region(ref, actor, extractor, reread=True)` возвращает crop и отдельное immutable региональное наблюдение. Требуется реальный `EvidenceRef`; произвольная координата не заменяет проверку доступа. Наблюдение не автоматически исправляет число в старом extraction. Полный Telegram-интерфейс просмотра и правок относится к A16; сейчас API доступен другим внутренним инструментам.

Используемые методы описаны в [pdfplumber](https://github.com/jsvine/pdfplumber), [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html) и [Tesseract TSV/OSD](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html). Локальный стек имеет MIT/Apache/BSD-компоненты; условия и notices PDFium и его зависимостей поставляются с pypdfium2. Лицензии компонентов сохранять при распространении.

## A06: datasets и расчёты

Зависимость: openpyxl >=3.1.5,<4. `ARTI_TABLES_ENABLED=0` отключает CSV/XLSX extractor; команды дополнительно требуют включённого `ARTI_MATERIALS_ENABLED`. Настройки окружения не менялись в ходе разработки. Миграция 011 применяется обычным `ensure_schema`; уже применённые 009/010 не переписаны. Рабочая база в этой сессии не мигрировалась.

Ответом на исходный файл:

```text
/dataset
/dataset sheet=1
/calc sum B2:B4 sheet=1
/calc percent B2 reference=B2:B4 sheet=1
/calc mean B2:B4 locale=ru missing=exclude sheet=1
/calc convert B2 unit=m sheet=1
/datafix B2 1300 sheet=1
```

`/datafix` — явное подтверждение автором, а не автоматическое предложение LLM. `locale` и `date_order` создают отдельную series нормализации; применяйте те же параметры при продолжении работы с этой series. Предложение через `propose_correction` само по себе не изменяет данные. Оригинал всегда доступен через EvidenceRef, confirmed override хранится отдельно. Currency conversion API требует EvidenceRef коэффициента, точного factor и при исправленном коэффициенте `dataset_id`; Telegram пока поддерживает только совместимые встроенные единицы, без угадывания курсов.

Стандартные лимиты: 3000 ячеек, 128 столбцов, 16 листов; фактически сохранённая сетка до 100000 ячеек, огромная объявленная dimension не используется. Неполная выборка помечается partial, диапазон с неизвлечёнными ячейками не считается. CSV загружается как UTF-8; явно настроенный CP1251 extractor доступен для importer, durable intake требует предварительного декодирования. Макросы не исполняются, внешние ссылки не открываются, formula-like CSV остаётся текстом.

Engine поддерживает закрытое подмножество Excel; [openpyxl не вычисляет формулы](https://openpyxl.readthedocs.io/en/3.1.2/simple_formulae.html). Кеш сохраняется как исходное наблюдение, сравнивается с новым результатом и не подменяет его. Нет `eval` или исполнения кода файла. COUNT поддерживает извлечённые числовые/blank ссылки; текстовые ссылки дают явный отказ, а не полную семантику Excel. Точность — 50 цифр, пределы числа и выражения ограничены; добавляются source refs, formula trace, engine version и dataset dependencies. Для проверки: `python -m tools.evaluate_datasets`, затем общий suite. Quota — до 2000 non-erased derivative payloads в realm; истёкшие/забытые источники исключаются до физической очистки.

## A07: фото, области и visual observations

`ARTI_IMAGES_ENABLED=0` отключает новый image extractor. Durable photo intake зависит от `ARTI_MATERIALS_ENABLED`; основной photo/album/pending flow переносит MaterialUse до отправки. `ARTI_OCR_ENABLED=0` оставляет uninterpreted оригинал. `ARTI_VISUAL_OBSERVATIONS_ENABLED=1` подключает отдельный routed provider pass; по умолчанию он выключен. `ARTI_VISION_MODEL` выбирает модель, default `gemini-2.5-flash`; registry A04 должен подтверждать image input нужного endpoint. Тестовый OpenRouter результат не включает автоматически другой proxy и не меняет env.

Региональные API: `evidence_region(ref,actor,ImageExtractor(),reread=True)` для OCR и `observe_image_region(ref,actor,extractor,analyzer)` для отдельного visual pass. Нужен существующий authorized region ref; исходник не переписывается. Нормализованные координаты относятся к [EXIF-displayed image](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html). До 512 OCR lines, 16M исходных pixels, crop ≤8M, zoom ≤4×, provider preview ≤1536px/5MiB. Превышения явные. Хранятся OCR preprocessing/rotation, uncertainty, source hashes и версия метода.

Проверки: `python -m tools.evaluate_images` offline; `python -m tools.evaluate_images --live --model stealth/space-bunny-alpha` вызывает OpenRouter на synthetic diagrams/axes. `--case log_axis` ограничивает повтор одним случаем и сохраняет отдельный отчёт. Live rejection/timeouts учитываются как failures модели, не как пройденная способность. Structured vision использует [async Gemini API](https://github.com/googleapis/python-genai) либо OpenAI-compatible messages, не предоставляет tools и строго проверяет JSON до сохранения.
