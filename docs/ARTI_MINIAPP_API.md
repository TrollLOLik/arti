# Mini App · контракт MA00–MA04 и H6 P2

Версия `miniapp-3`, 16.10.2026. Утверждённый клиент использует действующую сессию, общий текстовый обработчик Арти и личную историю Telegram. Социальные чаты и комнаты относятся к MA05–MA06; медиа и агентские карточки — к MA07–MA08. `miniapp-3` добавляет «Жизнь Арти» (H6, стадия P2): раздел [в конце документа](#жизнь-арти-h6-p2).

## Вход и права

`POST /api/auth/telegram` принимает `{init_data}` и проверяет [подпись Telegram](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), возраст до часа, будущее отклонение до 30 секунд, размер до 16 KiB, дубли полей и профиль человека. Идентичность берётся только из проверенного профиля. `GET /api/session`, `GET /api/bootstrap`, `POST /api/auth/logout` работают с серверной сессией.

Cookie `__Host-arti_session`: случайные 256 бит, Secure, HttpOnly, SameSite=None, Path=/; в PostgreSQL хранится только SHA-256. TTL до 30 минут и не за пределами действительности initData. Повторный вход вращает сессию, logout отзывает её. Изменения требуют точного Origin и X-CSRF-Token; ни query, ни заголовок с initData не заменяют сессию. Нет wildcard CORS. API возвращает Cache-Control:no-store. Клиент хранит в localStorage только тему.

`POST /api/auth/demo` разрешён только явно запущенным loopback preview. Его отдельная cookie/пустые данные изолированы от рабочего аккаунта. Demo не использует AI/Telegram/PostgreSQL и не включает отправку. Ошибка настоящей авторизации никогда не переключает приложение в demo.

Каждый запрос проверяет active membership и владельца личной беседы. UUID чужого и отсутствующего ресурса дают одинаковый 404. SSE повторяет проверку сессии и членства каждые две секунды.

## Беседы, сообщения и очередь

| Метод и путь | Тело / результат |
|---|---|
| GET /api/conversations | `{items:Conversation[],next_cursor:null}` |
| GET /api/conversations/{uuid} | `{conversation:Conversation}` |
| GET /api/conversations/{uuid}/messages?limit=50&before=123 | `{items:Message[],next_before:number|null,history_revision:number}` |
| GET /api/conversations/{uuid}/messages?after=123 | Новые сообщения по seq; нельзя вместе с before |
| POST /api/conversations/{uuid}/messages | `{text,client_message_id:UUID,history_revision:number,reply_to_id?:UUID|null}` → 202 `{message:Message,request:{id,state}}` |
| GET /api/conversations/{uuid}/requests/{request_id} | `{request:{id,state}}`, без payload и внутреннего текста ошибок |

Conversation: `id,kind,title,role,created_at,updated_at,last_message,message_count,unread_count,event_cursor,history_ready,history_revision`. Публичен основной `arti_private`, owner. message_count/last_message учитывают разрешённую историю; unread_count пока 0. Пагинация списка бесед появится с социальными чатами; сейчас лимит 50.

Message: `id,conversation_id,seq,author_id,author_kind,text,client_message_id,reply_to_id,created_at,source,request_id,request_state,mirror_state`. source — telegram/miniapp. request_state — queued/running/completed/succeeded/failed/cancelled/expired/delivery_unknown или null. mirror_state — prepared/sending/delivered/delivery_unknown/cancelled или null. Это подтверждение транспорта, не отметка прочтения человеком.

Текст — 1–8000 символов и до 32000 UTF-8 байт, без NUL. Reply обязан быть видимым сообщением той же беседы. Страница содержит до 100 записей, по умолчанию 50, в порядке seq. next_before указывает на самую старую запись, если ниже есть ещё страница.

UUID client_message_id идентифицирует одно намерение отправки. Повтор с тем же текстом/reply возвращает тот же message/job; изменённый payload даёт 409 idempotency_conflict. Повтор не продлевает budget и не запускает вторую генерацию. history_revision обязателен в HTTP: несовпадение под блокировкой беседы даёт 409 history_reset, включая reset пустого разговора. Старое намерение после reset не становится новым сообщением.

Одной транзакцией фиксируются когнитивный источник, app message/event/outbox, job и работа Telegram mirror. Исходный текст зарегистрирован до приёма HTTP, поэтому reset до старта worker не может воскресить источник. Worker использует существующий process_user_reply, cognition/persona/memory/model/search, checkpoints и budgets. Mini App передаёт message_id=None и явный source_key, без фиктивного Telegram Update. Общая FIFO-полоса — настоящий private chat ID пользователя, topic=-1, text. App-сообщения не объединяются и не ждут Telegram debounce.

Ответ сначала публикуется внутри приложения атомарно: UUID receipt, сообщение, cognitive delivered_action, зависимости/encode job и событие. Пара `(request_id,ordinal)` восстанавливает тот же UUID после перезапуска. Публикация проверяет lease/deadline, epoch/authority/reset/rebuilding, источники, память, материалы и владельца. Provider/network I/O не выполняется внутри транзакций.

Прямой POST в Mini App — явная просьба владельца; он не отменяется переключателем автоматических ответов Telegram, который по умолчанию выключен до /start. Ошибка генерации отмечает job failed и не записывает техническую заглушку как реплику Арти. Автоматический повтор платной генерации после ошибки не выполняется.

## Общая история и зеркало Telegram

История — проекция разрешённых cognitive user/delivered_action событий того же владельца в private/default/topic -1, с сохранением времени и источника. Для старых historical user-событий с неизвестной аудиторией требуется imported-связь с исходной memory_messages того же автора/private chat; неподтверждённые старые assistant-реплики не переносятся. Начальный backfill завершается перед новым intake, повторный проход идемпотентен. Нет импорта полного архива Telegram или макетных реплик. Длинный исторический текст свыше 8000 символов показан явно обозначенным фрагментом; вложения относятся к MA07. Live reconciliation ищет отсутствующие source IDs, а не глобальный max cursor; интерпретация/эмбеддинг при переносе не запускаются.

Копия входящего app-текста приходит от бота как «Из Mini App · Имя: …». Ответ зеркалируется отдельной durable доставкой. Копии не создают второй когнитивный ответ. Timeout внешней отправки даёт delivery_unknown только зеркалу; готовый app-ответ остаётся доступным. После неизвестной доставки зеркало вслепую не повторяется. Telegram-копии делятся по UTF-16, имеют отдельные receipt/ordinal. Повторное message.created с тем же message UUID может сообщать обновлённый mirror_state — клиент объединяет метаданные без второго пузыря.

Reset/suppression стирает текст app-проекции, отменяет/очищает очереди и незавершённые зеркала и повышает history_revision. Read/replay также проверяют текущую видимость источника и зависимостей. Сохраняются идентификаторы tombstone, поэтому повторный перенос не возвращает удалённую запись. Удаление app-проекции не заявляет удаление уже доставленных сообщений из Telegram.

## События и переподключение

`GET /api/conversations/{uuid}/events?after=0&limit=100`:

```json
{"items":[{"seq":1,"kind":"message.created","resource_id":"UUID","message":{}}],"cursor":1,"requests":[{"id":"request-id","state":"running","message_id":"UUID"}],"history_revision":1}
```

kind: message.created/history.reset/membership.changed. У удалённого или недоступного ресурса message=null. Курсор — последний обработанный seq внутри этой беседы; при ограниченной странице он не перескакивает через оставшиеся события. Будущий/некорректный курсор требует resync (409/400). requests — текущий snapshot активных и последних заданий, без исходных тел.

`GET .../events/stream?after=0` использует ту же cookie. [SSE](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events) отправляет `event:state`, `id:cursor`, JSON выше в data каждые две секунды, даже при пустом items. Last-Event-ID имеет приоритет над query. `session.expired`/`revoked` закрывают приватный UI. До трёх потоков на пользователя в процессе, ограниченное время записи медленному клиенту; HTTP имеет общий rate limit.

Клиент начинает replay с after=0, сверяет revision и не загружает через replay старые страницы автоматически. При смене revision он очищает историю, черновик и неподтверждённую попытку. При обрыве SSE используется ограниченный polling 3,5–15 секунд с восстановлением SSE. Скрытие/смена экрана/logout закрывает подписки. Чтение старых сообщений не прыгает вниз от входящих; доступна кнопка новых сообщений.

## Контексты и возможности

UUID app_conversation отделён от транспортного адреса. Привязка основного чата использует реальный Telegram user/private ID; старые пятиэлементные ContextKey.identity() и material scope hashes сохранены. Для комнат выделяются NativeContextKey/NativeAudienceScope с UUID и отдельный cognitive_native_contexts со своим начальным состоянием/сериализацией. Комната не наследует private-аудиторию владельца; её исполнение закрыто до MA06.

В live-режиме при active cognitive runtime доступны chat_read/chat_send/history_sync/live_events. В demo или без подключённого сервиса три последних false. human_chats/rooms/create/memory ещё false. Organizer сохраняет общий domain API; app-source участвует в правилах единственного планировщика и удаления. Голос, реакции, стикеры, вложения, генерация медиа и агентские карточки не выполняются из app-текста до следующих пакетов. Подключённые материалы/проекты используются с существующими guards.

`/api/organizer`, `/api/projects`, `/api/library`, `/api/library/{id}/download`, `/api/handoff` сохраняют права сессии/CSRF. Для UI раздаётся только frontend/dist по /static/app; исходники и blob-каталоги не публикуются.

Миграции 055–058 аддитивны и используют checksum ledger. Основной cognition runner применяет их при запуске бота; standalone foundation runner применяет 055–056. Миграции проверены на одноразовых БД, рабочего развёртывания в MA03–MA04 не выполнялось.

Ошибки: `{error:{code,message}}`; 400 ввод, 401 сессия, 403 Origin/CSRF, 404 ресурс, 409 конфликт/reset/недоступность, 429 частота, 503 сервис. Произвольный SQL/провайдерский exception клиенту не выдаётся. Реальные Telegram WebView/HTTPS/провайдеры проверяются отдельно перед пилотом.

## Жизнь Арти (H6, P2)

«Сейчас у Арти» и «Комната Арти» показывают три разных вида данных, и клиент подписывает их по-разному:

- **В образе** — занятия персонажа: вышивка, реставрация записи, вкусы в чае. Это заявленная художественная жизнь, общая для всех (`cognition/pursuits.py`, [H6](ARTI_H6_IN_CHARACTER_LIFE.md)).
- **По-настоящему · ссылка ↗** — последняя настоящая заметка о прочитанном (H4c) за 7 дней.
- **Настроение · по вашему разговору** — её настроение только в личном разговоре с этим человеком.

Capability `arti_life`: `true` в рабочем режиме, `false` в локальном просмотре. В просмотре эндпоинты работают, но возвращают пустую жизнь, а настройка хранится только в сессии просмотра.

| Метод и путь | Тело / результат |
|---|---|
| GET /api/arti/life | `ArtiLife`, см. ниже |
| GET /api/arti/life/settings | `{settings:{mentions,explainer_seen}}` |
| PUT /api/arti/life/settings | `{mentions?:bool,explainer_seen?:bool}` или `{reset:true}` → `{settings}` |

Права те же, что у остальных эндпоинтов: личность — только из серверной сессии, query и заголовки её не заменяют. PUT — изменение, поэтому нужны точный Origin, `X-CSRF-Token` и действует лимит действий пользователя. Тело PUT — только JSON. Лишние поля (`user_id` и другие), не-boolean значения, пустое тело, `reset:false` и `reset` вместе с другими полями дают 400. Значение, которого нет в теле, не меняется.

### Настройка «Жизнь Арти в образе»

- **Где хранится.** Таблица `arti_life_settings` (миграция `060`), одна строка на Telegram-пользователя: `mentions`, `explainer_seen_at`, `source='miniapp'`, `version`, `updated_at`. Без строки действуют значения по умолчанию: упоминания включены, объяснение не показано.
- **Явный источник.** Строку пишет только действие самого человека. Это не память: забытое сообщение (`/forget`), очистка диалога (`/clear_context`, `reset_history`) и пересборка когниции её не трогают.
- **Как снять.** Только сам человек — `PUT {reset:true}` возвращает значения по умолчанию — или стирание его данных: `life_settings.erase(conn, user_id)` для будущего полного удаления.
- **`explainer_seen`.** Первое `true` фиксирует момент показа, повторное его не сдвигает, `false` снимает отметку. Клиент показывает «Как живёт Арти» сам при первом входе в Комнату, пока флаг `false`, и отправляет `true` по кнопке «Понятно».
- **`mentions:false`** («Не упоминать»):
  - `ai/generation.py` не добавляет блок «[Твоя жизнь в образе — …]» в ответы этому человеку — и в Telegram, и в Mini App. Человек ответа — автор хода, а для личного чата без автора — сам чат.
  - Если настройку не удалось прочитать за общий тайм-аут блока (0,3 с), блока тоже нет.
  - Остальным собеседникам блок по-прежнему виден.

### ArtiLife

```json
{"settings":{"mentions":true,"explainer_seen":false},"in_character":true,
 "pursuits":[{"kind":"embroidery","title":"Вышивка «Мотылёк»","status":"active","progress":0.62,"intention":"вышивать дальше",
   "last_event":{"kind":"step","summary":"…","feeling":"…","at":"ISO","title":"Вышивка «Мотылёк»"},
   "embroidery":{"motif":"Мотылёк","stitched":340,"total":548,"replaced":{"motif":"Клевер","stitched":214,"total":812,"reason":"цвета спорят друг с другом"}}},
  {"kind":"restoration","title":"Запись № 7","status":"active","progress":0.43,"intention":"восстановить выпавшие места","last_event":null,
   "restoration":{"record":{"number":7,"medium":"кассета","content":"вальс на фортепиано","seconds":84},"stage":3,"step":3,"steps":7,
     "stages":[{"name":"осмотр","goal":"…","state":"done"},{"name":"очистка","goal":"…","state":"done"},{"name":"пропуски","goal":"…","state":"current"},{"name":"выравнивание","goal":"…","state":"next"},{"name":"сверка","goal":"…","state":"next"}]}},
  {"kind":"tea","title":"Вкусы в чае","status":"active","progress":0.125,"intention":"попробовать то, о чём прочитает о чае","last_event":null,"tea":{"tasted":3,"known":24}}],
 "embroidery":{"number":2,"motif":"Мотылёк","size":40,"rows":["....aa....", "…"],"palette":[{"thread":"бордо","color":"#8c1d2e"}],"stitched":340,"total":548},
 "tastes":[{"item":"бергамот","category":"аромат","verdict":"да","reason":"…","basis":"название заметки","source":"https://…","previous":null,"at":"ISO"}],
 "reading":{"title":"…","url":"https://…","opinion":"…","read_on":"2026-10-16","at":"ISO"},
 "mood":{"label":"ясное, с интересом","emotion":"interest","valence":0.3,"arousal":0.2}}
```

- **`pursuits`** — живые занятия (`active`/`paused`) в порядке вышивка, реставрация, чай; у приостановленного `intention=null`.
  - `last_event` — последнее событие этого вида занятий, её словами. `title` называет занятие события: после смены узора это может быть новый узор.
  - `restoration.stage` — номер этапа с 1, `step` — пройдено шагов из `steps`. При `step=0` реставрация ещё не начата. Звука нет: настоящая обработка записи — стадия P3.
- **`embroidery`** — схема текущей вышивки:
  - `rows[y][x]` — `.` (пусто) или метка `a`..`d` = `palette[0..3]`;
  - вышиты первые `stitched` меток в порядке чтения, строка за строкой слева направо;
  - `palette.thread` — нитка из состояния, `color` — только цвет для рисунка.
- **`tastes`** — текущие вердикты, до 12, свежие первыми.
  - `reason` — её слова; `basis` и `source` — настоящая заметка, на которой вердикт основан.
  - `previous` — прежний вердикт или `null`. Сам вердикт — часть образа.
- **`reading`** — последняя настоящая заметка за 7 дней или `null`.
- **`mood`** — настроение в личном контексте `arti`/этот человек/`default`/тема −1:
  - берётся только из `personal_state` этого владельца в этом контексте; группы и другие люди не читаются;
  - `label` = слово по медленному настроению (валентность и возбуждение: ясное, приподнятое, ровное, оживлённое, тихое, беспокойное) + доминирующая эмоция из фиксированного русского словаря («с интересом», «с грустью»…), без сгенерированного текста;
  - `valence` и `arousal` округлены до 0,1;
  - `null`, если разговора ещё нет, контекст не `active`, идёт пересборка, проекция другой версии модели, открыт разрыв с этим человеком (rupture, эффективная тяжесть ≥ `RUPTURE_CLOSED`) или доминирует гнев на него. Обида никогда не показывается.
- **`mentions:false`** — `in_character=false`, `pursuits=[]`, `embroidery=null`, `tastes=[]`; таблицы занятий при этом не читаются, остаются только `reading` и `mood`. При `ARTI_PURSUITS_ENABLED=0` так же, но `settings.mentions` остаётся выбором человека.

Миграция `060` аддитивна. Её применяет основной cognition runner при запуске бота; standalone runner `conversations/schema.py` её не применяет.