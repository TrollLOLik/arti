# Студия, задачи и подписки A12–A23

Дата: 1 октября 2026 года. Этот документ дополняет [runbook материалов](ARTI_MULTIMODAL_RUNBOOK.md). Mini App отсутствует. Полный статус и ограничения: [progress](ARTI_MULTIMODAL_AGENT_PROGRESS.md).

Обновление 2 октября: основной пользовательский вход — [меню Telegram](ARTI_TELEGRAM_MENU.md). Оно заменяет ввод команд, ID и JSON для обычных действий кнопками, списками объектов и формами. Команды ниже сохранены как технический совместимый интерфейс.

## Включение и откат

`ARTI_MATERIALS_ENABLED=1` включает проекты/материалы/артефакты. `ARTI_AGENTS_ENABLED=1` дополнительно включает обработку обычных агентских просьб, durable executor, scheduler и отдельный worker обновления карточек. Оба флага по умолчанию выключены; `.env` в этой сессии не менялся. Отключение исполнителя сохраняет задачи; новые внешние вызовы прекращаются при следующем цикле. Уже начавшийся remote effect может иметь неизвестный исход, который сохраняется в журнале.

Миграции 020–026 применяются обычным `ensure_schema`: artifacts/revisions, Tasks/ToolCalls/grants/work actions/delivery, workflow objects/subscriptions, replanning, task callbacks, timestamps, processing cards. Миграция 027 добавляет долговечные состояния меню и журнал native requests. Существующие миграции не переписывались. Рабочая база в этой сессии не мигрировалась. Перед активацией нужна согласованная резервная копия PostgreSQL и blobs.

Зависимости: существующие Pillow/reportlab/pypdf/python-docx/openpyxl и новый `jsonschema>=4.23,<5`. `artifacts/rendering/fonts/DejaVuSans.ttf` закреплён вместе с лицензией. PNG/SVG/PDF используют одну сцену; SVG включает шрифт, поэтому может быть существенно больше PNG. Экспорты создаются из текущего authorized head при запросе; безусловного публичного URL к личному файлу нет.

## Пользовательский путь

В меню открыть «Помоги с делом» → «Проекты», создать или выбрать проект и добавить разрешённые материалы. Затем в «Помоги с делом» выбрать «Инфографику» или «Поручить дело». При включённом агенте просьба «сделай инфографику из этих файлов» также сохраняет задачу планирования, связывает источники и открывает карточку. «Агент: …» запускает типизированную многошаговую задачу. Выбранные attachments, текущие позиции/решения и числовые свидетельства имеют отдельные provenance и guards. Долгая модельная подготовка восстанавливается после рестарта.

Карточка результата предлагает источники, JSON, PDF, PNG, SVG, формат и принятие/отклонение версии. Формат, непригодный для текущих данных, отвергается без потери старой версии. Текущая автоматически созданная версия и принятая пользователем версия хранятся отдельно. Reply «сделай синим», «удали второй блок», `замени второй блок на "новый текст"` превращается в ограниченную patch operation. Для неоднозначной правки требуется номер/текст; числовое исправление источника выполняется через `/datafix`, а не путём перерисовки числа.

Команды с JSON принимают UTF-8 reply text или reply document до 500 КБ. Изменение требует ожидаемой версии:

```text
/artifact create                  (reply ArtifactSpec JSON)
/artifact show ID
/artifact patch ID VERSION        (reply JSON array of typed operations)
/artifact rollback ID VERSION OLD_VERSION
/artifact accept ID VERSION
/artifact reject ID VERSION
/artifact style calm|ink|night
/task plan                       (reply Plan JSON)
/task show ID
/task result ID
/task pause|resume|cancel ID VERSION
/task revise ID VERSION          (reply new Plan JSON)
```

Точное написание `/artifact rollback` и остальные argument forms проверяются текущим command help. `/task result` по явной просьбе возвращает также сохранённые outputs незавершённой задачи; JSON содержит status и `overall_verified=false`. Автоматическая доставка выполняется только после общего успешного verification. `/task show` может открыть новую карточку по явной просьбе, в том числе после потери старой.

