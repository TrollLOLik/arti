# Реализация памяти и эмоций Арти

30 сентября 2026 года. Ветка `codex/cognitive-memory-emotion`.
Исходная ревизия: `840f7e3e3e7c5267f2e3c6481d448569c2a57a1f`.
Текущая модель: `cognition-2026-09-30.4`.

Код всех двадцати батчей B00–B19 реализован и подключён к приложению.
Результаты инженерных проверок приведены ниже. Реальное окно pilot и оценки
людей ещё не проведены: экспериментальную часть G5 нельзя считать пройденной.
После перехода 02.10.2026 production runtime всегда использует новую модель: `active`.
Старые результаты shadow/rollback ниже относятся к историческому этапу разработки.
Актуальное переключение и выбор модели описаны в `ARTI_COGNITION_RUNBOOK.md`.

## Покрытие батчей

| Батч | Реализация | Проверенное поведение |
|---|---|---|
| B00 | `contract.json`, синтетические корпуса, fake clock, evaluation drivers | Фиксированные критерии/версии/seeds, baseline и сохранённые восприятия, бюджеты и измерения |
| B01 | `memory/storage.py`, consolidator, models, generation, stickers | Нет самoархивирования и потери источников непринятой замены; owner-aware dedup; прямой поиск без cooldown; verified Wiki; целый промпт |
| B02 | `types.py`, repositories, SQL 001–007 | Неизменяемый transport source, owner/mode/scene/version; CAS, происхождение и строгая область доступа |
| B03 | `runtime.py`, `jobs.py`, `worker.py`, `delivery.py`, lifecycle в main | Два экземпляра используют одну lease; jobs восстанавливаются; квитанция отделена от генерации; неоднозначный send не повторяется |
| B04 | `forgetting.py`, epochs/dependencies, history, commands, logging | Удаление после replay; invalidation зависимого восприятия и собственных действий; пересчёт; блокировка/восстановление незаконченной пересборки; сохранность другого owner |
| B05 | `situations.py`, interpreter, динамические цели | Spans проверяются по raw; цитата/гипотеза/рассказ/доставка различаются; предпочтение и запрос не становятся завершённым успехом; цели индивидуальны |
| B06 | `affect.py`, bounded working state/residues | 11 совместимых адресных реакций, аналитическое затухание и mood, concerns/effort/circadian; частота polling не меняет динамику |
| B07 | `reappraisal.py`, ledger replay | Новое объяснение заменяет причинную трактовку, сохраняет исходное свидетельство, пересчитывает состояние; learned expectation меняет surprise |
| B08 | `regulation.py`, ExpressionPlan, TTS/sticker/text adapters | Уточнение, работа с причиной, переоценка, удержание выражения, восстановление внимания; выражение не начисляет настроение; канал учитывает предпочтения |
| B09 | `relationships.py`, personal_state, implicit expression bias | Familiarity отделена от reliability/benevolence; контакт не даёт доверия; собственная ошибка не снижает надёжность пользователя; ассоциация влияет ограниченно |
| B10 | `memory_repository.py`, selective details, episodes | Раздельные gist/name/place/date/wording; topic/gap boundaries; before/after по личной причине; отдельные времена и provenance |
| B11 | Lexical/vector retrieval, typed graph, prompting | Контекст/owner/версии; два шага spreading activation; familiarity; counterexample slot; candidate/recalled/included/expressed различаются |
| B12 | `memory_dynamics.py`, explicit archive | Доступность, точность, vividness и уверенность различаются; spacing/interference; недоступная дата маскируется в gist; архив находит не закодированный хвост raw |
| B13 | Условные beliefs/versions/support groups, старый consolidator | Новое актуальное значение сохраняет историю; condition/exception; повтор одной декларации не создаёт свидетелей; атомарные проекции |
| B14 | Reconstruction, memory_version, causal revisions | Сохраняются исходная запись, субъективная версия и текущая трактовка; проверенная запись отделена от обычного воспоминания |
| B15 | Autobiography/projects/rituals, intentions, scheduler | Собственные обещания возникают после доставленного действия; отмена закрывает то же намерение; deadline требует часовой пояс; напоминание доставляется один раз |
| B16 | Bounded replay/consolidation jobs | Ограничены частота, число traces и сила; разнообразие тем; укрепление доступности и semantic links; нет роста истинности/доверия от replay |
| B17 | `migration.py`, admin verify-copy, chunks/timeline checkpoints | Фиксированный snapshot, ascending ID, resume и повтор без дублей; неизвестное авторство/неподтверждённые производные идут в quarantine |
| B18 | Mechanisms, rich perception, dialogues, blind/component drivers | E1–E10/M1–M5, абляции, длинные траектории, независимый финальный набор и реальные вызовы OpenRouter на синтетике |
| B19 | Context authority, mutation gates, monitoring/admin, runbook | Active блокирует старые причинные начисления; локальный/global rollback проверены; legacy работает без нового провайдера; jobs/outbox/repair наблюдаемы |

