"""Short Arti copy, purpose-first labels, no command syntax on normal screens."""
def nav(label, target):
    return label, dict(nav=target)


def form(label, kind, **data):
    return label, dict(form=kind, **data)


def command(label, text):
    return label, dict(command=text)


SECTIONS = {
    'home': ('🧭 <b>Арти · Меню</b>\n\nТак. Что делаем?', [
        [nav('✨ Создать', 'create'), nav('📎 Материалы', 'materials')],
        [nav('🗂 Проекты', 'projects'), nav('⏳ Задачи', 'tasks')],
        [nav('🤝 Совместная работа', 'collaboration'), nav('🔁 По расписанию', 'routines')],
        [nav('🧠 Память', 'memory'), nav('⚙️ Настройки', 'settings')],
        [nav('❔ Как пользоваться', 'help')]]),
    'create': ('✨ <b>Создать</b>\n\nВыбери результат. Дальше проведу по шагам.', [
        [command('🎨 Картинку', '/image'), command('🎬 Видео', '/video')],
        [command('🎵 Музыку', '/music'), form('📊 Инфографику', 'infographic')],
        [form('🛠 Многошаговую задачу', 'agent'), form('📚 Учебный сценарий', 'scenario_new')],
        [nav('🎙 Работа с голосом', 'voices')]]),
    'materials': ('📎 <b>Материалы</b>\n\nФайл → действие → проверяемый результат. Пришли файл, когда попрошу.', [
        [form('📋 Таблицы и качество', 'dataset'), nav('🧮 Посчитать по таблице', 'calculations')],
        [form('✏️ Исправить ячейку', 'datafix'), form('🔎 Найти в материалах', 'material_search')],
        [form('🎧 Расшифровать аудио', 'transcript'), form('🎞 Кадры видео', 'storyboard')],
        [form('⏱ Момент видео', 'moment'), form('📝 Моя оценка файла', 'material_review')]]),
    'calculations': ('🧮 <b>Посчитать по таблице</b>\n\nВыбери расчёт. Я попрошу файл и нужные ячейки.', [
        [form('Сумма', 'calc', operation='sum'), form('Среднее', 'calc', operation='mean')],
        [form('Минимум', 'calc', operation='min'), form('Максимум', 'calc', operation='max')],
        [form('Количество', 'calc', operation='count'), form('Доля в процентах', 'calc', operation='percent')],
        [form('Изменение', 'calc', operation='change'), form('Разница', 'calc', operation='difference')],
        [form('Отношение', 'calc', operation='ratio'), form('Сравнить', 'calc', operation='compare')],
        [form('Сверить', 'calc', operation='reconcile'), form('Перевести единицы', 'calc', operation='convert')]]),
    'collaboration': ('🤝 <b>Совместная работа</b>\n\nОбсуждаем варианты, выбираем и договариваемся о делах.', [
        [nav('🗳 Решения', 'decision'), nav('📌 Поручения', 'assignment')],
        [form('Предложить решение', 'decision_new'), form('Предложить поручение', 'assignment_new')]]),
    'routines': ('🔁 <b>Повторять и следить</b>\n\nУдачный способ работы можно сохранить и запускать по расписанию.', [
        [nav('🧩 Мои процедуры', 'procedure'), nav('🕒 Мои подписки', 'subscription')],
        [nav('📚 Учебные сценарии', 'learning')]]),
    'memory': ('🧠 <b>Память</b>\n\nПосмотреть, что я помню о тебе, или убрать лишнее.', [
        [command('Мой профиль', '/my_profile'), command('Моё состояние', '/charge')],
        [form('Найти исходную запись', 'archive'), form('Забыть выбранное', 'forget')]]),
    'settings': ('⚙️ <b>Настройки</b>\n\nМодель, режим общения и мои инициативы в группе.', [
        [command('🤖 Выбрать модель', '/model'), nav('🎭 Ролевой режим', 'roleplay')],
        [nav('💬 Участие в группе', 'group'), nav('🎨 Оформление', 'styles')],
        [nav('🔌 Подключения', 'connections'), nav('⚠️ Управление чатом', 'chat_control')]]),
    'styles': ('🎨 <b>Оформление результатов</b>\n\nСохраню выбор для твоих следующих работ в текущем проекте.', [
        [command('Светлое · спокойное', '/artifact style calm')],
        [command('Контрастное · чернила', '/artifact style ink')],
        [command('Тёмное · ночь', '/artifact style night')]]),
    'voices': ('🎙 <b>Голос и озвучка</b>\n\nДубляж, сохранённые голоса и озвучивание своим образцом.', [
        [command('Дублировать запись', '/dub'), command('Озвучить образцом', '/vclone')],
        [command('Мои голоса', '/voices'), command('Сохранить голос', '/voice_save')],
        [command('Удалить голос', '/voice_delete')]]),
    'roleplay': ('🎭 <b>Ролевой режим</b>\n\nВ личном чате можно открыть сцену и потом вернуться к обычному общению.', [
        [command('Открыть сцену', '/rp')],
        [('Закончить сцену', dict(confirm=dict(special='rp_off'), title='Закончить текущую сцену?'))],
        [command('🪨 Камень, ножницы, бумага', '/rps')]]),
    'chat_control': ('⚠️ <b>Управление чатом</b>\n\nЭти действия влияют на весь чат. В группе нужны права администратора.', [
        [('Включить ответы', dict(confirm=dict(command='/start'), title='Включить мои ответы в этом чате?'))],
        [('Выключить ответы', dict(confirm=dict(command='/stop'), title='Выключить мои ответы в этом чате?'))],
        [('Начать разговор заново', dict(confirm=dict(command='/clear_context'), title='Очистить текущий контекст разговора? Память останется.'))],
        [('Остановить медиа-запросы', dict(confirm=dict(command='/cancel'), title='Остановить активные медиа-запросы чата? Ролевая сцена тоже будет закрыта.'))]]),
    'connections': ('🔌 <b>Подключения</b>\n\nКалендарь, внешние задачи и хранилище пока не подключены.\nКогда подключение появится, перед записью покажу действие и попрошу разрешение.', []),
    'help': ('❔ <b>Как пользоваться</b>\n\n1. Выбери, что хочешь сделать.\n2. Нажимай кнопки и отвечай на вопросы.\n3. Файлы и готовые результаты появятся отдельно.\n\nВ группе отвечай именно на сообщение меню — чужой разговор не попадёт в форму.\n«Назад» отменяет текущий ввод. Меню можно снова открыть кнопкой Telegram или сообщением «Меню».', [
        [nav('Что умеют проекты', 'help_projects'), nav('Что умеют задачи', 'help_tasks')]]),
    'help_projects': ('🗂 <b>Проект</b>\n\nОдна цель, связанные файлы и результаты. Можно работать одному или вместе. Права участников задаёт владелец. Личные файлы переходят в группу только после явного выбора аудитории и подтверждения.', []),
    'help_tasks': ('⏳ <b>Задача</b>\n\nЯ сохраняю план и готовые шаги. Задачу можно приостановить и продолжить. Для внешнего действия покажу параметры и адресата. «Разрешить» относится только к показанному действию.', []),
}

PARENTS = dict(create='home', materials='home', calculations='materials', collaboration='home',
    routines='home', memory='home', settings='home', styles='settings', voices='create',
    roleplay='settings', chat_control='settings', group='settings', connections='settings',
    help='home', help_projects='help', help_tasks='help', projects='home', tasks='home',
    artifacts='projects', decision='collaboration', assignment='collaboration', procedure='routines',
    subscription='routines', learning='routines')