Карточка обработки редактируется максимум раз в 5 секунд при изменении состояния. Неизвестная отправка/правка не вызывает автоматическую серию повторов; подтверждённый receipt и доставка результата независимы от успеха вычисления. Source erasure удаляет retained payloads, pixels и связанные receipt pointers. Изменение проекта, участников, topic, режима или RP scene проверяется перед доставкой.

## Инструменты, расходы и восстановление

Registry содержит 24 инструмента: материалы/search, datasets/compute/transform, artifacts/create/patch/export, research/search/fetch/compare, data/report exports, image/video/music, три connector preview/write пары, declarative runtime и workflow planning. Планировщик выдаёт JSON, код проверяет closed schema, версии, DAG/dependencies и outputs. Модель не получает инструмента выдачи grant или изменения Python.

Default Task: до 40 вызовов, 8 МиБ сохранённых outputs, 600 секунд и cost ceiling 1; API позволяет ограниченную настройку до 100 вызовов, 32 МиБ, суток и ceiling 100. Один вызов tool — до 4 МиБ, timeout до 300 секунд, два независимых reads одновременно. Изменения общих ресурсов сериализуются advisory locks между workers. Занятый ресурс возвращает задачу в очередь до записи нового call intent. Запланированная стоимость резервируется до вызова; известная фактическая usage cost заменяет резерв после результата.

`ARTI_AGENT_MODEL` выбирает модель; fallback — `COGNITIVE_MODEL`, затем `stealth/space-bunny-alpha`. `ARTI_PLANNER_COST_CEILING` и `ARTI_MEDIA_COST_CEILING` по умолчанию 1. Если provider не сообщает стоимость, сохраняется консервативный ceiling и diagnostic. Это внутренний лимит допуска вызовов, а не гарантия верхней границы счёта внешнего провайдера. Цены и реальные ограничения аккаунта калибруются отдельно.

При необходимости внешнего эффекта появляется кнопка «Проверить действие». Preview показывает фактические args, ресурсы, аудиторию, адресата, version и digest:

```text
/task preview ID STEP
/task approve ID VERSION STEP DIGEST
/task reconcile ID STEP
```

Согласие относится к показанному snapshot и реальному пользователю с правом approve. PDF, сайт, quote или stored Procedure не дают это право. Новый запуск процедуры требует своего применимого grant. После unknown внешняя запись не повторяется автоматически; сверка разрешена только поддерживающим её адаптером. Receipt/success старого эффекта не стирается при остановке дальнейших шагов.

Рендер и точные данные программные. Image generator создаёт декоративную часть; текст/цифры схемы поверх неё не доверяются изображению. Generated variant сохраняет pixels в source-bound derivative, prompt/provider/parameters и dependencies. Удаление запроса/ссылочного материала отзывает variant. Видео использует фактическое соответствие существующих моделей и duration 4/8; качество художественного содержимого не считается проверенным по одному файлу/receipt.

## Исследования и подключения

`research.fetch` читает публичный HTML/text/JSON через bounded URL adapter: DNS/redirect/actual peer, MIME/size/time limits. Snapshot содержит URL, read time, hash, coverage и untrusted role. Quote verification проверяет точное наличие и текущий доступ; сравнение сохраняет supports/objects/unknown, а не объявляет истинность или консенсус.

`ARTI_SEARCH_ENDPOINT` — необязательный публичный search endpoint с JSON `{results:[{url,title,snippet}]}`. Без него search возвращает `unavailable`; чтение явно предоставленных URL остаётся доступным. Calendar/tasks/storage представлены adapter contract: collection ACL, authorization context, preview, idempotency/reconciliation и revoke. В этой среде реальные accounts не подключены, поэтому default write возвращает unavailable. Contract/replay doubles не являются доказательством настоящей записи в календарь.

`runtime.transform` исполняет декларативную data program в отдельном worker без наследования секретов: sum/count/filter_equal/sort/take, максимум 100 операций, 4000 строк, 1 МиБ input, 15 секунд, 512 МиБ memory. Нет import/eval/произвольного Python, host paths или shell. Эта функциональность не объявляется sandbox для произвольного исполняемого кода.

## Совместные решения и процедуры