Конкретные модули находятся в `cognition/`; интеграция — в `main.py`,
`bot/handlers.py`, `bot/queue.py`, `bot/commands.py`, `bot/retry_bot.py`,
`ai/generation.py`, `ai/stickers.py` и `utils/chat_history.py`.
Проверки — `tests/cognition/`, legacy-регрессии — `tests/test_memory_regressions.py`.

## Измеренные результаты

- [Автоматические проверки](evaluation/automated_tests_full.json): **450/450**,
  без skip, с изолированной PostgreSQL и подменёнными провайдерами.
  Точное число, время и версия находятся в JSON отчёте.
- [Механизмы](evaluation/mechanisms.json): **15/15** контрастов E1–E10/M1–M5.
  3000 событий, **64,58 виртуального дня**; рабочие эмоциональные эпизоды ≤128.
  p95 чистого перехода около **0,13 мс**; 124 события через БД/оркестратор —
  p95 около **23,4 мс**, без LLM и транспорта. Это отдельные измерения.
- [Development](evaluation/full_development_full_v3_live.json): **24/24** схем
  и критериев, 35 попыток, p50 11,32 с / p95 38,06 с.
- [Первый rich held-out](evaluation/full_held_out_frozen_full_v3_live.json):
  **16/16** схем, **14/16** критериев. Два провала сохранены: несовпадение
  ситуации с аннотацией для иронии и поддержки подруги; границы импульса прошли.
  Этот набор после просмотра не используется как новый отложенный эксперимент.
- [Новый финальный held-out](evaluation/full_final_held_out_frozen_final_v4_live.json):
  **16/16**, 20 попыток, p50 **25,12 с**, p95 **53,48 с**.
  Корпус и критерии написаны до первого вызова. После дополнения механики
  learned expectations выполнен [повтор сохранённых восприятий](evaluation/full_final_held_out_frozen_final_v4_recorded.json):
  **16/16**. Контрольные суммы ядра обоих снимков сохранены; live-отчёт не переписан.
- [Многоходовые диалоги](evaluation/dialogues_frozen_v4_live.json): **11/11**
  проверок, 12 завершённых разборов / 17 попыток. Реальные вызовы выбранной модели
  прошли через runtime и disposable PostgreSQL; Telegram заменён fake transport.
- [Слепые пары](evaluation/blind_automated.json): 8 синтетических пар, полный
  вариант выиграл 6, один раз равенство, один раз выиграла sentiment-ablation.
  Это оценка модели, не людей. [Пакет для людей](evaluation/blind_packet.md)
  и отдельный ключ готовы; независимые человеческие оценки отсутствуют.
- [Четыре варианта](evaluation/component_automated.json) — repaired legacy adapter,
  appraisal-only, memory-only, full — проверены на четырёх контекстах (23 вызова).
  Воспроизведение: `tools/evaluate_component_responses.py`. Это контролируемая
  проверка адаптеров на одинаковых синтетических контекстах; её нельзя выдавать
  за replay исторического production-бота. [Ответы](evaluation/component_packet.json)
  и ключ/оценки сохраняются отдельно. У трёх вызовов неизвестна стоимость.
- [Миграция локальной копии](evaluation/migration_copy.json): snapshot max ID 199;
  **189 raw rows** обработаны с остановкой после 11 и последующим resume;
  **67** допустимых источников импортировано, **396** сообщений/производных
  помещено в quarantine, всего 463 mapped records. Повтор импортировал **0**.
  После переноса нет provenance gaps, owner violations, просроченных leases и
  незаконченных rebuild. Рабочая БД читалась согласованной read-only транзакцией.

