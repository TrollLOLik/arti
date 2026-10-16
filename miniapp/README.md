# Арти · Telegram Mini App

Реализованы MA00–MA04 по [плану](../docs/ARTI_MINIAPP_IMPLEMENTATION_PLAN.md): клиент принятого дизайна v3, Telegram-вход, отзываемые сессии, четыре раздела, настоящий текстовый чат с Арти и общая личная история с Telegram через существующий cognitive runtime и durable workers.

API и границы описаны в [контракте](../docs/ARTI_MINIAPP_API.md), проверки — в [прогрессе](../docs/ARTI_MINIAPP_PROGRESS.md). Галерея `mockup/` остаётся визуальным эталоном. Клиент находится в `miniapp/frontend/`; старые файлы `miniapp/static/` сервером не раздаются.

## Что работает сейчас

- Проверка Telegram initData на сервере, сессия в HttpOnly cookie, CSRF, срок действия, вращение и logout.
- Четыре настоящих раздела: Чаты / Создать / Пространство / Профиль. Light/dark/system, безопасные отступы Telegram, BackButton и состояния загрузки/ошибки.
- Создание одного основного личного чата на проверенного пользователя; чтение только своих бесед и сообщений.
- Отправка с client UUID и history_revision, очередь/ответ/ошибка, явный безопасный повтор, SSE и polling, сохранение места чтения и загрузка предыдущих сообщений.
- Общий private/default контекст с Telegram, идемпотентный перенос допустимой истории, атомарная публикация ответа с UUID receipt. Независимая Telegram-копия не создаёт второго когнитивного ответа; неизвестная внешняя доставка не повторяется вслепую.
- Reset и удаление источника стирают app-проекцию и отменяют связанную работу. Сервер отклоняет запрос из предыдущей ревизии даже после очистки пустой беседы.
- Серверная модель сообщений с устойчивым seq, идемпотентностью, reply в пределах видимого чата и атомарной записью события/outbox. Основы членства/отзыва и нативной идентичности комнаты доступны только внутреннему сервису.
- Существующие guarded API проектов, файлов и органайзера используют новую сессию. Клиент «Пространства» пока предоставляет только чтение поддерживаемых данных.
- Профиль показывает текущего пользователя, оформление сохраняется на устройстве, выход отзывает доступ на сервере.

Live-отправка включается только при подключённом active runtime. В preview composer отключён, данные пустые и ответы не имитируются. Переписки людей и комнаты — MA05–MA06; голос, вложения, генерация медиа и агентские карточки — следующие пакеты. Типизированная UUID-основа native room уже хранит отдельное начальное cognitive state, но room inference закрыт до MA06. Старый дополнительный `/api/handoff` сохранён для совместимости.

## Воспроизводимая локальная среда

Проверено: Python 3.12.14, Node 24.15.0, npm 11.12.1, PostgreSQL 18. Основные зависимости Python зафиксированы в `requirements.lock`, клиентские — в `frontend/package-lock.json`. Корневой бот продолжает использовать свои зависимости.

Старые `.venv`/`venv` этой копии указывают на отсутствующий Python310; они сохранены. Для HTTP/DB-проверок используется отдельное `.venv-miniapp`. На другой машине создать его через установленный Python 3.12:

```powershell
py -3.12 -m venv .venv-miniapp
.\.venv-miniapp\Scripts\python.exe -m pip install -r miniapp/requirements.lock
```

Из `miniapp/frontend/`:

