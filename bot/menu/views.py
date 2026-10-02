"""Conversation first; saved work and technical controls live one level deeper."""
def nav(label, target):
    return label, dict(nav=target)


def form(label, kind, **data):
    return label, dict(form=kind, **data)


def command(label, text):
    return label, dict(command=text)


SECTIONS = {
    'home': ('🧭 <b>Арти</b>\n\nЯ здесь. Можешь просто написать мне.\nА если хочется что-нибудь придумать вместе — выбирай.', [
        [nav('💬 Поболтаем', 'talk'), nav('✨ Придумаем', 'create')],
        [nav('🎭 Истории и игры', 'roleplay'), nav('🎙 Голос', 'voices')],
        [nav('🧠 Наша память', 'memory'), nav('🧰 Помоги с делом', 'work')],
        [nav('⚙️ Как общаемся', 'settings')]]),
    'talk': ('💬 <b>Поболтаем</b>\n\nМожно без повода и без задания. С чего начнём?', [
        [nav('Расскажу про свой день', 'talk_day')],
        [nav('Нужен взгляд со стороны', 'talk_thought')],
        [nav('Вернёмся к прошлой теме', 'talk_return')],
        [nav('📷 Покажу кое-что', 'share')]]),
    'talk_day': ('💬 <b>Я слушаю</b>\n\nЧто сегодня запомнилось? Можно начать с одной мелочи — или рассказать всё как есть. Просто напиши в чат.', []),
    'talk_thought': ('💬 <b>Давай разберёмся</b>\n\nРасскажи, что крутится в голове. Если нужен совет — скажи. Если хочется просто выговориться — тоже.', []),
    'talk_return': ('💬 <b>Продолжим?</b>\n\nНапомни, к какой теме вернёмся. Если не вспомню детали, лучше спрошу, чем придумаю их.', []),
    'share': ('📷 <b>Покажи</b>\n\nПришли фото, запись или видео и добавь, чем хочешь поделиться. Можно обсудить впечатление, спросить про деталь или придумать что-нибудь по мотивам.', []),
    'create': ('✨ <b>Придумаем что-нибудь</b>\n\nКартинку по нашей идее, короткое видео или музыку под настроение. Если идея ещё не сложилась — сначала обсудим её в чате.', [
        [command('🎨 Картинку', '/image'), command('🎬 Видео', '/video')],
        [command('🎵 Музыку', '/music')],
        [nav('💬 Сначала обсудим идею', 'talk')]]),
    'work': ('🧰 <b>Помоги с делом</b>\n\nЗдесь можно сохранить общую цель, разобрать файлы или поручить мне работу с несколькими шагами.', [
        [form('🛠 Поручить дело', 'agent'), nav('⏳ Задачи', 'tasks')],
        [nav('🗂 Проекты', 'projects'), nav('📎 Разобрать материалы', 'materials')],
        [form('📊 Инфографику', 'infographic'), nav('🤝 Договориться вместе', 'collaboration')],
        [nav('🔁 Повторять по расписанию', 'routines')],
        [nav('🎨 Оформление результатов', 'styles'), nav('🔌 Подключения', 'connections')],
        [nav('❔ Как устроена работа', 'help_work')]]),
    'materials': ('📎 <b>Разберём материалы</b>\n\nДля точного расчёта, поиска в файлах или разбора записи. Если хочешь просто обсудить фото или видео — пришли его в обычный разговор.', [
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
    'memory': ('🧠 <b>Наша память</b>\n\nПосмотреть, что я помню о тебе, найти прошлый разговор или убрать то, что не стоит хранить. Найденное для удаления сначала покажу тебе.', [
        [command('Что я помню о тебе', '/my_profile')],
        [form('Найти прошлый разговор', 'archive'), form('Убрать из памяти', 'forget')]]),
    'settings': ('⚙️ <b>Как общаемся</b>\n\nНастройки разговора и моего участия в группе. Здесь показываю только то, чем ты можешь управлять.', [
        [command('🤖 Выбрать модель', '/model')],
        [nav('💬 Участие в группе', 'group')],
        [nav('⚠️ Управление чатом', 'chat_control')],
        [nav('❔ Как пользоваться', 'help')]]),
    'diagnostics': ('🔍 <b>Диагностика</b>\n\nТехнический просмотр эмоционального состояния. Доступен только пользователям с отдельным разрешением.', [
        [command('Состояние Арти', '/charge')]]),
    'styles': ('🎨 <b>Оформление результатов</b>\n\nСохраню выбор для твоих следующих работ в текущем проекте.', [
        [command('Светлое · спокойное', '/artifact style calm')],
        [command('Контрастное · чернила', '/artifact style ink')],
        [command('Тёмное · ночь', '/artifact style night')]]),
    'voices': ('🎙 <b>Можно голосом</b>\n\nЗапиши голосовое прямо в чат — разберём его в разговоре. Для озвучки текста или дубляжа есть отдельные инструменты.', [
        [nav('💬 Хочу поговорить голосом', 'voice_chat')],
        [nav('🎧 Озвучка и мои голоса', 'voice_tools')]]),
    'voice_chat': ('🎙 <b>Я слушаю</b>\n\nПришли голосовое, как если бы мы разговаривали. Кнопки выбирать не нужно.', []),
    'voice_tools': ('🎧 <b>Озвучка и мои голоса</b>\n\nОзвучить текст, дублировать запись или выбрать сохранённый голос. Проведу по шагам.', [
        [command('Дублировать запись', '/dub'), command('Озвучить образцом', '/vclone')],
        [command('Мои голоса', '/voices'), command('Сохранить голос', '/voice_save')],
        [command('Удалить голос', '/voice_delete')]]),
    'roleplay': ('🎭 <b>Истории и игры</b>\n\nМожем разыграть сцену в личном чате или сыграть короткую партию здесь.', [
        [command('Открыть сцену', '/rp')],
        [command('🪨 Камень, ножницы, бумага', '/rps')],
        [nav('📚 Истории и квесты по этапам', 'learning')]]),
    'chat_control': ('⚠️ <b>Управление чатом</b>\n\nЭти действия влияют на весь чат. В группе нужны права администратора.', [
        [('Включить ответы', dict(confirm=dict(command='/start'), title='Включить мои ответы в этом чате?'))],
        [('Выключить ответы', dict(confirm=dict(command='/stop'), title='Выключить мои ответы в этом чате?'))],
        [('Начать разговор заново', dict(confirm=dict(command='/clear_context'), title='Очистить текущий контекст разговора? Память останется.'))],
        [('Остановить медиа-запросы', dict(confirm=dict(command='/cancel'), title='Остановить активные медиа-запросы чата? Ролевая сцена тоже будет закрыта.'))]]),
    'connections': ('🔌 <b>Подключения</b>\n\nКалендарь, внешние задачи и хранилище пока не подключены.\nКогда подключение появится, перед записью покажу действие и попрошу разрешение.', []),
    'help': ('❔ <b>Как со мной общаться</b>\n\nПросто пиши, присылай голосовые или делись тем, что заметил. Меню нужно, когда хочется выбрать конкретное действие.\n\nВ группе обратись ко мне по имени или ответь на моё сообщение. Если открыта форма, отвечай именно на её панель.\n\n«Назад» отменяет ввод. Сообщение «Меню» открывает новую панель внизу чата.', [
        [nav('🧰 Если нужна помощь с делом', 'help_work')]]),
    'help_work': ('🧰 <b>Когда нужно довести дело</b>\n\nВ «Помоги с делом» можно объединить файлы и результаты в проект, следить за шагами задачи и повторять проверенный способ по расписанию. Обычному разговору проект не нужен.', [
        [nav('Что умеют проекты', 'help_projects'), nav('Что умеют задачи', 'help_tasks')]]),
    'help_projects': ('🗂 <b>Проект</b>\n\nОдна цель, связанные файлы и результаты. Можно работать одному или вместе. Права участников задаёт владелец. Личные файлы переходят в группу только после явного выбора аудитории и подтверждения.', []),
    'help_tasks': ('⏳ <b>Задача</b>\n\nЯ сохраняю план и готовые шаги. Задачу можно приостановить и продолжить. Для внешнего действия покажу параметры и адресата. «Разрешить» относится только к показанному действию.', []),
}

PARENTS = dict(talk='home', talk_day='talk', talk_thought='talk', talk_return='talk', share='talk',
    create='home', work='home', materials='work', calculations='materials', collaboration='work',
    routines='work', memory='home', settings='home', styles='work', voices='home', voice_tools='voices',
    voice_chat='voices', roleplay='home', chat_control='settings', group='settings', connections='work',
    diagnostics='settings', help='settings', help_work='work', help_projects='help_work', help_tasks='help_work', projects='work', tasks='work',
    artifacts='projects', decision='collaboration', assignment='collaboration', procedure='routines',
    subscription='routines', learning='roleplay')

# These screens offer a conversational opening, not a form or a synthetic user turn.
CONVERSATION_SCREENS = frozenset(('talk', 'talk_day', 'talk_thought', 'talk_return', 'share', 'voice_chat'))
COMMAND_PARENTS = dict(image='create', video='create', music='create', dub='voice_tools',
    vclone='voice_tools', voices='voice_tools', voice_save='voice_tools', voice_delete='voice_tools',
    model='settings', my_profile='memory', memory_archive='memory', forget='memory', charge='diagnostics',
    rp='roleplay', rps='roleplay', project='projects', artifact='artifacts', task='tasks',
    dataset='materials', calc='calculations', datafix='materials', transcript='materials', listen='materials',
    transcript_fix='materials', storyboard='materials', moment='materials', materials_find='materials',
    material_review='materials', decision='decision', assignment='assignment', procedure='procedure',
    subscription='subscription', scenario='learning', proactivity='group', quiet='group',
    start='chat_control', stop='chat_control', clear_context='chat_control', cancel='chat_control')


def form_parent(kind):
    if kind in ('agent', 'infographic'):
        return 'work'
    if kind in ('archive', 'forget'):
        return 'memory'
    if kind.startswith('group_'):
        return 'group'
    for prefix, parent in (('project_', 'projects'), ('artifact_', 'artifacts'), ('task_', 'tasks'),
                           ('scenario_', 'learning'), ('decision_', 'decision'), ('assignment_', 'assignment'),
                           ('procedure_', 'procedure'), ('subscription_', 'subscription')):
        if kind.startswith(prefix):
            return parent
    return COMMAND_PARENTS.get(kind, 'materials')