Во всех live-проверках использованы только синтетические события и разрешённая
модель `stealth/space-bunny-alpha`. Рабочая переписка не отправлялась в тестовый
API. Reported cost завершённых вызовов — 0 там, где это сообщил провайдер;
это не гарантия цены. В blind report четыре попытки имеют неизвестную стоимость.
Неудачные предварительные запуски dialogue harness описаны
[отдельно](evaluation/dialogue_harness_preflight.json): учёт некоторых отменённых
внешних попыток неполон. Финальные метрики не объявляются общим расходом сессии.

## Практические ограничения

Embedding нового ядра — воспроизводимая subword-проекция 192 измерений, с
lexical search и typed graph. Это не обученная нейронная embedding-модель.
Формулы, priors и коэффициенты — инженерные гипотезы, не параметры,
оценённые по человеческим экспериментам. Абляции проверяют вклад механизмов,
не подтверждают биологическую достоверность или субъективные переживания.

Автоматическая отметка expressed доказывает только буквальное использование
доступного текста в доставленном ответе. Парафразы не объявляются проверенными.
Для медиа без доступной текстовой квитанции сохраняется факт канала/доставки;
содержание аудиофайла не объявляется независимо проверенным.

Проверенное забывание охватывает хранилища приложения и связанные источники.
Неизвестные legacy-производные инвалидируются; отсутствующее происхождение
не выдумывается. Исторические внешние логи/бэкапы не сертифицируются как очищенные.
Уже отправленное сообщение не исчезает из Telegram от удаления памяти.
Telegram не даёт общей транзакции с PostgreSQL: при неоднозначной доставке
система блокирует автоматический повтор и сохраняет delivery_unknown.
Очередь генерации ответа до создания outbox остаётся оперативной; после crash
вход/когнитивные jobs сохраняются, но сам недописанный ответ не обещается доставить.

## Контрольные точки и эксплуатация

G0–G3 закрыты кодом и инженерными сценариями. Для G4 получены синтетические
проверки полной модели и перенос на копии; ограничения интерпретатора и ресурсы
зафиксированы. Код B19 готов, rollback отрепетирован, старые mutators блокируются
в active-контекстах. Старые рабочие обработчики удалены/retired; исследовательский baseline
сохранён только в tools и допускает SQL лишь в disposable test DB.

Не проведены: реальное наблюдение в выбранных чатах и слепая оценка людьми.
Пакет, критерии, команды и схема сбора показателей готовы в
[операционном руководстве](ARTI_COGNITION_RUNBOOK.md). Реальный Telegram-бот
в исходном исследовательском этапе не запускался. 02.10.2026 рабочая БД переведена
в active: четыре контекста, без изменения источников и когнитивного состояния.
Переход, проверки выбранных моделей и запуск отражены в
[отчёте переключения](evaluation/cognition_cutover.json).

```powershell
python -m tools.run_cognition_tests
python -m tools.evaluate_mechanisms
python -m tools.evaluate_full_cognition --split final_held_out --run-name frozen_final_v4
python -m tools.cognition_admin verify-copy
```

У первых трёх команд нет вызовов тестового провайдера. Последняя создаёт локальную
копию в `arti_cognition_copy_<uuid>` и удаляет ровно её после проверки.
Существующие неподтверждённые данные не превращаются в достоверную историю Арти.


## MEMORY correctness audit follow-up (2026-10-02 UTC)

- Query-aware current beliefs follow correction lineage beyond the last-16 window; historical sources keep supersession and validity metadata.
- Source observation/event timestamps, uncertainty and author/modality reach final prompts without inferring narrated-event dates.
- A separate public observed-source path retains exact audience, scene, retention, opt-out and erasure fences, with inclusion/send-time revalidation. Private owner filters are unchanged.
- Recall accessibility and source-aged fidelity are separated; empty retrieval and replay cannot reset quality.
- Existing long prefix-only traces gain offset-keyed local semantic windows and a bounded raw-source lexical fallback, preserving quotation framing and original-age decay.
- Final exact aggregate: 838/838 passed; real pinned MiniLM run: 8/8 passed; 48 new regressions. No providers, production data or real Telegram traffic.
- Remaining boundaries: public recall is lexical; maximum-length sources use sampled semantic windows; live interpretation and answer quality were not evaluated. Details and verification commands: [MEMORY_CORRECTNESS.md](MEMORY_CORRECTNESS.md).