```powershell
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

Из корня проекта:

```powershell
.\.venv-miniapp\Scripts\python.exe -m miniapp --demo --port 8767
```

Открыть [локальный клиент](http://127.0.0.1:8767/) и нажать «Открыть локальный просмотр». Нужен предварительный `npm run build`. Без сборки сервер возвращает явный 503, а не старую страницу.

Preview требует `--demo`, слушает только loopback и не читает `.env`, PostgreSQL или AI. Начальные списки пусты. Каждая сессия изолирована; данные исчезают после истечения сессии/перезапуска. Это режим проверки интерфейса, не Telegram-аккаунт и не копия рабочей истории. Существующая статичная галерея на 8766 независима.

Для разработки клиент можно запустить `npm run dev`: Vite proxy по умолчанию ожидает Python на 8765; запустить demo с этим портом либо изменить локальный proxy. Vite не предназначен для производственного Telegram HTTPS-origin.

## Проверки

```powershell
.\.venv-miniapp\Scripts\python.exe -m pip install -r miniapp/requirements-integration.lock
.\.venv-miniapp\Scripts\python.exe -m unittest discover -s tests/miniapp -v
$env:ARTI_TEST_DB='1'
.\.venv-miniapp\Scripts\python.exe -m unittest discover -s tests/conversations -v
.\.venv-miniapp\Scripts\python.exe -m unittest tests.cognition.test_request_codec -v
.\.venv-miniapp\Scripts\python.exe -m unittest tests.conversations.test_live_chat tests.miniapp.test_live_http tests.cognition.test_request_store tests.cognition.test_request_runtime tests.cognition.test_request_delivery tests.cognition.test_organizer_ownership tests.cognition.test_cutover -v
```

DB-тесты читают только параметры подключения (переменные окружения или локальный `.env`), создают отдельную `arti_miniapp_test_<uuid>` либо `arti_cognition_test_<uuid>` и удаляют только её. Схема/история рабочей базы не инициализируется и не мигрирует. Нужны запущенный PostgreSQL и право создавать тестовую БД. При отсутствии `ARTI_TEST_DB=1` DB-сценарии пропускаются с явной причиной. Данные синтетические; Telegram и платные AI-провайдеры не вызываются.

Pillow в `requirements-test.txt` требуется существующему codec regression через подсистему материалов. Сам HTTP/domain импорт не требует ключей или создания AI-клиентов.

Для браузерного сквозного теста настоящих API/SQL/worker с заменёнными провайдерами есть отдельный opt-in модуль:

```powershell
$env:ARTI_TEST_DB='1'
$env:ARTI_MINIAPP_LIVE_FIXTURE='1'
.\.venv-miniapp\Scripts\python.exe -m tests.miniapp.serve_live_fixture
```

Он слушает только 127.0.0.1:8780, автоматически входит под синтетическим пользователем и удаляет собственную тестовую БД при Ctrl+C. Это тестовый инструмент с явным обходом внешней авторизации, не backend для развёртывания. В ответах прямо указана подмена модели. `/__fixture` меняет только синтетические Telegram-события/reset этой БД. Отдельная клиентская fixture описана в `frontend/README.md`.

## Подключение рабочего Telegram-входа

Обычный opt-in listener запускается существующим `main.py`, разделяет pool/event loop бота и не создаёт второй poller. Нужен собранный клиент и доступный HTTPS-origin, например по `Caddyfile.example`:

```dotenv
ARTI_MINIAPP_ENABLED=true
ARTI_MINIAPP_URL=https://arti.example.com
ARTI_MINIAPP_HOST=127.0.0.1
ARTI_MINIAPP_PORT=8765
```

Подписанные данные проверяются по текущему токену бота. Аддитивные миграции 055–058 обнаружит общий cognition runner при следующем обычном старте бота, включая случай отключённого UI. Startup Mini App также проверяет foundation через тот же checksum ledger/lock, без повторного применения. Старые миграции не меняются. Материалы зависят от `ARTI_MATERIALS_ENABLED`. Основная беседа разрешается в реальный private context пользователя, прежние material/project scope-хеши сохранены. Listener и mirror worker имеют явный lifecycle cleanup; второго Telegram poller нет.

В этом пакете рабочий бот не перезапускался, производственная миграция и настройка BotFather не выполнялись, `.env` не менялся. Реальные WebView/HTTPS и ограничения cookies Telegram Web проверяются на этапе интеграции/пилота; локальные тесты подписи не заявляются проверкой на устройстве.

## Дизайн

Принятая концепция 04 «Левая рука» ([mockup/README.md](../mockup/README.md), [прототип](../mockup/index.html)): день «бумага» и вечер «лампа», Cormorant Garamond для заголовков, Onest для интерфейса, IBM Plex Mono для машинных подписей и Caveat для её почерка, красная лента-закладка у активного раздела. Шрифты и портрет раздаются локально. React-компоненты используют настоящую адаптивную раскладку без рамок телефона и статичных примеров истории; состояние набора текста берётся только из реального состояния запроса. Элементы без серверных данных (реакции, «написала первой», Комната и Тетрадь Арти, вышивка, проявка) ждут MA07–MA10. Предыдущая основа v3 и источники изображений сохранены в [mockup/archive/v3/README.md](../mockup/archive/v3/README.md).