```text
/decision propose "Выбор даты" "Пятница" "Суббота"
/decision support|object ID VERSION "причина"
/decision confirm ID VERSION "Пятница" "причина выбора"
/decision revoke ID VERSION "причина"
/assignment offer USER_ID "Поручение" [ISO_TIME_WITH_TIMEZONE]
/assignment accept|decline|complete ID VERSION ["причина"]
/procedure save TASK_ID           (reply recipe/input_schema/examples JSON)
/procedure propose ID VERSION     (reply candidate recipe JSON)
/procedure approve ID VERSION PROPOSAL_ID
/procedure revise ID VERSION      (explicit confirmed recipe JSON)
/procedure run ID                 (reply bindings JSON)
```

Поручение организатора остаётся offered до принятия самим исполнителем. Организатор подтверждает своё решение; молчание остальных не считается голосом. Current project context хранит реальные позиции и статусы, имеет отдельный WorkflowUse и late revision guard. Частные события и другие топики не собираются в общий summary автоматически.

Procedure создаётся только из успешной задачи с сохранёнными outputs и deterministic checks, после native user confirmation. Схема `$input` bindings, pinned tool versions и 1–8 контрольных примеров проверяются перед принятием. Fixture checks не выполняют платных внешних записей. Proposal не меняет действующую accepted recipe до review; применимость проверяется при каждом запуске. Source expiry/revoke и несовместимая версия приостанавливают работу.

## Подписки и сценарии

```text
/subscription new PROCEDURE_ID   (reply JSON ниже)
/subscription list
/subscription show ID
/subscription last ID
/subscription change ID VERSION  (reply changed bindings/schedule/limits JSON)
/subscription pause|resume|delete ID VERSION
/scenario new                    (reply title/scenario/stages JSON)
/scenario answer ID VERSION STAGE_ID "ответ"
/scenario visual ID
/scenario list|show|json ID
```

Пример подписки:

```json
{
  "bindings": {"dataset_id": "EXISTING_CURRENT_DATASET_ID"},
  "schedule": {"kind": "daily", "timezone": "Asia/Yekaterinburg", "hour": 18, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
  "max_calls": 20,
  "max_cost": "1",
  "max_bytes": 8388608,
  "max_runs": 10
}
```

Bindings должны соответствовать схеме конкретной процедуры. Поддерживается также interval: seconds 300–2678400, timezone и ISO anchor. DST fold использует первую реальную occurrence, gap — следующую допустимую минуту. Пропущенные occurrences coalesce в последний выпуск. `until` и `max_runs` ограничивают продолжение; последняя ready delivery завершается до установки completion pause. Cursor/version/schedule обновляются атомарно.

Неизменный содержательный fingerprint проходит тихо. Байты файлов, случайные IDs, read timestamps и декоративные пиксели не становятся новостью; tool exports имеют hash содержания. Unknown доставка не продвигает known baseline и не повторяется автоматически. В группе выпуск проходит действующий G arbiter: public window, topic/full visibility, opt-out, quiet policy, budget/lease и финальный source/membership guard.

Сценарии `quest`, `story`, `visual_review` сохраняют источники/этапы/прогресс и создают версионируемый визуальный Artifact. Story требует `fiction:true` на каждом этапе; иллюстрации декоративны. Реальные утверждения visual review требуют quote proof, иначе остаются unknown. Учебный прогресс не считается измеренным mastery. `/decision`, `/assignment`, `/procedure`, `/subscription`, `/scenario` поддерживают `json ID`, `show ID` и управление согласно текущим правам; изменение по старой версии отвергается.

## Проверки и следующий этап

```text
python -m tools.run_materials_tests --all
python -m tools.evaluate_artifacts
python -m tools.evaluate_agents --live --model stealth/space-bunny-alpha
```

Offline suite использует `arti_cognition_test_<uuid>` и transport/provider doubles, без вызовов Telegram и рабочего DB mutation. Renderer corpus открывает реальные PNG/SVG/PDF, извлекает кириллицу/точные числа, растеризует PDF для визуальной проверки. Live planner работает только на собственных synthetic fixtures; отчёт не заменяет независимую оценку. Неудачные initial/schema/input-fence probes сохранены рядом с финальным отчётом.

A24: реальные смешанные материалы, human design/ASR/vision judgments, настоящие Telegram conversations, длительные subscription trials, реальные accounts для выбранных connectors, стоимость/latency и rollout/rollback. Runtime flags сохраняются до этого этапа.
