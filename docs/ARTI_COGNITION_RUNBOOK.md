# Эксплуатация когнитивной модели

## Установка и исходное состояние

Используются зависимости проекта из `requirements.txt`, PostgreSQL с pgvector
для совместимых legacy-таблиц, доступ на CREATE DATABASE для отдельного тестового
стенда и `OPENROUTER_API_KEYS` для нового интерпретатора. Ключи — только в `.env`
или окружении; отчёты не должны содержать credentials или рабочую переписку.

Настройки:

```dotenv
ARTI_COGNITION_MODE=shadow
ARTI_COGNITION_MODEL=stealth/space-bunny-alpha
```

`shadow` вычисляет отдельные проекции; старый путь обслуживает ответы.
`active` использует новое выражение/память и блокирует старые эмоциональные
mutators. Сохранённый explicit authority имеет приоритет над default для
конкретного контекста. `legacy` — глобальный откат: новый worker/интерпретатор
и новые намерения не исполняются, даже при сохранённых active-флагах.

Импорт модулей не запускает бота. Реальный запуск `python main.py` начинает
обслуживание Telegram и требует отдельного эксплуатационного решения.
Во время этой реализации бот не запускался, `.env` не переключался.

## Перенос и переключение

1. Сохранить обычную резервную копию и проверить доступ к ней. Не публиковать её.
2. Запустить offline проверки и перенос на локальной копии:

   ```powershell
   python -m tools.run_cognition_tests
   python -m tools.evaluate_mechanisms
   python -m tools.cognition_admin verify-copy
   ```

3. Остановить обработку на время переноса рабочей БД. Additive migrations
   применяются по номеру и checksum; редактировать применённый SQL нельзя.

   ```powershell
   python -m tools.cognition_admin migrate
   python -m tools.cognition_admin status
   ```

   Миграция использует фиксированный snapshot и checkpoint. Повтор продолжает
   тот же снимок. Неоднозначные записи идут в quarantine. Старые RP-сообщения
   остаются в сцене `legacy-unresolved`; они не становятся новой RP-сценой.
   Не реконструируются отсутствующее авторство, прошлые эмоции и обещания.

4. Выбранный уже существующий обычный контекст переключается явно:

   ```powershell
   python -m tools.cognition_admin authority --chat-id 123 --mode default --value active
   ```

   `123` — пример, заменить на согласованный chat ID. Для RP требуется также
   точный `--scene-id` из существующего контекста. Explicit switch и epoch fence
   не дают старой генерации продолжить доставку под новой authority.

5. Окно pilot: заранее зафиксировать 7 последовательных дней и состав чатов.
   Допуск для owner leak, source revival, двойных причинных эффектов и NaN — 0.
   Ежедневно сохранять только `status`/агрегаты, отмечать dead-letter,
   delivery_unknown, пропуски намерений, channel violations и субъективные
   замечания участников. Не подменять реальные дни виртуальной симуляцией.

6. После успешного окна и независимой оценки решить вопрос удаления физически
   оставшихся legacy-модулей. В active они уже лишены права начислять эффекты.
   Сейчас код сохранён для rollback; окно не объявлено пройденным.

## Диагностика и восстановление

```powershell
python -m tools.cognition_admin status
python -m tools.cognition_admin retry-rebuild --context-id 3
python -m tools.cognition_admin cancel-unknown --outbox-id 17
```

`status`: очереди/доставки по статусу, authority, возраст готового backlog,
просроченные leases, provenance gaps, owner violations, число rebuilding contexts.
Любой provenance gap/owner violation требует остановки active-контекста.

`retry-rebuild` применим к заблокированной незавершённой пересборке. Обычные jobs
и доставка ждут за durable barrier; исходные источники не возвращаются из tombstone.
Задание восстановления сохраняется до удаления. После трёх неудач требуется
операторский retry после устранения причины. Он не вызывает внешних сообщений.

`cancel-unknown` закрывает неоднозначный слот, не отправляя его повторно.
Сначала оператор проверяет реальный Telegram-результат. Между Telegram и SQL нет
общей транзакции; неизвестный исход нельзя автоматически объявить недоставленным.

Jobs: lease 300 с, renewal 40 с, до трёх job attempts; интерпретатор — до трёх
попыток по 90 с внутри job. Верхняя граница — девять попыток на observation.
Worker drain — 20 с. Не использовать приватный текст/исключения провайдера как
last_error; допустимы только фиксированные категории.

`/my_profile`, `/charge` читают владельца в актуальном контексте.
`/memory_archive тема` явно проверяет исходную запись.
`/forget тема` предлагает один источник или все найденные owned источники.
Bulk selection содержит только IDs/owner/chat/TTL, не сохраняет запрос в памяти.
Сам `/forget` не кодируется как новая автобиографическая реплика.

## Откат

```powershell
python -m tools.cognition_admin authority --chat-id 123 --mode default --value legacy
```

Для общего отката остановить приложение, выставить `ARTI_COGNITION_MODE=legacy`
и запустить снова. Новый провайдер для этого режима не требуется. Новые исходные
события/проекции остаются отдельной версией; numerical states не копируются
обратно в старый charge. Не отменяются уже доставленные сообщения. Удалённые
источники/производные не восстанавливаются из новых проекций при откате.

Для возврата в новый контур сменить global default и explicit authority нужных
контекстов. Schema/checkpoints/causal ledger не сбрасывать для повторного запуска.

## Повтор экспериментальных проверок

Offline numerical replay не расходует API:

```powershell
python -m tools.evaluate_full_cognition --split final_held_out --run-name frozen_final_v4
```

Live drivers — только синтетика:

```powershell
python -m tools.evaluate_full_cognition --split development --run-name next_development --live
python -m tools.evaluate_dialogues
python -m tools.evaluate_component_responses
```

Уже изученный final_held_out не является свежим hold-out для следующей настройки.
Новый научный цикл требует нового заранее выделенного корпуса и замороженных
критериев. `blind_packet.md` и `component_packet.json` допускают оценку людьми;
не показывать оценщикам отдельные файлы с ключами до завершения разметки.
Измерения модели не заменяют независимые человеческие оценки.
